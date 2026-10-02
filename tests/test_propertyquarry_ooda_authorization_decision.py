from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_authorization_decision as decision


NOW = datetime(2026, 8, 26, 9, 10, tzinfo=timezone.utc)


def _request() -> dict[str, object]:
    return {
        "schema": "propertyquarry.ooda_runtime_authorization_request.v1",
        "request_id": "pqar_0123456789abcdef01234567",
        "generated_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        "binding": {
            "review_packet_sha256": "a" * 64,
            "proposal_sha256": "b" * 64,
            "action_sha256": "c" * 64,
        },
        "scope": {
            "operation": "runtime_configuration_change",
            "change_id": "live_probe_origin",
            "current_value": "http://127.0.0.1:8090",
            "proposed_value": "http://127.0.0.1:8097",
            "public_host": "propertyquarry.com",
            "public_origin": "https://propertyquarry.com",
            "compose_project": "property",
        },
        "excluded_operations": [
            "deployment_or_restart",
            "provider_account_change",
            "provider_quota_consumption",
            "external_delivery",
        ],
    }


def _request_verification() -> dict[str, object]:
    return {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "authorization_recorded": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


@pytest.fixture(autouse=True)
def allow_synthetic_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        decision.authorization_request,
        "verify_authorization_request",
        lambda *_args, **_kwargs: {"status": "verified"},
    )


def _receipt(
    value: str = "approve_exact_scope",
    *,
    now: datetime = NOW,
) -> tuple[dict[str, object], dict[str, object]]:
    request = _request()
    receipt = decision.build_authorization_decision(
        request=request,
        request_sha256=decision._sha256(decision._canonical(request)),
        review_packet={},
        decision=value,
        decider_id="operator.local",
        now=now,
    )
    return request, receipt


def test_approve_receipt_authorizes_only_exact_scope_and_executes_nothing() -> None:
    request, receipt = _receipt()

    verification = decision.verify_authorization_decision(
        receipt,
        request=request,
        review_packet={},
        now=NOW,
    )

    assert verification["status"] == "verified"
    assert verification["decision"] == "approve_exact_scope"
    assert verification["exact_scope_authorized"] is True
    assert receipt["binding"]["request_sha256"] == decision._sha256(
        decision._canonical(request)
    )
    assert receipt["scope"] == request["scope"]
    assert receipt["authorization_recorded"] is True
    assert receipt["automatic_execution_allowed"] is False
    assert receipt["execution_authorized"] is False
    assert receipt["execution_performed"] is False
    assert receipt["deployment_or_restart_authorized"] is False
    assert receipt["provider_quota_consumption_allowed"] is False
    assert receipt["delivery_authorized"] is False
    assert receipt["decision"] == {
        "value": "approve_exact_scope",
        "decider_id": "operator.local",
        "explicit_input_recorded": True,
        "source": "local_operator_cli",
        "cryptographic_identity_verified": False,
    }


@pytest.mark.parametrize("value", ["reject", "defer"])
def test_reject_and_defer_never_authorize_scope(value: str) -> None:
    request, receipt = _receipt(value)

    verification = decision.verify_authorization_decision(
        receipt,
        request=request,
        review_packet={},
        now=NOW,
    )

    assert verification["status"] == "verified"
    assert verification["decision"] == value
    assert verification["authorization_recorded"] is True
    assert verification["exact_scope_authorized"] is False
    assert verification["execution_authorized"] is False


def test_decision_tamper_fails_even_when_integrity_is_recomputed() -> None:
    request, receipt = _receipt()
    receipt["deployment_or_restart_authorized"] = True

    integrity_failure = decision.verify_authorization_decision(
        receipt,
        request=request,
        review_packet={},
        now=NOW,
    )
    assert integrity_failure["blocking_reason"] == "authorization_decision_integrity_invalid"

    normalized = dict(receipt)
    normalized.pop("integrity")
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": decision._sha256(decision._canonical(normalized)),
    }
    contract_failure = decision.verify_authorization_decision(
        receipt,
        request=request,
        review_packet={},
        now=NOW,
    )
    assert contract_failure["blocking_reason"] == "authorization_decision_contract_not_admissible"
    assert contract_failure["exact_scope_authorized"] is False


def test_expired_decision_cannot_authorize_scope() -> None:
    request, receipt = _receipt()

    expired = decision.verify_authorization_decision(
        receipt,
        request=request,
        review_packet={},
        now=NOW + timedelta(minutes=16),
    )

    assert expired["status"] == "blocked"
    assert expired["blocking_reason"] == "authorization_decision_not_fresh"
    assert expired["authorization_recorded"] is False
    assert expired["exact_scope_authorized"] is False
    assert expired["execution_authorized"] is False


