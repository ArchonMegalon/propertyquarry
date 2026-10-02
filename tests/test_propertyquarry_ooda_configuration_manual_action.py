from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_configuration_change_preview as preview
from scripts import propertyquarry_ooda_configuration_action_status as action_status
from scripts import propertyquarry_ooda_configuration_manual_action as manual
from scripts import propertyquarry_ooda_configuration_plan as plan


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _source_root(tmp_path: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    (tmp_path / plan.COMPOSE_PATH).write_text(
        "services:\n"
        "  propertyquarry-api:\n"
        "    ports:\n"
        f'      - "{plan.CURRENT_PORT_EXPRESSION}"\n',
        encoding="utf-8",
    )
    (tmp_path / plan.COMPOSE_PATH).chmod(0o644)
    (tmp_path / plan.ISOLATION_PATH).write_text(
        'API_HOST_PORT_KEY = "EA_HOST_PORT"\n'
        'if root_values.get(API_HOST_PORT_KEY) != "8097":\n'
        "    raise ValueError\n",
        encoding="utf-8",
    )
    (tmp_path / plan.DEPLOYMENT_AUDIT_PATH).write_text(
        'DEFAULT_LOCAL_ORIGIN: Final = "http://127.0.0.1:8097"\n',
        encoding="utf-8",
    )
    (tmp_path / plan.LIVE_SMOKE_PATH).write_text(
        'default=_env("PROPERTYQUARRY_LIVE_BASE_URL", "http://localhost:8097")\n',
        encoding="utf-8",
    )
    return tmp_path


def _request() -> dict[str, object]:
    return {
        "schema": "propertyquarry.ooda_runtime_authorization_request.v1",
        "request_id": "pqar_0123456789abcdef01234567",
        "generated_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        "binding": {
            "review_packet_sha256": "a" * 64,
            "proposal_sha256": "b" * 64,
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


def _decision(request: dict[str, object]) -> dict[str, object]:
    request_sha256 = plan._sha256(plan._canonical(request))
    return {
        "status": "verified",
        "decision": "approve_exact_scope",
        "decision_id": "pqad_0123456789abcdef01234567",
        "decision_sha256": "c" * 64,
        "recorded_at": NOW.isoformat(),
        "expires_at": request["expires_at"],
        "scope": dict(request["scope"]),
        "progress": {
            "current_evidence_verified": True,
            "authorization_decision_recorded": True,
        },
        "request_id": request["request_id"],
        "request_sha256": request_sha256,
        "authorization_required": True,
        "authorization_recorded": True,
        "exact_scope_authorized": True,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _approved_preview_inputs(root: Path) -> tuple[object, ...]:
    request = _request()
    request_sha256 = plan._sha256(plan._canonical(request))
    decision = _decision(request)
    sources = plan.inspect_configuration_sources(root=root)
    plan_artifact = plan.build_configuration_plan(
        request=request,
        request_sha256=request_sha256,
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )
    plan_verification = plan.verify_configuration_plan(
        plan_artifact,
        request=request,
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )
    assert plan_verification["status"] == "verified"
    plan_verification["progress"]["current_evidence_verified"] = True
    source_text, source_raw, source_sha256 = plan._source_snapshot(
        plan.COMPOSE_PATH,
        root=root,
    )
    artifact = preview.build_configuration_change_preview(
        request=request,
        request_sha256=request_sha256,
        plan=plan_artifact,
        plan_verification=plan_verification,
        decision_verification=decision,
        source_posture=sources,
        source_text=source_text,
        source_raw=source_raw,
        source_sha256=source_sha256,
        now=NOW,
    )
    artifact_sha256 = preview._sha256(preview._canonical(artifact))
    verification = preview.verify_configuration_change_preview(
        artifact,
        request=request,
        request_sha256=request_sha256,
        plan=plan_artifact,
        plan_verification=plan_verification,
        decision_verification=decision,
        source_posture=sources,
        source_text=source_text,
        source_raw=source_raw,
        source_sha256=source_sha256,
        now=NOW,
    )
    assert verification["status"] == "verified"
    verification["status"] = "ready"
    verification["current_evidence_verified"] = True
    return verification, artifact, artifact_sha256, source_text, source_raw, source_sha256


def _stage(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    inputs = _approved_preview_inputs(root)
    monkeypatch.setattr(manual, "_preview_inputs", lambda **_kwargs: inputs)
    result = manual.stage_current_manual_action_handoff(
        handoff_dir=tmp_path / "handoffs",
        preview_dir=tmp_path / "previews",
        root=root,
        now=NOW,
    )
    assert result["status"] == "ready", result
    return result


@pytest.mark.parametrize("decision", ["pending", "reject", "defer"])
def test_non_approval_never_stages_manual_action(
    decision: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_dir = tmp_path / "handoffs"
    monkeypatch.setattr(
        manual,
        "_preview_inputs",
        lambda **_kwargs: (
            {
                "status": "not_authorized",
                "authorization_decision": decision,
                "request_id": "pqar_0123456789abcdef01234567",
                "request_sha256": "a" * 64,
                "current_evidence_verified": True,
            },
            None,
            "",
            "",
            b"",
            "",
        ),
    )

    result = manual.stage_current_manual_action_handoff(
        handoff_dir=handoff_dir,
        now=NOW,
    )

    assert result["status"] == "not_authorized"
    assert result["authorization_decision"] == decision
    assert result["manual_apply_authorized"] is False
    assert result["automatic_execution_allowed"] is False
    assert result["source_edit_performed"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert not handoff_dir.exists()


def test_approved_handoff_is_private_exact_non_applying_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    staged = _stage(root, tmp_path, monkeypatch)
    handoff_path = Path(staged["handoff_path"])
    source_before = (root / plan.COMPOSE_PATH).read_bytes()
    original = handoff_path.read_bytes()
    original_mtime_ns = handoff_path.stat().st_mtime_ns
    reused = manual.stage_current_manual_action_handoff(
        handoff_dir=tmp_path / "handoffs",
        preview_dir=tmp_path / "previews",
        root=root,
        now=NOW + timedelta(seconds=1),
    )

    assert staged["status"] == "ready"
    assert staged["handoff_state"] == "refreshed"
    assert staged["current_evidence_verified"] is True
    assert staged["handoff_verified"] is True
    assert staged["manual_apply_authorized"] is True
    assert staged["manual_rollback_authorized"] is True
    assert staged["automatic_execution_allowed"] is False
    assert staged["source_edit_performed"] is False
    assert "--apply" in staged["apply_command"]
    assert stat.S_IMODE(handoff_path.stat().st_mode) == 0o600
    assert reused["status"] == "ready"
    assert reused["handoff_state"] == "reused"
    assert handoff_path.read_bytes() == original
    assert handoff_path.stat().st_mtime_ns == original_mtime_ns
    assert (root / plan.COMPOSE_PATH).read_bytes() == source_before


def test_tampered_handoff_is_blocked_and_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    staged = _stage(root, tmp_path, monkeypatch)
    handoff_path = Path(staged["handoff_path"])
    payload = json.loads(handoff_path.read_text())
    payload["target"]["expected_after_sha256"] = "f" * 64
    payload.pop("integrity")
    payload["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": manual._sha256(manual._canonical(payload)),
    }
    handoff_path.write_bytes(manual._canonical(payload))
    handoff_path.chmod(0o600)
    tampered = handoff_path.read_bytes()

    blocked = manual.stage_current_manual_action_handoff(
        handoff_dir=tmp_path / "handoffs",
        preview_dir=tmp_path / "previews",
        root=root,
        now=NOW + timedelta(seconds=1),
    )

    assert blocked["status"] == "blocked"
    assert blocked["blocking_reason"] == "manual_action_handoff_persisted_not_verified"
    assert blocked["source_edit_performed"] is False
    assert handoff_path.read_bytes() == tampered


def test_explicit_apply_is_atomic_idempotent_and_exact_rollback_restores_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    staged = _stage(root, tmp_path, monkeypatch)
    source_path = root / plan.COMPOSE_PATH
    source_before = source_path.read_bytes()
    source_mode = stat.S_IMODE(source_path.stat().st_mode)
    current_verification = {
        **staged,
        "status": "verified",
    }
    monkeypatch.setattr(
        manual,
        "verify_current_manual_action_handoff",
        lambda **_kwargs: current_verification,
    )
    receipt_dir = tmp_path / "receipts"

    applied = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(seconds=2),
    )
    applied_bytes = source_path.read_bytes()
    repeated = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(seconds=3),
    )
    rolled_back = manual.execute_manual_rollback(
        expected_execution_id=str(applied["execution_id"]),
        expected_execution_sha256=str(applied["execution_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(minutes=30),
    )
    repeated_rollback = manual.execute_manual_rollback(
        expected_execution_id=str(applied["execution_id"]),
        expected_execution_sha256=str(applied["execution_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(minutes=31),
    )

    assert applied["status"] == "applied"
    assert applied["source_edit_performed"] is True
    assert applied["protected_operation_executed"] is True
    assert applied["automatic_execution_allowed"] is False
    assert applied["deployment_or_restart_performed"] is False
    assert applied["provider_quota_consumed"] is False
    assert applied["delivery_attempted"] is False
    assert plan.PROPOSED_PORT_EXPRESSION.encode() in applied_bytes
    assert plan.CURRENT_PORT_EXPRESSION.encode() not in applied_bytes
    assert stat.S_IMODE(source_path.stat().st_mode) == source_mode
    assert stat.S_IMODE(Path(applied["execution_path"]).stat().st_mode) == 0o600
    assert repeated["status"] == "unchanged"
    assert repeated["source_edit_performed"] is False
    assert rolled_back["status"] == "rolled_back"
    assert rolled_back["source_edit_performed"] is True
    assert source_path.read_bytes() == source_before
    assert stat.S_IMODE(source_path.stat().st_mode) == source_mode
    assert stat.S_IMODE(Path(rolled_back["execution_path"]).stat().st_mode) == 0o600
    assert repeated_rollback["status"] == "unchanged"
    assert repeated_rollback["source_edit_performed"] is False


def test_source_drift_blocks_apply_before_receipt_or_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    staged = _stage(root, tmp_path, monkeypatch)
    source_path = root / plan.COMPOSE_PATH
    source_path.write_text(source_path.read_text() + "# unrelated drift\n")
    drifted = source_path.read_bytes()
    monkeypatch.setattr(
        manual,
        "verify_current_manual_action_handoff",
        lambda **_kwargs: {**staged, "status": "verified"},
    )
    receipt_dir = tmp_path / "receipts"

    blocked = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(seconds=2),
    )

    assert blocked["status"] == "blocked"
    assert blocked["blocking_reason"] == "manual_apply_failed_closed"
    assert blocked["source_edit_performed"] is False
    assert source_path.read_bytes() == drifted
    assert not receipt_dir.exists()


def test_apply_intent_reconciles_after_receipt_finalize_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    staged = _stage(root, tmp_path, monkeypatch)
    monkeypatch.setattr(
        manual,
        "verify_current_manual_action_handoff",
        lambda **_kwargs: {**staged, "status": "verified"},
    )
    receipt_dir = tmp_path / "receipts"
    real_atomic_write = manual.atomic_write_bytes

    def fail_final_receipt(path: Path, payload: bytes, *, overwrite: bool) -> None:
        if overwrite and Path(path).parent == receipt_dir:
            raise OSError("simulated final receipt failure")
        real_atomic_write(path, payload, overwrite=overwrite)

    monkeypatch.setattr(manual, "atomic_write_bytes", fail_final_receipt)
    interrupted = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(seconds=2),
    )
    monkeypatch.setattr(manual, "atomic_write_bytes", real_atomic_write)
    reconciled = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(seconds=3),
    )

    assert interrupted["status"] == "blocked"
    assert interrupted["blocking_reason"] == "manual_apply_receipt_finalize_failed"
    assert interrupted["source_edit_performed"] is True
    assert reconciled["status"] == "applied"
    assert reconciled["source_edit_performed"] is False
    assert reconciled["source_after_sha256"] == staged["target"][
        "expected_after_sha256"
    ]
    assert json.loads(Path(reconciled["execution_path"]).read_text())["status"] == (
        "applied"
    )


def test_expired_handoff_blocks_apply_without_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    staged = _stage(root, tmp_path, monkeypatch)
    source_path = root / plan.COMPOSE_PATH
    source_before = source_path.read_bytes()
    monkeypatch.setattr(
        manual,
        "verify_current_manual_action_handoff",
        lambda **_kwargs: manual._blocked("manual_action_current_evidence_unavailable"),
    )

    blocked = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=tmp_path / "receipts",
        root=root,
        now=NOW + timedelta(minutes=16),
    )

    assert blocked["status"] == "blocked"
    assert blocked["blocking_reason"] == "manual_apply_current_authority_not_verified"
    assert blocked["source_edit_performed"] is False
    assert source_path.read_bytes() == source_before
    assert not (tmp_path / "receipts").exists()


def test_tampered_apply_receipt_cannot_authorize_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    staged = _stage(root, tmp_path, monkeypatch)
    monkeypatch.setattr(
        manual,
        "verify_current_manual_action_handoff",
        lambda **_kwargs: {**staged, "status": "verified"},
    )
    receipt_dir = tmp_path / "receipts"
    applied = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(seconds=2),
    )
    receipt_path = Path(applied["execution_path"])
    payload = json.loads(receipt_path.read_text())
    payload["target"]["expected_before_sha256"] = "f" * 64
    payload.pop("integrity")
    payload["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": manual._sha256(manual._canonical(payload)),
    }
    receipt_path.write_bytes(manual._canonical(payload))
    receipt_path.chmod(0o600)
    source_after_apply = (root / plan.COMPOSE_PATH).read_bytes()

    blocked = manual.execute_manual_rollback(
        expected_execution_id=str(applied["execution_id"]),
        expected_execution_sha256=str(applied["execution_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(minutes=30),
    )

    assert blocked["status"] == "blocked"
    assert blocked["blocking_reason"] == "manual_rollback_invocation_not_admissible"
    assert blocked["source_edit_performed"] is False
    assert (root / plan.COMPOSE_PATH).read_bytes() == source_after_apply


def test_action_status_absent_is_quiet_and_does_not_create_presentation_state(
    tmp_path: Path,
) -> None:
    presentation_state = tmp_path / "presentation.json"
    inspected = action_status.inspect_manual_action_status(
        receipt_dir=tmp_path / "receipts",
        handoff_dir=tmp_path / "handoffs",
        root=tmp_path,
        now=NOW,
    )
    projected = action_status.apply_manual_action_presentation_state(
        inspected,
        state_path=presentation_state,
    )
    recorded = action_status.record_manual_action_presentation(
        projected,
        state_path=presentation_state,
        expected_semantic_digest=str(projected["semantic_digest"]),
        now=NOW,
    )

    assert inspected["status"] == "ready"
    assert inspected["state"] == "absent"
    assert inspected["action_required"] is False
    assert projected["interrupt_operator"] is False
    assert recorded["status"] == "unchanged"
    assert not presentation_state.exists()


def test_applied_status_interrupts_once_and_exposes_exact_refresh_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    staged = _stage(root, tmp_path, monkeypatch)
    monkeypatch.setattr(
        manual,
        "verify_current_manual_action_handoff",
        lambda **_kwargs: {**staged, "status": "verified"},
    )
    receipt_dir = tmp_path / "receipts"
    applied = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(seconds=2),
    )
    presentation_state = tmp_path / "presentation.json"
    inspected = action_status.inspect_manual_action_status(
        receipt_dir=receipt_dir,
        handoff_dir=tmp_path / "handoffs",
        root=root,
        now=NOW + timedelta(minutes=30),
    )
    first = action_status.apply_manual_action_presentation_state(
        inspected,
        state_path=presentation_state,
    )
    first_record = action_status.record_manual_action_presentation(
        first,
        state_path=presentation_state,
        expected_semantic_digest=str(first["semantic_digest"]),
        now=NOW + timedelta(minutes=30),
    )
    original = presentation_state.read_bytes()
    original_mtime_ns = presentation_state.stat().st_mtime_ns
    repeated_inspection = action_status.inspect_manual_action_status(
        receipt_dir=receipt_dir,
        handoff_dir=tmp_path / "handoffs",
        root=root,
        now=NOW + timedelta(minutes=31),
    )
    repeated = action_status.apply_manual_action_presentation_state(
        repeated_inspection,
        state_path=presentation_state,
    )
    repeated_record = action_status.record_manual_action_presentation(
        repeated,
        state_path=presentation_state,
        expected_semantic_digest=str(repeated["semantic_digest"]),
        now=NOW + timedelta(minutes=31),
    )

    assert applied["status"] == "applied"
    assert inspected["state"] == "applied"
    assert inspected["receipt_verified"] is True
    assert inspected["source_state_verified"] is True
    assert inspected["action_required"] is True
    assert "--refresh" in inspected["action_command"]
    assert str(applied["execution_id"]) in inspected["action_command"]
    assert str(applied["execution_sha256"]) in inspected["action_command"]
    assert "--rollback" in inspected["rollback_command"]
    assert str(applied["execution_id"]) in inspected["rollback_command"]
    assert str(applied["execution_sha256"]) in inspected["rollback_command"]
    assert inspected["deployment_or_restart_performed"] is False
    assert inspected["provider_quota_consumed"] is False
    assert inspected["delivery_attempted"] is False
    assert first["interrupt_operator"] is True
    assert first_record["status"] == "recorded"
    assert repeated["status"] == "pending_action"
    assert repeated["interrupt_operator"] is False
    assert repeated_record["status"] == "unchanged"
    assert presentation_state.read_bytes() == original
    assert presentation_state.stat().st_mtime_ns == original_mtime_ns


def test_prepared_apply_status_distinguishes_retry_from_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    staged = _stage(root, tmp_path, monkeypatch)
    monkeypatch.setattr(
        manual,
        "verify_current_manual_action_handoff",
        lambda **_kwargs: {**staged, "status": "verified"},
    )
    receipt_dir = tmp_path / "receipts"
    real_replace = manual.atomic_replace_bytes_if_matches
    monkeypatch.setattr(
        manual,
        "atomic_replace_bytes_if_matches",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fail before edit")),
    )
    failed_before = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(seconds=2),
    )
    retry_status = action_status.inspect_manual_action_status(
        receipt_dir=receipt_dir,
        handoff_dir=tmp_path / "handoffs",
        root=root,
        now=NOW + timedelta(seconds=3),
    )
    monkeypatch.setattr(manual, "atomic_replace_bytes_if_matches", real_replace)
    real_atomic_write = manual.atomic_write_bytes

    def fail_final(path: Path, payload: bytes, *, overwrite: bool) -> None:
        if overwrite and Path(path).parent == receipt_dir:
            raise OSError("fail final")
        real_atomic_write(path, payload, overwrite=overwrite)

    monkeypatch.setattr(manual, "atomic_write_bytes", fail_final)
    failed_after = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(seconds=4),
    )
    reconciliation_status = action_status.inspect_manual_action_status(
        receipt_dir=receipt_dir,
        handoff_dir=tmp_path / "handoffs",
        root=root,
        now=NOW + timedelta(seconds=5),
    )

    assert failed_before["status"] == "blocked"
    assert retry_status["state"] == "apply_prepared"
    assert "--apply" in retry_status["action_command"]
    assert failed_after["blocking_reason"] == "manual_apply_receipt_finalize_failed"
    assert reconciliation_status["state"] == "apply_reconciliation_required"
    assert "source edit must not be repeated" in reconciliation_status["next_action"]


def test_verified_rollback_clears_presented_apply_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    staged = _stage(root, tmp_path, monkeypatch)
    monkeypatch.setattr(
        manual,
        "verify_current_manual_action_handoff",
        lambda **_kwargs: {**staged, "status": "verified"},
    )
    receipt_dir = tmp_path / "receipts"
    applied = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(seconds=2),
    )
    applied_status = action_status.inspect_manual_action_status(
        receipt_dir=receipt_dir,
        handoff_dir=tmp_path / "handoffs",
        root=root,
        now=NOW + timedelta(seconds=3),
    )
    presentation_state = tmp_path / "presentation.json"
    projected_apply = action_status.apply_manual_action_presentation_state(
        applied_status,
        state_path=presentation_state,
    )
    assert action_status.record_manual_action_presentation(
        projected_apply,
        state_path=presentation_state,
        expected_semantic_digest=str(projected_apply["semantic_digest"]),
        now=NOW + timedelta(seconds=3),
    )["status"] == "recorded"
    rolled_back = manual.execute_manual_rollback(
        expected_execution_id=str(applied["execution_id"]),
        expected_execution_sha256=str(applied["execution_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(minutes=30),
    )
    rollback_status = action_status.inspect_manual_action_status(
        receipt_dir=receipt_dir,
        handoff_dir=tmp_path / "handoffs",
        root=root,
        now=NOW + timedelta(minutes=31),
    )
    projected_rollback = action_status.apply_manual_action_presentation_state(
        rollback_status,
        state_path=presentation_state,
    )
    cleared = action_status.record_manual_action_presentation(
        projected_rollback,
        state_path=presentation_state,
        expected_semantic_digest=str(projected_rollback["semantic_digest"]),
        now=NOW + timedelta(minutes=31),
    )

    assert rolled_back["status"] == "rolled_back"
    assert rollback_status["status"] == "ready"
    assert rollback_status["state"] == "rolled_back"
    assert rollback_status["action_required"] is False
    assert projected_rollback["interrupt_operator"] is False
    assert cleared["status"] == "recorded"
    assert json.loads(presentation_state.read_text())["active_digest"] == ""


def test_action_status_tamper_or_source_drift_fails_closed_and_dedupes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    staged = _stage(root, tmp_path, monkeypatch)
    monkeypatch.setattr(
        manual,
        "verify_current_manual_action_handoff",
        lambda **_kwargs: {**staged, "status": "verified"},
    )
    receipt_dir = tmp_path / "receipts"
    applied = manual.execute_manual_apply(
        expected_handoff_id=str(staged["handoff_id"]),
        expected_handoff_sha256=str(staged["handoff_sha256"]),
        operator_id="operator@example.test",
        handoff_dir=tmp_path / "handoffs",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(seconds=2),
    )
    source_path = root / plan.COMPOSE_PATH
    source_path.write_text(source_path.read_text() + "# drift\n")
    drifted = action_status.inspect_manual_action_status(
        receipt_dir=receipt_dir,
        handoff_dir=tmp_path / "handoffs",
        root=root,
        now=NOW + timedelta(seconds=3),
    )
    assert drifted["status"] == "blocked"
    assert drifted["blocking_reason"] == "manual_action_current_source_drifted"
    assert drifted["deployment_or_restart_performed"] is False

    source_path.write_bytes(
        source_path.read_bytes().replace(b"# drift\n", b"")
    )
    receipt_path = Path(applied["execution_path"])
    receipt_path.write_text("{}", encoding="utf-8")
    receipt_path.chmod(0o600)
    tampered = action_status.inspect_manual_action_status(
        receipt_dir=receipt_dir,
        handoff_dir=tmp_path / "handoffs",
        root=root,
        now=NOW + timedelta(seconds=4),
    )
    presentation_state = tmp_path / "presentation.json"
    first = action_status.apply_manual_action_presentation_state(
        tampered,
        state_path=presentation_state,
    )
    assert action_status.record_manual_action_presentation(
        first,
        state_path=presentation_state,
        expected_semantic_digest=str(first["semantic_digest"]),
        now=NOW + timedelta(seconds=4),
    )["status"] == "recorded"
    repeated = action_status.apply_manual_action_presentation_state(
        action_status.inspect_manual_action_status(
            receipt_dir=receipt_dir,
            handoff_dir=tmp_path / "handoffs",
            root=root,
            now=NOW + timedelta(seconds=5),
        ),
        state_path=presentation_state,
    )

    assert tampered["status"] == "blocked"
    assert tampered["blocking_reason"] == "manual_action_receipt_chain_not_admissible"
    assert first["interrupt_operator"] is True
    assert repeated["interrupt_operator"] is False
    assert repeated["presentation"]["already_presented"] is True


def test_invalid_action_presentation_state_is_never_used_or_overwritten(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "presentation.json"
    state_path.write_text("{}", encoding="utf-8")
    state_path.chmod(0o600)
    original = state_path.read_bytes()
    status = action_status._blocked("test_evidence_blocked", now=NOW)

    with pytest.raises(ValueError, match="manual_action_presentation_state_not_admissible"):
        action_status.apply_manual_action_presentation_state(
            status,
            state_path=state_path,
        )
    record_candidate = {
        **status,
        "presentation": {
            "semantic_digest": status["semantic_digest"],
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        },
    }
    recorded = action_status.record_manual_action_presentation(
        record_candidate,
        state_path=state_path,
        expected_semantic_digest=str(status["semantic_digest"]),
        now=NOW,
    )

    assert recorded["status"] == "blocked"
    assert recorded["blocking_reason"] == (
        "manual_action_presentation_state_not_admissible"
    )
    assert state_path.read_bytes() == original


def _dotenv_applied_receipt(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, object], str, bytes]:
    root = tmp_path / "dotenv-root"
    root.mkdir(mode=0o700)
    before = b"EXISTING_SETTING=preserved\n"
    target = root / ".env"
    target.write_bytes(before)
    target.chmod(0o600)
    candidate_path = (
        root
        / manual.runtime_review.DEFAULT_DEPLOYMENT_ENV_LOCAL_RUNTIME_CANDIDATE_PATH
    )
    candidate_path.parent.mkdir(mode=0o700, parents=True)
    keys = sorted(manual.runtime_review._LOCAL_CANDIDATE_KEYS)
    candidate = b"\n".join(
        f"{key}=synthetic-{index}".encode("utf-8")
        for index, key in enumerate(keys)
    ) + b"\n"
    candidate_path.write_bytes(candidate)
    candidate_path.chmod(0o600)
    snapshot_path = root / (
        "_completion/propertyquarry_ooda_notification_cycle/"
        "runtime-configuration-rollback-snapshots/"
        "pqar_0123456789abcdef01234567."
        + "a" * 64
        + ".env"
    )
    snapshot_path.parent.mkdir(mode=0o700, parents=True)
    snapshot_path.write_bytes(before)
    snapshot_path.chmod(0o600)
    target.write_bytes(before + candidate)
    target.chmod(0o600)
    execution_id = "pqme_0123456789abcdef01234567"
    receipt: dict[str, object] = {
        "schema": manual.DOTENV_EXECUTION_SCHEMA,
        "status": "applied",
        "execution_id": execution_id,
        "applied_at": NOW.isoformat(),
        "operation": "merge_dotenv_add_missing_keys",
        "binding": {
            "request_id": "pqar_0123456789abcdef01234567",
            "request_sha256": "a" * 64,
            "decision_id": "pqad_0123456789abcdef01234567",
            "decision_sha256": "b" * 64,
            "plan_id": "pqcp_0123456789abcdef01234567",
            "plan_sha256": "c" * 64,
        },
        "target": {
            "path": ".env",
            "file_mode": 0o600,
            "configuration_merge_policy": "add_missing_keys_only",
            "existing_environment_values_overwrite_allowed": False,
            "added_keys": keys,
            "added_key_count": len(keys),
            "existing_keys_preserved": True,
            "missing_authorized_key_count_after_apply": 0,
        },
        "rollback": {
            "required": True,
            "available": True,
            "snapshot_path": snapshot_path.relative_to(root).as_posix(),
            "snapshot_file_mode": 0o600,
            "snapshot_exact_pre_apply_bytes_verified": True,
            "automatic_rollback_allowed": False,
        },
        "authorization_required": True,
        "authorization_recorded": True,
        "exact_scope_authorized": True,
        "manual_apply_authorized": True,
        "automatic_execution_allowed": False,
        "runtime_configuration_change_performed": True,
        "source_edit_performed": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": manual._sha256(
            manual._dotenv_canonical(receipt)
        ),
    }
    receipt_dir = (
        root
        / "_completion/propertyquarry_ooda_notification_cycle/"
        "runtime-configuration-manual-action-receipts"
    )
    receipt_dir.mkdir(mode=0o700, parents=True)
    receipt_path = receipt_dir / f"{execution_id}.json"
    raw = manual._dotenv_canonical(receipt)
    receipt_path.write_bytes(raw)
    receipt_path.chmod(0o600)
    return root, receipt_dir, receipt, manual._sha256(raw), before


def test_dotenv_apply_receipt_is_value_safe_and_projects_exact_refresh(
    tmp_path: Path,
) -> None:
    root, receipt_dir, receipt, receipt_sha256, _before = _dotenv_applied_receipt(
        tmp_path
    )

    verified = manual.verify_dotenv_apply_receipt_state(receipt, root=root)
    status = action_status.inspect_manual_action_status(
        receipt_dir=receipt_dir,
        handoff_dir=root / "unused-handoffs",
        include_evidence_refresh=False,
        root=root,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["receipt_sha256"] == receipt_sha256
    assert verified["environment_values_recorded"] is False
    assert verified["environment_values_hashed"] is False
    assert verified["state_binding"]["added_keys"] == sorted(
        manual.runtime_review._LOCAL_CANDIDATE_KEYS
    )
    assert status["status"] == "action_required"
    assert status["state"] == "applied"
    assert status["receipt_verified"] is True
    assert status["source_state_verified"] is True
    assert status["environment_values_recorded"] is False
    assert status["environment_values_hashed"] is False
    assert receipt_sha256 in status["action_command"]
    assert receipt_sha256 in status["rollback_command"]


def test_dotenv_candidate_drift_blocks_status_without_exposing_values(
    tmp_path: Path,
) -> None:
    root, receipt_dir, _receipt, _receipt_sha256, _before = (
        _dotenv_applied_receipt(tmp_path)
    )
    candidate_path = (
        root
        / manual.runtime_review.DEFAULT_DEPLOYMENT_ENV_LOCAL_RUNTIME_CANDIDATE_PATH
    )
    candidate_path.write_bytes(candidate_path.read_bytes().replace(b"synthetic-0", b"drifted-0"))
    candidate_path.chmod(0o600)

    status = action_status.inspect_manual_action_status(
        receipt_dir=receipt_dir,
        handoff_dir=root / "unused-handoffs",
        include_evidence_refresh=False,
        root=root,
        now=NOW,
    )

    assert status["status"] == "blocked"
    assert status["blocking_reason"] == "manual_action_current_source_drifted"
    assert status["receipt_verified"] is False


def test_dotenv_explicit_rollback_restores_private_snapshot_and_reconciles(
    tmp_path: Path,
) -> None:
    root, receipt_dir, _receipt, receipt_sha256, before = _dotenv_applied_receipt(
        tmp_path
    )

    rolled_back = manual.execute_manual_rollback(
        expected_execution_id="pqme_0123456789abcdef01234567",
        expected_execution_sha256=receipt_sha256,
        operator_id="operator@example.test",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(minutes=1),
    )
    repeated = manual.execute_manual_rollback(
        expected_execution_id="pqme_0123456789abcdef01234567",
        expected_execution_sha256=receipt_sha256,
        operator_id="operator@example.test",
        receipt_dir=receipt_dir,
        root=root,
        now=NOW + timedelta(minutes=2),
    )
    status = action_status.inspect_manual_action_status(
        receipt_dir=receipt_dir,
        handoff_dir=root / "unused-handoffs",
        include_evidence_refresh=False,
        root=root,
        now=NOW + timedelta(minutes=2),
    )

    assert rolled_back["status"] == "rolled_back"
    assert rolled_back["runtime_configuration_change_performed"] is True
    assert rolled_back["source_edit_performed"] is False
    assert rolled_back["environment_values_recorded"] is False
    assert rolled_back["environment_values_hashed"] is False
    assert (root / ".env").read_bytes() == before
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    assert repeated["status"] == "unchanged"
    assert status["status"] == "ready"
    assert status["state"] == "rolled_back"
    assert status["source_state_verified"] is True
