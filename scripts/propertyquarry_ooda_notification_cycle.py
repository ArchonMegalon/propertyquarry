#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_notify_gold_status as gold_notify
from scripts import propertyquarry_notify_scene_video_provider_refresh as scene_notify
from scripts import propertyquarry_ooda_approved_signals as approved_signals
from scripts.propertyquarry_operator_action import GOLD_STATUS_SCHEMA, propertyquarry_operator_action_summary
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_notification_cycle.v1"
SOURCE_EVIDENCE_SCHEMA = approved_signals.SOURCE_EVIDENCE_SCHEMA
DEFAULT_STATE_PATH = "_completion/propertyquarry_ooda_notification_cycle/state.json"
DEFAULT_RECEIPT_PATH = "_completion/propertyquarry_ooda_notification_cycle/latest.json"
DEFAULT_LOCK_PATH = "_completion/propertyquarry_ooda_notification_cycle/send.lock"
DEFAULT_PRINCIPAL_ID = "cf-email:tibor.girschele@gmail.com"
DEFAULT_BASE_URL = "https://propertyquarry.com"
DEFAULT_PUBLIC_ORIGIN_OBSERVATION_PATH = str(
    approved_signals.public_origin.DEFAULT_RECEIPT_PATH
)
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_CLEAR_EVIDENCE_AGE_HOURS = (
    approved_signals.DEFAULT_MAX_MANIFEST_AGE_SECONDS / 3600.0
)
_DELIVERY_MODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MAX_DELIVERY_MESSAGE_ID_LENGTH = 512
NOTIFICATION_STATE_SCHEMA = "propertyquarry.ooda_notification_state.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NOTIFICATION_STATE_KEYS = frozenset(
    {
        "schema",
        "updated_at",
        "active_actions",
        "last_cycle_status",
        "last_delivery_mode",
        "last_message_ids",
    }
)
_NOTIFICATION_STATE_LANES = frozenset(
    {"gold_live_runtime", "scene_video_provider_refresh"}
)
_NOTIFICATION_STATE_STATUSES = frozenset(
    {"completed", "silent", "deduplicated"}
)


def _observed_now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _lexical_absolute_path(value: object) -> Path:
    raw = str(value or "").strip()
    return Path(os.path.abspath(os.fspath(Path(raw).expanduser())))


def _resolve_alias_path(raw_path: str, aliases: tuple[str, ...]) -> Path:
    requested = _lexical_absolute_path(raw_path or aliases[0])
    try:
        requested.lstat()
    except OSError:
        pass
    else:
        return requested
    canonical = {_lexical_absolute_path(value) for value in aliases}
    if requested in canonical:
        for value in aliases:
            candidate = _lexical_absolute_path(value)
            try:
                candidate.lstat()
            except OSError:
                continue
            return candidate
    return requested


def _load_input(path: Path, *, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence: dict[str, Any] = {
        "path": str(path),
        "status": "invalid",
        "sha256": "",
    }
    try:
        payload, _raw, digest = load_strict_json_object_snapshot(
            path,
            field=label,
            maximum_bytes=MAX_INPUT_BYTES,
        )
    except Exception:
        try:
            path.lstat()
        except OSError:
            evidence["status"] = "missing"
        return {}, evidence
    evidence["status"] = "ready"
    evidence["sha256"] = digest
    metadata = path.stat(follow_symlinks=False)
    evidence["bytes"] = metadata.st_size
    evidence["mode"] = stat.S_IMODE(metadata.st_mode)
    return payload, evidence


def _acquire_send_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()}:
            raise ValueError("send_lock_not_admissible")
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


def _release_send_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def normalized_delivery_receipt(
    value: object,
) -> tuple[str, list[str]] | None:
    """Return bounded transport identity without assuming a specific provider."""

    if not isinstance(value, Mapping):
        return None
    delivery_mode = str(value.get("delivery_mode") or "").strip()
    raw_message_ids = value.get("message_ids")
    if not (
        _DELIVERY_MODE.fullmatch(delivery_mode)
        and isinstance(raw_message_ids, list)
        and raw_message_ids
    ):
        return None
    message_ids: list[str] = []
    for raw_message_id in raw_message_ids:
        if not isinstance(raw_message_id, str):
            return None
        message_id = raw_message_id.strip()
        if not (
            message_id == raw_message_id
            and 1 <= len(message_id) <= MAX_DELIVERY_MESSAGE_ID_LENGTH
            and message_id.isprintable()
            and message_id not in message_ids
        ):
            return None
        message_ids.append(message_id)
    return delivery_mode, message_ids


