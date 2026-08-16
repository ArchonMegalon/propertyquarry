from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.propertyquarry_phase_helpers import (
    install_property_run,
    property_client_with_workspace,
    reset_packet_repo,
    seed_packet,
    seed_property_search_preferences,
)
from tests.e2e.test_propertyquarry_greenfield_browser import browser, propertyquarry_browser_server


@pytest.fixture(autouse=True)
def _reset_repo() -> None:
    reset_packet_repo()


def test_packet_dashboard_renders_share_and_followup_state(monkeypatch, tmp_path: Path) -> None:
    client = property_client_with_workspace(principal_id="pq-phase1-browser", tmp_path=tmp_path)
    publication_id = seed_packet(client, property_ref="listing-phase1-browser")
    share = client.post(
        f"/app/api/properties/packets/{publication_id}/shares",
        json={"audience_type": "family", "channel": "link", "recipients": [{"name": "Anna", "email": "anna@example.com"}]},
    )
    recipient_id = share.json()["share"]["recipients"][0]["recipient_id"]
    share_id = share.json()["share"]["share_id"]
    client.post(
        f"/app/api/properties/packets/{publication_id}/engagement-events",
        json={"share_id": share_id, "recipient_id": recipient_id, "event_type": "opened"},
    )

    page = client.get("/app/properties/packets")
    assert page.status_code == 200
    assert "Save local review packets and keep the replies together." in page.text
    assert "Recipients" in page.text
    assert "Follow-ups" in page.text
    assert "Next step:" in page.text


def test_packet_dashboard_reviews_feedback_in_real_browser(
    browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    publication_id = seed_packet(client, property_ref="listing-phase1-browser")
    feedback = client.post(
        "/v1/integrations/fliplink/webhook",
        headers={"x-propertyquarry-webhook-secret": "webhook-secret"},
        json={
            "stakeholder_id": "family-bob",
            "publication_id": publication_id,
            "name": "Bob",
            "email": "bob@example.com",
            "custom_fields": {
                "viewer_role": "family",
                "reaction": "maybe",
                "question": "How noisy is the street?",
            },
        },
    )
    assert feedback.status_code == 200, feedback.text

    base_url = str(propertyquarry_browser_server["base_url"])
    context = browser.new_context(
        viewport={"width": 1440, "height": 1100},
        extra_http_headers={"X-EA-Principal-ID": "pq-greenfield-browser"},
    )
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/properties/packets", wait_until="networkidle")
        assert response is not None and response.ok
        assert page.locator("[data-feedback-review]").is_visible()
        assert page.locator("body", has_text="How noisy is the street?").is_visible()
        with page.expect_response("**/app/api/properties/packets/feedback/*/review") as review_response_info:
            page.locator('[data-feedback-action="convert_to_hard_rule"]').first.click()
        review_response = review_response_info.value
        assert review_response.ok, review_response.text()
        page.wait_for_function(
            "Array.from(document.querySelectorAll('[data-feedback-status]')).some((node) => (node.textContent || '').toLowerCase().includes('reviewed'))"
        )
        page.wait_for_function(
            "Array.from(document.querySelectorAll('[data-feedback-message]')).some((node) => (node.textContent || '').includes('Rule saved:'))"
        )
        assert page.locator("[data-feedback-status]", has_text="reviewed").first.is_visible()
    finally:
        context.close()


def test_workspace_shortlist_surfaces_packet_followup_entry(monkeypatch, tmp_path: Path) -> None:
    client = property_client_with_workspace(principal_id="pq-phase1-workspace", tmp_path=tmp_path)
    seed_property_search_preferences(client)
    install_property_run(monkeypatch, property_url="https://example.com/listing-1")
    page = client.get("/app/properties", params={"run_id": "run-phase1"})
    assert page.status_code == 200
    assert "Open property" in page.text
    assert "More" in page.text
