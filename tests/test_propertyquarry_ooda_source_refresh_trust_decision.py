from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_source_refresh_trust_decision as decision


NOW = datetime(2026, 8, 26, 22, 30, tzinfo=timezone.utc)


def _report(*, expires_at: datetime | None = None) -> dict[str, object]:
    candidate = {
        "producer_id": "receipt-producer.test",
        "key_id": "receipt-producer.test.2026-08",
        "public_key_sha256": "6" * 64,
        "lanes": ["gold_live_runtime"],
        "candidate_receipt_sha256": "7" * 64,
        "expires_at": (expires_at or NOW + timedelta(minutes=30)).isoformat(),
        "proof_of_possession_verified": True,
        "out_of_band_identity_verification_required": True,
        "candidate_confers_authority": False,
        "trust_enrollment_authorized": False,
    }
    return {
        "schema": decision.trust_intake.VERIFY_SCHEMA,
        "status": "verified",
        "intake_state": "candidate_ready_for_operator_review",
        "request_id": "pqtrustintake_0123456789abcdef01234567",
        "semantic_request_sha256": "8" * 64,
        "candidate_review_id": "pqtrustreview_0123456789abcdef01234567",
        "review_decision_options": list(decision.DECISIONS),
        "candidates": [candidate],
        "candidate_coverage": {
            "covered_lanes": ["gold_live_runtime"],
            "remaining_lanes": [],
            "all_requested_lanes_covered": True,
        },
        "next_action": "verify candidate identity out of band",
        "intake_receipt_sha256": "9" * 64,
        "verification_receipt_sha256": "a" * 64,
        "progress": {"candidate_evidence_count": 1},
        "action_required": True,
        "interrupt_operator": True,
        "operator_review_required": True,
        "public_key_candidate_recorded": True,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "producer_dispatch_authorized": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "protected_operation_executed": False,
    }


def _confirm_receipt(
    report: dict[str, object],
    *,
    now: datetime = NOW,
) -> dict[str, object]:
    return decision.build_candidate_review_decision(
        report,
        decision="confirm_identity_verified",
        decider_id="operator.local",
        acknowledged_public_key_sha256s=["6" * 64],
        identity_verification_method="trusted_channel",
        identity_evidence_ref="ticket:IDENTITY-123",
        now=now,
    )


def test_current_review_is_pending_without_immutable_decision(
    tmp_path: Path,
) -> None:
    report = _report()

    result = decision.verify_candidate_review_decision_for_report(
        report,
        decision_dir=tmp_path / "decisions",
        now=NOW,
    )

    assert result["status"] == "pending"
    assert result["action_required"] is True
    assert result["interrupt_operator"] is False
    assert result["operator_review_required"] is True
    assert result["decision_options"] == list(decision.DECISIONS)
    assert result["trust_enrollment_preview_authorized"] is False
    assert result["trust_enrollment_authorized"] is False
    assert result["trust_registry_modified"] is False


def test_confirmation_authorizes_only_a_reversible_enrollment_preview() -> None:
    report = _report()
    receipt = _confirm_receipt(report)

    verified = decision.verify_candidate_review_decision(
        receipt,
        report=report,
        now=NOW,
    )
    projected = decision.project_candidate_review_decision(report, verified)

    assert verified["status"] == "verified"
    assert verified["decision"] == "confirm_identity_verified"
    assert verified["identity_verification_asserted"] is True
    assert verified["trust_enrollment_preview_authorized"] is True
    assert verified["trust_enrollment_authorized"] is False
    assert verified["decision_confers_trust"] is False
    assert verified["trust_registry_modified"] is False
    assert verified["provider_quota_consumption_allowed"] is False
    assert verified["delivery_authorized"] is False
    assert verified["protected_operation_executed"] is False
    assert receipt["decision"]["acknowledged_public_key_sha256s"] == [
        "6" * 64
    ]
    assert projected["intake_state"] == (
        "candidate_identity_verified_preview_required"
    )
    assert projected["action_required"] is False
    assert projected["interrupt_operator"] is False
    assert projected["trust_enrollment_preview_authorized"] is True
    assert projected["trust_enrollment_authorized"] is False


