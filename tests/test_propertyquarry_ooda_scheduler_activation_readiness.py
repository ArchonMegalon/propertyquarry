from __future__ import annotations

import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts import propertyquarry_ooda_scheduler_activation_readiness as activation
from scripts import propertyquarry_ooda_scheduler_continuity as continuity
from scripts import propertyquarry_ooda_scheduler_witness as scheduler_witness


NOW = datetime(2026, 8, 26, 18, 15, tzinfo=timezone.utc)


def _runtime(*, active: bool) -> dict[str, object]:
    count = 1 if active else 0
    return {
        "schema": runtime_review.RUNTIME_OBSERVATION_VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": NOW.isoformat(),
        "runtime_condition": "present" if active else "absent",
        "runtime_observed_at": NOW.isoformat(),
        "current_observed_at": NOW.isoformat(),
        "runtime_observation_sha256": "a" * 64,
        "scheduler_service": runtime_review.PROPERTYQUARRY_SCHEDULER_SERVICE,
        "scheduler_condition": "running" if active else "absent",
        "scheduler_state": "running" if active else "",
        "scheduler_health": "healthy" if active else "none",
        "scheduler_container_healthy": active,
        "persistent_reevaluation_running": False,
        "progress": {
            "container_count": count,
            "running_container_count": count,
            "non_running_container_count": 0,
            "scheduler_container_count": count,
            "running_scheduler_container_count": count,
            "healthy_scheduler_container_count": count,
            "current_evidence_verified": True,
        },
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _witness() -> dict[str, object]:
    return {
        "schema": scheduler_witness.VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": NOW.isoformat(),
        "blocking_reason": "",
        "iteration_status": "completed",
        "iteration_blocking_reason": "",
        "iteration_updated_at": NOW.isoformat(),
        "iteration_witness_sha256": "b" * 64,
        "cycle_evidence": {
            "sha256": "c" * 64,
            "status": "silent",
            "execution_mode": "evaluate_only",
        },
        "progress": {
            "cycle_binding_verified": True,
            "current_evidence_verified": True,
        },
        "persistent_reevaluation_verified": True,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def _continuity(*, active: bool = False) -> dict[str, object]:
    runtime = _runtime(active=active)
    witness = _witness() if active else None
    receipt = continuity.build_scheduler_continuity_receipt(
        runtime_observation=runtime,
        scheduler_iteration_witness=witness,
        source_type="runtime_container" if active else "operator_filesystem_no_runtime_container",
        now=NOW,
    )
    verified = continuity.verify_scheduler_continuity_receipt(
        receipt,
        runtime_observation=runtime,
        scheduler_iteration_witness=witness,
        now=NOW,
    )
    verified["verification_receipt_persisted"] = True
    return verified


def _candidate_drop_posture(
    *,
    now: datetime = NOW,
) -> dict[str, object]:
    return activation._candidate_drop_observation(
        status="ready",
        observed_at=now,
        blocking_reason="",
        source_path_sha256="3" * 64,
        compose_sha256="4" * 64,
        runtime_image_spec_sha256="5" * 64,
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
    )


def _preflight(*, ready: bool) -> dict[str, object]:
    head_commit = "f" * 40
    return activation._observation(
        status="ready" if ready else "blocked",
        observed_at=NOW,
        blocking_reason="" if ready else "release_worktree_not_clean",
        duration_ms=12,
        exit_code=0 if ready else 2,
        script_sha256=activation._script_digest(),
        stdout=(
            b"READY local Docker deployment "
            + b"runtime="
            + (b"e" * 40)
            + b" envelope="
            + (b"f" * 40)
            + b" web=sha256:"
            + (b"1" * 64)
            + b" render=sha256:"
            + (b"2" * 64)
            + b"\n"
            if ready
            else b""
        ),
        stderr=(
            b"" if ready else b"repository-role audit failed: worktree_not_clean\n"
        ),
        project="property",
        runtime_commit_sha="e" * 40 if ready else "",
        envelope_commit_sha=head_commit if ready else "",
        web_image_digest="sha256:" + ("1" * 64) if ready else "",
        render_image_digest="sha256:" + ("2" * 64) if ready else "",
        release_tree_identity={
            "release_tree_identity_verified": True,
            "release_head_commit_sha": head_commit,
            "release_worktree_status_sha256": (
                activation._sha256(b"")
                if ready
                else activation._sha256(b" M changed\x00")
            ),
            "release_worktree_status_bytes": 0 if ready else 11,
            "release_worktree_clean": ready,
            "changed_paths_recorded": False,
        },
        candidate_drop_posture=_candidate_drop_posture(),
    )


def test_preflight_observation_maps_dirty_worktree_without_raw_output(
    monkeypatch,
) -> None:
    monkeypatch.setattr(activation, "_script_digest", lambda: "d" * 64)
    monkeypatch.setattr(
        activation,
        "observe_candidate_drop_posture",
        lambda **_kwargs: _candidate_drop_posture(),
    )
    monkeypatch.setattr(
        activation,
        "_release_tree_identity",
        lambda: {
            "release_tree_identity_verified": True,
            "release_head_commit_sha": "f" * 40,
            "release_worktree_status_sha256": "a" * 64,
            "release_worktree_status_bytes": 42,
            "release_worktree_clean": False,
            "changed_paths_recorded": False,
        },
    )
    monkeypatch.setattr(
        activation.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=2,
            stdout=b"",
            stderr=b"property repository-role audit failed: worktree_not_clean\n",
        ),
    )

    observed = activation.observe_scheduler_activation_preflight(
        project="property",
        now=NOW,
    )

    assert observed["status"] == "blocked"
    assert observed["blocking_reason"] == "release_worktree_not_clean"
    assert "worktree_not_clean" not in observed.values()
    assert observed["stderr_bytes"] > 0
    assert observed["release_tree_identity_verified"] is True
    assert observed["release_head_commit_sha"] == "f" * 40
    assert observed["release_worktree_status_sha256"] == "a" * 64
    assert observed["release_worktree_status_bytes"] == 42
    assert observed["release_worktree_clean"] is False
    assert observed["changed_paths_recorded"] is False
    assert observed["deployment_or_restart_performed"] is False
    assert observed["provider_quota_consumed"] is False
    assert observed["delivery_attempted"] is False


def test_release_tree_identity_records_no_changed_paths(monkeypatch) -> None:
    changed_status = b" M state/private-operator-material\x00"

    def run(args, **_kwargs):
        if args[1] == "rev-parse":
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=(b"f" * 40) + b"\n",
                stderr=b"",
            )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=changed_status,
            stderr=b"",
        )

    monkeypatch.setattr(activation.subprocess, "run", run)

    identity = activation._release_tree_identity()

    assert identity == {
        "release_tree_identity_verified": True,
        "release_head_commit_sha": "f" * 40,
        "release_worktree_status_sha256": activation._sha256(changed_status),
        "release_worktree_status_bytes": len(changed_status),
        "release_worktree_clean": False,
        "changed_paths_recorded": False,
    }
    assert b"private-operator-material" not in activation._canonical(identity)


