#!/usr/bin/env python3
"""Observe authenticated producer claims without granting execution authority."""

from __future__ import annotations

import argparse
import base64
import binascii
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

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


CLAIM_SCHEMA = "propertyquarry.ooda_source_refresh_producer_claim.v1"
TRUST_SCHEMA = "propertyquarry.ooda_source_refresh_producer_trust.v1"
SCHEMA = "propertyquarry.ooda_source_refresh_claim_lifecycle.v1"
VERIFY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_claim_lifecycle_verification.v1"
)
DEFAULT_TRUST_REGISTRY_PATH = Path(
    "config/propertyquarry_ooda_source_refresh_producer_trust.v1.json"
)
DEFAULT_CLAIM_DIR = Path(
    "_completion/propertyquarry_ooda_producer_claims"
)
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-claims.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-claims-verification.json"
)
DEFAULT_MAX_AGE_SECONDS = source_handoff.DEFAULT_MAX_AGE_SECONDS
MAX_CLAIM_TTL_SECONDS = 3600
MIN_CLAIM_TTL_SECONDS = 60
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_TRUST_REGISTRY_BYTES = 256 * 1024
MAX_CLAIM_FILES = 128
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}\Z")
_WORK_ITEM_ID = re.compile(r"pq-source-refresh-work-[0-9a-f]{24}\Z")
_CLAIM_ID = re.compile(r"pqscr_claim_[0-9a-f]{24}\Z")
_NONCE = re.compile(r"[A-Za-z0-9_-]{22,128}\Z")


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
        "claim_confers_authority": False,
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
        "claim_state": "blocked",
        "settlement_state": "unverified",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair the external claim evidence or trust registry, then "
            "reinspect; do not infer a producer claim or settlement"
        ),
        "handoff_id": "",
        "request_id": "",
        "producer_claim_recorded": False,
        "claims": [],
        "progress": {
            "current_evidence_verified": False,
            "handoff_binding_verified": False,
            "claim_count": 0,
            "expected_claim_count": 0,
        },
        **_safety_fields(),
    }


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{label}_fields_not_admissible")


def _decode_base64url(
    value: object,
    *,
    expected_bytes: int,
    label: str,
) -> bytes:
    text = str(value or "")
    if not text or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise ValueError(f"{label}_not_admissible")
    padding = "=" * ((4 - len(text) % 4) % 4)
    try:
        decoded = base64.b64decode(
            text + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label}_not_admissible") from exc
    if (
        len(decoded) != expected_bytes
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
        != text
    ):
        raise ValueError(f"{label}_not_admissible")
    return decoded


def _private_snapshot(
    path: Path,
    *,
    field: str,
    maximum_bytes: int = MAX_RECEIPT_BYTES,
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
        maximum_bytes=maximum_bytes,
    )


def _read_only_snapshot(
    path: Path,
    *,
    field: str,
    maximum_bytes: int,
) -> tuple[dict[str, Any], bytes, str]:
    target = Path(path).absolute()
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError(f"{field}_not_admissible")
    return load_strict_json_object_snapshot(
        target,
        field=field,
        maximum_bytes=maximum_bytes,
    )


