from __future__ import annotations

import base64
import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import (
    propertyquarry_ooda_source_refresh_trust_enrollment_execution as execution,
)


NOW = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)
READINESS_ID = "pqtrustready_" + "1" * 24
PREVIEW_ID = "pqtrustpreview_" + "2" * 24
AUTHORIZATION_ID = "pqtrustauth_" + "3" * 24


def _registry(*, active: bool) -> dict[str, object]:
    if not active:
        return execution.preview._with_integrity(
            {
                "schema": execution.claims.TRUST_SCHEMA,
                "status": "UNCONFIGURED",
                "rotation_epoch": 0,
                "producers": [],
            }
        )
    public_key_bytes = bytes(range(32))
    public_key = base64.urlsafe_b64encode(public_key_bytes).decode().rstrip("=")
    return execution.preview._with_integrity(
        {
            "schema": execution.claims.TRUST_SCHEMA,
            "status": "ACTIVE",
            "rotation_epoch": 1,
            "producers": [
                {
                    "producer_id": "producer.test",
                    "key_id": "key-2026-08",
                    "algorithm": "Ed25519",
                    "public_key": public_key,
                    "public_key_sha256": hashlib.sha256(
                        public_key_bytes
                    ).hexdigest(),
                    "lanes": ["gold_live_runtime"],
                    "status": "ACTIVE",
                }
            ],
        }
    )


def _ready(
    *,
    current_sha256: str,
    proposed_sha256: str,
) -> dict[str, object]:
    return {
        "schema": execution.readiness.VERIFY_SCHEMA,
        "status": "verified",
        "readiness_state": "ready_for_governed_execution",
        "readiness_id": READINESS_ID,
        "verification_receipt_sha256": "4" * 64,
        "preview_id": PREVIEW_ID,
        "authorization_id": AUTHORIZATION_ID,
        "authorization_receipt_sha256": "5" * 64,
        "authorization_decision": "authorize_exact_preview",
        "current_trust_registry_sha256": current_sha256,
        "proposed_trust_registry_sha256": proposed_sha256,
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        "action_required": True,
        "interrupt_operator": False,
        "operator_review_required": True,
        "explicit_authorization_recorded": True,
        "exact_preview_authorized": True,
        "trust_enrollment_authorized": True,
        "execution_request_staged": True,
        "execution_readiness_verified": True,
        "governed_execution_available": True,
        "trust_registry_modified": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "progress": {
            "current_evidence_verified": True,
            "authorization_binding_verified": True,
            "registry_binding_verified": True,
        },
    }


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    current = _registry(active=False)
    proposed = _registry(active=True)
    current_raw = execution._canonical(current)
    proposed_raw = execution._canonical(proposed)
    registry_path = tmp_path / "config" / "trust.json"
    registry_path.parent.mkdir(mode=0o700)
    registry_path.write_bytes(current_raw)
    current_sha256 = execution._sha256(current_raw)
    proposed_sha256 = execution._sha256(proposed_raw)
    ready = _ready(
        current_sha256=current_sha256,
        proposed_sha256=proposed_sha256,
    )
    preview_report = {
        "schema": execution.preview.VERIFY_SCHEMA,
        "status": "verified",
        "preview_state": "preview_staged",
        "preview_id": PREVIEW_ID,
        "preview_receipt_sha256": "6" * 64,
        "verification_receipt_sha256": "7" * 64,
        "current_trust_registry_sha256": current_sha256,
        "proposed_trust_registry_sha256": proposed_sha256,
        "proposed_registry": proposed,
        "action_required": True,
        "interrupt_operator": True,
        "operator_review_required": True,
        "preview_staged": True,
        "identity_verification_asserted": True,
        "trust_enrollment_preview_authorized": True,
        "trust_enrollment_authorized": False,
        "decision_confers_trust": False,
        "trust_registry_modified": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "automatic_execution_allowed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "preview_receipt_persisted": True,
        "verification_receipt_persisted": True,
        "authorization_scope": {
            "operation": "replace_trust_registry_with_exact_preview",
            "preview_id": PREVIEW_ID,
            "expected_current_trust_registry_sha256": current_sha256,
            "proposed_trust_registry_sha256": proposed_sha256,
            "decision_options": list(execution.authorization.DECISIONS),
            "authorization_recorded": False,
        },
        "progress": {
            "current_evidence_verified": True,
            "intake_binding_verified": True,
            "decision_binding_verified": True,
            "trust_registry_binding_verified": True,
            "preview_integrity_verified": True,
        },
    }
    monkeypatch.setattr(
        execution.readiness,
        "inspect_current_execution_readiness",
        lambda **_kwargs: ready,
    )
    monkeypatch.setattr(
        execution.preview,
        "inspect_current_enrollment_preview",
        lambda **_kwargs: preview_report,
    )
    return {
        "current_raw": current_raw,
        "proposed_raw": proposed_raw,
        "registry_path": registry_path,
        "ready": ready,
        "execution_dir": tmp_path / "executions",
        "backup_dir": tmp_path / "backups",
    }