def test_candidate_drop_posture_proves_non_root_read_only_mount_without_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    drop = tmp_path / "operator-owned-public-candidate-drop"
    drop.mkdir(mode=0o750)
    drop.chmod(0o750)
    monkeypatch.setenv(activation.CANDIDATE_DROP_ENV, str(drop))
    monkeypatch.setenv(
        activation.CANDIDATE_DROP_GID_ENV,
        str(drop.stat().st_gid),
    )

    observed = activation.observe_candidate_drop_posture(now=NOW)

    assert observed["status"] == "ready"
    assert observed["candidate_drop_ready"] is True
    assert observed["directory_mode"] == "0750"
    assert observed["directory_uid"] == drop.stat().st_uid
    assert observed["directory_gid"] == drop.stat().st_gid
    assert observed["compose_read_only_bind_verified"] is True
    assert observed["compose_create_host_path_disabled"] is True
    assert observed["compose_supplemental_group_verified"] is True
    assert observed["scheduler_runtime_uid"] == 10001
    assert observed["scheduler_non_root_identity_verified"] is True
    assert observed["source_path_recorded"] is False
    assert observed["entry_names_recorded"] is False
    assert str(drop) not in activation._canonical(observed).decode("utf-8")
    assert observed["directory_created"] is False
    assert observed["directory_modified"] is False


