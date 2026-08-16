from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


def _client(*, principal_id: str) -> TestClient:
    os.environ["EA_STORAGE_BACKEND"] = "memory"
    os.environ.pop("EA_LEDGER_BACKEND", None)
    os.environ["EA_API_TOKEN"] = ""
    os.environ.pop("EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER", None)
    os.environ.pop("EA_OPERATOR_PRINCIPAL_IDS", None)
    from app.api.app import create_app

    client = TestClient(create_app())
    client.headers.update({"X-EA-Principal-ID": principal_id})
    return client


class _TelegramScenarioAgent:
    def __init__(self, client: TestClient, *, secret: str, chat_id: int | str = 1354554303):
        self.client = client
        self.secret = secret
        self.chat_id = chat_id
        self._message_id = 9000

    def ask(self, text: str) -> dict[str, object]:
        self._message_id += 1
        return self.send_message_payload({"text": text})

    def send_message_payload(self, payload: dict[str, object]) -> dict[str, object]:
        response = self.client.post(
            "/v1/channels/telegram/ingest",
            headers={"X-Telegram-Bot-Api-Secret-Token": self.secret},
            json={
                "message": {
                    "message_id": self._message_id,
                    "date": 123 + self._message_id,
                    "chat": {"id": self.chat_id, "type": "private"},
                    **payload,
                }
            },
        )
        assert response.status_code == 200
        return response.json()