def load_producer_trust_registry(
    path: Path = DEFAULT_TRUST_REGISTRY_PATH,
) -> dict[str, Any]:
    registry, _raw, registry_digest = _read_only_snapshot(
        path,
        field="source_refresh_producer_trust_registry",
        maximum_bytes=MAX_TRUST_REGISTRY_BYTES,
    )
    _require_exact_keys(
        registry,
        {"schema", "status", "rotation_epoch", "producers", "integrity"},
        label="source_refresh_producer_trust_registry",
    )
    if not _integrity_verified(registry):
        raise ValueError("source_refresh_producer_trust_integrity_invalid")
    status_value = str(registry.get("status") or "")
    rotation_epoch = registry.get("rotation_epoch")
    producers = registry.get("producers")
    if not (
        registry.get("schema") == TRUST_SCHEMA
        and status_value in {"UNCONFIGURED", "ACTIVE"}
        and isinstance(rotation_epoch, int)
        and not isinstance(rotation_epoch, bool)
        and 0 <= rotation_epoch <= 2**31 - 1
        and isinstance(producers, list)
        and len(producers) <= 32
        and (status_value != "UNCONFIGURED" or not producers)
        and (status_value != "UNCONFIGURED" or rotation_epoch == 0)
        and (status_value != "ACTIVE" or rotation_epoch > 0)
    ):
        raise ValueError("source_refresh_producer_trust_not_admissible")
    normalized: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    active_count = 0
    for raw in producers:
        if not isinstance(raw, Mapping):
            raise ValueError("source_refresh_producer_trust_entry_not_admissible")
        _require_exact_keys(
            raw,
            {
                "producer_id",
                "key_id",
                "algorithm",
                "public_key",
                "public_key_sha256",
                "lanes",
                "status",
            },
            label="source_refresh_producer_trust_entry",
        )
        producer_id = str(raw.get("producer_id") or "")
        key_id = str(raw.get("key_id") or "")
        entry_status = str(raw.get("status") or "")
        lanes = raw.get("lanes")
        public_key = _decode_base64url(
            raw.get("public_key"),
            expected_bytes=32,
            label="source_refresh_producer_public_key",
        )
        identity = (producer_id, key_id)
        if not (
            _IDENTIFIER.fullmatch(producer_id)
            and _IDENTIFIER.fullmatch(key_id)
            and identity not in identities
            and raw.get("algorithm") == "Ed25519"
            and raw.get("public_key_sha256") == _sha256(public_key)
            and isinstance(lanes, list)
            and lanes
            and len(lanes) == len(set(lanes))
            and all(lane in source_refresh._LANE_ARTIFACTS for lane in lanes)
            and entry_status in {"ACTIVE", "REVOKED"}
        ):
            raise ValueError("source_refresh_producer_trust_entry_not_admissible")
        identities.add(identity)
        if entry_status == "ACTIVE":
            active_count += 1
        normalized.append(
            {
                "producer_id": producer_id,
                "key_id": key_id,
                "algorithm": "Ed25519",
                "public_key": str(raw["public_key"]),
                "public_key_sha256": str(raw["public_key_sha256"]),
                "lanes": sorted(str(lane) for lane in lanes),
                "status": entry_status,
            }
        )
    if status_value == "ACTIVE" and active_count == 0:
        raise ValueError("source_refresh_producer_trust_not_admissible")
    return {
        "status": status_value,
        "rotation_epoch": rotation_epoch,
        "producers": normalized,
        "trust_registry_sha256": registry_digest,
    }


def producer_trust_posture(
    trust_registry: Mapping[str, Any],
    *,
    required_lanes: list[str] | tuple[str, ...] | set[str],
) -> dict[str, Any]:
    """Project whether public-key trust can authenticate every required lane."""

    required = sorted(set(str(lane or "").strip() for lane in required_lanes))
    if any(lane not in source_refresh._LANE_ARTIFACTS for lane in required):
        raise ValueError("source_refresh_producer_trust_required_lane_invalid")
    producers = trust_registry.get("producers")
    if not isinstance(producers, list):
        raise ValueError("source_refresh_producer_trust_entries_invalid")
    active = [
        producer
        for producer in producers
        if isinstance(producer, Mapping) and producer.get("status") == "ACTIVE"
    ]
    trusted_lanes = sorted(
        {
            str(lane)
            for producer in active
            for lane in list(producer.get("lanes") or [])
            if str(lane) in source_refresh._LANE_ARTIFACTS
        }
    )
    missing_lanes = sorted(set(required) - set(trusted_lanes))
    configured = bool(
        trust_registry.get("status") == "ACTIVE" and active
    )
    return {
        "configured": configured,
        "active_producer_count": len(active),
        "required_lanes": required,
        "trusted_lanes": trusted_lanes,
        "missing_lanes": missing_lanes,
        "producer_trust_ready": bool(configured and not missing_lanes),
    }


def _claim_directory(
    path: Path,
    *,
    require_present: bool,
) -> tuple[bool, dict[str, Path]]:
    target = Path(path).absolute()
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        if require_present:
            raise ValueError("source_refresh_claim_directory_unavailable")
        return False, {}
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError("source_refresh_claim_directory_not_admissible")
    claims: dict[str, Path] = {}
    with os.scandir(target) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_CLAIM_FILES:
                raise ValueError("source_refresh_claim_directory_too_large")
            if not entry.is_file(follow_symlinks=False) or entry.is_symlink():
                raise ValueError("source_refresh_claim_entry_not_admissible")
            if not entry.name.endswith(".json"):
                raise ValueError("source_refresh_claim_entry_not_admissible")
            work_item_id = entry.name[:-5]
            if not _WORK_ITEM_ID.fullmatch(work_item_id):
                raise ValueError("source_refresh_claim_entry_not_admissible")
            if work_item_id in claims:
                raise ValueError("source_refresh_claim_entry_duplicate")
            claims[work_item_id] = Path(entry.path)
    return True, claims


def _trusted_producer(
    registry: Mapping[str, Any],
    *,
    producer_id: str,
    key_id: str,
    lane: str,
) -> dict[str, Any]:
    if registry.get("status") != "ACTIVE":
        raise ValueError("source_refresh_claim_trust_unconfigured")
    matches = [
        dict(entry)
        for entry in list(registry.get("producers") or [])
        if isinstance(entry, Mapping)
        and entry.get("producer_id") == producer_id
        and entry.get("key_id") == key_id
    ]
    if len(matches) != 1:
        raise ValueError("source_refresh_claim_producer_not_trusted")
    trusted = matches[0]
    if trusted.get("status") != "ACTIVE" or lane not in list(
        trusted.get("lanes") or []
    ):
        raise ValueError("source_refresh_claim_producer_not_trusted")
    return trusted


