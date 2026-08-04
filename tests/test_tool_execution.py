from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import urllib.request
from itertools import count
from pathlib import Path

import pytest

from app.domain.models import Artifact, ToolDefinition, ToolInvocationRequest, ToolInvocationResult
from app.repositories.delivery_outbox import InMemoryDeliveryOutboxRepository
from app.repositories.observation import InMemoryObservationEventRepository
from app.repositories.artifacts import InMemoryArtifactRepository
from app.repositories.connector_bindings import InMemoryConnectorBindingRepository
from app.repositories.evidence_objects import InMemoryEvidenceObjectRepository
from app.repositories.tool_registry import InMemoryToolRegistryRepository
from app.services.channel_runtime import ChannelRuntimeService
from app.services.browseract_ui_service_catalog import browseract_ui_service_by_service_key
from app.services.browseract_ui_template_catalog import browseract_ui_template_spec
from app.services.evidence_runtime import EvidenceRuntimeService
from app.services.orchestrator import RewriteOrchestrator, build_default_orchestrator
from app.services.provider_registry import CapabilityRoute, ProviderBinding, ProviderCapability, ProviderRegistryService
from app.services.responses_upstream import UpstreamResult
from app.services.tool_execution import (
    CONNECTOR_DISPATCH_IDEMPOTENCY_POLICY,
    CONNECTOR_DISPATCH_OPTIONAL_INPUT_FIELDS,
    CONNECTOR_DISPATCH_REQUIRED_INPUT_FIELDS,
    ToolExecutionError,
    ToolExecutionService,
)
from app.services.tool_execution_browseract_adapter import BrowserActToolAdapter
from app.services.tool_execution_gemini_vortex_adapter import GeminiVortexToolAdapter
from app.services.tool_execution_teable_adapter import TeableToolAdapter
from app.services.tool_runtime import ToolRuntimeService
from scripts.property_tour_layout_contract import source_geometry_projection


def _tool_execution_service(*args, **kwargs) -> ToolExecutionService:
    kwargs.setdefault("provider_registry", ProviderRegistryService())
    return ToolExecutionService(*args, **kwargs)


def _stub_mootion_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    monkeypatch.setattr(
        "app.mootion_remote_asset_policy.socket.getaddrinfo",
        lambda host, port, type=0: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )


def _approved_crezlo_writer_acceptance(*, floorplan_required: bool = False) -> dict[str, object]:
    acceptance: dict[str, object] = {
        "accepted": True,
        "spatial_provenance_verified": True,
        "exact_property_provenance_verified": True,
        "browser_receipt_verified": True,
        "scene_graph_connected": True,
        "all_required_scenes_navigable": True,
        "first_party_viewer_verified": True,
        "provider_control_route_verified": True,
        "floorplan_required": floorplan_required,
    }
    if floorplan_required:
        acceptance.update(
            {
                "floorplan_alignment_verified": True,
                "floorplan_layout_receipt_verified": True,
                "floorplan_geometry_projection_verified": True,
            }
        )
    return acceptance


def _approved_crezlo_source_provenance() -> dict[str, object]:
    return {
        "schema": "propertyquarry.crezlo_source_provenance.v1",
        "status": "pass",
        "provider": "crezlo",
        "hosted_url": "https://ea-property-tours-20260320.crezlotours.com/tours/verified",
        "capture": {
            "representation_kind": "captured_360",
            "scene_count": 3,
            "covered_space_count": 3,
            "navigation_hotspot_count": 2,
            "scene_graph_connected": True,
            "all_scenes_reachable": True,
        },
    }


def test_tool_execution_service_executes_builtin_artifact_repository_handler() -> None:
    artifacts = InMemoryArtifactRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(tool_runtime=tool_runtime, artifacts=artifacts)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-1",
            step_id="step-1",
            tool_name="artifact_repository",
            action_kind="artifact.save",
            payload_json={
                "source_text": "draft note",
                "expected_artifact": "rewrite_note",
                "plan_id": "plan-1",
                "plan_step_key": "step_artifact_save",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "artifact_repository"
    assert result.action_kind == "artifact.save"
    assert result.receipt_json["handler_key"] == "artifact_repository"
    assert result.receipt_json["invocation_contract"] == "tool.v1"
    assert result.output_json["artifact_kind"] == "rewrite_note"
    assert len(result.artifacts) == 1
    saved = artifacts.get(result.target_ref)
    assert saved is not None
    assert saved.content == "draft note"
    assert saved.principal_id == "exec-1"


def test_tool_execution_service_sends_rendered_video_to_telegram_when_audio_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = InMemoryArtifactRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    tool_runtime.upsert_connector_binding(
        principal_id="exec-video-send",
        connector_name="telegram_identity",
        external_account_ref="42",
        auth_metadata_json={"default_chat_ref": "42", "bot_key": "default", "bot_handle": "operator_concierge_bot"},
        scope_json={"assistant_surfaces": ["dm"]},
        status="enabled",
    )
    service = _tool_execution_service(tool_runtime=tool_runtime, artifacts=artifacts)
    monkeypatch.setenv(
        "EA_TELEGRAM_BOT_REGISTRY_JSON",
        json.dumps({"default": {"token": "telegram-token", "handle": "operator_concierge_bot"}}),
    )
    monkeypatch.setattr("app.services.telegram_delivery._telegram_video_has_audio", lambda value: True)
    monkeypatch.setattr("app.services.telegram_delivery._telegram_remote_ref_reachable", lambda value: True)
    tool_runtime.upsert_tool(
        tool_name="test.video.render",
        version="test-v1",
        input_schema_json={"type": "object"},
        output_schema_json={"type": "object"},
        policy_json={},
        enabled=True,
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 99}}).encode("utf-8")

    sent: list[dict[str, object]] = []

    def _fake_urlopen(request, timeout=30):
        sent.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return _FakeResponse()

    monkeypatch.setattr("app.services.telegram_delivery.urllib.request.urlopen", _fake_urlopen)
    def _fake_handler(request, definition):
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=str(request.action_kind or "movie.render") or "movie.render",
            target_ref="mootion:test",
            output_json={
                "result_title": "Brigittenau Shortlist Teaser",
                "render_status": "rendered",
                "asset_url": "https://cdn.example/mootion/brigittenau-shortlist.mp4",
                "public_url": "https://viewer.example/mootion/brigittenau-shortlist",
                "mime_type": "video/mp4",
                "structured_output_json": {
                    "render_status": "rendered",
                    "asset_url": "https://cdn.example/mootion/brigittenau-shortlist.mp4",
                    "public_url": "https://viewer.example/mootion/brigittenau-shortlist",
                },
            },
            receipt_json={"handler_key": definition.tool_name},
        )

    service.register_handler("test.video.render", _fake_handler)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-video-send-1",
            step_id="step-video-send-1",
            tool_name="test.video.render",
            action_kind="movie.render",
            payload_json={"script_text": "Create a teaser."},
            context_json={"principal_id": "exec-video-send"},
        )
    )

    assert sent and sent[0]["url"] == "https://api.telegram.org/bottelegram-token/sendVideo"
    assert sent[0]["payload"]["video"] == "https://cdn.example/mootion/brigittenau-shortlist.mp4"
    assert result.output_json["telegram_delivery_json"]["status"] == "sent"
    assert result.output_json["telegram_delivery_json"]["message_ids"] == ["99"]


def test_tool_execution_service_blocks_rendered_video_telegram_send_without_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = InMemoryArtifactRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    tool_runtime.upsert_connector_binding(
        principal_id="exec-video-silent",
        connector_name="telegram_identity",
        external_account_ref="42",
        auth_metadata_json={"default_chat_ref": "42", "bot_key": "default"},
        scope_json={"assistant_surfaces": ["dm"]},
        status="enabled",
    )
    service = _tool_execution_service(tool_runtime=tool_runtime, artifacts=artifacts)
    monkeypatch.setenv(
        "EA_TELEGRAM_BOT_REGISTRY_JSON",
        json.dumps({"default": {"token": "telegram-token"}}),
    )
    monkeypatch.setattr("app.services.telegram_delivery._telegram_video_has_audio", lambda value: False)
    tool_runtime.upsert_tool(
        tool_name="test.video.render",
        version="test-v1",
        input_schema_json={"type": "object"},
        output_schema_json={"type": "object"},
        policy_json={},
        enabled=True,
    )

    def _fake_handler(request, definition):
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=str(request.action_kind or "movie.render") or "movie.render",
            target_ref="mootion:test",
            output_json={
                "result_title": "Silent Teaser",
                "render_status": "rendered",
                "asset_url": "https://cdn.example/mootion/silent-shortlist.mp4",
                "mime_type": "video/mp4",
                "structured_output_json": {
                    "render_status": "rendered",
                    "asset_url": "https://cdn.example/mootion/silent-shortlist.mp4",
                },
            },
            receipt_json={"handler_key": definition.tool_name},
        )

    service.register_handler("test.video.render", _fake_handler)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-video-send-2",
            step_id="step-video-send-2",
            tool_name="test.video.render",
            action_kind="movie.render",
            payload_json={"script_text": "Create a teaser."},
            context_json={"principal_id": "exec-video-silent"},
        )
    )

    assert result.output_json["telegram_delivery_json"]["status"] == "failed"
    assert result.output_json["telegram_delivery_json"]["error"] == "telegram_video_audio_missing"


def test_tool_execution_service_auto_sends_audio_outputs_to_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = InMemoryArtifactRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    tool_runtime.upsert_connector_binding(
        principal_id="exec-audio-send",
        connector_name="telegram_identity",
        external_account_ref="42",
        auth_metadata_json={"default_chat_ref": "42", "bot_key": "default"},
        scope_json={"assistant_surfaces": ["dm"]},
        status="enabled",
    )
    service = _tool_execution_service(tool_runtime=tool_runtime, artifacts=artifacts)
    monkeypatch.setenv(
        "EA_TELEGRAM_BOT_REGISTRY_JSON",
        json.dumps({"default": {"token": "telegram-token"}}),
    )
    monkeypatch.setattr("app.services.telegram_delivery._telegram_remote_ref_reachable", lambda value: True)
    tool_runtime.upsert_tool(
        tool_name="test.audio.render",
        version="test-v1",
        input_schema_json={"type": "object"},
        output_schema_json={"type": "object"},
        policy_json={},
        enabled=True,
    )

    def _fake_handler(request, definition):
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=str(request.action_kind or "audio.render") or "audio.render",
            target_ref="pocket:test",
            output_json={
                "result_title": "Hospital conversation",
                "asset_url": "https://cdn.example/audio/hospital-conversation.mp3",
                "mime_type": "audio/mpeg",
            },
            receipt_json={"handler_key": definition.tool_name},
        )

    service.register_handler("test.audio.render", _fake_handler)
    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 21}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        sent.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data.decode("utf-8")),
            }
        )
        return _FakeResponse()

    monkeypatch.setattr("app.services.telegram_delivery.urllib.request.urlopen", _fake_urlopen)
    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-audio-send-1",
            step_id="step-audio-send-1",
            tool_name="test.audio.render",
            action_kind="audio.render",
            payload_json={"title": "Hospital conversation"},
            context_json={"principal_id": "exec-audio-send"},
        )
    )

    assert sent and sent[0]["url"] == "https://api.telegram.org/bottelegram-token/sendAudio"
    assert sent[0]["payload"]["audio"] == "https://cdn.example/audio/hospital-conversation.mp3"
    assert result.output_json["telegram_delivery_json"]["status"] == "sent"
    assert result.output_json["telegram_delivery_json"]["kind"] == "audio"
    assert result.output_json["telegram_delivery_json"]["message_ids"] == ["21"]


def test_tool_execution_service_rejects_non_executable_provider_tool_route() -> None:
    provider_registry = ProviderRegistryService()
    provider_registry._bindings = tuple(provider_registry.list_bindings()) + (
        ProviderBinding(
            provider_key="shadow_provider",
            display_name="Shadow Provider",
            executable=False,
            capabilities=(
                ProviderCapability(
                    provider_key="shadow_provider",
                    capability_key="shadow_action",
                    tool_name="shadow.provider.action",
                    executable=False,
                ),
            ),
        ),
    )
    service = _tool_execution_service(
        tool_runtime=ToolRuntimeService(
            tool_registry=InMemoryToolRegistryRepository(),
            connector_bindings=InMemoryConnectorBindingRepository(),
        ),
        artifacts=InMemoryArtifactRepository(),
        provider_registry=provider_registry,
    )
    with pytest.raises(ToolExecutionError, match="provider_tool_unavailable:shadow.provider.action"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-provider-route-1",
                step_id="step-provider-route-1",
                tool_name="shadow.provider.action",
                action_kind="shadow.action",
                payload_json={},
                context_json={"principal_id": "exec-1"},
            )
        )


def test_tool_execution_service_executes_structured_generate_via_brain_router(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GEMINI_VORTEX_COMMAND", "python3")
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    def _fake_execute(self, request, definition):
        prompt = str((request.payload_json or {}).get("prompt") or (request.payload_json or {}).get("normalized_text") or "")
        assert "Summarize fleet health" in prompt
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=str(request.action_kind or "content.generate") or "content.generate",
            target_ref="gemini-vortex:test",
            output_json={
                "normalized_text": '{"summary":"healthy"}',
                "structured_output_json": {"summary": "healthy"},
                "preview_text": '{"summary":"healthy"}',
                "mime_type": "application/json",
                "provider_backend": "gemini-cli",
            },
            receipt_json={
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "provider_key": "gemini_vortex",
            },
            model_name="gemini-2.5-flash",
            tokens_in=19,
            tokens_out=7,
        )

    monkeypatch.setattr(
        "app.services.tool_execution_gemini_vortex_adapter.GeminiVortexToolAdapter.execute",
        _fake_execute,
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-brain-router-1",
            step_id="step-brain-router-1",
            tool_name="provider.brain_router.structured_generate",
            action_kind="content.generate",
            payload_json={
                "brain_profile": "groundwork",
                "provider_hint_order": ["gemini_vortex"],
                "allowed_tools": ["provider.gemini_vortex.structured_generate", "artifact_repository"],
                "normalized_text": "Summarize fleet health.",
                "prompt": "Summarize fleet health.",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "provider.gemini_vortex.structured_generate"
    assert result.output_json["structured_output_json"]["summary"] == "healthy"
    assert result.output_json["brain_profile"] == "groundwork"
    assert result.output_json["routed_provider_key"] == "gemini_vortex"
    assert result.receipt_json["logical_tool_name"] == "provider.brain_router.structured_generate"


def test_tool_execution_service_executes_teable_table_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEABLE_API_KEY", "test-teable-key")
    monkeypatch.setenv(
        "TEABLE_TABLE_SYNC_CONFIG_JSON",
        json.dumps(
            {
                "preference_review_queue": {
                    "table_id": "tbl_preference_review_queue",
                    "key_field": "projection_id",
                    "field_key_type": "name",
                }
            }
        ),
    )

    observed: list[tuple[str, str, dict[str, object] | None]] = []

    def _request_json(self, *, method: str, url: str, api_key: str, body: dict[str, object] | None = None) -> dict[str, object]:
        assert api_key == "test-teable-key"
        observed.append((method, url, body))
        if method == "GET":
            return {"records": []}
        if method == "POST":
            return {"records": [{"id": "rec_pref_queue_1"}]}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(TeableToolAdapter, "_request_json", _request_json)

    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-teable-sync-1",
            step_id="step-teable-sync-1",
            tool_name="provider.teable.table_sync",
            action_kind="table.sync",
            payload_json={
                "projection_scope": "preference_profile",
                "person_id": "self",
                "tables_json": {
                    "preference_review_queue": [
                        {
                            "projection_id": "pref_node:self:willhaben:soft_preference:preferred_areas",
                            "display_name": "Tibor",
                            "domain": "willhaben",
                            "key": "preferred_areas",
                            "confidence": 0.8,
                            "editable_fields_allowlist": ["value_json", "strength"],
                        }
                    ]
                },
            },
            context_json={"principal_id": "pref-sync-principal"},
        )
    )

    assert result.tool_name == "provider.teable.table_sync"
    assert result.action_kind == "table.sync"
    assert result.target_ref == "teable-sync:preference_profile:self"
    assert result.output_json["synced_tables"] == ["preference_review_queue"]
    assert result.output_json["created_count"] == 1
    assert result.output_json["updated_count"] == 0
    assert result.receipt_json["provider_key"] == "teable"
    assert observed[0][0] == "GET"
    assert "/api/table/tbl_preference_review_queue/record?" in observed[0][1]
    assert observed[1][0] == "POST"
    assert observed[1][2]["records"][0]["fields"]["projection_id"] == "pref_node:self:willhaben:soft_preference:preferred_areas"
    assert isinstance(observed[1][2]["records"][0]["fields"]["editable_fields_allowlist"], str)


def test_teable_table_sync_compacts_oversized_json_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEABLE_API_KEY", "test-teable-key")
    monkeypatch.setenv(
        "TEABLE_TABLE_SYNC_CONFIG_JSON",
        json.dumps(
            {
                "propertyquarry_preferences": {
                    "table_id": "tbl_propertyquarry_preferences",
                    "key_field": "projection_id",
                    "field_key_type": "name",
                }
            }
        ),
    )
    observed: list[dict[str, object]] = []

    def _request_json(self, *, method: str, url: str, api_key: str, body: dict[str, object] | None = None) -> dict[str, object]:
        if method == "GET":
            return {"records": []}
        if method == "POST":
            observed.append(dict(body or {}))
            return {"records": [{"id": "rec_preferences_1"}]}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(TeableToolAdapter, "_request_json", _request_json)

    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-teable-sync-large",
            step_id="step-teable-sync-large",
            tool_name="provider.teable.table_sync",
            action_kind="table.sync",
            payload_json={
                "projection_scope": "propertyquarry",
                "person_id": "propertyquarry",
                "tables_json": {
                    "propertyquarry_preferences": [
                        {
                            "projection_id": "preferences:propertyquarry:cf-email:tibor.girschele@gmail.com:self",
                            "preferences_json": {"source_payload": "x" * 1_200_000},
                        }
                    ]
                },
            },
            context_json={"principal_id": "cf-email:tibor.girschele@gmail.com"},
        )
    )

    fields = observed[0]["records"][0]["fields"]
    compacted = json.loads(fields["preferences_json"])
    assert result.output_json["created_count"] == 1
    assert fields["projection_id"] == "preferences:propertyquarry:cf-email:tibor.girschele@gmail.com:self"
    assert compacted["truncated"] is True
    assert compacted["reason"] == "teable_record_fields_max_bytes"
    assert compacted["original_bytes"] > 1_000_000


def test_teable_tool_adapter_request_json_uses_browser_style_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEABLE_API_KEY", "test-teable-key")
    adapter = TeableToolAdapter()
    captured: dict[str, object] = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"{}"

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    adapter._request_json(method="GET", url="https://app.teable.ai/api/space", api_key="test-teable-key")

    headers = {str(key).lower(): value for key, value in dict(captured["headers"]).items()}
    assert headers["authorization"] == "Bearer test-teable-key"
    assert headers["origin"] == "https://app.teable.ai"
    assert headers["referer"] == "https://app.teable.ai/"
    assert "mozilla/5.0" in str(headers["user-agent"]).lower()


def test_gemini_vortex_adapter_honors_payload_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GEMINI_VORTEX_COMMAND", "python3")
    adapter = GeminiVortexToolAdapter()
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        seen["command"] = command
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='{"response":"{\\"text\\":\\"ok\\"}","stats":{}}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = adapter.execute(
        ToolInvocationRequest(
            session_id="session-gemini-timeout-1",
            step_id="step-gemini-timeout-1",
            tool_name="provider.gemini_vortex.structured_generate",
            action_kind="content.generate",
            payload_json={
                "source_text": "Say ok",
                "response_schema_json": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                },
                "timeout_seconds": 20,
                "model": "gemini-3-flash-preview",
            },
            context_json={"principal_id": "exec-1"},
        ),
        ToolDefinition(
            tool_name="provider.gemini_vortex.structured_generate",
            version="builtin",
            input_schema_json={},
            output_schema_json={},
            policy_json={},
            allowed_channels=("commentary",),
            approval_default="never",
            enabled=True,
            updated_at="2026-05-01T00:00:00Z",
        ),
    )

    assert seen["timeout"] == 20
    assert result.output_json["structured_output_json"]["text"] == "ok"


def test_provider_registry_exposes_binding_states(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSERLY_API_KEY", "browserly-test-key")
    monkeypatch.setenv("EA_GEMINI_VORTEX_COMMAND", "sh")

    registry = ProviderRegistryService()
    states = {row.provider_key: row for row in registry.list_binding_states()}

    assert states["artifact_repository"].state == "ready"
    assert states["artifact_repository"].auth_mode == "internal"
    assert states["browserly"].auth_mode == "api_key"
    assert states["browserly"].secret_configured is True
    assert states["browserly"].state == "configured"
    assert "browser_capture" in states["browserly"].capabilities
    assert states["gemini_vortex"].auth_mode == "cli"
    assert states["gemini_vortex"].state == "ready"


def test_provider_registry_cli_state_accepts_command_with_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GEMINI_VORTEX_COMMAND", "sh -c true")

    registry = ProviderRegistryService()
    state = registry.binding_state("gemini_vortex")

    assert state is not None
    assert state.auth_mode == "cli"
    assert state.secret_configured is True
    assert state.state == "ready"


def test_tool_execution_service_promotes_audit_review_profile_to_jury_action_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    provider_registry = ProviderRegistryService()
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        provider_registry=provider_registry,
    )
    tool_runtime.upsert_tool(
        tool_name="fake.review",
        version="v1",
        input_schema_json={"type": "object"},
        output_schema_json={"type": "object"},
        enabled=True,
    )

    def _fake_review(request: ToolInvocationRequest, _definition) -> ToolInvocationResult:
        assert request.action_kind == "audit.jury"
        return ToolInvocationResult(
            tool_name=request.tool_name,
            action_kind=request.action_kind,
            target_ref="fake-review-1",
            output_json={"status": "ok"},
            receipt_json={"handler_key": request.tool_name, "invocation_contract": "tool.v1"},
        )

    service.register_handler("fake.review", _fake_review)
    monkeypatch.setattr(
        provider_registry,
        "route_brain_profile_capability_with_context",
        lambda **kwargs: CapabilityRoute(
            provider_key="browseract",
            capability_key="reasoned_patch_review",
            tool_name="fake.review",
            executable=True,
        ),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-audit-1",
            step_id="step-audit-1",
            tool_name="provider.brain_router.reasoned_patch_review",
            action_kind="audit.review_light",
            payload_json={"brain_profile": "audit", "normalized_text": "Review this patch."},
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "fake.review"
    assert result.action_kind == "audit.jury"


def test_tool_execution_service_executes_registered_tool_not_in_provider_catalog() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    tool_runtime.upsert_tool(
        tool_name="email.send",
        version="v1",
        input_schema_json={"type": "object"},
        output_schema_json={"type": "object"},
        enabled=True,
    )

    def _email_send(
        request: ToolInvocationRequest, _definition
    ):
        recipient = str(request.payload_json.get("recipient", ""))
        return ToolInvocationResult(
            tool_name=request.tool_name,
            action_kind=request.action_kind or "delivery.send",
            target_ref="email-msg-1",
            output_json={"status": "queued", "recipient": recipient},
            receipt_json={"handler_key": request.tool_name, "invocation_contract": "tool.v1"},
        )

    service.register_handler("email.send", _email_send)
    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-custom-tool-1",
            step_id="step-custom-tool-1",
            tool_name="email.send",
            action_kind="delivery.send",
            payload_json={"recipient": "ops@example.com"},
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "email.send"
    assert result.output_json["status"] == "queued"
    assert result.receipt_json["handler_key"] == "email.send"


def test_tool_execution_service_re_registers_builtin_handlers_via_provider_registry_route() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="acct-browseract-1",
        scope_json={"scopes": ["browseract"], "services": ["BrowserAct"]},
        status="enabled",
    )

    service._handlers.clear()
    tool_runtime._tool_registry = InMemoryToolRegistryRepository()

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-provider-route-2",
                step_id="step-provider-route-2",
                tool_name="browseract.extract_account_inventory",
                action_kind="browseract.extract_account_inventory",
                payload_json={
                    "binding_id": binding.binding_id,
                    "service_names": ["BrowserAct"],
                    "requested_fields": ["plan_tier"],
                    "instructions": "refresh inventory",
                "account_hints_json": {},
                "run_url": "https://example.test/run/1",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_inventory"
    assert result.receipt_json["handler_key"] == "browseract.extract_account_inventory"


def test_tool_execution_service_materializes_evidence_objects_for_evidence_pack_artifacts() -> None:
    artifacts = InMemoryArtifactRepository()
    evidence_runtime = EvidenceRuntimeService(InMemoryEvidenceObjectRepository())
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=artifacts,
        evidence_runtime=evidence_runtime,
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-evidence-1",
            step_id="step-evidence-1",
            tool_name="artifact_repository",
            action_kind="artifact.save",
            payload_json={
                "source_text": "Market conditions suggest two viable options.",
                "expected_artifact": "decision_summary",
                "structured_output_json": {
                    "format": "evidence_pack",
                    "claims": ["Option A preserves margin", "Option B accelerates launch"],
                    "evidence_refs": ["browseract://run/123", "paper://abc"],
                    "open_questions": ["Need final vendor pricing"],
                    "confidence": 0.72,
                },
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.output_json["evidence_object_id"] == f"evidence-{result.target_ref}"
    assert result.output_json["citation_handle"] == f"evidence://evidence-{result.target_ref}"
    listed = evidence_runtime.list_objects(limit=10, principal_id="exec-1")
    assert len(listed) == 1
    assert listed[0].artifact_id == result.target_ref
    assert listed[0].claims == ("Option A preserves margin", "Option B accelerates launch")
    assert listed[0].evidence_refs == ("browseract://run/123", "paper://abc")


def test_evidence_runtime_merges_materialized_evidence_objects_without_reparsing_artifact_body() -> None:
    evidence_runtime = EvidenceRuntimeService(InMemoryEvidenceObjectRepository())
    first = evidence_runtime.record_artifact(
        Artifact(
            artifact_id="artifact-evidence-1",
            kind="decision_summary",
            content="Market conditions suggest two viable options.",
            execution_session_id="session-evidence-1",
            principal_id="exec-1",
            structured_output_json={
                "format": "evidence_pack",
                "claims": ["Option A preserves margin", "Option B accelerates launch"],
                "evidence_refs": ["browseract://run/123", "paper://abc"],
                "open_questions": ["Need final vendor pricing"],
                "confidence": 0.72,
            },
        )
    )
    second = evidence_runtime.record_artifact(
        Artifact(
            artifact_id="artifact-evidence-2",
            kind="decision_summary",
            content="Support load may fall if the simpler option ships first.",
            execution_session_id="session-evidence-2",
            principal_id="exec-1",
            structured_output_json={
                "format": "evidence_pack",
                "claims": ["Option C reduces support load"],
                "evidence_refs": ["paper://abc", "call://ops-review"],
                "open_questions": ["Need service staffing forecast"],
                "confidence": 0.58,
            },
        )
    )

    assert first is not None
    assert second is not None
    merged = evidence_runtime.merge_objects([first.evidence_id, second.evidence_id], principal_id="exec-1")

    assert merged.claims == (
        "Option A preserves margin",
        "Option B accelerates launch",
        "Option C reduces support load",
    )
    assert merged.evidence_refs == ("browseract://run/123", "paper://abc", "call://ops-review")
    assert merged.open_questions == ("Need final vendor pricing", "Need service staffing forecast")
    assert merged.source_artifact_ids == ("artifact-evidence-1", "artifact-evidence-2")
    assert merged.citation_handles == (
        "evidence://evidence-artifact-evidence-1",
        "evidence://evidence-artifact-evidence-2",
    )


def test_tool_execution_service_rejects_disabled_tools() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    tool_runtime.upsert_tool(
        tool_name="artifact_repository",
        version="v2",
        enabled=False,
    )

    with pytest.raises(ToolExecutionError, match="tool_disabled:artifact_repository"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-1",
                step_id="step-1",
                tool_name="artifact_repository",
                action_kind="artifact.save",
                payload_json={"source_text": "draft note"},
                context_json={"principal_id": "exec-1"},
            )
        )


def test_tool_execution_service_requires_principal_for_artifact_repository_handler() -> None:
    service = _tool_execution_service(
        tool_runtime=ToolRuntimeService(
            tool_registry=InMemoryToolRegistryRepository(),
            connector_bindings=InMemoryConnectorBindingRepository(),
        ),
        artifacts=InMemoryArtifactRepository(),
    )

    with pytest.raises(ToolExecutionError, match="principal_id_required"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-1",
                step_id="step-1",
                tool_name="artifact_repository",
                action_kind="artifact.save",
                payload_json={"source_text": "draft note"},
                context_json={},
            )
        )


def test_tool_execution_service_executes_builtin_connector_dispatch_handler() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-1",
        scope_json={"scopes": ["mail.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-2",
            step_id="step-2",
            tool_name="connector.dispatch",
            action_kind="delivery.send",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "channel": "email",
                "recipient": "ops@example.com",
                "content": "queued dispatch",
                "metadata": {"source": "tool"},
                "idempotency_key": "tool-dispatch-test",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "connector.dispatch"
    assert result.action_kind == "delivery.send"
    assert result.output_json["status"] == "queued"
    assert result.output_json["binding_id"] == binding.binding_id
    assert result.receipt_json["handler_key"] == "connector.dispatch"
    assert result.receipt_json["invocation_contract"] == "tool.v1"
    pending = channel_runtime.list_pending_delivery(limit=10)
    assert any(row.delivery_id == result.target_ref for row in pending)


def test_connector_dispatch_builtin_schema_matches_executor_contract() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=ChannelRuntimeService(
            observations=InMemoryObservationEventRepository(),
            outbox=InMemoryDeliveryOutboxRepository(),
        ),
    )

    tool = tool_runtime.get_tool("connector.dispatch")

    assert service is not None
    assert tool is not None
    assert tuple(tool.input_schema_json.get("required") or ()) == CONNECTOR_DISPATCH_REQUIRED_INPUT_FIELDS
    assert set(CONNECTOR_DISPATCH_REQUIRED_INPUT_FIELDS).issubset(tool.input_schema_json["properties"])
    assert set(CONNECTOR_DISPATCH_OPTIONAL_INPUT_FIELDS).issubset(tool.input_schema_json["properties"])
    assert tool.policy_json["idempotency_key_policy"] == CONNECTOR_DISPATCH_IDEMPOTENCY_POLICY


@pytest.mark.parametrize(
    ("missing_field", "expected_error"),
    [
        ("binding_id", "connector_binding_required:connector.dispatch"),
    ],
)
def test_connector_dispatch_executor_required_fields_match_declared_schema(
    missing_field: str,
    expected_error: str,
) -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-required-contract",
        scope_json={"scopes": ["mail.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )
    payload = {
        "binding_id": binding.binding_id,
        "principal_id": "exec-1",
        "channel": "email",
        "recipient": "ops@example.com",
        "content": "queued dispatch",
    }
    payload.pop(missing_field)

    tool = tool_runtime.get_tool("connector.dispatch")

    assert tool is not None
    assert missing_field in tuple(tool.input_schema_json.get("required") or ())
    with pytest.raises(ToolExecutionError, match=expected_error):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-contract-1",
                step_id="step-contract-1",
                tool_name="connector.dispatch",
                action_kind="delivery.send",
                payload_json=payload,
                context_json={"principal_id": "exec-1"},
            )
        )


def test_connector_dispatch_executor_allows_missing_optional_idempotency_key() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-optional-idem",
        scope_json={"scopes": ["mail.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    tool = tool_runtime.get_tool("connector.dispatch")
    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-optional-idem-1",
            step_id="step-optional-idem-1",
            tool_name="connector.dispatch",
            action_kind="delivery.send",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "channel": "email",
                "recipient": "ops@example.com",
                "content": "queued dispatch",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert tool is not None
    assert "idempotency_key" not in tuple(tool.input_schema_json.get("required") or ())
    assert tool.policy_json["idempotency_key_policy"] == CONNECTOR_DISPATCH_IDEMPOTENCY_POLICY
    assert result.output_json["idempotency_key"] == ""
    pending = channel_runtime.list_pending_delivery(limit=10)
    assert any(row.delivery_id == result.target_ref and row.idempotency_key == "" for row in pending)


def test_connector_dispatch_executor_accepts_request_principal_when_payload_principal_is_missing() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-optional-principal",
        scope_json={"scopes": ["mail.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-optional-principal-1",
            step_id="step-optional-principal-1",
            tool_name="connector.dispatch",
            action_kind="delivery.send",
            payload_json={
                "binding_id": binding.binding_id,
                "channel": "email",
                "recipient": "ops@example.com",
                "content": "queued dispatch",
            },
            context_json={"principal_id": "exec-1"},
        )
    )
    assert result.receipt_json["principal_id"] == "exec-1"


def test_connector_dispatch_executor_falls_back_to_builtin_allowed_channels_if_tool_definition_is_missing_it() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-fallback-channels",
        scope_json={"scopes": ["mail.send", "sms.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )
    tool_runtime.upsert_tool(
        tool_name="connector.dispatch",
        version="v1",
        input_schema_json={
            "type": "object",
            "required": ["binding_id", "channel", "recipient", "content"],
            "properties": {
                "binding_id": {"type": "string"},
                "channel": {"type": "string"},
                "recipient": {"type": "string"},
                "content": {"type": "string"},
            },
        },
        output_schema_json={
            "type": "object",
            "required": ["delivery_id", "status", "tool_name", "action_kind"],
        },
        policy_json={
            "builtin": True,
            "action_kind": "delivery.send",
            "idempotency_key_policy": CONNECTOR_DISPATCH_IDEMPOTENCY_POLICY,
        },
        allowed_channels=(),
        approval_default="manager",
        enabled=True,
    )

    with pytest.raises(
        ToolExecutionError,
        match="connector_dispatch_channel_not_allowed:sms:email,slack,telegram",
    ):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-channel-fallback-1",
                step_id="step-channel-fallback-1",
                tool_name="connector.dispatch",
                action_kind="delivery.send",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                    "channel": "sms",
                    "recipient": "ops@example.com",
                    "content": "blocked by fallback channels",
                },
                context_json={"principal_id": "exec-1"},
            )
        )


def test_connector_dispatch_executor_rejects_missing_principal_id() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-missing-principal",
        scope_json={"scopes": ["mail.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    with pytest.raises(ToolExecutionError, match="principal_id_required"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-missing-principal-1",
                step_id="step-missing-principal-1",
                tool_name="connector.dispatch",
                action_kind="delivery.send",
                payload_json={
                    "binding_id": binding.binding_id,
                    "channel": "email",
                    "recipient": "ops@example.com",
                    "content": "blocked dispatch",
                },
                context_json={},
            )
        )


def test_connector_dispatch_executor_rejects_context_principal_id_missing_even_if_payload_principal_present() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-missing-context-principal",
        scope_json={"scopes": ["mail.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    with pytest.raises(ToolExecutionError, match="principal_id_required"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-missing-context-principal-1",
                step_id="step-missing-context-principal-1",
                tool_name="connector.dispatch",
                action_kind="delivery.send",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                    "channel": "email",
                    "recipient": "ops@example.com",
                    "content": "blocked dispatch",
                },
                context_json={},
            )
        )


def test_connector_dispatch_executor_rejects_disallowed_channel() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-disallowed-channel",
        scope_json={"scopes": ["mail.send", "sms.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    with pytest.raises(
        ToolExecutionError,
        match="connector_dispatch_channel_not_allowed:sms:email,slack,telegram",
    ):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-disallowed-channel-1",
                step_id="step-disallowed-channel-1",
                tool_name="connector.dispatch",
                action_kind="delivery.send",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                    "channel": "sms",
                    "recipient": "ops@example.com",
                    "content": "blocked dispatch",
                },
                context_json={"principal_id": "exec-1"},
            )
        )