def test_telegram_bot_workflow_routes_documents_photos_and_ltd_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-e2e-routing")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-e2e-routing")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_API_KEY", "onedrive-key")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_AGENT_ID", "onedrive-agent")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_LABEL", "OneDrive documents")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_API_KEY", "shareone-key")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_AGENT_ID", "shareone-agent")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_LABEL", "ShareOne documents")
    from app.api.routes import channels as channels_route
    from app.domain.models import ToolInvocationResult

    sent: list[dict[str, object]] = []
    answerly_calls: list[dict[str, object]] = []
    executed_requests = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 9901}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    class _Account:
        def __init__(self):
            self.token_status = "active"
            self.binding = type("Binding", (), {"status": "enabled"})()
            self.granted_scopes = [channels_route.google_oauth_service.GOOGLE_SCOPE_PHOTOS_PICKER]
            self.google_email = "tibor.girschele@gmail.com"

    def _fake_answerly_chat(*, config, message, conversation_id=""):
        answerly_calls.append({"scope": config["scope"], "label": config["label"], "message": message})
        if config["scope"] == "onedrive":
            if "birth certificate" in message.lower():
                return {
                    "status": True,
                    "data": {
                        "messages": ["Noah Girschele's birth certificate is in the OneDrive document vault."],
                        "actionResponse": {"name": "conversational"},
                        "meta": {"source": [{"dataItemId": "onedrive-birth-cert-1"}]},
                    },
                }
            if "medication" in message.lower():
                return {
                    "status": True,
                    "data": {
                        "messages": ["Your medication is currently listed in the bedside drawer medication organizer."],
                        "actionResponse": {"name": "conversational"},
                        "meta": {"source": [{"dataItemId": "onedrive-medication-1"}]},
                    },
                }
            return {
                "status": True,
                "data": {
                    "messages": ["The OneDrive rehab approval confirms Rosenhügel NRZ and says the KfA authorization is active."],
                    "actionResponse": {"name": "conversational"},
                    "meta": {"source": [{"dataItemId": "onedrive-kfa-1"}]},
                },
            }
        return {
            "status": True,
            "data": {
                "messages": ["The ShareOne school packet says Noah still needs one follow-up form."],
                "actionResponse": {"name": "conversational"},
                "meta": {"source": [{"dataItemId": "shareone-school-1"}]},
            },
        }

    def _fake_execute(request):  # noqa: ANN001
        executed_requests.append(request)
        return ToolInvocationResult(
            tool_name=request.tool_name,
            action_kind=request.action_kind,
            target_ref="onemin:background-remove:test-asset-1",
            output_json={
                "ok": True,
                "provider_backend": "1min",
                "asset_urls": ["https://example.invalid/output/cat-no-background.png"],
            },
            receipt_json={
                "principal_id": request.context_json["principal_id"],
                "handler_key": request.tool_name,
                "invocation_contract": "tool.v1",
                "provider_key": "onemin",
                "provider_backend": "1min",
                "feature_type": request.payload_json["feature_type"],
                "model": "test-background-remover-v1",
            },
        )

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route, "_answerly_chat", _fake_answerly_chat)
    monkeypatch.setattr(
        channels_route,
        "resolve_telegram_message_payload",
        lambda *, payload, bot_token: {
            **dict(payload or {}),
            "text": (
                "Can you start the photo picker now?"
                if str(dict(payload or {}).get("kind") or "").strip().lower() == "voice"
                else str(dict(payload or {}).get("text") or "")
            ),
            "transcription_status": (
                "ok" if str(dict(payload or {}).get("kind") or "").strip().lower() == "voice" else ""
            ),
        },
    )
    monkeypatch.setattr(channels_route.google_oauth_service, "list_google_accounts", lambda **kwargs: [_Account()])
    monkeypatch.setattr(
        channels_route,
        "_telegram_ltd_runtime_profiles",
        lambda container: [
            SimpleNamespace(
                service_name="1min.AI",
                runtime_state="provider_executable",
                workspace_integration_tier="Tier 1",
                aliases=("1min ai",),
                actions=(
                    SimpleNamespace(
                        action_key="background_remove",
                        route_path="/v1/ltds/runtime-catalog/1min.AI/actions/background_remove",
                        executable=True,
                        description="Remove the background from an image.",
                        tool_name="provider.onemin.media_transform",
                        action_kind="media_transform",
                    ),
                ),
            ),
        ],
    )

    class _FakeCatalog:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(channels_route, "LtdRuntimeCatalogService", _FakeCatalog)
    monkeypatch.setattr(
        channels_route,
        "projected_task_key_for_request",
        lambda **kwargs: channels_route.projected_task_key("1min.AI", "background_remove")
        if "1min.ai" in str(kwargs.get("goal") or "").lower()
        else "",
    )

    client = _client(principal_id="")
    monkeypatch.setattr(client.app.state.container.tool_execution, "execute_invocation", _fake_execute)
    agent = _TelegramScenarioAgent(client, secret="tg-secret")

    onedrive = agent.ask("What does the latest OneDrive KfA rehab approval say?")
    assert onedrive["reply_sent"] is True
    assert "OneDrive rehab approval confirms Rosenhügel NRZ" in onedrive["reply_text"]
    assert answerly_calls[-1]["scope"] == "onedrive"

    birth_certificate = agent.ask("Send me the birth certificate of Noah Girschele.")
    assert birth_certificate["reply_sent"] is True
    assert "Noah Girschele's birth certificate is in the OneDrive document vault." in birth_certificate["reply_text"]
    assert answerly_calls[-1]["scope"] == "onedrive"

    medication = agent.ask("Where is my medication right now?")
    assert medication["reply_sent"] is True
    assert "Your medication is currently listed in the bedside drawer medication organizer." in medication["reply_text"]
    assert answerly_calls[-1]["scope"] == "onedrive"

    ambiguous = agent.ask("Search the documents for the rehab approval.")
    assert ambiguous["reply_sent"] is True
    assert "Your document backends stay separated." in ambiguous["reply_text"]

    shareone = agent.ask("Search ShareOne documents for the school paperwork.")
    assert shareone["reply_sent"] is True
    assert "ShareOne school packet says Noah still needs one follow-up form." in shareone["reply_text"]
    assert answerly_calls[-1]["scope"] == "shareone"

    photos = agent.ask("You should have access to my Google photos. Can you find me the picture where Noah is sleeping on a mattress?")
    assert photos["reply_sent"] is True
    assert "only on photos you explicitly select in the picker" in photos["reply_text"]

    agent._message_id += 1
    voice = agent.send_message_payload({"voice": {"file_id": "voice-file-1", "duration": 8}})
    assert voice["reply_sent"] is True
    assert "Google Photos Picker access is connected" in voice["reply_text"] or "Google Photos Picker is ready" in voice["reply_text"]

    image = agent.ask("Use 1min.AI to remove the background from https://example.invalid/cat.png")
    assert image["reply_sent"] is True
    assert "Executed 1min.AI background_remove with a principal-bound provider receipt." in image["reply_text"]
    assert executed_requests[-1].payload_json["image_url"] == "https://example.invalid/cat.png"
    assert len(sent) >= 5


