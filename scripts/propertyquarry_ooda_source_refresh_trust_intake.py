#!/usr/bin/env python3
"""Stage a public-key-only producer trust intake without enrolling trust."""

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

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_source_refresh_claims as claims
from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_source_refresh_trust_intake.v1"
VERIFY_SCHEMA = "propertyquarry.ooda_source_refresh_trust_intake_verification.v1"
CANDIDATE_SCHEMA = (
    "propertyquarry.ooda_source_refresh_producer_trust_candidate.v1"
)
PRESENTATION_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_presentation.v1"
)
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-intake.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-intake-verification.json"
)
DEFAULT_CANDIDATE_DIR = Path(
    "_completion/propertyquarry_ooda_producer_trust_candidates"
)
DEFAULT_PRESENTATION_STATE_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-candidate-presentation.json"
)
DEFAULT_MAX_AGE_SECONDS = claims.DEFAULT_MAX_AGE_SECONDS
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_CANDIDATE_FILES = 32
MIN_CANDIDATE_TTL_SECONDS = 60
MAX_CANDIDATE_TTL_SECONDS = 3600
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CANDIDATE_FILE = re.compile(r"([0-9a-f]{64})\.json\Z")


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return approved.canonical_json_bytes(dict(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
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
        "operator_review_required": False,
        "public_key_candidate_recorded": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
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
        "intake_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair the current signed-claim trust evidence before requesting "
            "producer public-key material; do not edit the trust registry"
        ),
        "request_id": "",
        "request_staged": False,
        "requested_lanes": [],
        "progress": {
            "current_evidence_verified": False,
            "claim_binding_verified": False,
            "trust_registry_binding_verified": False,
            "requested_lane_count": 0,
            "candidate_evidence_count": 0,
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


def _claim_source(
    claim_verification: Mapping[str, Any],
    *,
    claim_verification_receipt_sha256: str,
    trust_registry: Mapping[str, Any],
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    trust = claim_verification.get("trust")
    progress = claim_verification.get("progress")
    updated_at = _fresh_timestamp(
        claim_verification.get("updated_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    expires_at = _timestamp(claim_verification.get("expires_at"))
    claim_state = str(claim_verification.get("claim_state") or "")
    if not isinstance(trust, Mapping) or not isinstance(progress, Mapping):
        raise ValueError("source_refresh_trust_intake_claim_not_admissible")
    required_lanes = sorted(
        str(lane) for lane in list(trust.get("required_lanes") or [])
    )
    missing_lanes = sorted(
        str(lane) for lane in list(trust.get("missing_lanes") or [])
    )
    projected_trust = claims.producer_trust_posture(
        trust_registry,
        required_lanes=required_lanes,
    )
    intake_required = claim_state in {
        "producer_trust_unconfigured",
        "producer_trust_incomplete",
    }
    expected_trust_state = (
        "producer_trust_unconfigured"
        if trust_registry.get("status") == "UNCONFIGURED"
        else "producer_trust_incomplete"
    )
    if not (
        claim_verification.get("schema") == claims.VERIFY_SCHEMA
        and claim_verification.get("status") == "verified"
        and claim_state
        in {
            "producer_trust_unconfigured",
            "producer_trust_incomplete",
            "unclaimed",
            "partially_claimed",
            "claimed",
            "not_required",
        }
        and updated_at is not None
        and expires_at is not None
        and expires_at >= now
        and claims._integrity_verified(claim_verification)
        and _SHA256.fullmatch(claim_verification_receipt_sha256)
        and _SHA256.fullmatch(
            str(claim_verification.get("lifecycle_receipt_sha256") or "")
        )
        and _SHA256.fullmatch(
            str(trust.get("trust_registry_sha256") or "")
        )
        and trust.get("trust_registry_sha256")
        == trust_registry.get("trust_registry_sha256")
        and trust.get("status") == trust_registry.get("status")
        and trust.get("rotation_epoch") == trust_registry.get("rotation_epoch")
        and trust.get("configured") == projected_trust["configured"]
        and trust.get("active_producer_count")
        == projected_trust["active_producer_count"]
        and trust.get("required_lanes") == projected_trust["required_lanes"]
        and trust.get("trusted_lanes") == projected_trust["trusted_lanes"]
        and trust.get("missing_lanes") == projected_trust["missing_lanes"]
        and trust.get("producer_trust_ready")
        == projected_trust["producer_trust_ready"]
        and len(missing_lanes) == len(set(missing_lanes))
        and all(lane in source_refresh._LANE_ARTIFACTS for lane in missing_lanes)
        and bool(missing_lanes) is intake_required
        and (trust.get("producer_trust_ready") is False) is intake_required
        and (not intake_required or claim_state == expected_trust_state)
        and progress.get("current_evidence_verified") is True
        and progress.get("handoff_binding_verified") is True
        and progress.get("lifecycle_integrity_verified") is True
        and claim_verification.get("action_required") is False
        and claim_verification.get("interrupt_operator") is False
        and claim_verification.get("producer_dispatch_authorized") is False
        and claim_verification.get("producer_refresh_authorized") is False
        and claim_verification.get("provider_quota_consumption_allowed") is False
        and claim_verification.get("delivery_authorized") is False
        and claim_verification.get("protected_operation_executed") is False
    ):
        raise ValueError("source_refresh_trust_intake_claim_not_admissible")
    return {
        "claim_state": claim_state,
        "claim_updated_at": updated_at,
        "claim_expires_at": expires_at.isoformat(),
        "claim_verification_receipt_sha256": (
            claim_verification_receipt_sha256
        ),
        "claim_lifecycle_receipt_sha256": str(
            claim_verification.get("lifecycle_receipt_sha256") or ""
        ),
        "handoff_id": str(claim_verification.get("handoff_id") or ""),
        "request_id": str(claim_verification.get("request_id") or ""),
        "trust_registry_status": str(trust.get("status") or ""),
        "trust_rotation_epoch": int(trust.get("rotation_epoch") or 0),
        "trust_registry_sha256": str(
            trust.get("trust_registry_sha256") or ""
        ),
        "active_producer_count": int(
            trust.get("active_producer_count") or 0
        ),
        "missing_lanes": missing_lanes,
        "intake_required": intake_required,
    }


def _candidate_requirements() -> dict[str, Any]:
    return {
        "schema": CANDIDATE_SCHEMA,
        "required_fields": [
            "schema",
            "producer_id",
            "key_id",
            "algorithm",
            "public_key",
            "public_key_sha256",
            "lanes",
            "requested_status",
            "request_id",
            "semantic_request_sha256",
            "trust_registry_sha256",
            "issued_at",
            "expires_at",
            "nonce",
            "candidate",
            "proof_signature",
        ],
        "algorithm": "Ed25519",
        "public_key_encoding": "base64url_unpadded_32_bytes",
        "fingerprint_algorithm": "sha256",
        "requested_status": "ACTIVE",
        "private_key_material_allowed": False,
        "out_of_band_identity_verification_required": True,
        "operator_enrollment_decision_required_after_candidate": True,
        "candidate_confers_authority": False,
    }


def _candidate_directory(path: Path) -> tuple[bool, dict[str, Path]]:
    target = Path(path).absolute()
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return False, {}
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError("source_refresh_trust_candidate_directory_not_admissible")
    paths: dict[str, Path] = {}
    with os.scandir(target) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_CANDIDATE_FILES:
                raise ValueError("source_refresh_trust_candidate_directory_too_large")
            match = _CANDIDATE_FILE.fullmatch(entry.name)
            if (
                match is None
                or not entry.is_file(follow_symlinks=False)
                or entry.is_symlink()
                or match.group(1) in paths
            ):
                raise ValueError("source_refresh_trust_candidate_entry_not_admissible")
            paths[match.group(1)] = Path(entry.path)
    return True, paths


def _verify_candidate_evidence(
    candidate: Mapping[str, Any],
    *,
    candidate_path: Path,
    candidate_receipt_sha256: str,
    expected_public_key_sha256: str,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    expected_keys = set(_candidate_requirements()["required_fields"])
    scope = candidate.get("candidate")
    if set(candidate) != expected_keys or not isinstance(scope, Mapping):
        raise ValueError("source_refresh_trust_candidate_fields_not_admissible")
    expected_scope_keys = {
        "state",
        "producer_controls_private_key",
        "out_of_band_identity_verification_required",
        "candidate_confers_authority",
        "trust_enrollment_authorized",
        "provider_operation_authorized",
        "delivery_authorized",
        "private_key_material_included",
    }
    if set(scope) != expected_scope_keys:
        raise ValueError("source_refresh_trust_candidate_scope_not_admissible")
    producer_id = str(candidate.get("producer_id") or "")
    key_id = str(candidate.get("key_id") or "")
    public_key_sha256 = str(candidate.get("public_key_sha256") or "")
    request_id = str(candidate.get("request_id") or "")
    semantic_request_sha256 = str(
        candidate.get("semantic_request_sha256") or ""
    )
    trust_registry_sha256 = str(
        candidate.get("trust_registry_sha256") or ""
    )
    lanes = candidate.get("lanes")
    issued_at = _timestamp(candidate.get("issued_at"))
    expires_at = _timestamp(candidate.get("expires_at"))
    if issued_at is None or expires_at is None:
        raise ValueError("source_refresh_trust_candidate_timestamp_invalid")
    ttl_seconds = (expires_at - issued_at).total_seconds()
    age_seconds = (now - issued_at).total_seconds()
    public_key = claims._decode_base64url(
        candidate.get("public_key"),
        expected_bytes=32,
        label="source_refresh_trust_candidate_public_key",
    )
    signature = claims._decode_base64url(
        candidate.get("proof_signature"),
        expected_bytes=64,
        label="source_refresh_trust_candidate_proof_signature",
    )
    nonce = str(candidate.get("nonce") or "")
    if not (
        candidate.get("schema") == CANDIDATE_SCHEMA
        and claims._IDENTIFIER.fullmatch(producer_id)
        and claims._IDENTIFIER.fullmatch(key_id)
        and candidate.get("algorithm") == "Ed25519"
        and _SHA256.fullmatch(public_key_sha256)
        and public_key_sha256 == expected_public_key_sha256
        and public_key_sha256 == _sha256(public_key)
        and candidate.get("requested_status") == "ACTIVE"
        and isinstance(lanes, list)
        and lanes == sorted(set(lanes))
        and bool(lanes)
        and all(lane in source_refresh._LANE_ARTIFACTS for lane in lanes)
        and _SHA256.fullmatch(semantic_request_sha256)
        and _SHA256.fullmatch(trust_registry_sha256)
        and request_id.startswith("pqtrustintake_")
        and len(request_id) == len("pqtrustintake_") + 24
        and claims._NONCE.fullmatch(nonce)
        and math.isfinite(ttl_seconds)
        and MIN_CANDIDATE_TTL_SECONDS
        <= ttl_seconds
        <= MAX_CANDIDATE_TTL_SECONDS
        and math.isfinite(age_seconds)
        and -30.0 <= age_seconds <= max_age_seconds
        and now <= expires_at
        and scope.get("state") == "proposed"
        and scope.get("producer_controls_private_key") is True
        and scope.get("out_of_band_identity_verification_required") is True
        and scope.get("candidate_confers_authority") is False
        and scope.get("trust_enrollment_authorized") is False
        and scope.get("provider_operation_authorized") is False
        and scope.get("delivery_authorized") is False
        and scope.get("private_key_material_included") is False
        and _SHA256.fullmatch(candidate_receipt_sha256)
    ):
        raise ValueError("source_refresh_trust_candidate_not_admissible")
    signed_payload = {
        key: value
        for key, value in candidate.items()
        if key != "proof_signature"
    }
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            _canonical(signed_payload),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ValueError(
            "source_refresh_trust_candidate_proof_invalid"
        ) from exc
    return {
        "producer_id": producer_id,
        "key_id": key_id,
        "algorithm": "Ed25519",
        "public_key": str(candidate.get("public_key") or ""),
        "public_key_sha256": public_key_sha256,
        "lanes": list(lanes),
        "requested_status": "ACTIVE",
        "request_id": request_id,
        "semantic_request_sha256": semantic_request_sha256,
        "trust_registry_sha256": trust_registry_sha256,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "candidate_path": str(Path(candidate_path).absolute()),
        "candidate_receipt_sha256": candidate_receipt_sha256,
        "signed_payload_sha256": _sha256(_canonical(signed_payload)),
        "nonce_sha256": _sha256(nonce.encode("utf-8")),
        "proof_of_possession_verified": True,
        "out_of_band_identity_verification_required": True,
        "candidate_confers_authority": False,
        "trust_enrollment_authorized": False,
        "provider_operation_authorized": False,
        "delivery_authorized": False,
        "private_key_material_recorded": False,
    }


def _candidate_evidence(
    candidate_dir: Path,
    *,
    request_id: str,
    semantic_request_sha256: str,
    trust_registry_sha256: str,
    requested_lanes: list[str],
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    directory_present, paths = _candidate_directory(candidate_dir)
    current: list[dict[str, Any]] = []
    non_current_count = 0
    identities: set[tuple[str, str]] = set()
    nonce_hashes: set[str] = set()
    for public_key_sha256, path in sorted(paths.items()):
        candidate, _raw, receipt_digest = claims._read_only_snapshot(
            path,
            field="source_refresh_trust_candidate",
            maximum_bytes=MAX_RECEIPT_BYTES,
        )
        verified = _verify_candidate_evidence(
            candidate,
            candidate_path=path,
            candidate_receipt_sha256=receipt_digest,
            expected_public_key_sha256=public_key_sha256,
            now=now,
            max_age_seconds=max_age_seconds,
        )
        candidate_lanes = set(str(lane) for lane in verified["lanes"])
        current_binding = bool(
            verified["request_id"] == request_id
            and verified["semantic_request_sha256"]
            == semantic_request_sha256
            and verified["trust_registry_sha256"] == trust_registry_sha256
            and candidate_lanes <= set(requested_lanes)
        )
        if not current_binding:
            non_current_count += 1
            continue
        identity = (verified["producer_id"], verified["key_id"])
        if identity in identities or verified["nonce_sha256"] in nonce_hashes:
            raise ValueError("source_refresh_trust_candidate_replay_detected")
        identities.add(identity)
        nonce_hashes.add(str(verified["nonce_sha256"]))
        current.append(verified)
    covered_lanes = sorted(
        {
            str(lane)
            for candidate in current
            for lane in list(candidate.get("lanes") or [])
        }
    )
    return {
        "directory_present": directory_present,
        "candidate_file_count": len(paths),
        "current_candidates": current,
        "non_current_candidate_file_count": non_current_count,
        "covered_lanes": covered_lanes,
        "remaining_lanes": sorted(set(requested_lanes) - set(covered_lanes)),
    }


def build_trust_intake(
    claim_verification: Mapping[str, Any],
    *,
    claim_verification_receipt_sha256: str,
    trust_registry: Mapping[str, Any],
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    source = _claim_source(
        claim_verification,
        claim_verification_receipt_sha256=claim_verification_receipt_sha256,
        trust_registry=trust_registry,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    requirements = _candidate_requirements()
    semantic = {
        "source_refresh_request_id": source["request_id"],
        "source_refresh_handoff_id": source["handoff_id"],
        "trust_registry_sha256": source["trust_registry_sha256"],
        "trust_rotation_epoch": source["trust_rotation_epoch"],
        "requested_lanes": source["missing_lanes"],
        "candidate_requirements": requirements,
    }
    semantic_request_sha256 = _sha256(_canonical(semantic))
    request_id = f"pqtrustintake_{semantic_request_sha256[:24]}"
    staged = source["intake_required"] is True
    if staged:
        candidate_evidence = _candidate_evidence(
            candidate_dir,
            request_id=request_id,
            semantic_request_sha256=semantic_request_sha256,
            trust_registry_sha256=source["trust_registry_sha256"],
            requested_lanes=source["missing_lanes"],
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
    else:
        directory_present, paths = _candidate_directory(candidate_dir)
        candidate_evidence = {
            "directory_present": directory_present,
            "candidate_file_count": len(paths),
            "current_candidates": [],
            "non_current_candidate_file_count": len(paths),
            "covered_lanes": [],
            "remaining_lanes": [],
        }
    candidates = list(candidate_evidence["current_candidates"])
    review_semantic = {
        "request_id": request_id,
        "semantic_request_sha256": semantic_request_sha256,
        "candidates": [
            {
                "producer_id": candidate["producer_id"],
                "key_id": candidate["key_id"],
                "public_key_sha256": candidate["public_key_sha256"],
                "lanes": candidate["lanes"],
                "candidate_receipt_sha256": candidate[
                    "candidate_receipt_sha256"
                ],
            }
            for candidate in candidates
        ],
    }
    candidate_review_id = (
        f"pqtrustreview_{_sha256(_canonical(review_semantic))[:24]}"
        if candidates
        else ""
    )
    if candidates:
        intake_state = "candidate_ready_for_operator_review"
        next_action = (
            "verify the producer identity and Ed25519 fingerprints out of band, "
            "then record a separate confirm, reject, or defer review decision; "
            "this candidate receipt does not enroll trust or authorize registry edits"
        )
    elif staged:
        intake_state = "awaiting_producer_public_key_evidence"
        next_action = (
            "obtain producer-owned Ed25519 public-key metadata for the missing "
            f"lanes {','.join(source['missing_lanes'])} and stage only that public "
            "candidate for out-of-band operator identity verification; never "
            "provide a private key or edit the trust registry automatically"
        )
    else:
        intake_state = "not_required"
        next_action = "continue verifying producer claims under current public-key trust"
    intake = {
        "schema": SCHEMA,
        "status": "staged" if staged else "not_required",
        "intake_state": intake_state,
        "request_id": request_id,
        "semantic_request_sha256": semantic_request_sha256,
        "candidate_review_id": candidate_review_id,
        "review_decision_options": (
            ["confirm_identity_verified", "reject_candidate", "defer"]
            if candidates
            else []
        ),
        "generated_at": observed_now.isoformat(),
        "expires_at": source["claim_expires_at"],
        "blocking_reason": "",
        "next_action": next_action,
        "request_staged": staged,
        "requested_lanes": source["missing_lanes"],
        "candidate_submission_directory": str(Path(candidate_dir).absolute()),
        "candidate_requirements": requirements,
        "candidate_directory": {
            "path": str(Path(candidate_dir).absolute()),
            "present": candidate_evidence["directory_present"],
            "candidate_file_count": candidate_evidence[
                "candidate_file_count"
            ],
            "current_candidate_file_count": len(candidates),
            "non_current_candidate_file_count": candidate_evidence[
                "non_current_candidate_file_count"
            ],
        },
        "candidates": candidates,
        "candidate_coverage": {
            "covered_lanes": candidate_evidence["covered_lanes"],
            "remaining_lanes": candidate_evidence["remaining_lanes"],
            "all_requested_lanes_covered": bool(
                staged and not candidate_evidence["remaining_lanes"]
            ),
        },
        "source_binding": {
            key: value for key, value in source.items() if key != "missing_lanes"
        },
        "progress": {
            "current_evidence_verified": True,
            "claim_binding_verified": True,
            "trust_registry_binding_verified": True,
            "requested_lane_count": len(source["missing_lanes"]),
            "candidate_evidence_count": len(candidates),
            "candidate_proof_signature_count": len(candidates),
            "covered_lane_count": len(candidate_evidence["covered_lanes"]),
            "remaining_lane_count": len(
                candidate_evidence["remaining_lanes"]
            ),
            "non_current_candidate_file_count": candidate_evidence[
                "non_current_candidate_file_count"
            ],
        },
        **_safety_fields(),
        "action_required": bool(candidates),
        "interrupt_operator": bool(candidates),
        "operator_review_required": bool(candidates),
        "public_key_candidate_recorded": bool(candidates),
    }
    return _integrity_bound(intake)


def verify_trust_intake(
    intake: Mapping[str, Any],
    *,
    claim_verification: Mapping[str, Any],
    claim_verification_receipt_sha256: str,
    trust_registry: Mapping[str, Any],
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    generated_at = _fresh_timestamp(
        intake.get("generated_at"),
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    expires_at = _timestamp(intake.get("expires_at"))
    if (
        generated_at is None
        or expires_at is None
        or expires_at < observed_now
        or not _integrity_verified(intake)
    ):
        return _blocked("source_refresh_trust_intake_not_fresh", now=observed_now)
    try:
        expected = build_trust_intake(
            claim_verification,
            claim_verification_receipt_sha256=(
                claim_verification_receipt_sha256
            ),
            trust_registry=trust_registry,
            candidate_dir=candidate_dir,
            now=datetime.fromisoformat(generated_at),
            max_age_seconds=max_age_seconds,
        )
    except (OSError, TypeError, ValueError):
        return _blocked(
            "source_refresh_trust_intake_source_not_admissible",
            now=observed_now,
        )
    if dict(intake) != expected:
        return _blocked(
            "source_refresh_trust_intake_binding_mismatch",
            now=observed_now,
        )
    verification = {
        key: value
        for key, value in intake.items()
        if key not in {"schema", "integrity", "status", "generated_at"}
    }
    verification.update(
        {
            "schema": VERIFY_SCHEMA,
            "status": "verified",
            "request_status": str(intake.get("status") or ""),
            "updated_at": observed_now.isoformat(),
            "intake_generated_at": str(intake.get("generated_at") or ""),
            "progress": {
                **dict(intake.get("progress") or {}),
                "intake_integrity_verified": True,
            },
        }
    )
    return _integrity_bound(verification)


def materialize_trust_intake_bundle(
    *,
    claim_verification_path: Path = claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = claims.DEFAULT_TRUST_REGISTRY_PATH,
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    receipt_target = Path(receipt_path).absolute()
    verification_target = Path(verification_path).absolute()
    try:
        claim_verification, _claim_raw, claim_digest = _private_snapshot(
            claim_verification_path,
            field="source_refresh_trust_intake_claim_verification",
        )
        trust_registry = claims.load_producer_trust_registry(
            trust_registry_path
        )
        intake = build_trust_intake(
            claim_verification,
            claim_verification_receipt_sha256=claim_digest,
            trust_registry=trust_registry,
            candidate_dir=candidate_dir,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        atomic_write_bytes(
            receipt_target,
            _canonical(intake),
            overwrite=True,
        )
        persisted, receipt_raw, receipt_digest = _private_snapshot(
            receipt_target,
            field="source_refresh_trust_intake",
        )
        if persisted != intake:
            raise ValueError("source_refresh_trust_intake_persistence_mismatch")
        verification = verify_trust_intake(
            persisted,
            claim_verification=claim_verification,
            claim_verification_receipt_sha256=claim_digest,
            trust_registry=trust_registry,
            candidate_dir=candidate_dir,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        if verification.get("status") != "verified":
            raise ValueError("source_refresh_trust_intake_verification_failed")
        verification["intake_receipt_sha256"] = receipt_digest
        verification["progress"] = {
            **dict(verification.get("progress") or {}),
            "intake_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
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
            _private_snapshot(
                verification_target,
                field="source_refresh_trust_intake_verification",
            )
        )
        if persisted_verification != verification or not _integrity_verified(
            persisted_verification
        ):
            raise ValueError(
                "source_refresh_trust_intake_verification_persistence_mismatch"
            )
    except Exception:
        return _blocked(
            "source_refresh_trust_intake_evidence_or_persistence_not_admissible",
            now=observed_now,
        )
    result = dict(verification)
    result.pop("integrity", None)
    result.update(
        {
            "receipt_path": str(receipt_target),
            "verification_path": str(verification_target),
            "intake_receipt_sha256": receipt_digest,
            "intake_receipt_bytes": len(receipt_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "intake_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def inspect_trust_intake_bundle(
    *,
    claim_verification_path: Path = claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = claims.DEFAULT_TRUST_REGISTRY_PATH,
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Verify existing intake receipts without replacing them."""

    observed_now = _now(now)
    try:
        claim_verification, _claim_raw, claim_digest = _private_snapshot(
            claim_verification_path,
            field="source_refresh_trust_intake_claim_verification",
        )
        trust_registry = claims.load_producer_trust_registry(
            trust_registry_path
        )
        intake, receipt_raw, receipt_digest = _private_snapshot(
            receipt_path,
            field="source_refresh_trust_intake",
        )
        persisted_verification, verification_raw, verification_digest = (
            _private_snapshot(
                verification_path,
                field="source_refresh_trust_intake_verification",
            )
        )
        current_verification = verify_trust_intake(
            intake,
            claim_verification=claim_verification,
            claim_verification_receipt_sha256=claim_digest,
            trust_registry=trust_registry,
            candidate_dir=candidate_dir,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        expected_progress = {
            **dict(current_verification.get("progress") or {}),
            "intake_receipt_persisted": True,
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
                "intake_receipt_sha256",
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
            and persisted_verification.get("intake_receipt_sha256")
            == receipt_digest
            and persisted_without_variable == current_without_variable
            and dict(persisted_verification.get("progress") or {})
            == expected_progress
        ):
            raise ValueError("source_refresh_trust_intake_bundle_not_current")
    except Exception:
        return _blocked(
            "source_refresh_trust_intake_bundle_not_admissible",
            now=observed_now,
        )
    result = dict(persisted_verification)
    result.pop("integrity", None)
    result.update(
        {
            "receipt_path": str(Path(receipt_path).absolute()),
            "verification_path": str(Path(verification_path).absolute()),
            "intake_receipt_sha256": receipt_digest,
            "intake_receipt_bytes": len(receipt_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "intake_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def _review_is_admissible(report: Mapping[str, Any]) -> bool:
    candidates = report.get("candidates")
    progress = report.get("progress")
    review_id = str(report.get("candidate_review_id") or "")
    return bool(
        report.get("schema") == VERIFY_SCHEMA
        and report.get("status") == "verified"
        and report.get("intake_state")
        == "candidate_ready_for_operator_review"
        and review_id.startswith("pqtrustreview_")
        and len(review_id) == len("pqtrustreview_") + 24
        and isinstance(candidates, list)
        and bool(candidates)
        and isinstance(progress, Mapping)
        and progress.get("candidate_evidence_count") == len(candidates)
        and _SHA256.fullmatch(
            str(report.get("intake_receipt_sha256") or "")
        )
        and report.get("action_required") is True
        and report.get("operator_review_required") is True
        and report.get("public_key_candidate_recorded") is True
        and report.get("private_key_material_requested") is False
        and report.get("private_key_material_recorded") is False
        and report.get("trust_enrollment_authorized") is False
        and report.get("trust_registry_modified") is False
        and report.get("producer_dispatch_authorized") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("delivery_authorized") is False
        and report.get("protected_operation_executed") is False
        and all(
            isinstance(candidate, Mapping)
            and candidate.get("proof_of_possession_verified") is True
            and candidate.get("out_of_band_identity_verification_required")
            is True
            and candidate.get("candidate_confers_authority") is False
            and candidate.get("trust_enrollment_authorized") is False
            and _SHA256.fullmatch(
                str(candidate.get("candidate_receipt_sha256") or "")
            )
            for candidate in candidates
        )
    )


def apply_candidate_presentation_state(
    report: Mapping[str, Any],
    *,
    state_path: Path = DEFAULT_PRESENTATION_STATE_PATH,
) -> dict[str, Any]:
    """Suppress repeat interruption for an unchanged candidate review."""

    projected = dict(report)
    if report.get("status") != "verified" or report.get("action_required") is not True:
        projected["interrupt_operator"] = False
        projected["presentation"] = {
            "state": "not_required",
            "candidate_review_id": "",
            "already_presented": False,
        }
        return projected
    if not _review_is_admissible(report):
        raise ValueError("source_refresh_trust_candidate_review_not_admissible")
    target = Path(state_path).absolute()
    try:
        state, _raw, _digest = _private_snapshot(
            target,
            field="source_refresh_trust_candidate_presentation",
        )
    except FileNotFoundError:
        state = {}
    if state:
        expected_keys = {
            "schema",
            "candidate_review_id",
            "intake_receipt_sha256",
            "candidate_receipt_sha256s",
            "presented_at",
            "delivery_state_updated",
            "provider_quota_consumed",
            "trust_registry_modified",
            "protected_operation_executed",
            "integrity",
        }
        presented_at = _timestamp(state.get("presented_at"))
        if not (
            set(state) == expected_keys
            and state.get("schema") == PRESENTATION_SCHEMA
            and presented_at is not None
            and _integrity_verified(state)
            and state.get("delivery_state_updated") is False
            and state.get("provider_quota_consumed") is False
            and state.get("trust_registry_modified") is False
            and state.get("protected_operation_executed") is False
            and isinstance(state.get("candidate_receipt_sha256s"), list)
            and all(
                _SHA256.fullmatch(str(digest or ""))
                for digest in state.get("candidate_receipt_sha256s")
            )
        ):
            raise ValueError(
                "source_refresh_trust_candidate_presentation_not_admissible"
            )
    already_presented = bool(
        state
        and state.get("candidate_review_id")
        == report.get("candidate_review_id")
    )
    projected["interrupt_operator"] = not already_presented
    projected["presentation"] = {
        "state": "already_presented" if already_presented else "novel",
        "candidate_review_id": str(report.get("candidate_review_id") or ""),
        "already_presented": already_presented,
    }
    return projected


def record_candidate_presentation(
    report: Mapping[str, Any],
    *,
    state_path: Path = DEFAULT_PRESENTATION_STATE_PATH,
    expected_candidate_review_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record only the exact candidate review that was just presented."""

    observed_now = _now(now)
    if report.get("action_required") is not True:
        return {
            "status": "not_required",
            "candidate_review_id": "",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "trust_registry_modified": False,
            "protected_operation_executed": False,
        }
    if not (
        _review_is_admissible(report)
        and str(report.get("candidate_review_id") or "")
        == expected_candidate_review_id
    ):
        return {
            "status": "blocked",
            "blocking_reason": "source_refresh_trust_candidate_presentation_binding_mismatch",
            "candidate_review_id": "",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "trust_registry_modified": False,
            "protected_operation_executed": False,
        }
    target = Path(state_path).absolute()
    digests = sorted(
        str(candidate.get("candidate_receipt_sha256") or "")
        for candidate in list(report.get("candidates") or [])
        if isinstance(candidate, Mapping)
    )
    state = _integrity_bound(
        {
            "schema": PRESENTATION_SCHEMA,
            "candidate_review_id": expected_candidate_review_id,
            "intake_receipt_sha256": str(
                report.get("intake_receipt_sha256") or ""
            ),
            "candidate_receipt_sha256s": digests,
            "presented_at": observed_now.isoformat(),
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "trust_registry_modified": False,
            "protected_operation_executed": False,
        }
    )
    try:
        atomic_write_bytes(target, _canonical(state), overwrite=True)
        persisted, _raw, digest = _private_snapshot(
            target,
            field="source_refresh_trust_candidate_presentation",
        )
        if persisted != state or not _integrity_verified(persisted):
            raise ValueError(
                "source_refresh_trust_candidate_presentation_persistence_mismatch"
            )
    except Exception:
        return {
            "status": "blocked",
            "blocking_reason": "source_refresh_trust_candidate_presentation_persistence_failed",
            "candidate_review_id": "",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "trust_registry_modified": False,
            "protected_operation_executed": False,
        }
    return {
        "status": "recorded",
        "candidate_review_id": expected_candidate_review_id,
        "presentation_receipt_sha256": digest,
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "trust_registry_modified": False,
        "protected_operation_executed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage a public-key-only producer trust intake request without "
            "modifying trust or granting provider, delivery, or execution authority."
        )
    )
    parser.add_argument(
        "--claim-verification",
        type=Path,
        default=claims.DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--trust-registry",
        type=Path,
        default=claims.DEFAULT_TRUST_REGISTRY_PATH,
    )
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=DEFAULT_CANDIDATE_DIR,
    )
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument(
        "--verification",
        type=Path,
        default=DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="verify existing intake receipts without replacing them",
    )
    args = parser.parse_args(argv)
    operation = (
        inspect_trust_intake_bundle
        if args.inspect
        else materialize_trust_intake_bundle
    )
    result = operation(
        claim_verification_path=args.claim_verification,
        trust_registry_path=args.trust_registry,
        candidate_dir=args.candidate_dir,
        receipt_path=args.receipt,
        verification_path=args.verification,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
