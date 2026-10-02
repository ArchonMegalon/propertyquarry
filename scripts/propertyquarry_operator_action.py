from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


GOLD_STATUS_SCHEMA = "propertyquarry.gold_status.v1"
DEFAULT_MAX_SOURCE_AGE_SECONDS = 24.0 * 3600.0
GOVERNED_NOTIFICATION_MAX_SOURCE_AGE_SECONDS = 30.0 * 60.0
PROPERTYQUARRY_OPERATOR_NEXT_ACTIONS = {
    "live_runtime_tunnel_unavailable": (
        "inspect and stage the PropertyQuarry Cloudflare tunnel connector recovery for review; "
        "start or restart the connector only after explicit deployment/restart authorization, then "
        "rerun propertyquarry_live_mobile_surface_smoke.py"
    ),
    "live_runtime_host_admission_rejected": (
        "inspect and stage the PropertyQuarry host-admission, public-origin, and dedicated-runtime "
        "configuration for review; after an explicitly authorized deployment or restart, rerun "
        "propertyquarry_live_mobile_surface_smoke.py"
    ),
    "live_runtime_prerequisite_blocked": (
        "inspect and stage the reported live runtime or research-fixture prerequisite for review; "
        "after any required external change is explicitly authorized, rerun "
        "propertyquarry_live_mobile_surface_smoke.py"
    ),
}


def _parse_receipt_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def propertyquarry_operator_action_summary(
    receipt: dict[str, Any],
    *,
    now: datetime | None = None,
    max_source_age_seconds: float = DEFAULT_MAX_SOURCE_AGE_SECONDS,
) -> dict[str, Any]:
    """Project one tightly allowlisted operator action from a Gold receipt."""

    suppressed = {
        "status": "none",
        "action_required": False,
        "interrupt_operator": False,
    }
    if not isinstance(receipt, dict) or str(receipt.get("status") or "").strip().lower() != "blocked":
        return {**suppressed, "reason": "gold_not_blocked"}
    surface = receipt.get("live_mobile_surfaces")
    if not isinstance(surface, dict) or surface.get("route_probe_blocked") is not True:
        return {**suppressed, "reason": "no_pre_route_runtime_blocker"}
    action = surface.get("operator_action")
    if not isinstance(action, dict):
        return {**suppressed, "reason": "operator_action_missing"}

    source_generated_at = str(action.get("source_generated_at") or "").strip()
    source_time = _parse_receipt_datetime(source_generated_at)
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        maximum_age = float(max_source_age_seconds)
    except (TypeError, ValueError, OverflowError):
        return {**suppressed, "reason": "source_freshness_policy_not_admissible"}
    if not (
        math.isfinite(maximum_age)
        and 60.0 <= maximum_age <= DEFAULT_MAX_SOURCE_AGE_SECONDS
    ):
        return {**suppressed, "reason": "source_freshness_policy_not_admissible"}
    age_seconds = (
        (observed_now - source_time).total_seconds()
        if source_time is not None
        else float("nan")
    )

    stale_suppression = (
        action.get("required") is False
        and action.get("interrupt_operator") is False
        and action.get("source_fresh") is False
        and action.get("reason") == "source_receipt_not_fresh"
        and action.get("notification_policy") == "suppress_stale_signal"
        and action.get("provider_quota_consumption_allowed") is False
    )
    if stale_suppression:
        if not (
            source_time is not None
            and math.isfinite(age_seconds)
            and age_seconds > maximum_age
        ):
            return {**suppressed, "reason": "stale_source_claim_not_admissible"}
        return {
            **suppressed,
            "reason": "source_receipt_not_fresh",
            "source_generated_at": source_time.isoformat(),
        }

    if not (
        action.get("required") is True
        and action.get("interrupt_operator") is True
        and action.get("source_fresh") is True
        and action.get("notification_policy") == "action_required_only"
        and action.get("provider_quota_consumption_allowed") is False
    ):
        return {**suppressed, "reason": "operator_action_not_admissible"}

    consent_gate = action.get("consent_gate")
    expected_operations = [
        "runtime_configuration_change",
        "deployment_or_restart",
    ]
    if not (
        isinstance(consent_gate, dict)
        and consent_gate.get("required") is True
        and consent_gate.get("automatic_execution_allowed") is False
        and consent_gate.get("protected_operations") == expected_operations
    ):
        return {**suppressed, "reason": "consent_gate_not_admissible"}

    if source_time is None:
        return {**suppressed, "reason": "source_receipt_not_fresh"}
    if (
        not math.isfinite(age_seconds)
        or age_seconds < -30.0
        or age_seconds > maximum_age
    ):
        return {
            **suppressed,
            "reason": "source_receipt_not_fresh",
            "source_generated_at": source_time.isoformat(),
        }

    reason = str(action.get("reason") or "").strip()
    if reason not in PROPERTYQUARRY_OPERATOR_NEXT_ACTIONS:
        return {**suppressed, "reason": "operator_action_reason_not_admissible"}
    return {
        "status": "action_required",
        "action_required": True,
        "interrupt_operator": True,
        "reason": reason,
        "source_generated_at": source_generated_at,
        "reversible_next_action": PROPERTYQUARRY_OPERATOR_NEXT_ACTIONS[reason],
        "notification_policy": "action_required_only",
        "consent_gate": {
            "required": True,
            "automatic_execution_allowed": False,
            "protected_operations": expected_operations,
        },
        "provider_quota_consumption_allowed": False,
    }