def test_telegram_bot_workflow_persists_async_admin_followup_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-e2e-admin")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-e2e-admin")
    from app.api.routes import channels as channels_route

    class _InlineExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            fn(*args, **kwargs)
            return SimpleNamespace()

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 9902}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        return _FakeResponse()

    class _FakeProductService:
        def list_brief_items(self, *, principal_id: str, limit: int = 8, **kwargs):
            return []

        def list_queue(self, *, principal_id: str, limit: int = 8, **kwargs):
            return [
                SimpleNamespace(
                    id="queue-rehab",
                    object_ref="queue-rehab",
                    priority="high",
                    rank_score=96.0,
                    title="Check KfA rehab authorization",
                    summary="Rehab approval and KfA paperwork still need review.",
                    recommended_action="check rehab approvals",
                    profile_followup_refs=("profile_followup:insurance_admin:rehab_authorization_management",),
                ),
                SimpleNamespace(
                    id="queue-school",
                    object_ref="queue-school",
                    priority="high",
                    rank_score=85.0,
                    title="Review Noah school paperwork",
                    summary="School enrollment and coordination paperwork need a pass.",
                    recommended_action="review school paperwork",
                    profile_followup_refs=("profile_followup:school_admin:school_and_kindergarten_coordination",),
                ),
            ]

        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {"preference_nodes": []}

        def list_office_events(self, *, principal_id: str, limit: int = 12, **kwargs):
            return []

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route, "_telegram_real_ea_reply_text", lambda **kwargs: "Focus on the rehab approvals and KfA authorization paperwork first.")
    monkeypatch.setattr(channels_route, "_TELEGRAM_ASYNC_EXECUTOR", _InlineExecutor())
    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    client = _client(principal_id="")
    agent = _TelegramScenarioAgent(client, secret="tg-secret")

    first = agent.ask("What should I tackle first?")
    assert first["reply_sent"] is False
    observations = list(client.app.state.container.channel_runtime.list_recent_observations(limit=20, principal_id="exec-telegram-e2e-admin"))
    async_payload = next(dict(row.payload or {}) for row in observations if str(row.event_type) == "telegram.reply_async_sent")
    assert async_payload["intent_state"]["active_intent"] == "admin_followup"
    assert async_payload["intent_state"]["active_admin_primary_title"] == "Check KfA rehab authorization"

    second = agent.ask("And after that?")
    assert second["reply_sent"] is True
    assert "After that, focus on Review Noah school paperwork." in second["reply_text"]


def test_telegram_bot_workflow_persists_property_comparison_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-e2e-property")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-e2e-property")
    from app.api.routes import channels as channels_route
    from app.product.models import EvidenceRef

    upstream_groundings: list[str] = []

    class _InlineExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            fn(*args, **kwargs)
            return SimpleNamespace()

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 9903}}).encode("utf-8")

    class _FakeResult:
        def __init__(self, text: str) -> None:
            self.text = text

    def _fake_urlopen(request, timeout=30):
        return _FakeResponse()

    def _fake_generate_upstream_text(**kwargs):
        system_messages = [item["content"] for item in kwargs["messages"] if item["role"] == "system"]
        grounding_text = str(system_messages[1]) if len(system_messages) > 1 else ""
        upstream_groundings.append(grounding_text)
        user_messages = [item["content"] for item in kwargs["messages"] if item["role"] == "user"]
        prompt = str(user_messages[-1]) if user_messages else ""
        if "what about the other one" in prompt.lower():
            assert "comparison_secondary: Strong Doebling listing | willhaben:1071155412" in grounding_text
            return _FakeResult("The other one is the Strong Doebling listing. It is the backup because it still has lift and bike access, but the Waehring one stays ahead.")
        return _FakeResult("The Strong Waehring listing is still better. Keep the Strong Doebling listing as the backup option.")

    class _FakeProductService:
        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {"preference_nodes": [{"domain": "willhaben", "status": "active", "key": "preferred_areas", "value_json": ["Waehring", "Doebling"], "confidence": 0.95}]}

        def list_office_events(self, *, principal_id: str, limit: int = 12, **kwargs):
            return [{"channel": "product", "event_type": "property_alert_review_created", "summary": "New property alert analyzed."}]

        def list_brief_items(self, *, principal_id: str, limit: int = 5, **kwargs):
            return [
                SimpleNamespace(
                    id="brief-strong-waehring",
                    score=97.0,
                    title="Strong Waehring listing",
                    why_now="High-fit property alert with 360 media and preferred district match.",
                    recommended_action="review property alert",
                    object_ref="willhaben:1411708198",
                    evidence_refs=(EvidenceRef(ref_id="listing:1411708198", href="https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/wien-1180-waehring/1411708198/", label="Willhaben listing"),),
                ),
                SimpleNamespace(
                    id="brief-doebling-listing",
                    score=91.0,
                    title="Strong Doebling listing",
                    why_now="Another high-fit property alert with lift and bike access.",
                    recommended_action="compare against shortlist",
                    object_ref="willhaben:1071155412",
                    evidence_refs=(EvidenceRef(ref_id="listing:1071155412", href="https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/wien-1190-doebling/1071155412/", label="Willhaben listing"),),
                ),
            ]

        def list_queue(self, *, principal_id: str, limit: int = 5, **kwargs):
            return [
                SimpleNamespace(id="queue-property-1411708198", object_ref="queue-property-1411708198", priority="high", rank_score=96.0, title="Review apartment alert: Strong Waehring listing", summary="Personal fit 96/100 · shortlist · The listing is in Waehring.", evidence_refs=(EvidenceRef(ref_id="listing:1411708198", href="https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/wien-1180-waehring/1411708198/", label="Willhaben listing"),)),
                SimpleNamespace(id="queue-property-1071155412", object_ref="queue-property-1071155412", priority="high", rank_score=91.0, title="Review apartment alert: Strong Doebling listing", summary="Personal fit 91/100 · shortlist · Lift and bike access look strong.", evidence_refs=(EvidenceRef(ref_id="listing:1071155412", href="https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/wien-1190-doebling/1071155412/", label="Willhaben listing"),)),
            ]

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route.responses_route, "_generate_upstream_text", _fake_generate_upstream_text)
    monkeypatch.setattr(channels_route, "_TELEGRAM_ASYNC_EXECUTOR", _InlineExecutor())
    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    monkeypatch.setattr(channels_route, "_telegram_upcoming_calendar_events", lambda *args, **kwargs: [])
    client = _client(principal_id="")
    agent = _TelegramScenarioAgent(client, secret="tg-secret")

    first = agent.ask("Compare the two best property candidates.")
    assert first["reply_sent"] is False
    observations = list(client.app.state.container.channel_runtime.list_recent_observations(limit=24, principal_id="exec-telegram-e2e-property"))
    first_async = next(dict(row.payload or {}) for row in observations if str(row.event_type) == "telegram.reply_async_sent")
    assert first_async["comparison_state"]["comparison_primary"].startswith("Strong Waehring listing")
    assert first_async["comparison_state"]["comparison_secondary"].startswith("Strong Doebling listing")

    second = agent.ask("What about the other one?")
    assert second["reply_sent"] is False
    assert any("comparison_secondary: Strong Doebling listing | willhaben:1071155412" in item for item in upstream_groundings)


