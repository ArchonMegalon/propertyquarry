#!/usr/bin/env python3
"""Evaluate or deliver one governed operator ask for a trust-candidate artifact."""

from __future__ import annotations

import argparse
import hashlib
import math
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
from scripts import (
    propertyquarry_ooda_source_refresh_trust_candidate_artifact_request as artifact_request,
)
from scripts import (
    propertyquarry_ooda_source_refresh_trust_candidate_import as candidate_import,
)
from scripts import (
    propertyquarry_ooda_source_refresh_trust_candidate_manual_action as manual_action,
)
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_artifact_notification.v1"
)
VERIFY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_artifact_notification_verification.v1"
)
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-candidate-artifact-notification.json"
)
DEFAULT_PRESENTATION_STATE_PATH = candidate_import.DEFAULT_PRESENTATION_STATE_PATH
DEFAULT_MANUAL_ACTION_RECEIPT_PATH = manual_action.DEFAULT_RECEIPT_PATH
DEFAULT_LOCK_PATH = Path(notification_cycle.DEFAULT_LOCK_PATH)
DEFAULT_MAX_AGE_SECONDS = trust_intake.DEFAULT_MAX_AGE_SECONDS
_HEALTHY_STATUSES = {
    "not_required",
    "action_required",
    "deduplicated",
    "completed",
}
_FAILURE_STATUSES = {
    "blocked",
    "send_lock_unavailable",
    "send_cycle_busy",
    "delivery_failed",
    "delivery_unrecorded",
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
        "artifact_transport_authorized": False,
        "artifact_transport_attempted": False,
        "candidate_payload_recorded": False,
        "public_key_material_recorded": False,
        "host_path_values_recorded": False,
    }


def _safety_fields_admissible(value: Mapping[str, Any]) -> bool:
    delivery_fields = {"delivery_authorized", "delivery_attempted", "sent"}
    return all(
        value.get(key) is False
        for key in _safety_fields()
        if key not in delivery_fields
    )


def _expected_receipt_keys(status: str) -> frozenset[str]:
    keys = set(
        _base_report(
            status=status,
            now=datetime(2000, 1, 1, tzinfo=timezone.utc),
            execution_mode="evaluate_only",
        )
    )
    keys.add("integrity")
    if status == "action_required":
        keys.add("message_preview")
    return frozenset(keys)


def _expected_next_action(status: str, report: Mapping[str, Any]) -> str:
    if status == "not_required":
        return "await a current verified candidate-artifact action"
    if status == "action_required":
        return str(report.get("next_action") or "")
    return "await the external public candidate or the bounded reminder"


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
        "notification_policy": "genuine_action_only",
        "blocking_reason": "",
        "next_action": "await a current verified candidate-artifact action",
        "action_state": "",
        "request_id": "",
        "semantic_request_sha256": "",
        "artifact_request_receipt_sha256": "",
        "operator_action_receipt_sha256": "",
        "presentation_state": "not_required",
        "presentation_digest": "",
        "action_required": False,
        "interrupt_operator": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "would_send": False,
        "delivery_mode": "",
        "message_ids": [],
        "presentation_recorded": False,
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
                "repair the current artifact-action receipt chain; do not "
                "send, import, edit trust, or retry delivery manually"
            ),
        }
    )
    return report


