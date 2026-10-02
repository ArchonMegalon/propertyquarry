#!/usr/bin/env python3
"""Project one fail-closed operator status from the approved OODA cycle."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_notification_cycle as cycle
from scripts import propertyquarry_stage_ooda_signals as stage
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_operator_status.v1"
PRESENTATION_STATE_SCHEMA = "propertyquarry.ooda_operator_presentation_state.v1"
PRESENTATION_RECEIPT_SCHEMA = "propertyquarry.ooda_operator_presentation_receipt.v1"
DEFAULT_CYCLE_RECEIPT = Path("_completion/propertyquarry_ooda_notification_cycle/latest.json")
DEFAULT_SIGNAL_DIR = Path("_completion/propertyquarry_ooda_signal_ingress")
DEFAULT_PRESENTATION_STATE = Path(
    "_completion/propertyquarry_ooda_notification_cycle/operator-presentation-state.json"
)
DEFAULT_MAX_AGE_SECONDS = 1800.0
MAX_RECEIPT_BYTES = 512 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GOLD_ACTIONS = {
    "live_runtime_tunnel_unavailable": (
        "inspect and stage the PropertyQuarry Cloudflare tunnel connector recovery for review; "
        "start or restart the connector only after explicit deployment/restart authorization, then "
        "rerun propertyquarry_live_mobile_surface_smoke.py"
    ),
    "live_runtime_host_admission_rejected": (
        "inspect and stage the PropertyQuarry host-admission, public-origin, and dedicated-runtime "
        "configuration for review; after an explicitly authorized deployment or restart, rerun "
        "propertyquarry_live_mobile_surface_smoke.py"
    ),
    "live_runtime_prerequisite_blocked": (
        "inspect and stage the reported live runtime or research-fixture prerequisite for review; "
        "after any required external change is explicitly authorized, rerun "
        "propertyquarry_live_mobile_surface_smoke.py"
    ),
}
_SCENE_ACTION = (
    "place the missing or refreshed provider account material in the governed operator-import lane "
    "for review; do not paste credentials into chat, change a provider plan, merge runtime "
    "configuration, or run a proof render without explicit approval"
)
_SCENE_REASONS = {
    "provider_account_material_required",
    "provider_credit_review_required",
    "provider_account_and_credit_review_required",
}
_PROTECTED_BY_LANE = {
    "gold_live_runtime": [
        "runtime_configuration_change",
        "deployment_or_restart",
    ],
    "scene_video_provider_refresh": [
        "provider_account_material_import",
        "provider_credit_or_plan_change",
        "runtime_configuration_change",
        "provider_quota_consumption",
    ],
}
_PRESENTATION_LANES = frozenset(_PROTECTED_BY_LANE) | frozenset(
    {
        "scheduler_activation",
        "scheduler_activation_settlement",
    }
)
_APPROVED_SIGNAL_APPROVAL_KEYS = frozenset(
    {"approved", "reason", "manifest_generated_at", "policy"}
)
_REVOKED_SIGNAL_APPROVAL_KEYS = frozenset(
    {
        "approved",
        "reason",
        "manifest_generated_at",
        "policy",
        "revocation_verified",
        "revocation_manifest_sha256",
        "source_lanes",
        "source_evidence_posture",
    }
)
_ACTION_KEYS = frozenset(
    {
        "lane",
        "reason",
        "source_generated_at",
        "safe_next_action",
        "consent_required",
        "automatic_execution_allowed",
        "protected_operations",
        "provider_quota_consumption_allowed",
    }
)
_SCENE_PROVIDER_KEYS = frozenset(
    {"provider", "provider_label", "visible_account_gap", "action_reasons"}
)
_PRESENTATION_STATE_KEYS = frozenset(
    {
        "schema",
        "updated_at",
        "source_cycle_receipt_sha256",
        "active_presentations",
        "delivery_state_updated",
        "provider_quota_consumed",
        "protected_operation_executed",
    }
)
_PRESENTATION_IDENTITY_KEYS = frozenset(
    {
        "action_digest",
        "first_presented_at",
        "last_presented_at",
        "source_cycle_receipt_sha256",
    }
)
_CYCLE_COMMON_KEYS = frozenset(
    {
        "schema",
        "generated_at",
        "status",
        "execution_mode",
        "notification_policy",
        "delivery_authorized",
        "delivery_attempted",
        "sent",
        "would_send",
        "notification_count",
        "message_ids",
        "delivery_mode",
        "state_path",
        "state_updated",
        "notification_state_status",
        "notification_state_sha256",
        "notification_state_admissible",
        "input_evidence",
        "lanes",
        "source_evidence_posture",
        "action_required_count",
        "novel_action_count",
        "operator_action_required",
        "interrupt_operator",
        "active_action_count_before",
        "active_action_count_projected",
        "protected_operation_executed",
        "automatic_execution_allowed",
        "provider_quota_consumption_allowed",
        "actions",
        "signal_approval",
    }
)
_CYCLE_ACTION_REQUIRED_KEYS = _CYCLE_COMMON_KEYS | frozenset(
    {"message_preview", "next_action"}
)
_CYCLE_SETTLED_KEYS = _CYCLE_COMMON_KEYS | frozenset(
    {"active_action_count_after", "next_action"}
)
_CYCLE_REVOCATION_KEYS = _CYCLE_SETTLED_KEYS | frozenset(
    {"publication_status", "revocation_suppressed_action_count"}
)
_CYCLE_INPUT_EVIDENCE_KEYS = frozenset(
    {"path", "status", "sha256", "bytes", "mode"}
)
_CYCLE_INPUT_EVIDENCE_MINIMUM_KEYS = frozenset(
    {"path", "status", "sha256"}
)
_CYCLE_LANE_KEYS = frozenset(
    {
        "lane",
        "source_status",
        "action_required",
        "clear_verified",
        "reason",
        "source_generated_at",
        "observed_source_generated_at",
    }
)
_CYCLE_LANE_ACTION_KEYS = frozenset({"action_digest", "novel_action"})


def _observed_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    observed_at = _observed_now(now).isoformat()
    return {
        "schema": SCHEMA,
        "status": "blocked",
        "action_required": False,
        "interrupt_operator": False,
        "updated_at": observed_at,
        "blocking_reason": reason,
        "next_action": "regenerate and verify the approved evaluate-only OODA cycle",
        "progress": {
            "approved_snapshot_verified": False,
            "action_required_count": 0,
            "novel_action_count": 0,
        },
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
        "actions": [],
    }


def _parse_fresh_timestamp(
    value: object,
    *,
    now: datetime,
    max_age_seconds: float,
) -> str | None:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    age_seconds = (now - parsed.astimezone(timezone.utc)).total_seconds()
    if (
        not math.isfinite(age_seconds)
        or age_seconds < -30.0
        or age_seconds > max_age_seconds
    ):
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _strict_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _cycle_receipt_envelope_admissible(receipt: Mapping[str, Any]) -> bool:
    """Require one complete producer envelope before interpreting quiet status."""

    status = str(receipt.get("status") or "")
    approval_row = receipt.get("signal_approval")
    verified_revocation = bool(
        isinstance(approval_row, Mapping)
        and approval_row.get("revocation_verified") is True
    )
    if status == "action_required":
        expected_keys = _CYCLE_ACTION_REQUIRED_KEYS
        if "send_requested" in receipt:
            expected_keys = expected_keys | frozenset({"send_requested"})
    elif status in {
        "silent",
        "deduplicated",
        "completed",
        "notification_state_invalid",
    }:
        expected_keys = (
            _CYCLE_REVOCATION_KEYS
            if status == "silent" and verified_revocation
            else _CYCLE_SETTLED_KEYS
        )
        if (
            (verified_revocation or status == "notification_state_invalid")
            and "send_requested" in receipt
        ):
            expected_keys = expected_keys | frozenset({"send_requested"})
    else:
        return False
    if set(receipt) != expected_keys:
        return False

    notification_state_status = str(
        receipt.get("notification_state_status") or ""
    )
    notification_state_sha256 = str(
        receipt.get("notification_state_sha256") or ""
    )
    notification_state_admissible = receipt.get(
        "notification_state_admissible"
    )
    if not (
        notification_state_status in {"missing", "ready", "invalid"}
        and isinstance(notification_state_admissible, bool)
        and notification_state_admissible
        is (notification_state_status != "invalid")
        and (
            _SHA256.fullmatch(notification_state_sha256)
            if notification_state_status == "ready"
            else (
                not notification_state_sha256
                or _SHA256.fullmatch(notification_state_sha256)
            )
        )
        and (
            status in {"action_required", "notification_state_invalid"}
            or notification_state_status != "invalid"
        )
        and (
            receipt.get("state_updated") is not True
            or notification_state_status == "ready"
        )
        and (
            status != "completed"
            or notification_state_status == "ready"
        )
        and (
            "send_requested" not in receipt
            or (
                receipt.get("send_requested") is True
                and (
                    (
                        status == "action_required"
                        and notification_state_status == "invalid"
                    )
                    or (
                        status == "notification_state_invalid"
                        and notification_state_status == "invalid"
                    )
                    or (
                        status == "silent"
                        and verified_revocation
                        and receipt.get("execution_mode")
                        == "revocation_fail_closed"
                    )
                )
            )
        )
    ):
        return False

    active_before = _strict_count(receipt.get("active_action_count_before"))
    active_projected = _strict_count(
        receipt.get("active_action_count_projected")
    )
    active_after = (
        _strict_count(receipt.get("active_action_count_after"))
        if status != "action_required"
        else None
    )
    if not (
        isinstance(receipt.get("state_path"), str)
        and str(receipt.get("state_path") or "").strip()
        and isinstance(receipt.get("state_updated"), bool)
        and active_before is not None
        and active_projected is not None
        and active_before <= len(_PROTECTED_BY_LANE)
        and active_projected <= len(_PROTECTED_BY_LANE)
        and (
            status == "action_required"
            or (
                active_after is not None
                and active_after <= len(_PROTECTED_BY_LANE)
            )
        )
        and isinstance(receipt.get("delivery_authorized"), bool)
        and isinstance(receipt.get("delivery_attempted"), bool)
        and isinstance(receipt.get("sent"), bool)
        and isinstance(receipt.get("would_send"), bool)
        and isinstance(receipt.get("state_updated"), bool)
        and isinstance(receipt.get("message_ids"), list)
        and isinstance(receipt.get("delivery_mode"), str)
        and isinstance(receipt.get("next_action"), str)
        and str(receipt.get("next_action") or "").strip()
    ):
        return False

    if status == "action_required":
        if not (
            receipt.get("execution_mode") == "evaluate_only"
            and receipt.get("delivery_authorized") is False
            and receipt.get("delivery_attempted") is False
            and receipt.get("sent") is False
            and receipt.get("would_send") is True
            and receipt.get("notification_count") == 0
            and receipt.get("message_ids") == []
            and receipt.get("delivery_mode") == ""
            and receipt.get("state_updated") is False
            and isinstance(receipt.get("message_preview"), str)
            and str(receipt.get("message_preview") or "").strip()
        ):
            return False
    elif status == "notification_state_invalid":
        if not (
            receipt.get("execution_mode") == "evaluate_only"
            and receipt.get("delivery_authorized") is False
            and receipt.get("delivery_attempted") is False
            and receipt.get("sent") is False
            and receipt.get("would_send") is False
            and receipt.get("notification_count") == 0
            and receipt.get("message_ids") == []
            and receipt.get("delivery_mode") == ""
            and receipt.get("state_updated") is False
            and receipt.get("notification_state_status") == "invalid"
            and receipt.get("notification_state_admissible") is False
        ):
            return False
    elif status in {"silent", "deduplicated"}:
        quiet_delivery = bool(
            receipt.get("delivery_attempted") is False
            and receipt.get("sent") is False
            and receipt.get("would_send") is False
            and receipt.get("notification_count") == 0
            and receipt.get("message_ids") == []
            and receipt.get("delivery_mode") == ""
        )
        if verified_revocation:
            if not (
                quiet_delivery
                and receipt.get("execution_mode")
                in {"evaluate_only", "revocation_fail_closed"}
                and receipt.get("delivery_authorized") is False
                and receipt.get("publication_status") == "revoked"
                and (
                    "send_requested" not in receipt
                    or receipt.get("send_requested") is True
                )
            ):
                return False
        elif not (
            quiet_delivery
            and receipt.get("execution_mode") in {"evaluate_only", "send"}
            and receipt.get("delivery_authorized")
            is (receipt.get("execution_mode") == "send")
        ):
            return False

    input_evidence = receipt.get("input_evidence")
    expected_evidence_names = set(approved.SIGNAL_FILENAMES) | {
        "approval_manifest"
    }
    if not (
        isinstance(input_evidence, Mapping)
        and set(input_evidence) == expected_evidence_names
    ):
        return False
    for name, raw_row in input_evidence.items():
        if not isinstance(raw_row, Mapping):
            return False
        row_keys = set(raw_row)
        if verified_revocation and name != "approval_manifest":
            if not (
                _CYCLE_INPUT_EVIDENCE_MINIMUM_KEYS <= row_keys
                and row_keys <= _CYCLE_INPUT_EVIDENCE_KEYS
            ):
                return False
        elif row_keys != _CYCLE_INPUT_EVIDENCE_KEYS:
            return False
        path = raw_row.get("path")
        digest = str(raw_row.get("sha256") or "")
        if not (
            isinstance(path, str)
            and path.strip()
            and isinstance(raw_row.get("status"), str)
            and str(raw_row.get("status") or "").strip()
            and (not digest or _SHA256.fullmatch(digest))
        ):
            return False
        if (name == "approval_manifest" or not verified_revocation) and not (
            raw_row.get("status") == "ready"
            and _SHA256.fullmatch(digest)
        ):
            return False
        if ("bytes" in raw_row) != ("mode" in raw_row):
            return False
        if "bytes" in raw_row:
            size = _strict_count(raw_row.get("bytes"))
            mode = raw_row.get("mode")
            if not (
                size is not None
                and 1 <= size <= stage.MAX_SOURCE_BYTES
                and not isinstance(mode, bool)
                and isinstance(mode, int)
                and mode & 0o022 == 0
            ):
                return False

    lanes = receipt.get("lanes")
    if not (
        isinstance(lanes, list)
        and len(lanes) == len(_PROTECTED_BY_LANE)
        and all(isinstance(row, Mapping) for row in lanes)
    ):
        return False
    for row in lanes:
        row_keys = set(row)
        optional_keys = frozenset(row_keys - _CYCLE_LANE_KEYS)
        if not (
            _CYCLE_LANE_KEYS <= row_keys
            and optional_keys in {frozenset(), _CYCLE_LANE_ACTION_KEYS}
            and str(row.get("lane") or "") in _PROTECTED_BY_LANE
            and isinstance(row.get("action_required"), bool)
            and isinstance(row.get("clear_verified"), bool)
            and isinstance(row.get("reason"), str)
            and str(row.get("reason") or "").strip()
        ):
            return False
        if optional_keys and not (
            _SHA256.fullmatch(str(row.get("action_digest") or ""))
            and isinstance(row.get("novel_action"), bool)
        ):
            return False
    return True


def _presentation_digest(action: Mapping[str, Any]) -> str:
    normalized = {
        key: value
        for key, value in action.items()
        if key not in {"source_generated_at", "presentation_digest"}
    }
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _presentation_digest_without_context(action: Mapping[str, Any]) -> str:
    projected = dict(action)
    projected.pop("presentation_context_digest", None)
    projected.pop("presentation_digest", None)
    return _presentation_digest(projected)


def _presentation_context_digest(context: Mapping[str, Any]) -> str:
    """Hash bounded semantic decision context, excluding receipt-level churn."""

    if not isinstance(context, Mapping) or not context:
        raise ValueError("presentation_context_not_admissible")
    try:
        encoded = json.dumps(
            dict(context),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("presentation_context_not_admissible") from exc
    if len(encoded) > 64 * 1024:
        raise ValueError("presentation_context_not_admissible")
    return hashlib.sha256(encoded).hexdigest()


def _load_presentation_state(
    path: Path,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str, str]:
    try:
        metadata = path.lstat()
    except OSError:
        return {}, "missing", ""
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        return {}, "invalid", ""
    try:
        payload, _raw, digest = load_strict_json_object_snapshot(
            path,
            field="operator_presentation_state",
            maximum_bytes=MAX_RECEIPT_BYTES,
        )
    except Exception:
        return {}, "invalid", ""
    updated_at_raw = payload.get("updated_at")
    source_cycle_receipt_sha256 = str(
        payload.get("source_cycle_receipt_sha256") or ""
    )
    try:
        updated_at = datetime.fromisoformat(
            updated_at_raw.replace("Z", "+00:00")
        ) if isinstance(updated_at_raw, str) else None
    except ValueError:
        updated_at = None
    observed_now = _observed_now(now)
    if (
        set(payload) != _PRESENTATION_STATE_KEYS
        or payload.get("schema") != PRESENTATION_STATE_SCHEMA
        or updated_at is None
        or updated_at.tzinfo is None
        or updated_at.astimezone(timezone.utc) > observed_now + timedelta(seconds=30)
        or not _SHA256.fullmatch(source_cycle_receipt_sha256)
        or payload.get("delivery_state_updated") is not False
        or payload.get("provider_quota_consumed") is not False
        or payload.get("protected_operation_executed") is not False
    ):
        return {}, "invalid", ""
    raw_actions = payload.get("active_presentations")
    if not isinstance(raw_actions, dict):
        return {}, "invalid", ""
    for lane, row in raw_actions.items():
        context_digest = (
            str(row.get("presentation_context_digest") or "")
            if isinstance(row, dict)
            else ""
        )
        expected_row_keys = set(_PRESENTATION_IDENTITY_KEYS)
        if context_digest:
            expected_row_keys.add("presentation_context_digest")
        first_presented_raw = (
            row.get("first_presented_at") if isinstance(row, dict) else None
        )
        last_presented_raw = (
            row.get("last_presented_at") if isinstance(row, dict) else None
        )
        try:
            first_presented_at = datetime.fromisoformat(
                first_presented_raw.replace("Z", "+00:00")
            ) if isinstance(first_presented_raw, str) else None
            last_presented_at = datetime.fromisoformat(
                last_presented_raw.replace("Z", "+00:00")
            ) if isinstance(last_presented_raw, str) else None
        except ValueError:
            first_presented_at = None
            last_presented_at = None
        if not (
            lane in _PRESENTATION_LANES
            and isinstance(row, dict)
            and set(row) == expected_row_keys
            and _SHA256.fullmatch(str(row.get("action_digest") or ""))
            and (
                not context_digest
                or _SHA256.fullmatch(context_digest)
            )
            and first_presented_at is not None
            and first_presented_at.tzinfo is not None
            and last_presented_at is not None
            and last_presented_at.tzinfo is not None
            and first_presented_at.astimezone(timezone.utc)
            <= last_presented_at.astimezone(timezone.utc)
            <= updated_at.astimezone(timezone.utc)
            and _SHA256.fullmatch(str(row.get("source_cycle_receipt_sha256") or ""))
        ):
            return {}, "invalid", ""
    return payload, "ready", digest


def apply_operator_presentation_state(
    status: dict[str, Any],
    *,
    state_path: Path,
    presentation_context_by_lane: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Suppress repeated presentation using action plus semantic decision context."""

    if status.get("status") == "blocked":
        return status
    context_by_lane = dict(presentation_context_by_lane or {})
    state, state_status, state_sha256 = _load_presentation_state(
        state_path,
        now=now,
    )
    if state_status == "invalid":
        blocked = _blocked(
            "operator_presentation_state_not_admissible",
            now=now,
        )
        blocked["next_action"] = (
            "repair or explicitly replace the private operator-presentation "
            "ledger before presenting another action"
        )
        blocked["source_cycle_receipt_sha256"] = str(
            status.get("source_cycle_receipt_sha256") or ""
        )
        blocked["presentation"] = {
            "state_path": str(state_path),
            "state_status": "invalid",
            "state_sha256": state_sha256,
            "pending_action_count": 0,
            "novel_action_count": 0,
            "delivery_state_independent": True,
            "delivery_state_updated": False,
        }
        return blocked
    active_presentations = dict(state.get("active_presentations") or {})
    if status.get("status") == "waiting_for_evidence":
        retained_lanes = sorted(active_presentations)
        status["presentation"] = {
            "state_path": str(state_path),
            "state_status": state_status,
            "state_sha256": state_sha256,
            "pending_action_count": 0,
            "novel_action_count": 0,
            "retained_context_lanes": retained_lanes,
            "context_hydration_lanes": [],
            "freshness_gap_retained": True,
            "delivery_state_independent": True,
            "delivery_state_updated": False,
        }
        status["progress"]["presentation_novel_action_count"] = 0
        status["actions"] = []
        status["interrupt_operator"] = False
        return status
    current_actions: list[dict[str, Any]] = []
    retained_context_lanes: list[str] = []
    context_hydration_lanes: list[str] = []
    for value in list(status.get("actions") or []):
        if not isinstance(value, dict):
            continue
        action = dict(value)
        lane = str(action.get("lane") or "")
        context = context_by_lane.get(lane)
        if context is not None:
            action["presentation_context_digest"] = _presentation_context_digest(
                context
            )
            prior_identity = dict(active_presentations.get(lane) or {})
            if (
                not str(prior_identity.get("presentation_context_digest") or "")
                and str(prior_identity.get("action_digest") or "")
                == _presentation_digest_without_context(action)
            ):
                context_hydration_lanes.append(lane)
        else:
            prior_context_digest = str(
                dict(active_presentations.get(lane) or {}).get(
                    "presentation_context_digest"
                )
                or ""
            )
            if _SHA256.fullmatch(prior_context_digest):
                action["presentation_context_digest"] = prior_context_digest
                retained_context_lanes.append(lane)
        action["presentation_digest"] = _presentation_digest(action)
        current_actions.append(action)
    novel_actions = [
        action
        for action in current_actions
        if (
            str(action.get("lane") or "") not in context_hydration_lanes
            and str(
                dict(active_presentations.get(str(action.get("lane") or "")) or {}).get(
                    "action_digest"
                )
                or ""
            )
            != action["presentation_digest"]
        )
    ]
    status["presentation"] = {
        "state_path": str(state_path),
        "state_status": state_status,
        "state_sha256": state_sha256,
        "pending_action_count": len(current_actions),
        "novel_action_count": len(novel_actions),
        "retained_context_lanes": sorted(set(retained_context_lanes)),
        "context_hydration_lanes": sorted(set(context_hydration_lanes)),
        "delivery_state_independent": True,
        "delivery_state_updated": False,
    }
    status["progress"]["presentation_novel_action_count"] = len(novel_actions)
    if status.get("action_required") is not True:
        return status
    status["pending_actions"] = current_actions
    status["actions"] = novel_actions
    status["interrupt_operator"] = bool(novel_actions)
    if not novel_actions:
        status["status"] = "pending_action"
        status["next_action"] = (
            str(current_actions[0].get("safe_next_action") or "")
            if len(current_actions) == 1
            and current_actions[0].get("manual_apply_authorized") is True
            else "await an explicit operator decision or a verified action change"
        )
    return status


