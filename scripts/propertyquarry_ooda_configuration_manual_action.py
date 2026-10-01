#!/usr/bin/env python3
"""Stage and explicitly execute one reversible approved source edit.

Staging is always non-applying.  Source bytes can change only through an
explicit ``--apply`` or ``--rollback`` invocation bound to immutable private
artifacts.  Deployment, restart, provider, quota, and delivery operations are
outside this module's contract.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_authorization_decision as authorization_decision
from scripts import propertyquarry_ooda_authorization_request as authorization_request
from scripts import propertyquarry_ooda_configuration_change_preview as change_preview
from scripts import propertyquarry_ooda_configuration_plan as configuration_plan
from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts.propertyquarry_secure_file_io import (
    OutputExistsError,
    atomic_replace_bytes_if_matches,
    atomic_write_bytes,
    read_stable_bytes,
)


SCHEMA = "propertyquarry.ooda_runtime_configuration_manual_action_handoff.v1"
VERIFY_SCHEMA = (
    "propertyquarry.ooda_runtime_configuration_manual_action_verification.v1"
)
HANDOFF_SCHEMA = (
    "propertyquarry.ooda_runtime_configuration_manual_action_staging.v1"
)
EXECUTION_SCHEMA = (
    "propertyquarry.ooda_runtime_configuration_manual_action_execution.v1"
)
DOTENV_EXECUTION_SCHEMA = (
    "propertyquarry.ooda_runtime_configuration_dotenv_manual_apply.v1"
)
DOTENV_ROLLBACK_SCHEMA = (
    "propertyquarry.ooda_runtime_configuration_dotenv_manual_rollback.v1"
)
RESULT_SCHEMA = (
    "propertyquarry.ooda_runtime_configuration_manual_action_result.v1"
)
DEFAULT_HANDOFF_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "runtime-configuration-manual-actions"
)
DEFAULT_RECEIPT_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "runtime-configuration-manual-action-receipts"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "runtime-configuration-manual-action-verification.json"
)
MAX_HANDOFF_BYTES = 512 * 1024
MAX_EXECUTION_BYTES = 256 * 1024
MAX_DOTENV_BYTES = 4 * 1024 * 1024
_HANDOFF_ID = re.compile(r"pqmh_[0-9a-f]{24}\Z")
_APPLY_ID = re.compile(r"pqme_[0-9a-f]{24}\Z")
_ROLLBACK_ID = re.compile(r"pqmr_[0-9a-f]{24}\Z")
_PLAN_ID = re.compile(r"pqcp_[0-9a-f]{24}\Z")
_DOTENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _dotenv_canonical(value: Mapping[str, Any]) -> bytes:
    """Canonical form used by the imported value-free dotenv apply receipt."""

    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _dotenv_relative_path(value: object, *, field: str) -> Path:
    path = Path(str(value or ""))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"{field}_path_not_admissible")
    return path


def _dotenv_private_bytes(
    path: Path,
    *,
    root: Path,
    field: str,
) -> bytes:
    relative = _dotenv_relative_path(path, field=field)
    target = root / relative
    metadata = target.lstat()
    if not (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid in {0, os.geteuid()}
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and 0 < metadata.st_size <= MAX_DOTENV_BYTES
    ):
        raise ValueError(f"{field}_file_not_admissible")
    return read_stable_bytes(
        target,
        maximum_bytes=MAX_DOTENV_BYTES,
        require_nonempty=True,
    )


def _dotenv_entries(raw: bytes, *, field: str) -> dict[str, bytes]:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{field}_utf8_invalid") from exc
    entries: dict[str, bytes] = {}
    for original in raw.splitlines():
        normalized = original.strip()
        if not normalized or normalized.startswith(b"#"):
            continue
        if normalized.startswith(b"export "):
            normalized = normalized[7:].lstrip()
        if b"=" not in normalized:
            raise ValueError(f"{field}_line_invalid")
        key_raw, _value = normalized.split(b"=", 1)
        try:
            key = key_raw.strip().decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{field}_key_invalid") from exc
        if _DOTENV_KEY.fullmatch(key) is None or key in entries:
            raise ValueError(f"{field}_key_invalid_or_duplicate")
        entries[key] = original
    return entries


def _dotenv_apply_receipt_envelope(
    receipt: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    normalized = dict(receipt)
    integrity = normalized.pop("integrity", None)
    binding = receipt.get("binding")
    target = receipt.get("target")
    rollback = receipt.get("rollback")
    applied_at = _parse_timestamp(receipt.get("applied_at"))
    expected_keys = sorted(runtime_review._LOCAL_CANDIDATE_KEYS)
    if not (
        set(receipt)
        == {
            "schema",
            "status",
            "execution_id",
            "applied_at",
            "operation",
            "binding",
            "target",
            "rollback",
            "authorization_required",
            "authorization_recorded",
            "exact_scope_authorized",
            "manual_apply_authorized",
            "automatic_execution_allowed",
            "runtime_configuration_change_performed",
            "source_edit_performed",
            "deployment_or_restart_authorized",
            "deployment_or_restart_performed",
            "provider_quota_consumption_allowed",
            "provider_quota_consumed",
            "delivery_authorized",
            "delivery_attempted",
            "environment_values_recorded",
            "environment_values_hashed",
            "secret_values_recorded",
            "integrity",
        }
        and receipt.get("schema") == DOTENV_EXECUTION_SCHEMA
        and receipt.get("status") == "applied"
        and _APPLY_ID.fullmatch(str(receipt.get("execution_id") or ""))
        and applied_at is not None
        and receipt.get("operation") == "merge_dotenv_add_missing_keys"
        and isinstance(binding, Mapping)
        and set(binding)
        == {
            "request_id",
            "request_sha256",
            "decision_id",
            "decision_sha256",
            "plan_id",
            "plan_sha256",
        }
        and authorization_request._REQUEST_ID.fullmatch(
            str(binding.get("request_id") or "")
        )
        and authorization_decision._DECISION_ID.fullmatch(
            str(binding.get("decision_id") or "")
        )
        and _PLAN_ID.fullmatch(str(binding.get("plan_id") or ""))
        and all(
            authorization_request._SHA256.fullmatch(str(binding.get(key) or ""))
            for key in ("request_sha256", "decision_sha256", "plan_sha256")
        )
        and isinstance(target, Mapping)
        and set(target)
        == {
            "path",
            "file_mode",
            "configuration_merge_policy",
            "existing_environment_values_overwrite_allowed",
            "added_keys",
            "added_key_count",
            "existing_keys_preserved",
            "missing_authorized_key_count_after_apply",
        }
        and target.get("path")
        == runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH.as_posix()
        and target.get("file_mode") == 0o600
        and target.get("configuration_merge_policy") == "add_missing_keys_only"
        and target.get("existing_environment_values_overwrite_allowed") is False
        and target.get("added_keys") == expected_keys
        and target.get("added_key_count") == len(expected_keys)
        and target.get("existing_keys_preserved") is True
        and target.get("missing_authorized_key_count_after_apply") == 0
        and isinstance(rollback, Mapping)
        and set(rollback)
        == {
            "required",
            "available",
            "snapshot_path",
            "snapshot_file_mode",
            "snapshot_exact_pre_apply_bytes_verified",
            "automatic_rollback_allowed",
        }
        and rollback.get("required") is True
        and rollback.get("available") is True
        and rollback.get("snapshot_file_mode") == 0o600
        and rollback.get("snapshot_exact_pre_apply_bytes_verified") is True
        and rollback.get("automatic_rollback_allowed") is False
        and _dotenv_relative_path(
            rollback.get("snapshot_path"),
            field="dotenv_rollback_snapshot",
        ).name
        == (
            f"{binding.get('request_id')}.{binding.get('request_sha256')}.env"
        )
        and receipt.get("authorization_required") is True
        and receipt.get("authorization_recorded") is True
        and receipt.get("exact_scope_authorized") is True
        and receipt.get("manual_apply_authorized") is True
        and receipt.get("automatic_execution_allowed") is False
        and receipt.get("runtime_configuration_change_performed") is True
        and receipt.get("source_edit_performed") is False
        and receipt.get("deployment_or_restart_authorized") is False
        and receipt.get("deployment_or_restart_performed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("provider_quota_consumed") is False
        and receipt.get("delivery_authorized") is False
        and receipt.get("delivery_attempted") is False
        and receipt.get("environment_values_recorded") is False
        and receipt.get("environment_values_hashed") is False
        and receipt.get("secret_values_recorded") is False
        and isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and integrity.get("canonical_payload_sha256")
        == _sha256(_dotenv_canonical(normalized))
    ):
        raise ValueError("dotenv_apply_receipt_not_admissible")
    return (
        dict(binding),
        dict(target),
        dict(rollback),
        _sha256(_dotenv_canonical(receipt)),
    )


def _dotenv_apply_state(
    receipt: Mapping[str, Any],
    *,
    root: Path,
) -> tuple[dict[str, Any], bytes, bytes, str]:
    binding, target, rollback, receipt_sha256 = _dotenv_apply_receipt_envelope(
        receipt
    )
    expected_keys = sorted(runtime_review._LOCAL_CANDIDATE_KEYS)
    target_path = _dotenv_relative_path(target.get("path"), field="dotenv_target")
    candidate_path = runtime_review.DEFAULT_DEPLOYMENT_ENV_LOCAL_RUNTIME_CANDIDATE_PATH
    snapshot_path = _dotenv_relative_path(
        rollback.get("snapshot_path"),
        field="dotenv_rollback_snapshot",
    )
    current_raw = _dotenv_private_bytes(
        target_path,
        root=root,
        field="dotenv_target",
    )
    candidate_raw = _dotenv_private_bytes(
        candidate_path,
        root=root,
        field="dotenv_candidate",
    )
    snapshot_raw = _dotenv_private_bytes(
        snapshot_path,
        root=root,
        field="dotenv_rollback_snapshot",
    )
    current_entries = _dotenv_entries(current_raw, field="dotenv_target")
    candidate_entries = _dotenv_entries(candidate_raw, field="dotenv_candidate")
    snapshot_entries = _dotenv_entries(snapshot_raw, field="dotenv_rollback_snapshot")
    expected_after = (
        snapshot_raw
        + (b"" if snapshot_raw.endswith(b"\n") else b"\n")
        + b"\n".join(candidate_entries[key] for key in expected_keys)
        + b"\n"
    )
    if not (
        sorted(candidate_entries) == expected_keys
        and all(key not in snapshot_entries for key in expected_keys)
        and current_raw == expected_after
        and all(current_entries.get(key) == value for key, value in snapshot_entries.items())
        and all(current_entries.get(key) == candidate_entries[key] for key in expected_keys)
    ):
        raise ValueError("dotenv_apply_current_state_not_admissible")
    state_binding = {
        "kind": "dotenv_add_missing_keys",
        "target_path": target_path.as_posix(),
        "candidate_path": candidate_path.as_posix(),
        "rollback_snapshot_path": snapshot_path.as_posix(),
        "added_keys": expected_keys,
        "added_key_count": len(expected_keys),
        "existing_environment_values_preserved": True,
        "candidate_environment_values_present": True,
        "target_file_mode": 0o600,
        "candidate_file_mode": 0o600,
        "rollback_snapshot_file_mode": 0o600,
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
    }
    return state_binding, current_raw, snapshot_raw, receipt_sha256


def verify_dotenv_apply_receipt_state(
    receipt: Mapping[str, Any],
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    state_binding, _current_raw, _snapshot_raw, receipt_sha256 = (
        _dotenv_apply_state(receipt, root=root)
    )
    return {
        "status": "verified",
        "execution_id": str(receipt.get("execution_id") or ""),
        "receipt_sha256": receipt_sha256,
        "applied_at": str(receipt.get("applied_at") or ""),
        "binding": dict(receipt.get("binding") or {}),
        "target": dict(receipt.get("target") or {}),
        "rollback": dict(receipt.get("rollback") or {}),
        "state_binding": state_binding,
        "source_state_verification_sha256": _sha256(_canonical(state_binding)),
        "current_evidence_verified": True,
        "receipt_verified": True,
        "source_state_verified": True,
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
        "automatic_execution_allowed": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
    }


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return runtime_review._canonical(dict(value))


def _sha256(value: bytes) -> str:
    return runtime_review._sha256(value)


def _parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _blocked(
    reason: str,
    *,
    now: datetime | None = None,
    source_edit_performed: bool | None = False,
) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "status": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "reverify the exact private handoff and source receipt before any retry"
        ),
        "current_evidence_verified": False,
        "handoff_verified": False,
        "authorization_required": True,
        "authorization_recorded": False,
        "exact_scope_authorized": False,
        "manual_apply_authorized": False,
        "manual_rollback_authorized": False,
        "manual_invocation_required": True,
        "automatic_execution_allowed": False,
        "source_edit_performed": source_edit_performed,
        "source_edit_state": (
            "performed"
            if source_edit_performed is True
            else "not_performed"
            if source_edit_performed is False
            else "unknown_requires_reconciliation"
        ),
        "protected_operation_executed": source_edit_performed,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
    }


def _not_authorized(
    preview_handoff: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    decision = str(preview_handoff.get("authorization_decision") or "pending")
    return {
        "schema": HANDOFF_SCHEMA,
        "status": "not_authorized",
        "updated_at": now.isoformat(),
        "blocking_reason": "explicit_exact_scope_approval_required",
        "next_action": "record approve_exact_scope before staging a manual action",
        "authorization_decision": decision,
        "request_id": str(preview_handoff.get("request_id") or ""),
        "request_sha256": str(preview_handoff.get("request_sha256") or ""),
        "handoff_state": "absent",
        "handoff_state_updated": False,
        "current_evidence_verified": preview_handoff.get(
            "current_evidence_verified"
        )
        is True,
        "handoff_verified": False,
        "authorization_required": True,
        "authorization_recorded": decision in {"reject", "defer"},
        "exact_scope_authorized": False,
        "manual_apply_authorized": False,
        "manual_rollback_authorized": False,
        "manual_invocation_required": True,
        "automatic_execution_allowed": False,
        "source_edit_performed": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
    }


def _handoff_path(handoff_dir: Path, handoff_id: str) -> Path:
    if _HANDOFF_ID.fullmatch(handoff_id) is None:
        raise ValueError("manual_action_handoff_id_invalid")
    return runtime_review._rooted(handoff_dir) / f"{handoff_id}.json"


def _execution_path(receipt_dir: Path, execution_id: str) -> Path:
    if not (
        _APPLY_ID.fullmatch(execution_id)
        or _ROLLBACK_ID.fullmatch(execution_id)
    ):
        raise ValueError("manual_action_execution_id_invalid")
    return runtime_review._rooted(receipt_dir) / f"{execution_id}.json"


def _private_object(
    path: Path,
    *,
    field: str,
    maximum_bytes: int,
) -> tuple[dict[str, Any], bytes, str]:
    return authorization_request._private_object(
        path,
        field=field,
        maximum_bytes=maximum_bytes,
    )


def _source_snapshot(*, root: Path) -> tuple[str, bytes, str]:
    return configuration_plan._source_snapshot(
        configuration_plan.COMPOSE_PATH,
        root=root,
    )


def _preview_contract(
    preview_artifact: Mapping[str, Any],
    preview_sha256: str,
    preview_verification: Mapping[str, Any],
    *,
    source_text: str,
    source_raw: bytes,
    source_sha256: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any], datetime, datetime, bytes]:
    preview = preview_artifact.get("preview")
    binding = preview_artifact.get("binding")
    generated_at = _parse_timestamp(preview_artifact.get("generated_at"))
    expires_at = _parse_timestamp(preview_artifact.get("expires_at"))
    if not (
        preview_artifact.get("schema") == change_preview.SCHEMA
        and change_preview._PREVIEW_ID.fullmatch(
            str(preview_artifact.get("preview_id") or "")
        )
        and preview_sha256 == _sha256(_canonical(preview_artifact))
        and generated_at is not None
        and expires_at is not None
        and generated_at <= now <= expires_at
        and isinstance(preview, Mapping)
        and isinstance(binding, Mapping)
        and preview_verification.get("status") in {"ready", "verified"}
        and preview_verification.get("current_evidence_verified") is True
        and preview_verification.get("preview_verified") is True
        and preview_verification.get("preview_id")
        == preview_artifact.get("preview_id")
        and preview_verification.get("preview_sha256") == preview_sha256
        and preview_verification.get("request_id") == binding.get("request_id")
        and preview_verification.get("request_sha256")
        == binding.get("request_sha256")
        and preview_verification.get("decision_id") == binding.get("decision_id")
        and preview_verification.get("decision_sha256")
        == binding.get("decision_sha256")
        and preview_verification.get("plan_id") == binding.get("plan_id")
        and preview_verification.get("plan_sha256") == binding.get("plan_sha256")
        and preview_verification.get("authorization_recorded") is True
        and preview_verification.get("exact_scope_authorized") is True
        and preview_verification.get("manual_apply_authorized") is True
        and preview_verification.get("automatic_apply_allowed") is False
        and preview_verification.get("source_edit_performed") is False
        and preview_verification.get("execution_authorized") is False
        and preview_verification.get("execution_performed") is False
        and preview_verification.get("deployment_or_restart_authorized") is False
        and preview_verification.get("protected_operation_executed") is False
        and preview_verification.get("provider_quota_consumption_allowed") is False
        and preview_verification.get("delivery_authorized") is False
        and preview.get("operation") == "replace_exact_text_preview"
        and preview.get("path") == configuration_plan.COMPOSE_PATH.as_posix()
        and preview.get("selector") == "services.propertyquarry-api.ports[0]"
        and preview.get("before_sha256") == source_sha256
        and preview.get("before_bytes") == len(source_raw)
        and preview.get("replacement_count") == 1
        and preview.get("current_expression")
        == configuration_plan.CURRENT_PORT_EXPRESSION
        and preview.get("proposed_expression")
        == configuration_plan.PROPOSED_PORT_EXPRESSION
        and source_text.count(configuration_plan.CURRENT_PORT_EXPRESSION) == 1
        and source_text.count(configuration_plan.PROPOSED_PORT_EXPRESSION) == 0
    ):
        raise ValueError("manual_action_preview_not_admissible")
    after_text = source_text.replace(
        configuration_plan.CURRENT_PORT_EXPRESSION,
        configuration_plan.PROPOSED_PORT_EXPRESSION,
        1,
    )
    after_raw = after_text.encode("utf-8")
    if not (
        preview.get("after_sha256") == _sha256(after_raw)
        and preview.get("after_bytes") == len(after_raw)
        and preview.get("forward_unified_diff_sha256")
        == _sha256(str(preview.get("forward_unified_diff") or "").encode("utf-8"))
        and preview.get("rollback_unified_diff_sha256")
        == _sha256(str(preview.get("rollback_unified_diff") or "").encode("utf-8"))
    ):
        raise ValueError("manual_action_preview_change_not_admissible")
    return dict(binding), dict(preview), generated_at, expires_at, after_raw


def build_manual_action_handoff(
    *,
    preview_artifact: Mapping[str, Any],
    preview_sha256: str,
    preview_verification: Mapping[str, Any],
    source_text: str,
    source_raw: bytes,
    source_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    binding, preview, generated_at, expires_at, after_raw = _preview_contract(
        preview_artifact,
        preview_sha256,
        preview_verification,
        source_text=source_text,
        source_raw=source_raw,
        source_sha256=source_sha256,
        now=observed_now,
    )
    handoff_binding = {
        **binding,
        "preview_id": str(preview_artifact.get("preview_id") or ""),
        "preview_sha256": preview_sha256,
    }
    target = {
        "path": configuration_plan.COMPOSE_PATH.as_posix(),
        "selector": "services.propertyquarry-api.ports[0]",
        "expected_before_sha256": source_sha256,
        "expected_after_sha256": _sha256(after_raw),
        "before_bytes": len(source_raw),
        "after_bytes": len(after_raw),
        "current_expression": configuration_plan.CURRENT_PORT_EXPRESSION,
        "proposed_expression": configuration_plan.PROPOSED_PORT_EXPRESSION,
        "replacement_count": 1,
        "forward_unified_diff_sha256": str(
            preview.get("forward_unified_diff_sha256") or ""
        ),
        "rollback_unified_diff_sha256": str(
            preview.get("rollback_unified_diff_sha256") or ""
        ),
    }
    actions = {
        "apply": {
            "operation": "replace_exact_text",
            "explicit_flag": "--apply",
            "expected_source_sha256": source_sha256,
            "result_source_sha256": _sha256(after_raw),
            "fresh_authorization_required": True,
        },
        "rollback": {
            "operation": "restore_exact_text",
            "explicit_flag": "--rollback",
            "expected_source_sha256": _sha256(after_raw),
            "result_source_sha256": source_sha256,
            "verified_apply_receipt_required": True,
        },
    }
    handoff_digest = _sha256(
        _canonical(
            {
                "binding": handoff_binding,
                "target": target,
                "actions": actions,
            }
        )
    )
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "handoff_id": f"pqmh_{handoff_digest[:24]}",
        "generated_at": generated_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "status": "exact_manual_action_ready",
        "next_action": (
            "invoke the exact apply command manually before expiry; retain its "
            "private receipt for an exact rollback"
        ),
        "binding": handoff_binding,
        "target": target,
        "actions": actions,
        "authorization": {
            "required": True,
            "recorded": True,
            "decision": "approve_exact_scope",
            "exact_scope_authorized": True,
        },
        "manual_apply_authorized": True,
        "manual_rollback_available": True,
        "manual_invocation_required": True,
        "automatic_execution_allowed": False,
        "source_edit_performed": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "secret_values_recorded": False,
    }
    artifact["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(artifact)),
    }
    if len(_canonical(artifact)) > MAX_HANDOFF_BYTES:
        raise ValueError("manual_action_handoff_too_large")
    return artifact


def _handoff_envelope(
    artifact: Mapping[str, Any],
    *,
    now: datetime,
    require_fresh: bool,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    normalized = dict(artifact)
    integrity = normalized.pop("integrity", None)
    digest = _sha256(_canonical(artifact))
    generated_at = _parse_timestamp(artifact.get("generated_at"))
    expires_at = _parse_timestamp(artifact.get("expires_at"))
    binding = artifact.get("binding")
    target = artifact.get("target")
    actions = artifact.get("actions")
    apply_action = actions.get("apply") if isinstance(actions, Mapping) else None
    rollback_action = (
        actions.get("rollback") if isinstance(actions, Mapping) else None
    )
    fresh = (
        generated_at is not None
        and expires_at is not None
        and generated_at <= now <= expires_at
    )
    if not (
        artifact.get("schema") == SCHEMA
        and _HANDOFF_ID.fullmatch(str(artifact.get("handoff_id") or ""))
        and isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
        and generated_at is not None
        and expires_at is not None
        and generated_at <= expires_at
        and (fresh or not require_fresh)
        and artifact.get("status") == "exact_manual_action_ready"
        and isinstance(binding, Mapping)
        and authorization_request._REQUEST_ID.fullmatch(
            str(binding.get("request_id") or "")
        )
        and authorization_request._SHA256.fullmatch(
            str(binding.get("request_sha256") or "")
        )
        and authorization_decision._DECISION_ID.fullmatch(
            str(binding.get("decision_id") or "")
        )
        and authorization_request._SHA256.fullmatch(
            str(binding.get("decision_sha256") or "")
        )
        and _PLAN_ID.fullmatch(
            str(binding.get("plan_id") or "")
        )
        and authorization_request._SHA256.fullmatch(
            str(binding.get("plan_sha256") or "")
        )
        and change_preview._PREVIEW_ID.fullmatch(
            str(binding.get("preview_id") or "")
        )
        and authorization_request._SHA256.fullmatch(
            str(binding.get("preview_sha256") or "")
        )
        and isinstance(target, Mapping)
        and target.get("path") == configuration_plan.COMPOSE_PATH.as_posix()
        and target.get("selector") == "services.propertyquarry-api.ports[0]"
        and authorization_request._SHA256.fullmatch(
            str(target.get("expected_before_sha256") or "")
        )
        and authorization_request._SHA256.fullmatch(
            str(target.get("expected_after_sha256") or "")
        )
        and target.get("current_expression")
        == configuration_plan.CURRENT_PORT_EXPRESSION
        and target.get("proposed_expression")
        == configuration_plan.PROPOSED_PORT_EXPRESSION
        and target.get("replacement_count") == 1
        and isinstance(apply_action, Mapping)
        and apply_action.get("operation") == "replace_exact_text"
        and apply_action.get("explicit_flag") == "--apply"
        and apply_action.get("expected_source_sha256")
        == target.get("expected_before_sha256")
        and apply_action.get("result_source_sha256")
        == target.get("expected_after_sha256")
        and apply_action.get("fresh_authorization_required") is True
        and isinstance(rollback_action, Mapping)
        and rollback_action.get("operation") == "restore_exact_text"
        and rollback_action.get("explicit_flag") == "--rollback"
        and rollback_action.get("expected_source_sha256")
        == target.get("expected_after_sha256")
        and rollback_action.get("result_source_sha256")
        == target.get("expected_before_sha256")
        and rollback_action.get("verified_apply_receipt_required") is True
        and artifact.get("authorization")
        == {
            "required": True,
            "recorded": True,
            "decision": "approve_exact_scope",
            "exact_scope_authorized": True,
        }
        and artifact.get("manual_apply_authorized") is True
        and artifact.get("manual_rollback_available") is True
        and artifact.get("manual_invocation_required") is True
        and artifact.get("automatic_execution_allowed") is False
        and artifact.get("source_edit_performed") is False
        and artifact.get("deployment_or_restart_authorized") is False
        and artifact.get("deployment_or_restart_performed") is False
        and artifact.get("provider_quota_consumption_allowed") is False
        and artifact.get("provider_quota_consumed") is False
        and artifact.get("delivery_authorized") is False
        and artifact.get("delivery_attempted") is False
        and artifact.get("secret_values_recorded") is False
    ):
        raise ValueError("manual_action_handoff_envelope_not_admissible")
    return dict(binding), dict(target), digest


def verify_manual_action_handoff(
    artifact: Mapping[str, Any],
    *,
    preview_artifact: Mapping[str, Any],
    preview_sha256: str,
    preview_verification: Mapping[str, Any],
    source_text: str,
    source_raw: bytes,
    source_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    replacement_attempted = False
    try:
        binding, target, digest = _handoff_envelope(
            artifact,
            now=observed_now,
            require_fresh=True,
        )
        generated_at = _parse_timestamp(artifact.get("generated_at"))
        if generated_at is None:
            raise ValueError("manual_action_handoff_timestamp_invalid")
        expected = build_manual_action_handoff(
            preview_artifact=preview_artifact,
            preview_sha256=preview_sha256,
            preview_verification=preview_verification,
            source_text=source_text,
            source_raw=source_raw,
            source_sha256=source_sha256,
            now=generated_at,
        )
        if dict(artifact) != expected:
            raise ValueError("manual_action_handoff_contract_mismatch")
    except (TypeError, ValueError):
        return _blocked(
            "manual_action_handoff_not_admissible",
            now=observed_now,
        )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": str(artifact.get("next_action") or ""),
        "handoff_id": str(artifact.get("handoff_id") or ""),
        "handoff_sha256": digest,
        "request_id": str(binding.get("request_id") or ""),
        "request_sha256": str(binding.get("request_sha256") or ""),
        "decision_id": str(binding.get("decision_id") or ""),
        "decision_sha256": str(binding.get("decision_sha256") or ""),
        "plan_id": str(binding.get("plan_id") or ""),
        "plan_sha256": str(binding.get("plan_sha256") or ""),
        "preview_id": str(binding.get("preview_id") or ""),
        "preview_sha256": str(binding.get("preview_sha256") or ""),
        "expires_at": str(artifact.get("expires_at") or ""),
        "target": target,
        "current_evidence_verified": False,
        "handoff_verified": True,
        "authorization_required": True,
        "authorization_recorded": True,
        "exact_scope_authorized": True,
        "manual_apply_authorized": True,
        "manual_rollback_authorized": True,
        "manual_invocation_required": True,
        "automatic_execution_allowed": False,
        "source_edit_performed": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
    }


def _preview_inputs(
    *,
    stage: bool,
    preview_dir: Path,
    plan_path: Path,
    request_path: Path,
    packet_path: Path,
    cycle_receipt_path: Path,
    signal_dir: Path,
    live_mobile_receipt_path: Path,
    release_manifest_path: Path,
    decision_dir: Path,
    project: str,
    root: Path,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any] | None, str, str, bytes, str]:
    preview_args = {
        "preview_dir": preview_dir,
        "plan_path": plan_path,
        "request_path": request_path,
        "packet_path": packet_path,
        "cycle_receipt_path": cycle_receipt_path,
        "signal_dir": signal_dir,
        "live_mobile_receipt_path": live_mobile_receipt_path,
        "release_manifest_path": release_manifest_path,
        "decision_dir": decision_dir,
        "project": project,
        "root": root,
        "now": now,
    }
    if stage:
        preview_verification = change_preview.stage_current_configuration_change_preview(
            **preview_args
        )
    else:
        preview_verification = change_preview.verify_current_configuration_change_preview(
            **preview_args
        )
    if preview_verification.get("status") not in {"ready", "verified"}:
        return preview_verification, None, "", "", b"", ""
    preview_path = str(preview_verification.get("preview_path") or "")
    preview_artifact, _raw, preview_sha256 = _private_object(
        Path(preview_path),
        field="runtime_configuration_change_preview",
        maximum_bytes=change_preview.MAX_PREVIEW_BYTES,
    )
    source_text, source_raw, source_sha256 = _source_snapshot(root=root)
    return (
        preview_verification,
        preview_artifact,
        preview_sha256,
        source_text,
        source_raw,
        source_sha256,
    )


def _handoff_commands(
    *,
    handoff_id: str,
    handoff_sha256: str,
) -> dict[str, str]:
    script = "scripts/propertyquarry_ooda_configuration_manual_action.py"
    apply_command = (
        f"python3 {script} --apply --operator-id \"$PROPERTYQUARRY_OPERATOR_ID\" "
        f"--handoff-id {shlex.quote(handoff_id)} "
        f"--handoff-sha256 {shlex.quote(handoff_sha256)}"
    )
    return {
        "apply_command": apply_command,
        "rollback_command_source": (
            "use the exact rollback_command emitted by the verified apply receipt"
        ),
    }


def _ready_staging(
    verification: Mapping[str, Any],
    *,
    handoff_path: Path,
    handoff_state: str,
    handoff_state_updated: bool,
    now: datetime,
) -> dict[str, Any]:
    if not (
        verification.get("status") == "verified"
        and verification.get("current_evidence_verified") is True
        and verification.get("handoff_verified") is True
        and verification.get("manual_apply_authorized") is True
        and verification.get("manual_rollback_authorized") is True
        and verification.get("manual_invocation_required") is True
        and verification.get("automatic_execution_allowed") is False
        and verification.get("source_edit_performed") is False
        and verification.get("deployment_or_restart_authorized") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and verification.get("delivery_authorized") is False
    ):
        return _blocked("manual_action_staging_not_admissible", now=now)
    result = {
        "schema": HANDOFF_SCHEMA,
        "status": "ready",
        "updated_at": now.isoformat(),
        "blocking_reason": "",
        "next_action": str(verification.get("next_action") or ""),
        "handoff_path": str(handoff_path),
        "handoff_state": handoff_state,
        "handoff_state_updated": handoff_state_updated,
        **{
            key: verification.get(key)
            for key in (
                "handoff_id",
                "handoff_sha256",
                "request_id",
                "request_sha256",
                "decision_id",
                "decision_sha256",
                "plan_id",
                "plan_sha256",
                "preview_id",
                "preview_sha256",
                "expires_at",
                "target",
                "current_evidence_verified",
                "handoff_verified",
                "authorization_required",
                "authorization_recorded",
                "exact_scope_authorized",
                "manual_apply_authorized",
                "manual_rollback_authorized",
                "manual_invocation_required",
                "automatic_execution_allowed",
                "source_edit_performed",
                "deployment_or_restart_authorized",
                "deployment_or_restart_performed",
                "provider_quota_consumption_allowed",
                "provider_quota_consumed",
                "delivery_authorized",
                "delivery_attempted",
            )
        },
    }
    result.update(
        _handoff_commands(
            handoff_id=str(verification.get("handoff_id") or ""),
            handoff_sha256=str(verification.get("handoff_sha256") or ""),
        )
    )
    return result


def stage_current_manual_action_handoff(
    *,
    handoff_dir: Path = DEFAULT_HANDOFF_DIR,
    preview_dir: Path = change_preview.DEFAULT_PREVIEW_DIR,
    plan_path: Path = configuration_plan.DEFAULT_PLAN_PATH,
    request_path: Path = authorization_request.DEFAULT_REQUEST_PATH,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    decision_dir: Path = authorization_decision.DEFAULT_DECISION_DIR,
    project: str = "property",
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Stage one immutable manual handoff without editing any source."""

    observed_now = _now(now)
    try:
        (
            preview_verification,
            preview_artifact,
            preview_sha256,
            source_text,
            source_raw,
            source_sha256,
        ) = _preview_inputs(
            stage=True,
            preview_dir=preview_dir,
            plan_path=plan_path,
            request_path=request_path,
            packet_path=packet_path,
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            live_mobile_receipt_path=live_mobile_receipt_path,
            release_manifest_path=release_manifest_path,
            decision_dir=decision_dir,
            project=project,
            root=root,
            now=observed_now,
        )
    except Exception:
        return _blocked("manual_action_current_evidence_unavailable", now=observed_now)
    if preview_verification.get("status") == "not_authorized":
        return _not_authorized(preview_verification, now=observed_now)
    if preview_verification.get("status") != "ready" or preview_artifact is None:
        return _blocked("manual_action_preview_not_ready", now=observed_now)
    try:
        expected = build_manual_action_handoff(
            preview_artifact=preview_artifact,
            preview_sha256=preview_sha256,
            preview_verification=preview_verification,
            source_text=source_text,
            source_raw=source_raw,
            source_sha256=source_sha256,
            now=observed_now,
        )
        handoff_path = _handoff_path(
            handoff_dir,
            str(expected.get("handoff_id") or ""),
        )
    except (OSError, TypeError, ValueError):
        return _blocked("manual_action_handoff_build_failed", now=observed_now)
    handoff_state = "refreshed"
    handoff_state_updated = True
    try:
        atomic_write_bytes(handoff_path, _canonical(expected), overwrite=False)
    except OutputExistsError:
        handoff_state = "reused"
        handoff_state_updated = False
    except Exception:
        return _blocked("manual_action_handoff_write_failed", now=observed_now)
    try:
        persisted, _raw, persisted_sha256 = _private_object(
            handoff_path,
            field="runtime_configuration_manual_action_handoff",
            maximum_bytes=MAX_HANDOFF_BYTES,
        )
    except Exception:
        return _blocked("manual_action_handoff_file_not_admissible", now=observed_now)
    verification = verify_manual_action_handoff(
        persisted,
        preview_artifact=preview_artifact,
        preview_sha256=preview_sha256,
        preview_verification=preview_verification,
        source_text=source_text,
        source_raw=source_raw,
        source_sha256=source_sha256,
        now=observed_now,
    )
    if not (
        (handoff_state == "reused" or persisted == expected)
        and verification.get("status") == "verified"
        and verification.get("handoff_sha256") == persisted_sha256
        and verification.get("handoff_id") == expected.get("handoff_id")
    ):
        return _blocked(
            "manual_action_handoff_persisted_not_verified",
            now=observed_now,
        )
    verification["current_evidence_verified"] = True
    return _ready_staging(
        verification,
        handoff_path=handoff_path,
        handoff_state=handoff_state,
        handoff_state_updated=handoff_state_updated,
        now=observed_now,
    )


