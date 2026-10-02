#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import stat
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_public_origin_observation as public_origin
from scripts import propertyquarry_notify_gold_status as gold_notify
from scripts import propertyquarry_notify_scene_video_provider_refresh as scene_notify
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_signal_stage.v1"
REVOCATION_SCHEMA = approved.REVOCATION_SCHEMA
DEFAULT_TARGET_DIR = "_completion/propertyquarry_ooda_signal_ingress"
DEFAULT_RECEIPT_PATH = "_completion/propertyquarry_ooda_notification_cycle/approved-signal-stage-latest.json"
DEFAULT_PUBLIC_ORIGIN_OBSERVATION_PATH = public_origin.DEFAULT_RECEIPT_PATH
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_STATUS_BYTES = 256 * 1024
DEFAULT_INTERVAL_SECONDS = 300.0
MIN_INTERVAL_SECONDS = 60.0
MAX_INTERVAL_SECONDS = 1800.0
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _observed_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _absolute_lexical_path(value: object) -> Path:
    return Path(os.path.abspath(os.fspath(Path(str(value or "").strip()).expanduser())))


def _load_source(path: Path, *, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label}_source_not_admissible") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError(f"{label}_source_not_admissible")
    payload, raw, digest = load_strict_json_object_snapshot(
        path,
        field=label,
        maximum_bytes=MAX_SOURCE_BYTES,
    )
    return payload, {
        "path": str(path),
        "sha256": digest,
        "bytes": len(raw),
    }


