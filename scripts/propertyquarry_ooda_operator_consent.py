#!/usr/bin/env python3
"""Project and persist non-executing operator consent outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]

from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


STATE_SCHEMA = "propertyquarry.ooda_operator_consent_state.v1"
RECEIPT_SCHEMA = "propertyquarry.ooda_operator_consent_receipt.v1"
DEFAULT_STATE_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/operator-consent-state.json"
)
MAX_STATE_BYTES = 512 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_ID = re.compile(r"pqar_[0-9a-f]{24}\Z")
_DECISION_ID = re.compile(r"pqad_[0-9a-f]{24}\Z")
_PLAN_ID = re.compile(r"pqcp_[0-9a-f]{24}\Z")
_NEGATIVE_DECISIONS = {"reject", "defer"}
_APPROVED_NEXT_ACTION = (
    "manually apply only the exact one-line replacement, then regenerate all bound evidence; "
    "do not deploy or restart"
)
_CURRENT_EXPRESSION = "${EA_HOST_BIND:-127.0.0.1}:${EA_HOST_PORT:-8090}:8090"
_PROPOSED_EXPRESSION = "${EA_HOST_BIND:-127.0.0.1}:${EA_HOST_PORT:-8097}:8090"


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _rooted(path: Path) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else ROOT / candidate


def _parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _resolution_admissible(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    row = dict(value)
    digest = row.pop("resolution_digest", None)
    context_digest = str(value.get("presentation_context_digest") or "")
    return bool(
        set(value)
        == {
            "action_digest",
            "presentation_context_digest",
            "decision",
            "decision_id",
            "decision_sha256",
            "request_id",
            "request_sha256",
            "resolved_at",
            "source_cycle_receipt_sha256",
            "resolution_digest",
        }
        and _SHA256.fullmatch(str(value.get("action_digest") or ""))
        and _SHA256.fullmatch(context_digest)
        and value.get("decision") in _NEGATIVE_DECISIONS
        and _DECISION_ID.fullmatch(str(value.get("decision_id") or ""))
        and _SHA256.fullmatch(str(value.get("decision_sha256") or ""))
        and _REQUEST_ID.fullmatch(str(value.get("request_id") or ""))
        and _SHA256.fullmatch(str(value.get("request_sha256") or ""))
        and _parse_timestamp(value.get("resolved_at")) is not None
        and _SHA256.fullmatch(
            str(value.get("source_cycle_receipt_sha256") or "")
        )
        and _SHA256.fullmatch(str(digest or ""))
        and digest == _sha256(_canonical(row))
    )


def _load_state(path: Path) -> tuple[dict[str, Any], str, str]:
    target = _rooted(path)
    try:
        metadata = target.lstat()
    except OSError:
        return {}, "missing", ""
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        return {}, "invalid", ""
    try:
        payload, _raw, digest = load_strict_json_object_snapshot(
            target,
            field="operator_consent_state",
            maximum_bytes=MAX_STATE_BYTES,
        )
    except Exception:
        return {}, "invalid", ""
    resolutions = payload.get("active_resolutions")
    if not (
        set(payload)
        == {
            "schema",
            "updated_at",
            "source_cycle_receipt_sha256",
            "active_resolutions",
            "automatic_execution_allowed",
            "delivery_state_updated",
            "provider_quota_consumed",
            "protected_operation_executed",
        }
        and payload.get("schema") == STATE_SCHEMA
        and _parse_timestamp(payload.get("updated_at")) is not None
        and _SHA256.fullmatch(
            str(payload.get("source_cycle_receipt_sha256") or "")
        )
        and isinstance(resolutions, dict)
        and all(
            str(lane).strip() and _resolution_admissible(row)
            for lane, row in resolutions.items()
        )
        and payload.get("automatic_execution_allowed") is False
        and payload.get("delivery_state_updated") is False
        and payload.get("provider_quota_consumed") is False
        and payload.get("protected_operation_executed") is False
    ):
        return {}, "invalid", ""
    return payload, "ready", digest


def _contextual_action(
    action: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None,
    prior_resolution: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    projected = dict(action)
    context_digest = ""
    if context is not None:
        context_digest = operator_status._presentation_context_digest(context)
    elif prior_resolution is not None:
        prior_digest = str(
            prior_resolution.get("presentation_context_digest") or ""
        )
        if _SHA256.fullmatch(prior_digest):
            context_digest = prior_digest
    if context_digest:
        projected["presentation_context_digest"] = context_digest
    projected["presentation_digest"] = operator_status._presentation_digest(
        projected
    )
    return projected, context_digest


def _resolved_action_admissible(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    action = dict(value)
    context_digest = str(action.pop("presentation_context_digest", "") or "")
    presentation_digest = str(action.pop("presentation_digest", "") or "")
    lane = str(action.get("lane") or "")
    reason = str(action.get("reason") or "")
    return bool(
        set(action)
        == {
            "lane",
            "reason",
            "source_generated_at",
            "safe_next_action",
            "consent_required",
            "automatic_execution_allowed",
            "protected_operations",
            "provider_quota_consumption_allowed",
        }
        and lane == "gold_live_runtime"
        and reason in operator_status._GOLD_ACTIONS
        and action.get("safe_next_action")
        == operator_status._GOLD_ACTIONS[reason]
        and _parse_timestamp(action.get("source_generated_at")) is not None
        and action.get("consent_required") is True
        and action.get("automatic_execution_allowed") is False
        and action.get("protected_operations")
        == operator_status._PROTECTED_BY_LANE[lane]
        and action.get("provider_quota_consumption_allowed") is False
        and _SHA256.fullmatch(context_digest)
        and _SHA256.fullmatch(presentation_digest)
        and operator_status._presentation_digest(value) == presentation_digest
    )


def _expected_scope(context: Mapping[str, Any]) -> dict[str, Any]:
    scope = context.get("scope")
    if not (
        context.get("lane") == "gold_live_runtime"
        and isinstance(scope, Mapping)
    ):
        raise ValueError("operator_consent_context_not_admissible")
    expected = {
        "operation": "runtime_configuration_change",
        "change_id": "live_probe_origin",
        "current_value": str(scope.get("current_probe_origin") or ""),
        "proposed_value": str(scope.get("proposed_probe_origin") or ""),
        "public_host": str(scope.get("proposed_probe_host") or ""),
        "public_origin": str(scope.get("release_public_origin") or ""),
        "compose_project": str(scope.get("compose_project") or ""),
    }
    if not all(expected.values()):
        raise ValueError("operator_consent_context_not_admissible")
    return expected


def _verified_decision(
    value: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
) -> str:
    progress = value.get("progress")
    decision = str(value.get("decision") or "")
    exact_scope_authorized = decision == "approve_exact_scope"
    if not (
        value.get("status") == "verified"
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
        and progress.get("authorization_decision_recorded") is True
        and decision in {"approve_exact_scope", "reject", "defer"}
        and _DECISION_ID.fullmatch(str(value.get("decision_id") or ""))
        and _SHA256.fullmatch(str(value.get("decision_sha256") or ""))
        and _REQUEST_ID.fullmatch(str(value.get("request_id") or ""))
        and _SHA256.fullmatch(str(value.get("request_sha256") or ""))
        and _parse_timestamp(value.get("recorded_at")) is not None
        and _parse_timestamp(value.get("expires_at")) is not None
        and value.get("scope") == _expected_scope(context)
        and value.get("authorization_required") is True
        and value.get("authorization_recorded") is True
        and value.get("exact_scope_authorized") is exact_scope_authorized
        and value.get("automatic_execution_allowed") is False
        and value.get("execution_authorized") is False
        and value.get("execution_performed") is False
        and value.get("deployment_or_restart_authorized") is False
        and value.get("protected_operation_executed") is False
        and value.get("provider_quota_consumption_allowed") is False
        and value.get("delivery_authorized") is False
    ):
        raise ValueError("operator_consent_decision_not_admissible")
    return decision


def _negative_resolution(
    *,
    action: Mapping[str, Any],
    context: Mapping[str, Any],
    decision: Mapping[str, Any],
    source_cycle_receipt_sha256: str,
) -> dict[str, Any]:
    value = _verified_decision(decision, context=context)
    if value not in _NEGATIVE_DECISIONS:
        raise ValueError("operator_consent_negative_decision_required")
    row: dict[str, Any] = {
        "action_digest": str(action.get("presentation_digest") or ""),
        "presentation_context_digest": str(
            action.get("presentation_context_digest") or ""
        ),
        "decision": value,
        "decision_id": str(decision.get("decision_id") or ""),
        "decision_sha256": str(decision.get("decision_sha256") or ""),
        "request_id": str(decision.get("request_id") or ""),
        "request_sha256": str(decision.get("request_sha256") or ""),
        "resolved_at": str(decision.get("recorded_at") or ""),
        "source_cycle_receipt_sha256": source_cycle_receipt_sha256,
    }
    row["resolution_digest"] = _sha256(_canonical(row))
    if not _resolution_admissible(row):
        raise ValueError("operator_consent_resolution_not_admissible")
    return row


def _approved_action(
    *,
    action: Mapping[str, Any],
    context: Mapping[str, Any],
    decision: Mapping[str, Any],
    configuration: Mapping[str, Any],
    configuration_preview: Mapping[str, Any],
    manual_action_handoff: Mapping[str, Any],
) -> dict[str, Any]:
    if _verified_decision(decision, context=context) != "approve_exact_scope":
        raise ValueError("operator_consent_approval_not_admissible")
    change = configuration.get("change")
    preview = configuration_preview.get("preview")
    manual_target = manual_action_handoff.get("target")
    handoff_id = str(manual_action_handoff.get("handoff_id") or "")
    handoff_sha256 = str(manual_action_handoff.get("handoff_sha256") or "")
    expected_apply_command = (
        "python3 scripts/propertyquarry_ooda_configuration_manual_action.py "
        '--apply --operator-id "$PROPERTYQUARRY_OPERATOR_ID" '
        f"--handoff-id {handoff_id} --handoff-sha256 {handoff_sha256}"
    )
    if not (
        configuration.get("status") == "ready"
        and configuration.get("current_evidence_verified") is True
        and configuration.get("request_id") == decision.get("request_id")
        and configuration.get("request_sha256") == decision.get("request_sha256")
        and _PLAN_ID.fullmatch(str(configuration.get("plan_id") or ""))
        and _SHA256.fullmatch(str(configuration.get("plan_sha256") or ""))
        and configuration.get("plan_status") == "exact_scope_authorized"
        and configuration.get("authorization_decision") == "approve_exact_scope"
        and configuration.get("authorization_recorded") is True
        and configuration.get("exact_scope_authorized") is True
        and configuration.get("manual_apply_authorized") is True
        and configuration.get("automatic_apply_allowed") is False
        and configuration.get("apply_performed") is False
        and configuration.get("execution_authorized") is False
        and configuration.get("execution_performed") is False
        and configuration.get("deployment_or_restart_authorized") is False
        and configuration.get("protected_operation_executed") is False
        and configuration.get("provider_quota_consumption_allowed") is False
        and configuration.get("delivery_authorized") is False
        and configuration.get("next_action") == _APPROVED_NEXT_ACTION
        and isinstance(change, Mapping)
        and change.get("operation") == "replace_exact_text"
        and change.get("path") == "docker-compose.property.yml"
        and change.get("selector") == "services.propertyquarry-api.ports[0]"
        and change.get("current_expression") == _CURRENT_EXPRESSION
        and change.get("proposed_expression") == _PROPOSED_EXPRESSION
        and change.get("change_required") is True
        and change.get("replacement_count") == 1
        and change.get("rollback_expression") == _CURRENT_EXPRESSION
        and _SHA256.fullmatch(str(change.get("expected_before_sha256") or ""))
        and configuration_preview.get("status") == "ready"
        and configuration_preview.get("current_evidence_verified") is True
        and configuration_preview.get("preview_verified") is True
        and configuration_preview.get("request_id") == decision.get("request_id")
        and configuration_preview.get("request_sha256")
        == decision.get("request_sha256")
        and configuration_preview.get("decision_id")
        == decision.get("decision_id")
        and configuration_preview.get("decision_sha256")
        == decision.get("decision_sha256")
        and configuration_preview.get("plan_id") == configuration.get("plan_id")
        and configuration_preview.get("plan_sha256")
        == configuration.get("plan_sha256")
        and _PLAN_ID.fullmatch(str(configuration_preview.get("plan_id") or ""))
        and re.fullmatch(
            r"pqcv_[0-9a-f]{24}",
            str(configuration_preview.get("preview_id") or ""),
        )
        and _SHA256.fullmatch(
            str(configuration_preview.get("preview_sha256") or "")
        )
        and str(configuration_preview.get("preview_path") or "").endswith(
            f"/{configuration_preview.get('preview_id')}.json"
        )
        and configuration_preview.get("authorization_recorded") is True
        and configuration_preview.get("exact_scope_authorized") is True
        and configuration_preview.get("manual_apply_authorized") is True
        and configuration_preview.get("automatic_apply_allowed") is False
        and configuration_preview.get("source_edit_performed") is False
        and configuration_preview.get("execution_authorized") is False
        and configuration_preview.get("execution_performed") is False
        and configuration_preview.get("deployment_or_restart_authorized") is False
        and configuration_preview.get("protected_operation_executed") is False
        and configuration_preview.get("provider_quota_consumption_allowed") is False
        and configuration_preview.get("delivery_authorized") is False
        and isinstance(preview, Mapping)
        and preview.get("operation") == "replace_exact_text_preview"
        and preview.get("path") == change.get("path")
        and preview.get("selector") == change.get("selector")
        and preview.get("before_sha256") == change.get("expected_before_sha256")
        and _SHA256.fullmatch(str(preview.get("after_sha256") or ""))
        and preview.get("replacement_count") == 1
        and preview.get("current_expression") == _CURRENT_EXPRESSION
        and preview.get("proposed_expression") == _PROPOSED_EXPRESSION
        and _SHA256.fullmatch(
            str(preview.get("forward_unified_diff_sha256") or "")
        )
        and _SHA256.fullmatch(
            str(preview.get("rollback_unified_diff_sha256") or "")
        )
        and manual_action_handoff.get("status") == "ready"
        and manual_action_handoff.get("current_evidence_verified") is True
        and manual_action_handoff.get("handoff_verified") is True
        and re.fullmatch(r"pqmh_[0-9a-f]{24}", handoff_id)
        and _SHA256.fullmatch(handoff_sha256)
        and str(manual_action_handoff.get("handoff_path") or "").endswith(
            f"/{handoff_id}.json"
        )
        and manual_action_handoff.get("request_id") == decision.get("request_id")
        and manual_action_handoff.get("request_sha256")
        == decision.get("request_sha256")
        and manual_action_handoff.get("decision_id") == decision.get("decision_id")
        and manual_action_handoff.get("decision_sha256")
        == decision.get("decision_sha256")
        and manual_action_handoff.get("plan_id") == configuration.get("plan_id")
        and manual_action_handoff.get("plan_sha256")
        == configuration.get("plan_sha256")
        and manual_action_handoff.get("preview_id")
        == configuration_preview.get("preview_id")
        and manual_action_handoff.get("preview_sha256")
        == configuration_preview.get("preview_sha256")
        and manual_action_handoff.get("authorization_recorded") is True
        and manual_action_handoff.get("exact_scope_authorized") is True
        and manual_action_handoff.get("manual_apply_authorized") is True
        and manual_action_handoff.get("manual_rollback_authorized") is True
        and manual_action_handoff.get("manual_invocation_required") is True
        and manual_action_handoff.get("automatic_execution_allowed") is False
        and manual_action_handoff.get("source_edit_performed") is False
        and manual_action_handoff.get("deployment_or_restart_authorized") is False
        and manual_action_handoff.get("deployment_or_restart_performed") is False
        and manual_action_handoff.get("provider_quota_consumption_allowed") is False
        and manual_action_handoff.get("provider_quota_consumed") is False
        and manual_action_handoff.get("delivery_authorized") is False
        and manual_action_handoff.get("delivery_attempted") is False
        and manual_action_handoff.get("apply_command") == expected_apply_command
        and manual_action_handoff.get("rollback_command_source")
        == "use the exact rollback_command emitted by the verified apply receipt"
        and isinstance(manual_target, Mapping)
        and manual_target.get("path") == change.get("path")
        and manual_target.get("selector") == change.get("selector")
        and manual_target.get("expected_before_sha256")
        == preview.get("before_sha256")
        and manual_target.get("expected_after_sha256")
        == preview.get("after_sha256")
        and manual_target.get("current_expression") == _CURRENT_EXPRESSION
        and manual_target.get("proposed_expression") == _PROPOSED_EXPRESSION
        and manual_target.get("replacement_count") == 1
        and manual_target.get("forward_unified_diff_sha256")
        == preview.get("forward_unified_diff_sha256")
        and manual_target.get("rollback_unified_diff_sha256")
        == preview.get("rollback_unified_diff_sha256")
    ):
        raise ValueError("operator_consent_configuration_not_admissible")
    projected = {
        "lane": str(action.get("lane") or ""),
        "reason": "exact_scope_configuration_change_authorized",
        "source_generated_at": str(action.get("source_generated_at") or ""),
        "safe_next_action": _APPROVED_NEXT_ACTION,
        "consent_required": False,
        "authorization_recorded": True,
        "exact_scope_authorized": True,
        "manual_apply_authorized": True,
        "configuration_plan_id": str(configuration.get("plan_id") or ""),
        "configuration_plan_sha256": str(
            configuration.get("plan_sha256") or ""
        ),
        "configuration_preview_id": str(
            configuration_preview.get("preview_id") or ""
        ),
        "configuration_preview_sha256": str(
            configuration_preview.get("preview_sha256") or ""
        ),
        "configuration_preview_path": str(
            configuration_preview.get("preview_path") or ""
        ),
        "expected_after_sha256": str(preview.get("after_sha256") or ""),
        "rollback_unified_diff_sha256": str(
            preview.get("rollback_unified_diff_sha256") or ""
        ),
        "manual_action_handoff_id": handoff_id,
        "manual_action_handoff_sha256": handoff_sha256,
        "manual_action_handoff_path": str(
            manual_action_handoff.get("handoff_path") or ""
        ),
        "manual_apply_command": expected_apply_command,
        "manual_rollback_source": str(
            manual_action_handoff.get("rollback_command_source") or ""
        ),
        "change": dict(change),
        "automatic_execution_allowed": False,
        "protected_operations": ["runtime_configuration_change"],
        "deployment_or_restart_authorized": False,
        "provider_quota_consumption_allowed": False,
    }
    return projected


def project_operator_consent(
    status: dict[str, Any],
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    presentation_context_by_lane: Mapping[str, Mapping[str, Any]] | None = None,
    consent_outcome_by_lane: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply exact consent outcomes without granting automatic authority."""

    if status.get("status") == "blocked" or status.get("action_required") is not True:
        return status
    source_digest = str(status.get("source_cycle_receipt_sha256") or "")
    if not (
        _SHA256.fullmatch(source_digest)
        and status.get("automatic_execution_allowed") is False
        and status.get("provider_quota_consumption_allowed") is False
        and status.get("protected_operation_executed") is False
    ):
        raise ValueError("operator_consent_projection_not_admissible")
    contexts = dict(presentation_context_by_lane or {})
    outcomes = dict(consent_outcome_by_lane or {})
    prior_state, prior_status, prior_sha256 = _load_state(Path(state_path))
    if prior_status == "invalid":
        raise ValueError("operator_consent_state_not_admissible")
    prior_resolutions = dict(prior_state.get("active_resolutions") or {})
    projected_resolutions: dict[str, dict[str, Any]] = {}
    new_resolution_lanes: list[str] = []
    retained_resolution_lanes: list[str] = []
    resolved_actions: list[dict[str, Any]] = []
    projected_actions: list[dict[str, Any]] = []
    approved_lanes: list[str] = []

    for value in list(status.get("actions") or []):
        if not isinstance(value, Mapping):
            raise ValueError("operator_consent_action_not_admissible")
        lane = str(value.get("lane") or "").strip()
        if not lane:
            raise ValueError("operator_consent_action_not_admissible")
        prior_resolution = prior_resolutions.get(lane)
        context = contexts.get(lane)
        contextual_action, context_digest = _contextual_action(
            value,
            context=context,
            prior_resolution=(
                prior_resolution if isinstance(prior_resolution, Mapping) else None
            ),
        )
        outcome = outcomes.get(lane)
        decision_row = (
            outcome.get("decision") if isinstance(outcome, Mapping) else None
        )
        configuration = (
            outcome.get("configuration")
            if isinstance(outcome, Mapping)
            else None
        )
        configuration_preview = (
            outcome.get("configuration_preview")
            if isinstance(outcome, Mapping)
            else None
        )
        manual_action_handoff = (
            outcome.get("manual_action_handoff")
            if isinstance(outcome, Mapping)
            else None
        )
        decision_status = (
            str(decision_row.get("status") or "")
            if isinstance(decision_row, Mapping)
            else ""
        )
        if decision_status == "verified":
            if context is None:
                raise ValueError("operator_consent_context_required")
            decision_value = _verified_decision(decision_row, context=context)
            if decision_value in _NEGATIVE_DECISIONS:
                resolution = _negative_resolution(
                    action=contextual_action,
                    context=context,
                    decision=decision_row,
                    source_cycle_receipt_sha256=source_digest,
                )
                if (
                    prior_status == "ready"
                    and isinstance(prior_resolution, Mapping)
                    and _resolution_admissible(prior_resolution)
                    and prior_resolution.get("action_digest")
                    == resolution.get("action_digest")
                    and prior_resolution.get("presentation_context_digest")
                    == resolution.get("presentation_context_digest")
                    and prior_resolution.get("decision_id")
                    == resolution.get("decision_id")
                    and prior_resolution.get("decision_sha256")
                    == resolution.get("decision_sha256")
                ):
                    projected_resolutions[lane] = dict(prior_resolution)
                    retained_resolution_lanes.append(lane)
                else:
                    projected_resolutions[lane] = resolution
                    new_resolution_lanes.append(lane)
                resolved_actions.append(contextual_action)
                continue
            if not (
                isinstance(configuration, Mapping)
                and isinstance(configuration_preview, Mapping)
                and isinstance(manual_action_handoff, Mapping)
            ):
                raise ValueError("operator_consent_configuration_required")
            projected_actions.append(
                _approved_action(
                    action=contextual_action,
                    context=context,
                    decision=decision_row,
                    configuration=configuration,
                    configuration_preview=configuration_preview,
                    manual_action_handoff=manual_action_handoff,
                )
            )
            approved_lanes.append(lane)
            continue
        if decision_status not in {"", "pending", "blocked"}:
            raise ValueError("operator_consent_decision_not_admissible")
        retain_prior = decision_status in {"", "pending"}
        if (
            retain_prior
            and prior_status == "ready"
            and isinstance(prior_resolution, Mapping)
            and _resolution_admissible(prior_resolution)
            and prior_resolution.get("action_digest")
            == contextual_action.get("presentation_digest")
            and prior_resolution.get("presentation_context_digest")
            == context_digest
        ):
            projected_resolutions[lane] = dict(prior_resolution)
            retained_resolution_lanes.append(lane)
            resolved_actions.append(contextual_action)
            continue
        projected_actions.append(dict(value))

    status["consent"] = {
        "state_path": str(state_path),
        "state_status": prior_status,
        "state_sha256": prior_sha256,
        "projected_resolutions": projected_resolutions,
        "new_resolution_lanes": sorted(new_resolution_lanes),
        "retained_resolution_lanes": sorted(retained_resolution_lanes),
        "approved_lanes": sorted(approved_lanes),
        "resolved_outcomes": [
            {
                "lane": lane,
                "decision": row["decision"],
                "request_id": row["request_id"],
                "resolved_at": row["resolved_at"],
            }
            for lane, row in sorted(projected_resolutions.items())
        ],
        "automatic_execution_allowed": False,
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "protected_operation_executed": False,
    }
    status["resolved_actions"] = resolved_actions
    status["actions"] = projected_actions
    status["progress"]["consent_resolved_action_count"] = len(resolved_actions)
    status["progress"]["consent_approved_action_count"] = len(approved_lanes)
    status["action_required"] = bool(projected_actions)
    status["interrupt_operator"] = bool(projected_actions)
    if not projected_actions:
        decisions = sorted(
            {row["decision"] for row in projected_resolutions.values()}
        )
        status["status"] = "ready"
        status["blocking_reason"] = ""
        status["next_action"] = (
            "await a verified semantic action change; the current exact action was "
            + (decisions[0] if len(decisions) == 1 else "resolved")
        )
    return status


