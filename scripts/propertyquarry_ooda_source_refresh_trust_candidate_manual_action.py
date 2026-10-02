#!/usr/bin/env python3
"""Stage one path-free manual action for the current trust-candidate intake."""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import (
    propertyquarry_ooda_source_refresh_trust_candidate_artifact_request as artifact_request,
)
from scripts import (
    propertyquarry_ooda_source_refresh_trust_candidate_import as candidate_import,
)
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


SCHEMA = "propertyquarry.ooda_source_refresh_trust_candidate_manual_action.v1"
VERIFY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_manual_action_verification.v1"
)
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-candidate-manual-action.json"
)
DEFAULT_MAX_AGE_SECONDS = trust_intake.DEFAULT_MAX_AGE_SECONDS
_ACTION_STATES = {
    "awaiting_external_artifact",
    "external_artifact_not_admissible",
    "ready_for_manual_import",
    "recovery_required",
}
_INACTIVE_STATES = {
    "not_required",
    "succeeded",
}
_PRESENTATION_STATES = {"novel", "reminder_due", "already_presented"}
_INADMISSIBLE_REPAIR_STEPS = {
    "candidates_not_admissible": (
        "Replace the drop contents with exactly one current, request-bound "
        "public candidate JSON using the required public-file permissions; "
        "never include a private key."
    ),
    "candidate_ambiguous": (
        "Reduce the drop to exactly one current, request-bound public "
        "candidate JSON before read-only inspection resumes."
    ),
    "directory_not_admissible": (
        "Repair the operator-managed drop ownership and read-only access "
        "permissions before adding or inspecting public candidate material."
    ),
    "too_many_entries": (
        "Reduce the drop to one admissible current public candidate JSON "
        "before read-only inspection resumes."
    ),
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
        "transport_delivery_authorized": False,
        "transport_delivery_attempted": False,
        "notification_sent": False,
        "host_path_values_recorded": False,
        "candidate_payload_recorded": False,
        "public_key_material_recorded": False,
    }


