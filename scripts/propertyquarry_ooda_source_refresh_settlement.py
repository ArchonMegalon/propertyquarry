#!/usr/bin/env python3
"""Attribute source-refresh settlement to authenticated producer completions."""

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

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


COMPLETION_SCHEMA = "propertyquarry.ooda_source_refresh_producer_completion.v1"
SCHEMA = "propertyquarry.ooda_source_refresh_settlement.v1"
VERIFY_SCHEMA = "propertyquarry.ooda_source_refresh_settlement_verification.v1"
DEFAULT_COMPLETION_DIR = Path(
    "_completion/propertyquarry_ooda_producer_completions"
)
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-settlement.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-settlement-verification.json"
)
DEFAULT_MAX_AGE_SECONDS = source_claims.DEFAULT_MAX_AGE_SECONDS
MAX_COMPLETION_TTL_SECONDS = 3600
MIN_COMPLETION_TTL_SECONDS = 60
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_SIGNAL_BYTES = 16 * 1024 * 1024
MAX_COMPLETION_FILES = source_claims.MAX_CLAIM_FILES
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WORK_ITEM_ID = re.compile(r"pq-source-refresh-work-[0-9a-f]{24}\Z")
_COMPLETION_ID = re.compile(r"pqsrr_completion_[0-9a-f]{24}\Z")
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
    return source_claims._integrity_bound(payload)


def _integrity_verified(payload: Mapping[str, Any]) -> bool:
    return source_claims._integrity_verified(payload)


