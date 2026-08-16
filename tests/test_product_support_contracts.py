from __future__ import annotations

from tests.product_test_helpers import build_operator_product_client, build_product_client, seed_product_state, start_workspace


def _seed(principal_id: str = "exec-support-contracts"):
    client = build_operator_product_client(principal_id=principal_id, operator_id="operator-office")
    start_workspace(client, mode="executive_ops", workspace_name="Executive Office")
    seeded = seed_product_state(client, principal_id=principal_id)
    return client, seeded


def test_support_surfaces_require_operator_context() -> None:
    client = build_product_client(principal_id="exec-support-principal")
    start_workspace(client, mode="personal", workspace_name="Founder Office")

    assert client.get("/app/settings/support").status_code == 200
    assert client.get("/app/api/support").status_code == 403
    assert client.get("/app/api/diagnostics/export").status_code == 403
    assert client.post("/app/api/support/fix-verification/request").status_code == 403


def test_surface_open_events_flow_into_workspace_diagnostics() -> None:
    client, _seeded = _seed("exec-diagnostics-events")

    assert client.get("/register").status_code == 200
    assert client.get("/app/today").status_code == 200
    assert client.get("/app/settings").status_code == 200
    assert client.get("/app/api/plan").status_code == 200
    assert client.get("/app/api/usage").status_code == 200
    assert client.get("/app/api/support").status_code == 200
    assert client.get("/app/channel-loop/memo").status_code == 200
    assert client.get("/app/channel-loop/operator").status_code == 200
    assert client.get("/app/channel-loop/memo/plain").status_code == 200

    diagnostics = client.get("/app/api/diagnostics")
    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    counts = dict(payload["analytics"]["counts"])
    assert counts.get("activation_opened", 0) >= 1
    assert counts.get("account_opened", 0) >= 1
    assert counts.get("plan_opened", 0) >= 1
    assert counts.get("usage_opened", 0) >= 1
    assert counts.get("support_opened", 0) >= 1
    assert counts.get("channel_digest_opened", 0) >= 2
    assert counts.get("channel_digest_plain_opened", 0) >= 1
    assert payload["billing"]["invoice_status"] == "property_billing_authoritative"
    assert "risk_state" in payload["providers"]
    assert "blocked_actions" in payload["commercial"]
    assert "blocked_action_message" in payload["commercial"]
    assert "load_score" in payload["queue_health"]
    assert "retrying_delivery" in payload["queue_health"]
    assert payload["product_control"]["summary"]
    assert payload["product_control"]["journey_gate_health"]["state"]
    assert "support_fallout" in payload["product_control"]
    assert "public_guide_freshness" in payload["product_control"]
    assert "state" in payload["support_verification"]
    assert "churn_risk" in payload["analytics"]
    assert "success_summary" in payload["analytics"]


def test_support_bundle_export_includes_commercial_state_and_records_event() -> None:
    client, _seeded = _seed("exec-support-bundle")

    signal = client.post(
        "/app/api/signals/ingest",
        json={
            "signal_type": "calendar_note",
            "channel": "calendar",
            "title": "Board prep",
            "summary": "Confirm the board memo owner before the afternoon meeting.",
            "source_ref": "calendar-event-1",
            "external_id": "calendar-note-1",
        },
    )
    assert signal.status_code == 200

    export = client.get("/app/api/diagnostics/export")
    assert export.status_code == 200
    body = export.json()
    assert body["plan"]["display_name"] == "Managed workspace"
    assert body["billing"]["support_tier"] == "priority"
    assert body["billing"]["billing_portal_state"] == "property_billing_authoritative"
    assert body["entitlements"]["operator_seats"] >= 1
    assert body["analytics"]["counts"].get("support_bundle_opened", 0) >= 1
    assert "queue_health" in body
    assert "assignment_suggestions" in body
    assert "sla_breaches" in body["queue_health"]
    assert "unclaimed_handoffs" in body["queue_health"]
    assert "load_score" in body["queue_health"]
    assert "retrying_delivery" in body["queue_health"]
    assert "risk_state" in body["providers"]
    assert "blocked_action_message" in body["commercial"]
    assert "pending" in body["approvals"]
    assert isinstance(body["human_tasks"], list)
    assert body["product_control"]["summary"]
    assert "journey_gate_freshness" in body["product_control"]
    assert "support_fallout" in body["product_control"]
    assert "public_guide_freshness" in body["product_control"]
    assert "state" in body["support_verification"]
    assert "success_summary" in body["analytics"]
    assert isinstance(body["recent_events"], list)
    assert any(item["event_type"] == "office_signal_calendar_note" for item in body["recent_events"])

    diagnostics = client.get("/app/api/diagnostics")
    assert diagnostics.status_code == 200
    counts = diagnostics.json()["analytics"]["counts"]
    assert counts.get("support_bundle_opened", 0) >= 1


def test_support_bundle_download_sets_attachment_headers_and_records_event() -> None:
    client, _seeded = _seed("exec-support-bundle-download")

    export = client.get("/app/api/diagnostics/export", params={"download": "1"})
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("application/json")
    content_disposition = str(export.headers.get("content-disposition") or "")
    assert "attachment;" in content_disposition
    assert "support-bundle" in content_disposition
    body = export.json()
    assert body["workspace"]["name"] == "Executive Office"

    diagnostics = client.get("/app/api/diagnostics")
    assert diagnostics.status_code == 200
    counts = diagnostics.json()["analytics"]["counts"]
    assert counts.get("support_bundle_downloaded", 0) >= 1


def test_people_history_endpoint_reflects_memory_corrections() -> None:
    client, seeded = _seed("exec-people-history")
    person_id = seeded["stakeholder_id"]

    correction = client.post(
        f"/app/actions/people/{person_id}/correct",
        data={
            "preferred_tone": "warmer",
            "add_theme": "board packet",
            "add_risk": "travel coordination",
            "return_to": f"/app/people/{person_id}",
        },
        follow_redirects=False,
    )
    assert correction.status_code == 303

    detail = client.get(f"/app/api/people/{person_id}")
    assert detail.status_code == 200
    profile = detail.json()
    assert profile["preferred_tone"] == "warmer"
    assert "board packet" in profile["themes"]
    assert "travel coordination" in profile["risks"]

    history = client.get(f"/app/api/people/{person_id}/detail/history")
    assert history.status_code == 200
    entries = history.json()
    assert any(entry["event_type"] == "memory_corrected" for entry in entries)
