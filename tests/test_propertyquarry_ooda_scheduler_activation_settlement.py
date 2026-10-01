from __future__ import annotations

import json
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_scheduler_activation_execution as execution
from scripts import propertyquarry_ooda_scheduler_activation_settlement as settlement
from scripts import propertyquarry_ooda_scheduler_continuity as continuity
from scripts.propertyquarry_ooda_operator_status import (
    apply_operator_presentation_state,
    record_operator_presentation,
)


NOW = datetime(2026, 8, 26, 19, 35, tzinfo=timezone.utc)
RELEASE = {
    "runtime_commit_sha": "f" * 40,
    "envelope_commit_sha": "1" * 40,
    "web_image_digest": "sha256:" + ("2" * 64),
    "render_image_digest": "sha256:" + ("3" * 64),
    "preflight_script_sha256": "4" * 64,
}


def _continuity(
    state: str,
    *,
    now: datetime = NOW,
) -> dict[str, object]:
    active = state == "active"
    return {
        "schema": continuity.VERIFY_SCHEMA,
        "status": "verified",
        "continuity_state": state,
        "updated_at": now.isoformat(),
        "continuity_observed_at": now.isoformat(),
        "continuity_receipt_sha256": "a" * 64,
        "blocking_reason": "" if active else "scheduler_container_absent",
        "next_action": "continue verification",
        "source_evidence": {
            "runtime_observation": {
                "sha256": "b" * 64,
                "scheduler_condition": "running" if active else "absent",
            },
            "scheduler_iteration_witness": {
                "status": "verified" if active else "absent",
                "iteration_witness_sha256": "c" * 64 if active else "",
                "cycle_receipt_sha256": "d" * 64 if active else "",
                "cycle_binding_verified": active,
                "persistent_reevaluation_verified": active,
            },
        },
        "action_required": False,
        "interrupt_operator": False,
        "progress": {
            "current_evidence_verified": True,
            "receipt_integrity_verified": True,
        },
        "persistent_reevaluation_verified": active,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "verification_receipt_persisted": True,
    }


def _release_observation(
    *,
    present: bool,
    runtime_digit: str = "f",
    image_digit: str = "2",
    now: datetime = NOW,
) -> dict[str, object]:
    if not present:
        return settlement._release_observation(
            status="absent",
            observed_at=now,
            project="property",
            container_count=0,
        )
    return settlement._release_observation(
        status="observed",
        observed_at=now,
        project="property",
        container_count=1,
        container_id="5" * 64,
        container_state="running",
        container_health="healthy",
        image_digest="sha256:" + (image_digit * 64),
        release_commit_sha=runtime_digit * 40,
        release_image_digest="sha256:" + (image_digit * 64),
    )


def _history(
    execution_state: str | None,
    *,
    blocking_reason: str = "",
) -> dict[str, object]:
    if execution_state is None:
        return {
            "schema": execution.HISTORY_SCHEMA,
            "status": "verified",
            "history_state": "no_execution_history",
            "updated_at": NOW.isoformat(),
            "blocking_reason": "",
            "next_action": "continue verification",
            "execution_history_present": False,
            "action_required": False,
            "interrupt_operator": False,
            "authorization_recorded": False,
            "authorization_consumed": False,
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "deployment_attempted": False,
            "deployment_or_restart_performed": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "progress": {
                "current_evidence_verified": True,
                "claim_count": 0,
                "result_count": 0,
            },
        }
    claim_id = "pqsaec_" + ("5" * 24)
    decision_id = "pqsad_" + ("6" * 24)
    result_present = execution_state != "claimed"
    attempted = execution_state in {"deployment_failed", "succeeded"}
    latest = {
        "schema": execution.VERIFY_SCHEMA,
        "status": "verified",
        "execution_state": execution_state,
        "updated_at": NOW.isoformat(),
        "blocking_reason": blocking_reason,
        "claim_id": claim_id,
        "claim_sha256": "7" * 64,
        "claimed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        "request_id": "pqsar_" + ("8" * 24),
        "request_sha256": "9" * 64,
        "decision_id": decision_id,
        "decision_sha256": "e" * 64,
        "scope": {"compose_project": "property"},
        "release_evidence": dict(RELEASE),
        "authorization_recorded": True,
        "authorization_consumed": True,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_attempted": attempted,
        "deployment_or_restart_performed": attempted,
        "protected_operation_executed": attempted,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "result_sha256": "0" * 64 if result_present else "",
    }
    return {
        "schema": execution.HISTORY_SCHEMA,
        "status": "verified",
        "history_state": (
            "execution_result" if result_present else "claimed_without_result"
        ),
        "updated_at": NOW.isoformat(),
        "blocking_reason": blocking_reason,
        "next_action": "settle execution",
        "execution_history_present": True,
        "latest_execution": latest,
        "latest_claim_id": claim_id,
        "latest_claim_sha256": "7" * 64,
        "latest_result_sha256": "0" * 64 if result_present else "",
        "action_required": False,
        "interrupt_operator": False,
        "authorization_recorded": True,
        "authorization_consumed": True,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_attempted": attempted,
        "deployment_or_restart_performed": attempted,
        "protected_operation_executed": attempted,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {
            "current_evidence_verified": True,
            "claim_count": 1,
            "result_count": 1 if result_present else 0,
            "historical_claims_verified": True,
            "historical_results_verified": True,
        },
    }


