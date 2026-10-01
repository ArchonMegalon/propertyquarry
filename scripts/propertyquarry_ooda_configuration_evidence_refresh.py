#!/usr/bin/env python3
"""Refresh the approved evaluate-only OODA chain after a manual source action.

The refresh is bound to one verified apply or rollback receipt and is deliberately
incapable of deploying, restarting, calling a provider, or delivering a message.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_configuration_action_status as action_status
from scripts import propertyquarry_ooda_configuration_manual_action as manual_action
from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts import propertyquarry_stage_ooda_signals as signal_stage
from scripts.propertyquarry_secure_file_io import atomic_write_bytes, read_stable_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_runtime_configuration_evidence_refresh.v1"
RESULT_SCHEMA = "propertyquarry.ooda_runtime_configuration_evidence_refresh_result.v1"
DEFAULT_RECEIPT_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "runtime-configuration-evidence-refresh-receipts"
)
DEFAULT_LOCK_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "runtime-configuration-evidence-refresh.lock"
)
DEFAULT_STAGE_RECEIPT = Path(signal_stage.DEFAULT_RECEIPT_PATH)
DEFAULT_SIGNAL_DIR = Path(signal_stage.DEFAULT_TARGET_DIR)
DEFAULT_CYCLE_RECEIPT = Path(notification_cycle.DEFAULT_RECEIPT_PATH)
DEFAULT_CYCLE_STATE = Path(notification_cycle.DEFAULT_STATE_PATH)
DEFAULT_CYCLE_LOCK = Path(notification_cycle.DEFAULT_LOCK_PATH)
DEFAULT_REVIEW_PACKET = runtime_review.DEFAULT_PACKET_PATH
DEFAULT_REVIEW_VERIFICATION = runtime_review.DEFAULT_VERIFICATION_PATH
MAX_RECEIPT_BYTES = 512 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_COUNT = 64
REFRESH_WINDOW_SECONDS = int(runtime_review.DEFAULT_MAX_AGE_SECONDS)
_REFRESH_ID = re.compile(r"pqer_[0-9a-f]{24}\Z")
_ACTION_ID = re.compile(r"pqm[er]_[0-9a-f]{24}\Z")
_SAFETY = {
    "automatic_execution_allowed": False,
    "source_edit_performed": False,
    "deployment_or_restart_authorized": False,
    "deployment_or_restart_performed": False,
    "provider_quota_consumption_allowed": False,
    "provider_quota_consumed": False,
    "delivery_authorized": False,
    "delivery_attempted": False,
    "protected_operation_executed": False,
}


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return runtime_review._canonical(dict(value))


def _sha256(value: bytes) -> str:
    return runtime_review._sha256(value)


def _parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _rooted(path: Path, *, root: Path) -> Path:
    expanded = Path(path).expanduser()
    return expanded if expanded.is_absolute() else root / expanded


def _window_started_at(now: datetime) -> str:
    timestamp = now.timestamp()
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("evidence_refresh_clock_not_admissible")
    start = int(timestamp // REFRESH_WINDOW_SECONDS) * REFRESH_WINDOW_SECONDS
    return datetime.fromtimestamp(start, tz=timezone.utc).isoformat()


def evidence_refresh_command(*, action_receipt_id: str, action_receipt_sha256: str) -> str:
    return (
        "python3 scripts/propertyquarry_ooda_configuration_evidence_refresh.py "
        "--refresh "
        f"--action-receipt-id {action_receipt_id} "
        f"--action-receipt-sha256 {action_receipt_sha256}"
    )


def _result(
    status: str,
    *,
    now: datetime,
    blocking_reason: str = "",
    next_action: str,
    refresh_id: str = "",
    receipt_path: Path | None = None,
    receipt_sha256: str = "",
    receipt_persisted: bool = False,
) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "status": status,
        "updated_at": now.isoformat(),
        "blocking_reason": blocking_reason,
        "next_action": next_action,
        "refresh_id": refresh_id,
        "receipt_path": str(receipt_path or ""),
        "receipt_sha256": receipt_sha256,
        "receipt_persisted": receipt_persisted,
        **_SAFETY,
    }


def _blocked(
    reason: str,
    *,
    now: datetime,
    next_action: str = (
        "inspect the exact action receipt and evaluate-only refresh evidence; "
        "do not deploy, restart, call providers, or send"
    ),
    refresh_id: str = "",
    receipt_path: Path | None = None,
    receipt_sha256: str = "",
    receipt_persisted: bool = False,
) -> dict[str, Any]:
    return _result(
        "blocked",
        now=now,
        blocking_reason=reason,
        next_action=next_action,
        refresh_id=refresh_id,
        receipt_path=receipt_path,
        receipt_sha256=receipt_sha256,
        receipt_persisted=receipt_persisted,
    )


def _json_evidence(
    path: Path,
    *,
    field: str,
    maximum_bytes: int,
) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"{field}_not_admissible") from exc
    if not (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid in {0, os.geteuid()}
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) & 0o022 == 0
    ):
        raise ValueError(f"{field}_not_admissible")
    _payload, raw, digest = load_strict_json_object_snapshot(
        path,
        field=field,
        maximum_bytes=maximum_bytes,
    )
    return {
        "path": str(path),
        "sha256": digest,
        "bytes": len(raw),
    }


def _file_evidence(
    path: Path,
    *,
    field: str,
    maximum_bytes: int,
) -> dict[str, Any]:
    try:
        raw = read_stable_bytes(
            path,
            maximum_bytes=maximum_bytes,
            require_nonempty=True,
        )
    except Exception as exc:
        raise ValueError(f"{field}_not_admissible") from exc
    return {
        "path": str(path),
        "sha256": _sha256(raw),
        "bytes": len(raw),
    }


def _stable_runtime_binding(
    *,
    project: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime = runtime_review._observe_runtime(project=project, now=now)
    release = runtime_review._release_posture(now=now)
    if not (
        runtime.get("query_status") == "pass"
        and runtime_review._SHA256.fullmatch(
            str(runtime.get("fingerprint_sha256") or "")
        )
        and runtime_review._SHA256.fullmatch(
            str(release.get("worktree_fingerprint_sha256") or "")
        )
        and re.fullmatch(r"[0-9a-f]{40}", str(release.get("head_sha") or ""))
    ):
        raise ValueError("evidence_refresh_runtime_posture_not_admissible")
    return (
        {
            "project": str(runtime.get("project") or project),
            "container_count": int(runtime.get("container_count") or 0),
            "fingerprint_sha256": str(runtime.get("fingerprint_sha256") or ""),
        },
        {
            "head_sha": str(release.get("head_sha") or ""),
            "worktree_clean": release.get("worktree_clean") is True,
            "changed_path_count": int(release.get("changed_path_count") or 0),
            "worktree_fingerprint_sha256": str(
                release.get("worktree_fingerprint_sha256") or ""
            ),
        },
    )


def _refresh_binding(
    *,
    expected_action_receipt_id: str,
    expected_action_receipt_sha256: str,
    action_receipt_dir: Path,
    action_handoff_dir: Path,
    source_root: Path,
    gold_receipt_path: Path,
    scene_packet_path: Path,
    scene_verifier_path: Path,
    scene_runtime_status_path: Path,
    live_mobile_receipt_path: Path,
    release_manifest_path: Path,
    project: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not (
        _ACTION_ID.fullmatch(expected_action_receipt_id)
        and runtime_review._SHA256.fullmatch(expected_action_receipt_sha256)
    ):
        raise ValueError("evidence_refresh_action_binding_not_admissible")
    current_action = action_status.inspect_manual_action_status(
        receipt_dir=action_receipt_dir,
        handoff_dir=action_handoff_dir,
        root=source_root,
        now=now,
        include_evidence_refresh=False,
    )
    if not (
        current_action.get("status") in {"action_required", "ready"}
        and current_action.get("state") in {"applied", "rolled_back"}
        and current_action.get("receipt_verified") is True
        and current_action.get("source_state_verified") is True
        and current_action.get("current_evidence_verified") is True
        and current_action.get("receipt_id") == expected_action_receipt_id
        and current_action.get("receipt_sha256")
        == expected_action_receipt_sha256
        and runtime_review._SHA256.fullmatch(
            str(current_action.get("source_observed_sha256") or "")
        )
    ):
        raise ValueError("evidence_refresh_current_action_not_admissible")

    source_paths = {
        "gold_receipt": gold_receipt_path,
        "public_origin_observation": live_mobile_receipt_path,
        "scene_packet": scene_packet_path,
        "scene_verifier": scene_verifier_path,
        "scene_runtime_status": scene_runtime_status_path,
    }
    approved_sources: dict[str, dict[str, Any]] = {}
    for name, path in source_paths.items():
        _payload, approved_sources[name] = signal_stage._load_source(path, label=name)
    runtime_binding, release_binding = _stable_runtime_binding(
        project=project,
        now=now,
    )
    binding = {
        "action_receipt_id": expected_action_receipt_id,
        "action_receipt_sha256": expected_action_receipt_sha256,
        "action_state": str(current_action.get("state") or ""),
        "source_observed_sha256": str(
            current_action.get("source_observed_sha256") or ""
        ),
        "window_started_at": _window_started_at(now),
        "approved_sources": approved_sources,
        "live_mobile_receipt": _json_evidence(
            live_mobile_receipt_path,
            field="evidence_refresh_live_mobile_receipt",
            maximum_bytes=runtime_review.MAX_LIVE_MOBILE_RECEIPT_BYTES,
        ),
        "release_manifest": _file_evidence(
            release_manifest_path,
            field="evidence_refresh_release_manifest",
            maximum_bytes=runtime_review.MAX_RELEASE_MANIFEST_BYTES,
        ),
        "runtime_posture": runtime_binding,
        "release_posture": release_binding,
    }
    return current_action, binding


def _refresh_id(binding: Mapping[str, Any]) -> str:
    return f"pqer_{_sha256(_canonical(binding))[:24]}"


def _receipt_path(receipt_dir: Path, refresh_id: str, *, root: Path) -> Path:
    if _REFRESH_ID.fullmatch(refresh_id) is None:
        raise ValueError("evidence_refresh_id_not_admissible")
    return _rooted(receipt_dir, root=root) / f"{refresh_id}.json"


def _receipt_document(
    *,
    refresh_id: str,
    status: str,
    prepared_at: datetime,
    binding: Mapping[str, Any],
    outputs: Mapping[str, Any] | None = None,
    completed_at: datetime | None = None,
    blocking_reason: str = "",
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "refresh_id": refresh_id,
        "status": status,
        "prepared_at": prepared_at.isoformat(),
        "completed_at": completed_at.isoformat() if completed_at is not None else "",
        "blocking_reason": blocking_reason,
        "binding": dict(binding),
        "outputs": dict(outputs or {}),
        "safety": dict(_SAFETY),
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(receipt)),
    }
    return receipt


def _strict_count(value: object, *, maximum: int = 1_000_000) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value <= maximum
    )


def _evidence_admissible(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "bytes"}:
        return False
    path = Path(str(value.get("path") or ""))
    return (
        path.is_absolute()
        and path.name not in {"", ".", ".."}
        and runtime_review._SHA256.fullmatch(str(value.get("sha256") or ""))
        is not None
        and _strict_count(value.get("bytes"), maximum=MAX_INPUT_BYTES)
    )


def _binding_admissible(binding: object) -> bool:
    if not isinstance(binding, Mapping) or set(binding) != {
        "action_receipt_id",
        "action_receipt_sha256",
        "action_state",
        "source_observed_sha256",
        "window_started_at",
        "approved_sources",
        "live_mobile_receipt",
        "release_manifest",
        "runtime_posture",
        "release_posture",
    }:
        return False
    window_started_at = _parse_timestamp(binding.get("window_started_at"))
    approved_sources = binding.get("approved_sources")
    runtime = binding.get("runtime_posture")
    release = binding.get("release_posture")
    if not (
        _ACTION_ID.fullmatch(str(binding.get("action_receipt_id") or ""))
        and runtime_review._SHA256.fullmatch(
            str(binding.get("action_receipt_sha256") or "")
        )
        and binding.get("action_state") in {"applied", "rolled_back"}
        and runtime_review._SHA256.fullmatch(
            str(binding.get("source_observed_sha256") or "")
        )
        and window_started_at is not None
        and int(window_started_at.timestamp()) % REFRESH_WINDOW_SECONDS == 0
        and isinstance(approved_sources, Mapping)
        and set(approved_sources) == set(approved.SIGNAL_FILENAMES)
        and all(_evidence_admissible(value) for value in approved_sources.values())
        and _evidence_admissible(binding.get("live_mobile_receipt"))
        and _evidence_admissible(binding.get("release_manifest"))
        and isinstance(runtime, Mapping)
        and set(runtime)
        == {"project", "container_count", "fingerprint_sha256"}
        and re.fullmatch(
            r"[a-z0-9][a-z0-9_.-]{0,62}",
            str(runtime.get("project") or ""),
        )
        and _strict_count(runtime.get("container_count"), maximum=10_000)
        and runtime_review._SHA256.fullmatch(
            str(runtime.get("fingerprint_sha256") or "")
        )
        and isinstance(release, Mapping)
        and set(release)
        == {
            "head_sha",
            "worktree_clean",
            "changed_path_count",
            "worktree_fingerprint_sha256",
        }
        and re.fullmatch(r"[0-9a-f]{40}", str(release.get("head_sha") or ""))
        and isinstance(release.get("worktree_clean"), bool)
        and _strict_count(release.get("changed_path_count"))
        and runtime_review._SHA256.fullmatch(
            str(release.get("worktree_fingerprint_sha256") or "")
        )
    ):
        return False
    return True


def _outputs_admissible(outputs: object, *, status: str) -> bool:
    if not isinstance(outputs, Mapping):
        return False
    allowed = {
        "approved_signal_stage",
        "approval_manifest",
        "notification_cycle",
        "runtime_review_packet",
        "runtime_review_verification",
    }
    if status == "prepared":
        return not outputs
    if status == "completed" and set(outputs) != allowed:
        return False
    if status == "failed" and not set(outputs).issubset(allowed):
        return False
    for name, value in outputs.items():
        if name == "runtime_review_verification":
            if not (
                isinstance(value, Mapping)
                and set(value)
                == {
                    "path",
                    "sha256",
                    "bytes",
                    "status",
                    "current_evidence_verified",
                }
                and _evidence_admissible(
                    {
                        "path": value.get("path"),
                        "sha256": value.get("sha256"),
                        "bytes": value.get("bytes"),
                    }
                )
                and value.get("status") == "verified"
                and value.get("current_evidence_verified") is True
            ):
                return False
        elif not _evidence_admissible(value):
            return False
    return True


def _receipt_envelope(
    receipt: Mapping[str, Any],
) -> tuple[str, str, datetime, datetime | None, dict[str, Any], str]:
    normalized = dict(receipt)
    integrity = normalized.pop("integrity", None)
    refresh_id = str(receipt.get("refresh_id") or "")
    status = str(receipt.get("status") or "")
    prepared_at = _parse_timestamp(receipt.get("prepared_at"))
    completed_at = _parse_timestamp(receipt.get("completed_at"))
    binding = receipt.get("binding")
    outputs = receipt.get("outputs")
    if not (
        receipt.get("schema") == SCHEMA
        and set(receipt)
        == {
            "schema",
            "refresh_id",
            "status",
            "prepared_at",
            "completed_at",
            "blocking_reason",
            "binding",
            "outputs",
            "safety",
            "integrity",
        }
        and _REFRESH_ID.fullmatch(refresh_id)
        and status in {"prepared", "completed", "failed"}
        and prepared_at is not None
        and _binding_admissible(binding)
        and _outputs_admissible(outputs, status=status)
        and receipt.get("safety") == _SAFETY
        and isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
        and refresh_id == _refresh_id(binding)
    ):
        raise ValueError("evidence_refresh_receipt_not_admissible")
    if status == "prepared" and (
        completed_at is not None or receipt.get("blocking_reason") or outputs
    ):
        raise ValueError("evidence_refresh_prepared_receipt_not_admissible")
    if status == "completed" and not (
        completed_at is not None
        and completed_at >= prepared_at
        and not receipt.get("blocking_reason")
    ):
        raise ValueError("evidence_refresh_completed_receipt_not_admissible")
    if status == "failed" and not (
        completed_at is not None
        and completed_at >= prepared_at
        and str(receipt.get("blocking_reason") or "").startswith(
            "evidence_refresh_"
        )
    ):
        raise ValueError("evidence_refresh_failed_receipt_not_admissible")
    return (
        refresh_id,
        status,
        prepared_at,
        completed_at,
        dict(binding),
        _sha256(_canonical(receipt)),
    )


def _load_receipt(path: Path) -> tuple[dict[str, Any], str]:
    receipt, _raw, digest = manual_action._private_object(
        path,
        field="runtime_configuration_evidence_refresh_receipt",
        maximum_bytes=MAX_RECEIPT_BYTES,
    )
    _refresh_id_value, _status, _prepared, _completed, _binding, verified = (
        _receipt_envelope(receipt)
    )
    if digest != verified or path.stem != receipt.get("refresh_id"):
        raise ValueError("evidence_refresh_receipt_binding_mismatch")
    return receipt, digest


def _private_receipt_paths(receipt_dir: Path, *, root: Path) -> list[Path]:
    target = _rooted(receipt_dir, root=root)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return []
    if not (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid in {0, os.geteuid()}
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    ):
        raise ValueError("evidence_refresh_receipt_directory_not_admissible")
    paths = sorted(target.iterdir(), key=lambda item: item.name)
    if len(paths) > MAX_RECEIPT_COUNT or any(
        not (path.name.endswith(".json") and _REFRESH_ID.fullmatch(path.stem))
        for path in paths
    ):
        raise ValueError("evidence_refresh_receipt_set_not_admissible")
    return paths


def inspect_latest_evidence_refresh(
    *,
    action_receipt_id: str,
    action_receipt_sha256: str,
    receipt_dir: Path = DEFAULT_RECEIPT_DIR,
    root: Path = ROOT,
    now: datetime | None = None,
    max_age_seconds: float = runtime_review.DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    base = {
        "status": "absent",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": "run the exact evaluate-only evidence refresh command",
        "refresh_id": "",
        "receipt_path": "",
        "receipt_sha256": "",
        **_SAFETY,
    }
    if not (
        _ACTION_ID.fullmatch(action_receipt_id)
        and runtime_review._SHA256.fullmatch(action_receipt_sha256)
        and math.isfinite(float(max_age_seconds))
        and 60.0 <= float(max_age_seconds) <= 86400.0
    ):
        return {
            **base,
            "status": "blocked",
            "blocking_reason": "evidence_refresh_inspection_binding_not_admissible",
        }
    try:
        matching: list[tuple[datetime, str, Path, dict[str, Any], str]] = []
        for path in _private_receipt_paths(receipt_dir, root=root):
            receipt, digest = _load_receipt(path)
            binding = dict(receipt.get("binding") or {})
            if (
                binding.get("action_receipt_id") == action_receipt_id
                and binding.get("action_receipt_sha256") == action_receipt_sha256
            ):
                prepared_at = _parse_timestamp(receipt.get("prepared_at"))
                if prepared_at is None:
                    raise ValueError("evidence_refresh_timestamp_missing")
                matching.append(
                    (prepared_at, str(receipt.get("refresh_id") or ""), path, receipt, digest)
                )
    except Exception:
        return {
            **base,
            "status": "blocked",
            "blocking_reason": "evidence_refresh_receipt_chain_not_admissible",
        }
    if not matching:
        return base
    matching.sort(key=lambda row: (row[0], row[1]))
    latest_time, _refresh_id_value, path, receipt, digest = matching[-1]
    if sum(1 for row in matching if row[0] == latest_time) != 1:
        return {
            **base,
            "status": "blocked",
            "blocking_reason": "evidence_refresh_latest_receipt_ambiguous",
        }
    status = str(receipt.get("status") or "")
    completed_at = _parse_timestamp(receipt.get("completed_at"))
    evidence_time = completed_at or latest_time
    age_seconds = (observed_now - evidence_time).total_seconds()
    projected = {
        **base,
        "status": status,
        "refresh_id": str(receipt.get("refresh_id") or ""),
        "receipt_path": str(path),
        "receipt_sha256": digest,
        "prepared_at": latest_time.isoformat(),
        "completed_at": completed_at.isoformat() if completed_at else "",
        "blocking_reason": str(receipt.get("blocking_reason") or ""),
    }
    if status == "completed" and (
        not math.isfinite(age_seconds)
        or age_seconds < -30.0
        or age_seconds > float(max_age_seconds)
    ):
        projected.update(
            {
                "status": "stale",
                "blocking_reason": "evidence_refresh_receipt_not_fresh",
                "next_action": "run the exact evaluate-only evidence refresh command again",
            }
        )
    elif status == "completed":
        projected["next_action"] = (
            "review the fresh OODA posture; deployment or restart remains separately consent-gated"
        )
    elif status in {"prepared", "failed"}:
        projected["next_action"] = (
            "retry the exact evaluate-only refresh command to reconcile this safe local attempt"
        )
    return projected


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> str:
    payload = _canonical(receipt)
    atomic_write_bytes(path, payload, overwrite=True)
    return _sha256(payload)


def _output_evidence(path: Path, *, field: str) -> dict[str, Any]:
    evidence = _json_evidence(path, field=field, maximum_bytes=MAX_INPUT_BYTES)
    return evidence


def _assert_refresh_outputs(
    *,
    stage_report: Mapping[str, Any],
    cycle_report: Mapping[str, Any],
    packet: Mapping[str, Any],
    verification: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> None:
    if not (
        stage_report.get("status") == "staged"
        and stage_report.get("publication_verified") is True
        and stage_report.get("source_evidence") == binding.get("approved_sources")
        and stage_report.get("automatic_execution_allowed") is False
        and stage_report.get("provider_quota_consumption_allowed") is False
        and stage_report.get("protected_operation_executed") is False
        and cycle_report.get("execution_mode") == "evaluate_only"
        and cycle_report.get("delivery_authorized") is False
        and cycle_report.get("delivery_attempted") is False
        and cycle_report.get("sent") is False
        and cycle_report.get("automatic_execution_allowed") is False
        and cycle_report.get("provider_quota_consumption_allowed") is False
        and cycle_report.get("protected_operation_executed") is False
        and dict(cycle_report.get("signal_approval") or {}).get("approved") is True
        and packet.get("schema") == runtime_review.SCHEMA
        and verification.get("status") == "verified"
        and dict(verification.get("progress") or {}).get("current_evidence_verified")
        is True
        and verification.get("execution_authorized") is False
        and verification.get("protected_operation_executed") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and verification.get("delivery_authorized") is False
        and dict(packet.get("runtime_posture") or {}).get("fingerprint_sha256")
        == dict(binding.get("runtime_posture") or {}).get("fingerprint_sha256")
        and dict(packet.get("release_posture") or {}).get(
            "worktree_fingerprint_sha256"
        )
        == dict(binding.get("release_posture") or {}).get(
            "worktree_fingerprint_sha256"
        )
        and dict(packet.get("probe_posture") or {}).get("source_sha256")
        == dict(binding.get("live_mobile_receipt") or {}).get("sha256")
        and dict(packet.get("release_authority") or {}).get("source_sha256")
        == dict(binding.get("release_manifest") or {}).get("sha256")
    ):
        raise ValueError("evidence_refresh_output_contract_not_admissible")


def execute_evidence_refresh(
    *,
    expected_action_receipt_id: str,
    expected_action_receipt_sha256: str,
    action_receipt_dir: Path = manual_action.DEFAULT_RECEIPT_DIR,
    action_handoff_dir: Path = manual_action.DEFAULT_HANDOFF_DIR,
    refresh_receipt_dir: Path = DEFAULT_RECEIPT_DIR,
    refresh_lock_path: Path = DEFAULT_LOCK_PATH,
    gold_receipt_path: Path = Path(signal_stage.gold_notify._CANONICAL_GOLD_RECEIPT_PATHS[0]),
    scene_packet_path: Path = Path(signal_stage.scene_notify._CANONICAL_PACKET_PATHS[0]),
    scene_verifier_path: Path = Path(signal_stage.scene_notify._CANONICAL_VERIFIER_PATHS[0]),
    scene_runtime_status_path: Path = Path(
        signal_stage.scene_notify._CANONICAL_RUNTIME_STATUS_PATHS[0]
    ),
    signal_dir: Path = DEFAULT_SIGNAL_DIR,
    stage_receipt_path: Path = DEFAULT_STAGE_RECEIPT,
    cycle_receipt_path: Path = DEFAULT_CYCLE_RECEIPT,
    cycle_state_path: Path = DEFAULT_CYCLE_STATE,
    cycle_lock_path: Path = DEFAULT_CYCLE_LOCK,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    review_packet_path: Path = DEFAULT_REVIEW_PACKET,
    review_verification_path: Path = DEFAULT_REVIEW_VERIFICATION,
    project: str = "property",
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)

    def finished_at() -> datetime:
        return observed_now if now is not None else _now()

    def rooted(path: Path) -> Path:
        return _rooted(path, root=root)

    binding_args = {
        "expected_action_receipt_id": expected_action_receipt_id,
        "expected_action_receipt_sha256": expected_action_receipt_sha256,
        "action_receipt_dir": rooted(action_receipt_dir),
        "action_handoff_dir": rooted(action_handoff_dir),
        "source_root": root,
        "gold_receipt_path": rooted(gold_receipt_path),
        "scene_packet_path": rooted(scene_packet_path),
        "scene_verifier_path": rooted(scene_verifier_path),
        "scene_runtime_status_path": rooted(scene_runtime_status_path),
        "live_mobile_receipt_path": rooted(live_mobile_receipt_path),
        "release_manifest_path": rooted(release_manifest_path),
        "project": project,
        "now": observed_now,
    }
    try:
        _current_action, binding = _refresh_binding(**binding_args)
        refresh_id = _refresh_id(binding)
        receipt_path = _receipt_path(
            refresh_receipt_dir,
            refresh_id,
            root=root,
        )
    except Exception:
        return _blocked("evidence_refresh_preflight_not_admissible", now=observed_now)

    prepared_at = observed_now
    try:
        existing, existing_sha256 = _load_receipt(receipt_path)
    except FileNotFoundError:
        existing = None
        existing_sha256 = ""
    except Exception:
        return _blocked(
            "evidence_refresh_existing_receipt_not_admissible",
            now=observed_now,
            refresh_id=refresh_id,
            receipt_path=receipt_path,
        )
    if existing is not None:
        if existing.get("binding") != binding:
            return _blocked(
                "evidence_refresh_existing_receipt_binding_mismatch",
                now=observed_now,
                refresh_id=refresh_id,
                receipt_path=receipt_path,
                receipt_sha256=existing_sha256,
                receipt_persisted=True,
            )
        if existing.get("status") == "completed":
            return _result(
                "unchanged",
                now=observed_now,
                next_action=(
                    "review the already refreshed OODA posture; deployment or restart "
                    "remains separately consent-gated"
                ),
                refresh_id=refresh_id,
                receipt_path=receipt_path,
                receipt_sha256=existing_sha256,
                receipt_persisted=True,
            )
        persisted_prepared_at = _parse_timestamp(existing.get("prepared_at"))
        if persisted_prepared_at is None:
            return _blocked(
                "evidence_refresh_existing_receipt_not_admissible",
                now=observed_now,
                refresh_id=refresh_id,
                receipt_path=receipt_path,
            )
        prepared_at = persisted_prepared_at

    lock_descriptor: int | None = None
    try:
        lock_descriptor = notification_cycle._acquire_send_lock(
            rooted(refresh_lock_path)
        )
    except Exception:
        return _blocked(
            "evidence_refresh_lock_not_admissible",
            now=observed_now,
            refresh_id=refresh_id,
            receipt_path=receipt_path,
        )
    if lock_descriptor is None:
        return _blocked(
            "evidence_refresh_busy",
            now=observed_now,
            next_action="wait for the active evaluate-only refresh receipt",
            refresh_id=refresh_id,
            receipt_path=receipt_path,
        )

    try:
        try:
            _current_action, current_binding = _refresh_binding(**binding_args)
            if current_binding != binding:
                raise ValueError("evidence_refresh_binding_changed")
        except Exception:
            return _blocked(
                "evidence_refresh_binding_changed",
                now=observed_now,
                refresh_id=refresh_id,
                receipt_path=receipt_path,
            )

        prepared = _receipt_document(
            refresh_id=refresh_id,
            status="prepared",
            prepared_at=prepared_at,
            binding=binding,
        )
        try:
            prepared_sha256 = _write_receipt(receipt_path, prepared)
        except Exception:
            return _blocked(
                "evidence_refresh_prepare_receipt_write_failed",
                now=observed_now,
                refresh_id=refresh_id,
                receipt_path=receipt_path,
            )

        outputs: dict[str, Any] = {}
        failure_stage = "stage"
        try:
            rooted_signal_dir = rooted(signal_dir)
            rooted_stage_receipt = rooted(stage_receipt_path)
            stage_report = signal_stage.stage_approved_signals(
                gold_receipt_path=rooted(gold_receipt_path),
                public_origin_observation_path=rooted(
                    live_mobile_receipt_path
                ),
                scene_packet_path=rooted(scene_packet_path),
                scene_verifier_path=rooted(scene_verifier_path),
                scene_runtime_status_path=rooted(scene_runtime_status_path),
                target_dir=rooted_signal_dir,
                receipt_path=rooted_stage_receipt,
                now=observed_now,
            )
            outputs["approved_signal_stage"] = _output_evidence(
                rooted_stage_receipt,
                field="evidence_refresh_stage_receipt",
            )
            manifest_path = rooted_signal_dir / "manifest.json"
            outputs["approval_manifest"] = _output_evidence(
                manifest_path,
                field="evidence_refresh_approval_manifest",
            )

            failure_stage = "cycle"
            rooted_cycle_receipt = rooted(cycle_receipt_path)
            cycle_report = notification_cycle.run_cycle_once(
                gold_receipt=str(
                    rooted_signal_dir / approved.SIGNAL_FILENAMES["gold_receipt"]
                ),
                public_origin_observation=str(
                    rooted_signal_dir
                    / approved.SIGNAL_FILENAMES[
                        "public_origin_observation"
                    ]
                ),
                scene_packet=str(
                    rooted_signal_dir / approved.SIGNAL_FILENAMES["scene_packet"]
                ),
                scene_verifier=str(
                    rooted_signal_dir / approved.SIGNAL_FILENAMES["scene_verifier"]
                ),
                scene_runtime_status=str(
                    rooted_signal_dir
                    / approved.SIGNAL_FILENAMES["scene_runtime_status"]
                ),
                approval_manifest=str(manifest_path),
                require_approval_manifest=True,
                state_file=str(rooted(cycle_state_path)),
                lock_file=str(rooted(cycle_lock_path)),
                write=str(rooted_cycle_receipt),
                send=False,
                now=observed_now,
            )
            outputs["notification_cycle"] = _output_evidence(
                rooted_cycle_receipt,
                field="evidence_refresh_cycle_receipt",
            )

            failure_stage = "review"
            rooted_review_packet = rooted(review_packet_path)
            packet = runtime_review.materialize_current_review_packet(
                cycle_receipt_path=rooted_cycle_receipt,
                signal_dir=rooted_signal_dir,
                live_mobile_receipt_path=rooted(live_mobile_receipt_path),
                release_manifest_path=rooted(release_manifest_path),
                write_path=rooted_review_packet,
                project=project,
                now=observed_now,
            )
            verification = runtime_review.verify_current_review_packet(
                packet_path=rooted_review_packet,
                cycle_receipt_path=rooted_cycle_receipt,
                signal_dir=rooted_signal_dir,
                live_mobile_receipt_path=rooted(live_mobile_receipt_path),
                release_manifest_path=rooted(release_manifest_path),
                project=project,
                now=observed_now,
            )
            _assert_refresh_outputs(
                stage_report=stage_report,
                cycle_report=cycle_report,
                packet=packet,
                verification=verification,
                binding=binding,
            )
            outputs["runtime_review_packet"] = _output_evidence(
                rooted_review_packet,
                field="evidence_refresh_review_packet",
            )
            verification_snapshot = {
                "schema": str(verification.get("schema") or ""),
                "status": "verified",
                "updated_at": str(verification.get("updated_at") or ""),
                "blocking_reason": str(verification.get("blocking_reason") or ""),
                "current_evidence_verified": True,
                **_SAFETY,
            }
            rooted_verification = rooted(review_verification_path)
            atomic_write_bytes(
                rooted_verification,
                _canonical(verification_snapshot),
                overwrite=True,
            )
            outputs["runtime_review_verification"] = {
                **_output_evidence(
                    rooted_verification,
                    field="evidence_refresh_review_verification",
                ),
                "status": "verified",
                "current_evidence_verified": True,
            }
        except Exception:
            failure_observed_at = finished_at()
            failed = _receipt_document(
                refresh_id=refresh_id,
                status="failed",
                prepared_at=prepared_at,
                completed_at=failure_observed_at,
                blocking_reason=f"evidence_refresh_{failure_stage}_failed",
                binding=binding,
                outputs=outputs,
            )
            try:
                failed_sha256 = _write_receipt(receipt_path, failed)
            except Exception:
                failed_sha256 = prepared_sha256
            return _blocked(
                f"evidence_refresh_{failure_stage}_failed",
                now=failure_observed_at,
                next_action=(
                    "retry the same exact evaluate-only refresh command; no source edit, "
                    "deployment, restart, provider call, or delivery was authorized"
                ),
                refresh_id=refresh_id,
                receipt_path=receipt_path,
                receipt_sha256=failed_sha256,
                receipt_persisted=True,
            )

        completion_observed_at = finished_at()
        completed = _receipt_document(
            refresh_id=refresh_id,
            status="completed",
            prepared_at=prepared_at,
            completed_at=completion_observed_at,
            binding=binding,
            outputs=outputs,
        )
        try:
            completed_sha256 = _write_receipt(receipt_path, completed)
        except Exception:
            return _blocked(
                "evidence_refresh_finalize_receipt_write_failed",
                now=completion_observed_at,
                next_action=(
                    "retry the exact evaluate-only refresh command to reconcile the "
                    "prepared receipt; do not infer completion"
                ),
                refresh_id=refresh_id,
                receipt_path=receipt_path,
                receipt_sha256=prepared_sha256,
                receipt_persisted=True,
            )
        return _result(
            "refreshed",
            now=completion_observed_at,
            next_action=(
                "review the fresh OODA posture; any deployment or restart requires a "
                "separate exact authorization"
            ),
            refresh_id=refresh_id,
            receipt_path=receipt_path,
            receipt_sha256=completed_sha256,
            receipt_persisted=True,
        )
    finally:
        notification_cycle._release_send_lock(lock_descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh approved PropertyQuarry evaluate-only evidence for one exact manual "
            "action receipt without deploying, restarting, calling providers, or sending."
        )
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--action-receipt-id", default="")
    parser.add_argument("--action-receipt-sha256", default="")
    args = parser.parse_args(argv)
    if not args.refresh:
        result = _blocked(
            "evidence_refresh_explicit_invocation_required",
            now=_now(),
            next_action="use the exact receipt-bound --refresh command from operator status",
        )
    else:
        result = execute_evidence_refresh(
            expected_action_receipt_id=str(args.action_receipt_id or ""),
            expected_action_receipt_sha256=str(args.action_receipt_sha256 or ""),
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") in {"refreshed", "unchanged"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
