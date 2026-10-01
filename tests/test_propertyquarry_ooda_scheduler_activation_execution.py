from __future__ import annotations

import json
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_scheduler_activation_authorization as authorization
from scripts import propertyquarry_ooda_scheduler_activation_decision as decision
from scripts import propertyquarry_ooda_scheduler_activation_execution as execution
from scripts import propertyquarry_ooda_scheduler_activation_readiness as readiness
from scripts.propertyquarry_ooda_operator_status import (
    apply_operator_presentation_state,
    record_operator_presentation,
)


NOW = datetime(2026, 8, 26, 19, 10, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _readiness(*, now: datetime = NOW) -> dict[str, object]:
    return {
        "schema": readiness.VERIFY_SCHEMA,
        "status": "verified",
        "readiness_state": "ready_for_authorization",
        "updated_at": now.isoformat(),
        "readiness_observed_at": now.isoformat(),
        "readiness_receipt_sha256": "a" * 64,
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
                "render_image_digest": "sha256:" + ("3" * 64),
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
            "preflight_script_sha256": readiness._script_digest(),
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


def _authority(
    tmp_path: Path,
    *,
    decision_value: str = "approve_exact_scope",
) -> dict[str, object]:
    activation_readiness = _readiness()
    request = authorization.build_scheduler_activation_authorization_request(
        activation_readiness=activation_readiness,
        now=NOW,
    )
    receipt = decision.build_scheduler_activation_authorization_decision(
        request=request,
        activation_readiness=activation_readiness,
        decision=decision_value,
        decider_id="operator:test",
        now=NOW + timedelta(seconds=1),
    )
    request_path = tmp_path / "request.json"
    readiness_path = tmp_path / "readiness.json"
    decision_dir = tmp_path / "decisions"
    request_path.write_bytes(authorization._canonical(request))
    request_path.chmod(0o600)
    readiness_path.write_bytes(authorization._canonical(activation_readiness))
    readiness_path.chmod(0o600)
    decision_dir.mkdir(mode=0o700)
    decision_path = decision_dir / "approval.json"
    decision_path.write_bytes(decision._canonical(receipt))
    decision_path.chmod(0o600)
    verified = decision.verify_current_scheduler_activation_authorization_decision(
        request_path=request_path,
        readiness_path=readiness_path,
        decision_dir=decision_dir,
        now=NOW + timedelta(seconds=2),
    )
    assert verified["status"] == "verified"
    return {
        "request": request,
        "request_path": request_path,
        "readiness_path": readiness_path,
        "decision_dir": decision_dir,
        "decision": verified,
    }


def _preflight(
    release: dict[str, object],
    *,
    now: datetime,
    render_digit: str = "3",
) -> dict[str, object]:
    return readiness._observation(
        status="ready",
        observed_at=now,
        blocking_reason="",
        duration_ms=10,
        exit_code=0,
        script_sha256=readiness._script_digest(),
        stdout=b"ready",
        stderr=b"",
        project="property",
        runtime_commit_sha=str(release["runtime_commit_sha"]),
        envelope_commit_sha=str(release["envelope_commit_sha"]),
        web_image_digest=str(release["web_image_digest"]),
        render_image_digest="sha256:" + (render_digit * 64),
        release_tree_identity={
            "release_tree_identity_verified": True,
            "release_head_commit_sha": str(release["envelope_commit_sha"]),
            "release_worktree_status_sha256": readiness._sha256(b""),
            "release_worktree_status_bytes": 0,
            "release_worktree_clean": True,
            "changed_paths_recorded": False,
        },
        candidate_drop_posture=readiness._candidate_drop_observation(
            status="ready",
            observed_at=now,
            blocking_reason="",
            source_path_sha256="4" * 64,
            compose_sha256="5" * 64,
            runtime_image_spec_sha256="6" * 64,
            configured_group_gid=1000,
            directory_present=True,
            directory_mode="0750",
            directory_uid=1000,
            directory_gid=1000,
            directory_type_verified=True,
            directory_owner_verified=True,
            directory_group_verified=True,
            directory_permissions_verified=True,
            compose_read_only_bind_verified=True,
            compose_create_host_path_disabled=True,
            compose_supplemental_group_verified=True,
            scheduler_non_root_identity_verified=True,
        ),
    )


def _runtime_receipt(
    path: Path,
    *,
    release: dict[str, object],
    now: datetime,
) -> None:
    payload = {
        "schema": "propertyquarry.local_docker_deployment.v1",
        "observed_at": now.isoformat(),
        "authority": {"scope": "local_docker", "github_actions_used": False},
        "runtime_commit_sha": release["runtime_commit_sha"],
        "envelope_head_sha": release["envelope_commit_sha"],
        "images": {
            "web": release["web_image_digest"],
            "render": release["render_image_digest"],
        },
        "compose": {"project": "property"},
        "services": {
            "propertyquarry-scheduler": {
                "status": "running",
                "health": "healthy",
                "image_id": release["web_image_digest"],
            }
        },
        "secret_values_recorded": False,
        "passed": True,
        "failures": [],
    }
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def _success_observation(**kwargs: object) -> dict[str, object]:
    release = dict(kwargs["release_evidence"])
    receipt_path = Path(kwargs["deployment_receipt_path"])
    return execution._deployment_observation(
        status="succeeded",
        started_at=NOW + timedelta(seconds=2),
        completed_at=NOW + timedelta(seconds=3),
        duration_ms=1_000,
        exit_code=0,
        blocking_reason="",
        stdout=b"deployed",
        stderr=b"",
        release_evidence=release,
        project=str(kwargs["project"]),
        deployment_receipt_path=receipt_path,
        reported_runtime_commit_sha=str(release["runtime_commit_sha"]),
        reported_envelope_commit_sha=str(release["envelope_commit_sha"]),
    )


def test_current_approval_projects_one_exact_nonautomatic_command(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    execution_dir = tmp_path / "executions"
    status = execution.inspect_current_scheduler_activation_execution(
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=execution_dir,
        deployment_receipt_path=tmp_path / "deployment.json",
        now=NOW + timedelta(seconds=2),
    )

    assert status["status"] == "action_required"
    assert status["execution_state"] == "authorized_pending_manual_execution"
    assert status["interrupt_operator"] is True
    assert status["manual_deployment_authorized"] is True
    assert status["automatic_execution_allowed"] is False
    assert status["deployment_attempted"] is False
    assert status["provider_quota_consumption_allowed"] is False
    assert status["delivery_authorized"] is False
    assert "--execute-approved-deployment" in status["execution_command_argv"]
    assert str(authority["decision"]["decision_id"]) in status[
        "execution_command_argv"
    ]
    assert not execution_dir.exists()


def test_negative_decision_never_projects_or_claims_execution(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path, decision_value="reject")
    execution_dir = tmp_path / "executions"
    status = execution.inspect_current_scheduler_activation_execution(
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=execution_dir,
        now=NOW + timedelta(seconds=2),
    )
    assert status["status"] == "ready"
    assert status["execution_state"] == "negative_decision_recorded"
    assert status["action_required"] is False
    assert status["execution_authorized"] is False
    assert status["deployment_attempted"] is False
    assert status["actions"] == []
    assert not execution_dir.exists()


def test_execution_history_absence_is_verified_without_creating_a_ledger(
    tmp_path: Path,
) -> None:
    execution_dir = tmp_path / "executions"
    history = execution.inspect_scheduler_activation_execution_history(
        execution_dir=execution_dir,
        now=NOW,
    )
    assert history["status"] == "verified"
    assert history["history_state"] == "no_execution_history"
    assert history["execution_history_present"] is False
    assert history["action_required"] is False
    assert not execution_dir.exists()


def test_pending_manual_execution_is_presented_once_per_exact_decision(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    state_path = tmp_path / "presentation.json"
    common = {
        "request_path": Path(authority["request_path"]),
        "readiness_path": Path(authority["readiness_path"]),
        "decision_dir": Path(authority["decision_dir"]),
        "execution_dir": tmp_path / "executions",
        "deployment_receipt_path": tmp_path / "deployment.json",
    }
    first = execution.inspect_current_scheduler_activation_execution(
        **common,
        now=NOW + timedelta(seconds=2),
    )
    first = apply_operator_presentation_state(first, state_path=state_path)
    assert first["interrupt_operator"] is True
    recorded = record_operator_presentation(
        first,
        state_path=state_path,
        expected_source_cycle_receipt_sha256="a" * 64,
        now=NOW + timedelta(seconds=2),
    )
    assert recorded["status"] == "recorded"

    repeated = execution.inspect_current_scheduler_activation_execution(
        **common,
        now=NOW + timedelta(seconds=3),
    )
    repeated = apply_operator_presentation_state(
        repeated,
        state_path=state_path,
    )
    assert repeated["status"] == "pending_action"
    assert repeated["interrupt_operator"] is False
    assert repeated["actions"] == []
    assert len(repeated["pending_actions"]) == 1


def test_success_is_private_receipted_and_cannot_be_replayed(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    current = dict(authority["decision"])
    release = dict(current["release_evidence"])
    execution_dir = tmp_path / "executions"
    deployment_receipt = tmp_path / "deployment.json"
    _runtime_receipt(
        deployment_receipt,
        release=release,
        now=NOW + timedelta(seconds=2),
    )
    calls = {"preflight": 0, "deployment": 0}

    def preflight_runner(**kwargs: object) -> dict[str, object]:
        calls["preflight"] += 1
        return _preflight(release, now=kwargs["now"])

    def deployment_runner(**kwargs: object) -> dict[str, object]:
        calls["deployment"] += 1
        return _success_observation(**kwargs)

    result = execution.execute_current_scheduler_activation(
        expected_request_id=str(current["request_id"]),
        expected_request_sha256=str(current["request_sha256"]),
        expected_decision_id=str(current["decision_id"]),
        expected_decision_sha256=str(current["decision_sha256"]),
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=execution_dir,
        deployment_receipt_path=deployment_receipt,
        now=NOW + timedelta(seconds=2),
        preflight_runner=preflight_runner,
        deployment_runner=deployment_runner,
    )

    assert result["status"] == "verified"
    assert result["execution_state"] == "succeeded"
    assert result["authorization_consumed"] is True
    assert result["manual_deployment_authorized"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["deployment_attempted"] is True
    assert result["deployment_or_restart_performed"] is True
    assert result["runtime_receipt_verified"] is True
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert calls == {"preflight": 1, "deployment": 1}
    claim_path = Path(str(result["claim_path"]))
    result_path = Path(str(result["result_path"]))
    assert stat.S_IMODE(execution_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(claim_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600

    replay = execution.execute_current_scheduler_activation(
        expected_request_id=str(current["request_id"]),
        expected_request_sha256=str(current["request_sha256"]),
        expected_decision_id=str(current["decision_id"]),
        expected_decision_sha256=str(current["decision_sha256"]),
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=execution_dir,
        deployment_receipt_path=deployment_receipt,
        now=NOW + timedelta(seconds=3),
        preflight_runner=preflight_runner,
        deployment_runner=deployment_runner,
    )
    assert replay["blocking_reason"] == (
        "scheduler_activation_execution_already_claimed"
    )
    assert calls == {"preflight": 1, "deployment": 1}

    inspected = execution.inspect_current_scheduler_activation_execution(
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=execution_dir,
        deployment_receipt_path=deployment_receipt,
        now=NOW + timedelta(seconds=3),
    )
    assert inspected["status"] == "verified"
    assert inspected["execution_state"] == "succeeded"
    assert inspected["result_sha256"] == result["result_sha256"]

    historical = execution.inspect_scheduler_activation_execution_history(
        execution_dir=execution_dir,
        now=NOW + timedelta(seconds=2_000),
    )
    assert historical["status"] == "verified"
    assert historical["history_state"] == "execution_result"
    assert historical["latest_execution"]["execution_state"] == "succeeded"
    assert historical["latest_result_sha256"] == result["result_sha256"]
    assert historical["execution_authorized"] is False
    assert historical["authorization_consumed"] is True


def test_concurrent_invocations_allow_only_one_deployment_attempt(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    current = dict(authority["decision"])
    release = dict(current["release_evidence"])
    execution_dir = tmp_path / "executions"
    deployment_receipt = tmp_path / "deployment.json"
    _runtime_receipt(
        deployment_receipt,
        release=release,
        now=NOW + timedelta(seconds=2),
    )
    preflight_entered = threading.Event()
    release_preflight = threading.Event()
    calls = {"deployment": 0}

    def preflight_runner(**kwargs: object) -> dict[str, object]:
        preflight_entered.set()
        assert release_preflight.wait(timeout=5)
        return _preflight(release, now=kwargs["now"])

    def deployment_runner(**kwargs: object) -> dict[str, object]:
        calls["deployment"] += 1
        return _success_observation(**kwargs)

    arguments = {
        "expected_request_id": str(current["request_id"]),
        "expected_request_sha256": str(current["request_sha256"]),
        "expected_decision_id": str(current["decision_id"]),
        "expected_decision_sha256": str(current["decision_sha256"]),
        "request_path": Path(authority["request_path"]),
        "readiness_path": Path(authority["readiness_path"]),
        "decision_dir": Path(authority["decision_dir"]),
        "execution_dir": execution_dir,
        "deployment_receipt_path": deployment_receipt,
        "now": NOW + timedelta(seconds=2),
        "preflight_runner": preflight_runner,
        "deployment_runner": deployment_runner,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(
            execution.execute_current_scheduler_activation,
            **arguments,
        )
        assert preflight_entered.wait(timeout=5)
        loser = pool.submit(
            execution.execute_current_scheduler_activation,
            **arguments,
        )
        loser_result = loser.result(timeout=5)
        release_preflight.set()
        winner_result = winner.result(timeout=5)

    assert loser_result["blocking_reason"] == (
        "scheduler_activation_execution_already_claimed"
    )
    assert winner_result["execution_state"] == "succeeded"
    assert calls["deployment"] == 1


def test_expected_identity_mismatch_does_not_claim_or_execute(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    current = dict(authority["decision"])
    execution_dir = tmp_path / "executions"
    calls = {"preflight": 0, "deployment": 0}

    result = execution.execute_current_scheduler_activation(
        expected_request_id=str(current["request_id"]),
        expected_request_sha256="9" * 64,
        expected_decision_id=str(current["decision_id"]),
        expected_decision_sha256=str(current["decision_sha256"]),
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=execution_dir,
        now=NOW + timedelta(seconds=2),
        preflight_runner=lambda **_: calls.__setitem__("preflight", 1),
        deployment_runner=lambda **_: calls.__setitem__("deployment", 1),
    )
    assert result["blocking_reason"] == (
        "scheduler_activation_execution_expected_authority_mismatch"
    )
    assert result["deployment_attempted"] is False
    assert calls == {"preflight": 0, "deployment": 0}
    assert not execution_dir.exists()


def test_preflight_release_drift_consumes_claim_without_deployment(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    current = dict(authority["decision"])
    release = dict(current["release_evidence"])
    calls = {"deployment": 0}

    result = execution.execute_current_scheduler_activation(
        expected_request_id=str(current["request_id"]),
        expected_request_sha256=str(current["request_sha256"]),
        expected_decision_id=str(current["decision_id"]),
        expected_decision_sha256=str(current["decision_sha256"]),
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=tmp_path / "executions",
        now=NOW + timedelta(seconds=2),
        preflight_runner=lambda **kwargs: _preflight(
            release,
            now=kwargs["now"],
            render_digit="4",
        ),
        deployment_runner=lambda **_: calls.__setitem__("deployment", 1),
    )

    assert result["status"] == "verified"
    assert result["execution_state"] == "blocked_before_execution"
    assert result["blocking_reason"] == "authorized_release_evidence_changed"
    assert result["authorization_consumed"] is True
    assert result["deployment_attempted"] is False
    assert result["protected_operation_executed"] is False
    assert calls["deployment"] == 0


def test_failed_deployment_is_receipted_and_not_retried(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    current = dict(authority["decision"])
    release = dict(current["release_evidence"])
    execution_dir = tmp_path / "executions"
    calls = {"deployment": 0}

    def failed_deployment(**kwargs: object) -> dict[str, object]:
        calls["deployment"] += 1
        return execution._deployment_observation(
            status="failed",
            started_at=NOW + timedelta(seconds=2),
            completed_at=NOW + timedelta(seconds=3),
            duration_ms=1_000,
            exit_code=2,
            blocking_reason="activation_deployment_failed",
            stdout=b"",
            stderr=b"failed",
            release_evidence=dict(kwargs["release_evidence"]),
            project=str(kwargs["project"]),
            deployment_receipt_path=Path(kwargs["deployment_receipt_path"]),
        )

    result = execution.execute_current_scheduler_activation(
        expected_request_id=str(current["request_id"]),
        expected_request_sha256=str(current["request_sha256"]),
        expected_decision_id=str(current["decision_id"]),
        expected_decision_sha256=str(current["decision_sha256"]),
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=execution_dir,
        now=NOW + timedelta(seconds=2),
        preflight_runner=lambda **kwargs: _preflight(
            release,
            now=kwargs["now"],
        ),
        deployment_runner=failed_deployment,
    )
    assert result["status"] == "verified"
    assert result["execution_state"] == "deployment_failed"
    assert result["deployment_attempted"] is True
    assert result["protected_operation_executed"] is True
    assert result["runtime_receipt_verified"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert calls["deployment"] == 1

    replay = execution.execute_current_scheduler_activation(
        expected_request_id=str(current["request_id"]),
        expected_request_sha256=str(current["request_sha256"]),
        expected_decision_id=str(current["decision_id"]),
        expected_decision_sha256=str(current["decision_sha256"]),
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=execution_dir,
        now=NOW + timedelta(seconds=3),
        preflight_runner=lambda **kwargs: _preflight(
            release,
            now=kwargs["now"],
        ),
        deployment_runner=failed_deployment,
    )
    assert replay["blocking_reason"] == (
        "scheduler_activation_execution_already_claimed"
    )
    assert calls["deployment"] == 1


def test_claim_without_result_requires_recovery_not_replay(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    current = dict(authority["decision"])
    execution_dir = tmp_path / "executions"
    execution_dir.mkdir(mode=0o700)
    claim = execution.build_scheduler_activation_execution_claim(
        decision_verification=current,
        now=NOW + timedelta(seconds=2),
    )
    claim_path, _result_path = execution._paths(
        execution_dir,
        str(current["decision_id"]),
    )
    claim_path.write_bytes(execution._canonical(claim))
    claim_path.chmod(0o600)

    status = execution.inspect_current_scheduler_activation_execution(
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=execution_dir,
        now=NOW + timedelta(seconds=3),
    )
    assert status["status"] == "action_required"
    assert status["execution_state"] == "claimed_without_result"
    assert status["authorization_consumed"] is True
    assert status["deployment_attempted"] is False
    assert "do not replay" in status["next_action"]

    historical = execution.inspect_scheduler_activation_execution_history(
        execution_dir=execution_dir,
        now=NOW + timedelta(seconds=2_000),
    )
    assert historical["status"] == "verified"
    assert historical["history_state"] == "claimed_without_result"
    assert historical["latest_claim_id"] == claim["claim_id"]
    assert historical["execution_authorized"] is False


def test_deployment_runner_exception_is_fail_closed_as_an_attempt(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    current = dict(authority["decision"])
    release = dict(current["release_evidence"])

    def unavailable_deployment(**_: object) -> dict[str, object]:
        raise OSError("process unavailable")

    result = execution.execute_current_scheduler_activation(
        expected_request_id=str(current["request_id"]),
        expected_request_sha256=str(current["request_sha256"]),
        expected_decision_id=str(current["decision_id"]),
        expected_decision_sha256=str(current["decision_sha256"]),
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=tmp_path / "executions",
        now=NOW + timedelta(seconds=2),
        preflight_runner=lambda **kwargs: _preflight(
            release,
            now=kwargs["now"],
        ),
        deployment_runner=unavailable_deployment,
    )
    assert result["status"] == "verified"
    assert result["execution_state"] == "deployment_failed"
    assert result["deployment_attempted"] is True
    assert result["protected_operation_executed"] is True
    assert result["runtime_receipt_verified"] is False


def test_tampered_result_fails_closed(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    current = dict(authority["decision"])
    release = dict(current["release_evidence"])
    execution_dir = tmp_path / "executions"
    deployment_receipt = tmp_path / "deployment.json"
    _runtime_receipt(
        deployment_receipt,
        release=release,
        now=NOW + timedelta(seconds=2),
    )
    result = execution.execute_current_scheduler_activation(
        expected_request_id=str(current["request_id"]),
        expected_request_sha256=str(current["request_sha256"]),
        expected_decision_id=str(current["decision_id"]),
        expected_decision_sha256=str(current["decision_sha256"]),
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=execution_dir,
        deployment_receipt_path=deployment_receipt,
        now=NOW + timedelta(seconds=2),
        preflight_runner=lambda **kwargs: _preflight(
            release,
            now=kwargs["now"],
        ),
        deployment_runner=_success_observation,
    )
    result_path = Path(str(result["result_path"]))
    payload = json.loads(result_path.read_text())
    payload["provider_quota_consumption_allowed"] = True
    payload.pop("integrity")
    payload["integrity"] = execution._integrity(payload)
    result_path.write_bytes(execution._canonical(payload))

    inspected = execution.inspect_current_scheduler_activation_execution(
        request_path=Path(authority["request_path"]),
        readiness_path=Path(authority["readiness_path"]),
        decision_dir=Path(authority["decision_dir"]),
        execution_dir=execution_dir,
        deployment_receipt_path=deployment_receipt,
        now=NOW + timedelta(seconds=3),
    )
    assert inspected["status"] == "blocked"
    assert inspected["blocking_reason"] == (
        "scheduler_activation_execution_evidence_not_admissible"
    )
    historical = execution.inspect_scheduler_activation_execution_history(
        execution_dir=execution_dir,
        now=NOW + timedelta(seconds=2_000),
    )
    assert historical["status"] == "blocked"
    assert historical["blocking_reason"] == (
        "scheduler_activation_execution_history_not_admissible"
    )


def test_deploy_script_rejects_partial_exact_release_guards_before_preflight() -> None:
    result = subprocess.run(
        [
            "bash",
            "scripts/deploy_propertyquarry.sh",
            "--preflight-only",
            "--no-build",
            "--expected-runtime-commit",
            "f" * 40,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Exact-release guards must be supplied together." in result.stderr
    assert "READY local Docker deployment" not in result.stdout


def test_deploy_script_requires_no_build_for_exact_release_guard() -> None:
    result = subprocess.run(
        [
            "bash",
            "scripts/deploy_propertyquarry.sh",
            "--preflight-only",
            "--expected-runtime-commit",
            "f" * 40,
            "--expected-envelope-commit",
            "1" * 40,
            "--expected-web-image",
            "sha256:" + ("2" * 64),
            "--expected-render-image",
            "sha256:" + ("3" * 64),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "requires --no-build" in result.stderr
    assert "READY local Docker deployment" not in result.stdout


def test_deploy_exact_release_guard_precedes_every_mutating_release_step() -> None:
    source = (ROOT / "scripts/deploy_propertyquarry.sh").read_text()
    digest_guard = source.index(
        "Governed deploy script digest does not match the authorized preflight."
    )
    environment_load = source.index('load_env_file "${APP_ROOT}/.env"')
    release_guard = source.index(
        "Current release evidence does not match the explicitly authorized exact scope."
    )
    signal_dir_mutation = source.index(
        'if [[ -L "${PROPERTYQUARRY_OODA_SIGNAL_DIR}" ]]'
    )
    database_start = source.index(
        'up --detach --wait --wait-timeout 120 propertyquarry-db'
    )

    assert digest_guard < environment_load
    assert release_guard < signal_dir_mutation < database_start
