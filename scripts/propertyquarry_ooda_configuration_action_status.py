#!/usr/bin/env python3
"""Project current manual configuration action state from private receipts."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_authorization_request as authorization_request
from scripts import propertyquarry_ooda_configuration_manual_action as manual_action
from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


SCHEMA = "propertyquarry.ooda_runtime_configuration_action_status.v1"
PRESENTATION_SCHEMA = (
    "propertyquarry.ooda_runtime_configuration_action_presentation_state.v1"
)
PRESENTATION_RECEIPT_SCHEMA = (
    "propertyquarry.ooda_runtime_configuration_action_presentation_receipt.v1"
)
DEFAULT_PRESENTATION_STATE = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "runtime-configuration-action-presentation-state.json"
)
DEFAULT_EVIDENCE_REFRESH_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "runtime-configuration-evidence-refresh-receipts"
)
MAX_PRESENTATION_BYTES = 128 * 1024
MAX_RECEIPT_COUNT = 64


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


def _base(*, now: datetime) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "ready",
        "state": "absent",
        "updated_at": now.isoformat(),
        "blocking_reason": "",
        "next_action": "await an explicitly approved manual configuration action",
        "action_required": False,
        "interrupt_operator": False,
        "current_evidence_verified": True,
        "receipt_verified": False,
        "source_state_verified": True,
        "manual_invocation_required": True,
        "automatic_execution_allowed": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "provider_quota_consumed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
    }


def _blocked(
    reason: str,
    *,
    now: datetime,
    evidence_token: str = "",
) -> dict[str, Any]:
    result = _base(now=now)
    result.update(
        {
            "status": "blocked",
            "state": "evidence_blocked",
            "blocking_reason": reason,
            "next_action": (
                "inspect the private manual-action receipt chain and current source; "
                "do not retry, deploy, restart, or roll back from unverified evidence"
            ),
            "action_required": True,
            "interrupt_operator": True,
            "current_evidence_verified": False,
            "receipt_verified": False,
            "source_state_verified": False,
            "evidence_token": evidence_token,
        }
    )
    return _with_digest(result)


def _with_digest(value: dict[str, Any]) -> dict[str, Any]:
    semantic = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "updated_at",
            "interrupt_operator",
            "presentation",
            "semantic_digest",
        }
    }
    value["semantic_digest"] = _sha256(_canonical(semantic))
    return value


def _private_directory_paths(receipt_dir: Path) -> list[Path]:
    target = runtime_review._rooted(receipt_dir)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return []
    if not (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid in {0, os.geteuid()}
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    ):
        raise ValueError("manual_action_receipt_directory_not_admissible")
    paths = sorted(target.iterdir(), key=lambda item: item.name)
    if len(paths) > MAX_RECEIPT_COUNT:
        raise ValueError("manual_action_receipt_count_not_admissible")
    if any(
        not (
            path.name.endswith(".json")
            and (
                manual_action._APPLY_ID.fullmatch(path.stem)
                or manual_action._ROLLBACK_ID.fullmatch(path.stem)
            )
        )
        for path in paths
    ):
        raise ValueError("manual_action_receipt_name_not_admissible")
    return paths


def _load_handoff(
    *,
    handoff_dir: Path,
    handoff_id: str,
    handoff_sha256: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    path = manual_action._handoff_path(handoff_dir, handoff_id)
    artifact, _raw, persisted_sha256 = manual_action._private_object(
        path,
        field="runtime_configuration_manual_action_handoff",
        maximum_bytes=manual_action.MAX_HANDOFF_BYTES,
    )
    binding, target, envelope_sha256 = manual_action._handoff_envelope(
        artifact,
        now=now,
        require_fresh=False,
    )
    if not (
        artifact.get("handoff_id") == handoff_id
        and persisted_sha256 == handoff_sha256 == envelope_sha256
    ):
        raise ValueError("manual_action_status_handoff_binding_mismatch")
    return artifact, binding, target, persisted_sha256


def _load_receipts(
    *,
    receipt_dir: Path,
    handoff_dir: Path,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    paths = _private_directory_paths(receipt_dir)
    apply_rows: list[dict[str, Any]] = []
    rollback_artifacts: list[tuple[Path, dict[str, Any], str]] = []
    for path in paths:
        artifact, _raw, digest = manual_action._private_object(
            path,
            field="runtime_configuration_manual_action_execution_receipt",
            maximum_bytes=manual_action.MAX_EXECUTION_BYTES,
        )
        if path.stem != artifact.get("execution_id"):
            raise ValueError("manual_action_status_receipt_filename_mismatch")
        if artifact.get("schema") == manual_action.DOTENV_EXECUTION_SCHEMA:
            binding, target, rollback, verified_digest = (
                manual_action._dotenv_apply_receipt_envelope(artifact)
            )
            applied_at = _parse_timestamp(artifact.get("applied_at"))
            if digest != verified_digest or applied_at is None:
                raise ValueError("manual_action_status_dotenv_apply_receipt_mismatch")
            apply_rows.append(
                {
                    "kind": "dotenv",
                    "artifact": artifact,
                    "receipt_path": str(path),
                    "receipt_sha256": digest,
                    "execution_id": str(artifact.get("execution_id") or ""),
                    "status": "applied",
                    "prepared_at": applied_at,
                    "completed_at": applied_at,
                    "operator_id": "",
                    "binding": binding,
                    "target": target,
                    "rollback": rollback,
                }
            )
            continue
        if artifact.get("schema") == manual_action.DOTENV_ROLLBACK_SCHEMA:
            rollback_artifacts.append((path, artifact, digest))
            continue
        if manual_action._APPLY_ID.fullmatch(path.stem):
            receipt_binding = artifact.get("binding")
            if not isinstance(receipt_binding, Mapping):
                raise ValueError("manual_action_status_apply_binding_missing")
            handoff_id = str(receipt_binding.get("handoff_id") or "")
            handoff_sha256 = str(receipt_binding.get("handoff_sha256") or "")
            handoff, handoff_binding, target, _handoff_digest = _load_handoff(
                handoff_dir=handoff_dir,
                handoff_id=handoff_id,
                handoff_sha256=handoff_sha256,
                now=now,
            )
            execution_id, status, prepared_at, verified_digest = (
                manual_action._execution_envelope(
                    artifact,
                    action="apply",
                    binding=receipt_binding,
                    target=target,
                    operator_id=str(artifact.get("operator_id") or ""),
                )
            )
            completed_at = _parse_timestamp(artifact.get("completed_at"))
            if not (
                digest == verified_digest
                and execution_id
                == manual_action._execution_id(
                    action="apply",
                    binding=receipt_binding,
                )
                and receipt_binding.get("preview_id")
                == handoff_binding.get("preview_id")
                and receipt_binding.get("preview_sha256")
                == handoff_binding.get("preview_sha256")
            ):
                raise ValueError("manual_action_status_apply_receipt_mismatch")
            apply_rows.append(
                {
                    "artifact": artifact,
                    "receipt_path": str(path),
                    "receipt_sha256": digest,
                    "execution_id": execution_id,
                    "status": status,
                    "prepared_at": prepared_at,
                    "completed_at": completed_at,
                    "operator_id": str(artifact.get("operator_id") or ""),
                    "binding": dict(receipt_binding),
                    "target": target,
                    "handoff": handoff,
                    "handoff_binding": handoff_binding,
                }
            )
        else:
            rollback_artifacts.append((path, artifact, digest))

    apply_by_id = {row["execution_id"]: row for row in apply_rows}
    if len(apply_by_id) != len(apply_rows):
        raise ValueError("manual_action_status_duplicate_apply_receipt")
    rollback_rows: list[dict[str, Any]] = []
    for path, artifact, digest in rollback_artifacts:
        receipt_binding = artifact.get("binding")
        if not isinstance(receipt_binding, Mapping):
            raise ValueError("manual_action_status_rollback_binding_missing")
        apply_id = str(receipt_binding.get("apply_execution_id") or "")
        apply_row = apply_by_id.get(apply_id)
        if apply_row is None:
            raise ValueError("manual_action_status_rollback_apply_missing")
        if artifact.get("schema") == manual_action.DOTENV_ROLLBACK_SCHEMA:
            apply_artifact = dict(apply_row.get("artifact") or {})
            if apply_artifact.get("schema") != manual_action.DOTENV_EXECUTION_SCHEMA:
                raise ValueError("manual_action_status_dotenv_rollback_apply_mismatch")
            execution_id, status, prepared_at, verified_digest = (
                manual_action._dotenv_rollback_receipt_envelope(
                    artifact,
                    apply_receipt=apply_artifact,
                    apply_receipt_sha256=str(apply_row.get("receipt_sha256") or ""),
                )
            )
            completed_at = _parse_timestamp(artifact.get("completed_at"))
            if digest != verified_digest:
                raise ValueError("manual_action_status_dotenv_rollback_receipt_mismatch")
            rollback_rows.append(
                {
                    "kind": "dotenv",
                    "artifact": artifact,
                    "receipt_path": str(path),
                    "receipt_sha256": digest,
                    "execution_id": execution_id,
                    "status": status,
                    "prepared_at": prepared_at,
                    "completed_at": completed_at,
                    "operator_id": str(artifact.get("operator_id") or ""),
                    "binding": dict(receipt_binding),
                    "target": dict(artifact.get("target") or {}),
                    "apply": apply_row,
                }
            )
            continue
        execution_id, status, prepared_at, verified_digest = (
            manual_action._execution_envelope(
                artifact,
                action="rollback",
                binding=receipt_binding,
                target=apply_row["target"],
                operator_id=str(artifact.get("operator_id") or ""),
            )
        )
        completed_at = _parse_timestamp(artifact.get("completed_at"))
        if not (
            digest == verified_digest
            and execution_id
            == manual_action._execution_id(
                action="rollback",
                binding=receipt_binding,
            )
            and receipt_binding
            == {
                "apply_execution_id": apply_id,
                "apply_execution_sha256": apply_row["receipt_sha256"],
                "handoff_id": apply_row["binding"]["handoff_id"],
                "handoff_sha256": apply_row["binding"]["handoff_sha256"],
            }
        ):
            raise ValueError("manual_action_status_rollback_receipt_mismatch")
        rollback_rows.append(
            {
                "artifact": artifact,
                "receipt_path": str(path),
                "receipt_sha256": digest,
                "execution_id": execution_id,
                "status": status,
                "prepared_at": prepared_at,
                "completed_at": completed_at,
                "operator_id": str(artifact.get("operator_id") or ""),
                "binding": dict(receipt_binding),
                "target": apply_row["target"],
                "apply": apply_row,
            }
        )
    return apply_rows, rollback_rows


def _inspect_dotenv_event(
    *,
    latest: Mapping[str, Any],
    latest_priority: int,
    latest_time: datetime,
    refresh_receipt_dir: Path | None,
    include_evidence_refresh: bool,
    receipt_dir: Path,
    root: Path,
    now: datetime,
) -> dict[str, Any]:
    try:
        if latest_priority == 0:
            verification = manual_action.verify_dotenv_apply_receipt_state(
                dict(latest.get("artifact") or {}),
                root=root,
            )
            action_state = "applied"
            action_required = True
            next_action = (
                "run the exact evaluate-only evidence refresh before any deployment "
                "or restart; exact rollback remains available"
            )
        else:
            apply_row = dict(latest.get("apply") or {})
            verification = manual_action.verify_dotenv_rollback_receipt_state(
                dict(latest.get("artifact") or {}),
                apply_receipt=dict(apply_row.get("artifact") or {}),
                apply_receipt_sha256=str(apply_row.get("receipt_sha256") or ""),
                root=root,
            )
            action_state = "rolled_back"
            action_required = False
            next_action = (
                "run the exact evaluate-only evidence refresh if the runtime blocker "
                "still requires attention"
            )
    except Exception:
        return _blocked(
            "manual_action_current_source_drifted",
            now=now,
            evidence_token=str(latest.get("receipt_sha256") or ""),
        )
    if not (
        verification.get("status") == "verified"
        and verification.get("receipt_verified") is True
        and verification.get("source_state_verified") is True
        and verification.get("environment_values_recorded") is False
        and verification.get("environment_values_hashed") is False
        and verification.get("secret_values_recorded") is False
    ):
        return _blocked(
            "manual_action_dotenv_state_not_admissible",
            now=now,
            evidence_token=str(latest.get("receipt_sha256") or ""),
        )
    result = _base(now=now)
    result.update(
        {
            "status": "action_required" if action_required else "ready",
            "state": action_state,
            "next_action": next_action,
            "action_required": action_required,
            "interrupt_operator": action_required,
            "receipt_verified": True,
            "source_state_verified": True,
            "evidence_generated_at": latest_time.isoformat(),
            "source_observed_sha256": str(
                verification.get("source_state_verification_sha256") or ""
            ),
            "source_state_binding": dict(verification.get("state_binding") or {}),
            "receipt_path": str(latest.get("receipt_path") or ""),
            "receipt_id": str(latest.get("execution_id") or ""),
            "receipt_sha256": str(latest.get("receipt_sha256") or ""),
            "target": dict(latest.get("target") or {}),
            "environment_values_recorded": False,
            "environment_values_hashed": False,
            "secret_values_recorded": False,
        }
    )
    if latest_priority == 0:
        result["action_command"] = _command_for_evidence_refresh(latest)
        result["rollback_command"] = _command_for_rollback(latest)
    else:
        result["action_command"] = _command_for_evidence_refresh(latest)
    if include_evidence_refresh:
        selected_refresh_dir = refresh_receipt_dir
        if selected_refresh_dir is None:
            rooted_action_dir = runtime_review._rooted(receipt_dir)
            rooted_default_dir = runtime_review._rooted(
                manual_action.DEFAULT_RECEIPT_DIR
            )
            selected_refresh_dir = (
                DEFAULT_EVIDENCE_REFRESH_DIR
                if rooted_action_dir == rooted_default_dir
                else rooted_action_dir.parent
                / "runtime-configuration-evidence-refresh-receipts"
            )
        result = _with_evidence_refresh(
            result,
            receipt_dir=selected_refresh_dir,
            root=root,
            now=now,
        )
    return _with_digest(result)


def _event_time(row: Mapping[str, Any]) -> datetime:
    completed_at = row.get("completed_at")
    prepared_at = row.get("prepared_at")
    value = completed_at if isinstance(completed_at, datetime) else prepared_at
    if not isinstance(value, datetime):
        raise ValueError("manual_action_status_event_timestamp_missing")
    return value


def _command_for_apply(row: Mapping[str, Any]) -> str:
    binding = dict(row.get("binding") or {})
    return (
        "python3 scripts/propertyquarry_ooda_configuration_manual_action.py "
        '--apply --operator-id "$PROPERTYQUARRY_OPERATOR_ID" '
        f"--handoff-id {binding.get('handoff_id') or ''} "
        f"--handoff-sha256 {binding.get('handoff_sha256') or ''}"
    )


def _command_for_rollback(apply_row: Mapping[str, Any]) -> str:
    return (
        "python3 scripts/propertyquarry_ooda_configuration_manual_action.py "
        '--rollback --operator-id "$PROPERTYQUARRY_OPERATOR_ID" '
        f"--execution-id {apply_row.get('execution_id') or ''} "
        f"--execution-sha256 {apply_row.get('receipt_sha256') or ''}"
    )


def _command_for_evidence_refresh(row: Mapping[str, Any]) -> str:
    return (
        "python3 scripts/propertyquarry_ooda_configuration_evidence_refresh.py "
        "--refresh "
        f"--action-receipt-id {row.get('execution_id') or ''} "
        f"--action-receipt-sha256 {row.get('receipt_sha256') or ''}"
    )


def _with_evidence_refresh(
    result: dict[str, Any],
    *,
    receipt_dir: Path,
    root: Path,
    now: datetime,
) -> dict[str, Any]:
    from scripts import (
        propertyquarry_ooda_configuration_evidence_refresh as evidence_refresh,
    )

    refresh = evidence_refresh.inspect_latest_evidence_refresh(
        action_receipt_id=str(result.get("receipt_id") or ""),
        action_receipt_sha256=str(result.get("receipt_sha256") or ""),
        receipt_dir=receipt_dir,
        root=root,
        now=now,
    )
    refresh_status = str(refresh.get("status") or "blocked")
    result["evidence_refresh"] = {
        key: refresh.get(key)
        for key in (
            "status",
            "blocking_reason",
            "next_action",
            "refresh_id",
            "receipt_path",
            "receipt_sha256",
            "prepared_at",
            "completed_at",
            "automatic_execution_allowed",
            "source_edit_performed",
            "deployment_or_restart_authorized",
            "deployment_or_restart_performed",
            "provider_quota_consumption_allowed",
            "provider_quota_consumed",
            "delivery_authorized",
            "delivery_attempted",
            "protected_operation_executed",
        )
        if key in refresh
    }
    command = _command_for_evidence_refresh(
        {
            "execution_id": result.get("receipt_id"),
            "receipt_sha256": result.get("receipt_sha256"),
        }
    )
    if refresh_status == "absent":
        return result
    if refresh_status == "blocked":
        result.update(
            {
                "status": "blocked",
                "state": "evidence_refresh_blocked",
                "blocking_reason": str(
                    refresh.get("blocking_reason")
                    or "evidence_refresh_receipt_chain_not_admissible"
                ),
                "next_action": (
                    "inspect the private evidence-refresh receipt chain without "
                    "retrying, deploying, restarting, calling providers, or sending"
                ),
                "action_required": True,
                "interrupt_operator": True,
                "current_evidence_verified": False,
            }
        )
        result.pop("action_command", None)
        return result
    if refresh_status == "completed":
        action_state = str(result.get("state") or "")
        result.update(
            {
                "status": "ready",
                "state": f"{action_state}_evidence_refreshed",
                "blocking_reason": "",
                "next_action": (
                    "review the fresh OODA posture; any deployment or restart "
                    "requires a separate exact authorization"
                ),
                "action_required": False,
                "interrupt_operator": False,
                "current_evidence_verified": True,
            }
        )
        result.pop("action_command", None)
        return result
    result.update(
        {
            "status": "action_required",
            "state": {
                "prepared": "evidence_refresh_reconciliation_required",
                "failed": "evidence_refresh_failed",
                "stale": "evidence_refresh_stale",
            }.get(refresh_status, "evidence_refresh_reconciliation_required"),
            "blocking_reason": str(refresh.get("blocking_reason") or ""),
            "next_action": str(
                refresh.get("next_action")
                or "retry the exact evaluate-only evidence refresh command"
            ),
            "action_required": True,
            "interrupt_operator": True,
            "current_evidence_verified": False,
            "action_command": command,
        }
    )
    return result


def inspect_manual_action_status(
    *,
    receipt_dir: Path = manual_action.DEFAULT_RECEIPT_DIR,
    handoff_dir: Path = manual_action.DEFAULT_HANDOFF_DIR,
    refresh_receipt_dir: Path | None = None,
    include_evidence_refresh: bool = True,
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify the latest source-action event without relying on old review state."""

    observed_now = _now(now)
    try:
        apply_rows, rollback_rows = _load_receipts(
            receipt_dir=receipt_dir,
            handoff_dir=handoff_dir,
            now=observed_now,
        )
    except Exception:
        return _blocked(
            "manual_action_receipt_chain_not_admissible",
            now=observed_now,
        )
    if not apply_rows and not rollback_rows:
        return _with_digest(_base(now=observed_now))

    events: list[tuple[datetime, int, str, dict[str, Any]]] = []
    for row in apply_rows:
        events.append((_event_time(row), 0, str(row["execution_id"]), row))
    for row in rollback_rows:
        events.append((_event_time(row), 1, str(row["execution_id"]), row))
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    latest_time, latest_priority, _event_id, latest = events[-1]
    tied = [
        item
        for item in events
        if item[0] == latest_time and item[1] == latest_priority
    ]
    if len(tied) != 1:
        return _blocked(
            "manual_action_latest_event_ambiguous",
            now=observed_now,
        )
    if latest.get("kind") == "dotenv":
        return _inspect_dotenv_event(
            latest=latest,
            latest_priority=latest_priority,
            latest_time=latest_time,
            refresh_receipt_dir=refresh_receipt_dir,
            include_evidence_refresh=include_evidence_refresh,
            receipt_dir=receipt_dir,
            root=root,
            now=observed_now,
        )
    target = dict(latest.get("target") or {})
    try:
        _before_raw, _after_raw, source_sha256 = manual_action._source_pair(
            root=root,
            target=target,
        )
    except Exception:
        return _blocked(
            "manual_action_current_source_drifted",
            now=observed_now,
            evidence_token=str(latest.get("receipt_sha256") or ""),
        )

    result = _base(now=observed_now)
    result.update(
        {
            "status": "action_required",
            "state": "",
            "next_action": "",
            "action_required": True,
            "interrupt_operator": True,
            "receipt_verified": True,
            "source_state_verified": True,
            "evidence_generated_at": latest_time.isoformat(),
            "source_observed_sha256": source_sha256,
            "receipt_path": str(latest.get("receipt_path") or ""),
            "receipt_id": str(latest.get("execution_id") or ""),
            "receipt_sha256": str(latest.get("receipt_sha256") or ""),
            "target": target,
        }
    )
    if latest_priority == 0:
        apply_row = latest
        status = str(apply_row.get("status") or "")
        before = str(target.get("expected_before_sha256") or "")
        after = str(target.get("expected_after_sha256") or "")
        if status == "prepared" and source_sha256 == before:
            expires_at = _parse_timestamp(
                dict(apply_row.get("handoff") or {}).get("expires_at")
            )
            if expires_at is None or observed_now > expires_at:
                return _blocked(
                    "manual_apply_prepared_authority_expired",
                    now=observed_now,
                    evidence_token=str(apply_row.get("receipt_sha256") or ""),
                )
            result.update(
                {
                    "state": "apply_prepared",
                    "next_action": (
                        "retry the exact apply command before its approval expires"
                    ),
                    "action_command": _command_for_apply(apply_row),
                    "required_operator_id": str(apply_row.get("operator_id") or ""),
                }
            )
        elif status == "prepared" and source_sha256 == after:
            result.update(
                {
                    "state": "apply_reconciliation_required",
                    "next_action": (
                        "run the exact apply command once more to finalize the prepared receipt; "
                        "the source edit must not be repeated"
                    ),
                    "action_command": _command_for_apply(apply_row),
                    "required_operator_id": str(apply_row.get("operator_id") or ""),
                }
            )
        elif status == "applied" and source_sha256 == after:
            result.update(
                {
                    "state": "applied",
                    "next_action": (
                        "run the exact evaluate-only evidence refresh before any "
                        "deployment or restart; exact rollback remains available"
                    ),
                    "action_command": _command_for_evidence_refresh(apply_row),
                    "rollback_command": _command_for_rollback(apply_row),
                }
            )
        else:
            return _blocked(
                "manual_apply_receipt_source_state_mismatch",
                now=observed_now,
                evidence_token=str(apply_row.get("receipt_sha256") or ""),
            )
    else:
        rollback_row = latest
        apply_row = dict(rollback_row.get("apply") or {})
        status = str(rollback_row.get("status") or "")
        before = str(target.get("expected_before_sha256") or "")
        after = str(target.get("expected_after_sha256") or "")
        if status == "prepared" and source_sha256 in {before, after}:
            result.update(
                {
                    "state": "rollback_reconciliation_required",
                    "next_action": (
                        "run the exact rollback command once more to complete or finalize "
                        "the prepared rollback receipt"
                    ),
                    "action_command": _command_for_rollback(apply_row),
                    "required_operator_id": str(
                        rollback_row.get("operator_id") or ""
                    ),
                }
            )
        elif status == "rolled_back" and source_sha256 == before:
            result.update(
                {
                    "status": "ready",
                    "state": "rolled_back",
                    "next_action": (
                        "run the exact evaluate-only evidence refresh if the runtime "
                        "blocker still requires attention"
                    ),
                    "action_command": _command_for_evidence_refresh(rollback_row),
                    "action_required": False,
                    "interrupt_operator": False,
                }
            )
        else:
            return _blocked(
                "manual_rollback_receipt_source_state_mismatch",
                now=observed_now,
                evidence_token=str(rollback_row.get("receipt_sha256") or ""),
            )
    if include_evidence_refresh:
        selected_refresh_dir = refresh_receipt_dir
        if selected_refresh_dir is None:
            rooted_action_dir = runtime_review._rooted(receipt_dir)
            rooted_default_dir = runtime_review._rooted(
                manual_action.DEFAULT_RECEIPT_DIR
            )
            selected_refresh_dir = (
                DEFAULT_EVIDENCE_REFRESH_DIR
                if rooted_action_dir == rooted_default_dir
                else rooted_action_dir.parent
                / "runtime-configuration-evidence-refresh-receipts"
            )
        result = _with_evidence_refresh(
            result,
            receipt_dir=selected_refresh_dir,
            root=root,
            now=observed_now,
        )
    return _with_digest(result)