def _build(
    *,
    continuity_state: str,
    history_state: str | None,
    release_present: bool,
    runtime_digit: str = "f",
    image_digit: str = "2",
) -> dict[str, object]:
    return settlement.build_scheduler_activation_settlement_receipt(
        scheduler_continuity=_continuity(continuity_state),
        execution_history=_history(history_state),
        release_identity_observation=_release_observation(
            present=release_present,
            runtime_digit=runtime_digit,
            image_digit=image_digit,
        ),
        now=NOW,
    )


def test_no_history_distinguishes_inactive_from_externally_active() -> None:
    inactive = _build(
        continuity_state="inactive",
        history_state=None,
        release_present=False,
    )
    active = _build(
        continuity_state="active",
        history_state=None,
        release_present=True,
    )
    assert inactive["status"] == "no_governed_execution"
    assert active["status"] == "externally_active"
    assert inactive["action_required"] is False
    assert active["action_required"] is False
    assert active["authorization_recorded"] is False
    assert active["progress"]["governed_execution_settled"] is False


def test_exact_success_settles_only_against_active_matching_release() -> None:
    receipt = _build(
        continuity_state="active",
        history_state="succeeded",
        release_present=True,
    )
    verified = settlement.verify_scheduler_activation_settlement_receipt(
        receipt,
        scheduler_continuity=_continuity("active"),
        execution_history=_history("succeeded"),
        release_identity_observation=_release_observation(present=True),
        now=NOW,
    )
    assert verified["status"] == "verified"
    assert verified["settlement_state"] == "settled_success"
    assert verified["action_required"] is False
    assert verified["authorization_consumed"] is True
    assert verified["execution_authorized"] is False
    assert verified["historical_deployment_attempted"] is True
    assert verified["protected_operation_executed"] is False
    assert verified["historical_protected_operation_executed"] is True
    assert verified["progress"]["governed_execution_settled"] is True


def test_success_with_inactive_or_changed_release_requires_recovery() -> None:
    inactive = _build(
        continuity_state="inactive",
        history_state="succeeded",
        release_present=False,
    )
    changed = _build(
        continuity_state="active",
        history_state="succeeded",
        release_present=True,
        runtime_digit="0",
    )
    assert inactive["status"] == "recovery_required"
    assert inactive["blocking_reason"] == (
        "scheduler_activation_success_not_currently_active"
    )
    assert changed["status"] == "recovery_required"
    assert changed["blocking_reason"] == (
        "scheduler_activation_current_release_identity_mismatch"
    )
    assert inactive["action_required"] is True
    assert changed["action_required"] is True
    assert "do not replay" in inactive["next_action"]


def test_claim_or_failed_result_requires_recovery_without_new_authority() -> None:
    claimed = _build(
        continuity_state="inactive",
        history_state="claimed",
        release_present=False,
    )
    failed = settlement.build_scheduler_activation_settlement_receipt(
        scheduler_continuity=_continuity("inactive"),
        execution_history=_history(
            "deployment_failed",
            blocking_reason="activation_deployment_failed",
        ),
        release_identity_observation=_release_observation(present=False),
        now=NOW,
    )
    for receipt in (claimed, failed):
        assert receipt["status"] == "recovery_required"
        assert receipt["action_required"] is True
        assert receipt["execution_authorized"] is False
        assert receipt["deployment_or_restart_authorized"] is False
        assert receipt["provider_quota_consumption_allowed"] is False
        assert receipt["delivery_authorized"] is False


