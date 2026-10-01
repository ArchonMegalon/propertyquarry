#!/usr/bin/env python3
"""Record an exact candidate review without enrolling producer trust."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_source_refresh_claims as claims
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts.propertyquarry_secure_file_io import OutputExistsError, atomic_write_bytes


SCHEMA = "propertyquarry.ooda_source_refresh_trust_candidate_decision.v1"
VERIFY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_decision_verification.v1"
)
DEFAULT_DECISION_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-candidate-decisions"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-candidate-decision-verification.json"
)
MAX_DECISION_BYTES = 256 * 1024
DECISIONS = (
    "confirm_identity_verified",
    "reject_candidate",
    "defer",
)
IDENTITY_VERIFICATION_METHODS = (
    "contract_record",
    "in_person",
    "trusted_channel",
    "video_call",
    "voice_call",
)
_DECISION_ID = re.compile(r"pqtrustdecision_[0-9a-f]{24}\Z")
_DECIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_EVIDENCE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return trust_intake._canonical(dict(value))


def _sha256(value: bytes) -> str:
    return trust_intake._sha256(value)


def _timestamp(value: object) -> datetime | None:
    return trust_intake._timestamp(value)


def _safety_fields() -> dict[str, bool]:
    return {
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "identity_verification_asserted": False,
        "trust_enrollment_preview_authorized": False,
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
        "review_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair the exact candidate-review evidence before recording a decision; "
            "do not enroll trust or edit the registry"
        ),
        "candidate_review_id": "",
        "decision_id": "",
        "decision": "",
        "progress": {
            "current_evidence_verified": False,
            "candidate_review_verified": False,
            "candidate_decision_recorded": False,
        },
        **_safety_fields(),
    }


def _candidate_scope(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    scope: list[dict[str, Any]] = []
    for candidate in list(report.get("candidates") or []):
        if not isinstance(candidate, Mapping):
            raise ValueError("trust_candidate_decision_candidate_not_admissible")
        scope.append(
            {
                "producer_id": str(candidate.get("producer_id") or ""),
                "key_id": str(candidate.get("key_id") or ""),
                "public_key_sha256": str(
                    candidate.get("public_key_sha256") or ""
                ),
                "lanes": list(candidate.get("lanes") or []),
                "candidate_receipt_sha256": str(
                    candidate.get("candidate_receipt_sha256") or ""
                ),
            }
        )
    return scope


def _candidate_fingerprints(report: Mapping[str, Any]) -> list[str]:
    return sorted(
        {
            str(candidate.get("public_key_sha256") or "")
            for candidate in list(report.get("candidates") or [])
            if isinstance(candidate, Mapping)
        }
    )


def _report_expires_at(report: Mapping[str, Any]) -> datetime:
    expirations = [
        _timestamp(candidate.get("expires_at"))
        for candidate in list(report.get("candidates") or [])
        if isinstance(candidate, Mapping)
    ]
    if not expirations or any(value is None for value in expirations):
        raise ValueError("trust_candidate_decision_expiry_not_admissible")
    return min(value for value in expirations if value is not None)


def _review_report_is_admissible(
    report: Mapping[str, Any],
    *,
    now: datetime,
) -> bool:
    candidates = list(report.get("candidates") or [])
    try:
        expires_at = _report_expires_at(report)
        scope = _candidate_scope(report)
    except (TypeError, ValueError):
        return False
    return bool(
        trust_intake._review_is_admissible(report)
        and trust_intake._SHA256.fullmatch(
            str(report.get("verification_receipt_sha256") or "")
        )
        and report.get("review_decision_options") == list(DECISIONS)
        and expires_at >= now
        and len(scope) == len(candidates)
        and all(
            claims._IDENTIFIER.fullmatch(row["producer_id"])
            and claims._IDENTIFIER.fullmatch(row["key_id"])
            and trust_intake._SHA256.fullmatch(row["public_key_sha256"])
            and trust_intake._SHA256.fullmatch(
                row["candidate_receipt_sha256"]
            )
            and row["lanes"] == sorted(set(row["lanes"]))
            for row in scope
        )
    )


def _decision_path(
    decision_dir: Path,
    *,
    candidate_review_id: str,
    trust_intake_verification_sha256: str,
) -> Path:
    if not (
        candidate_review_id.startswith("pqtrustreview_")
        and len(candidate_review_id) == len("pqtrustreview_") + 24
        and trust_intake._SHA256.fullmatch(
            trust_intake_verification_sha256
        )
    ):
        raise ValueError("trust_candidate_decision_binding_not_admissible")
    return (
        Path(decision_dir).absolute()
        / f"{candidate_review_id}.{trust_intake_verification_sha256}.json"
    )


def _pending(
    report: Mapping[str, Any],
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
        "review_state": "candidate_review_pending",
        "updated_at": now.isoformat(),
        "blocking_reason": "explicit_candidate_review_decision_required",
        "next_action": str(report.get("next_action") or ""),
        "candidate_review_id": str(report.get("candidate_review_id") or ""),
        "trust_intake_verification_sha256": str(
            report.get("verification_receipt_sha256") or ""
        ),
        "candidate_scope": _candidate_scope(report),
        "decision_options": list(DECISIONS),
        "decision_path": str(decision_path),
        "decision_id": "",
        "decision": "",
        "progress": {
            "current_evidence_verified": True,
            "candidate_review_verified": True,
            "candidate_decision_recorded": False,
        },
        **safety,
    }


def _not_required(*, now: datetime) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "not_required",
        "review_state": "not_required",
        "updated_at": now.isoformat(),
        "blocking_reason": "",
        "next_action": "await a verified producer public-key candidate",
        "candidate_review_id": "",
        "decision_id": "",
        "decision": "",
        "progress": {
            "current_evidence_verified": True,
            "candidate_review_verified": True,
            "candidate_decision_recorded": False,
        },
        **_safety_fields(),
    }


def build_candidate_review_decision(
    report: Mapping[str, Any],
    *,
    decision: str,
    decider_id: str,
    acknowledged_public_key_sha256s: Sequence[str] = (),
    identity_verification_method: str = "",
    identity_evidence_ref: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    recorded_at = _now(now)
    normalized_decision = str(decision or "").strip()
    normalized_decider = str(decider_id or "").strip()
    normalized_method = str(identity_verification_method or "").strip()
    normalized_evidence_ref = str(identity_evidence_ref or "").strip()
    acknowledged = sorted(
        {str(value or "").strip() for value in acknowledged_public_key_sha256s}
    )
    fingerprints = _candidate_fingerprints(report)
    confirm = normalized_decision == "confirm_identity_verified"
    if not (
        _review_report_is_admissible(report, now=recorded_at)
        and normalized_decision in DECISIONS
        and _DECIDER_ID.fullmatch(normalized_decider)
        and (
            confirm
            and acknowledged == fingerprints
            and all(
                trust_intake._SHA256.fullmatch(value) for value in acknowledged
            )
            and normalized_method in IDENTITY_VERIFICATION_METHODS
            and _EVIDENCE_REF.fullmatch(normalized_evidence_ref)
            or not confirm
            and not acknowledged
            and not normalized_method
            and not normalized_evidence_ref
        )
    ):
        raise ValueError("trust_candidate_decision_input_not_admissible")
    expires_at = _report_expires_at(report)
    scope = _candidate_scope(report)
    binding = {
        "candidate_review_id": str(report.get("candidate_review_id") or ""),
        "request_id": str(report.get("request_id") or ""),
        "semantic_request_sha256": str(
            report.get("semantic_request_sha256") or ""
        ),
        "intake_receipt_sha256": str(
            report.get("intake_receipt_sha256") or ""
        ),
        "trust_intake_verification_sha256": str(
            report.get("verification_receipt_sha256") or ""
        ),
        "candidate_scope_sha256": _sha256(_canonical({"candidates": scope})),
    }
    decision_row = {
        "value": normalized_decision,
        "decider_id": normalized_decider,
        "explicit_input_recorded": True,
        "source": "local_operator_cli",
        "operator_identity_cryptographically_verified": False,
        "identity_verification_asserted": confirm,
        "identity_verification_method": normalized_method,
        "identity_evidence_ref": normalized_evidence_ref,
        "acknowledged_public_key_sha256s": acknowledged,
    }
    digest = _sha256(
        _canonical(
            {
                "binding": binding,
                "candidate_scope": scope,
                "decision": decision_row,
                "recorded_at": recorded_at.isoformat(),
            }
        )
    )
    status = {
        "confirm_identity_verified": "identity_verified",
        "reject_candidate": "candidate_rejected",
        "defer": "deferred",
    }[normalized_decision]
    next_action = {
        "confirm_identity_verified": (
            "stage an exact trust-registry enrollment preview for separate "
            "operator authorization; do not modify the registry or enroll trust"
        ),
        "reject_candidate": (
            "leave producer trust unchanged and remove or replace the rejected "
            "candidate through the producer-owned intake lane"
        ),
        "defer": (
            "leave producer trust unchanged; submit a fresh candidate review "
            "when the operator is ready"
        ),
    }[normalized_decision]
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "decision_id": f"pqtrustdecision_{digest[:24]}",
        "recorded_at": recorded_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "status": status,
        "next_action": next_action,
        "binding": binding,
        "candidate_scope": scope,
        "decision": decision_row,
        "candidate_review_recorded": True,
        **_safety_fields(),
        "identity_verification_asserted": confirm,
        "trust_enrollment_preview_authorized": confirm,
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(receipt)),
    }
    return receipt


def verify_candidate_review_decision(
    receipt: Mapping[str, Any],
    *,
    report: Mapping[str, Any],
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
        return _blocked("trust_candidate_decision_integrity_invalid", now=observed_now)
    recorded_at = _timestamp(receipt.get("recorded_at"))
    expires_at = _timestamp(receipt.get("expires_at"))
    decision_row = receipt.get("decision")
    if not (
        recorded_at is not None
        and expires_at is not None
        and recorded_at <= expires_at
        and recorded_at <= observed_now <= expires_at
        and isinstance(decision_row, Mapping)
    ):
        return _blocked("trust_candidate_decision_not_fresh", now=observed_now)
    try:
        expected = build_candidate_review_decision(
            report,
            decision=str(decision_row.get("value") or ""),
            decider_id=str(decision_row.get("decider_id") or ""),
            acknowledged_public_key_sha256s=list(
                decision_row.get("acknowledged_public_key_sha256s") or []
            ),
            identity_verification_method=str(
                decision_row.get("identity_verification_method") or ""
            ),
            identity_evidence_ref=str(
                decision_row.get("identity_evidence_ref") or ""
            ),
            now=recorded_at,
        )
    except (TypeError, ValueError):
        return _blocked(
            "trust_candidate_decision_review_not_admissible",
            now=observed_now,
        )
    if not (
        dict(receipt) == expected
        and _DECISION_ID.fullmatch(str(receipt.get("decision_id") or ""))
        and receipt.get("decision_confers_trust") is False
        and receipt.get("trust_enrollment_authorized") is False
        and receipt.get("trust_registry_modified") is False
        and receipt.get("private_key_material_requested") is False
        and receipt.get("producer_dispatch_authorized") is False
        and receipt.get("automatic_execution_allowed") is False
        and receipt.get("execution_authorized") is False
        and receipt.get("deployment_or_restart_authorized") is False
        and receipt.get("protected_operation_executed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("delivery_authorized") is False
    ):
        return _blocked(
            "trust_candidate_decision_contract_not_admissible",
            now=observed_now,
        )
    decision_value = str(decision_row.get("value") or "")
    confirm = decision_value == "confirm_identity_verified"
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "review_state": str(receipt.get("status") or ""),
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": str(receipt.get("next_action") or ""),
        "candidate_review_id": str(
            receipt.get("binding", {}).get("candidate_review_id") or ""
        ),
        "trust_intake_verification_sha256": str(
            receipt.get("binding", {}).get(
                "trust_intake_verification_sha256"
            )
            or ""
        ),
        "candidate_scope": list(receipt.get("candidate_scope") or []),
        "decision_id": str(receipt.get("decision_id") or ""),
        "decision": decision_value,
        "decider_id": str(decision_row.get("decider_id") or ""),
        "recorded_at": str(receipt.get("recorded_at") or ""),
        "expires_at": str(receipt.get("expires_at") or ""),
        "identity_verification_method": str(
            decision_row.get("identity_verification_method") or ""
        ),
        "identity_evidence_ref": str(
            decision_row.get("identity_evidence_ref") or ""
        ),
        "progress": {
            "current_evidence_verified": False,
            "candidate_review_verified": True,
            "candidate_decision_recorded": True,
        },
        **_safety_fields(),
        "identity_verification_asserted": confirm,
        "trust_enrollment_preview_authorized": confirm,
    }


def verify_candidate_review_decision_for_report(
    report: Mapping[str, Any],
    *,
    decision_dir: Path = DEFAULT_DECISION_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    candidates = list(report.get("candidates") or [])
    if not candidates:
        if not (
            report.get("status") == "verified"
            and report.get("action_required") is False
            and report.get("public_key_candidate_recorded") is False
        ):
            return _blocked(
                "trust_candidate_decision_current_intake_not_admissible",
                now=observed_now,
            )
        return _not_required(now=observed_now)
    if not _review_report_is_admissible(report, now=observed_now):
        return _blocked(
            "trust_candidate_decision_current_review_not_admissible",
            now=observed_now,
        )
    try:
        path = _decision_path(
            decision_dir,
            candidate_review_id=str(report.get("candidate_review_id") or ""),
            trust_intake_verification_sha256=str(
                report.get("verification_receipt_sha256") or ""
            ),
        )
        try:
            receipt, _raw, receipt_sha256 = trust_intake._private_snapshot(
                path,
                field="source_refresh_trust_candidate_decision",
            )
        except FileNotFoundError:
            return _pending(report, decision_path=path, now=observed_now)
    except Exception:
        return _blocked(
            "trust_candidate_decision_receipt_not_admissible",
            now=observed_now,
        )
    verification = verify_candidate_review_decision(
        receipt,
        report=report,
        now=observed_now,
    )
    if verification.get("status") != "verified":
        return verification
    verification["progress"]["current_evidence_verified"] = True
    verification["decision_path"] = str(path)
    verification["decision_sha256"] = receipt_sha256
    return verification


def materialize_candidate_review_decision_for_report(
    report: Mapping[str, Any],
    *,
    decision: str,
    decider_id: str,
    expected_candidate_review_id: str,
    expected_trust_intake_verification_sha256: str,
    acknowledged_public_key_sha256s: Sequence[str] = (),
    identity_verification_method: str = "",
    identity_evidence_ref: str = "",
    decision_dir: Path = DEFAULT_DECISION_DIR,
    now: datetime | None = None,
) -> tuple[dict[str, Any], Path]:
    observed_now = _now(now)
    if not (
        _review_report_is_admissible(report, now=observed_now)
        and expected_candidate_review_id
        == str(report.get("candidate_review_id") or "")
        and expected_trust_intake_verification_sha256
        == str(report.get("verification_receipt_sha256") or "")
    ):
        raise ValueError("operator_candidate_review_binding_mismatch")
    receipt = build_candidate_review_decision(
        report,
        decision=decision,
        decider_id=decider_id,
        acknowledged_public_key_sha256s=acknowledged_public_key_sha256s,
        identity_verification_method=identity_verification_method,
        identity_evidence_ref=identity_evidence_ref,
        now=observed_now,
    )
    path = _decision_path(
        decision_dir,
        candidate_review_id=expected_candidate_review_id,
        trust_intake_verification_sha256=(
            expected_trust_intake_verification_sha256
        ),
    )
    try:
        atomic_write_bytes(path, _canonical(receipt), overwrite=False)
    except OutputExistsError:
        existing, _raw, _digest = trust_intake._private_snapshot(
            path,
            field="source_refresh_trust_candidate_decision",
        )
        existing_decision = existing.get("decision")
        existing_verification = verify_candidate_review_decision(
            existing,
            report=report,
            now=observed_now,
        )
        if not (
            existing_verification.get("status") == "verified"
            and isinstance(existing_decision, Mapping)
            and existing_decision.get("value") == str(decision or "").strip()
            and existing_decision.get("decider_id")
            == str(decider_id or "").strip()
            and existing_decision.get("acknowledged_public_key_sha256s")
            == sorted(
                {
                    str(value or "").strip()
                    for value in acknowledged_public_key_sha256s
                }
            )
            and existing_decision.get("identity_verification_method")
            == str(identity_verification_method or "").strip()
            and existing_decision.get("identity_evidence_ref")
            == str(identity_evidence_ref or "").strip()
        ):
            raise ValueError("trust_candidate_decision_already_recorded")
        receipt = existing
    return receipt, path


def _current_report(
    *,
    claim_verification_path: Path,
    trust_registry_path: Path,
    candidate_dir: Path,
    intake_receipt_path: Path,
    intake_verification_path: Path,
    now: datetime,
) -> dict[str, Any]:
    return trust_intake.inspect_trust_intake_bundle(
        claim_verification_path=claim_verification_path,
        trust_registry_path=trust_registry_path,
        candidate_dir=candidate_dir,
        receipt_path=intake_receipt_path,
        verification_path=intake_verification_path,
        now=now,
    )


def verify_current_candidate_review_decision(
    *,
    decision_dir: Path = DEFAULT_DECISION_DIR,
    claim_verification_path: Path = claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = claims.DEFAULT_TRUST_REGISTRY_PATH,
    candidate_dir: Path = trust_intake.DEFAULT_CANDIDATE_DIR,
    intake_receipt_path: Path = trust_intake.DEFAULT_RECEIPT_PATH,
    intake_verification_path: Path = trust_intake.DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        report = _current_report(
            claim_verification_path=claim_verification_path,
            trust_registry_path=trust_registry_path,
            candidate_dir=candidate_dir,
            intake_receipt_path=intake_receipt_path,
            intake_verification_path=intake_verification_path,
            now=observed_now,
        )
    except Exception:
        return _blocked(
            "trust_candidate_decision_current_evidence_unavailable",
            now=observed_now,
        )
    return verify_candidate_review_decision_for_report(
        report,
        decision_dir=decision_dir,
        now=observed_now,
    )


def project_candidate_review_decision(
    report: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    projected = dict(report)
    status = str(decision.get("status") or "")
    if status == "not_required":
        projected["review_decision"] = dict(decision)
        return projected
    if status == "pending":
        if not (
            decision.get("candidate_review_id")
            == report.get("candidate_review_id")
            and decision.get("trust_intake_verification_sha256")
            == report.get("verification_receipt_sha256")
        ):
            raise ValueError("trust_candidate_decision_pending_binding_mismatch")
        projected["review_decision"] = dict(decision)
        return projected
    if status != "verified" or not (
        decision.get("candidate_review_id")
        == report.get("candidate_review_id")
        and decision.get("trust_intake_verification_sha256")
        == report.get("verification_receipt_sha256")
        and decision.get("trust_enrollment_authorized") is False
        and decision.get("trust_registry_modified") is False
        and decision.get("provider_quota_consumption_allowed") is False
        and decision.get("delivery_authorized") is False
        and decision.get("protected_operation_executed") is False
    ):
        raise ValueError("trust_candidate_decision_projection_not_admissible")
    decision_value = str(decision.get("decision") or "")
    projected["intake_state"] = {
        "confirm_identity_verified": "candidate_identity_verified_preview_required",
        "reject_candidate": "candidate_rejected",
        "defer": "candidate_review_deferred",
    }[decision_value]
    projected["next_action"] = str(decision.get("next_action") or "")
    projected["action_required"] = False
    projected["interrupt_operator"] = False
    projected["operator_review_required"] = False
    projected["trust_enrollment_preview_authorized"] = (
        decision.get("trust_enrollment_preview_authorized") is True
    )
    projected["trust_enrollment_authorized"] = False
    projected["trust_registry_modified"] = False
    projected["review_decision"] = dict(decision)
    return projected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record or verify an exact producer-key candidate review. This "
            "command never enrolls trust or edits the trust registry."
        )
    )
    parser.add_argument("--verify-current", action="store_true")
    parser.add_argument("--decision", choices=DECISIONS)
    parser.add_argument("--decider-id")
    parser.add_argument("--candidate-review-id")
    parser.add_argument("--trust-intake-verification-sha256")
    parser.add_argument(
        "--expected-public-key-sha256",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--identity-verification-method",
        choices=IDENTITY_VERIFICATION_METHODS,
        default="",
    )
    parser.add_argument("--identity-evidence-ref", default="")
    parser.add_argument("--decision-dir", type=Path, default=DEFAULT_DECISION_DIR)
    parser.add_argument(
        "--verification-write",
        type=Path,
        default=DEFAULT_VERIFICATION_PATH,
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
    args = parser.parse_args(argv)
    observed_now = _now()
    path: Path | None = None
    try:
        report = _current_report(
            claim_verification_path=args.claim_verification,
            trust_registry_path=args.trust_registry,
            candidate_dir=args.candidate_dir,
            intake_receipt_path=args.intake_receipt,
            intake_verification_path=args.intake_verification,
            now=observed_now,
        )
        if args.verify_current:
            result = verify_candidate_review_decision_for_report(
                report,
                decision_dir=args.decision_dir,
                now=observed_now,
            )
        else:
            if not all(
                str(value or "").strip()
                for value in (
                    args.decision,
                    args.decider_id,
                    args.candidate_review_id,
                    args.trust_intake_verification_sha256,
                )
            ):
                raise ValueError("explicit_candidate_decision_arguments_required")
            _receipt, path = materialize_candidate_review_decision_for_report(
                report,
                decision=str(args.decision),
                decider_id=str(args.decider_id),
                expected_candidate_review_id=str(args.candidate_review_id),
                expected_trust_intake_verification_sha256=str(
                    args.trust_intake_verification_sha256
                ),
                acknowledged_public_key_sha256s=(
                    args.expected_public_key_sha256
                ),
                identity_verification_method=(
                    args.identity_verification_method
                ),
                identity_evidence_ref=args.identity_evidence_ref,
                decision_dir=args.decision_dir,
                now=observed_now,
            )
            result = verify_candidate_review_decision_for_report(
                report,
                decision_dir=args.decision_dir,
                now=observed_now,
            )
    except Exception:
        result = _blocked(
            "trust_candidate_decision_materialization_failed",
            now=observed_now,
        )
    if path is not None:
        result["decision_path"] = str(path)
    try:
        atomic_write_bytes(
            Path(args.verification_write).absolute(),
            _canonical(result),
            overwrite=True,
        )
        result["verification_receipt_persisted"] = True
    except Exception:
        result["verification_receipt_persisted"] = False
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") in {
        "not_required",
        "pending",
        "verified",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