def _load_presentation_state(
    state_path: Path,
) -> tuple[dict[str, Any], str]:
    try:
        state, _raw, digest = authorization_request._private_object(
            state_path,
            field="runtime_configuration_action_presentation_state",
            maximum_bytes=MAX_PRESENTATION_BYTES,
        )
    except FileNotFoundError:
        return {}, "absent"
    except Exception:
        return {}, "invalid"
    normalized = dict(state)
    integrity = normalized.pop("integrity", None)
    active_digest = str(state.get("active_digest") or "")
    recorded_at = _parse_timestamp(state.get("recorded_at"))
    if not (
        state.get("schema") == PRESENTATION_SCHEMA
        and set(state)
        == {
            "schema",
            "recorded_at",
            "active_digest",
            "source_state",
            "source_receipt_id",
            "integrity",
        }
        and recorded_at is not None
        and (not active_digest or authorization_request._SHA256.fullmatch(active_digest))
        and isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    ):
        return {}, "invalid"
    state["state_sha256"] = digest
    return state, "ready"


def apply_manual_action_presentation_state(
    status: Mapping[str, Any],
    *,
    state_path: Path = DEFAULT_PRESENTATION_STATE,
) -> dict[str, Any]:
    projected = dict(status)
    digest = str(projected.get("semantic_digest") or "")
    if authorization_request._SHA256.fullmatch(digest) is None:
        raise ValueError("manual_action_status_digest_not_admissible")
    prior, prior_status = _load_presentation_state(state_path)
    if prior_status == "invalid":
        raise ValueError("manual_action_presentation_state_not_admissible")
    repeated = bool(
        projected.get("action_required") is True
        and prior_status == "ready"
        and prior.get("active_digest") == digest
    )
    projected["interrupt_operator"] = bool(
        projected.get("action_required") is True and not repeated
    )
    if repeated and projected.get("status") == "action_required":
        projected["status"] = "pending_action"
    projected["presentation"] = {
        "state_path": str(state_path),
        "state_status": prior_status,
        "state_sha256": str(prior.get("state_sha256") or ""),
        "semantic_digest": digest,
        "already_presented": repeated,
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "protected_operation_executed": False,
    }
    return projected


