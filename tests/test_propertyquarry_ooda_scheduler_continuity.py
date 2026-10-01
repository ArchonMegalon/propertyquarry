from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts import propertyquarry_ooda_scheduler_continuity as continuity
from scripts import propertyquarry_ooda_scheduler_witness as scheduler_witness


NOW = datetime(2026, 8, 26, 17, 45, tzinfo=timezone.utc)


def _runtime(
    *,
    condition: str = "absent",
    state: str = "",
    health: str = "none",
    scheduler_count: int = 0,
    running_count: int = 0,
    healthy_count: int = 0,
) -> dict[str, object]:
    container_healthy = condition == "running"
    return {
        "schema": runtime_review.RUNTIME_OBSERVATION_VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": NOW.isoformat(),
        "blocking_reason": "",
        "runtime_condition": "present" if scheduler_count else "absent",
        "runtime_observed_at": NOW.isoformat(),
        "current_observed_at": NOW.isoformat(),
        "runtime_observation_sha256": "a" * 64,
        "scheduler_service": runtime_review.PROPERTYQUARRY_SCHEDULER_SERVICE,
        "scheduler_condition": condition,
        "scheduler_state": state,
        "scheduler_health": health,
        "scheduler_container_healthy": container_healthy,
        "persistent_reevaluation_running": False,
        "progress": {
            "container_count": scheduler_count,
            "running_container_count": running_count,
            "non_running_container_count": scheduler_count - running_count,
            "scheduler_container_count": scheduler_count,
            "running_scheduler_container_count": running_count,
            "healthy_scheduler_container_count": healthy_count,
            "current_evidence_verified": True,
        },
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _healthy_runtime() -> dict[str, object]:
    return _runtime(
        condition="running",
        state="running",
        health="healthy",
        scheduler_count=1,
        running_count=1,
        healthy_count=1,
    )


def _witness(
    *,
    persistent: bool = True,
    iteration_status: str = "completed",
    cycle_binding: bool = True,
) -> dict[str, object]:
    return {
        "schema": scheduler_witness.VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": NOW.isoformat(),
        "blocking_reason": "",
        "iteration_status": iteration_status,
        "iteration_blocking_reason": (
            "" if persistent else "scheduler_step_timeout"
        ),
        "iteration_updated_at": NOW.isoformat(),
        "iteration_witness_sha256": "b" * 64,
        "cycle_evidence": (
            {
                "sha256": "c" * 64,
                "status": "silent",
                "execution_mode": "evaluate_only",
            }
            if cycle_binding
            else {}
        ),
        "progress": {
            "cycle_binding_verified": cycle_binding,
            "current_evidence_verified": True,
        },
        "persistent_reevaluation_verified": persistent,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def test_absent_scheduler_materializes_verified_private_inactive_continuity(
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "scheduler-continuity.json"
    verification_path = tmp_path / "scheduler-continuity-verification.json"

    verified = continuity.materialize_current_scheduler_continuity_bundle(
        runtime_observation=_runtime(),
        scheduler_iteration_witness=None,
        source_type="operator_filesystem_no_runtime_container",
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["continuity_state"] == "inactive"
    assert verified["blocking_reason"] == "scheduler_container_absent"
    assert verified["progress"]["current_evidence_verified"] is True
    assert verified["progress"]["receipt_integrity_verified"] is True
    assert verified["persistent_reevaluation_verified"] is False
    assert verified["verification_receipt_persisted"] is True
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600
    assert verified["execution_authorized"] is False
    assert verified["deployment_or_restart_authorized"] is False
    assert verified["provider_quota_consumption_allowed"] is False
    assert verified["delivery_authorized"] is False


def test_continuity_is_active_only_with_healthy_container_and_bound_witness() -> None:
    receipt = continuity.build_scheduler_continuity_receipt(
        runtime_observation=_healthy_runtime(),
        scheduler_iteration_witness=_witness(),
        source_type="runtime_container",
        now=NOW,
    )
    verified = continuity.verify_scheduler_continuity_receipt(
        receipt,
        runtime_observation=_healthy_runtime(),
        scheduler_iteration_witness=_witness(),
        now=NOW,
    )

    assert receipt["status"] == "active"
    assert receipt["blocking_reason"] == ""
    assert receipt["source_evidence"]["runtime_observation"]["sha256"] == (
        "a" * 64
    )
    assert receipt["source_evidence"]["scheduler_iteration_witness"][
        "iteration_witness_sha256"
    ] == ("b" * 64)
    assert receipt["source_evidence"]["scheduler_iteration_witness"][
        "cycle_receipt_sha256"
    ] == ("c" * 64)
    assert verified["status"] == "verified"
    assert verified["continuity_state"] == "active"
    assert verified["persistent_reevaluation_verified"] is True


@pytest.mark.parametrize(
    ("runtime", "witness", "expected_state", "expected_reason"),
    [
        (
            _healthy_runtime(),
            None,
            "blocked",
            "scheduler_iteration_witness_absent",
        ),
        (
            _healthy_runtime(),
            _witness(
                persistent=False,
                iteration_status="timeout",
                cycle_binding=False,
            ),
            "blocked",
            "scheduler_step_timeout",
        ),
        (
            _runtime(
                condition="unhealthy",
                state="running",
                health="unhealthy",
                scheduler_count=1,
                running_count=1,
                healthy_count=0,
            ),
            _witness(),
            "degraded",
            "scheduler_container_unhealthy",
        ),
        (
            _runtime(),
            _witness(),
            "degraded",
            "scheduler_witness_present_without_container",
        ),
    ],
)
def test_continuity_fails_closed_for_unmatched_or_incomplete_sources(
    runtime: dict[str, object],
    witness: dict[str, object] | None,
    expected_state: str,
    expected_reason: str,
) -> None:
    receipt = continuity.build_scheduler_continuity_receipt(
        runtime_observation=runtime,
        scheduler_iteration_witness=witness,
        source_type="runtime_container",
        now=NOW,
    )

    assert receipt["status"] == expected_state
    assert receipt["blocking_reason"] == expected_reason
    assert receipt["persistent_reevaluation_verified"] is False
    assert receipt["action_required"] is False
    assert receipt["interrupt_operator"] is False
    assert receipt["execution_authorized"] is False


def test_continuity_rejects_stale_tampered_or_rebound_sources() -> None:
    runtime = _healthy_runtime()
    witness = _witness()
    receipt = continuity.build_scheduler_continuity_receipt(
        runtime_observation=runtime,
        scheduler_iteration_witness=witness,
        source_type="runtime_container",
        now=NOW,
    )

    stale = continuity.verify_scheduler_continuity_receipt(
        receipt,
        runtime_observation=runtime,
        scheduler_iteration_witness=witness,
        now=NOW + timedelta(seconds=301),
    )
    assert stale["blocking_reason"] == "scheduler_continuity_receipt_not_fresh"

    tampered = dict(receipt)
    tampered["deployment_or_restart_authorized"] = True
    normalized = dict(tampered)
    normalized.pop("integrity")
    tampered["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": continuity._sha256(
            continuity._canonical(normalized)
        ),
    }
    invalid = continuity.verify_scheduler_continuity_receipt(
        tampered,
        runtime_observation=runtime,
        scheduler_iteration_witness=witness,
        now=NOW,
    )
    assert invalid["blocking_reason"] == (
        "scheduler_continuity_source_binding_mismatch"
    )

    changed_runtime = dict(runtime)
    changed_runtime["runtime_observation_sha256"] = "d" * 64
    rebound = continuity.verify_scheduler_continuity_receipt(
        receipt,
        runtime_observation=changed_runtime,
        scheduler_iteration_witness=witness,
        now=NOW,
    )
    assert rebound["blocking_reason"] == (
        "scheduler_continuity_source_binding_mismatch"
    )
    assert rebound["persistent_reevaluation_verified"] is False
