from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.product.property_score_methodology import supported_property_score_methodology_languages
from app.repositories import property_packet_publications
from app.repositories.property_packet_publications import build_property_packet_publication_repository
from app.services.fliplink.pdf_renderer import PDF_RENDERER_VERSION
from tests.product_test_helpers import build_property_client, start_workspace


@pytest.fixture(autouse=True)
def _reset_packet_publication_repo(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LEGACY_PDF_RENDERER_ALLOW", "1")
    repo = property_packet_publications._MEMORY_REPO
    repo._publications.clear()
    repo._publication_order.clear()
    repo._events.clear()
    repo._event_order.clear()


def _seed_packet(
    client,
    *,
    packet_kind: str = "family_review",
    privacy_mode: str = "family_review",
    fliplink_format: str = "flipbook_3d",
    property_ref: str = "listing-123",
    payload: dict[str, object] | None = None,
) -> str:
    property_payload = payload or {
        "title": "Family flat near Augarten",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/demo",
        "match_reasons": ["Floorplan and family fit."],
        "floorplan_refs": ["https://packets.propertyquarry.com/assets/floorplan.pdf"],
        "photo_refs": ["https://packets.propertyquarry.com/assets/photo.jpg"],
        "property_facts": {
            "rooms": 3,
            "area_m2": 84,
            "street_address": "Private Street 4",
            "map_lat": 48.2,
            "map_lng": 16.3,
            "has_floorplan": True,
            "postal_name": "1020 Wien",
        },
        "public_preference_snapshot": {"prefer_balcony": True},
    }
    response = client.post(
        f"/app/api/properties/{property_ref}/packets/render",
        json={
            "packet_kind": packet_kind,
            "privacy_mode": privacy_mode,
            "fliplink_format": fliplink_format,
            "property_payload": property_payload,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["publication"]["publication_id"]


def test_fliplink_manual_packet_lane_and_url_validation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_ARTIFACTS_DIR", str(tmp_path))
    client = build_property_client(principal_id="fliplink-owner")
    start_workspace(client, mode="personal", workspace_name="FlipLink Office")

    publication_id = _seed_packet(client)
    pdf = client.get(f"/app/api/properties/packets/{publication_id}/pdf")
    assert pdf.status_code == 200, pdf.text
    assert pdf.content.startswith(b"%PDF-1.4")

    rejected = client.post(
        f"/app/api/properties/packets/{publication_id}/fliplink/manual-link",
        json={"fliplink_url": "https://example.com/p/not-allowed"},
    )
    assert rejected.status_code == 422

    accepted = client.post(
        f"/app/api/properties/packets/{publication_id}/fliplink/manual-link",
        json={
            "fliplink_url": "https://packets.propertyquarry.com/p/family-flat",
            "fliplink_format": "flipbook_3d",
            "lead_capture_enabled": True,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["publication"]["status"] == "published"
    assert accepted.json()["publication"]["lead_capture_enabled"] is True

    analytics = client.post(
        f"/app/api/properties/packets/{publication_id}/fliplink/analytics-snapshot",
        json={"views": 14, "unique_visitors": 4, "average_time_seconds": 91},
    )
    assert analytics.status_code == 200, analytics.text
    assert analytics.json()["snapshot"]["views"] == 14
    assert analytics.json()["snapshot"]["trust"] == "operator_entered_or_imported"

    listing = client.get("/app/api/properties/packets")
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1
    assert listing.json()["capacity"]["cap"] >= 1
    listed = next(item for item in listing.json()["items"] if item["publication_id"] == publication_id)
    assert listed["analytics"]["views"] == 14
    assert listed["renderer_version"] == PDF_RENDERER_VERSION
    assert "/v1/integrations/fliplink/documents/property-packets/" in listed["artifact_download_path"]
    public_client = TestClient(client.app, base_url="https://propertyquarry.com")
    public_pdf = public_client.get(listed["artifact_download_path"])
    assert public_pdf.status_code == 200, public_pdf.text
    assert public_pdf.content.startswith(b"%PDF-1.4")

    archived = client.post(
        f"/app/api/properties/packets/{publication_id}/archive",
        json={"note": "No longer needed."},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["publication"]["status"] == "archived"
    assert archived.json()["publication"]["artifact_download_path"] == ""
    assert archived.json()["publication"]["public_pdf_path"] == ""
    revoked_public_pdf = public_client.get(listed["artifact_download_path"])
    assert revoked_public_pdf.status_code == 404
    listing_after_archive = client.get("/app/api/properties/packets")
    assert listing_after_archive.status_code == 200
    archived_row = next(item for item in listing_after_archive.json()["items"] if item["publication_id"] == publication_id)
    assert archived_row["status"] == "archived"
    assert archived_row["artifact_download_path"] == ""
    assert archived_row["public_pdf_path"] == ""
    events = client.get(f"/app/api/properties/packets/{publication_id}")
    assert events.status_code == 200
    assert any(event["event_type"] == "fliplink_publication_archived" for event in events.json()["events"])


def test_fliplink_manual_publish_enforces_privacy_and_sale_policy(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_ARTIFACTS_DIR", str(tmp_path))
    client = build_property_client(principal_id="fliplink-policy-owner")
    start_workspace(client, mode="personal", workspace_name="Policy Office")

    private_publication = _seed_packet(
        client,
        packet_kind="owner_review",
        privacy_mode="owner_private",
        fliplink_format="smart_document",
        property_ref="owner-private",
    )
    private_denied = client.post(
        f"/app/api/properties/packets/{private_publication}/fliplink/manual-link",
        json={"fliplink_url": "https://packets.propertyquarry.com/p/owner-private"},
    )
    assert private_denied.status_code == 422
    assert private_denied.json()["error"]["code"] == "owner_private_requires_password"

    family_publication = _seed_packet(client, property_ref="family-sale-blocked")
    sale_denied = client.post(
        f"/app/api/properties/packets/{family_publication}/fliplink/manual-link",
        json={
            "fliplink_url": "https://packets.propertyquarry.com/p/family-sale",
            "fliplink_format": "flipbook_3d",
            "sale_mode_enabled": True,
        },
    )
    assert sale_denied.status_code == 422
    assert sale_denied.json()["error"]["code"] == "sale_mode_requires_paid_market_report"

    report_publication = _seed_packet(
        client,
        packet_kind="paid_market_report",
        privacy_mode="paid_customer",
        fliplink_format="smart_document",
        property_ref="paid-report",
        payload={
            "title": "Vienna market report",
            "property_facts": {
                "district": "Vienna",
                "methodology": "Provider scan and benchmark model.",
                "freshness_date": "2026-06-06",
            },
        },
    )
    sale_ok = client.post(
        f"/app/api/properties/packets/{report_publication}/fliplink/manual-link",
        json={
            "fliplink_url": "https://reports.propertyquarry.com/r/vienna-market",
            "fliplink_format": "smart_document",
            "sale_mode_enabled": True,
        },
    )
    assert sale_ok.status_code == 200, sale_ok.text
    assert sale_ok.json()["publication"]["sale_mode_enabled"] is True


def test_fliplink_render_route_builds_candidate_score_methodology(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_ARTIFACTS_DIR", str(tmp_path))
    client = build_property_client(principal_id="fliplink-score-owner")
    start_workspace(client, mode="personal", workspace_name="FlipLink Score Office")

    publication_id = _seed_packet(
        client,
        property_ref="score-demo",
        payload={
            "title": "Score demo apartment",
            "fit_score": 62,
            "match_reasons": ["Echte 360-Tour vorhanden."],
            "mismatch_reasons": ["Heizungsdetail fehlt noch."],
            "property_facts": {
                "country_code": "AT",
                "language_code": "de",
                "rooms": 3,
                "area_m2": 82,
                "postal_name": "1020 Wien",
            },
        },
    )
    detail = client.get(f"/app/api/properties/packets/{publication_id}")
    assert detail.status_code == 200, detail.text
    redacted_payload = detail.json()["publication"]["packet_summary_json"]["redacted_payload"]
    score_methodology = redacted_payload["score_methodology"]

    assert redacted_payload["fit_score"] == 62.0
    assert score_methodology["language_code"] == "de"
    assert score_methodology["candidate_application"]["fit_score"] == 62
    assert score_methodology["candidate_application"]["band_label"] == "Starke Passung"
    assert score_methodology["candidate_application"]["positive_signals"] == ["Echte 360-Tour vorhanden."]
    assert score_methodology["calculation_detail_title"] == "Wo jede Zahl herkommt"
    assert any(row["delta"] == "+8" for row in score_methodology["calculation_detail_rows"])
    assert any(row["level"] == "Starker Wunsch" for row in score_methodology["weight_ladder_rows"])


def test_score_methodology_pdf_endpoint_uses_requested_language(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_ARTIFACTS_DIR", str(tmp_path))
    client = build_property_client(principal_id="score-methodology-owner")
    start_workspace(client, mode="personal", workspace_name="Score Guide Office", region="AT", language="de")

    response = client.get("/app/api/properties/score-methodology/pdf?language=de")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["x-propertyquarry-renderer"] == "fliplink-legacy-score"
    assert 'inline; filename="propertyquarry-score-methodology-de.pdf"' == response.headers.get("content-disposition", "")
    assert response.content.startswith(b"%PDF-1.4")
    pdf_text = response.content.decode("latin-1", errors="ignore")
    assert "Wie der PropertyQuarry-Score berechnet" in pdf_text
    assert "62/100" in pdf_text
    assert "Beispielrechnung" in pdf_text
    assert "50 + 8 + 10 + 6 + 0 - 8 - 3 - 5 = 58" in pdf_text
    assert "Wo jede Zahl herkommt" in pdf_text
    assert "starker Wunsch etwa +12" in pdf_text
    assert "Wünschenswert etwa -3" in pdf_text
    assert "Engine steps" not in pdf_text
    assert "Examples" not in pdf_text


def test_score_methodology_pdf_endpoint_supports_every_country_provider_language(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_ARTIFACTS_DIR", str(tmp_path))
    client = build_property_client(principal_id="score-methodology-language-owner")
    start_workspace(client, mode="personal", workspace_name="Score Guide Languages", region="AT", language="de")

    for language_code in supported_property_score_methodology_languages():
        response = client.get(f"/app/api/properties/score-methodology/pdf?language={language_code}")
        assert response.status_code == 200, f"{language_code}: {response.text}"
        assert response.headers["content-type"].startswith("application/pdf")
        assert response.headers["x-propertyquarry-renderer"] == "fliplink-legacy-score"
        assert (
            f'inline; filename="propertyquarry-score-methodology-{language_code}.pdf"'
            == response.headers.get("content-disposition", "")
        )
        assert response.content.startswith(b"%PDF-1.4")
        pdf_text = response.content.decode("latin-1", errors="ignore")
        assert "62/100" in pdf_text


def test_fliplink_browseract_publish_request_is_guarded_and_audited(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("FLIPLINK_BROWSERACT_ENABLED", "0")
    client = build_property_client(principal_id="fliplink-browseract-owner")
    start_workspace(client, mode="personal", workspace_name="FlipLink BrowserAct Office")
    publication_id = _seed_packet(client)

    disabled = client.post(f"/app/api/properties/packets/{publication_id}/fliplink/browseract-publish", json={})
    assert disabled.status_code == 409

    monkeypatch.setenv("FLIPLINK_BROWSERACT_ENABLED", "1")
    monkeypatch.setenv("FLIPLINK_LOGIN_EMAIL", "fliplink-browseract@example.test")
    monkeypatch.setenv("FLIPLINK_LOGIN_PASSWORD", "test-only-password")
    queued = client.post(
        f"/app/api/properties/packets/{publication_id}/fliplink/browseract-publish",
        json={"lead_capture_enabled": True, "password_required": False},
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "queued_operator_assist"
    assert queued.json()["task_name"] == "browseract.fliplink_publish_property_packet"
    assert queued.json()["contract_version"] == "fliplink_browseract_publish_v1"
    assert queued.json()["required_outputs"] == ["fliplink_url", "screenshot_proof_ref"]
    assert queued.json()["completion_payload_schema"]["required"] == ["fliplink_url", "screenshot_proof_ref"]
    assert queued.json()["runner_payload"]["publication_id"] == publication_id
    assert queued.json()["runner_payload"]["completion"]["endpoint"] == queued.json()["completion_endpoint"]
    assert queued.json()["runner_payload"]["proof_policy"]["screenshot_proof_ref_required"] is True
    assert queued.json()["human_task_id"]
    assert queued.json()["queue_item_ref"] == f"human_task:{queued.json()['human_task_id']}"
    assert queued.json()["completion_endpoint"] == f"/app/api/properties/packets/{publication_id}/fliplink/browseract-complete"
    task_listing = client.get("/v1/human/tasks", params={"limit": 20})
    assert task_listing.status_code == 200, task_listing.text
    matching_tasks = [
        item for item in task_listing.json()
        if item["task_type"] == "fliplink_browseract_publish"
        and item["input_json"]["publication_id"] == publication_id
    ]
    assert len(matching_tasks) == 1
    task = matching_tasks[0]
    assert task["input_json"]["pdf_artifact_ref"]
    assert task["input_json"]["source_pdf_sha256"]
    assert task["input_json"]["completion_endpoint"] == queued.json()["completion_endpoint"]
    assert task["input_json"]["contract_version"] == "fliplink_browseract_publish_v1"
    assert task["input_json"]["required_outputs"] == ["fliplink_url", "screenshot_proof_ref"]
    assert task["input_json"]["completion_payload_schema"]["required"] == ["fliplink_url", "screenshot_proof_ref"]
    assert task["input_json"]["browseract_runner_payload"] == queued.json()["runner_payload"]
    assert task["input_json"]["browseract_runner_payload"]["source_pdf_sha256"] == task["input_json"]["source_pdf_sha256"]
    assert task["desired_output_json"]["contract_version"] == "fliplink_browseract_publish_v1"
    assert task["desired_output_json"]["required_outputs"] == ["fliplink_url", "screenshot_proof_ref"]
    assert task["quality_rubric_json"]["must_follow_contract_version"] == "fliplink_browseract_publish_v1"
    assert task["quality_rubric_json"]["must_return_required_outputs"] == ["fliplink_url", "screenshot_proof_ref"]
    assert task["quality_rubric_json"]["must_not_upload_unredacted_source_payload"] is True

    duplicate = client.post(
        f"/app/api/properties/packets/{publication_id}/fliplink/browseract-publish",
        json={"lead_capture_enabled": True, "password_required": False},
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["human_task_id"] == queued.json()["human_task_id"]
    assert duplicate.json()["deduplicated"] is True
    events = client.get(f"/app/api/properties/packets/{publication_id}")
    assert events.status_code == 200
    assert any(event["event_type"] == "fliplink_browser_publish_requested" for event in events.json()["events"])

    missing_proof = client.post(
        f"/app/api/properties/packets/{publication_id}/fliplink/browseract-complete",
        json={
            "fliplink_url": "https://packets.propertyquarry.com/p/browseract-family",
            "lead_capture_enabled": True,
        },
    )
    assert missing_proof.status_code == 422
    assert "browseract_screenshot_proof_required" in missing_proof.text

    completed = client.post(
        f"/app/api/properties/packets/{publication_id}/fliplink/browseract-complete",
        json={
            "fliplink_url": "https://packets.propertyquarry.com/p/browseract-family",
            "screenshot_proof_ref": "artifact:screenshot.png",
            "qr_url": "https://packets.propertyquarry.com/qr/browseract-family.png",
            "lead_capture_enabled": True,
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["publication"]["status"] == "published"
    task_listing_after = client.get("/v1/human/tasks", params={"status": "returned", "limit": 20})
    assert task_listing_after.status_code == 200, task_listing_after.text
    returned_tasks = [
        item for item in task_listing_after.json()
        if item["task_type"] == "fliplink_browseract_publish"
        and item["input_json"]["publication_id"] == publication_id
    ]
    assert len(returned_tasks) == 1
    assert returned_tasks[0]["resolution"] == "published"
    assert returned_tasks[0]["returned_payload_json"]["fliplink_url"] == "https://packets.propertyquarry.com/p/browseract-family"
    assert returned_tasks[0]["returned_payload_json"]["contract_version"] == "fliplink_browseract_publish_v1"
    events_after = client.get(f"/app/api/properties/packets/{publication_id}")
    assert any(event["event_type"] == "fliplink_browser_publish_completed" for event in events_after.json()["events"])
    assert any(event["event_type"] == "fliplink_browser_publish_task_closed" for event in events_after.json()["events"])
    completed_event = next(
        event for event in events_after.json()["events"]
        if event["event_type"] == "fliplink_browser_publish_completed"
    )
    assert completed_event["payload_json"]["contract_version"] == "fliplink_browseract_publish_v1"

    republish = client.post(
        f"/app/api/properties/packets/{publication_id}/fliplink/browseract-publish",
        json={"lead_capture_enabled": True, "password_required": False},
    )
    assert republish.status_code == 200, republish.text
    assert republish.json()["status"] == "published_existing"
    all_tasks = client.get("/v1/human/tasks", params={"limit": 50})
    browseract_tasks = [
        item for item in all_tasks.json()
        if item["task_type"] == "fliplink_browseract_publish"
        and item["input_json"]["publication_id"] == publication_id
    ]
    assert len(browseract_tasks) == 1


def test_fliplink_packet_capacity_blocks_new_renders_until_archive(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("FLIPLINK_ACTIVE_PUBLICATION_CAP", "1")
    client = build_property_client(principal_id="fliplink-capacity-owner")
    start_workspace(client, mode="personal", workspace_name="FlipLink Capacity Office")

    publication_id = _seed_packet(client)
    listing = client.get("/app/api/properties/packets")
    assert listing.status_code == 200
    assert listing.json()["capacity"]["state"] == "blocked"
    assert listing.json()["capacity"]["active"] == 1
    assert listing.json()["capacity"]["global_active"] == 1
    assert listing.json()["capacity"]["principal_active"] == 1

    blocked = client.post(
        "/app/api/properties/listing-456/packets/render",
        json={
            "packet_kind": "family_review",
            "privacy_mode": "family_review",
            "property_payload": {"title": "Second packet"},
        },
    )
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "fliplink_active_publication_cap_reached"

    archived = client.post(f"/app/api/properties/packets/{publication_id}/archive", json={})
    assert archived.status_code == 200
    second = client.post(
        "/app/api/properties/listing-456/packets/render",
        json={
            "packet_kind": "family_review",
            "privacy_mode": "family_review",
            "property_payload": {"title": "Second packet"},
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["capacity"]["active"] == 1
    assert second.json()["capacity"]["global_remaining"] == 0


def test_fliplink_webhook_requires_secret_and_records_untrusted_feedback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("FLIPLINK_WEBHOOK_SECRET", "webhook-secret")
    client = build_property_client(principal_id="fliplink-webhook-owner")
    start_workspace(client, mode="personal", workspace_name="FlipLink Webhook Office")
    publication_id = _seed_packet(client)

    denied = client.post("/v1/integrations/fliplink/webhook", json={"publication_id": publication_id})
    assert denied.status_code == 401

    query_secret_denied = client.post(
        f"/v1/integrations/fliplink/webhook?secret=webhook-secret",
        json={"publication_id": publication_id},
    )
    assert query_secret_denied.status_code == 401
    assert query_secret_denied.json()["error"]["code"] == "fliplink_webhook_query_secret_disabled"

    oversized = client.post(
        "/v1/integrations/fliplink/webhook",
        headers={"x-propertyquarry-webhook-secret": "webhook-secret", "content-type": "application/json"},
        content=b'{"publication_id":"' + publication_id.encode("utf-8") + b'","blob":"' + (b"x" * 65_000) + b'"}',
    )
    assert oversized.status_code == 413

    accepted = client.post(
        "/v1/integrations/fliplink/webhook",
        headers={"x-propertyquarry-webhook-secret": "webhook-secret"},
        json={
            "publication_id": publication_id,
            "name": "Alex Reviewer",
            "email": "alex@example.com",
            "custom_fields": {
                "viewer_role": "family",
                "reaction": "maybe",
                "question": "How noisy is the street?",
                "rawNested": {"must": "not persist"},
                "unexpected_field": "not needed",
            },
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["trust"] == "untrusted_external"
    assert accepted.json()["secret_mode"] == "header"

    inbox = client.get("/app/api/properties/packets/feedback-inbox")
    assert inbox.status_code == 200
    items = inbox.json()["items"]
    assert items
    assert items[0]["trust"] == "untrusted_external"
    assert items[0]["status"] == "pending_owner_review"
    assert items[0]["reviewer"]["email_masked"] == "al***@example.com"
    assert "alex@example.com" not in str(items[0])
    assert items[0]["custom_fields"]["question"] == "How noisy is the street?"
    assert items[0]["custom_fields"]["custom_fields_extra_redacted"] is True
    assert "rawNested" not in str(items[0])

    viewing = client.post(
        f"/app/api/properties/packets/feedback/{items[0]['event_id']}/review",
        json={"action": "accept_as_viewing_question", "note": "Ask about road noise at the viewing."},
    )
    assert viewing.status_code == 200, viewing.text
    assert viewing.json()["viewing_question_event"]["event_type"] == "fliplink_viewing_question_accepted"

    reviewed = client.post(
        f"/app/api/properties/packets/feedback/{items[0]['event_id']}/review",
        json={"action": "convert_to_hard_rule", "note": "Quiet micro-location should be a hard screening rule."},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "reviewed"
    applied = reviewed.json()["preference_application"]
    assert applied["hard_rule_node"]["key"] == "require_quiet_micro_location"
    assert applied["hard_rule_node"]["category"] == "constraint"
    bundle = client.app.state.container.preference_profiles.get_profile_bundle(
        principal_id="fliplink-webhook-owner",
        person_id="self",
    )
    nodes_by_key = {node["key"]: node for node in bundle["preference_nodes"]}
    assert nodes_by_key["require_quiet_micro_location"]["value_json"] is True
    recent_event = bundle["recent_evidence_events"][0]
    assert recent_event["domain"] == "property"
    assert recent_event["event_type"] == "feedback_inbox_accepted"
    assert recent_event["object_type"] == "feedback"

    unmatched = client.post(
        "/v1/integrations/fliplink/webhook",
        headers={"x-propertyquarry-webhook-secret": "webhook-secret"},
        json={"publication_id": "missing-publication", "url": "https://packets.propertyquarry.com/p/missing"},
    )
    assert unmatched.status_code == 200, unmatched.text
    assert unmatched.json()["status"] == "accepted_unmatched"
    repo_events = build_property_packet_publication_repository(client.app.state.container.settings).list_events(
        event_type="fliplink_webhook_unmatched"
    )
    assert repo_events
    assert repo_events[0]["payload_json"]["publication_id_present"] is True

    monkeypatch.setenv("FLIPLINK_WEBHOOK_ALLOW_QUERY_SECRET", "1")
    query_accepted = client.post(
        f"/v1/integrations/fliplink/webhook?secret=webhook-secret",
        json={"publication_id": publication_id, "custom_fields": {"viewer_role": "agent", "intent": "viewing"}},
    )
    assert query_accepted.status_code == 200, query_accepted.text
    assert query_accepted.json()["secret_mode"] == "query"


def test_fliplink_packet_dashboard_and_property_actions_render(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_ARTIFACTS_DIR", str(tmp_path))
    client = build_property_client(principal_id="fliplink-dashboard-owner")
    start_workspace(client, mode="personal", workspace_name="FlipLink Dashboard")
    publication_id = _seed_packet(client)
    feedback = client.post(
        "/app/api/property-feedback",
        json={
            "stakeholder_id": "family-anna",
            "stakeholder_label": "Anna",
            "property_ref": "listing-123",
            "publication_id": publication_id,
            "category": "dealbreaker",
            "sentiment": "negative",
            "importance": 5,
            "text": "Street noise feels like a blocker.",
            "decision_state": "rejected",
        },
    )
    assert feedback.status_code == 200, feedback.text

    dashboard = client.get("/app/properties/packets", headers={"host": "propertyquarry.com"})
    assert dashboard.status_code == 200, dashboard.text
    assert "data-property-packets-dashboard" in dashboard.text
    assert "data-property-research-topnav" in dashboard.text
    assert 'aria-label="Account navigation"' in dashboard.text
    assert "Saved packets" in dashboard.text
    assert "Save local review packets and keep the replies together." in dashboard.text
    assert "Viewer responses" in dashboard.text
    assert "FlipLink leads" not in dashboard.text
    assert "Leads" not in dashboard.text
    assert "Family flat near Augarten" in dashboard.text
    assert "Download PDF" in dashboard.text
    assert "Save views" in dashboard.text
    assert "data-fliplink-manual-form" in dashboard.text
    assert "data-copy-kind=\"webhook\"" in dashboard.text
    assert "data-copy-lead-schema" in dashboard.text
    assert "data-browseract-publish" in dashboard.text
    assert "data-archive-publication" in dashboard.text
    assert "Reactions" in dashboard.text

    run_bound = client.post(
        "/app/api/properties/listing-456/packets/render",
        json={
            "packet_kind": "family_review",
            "privacy_mode": "family_review",
            "fliplink_format": "flipbook_3d",
            "search_run_id": "run-packets-ctx",
            "property_payload": {
                "title": "Run-bound villa",
                "property_url": "https://www.willhaben.at/iad/immobilien/d/demo-run-bound",
                "match_reasons": ["Run context should stay attached."],
                "property_facts": {"rooms": 5, "area_m2": 140, "postal_name": "1190 Wien"},
            },
        },
    )
    assert run_bound.status_code == 200, run_bound.text

    dashboard_with_run = client.get("/app/properties/packets", params={"run_id": "run-packets-ctx"}, headers={"host": "propertyquarry.com"})
    assert 'href="/app/properties?run_id=run-packets-ctx"' in dashboard_with_run.text
    assert 'href="/app/shortlist?run_id=run-packets-ctx"' in dashboard_with_run.text
    assert 'href="/app/account?run_id=run-packets-ctx#search-defaults"' in dashboard_with_run.text
    assert 'href="/app/account?run_id=run-packets-ctx#connected-services"' in dashboard_with_run.text
    assert "Notes" in dashboard.text
    assert "What changed" in dashboard.text
    assert "data-feedback-action=\"accept_as_preference_signal\"" in dashboard.text or "No viewer responses" in dashboard.text

    properties = client.get("/app/properties", headers={"host": "propertyquarry.com"})
    assert properties.status_code == 200
    assert "data-property-research-topnav" in properties.text
    assert "Adjust search" in properties.text
    assert "Research" in properties.text
