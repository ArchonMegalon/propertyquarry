from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from app.domain.models import ToolInvocationResult
from app.services.ltd_runtime_catalog import LtdRuntimeCatalogService


def _sample_ltd_markdown() -> str:
    return """
# LTDs

Updated: 2026-05-02

## Non-AppSumo / Other LTDs

| Service | Plan / Tier | Holding | Status | Redeem By | Workspace Integration Tier | Local Integration | Notes |
|---|---|---|---|---|---|---|---|
| `1min.AI` | `Advanced Business Plan` | `12 licenses` | `Owned` |  | `Tier 1` | Local `.env` key rotation slots | Primary API-key lane is already wired. |
| `Emailit` | `Tier 5` | `1 key` | `Owned` |  | `Tier 1` | Local `.env` key plus sender-domain wiring | Transactional delivery already runs through EA. |

## AppSumo LTDs

| Service | Plan / Tier | Holding | Status | Redeem By | Workspace Integration Tier | Local Integration | Notes |
|---|---|---|---|---|---|---|---|
| `Documentation.AI` | `License Tier 3` | `1 license` | `Activated` |  | `Tier 4` | Local `.env` username/password only | Owned for operator docs and cited answers. |
| `FlipLink.me` | `Tier 10` | `1 account` | `Owned` |  | `Tier 2` | Local `.env` credentials plus bounded PropertyQuarry review-packet flipbook lane | Use only for shareable redacted review packets downstream of PropertyQuarry. |
| `MarkupGo` | `7x code-based` | `7 codes` | `Activated` |  | `Tier 3` | None | BrowserAct workspace reader exists even though the direct provider lane is not executable. |
""".strip()


def _client(*, principal_id: str = "ops-1") -> TestClient:
    os.environ["EA_STORAGE_BACKEND"] = "memory"
    os.environ["EA_API_TOKEN"] = "test-token"
    os.environ["EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER"] = "1"
    os.environ["EA_OPERATOR_PRINCIPAL_IDS"] = principal_id
    os.environ["PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES"] = "1"
    from app.api.app import create_app

    client = TestClient(create_app())
    client.headers.update({"Authorization": "Bearer test-token"})
    client.headers.update({"X-EA-Principal-ID": principal_id})
    return client


def _patch_catalog(monkeypatch: pytest.MonkeyPatch, client: TestClient, tmp_path: Path) -> None:
    markdown_path = tmp_path / "LTDs.md"
    markdown_path.write_text(_sample_ltd_markdown(), encoding="utf-8")
    from app.api.routes import ltd_runtime as ltd_runtime_route

    monkeypatch.setattr(
        ltd_runtime_route,
        "_catalog",
        lambda container: LtdRuntimeCatalogService(
            provider_registry=container.provider_registry,
            markdown_path=markdown_path,
        ),
    )


def _verified_tool_receipt(request, **extra: object) -> dict[str, object]:  # noqa: ANN001
    return {
        "principal_id": request.context_json["principal_id"],
        "handler_key": request.tool_name,
        "invocation_contract": "tool.v1",
        "tool_version": "test-v1",
        **extra,
    }


def test_ltd_runtime_catalog_route_lists_profiles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _client()
    _patch_catalog(monkeypatch, client, tmp_path)

    response = client.get("/v1/ltds/runtime-catalog")
    assert response.status_code == 200
    body = response.json()
    service_names = {row["service_name"] for row in body}
    assert {"1min.AI", "Documentation.AI", "Emailit", "FlipLink.me", "MarkupGo"} <= service_names

    documentation = next(row for row in body if row["service_name"] == "Documentation.AI")
    assert documentation["runtime_state"] == "browseract_template_available"
    assert documentation["evidence_status"] == "missing"
    assert documentation["live_evidence_verified"] is False
    assert documentation["propertyquarry_customer_integration_verified"] is False
    assert {action["action_key"] for action in documentation["actions"]} == {
        "discover_account",
        "inspect_workspace",
    }

    fliplink = next(row for row in body if row["service_name"] == "FlipLink.me")
    assert fliplink["runtime_state"] == "runtime_contract_available"
    assert fliplink["live_evidence_verified"] is False
    assert fliplink["propertyquarry_customer_integration_verified"] is False
    assert {action["action_key"] for action in fliplink["actions"]} == {
        "discover_account",
        "publish_property_flipbook",
    }


