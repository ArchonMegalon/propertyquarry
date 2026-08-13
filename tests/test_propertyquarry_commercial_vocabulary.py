from __future__ import annotations

from ea.app.product.commercial import (
    workspace_commercial_snapshot,
    workspace_plan_for_mode,
)


def test_workspace_modes_are_not_propertyquarry_billing_plans() -> None:
    plans = [
        workspace_plan_for_mode("personal"),
        workspace_plan_for_mode("team"),
        workspace_plan_for_mode("executive_ops"),
    ]

    assert [plan.plan_key for plan in plans] == [
        "workspace_personal",
        "workspace_team",
        "workspace_managed",
    ]
    assert all(plan.billing_state == "non_authoritative_workspace_mode" for plan in plans)
    assert all(plan.billing_portal_path == "/app/billing" for plan in plans)
    assert not {"pilot", "core", "executive"} & {plan.plan_key for plan in plans}


def test_workspace_snapshot_defers_to_property_billing_authority() -> None:
    snapshot = workspace_commercial_snapshot(
        workspace_plan_for_mode("personal"),
        seats_used=1,
        selected_channels=("telegram",),
    )

    assert snapshot["billing"]["authority"] == "property_billing_service"
    assert snapshot["billing"]["invoice_status"] == "property_billing_authoritative"
    assert snapshot["billing"]["billing_portal_path"] == "/app/billing"
    assert "Pilot" not in " ".join(snapshot["commercial"]["warnings"])
