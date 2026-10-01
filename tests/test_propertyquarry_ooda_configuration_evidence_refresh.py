from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts import propertyquarry_ooda_configuration_action_status as action_status
from scripts import propertyquarry_ooda_configuration_evidence_refresh as refresh


NOW = datetime(2026, 8, 26, 14, 2, tzinfo=timezone.utc)
ACTION_ID = "pqme_" + "1" * 24
ACTION_SHA256 = "2" * 64
SOURCE_SHA256 = "3" * 64
RUNTIME_SHA256 = "4" * 64
WORKTREE_SHA256 = "5" * 64
HEAD_SHA = "6" * 40


def _write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(mode)
    return path


def _action() -> dict[str, Any]:
    return {
        "schema": action_status.SCHEMA,
        "status": "action_required",
        "state": "applied",
        "updated_at": NOW.isoformat(),
        "blocking_reason": "",
        "next_action": "refresh",
        "action_required": True,
        "interrupt_operator": True,
        "current_evidence_verified": True,
        "receipt_verified": True,
        "source_state_verified": True,
        "receipt_id": ACTION_ID,
        "receipt_sha256": ACTION_SHA256,
        "source_observed_sha256": SOURCE_SHA256,
        "automatic_execution_allowed": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
    }


def _paths(tmp_path: Path) -> dict[str, Path]:
    source_dir = tmp_path / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    release_manifest_path = source_dir / "PROPERTYQUARRY_RELEASE_MANIFEST.md"
    release_manifest_path.write_text(
        "# PropertyQuarry release authority\n\nOpaque signed authority content.\n",
        encoding="utf-8",
    )
    release_manifest_path.chmod(0o644)
    paths = {
        "gold_receipt_path": _write_json(source_dir / "gold.json", {"source": "gold"}),
        "scene_packet_path": _write_json(source_dir / "scene-packet.json", {"source": "packet"}),
        "scene_verifier_path": _write_json(
            source_dir / "scene-verifier.json", {"source": "verifier"}
        ),
        "scene_runtime_status_path": _write_json(
            source_dir / "scene-runtime.json", {"source": "runtime"}
        ),
        "live_mobile_receipt_path": _write_json(
            source_dir / "live-mobile.json", {"source": "live"}
        ),
        "release_manifest_path": release_manifest_path,
        "signal_dir": tmp_path / "signals",
        "stage_receipt_path": tmp_path / "cycle" / "stage.json",
        "cycle_receipt_path": tmp_path / "cycle" / "latest.json",
        "cycle_state_path": tmp_path / "cycle" / "state.json",
        "cycle_lock_path": tmp_path / "cycle" / "send.lock",
        "review_packet_path": tmp_path / "cycle" / "review.json",
        "review_verification_path": tmp_path / "cycle" / "review-verification.json",
        "action_receipt_dir": tmp_path / "actions",
        "action_handoff_dir": tmp_path / "handoffs",
        "refresh_receipt_dir": tmp_path / "refresh-receipts",
        "refresh_lock_path": tmp_path / "refresh.lock",
    }
    return paths


def _install_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        refresh.action_status,
        "inspect_manual_action_status",
        lambda **_kwargs: _action(),
    )
    monkeypatch.setattr(
        refresh.runtime_review,
        "_observe_runtime",
        lambda **_kwargs: {
            "query_status": "pass",
            "project": "property",
            "container_count": 1,
            "fingerprint_sha256": RUNTIME_SHA256,
        },
    )
    monkeypatch.setattr(
        refresh.runtime_review,
        "_release_posture",
        lambda **_kwargs: {
            "head_sha": HEAD_SHA,
            "worktree_clean": False,
            "changed_path_count": 3,
            "worktree_fingerprint_sha256": WORKTREE_SHA256,
        },
    )