def test_ltd_runtime_discover_account_executes_browseract_extract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _client(principal_id="ops-discover")
    _patch_catalog(monkeypatch, client, tmp_path)

    captured: list[object] = []

    def _fake_execute(request):  # noqa: ANN001
        captured.append(request)
        return ToolInvocationResult(
            tool_name=request.tool_name,
            action_kind=request.action_kind,
            target_ref="browseract:binding-browseract-1:markupgo",
            output_json={"service_name": request.payload_json["service_name"]},
            receipt_json=_verified_tool_receipt(request),
        )

    monkeypatch.setattr(client.app.state.container.tool_execution, "execute_invocation", _fake_execute)

    response = client.post(
        "/v1/ltds/runtime-catalog/MarkupGo/discover-account",
        json={
            "binding_id": "binding-browseract-1",
            "requested_fields": ["tier", "account_email"],
            "instructions": "Verify account facts",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tool_name"] == "browseract.extract_account_facts"
    assert body["output_json"]["service_name"] == "MarkupGo"
    assert body["receipt_json"]["status"] == "verified"
    assert body["receipt_json"]["principal_bound"] is True
    assert body["receipt_json"]["proof_scope"] == "principal_bound_tool_invocation"
    assert "principal_id" not in body["receipt_json"]
    assert body["target_ref"] == "browseract:discover_account"
    assert "binding-browseract-1" not in body["target_ref"]
    request = captured[0]
    assert request.tool_name == "browseract.extract_account_facts"
    assert request.payload_json["binding_id"] == "binding-browseract-1"
    assert request.payload_json["requested_fields"] == ["tier", "account_email"]
    assert request.payload_json["service_name"] == "MarkupGo"
    assert request.context_json["principal_id"] == "ops-discover"


def test_ltd_runtime_inspect_workspace_executes_browseract_ui_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _client(principal_id="ops-inspect")
    _patch_catalog(monkeypatch, client, tmp_path)

    captured: list[object] = []

    def _fake_execute(request):  # noqa: ANN001
        captured.append(request)
        return ToolInvocationResult(
            tool_name=request.tool_name,
            action_kind=request.action_kind,
            target_ref="browseract:binding-browseract-2:documentation-ai",
            output_json={"requested_url": request.payload_json["page_url"]},
            receipt_json=_verified_tool_receipt(
                request,
                requested_url=request.payload_json["page_url"],
                workflow_id="internal-workflow-1",
            ),
        )

    monkeypatch.setattr(client.app.state.container.tool_execution, "execute_invocation", _fake_execute)

    response = client.post(
        "/v1/ltds/runtime-catalog/Documentation.AI/inspect-workspace",
        json={
            "binding_id": "binding-browseract-2",
            "page_url": "https://docs.example/workspace",
            "result_title": "Documentation AI Workspace",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tool_name"] == "browseract.documentation_ai_workspace_reader"
    assert body["action_key"] == "inspect_workspace"
    assert body["receipt_json"]["proof_scope"] == "browser_session_call"
    assert "requested_url" not in body["output_json"]
    assert "workflow_id" not in body["receipt_json"]
    assert body["target_ref"] == "browseract:inspect_workspace"
    request = captured[0]
    assert request.tool_name == "browseract.documentation_ai_workspace_reader"
    assert request.payload_json["page_url"] == "https://docs.example/workspace"
    assert request.context_json["principal_id"] == "ops-inspect"


def test_ltd_runtime_rejects_non_executable_runtime_managed_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _client()
    _patch_catalog(monkeypatch, client, tmp_path)

    response = client.post(
        "/v1/ltds/runtime-catalog/Emailit/actions/delivery_outbox",
        json={},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ltd_runtime_action_not_executable"


def test_ltd_runtime_executes_direct_provider_action(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _client(principal_id="ops-onemin")
    _patch_catalog(monkeypatch, client, tmp_path)

    captured: list[object] = []

    def _fake_execute(request):  # noqa: ANN001
        captured.append(request)
        return ToolInvocationResult(
            tool_name=request.tool_name,
            action_kind=request.action_kind,
            target_ref="provider://onemin/code",
            output_json={
                "language": request.payload_json["language"],
                "provider_account_name": "internal-account-label",
                "provider_key_slot": "ONEMIN_AI_API_KEY",
                "structured_output_json": {
                    "raw_response": {"internal": True},
                    "result": "generated code",
                },
            },
            receipt_json=_verified_tool_receipt(
                request,
                provider_key="onemin",
                provider_backend="1min",
                provider_account_name="internal-account-label",
                provider_key_slot="ONEMIN_AI_API_KEY",
                model="code-model",
            ),
        )

    monkeypatch.setattr(client.app.state.container.tool_execution, "execute_invocation", _fake_execute)

    response = client.post(
        "/v1/ltds/runtime-catalog/1min.AI/actions/code_generate",
        json={
            "prompt": "Create a small CLI",
            "language": "python",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tool_name"] == "provider.onemin.code_generate"
    assert body["receipt_json"]["proof_scope"] == "provider_call"
    assert body["receipt_json"]["provider_key"] == "onemin"
    assert "provider_account_name" not in body["receipt_json"]
    assert "provider_key_slot" not in body["receipt_json"]
    assert "provider_account_name" not in body["output_json"]
    assert "provider_key_slot" not in body["output_json"]
    assert "raw_response" not in body["output_json"]["structured_output_json"]
    request = captured[0]
    assert request.tool_name == "provider.onemin.code_generate"
    assert request.payload_json["prompt"] == "Create a small CLI"
    assert request.payload_json["language"] == "python"
    assert request.context_json["principal_id"] == "ops-onemin"


def test_ltd_runtime_executes_specialized_onemin_background_remove_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _client(principal_id="ops-onemin-media")
    _patch_catalog(monkeypatch, client, tmp_path)

    captured: list[object] = []

    def _fake_execute(request):  # noqa: ANN001
        captured.append(request)
        return ToolInvocationResult(
            tool_name=request.tool_name,
            action_kind=request.action_kind,
            target_ref="provider://onemin/background-remove",
            output_json={"feature_type": request.payload_json["feature_type"]},
            receipt_json=_verified_tool_receipt(
                request,
                provider_key="onemin",
                provider_backend="1min",
                feature_type=request.payload_json["feature_type"],
                model="image-model",
            ),
        )

    monkeypatch.setattr(client.app.state.container.tool_execution, "execute_invocation", _fake_execute)

    response = client.post(
        "/v1/ltds/runtime-catalog/1min.AI/actions/background_remove",
        json={
            "image_url": "https://example.invalid/notebook.png",
            "output_format": "png",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tool_name"] == "provider.onemin.media_transform"
    request = captured[0]
    assert request.tool_name == "provider.onemin.media_transform"
    assert request.payload_json["feature_type"] == "BACKGROUND_REMOVER"
    assert request.payload_json["image_url"] == "https://example.invalid/notebook.png"
    assert request.payload_json["action_key"] == "background_remove"
    assert request.context_json["principal_id"] == "ops-onemin-media"


def test_ltd_runtime_rejects_receipt_bound_to_another_principal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _client(principal_id="ops-onemin-unbound")
    _patch_catalog(monkeypatch, client, tmp_path)

    def _fake_execute(request):  # noqa: ANN001
        receipt = _verified_tool_receipt(
            request,
            provider_key="onemin",
            provider_backend="1min",
        )
        receipt["principal_id"] = "different-principal"
        return ToolInvocationResult(
            tool_name=request.tool_name,
            action_kind=request.action_kind,
            target_ref="provider://onemin/unbound-code",
            output_json={"normalized_text": "untrusted"},
            receipt_json=receipt,
        )

    monkeypatch.setattr(client.app.state.container.tool_execution, "execute_invocation", _fake_execute)
    response = client.post(
        "/v1/ltds/runtime-catalog/1min.AI/actions/code_generate",
        json={"prompt": "Create a small CLI"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ltd_runtime_execution_receipt_unverified"
