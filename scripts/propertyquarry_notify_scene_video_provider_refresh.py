#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / "ea", ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_notify_gold_status as gold_notify
from scripts.propertyquarry_operator_action import (
    GOVERNED_NOTIFICATION_MAX_SOURCE_AGE_SECONDS,
)


_CANONICAL_PACKET_PATHS = (
    "_completion/scene_video_readiness/provider-refresh-packet.json",
    "_completion/scene_video_readiness/property-scene-video-provider-refresh-packet.json",
)
_CANONICAL_VERIFIER_PATHS = (
    "_completion/scene_video_readiness/provider-refresh-packet-verifier.json",
    "_completion/scene_video_readiness/property-scene-video-provider-refresh-packet-verifier.json",
)
_CANONICAL_RUNTIME_STATUS_PATHS = (
    "_completion/scene_video_readiness/runtime-status.json",
)
_DEFAULT_STATE_PATH = "_completion/scene_video_readiness/provider-refresh-telegram-state.json"
_DEFAULT_REPORT_PATH = "_completion/scene_video_readiness/provider-refresh-telegram-report.json"
_DEFAULT_BASE_URL = "https://propertyquarry.com"
_DEFAULT_PRINCIPAL_ID = "cf-email:tibor.girschele@gmail.com"
_ACCOUNT_FILE_TARGET = "state/incoming_property_tours/_operator-import-lane/scene_video_provider_accounts"
_PACKET_CONTRACT = "propertyquarry.scene_video_provider_refresh_packet.v1"
_RUNTIME_CONTRACT = "propertyquarry.scene_video_runtime_status.v1"
_READINESS_CONTRACT = "propertyquarry.scene_video_readiness.v1"
_ALLOWED_PROVIDERS = ("magicfit", "omagic")
_MAX_SOURCE_AGE_HOURS = 24.0
_MAX_SOURCE_SKEW_SECONDS = 300.0
_PROTECTED_OPERATIONS = [
    "provider_account_material_import",
    "provider_credit_or_plan_change",
    "runtime_configuration_change",
    "provider_quota_consumption",
]


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("json_root_not_object")
    return payload


def _resolve_alias_path(raw_path: str, aliases: tuple[str, ...]) -> Path:
    requested = Path(str(raw_path or "").strip() or aliases[0]).expanduser().resolve()
    if requested.is_file():
        return requested
    canonical_targets = {Path(path).expanduser().resolve() for path in aliases}
    if requested in canonical_targets:
        for candidate_raw in aliases:
            candidate = Path(candidate_raw).expanduser().resolve()
            if candidate.is_file():
                return candidate
    return requested