def _install_success_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    paths: dict[str, Path],
    calls: dict[str, int],
) -> None:
    def stage_signals(**kwargs: Any) -> dict[str, Any]:
        calls["stage"] += 1
        target = Path(kwargs["target_dir"])
        source_evidence: dict[str, dict[str, Any]] = {}
        source_paths = {
            "gold_receipt": Path(kwargs["gold_receipt_path"]),
            "public_origin_observation": Path(
                kwargs["public_origin_observation_path"]
            ),
            "scene_packet": Path(kwargs["scene_packet_path"]),
            "scene_verifier": Path(kwargs["scene_verifier_path"]),
            "scene_runtime_status": Path(kwargs["scene_runtime_status_path"]),
        }
        for name, source_path in source_paths.items():
            _payload, source_evidence[name] = refresh.signal_stage._load_source(
                source_path,
                label=name,
            )
            _write_json(
                target / refresh.approved.SIGNAL_FILENAMES[name],
                {"sanitized": name},
                mode=0o644,
            )
        _write_json(target / "manifest.json", {"approved": True}, mode=0o644)
        report = {
            "schema": refresh.signal_stage.SCHEMA,
            "status": "staged",
            "publication_verified": True,
            "source_evidence": source_evidence,
            "automatic_execution_allowed": False,
            "provider_quota_consumption_allowed": False,
            "protected_operation_executed": False,
        }
        _write_json(Path(kwargs["receipt_path"]), report)
        return report

    def run_cycle(**kwargs: Any) -> dict[str, Any]:
        calls["cycle"] += 1
        assert kwargs["send"] is False
        assert kwargs["require_approval_manifest"] is True
        report = {
            "schema": refresh.notification_cycle.SCHEMA,
            "status": "action_required",
            "execution_mode": "evaluate_only",
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "automatic_execution_allowed": False,
            "provider_quota_consumption_allowed": False,
            "protected_operation_executed": False,
            "signal_approval": {"approved": True},
        }
        _write_json(Path(kwargs["write"]), report)
        return report

    live_sha256 = refresh._json_evidence(
        paths["live_mobile_receipt_path"],
        field="test_live",
        maximum_bytes=refresh.MAX_INPUT_BYTES,
    )["sha256"]
    release_sha256 = refresh._file_evidence(
        paths["release_manifest_path"],
        field="test_release",
        maximum_bytes=refresh.MAX_INPUT_BYTES,
    )["sha256"]

    def materialize_review(**kwargs: Any) -> dict[str, Any]:
        calls["review"] += 1
        packet = {
            "schema": refresh.runtime_review.SCHEMA,
            "runtime_posture": {"fingerprint_sha256": RUNTIME_SHA256},
            "release_posture": {
                "worktree_fingerprint_sha256": WORKTREE_SHA256
            },
            "probe_posture": {"source_sha256": live_sha256},
            "release_authority": {"source_sha256": release_sha256},
        }
        _write_json(Path(kwargs["write_path"]), packet)
        return packet

    def verify_review(**_kwargs: Any) -> dict[str, Any]:
        return {
            "schema": refresh.runtime_review.VERIFY_SCHEMA,
            "status": "verified",
            "updated_at": NOW.isoformat(),
            "blocking_reason": "live_runtime_host_admission_rejected",
            "progress": {"current_evidence_verified": True},
            "execution_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        }

    monkeypatch.setattr(refresh.signal_stage, "stage_approved_signals", stage_signals)
    monkeypatch.setattr(refresh.notification_cycle, "run_cycle_once", run_cycle)
    monkeypatch.setattr(
        refresh.runtime_review,
        "materialize_current_review_packet",
        materialize_review,
    )
    monkeypatch.setattr(
        refresh.runtime_review,
        "verify_current_review_packet",
        verify_review,
    )


def _execute(paths: dict[str, Path], **overrides: Any) -> dict[str, Any]:
    return refresh.execute_evidence_refresh(
        expected_action_receipt_id=str(
            overrides.pop("expected_action_receipt_id", ACTION_ID)
        ),
        expected_action_receipt_sha256=str(
            overrides.pop("expected_action_receipt_sha256", ACTION_SHA256)
        ),
        root=Path(overrides.pop("root", paths["action_receipt_dir"].parent)),
        now=overrides.pop("now", NOW),
        project="property",
        **paths,
        **overrides,
    )


def test_exact_refresh_is_evaluate_only_private_and_replay_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    calls = {"stage": 0, "cycle": 0, "review": 0}
    _install_preflight(monkeypatch)
    _install_success_pipeline(monkeypatch, paths, calls)

    completed = _execute(paths)
    receipt_path = Path(completed["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    original = receipt_path.read_bytes()
    original_mtime_ns = receipt_path.stat().st_mtime_ns
    replayed = _execute(paths)

    assert completed["status"] == "refreshed"
    assert completed["source_edit_performed"] is False
    assert completed["deployment_or_restart_performed"] is False
    assert completed["provider_quota_consumed"] is False
    assert completed["delivery_attempted"] is False
    assert receipt["status"] == "completed"
    assert receipt["binding"]["action_receipt_id"] == ACTION_ID
    assert receipt["binding"]["action_receipt_sha256"] == ACTION_SHA256
    assert set(receipt["binding"]["approved_sources"]) == set(
        refresh.approved.SIGNAL_FILENAMES
    )
    assert receipt["binding"]["approved_sources"][
        "public_origin_observation"
    ]["sha256"] == receipt["binding"]["live_mobile_receipt"]["sha256"]
    assert receipt["safety"] == refresh._SAFETY
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert replayed["status"] == "unchanged"
    assert calls == {"stage": 1, "cycle": 1, "review": 1}
    assert receipt_path.read_bytes() == original
    assert receipt_path.stat().st_mtime_ns == original_mtime_ns


def test_failed_safe_refresh_can_reconcile_without_any_protected_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    calls = {"stage": 0, "cycle": 0, "review": 0}
    _install_preflight(monkeypatch)

    def fail_stage(**_kwargs: Any) -> dict[str, Any]:
        calls["stage"] += 1
        raise OSError("simulated safe local failure")

    monkeypatch.setattr(refresh.signal_stage, "stage_approved_signals", fail_stage)
    failed = _execute(paths)
    failed_receipt = json.loads(Path(failed["receipt_path"]).read_text())

    _install_success_pipeline(monkeypatch, paths, calls)
    reconciled = _execute(paths)

    assert failed["status"] == "blocked"
    assert failed["blocking_reason"] == "evidence_refresh_stage_failed"
    assert failed_receipt["status"] == "failed"
    assert failed_receipt["safety"] == refresh._SAFETY
    assert reconciled["status"] == "refreshed"
    assert calls == {"stage": 2, "cycle": 1, "review": 1}


def test_prepared_receipt_reconciles_after_finalize_crash_without_claiming_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    calls = {"stage": 0, "cycle": 0, "review": 0}
    _install_preflight(monkeypatch)
    _install_success_pipeline(monkeypatch, paths, calls)
    real_atomic_write = refresh.atomic_write_bytes

    def fail_completed_receipt(
        path: Path,
        payload: bytes,
        *,
        overwrite: bool,
    ) -> None:
        decoded = json.loads(payload)
        if (
            Path(path).parent == paths["refresh_receipt_dir"]
            and decoded.get("status") == "completed"
        ):
            raise OSError("simulated finalize crash")
        real_atomic_write(path, payload, overwrite=overwrite)

    monkeypatch.setattr(refresh, "atomic_write_bytes", fail_completed_receipt)
    interrupted = _execute(paths)
    receipt_path = Path(interrupted["receipt_path"])
    prepared = json.loads(receipt_path.read_text())
    inspected = refresh.inspect_latest_evidence_refresh(
        action_receipt_id=ACTION_ID,
        action_receipt_sha256=ACTION_SHA256,
        receipt_dir=paths["refresh_receipt_dir"],
        root=tmp_path,
        now=NOW,
    )

    monkeypatch.setattr(refresh, "atomic_write_bytes", real_atomic_write)
    reconciled = _execute(paths)

    assert interrupted["status"] == "blocked"
    assert interrupted["blocking_reason"] == (
        "evidence_refresh_finalize_receipt_write_failed"
    )
    assert prepared["status"] == "prepared"
    assert inspected["status"] == "prepared"
    assert reconciled["status"] == "refreshed"
    assert calls == {"stage": 2, "cycle": 2, "review": 2}
    assert json.loads(receipt_path.read_text())["status"] == "completed"


def test_wrong_action_binding_is_quiet_and_creates_no_refresh_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    _install_preflight(monkeypatch)

    blocked = _execute(
        paths,
        expected_action_receipt_id="pqme_" + "9" * 24,
    )

    assert blocked["status"] == "blocked"
    assert blocked["blocking_reason"] == "evidence_refresh_preflight_not_admissible"
    assert not paths["refresh_receipt_dir"].exists()
    assert not paths["refresh_lock_path"].exists()


def test_recomputed_receipt_contract_tamper_fails_closed_without_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    calls = {"stage": 0, "cycle": 0, "review": 0}
    _install_preflight(monkeypatch)
    _install_success_pipeline(monkeypatch, paths, calls)
    completed = _execute(paths)
    receipt_path = Path(completed["receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    nested_tamper = json.loads(json.dumps(receipt))
    nested_tamper["binding"]["runtime_posture"]["container_count"] = "one"
    nested_tamper["refresh_id"] = refresh._refresh_id(nested_tamper["binding"])
    nested_tamper.pop("integrity")
    nested_tamper["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": refresh._sha256(
            refresh._canonical(nested_tamper)
        ),
    }
    with pytest.raises(ValueError, match="evidence_refresh_receipt_not_admissible"):
        refresh._receipt_envelope(nested_tamper)

    receipt["unauthorized_field"] = "deploy"
    receipt.pop("integrity")
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": refresh._sha256(refresh._canonical(receipt)),
    }
    receipt_path.write_bytes(refresh._canonical(receipt))
    receipt_path.chmod(0o600)

    blocked = _execute(paths)

    assert blocked["status"] == "blocked"
    assert blocked["blocking_reason"] == "evidence_refresh_existing_receipt_not_admissible"
    assert calls == {"stage": 1, "cycle": 1, "review": 1}


def test_refresh_inspection_distinguishes_completed_stale_and_tampered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    calls = {"stage": 0, "cycle": 0, "review": 0}
    _install_preflight(monkeypatch)
    _install_success_pipeline(monkeypatch, paths, calls)
    completed = _execute(paths)

    current = refresh.inspect_latest_evidence_refresh(
        action_receipt_id=ACTION_ID,
        action_receipt_sha256=ACTION_SHA256,
        receipt_dir=paths["refresh_receipt_dir"],
        root=tmp_path,
        now=NOW,
    )
    stale = refresh.inspect_latest_evidence_refresh(
        action_receipt_id=ACTION_ID,
        action_receipt_sha256=ACTION_SHA256,
        receipt_dir=paths["refresh_receipt_dir"],
        root=tmp_path,
        now=NOW + timedelta(seconds=1801),
    )
    Path(completed["receipt_path"]).write_text("{}", encoding="utf-8")
    Path(completed["receipt_path"]).chmod(0o600)
    tampered = refresh.inspect_latest_evidence_refresh(
        action_receipt_id=ACTION_ID,
        action_receipt_sha256=ACTION_SHA256,
        receipt_dir=paths["refresh_receipt_dir"],
        root=tmp_path,
        now=NOW,
    )

    assert current["status"] == "completed"
    assert stale["status"] == "stale"
    assert stale["blocking_reason"] == "evidence_refresh_receipt_not_fresh"
    assert tampered["status"] == "blocked"
    assert tampered["blocking_reason"] == "evidence_refresh_receipt_chain_not_admissible"


def test_action_projection_resolves_completed_refresh_and_retries_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {
        **_action(),
        "receipt_path": str(tmp_path / "apply.json"),
        "rollback_command": "python3 manual.py --rollback",
    }
    completed_refresh = {
        "status": "completed",
        "updated_at": NOW.isoformat(),
        "blocking_reason": "",
        "next_action": "review fresh posture",
        "refresh_id": "pqer_" + "7" * 24,
        "receipt_path": str(tmp_path / "refresh.json"),
        "receipt_sha256": "8" * 64,
        "prepared_at": NOW.isoformat(),
        "completed_at": NOW.isoformat(),
        **refresh._SAFETY,
    }
    monkeypatch.setattr(
        refresh,
        "inspect_latest_evidence_refresh",
        lambda **_kwargs: dict(completed_refresh),
    )

    resolved = action_status._with_evidence_refresh(
        dict(base),
        receipt_dir=tmp_path / "refreshes",
        root=tmp_path,
        now=NOW,
    )
    failed_refresh = {
        **completed_refresh,
        "status": "failed",
        "blocking_reason": "evidence_refresh_review_failed",
    }
    monkeypatch.setattr(
        refresh,
        "inspect_latest_evidence_refresh",
        lambda **_kwargs: dict(failed_refresh),
    )
    retry = action_status._with_evidence_refresh(
        dict(base),
        receipt_dir=tmp_path / "refreshes",
        root=tmp_path,
        now=NOW,
    )

    assert resolved["state"] == "applied_evidence_refreshed"
    assert resolved["action_required"] is False
    assert resolved["interrupt_operator"] is False
    assert "action_command" not in resolved
    assert resolved["rollback_command"].endswith("--rollback")
    assert retry["state"] == "evidence_refresh_failed"
    assert retry["action_required"] is True
    assert "--refresh" in retry["action_command"]
    assert retry["deployment_or_restart_performed"] is False
