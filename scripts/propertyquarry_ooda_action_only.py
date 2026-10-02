#!/usr/bin/env python3
"""Render one fresh, novel PropertyQuarry operator action without deep review work."""

from __future__ import annotations

import argparse
import fcntl
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

from scripts import propertyquarry_local_deployment_receipt as local_deployment
from scripts import propertyquarry_ooda_authorization_request as runtime_authorization
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts import propertyquarry_ooda_source_refresh_trust_notification as trust_notification
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_action_only.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRUST_PROTECTED_OPERATIONS = [
    "producer_identity_verification_assertion",
    "immutable_trust_candidate_decision",
    "trust_registry_change",
]
_RUNTIME_AUTHORIZATION_PRESENTATION_SCHEMA = (
    "propertyquarry.ooda_runtime_authorization_presentation.v1"
)
_RUNTIME_CONTROL_SCHEMA = "propertyquarry.ooda_runtime_control.v1"
_RUNTIME_CONTROL_PRESENTATION_SETTLEMENT_SCHEMA = (
    "propertyquarry.ooda_runtime_control_presentation_settlement.v1"
)
_RUNTIME_CONTROL_EFFECTIVE_VERIFICATION_SCHEMA = (
    "propertyquarry.ooda_runtime_control_effective_verification.v1"
)
DEFAULT_RUNTIME_CONTROL_RECEIPT = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "runtime-control-current.json"
)
DEFAULT_RUNTIME_CONTROL_LOCK = Path(
    "_completion/propertyquarry_ooda_notification_cycle/safe-tick.lock"
)
DEFAULT_SAFE_TICK_RECEIPT = Path(
    "_completion/propertyquarry_ooda_notification_cycle/safe-tick-latest.json"
)
DEFAULT_RUNTIME_CONTROL_EFFECTIVE_VERIFICATION = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "runtime-control-effective-verification.json"
)


def _false_runtime_authority(value: Mapping[str, Any]) -> bool:
    return bool(
        value.get("automatic_execution_allowed") is False
        and value.get("execution_authorized") is False
        and value.get("deployment_or_restart_authorized") is False
        and value.get("protected_operation_executed") is False
        and value.get("provider_quota_consumption_allowed") is False
        and value.get("delivery_authorized") is False
        and value.get("delivery_attempted") is False
        and value.get("sent") is False
    )