def _receipt_blocked(reason: str, *, now: datetime) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "blocked",
        "recorded_at": now.isoformat(),
        "blocking_reason": reason,
        "state_updated": False,
        "automatic_execution_allowed": False,
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "protected_operation_executed": False,
    }


def record_operator_consent(
    status: Mapping[str, Any],
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    expected_source_cycle_receipt_sha256: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a rendered negative resolution; never persist approval authority."""

    recorded_at = _now(now)
    source_digest = str(status.get("source_cycle_receipt_sha256") or "")
    expected_digest = str(expected_source_cycle_receipt_sha256 or "").strip()
    consent = status.get("consent")
    if not isinstance(consent, Mapping):
        return {
            "schema": RECEIPT_SCHEMA,
            "status": "unchanged",
            "recorded_at": recorded_at.isoformat(),
            "blocking_reason": "",
            "state_updated": False,
            "automatic_execution_allowed": False,
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    resolutions = consent.get("projected_resolutions")
    resolved_actions = status.get("resolved_actions")
    resolved_identities: dict[str, dict[str, str]] = {}
    if isinstance(resolved_actions, list):
        for action in resolved_actions:
            if not _resolved_action_admissible(action):
                break
            lane = str(action.get("lane") or "")
            if lane in resolved_identities:
                break
            resolved_identities[lane] = {
                "action_digest": str(action.get("presentation_digest") or ""),
                "presentation_context_digest": str(
                    action.get("presentation_context_digest") or ""
                ),
            }
    if not (
        _SHA256.fullmatch(source_digest)
        and (not expected_digest or expected_digest == source_digest)
        and consent.get("state_path") == str(state_path)
        and status.get("automatic_execution_allowed") is False
        and status.get("provider_quota_consumption_allowed") is False
        and status.get("protected_operation_executed") is False
        and isinstance(resolutions, Mapping)
        and all(
            lane == "gold_live_runtime" and _resolution_admissible(row)
            for lane, row in resolutions.items()
        )
        and isinstance(resolved_actions, list)
        and len(resolved_identities) == len(resolved_actions)
        and set(resolved_identities) == set(resolutions)
        and all(
            resolved_identities[str(lane)]
            == {
                "action_digest": str(row.get("action_digest") or ""),
                "presentation_context_digest": str(
                    row.get("presentation_context_digest") or ""
                ),
            }
            for lane, row in resolutions.items()
        )
        and consent.get("automatic_execution_allowed") is False
        and consent.get("delivery_state_updated") is False
        and consent.get("provider_quota_consumed") is False
        and consent.get("protected_operation_executed") is False
    ):
        return _receipt_blocked(
            "operator_consent_projection_not_admissible",
            now=recorded_at,
        )
    current_state, current_status, current_sha256 = _load_state(Path(state_path))
    if (
        current_status != consent.get("state_status")
        or current_sha256 != consent.get("state_sha256")
        or current_status == "invalid"
    ):
        return _receipt_blocked(
            "operator_consent_state_binding_mismatch",
            now=recorded_at,
        )
    current_resolutions = dict(current_state.get("active_resolutions") or {})
    expected_resolutions = {
        str(lane): dict(row) for lane, row in resolutions.items()
    }
    new_resolution_lanes = {
        lane
        for lane, row in expected_resolutions.items()
        if current_resolutions.get(lane) != row
    }
    retained_resolution_lanes = {
        lane
        for lane, row in expected_resolutions.items()
        if current_resolutions.get(lane) == row
    }
    if not (
        sorted(new_resolution_lanes)
        == list(consent.get("new_resolution_lanes") or [])
        and sorted(retained_resolution_lanes)
        == list(consent.get("retained_resolution_lanes") or [])
    ):
        return _receipt_blocked(
            "operator_consent_resolution_binding_mismatch",
            now=recorded_at,
        )
    if current_status == "ready" and current_resolutions == expected_resolutions:
        return {
            "schema": RECEIPT_SCHEMA,
            "status": "unchanged",
            "recorded_at": recorded_at.isoformat(),
            "blocking_reason": "",
            "state_path": str(state_path),
            "state_sha256": current_sha256,
            "active_resolution_count": len(current_resolutions),
            "state_updated": False,
            "automatic_execution_allowed": False,
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    if current_status == "missing" and not expected_resolutions:
        return {
            "schema": RECEIPT_SCHEMA,
            "status": "unchanged",
            "recorded_at": recorded_at.isoformat(),
            "blocking_reason": "",
            "state_path": str(state_path),
            "state_sha256": "",
            "active_resolution_count": 0,
            "state_updated": False,
            "automatic_execution_allowed": False,
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    payload = {
        "schema": STATE_SCHEMA,
        "updated_at": recorded_at.isoformat(),
        "source_cycle_receipt_sha256": source_digest,
        "active_resolutions": expected_resolutions,
        "automatic_execution_allowed": False,
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "protected_operation_executed": False,
    }
    try:
        atomic_write_bytes(
            _rooted(Path(state_path)),
            _canonical(payload),
            overwrite=True,
        )
        persisted, persisted_status, persisted_sha256 = _load_state(
            Path(state_path)
        )
    except Exception:
        return _receipt_blocked(
            "operator_consent_state_write_failed",
            now=recorded_at,
        )
    if persisted_status != "ready" or persisted != payload:
        return _receipt_blocked(
            "operator_consent_state_write_not_verified",
            now=recorded_at,
        )
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "recorded",
        "recorded_at": recorded_at.isoformat(),
        "blocking_reason": "",
        "state_path": str(state_path),
        "state_sha256": persisted_sha256,
        "active_resolution_count": len(expected_resolutions),
        "resolution_recorded_count": len(
            set(expected_resolutions) - set(current_resolutions)
        ),
        "resolution_cleared_count": len(
            set(current_resolutions) - set(expected_resolutions)
        ),
        "state_updated": True,
        "automatic_execution_allowed": False,
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "protected_operation_executed": False,
    }