def test_telegram_bot_property_link_e2e_sends_diorama_photo_and_artifact_buttons(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-e2e-property-link")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-e2e-property-link")
    monkeypatch.setenv("EA_PUBLIC_APP_BASE_URL", "https://propertyquarry.com")
    monkeypatch.setenv("EA_TELEGRAM_PROPERTY_LINK_BUNDLE_POLL_ATTEMPTS", "0")
    from app.api.routes import channels as channels_route
    import app.product.service as product_service
    from app.product.service import ProductService

    class _InlineExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            fn(*args, **kwargs)
            return SimpleNamespace()

    class _Receipt:
        chat_id = "1354554303"
        message_ids = ("tg-property-link-1",)

    dossier_path = tmp_path / "property-link-dossier.pdf"
    dossier_path.write_bytes(b"%PDF-1.4\n% property link e2e\n")
    tour_dir = tmp_path / "e2e-property-link"
    tour_dir.mkdir(parents=True)
    (tour_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": "e2e-property-link",
                "matterport_url": "https://my.matterport.com/show/?m=E2ELINK",
                "three_d_vista_entry_relpath": "3dvista/index.htm",
            }
        ),
        encoding="utf-8",
    )
    sent_photos: list[dict[str, object]] = []
    viewer_gate_calls: list[dict[str, str]] = []

    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setattr(channels_route, "_TELEGRAM_FREE_RENDER_EXECUTOR", _InlineExecutor())
    monkeypatch.setattr(
        ProductService,
        "create_generic_property_tour",
        lambda self, **kwargs: {
            "status": "created",
            "tour_url": "https://propertyquarry.com/tours/e2e-property-link",
            "vendor_tour_url": "",
            "blocked_reason": "",
            "personal_fit_assessment": {"recommendation": "shortlist", "fit_score": 81.0},
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_scout_page_preview",
        lambda property_url: {
            "title": "E2E Telegram Property",
            "listing_id": "e2e-property-link",
            "description": "Bright family apartment with a usable floorplan.",
            "media_urls_json": ["https://cdn.example.com/e2e/photo.jpg"],
            "floorplan_urls_json": ["https://cdn.example.com/e2e/floorplan.jpg"],
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_scout_candidate_payload_from_preview",
        lambda *, property_url, preview: {"listing_id": "e2e-property-link", "rooms": 3, "area_sqm": 72, "has_floorplan": True},
    )
    monkeypatch.setattr(product_service, "_merge_property_facts_with_source_research", lambda **kwargs: dict(kwargs.get("property_facts") or {}))
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_video_delivery",
        lambda tour_url: {
            "video_url": "https://propertyquarry.com/tours/files/e2e-property-link/tour.mp4",
            "provider_key": "magicfit",
            "duration_seconds": 120.0,
            "coverage_proof": "boundary_verified_frame_continuation",
            "covered_route_labels": ["living room", "bedroom", "bedroom 2"],
        },
    )
    monkeypatch.setattr(product_service, "_property_bundle_exit_gate_http_url", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(
        product_service,
        "_property_3d_viewer_links_exit_gate",
        lambda viewer_links, **kwargs: viewer_gate_calls.append(dict(viewer_links)) or (True, "", {"checked": dict(viewer_links)}),
    )
    monkeypatch.setattr(
        ProductService,
        "_render_property_scout_dossier",
        lambda self, **kwargs: {
            "status": "rendered",
            "publication_id": "pub-e2e-property-link",
            "pdf_path": str(dossier_path),
            "public_pdf_url": "https://propertyquarry.com/v1/integrations/fliplink/documents/property-packets/e2e-token",
            "caption": "PropertyQuarry dossier · E2E Telegram Property",
            "diorama_preview_url": "https://propertyquarry.com/tours/files/e2e-property-link/diorama-preview.png",
        },
    )
    monkeypatch.setattr(
        product_service,
        "send_telegram_photo_for_principal",
        lambda tool_runtime, **kwargs: sent_photos.append(dict(kwargs)) or _Receipt(),
    )
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("diorama photo reply should be used")),
    )

    client = _client(principal_id="")
    agent = _TelegramScenarioAgent(client, secret="tg-secret")
    reply = agent.ask(
        "Please make a scout update with a warm diorama style for https://www.immobilienscout24.at/expose/e2e-property-link"
    )

    assert reply["reply_sent"] is False
    assert sent_photos
    assert viewer_gate_calls[-1] == {
        "matterport": "https://propertyquarry.com/tours/e2e-property-link/control/matterport",
    }
    photo = sent_photos[-1]
    assert photo["principal_id"] == "exec-telegram-e2e-property-link"
    assert photo["photo_ref"] == "https://propertyquarry.com/tours/files/e2e-property-link/diorama-preview.png"
    assert "3D tour, walkthrough, and review PDF are ready." in str(photo["caption"])
    buttons = list(photo["url_buttons"] or [])
    flattened = [button for row in buttons for button in row]
    assert ("Open 3D tour", "https://propertyquarry.com/tours/e2e-property-link/control/matterport") in flattened
    assert ("Open 3DVista", "https://propertyquarry.com/tours/e2e-property-link/control/3dvista") not in flattened
    assert ("Open walkthrough", "https://propertyquarry.com/tours/files/e2e-property-link/tour.mp4") in flattened
    assert any(label == "Open review PDF" for label, _url in flattened)