def _presentation_receipt(
    status: str,
    *,
    now: datetime,
    state_updated: bool,
    blocking_reason: str = "",
) -> dict[str, Any]:
    return {
        "schema": PRESENTATION_RECEIPT_SCHEMA,
        "status": status,
        "recorded_at": now.isoformat(),
        "blocking_reason": blocking_reason,
        "state_updated": state_updated,
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "protected_operation_executed": False,
    }


def record_manual_action_presentation(
    status: Mapping[str, Any],
    *,
    state_path: Path = DEFAULT_PRESENTATION_STATE,
    expected_semantic_digest: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    recorded_at = _now(now)
    semantic_digest = str(status.get("semantic_digest") or "")
    presentation = status.get("presentation")
    if not (
        authorization_request._SHA256.fullmatch(expected_semantic_digest)
        and semantic_digest == expected_semantic_digest
        and isinstance(presentation, Mapping)
        and presentation.get("semantic_digest") == semantic_digest
        and presentation.get("delivery_state_updated") is False
        and presentation.get("provider_quota_consumed") is False
        and presentation.get("protected_operation_executed") is False
        and status.get("automatic_execution_allowed") is False
        and status.get("deployment_or_restart_performed") is False
        and status.get("provider_quota_consumed") is False
        and status.get("delivery_attempted") is False
    ):
        return _presentation_receipt(
            "blocked",
            now=recorded_at,
            state_updated=False,
            blocking_reason="manual_action_presentation_binding_not_admissible",
        )
    prior, prior_status = _load_presentation_state(state_path)
    if prior_status == "invalid":
        return _presentation_receipt(
            "blocked",
            now=recorded_at,
            state_updated=False,
            blocking_reason="manual_action_presentation_state_not_admissible",
        )
    desired_digest = semantic_digest if status.get("action_required") is True else ""
    if prior_status == "absent" and not desired_digest:
        return _presentation_receipt(
            "unchanged",
            now=recorded_at,
            state_updated=False,
        )
    if prior_status == "ready" and prior.get("active_digest") == desired_digest:
        return _presentation_receipt(
            "unchanged",
            now=recorded_at,
            state_updated=False,
        )
    state: dict[str, Any] = {
        "schema": PRESENTATION_SCHEMA,
        "recorded_at": recorded_at.isoformat(),
        "active_digest": desired_digest,
        "source_state": str(status.get("state") or ""),
        "source_receipt_id": str(status.get("receipt_id") or ""),
    }
    state["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(state)),
    }
    try:
        atomic_write_bytes(
            runtime_review._rooted(state_path),
            _canonical(state),
            overwrite=True,
        )
    except Exception:
        return _presentation_receipt(
            "blocked",
            now=recorded_at,
            state_updated=False,
            blocking_reason="manual_action_presentation_state_write_failed",
        )
    return _presentation_receipt(
        "recorded",
        now=recorded_at,
        state_updated=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Project current PropertyQuarry manual configuration action state "
            "from private receipts without deploying, restarting, calling providers, or sending."
        )
    )
    parser.add_argument("--receipt-dir", type=Path, default=manual_action.DEFAULT_RECEIPT_DIR)
    parser.add_argument("--handoff-dir", type=Path, default=manual_action.DEFAULT_HANDOFF_DIR)
    parser.add_argument(
        "--evidence-refresh-receipt-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_REFRESH_DIR,
    )
    parser.add_argument("--presentation-state", type=Path, default=DEFAULT_PRESENTATION_STATE)
    parser.add_argument("--record-presentation", action="store_true")
    parser.add_argument("--expected-semantic-digest", default="")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    status = inspect_manual_action_status(
        receipt_dir=args.receipt_dir,
        handoff_dir=args.handoff_dir,
        refresh_receipt_dir=args.evidence_refresh_receipt_dir,
        root=args.root,
    )
    try:
        projected = apply_manual_action_presentation_state(
            status,
            state_path=args.presentation_state,
        )
    except ValueError:
        projected = _blocked(
            "manual_action_presentation_state_not_admissible",
            now=_now(),
            evidence_token=str(status.get("semantic_digest") or ""),
        )
    if args.record_presentation:
        projected["presentation_receipt"] = record_manual_action_presentation(
            projected,
            state_path=args.presentation_state,
            expected_semantic_digest=args.expected_semantic_digest,
        )
    print(json.dumps(projected, sort_keys=True))
    return 1 if projected.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
