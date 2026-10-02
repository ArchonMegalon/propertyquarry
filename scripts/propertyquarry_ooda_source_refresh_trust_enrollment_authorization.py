#!/usr/bin/env python3
"""Record exact enrollment-preview consent without modifying producer trust."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_source_refresh_claims as claims
from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as preview
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts.propertyquarry_secure_file_io import OutputExistsError, atomic_write_bytes


SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_enrollment_authorization.v1"
)
VERIFY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_enrollment_authorization_verification.v1"
)
DEFAULT_DECISION_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-enrollment-authorizations"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-enrollment-authorization-verification.json"
)
DECISIONS = ("authorize_exact_preview", "reject", "defer")
AUTHORIZATION_METHODS = (
    "authenticated_operator_session",
    "contract_record",
    "in_person",
    "trusted_channel",
)
_PREVIEW_ID = re.compile(r"pqtrustpreview_[0-9a-f]{24}\Z")
_AUTHORIZATION_ID = re.compile(r"pqtrustauth_[0-9a-f]{24}\Z")


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return preview._canonical(dict(value))


def _sha256(value: bytes) -> str:
    return preview._sha256(value)


def _timestamp(value: object) -> datetime | None:
    return preview._timestamp(value)


def _safety_fields() -> dict[str, bool]:
    return {
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "explicit_authorization_recorded": False,
        "exact_preview_authorized": False,
        "trust_enrollment_authorized": False,
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
        "authorization_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair the exact preview and immutable authorization evidence; "
            "do not modify the trust registry or enroll trust"
        ),
        "preview_id": "",
        "authorization_id": "",
        "decision": "",
        "current_trust_registry_sha256": "",
        "proposed_trust_registry_sha256": "",
        "progress": {
            "current_evidence_verified": False,
            "preview_binding_verified": False,
            "authorization_decision_recorded": False,
        },
        **_safety_fields(),
    }


def _preview_is_admissible(report: Mapping[str, Any]) -> bool:
    progress = report.get("progress")
    authorization_scope = report.get("authorization_scope")
    return bool(
        report.get("schema") == preview.VERIFY_SCHEMA
        and report.get("status") == "verified"
        and report.get("preview_state") == "preview_staged"
        and _PREVIEW_ID.fullmatch(str(report.get("preview_id") or ""))
        and trust_intake._SHA256.fullmatch(
            str(report.get("preview_receipt_sha256") or "")
        )
        and trust_intake._SHA256.fullmatch(
            str(report.get("verification_receipt_sha256") or "")
        )
        and trust_intake._SHA256.fullmatch(
            str(report.get("current_trust_registry_sha256") or "")
        )
        and trust_intake._SHA256.fullmatch(
            str(report.get("proposed_trust_registry_sha256") or "")
        )
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
        and progress.get("intake_binding_verified") is True
        and progress.get("decision_binding_verified") is True
        and progress.get("trust_registry_binding_verified") is True
        and progress.get("preview_integrity_verified") is True
        and report.get("preview_receipt_persisted") is True
        and report.get("verification_receipt_persisted") is True
        and report.get("action_required") is True
        and report.get("operator_review_required") is True
        and report.get("preview_staged") is True
        and report.get("identity_verification_asserted") is True
        and report.get("trust_enrollment_preview_authorized") is True
        and report.get("trust_enrollment_authorized") is False
        and report.get("trust_registry_modified") is False
        and report.get("decision_confers_trust") is False
        and report.get("private_key_material_requested") is False
        and report.get("private_key_material_recorded") is False
        and report.get("automatic_execution_allowed") is False
        and report.get("protected_operation_executed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("delivery_authorized") is False
        and isinstance(authorization_scope, Mapping)
        and authorization_scope.get("operation")
        == "replace_trust_registry_with_exact_preview"
        and authorization_scope.get("preview_id") == report.get("preview_id")
        and authorization_scope.get(
            "expected_current_trust_registry_sha256"
        )
        == report.get("current_trust_registry_sha256")
        and authorization_scope.get("proposed_trust_registry_sha256")
        == report.get("proposed_trust_registry_sha256")
        and authorization_scope.get("decision_options") == list(DECISIONS)
        and authorization_scope.get("authorization_recorded") is False
    )


def _decision_path(
    decision_dir: Path,
    *,
    preview_id: str,
    preview_verification_sha256: str,
) -> Path:
    if not (
        _PREVIEW_ID.fullmatch(preview_id)
        and trust_intake._SHA256.fullmatch(preview_verification_sha256)
    ):
        raise ValueError("trust_enrollment_authorization_binding_invalid")
    return (
        Path(decision_dir).absolute()
        / f"{preview_id}.{preview_verification_sha256}.json"
    )


def _not_required(
    preview_report: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "not_required",
        "authorization_state": "not_required",
        "updated_at": now.isoformat(),
        "blocking_reason": "",
        "next_action": str(preview_report.get("next_action") or ""),
        "preview_id": "",
        "authorization_id": "",
        "decision": "",
        "current_trust_registry_sha256": str(
            preview_report.get("current_trust_registry_sha256") or ""
        ),
        "proposed_trust_registry_sha256": "",
        "progress": {
            "current_evidence_verified": True,
            "preview_binding_verified": True,
            "authorization_decision_recorded": False,
        },
        **_safety_fields(),
    }


def _pending(
    preview_report: Mapping[str, Any],
    *,
    decision_path: Path,
    now: datetime,
) -> dict[str, Any]:
    safety = _safety_fields()
    safety.update(
        {
            "action_required": True,
            "operator_review_required": True,
        }
    )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "pending",
        "authorization_state": "exact_preview_authorization_pending",
        "updated_at": now.isoformat(),
        "blocking_reason": "explicit_exact_preview_authorization_required",
        "next_action": str(preview_report.get("next_action") or ""),
        "preview_id": str(preview_report.get("preview_id") or ""),
        "preview_verification_sha256": str(
            preview_report.get("verification_receipt_sha256") or ""
        ),
        "authorization_id": "",
        "decision": "",
        "decision_options": list(DECISIONS),
        "decision_path": str(decision_path),
        "current_trust_registry_sha256": str(
            preview_report.get("current_trust_registry_sha256") or ""
        ),
        "proposed_trust_registry_sha256": str(
            preview_report.get("proposed_trust_registry_sha256") or ""
        ),
        "progress": {
            "current_evidence_verified": True,
            "preview_binding_verified": True,
            "authorization_decision_recorded": False,
        },
        **safety,
    }


def build_enrollment_authorization(
    preview_report: Mapping[str, Any],
    *,
    decision: str,
    authorizer_id: str,
    expected_current_trust_registry_sha256: str,
    expected_proposed_trust_registry_sha256: str,
    authorization_method: str = "",
    authorization_evidence_ref: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    recorded_at = _now(now)
    normalized_decision = str(decision or "").strip()
    normalized_authorizer = str(authorizer_id or "").strip()
    normalized_method = str(authorization_method or "").strip()
    normalized_evidence_ref = str(authorization_evidence_ref or "").strip()
    approve = normalized_decision == "authorize_exact_preview"
    if not (
        _preview_is_admissible(preview_report)
        and normalized_decision in DECISIONS
        and trust_decision._DECIDER_ID.fullmatch(normalized_authorizer)
        and expected_current_trust_registry_sha256
        == preview_report.get("current_trust_registry_sha256")
        and expected_proposed_trust_registry_sha256
        == preview_report.get("proposed_trust_registry_sha256")
        and (
            approve
            and normalized_method in AUTHORIZATION_METHODS
            and trust_decision._EVIDENCE_REF.fullmatch(
                normalized_evidence_ref
            )
            or not approve
            and not normalized_method
            and not normalized_evidence_ref
        )
    ):
        raise ValueError("trust_enrollment_authorization_input_not_admissible")
    expires_at = _timestamp(preview_report.get("expires_at"))
    if expires_at is None or expires_at < recorded_at:
        raise ValueError("trust_enrollment_authorization_preview_not_fresh")
    binding = {
        "preview_id": str(preview_report.get("preview_id") or ""),
        "preview_receipt_sha256": str(
            preview_report.get("preview_receipt_sha256") or ""
        ),
        "preview_verification_sha256": str(
            preview_report.get("verification_receipt_sha256") or ""
        ),
        "current_trust_registry_sha256": str(
            preview_report.get("current_trust_registry_sha256") or ""
        ),
        "proposed_trust_registry_sha256": str(
            preview_report.get("proposed_trust_registry_sha256") or ""
        ),
    }
    decision_row = {
        "value": normalized_decision,
        "authorizer_id": normalized_authorizer,
        "explicit_input_recorded": True,
        "source": "local_operator_cli",
        "operator_identity_cryptographically_verified": False,
        "authorization_method": normalized_method,
        "authorization_evidence_ref": normalized_evidence_ref,
        "acknowledged_current_trust_registry_sha256": (
            expected_current_trust_registry_sha256
        ),
        "acknowledged_proposed_trust_registry_sha256": (
            expected_proposed_trust_registry_sha256
        ),
    }
    semantic = {
        "binding": binding,
        "decision": decision_row,
        "recorded_at": recorded_at.isoformat(),
    }
    digest = _sha256(_canonical(semantic))
    authorization_state = {
        "authorize_exact_preview": "exact_preview_authorized",
        "reject": "exact_preview_rejected",
        "defer": "deferred",
    }[normalized_decision]
    next_action = {
        "authorize_exact_preview": (
            "authorization is recorded only for this exact preview; a separate "
            "governed execution must reverify every digest before any registry write"
        ),
        "reject": (
            "leave producer trust unchanged and stage a new verified candidate if needed"
        ),
        "defer": (
            "leave producer trust unchanged and submit a fresh preview when ready"
        ),
    }[normalized_decision]
    safety = _safety_fields()
    safety.update(
        {
            "explicit_authorization_recorded": True,
            "exact_preview_authorized": approve,
            "trust_enrollment_authorized": approve,
        }
    )
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "authorization_id": f"pqtrustauth_{digest[:24]}",
        "recorded_at": recorded_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "status": authorization_state,
        "next_action": next_action,
        "binding": binding,
        "decision": decision_row,
        **safety,
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(receipt)),
    }
    return receipt


def verify_enrollment_authorization(
    receipt: Mapping[str, Any],
    *,
    preview_report: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    normalized = dict(receipt)
    integrity = normalized.pop("integrity", None)
    if not (
        isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and trust_intake._SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    ):
        return _blocked(
            "trust_enrollment_authorization_integrity_invalid",
            now=observed_now,
        )
    recorded_at = _timestamp(receipt.get("recorded_at"))
    expires_at = _timestamp(receipt.get("expires_at"))
    decision_row = receipt.get("decision")
    if not (
        recorded_at is not None
        and expires_at is not None
        and recorded_at <= observed_now <= expires_at
        and isinstance(decision_row, Mapping)
    ):
        return _blocked(
            "trust_enrollment_authorization_not_fresh",
            now=observed_now,
        )
    try:
        expected = build_enrollment_authorization(
            preview_report,
            decision=str(decision_row.get("value") or ""),
            authorizer_id=str(decision_row.get("authorizer_id") or ""),
            expected_current_trust_registry_sha256=str(
                decision_row.get(
                    "acknowledged_current_trust_registry_sha256"
                )
                or ""
            ),
            expected_proposed_trust_registry_sha256=str(
                decision_row.get(
                    "acknowledged_proposed_trust_registry_sha256"
                )
                or ""
            ),
            authorization_method=str(
                decision_row.get("authorization_method") or ""
            ),
            authorization_evidence_ref=str(
                decision_row.get("authorization_evidence_ref") or ""
            ),
            now=recorded_at,
        )
    except (TypeError, ValueError):
        return _blocked(
            "trust_enrollment_authorization_preview_not_admissible",
            now=observed_now,
        )
    approve = decision_row.get("value") == "authorize_exact_preview"
    if not (
        dict(receipt) == expected
        and _AUTHORIZATION_ID.fullmatch(
            str(receipt.get("authorization_id") or "")
        )
        and receipt.get("explicit_authorization_recorded") is True
        and receipt.get("exact_preview_authorized") is approve
        and receipt.get("trust_enrollment_authorized") is approve
        and receipt.get("trust_registry_modified") is False
        and receipt.get("decision_confers_trust") is False
        and receipt.get("private_key_material_requested") is False
        and receipt.get("private_key_material_recorded") is False
        and receipt.get("automatic_execution_allowed") is False
        and receipt.get("execution_authorized") is False
        and receipt.get("deployment_or_restart_authorized") is False
        and receipt.get("protected_operation_executed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("delivery_authorized") is False
        and receipt.get("delivery_attempted") is False
        and receipt.get("sent") is False
    ):
        return _blocked(
            "trust_enrollment_authorization_contract_not_admissible",
            now=observed_now,
        )
    binding = dict(receipt.get("binding") or {})
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "authorization_state": str(receipt.get("status") or ""),
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": str(receipt.get("next_action") or ""),
        "preview_id": str(binding.get("preview_id") or ""),
        "preview_verification_sha256": str(
            binding.get("preview_verification_sha256") or ""
        ),
        "authorization_id": str(receipt.get("authorization_id") or ""),
        "decision": str(decision_row.get("value") or ""),
        "authorizer_id": str(decision_row.get("authorizer_id") or ""),
        "authorization_method": str(
            decision_row.get("authorization_method") or ""
        ),
        "authorization_evidence_ref": str(
            decision_row.get("authorization_evidence_ref") or ""
        ),
        "recorded_at": str(receipt.get("recorded_at") or ""),
        "expires_at": str(receipt.get("expires_at") or ""),
        "current_trust_registry_sha256": str(
            binding.get("current_trust_registry_sha256") or ""
        ),
        "proposed_trust_registry_sha256": str(
            binding.get("proposed_trust_registry_sha256") or ""
        ),
        "progress": {
            "current_evidence_verified": True,
            "preview_binding_verified": True,
            "authorization_decision_recorded": True,
        },
        **_safety_fields(),
        "explicit_authorization_recorded": True,
        "exact_preview_authorized": approve,
        "trust_enrollment_authorized": approve,
    }


def verify_authorization_for_preview(
    preview_report: Mapping[str, Any],
    *,
    decision_dir: Path = DEFAULT_DECISION_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    if preview_report.get("status") != "verified":
        return _blocked(
            "trust_enrollment_authorization_preview_unverified",
            now=observed_now,
        )
    if preview_report.get("preview_state") != "preview_staged":
        if not (
            preview_report.get("preview_state")
            in {"not_required", "awaiting_decision"}
            and preview_report.get("action_required") is False
            and preview_report.get("trust_enrollment_authorized") is False
            and preview_report.get("trust_registry_modified") is False
        ):
            return _blocked(
                "trust_enrollment_authorization_preview_not_admissible",
                now=observed_now,
            )
        return _not_required(preview_report, now=observed_now)
    if not _preview_is_admissible(preview_report):
        return _blocked(
            "trust_enrollment_authorization_preview_not_admissible",
            now=observed_now,
        )
    try:
        path = _decision_path(
            decision_dir,
            preview_id=str(preview_report.get("preview_id") or ""),
            preview_verification_sha256=str(
                preview_report.get("verification_receipt_sha256") or ""
            ),
        )
        try:
            receipt, _raw, receipt_sha256 = trust_intake._private_snapshot(
                path,
                field="source_refresh_trust_enrollment_authorization",
            )
        except FileNotFoundError:
            return _pending(
                preview_report,
                decision_path=path,
                now=observed_now,
            )
    except Exception:
        return _blocked(
            "trust_enrollment_authorization_receipt_not_admissible",
            now=observed_now,
        )
    verification = verify_enrollment_authorization(
        receipt,
        preview_report=preview_report,
        now=observed_now,
    )
    if verification.get("status") != "verified":
        return verification
    verification["decision_path"] = str(path)
    verification["authorization_receipt_sha256"] = receipt_sha256
    return verification


def materialize_enrollment_authorization(
    preview_report: Mapping[str, Any],
    *,
    decision: str,
    authorizer_id: str,
    expected_preview_id: str,
    expected_preview_verification_sha256: str,
    expected_current_trust_registry_sha256: str,
    expected_proposed_trust_registry_sha256: str,
    authorization_method: str = "",
    authorization_evidence_ref: str = "",
    decision_dir: Path = DEFAULT_DECISION_DIR,
    now: datetime | None = None,
) -> tuple[dict[str, Any], Path]:
    observed_now = _now(now)
    if not (
        _preview_is_admissible(preview_report)
        and expected_preview_id == preview_report.get("preview_id")
        and expected_preview_verification_sha256
        == preview_report.get("verification_receipt_sha256")
    ):
        raise ValueError("operator_trust_enrollment_authorization_binding_mismatch")
    receipt = build_enrollment_authorization(
        preview_report,
        decision=decision,
        authorizer_id=authorizer_id,
        expected_current_trust_registry_sha256=(
            expected_current_trust_registry_sha256
        ),
        expected_proposed_trust_registry_sha256=(
            expected_proposed_trust_registry_sha256
        ),
        authorization_method=authorization_method,
        authorization_evidence_ref=authorization_evidence_ref,
        now=observed_now,
    )
    path = _decision_path(
        decision_dir,
        preview_id=expected_preview_id,
        preview_verification_sha256=expected_preview_verification_sha256,
    )
    try:
        atomic_write_bytes(path, _canonical(receipt), overwrite=False)
    except OutputExistsError:
        existing, _raw, _digest = trust_intake._private_snapshot(
            path,
            field="source_refresh_trust_enrollment_authorization",
        )
        existing_verification = verify_enrollment_authorization(
            existing,
            preview_report=preview_report,
            now=observed_now,
        )
        if not (
            existing_verification.get("status") == "verified"
            and existing.get("decision") == receipt.get("decision")
        ):
            raise ValueError("trust_enrollment_authorization_already_recorded")
        receipt = existing
    return receipt, path


def verify_current_enrollment_authorization(
    *,
    decision_dir: Path = DEFAULT_DECISION_DIR,
    preview_decision_dir: Path = trust_decision.DEFAULT_DECISION_DIR,
    claim_verification_path: Path = claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = claims.DEFAULT_TRUST_REGISTRY_PATH,
    candidate_dir: Path = trust_intake.DEFAULT_CANDIDATE_DIR,
    intake_receipt_path: Path = trust_intake.DEFAULT_RECEIPT_PATH,
    intake_verification_path: Path = trust_intake.DEFAULT_VERIFICATION_PATH,
    preview_receipt_path: Path = preview.DEFAULT_RECEIPT_PATH,
    preview_verification_path: Path = preview.DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = preview.DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
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
    return verify_authorization_for_preview(
        preview_report,
        decision_dir=decision_dir,
        now=observed_now,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record or verify exact enrollment-preview consent. This command "
            "never modifies the trust registry or enrolls trust."
        )
    )
    parser.add_argument("--verify-current", action="store_true")
    parser.add_argument("--decision", choices=DECISIONS)
    parser.add_argument("--authorizer-id")
    parser.add_argument("--preview-id")
    parser.add_argument("--preview-verification-sha256")
    parser.add_argument("--current-trust-registry-sha256")
    parser.add_argument("--proposed-trust-registry-sha256")
    parser.add_argument(
        "--authorization-method",
        choices=AUTHORIZATION_METHODS,
        default="",
    )
    parser.add_argument("--authorization-evidence-ref", default="")
    parser.add_argument("--decision-dir", type=Path, default=DEFAULT_DECISION_DIR)
    parser.add_argument("--verification-write", type=Path, default=DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--preview-decision-dir", type=Path, default=trust_decision.DEFAULT_DECISION_DIR)
    parser.add_argument("--claim-verification", type=Path, default=claims.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--trust-registry", type=Path, default=claims.DEFAULT_TRUST_REGISTRY_PATH)
    parser.add_argument("--candidate-dir", type=Path, default=trust_intake.DEFAULT_CANDIDATE_DIR)
    parser.add_argument("--intake-receipt", type=Path, default=trust_intake.DEFAULT_RECEIPT_PATH)
    parser.add_argument("--intake-verification", type=Path, default=trust_intake.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--preview-receipt", type=Path, default=preview.DEFAULT_RECEIPT_PATH)
    parser.add_argument("--preview-verification", type=Path, default=preview.DEFAULT_VERIFICATION_PATH)
    args = parser.parse_args(argv)
    preview_report = preview.inspect_current_enrollment_preview(
        decision_dir=args.preview_decision_dir,
        claim_verification_path=args.claim_verification,
        trust_registry_path=args.trust_registry,
        candidate_dir=args.candidate_dir,
        intake_receipt_path=args.intake_receipt,
        intake_verification_path=args.intake_verification,
        receipt_path=args.preview_receipt,
        verification_path=args.preview_verification,
    )
    if args.decision:
        required = (
            args.authorizer_id,
            args.preview_id,
            args.preview_verification_sha256,
            args.current_trust_registry_sha256,
            args.proposed_trust_registry_sha256,
        )
        if not all(required):
            parser.error("decision recording requires every exact preview binding")
        materialize_enrollment_authorization(
            preview_report,
            decision=args.decision,
            authorizer_id=args.authorizer_id,
            expected_preview_id=args.preview_id,
            expected_preview_verification_sha256=(
                args.preview_verification_sha256
            ),
            expected_current_trust_registry_sha256=(
                args.current_trust_registry_sha256
            ),
            expected_proposed_trust_registry_sha256=(
                args.proposed_trust_registry_sha256
            ),
            authorization_method=args.authorization_method,
            authorization_evidence_ref=args.authorization_evidence_ref,
            decision_dir=args.decision_dir,
        )
    result = verify_authorization_for_preview(
        preview_report,
        decision_dir=args.decision_dir,
    )
    try:
        atomic_write_bytes(
            args.verification_write,
            _canonical(result),
            overwrite=True,
        )
    except Exception:
        result = _blocked(
            "trust_enrollment_authorization_verification_persistence_failed"
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") in {"not_required", "pending", "verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