def verify_current_manual_action_handoff(
    *,
    handoff_dir: Path = DEFAULT_HANDOFF_DIR,
    preview_dir: Path = change_preview.DEFAULT_PREVIEW_DIR,
    plan_path: Path = configuration_plan.DEFAULT_PLAN_PATH,
    request_path: Path = authorization_request.DEFAULT_REQUEST_PATH,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    decision_dir: Path = authorization_decision.DEFAULT_DECISION_DIR,
    project: str = "property",
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        (
            preview_verification,
            preview_artifact,
            preview_sha256,
            source_text,
            source_raw,
            source_sha256,
        ) = _preview_inputs(
            stage=False,
            preview_dir=preview_dir,
            plan_path=plan_path,
            request_path=request_path,
            packet_path=packet_path,
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            live_mobile_receipt_path=live_mobile_receipt_path,
            release_manifest_path=release_manifest_path,
            decision_dir=decision_dir,
            project=project,
            root=root,
            now=observed_now,
        )
        if preview_artifact is None:
            raise ValueError("manual_action_preview_not_current")
        expected = build_manual_action_handoff(
            preview_artifact=preview_artifact,
            preview_sha256=preview_sha256,
            preview_verification=preview_verification,
            source_text=source_text,
            source_raw=source_raw,
            source_sha256=source_sha256,
            now=observed_now,
        )
        handoff_path = _handoff_path(
            handoff_dir,
            str(expected.get("handoff_id") or ""),
        )
        persisted, _raw, persisted_sha256 = _private_object(
            handoff_path,
            field="runtime_configuration_manual_action_handoff",
            maximum_bytes=MAX_HANDOFF_BYTES,
        )
    except Exception:
        return _blocked("manual_action_current_evidence_unavailable", now=observed_now)
    verification = verify_manual_action_handoff(
        persisted,
        preview_artifact=preview_artifact,
        preview_sha256=preview_sha256,
        preview_verification=preview_verification,
        source_text=source_text,
        source_raw=source_raw,
        source_sha256=source_sha256,
        now=observed_now,
    )
    if not (
        verification.get("status") == "verified"
        and verification.get("handoff_sha256") == persisted_sha256
    ):
        return verification
    verification["current_evidence_verified"] = True
    verification["handoff_path"] = str(handoff_path)
    verification.update(
        _handoff_commands(
            handoff_id=str(verification.get("handoff_id") or ""),
            handoff_sha256=str(verification.get("handoff_sha256") or ""),
        )
    )
    return verification


def _execution_id(
    *,
    action: str,
    binding: Mapping[str, Any],
) -> str:
    prefix = "pqme" if action == "apply" else "pqmr"
    digest = _sha256(_canonical({"action": action, "binding": dict(binding)}))
    return f"{prefix}_{digest[:24]}"


def _build_execution_receipt(
    *,
    action: str,
    status: str,
    execution_id: str,
    operator_id: str,
    binding: Mapping[str, Any],
    target: Mapping[str, Any],
    prepared_at: datetime,
    completed_at: datetime | None,
) -> dict[str, Any]:
    performed = status in {"applied", "rolled_back"}
    receipt: dict[str, Any] = {
        "schema": EXECUTION_SCHEMA,
        "execution_id": execution_id,
        "action": action,
        "status": status,
        "prepared_at": prepared_at.isoformat(),
        "completed_at": completed_at.isoformat() if completed_at else "",
        "operator_id": operator_id,
        "binding": dict(binding),
        "target": dict(target),
        "manual_invocation_verified": True,
        "automatic_execution_allowed": False,
        "source_edit_performed": performed,
        "protected_operation_executed": performed,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "secret_values_recorded": False,
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(receipt)),
    }
    if len(_canonical(receipt)) > MAX_EXECUTION_BYTES:
        raise ValueError("manual_action_execution_receipt_too_large")
    return receipt


