from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlanEntitlements:
    principal_seats: int
    operator_seats: int
    messaging_channels_enabled: bool
    audit_retention: str
    feature_flags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WorkspacePlan:
    plan_key: str
    display_name: str
    unit_of_sale: str
    entitlements: PlanEntitlements
    support_tier: str
    billing_state: str
    price_label: str
    billing_cadence: str
    invoice_window_label: str
    renewal_window_label: str
    billing_portal_state: str
    billing_portal_path: str
    upgrade_target_mode: str
    renewal_owner_role: str
    contract_note: str


_PLANS = {
    "personal": WorkspacePlan(
        plan_key="workspace_personal",
        display_name="Personal workspace",
        unit_of_sale="workspace",
        entitlements=PlanEntitlements(
            principal_seats=1,
            operator_seats=1,
            messaging_channels_enabled=False,
            audit_retention="30d",
            feature_flags=("market_update", "review_queue", "follow-up_ledger", "review_workflow"),
        ),
        support_tier="guided",
        billing_state="non_authoritative_workspace_mode",
        price_label="PropertyQuarry plan is managed separately",
        billing_cadence="not_applicable",
        invoice_window_label="See PropertyQuarry billing",
        renewal_window_label="See PropertyQuarry billing",
        billing_portal_state="property_billing_authoritative",
        billing_portal_path="/app/billing",
        upgrade_target_mode="team",
        renewal_owner_role="principal",
        contract_note="Personal workspace mode; this object does not grant a paid PropertyQuarry plan.",
    ),
    "team": WorkspacePlan(
        plan_key="workspace_team",
        display_name="Shared workspace",
        unit_of_sale="workspace",
        entitlements=PlanEntitlements(
            principal_seats=1,
            operator_seats=2,
            messaging_channels_enabled=True,
            audit_retention="90d",
            feature_flags=("market_update", "review_queue", "follow-up_ledger", "review_workflow", "collaborator_notes", "shared_follow_ups"),
        ),
        support_tier="standard",
        billing_state="non_authoritative_workspace_mode",
        price_label="PropertyQuarry plan is managed separately",
        billing_cadence="not_applicable",
        invoice_window_label="See PropertyQuarry billing",
        renewal_window_label="See PropertyQuarry billing",
        billing_portal_state="property_billing_authoritative",
        billing_portal_path="/app/billing",
        upgrade_target_mode="executive_ops",
        renewal_owner_role="office_admin",
        contract_note="Shared workspace mode; this object does not grant a paid PropertyQuarry plan.",
    ),
    "executive_ops": WorkspacePlan(
        plan_key="workspace_managed",
        display_name="Managed workspace",
        unit_of_sale="workspace",
        entitlements=PlanEntitlements(
            principal_seats=1,
            operator_seats=1000,
            messaging_channels_enabled=True,
            audit_retention="180d",
            feature_flags=(
                "market_update",
                "review_queue",
                "follow-up_ledger",
                "review_workflow",
                "collaborator_notes",
                "shared_follow_ups",
                "account_history",
            ),
        ),
        support_tier="priority",
        billing_state="non_authoritative_workspace_mode",
        price_label="PropertyQuarry plan is managed separately",
        billing_cadence="not_applicable",
        invoice_window_label="See PropertyQuarry billing",
        renewal_window_label="See PropertyQuarry billing",
        billing_portal_state="property_billing_authoritative",
        billing_portal_path="/app/billing",
        upgrade_target_mode="executive_ops",
        renewal_owner_role="account_lead",
        contract_note="Managed workspace mode; this object does not grant a paid PropertyQuarry plan.",
    ),
}


def workspace_plan_for_mode(workspace_mode: str) -> WorkspacePlan:
    normalized = str(workspace_mode or "").strip().lower() or "personal"
    if normalized == "shared":
        normalized = "team"
    return _PLANS.get(normalized, _PLANS["personal"])


def workspace_commercial_snapshot(
    plan: WorkspacePlan,
    *,
    seats_used: int,
    selected_channels: tuple[str, ...] = (),
) -> dict[str, dict[str, object]]:
    selected_messaging = sorted({value for value in selected_channels if value in {"telegram", "whatsapp"}})
    seat_limit = int(plan.entitlements.operator_seats or 0)
    seat_overage = max(int(seats_used or 0) - seat_limit, 0)
    warnings: list[str] = []
    blocked_actions: list[str] = []
    upgrade_target = workspace_plan_for_mode(plan.upgrade_target_mode or "team")
    invoice_status = "property_billing_authoritative"
    blocked_action_message = "No current commercial blocks."
    usage_pressure_state = "within_limit"
    if seat_overage:
        warnings.append("Active collaborators exceed included seats.")
        blocked_actions.append("operator_seat_overage")
        blocked_action_message = (
            f"Remove {seat_overage} collaborator seat"
            f"{'' if seat_overage == 1 else 's'} or change workspace mode before assigning more people."
        )
        usage_pressure_state = "seat_overage"
    elif selected_messaging and not plan.entitlements.messaging_channels_enabled:
        warnings.append("Messaging channels are selected but not included in this plan.")
        blocked_actions.append("messaging_setup")
        blocked_action_message = "Messaging is not available in this workspace mode."
        usage_pressure_state = "messaging_locked"
    seat_pressure_label = f"{int(seats_used or 0)} of {seat_limit} collaborator seats used"
    if seat_overage:
        seat_pressure_label = f"{int(seats_used or 0)} active collaborators across {seat_limit} included seats"
    upgrade_path_key = ""
    upgrade_path_label = ""
    if upgrade_target.plan_key != plan.plan_key:
        upgrade_path_key = upgrade_target.plan_key
        upgrade_path_label = upgrade_target.display_name
    return {
        "billing": {
            "authority": "property_billing_service",
            "billing_state": plan.billing_state,
            "support_tier": plan.support_tier,
            "renewal_owner_role": plan.renewal_owner_role,
            "contract_note": plan.contract_note,
            "price_label": plan.price_label,
            "billing_cadence": plan.billing_cadence,
            "invoice_status": invoice_status,
            "invoice_window_label": plan.invoice_window_label,
            "renewal_window_label": plan.renewal_window_label,
            "billing_portal_state": plan.billing_portal_state,
            "billing_portal_path": plan.billing_portal_path,
        },
        "commercial": {
            "selected_messaging_channels": selected_messaging,
            "messaging_scope_mismatch": bool(selected_messaging and not plan.entitlements.messaging_channels_enabled),
            "warnings": warnings,
            "blocked_actions": blocked_actions,
            "blocked_action_message": blocked_action_message,
            "seat_pressure_label": seat_pressure_label,
            "usage_pressure_state": usage_pressure_state,
            "upgrade_path_key": upgrade_path_key,
            "upgrade_path_label": upgrade_path_label,
        },
    }