def test_current_verifier_is_pending_until_private_immutable_decision_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    request_sha256 = decision._sha256(decision._canonical(request))
    monkeypatch.setattr(
        decision,
        "_current_inputs",
        lambda **_kwargs: (request, request_sha256, {}),
    )
    decision_dir = tmp_path / "decisions"

    pending = decision.verify_current_authorization_decision(
        decision_dir=decision_dir,
        now=NOW,
    )
    assert pending["status"] == "pending"
    assert pending["authorization_recorded"] is False
    assert pending["exact_scope_authorized"] is False

    receipt, decision_path = decision.materialize_current_authorization_decision(
        decision="approve_exact_scope",
        decider_id="operator.local",
        expected_request_id=str(request["request_id"]),
        expected_request_sha256=request_sha256,
        decision_dir=decision_dir,
        now=NOW,
    )
    verified = decision.verify_current_authorization_decision(
        decision_dir=decision_dir,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["progress"]["current_evidence_verified"] is True
    assert verified["exact_scope_authorized"] is True
    assert verified["decision_sha256"] == decision._sha256(
        decision._canonical(receipt)
    )
    assert decision_path.name == (
        f"{request['request_id']}.{request_sha256}.json"
    )
    assert stat.S_IMODE(decision_path.stat().st_mode) == 0o600
    assert json.loads(decision_path.read_text()) == receipt

    retried, retried_path = decision.materialize_current_authorization_decision(
        decision="approve_exact_scope",
        decider_id="operator.local",
        expected_request_id=str(request["request_id"]),
        expected_request_sha256=request_sha256,
        decision_dir=decision_dir,
        now=NOW + timedelta(seconds=1),
    )
    assert retried == receipt
    assert retried_path == decision_path

    with pytest.raises(ValueError, match="authorization_decision_already_recorded"):
        decision.materialize_current_authorization_decision(
            decision="reject",
            decider_id="operator.local",
            expected_request_id=str(request["request_id"]),
            expected_request_sha256=request_sha256,
            decision_dir=decision_dir,
            now=NOW,
        )


def test_refreshed_exact_request_can_record_a_new_immutable_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_request = _request()
    original_sha256 = decision._sha256(decision._canonical(original_request))
    current = {
        "request": original_request,
        "sha256": original_sha256,
    }
    monkeypatch.setattr(
        decision,
        "_current_inputs",
        lambda **_kwargs: (current["request"], current["sha256"], {}),
    )
    decision_dir = tmp_path / "decisions"

    original_receipt, original_path = (
        decision.materialize_current_authorization_decision(
            decision="reject",
            decider_id="operator.local",
            expected_request_id=str(original_request["request_id"]),
            expected_request_sha256=original_sha256,
            decision_dir=decision_dir,
            now=NOW,
        )
    )
    original_bytes = original_path.read_bytes()

    refreshed_request = dict(original_request)
    refreshed_request["generated_at"] = (NOW + timedelta(minutes=10)).isoformat()
    refreshed_request["expires_at"] = (NOW + timedelta(minutes=25)).isoformat()
    refreshed_sha256 = decision._sha256(decision._canonical(refreshed_request))
    current["request"] = refreshed_request
    current["sha256"] = refreshed_sha256

    pending = decision.verify_current_authorization_decision(
        decision_dir=decision_dir,
        now=NOW + timedelta(minutes=10),
    )
    refreshed_receipt, refreshed_path = (
        decision.materialize_current_authorization_decision(
            decision="defer",
            decider_id="operator.local",
            expected_request_id=str(refreshed_request["request_id"]),
            expected_request_sha256=refreshed_sha256,
            decision_dir=decision_dir,
            now=NOW + timedelta(minutes=10),
        )
    )

    assert pending["status"] == "pending"
    assert pending["request_id"] == original_request["request_id"]
    assert pending["request_sha256"] == refreshed_sha256
    assert pending["decision_path"] == str(refreshed_path)
    assert refreshed_path != original_path
    assert refreshed_path.name == (
        f"{refreshed_request['request_id']}.{refreshed_sha256}.json"
    )
    assert original_path.read_bytes() == original_bytes
    assert json.loads(original_path.read_text()) == original_receipt
    assert json.loads(refreshed_path.read_text()) == refreshed_receipt
    assert refreshed_receipt["decision"]["value"] == "defer"


def test_materializer_requires_exact_operator_supplied_request_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    request_sha256 = decision._sha256(decision._canonical(request))
    monkeypatch.setattr(
        decision,
        "_current_inputs",
        lambda **_kwargs: (request, request_sha256, {}),
    )

    with pytest.raises(ValueError, match="operator_request_binding_mismatch"):
        decision.materialize_current_authorization_decision(
            decision="approve_exact_scope",
            decider_id="operator.local",
            expected_request_id=str(request["request_id"]),
            expected_request_sha256="f" * 64,
            now=NOW,
        )


def test_cli_failure_receipt_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    verification_path = tmp_path / "verification.json"
    monkeypatch.setattr(
        decision,
        "materialize_current_authorization_decision",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("must-not-be-recorded")),
    )

    status = decision.main(
        [
            "--decision",
            "approve_exact_scope",
            "--decider-id",
            "operator.local",
            "--request-id",
            "pqar_0123456789abcdef01234567",
            "--request-sha256",
            "a" * 64,
            "--verification-write",
            str(verification_path),
        ]
    )
    receipt = json.loads(verification_path.read_text())

    assert status == 1
    assert receipt["blocking_reason"] == "authorization_decision_materialization_failed"
    assert receipt["authorization_recorded"] is False
    assert receipt["exact_scope_authorized"] is False
    assert receipt["execution_authorized"] is False
    assert receipt["verification_receipt_persisted"] is True
    assert "must-not-be-recorded" not in json.dumps(receipt)
    capsys.readouterr()