def _make_sanitized_readable(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError("staged_signal_identity_not_admissible")
        os.fchmod(descriptor, 0o644)
    finally:
        os.close(descriptor)


def _publish_sanitized(path: Path, payload: bytes) -> None:
    atomic_write_bytes(path, payload, overwrite=True)
    _make_sanitized_readable(path)


def _published_evidence(path: Path, *, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label}_not_admissible") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
    ):
        raise ValueError(f"{label}_not_admissible")
    payload, raw, digest = load_strict_json_object_snapshot(
        path,
        field=label,
        maximum_bytes=MAX_SOURCE_BYTES,
    )
    return payload, {
        "status": "ready",
        "sha256": digest,
        "bytes": len(raw),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _prepare_target_dir(target_dir: Path, *, create: bool = True) -> Path:
    target = _absolute_lexical_path(target_dir)
    if target == Path("/") or target.name in {"", ".", ".."}:
        raise ValueError("target_directory_invalid")
    try:
        target_metadata = target.lstat()
    except FileNotFoundError:
        if not create:
            raise ValueError("target_directory_not_admissible")
        target.mkdir(parents=True, mode=0o700)
        target_metadata = target.lstat()
    if not stat.S_ISDIR(target_metadata.st_mode) or target_metadata.st_uid != os.geteuid():
        raise ValueError("target_directory_not_admissible")
    if stat.S_IMODE(target_metadata.st_mode) & 0o022:
        raise ValueError("target_directory_peer_writable")
    return target


def _gold_source_lane(
    projection: dict[str, Any],
    public_origin_projection: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    gold_action = gold_notify.propertyquarry_operator_action_summary(
        projection,
        now=now,
        max_source_age_seconds=approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    public_action = approved.public_origin_operator_action_summary(
        public_origin_projection,
        now=now,
        max_source_age_seconds=approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    public_action_required = public_action.get("action_required") is True
    action = public_action if public_action_required else gold_action
    observed_at = (
        approved.public_origin_source_timestamp(public_origin_projection)
        if public_action_required
        else approved.gold_source_timestamp(projection)
    )
    return {
        "lane": "gold_live_runtime",
        "source_status": "ready",
        "action_required": action.get("action_required") is True,
        "clear_verified": (
            not public_action_required
            and gold_action.get("action_required") is not True
            and str(gold_action.get("reason") or "").strip()
            in {"gold_not_blocked", "no_pre_route_runtime_blocker"}
            and approved.source_timestamp_is_current(observed_at, now=now)
        ),
        "reason": str(action.get("reason") or "").strip(),
        "source_generated_at": str(action.get("source_generated_at") or "").strip(),
        "observed_source_generated_at": observed_at,
    }


def _scene_source_lane(
    projections: dict[str, dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    action = scene_notify.scene_video_operator_action_summary(
        packet=projections["scene_packet"],
        verifier=projections["scene_verifier"],
        runtime_status=projections["scene_runtime_status"],
        now=now,
        max_source_age_seconds=approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    observed_at = approved.scene_source_timestamp(
        projections["scene_packet"],
        projections["scene_verifier"],
        projections["scene_runtime_status"],
    )
    return {
        "lane": "scene_video_provider_refresh",
        "source_status": "ready",
        "action_required": action.get("action_required") is True,
        "clear_verified": (
            action.get("source_verified") is True
            and action.get("reason") == "no_actionable_provider_refresh"
        ),
        "reason": str(action.get("reason") or "").strip(),
        "source_generated_at": str(action.get("source_generated_at") or "").strip(),
        "observed_source_generated_at": observed_at,
    }


def _unavailable_source_lane(lane: str, reason: str) -> dict[str, Any]:
    return {
        "lane": lane,
        "source_status": "unavailable",
        "action_required": False,
        "clear_verified": False,
        "reason": reason,
        "source_generated_at": "",
        "observed_source_generated_at": "",
    }


def inspect_source_lanes(
    *,
    gold_receipt_path: Path,
    public_origin_observation_path: Path = DEFAULT_PUBLIC_ORIGIN_OBSERVATION_PATH,
    scene_packet_path: Path,
    scene_verifier_path: Path,
    scene_runtime_status_path: Path,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Project source readiness without publishing or exposing producer payloads."""

    observed_at = _observed_now(now)
    try:
        gold_source, _gold_evidence = _load_source(
            gold_receipt_path,
            label="gold_receipt",
        )
        gold_projection = approved.sanitize_gold_receipt(
            gold_source,
            now=observed_at,
        )
        public_origin_source, _public_origin_evidence = _load_source(
            public_origin_observation_path,
            label="public_origin_observation",
        )
        public_origin_projection = approved.sanitize_public_origin_observation(
            public_origin_source
        )
        gold_lane = _gold_source_lane(
            gold_projection,
            public_origin_projection,
            now=observed_at,
        )
    except (OSError, TypeError, ValueError):
        gold_lane = _unavailable_source_lane(
            "gold_live_runtime",
            "gold_live_runtime_source_receipts_unavailable",
        )

    scene_sources: dict[str, dict[str, Any]] = {}
    try:
        for name, path in {
            "scene_packet": scene_packet_path,
            "scene_verifier": scene_verifier_path,
            "scene_runtime_status": scene_runtime_status_path,
        }.items():
            scene_sources[name], _scene_evidence = _load_source(path, label=name)
        scene_projections = approved.sanitize_scene_receipts(
            packet=scene_sources["scene_packet"],
            verifier=scene_sources["scene_verifier"],
            runtime_status=scene_sources["scene_runtime_status"],
            now=observed_at,
        )
        scene_lane = _scene_source_lane(scene_projections, now=observed_at)
    except (OSError, TypeError, ValueError):
        scene_lane = _unavailable_source_lane(
            "scene_video_provider_refresh",
            "scene_source_receipts_unavailable",
        )
    return [gold_lane, scene_lane]


def _revocation_payload(
    *,
    source_lanes: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    return approved.build_revocation_manifest(
        source_lanes=source_lanes,
        now=now,
    )


def publish_source_revocation(
    *,
    target_dir: Path,
    source_lanes: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Atomically replace approval authority with a sanitized revocation marker."""

    target = _prepare_target_dir(target_dir)
    payload = _revocation_payload(source_lanes=source_lanes, now=now)
    raw = approved.canonical_json_bytes(payload)
    os.chmod(target, 0o755, follow_symlinks=False)
    _publish_sanitized(target / "manifest.json", raw)
    return {
        "payload": payload,
        "sha256": approved.sha256_bytes(raw),
        "bytes": len(raw),
    }


def inspect_source_revocation(
    target_dir: Path,
    *,
    source_lanes: list[dict[str, Any]],
    generated_at: str,
    expected_sha256: str,
) -> dict[str, Any]:
    target = _prepare_target_dir(target_dir, create=False)
    parsed = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("source_revocation_timestamp_invalid")
    expected = _revocation_payload(
        source_lanes=source_lanes,
        now=parsed.astimezone(timezone.utc),
    )
    payload, evidence = _published_evidence(
        target / "manifest.json",
        label="source_revocation_manifest",
    )
    if not (
        payload == expected
        and _SHA256.fullmatch(str(expected_sha256 or ""))
        and evidence.get("sha256") == expected_sha256
    ):
        raise ValueError("source_revocation_not_admissible")
    return {
        "approved": False,
        "reason": "approved_signal_snapshot_revoked",
        "policy": "",
        "revocation_verified": True,
        "revocation_generated_at": parsed.astimezone(timezone.utc).isoformat(),
        "revocation_manifest_sha256": str(expected_sha256),
    }


def inspect_approved_signal_dir(
    target_dir: Path,
    *,
    now: datetime | None = None,
    max_age_seconds: float = approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
) -> dict[str, Any]:
    """Verify the complete published snapshot without trusting daemon state."""

    target = _absolute_lexical_path(target_dir)
    try:
        target_metadata = target.lstat()
    except OSError as exc:
        raise ValueError("target_directory_not_admissible") from exc
    if (
        not stat.S_ISDIR(target_metadata.st_mode)
        or target_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(target_metadata.st_mode) & 0o022
    ):
        raise ValueError("target_directory_not_admissible")

    manifest, manifest_evidence = _published_evidence(
        target / "manifest.json",
        label="approval_manifest",
    )
    input_evidence: dict[str, dict[str, Any]] = {}
    input_payloads: dict[str, dict[str, Any]] = {}
    if manifest.get("schema") != approved.REVOCATION_SCHEMA:
        for name, filename in approved.SIGNAL_FILENAMES.items():
            input_payloads[name], input_evidence[name] = _published_evidence(
                target / filename,
                label=name,
            )
    return approved.verify_approval_manifest(
        manifest=manifest,
        manifest_evidence=manifest_evidence,
        input_evidence=input_evidence,
        input_payloads=input_payloads,
        now=_observed_now(now),
        max_age_seconds=max_age_seconds,
    )


def inspect_stage_status_receipt(
    receipt_path: Path,
    *,
    now: datetime | None = None,
    max_age_seconds: float = approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
) -> dict[str, Any]:
    """Validate the private daemon status independently of process logs."""

    path = _absolute_lexical_path(receipt_path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError("stage_status_receipt_not_admissible") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("stage_status_receipt_not_admissible")
    payload, _raw, _digest = load_strict_json_object_snapshot(
        path,
        field="stage_status_receipt",
        maximum_bytes=MAX_STATUS_BYTES,
    )
    updated_raw = str(payload.get("updated_at") or "").strip()
    try:
        updated_at = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("stage_status_timestamp_invalid") from exc
    if updated_at.tzinfo is None:
        raise ValueError("stage_status_timestamp_invalid")
    age_limit = float(max_age_seconds)
    age_seconds = (_observed_now(now) - updated_at.astimezone(timezone.utc)).total_seconds()
    if (
        not math.isfinite(age_limit)
        or not 60.0 <= age_limit <= 86400.0
        or not math.isfinite(age_seconds)
        or age_seconds < -30.0
        or age_seconds > age_limit
    ):
        raise ValueError("stage_status_not_fresh")
    progress = payload.get("progress")
    if (
        payload.get("schema") != SCHEMA
        or payload.get("status") != "ready"
        or payload.get("blocking_reason") != ""
        or not isinstance(progress, dict)
        or payload.get("automatic_execution_allowed") is not False
        or payload.get("provider_quota_consumption_allowed") is not False
        or payload.get("protected_operation_executed") is not False
    ):
        raise ValueError("stage_status_contract_not_admissible")
    try:
        source_evidence_posture = approved.verify_source_evidence_posture(
            lanes=payload.get("source_lanes"),
            posture=payload.get("source_evidence_posture"),
            now=_observed_now(now),
            max_age_seconds=max_age_seconds,
        )
    except ValueError as exc:
        raise ValueError("stage_status_source_evidence_not_admissible") from exc
    source_status = str(source_evidence_posture.get("status") or "")
    source_progress = dict(source_evidence_posture.get("progress") or {})
    publication_status = str(payload.get("publication_status") or "")
    expected_publication_status = (
        "revoked"
        if int(source_progress.get("unavailable_lane_count") or 0) > 0
        else "approved"
    )
    expected_next_action = (
        "republish_after_configured_interval"
        if source_status == "verified_current"
        else str(source_evidence_posture.get("next_action") or "")
    )
    if (
        payload.get("next_action") != expected_next_action
        or payload.get("source_status") != source_status
        or publication_status != expected_publication_status
        or not _SHA256.fullmatch(str(payload.get("manifest_sha256") or ""))
    ):
        raise ValueError("stage_status_contract_not_admissible")
    return {
        "status": "ready",
        "updated_at": updated_at.astimezone(timezone.utc).isoformat(),
        "blocking_reason": "",
        "next_action": str(payload["next_action"]),
        "progress": dict(progress),
        "source_status": source_status,
        "publication_status": publication_status,
        "manifest_sha256": str(payload["manifest_sha256"]),
        "source_lanes": [dict(row) for row in list(payload.get("source_lanes") or [])],
        "source_evidence_posture": source_evidence_posture,
    }


def _bounded_runtime_values(
    *,
    interval_seconds: float,
    max_age_seconds: float,
) -> tuple[float, float]:
    interval = float(interval_seconds)
    age_limit = float(max_age_seconds)
    if (
        not math.isfinite(interval)
        or not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS
        or not math.isfinite(age_limit)
        or not 60.0 <= age_limit <= 86400.0
        or interval * 2.0 > age_limit
    ):
        raise ValueError("stage_runtime_bounds_invalid")
    return interval, age_limit


def stage_approved_signals(
    *,
    gold_receipt_path: Path,
    public_origin_observation_path: Path = DEFAULT_PUBLIC_ORIGIN_OBSERVATION_PATH,
    scene_packet_path: Path,
    scene_verifier_path: Path,
    scene_runtime_status_path: Path,
    target_dir: Path,
    receipt_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = _observed_now(now)
    source_paths = {
        "gold_receipt": gold_receipt_path,
        "public_origin_observation": public_origin_observation_path,
        "scene_packet": scene_packet_path,
        "scene_verifier": scene_verifier_path,
        "scene_runtime_status": scene_runtime_status_path,
    }
    sources: dict[str, dict[str, Any]] = {}
    source_evidence: dict[str, dict[str, Any]] = {}
    for name, path in source_paths.items():
        sources[name], source_evidence[name] = _load_source(path, label=name)

    projections: dict[str, dict[str, Any]] = {
        "gold_receipt": approved.sanitize_gold_receipt(
            sources["gold_receipt"],
            now=observed_at,
        ),
        "public_origin_observation": approved.sanitize_public_origin_observation(
            sources["public_origin_observation"]
        ),
    }
    projections.update(
        approved.sanitize_scene_receipts(
            packet=sources["scene_packet"],
            verifier=sources["scene_verifier"],
            runtime_status=sources["scene_runtime_status"],
            now=observed_at,
        )
    )
    signal_bytes = {
        name: approved.canonical_json_bytes(payload)
        for name, payload in projections.items()
    }
    source_generated_at = {
        "gold_receipt": str(sources["gold_receipt"].get("generated_at") or "").strip(),
        "public_origin_observation": str(
            sources["public_origin_observation"].get("generated_at") or ""
        ).strip(),
        "scene_packet": str(sources["scene_packet"].get("generated_at") or "").strip(),
        "scene_verifier": str(sources["scene_verifier"].get("generated_at") or "").strip(),
        "scene_runtime_status": str(sources["scene_runtime_status"].get("generated_at") or "").strip(),
    }
    source_contracts = {
        "gold_receipt": str(sources["gold_receipt"].get("schema") or "").strip(),
        "public_origin_observation": str(
            sources["public_origin_observation"].get("schema") or ""
        ).strip(),
        "scene_packet": str(sources["scene_packet"].get("contract_name") or "").strip(),
        "scene_verifier": "propertyquarry.scene_video_provider_refresh_verifier.v1",
        "scene_runtime_status": str(
            sources["scene_runtime_status"].get("contract_name") or ""
        ).strip(),
    }
    manifest = approved.build_approval_manifest(
        signal_bytes=signal_bytes,
        source_evidence=source_evidence,
        source_generated_at=source_generated_at,
        source_contracts=source_contracts,
        now=observed_at,
    )
    manifest_bytes = approved.canonical_json_bytes(manifest)

    target = _prepare_target_dir(target_dir)

    for name, filename in approved.SIGNAL_FILENAMES.items():
        _publish_sanitized(target / filename, signal_bytes[name])
    os.chmod(target, 0o755, follow_symlinks=False)
    manifest_path = target / "manifest.json"
    _publish_sanitized(manifest_path, manifest_bytes)

    publication = inspect_approved_signal_dir(
        target,
        now=observed_at,
    )
    if publication.get("approved") is not True:
        raise ValueError("published_snapshot_not_approved")

    gold_action = gold_notify.propertyquarry_operator_action_summary(
        projections["gold_receipt"],
        now=observed_at,
        max_source_age_seconds=approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    public_origin_action = approved.public_origin_operator_action_summary(
        projections["public_origin_observation"],
        now=observed_at,
        max_source_age_seconds=approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    propertyquarry_action = (
        public_origin_action
        if public_origin_action.get("action_required") is True
        else gold_action
    )
    scene_action = scene_notify.scene_video_operator_action_summary(
        packet=projections["scene_packet"],
        verifier=projections["scene_verifier"],
        runtime_status=projections["scene_runtime_status"],
        now=observed_at,
        max_source_age_seconds=approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    source_lanes = [
        _gold_source_lane(
            projections["gold_receipt"],
            projections["public_origin_observation"],
            now=observed_at,
        ),
        _scene_source_lane(projections, now=observed_at),
    ]
    source_evidence_posture = approved.build_source_evidence_posture(source_lanes)
    report = {
        "schema": SCHEMA,
        "generated_at": observed_at.isoformat(),
        "updated_at": observed_at.isoformat(),
        "status": "staged",
        "blocking_reason": "",
        "next_action": (
            "republish_after_configured_interval"
            if source_evidence_posture.get("status") == "verified_current"
            else str(source_evidence_posture.get("next_action") or "")
        ),
        "progress": {
            "sanitized_signal_count": len(signal_bytes),
            "expected_signal_count": len(approved.SIGNAL_FILENAMES),
        },
        "target_dir": str(target),
        "manifest_path": str(manifest_path),
        "manifest_sha256": approved.sha256_bytes(manifest_bytes),
        "publication_status": "approved",
        "publication_order": "signals_then_manifest",
        "publication_verified": True,
        "source_evidence": source_evidence,
        "source_lanes": source_lanes,
        "source_evidence_posture": source_evidence_posture,
        "lane_posture": {
            "gold_live_runtime": {
                "action_required": propertyquarry_action.get("action_required") is True,
                "reason": str(propertyquarry_action.get("reason") or "").strip(),
                "source_generated_at": str(
                    propertyquarry_action.get("source_generated_at") or ""
                ).strip(),
            },
            "scene_video_provider_refresh": {
                "action_required": scene_action.get("action_required") is True,
                "reason": str(scene_action.get("reason") or "").strip(),
                "source_generated_at": str(scene_action.get("source_generated_at") or "").strip(),
            },
        },
        "sanitized_signal_count": len(signal_bytes),
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
    }
    atomic_write_bytes(
        _absolute_lexical_path(receipt_path),
        approved.canonical_json_bytes(report),
        overwrite=True,
    )
    return report


def _build_stage_iteration_event(
    *,
    gold_receipt_path: Path,
    public_origin_observation_path: Path = DEFAULT_PUBLIC_ORIGIN_OBSERVATION_PATH,
    scene_packet_path: Path,
    scene_verifier_path: Path,
    scene_runtime_status_path: Path,
    target_dir: Path,
    receipt_path: Path,
    max_age_seconds: float = approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Build one approved or fail-closed revoked stage event."""

    observed_now = _observed_now(now)
    generated_at = observed_now.isoformat()
    try:
        report = stage_approved_signals(
            gold_receipt_path=gold_receipt_path,
            public_origin_observation_path=public_origin_observation_path,
            scene_packet_path=scene_packet_path,
            scene_verifier_path=scene_verifier_path,
            scene_runtime_status_path=scene_runtime_status_path,
            target_dir=target_dir,
            receipt_path=receipt_path,
            now=observed_now,
        )
        health = inspect_approved_signal_dir(
            target_dir,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        if health.get("approved") is not True:
            raise ValueError("published_snapshot_not_approved")
        event = {
            "schema": SCHEMA,
            "generated_at": str(report.get("generated_at") or generated_at),
            "updated_at": str(report.get("updated_at") or generated_at),
            "status": "ready",
            "blocking_reason": "",
            "next_action": (
                "republish_after_configured_interval"
                if dict(report.get("source_evidence_posture") or {}).get(
                    "status"
                )
                == "verified_current"
                else str(
                    dict(report.get("source_evidence_posture") or {}).get(
                        "next_action"
                    )
                    or "await_producer_owned_source_receipts"
                )
            ),
            "manifest_sha256": str(report.get("manifest_sha256") or ""),
            "publication_status": "approved",
            "sanitized_signal_count": int(report.get("sanitized_signal_count") or 0),
            "source_status": str(
                dict(report.get("source_evidence_posture") or {}).get("status")
                or "unavailable"
            ),
            "source_lanes": list(report.get("source_lanes") or []),
            "source_evidence_posture": dict(
                report.get("source_evidence_posture") or {}
            ),
            "automatic_execution_allowed": False,
            "provider_quota_consumption_allowed": False,
            "protected_operation_executed": False,
        }
        return event, True
    except Exception as exc:
        source_lanes: list[dict[str, Any]] = []
        source_evidence_posture: dict[str, Any] = {}
        source_inspection_error: Exception | None = None
        try:
            source_lanes = inspect_source_lanes(
                gold_receipt_path=gold_receipt_path,
                public_origin_observation_path=public_origin_observation_path,
                scene_packet_path=scene_packet_path,
                scene_verifier_path=scene_verifier_path,
                scene_runtime_status_path=scene_runtime_status_path,
                now=observed_now,
            )
            source_evidence_posture = approved.build_source_evidence_posture(
                source_lanes
            )
        except Exception as inspect_exc:
            source_inspection_error = inspect_exc
        unavailable_count = (
            int(
                dict(source_evidence_posture.get("progress") or {}).get(
                    "unavailable_lane_count"
                )
                or 0
            )
            if source_evidence_posture
            else 0
        )
        revocation: dict[str, Any] | None = None
        revocation_error: Exception | None = None
        if unavailable_count:
            try:
                revocation = publish_source_revocation(
                    target_dir=target_dir,
                    source_lanes=source_lanes,
                    now=observed_now,
                )
            except Exception as revoke_exc:
                revocation_error = revoke_exc
        if revocation is not None:
            return {
                "schema": SCHEMA,
                "generated_at": generated_at,
                "updated_at": generated_at,
                "status": "ready",
                "blocking_reason": "",
                "next_action": str(
                    source_evidence_posture.get("next_action") or ""
                ),
                "manifest_sha256": str(revocation.get("sha256") or ""),
                "publication_status": "revoked",
                "sanitized_signal_count": 0,
                "source_status": str(
                    source_evidence_posture.get("status") or "unavailable"
                ),
                "source_lanes": source_lanes,
                "source_evidence_posture": source_evidence_posture,
                "automatic_execution_allowed": False,
                "provider_quota_consumption_allowed": False,
                "protected_operation_executed": False,
            }, False
        effective_error = revocation_error or source_inspection_error or exc
        return {
            "schema": SCHEMA,
            "generated_at": generated_at,
            "updated_at": generated_at,
            "status": "stage_failed",
            "blocking_reason": "stage_cycle_failed",
            "next_action": "inspect_source_contract_freshness_and_permissions_then_retry",
            "error_type": type(effective_error).__name__,
            "automatic_execution_allowed": False,
            "provider_quota_consumption_allowed": False,
            "protected_operation_executed": False,
        }, False


def _persist_stage_iteration_event(
    event: dict[str, Any],
    *,
    receipt_path: Path,
) -> dict[str, Any]:
    persisted = dict(event)
    persisted["receipt_persisted"] = True
    try:
        atomic_write_bytes(
            _absolute_lexical_path(receipt_path),
            approved.canonical_json_bytes(persisted),
            overwrite=True,
        )
    except Exception:
        persisted["receipt_persisted"] = False
    return persisted


def run_stage_iteration(
    *,
    gold_receipt_path: Path,
    public_origin_observation_path: Path = DEFAULT_PUBLIC_ORIGIN_OBSERVATION_PATH,
    scene_packet_path: Path,
    scene_verifier_path: Path,
    scene_runtime_status_path: Path,
    target_dir: Path,
    receipt_path: Path,
    max_age_seconds: float = approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Publish one approved or revoked snapshot and its current private status."""

    event, successful = _build_stage_iteration_event(
        gold_receipt_path=gold_receipt_path,
        public_origin_observation_path=public_origin_observation_path,
        scene_packet_path=scene_packet_path,
        scene_verifier_path=scene_verifier_path,
        scene_runtime_status_path=scene_runtime_status_path,
        target_dir=target_dir,
        receipt_path=receipt_path,
        max_age_seconds=max_age_seconds,
        now=now,
    )
    event["progress"] = {
        "cycles": 1,
        "successful_cycles": 1 if successful else 0,
        "failed_cycles": 0 if successful else 1,
        "last_successful_at": str(event.get("generated_at") or "")
        if successful
        else "",
    }
    return _persist_stage_iteration_event(event, receipt_path=receipt_path)


def run_stage_daemon(
    *,
    gold_receipt_path: Path,
    public_origin_observation_path: Path = DEFAULT_PUBLIC_ORIGIN_OBSERVATION_PATH,
    scene_packet_path: Path,
    scene_verifier_path: Path,
    scene_runtime_status_path: Path,
    target_dir: Path,
    receipt_path: Path,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    max_age_seconds: float = approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    stop_event: threading.Event | None = None,
) -> int:
    """Continuously republish bounded projections; never deliver or call providers."""

    interval, age_limit = _bounded_runtime_values(
        interval_seconds=interval_seconds,
        max_age_seconds=max_age_seconds,
    )
    daemon_stop = stop_event or threading.Event()
    if stop_event is None:
        def request_stop(_signum: int, _frame: object) -> None:
            daemon_stop.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

    cycles = 0
    successful_cycles = 0
    failed_cycles = 0
    last_successful_at = ""
    while not daemon_stop.is_set():
        cycles += 1
        event, successful = _build_stage_iteration_event(
            gold_receipt_path=gold_receipt_path,
            public_origin_observation_path=public_origin_observation_path,
            scene_packet_path=scene_packet_path,
            scene_verifier_path=scene_verifier_path,
            scene_runtime_status_path=scene_runtime_status_path,
            target_dir=target_dir,
            receipt_path=receipt_path,
            max_age_seconds=age_limit,
        )
        if successful:
            successful_cycles += 1
            last_successful_at = str(event.get("generated_at") or "")
        else:
            failed_cycles += 1
        event["progress"] = {
            "cycles": cycles,
            "successful_cycles": successful_cycles,
            "failed_cycles": failed_cycles,
            "last_successful_at": last_successful_at,
        }
        event = _persist_stage_iteration_event(event, receipt_path=receipt_path)
        print(json.dumps(event, sort_keys=True), flush=True)
        daemon_stop.wait(interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage sanitized, hash-bound PropertyQuarry OODA signals for read-only scheduler ingress."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true")
    mode.add_argument("--health", action="store_true")
    parser.add_argument("--require-status-receipt", action="store_true")
    parser.add_argument("--gold-receipt", default=gold_notify._CANONICAL_GOLD_RECEIPT_PATHS[0])
    parser.add_argument(
        "--public-origin-observation",
        default=str(DEFAULT_PUBLIC_ORIGIN_OBSERVATION_PATH),
    )
    parser.add_argument("--scene-packet", default=scene_notify._CANONICAL_PACKET_PATHS[0])
    parser.add_argument("--scene-verifier", default=scene_notify._CANONICAL_VERIFIER_PATHS[0])
    parser.add_argument(
        "--scene-runtime-status",
        default=scene_notify._CANONICAL_RUNTIME_STATUS_PATHS[0],
    )
    parser.add_argument("--target-dir", default=DEFAULT_TARGET_DIR)
    parser.add_argument("--write", default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--approval-max-age-seconds",
        type=float,
        default=approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    args = parser.parse_args(argv)

    target_dir = _absolute_lexical_path(args.target_dir)
    if args.health:
        try:
            if args.require_status_receipt:
                stage_status = inspect_stage_status_receipt(
                    _absolute_lexical_path(args.write),
                    max_age_seconds=args.approval_max_age_seconds,
                )
                if stage_status.get("publication_status") == "revoked":
                    health = inspect_source_revocation(
                        target_dir,
                        source_lanes=list(stage_status.get("source_lanes") or []),
                        generated_at=str(stage_status.get("updated_at") or ""),
                        expected_sha256=str(
                            stage_status.get("manifest_sha256") or ""
                        ),
                    )
                else:
                    health = inspect_approved_signal_dir(
                        target_dir,
                        max_age_seconds=args.approval_max_age_seconds,
                    )
                    if health.get("approved") is not True:
                        raise ValueError("published_snapshot_not_approved")
                health.update(stage_status)
            else:
                health = inspect_approved_signal_dir(
                    target_dir,
                    max_age_seconds=args.approval_max_age_seconds,
                )
                health.update(
                    {
                        "status": "ready",
                        "updated_at": str(health.get("manifest_generated_at") or ""),
                        "blocking_reason": "",
                        "next_action": "verify_again_before_manifest_expiry",
                        "progress": {"publication_verified": True},
                    }
                )
        except (OSError, ValueError):
            health = {
                "approved": False,
                "reason": "approved_signal_snapshot_not_admissible",
                "policy": "",
                "status": "blocked",
                "updated_at": _observed_now().isoformat(),
                "blocking_reason": "approved_signal_snapshot_not_admissible",
                "next_action": "inspect_stage_status_and_source_receipts_then_retry",
                "progress": {"publication_verified": False},
            }
        print(json.dumps(health, sort_keys=True))
        return (
            0
            if (
                health.get("status") == "ready"
                if args.require_status_receipt
                else health.get("approved") is True
            )
            else 1
        )

    call = {
        "gold_receipt_path": _absolute_lexical_path(args.gold_receipt),
        "public_origin_observation_path": _absolute_lexical_path(
            args.public_origin_observation
        ),
        "scene_packet_path": _absolute_lexical_path(args.scene_packet),
        "scene_verifier_path": _absolute_lexical_path(args.scene_verifier),
        "scene_runtime_status_path": _absolute_lexical_path(args.scene_runtime_status),
        "target_dir": target_dir,
        "receipt_path": _absolute_lexical_path(args.write),
    }
    if args.daemon:
        return run_stage_daemon(
            **call,
            interval_seconds=args.interval_seconds,
            max_age_seconds=args.approval_max_age_seconds,
        )

    report = stage_approved_signals(
        **call,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