def _execute(values: dict[str, object], **overrides: object) -> dict[str, object]:
    ready = dict(values["ready"])
    arguments: dict[str, object] = {
        "expected_readiness_id": READINESS_ID,
        "expected_readiness_verification_sha256": ready[
            "verification_receipt_sha256"
        ],
        "expected_authorization_id": AUTHORIZATION_ID,
        "expected_authorization_receipt_sha256": ready[
            "authorization_receipt_sha256"
        ],
        "expected_current_trust_registry_sha256": ready[
            "current_trust_registry_sha256"
        ],
        "expected_proposed_trust_registry_sha256": ready[
            "proposed_trust_registry_sha256"
        ],
        "executor_id": "operator.local",
        "execution_method": "authenticated_operator_session",
        "execution_evidence_ref": "ticket:TRUST-EXEC-1",
        "trust_registry_path": values["registry_path"],
        "execution_dir": values["execution_dir"],
        "backup_dir": values["backup_dir"],
        "now": NOW,
    }
    arguments.update(overrides)
    return execution.execute_current_enrollment(**arguments)


def test_exact_execution_replaces_temp_registry_and_persists_recovery_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path, monkeypatch)

    result = _execute(values)

    assert result["status"] == "verified"
    assert result["execution_state"] == "succeeded"
    assert result["trust_registry_write_attempted"] is True
    assert result["trust_registry_modified"] is True
    assert result["rollback_available"] is True
    assert result["protected_operation_executed"] is True
    assert Path(values["registry_path"]).read_bytes() == values["proposed_raw"]
    backup_path = Path(str(result["backup_path"]))
    assert backup_path.read_bytes() == values["current_raw"]
    claim_path = Path(values["execution_dir"]) / f"claim--{READINESS_ID}.json"
    result_path = Path(values["execution_dir"]) / f"result--{READINESS_ID}.json"
    assert stat.S_IMODE(claim_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
    history = execution.inspect_execution_history(
        execution_dir=Path(values["execution_dir"]),
        backup_dir=Path(values["backup_dir"]),
        now=NOW,
    )
    assert history["status"] == "verified"
    assert history["latest_execution"]["execution_state"] == "succeeded"


def test_exact_binding_mismatch_creates_no_claim_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path, monkeypatch)

    result = _execute(
        values,
        expected_proposed_trust_registry_sha256="9" * 64,
    )

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "trust_enrollment_execution_exact_binding_mismatch"
    )
    assert result["trust_registry_write_attempted"] is False
    assert Path(values["registry_path"]).read_bytes() == values["current_raw"]
    assert not Path(values["execution_dir"]).exists()


def test_immutable_claim_blocks_replay_even_if_registry_is_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path, monkeypatch)
    assert _execute(values)["execution_state"] == "succeeded"
    Path(values["registry_path"]).write_bytes(values["current_raw"])

    replay = _execute(values)

    assert replay["status"] == "blocked"
    assert replay["blocking_reason"] == (
        "trust_enrollment_execution_readiness_already_claimed"
    )
    assert Path(values["registry_path"]).read_bytes() == values["current_raw"]


def test_compare_and_swap_failure_records_backup_without_modification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        execution,
        "atomic_replace_bytes_if_matches",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated_compare_and_swap_failure")
        ),
    )

    result = _execute(values)

    assert result["status"] == "verified"
    assert result["execution_state"] == "write_failed"
    assert result["trust_registry_write_attempted"] is True
    assert result["trust_registry_modified"] is False
    assert result["rollback_available"] is True
    assert Path(values["registry_path"]).read_bytes() == values["current_raw"]


