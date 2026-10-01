#!/usr/bin/env python3
"""Bind scheduler container posture and OODA iteration proof into one receipt."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts import propertyquarry_ooda_scheduler_witness as scheduler_witness
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


SCHEMA = "propertyquarry.ooda_scheduler_continuity.v1"
VERIFY_SCHEMA = "propertyquarry.ooda_scheduler_continuity_verification.v1"
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/scheduler-continuity.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/scheduler-continuity-verification.json"
)
DEFAULT_SOURCE_MAX_AGE_SECONDS = 300.0
DEFAULT_RECEIPT_MAX_AGE_SECONDS = 300.0
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_TYPE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


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


def _count(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field}_not_admissible")
    if value < 0 or value > 2**31 - 1:
        raise ValueError(f"{field}_not_admissible")
    return value


def _safe_reason(value: object, *, default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if _REASON.fullmatch(normalized) else default


def _runtime_projection(
    runtime: Mapping[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    progress = runtime.get("progress")
    if not isinstance(progress, Mapping):
        raise ValueError("scheduler_continuity_runtime_not_admissible")
    updated_at = _parse_fresh_timestamp(
        runtime.get("updated_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    observed_at = _parse_fresh_timestamp(
        runtime.get("current_observed_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    scheduler_condition = str(runtime.get("scheduler_condition") or "").strip()
    scheduler_state = str(runtime.get("scheduler_state") or "").strip()
    scheduler_health = str(runtime.get("scheduler_health") or "").strip()
    scheduler_count = _count(
        progress.get("scheduler_container_count"),
        field="scheduler_container_count",
    )
    running_count = _count(
        progress.get("running_scheduler_container_count"),
        field="running_scheduler_container_count",
    )
    healthy_count = _count(
        progress.get("healthy_scheduler_container_count"),
        field="healthy_scheduler_container_count",
    )
    container_healthy = runtime.get("scheduler_container_healthy") is True
    digest = str(runtime.get("runtime_observation_sha256") or "").strip()
    if not (
        runtime.get("schema") == runtime_review.RUNTIME_OBSERVATION_VERIFY_SCHEMA
        and runtime.get("status") == "verified"
        and updated_at is not None
        and observed_at is not None
        and progress.get("current_evidence_verified") is True
        and _SHA256.fullmatch(digest)
        and runtime.get("scheduler_service")
        == runtime_review.PROPERTYQUARRY_SCHEDULER_SERVICE
        and scheduler_condition
        in {
            "absent",
            "running",
            "starting",
            "unhealthy",
            "not_running",
            "health_unverified",
            "ambiguous",
        }
        and runtime.get("persistent_reevaluation_running") is False
        and runtime.get("execution_authorized") is False
        and runtime.get("deployment_or_restart_authorized") is False
        and runtime.get("protected_operation_executed") is False
        and runtime.get("provider_quota_consumption_allowed") is False
        and runtime.get("delivery_authorized") is False
        and (scheduler_condition == "running") is container_healthy
        and (scheduler_condition == "absent") is (scheduler_count == 0)
        and running_count <= scheduler_count
        and healthy_count <= running_count
        and (
            not container_healthy
            or (scheduler_count, running_count, healthy_count) == (1, 1, 1)
        )
    ):
        raise ValueError("scheduler_continuity_runtime_not_admissible")
    return {
        "status": "verified",
        "updated_at": updated_at,
        "observed_at": observed_at,
        "sha256": digest,
        "scheduler_service": str(runtime.get("scheduler_service") or ""),
        "scheduler_condition": scheduler_condition,
        "scheduler_state": scheduler_state,
        "scheduler_health": scheduler_health,
        "scheduler_container_count": scheduler_count,
        "running_scheduler_container_count": running_count,
        "healthy_scheduler_container_count": healthy_count,
        "scheduler_container_healthy": container_healthy,
    }


def _witness_projection(
    witness: Mapping[str, Any] | None,
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    value = dict(witness or {})
    if not value:
        return {
            "status": "absent",
            "updated_at": "",
            "iteration_status": "",
            "iteration_witness_sha256": "",
            "cycle_receipt_sha256": "",
            "cycle_binding_verified": False,
            "persistent_reevaluation_verified": False,
            "blocking_reason": "scheduler_iteration_witness_absent",
            "source_delivery_authorized": False,
            "source_delivery_attempted": False,
            "source_sent": False,
        }
    progress = value.get("progress")
    if not isinstance(progress, Mapping):
        progress = {}
    if value.get("status") != "verified":
        return {
            "status": "blocked",
            "updated_at": str(value.get("updated_at") or ""),
            "iteration_status": str(value.get("iteration_status") or ""),
            "iteration_witness_sha256": "",
            "cycle_receipt_sha256": "",
            "cycle_binding_verified": False,
            "persistent_reevaluation_verified": False,
            "blocking_reason": str(
                _safe_reason(
                    value.get("blocking_reason"),
                    default="scheduler_iteration_witness_unavailable",
                )
            ),
            "source_delivery_authorized": False,
            "source_delivery_attempted": False,
            "source_sent": False,
        }
    updated_at = _parse_fresh_timestamp(
        value.get("updated_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    iteration_digest = str(
        value.get("iteration_witness_sha256") or ""
    ).strip()
    cycle_evidence = value.get("cycle_evidence")
    cycle_digest = (
        str(cycle_evidence.get("sha256") or "").strip()
        if isinstance(cycle_evidence, Mapping)
        else ""
    )
    cycle_binding_verified = progress.get("cycle_binding_verified") is True
    persistent_verified = (
        value.get("persistent_reevaluation_verified") is True
    )
    if not (
        value.get("schema") == scheduler_witness.VERIFY_SCHEMA
        and updated_at is not None
        and progress.get("current_evidence_verified") is True
        and _SHA256.fullmatch(iteration_digest)
        and value.get("execution_authorized") is False
        and value.get("deployment_or_restart_authorized") is False
        and value.get("protected_operation_executed") is False
        and value.get("provider_quota_consumption_allowed") is False
        and (
            not persistent_verified
            or (
                value.get("iteration_status") == "completed"
                and cycle_binding_verified
                and _SHA256.fullmatch(cycle_digest)
            )
        )
    ):
        raise ValueError("scheduler_continuity_witness_not_admissible")
    return {
        "status": "verified",
        "updated_at": updated_at,
        "iteration_status": str(value.get("iteration_status") or ""),
        "iteration_witness_sha256": iteration_digest,
        "cycle_receipt_sha256": cycle_digest,
        "cycle_binding_verified": cycle_binding_verified,
        "persistent_reevaluation_verified": persistent_verified,
        "blocking_reason": str(
            _safe_reason(
                value.get("iteration_blocking_reason"),
                default="",
            )
        ),
        "source_delivery_authorized": value.get("delivery_authorized") is True,
        "source_delivery_attempted": value.get("delivery_attempted") is True,
        "source_sent": value.get("sent") is True,
    }


def _continuity_state(
    runtime: Mapping[str, Any],
    witness: Mapping[str, Any],
) -> tuple[str, str, str]:
    condition = str(runtime.get("scheduler_condition") or "")
    if condition == "absent":
        if witness.get("status") == "verified":
            return (
                "degraded",
                "scheduler_witness_present_without_container",
                "reobserve the runtime and witness sources without starting or restarting anything",
            )
        return (
            "inactive",
            "scheduler_container_absent",
            "persistent reevaluation remains inactive; deployment or start requires separate explicit consent",
        )
    if runtime.get("scheduler_container_healthy") is not True:
        return (
            "degraded",
            f"scheduler_container_{condition}",
            "inspect the current scheduler container and iteration evidence; any restart remains separately consent-gated",
        )
    if witness.get("status") != "verified":
        return (
            "blocked",
            str(
                witness.get("blocking_reason")
                or "scheduler_iteration_witness_unavailable"
            ),
            "obtain a current cycle-bound scheduler iteration witness before claiming persistent reevaluation",
        )
    if witness.get("persistent_reevaluation_verified") is not True:
        return (
            "blocked",
            str(
                witness.get("blocking_reason")
                or "scheduler_iteration_not_completed"
            ),
            "wait for or repair one completed cycle-bound scheduler iteration without granting deployment, delivery, or provider authority",
        )
    return (
        "active",
        "",
        "continue periodic read-only continuity verification from fresh runtime and iteration receipts",
    )


def build_scheduler_continuity_receipt(
    *,
    runtime_observation: Mapping[str, Any],
    scheduler_iteration_witness: Mapping[str, Any] | None,
    source_type: str,
    now: datetime | None = None,
    source_max_age_seconds: float = DEFAULT_SOURCE_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    normalized_source_type = str(source_type or "").strip()
    if _SOURCE_TYPE.fullmatch(normalized_source_type) is None:
        raise ValueError("scheduler_continuity_source_type_not_admissible")
    runtime = _runtime_projection(
        runtime_observation,
        now=observed_now,
        max_age_seconds=source_max_age_seconds,
    )
    witness = _witness_projection(
        scheduler_iteration_witness,
        now=observed_now,
        max_age_seconds=source_max_age_seconds,
    )
    state, blocking_reason, next_action = _continuity_state(runtime, witness)
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": state,
        "updated_at": observed_now.isoformat(),
        "blocking_reason": blocking_reason,
        "next_action": next_action,
        "action_required": False,
        "interrupt_operator": False,
        "source_type": normalized_source_type,
        "source_evidence": {
            "runtime_observation": runtime,
            "scheduler_iteration_witness": witness,
        },
        "progress": {
            "runtime_observation_current": True,
            "scheduler_container_healthy": runtime[
                "scheduler_container_healthy"
            ],
            "scheduler_iteration_witness_current": (
                witness["status"] == "verified"
            ),
            "cycle_binding_verified": witness[
                "cycle_binding_verified"
            ],
            "current_evidence_verified": state != "blocked",
        },
        "persistent_reevaluation_verified": state == "active",
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "receipt_persisted": True,
        "secret_values_recorded": False,
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(receipt)),
    }
    return receipt


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "continuity_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": "regenerate the continuity receipt from fresh runtime and scheduler iteration evidence",
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


def verify_scheduler_continuity_receipt(
    receipt: Mapping[str, Any],
    *,
    runtime_observation: Mapping[str, Any],
    scheduler_iteration_witness: Mapping[str, Any] | None,
    now: datetime | None = None,
    receipt_max_age_seconds: float = DEFAULT_RECEIPT_MAX_AGE_SECONDS,
    source_max_age_seconds: float = DEFAULT_SOURCE_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    updated_at = _parse_fresh_timestamp(
        receipt.get("updated_at"),
        now=observed_now,
        max_age_seconds=receipt_max_age_seconds,
    )
    if updated_at is None:
        return _blocked("scheduler_continuity_receipt_not_fresh", now=observed_now)
    normalized = dict(receipt)
    integrity = normalized.pop("integrity", None)
    if not (
        isinstance(integrity, dict)
        and integrity.get("algorithm") == "sha256"
        and _SHA256.fullmatch(str(integrity.get("canonical_payload_sha256") or ""))
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    ):
        return _blocked(
            "scheduler_continuity_receipt_integrity_invalid",
            now=observed_now,
        )
    try:
        rebuilt = build_scheduler_continuity_receipt(
            runtime_observation=runtime_observation,
            scheduler_iteration_witness=scheduler_iteration_witness,
            source_type=str(receipt.get("source_type") or ""),
            now=datetime.fromisoformat(updated_at),
            source_max_age_seconds=source_max_age_seconds,
        )
    except (TypeError, ValueError):
        return _blocked(
            "scheduler_continuity_source_evidence_not_admissible",
            now=observed_now,
        )
    if dict(receipt) != rebuilt:
        return _blocked(
            "scheduler_continuity_source_binding_mismatch",
            now=observed_now,
        )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "continuity_state": str(receipt.get("status") or ""),
        "updated_at": observed_now.isoformat(),
        "continuity_observed_at": updated_at,
        "continuity_receipt_sha256": _sha256(_canonical(receipt)),
        "blocking_reason": str(receipt.get("blocking_reason") or ""),
        "next_action": str(receipt.get("next_action") or ""),
        "source_evidence": dict(receipt.get("source_evidence") or {}),
        "action_required": False,
        "interrupt_operator": False,
        "progress": {
            **dict(receipt.get("progress") or {}),
            "receipt_integrity_verified": True,
        },
        "persistent_reevaluation_verified": (
            receipt.get("persistent_reevaluation_verified") is True
        ),
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def materialize_current_scheduler_continuity_bundle(
    *,
    runtime_observation: Mapping[str, Any],
    scheduler_iteration_witness: Mapping[str, Any] | None,
    source_type: str,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    source_max_age_seconds: float = DEFAULT_SOURCE_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    receipt = build_scheduler_continuity_receipt(
        runtime_observation=runtime_observation,
        scheduler_iteration_witness=scheduler_iteration_witness,
        source_type=source_type,
        now=observed_now,
        source_max_age_seconds=source_max_age_seconds,
    )
    atomic_write_bytes(
        Path(receipt_path).absolute(),
        _canonical(receipt),
        overwrite=True,
    )
    verification = verify_scheduler_continuity_receipt(
        receipt,
        runtime_observation=runtime_observation,
        scheduler_iteration_witness=scheduler_iteration_witness,
        now=observed_now,
        source_max_age_seconds=source_max_age_seconds,
    )
    verification["receipt_path"] = str(Path(receipt_path).absolute())
    verification["verification_path"] = str(
        Path(verification_path).absolute()
    )
    verification["verification_receipt_persisted"] = True
    try:
        atomic_write_bytes(
            Path(verification_path).absolute(),
            _canonical(verification),
            overwrite=True,
        )
    except Exception:
        verification["verification_receipt_persisted"] = False
        verification.update(
            {
                "status": "blocked",
                "continuity_state": "blocked",
                "blocking_reason": (
                    "scheduler_continuity_verification_persistence_failed"
                ),
                "persistent_reevaluation_verified": False,
            }
        )
        progress = dict(verification.get("progress") or {})
        progress["current_evidence_verified"] = False
        verification["progress"] = progress
    return verification