def test_connector_dispatch_executor_prefers_allowed_channel_validation_before_scope_validation() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-disallowed-channel-no-scope",
        scope_json={"scopes": ["mail.readonly"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    with pytest.raises(
        ToolExecutionError,
        match="connector_dispatch_channel_not_allowed:push:email,slack,telegram",
    ):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-disallowed-channel-before-scope-1",
                step_id="step-disallowed-channel-before-scope-1",
                tool_name="connector.dispatch",
                action_kind="delivery.send",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                    "channel": "push",
                    "recipient": "ops@example.com",
                    "content": "blocked dispatch",
                },
                context_json={"principal_id": "exec-1"},
            )
        )


def test_connector_dispatch_executor_rejects_principal_scope_mismatch() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-mismatch",
        scope_json={"scopes": ["mail.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    with pytest.raises(ToolExecutionError, match="principal_scope_mismatch"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-dispatched-mismatch-1",
                step_id="step-dispatched-mismatch-1",
                tool_name="connector.dispatch",
                action_kind="delivery.send",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                    "channel": "email",
                    "recipient": "ops@example.com",
                    "content": "blocked dispatch",
                },
                context_json={"principal_id": "exec-2"},
            )
        )


def test_connector_dispatch_executor_normalizes_channel_for_allowed_channels() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-case",
        scope_json={"scopes": ["mail.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-dispatched-case-1",
            step_id="step-dispatched-case-1",
            tool_name="connector.dispatch",
            action_kind="delivery.send",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "channel": "EMAIL",
                "recipient": "ops@example.com",
                "content": "queued dispatch",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.output_json["channel"] == "email"
    pending = channel_runtime.list_pending_delivery(limit=10)
    assert any(row.delivery_id == result.target_ref and row.channel == "email" for row in pending)


def test_connector_dispatch_executor_enforces_sorted_allowed_channels_deterministically() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-disallowed-channel-order",
        scope_json={"scopes": ["mail.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )
    tool_runtime.upsert_tool(
        tool_name="connector.dispatch",
        version="v1",
        input_schema_json={
            "type": "object",
            "required": ["binding_id", "channel", "recipient", "content"],
            "properties": {
                "binding_id": {"type": "string"},
                "channel": {"type": "string"},
                "recipient": {"type": "string"},
                "content": {"type": "string"},
                "metadata": {"type": "object"},
            },
        },
        output_schema_json={
            "type": "object",
            "required": ["delivery_id", "status", "tool_name", "action_kind"],
        },
        policy_json={
            "builtin": True,
            "action_kind": "delivery.send",
            "idempotency_key_policy": CONNECTOR_DISPATCH_IDEMPOTENCY_POLICY,
        },
        allowed_channels=("telegram", "email", "slack"),
        approval_default="manager",
        enabled=True,
    )

    with pytest.raises(
        ToolExecutionError,
        match="connector_dispatch_channel_not_allowed:push:email,slack,telegram",
    ):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-disallowed-channel-order-1",
                step_id="step-disallowed-channel-order-1",
                tool_name="connector.dispatch",
                action_kind="delivery.send",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                    "channel": "push",
                    "recipient": "ops@example.com",
                    "content": "blocked dispatch",
                },
                context_json={"principal_id": "exec-1"},
            )
        )


def test_connector_dispatch_executor_rejects_request_principal_mismatch_even_when_payload_principal_present() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-dispatch-request-principal-mismatch",
        scope_json={"scopes": ["mail.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    with pytest.raises(ToolExecutionError, match="principal_scope_mismatch"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-dispatch-request-principal-mismatch-1",
                step_id="step-dispatch-request-principal-mismatch-1",
                tool_name="connector.dispatch",
                action_kind="delivery.send",
                payload_json={
                    "principal_id": "exec-1",
                    "binding_id": binding.binding_id,
                    "channel": "email",
                    "recipient": "ops@example.com",
                    "content": "blocked dispatch",
                },
                context_json={"principal_id": "exec-2"},
            )
        )


def test_browseract_tool_dispatch_requires_request_principal_id_even_if_payload_supplies_mismatch() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={"service_accounts_json": {"BrowserAct": {"tier": "Tier 3"}}},
        status="enabled",
    )

    with pytest.raises(ToolExecutionError, match="principal_id_required"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-browseract-principal-missing-1",
                step_id="step-browseract-principal-missing-1",
                tool_name="browseract.extract_account_facts",
                action_kind="account.extract",
                payload_json={
                    "binding_id": binding.binding_id,
                    "service_name": "BrowserAct",
                    "principal_id": "exec-1",
                },
                context_json={},
            )
        )


def test_browseract_tool_dispatch_accepts_request_principal_when_payload_principal_is_missing() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={"service_accounts_json": {"BrowserAct": {"tier": "Tier 3"}}},
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-principal-optional-1",
            step_id="step-browseract-principal-optional-1",
            tool_name="browseract.extract_account_facts",
            action_kind="account.extract",
            payload_json={
                "binding_id": binding.binding_id,
                "service_name": "BrowserAct",
            },
            context_json={"principal_id": "exec-1"},
        )
    )
    assert result.receipt_json["principal_id"] == "exec-1"


def test_browseract_tool_dispatch_rejects_request_principal_scope_mismatch() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={"service_accounts_json": {"BrowserAct": {"tier": "Tier 3"}}},
        status="enabled",
    )

    with pytest.raises(ToolExecutionError, match="^principal_scope_mismatch$"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-browseract-principal-mismatch-1",
                step_id="step-browseract-principal-mismatch-1",
                tool_name="browseract.extract_account_facts",
                action_kind="account.extract",
                payload_json={
                    "binding_id": binding.binding_id,
                    "service_name": "BrowserAct",
                    "principal_id": "exec-1",
                },
                context_json={"principal_id": "exec-2"},
            )
        )


def test_browseract_tool_dispatch_rejects_service_scope_mismatch() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={"service_accounts_json": {"BrowserAct": {"tier": "Tier 3"}}},
        status="enabled",
    )

    with pytest.raises(ToolExecutionError) as exc:
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-browseract-scope-mismatch-1",
                step_id="step-browseract-scope-mismatch-1",
                tool_name="browseract.extract_account_facts",
                action_kind="account.extract",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                    "service_name": "Teable",
                },
                context_json={"principal_id": "exec-1"},
            )
    )
    assert str(exc.value) == f"connector_binding_scope_mismatch:{binding.binding_id}:teable"


def test_tool_execution_service_rejects_browseract_inventory_scope_mismatch_for_explicit_services_without_metadata() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"scopes": ["BrowserAct"]},
        status="enabled",
    )

    with pytest.raises(ToolExecutionError) as exc:
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-browseract-inventory-scope-only-services-1",
                step_id="step-browseract-inventory-scope-only-services-1",
                tool_name="browseract.extract_account_inventory",
                action_kind="account.extract_inventory",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                    "service_names": ["BrowserAct", "Teable"],
                    "requested_fields": ["tier", "account_email", "status"],
                    "instructions": "use scope-only binding without services metadata",
                    "run_url": "https://browseract.example/run",
                },
                context_json={"principal_id": "exec-1"},
            )
        )

    assert str(exc.value) == f"connector_binding_scope_mismatch:{binding.binding_id}:browseract,teable"


def test_tool_execution_service_executes_browseract_inventory_with_scope_authorization_for_explicit_services_without_metadata() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"scopes": ["BrowserAct"]},
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-inventory-scope-only-services-1",
            step_id="step-browseract-inventory-scope-only-services-1",
            tool_name="browseract.extract_account_inventory",
            action_kind="account.extract_inventory",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_names": ["BrowserAct"],
                "requested_fields": ["tier", "account_email", "status"],
                "instructions": "use scope-only binding without services metadata",
                "run_url": "https://browseract.example/run",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_inventory"
    assert result.action_kind == "account.extract_inventory"
    assert result.output_json["service_names"] == ["BrowserAct"]
    assert result.output_json["missing_services"] == ["BrowserAct"]
    assert result.output_json["instructions"] == "use scope-only binding without services metadata"
    assert result.receipt_json["handler_key"] == "browseract.extract_account_inventory"
    assert result.receipt_json["invocation_contract"] == "tool.v1"


def test_tool_execution_service_executes_browseract_extract_with_scope_authorization_without_services_metadata() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"scopes": ["BrowserAct"]},
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-extract-scope-only-services-1",
            step_id="step-browseract-extract-scope-only-services-1",
            tool_name="browseract.extract_account_facts",
            action_kind="account.extract",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_name": "BrowserAct",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_facts"
    assert result.output_json["service_name"] == "BrowserAct"


def test_tool_execution_service_executes_browseract_extract_with_scope_authorization_from_string_scope_json_without_services_metadata() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"scopes": "BrowserAct"},
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-extract-scope-only-string-1",
            step_id="step-browseract-extract-scope-only-string-1",
            tool_name="browseract.extract_account_facts",
            action_kind="account.extract",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_name": "BrowserAct",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_facts"
    assert result.output_json["service_name"] == "BrowserAct"


def test_tool_execution_service_executes_builtin_browseract_extract_handler() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"services": ["BrowserAct", "Teable"]},
        auth_metadata_json={
            "service_accounts_json": {
                "BrowserAct": {
                    "tier": "Tier 3",
                    "account_email": "ops@example.com",
                    "status": "activated",
                }
            }
        },
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-1",
            step_id="step-browseract-1",
            tool_name="browseract.extract_account_facts",
            action_kind="account.extract",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_name": "BrowserAct",
                "requested_fields": ["tier", "account_email", "status"],
                "instructions": "Use stored BrowserAct credentials",
                "account_hints_json": {"BrowserAct": {"workspace": "primary"}},
                "run_url": "https://browseract.example/run",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_facts"
    assert result.action_kind == "account.extract"
    assert result.output_json["service_name"] == "BrowserAct"
    assert result.output_json["facts_json"]["tier"] == "Tier 3"
    assert result.output_json["account_email"] == "ops@example.com"
    assert result.output_json["missing_fields"] == []
    assert result.output_json["structured_output_json"]["verification_source"] == "connector_metadata"
    assert result.output_json["instructions"] == "Use stored BrowserAct credentials"
    assert result.output_json["account_hints_json"] == {"BrowserAct": {"workspace": "primary"}}
    assert result.output_json["requested_run_url"] == "https://browseract.example/run"
    assert result.output_json["structured_output_json"]["requested_run_url"] == "https://browseract.example/run"
    assert result.receipt_json["handler_key"] == "browseract.extract_account_facts"
    assert result.receipt_json["invocation_contract"] == "tool.v1"
    assert result.receipt_json["requested_run_url"] == "https://browseract.example/run"


def test_tool_execution_service_executes_browseract_extract_with_native_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-maps",
        scope_json={"scopes": ["google_maps_distance_research"]},
        auth_metadata_json={
            "browseract_service_workflows_json": {
                "google_maps_distance_research": {
                    "browseract_workflow_id": "workflow-google-maps",
                }
            },
            "browseract_workflow_input_names_json": [
                "KeyWords",
                "language",
                "country",
            ],
        },
        status="enabled",
    )
    adapter = service._browseract_module._adapter
    captured: dict[str, object] = {}

    def _run_workflow(*, workflow_id: str, input_values: dict[str, object]) -> dict[str, object]:
        captured.update({"workflow_id": workflow_id, "input_values": input_values})
        return {"id": "task-maps-1"}

    monkeypatch.setenv("BROWSERACT_API_KEY", "test-browseract-key")
    monkeypatch.setattr(adapter, "_run_browseract_workflow_task_with_inputs", _run_workflow)
    monkeypatch.setattr(
        adapter,
        "_wait_for_browseract_task",
        lambda **_: {
            "status": "finished",
            "output": {
                "string": json.dumps(
                    [
                        {
                            "fact_key": "supermarket",
                            "place_name": "BILLA",
                            "place_category": "Supermarket",
                            "place_id": "ChIJexample",
                            "destination_latitude": 48.2251,
                            "destination_longitude": 16.4012,
                            "final_surface_url": "https://www.google.com/maps/dir/?api=1&destination=48.2251,16.4012",
                            "visible_text": "BILLA — Supermarket",
                        }
                    ]
                )
            },
        },
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-maps-workflow-1",
            step_id="step-browseract-maps-workflow-1",
            tool_name="browseract.extract_account_facts",
            action_kind="account.extract",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_name": "google_maps_distance_research",
                "requested_fields": [
                    "fact_key",
                    "place_name",
                    "place_category",
                    "place_id",
                    "destination_latitude",
                    "destination_longitude",
                    "final_surface_url",
                    "visible_text",
                ],
                "instructions": "Return only coordinate-bound Google Maps evidence.",
                "account_hints_json": {
                    "query_url": "https://www.google.com/maps/dir/?api=1&origin=48.224,16.400&destination=supermarket",
                    "fact_key": "supermarket",
                    "listing_latitude": 48.224,
                    "listing_longitude": 16.4,
                    "travel_mode": "walking",
                },
                "workflow_inputs_json": {
                    "KeyWords": "supermarket near 48.224,16.400",
                    "language": "de",
                    "country": "at",
                    "unapproved_input": "must not cross the binding boundary",
                    "service_name": "must not override the reserved input",
                },
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert captured["workflow_id"] == "workflow-google-maps"
    workflow_inputs = captured["input_values"]
    assert isinstance(workflow_inputs, dict)
    assert workflow_inputs["service_name"] == "google_maps_distance_research"
    assert json.loads(str(workflow_inputs["requested_fields_json"])) == result.output_json["requested_fields"]
    assert json.loads(str(workflow_inputs["account_hints_json"]))["fact_key"] == "supermarket"
    assert workflow_inputs["query_url"].startswith("https://www.google.com/maps/dir/")
    assert workflow_inputs["KeyWords"] == "supermarket near 48.224,16.400"
    assert workflow_inputs["language"] == "de"
    assert workflow_inputs["country"] == "at"
    assert "unapproved_input" not in workflow_inputs
    assert workflow_inputs["service_name"] == "google_maps_distance_research"
    assert result.output_json["facts_json"]["place_id"] == "ChIJexample"
    assert result.output_json["verification_source"] == "browseract_workflow"
    assert result.output_json["requested_workflow_id"] == "workflow-google-maps"
    assert result.output_json["browseract_task_id"] == "task-maps-1"
    assert result.output_json["missing_fields"] == []
    assert result.receipt_json["requested_workflow_id"] == "workflow-google-maps"
    assert result.receipt_json["browseract_task_id"] == "task-maps-1"


def test_tool_execution_service_browseract_extract_workflow_fails_closed_without_facts_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-maps",
        scope_json={"scopes": ["google_maps_distance_research"]},
        auth_metadata_json={"browseract_workflow_id": "workflow-google-maps"},
        status="enabled",
    )
    adapter = service._browseract_module._adapter
    monkeypatch.setenv("BROWSERACT_API_KEY", "test-browseract-key")
    monkeypatch.setattr(
        adapter,
        "_run_browseract_workflow_task_with_inputs",
        lambda **_: {"id": "task-maps-2"},
    )
    monkeypatch.setattr(
        adapter,
        "_wait_for_browseract_task",
        lambda **_: {
            "status": "finished",
            "output": {"string": json.dumps({"summary": "A nearby supermarket exists."})},
        },
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-maps-workflow-missing-1",
            step_id="step-browseract-maps-workflow-missing-1",
            tool_name="browseract.extract_account_facts",
            action_kind="account.extract",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_name": "google_maps_distance_research",
                "requested_fields": ["place_id", "destination_latitude", "destination_longitude"],
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.output_json["discovery_status"] == "missing"
    assert result.output_json["verification_source"] == "missing"
    assert result.output_json["missing_fields"] == ["place_id", "destination_latitude", "destination_longitude"]
    assert result.output_json["requested_workflow_id"] == "workflow-google-maps"
    assert result.output_json["browseract_task_id"] == ""
    assert result.output_json["live_discovery_error"] == "browseract_workflow_facts_missing:task-maps-2"
    assert result.output_json["facts_json"] == {"service_name": "google_maps_distance_research"}


def test_tool_execution_service_executes_builtin_browseract_inventory_handler() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"services": ["BrowserAct", "Teable", "UnknownService"]},
        auth_metadata_json={
            "service_accounts_json": {
                "BrowserAct": {
                    "tier": "Tier 3",
                    "account_email": "ops@example.com",
                    "status": "activated",
                },
                "Teable": {
                    "tier": "License Tier 4",
                    "account_email": "ops@teable.example",
                    "status": "activated",
                },
            }
        },
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-inventory-1",
            step_id="step-browseract-inventory-1",
            tool_name="browseract.extract_account_inventory",
            action_kind="account.extract_inventory",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_names": ["BrowserAct", "Teable", "UnknownService"],
                "requested_fields": ["tier", "account_email", "status"],
                "instructions": "Use stored BrowserAct credentials",
                "account_hints_json": {"Teable": {"workspace": "ops"}},
                "run_url": "https://browseract.example/run",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_inventory"
    assert result.action_kind == "account.extract_inventory"
    assert result.output_json["service_names"] == ["BrowserAct", "Teable", "UnknownService"]
    assert result.output_json["missing_services"] == ["UnknownService"]
    assert result.output_json["instructions"] == "Use stored BrowserAct credentials"
    assert result.output_json["account_hints_json"] == {"Teable": {"workspace": "ops"}}
    assert result.output_json["requested_run_url"] == "https://browseract.example/run"
    assert len(result.output_json["services_json"]) == 3
    assert result.output_json["services_json"][0]["plan_tier"] == "Tier 3"
    assert result.output_json["services_json"][1]["account_email"] == "ops@teable.example"
    assert result.output_json["services_json"][1]["structured_output_json"]["account_hints_json"] == {
        "Teable": {"workspace": "ops"}
    }
    assert result.output_json["services_json"][2]["discovery_status"] == "missing"
    assert "Service: BrowserAct" in result.output_json["normalized_text"]
    assert "Service: UnknownService" in result.output_json["normalized_text"]
    assert result.receipt_json["handler_key"] == "browseract.extract_account_inventory"
    assert result.receipt_json["invocation_contract"] == "tool.v1"
    assert result.receipt_json["requested_run_url"] == "https://browseract.example/run"


def test_tool_execution_service_executes_browseract_inventory_with_scope_authorization_without_services_metadata() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"scopes": ["BrowserAct"]},
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-inventory-scope-only-1",
            step_id="step-browseract-inventory-scope-only-1",
            tool_name="browseract.extract_account_inventory",
            action_kind="account.extract_inventory",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "requested_fields": ["tier", "account_email", "status"],
                "instructions": "use scope-only binding",
                "run_url": "https://browseract.example/run",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_inventory"
    assert result.output_json["service_names"] == ["BrowserAct"]
    assert result.output_json["missing_services"] == ["BrowserAct"]
    assert result.output_json["services_json"][0]["discovery_status"] == "missing"
    assert result.output_json["instructions"] == "use scope-only binding"
    assert result.receipt_json["handler_key"] == "browseract.extract_account_inventory"
    assert result.receipt_json["invocation_contract"] == "tool.v1"


def test_tool_execution_service_executes_browseract_inventory_with_services_authorization_without_service_accounts_metadata() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"services": ["BrowserAct"]},
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-inventory-services-only-1",
            step_id="step-browseract-inventory-services-only-1",
            tool_name="browseract.extract_account_inventory",
            action_kind="account.extract_inventory",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "requested_fields": ["tier", "account_email", "status"],
                "instructions": "use services-only binding",
                "run_url": "https://browseract.example/run",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_inventory"
    assert result.output_json["service_names"] == ["BrowserAct"]
    assert result.output_json["missing_services"] == ["BrowserAct"]
    assert result.output_json["services_json"][0]["discovery_status"] == "missing"
    assert result.output_json["instructions"] == "use services-only binding"
    assert result.receipt_json["handler_key"] == "browseract.extract_account_inventory"
    assert result.receipt_json["invocation_contract"] == "tool.v1"


def test_tool_execution_service_executes_browseract_inventory_with_service_list_authorization_for_services_scope_without_metadata() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"services": ["BrowserAct"]},
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-inventory-services-only-services-2",
            step_id="step-browseract-inventory-services-only-services-2",
            tool_name="browseract.extract_account_inventory",
            action_kind="account.extract_inventory",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_names": ["BrowserAct"],
                "requested_fields": ["tier", "account_email", "status"],
                "instructions": "use services-only scope with explicit service_names",
                "run_url": "https://browseract.example/run",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_inventory"
    assert result.output_json["service_names"] == ["BrowserAct"]
    assert result.output_json["missing_services"] == ["BrowserAct"]
    assert result.output_json["instructions"] == "use services-only scope with explicit service_names"
    assert result.receipt_json["handler_key"] == "browseract.extract_account_inventory"
    assert result.receipt_json["invocation_contract"] == "tool.v1"


def test_tool_execution_service_executes_browseract_inventory_fallback_dedupes_overlapping_metadata_and_scope_services() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"services": ["BrowserAct"], "scopes": ["BrowserAct"]},
        auth_metadata_json={
            "service_accounts_json": [
                {"service_name": "BrowserAct", "account_email": "ops@browseract.example"},
            ]
        },
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-inventory-fallback-dedupe-1",
            step_id="step-browseract-inventory-fallback-dedupe-1",
            tool_name="browseract.extract_account_inventory",
            action_kind="account.extract_inventory",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "requested_fields": ["tier", "account_email"],
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_inventory"
    assert result.output_json["service_names"] == ["BrowserAct"]
    assert len(result.output_json["services_json"]) == 1
    assert result.output_json["services_json"][0]["service_name"] == "BrowserAct"


def test_tool_execution_service_executes_browseract_inventory_fallback_dedupes_mixed_case_services_preserving_first_seen_casing() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"services": ["browseract"], "scopes": ["BROWSERACT"]},
        auth_metadata_json={
            "service_accounts_json": [
                {"service_name": "BrowserAct", "account_email": "ops@browseract.example"},
            ]
        },
        status="enabled",
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-inventory-fallback-dedupe-mixed-case-1",
            step_id="step-browseract-inventory-fallback-dedupe-mixed-case-1",
            tool_name="browseract.extract_account_inventory",
            action_kind="account.extract_inventory",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "requested_fields": ["tier", "account_email"],
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_inventory"
    assert result.output_json["service_names"] == ["BrowserAct"]
    assert len(result.output_json["services_json"]) == 1
    assert result.output_json["services_json"][0]["service_name"] == "BrowserAct"


def test_tool_execution_service_executes_browseract_facts_with_scope_authorization_without_service_accounts_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"scopes": ["BrowserAct"]},
        status="enabled",
    )

    monkeypatch.setattr(
        service,
        "_browseract_live_extract",
        lambda **_: {
            "tier": "Tier 3",
            "account_email": "ops@example.com",
            "status": "activated",
        },
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-facts-scope-only-1",
            step_id="step-browseract-facts-scope-only-1",
            tool_name="browseract.extract_account_facts",
            action_kind="account.extract",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_name": "BrowserAct",
                "requested_fields": ["tier", "account_email", "status"],
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_facts"
    assert result.action_kind == "account.extract"
    assert result.output_json["service_name"] == "BrowserAct"
    assert result.output_json["facts_json"]["tier"] == "Tier 3"
    assert result.output_json["facts_json"]["account_email"] == "ops@example.com"
    assert result.output_json["facts_json"]["status"] == "activated"
    assert result.receipt_json["handler_key"] == "browseract.extract_account_facts"
    assert result.receipt_json["invocation_contract"] == "tool.v1"


def test_tool_execution_service_executes_browseract_facts_with_services_authorization_without_service_accounts_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"services": ["BrowserAct"]},
        status="enabled",
    )

    monkeypatch.setattr(
        service,
        "_browseract_live_extract",
        lambda **_: {
            "tier": "Tier 3",
            "account_email": "ops@example.com",
            "status": "activated",
        },
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-facts-services-only-1",
            step_id="step-browseract-facts-services-only-1",
            tool_name="browseract.extract_account_facts",
            action_kind="account.extract",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_name": "BrowserAct",
                "requested_fields": ["tier", "account_email", "status"],
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_facts"
    assert result.action_kind == "account.extract"
    assert result.output_json["service_name"] == "BrowserAct"
    assert result.output_json["facts_json"]["tier"] == "Tier 3"
    assert result.output_json["facts_json"]["account_email"] == "ops@example.com"
    assert result.output_json["facts_json"]["status"] == "activated"
    assert result.receipt_json["handler_key"] == "browseract.extract_account_facts"
    assert result.receipt_json["invocation_contract"] == "tool.v1"


def test_tool_execution_service_tolerates_live_browseract_inventory_fallback_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={"services": ["BrowserAct", "UnknownService"]},
        auth_metadata_json={
            "service_accounts_json": {
                "BrowserAct": {
                    "tier": "Tier 3",
                    "account_email": "ops@example.com",
                    "status": "activated",
                }
            }
        },
        status="enabled",
    )

    def _boom(**_: object) -> dict[str, object] | None:
        raise ToolExecutionError("browseract_live_transport_error:offline")

    monkeypatch.setattr(service, "_browseract_live_extract", _boom)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-inventory-2",
            step_id="step-browseract-inventory-2",
            tool_name="browseract.extract_account_inventory",
            action_kind="account.extract_inventory",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_names": ["BrowserAct", "UnknownService"],
                "requested_fields": ["tier", "account_email", "status"],
                "run_url": "https://browseract.example/run",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.output_json["missing_services"] == ["UnknownService"]
    assert result.output_json["services_json"][0]["plan_tier"] == "Tier 3"
    assert result.output_json["services_json"][1]["discovery_status"] == "missing"
    assert result.output_json["services_json"][1]["live_discovery_error"] == "browseract_live_transport_error:offline"
    assert result.output_json["services_json"][1]["structured_output_json"]["live_discovery_error"] == (
        "browseract_live_transport_error:offline"
    )


def test_tool_execution_service_rejects_foreign_connector_binding_scope() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-1",
        scope_json={"scopes": ["mail.readonly"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    with pytest.raises(ToolExecutionError) as exc:
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-3",
                step_id="step-3",
                tool_name="connector.dispatch",
                action_kind="delivery.send",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                    "channel": "email",
                    "recipient": "ops@example.com",
                    "content": "blocked dispatch",
                },
                context_json={"principal_id": "exec-1"},
            )
        )
    assert str(exc.value) == (
        f"connector_binding_scope_mismatch:{binding.binding_id}:email,email.send,mail,mail.send,send.mail"
    )


def test_tool_execution_service_rejects_connector_scope_mismatch() -> None:
    test_tool_execution_service_rejects_foreign_connector_binding_scope()


def test_tool_execution_service_rejects_foreign_browseract_binding_scope() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={"service_accounts_json": {"BrowserAct": {"tier": "Tier 3"}}},
        status="enabled",
    )

    with pytest.raises(ToolExecutionError, match="principal_scope_mismatch"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-browseract-2",
                step_id="step-browseract-2",
                tool_name="browseract.extract_account_facts",
                action_kind="account.extract",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                    "service_name": "BrowserAct",
                },
                context_json={"principal_id": "exec-2"},
            )
        )


def test_tool_execution_service_self_heals_missing_builtin_artifact_definition() -> None:
    artifacts = InMemoryArtifactRepository()
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(tool_runtime=tool_runtime, artifacts=artifacts)

    registry._rows.clear()  # type: ignore[attr-defined]
    registry._order.clear()  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-4",
            step_id="step-4",
            tool_name="artifact_repository",
            action_kind="artifact.save",
            payload_json={"source_text": "self-healed artifact"},
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "artifact_repository"
    assert tool_runtime.get_tool("artifact_repository") is not None
    saved = artifacts.get(result.target_ref)
    assert saved is not None
    assert saved.content == "self-healed artifact"


def test_tool_execution_service_self_heals_missing_builtin_connector_dispatch_definition() -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    channel_runtime = ChannelRuntimeService(
        observations=InMemoryObservationEventRepository(),
        outbox=InMemoryDeliveryOutboxRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        channel_runtime=channel_runtime,
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="gmail",
        external_account_ref="acct-self-heal",
        scope_json={"scopes": ["mail.send"]},
        auth_metadata_json={"provider": "google"},
        status="enabled",
    )

    registry._rows.pop("connector.dispatch", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "connector.dispatch"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-5",
            step_id="step-5",
            tool_name="connector.dispatch",
            action_kind="delivery.send",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "channel": "email",
                "recipient": "ops@example.com",
                "content": "self-healed dispatch",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "connector.dispatch"
    assert tool_runtime.get_tool("connector.dispatch") is not None


def test_tool_execution_service_self_heals_missing_builtin_browseract_definition() -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={"service_accounts_json": {"BrowserAct": {"tier": "Tier 3"}}},
        status="enabled",
    )

    registry._rows.pop("browseract.extract_account_facts", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.extract_account_facts"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-3",
            step_id="step-browseract-3",
            tool_name="browseract.extract_account_facts",
            action_kind="account.extract",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "service_name": "BrowserAct",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.extract_account_facts"
    assert tool_runtime.get_tool("browseract.extract_account_facts") is not None


def test_tool_execution_service_self_heals_missing_builtin_browseract_chatplayground_audit_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        status="enabled",
    )

    def _fake_audit(**_: object) -> dict[str, object]:
        return {
            "consensus": "default consensus",
            "summary": "default summary",
            "recommendation": "default recommendation",
            "roles": ["factuality", "adversarial", "completeness", "risk"],
            "disagreements": [],
            "risks": ["none"],
            "model_deltas": ["delta"],
            "instruction_trace": ["trace"],
            "raw_response": {"ok": True},
        }

    monkeypatch.setattr(service, "_browseract_chatplayground_audit", _fake_audit)
    registry._rows.pop("browseract.chatplayground_audit", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.chatplayground_audit"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-audit-1",
            step_id="step-browseract-audit-1",
            tool_name="browseract.chatplayground_audit",
            action_kind="audit.jury",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "prompt": "Review the proposed patch for edge cases.",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.chatplayground_audit"
    assert result.action_kind == "audit.jury"
    assert result.output_json["roles"] == ["factuality", "adversarial", "completeness", "risk"]
    assert result.receipt_json["handler_key"] == "browseract.chatplayground_audit"
    assert tool_runtime.get_tool("browseract.chatplayground_audit") is not None


def test_tool_execution_service_rejects_chatplayground_audit_without_prompt() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        status="enabled",
    )

    with pytest.raises(ToolExecutionError, match="prompt_required:browseract.chatplayground_audit"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-browseract-audit-no-prompt-1",
                step_id="step-browseract-audit-no-prompt-1",
                tool_name="browseract.chatplayground_audit",
                action_kind="audit.jury",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                },
                context_json={"principal_id": "exec-1"},
            )
        )