def test_telegram_bot_property_pdf_upload_e2e_returns_rendered_pdf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-e2e-property-pdf")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-e2e-property-pdf")
    from app.api.routes import channels as channels_route
    from app.services import telegram_session_service
    import app.product.service as product_service
    from app.product.service import ProductService

    class _InlineExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            fn(*args, **kwargs)
            return SimpleNamespace()

    class _Receipt:
        chat_id = "1354554303"
        message_ids = ("tg-property-pdf-1",)

    returned_pdf = tmp_path / "returned-property-packet.pdf"
    returned_pdf.write_bytes(b"%PDF-1.4\n% returned property pdf e2e\n")
    combined_pdf = tmp_path / "combined-property-packet.pdf"
    combined_pdf.write_bytes(b"%PDF-1.4\n% combined property pdf e2e\n")
    tour_dir = tmp_path / "pdf-upload-tour"
    tour_dir.mkdir(parents=True)
    three_d_vista_dir = tour_dir / "3dvista"
    three_d_vista_dir.mkdir()
    (three_d_vista_dir / "index.htm").write_text(
        "<!doctype html><html><body><div id='tourviewer'>3D tour ready</div>"
        "<script>window.TDVPlayer = { ready: true };</script></body></html>",
        encoding="utf-8",
    )
    (tour_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": "pdf-upload-tour",
                "three_d_vista_entry_relpath": "3dvista/index.htm",
                "three_d_vista_import": {"source_project": "propertyquarry"},
                "three_d_vista_white_label_proof": {
                    "source_project": "propertyquarry",
                    "private_viewer_verified": True,
                    "non_trial_export_verified": True,
                    "propertyquarry_tour_metadata": True,
                    "trial_branding_checked": True,
                    "trial_branding_present": False,
                },
                "three_d_vista_browser_render_proof": {
                    "provider": "3dvista",
                    "status": "pass",
                    "rendered_viewer": True,
                },
            }
        ),
        encoding="utf-8",
    )
    sent_documents: list[dict[str, object]] = []
    render_calls: list[dict[str, object]] = []
    viewer_gate_calls: list[dict[str, str]] = []

    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setattr(channels_route, "_TELEGRAM_ASYNC_EXECUTOR", _InlineExecutor())
    monkeypatch.setattr(
        telegram_session_service,
        "_telegram_file_download_url",
        lambda *, bot_token, file_id: "https://api.telegram.org/file/botredacted/documents/property-upload.pdf",
    )
    monkeypatch.setattr(
        product_service,
        "_write_hosted_floorplan_property_tour_bundle",
        lambda **kwargs: {
            "hosted_url": "https://propertyquarry.com/tours/pdf-upload-tour",
            "public_url": "https://propertyquarry.com/tours/pdf-upload-tour",
            "creation_mode": "hosted_floorplan_tour",
        },
    )
    monkeypatch.setattr(
        product_service,
        "_render_magicfit_property_flythrough_into_hosted_tour",
        lambda **kwargs: {
            "status": "rendered",
            "provider_key": "magicfit",
            "video_file_path": "/tmp/pdf-upload-tour/tour.mp4",
        },
    )
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_video_delivery",
        lambda tour_url: {
            "video_url": "https://propertyquarry.com/tours/files/pdf-upload-tour/tour.mp4",
            "provider_key": "magicfit",
            "duration_seconds": 30.0,
            "coverage_proof": "boundary_verified_frame_continuation",
        },
    )
    monkeypatch.setattr(
        product_service,
        "_append_propertyquarry_pdf_to_source_pdf",
        lambda **kwargs: str(combined_pdf),
    )
    monkeypatch.setattr(
        product_service,
        "_property_3d_viewer_links_exit_gate",
        lambda viewer_links, **kwargs: viewer_gate_calls.append(dict(viewer_links)) or (True, "", {"checked": dict(viewer_links)}),
    )
    monkeypatch.setattr(
        ProductService,
        "_render_property_scout_dossier",
        lambda self, **kwargs: render_calls.append(dict(kwargs)) or {
            "status": "rendered",
            "publication_id": "pub-e2e-property-pdf",
            "pdf_path": str(returned_pdf),
            "public_pdf_url": "https://propertyquarry.com/v1/integrations/fliplink/documents/property-packets/pdf-token",
            "caption": "PropertyQuarry dossier · Uploaded property PDF",
        },
    )
    monkeypatch.setattr(
        product_service,
        "send_telegram_document_for_principal",
        lambda tool_runtime, **kwargs: sent_documents.append(dict(kwargs)) or _Receipt(),
    )

    client = _client(principal_id="")
    agent = _TelegramScenarioAgent(client, secret="tg-secret")
    agent._message_id += 1
    response = agent.send_message_payload(
        {
            "caption": "PropertyQuarry scout update from this Wohnung expose PDF",
            "document": {
                "file_id": "telegram-property-pdf-file",
                "file_name": "wohnung-expose.pdf",
                "mime_type": "application/pdf",
            },
        }
    )

    assert response["reply_sent"] is False
    assert render_calls
    assert render_calls[-1]["property_url"] == "https://api.telegram.org/file/botredacted/documents/property-upload.pdf"
    assert render_calls[-1]["appendix_mode"] == "telegram_pdf_appendix"
    assert sent_documents
    assert viewer_gate_calls[-1] == {
        "3dvista": "https://propertyquarry.com/tours/pdf-upload-tour/control/3dvista",
    }
    assert sent_documents[-1]["principal_id"] == "exec-telegram-e2e-property-pdf"
    assert sent_documents[-1]["document_ref"] == str(combined_pdf)
    assert "Original PDF first" in str(sent_documents[-1]["caption"])
    buttons = [button for row in list(sent_documents[-1]["url_buttons"] or []) for button in row]
    assert ("Open 3D tour", "https://propertyquarry.com/tours/pdf-upload-tour/control/3dvista") in buttons
    assert not any(label in {"Open Matterport", "Open 3DVista"} for label, _url in buttons)
    assert ("Open walkthrough", "https://propertyquarry.com/tours/files/pdf-upload-tour/tour.mp4") in buttons
    observations = list(client.app.state.container.channel_runtime.list_recent_observations(limit=20, principal_id="exec-telegram-e2e-property-pdf"))
    assert any(str(row.event_type) == "telegram.reply_async_sent" for row in observations)