def _safety_fields() -> dict[str, bool]:
    return {
        "action_required": False,
        "interrupt_operator": False,
        "completion_confers_authority": False,
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
        "settlement_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair the prior claim, signed completion, or current source "
            "binding; do not infer producer completion or settlement"
        ),
        "producer_completion_recorded": False,
        "settlement_attributed": False,
        "settlements": [],
        "progress": {
            "current_evidence_verified": False,
            "prior_claim_binding_verified": False,
            "completion_signature_count": 0,
            "settled_count": 0,
            "expected_work_item_count": 0,
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
    return source_claims._decode_base64url(
        value,
        expected_bytes=expected_bytes,
        label=label,
    )


def _private_snapshot(
    path: Path,
    *,
    field: str,
) -> tuple[dict[str, Any], bytes, str]:
    return source_claims._private_snapshot(
        path,
        field=field,
        maximum_bytes=MAX_RECEIPT_BYTES,
    )


def _read_only_snapshot(
    path: Path,
    *,
    field: str,
    maximum_bytes: int = MAX_RECEIPT_BYTES,
) -> tuple[dict[str, Any], bytes, str]:
    return source_claims._read_only_snapshot(
        path,
        field=field,
        maximum_bytes=maximum_bytes,
    )


def _completion_directory(
    path: Path,
    *,
    require_present: bool,
) -> tuple[bool, dict[str, Path], list[dict[str, str]]]:
    target = Path(path).absolute()
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        if require_present:
            raise ValueError("source_refresh_completion_directory_unavailable")
        return False, {}, []
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError("source_refresh_completion_directory_not_admissible")
    paths: dict[str, Path] = {}
    snapshots: list[dict[str, str]] = []
    with os.scandir(target) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_COMPLETION_FILES:
                raise ValueError("source_refresh_completion_directory_too_large")
            if not entry.is_file(follow_symlinks=False) or entry.is_symlink():
                raise ValueError("source_refresh_completion_entry_not_admissible")
            if not entry.name.endswith(".json"):
                raise ValueError("source_refresh_completion_entry_not_admissible")
            work_item_id = entry.name[:-5]
            if not _WORK_ITEM_ID.fullmatch(work_item_id) or work_item_id in paths:
                raise ValueError("source_refresh_completion_entry_not_admissible")
            entry_path = Path(entry.path)
            _payload, _raw, digest = _read_only_snapshot(
                entry_path,
                field="source_refresh_producer_completion",
            )
            paths[work_item_id] = entry_path
            snapshots.append(
                {
                    "work_item_id": work_item_id,
                    "receipt_sha256": digest,
                }
            )
    return True, paths, sorted(snapshots, key=lambda row: row["work_item_id"])


def _current_source_snapshot(
    operator_projection: Mapping[str, Any],
    *,
    signal_dir: Path,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    approval = operator_projection.get("signal_approval")
    source_evidence = operator_projection.get("source_evidence")
    progress = operator_projection.get("progress")
    approved_binding = bool(
        isinstance(approval, Mapping)
        and approval.get("approved") is True
        and approval.get("reason") == "approved_projection_manifest_verified"
        and approval.get("policy") == approved.POLICY
    )
    revocation_binding = bool(
        isinstance(approval, Mapping)
        and approval.get("approved") is False
        and approval.get("reason") == "approved_projection_manifest_revoked"
        and approval.get("revocation_verified") is True
        and approval.get("policy") == approved.POLICY
    )
    if not (
        operator_projection.get("schema") == operator_status.SCHEMA
        and operator_projection.get("status")
        in {
            "ready",
            "waiting_for_evidence",
            "action_required",
            "pending_action",
        }
        and _fresh_timestamp(
            operator_projection.get("updated_at"),
            now=now,
            max_age_seconds=max_age_seconds,
        )
        is not None
        and _SHA256.fullmatch(
            str(operator_projection.get("source_cycle_receipt_sha256") or "")
        )
        and isinstance(progress, Mapping)
        and progress.get("current_snapshot_hashes_verified") is True
        and (approved_binding or revocation_binding)
        and isinstance(source_evidence, Mapping)
        and source_evidence.get("schema") == approved.SOURCE_EVIDENCE_SCHEMA
        and source_evidence.get("status")
        in {"verified_current", "waiting_for_fresh_sources"}
        and operator_projection.get("automatic_execution_allowed") is False
        and operator_projection.get("provider_quota_consumption_allowed") is False
        and operator_projection.get("protected_operation_executed") is False
    ):
        raise ValueError("source_refresh_settlement_projection_not_admissible")
    manifest_path = Path(signal_dir).absolute() / "manifest.json"
    manifest, _manifest_raw, manifest_digest = _read_only_snapshot(
        manifest_path,
        field="source_refresh_settlement_manifest",
        maximum_bytes=MAX_SIGNAL_BYTES,
    )
    manifest_signals = manifest.get("signals")
    if approved_binding:
        manifest_approval = manifest.get("approval")
        manifest_admissible = bool(
            manifest.get("schema") == approved.SCHEMA
            and manifest.get("status") == "approved_projection"
            and manifest.get("generated_at")
            == approval.get("manifest_generated_at")
            and isinstance(manifest_approval, Mapping)
            and manifest_approval.get("decision") == "approved_projection"
            and manifest_approval.get("authority")
            == "operator_owned_read_only_ingress"
            and manifest_approval.get("policy") == approved.POLICY
            and manifest_approval.get("notification_policy")
            == "action_required_only"
            and manifest_approval.get("automatic_execution_allowed") is False
            and manifest_approval.get("provider_quota_consumption_allowed")
            is False
            and manifest_approval.get("protected_operations")
            == approved.PROTECTED_OPERATIONS
            and manifest.get("publication")
            == {
                "commit_file": "manifest.json",
                "commit_order": "signals_then_manifest",
            }
            and isinstance(manifest_signals, Mapping)
            and set(manifest_signals) == set(approved.SIGNAL_FILENAMES)
        )
        manifest_state = "approved_projection"
    else:
        revocation = manifest.get("revocation")
        manifest_admissible = bool(
            manifest.get("schema") == approved.REVOCATION_SCHEMA
            and manifest.get("status") == "revoked"
            and manifest.get("reason") == "producer_source_unavailable"
            and manifest.get("generated_at")
            == approval.get("manifest_generated_at")
            and isinstance(revocation, Mapping)
            and revocation.get("decision") == "revoked"
            and revocation.get("policy") == approved.POLICY
        )
        manifest_state = "revoked"
    if not manifest_admissible:
        raise ValueError("source_refresh_settlement_manifest_not_admissible")
    artifacts: dict[str, dict[str, Any]] = {}
    for artifact, filename in (
        approved.SIGNAL_FILENAMES.items() if approved_binding else ()
    ):
        row = manifest_signals.get(artifact)
        if not isinstance(row, Mapping) or row.get("filename") != filename:
            raise ValueError("source_refresh_settlement_manifest_not_admissible")
        payload, raw, digest = _read_only_snapshot(
            Path(signal_dir).absolute() / filename,
            field=f"source_refresh_settlement_{artifact}",
            maximum_bytes=MAX_SIGNAL_BYTES,
        )
        if not (
            isinstance(payload, Mapping)
            and row.get("sha256") == digest
            and row.get("bytes") == len(raw)
        ):
            raise ValueError("source_refresh_settlement_signal_not_current")
        artifacts[artifact] = {
            "sha256": digest,
            "bytes": len(raw),
        }
    lanes: dict[str, dict[str, Any]] = {}
    for raw in list(source_evidence.get("lanes") or []):
        if not isinstance(raw, Mapping):
            raise ValueError("source_refresh_settlement_lane_not_admissible")
        lane = str(raw.get("lane") or "")
        if (
            lane not in source_refresh._LANE_ARTIFACTS
            or lane in lanes
            or raw.get("status") not in {"current", "stale", "unavailable"}
            or raw.get("source_artifacts")
            != source_refresh._LANE_ARTIFACTS[lane]
        ):
            raise ValueError("source_refresh_settlement_lane_not_admissible")
        lanes[lane] = dict(raw)
    if set(lanes) != set(source_refresh._LANE_ARTIFACTS):
        raise ValueError("source_refresh_settlement_lane_set_not_admissible")
    return {
        "source_cycle_receipt_sha256": str(
            operator_projection.get("source_cycle_receipt_sha256") or ""
        ),
        "source_status_updated_at": str(
            operator_projection.get("updated_at") or ""
        ),
        "source_manifest_generated_at": str(
            approval.get("manifest_generated_at") or ""
        ),
        "source_manifest_sha256": manifest_digest,
        "source_manifest_state": manifest_state,
        "lanes": lanes,
        "artifacts": artifacts,
    }


def _load_prior_chain(
    *,
    handoff_path: Path,
    handoff_verification_path: Path,
    claim_lifecycle_path: Path,
    claim_verification_path: Path,
    claim_dir: Path,
    trust_registry: Mapping[str, Any],
    require_claim_dir: bool,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any] | None:
    paths = (
        Path(handoff_path).absolute(),
        Path(handoff_verification_path).absolute(),
        Path(claim_lifecycle_path).absolute(),
        Path(claim_verification_path).absolute(),
    )
    present = [path.exists() for path in paths]
    if not any(present):
        directory_present, claim_paths = source_claims._claim_directory(
            claim_dir,
            require_present=require_claim_dir,
        )
        if claim_paths:
            raise ValueError("source_refresh_prior_chain_missing_for_claims")
        return {
            "present": False,
            "claim_directory_present": directory_present,
            "handoff": {},
            "work_items": {},
            "claims": {},
        }
    if not all(present):
        raise ValueError("source_refresh_prior_chain_partial")
    handoff, _handoff_raw, handoff_digest = _private_snapshot(
        paths[0],
        field="source_refresh_settlement_prior_handoff",
    )
    handoff_verification, _handoff_v_raw, handoff_v_digest = _private_snapshot(
        paths[1],
        field="source_refresh_settlement_prior_handoff_verification",
    )
    lifecycle, _lifecycle_raw, lifecycle_digest = _private_snapshot(
        paths[2],
        field="source_refresh_settlement_prior_claim_lifecycle",
    )
    lifecycle_verification, _lifecycle_v_raw, lifecycle_v_digest = (
        _private_snapshot(
            paths[3],
            field="source_refresh_settlement_prior_claim_verification",
        )
    )
    handoff_generated_at = _timestamp(handoff.get("generated_at"))
    lifecycle_generated_at = _timestamp(lifecycle.get("generated_at"))
    handoff_generated = _fresh_timestamp(
        handoff.get("generated_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    lifecycle_generated = _fresh_timestamp(
        lifecycle.get("generated_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    if not (
        handoff.get("schema") == source_handoff.SCHEMA
        and handoff.get("handoff_state")
        in {"producer_pickup_available", "current_sources_verified"}
        and handoff_generated_at is not None
        and lifecycle_generated_at is not None
        and _integrity_verified(handoff)
        and handoff_verification.get("schema") == source_handoff.VERIFY_SCHEMA
        and handoff_verification.get("status") == "verified"
        and _integrity_verified(handoff_verification)
        and handoff_verification.get("handoff_receipt_sha256")
        == handoff_digest
        and lifecycle.get("schema") == source_claims.SCHEMA
        and lifecycle.get("status") == "verified"
        and _integrity_verified(lifecycle)
        and lifecycle.get("handoff_receipt_sha256") == handoff_digest
        and lifecycle.get("handoff_verification_receipt_sha256")
        == handoff_v_digest
        and lifecycle_verification.get("schema") == source_claims.VERIFY_SCHEMA
        and lifecycle_verification.get("status") == "verified"
        and _integrity_verified(lifecycle_verification)
        and lifecycle_verification.get("lifecycle_receipt_sha256")
        == lifecycle_digest
        and dict(lifecycle_verification.get("progress") or {}).get(
            "lifecycle_integrity_verified"
        )
        is True
        and dict(lifecycle_verification.get("progress") or {}).get(
            "handoff_binding_verified"
        )
        is True
        and dict(lifecycle.get("trust") or {}).get("trust_registry_sha256")
        == trust_registry.get("trust_registry_sha256")
    ):
        raise ValueError("source_refresh_prior_chain_not_admissible")
    work_items: dict[str, dict[str, Any]] = {}
    for raw in list(handoff.get("work_items") or []):
        if not isinstance(raw, Mapping):
            raise ValueError("source_refresh_prior_work_item_not_admissible")
        work_item_id = str(raw.get("work_item_id") or "")
        lane = str(raw.get("lane") or "")
        if not (
            _WORK_ITEM_ID.fullmatch(work_item_id)
            and work_item_id not in work_items
            and lane in source_refresh._LANE_ARTIFACTS
            and raw.get("required_artifacts")
            == source_refresh._LANE_ARTIFACTS[lane]
        ):
            raise ValueError("source_refresh_prior_work_item_not_admissible")
        work_items[work_item_id] = dict(raw)
    directory_present, claim_paths = source_claims._claim_directory(
        claim_dir,
        require_present=require_claim_dir,
    )
    recorded_claims = lifecycle.get("claims")
    if not isinstance(recorded_claims, list):
        raise ValueError("source_refresh_prior_claims_not_admissible")
    recorded_verified_claims: dict[str, dict[str, Any]] = {}
    for raw in recorded_claims:
        if not isinstance(raw, Mapping):
            raise ValueError("source_refresh_prior_claim_not_admissible")
        work_item_id = str(raw.get("work_item_id") or "")
        work_item = work_items.get(work_item_id)
        claim_path = claim_paths.get(work_item_id)
        if (
            work_item is None
            or claim_path is None
            or work_item_id in recorded_verified_claims
        ):
            raise ValueError("source_refresh_prior_claim_not_admissible")
        claim, _claim_raw, claim_digest = _read_only_snapshot(
            claim_path,
            field="source_refresh_settlement_prior_claim",
        )
        verified = source_claims.verify_producer_claim(
            claim,
            work_item=work_item,
            handoff=handoff,
            handoff_receipt_sha256=handoff_digest,
            trust_registry=trust_registry,
            claim_receipt_sha256=claim_digest,
            now=lifecycle_generated_at,
        )
        expected_record = dict(verified)
        expected_record["claim_path"] = str(Path(claim_path).absolute())
        if dict(raw) != expected_record:
            raise ValueError("source_refresh_prior_claim_record_mismatch")
        recorded_verified_claims[work_item_id] = {
            "record": dict(raw),
            "receipt": claim,
        }
    if len(recorded_verified_claims) != int(
        dict(lifecycle.get("progress") or {}).get("claim_count") or 0
    ):
        raise ValueError("source_refresh_prior_claim_count_mismatch")

    # A producer may claim work after the previous scheduler tick persisted
    # its lifecycle. Rebuild the claim view against the exact prior handoff so
    # a valid late claim is authenticated rather than silently superseded.
    if handoff_generated is None or lifecycle_generated is None:
        if claim_paths:
            raise ValueError("source_refresh_stale_prior_chain_cannot_accept_claims")
        refreshed_records: list[dict[str, Any]] = []
    else:
        refreshed_lifecycle = source_claims.build_claim_lifecycle(
            handoff,
            handoff_receipt_sha256=handoff_digest,
            handoff_verification_receipt_sha256=handoff_v_digest,
            trust_registry=trust_registry,
            claim_dir=claim_dir,
            require_claim_dir=require_claim_dir,
            now=now,
        )
        refreshed_records = refreshed_lifecycle.get("claims")
        if not (
            refreshed_lifecycle.get("status") == "verified"
            and isinstance(refreshed_records, list)
        ):
            raise ValueError("source_refresh_current_prior_claims_not_admissible")
    refreshed_by_id = {
        str(raw.get("work_item_id") or ""): dict(raw)
        for raw in refreshed_records
        if isinstance(raw, Mapping)
    }
    if len(refreshed_by_id) != len(refreshed_records) or any(
        refreshed_by_id.get(work_item_id) != evidence["record"]
        for work_item_id, evidence in recorded_verified_claims.items()
    ):
        raise ValueError("source_refresh_prior_claim_disappeared_or_changed")
    verified_claims: dict[str, dict[str, Any]] = {}
    for work_item_id, record in refreshed_by_id.items():
        claim_path = claim_paths.get(work_item_id)
        if claim_path is None:
            raise ValueError("source_refresh_current_prior_claim_missing")
        claim, _claim_raw, claim_digest = _read_only_snapshot(
            claim_path,
            field="source_refresh_settlement_current_prior_claim",
        )
        if claim_digest != record.get("claim_receipt_sha256"):
            raise ValueError("source_refresh_current_prior_claim_changed")
        verified_claims[work_item_id] = {
            "record": record,
            "receipt": claim,
        }
    return {
        "present": True,
        "claim_directory_present": directory_present,
        "handoff": handoff,
        "handoff_receipt_sha256": handoff_digest,
        "handoff_verification_receipt_sha256": handoff_v_digest,
        "claim_lifecycle_receipt_sha256": lifecycle_digest,
        "claim_verification_receipt_sha256": lifecycle_v_digest,
        "work_items": work_items,
        "claims": verified_claims,
    }


def verify_producer_completion(
    completion: Mapping[str, Any],
    *,
    work_item: Mapping[str, Any],
    handoff: Mapping[str, Any],
    handoff_receipt_sha256: str,
    claim_record: Mapping[str, Any],
    claim_receipt: Mapping[str, Any],
    trust_registry: Mapping[str, Any],
    completion_receipt_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    _require_exact_keys(
        completion,
        {
            "schema",
            "issuer",
            "key_id",
            "completion_id",
            "claim_id",
            "claim_receipt_sha256",
            "handoff_id",
            "handoff_receipt_sha256",
            "request_id",
            "semantic_request_sha256",
            "work_item_id",
            "lane",
            "completed_operation",
            "artifact_receipts",
            "completed_at",
            "issued_at",
            "expires_at",
            "nonce",
            "completion",
            "signature",
        },
        label="source_refresh_producer_completion",
    )
    scope = completion.get("completion")
    if not isinstance(scope, Mapping):
        raise ValueError("source_refresh_producer_completion_scope_not_admissible")
    _require_exact_keys(
        scope,
        {
            "state",
            "consumer_reverification_required",
            "completion_confers_authority",
            "provider_operation_authorized",
            "delivery_authorized",
        },
        label="source_refresh_producer_completion_scope",
    )
    producer_id = str(completion.get("issuer") or "")
    key_id = str(completion.get("key_id") or "")
    completion_id = str(completion.get("completion_id") or "")
    nonce = str(completion.get("nonce") or "")
    lane = str(work_item.get("lane") or "")
    completed_at = _timestamp(completion.get("completed_at"))
    issued_at = _timestamp(completion.get("issued_at"))
    expires_at = _timestamp(completion.get("expires_at"))
    claim_issued_at = _timestamp(claim_receipt.get("issued_at"))
    claim_expires_at = _timestamp(claim_receipt.get("expires_at"))
    artifact_receipts = completion.get("artifact_receipts")
    if not (
        completed_at is not None
        and issued_at is not None
        and expires_at is not None
        and claim_issued_at is not None
        and claim_expires_at is not None
        and claim_issued_at <= completed_at <= claim_expires_at
        and completed_at <= issued_at <= completed_at + timedelta(minutes=5)
        and MIN_COMPLETION_TTL_SECONDS
        <= (expires_at - issued_at).total_seconds()
        <= MAX_COMPLETION_TTL_SECONDS
        and -30.0
        <= (observed_now - issued_at).total_seconds()
        <= MAX_COMPLETION_TTL_SECONDS
        and observed_now <= expires_at
        and completion.get("schema") == COMPLETION_SCHEMA
        and _COMPLETION_ID.fullmatch(completion_id)
        and _NONCE.fullmatch(nonce)
        and _SHA256.fullmatch(str(completion_receipt_sha256 or ""))
        and producer_id == claim_record.get("producer_id")
        and key_id == claim_record.get("key_id")
        and completion.get("claim_id") == claim_record.get("claim_id")
        and completion.get("claim_receipt_sha256")
        == claim_record.get("claim_receipt_sha256")
        and completion.get("handoff_id") == handoff.get("handoff_id")
        and completion.get("handoff_receipt_sha256")
        == handoff_receipt_sha256
        and completion.get("request_id") == handoff.get("request_id")
        and completion.get("semantic_request_sha256")
        == handoff.get("semantic_request_sha256")
        and completion.get("work_item_id") == work_item.get("work_item_id")
        and completion.get("lane") == lane
        and completion.get("completed_operation") == "refresh_receipts"
        and isinstance(artifact_receipts, list)
        and scope.get("state") == "completed"
        and scope.get("consumer_reverification_required") is True
        and scope.get("completion_confers_authority") is False
        and scope.get("provider_operation_authorized") is False
        and scope.get("delivery_authorized") is False
    ):
        raise ValueError("source_refresh_producer_completion_binding_invalid")
    normalized_artifacts: list[dict[str, str]] = []
    expected_artifacts = list(work_item.get("required_artifacts") or [])
    for index, raw in enumerate(artifact_receipts):
        if not isinstance(raw, Mapping):
            raise ValueError("source_refresh_producer_completion_artifact_invalid")
        _require_exact_keys(
            raw,
            {"artifact", "approved_signal_sha256"},
            label="source_refresh_producer_completion_artifact",
        )
        artifact = str(raw.get("artifact") or "")
        digest = str(raw.get("approved_signal_sha256") or "")
        if not (
            index < len(expected_artifacts)
            and artifact == expected_artifacts[index]
            and _SHA256.fullmatch(digest)
        ):
            raise ValueError("source_refresh_producer_completion_artifact_invalid")
        normalized_artifacts.append(
            {"artifact": artifact, "approved_signal_sha256": digest}
        )
    if len(normalized_artifacts) != len(expected_artifacts):
        raise ValueError("source_refresh_producer_completion_artifact_invalid")
    trusted = source_claims._trusted_producer(
        trust_registry,
        producer_id=producer_id,
        key_id=key_id,
        lane=lane,
    )
    signature = _decode_base64url(
        completion.get("signature"),
        expected_bytes=64,
        label="source_refresh_producer_completion_signature",
    )
    signed_payload = {
        key: value for key, value in completion.items() if key != "signature"
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
        raise ValueError(
            "source_refresh_producer_completion_signature_invalid"
        ) from exc
    return {
        "status": "verified",
        "completion_id": completion_id,
        "claim_id": str(claim_record.get("claim_id") or ""),
        "producer_id": producer_id,
        "key_id": key_id,
        "lane": lane,
        "work_item_id": str(work_item.get("work_item_id") or ""),
        "completed_at": completed_at.isoformat(),
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "artifact_receipts": normalized_artifacts,
        "claim_receipt_sha256": str(
            claim_record.get("claim_receipt_sha256") or ""
        ),
        "completion_receipt_sha256": completion_receipt_sha256,
        "signed_payload_sha256": _sha256(_canonical(signed_payload)),
        "nonce_sha256": _sha256(nonce.encode("utf-8")),
        "trust_registry_sha256": str(
            trust_registry.get("trust_registry_sha256") or ""
        ),
        "consumer_reverification_required": True,
        **_safety_fields(),
    }


def build_settlement(
    operator_projection: Mapping[str, Any],
    *,
    signal_dir: Path,
    handoff_path: Path,
    handoff_verification_path: Path,
    claim_lifecycle_path: Path,
    claim_verification_path: Path,
    trust_registry: Mapping[str, Any],
    claim_dir: Path,
    completion_dir: Path,
    require_claim_dir: bool,
    require_completion_dir: bool,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Bind prior signed work to the freshly verified consumer evidence."""

    observed_now = _now(now)
    current = _current_source_snapshot(
        operator_projection,
        signal_dir=signal_dir,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    completion_present, completion_paths, completion_snapshot = (
        _completion_directory(
            completion_dir,
            require_present=require_completion_dir,
        )
    )
    prior = _load_prior_chain(
        handoff_path=handoff_path,
        handoff_verification_path=handoff_verification_path,
        claim_lifecycle_path=claim_lifecycle_path,
        claim_verification_path=claim_verification_path,
        claim_dir=claim_dir,
        trust_registry=trust_registry,
        require_claim_dir=require_claim_dir,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    work_items = dict(prior.get("work_items") or {})
    claims = dict(prior.get("claims") or {})
    trust_posture = source_claims.producer_trust_posture(
        trust_registry,
        required_lanes=[
            str(work_item.get("lane") or "")
            for work_item in work_items.values()
        ],
    )
    expected_ids = set(work_items)
    completion_ids: set[str] = set()
    completion_nonces: set[str] = set()
    settlements: list[dict[str, Any]] = []
    counts = {
        "settled_attributed": 0,
        "completion_verified_awaiting_current_evidence": 0,
        "claimed_awaiting_completion": 0,
        "current_evidence_verified_unattributed": 0,
        "unclaimed": 0,
    }
    for work_item_id in sorted(work_items):
        work_item = work_items[work_item_id]
        lane = str(work_item.get("lane") or "")
        lane_evidence = dict(current["lanes"].get(lane) or {})
        lane_current = lane_evidence.get("status") == "current"
        claim = claims.get(work_item_id)
        completion_path = completion_paths.get(work_item_id)
        if completion_path is not None and claim is None:
            raise ValueError("source_refresh_completion_without_verified_claim")

        completion_record: dict[str, Any] | None = None
        if completion_path is not None:
            completion, _raw, completion_digest = _read_only_snapshot(
                completion_path,
                field="source_refresh_producer_completion",
            )
            completion_record = verify_producer_completion(
                completion,
                work_item=work_item,
                handoff=prior["handoff"],
                handoff_receipt_sha256=str(
                    prior.get("handoff_receipt_sha256") or ""
                ),
                claim_record=claim["record"],
                claim_receipt=claim["receipt"],
                trust_registry=trust_registry,
                completion_receipt_sha256=completion_digest,
                now=observed_now,
            )
            completion_id = str(completion_record.get("completion_id") or "")
            nonce_sha256 = str(completion_record.get("nonce_sha256") or "")
            if completion_id in completion_ids or nonce_sha256 in completion_nonces:
                raise ValueError("source_refresh_producer_completion_replayed")
            completion_ids.add(completion_id)
            completion_nonces.add(nonce_sha256)

        current_artifacts: list[dict[str, str]] = []
        if lane_current:
            for artifact in list(work_item.get("required_artifacts") or []):
                current_artifacts.append(
                    {
                        "artifact": artifact,
                        "approved_signal_sha256": str(
                            current["artifacts"][artifact]["sha256"]
                        ),
                    }
                )
        if completion_record is not None and lane_current:
            if completion_record["artifact_receipts"] != current_artifacts:
                raise ValueError(
                    "source_refresh_producer_completion_artifact_binding_mismatch"
                )
            settlement_state = "settled_attributed"
        elif completion_record is not None:
            settlement_state = "completion_verified_awaiting_current_evidence"
        elif lane_current:
            settlement_state = "current_evidence_verified_unattributed"
        elif claim is not None:
            settlement_state = "claimed_awaiting_completion"
        else:
            settlement_state = "unclaimed"
        counts[settlement_state] += 1

        row = {
            "work_item_id": work_item_id,
            "lane": lane,
            "operation": str(work_item.get("operation") or ""),
            "required_artifacts": list(
                work_item.get("required_artifacts") or []
            ),
            "prior_handoff_id": str(
                prior.get("handoff", {}).get("handoff_id") or ""
            ),
            "prior_handoff_receipt_sha256": str(
                prior.get("handoff_receipt_sha256") or ""
            ),
            "claim_id": str(
                dict(claim.get("record") or {}).get("claim_id")
                if claim
                else ""
            ),
            "claim_receipt_sha256": str(
                dict(claim.get("record") or {}).get("claim_receipt_sha256")
                if claim
                else ""
            ),
            "producer_id": str(
                dict(claim.get("record") or {}).get("producer_id")
                if claim
                else ""
            ),
            "key_id": str(
                dict(claim.get("record") or {}).get("key_id")
                if claim
                else ""
            ),
            "completion": completion_record,
            "current_source_status": str(lane_evidence.get("status") or ""),
            "current_source_generated_at": str(
                lane_evidence.get("source_generated_at") or ""
            ),
            "current_artifact_receipts": current_artifacts,
            "settlement_state": settlement_state,
            "settlement_attributed": settlement_state == "settled_attributed",
            **_safety_fields(),
        }
        settlements.append(row)

    expected_count = len(work_items)
    completion_count = len(completion_ids)
    settled_count = counts["settled_attributed"]
    non_current_completion_count = len(set(completion_paths) - expected_ids)
    if expected_count == 0:
        settlement_state = "no_prior_work"
        next_action = (
            "continue monitoring; no prior source-refresh work exists to settle"
        )
    elif not trust_posture["producer_trust_ready"]:
        settlement_state = (
            "producer_trust_unconfigured"
            if trust_registry.get("status") == "UNCONFIGURED"
            else "producer_trust_incomplete"
        )
        next_action = (
            "enroll ACTIVE Ed25519 producer public keys for the missing lanes "
            f"{','.join(trust_posture['missing_lanes'])} in the operator-owned "
            "trust registry; private keys remain producer-owned and enrollment "
            "grants no provider, dispatch, delivery, deployment, or execution authority"
        )
    elif settled_count == expected_count:
        settlement_state = "settled_attributed"
        next_action = (
            "continue monitoring from the newly verified source baseline; "
            "no execution authority was granted"
        )
    elif counts["current_evidence_verified_unattributed"]:
        settlement_state = "current_evidence_verified_unattributed"
        next_action = (
            "retain the current source evidence but do not attribute it to a "
            "producer without a matching signed completion"
        )
    elif counts["completion_verified_awaiting_current_evidence"]:
        settlement_state = "completion_verified_awaiting_current_evidence"
        next_action = (
            "await consumer-verified current source evidence before attributing "
            "the signed producer completion"
        )
    elif counts["claimed_awaiting_completion"]:
        settlement_state = "claimed_awaiting_completion"
        next_action = (
            "await a matching signed producer completion and independently "
            "verified current source evidence"
        )
    else:
        settlement_state = "unclaimed"
        next_action = (
            "await an authenticated producer claim and completion; no provider "
            "operation is authorized here"
        )

    expires_at = observed_now + timedelta(seconds=float(max_age_seconds))
    for row in settlements:
        completion = row.get("completion")
        if isinstance(completion, Mapping):
            completion_expires_at = _timestamp(completion.get("expires_at"))
            if completion_expires_at is None:
                raise ValueError("source_refresh_producer_completion_expiry_invalid")
            expires_at = min(expires_at, completion_expires_at)
    settlement = {
        "schema": SCHEMA,
        "status": "verified",
        "settlement_state": settlement_state,
        "generated_at": observed_now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "blocking_reason": "",
        "next_action": next_action,
        "source_cycle_receipt_sha256": current[
            "source_cycle_receipt_sha256"
        ],
        "source_status_updated_at": current["source_status_updated_at"],
        "source_manifest_generated_at": current[
            "source_manifest_generated_at"
        ],
        "source_manifest_sha256": current["source_manifest_sha256"],
        "source_manifest_state": current["source_manifest_state"],
        "prior_chain": {
            "present": bool(prior.get("present")),
            "handoff_id": str(
                prior.get("handoff", {}).get("handoff_id") or ""
            ),
            "handoff_receipt_sha256": str(
                prior.get("handoff_receipt_sha256") or ""
            ),
            "handoff_verification_receipt_sha256": str(
                prior.get("handoff_verification_receipt_sha256") or ""
            ),
            "claim_lifecycle_receipt_sha256": str(
                prior.get("claim_lifecycle_receipt_sha256") or ""
            ),
            "claim_verification_receipt_sha256": str(
                prior.get("claim_verification_receipt_sha256") or ""
            ),
        },
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
            "present": bool(prior.get("claim_directory_present")),
        },
        "completion_directory": {
            "path": str(Path(completion_dir).absolute()),
            "required": require_completion_dir,
            "present": completion_present,
            "entries": completion_snapshot,
            "current_completion_file_count": sum(
                work_item_id in completion_paths for work_item_id in expected_ids
            ),
            "non_current_completion_file_count": non_current_completion_count,
        },
        "producer_completion_recorded": completion_count > 0,
        "settlement_attributed": expected_count > 0
        and settled_count == expected_count,
        "settlements": settlements,
        "progress": {
            "current_evidence_verified": True,
            "prior_claim_binding_verified": bool(prior.get("present")),
            "completion_signature_count": completion_count,
            "settled_count": settled_count,
            "expected_work_item_count": expected_count,
            "claimed_awaiting_completion_count": counts[
                "claimed_awaiting_completion"
            ],
            "completion_awaiting_current_evidence_count": counts[
                "completion_verified_awaiting_current_evidence"
            ],
            "current_evidence_unattributed_count": counts[
                "current_evidence_verified_unattributed"
            ],
            "unclaimed_count": counts["unclaimed"],
            "non_current_completion_file_count": non_current_completion_count,
            "producer_trust_ready": trust_posture["producer_trust_ready"],
            "active_producer_count": trust_posture["active_producer_count"],
            "missing_trust_lane_count": len(trust_posture["missing_lanes"]),
        },
        **_safety_fields(),
    }
    return _integrity_bound(settlement)


def verify_settlement_current(
    settlement: Mapping[str, Any],
    operator_projection: Mapping[str, Any],
    *,
    signal_dir: Path,
    trust_registry: Mapping[str, Any],
    completion_dir: Path,
    require_completion_dir: bool,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Verify a private settlement against current consumer-owned evidence."""

    observed_now = _now(now)
    generated_at = _fresh_timestamp(
        settlement.get("generated_at"),
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    expires_at = _timestamp(settlement.get("expires_at"))
    if (
        generated_at is None
        or expires_at is None
        or expires_at < observed_now
        or not _integrity_verified(settlement)
    ):
        return _blocked(
            "source_refresh_settlement_not_fresh_or_integrity_invalid",
            now=observed_now,
        )
    try:
        current = _current_source_snapshot(
            operator_projection,
            signal_dir=signal_dir,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        completion_present, _completion_paths, completion_snapshot = (
            _completion_directory(
                completion_dir,
                require_present=require_completion_dir,
            )
        )
        trust = settlement.get("trust")
        directory = settlement.get("completion_directory")
        prior_chain = settlement.get("prior_chain")
        rows = settlement.get("settlements")
        progress = settlement.get("progress")
        if not (
            settlement.get("schema") == SCHEMA
            and settlement.get("status") == "verified"
            and settlement.get("settlement_state")
            in {
                "no_prior_work",
                "settled_attributed",
                "completion_verified_awaiting_current_evidence",
                "claimed_awaiting_completion",
                "current_evidence_verified_unattributed",
                "unclaimed",
                "producer_trust_unconfigured",
                "producer_trust_incomplete",
            }
            and settlement.get("source_cycle_receipt_sha256")
            == current["source_cycle_receipt_sha256"]
            and settlement.get("source_status_updated_at")
            == current["source_status_updated_at"]
            and settlement.get("source_manifest_generated_at")
            == current["source_manifest_generated_at"]
            and settlement.get("source_manifest_sha256")
            == current["source_manifest_sha256"]
            and settlement.get("source_manifest_state")
            == current["source_manifest_state"]
            and isinstance(trust, Mapping)
            and isinstance(directory, Mapping)
            and directory.get("path") == str(Path(completion_dir).absolute())
            and directory.get("required") is require_completion_dir
            and directory.get("present") is completion_present
            and directory.get("entries") == completion_snapshot
            and isinstance(prior_chain, Mapping)
            and isinstance(rows, list)
            and isinstance(progress, Mapping)
            and progress.get("current_evidence_verified") is True
            and settlement.get("completion_confers_authority") is False
            and settlement.get("claim_confers_authority") is False
            and settlement.get("producer_dispatch_authorized") is False
            and settlement.get("producer_refresh_authorized") is False
            and settlement.get("automatic_source_refresh_allowed") is False
            and settlement.get("automatic_execution_allowed") is False
            and settlement.get("execution_authorized") is False
            and settlement.get("provider_quota_consumption_allowed") is False
            and settlement.get("protected_operation_executed") is False
            and settlement.get("delivery_authorized") is False
            and settlement.get("delivery_attempted") is False
            and settlement.get("sent") is False
        ):
            raise ValueError("source_refresh_settlement_binding_invalid")

        expected_trust_posture = source_claims.producer_trust_posture(
            trust_registry,
            required_lanes=[
                str(row.get("lane") or "")
                for row in rows
                if isinstance(row, Mapping)
            ],
        )
        if dict(trust) != {
            "status": str(trust_registry.get("status") or ""),
            "rotation_epoch": int(trust_registry.get("rotation_epoch") or 0),
            "trust_registry_sha256": str(
                trust_registry.get("trust_registry_sha256") or ""
            ),
            **expected_trust_posture,
        }:
            raise ValueError("source_refresh_settlement_trust_binding_invalid")

        work_item_ids: set[str] = set()
        completion_ids: set[str] = set()
        nonce_hashes: set[str] = set()
        derived_counts = {
            "settled_attributed": 0,
            "completion_verified_awaiting_current_evidence": 0,
            "claimed_awaiting_completion": 0,
            "current_evidence_verified_unattributed": 0,
            "unclaimed": 0,
        }
        completion_count = 0
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("source_refresh_settlement_row_invalid")
            work_item_id = str(row.get("work_item_id") or "")
            lane = str(row.get("lane") or "")
            state = str(row.get("settlement_state") or "")
            required_artifacts = row.get("required_artifacts")
            current_artifacts = row.get("current_artifact_receipts")
            lane_evidence = current["lanes"].get(lane)
            if not (
                _WORK_ITEM_ID.fullmatch(work_item_id)
                and work_item_id not in work_item_ids
                and lane in source_refresh._LANE_ARTIFACTS
                and required_artifacts == source_refresh._LANE_ARTIFACTS[lane]
                and state in derived_counts
                and isinstance(lane_evidence, Mapping)
                and row.get("current_source_status")
                == lane_evidence.get("status")
                and row.get("current_source_generated_at")
                == str(lane_evidence.get("source_generated_at") or "")
                and row.get("settlement_attributed")
                is (state == "settled_attributed")
                and row.get("completion_confers_authority") is False
                and row.get("producer_dispatch_authorized") is False
                and row.get("producer_refresh_authorized") is False
                and row.get("automatic_execution_allowed") is False
                and row.get("provider_quota_consumption_allowed") is False
                and row.get("protected_operation_executed") is False
                and row.get("delivery_authorized") is False
                and row.get("sent") is False
            ):
                raise ValueError("source_refresh_settlement_row_invalid")
            work_item_ids.add(work_item_id)
            derived_counts[state] += 1
            expected_current_artifacts = []
            if lane_evidence.get("status") == "current":
                expected_current_artifacts = [
                    {
                        "artifact": artifact,
                        "approved_signal_sha256": current["artifacts"][artifact][
                            "sha256"
                        ],
                    }
                    for artifact in required_artifacts
                ]
            if current_artifacts != expected_current_artifacts:
                raise ValueError("source_refresh_settlement_artifact_invalid")
            completion = row.get("completion")
            if completion is not None:
                if not isinstance(completion, Mapping):
                    raise ValueError("source_refresh_settlement_completion_invalid")
                completion_id = str(completion.get("completion_id") or "")
                nonce_hash = str(completion.get("nonce_sha256") or "")
                if not (
                    completion.get("status") == "verified"
                    and _COMPLETION_ID.fullmatch(completion_id)
                    and _SHA256.fullmatch(nonce_hash)
                    and completion_id not in completion_ids
                    and nonce_hash not in nonce_hashes
                    and completion.get("work_item_id") == work_item_id
                    and completion.get("lane") == lane
                    and completion.get("producer_id") == row.get("producer_id")
                    and completion.get("key_id") == row.get("key_id")
                    and completion.get("claim_id") == row.get("claim_id")
                    and completion.get("claim_receipt_sha256")
                    == row.get("claim_receipt_sha256")
                    and completion.get("trust_registry_sha256")
                    == trust_registry.get("trust_registry_sha256")
                    and _timestamp(completion.get("completed_at")) is not None
                    and _timestamp(completion.get("issued_at")) is not None
                    and _timestamp(completion.get("expires_at")) is not None
                    and _timestamp(completion.get("expires_at")) >= observed_now
                    and any(
                        entry.get("work_item_id") == work_item_id
                        and entry.get("receipt_sha256")
                        == completion.get("completion_receipt_sha256")
                        for entry in completion_snapshot
                    )
                    and completion.get("completion_confers_authority") is False
                    and completion.get("producer_refresh_authorized") is False
                    and completion.get("automatic_execution_allowed") is False
                    and completion.get("provider_quota_consumption_allowed")
                    is False
                    and completion.get("protected_operation_executed") is False
                    and completion.get("delivery_authorized") is False
                    and completion.get("sent") is False
                ):
                    raise ValueError("source_refresh_settlement_completion_invalid")
                completion_ids.add(completion_id)
                nonce_hashes.add(nonce_hash)
                completion_count += 1
            if state == "settled_attributed" and not (
                completion is not None
                and lane_evidence.get("status") == "current"
                and completion.get("artifact_receipts")
                == expected_current_artifacts
            ):
                raise ValueError("source_refresh_settlement_attribution_invalid")
            if state == "completion_verified_awaiting_current_evidence" and not (
                completion is not None and lane_evidence.get("status") != "current"
            ):
                raise ValueError("source_refresh_settlement_state_invalid")
            if state == "claimed_awaiting_completion" and not (
                completion is None
                and bool(row.get("claim_id"))
                and lane_evidence.get("status") != "current"
            ):
                raise ValueError("source_refresh_settlement_state_invalid")
            if state == "current_evidence_verified_unattributed" and not (
                completion is None and lane_evidence.get("status") == "current"
            ):
                raise ValueError("source_refresh_settlement_state_invalid")
            if state == "unclaimed" and not (
                completion is None
                and not row.get("claim_id")
                and lane_evidence.get("status") != "current"
            ):
                raise ValueError("source_refresh_settlement_state_invalid")

        expected_count = len(rows)
        settled_count = derived_counts["settled_attributed"]
        non_current_completion_count = len(completion_snapshot) - sum(
            entry.get("work_item_id") in work_item_ids
            for entry in completion_snapshot
        )
        expected_top_state = "unclaimed"
        if expected_count == 0:
            expected_top_state = "no_prior_work"
        elif not expected_trust_posture["producer_trust_ready"]:
            expected_top_state = (
                "producer_trust_unconfigured"
                if trust_registry.get("status") == "UNCONFIGURED"
                else "producer_trust_incomplete"
            )
        elif settled_count == expected_count:
            expected_top_state = "settled_attributed"
        elif derived_counts["current_evidence_verified_unattributed"]:
            expected_top_state = "current_evidence_verified_unattributed"
        elif derived_counts["completion_verified_awaiting_current_evidence"]:
            expected_top_state = "completion_verified_awaiting_current_evidence"
        elif derived_counts["claimed_awaiting_completion"]:
            expected_top_state = "claimed_awaiting_completion"
        if not (
            settlement.get("settlement_state") == expected_top_state
            and settlement.get("producer_completion_recorded")
            is (completion_count > 0)
            and settlement.get("settlement_attributed")
            is (expected_count > 0 and settled_count == expected_count)
            and progress.get("expected_work_item_count") == expected_count
            and progress.get("completion_signature_count") == completion_count
            and progress.get("settled_count") == settled_count
            and progress.get("claimed_awaiting_completion_count")
            == derived_counts["claimed_awaiting_completion"]
            and progress.get("completion_awaiting_current_evidence_count")
            == derived_counts["completion_verified_awaiting_current_evidence"]
            and progress.get("current_evidence_unattributed_count")
            == derived_counts["current_evidence_verified_unattributed"]
            and progress.get("unclaimed_count") == derived_counts["unclaimed"]
            and directory.get("current_completion_file_count")
            == len(completion_ids)
            and directory.get("non_current_completion_file_count")
            == non_current_completion_count
            and progress.get("non_current_completion_file_count")
            == non_current_completion_count
            and progress.get("producer_trust_ready")
            is expected_trust_posture["producer_trust_ready"]
            and progress.get("active_producer_count")
            == expected_trust_posture["active_producer_count"]
            and progress.get("missing_trust_lane_count")
            == len(expected_trust_posture["missing_lanes"])
        ):
            raise ValueError("source_refresh_settlement_progress_invalid")
    except (OSError, TypeError, ValueError):
        return _blocked(
            "source_refresh_settlement_current_evidence_not_admissible",
            now=observed_now,
        )

    verification = {
        key: value
        for key, value in settlement.items()
        if key not in {"schema", "integrity", "generated_at"}
    }
    verification.update(
        {
            "schema": VERIFY_SCHEMA,
            "status": "verified",
            "updated_at": observed_now.isoformat(),
            "settlement_generated_at": str(settlement.get("generated_at") or ""),
            "progress": {
                **dict(settlement.get("progress") or {}),
                "settlement_integrity_verified": True,
                "current_source_binding_verified": True,
                "completion_directory_binding_verified": True,
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
    settlement = _integrity_bound(
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
        atomic_write_bytes(receipt_path, _canonical(settlement), overwrite=True)
        persisted, settlement_raw, settlement_digest = _private_snapshot(
            receipt_path,
            field="source_refresh_blocked_settlement",
        )
        if persisted != settlement or not _integrity_verified(persisted):
            raise ValueError("source_refresh_blocked_settlement_not_persisted")
        verification["settlement_receipt_sha256"] = settlement_digest
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
                field="source_refresh_blocked_settlement_verification",
            )
        )
        if persisted_verification != verification or not _integrity_verified(
            persisted_verification
        ):
            raise ValueError(
                "source_refresh_blocked_settlement_verification_not_persisted"
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
            "settlement_receipt_sha256": settlement_digest,
            "settlement_receipt_bytes": len(settlement_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "settlement_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def materialize_settlement_bundle(
    operator_projection: Mapping[str, Any],
    *,
    signal_dir: Path = operator_status.DEFAULT_SIGNAL_DIR,
    handoff_path: Path = source_handoff.DEFAULT_HANDOFF_PATH,
    handoff_verification_path: Path = source_handoff.DEFAULT_VERIFICATION_PATH,
    claim_lifecycle_path: Path = source_claims.DEFAULT_RECEIPT_PATH,
    claim_verification_path: Path = source_claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = source_claims.DEFAULT_TRUST_REGISTRY_PATH,
    claim_dir: Path = source_claims.DEFAULT_CLAIM_DIR,
    completion_dir: Path = DEFAULT_COMPLETION_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    require_claim_dir: bool = False,
    require_completion_dir: bool = False,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    receipt_target = Path(receipt_path).absolute()
    verification_target = Path(verification_path).absolute()
    try:
        trust_registry = source_claims.load_producer_trust_registry(
            trust_registry_path
        )
        settlement = build_settlement(
            operator_projection,
            signal_dir=Path(signal_dir).absolute(),
            handoff_path=Path(handoff_path).absolute(),
            handoff_verification_path=Path(
                handoff_verification_path
            ).absolute(),
            claim_lifecycle_path=Path(claim_lifecycle_path).absolute(),
            claim_verification_path=Path(claim_verification_path).absolute(),
            trust_registry=trust_registry,
            claim_dir=Path(claim_dir).absolute(),
            completion_dir=Path(completion_dir).absolute(),
            require_claim_dir=require_claim_dir,
            require_completion_dir=require_completion_dir,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        atomic_write_bytes(
            receipt_target,
            _canonical(settlement),
            overwrite=True,
        )
        persisted, settlement_raw, settlement_digest = _private_snapshot(
            receipt_target,
            field="source_refresh_settlement",
        )
        if persisted != settlement:
            raise ValueError("source_refresh_settlement_persistence_mismatch")
        verification = verify_settlement_current(
            persisted,
            operator_projection,
            signal_dir=Path(signal_dir).absolute(),
            trust_registry=trust_registry,
            completion_dir=Path(completion_dir).absolute(),
            require_completion_dir=require_completion_dir,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        if verification.get("status") != "verified":
            raise ValueError("source_refresh_settlement_verification_failed")
        verification["settlement_receipt_sha256"] = settlement_digest
        verification["progress"] = {
            **dict(verification.get("progress") or {}),
            "settlement_receipt_persisted": True,
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
                field="source_refresh_settlement_verification",
            )
        )
        if persisted_verification != verification or not _integrity_verified(
            persisted_verification
        ):
            raise ValueError(
                "source_refresh_settlement_verification_persistence_mismatch"
            )
    except Exception:
        return _persist_blocked_bundle(
            "source_refresh_settlement_evidence_or_persistence_not_admissible",
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
            "settlement_receipt_sha256": settlement_digest,
            "settlement_receipt_bytes": len(settlement_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "settlement_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def inspect_settlement_bundle(
    operator_projection: Mapping[str, Any],
    *,
    signal_dir: Path = operator_status.DEFAULT_SIGNAL_DIR,
    trust_registry_path: Path = source_claims.DEFAULT_TRUST_REGISTRY_PATH,
    completion_dir: Path = DEFAULT_COMPLETION_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    require_completion_dir: bool = False,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Verify existing settlement receipts without replacing them."""

    observed_now = _now(now)
    try:
        trust_registry = source_claims.load_producer_trust_registry(
            trust_registry_path
        )
        settlement, settlement_raw, settlement_digest = _private_snapshot(
            receipt_path,
            field="source_refresh_settlement",
        )
        persisted_verification, verification_raw, verification_digest = (
            _private_snapshot(
                verification_path,
                field="source_refresh_settlement_verification",
            )
        )
        current_verification = verify_settlement_current(
            settlement,
            operator_projection,
            signal_dir=Path(signal_dir).absolute(),
            trust_registry=trust_registry,
            completion_dir=Path(completion_dir).absolute(),
            require_completion_dir=require_completion_dir,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        expected_progress = {
            **dict(current_verification.get("progress") or {}),
            "settlement_receipt_persisted": True,
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
                "settlement_receipt_sha256",
            }
        }
        verification_updated_at = _fresh_timestamp(
            persisted_verification.get("updated_at"),
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        if not (
            current_verification.get("status") == "verified"
            and persisted_verification.get("schema") == VERIFY_SCHEMA
            and persisted_verification.get("status") == "verified"
            and verification_updated_at is not None
            and _integrity_verified(persisted_verification)
            and persisted_verification.get("settlement_receipt_sha256")
            == settlement_digest
            and persisted_without_variable == current_without_variable
            and dict(persisted_verification.get("progress") or {})
            == expected_progress
        ):
            raise ValueError("source_refresh_settlement_bundle_not_current")
    except Exception:
        return _blocked(
            "source_refresh_settlement_bundle_not_admissible",
            now=observed_now,
        )
    result = dict(persisted_verification)
    result.pop("integrity", None)
    result.update(
        {
            "receipt_path": str(Path(receipt_path).absolute()),
            "verification_path": str(Path(verification_path).absolute()),
            "settlement_receipt_sha256": settlement_digest,
            "settlement_receipt_bytes": len(settlement_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "settlement_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def materialize_current_settlement_bundle(
    *,
    cycle_receipt_path: Path = operator_status.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = operator_status.DEFAULT_SIGNAL_DIR,
    handoff_path: Path = source_handoff.DEFAULT_HANDOFF_PATH,
    handoff_verification_path: Path = source_handoff.DEFAULT_VERIFICATION_PATH,
    claim_lifecycle_path: Path = source_claims.DEFAULT_RECEIPT_PATH,
    claim_verification_path: Path = source_claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = source_claims.DEFAULT_TRUST_REGISTRY_PATH,
    claim_dir: Path = source_claims.DEFAULT_CLAIM_DIR,
    completion_dir: Path = DEFAULT_COMPLETION_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    require_claim_dir: bool = False,
    require_completion_dir: bool = False,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    projection = operator_status.load_operator_status(
        cycle_receipt_path=Path(cycle_receipt_path),
        signal_dir=Path(signal_dir),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    return materialize_settlement_bundle(
        projection,
        signal_dir=signal_dir,
        handoff_path=handoff_path,
        handoff_verification_path=handoff_verification_path,
        claim_lifecycle_path=claim_lifecycle_path,
        claim_verification_path=claim_verification_path,
        trust_registry_path=trust_registry_path,
        claim_dir=claim_dir,
        completion_dir=completion_dir,
        receipt_path=receipt_path,
        verification_path=verification_path,
        require_claim_dir=require_claim_dir,
        require_completion_dir=require_completion_dir,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def inspect_current_settlement_bundle(
    *,
    cycle_receipt_path: Path = operator_status.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = operator_status.DEFAULT_SIGNAL_DIR,
    trust_registry_path: Path = source_claims.DEFAULT_TRUST_REGISTRY_PATH,
    completion_dir: Path = DEFAULT_COMPLETION_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    require_completion_dir: bool = False,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    projection = operator_status.load_operator_status(
        cycle_receipt_path=Path(cycle_receipt_path),
        signal_dir=Path(signal_dir),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    return inspect_settlement_bundle(
        projection,
        signal_dir=signal_dir,
        trust_registry_path=trust_registry_path,
        completion_dir=completion_dir,
        receipt_path=receipt_path,
        verification_path=verification_path,
        require_completion_dir=require_completion_dir,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize or inspect authenticated source-refresh completion "
            "settlement without granting provider, delivery, deployment, or "
            "execution authority."
        )
    )
    parser.add_argument(
        "--cycle-receipt", type=Path, default=operator_status.DEFAULT_CYCLE_RECEIPT
    )
    parser.add_argument(
        "--signal-dir", type=Path, default=operator_status.DEFAULT_SIGNAL_DIR
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
        "--claim-lifecycle",
        type=Path,
        default=source_claims.DEFAULT_RECEIPT_PATH,
    )
    parser.add_argument(
        "--claim-verification",
        type=Path,
        default=source_claims.DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--trust-registry",
        type=Path,
        default=source_claims.DEFAULT_TRUST_REGISTRY_PATH,
    )
    parser.add_argument(
        "--claim-dir", type=Path, default=source_claims.DEFAULT_CLAIM_DIR
    )
    parser.add_argument(
        "--completion-dir", type=Path, default=DEFAULT_COMPLETION_DIR
    )
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
        "--require-completion-dir",
        action="store_true",
        help="Fail closed when the externally owned completion directory is absent.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Verify existing settlement receipts without replacing them.",
    )
    args = parser.parse_args(argv)
    if args.inspect:
        result = inspect_current_settlement_bundle(
            cycle_receipt_path=args.cycle_receipt,
            signal_dir=args.signal_dir,
            trust_registry_path=args.trust_registry,
            completion_dir=args.completion_dir,
            receipt_path=args.receipt,
            verification_path=args.verification,
            require_completion_dir=args.require_completion_dir,
            max_age_seconds=args.max_age_seconds,
        )
    else:
        result = materialize_current_settlement_bundle(
            cycle_receipt_path=args.cycle_receipt,
            signal_dir=args.signal_dir,
            handoff_path=args.handoff,
            handoff_verification_path=args.handoff_verification,
            claim_lifecycle_path=args.claim_lifecycle,
            claim_verification_path=args.claim_verification,
            trust_registry_path=args.trust_registry,
            claim_dir=args.claim_dir,
            completion_dir=args.completion_dir,
            receipt_path=args.receipt,
            verification_path=args.verification,
            require_claim_dir=args.require_claim_dir,
            require_completion_dir=args.require_completion_dir,
            max_age_seconds=args.max_age_seconds,
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