def test_tool_execution_service_chatplayground_audit_accepts_normalized_text_as_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSERACT_API_KEY", "browseract-key")
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    def _fake_audit(**kwargs: object) -> dict[str, object]:
        prompt = kwargs.get("prompt")
        if prompt is None and isinstance(kwargs.get("kwargs"), dict):
            prompt = kwargs["kwargs"].get("prompt")
            if prompt is None and isinstance(kwargs["kwargs"].get("payload"), dict):
                prompt = kwargs["kwargs"]["payload"].get("prompt")
        assert prompt == "Review this generated summary."
        return {
            "consensus": "pass",
            "recommendation": "ship",
            "disagreements": [],
            "risks": [],
            "model_deltas": [],
            "roles": ["factuality", "adversarial", "completeness", "risk"],
            "requested_at": "2026-03-18T00:00:00Z",
        }

    monkeypatch.setattr(service, "_browseract_chatplayground_audit", _fake_audit)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-audit-normalized-1",
            step_id="step-browseract-audit-normalized-1",
            tool_name="browseract.chatplayground_audit",
            action_kind="audit.review_light",
            payload_json={
                "normalized_text": "Review this generated summary.",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.chatplayground_audit"
    assert result.output_json["consensus"] == "pass"
    assert result.output_json["recommendation"] == "ship"


def test_tool_execution_service_uses_default_chatplayground_audit_roles_and_default_url(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        status="enabled",
    )

    def _fake_audit(*, run_url: str, request_payload: dict[str, object]) -> dict[str, object]:
        assert run_url == "https://web.chatplayground.ai/"
        assert request_payload["roles"] == ["factuality", "adversarial", "completeness", "risk"]
        assert request_payload["audit_scope"] == "jury"
        return {
            "consensus": "consistent result",
            "recommendation": "apply suggestion",
            "disagreements": [],
            "risks": [],
            "model_deltas": [],
            "instruction_trace": [],
            "roles": [],
            "raw_response": {},
        }

    monkeypatch.setattr(service, "_browseract_chatplayground_audit", _fake_audit)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-audit-2",
            step_id="step-browseract-audit-2",
            tool_name="browseract.chatplayground_audit",
            action_kind="audit.jury",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "prompt": "Validate migration plan for concurrency safety.",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.output_json["requested_url"] == "https://web.chatplayground.ai/"
    assert result.output_json["requested_roles"] == ["factuality", "adversarial", "completeness", "risk"]
    assert result.output_json["audit_scope"] == "jury"
    assert result.receipt_json["requested_url"] == "https://web.chatplayground.ai/"


def test_tool_execution_service_uses_env_backed_chatplayground_audit_without_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    monkeypatch.setenv("BROWSERACT_API_KEY", "chatplayground-key")
    monkeypatch.setenv("BROWSERACT_CHATPLAYGROUND_URL", "https://web.chatplayground.ai/")
    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_resolve_chatplayground_workflow",
        lambda self, *, payload, binding_metadata: ("", ""),
    )

    calls: list[tuple[str, dict[str, object], int]] = []

    def _fake_post_browseract_json(
        self,
        *,
        run_url: str,
        request_payload: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        calls.append((run_url, dict(request_payload), timeout_seconds))
        assert request_payload["principal_id"] == "exec-1"
        assert request_payload["binding_id"] == ""
        assert request_payload["roles"] == ["factuality", "adversarial", "completeness", "risk"]
        return {
            "consensus": "pass",
            "recommendation": "ship it",
            "disagreements": [],
            "risks": [],
            "model_deltas": [],
            "roles": request_payload["roles"],
            "requested_at": "2026-03-18T00:00:00Z",
        }

    monkeypatch.setattr(BrowserActToolAdapter, "_post_browseract_json", _fake_post_browseract_json)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-audit-env-1",
            step_id="step-browseract-audit-env-1",
            tool_name="browseract.chatplayground_audit",
            action_kind="audit.jury",
            payload_json={
                "principal_id": "exec-1",
                "prompt": "Validate migration plan for concurrency safety.",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert calls[0][0] == "https://web.chatplayground.ai/api/chat/lmsys"
    assert result.output_json["binding_id"] == ""
    assert result.output_json["requested_url"] == "https://web.chatplayground.ai/api/chat/lmsys"
    assert result.output_json["consensus"] == "pass"
    assert result.receipt_json["handler"] == "run_url"


def test_tool_execution_service_uses_browseract_workflow_api_for_chatplayground_audit_without_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    monkeypatch.setenv("BROWSERACT_API_KEY", "chatplayground-key")

    calls: list[tuple[str, str, dict[str, object] | None, dict[str, str] | None]] = []

    def _fake_browseract_api_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        query: dict[str, str] | None = None,
        timeout_seconds: int = 120,
    ) -> dict[str, object]:
        calls.append((method, path, dict(payload or {}), dict(query or {})))
        if path == "/run-task":
            assert payload is not None
            assert payload["workflow_id"] == "workflow-123"
            assert payload["input_parameters"][0]["name"] == "prompt"
            rendered_prompt = str(payload["input_parameters"][0]["value"] or "")
            assert "Validate migration plan for concurrency safety." in rendered_prompt
            assert "return exactly one json object" in rendered_prompt.lower()
            assert '"consensus":"pass|fail|needs_revision|unavailable"' in rendered_prompt
            assert "<material>" in rendered_prompt
            return {"task_id": "task-456"}
        if path == "/get-task-status":
            assert query == {"task_id": "task-456"}
            return {"status": "finished"}
        if path == "/get-task":
            assert query == {"task_id": "task-456"}
            return {
                "status": "finished",
                "output": {
                    "string": json.dumps(
                        [
                            {
                                "audit_response": json.dumps(
                                    {
                                        "consensus": "pass",
                                        "recommendation": "ship it",
                                        "disagreements": [],
                                        "risks": [],
                                        "model_delta": ["delta"],
                                        "roles": ["factuality", "adversarial", "completeness", "risk"],
                                    }
                                )
                            }
                        ]
                    )
                },
            }
        raise AssertionError(f"unexpected BrowserAct API path: {path}")

    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_resolve_chatplayground_workflow",
        lambda self, *, payload, binding_metadata: ("workflow-123", "test-fixture"),
    )
    monkeypatch.setattr(BrowserActToolAdapter, "_browseract_api_request", _fake_browseract_api_request)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-audit-env-workflow-1",
            step_id="step-browseract-audit-env-workflow-1",
            tool_name="browseract.chatplayground_audit",
            action_kind="audit.jury",
            payload_json={
                "principal_id": "exec-1",
                "prompt": "Validate migration plan for concurrency safety.",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert [path for _, path, _, _ in calls] == ["/run-task", "/get-task-status", "/get-task"]
    assert result.output_json["consensus"] == "pass"
    assert result.output_json["model_deltas"] == ["delta"]
    assert result.output_json["workflow_prompt_chars"] > len("Validate migration plan for concurrency safety.")
    assert result.output_json["workflow_id"] == "workflow-123"
    assert result.output_json["task_id"] == "task-456"
    assert result.output_json["requested_url"] == "browseract://workflow/workflow-123/task/task-456"
    assert result.receipt_json["handler"] == "workflow_api"
    assert result.receipt_json["workflow_source"] == "test-fixture"


def test_tool_execution_service_retries_browseract_workflow_api_after_inconsistent_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    monkeypatch.setenv("BROWSERACT_API_KEY", "chatplayground-key")

    calls: list[tuple[str, str, dict[str, object] | None, dict[str, str] | None]] = []
    task_status_counts: dict[str, int] = {}

    def _fake_browseract_api_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        query: dict[str, str] | None = None,
        timeout_seconds: int = 120,
    ) -> dict[str, object]:
        calls.append((method, path, dict(payload or {}), dict(query or {})))
        if path == "/run-task":
            attempt = sum(1 for _, logged_path, _, _ in calls if logged_path == "/run-task")
            return {"task_id": f"task-{attempt}"}
        if path == "/get-task-status":
            task_id = str((query or {}).get("task_id") or "")
            task_status_counts[task_id] = task_status_counts.get(task_id, 0) + 1
            if task_id == "task-1":
                return {"status": "created"}
            if task_id == "task-2":
                return {"status": "finished"}
        if path == "/get-task":
            task_id = str((query or {}).get("task_id") or "")
            if task_id == "task-1":
                return {
                    "status": "created",
                    "finished_at": "2026-03-18T00:00:00Z",
                    "output": {"string": None, "files": None},
                    "steps": [],
                }
            if task_id == "task-2":
                return {
                    "status": "finished",
                    "output": {
                        "string": json.dumps(
                            [
                                {
                                    "audit_response": json.dumps(
                                        {
                                            "consensus": "pass",
                                            "recommendation": "ship it",
                                            "disagreements": [],
                                            "risks": [],
                                            "model_deltas": [],
                                            "roles": ["factuality", "adversarial", "completeness", "risk"],
                                        }
                                    )
                                }
                            ]
                        )
                    },
                }
        raise AssertionError(f"unexpected BrowserAct API path: {path}")

    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_resolve_chatplayground_workflow",
        lambda self, *, payload, binding_metadata: ("workflow-123", "test-fixture"),
    )
    monkeypatch.setattr(BrowserActToolAdapter, "_browseract_api_request", _fake_browseract_api_request)
    tick = count()
    monkeypatch.setattr("app.services.tool_execution_browseract_adapter.time.time", lambda: float(next(tick) * 5))
    monkeypatch.setattr("app.services.tool_execution_browseract_adapter.time.sleep", lambda _: None)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-audit-env-workflow-retry-1",
            step_id="step-browseract-audit-env-workflow-retry-1",
            tool_name="browseract.chatplayground_audit",
            action_kind="audit.jury",
            payload_json={
                "principal_id": "exec-1",
                "prompt": "Validate migration plan for concurrency safety.",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert [path for _, path, _, _ in calls].count("/run-task") == 2
    assert result.output_json["consensus"] == "pass"
    assert result.output_json["task_id"] == "task-2"


def test_tool_execution_service_keeps_polling_browseract_workflow_when_created_status_has_step_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    monkeypatch.setenv("BROWSERACT_API_KEY", "chatplayground-key")

    calls: list[tuple[str, str, dict[str, object] | None, dict[str, str] | None]] = []
    get_task_calls = 0

    def _fake_browseract_api_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        query: dict[str, str] | None = None,
        timeout_seconds: int = 120,
    ) -> dict[str, object]:
        nonlocal get_task_calls
        calls.append((method, path, dict(payload or {}), dict(query or {})))
        if path == "/run-task":
            return {"task_id": "task-progress-1"}
        if path == "/get-task-status":
            return {"status": "created"}
        if path == "/get-task":
            get_task_calls += 1
            if get_task_calls == 1:
                return {
                    "status": "created",
                    "finished_at": "2026-03-18T00:00:00Z",
                    "output": {"string": None, "files": None},
                    "steps": [{"id": "step-1", "status": "succeed", "step_goal": "Open ChatPlayground"}],
                }
            return {
                "status": "finished",
                "output": {
                    "string": json.dumps(
                        [
                            {
                                "audit_response": json.dumps(
                                    {
                                        "consensus": "pass",
                                        "recommendation": "ship it",
                                        "disagreements": [],
                                        "risks": [],
                                        "model_deltas": [],
                                        "roles": ["factuality", "adversarial", "completeness", "risk"],
                                    }
                                )
                            }
                        ]
                    )
                },
                "steps": [{"id": "step-2", "status": "succeed", "step_goal": "Extract audit response"}],
            }
        raise AssertionError(f"unexpected BrowserAct API path: {path}")

    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_resolve_chatplayground_workflow",
        lambda self, *, payload, binding_metadata: ("workflow-123", "test-fixture"),
    )
    monkeypatch.setattr(BrowserActToolAdapter, "_browseract_api_request", _fake_browseract_api_request)
    monkeypatch.setattr("app.services.tool_execution_browseract_adapter.time.sleep", lambda _: None)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-audit-env-workflow-progress-1",
            step_id="step-browseract-audit-env-workflow-progress-1",
            tool_name="browseract.chatplayground_audit",
            action_kind="audit.jury",
            payload_json={
                "principal_id": "exec-1",
                "prompt": "Validate migration plan for concurrency safety.",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert [path for _, path, _, _ in calls].count("/get-task") >= 2
    assert result.output_json["consensus"] == "pass"
    assert result.output_json["task_id"] == "task-progress-1"


def test_tool_execution_service_detects_chatplayground_human_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        status="enabled",
    )

    def _fake_audit(**_: object) -> dict[str, object]:
        return {
            "page_title": "ChatGPT",
            "visible_text": "Please verify you are human to continue",
            "requested_url": "https://web.chatplayground.ai/",
        }

    monkeypatch.setattr(service, "_browseract_chatplayground_audit", _fake_audit)

    with pytest.raises(ToolExecutionError, match="ui_lane_failure:chatplayground:challenge_required"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-browseract-audit-challenge",
                step_id="step-browseract-audit-challenge",
                tool_name="browseract.chatplayground_audit",
                action_kind="audit.jury",
                payload_json={
                    "binding_id": binding.binding_id,
                    "principal_id": "exec-1",
                    "prompt": "Validate migration plan for concurrency safety.",
                },
                context_json={"principal_id": "exec-1"},
            )
        )


def test_tool_execution_service_self_heals_missing_builtin_browseract_gemini_web_generate_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        status="enabled",
    )

    def _fake_generate(**_: object) -> dict[str, object]:
        return {
            "text": "browseract gemini response",
            "mode_used": "thinking",
            "latency_ms": 321,
            "citations": [],
        }

    monkeypatch.setattr(service, "_browseract_gemini_web_generate", _fake_generate)
    registry._rows.pop("browseract.gemini_web_generate", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.gemini_web_generate"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-gemini-web-1",
            step_id="step-browseract-gemini-web-1",
            tool_name="browseract.gemini_web_generate",
            action_kind="content.generate",
            payload_json={
                "binding_id": binding.binding_id,
                "packet": {
                    "objective": "Answer the question",
                    "instructions": "Be concise",
                    "condensed_history": "Earlier context",
                    "current_input": "What is the next step?",
                    "desired_format": "plain_text",
                    "fingerprint": "abc123",
                },
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.gemini_web_generate"
    assert result.output_json["text"] == "browseract gemini response"
    assert result.output_json["provider_backend"] == "gemini_web"
    assert result.receipt_json["route"] == "browseract.gemini_web_generate"
    assert tool_runtime.get_tool("browseract.gemini_web_generate") is not None


def test_tool_execution_service_self_heals_missing_builtin_browseract_onemin_billing_usage_definition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("EA_RESPONSES_PROVIDER_LEDGER_DIR", str(tmp_path))
    from app.services import responses_upstream as upstream

    upstream._test_reset_onemin_states()
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="acct-onemin-primary",
        scope_json={},
        auth_metadata_json={"onemin_billing_usage_run_url": "https://browseract.example/run/billing"},
        status="enabled",
    )

    def _fake_billing_usage(**_: object) -> dict[str, object]:
        return {
            "billing_usage_page": "\n".join(
                [
                    "Plan: BUSINESS",
                    "Billing Cycle: LIFETIME",
                    "Status: Active",
                    "Remaining credits: 1234567",
                    "Max credits: 2000000",
                    "Used percent: 38.27",
                    "Next top-up: 2026-03-31T00:00:00Z",
                    "Top-up amount: 2000000",
                    "Unlock Free Credits",
                    "Lifetime credits roll over month to month",
                ]
            )
        }

    service._browseract_onemin_billing_usage = _fake_billing_usage
    registry._rows.pop("browseract.onemin_billing_usage", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.onemin_billing_usage"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-onemin-billing-1",
            step_id="step-browseract-onemin-billing-1",
            tool_name="browseract.onemin_billing_usage",
            action_kind="billing.inspect",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "run_url": "https://browseract.example/run/billing",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.onemin_billing_usage"
    assert result.output_json["remaining_credits"] == 1234567
    assert result.output_json["next_topup_at"] == "2026-03-31T00:00:00Z"
    assert result.output_json["topup_amount"] == 2000000
    assert result.output_json["rollover_enabled"] is True
    assert result.output_json["plan_name"] == "BUSINESS"
    assert result.output_json["billing_cycle"] == "LIFETIME"
    assert result.output_json["subscription_status"] == "Active"
    assert result.output_json["daily_bonus_cta_text"] == "Unlock Free Credits"
    assert result.output_json["daily_bonus_available"] is True
    assert result.output_json["basis"] == "actual_billing_usage_page"
    assert result.output_json["structured_output_json"]["persisted_snapshot"]["remaining_credits"] == 1234567
    assert tool_runtime.get_tool("browseract.onemin_billing_usage") is not None


def test_tool_execution_service_uses_template_backed_onemin_billing_fallback_with_account_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("EA_RESPONSES_PROVIDER_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "owner_email": "owner@example.com",
                    }
                ]
            }
        ),
    )
    from app.services import responses_upstream as upstream

    upstream._test_reset_onemin_states()
    monkeypatch.setattr(
        upstream,
        "onemin_owner_rows",
        lambda: (
            {"account_name": "ONEMIN_AI_API_KEY", "owner_email": "owner@example.com", "owner_label": "owner@example.com"},
        ),
    )
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="acct-onemin-primary",
        scope_json={},
        auth_metadata_json={
            "onemin_account_credentials_json": {
                "ONEMIN_AI_API_KEY": {
                    "login_email": "slot@example.com",
                    "login_password": "slotpass",
                }
            },
            "browser_proxy_server": "http://proxy.pool.local:9000",
            "browser_proxy_username": "pool-user",
            "browser_proxy_password": "pool-pass",
            "browser_proxy_bypass": "localhost,127.0.0.1",
        },
        status="enabled",
    )
    observed: dict[str, object] = {}

    def _fake_template_direct(_cls, **kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {
            "billing_usage_page": "\n".join(
                [
                    "Plan: BUSINESS",
                    "Remaining credits: 12345",
                    "Max credits: 20000",
                    "Next top-up: 2026-03-31T00:00:00Z",
                    "Top-up amount: 20000",
                ]
            )
        }

    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_create_template_backed_ui_service_direct",
        classmethod(_fake_template_direct),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-onemin-billing-template-1",
            step_id="step-browseract-onemin-billing-template-1",
            tool_name="browseract.onemin_billing_usage",
            action_kind="billing.inspect",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "account_label": "ONEMIN_AI_API_KEY",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert observed["service"].service_key == "onemin_billing_usage"
    assert observed["request_payload"]["login_email"] == "slot@example.com"
    assert observed["request_payload"]["browseract_password"] == "slotpass"
    assert observed["request_payload"]["browser_proxy_server"] == "http://proxy.pool.local:9000"
    assert observed["request_payload"]["browser_proxy_username"] == "pool-user"
    assert observed["request_payload"]["browser_proxy_password"] == "pool-pass"
    assert observed["request_payload"]["browser_proxy_bypass"] == "localhost,127.0.0.1"
    assert observed["requested_inputs"]["browseract_username"] == "slot@example.com"
    assert observed["requested_inputs"]["browseract_password"] == "slotpass"
    assert result.output_json["remaining_credits"] == 12345
    assert result.output_json["next_topup_at"] == "2026-03-31T00:00:00Z"


def test_tool_execution_service_passes_proxy_settings_to_onemin_member_template_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("EA_RESPONSES_PROVIDER_LEDGER_DIR", str(tmp_path))
    from app.services import responses_upstream as upstream

    upstream._test_reset_onemin_states()
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="acct-onemin-primary",
        scope_json={},
        auth_metadata_json={
            "onemin_account_credentials_json": {
                "ONEMIN_AI_API_KEY": {
                    "login_email": "slot@example.com",
                    "login_password": "slotpass",
                }
            },
            "proxy_server": "http://proxy.pool.local:9100",
            "proxy_username": "members-user",
            "proxy_password": "members-pass",
        },
        status="enabled",
    )
    observed: dict[str, object] = {}

    def _fake_template_direct(_cls, **kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {
            "members_page": "\n".join(
                [
                    "Owner One - slot@example.com - active - owner",
                ]
            )
        }

    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_create_template_backed_ui_service_direct",
        classmethod(_fake_template_direct),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-onemin-members-template-1",
            step_id="step-browseract-onemin-members-template-1",
            tool_name="browseract.onemin_member_reconciliation",
            action_kind="billing.reconcile_members",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "account_label": "ONEMIN_AI_API_KEY",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert observed["service"].service_key == "onemin_member_reconciliation"
    assert observed["request_payload"]["browser_proxy_server"] == "http://proxy.pool.local:9100"
    assert observed["request_payload"]["browser_proxy_username"] == "members-user"
    assert observed["request_payload"]["browser_proxy_password"] == "members-pass"
    assert result.output_json["member_count"] == 1


def test_tool_execution_service_parses_onemin_billing_workflow_usage_history_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("EA_RESPONSES_PROVIDER_LEDGER_DIR", str(tmp_path))
    from app.services import responses_upstream as upstream

    upstream._test_reset_onemin_states()
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="acct-onemin-primary",
        scope_json={},
        auth_metadata_json={"onemin_billing_usage_run_url": "https://browseract.example/run/billing"},
        status="enabled",
    )

    def _fake_billing_usage(**_: object) -> dict[str, object]:
        return {
            "task_id": "task-usage-1",
            "status": "finished",
            "input_parameters": "browseract_username=owner@example.com; browseract_password=topsecret",
            "steps": [
                {
                    "task_element_order": 1,
                    "goal": "Navigate to login",
                    "status": "succeed",
                }
            ],
            "output": {
                "string": json.dumps(
                    [
                        {
                            "user": "Test User",
                            "before_deduction": 2716780,
                            "after_deduction": 2716749,
                            "credit": 31,
                            "date": "2026-03-20",
                            "time": "17:38:27",
                        },
                        {
                            "user": "Test User",
                            "before_deduction": 2738801,
                            "after_deduction": 2716997,
                            "credit": 21804,
                            "date": "2026-03-20",
                            "time": "15:58:28",
                        },
                    ]
                )
            },
        }

    service._browseract_onemin_billing_usage = _fake_billing_usage
    registry._rows.pop("browseract.onemin_billing_usage", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.onemin_billing_usage"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-onemin-billing-usage-1",
            step_id="step-browseract-onemin-billing-usage-1",
            tool_name="browseract.onemin_billing_usage",
            action_kind="billing.inspect",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "run_url": "https://browseract.example/run/billing",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.output_json["remaining_credits"] == 2716749
    assert result.output_json["basis"] == "actual_billing_usage_page"
    assert result.output_json["usage_history_count"] == 2
    assert result.output_json["latest_usage_at"] == "2026-03-20T17:38:27Z"
    assert result.output_json["earliest_usage_at"] == "2026-03-20T15:58:28Z"
    assert result.output_json["latest_usage_credit"] == 31
    assert result.output_json["observed_usage_credits_total"] == 21835
    assert result.output_json["observed_usage_window_hours"] == pytest.approx(1.6664)
    assert result.output_json["observed_usage_burn_credits_per_hour"] == pytest.approx(13103.18)
    structured = result.output_json["structured_output_json"]
    assert structured["usage_history_json"][0]["after_deduction"] == 2716749
    assert structured["usage_summary_json"]["observed_usage_credits_total"] == 21835
    assert "browseract_password" not in structured["raw_text"]
    assert "topsecret" not in structured["raw_text"]
    assert "input_parameters" not in structured["label_map"]
    assert structured["persisted_snapshot"]["remaining_credits"] == 2716749


def test_tool_execution_service_parses_sectioned_onemin_billing_workflow_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("EA_RESPONSES_PROVIDER_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps({"slots": [{"owner_email": "owner@example.com"}]}),
    )
    from app.services import responses_upstream as upstream

    upstream._test_reset_onemin_states()
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="acct-onemin-primary",
        scope_json={},
        auth_metadata_json={"onemin_billing_usage_run_url": "https://browseract.example/run/billing"},
        status="enabled",
    )

    def _fake_billing_usage(**_: object) -> dict[str, object]:
        return {
            "billing_usage_bonus_page": [
                {
                    "section_type": "billing_settings_page",
                    "plan_name": "BUSINESS",
                    "billing_cycle": "LIFETIME",
                    "status": "Active",
                    "current_credit": 4264349,
                    "manage_subscription_button_text": "Manage Subscription",
                    "top_up_credits_button_text": "Top Up Credits",
                    "unlock_free_credits_button_text": "Unlock Free Credits",
                },
                {
                    "section_type": "usage_records_page",
                    "user_name": "Test User",
                    "before_deduction": 4264380,
                    "after_deduction": 4264349,
                    "credit": 31,
                    "date": "2026-03-20 19:47:24",
                },
                {
                    "section_type": "usage_records_page",
                    "user_name": "Test User",
                    "before_deduction": 4264411,
                    "after_deduction": 4264380,
                    "credit": 31,
                    "date": "2026-03-20 19:45:07",
                },
                {
                    "section_type": "billing_usage_bonus_page",
                    "bonus_type": "Daily Visit Credits",
                    "bonus_credits": 30000,
                    "description": "Unlock 15,000 FREE credits EVERY DAY.",
                },
                {
                    "section_type": "billing_usage_bonus_page",
                    "bonus_type": "Referral Signup Bonus",
                    "bonus_credits": 20000,
                    "description": "Referral bonus",
                },
            ]
        }

    service._browseract_onemin_billing_usage = _fake_billing_usage
    registry._rows.pop("browseract.onemin_billing_usage", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.onemin_billing_usage"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-onemin-billing-sections-1",
            step_id="step-browseract-onemin-billing-sections-1",
            tool_name="browseract.onemin_billing_usage",
            action_kind="billing.inspect",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "run_url": "https://browseract.example/run/billing",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.output_json["remaining_credits"] == 4264349
    assert result.output_json["plan_name"] == "BUSINESS"
    assert result.output_json["billing_cycle"] == "LIFETIME"
    assert result.output_json["subscription_status"] == "Active"
    assert result.output_json["daily_bonus_cta_text"] == "Unlock Free Credits"
    assert result.output_json["daily_bonus_available"] is True
    assert result.output_json["daily_bonus_credits"] == 30000
    assert result.output_json["usage_history_count"] == 2
    assert result.output_json["observed_usage_credits_total"] == 62
    structured = result.output_json["structured_output_json"]
    assert structured["visible_actions_json"] == [
        "Manage Subscription",
        "Top Up Credits",
        "Unlock Free Credits",
    ]
    assert structured["visible_tabs_json"] == ["Subscription", "Usage Records", "Voucher"]
    assert structured["billing_overview_json"]["plan_name"] == "BUSINESS"
    assert structured["billing_overview_json"]["daily_bonus_credits"] == 30000
    assert structured["bonus_catalog_json"][0]["bonus_type"] == "Daily Visit Credits"


def test_tool_execution_service_parses_json_array_raw_text_onemin_billing_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("EA_RESPONSES_PROVIDER_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps({"slots": [{"owner_email": "owner@example.com"}]}),
    )
    from app.services import responses_upstream as upstream

    upstream._test_reset_onemin_states()
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="acct-onemin-primary",
        scope_json={},
        auth_metadata_json={"onemin_billing_usage_run_url": "https://browseract.example/run/billing"},
        status="enabled",
    )

    def _fake_billing_usage(**_: object) -> dict[str, object]:
        return {
            "billing_usage_bonus_page": json.dumps(
                [
                    {
                        "plan_name": "BUSINESS",
                        "billing_cycle": "LIFETIME",
                        "status": "Active",
                        "available_credit": 4264349,
                    },
                    {
                        "user_name": "Test User",
                        "before_deduction": 4264380,
                        "after_deduction": 4264349,
                        "credit": 31,
                        "date": "2026-03-20",
                        "time": "19:47:24",
                        "record_type": "Show record",
                    },
                    {
                        "bonus_type": "Daily Visit",
                        "bonus_description": "Unlock 15,000 FREE credits EVERY DAY.",
                        "bonus_credits": 15000,
                        "requirement": "Visit the web app once per day.",
                    },
                ]
            )
        }

    service._browseract_onemin_billing_usage = _fake_billing_usage
    registry._rows.pop("browseract.onemin_billing_usage", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.onemin_billing_usage"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-onemin-billing-json-text-1",
            step_id="step-browseract-onemin-billing-json-text-1",
            tool_name="browseract.onemin_billing_usage",
            action_kind="billing.inspect",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "run_url": "https://browseract.example/run/billing",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.output_json["remaining_credits"] == 4264349
    assert result.output_json["plan_name"] == "BUSINESS"
    assert result.output_json["billing_cycle"] == "LIFETIME"
    assert result.output_json["subscription_status"] == "Active"
    assert result.output_json["daily_bonus_available"] is True
    assert result.output_json["daily_bonus_credits"] == 15000
    structured = result.output_json["structured_output_json"]
    assert structured["billing_overview_json"]["plan_name"] == "BUSINESS"
    assert structured["billing_overview_json"]["daily_bonus_credits"] == 15000
    assert structured["bonus_catalog_json"][0]["bonus_type"] == "Daily Visit"


def test_tool_execution_service_parses_nested_extract_text_onemin_billing_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("EA_RESPONSES_PROVIDER_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps({"slots": [{"owner_email": "owner@example.com"}]}),
    )
    from app.services import responses_upstream as upstream

    upstream._test_reset_onemin_states()
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="acct-onemin-primary",
        scope_json={},
        auth_metadata_json={"onemin_billing_usage_run_url": "https://browseract.example/run/billing"},
        status="enabled",
    )

    def _fake_billing_usage(**_: object) -> dict[str, object]:
        return {
            "editor_url": "https://app.1min.ai/free-credits",
            "body_text": "Free Credits\n4,265,000",
            "structured_output_json": {
                "extracts": {
                    "billing_settings_page": (
                        "Billing - Usage\n"
                        "4,265,000\n"
                        "Plan\nBUSINESS\n"
                        "Billing Cycle\nLIFETIME\n"
                        "Status\nActive\n"
                        "Credit\n4,265,000\n"
                        "Manage Subscription\n"
                        "Top Up Credits\n"
                        "Unlock Free Credits\n"
                    ),
                    "usage_records_page": "Usage Records\nNo data\n",
                    "billing_usage_bonus_page": "Free Credits\n4,265,000\nDaily Visit Credits\n15,000\n",
                }
            },
        }

    service._browseract_onemin_billing_usage = _fake_billing_usage
    registry._rows.pop("browseract.onemin_billing_usage", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.onemin_billing_usage"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-onemin-billing-extract-text-1",
            step_id="step-browseract-onemin-billing-extract-text-1",
            tool_name="browseract.onemin_billing_usage",
            action_kind="billing.inspect",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "run_url": "https://browseract.example/run/billing",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.output_json["remaining_credits"] == 4265000
    assert result.output_json["plan_name"] == "BUSINESS"
    assert result.output_json["billing_cycle"] == "LIFETIME"
    assert result.output_json["subscription_status"] == "Active"
    structured = result.output_json["structured_output_json"]
    assert structured["visible_actions_json"] == [
        "Manage Subscription",
        "Top Up Credits",
        "Unlock Free Credits",
    ]
    assert structured["visible_tabs_json"] == ["Subscription", "Usage Records", "Voucher"]


def test_tool_execution_service_self_heals_missing_builtin_browseract_onemin_member_reconciliation_definition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("EA_RESPONSES_PROVIDER_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {"owner_email": "owner1@example.com"},
                    {"owner_email": "owner2@example.com"},
                ]
            }
        ),
    )
    from app.services import responses_upstream as upstream

    upstream._test_reset_onemin_states()
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="acct-onemin-primary",
        scope_json={},
        auth_metadata_json={"onemin_members_run_url": "https://browseract.example/run/members"},
        status="enabled",
    )

    def _fake_member_reconciliation(**_: object) -> dict[str, object]:
        return {
            "members_page": "\n".join(
                [
                    "Owner One - owner1@example.com - active - owner",
                    "Other User - other@example.com - active - member - limit 500000",
                ]
            )
        }

    service._browseract_onemin_member_reconciliation = _fake_member_reconciliation
    registry._rows.pop("browseract.onemin_member_reconciliation", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.onemin_member_reconciliation"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-onemin-members-1",
            step_id="step-browseract-onemin-members-1",
            tool_name="browseract.onemin_member_reconciliation",
            action_kind="billing.reconcile_members",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "run_url": "https://browseract.example/run/members",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.onemin_member_reconciliation"
    assert result.output_json["member_count"] == 2
    assert result.output_json["matched_owner_slots"] == 1
    assert result.output_json["missing_owner_emails"] == ["owner2@example.com"]
    assert result.output_json["owner_mismatches"][0]["email"] == "other@example.com"
    assert result.output_json["structured_output_json"]["persisted_snapshot"]["member_count"] == 2
    assert tool_runtime.get_tool("browseract.onemin_member_reconciliation") is not None


def test_tool_execution_service_self_heals_missing_builtin_browseract_crezlo_property_tour_definition() -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="crezlo-workspace-1",
        scope_json={},
        auth_metadata_json={"crezlo_property_tour_workflow_id": "wf-crezlo-1"},
        status="enabled",
    )

    def _fake_crezlo_property_tour(**kwargs: object) -> dict[str, object]:
        requested_inputs = dict(kwargs.get("requested_inputs") or {})
        assert requested_inputs["tour_title"] == "Kahlenberg Variant A"
        assert requested_inputs["property_url"] == "https://www.willhaben.at/listing/kahlenberg"
        assert requested_inputs["theme_name"] == "Cinematic Warm"
        return {
            "task_id": "task-crezlo-1",
            "status": "completed",
            "output": {
                "result": {
                    "tour_title": requested_inputs["tour_title"],
                    "tour_status": "published",
                    "share_url": "https://tours.crezlo.com/share/kahlenberg-variant-a",
                    "editor_url": "https://tours.crezlo.com/admin/tours/kahlenberg-variant-a",
                    "public_url": "https://ea-property-tours-20260320.crezlotours.com/tours/kahlenberg-variant-a",
                }
            },
        }

    service._browseract_crezlo_property_tour = _fake_crezlo_property_tour
    registry._rows.pop("browseract.crezlo_property_tour", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.crezlo_property_tour"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-crezlo-tour-1",
            step_id="step-browseract-crezlo-tour-1",
            tool_name="browseract.crezlo_property_tour",
            action_kind="property_tour.create",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "tour_title": "Kahlenberg Variant A",
                "property_url": "https://www.willhaben.at/listing/kahlenberg",
                "theme_name": "Cinematic Warm",
                "media_urls_json": [
                    "https://assets.example/photo-1.jpg",
                    "https://assets.example/photo-2.jpg",
                ],
                "floorplan_urls_json": [
                    "https://assets.example/floorplan-1.jpg",
                ],
                "property_facts_json": {
                    "listing_title": "Exklusive 2 Zimmer Wohnung mit Blick auf den Kahlenberg",
                    "rooms": "2",
                    "area_sqm": "58",
                },
                "login_email": "the.girscheles@gmail.com",
                "login_password": "fixture-crezlo-password",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.crezlo_property_tour"
    assert result.output_json["tour_status"] == "blocked"
    assert not result.output_json.get("share_url")
    assert not result.output_json.get("public_url")
    assert result.output_json["workflow_id"] == "wf-crezlo-1"
    structured = result.output_json["structured_output_json"]
    assert structured["requested_url"] == "browseract://workflow/wf-crezlo-1"
    assert structured["requested_inputs"]["theme_name"] == "Cinematic Warm"
    assert "login_password" not in structured["requested_inputs"]
    assert "login_email" not in structured["requested_inputs"]
    assert tool_runtime.get_tool("browseract.crezlo_property_tour") is not None


def test_tool_execution_service_self_heals_missing_builtin_browseract_mootion_movie_definition() -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={"mootion_movie_workflow_id": "wf-mootion-1"},
        status="enabled",
    )

    def _fake_mootion_movie(**kwargs: object) -> dict[str, object]:
        requested_inputs = dict(kwargs.get("requested_inputs") or {})
        assert requested_inputs["prompt"] == "Create a cinematic teaser for the Brigittenau shortlist."
        assert requested_inputs["visual_style"] == "cinematic_real_estate"
        assert requested_inputs["aspect_ratio"] == "16:9"
        return {
            "task_id": "task-mootion-1",
            "status": "completed",
            "output": {
                "result": {
                    "title": "Brigittenau Shortlist Teaser",
                    "status": "rendered",
                    "video_url": "https://cdn.example/mootion/brigittenau-shortlist.mp4",
                    "download_url": "https://cdn.example/mootion/brigittenau-shortlist.mp4?download=1",
                    "preview_url": "https://viewer.example/mootion/brigittenau-shortlist",
                    "editor_url": "https://mootion.com/projects/brigittenau-shortlist",
                }
            },
        }

    service._browseract_ui_service_callbacks["mootion_movie"] = _fake_mootion_movie
    registry._rows.pop("browseract.mootion_movie", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.mootion_movie"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-mootion-1",
            step_id="step-browseract-mootion-1",
            tool_name="browseract.mootion_movie",
            action_kind="movie.render",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "script_text": "Create a cinematic teaser for the Brigittenau shortlist.",
                "title": "Brigittenau Shortlist Teaser",
                "visual_style": "cinematic_real_estate",
                "aspect_ratio": "16:9",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.mootion_movie"
    assert result.output_json["service_key"] == "mootion_movie"
    assert result.output_json["result_title"] == "Brigittenau Shortlist Teaser"
    assert result.output_json["render_status"] == "rendered"
    assert result.output_json["asset_url"] == "https://cdn.example/mootion/brigittenau-shortlist.mp4"
    assert result.output_json["public_url"] == "https://viewer.example/mootion/brigittenau-shortlist"
    assert result.output_json["editor_url"] == "https://mootion.com/projects/brigittenau-shortlist"
    assert result.output_json["workflow_id"] == "wf-mootion-1"
    structured = result.output_json["structured_output_json"]
    assert structured["requested_inputs"]["prompt"] == "Create a cinematic teaser for the Brigittenau shortlist."
    assert tool_runtime.get_tool("browseract.mootion_movie") is not None


def test_tool_execution_service_injects_ui_service_credentials_for_browseract_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={
            "mootion_movie_workflow_id": "wf-mootion-1",
            "service_accounts_json": {
                "Mootion": {
                    "account_email": "binding-mootion@example.com",
                }
            },
        },
        status="enabled",
    )
    captured: dict[str, object] = {}

    def _fake_run_workflow(self, *, workflow_id: str, input_values: dict[str, object]) -> dict[str, object]:
        captured["workflow_id"] = workflow_id
        captured["input_values"] = dict(input_values)
        return {"task_id": "task-mootion-2"}

    def _fake_wait(self, *, task_id: str, timeout_seconds: int, created_stall_seconds: int = 120) -> dict[str, object]:
        captured["task_id"] = task_id
        captured["timeout_seconds"] = timeout_seconds
        captured["created_stall_seconds"] = created_stall_seconds
        return {
            "task_id": task_id,
            "status": "completed",
            "output": {
                "result": {
                    "title": "Workflow Credential Smoke",
                    "status": "rendered",
                    "video_url": "https://cdn.example/mootion/workflow-credential-smoke.mp4",
                    "preview_url": "https://viewer.example/mootion/workflow-credential-smoke",
                }
            },
        }

    monkeypatch.setenv("EA_UI_SERVICE_LOGIN_EMAIL", "env-default@example.com")
    monkeypatch.setenv("EA_UI_SERVICE_LOGIN_PASSWORD", "env-pass-123")
    monkeypatch.setattr(BrowserActToolAdapter, "_run_browseract_workflow_task_with_inputs", _fake_run_workflow)
    monkeypatch.setattr(BrowserActToolAdapter, "_wait_for_browseract_task", _fake_wait)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-mootion-credentials-1",
            step_id="step-browseract-mootion-credentials-1",
            tool_name="browseract.mootion_movie",
            action_kind="movie.render",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "script_text": "Create a cinematic teaser for the Brigittenau shortlist.",
                "title": "Workflow Credential Smoke",
                "force_browseract": True,
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert captured["workflow_id"] == "wf-mootion-1"
    assert captured["input_values"] == {
        "prompt": "Create a cinematic teaser for the Brigittenau shortlist.",
        "title": "Workflow Credential Smoke",
        "browseract_username": "binding-mootion@example.com",
        "browseract_password": "env-pass-123",
    }
    assert result.output_json["asset_url"] == "https://cdn.example/mootion/workflow-credential-smoke.mp4"
    assert result.output_json["public_url"] == "https://viewer.example/mootion/workflow-credential-smoke"
    assert result.output_json["workflow_id"] == "wf-mootion-1"
    assert result.output_json["structured_output_json"]["requested_inputs"]["prompt"] == (
        "Create a cinematic teaser for the Brigittenau shortlist."
    )
    assert "browseract_username" not in result.output_json["structured_output_json"]["requested_inputs"]
    assert "browseract_password" not in result.output_json["structured_output_json"]["requested_inputs"]


