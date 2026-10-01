from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_notification_cycle as cycle
from scripts import propertyquarry_ooda_scheduler_witness as witness


NOW = datetime(2026, 8, 26, 17, 30, tzinfo=timezone.utc)


def _summary(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "ran": True,
        "status": "silent",
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "would_send": False,
        "action_required_count": 0,
        "novel_action_count": 0,
        "errors": 0,
    }
    result.update(overrides)
    return result


def _write_cycle(path: Path, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": cycle.SCHEMA,
        "generated_at": NOW.isoformat(),
        "status": "silent",
        "execution_mode": "evaluate_only",
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "would_send": False,
        "action_required_count": 0,
        "novel_action_count": 0,
        "signal_approval": {
            "approved": False,
            "revocation_verified": True,
            "reason": "approved_projection_manifest_revoked",
        },
    }
    payload.update(overrides)
    path.write_bytes(approved.canonical_json_bytes(payload))
    path.chmod(0o600)
    return payload


def test_completed_iteration_is_private_cycle_bound_and_current(
    tmp_path: Path,
) -> None:
    cycle_path = tmp_path / "latest.json"
    receipt_path = tmp_path / "scheduler-iteration.json"
    _write_cycle(cycle_path)

    receipt = witness.persist_scheduler_iteration_receipt(
        _summary(),
        receipt_path=receipt_path,
        cycle_receipt_path=cycle_path,
        now=NOW,
    )
    verified = witness.verify_current_scheduler_iteration(
        receipt_path=receipt_path,
        cycle_receipt_path=cycle_path,
        now=NOW,
    )

    assert receipt["status"] == "completed"
    assert receipt["cycle_evidence"]["status"] == "silent"
    assert receipt["cycle_evidence"]["execution_mode"] == "evaluate_only"
    assert receipt["cycle_evidence"]["signal_approval_reason"] == (
        "approved_projection_manifest_revoked"
    )
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert verified["status"] == "verified"
    assert verified["iteration_status"] == "completed"
    assert verified["progress"]["cycle_binding_verified"] is True
    assert verified["progress"]["current_evidence_verified"] is True
    assert verified["persistent_reevaluation_verified"] is True
    assert verified["automatic_execution_allowed"] is False
    assert verified["deployment_or_restart_authorized"] is False
    assert verified["provider_quota_consumption_allowed"] is False
    assert verified["delivery_authorized"] is False
    assert verified["delivery_attempted"] is False
    assert verified["sent"] is False


def test_witness_freshness_window_tracks_bounded_scheduler_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_SCHEDULER_PROPERTYQUARRY_OODA_INTERVAL_SECONDS", "3600")
    monkeypatch.delenv(
        "EA_SCHEDULER_PROPERTYQUARRY_OODA_WITNESS_MAX_AGE_SECONDS",
        raising=False,
    )
    assert witness.configured_max_age_seconds() == 7200.0

    monkeypatch.setenv(
        "EA_SCHEDULER_PROPERTYQUARRY_OODA_WITNESS_MAX_AGE_SECONDS",
        "999999",
    )
    assert witness.configured_max_age_seconds() == 86400.0


