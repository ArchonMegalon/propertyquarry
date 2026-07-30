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


def test_workspace_and_packet_dashboard_show_commercial_and_page_idea_language(monkeypatch, tmp_path: Path) -> None:
    client = property_client_with_workspace(principal_id="pq-phase6-browser", tmp_path=tmp_path)
    publication_id = seed_packet(client, property_ref="listing-phase6-browser")
    client.post(
        f"/app/api/properties/packets/{publication_id}/fliplink/analytics-snapshot",
        json={"views": 8, "unique_visitors": 2, "average_time_seconds": 55},
    )
    seed_property_search_preferences(client)
    empty_workspace = client.get("/app/properties")
    assert empty_workspace.status_code == 200
    assert "Premium next step" not in empty_workspace.text
    assert "Open checkout" not in empty_workspace.text

    install_property_run(monkeypatch, property_url="https://example.com/listing-phase6")
    workspace = client.get("/app/properties", params={"run_id": "run-phase6"})
    assert workspace.status_code == 200
    assert "At a glance" in workspace.text
    assert "Premium next step" in workspace.text
    assert "Open checkout" in workspace.text
    assert "Account" in workspace.text

    packets = client.get("/app/properties/packets")
    assert packets.status_code == 200
    assert "Page ideas" in packets.text


def test_agent_workspace_does_not_offer_already_included_premium_steps(
    monkeypatch,
    tmp_path: Path,
) -> None:
    principal_id = "pq-agent-no-premium-offer"
    client = property_client_with_workspace(
        principal_id=principal_id,
        tmp_path=tmp_path,
    )
    client.app.state.container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json={
            "country_code": "AT",
            "listing_mode": "rent",
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
        trusted_commercial_update=True,
    )
    install_property_run(
        monkeypatch,
        property_url="https://example.com/agent-listing",
    )

    workspace = client.get(
        "/app/properties",
        params={"run_id": "run-phase6"},
    )

    assert workspace.status_code == 200
    assert "At a glance" in workspace.text
    assert "Premium next step" not in workspace.text
    assert "Open checkout" not in workspace.text


def test_packet_dashboard_acknowledges_page_ideas_in_real_browser(
    browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    monkeypatch = pytest.MonkeyPatch()
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    publication_id = seed_packet(client, property_ref="listing-phase6-browser")
    share = client.post(
        f"/app/api/properties/packets/{publication_id}/shares",
        json={"audience_type": "family", "channel": "link", "recipients": [{"name": "Omar", "email": "omar@example.com"}]},
    )
    assert share.status_code == 200, share.text
    share_id = share.json()["share"]["share_id"]
    recipient_id = share.json()["share"]["recipients"][0]["recipient_id"]
    engaged = client.post(
        f"/app/api/properties/packets/{publication_id}/engagement-events",
        json={"share_id": share_id, "recipient_id": recipient_id, "event_type": "opened"},
    )
    assert engaged.status_code == 200, engaged.text
    analytics = client.post(
        f"/app/api/properties/packets/{publication_id}/fliplink/analytics-snapshot",
        json={"views": 12, "unique_visitors": 4, "average_time_seconds": 90, "device_breakdown": {"mobile": 7, "desktop": 2}},
    )
    assert analytics.status_code == 200, analytics.text
    seed_property_search_preferences(client)
    install_property_run(monkeypatch, property_url="https://example.com/listing-phase6")

    base_url = str(propertyquarry_browser_server["base_url"])
    context = browser.new_context(
        viewport={"width": 1440, "height": 1100},
        extra_http_headers={"X-EA-Principal-ID": "pq-greenfield-browser"},
    )
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/properties?run_id=run-phase6", wait_until="networkidle")
        assert response is not None and response.ok
        assert page.locator("body", has_text="Upgrade").is_visible()
        checkout_link = page.get_by_role("link", name="Open checkout").first
        assert checkout_link.is_visible()
        checkout_href = checkout_link.get_attribute("href")
        assert checkout_href and "/pricing?offer_id=" in checkout_href
        assert page.locator("body", has_text="Account").is_visible()

        response = page.goto(f"{base_url}{checkout_href}", wait_until="networkidle")
        assert response is not None and response.ok
        assert page.locator("body", has_text="Free").is_visible()

        response = page.goto(f"{base_url}/app/properties/packets", wait_until="networkidle")
        assert response is not None and response.ok
        assert page.locator("[data-property-packets-dashboard]").is_visible()
        assert page.locator("body", has_text="Page ideas").is_visible()
        assert page.locator("body", has_text="followup · high · open").is_visible()
        assert page.locator("body", has_text="mobile readability · medium · open").is_visible()

        optimization = client.get(f"/app/api/properties/packets/{publication_id}/optimization")
        assert optimization.status_code == 200, optimization.text
        recommendation_id = optimization.json()["items"][0]["recommendation_id"]
        ack_result = page.evaluate(
            """async ({ publicationId, recommendationId }) => {
              const response = await fetch(`/app/api/properties/packets/${encodeURIComponent(publicationId)}/optimization/ack`, {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ recommendation_id: recommendationId }),
              });
              const body = await response.json();
              return { ok: response.ok, body };
            }""",
            {"publicationId": publication_id, "recommendationId": recommendation_id},
        )
        assert ack_result["ok"] is True

        page.reload(wait_until="networkidle")
        assert page.locator("body", has_text="acknowledged").is_visible()
    finally:
        context.close()
        monkeypatch.undo()
