#!/usr/bin/env python3
"""Execute one explicitly authorized trust-registry preview exactly once."""

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

from scripts import propertyquarry_ooda_source_refresh_claims as claims
from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_authorization as authorization
from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness as readiness
from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as preview
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts.propertyquarry_secure_file_io import (
    OutputExistsError,
    atomic_replace_bytes_if_matches,
    atomic_write_bytes,
    read_stable_bytes,
)
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


CLAIM_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_enrollment_execution_claim.v1"
)
RESULT_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_enrollment_execution_result.v1"
)
VERIFY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_enrollment_execution_verification.v1"
)
HISTORY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_enrollment_execution_history.v1"
)
DEFAULT_EXECUTION_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-enrollment-executions"
)
DEFAULT_BACKUP_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-enrollment-backups"
)
MAX_OBJECT_BYTES = 512 * 1024
MAX_HISTORY_ENTRIES = 512
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_READINESS_ID = re.compile(r"pqtrustready_[0-9a-f]{24}\Z")
_PREVIEW_ID = re.compile(r"pqtrustpreview_[0-9a-f]{24}\Z")
_CLAIM_ID = re.compile(r"pqtrustexec_[0-9a-f]{24}\Z")
_CLAIM_KEYS = frozenset(
    {
        "schema",
        "claim_id",
        "status",
        "claimed_at",
        "expires_at",
        "scope",
        "executor",
        "backup_path",
        "authorization_recorded",
        "authorization_consumed",
        "manual_execution_authorized",
        "automatic_execution_allowed",
        "execution_authorized",
        "trust_registry_write_attempted",
        "trust_registry_modified",
        "rollback_available",
        "deployment_or_restart_authorized",
        "protected_operation_executed",
        "provider_quota_consumption_allowed",
        "delivery_authorized",
        "delivery_attempted",
        "sent",
        "secret_values_recorded",
        "integrity",
    }
)
_CLAIM_SCOPE_KEYS = frozenset(
    {
        "readiness_id",
        "readiness_verification_sha256",
        "preview_id",
        "authorization_id",
        "authorization_receipt_sha256",
        "expected_current_trust_registry_sha256",
        "expected_proposed_trust_registry_sha256",
    }
)
_CLAIM_EXECUTOR_KEYS = frozenset(
    {
        "executor_id",
        "execution_method",
        "execution_evidence_ref",
        "explicit_invocation_recorded",
        "source",
    }
)
_RESULT_KEYS = frozenset(
    {
        "schema",
        "status",
        "completed_at",
        "blocking_reason",
        "claim_id",
        "claim_sha256",
        "readiness_id",
        "readiness_verification_sha256",
        "authorization_id",
        "authorization_receipt_sha256",
        "expected_current_trust_registry_sha256",
        "expected_proposed_trust_registry_sha256",
        "observed_trust_registry_sha256",
        "backup_path",
        "backup_sha256",
        "authorization_recorded",
        "authorization_consumed",
        "manual_execution_authorized",
        "automatic_execution_allowed",
        "execution_authorized",
        "trust_registry_write_attempted",
        "trust_registry_modified",
        "rollback_available",
        "deployment_or_restart_authorized",
        "protected_operation_executed",
        "provider_quota_consumption_allowed",
        "delivery_authorized",
        "delivery_attempted",
        "sent",
        "secret_values_recorded",
        "postcondition_verified",
        "integrity",
    }
)


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return preview._canonical(dict(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _timestamp(value: object) -> datetime | None:
    return preview._timestamp(value)


def _with_integrity(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
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
        and _SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    )


def _private_directory(path: Path, *, create: bool) -> Path:
    target = Path(path).absolute()
    if not target.exists() and create:
        target.mkdir(parents=True, mode=0o700)
    metadata = target.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("trust_enrollment_execution_directory_not_admissible")
    return target


def _private_object(
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
        raise ValueError(f"{field}_file_not_admissible")
    return load_strict_json_object_snapshot(
        target,
        field=field,
        maximum_bytes=MAX_OBJECT_BYTES,
    )


def _paths(
    execution_dir: Path,
    backup_dir: Path,
    readiness_id: str,
    current_registry_sha256: str,
) -> tuple[Path, Path, Path]:
    if not (
        _READINESS_ID.fullmatch(readiness_id)
        and _SHA256.fullmatch(current_registry_sha256)
    ):
        raise ValueError("trust_enrollment_execution_path_binding_invalid")
    execution_target = Path(execution_dir).absolute()
    backup_target = Path(backup_dir).absolute()
    return (
        execution_target / f"claim--{readiness_id}.json",
        execution_target / f"result--{readiness_id}.json",
        backup_target
        / f"backup--{readiness_id}--{current_registry_sha256}.json",
    )


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "execution_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "inspect current immutable execution history and obtain a fresh "
            "exact preview and authorization before any retry"
        ),
        "readiness_id": "",
        "claim_id": "",
        "action_required": False,
        "interrupt_operator": False,
        "authorization_recorded": False,
        "authorization_consumed": False,
        "manual_execution_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "trust_registry_write_attempted": False,
        "trust_registry_modified": False,
        "rollback_available": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {"current_evidence_verified": False},
        "actions": [],
    }


def _readiness_admissible(value: Mapping[str, Any]) -> bool:
    return bool(
        value.get("schema") == readiness.VERIFY_SCHEMA
        and value.get("status") == "verified"
        and value.get("readiness_state") == "ready_for_governed_execution"
        and _READINESS_ID.fullmatch(str(value.get("readiness_id") or ""))
        and _SHA256.fullmatch(
            str(value.get("verification_receipt_sha256") or "")
        )
        and _PREVIEW_ID.fullmatch(str(value.get("preview_id") or ""))
        and authorization._AUTHORIZATION_ID.fullmatch(
            str(value.get("authorization_id") or "")
        )
        and _SHA256.fullmatch(
            str(value.get("authorization_receipt_sha256") or "")
        )
        and _SHA256.fullmatch(
            str(value.get("current_trust_registry_sha256") or "")
        )
        and _SHA256.fullmatch(
            str(value.get("proposed_trust_registry_sha256") or "")
        )
        and value.get("authorization_decision") == "authorize_exact_preview"
        and value.get("explicit_authorization_recorded") is True
        and value.get("exact_preview_authorized") is True
        and value.get("trust_enrollment_authorized") is True
        and value.get("execution_request_staged") is True
        and value.get("execution_readiness_verified") is True
        and value.get("governed_execution_available") is True
        and value.get("action_required") is True
        and value.get("interrupt_operator") is False
        and value.get("operator_review_required") is True
        and value.get("trust_registry_modified") is False
        and value.get("automatic_execution_allowed") is False
        and value.get("execution_authorized") is False
        and value.get("protected_operation_executed") is False
        and value.get("provider_quota_consumption_allowed") is False
        and value.get("delivery_authorized") is False
        and dict(value.get("progress") or {}).get(
            "current_evidence_verified"
        )
        is True
        and dict(value.get("progress") or {}).get(
            "authorization_binding_verified"
        )
        is True
        and dict(value.get("progress") or {}).get("registry_binding_verified")
        is True
    )


def build_execution_claim(
    readiness_report: Mapping[str, Any],
    *,
    executor_id: str,
    execution_method: str,
    execution_evidence_ref: str,
    backup_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    claimed_at = _now(now)
    normalized_executor = str(executor_id or "").strip()
    normalized_method = str(execution_method or "").strip()
    normalized_evidence = str(execution_evidence_ref or "").strip()
    expires_at = _timestamp(readiness_report.get("expires_at"))
    if not (
        _readiness_admissible(readiness_report)
        and expires_at is not None
        and claimed_at <= expires_at
        and trust_decision._DECIDER_ID.fullmatch(normalized_executor)
        and normalized_method in authorization.AUTHORIZATION_METHODS
        and trust_decision._EVIDENCE_REF.fullmatch(normalized_evidence)
    ):
        raise ValueError("trust_enrollment_execution_claim_input_not_admissible")
    scope = {
        "readiness_id": str(readiness_report.get("readiness_id") or ""),
        "readiness_verification_sha256": str(
            readiness_report.get("verification_receipt_sha256") or ""
        ),
        "preview_id": str(readiness_report.get("preview_id") or ""),
        "authorization_id": str(
            readiness_report.get("authorization_id") or ""
        ),
        "authorization_receipt_sha256": str(
            readiness_report.get("authorization_receipt_sha256") or ""
        ),
        "expected_current_trust_registry_sha256": str(
            readiness_report.get("current_trust_registry_sha256") or ""
        ),
        "expected_proposed_trust_registry_sha256": str(
            readiness_report.get("proposed_trust_registry_sha256") or ""
        ),
    }
    executor = {
        "executor_id": normalized_executor,
        "execution_method": normalized_method,
        "execution_evidence_ref": normalized_evidence,
        "explicit_invocation_recorded": True,
        "source": "governed_operator_cli",
    }
    semantic = {"scope": scope, "executor": executor}
    claim_id = f"pqtrustexec_{_sha256(_canonical(semantic))[:24]}"
    return _with_integrity(
        {
            "schema": CLAIM_SCHEMA,
            "claim_id": claim_id,
            "status": "claimed",
            "claimed_at": claimed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "scope": scope,
            "executor": executor,
            "backup_path": str(Path(backup_path).absolute()),
            "authorization_recorded": True,
            "authorization_consumed": True,
            "manual_execution_authorized": True,
            "automatic_execution_allowed": False,
            "execution_authorized": True,
            "trust_registry_write_attempted": False,
            "trust_registry_modified": False,
            "rollback_available": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "secret_values_recorded": False,
        }
    )


def verify_execution_claim_history(
    claim: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    claimed_at = _timestamp(claim.get("claimed_at"))
    expires_at = _timestamp(claim.get("expires_at"))
    scope = claim.get("scope")
    executor = claim.get("executor")
    backup_path = Path(str(claim.get("backup_path") or ""))
    if not (
        frozenset(claim) == _CLAIM_KEYS
        and claim.get("schema") == CLAIM_SCHEMA
        and claim.get("status") == "claimed"
        and _integrity_verified(claim)
        and _CLAIM_ID.fullmatch(str(claim.get("claim_id") or ""))
        and claimed_at is not None
        and expires_at is not None
        and claimed_at <= expires_at
        and isinstance(scope, Mapping)
        and frozenset(scope) == _CLAIM_SCOPE_KEYS
        and isinstance(executor, Mapping)
        and frozenset(executor) == _CLAIM_EXECUTOR_KEYS
        and _READINESS_ID.fullmatch(str(scope.get("readiness_id") or ""))
        and _SHA256.fullmatch(
            str(scope.get("readiness_verification_sha256") or "")
        )
        and _PREVIEW_ID.fullmatch(str(scope.get("preview_id") or ""))
        and authorization._AUTHORIZATION_ID.fullmatch(
            str(scope.get("authorization_id") or "")
        )
        and _SHA256.fullmatch(
            str(scope.get("authorization_receipt_sha256") or "")
        )
        and _SHA256.fullmatch(
            str(scope.get("expected_current_trust_registry_sha256") or "")
        )
        and _SHA256.fullmatch(
            str(scope.get("expected_proposed_trust_registry_sha256") or "")
        )
        and trust_decision._DECIDER_ID.fullmatch(
            str(executor.get("executor_id") or "")
        )
        and executor.get("execution_method")
        in authorization.AUTHORIZATION_METHODS
        and trust_decision._EVIDENCE_REF.fullmatch(
            str(executor.get("execution_evidence_ref") or "")
        )
        and executor.get("explicit_invocation_recorded") is True
        and executor.get("source") == "governed_operator_cli"
        and backup_path.is_absolute()
        and backup_path.name
        == (
            f"backup--{scope.get('readiness_id')}--"
            f"{scope.get('expected_current_trust_registry_sha256')}.json"
        )
        and claim.get("authorization_recorded") is True
        and claim.get("authorization_consumed") is True
        and claim.get("manual_execution_authorized") is True
        and claim.get("automatic_execution_allowed") is False
        and claim.get("execution_authorized") is True
        and claim.get("trust_registry_write_attempted") is False
        and claim.get("trust_registry_modified") is False
        and claim.get("rollback_available") is False
        and claim.get("deployment_or_restart_authorized") is False
        and claim.get("protected_operation_executed") is False
        and claim.get("provider_quota_consumption_allowed") is False
        and claim.get("delivery_authorized") is False
        and claim.get("delivery_attempted") is False
        and claim.get("sent") is False
        and claim.get("secret_values_recorded") is False
    ):
        return _blocked(
            "trust_enrollment_execution_historical_claim_not_admissible",
            now=observed_now,
        )
    semantic = {"scope": dict(scope), "executor": dict(executor)}
    if claim.get("claim_id") != (
        f"pqtrustexec_{_sha256(_canonical(semantic))[:24]}"
    ):
        return _blocked(
            "trust_enrollment_execution_historical_claim_identity_invalid",
            now=observed_now,
        )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "execution_state": "claimed",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "claim_id": str(claim.get("claim_id") or ""),
        "claim_sha256": _sha256(_canonical(claim)),
        "claimed_at": claimed_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "readiness_id": str(scope.get("readiness_id") or ""),
        "readiness_verification_sha256": str(
            scope.get("readiness_verification_sha256") or ""
        ),
        "authorization_id": str(scope.get("authorization_id") or ""),
        "authorization_receipt_sha256": str(
            scope.get("authorization_receipt_sha256") or ""
        ),
        "expected_current_trust_registry_sha256": str(
            scope.get("expected_current_trust_registry_sha256") or ""
        ),
        "expected_proposed_trust_registry_sha256": str(
            scope.get("expected_proposed_trust_registry_sha256") or ""
        ),
        "executor_id": str(executor.get("executor_id") or ""),
        "backup_path": str(claim.get("backup_path") or ""),
        "authorization_recorded": True,
        "authorization_consumed": True,
        "manual_execution_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "trust_registry_write_attempted": False,
        "trust_registry_modified": False,
        "rollback_available": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "action_required": True,
        "interrupt_operator": False,
        "progress": {"execution_claim_integrity_verified": True},
        "actions": [],
    }


def verify_execution_claim(
    claim: Mapping[str, Any],
    *,
    readiness_report: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    claimed_at = _timestamp(claim.get("claimed_at"))
    historical = verify_execution_claim_history(claim, now=now)
    if claimed_at is None or historical.get("status") != "verified":
        return _blocked("trust_enrollment_execution_claim_invalid", now=now)
    try:
        executor = dict(claim.get("executor") or {})
        expected = build_execution_claim(
            readiness_report,
            executor_id=str(executor.get("executor_id") or ""),
            execution_method=str(executor.get("execution_method") or ""),
            execution_evidence_ref=str(
                executor.get("execution_evidence_ref") or ""
            ),
            backup_path=Path(str(claim.get("backup_path") or "")),
            now=claimed_at,
        )
    except (OSError, TypeError, ValueError):
        return _blocked(
            "trust_enrollment_execution_claim_source_not_admissible",
            now=now,
        )
    if dict(claim) != expected:
        return _blocked(
            "trust_enrollment_execution_claim_binding_mismatch",
            now=now,
        )
    return historical


def _build_result(
    *,
    claim: Mapping[str, Any],
    status: str,
    blocking_reason: str,
    backup_sha256: str,
    observed_registry_sha256: str,
    write_attempted: bool,
    registry_modified: bool,
    rollback_available: bool,
    now: datetime,
) -> dict[str, Any]:
    scope = dict(claim.get("scope") or {})
    succeeded = status == "succeeded"
    return _with_integrity(
        {
            "schema": RESULT_SCHEMA,
            "status": status,
            "completed_at": now.isoformat(),
            "blocking_reason": blocking_reason,
            "claim_id": str(claim.get("claim_id") or ""),
            "claim_sha256": _sha256(_canonical(claim)),
            "readiness_id": str(scope.get("readiness_id") or ""),
            "readiness_verification_sha256": str(
                scope.get("readiness_verification_sha256") or ""
            ),
            "authorization_id": str(scope.get("authorization_id") or ""),
            "authorization_receipt_sha256": str(
                scope.get("authorization_receipt_sha256") or ""
            ),
            "expected_current_trust_registry_sha256": str(
                scope.get("expected_current_trust_registry_sha256") or ""
            ),
            "expected_proposed_trust_registry_sha256": str(
                scope.get("expected_proposed_trust_registry_sha256") or ""
            ),
            "observed_trust_registry_sha256": observed_registry_sha256,
            "backup_path": str(claim.get("backup_path") or ""),
            "backup_sha256": backup_sha256,
            "authorization_recorded": True,
            "authorization_consumed": True,
            "manual_execution_authorized": False,
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "trust_registry_write_attempted": write_attempted,
            "trust_registry_modified": registry_modified,
            "rollback_available": rollback_available,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": registry_modified,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "secret_values_recorded": False,
            "postcondition_verified": succeeded,
        }
    )


def verify_execution_result(
    result: Mapping[str, Any],
    *,
    claim: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    completed_at = _timestamp(result.get("completed_at"))
    claimed_at = _timestamp(claim.get("claimed_at"))
    expires_at = _timestamp(claim.get("expires_at"))
    status_value = str(result.get("status") or "")
    scope = dict(claim.get("scope") or {})
    modified = result.get("trust_registry_modified") is True
    succeeded = status_value == "succeeded"
    write_attempted = result.get("trust_registry_write_attempted") is True
    rollback = result.get("rollback_available") is True
    state_fields_admissible = {
        "blocked_before_write": not write_attempted and not modified,
        "write_failed": write_attempted and not modified,
        "postcondition_unverified": write_attempted and modified,
        "succeeded": write_attempted and modified,
    }.get(status_value, False)
    observed_registry_sha256 = str(
        result.get("observed_trust_registry_sha256") or ""
    )
    if not (
        frozenset(result) == _RESULT_KEYS
        and result.get("schema") == RESULT_SCHEMA
        and status_value
        in {
            "blocked_before_write",
            "write_failed",
            "postcondition_unverified",
            "succeeded",
        }
        and _integrity_verified(result)
        and completed_at is not None
        and claimed_at is not None
        and expires_at is not None
        and claimed_at <= completed_at <= expires_at
        and result.get("claim_id") == claim.get("claim_id")
        and result.get("claim_sha256") == _sha256(_canonical(claim))
        and result.get("readiness_id") == scope.get("readiness_id")
        and result.get("readiness_verification_sha256")
        == scope.get("readiness_verification_sha256")
        and result.get("authorization_id") == scope.get("authorization_id")
        and result.get("authorization_receipt_sha256")
        == scope.get("authorization_receipt_sha256")
        and result.get("expected_current_trust_registry_sha256")
        == scope.get("expected_current_trust_registry_sha256")
        and result.get("expected_proposed_trust_registry_sha256")
        == scope.get("expected_proposed_trust_registry_sha256")
        and bool(_SHA256.fullmatch(str(result.get("backup_sha256") or "")))
        is rollback
        and (
            not observed_registry_sha256
            or bool(_SHA256.fullmatch(observed_registry_sha256))
        )
        and result.get("authorization_recorded") is True
        and result.get("authorization_consumed") is True
        and result.get("manual_execution_authorized") is False
        and result.get("automatic_execution_allowed") is False
        and result.get("execution_authorized") is False
        and state_fields_admissible
        and (not write_attempted or rollback)
        and result.get("deployment_or_restart_authorized") is False
        and result.get("protected_operation_executed") is modified
        and result.get("provider_quota_consumption_allowed") is False
        and result.get("delivery_authorized") is False
        and result.get("delivery_attempted") is False
        and result.get("sent") is False
        and result.get("secret_values_recorded") is False
        and result.get("postcondition_verified") is succeeded
        and (
            modified is False
            or observed_registry_sha256
            == scope.get("expected_proposed_trust_registry_sha256")
        )
        and bool(not str(result.get("blocking_reason") or "")) is succeeded
    ):
        return _blocked(
            "trust_enrollment_execution_result_not_admissible",
            now=observed_now,
        )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "execution_state": status_value,
        "updated_at": observed_now.isoformat(),
        "completed_at": completed_at.isoformat(),
        "blocking_reason": str(result.get("blocking_reason") or ""),
        "next_action": (
            "continue signed producer claim verification"
            if succeeded
            else "inspect the immutable claim, result, and backup before any retry"
        ),
        "claim_id": str(result.get("claim_id") or ""),
        "claim_sha256": str(result.get("claim_sha256") or ""),
        "readiness_id": str(result.get("readiness_id") or ""),
        "authorization_id": str(result.get("authorization_id") or ""),
        "expected_current_trust_registry_sha256": str(
            result.get("expected_current_trust_registry_sha256") or ""
        ),
        "expected_proposed_trust_registry_sha256": str(
            result.get("expected_proposed_trust_registry_sha256") or ""
        ),
        "observed_trust_registry_sha256": str(
            result.get("observed_trust_registry_sha256") or ""
        ),
        "backup_path": str(result.get("backup_path") or ""),
        "backup_sha256": str(result.get("backup_sha256") or ""),
        "action_required": not succeeded,
        "interrupt_operator": False,
        "authorization_recorded": True,
        "authorization_consumed": True,
        "manual_execution_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "trust_registry_write_attempted": write_attempted,
        "trust_registry_modified": modified,
        "rollback_available": rollback,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": modified,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {
            "execution_claim_integrity_verified": True,
            "execution_result_integrity_verified": True,
            "postcondition_verified": succeeded,
        },
        "actions": [],
    }


def _history_blocked(reason: str, *, now: datetime) -> dict[str, Any]:
    return {
        **_blocked(reason, now=now),
        "schema": HISTORY_SCHEMA,
        "history_state": "blocked",
        "execution_history_present": False,
    }


def inspect_execution_history(
    *,
    execution_dir: Path = DEFAULT_EXECUTION_DIR,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    target = Path(execution_dir).absolute()
    if not target.exists():
        return {
            **_history_blocked(
                "trust_enrollment_execution_history_absent",
                now=observed_now,
            ),
            "status": "verified",
            "history_state": "no_execution_history",
            "blocking_reason": "",
            "next_action": "continue current readiness verification",
            "execution_history_present": False,
            "action_required": False,
            "progress": {
                "current_evidence_verified": True,
                "claim_count": 0,
                "result_count": 0,
            },
        }
    try:
        _private_directory(target, create=False)
        candidates = sorted(target.iterdir(), key=lambda path: path.name)
        if len(candidates) > MAX_HISTORY_ENTRIES:
            raise ValueError("trust_enrollment_execution_history_too_large")
        claim_rows: dict[str, tuple[dict[str, Any], dict[str, Any], Path]] = {}
        result_rows: dict[str, tuple[dict[str, Any], str, Path]] = {}
        for path in candidates:
            claim_match = re.fullmatch(
                r"claim--(pqtrustready_[0-9a-f]{24})\.json",
                path.name,
            )
            result_match = re.fullmatch(
                r"result--(pqtrustready_[0-9a-f]{24})\.json",
                path.name,
            )
            if claim_match:
                readiness_id = claim_match.group(1)
                claim, _raw, _digest = _private_object(
                    path,
                    field="trust_enrollment_execution_historical_claim",
                )
                verified = verify_execution_claim_history(
                    claim,
                    now=observed_now,
                )
                if not (
                    verified.get("status") == "verified"
                    and verified.get("readiness_id") == readiness_id
                    and readiness_id not in claim_rows
                ):
                    raise ValueError(
                        "trust_enrollment_execution_history_claim_invalid"
                    )
                claim_backup_path = Path(
                    str(claim.get("backup_path") or "")
                ).absolute()
                if not claim_backup_path.is_relative_to(
                    Path(backup_dir).absolute()
                ):
                    raise ValueError(
                        "trust_enrollment_execution_history_backup_path_invalid"
                    )
                claim_rows[readiness_id] = (claim, verified, path)
            elif result_match:
                readiness_id = result_match.group(1)
                result, _raw, digest = _private_object(
                    path,
                    field="trust_enrollment_execution_historical_result",
                )
                if (
                    result.get("readiness_id") != readiness_id
                    or readiness_id in result_rows
                ):
                    raise ValueError(
                        "trust_enrollment_execution_history_result_invalid"
                    )
                result_rows[readiness_id] = (result, digest, path)
            else:
                raise ValueError(
                    "trust_enrollment_execution_history_entry_invalid"
                )
        if set(result_rows) - set(claim_rows):
            raise ValueError(
                "trust_enrollment_execution_history_result_without_claim"
            )
        rows: list[dict[str, Any]] = []
        for readiness_id, (claim, claim_verification, claim_path) in claim_rows.items():
            row = {
                **claim_verification,
                "history_state": "claimed_without_result",
                "claim_path": str(claim_path),
                "result_path": "",
                "result_sha256": "",
            }
            if readiness_id in result_rows:
                result, digest, result_path = result_rows[readiness_id]
                result_verification = verify_execution_result(
                    result,
                    claim=claim,
                    now=observed_now,
                )
                if result_verification.get("status") != "verified":
                    raise ValueError(
                        "trust_enrollment_execution_history_result_invalid"
                    )
                backup_path = Path(
                    str(result_verification.get("backup_path") or "")
                ).absolute()
                if not backup_path.is_relative_to(Path(backup_dir).absolute()):
                    raise ValueError(
                        "trust_enrollment_execution_history_backup_path_invalid"
                    )
                if result_verification.get("rollback_available") is True:
                    backup = read_stable_bytes(
                        backup_path,
                        maximum_bytes=claims.MAX_TRUST_REGISTRY_BYTES,
                    )
                    if _sha256(backup) != result_verification.get(
                        "backup_sha256"
                    ):
                        raise ValueError(
                            "trust_enrollment_execution_history_backup_invalid"
                        )
                row = {
                    **result_verification,
                    "history_state": "execution_result",
                    "claimed_at": str(
                        claim_verification.get("claimed_at") or ""
                    ),
                    "expires_at": str(
                        claim_verification.get("expires_at") or ""
                    ),
                    "claim_path": str(claim_path),
                    "result_path": str(result_path),
                    "result_sha256": digest,
                }
            rows.append(row)
        if not rows:
            return {
                **_history_blocked(
                    "trust_enrollment_execution_history_absent",
                    now=observed_now,
                ),
                "status": "verified",
                "history_state": "no_execution_history",
                "blocking_reason": "",
                "next_action": "continue current readiness verification",
                "execution_history_present": False,
                "action_required": False,
                "progress": {
                    "current_evidence_verified": True,
                    "claim_count": 0,
                    "result_count": 0,
                },
            }
        rows.sort(
            key=lambda row: (
                _timestamp(row.get("claimed_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
                str(row.get("readiness_id") or ""),
            )
        )
        latest = rows[-1]
        return {
            "schema": HISTORY_SCHEMA,
            "status": "verified",
            "history_state": str(latest.get("history_state") or ""),
            "updated_at": observed_now.isoformat(),
            "blocking_reason": str(latest.get("blocking_reason") or ""),
            "next_action": str(latest.get("next_action") or ""),
            "execution_history_present": True,
            "latest_execution": latest,
            "latest_readiness_id": str(latest.get("readiness_id") or ""),
            "latest_claim_id": str(latest.get("claim_id") or ""),
            "latest_result_sha256": str(latest.get("result_sha256") or ""),
            "action_required": latest.get("execution_state") != "succeeded",
            "interrupt_operator": False,
            "authorization_recorded": True,
            "authorization_consumed": True,
            "manual_execution_authorized": False,
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "trust_registry_write_attempted": latest.get(
                "trust_registry_write_attempted"
            )
            is True,
            "trust_registry_modified": latest.get("trust_registry_modified")
            is True,
            "rollback_available": latest.get("rollback_available") is True,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": latest.get(
                "protected_operation_executed"
            )
            is True,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "progress": {
                "current_evidence_verified": True,
                "claim_count": len(claim_rows),
                "result_count": len(result_rows),
            },
        }
    except Exception:
        return _history_blocked(
            "trust_enrollment_execution_history_not_admissible",
            now=observed_now,
        )


def inspect_execution_for_readiness(
    readiness_report: Mapping[str, Any],
    *,
    execution_dir: Path = DEFAULT_EXECUTION_DIR,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    history = inspect_execution_history(
        execution_dir=execution_dir,
        backup_dir=backup_dir,
        now=observed_now,
    )
    if history.get("status") != "verified":
        return _blocked(
            str(
                history.get("blocking_reason")
                or "trust_enrollment_execution_history_unavailable"
            ),
            now=observed_now,
        )
    latest = dict(history.get("latest_execution") or {})
    current_readiness_id = str(readiness_report.get("readiness_id") or "")
    if latest and (
        latest.get("execution_state") == "succeeded"
        and (
            latest.get("readiness_id") == current_readiness_id
            or readiness_report.get("readiness_state") == "not_required"
        )
    ):
        progress = dict(latest.get("progress") or {})
        progress["current_evidence_verified"] = True
        return {
            **latest,
            "schema": VERIFY_SCHEMA,
            "status": "verified",
            "execution_state": "succeeded",
            "updated_at": observed_now.isoformat(),
            "action_required": False,
            "interrupt_operator": False,
            "progress": progress,
            "actions": [],
            "execution_history": history,
        }
    if latest and latest.get("execution_state") != "succeeded":
        progress = dict(latest.get("progress") or {})
        progress["current_evidence_verified"] = True
        return {
            **latest,
            "schema": VERIFY_SCHEMA,
            "status": "verified",
            "execution_state": "recovery_required",
            "updated_at": observed_now.isoformat(),
            "action_required": True,
            "interrupt_operator": False,
            "progress": progress,
            "actions": [],
            "execution_history": history,
        }
    state = str(readiness_report.get("readiness_state") or "")
    mapping = {
        "not_required": "not_required",
        "awaiting_exact_preview_authorization": "awaiting_authorization",
        "authorization_rejected": "authorization_rejected",
        "deferred": "deferred",
    }
    if state in mapping and readiness_report.get("status") == "verified":
        return {
            **_blocked("", now=observed_now),
            "status": "verified",
            "execution_state": mapping[state],
            "blocking_reason": "",
            "next_action": str(readiness_report.get("next_action") or ""),
            "readiness_id": current_readiness_id,
            "progress": {"current_evidence_verified": True},
            "execution_history": history,
        }
    if not _readiness_admissible(readiness_report):
        return _blocked(
            "trust_enrollment_execution_readiness_not_admissible",
            now=observed_now,
        )
    scope = {
        "readiness_id": current_readiness_id,
        "readiness_verification_sha256": str(
            readiness_report.get("verification_receipt_sha256") or ""
        ),
        "authorization_id": str(
            readiness_report.get("authorization_id") or ""
        ),
        "authorization_receipt_sha256": str(
            readiness_report.get("authorization_receipt_sha256") or ""
        ),
        "current_trust_registry_sha256": str(
            readiness_report.get("current_trust_registry_sha256") or ""
        ),
        "proposed_trust_registry_sha256": str(
            readiness_report.get("proposed_trust_registry_sha256") or ""
        ),
    }
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "execution_state": "ready_for_manual_execution",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": (
            "invoke the one-shot governed executor with every exact binding, "
            "executor identity, evidence, and literal confirmation"
        ),
        **scope,
        "action_required": True,
        "interrupt_operator": False,
        "authorization_recorded": True,
        "authorization_consumed": False,
        "manual_execution_authorized": True,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "trust_registry_write_attempted": False,
        "trust_registry_modified": False,
        "rollback_available": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {"current_evidence_verified": True},
        "actions": [
            {
                "lane": "source_refresh_trust_enrollment",
                "reason": "exact_trust_registry_execution_ready",
                "safe_next_action": (
                    "run the exact one-shot executor or defer; automatic execution is disabled"
                ),
                "consent_required": False,
                "manual_execution_authorized": True,
                "automatic_execution_allowed": False,
                "protected_operations": ["replace_exact_trust_registry"],
                "provider_quota_consumption_allowed": False,
                **scope,
            }
        ],
        "execution_history": history,
    }


def inspect_current_execution(
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
    readiness_receipt_path: Path = readiness.DEFAULT_RECEIPT_PATH,
    readiness_verification_path: Path = readiness.DEFAULT_VERIFICATION_PATH,
    execution_dir: Path = DEFAULT_EXECUTION_DIR,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    now: datetime | None = None,
    max_age_seconds: float = readiness.DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    current_readiness = readiness.inspect_current_execution_readiness(
        authorization_dir=authorization_dir,
        preview_decision_dir=preview_decision_dir,
        claim_verification_path=claim_verification_path,
        trust_registry_path=trust_registry_path,
        candidate_dir=candidate_dir,
        intake_receipt_path=intake_receipt_path,
        intake_verification_path=intake_verification_path,
        preview_receipt_path=preview_receipt_path,
        preview_verification_path=preview_verification_path,
        receipt_path=readiness_receipt_path,
        verification_path=readiness_verification_path,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    return inspect_execution_for_readiness(
        current_readiness,
        execution_dir=execution_dir,
        backup_dir=backup_dir,
        now=observed_now,
    )


def execute_current_enrollment(
    *,
    expected_readiness_id: str,
    expected_readiness_verification_sha256: str,
    expected_authorization_id: str,
    expected_authorization_receipt_sha256: str,
    expected_current_trust_registry_sha256: str,
    expected_proposed_trust_registry_sha256: str,
    executor_id: str,
    execution_method: str,
    execution_evidence_ref: str,
    authorization_dir: Path = authorization.DEFAULT_DECISION_DIR,
    preview_decision_dir: Path = trust_decision.DEFAULT_DECISION_DIR,
    claim_verification_path: Path = claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = claims.DEFAULT_TRUST_REGISTRY_PATH,
    candidate_dir: Path = trust_intake.DEFAULT_CANDIDATE_DIR,
    intake_receipt_path: Path = trust_intake.DEFAULT_RECEIPT_PATH,
    intake_verification_path: Path = trust_intake.DEFAULT_VERIFICATION_PATH,
    preview_receipt_path: Path = preview.DEFAULT_RECEIPT_PATH,
    preview_verification_path: Path = preview.DEFAULT_VERIFICATION_PATH,
    readiness_receipt_path: Path = readiness.DEFAULT_RECEIPT_PATH,
    readiness_verification_path: Path = readiness.DEFAULT_VERIFICATION_PATH,
    execution_dir: Path = DEFAULT_EXECUTION_DIR,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    now: datetime | None = None,
    max_age_seconds: float = readiness.DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    claimed_now = _now(now)
    current_readiness = readiness.inspect_current_execution_readiness(
        authorization_dir=authorization_dir,
        preview_decision_dir=preview_decision_dir,
        claim_verification_path=claim_verification_path,
        trust_registry_path=trust_registry_path,
        candidate_dir=candidate_dir,
        intake_receipt_path=intake_receipt_path,
        intake_verification_path=intake_verification_path,
        preview_receipt_path=preview_receipt_path,
        preview_verification_path=preview_verification_path,
        receipt_path=readiness_receipt_path,
        verification_path=readiness_verification_path,
        now=claimed_now,
        max_age_seconds=max_age_seconds,
    )
    expected = {
        "readiness_id": expected_readiness_id,
        "verification_receipt_sha256": (
            expected_readiness_verification_sha256
        ),
        "authorization_id": expected_authorization_id,
        "authorization_receipt_sha256": (
            expected_authorization_receipt_sha256
        ),
        "current_trust_registry_sha256": (
            expected_current_trust_registry_sha256
        ),
        "proposed_trust_registry_sha256": (
            expected_proposed_trust_registry_sha256
        ),
    }
    if not (
        _readiness_admissible(current_readiness)
        and all(current_readiness.get(key) == value for key, value in expected.items())
    ):
        return _blocked(
            "trust_enrollment_execution_exact_binding_mismatch",
            now=claimed_now,
        )
    preview_report = preview.inspect_current_enrollment_preview(
        decision_dir=preview_decision_dir,
        claim_verification_path=claim_verification_path,
        trust_registry_path=trust_registry_path,
        candidate_dir=candidate_dir,
        intake_receipt_path=intake_receipt_path,
        intake_verification_path=intake_verification_path,
        receipt_path=preview_receipt_path,
        verification_path=preview_verification_path,
        now=claimed_now,
        max_age_seconds=max_age_seconds,
    )
    proposed_registry = dict(preview_report.get("proposed_registry") or {})
    proposed_raw = _canonical(proposed_registry)
    try:
        current_raw = read_stable_bytes(
            Path(trust_registry_path).absolute(),
            maximum_bytes=claims.MAX_TRUST_REGISTRY_BYTES,
        )
        if not (
            authorization._preview_is_admissible(preview_report)
            and preview_report.get("preview_id")
            == current_readiness.get("preview_id")
            and preview_report.get("proposed_trust_registry_sha256")
            == expected_proposed_trust_registry_sha256
            and preview_report.get("current_trust_registry_sha256")
            == expected_current_trust_registry_sha256
            and preview_report.get("trust_registry_modified") is False
            and dict(preview_report.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and _sha256(current_raw) == expected_current_trust_registry_sha256
            and _sha256(proposed_raw)
            == expected_proposed_trust_registry_sha256
            and preview._integrity_verified(proposed_registry)
        ):
            raise ValueError("trust_enrollment_execution_registry_binding_invalid")
        _private_directory(execution_dir, create=True)
        _private_directory(backup_dir, create=True)
        claim_path, result_path, backup_path = _paths(
            execution_dir,
            backup_dir,
            expected_readiness_id,
            expected_current_trust_registry_sha256,
        )
        try:
            claim_path.lstat()
        except FileNotFoundError:
            try:
                result_path.lstat()
            except FileNotFoundError:
                pass
            else:
                raise ValueError(
                    "trust_enrollment_execution_result_already_present"
                )
        claim = build_execution_claim(
            current_readiness,
            executor_id=executor_id,
            execution_method=execution_method,
            execution_evidence_ref=execution_evidence_ref,
            backup_path=backup_path,
            now=claimed_now,
        )
        atomic_write_bytes(claim_path, _canonical(claim), overwrite=False)
    except OutputExistsError:
        return _blocked(
            "trust_enrollment_execution_readiness_already_claimed",
            now=claimed_now,
        )
    except Exception:
        return _blocked(
            "trust_enrollment_execution_preclaim_not_admissible",
            now=claimed_now,
        )
    backup_sha256 = ""
    observed_registry_sha256 = expected_current_trust_registry_sha256
    write_attempted = False
    registry_modified = False
    rollback_available = False
    result_status = "blocked_before_write"
    blocking_reason = "trust_enrollment_execution_not_started"
    try:
        persisted_claim, _raw, _digest = _private_object(
            claim_path,
            field="trust_enrollment_execution_claim",
        )
        if (
            persisted_claim != claim
            or verify_execution_claim(
                persisted_claim,
                readiness_report=current_readiness,
                now=claimed_now,
            ).get("status")
            != "verified"
        ):
            raise ValueError("trust_enrollment_execution_claim_persistence_invalid")
        atomic_write_bytes(backup_path, current_raw, overwrite=False)
        backup = read_stable_bytes(
            backup_path,
            maximum_bytes=claims.MAX_TRUST_REGISTRY_BYTES,
        )
        if backup != current_raw:
            raise ValueError("trust_enrollment_execution_backup_invalid")
        backup_sha256 = _sha256(backup)
        rollback_available = True
        write_attempted = True
        atomic_replace_bytes_if_matches(
            Path(trust_registry_path).absolute(),
            expected=current_raw,
            replacement=proposed_raw,
            maximum_bytes=claims.MAX_TRUST_REGISTRY_BYTES,
        )
        observed_registry = claims.load_producer_trust_registry(
            trust_registry_path
        )
        observed_registry_sha256 = str(
            observed_registry.get("trust_registry_sha256") or ""
        )
        if observed_registry_sha256 != expected_proposed_trust_registry_sha256:
            raise ValueError("trust_enrollment_execution_postcondition_invalid")
        registry_modified = True
        result_status = "succeeded"
        blocking_reason = ""
    except Exception as exc:
        blocking_reason = str(exc) or "trust_enrollment_execution_failed"
        try:
            observed_raw = read_stable_bytes(
                Path(trust_registry_path).absolute(),
                maximum_bytes=claims.MAX_TRUST_REGISTRY_BYTES,
            )
            observed_registry_sha256 = _sha256(observed_raw)
            registry_modified = observed_raw == proposed_raw
        except Exception:
            observed_registry_sha256 = ""
        if write_attempted:
            result_status = (
                "postcondition_unverified" if registry_modified else "write_failed"
            )
        else:
            result_status = "blocked_before_write"
    completed_at = _now(now)
    result = _build_result(
        claim=claim,
        status=result_status,
        blocking_reason=blocking_reason,
        backup_sha256=backup_sha256,
        observed_registry_sha256=observed_registry_sha256,
        write_attempted=write_attempted,
        registry_modified=registry_modified,
        rollback_available=rollback_available,
        now=completed_at,
    )
    try:
        atomic_write_bytes(result_path, _canonical(result), overwrite=False)
        persisted_result, _raw, _digest = _private_object(
            result_path,
            field="trust_enrollment_execution_result",
        )
        if persisted_result != result:
            raise ValueError("trust_enrollment_execution_result_persistence_invalid")
    except Exception:
        return {
            **_blocked(
                "trust_enrollment_execution_result_persistence_failed",
                now=completed_at,
            ),
            "execution_state": "recovery_required",
            "readiness_id": expected_readiness_id,
            "claim_id": str(claim.get("claim_id") or ""),
            "action_required": True,
            "authorization_recorded": True,
            "authorization_consumed": True,
            "trust_registry_write_attempted": write_attempted,
            "trust_registry_modified": registry_modified,
            "rollback_available": rollback_available,
            "protected_operation_executed": registry_modified,
            "observed_trust_registry_sha256": observed_registry_sha256,
            "backup_path": str(claim.get("backup_path") or ""),
            "backup_sha256": backup_sha256,
            "progress": {
                "current_evidence_verified": False,
                "execution_claim_persisted": True,
                "execution_result_persisted": False,
            },
        }
    return verify_execution_result(
        result,
        claim=claim,
        now=completed_at,
    )


def _path_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--authorization-dir", type=Path, default=authorization.DEFAULT_DECISION_DIR)
    parser.add_argument("--preview-decision-dir", type=Path, default=trust_decision.DEFAULT_DECISION_DIR)
    parser.add_argument("--claim-verification", type=Path, default=claims.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--trust-registry", type=Path, default=claims.DEFAULT_TRUST_REGISTRY_PATH)
    parser.add_argument("--candidate-dir", type=Path, default=trust_intake.DEFAULT_CANDIDATE_DIR)
    parser.add_argument("--intake-receipt", type=Path, default=trust_intake.DEFAULT_RECEIPT_PATH)
    parser.add_argument("--intake-verification", type=Path, default=trust_intake.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--preview-receipt", type=Path, default=preview.DEFAULT_RECEIPT_PATH)
    parser.add_argument("--preview-verification", type=Path, default=preview.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--readiness-receipt", type=Path, default=readiness.DEFAULT_RECEIPT_PATH)
    parser.add_argument("--readiness-verification", type=Path, default=readiness.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--execution-dir", type=Path, default=DEFAULT_EXECUTION_DIR)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or execute one exact trust-registry enrollment. Execution "
            "requires current consent, exact hashes, identity evidence, and an "
            "explicit one-shot flag."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true")
    action.add_argument(
        "--execute-exact-readiness",
        action="store_true",
        help="confirm deliberate invocation of the one-shot governed executor",
    )
    _path_arguments(parser)
    parser.add_argument("--readiness-id")
    parser.add_argument("--readiness-verification-sha256")
    parser.add_argument("--authorization-id")
    parser.add_argument("--authorization-receipt-sha256")
    parser.add_argument("--current-trust-registry-sha256")
    parser.add_argument("--proposed-trust-registry-sha256")
    parser.add_argument("--executor-id")
    parser.add_argument(
        "--execution-method",
        choices=authorization.AUTHORIZATION_METHODS,
    )
    parser.add_argument("--execution-evidence-ref")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    common = {
        "authorization_dir": args.authorization_dir,
        "preview_decision_dir": args.preview_decision_dir,
        "claim_verification_path": args.claim_verification,
        "trust_registry_path": args.trust_registry,
        "candidate_dir": args.candidate_dir,
        "intake_receipt_path": args.intake_receipt,
        "intake_verification_path": args.intake_verification,
        "preview_receipt_path": args.preview_receipt,
        "preview_verification_path": args.preview_verification,
        "readiness_receipt_path": args.readiness_receipt,
        "readiness_verification_path": args.readiness_verification,
        "execution_dir": args.execution_dir,
        "backup_dir": args.backup_dir,
    }
    if args.inspect:
        result = inspect_current_execution(**common)
    else:
        required = (
            args.readiness_id,
            args.readiness_verification_sha256,
            args.authorization_id,
            args.authorization_receipt_sha256,
            args.current_trust_registry_sha256,
            args.proposed_trust_registry_sha256,
            args.executor_id,
            args.execution_method,
            args.execution_evidence_ref,
        )
        if not all(required):
            parser.error("execution requires every exact binding and evidence field")
        result = execute_current_enrollment(
            expected_readiness_id=args.readiness_id,
            expected_readiness_verification_sha256=(
                args.readiness_verification_sha256
            ),
            expected_authorization_id=args.authorization_id,
            expected_authorization_receipt_sha256=(
                args.authorization_receipt_sha256
            ),
            expected_current_trust_registry_sha256=(
                args.current_trust_registry_sha256
            ),
            expected_proposed_trust_registry_sha256=(
                args.proposed_trust_registry_sha256
            ),
            executor_id=args.executor_id,
            execution_method=args.execution_method,
            execution_evidence_ref=args.execution_evidence_ref,
            **common,
        )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("execution_state") in {
        "not_required",
        "awaiting_authorization",
        "authorization_rejected",
        "deferred",
        "ready_for_manual_execution",
        "succeeded",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
