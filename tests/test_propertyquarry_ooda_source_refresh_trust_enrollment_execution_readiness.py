from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness as readiness


NOW = datetime(2026, 8, 27, 2, 0, tzinfo=timezone.utc)


def _preview(*, state: str = "preview_staged") -> dict[str, object]:
    staged = state == "preview_staged"
    preview_id = "pqtrustpreview_" + "1" * 24 if staged else ""
    proposed_digest = "3" * 64 if staged else ""
    return {
        "schema": readiness.preview.VERIFY_SCHEMA,
        "status": "verified",
        "preview_state": state,
        "preview_id": preview_id,
        "preview_receipt_sha256": "4" * 64,
        "verification_receipt_sha256": "5" * 64,
        "current_trust_registry_sha256": "2" * 64,
        "proposed_trust_registry_sha256": proposed_digest,
        "generated_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        "next_action": "review exact preview" if staged else "await evidence",
        "authorization_scope": {
            "operation": "replace_trust_registry_with_exact_preview",
            "preview_id": preview_id,
            "expected_current_trust_registry_sha256": "2" * 64,
            "proposed_trust_registry_sha256": proposed_digest,
            "decision_options": list(readiness.authorization.DECISIONS),
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
        "action_required": staged,
        "interrupt_operator": staged,
        "operator_review_required": staged,
        "preview_staged": staged,
        "identity_verification_asserted": staged,
        "trust_enrollment_preview_authorized": staged,
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


def _authorization(
    report: dict[str, object],
    *,
    decision: str = "authorize_exact_preview",
) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if decision == "authorize_exact_preview":
        kwargs = {
            "authorization_method": "authenticated_operator_session",
            "authorization_evidence_ref": "ticket:TRUST-789",
        }
    receipt = readiness.authorization.build_enrollment_authorization(
        report,
        decision=decision,
        authorizer_id="operator.local",
        expected_current_trust_registry_sha256=str(
            report["current_trust_registry_sha256"]
        ),
        expected_proposed_trust_registry_sha256=str(
            report["proposed_trust_registry_sha256"]
        ),
        now=NOW,
        **kwargs,
    )
    verified = readiness.authorization.verify_enrollment_authorization(
        receipt,
        preview_report=report,
        now=NOW,
    )
    verified["authorization_receipt_sha256"] = readiness._sha256(
        readiness._canonical(receipt)
    )
    return verified


def test_pending_authorization_stages_no_execution_request(tmp_path: Path) -> None:
    report = _preview()
    authorization = readiness.authorization.verify_authorization_for_preview(
        report,
        decision_dir=tmp_path / "authorizations",
        now=NOW,
    )

    receipt = readiness.build_execution_readiness(
        report,
        authorization,
        now=NOW,
    )
    verified = readiness.verify_execution_readiness(
        receipt,
        preview_report=report,
        authorization_report=authorization,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["readiness_state"] == (
        "awaiting_exact_preview_authorization"
    )
    assert verified["execution_request_staged"] is False
    assert verified["execution_readiness_verified"] is False
    assert verified["action_required"] is False
    assert verified["interrupt_operator"] is False
    assert verified["trust_registry_modified"] is False


def test_approved_preview_stages_exact_request_without_execution() -> None:
    report = _preview()
    authorization = _authorization(report)

    receipt = readiness.build_execution_readiness(
        report,
        authorization,
        now=NOW,
    )
    verified = readiness.verify_execution_readiness(
        receipt,
        preview_report=report,
        authorization_report=authorization,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["readiness_state"] == "ready_for_governed_execution"
    assert verified["authorization_id"] == authorization["authorization_id"]
    assert verified["authorization_receipt_sha256"] == (
        authorization["authorization_receipt_sha256"]
    )
    assert verified["current_trust_registry_sha256"] == (
        report["current_trust_registry_sha256"]
    )
    assert verified["proposed_trust_registry_sha256"] == (
        report["proposed_trust_registry_sha256"]
    )
    assert verified["explicit_authorization_recorded"] is True
    assert verified["exact_preview_authorized"] is True
    assert verified["trust_enrollment_authorized"] is True
    assert verified["execution_request_staged"] is True
    assert verified["execution_readiness_verified"] is True
    assert verified["governed_execution_available"] is True
    assert verified["action_required"] is True
    assert verified["operator_review_required"] is True
    assert verified["interrupt_operator"] is False
    assert verified["automatic_execution_allowed"] is False
    assert verified["execution_authorized"] is False
    assert verified["trust_registry_modified"] is False
    assert verified["protected_operation_executed"] is False
    assert verified["provider_quota_consumption_allowed"] is False
    assert verified["delivery_authorized"] is False


@pytest.mark.parametrize(
    ("decision", "state"),
    (("reject", "authorization_rejected"), ("defer", "deferred")),
)
def test_negative_authorization_resolves_without_execution_request(
    decision: str,
    state: str,
) -> None:
    report = _preview()
    authorization = _authorization(report, decision=decision)

    receipt = readiness.build_execution_readiness(
        report,
        authorization,
        now=NOW,
    )
    verified = readiness.verify_execution_readiness(
        receipt,
        preview_report=report,
        authorization_report=authorization,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["readiness_state"] == state
    assert verified["explicit_authorization_recorded"] is True
    assert verified["execution_request_staged"] is False
    assert verified["execution_readiness_verified"] is False
    assert verified["execution_authorized"] is False
    assert verified["trust_registry_modified"] is False


def test_not_required_state_is_verified_without_authority(tmp_path: Path) -> None:
    report = _preview(state="not_required")
    authorization = readiness.authorization.verify_authorization_for_preview(
        report,
        decision_dir=tmp_path / "authorizations",
        now=NOW,
    )

    receipt = readiness.build_execution_readiness(
        report,
        authorization,
        now=NOW,
    )
    verified = readiness.verify_execution_readiness(
        receipt,
        preview_report=report,
        authorization_report=authorization,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["readiness_state"] == "not_required"
    assert verified["explicit_authorization_recorded"] is False
    assert verified["execution_request_staged"] is False
    assert verified["trust_enrollment_authorized"] is False


def test_authorization_or_registry_drift_fails_closed() -> None:
    report = _preview()
    authorization = _authorization(report)
    receipt = readiness.build_execution_readiness(
        report,
        authorization,
        now=NOW,
    )
    authorization["current_trust_registry_sha256"] = "9" * 64

    verified = readiness.verify_execution_readiness(
        receipt,
        preview_report=report,
        authorization_report=authorization,
        now=NOW,
    )

    assert verified["status"] == "blocked"
    assert verified["execution_request_staged"] is False
    assert verified["trust_registry_modified"] is False


def test_pending_authorization_binding_drift_fails_closed(tmp_path: Path) -> None:
    report = _preview()
    authorization = readiness.authorization.verify_authorization_for_preview(
        report,
        decision_dir=tmp_path / "authorizations",
        now=NOW,
    )
    authorization["proposed_trust_registry_sha256"] = "9" * 64

    with pytest.raises(
        ValueError,
        match="execution_readiness_authorization_not_admissible",
    ):
        readiness.build_execution_readiness(
            report,
            authorization,
            now=NOW,
        )


def test_tamper_and_staleness_fail_closed() -> None:
    report = _preview()
    authorization = _authorization(report)
    tampered = readiness.build_execution_readiness(
        report,
        authorization,
        now=NOW,
    )
    tampered["trust_registry_modified"] = True

    assert readiness.verify_execution_readiness(
        tampered,
        preview_report=report,
        authorization_report=authorization,
        now=NOW,
    )["status"] == "blocked"

    fresh = readiness.build_execution_readiness(
        report,
        authorization,
        now=NOW,
        max_age_seconds=60,
    )
    assert readiness.verify_execution_readiness(
        fresh,
        preview_report=report,
        authorization_report=authorization,
        now=NOW + timedelta(seconds=61),
        max_age_seconds=60,
    )["status"] == "blocked"


def test_materialize_and_inspect_rebind_current_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _preview()
    authorization = _authorization(report)
    receipt_path = tmp_path / "readiness.json"
    verification_path = tmp_path / "readiness-verification.json"

    materialized = readiness.materialize_execution_readiness(
        report,
        authorization,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert materialized["status"] == "verified"
    assert materialized["readiness_state"] == "ready_for_governed_execution"
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == (
        "ready_for_governed_execution"
    )

    monkeypatch.setattr(
        readiness.preview,
        "inspect_current_enrollment_preview",
        lambda **_kwargs: report,
    )
    monkeypatch.setattr(
        readiness.authorization,
        "verify_authorization_for_preview",
        lambda *_args, **_kwargs: authorization,
    )
    inspected = readiness.inspect_current_execution_readiness(
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert inspected["status"] == "verified"
    assert inspected["readiness_receipt_persisted"] is True
    assert inspected["verification_receipt_persisted"] is True

    drifted = dict(authorization)
    drifted["authorization_receipt_sha256"] = "8" * 64
    monkeypatch.setattr(
        readiness.authorization,
        "verify_authorization_for_preview",
        lambda *_args, **_kwargs: drifted,
    )
    blocked = readiness.inspect_current_execution_readiness(
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )
    assert blocked["status"] == "blocked"
    assert blocked["trust_registry_modified"] is False