def test_tool_execution_service_uses_binding_account_email_for_direct_ui_worker_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={
            "service_accounts_json": {
                "AvoMap": {
                    "account_email": "binding-avomap@example.com",
                }
            },
        },
        status="enabled",
    )
    captured: dict[str, object] = {}

    def _fake_worker(cls, *, service_key: str, packet: dict[str, object], timeout_seconds: int) -> dict[str, object]:
        captured["service_key"] = service_key
        captured["packet"] = dict(packet)
        captured["timeout_seconds"] = timeout_seconds
        return {
            "service_key": "avomap_flyover",
            "result_title": "Brigittenau Flyover Direct",
            "render_status": "completed",
            "asset_url": "https://cdn.example/avomap/brigittenau-flyover.mp4",
            "editor_url": "https://app.avomap.com/projects/brigittenau-flyover",
            "mime_type": "video/mp4",
            "structured_output_json": {
                "result_title": "Brigittenau Flyover Direct",
                "render_status": "completed",
            },
        }

    def _fake_publish(cls, row: dict[str, object]) -> str:
        captured["published_row"] = dict(row)
        return "https://ea.girschele.com/results/avomap/brigittenau-flyover"

    monkeypatch.setenv("EA_UI_SERVICE_LOGIN_EMAIL", "env-default@example.com")
    monkeypatch.setenv("EA_UI_SERVICE_LOGIN_PASSWORD", "env-direct-pass")
    monkeypatch.setattr(BrowserActToolAdapter, "_run_ui_service_worker", classmethod(_fake_worker))
    monkeypatch.setattr(BrowserActToolAdapter, "_publish_ui_service_result", classmethod(_fake_publish))

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-avomap-direct-1",
            step_id="step-browseract-avomap-direct-1",
            tool_name="browseract.avomap_flyover",
            action_kind="map.flyover_render",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "route_data": "LINESTRING(16.3665 48.2356, 16.3727 48.2392)",
                "title": "Brigittenau Flyover Direct",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert captured["service_key"] == "avomap_flyover"
    assert captured["packet"]["login_email"] == "binding-avomap@example.com"
    assert captured["packet"]["login_password"] == "env-direct-pass"
    assert captured["packet"]["route_data"] == "LINESTRING(16.3665 48.2356, 16.3727 48.2392)"
    assert result.output_json["public_url"] == "https://ea.girschele.com/results/avomap/brigittenau-flyover"
    assert result.output_json["asset_url"] == "https://cdn.example/avomap/brigittenau-flyover.mp4"
    assert result.output_json["structured_output_json"]["requested_inputs"]["route_data"] == (
        "LINESTRING(16.3665 48.2356, 16.3727 48.2392)"
    )
    assert "login_email" not in result.output_json["structured_output_json"]["requested_inputs"]
    assert "login_password" not in result.output_json["structured_output_json"]["requested_inputs"]


def test_tool_execution_service_executes_template_backed_ui_worker_without_workflow_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={
            "service_accounts_json": {
                "Paperguide": {
                    "account_email": "binding-paperguide@example.com",
                }
            },
        },
        status="enabled",
    )
    captured: dict[str, object] = {}

    def _fake_worker(cls, *, service_key: str, packet: dict[str, object], timeout_seconds: int) -> dict[str, object]:
        captured["service_key"] = service_key
        captured["packet"] = dict(packet)
        captured["timeout_seconds"] = timeout_seconds
        return {
            "service_key": "paperguide_workspace_reader",
            "result_title": "Paperguide Research Surface",
            "render_status": "completed",
            "asset_path": "/tmp/paperguide-research-surface.html",
            "mime_type": "text/html",
            "editor_url": "https://paperguide.ai/workspace/research",
            "raw_text": "Paperguide workspace captured.",
            "structured_output_json": {
                "service": "Paperguide",
                "template_key": "paperguide_workspace_reader",
                "render_status": "completed",
            },
        }

    def _fake_publish(cls, row: dict[str, object]) -> str:
        captured["published_row"] = dict(row)
        return "https://ea.girschele.com/results/paperguide-research-surface"

    monkeypatch.setenv("EA_UI_SERVICE_LOGIN_PASSWORD", "env-template-pass")
    monkeypatch.setattr(BrowserActToolAdapter, "_run_ui_service_worker", classmethod(_fake_worker))
    monkeypatch.setattr(BrowserActToolAdapter, "_publish_ui_service_result", classmethod(_fake_publish))

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-paperguide-direct-1",
            step_id="step-browseract-paperguide-direct-1",
            tool_name="browseract.paperguide_workspace_reader",
            action_kind="workspace.capture",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "page_url": "https://paperguide.ai/workspace/research",
                "title": "Paperguide Research Surface",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    packet = dict(captured["packet"])
    assert captured["service_key"] == "paperguide_workspace_reader"
    assert packet["login_email"] == "binding-paperguide@example.com"
    assert packet["login_password"] == "env-template-pass"
    assert packet["template_key"] == "paperguide_workspace_reader"
    assert packet["workflow_spec_json"]["meta"]["slug"] == "paperguide_workspace_reader"
    assert packet["workflow_spec_json"]["meta"]["workflow_kind"] == "page_extract"
    assert packet["page_url"] == "https://paperguide.ai/workspace/research"
    assert result.output_json["public_url"] == "https://ea.girschele.com/results/paperguide-research-surface"
    assert result.output_json["editor_url"] == "https://paperguide.ai/workspace/research"
    assert result.output_json["requested_url"] == "browseract-template://paperguide_workspace_reader"
    assert result.output_json["structured_output_json"]["requested_inputs"]["page_url"] == (
        "https://paperguide.ai/workspace/research"
    )


def test_browseract_ui_template_spec_waits_for_direct_login_fields() -> None:
    spec = browseract_ui_template_spec("approvethis_queue_reader")
    assert spec["meta"]["authorized_credential_queries"] == ["approvethis.com"]
    node_ids = [str(node.get("id") or "") for node in spec["nodes"]]
    assert "wait_login_form" in node_ids
    wait_node = next(node for node in spec["nodes"] if node.get("id") == "wait_login_form")
    assert wait_node["type"] == "wait"
    assert wait_node["config"]["timeout_ms"] == 45000
    assert ["open_login", "wait_login_form"] in spec["edges"]
    assert ["wait_login_form", "email"] in spec["edges"]
    submit_node = next(node for node in spec["nodes"] if node.get("id") == "submit")
    assert submit_node["type"] == "submit_login_form"
    assert submit_node["config"]["password_selector"] == (
        "input[type=password], input[name=password], input[name=Passwd], "
        "input[autocomplete='current-password'], input[placeholder*='Password' i]"
    )


def test_browseract_ui_template_spec_waits_for_google_entry_before_click() -> None:
    spec = browseract_ui_template_spec("paperguide_workspace_reader")
    assert spec["meta"]["auth_flow"] == "google_oauth"
    assert spec["meta"]["authorized_credential_queries"] == ["google.com"]
    node_ids = [str(node.get("id") or "") for node in spec["nodes"]]
    assert "wait_google_entry" in node_ids
    wait_node = next(node for node in spec["nodes"] if node.get("id") == "wait_google_entry")
    assert wait_node["type"] == "wait"
    assert wait_node["config"]["selector"] == (
        "button:has-text(\"Login with Google\"), a:has-text(\"Login with Google\")"
    )
    assert ["open_login", "wait_google_entry"] in spec["edges"]
    assert ["wait_google_entry", "enter_google"] in spec["edges"]


def test_browseract_ui_template_spec_uses_explicit_onemin_billing_workflow() -> None:
    spec = browseract_ui_template_spec("onemin_billing_usage_reader_live")
    node_ids = [str(node.get("id") or "") for node in spec["nodes"]]
    assert spec["meta"]["tool_url"] == "https://app.1min.ai/billing-usage"
    assert spec["meta"]["authorized_credential_queries"] == ["1min.ai", "app.1min.ai"]
    assert spec["inputs"][-1]["name"] == "page_url"
    assert "open_billing_usage" in node_ids
    assert "extract_billing_settings" in node_ids
    assert "extract_usage_records" in node_ids
    assert "extract_pre_bonus_page" in node_ids
    assert "extract_billing_bonus_page" in node_ids
    open_login_entry = next(node for node in spec["nodes"] if node.get("id") == "open_login_entry")
    assert open_login_entry["config"]["dom_click"] is True
    assert open_login_entry["config"]["react_click"] is True
    submit_node = next(node for node in spec["nodes"] if node.get("id") == "submit")
    assert submit_node["type"] == "submit_login_form"
    assert submit_node["config"]["react_click"] is True
    assert submit_node["config"]["form_selector"] == "form[name='login'], .ant-modal form, .ant-modal-root form, form"
    assert submit_node["config"]["auth_advance_timeout_ms"] == 12000
    assert submit_node["config"]["pre_submit_cookie_name"] == "cf_clearance"
    assert submit_node["config"]["submit_retry_count"] == 1
    assert "wait_pre_auth_dismiss_overlay_01" in node_ids
    assert ["open_login_entry", "wait_pre_auth_dismiss_overlay_01"] in spec["edges"]
    assert "otlp.1min.ai" in spec["meta"]["blocked_url_markers"]
    output_node = next(node for node in spec["nodes"] if node.get("id") == "output_result")
    assert output_node["config"]["field_name"] == "billing_usage_bonus_page"


def test_browseract_ui_template_spec_uses_explicit_onemin_members_workflow() -> None:
    spec = browseract_ui_template_spec("onemin_members_reconciliation_live")
    node_ids = [str(node.get("id") or "") for node in spec["nodes"]]
    assert spec["meta"]["tool_url"] == "https://app.1min.ai/members"
    assert "open_members" in node_ids
    assert "extract_members" in node_ids
    wait_login_entry = next(node for node in spec["nodes"] if node.get("id") == "wait_login_entry")
    assert wait_login_entry["config"]["optional"] is True
    open_login_entry = next(node for node in spec["nodes"] if node.get("id") == "open_login_entry")
    assert open_login_entry["config"]["dom_click"] is True
    assert open_login_entry["config"]["react_click"] is True
    submit_node = next(node for node in spec["nodes"] if node.get("id") == "submit")
    assert submit_node["config"]["react_click"] is True
    assert submit_node["config"]["form_selector"] == "form[name='login'], .ant-modal form, .ant-modal-root form, form"
    assert submit_node["config"]["pre_submit_cookie_name"] == "cf_clearance"
    assert "wait_pre_auth_dismiss_overlay_01" in node_ids
    assert ["open_login_entry", "wait_pre_auth_dismiss_overlay_01"] in spec["edges"]
    dismiss_node = next(node for node in spec["nodes"] if node.get("id") == "dismiss_overlay_01")
    assert dismiss_node["config"]["optional"] is True


def test_browseract_ui_template_spec_omits_dismiss_overlay_clicks_by_default() -> None:
    spec = browseract_ui_template_spec("documentation_ai_workspace_reader")
    node_ids = [str(node.get("id") or "") for node in spec["nodes"]]
    assert "wait_google_entry" in node_ids
    assert not any(node_id.startswith("dismiss_") for node_id in node_ids)


def test_browseract_ui_template_spec_omits_runtime_only_open_tool_node() -> None:
    spec = browseract_ui_template_spec("documentation_ai_workspace_reader")
    node_ids = [str(node.get("id") or "") for node in spec["nodes"]]
    assert "open_tool" not in node_ids
    assert spec["inputs"] == []
    assert spec["meta"]["runtime_input_name"] == "page_url"


def test_browseract_ui_template_spec_supports_poppy_workspace_reader() -> None:
    spec = browseract_ui_template_spec("poppy_workspace_reader")
    node_ids = [str(node.get("id") or "") for node in spec["nodes"]]
    assert spec["meta"]["authorized_credential_queries"] == ["getpoppy.ai", "app.getpoppy.ai", "poppy.ai"]
    assert spec["meta"]["runtime_input_name"] == "page_url"
    open_login = next(node for node in spec["nodes"] if node.get("id") == "open_login")
    assert open_login["config"]["url"] == "https://app.getpoppy.ai/login"
    assert "wait_login_form" in node_ids
    assert "wait_dismiss_01" in node_ids
    assert "dismiss_01" in node_ids
    assert "open_tool" not in node_ids
    submit_node = next(node for node in spec["nodes"] if node.get("id") == "submit")
    assert submit_node["config"]["selector"].startswith('form button:has-text("Continue")')


def test_tool_execution_service_prefers_local_worker_for_template_backed_ui_service_even_with_workflow_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={
            "approvethis_queue_reader_workflow_id": "wf-approvethis-1",
            "service_accounts_json": {
                "ApproveThis": {
                    "account_email": "binding-approvethis@example.com",
                }
            },
        },
        status="enabled",
    )
    captured: dict[str, object] = {}

    def _fake_worker(cls, *, service_key: str, packet: dict[str, object], timeout_seconds: int) -> dict[str, object]:
        captured["service_key"] = service_key
        captured["packet"] = dict(packet)
        captured["timeout_seconds"] = timeout_seconds
        return {
            "service_key": "approvethis_queue_reader",
            "result_title": "ApproveThis Queue",
            "render_status": "completed",
            "asset_path": "/tmp/approvethis-queue.html",
            "mime_type": "text/html",
            "editor_url": "https://app.approvethis.com/requests",
            "raw_text": "ApproveThis queue captured.",
            "structured_output_json": {
                "service": "ApproveThis",
                "template_key": "approvethis_queue_reader",
                "render_status": "completed",
            },
        }

    def _fake_run_workflow(self, *, workflow_id: str, input_values: dict[str, object]) -> dict[str, object]:
        raise AssertionError("template-backed remote BrowserAct workflow should not run by default")

    def _fake_wait(self, *, task_id: str, timeout_seconds: int, created_stall_seconds: int = 120) -> dict[str, object]:
        raise AssertionError("template-backed remote BrowserAct wait should not run by default")

    monkeypatch.setenv("EA_UI_SERVICE_LOGIN_PASSWORD", "env-template-pass")
    monkeypatch.setattr(BrowserActToolAdapter, "_run_ui_service_worker", classmethod(_fake_worker))
    monkeypatch.setattr(BrowserActToolAdapter, "_run_browseract_workflow_task_with_inputs", _fake_run_workflow)
    monkeypatch.setattr(BrowserActToolAdapter, "_wait_for_browseract_task", _fake_wait)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-approvethis-live-1",
            step_id="step-browseract-approvethis-live-1",
            tool_name="browseract.approvethis_queue_reader",
            action_kind="workspace.capture",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "title": "ApproveThis Queue",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    packet = dict(captured["packet"])
    assert captured["service_key"] == "approvethis_queue_reader"
    assert captured["timeout_seconds"] == 360
    assert packet["workflow_id"] == "wf-approvethis-1"
    assert packet["browseract_username"] == "binding-approvethis@example.com"
    assert packet["browseract_password"] == "env-template-pass"
    assert packet["template_key"] == "approvethis_queue_reader"
    assert packet["workflow_spec_json"]["meta"]["slug"] == "approvethis_queue_reader"
    assert packet["workflow_spec_json"]["meta"]["workflow_kind"] == "page_extract"
    assert result.output_json["editor_url"] == "https://app.approvethis.com/requests"
    assert result.output_json["workflow_id"] == "wf-approvethis-1"
    assert result.output_json["requested_url"] == "browseract://workflow/wf-approvethis-1"


def test_tool_execution_service_falls_back_to_remote_browseract_when_template_worker_hits_auth_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={
            "paperguide_workspace_reader_workflow_id": "wf-paperguide-1",
            "service_accounts_json": {
                "Paperguide": {
                    "account_email": "binding-paperguide@example.com",
                }
            },
        },
        status="enabled",
    )
    captured: dict[str, object] = {}

    def _fake_worker(cls, *, service_key: str, packet: dict[str, object], timeout_seconds: int) -> dict[str, object]:
        captured["packet"] = dict(packet)
        return {
            "service_key": "paperguide_workspace_reader",
            "result_title": "Paperguide Workspace",
            "render_status": "auth_handoff_required",
            "editor_url": "https://accounts.google.com/signin/v2/identifier",
            "raw_text": "Sign in with Google to continue.",
            "structured_output_json": {
                "service": "Paperguide",
                "template_key": "paperguide_workspace_reader",
                "auth_handoff": {"state": "auth_handoff_required", "provider": "google"},
            },
        }

    def _fake_run_workflow(self, *, workflow_id: str, input_values: dict[str, object]) -> dict[str, object]:
        captured["workflow_id"] = workflow_id
        captured["workflow_inputs"] = dict(input_values)
        return {"task_id": "task-paperguide-1"}

    def _fake_wait(self, *, task_id: str, timeout_seconds: int, created_stall_seconds: int = 120) -> dict[str, object]:
        captured["task_id"] = task_id
        return {
            "status": "completed",
            "output": {
                "public_url": "https://ea.girschele.com/results/paperguide-workspace-remote",
                "editor_url": "https://paperguide.ai/workspace/research",
                "page_body": "Paperguide workspace captured remotely.",
            },
        }

    monkeypatch.setenv("EA_UI_SERVICE_LOGIN_PASSWORD", "env-template-pass")
    monkeypatch.setattr(BrowserActToolAdapter, "_run_ui_service_worker", classmethod(_fake_worker))
    monkeypatch.setattr(BrowserActToolAdapter, "_run_browseract_workflow_task_with_inputs", _fake_run_workflow)
    monkeypatch.setattr(BrowserActToolAdapter, "_wait_for_browseract_task", _fake_wait)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-paperguide-fallback-1",
            step_id="step-browseract-paperguide-fallback-1",
            tool_name="browseract.paperguide_workspace_reader",
            action_kind="workspace.capture",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "page_url": "https://paperguide.ai/workspace/research",
                "title": "Paperguide Workspace",
                "allow_browseract_remote_fallback": True,
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert captured["workflow_id"] == "wf-paperguide-1"
    assert captured["task_id"] == "task-paperguide-1"
    assert captured["workflow_inputs"]["page_url"] == "https://paperguide.ai/workspace/research"
    assert captured["workflow_inputs"]["browseract_username"] == "binding-paperguide@example.com"
    assert captured["workflow_inputs"]["browseract_password"] == "env-template-pass"
    assert result.output_json["public_url"] == "https://ea.girschele.com/results/paperguide-workspace-remote"
    assert result.output_json["editor_url"] == "https://paperguide.ai/workspace/research"
    assert result.output_json["requested_url"] == "browseract://workflow/wf-paperguide-1"


def test_wait_for_browseract_task_salvages_failed_optional_close_when_output_exists() -> None:
    adapter = BrowserActToolAdapter(connector_dispatch=None)  # type: ignore[arg-type]
    calls: list[tuple[str, str]] = []

    def _fake_api_request(
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        timeout_seconds: int = 60,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append((method, path))
        if path == "/get-task-status":
            return {"status": "failed"}
        if path == "/get-task":
            return {
                "status": "failed",
                "task_failure_info": {
                    "code": 5024,
                    "message": "target element not found",
                },
                "steps": [
                    {"step": 9, "status": "succeed", "step_goal": "Navigate to \"https://dashboard.documentation.ai/\""},
                    {"step": 10, "status": "failed", "step_goal": "Click on \"button[aria-label='Close']\" (target element not found)"},
                ],
                "output": {
                    "string": "Documentation.AI workspace captured remotely.",
                    "editor_url": "https://dashboard.documentation.ai/",
                },
            }
        raise AssertionError(path)

    adapter._browseract_api_request = _fake_api_request  # type: ignore[method-assign]

    task_body = adapter._wait_for_browseract_task(
        task_id="task-docsai-1",
        timeout_seconds=30,
        created_stall_seconds=30,
    )

    assert task_body["status"] == "failed"
    assert task_body["output"]["editor_url"] == "https://dashboard.documentation.ai/"
    assert calls == [("GET", "/get-task-status"), ("GET", "/get-task")]


def test_browseract_ui_payload_normalization_uses_task_body_when_output_is_empty() -> None:
    service = browseract_ui_service_by_service_key("documentation_ai_workspace_reader")
    assert service is not None
    normalized = BrowserActToolAdapter._normalize_browseract_ui_service_payload(
        service=service,
        response={
            "status": "failed",
            "live_url": "https://www.browseract.com/remote/docsai-live",
            "task_failure_info": {"code": 5024, "message": "target element not found"},
            "steps": [
                {"step": 9, "status": "succeed", "step_goal": "Navigate to \"https://dashboard.documentation.ai/\""},
                {"step": 10, "status": "failed", "step_goal": "Click on \"button[aria-label='Close']\" (target element not found)"},
            ],
            "output": {"string": "", "files": None},
        },
        workflow_id="wf-docsai-1",
        requested_url="browseract://workflow/wf-docsai-1",
        requested_inputs={"page_url": "https://dashboard.documentation.ai/"},
        result_title="Documentation AI Smoke",
    )
    assert normalized["public_url"] == "https://www.browseract.com/remote/docsai-live"
    assert normalized["render_status"] == "failed"


def test_tool_execution_service_executes_crezlo_property_tour_via_direct_api_remote_assets_without_browseract_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="crezlo-workspace-1",
        scope_json={},
        auth_metadata_json={
            "crezlo_workspace_id": "workspace-crezlo-1",
            "crezlo_workspace_domain": "ea-property-tours-20260320.crezlotours.com",
        },
        status="enabled",
    )

    created_files: list[dict[str, object]] = []
    created_scenes: list[dict[str, object]] = []

    def _fake_login(*, login_email: str, login_password: str, timeout_seconds: int = 120) -> str:
        assert login_email == "the.girscheles@gmail.com"
        assert login_password == "fixture-crezlo-password"
        return "crezlo-token-1"

    def _fake_create_tour(
        cls,
        *,
        access_token: str,
        workspace_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        assert access_token == "crezlo-token-1"
        assert workspace_id == "workspace-crezlo-1"
        assert payload["title"] == "Kahlenberg Variant B"
        assert payload["status"] == "draft"
        return {
            "id": "tour-crezlo-2",
            "title": "Kahlenberg Variant B",
            "slug": "kahlenberg-variant-b",
            "status": "draft",
        }

    def _fake_create_file_record(
        cls,
        *,
        access_token: str,
        workspace_id: str,
        name: str,
        mime_type: str,
        path: str,
        meta: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert access_token == "crezlo-token-1"
        assert workspace_id == "workspace-crezlo-1"
        created_files.append(
            {
                "name": name,
                "mime_type": mime_type,
                "path": path,
                "meta": dict(meta or {}),
            }
        )
        return {
            "id": f"file-{len(created_files)}",
            "name": name,
            "mime_type": mime_type,
            "path": path,
            "meta": dict(meta or {}),
        }

    def _fake_create_scenes(
        cls,
        *,
        access_token: str,
        workspace_id: str,
        tour_id: str,
        scenes: list[dict[str, object]],
    ) -> dict[str, object]:
        assert access_token == "crezlo-token-1"
        assert workspace_id == "workspace-crezlo-1"
        assert tour_id == "tour-crezlo-2"
        created_scenes.extend(scenes)
        return {"data": list(scenes)}

    def _fake_fetch(cls, *, access_token: str, workspace_id: str, tour_id: str) -> dict[str, object]:
        assert access_token == "crezlo-token-1"
        assert workspace_id == "workspace-crezlo-1"
        assert tour_id == "tour-crezlo-2"
        return {
            "id": tour_id,
            "title": "Kahlenberg Variant B",
            "slug": "kahlenberg-variant-b",
            "status": "published",
            "payload": [],
            "scenes": [
                {"id": "scene-1", "file": {"id": "file-1"}},
                {"id": "scene-2", "file": {"id": "file-2"}},
                {"id": "scene-3", "file": {"id": "file-3"}},
            ],
            "display_title": None,
            "is_private": False,
            "workspace_id": workspace_id,
            "folder": None,
        }

    def _fake_update(
        cls,
        *,
        access_token: str,
        workspace_id: str,
        tour_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        assert body["title"] == "Kahlenberg Variant B"
        assert body["display_title"] == "Kahlenberg Panorama"
        assert body["is_private"] is False
        return {
            "id": tour_id,
            "title": body["title"],
            "slug": "kahlenberg-variant-b",
            "status": "published",
            "payload": [],
            "display_title": body["display_title"],
            "is_private": body["is_private"],
            "workspace_id": workspace_id,
            "folder": None,
            "scenes": [
                {"id": "scene-1", "file": {"id": "file-1"}},
                {"id": "scene-2", "file": {"id": "file-2"}},
                {"id": "scene-3", "file": {"id": "file-3"}},
            ],
        }

    monkeypatch.setattr(BrowserActToolAdapter, "_crezlo_login", staticmethod(_fake_login))
    monkeypatch.setattr(BrowserActToolAdapter, "_crezlo_create_tour", classmethod(_fake_create_tour))
    monkeypatch.setattr(BrowserActToolAdapter, "_crezlo_create_file_record", classmethod(_fake_create_file_record))
    monkeypatch.setattr(BrowserActToolAdapter, "_crezlo_create_scenes", classmethod(_fake_create_scenes))
    monkeypatch.setattr(BrowserActToolAdapter, "_crezlo_fetch_tour_detail", classmethod(_fake_fetch))
    monkeypatch.setattr(BrowserActToolAdapter, "_crezlo_update_tour", classmethod(_fake_update))
    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_publish_crezlo_public_tour_bundle",
        classmethod(lambda cls, normalized: "https://ea.girschele.com/tours/kahlenberg-variant-b"),
    )

    registry._rows.pop("browseract.crezlo_property_tour", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "browseract.crezlo_property_tour"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-crezlo-tour-2",
            step_id="step-browseract-crezlo-tour-2",
            tool_name="browseract.crezlo_property_tour",
            action_kind="property_tour.create",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "tour_title": "Kahlenberg Variant B",
                "property_url": "https://www.willhaben.at/listing/kahlenberg",
                "scene_strategy": "layout_first",
                "scene_selection_json": {"max_photos": 2, "include_floorplans": True},
                "display_title": "Kahlenberg Panorama",
                "tour_visibility": "public",
                "media_urls_json": [
                    "https://assets.example/photo-1.jpg",
                    "https://assets.example/photo-2.jpg",
                ],
                "floorplan_urls_json": [
                    "https://assets.example/floorplan-1.jpg",
                ],
                "login_email": "the.girscheles@gmail.com",
                "login_password": "fixture-crezlo-password",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.crezlo_property_tour"
    assert result.output_json["tour_status"] == "blocked"
    assert result.output_json["tour_id"] == "tour-crezlo-2"
    assert result.output_json["slug"] == "kahlenberg-variant-b"
    assert result.output_json["creation_mode"] == "crezlo_api_remote_assets"
    assert result.output_json["scene_count"] == 3
    assert result.output_json["public_url"] is None
    assert result.output_json["hosted_url"] is None
    assert result.output_json["crezlo_public_url"] == "https://ea-property-tours-20260320.crezlotours.com/tours/kahlenberg-variant-b"
    assert result.output_json["workflow_id"] is None
    assert result.output_json["requested_url"] == "crezlo://direct/workspace-crezlo-1"
    assert result.output_json["editor_url"] == "https://ea-property-tours-20260320.crezlotours.com/admin/tours/tour-crezlo-2"
    structured = result.output_json["structured_output_json"]
    assert structured["tour_id"] == "tour-crezlo-2"
    assert structured["slug"] == "kahlenberg-variant-b"
    assert structured["crezlo_public_url"] == "https://ea-property-tours-20260320.crezlotours.com/tours/kahlenberg-variant-b"
    assert structured["quality_gate_status"] == "blocked"
    assert structured["quality_gate_reason"] == "spatial_scenes_missing"
    assert structured["requested_inputs"]["scene_strategy"] == "layout_first"
    assert [entry["meta"]["role"] for entry in created_files] == ["floorplan", "photo", "photo"]
    assert [entry["path"] for entry in created_files] == [
        "https://assets.example/floorplan-1.jpg",
        "https://assets.example/photo-1.jpg",
        "https://assets.example/photo-2.jpg",
    ]
    assert created_scenes == [
        {"name": created_files[0]["name"], "order": 0, "file_id": "file-1"},
        {"name": created_files[1]["name"], "order": 1, "file_id": "file-2"},
        {"name": created_files[2]["name"], "order": 2, "file_id": "file-3"},
    ]
    assert tool_runtime.get_tool("browseract.crezlo_property_tour") is not None


def test_tool_execution_service_keeps_direct_crezlo_tour_when_followup_workflow_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="crezlo-workspace-2",
        scope_json={},
        auth_metadata_json={
            "crezlo_workspace_id": "workspace-crezlo-2",
            "crezlo_workspace_domain": "ea-property-tours-20260320.crezlotours.com",
            "crezlo_workspace_base_url": "https://ea-property-tours-20260320.crezlotours.com",
            "crezlo_property_tour_workflow_id": "wf-crezlo-followup-1",
        },
        status="enabled",
    )

    def _fake_direct_create(
        cls,
        *,
        payload: dict[str, object],
        binding_metadata: dict[str, object],
        requested_inputs: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        assert requested_inputs["tour_title"] == "Wahring Layout First"
        return {
            "tour_id": "tour-crezlo-3",
            "slug": "wahring-layout-first",
            "tour_title": "Wahring Layout First",
            "tour_status": "published",
            "public_url": "https://ea-property-tours-20260320.crezlotours.com/tours/wahring-layout-first",
            "editor_url": "https://ea-property-tours-20260320.crezlotours.com/admin/tours/tour-crezlo-3",
            "workspace_id": "workspace-crezlo-2",
            "workspace_domain": "ea-property-tours-20260320.crezlotours.com",
            "workspace_base_url": "https://ea-property-tours-20260320.crezlotours.com",
            "scene_count": 3,
            "creation_mode": "crezlo_api_remote_assets",
        }

    def _fake_run_workflow(self, *, workflow_id: str, input_values: dict[str, object]) -> dict[str, object]:
        assert workflow_id == "wf-crezlo-followup-1"
        assert input_values["editor_url"] == "https://ea-property-tours-20260320.crezlotours.com/admin/tours/tour-crezlo-3"
        return {"task_id": "task-crezlo-followup-1"}

    def _fake_wait(self, *, task_id: str, timeout_seconds: int, created_stall_seconds: int) -> dict[str, object]:
        assert task_id == "task-crezlo-followup-1"
        raise ToolExecutionError('browseract_task_failed:{"code": 5024, "message": "target element not found"}')

    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_create_crezlo_property_tour_direct",
        classmethod(_fake_direct_create),
    )
    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_run_browseract_workflow_task_with_inputs",
        _fake_run_workflow,
    )
    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_wait_for_browseract_task",
        _fake_wait,
    )
    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_publish_crezlo_public_tour_bundle",
        classmethod(lambda cls, normalized: "https://myexternalbrain.com/tours/wahring-layout-first"),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-crezlo-tour-followup-1",
            step_id="step-browseract-crezlo-tour-followup-1",
            tool_name="browseract.crezlo_property_tour",
            action_kind="property_tour.create",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "tour_title": "Wahring Layout First",
                "property_url": "https://www.willhaben.at/listing/wahring-layout-first",
                "media_urls_json": ["https://assets.example/photo-1.jpg"],
                "login_email": "the.girscheles@gmail.com",
                "login_password": "fixture-crezlo-password",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.output_json["tour_id"] == "tour-crezlo-3"
    assert result.output_json["workflow_id"] == "wf-crezlo-followup-1"
    assert result.output_json["public_url"] is None
    assert result.output_json["hosted_url"] is None
    assert result.output_json["crezlo_public_url"] == "https://ea-property-tours-20260320.crezlotours.com/tours/wahring-layout-first"
    structured = result.output_json["structured_output_json"]
    assert structured["workflow_followup_status"] == "failed"
    assert "browseract_task_failed" in structured["workflow_followup_error"]
    assert structured["direct_create_json"]["editor_url"] == "https://ea-property-tours-20260320.crezlotours.com/admin/tours/tour-crezlo-3"
    assert structured["quality_gate_status"] == "blocked"
    assert structured["quality_gate_reason"] == "spatial_scenes_missing"


