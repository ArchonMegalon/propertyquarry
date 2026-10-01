#!/usr/bin/env python3
"""Stage a digest-bound trust enrollment execution request without executing it."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_source_refresh_claims as claims
from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_authorization as authorization
from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as preview
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_enrollment_execution_readiness.v1"
)
VERIFY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_enrollment_execution_readiness_verification.v1"
)
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-enrollment-execution-readiness.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-enrollment-execution-readiness-verification.json"
)
DEFAULT_MAX_AGE_SECONDS = preview.DEFAULT_MAX_AGE_SECONDS
_READINESS_ID = re.compile(r"pqtrustready_[0-9a-f]{24}\Z")


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return preview._canonical(dict(value))


def _sha256(value: bytes) -> str:
    return preview._sha256(value)


def _timestamp(value: object) -> datetime | None:
    return preview._timestamp(value)


def _with_integrity(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(payload)),
    }
    return payload


def _integrity_verified(value: Mapping[str, Any]) -> bool:
    payload = dict(value)
    integrity = payload.pop("integrity", None)
    return bool(
        isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and trust_intake._SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(payload))
    )


def _safety_fields() -> dict[str, bool]:
    return {
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "explicit_authorization_recorded": False,
        "exact_preview_authorized": False,
        "trust_enrollment_authorized": False,
        "execution_request_staged": False,
        "execution_readiness_verified": False,
        "governed_execution_available": False,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
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
        "readiness_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair current preview, authorization, and registry bindings; "
            "do not modify the trust registry"
        ),
        "readiness_id": "",
        "preview_id": "",
        "authorization_id": "",
        "authorization_decision": "",
        "current_trust_registry_sha256": "",
        "proposed_trust_registry_sha256": "",
        "progress": {
            "current_evidence_verified": False,
            "preview_binding_verified": False,
            "authorization_binding_verified": False,
            "registry_binding_verified": False,
            "readiness_receipt_persisted": False,
            "verification_receipt_persisted": False,
        },
        **_safety_fields(),
    }


def _source_state(
    preview_report: Mapping[str, Any],
    authorization_report: Mapping[str, Any],
) -> tuple[str, str, bool, bool]:
    if preview_report.get("status") != "verified":
        raise ValueError("execution_readiness_preview_unverified")
    preview_state = str(preview_report.get("preview_state") or "")
    authorization_status = str(authorization_report.get("status") or "")
    authorization_state = str(
        authorization_report.get("authorization_state") or ""
    )
    if preview_state in {"not_required", "awaiting_decision"}:
        if not (
            authorization_status == "not_required"
            and authorization_state == "not_required"
            and authorization_report.get("exact_preview_authorized") is False
            and authorization_report.get("trust_enrollment_authorized") is False
            and authorization_report.get("trust_registry_modified") is False
        ):
            raise ValueError("execution_readiness_authorization_not_admissible")
        return "not_required", "", False, False
    if not authorization._preview_is_admissible(preview_report):
        raise ValueError("execution_readiness_preview_not_admissible")
    if authorization_status == "pending":
        if not (
            authorization_state == "exact_preview_authorization_pending"
            and authorization_report.get("preview_id")
            == preview_report.get("preview_id")
            and authorization_report.get("preview_verification_sha256")
            == preview_report.get("verification_receipt_sha256")
            and authorization_report.get("current_trust_registry_sha256")
            == preview_report.get("current_trust_registry_sha256")
            and authorization_report.get("proposed_trust_registry_sha256")
            == preview_report.get("proposed_trust_registry_sha256")
            and authorization_report.get("action_required") is True
            and authorization_report.get("exact_preview_authorized") is False
            and authorization_report.get("trust_enrollment_authorized") is False
            and authorization_report.get("trust_registry_modified") is False
        ):
            raise ValueError("execution_readiness_authorization_not_admissible")
        return "awaiting_exact_preview_authorization", "", False, False
    decision = str(authorization_report.get("decision") or "")
    expected_state = {
        "authorize_exact_preview": "exact_preview_authorized",
        "reject": "exact_preview_rejected",
        "defer": "deferred",
    }.get(decision)
    recorded = bool(
        authorization_status == "verified"
        and expected_state
        and authorization_state == expected_state
        and authorization._AUTHORIZATION_ID.fullmatch(
            str(authorization_report.get("authorization_id") or "")
        )
        and trust_intake._SHA256.fullmatch(
            str(
                authorization_report.get("authorization_receipt_sha256")
                or ""
            )
        )
        and authorization_report.get("explicit_authorization_recorded") is True
        and authorization_report.get("trust_registry_modified") is False
        and authorization_report.get("protected_operation_executed") is False
    )
    approved = decision == "authorize_exact_preview"
    if not (
        recorded
        and authorization_report.get("preview_id")
        == preview_report.get("preview_id")
        and authorization_report.get("preview_verification_sha256")
        == preview_report.get("verification_receipt_sha256")
        and authorization_report.get("current_trust_registry_sha256")
        == preview_report.get("current_trust_registry_sha256")
        and authorization_report.get("proposed_trust_registry_sha256")
        == preview_report.get("proposed_trust_registry_sha256")
        and authorization_report.get("exact_preview_authorized") is approved
        and authorization_report.get("trust_enrollment_authorized") is approved
        and authorization_report.get("automatic_execution_allowed") is False
        and authorization_report.get("execution_authorized") is False
    ):
        raise ValueError("execution_readiness_authorization_not_admissible")
    return {
        "authorize_exact_preview": "ready_for_governed_execution",
        "reject": "authorization_rejected",
        "defer": "deferred",
    }[decision], decision, True, approved


def build_execution_readiness(
    preview_report: Mapping[str, Any],
    authorization_report: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    generated_at = _now(now)
    readiness_state, decision, recorded, approved = _source_state(
        preview_report,
        authorization_report,
    )
    source_expiry = _timestamp(
        authorization_report.get("expires_at")
        or preview_report.get("expires_at")
    )
    expires_at = min(
        source_expiry
        or generated_at + timedelta(seconds=max(60.0, max_age_seconds)),
        generated_at + timedelta(seconds=max(60.0, max_age_seconds)),
    )
    if expires_at < generated_at:
        raise ValueError("execution_readiness_source_not_fresh")
    binding = {
        "preview_id": str(preview_report.get("preview_id") or ""),
        "preview_receipt_sha256": str(
            preview_report.get("preview_receipt_sha256") or ""
        ),
        "preview_verification_sha256": str(
            preview_report.get("verification_receipt_sha256") or ""
        ),
        "authorization_id": str(
            authorization_report.get("authorization_id") or ""
        ),
        "authorization_receipt_sha256": str(
            authorization_report.get("authorization_receipt_sha256") or ""
        ),
        "authorization_decision": decision,
        "current_trust_registry_sha256": str(
            preview_report.get("current_trust_registry_sha256") or ""
        ),
        "proposed_trust_registry_sha256": str(
            preview_report.get("proposed_trust_registry_sha256") or ""
        ),
    }
    semantic = {
        "readiness_state": readiness_state,
        "binding": binding,
    }
    safety = _safety_fields()
    safety.update(
        {
            "action_required": approved,
            "operator_review_required": approved,
            "explicit_authorization_recorded": recorded,
            "exact_preview_authorized": approved,
            "trust_enrollment_authorized": approved,
            "execution_request_staged": approved,
            "execution_readiness_verified": approved,
            "governed_execution_available": approved,
        }
    )
    next_action = {
        "not_required": str(preview_report.get("next_action") or ""),
        "awaiting_exact_preview_authorization": (
            "record one exact authorization, rejection, or deferral; no "
            "execution request has been staged"
        ),
        "authorization_rejected": (
            "leave producer trust unchanged; a new candidate requires a new preview"
        ),
        "deferred": (
            "leave producer trust unchanged and submit fresh evidence when ready"
        ),
        "ready_for_governed_execution": (
            "invoke the separately governed one-shot executor with every exact "
            "binding and operator evidence, or defer without modifying trust"
        ),
    }[readiness_state]
    return _with_integrity(
        {
            "schema": SCHEMA,
            "readiness_id": (
                f"pqtrustready_{_sha256(_canonical(semantic))[:24]}"
            ),
            "generated_at": generated_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "status": readiness_state,
            "blocking_reason": "",
            "next_action": next_action,
            "binding": binding,
            **safety,
        }
    )


def verify_execution_readiness(
    receipt: Mapping[str, Any],
    *,
    preview_report: Mapping[str, Any],
    authorization_report: Mapping[str, Any],
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    generated_at = _timestamp(receipt.get("generated_at"))
    expires_at = _timestamp(receipt.get("expires_at"))
    if not (
        _integrity_verified(receipt)
        and generated_at is not None
        and expires_at is not None
        and generated_at <= observed_now <= expires_at
    ):
        return _blocked("execution_readiness_integrity_or_freshness_invalid", now=observed_now)
    try:
        expected = build_execution_readiness(
            preview_report,
            authorization_report,
            now=generated_at,
            max_age_seconds=max_age_seconds,
        )
    except (TypeError, ValueError):
        return _blocked("execution_readiness_source_not_admissible", now=observed_now)
    state = str(receipt.get("status") or "")
    approved = state == "ready_for_governed_execution"
    recorded = state in {
        "ready_for_governed_execution",
        "authorization_rejected",
        "deferred",
    }
    if not (
        dict(receipt) == expected
        and _READINESS_ID.fullmatch(str(receipt.get("readiness_id") or ""))
        and receipt.get("explicit_authorization_recorded") is recorded
        and receipt.get("exact_preview_authorized") is approved
        and receipt.get("trust_enrollment_authorized") is approved
        and receipt.get("execution_request_staged") is approved
        and receipt.get("execution_readiness_verified") is approved
        and receipt.get("action_required") is approved
        and receipt.get("interrupt_operator") is False
        and receipt.get("operator_review_required") is approved
        and receipt.get("governed_execution_available") is approved
        and receipt.get("trust_registry_modified") is False
        and receipt.get("automatic_execution_allowed") is False
        and receipt.get("execution_authorized") is False
        and receipt.get("protected_operation_executed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("delivery_authorized") is False
    ):
        return _blocked("execution_readiness_contract_not_admissible", now=observed_now)
    binding = dict(receipt.get("binding") or {})
    return _with_integrity(
        {
            "schema": VERIFY_SCHEMA,
            "status": "verified",
            "readiness_state": state,
            "readiness_id": str(receipt.get("readiness_id") or ""),
            "updated_at": observed_now.isoformat(),
            "generated_at": str(receipt.get("generated_at") or ""),
            "expires_at": str(receipt.get("expires_at") or ""),
            "blocking_reason": "",
            "next_action": str(receipt.get("next_action") or ""),
            "preview_id": str(binding.get("preview_id") or ""),
            "preview_receipt_sha256": str(
                binding.get("preview_receipt_sha256") or ""
            ),
            "preview_verification_sha256": str(
                binding.get("preview_verification_sha256") or ""
            ),
            "authorization_id": str(binding.get("authorization_id") or ""),
            "authorization_receipt_sha256": str(
                binding.get("authorization_receipt_sha256") or ""
            ),
            "authorization_decision": str(
                binding.get("authorization_decision") or ""
            ),
            "current_trust_registry_sha256": str(
                binding.get("current_trust_registry_sha256") or ""
            ),
            "proposed_trust_registry_sha256": str(
                binding.get("proposed_trust_registry_sha256") or ""
            ),
            "progress": {
                "current_evidence_verified": True,
                "preview_binding_verified": True,
                "authorization_binding_verified": True,
                "registry_binding_verified": True,
            },
            **_safety_fields(),
            "action_required": approved,
            "operator_review_required": approved,
            "explicit_authorization_recorded": recorded,
            "exact_preview_authorized": approved,
            "trust_enrollment_authorized": approved,
            "execution_request_staged": approved,
            "execution_readiness_verified": approved,
            "governed_execution_available": approved,
        }
    )


def _persist_verification(
    verification: Mapping[str, Any],
    *,
    receipt_path: Path,
    verification_path: Path,
) -> dict[str, Any]:
    receipt_target = Path(receipt_path).absolute()
    verification_target = Path(verification_path).absolute()
    receipt, receipt_raw, receipt_digest = trust_intake._private_snapshot(
        receipt_target,
        field="source_refresh_trust_enrollment_execution_readiness",
    )
    if not _integrity_verified(receipt):
        raise ValueError("execution_readiness_persistence_mismatch")
    payload = _with_integrity(
        {
            key: value
            for key, value in verification.items()
            if key != "integrity"
        }
        | {
            "readiness_receipt_sha256": receipt_digest,
            "progress": {
                **dict(verification.get("progress") or {}),
                "readiness_receipt_persisted": True,
                "verification_receipt_persisted": True,
            },
        }
    )
    atomic_write_bytes(verification_target, _canonical(payload), overwrite=True)
    persisted, verification_raw, verification_digest = trust_intake._private_snapshot(
        verification_target,
        field="source_refresh_trust_enrollment_execution_readiness_verification",
    )
    if persisted != payload or not _integrity_verified(persisted):
        raise ValueError("execution_readiness_verification_persistence_mismatch")
    result = dict(payload)
    result.pop("integrity", None)
    result.update(
        {
            "receipt_path": str(receipt_target),
            "verification_path": str(verification_target),
            "readiness_receipt_sha256": receipt_digest,
            "readiness_receipt_bytes": len(receipt_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "readiness_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def materialize_execution_readiness(
    preview_report: Mapping[str, Any],
    authorization_report: Mapping[str, Any],
    *,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        receipt = build_execution_readiness(
            preview_report,
            authorization_report,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        atomic_write_bytes(
            Path(receipt_path).absolute(),
            _canonical(receipt),
            overwrite=True,
        )
        persisted, _raw, _digest = trust_intake._private_snapshot(
            receipt_path,
            field="source_refresh_trust_enrollment_execution_readiness",
        )
        if persisted != receipt:
            raise ValueError("execution_readiness_persistence_mismatch")
        verification = verify_execution_readiness(
            persisted,
            preview_report=preview_report,
            authorization_report=authorization_report,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        if verification.get("status") != "verified":
            raise ValueError("execution_readiness_verification_failed")
        return _persist_verification(
            verification,
            receipt_path=receipt_path,
            verification_path=verification_path,
        )
    except Exception:
        return _blocked(
            "execution_readiness_evidence_or_persistence_not_admissible",
            now=observed_now,
        )


def inspect_current_execution_readiness(
    *,
    authorization_dir: Path = authorization.DEFAULT_DECISION_DIR,
    preview_decision_dir: Path = trust_decision.DEFAULT_DECISION_DIR,
    claim_verification_path: Path = claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = claims.DEFAULT_TRUST_REGISTRY_PATH,
    candidate_dir: Path = trust_intake.DEFAULT_CANDIDATE_DIR,
    intake_receipt_path: Path = trust_intake.DEFAULT_RECEIPT_PATH,
    intake_verification_path: Path = trust_intake.DEFAULT_VERIFICATION_PATH,
    preview_receipt_path: Path = preview.DEFAULT_RECEIPT_PATH,
    preview_verification_path: Path = preview.DEFAULT_VERIFICATION_PATH,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        preview_report = preview.inspect_current_enrollment_preview(
            decision_dir=preview_decision_dir,
            claim_verification_path=claim_verification_path,
            trust_registry_path=trust_registry_path,
            candidate_dir=candidate_dir,
            intake_receipt_path=intake_receipt_path,
            intake_verification_path=intake_verification_path,
            receipt_path=preview_receipt_path,
            verification_path=preview_verification_path,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        authorization_report = authorization.verify_authorization_for_preview(
            preview_report,
            decision_dir=authorization_dir,
            now=observed_now,
        )
        receipt, _receipt_raw, receipt_digest = trust_intake._private_snapshot(
            receipt_path,
            field="source_refresh_trust_enrollment_execution_readiness",
        )
        persisted, _verification_raw, _verification_digest = trust_intake._private_snapshot(
            verification_path,
            field="source_refresh_trust_enrollment_execution_readiness_verification",
        )
        current = verify_execution_readiness(
            receipt,
            preview_report=preview_report,
            authorization_report=authorization_report,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        expected_progress = {
            **dict(current.get("progress") or {}),
            "readiness_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
        current_without_variable = {
            key: value
            for key, value in current.items()
            if key not in {"integrity", "progress", "updated_at"}
        }
        persisted_without_variable = {
            key: value
            for key, value in persisted.items()
            if key not in {
                "integrity",
                "progress",
                "updated_at",
                "readiness_receipt_sha256",
            }
        }
        if not (
            current.get("status") == "verified"
            and persisted.get("schema") == VERIFY_SCHEMA
            and persisted.get("status") == "verified"
            and _integrity_verified(persisted)
            and persisted.get("readiness_receipt_sha256") == receipt_digest
            and persisted_without_variable == current_without_variable
            and dict(persisted.get("progress") or {}) == expected_progress
        ):
            raise ValueError("execution_readiness_bundle_not_current")
    except Exception:
        return _blocked("execution_readiness_bundle_not_admissible", now=observed_now)
    result = dict(persisted)
    result.pop("integrity", None)
    result.update(
        {
            "receipt_path": str(Path(receipt_path).absolute()),
            "verification_path": str(Path(verification_path).absolute()),
            "readiness_receipt_sha256": receipt_digest,
            "readiness_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect digest-bound trust enrollment execution readiness. This "
            "command never modifies the trust registry or executes enrollment."
        )
    )
    parser.add_argument("--authorization-dir", type=Path, default=authorization.DEFAULT_DECISION_DIR)
    parser.add_argument("--preview-decision-dir", type=Path, default=trust_decision.DEFAULT_DECISION_DIR)
    parser.add_argument("--claim-verification", type=Path, default=claims.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--trust-registry", type=Path, default=claims.DEFAULT_TRUST_REGISTRY_PATH)
    parser.add_argument("--candidate-dir", type=Path, default=trust_intake.DEFAULT_CANDIDATE_DIR)
    parser.add_argument("--intake-receipt", type=Path, default=trust_intake.DEFAULT_RECEIPT_PATH)
    parser.add_argument("--intake-verification", type=Path, default=trust_intake.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--preview-receipt", type=Path, default=preview.DEFAULT_RECEIPT_PATH)
    parser.add_argument("--preview-verification", type=Path, default=preview.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--verification", type=Path, default=DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--max-age-seconds", type=float, default=DEFAULT_MAX_AGE_SECONDS)
    parser.add_argument("--materialize", action="store_true")
    args = parser.parse_args(argv)
    if args.materialize:
        preview_report = preview.inspect_current_enrollment_preview(
            decision_dir=args.preview_decision_dir,
            claim_verification_path=args.claim_verification,
            trust_registry_path=args.trust_registry,
            candidate_dir=args.candidate_dir,
            intake_receipt_path=args.intake_receipt,
            intake_verification_path=args.intake_verification,
            receipt_path=args.preview_receipt,
            verification_path=args.preview_verification,
            max_age_seconds=args.max_age_seconds,
        )
        authorization_report = authorization.verify_authorization_for_preview(
            preview_report,
            decision_dir=args.authorization_dir,
        )
        result = materialize_execution_readiness(
            preview_report,
            authorization_report,
            receipt_path=args.receipt,
            verification_path=args.verification,
            max_age_seconds=args.max_age_seconds,
        )
    else:
        result = inspect_current_execution_readiness(
            authorization_dir=args.authorization_dir,
            preview_decision_dir=args.preview_decision_dir,
            claim_verification_path=args.claim_verification,
            trust_registry_path=args.trust_registry,
            candidate_dir=args.candidate_dir,
            intake_receipt_path=args.intake_receipt,
            intake_verification_path=args.intake_verification,
            preview_receipt_path=args.preview_receipt,
            preview_verification_path=args.preview_verification,
            receipt_path=args.receipt,
            verification_path=args.verification,
            max_age_seconds=args.max_age_seconds,
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
