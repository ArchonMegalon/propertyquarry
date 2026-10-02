#!/usr/bin/env python3
"""Deliver one receipt-backed operator alert for a current trust candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_notify_gold_status as gold_notify
from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


SCHEMA = "propertyquarry.ooda_source_refresh_trust_candidate_notification.v1"
VERIFY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_notification_verification.v1"
)
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-candidate-notification.json"
)
DEFAULT_PRESENTATION_STATE_PATH = trust_intake.DEFAULT_PRESENTATION_STATE_PATH
DEFAULT_LOCK_PATH = Path(notification_cycle.DEFAULT_LOCK_PATH)
DEFAULT_MAX_AGE_SECONDS = trust_intake.DEFAULT_MAX_AGE_SECONDS
MAX_RECEIPT_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HEALTHY_STATUSES = {
    "not_required",
    "resolved",
    "action_required",
    "deduplicated",
    "completed",
}
_FAILURE_STATUSES = {
    "blocked",
    "delivery_failed",
    "delivery_unrecorded",
    "send_cycle_busy",
    "send_lock_unavailable",
}


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return trust_intake._canonical(dict(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safety_fields() -> dict[str, bool]:
    return {
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
        "secret_values_recorded": False,
    }


def _base_report(
    *,
    status: str,
    now: datetime,
    execution_mode: str,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": status,
        "generated_at": now.isoformat(),
        "execution_mode": execution_mode,
        "notification_policy": "action_required_only",
        "blocking_reason": "",
        "next_action": "await a current verified producer public-key candidate",
        "candidate_review_id": "",
        "trust_intake_receipt_sha256": "",
        "trust_intake_verification_sha256": "",
        "candidate_scope": [],
        "candidate_receipt_sha256s": [],
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "would_send": False,
        "delivery_mode": "",
        "message_ids": [],
        "presentation_recorded": False,
        "presentation_status": "not_required",
        "receipt_persisted": True,
        **_safety_fields(),
    }


def _blocked(
    reason: str,
    *,
    now: datetime,
    execution_mode: str,
) -> dict[str, Any]:
    report = _base_report(
        status="blocked",
        now=now,
        execution_mode=execution_mode,
    )
    report.update(
        {
            "blocking_reason": reason,
            "next_action": (
                "repair the current candidate-review notification evidence; "
                "do not enroll trust, edit the registry, or retry delivery manually"
            ),
        }
    )
    return report


def _candidate_scope(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
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
        for candidate in list(report.get("candidates") or [])
        if isinstance(candidate, Mapping)
    ]


def _pending_review_is_admissible(
    intake: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> bool:
    progress = decision.get("progress")
    return bool(
        trust_intake._review_is_admissible(intake)
        and decision.get("schema") == trust_decision.VERIFY_SCHEMA
        and decision.get("status") == "pending"
        and decision.get("review_state") == "candidate_review_pending"
        and decision.get("candidate_review_id")
        == intake.get("candidate_review_id")
        and decision.get("trust_intake_verification_sha256")
        == intake.get("verification_receipt_sha256")
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
        and progress.get("candidate_review_verified") is True
        and progress.get("candidate_decision_recorded") is False
        and decision.get("action_required") is True
        and decision.get("operator_review_required") is True
        and decision.get("interrupt_operator") is False
        and decision.get("identity_verification_asserted") is False
        and decision.get("trust_enrollment_preview_authorized") is False
        and decision.get("trust_enrollment_authorized") is False
        and decision.get("trust_registry_modified") is False
        and decision.get("decision_confers_trust") is False
        and decision.get("private_key_material_requested") is False
        and decision.get("private_key_material_recorded") is False
        and decision.get("producer_dispatch_authorized") is False
        and decision.get("producer_refresh_authorized") is False
        and decision.get("automatic_source_refresh_allowed") is False
        and decision.get("automatic_execution_allowed") is False
        and decision.get("execution_authorized") is False
        and decision.get("deployment_or_restart_authorized") is False
        and decision.get("protected_operation_executed") is False
        and decision.get("provider_quota_consumption_allowed") is False
        and decision.get("delivery_authorized") is False
        and decision.get("delivery_attempted") is False
        and decision.get("sent") is False
    )


def _resolved_review_is_admissible(
    intake: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> bool:
    progress = decision.get("progress")
    confirm = decision.get("decision") == "confirm_identity_verified"
    return bool(
        decision.get("schema") == trust_decision.VERIFY_SCHEMA
        and decision.get("status") == "verified"
        and decision.get("candidate_review_id")
        == intake.get("candidate_review_id")
        and decision.get("trust_intake_verification_sha256")
        == intake.get("verification_receipt_sha256")
        and decision.get("decision") in trust_decision.DECISIONS
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
        and progress.get("candidate_review_verified") is True
        and progress.get("candidate_decision_recorded") is True
        and decision.get("action_required") is False
        and decision.get("interrupt_operator") is False
        and decision.get("operator_review_required") is False
        and (decision.get("identity_verification_asserted") is True)
        is confirm
        and (decision.get("trust_enrollment_preview_authorized") is True)
        is confirm
        and decision.get("trust_enrollment_authorized") is False
        and decision.get("trust_registry_modified") is False
        and decision.get("decision_confers_trust") is False
        and decision.get("private_key_material_requested") is False
        and decision.get("private_key_material_recorded") is False
        and decision.get("producer_dispatch_authorized") is False
        and decision.get("producer_refresh_authorized") is False
        and decision.get("automatic_source_refresh_allowed") is False
        and decision.get("automatic_execution_allowed") is False
        and decision.get("execution_authorized") is False
        and decision.get("deployment_or_restart_authorized") is False
        and decision.get("provider_quota_consumption_allowed") is False
        and decision.get("delivery_authorized") is False
        and decision.get("delivery_attempted") is False
        and decision.get("sent") is False
        and decision.get("protected_operation_executed") is False
    )


def _not_required_is_admissible(
    intake: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> bool:
    progress = decision.get("progress")
    return bool(
        intake.get("status") == "verified"
        and not list(intake.get("candidates") or [])
        and intake.get("action_required") is False
        and intake.get("public_key_candidate_recorded") is False
        and decision.get("schema") == trust_decision.VERIFY_SCHEMA
        and decision.get("status") == "not_required"
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
        and progress.get("candidate_review_verified") is True
        and progress.get("candidate_decision_recorded") is False
        and decision.get("action_required") is False
        and decision.get("interrupt_operator") is False
        and decision.get("operator_review_required") is False
        and decision.get("identity_verification_asserted") is False
        and decision.get("trust_enrollment_preview_authorized") is False
        and decision.get("trust_enrollment_authorized") is False
        and decision.get("trust_registry_modified") is False
        and decision.get("decision_confers_trust") is False
        and decision.get("private_key_material_requested") is False
        and decision.get("private_key_material_recorded") is False
        and decision.get("producer_dispatch_authorized") is False
        and decision.get("producer_refresh_authorized") is False
        and decision.get("automatic_source_refresh_allowed") is False
        and decision.get("automatic_execution_allowed") is False
        and decision.get("execution_authorized") is False
        and decision.get("deployment_or_restart_authorized") is False
        and decision.get("provider_quota_consumption_allowed") is False
        and decision.get("delivery_authorized") is False
        and decision.get("delivery_attempted") is False
        and decision.get("sent") is False
        and decision.get("protected_operation_executed") is False
    )


def _bind_review(report: dict[str, Any], intake: Mapping[str, Any]) -> None:
    scope = _candidate_scope(intake)
    report.update(
        {
            "candidate_review_id": str(
                intake.get("candidate_review_id") or ""
            ),
            "trust_intake_receipt_sha256": str(
                intake.get("intake_receipt_sha256") or ""
            ),
            "trust_intake_verification_sha256": str(
                intake.get("verification_receipt_sha256") or ""
            ),
            "candidate_scope": scope,
            "candidate_receipt_sha256s": sorted(
                row["candidate_receipt_sha256"] for row in scope
            ),
        }
    )


def _message(intake: Mapping[str, Any], *, generated_at: str) -> str:
    lines = [
        "PropertyQuarry producer identity review required.",
        f"Generated: {generated_at}",
        f"Review ID: {str(intake.get('candidate_review_id') or '')}",
        "",
    ]
    for row in _candidate_scope(intake):
        lines.append(
            "Candidate: "
            f"producer={row['producer_id']} key={row['key_id']} "
            f"fingerprint=sha256:{row['public_key_sha256']} "
            f"lanes={','.join(row['lanes'])}"
        )
    lines.extend(
        [
            "",
            (
                "Safe next action: verify producer identity and every fingerprint "
                "out of band, then use the operator summary's exact immutable "
                "confirm, reject, or defer command."
            ),
            (
                "Consent gate: this alert and candidate confer no trust; enrollment, "
                "registry edits, provider access, delivery beyond this alert, and "
                "execution remain unauthorized."
            ),
        ]
    )
    return "\n".join(lines)


def _with_integrity(report: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(report)
    normalized.pop("integrity", None)
    normalized["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(normalized)),
    }
    return normalized


def _integrity_verified(report: Mapping[str, Any]) -> bool:
    normalized = dict(report)
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


def _persist(report: Mapping[str, Any], *, receipt_path: Path) -> dict[str, Any]:
    target = Path(receipt_path).absolute()
    payload = _with_integrity(report)
    try:
        atomic_write_bytes(target, _canonical(payload), overwrite=True)
        persisted, raw, digest = trust_intake._private_snapshot(
            target,
            field="source_refresh_trust_candidate_notification",
        )
        if persisted != payload or not _integrity_verified(persisted):
            raise ValueError(
                "source_refresh_trust_candidate_notification_persistence_mismatch"
            )
    except Exception:
        failed = dict(report)
        failed.update(
            {
                "status": (
                    "delivery_unrecorded"
                    if report.get("sent") is True
                    else "blocked"
                ),
                "blocking_reason": (
                    "source_refresh_trust_candidate_notification_persistence_failed"
                ),
                "interrupt_operator": False,
                "would_send": False,
                "receipt_persisted": False,
            }
        )
        return failed
    result = dict(payload)
    result.pop("integrity", None)
    result.update(
        {
            "receipt_path": str(target),
            "notification_receipt_sha256": digest,
            "notification_receipt_bytes": len(raw),
        }
    )
    return result


def run_candidate_notification(
    intake: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    presentation_state_path: Path = DEFAULT_PRESENTATION_STATE_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    principal_id: str = notification_cycle.DEFAULT_PRINCIPAL_ID,
    base_url: str = notification_cycle.DEFAULT_BASE_URL,
    send: bool = False,
    now: datetime | None = None,
    deliver: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate or deliver one current candidate-review alert."""

    observed_now = _now(now)
    execution_mode = "send" if send else "evaluate_only"
    if _not_required_is_admissible(intake, decision):
        report = _base_report(
            status="not_required",
            now=observed_now,
            execution_mode=execution_mode,
        )
        _bind_review(report, intake)
        return _persist(
            report,
            receipt_path=receipt_path,
        )
    if _resolved_review_is_admissible(intake, decision):
        report = _base_report(
            status="resolved",
            now=observed_now,
            execution_mode=execution_mode,
        )
        _bind_review(report, intake)
        report["next_action"] = str(decision.get("next_action") or "")
        report["trust_enrollment_preview_authorized"] = (
            decision.get("trust_enrollment_preview_authorized") is True
        )
        return _persist(report, receipt_path=receipt_path)
    if not _pending_review_is_admissible(intake, decision):
        return _persist(
            _blocked(
                "source_refresh_trust_candidate_notification_review_not_admissible",
                now=observed_now,
                execution_mode=execution_mode,
            ),
            receipt_path=receipt_path,
        )
    try:
        projected = trust_intake.apply_candidate_presentation_state(
            intake,
            state_path=presentation_state_path,
        )
    except Exception:
        return _persist(
            _blocked(
                "source_refresh_trust_candidate_presentation_not_admissible",
                now=observed_now,
                execution_mode=execution_mode,
            ),
            receipt_path=receipt_path,
        )
    report = _base_report(
        status="action_required",
        now=observed_now,
        execution_mode=execution_mode,
    )
    _bind_review(report, intake)
    report.update(
        {
            "next_action": str(intake.get("next_action") or ""),
            "action_required": True,
            "operator_review_required": True,
            "interrupt_operator": projected.get("interrupt_operator") is True,
            "would_send": projected.get("interrupt_operator") is True,
            "presentation_status": str(
                dict(projected.get("presentation") or {}).get("state")
                or "unknown"
            ),
        }
    )
    if not report["interrupt_operator"]:
        report.update(
            {
                "status": "deduplicated",
                "would_send": False,
                "next_action": (
                    "await an immutable candidate decision or a new verified review"
                ),
            }
        )
        return _persist(report, receipt_path=receipt_path)
    if not send:
        report["message_preview"] = _message(
            intake,
            generated_at=report["generated_at"],
        )
        return _persist(report, receipt_path=receipt_path)
    if not str(principal_id or "").strip():
        report.update(
            {
                "status": "blocked",
                "blocking_reason": "delivery_authority_incomplete",
                "interrupt_operator": False,
                "would_send": False,
            }
        )
        return _persist(report, receipt_path=receipt_path)
    report["delivery_authorized"] = True
    try:
        lock_descriptor = notification_cycle._acquire_send_lock(
            Path(lock_path).absolute()
        )
    except Exception as exc:
        report.update(
            {
                "status": "send_lock_unavailable",
                "blocking_reason": type(exc).__name__,
                "interrupt_operator": False,
                "would_send": False,
            }
        )
        return _persist(report, receipt_path=receipt_path)
    if lock_descriptor is None:
        report.update(
            {
                "status": "send_cycle_busy",
                "blocking_reason": "send_cycle_busy",
                "interrupt_operator": False,
                "would_send": False,
            }
        )
        return _persist(report, receipt_path=receipt_path)
    try:
        report["delivery_attempted"] = True
        delivery_function = deliver or gold_notify.deliver_notification_for_principal
        try:
            delivery = delivery_function(
                principal_id=str(principal_id or "").strip(),
                text=_message(intake, generated_at=report["generated_at"]),
                url_buttons=[[('Open PropertyQuarry', str(base_url or "").strip())]],
            )
        except Exception as exc:
            report.update(
                {
                    "status": "delivery_failed",
                    "blocking_reason": type(exc).__name__,
                    "interrupt_operator": False,
                    "would_send": False,
                }
            )
            return _persist(report, receipt_path=receipt_path)
        delivery_mode = str(delivery.get("delivery_mode") or "").strip()
        message_ids = [
            str(value)
            for value in list(delivery.get("message_ids") or [])
            if str(value or "").strip()
        ]
        if not delivery_mode or not message_ids:
            report.update(
                {
                    "status": "delivery_failed",
                    "blocking_reason": "delivery_receipt_incomplete",
                    "interrupt_operator": False,
                    "would_send": False,
                }
            )
            return _persist(report, receipt_path=receipt_path)
        report.update(
            {
                "sent": True,
                "would_send": False,
                "delivery_mode": delivery_mode,
                "message_ids": message_ids,
            }
        )
        presentation = trust_intake.record_candidate_presentation(
            intake,
            state_path=presentation_state_path,
            expected_candidate_review_id=str(
                intake.get("candidate_review_id") or ""
            ),
            now=observed_now,
        )
        report["presentation_status"] = str(
            presentation.get("status") or "blocked"
        )
        if presentation.get("status") not in {"recorded", "unchanged"}:
            report.update(
                {
                    "status": "delivery_unrecorded",
                    "blocking_reason": str(
                        presentation.get("blocking_reason")
                        or "candidate_presentation_not_recorded"
                    ),
                    "interrupt_operator": False,
                }
            )
            return _persist(report, receipt_path=receipt_path)
        report.update(
            {
                "status": "completed",
                "interrupt_operator": False,
                "presentation_recorded": True,
                "next_action": (
                    "await the exact immutable confirm, reject, or defer decision"
                ),
            }
        )
        return _persist(report, receipt_path=receipt_path)
    finally:
        notification_cycle._release_send_lock(lock_descriptor)