def test_tool_execution_service_executes_crezlo_property_tour_via_ui_worker_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="crezlo-workspace-ui-1",
        scope_json={},
        auth_metadata_json={
            "crezlo_workspace_id": "workspace-crezlo-ui-1",
            "crezlo_workspace_domain": "ea-property-tours-20260320.crezlotours.com",
            "crezlo_workspace_base_url": "https://ea-property-tours-20260320.crezlotours.com",
        },
        status="enabled",
    )

    captured: dict[str, object] = {}

    def _fake_worker(cls, *, packet: dict[str, object], timeout_seconds: int) -> dict[str, object]:
        captured["packet"] = dict(packet)
        captured["timeout_seconds"] = timeout_seconds
        return {
            "creation_mode": "crezlo_ui_worker",
            "tour_id": "tour-crezlo-ui-1",
            "slug": "wahring-ui-worker",
            "tour_title": "Wahring UI Worker",
            "tour_status": "published",
            "editor_url": "https://ea-property-tours-20260320.crezlotours.com/admin/tours/tour-crezlo-ui-1",
            "public_url": "https://ea-property-tours-20260320.crezlotours.com/tours/wahring-ui-worker",
            "scene_count": 4,
            "scenes_response_json": {
                "data": {
                    "data": [
                        {
                            "id": "scene-1",
                            "order": 0,
                            "name": "scene-1.jpg",
                            "file": {
                                "id": "file-1",
                                "name": "scene-1.jpg",
                                "path": "tours/2026-05-03/scene-1.jpg",
                                "mime_type": "image/jpeg",
                            },
                        }
                    ]
                }
            },
        }

    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_run_crezlo_property_tour_worker",
        classmethod(_fake_worker),
    )
    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_publish_crezlo_public_tour_bundle",
        classmethod(lambda cls, normalized: "https://myexternalbrain.com/tours/wahring-ui-worker"),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-crezlo-tour-ui-1",
            step_id="step-browseract-crezlo-tour-ui-1",
            tool_name="browseract.crezlo_property_tour",
            action_kind="property_tour.create",
            payload_json={
                "binding_id": binding.binding_id,
                "principal_id": "exec-1",
                "force_ui_worker": True,
                "tour_title": "Wahring UI Worker",
                "property_url": "https://www.willhaben.at/listing/wahring-ui-worker",
                "scene_strategy": "light_and_view",
                "scene_selection_json": {"max_photos": 4},
                "media_urls_json": [
                    "https://assets.example/photo-1.jpg",
                    "https://assets.example/photo-2.jpg",
                ],
                "floorplan_urls_json": [],
                "login_email": "the.girscheles@gmail.com",
                "login_password": "fixture-crezlo-password",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    packet = dict(captured["packet"] or {})
    assert packet["workspace_id"] == "workspace-crezlo-ui-1"
    assert packet["workspace_domain"] == "ea-property-tours-20260320.crezlotours.com"
    assert "EA Property Tours" in packet["workspace_label_candidates"]
    assert packet["tour_title"] == "Wahring UI Worker"
    assert result.output_json["tour_id"] == "tour-crezlo-ui-1"
    assert result.output_json["creation_mode"] == "crezlo_ui_worker_upload"
    assert result.output_json["public_url"] is None
    assert result.output_json["hosted_url"] is None
    assert result.output_json["crezlo_public_url"] == "https://ea-property-tours-20260320.crezlotours.com/tours/wahring-ui-worker"
    structured = dict(result.output_json["structured_output_json"] or {})
    workflow_output = dict(structured.get("workflow_output_json") or {})
    assert workflow_output["file_records_json"][0]["path"] == "https://media.crezlo.com/tours/2026-05-03/scene-1.jpg"
    assert workflow_output["tour_detail_json"]["scenes"][0]["file"]["meta"]["source_url"] == "https://assets.example/photo-1.jpg"
    assert structured["quality_gate_status"] == "blocked"
    assert structured["quality_gate_reason"] == "spatial_scenes_missing"


def test_crezlo_property_tour_env_credentials_populate_inputs_and_worker_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_CREZLO_LOGIN_EMAIL", "env-crezlo@example.com")
    monkeypatch.setenv("EA_CREZLO_LOGIN_PASSWORD", "env-crezlo-password")

    payload = {
        "tour_title": "Env Credential Tour",
        "property_url": "https://www.willhaben.at/listing/env-credential-tour",
        "media_urls_json": ["https://assets.example/photo-1.jpg"],
        "floorplan_urls_json": ["https://assets.example/floorplan-1.jpg"],
        "floorplan_analysis_json": {
            "contract_name": "propertyquarry.floorplan_analysis.v2",
            "review_status": "approved",
            "room_count": 1,
            "source_geometry": {"portals": [{"id": "entrance-exit-gate"}]},
        },
    }
    binding_metadata: dict[str, object] = {}

    inputs = BrowserActToolAdapter._build_crezlo_property_tour_inputs(
        payload=payload,
        binding_metadata=binding_metadata,
    )
    assert inputs["login_email"] == "env-crezlo@example.com"
    assert inputs["crezlo_login_email"] == "env-crezlo@example.com"
    assert inputs["browseract_username"] == "env-crezlo@example.com"
    assert inputs["login_password"] == "env-crezlo-password"
    assert inputs["crezlo_login_password"] == "env-crezlo-password"
    assert inputs["browseract_password"] == "env-crezlo-password"

    packet = BrowserActToolAdapter._build_crezlo_property_tour_worker_packet(
        payload=payload,
        binding_metadata=binding_metadata,
        requested_inputs=inputs,
        workspace={
            "workspace_id": "workspace-crezlo-env-1",
            "workspace_domain": "ea-property-tours-20260320.crezlotours.com",
            "workspace_base_url": "https://ea-property-tours-20260320.crezlotours.com",
            "workspace_tours_url": "https://ea-property-tours-20260320.crezlotours.com/admin/tours",
        },
        timeout_seconds=120,
    )
    assert packet["login_email"] == "env-crezlo@example.com"
    assert packet["login_password"] == "env-crezlo-password"
    assert packet["floorplan_analysis_json"]["contract_name"] == "propertyquarry.floorplan_analysis.v2"


def test_crezlo_inspection_workflow_sends_only_declared_existing_editor_inputs() -> None:
    inputs = {
        "browseract_username": "owner@example.com",
        "browseract_password": "secret",
        "login_email": "owner@example.com",
        "tour_title": "Must not reach inspector",
        "media_urls_json": ["https://assets.example/photo.jpg"],
        "workspace_id": "workspace-1",
    }

    result = BrowserActToolAdapter._crezlo_workflow_inputs(
        workflow_id="86048166080352916",
        requested_inputs=inputs,
        binding_metadata={},
        editor_url="https://workspace.crezlotours.com/admin/tours/tour-1",
    )

    assert result == {
        "browseract_username": "owner@example.com",
        "browseract_password": "secret",
        "editor_url": "https://workspace.crezlotours.com/admin/tours/tour-1",
    }


def test_crezlo_inspection_workflow_requires_existing_editor_url() -> None:
    with pytest.raises(ToolExecutionError, match="crezlo_inspection_editor_url_missing"):
        BrowserActToolAdapter._crezlo_workflow_inputs(
            workflow_id="86048166080352916",
            requested_inputs={
                "browseract_username": "owner@example.com",
                "browseract_password": "secret",
            },
            binding_metadata={},
        )


def test_crezlo_immersive_acceptance_rejects_flat_jpeg_scenes() -> None:
    result = BrowserActToolAdapter._crezlo_immersive_acceptance(
        {
            "structured_output_json": {
                "workflow_output_json": {
                    "tour_detail_json": {
                        "scenes": [
                            {"id": "scene-1", "payload": [], "file": {"mime_type": "image/jpeg", "cube_maps": []}},
                            {"id": "scene-2", "payload": [], "file": {"mime_type": "image/jpeg", "cube_maps": []}},
                        ]
                    }
                }
            }
        }
    )

    assert result["accepted"] is False
    assert result["reason"] == "spatial_scenes_missing"
    assert result["spatial_scene_count"] == 0


def test_crezlo_immersive_acceptance_rejects_token_two_scene_demo() -> None:
    scene = {
        "file": {
            "meta": {
                "projection": "equirectangular",
                "width": 4096,
                "height": 2048,
            }
        }
    }
    result = BrowserActToolAdapter._crezlo_immersive_acceptance(
        {
            "structured_output_json": {
                "workflow_output_json": {
                    "tour_detail_json": {
                        "scenes": [
                            {"id": "scene-1", **scene},
                            {"id": "scene-2", **scene},
                        ]
                    }
                }
            }
        }
    )

    assert result["accepted"] is False
    assert result["reason"] == "spatial_scenes_missing"
    assert result["spatial_scene_count"] == 2
    assert result["required_spatial_scene_count"] == 3


def test_crezlo_immersive_acceptance_requires_spatial_and_first_party_browser_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_crezlo_public_tour_base_url",
        staticmethod(lambda: "https://propertyquarry.com/tours"),
    )
    layout_contract = {
        "contract_name": "propertyquarry.floorplan_analysis.v2",
        "review_status": "approved",
        "room_count": 3,
        "rooms": [
            {"id": "entrance-vestibule", "components": [{"x": 0, "z": 0, "width": 2, "depth": 2}]},
            {"id": "living-kitchen", "components": [{"x": 2, "z": 0, "width": 4, "depth": 3}]},
            {"id": "balcony-loggia", "components": [{"x": 6, "z": 0, "width": 2, "depth": 2}]},
        ],
        "doorway_edges": [
            ["entrance-vestibule", "living-kitchen"],
            ["living-kitchen", "balcony-loggia"],
        ],
        "source_geometry": {
            "portals": [
                {"id": "entrance-to-living", "room_ids": ["entrance-vestibule", "living-kitchen"]},
                {"id": "entrance-exit-gate", "room_ids": ["entrance-vestibule", "outside"]},
            ]
        },
        "round_trip": {"status": "pass"},
    }
    layout_hash = hashlib.sha256(
        json.dumps(layout_contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    projection_hash = str(source_geometry_projection(layout_contract)["sha256"])
    result = BrowserActToolAdapter._crezlo_immersive_acceptance(
        {
            "structured_output_json": {
                "requested_inputs": {
                    "property_url": "https://www.willhaben.at/listing/verified-property",
                    "floorplan_urls_json": ["https://assets.example/floorplan.jpg"],
                    "floorplan_analysis_json": layout_contract,
                },
                "workflow_output_json": {
                    "tour_detail_json": {
                        "scenes": [
                            {
                                "id": "scene-1",
                                "payload": {"hotspots": [{"target_scene_id": "scene-2"}]},
                                "file": {
                                    "meta": {
                                        "projection": "equirectangular",
                                        "width": 4096,
                                        "height": 2048,
                                    }
                                },
                            },
                            {
                                "id": "scene-2",
                                "payload": {"hotspots": [{"target_scene_id": "scene-3"}]},
                                "file": {
                                    "meta": {
                                        "projection": "equirectangular",
                                        "width": 4096,
                                        "height": 2048,
                                    }
                                },
                            },
                            {
                                "id": "scene-3",
                                "file": {
                                    "meta": {
                                        "projection": "equirectangular",
                                        "width": 4096,
                                        "height": 2048,
                                    }
                                },
                            },
                        ]
                    },
                    "immersive_viewer_proof": {
                        "status": "pass",
                        "required_spatial_scene_count": 3,
                        "required_space_count": 3,
                        "covered_space_count": 3,
                        "anonymous_http_status": 200,
                        "drag_look_verified": True,
                        "scene_navigation_verified": True,
                        "scene_graph_connected": True,
                        "all_required_scenes_navigable": True,
                        "desktop_viewer_verified": True,
                        "mobile_viewer_verified": True,
                        "touch_look_verified": True,
                        "captured_360_verified": True,
                        "exact_property_provenance_verified": True,
                        "source_property_url": "https://www.willhaben.at/listing/verified-property",
                        "source_asset_hashes": ["a" * 64, "b" * 64],
                        "browser_receipt_sha256": "c" * 64,
                        "floorplan_alignment_verified": True,
                        "floorplan_geometry_receipt_verified": True,
                        "floorplan_analysis_sha256": layout_hash,
                        "floorplan_analysis_contract_name": "propertyquarry.floorplan_analysis.v2",
                        "floorplan_analysis_review_status": "approved",
                        "floorplan_source_room_count": 3,
                        "floorplan_source_portal_ids": ["entrance-to-living", "entrance-exit-gate"],
                        "floorplan_geometry_projection_verified": True,
                        "floorplan_source_geometry_projection_sha256": projection_hash,
                        "first_party_viewer_verified": True,
                        "first_party_public_url": "https://propertyquarry.com/tours/verified-crezlo/control/crezlo",
                        "provider_control_route_verified": True,
                    },
                }
            }
        }
    )

    assert result["accepted"] is True
    assert result["reason"] == ""
    assert result["spatial_scene_count"] == 3
    assert result["hotspot_count"] == 2
    assert result["covered_space_count"] == 3
    assert result["floorplan_alignment_verified"] is True


def test_crezlo_public_tour_bundle_writer_downloads_assets_and_writes_tour_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://ea.example/tours")

    def _fake_download(cls, url: str) -> tuple[bytes, str]:
        return (f"asset:{url}".encode("utf-8"), "image/jpeg")

    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_crezlo_download_public_asset",
        classmethod(_fake_download),
    )

    hosted_url = BrowserActToolAdapter._publish_crezlo_public_tour_bundle(
        {
            "tour_title": "Kahlenberg Variant B",
            "tour_id": "tour-crezlo-2",
            "slug": "kahlenberg-variant-b",
            "public_url": "https://ea-property-tours-20260320.crezlotours.com/tours/kahlenberg-variant-b",
            "editor_url": "https://ea-property-tours-20260320.crezlotours.com/admin/tours/tour-crezlo-2",
            "structured_output_json": {
                "immersive_acceptance_json": _approved_crezlo_writer_acceptance(),
                "crezlo_source_provenance": _approved_crezlo_source_provenance(),
                "requested_inputs": {
                    "tour_title": "Kahlenberg Variant B",
                    "property_url": "https://www.willhaben.at/listing/kahlenberg",
                    "scene_strategy": "layout_first",
                    "display_title": "Kahlenberg Panorama",
                    "theme_name": "Cinematic Warm",
                    "tour_style": "guided walkthrough",
                    "audience": "renters",
                    "creative_brief": "Lead with the view and floorplan clarity.",
                    "call_to_action": "Book a viewing.",
                    "property_facts_json": {
                        "listing_title": "Exklusive 2 Zimmer Wohnung mit Blick auf den Kahlenberg",
                        "rooms": 2,
                        "area_sqm": 58,
                        "total_rent_eur": 897,
                        "availability": "ab sofort",
                        "address_lines": ["1200 Wien"],
                        "teaser_attributes": ["Kahlenbergblick"],
                    },
                },
                "workflow_output_json": {
                    "file_records_json": [
                        {
                            "id": "file-1",
                            "name": "floorplan.jpg",
                            "path": "https://assets.example/floorplan.jpg",
                            "mime_type": "image/jpeg",
                            "meta": {
                                "role": "floorplan",
                                "source_url": "https://assets.example/floorplan.jpg",
                                "property_url": "https://www.willhaben.at/listing/kahlenberg",
                            },
                        },
                        {
                            "id": "file-2",
                            "name": "living-room.jpg",
                            "path": "https://assets.example/living-room.jpg",
                            "mime_type": "image/jpeg",
                            "meta": {
                                "role": "photo",
                                "source_url": "https://assets.example/living-room.jpg",
                                "property_url": "https://www.willhaben.at/listing/kahlenberg",
                            },
                        },
                    ],
                },
            },
        }
    )

    assert hosted_url == "https://ea.example/tours/kahlenberg-variant-b"
    bundle_dir = tmp_path / "kahlenberg-variant-b"
    assert (bundle_dir / "scene-01.jpg").read_bytes() == b"asset:https://assets.example/floorplan.jpg"
    assert (bundle_dir / "scene-02.jpg").read_bytes() == b"asset:https://assets.example/living-room.jpg"
    payload = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert payload["hosted_url"] == "https://ea.example/tours/kahlenberg-variant-b"
    assert payload["scene_count"] == 2
    assert payload["scenes"][0]["asset_relpath"] == "scene-01.jpg"
    serialized_payload = json.dumps(payload, sort_keys=True)
    assert "listing_url" not in payload
    assert "crezlo_public_url" not in payload
    assert "brief" not in payload
    assert "willhaben.at/listing/kahlenberg" not in serialized_payload
    private_payload = json.loads((bundle_dir / "tour.private.json").read_text(encoding="utf-8"))
    assert private_payload["listing_url"] == "https://www.willhaben.at/listing/kahlenberg"
    assert private_payload["crezlo_public_url"] == "https://ea-property-tours-20260320.crezlotours.com/tours/kahlenberg-variant-b"
    assert private_payload["brief"]["creative_brief"] == "Lead with the view and floorplan clarity."


def test_crezlo_public_tour_bundle_writer_refuses_explicitly_blocked_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    hosted_url = BrowserActToolAdapter._publish_crezlo_public_tour_bundle(
        {
            "tour_title": "Blocked direct writer call",
            "structured_output_json": {
                "immersive_acceptance_json": {
                    "accepted": False,
                    "reason": "floorplan_geometry_receipt_unverified",
                },
                "workflow_output_json": {
                    "file_records_json": [
                        {"path": "https://assets.example/blocked.jpg", "mime_type": "image/jpeg"}
                    ]
                },
            },
        }
    )
    assert hosted_url == ""
    assert not list(tmp_path.iterdir())


def test_crezlo_public_tour_bundle_writer_supports_ui_worker_scene_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://ea.example/tours")

    def _fake_download(cls, url: str) -> tuple[bytes, str]:
        return (f"asset:{url}".encode("utf-8"), "image/jpeg")

    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_crezlo_download_public_asset",
        classmethod(_fake_download),
    )

    hosted_url = BrowserActToolAdapter._publish_crezlo_public_tour_bundle(
        {
            "tour_title": "Wahring UI Worker",
            "tour_id": "tour-crezlo-ui-1",
            "slug": "wahring-ui-worker",
            "public_url": "https://ea-property-tours-20260320.crezlotours.com/tours/wahring-ui-worker",
            "editor_url": "https://ea-property-tours-20260320.crezlotours.com/admin/tours/tour-crezlo-ui-1",
            "structured_output_json": {
                "immersive_acceptance_json": _approved_crezlo_writer_acceptance(),
                "crezlo_source_provenance": _approved_crezlo_source_provenance(),
                "requested_inputs": {
                    "tour_title": "Wahring UI Worker",
                    "property_url": "https://www.willhaben.at/listing/wahring-ui-worker",
                    "scene_strategy": "story_first",
                    "scene_selection_json": {"max_photos": 2, "include_floorplans": False},
                    "media_urls_json": [
                        "https://assets.example/photo-1.jpg",
                        "https://assets.example/photo-2.jpg",
                    ],
                },
                "workflow_output_json": {
                    "scenes_response_json": {
                        "data": {
                            "data": [
                                {
                                    "id": "scene-1",
                                    "order": 0,
                                    "name": "scene-1.jpg",
                                    "file": {
                                        "id": "file-1",
                                        "name": "scene-1.jpg",
                                        "path": "tours/2026-05-03/scene-1.jpg",
                                        "mime_type": "image/jpeg",
                                    },
                                },
                                {
                                    "id": "scene-2",
                                    "order": 1,
                                    "name": "scene-2.jpg",
                                    "file": {
                                        "id": "file-2",
                                        "name": "scene-2.jpg",
                                        "path": "tours/2026-05-03/scene-2.jpg",
                                        "mime_type": "image/jpeg",
                                    },
                                },
                            ]
                        }
                    }
                },
            },
        }
    )

    assert hosted_url == "https://ea.example/tours/wahring-ui-worker"
    bundle_dir = tmp_path / "wahring-ui-worker"
    assert (bundle_dir / "scene-01.jpg").read_bytes() == b"asset:https://media.crezlo.com/tours/2026-05-03/scene-1.jpg"
    payload = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert payload["hosted_url"] == "https://ea.example/tours/wahring-ui-worker"
    assert payload["scene_count"] == 2
    assert payload["brand_name"] == "Pioche Lecombe"
    assert "listing_url" not in payload
    assert "crezlo_public_url" not in payload
    private_payload = json.loads((bundle_dir / "tour.private.json").read_text(encoding="utf-8"))
    assert private_payload["listing_url"] == "https://www.willhaben.at/listing/wahring-ui-worker"
    assert private_payload["crezlo_public_url"] == "https://ea-property-tours-20260320.crezlotours.com/tours/wahring-ui-worker"


def test_crezlo_public_tour_bundle_writer_rejects_requested_media_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://ea.example/tours")

    def _fake_download(cls, url: str) -> tuple[bytes, str]:
        return (f"asset:{url}".encode("utf-8"), "image/jpeg")

    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_crezlo_download_public_asset",
        classmethod(_fake_download),
    )

    hosted_url = BrowserActToolAdapter._publish_crezlo_public_tour_bundle(
        {
            "tour_title": "Fallback Media Tour",
            "tour_id": "tour-crezlo-fallback-1",
            "slug": "fallback-media-tour",
            "public_url": "https://ea-property-tours-20260320.crezlotours.com/tours/fallback-media-tour",
            "editor_url": "https://ea-property-tours-20260320.crezlotours.com/admin/tours/tour-crezlo-fallback-1",
            "structured_output_json": {
                "requested_inputs": {
                    "tour_title": "Fallback Media Tour",
                    "property_url": "https://www.willhaben.at/listing/fallback-media-tour",
                    "scene_strategy": "layout_first",
                    "scene_selection_json": {"include_floorplans": True},
                    "media_urls_json": [
                        "https://assets.example/fallback-photo-1.jpg",
                        "https://assets.example/fallback-photo-2.jpg",
                    ],
                    "floorplan_urls_json": ["https://assets.example/fallback-floorplan-1.jpg"],
                    "source_virtual_tour_url": "https://360.example.test/view/portal/id/demo-tour",
                    "panorama_source": "feelestate_kalandra",
                    "brand_name": "Pioche Lecombe",
                    "property_facts_json": {
                        "listing_title": "Fallback Media Tour Listing",
                    },
                },
                "workflow_output_json": {
                    "tour_detail_json": {},
                    "file_records_json": [],
                },
            },
        }
    )

    assert hosted_url == ""
    assert not list(tmp_path.iterdir())


def test_crezlo_public_tour_base_url_defaults_to_propertyquarry_public_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EA_PUBLIC_TOUR_BASE_URL", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_PUBLIC_TOUR_BASE_URL", raising=False)
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_BASE_URL", "https://propertyquarry.com")
    monkeypatch.setenv("EA_PUBLIC_APP_BASE_URL", "https://propertyquarry.com")

    assert BrowserActToolAdapter._crezlo_public_tour_base_url() == "https://propertyquarry.com/tours"


def test_crezlo_public_tour_bundle_writer_replaces_stale_bundle_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://ea.example/tours")

    slug = "atomic-bundle-tour"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01.jpg").write_bytes(b"old-scene-1")
    (bundle_dir / "scene-02.jpg").write_bytes(b"old-scene-2")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "scenes": [
                    {"asset_relpath": "scene-01.jpg"},
                    {"asset_relpath": "scene-02.jpg"},
                ],
            }
        ),
        encoding="utf-8",
    )

    def _fake_download(cls, url: str) -> tuple[bytes, str]:
        return (f"asset:{url}".encode("utf-8"), "image/jpeg")

    monkeypatch.setattr(
        BrowserActToolAdapter,
        "_crezlo_download_public_asset",
        classmethod(_fake_download),
    )

    hosted_url = BrowserActToolAdapter._publish_crezlo_public_tour_bundle(
        {
            "tour_title": "Atomic Bundle Tour",
            "slug": slug,
            "structured_output_json": {
                "immersive_acceptance_json": _approved_crezlo_writer_acceptance(),
                "crezlo_source_provenance": _approved_crezlo_source_provenance(),
                "requested_inputs": {
                    "tour_title": "Atomic Bundle Tour",
                    "property_url": "https://www.willhaben.at/listing/atomic-bundle-tour",
                    "media_urls_json": ["https://assets.example/new-scene-1.jpg"],
                }
            },
        }
    )

    assert hosted_url == f"https://ea.example/tours/{slug}"
    assert (bundle_dir / "scene-01.jpg").read_bytes() == b"asset:https://assets.example/new-scene-1.jpg"
    assert not (bundle_dir / "scene-02.jpg").exists()
    assert not list(tmp_path.glob(f".{slug}.tmp-*"))
    assert not list(tmp_path.glob(f".{slug}.bak-*"))


def test_crezlo_worker_script_path_resolves_existing_worker() -> None:
    path = BrowserActToolAdapter._crezlo_worker_script_path()
    assert path.name == "crezlo_property_tour_worker.py"
    assert path.exists()


def test_crezlo_worker_uses_live_workspace_api_and_normalizes_bare_fqdn() -> None:
    source = BrowserActToolAdapter._crezlo_worker_script_path().read_text(encoding="utf-8")

    assert "https://api.caliqik.com/api/seller/tours/workspaces" in source
    assert "https://tours.crezlo.com/api/seller/tours/workspaces" not in source
    assert "rawWorkspaceDomain && !rawWorkspaceDomain.includes('.')" in source
    assert "`${rawWorkspaceDomain}.crezlotours.com`" in source


@pytest.mark.parametrize(
    ("service_key", "worker_name"),
    [
        ("onemin_billing_usage", "browseract_template_service_worker.py"),
        ("onemin_member_reconciliation", "browseract_template_service_worker.py"),
    ],
)
def test_ui_service_worker_script_path_resolves_onemin_builtin_workers(
    service_key: str,
    worker_name: str,
) -> None:
    path = BrowserActToolAdapter._ui_service_worker_script_path(service_key)
    assert path.name == worker_name
    assert path.exists()