def _apply_presentation_state(
    status: dict[str, Any],
    *,
    state_path: Path,
) -> dict[str, Any]:
    """Backward-compatible action-only presentation projection."""

    return apply_operator_presentation_state(status, state_path=state_path)


def _presentation_action_identity(
    value: object,
) -> tuple[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        raise ValueError("presentation_action_not_admissible")
    lane = str(value.get("lane") or "").strip()
    action_digest = str(value.get("presentation_digest") or "")
    context_digest = str(value.get("presentation_context_digest") or "")
    if (
        not lane
        or not _SHA256.fullmatch(action_digest)
        or _presentation_digest(value) != action_digest
        or (context_digest and not _SHA256.fullmatch(context_digest))
    ):
        raise ValueError("presentation_action_not_admissible")
    identity = {"action_digest": action_digest}
    if context_digest:
        identity["presentation_context_digest"] = context_digest
    return lane, identity


def record_operator_presentation(
    status: Mapping[str, Any],
    *,
    state_path: Path = DEFAULT_PRESENTATION_STATE,
    expected_source_cycle_receipt_sha256: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a rendered operator projection without touching delivery state."""

    recorded_at = _observed_now(now).isoformat()
    source_digest = str(status.get("source_cycle_receipt_sha256") or "")
    expected_source_digest = str(
        expected_source_cycle_receipt_sha256 or ""
    ).strip()
    safe_status = str(status.get("status") or "") in {
        "ready",
        "action_required",
        "pending_action",
        "waiting_for_evidence",
    }
    if not (
        safe_status
        and _SHA256.fullmatch(source_digest)
        and status.get("automatic_execution_allowed") is False
        and status.get("provider_quota_consumption_allowed") is False
        and status.get("protected_operation_executed") is False
    ):
        return {
            "schema": PRESENTATION_RECEIPT_SCHEMA,
            "status": "blocked",
            "recorded_at": recorded_at,
            "blocking_reason": "operator_projection_not_admissible",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    if expected_source_digest and (
        not _SHA256.fullmatch(expected_source_digest)
        or expected_source_digest != source_digest
    ):
        return {
            "schema": PRESENTATION_RECEIPT_SCHEMA,
            "status": "blocked",
            "recorded_at": recorded_at,
            "blocking_reason": "presentation_source_binding_mismatch",
            "expected_source_cycle_receipt_sha256": expected_source_digest,
            "actual_source_cycle_receipt_sha256": source_digest,
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    prior_state, prior_status, prior_sha256 = _load_presentation_state(
        Path(state_path),
        now=now,
    )
    if prior_status == "invalid":
        return {
            "schema": PRESENTATION_RECEIPT_SCHEMA,
            "status": "blocked",
            "recorded_at": recorded_at,
            "blocking_reason": "presentation_state_not_admissible",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    if status.get("status") == "waiting_for_evidence":
        prior_presentations = dict(prior_state.get("active_presentations") or {})
        presentation = status.get("presentation")
        if not (
            status.get("action_required") is False
            and status.get("interrupt_operator") is False
            and status.get("actions") == []
            and isinstance(presentation, Mapping)
            and presentation.get("state_path") == str(state_path)
            and presentation.get("state_status") == prior_status
            and presentation.get("state_sha256") == prior_sha256
            and presentation.get("pending_action_count") == 0
            and presentation.get("novel_action_count") == 0
            and presentation.get("retained_context_lanes")
            == sorted(prior_presentations)
            and presentation.get("context_hydration_lanes") == []
            and presentation.get("freshness_gap_retained") is True
            and presentation.get("delivery_state_independent") is True
            and presentation.get("delivery_state_updated") is False
        ):
            return {
                "schema": PRESENTATION_RECEIPT_SCHEMA,
                "status": "blocked",
                "recorded_at": recorded_at,
                "blocking_reason": "presentation_freshness_gap_not_admissible",
                "delivery_state_updated": False,
                "provider_quota_consumed": False,
                "protected_operation_executed": False,
            }
        return {
            "schema": PRESENTATION_RECEIPT_SCHEMA,
            "status": "unchanged",
            "recorded_at": recorded_at,
            "state_path": str(state_path),
            "state_sha256": prior_sha256,
            "source_cycle_receipt_sha256": source_digest,
            "state_source_cycle_receipt_sha256": str(
                prior_state.get("source_cycle_receipt_sha256") or ""
            ),
            "active_presentation_count": len(prior_presentations),
            "freshness_gap_retained": True,
            "presentation_recorded": False,
            "state_updated": False,
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    raw_actions = (
        status.get("pending_actions")
        if status.get("action_required") is True
        else []
    )
    displayed_actions = status.get("actions")
    if not isinstance(raw_actions, list) or not isinstance(displayed_actions, list):
        return {
            "schema": PRESENTATION_RECEIPT_SCHEMA,
            "status": "blocked",
            "recorded_at": recorded_at,
            "blocking_reason": "presentation_action_not_admissible",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    prior_presentations = dict(prior_state.get("active_presentations") or {})
    prior_identities = {
        lane: {
            key: str(row.get(key) or "")
            for key in ("action_digest", "presentation_context_digest")
            if str(row.get(key) or "")
        }
        for lane, row in prior_presentations.items()
    }
    current_identities: dict[str, dict[str, str]] = {}
    current_actions: dict[str, dict[str, Any]] = {}
    displayed_identities: dict[str, dict[str, str]] = {}
    try:
        for value in raw_actions:
            lane, identity = _presentation_action_identity(value)
            if lane in current_identities:
                raise ValueError("presentation_action_not_admissible")
            current_identities[lane] = identity
            current_actions[lane] = dict(value)
        for value in displayed_actions:
            lane, identity = _presentation_action_identity(value)
            if lane in displayed_identities:
                raise ValueError("presentation_action_not_admissible")
            displayed_identities[lane] = identity
    except ValueError:
        return {
            "schema": PRESENTATION_RECEIPT_SCHEMA,
            "status": "blocked",
            "recorded_at": recorded_at,
            "blocking_reason": "presentation_action_not_admissible",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    presentation_row = dict(status.get("presentation") or {})
    raw_context_hydration_lanes = presentation_row.get(
        "context_hydration_lanes",
        [],
    )
    if not isinstance(raw_context_hydration_lanes, list) or any(
        not isinstance(lane, str) or not lane.strip()
        for lane in raw_context_hydration_lanes
    ):
        return {
            "schema": PRESENTATION_RECEIPT_SCHEMA,
            "status": "blocked",
            "recorded_at": recorded_at,
            "blocking_reason": "presentation_context_hydration_not_admissible",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    context_hydration_lanes = set(raw_context_hydration_lanes)
    derived_context_hydration_lanes = {
        lane
        for lane, identity in current_identities.items()
        if lane in prior_identities
        and not prior_identities[lane].get("presentation_context_digest")
        and bool(identity.get("presentation_context_digest"))
        and prior_identities[lane].get("action_digest")
        == _presentation_digest_without_context(current_actions[lane])
    }
    if (
        len(context_hydration_lanes) != len(raw_context_hydration_lanes)
        or context_hydration_lanes != derived_context_hydration_lanes
    ):
        return {
            "schema": PRESENTATION_RECEIPT_SCHEMA,
            "status": "blocked",
            "recorded_at": recorded_at,
            "blocking_reason": "presentation_context_hydration_not_admissible",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    changed_current_lanes = {
        lane
        for lane, identity in current_identities.items()
        if prior_identities.get(lane) != identity
    }
    user_visible_changed_lanes = changed_current_lanes - context_hydration_lanes
    expected_displayed_identities = {
        lane: current_identities[lane] for lane in user_visible_changed_lanes
    }
    if (
        displayed_identities != expected_displayed_identities
        or (status.get("interrupt_operator") is True)
        != bool(user_visible_changed_lanes)
    ):
        return {
            "schema": PRESENTATION_RECEIPT_SCHEMA,
            "status": "blocked",
            "recorded_at": recorded_at,
            "blocking_reason": "presentation_render_binding_mismatch",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    if prior_status == "ready" and prior_identities == current_identities:
        return {
            "schema": PRESENTATION_RECEIPT_SCHEMA,
            "status": "unchanged",
            "recorded_at": recorded_at,
            "state_path": str(state_path),
            "state_sha256": prior_sha256,
            "source_cycle_receipt_sha256": source_digest,
            "state_source_cycle_receipt_sha256": str(
                prior_state.get("source_cycle_receipt_sha256") or ""
            ),
            "active_presentation_count": len(current_identities),
            "presentation_recorded": False,
            "state_updated": False,
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    active_presentations: dict[str, dict[str, str]] = {}
    for lane, identity in current_identities.items():
        prior = dict(prior_presentations.get(lane) or {})
        if prior_identities.get(lane) == identity or lane in context_hydration_lanes:
            active_presentations[lane] = {
                "action_digest": identity["action_digest"],
                "first_presented_at": str(prior.get("first_presented_at") or ""),
                "last_presented_at": str(prior.get("last_presented_at") or ""),
                "source_cycle_receipt_sha256": str(
                    prior.get("source_cycle_receipt_sha256") or ""
                ),
            }
        else:
            active_presentations[lane] = {
                "action_digest": identity["action_digest"],
                "first_presented_at": recorded_at,
                "last_presented_at": recorded_at,
                "source_cycle_receipt_sha256": source_digest,
            }
        if identity.get("presentation_context_digest"):
            active_presentations[lane]["presentation_context_digest"] = identity[
                "presentation_context_digest"
            ]
    state_payload = {
        "schema": PRESENTATION_STATE_SCHEMA,
        "updated_at": recorded_at,
        "source_cycle_receipt_sha256": source_digest,
        "active_presentations": active_presentations,
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "protected_operation_executed": False,
    }
    from scripts import propertyquarry_notify_gold_status as gold_notify

    gold_notify._write_notification_state(Path(state_path), state_payload)
    persisted, persisted_status, persisted_sha256 = _load_presentation_state(
        Path(state_path),
        now=now,
    )
    if persisted_status != "ready" or persisted != state_payload:
        return {
            "schema": PRESENTATION_RECEIPT_SCHEMA,
            "status": "blocked",
            "recorded_at": recorded_at,
            "blocking_reason": "presentation_state_write_not_verified",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }
    return {
        "schema": PRESENTATION_RECEIPT_SCHEMA,
        "status": "recorded",
        "recorded_at": recorded_at,
        "state_path": str(state_path),
        "state_sha256": persisted_sha256,
        "source_cycle_receipt_sha256": source_digest,
        "active_presentation_count": len(active_presentations),
        "presentation_recorded": bool(user_visible_changed_lanes),
        "context_hydrated_count": len(context_hydration_lanes),
        "cleared_presentation_count": len(
            set(prior_identities) - set(current_identities)
        ),
        "state_updated": True,
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "protected_operation_executed": False,
    }


def project_operator_presentation_with_context_hydration(
    status: Mapping[str, Any],
    *,
    state_path: Path,
    presentation_context_by_lane: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project semantic novelty and silently hydrate only a legacy baseline."""

    source = copy.deepcopy(dict(status))
    projected = apply_operator_presentation_state(
        copy.deepcopy(source),
        state_path=state_path,
        presentation_context_by_lane=presentation_context_by_lane,
        now=now,
    )
    presentation = dict(projected.get("presentation") or {})
    hydration_lanes = list(
        presentation.get("context_hydration_lanes") or []
    )
    if not hydration_lanes or list(projected.get("actions") or []):
        presentation["context_hydration_state_updated"] = False
        presentation["context_hydrated_lanes"] = []
        projected["presentation"] = presentation
        return projected

    receipt = record_operator_presentation(
        projected,
        state_path=state_path,
        expected_source_cycle_receipt_sha256=str(
            projected.get("source_cycle_receipt_sha256") or ""
        ),
        now=now,
    )
    if not (
        receipt.get("status") == "recorded"
        and receipt.get("presentation_recorded") is False
        and receipt.get("context_hydrated_count") == len(hydration_lanes)
        and receipt.get("state_updated") is True
        and receipt.get("delivery_state_updated") is False
        and receipt.get("provider_quota_consumed") is False
        and receipt.get("protected_operation_executed") is False
    ):
        raise ValueError("operator_presentation_context_hydration_failed")

    refreshed = apply_operator_presentation_state(
        copy.deepcopy(source),
        state_path=state_path,
        presentation_context_by_lane=presentation_context_by_lane,
        now=now,
    )
    refreshed_presentation = dict(refreshed.get("presentation") or {})
    if (
        refreshed_presentation.get("context_hydration_lanes") != []
        or refreshed.get("interrupt_operator") is not False
    ):
        raise ValueError("operator_presentation_context_hydration_not_current")
    refreshed_presentation["context_hydration_state_updated"] = True
    refreshed_presentation["context_hydrated_lanes"] = sorted(
        set(hydration_lanes)
    )
    refreshed["presentation"] = refreshed_presentation
    return refreshed


def _project_action(
    value: object,
    *,
    now: datetime,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    lane = str(value.get("lane") or "").strip()
    reason = str(value.get("reason") or "").strip()
    if lane == "gold_live_runtime":
        canonical_next_action = _GOLD_ACTIONS.get(reason)
    elif lane == "scene_video_provider_refresh" and reason in _SCENE_REASONS:
        canonical_next_action = _SCENE_ACTION
    else:
        return None
    expected_keys = set(_ACTION_KEYS)
    if lane == "scene_video_provider_refresh":
        expected_keys.add("providers")
    if (
        set(value) != expected_keys
        or canonical_next_action is None
        or value.get("safe_next_action") != canonical_next_action
        or value.get("consent_required") is not True
        or value.get("automatic_execution_allowed") is not False
        or value.get("provider_quota_consumption_allowed") is not False
        or value.get("protected_operations") != _PROTECTED_BY_LANE[lane]
    ):
        return None
    source_generated_at = _parse_fresh_timestamp(
        value.get("source_generated_at"),
        now=now,
        max_age_seconds=approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    if source_generated_at is None:
        return None
    projected: dict[str, Any] = {
        "lane": lane,
        "reason": reason,
        "source_generated_at": source_generated_at,
        "safe_next_action": canonical_next_action,
        "consent_required": True,
        "automatic_execution_allowed": False,
        "protected_operations": list(_PROTECTED_BY_LANE[lane]),
        "provider_quota_consumption_allowed": False,
    }
    if lane == "scene_video_provider_refresh":
        providers: list[dict[str, Any]] = []
        provider_names: set[str] = set()
        reason_codes: set[str] = set()
        for row in list(value.get("providers") or []):
            if not isinstance(row, dict) or set(row) != _SCENE_PROVIDER_KEYS:
                return None
            provider = str(row.get("provider") or "").strip().lower()
            visible_gap = _strict_count(row.get("visible_account_gap"))
            action_reasons = [
                str(item)
                for item in list(row.get("action_reasons") or [])
                if str(item).strip()
            ]
            if (
                provider not in {"magicfit", "omagic"}
                or provider in provider_names
                or visible_gap is None
                or not action_reasons
                or len(action_reasons) != len(set(action_reasons))
                or any(
                    item not in {
                        "provider_account_material_required",
                        "provider_credit_review_required",
                    }
                    for item in action_reasons
                )
            ):
                return None
            provider_names.add(provider)
            reason_codes.update(action_reasons)
            providers.append(
                {
                    "provider": provider,
                    "provider_label": "MagicFit" if provider == "magicfit" else "OMagic/Magic",
                    "visible_account_gap": visible_gap,
                    "action_reasons": action_reasons,
                }
            )
        expected_reason = (
            "provider_account_material_required"
            if reason_codes == {"provider_account_material_required"}
            else "provider_credit_review_required"
            if reason_codes == {"provider_credit_review_required"}
            else "provider_account_and_credit_review_required"
        )
        if not providers or expected_reason != reason:
            return None
        projected["providers"] = providers
    return projected


def _project_actions_bound_to_source_lanes(
    raw_actions: object,
    *,
    source_lanes: object,
    now: datetime,
) -> list[dict[str, Any]] | None:
    """Validate each presented/delivered action against its exact source lane."""

    if not (
        isinstance(raw_actions, list)
        and isinstance(source_lanes, list)
        and all(isinstance(row, dict) for row in source_lanes)
    ):
        return None
    lanes_by_name = {
        str(row.get("lane") or "").strip(): row for row in source_lanes
    }
    projected_actions: list[dict[str, Any]] = []
    observed_action_lanes: set[str] = set()
    for raw_action in raw_actions:
        projected = _project_action(raw_action, now=now)
        if projected is None:
            return None
        lane = str(projected.get("lane") or "")
        source_lane = lanes_by_name.get(lane)
        if (
            lane in observed_action_lanes
            or not isinstance(source_lane, Mapping)
            or source_lane.get("source_status") != "ready"
            or source_lane.get("action_required") is not True
            or source_lane.get("clear_verified") is not False
            or source_lane.get("reason") != projected.get("reason")
            or approved.normalized_source_timestamp(
                source_lane.get("source_generated_at")
            )
            != approved.normalized_source_timestamp(
                projected.get("source_generated_at")
            )
        ):
            return None
        observed_action_lanes.add(lane)
        projected_actions.append(projected)
    return projected_actions


def _completed_delivery_receipt_admissible(
    receipt: Mapping[str, Any],
    *,
    action_count: int,
    novel_count: int,
    completed_actions: list[dict[str, Any]] | None,
) -> bool:
    normalized_delivery = cycle.normalized_delivery_receipt(
        {
            "delivery_mode": receipt.get("delivery_mode"),
            "message_ids": receipt.get("message_ids"),
        }
    )
    return bool(
        completed_actions is not None
        and action_count >= novel_count >= 1
        and novel_count == len(completed_actions)
        and receipt.get("execution_mode") == "send"
        and receipt.get("delivery_authorized") is True
        and receipt.get("delivery_attempted") is True
        and receipt.get("sent") is True
        and receipt.get("would_send") is False
        and receipt.get("operator_action_required") is True
        and receipt.get("interrupt_operator") is True
        and receipt.get("notification_count") == 1
        and normalized_delivery is not None
        and receipt.get("delivery_mode") == normalized_delivery[0]
        and receipt.get("message_ids") == normalized_delivery[1]
        and receipt.get("state_updated") is True
        and receipt.get("active_action_count_after") == action_count
        and receipt.get("next_action")
        == "await fresh approved signals; protected operations remain consent-gated"
        and "delivery_error_code" not in receipt
        and "state_error_code" not in receipt
    )


def _project_source_evidence_posture(
    receipt: Mapping[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
    action_count: int | None,
) -> dict[str, Any] | None:
    try:
        return approved.verify_source_evidence_posture(
            lanes=receipt.get("lanes"),
            posture=receipt.get("source_evidence_posture"),
            now=now,
            max_age_seconds=max_age_seconds,
            expected_action_count=action_count,
        )
    except ValueError:
        return None


def project_operator_status(
    receipt: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _observed_now(now)
    try:
        age_limit = float(max_age_seconds)
    except (TypeError, ValueError, OverflowError):
        return _blocked("operator_status_age_bound_invalid", now=observed_now)
    if not math.isfinite(age_limit) or not 60.0 <= age_limit <= 86400.0:
        return _blocked("operator_status_age_bound_invalid", now=observed_now)
    cycle_generated_at = _parse_fresh_timestamp(
        receipt.get("generated_at"),
        now=observed_now,
        max_age_seconds=age_limit,
    )
    approval_row = receipt.get("signal_approval")
    manifest_generated_at = (
        _parse_fresh_timestamp(
            approval_row.get("manifest_generated_at"),
            now=observed_now,
            max_age_seconds=age_limit,
        )
        if isinstance(approval_row, dict)
        else None
    )
    if (
        receipt.get("schema") != cycle.SCHEMA
        or not _cycle_receipt_envelope_admissible(receipt)
    ):
        return _blocked("cycle_receipt_contract_not_admissible", now=observed_now)
    notification_state_status = str(
        receipt.get("notification_state_status") or ""
    )
    notification_state_sha256 = str(
        receipt.get("notification_state_sha256") or ""
    )
    notification_state_admissible = receipt.get(
        "notification_state_admissible"
    )
    if cycle_generated_at is None or manifest_generated_at is None:
        return _blocked("cycle_or_manifest_receipt_not_fresh", now=observed_now)
    approved_snapshot = bool(
        isinstance(approval_row, dict)
        and set(approval_row) == _APPROVED_SIGNAL_APPROVAL_KEYS
        and approval_row.get("approved") is True
        and approval_row.get("policy") == approved.POLICY
        and approval_row.get("reason") == "approved_projection_manifest_verified"
    )
    verified_revocation = bool(
        isinstance(approval_row, dict)
        and set(approval_row) == _REVOKED_SIGNAL_APPROVAL_KEYS
        and approval_row.get("approved") is False
        and approval_row.get("revocation_verified") is True
        and approval_row.get("policy") == approved.POLICY
        and approval_row.get("reason") == "approved_projection_manifest_revoked"
        and _SHA256.fullmatch(
            str(approval_row.get("revocation_manifest_sha256") or "")
        )
        and receipt.get("publication_status") == "revoked"
    )
    if not (
        (approved_snapshot or verified_revocation)
        and receipt.get("notification_policy") == "action_required_only"
        and receipt.get("automatic_execution_allowed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("protected_operation_executed") is False
    ):
        return _blocked("cycle_approval_or_safety_contract_not_admissible", now=observed_now)

    action_count = _strict_count(receipt.get("action_required_count"))
    novel_count = _strict_count(receipt.get("novel_action_count"))
    if action_count is None or novel_count is None or novel_count > action_count:
        return _blocked("cycle_action_counts_not_admissible", now=observed_now)
    source_evidence = _project_source_evidence_posture(
        receipt,
        now=observed_now,
        max_age_seconds=approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
        action_count=None if verified_revocation else action_count,
    )
    if source_evidence is None:
        return _blocked("cycle_source_evidence_not_admissible", now=observed_now)
    if verified_revocation:
        suppressed_action_count = _strict_count(
            receipt.get("revocation_suppressed_action_count")
        )
        source_lanes = list(receipt.get("lanes") or [])
        actual_source_action_count = sum(
            1
            for row in source_lanes
            if isinstance(row, dict) and row.get("action_required") is True
        )
        if not (
            suppressed_action_count == actual_source_action_count
            and approval_row.get("source_lanes") == source_lanes
            and approval_row.get("source_evidence_posture")
            == receipt.get("source_evidence_posture")
        ):
            return _blocked("cycle_revocation_contract_not_admissible", now=observed_now)
    raw_actions = receipt.get("actions")
    if not isinstance(raw_actions, list):
        return _blocked("cycle_action_contract_not_admissible", now=observed_now)
    source_progress = dict(source_evidence.get("progress") or {})
    progress = {
        "approved_snapshot_verified": approved_snapshot,
        "action_required_count": action_count,
        "novel_action_count": novel_count,
        "source_current_lane_count": source_progress.get("current_lane_count", 0),
        "source_stale_lane_count": source_progress.get("stale_lane_count", 0),
        "source_unavailable_lane_count": source_progress.get(
            "unavailable_lane_count", 0
        ),
    }
    if verified_revocation:
        progress["revocation_verified"] = True
        progress["revocation_suppressed_action_count"] = int(
            receipt.get("revocation_suppressed_action_count") or 0
        )
    if receipt.get("status") == "notification_state_invalid":
        expected_next_action = (
            "repair or explicitly replace the private notification incident ledger before "
            "another send-enabled cycle"
        )
        if not (
            action_count == 0
            and novel_count == 0
            and receipt.get("operator_action_required") is False
            and receipt.get("interrupt_operator") is False
            and receipt.get("active_action_count_before") == 0
            and receipt.get("active_action_count_projected") == 0
            and receipt.get("active_action_count_after") == 0
            and receipt.get("actions") == []
            and receipt.get("next_action") == expected_next_action
        ):
            return _blocked(
                "notification_state_incident_contract_not_admissible",
                now=observed_now,
            )
        result = _blocked("notification_state_not_admissible", now=observed_now)
        result["updated_at"] = cycle_generated_at
        result["next_action"] = expected_next_action
        result["notification_state"] = {
            "status": notification_state_status,
            "sha256": notification_state_sha256,
            "admissible": False,
            "state_updated": False,
        }
        return result
    if receipt.get("status") != "action_required":
        if receipt.get("status") not in {"silent", "deduplicated", "completed"}:
            return _blocked("cycle_status_requires_operator_review", now=observed_now)
        completed_actions = (
            _project_actions_bound_to_source_lanes(
                raw_actions,
                source_lanes=receipt.get("lanes"),
                now=observed_now,
            )
            if receipt.get("status") == "completed"
            else []
        )
        expected_non_action_next = (
            str(source_evidence.get("next_action") or "")
            if verified_revocation
            or (
                receipt.get("status") == "silent"
                and source_evidence.get("status") == "waiting_for_fresh_sources"
            )
            else "await a verified action change or fresh clear evidence"
            if receipt.get("status") == "deduplicated"
            else "await fresh approved signals"
        )
        status_contract_admissible = (
            receipt.get("status") == "silent"
            and action_count == 0
            and novel_count == 0
            and receipt.get("operator_action_required") is False
            and receipt.get("interrupt_operator") is False
            and receipt.get("would_send") is False
            and not list(receipt.get("actions") or [])
            and receipt.get("next_action") == expected_non_action_next
        ) or (
            receipt.get("status") == "deduplicated"
            and action_count >= 1
            and novel_count == 0
            and receipt.get("operator_action_required") is True
            and receipt.get("interrupt_operator") is False
            and receipt.get("would_send") is False
            and not list(receipt.get("actions") or [])
            and receipt.get("next_action") == expected_non_action_next
        ) or (
            receipt.get("status") == "completed"
            and completed_actions is not None
            and len(raw_actions) == len(completed_actions)
            and _completed_delivery_receipt_admissible(
                receipt,
                action_count=action_count,
                novel_count=novel_count,
                completed_actions=completed_actions,
            )
        )
        if not status_contract_admissible:
            return _blocked("cycle_non_action_contract_not_admissible", now=observed_now)
        if verified_revocation and not (
            receipt.get("status") == "silent"
            and receipt.get("execution_mode")
            in {"evaluate_only", "revocation_fail_closed"}
            and receipt.get("delivery_authorized") is False
            and receipt.get("delivery_attempted") is False
            and receipt.get("sent") is False
        ):
            return _blocked("cycle_revocation_safety_contract_not_admissible", now=observed_now)
        waiting_for_evidence = (
            receipt.get("status") == "silent"
            and source_evidence.get("status") == "waiting_for_fresh_sources"
        )
        if receipt.get("status") == "completed":
            progress["delivery_receipt_verified"] = True
            progress["delivered_action_count"] = novel_count
        result = {
            "schema": SCHEMA,
            "status": "waiting_for_evidence" if waiting_for_evidence else "ready",
            "action_required": False,
            "interrupt_operator": False,
            "updated_at": cycle_generated_at,
            "blocking_reason": (
                "approved_source_snapshot_revoked"
                if verified_revocation
                else "approved_source_evidence_not_current"
                if waiting_for_evidence
                else ""
            ),
            "next_action": (
                str(source_evidence.get("next_action") or "")
                if waiting_for_evidence
                else "await fresh approved signals"
            ),
            "progress": progress,
            "automatic_execution_allowed": False,
            "provider_quota_consumption_allowed": False,
            "protected_operation_executed": False,
            "signal_approval": dict(approval_row),
            "source_evidence": source_evidence,
            "actions": [],
        }
        result["notification_state"] = {
            "status": notification_state_status,
            "sha256": notification_state_sha256,
            "admissible": notification_state_admissible is True,
            "state_updated": receipt.get("state_updated") is True,
        }
        return result
    if verified_revocation:
        return _blocked("revoked_cycle_requested_operator_action", now=observed_now)
    actions = _project_actions_bound_to_source_lanes(
        raw_actions,
        source_lanes=receipt.get("lanes"),
        now=observed_now,
    )
    if not (
        actions is not None
        and receipt.get("execution_mode") == "evaluate_only"
        and receipt.get("delivery_authorized") is False
        and receipt.get("delivery_attempted") is False
        and receipt.get("sent") is False
        and receipt.get("would_send") is True
        and receipt.get("operator_action_required") is True
        and receipt.get("interrupt_operator") is True
        and action_count >= novel_count >= 1
        and novel_count == len(raw_actions) == len(actions)
        and receipt.get("message_preview")
        == cycle._build_consolidated_message(
            raw_actions,
            generated_at=str(receipt.get("generated_at") or ""),
        )
        and receipt.get("next_action")
        == (
            "repair or explicitly replace the private notification incident ledger, then "
            "rerun with --send only when factual operator delivery remains authorized"
            if receipt.get("notification_state_status") == "invalid"
            else "rerun with --send only when factual operator delivery is authorized"
        )
    ):
        return _blocked("cycle_action_contract_not_admissible", now=observed_now)
    result = {
        "schema": SCHEMA,
        "status": "action_required",
        "action_required": True,
        "interrupt_operator": True,
        "updated_at": cycle_generated_at,
        "blocking_reason": ",".join(action["reason"] for action in actions),
        "next_action": (
            actions[0]["safe_next_action"]
            if len(actions) == 1
            else "review each listed action; keep delivery and protected operations separately authorized"
        ),
        "progress": progress,
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
        "signal_approval": dict(approval_row),
        "source_evidence": source_evidence,
        "actions": actions,
    }
    result["notification_state"] = {
        "status": str(receipt.get("notification_state_status") or ""),
        "sha256": str(receipt.get("notification_state_sha256") or ""),
        "admissible": receipt.get("notification_state_admissible") is True,
        "state_updated": receipt.get("state_updated") is True,
    }
    if receipt.get("notification_state_status") == "invalid":
        result["blocking_reason"] = (
            "notification_state_not_admissible," + result["blocking_reason"]
        )
        result["next_action"] = str(receipt.get("next_action") or "")
    return result


def _strict_private_receipt(path: Path) -> tuple[dict[str, Any], str]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("cycle_receipt_file_not_admissible")
    payload, _raw, digest = load_strict_json_object_snapshot(
        path,
        field="ooda_cycle_receipt",
        maximum_bytes=MAX_RECEIPT_BYTES,
    )
    return payload, digest


def _current_input_evidence(
    signal_dir: Path,
    *,
    manifest_only: bool = False,
) -> dict[str, dict[str, Any]]:
    files = (
        {"approval_manifest": "manifest.json"}
        if manifest_only
        else {
            **approved.SIGNAL_FILENAMES,
            "approval_manifest": "manifest.json",
        }
    )
    evidence: dict[str, dict[str, Any]] = {}
    for name, filename in files.items():
        path = signal_dir / filename
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ValueError(f"{name}_not_admissible")
        _payload, raw, digest = load_strict_json_object_snapshot(
            path,
            field=name,
            maximum_bytes=stage.MAX_SOURCE_BYTES,
        )
        evidence[name] = {
            "sha256": digest,
            "bytes": len(raw),
        }
    return evidence


def load_operator_status(
    *,
    cycle_receipt_path: Path = DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = DEFAULT_SIGNAL_DIR,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    presentation_state_path: Path | None = None,
) -> dict[str, Any]:
    """Load a fresh cycle and prove it still binds the current approved snapshot."""

    observed_now = _observed_now(now)
    try:
        receipt, receipt_digest = _strict_private_receipt(Path(cycle_receipt_path))
    except Exception:
        return _blocked("cycle_receipt_file_not_admissible", now=observed_now)
    projected = project_operator_status(
        receipt,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    if projected["status"] == "blocked":
        return projected
    projected_approval = dict(projected.get("signal_approval") or {})
    verified_revocation = projected_approval.get("revocation_verified") is True
    try:
        approval_status = stage.inspect_approved_signal_dir(
            Path(signal_dir),
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        current_evidence = _current_input_evidence(
            Path(signal_dir),
            manifest_only=verified_revocation,
        )
    except Exception:
        return _blocked("approved_signal_snapshot_not_admissible", now=observed_now)
    if verified_revocation:
        if not (
            approval_status.get("approved") is False
            and approval_status.get("revocation_verified") is True
            and approval_status.get("reason")
            == "approved_projection_manifest_revoked"
            and approval_status.get("policy") == approved.POLICY
            and approval_status.get("revocation_manifest_sha256")
            == projected_approval.get("revocation_manifest_sha256")
            and approval_status.get("source_lanes")
            == projected_approval.get("source_lanes")
            and approval_status.get("source_evidence_posture")
            == projected_approval.get("source_evidence_posture")
        ):
            return _blocked("revoked_signal_snapshot_not_admissible", now=observed_now)
    elif approval_status.get("approved") is not True:
        return _blocked("approved_signal_snapshot_not_admissible", now=observed_now)
    if dict(approval_status) != projected_approval:
        return _blocked("cycle_snapshot_approval_binding_mismatch", now=observed_now)
    cycle_evidence = receipt.get("input_evidence")
    if not isinstance(cycle_evidence, dict):
        return _blocked("cycle_snapshot_binding_not_admissible", now=observed_now)
    for name, current in current_evidence.items():
        prior = cycle_evidence.get(name)
        if not (
            isinstance(prior, dict)
            and prior.get("status") == "ready"
            and prior.get("sha256") == current["sha256"]
            and prior.get("bytes") == current["bytes"]
            and _SHA256.fullmatch(str(prior.get("sha256") or ""))
        ):
            return _blocked("cycle_snapshot_binding_mismatch", now=observed_now)
    if (
        approval_status.get("manifest_generated_at")
        != projected["signal_approval"].get("manifest_generated_at")
    ):
        return _blocked("cycle_snapshot_binding_mismatch", now=observed_now)
    projected["progress"]["current_snapshot_hashes_verified"] = True
    if verified_revocation:
        projected["progress"]["current_revocation_manifest_verified"] = True
    projected["source_cycle_receipt_sha256"] = receipt_digest
    if presentation_state_path is not None:
        projected = apply_operator_presentation_state(
            projected,
            state_path=Path(presentation_state_path),
            now=observed_now,
        )
    return projected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project a fresh operator-safe status from the approved OODA cycle."
    )
    parser.add_argument("--receipt", type=Path, default=DEFAULT_CYCLE_RECEIPT)
    parser.add_argument("--signal-dir", type=Path, default=DEFAULT_SIGNAL_DIR)
    parser.add_argument(
        "--presentation-state",
        type=Path,
        default=DEFAULT_PRESENTATION_STATE,
        help="Private operator-presentation ledger, independent of delivery state.",
    )
    parser.add_argument(
        "--record-presentation",
        action="store_true",
        help="Record the successfully rendered projection without sending or executing it.",
    )
    parser.add_argument(
        "--expected-cycle-receipt-sha256",
        default="",
        help="Bind a record step to the exact cycle receipt that was already rendered.",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=DEFAULT_MAX_AGE_SECONDS,
    )
    parser.add_argument(
        "--source-type",
        choices=("operator_filesystem", "runtime_container"),
        default="operator_filesystem",
    )
    args = parser.parse_args(argv)
    status = load_operator_status(
        cycle_receipt_path=args.receipt,
        signal_dir=args.signal_dir,
        max_age_seconds=args.max_age_seconds,
        presentation_state_path=args.presentation_state,
    )
    status["source"] = {
        "type": args.source_type,
        "cycle_receipt": str(args.receipt),
        "signal_dir": str(args.signal_dir),
        "presentation_state": str(args.presentation_state),
    }
    if args.record_presentation and status.get("status") != "blocked":
        expected_source_digest = str(
            args.expected_cycle_receipt_sha256 or ""
        ).strip()
        if args.source_type == "runtime_container" and not expected_source_digest:
            status["presentation_receipt"] = {
                "schema": PRESENTATION_RECEIPT_SCHEMA,
                "status": "blocked",
                "recorded_at": _observed_now().isoformat(),
                "blocking_reason": "presentation_source_binding_required",
                "delivery_state_updated": False,
                "provider_quota_consumed": False,
                "protected_operation_executed": False,
            }
        else:
            status["presentation_receipt"] = record_operator_presentation(
                status,
                state_path=args.presentation_state,
                expected_source_cycle_receipt_sha256=expected_source_digest,
            )
    print(json.dumps(status, sort_keys=True))
    return 1 if status.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
