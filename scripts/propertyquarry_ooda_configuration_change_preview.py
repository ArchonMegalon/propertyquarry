#!/usr/bin/env python3
"""Stage an immutable, non-applying preview for one approved config change."""

from __future__ import annotations

import argparse
import difflib
import json
import math
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

from scripts import propertyquarry_ooda_authorization_decision as authorization_decision
from scripts import propertyquarry_ooda_authorization_request as authorization_request
from scripts import propertyquarry_ooda_configuration_plan as configuration_plan
from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts.propertyquarry_secure_file_io import OutputExistsError, atomic_write_bytes


SCHEMA = "propertyquarry.ooda_runtime_configuration_change_preview.v1"
VERIFY_SCHEMA = (
    "propertyquarry.ooda_runtime_configuration_change_preview_verification.v1"
)
HANDOFF_SCHEMA = (
    "propertyquarry.ooda_runtime_configuration_change_preview_handoff.v1"
)
DEFAULT_PREVIEW_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/runtime-configuration-change-previews"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "runtime-configuration-change-preview-verification.json"
)
MAX_PREVIEW_BYTES = 1024 * 1024
MAX_DIFF_BYTES = 256 * 1024
_PREVIEW_ID = re.compile(r"pqcv_[0-9a-f]{24}\Z")


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


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": "refresh and reverify the exact request, decision, plan, and source snapshot",
        "current_evidence_verified": False,
        "preview_verified": False,
        "authorization_required": True,
        "authorization_recorded": False,
        "exact_scope_authorized": False,
        "manual_apply_authorized": False,
        "automatic_apply_allowed": False,
        "source_edit_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _not_authorized(
    *,
    decision: str,
    request_id: str,
    request_sha256: str,
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema": HANDOFF_SCHEMA,
        "status": "not_authorized",
        "updated_at": now.isoformat(),
        "blocking_reason": "explicit_exact_scope_approval_required",
        "next_action": "record approve_exact_scope before staging an exact change preview",
        "authorization_decision": decision or "pending",
        "request_id": request_id,
        "request_sha256": request_sha256,
        "preview_state": "absent",
        "preview_state_updated": False,
        "current_evidence_verified": True,
        "preview_verified": False,
        "authorization_required": True,
        "authorization_recorded": decision in {"reject", "defer"},
        "exact_scope_authorized": False,
        "manual_apply_authorized": False,
        "automatic_apply_allowed": False,
        "source_edit_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _preview_path(preview_dir: Path, preview_id: str) -> Path:
    if _PREVIEW_ID.fullmatch(preview_id) is None:
        raise ValueError("configuration_change_preview_id_invalid")
    return runtime_review._rooted(preview_dir) / f"{preview_id}.json"


def _unified_diff(before: str, after: str, *, reverse: bool = False) -> str:
    source, target = (after, before) if reverse else (before, after)
    from_name = (
        f"a/{configuration_plan.COMPOSE_PATH.as_posix()}"
        if not reverse
        else f"b/{configuration_plan.COMPOSE_PATH.as_posix()}"
    )
    to_name = (
        f"b/{configuration_plan.COMPOSE_PATH.as_posix()}"
        if not reverse
        else f"a/{configuration_plan.COMPOSE_PATH.as_posix()}"
    )
    result = "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            target.splitlines(keepends=True),
            fromfile=from_name,
            tofile=to_name,
        )
    )
    if not result or len(result.encode("utf-8")) > MAX_DIFF_BYTES:
        raise ValueError("configuration_change_preview_diff_not_admissible")
    return result


def _current_inputs(
    *,
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
) -> tuple[
    dict[str, Any],
    str,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    str,
    bytes,
    str,
]:
    request, request_sha256, request_verification, decision_verification, sources = (
        configuration_plan._current_inputs(
            request_path=request_path,
            packet_path=packet_path,
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            live_mobile_receipt_path=live_mobile_receipt_path,
            release_manifest_path=release_manifest_path,
            decision_dir=decision_dir,
            project=project,
            root=root,
            now=now,
        )
    )
    plan, _plan_raw, plan_sha256 = authorization_request._private_object(
        plan_path,
        field="runtime_configuration_plan",
        maximum_bytes=configuration_plan.MAX_PLAN_BYTES,
    )
    plan_verification = configuration_plan.verify_configuration_plan(
        plan,
        request=request,
        request_verification=request_verification,
        decision_verification=decision_verification,
        source_posture=sources,
        now=now,
    )
    if not (
        plan_verification.get("status") == "verified"
        and plan_verification.get("request_sha256") == request_sha256
        and plan_verification.get("plan_sha256") == plan_sha256
    ):
        raise ValueError("configuration_change_preview_plan_not_current")
    plan_verification["progress"]["current_evidence_verified"] = True
    source_text, source_raw, source_sha256 = configuration_plan._source_snapshot(
        configuration_plan.COMPOSE_PATH,
        root=root,
    )
    return (
        request,
        request_sha256,
        plan,
        plan_verification,
        decision_verification,
        sources,
        source_text,
        source_raw,
        source_sha256,
    )