@pytest.mark.parametrize(
    ("value", "state"),
    [
        ("reject_candidate", "candidate_rejected"),
        ("defer", "deferred"),
    ],
)
def test_reject_and_defer_resolve_review_without_authority(
    value: str,
    state: str,
) -> None:
    report = _report()
    receipt = decision.build_candidate_review_decision(
        report,
        decision=value,
        decider_id="operator.local",
        now=NOW,
    )

    verified = decision.verify_candidate_review_decision(
        receipt,
        report=report,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["review_state"] == state
    assert verified["identity_verification_asserted"] is False
    assert verified["trust_enrollment_preview_authorized"] is False
    assert verified["trust_enrollment_authorized"] is False
    assert verified["trust_registry_modified"] is False


def test_confirmation_requires_exact_fingerprint_and_identity_evidence() -> None:
    report = _report()

    with pytest.raises(
        ValueError,
        match="trust_candidate_decision_input_not_admissible",
    ):
        decision.build_candidate_review_decision(
            report,
            decision="confirm_identity_verified",
            decider_id="operator.local",
            acknowledged_public_key_sha256s=["f" * 64],
            identity_verification_method="trusted_channel",
            identity_evidence_ref="ticket:IDENTITY-123",
            now=NOW,
        )
    with pytest.raises(
        ValueError,
        match="trust_candidate_decision_input_not_admissible",
    ):
        decision.build_candidate_review_decision(
            report,
            decision="confirm_identity_verified",
            decider_id="operator.local",
            acknowledged_public_key_sha256s=["6" * 64],
            now=NOW,
        )


def test_decision_tamper_fails_even_with_recomputed_integrity() -> None:
    report = _report()
    receipt = _confirm_receipt(report)
    receipt["trust_enrollment_authorized"] = True

    integrity_failure = decision.verify_candidate_review_decision(
        receipt,
        report=report,
        now=NOW,
    )
    assert integrity_failure["blocking_reason"] == (
        "trust_candidate_decision_integrity_invalid"
    )

    normalized = dict(receipt)
    normalized.pop("integrity")
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": decision._sha256(
            decision._canonical(normalized)
        ),
    }
    contract_failure = decision.verify_candidate_review_decision(
        receipt,
        report=report,
        now=NOW,
    )
    assert contract_failure["blocking_reason"] == (
        "trust_candidate_decision_contract_not_admissible"
    )
    assert contract_failure["trust_enrollment_authorized"] is False


def test_expired_decision_fails_closed() -> None:
    report = _report(expires_at=NOW + timedelta(minutes=1))
    receipt = _confirm_receipt(report)

    expired = decision.verify_candidate_review_decision(
        receipt,
        report=report,
        now=NOW + timedelta(minutes=2),
    )

    assert expired["status"] == "blocked"
    assert expired["blocking_reason"] == "trust_candidate_decision_not_fresh"
    assert expired["trust_enrollment_preview_authorized"] is False
    assert expired["trust_enrollment_authorized"] is False


def test_private_decision_is_immutable_and_idempotent(
    tmp_path: Path,
) -> None:
    report = _report()
    decision_dir = tmp_path / "decisions"
    kwargs = {
        "decision": "confirm_identity_verified",
        "decider_id": "operator.local",
        "expected_candidate_review_id": report["candidate_review_id"],
        "expected_trust_intake_verification_sha256": report[
            "verification_receipt_sha256"
        ],
        "acknowledged_public_key_sha256s": ["6" * 64],
        "identity_verification_method": "trusted_channel",
        "identity_evidence_ref": "ticket:IDENTITY-123",
        "decision_dir": decision_dir,
    }

    receipt, path = decision.materialize_candidate_review_decision_for_report(
        report,
        now=NOW,
        **kwargs,
    )
    verified = decision.verify_candidate_review_decision_for_report(
        report,
        decision_dir=decision_dir,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["progress"]["current_evidence_verified"] is True
    assert verified["decision_sha256"] == decision._sha256(
        decision._canonical(receipt)
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == receipt

    retried, retried_path = (
        decision.materialize_candidate_review_decision_for_report(
            report,
            now=NOW + timedelta(seconds=1),
            **kwargs,
        )
    )
    assert retried == receipt
    assert retried_path == path

    with pytest.raises(
        ValueError,
        match="trust_candidate_decision_already_recorded",
    ):
        decision.materialize_candidate_review_decision_for_report(
            report,
            decision="reject_candidate",
            decider_id="operator.local",
            expected_candidate_review_id=str(report["candidate_review_id"]),
            expected_trust_intake_verification_sha256=str(
                report["verification_receipt_sha256"]
            ),
            decision_dir=decision_dir,
            now=NOW,
        )


def test_no_candidate_requires_no_decision(tmp_path: Path) -> None:
    report = {
        "schema": decision.trust_intake.VERIFY_SCHEMA,
        "status": "verified",
        "candidates": [],
        "action_required": False,
        "public_key_candidate_recorded": False,
    }

    result = decision.verify_candidate_review_decision_for_report(
        report,
        decision_dir=tmp_path / "decisions",
        now=NOW,
    )

    assert result["status"] == "not_required"
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["trust_enrollment_preview_authorized"] is False