@pytest.mark.parametrize(
    ("summary", "expected_status", "expected_reason"),
    [
        (
            _summary(
                ran=False,
                status="delivery_authority_incomplete",
                errors=1,
            ),
            "blocked",
            "delivery_authority_incomplete",
        ),
        (
            _summary(
                ran=True,
                status="timeout",
                timeout=True,
                running=True,
                errors=1,
            ),
            "timeout",
            "scheduler_step_timeout",
        ),
        (
            _summary(
                ran=True,
                status="timeout",
                deferred=True,
                errors=1,
            ),
            "deferred",
            "scheduler_step_deferred",
        ),
        (
            _summary(
                ran=True,
                status="timeout",
                shutdown=True,
                errors=1,
            ),
            "stopped",
            "scheduler_shutdown_observed",
        ),
    ],
)
def test_noncompleted_iteration_is_current_but_never_continuity_proof(
    tmp_path: Path,
    summary: dict[str, object],
    expected_status: str,
    expected_reason: str,
) -> None:
    receipt_path = tmp_path / f"{expected_status}.json"

    receipt = witness.persist_scheduler_iteration_receipt(
        summary,
        receipt_path=receipt_path,
        cycle_receipt_path=tmp_path / "missing-cycle.json",
        now=NOW,
    )
    verified = witness.verify_current_scheduler_iteration(
        receipt_path=receipt_path,
        cycle_receipt_path=tmp_path / "missing-cycle.json",
        now=NOW,
    )

    assert receipt["status"] == expected_status
    assert receipt["blocking_reason"] == expected_reason
    assert receipt["cycle_evidence"] == {}
    assert verified["status"] == "verified"
    assert verified["iteration_status"] == expected_status
    assert verified["iteration_blocking_reason"] == expected_reason
    assert verified["progress"]["current_evidence_verified"] is True
    assert verified["progress"]["cycle_binding_verified"] is False
    assert verified["persistent_reevaluation_verified"] is False
    assert verified["action_required"] is False
    assert verified["interrupt_operator"] is False


def test_failed_iteration_records_only_safe_error_type(tmp_path: Path) -> None:
    receipt_path = tmp_path / "failed.json"

    receipt = witness.persist_scheduler_iteration_receipt(
        _summary(ran=False, status="iteration_exception", errors=1),
        receipt_path=receipt_path,
        cycle_receipt_path=tmp_path / "missing-cycle.json",
        error_type="RuntimeError",
        now=NOW,
    )

    assert receipt["status"] == "failed"
    assert receipt["error_type"] == "RuntimeError"
    assert "must-not-leak" not in json.dumps(receipt)
    assert receipt["secret_values_recorded"] is False


def test_witness_rejects_stale_tampered_or_rebound_cycle(tmp_path: Path) -> None:
    cycle_path = tmp_path / "latest.json"
    receipt_path = tmp_path / "scheduler-iteration.json"
    _write_cycle(cycle_path)
    receipt = witness.persist_scheduler_iteration_receipt(
        _summary(),
        receipt_path=receipt_path,
        cycle_receipt_path=cycle_path,
        now=NOW,
    )

    stale = witness.verify_scheduler_iteration_receipt(
        receipt,
        cycle_receipt_path=cycle_path,
        now=NOW + timedelta(seconds=1801),
    )
    assert stale["blocking_reason"] == "scheduler_iteration_witness_not_fresh"

    tampered = dict(receipt)
    tampered["deployment_or_restart_authorized"] = True
    normalized = dict(tampered)
    normalized.pop("integrity")
    tampered["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": witness._sha256(witness._canonical(normalized)),
    }
    invalid = witness.verify_scheduler_iteration_receipt(
        tampered,
        cycle_receipt_path=cycle_path,
        now=NOW,
    )
    assert invalid["blocking_reason"] == (
        "scheduler_iteration_witness_contract_invalid"
    )

    _write_cycle(cycle_path, status="action_required", action_required_count=1)
    rebound = witness.verify_scheduler_iteration_receipt(
        receipt,
        cycle_receipt_path=cycle_path,
        now=NOW,
    )
    assert rebound["blocking_reason"] == (
        "scheduler_iteration_cycle_binding_unavailable"
    )
    assert rebound["persistent_reevaluation_verified"] is False


def test_cli_verifies_current_iteration_without_mutating_authority(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cycle_path = tmp_path / "latest.json"
    receipt_path = tmp_path / "scheduler-iteration.json"
    _write_cycle(cycle_path)
    witness.persist_scheduler_iteration_receipt(
        _summary(),
        receipt_path=receipt_path,
        cycle_receipt_path=cycle_path,
        now=NOW,
    )

    exit_code = witness.main(
        [
            "--verify",
            "--receipt",
            str(receipt_path),
            "--cycle-receipt",
            str(cycle_path),
            "--max-age-seconds",
            "999999999",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "verified"
    assert output["persistent_reevaluation_verified"] is True
    assert output["deployment_or_restart_authorized"] is False