def build_configuration_change_preview(
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    plan: Mapping[str, Any],
    plan_verification: Mapping[str, Any],
    decision_verification: Mapping[str, Any],
    source_posture: Mapping[str, Any],
    source_text: str,
    source_raw: bytes,
    source_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = _now(now)
    expires_at = _parse_timestamp(plan.get("expires_at"))
    change = plan_verification.get("change")
    scope = request.get("scope")
    target = source_posture.get("target")
    if not (
        expires_at is not None
        and generated_at <= expires_at
        and request_sha256 == _sha256(_canonical(request))
        and plan_verification.get("status") == "verified"
        and plan_verification.get("progress", {}).get("current_evidence_verified")
        is True
        and plan_verification.get("request_id") == request.get("request_id")
        and plan_verification.get("request_sha256") == request_sha256
        and plan_verification.get("plan_status") == "exact_scope_authorized"
        and plan_verification.get("authorization_recorded") is True
        and plan_verification.get("exact_scope_authorized") is True
        and plan_verification.get("manual_apply_authorized") is True
        and plan_verification.get("automatic_apply_allowed") is False
        and plan_verification.get("apply_performed") is False
        and plan_verification.get("execution_authorized") is False
        and plan_verification.get("execution_performed") is False
        and plan_verification.get("deployment_or_restart_authorized") is False
        and plan_verification.get("protected_operation_executed") is False
        and plan_verification.get("provider_quota_consumption_allowed") is False
        and plan_verification.get("delivery_authorized") is False
        and decision_verification.get("status") == "verified"
        and decision_verification.get("progress", {}).get(
            "current_evidence_verified"
        )
        is True
        and decision_verification.get("decision") == "approve_exact_scope"
        and decision_verification.get("request_id") == request.get("request_id")
        and decision_verification.get("request_sha256") == request_sha256
        and authorization_request._SHA256.fullmatch(
            str(decision_verification.get("decision_sha256") or "")
        )
        and decision_verification.get("exact_scope_authorized") is True
        and decision_verification.get("automatic_execution_allowed") is False
        and decision_verification.get("execution_authorized") is False
        and decision_verification.get("execution_performed") is False
        and decision_verification.get("deployment_or_restart_authorized") is False
        and decision_verification.get("protected_operation_executed") is False
        and decision_verification.get("provider_quota_consumption_allowed") is False
        and decision_verification.get("delivery_authorized") is False
        and isinstance(scope, Mapping)
        and decision_verification.get("scope") == scope
        and isinstance(change, Mapping)
        and change.get("operation") == "replace_exact_text"
        and change.get("path") == configuration_plan.COMPOSE_PATH.as_posix()
        and change.get("selector") == "services.propertyquarry-api.ports[0]"
        and change.get("expected_before_sha256") == source_sha256
        and change.get("current_expression")
        == configuration_plan.CURRENT_PORT_EXPRESSION
        and change.get("proposed_expression")
        == configuration_plan.PROPOSED_PORT_EXPRESSION
        and change.get("change_required") is True
        and change.get("replacement_count") == 1
        and change.get("rollback_expression")
        == configuration_plan.CURRENT_PORT_EXPRESSION
        and isinstance(target, Mapping)
        and target.get("sha256") == source_sha256
        and target.get("current_expression_count") == 1
        and target.get("proposed_expression_count") == 0
        and source_sha256 == _sha256(source_raw)
        and source_text.count(configuration_plan.CURRENT_PORT_EXPRESSION) == 1
        and source_text.count(configuration_plan.PROPOSED_PORT_EXPRESSION) == 0
    ):
        raise ValueError("configuration_change_preview_input_not_admissible")
    after_text = source_text.replace(
        configuration_plan.CURRENT_PORT_EXPRESSION,
        configuration_plan.PROPOSED_PORT_EXPRESSION,
        1,
    )
    after_raw = after_text.encode("utf-8")
    forward_diff = _unified_diff(source_text, after_text)
    rollback_diff = _unified_diff(source_text, after_text, reverse=True)
    binding = {
        "request_id": str(request.get("request_id") or ""),
        "request_sha256": request_sha256,
        "decision_id": str(decision_verification.get("decision_id") or ""),
        "decision_sha256": str(
            decision_verification.get("decision_sha256") or ""
        ),
        "plan_id": str(plan_verification.get("plan_id") or ""),
        "plan_sha256": str(plan_verification.get("plan_sha256") or ""),
        "source_fingerprint_sha256": str(
            source_posture.get("fingerprint_sha256") or ""
        ),
    }
    preview = {
        "operation": "replace_exact_text_preview",
        "path": configuration_plan.COMPOSE_PATH.as_posix(),
        "selector": "services.propertyquarry-api.ports[0]",
        "before_sha256": source_sha256,
        "after_sha256": _sha256(after_raw),
        "before_bytes": len(source_raw),
        "after_bytes": len(after_raw),
        "replacement_count": 1,
        "current_expression": configuration_plan.CURRENT_PORT_EXPRESSION,
        "proposed_expression": configuration_plan.PROPOSED_PORT_EXPRESSION,
        "forward_unified_diff": forward_diff,
        "forward_unified_diff_sha256": _sha256(forward_diff.encode("utf-8")),
        "rollback_unified_diff": rollback_diff,
        "rollback_unified_diff_sha256": _sha256(
            rollback_diff.encode("utf-8")
        ),
    }
    preview_digest = _sha256(
        _canonical({"binding": binding, "scope": scope, "preview": preview})
    )
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "preview_id": f"pqcv_{preview_digest[:24]}",
        "generated_at": generated_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "status": "exact_change_preview_ready",
        "next_action": (
            "review the forward and rollback diffs; any source edit remains a separate manual action, "
            "and deployment or restart remains excluded"
        ),
        "binding": binding,
        "scope": dict(scope),
        "preview": preview,
        "authorization": {
            "required": True,
            "recorded": True,
            "decision": "approve_exact_scope",
            "exact_scope_authorized": True,
        },
        "manual_apply_authorized": True,
        "automatic_apply_allowed": False,
        "source_edit_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "secret_values_recorded": False,
    }
    artifact["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(artifact)),
    }
    if len(_canonical(artifact)) > MAX_PREVIEW_BYTES:
        raise ValueError("configuration_change_preview_too_large")
    return artifact