def _positive_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _provider_label(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "magicfit":
        return "MagicFit"
    if normalized in {"omagic", "magic"}:
        return "OMagic/Magic"
    return str(value or "unknown").strip() or "unknown"


def _actionable_providers(packet: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_row in list(packet.get("providers") or []):
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        credit_refresh_required = row.get("credit_refresh_required") is True
        credit_state = str(row.get("credit_state") or "").strip().lower()
        if (
            _positive_int(row.get("visible_account_gap")) > 0
            or credit_refresh_required
            or credit_state in {"constrained", "insufficient"}
        ):
            rows.append(row)
    return rows


def _combined_digest(packet: dict[str, Any], verifier: dict[str, Any], runtime_status: dict[str, Any]) -> str:
    payload = {
        "packet_digest": gold_notify._payload_digest(packet),
        "verifier_digest": gold_notify._payload_digest(verifier),
        "runtime_status_digest": gold_notify._payload_digest(runtime_status),
    }
    return gold_notify._payload_digest(payload)


def _parse_receipt_datetime(value: object) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fresh_source_times(
    *,
    packet: dict[str, Any],
    verifier: dict[str, Any],
    runtime_status: dict[str, Any],
    now: datetime | None,
    max_source_age_seconds: float,
) -> tuple[dict[str, str], str]:
    raw_times = {
        "packet": str(packet.get("generated_at") or "").strip(),
        "verifier": str(verifier.get("generated_at") or "").strip(),
        "runtime_status": str(runtime_status.get("generated_at") or "").strip(),
        "source_receipt": str(packet.get("source_receipt_generated_at") or "").strip(),
    }
    parsed_times = {key: _parse_receipt_datetime(value) for key, value in raw_times.items()}
    if any(value is None for value in parsed_times.values()):
        return raw_times, "source_receipt_timestamp_invalid"
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        maximum_age = float(max_source_age_seconds)
    except (TypeError, ValueError, OverflowError):
        return raw_times, "source_freshness_policy_not_admissible"
    if not (
        math.isfinite(maximum_age)
        and 60.0 <= maximum_age <= _MAX_SOURCE_AGE_HOURS * 3600.0
    ):
        return raw_times, "source_freshness_policy_not_admissible"
    for parsed in parsed_times.values():
        if parsed is None:
            return raw_times, "source_receipt_timestamp_invalid"
        age_seconds = (observed_now - parsed).total_seconds()
        if (
            not math.isfinite(age_seconds)
            or age_seconds < -30.0
            or age_seconds > maximum_age
        ):
            return raw_times, "source_receipt_not_fresh"
    packet_time = parsed_times["packet"]
    verifier_time = parsed_times["verifier"]
    if packet_time != verifier_time:
        return raw_times, "packet_verifier_not_coherent"
    observed = [value for value in parsed_times.values() if value is not None]
    if (max(observed) - min(observed)).total_seconds() > _MAX_SOURCE_SKEW_SECONDS:
        return raw_times, "source_receipts_not_coherent"
    return raw_times, ""


def scene_video_operator_action_summary(
    *,
    packet: dict[str, Any],
    verifier: dict[str, Any],
    runtime_status: dict[str, Any],
    now: datetime | None = None,
    max_source_age_seconds: float = _MAX_SOURCE_AGE_HOURS * 3600.0,
) -> dict[str, Any]:
    def suppressed(reason: str, *, source_verified: bool = False) -> dict[str, Any]:
        return {
            "status": "none",
            "action_required": False,
            "interrupt_operator": False,
            "reason": reason,
            "source_verified": source_verified,
        }

    if str(packet.get("contract_name") or "").strip() != _PACKET_CONTRACT:
        return suppressed("packet_contract_not_admissible")
    if str(runtime_status.get("contract_name") or "").strip() != _RUNTIME_CONTRACT:
        return suppressed("runtime_contract_not_admissible")
    if (
        str(packet.get("source_receipt_contract_name") or "").strip() != _READINESS_CONTRACT
        or str(runtime_status.get("source_contract_name") or "").strip() != _READINESS_CONTRACT
        or str(runtime_status.get("source_kind") or "").strip() != "receipt_file"
        or not str(packet.get("source_receipt") or "").strip()
        or str(packet.get("source_receipt") or "").strip()
        != str(runtime_status.get("source_ref") or "").strip()
    ):
        return suppressed("source_receipt_provenance_not_admissible")
    if str(verifier.get("status") or "").strip().lower() != "pass":
        return suppressed("packet_verifier_not_pass")

    packet_rows = [row for row in list(packet.get("providers") or []) if isinstance(row, dict)]
    packet_providers = [
        str(row.get("provider") or "").strip().lower()
        for row in packet_rows
        if str(row.get("provider") or "").strip()
    ]
    checked_providers = [
        str(value or "").strip().lower()
        for value in list(verifier.get("checked_providers") or [])
        if str(value or "").strip()
    ]
    if (
        not packet_providers
        or len(packet_providers) != len(set(packet_providers))
        or any(provider not in _ALLOWED_PROVIDERS for provider in packet_providers)
        or set(checked_providers) != set(packet_providers)
        or _positive_int(verifier.get("provider_count")) != len(packet_providers)
        or bool(list(verifier.get("blockers") or []))
    ):
        return suppressed("packet_verifier_scope_not_admissible")
    for row in packet_rows:
        expected = _positive_int(row.get("expected_account_count"))
        runtime = _positive_int(row.get("runtime_account_count"))
        visible_gap = _positive_int(row.get("visible_account_gap"))
        if expected <= 0 or visible_gap != max(0, expected - runtime):
            return suppressed("packet_provider_counts_not_admissible")

    source_times, freshness_error = _fresh_source_times(
        packet=packet,
        verifier=verifier,
        runtime_status=runtime_status,
        now=now,
        max_source_age_seconds=max_source_age_seconds,
    )
    if freshness_error:
        return suppressed(freshness_error)

    candidates = _actionable_providers(packet)
    if not candidates:
        return suppressed("no_actionable_provider_refresh", source_verified=True)

    runtime_summary = dict(runtime_status.get("summary") or {})
    runtime_action_providers = {
        str(value or "").strip().lower()
        for value in list(runtime_summary.get("action_required_providers") or [])
        if str(value or "").strip()
    }
    runtime_rows = {
        str(row.get("provider") or row.get("provider_key") or "").strip().lower(): row
        for row in list(runtime_status.get("providers") or [])
        if isinstance(row, dict)
        and str(row.get("provider") or row.get("provider_key") or "").strip()
    }
    candidate_providers = {
        str(row.get("provider") or "").strip().lower()
        for row in candidates
    }
    runtime_provider_keys = [
        str(row.get("provider") or row.get("provider_key") or "").strip().lower()
        for row in list(runtime_status.get("providers") or [])
        if isinstance(row, dict)
        and str(row.get("provider") or row.get("provider_key") or "").strip()
    ]
    if (
        len(runtime_provider_keys) != len(set(runtime_provider_keys))
        or _positive_int(runtime_summary.get("action_required_count"))
        != len(runtime_action_providers)
        or not candidate_providers.issubset(runtime_action_providers)
        or any(
        not isinstance(runtime_rows.get(provider), dict)
        or runtime_rows[provider].get("attention_required") is not True
        for provider in candidate_providers
        )
    ):
        return suppressed("runtime_does_not_confirm_operator_action")

    action_rows: list[dict[str, Any]] = []
    reason_codes: set[str] = set()
    for row in candidates:
        provider = str(row.get("provider") or "").strip().lower()
        row_reasons: list[str] = []
        if _positive_int(row.get("visible_account_gap")) > 0:
            row_reasons.append("provider_account_material_required")
        credit_state = str(row.get("credit_state") or "").strip().lower()
        if row.get("credit_refresh_required") is True or credit_state in {"constrained", "insufficient"}:
            row_reasons.append("provider_credit_review_required")
        if not row_reasons:
            continue
        reason_codes.update(row_reasons)
        action_rows.append(
            {
                "provider": provider,
                "provider_label": _provider_label(provider),
                "expected_account_count": _positive_int(row.get("expected_account_count")),
                "runtime_account_count": _positive_int(row.get("runtime_account_count")),
                "visible_account_gap": _positive_int(row.get("visible_account_gap")),
                "tracked_account_count": _positive_int(row.get("tracked_account_count")),
                "unavailable_account_count": _positive_int(row.get("unavailable_account_count")),
                "action_reasons": row_reasons,
            }
        )
    if not action_rows:
        return suppressed("no_admissible_operator_action")
    action_rows.sort(key=lambda row: str(row.get("provider") or ""))

    if reason_codes == {"provider_account_material_required"}:
        reason = "provider_account_material_required"
    elif reason_codes == {"provider_credit_review_required"}:
        reason = "provider_credit_review_required"
    else:
        reason = "provider_account_and_credit_review_required"
    return {
        "status": "action_required",
        "action_required": True,
        "interrupt_operator": True,
        "reason": reason,
        "source_verified": True,
        "source_generated_at": source_times["source_receipt"],
        "source_receipts": source_times,
        "providers": action_rows,
        "reversible_next_action": (
            "place the missing or refreshed provider account material in the governed operator-import lane "
            "for review; do not paste credentials into chat, change a provider plan, merge runtime "
            "configuration, or run a proof render without explicit approval"
        ),
        "operator_import_target": _ACCOUNT_FILE_TARGET,
        "notification_policy": "action_required_only",
        "consent_gate": {
            "required": True,
            "automatic_execution_allowed": False,
            "protected_operations": list(_PROTECTED_OPERATIONS),
        },
        "provider_quota_consumption_allowed": False,
    }


def _action_digest(action: dict[str, Any]) -> str:
    semantic_action = {
        key: value
        for key, value in action.items()
        if key not in {"source_generated_at", "source_receipts"}
    }
    return gold_notify._payload_digest(semantic_action)


def _build_message(
    *,
    action: dict[str, Any],
    packet_path: Path,
) -> str:
    consent_gate = dict(action.get("consent_gate") or {})
    lines = [
        "PropertyQuarry scene-video operator action required.",
        f"Reason: {str(action.get('reason') or '').strip()}",
        f"Source generated: {str(action.get('source_generated_at') or '').strip()}",
        f"Governed import target: {str(action.get('operator_import_target') or '').strip()}",
    ]
    for row in list(action.get("providers") or []):
        if not isinstance(row, dict):
            continue
        lines.append(
            f"{str(row.get('provider_label') or 'Provider')}: "
            f"accounts visible {_positive_int(row.get('runtime_account_count'))}/"
            f"{_positive_int(row.get('expected_account_count'))}; "
            f"missing {_positive_int(row.get('visible_account_gap'))}; "
            f"review reasons {', '.join(str(value) for value in list(row.get('action_reasons') or []))}."
        )
    lines.extend(
        [
            f"Safe next action: {str(action.get('reversible_next_action') or '').strip()}",
            (
                "Consent gate: required; automatic execution disabled; protected="
                + ", ".join(
                    str(value)
                    for value in list(consent_gate.get("protected_operations") or [])
                )
            ),
            "Provider quota: disabled",
        ]
    )
    lines.append(f"Packet: {packet_path}")
    return "\n".join(lines)


def build_notification_report(
    *,
    packet: dict[str, Any],
    packet_path: Path,
    verifier: dict[str, Any],
    verifier_path: Path,
    runtime_status: dict[str, Any],
    runtime_status_path: Path,
    state_path: Path,
    principal_id: str,
    base_url: str,
    force: bool,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    verifier_status = str(verifier.get("status") or "").strip().lower()
    action = scene_video_operator_action_summary(
        packet=packet,
        verifier=verifier,
        runtime_status=runtime_status,
        now=now,
        max_source_age_seconds=GOVERNED_NOTIFICATION_MAX_SOURCE_AGE_SECONDS,
    )
    action_required = (
        action.get("action_required") is True
        and action.get("interrupt_operator") is True
        and action.get("notification_policy") == "action_required_only"
    )
    action_digest = _action_digest(action) if action_required else ""
    consent_gate = dict(action.get("consent_gate") or {})
    report: dict[str, Any] = {
        "notification_kind": "scene_video_operator_action_required",
        "packet_path": str(packet_path),
        "verifier_path": str(verifier_path),
        "runtime_status_path": str(runtime_status_path),
        "state_path": str(state_path),
        "principal_id": principal_id,
        "base_url": base_url,
        "verifier_status": verifier_status,
        "actionable_provider_count": len(list(action.get("providers") or [])),
        "packet_generated_at": str(packet.get("generated_at") or "").strip(),
        "runtime_generated_at": str(runtime_status.get("generated_at") or "").strip(),
        "source_digest": _combined_digest(packet, verifier, runtime_status),
        "action_digest": action_digest,
        "action_status": str(action.get("status") or "").strip(),
        "action_required": action_required,
        "interrupt_operator": action.get("interrupt_operator") is True,
        "action_reason": str(action.get("reason") or "").strip(),
        "action_source_generated_at": str(action.get("source_generated_at") or "").strip(),
        "source_verified": action.get("source_verified") is True,
        "consent_required": consent_gate.get("required") is True,
        "automatic_execution_allowed": consent_gate.get("automatic_execution_allowed") is True,
        "provider_quota_consumption_allowed": action.get("provider_quota_consumption_allowed") is True,
        "sent": False,
        "would_send": False,
        "state_updated": False,
        "skipped_reason": "",
        "message_ids": [],
        "delivery_mode": "",
        "checked_at": gold_notify._utc_now_iso(),
    }
    if not action_required:
        report["skipped_reason"] = "no_fresh_operator_action"
        if (
            action.get("source_verified") is True
            and action.get("reason") == "no_actionable_provider_refresh"
            and not dry_run
        ):
            gold_notify._write_notification_state(
                state_path,
                {
                    "last_observed_at": gold_notify._utc_now_iso(),
                    "last_observed_status": "clear",
                    "active_action_digest": "",
                    "notification_kind": "scene_video_operator_action_required",
                    "last_packet_path": str(packet_path),
                    "last_verifier_path": str(verifier_path),
                    "last_runtime_status_path": str(runtime_status_path),
                },
            )
            report["state_updated"] = True
        return report

    if not force and state_path.is_file():
        prior = gold_notify._load_notification_state(state_path)
        if str(prior.get("active_action_digest") or "").strip() == action_digest:
            report["skipped_reason"] = "already_notified_same_action"
            return report

    message = _build_message(action=action, packet_path=packet_path)
    report["would_send"] = True
    if dry_run:
        report["skipped_reason"] = "dry_run"
        report["message_preview"] = message
        return report

    url_buttons = [[("Open PropertyQuarry", base_url)]]
    delivery = gold_notify.deliver_notification_for_principal(
        principal_id=principal_id,
        text=message,
        url_buttons=url_buttons,
    )
    report["delivery_mode"] = str(delivery.get("delivery_mode") or "").strip()
    report["message_ids"] = [str(value) for value in list(delivery.get("message_ids") or []) if str(value or "").strip()]
    if delivery.get("runtime_error"):
        report["runtime_error"] = str(delivery.get("runtime_error") or "").strip()
    if delivery.get("container_runtime_error"):
        report["container_runtime_error"] = str(delivery.get("container_runtime_error") or "").strip()

    gold_notify._write_notification_state(
        state_path,
        {
            "last_notified_at": gold_notify._utc_now_iso(),
            "last_notified_digest": action_digest,
            "active_action_digest": action_digest,
            "last_observed_status": "action_required",
            "last_action_reason": str(action.get("reason") or "").strip(),
            "last_action_source_generated_at": str(action.get("source_generated_at") or "").strip(),
            "notification_kind": "scene_video_operator_action_required",
            "last_packet_path": str(packet_path),
            "last_verifier_path": str(verifier_path),
            "last_runtime_status_path": str(runtime_status_path),
            "message_ids": list(report["message_ids"]),
            "delivery_mode": report["delivery_mode"],
            "principal_id": principal_id,
            "base_url": base_url,
        },
    )
    report["state_updated"] = True
    report["sent"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    gold_notify._load_local_env_defaults()
    parser = argparse.ArgumentParser(
        description="Send only a fresh, verified PropertyQuarry scene-video operator action."
    )
    parser.add_argument("--packet", default=_CANONICAL_PACKET_PATHS[0])
    parser.add_argument("--verifier", default=_CANONICAL_VERIFIER_PATHS[0])
    parser.add_argument("--runtime-status", default=_CANONICAL_RUNTIME_STATUS_PATHS[0])
    parser.add_argument("--state-file", default=_DEFAULT_STATE_PATH)
    parser.add_argument("--principal-id", default=_DEFAULT_PRINCIPAL_ID)
    parser.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and write a sanitized report without sending or changing dedupe state.",
    )
    parser.add_argument("--write", default=_DEFAULT_REPORT_PATH)
    args = parser.parse_args(argv)

    packet_path = _resolve_alias_path(str(args.packet or ""), _CANONICAL_PACKET_PATHS)
    verifier_path = _resolve_alias_path(str(args.verifier or ""), _CANONICAL_VERIFIER_PATHS)
    runtime_status_path = _resolve_alias_path(str(args.runtime_status or ""), _CANONICAL_RUNTIME_STATUS_PATHS)
    if not packet_path.is_file():
        raise SystemExit(f"Scene-video provider refresh packet not found: {packet_path}")
    if not verifier_path.is_file():
        raise SystemExit(f"Scene-video provider refresh verifier not found: {verifier_path}")
    if not runtime_status_path.is_file():
        raise SystemExit(f"Scene-video runtime status receipt not found: {runtime_status_path}")

    report = build_notification_report(
        packet=_load_json(packet_path),
        packet_path=packet_path,
        verifier=_load_json(verifier_path),
        verifier_path=verifier_path,
        runtime_status=_load_json(runtime_status_path),
        runtime_status_path=runtime_status_path,
        state_path=Path(args.state_file).expanduser().resolve(),
        principal_id=str(args.principal_id or "").strip() or _DEFAULT_PRINCIPAL_ID,
        base_url=str(args.base_url or "").strip() or _DEFAULT_BASE_URL,
        force=bool(args.force),
        dry_run=bool(args.dry_run),
    )
    output = json.dumps(report, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write).expanduser().resolve()
        gold_notify._write_notification_state(out_path, report)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
