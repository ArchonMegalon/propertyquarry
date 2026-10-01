from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_authorization as authorization


NOW = datetime(2026, 8, 27, 0, 45, tzinfo=timezone.utc)


def _preview() -> dict[str, object]:
    preview_id = "pqtrustpreview_" + "1" * 24
    current_digest = "2" * 64
    proposed_digest = "3" * 64
    return {
        "schema": authorization.preview.VERIFY_SCHEMA,
        "status": "verified",
        "preview_state": "preview_staged",
        "preview_id": preview_id,
        "preview_receipt_sha256": "4" * 64,
        "verification_receipt_sha256": "5" * 64,
        "current_trust_registry_sha256": current_digest,
        "proposed_trust_registry_sha256": proposed_digest,
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        "next_action": "review and separately authorize or reject exact preview",
        "authorization_scope": {
            "operation": "replace_trust_registry_with_exact_preview",
            "preview_id": preview_id,
            "expected_current_trust_registry_sha256": current_digest,
            "proposed_trust_registry_sha256": proposed_digest,
            "decision_options": list(authorization.DECISIONS),
            "authorization_recorded": False,
        },
        "progress": {
            "current_evidence_verified": True,
            "intake_binding_verified": True,
            "decision_binding_verified": True,
            "trust_registry_binding_verified": True,
            "preview_integrity_verified": True,
        },
        "preview_receipt_persisted": True,
        "verification_receipt_persisted": True,
        "action_required": True,
        "interrupt_operator": True,
        "operator_review_required": True,
        "preview_staged": True,
        "identity_verification_asserted": True,
        "trust_enrollment_preview_authorized": True,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "automatic_execution_allowed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _approval_receipt(report: dict[str, object]) -> dict[str, object]:
    return authorization.build_enrollment_authorization(
        report,
        decision="authorize_exact_preview",
        authorizer_id="operator.local",
        expected_current_trust_registry_sha256=str(
            report["current_trust_registry_sha256"]
        ),
        expected_proposed_trust_registry_sha256=str(
            report["proposed_trust_registry_sha256"]
        ),
        authorization_method="authenticated_operator_session",
        authorization_evidence_ref="ticket:TRUST-456",
        now=NOW,
    )


def test_exact_preview_is_pending_without_immutable_authorization(
    tmp_path: Path,
) -> None:
    report = _preview()

    result = authorization.verify_authorization_for_preview(
        report,
        decision_dir=tmp_path / "authorizations",
        now=NOW,
    )

    assert result["status"] == "pending"
    assert result["authorization_state"] == (
        "exact_preview_authorization_pending"
    )
    assert result["action_required"] is True
    assert result["interrupt_operator"] is False
    assert result["decision_options"] == list(authorization.DECISIONS)
    assert result["exact_preview_authorized"] is False
    assert result["trust_enrollment_authorized"] is False
    assert result["trust_registry_modified"] is False


def test_exact_approval_records_scope_but_performs_no_registry_write() -> None:
    report = _preview()
    receipt = _approval_receipt(report)

    verified = authorization.verify_enrollment_authorization(
        receipt,
        preview_report=report,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["authorization_state"] == "exact_preview_authorized"
    assert verified["decision"] == "authorize_exact_preview"
    assert verified["explicit_authorization_recorded"] is True
    assert verified["exact_preview_authorized"] is True
    assert verified["trust_enrollment_authorized"] is True
    assert verified["trust_registry_modified"] is False
    assert verified["decision_confers_trust"] is False
    assert verified["automatic_execution_allowed"] is False
    assert verified["execution_authorized"] is False
    assert verified["protected_operation_executed"] is False
    assert verified["provider_quota_consumption_allowed"] is False
    assert verified["delivery_authorized"] is False
    assert receipt["decision"][
        "acknowledged_current_trust_registry_sha256"
    ] == report["current_trust_registry_sha256"]
    assert receipt["decision"][
        "acknowledged_proposed_trust_registry_sha256"
    ] == report["proposed_trust_registry_sha256"]


@pytest.mark.parametrize(
    ("value", "state"),
    [
        ("reject", "exact_preview_rejected"),
        ("defer", "deferred"),
    ],
)
def test_negative_decisions_resolve_without_enrollment_authority(
    value: str,
    state: str,
) -> None:
    report = _preview()
    receipt = authorization.build_enrollment_authorization(
        report,
        decision=value,
        authorizer_id="operator.local",
        expected_current_trust_registry_sha256=str(
            report["current_trust_registry_sha256"]
        ),
        expected_proposed_trust_registry_sha256=str(
            report["proposed_trust_registry_sha256"]
        ),
        now=NOW,
    )

    verified = authorization.verify_enrollment_authorization(
        receipt,
        preview_report=report,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["authorization_state"] == state
    assert verified["exact_preview_authorized"] is False
    assert verified["trust_enrollment_authorized"] is False
    assert verified["trust_registry_modified"] is False


def test_approval_requires_exact_digests_and_evidence() -> None:
    report = _preview()

    with pytest.raises(
        ValueError,
        match="trust_enrollment_authorization_input_not_admissible",
    ):
        authorization.build_enrollment_authorization(
            report,
            decision="authorize_exact_preview",
            authorizer_id="operator.local",
            expected_current_trust_registry_sha256="f" * 64,
            expected_proposed_trust_registry_sha256=str(
                report["proposed_trust_registry_sha256"]
            ),
            authorization_method="authenticated_operator_session",
            authorization_evidence_ref="ticket:TRUST-456",
            now=NOW,
        )
    with pytest.raises(
        ValueError,
        match="trust_enrollment_authorization_input_not_admissible",
    ):
        authorization.build_enrollment_authorization(
            report,
            decision="authorize_exact_preview",
            authorizer_id="operator.local",
            expected_current_trust_registry_sha256=str(
                report["current_trust_registry_sha256"]
            ),
            expected_proposed_trust_registry_sha256=str(
                report["proposed_trust_registry_sha256"]
            ),
            now=NOW,
        )


def test_authorization_tamper_and_staleness_fail_closed() -> None:
    report = _preview()
    receipt = _approval_receipt(report)
    receipt["trust_registry_modified"] = True

    integrity_failure = authorization.verify_enrollment_authorization(
        receipt,
        preview_report=report,
        now=NOW,
    )
    assert integrity_failure["status"] == "blocked"
    assert integrity_failure["trust_registry_modified"] is False

    expired_report = _preview()
    expired_report["expires_at"] = (NOW + timedelta(seconds=1)).isoformat()
    expired_receipt = _approval_receipt(expired_report)
    stale = authorization.verify_enrollment_authorization(
        expired_receipt,
        preview_report=expired_report,
        now=NOW + timedelta(seconds=2),
    )
    assert stale["status"] == "blocked"
    assert stale["blocking_reason"] == (
        "trust_enrollment_authorization_not_fresh"
    )


def test_authorization_receipt_is_private_immutable_and_idempotent(
    tmp_path: Path,
) -> None:
    report = _preview()
    decision_dir = tmp_path / "authorizations"
    kwargs = {
        "decision": "authorize_exact_preview",
        "authorizer_id": "operator.local",
        "expected_preview_id": report["preview_id"],
        "expected_preview_verification_sha256": report[
            "verification_receipt_sha256"
        ],
        "expected_current_trust_registry_sha256": report[
            "current_trust_registry_sha256"
        ],
        "expected_proposed_trust_registry_sha256": report[
            "proposed_trust_registry_sha256"
        ],
        "authorization_method": "authenticated_operator_session",
        "authorization_evidence_ref": "ticket:TRUST-456",
        "decision_dir": decision_dir,
    }

    receipt, path = authorization.materialize_enrollment_authorization(
        report,
        now=NOW,
        **kwargs,
    )
    verified = authorization.verify_authorization_for_preview(
        report,
        decision_dir=decision_dir,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["authorization_receipt_sha256"] == authorization._sha256(
        authorization._canonical(receipt)
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == receipt

    retried, retried_path = authorization.materialize_enrollment_authorization(
        report,
        now=NOW + timedelta(seconds=1),
        **kwargs,
    )
    assert retried == receipt
    assert retried_path == path

    with pytest.raises(
        ValueError,
        match="trust_enrollment_authorization_already_recorded",
    ):
        authorization.materialize_enrollment_authorization(
            report,
            decision="reject",
            authorizer_id="operator.local",
            expected_preview_id=str(report["preview_id"]),
            expected_preview_verification_sha256=str(
                report["verification_receipt_sha256"]
            ),
            expected_current_trust_registry_sha256=str(
                report["current_trust_registry_sha256"]
            ),
            expected_proposed_trust_registry_sha256=str(
                report["proposed_trust_registry_sha256"]
            ),
            decision_dir=decision_dir,
            now=NOW,
        )


def test_non_staged_preview_requires_no_authorization() -> None:
    report = {
        **_preview(),
        "preview_state": "not_required",
        "preview_id": "",
        "proposed_trust_registry_sha256": "",
        "authorization_scope": {},
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "preview_staged": False,
        "identity_verification_asserted": False,
        "trust_enrollment_preview_authorized": False,
    }

    result = authorization.verify_authorization_for_preview(
        report,
        now=NOW,
    )

    assert result["status"] == "not_required"
    assert result["trust_enrollment_authorized"] is False
    assert result["trust_registry_modified"] is False
