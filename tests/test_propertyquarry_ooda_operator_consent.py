from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_operator_consent as consent
from scripts import propertyquarry_ooda_operator_status as operator_status


NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)


def _status() -> dict[str, object]:
    return {
        "schema": operator_status.SCHEMA,
        "status": "action_required",
        "action_required": True,
        "interrupt_operator": True,
        "updated_at": NOW.isoformat(),
        "blocking_reason": "live_runtime_host_admission_rejected",
        "next_action": operator_status._GOLD_ACTIONS[
            "live_runtime_host_admission_rejected"
        ],
        "progress": {
            "approved_snapshot_verified": True,
            "action_required_count": 1,
            "novel_action_count": 1,
            "current_snapshot_hashes_verified": True,
        },
        "source_cycle_receipt_sha256": "a" * 64,
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
        "actions": [
            {
                "lane": "gold_live_runtime",
                "reason": "live_runtime_host_admission_rejected",
                "source_generated_at": (NOW - timedelta(minutes=10)).isoformat(),
                "safe_next_action": operator_status._GOLD_ACTIONS[
                    "live_runtime_host_admission_rejected"
                ],
                "consent_required": True,
                "automatic_execution_allowed": False,
                "protected_operations": [
                    "runtime_configuration_change",
                    "deployment_or_restart",
                ],
                "provider_quota_consumption_allowed": False,
            }
        ],
    }


def _context(*, proposed_origin: str = "http://127.0.0.1:8097") -> dict[str, object]:
    return {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {
            "operation": "runtime_configuration_change",
            "change_id": "propertyquarry_local_probe_origin",
            "current_probe_origin": "http://127.0.0.1:8090",
            "proposed_probe_origin": proposed_origin,
            "current_probe_host": "propertyquarry.com",
            "proposed_probe_host": "propertyquarry.com",
            "release_public_origin": "https://propertyquarry.com",
            "compose_project": "property",
            "protected_operations": [
                "runtime_configuration_change",
                "deployment_or_restart",
            ],
        },
    }


def _decision(value: str) -> dict[str, object]:
    context = _context()
    scope = context["scope"]
    assert isinstance(scope, dict)
    return {
        "status": "verified",
        "blocking_reason": "",
        "progress": {
            "current_evidence_verified": True,
            "authorization_request_verified": True,
            "authorization_decision_recorded": True,
        },
        "request_id": "pqar_0123456789abcdef01234567",
        "request_sha256": "b" * 64,
        "decision_id": "pqad_0123456789abcdef01234567",
        "decision_sha256": "c" * 64,
        "decision": value,
        "recorded_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        "scope": {
            "operation": "runtime_configuration_change",
            "change_id": "live_probe_origin",
            "current_value": scope["current_probe_origin"],
            "proposed_value": scope["proposed_probe_origin"],
            "public_host": scope["proposed_probe_host"],
            "public_origin": scope["release_public_origin"],
            "compose_project": scope["compose_project"],
        },
        "authorization_required": True,
        "authorization_recorded": True,
        "exact_scope_authorized": value == "approve_exact_scope",
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _configuration() -> dict[str, object]:
    return {
        "status": "ready",
        "current_evidence_verified": True,
        "request_id": "pqar_0123456789abcdef01234567",
        "request_sha256": "b" * 64,
        "plan_id": "pqcp_0123456789abcdef01234567",
        "plan_sha256": "d" * 64,
        "plan_status": "exact_scope_authorized",
        "authorization_decision": "approve_exact_scope",
        "authorization_recorded": True,
        "exact_scope_authorized": True,
        "manual_apply_authorized": True,
        "automatic_apply_allowed": False,
        "apply_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "next_action": consent._APPROVED_NEXT_ACTION,
        "change": {
            "operation": "replace_exact_text",
            "path": "docker-compose.property.yml",
            "selector": "services.propertyquarry-api.ports[0]",
            "expected_before_sha256": "e" * 64,
            "current_expression": consent._CURRENT_EXPRESSION,
            "proposed_expression": consent._PROPOSED_EXPRESSION,
            "change_required": True,
            "replacement_count": 1,
            "rollback_expression": consent._CURRENT_EXPRESSION,
        },
    }


