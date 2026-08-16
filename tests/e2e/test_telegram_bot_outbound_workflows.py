from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from app.domain.models import Artifact
from app.services import google_oauth as google_oauth_service
from app.services.registration_email import RegistrationEmailReceipt

TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from product_test_helpers import build_product_client, seed_product_state, start_workspace

pytest.importorskip("fastapi")

import app.product.service as product_service
from app.product.service import ProductService


def test_telegram_outbound_workflow_property_tour_sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BROWSERACT_API_KEY", "test-browseract-api-key")
    monkeypatch.setenv("EA_WILLHABEN_PROPERTY_TOUR_REQUIRE_360", "0")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    principal_id = "cf-email:tibor.girschele@gmail.com"
    client = build_product_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Telegram Outbound Tour Office")

    panorama_url = "https://cache.willhaben.at/mmo/1/1739164131.jpg"
    packet = {
        "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/outbound-tour-1739164131",
        "listing_id": "1739164131",
        "title": "Bright Brigittenau apartment",
        "listing_uuid": "listing-uuid-123",
        "property_facts_json": {},
        "media_urls_json": [panorama_url],
        "panorama_media_urls_json": [panorama_url],
        "media_assets_json": [
            {
                "url": panorama_url,
                "role": "photo",
                "panorama_candidate": True,
                "panorama_reason": "xmp_equirectangular",
                "width": 4096,
                "height": 2048,
                "aspect_ratio": 2.0,
            }
        ],
        "floorplan_urls_json": [],
        "tour_variants_json": [
            {
                "variant_key": "layout_first",
                "scene_strategy": "layout_first",
                "theme_name": "clean_light",
                "tour_style": "guided_layout_walkthrough",
                "audience": "tenant_screening",
            }
        ],
    }
    monkeypatch.setattr(product_service, "_load_willhaben_property_packet", lambda url: dict(packet))
    monkeypatch.setattr(
        product_service,
        "send_property_tour_email",
        lambda **kwargs: RegistrationEmailReceipt(
            provider="emailit",
            message_id="property-tour-message-1",
            accepted_at="2026-05-02T00:00:00+00:00",
        ),
    )
    monkeypatch.setattr(
        product_service,
        "resolve_primary_telegram_binding",
        lambda tool_runtime, *, principal_id: SimpleNamespace(
            external_account_ref="1354554303",
            auth_metadata_json={"default_chat_ref": "1354554303"},
        ),
    )
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda tool_runtime, *, principal_id, text: SimpleNamespace(
            chat_id="1354554303",
            bot_key="default",
            bot_handle="tibor_concierge_bot",
            message_ids=("tg-1",),
        ),
    )
    monkeypatch.setattr(
        product_service,
        "send_telegram_video_for_principal",
        lambda tool_runtime, *, principal_id, video_ref, audio_probe_ref="", caption="": SimpleNamespace(
            chat_id="1354554303",
            bot_key="default",
            bot_handle="tibor_concierge_bot",
            message_ids=("tg-video-1",),
        ),
    )

    def _fake_execute_task_artifact(request):  # type: ignore[no-untyped-def]
        return Artifact(
            artifact_id="artifact-property-tour-1",
            kind="property_tour_packet",
            content="Property tour created.",
            execution_session_id="session-property-tour-1",
            principal_id=principal_id,
            structured_output_json={
                "hosted_url": "https://propertyquarry.com/tours/brigittenau-apartment-a",
                "public_url": "https://propertyquarry.com/tours/brigittenau-apartment-a",
                "crezlo_public_url": "https://vendor.example.com/tours/brigittenau-apartment-a",
                "editor_url": "https://vendor.example.com/editor/brigittenau-apartment-a",
                "tour_id": "tour-123",
            },
        )

    original_get_contract = client.app.state.container.task_contracts.get_contract
    monkeypatch.setattr(
        client.app.state.container.task_contracts,
        "get_contract",
        lambda task_key: (
            SimpleNamespace(task_key=task_key)
            if task_key == "create_property_tour"
            else original_get_contract(task_key)
        ),
    )
    client.app.state.container.orchestrator.execute_task_artifact = _fake_execute_task_artifact
    bundle_dir = tmp_path / "brigittenau-apartment-a"
    bundle_dir.mkdir(parents=True)
    three_d_vista_dir = bundle_dir / "3dvista"
    three_d_vista_dir.mkdir()
    (three_d_vista_dir / "index.htm").write_text(
        "<!doctype html><html><body><div id='tourviewer'>3D tour ready</div>"
        "<script>window.TDVPlayer = { ready: true };</script></body></html>",
        encoding="utf-8",
    )
    (bundle_dir / "tour.mp4").write_bytes(b"fake-video")
    (bundle_dir / "tour.json").write_text(
        '{"slug":"brigittenau-apartment-a","video_relpath":"tour.mp4",'
        '"scenes":[{"asset_relpath":"scene-01.jpg"}]}',
        encoding="utf-8",
    )
    (bundle_dir / "tour.private.json").write_text(
        '{"principal_id":"cf-email:tibor.girschele@gmail.com",'
        '"three_d_vista_entry_relpath":"3dvista/index.htm",'
        '"three_d_vista_import":{"source_project":"propertyquarry"},'
        '"three_d_vista_white_label_proof":{"source_project":"propertyquarry",'
        '"private_viewer_verified":true,"non_trial_export_verified":true,'
        '"propertyquarry_tour_metadata":true,"trial_branding_checked":true,'
        '"trial_branding_present":false},'
        '"three_d_vista_browser_render_proof":{"provider":"3dvista","status":"pass",'
        '"rendered_viewer":true}}',
        encoding="utf-8",
    )
    (bundle_dir / "scene-01.jpg").write_bytes(b"scene")

    created = client.post(
        "/app/api/signals/willhaben/property-tour",
        json={
            "property_url": packet["property_url"],
            "binding_id": "browseract-binding-1",
            "auto_deliver": True,
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "ready", body
    assert body["telegram_delivery_status"] == "sent"
    assert body["telegram_chat_ref"] == "1354554303"
    assert body["telegram_message_ids"] == ["tg-1"]
    assert body["telegram_video_delivery_status"] == "sent"
    assert body["telegram_video_message_ids"] == ["tg-video-1"]
    assert body["telegram_video_url"] == "https://propertyquarry.com/tours/files/brigittenau-apartment-a/tour.mp4"

    tg_events = client.get("/app/api/events", params={"channel": "product", "event_type": "willhaben_property_tour_telegram_sent"})
    assert tg_events.status_code == 200
    assert any(item["payload"]["telegram_chat_ref"] == "1354554303" for item in tg_events.json()["items"])
    video_events = client.get("/app/api/events", params={"channel": "product", "event_type": "willhaben_property_tour_telegram_video_sent"})
    assert video_events.status_code == 200
    assert any(item["payload"]["telegram_video_url"].endswith("/tour.mp4") for item in video_events.json()["items"])


def test_telegram_outbound_workflow_blocked_property_tour_sends_scout_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSERACT_API_KEY", raising=False)
    monkeypatch.setenv("EA_WILLHABEN_PROPERTY_TOUR_REQUIRE_360", "0")
    principal_id = "exec-telegram-outbound-followup"
    client = build_product_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Telegram Outbound Followup Office")

    panorama_url = "https://cache.willhaben.at/mmo/1/1739164132.jpg"
    packet = {
        "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/outbound-followup-1739164132",
        "listing_id": "1739164132",
        "title": "Quiet district apartment",
        "listing_uuid": "listing-followup-uuid-001",
        "property_facts_json": {},
        "media_urls_json": [panorama_url],
        "panorama_media_urls_json": [panorama_url],
        "media_assets_json": [
            {
                "url": panorama_url,
                "role": "photo",
                "panorama_candidate": True,
                "panorama_reason": "xmp_equirectangular",
                "width": 4096,
                "height": 2048,
                "aspect_ratio": 2.0,
            }
        ],
        "floorplan_urls_json": [],
        "tour_variants_json": [
            {
                "variant_key": "layout_first",
                "scene_strategy": "layout_first",
                "theme_name": "clean_light",
                "tour_style": "guided_layout_walkthrough",
                "audience": "tenant_screening",
            }
        ],
    }
    monkeypatch.setattr(product_service, "_load_willhaben_property_packet", lambda url: dict(packet))
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: sent.append({"args": args, "kwargs": kwargs}) or SimpleNamespace(message_ids=["tg-followup-1"], chat_id="1354554303"),
    )

    created = client.post("/app/api/signals/willhaben/property-tour", json={"property_url": packet["property_url"]})
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "browseract_connector_unconfigured"
    assert sent == []

    events = client.get("/app/api/events", params={"channel": "product", "event_type": "property_tour_followup_telegram_suppressed"})
    assert events.status_code == 200
    assert any(item["payload"]["reason"] == "not_customer_actionable" for item in events.json()["items"])


def test_telegram_outbound_workflow_suppresses_weak_property_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-telegram-outbound-suppressed"
    client = build_product_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Telegram Outbound Quiet Office")

    monkeypatch.setattr(
        google_oauth_service,
        "list_recent_workspace_signals",
        lambda **_: google_oauth_service.GoogleWorkspaceSignalSync(
            account_email="elisabeth.girschele@gmail.com",
            account_emails=("elisabeth.girschele@gmail.com",),
            granted_scopes=(google_oauth_service.GOOGLE_SCOPE_GMAIL_MODIFY,),
            signals=(
                google_oauth_service.GoogleWorkspaceSignal(
                    signal_type="email_thread",
                    channel="gmail",
                    title='"Eigentumswohnungen" hat 5 neue Anzeigen für dich gefunden',
                    summary="Recent mail from willhaben-Suchagent.",
                    text="Neue Anzeigen gefunden.",
                    source_ref="gmail-thread:elisabeth.girschele@gmail.com:quiet-digest-1",
                    external_id="gmail-message:elisabeth.girschele@gmail.com:quiet-digest-1",
                    counterparty="willhaben-Suchagent",
                    due_at=None,
                    payload={
                        "from_email": "no-reply@agent.willhaben.at",
                        "from_name": "willhaben-Suchagent",
                        "account_email": "elisabeth.girschele@gmail.com",
                        "body_text_excerpt": "Neue Anzeigen gefunden.",
                        "labels": ["CATEGORY_UPDATES", "INBOX"],
                    },
                ),
            ),
        ),
    )
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: sent.append({"args": args, "kwargs": kwargs}) or SimpleNamespace(message_ids=["1"], chat_id="chat"),
    )

    synced = client.post("/app/api/signals/google/property-sync", params={"account_email": "elisabeth.girschele@gmail.com", "email_limit": 5})
    assert synced.status_code == 200
    assert synced.json()["synced_total"] == 1
    assert sent == []

    events = client.get("/app/api/events", params={"channel": "product"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "property_alert_review_created" in event_types
    assert "property_alert_review_telegram_suppressed" in event_types


def test_telegram_outbound_workflow_google_photos_sync_sends_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-telegram-outbound-photos"
    client = build_product_client(principal_id=principal_id)
    seed_product_state(client, principal_id=principal_id)

    monkeypatch.setattr(
        google_oauth_service,
        "sync_google_photos_picker_session",
        lambda **_: google_oauth_service.GooglePhotosSignalSync(
            account_email="tibor.girschele@gmail.com",
            account_emails=("tibor.girschele@gmail.com",),
            binding_id="exec-google-photos:google_gmail",
            session_id="photos-session-2",
            granted_scopes=(google_oauth_service.GOOGLE_SCOPE_PHOTOS_PICKER,),
            media_items_set=True,
            signals=(
                google_oauth_service.GoogleWorkspaceSignal(
                    signal_type="photo_library_item",
                    channel="google_photos",
                    title="IMG_1001.JPG",
                    summary="PHOTO · 4032x3024 · Apple iPhone",
                    text="Google Photos photo selected by tibor.girschele@gmail.com.",
                    source_ref="google-photo:tibor.girschele@gmail.com:item-1001",
                    external_id="google-photo:tibor.girschele@gmail.com:item-1001",
                    counterparty="tibor.girschele@gmail.com",
                    due_at=None,
                    payload={
                        "account_email": "tibor.girschele@gmail.com",
                        "google_photos_session_id": "photos-session-2",
                        "google_photos_media_item_id": "item-1001",
                        "mime_type": "image/jpeg",
                        "filename": "IMG_1001.JPG",
                        "preview_url": "https://example.test/photo-1001.jpg",
                        "suppress_candidate_staging": True,
                    },
                ),
            ),
        ),
    )
    from app.services import photo_signal_analysis

    monkeypatch.setattr(
        photo_signal_analysis,
        "analyze_photo_url",
        lambda **_: {
            "summary": "Family outing in a green park with bikes and a playground nearby.",
            "signal_kind": "outing",
            "tags": ["family", "park", "bike", "playground"],
            "suggestions": [
                "This strengthens green-space and bike-infrastructure signals.",
                "Consider saving this as a family lifestyle reference for housing scoring.",
            ],
            "notable_details": ["bikes", "green lawn", "playground"],
            "sensitivity": "medium",
            "confidence": 0.82,
            "provider": "overlay_vision",
            "status": "analyzed",
        },
    )
    monkeypatch.setattr(
        "app.product.service.send_telegram_message_for_principal",
        lambda **_: {"message_ids": ["tg-photo-1"]},
    )

    synced = client.post(
        "/app/api/signals/google/photos/sync",
        json={"session_id": "photos-session-2", "account_email": "tibor.girschele@gmail.com", "max_items": 10, "delete_session": False},
    )
    assert synced.status_code == 200
    assert synced.json()["analyzed_total"] == 1

    product_events = client.get("/app/api/events", params={"channel": "product"})
    assert product_events.status_code == 200
    assert any(item["event_type"] == "google_photos_sync_telegram_sent" for item in product_events.json()["items"])


def test_telegram_outbound_workflow_memo_delivery_records_telegram_send_and_profile_nudge(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-telegram-outbound-memo"
    client = build_product_client(principal_id=principal_id)
    seed_product_state(client, principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Telegram Outbound Memo Office")
    product = ProductService(client.app.state.container)

    product.upsert_preference_profile(principal_id=principal_id, person_id="self", display_name="Tibor Girschele", learning_enabled=True)
    product.upsert_preference_node(
        principal_id=principal_id,
        person_id="self",
        domain="life_admin",
        category="insurance_admin",
        key="rehab_authorization_management",
        value_json={"enabled": True},
        strength="high",
        confidence=0.9,
        source_mode="inferred",
        status="active",
        decay_policy="reinforce_only",
    )
    product.record_preference_evidence(
        principal_id=principal_id,
        person_id="self",
        domain="document_ingest",
        event_type="document_pattern_detected",
        object_type="scanned_document_batch",
        object_id="onedrive:tibor-insurance-rehab-authorizations",
        source_ref="/mnt/onedrive/Documents/Scanned Documents",
        raw_signal_json={"sample_documents": ["20250615 Kfa Bewilligung Physio.pdf"]},
        interpreted_signal_json={"summary": "Recurring KfA and rehab authorization paperwork."},
        signal_strength=0.9,
        reversible=True,
    )
    monkeypatch.setattr(
        "app.product.service.send_telegram_message_for_principal",
        lambda *args, **_: SimpleNamespace(message_ids=["tg-memo-1"], chat_id="1354554303"),
    )

    delivery = client.post(
        "/app/api/channel-loop/memo/deliveries",
        json={
            "recipient_email": "principal@example.com",
            "role": "principal",
            "display_name": "Principal Digest",
            "delivery_channel": "telegram",
        },
    )
    assert delivery.status_code == 200
    body = delivery.json()
    assert body["telegram_delivery_status"] == "sent"
    assert body["telegram_chat_ref"] == "1354554303"
    assert body["telegram_message_ids"] == ["tg-memo-1"]

    product_events = client.get("/app/api/events", params={"channel": "product"})
    assert product_events.status_code == 200
    event_types = [item["event_type"] for item in product_events.json()["items"]]
    assert "channel_digest_delivery_telegram_sent" in event_types
    assert "profile_followup_nudged" in event_types