def verify_notification_receipt(
    receipt: Mapping[str, Any],
    *,
    intake: Mapping[str, Any],
    decision: Mapping[str, Any],
    presentation_state_path: Path = DEFAULT_PRESENTATION_STATE_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    generated_at = trust_intake._timestamp(receipt.get("generated_at"))
    age_seconds = (
        (observed_now - generated_at).total_seconds()
        if generated_at is not None
        else math.inf
    )
    status = str(receipt.get("status") or "")
    try:
        expected_message_preview = (
            _message(
                intake,
                generated_at=str(receipt.get("generated_at") or ""),
            )
            if status == "action_required"
            else None
        )
    except (TypeError, ValueError):
        expected_message_preview = None
    if not (
        receipt.get("schema") == SCHEMA
        and generated_at is not None
        and math.isfinite(age_seconds)
        and -30.0 <= age_seconds <= max_age_seconds
        and _integrity_verified(receipt)
        and status in _HEALTHY_STATUSES
        and receipt.get("trust_enrollment_authorized") is False
        and receipt.get("trust_registry_modified") is False
        and receipt.get("decision_confers_trust") is False
        and receipt.get("private_key_material_requested") is False
        and receipt.get("private_key_material_recorded") is False
        and receipt.get("producer_dispatch_authorized") is False
        and receipt.get("producer_refresh_authorized") is False
        and receipt.get("automatic_source_refresh_allowed") is False
        and receipt.get("automatic_execution_allowed") is False
        and receipt.get("execution_authorized") is False
        and receipt.get("deployment_or_restart_authorized") is False
        and receipt.get("protected_operation_executed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("secret_values_recorded") is False
        and receipt.get("receipt_persisted") is True
        and ("message_preview" in receipt)
        is (status == "action_required")
        and (
            status != "action_required"
            or receipt.get("message_preview") == expected_message_preview
        )
        and (
            receipt.get("trust_enrollment_preview_authorized") is True
        )
        is (
            status == "resolved"
            and decision.get("trust_enrollment_preview_authorized") is True
        )
    ):
        return {
            **_blocked(
                "source_refresh_trust_candidate_notification_receipt_not_admissible",
                now=observed_now,
                execution_mode="inspect",
            ),
            "schema": VERIFY_SCHEMA,
        }
    candidates_present = bool(list(intake.get("candidates") or []))
    expected_binding = {
        "candidate_review_id": str(intake.get("candidate_review_id") or ""),
        "trust_intake_receipt_sha256": str(
            intake.get("intake_receipt_sha256") or ""
        ),
        "trust_intake_verification_sha256": str(
            intake.get("verification_receipt_sha256") or ""
        ),
        "candidate_scope": _candidate_scope(intake),
        "candidate_receipt_sha256s": sorted(
            row["candidate_receipt_sha256"] for row in _candidate_scope(intake)
        ),
    }
    if status == "not_required":
        current_binding = _not_required_is_admissible(intake, decision)
    elif status == "resolved":
        current_binding = _resolved_review_is_admissible(intake, decision)
    else:
        current_binding = _pending_review_is_admissible(intake, decision)
    if not (
        current_binding
        and all(receipt.get(key) == value for key, value in expected_binding.items())
        and (receipt.get("action_required") is True)
        is (status in {"action_required", "deduplicated", "completed"})
        and (receipt.get("operator_review_required") is True)
        is (status in {"action_required", "deduplicated", "completed"})
        and (receipt.get("delivery_attempted") is True)
        is (status == "completed")
        and (receipt.get("sent") is True) is (status == "completed")
        and (receipt.get("presentation_recorded") is True)
        is (status == "completed")
        and (receipt.get("delivery_authorized") is True)
        is (status == "completed")
        and (receipt.get("interrupt_operator") is True)
        is (status == "action_required")
        and (receipt.get("would_send") is True)
        is (status == "action_required")
        and bool(str(receipt.get("delivery_mode") or "").strip())
        is (status == "completed")
        and bool(list(receipt.get("message_ids") or []))
        is (status == "completed")
        and (not candidates_present or status != "not_required")
    ):
        return {
            **_blocked(
                "source_refresh_trust_candidate_notification_binding_mismatch",
                now=observed_now,
                execution_mode="inspect",
            ),
            "schema": VERIFY_SCHEMA,
        }
    if status in {"completed", "deduplicated"}:
        try:
            current_projection = trust_intake.apply_candidate_presentation_state(
                intake,
                state_path=presentation_state_path,
            )
        except Exception:
            current_projection = {}
        if current_projection.get("interrupt_operator") is not False:
            return {
                **_blocked(
                    "source_refresh_trust_candidate_presentation_binding_unavailable",
                    now=observed_now,
                    execution_mode="inspect",
                ),
                "schema": VERIFY_SCHEMA,
            }
    verification = {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "notification_status": status,
        "updated_at": observed_now.isoformat(),
        "generated_at": generated_at.isoformat(),
        "blocking_reason": "",
        "candidate_review_id": str(
            receipt.get("candidate_review_id") or ""
        ),
        "action_required": receipt.get("action_required") is True,
        "interrupt_operator": receipt.get("interrupt_operator") is True,
        "delivery_authorized": receipt.get("delivery_authorized") is True,
        "delivery_attempted": receipt.get("delivery_attempted") is True,
        "sent": receipt.get("sent") is True,
        "presentation_recorded": receipt.get("presentation_recorded") is True,
        **_safety_fields(),
        "progress": {
            "current_evidence_verified": True,
            "notification_integrity_verified": True,
            "candidate_review_binding_verified": True,
        },
    }
    verification["trust_enrollment_preview_authorized"] = (
        receipt.get("trust_enrollment_preview_authorized") is True
    )
    return verification


def inspect_current_candidate_notification(
    *,
    decision_dir: Path = trust_decision.DEFAULT_DECISION_DIR,
    claim_verification_path: Path = source_claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = source_claims.DEFAULT_TRUST_REGISTRY_PATH,
    candidate_dir: Path = trust_intake.DEFAULT_CANDIDATE_DIR,
    intake_receipt_path: Path = trust_intake.DEFAULT_RECEIPT_PATH,
    intake_verification_path: Path = trust_intake.DEFAULT_VERIFICATION_PATH,
    presentation_state_path: Path = DEFAULT_PRESENTATION_STATE_PATH,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        intake = trust_intake.inspect_trust_intake_bundle(
            claim_verification_path=claim_verification_path,
            trust_registry_path=trust_registry_path,
            candidate_dir=candidate_dir,
            receipt_path=intake_receipt_path,
            verification_path=intake_verification_path,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        decision = trust_decision.verify_candidate_review_decision_for_report(
            intake,
            decision_dir=decision_dir,
            now=observed_now,
        )
        receipt, raw, digest = trust_intake._private_snapshot(
            receipt_path,
            field="source_refresh_trust_candidate_notification",
        )
        verification = verify_notification_receipt(
            receipt,
            intake=intake,
            decision=decision,
            presentation_state_path=presentation_state_path,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
    except Exception:
        return {
            **_blocked(
                "source_refresh_trust_candidate_notification_unavailable",
                now=observed_now,
                execution_mode="inspect",
            ),
            "schema": VERIFY_SCHEMA,
        }
    if verification.get("status") == "verified":
        verification.update(
            {
                "receipt_path": str(Path(receipt_path).absolute()),
                "notification_receipt_sha256": digest,
                "notification_receipt_bytes": len(raw),
            }
        )
    return verification


def record_current_candidate_notification_presentation(
    *,
    expected_candidate_review_id: str,
    decision_dir: Path = trust_decision.DEFAULT_DECISION_DIR,
    claim_verification_path: Path = source_claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = source_claims.DEFAULT_TRUST_REGISTRY_PATH,
    candidate_dir: Path = trust_intake.DEFAULT_CANDIDATE_DIR,
    intake_receipt_path: Path = trust_intake.DEFAULT_RECEIPT_PATH,
    intake_verification_path: Path = trust_intake.DEFAULT_VERIFICATION_PATH,
    presentation_state_path: Path = DEFAULT_PRESENTATION_STATE_PATH,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Record one exact displayed review and refresh its runtime receipt."""

    observed_now = _now(now)
    expected_review_id = str(expected_candidate_review_id or "").strip()
    try:
        intake = trust_intake.inspect_trust_intake_bundle(
            claim_verification_path=claim_verification_path,
            trust_registry_path=trust_registry_path,
            candidate_dir=candidate_dir,
            receipt_path=intake_receipt_path,
            verification_path=intake_verification_path,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        decision = trust_decision.verify_candidate_review_decision_for_report(
            intake,
            decision_dir=decision_dir,
            now=observed_now,
        )
        if not (
            expected_review_id
            and expected_review_id == intake.get("candidate_review_id")
            and _pending_review_is_admissible(intake, decision)
        ):
            raise ValueError(
                "source_refresh_trust_candidate_presentation_binding_mismatch"
            )
        presentation = trust_intake.record_candidate_presentation(
            intake,
            state_path=presentation_state_path,
            expected_candidate_review_id=expected_review_id,
            now=observed_now,
        )
        if not (
            presentation.get("status") in {"recorded", "unchanged"}
            and presentation.get("candidate_review_id") == expected_review_id
            and presentation.get("delivery_state_updated") is False
            and presentation.get("provider_quota_consumed") is False
            and presentation.get("trust_registry_modified") is False
            and presentation.get("protected_operation_executed") is False
        ):
            raise ValueError(
                "source_refresh_trust_candidate_presentation_not_recorded"
            )
        refreshed = run_candidate_notification(
            intake,
            decision,
            presentation_state_path=presentation_state_path,
            receipt_path=receipt_path,
            send=False,
            now=observed_now,
        )
        if not (
            refreshed.get("status") == "deduplicated"
            and refreshed.get("action_required") is True
            and refreshed.get("operator_review_required") is True
            and refreshed.get("interrupt_operator") is False
            and refreshed.get("would_send") is False
            and refreshed.get("delivery_authorized") is False
            and refreshed.get("delivery_attempted") is False
            and refreshed.get("sent") is False
            and refreshed.get("receipt_persisted") is True
            and refreshed.get("trust_enrollment_authorized") is False
            and refreshed.get("trust_registry_modified") is False
            and refreshed.get("producer_dispatch_authorized") is False
            and refreshed.get("producer_refresh_authorized") is False
            and refreshed.get("automatic_source_refresh_allowed") is False
            and refreshed.get("automatic_execution_allowed") is False
            and refreshed.get("execution_authorized") is False
            and refreshed.get("deployment_or_restart_authorized") is False
            and refreshed.get("protected_operation_executed") is False
            and refreshed.get("provider_quota_consumption_allowed") is False
            and refreshed.get("private_key_material_requested") is False
            and refreshed.get("private_key_material_recorded") is False
            and refreshed.get("secret_values_recorded") is False
        ):
            raise ValueError(
                "source_refresh_trust_candidate_notification_refresh_not_admissible"
            )
        persisted, raw, digest = trust_intake._private_snapshot(
            receipt_path,
            field="source_refresh_trust_candidate_notification",
        )
        verification = verify_notification_receipt(
            persisted,
            intake=intake,
            decision=decision,
            presentation_state_path=presentation_state_path,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        if not (
            verification.get("status") == "verified"
            and verification.get("notification_status") == "deduplicated"
            and verification.get("action_required") is True
            and verification.get("interrupt_operator") is False
        ):
            raise ValueError(
                "source_refresh_trust_candidate_notification_refresh_not_verified"
            )
    except Exception as exc:
        candidate_reason = str(exc).strip()
        safe_reasons = {
            "source_refresh_trust_candidate_presentation_binding_mismatch",
            "source_refresh_trust_candidate_presentation_not_recorded",
            "source_refresh_trust_candidate_notification_refresh_not_admissible",
            "source_refresh_trust_candidate_notification_refresh_not_verified",
        }
        reason = (
            candidate_reason
            if isinstance(exc, ValueError) and candidate_reason in safe_reasons
            else "source_refresh_trust_candidate_notification_presentation_unavailable"
        )
        return {
            **_blocked(
                reason,
                now=observed_now,
                execution_mode="record_presentation",
            ),
            "schema": VERIFY_SCHEMA,
            "presentation_transition_recorded": False,
        }
    verification.update(
        {
            "receipt_path": str(Path(receipt_path).absolute()),
            "notification_receipt_sha256": digest,
            "notification_receipt_bytes": len(raw),
            "presentation_transition_recorded": True,
            "presentation_receipt": dict(presentation),
        }
    )
    return verification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the current receipt-backed producer trust-candidate alert."
        )
    )
    parser.add_argument(
        "--decision-dir",
        type=Path,
        default=trust_decision.DEFAULT_DECISION_DIR,
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
        "--presentation-state",
        type=Path,
        default=DEFAULT_PRESENTATION_STATE_PATH,
    )
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=DEFAULT_MAX_AGE_SECONDS,
    )
    parser.add_argument("--record-presentation", action="store_true")
    parser.add_argument("--expected-candidate-review-id", default="")
    args = parser.parse_args(argv)
    common = {
        "decision_dir": args.decision_dir,
        "claim_verification_path": args.claim_verification,
        "trust_registry_path": args.trust_registry,
        "candidate_dir": args.candidate_dir,
        "intake_receipt_path": args.intake_receipt,
        "intake_verification_path": args.intake_verification,
        "presentation_state_path": args.presentation_state,
        "receipt_path": args.receipt,
        "max_age_seconds": float(args.max_age_seconds),
    }
    if args.record_presentation:
        result = record_current_candidate_notification_presentation(
            expected_candidate_review_id=(
                args.expected_candidate_review_id
            ),
            **common,
        )
    else:
        result = inspect_current_candidate_notification(**common)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