def verify_producer_claim(
    claim: Mapping[str, Any],
    *,
    work_item: Mapping[str, Any],
    handoff: Mapping[str, Any],
    handoff_receipt_sha256: str,
    trust_registry: Mapping[str, Any],
    claim_receipt_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    _require_exact_keys(
        claim,
        {
            "schema",
            "issuer",
            "key_id",
            "claim_id",
            "handoff_id",
            "handoff_receipt_sha256",
            "request_id",
            "semantic_request_sha256",
            "work_item_id",
            "lane",
            "requested_operation",
            "required_artifacts",
            "issued_at",
            "expires_at",
            "nonce",
            "claim",
            "signature",
        },
        label="source_refresh_producer_claim",
    )
    claim_scope = claim.get("claim")
    if not isinstance(claim_scope, Mapping):
        raise ValueError("source_refresh_producer_claim_scope_not_admissible")
    _require_exact_keys(
        claim_scope,
        {
            "state",
            "completion_receipt_required",
            "consumer_reverification_required",
            "claim_confers_authority",
            "provider_operation_authorized",
            "delivery_authorized",
        },
        label="source_refresh_producer_claim_scope",
    )
    producer_id = str(claim.get("issuer") or "")
    key_id = str(claim.get("key_id") or "")
    claim_id = str(claim.get("claim_id") or "")
    nonce = str(claim.get("nonce") or "")
    lane = str(work_item.get("lane") or "")
    issued_at = _timestamp(claim.get("issued_at"))
    expires_at = _timestamp(claim.get("expires_at"))
    if issued_at is None or expires_at is None:
        raise ValueError("source_refresh_producer_claim_timestamp_invalid")
    ttl_seconds = (expires_at - issued_at).total_seconds()
    age_seconds = (observed_now - issued_at).total_seconds()
    if not (
        math.isfinite(ttl_seconds)
        and MIN_CLAIM_TTL_SECONDS <= ttl_seconds <= MAX_CLAIM_TTL_SECONDS
        and math.isfinite(age_seconds)
        and -30.0 <= age_seconds <= MAX_CLAIM_TTL_SECONDS
        and observed_now <= expires_at
        and claim.get("schema") == CLAIM_SCHEMA
        and _IDENTIFIER.fullmatch(producer_id)
        and _IDENTIFIER.fullmatch(key_id)
        and _CLAIM_ID.fullmatch(claim_id)
        and _NONCE.fullmatch(nonce)
        and _SHA256.fullmatch(str(claim_receipt_sha256 or ""))
        and claim.get("handoff_id") == handoff.get("handoff_id")
        and claim.get("handoff_receipt_sha256")
        == handoff_receipt_sha256
        and claim.get("request_id") == handoff.get("request_id")
        and claim.get("semantic_request_sha256")
        == handoff.get("semantic_request_sha256")
        and claim.get("work_item_id") == work_item.get("work_item_id")
        and claim.get("lane") == lane
        and claim.get("requested_operation") == "refresh_receipts"
        and claim.get("required_artifacts")
        == work_item.get("required_artifacts")
        and claim_scope.get("state") == "claimed"
        and claim_scope.get("completion_receipt_required") is True
        and claim_scope.get("consumer_reverification_required") is True
        and claim_scope.get("claim_confers_authority") is False
        and claim_scope.get("provider_operation_authorized") is False
        and claim_scope.get("delivery_authorized") is False
    ):
        raise ValueError("source_refresh_producer_claim_binding_invalid")
    trusted = _trusted_producer(
        trust_registry,
        producer_id=producer_id,
        key_id=key_id,
        lane=lane,
    )
    signature = _decode_base64url(
        claim.get("signature"),
        expected_bytes=64,
        label="source_refresh_producer_claim_signature",
    )
    signed_payload = {
        key: value for key, value in claim.items() if key != "signature"
    }
    public_key = _decode_base64url(
        trusted.get("public_key"),
        expected_bytes=32,
        label="source_refresh_producer_public_key",
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            _canonical(signed_payload),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("source_refresh_producer_claim_signature_invalid") from exc
    return {
        "status": "verified",
        "claim_state": "claimed",
        "claim_id": claim_id,
        "producer_id": producer_id,
        "key_id": key_id,
        "lane": lane,
        "work_item_id": str(work_item.get("work_item_id") or ""),
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "claim_receipt_sha256": claim_receipt_sha256,
        "signed_payload_sha256": _sha256(_canonical(signed_payload)),
        "nonce_sha256": _sha256(nonce.encode("utf-8")),
        "trust_registry_sha256": str(
            trust_registry.get("trust_registry_sha256") or ""
        ),
        "trust_rotation_epoch": int(
            trust_registry.get("rotation_epoch") or 0
        ),
        "completion_receipt_required": True,
        "consumer_reverification_required": True,
        **_safety_fields(),
    }


def _current_handoff(
    operator_projection: Mapping[str, Any],
    *,
    request_path: Path,
    request_verification_path: Path,
    handoff_path: Path,
    handoff_verification_path: Path,
    now: datetime,
    max_age_seconds: float,
) -> tuple[dict[str, Any], str, str]:
    inspection = source_handoff.inspect_source_refresh_handoff_bundle(
        operator_projection,
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=handoff_path,
        verification_path=handoff_verification_path,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    if not (
        inspection.get("status") == "verified"
        and inspection.get("handoff_state")
        in {"producer_pickup_available", "current_sources_verified"}
        and dict(inspection.get("progress") or {}).get(
            "handoff_integrity_verified"
        )
        is True
        and dict(inspection.get("progress") or {}).get(
            "request_binding_verified"
        )
        is True
        and inspection.get("handoff_confers_authority") is False
        and inspection.get("producer_dispatch_authorized") is False
        and inspection.get("provider_quota_consumption_allowed") is False
        and inspection.get("delivery_authorized") is False
    ):
        raise ValueError("source_refresh_claim_handoff_not_current")
    handoff, _raw, handoff_digest = _private_snapshot(
        handoff_path,
        field="source_refresh_claim_source_handoff",
    )
    _verification, _verification_raw, verification_digest = _private_snapshot(
        handoff_verification_path,
        field="source_refresh_claim_source_handoff_verification",
    )
    if not (
        handoff_digest == inspection.get("handoff_receipt_sha256")
        and verification_digest
        == inspection.get("verification_receipt_sha256")
    ):
        raise ValueError("source_refresh_claim_handoff_snapshot_changed")
    return handoff, handoff_digest, verification_digest


def build_claim_lifecycle(
    handoff: Mapping[str, Any],
    *,
    handoff_receipt_sha256: str,
    handoff_verification_receipt_sha256: str,
    trust_registry: Mapping[str, Any],
    claim_dir: Path,
    require_claim_dir: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    generated_at = _fresh_timestamp(
        handoff.get("generated_at"),
        now=observed_now,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
    )
    expires_at = _timestamp(handoff.get("expires_at"))
    work_items = handoff.get("work_items")
    if not (
        handoff.get("schema") == source_handoff.SCHEMA
        and handoff.get("handoff_state")
        in {"producer_pickup_available", "current_sources_verified"}
        and generated_at is not None
        and expires_at is not None
        and expires_at >= observed_now
        and _integrity_verified(handoff)
        and _SHA256.fullmatch(str(handoff_receipt_sha256 or ""))
        and _sha256(_canonical(handoff)) == handoff_receipt_sha256
        and _SHA256.fullmatch(
            str(handoff_verification_receipt_sha256 or "")
        )
        and isinstance(work_items, list)
        and handoff.get("handoff_available") is bool(work_items)
        and handoff.get("handoff_confers_authority") is False
        and handoff.get("producer_dispatch_authorized") is False
        and handoff.get("provider_quota_consumption_allowed") is False
        and handoff.get("delivery_authorized") is False
    ):
        raise ValueError("source_refresh_claim_handoff_not_admissible")
    directory_present, candidate_paths = _claim_directory(
        claim_dir,
        require_present=require_claim_dir,
    )
    expected: dict[str, dict[str, Any]] = {}
    for raw in work_items:
        if not isinstance(raw, Mapping):
            raise ValueError("source_refresh_claim_work_item_not_admissible")
        work_item_id = str(raw.get("work_item_id") or "")
        if not (
            _WORK_ITEM_ID.fullmatch(work_item_id)
            and work_item_id not in expected
            and raw.get("work_item_state")
            == "available_for_independent_pickup"
            and raw.get("requested_operation") == "refresh_receipts"
            and raw.get("lane") in source_refresh._LANE_ARTIFACTS
            and raw.get("required_artifacts")
            == source_refresh._LANE_ARTIFACTS[str(raw.get("lane") or "")]
        ):
            raise ValueError("source_refresh_claim_work_item_not_admissible")
        expected[work_item_id] = dict(raw)

    verified_claims: list[dict[str, Any]] = []
    claim_ids: set[str] = set()
    nonce_digests: set[str] = set()
    for work_item_id, work_item in sorted(expected.items()):
        claim_path = candidate_paths.get(work_item_id)
        if claim_path is None:
            continue
        claim, _claim_raw, claim_digest = _read_only_snapshot(
            claim_path,
            field="source_refresh_producer_claim",
            maximum_bytes=MAX_RECEIPT_BYTES,
        )
        verified = verify_producer_claim(
            claim,
            work_item=work_item,
            handoff=handoff,
            handoff_receipt_sha256=handoff_receipt_sha256,
            trust_registry=trust_registry,
            claim_receipt_sha256=claim_digest,
            now=observed_now,
        )
        if (
            verified["claim_id"] in claim_ids
            or verified["nonce_sha256"] in nonce_digests
        ):
            raise ValueError("source_refresh_producer_claim_replay_detected")
        claim_ids.add(str(verified["claim_id"]))
        nonce_digests.add(str(verified["nonce_sha256"]))
        verified["claim_path"] = str(claim_path.absolute())
        verified_claims.append(verified)

    expected_count = len(expected)
    claim_count = len(verified_claims)
    unmatched_count = len(set(candidate_paths) - set(expected))
    trust_posture = producer_trust_posture(
        trust_registry,
        required_lanes=[str(item.get("lane") or "") for item in expected.values()],
    )
    if not expected_count:
        claim_state = "not_required"
        settlement_state = "current_evidence_verified"
        next_action = "continue monitoring approved source evidence"
    elif not trust_posture["producer_trust_ready"]:
        claim_state = (
            "producer_trust_unconfigured"
            if trust_registry.get("status") == "UNCONFIGURED"
            else "producer_trust_incomplete"
        )
        settlement_state = "awaiting_producer_trust"
        next_action = (
            "enroll ACTIVE Ed25519 producer public keys for the missing lanes "
            f"{','.join(trust_posture['missing_lanes'])} in the operator-owned "
            "trust registry; private keys remain producer-owned and enrollment "
            "grants no provider, dispatch, delivery, deployment, or execution authority"
        )
    elif claim_count == 0:
        claim_state = "unclaimed"
        settlement_state = "awaiting_current_evidence"
        next_action = (
            "await an independently signed producer claim or refreshed source "
            "evidence; do not interrupt the operator or infer pickup"
        )
    elif claim_count < expected_count:
        claim_state = "partially_claimed"
        settlement_state = "awaiting_current_evidence"
        next_action = (
            "await the remaining independently signed claims and refreshed "
            "source evidence; no provider operation is authorized here"
        )
    else:
        claim_state = "claimed"
        settlement_state = "awaiting_current_evidence"
        next_action = (
            "await refreshed producer receipts and let the next approved OODA "
            "cycle verify settlement; this claim grants no execution authority"
        )
    lifecycle = {
        "schema": SCHEMA,
        "status": "verified",
        "claim_state": claim_state,
        "settlement_state": settlement_state,
        "generated_at": observed_now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "blocking_reason": "",
        "next_action": next_action,
        "handoff_id": str(handoff.get("handoff_id") or ""),
        "request_id": str(handoff.get("request_id") or ""),
        "semantic_request_sha256": str(
            handoff.get("semantic_request_sha256") or ""
        ),
        "handoff_receipt_sha256": handoff_receipt_sha256,
        "handoff_verification_receipt_sha256": (
            handoff_verification_receipt_sha256
        ),
        "trust": {
            "status": str(trust_registry.get("status") or ""),
            "rotation_epoch": int(trust_registry.get("rotation_epoch") or 0),
            "trust_registry_sha256": str(
                trust_registry.get("trust_registry_sha256") or ""
            ),
            **trust_posture,
        },
        "claim_directory": {
            "path": str(Path(claim_dir).absolute()),
            "required": require_claim_dir,
            "present": directory_present,
            "current_claim_file_count": sum(
                work_item_id in candidate_paths for work_item_id in expected
            ),
            "non_current_claim_file_count": unmatched_count,
        },
        "producer_claim_recorded": bool(verified_claims),
        "claims": verified_claims,
        "progress": {
            "expected_claim_count": expected_count,
            "claim_count": claim_count,
            "unclaimed_count": expected_count - claim_count,
            "non_current_claim_file_count": unmatched_count,
            "current_evidence_verified": True,
            "handoff_binding_verified": True,
            "claim_signatures_verified": claim_count,
            "producer_trust_ready": trust_posture["producer_trust_ready"],
            "active_producer_count": trust_posture["active_producer_count"],
            "missing_trust_lane_count": len(trust_posture["missing_lanes"]),
        },
        **_safety_fields(),
    }
    lifecycle["producer_claim_recorded"] = bool(verified_claims)
    return _integrity_bound(lifecycle)


def verify_claim_lifecycle(
    lifecycle: Mapping[str, Any],
    *,
    handoff: Mapping[str, Any],
    handoff_receipt_sha256: str,
    handoff_verification_receipt_sha256: str,
    trust_registry: Mapping[str, Any],
    claim_dir: Path,
    require_claim_dir: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    generated_at = _fresh_timestamp(
        lifecycle.get("generated_at"),
        now=observed_now,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
    )
    expires_at = _timestamp(lifecycle.get("expires_at"))
    if generated_at is None or expires_at is None or expires_at < observed_now:
        return _blocked("source_refresh_claim_lifecycle_not_fresh", now=observed_now)
    if not _integrity_verified(lifecycle):
        return _blocked(
            "source_refresh_claim_lifecycle_integrity_invalid",
            now=observed_now,
        )
    lifecycle_claims = lifecycle.get("claims")
    if not isinstance(lifecycle_claims, list) or any(
        not isinstance(claim, Mapping)
        or _timestamp(claim.get("expires_at")) is None
        or _timestamp(claim.get("expires_at")) < observed_now
        for claim in lifecycle_claims
    ):
        return _blocked(
            "source_refresh_claim_lifecycle_claim_not_fresh",
            now=observed_now,
        )
    try:
        expected = build_claim_lifecycle(
            handoff,
            handoff_receipt_sha256=handoff_receipt_sha256,
            handoff_verification_receipt_sha256=(
                handoff_verification_receipt_sha256
            ),
            trust_registry=trust_registry,
            claim_dir=claim_dir,
            require_claim_dir=require_claim_dir,
            now=datetime.fromisoformat(generated_at),
        )
    except (OSError, TypeError, ValueError):
        return _blocked(
            "source_refresh_claim_current_evidence_not_admissible",
            now=observed_now,
        )
    if dict(lifecycle) != expected:
        return _blocked(
            "source_refresh_claim_lifecycle_binding_mismatch",
            now=observed_now,
        )
    verification = {
        key: value
        for key, value in lifecycle.items()
        if key not in {"schema", "integrity", "generated_at"}
    }
    verification.update(
        {
            "schema": VERIFY_SCHEMA,
            "status": "verified",
            "updated_at": observed_now.isoformat(),
            "lifecycle_generated_at": str(
                lifecycle.get("generated_at") or ""
            ),
            "progress": {
                **dict(lifecycle.get("progress") or {}),
                "lifecycle_integrity_verified": True,
                "handoff_binding_verified": True,
                "current_evidence_verified": True,
            },
        }
    )
    return _integrity_bound(verification)


def _persist_blocked_bundle(
    reason: str,
    *,
    receipt_path: Path,
    verification_path: Path,
    now: datetime,
) -> dict[str, Any]:
    lifecycle = _integrity_bound(
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
        atomic_write_bytes(receipt_path, _canonical(lifecycle), overwrite=True)
        persisted, lifecycle_raw, lifecycle_digest = _private_snapshot(
            receipt_path,
            field="source_refresh_blocked_claim_lifecycle",
        )
        if persisted != lifecycle or not _integrity_verified(persisted):
            raise ValueError("source_refresh_blocked_claim_lifecycle_not_persisted")
        verification["lifecycle_receipt_sha256"] = lifecycle_digest
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
                field="source_refresh_blocked_claim_verification",
            )
        )
        if persisted_verification != verification or not _integrity_verified(
            persisted_verification
        ):
            raise ValueError(
                "source_refresh_blocked_claim_verification_not_persisted"
            )
    except Exception:
        result = _blocked(reason, now=now)
        result["receipt_path"] = str(receipt_path)
        result["verification_path"] = str(verification_path)
        return result
    result = dict(verification)
    result.pop("integrity", None)
    result.update(
        {
            "receipt_path": str(receipt_path),
            "verification_path": str(verification_path),
            "lifecycle_receipt_sha256": lifecycle_digest,
            "lifecycle_receipt_bytes": len(lifecycle_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "lifecycle_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def materialize_claim_lifecycle_bundle(
    operator_projection: Mapping[str, Any],
    *,
    request_path: Path = source_refresh.DEFAULT_REQUEST_PATH,
    request_verification_path: Path = source_refresh.DEFAULT_VERIFICATION_PATH,
    handoff_path: Path = source_handoff.DEFAULT_HANDOFF_PATH,
    handoff_verification_path: Path = source_handoff.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = DEFAULT_TRUST_REGISTRY_PATH,
    claim_dir: Path = DEFAULT_CLAIM_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    require_claim_dir: bool = False,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    receipt_target = Path(receipt_path).absolute()
    verification_target = Path(verification_path).absolute()
    try:
        handoff, handoff_digest, handoff_verification_digest = (
            _current_handoff(
                operator_projection,
                request_path=Path(request_path).absolute(),
                request_verification_path=Path(
                    request_verification_path
                ).absolute(),
                handoff_path=Path(handoff_path).absolute(),
                handoff_verification_path=Path(
                    handoff_verification_path
                ).absolute(),
                now=observed_now,
                max_age_seconds=max_age_seconds,
            )
        )
        trust_registry = load_producer_trust_registry(trust_registry_path)
        lifecycle = build_claim_lifecycle(
            handoff,
            handoff_receipt_sha256=handoff_digest,
            handoff_verification_receipt_sha256=(
                handoff_verification_digest
            ),
            trust_registry=trust_registry,
            claim_dir=claim_dir,
            require_claim_dir=require_claim_dir,
            now=observed_now,
        )
        atomic_write_bytes(
            receipt_target,
            _canonical(lifecycle),
            overwrite=True,
        )
        persisted, lifecycle_raw, lifecycle_digest = _private_snapshot(
            receipt_target,
            field="source_refresh_claim_lifecycle",
        )
        if persisted != lifecycle:
            raise ValueError("source_refresh_claim_lifecycle_persistence_mismatch")
        verification = verify_claim_lifecycle(
            persisted,
            handoff=handoff,
            handoff_receipt_sha256=handoff_digest,
            handoff_verification_receipt_sha256=(
                handoff_verification_digest
            ),
            trust_registry=trust_registry,
            claim_dir=claim_dir,
            require_claim_dir=require_claim_dir,
            now=observed_now,
        )
        if verification.get("status") != "verified":
            raise ValueError("source_refresh_claim_lifecycle_verification_failed")
        verification["lifecycle_receipt_sha256"] = lifecycle_digest
        verification["progress"] = {
            **dict(verification.get("progress") or {}),
            "lifecycle_receipt_persisted": True,
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
                field="source_refresh_claim_lifecycle_verification",
            )
        )
        if persisted_verification != verification or not _integrity_verified(
            persisted_verification
        ):
            raise ValueError(
                "source_refresh_claim_verification_persistence_mismatch"
            )
    except Exception:
        return _persist_blocked_bundle(
            "source_refresh_claim_evidence_or_persistence_not_admissible",
            receipt_path=receipt_target,
            verification_path=verification_target,
            now=observed_now,
        )
    result = dict(verification)
    result.pop("integrity", None)
    result.update(
        {
            "receipt_path": str(receipt_target),
            "verification_path": str(verification_target),
            "lifecycle_receipt_sha256": lifecycle_digest,
            "lifecycle_receipt_bytes": len(lifecycle_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "lifecycle_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def inspect_claim_lifecycle_bundle(
    operator_projection: Mapping[str, Any],
    *,
    request_path: Path = source_refresh.DEFAULT_REQUEST_PATH,
    request_verification_path: Path = source_refresh.DEFAULT_VERIFICATION_PATH,
    handoff_path: Path = source_handoff.DEFAULT_HANDOFF_PATH,
    handoff_verification_path: Path = source_handoff.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = DEFAULT_TRUST_REGISTRY_PATH,
    claim_dir: Path = DEFAULT_CLAIM_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    require_claim_dir: bool = False,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Verify existing claim lifecycle receipts without replacing them."""

    observed_now = _now(now)
    try:
        handoff, handoff_digest, handoff_verification_digest = (
            _current_handoff(
                operator_projection,
                request_path=Path(request_path).absolute(),
                request_verification_path=Path(
                    request_verification_path
                ).absolute(),
                handoff_path=Path(handoff_path).absolute(),
                handoff_verification_path=Path(
                    handoff_verification_path
                ).absolute(),
                now=observed_now,
                max_age_seconds=max_age_seconds,
            )
        )
        trust_registry = load_producer_trust_registry(trust_registry_path)
        lifecycle, lifecycle_raw, lifecycle_digest = _private_snapshot(
            receipt_path,
            field="source_refresh_claim_lifecycle",
        )
        persisted_verification, verification_raw, verification_digest = (
            _private_snapshot(
                verification_path,
                field="source_refresh_claim_lifecycle_verification",
            )
        )
        current_verification = verify_claim_lifecycle(
            lifecycle,
            handoff=handoff,
            handoff_receipt_sha256=handoff_digest,
            handoff_verification_receipt_sha256=(
                handoff_verification_digest
            ),
            trust_registry=trust_registry,
            claim_dir=claim_dir,
            require_claim_dir=require_claim_dir,
            now=observed_now,
        )
        expected_progress = {
            **dict(current_verification.get("progress") or {}),
            "lifecycle_receipt_persisted": True,
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
                "lifecycle_receipt_sha256",
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
            and persisted_verification.get("lifecycle_receipt_sha256")
            == lifecycle_digest
            and persisted_without_variable == current_without_variable
            and dict(persisted_verification.get("progress") or {})
            == expected_progress
        ):
            raise ValueError("source_refresh_claim_lifecycle_bundle_not_current")
    except Exception:
        return _blocked(
            "source_refresh_claim_lifecycle_bundle_not_admissible",
            now=observed_now,
        )
    result = dict(persisted_verification)
    result.pop("integrity", None)
    result.update(
        {
            "receipt_path": str(Path(receipt_path).absolute()),
            "verification_path": str(Path(verification_path).absolute()),
            "lifecycle_receipt_sha256": lifecycle_digest,
            "lifecycle_receipt_bytes": len(lifecycle_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "lifecycle_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def materialize_current_claim_lifecycle_bundle(
    *,
    cycle_receipt_path: Path = operator_status.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = operator_status.DEFAULT_SIGNAL_DIR,
    request_path: Path = source_refresh.DEFAULT_REQUEST_PATH,
    request_verification_path: Path = source_refresh.DEFAULT_VERIFICATION_PATH,
    handoff_path: Path = source_handoff.DEFAULT_HANDOFF_PATH,
    handoff_verification_path: Path = source_handoff.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = DEFAULT_TRUST_REGISTRY_PATH,
    claim_dir: Path = DEFAULT_CLAIM_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    require_claim_dir: bool = False,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    projection = operator_status.load_operator_status(
        cycle_receipt_path=Path(cycle_receipt_path),
        signal_dir=Path(signal_dir),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    return materialize_claim_lifecycle_bundle(
        projection,
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=handoff_path,
        handoff_verification_path=handoff_verification_path,
        trust_registry_path=trust_registry_path,
        claim_dir=claim_dir,
        receipt_path=receipt_path,
        verification_path=verification_path,
        require_claim_dir=require_claim_dir,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def inspect_current_claim_lifecycle_bundle(
    *,
    cycle_receipt_path: Path = operator_status.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = operator_status.DEFAULT_SIGNAL_DIR,
    request_path: Path = source_refresh.DEFAULT_REQUEST_PATH,
    request_verification_path: Path = source_refresh.DEFAULT_VERIFICATION_PATH,
    handoff_path: Path = source_handoff.DEFAULT_HANDOFF_PATH,
    handoff_verification_path: Path = source_handoff.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = DEFAULT_TRUST_REGISTRY_PATH,
    claim_dir: Path = DEFAULT_CLAIM_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    require_claim_dir: bool = False,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    projection = operator_status.load_operator_status(
        cycle_receipt_path=Path(cycle_receipt_path),
        signal_dir=Path(signal_dir),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    return inspect_claim_lifecycle_bundle(
        projection,
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=handoff_path,
        handoff_verification_path=handoff_verification_path,
        trust_registry_path=trust_registry_path,
        claim_dir=claim_dir,
        receipt_path=receipt_path,
        verification_path=verification_path,
        require_claim_dir=require_claim_dir,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize or inspect signed producer claim lifecycle evidence "
            "without granting provider, delivery, deployment, or execution authority."
        )
    )
    parser.add_argument(
        "--cycle-receipt", type=Path, default=operator_status.DEFAULT_CYCLE_RECEIPT
    )
    parser.add_argument(
        "--signal-dir", type=Path, default=operator_status.DEFAULT_SIGNAL_DIR
    )
    parser.add_argument(
        "--request", type=Path, default=source_refresh.DEFAULT_REQUEST_PATH
    )
    parser.add_argument(
        "--request-verification",
        type=Path,
        default=source_refresh.DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--handoff", type=Path, default=source_handoff.DEFAULT_HANDOFF_PATH
    )
    parser.add_argument(
        "--handoff-verification",
        type=Path,
        default=source_handoff.DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--trust-registry", type=Path, default=DEFAULT_TRUST_REGISTRY_PATH
    )
    parser.add_argument("--claim-dir", type=Path, default=DEFAULT_CLAIM_DIR)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument(
        "--verification", type=Path, default=DEFAULT_VERIFICATION_PATH
    )
    parser.add_argument(
        "--max-age-seconds", type=float, default=DEFAULT_MAX_AGE_SECONDS
    )
    parser.add_argument(
        "--require-claim-dir",
        action="store_true",
        help="Fail closed when the externally owned claim directory is absent.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Verify existing lifecycle receipts without replacing them.",
    )
    args = parser.parse_args(argv)
    operation = (
        inspect_current_claim_lifecycle_bundle
        if args.inspect
        else materialize_current_claim_lifecycle_bundle
    )
    result = operation(
        cycle_receipt_path=args.cycle_receipt,
        signal_dir=args.signal_dir,
        request_path=args.request,
        request_verification_path=args.request_verification,
        handoff_path=args.handoff,
        handoff_verification_path=args.handoff_verification,
        trust_registry_path=args.trust_registry,
        claim_dir=args.claim_dir,
        receipt_path=args.receipt,
        verification_path=args.verification,
        require_claim_dir=args.require_claim_dir,
        max_age_seconds=args.max_age_seconds,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