def _configuration_preview() -> dict[str, object]:
    return {
        "status": "ready",
        "current_evidence_verified": True,
        "preview_verified": True,
        "preview_id": "pqcv_0123456789abcdef01234567",
        "preview_sha256": "f" * 64,
        "preview_path": (
            "/private/previews/pqcv_0123456789abcdef01234567.json"
        ),
        "request_id": "pqar_0123456789abcdef01234567",
        "request_sha256": "b" * 64,
        "decision_id": "pqad_0123456789abcdef01234567",
        "decision_sha256": "c" * 64,
        "plan_id": "pqcp_0123456789abcdef01234567",
        "plan_sha256": "d" * 64,
        "authorization_recorded": True,
        "exact_scope_authorized": True,
        "manual_apply_authorized": True,
        "automatic_apply_allowed": False,
        "source_edit_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "preview": {
            "operation": "replace_exact_text_preview",
            "path": "docker-compose.property.yml",
            "selector": "services.propertyquarry-api.ports[0]",
            "before_sha256": "e" * 64,
            "after_sha256": "1" * 64,
            "replacement_count": 1,
            "current_expression": consent._CURRENT_EXPRESSION,
            "proposed_expression": consent._PROPOSED_EXPRESSION,
            "forward_unified_diff_sha256": "2" * 64,
            "rollback_unified_diff_sha256": "3" * 64,
        },
    }


def _manual_action_handoff() -> dict[str, object]:
    handoff_id = "pqmh_0123456789abcdef01234567"
    handoff_sha256 = "4" * 64
    return {
        "status": "ready",
        "current_evidence_verified": True,
        "handoff_verified": True,
        "handoff_id": handoff_id,
        "handoff_sha256": handoff_sha256,
        "handoff_path": f"/private/manual-actions/{handoff_id}.json",
        "request_id": "pqar_0123456789abcdef01234567",
        "request_sha256": "b" * 64,
        "decision_id": "pqad_0123456789abcdef01234567",
        "decision_sha256": "c" * 64,
        "plan_id": "pqcp_0123456789abcdef01234567",
        "plan_sha256": "d" * 64,
        "preview_id": "pqcv_0123456789abcdef01234567",
        "preview_sha256": "f" * 64,
        "authorization_recorded": True,
        "exact_scope_authorized": True,
        "manual_apply_authorized": True,
        "manual_rollback_authorized": True,
        "manual_invocation_required": True,
        "automatic_execution_allowed": False,
        "source_edit_performed": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "apply_command": (
            "python3 scripts/propertyquarry_ooda_configuration_manual_action.py "
            '--apply --operator-id "$PROPERTYQUARRY_OPERATOR_ID" '
            f"--handoff-id {handoff_id} --handoff-sha256 {handoff_sha256}"
        ),
        "rollback_command_source": (
            "use the exact rollback_command emitted by the verified apply receipt"
        ),
        "target": {
            "path": "docker-compose.property.yml",
            "selector": "services.propertyquarry-api.ports[0]",
            "expected_before_sha256": "e" * 64,
            "expected_after_sha256": "1" * 64,
            "current_expression": consent._CURRENT_EXPRESSION,
            "proposed_expression": consent._PROPOSED_EXPRESSION,
            "replacement_count": 1,
            "forward_unified_diff_sha256": "2" * 64,
            "rollback_unified_diff_sha256": "3" * 64,
        },
    }