def _semantic_action_digest(action: dict[str, Any]) -> str:
    normalized = {
        key: value
        for key, value in action.items()
        if key not in {"source_generated_at", "source_receipts"}
    }
    return gold_notify._payload_digest(normalized)


def _fresh_gold_receipt(
    receipt: dict[str, Any],
    *,
    now: datetime | None,
) -> bool:
    generated_at = scene_notify._parse_receipt_datetime(receipt.get("generated_at"))
    if generated_at is None:
        return False
    age_seconds = (_observed_now(now) - generated_at).total_seconds()
    return (
        math.isfinite(age_seconds)
        and age_seconds >= -30.0
        and age_seconds <= MAX_CLEAR_EVIDENCE_AGE_HOURS * 3600.0
    )


def _fresh_gold_clear_evidence(
    receipt: dict[str, Any],
    action: dict[str, Any],
    *,
    now: datetime | None,
) -> bool:
    return (
        action.get("action_required") is not True
        and str(action.get("reason") or "").strip()
        in {"gold_not_blocked", "no_pre_route_runtime_blocker"}
        and _fresh_gold_receipt(receipt, now=now)
    )


def _normalized_source_timestamp(*values: object) -> str:
    return approved_signals.normalized_source_timestamp(*values)


def _gold_source_timestamp(receipt: dict[str, Any]) -> str:
    return approved_signals.gold_source_timestamp(receipt)


def _scene_source_timestamp(
    packet: dict[str, Any],
    verifier: dict[str, Any],
    runtime_status: dict[str, Any],
) -> str:
    return approved_signals.scene_source_timestamp(
        packet,
        verifier,
        runtime_status,
    )


def _build_source_evidence_posture(
    lanes: list[dict[str, Any]],
) -> dict[str, Any]:
    return approved_signals.build_source_evidence_posture(lanes)


def _action_projection(lane: str, action: dict[str, Any]) -> dict[str, Any]:
    consent_gate = dict(action.get("consent_gate") or {})
    projection: dict[str, Any] = {
        "lane": lane,
        "reason": str(action.get("reason") or "").strip(),
        "source_generated_at": str(action.get("source_generated_at") or "").strip(),
        "safe_next_action": str(action.get("reversible_next_action") or "").strip(),
        "consent_required": consent_gate.get("required") is True,
        "automatic_execution_allowed": consent_gate.get("automatic_execution_allowed") is True,
        "protected_operations": [
            str(value)
            for value in list(consent_gate.get("protected_operations") or [])
            if str(value).strip()
        ],
        "provider_quota_consumption_allowed": action.get("provider_quota_consumption_allowed") is True,
    }
    providers = []
    for row in list(action.get("providers") or []):
        if not isinstance(row, dict):
            continue
        providers.append(
            {
                "provider": str(row.get("provider") or "").strip(),
                "provider_label": str(row.get("provider_label") or "").strip(),
                "visible_account_gap": scene_notify._positive_int(row.get("visible_account_gap")),
                "action_reasons": [
                    str(value)
                    for value in list(row.get("action_reasons") or [])
                    if str(value).strip()
                ],
            }
        )
    if providers:
        projection["providers"] = providers
    return projection


def _build_consolidated_message(actions: list[dict[str, Any]], *, generated_at: str) -> str:
    labels = {
        "gold_live_runtime": "Live runtime",
        "scene_video_provider_refresh": "Scene-video providers",
    }
    lines = [
        "PropertyQuarry operator action required.",
        f"Cycle generated: {generated_at}",
    ]
    protected_operations: list[str] = []
    for index, action in enumerate(actions, start=1):
        lane = str(action.get("lane") or "").strip()
        lines.extend(
            [
                "",
                f"{index}. {labels.get(lane, lane or 'Action')}",
                f"Reason: {str(action.get('reason') or '').strip()}",
                f"Source generated: {str(action.get('source_generated_at') or '').strip()}",
            ]
        )
        providers = [row for row in list(action.get("providers") or []) if isinstance(row, dict)]
        if providers:
            provider_summary = ", ".join(
                f"{str(row.get('provider_label') or row.get('provider') or 'provider')}: "
                f"missing {scene_notify._positive_int(row.get('visible_account_gap'))}"
                for row in providers
            )
            lines.append(f"Scope: {provider_summary}")
        lines.append(f"Safe next action: {str(action.get('safe_next_action') or '').strip()}")
        for operation in list(action.get("protected_operations") or []):
            normalized = str(operation or "").strip()
            if normalized and normalized not in protected_operations:
                protected_operations.append(normalized)
    lines.extend(
        [
            "",
            (
                "Consent gate: required; automatic execution disabled; protected="
                + ", ".join(protected_operations)
            ),
            "Provider quota: disabled",
        ]
    )
    return "\n".join(lines)