def test_candidate_drop_posture_rejects_unsafe_mode_without_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    drop = tmp_path / "candidate-drop"
    drop.mkdir(mode=0o770)
    drop.chmod(0o770)
    monkeypatch.setenv(activation.CANDIDATE_DROP_ENV, str(drop))
    monkeypatch.setenv(
        activation.CANDIDATE_DROP_GID_ENV,
        str(drop.stat().st_gid),
    )

    observed = activation.observe_candidate_drop_posture(now=NOW)

    assert observed["status"] == "blocked"
    assert observed["blocking_reason"] == (
        "candidate_drop_directory_mode_not_admissible"
    )
    assert observed["directory_present"] is True
    assert observed["directory_type_verified"] is True
    assert observed["directory_group_verified"] is True
    assert observed["directory_permissions_verified"] is False
    assert observed["directory_mode"] == "0770"
    assert stat.S_IMODE(drop.stat().st_mode) == 0o770
    assert observed["directory_modified"] is False


def test_candidate_drop_blocker_short_circuits_deployment_preflight(
    monkeypatch,
) -> None:
    blocked_drop = activation._candidate_drop_observation(
        status="blocked",
        observed_at=NOW,
        blocking_reason="candidate_drop_directory_absent",
        source_path_sha256="3" * 64,
        configured_group_gid=1000,
    )
    monkeypatch.setattr(activation, "_script_digest", lambda: "d" * 64)
    monkeypatch.setattr(
        activation,
        "observe_candidate_drop_posture",
        lambda **_kwargs: blocked_drop,
    )
    monkeypatch.setattr(
        activation,
        "_release_tree_identity",
        lambda: {
            "release_tree_identity_verified": True,
            "release_head_commit_sha": "f" * 40,
            "release_worktree_status_sha256": "a" * 64,
            "release_worktree_status_bytes": 1,
            "release_worktree_clean": False,
            "changed_paths_recorded": False,
        },
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("deployment preflight must not run")

    monkeypatch.setattr(activation.subprocess, "run", forbidden)

    observed = activation.observe_scheduler_activation_preflight(
        project="property",
        now=NOW,
    )

    assert observed["status"] == "blocked"
    assert observed["blocking_reason"] == "candidate_drop_directory_absent"
    assert observed["candidate_drop"] == blocked_drop
    assert observed["deployment_or_restart_performed"] is False


def test_preflight_fails_closed_when_release_tree_identity_is_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(activation, "_script_digest", lambda: "d" * 64)
    monkeypatch.setattr(
        activation,
        "observe_candidate_drop_posture",
        lambda **_kwargs: _candidate_drop_posture(),
    )

    def unavailable():
        raise ValueError("scheduler_activation_release_tree_unavailable")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("deployment preflight must not run without tree identity")

    monkeypatch.setattr(activation, "_release_tree_identity", unavailable)
    monkeypatch.setattr(activation.subprocess, "run", forbidden)

    observed = activation.observe_scheduler_activation_preflight(
        project="property",
        now=NOW,
    )

    assert observed["status"] == "failed"
    assert observed["blocking_reason"] == "release_tree_identity_unavailable"
    assert observed["release_tree_identity_verified"] is False
    assert observed["release_head_commit_sha"] == ""
    assert observed["release_worktree_status_sha256"] == ""
    assert observed["changed_paths_recorded"] is False
    receipt = activation.build_scheduler_activation_readiness_receipt(
        scheduler_continuity=_continuity(),
        preflight_observation=observed,
        now=NOW,
    )
    assert receipt["status"] == "blocked"
    assert receipt["authorization_required"] is False
    assert receipt["deployment_or_restart_authorized"] is False


def test_blocked_preflight_is_current_evidence_not_operator_action() -> None:
    receipt = activation.build_scheduler_activation_readiness_receipt(
        scheduler_continuity=_continuity(),
        preflight_observation=_preflight(ready=False),
        now=NOW,
    )
    verified = activation.verify_scheduler_activation_readiness_receipt(
        receipt,
        scheduler_continuity=_continuity(),
        preflight_observation=_preflight(ready=False),
        now=NOW,
    )

    assert receipt["status"] == "blocked"
    assert receipt["blocking_reason"] == "release_worktree_not_clean"
    assert receipt["action_required"] is False
    assert receipt["interrupt_operator"] is False
    assert receipt["authorization_required"] is False
    assert receipt["execution_authorized"] is False
    assert receipt["deployment_or_restart_authorized"] is False
    assert verified["status"] == "verified"
    assert verified["readiness_state"] == "blocked"
    assert verified["progress"]["current_evidence_verified"] is True
    assert verified["progress"]["receipt_integrity_verified"] is True
    assert verified["progress"]["release_tree_identity_verified"] is True
    assert verified["progress"]["release_worktree_clean"] is False
    assert receipt["source_bindings"]["release_head_commit_sha"] == "f" * 40
    assert receipt["progress"]["candidate_drop_current"] is True
    assert receipt["progress"]["candidate_drop_ready"] is True
    assert receipt["source_bindings"][
        "candidate_drop_source_path_sha256"
    ] == "3" * 64


def test_ready_preflight_requires_separate_authorization_without_interrupt() -> None:
    receipt = activation.build_scheduler_activation_readiness_receipt(
        scheduler_continuity=_continuity(),
        preflight_observation=_preflight(ready=True),
        now=NOW,
    )

    assert receipt["status"] == "ready_for_authorization"
    assert receipt["action_required"] is True
    assert receipt["interrupt_operator"] is False
    assert receipt["authorization_required"] is True
    assert receipt["authorization_recorded"] is False
    assert receipt["scope"]["operation"] == (
        "authoritative_local_propertyquarry_deployment"
    )
    assert receipt["scope"]["requested_execution_mode"] == (
        "manual_after_separate_explicit_authorization"
    )
    assert receipt["automatic_execution_allowed"] is False
    assert receipt["deployment_or_restart_authorized"] is False
    assert receipt["deployment_or_restart_performed"] is False
    assert receipt["progress"][
        "candidate_drop_activation_prerequisite_verified"
    ] is True


def test_active_scheduler_skips_preflight_and_materializes_private_receipts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not run for active continuity")

    monkeypatch.setattr(activation.subprocess, "run", forbidden)
    receipt_path = tmp_path / "activation-readiness.json"
    verification_path = tmp_path / "activation-readiness-verification.json"

    verified = activation.materialize_current_scheduler_activation_readiness_bundle(
        scheduler_continuity=_continuity(active=True),
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["readiness_state"] == "not_required"
    assert verified["blocking_reason"] == "scheduler_continuity_active"
    assert verified["action_required"] is False
    assert verified["verification_receipt_persisted"] is True
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600


def test_readiness_rejects_stale_tampered_or_rebound_evidence() -> None:
    scheduler_continuity = _continuity()
    preflight = _preflight(ready=True)
    receipt = activation.build_scheduler_activation_readiness_receipt(
        scheduler_continuity=scheduler_continuity,
        preflight_observation=preflight,
        now=NOW,
    )

    stale = activation.verify_scheduler_activation_readiness_receipt(
        receipt,
        scheduler_continuity=scheduler_continuity,
        preflight_observation=preflight,
        now=NOW + timedelta(seconds=301),
    )
    assert stale["blocking_reason"] == (
        "scheduler_activation_readiness_receipt_not_fresh"
    )

    tampered = dict(receipt)
    tampered["deployment_or_restart_authorized"] = True
    normalized = dict(tampered)
    normalized.pop("integrity")
    tampered["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": activation._sha256(
            activation._canonical(normalized)
        ),
    }
    invalid = activation.verify_scheduler_activation_readiness_receipt(
        tampered,
        scheduler_continuity=scheduler_continuity,
        preflight_observation=preflight,
        now=NOW,
    )
    assert invalid["blocking_reason"] == (
        "scheduler_activation_readiness_source_binding_mismatch"
    )

    changed_continuity = dict(scheduler_continuity)
    changed_continuity["continuity_receipt_sha256"] = "9" * 64
    rebound = activation.verify_scheduler_activation_readiness_receipt(
        receipt,
        scheduler_continuity=changed_continuity,
        preflight_observation=preflight,
        now=NOW,
    )
    assert rebound["blocking_reason"] == (
        "scheduler_activation_readiness_source_binding_mismatch"
    )
    assert rebound["deployment_or_restart_authorized"] is False

    changed_preflight = dict(preflight)
    changed_preflight["preflight_script_sha256"] = "8" * 64
    wrong_script = activation.verify_scheduler_activation_readiness_receipt(
        receipt,
        scheduler_continuity=scheduler_continuity,
        preflight_observation=changed_preflight,
        now=NOW,
    )
    assert wrong_script["blocking_reason"] == (
        "scheduler_activation_readiness_source_not_admissible"
    )

    blocked_preflight = _preflight(ready=False)
    blocked_receipt = activation.build_scheduler_activation_readiness_receipt(
        scheduler_continuity=scheduler_continuity,
        preflight_observation=blocked_preflight,
        now=NOW,
    )
    changed_tree = dict(blocked_preflight)
    changed_tree["release_worktree_status_sha256"] = "7" * 64
    wrong_tree = activation.verify_scheduler_activation_readiness_receipt(
        blocked_receipt,
        scheduler_continuity=scheduler_continuity,
        preflight_observation=changed_tree,
        now=NOW,
    )
    assert wrong_tree["blocking_reason"] == (
        "scheduler_activation_readiness_source_binding_mismatch"
    )

    changed_drop = dict(preflight)
    changed_drop_row = dict(preflight["candidate_drop"])
    changed_drop_row["source_path_sha256"] = "8" * 64
    changed_drop["candidate_drop"] = changed_drop_row
    wrong_drop = activation.verify_scheduler_activation_readiness_receipt(
        receipt,
        scheduler_continuity=scheduler_continuity,
        preflight_observation=changed_drop,
        now=NOW,
    )
    assert wrong_drop["blocking_reason"] == (
        "scheduler_activation_readiness_source_binding_mismatch"
    )


def test_ready_preflight_rejects_dirty_or_wrong_head_tree_identity() -> None:
    scheduler_continuity = _continuity()
    dirty = _preflight(ready=True)
    dirty["release_worktree_clean"] = False
    dirty["release_worktree_status_bytes"] = 1
    dirty["release_worktree_status_sha256"] = "7" * 64

    try:
        activation.build_scheduler_activation_readiness_receipt(
            scheduler_continuity=scheduler_continuity,
            preflight_observation=dirty,
            now=NOW,
        )
    except ValueError as exc:
        assert str(exc) == "scheduler_activation_preflight_not_admissible"
    else:
        raise AssertionError("dirty ready preflight must fail closed")

    wrong_head = _preflight(ready=True)
    wrong_head["release_head_commit_sha"] = "9" * 40
    try:
        activation.build_scheduler_activation_readiness_receipt(
            scheduler_continuity=scheduler_continuity,
            preflight_observation=wrong_head,
            now=NOW,
        )
    except ValueError as exc:
        assert str(exc) == "scheduler_activation_preflight_not_admissible"
    else:
        raise AssertionError("wrong HEAD ready preflight must fail closed")


def test_operator_summary_projects_only_bound_path_free_release_identity() -> None:
    operator_summary = (activation.ROOT / "scripts/operator_summary.sh").read_text(
        encoding="utf-8"
    )

    assert (
        'activation_source.get("release_tree_identity_verified") is True'
        in operator_summary
    )
    assert (
        'activation_source.get("changed_paths_recorded") is False'
        in operator_summary
    )
    assert (
        'activation_bindings.get("release_head_commit_sha")'
        in operator_summary
    )
    assert (
        'activation_bindings.get("release_worktree_status_sha256")'
        in operator_summary
    )
    assert '" paths=not-recorded"' in operator_summary
    assert "activation release:  UNAVAILABLE (no verified tree identity)" in (
        operator_summary
    )
    assert "candidate_drop_binding_verified = bool(" in operator_summary
    assert "activation candidate drop: " in operator_summary
    assert "activation candidate bind: " in operator_summary
    assert "paths=not-recorded entries=not-recorded" in operator_summary