def test_recovery_presentation_is_semantically_deduplicated(
    tmp_path: Path,
) -> None:
    receipt = _build(
        continuity_state="inactive",
        history_state="succeeded",
        release_present=False,
    )
    verified = settlement.verify_scheduler_activation_settlement_receipt(
        receipt,
        scheduler_continuity=_continuity("inactive"),
        execution_history=_history("succeeded"),
        release_identity_observation=_release_observation(present=False),
        now=NOW,
    )
    state_path = tmp_path / "presentation.json"
    first = settlement.project_scheduler_activation_settlement_handoff(
        verified
    )
    first = apply_operator_presentation_state(first, state_path=state_path)
    assert first["interrupt_operator"] is True
    recorded = record_operator_presentation(
        first,
        state_path=state_path,
        expected_source_cycle_receipt_sha256="a" * 64,
        now=NOW,
    )
    assert recorded["status"] == "recorded"

    refreshed = settlement.build_scheduler_activation_settlement_receipt(
        scheduler_continuity=_continuity(
            "inactive",
            now=NOW + timedelta(seconds=1),
        ),
        execution_history={
            **_history("succeeded"),
            "updated_at": (NOW + timedelta(seconds=1)).isoformat(),
        },
        release_identity_observation=_release_observation(
            present=False,
            now=NOW + timedelta(seconds=1),
        ),
        now=NOW + timedelta(seconds=1),
    )
    refreshed_verified = (
        settlement.verify_scheduler_activation_settlement_receipt(
            refreshed,
            scheduler_continuity=_continuity(
                "inactive",
                now=NOW + timedelta(seconds=1),
            ),
            execution_history={
                **_history("succeeded"),
                "updated_at": (NOW + timedelta(seconds=1)).isoformat(),
            },
            release_identity_observation=_release_observation(
                present=False,
                now=NOW + timedelta(seconds=1),
            ),
            now=NOW + timedelta(seconds=1),
        )
    )
    repeated = settlement.project_scheduler_activation_settlement_handoff(
        refreshed_verified
    )
    repeated = apply_operator_presentation_state(
        repeated,
        state_path=state_path,
    )
    assert repeated["status"] == "pending_action"
    assert repeated["interrupt_operator"] is False
    assert repeated["actions"] == []


def test_stale_or_tampered_sources_fail_closed() -> None:
    receipt = _build(
        continuity_state="active",
        history_state="succeeded",
        release_present=True,
    )
    stale = settlement.verify_scheduler_activation_settlement_receipt(
        receipt,
        scheduler_continuity=_continuity("active"),
        execution_history=_history("succeeded"),
        release_identity_observation=_release_observation(present=True),
        now=NOW + timedelta(seconds=301),
    )
    assert stale["blocking_reason"] == "scheduler_activation_settlement_not_fresh"

    tampered = dict(receipt)
    tampered["historical_deployment_attempted"] = False
    normalized = dict(tampered)
    normalized.pop("integrity")
    tampered["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": settlement._sha256(
            settlement._canonical(normalized)
        ),
    }
    invalid = settlement.verify_scheduler_activation_settlement_receipt(
        tampered,
        scheduler_continuity=_continuity("active"),
        execution_history=_history("succeeded"),
        release_identity_observation=_release_observation(present=True),
        now=NOW,
    )
    assert invalid["blocking_reason"] == (
        "scheduler_activation_settlement_source_binding_mismatch"
    )


def test_materialized_no_history_receipts_are_private(
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "settlement.json"
    verification_path = tmp_path / "settlement-verification.json"
    result = settlement.materialize_current_scheduler_activation_settlement_bundle(
        scheduler_continuity=_continuity("inactive"),
        execution_dir=tmp_path / "executions",
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
        release_identity_observation=_release_observation(present=False),
    )
    assert result["status"] == "verified"
    assert result["settlement_state"] == "no_governed_execution"
    assert result["verification_receipt_persisted"] is True
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600
    persisted = json.loads(verification_path.read_text())
    assert persisted["provider_quota_consumption_allowed"] is False
    assert persisted["delivery_authorized"] is False


def test_release_observer_projects_absence_without_authority(monkeypatch) -> None:
    def fake_run(*_: object, **__: object):
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(settlement.subprocess, "run", fake_run)
    observed = settlement.observe_current_scheduler_release_identity(now=NOW)
    assert observed["status"] == "absent"
    assert observed["container_count"] == 0
    assert observed["protected_operation_executed"] is False
    assert observed["provider_quota_consumption_allowed"] is False
    assert observed["delivery_authorized"] is False