def _execution_envelope(
    receipt: Mapping[str, Any],
    *,
    action: str,
    binding: Mapping[str, Any],
    target: Mapping[str, Any],
    operator_id: str,
) -> tuple[str, str, datetime, str]:
    normalized = dict(receipt)
    integrity = normalized.pop("integrity", None)
    execution_id = str(receipt.get("execution_id") or "")
    status = str(receipt.get("status") or "")
    prepared_at = _parse_timestamp(receipt.get("prepared_at"))
    completed_at = _parse_timestamp(receipt.get("completed_at"))
    expected_statuses = (
        {"prepared", "applied"} if action == "apply" else {"prepared", "rolled_back"}
    )
    id_pattern = _APPLY_ID if action == "apply" else _ROLLBACK_ID
    performed = status in {"applied", "rolled_back"}
    if not (
        receipt.get("schema") == EXECUTION_SCHEMA
        and id_pattern.fullmatch(execution_id)
        and receipt.get("action") == action
        and status in expected_statuses
        and prepared_at is not None
        and ((not performed and completed_at is None) or (performed and completed_at is not None))
        and receipt.get("operator_id") == operator_id
        and authorization_decision._DECIDER_ID.fullmatch(operator_id)
        and receipt.get("binding") == dict(binding)
        and receipt.get("target") == dict(target)
        and isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
        and receipt.get("manual_invocation_verified") is True
        and receipt.get("automatic_execution_allowed") is False
        and receipt.get("source_edit_performed") is performed
        and receipt.get("protected_operation_executed") is performed
        and receipt.get("deployment_or_restart_authorized") is False
        and receipt.get("deployment_or_restart_performed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("provider_quota_consumed") is False
        and receipt.get("delivery_authorized") is False
        and receipt.get("delivery_attempted") is False
        and receipt.get("secret_values_recorded") is False
    ):
        raise ValueError("manual_action_execution_receipt_not_admissible")
    return execution_id, status, prepared_at, _sha256(_canonical(receipt))


def _source_pair(
    *,
    root: Path,
    target: Mapping[str, Any],
) -> tuple[bytes, bytes, str]:
    source_text, source_raw, source_sha256 = _source_snapshot(root=root)
    current = str(target.get("current_expression") or "")
    proposed = str(target.get("proposed_expression") or "")
    if source_sha256 == target.get("expected_before_sha256"):
        if not (source_text.count(current) == 1 and source_text.count(proposed) == 0):
            raise ValueError("manual_action_source_before_not_admissible")
        after_raw = source_text.replace(current, proposed, 1).encode("utf-8")
        before_raw = source_raw
    elif source_sha256 == target.get("expected_after_sha256"):
        if not (source_text.count(current) == 0 and source_text.count(proposed) == 1):
            raise ValueError("manual_action_source_after_not_admissible")
        before_raw = source_text.replace(proposed, current, 1).encode("utf-8")
        after_raw = source_raw
    else:
        raise ValueError("manual_action_source_drifted")
    if not (
        _sha256(before_raw) == target.get("expected_before_sha256")
        and _sha256(after_raw) == target.get("expected_after_sha256")
        and len(before_raw) == target.get("before_bytes")
        and len(after_raw) == target.get("after_bytes")
    ):
        raise ValueError("manual_action_source_pair_not_admissible")
    return before_raw, after_raw, source_sha256


def _action_result(
    *,
    status: str,
    action: str,
    execution_id: str,
    execution_path: Path,
    execution_sha256: str,
    handoff_id: str,
    handoff_sha256: str,
    source_before_sha256: str,
    source_after_sha256: str,
    source_edit_performed: bool,
    now: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "status": status,
        "action": action,
        "updated_at": now.isoformat(),
        "blocking_reason": "",
        "execution_id": execution_id,
        "execution_path": str(execution_path),
        "execution_sha256": execution_sha256,
        "handoff_id": handoff_id,
        "handoff_sha256": handoff_sha256,
        "source_before_sha256": source_before_sha256,
        "source_after_sha256": source_after_sha256,
        "source_edit_performed": source_edit_performed,
        "protected_operation_executed": source_edit_performed,
        "manual_invocation_verified": True,
        "automatic_execution_allowed": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
    }
    if action == "apply" and status in {"applied", "unchanged"}:
        script = "scripts/propertyquarry_ooda_configuration_manual_action.py"
        result["rollback_command"] = (
            f"python3 {script} --rollback "
            "--operator-id \"$PROPERTYQUARRY_OPERATOR_ID\" "
            f"--execution-id {shlex.quote(execution_id)} "
            f"--execution-sha256 {shlex.quote(execution_sha256)}"
        )
    return result


def execute_manual_apply(
    *,
    expected_handoff_id: str,
    expected_handoff_sha256: str,
    operator_id: str,
    handoff_dir: Path = DEFAULT_HANDOFF_DIR,
    receipt_dir: Path = DEFAULT_RECEIPT_DIR,
    preview_dir: Path = change_preview.DEFAULT_PREVIEW_DIR,
    plan_path: Path = configuration_plan.DEFAULT_PLAN_PATH,
    request_path: Path = authorization_request.DEFAULT_REQUEST_PATH,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    decision_dir: Path = authorization_decision.DEFAULT_DECISION_DIR,
    project: str = "property",
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    normalized_operator = str(operator_id or "").strip()
    try:
        if not (
            _HANDOFF_ID.fullmatch(expected_handoff_id)
            and authorization_request._SHA256.fullmatch(expected_handoff_sha256)
            and authorization_decision._DECIDER_ID.fullmatch(normalized_operator)
        ):
            raise ValueError("manual_action_invocation_not_admissible")
        handoff_path = _handoff_path(handoff_dir, expected_handoff_id)
        handoff, _raw, handoff_sha256 = _private_object(
            handoff_path,
            field="runtime_configuration_manual_action_handoff",
            maximum_bytes=MAX_HANDOFF_BYTES,
        )
        binding, target, envelope_sha256 = _handoff_envelope(
            handoff,
            now=observed_now,
            require_fresh=False,
        )
        if not (
            handoff_sha256 == expected_handoff_sha256 == envelope_sha256
            and handoff.get("handoff_id") == expected_handoff_id
        ):
            raise ValueError("manual_action_handoff_binding_mismatch")
        receipt_binding = {
            "handoff_id": expected_handoff_id,
            "handoff_sha256": handoff_sha256,
            "preview_id": str(binding.get("preview_id") or ""),
            "preview_sha256": str(binding.get("preview_sha256") or ""),
        }
        execution_id = _execution_id(action="apply", binding=receipt_binding)
        receipt_path = _execution_path(receipt_dir, execution_id)
    except Exception:
        return _blocked("manual_apply_invocation_not_admissible", now=observed_now)

    existing: dict[str, Any] | None = None
    try:
        existing, _raw, existing_sha256 = _private_object(
            receipt_path,
            field="runtime_configuration_manual_apply_receipt",
            maximum_bytes=MAX_EXECUTION_BYTES,
        )
    except FileNotFoundError:
        existing_sha256 = ""
    except Exception:
        return _blocked("manual_apply_receipt_not_admissible", now=observed_now)
    if existing is not None:
        try:
            _, existing_status, prepared_at, verified_sha256 = _execution_envelope(
                existing,
                action="apply",
                binding=receipt_binding,
                target=target,
                operator_id=normalized_operator,
            )
            before_raw, after_raw, current_sha256 = _source_pair(
                root=root,
                target=target,
            )
            if existing_sha256 != verified_sha256:
                raise ValueError("manual_apply_receipt_digest_mismatch")
            if existing_status == "applied" and current_sha256 == target.get(
                "expected_after_sha256"
            ):
                return _action_result(
                    status="unchanged",
                    action="apply",
                    execution_id=execution_id,
                    execution_path=receipt_path,
                    execution_sha256=existing_sha256,
                    handoff_id=expected_handoff_id,
                    handoff_sha256=handoff_sha256,
                    source_before_sha256=str(target.get("expected_before_sha256") or ""),
                    source_after_sha256=current_sha256,
                    source_edit_performed=False,
                    now=observed_now,
                )
            if existing_status != "prepared":
                raise ValueError("manual_apply_receipt_state_mismatch")
            if current_sha256 == target.get("expected_after_sha256"):
                completed = _build_execution_receipt(
                    action="apply",
                    status="applied",
                    execution_id=execution_id,
                    operator_id=normalized_operator,
                    binding=receipt_binding,
                    target=target,
                    prepared_at=prepared_at,
                    completed_at=observed_now,
                )
                atomic_write_bytes(receipt_path, _canonical(completed), overwrite=True)
                return _action_result(
                    status="applied",
                    action="apply",
                    execution_id=execution_id,
                    execution_path=receipt_path,
                    execution_sha256=_sha256(_canonical(completed)),
                    handoff_id=expected_handoff_id,
                    handoff_sha256=handoff_sha256,
                    source_before_sha256=str(target.get("expected_before_sha256") or ""),
                    source_after_sha256=current_sha256,
                    source_edit_performed=False,
                    now=observed_now,
                )
            if current_sha256 != target.get("expected_before_sha256"):
                raise ValueError("manual_apply_source_state_mismatch")
        except Exception:
            return _blocked("manual_apply_reconciliation_blocked", now=observed_now)
    else:
        prepared_at = observed_now

    current_verification = verify_current_manual_action_handoff(
        handoff_dir=handoff_dir,
        preview_dir=preview_dir,
        plan_path=plan_path,
        request_path=request_path,
        packet_path=packet_path,
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=signal_dir,
        live_mobile_receipt_path=live_mobile_receipt_path,
        release_manifest_path=release_manifest_path,
        decision_dir=decision_dir,
        project=project,
        root=root,
        now=observed_now,
    )
    if not (
        current_verification.get("status") == "verified"
        and current_verification.get("current_evidence_verified") is True
        and current_verification.get("handoff_id") == expected_handoff_id
        and current_verification.get("handoff_sha256") == expected_handoff_sha256
        and current_verification.get("manual_apply_authorized") is True
        and current_verification.get("automatic_execution_allowed") is False
    ):
        return _blocked("manual_apply_current_authority_not_verified", now=observed_now)
    replacement_attempted = False
    try:
        before_raw, after_raw, current_sha256 = _source_pair(root=root, target=target)
        if current_sha256 != target.get("expected_before_sha256"):
            raise ValueError("manual_apply_source_not_at_before_state")
        if existing is None:
            prepared = _build_execution_receipt(
                action="apply",
                status="prepared",
                execution_id=execution_id,
                operator_id=normalized_operator,
                binding=receipt_binding,
                target=target,
                prepared_at=prepared_at,
                completed_at=None,
            )
            try:
                atomic_write_bytes(receipt_path, _canonical(prepared), overwrite=False)
            except OutputExistsError:
                return _blocked("manual_apply_concurrent_invocation", now=observed_now)
        replacement_attempted = True
        atomic_replace_bytes_if_matches(
            root / configuration_plan.COMPOSE_PATH,
            expected=before_raw,
            replacement=after_raw,
            maximum_bytes=configuration_plan.MAX_SOURCE_BYTES,
        )
        _text, _raw, applied_sha256 = _source_snapshot(root=root)
        if applied_sha256 != target.get("expected_after_sha256"):
            return _blocked(
                "manual_apply_postcondition_not_verified",
                now=observed_now,
                source_edit_performed=True,
            )
        completed = _build_execution_receipt(
            action="apply",
            status="applied",
            execution_id=execution_id,
            operator_id=normalized_operator,
            binding=receipt_binding,
            target=target,
            prepared_at=prepared_at,
            completed_at=observed_now,
        )
        try:
            atomic_write_bytes(receipt_path, _canonical(completed), overwrite=True)
        except Exception:
            return _blocked(
                "manual_apply_receipt_finalize_failed",
                now=observed_now,
                source_edit_performed=True,
            )
    except Exception:
        edit_state: bool | None = False
        if replacement_attempted:
            try:
                _text, _raw, observed_sha256 = _source_snapshot(root=root)
            except Exception:
                edit_state = None
            else:
                if observed_sha256 == target.get("expected_after_sha256"):
                    edit_state = True
                elif observed_sha256 != target.get("expected_before_sha256"):
                    edit_state = None
        return _blocked(
            "manual_apply_failed_closed",
            now=observed_now,
            source_edit_performed=edit_state,
        )
    return _action_result(
        status="applied",
        action="apply",
        execution_id=execution_id,
        execution_path=receipt_path,
        execution_sha256=_sha256(_canonical(completed)),
        handoff_id=expected_handoff_id,
        handoff_sha256=handoff_sha256,
        source_before_sha256=str(target.get("expected_before_sha256") or ""),
        source_after_sha256=str(target.get("expected_after_sha256") or ""),
        source_edit_performed=True,
        now=observed_now,
    )


def _build_dotenv_rollback_receipt(
    *,
    status: str,
    execution_id: str,
    operator_id: str,
    binding: Mapping[str, Any],
    target: Mapping[str, Any],
    prepared_at: datetime,
    completed_at: datetime | None,
) -> dict[str, Any]:
    performed = status == "rolled_back"
    receipt: dict[str, Any] = {
        "schema": DOTENV_ROLLBACK_SCHEMA,
        "execution_id": execution_id,
        "action": "rollback",
        "status": status,
        "prepared_at": prepared_at.isoformat(),
        "completed_at": completed_at.isoformat() if completed_at else "",
        "operator_id": operator_id,
        "binding": dict(binding),
        "target": dict(target),
        "manual_invocation_verified": True,
        "automatic_execution_allowed": False,
        "runtime_configuration_change_performed": performed,
        "source_edit_performed": False,
        "protected_operation_executed": performed,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(receipt)),
    }
    return receipt


def _dotenv_rollback_receipt_envelope(
    receipt: Mapping[str, Any],
    *,
    apply_receipt: Mapping[str, Any],
    apply_receipt_sha256: str,
) -> tuple[str, str, datetime, str]:
    normalized = dict(receipt)
    integrity = normalized.pop("integrity", None)
    execution_id = str(receipt.get("execution_id") or "")
    status = str(receipt.get("status") or "")
    prepared_at = _parse_timestamp(receipt.get("prepared_at"))
    completed_at = _parse_timestamp(receipt.get("completed_at"))
    binding = receipt.get("binding")
    target = receipt.get("target")
    apply_rollback = dict(apply_receipt.get("rollback") or {})
    expected_keys = sorted(runtime_review._LOCAL_CANDIDATE_KEYS)
    performed = status == "rolled_back"
    if not (
        receipt.get("schema") == DOTENV_ROLLBACK_SCHEMA
        and _ROLLBACK_ID.fullmatch(execution_id)
        and receipt.get("action") == "rollback"
        and status in {"prepared", "rolled_back"}
        and prepared_at is not None
        and ((status == "prepared" and completed_at is None) or (performed and completed_at is not None))
        and authorization_decision._DECIDER_ID.fullmatch(
            str(receipt.get("operator_id") or "")
        )
        and isinstance(binding, Mapping)
        and binding
        == {
            "apply_execution_id": str(apply_receipt.get("execution_id") or ""),
            "apply_execution_sha256": apply_receipt_sha256,
        }
        and isinstance(target, Mapping)
        and target
        == {
            "path": runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH.as_posix(),
            "rollback_snapshot_path": str(
                apply_rollback.get("snapshot_path") or ""
            ),
            "removed_keys": expected_keys,
            "removed_key_count": len(expected_keys),
            "restored_private_pre_apply_snapshot": True,
            "target_file_mode": 0o600,
            "rollback_snapshot_file_mode": 0o600,
        }
        and isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
        and receipt.get("manual_invocation_verified") is True
        and receipt.get("automatic_execution_allowed") is False
        and receipt.get("runtime_configuration_change_performed") is performed
        and receipt.get("source_edit_performed") is False
        and receipt.get("protected_operation_executed") is performed
        and receipt.get("deployment_or_restart_authorized") is False
        and receipt.get("deployment_or_restart_performed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("provider_quota_consumed") is False
        and receipt.get("delivery_authorized") is False
        and receipt.get("delivery_attempted") is False
        and receipt.get("environment_values_recorded") is False
        and receipt.get("environment_values_hashed") is False
        and receipt.get("secret_values_recorded") is False
    ):
        raise ValueError("dotenv_rollback_receipt_not_admissible")
    expected_id = _execution_id(action="rollback", binding=dict(binding))
    if execution_id != expected_id:
        raise ValueError("dotenv_rollback_receipt_id_mismatch")
    return execution_id, status, prepared_at, _sha256(_canonical(receipt))


def verify_dotenv_rollback_receipt_state(
    receipt: Mapping[str, Any],
    *,
    apply_receipt: Mapping[str, Any],
    apply_receipt_sha256: str,
    root: Path = ROOT,
) -> dict[str, Any]:
    execution_id, status, prepared_at, receipt_sha256 = (
        _dotenv_rollback_receipt_envelope(
            receipt,
            apply_receipt=apply_receipt,
            apply_receipt_sha256=apply_receipt_sha256,
        )
    )
    rollback = dict(apply_receipt.get("rollback") or {})
    target_path = runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH
    snapshot_path = _dotenv_relative_path(
        rollback.get("snapshot_path"),
        field="dotenv_rollback_snapshot",
    )
    current_raw = _dotenv_private_bytes(
        target_path,
        root=root,
        field="dotenv_target",
    )
    snapshot_raw = _dotenv_private_bytes(
        snapshot_path,
        root=root,
        field="dotenv_rollback_snapshot",
    )
    if status == "rolled_back" and current_raw != snapshot_raw:
        raise ValueError("dotenv_rollback_current_state_not_admissible")
    state_binding = {
        "kind": "dotenv_rollback_snapshot_restore",
        "target_path": target_path.as_posix(),
        "rollback_snapshot_path": snapshot_path.as_posix(),
        "removed_keys": sorted(runtime_review._LOCAL_CANDIDATE_KEYS),
        "removed_key_count": len(runtime_review._LOCAL_CANDIDATE_KEYS),
        "target_file_mode": 0o600,
        "rollback_snapshot_file_mode": 0o600,
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
    }
    return {
        "status": "verified",
        "execution_id": execution_id,
        "action_status": status,
        "prepared_at": prepared_at.isoformat(),
        "receipt_sha256": receipt_sha256,
        "state_binding": state_binding,
        "source_state_verification_sha256": _sha256(_canonical(state_binding)),
        "current_evidence_verified": status == "rolled_back",
        "receipt_verified": True,
        "source_state_verified": status == "rolled_back",
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
    }


def _execute_dotenv_manual_rollback(
    *,
    apply_receipt: Mapping[str, Any],
    apply_receipt_sha256: str,
    operator_id: str,
    receipt_dir: Path,
    root: Path,
    now: datetime,
) -> dict[str, Any]:
    try:
        _binding, _target, rollback, verified_apply_sha256 = (
            _dotenv_apply_receipt_envelope(apply_receipt)
        )
        if apply_receipt_sha256 != verified_apply_sha256:
            raise ValueError("dotenv_rollback_apply_receipt_digest_mismatch")
        receipt_binding = {
            "apply_execution_id": str(apply_receipt.get("execution_id") or ""),
            "apply_execution_sha256": apply_receipt_sha256,
        }
        execution_id = _execution_id(action="rollback", binding=receipt_binding)
        receipt_path = _execution_path(receipt_dir, execution_id)
        snapshot_path = _dotenv_relative_path(
            rollback.get("snapshot_path"),
            field="dotenv_rollback_snapshot",
        )
        rollback_target = {
            "path": runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH.as_posix(),
            "rollback_snapshot_path": snapshot_path.as_posix(),
            "removed_keys": sorted(runtime_review._LOCAL_CANDIDATE_KEYS),
            "removed_key_count": len(runtime_review._LOCAL_CANDIDATE_KEYS),
            "restored_private_pre_apply_snapshot": True,
            "target_file_mode": 0o600,
            "rollback_snapshot_file_mode": 0o600,
        }
    except Exception:
        return _blocked("manual_rollback_invocation_not_admissible", now=now)

    try:
        existing, _raw, existing_sha256 = _private_object(
            receipt_path,
            field="runtime_configuration_dotenv_rollback_receipt",
            maximum_bytes=MAX_EXECUTION_BYTES,
        )
    except FileNotFoundError:
        existing = None
        existing_sha256 = ""
    except Exception:
        return _blocked("manual_rollback_receipt_not_admissible", now=now)
    if existing is not None:
        try:
            verification = verify_dotenv_rollback_receipt_state(
                existing,
                apply_receipt=apply_receipt,
                apply_receipt_sha256=apply_receipt_sha256,
                root=root,
            )
            if existing_sha256 != verification.get("receipt_sha256"):
                raise ValueError("dotenv_rollback_receipt_digest_mismatch")
            if existing.get("status") == "rolled_back":
                return {
                    **_action_result(
                        status="unchanged",
                        action="rollback",
                        execution_id=execution_id,
                        execution_path=receipt_path,
                        execution_sha256=existing_sha256,
                        handoff_id="",
                        handoff_sha256="",
                        source_before_sha256="",
                        source_after_sha256="",
                        source_edit_performed=False,
                        now=now,
                    ),
                    "runtime_configuration_change_performed": False,
                    "environment_values_recorded": False,
                    "environment_values_hashed": False,
                }
        except Exception:
            return _blocked("manual_rollback_reconciliation_blocked", now=now)
        prepared_at = _parse_timestamp(existing.get("prepared_at"))
        if prepared_at is None:
            return _blocked("manual_rollback_reconciliation_blocked", now=now)
    else:
        prepared_at = now

    try:
        _state_binding, current_raw, snapshot_raw, _digest = _dotenv_apply_state(
            apply_receipt,
            root=root,
        )
        if existing is None:
            prepared = _build_dotenv_rollback_receipt(
                status="prepared",
                execution_id=execution_id,
                operator_id=operator_id,
                binding=receipt_binding,
                target=rollback_target,
                prepared_at=prepared_at,
                completed_at=None,
            )
            atomic_write_bytes(receipt_path, _canonical(prepared), overwrite=False)
        atomic_replace_bytes_if_matches(
            root / runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH,
            expected=current_raw,
            replacement=snapshot_raw,
            maximum_bytes=MAX_DOTENV_BYTES,
        )
        completed = _build_dotenv_rollback_receipt(
            status="rolled_back",
            execution_id=execution_id,
            operator_id=operator_id,
            binding=receipt_binding,
            target=rollback_target,
            prepared_at=prepared_at,
            completed_at=now,
        )
        atomic_write_bytes(receipt_path, _canonical(completed), overwrite=True)
        verification = verify_dotenv_rollback_receipt_state(
            completed,
            apply_receipt=apply_receipt,
            apply_receipt_sha256=apply_receipt_sha256,
            root=root,
        )
        if verification.get("status") != "verified":
            raise ValueError("dotenv_rollback_postcondition_not_verified")
    except Exception:
        return _blocked(
            "manual_rollback_failed_closed",
            now=now,
            source_edit_performed=None,
        )
    return {
        **_action_result(
            status="rolled_back",
            action="rollback",
            execution_id=execution_id,
            execution_path=receipt_path,
            execution_sha256=str(verification.get("receipt_sha256") or ""),
            handoff_id="",
            handoff_sha256="",
            source_before_sha256="",
            source_after_sha256="",
            source_edit_performed=True,
            now=now,
        ),
        "source_edit_performed": False,
        "runtime_configuration_change_performed": True,
        "protected_operation_executed": True,
        "environment_values_recorded": False,
        "environment_values_hashed": False,
    }


def execute_manual_rollback(
    *,
    expected_execution_id: str,
    expected_execution_sha256: str,
    operator_id: str,
    handoff_dir: Path = DEFAULT_HANDOFF_DIR,
    receipt_dir: Path = DEFAULT_RECEIPT_DIR,
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    normalized_operator = str(operator_id or "").strip()
    replacement_attempted = False
    try:
        if not (
            _APPLY_ID.fullmatch(expected_execution_id)
            and authorization_request._SHA256.fullmatch(expected_execution_sha256)
            and authorization_decision._DECIDER_ID.fullmatch(normalized_operator)
        ):
            raise ValueError("manual_rollback_invocation_not_admissible")
        initial_apply_path = _execution_path(receipt_dir, expected_execution_id)
        initial_apply_receipt, _raw, initial_apply_sha256 = _private_object(
            initial_apply_path,
            field="runtime_configuration_manual_apply_receipt",
            maximum_bytes=MAX_EXECUTION_BYTES,
        )
    except Exception:
        return _blocked("manual_rollback_invocation_not_admissible", now=observed_now)
    if initial_apply_receipt.get("schema") == DOTENV_EXECUTION_SCHEMA:
        if initial_apply_sha256 != expected_execution_sha256:
            return _blocked("manual_rollback_invocation_not_admissible", now=observed_now)
        return _execute_dotenv_manual_rollback(
            apply_receipt=initial_apply_receipt,
            apply_receipt_sha256=initial_apply_sha256,
            operator_id=normalized_operator,
            receipt_dir=receipt_dir,
            root=root,
            now=observed_now,
        )
    try:
        if not (
            _APPLY_ID.fullmatch(expected_execution_id)
            and authorization_request._SHA256.fullmatch(expected_execution_sha256)
            and authorization_decision._DECIDER_ID.fullmatch(normalized_operator)
        ):
            raise ValueError("manual_rollback_invocation_not_admissible")
        apply_path = _execution_path(receipt_dir, expected_execution_id)
        apply_receipt, _raw, apply_sha256 = _private_object(
            apply_path,
            field="runtime_configuration_manual_apply_receipt",
            maximum_bytes=MAX_EXECUTION_BYTES,
        )
        apply_binding = dict(apply_receipt.get("binding") or {})
        handoff_id = str(apply_binding.get("handoff_id") or "")
        handoff_sha256 = str(apply_binding.get("handoff_sha256") or "")
        handoff_path = _handoff_path(handoff_dir, handoff_id)
        handoff, _raw, persisted_handoff_sha256 = _private_object(
            handoff_path,
            field="runtime_configuration_manual_action_handoff",
            maximum_bytes=MAX_HANDOFF_BYTES,
        )
        _binding, target, envelope_handoff_sha256 = _handoff_envelope(
            handoff,
            now=observed_now,
            require_fresh=False,
        )
        if not (
            apply_sha256 == expected_execution_sha256
            and handoff_sha256
            == persisted_handoff_sha256
            == envelope_handoff_sha256
        ):
            raise ValueError("manual_rollback_binding_mismatch")
        _execution_envelope(
            apply_receipt,
            action="apply",
            binding=apply_binding,
            target=target,
            operator_id=str(apply_receipt.get("operator_id") or ""),
        )
        if apply_receipt.get("status") != "applied":
            raise ValueError("manual_rollback_apply_not_complete")
        rollback_binding = {
            "apply_execution_id": expected_execution_id,
            "apply_execution_sha256": apply_sha256,
            "handoff_id": handoff_id,
            "handoff_sha256": handoff_sha256,
        }
        rollback_id = _execution_id(action="rollback", binding=rollback_binding)
        rollback_path = _execution_path(receipt_dir, rollback_id)
    except Exception:
        return _blocked("manual_rollback_invocation_not_admissible", now=observed_now)

    existing: dict[str, Any] | None = None
    try:
        existing, _raw, existing_sha256 = _private_object(
            rollback_path,
            field="runtime_configuration_manual_rollback_receipt",
            maximum_bytes=MAX_EXECUTION_BYTES,
        )
    except FileNotFoundError:
        existing_sha256 = ""
    except Exception:
        return _blocked("manual_rollback_receipt_not_admissible", now=observed_now)
    if existing is not None:
        try:
            _, existing_status, prepared_at, verified_sha256 = _execution_envelope(
                existing,
                action="rollback",
                binding=rollback_binding,
                target=target,
                operator_id=normalized_operator,
            )
            before_raw, after_raw, current_sha256 = _source_pair(
                root=root,
                target=target,
            )
            if existing_sha256 != verified_sha256:
                raise ValueError("manual_rollback_receipt_digest_mismatch")
            if existing_status == "rolled_back" and current_sha256 == target.get(
                "expected_before_sha256"
            ):
                return _action_result(
                    status="unchanged",
                    action="rollback",
                    execution_id=rollback_id,
                    execution_path=rollback_path,
                    execution_sha256=existing_sha256,
                    handoff_id=handoff_id,
                    handoff_sha256=handoff_sha256,
                    source_before_sha256=str(target.get("expected_after_sha256") or ""),
                    source_after_sha256=current_sha256,
                    source_edit_performed=False,
                    now=observed_now,
                )
            if existing_status != "prepared":
                raise ValueError("manual_rollback_receipt_state_mismatch")
            if current_sha256 == target.get("expected_before_sha256"):
                completed = _build_execution_receipt(
                    action="rollback",
                    status="rolled_back",
                    execution_id=rollback_id,
                    operator_id=normalized_operator,
                    binding=rollback_binding,
                    target=target,
                    prepared_at=prepared_at,
                    completed_at=observed_now,
                )
                atomic_write_bytes(rollback_path, _canonical(completed), overwrite=True)
                return _action_result(
                    status="rolled_back",
                    action="rollback",
                    execution_id=rollback_id,
                    execution_path=rollback_path,
                    execution_sha256=_sha256(_canonical(completed)),
                    handoff_id=handoff_id,
                    handoff_sha256=handoff_sha256,
                    source_before_sha256=str(target.get("expected_after_sha256") or ""),
                    source_after_sha256=current_sha256,
                    source_edit_performed=False,
                    now=observed_now,
                )
            if current_sha256 != target.get("expected_after_sha256"):
                raise ValueError("manual_rollback_source_state_mismatch")
        except Exception:
            return _blocked("manual_rollback_reconciliation_blocked", now=observed_now)
    else:
        prepared_at = observed_now

    try:
        before_raw, after_raw, current_sha256 = _source_pair(root=root, target=target)
        if current_sha256 != target.get("expected_after_sha256"):
            raise ValueError("manual_rollback_source_not_at_after_state")
        if existing is None:
            prepared = _build_execution_receipt(
                action="rollback",
                status="prepared",
                execution_id=rollback_id,
                operator_id=normalized_operator,
                binding=rollback_binding,
                target=target,
                prepared_at=prepared_at,
                completed_at=None,
            )
            try:
                atomic_write_bytes(rollback_path, _canonical(prepared), overwrite=False)
            except OutputExistsError:
                return _blocked("manual_rollback_concurrent_invocation", now=observed_now)
        replacement_attempted = True
        atomic_replace_bytes_if_matches(
            root / configuration_plan.COMPOSE_PATH,
            expected=after_raw,
            replacement=before_raw,
            maximum_bytes=configuration_plan.MAX_SOURCE_BYTES,
        )
        _text, _raw, rolled_back_sha256 = _source_snapshot(root=root)
        if rolled_back_sha256 != target.get("expected_before_sha256"):
            return _blocked(
                "manual_rollback_postcondition_not_verified",
                now=observed_now,
                source_edit_performed=True,
            )
        completed = _build_execution_receipt(
            action="rollback",
            status="rolled_back",
            execution_id=rollback_id,
            operator_id=normalized_operator,
            binding=rollback_binding,
            target=target,
            prepared_at=prepared_at,
            completed_at=observed_now,
        )
        try:
            atomic_write_bytes(rollback_path, _canonical(completed), overwrite=True)
        except Exception:
            return _blocked(
                "manual_rollback_receipt_finalize_failed",
                now=observed_now,
                source_edit_performed=True,
            )
    except Exception:
        edit_state: bool | None = False
        if replacement_attempted:
            try:
                _text, _raw, observed_sha256 = _source_snapshot(root=root)
            except Exception:
                edit_state = None
            else:
                if observed_sha256 == target.get("expected_before_sha256"):
                    edit_state = True
                elif observed_sha256 != target.get("expected_after_sha256"):
                    edit_state = None
        return _blocked(
            "manual_rollback_failed_closed",
            now=observed_now,
            source_edit_performed=edit_state,
        )
    return _action_result(
        status="rolled_back",
        action="rollback",
        execution_id=rollback_id,
        execution_path=rollback_path,
        execution_sha256=_sha256(_canonical(completed)),
        handoff_id=handoff_id,
        handoff_sha256=handoff_sha256,
        source_before_sha256=str(target.get("expected_after_sha256") or ""),
        source_after_sha256=str(target.get("expected_before_sha256") or ""),
        source_edit_performed=True,
        now=observed_now,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage or explicitly invoke one approved reversible PropertyQuarry "
            "source edit. This command never deploys, restarts, calls providers, "
            "consumes quota, or sends."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-current", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    parser.add_argument("--operator-id")
    parser.add_argument("--handoff-id")
    parser.add_argument("--handoff-sha256")
    parser.add_argument("--execution-id")
    parser.add_argument("--execution-sha256")
    parser.add_argument("--handoff-dir", type=Path, default=DEFAULT_HANDOFF_DIR)
    parser.add_argument("--receipt-dir", type=Path, default=DEFAULT_RECEIPT_DIR)
    parser.add_argument("--preview-dir", type=Path, default=change_preview.DEFAULT_PREVIEW_DIR)
    parser.add_argument("--plan", type=Path, default=configuration_plan.DEFAULT_PLAN_PATH)
    parser.add_argument("--request", type=Path, default=authorization_request.DEFAULT_REQUEST_PATH)
    parser.add_argument("--packet", type=Path, default=runtime_review.DEFAULT_PACKET_PATH)
    parser.add_argument("--cycle-receipt", type=Path, default=runtime_review.DEFAULT_CYCLE_RECEIPT)
    parser.add_argument("--signal-dir", type=Path, default=runtime_review.DEFAULT_SIGNAL_DIR)
    parser.add_argument("--live-mobile-receipt", type=Path, default=runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT)
    parser.add_argument("--release-manifest", type=Path, default=runtime_review.DEFAULT_RELEASE_MANIFEST)
    parser.add_argument("--decision-dir", type=Path, default=authorization_decision.DEFAULT_DECISION_DIR)
    parser.add_argument("--project", default="property")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--verification-write", type=Path, default=DEFAULT_VERIFICATION_PATH)
    args = parser.parse_args(argv)
    current_args = {
        "handoff_dir": args.handoff_dir,
        "preview_dir": args.preview_dir,
        "plan_path": args.plan,
        "request_path": args.request,
        "packet_path": args.packet,
        "cycle_receipt_path": args.cycle_receipt,
        "signal_dir": args.signal_dir,
        "live_mobile_receipt_path": args.live_mobile_receipt,
        "release_manifest_path": args.release_manifest,
        "decision_dir": args.decision_dir,
        "project": args.project,
        "root": args.root,
    }
    if args.apply:
        if not (args.operator_id and args.handoff_id and args.handoff_sha256):
            parser.error("--apply requires --operator-id, --handoff-id, and --handoff-sha256")
        result = execute_manual_apply(
            expected_handoff_id=args.handoff_id,
            expected_handoff_sha256=args.handoff_sha256,
            operator_id=args.operator_id,
            receipt_dir=args.receipt_dir,
            **current_args,
        )
    elif args.rollback:
        if not (args.operator_id and args.execution_id and args.execution_sha256):
            parser.error("--rollback requires --operator-id, --execution-id, and --execution-sha256")
        result = execute_manual_rollback(
            expected_execution_id=args.execution_id,
            expected_execution_sha256=args.execution_sha256,
            operator_id=args.operator_id,
            handoff_dir=args.handoff_dir,
            receipt_dir=args.receipt_dir,
            root=args.root,
        )
    elif args.verify_current:
        result = verify_current_manual_action_handoff(**current_args)
        atomic_write_bytes(
            runtime_review._rooted(args.verification_write),
            _canonical(result),
            overwrite=True,
        )
        result["verification_receipt_persisted"] = True
    else:
        result = stage_current_manual_action_handoff(**current_args)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") in {
        "ready",
        "verified",
        "not_authorized",
        "applied",
        "rolled_back",
        "unchanged",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