@pytest.mark.parametrize("value", ["reject", "defer"])
def test_negative_decision_closes_and_durably_suppresses_same_semantic_action(
    value: str,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "consent-state.json"
    context = _context()
    first = consent.project_operator_consent(
        _status(),
        state_path=state_path,
        presentation_context_by_lane={"gold_live_runtime": context},
        consent_outcome_by_lane={
            "gold_live_runtime": {"decision": _decision(value)}
        },
    )

    assert first["status"] == "ready"
    assert first["action_required"] is False
    assert first["interrupt_operator"] is False
    assert first["actions"] == []
    assert first["consent"]["new_resolution_lanes"] == ["gold_live_runtime"]
    assert first["consent"]["resolved_outcomes"][0]["decision"] == value
    assert first["automatic_execution_allowed"] is False
    assert first["provider_quota_consumption_allowed"] is False
    assert first["protected_operation_executed"] is False

    recorded = consent.record_operator_consent(
        first,
        state_path=state_path,
        expected_source_cycle_receipt_sha256="a" * 64,
        now=NOW,
    )
    original = state_path.read_bytes()
    assert recorded["status"] == "recorded"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert recorded["active_resolution_count"] == 1

    repeated = consent.project_operator_consent(
        _status(),
        state_path=state_path,
        presentation_context_by_lane={},
        consent_outcome_by_lane={
            "gold_live_runtime": {"decision": {"status": "pending"}}
        },
    )
    repeated_record = consent.record_operator_consent(
        repeated,
        state_path=state_path,
        expected_source_cycle_receipt_sha256="a" * 64,
        now=NOW + timedelta(minutes=20),
    )

    assert repeated["status"] == "ready"
    assert repeated["interrupt_operator"] is False
    assert repeated["consent"]["retained_resolution_lanes"] == [
        "gold_live_runtime"
    ]
    assert repeated_record["status"] == "unchanged"
    assert state_path.read_bytes() == original

    refreshed_status = _status()
    refreshed_status["source_cycle_receipt_sha256"] = "f" * 64
    refreshed = consent.project_operator_consent(
        refreshed_status,
        state_path=state_path,
        presentation_context_by_lane={"gold_live_runtime": context},
        consent_outcome_by_lane={
            "gold_live_runtime": {"decision": _decision(value)}
        },
    )
    refreshed_record = consent.record_operator_consent(
        refreshed,
        state_path=state_path,
        expected_source_cycle_receipt_sha256="f" * 64,
        now=NOW + timedelta(minutes=1),
    )
    assert refreshed["consent"]["retained_resolution_lanes"] == [
        "gold_live_runtime"
    ]
    assert refreshed_record["status"] == "unchanged"
    assert state_path.read_bytes() == original


def test_semantic_scope_change_rearms_and_clears_negative_resolution(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "consent-state.json"
    first = consent.project_operator_consent(
        _status(),
        state_path=state_path,
        presentation_context_by_lane={"gold_live_runtime": _context()},
        consent_outcome_by_lane={
            "gold_live_runtime": {"decision": _decision("reject")}
        },
    )
    assert consent.record_operator_consent(
        first,
        state_path=state_path,
        now=NOW,
    )["status"] == "recorded"

    changed = consent.project_operator_consent(
        _status(),
        state_path=state_path,
        presentation_context_by_lane={
            "gold_live_runtime": _context(
                proposed_origin="http://127.0.0.1:8098"
            )
        },
        consent_outcome_by_lane={
            "gold_live_runtime": {"decision": {"status": "pending"}}
        },
    )

    assert changed["status"] == "action_required"
    assert changed["action_required"] is True
    assert changed["interrupt_operator"] is True
    assert changed["consent"]["projected_resolutions"] == {}
    cleared = consent.record_operator_consent(
        changed,
        state_path=state_path,
        now=NOW + timedelta(minutes=1),
    )
    assert cleared["status"] == "recorded"
    assert cleared["resolution_cleared_count"] == 1
    assert json.loads(state_path.read_text())["active_resolutions"] == {}


def test_approval_projects_only_fresh_exact_manual_step_and_does_not_persist_authority(
    tmp_path: Path,
) -> None:
    consent_state = tmp_path / "consent-state.json"
    presentation_state = tmp_path / "presentation-state.json"
    context = _context()
    outcome = {
        "gold_live_runtime": {
            "decision": _decision("approve_exact_scope"),
            "configuration": _configuration(),
            "configuration_preview": _configuration_preview(),
            "manual_action_handoff": _manual_action_handoff(),
        }
    }
    first = consent.project_operator_consent(
        _status(),
        state_path=consent_state,
        presentation_context_by_lane={"gold_live_runtime": context},
        consent_outcome_by_lane=outcome,
    )

    action = first["actions"][0]
    assert first["action_required"] is True
    assert action["reason"] == "exact_scope_configuration_change_authorized"
    assert action["consent_required"] is False
    assert action["manual_apply_authorized"] is True
    assert action["protected_operations"] == ["runtime_configuration_change"]
    assert action["deployment_or_restart_authorized"] is False
    assert action["automatic_execution_allowed"] is False
    assert action["configuration_preview_id"] == (
        "pqcv_0123456789abcdef01234567"
    )
    assert action["expected_after_sha256"] == "1" * 64
    assert action["rollback_unified_diff_sha256"] == "3" * 64
    assert action["manual_action_handoff_id"] == (
        "pqmh_0123456789abcdef01234567"
    )
    assert "--apply" in action["manual_apply_command"]
    assert first["consent"]["projected_resolutions"] == {}
    assert consent.record_operator_consent(
        first,
        state_path=consent_state,
        now=NOW,
    )["status"] == "unchanged"
    assert not consent_state.exists()

    presented = operator_status.apply_operator_presentation_state(
        first,
        state_path=presentation_state,
        presentation_context_by_lane={"gold_live_runtime": context},
    )
    assert presented["interrupt_operator"] is True
    assert operator_status.record_operator_presentation(
        presented,
        state_path=presentation_state,
        now=NOW,
    )["status"] == "recorded"

    repeated = operator_status.apply_operator_presentation_state(
        consent.project_operator_consent(
            _status(),
            state_path=consent_state,
            presentation_context_by_lane={"gold_live_runtime": context},
            consent_outcome_by_lane=outcome,
        ),
        state_path=presentation_state,
        presentation_context_by_lane={"gold_live_runtime": context},
    )
    assert repeated["status"] == "pending_action"
    assert repeated["interrupt_operator"] is False
    assert repeated["next_action"] == consent._APPROVED_NEXT_ACTION

    expired = consent.project_operator_consent(
        _status(),
        state_path=consent_state,
        presentation_context_by_lane={"gold_live_runtime": context},
        consent_outcome_by_lane={
            "gold_live_runtime": {
                "decision": {
                    "status": "blocked",
                    "blocking_reason": "authorization_decision_not_fresh",
                }
            }
        },
    )
    assert expired["action_required"] is True
    assert expired["actions"][0]["reason"] == "live_runtime_host_admission_rejected"
    assert expired["actions"][0].get("manual_apply_authorized") is None


def test_approval_without_verified_change_preview_fails_closed(
    tmp_path: Path,
) -> None:
    outcome = {
        "gold_live_runtime": {
            "decision": _decision("approve_exact_scope"),
            "configuration": _configuration(),
            "configuration_preview": {
                **_configuration_preview(),
                "source_edit_performed": True,
            },
            "manual_action_handoff": _manual_action_handoff(),
        }
    }

    with pytest.raises(ValueError, match="operator_consent_configuration_not_admissible"):
        consent.project_operator_consent(
            _status(),
            state_path=tmp_path / "consent-state.json",
            presentation_context_by_lane={"gold_live_runtime": _context()},
            consent_outcome_by_lane=outcome,
        )


def test_approval_without_verified_manual_action_handoff_fails_closed(
    tmp_path: Path,
) -> None:
    outcome = {
        "gold_live_runtime": {
            "decision": _decision("approve_exact_scope"),
            "configuration": _configuration(),
            "configuration_preview": _configuration_preview(),
            "manual_action_handoff": {
                **_manual_action_handoff(),
                "source_edit_performed": True,
            },
        }
    }

    with pytest.raises(ValueError, match="operator_consent_configuration_not_admissible"):
        consent.project_operator_consent(
            _status(),
            state_path=tmp_path / "consent-state.json",
            presentation_context_by_lane={"gold_live_runtime": _context()},
            consent_outcome_by_lane=outcome,
        )


def test_tampered_decision_or_projected_resolution_fails_closed(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "consent-state.json"
    tampered_decision = _decision("reject")
    tampered_decision["scope"]["proposed_value"] = "http://127.0.0.1:8098"
    with pytest.raises(ValueError, match="operator_consent_decision_not_admissible"):
        consent.project_operator_consent(
            _status(),
            state_path=state_path,
            presentation_context_by_lane={"gold_live_runtime": _context()},
            consent_outcome_by_lane={
                "gold_live_runtime": {"decision": tampered_decision}
            },
        )
    assert not state_path.exists()

    projected = consent.project_operator_consent(
        _status(),
        state_path=state_path,
        presentation_context_by_lane={"gold_live_runtime": _context()},
        consent_outcome_by_lane={
            "gold_live_runtime": {"decision": _decision("defer")}
        },
    )
    projected["consent"]["projected_resolutions"]["gold_live_runtime"][
        "decision"
    ] = "reject"
    blocked = consent.record_operator_consent(
        projected,
        state_path=state_path,
        now=NOW,
    )
    assert blocked["status"] == "blocked"
    assert blocked["blocking_reason"] == "operator_consent_projection_not_admissible"
    assert not state_path.exists()


def test_invalid_consent_state_is_never_used_for_suppression_or_overwritten(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "consent-state.json"
    state_path.write_text("{}", encoding="utf-8")
    state_path.chmod(0o600)
    original = state_path.read_bytes()

    with pytest.raises(ValueError, match="operator_consent_state_not_admissible"):
        consent.project_operator_consent(
            _status(),
            state_path=state_path,
            presentation_context_by_lane={"gold_live_runtime": _context()},
            consent_outcome_by_lane={
                "gold_live_runtime": {"decision": {"status": "pending"}}
            },
        )
    assert state_path.read_bytes() == original
