#!/usr/bin/env python3
"""Publish a producer-readable, non-authorizing source-refresh handoff."""

from __future__ import annotations

import argparse
import hashlib
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
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_source_refresh_handoff.v1"
VERIFY_SCHEMA = "propertyquarry.ooda_source_refresh_handoff_verification.v1"
DEFAULT_HANDOFF_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-handoff.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-handoff-verification.json"
)
DEFAULT_MAX_AGE_SECONDS = source_refresh.DEFAULT_MAX_AGE_SECONDS
MAX_RECEIPT_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


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
        "handoff_confers_authority": False,
        "producer_claim_recorded": False,
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


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "handoff_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair and restage the current source-refresh request before "
            "producer pickup; do not dispatch or claim this blocked projection"
        ),
        "handoff_id": "",
        "request_id": "",
        "handoff_available": False,
        "work_items": [],
        "progress": {
            "current_evidence_verified": False,
            "request_binding_verified": False,
            "work_item_count": 0,
        },
        **_safety_fields(),
    }


def _private_snapshot(
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


def _source_request_is_admissible(
    request: Mapping[str, Any],
    *,
    request_receipt_sha256: str,
    request_verification_receipt_sha256: str,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    generated_at = _fresh_timestamp(
        request.get("generated_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    expires_at = _timestamp(request.get("expires_at"))
    staged = request.get("request_staged") is True
    expected_state = (
        "producer_refresh_staged" if staged else "current_sources_verified"
    )
    lanes = request.get("requested_lanes")
    return bool(
        request.get("schema") == source_refresh.SCHEMA
        and request.get("status") == ("staged" if staged else "not_required")
        and request.get("request_state") == expected_state
        and generated_at is not None
        and expires_at is not None
        and expires_at >= now
        and _integrity_verified(request)
        and _SHA256.fullmatch(str(request_receipt_sha256 or ""))
        and _SHA256.fullmatch(
            str(request_verification_receipt_sha256 or "")
        )
        and _sha256(_canonical(request)) == request_receipt_sha256
        and _SHA256.fullmatch(
            str(request.get("source_cycle_receipt_sha256") or "")
        )
        and _SHA256.fullmatch(
            str(request.get("semantic_request_sha256") or "")
        )
        and isinstance(lanes, list)
        and bool(lanes) is staged
        and request.get("action_required") is False
        and request.get("interrupt_operator") is False
        and request.get("producer_dispatch_authorized") is False
        and request.get("producer_refresh_authorized") is False
        and request.get("automatic_source_refresh_allowed") is False
        and request.get("automatic_execution_allowed") is False
        and request.get("execution_authorized") is False
        and request.get("deployment_or_restart_authorized") is False
        and request.get("protected_operation_executed") is False
        and request.get("provider_quota_consumption_allowed") is False
        and request.get("delivery_authorized") is False
        and request.get("delivery_attempted") is False
        and request.get("sent") is False
        and request.get("secret_values_recorded") is False
    )


def _work_items(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    semantic_request_sha256 = str(
        request.get("semantic_request_sha256") or ""
    )
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(request.get("requested_lanes") or []):
        if not isinstance(raw, Mapping):
            raise ValueError("source_refresh_handoff_lane_not_admissible")
        lane = str(raw.get("lane") or "").strip()
        expected_artifacts = source_refresh._LANE_ARTIFACTS.get(lane)
        source_artifacts = list(raw.get("source_artifacts") or [])
        if not (
            lane
            and lane not in seen
            and expected_artifacts is not None
            and source_artifacts == expected_artifacts
            and raw.get("producer_authority") == "external_receipt_producer"
            and raw.get("requested_operation") == "refresh_receipts"
            and raw.get("current_reverification_required") is True
        ):
            raise ValueError("source_refresh_handoff_lane_not_admissible")
        seen.add(lane)
        semantic_item = {
            "semantic_request_sha256": semantic_request_sha256,
            "lane": lane,
            "required_artifacts": source_artifacts,
        }
        item_digest = _sha256(_canonical(semantic_item))
        items.append(
            {
                "work_item_id": f"pq-source-refresh-work-{item_digest[:24]}",
                "work_item_state": "available_for_independent_pickup",
                "lane": lane,
                "reason": str(raw.get("reason") or ""),
                "source_generated_at": str(
                    raw.get("source_generated_at") or ""
                ),
                "producer_kind": "external_receipt_producer",
                "requested_operation": "refresh_receipts",
                "required_artifacts": source_artifacts,
                "pickup_contract": {
                    "mode": "read_only_artifact",
                    "claim_receipt_observed": False,
                    "authority_source": "independent_producer_governance",
                    "handoff_confers_authority": False,
                },
                "settlement_contract": {
                    "consumer": "approved_ooda_cycle",
                    "required_lane_status": "current",
                    "required_artifacts": source_artifacts,
                    "current_reverification_required": True,
                },
            }
        )
    items.sort(key=lambda item: str(item["lane"]))
    return items


def build_source_refresh_handoff(
    source_refresh_request: Mapping[str, Any],
    *,
    request_receipt_sha256: str,
    request_verification_receipt_sha256: str,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    if not (
        math.isfinite(float(max_age_seconds))
        and 60.0 <= float(max_age_seconds) <= 86400.0
        and _source_request_is_admissible(
            source_refresh_request,
            request_receipt_sha256=request_receipt_sha256,
            request_verification_receipt_sha256=(
                request_verification_receipt_sha256
            ),
            now=observed_now,
            max_age_seconds=float(max_age_seconds),
        )
    ):
        raise ValueError("source_refresh_handoff_request_not_admissible")

    items = _work_items(source_refresh_request)
    staged = source_refresh_request.get("request_staged") is True
    semantic_request_sha256 = str(
        source_refresh_request.get("semantic_request_sha256") or ""
    )
    handoff_id = (
        f"pq-source-refresh-handoff-{semantic_request_sha256[:24]}"
        if staged
        else ""
    )
    handoff: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "available" if staged else "not_required",
        "handoff_state": (
            "producer_pickup_available"
            if staged
            else "current_sources_verified"
        ),
        "generated_at": observed_now.isoformat(),
        "expires_at": str(source_refresh_request.get("expires_at") or ""),
        "blocking_reason": "",
        "next_action": (
            "an independently governed producer may pick up each work item, "
            "refresh only its named artifacts, and let the next approved OODA "
            "cycle verify settlement; this handoff grants no authority"
            if staged
            else "await a newly non-current producer lane"
        ),
        "handoff_id": handoff_id,
        "request_id": str(source_refresh_request.get("request_id") or ""),
        "semantic_request_sha256": semantic_request_sha256,
        "request_receipt_sha256": request_receipt_sha256,
        "request_verification_receipt_sha256": (
            request_verification_receipt_sha256
        ),
        "source_cycle_receipt_sha256": str(
            source_refresh_request.get("source_cycle_receipt_sha256") or ""
        ),
        "handoff_available": staged,
        "work_items": items,
        "progress": {
            "work_item_count": len(items),
            "available_work_item_count": len(items),
            "claimed_work_item_count": 0,
            "settled_work_item_count": 0,
            "current_evidence_verified": True,
            "request_binding_verified": True,
        },
        **_safety_fields(),
    }
    return _integrity_bound(handoff)


def verify_source_refresh_handoff(
    handoff: Mapping[str, Any],
    *,
    source_refresh_request: Mapping[str, Any],
    request_receipt_sha256: str,
    request_verification_receipt_sha256: str,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    generated_at = _fresh_timestamp(
        handoff.get("generated_at"),
        now=observed_now,
        max_age_seconds=float(max_age_seconds),
    )
    expires_at = _timestamp(handoff.get("expires_at"))
    if generated_at is None or expires_at is None or expires_at < observed_now:
        return _blocked("source_refresh_handoff_not_fresh", now=observed_now)
    if not _integrity_verified(handoff):
        return _blocked("source_refresh_handoff_integrity_invalid", now=observed_now)
    try:
        expected = build_source_refresh_handoff(
            source_refresh_request,
            request_receipt_sha256=request_receipt_sha256,
            request_verification_receipt_sha256=(
                request_verification_receipt_sha256
            ),
            now=datetime.fromisoformat(generated_at),
            max_age_seconds=max_age_seconds,
        )
    except (TypeError, ValueError):
        return _blocked(
            "source_refresh_handoff_request_not_admissible",
            now=observed_now,
        )
    if dict(handoff) != expected:
        return _blocked(
            "source_refresh_handoff_request_binding_mismatch",
            now=observed_now,
        )
    verification = {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "handoff_state": str(handoff.get("handoff_state") or ""),
        "updated_at": observed_now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "blocking_reason": "",
        "next_action": str(handoff.get("next_action") or ""),
        "handoff_id": str(handoff.get("handoff_id") or ""),
        "request_id": str(handoff.get("request_id") or ""),
        "semantic_request_sha256": str(
            handoff.get("semantic_request_sha256") or ""
        ),
        "request_receipt_sha256": request_receipt_sha256,
        "request_verification_receipt_sha256": (
            request_verification_receipt_sha256
        ),
        "source_cycle_receipt_sha256": str(
            handoff.get("source_cycle_receipt_sha256") or ""
        ),
        "handoff_available": handoff.get("handoff_available") is True,
        "work_items": [
            dict(item)
            for item in list(handoff.get("work_items") or [])
            if isinstance(item, Mapping)
        ],
        "progress": {
            **dict(handoff.get("progress") or {}),
            "handoff_integrity_verified": True,
            "request_binding_verified": True,
            "current_evidence_verified": True,
        },
        **_safety_fields(),
    }
    return _integrity_bound(verification)


def _current_source_request(
    operator_projection: Mapping[str, Any],
    *,
    request_path: Path,
    request_verification_path: Path,
    now: datetime,
    max_age_seconds: float,
) -> tuple[dict[str, Any], str, str]:
    inspection = source_refresh.inspect_source_refresh_request_bundle(
        operator_projection,
        request_path=request_path,
        verification_path=request_verification_path,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    if not (
        inspection.get("status") == "verified"
        and inspection.get("request_state")
        in {"producer_refresh_staged", "current_sources_verified"}
        and dict(inspection.get("progress") or {}).get(
            "request_integrity_verified"
        )
        is True
        and dict(inspection.get("progress") or {}).get(
            "source_binding_verified"
        )
        is True
        and inspection.get("producer_dispatch_authorized") is False
        and inspection.get("provider_quota_consumption_allowed") is False
        and inspection.get("delivery_authorized") is False
    ):
        raise ValueError("source_refresh_handoff_request_bundle_not_current")
    request, _raw, request_digest = _private_snapshot(
        request_path,
        field="source_refresh_handoff_source_request",
    )
    _verification, _verification_raw, verification_digest = _private_snapshot(
        request_verification_path,
        field="source_refresh_handoff_source_verification",
    )
    if not (
        request_digest == inspection.get("request_receipt_sha256")
        and verification_digest
        == inspection.get("verification_receipt_sha256")
    ):
        raise ValueError("source_refresh_handoff_request_snapshot_changed")
    return request, request_digest, verification_digest


def _persist_blocked_bundle(
    reason: str,
    *,
    handoff_path: Path,
    verification_path: Path,
    now: datetime,
) -> dict[str, Any]:
    handoff = _integrity_bound(
        {
            **_blocked(reason, now=now),
            "schema": SCHEMA,
            "status": "blocked",
            "generated_at": now.isoformat(),
            "expires_at": now.isoformat(),
        }
    )
    verification = _integrity_bound(_blocked(reason, now=now))
    try:
        atomic_write_bytes(handoff_path, _canonical(handoff), overwrite=True)
        persisted_handoff, handoff_raw, handoff_digest = _private_snapshot(
            handoff_path,
            field="source_refresh_blocked_handoff",
        )
        if persisted_handoff != handoff or not _integrity_verified(
            persisted_handoff
        ):
            raise ValueError("source_refresh_blocked_handoff_not_persisted")
        verification["handoff_receipt_sha256"] = handoff_digest
        verification = _integrity_bound(
            {key: value for key, value in verification.items() if key != "integrity"}
        )
        atomic_write_bytes(
            verification_path,
            _canonical(verification),
            overwrite=True,
        )
        persisted_verification, verification_raw, verification_digest = (
            _private_snapshot(
                verification_path,
                field="source_refresh_blocked_handoff_verification",
            )
        )
        if persisted_verification != verification or not _integrity_verified(
            persisted_verification
        ):
            raise ValueError(
                "source_refresh_blocked_handoff_verification_not_persisted"
            )
    except Exception:
        result = _blocked(reason, now=now)
        result["handoff_path"] = str(handoff_path)
        result["verification_path"] = str(verification_path)
        return result
    result = dict(verification)
    result.pop("integrity", None)
    result.update(
        {
            "handoff_path": str(handoff_path),
            "verification_path": str(verification_path),
            "handoff_receipt_sha256": handoff_digest,
            "handoff_receipt_bytes": len(handoff_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "handoff_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def materialize_source_refresh_handoff_bundle(
    operator_projection: Mapping[str, Any],
    *,
    request_path: Path = source_refresh.DEFAULT_REQUEST_PATH,
    request_verification_path: Path = source_refresh.DEFAULT_VERIFICATION_PATH,
    handoff_path: Path = DEFAULT_HANDOFF_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    handoff_target = Path(handoff_path).absolute()
    verification_target = Path(verification_path).absolute()
    try:
        request, request_digest, request_verification_digest = (
            _current_source_request(
                operator_projection,
                request_path=Path(request_path).absolute(),
                request_verification_path=Path(
                    request_verification_path
                ).absolute(),
                now=observed_now,
                max_age_seconds=max_age_seconds,
            )
        )
        handoff = build_source_refresh_handoff(
            request,
            request_receipt_sha256=request_digest,
            request_verification_receipt_sha256=(
                request_verification_digest
            ),
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        atomic_write_bytes(
            handoff_target,
            _canonical(handoff),
            overwrite=True,
        )
        persisted, handoff_raw, handoff_digest = _private_snapshot(
            handoff_target,
            field="source_refresh_handoff",
        )
        if persisted != handoff:
            raise ValueError("source_refresh_handoff_persistence_mismatch")
        verification = verify_source_refresh_handoff(
            persisted,
            source_refresh_request=request,
            request_receipt_sha256=request_digest,
            request_verification_receipt_sha256=(
                request_verification_digest
            ),
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        if verification.get("status") != "verified":
            raise ValueError("source_refresh_handoff_verification_failed")
        verification["handoff_receipt_sha256"] = handoff_digest
        verification["progress"] = {
            **dict(verification.get("progress") or {}),
            "handoff_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
        verification = _integrity_bound(
            {key: value for key, value in verification.items() if key != "integrity"}
        )
        atomic_write_bytes(
            verification_target,
            _canonical(verification),
            overwrite=True,
        )
        persisted_verification, verification_raw, verification_digest = (
            _private_snapshot(
                verification_target,
                field="source_refresh_handoff_verification",
            )
        )
        if persisted_verification != verification or not _integrity_verified(
            persisted_verification
        ):
            raise ValueError(
                "source_refresh_handoff_verification_persistence_mismatch"
            )
    except Exception:
        return _persist_blocked_bundle(
            "source_refresh_handoff_source_or_persistence_not_admissible",
            handoff_path=handoff_target,
            verification_path=verification_target,
            now=observed_now,
        )
    result = dict(verification)
    result.pop("integrity", None)
    result.update(
        {
            "handoff_path": str(handoff_target),
            "verification_path": str(verification_target),
            "handoff_receipt_sha256": handoff_digest,
            "handoff_receipt_bytes": len(handoff_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "handoff_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def inspect_source_refresh_handoff_bundle(
    operator_projection: Mapping[str, Any],
    *,
    request_path: Path = source_refresh.DEFAULT_REQUEST_PATH,
    request_verification_path: Path = source_refresh.DEFAULT_VERIFICATION_PATH,
    handoff_path: Path = DEFAULT_HANDOFF_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Verify an existing handoff bundle without replacing any receipt."""

    observed_now = _now(now)
    try:
        request, request_digest, request_verification_digest = (
            _current_source_request(
                operator_projection,
                request_path=Path(request_path).absolute(),
                request_verification_path=Path(
                    request_verification_path
                ).absolute(),
                now=observed_now,
                max_age_seconds=max_age_seconds,
            )
        )
        handoff, handoff_raw, handoff_digest = _private_snapshot(
            handoff_path,
            field="source_refresh_handoff",
        )
        persisted_verification, verification_raw, verification_digest = (
            _private_snapshot(
                verification_path,
                field="source_refresh_handoff_verification",
            )
        )
        current_verification = verify_source_refresh_handoff(
            handoff,
            source_refresh_request=request,
            request_receipt_sha256=request_digest,
            request_verification_receipt_sha256=(
                request_verification_digest
            ),
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        expected_progress = {
            **dict(current_verification.get("progress") or {}),
            "handoff_receipt_persisted": True,
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
                "handoff_receipt_sha256",
            }
        }
        verification_updated_at = _fresh_timestamp(
            persisted_verification.get("updated_at"),
            now=observed_now,
            max_age_seconds=float(max_age_seconds),
        )
        if not (
            current_verification.get("status") == "verified"
            and persisted_verification.get("schema") == VERIFY_SCHEMA
            and persisted_verification.get("status") == "verified"
            and verification_updated_at is not None
            and _integrity_verified(persisted_verification)
            and persisted_verification.get("handoff_receipt_sha256")
            == handoff_digest
            and persisted_without_variable == current_without_variable
            and dict(persisted_verification.get("progress") or {})
            == expected_progress
        ):
            raise ValueError("source_refresh_handoff_bundle_not_current")
    except Exception:
        return _blocked(
            "source_refresh_handoff_bundle_not_admissible",
            now=observed_now,
        )
    result = dict(persisted_verification)
    result.pop("integrity", None)
    result.update(
        {
            "handoff_path": str(Path(handoff_path).absolute()),
            "verification_path": str(Path(verification_path).absolute()),
            "handoff_receipt_sha256": handoff_digest,
            "handoff_receipt_bytes": len(handoff_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "handoff_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def materialize_current_source_refresh_handoff_bundle(
    *,
    cycle_receipt_path: Path = operator_status.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = operator_status.DEFAULT_SIGNAL_DIR,
    request_path: Path = source_refresh.DEFAULT_REQUEST_PATH,
    request_verification_path: Path = source_refresh.DEFAULT_VERIFICATION_PATH,
    handoff_path: Path = DEFAULT_HANDOFF_PATH,
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
    return materialize_source_refresh_handoff_bundle(
        projection,
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=handoff_path,
        verification_path=verification_path,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def inspect_current_source_refresh_handoff_bundle(
    *,
    cycle_receipt_path: Path = operator_status.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = operator_status.DEFAULT_SIGNAL_DIR,
    request_path: Path = source_refresh.DEFAULT_REQUEST_PATH,
    request_verification_path: Path = source_refresh.DEFAULT_VERIFICATION_PATH,
    handoff_path: Path = DEFAULT_HANDOFF_PATH,
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
    return inspect_source_refresh_handoff_bundle(
        projection,
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=handoff_path,
        verification_path=verification_path,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage or inspect a producer-readable source-refresh handoff "
            "without granting dispatch, provider, delivery, or execution authority."
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
    parser.add_argument(
        "--request",
        type=Path,
        default=source_refresh.DEFAULT_REQUEST_PATH,
    )
    parser.add_argument(
        "--request-verification",
        type=Path,
        default=source_refresh.DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument("--handoff", type=Path, default=DEFAULT_HANDOFF_PATH)
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
        help="Verify existing receipts without writing or replacing them.",
    )
    args = parser.parse_args(argv)
    operation = (
        inspect_current_source_refresh_handoff_bundle
        if args.inspect
        else materialize_current_source_refresh_handoff_bundle
    )
    result = operation(
        cycle_receipt_path=args.cycle_receipt,
        signal_dir=args.signal_dir,
        request_path=args.request,
        request_verification_path=args.request_verification,
        handoff_path=args.handoff,
        verification_path=args.verification,
        max_age_seconds=args.max_age_seconds,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