def test_postcondition_failure_reports_a_real_temp_registry_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        execution.claims,
        "load_producer_trust_registry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated_postcondition_failure")
        ),
    )

    result = _execute(values)

    assert result["status"] == "verified"
    assert result["execution_state"] == "postcondition_unverified"
    assert result["trust_registry_modified"] is True
    assert result["protected_operation_executed"] is True
    assert result["rollback_available"] is True
    assert Path(values["registry_path"]).read_bytes() == values["proposed_raw"]


def test_result_persistence_failure_does_not_hide_a_registry_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path, monkeypatch)
    original_write = execution.atomic_write_bytes

    def fail_result(path: Path, payload: bytes, *, overwrite: bool) -> None:
        if Path(path).name.startswith("result--"):
            raise RuntimeError("simulated_result_persistence_failure")
        original_write(path, payload, overwrite=overwrite)

    monkeypatch.setattr(execution, "atomic_write_bytes", fail_result)

    result = _execute(values)

    assert result["status"] == "blocked"
    assert result["execution_state"] == "recovery_required"
    assert result["blocking_reason"] == (
        "trust_enrollment_execution_result_persistence_failed"
    )
    assert result["trust_registry_write_attempted"] is True
    assert result["trust_registry_modified"] is True
    assert result["protected_operation_executed"] is True
    assert result["rollback_available"] is True
    assert Path(values["registry_path"]).read_bytes() == values["proposed_raw"]


def test_history_tamper_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path, monkeypatch)
    assert _execute(values)["execution_state"] == "succeeded"
    result_path = Path(values["execution_dir"]) / f"result--{READINESS_ID}.json"
    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    persisted["trust_registry_modified"] = False
    result_path.write_text(json.dumps(persisted), encoding="utf-8")

    history = execution.inspect_execution_history(
        execution_dir=Path(values["execution_dir"]),
        backup_dir=Path(values["backup_dir"]),
        now=NOW,
    )

    assert history["status"] == "blocked"
    assert history["history_state"] == "blocked"
    assert history["trust_registry_modified"] is False


def test_history_rejects_unknown_claim_fields_with_recomputed_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path, monkeypatch)
    assert _execute(values)["execution_state"] == "succeeded"
    claim_path = Path(values["execution_dir"]) / f"claim--{READINESS_ID}.json"
    persisted = json.loads(claim_path.read_text(encoding="utf-8"))
    persisted.pop("integrity")
    persisted["unexpected_authority"] = True
    claim_path.write_bytes(execution._canonical(execution._with_integrity(persisted)))

    history = execution.inspect_execution_history(
        execution_dir=Path(values["execution_dir"]),
        backup_dir=Path(values["backup_dir"]),
        now=NOW,
    )

    assert history["status"] == "blocked"
    assert history["history_state"] == "blocked"


def test_history_rejects_result_completed_after_authorization_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path, monkeypatch)
    assert _execute(values)["execution_state"] == "succeeded"
    result_path = Path(values["execution_dir"]) / f"result--{READINESS_ID}.json"
    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    persisted.pop("integrity")
    persisted["completed_at"] = (NOW + timedelta(hours=1)).isoformat()
    result_path.write_bytes(
        execution._canonical(execution._with_integrity(persisted))
    )

    history = execution.inspect_execution_history(
        execution_dir=Path(values["execution_dir"]),
        backup_dir=Path(values["backup_dir"]),
        now=NOW,
    )

    assert history["status"] == "blocked"
    assert history["history_state"] == "blocked"


def test_readiness_inspection_is_read_only_and_exposes_exact_manual_scope(
    tmp_path: Path,
) -> None:
    current = execution._canonical(_registry(active=False))
    proposed = execution._canonical(_registry(active=True))
    ready = _ready(
        current_sha256=execution._sha256(current),
        proposed_sha256=execution._sha256(proposed),
    )

    inspection = execution.inspect_execution_for_readiness(
        ready,
        execution_dir=tmp_path / "missing-executions",
        backup_dir=tmp_path / "missing-backups",
        now=NOW,
    )

    assert inspection["status"] == "verified"
    assert inspection["execution_state"] == "ready_for_manual_execution"
    assert inspection["action_required"] is True
    assert inspection["interrupt_operator"] is False
    assert inspection["manual_execution_authorized"] is True
    assert inspection["automatic_execution_allowed"] is False
    assert inspection["trust_registry_modified"] is False
    assert not (tmp_path / "missing-executions").exists()
    assert not (tmp_path / "missing-backups").exists()
