#!/usr/bin/env python3
"""Stage one path-free public request for an external producer candidate."""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


from scripts import propertyquarry_ooda_source_refresh_trust_candidate_import as candidate_import
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_artifact_request.v1"
)
VERIFY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_artifact_request_verification.v1"
)
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-candidate-artifact-request.json"
)
DEFAULT_MAX_AGE_SECONDS = trust_intake.DEFAULT_MAX_AGE_SECONDS
_STAGED_STATES = {
    "awaiting_external_artifact",
    "external_artifact_not_admissible",
}
_NOT_REQUIRED_STATES = {
    "ready_for_manual_import",
    "not_required",
    "succeeded",
    "recovery_required",
}


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return candidate_import._canonical(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _with_integrity(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("integrity", None)
    result["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(result)),
    }
    return result


def _integrity_verified(value: Mapping[str, Any]) -> bool:
    normalized = dict(value)
    integrity = normalized.pop("integrity", None)
    return bool(
        isinstance(integrity, Mapping)
        and frozenset(integrity)
        == {"algorithm", "canonical_payload_sha256"}
        and integrity.get("algorithm") == "sha256"
        and candidate_import._SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    )


def _contains_absolute_path(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(item) for item in value)
    return isinstance(value, str) and Path(value).is_absolute()


def _safety_fields() -> dict[str, bool]:
    return {
        **candidate_import._safety_fields(),
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "candidate_imported": False,
        "public_key_candidate_recorded": False,
        "producer_contacted": False,
        "transport_delivery_attempted": False,
        "host_path_values_recorded": False,
        "candidate_payload_recorded": False,
        "public_key_material_recorded": False,
    }


def _public_request(report: Mapping[str, Any]) -> dict[str, Any]:
    if not candidate_import._presentation_report_admissible(report):
        raise ValueError("trust_candidate_artifact_request_source_not_admissible")
    artifact = dict(report.get("artifact_request") or {})
    expires_at = candidate_import._timestamp(artifact.get("expires_at"))
    if expires_at is None:
        raise ValueError("trust_candidate_artifact_request_expiry_invalid")
    request = {
        "artifact_kind": artifact["artifact_kind"],
        "candidate_schema": artifact["candidate_schema"],
        "candidate_requirements": dict(
            artifact.get("candidate_requirements") or {}
        ),
        "request_id": artifact["request_id"],
        "semantic_request_sha256": artifact["semantic_request_sha256"],
        "trust_registry_sha256": artifact["trust_registry_sha256"],
        "requested_lanes": list(artifact.get("requested_lanes") or []),
        "request_expires_at": expires_at.isoformat(),
        "proof_contract": {
            "algorithm": "Ed25519",
            "public_key_encoding": "base64url_unpadded_32_bytes",
            "signature_encoding": "base64url_unpadded_64_bytes",
            "signature_payload": (
                "canonical_strict_json_candidate_without_proof_signature"
            ),
            "proof_of_private_key_possession_required": True,
            "private_key_material_must_not_be_included": True,
        },
        "candidate_validity_contract": {
            "issued_at_must_be_current": True,
            "minimum_ttl_seconds": trust_intake.MIN_CANDIDATE_TTL_SECONDS,
            "maximum_ttl_seconds": trust_intake.MAX_CANDIDATE_TTL_SECONDS,
            "maximum_accepted_age_seconds": (
                trust_intake.DEFAULT_MAX_AGE_SECONDS
            ),
        },
        "candidate_scope": {
            "state": "proposed",
            "producer_controls_private_key": True,
            "out_of_band_identity_verification_required": True,
            "candidate_confers_authority": False,
            "trust_enrollment_authorized": False,
            "provider_operation_authorized": False,
            "delivery_authorized": False,
            "private_key_material_included": False,
        },
        "submission_contract": {
            "format": "strict_json_object",
            "filename_pattern": "*.json",
            "operator_managed_drop_directory_mode": "0750",
            "candidate_file_mode": "0640",
            "scheduler_access": "supplemental_group_read_only",
            "group_write_allowed": False,
            "other_access_allowed": False,
            "automatic_import_allowed": False,
        },
        "authority_contract": {
            "candidate_confers_authority": False,
            "manual_hash_bound_import_required": True,
            "out_of_band_identity_review_required_after_import": True,
            "separate_exact_enrollment_authorization_required": True,
            "provider_access_authorized": False,
            "delivery_authorized": False,
            "deployment_or_restart_authorized": False,
        },
    }
    if _contains_absolute_path(request):
        raise ValueError("trust_candidate_artifact_request_contains_host_path")
    return request


def build_candidate_artifact_request(
    report: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project a strict public packet or an expiry-safe inactive tombstone."""

    observed_now = _now(now)
    import_state = str(report.get("import_state") or "")
    if report.get("status") != "verified" or import_state not in (
        _STAGED_STATES | _NOT_REQUIRED_STATES
    ):
        raise ValueError("trust_candidate_artifact_request_source_not_current")
    request: dict[str, Any] = {}
    status = "not_required"
    request_id = ""
    expires_at = ""
    request_semantic_sha256 = ""
    intake_verification_sha256 = ""
    if import_state in _STAGED_STATES:
        request = _public_request(report)
        parsed_expiry = candidate_import._timestamp(
            request.get("request_expires_at")
        )
        if parsed_expiry is None or observed_now > parsed_expiry:
            raise ValueError("trust_candidate_artifact_request_expired")
        status = "staged"
        request_id = str(request.get("request_id") or "")
        expires_at = parsed_expiry.isoformat()
        request_semantic_sha256 = _sha256(_canonical(request))
        intake_verification_sha256 = str(
            report.get("intake_verification_sha256") or ""
        )
        if not candidate_import._SHA256.fullmatch(
            intake_verification_sha256
        ):
            raise ValueError(
                "trust_candidate_artifact_request_intake_binding_invalid"
            )
    receipt = _with_integrity(
        {
            "schema": SCHEMA,
            "status": status,
            "request_state": import_state,
            "updated_at": observed_now.isoformat(),
            "expires_at": expires_at,
            "request_id": request_id,
            "request_semantic_sha256": request_semantic_sha256,
            "public_request": request,
            "source_binding": {
                "intake_verification_sha256": intake_verification_sha256,
                "candidate_import_state": import_state,
            },
            "action_required": status == "staged",
            "interrupt_operator": False,
            "artifact_request_staged": status == "staged",
            "read_only_source_projection": True,
            **_safety_fields(),
        }
    )
    if _contains_absolute_path(receipt):
        raise ValueError("trust_candidate_artifact_request_contains_host_path")
    return receipt


def _receipt_verified(
    value: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    updated_at = candidate_import._timestamp(value.get("updated_at"))
    if updated_at is None:
        return False
    age_seconds = (now - updated_at).total_seconds()
    if not -30.0 <= age_seconds <= max_age_seconds:
        return False
    try:
        expected = build_candidate_artifact_request(
            report,
            now=updated_at,
        )
    except (TypeError, ValueError):
        return False
    expires_at = candidate_import._timestamp(value.get("expires_at"))
    return bool(
        value == expected
        and _integrity_verified(value)
        and not _contains_absolute_path(value)
        and (
            value.get("status") != "staged"
            or (expires_at is not None and now <= expires_at)
        )
    )


def _verification(
    receipt: Mapping[str, Any],
    *,
    receipt_sha256: str,
    now: datetime,
) -> dict[str, Any]:
    staged = receipt.get("status") == "staged"
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "request_state": str(receipt.get("request_state") or ""),
        "updated_at": now.isoformat(),
        "expires_at": str(receipt.get("expires_at") or ""),
        "request_id": str(receipt.get("request_id") or ""),
        "request_semantic_sha256": str(
            receipt.get("request_semantic_sha256") or ""
        ),
        "artifact_request_receipt_sha256": receipt_sha256,
        "artifact_request_staged": staged,
        "action_required": staged,
        "interrupt_operator": False,
        "receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "receipt_integrity_verified": True,
            "path_free_projection_verified": True,
            "public_only_projection_verified": True,
        },
        **_safety_fields(),
    }


def materialize_candidate_artifact_request(
    report: Mapping[str, Any],
    *,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Persist only the redacted public request; never send or import it."""

    observed_now = _now(now)
    receipt = build_candidate_artifact_request(report, now=observed_now)
    target = Path(receipt_path).absolute()
    atomic_write_bytes(target, _canonical(receipt), overwrite=True)
    persisted, raw, digest = candidate_import._private_object(
        target,
        field="trust_candidate_artifact_request",
    )
    if not _receipt_verified(
        persisted,
        report,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    ):
        raise ValueError("trust_candidate_artifact_request_persistence_invalid")
    if raw != _canonical(receipt) or digest != _sha256(raw):
        raise ValueError("trust_candidate_artifact_request_digest_mismatch")
    return _verification(
        persisted,
        receipt_sha256=digest,
        now=observed_now,
    )


def inspect_candidate_artifact_request(
    report: Mapping[str, Any],
    *,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Verify one persisted request against current candidate-intake truth."""

    observed_now = _now(now)
    receipt, raw, digest = candidate_import._private_object(
        Path(receipt_path).absolute(),
        field="trust_candidate_artifact_request",
    )
    if not _receipt_verified(
        receipt,
        report,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    ):
        raise ValueError("trust_candidate_artifact_request_not_current")
    if digest != _sha256(raw):
        raise ValueError("trust_candidate_artifact_request_digest_mismatch")
    return _verification(
        receipt,
        receipt_sha256=digest,
        now=observed_now,
    )


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "request_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "artifact_request_staged": False,
        "action_required": False,
        "interrupt_operator": False,
        "receipt_persisted": False,
        "progress": {"current_evidence_verified": False},
        **_safety_fields(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--materialize-current", action="store_true")
    operation.add_argument("--inspect-current", action="store_true")
    parser.add_argument(
        "--claim-verification",
        type=Path,
        default=candidate_import.claims.DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--trust-registry",
        type=Path,
        default=candidate_import.claims.DEFAULT_TRUST_REGISTRY_PATH,
    )
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=trust_intake.DEFAULT_CANDIDATE_DIR,
    )
    parser.add_argument(
        "--intake-receipt",
        type=Path,
        default=trust_intake.DEFAULT_RECEIPT_PATH,
    )
    parser.add_argument(
        "--intake-verification",
        type=Path,
        default=trust_intake.DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--import-dir",
        type=Path,
        default=candidate_import.DEFAULT_IMPORT_DIR,
    )
    parser.add_argument(
        "--source-discovery-dir",
        type=Path,
        default=candidate_import.DEFAULT_SOURCE_DISCOVERY_DIR,
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=DEFAULT_RECEIPT_PATH,
    )
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=DEFAULT_MAX_AGE_SECONDS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = candidate_import.inspect_current_candidate_import(
            claim_verification_path=args.claim_verification,
            trust_registry_path=args.trust_registry,
            candidate_dir=args.candidate_dir,
            intake_receipt_path=args.intake_receipt,
            intake_verification_path=args.intake_verification,
            import_dir=args.import_dir,
            source_discovery_dir=args.source_discovery_dir,
            max_age_seconds=args.max_age_seconds,
        )
        result = (
            materialize_candidate_artifact_request(
                report,
                receipt_path=args.receipt,
                max_age_seconds=args.max_age_seconds,
            )
            if args.materialize_current
            else inspect_candidate_artifact_request(
                report,
                receipt_path=args.receipt,
                max_age_seconds=args.max_age_seconds,
            )
        )
    except Exception:
        result = _blocked("trust_candidate_artifact_request_not_current")
    sys.stdout.buffer.write(_canonical(result) + b"\n")
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
