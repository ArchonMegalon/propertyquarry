#!/usr/bin/env python3
"""Persist and verify a cycle-bound PropertyQuarry scheduler iteration witness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_notification_cycle as cycle
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_scheduler_iteration.v1"
VERIFY_SCHEMA = "propertyquarry.ooda_scheduler_iteration_verification.v1"
DEFAULT_RECEIPT_PATH = Path(
    "/data/artifacts/propertyquarry-ooda-notification/scheduler-iteration.json"
)
DEFAULT_CYCLE_RECEIPT_PATH = Path(
    "/data/artifacts/propertyquarry-ooda-notification/latest.json"
)
DEFAULT_MAX_AGE_SECONDS = 1800.0
MAX_RECEIPT_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_STATUS = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ERROR_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,127}\Z")
_RESULT_FIELDS = {
    "ran",
    "status",
    "deferred",
    "timeout",
    "shutdown",
    "running",
    "delivery_authorized",
    "delivery_attempted",
    "sent",
    "would_send",
    "action_required_count",
    "novel_action_count",
    "errors",
}
_CYCLE_EVIDENCE_FIELDS = {
    "schema",
    "generated_at",
    "status",
    "execution_mode",
    "sha256",
    "bytes",
    "signal_approval_reason",
}


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _environment_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except (TypeError, ValueError):
        value = default
    return value


def configured_max_age_seconds() -> float:
    interval = max(
        60.0,
        _environment_float(
            "EA_SCHEDULER_PROPERTYQUARRY_OODA_INTERVAL_SECONDS",
            900.0,
        ),
    )
    return max(
        60.0,
        min(
            _environment_float(
                "EA_SCHEDULER_PROPERTYQUARRY_OODA_WITNESS_MAX_AGE_SECONDS",
                max(DEFAULT_MAX_AGE_SECONDS, interval * 2.0),
            ),
            86400.0,
        ),
    )


def _canonical(value: Mapping[str, Any]) -> bytes:
    return approved.canonical_json_bytes(dict(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_fresh_timestamp(
    value: object,
    *,
    now: datetime,
    max_age_seconds: float,
) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    normalized = parsed.astimezone(timezone.utc)
    age = (now - normalized).total_seconds()
    if age < 0 or age > float(max_age_seconds):
        return None
    return normalized.isoformat()


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


def _non_negative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field}_not_admissible")
    try:
        normalized = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_not_admissible") from exc
    if normalized < 0 or normalized > 2**31 - 1:
        raise ValueError(f"{field}_not_admissible")
    return normalized


def normalize_iteration_result(summary: Mapping[str, Any]) -> dict[str, Any]:
    status = str(summary.get("status") or "unknown").strip().lower()
    if _STATUS.fullmatch(status) is None:
        raise ValueError("scheduler_iteration_status_not_admissible")
    result: dict[str, Any] = {
        "ran": summary.get("ran") is True,
        "status": status,
        "deferred": summary.get("deferred") is True,
        "timeout": summary.get("timeout") is True,
        "shutdown": summary.get("shutdown") is True,
        "running": summary.get("running") is True,
        "delivery_authorized": summary.get("delivery_authorized") is True,
        "delivery_attempted": summary.get("delivery_attempted") is True,
        "sent": summary.get("sent") is True,
        "would_send": summary.get("would_send") is True,
        "action_required_count": _non_negative_int(
            summary.get("action_required_count"),
            field="action_required_count",
        ),
        "novel_action_count": _non_negative_int(
            summary.get("novel_action_count"),
            field="novel_action_count",
        ),
        "errors": _non_negative_int(summary.get("errors"), field="errors"),
    }
    if result["sent"] and not (
        result["delivery_authorized"] and result["delivery_attempted"]
    ):
        raise ValueError("scheduler_iteration_delivery_contract_not_admissible")
    if result["delivery_attempted"] and not result["delivery_authorized"]:
        raise ValueError("scheduler_iteration_delivery_contract_not_admissible")
    return result


def cycle_receipt_evidence(
    path: Path,
    *,
    result: Mapping[str, Any],
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    payload, raw, digest = _private_snapshot(path, field="scheduler_cycle_receipt")
    generated_at = _parse_fresh_timestamp(
        payload.get("generated_at"),
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    approval = payload.get("signal_approval")
    approval_reason = (
        str(approval.get("reason") or "").strip()
        if isinstance(approval, Mapping)
        else ""
    )
    if not (
        payload.get("schema") == cycle.SCHEMA
        and generated_at is not None
        and payload.get("status") == result.get("status")
        and payload.get("delivery_authorized")
        is result.get("delivery_authorized")
        and payload.get("delivery_attempted")
        is result.get("delivery_attempted")
        and payload.get("sent") is result.get("sent")
        and payload.get("would_send") is result.get("would_send")
        and int(payload.get("action_required_count") or 0)
        == result.get("action_required_count")
        and int(payload.get("novel_action_count") or 0)
        == result.get("novel_action_count")
        and payload.get("execution_mode") in {"evaluate_only", "send"}
        and approval_reason
    ):
        raise ValueError("scheduler_cycle_receipt_not_bound")
    return {
        "schema": cycle.SCHEMA,
        "generated_at": generated_at,
        "status": str(payload.get("status") or ""),
        "execution_mode": str(payload.get("execution_mode") or ""),
        "sha256": digest,
        "bytes": len(raw),
        "signal_approval_reason": approval_reason,
    }


def _iteration_outcome(
    result: Mapping[str, Any],
    *,
    cycle_bound: bool,
    error_type: str,
) -> str:
    if error_type:
        return "failed"
    if result.get("shutdown") is True:
        return "stopped"
    if result.get("timeout") is True:
        return "timeout"
    if result.get("deferred") is True:
        return "deferred"
    if (
        result.get("ran") is True
        and cycle_bound
        and int(result.get("errors") or 0) == 0
    ):
        return "completed"
    return "blocked"


def build_scheduler_iteration_receipt(
    summary: Mapping[str, Any],
    *,
    cycle_evidence: Mapping[str, Any] | None = None,
    error_type: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = _now(now).isoformat()
    result = normalize_iteration_result(summary)
    normalized_cycle = dict(cycle_evidence or {})
    if normalized_cycle and not (
        set(normalized_cycle) == _CYCLE_EVIDENCE_FIELDS
        and normalized_cycle.get("schema") == cycle.SCHEMA
        and _SHA256.fullmatch(str(normalized_cycle.get("sha256") or ""))
        and isinstance(normalized_cycle.get("bytes"), int)
        and not isinstance(normalized_cycle.get("bytes"), bool)
        and int(normalized_cycle.get("bytes") or 0) > 0
        and normalized_cycle.get("execution_mode") in {"evaluate_only", "send"}
        and normalized_cycle.get("status") == result["status"]
        and str(normalized_cycle.get("generated_at") or "").strip()
        and str(normalized_cycle.get("signal_approval_reason") or "").strip()
    ):
        raise ValueError("scheduler_cycle_evidence_not_admissible")
    normalized_error_type = str(error_type or "").strip()
    if normalized_error_type and _ERROR_TYPE.fullmatch(normalized_error_type) is None:
        raise ValueError("scheduler_iteration_error_type_not_admissible")
    outcome = _iteration_outcome(
        result,
        cycle_bound=bool(normalized_cycle),
        error_type=normalized_error_type,
    )
    blocking_reason = {
        "completed": "",
        "deferred": "scheduler_step_deferred",
        "timeout": "scheduler_step_timeout",
        "stopped": "scheduler_shutdown_observed",
        "failed": "scheduler_iteration_exception",
        "blocked": result["status"],
    }[outcome]
    next_action = (
        "await the next bounded PropertyQuarry OODA scheduler iteration"
        if outcome == "completed"
        else "inspect the current scheduler iteration and cycle receipts without starting, restarting, sending, or changing protected configuration"
    )
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": outcome,
        "updated_at": observed_at,
        "blocking_reason": blocking_reason,
        "next_action": next_action,
        "action_required": False,
        "interrupt_operator": False,
        "result": result,
        "cycle_evidence": normalized_cycle,
        "error_type": normalized_error_type,
        "progress": {
            "iteration_attempted": True,
            "iteration_completed": bool(normalized_cycle),
            "cycle_receipt_bound": bool(normalized_cycle),
            "current_evidence_verified": False,
        },
        "persistent_reevaluation_verified": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": result["delivery_authorized"],
        "delivery_attempted": result["delivery_attempted"],
        "sent": result["sent"],
        "receipt_persisted": True,
        "secret_values_recorded": False,
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(receipt)),
    }
    return receipt


def persist_scheduler_iteration_receipt(
    summary: Mapping[str, Any],
    *,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    cycle_receipt_path: Path = DEFAULT_CYCLE_RECEIPT_PATH,
    error_type: str = "",
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    result = normalize_iteration_result(summary)
    evidence: dict[str, Any] = {}
    if (
        result["ran"]
        and not result["timeout"]
        and not result["deferred"]
        and not result["shutdown"]
        and not error_type
    ):
        evidence = cycle_receipt_evidence(
            cycle_receipt_path,
            result=result,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
    receipt = build_scheduler_iteration_receipt(
        result,
        cycle_evidence=evidence,
        error_type=error_type,
        now=observed_now,
    )
    atomic_write_bytes(
        Path(receipt_path).absolute(),
        _canonical(receipt),
        overwrite=True,
    )
    return receipt


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": "obtain a fresh private scheduler iteration witness without starting, restarting, sending, or changing protected configuration",
        "action_required": False,
        "interrupt_operator": False,
        "progress": {"current_evidence_verified": False},
        "persistent_reevaluation_verified": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def verify_scheduler_iteration_receipt(
    receipt: Mapping[str, Any],
    *,
    cycle_receipt_path: Path = DEFAULT_CYCLE_RECEIPT_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    updated_at = _parse_fresh_timestamp(
        receipt.get("updated_at"),
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    if updated_at is None:
        return _blocked("scheduler_iteration_witness_not_fresh", now=observed_now)
    normalized = dict(receipt)
    integrity = normalized.pop("integrity", None)
    if not (
        isinstance(integrity, dict)
        and integrity.get("algorithm") == "sha256"
        and _SHA256.fullmatch(str(integrity.get("canonical_payload_sha256") or ""))
        and integrity.get("canonical_payload_sha256") == _sha256(_canonical(normalized))
    ):
        return _blocked("scheduler_iteration_witness_integrity_invalid", now=observed_now)
    result = receipt.get("result")
    cycle_evidence_value = receipt.get("cycle_evidence")
    if not isinstance(result, Mapping) or not isinstance(cycle_evidence_value, Mapping):
        return _blocked("scheduler_iteration_witness_contract_invalid", now=observed_now)
    try:
        rebuilt = build_scheduler_iteration_receipt(
            result,
            cycle_evidence=cycle_evidence_value,
            error_type=str(receipt.get("error_type") or ""),
            now=datetime.fromisoformat(updated_at),
        )
    except (TypeError, ValueError):
        return _blocked("scheduler_iteration_witness_contract_invalid", now=observed_now)
    if dict(receipt) != rebuilt:
        return _blocked("scheduler_iteration_witness_contract_invalid", now=observed_now)
    cycle_binding_verified = False
    if cycle_evidence_value:
        try:
            current_cycle_evidence = cycle_receipt_evidence(
                cycle_receipt_path,
                result=result,
                now=observed_now,
                max_age_seconds=max_age_seconds,
            )
        except (OSError, TypeError, ValueError):
            return _blocked("scheduler_iteration_cycle_binding_unavailable", now=observed_now)
        if dict(cycle_evidence_value) != current_cycle_evidence:
            return _blocked("scheduler_iteration_cycle_binding_mismatch", now=observed_now)
        cycle_binding_verified = True
    persistent_verified = bool(
        receipt.get("status") == "completed" and cycle_binding_verified
    )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": str(receipt.get("next_action") or ""),
        "iteration_status": str(receipt.get("status") or ""),
        "iteration_blocking_reason": str(
            receipt.get("blocking_reason") or ""
        ),
        "iteration_updated_at": updated_at,
        "iteration_witness_sha256": _sha256(_canonical(receipt)),
        "cycle_evidence": dict(cycle_evidence_value),
        "result": dict(result),
        "action_required": False,
        "interrupt_operator": False,
        "progress": {
            **dict(receipt.get("progress") or {}),
            "cycle_binding_verified": cycle_binding_verified,
            "current_evidence_verified": True,
        },
        "persistent_reevaluation_verified": persistent_verified,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": receipt.get("delivery_authorized") is True,
        "delivery_attempted": receipt.get("delivery_attempted") is True,
        "sent": receipt.get("sent") is True,
    }


def verify_current_scheduler_iteration(
    *,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    cycle_receipt_path: Path = DEFAULT_CYCLE_RECEIPT_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        receipt, _raw, _digest = _private_snapshot(
            receipt_path,
            field="scheduler_iteration_witness",
        )
    except Exception:
        return _blocked("scheduler_iteration_witness_file_not_admissible", now=observed_now)
    return verify_scheduler_iteration_receipt(
        receipt,
        cycle_receipt_path=cycle_receipt_path,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the current private PropertyQuarry OODA scheduler iteration witness."
    )
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument(
        "--cycle-receipt",
        type=Path,
        default=DEFAULT_CYCLE_RECEIPT_PATH,
    )
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=configured_max_age_seconds(),
    )
    args = parser.parse_args(argv)
    if not args.verify:
        parser.error("--verify is required")
    result = verify_current_scheduler_iteration(
        receipt_path=args.receipt,
        cycle_receipt_path=args.cycle_receipt,
        max_age_seconds=args.max_age_seconds,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