def test_telegram_bot_workflow_answers_focus_on_tomorrow_from_calendar_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-e2e-focus")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-e2e-focus")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 9904}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="")
    agent = _TelegramScenarioAgent(client, secret="tg-secret")

    tomorrow_vienna = (datetime.now(ZoneInfo("Europe/Vienna")) + timedelta(days=1)).replace(
        hour=9,
        minute=30,
        second=0,
        microsecond=0,
    )
    client.app.state.container.channel_runtime.ingest_observation(
        principal_id="exec-telegram-e2e-focus",
        channel="calendar",
        event_type="office_signal_calendar_note",
        payload={
            "title": "Strategy Review",
            "summary": "Strategy Review",
            "start_at": tomorrow_vienna.isoformat(),
            "location": "HQ",
        },
        source_id="calendar-event:e2e-focus-1",
        external_id="calendar-event:e2e-focus-1",
        dedupe_key="calendar-event:e2e-focus-1",
    )

    reply = agent.ask("What should I focus on tomorrow?")
    assert reply["reply_sent"] is True
    assert "Tomorrow, focus first on Strategy Review at 09:30." in reply["reply_text"]
    assert "Location: HQ." in reply["reply_text"]


def test_telegram_codex_human_audit_simulation_checks_calendar_pocket_semantic_fallback_and_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-e2e-codex-audit")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-e2e-codex-audit")
    from app.api.routes import channels as channels_route

    sent_payloads: list[dict[str, object]] = []

    class _InlineExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            fn(*args, **kwargs)
            return SimpleNamespace()

    class _FakeResponse:
        def __init__(self, message_id: int) -> None:
            self._message_id = message_id

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": self._message_id}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent_payloads.append(payload)
        return _FakeResponse(12000 + len(sent_payloads))

    class _FakeResult:
        def __init__(self, text: str) -> None:
            self.text = text

    class _FakeProductService:
        def search_pocket_recordings(
            self,
            *,
            principal_id: str,
            actor: str,
            query: str = "",
            before: str = "",
            after: str = "",
            limit: int = 10,
        ) -> dict[str, object]:
            exact_hit = {
                "recording_id": "rec-hanusch-1",
                "title": "Hospital medical discussion and care",
                "recording_at": "2026-05-22T15:28:13Z",
                "archive_status": "archived",
                "archive_path": "/mnt/pcloud/EA/pocket-ai-audio/hanusch.mp3",
                "archive_sha256": "abc123",
                "summary_markdown": "Conversation with father in hospital about his condition.",
                "transcript_text": "We are in Hanusch hospital and he talks about his condition and the family.",
                "transcript_excerpt": "Hanusch hospital conversation with father about his condition.",
                "location_name": "Hanusch Spital",
                "location_address": "Hanusch Krankenhaus, Wien",
                "location_match_status": "nearest",
                "location_confidence": 0.95,
            }
            semantic_candidates = [
                {
                    "recording_id": "rec-hanusch-2",
                    "title": "Hospital call about emergency admission",
                    "recording_at": "2026-05-22T10:11:00Z",
                    "archive_status": "archived",
                    "archive_path": "/mnt/pcloud/EA/pocket-ai-audio/hanusch-2.mp3",
                    "archive_sha256": "def456",
                    "summary_markdown": "Hospital conversation with father and family context.",
                    "transcript_text": "He talks about his chessboard staying in the family and his mother being a power person.",
                    "transcript_excerpt": "chessboard staying in the family",
                    "location_name": "Hanusch Spital",
                    "location_address": "Hanusch Krankenhaus, Wien",
                    "location_match_status": "matched",
                    "location_confidence": 0.91,
                },
                {
                    "recording_id": "rec-hanusch-3",
                    "title": "Noah medication and feeding",
                    "recording_at": "2026-05-22T18:05:40Z",
                    "archive_status": "archived",
                    "archive_path": "/mnt/pcloud/EA/pocket-ai-audio/hanusch-3.mp3",
                    "archive_sha256": "ghi789",
                    "summary_markdown": "Hospital bedside conversation with family context.",
                    "transcript_text": "His brother and mother are mentioned in the hospital discussion.",
                    "transcript_excerpt": "brother and mother in the hospital discussion",
                    "location_name": "Hanusch Spital",
                    "location_address": "Hanusch Krankenhaus, Wien",
                    "location_match_status": "matched",
                    "location_confidence": 0.89,
                },
            ]
            normalized_query = str(query or "").strip().lower()
            if actor == "telegram-semantic-fallback":
                items = semantic_candidates[:limit]
            elif "hanusch" in normalized_query:
                items = [exact_hit][:limit]
            elif "chessboard" in normalized_query or "power person" in normalized_query:
                items = []
            else:
                items = []
            return {
                "generated_at": "2026-05-30T00:00:00Z",
                "query": str(query or "").strip(),
                "before": before,
                "after": after,
                "total": len(items),
                "items": items,
            }

        def deliver_pocket_recording_to_telegram(self, *, principal_id: str, actor: str, recording_id: str) -> dict[str, object]:
            return {
                "recording_id": recording_id,
                "title": "Hospital call about emergency admission" if recording_id == "rec-hanusch-2" else "Hospital medical discussion and care",
                "telegram_delivery_status": "sent",
                "telegram_message_ids": ["tg-msg-pocket-1"],
                "telegram_chat_ref": "1354554303",
            }

        def list_brief_items(self, *, principal_id: str, limit: int = 8, **kwargs):
            return []

        def list_queue(self, *, principal_id: str, limit: int = 8, **kwargs):
            return []

        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {"preference_nodes": []}

        def list_office_events(self, *, principal_id: str, limit: int = 12, **kwargs):
            return []

    def _fake_generate_upstream_text(**kwargs):
        payload = json.loads(str(kwargs["messages"][-1]["content"]))
        candidates = list(payload.get("candidates") or [])
        chosen = []
        for item in candidates[:2]:
            chosen.append(
                {
                    "recording_id": item["recording_id"],
                    "reason": "Mentions the chessboard staying in the family and the mother as a power person.",
                }
            )
        return _FakeResult(json.dumps({"candidates": chosen}))

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route, "_TELEGRAM_ASYNC_EXECUTOR", _InlineExecutor())
    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    monkeypatch.setattr(channels_route.responses_route, "_generate_upstream_text", _fake_generate_upstream_text)
    monkeypatch.setattr(
        channels_route,
        "_telegram_real_ea_reply_text",
        lambda **kwargs: "Short audit result: next check the appointment timing and then send the selected hospital recording.",
    )

    client = _client(principal_id="")
    agent = _TelegramScenarioAgent(client, secret="tg-secret")

    next_vienna = (datetime.now(ZoneInfo("Europe/Vienna")) + timedelta(hours=2)).replace(second=0, microsecond=0)
    client.app.state.container.channel_runtime.ingest_observation(
        principal_id="exec-telegram-e2e-codex-audit",
        channel="calendar",
        event_type="office_signal_calendar_note",
        payload={
            "title": "BIP appointment",
            "summary": "BIP appointment",
            "start_at": next_vienna.isoformat(),
            "location": "Hanusch",
        },
        source_id="calendar-event:e2e-codex-audit-1",
        external_id="calendar-event:e2e-codex-audit-1",
        dedupe_key="calendar-event:e2e-codex-audit-1",
    )

    appointment = agent.ask("What is my next appointment?")
    assert appointment["reply_sent"] is True
    assert "BIP appointment" in appointment["reply_text"]

    exact_pocket = agent.ask("Please summarize the best Hanusch hospital Pocket audio before May 23 and tell me why it matches.")
    assert exact_pocket["reply_sent"] is True
    assert "Hospital medical discussion and care" in exact_pocket["reply_text"]
    assert "Hanusch Spital" in exact_pocket["reply_text"]

    upload_announcement = agent.ask(
        "Ich schicke mir die Audioaufnahme vom Gespräch im Hanusch Krankenhaus zwischen mir und meinem Vater."
    )
    assert upload_announcement["reply_sent"] is True
    assert "schick die Audioaufnahme" in upload_announcement["reply_text"]
    assert "Pocket recording" not in upload_announcement["reply_text"]

    vague_memory = agent.ask(
        "I am looking for the conversation with my father in the hospital where he talked about his chessboard and his mother being a power person before May 23."
    )
    assert vague_memory["reply_sent"] is True
    assert "I found these likely Pocket candidates:" in vague_memory["reply_text"]
    assert "send 1" in vague_memory["reply_text"]

    send_selected = agent.ask("send 1")
    assert send_selected["reply_sent"] is True
    assert "Sent: Hospital call about emergency admission." in send_selected["reply_text"]

    async_audit = agent.ask("Bip, bip, bip. Give me a short audit plan for today.")
    assert async_audit["reply_sent"] is False
    channels_route._telegram_async_assistant_reply_worker(
        container=client.app.state.container,
        principal_id="exec-telegram-e2e-codex-audit",
        bot_config={"handle": "tibor_concierge_bot", "token": "telegram-token-e2e-codex-audit"},
        chat_id=str(agent.chat_id),
        text="Bip, bip, bip. Give me a short audit plan for today.",
        current_message_id=str(agent._message_id),
    )
    observations = list(
        client.app.state.container.channel_runtime.list_recent_observations(
            limit=40,
            principal_id="exec-telegram-e2e-codex-audit",
        )
    )
    assert any(str(row.event_type) == "telegram.reply_async_started" for row in observations)
    assert any(str(row.event_type) == "telegram.reply_async_sent" for row in observations)
    assert any(str(row.event_type) == "telegram.pocket_candidate_suggestions_sent" for row in observations)
    assert any(
        "processing this asynchronously now" in str(payload.get("text") or "")
        or "processing it asynchronously" in str(payload.get("text") or "")
        for payload in sent_payloads
        if isinstance(payload, dict)
    )