def test_tool_execution_service_self_heals_missing_builtin_onemin_code_generate_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEMIN_AI_API_KEY", "onemin-code-key")
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    def _fake_call_text(self, *, prompt: str, model: str, lane: str, principal_id: str = ""):
        assert "Implement a safe parser" in prompt
        assert lane == "hard"
        assert principal_id == "exec-1"
        return UpstreamResult(
            text='{"patch":"safe parser","notes":["bounded change"]}',
            provider_key="onemin",
            model=model,
            provider_key_slot="primary",
            provider_backend="1min",
            provider_account_name="ONEMIN_AI_API_KEY",
            tokens_in=111,
            tokens_out=37,
        )

    monkeypatch.setattr(
        "app.services.tool_execution_onemin_adapter.OneminToolAdapter._call_text",
        _fake_call_text,
    )
    registry._rows.pop("provider.onemin.code_generate", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "provider.onemin.code_generate"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-onemin-code-1",
            step_id="step-onemin-code-1",
            tool_name="provider.onemin.code_generate",
            action_kind="code.generate",
            payload_json={
                "prompt": "Implement a safe parser for the billing payload.",
                "instructions": "Return a compact patch summary.",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "provider.onemin.code_generate"
    assert result.tokens_in == 111
    assert result.tokens_out == 37
    assert result.output_json["mime_type"] == "application/json"
    assert result.output_json["structured_output_json"]["patch"] == "safe parser"
    assert result.output_json["provider_backend"] == "1min"
    assert result.receipt_json["provider_key"] == "onemin"
    assert tool_runtime.get_tool("provider.onemin.code_generate") is not None


def test_tool_execution_service_self_heals_missing_builtin_magixai_structured_generate_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_MAGICX_API_KEY", "magicx-key")
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    def _fake_call_text(self, *, prompt: str, model: str, lane: str):
        assert "Summarize the fleet status" in prompt
        assert lane == "easy"
        return UpstreamResult(
            text='{"summary":"fleet stable","risk":"low"}',
            provider_key="magixai",
            model=model,
            provider_key_slot="primary",
            provider_backend="aimagicx",
            provider_account_name="AI_MAGICX_API_KEY",
            tokens_in=23,
            tokens_out=11,
        )

    monkeypatch.setattr(
        "app.services.tool_execution_magixai_adapter.MagixaiToolAdapter._call_text",
        _fake_call_text,
    )
    registry._rows.pop("provider.magixai.structured_generate", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "provider.magixai.structured_generate"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-magix-1",
            step_id="step-magix-1",
            tool_name="provider.magixai.structured_generate",
            action_kind="content.generate",
            payload_json={
                "prompt": "Summarize the fleet status.",
                "generation_instruction": "Return JSON.",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "provider.magixai.structured_generate"
    assert result.tokens_in == 23
    assert result.tokens_out == 11
    assert result.output_json["structured_output_json"]["summary"] == "fleet stable"
    assert result.output_json["provider_backend"] == "aimagicx"
    assert result.receipt_json["provider_key"] == "magixai"
    assert tool_runtime.get_tool("provider.magixai.structured_generate") is not None


def test_tool_execution_service_self_heals_missing_builtin_onemin_image_generate_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEMIN_AI_API_KEY", "onemin-image-key")
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    def _fake_call_feature(
        self,
        *,
        feature_payload: dict[str, object],
        lane: str,
        capability: str,
        principal_id: str = "",
        allow_reserve: bool = False,
    ):
        assert feature_payload["type"] == "IMAGE_GENERATOR"
        assert lane == "hard"
        assert capability == "image_generate"
        assert principal_id == "exec-1"
        assert allow_reserve is False
        return (
            {
                "aiRecord": {
                    "aiRecordDetail": {
                        "resultObject": {
                            "url": "https://cdn.1min.ai/generated/test-image.png",
                        }
                    }
                }
            },
            "ONEMIN_AI_API_KEY",
            "primary",
            str(feature_payload.get("model") or "gpt-image-1-mini"),
            0,
            0,
        )

    monkeypatch.setattr(
        "app.services.tool_execution_onemin_adapter.OneminToolAdapter._call_feature",
        _fake_call_feature,
    )
    registry._rows.pop("provider.onemin.image_generate", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "provider.onemin.image_generate"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-onemin-image-1",
            step_id="step-onemin-image-1",
            tool_name="provider.onemin.image_generate",
            action_kind="image.generate",
            payload_json={
                "prompt": "Render a neon-lit operator dashboard banner.",
                "size": "1024x1024",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "provider.onemin.image_generate"
    assert result.output_json["asset_urls"] == ["https://cdn.1min.ai/generated/test-image.png"]
    assert result.output_json["provider_backend"] == "1min"
    assert result.receipt_json["feature_type"] == "IMAGE_GENERATOR"
    assert tool_runtime.get_tool("provider.onemin.image_generate") is not None


def test_tool_execution_service_self_heals_missing_builtin_onemin_property_walkthrough_video_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEMIN_AI_API_KEY", "onemin-video-key")
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    def _fake_call_property_walkthrough_feature(
        self,
        *,
        first_frame_path: str,
        image_url: str,
        feature_model: str,
        prompt_object: dict[str, object],
        principal_id: str,
        allow_reserve: bool,
        timeout_seconds: int,
    ):
        assert image_url == "https://cdn.example/first-frame.jpg"
        assert feature_model == "pika"
        assert prompt_object["imageUrl"] == "https://cdn.example/first-frame.jpg"
        assert principal_id == "exec-1"
        assert allow_reserve is True
        assert timeout_seconds == 45
        return (
            {
                "aiRecord": {
                    "aiRecordDetail": {
                        "resultObject": {
                            "url": "https://cdn.1min.ai/generated/walkthrough.mp4",
                        }
                    }
                }
            },
            "Ma01",
            "ONEMIN_AI_API_KEY_FALLBACK_1",
            feature_model,
            0,
            0,
        )

    monkeypatch.setattr(
        "app.services.tool_execution_onemin_adapter.OneminToolAdapter._call_property_walkthrough_feature",
        _fake_call_property_walkthrough_feature,
    )
    registry._rows.pop("provider.onemin.property_walkthrough_video", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "provider.onemin.property_walkthrough_video"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-onemin-property-video-1",
            step_id="step-onemin-property-video-1",
            tool_name="provider.onemin.property_walkthrough_video",
            action_kind="video.generate",
            payload_json={
                "prompt": "One continuous photorealistic walkthrough, no cuts.",
                "image_url": "https://cdn.example/first-frame.jpg",
                "model_order": ["pika"],
                "duration": 5,
                "allow_reserve": True,
                "timeout_seconds": 45,
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "provider.onemin.property_walkthrough_video"
    assert result.action_kind == "video.generate"
    assert result.output_json["video_url"] == "https://cdn.1min.ai/generated/walkthrough.mp4"
    assert result.output_json["asset_url"] == "https://cdn.1min.ai/generated/walkthrough.mp4"
    assert result.output_json["provider_backend"] == "1min"
    assert result.receipt_json["feature_type"] == "IMAGE_TO_VIDEO"
    assert result.receipt_json["provider_key_slot"] == "ONEMIN_AI_API_KEY_FALLBACK_1"
    assert tool_runtime.get_tool("provider.onemin.property_walkthrough_video") is not None


def test_tool_execution_service_self_heals_missing_builtin_scene_video_generate_definition_and_normalizes_contract_provider_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    captured_render_kwargs: dict[str, object] = {}

    def _fake_render_property_flythrough_into_hosted_tour(**kwargs):
        captured_render_kwargs.update(kwargs)
        return {
            "status": "rendered",
            "provider_key": "omagic",
            "media_route_provider_key": "omagic",
            "editor_url": "https://editor.example/scene-video",
            "reason": "",
        }

    monkeypatch.setattr(
        "app.product.service._render_property_flythrough_into_hosted_tour",
        _fake_render_property_flythrough_into_hosted_tour,
    )
    monkeypatch.setattr(
        "app.product.service._hosted_property_tour_video_delivery",
        lambda tour_url: {
            "video_url": "https://cdn.example/property/walkthrough.mp4",
            "flythrough_url": "https://viewer.example/property/walkthrough",
            "provider_key": "omagic",
        },
    )
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "omagic",
            "provider_backend_key": "omagic",
            "ready": True,
            "status": "ready",
            "blockers": [],
            "checks": {},
        },
    )
    monkeypatch.setattr(
        "app.services.scene_video_contract.resolve_property_walkthrough_runtime_provider",
        lambda provider_key: {
            "provider_key": "omagic",
            "provider_backend_key": "omagic",
            "runtime_readiness_json": {
                "provider_key": "omagic",
                "provider_backend_key": "omagic",
                "ready": True,
                "status": "ready",
                "blockers": [],
                "checks": {},
            },
            "checked": [],
            "selected_via": "explicit",
        },
    )
    registry._rows.pop("ea.scene_video_generate", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "ea.scene_video_generate"]  # type: ignore[attr-defined]
    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-1",
            step_id="step-scene-video-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "magic",
                "context_kind": "property_walkthrough",
                "title": "Runsite Walkthrough",
                "tour_url": "https://property.example/tours/runsite",
                "tour_context_json": {
                    "verified_provider": "3dvista",
                    "control_url": "https://property.example/tours/runsite/control/3dvista",
                },
                "external_processing_consent_receipt": "opaque-server-issued-receipt",
                "property_facts_json": {"room_count": 4},
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "ea.scene_video_generate"
    assert result.action_kind == "video.generate"
    assert result.output_json["provider_key"] == "omagic"
    assert result.output_json["provider_backend_key"] == "omagic"
    assert result.output_json["video_url"] == "https://cdn.example/property/walkthrough.mp4"
    assert captured_render_kwargs["tour_context_json"] == {
        "verified_provider": "3dvista",
        "control_url": "https://property.example/tours/runsite/control/3dvista",
    }
    assert captured_render_kwargs["external_processing_consent_receipt"] == ""
    structured = result.output_json["structured_output_json"]
    assert structured["provider_key"] == "omagic"
    assert structured["provider_backend_key"] == "omagic"
    assert structured["structured_output_json"]["provider_backend_key"] == "omagic"
    assert structured["tour_context_json"]["verified_provider"] == "3dvista"
    assert result.receipt_json["provider_key"] == "omagic"
    assert result.receipt_json["provider_backend_key"] == "omagic"
    assert result.receipt_json["tour_context_present"] is True
    assert "external_processing_consent_granted" not in json.dumps(result.output_json)
    assert "external_processing_consent_granted" not in json.dumps(result.receipt_json)
    definition = tool_runtime.get_tool("ea.scene_video_generate")
    assert definition is not None
    assert "external_processing_consent_receipt" not in definition.input_schema_json["properties"]
    assert "external_processing_consent_granted" not in definition.input_schema_json["properties"]
    assert "external_processing_consent_json" not in definition.input_schema_json["properties"]

    captured_render_kwargs.clear()
    service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-untrusted-boolean",
            step_id="step-scene-video-untrusted-boolean",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "magic",
                "context_kind": "property_walkthrough",
                "title": "Untrusted Boolean Walkthrough",
                "tour_url": "https://property.example/tours/runsite",
                "external_processing_consent_granted": True,
            },
            context_json={"principal_id": "exec-1"},
        )
    )
    assert captured_render_kwargs["external_processing_consent_receipt"] == ""
    assert "external_processing_consent_granted" not in captured_render_kwargs
    with pytest.raises(
        ToolExecutionError,
        match="governed_render_authenticated_principal_context_missing",
    ):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-scene-video-payload-principal",
                step_id="step-scene-video-payload-principal",
                tool_name="ea.scene_video_generate",
                action_kind="video.generate",
                payload_json={
                    "provider_key": "magic",
                    "context_kind": "property_walkthrough",
                    "title": "Payload Principal Walkthrough",
                    "tour_url": "https://property.example/tours/runsite",
                    "principal_id": "claimed-owner",
                    "external_processing_consent_receipt": "leaked-receipt",
                },
                context_json={},
            )
        )


def test_tool_execution_property_walkthrough_blocks_before_render_when_governed_lane_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    monkeypatch.setattr(
        "app.product.service._render_property_flythrough_into_hosted_tour",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("blocked walkthrough should not render")),
    )
    monkeypatch.setattr("app.product.service._hosted_property_tour_video_delivery", lambda tour_url: {})
    monkeypatch.setattr(
        "app.services.scene_video_contract.resolve_property_walkthrough_runtime_provider",
        lambda provider_key: {
            "provider_key": "omagic",
            "provider_backend_key": "omagic",
            "runtime_readiness_json": {
                "provider_key": "omagic",
                "provider_backend_key": "omagic",
                "execution_lane": "ea_governed_render",
                "ready": False,
                "status": "blocked",
                "blockers": ["governed_render_endpoint_missing"],
                "checks": {"endpoint_configured": False},
            },
            "checked": [],
            "selected_via": "governed_render_explicit",
        },
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-property-walkthrough-blocked-1",
            step_id="step-property-walkthrough-blocked-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "magic",
                "context_kind": "property_walkthrough",
                "title": "Blocked Walkthrough",
                "tour_url": "https://property.example/tours/blocked",
                "tour_context_json": {"verified_provider": "3dvista"},
                "telegram_delivery_requested": True,
            },
            context_json={"principal_id": "exec-property-walkthrough-blocked"},
        )
    )

    assert result.output_json["provider_key"] == "omagic"
    assert result.output_json["render_status"] == "blocked"
    assert result.output_json["runtime_readiness_json"]["execution_lane"] == "ea_governed_render"
    assert result.output_json["runtime_readiness_json"]["checks"]["endpoint_configured"] is False
    assert result.output_json["telegram_delivery_readiness_json"]["status"] == "blocked"
    assert result.output_json["tour_context_json"]["verified_provider"] == "3dvista"


def test_tool_execution_property_walkthrough_returns_cached_video_when_provider_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    monkeypatch.setattr(
        "app.product.service._render_property_flythrough_into_hosted_tour",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("cached walkthrough should not render")),
    )
    monkeypatch.setattr(
        "app.product.service._hosted_property_tour_video_delivery",
        lambda tour_url: {
            "video_url": "https://cdn.example/property/cached-walkthrough.mp4",
            "flythrough_url": "https://viewer.example/property/cached-walkthrough",
            "provider_key": "magicfit",
        },
    )
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "omagic",
            "provider_backend_key": "omagic",
            "ready": False,
            "status": "blocked",
            "blockers": ["omagic_model_upload_adapter_missing"],
            "checks": {"model_upload_supported": False},
        },
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-property-walkthrough-cached-1",
            step_id="step-property-walkthrough-cached-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "magic",
                "context_kind": "property_walkthrough",
                "title": "Cached Walkthrough",
                "tour_url": "https://property.example/tours/cached",
                "tour_context_json": {"verified_provider": "3dvista"},
            },
            context_json={"principal_id": "exec-property-walkthrough-cached"},
        )
    )

    assert result.output_json["provider_key"] == "magicfit"
    assert result.output_json["render_status"] == "completed"
    assert result.output_json["video_url"] == "https://cdn.example/property/cached-walkthrough.mp4"
    assert result.receipt_json["cached_video_used"] is True
    assert result.receipt_json["runtime_readiness_json"]["status"] == "blocked"


def test_tool_execution_property_walkthrough_blank_provider_auto_selects_runtime_ready_magicfit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    captured_render_kwargs: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.scene_video_contract.resolve_property_walkthrough_runtime_provider",
        lambda value, allow_non_final_fallback=True: {
            "provider_key": "magicfit",
            "provider_backend_key": "magicfit",
            "runtime_readiness_json": {
                "provider_key": "magicfit",
                "provider_backend_key": "magicfit",
                "ready": True,
                "status": "ready",
                "blockers": [],
                "checks": {},
            },
            "checked": [
                {"provider_key": "omagic", "ready": False, "status": "blocked", "blockers": ["omagic_model_upload_adapter_missing"]},
                {"provider_key": "magicfit", "ready": True, "status": "ready", "blockers": []},
            ],
            "selected_via": "auto_final_ready",
            "explicit_requested": False,
        },
    )
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "magicfit",
            "provider_backend_key": "magicfit",
            "ready": True,
            "status": "ready",
            "blockers": [],
            "checks": {},
        },
    )
    monkeypatch.setattr(
        "app.product.service._render_property_flythrough_into_hosted_tour",
        lambda **kwargs: captured_render_kwargs.update(kwargs)
        or {
            "status": "rendered",
            "provider_key": "magicfit",
            "media_route_provider_key": "magicfit",
            "video_url": "https://cdn.example/property/runtime-selected.mp4",
            "flythrough_url": "https://viewer.example/property/runtime-selected",
            "reason": "",
        },
    )
    monkeypatch.setattr(
        "app.product.service._hosted_property_tour_video_delivery",
        lambda tour_url: {
            "video_url": "https://cdn.example/property/runtime-selected.mp4",
            "flythrough_url": "https://viewer.example/property/runtime-selected",
            "provider_key": "magicfit",
        },
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-property-walkthrough-default-1",
            step_id="step-property-walkthrough-default-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "context_kind": "property_walkthrough",
                "title": "Runtime-selected Walkthrough",
                "tour_url": "https://property.example/tours/runtime-selected",
                "tour_context_json": {"verified_provider": "3dvista"},
            },
            context_json={"principal_id": "exec-property-walkthrough-default"},
        )
    )

    assert captured_render_kwargs["preferred_provider_key"] == "magicfit"
    assert result.output_json["provider_key"] == "magicfit"
    assert result.output_json["video_url"] == "https://cdn.example/property/runtime-selected.mp4"
    assert result.receipt_json["runtime_provider_resolution_json"]["selected_via"] == "auto_final_ready"


def test_tool_execution_property_walkthrough_blank_provider_can_publish_with_runtime_selected_onemin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    captured_render_kwargs: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.scene_video_contract.resolve_property_walkthrough_runtime_provider",
        lambda value, allow_non_final_fallback=False: {
            "provider_key": "onemin_i2v",
            "provider_backend_key": "onemin_i2v",
            "runtime_readiness_json": {
                "provider_key": "onemin_i2v",
                "provider_backend_key": "onemin_i2v",
                "ready": True,
                "status": "ready",
                "blockers": [],
                "checks": {},
            },
            "checked": [
                {"provider_key": "omagic", "ready": False, "status": "blocked", "blockers": ["omagic_model_upload_adapter_missing"]},
                {"provider_key": "magicfit", "ready": False, "status": "blocked", "blockers": ["magicfit_insufficient_credits"]},
                {"provider_key": "onemin_i2v", "ready": True, "status": "ready", "blockers": []},
            ],
            "selected_via": "auto_final_ready",
            "explicit_requested": False,
        },
    )
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "onemin_i2v",
            "provider_backend_key": "onemin_i2v",
            "ready": True,
            "status": "ready",
            "blockers": [],
            "checks": {},
        },
    )
    monkeypatch.setattr(
        "app.product.service._render_property_flythrough_into_hosted_tour",
        lambda **kwargs: captured_render_kwargs.update(kwargs)
        or {
            "status": "rendered",
            "provider_key": "onemin_i2v",
            "media_route_provider_key": "onemin_i2v",
            "video_url": "https://cdn.example/property/runtime-selected-onemin.mp4",
            "flythrough_url": "https://viewer.example/property/runtime-selected-onemin",
            "reason": "",
        },
    )
    monkeypatch.setattr(
        "app.product.service._hosted_property_tour_video_delivery",
        lambda tour_url: {
            "video_url": "https://cdn.example/property/runtime-selected-onemin.mp4",
            "flythrough_url": "https://viewer.example/property/runtime-selected-onemin",
            "provider_key": "onemin_i2v",
        },
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-property-walkthrough-default-onemin-1",
            step_id="step-property-walkthrough-default-onemin-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "context_kind": "property_walkthrough",
                "title": "Runtime-selected Onemin Walkthrough",
                "tour_url": "https://property.example/tours/runtime-selected-onemin",
                "tour_context_json": {"verified_provider": "3dvista"},
            },
            context_json={"principal_id": "exec-property-walkthrough-default-onemin"},
        )
    )

    assert captured_render_kwargs["preferred_provider_key"] == "onemin_i2v"
    assert result.output_json["provider_key"] == "onemin_i2v"
    assert result.output_json["video_url"] == "https://cdn.example/property/runtime-selected-onemin.mp4"


def test_tool_execution_scene_video_forwards_onemin_model_order_to_delegate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    provider_registry = ProviderRegistryService()

    def _route_tool_with_context(tool_name: str, *, principal_id: str | None = None) -> CapabilityRoute:
        if tool_name == "ea.scene_video_generate":
            return CapabilityRoute(
                provider_key="ea",
                capability_key="scene_video_generate",
                tool_name="ea.scene_video_generate",
                executable=True,
            )
        assert tool_name == "provider.onemin.property_walkthrough_video"
        assert principal_id == "exec-scene-video-model-order"
        return CapabilityRoute(
            provider_key="onemin",
            capability_key="property_walkthrough_video",
            tool_name="provider.onemin.property_walkthrough_video",
            executable=True,
        )

    monkeypatch.setattr(provider_registry, "route_tool_with_context", _route_tool_with_context)
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "onemin_i2v",
            "provider_backend_key": "onemin_i2v",
            "ready": True,
            "status": "ready",
            "blockers": [],
            "checks": {},
        },
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        provider_registry=provider_registry,
    )
    captured_payload: dict[str, object] = {}

    def _fake_onemin_scene_video(request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        captured_payload.update(dict(request.payload_json or {}))
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind="video.generate",
            target_ref="https://cdn.example/runsite-scene.mp4",
            output_json={
                "video_url": "https://cdn.example/runsite-scene.mp4",
                "asset_url": "https://cdn.example/runsite-scene.mp4",
                "structured_output_json": {"video_url": "https://cdn.example/runsite-scene.mp4"},
            },
            receipt_json={"handler_key": definition.tool_name},
        )

    service.register_handler("provider.onemin.property_walkthrough_video", _fake_onemin_scene_video)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-model-order-1",
            step_id="step-scene-video-model-order-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "onemin_i2v",
                "context_kind": "scene_briefing",
                "title": "Runsite fight scene",
                "script_text": "A tactical runsite briefing push-in.",
                "first_frame_path": str(tmp_path / "first-frame.png"),
                "model_order": ["skyreels"],
                "duration_seconds": 4,
                "timeout_seconds": 120,
            },
            context_json={"principal_id": "exec-scene-video-model-order"},
        )
    )

    assert result.output_json["provider_key"] == "onemin_i2v"
    assert captured_payload["model_order"] == ["skyreels"]
    assert captured_payload["duration"] == 4
    assert captured_payload["timeout_seconds"] == 120


def test_tool_execution_scene_video_sends_successful_video_to_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    tool_runtime.upsert_connector_binding(
        principal_id="exec-scene-video-telegram",
        connector_name="telegram_identity",
        external_account_ref="42",
        auth_metadata_json={"default_chat_ref": "42", "bot_key": "default", "bot_handle": "operator_concierge_bot"},
        scope_json={"assistant_surfaces": ["dm"]},
        status="enabled",
    )
    provider_registry = ProviderRegistryService()

    def _route_tool_with_context(tool_name: str, *, principal_id: str | None = None) -> CapabilityRoute:
        if tool_name == "ea.scene_video_generate":
            return CapabilityRoute(
                provider_key="ea",
                capability_key="scene_video_generate",
                tool_name="ea.scene_video_generate",
                executable=True,
            )
        assert tool_name == "provider.onemin.property_walkthrough_video"
        assert principal_id == "exec-scene-video-telegram"
        return CapabilityRoute(
            provider_key="onemin",
            capability_key="property_walkthrough_video",
            tool_name="provider.onemin.property_walkthrough_video",
            executable=True,
        )

    monkeypatch.setattr(provider_registry, "route_tool_with_context", _route_tool_with_context)
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "onemin_i2v",
            "provider_backend_key": "onemin_i2v",
            "ready": True,
            "status": "ready",
            "blockers": [],
            "checks": {},
        },
    )
    monkeypatch.setenv(
        "EA_TELEGRAM_BOT_REGISTRY_JSON",
        json.dumps({"default": {"token": "telegram-token", "handle": "operator_concierge_bot"}}),
    )
    monkeypatch.setattr("app.services.telegram_delivery._telegram_video_has_audio", lambda value: True)
    monkeypatch.setattr("app.services.telegram_delivery._telegram_remote_ref_reachable", lambda value: True)
    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 177}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        sent.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return _FakeResponse()

    monkeypatch.setattr("app.services.telegram_delivery.urllib.request.urlopen", _fake_urlopen)
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        provider_registry=provider_registry,
    )

    def _fake_onemin_scene_video(request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        encoded_video = json.dumps(
            {
                "video_url": "https://cdn.example/runsite-scene.mp4",
                "model": "pika",
                "provider_account_name": "ONEMIN_AI_API_KEY_FALLBACK_35",
            }
        )
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind="video.generate",
            target_ref=encoded_video,
            output_json={
                "video_url": encoded_video,
                "asset_url": encoded_video,
                "structured_output_json": {"video_url": encoded_video},
            },
            receipt_json={"handler_key": definition.tool_name},
        )

    service.register_handler("provider.onemin.property_walkthrough_video", _fake_onemin_scene_video)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-telegram-1",
            step_id="step-scene-video-telegram-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "onemin_i2v",
                "context_kind": "scene_briefing",
                "title": "Runsite Telegram scene",
                "script_text": "A tactical runsite briefing push-in.",
                "first_frame_path": "/tmp/first-frame.png",
                "model_order": ["skyreels"],
            },
            context_json={"principal_id": "exec-scene-video-telegram"},
        )
    )

    assert sent and sent[0]["url"] == "https://api.telegram.org/bottelegram-token/sendVideo"
    assert sent[0]["payload"]["video"] == "https://cdn.example/runsite-scene.mp4"
    assert sent[0]["payload"]["caption"] == "Runsite Telegram scene"
    assert result.output_json["provider_key"] == "onemin_i2v"
    assert result.output_json["video_url"] == "https://cdn.example/runsite-scene.mp4"
    assert result.output_json["telegram_delivery_json"]["status"] == "sent"
    assert result.output_json["telegram_delivery_json"]["message_ids"] == ["177"]
    assert result.receipt_json["telegram_delivery_json"]["media_ref"] == "https://cdn.example/runsite-scene.mp4"


def test_tool_execution_scene_video_delivery_probe_sends_existing_video_without_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    tool_runtime.upsert_connector_binding(
        principal_id="exec-scene-video-probe",
        connector_name="telegram_identity",
        external_account_ref="42",
        auth_metadata_json={"default_chat_ref": "42", "bot_key": "default", "bot_handle": "operator_concierge_bot"},
        scope_json={"assistant_surfaces": ["dm"]},
        status="enabled",
    )
    provider_registry = ProviderRegistryService()
    delegate_routes: list[str] = []

    def _route_tool_with_context(tool_name: str, *, principal_id: str | None = None) -> CapabilityRoute:
        if tool_name == "ea.scene_video_generate":
            return CapabilityRoute(
                provider_key="ea",
                capability_key="scene_video_generate",
                tool_name="ea.scene_video_generate",
                executable=True,
            )
        delegate_routes.append(tool_name)
        raise AssertionError(f"unexpected delegate route: {tool_name}")

    monkeypatch.setattr(provider_registry, "route_tool_with_context", _route_tool_with_context)
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "omagic",
            "provider_backend_key": "omagic",
            "ready": True,
            "status": "ready",
            "blockers": [],
            "checks": {},
        },
    )
    monkeypatch.setenv(
        "EA_TELEGRAM_BOT_REGISTRY_JSON",
        json.dumps({"default": {"token": "telegram-token", "handle": "operator_concierge_bot"}}),
    )
    monkeypatch.setattr("app.services.telegram_delivery._telegram_video_has_audio", lambda value: True)
    monkeypatch.setattr("app.services.telegram_delivery._telegram_remote_ref_reachable", lambda value: True)
    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 188}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        sent.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return _FakeResponse()

    monkeypatch.setattr("app.services.telegram_delivery.urllib.request.urlopen", _fake_urlopen)
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        provider_registry=provider_registry,
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-probe-1",
            step_id="step-scene-video-probe-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "magic",
                "context_kind": "scene_briefing",
                "title": "Runsite Telegram probe",
                "delivery_probe_video_url": "https://cdn.example/runsite-probe.mp4",
                "telegram_delivery_requested": True,
            },
            context_json={"principal_id": "exec-scene-video-probe"},
        )
    )

    assert delegate_routes == []
    assert sent and sent[0]["url"] == "https://api.telegram.org/bottelegram-token/sendVideo"
    assert sent[0]["payload"]["video"] == "https://cdn.example/runsite-probe.mp4"
    assert sent[0]["payload"]["caption"] == "Runsite Telegram probe"
    assert result.output_json["provider_key"] == "omagic"
    assert result.output_json["render_status"] == "completed"
    assert result.output_json["telegram_delivery_json"]["status"] == "sent"
    assert result.receipt_json["delivery_probe"] is True
    assert result.receipt_json["telegram_delivery_json"]["media_ref"] == "https://cdn.example/runsite-probe.mp4"


def test_tool_execution_scene_video_mootion_remote_browseract_bypasses_local_docker_blockers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_mootion_public_dns(monkeypatch)
    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS", "cdn.example")
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-scene-video-mootion-remote",
        connector_name="browseract",
        external_account_ref="mootion-scene-video-remote",
        scope_json={"services": ["mootion", "mootion_movie"]},
        auth_metadata_json={
            "mootion_browseract_bridge": True,
            "service_key": "mootion_movie",
            "mootion_movie_workflow_id": "wf-mootion-remote",
        },
        status="enabled",
    )
    provider_registry = ProviderRegistryService()

    def _route_tool_with_context(tool_name: str, *, principal_id: str | None = None) -> CapabilityRoute:
        if tool_name == "ea.scene_video_generate":
            return CapabilityRoute(
                provider_key="ea",
                capability_key="scene_video_generate",
                tool_name="ea.scene_video_generate",
                executable=True,
            )
        assert tool_name == "browseract.mootion_movie"
        assert principal_id == "exec-scene-video-mootion-remote"
        return CapabilityRoute(
            provider_key="browseract",
            capability_key="mootion_movie",
            tool_name="browseract.mootion_movie",
            executable=True,
        )

    monkeypatch.setattr(provider_registry, "route_tool_with_context", _route_tool_with_context)
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "mootion",
            "provider_backend_key": "mootion",
            "ready": False,
            "status": "blocked",
            "blockers": ["mootion_docker_socket_missing", "mootion_docker_cli_missing"],
            "checks": {
                "script_exists": True,
                "docker_socket_configured": False,
                "docker_cli_configured": False,
            },
        },
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        provider_registry=provider_registry,
    )
    captured_payload: dict[str, object] = {}

    def _fake_mootion_scene_video(request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        captured_payload.update(dict(request.payload_json or {}))
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind="movie.render",
            target_ref="https://cdn.example/mootion/runsite-remote.mp4",
            output_json={
                "render_status": "rendered",
                "asset_url": "https://cdn.example/mootion/runsite-remote.mp4",
                "download_url": "https://cdn.example/mootion/runsite-remote.mp4",
                "editor_url": "https://mootion.example/project/runsite",
                "structured_output_json": {
                    "asset_url": "https://cdn.example/mootion/runsite-remote.mp4",
                    "workflow_id": "wf-mootion-remote",
                },
            },
            receipt_json={"handler_key": definition.tool_name},
        )

    service.register_handler("browseract.mootion_movie", _fake_mootion_scene_video)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-mootion-remote-1",
            step_id="step-scene-video-mootion-remote-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "mootion",
                "context_kind": "scene_briefing",
                "title": "Runsite Mootion remote scene",
                "script_text": "A remote BrowserAct Mootion briefing render.",
                "binding_id": binding.binding_id,
                "workflow_id": "wf-mootion-remote",
                "timeout_seconds": 120,
            },
            context_json={"principal_id": "exec-scene-video-mootion-remote"},
        )
    )

    assert captured_payload["force_browseract"] is True
    assert captured_payload["allow_browseract_remote_fallback"] is True
    assert captured_payload["remote_fallback_allowed"] is True
    assert captured_payload["binding_id"] == binding.binding_id
    assert captured_payload["workflow_id"] == "wf-mootion-remote"
    assert result.output_json["provider_key"] == "mootion"
    assert result.output_json["render_status"] == "rendered"
    assert result.output_json["video_url"] == "https://cdn.example/mootion/runsite-remote.mp4"


def test_tool_execution_scene_video_mootion_auto_selects_browseract_bridge_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_mootion_public_dns(monkeypatch)
    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS", "cdn.example")
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-scene-video-mootion-auto",
        connector_name="browseract",
        external_account_ref="mootion-scene-video-bridge",
        scope_json={"services": ["mootion", "mootion_movie", "browseract.mootion_movie"]},
        auth_metadata_json={
            "mootion_browseract_bridge": True,
            "service_key": "mootion_movie",
            "mootion_movie_workflow_id": "wf-mootion-auto",
            "browseract_mootion_movie_workflow_id": "wf-mootion-auto",
        },
        status="enabled",
    )
    provider_registry = ProviderRegistryService()

    def _route_tool_with_context(tool_name: str, *, principal_id: str | None = None) -> CapabilityRoute:
        if tool_name == "ea.scene_video_generate":
            return CapabilityRoute(
                provider_key="ea",
                capability_key="scene_video_generate",
                tool_name="ea.scene_video_generate",
                executable=True,
            )
        assert tool_name == "browseract.mootion_movie"
        assert principal_id == "exec-scene-video-mootion-auto"
        return CapabilityRoute(
            provider_key="browseract",
            capability_key="mootion_movie",
            tool_name="browseract.mootion_movie",
            executable=True,
        )

    monkeypatch.setattr(provider_registry, "route_tool_with_context", _route_tool_with_context)
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "mootion",
            "provider_backend_key": "mootion",
            "ready": False,
            "status": "blocked",
            "blockers": ["mootion_docker_socket_missing", "mootion_docker_cli_missing"],
            "checks": {
                "script_exists": True,
                "docker_socket_configured": False,
                "docker_cli_configured": False,
            },
        },
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        provider_registry=provider_registry,
    )
    captured_payload: dict[str, object] = {}

    def _fake_mootion_scene_video(request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        captured_payload.update(dict(request.payload_json or {}))
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind="movie.render",
            target_ref="https://cdn.example/mootion/auto-bridge.mp4",
            output_json={
                "render_status": "rendered",
                "asset_url": "https://cdn.example/mootion/auto-bridge.mp4",
                "structured_output_json": {
                    "asset_url": "https://cdn.example/mootion/auto-bridge.mp4",
                    "workflow_id": "wf-mootion-auto",
                },
            },
            receipt_json={"handler_key": definition.tool_name},
        )

    service.register_handler("browseract.mootion_movie", _fake_mootion_scene_video)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-mootion-auto-1",
            step_id="step-scene-video-mootion-auto-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "mootion",
                "context_kind": "scene_briefing",
                "title": "Runsite Mootion auto bridge scene",
                "script_text": "Auto-select the principal Mootion BrowserAct bridge.",
            },
            context_json={"principal_id": "exec-scene-video-mootion-auto"},
        )
    )

    assert captured_payload["binding_id"] == binding.binding_id
    assert captured_payload["workflow_id"] == "wf-mootion-auto"
    assert captured_payload["force_browseract"] is True
    assert captured_payload["allow_browseract_remote_fallback"] is True
    assert result.output_json["provider_key"] == "mootion"
    assert result.output_json["video_url"] == "https://cdn.example/mootion/auto-bridge.mp4"

    monkeypatch.delenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS")
    blocked = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-mootion-auto-readiness-2",
            step_id="step-scene-video-mootion-auto-readiness-2",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "mootion",
                "context_kind": "scene_briefing",
                "title": "Mootion bridge host policy readiness",
                "script_text": "Require the remote asset host policy.",
                "readiness_only": True,
            },
            context_json={"principal_id": "exec-scene-video-mootion-auto"},
        )
    )
    assert blocked.output_json["render_status"] == "blocked"
    assert "mootion_remote_asset_host_allowlist_missing" in blocked.output_json["structured_output_json"]["reason"]

    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS", "https://cdn.example")
    invalid = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-mootion-auto-readiness-3",
            step_id="step-scene-video-mootion-auto-readiness-3",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "mootion",
                "context_kind": "scene_briefing",
                "title": "Mootion bridge malformed host policy readiness",
                "script_text": "Reject malformed remote asset host policy.",
                "readiness_only": True,
            },
            context_json={"principal_id": "exec-scene-video-mootion-auto"},
        )
    )
    assert invalid.output_json["render_status"] == "blocked"
    assert "mootion_remote_asset_host_allowlist_invalid" in invalid.output_json["structured_output_json"]["reason"]