def verify_configuration_change_preview(
    artifact: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    plan: Mapping[str, Any],
    plan_verification: Mapping[str, Any],
    decision_verification: Mapping[str, Any],
    source_posture: Mapping[str, Any],
    source_text: str,
    source_raw: bytes,
    source_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    normalized = dict(artifact)
    integrity = normalized.pop("integrity", None)
    if not (
        isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    ):
        return _blocked(
            "configuration_change_preview_integrity_invalid",
            now=observed_now,
        )
    generated_at = _parse_timestamp(artifact.get("generated_at"))
    expires_at = _parse_timestamp(artifact.get("expires_at"))
    if generated_at is None or expires_at is None:
        return _blocked(
            "configuration_change_preview_timestamp_invalid",
            now=observed_now,
        )
    age_seconds = (observed_now - generated_at).total_seconds()
    if not (
        math.isfinite(age_seconds)
        and age_seconds >= -30.0
        and generated_at <= expires_at
        and observed_now <= expires_at
    ):
        return _blocked(
            "configuration_change_preview_not_fresh",
            now=observed_now,
        )
    try:
        expected = build_configuration_change_preview(
            request=request,
            request_sha256=request_sha256,
            plan=plan,
            plan_verification=plan_verification,
            decision_verification=decision_verification,
            source_posture=source_posture,
            source_text=source_text,
            source_raw=source_raw,
            source_sha256=source_sha256,
            now=generated_at,
        )
    except (TypeError, ValueError):
        return _blocked(
            "configuration_change_preview_inputs_not_admissible",
            now=observed_now,
        )
    if not (
        dict(artifact) == expected
        and _PREVIEW_ID.fullmatch(str(artifact.get("preview_id") or ""))
        and artifact.get("manual_apply_authorized") is True
        and artifact.get("automatic_apply_allowed") is False
        and artifact.get("source_edit_performed") is False
        and artifact.get("execution_authorized") is False
        and artifact.get("execution_performed") is False
        and artifact.get("deployment_or_restart_authorized") is False
        and artifact.get("protected_operation_executed") is False
        and artifact.get("provider_quota_consumption_allowed") is False
        and artifact.get("delivery_authorized") is False
        and artifact.get("secret_values_recorded") is False
    ):
        return _blocked(
            "configuration_change_preview_contract_not_admissible",
            now=observed_now,
        )
    preview = dict(artifact.get("preview") or {})
    binding = dict(artifact.get("binding") or {})
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": str(artifact.get("next_action") or ""),
        "preview_id": str(artifact.get("preview_id") or ""),
        "preview_sha256": _sha256(_canonical(artifact)),
        "request_id": str(binding.get("request_id") or ""),
        "request_sha256": str(binding.get("request_sha256") or ""),
        "decision_id": str(binding.get("decision_id") or ""),
        "decision_sha256": str(binding.get("decision_sha256") or ""),
        "plan_id": str(binding.get("plan_id") or ""),
        "plan_sha256": str(binding.get("plan_sha256") or ""),
        "expires_at": str(artifact.get("expires_at") or ""),
        "preview": preview,
        "current_evidence_verified": False,
        "preview_verified": True,
        "authorization_required": True,
        "authorization_recorded": True,
        "exact_scope_authorized": True,
        "manual_apply_authorized": True,
        "automatic_apply_allowed": False,
        "source_edit_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _ready_handoff(
    verification: Mapping[str, Any],
    *,
    preview_path: Path,
    preview_state: str,
    preview_state_updated: bool,
    now: datetime,
) -> dict[str, Any]:
    if not (
        verification.get("status") == "verified"
        and verification.get("current_evidence_verified") is True
        and verification.get("manual_apply_authorized") is True
        and verification.get("automatic_apply_allowed") is False
        and verification.get("source_edit_performed") is False
        and verification.get("execution_authorized") is False
        and verification.get("execution_performed") is False
        and verification.get("deployment_or_restart_authorized") is False
        and verification.get("protected_operation_executed") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and verification.get("delivery_authorized") is False
    ):
        return _blocked(
            "configuration_change_preview_handoff_not_admissible",
            now=now,
        )
    return {
        "schema": HANDOFF_SCHEMA,
        "status": "ready",
        "updated_at": now.isoformat(),
        "blocking_reason": "",
        "next_action": str(verification.get("next_action") or ""),
        "preview_path": str(preview_path),
        "preview_state": preview_state,
        "preview_state_updated": preview_state_updated,
        "preview_id": str(verification.get("preview_id") or ""),
        "preview_sha256": str(verification.get("preview_sha256") or ""),
        "request_id": str(verification.get("request_id") or ""),
        "request_sha256": str(verification.get("request_sha256") or ""),
        "decision_id": str(verification.get("decision_id") or ""),
        "decision_sha256": str(verification.get("decision_sha256") or ""),
        "plan_id": str(verification.get("plan_id") or ""),
        "plan_sha256": str(verification.get("plan_sha256") or ""),
        "expires_at": str(verification.get("expires_at") or ""),
        "preview": dict(verification.get("preview") or {}),
        "current_evidence_verified": True,
        "preview_verified": True,
        "authorization_required": True,
        "authorization_recorded": True,
        "exact_scope_authorized": True,
        "manual_apply_authorized": True,
        "automatic_apply_allowed": False,
        "source_edit_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def stage_current_configuration_change_preview(
    *,
    preview_dir: Path = DEFAULT_PREVIEW_DIR,
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
    """Stage one immutable preview; never edit source or execute the change."""

    observed_now = _now(now)
    try:
        (
            request,
            request_sha256,
            plan,
            plan_verification,
            decision_verification,
            sources,
            source_text,
            source_raw,
            source_sha256,
        ) = _current_inputs(
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
        return _blocked(
            "configuration_change_preview_current_evidence_unavailable",
            now=observed_now,
        )
    decision_value = str(decision_verification.get("decision") or "pending")
    if not (
        plan_verification.get("manual_apply_authorized") is True
        and decision_verification.get("status") == "verified"
        and decision_value == "approve_exact_scope"
        and decision_verification.get("exact_scope_authorized") is True
    ):
        return _not_authorized(
            decision=decision_value,
            request_id=str(request.get("request_id") or ""),
            request_sha256=request_sha256,
            now=observed_now,
        )
    try:
        expected = build_configuration_change_preview(
            request=request,
            request_sha256=request_sha256,
            plan=plan,
            plan_verification=plan_verification,
            decision_verification=decision_verification,
            source_posture=sources,
            source_text=source_text,
            source_raw=source_raw,
            source_sha256=source_sha256,
            now=observed_now,
        )
        preview_path = _preview_path(
            preview_dir,
            str(expected.get("preview_id") or ""),
        )
    except (OSError, TypeError, ValueError):
        return _blocked(
            "configuration_change_preview_build_failed",
            now=observed_now,
        )
    preview_state = "refreshed"
    preview_state_updated = True
    try:
        atomic_write_bytes(
            preview_path,
            _canonical(expected),
            overwrite=False,
        )
    except OutputExistsError:
        preview_state = "reused"
        preview_state_updated = False
    except Exception:
        return _blocked(
            "configuration_change_preview_write_failed",
            now=observed_now,
        )
    try:
        persisted, _raw, persisted_sha256 = authorization_request._private_object(
            preview_path,
            field="runtime_configuration_change_preview",
            maximum_bytes=MAX_PREVIEW_BYTES,
        )
    except Exception:
        return _blocked(
            "configuration_change_preview_file_not_admissible",
            now=observed_now,
        )
    verification = verify_configuration_change_preview(
        persisted,
        request=request,
        request_sha256=request_sha256,
        plan=plan,
        plan_verification=plan_verification,
        decision_verification=decision_verification,
        source_posture=sources,
        source_text=source_text,
        source_raw=source_raw,
        source_sha256=source_sha256,
        now=observed_now,
    )
    if not (
        (preview_state == "reused" or persisted == expected)
        and verification.get("status") == "verified"
        and verification.get("preview_sha256") == persisted_sha256
        and verification.get("preview_id") == expected.get("preview_id")
    ):
        return _blocked(
            "configuration_change_preview_persisted_not_verified",
            now=observed_now,
        )
    verification["current_evidence_verified"] = True
    return _ready_handoff(
        verification,
        preview_path=preview_path,
        preview_state=preview_state,
        preview_state_updated=preview_state_updated,
        now=observed_now,
    )


def verify_current_configuration_change_preview(
    **kwargs: Any,
) -> dict[str, Any]:
    """Verify the exact preview derived from current evidence without writing it."""

    observed_now = _now(kwargs.pop("now", None))
    preview_dir = Path(kwargs.pop("preview_dir", DEFAULT_PREVIEW_DIR))
    try:
        (
            request,
            request_sha256,
            plan,
            plan_verification,
            decision_verification,
            sources,
            source_text,
            source_raw,
            source_sha256,
        ) = _current_inputs(now=observed_now, **kwargs)
        expected = build_configuration_change_preview(
            request=request,
            request_sha256=request_sha256,
            plan=plan,
            plan_verification=plan_verification,
            decision_verification=decision_verification,
            source_posture=sources,
            source_text=source_text,
            source_raw=source_raw,
            source_sha256=source_sha256,
            now=observed_now,
        )
        preview_path = _preview_path(
            preview_dir,
            str(expected.get("preview_id") or ""),
        )
        persisted, _raw, persisted_sha256 = authorization_request._private_object(
            preview_path,
            field="runtime_configuration_change_preview",
            maximum_bytes=MAX_PREVIEW_BYTES,
        )
    except Exception:
        return _blocked(
            "configuration_change_preview_current_evidence_unavailable",
            now=observed_now,
        )
    verification = verify_configuration_change_preview(
        persisted,
        request=request,
        request_sha256=request_sha256,
        plan=plan,
        plan_verification=plan_verification,
        decision_verification=decision_verification,
        source_posture=sources,
        source_text=source_text,
        source_raw=source_raw,
        source_sha256=source_sha256,
        now=observed_now,
    )
    if not (
        verification.get("status") == "verified"
        and verification.get("preview_sha256") == persisted_sha256
    ):
        return verification
    verification["current_evidence_verified"] = True
    verification["preview_path"] = str(preview_path)
    return verification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage or verify an immutable PropertyQuarry source-change preview. "
            "This command never edits source, deploys, restarts, calls providers, or sends."
        )
    )
    parser.add_argument("--verify-current", action="store_true")
    parser.add_argument("--preview-dir", type=Path, default=DEFAULT_PREVIEW_DIR)
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
    inputs = {
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
    result = (
        verify_current_configuration_change_preview(**inputs)
        if args.verify_current
        else stage_current_configuration_change_preview(**inputs)
    )
    result["verification_receipt_persisted"] = True
    try:
        atomic_write_bytes(
            runtime_review._rooted(args.verification_write),
            _canonical(result),
            overwrite=True,
        )
    except Exception:
        result["verification_receipt_persisted"] = False
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") in {"ready", "verified", "not_authorized"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
