from __future__ import annotations

import json
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_scheduler_activation_authorization as authorization
from scripts import propertyquarry_ooda_scheduler_activation_decision as decision
from scripts import propertyquarry_ooda_scheduler_activation_readiness as readiness


NOW = datetime(2026, 8, 26, 18, 45, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _readiness(
    *,
    receipt_digest: str = "a" * 64,
    render_digit: str = "3",
    now: datetime = NOW,
) -> dict[str, object]:
    return {
        "schema": readiness.VERIFY_SCHEMA,
        "status": "verified",
        "readiness_state": "ready_for_authorization",
        "updated_at": now.isoformat(),
        "readiness_observed_at": now.isoformat(),
        "readiness_receipt_sha256": receipt_digest,
        "blocking_reason": "explicit_scheduler_activation_authorization_required",
        "scope": {
            **authorization._EXPECTED_SCOPE,
            "compose_project": "property",
        },
        "source_evidence": {
            "activation_preflight": {
                "status": "ready",
                "runtime_commit_sha": "f" * 40,
                "envelope_commit_sha": "1" * 40,
                "web_image_digest": "sha256:" + ("2" * 64),
                "render_image_digest": "sha256:" + (render_digit * 64),
                "build_performed": False,
                "deployment_or_restart_performed": False,
                "provider_quota_consumed": False,
                "delivery_attempted": False,
            }
        },
        "source_bindings": {
            "continuity_receipt_sha256": "b" * 64,
            "runtime_observation_sha256": "c" * 64,
            "preflight_observation_sha256": "d" * 64,
            "preflight_script_sha256": "e" * 64,
        },
        "action_required": True,
        "interrupt_operator": False,
        "progress": {
            "activation_preflight_current": True,
            "activation_preflight_passed": True,
            "current_evidence_verified": True,
            "receipt_integrity_verified": True,
        },
        "authorization_required": True,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "verification_receipt_persisted": True,
    }


def _request() -> dict[str, object]:
    return authorization.build_scheduler_activation_authorization_request(
        activation_readiness=_readiness(),
        now=NOW,
    )


def test_explicit_approval_is_the_only_authority_bearing_decision() -> None:
    request = _request()
    receipt = decision.build_scheduler_activation_authorization_decision(
        request=request,
        activation_readiness=_readiness(),
        decision="approve_exact_scope",
        decider_id="operator:test",
        now=NOW + timedelta(seconds=1),
    )
    verified = decision.verify_scheduler_activation_authorization_decision(
        receipt,
        request=request,
        activation_readiness=_readiness(),
        now=NOW + timedelta(seconds=1),
    )

    assert verified["status"] == "verified"
    assert verified["decision"] == "approve_exact_scope"
    assert verified["authorization_recorded"] is True
    assert verified["exact_scope_authorized"] is True
    assert verified["manual_deployment_authorized"] is True
    assert verified["execution_authorized"] is True
    assert verified["deployment_or_restart_authorized"] is True
    assert verified["automatic_execution_allowed"] is False
    assert verified["deployment_or_restart_performed"] is False
    assert verified["protected_operation_executed"] is False
    assert verified["provider_quota_consumption_allowed"] is False
    assert verified["delivery_authorized"] is False


@pytest.mark.parametrize("decision_value", ["reject", "defer"])
def test_negative_decisions_record_outcome_without_authority(
    decision_value: str,
) -> None:
    request = _request()
    receipt = decision.build_scheduler_activation_authorization_decision(
        request=request,
        activation_readiness=_readiness(),
        decision=decision_value,
        decider_id="operator:test",
        now=NOW + timedelta(seconds=1),
    )
    verified = decision.verify_scheduler_activation_authorization_decision(
        receipt,
        request=request,
        activation_readiness=_readiness(),
        now=NOW + timedelta(seconds=1),
    )

    assert verified["status"] == "verified"
    assert verified["decision"] == decision_value
    assert verified["authorization_recorded"] is True
    assert verified["exact_scope_authorized"] is False
    assert verified["manual_deployment_authorized"] is False
    assert verified["execution_authorized"] is False
    assert verified["deployment_or_restart_authorized"] is False


def test_decision_rejects_stale_tampered_or_rebound_evidence() -> None:
    request = _request()
    receipt = decision.build_scheduler_activation_authorization_decision(
        request=request,
        activation_readiness=_readiness(),
        decision="approve_exact_scope",
        decider_id="operator:test",
        now=NOW + timedelta(seconds=1),
    )

    stale = decision.verify_scheduler_activation_authorization_decision(
        receipt,
        request=request,
        activation_readiness=_readiness(),
        now=NOW + timedelta(seconds=901),
    )
    assert stale["blocking_reason"] == "scheduler_activation_decision_not_fresh"

    tampered = dict(receipt)
    tampered["deployment_or_restart_performed"] = True
    normalized = dict(tampered)
    normalized.pop("integrity")
    tampered["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": decision._sha256(
            decision._canonical(normalized)
        ),
    }
    invalid = decision.verify_scheduler_activation_authorization_decision(
        tampered,
        request=request,
        activation_readiness=_readiness(),
        now=NOW + timedelta(seconds=1),
    )
    assert invalid["blocking_reason"] == (
        "scheduler_activation_decision_source_binding_mismatch"
    )

    rebound = decision.verify_scheduler_activation_authorization_decision(
        receipt,
        request=request,
        activation_readiness=_readiness(render_digit="4"),
        now=NOW + timedelta(seconds=1),
    )
    assert rebound["blocking_reason"] == (
        "scheduler_activation_decision_source_not_admissible"
    )
    assert rebound["deployment_or_restart_authorized"] is False


def test_record_current_decision_requires_expected_request_and_private_files(
    tmp_path: Path,
) -> None:
    request = _request()
    activation_readiness = _readiness()
    request_path = tmp_path / "request.json"
    readiness_path = tmp_path / "readiness.json"
    decision_dir = tmp_path / "decisions"
    request_path.write_bytes(authorization._canonical(request))
    request_path.chmod(0o600)
    readiness_path.write_bytes(authorization._canonical(activation_readiness))
    readiness_path.chmod(0o600)
    request_digest = authorization._sha256(authorization._canonical(request))

    pending = decision.verify_current_scheduler_activation_authorization_decision(
        request_path=request_path,
        readiness_path=readiness_path,
        decision_dir=decision_dir,
        now=NOW + timedelta(seconds=1),
    )
    assert pending["status"] == "pending"
    assert pending["authorization_recorded"] is False

    mismatch = decision.record_current_scheduler_activation_authorization_decision(
        decision="approve_exact_scope",
        decider_id="operator:test",
        expected_request_id=str(request["request_id"]),
        expected_request_sha256="9" * 64,
        request_path=request_path,
        readiness_path=readiness_path,
        decision_dir=decision_dir,
        now=NOW + timedelta(seconds=1),
    )
    assert mismatch["status"] == "blocked"
    assert mismatch["blocking_reason"] == (
        "scheduler_activation_decision_expected_request_mismatch"
    )
    assert not decision_dir.exists()

    recorded = decision.record_current_scheduler_activation_authorization_decision(
        decision="approve_exact_scope",
        decider_id="operator:test",
        expected_request_id=str(request["request_id"]),
        expected_request_sha256=request_digest,
        request_path=request_path,
        readiness_path=readiness_path,
        decision_dir=decision_dir,
        now=NOW + timedelta(seconds=1),
    )
    decision_path = Path(str(recorded["decision_path"]))
    assert recorded["status"] == "verified"
    assert recorded["decision_receipt_persisted"] is True
    assert stat.S_IMODE(decision_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(decision_path.stat().st_mode) == 0o600

    current = decision.verify_current_scheduler_activation_authorization_decision(
        request_path=request_path,
        readiness_path=readiness_path,
        decision_dir=decision_dir,
        now=NOW + timedelta(seconds=2),
    )
    assert current["status"] == "verified"
    assert current["decision"] == "approve_exact_scope"

    refreshed_readiness = _readiness(
        receipt_digest="8" * 64,
        now=NOW + timedelta(seconds=2),
    )
    readiness_path.write_bytes(
        authorization._canonical(refreshed_readiness)
    )
    refreshed_current = (
        decision.verify_current_scheduler_activation_authorization_decision(
            request_path=request_path,
            readiness_path=readiness_path,
            decision_dir=decision_dir,
            now=NOW + timedelta(seconds=3),
        )
    )
    assert refreshed_current["status"] == "verified"
    assert refreshed_current["decision_id"] == current["decision_id"]
    assert refreshed_current["readiness_receipt_sha256"] == "a" * 64

    duplicate = decision.record_current_scheduler_activation_authorization_decision(
        decision="defer",
        decider_id="operator:test",
        expected_request_id=str(request["request_id"]),
        expected_request_sha256=request_digest,
        request_path=request_path,
        readiness_path=readiness_path,
        decision_dir=decision_dir,
        now=NOW + timedelta(seconds=4),
    )
    assert duplicate["status"] == "blocked"
    assert duplicate["blocking_reason"] == (
        "scheduler_activation_decision_already_recorded"
    )


def test_decision_cli_records_only_the_explicit_expected_request(
    tmp_path: Path,
) -> None:
    current_now = datetime.now(timezone.utc)
    activation_readiness = _readiness(now=current_now)
    request = authorization.build_scheduler_activation_authorization_request(
        activation_readiness=activation_readiness,
        now=current_now,
    )
    request_path = tmp_path / "request.json"
    readiness_path = tmp_path / "readiness.json"
    decision_dir = tmp_path / "cli-decisions"
    request_path.write_bytes(authorization._canonical(request))
    request_path.chmod(0o600)
    readiness_path.write_bytes(authorization._canonical(activation_readiness))
    readiness_path.chmod(0o600)
    request_digest = authorization._sha256(authorization._canonical(request))

    result = subprocess.run(
        [
            sys.executable,
            "scripts/propertyquarry_ooda_scheduler_activation_decision.py",
            "--decision",
            "reject",
            "--decider-id",
            "operator:test",
            "--request-id",
            str(request["request_id"]),
            "--request-sha256",
            request_digest,
            "--request",
            str(request_path),
            "--readiness",
            str(readiness_path),
            "--decision-dir",
            str(decision_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)

    assert payload["status"] == "verified"
    assert payload["decision"] == "reject"
    assert payload["authorization_recorded"] is True
    assert payload["deployment_or_restart_authorized"] is False
    assert payload["deployment_or_restart_performed"] is False


def test_expired_prior_receipt_for_same_semantic_request_does_not_block_renewal(
    tmp_path: Path,
) -> None:
    old_request = _request()
    old_decision = decision.build_scheduler_activation_authorization_decision(
        request=old_request,
        activation_readiness=_readiness(),
        decision="approve_exact_scope",
        decider_id="operator:test",
        now=NOW + timedelta(seconds=1),
    )
    renewed_at = NOW + timedelta(seconds=901)
    current_readiness = _readiness(
        receipt_digest="7" * 64,
        now=renewed_at,
    )
    current_request = (
        authorization.build_scheduler_activation_authorization_request(
            activation_readiness=current_readiness,
            now=renewed_at,
        )
    )
    assert current_request["request_id"] == old_request["request_id"]
    assert authorization._canonical(current_request) != authorization._canonical(
        old_request
    )

    request_path = tmp_path / "request.json"
    readiness_path = tmp_path / "readiness.json"
    decision_dir = tmp_path / "decisions"
    request_path.write_bytes(authorization._canonical(current_request))
    request_path.chmod(0o600)
    readiness_path.write_bytes(authorization._canonical(current_readiness))
    readiness_path.chmod(0o600)
    decision_dir.mkdir(mode=0o700)
    old_path = decision_dir / "old-decision.json"
    old_path.write_bytes(decision._canonical(old_decision))
    old_path.chmod(0o600)

    current = decision.verify_current_scheduler_activation_authorization_decision(
        request_path=request_path,
        readiness_path=readiness_path,
        decision_dir=decision_dir,
        now=renewed_at + timedelta(seconds=1),
    )
    assert current["status"] == "pending"
    assert current["request_sha256"] == authorization._sha256(
        authorization._canonical(current_request)
    )