def test_tool_execution_property_walkthrough_mootion_never_uses_direct_browseract_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_mootion_public_dns(monkeypatch)
    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS", "cdn.example")
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-property-mootion-remote",
        connector_name="browseract",
        external_account_ref="mootion-property-bridge",
        scope_json={"services": ["mootion", "mootion_movie", "propertyquarry"]},
        auth_metadata_json={
            "mootion_browseract_bridge": True,
            "service_key": "mootion_movie",
            "mootion_movie_workflow_id": "wf-property-mootion-remote",
        },
        status="enabled",
    )
    provider_registry = ProviderRegistryService()

    def _route_tool_with_context(tool_name: str, *, principal_id: str | None = None) -> CapabilityRoute:
        if tool_name == "ea.scene_video_generate":
            return CapabilityRoute(
                provider_key="ea",
                capability_key="scene_video_generate",
                tool_name="ea.scene_video_generate",
                executable=True,
            )
        assert tool_name == "browseract.mootion_movie"
        assert principal_id == "exec-property-mootion-remote"
        return CapabilityRoute(
            provider_key="browseract",
            capability_key="mootion_movie",
            tool_name="browseract.mootion_movie",
            executable=True,
        )

    monkeypatch.setattr(provider_registry, "route_tool_with_context", _route_tool_with_context)
    monkeypatch.setattr(
        "app.services.scene_video_contract.resolve_property_walkthrough_runtime_provider",
        lambda provider_key: {
            "provider_key": "mootion",
            "provider_backend_key": "mootion",
            "runtime_readiness_json": {
                "provider_key": "mootion",
                "provider_backend_key": "mootion",
                "ready": True,
                "status": "ready",
                "blockers": [],
                "execution_lane": "ea_governed_render",
                "checks": {"provider_execution_in_web_process": False},
            },
            "checked": [],
            "selected_via": "governed_render_explicit",
        },
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        provider_registry=provider_registry,
    )
    captured_remote_payload: dict[str, object] = {}

    def _fake_mootion_remote(request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        captured_remote_payload.update(dict(request.payload_json or {}))
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind="movie.render",
            target_ref="https://cdn.example/mootion/property-remote.mp4",
            output_json={
                "binding_id": binding.binding_id,
                "workflow_id": "wf-property-mootion-remote",
                "task_id": "task-property-mootion-remote",
                "render_status": "rendered",
                "download_url": "https://cdn.example/mootion/property-remote.mp4",
                "editor_url": "https://mootion.example/project/property-remote",
                "structured_output_json": {
                    "render_status": "rendered",
                    "download_url": "https://cdn.example/mootion/property-remote.mp4",
                },
            },
            receipt_json={"handler_key": definition.tool_name},
        )

    service.register_handler("browseract.mootion_movie", _fake_mootion_remote)
    captured_render_kwargs: dict[str, object] = {}

    def _fake_property_render(**kwargs: object) -> dict[str, object]:
        captured_render_kwargs.update(kwargs)
        assert kwargs.get("mootion_remote_render_callback") is None
        return {
            "status": "pending",
            "provider_key": "mootion",
            "media_route_provider_key": "mootion",
            "execution_lane": "ea_governed_render",
            "video_url": "",
            "flythrough_url": "",
        }

    monkeypatch.setattr("app.product.service._render_property_flythrough_into_hosted_tour", _fake_property_render)
    monkeypatch.setattr(
        "app.product.service._hosted_property_tour_video_delivery",
        lambda tour_url: {},
    )
    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-property-mootion-remote-1",
            step_id="step-property-mootion-remote-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "mootion",
                "context_kind": "property_walkthrough",
                "title": "Remote Property Walkthrough",
                "tour_url": "https://property.example/tours/remote",
                "tour_context_json": {"verified_provider": "3dvista"},
                "external_processing_consent_receipt": "opaque-server-issued-mootion-receipt",
                "workflow_id": "wf-caller-must-not-override-binding",
                "run_url": "https://attacker.example/collect",
            },
            context_json={"principal_id": "exec-property-mootion-remote"},
        )
    )

    assert captured_remote_payload == {}
    assert captured_render_kwargs["mootion_remote_render_callback"] is None
    assert captured_render_kwargs["external_processing_consent_receipt"] == ""
    assert result.output_json["provider_key"] == "mootion"
    assert result.output_json["render_status"] == "pending"
    assert result.output_json["video_url"] == ""
    assert result.output_json["runtime_readiness_json"]["execution_lane"] == "ea_governed_render"

    def _fake_mootion_missing_status(request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind="movie.render",
            target_ref="https://cdn.example/mootion/missing-status.mp4",
            output_json={
                "binding_id": binding.binding_id,
                "workflow_id": "wf-property-mootion-remote",
                "download_url": "https://cdn.example/mootion/missing-status.mp4",
                "structured_output_json": {},
            },
            receipt_json={"handler_key": definition.tool_name},
        )

    service.register_handler("browseract.mootion_movie", _fake_mootion_missing_status)
    repeated = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-property-mootion-missing-status-1",
            step_id="step-property-mootion-missing-status-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "mootion",
                "context_kind": "property_walkthrough",
                "title": "Missing Status Property Walkthrough",
                "tour_url": "https://property.example/tours/remote",
                "external_processing_consent_receipt": "opaque-server-issued-mootion-receipt-2",
            },
            context_json={"principal_id": "exec-property-mootion-remote"},
        )
    )
    assert repeated.output_json["render_status"] == "pending"
    assert captured_remote_payload == {}


def test_tool_execution_mootion_remote_binding_is_principal_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    foreign_binding = tool_runtime.upsert_connector_binding(
        principal_id="other-principal",
        connector_name="browseract",
        external_account_ref="mootion-foreign-bridge",
        scope_json={"services": ["mootion", "mootion_movie"]},
        auth_metadata_json={
            "mootion_browseract_bridge": True,
            "service_key": "mootion_movie",
            "mootion_movie_workflow_id": "wf-mootion-foreign",
        },
        status="enabled",
    )
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "mootion",
            "provider_backend_key": "mootion",
            "ready": True,
            "status": "ready",
            "blockers": [],
            "execution_lane": "browseract_remote",
            "checks": {
                "mootion_execution_lane": "browseract_remote",
                "mootion_local_worker_blockers": ["mootion_docker_socket_missing"],
                "mootion_browseract_remote": {"ready": True, "status": "ready", "target_count": 1},
            },
        },
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-mootion-foreign-1",
            step_id="step-scene-video-mootion-foreign-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "mootion",
                "context_kind": "scene_briefing",
                "title": "Foreign binding must not satisfy readiness",
                "script_text": "Keep connector bindings principal scoped.",
                "binding_id": foreign_binding.binding_id,
                "workflow_id": "wf-mootion-foreign",
                "readiness_only": True,
            },
            context_json={"principal_id": "requesting-principal"},
        )
    )

    assert result.output_json["render_status"] == "blocked"
    assert "mootion_browseract_principal_binding_missing" in result.output_json["structured_output_json"]["reason"]
    assert result.output_json["runtime_readiness_json"]["execution_lane"] == "blocked"

    no_binding_result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-mootion-no-binding-1",
            step_id="step-scene-video-mootion-no-binding-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "mootion",
                "context_kind": "scene_briefing",
                "title": "Workflow without a binding must not satisfy readiness",
                "script_text": "A workflow ID alone is not authority.",
                "workflow_id": "wf-caller-only",
                "readiness_only": True,
            },
            context_json={"principal_id": "requesting-principal"},
        )
    )

    assert no_binding_result.output_json["render_status"] == "blocked"
    assert "mootion_browseract_principal_binding_missing" in no_binding_result.output_json["structured_output_json"]["reason"]


def test_property_mootion_remote_asset_keeps_hosted_tour_quality_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.product import service as product_service
    from app import mootion_remote_asset_fetch as remote_fetch

    _stub_mootion_public_dns(monkeypatch)
    monkeypatch.setattr(
        product_service,
        "_materialize_mootion_remote_video_asset",
        product_service._materialize_mootion_remote_video_asset_in_process,
    )

    bundle_dir = tmp_path / "hosted-tour"
    manifest_updates: list[dict[str, object]] = []
    progress_rows: list[dict[str, object]] = []
    callback_packet: dict[str, object] = {}
    video_bytes = b"bounded-mootion-video"

    class _FakeResponse:
        headers = {"Content-Length": str(len(video_bytes))}

        def __init__(self) -> None:
            self._offset = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def geturl(self) -> str:
            return "https://cdn.example/mootion/property-gated.mp4"

        def read1(self, size: int = -1) -> bytes:
            if self._offset >= len(video_bytes):
                return b""
            end = len(video_bytes) if size < 0 else min(len(video_bytes), self._offset + size)
            chunk = video_bytes[self._offset:end]
            self._offset = end
            return chunk

    class _FakeOpener:
        def open(self, request, timeout: float):
            return _FakeResponse()

    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_bundle_dir",
        lambda tour_url: ("remote-gated", bundle_dir),
    )
    monkeypatch.setattr(
        product_service,
        "_property_walkthrough_scene_video_context",
        lambda tour_url, principal_id="": {"verified_provider": "3dvista", "scene_count": 2},
    )
    monkeypatch.setattr(
        product_service,
        "_property_walkthrough_enrich_facts_with_context",
        lambda property_facts, tour_context_json=None: dict(property_facts or {}),
    )
    monkeypatch.setattr(product_service, "_property_walkthrough_context_reference_text", lambda context: "Verified tour context.")
    monkeypatch.setattr(product_service, "_hosted_property_tour_scene_count", lambda tour_url: 2)
    monkeypatch.setattr(product_service, "_magicfit_property_room_visit_plan", lambda **kwargs: (2, ["entry", "living room"]))
    monkeypatch.setattr(product_service, "_magicfit_flythrough_minimum_duration_seconds", lambda **kwargs: 12.0)
    monkeypatch.setattr(product_service, "_video_duration_seconds", lambda path: 18.0)
    monkeypatch.setattr(product_service, "_video_continuous_shot_gate", lambda path: (True, "", {"cut_count": 0}))
    monkeypatch.setattr(
        product_service,
        "_write_hosted_property_visual_progress",
        lambda **kwargs: progress_rows.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        product_service,
        "_update_hosted_property_tour_video_manifest",
        lambda **kwargs: manifest_updates.append(dict(kwargs)),
    )
    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS", "cdn.example")
    class _FakeSocket:
        def settimeout(self, timeout: float) -> None:
            return None

    _FakeResponse.fp = type("FakeFp", (), {"raw": type("FakeRaw", (), {"_sock": _FakeSocket()})()})()
    monkeypatch.setattr(
        remote_fetch,
        "_mootion_remote_asset_global_addresses",
        lambda hostname, deadline=None: ("93.184.216.34",),
    )
    monkeypatch.setattr(remote_fetch, "_mootion_remote_asset_opener", lambda: _FakeOpener())

    def _remote_render(packet: dict[str, object]) -> dict[str, object]:
        callback_packet.update(packet)
        return {
            "render_status": "rendered",
            "download_url": "https://cdn.example/mootion/property-gated.mp4",
            "editor_url": "https://mootion.example/project/property-gated",
            "raw_text": "private provider output",
            "structured_output_json": {
                "execution_lane": "browseract_remote",
                "browseract_binding_id": "private-binding-id",
                "nested_secret": "private-provider-metadata",
            },
        }

    result = product_service._render_mootion_property_flythrough_into_hosted_tour(
        tour_url="https://property.example/tours/remote-gated",
        title="Remote Gated Walkthrough",
        principal_id="exec-property-mootion-gated",
        property_facts={"rooms": 2},
        remote_render_callback=_remote_render,
    )

    assert callback_packet["camera_style"] == "continuous first-person real-estate walkthrough"
    assert callback_packet["duration_seconds"] >= 12
    assert result["status"] == "rendered"
    assert result["continuous_shot_gate"]["cut_count"] == 0
    assert Path(str(result["video_file_path"])).read_bytes() == video_bytes
    assert manifest_updates and manifest_updates[0]["provider_key"] == "mootion"
    assert progress_rows[-1]["status"] == "ready"
    sidecar = json.loads(Path(str(result["sidecar_path"])).read_text())
    assert sidecar["execution_lane"] == "browseract_remote"
    assert sidecar["quality_gates"]["continuous_shot"] == "pass"
    assert "editor_url" not in sidecar
    assert "raw_text" not in sidecar
    assert "structured_output_json" not in sidecar

    (bundle_dir / "accepted-manifest.json").write_text('{"video":"accepted"}', encoding="utf-8")
    accepted_bundle = {path.name: path.read_bytes() for path in bundle_dir.iterdir() if path.is_file()}
    rejected_asset = tmp_path / "rejected-rerender.mp4"
    rejected_asset.write_bytes(b"rejected-rerender-video")
    monkeypatch.setattr(product_service, "_video_duration_seconds", lambda path: 2.0)
    too_short = product_service._render_mootion_property_flythrough_into_hosted_tour(
        tour_url="https://property.example/tours/remote-gated",
        title="Rejected Remote Rerender",
        principal_id="exec-property-mootion-gated",
        property_facts={"rooms": 2},
        remote_render_callback=lambda packet: {
            "render_status": "completed",
            "asset_path": str(rejected_asset),
        },
    )
    assert too_short["reason"] == "mootion_render_too_short"
    assert {path.name: path.read_bytes() for path in bundle_dir.iterdir() if path.is_file()} == accepted_bundle

    monkeypatch.setattr(product_service, "_video_duration_seconds", lambda path: 18.0)
    monkeypatch.setattr(
        product_service,
        "_update_hosted_property_tour_video_manifest",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("manifest unavailable")),
    )
    manifest_failed = product_service._render_mootion_property_flythrough_into_hosted_tour(
        tour_url="https://property.example/tours/remote-gated",
        title="Manifest-Failed Remote Rerender",
        principal_id="exec-property-mootion-gated",
        property_facts={"rooms": 2},
        remote_render_callback=lambda packet: {
            "render_status": "completed",
            "asset_path": str(rejected_asset),
        },
    )
    assert manifest_failed["reason"] == "mootion_manifest_update_failed"
    assert {path.name: path.read_bytes() for path in bundle_dir.iterdir() if path.is_file()} == accepted_bundle


@pytest.mark.parametrize(
    ("asset_url", "reason"),
    (
        ("http://cdn.example/mootion/video.mp4", "mootion_remote_asset_url_invalid"),
        ("https://127.0.0.1/mootion/video.mp4", "mootion_remote_asset_url_invalid"),
        ("https://service.internal/mootion/video.mp4", "mootion_remote_asset_host_blocked"),
    ),
)
def test_property_mootion_remote_asset_rejects_unsafe_targets(
    tmp_path: Path,
    asset_url: str,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.product import service as product_service

    _stub_mootion_public_dns(monkeypatch)
    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS", "cdn.example")
    with pytest.raises(RuntimeError, match=reason):
        product_service._materialize_mootion_remote_video_asset_in_process(asset_url, target_dir=tmp_path)


@pytest.mark.parametrize(
    ("hostname", "reason"),
    (
        ("2130706433", "mootion_remote_asset_host_allowlist_invalid"),
        ("private-dns.example", "mootion_remote_asset_host_blocked"),
    ),
)
def test_property_mootion_remote_asset_rejects_private_dns_resolution(
    hostname: str,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import mootion_remote_asset_fetch as remote_fetch

    monkeypatch.setattr(
        remote_fetch.socket,
        "getaddrinfo",
        lambda host, port, type=0: [
            (remote_fetch.socket.AF_INET, remote_fetch.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )

    with pytest.raises(RuntimeError, match=reason):
        remote_fetch._mootion_remote_asset_global_addresses(hostname)


def test_property_mootion_remote_asset_validates_redirect_before_second_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.product import service as product_service
    from app import mootion_remote_asset_fetch as remote_fetch

    _stub_mootion_public_dns(monkeypatch)

    calls: list[str] = []

    class _RedirectingOpener:
        def open(self, request, timeout: float):
            calls.append(request.full_url)
            raise product_service.urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "https://127.0.0.1/private.mp4"},
                None,
            )

    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS", "public.example")
    monkeypatch.setattr(
        remote_fetch,
        "_mootion_remote_asset_global_addresses",
        lambda hostname, deadline=None: ("93.184.216.34",),
    )
    monkeypatch.setattr(remote_fetch, "_mootion_remote_asset_opener", lambda: _RedirectingOpener())

    with pytest.raises(RuntimeError, match="mootion_remote_asset_url_invalid"):
        product_service._materialize_mootion_remote_video_asset_in_process(
            "https://public.example/video.mp4",
            target_dir=tmp_path,
        )

    assert calls == ["https://public.example/video.mp4"]


def test_property_mootion_remote_asset_enforces_stream_size_and_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.product import service as product_service
    from app import mootion_remote_asset_fetch as remote_fetch

    _stub_mootion_public_dns(monkeypatch)

    class _FakeResponse:
        headers: dict[str, str] = {}

        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = list(chunks)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def geturl(self) -> str:
            return "https://cdn.example/video.mp4"

        def read1(self, size: int = -1) -> bytes:
            return self._chunks.pop(0) if self._chunks else b""

    class _FakeSocket:
        def settimeout(self, timeout: float) -> None:
            return None

    _FakeResponse.fp = type("FakeFp", (), {"raw": type("FakeRaw", (), {"_sock": _FakeSocket()})()})()

    class _FakeOpener:
        def __init__(self, response: _FakeResponse) -> None:
            self._response = response

        def open(self, request, timeout: float):
            return self._response

    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS", "cdn.example")
    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_MAX_BYTES", str(1024 * 1024))
    monkeypatch.setattr(
        remote_fetch,
        "_mootion_remote_asset_global_addresses",
        lambda hostname, deadline=None: ("93.184.216.34",),
    )
    monkeypatch.setattr(
        remote_fetch,
        "_mootion_remote_asset_opener",
        lambda: _FakeOpener(_FakeResponse([b"a" * 700_000, b"b" * 700_000])),
    )

    with pytest.raises(RuntimeError, match="mootion_remote_asset_too_large"):
        product_service._materialize_mootion_remote_video_asset_in_process(
            "https://cdn.example/video.mp4",
            target_dir=tmp_path,
        )
    assert not (tmp_path / "mootion-browseract-remote.mp4").exists()

    def _deadline_exceeded(*args, **kwargs):
        raise RuntimeError("mootion_remote_asset_deadline_exceeded")

    monkeypatch.setattr(remote_fetch, "_stream_mootion_remote_asset_response", _deadline_exceeded)
    monkeypatch.setattr(
        remote_fetch,
        "_mootion_remote_asset_opener",
        lambda: _FakeOpener(_FakeResponse([b"video"])),
    )

    with pytest.raises(RuntimeError, match="mootion_remote_asset_deadline_exceeded"):
        product_service._materialize_mootion_remote_video_asset_in_process(
            "https://cdn.example/video.mp4",
            target_dir=tmp_path,
        )
    assert not (tmp_path / "mootion-browseract-remote.mp4").exists()


def test_property_mootion_remote_asset_deadline_stops_a_real_trickling_stream(tmp_path: Path) -> None:
    import http.client
    import socket
    import threading
    import time

    from app import mootion_remote_asset_fetch as remote_fetch

    client_socket, server_socket = socket.socketpair()
    stop = threading.Event()

    def _serve_trickle() -> None:
        try:
            server_socket.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            while not stop.is_set():
                server_socket.sendall(b"x")
                time.sleep(0.02)
        except OSError:
            return

    worker = threading.Thread(target=_serve_trickle, daemon=True)
    worker.start()
    response = http.client.HTTPResponse(client_socket)
    response.begin()
    output_path = tmp_path / "trickle.part"
    started_at = time.monotonic()
    try:
        with output_path.open("wb") as output:
            with pytest.raises(RuntimeError, match="mootion_remote_asset_deadline_exceeded"):
                remote_fetch._stream_mootion_remote_asset_response(
                    response,
                    output=output,
                    max_bytes=1024 * 1024,
                    deadline=started_at + 0.15,
                )
    finally:
        stop.set()
        response.close()
        client_socket.close()
        server_socket.close()
        worker.join(timeout=1.0)
    assert time.monotonic() - started_at < 0.75
    assert output_path.stat().st_size < 100


def test_property_mootion_remote_asset_worker_watchdog_stops_header_trickle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    from app.product import service as product_service

    worker_path = tmp_path / "header_trickle_worker.py"
    worker_path.write_text(
        """
import http.client
import json
import socket
import sys
import threading
import time
from pathlib import Path

request = json.load(sys.stdin)
target = Path(request["target_dir"]) / "mootion-browseract-remote.mp4"
target.write_bytes(b"partial-header-fetch")
client, server = socket.socketpair()

def trickle():
    try:
        for value in b"HTTP/1.1 200 OK\\r\\nX-Test: never-finished":
            server.sendall(bytes((value,)))
            time.sleep(0.02)
    except OSError:
        pass

threading.Thread(target=trickle, daemon=True).start()
response = http.client.HTTPResponse(client)
response.begin()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS", "cdn.example")
    monkeypatch.setattr(product_service, "_mootion_remote_asset_fetch_worker_path", lambda: worker_path)
    target_path = tmp_path / "mootion-browseract-remote.mp4"
    started_at = time.monotonic()

    with pytest.raises(RuntimeError, match="mootion_remote_asset_deadline_exceeded"):
        product_service._run_mootion_remote_asset_fetch_worker(
            asset_url="https://cdn.example/video.mp4",
            target_dir=tmp_path,
            target_path=target_path,
            deadline=started_at + 0.4,
        )

    assert time.monotonic() - started_at < 1.25
    assert not target_path.exists()
    time.sleep(0.1)
    assert not target_path.exists()


def test_property_mootion_remote_asset_worker_wrapper_validates_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.product import service as product_service

    worker_path = tmp_path / "successful_worker.py"
    worker_path.write_text(
        """
import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
target = Path(request["target_dir"]) / "mootion-browseract-remote.mp4"
target.write_bytes(b"bounded-worker-video")
print(json.dumps({"status": "completed", "size_bytes": target.stat().st_size}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS", "cdn.example")
    monkeypatch.setattr(product_service, "_mootion_remote_asset_fetch_worker_path", lambda: worker_path)

    target_path = product_service._materialize_mootion_remote_video_asset(
        "https://cdn.example/video.mp4",
        target_dir=tmp_path,
    )

    assert target_path.read_bytes() == b"bounded-worker-video"


def test_property_mootion_remote_asset_real_worker_fails_closed_on_malformed_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.product import service as product_service

    monkeypatch.setenv("PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS", "https://cdn.example")

    with pytest.raises(RuntimeError, match="mootion_remote_asset_host_allowlist_invalid"):
        product_service._materialize_mootion_remote_video_asset(
            "https://cdn.example/video.mp4",
            target_dir=tmp_path,
        )
    assert not (tmp_path / "mootion-browseract-remote.mp4").exists()


def test_tool_execution_scene_video_omagic_uses_model_upload_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    provider_registry = ProviderRegistryService()
    monkeypatch.setattr(
        provider_registry,
        "route_tool_with_context",
        lambda tool_name, principal_id=None: CapabilityRoute(
            provider_key="ea",
            capability_key="scene_video_generate",
            tool_name="ea.scene_video_generate",
            executable=True,
        ),
    )
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "omagic",
            "provider_backend_key": "omagic",
            "ready": True,
            "status": "ready",
            "blockers": [],
            "checks": {"model_upload_supported": True},
        },
    )
    fake_adapter = tmp_path / "fake_omagic_adapter.py"
    fake_adapter.write_text(
        """
from __future__ import annotations

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--request-json", required=True)
parser.add_argument("--out", required=True)
parser.add_argument("--state-json", required=True)
args = parser.parse_args()
request = json.loads(Path(args.request_json).read_text(encoding="utf-8"))
assert request["provider_key"] == "omagic"
assert request["model_path"].endswith("scene.glb")
Path(args.out).write_bytes(b"tool-omagic-video")
state = {
    "render_status": "completed",
    "video_path": args.out,
    "model_input_consumed": True,
    "model_input_consumption_proof": "fake-tool-command-adapter",
}
Path(args.state_json).write_text(json.dumps(state), encoding="utf-8")
print(json.dumps(state))
""".lstrip(),
        encoding="utf-8",
    )
    model_path = tmp_path / "scene.glb"
    model_path.write_bytes(b"glTF")
    first_frame = tmp_path / "first-frame.png"
    first_frame.write_bytes(b"png")
    monkeypatch.setenv("PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_OMAGIC_RENDER_COMMAND", f"{sys.executable} {fake_adapter}")
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        provider_registry=provider_registry,
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-omagic-adapter-1",
            step_id="step-scene-video-omagic-adapter-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "magic",
                "context_kind": "scene_briefing",
                "title": "Runsite OMagic model render",
                "script_text": "A model-backed camera move through the generated apartment.",
                "model_path": str(model_path),
                "model_asset_kind": "glb",
                "first_frame_path": str(first_frame),
            },
            context_json={"principal_id": "exec-scene-video-omagic-adapter"},
        )
    )

    structured = result.output_json["structured_output_json"]["structured_output_json"]
    assert result.output_json["provider_key"] == "omagic"
    assert result.output_json["provider_backend_key"] == "omagic"
    assert result.output_json["render_status"] == "completed"
    assert result.output_json["video_url"].endswith("scene-video.mp4")
    assert structured["model_input_consumed"] is True
    assert structured["model_input_consumption_proof"] == "fake-tool-command-adapter"
    assert result.receipt_json["provider_backend_key"] == "omagic"
    assert result.receipt_json["model_input_consumed"] is True


def test_tool_execution_scene_video_blocks_before_delegate_when_runtime_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    provider_registry = ProviderRegistryService()

    monkeypatch.setattr(
        provider_registry,
        "route_tool_with_context",
        lambda tool_name, principal_id=None: CapabilityRoute(
            provider_key="ea",
            capability_key="scene_video_generate",
            tool_name="ea.scene_video_generate",
            executable=True,
        ),
    )
    monkeypatch.setattr(
        "app.services.scene_video_contract.scene_video_provider_runtime_readiness",
        lambda provider_key: {
            "provider_key": "omagic",
            "provider_backend_key": "omagic",
            "ready": False,
            "status": "blocked",
            "blockers": ["omagic_model_upload_adapter_missing"],
            "checks": {"model_upload_supported": False},
        },
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        provider_registry=provider_registry,
    )

    def _unexpected_delegate(request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        raise AssertionError("blocked scene-video render should not call the provider delegate")

    service.register_handler("provider.onemin.property_walkthrough_video", _unexpected_delegate)

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-blocked-1",
            step_id="step-scene-video-blocked-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "magic",
                "context_kind": "scene_briefing",
                "title": "Runsite fight scene",
                "script_text": "A tactical runsite briefing push-in.",
                "first_frame_path": "/tmp/first-frame.png",
            },
            context_json={"principal_id": "exec-scene-video-blocked"},
        )
    )

    assert result.output_json["provider_key"] == "omagic"
    assert result.output_json["render_status"] == "blocked"
    assert result.output_json["runtime_readiness_json"]["checks"]["model_upload_supported"] is False
    assert result.receipt_json["runtime_readiness_json"]["blockers"] == ["omagic_model_upload_adapter_missing"]


def test_tool_execution_scene_video_readiness_only_reports_runtime_and_telegram_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    (script_dir / "render_magicfit_property_flythrough.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    monkeypatch.setenv("EA_REPO_ROOT", str(tmp_path))
    provider_ledger_dir = tmp_path / "provider-ledger"
    monkeypatch.setenv("EA_RESPONSES_PROVIDER_LEDGER_DIR", str(provider_ledger_dir))
    monkeypatch.setenv("PROPERTYQUARRY_PROVIDER_LEDGER_DIR", str(provider_ledger_dir))
    monkeypatch.setenv("MAGICFIT_EMAIL", "operator@example.test")
    monkeypatch.setenv("MAGICFIT_PASSWORD", "secret")
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    tool_runtime.upsert_connector_binding(
        principal_id="exec-scene-video-ready",
        connector_name="telegram_identity",
        external_account_ref="42",
        auth_metadata_json={"default_chat_ref": "42", "bot_key": "default", "bot_handle": "operator_concierge_bot"},
        scope_json={"assistant_surfaces": ["dm"]},
        status="enabled",
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-scene-video-readiness-1",
            step_id="step-scene-video-readiness-1",
            tool_name="ea.scene_video_generate",
            action_kind="video.generate",
            payload_json={
                "provider_key": "magic fit",
                "context_kind": "scene_briefing",
                "title": "Runsite readiness",
                "readiness_only": True,
                "telegram_delivery_requested": True,
            },
            context_json={"principal_id": "exec-scene-video-ready"},
        )
    )

    assert result.output_json["provider_key"] == "magicfit"
    assert result.output_json["render_status"] == "ready"
    assert result.output_json["runtime_readiness_json"]["status"] == "ready"
    assert result.output_json["runtime_readiness_json"]["checks"]["credit_state"] == "unprobed"
    assert result.output_json["telegram_delivery_readiness_json"]["status"] == "ready"
    assert result.receipt_json["runtime_readiness_json"]["checks"]["credentials_configured"] is True


def test_tool_execution_service_self_heals_missing_builtin_comfyui_image_generate_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMFYUI_URL", "https://images.example")
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    def _fake_execute_image_generate(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind="image.generate",
            target_ref="comfyui:test",
            output_json={
                "asset_urls": ["https://images.example/view?filename=test.png&type=output"],
                "provider_backend": "comfyui",
                "preview_text": "test prompt",
            },
            receipt_json={
                "handler_key": definition.tool_name,
                "provider_key": "comfyui",
            },
            model_name="SDXL-Lightning-4step",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
        )

    monkeypatch.setattr(
        "app.services.tool_execution_comfyui_adapter.ComfyUIToolAdapter.execute_image_generate",
        _fake_execute_image_generate,
    )
    registry._rows.pop("provider.comfyui.image_generate", None)  # type: ignore[attr-defined]
    registry._order = [key for key in registry._order if key != "provider.comfyui.image_generate"]  # type: ignore[attr-defined]

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-comfyui-image-1",
            step_id="step-comfyui-image-1",
            tool_name="provider.comfyui.image_generate",
            action_kind="image.generate",
            payload_json={"prompt": "Render a cinematic office still."},
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "provider.comfyui.image_generate"
    assert result.output_json["provider_backend"] == "comfyui"
    assert tool_runtime.get_tool("provider.comfyui.image_generate") is not None


def test_comfyui_tool_adapter_falls_back_to_onemin_when_primary_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.tool_execution_comfyui_adapter import ComfyUIToolAdapter

    monkeypatch.setenv("COMFYUI_URL", "https://images.example")
    monkeypatch.setenv("COMFYUI_FALLBACK_TO_ONEMIN", "1")

    def _boom_call_comfyui(prompt: str, *, width: int = 1024, height: int = 1408, steps: int = 4) -> dict[str, object]:
        raise ToolExecutionError("comfyui_connection_failed:timeout")

    def _fake_onemin_execute(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        assert request.tool_name == "provider.onemin.image_generate"
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind="image.generate",
            target_ref="onemin:test",
            output_json={
                "asset_urls": ["https://cdn.1min.ai/generated/fallback-image.png"],
                "provider_backend": "1min",
                "provider_account_name": "acct-image",
            },
            receipt_json={
                "handler_key": definition.tool_name,
                "provider_key": "onemin",
                "provider_backend": "1min",
            },
            model_name="gpt-image-1-mini",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
        )

    monkeypatch.setattr("app.services.tool_execution_comfyui_adapter._call_comfyui", _boom_call_comfyui)
    monkeypatch.setattr(
        "app.services.tool_execution_onemin_adapter.OneminToolAdapter.execute_image_generate",
        _fake_onemin_execute,
    )

    adapter = ComfyUIToolAdapter()
    result = adapter.execute_image_generate(
        ToolInvocationRequest(
            session_id="session-comfy-fallback",
            step_id="step-comfy-fallback",
            tool_name="provider.comfyui.image_generate",
            action_kind="image.generate",
            payload_json={"prompt": "Render fallback art.", "width": 1024, "height": 1024},
            context_json={"principal_id": "exec-1"},
        ),
        ToolDefinition(
            tool_name="provider.comfyui.image_generate",
            version="v1",
            input_schema_json={},
            output_schema_json={},
            policy_json={"builtin": True, "action_kind": "image.generate"},
            allowed_channels=(),
            approval_default="none",
            enabled=True,
            updated_at="2026-04-22T00:00:00Z",
        ),
    )

    assert result.tool_name == "provider.onemin.image_generate"
    assert result.output_json["asset_urls"] == ["https://cdn.1min.ai/generated/fallback-image.png"]
    assert result.receipt_json["provider_key"] == "onemin"