def _artifact_verification_admissible(
    verification: Mapping[str, Any],
    *,
    report: Mapping[str, Any],
    import_state: str,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    staged = import_state in artifact_request._STAGED_STATES
    updated_at = candidate_import._timestamp(verification.get("updated_at"))
    age_seconds = (
        (now - updated_at).total_seconds()
        if updated_at is not None
        else float("inf")
    )
    progress = verification.get("progress")
    expected_keys = {
        "schema",
        "status",
        "request_state",
        "updated_at",
        "expires_at",
        "request_id",
        "request_semantic_sha256",
        "artifact_request_receipt_sha256",
        "artifact_request_staged",
        "action_required",
        "interrupt_operator",
        "receipt_persisted",
        "progress",
        *artifact_request._safety_fields(),
    }
    expected_progress = {
        "current_evidence_verified": True,
        "receipt_integrity_verified": True,
        "path_free_projection_verified": True,
        "public_only_projection_verified": True,
    }
    if staged:
        try:
            public_request = artifact_request._public_request(report)
        except (TypeError, ValueError):
            return False
        expected_request_id = str(public_request.get("request_id") or "")
        expected_expires_at = str(
            public_request.get("request_expires_at") or ""
        )
        expected_semantic_sha256 = artifact_request._sha256(
            artifact_request._canonical(public_request)
        )
        expires_at = candidate_import._timestamp(
            verification.get("expires_at")
        )
    else:
        expected_request_id = ""
        expected_expires_at = ""
        expected_semantic_sha256 = ""
        expires_at = None
    return bool(
        frozenset(verification) == frozenset(expected_keys)
        and verification.get("schema") == artifact_request.VERIFY_SCHEMA
        and verification.get("status") == "verified"
        and verification.get("request_state") == import_state
        and updated_at is not None
        and -30.0 <= age_seconds <= max_age_seconds
        and verification.get("expires_at") == expected_expires_at
        and verification.get("request_id") == expected_request_id
        and verification.get("request_semantic_sha256")
        == expected_semantic_sha256
        and (not staged or (expires_at is not None and now <= expires_at))
        and verification.get("artifact_request_staged") is staged
        and verification.get("action_required") is staged
        and verification.get("interrupt_operator") is False
        and verification.get("receipt_persisted") is True
        and progress == expected_progress
        and candidate_import._SHA256.fullmatch(
            str(
                verification.get("artifact_request_receipt_sha256") or ""
            )
        )
        and all(
            verification.get(key) is False
            for key in artifact_request._safety_fields()
        )
    )


def _presentation(report: Mapping[str, Any]) -> dict[str, Any]:
    value = report.get("presentation")
    if not isinstance(value, Mapping):
        raise ValueError("trust_candidate_manual_action_presentation_missing")
    state = str(value.get("state") or "")
    digest = str(value.get("presentation_digest") or "")
    if not (
        state in _PRESENTATION_STATES
        and candidate_import._SHA256.fullmatch(digest)
        and (value.get("already_presented") is True)
        is (state == "already_presented")
        and (value.get("reminder_due") is True)
        is (state == "reminder_due")
        and (report.get("interrupt_operator") is True)
        is (state in {"novel", "reminder_due"})
    ):
        raise ValueError(
            "trust_candidate_manual_action_presentation_not_admissible"
        )
    return {
        "state": state,
        "presentation_digest": digest,
    }


def _inadmissible_repair_context(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    discovery = report.get("source_discovery")
    if not (
        report.get("import_state") == "external_artifact_not_admissible"
        and isinstance(discovery, Mapping)
        and candidate_import._source_discovery_admissible(discovery)
    ):
        raise ValueError(
            "trust_candidate_manual_action_repair_context_not_admissible"
        )
    discovery_state = str(discovery.get("discovery_state") or "")
    safe_next_step = _INADMISSIBLE_REPAIR_STEPS.get(discovery_state)
    progress = discovery.get("progress")
    if not safe_next_step or not isinstance(progress, Mapping):
        raise ValueError(
            "trust_candidate_manual_action_repair_context_not_admissible"
        )
    return {
        "discovery_state": discovery_state,
        "scanned_file_count": int(progress.get("scanned_file_count") or 0),
        "valid_candidate_count": int(
            progress.get("valid_candidate_count") or 0
        ),
        "invalid_candidate_count": int(
            progress.get("invalid_candidate_count") or 0
        ),
        "safe_next_step": safe_next_step,
    }


def _action(
    report: Mapping[str, Any],
    *,
    import_state: str,
) -> dict[str, Any]:
    if import_state in artifact_request._STAGED_STATES:
        public_request = artifact_request._public_request(report)
        repair_context = (
            _inadmissible_repair_context(report)
            if import_state == "external_artifact_not_admissible"
            else None
        )
        return {
            "kind": (
                "provide_external_public_candidate"
                if import_state == "awaiting_external_artifact"
                else "replace_inadmissible_public_candidate"
            ),
            "summary": (
                "Provide one producer-owned Ed25519 public candidate JSON "
                "for the current source-refresh request."
                if repair_context is None
                else "Repair the public candidate drop before governed "
                "read-only inspection can continue."
            ),
            "safe_next_step": (
                "Place the public-only JSON in the configured operator-managed "
                "PropertyQuarry trust-candidate drop for read-only inspection."
                if repair_context is None
                else str(repair_context["safe_next_step"])
            ),
            "required_artifact": public_request,
            **(
                {"repair_context": repair_context}
                if repair_context is not None
                else {}
            ),
            "operator_confirmation_required_for_import": True,
            "out_of_band_identity_review_required_after_import": True,
        }
    if import_state == "recovery_required":
        return {
            "kind": "inspect_failed_candidate_import",
            "summary": (
                "Inspect the immutable candidate-import claim and result "
                "receipts before deciding whether a new attempt is safe."
            ),
            "safe_next_step": (
                "Compare the displayed claim, result, request, and public-key "
                "hashes with the private import history; do not delete, "
                "overwrite, or retry automatically."
            ),
            "recovery_reference": {
                "request_id": str(report.get("request_id") or ""),
                "claim_id": str(report.get("claim_id") or ""),
                "claim_sha256": str(report.get("claim_sha256") or ""),
                "result_sha256": str(report.get("result_sha256") or ""),
                "public_key_sha256": str(
                    report.get("public_key_sha256") or ""
                ),
                "blocking_reason": str(
                    report.get("blocking_reason") or ""
                ),
                "candidate_import_attempted": (
                    report.get("candidate_import_attempted") is True
                ),
            },
            "automatic_retry_allowed": False,
            "claim_deletion_allowed": False,
            "candidate_overwrite_allowed": False,
            "operator_confirmation_required_for_import": True,
            "out_of_band_identity_review_required_after_import": True,
        }
    return {
        "kind": "inspect_and_import_verified_public_candidate",
        "summary": (
            "Inspect the discovered public candidate and invoke the exact "
            "hash-bound governed import only after operator verification."
        ),
        "safe_next_step": (
            "Use the operator-local inspect command, compare both reported "
            "hashes, then use the exact confirmation-gated import command."
        ),
        "candidate_reference": {
            "source_sha256": str(report.get("source_sha256") or ""),
            "candidate_sha256": str(report.get("candidate_sha256") or ""),
            "public_key_sha256": str(
                report.get("public_key_sha256") or ""
            ),
            "producer_id": str(report.get("producer_id") or ""),
            "key_id": str(report.get("key_id") or ""),
            "lanes": list(report.get("candidate_lanes") or []),
        },
        "operator_confirmation_required_for_import": True,
        "out_of_band_identity_review_required_after_import": True,
    }


def build_candidate_manual_action(
    report: Mapping[str, Any],
    artifact_verification: Mapping[str, Any],
    *,
    now: datetime | None = None,
    evidence_now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Build a fresh path-free action receipt without delivering or importing."""

    observed_now = _now(now)
    evidence_observed_now = (
        _now(evidence_now) if evidence_now is not None else observed_now
    )
    import_state = str(report.get("import_state") or "")
    if not (
        report.get("status") == "verified"
        and import_state in (_ACTION_STATES | _INACTIVE_STATES)
        and _artifact_verification_admissible(
            artifact_verification,
            report=report,
            import_state=import_state,
            now=evidence_observed_now,
            max_age_seconds=max_age_seconds,
        )
    ):
        raise ValueError("trust_candidate_manual_action_source_not_current")
    staged = import_state in _ACTION_STATES
    presentation = _presentation(report) if staged else {
        "state": "not_required",
        "presentation_digest": "",
    }
    action = _action(report, import_state=import_state) if staged else {}
    expires_at = ""
    if import_state in artifact_request._STAGED_STATES:
        expires_at = str(
            dict(action.get("required_artifact") or {}).get(
                "request_expires_at"
            )
            or ""
        )
        parsed_expiry = candidate_import._timestamp(expires_at)
        if parsed_expiry is None or observed_now > parsed_expiry:
            raise ValueError("trust_candidate_manual_action_expired")
        expires_at = parsed_expiry.isoformat()
    receipt = _with_integrity(
        {
            "schema": SCHEMA,
            "status": "action_required" if staged else "not_required",
            "action_state": import_state,
            "updated_at": observed_now.isoformat(),
            "expires_at": expires_at,
            "request_id": (
                str(report.get("request_id") or "") if staged else ""
            ),
            "semantic_request_sha256": (
                str(report.get("semantic_request_sha256") or "")
                if staged
                else ""
            ),
            "artifact_request_receipt_sha256": str(
                artifact_verification.get(
                    "artifact_request_receipt_sha256"
                )
                or ""
            ),
            "presentation": presentation,
            "manual_action": action,
            "action_required": staged,
            "interrupt_operator": bool(
                staged and report.get("interrupt_operator") is True
            ),
            "operator_action_receipt_staged": staged,
            "read_only_source_projection": True,
            **_safety_fields(),
        }
    )
    if _contains_absolute_path(receipt):
        raise ValueError("trust_candidate_manual_action_contains_host_path")
    return receipt


def _receipt_verified(
    value: Mapping[str, Any],
    report: Mapping[str, Any],
    artifact_verification: Mapping[str, Any],
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
        expected = build_candidate_manual_action(
            report,
            artifact_verification,
            now=updated_at,
            evidence_now=now,
            max_age_seconds=max_age_seconds,
        )
    except (TypeError, ValueError):
        return False
    expires_at = candidate_import._timestamp(value.get("expires_at"))
    return bool(
        value == expected
        and _integrity_verified(value)
        and not _contains_absolute_path(value)
        and (
            value.get("expires_at") == ""
            or (expires_at is not None and now <= expires_at)
        )
    )


def _verification(
    receipt: Mapping[str, Any],
    *,
    receipt_sha256: str,
    now: datetime,
) -> dict[str, Any]:
    staged = receipt.get("status") == "action_required"
    presentation = dict(receipt.get("presentation") or {})
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "action_state": str(receipt.get("action_state") or ""),
        "updated_at": now.isoformat(),
        "expires_at": str(receipt.get("expires_at") or ""),
        "request_id": str(receipt.get("request_id") or ""),
        "semantic_request_sha256": str(
            receipt.get("semantic_request_sha256") or ""
        ),
        "artifact_request_receipt_sha256": str(
            receipt.get("artifact_request_receipt_sha256") or ""
        ),
        "presentation_state": str(presentation.get("state") or ""),
        "presentation_digest": str(
            presentation.get("presentation_digest") or ""
        ),
        "operator_action_receipt_sha256": receipt_sha256,
        "operator_action_receipt_staged": staged,
        "action_required": staged,
        "interrupt_operator": receipt.get("interrupt_operator") is True,
        "receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "receipt_integrity_verified": True,
            "artifact_request_binding_verified": True,
            "presentation_binding_verified": True,
            "path_free_projection_verified": True,
            "public_only_projection_verified": True,
        },
        **_safety_fields(),
    }


def materialize_candidate_manual_action(
    report: Mapping[str, Any],
    artifact_verification: Mapping[str, Any],
    *,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Persist the action projection; never send, import, or mutate trust."""

    observed_now = _now(now)
    receipt = build_candidate_manual_action(
        report,
        artifact_verification,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    target = Path(receipt_path).absolute()
    atomic_write_bytes(target, _canonical(receipt), overwrite=True)
    persisted, raw, digest = candidate_import._private_object(
        target,
        field="trust_candidate_manual_action",
    )
    if not _receipt_verified(
        persisted,
        report,
        artifact_verification,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    ):
        raise ValueError("trust_candidate_manual_action_persistence_invalid")
    if raw != _canonical(receipt) or digest != _sha256(raw):
        raise ValueError("trust_candidate_manual_action_digest_mismatch")
    return _verification(
        persisted,
        receipt_sha256=digest,
        now=observed_now,
    )


def inspect_candidate_manual_action(
    report: Mapping[str, Any],
    artifact_verification: Mapping[str, Any],
    *,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Verify the persisted action against the current presentation ledger."""

    observed_now = _now(now)
    receipt, raw, digest = candidate_import._private_object(
        Path(receipt_path).absolute(),
        field="trust_candidate_manual_action",
    )
    if not _receipt_verified(
        receipt,
        report,
        artifact_verification,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    ):
        raise ValueError("trust_candidate_manual_action_not_current")
    if digest != _sha256(raw):
        raise ValueError("trust_candidate_manual_action_digest_mismatch")
    return _verification(
        receipt,
        receipt_sha256=digest,
        now=observed_now,
    )


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "action_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "operator_action_receipt_staged": False,
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
        "--presentation-state",
        type=Path,
        default=candidate_import.DEFAULT_PRESENTATION_STATE_PATH,
    )
    parser.add_argument(
        "--artifact-request-receipt",
        type=Path,
        default=artifact_request.DEFAULT_RECEIPT_PATH,
    )
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
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
        projected = candidate_import.apply_candidate_import_presentation_state(
            report,
            state_path=args.presentation_state,
        )
        artifact_verification = (
            artifact_request.inspect_candidate_artifact_request(
                report,
                receipt_path=args.artifact_request_receipt,
                max_age_seconds=args.max_age_seconds,
            )
        )
        result = (
            materialize_candidate_manual_action(
                projected,
                artifact_verification,
                receipt_path=args.receipt,
                max_age_seconds=args.max_age_seconds,
            )
            if args.materialize_current
            else inspect_candidate_manual_action(
                projected,
                artifact_verification,
                receipt_path=args.receipt,
                max_age_seconds=args.max_age_seconds,
            )
        )
    except Exception:
        result = _blocked("trust_candidate_manual_action_not_current")
    sys.stdout.buffer.write(_canonical(result) + b"\n")
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