def _active_actions_from_state(state: dict[str, Any]) -> dict[str, str]:
    raw = state.get("active_actions")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }


def _load_notification_cycle_state(
    path: Path,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str, str]:
    """Load one exact incident ledger; invalid state must never deduplicate."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {}, "missing", ""
    except OSError:
        return {}, "invalid", ""
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
            field="ooda_notification_state",
            maximum_bytes=gold_notify._MAX_NOTIFICATION_STATE_BYTES,
        )
    except Exception:
        return {}, "invalid", ""
    updated_at_raw = payload.get("updated_at")
    try:
        updated_at = datetime.fromisoformat(
            updated_at_raw.replace("Z", "+00:00")
        ) if isinstance(updated_at_raw, str) else None
    except ValueError:
        updated_at = None
    active_actions = payload.get("active_actions")
    last_cycle_status = str(payload.get("last_cycle_status") or "")
    last_delivery_mode = str(payload.get("last_delivery_mode") or "")
    last_message_ids = payload.get("last_message_ids")
    if not (
        set(payload) == _NOTIFICATION_STATE_KEYS
        and payload.get("schema") == NOTIFICATION_STATE_SCHEMA
        and updated_at is not None
        and updated_at.tzinfo is not None
        and updated_at.astimezone(timezone.utc)
        <= _observed_now(now) + timedelta(seconds=30)
        and isinstance(active_actions, dict)
        and set(active_actions) <= _NOTIFICATION_STATE_LANES
        and all(
            isinstance(lane, str)
            and _SHA256.fullmatch(str(action_digest or ""))
            for lane, action_digest in active_actions.items()
        )
        and last_cycle_status in _NOTIFICATION_STATE_STATUSES
        and isinstance(last_message_ids, list)
    ):
        return {}, "invalid", digest
    if last_cycle_status == "completed":
        if normalized_delivery_receipt(
            {
                "delivery_mode": last_delivery_mode,
                "message_ids": last_message_ids,
            }
        ) != (last_delivery_mode, last_message_ids):
            return {}, "invalid", digest
    elif last_delivery_mode or last_message_ids:
        return {}, "invalid", digest
    return payload, "ready", digest


def build_cycle_report(
    *,
    gold_receipt: dict[str, Any],
    scene_packet: dict[str, Any],
    scene_verifier: dict[str, Any],
    scene_runtime_status: dict[str, Any],
    input_evidence: dict[str, dict[str, Any]],
    state_path: Path,
    principal_id: str,
    base_url: str,
    send: bool,
    now: datetime | None = None,
    deliver: Callable[..., dict[str, Any]] | None = None,
    public_origin_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    generated_at = _observed_now(now).isoformat()
    prior_state, notification_state_status, notification_state_sha256 = (
        _load_notification_cycle_state(state_path, now=now)
    )
    active_before = _active_actions_from_state(prior_state)
    current_actions: dict[str, tuple[str, dict[str, Any]]] = {}
    clear_lanes: set[str] = set()
    lanes: list[dict[str, Any]] = []

    gold_input_ready = input_evidence.get("gold_receipt", {}).get("status") == "ready"
    gold_contract_ready = str(gold_receipt.get("schema") or "").strip() == GOLD_STATUS_SCHEMA
    gold_receipt_fresh = _fresh_gold_receipt(gold_receipt, now=now) if gold_contract_ready else False
    if gold_input_ready and gold_contract_ready and gold_receipt_fresh:
        gold_action = propertyquarry_operator_action_summary(
            gold_receipt,
            now=now,
            max_source_age_seconds=(
                approved_signals.DEFAULT_MAX_MANIFEST_AGE_SECONDS
            ),
        )
        gold_action_required = (
            gold_action.get("action_required") is True
            and gold_action.get("interrupt_operator") is True
            and gold_action.get("notification_policy") == "action_required_only"
        )
        gold_clear = _fresh_gold_clear_evidence(gold_receipt, gold_action, now=now)
    elif not gold_input_ready:
        gold_action = {"reason": f"input_{input_evidence.get('gold_receipt', {}).get('status') or 'invalid'}"}
        gold_action_required = False
        gold_clear = False
    elif not gold_contract_ready:
        gold_action = {"reason": "gold_contract_not_admissible"}
        gold_action_required = False
        gold_clear = False
    else:
        gold_action = {"reason": "gold_receipt_not_fresh"}
        gold_action_required = False
        gold_clear = False
    gold_lane: dict[str, Any] = {
        "lane": "gold_live_runtime",
        "source_status": input_evidence.get("gold_receipt", {}).get("status", "invalid"),
        "action_required": gold_action_required,
        "clear_verified": gold_clear,
        "reason": str(gold_action.get("reason") or "").strip(),
        "source_generated_at": str(gold_action.get("source_generated_at") or "").strip(),
        "observed_source_generated_at": _gold_source_timestamp(gold_receipt),
    }
    public_projection = dict(public_origin_observation or {})
    public_input_ready = (
        input_evidence.get("public_origin_observation", {}).get("status")
        == "ready"
    )
    public_action = (
        approved_signals.public_origin_operator_action_summary(
            public_projection,
            now=now,
            max_source_age_seconds=(
                approved_signals.DEFAULT_MAX_MANIFEST_AGE_SECONDS
            ),
        )
        if public_input_ready
        else {"action_required": False}
    )
    public_action_required = (
        public_action.get("action_required") is True
        and public_action.get("interrupt_operator") is True
        and public_action.get("notification_policy") == "action_required_only"
    )
    if public_action_required:
        gold_action = public_action
        gold_action_required = True
        gold_clear = False
        gold_lane.update(
            {
                "source_status": "ready",
                "action_required": True,
                "clear_verified": False,
                "reason": str(public_action.get("reason") or "").strip(),
                "source_generated_at": str(
                    public_action.get("source_generated_at") or ""
                ).strip(),
                "observed_source_generated_at": (
                    approved_signals.public_origin_source_timestamp(
                        public_projection
                    )
                ),
            }
        )
    if gold_action_required:
        digest = _semantic_action_digest(gold_action)
        projection = _action_projection("gold_live_runtime", gold_action)
        current_actions["gold_live_runtime"] = (digest, projection)
        gold_lane["action_digest"] = digest
        gold_lane["novel_action"] = active_before.get("gold_live_runtime") != digest
    elif gold_clear:
        clear_lanes.add("gold_live_runtime")
    lanes.append(gold_lane)

    scene_inputs_ready = all(
        input_evidence.get(name, {}).get("status") == "ready"
        for name in ("scene_packet", "scene_verifier", "scene_runtime_status")
    )
    if scene_inputs_ready:
        scene_action = scene_notify.scene_video_operator_action_summary(
            packet=scene_packet,
            verifier=scene_verifier,
            runtime_status=scene_runtime_status,
            now=now,
            max_source_age_seconds=(
                approved_signals.DEFAULT_MAX_MANIFEST_AGE_SECONDS
            ),
        )
        scene_action_required = (
            scene_action.get("action_required") is True
            and scene_action.get("interrupt_operator") is True
            and scene_action.get("notification_policy") == "action_required_only"
        )
        scene_clear = (
            scene_action.get("source_verified") is True
            and scene_action.get("reason") == "no_actionable_provider_refresh"
        )
    else:
        failed_statuses = sorted(
            {
                str(input_evidence.get(name, {}).get("status") or "invalid")
                for name in ("scene_packet", "scene_verifier", "scene_runtime_status")
                if input_evidence.get(name, {}).get("status") != "ready"
            }
        )
        scene_action = {"reason": f"input_{'_'.join(failed_statuses) or 'invalid'}"}
        scene_action_required = False
        scene_clear = False
    scene_lane: dict[str, Any] = {
        "lane": "scene_video_provider_refresh",
        "source_status": "ready" if scene_inputs_ready else "invalid",
        "action_required": scene_action_required,
        "clear_verified": scene_clear,
        "reason": str(scene_action.get("reason") or "").strip(),
        "source_generated_at": str(scene_action.get("source_generated_at") or "").strip(),
        "observed_source_generated_at": _scene_source_timestamp(
            scene_packet,
            scene_verifier,
            scene_runtime_status,
        ),
    }
    if scene_action_required:
        digest = _semantic_action_digest(scene_action)
        projection = _action_projection("scene_video_provider_refresh", scene_action)
        current_actions["scene_video_provider_refresh"] = (digest, projection)
        scene_lane["action_digest"] = digest
        scene_lane["novel_action"] = active_before.get("scene_video_provider_refresh") != digest
    elif scene_clear:
        clear_lanes.add("scene_video_provider_refresh")
    lanes.append(scene_lane)

    novel_actions = [
        projection
        for lane, (digest, projection) in current_actions.items()
        if active_before.get(lane) != digest
    ]
    next_active = dict(active_before)
    for lane in clear_lanes:
        next_active.pop(lane, None)
    projected_active = dict(next_active)
    for lane, (digest, _projection) in current_actions.items():
        projected_active[lane] = digest

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "status": "silent",
        "execution_mode": "send" if send else "evaluate_only",
        "notification_policy": "action_required_only",
        "delivery_authorized": send,
        "delivery_attempted": False,
        "sent": False,
        "would_send": bool(novel_actions),
        "notification_count": 0,
        "message_ids": [],
        "delivery_mode": "",
        "state_path": str(state_path),
        "state_updated": False,
        "notification_state_status": notification_state_status,
        "notification_state_sha256": notification_state_sha256,
        "notification_state_admissible": notification_state_status != "invalid",
        "input_evidence": input_evidence,
        "lanes": lanes,
        "source_evidence_posture": _build_source_evidence_posture(lanes),
        "action_required_count": len(current_actions),
        "novel_action_count": len(novel_actions),
        "operator_action_required": bool(current_actions),
        "interrupt_operator": bool(novel_actions),
        "active_action_count_before": len(active_before),
        "active_action_count_projected": len(projected_active),
        "protected_operation_executed": False,
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "actions": novel_actions,
    }

    if novel_actions:
        message = _build_consolidated_message(novel_actions, generated_at=generated_at)
        if notification_state_status == "invalid":
            report["status"] = "action_required"
            report["execution_mode"] = "evaluate_only"
            report["delivery_authorized"] = False
            report["message_preview"] = message
            report["next_action"] = (
                "repair or explicitly replace the private notification incident ledger, then "
                "rerun with --send only when factual operator delivery remains authorized"
            )
            if send:
                report["send_requested"] = True
            return report
        if not send:
            report["status"] = "action_required"
            report["message_preview"] = message
            report["next_action"] = "rerun with --send only when factual operator delivery is authorized"
            return report
        report["delivery_attempted"] = True
        delivery_function = deliver or gold_notify.deliver_notification_for_principal
        try:
            delivery = delivery_function(
                principal_id=principal_id,
                text=message,
                url_buttons=[[('Open PropertyQuarry', base_url)]],
            )
        except Exception as exc:
            report["status"] = "delivery_failed"
            report["delivery_error_code"] = type(exc).__name__
            report["next_action"] = "inspect the operator transport binding without changing protected operations"
            return report
        normalized_delivery = normalized_delivery_receipt(delivery)
        if normalized_delivery is None:
            report["status"] = "delivery_failed"
            report["delivery_error_code"] = "delivery_receipt_incomplete"
            report["next_action"] = "inspect the operator transport receipt without changing protected operations"
            return report
        delivery_mode, message_ids = normalized_delivery
        report["sent"] = True
        report["would_send"] = False
        report["notification_count"] = 1
        report["delivery_mode"] = delivery_mode
        report["message_ids"] = message_ids
        for lane, (digest, _projection) in current_actions.items():
            next_active[lane] = digest
        report["status"] = "completed"
        report["next_action"] = "await fresh approved signals; protected operations remain consent-gated"
    elif current_actions:
        report["status"] = "deduplicated"
        report["would_send"] = False
        report["interrupt_operator"] = False
        report["next_action"] = "await a verified action change or fresh clear evidence"
    else:
        report["status"] = "silent"
        source_posture = dict(report.get("source_evidence_posture") or {})
        report["next_action"] = (
            str(source_posture.get("next_action") or "")
            if source_posture.get("status") == "waiting_for_fresh_sources"
            else "await fresh approved signals"
        )

    if notification_state_status == "invalid":
        report["status"] = "notification_state_invalid"
        report["execution_mode"] = "evaluate_only"
        report["delivery_authorized"] = False
        report["interrupt_operator"] = False
        report["actions"] = []
        report["next_action"] = (
            "repair or explicitly replace the private notification incident ledger before "
            "another send-enabled cycle"
        )
        if send:
            report["send_requested"] = True
        report["active_action_count_after"] = 0
        return report

    if send and next_active != active_before:
        try:
            gold_notify._write_notification_state(
                state_path,
                {
                    "schema": NOTIFICATION_STATE_SCHEMA,
                    "updated_at": generated_at,
                    "active_actions": next_active,
                    "last_cycle_status": report["status"],
                    "last_delivery_mode": report["delivery_mode"],
                    "last_message_ids": list(report["message_ids"]),
                },
            )
        except Exception as exc:
            report["state_error_code"] = type(exc).__name__
            report["status"] = "delivery_unrecorded" if report["sent"] else "state_update_failed"
            report["next_action"] = "repair the private incident ledger before another send-enabled cycle"
        else:
            persisted_state, persisted_status, persisted_sha256 = (
                _load_notification_cycle_state(state_path, now=now)
            )
            report["notification_state_status"] = persisted_status
            report["notification_state_sha256"] = persisted_sha256
            report["notification_state_admissible"] = (
                persisted_status != "invalid"
            )
            if not (
                persisted_status == "ready"
                and _active_actions_from_state(persisted_state) == next_active
            ):
                report["state_error_code"] = "state_verification_failed"
                report["status"] = (
                    "delivery_unrecorded" if report["sent"] else "state_update_failed"
                )
                report["next_action"] = (
                    "repair the private incident ledger before another send-enabled cycle"
                )
            else:
                report["state_updated"] = True
    report["active_action_count_after"] = len(next_active)
    return report


def _load_cycle_inputs(
    *,
    gold_receipt: str,
    public_origin_observation: str,
    scene_packet: str,
    scene_verifier: str,
    scene_runtime_status: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    paths = {
        "gold_receipt": _resolve_alias_path(
            gold_receipt,
            gold_notify._CANONICAL_GOLD_RECEIPT_PATHS,
        ),
        "scene_packet": _resolve_alias_path(
            scene_packet,
            scene_notify._CANONICAL_PACKET_PATHS,
        ),
        "scene_verifier": _resolve_alias_path(
            scene_verifier,
            scene_notify._CANONICAL_VERIFIER_PATHS,
        ),
        "scene_runtime_status": _resolve_alias_path(
            scene_runtime_status,
            scene_notify._CANONICAL_RUNTIME_STATUS_PATHS,
        ),
    }
    payloads: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for name, path in paths.items():
        payloads[name], evidence[name] = _load_input(path, label=name)
    if public_origin_observation:
        public_origin_path = _lexical_absolute_path(public_origin_observation)
        payloads["public_origin_observation"], evidence[
            "public_origin_observation"
        ] = _load_input(
            public_origin_path,
            label="public_origin_observation",
        )
    else:
        payloads["public_origin_observation"] = {}
        evidence["public_origin_observation"] = {
            "path": "",
            "status": "not_configured",
            "sha256": "",
        }
    return payloads, evidence


def _apply_verified_revocation(
    report: dict[str, Any],
    *,
    signal_approval: dict[str, Any],
    send_requested: bool,
) -> None:
    """Publish exact source posture while suppressing every revoked action."""

    source_lanes = signal_approval.get("source_lanes")
    source_evidence_posture = signal_approval.get("source_evidence_posture")
    if not (
        signal_approval.get("approved") is False
        and signal_approval.get("revocation_verified") is True
        and signal_approval.get("reason")
        == "approved_projection_manifest_revoked"
        and signal_approval.get("policy") == approved_signals.POLICY
        and isinstance(source_lanes, list)
        and all(isinstance(row, dict) for row in source_lanes)
        and isinstance(source_evidence_posture, dict)
        and report.get("status") == "silent"
        and int(report.get("action_required_count") or 0) == 0
        and int(report.get("novel_action_count") or 0) == 0
        and report.get("delivery_attempted") is False
        and report.get("sent") is False
        and not list(report.get("actions") or [])
    ):
        raise ValueError("verified_revocation_cycle_not_admissible")
    suppressed_action_count = sum(
        1 for row in source_lanes if row.get("action_required") is True
    )
    report.update(
        {
            "status": "silent",
            "execution_mode": (
                "revocation_fail_closed" if send_requested else "evaluate_only"
            ),
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "would_send": False,
            "operator_action_required": False,
            "interrupt_operator": False,
            "publication_status": "revoked",
            "revocation_suppressed_action_count": suppressed_action_count,
            "lanes": [dict(row) for row in source_lanes],
            "source_evidence_posture": dict(source_evidence_posture),
            "next_action": str(source_evidence_posture.get("next_action") or ""),
        }
    )
    if send_requested:
        report["send_requested"] = True


def run_cycle_once(
    *,
    gold_receipt: str = gold_notify._CANONICAL_GOLD_RECEIPT_PATHS[0],
    public_origin_observation: str = "",
    scene_packet: str = scene_notify._CANONICAL_PACKET_PATHS[0],
    scene_verifier: str = scene_notify._CANONICAL_VERIFIER_PATHS[0],
    scene_runtime_status: str = scene_notify._CANONICAL_RUNTIME_STATUS_PATHS[0],
    approval_manifest: str = "",
    require_approval_manifest: bool = False,
    approval_manifest_max_age_seconds: float = approved_signals.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    state_file: str = DEFAULT_STATE_PATH,
    lock_file: str = DEFAULT_LOCK_PATH,
    write: str = DEFAULT_RECEIPT_PATH,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    base_url: str = DEFAULT_BASE_URL,
    send: bool = False,
    now: datetime | None = None,
    deliver: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one bounded cycle without argparse or implicit environment loading."""

    payloads, evidence = _load_cycle_inputs(
        gold_receipt=str(gold_receipt or ""),
        public_origin_observation=str(public_origin_observation or ""),
        scene_packet=str(scene_packet or ""),
        scene_verifier=str(scene_verifier or ""),
        scene_runtime_status=str(scene_runtime_status or ""),
    )
    if approval_manifest:
        manifest_payload, manifest_evidence = _load_input(
            _lexical_absolute_path(approval_manifest),
            label="approval_manifest",
        )
        evidence["approval_manifest"] = manifest_evidence
        signal_approval = approved_signals.verify_approval_manifest(
            manifest=manifest_payload,
            manifest_evidence=manifest_evidence,
            input_evidence=evidence,
            input_payloads=payloads,
            now=now,
            max_age_seconds=approval_manifest_max_age_seconds,
        )
    elif require_approval_manifest:
        evidence["approval_manifest"] = {
            "path": "",
            "status": "missing",
            "sha256": "",
        }
        signal_approval = {
            "approved": False,
            "reason": "approval_manifest_missing",
            "manifest_generated_at": "",
            "policy": "",
        }
    else:
        signal_approval = {
            "approved": True,
            "reason": "approval_manifest_not_required",
            "manifest_generated_at": "",
            "policy": "legacy_direct_evaluation",
        }
    verified_revocation = signal_approval.get("revocation_verified") is True
    if (require_approval_manifest or bool(approval_manifest)) and signal_approval.get("approved") is not True:
        for name in (
            "gold_receipt",
            "public_origin_observation",
            "scene_packet",
            "scene_verifier",
            "scene_runtime_status",
        ):
            payloads[name] = {}
            if evidence.get(name, {}).get("status") == "ready":
                evidence[name]["status"] = (
                    "revoked" if verified_revocation else "unapproved"
                )
    state_path = _lexical_absolute_path(state_file or DEFAULT_STATE_PATH)
    build_args = {
        "gold_receipt": payloads["gold_receipt"],
        "public_origin_observation": payloads[
            "public_origin_observation"
        ],
        "scene_packet": payloads["scene_packet"],
        "scene_verifier": payloads["scene_verifier"],
        "scene_runtime_status": payloads["scene_runtime_status"],
        "input_evidence": evidence,
        "state_path": state_path,
        "principal_id": str(principal_id or "").strip() or DEFAULT_PRINCIPAL_ID,
        "base_url": str(base_url or "").strip() or DEFAULT_BASE_URL,
        "now": now,
        "deliver": deliver,
    }
    if not send or verified_revocation:
        report = build_cycle_report(**build_args, send=False)
    else:
        lock_path = _lexical_absolute_path(lock_file or DEFAULT_LOCK_PATH)
        try:
            lock_descriptor = _acquire_send_lock(lock_path)
        except Exception as exc:
            report = build_cycle_report(**build_args, send=False)
            report.update(
                {
                    "status": "send_lock_unavailable",
                    "execution_mode": "send",
                    "delivery_authorized": True,
                    "delivery_attempted": False,
                    "interrupt_operator": False,
                    "lock_error_code": type(exc).__name__,
                    "next_action": "repair the private send lock before another send-enabled cycle",
                }
            )
        else:
            if lock_descriptor is None:
                report = build_cycle_report(**build_args, send=False)
                report.update(
                    {
                        "status": "send_cycle_busy",
                        "execution_mode": "send",
                        "delivery_authorized": True,
                        "delivery_attempted": False,
                        "interrupt_operator": False,
                        "next_action": "wait for the active send-enabled cycle to publish its receipt",
                    }
                )
            else:
                try:
                    report = build_cycle_report(**build_args, send=True)
                finally:
                    _release_send_lock(lock_descriptor)
    if verified_revocation:
        _apply_verified_revocation(
            report,
            signal_approval=signal_approval,
            send_requested=send,
        )
    output_path = _lexical_absolute_path(write or DEFAULT_RECEIPT_PATH)
    report["signal_approval"] = signal_approval
    gold_notify._write_notification_state(output_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate approved PropertyQuarry signals and optionally send one consolidated action alert."
    )
    parser.add_argument("--gold-receipt", default=gold_notify._CANONICAL_GOLD_RECEIPT_PATHS[0])
    parser.add_argument(
        "--public-origin-observation",
        default="",
        help="Optional explicit normalized public-origin observation receipt.",
    )
    parser.add_argument("--scene-packet", default=scene_notify._CANONICAL_PACKET_PATHS[0])
    parser.add_argument("--scene-verifier", default=scene_notify._CANONICAL_VERIFIER_PATHS[0])
    parser.add_argument("--scene-runtime-status", default=scene_notify._CANONICAL_RUNTIME_STATUS_PATHS[0])
    parser.add_argument("--approval-manifest", default="")
    parser.add_argument(
        "--require-approval-manifest",
        action="store_true",
        help="Fail closed unless a fresh manifest binds every sanitized signal input.",
    )
    parser.add_argument(
        "--approval-manifest-max-age-seconds",
        type=float,
        default=approved_signals.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    parser.add_argument("--state-file", default=DEFAULT_STATE_PATH)
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_PATH)
    parser.add_argument("--write", default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--principal-id", default=DEFAULT_PRINCIPAL_ID)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--send",
        action="store_true",
        help="Authorize one factual notification after every per-source action gate passes.",
    )
    args = parser.parse_args(argv)

    if args.send:
        gold_notify._load_local_env_defaults()
    report = run_cycle_once(
        gold_receipt=str(args.gold_receipt or ""),
        public_origin_observation=str(args.public_origin_observation or ""),
        scene_packet=str(args.scene_packet or ""),
        scene_verifier=str(args.scene_verifier or ""),
        scene_runtime_status=str(args.scene_runtime_status or ""),
        approval_manifest=str(args.approval_manifest or ""),
        require_approval_manifest=bool(args.require_approval_manifest),
        approval_manifest_max_age_seconds=float(args.approval_manifest_max_age_seconds),
        state_file=str(args.state_file or ""),
        lock_file=str(args.lock_file or ""),
        write=str(args.write or ""),
        principal_id=str(args.principal_id or ""),
        base_url=str(args.base_url or ""),
        send=bool(args.send),
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if (
        report.get("notification_state_admissible") is False
        or report.get("status") in {
        "delivery_failed",
        "delivery_unrecorded",
        "state_update_failed",
        "send_cycle_busy",
        "send_lock_unavailable",
        "notification_state_invalid",
        }
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
