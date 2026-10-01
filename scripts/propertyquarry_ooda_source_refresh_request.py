#!/usr/bin/env python3
"""Stage a reversible producer work item for non-current OODA sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_source_refresh_request.v1"
VERIFY_SCHEMA = "propertyquarry.ooda_source_refresh_request_verification.v1"
DEFAULT_REQUEST_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/source-refresh-request.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-request-verification.json"
)
DEFAULT_MAX_AGE_SECONDS = approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS
MAX_RECEIPT_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LANE_ARTIFACTS = {
    "gold_live_runtime": ["gold_receipt"],
    "scene_video_provider_refresh": [
        "scene_packet",
        "scene_verifier",
        "scene_runtime_status",
    ],
}
_LANE_STATUSES = frozenset({"current", "stale", "unavailable"})


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return approved.canonical_json_bytes(dict(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fresh_timestamp(
    value: object,
    *,
    now: datetime,
    max_age_seconds: float,
) -> str | None:
    parsed = _timestamp(value)
    if parsed is None:
        return None
    age = (now - parsed).total_seconds()
    if not math.isfinite(age) or age < -30.0 or age > max_age_seconds:
        return None
    return parsed.isoformat()


def _integrity_bound(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(result)),
    }
    return result


def _integrity_verified(payload: Mapping[str, Any]) -> bool:
    normalized = dict(payload)
    integrity = normalized.pop("integrity", None)
    return bool(
        isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and _SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    )


def _safety_fields() -> dict[str, bool]:
    return {
        "action_required": False,
        "interrupt_operator": False,
        "reversible_staging": True,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_source_refresh_allowed": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "secret_values_recorded": False,
    }


def _blocked(
    reason: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = _now(now).isoformat()
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "request_state": "blocked",
        "updated_at": observed_at,
        "blocking_reason": reason,
        "next_action": (
            "regenerate a fresh approved OODA cycle and re-stage the producer "
            "refresh request; do not dispatch the stale request"
        ),
        "request_id": "",
        "request_staged": False,
        "source_cycle_receipt_sha256": "",
        "requested_lanes": [],
        "progress": {
            "current_evidence_verified": False,
            "request_receipt_persisted": False,
            "verification_receipt_persisted": False,
        },
        **_safety_fields(),
    }


def _blocked_request(
    reason: str,
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    return _integrity_bound(
        {
            "schema": SCHEMA,
            "status": "blocked",
            "request_state": "blocked",
            "generated_at": now.isoformat(),
            "expires_at": (
                now + timedelta(seconds=float(max_age_seconds))
            ).isoformat(),
            "blocking_reason": reason,
            "next_action": (
                "regenerate a fresh approved OODA cycle and re-stage the "
                "producer refresh request; do not dispatch this tombstone"
            ),
            "request_id": "",
            "semantic_request_sha256": "",
            "source_cycle_receipt_sha256": "",
            "source_status_updated_at": "",
            "source_manifest_generated_at": "",
            "approval_disposition": "unverified",
            "request_scope": {
                "operation": "none",
                "effect": "blocked_tombstone",
                "consumer_reverification_required": True,
            },
            "requested_lanes": [],
            "request_staged": False,
            "progress": {
                "requested_lane_count": 0,
                "current_evidence_verified": False,
            },
            **_safety_fields(),
        }
    )


def _approval_disposition(value: object) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("source_refresh_approval_not_admissible")
    policy = str(value.get("policy") or "").strip()
    reason = str(value.get("reason") or "").strip()
    if policy != approved.POLICY:
        raise ValueError("source_refresh_approval_not_admissible")
    if value.get("approved") is True and reason == "approved_projection_manifest_verified":
        return "approved", reason
    if (
        value.get("approved") is False
        and value.get("revocation_verified") is True
        and reason == "approved_projection_manifest_revoked"
    ):
        return "revoked", reason
    raise ValueError("source_refresh_approval_not_admissible")


def _project_source_evidence(
    summary: Mapping[str, Any],
) -> tuple[str, str, list[dict[str, Any]], dict[str, int]]:
    source_digest = str(summary.get("source_cycle_receipt_sha256") or "")
    if not (
        summary.get("schema") == operator_status.SCHEMA
        and summary.get("status")
        in {"ready", "waiting_for_evidence", "action_required", "pending_action"}
        and _SHA256.fullmatch(source_digest)
        and dict(summary.get("progress") or {}).get(
            "current_snapshot_hashes_verified"
        )
        is True
        and summary.get("automatic_execution_allowed") is False
        and summary.get("provider_quota_consumption_allowed") is False
        and summary.get("protected_operation_executed") is False
    ):
        raise ValueError("source_refresh_operator_status_not_admissible")
    approval_disposition, _approval_reason = _approval_disposition(
        summary.get("signal_approval")
    )
    source = summary.get("source_evidence")
    if not isinstance(source, Mapping):
        raise ValueError("source_refresh_evidence_not_admissible")
    raw_lanes = source.get("lanes")
    raw_progress = source.get("progress")
    if not (
        source.get("schema") == approved.SOURCE_EVIDENCE_SCHEMA
        and source.get("status")
        in {"verified_current", "waiting_for_fresh_sources"}
        and source.get("automatic_source_refresh_allowed") is False
        and source.get("automatic_execution_allowed") is False
        and source.get("provider_quota_consumption_allowed") is False
        and source.get("delivery_authorized") is False
        and source.get("protected_operation_executed") is False
        and isinstance(raw_lanes, list)
        and isinstance(raw_progress, Mapping)
        and len(raw_lanes) == len(_LANE_ARTIFACTS)
    ):
        raise ValueError("source_refresh_evidence_not_admissible")

    lanes: list[dict[str, Any]] = []
    counts = {"current": 0, "stale": 0, "unavailable": 0}
    seen: set[str] = set()
    for value in raw_lanes:
        if not isinstance(value, Mapping):
            raise ValueError("source_refresh_lane_not_admissible")
        lane = str(value.get("lane") or "").strip()
        lane_status = str(value.get("status") or "").strip()
        reason = str(value.get("reason") or "").strip()
        source_generated_at = str(
            value.get("source_generated_at") or ""
        ).strip()
        if not (
            lane in _LANE_ARTIFACTS
            and lane not in seen
            and lane_status in _LANE_STATUSES
            and reason
            and value.get("source_artifacts") == _LANE_ARTIFACTS[lane]
            and value.get("producer_authority") == "external_receipt_producer"
            and value.get("producer_refresh_required")
            is (lane_status != "current")
            and value.get("automatic_source_refresh_allowed") is False
            and (
                not source_generated_at
                or _timestamp(source_generated_at) is not None
            )
        ):
            raise ValueError("source_refresh_lane_not_admissible")
        seen.add(lane)
        counts[lane_status] += 1
        if lane_status != "current":
            lanes.append(
                {
                    "lane": lane,
                    "status": lane_status,
                    "reason": reason,
                    "source_generated_at": source_generated_at,
                    "source_artifacts": list(_LANE_ARTIFACTS[lane]),
                    "producer_authority": "external_receipt_producer",
                    "requested_operation": "refresh_receipts",
                    "current_reverification_required": True,
                }
            )
    lanes.sort(key=lambda row: str(row["lane"]))
    if seen != set(_LANE_ARTIFACTS):
        raise ValueError("source_refresh_lane_set_not_admissible")
    expected_progress = {
        "expected_lane_count": len(_LANE_ARTIFACTS),
        "current_lane_count": counts["current"],
        "stale_lane_count": counts["stale"],
        "unavailable_lane_count": counts["unavailable"],
    }
    if dict(raw_progress) != expected_progress:
        raise ValueError("source_refresh_progress_not_admissible")
    expected_source_status = (
        "verified_current"
        if counts["current"] == len(_LANE_ARTIFACTS)
        else "waiting_for_fresh_sources"
    )
    if source.get("status") != expected_source_status:
        raise ValueError("source_refresh_evidence_status_not_admissible")
    return source_digest, approval_disposition, lanes, expected_progress


def build_source_refresh_request(
    summary: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    if not (
        math.isfinite(float(max_age_seconds))
        and 60.0 <= float(max_age_seconds) <= 86400.0
    ):
        raise ValueError("source_refresh_age_bound_not_admissible")
    source_digest, approval_disposition, lanes, source_progress = (
        _project_source_evidence(summary)
    )
    source_updated_at = _fresh_timestamp(
        summary.get("updated_at"),
        now=observed_now,
        max_age_seconds=float(max_age_seconds),
    )
    approval_row = dict(summary.get("signal_approval") or {})
    manifest_generated_at = _fresh_timestamp(
        approval_row.get("manifest_generated_at"),
        now=observed_now,
        max_age_seconds=float(max_age_seconds),
    )
    if source_updated_at is None or manifest_generated_at is None:
        raise ValueError("source_refresh_source_not_fresh")

    semantic_scope = {
        "approval_disposition": approval_disposition,
        "operation": "refresh_producer_owned_receipts",
        "requested_lanes": lanes,
    }
    semantic_digest = _sha256(_canonical(semantic_scope))
    request_staged = bool(lanes)
    request_id = f"pq-source-refresh-{semantic_digest[:24]}" if lanes else ""
    request = {
        "schema": SCHEMA,
        "status": "staged" if request_staged else "not_required",
        "request_state": (
            "producer_refresh_staged"
            if request_staged
            else "current_sources_verified"
        ),
        "generated_at": observed_now.isoformat(),
        "expires_at": (
            observed_now + timedelta(seconds=float(max_age_seconds))
        ).isoformat(),
        "blocking_reason": "",
        "next_action": (
            "route the staged work item to each named receipt producer under "
            "that producer's independently governed authority; this request "
            "does not authorize provider access, delivery, deployment, or execution"
            if request_staged
            else "await a verified source change or a newly non-current producer lane"
        ),
        "request_id": request_id,
        "semantic_request_sha256": semantic_digest,
        "source_cycle_receipt_sha256": source_digest,
        "source_status_updated_at": source_updated_at,
        "source_manifest_generated_at": manifest_generated_at,
        "approval_disposition": approval_disposition,
        "request_scope": {
            "operation": "refresh_producer_owned_receipts",
            "effect": "staged_request_only",
            "consumer_reverification_required": True,
        },
        "requested_lanes": lanes,
        "request_staged": request_staged,
        "progress": {
            **source_progress,
            "requested_lane_count": len(lanes),
            "current_evidence_verified": True,
        },
        **_safety_fields(),
    }
    return _integrity_bound(request)


def verify_source_refresh_request(
    receipt: Mapping[str, Any],
    *,
    operator_projection: Mapping[str, Any],
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    generated_at = _fresh_timestamp(
        receipt.get("generated_at"),
        now=observed_now,
        max_age_seconds=float(max_age_seconds),
    )
    expires_at = _timestamp(receipt.get("expires_at"))
    if generated_at is None or expires_at is None or expires_at < observed_now:
        return _blocked("source_refresh_request_not_fresh", now=observed_now)
    if not _integrity_verified(receipt):
        return _blocked("source_refresh_request_integrity_invalid", now=observed_now)
    try:
        expected = build_source_refresh_request(
            operator_projection,
            now=datetime.fromisoformat(generated_at),
            max_age_seconds=max_age_seconds,
        )
    except (TypeError, ValueError):
        return _blocked("source_refresh_current_projection_not_admissible", now=observed_now)
    if dict(receipt) != expected:
        return _blocked("source_refresh_request_source_binding_mismatch", now=observed_now)
    verification = {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "request_state": str(receipt.get("request_state") or ""),
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": str(receipt.get("next_action") or ""),
        "request_id": str(receipt.get("request_id") or ""),
        "request_staged": receipt.get("request_staged") is True,
        "request_expires_at": expires_at.isoformat(),
        "semantic_request_sha256": str(
            receipt.get("semantic_request_sha256") or ""
        ),
        "source_cycle_receipt_sha256": str(
            receipt.get("source_cycle_receipt_sha256") or ""
        ),
        "requested_lanes": [
            dict(value)
            for value in list(receipt.get("requested_lanes") or [])
            if isinstance(value, Mapping)
        ],
        "progress": {
            **dict(receipt.get("progress") or {}),
            "request_integrity_verified": True,
            "source_binding_verified": True,
            "current_evidence_verified": True,
        },
        **_safety_fields(),
    }
    return _integrity_bound(verification)


def _persisted_snapshot(
    path: Path,
    *,
    field: str,
) -> tuple[dict[str, Any], bytes, str]:
    target = Path(path).absolute()
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError(f"{field}_not_admissible")
    return load_strict_json_object_snapshot(
        target,
        field=field,
        maximum_bytes=MAX_RECEIPT_BYTES,
    )


def _persist_blocked_bundle(
    reason: str,
    *,
    request_target: Path,
    verification_target: Path,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    request = _blocked_request(
        reason,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    try:
        atomic_write_bytes(
            request_target,
            _canonical(request),
            overwrite=True,
        )
        persisted_request, request_raw, request_digest = _persisted_snapshot(
            request_target,
            field="source_refresh_blocked_request",
        )
        if persisted_request != request or not _integrity_verified(
            persisted_request
        ):
            raise ValueError("source_refresh_blocked_request_not_persisted")
        verification = _blocked(reason, now=now)
        verification["request_receipt_sha256"] = request_digest
        verification["progress"] = {
            **dict(verification.get("progress") or {}),
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
        verification = _integrity_bound(verification)
        atomic_write_bytes(
            verification_target,
            _canonical(verification),
            overwrite=True,
        )
        persisted_verification, verification_raw, verification_digest = (
            _persisted_snapshot(
                verification_target,
                field="source_refresh_blocked_verification",
            )
        )
        if persisted_verification != verification or not _integrity_verified(
            persisted_verification
        ):
            raise ValueError(
                "source_refresh_blocked_verification_not_persisted"
            )
    except Exception:
        result = _blocked(reason, now=now)
        result["request_path"] = str(request_target)
        result["verification_path"] = str(verification_target)
        return result
    result = dict(verification)
    result.pop("integrity", None)
    result.update(
        {
            "request_path": str(request_target),
            "verification_path": str(verification_target),
            "request_receipt_sha256": request_digest,
            "request_receipt_bytes": len(request_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
            "verification_receipt_integrity_verified": True,
        }
    )
    return result


def materialize_source_refresh_request_bundle(
    operator_projection: Mapping[str, Any],
    *,
    request_path: Path = DEFAULT_REQUEST_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    request_target = Path(request_path).absolute()
    verification_target = Path(verification_path).absolute()
    try:
        request = build_source_refresh_request(
            operator_projection,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
    except (TypeError, ValueError):
        return _persist_blocked_bundle(
            "source_refresh_current_projection_not_admissible",
            request_target=request_target,
            verification_target=verification_target,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
    try:
        atomic_write_bytes(
            request_target,
            _canonical(request),
            overwrite=True,
        )
        persisted, raw, request_digest = _persisted_snapshot(
            request_target,
            field="source_refresh_request",
        )
        if persisted != request:
            raise ValueError("source_refresh_request_persistence_mismatch")
        verification = verify_source_refresh_request(
            persisted,
            operator_projection=operator_projection,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        if verification.get("status") != "verified":
            raise ValueError("source_refresh_request_verification_failed")
        verification["progress"] = {
            **dict(verification.get("progress") or {}),
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
        verification["request_receipt_sha256"] = request_digest
        verification = _integrity_bound(
            {
                key: value
                for key, value in verification.items()
                if key != "integrity"
            }
        )
        atomic_write_bytes(
            verification_target,
            _canonical(verification),
            overwrite=True,
        )
        persisted_verification, verification_raw, verification_digest = (
            _persisted_snapshot(
                verification_target,
                field="source_refresh_request_verification",
            )
        )
        if persisted_verification != verification or not _integrity_verified(
            persisted_verification
        ):
            raise ValueError("source_refresh_verification_persistence_mismatch")
    except Exception:
        return _persist_blocked_bundle(
            "source_refresh_request_persistence_or_verification_failed",
            request_target=request_target,
            verification_target=verification_target,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
    result = dict(verification)
    result.pop("integrity", None)
    result.update(
        {
            "request_path": str(request_target),
            "verification_path": str(verification_target),
            "request_receipt_sha256": request_digest,
            "request_receipt_bytes": len(raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
            "verification_receipt_integrity_verified": True,
        }
    )
    return result


def inspect_source_refresh_request_bundle(
    operator_projection: Mapping[str, Any],
    *,
    request_path: Path = DEFAULT_REQUEST_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Verify an already staged bundle without mutating either receipt."""

    observed_now = _now(now)
    request_target = Path(request_path).absolute()
    verification_target = Path(verification_path).absolute()
    try:
        request, request_raw, request_digest = _persisted_snapshot(
            request_target,
            field="source_refresh_request",
        )
        persisted_verification, verification_raw, verification_digest = (
            _persisted_snapshot(
                verification_target,
                field="source_refresh_request_verification",
            )
        )
    except Exception:
        return _blocked(
            "source_refresh_request_bundle_not_admissible",
            now=observed_now,
        )
    current_verification = verify_source_refresh_request(
        request,
        operator_projection=operator_projection,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    verification_updated_at = _fresh_timestamp(
        persisted_verification.get("updated_at"),
        now=observed_now,
        max_age_seconds=float(max_age_seconds),
    )
    expected_progress = {
        **dict(current_verification.get("progress") or {}),
        "request_receipt_persisted": True,
        "verification_receipt_persisted": True,
    }
    current_without_variable = {
        key: value
        for key, value in current_verification.items()
        if key not in {"integrity", "progress", "updated_at"}
    }
    persisted_without_variable = {
        key: value
        for key, value in persisted_verification.items()
        if key
        not in {
            "integrity",
            "progress",
            "updated_at",
            "request_receipt_sha256",
        }
    }
    if not (
        current_verification.get("status") == "verified"
        and persisted_verification.get("schema") == VERIFY_SCHEMA
        and persisted_verification.get("status") == "verified"
        and verification_updated_at is not None
        and _integrity_verified(persisted_verification)
        and persisted_verification.get("request_receipt_sha256")
        == request_digest
        and persisted_without_variable == current_without_variable
        and dict(persisted_verification.get("progress") or {})
        == expected_progress
    ):
        return _blocked(
            "source_refresh_request_bundle_not_current",
            now=observed_now,
        )
    result = dict(persisted_verification)
    result.pop("integrity", None)
    result.update(
        {
            "request_path": str(request_target),
            "verification_path": str(verification_target),
            "request_receipt_sha256": request_digest,
            "request_receipt_bytes": len(request_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
            "verification_receipt_integrity_verified": True,
        }
    )
    return result


def materialize_current_source_refresh_request_bundle(
    *,
    cycle_receipt_path: Path = operator_status.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = operator_status.DEFAULT_SIGNAL_DIR,
    request_path: Path = DEFAULT_REQUEST_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    projection = operator_status.load_operator_status(
        cycle_receipt_path=Path(cycle_receipt_path),
        signal_dir=Path(signal_dir),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    return materialize_source_refresh_request_bundle(
        projection,
        request_path=request_path,
        verification_path=verification_path,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def inspect_current_source_refresh_request_bundle(
    *,
    cycle_receipt_path: Path = operator_status.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = operator_status.DEFAULT_SIGNAL_DIR,
    request_path: Path = DEFAULT_REQUEST_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    projection = operator_status.load_operator_status(
        cycle_receipt_path=Path(cycle_receipt_path),
        signal_dir=Path(signal_dir),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    return inspect_source_refresh_request_bundle(
        projection,
        request_path=request_path,
        verification_path=verification_path,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage and verify a reversible producer-owned source receipt "
            "refresh request without dispatch or execution authority."
        )
    )
    parser.add_argument(
        "--cycle-receipt",
        type=Path,
        default=operator_status.DEFAULT_CYCLE_RECEIPT,
    )
    parser.add_argument(
        "--signal-dir",
        type=Path,
        default=operator_status.DEFAULT_SIGNAL_DIR,
    )
    parser.add_argument("--request", type=Path, default=DEFAULT_REQUEST_PATH)
    parser.add_argument(
        "--verification",
        type=Path,
        default=DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=DEFAULT_MAX_AGE_SECONDS,
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help=(
            "Verify the already staged request and verification receipts "
            "without writing or replacing either file."
        ),
    )
    args = parser.parse_args(argv)
    operation = (
        inspect_current_source_refresh_request_bundle
        if args.inspect
        else materialize_current_source_refresh_request_bundle
    )
    result = operation(
        cycle_receipt_path=args.cycle_receipt,
        signal_dir=args.signal_dir,
        request_path=args.request,
        verification_path=args.verification,
        max_age_seconds=args.max_age_seconds,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