def _source_admissible(
    report: Mapping[str, Any],
    artifact_verification: Mapping[str, Any],
    action_verification: Mapping[str, Any],
    *,
    now: datetime,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    import_state = str(report.get("import_state") or "")
    staged = import_state in manual_action._ACTION_STATES
    action_progress = action_verification.get("progress")
    if not (
        report.get("status") == "verified"
        and import_state
        in (manual_action._ACTION_STATES | manual_action._INACTIVE_STATES)
            and manual_action._artifact_verification_admissible(
                artifact_verification,
                report=report,
                import_state=import_state,
                now=now,
                max_age_seconds=max_age_seconds,
            )
        and action_verification.get("schema") == manual_action.VERIFY_SCHEMA
        and action_verification.get("status") == "verified"
        and action_verification.get("action_state") == import_state
        and action_verification.get("operator_action_receipt_staged")
        is staged
        and action_verification.get("action_required") is staged
        and action_verification.get("receipt_persisted") is True
        and candidate_import._SHA256.fullmatch(
            str(
                action_verification.get("operator_action_receipt_sha256")
                or ""
            )
        )
        and action_verification.get("artifact_request_receipt_sha256")
        == artifact_verification.get("artifact_request_receipt_sha256")
        and isinstance(action_progress, Mapping)
        and action_progress.get("current_evidence_verified") is True
        and action_progress.get("receipt_integrity_verified") is True
        and action_progress.get("artifact_request_binding_verified") is True
        and action_progress.get("presentation_binding_verified") is True
        and action_progress.get("path_free_projection_verified") is True
        and action_progress.get("public_only_projection_verified") is True
        and action_verification.get("producer_contacted") is False
        and action_verification.get("transport_delivery_authorized") is False
        and action_verification.get("transport_delivery_attempted") is False
        and action_verification.get("notification_sent") is False
        and action_verification.get("candidate_import_authorized") is False
        and action_verification.get("candidate_import_attempted") is False
        and action_verification.get("trust_registry_modified") is False
        and action_verification.get("provider_quota_consumption_allowed")
        is False
        and action_verification.get("delivery_authorized") is False
    ):
        return False
    if not staged:
        return bool(
            action_verification.get("interrupt_operator") is False
            and action_verification.get("presentation_state") == "not_required"
            and action_verification.get("presentation_digest") == ""
        )
    presentation = report.get("presentation")
    return bool(
        isinstance(presentation, Mapping)
        and action_verification.get("presentation_state")
        == presentation.get("state")
        and action_verification.get("presentation_digest")
        == presentation.get("presentation_digest")
        and (action_verification.get("interrupt_operator") is True)
        is (report.get("interrupt_operator") is True)
    )


def _bind_action(
    target: dict[str, Any],
    action_verification: Mapping[str, Any],
) -> None:
    for key in (
        "action_state",
        "request_id",
        "semantic_request_sha256",
        "artifact_request_receipt_sha256",
        "operator_action_receipt_sha256",
        "presentation_state",
        "presentation_digest",
    ):
        target[key] = str(action_verification.get(key) or "")


def _message(report: Mapping[str, Any], *, generated_at: str) -> str:
    import_state = str(report.get("import_state") or "")
    artifact = dict(report.get("artifact_request") or {})
    lines = [
        (
            "PropertyQuarry trust-candidate drop requires repair."
            if import_state == "external_artifact_not_admissible"
            else "PropertyQuarry public trust-candidate artifact required."
        ),
        f"Generated: {generated_at}",
        f"Request ID: {str(report.get('request_id') or '')}",
        f"State: {import_state}",
        (
            "Lanes: "
            + ",".join(
                str(value)
                for value in list(artifact.get("requested_lanes") or [])
            )
        ),
        f"Candidate schema: {str(artifact.get('candidate_schema') or '')}",
        "",
    ]
    if import_state == "recovery_required":
        lines.extend(
            [
                f"Import claim: {str(report.get('claim_id') or '')}",
                (
                    "Candidate fingerprint: sha256:"
                    f"{str(report.get('public_key_sha256') or '')}"
                ),
                (
                    "Safe next action: inspect the immutable candidate-import "
                    "claim and result receipts and compare their hashes before "
                    "deciding whether a separate retry should be authorized."
                ),
                (
                    "Do not delete the claim, overwrite the candidate, or retry "
                    "the import automatically."
                ),
            ]
        )
    elif import_state == "ready_for_manual_import":
        lines.extend(
            [
                (
                    "Candidate fingerprint: sha256:"
                    f"{str(report.get('public_key_sha256') or '')}"
                ),
                (
                    "Safe next action: use the operator summary's exact "
                    "inspect command, compare both hashes, then invoke the "
                    "confirmation-gated public-candidate import."
                ),
            ]
        )
    elif import_state == "external_artifact_not_admissible":
        repair = manual_action._inadmissible_repair_context(report)
        lines.extend(
            [
                f"Repair state: {str(repair['discovery_state'])}",
                (
                    "Observed entries: "
                    f"scanned={int(repair['scanned_file_count'])} "
                    f"valid={int(repair['valid_candidate_count'])} "
                    f"invalid={int(repair['invalid_candidate_count'])}"
                ),
                f"Safe next action: {str(repair['safe_next_step'])}",
                (
                    "No entry names, host paths, candidate payloads, or key "
                    "material are included in this alert."
                ),
            ]
        )
    else:
        lines.extend(
            [
                (
                    "Safe next action: place one public-only JSON candidate "
                    "in the configured operator-managed PropertyQuarry trust "
                    "candidate drop; use the operator summary for the exact "
                    "local folder and inspection command."
                ),
                "Accepted file pattern: *.json; directory mode 0750; file mode 0640.",
            ]
        )
    lines.extend(
        [
            "Never provide or include the producer private key.",
            (
                "Consent gate: this alert grants no import, trust enrollment, "
                "registry edit, provider, artifact delivery, deployment, "
                "restart, or execution authority."
            ),
        ]
    )
    message = "\n".join(lines)
    if _contains_absolute_path(message):
        raise ValueError("trust_candidate_artifact_notification_path_leak")
    return message


def _persist(report: Mapping[str, Any], *, receipt_path: Path) -> dict[str, Any]:
    target = Path(receipt_path).absolute()
    payload = _with_integrity(report)
    if _contains_absolute_path(payload):
        raise ValueError("trust_candidate_artifact_notification_path_leak")
    try:
        atomic_write_bytes(target, _canonical(payload), overwrite=True)
        persisted, raw, digest = candidate_import._private_object(
            target,
            field="trust_candidate_artifact_notification",
        )
        if persisted != payload or not _integrity_verified(persisted):
            raise ValueError(
                "trust_candidate_artifact_notification_persistence_mismatch"
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
                    "trust_candidate_artifact_notification_persistence_failed"
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


def run_candidate_artifact_notification(
    report: Mapping[str, Any],
    artifact_verification: Mapping[str, Any],
    action_verification: Mapping[str, Any],
    *,
    presentation_state_path: Path = DEFAULT_PRESENTATION_STATE_PATH,
    manual_action_receipt_path: Path = DEFAULT_MANUAL_ACTION_RECEIPT_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    principal_id: str = notification_cycle.DEFAULT_PRINCIPAL_ID,
    base_url: str = notification_cycle.DEFAULT_BASE_URL,
    send: bool = False,
    now: datetime | None = None,
    deliver: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate or deliver one current, deduplicated external-artifact ask."""

    observed_now = _now(now)
    execution_mode = "send" if send else "evaluate_only"
    if not _source_admissible(
        report,
        artifact_verification,
        action_verification,
        now=observed_now,
    ):
        return _persist(
            _blocked(
                "trust_candidate_artifact_notification_source_not_admissible",
                now=observed_now,
                execution_mode=execution_mode,
            ),
            receipt_path=receipt_path,
        )
    staged = action_verification.get("operator_action_receipt_staged") is True
    if not staged:
        result = _base_report(
            status="not_required",
            now=observed_now,
            execution_mode=execution_mode,
        )
        _bind_action(result, action_verification)
        return _persist(result, receipt_path=receipt_path)
    result = _base_report(
        status="action_required",
        now=observed_now,
        execution_mode=execution_mode,
    )
    _bind_action(result, action_verification)
    result.update(
        {
            "next_action": str(report.get("next_action") or ""),
            "action_required": True,
            "interrupt_operator": action_verification.get(
                "interrupt_operator"
            )
            is True,
            "would_send": action_verification.get("interrupt_operator")
            is True,
        }
    )
    if not result["interrupt_operator"]:
        result.update(
            {
                "status": "deduplicated",
                "would_send": False,
                "next_action": (
                    "await the external public candidate or the bounded reminder"
                ),
            }
        )
        return _persist(result, receipt_path=receipt_path)
    if not send:
        result["message_preview"] = _message(
            report,
            generated_at=result["generated_at"],
        )
        return _persist(result, receipt_path=receipt_path)
    if not str(principal_id or "").strip():
        result.update(
            {
                "status": "blocked",
                "blocking_reason": "delivery_authority_incomplete",
                "interrupt_operator": False,
                "would_send": False,
            }
        )
        return _persist(result, receipt_path=receipt_path)
    result["delivery_authorized"] = True
    try:
        lock_descriptor = notification_cycle._acquire_send_lock(
            Path(lock_path).absolute()
        )
    except Exception as exc:
        result.update(
            {
                "status": "send_lock_unavailable",
                "blocking_reason": type(exc).__name__,
                "interrupt_operator": False,
                "would_send": False,
            }
        )
        return _persist(result, receipt_path=receipt_path)
    if lock_descriptor is None:
        result.update(
            {
                "status": "send_cycle_busy",
                "blocking_reason": "send_cycle_busy",
                "interrupt_operator": False,
                "would_send": False,
            }
        )
        return _persist(result, receipt_path=receipt_path)
    try:
        result["delivery_attempted"] = True
        delivery_function = deliver or gold_notify.deliver_notification_for_principal
        try:
            delivery = delivery_function(
                principal_id=str(principal_id or "").strip(),
                text=_message(report, generated_at=result["generated_at"]),
                url_buttons=[[("Open PropertyQuarry", str(base_url or "").strip())]],
            )
        except Exception as exc:
            result.update(
                {
                    "status": "delivery_failed",
                    "blocking_reason": type(exc).__name__,
                    "interrupt_operator": False,
                    "would_send": False,
                }
            )
            return _persist(result, receipt_path=receipt_path)
        delivery_mode = str(delivery.get("delivery_mode") or "").strip()
        message_ids = [
            str(value)
            for value in list(delivery.get("message_ids") or [])
            if str(value or "").strip()
        ]
        if not delivery_mode or not message_ids:
            result.update(
                {
                    "status": "delivery_failed",
                    "blocking_reason": "delivery_receipt_incomplete",
                    "interrupt_operator": False,
                    "would_send": False,
                }
            )
            return _persist(result, receipt_path=receipt_path)
        presentation_digest = str(
            action_verification.get("presentation_digest") or ""
        )
        presentation_receipt = candidate_import.record_candidate_import_presentation(
            report,
            state_path=presentation_state_path,
            expected_presentation_digest=presentation_digest,
            now=observed_now,
        )
        if presentation_receipt.get("status") not in {"recorded", "unchanged"}:
            result.update(
                {
                    "status": "delivery_unrecorded",
                    "blocking_reason": str(
                        presentation_receipt.get("blocking_reason")
                        or "artifact_action_presentation_not_recorded"
                    ),
                    "interrupt_operator": False,
                    "sent": True,
                    "would_send": False,
                    "delivery_mode": delivery_mode,
                    "message_ids": message_ids,
                }
            )
            return _persist(result, receipt_path=receipt_path)
        refreshed_report = candidate_import.apply_candidate_import_presentation_state(
            report,
            state_path=presentation_state_path,
            now=observed_now,
        )
        refreshed_action = manual_action.materialize_candidate_manual_action(
            refreshed_report,
            artifact_verification,
            receipt_path=manual_action_receipt_path,
            now=observed_now,
        )
        if not (
            refreshed_action.get("status") == "verified"
            and refreshed_action.get("interrupt_operator") is False
            and refreshed_action.get("receipt_persisted") is True
        ):
            result.update(
                {
                    "status": "delivery_unrecorded",
                    "blocking_reason": "manual_action_refresh_failed",
                    "interrupt_operator": False,
                    "sent": True,
                    "would_send": False,
                    "delivery_mode": delivery_mode,
                    "message_ids": message_ids,
                }
            )
            return _persist(result, receipt_path=receipt_path)
        _bind_action(result, refreshed_action)
        result.update(
            {
                "status": "completed",
                "action_required": True,
                "interrupt_operator": False,
                "delivery_authorized": True,
                "delivery_attempted": True,
                "sent": True,
                "would_send": False,
                "delivery_mode": delivery_mode,
                "message_ids": message_ids,
                "presentation_recorded": True,
                "next_action": (
                    "await the external public candidate or the bounded reminder"
                ),
            }
        )
        return _persist(result, receipt_path=receipt_path)
    finally:
        notification_cycle._release_send_lock(lock_descriptor)


def verify_candidate_artifact_notification(
    receipt: Mapping[str, Any],
    *,
    report: Mapping[str, Any],
    artifact_verification: Mapping[str, Any],
    action_verification: Mapping[str, Any],
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    generated_at = candidate_import._timestamp(receipt.get("generated_at"))
    age_seconds = (
        (observed_now - generated_at).total_seconds()
        if generated_at is not None
        else math.inf
    )
    status = str(receipt.get("status") or "")
    staged = action_verification.get("operator_action_receipt_staged") is True
    current_interrupt = action_verification.get("interrupt_operator") is True
    expected_statuses = (
        {"not_required"}
        if not staged
        else {"action_required"}
        if current_interrupt
        else {"deduplicated", "completed"}
    )
    expected_binding: dict[str, Any] = {}
    _bind_action(expected_binding, action_verification)
    try:
        expected_message_preview = (
            _message(report, generated_at=str(receipt.get("generated_at") or ""))
            if status == "action_required"
            else None
        )
    except (TypeError, ValueError):
        expected_message_preview = None
    if not (
        frozenset(receipt) == _expected_receipt_keys(status)
        and receipt.get("schema") == SCHEMA
        and generated_at is not None
        and math.isfinite(age_seconds)
        and -30.0 <= age_seconds <= max_age_seconds
        and _integrity_verified(receipt)
        and not _contains_absolute_path(receipt)
        and status in _HEALTHY_STATUSES
        and status in expected_statuses
        and receipt.get("notification_policy") == "genuine_action_only"
        and receipt.get("blocking_reason") == ""
        and receipt.get("next_action")
        == _expected_next_action(status, report)
        and receipt.get("execution_mode") in {"evaluate_only", "send"}
        and (
            status != "action_required"
            or receipt.get("execution_mode") == "evaluate_only"
        )
        and (
            status != "completed"
            or receipt.get("execution_mode") == "send"
        )
        and _source_admissible(
            report,
            artifact_verification,
            action_verification,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        and all(receipt.get(key) == value for key, value in expected_binding.items())
        and ("message_preview" in receipt)
        is (status == "action_required")
        and (
            status != "action_required"
            or receipt.get("message_preview") == expected_message_preview
        )
        and (receipt.get("action_required") is True) is staged
        and (receipt.get("interrupt_operator") is True)
        is (status == "action_required")
        and (receipt.get("would_send") is True)
        is (status == "action_required")
        and (receipt.get("delivery_authorized") is True)
        is (status == "completed")
        and (receipt.get("delivery_attempted") is True)
        is (status == "completed")
        and (receipt.get("sent") is True) is (status == "completed")
        and (receipt.get("presentation_recorded") is True)
        is (status == "completed")
        and bool(str(receipt.get("delivery_mode") or "").strip())
        is (status == "completed")
        and bool(list(receipt.get("message_ids") or []))
        is (status == "completed")
        and _safety_fields_admissible(receipt)
        and receipt.get("receipt_persisted") is True
    ):
        return {
            **_blocked(
                "trust_candidate_artifact_notification_not_current",
                now=observed_now,
                execution_mode="inspect",
            ),
            "schema": VERIFY_SCHEMA,
        }
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "notification_status": status,
        "updated_at": observed_now.isoformat(),
        "generated_at": generated_at.isoformat(),
        "action_state": str(receipt.get("action_state") or ""),
        "request_id": str(receipt.get("request_id") or ""),
        "operator_action_receipt_sha256": str(
            receipt.get("operator_action_receipt_sha256") or ""
        ),
        "action_required": receipt.get("action_required") is True,
        "interrupt_operator": receipt.get("interrupt_operator") is True,
        "delivery_authorized": receipt.get("delivery_authorized") is True,
        "delivery_attempted": receipt.get("delivery_attempted") is True,
        "sent": receipt.get("sent") is True,
        "presentation_recorded": receipt.get("presentation_recorded") is True,
        "receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "notification_integrity_verified": True,
            "artifact_request_binding_verified": True,
            "operator_action_binding_verified": True,
            "presentation_binding_verified": True,
            "path_free_projection_verified": True,
        },
        **_safety_fields(),
    }


def inspect_candidate_artifact_notification(
    report: Mapping[str, Any],
    artifact_verification: Mapping[str, Any],
    action_verification: Mapping[str, Any],
    *,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        receipt, raw, digest = candidate_import._private_object(
            Path(receipt_path).absolute(),
            field="trust_candidate_artifact_notification",
        )
        verification = verify_candidate_artifact_notification(
            receipt,
            report=report,
            artifact_verification=artifact_verification,
            action_verification=action_verification,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
    except Exception:
        return {
            **_blocked(
                "trust_candidate_artifact_notification_unavailable",
                now=observed_now,
                execution_mode="inspect",
            ),
            "schema": VERIFY_SCHEMA,
        }
    if verification.get("status") == "verified":
        verification.update(
            {
                "notification_receipt_sha256": digest,
                "notification_receipt_bytes": len(raw),
            }
        )
    return verification


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--evaluate-current", action="store_true")
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
    parser.add_argument("--candidate-dir", type=Path, default=trust_intake.DEFAULT_CANDIDATE_DIR)
    parser.add_argument("--intake-receipt", type=Path, default=trust_intake.DEFAULT_RECEIPT_PATH)
    parser.add_argument("--intake-verification", type=Path, default=trust_intake.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--import-dir", type=Path, default=candidate_import.DEFAULT_IMPORT_DIR)
    parser.add_argument("--source-discovery-dir", type=Path, default=candidate_import.DEFAULT_SOURCE_DISCOVERY_DIR)
    parser.add_argument("--presentation-state", type=Path, default=DEFAULT_PRESENTATION_STATE_PATH)
    parser.add_argument("--artifact-request-receipt", type=Path, default=artifact_request.DEFAULT_RECEIPT_PATH)
    parser.add_argument("--manual-action-receipt", type=Path, default=DEFAULT_MANUAL_ACTION_RECEIPT_PATH)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--max-age-seconds", type=float, default=DEFAULT_MAX_AGE_SECONDS)
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
        artifact = artifact_request.inspect_candidate_artifact_request(
            report,
            receipt_path=args.artifact_request_receipt,
            max_age_seconds=args.max_age_seconds,
        )
        action = manual_action.inspect_candidate_manual_action(
            projected,
            artifact,
            receipt_path=args.manual_action_receipt,
            max_age_seconds=args.max_age_seconds,
        )
        result = (
            run_candidate_artifact_notification(
                projected,
                artifact,
                action,
                presentation_state_path=args.presentation_state,
                manual_action_receipt_path=args.manual_action_receipt,
                receipt_path=args.receipt,
                send=False,
            )
            if args.evaluate_current
            else inspect_candidate_artifact_notification(
                projected,
                artifact,
                action,
                receipt_path=args.receipt,
                max_age_seconds=args.max_age_seconds,
            )
        )
    except Exception:
        result = {
            **_blocked(
                "trust_candidate_artifact_notification_not_current",
                now=_now(),
                execution_mode="inspect",
            ),
            "schema": VERIFY_SCHEMA,
        }
    sys.stdout.buffer.write(_canonical(result) + b"\n")
    return 0 if result.get("status") in _HEALTHY_STATUSES | {"verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