def _acquire_runtime_control_lock(path: Path) -> int | None:
    target = runtime_review._rooted(path)
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent = target.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise ValueError("runtime_control_lock_directory_not_admissible")
    descriptor = os.open(
        target,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("runtime_control_lock_not_admissible")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _release_runtime_control_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def settle_runtime_control_presentation(
    projection: Mapping[str, Any],
    presentation_receipt: Mapping[str, Any],
    *,
    receipt_path: Path = DEFAULT_RUNTIME_CONTROL_RECEIPT,
    lock_path: Path = DEFAULT_RUNTIME_CONTROL_LOCK,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Settle one recorded interrupt into retained pending-action posture."""

    observed_at = (now or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ).isoformat()
    try:
        lock_descriptor = _acquire_runtime_control_lock(lock_path)
    except Exception:
        lock_descriptor = None
    if lock_descriptor is None:
        return {
            "schema": _RUNTIME_CONTROL_PRESENTATION_SETTLEMENT_SCHEMA,
            "status": "blocked",
            "settled_at": observed_at,
            "blocking_reason": "runtime_control_settlement_lock_unavailable",
            "presentation_recorded": False,
            "current_evidence_verified": False,
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
        }

    target = runtime_review._rooted(receipt_path)
    try:
        current, _raw, current_sha256 = load_strict_json_object_snapshot(
            target,
            field="runtime control receipt",
            maximum_bytes=512 * 1024,
        )
        source_digest = str(
            projection.get("source_cycle_receipt_sha256") or ""
        )
        recorded_at = str(
            presentation_receipt.get("recorded_at") or observed_at
        )
        if not (
            current.get("schema") == _RUNTIME_CONTROL_SCHEMA
            and current.get("status") == "verified"
            and current.get("current_evidence_verified") is True
            and current.get("action_required") is True
            and current.get("interrupt_operator") is True
            and current.get("presentation_recorded") is False
            and current.get("novel_action_count") == 1
            and current.get("action_projection") == dict(projection)
            and _false_runtime_authority(current)
            and projection.get("schema") == SCHEMA
            and projection.get("status") == "action_required"
            and projection.get("execution_authorized") is False
            and projection.get("deployment_or_restart_authorized") is False
            and projection.get("protected_operation_executed") is False
            and projection.get("provider_quota_consumption_allowed") is False
            and projection.get("delivery_authorized") is False
            and projection.get("delivery_attempted") is False
            and projection.get("sent") is False
            and _SHA256.fullmatch(source_digest)
            and presentation_receipt.get("schema")
            == operator_status.PRESENTATION_RECEIPT_SCHEMA
            and presentation_receipt.get("status") in {"recorded", "unchanged"}
            and presentation_receipt.get("source_cycle_receipt_sha256")
            == source_digest
            and _SHA256.fullmatch(
                str(presentation_receipt.get("state_sha256") or "")
            )
            and presentation_receipt.get("delivery_state_updated") is False
            and presentation_receipt.get("provider_quota_consumed") is False
            and presentation_receipt.get("protected_operation_executed")
            is False
        ):
            raise ValueError("runtime_control_settlement_binding_mismatch")

        settled = dict(current)
        settled.update(
            {
                "updated_at": recorded_at,
                "action_required": True,
                "interrupt_operator": False,
                "presentation_recorded": True,
                "novel_action_count": 0,
                "action_projection": {},
                "presentation_settlement": {
                    "schema": (
                        _RUNTIME_CONTROL_PRESENTATION_SETTLEMENT_SCHEMA
                    ),
                    "status": "settled",
                    "settled_at": recorded_at,
                    "source_runtime_control_receipt_sha256": current_sha256,
                    "source_cycle_receipt_sha256": source_digest,
                    "presentation_state_path": str(
                        presentation_receipt.get("state_path") or ""
                    ),
                    "presentation_state_sha256": str(
                        presentation_receipt.get("state_sha256") or ""
                    ),
                    "presentation_receipt_status": str(
                        presentation_receipt.get("status") or ""
                    ),
                    "current_evidence_verified": True,
                    "automatic_execution_allowed": False,
                    "execution_authorized": False,
                    "deployment_or_restart_authorized": False,
                    "protected_operation_executed": False,
                    "provider_quota_consumption_allowed": False,
                    "delivery_authorized": False,
                    "delivery_attempted": False,
                    "sent": False,
                },
            }
        )
        atomic_write_bytes(
            target,
            runtime_review._canonical(settled),
            overwrite=True,
        )
        persisted, _persisted_raw, _persisted_sha256 = (
            load_strict_json_object_snapshot(
                target,
                field="settled runtime control receipt",
                maximum_bytes=512 * 1024,
            )
        )
        if persisted != settled:
            raise ValueError("runtime_control_settlement_write_not_verified")
        return dict(settled["presentation_settlement"])
    except Exception as exc:
        return {
            "schema": _RUNTIME_CONTROL_PRESENTATION_SETTLEMENT_SCHEMA,
            "status": "blocked",
            "settled_at": observed_at,
            "blocking_reason": str(exc),
            "presentation_recorded": False,
            "current_evidence_verified": False,
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
        }
    finally:
        _release_runtime_control_lock(lock_descriptor)


def verify_effective_runtime_control(
    *,
    safe_tick_receipt_path: Path = DEFAULT_SAFE_TICK_RECEIPT,
    runtime_control_receipt_path: Path = DEFAULT_RUNTIME_CONTROL_RECEIPT,
    presentation_state_path: Path = operator_status.DEFAULT_PRESENTATION_STATE,
    verification_path: Path = (
        DEFAULT_RUNTIME_CONTROL_EFFECTIVE_VERIFICATION
    ),
    lock_path: Path = DEFAULT_RUNTIME_CONTROL_LOCK,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify historical tick evidence into effective post-presentation truth."""

    verified_at = (now or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ).isoformat()
    blocked = {
        "schema": _RUNTIME_CONTROL_EFFECTIVE_VERIFICATION_SCHEMA,
        "status": "blocked",
        "verified_at": verified_at,
        "blocking_reason": "effective_runtime_control_not_current",
        "historical_tick_preserved": True,
        "action_required": False,
        "interrupt_operator": False,
        "presentation_recorded": False,
        "current_evidence_verified": False,
        "verification_receipt_persisted": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
    try:
        lock_descriptor = _acquire_runtime_control_lock(lock_path)
    except Exception:
        lock_descriptor = None
    if lock_descriptor is None:
        return {
            **blocked,
            "blocking_reason": "effective_runtime_control_lock_unavailable",
        }

    verification_target = runtime_review._rooted(verification_path)
    try:
        tick, _tick_raw, tick_sha256 = load_strict_json_object_snapshot(
            runtime_review._rooted(safe_tick_receipt_path),
            field="safe tick receipt",
            maximum_bytes=2 * 1024 * 1024,
        )
        control, _control_raw, control_sha256 = (
            load_strict_json_object_snapshot(
                runtime_review._rooted(runtime_control_receipt_path),
                field="effective runtime control receipt",
                maximum_bytes=512 * 1024,
            )
        )
        presentation, _presentation_raw, presentation_sha256 = (
            load_strict_json_object_snapshot(
                runtime_review._rooted(presentation_state_path),
                field="operator presentation state",
                maximum_bytes=operator_status.MAX_RECEIPT_BYTES,
            )
        )
        tick_components = tick.get("components")
        historical_control = (
            tick_components.get("runtime_control")
            if isinstance(tick_components, Mapping)
            else None
        )
        settlement = control.get("presentation_settlement")
        historical_projection = (
            historical_control.get("action_projection")
            if isinstance(historical_control, Mapping)
            else None
        )
        actions = (
            list(historical_projection.get("actions") or [])
            if isinstance(historical_projection, Mapping)
            else []
        )
        action = actions[0] if len(actions) == 1 else None
        active_presentations = presentation.get("active_presentations")
        active_runtime = (
            active_presentations.get("gold_live_runtime")
            if isinstance(active_presentations, Mapping)
            else None
        )
        historical_control_sha256 = (
            runtime_review._sha256(
                runtime_review._canonical(dict(historical_control))
            )
            if isinstance(historical_control, Mapping)
            else ""
        )
        expected_control = dict(historical_control or {})
        if isinstance(settlement, Mapping):
            expected_control.update(
                {
                    "updated_at": settlement.get("settled_at"),
                    "action_required": True,
                    "interrupt_operator": False,
                    "presentation_recorded": True,
                    "novel_action_count": 0,
                    "action_projection": {},
                    "presentation_settlement": dict(settlement),
                }
            )
        source_digest = str(
            historical_projection.get("source_cycle_receipt_sha256") or ""
        ) if isinstance(historical_projection, Mapping) else ""
        if not (
            tick.get("schema") == "propertyquarry.ooda_safe_tick.v1"
            and tick.get("status") == "ready"
            and tick.get("action_required") is True
            and tick.get("interrupt_operator") is True
            and _false_runtime_authority(tick)
            and isinstance(historical_control, Mapping)
            and historical_control.get("schema") == _RUNTIME_CONTROL_SCHEMA
            and historical_control.get("status") == "verified"
            and historical_control.get("current_evidence_verified") is True
            and historical_control.get("action_required") is True
            and historical_control.get("interrupt_operator") is True
            and historical_control.get("presentation_recorded") is False
            and historical_control.get("novel_action_count") == 1
            and _false_runtime_authority(historical_control)
            and isinstance(historical_projection, Mapping)
            and historical_projection.get("schema") == SCHEMA
            and historical_projection.get("status") == "action_required"
            and len(actions) == 1
            and isinstance(action, Mapping)
            and action.get("lane") == "gold_live_runtime"
            and _SHA256.fullmatch(
                str(action.get("presentation_digest") or "")
            )
            and _SHA256.fullmatch(
                str(action.get("presentation_context_digest") or "")
            )
            and _SHA256.fullmatch(source_digest)
            and isinstance(settlement, Mapping)
            and settlement.get("schema")
            == _RUNTIME_CONTROL_PRESENTATION_SETTLEMENT_SCHEMA
            and settlement.get("status") == "settled"
            and settlement.get("current_evidence_verified") is True
            and settlement.get("source_runtime_control_receipt_sha256")
            == historical_control_sha256
            and settlement.get("source_cycle_receipt_sha256")
            == source_digest
            and settlement.get("presentation_state_path")
            == str(presentation_state_path)
            and settlement.get("presentation_state_sha256")
            == presentation_sha256
            and _false_runtime_authority(settlement)
            and control == expected_control
            and control.get("status") == "verified"
            and control.get("current_evidence_verified") is True
            and control.get("action_required") is True
            and control.get("interrupt_operator") is False
            and control.get("presentation_recorded") is True
            and control.get("novel_action_count") == 0
            and control.get("action_projection") == {}
            and _false_runtime_authority(control)
            and presentation.get("schema")
            == operator_status.PRESENTATION_STATE_SCHEMA
            and presentation.get("source_cycle_receipt_sha256")
            == source_digest
            and presentation.get("delivery_state_updated") is False
            and presentation.get("provider_quota_consumed") is False
            and presentation.get("protected_operation_executed") is False
            and isinstance(active_runtime, Mapping)
            and active_runtime.get("action_digest")
            == action.get("presentation_digest")
            and active_runtime.get("presentation_context_digest")
            == action.get("presentation_context_digest")
            and active_runtime.get("source_cycle_receipt_sha256")
            == source_digest
        ):
            raise ValueError("effective_runtime_control_binding_mismatch")

        result = {
            "schema": _RUNTIME_CONTROL_EFFECTIVE_VERIFICATION_SCHEMA,
            "status": "verified",
            "verified_at": verified_at,
            "blocking_reason": "",
            "transition": "operator_presentation_settled",
            "historical_tick": {
                "path": str(safe_tick_receipt_path),
                "sha256": tick_sha256,
                "updated_at": str(tick.get("updated_at") or ""),
                "interrupt_operator": True,
                "runtime_control_sha256": historical_control_sha256,
            },
            "effective_runtime_control": {
                "path": str(runtime_control_receipt_path),
                "sha256": control_sha256,
                "updated_at": str(control.get("updated_at") or ""),
                "action_required": True,
                "interrupt_operator": False,
                "presentation_recorded": True,
            },
            "presentation_ledger": {
                "path": str(presentation_state_path),
                "sha256": presentation_sha256,
                "updated_at": str(presentation.get("updated_at") or ""),
                "source_cycle_receipt_sha256": source_digest,
                "action_digest": str(action.get("presentation_digest") or ""),
                "presentation_context_digest": str(
                    action.get("presentation_context_digest") or ""
                ),
            },
            "historical_tick_preserved": True,
            "action_required": True,
            "interrupt_operator": False,
            "presentation_recorded": True,
            "current_evidence_verified": True,
            "verification_receipt_persisted": True,
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
        }
        atomic_write_bytes(
            verification_target,
            runtime_review._canonical(result),
            overwrite=True,
        )
        persisted, _persisted_raw, _persisted_sha256 = (
            load_strict_json_object_snapshot(
                verification_target,
                field="effective runtime control verification",
                maximum_bytes=512 * 1024,
            )
        )
        if persisted != result:
            raise ValueError("effective_runtime_control_write_not_verified")
        return result
    except Exception as exc:
        failure = {
            **blocked,
            "blocking_reason": str(exc),
            "verification_receipt_persisted": True,
        }
        try:
            atomic_write_bytes(
                verification_target,
                runtime_review._canonical(failure),
                overwrite=True,
            )
        except Exception:
            failure["verification_receipt_persisted"] = False
        return failure
    finally:
        _release_runtime_control_lock(lock_descriptor)


def _runtime_authorization_presentation(
    handoff: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    context_scope = context.get("scope")
    consent_request = (
        context_scope.get("consent_request")
        if isinstance(context_scope, Mapping)
        else None
    )
    consent_scope = (
        consent_request.get("authorization_scope")
        if isinstance(consent_request, Mapping)
        else None
    )
    request_scope = handoff.get("scope")
    request_id = str(handoff.get("request_id") or "")
    expected_scope = {
        "operation": "runtime_configuration_change",
        "change_id": "local_environment_candidate_merge",
        "recovery_preview_sha256": (
            consent_scope.get("recovery_preview_sha256")
            if isinstance(consent_scope, Mapping)
            else ""
        ),
        "candidate_path": (
            consent_scope.get("candidate_path")
            if isinstance(consent_scope, Mapping)
            else ""
        ),
        "candidate_policy_schema": (
            consent_scope.get("candidate_policy_schema")
            if isinstance(consent_scope, Mapping)
            else ""
        ),
        "candidate_review_keys": list(
            consent_scope.get("candidate_review_keys") or []
        )
        if isinstance(consent_scope, Mapping)
        else [],
        "candidate_review_key_count": (
            consent_scope.get("candidate_review_key_count")
            if isinstance(consent_scope, Mapping)
            else -1
        ),
        "configuration_merge_policy": (
            consent_scope.get("configuration_merge_policy")
            if isinstance(consent_scope, Mapping)
            else ""
        ),
        "existing_environment_values_overwrite_allowed": (
            consent_scope.get("existing_environment_values_overwrite_allowed")
            if isinstance(consent_scope, Mapping)
            else None
        ),
        "rollback_required": (
            consent_scope.get("rollback_required")
            if isinstance(consent_scope, Mapping)
            else None
        ),
        "compose_project": (
            context_scope.get("compose_project")
            if isinstance(context_scope, Mapping)
            else ""
        ),
    }
    if not (
        handoff.get("schema") == runtime_authorization.HANDOFF_SCHEMA
        and handoff.get("status") == "ready"
        and handoff.get("request_state") in {"reused", "refreshed"}
        and handoff.get("current_evidence_verified") is True
        and runtime_authorization._REQUEST_ID.fullmatch(request_id)
        and handoff.get("decision_options")
        == ["approve_exact_scope", "reject", "defer"]
        and isinstance(request_scope, Mapping)
        and dict(request_scope) == expected_scope
        and runtime_authorization._scope_kind(request_scope)
        == "local_environment_candidate_merge"
        and handoff.get("authorization_required") is True
        and handoff.get("authorization_recorded") is False
        and handoff.get("execution_authorized") is False
        and handoff.get("deployment_or_restart_authorized") is False
        and handoff.get("protected_operation_executed") is False
        and handoff.get("provider_quota_consumption_allowed") is False
        and handoff.get("delivery_authorized") is False
    ):
        raise ValueError("runtime_authorization_handoff_not_admissible")
    instruction = (
        "reply exactly 'Authorize "
        + request_id
        + "' to authorize only this current add-only configuration merge; "
        "any changed or expired request requires a new authorization, and "
        "deployment or restart remains separately unauthorized"
    )
    return {
        "schema": _RUNTIME_AUTHORIZATION_PRESENTATION_SCHEMA,
        "status": "awaiting_explicit_authorization",
        "request_id": request_id,
        "decision_options": ["approve_exact_scope", "reject", "defer"],
        "authorization_instruction": instruction,
        "authorization_scope": dict(request_scope),
        "authorization_required": True,
        "authorization_recorded": False,
        "configuration_merge_authorized": False,
        "deployment_or_restart_authorized": False,
        "execution_authorized": False,
        "protected_operations": [
            "runtime_configuration_change",
            "deployment_or_restart",
        ],
    }


def _runtime_external_input_instruction(
    presentation_context_by_lane: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    context = presentation_context_by_lane.get("gold_live_runtime")
    if context is None:
        return None
    if not (
        isinstance(context, Mapping)
        and context.get("schema")
        == "propertyquarry.ooda_operator_presentation_context.v1"
        and context.get("lane") == "gold_live_runtime"
        and isinstance(context.get("scope"), Mapping)
    ):
        raise ValueError("runtime_action_context_not_admissible")
    scope = context["scope"]
    intake = (
        scope.get("deployment_environment_intake")
        if isinstance(scope, Mapping)
        else None
    )
    if intake is None:
        return None
    keys = list(intake.get("operator_external_input_keys") or [])
    count = intake.get("operator_external_input_key_count")
    merge_keys = list(intake.get("operator_merge_review_keys") or [])
    merge_count = intake.get("operator_merge_review_key_count")
    locally_stageable_count = intake.get("locally_stageable_key_count")
    locally_staged_count = intake.get("locally_staged_key_count")
    locally_pending_count = intake.get("locally_pending_key_count")
    local_merge_keys = list(intake.get("local_merge_review_keys") or [])
    local_merge_count = intake.get("local_merge_review_key_count")
    import_path_text = str(intake.get("operator_import_path") or "").strip()
    import_path = Path(import_path_text)
    local_candidate_path_text = str(
        intake.get("local_runtime_candidate_path") or ""
    ).strip()
    local_candidate_path = Path(local_candidate_path_text)
    if not (
        set(intake)
        == {
            "status",
            "blocking_reason",
            "next_action",
            "operator_external_input_required",
            "operator_external_input_keys",
            "operator_external_input_key_count",
            "operator_import_path",
            "operator_import_status",
            "operator_import_file_status",
            "operator_import_ready_for_review",
            "operator_merge_review_required",
            "operator_merge_review_keys",
            "operator_merge_review_key_count",
            "locally_stageable_key_count",
            "locally_staged_key_count",
            "locally_pending_key_count",
            "local_merge_review_required",
            "local_merge_review_keys",
            "local_merge_review_key_count",
            "local_runtime_candidate_path",
            "local_runtime_candidate_status",
            "local_runtime_candidate_ready_for_merge_review",
            "local_runtime_candidate_values_hashed",
            "configuration_merge_authorized",
            "configuration_merged",
            "configuration_apply_authorized",
            "deployment_or_restart_authorized",
            "environment_values_recorded",
            "secret_values_recorded",
        }
        and keys == sorted(set(keys))
        and all(re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key) for key in keys)
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count == len(keys)
        and merge_keys == sorted(set(merge_keys))
        and all(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key)
            for key in merge_keys
        )
        and isinstance(merge_count, int)
        and not isinstance(merge_count, bool)
        and merge_count == len(merge_keys)
        and import_path_text
        and not import_path.is_absolute()
        and ".." not in import_path.parts
        and import_path.name not in {"", ".", ".."}
        and isinstance(locally_stageable_count, int)
        and not isinstance(locally_stageable_count, bool)
        and locally_stageable_count >= 0
        and isinstance(locally_staged_count, int)
        and not isinstance(locally_staged_count, bool)
        and isinstance(locally_pending_count, int)
        and not isinstance(locally_pending_count, bool)
        and locally_staged_count >= 0
        and locally_pending_count >= 0
        and locally_staged_count + locally_pending_count
        == locally_stageable_count
        and local_merge_keys == sorted(set(local_merge_keys))
        and all(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key)
            for key in local_merge_keys
        )
        and isinstance(local_merge_count, int)
        and not isinstance(local_merge_count, bool)
        and local_merge_count == len(local_merge_keys)
        and (
            intake.get("local_merge_review_required") is True
        )
        == bool(local_merge_keys)
        and local_candidate_path_text
        and not local_candidate_path.is_absolute()
        and ".." not in local_candidate_path.parts
        and local_candidate_path.name not in {"", ".", ".."}
        and intake.get("local_runtime_candidate_status")
        in {
            "ready_for_merge_review",
            "candidate_missing",
            "candidate_stale",
            "blocked",
        }
        and (
            intake.get("local_runtime_candidate_ready_for_merge_review")
            is True
        )
        == (
            intake.get("local_runtime_candidate_status")
            == "ready_for_merge_review"
        )
        and intake.get("local_runtime_candidate_values_hashed") is False
        and intake.get("configuration_merge_authorized") is False
        and intake.get("configuration_merged") is False
        and intake.get("configuration_apply_authorized") is False
        and intake.get("deployment_or_restart_authorized") is False
        and intake.get("environment_values_recorded") is False
        and intake.get("secret_values_recorded") is False
    ):
        raise ValueError("runtime_action_context_not_admissible")
    external_required = intake.get("operator_external_input_required") is True
    merge_review_required = intake.get("operator_merge_review_required") is True
    local_merge_review_required = (
        intake.get("local_merge_review_required") is True
    )
    next_action = str(intake.get("next_action") or "").strip()
    allowed_external_keys = set().union(
        *(
            set(contract["keys"])
            for contract in (
                runtime_review._DEPLOYMENT_ENVIRONMENT_REQUIREMENT_CLASSES
            )
            if contract["operator_value_required"] is True
        )
    )
    allowed_local_keys = set().union(
        *(
            set(contract["keys"])
            for contract in (
                runtime_review._DEPLOYMENT_ENVIRONMENT_REQUIREMENT_CLASSES
            )
            if contract["operator_value_required"] is False
        )
    )
    if external_required and not merge_review_required:
        import_status = str(intake.get("operator_import_status") or "")
        blocked = intake.get("status") == "operator_import_blocked"
        expected_next_action = (
            "repair the private operator-import fragment at "
            + import_path_text
            + " so it is owner-only and contains exactly the named keys; "
            "do not paste values into chat: "
            + ", ".join(keys)
            if blocked
            else "place a private dotenv fragment containing exactly the named "
            "keys at "
            + import_path_text
            + "; set mode 0600 and do not paste values into chat: "
            + ", ".join(keys)
        )
        if not (
            keys
            and not merge_keys
            and merge_count == 0
            and set(keys).issubset(allowed_external_keys)
            and intake.get("operator_import_ready_for_review") is False
            and import_status
            in {"awaiting_operator_import", "incomplete", "blocked"}
            and (
                blocked
                or (
                    intake.get("status") == "operator_input_required"
                    and intake.get("blocking_reason")
                    == "external_account_material_required"
                )
            )
            and next_action == expected_next_action
        ):
            raise ValueError("runtime_action_context_not_admissible")
        return {
            "blocking_reason": str(intake.get("blocking_reason") or ""),
            "next_action": next_action,
            "operator_projection_key": "operator_input",
            "operator_projection": {
                "lane": "governed_operator_import",
                "drop_path": import_path_text,
                "import_status": import_status,
                "file_status": intake.get("operator_import_file_status"),
                "required_keys": keys,
                "required_key_count": count,
                "locally_stageable_key_count": locally_stageable_count,
                "locally_staged_key_count": locally_staged_count,
                "locally_pending_key_count": locally_pending_count,
                "local_runtime_candidate_path": local_candidate_path_text,
                "local_runtime_candidate_status": intake.get(
                    "local_runtime_candidate_status"
                ),
                "values_allowed_in_chat": False,
                "configuration_merge_authorized": False,
                "environment_values_recorded": False,
                "secret_values_recorded": False,
            },
        }
    if not external_required and merge_review_required:
        expected_next_action = (
            "review the staged external account import at "
            + import_path_text
            + " and record explicit runtime configuration merge authority "
            "separately; no values were recorded or hashed"
        )
        if not (
            not keys
            and count == 0
            and merge_keys
            and set(merge_keys).issubset(allowed_external_keys)
            and intake.get("status") == "operator_merge_review_required"
            and intake.get("blocking_reason")
            == "external_account_material_merge_review_required"
            and intake.get("operator_import_status") == "ready_for_review"
            and intake.get("operator_import_file_status") == "admissible"
            and intake.get("operator_import_ready_for_review") is True
            and next_action == expected_next_action
        ):
            raise ValueError("runtime_action_context_not_admissible")
        return {
            "blocking_reason": str(intake.get("blocking_reason") or ""),
            "next_action": next_action,
            "operator_projection_key": "operator_review",
            "operator_projection": {
                "lane": "governed_operator_import",
                "drop_path": import_path_text,
                "review_keys": merge_keys,
                "review_key_count": merge_count,
                "locally_stageable_key_count": locally_stageable_count,
                "locally_staged_key_count": locally_staged_count,
                "locally_pending_key_count": locally_pending_count,
                "local_runtime_candidate_path": local_candidate_path_text,
                "local_runtime_candidate_status": intake.get(
                    "local_runtime_candidate_status"
                ),
                "configuration_merge_authorized": False,
                "configuration_merged": False,
                "environment_values_recorded": False,
                "secret_values_recorded": False,
            },
        }
    if (
        not external_required
        and not merge_review_required
        and local_merge_review_required
    ):
        consent_request = scope.get("consent_request")
        preview_scope = (
            consent_request.get("authorization_scope")
            if isinstance(consent_request, Mapping)
            else None
        )
        preview_request_id = str(
            consent_request.get("request_id") or ""
        ) if isinstance(consent_request, Mapping) else ""
        preview_instruction = (
            "reply exactly 'Authorize "
            + preview_request_id
            + "' to authorize only this current add-only configuration merge; "
            "any changed or stale preview requires a new request"
        )
        authorization_request = scope.get("authorization_request")
        authorization_scope = (
            authorization_request.get("authorization_scope")
            if isinstance(authorization_request, Mapping)
            else None
        )
        request_id = str(
            authorization_request.get("request_id") or ""
        ) if isinstance(authorization_request, Mapping) else ""
        authorization_instruction = (
            "reply exactly 'Authorize "
            + request_id
            + "' to authorize only this current add-only configuration merge; "
            "any changed or expired request requires a new authorization, and "
            "deployment or restart remains separately unauthorized"
        )
        expected_next_action = (
            "review the staged local deployment candidate at "
            + local_candidate_path_text
            + " and record explicit runtime configuration merge authority "
            "separately; no values were recorded or hashed"
        )
        if not (
            not keys
            and count == 0
            and not merge_keys
            and merge_count == 0
            and local_merge_keys
            and set(local_merge_keys).issubset(allowed_local_keys)
            and locally_pending_count == 0
            and locally_staged_count == locally_stageable_count
            and intake.get("status") == "local_merge_review_required"
            and intake.get("blocking_reason")
            == "local_deployment_configuration_merge_review_required"
            and intake.get("local_runtime_candidate_status")
            == "ready_for_merge_review"
            and intake.get("local_runtime_candidate_ready_for_merge_review")
            is True
            and next_action == expected_next_action
            and isinstance(consent_request, Mapping)
            and set(consent_request)
            == {
                "required",
                "request_state",
                "request_id",
                "freshness_window_seconds",
                "decision_options",
                "authorization_instruction",
                "authorization_method_required",
                "authorization_scope",
                "authorization_evidence_recorded",
                "authorization_recorded",
                "configuration_merge_authorized",
                "deployment_or_restart_authorized",
                "execution_authorized",
                "protected_operations",
            }
            and consent_request.get("required") is True
            and consent_request.get("request_state")
            == "exact_preview_authorization_pending"
            and runtime_review._RUNTIME_CONSENT_REQUEST_ID.fullmatch(
                preview_request_id
            )
            and consent_request.get("freshness_window_seconds")
            == runtime_review._RUNTIME_CONSENT_FRESHNESS_WINDOW_SECONDS
            and consent_request.get("decision_options")
            == runtime_review._RUNTIME_CONSENT_DECISIONS
            and consent_request.get("authorization_instruction")
            == preview_instruction
            and consent_request.get("authorization_method_required")
            == "authenticated_operator_instruction"
            and isinstance(preview_scope, Mapping)
            and set(preview_scope)
            == {
                "operation",
                "recovery_preview_sha256",
                "candidate_path",
                "candidate_policy_schema",
                "candidate_review_keys",
                "candidate_review_key_count",
                "configuration_merge_policy",
                "existing_environment_values_overwrite_allowed",
                "rollback_required",
                "protected_operations",
            }
            and preview_scope.get("operation")
            == "add_missing_local_environment_candidate"
            and preview_scope.get("recovery_preview_sha256")
            == scope.get("recovery_preview_sha256")
            and _SHA256.fullmatch(
                str(preview_scope.get("recovery_preview_sha256") or "")
            )
            and preview_scope.get("candidate_path")
            == local_candidate_path_text
            and preview_scope.get("candidate_policy_schema")
            == "propertyquarry.deployment_environment_local_runtime_candidate.v2"
            and preview_scope.get("candidate_review_keys")
            == local_merge_keys
            and preview_scope.get("candidate_review_key_count")
            == local_merge_count
            and preview_scope.get("configuration_merge_policy")
            == "add_missing_keys_only"
            and preview_scope.get(
                "existing_environment_values_overwrite_allowed"
            )
            is False
            and preview_scope.get("rollback_required") is True
            and preview_scope.get("protected_operations")
            == ["runtime_configuration_change"]
            and consent_request.get("authorization_evidence_recorded") is False
            and consent_request.get("authorization_recorded") is False
            and consent_request.get("configuration_merge_authorized") is False
            and consent_request.get("deployment_or_restart_authorized") is False
            and consent_request.get("execution_authorized") is False
            and consent_request.get("protected_operations")
            == ["runtime_configuration_change", "deployment_or_restart"]
            and isinstance(authorization_request, Mapping)
            and set(authorization_request)
            == {
                "schema",
                "status",
                "request_id",
                "decision_options",
                "authorization_instruction",
                "authorization_scope",
                "authorization_required",
                "authorization_recorded",
                "configuration_merge_authorized",
                "deployment_or_restart_authorized",
                "execution_authorized",
                "protected_operations",
            }
            and authorization_request.get("schema")
            == _RUNTIME_AUTHORIZATION_PRESENTATION_SCHEMA
            and authorization_request.get("status")
            == "awaiting_explicit_authorization"
            and runtime_authorization._REQUEST_ID.fullmatch(request_id)
            and authorization_request.get("decision_options")
            == ["approve_exact_scope", "reject", "defer"]
            and authorization_request.get("authorization_instruction")
            == authorization_instruction
            and isinstance(authorization_scope, Mapping)
            and authorization_scope
            == {
                "operation": "runtime_configuration_change",
                "change_id": "local_environment_candidate_merge",
                "recovery_preview_sha256": preview_scope.get(
                    "recovery_preview_sha256"
                ),
                "candidate_path": local_candidate_path_text,
                "candidate_policy_schema": preview_scope.get(
                    "candidate_policy_schema"
                ),
                "candidate_review_keys": local_merge_keys,
                "candidate_review_key_count": local_merge_count,
                "configuration_merge_policy": "add_missing_keys_only",
                "existing_environment_values_overwrite_allowed": False,
                "rollback_required": True,
                "compose_project": scope.get("compose_project"),
            }
            and runtime_authorization._scope_kind(authorization_scope)
            == "local_environment_candidate_merge"
            and authorization_request.get("authorization_required") is True
            and authorization_request.get("authorization_recorded") is False
            and authorization_request.get("configuration_merge_authorized")
            is False
            and authorization_request.get("deployment_or_restart_authorized")
            is False
            and authorization_request.get("execution_authorized") is False
            and authorization_request.get("protected_operations")
            == ["runtime_configuration_change", "deployment_or_restart"]
        ):
            raise ValueError("runtime_action_context_not_admissible")
        return {
            "blocking_reason": str(intake.get("blocking_reason") or ""),
            "next_action": authorization_instruction,
            "operator_projection_key": "operator_review",
            "authorization_request": dict(authorization_request),
            "operator_projection": {
                "lane": "governed_local_configuration",
                "candidate_path": local_candidate_path_text,
                "candidate_status": intake.get(
                    "local_runtime_candidate_status"
                ),
                "review_keys": local_merge_keys,
                "review_key_count": local_merge_count,
                "configuration_merge_authorized": False,
                "configuration_merged": False,
                "environment_values_recorded": False,
                "secret_values_recorded": False,
            },
        }
    if (
        keys
        or count != 0
        or merge_keys
        or merge_count != 0
        or local_merge_keys
        or local_merge_count != 0
        or local_merge_review_required
    ):
        raise ValueError("runtime_action_context_not_admissible")
    return None


def build_action_only_projection(
    status: Mapping[str, Any],
    *,
    presentation_context_by_lane: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return only a novel operator interrupt; retained actions stay silent."""

    if status.get("interrupt_operator") is not True:
        return None
    actions = list(status.get("actions") or [])
    source_digest = str(
        status.get("source_cycle_receipt_sha256") or ""
    ).strip()
    if not (
        status.get("status") == "action_required"
        and status.get("action_required") is True
        and actions
        and all(isinstance(action, Mapping) for action in actions)
        and _SHA256.fullmatch(source_digest)
        and status.get("automatic_execution_allowed") is False
        and status.get("provider_quota_consumption_allowed") is False
        and status.get("protected_operation_executed") is False
    ):
        raise ValueError("action_only_projection_not_admissible")
    protected_operations: list[str] = []
    for action in actions:
        if not (
            action.get("consent_required") is True
            and action.get("automatic_execution_allowed") is False
            and action.get("provider_quota_consumption_allowed") is False
        ):
            raise ValueError("action_only_projection_not_admissible")
        for operation in list(action.get("protected_operations") or []):
            normalized = str(operation or "").strip()
            if normalized and normalized not in protected_operations:
                protected_operations.append(normalized)
    if not protected_operations:
        raise ValueError("action_only_projection_not_admissible")
    projection = {
        "schema": SCHEMA,
        "status": "action_required",
        "updated_at": str(status.get("updated_at") or "").strip(),
        "blocking_reason": str(
            status.get("blocking_reason") or ""
        ).strip(),
        "next_action": str(status.get("next_action") or "").strip(),
        "actions": [dict(action) for action in actions],
        "consent_gate": {
            "required": True,
            "protected_operations": protected_operations,
        },
        "source_cycle_receipt_sha256": source_digest,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
    instruction = _runtime_external_input_instruction(
        dict(presentation_context_by_lane or {})
    )
    if instruction is not None and any(
        action.get("lane") == "gold_live_runtime" for action in actions
    ):
        projection["blocking_reason"] = instruction["blocking_reason"]
        projection["next_action"] = instruction["next_action"]
        projection[instruction["operator_projection_key"]] = instruction[
            "operator_projection"
        ]
        if isinstance(instruction.get("authorization_request"), Mapping):
            projection["authorization_request"] = dict(
                instruction["authorization_request"]
            )
        for action in projection["actions"]:
            if action.get("lane") == "gold_live_runtime":
                action["safe_next_action"] = instruction["next_action"]
    return projection


def load_current_runtime_presentation_context(
    status: Mapping[str, Any],
    *,
    packet_path: Path,
    cycle_receipt_path: Path,
    signal_dir: Path,
    live_mobile_receipt_path: Path,
    release_manifest_path: Path,
    deployment_env_path: Path,
    project: str,
    max_age_seconds: float,
    authorization_request_path: Path = (
        runtime_authorization.DEFAULT_REQUEST_PATH
    ),
) -> dict[str, Mapping[str, Any]]:
    """Bind a current runtime action to its verified recovery decision scope."""

    runtime_actions = [
        action
        for action in list(status.get("actions") or [])
        if isinstance(action, Mapping)
        and action.get("lane") == "gold_live_runtime"
    ]
    if not runtime_actions:
        return {}
    if len(runtime_actions) != 1:
        raise ValueError("runtime_action_source_not_admissible")
    verification = runtime_review.verify_current_review_packet(
        packet_path=packet_path,
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=signal_dir,
        live_mobile_receipt_path=live_mobile_receipt_path,
        release_manifest_path=release_manifest_path,
        deployment_env_path=deployment_env_path,
        project=project,
        max_age_seconds=max_age_seconds,
    )
    progress = verification.get("progress")
    packet_verifiable = (
        verification.get("status") == "verified"
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
    )
    if not packet_verifiable:
        blocking_reason = verification.get("blocking_reason")
        if blocking_reason == "review_packet_file_not_admissible":
            # Graceful degradation: an absent or unreadable review packet must
            # not abort the operator projection. The gold-live-runtime action
            # stays projected as REQUIRED and the operator summary renders
            # "review packet: not verified" with the remediation command from
            # its own packet-status branch.
            return {}
        # Stale or rejected packet content is a hard integrity failure and
        # must fail closed.
        raise ValueError("runtime_action_review_not_current")
    if not (
        verification.get("execution_authorized") is False
        and verification.get("protected_operation_executed") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and verification.get("delivery_authorized") is False
    ):
        raise ValueError("runtime_action_review_not_current")
    context = runtime_review.operator_presentation_context(verification)
    if not (
        context.get("schema")
        == "propertyquarry.ooda_operator_presentation_context.v1"
        and context.get("lane") == "gold_live_runtime"
    ):
        raise ValueError("runtime_action_context_not_admissible")
    context_scope = context.get("scope")
    consent_request = (
        context_scope.get("consent_request")
        if isinstance(context_scope, Mapping)
        else None
    )
    if (
        isinstance(consent_request, Mapping)
        and consent_request.get("request_state")
        == "exact_preview_authorization_pending"
    ):
        handoff = runtime_authorization.stage_current_authorization_handoff(
            request_path=authorization_request_path,
            packet_path=packet_path,
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            live_mobile_receipt_path=live_mobile_receipt_path,
            release_manifest_path=release_manifest_path,
            deployment_env_path=deployment_env_path,
            project=project,
        )
        authorization_presentation = _runtime_authorization_presentation(
            handoff,
            context,
        )
        context = dict(context)
        context["scope"] = dict(context_scope)
        context["scope"]["authorization_request"] = (
            authorization_presentation
        )
    return {"gold_live_runtime": context}


def load_trust_action_source(
    *,
    decision_dir: Path,
    claim_verification_path: Path,
    trust_registry_path: Path,
    candidate_dir: Path,
    intake_receipt_path: Path,
    intake_verification_path: Path,
    presentation_state_path: Path,
    notification_receipt_path: Path,
    max_age_seconds: float,
) -> dict[str, Any] | None:
    """Load one verified novel trust-review notification, if present."""

    verification = trust_notification.inspect_current_candidate_notification(
        decision_dir=decision_dir,
        claim_verification_path=claim_verification_path,
        trust_registry_path=trust_registry_path,
        candidate_dir=candidate_dir,
        intake_receipt_path=intake_receipt_path,
        intake_verification_path=intake_verification_path,
        presentation_state_path=presentation_state_path,
        receipt_path=notification_receipt_path,
        max_age_seconds=max_age_seconds,
    )
    if verification.get("status") != "verified":
        raise ValueError("trust_action_source_not_admissible")
    if verification.get("interrupt_operator") is not True:
        return None
    receipt, _raw, digest = trust_intake._private_snapshot(
        notification_receipt_path,
        field="action_only_trust_candidate_notification",
    )
    candidate_scope = receipt.get("candidate_scope")
    review_id = str(receipt.get("candidate_review_id") or "")
    if not (
        verification.get("notification_status") == "action_required"
        and verification.get("action_required") is True
        and verification.get("delivery_authorized") is False
        and verification.get("delivery_attempted") is False
        and verification.get("sent") is False
        and verification.get("trust_enrollment_authorized") is False
        and verification.get("trust_registry_modified") is False
        and verification.get("protected_operation_executed") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and receipt.get("schema") == trust_notification.SCHEMA
        and receipt.get("status") == "action_required"
        and receipt.get("execution_mode") == "evaluate_only"
        and receipt.get("action_required") is True
        and receipt.get("interrupt_operator") is True
        and receipt.get("operator_review_required") is True
        and receipt.get("would_send") is True
        and receipt.get("delivery_authorized") is False
        and receipt.get("delivery_attempted") is False
        and receipt.get("sent") is False
        and receipt.get("presentation_recorded") is False
        and receipt.get("trust_enrollment_authorized") is False
        and receipt.get("trust_registry_modified") is False
        and receipt.get("decision_confers_trust") is False
        and receipt.get("private_key_material_requested") is False
        and receipt.get("private_key_material_recorded") is False
        and receipt.get("automatic_execution_allowed") is False
        and receipt.get("execution_authorized") is False
        and receipt.get("deployment_or_restart_authorized") is False
        and receipt.get("protected_operation_executed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("secret_values_recorded") is False
        and review_id == verification.get("candidate_review_id")
        and review_id.startswith("pqtrustreview_")
        and isinstance(candidate_scope, list)
        and bool(candidate_scope)
        and all(isinstance(row, Mapping) for row in candidate_scope)
        and digest == verification.get("notification_receipt_sha256")
        and _SHA256.fullmatch(digest)
        and isinstance(receipt.get("next_action"), str)
        and str(receipt.get("next_action") or "").strip()
    ):
        raise ValueError("trust_action_source_not_admissible")
    return {
        "receipt": dict(receipt),
        "receipt_path": str(Path(notification_receipt_path).absolute()),
        "receipt_sha256": digest,
    }


def build_trust_action_only_projection(
    source: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = source.get("receipt")
    receipt_path = str(source.get("receipt_path") or "").strip()
    receipt_digest = str(source.get("receipt_sha256") or "").strip()
    if not (
        isinstance(receipt, Mapping)
        and receipt_path
        and _SHA256.fullmatch(receipt_digest)
    ):
        raise ValueError("trust_action_projection_not_admissible")
    review_id = str(receipt.get("candidate_review_id") or "").strip()
    candidate_scope = list(receipt.get("candidate_scope") or [])
    next_action = str(receipt.get("next_action") or "").strip()
    if not (
        review_id.startswith("pqtrustreview_")
        and candidate_scope
        and all(isinstance(row, Mapping) for row in candidate_scope)
        and next_action
    ):
        raise ValueError("trust_action_projection_not_admissible")
    action = {
        "lane": "source_refresh_trust_candidate_review",
        "reason": "producer_identity_review_required",
        "candidate_review_id": review_id,
        "candidate_scope": [dict(row) for row in candidate_scope],
        "safe_next_action": next_action,
        "consent_required": True,
        "automatic_execution_allowed": False,
        "protected_operations": list(_TRUST_PROTECTED_OPERATIONS),
        "provider_quota_consumption_allowed": False,
    }
    return {
        "schema": SCHEMA,
        "status": "action_required",
        "updated_at": str(receipt.get("generated_at") or "").strip(),
        "blocking_reason": "producer_identity_review_required",
        "next_action": next_action,
        "actions": [action],
        "consent_gate": {
            "required": True,
            "protected_operations": list(_TRUST_PROTECTED_OPERATIONS),
        },
        "source": {
            "type": "trust_candidate_notification",
            "receipt_path": receipt_path,
            "receipt_sha256": receipt_digest,
        },
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Print and record only a fresh novel PropertyQuarry operator action."
        )
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=operator_status.DEFAULT_CYCLE_RECEIPT,
    )
    parser.add_argument(
        "--signal-dir",
        type=Path,
        default=operator_status.DEFAULT_SIGNAL_DIR,
    )
    parser.add_argument(
        "--presentation-state",
        type=Path,
        default=operator_status.DEFAULT_PRESENTATION_STATE,
    )
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=operator_status.DEFAULT_MAX_AGE_SECONDS,
    )
    parser.add_argument(
        "--runtime-review-packet",
        type=Path,
        default=runtime_review.DEFAULT_PACKET_PATH,
    )
    parser.add_argument(
        "--runtime-review-live-mobile-receipt",
        type=Path,
        default=runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    )
    parser.add_argument(
        "--runtime-review-release-manifest",
        type=Path,
        default=runtime_review.DEFAULT_RELEASE_MANIFEST,
    )
    parser.add_argument(
        "--runtime-review-deployment-env",
        type=Path,
        default=runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH,
    )
    parser.add_argument(
        "--runtime-review-project",
        default=local_deployment.DEFAULT_COMPOSE_PROJECT,
    )
    parser.add_argument(
        "--runtime-authorization-request",
        type=Path,
        default=runtime_authorization.DEFAULT_REQUEST_PATH,
    )
    parser.add_argument(
        "--runtime-control-receipt",
        type=Path,
        default=DEFAULT_RUNTIME_CONTROL_RECEIPT,
    )
    parser.add_argument(
        "--runtime-control-lock",
        type=Path,
        default=DEFAULT_RUNTIME_CONTROL_LOCK,
    )
    parser.add_argument(
        "--safe-tick-receipt",
        type=Path,
        default=DEFAULT_SAFE_TICK_RECEIPT,
    )
    parser.add_argument(
        "--runtime-control-effective-verification",
        type=Path,
        default=DEFAULT_RUNTIME_CONTROL_EFFECTIVE_VERIFICATION,
    )
    parser.add_argument(
        "--trust-notification-receipt",
        type=Path,
        default=trust_notification.DEFAULT_RECEIPT_PATH,
    )
    parser.add_argument(
        "--trust-presentation-state",
        type=Path,
        default=trust_notification.DEFAULT_PRESENTATION_STATE_PATH,
    )
    parser.add_argument(
        "--trust-decision-dir",
        type=Path,
        default=trust_decision.DEFAULT_DECISION_DIR,
    )
    parser.add_argument(
        "--trust-claim-verification",
        type=Path,
        default=source_claims.DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--trust-registry",
        type=Path,
        default=source_claims.DEFAULT_TRUST_REGISTRY_PATH,
    )
    parser.add_argument(
        "--trust-candidate-dir",
        type=Path,
        default=trust_intake.DEFAULT_CANDIDATE_DIR,
    )
    parser.add_argument(
        "--trust-intake-receipt",
        type=Path,
        default=trust_intake.DEFAULT_RECEIPT_PATH,
    )
    parser.add_argument(
        "--trust-intake-verification",
        type=Path,
        default=trust_intake.DEFAULT_VERIFICATION_PATH,
    )
    args = parser.parse_args(argv)

    try:
        status = operator_status.load_operator_status(
            cycle_receipt_path=args.receipt,
            signal_dir=args.signal_dir,
            max_age_seconds=args.max_age_seconds,
        )
        if status.get("status") == "blocked":
            raise ValueError("action_only_source_not_admissible")
        presentation_contexts = load_current_runtime_presentation_context(
            status,
            packet_path=args.runtime_review_packet,
            cycle_receipt_path=args.receipt,
            signal_dir=args.signal_dir,
            live_mobile_receipt_path=(
                args.runtime_review_live_mobile_receipt
            ),
            release_manifest_path=args.runtime_review_release_manifest,
            deployment_env_path=args.runtime_review_deployment_env,
            project=args.runtime_review_project,
            max_age_seconds=args.max_age_seconds,
            authorization_request_path=(
                args.runtime_authorization_request
            ),
        )
        status = (
            operator_status.project_operator_presentation_with_context_hydration(
                status,
                state_path=args.presentation_state,
                presentation_context_by_lane=presentation_contexts,
            )
        )
        if status.get("status") == "blocked":
            raise ValueError("action_only_source_not_admissible")
        projection = build_action_only_projection(
            status,
            presentation_context_by_lane=presentation_contexts,
        )
        projection_kind = "cycle"
        trust_source: dict[str, Any] | None = None
        if projection is None:
            trust_source = load_trust_action_source(
                decision_dir=args.trust_decision_dir,
                claim_verification_path=args.trust_claim_verification,
                trust_registry_path=args.trust_registry,
                candidate_dir=args.trust_candidate_dir,
                intake_receipt_path=args.trust_intake_receipt,
                intake_verification_path=args.trust_intake_verification,
                presentation_state_path=args.trust_presentation_state,
                notification_receipt_path=(
                    args.trust_notification_receipt
                ),
                max_age_seconds=args.max_age_seconds,
            )
            if trust_source is not None:
                projection = build_trust_action_only_projection(
                    trust_source
                )
                projection_kind = "trust_candidate"
    except Exception:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": "blocked",
                    "blocking_reason": "action_only_projection_unavailable",
                    "automatic_execution_allowed": False,
                    "execution_authorized": False,
                    "deployment_or_restart_authorized": False,
                    "protected_operation_executed": False,
                    "provider_quota_consumption_allowed": False,
                    "delivery_authorized": False,
                    "delivery_attempted": False,
                    "sent": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    if projection is None:
        return 0

    print(json.dumps(projection, sort_keys=True), flush=True)
    if projection_kind == "cycle":
        presentation_receipt = operator_status.record_operator_presentation(
            status,
            state_path=args.presentation_state,
            expected_source_cycle_receipt_sha256=str(
                status.get("source_cycle_receipt_sha256") or ""
            ),
        )
        presentation_recorded = bool(
            presentation_receipt.get("status") in {"recorded", "unchanged"}
            and presentation_receipt.get("delivery_state_updated") is False
            and presentation_receipt.get("provider_quota_consumed") is False
            and presentation_receipt.get("protected_operation_executed")
            is False
        )
    else:
        review_id = str(
            projection["actions"][0].get("candidate_review_id") or ""
        )
        presentation_receipt = (
            trust_notification.record_current_candidate_notification_presentation(
                expected_candidate_review_id=review_id,
                decision_dir=args.trust_decision_dir,
                claim_verification_path=args.trust_claim_verification,
                trust_registry_path=args.trust_registry,
                candidate_dir=args.trust_candidate_dir,
                intake_receipt_path=args.trust_intake_receipt,
                intake_verification_path=args.trust_intake_verification,
                presentation_state_path=args.trust_presentation_state,
                receipt_path=args.trust_notification_receipt,
                max_age_seconds=args.max_age_seconds,
            )
        )
        presentation_recorded = bool(
            presentation_receipt.get("status") == "verified"
            and presentation_receipt.get("notification_status")
            == "deduplicated"
            and presentation_receipt.get("presentation_transition_recorded")
            is True
            and presentation_receipt.get("action_required") is True
            and presentation_receipt.get("interrupt_operator") is False
            and presentation_receipt.get("delivery_authorized") is False
            and presentation_receipt.get("delivery_attempted") is False
            and presentation_receipt.get("sent") is False
            and presentation_receipt.get("trust_enrollment_authorized")
            is False
            and presentation_receipt.get("trust_registry_modified") is False
            and presentation_receipt.get("protected_operation_executed")
            is False
            and presentation_receipt.get("provider_quota_consumption_allowed")
            is False
        )
    if not presentation_recorded:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": "blocked",
                    "blocking_reason": "action_only_presentation_not_recorded",
                    "automatic_execution_allowed": False,
                    "execution_authorized": False,
                    "deployment_or_restart_authorized": False,
                    "protected_operation_executed": False,
                    "provider_quota_consumption_allowed": False,
                    "delivery_authorized": False,
                    "delivery_attempted": False,
                    "sent": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    if (
        projection_kind == "cycle"
        and any(
            isinstance(action, Mapping)
            and action.get("lane") == "gold_live_runtime"
            for action in list(projection.get("actions") or [])
        )
        # Fresh-host tolerance: summary.sh passes no --runtime-control-receipt,
        # so an argument left at the argparse default means the host controller
        # has never published a receipt - there is no presentation binding to
        # settle and no effective state to verify, and the print-only projection
        # proceeds (first-run hosts must render the action summary; the rendered
        # consent gate still shows required=True with execution unauthorized).
        # Callers that explicitly pass the flag (unit tests, host controllers)
        # keep the fail-closed settlement and effective-verification gates below
        # fully active.
        and str(args.runtime_control_receipt) != str(DEFAULT_RUNTIME_CONTROL_RECEIPT)
    ):
        settlement = settle_runtime_control_presentation(
            projection,
            presentation_receipt,
            receipt_path=args.runtime_control_receipt,
            lock_path=args.runtime_control_lock,
        )
        if not (
            settlement.get("status") == "settled"
            and settlement.get("current_evidence_verified") is True
            and settlement.get("automatic_execution_allowed") is False
            and settlement.get("execution_authorized") is False
            and settlement.get("deployment_or_restart_authorized") is False
            and settlement.get("protected_operation_executed") is False
            and settlement.get("provider_quota_consumption_allowed") is False
            and settlement.get("delivery_authorized") is False
            and settlement.get("delivery_attempted") is False
            and settlement.get("sent") is False
        ):
            print(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "status": "blocked",
                        "blocking_reason": (
                            "runtime_control_presentation_not_settled"
                        ),
                        "settlement": settlement,
                        "automatic_execution_allowed": False,
                        "execution_authorized": False,
                        "deployment_or_restart_authorized": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                        "delivery_attempted": False,
                        "sent": False,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 3
        effective_verification = verify_effective_runtime_control(
            safe_tick_receipt_path=args.safe_tick_receipt,
            runtime_control_receipt_path=args.runtime_control_receipt,
            presentation_state_path=args.presentation_state,
            verification_path=(
                args.runtime_control_effective_verification
            ),
            lock_path=args.runtime_control_lock,
        )
        if not (
            effective_verification.get("status") == "verified"
            and effective_verification.get("historical_tick_preserved") is True
            and effective_verification.get("action_required") is True
            and effective_verification.get("interrupt_operator") is False
            and effective_verification.get("presentation_recorded") is True
            and effective_verification.get("current_evidence_verified") is True
            and effective_verification.get("verification_receipt_persisted")
            is True
            and effective_verification.get("automatic_execution_allowed")
            is False
            and effective_verification.get("execution_authorized") is False
            and effective_verification.get("deployment_or_restart_authorized")
            is False
            and effective_verification.get("protected_operation_executed")
            is False
            and effective_verification.get(
                "provider_quota_consumption_allowed"
            )
            is False
            and effective_verification.get("delivery_authorized") is False
            and effective_verification.get("delivery_attempted") is False
            and effective_verification.get("sent") is False
        ):
            print(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "status": "blocked",
                        "blocking_reason": (
                            "effective_runtime_control_not_verified"
                        ),
                        "verification": effective_verification,
                        "automatic_execution_allowed": False,
                        "execution_authorized": False,
                        "deployment_or_restart_authorized": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                        "delivery_attempted": False,
                        "sent": False,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