def test_onemin_tool_adapter_feature_uses_manager_to_avoid_core_occupied_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services import responses_upstream as upstream
    from app.services.onemin_manager import OneminManagerService, register_onemin_manager
    from app.services.tool_execution_onemin_adapter import OneminToolAdapter

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    register_onemin_manager(manager)

    class _Config:
        api_keys = ("key-core", "key-image")
        timeout_seconds = 5

    class _State:
        def __init__(self, key: str) -> None:
            self.key = key
            self.failure_count = 0
            self.last_success_at = 0.0
            self.last_used_at = 0.0
            self.last_error = ""

    monkeypatch.setattr(upstream, "_provider_configs", lambda: {"onemin": _Config()})
    monkeypatch.setattr(upstream, "_ordered_onemin_keys_allow_reserve", lambda allow_reserve: ("key-core", "key-image"))
    monkeypatch.setattr(upstream, "_onemin_states_snapshot", lambda keys: {key: _State(key) for key in keys})
    monkeypatch.setattr(upstream, "_onemin_reserve_keys", lambda: ())
    monkeypatch.setattr(upstream, "_onemin_key_state_label", lambda state, now=0.0: "ready")
    monkeypatch.setattr(upstream, "_now_epoch", lambda: 0.0)
    monkeypatch.setattr(upstream, "_provider_account_name", lambda provider, key_names, key: "acct-core" if key == "key-core" else "acct-image")
    monkeypatch.setattr(upstream, "_onemin_key_slot", lambda key, key_names=(): "ONEMIN_AI_API_KEY" if key == "key-core" else "ONEMIN_AI_API_KEY_FALLBACK_1")
    monkeypatch.setattr(upstream, "_onemin_slot_role_for_key", lambda key, active_keys=(), reserve_keys=(): "mixed")
    monkeypatch.setattr(
        upstream,
        "_provider_health_report",
        lambda: {
            "providers": {
                "onemin": {
                    "slots": [
                        {
                            "account_name": "acct-core",
                            "slot": "ONEMIN_AI_API_KEY",
                            "slot_env_name": "ONEMIN_AI_API_KEY",
                            "state": "ready",
                            "estimated_remaining_credits": 100000,
                        },
                        {
                            "account_name": "acct-image",
                            "slot": "ONEMIN_AI_API_KEY_FALLBACK_1",
                            "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_1",
                            "state": "ready",
                            "estimated_remaining_credits": 100000,
                        },
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(upstream, "_pick_onemin_key", lambda allow_reserve=False: (_ for _ in ()).throw(AssertionError("legacy picker should not be used")))
    monkeypatch.setattr(upstream, "_mark_onemin_request_start", lambda api_key: None)
    monkeypatch.setattr(upstream, "_mark_onemin_success", lambda api_key: None)
    monkeypatch.setattr(upstream, "_mark_onemin_failure", lambda api_key, detail, temporary_quarantine=False, quarantine_seconds=None: None)
    monkeypatch.setattr(upstream, "_record_onemin_usage_and_measure_delta", lambda **kwargs: (1200, "image_estimate"))
    monkeypatch.setattr(upstream, "_now_ms", lambda: 1000)
    monkeypatch.setattr(upstream, "_trim_error_payload", lambda payload: str(payload))
    monkeypatch.setattr(upstream, "_extract_onemin_error", lambda payload: "")
    monkeypatch.setattr(upstream, "_extract_onemin_model", lambda payload: str(payload.get("model") or "gpt-image-1-mini"))

    seen_headers: list[str] = []

    def _fake_post_json(*, url: str, headers: dict[str, str], payload: dict[str, object], timeout_seconds: int):
        seen_headers.append(str(headers.get("API-KEY") or ""))
        return 200, {
            "model": payload.get("model") or "gpt-image-1-mini",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "aiRecord": {
                "aiRecordDetail": {
                    "resultObject": {
                        "url": "https://cdn.1min.ai/generated/manager-image.png",
                    }
                }
            },
        }

    monkeypatch.setattr(upstream, "_post_json", _fake_post_json)

    core_lease = manager.reserve_for_candidates(
        candidates=[
            {
                "api_key": "key-core",
                "account_id": "acct-core",
                "account_name": "acct-core",
                "credential_id": "ONEMIN_AI_API_KEY",
                "slot_name": "ONEMIN_AI_API_KEY",
                "secret_env_name": "ONEMIN_AI_API_KEY",
                "slot_role": "mixed",
                "state": "ready",
                "estimated_remaining_credits": 100000,
                "failure_count": 0,
                "last_success_at": 0.0,
                "last_used_at": 0.0,
                "last_error": "",
            }
        ],
        lane="hard",
        capability="code_generate",
        principal_id="core-principal",
        request_id="req-core",
        estimated_credits=None,
        allow_reserve=False,
    )
    assert core_lease is not None

    adapter = OneminToolAdapter()
    payload, account_name, key_slot, resolved_model, tokens_in, tokens_out = adapter._call_feature(
        feature_payload={
            "type": "IMAGE_GENERATOR",
            "model": "gpt-image-1-mini",
            "promptObject": {"prompt": "render a banner"},
        },
        lane="hard",
        capability="image_generate",
        principal_id="image-principal",
    )

    assert seen_headers == ["key-image"]
    assert account_name == "acct-image"
    assert key_slot == "ONEMIN_AI_API_KEY_FALLBACK_1"
    assert resolved_model == "gpt-image-1-mini"
    assert tokens_in == 0
    assert tokens_out == 0
    assert payload["aiRecord"]["aiRecordDetail"]["resultObject"]["url"] == "https://cdn.1min.ai/generated/manager-image.png"
    assert manager.occupancy_snapshot()["active_lease_count"] == 1
    released_image_lease = next(row for row in manager.leases_snapshot() if row["capability"] == "image_generate")
    assert released_image_lease["actual_credits_delta"] == 1200
    assert released_image_lease["status"] == "released"

    register_onemin_manager(None)


def test_onemin_tool_adapter_image_request_can_start_on_reserve_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.domain.models import ToolDefinition
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services import responses_upstream as upstream
    from app.services.onemin_manager import OneminManagerService, register_onemin_manager
    from app.services.tool_execution_onemin_adapter import OneminToolAdapter

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    register_onemin_manager(manager)

    class _Config:
        api_keys = ("key-active", "key-reserve")
        timeout_seconds = 5

    class _State:
        def __init__(self, key: str) -> None:
            self.key = key
            self.failure_count = 0
            self.last_success_at = 0.0
            self.last_used_at = 0.0
            self.last_error = ""

    monkeypatch.setattr(upstream, "_provider_configs", lambda: {"onemin": _Config()})
    monkeypatch.setattr(
        upstream,
        "_ordered_onemin_keys_allow_reserve",
        lambda allow_reserve: ("key-active",) if not allow_reserve else ("key-active", "key-reserve"),
    )
    monkeypatch.setattr(upstream, "_onemin_states_snapshot", lambda keys: {key: _State(key) for key in keys})
    monkeypatch.setattr(upstream, "_onemin_reserve_keys", lambda: ("key-reserve",))
    monkeypatch.setattr(upstream, "_onemin_key_state_label", lambda state, now=0.0: "ready")
    monkeypatch.setattr(upstream, "_now_epoch", lambda: 0.0)
    monkeypatch.setattr(
        upstream,
        "_provider_account_name",
        lambda provider, key_names, key: "acct-active" if key == "key-active" else "acct-reserve",
    )
    monkeypatch.setattr(
        upstream,
        "_onemin_key_slot",
        lambda key, key_names=(): "ONEMIN_AI_API_KEY" if key == "key-active" else "ONEMIN_AI_API_KEY_FALLBACK_9",
    )
    monkeypatch.setattr(
        upstream,
        "_onemin_slot_role_for_key",
        lambda key, active_keys=(), reserve_keys=(): "image" if key == "key-active" else "reserve",
    )
    monkeypatch.setattr(
        upstream,
        "_provider_health_report",
        lambda: {
            "providers": {
                "onemin": {
                    "slots": [
                        {
                            "account_name": "acct-active",
                            "slot": "ONEMIN_AI_API_KEY",
                            "slot_env_name": "ONEMIN_AI_API_KEY",
                            "slot_role": "image",
                            "state": "degraded",
                            "estimated_remaining_credits": 730,
                        },
                        {
                            "account_name": "acct-reserve",
                            "slot": "ONEMIN_AI_API_KEY_FALLBACK_9",
                            "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_9",
                            "slot_role": "reserve",
                            "state": "ready",
                            "estimated_remaining_credits": 100000,
                        },
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(upstream, "_pick_onemin_key", lambda allow_reserve=False: (_ for _ in ()).throw(AssertionError("legacy picker should not be used")))
    monkeypatch.setattr(upstream, "_mark_onemin_request_start", lambda api_key: None)
    monkeypatch.setattr(upstream, "_mark_onemin_success", lambda api_key: None)
    monkeypatch.setattr(upstream, "_mark_onemin_failure", lambda api_key, detail, temporary_quarantine=False, quarantine_seconds=None: None)
    monkeypatch.setattr(upstream, "_record_onemin_usage_and_measure_delta", lambda **kwargs: (1887, "image_estimate"))
    monkeypatch.setattr(upstream, "_now_ms", lambda: 1000)
    monkeypatch.setattr(upstream, "_trim_error_payload", lambda payload: str(payload))
    monkeypatch.setattr(upstream, "_extract_onemin_error", lambda payload: "")
    monkeypatch.setattr(upstream, "_extract_onemin_model", lambda payload: str(payload.get("model") or "gpt-image-1-mini"))

    seen_headers: list[str] = []

    def _fake_post_json(*, url: str, headers: dict[str, str], payload: dict[str, object], timeout_seconds: int):
        seen_headers.append(str(headers.get("API-KEY") or ""))
        return 200, {
            "model": payload.get("model") or "gpt-image-1-mini",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "aiRecord": {
                "aiRecordDetail": {
                    "resultObject": {
                        "url": "https://cdn.1min.ai/generated/reserve-first-image.png",
                    }
                }
            },
        }

    monkeypatch.setattr(upstream, "_post_json", _fake_post_json)

    adapter = OneminToolAdapter()
    result = adapter.execute_image_generate(
        ToolInvocationRequest(
            session_id="session-onemin-image-reserve",
            step_id="step-onemin-image-reserve",
            tool_name="provider.onemin.image_generate",
            action_kind="image.generate",
            payload_json={
                "prompt": "Render a receipt-heavy skyline banner.",
                "size": "1536x1024",
                "manager_allow_reserve": True,
            },
            context_json={"principal_id": "image-principal"},
        ),
        ToolDefinition(
            tool_name="provider.onemin.image_generate",
            version="v1",
            input_schema_json={},
            output_schema_json={},
            policy_json={"builtin": True, "action_kind": "image.generate"},
            allowed_channels=(),
            approval_default="none",
            enabled=True,
            updated_at="2026-03-23T00:00:00Z",
        ),
    )

    assert seen_headers == ["key-reserve"]
    assert result.output_json["provider_account_name"] == "acct-reserve"
    assert result.output_json["provider_key_slot"] == "ONEMIN_AI_API_KEY_FALLBACK_9"
    assert result.output_json["asset_urls"] == ["https://cdn.1min.ai/generated/reserve-first-image.png"]
    released_image_lease = next(row for row in manager.leases_snapshot() if row["capability"] == "image_generate")
    assert released_image_lease["actual_credits_delta"] == 1887
    assert released_image_lease["status"] == "released"

    register_onemin_manager(None)


def test_onemin_tool_adapter_releases_manager_lease_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services import responses_upstream as upstream
    from app.services.onemin_manager import OneminManagerService, register_onemin_manager
    from app.services.tool_execution_onemin_adapter import OneminToolAdapter

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    register_onemin_manager(manager)

    class _Config:
        api_keys = ("key-image",)
        timeout_seconds = 1

    class _State:
        def __init__(self, key: str) -> None:
            self.key = key
            self.failure_count = 0
            self.last_success_at = 0.0
            self.last_used_at = 0.0
            self.last_error = ""

    monkeypatch.setattr(upstream, "_provider_configs", lambda: {"onemin": _Config()})
    monkeypatch.setattr(upstream, "_ordered_onemin_keys_allow_reserve", lambda allow_reserve: ("key-image",))
    monkeypatch.setattr(upstream, "_onemin_states_snapshot", lambda keys: {key: _State(key) for key in keys})
    monkeypatch.setattr(upstream, "_onemin_reserve_keys", lambda: ())
    monkeypatch.setattr(upstream, "_onemin_key_state_label", lambda state, now=0.0: "ready")
    monkeypatch.setattr(upstream, "_now_epoch", lambda: 0.0)
    monkeypatch.setattr(upstream, "_provider_account_name", lambda provider, key_names, key: "acct-image")
    monkeypatch.setattr(upstream, "_onemin_key_slot", lambda key, key_names=(): "ONEMIN_AI_API_KEY_FALLBACK_1")
    monkeypatch.setattr(upstream, "_onemin_slot_role_for_key", lambda key, active_keys=(), reserve_keys=(): "mixed")
    monkeypatch.setattr(
        upstream,
        "_provider_health_report",
        lambda: {
            "providers": {
                "onemin": {
                    "slots": [
                        {
                            "account_name": "acct-image",
                            "slot": "ONEMIN_AI_API_KEY_FALLBACK_1",
                            "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_1",
                            "state": "ready",
                            "estimated_remaining_credits": 100000,
                        },
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(upstream, "_pick_onemin_key", lambda allow_reserve=False: (_ for _ in ()).throw(AssertionError("legacy picker should not be used")))
    monkeypatch.setattr(upstream, "_mark_onemin_request_start", lambda api_key: None)
    monkeypatch.setattr(upstream, "_mark_onemin_success", lambda api_key: None)
    monkeypatch.setattr(upstream, "_mark_onemin_failure", lambda api_key, detail, temporary_quarantine=False, quarantine_seconds=None: None)

    def _boom_post_json(*, url: str, headers: dict[str, str], payload: dict[str, object], timeout_seconds: int):
        raise upstream.ResponsesUpstreamError("request_timeout:1s")

    monkeypatch.setattr(upstream, "_post_json", _boom_post_json)

    adapter = OneminToolAdapter()
    with pytest.raises(ToolExecutionError, match="onemin_feature_failed:ONEMIN_AI_API_KEY_FALLBACK_1:request_timeout:1s"):
        adapter._call_feature(
            feature_payload={
                "type": "IMAGE_GENERATOR",
                "model": "gpt-image-1-mini",
                "promptObject": {"prompt": "render a banner"},
            },
            lane="hard",
            capability="image_generate",
            principal_id="image-principal",
        )

    leases = manager.leases_snapshot()
    assert len(leases) == 1
    assert leases[0]["status"] == "failed"
    assert leases[0]["finished_at"]
    assert manager.occupancy_snapshot()["active_lease_count"] == 0

    register_onemin_manager(None)


def test_onemin_property_walkthrough_auth_failure_quarantines_key_and_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services import responses_upstream as upstream
    from app.services.onemin_manager import OneminManagerService, register_onemin_manager
    from app.services.tool_execution_onemin_adapter import OneminToolAdapter

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    register_onemin_manager(manager)

    class _Config:
        api_keys = ("key-video",)
        timeout_seconds = 1

    class _State:
        def __init__(self, key: str) -> None:
            self.key = key
            self.failure_count = 0
            self.last_success_at = 0.0
            self.last_used_at = 0.0
            self.last_error = ""

    monkeypatch.setattr(upstream, "_provider_configs", lambda: {"onemin": _Config()})
    monkeypatch.setattr(upstream, "_ordered_onemin_keys_allow_reserve", lambda allow_reserve: ("key-video",))
    monkeypatch.setattr(upstream, "_onemin_states_snapshot", lambda keys: {key: _State(key) for key in keys})
    monkeypatch.setattr(upstream, "_onemin_reserve_keys", lambda: ())
    monkeypatch.setattr(upstream, "_onemin_key_state_label", lambda state, now=0.0: "ready")
    monkeypatch.setattr(upstream, "_now_epoch", lambda: 0.0)
    monkeypatch.setattr(upstream, "_provider_account_name", lambda provider, key_names, key: "acct-video")
    monkeypatch.setattr(upstream, "_onemin_key_slot", lambda key, key_names=(): "ONEMIN_AI_API_KEY")
    monkeypatch.setattr(upstream, "_onemin_slot_role_for_key", lambda key, active_keys=(), reserve_keys=(): "mixed")
    monkeypatch.setattr(
        upstream,
        "_provider_health_report",
        lambda: {
            "providers": {
                "onemin": {
                    "slots": [
                        {
                            "account_name": "acct-video",
                            "slot": "ONEMIN_AI_API_KEY",
                            "slot_env_name": "ONEMIN_AI_API_KEY",
                            "state": "ready",
                            "estimated_remaining_credits": 2_000_000,
                        },
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(upstream, "_onemin_code_url", lambda: "https://api.1min.test/api/features")
    monkeypatch.setattr(upstream, "_trim_error_payload", lambda payload: "deleted_api_key")
    monkeypatch.setattr(upstream, "_extract_onemin_error", lambda payload: "")
    monkeypatch.setattr(upstream, "_is_auth_error", lambda payload: "deleted" in str(payload) or "auth" in str(payload))
    monkeypatch.setattr(upstream, "_is_deleted_onemin_key_error", lambda payload: "deleted" in str(payload))
    monkeypatch.setattr(upstream, "_deleted_onemin_key_quarantine_seconds", lambda: 86_400)

    failures: list[tuple[str, str, bool, int | None]] = []

    def _mark_failure(api_key: str, detail: str, temporary_quarantine: bool = False, quarantine_seconds: int | None = None) -> None:
        failures.append((api_key, detail, temporary_quarantine, quarantine_seconds))

    monkeypatch.setattr(upstream, "_mark_onemin_failure", _mark_failure)

    class _Response:
        status_code = 401
        text = "deleted api key"

        def json(self) -> dict[str, str]:
            return {"error": "deleted api key"}

    monkeypatch.setattr(
        "app.services.tool_execution_onemin_adapter.requests.post",
        lambda *args, **kwargs: _Response(),
    )

    adapter = OneminToolAdapter()
    with pytest.raises(ToolExecutionError, match="onemin_property_walkthrough_video_failed"):
        adapter._call_property_walkthrough_feature(
            first_frame_path="",
            image_url="https://cdn.example.test/frame.jpg",
            feature_model="kling",
            prompt_object={"prompt": "slow premium apartment walkthrough"},
            principal_id="property-user",
            allow_reserve=False,
            timeout_seconds=1,
        )

    assert failures == [("key-video", "deleted_api_key", True, 86_400)]
    leases = manager.leases_snapshot()
    assert len(leases) == 1
    assert leases[0]["status"] == "failed"
    assert leases[0]["finished_at"]
    assert manager.occupancy_snapshot()["active_lease_count"] == 0

    register_onemin_manager(None)


def test_onemin_property_walkthrough_gateway_timeout_does_not_retry_other_accounts_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services import responses_upstream as upstream
    from app.services.onemin_manager import OneminManagerService, register_onemin_manager
    from app.services.tool_execution_onemin_adapter import OneminToolAdapter

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    register_onemin_manager(manager)

    class _Config:
        api_keys = ("key-video-1", "key-video-2")
        timeout_seconds = 1

    class _State:
        def __init__(self, key: str) -> None:
            self.key = key
            self.failure_count = 0
            self.last_success_at = 0.0
            self.last_used_at = 0.0
            self.last_error = ""

    monkeypatch.delenv("PROPERTYQUARRY_ONEMIN_VIDEO_RETRY_GATEWAY_TIMEOUTS", raising=False)
    monkeypatch.setattr(upstream, "_provider_configs", lambda: {"onemin": _Config()})
    monkeypatch.setattr(upstream, "_ordered_onemin_keys_allow_reserve", lambda allow_reserve: ("key-video-1", "key-video-2"))
    monkeypatch.setattr(upstream, "_onemin_states_snapshot", lambda keys: {key: _State(key) for key in keys})
    monkeypatch.setattr(upstream, "_onemin_reserve_keys", lambda: ())
    monkeypatch.setattr(upstream, "_onemin_key_state_label", lambda state, now=0.0: "ready")
    monkeypatch.setattr(upstream, "_now_epoch", lambda: 0.0)
    monkeypatch.setattr(upstream, "_provider_account_name", lambda provider, key_names, key: f"acct-{key[-1]}")
    monkeypatch.setattr(upstream, "_onemin_key_slot", lambda key, key_names=(): f"fallback_3{key[-1]}")
    monkeypatch.setattr(upstream, "_onemin_slot_role_for_key", lambda key, active_keys=(), reserve_keys=(): "mixed")
    monkeypatch.setattr(
        upstream,
        "_provider_health_report",
        lambda: {
            "providers": {
                "onemin": {
                    "slots": [
                        {
                            "account_name": "acct-1",
                            "slot": "fallback_31",
                            "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_31",
                            "state": "ready",
                            "estimated_remaining_credits": 2_000_000,
                        },
                        {
                            "account_name": "acct-2",
                            "slot": "fallback_32",
                            "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_32",
                            "state": "ready",
                            "estimated_remaining_credits": 2_000_000,
                        },
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(upstream, "_onemin_code_url", lambda: "https://api.1min.test/api/features")
    monkeypatch.setattr(upstream, "_trim_error_payload", lambda payload: "cloudflare_timeout")
    monkeypatch.setattr(upstream, "_extract_onemin_error", lambda payload: "")
    monkeypatch.setattr(upstream, "_is_auth_error", lambda payload: False)
    monkeypatch.setattr(upstream, "_is_deleted_onemin_key_error", lambda payload: False)
    monkeypatch.setattr(upstream, "_deleted_onemin_key_quarantine_seconds", lambda: 86_400)
    monkeypatch.setattr(upstream, "_mark_onemin_failure", lambda *args, **kwargs: None)

    calls: list[str] = []

    class _Response:
        status_code = 524
        text = "gateway timeout"

        def json(self) -> dict[str, str]:
            return {"error": "gateway timeout"}

    def _fake_post(*args, **kwargs):
        calls.append(str((kwargs.get("headers") or {}).get("API-KEY") or ""))
        return _Response()

    monkeypatch.setattr("app.services.tool_execution_onemin_adapter.requests.post", _fake_post)

    adapter = OneminToolAdapter()
    with pytest.raises(ToolExecutionError, match="http_524"):
        adapter._call_property_walkthrough_feature(
            first_frame_path="",
            image_url="https://cdn.example.test/frame.jpg",
            feature_model="pika",
            prompt_object={"prompt": "short runsite fight scene", "duration": 5},
            principal_id="property-user",
            allow_reserve=False,
            timeout_seconds=1,
        )

    assert calls == ["key-video-1"]
    leases = manager.leases_snapshot()
    assert len(leases) == 1
    assert leases[0]["status"] == "failed"
    assert manager.occupancy_snapshot()["active_lease_count"] == 0

    register_onemin_manager(None)


def test_tool_execution_service_detects_gemini_web_human_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = InMemoryToolRegistryRepository()
    tool_runtime = ToolRuntimeService(
        tool_registry=registry,
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )
    binding = tool_runtime.upsert_connector_binding(
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        status="enabled",
    )

    def _fake_generate(**_: object) -> dict[str, object]:
        return {
            "page_title": "Just a moment...",
            "visible_text": "Verify you are human",
            "requested_url": "https://gemini.google.com/app",
        }

    monkeypatch.setattr(service, "_browseract_gemini_web_generate", _fake_generate)

    with pytest.raises(ToolExecutionError, match="ui_lane_failure:gemini_web:challenge_required"):
        service.execute_invocation(
            ToolInvocationRequest(
                session_id="session-browseract-gemini-web-challenge",
                step_id="step-browseract-gemini-web-challenge",
                tool_name="browseract.gemini_web_generate",
                action_kind="content.generate",
                payload_json={
                    "binding_id": binding.binding_id,
                    "packet": {
                        "objective": "Answer the question",
                        "instructions": "Be concise",
                        "condensed_history": "Earlier context",
                        "current_input": "What is the next step?",
                        "desired_format": "plain_text",
                        "fingerprint": "abc123",
                    },
                },
                context_json={"principal_id": "exec-1"},
            )
        )


def test_tool_execution_service_builds_browseract_workflow_spec_packets() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-spec-1",
            step_id="step-browseract-spec-1",
            tool_name="browseract.build_workflow_spec",
            action_kind="workflow.spec_build",
            payload_json={
                "workflow_name": "Prompt Forge",
                "purpose": "Build a prepared BrowserAct workflow spec for prompt refinement.",
                "login_url": "https://browseract.example/login",
                "tool_url": "https://browseract.example/tools/prompting-systems",
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.build_workflow_spec"
    assert result.output_json["mime_type"] == "application/json"
    assert result.output_json["structured_output_json"]["workflow_name"] == "Prompt Forge"
    assert result.output_json["structured_output_json"]["meta"]["slug"] == "prompt_forge"
    nodes = result.output_json["structured_output_json"]["nodes"]
    assert [node["id"] for node in nodes[-3:]] == ["wait_result", "extract_result", "output_result"]
    assert next(node for node in nodes if node["id"] == "extract_result")["config"]["field_name"] == "result_text"
    assert result.receipt_json["handler_key"] == "browseract.build_workflow_spec"
    assert tool_runtime.get_tool("browseract.build_workflow_spec") is not None


def test_tool_execution_service_builds_page_extract_browseract_packets() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-spec-2",
            step_id="step-browseract-spec-2",
            tool_name="browseract.build_workflow_spec",
            action_kind="workflow.spec_build",
            payload_json={
                "workflow_name": "Economist Reader",
                "purpose": "Open a logged-in Economist article and extract the readable title and body.",
                "login_url": "https://www.economist.com/login",
                "tool_url": "https://www.economist.com",
                "workflow_kind": "page_extract",
                "runtime_input_name": "article_url",
                "wait_selector": "article",
                "title_selector": "article h1",
                "result_selector": "article",
                "dismiss_selectors": ["button[aria-label='Close']"],
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    spec = result.output_json["structured_output_json"]
    assert spec["meta"]["workflow_kind"] == "page_extract"
    assert spec["inputs"][0]["name"] == "article_url"
    open_tool = next(node for node in spec["nodes"] if node["id"] == "open_tool")
    assert open_tool["type"] == "visit_page"
    assert open_tool["config"]["value_from_input"] == "article_url"
    assert any(node["id"] == "extract_title" for node in spec["nodes"])
    assert any(node["id"] == "extract_result" for node in spec["nodes"])
    assert any(node["id"] == "output_result" for node in spec["nodes"])
    assert next(node for node in spec["nodes"] if node["id"] == "extract_result")["config"]["field_name"] == "page_body"
    assert "Kind: page_extract" in result.output_json["normalized_text"]


def test_tool_execution_service_builds_explicit_browseract_workflow_spec_packets() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-spec-explicit-1",
            step_id="step-browseract-spec-explicit-1",
            tool_name="browseract.build_workflow_spec",
            action_kind="workflow.spec_build",
            payload_json={
                "workflow_name": "1min Billing Usage Reader",
                "purpose": "Capture billing settings, usage rows, and free-credit unlock state.",
                "login_url": "https://app.1min.ai/login",
                "tool_url": "https://app.1min.ai/billing-usage",
                "workflow_kind": "page_extract",
                "workflow_spec_json": {
                    "publish": True,
                    "mcp_ready": False,
                    "inputs": [
                        {"name": "browseract_username", "description": "Login email"},
                        {"name": "browseract_password", "description": "Login password"},
                    ],
                    "nodes": [
                        {"id": "open_login", "type": "visit_page", "label": "Open Login", "config": {"url": "https://app.1min.ai/login"}},
                        {"id": "open_login_modal", "type": "click", "label": "Open Login Modal", "config": {"selector": "button:has-text(\"Log In\")"}},
                        {"id": "output_result", "type": "output", "label": "Output Result", "config": {"field_name": "unlock_free_credits_surface"}},
                    ],
                    "edges": [
                        ["open_login", "open_login_modal"],
                        ["open_login_modal", "output_result"],
                    ],
                },
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    spec = result.output_json["structured_output_json"]
    assert spec["workflow_name"] == "1min Billing Usage Reader"
    assert spec["meta"]["workflow_kind"] == "page_extract"
    assert spec["inputs"][0]["name"] == "browseract_username"
    assert [node["id"] for node in spec["nodes"]] == ["open_login", "open_login_modal", "output_result"]
    assert spec["edges"] == [["open_login", "open_login_modal"], ["open_login_modal", "output_result"]]


def test_tool_execution_service_repairs_browseract_workflow_spec_packets(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {
                    "response": json.dumps(
                        {
                            "diagnosis": "BrowserAct typed the runtime placeholder literally.",
                            "repair_strategy": "Restore value_from_input on the input_text node and keep result extraction compact.",
                            "operator_checks": [
                                "Confirm the input_text node uses value_from_input text.",
                                "Confirm the output still extracts the main humanized result.",
                            ],
                            "workflow_spec": {
                                "workflow_name": "Undetectable Humanizer",
                                "description": "Repair the humanizer workflow after a literal input binding failure.",
                                "publish": True,
                                "mcp_ready": False,
                                "nodes": [
                                    {
                                        "id": "open_tool",
                                        "type": "visit_page",
                                        "config": {"url": "https://undetectable.ai/ai-humanizer"},
                                    },
                                    {
                                        "id": "input_text",
                                        "type": "input_text",
                                        "config": {
                                            "selector": "textarea[aria-label='Input text']",
                                            "value_from_input": "text",
                                        },
                                    },
                                ],
                                "edges": [["open_tool", "input_text"]],
                                "meta": {"slug": "undetectable_humanizer_live"},
                            },
                        }
                    ),
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.tool_execution_browseract_adapter.subprocess.run",
        fake_run,
    )

    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    service = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
    )

    result = service.execute_invocation(
        ToolInvocationRequest(
            session_id="session-browseract-repair-1",
            step_id="step-browseract-repair-1",
            tool_name="browseract.repair_workflow_spec",
            action_kind="workflow.spec_repair",
            payload_json={
                "workflow_name": "Undetectable Humanizer",
                "purpose": "Repair the humanizer workflow after a literal input binding failure.",
                "tool_url": "https://undetectable.ai/ai-humanizer",
                "failure_summary": "browseract:literal_input_binding:/text",
                "failing_step_goals": ['Input "/text" into the main textarea'],
                "current_workflow_spec_json": {
                    "workflow_name": "Undetectable Humanizer",
                    "nodes": [{"id": "input_text", "type": "input_text", "config": {"value": "/text"}}],
                    "edges": [["open_tool", "input_text"]],
                },
            },
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "browseract.repair_workflow_spec"
    assert result.output_json["mime_type"] == "application/json"
    assert result.output_json["structured_output_json"]["workflow_spec"]["meta"]["repair_source"] == "gemini_vortex"
    assert result.output_json["structured_output_json"]["workflow_spec"]["nodes"][1]["config"]["value_from_input"] == "text"
    assert result.receipt_json["handler_key"] == "browseract.repair_workflow_spec"
    assert tool_runtime.get_tool("browseract.repair_workflow_spec") is not None


def test_rewrite_orchestrator_without_explicit_tool_runtime_does_not_hide_in_memory_fallback() -> None:
    orchestrator = RewriteOrchestrator()

    with pytest.raises(RuntimeError, match="tool_execution_unconfigured"):
        orchestrator._tool_execution.execute_invocation(  # type: ignore[attr-defined]
            ToolInvocationRequest(
                session_id="session-unconfigured-tool-1",
                step_id="step-unconfigured-tool-1",
                tool_name="artifact_repository",
                action_kind="artifact.save",
                payload_json={"source_text": "draft note"},
                context_json={"principal_id": "exec-1"},
            )
        )


def test_build_default_orchestrator_uses_explicit_tool_execution_for_tool_execution() -> None:
    tool_runtime = ToolRuntimeService(
        tool_registry=InMemoryToolRegistryRepository(),
        connector_bindings=InMemoryConnectorBindingRepository(),
    )
    tool_execution = _tool_execution_service(
        tool_runtime=tool_runtime,
        artifacts=InMemoryArtifactRepository(),
        evidence_runtime=EvidenceRuntimeService(InMemoryEvidenceObjectRepository()),
    )

    orchestrator = build_default_orchestrator(
        artifacts=InMemoryArtifactRepository(),
        evidence_runtime=EvidenceRuntimeService(InMemoryEvidenceObjectRepository()),
        tool_execution=tool_execution,
    )

    result = orchestrator._tool_execution.execute_invocation(  # type: ignore[attr-defined]
        ToolInvocationRequest(
            session_id="session-builder-tool-1",
            step_id="step-builder-tool-1",
            tool_name="artifact_repository",
            action_kind="artifact.save",
            payload_json={"source_text": "built with explicit tool runtime"},
            context_json={"principal_id": "exec-1"},
        )
    )

    assert result.tool_name == "artifact_repository"
    assert tool_runtime.get_tool("artifact_repository") is not None


def test_build_default_orchestrator_without_explicit_tool_runtime_keeps_tool_execution_unconfigured() -> None:
    orchestrator = build_default_orchestrator(
        artifacts=InMemoryArtifactRepository(),
        evidence_runtime=EvidenceRuntimeService(InMemoryEvidenceObjectRepository()),
    )

    with pytest.raises(RuntimeError, match="tool_execution_unconfigured"):
        orchestrator._tool_execution.execute_invocation(  # type: ignore[attr-defined]
            ToolInvocationRequest(
                session_id="session-builder-unconfigured-tool-1",
                step_id="step-builder-unconfigured-tool-1",
                tool_name="artifact_repository",
                action_kind="artifact.save",
                payload_json={"source_text": "should stay unconfigured"},
                context_json={"principal_id": "exec-1"},
            )
        )
