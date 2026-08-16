from __future__ import annotations

from tests.product_test_helpers import (
    seed_executive_operator_fixture,
    seed_founder_fixture,
    seed_team_fixture,
)


def test_founder_fixture_supports_first_value_loop() -> None:
    client, _ = seed_founder_fixture()

    diagnostics = client.get("/app/api/diagnostics")
    assert diagnostics.status_code == 200
    body = diagnostics.json()
    assert body["workspace"]["mode"] == "personal"
    assert body["plan"]["plan_key"] == "workspace_personal"
    assert body["billing"]["authority"] == "property_billing_service"
    assert body["billing"]["billing_state"] == "non_authoritative_workspace_mode"
    assert body["billing"]["support_tier"] == "guided"
    assert "google" in body["selected_channels"]

    bundle = client.get("/app/api/diagnostics/export")
    assert bundle.status_code == 403

    today = client.get("/app/today")
    assert today.status_code == 200
    assert "Search brief" in today.text


def test_executive_operator_fixture_supports_admin_and_handoff_loop() -> None:
    client, seeded = seed_executive_operator_fixture()

    diagnostics = client.get("/app/api/diagnostics")
    assert diagnostics.status_code == 200
    body = diagnostics.json()
    assert body["workspace"]["mode"] == "executive_ops"
    assert body["plan"]["plan_key"] == "workspace_managed"
    assert body["billing"]["billing_state"] == "non_authoritative_workspace_mode"
    assert body["billing"]["support_tier"] == "priority"

    operators = client.get("/admin/operators")
    assert operators.status_code == 200
    assert "Operators" in operators.text
    assert seeded["human_task_id"] in client.get("/app/api/handoffs").text

    bundle = client.get("/app/api/diagnostics/export")
    assert bundle.status_code == 200
    assert bundle.json()["billing"]["renewal_owner_role"] == "account_lead"


def test_team_fixture_supports_shared_operator_shape() -> None:
    client, _ = seed_team_fixture()

    diagnostics = client.get("/app/api/diagnostics")
    assert diagnostics.status_code == 200
    body = diagnostics.json()
    assert body["workspace"]["mode"] == "team"
    assert body["plan"]["plan_key"] == "workspace_team"
    assert body["billing"]["billing_state"] == "non_authoritative_workspace_mode"
    assert body["billing"]["support_tier"] == "standard"
    assert body["operators"]["active_count"] >= 2
    assert "telegram" in body["selected_channels"]

    bundle = client.get("/app/api/diagnostics/export")
    assert bundle.status_code == 200
    assert bundle.json()["billing"]["renewal_owner_role"] == "office_admin"
