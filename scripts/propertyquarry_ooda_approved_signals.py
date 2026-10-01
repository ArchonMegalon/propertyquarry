from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from scripts import propertyquarry_notify_scene_video_provider_refresh as scene_notify
from scripts import propertyquarry_ooda_public_origin_observation as public_origin
from scripts.propertyquarry_operator_action import (
    GOLD_STATUS_SCHEMA,
    PROPERTYQUARRY_OPERATOR_NEXT_ACTIONS,
    propertyquarry_operator_action_summary,
)


SCHEMA = "propertyquarry.ooda_approved_signals.v1"
REVOCATION_SCHEMA = "propertyquarry.ooda_signal_revocation.v1"
POLICY = "propertyquarry.ooda_action_required_only.v1"
SOURCE_EVIDENCE_SCHEMA = "propertyquarry.ooda_source_evidence_posture.v1"
DEFAULT_MAX_MANIFEST_AGE_SECONDS = 1800.0
SIGNAL_FILENAMES = {
    "gold_receipt": "property-gold-status.json",
    "public_origin_observation": "public-origin-observation.json",
    "scene_packet": "scene-video-provider-refresh-packet.json",
    "scene_verifier": "scene-video-provider-refresh-verifier.json",
    "scene_runtime_status": "scene-video-runtime-status.json",
}
SOURCE_CONTRACTS = {
    "gold_receipt": GOLD_STATUS_SCHEMA,
    "public_origin_observation": public_origin.SCHEMA,
    "scene_packet": scene_notify._PACKET_CONTRACT,
    "scene_verifier": "propertyquarry.scene_video_provider_refresh_verifier.v1",
    "scene_runtime_status": scene_notify._RUNTIME_CONTRACT,
}
PROTECTED_OPERATIONS = [
    "runtime_configuration_change",
    "deployment_or_restart",
    "provider_account_material_import",
    "provider_credit_or_plan_change",
    "provider_quota_consumption",
]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_PROVIDERS = frozenset({"magicfit", "omagic"})
_ALLOWED_GOLD_REASONS = frozenset(
    {
        "live_runtime_tunnel_unavailable",
        "live_runtime_host_admission_rejected",
        "live_runtime_prerequisite_blocked",
    }
)
_SOURCE_EVIDENCE_LANES = {
    # The public-origin observation is refreshed locally and only corroborates
    # this lane. It is not an externally refreshable producer artifact.
    "gold_live_runtime": ("gold_receipt",),
    "scene_video_provider_refresh": (
        "scene_packet",
        "scene_verifier",
        "scene_runtime_status",
    ),
}
_STALE_SOURCE_REASONS = frozenset(
    {"gold_receipt_not_fresh", "source_receipt_not_fresh"}
)
_REVOCATION_SOURCE_LANE_KEYS = frozenset(
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
_REVOCATION_UNAVAILABLE_REASONS = {
    "gold_live_runtime": "gold_live_runtime_source_receipts_unavailable",
    "scene_video_provider_refresh": "scene_source_receipts_unavailable",
}


def _observed_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def source_timestamp_is_current(
    value: object,
    *,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_MANIFEST_AGE_SECONDS,
) -> bool:
    parsed = _parse_datetime(value)
    try:
        age_limit = float(max_age_seconds)
    except (TypeError, ValueError, OverflowError):
        return False
    if parsed is None or not (
        math.isfinite(age_limit) and 60.0 <= age_limit <= 86400.0
    ):
        return False
    age_seconds = (_observed_now(now) - parsed).total_seconds()
    return bool(
        math.isfinite(age_seconds)
        and age_seconds >= -30.0
        and age_seconds <= age_limit
    )


def normalized_source_timestamp(*values: object) -> str:
    for value in values:
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed.isoformat()
    return ""


def gold_source_timestamp(receipt: Mapping[str, Any]) -> str:
    surface = receipt.get("live_mobile_surfaces")
    action = surface.get("operator_action") if isinstance(surface, Mapping) else None
    return normalized_source_timestamp(
        action.get("source_generated_at") if isinstance(action, Mapping) else None,
        receipt.get("generated_at"),
    )


def public_origin_source_timestamp(receipt: Mapping[str, Any]) -> str:
    return normalized_source_timestamp(receipt.get("generated_at"))


def sanitize_public_origin_observation(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return public_origin.sanitize_public_origin_observation(dict(payload))


def public_origin_operator_action_summary(
    observation: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_source_age_seconds: float = DEFAULT_MAX_MANIFEST_AGE_SECONDS,
) -> dict[str, Any]:
    suppressed = {
        "status": "none",
        "action_required": False,
        "interrupt_operator": False,
    }
    try:
        projection = sanitize_public_origin_observation(observation)
    except ValueError:
        return {**suppressed, "reason": "public_origin_observation_not_admissible"}
    generated_at = public_origin_source_timestamp(projection)
    if not source_timestamp_is_current(
        generated_at,
        now=now,
        max_age_seconds=max_source_age_seconds,
    ):
        return {
            **suppressed,
            "reason": "public_origin_observation_not_fresh",
            "source_generated_at": generated_at,
        }
    if projection.get("status") != "blocked":
        return {
            **suppressed,
            "reason": (
                "public_origin_reachable"
                if projection.get("status") == "reachable"
                else "public_origin_observation_indeterminate"
            ),
            "source_generated_at": generated_at,
        }
    return {
        "status": "action_required",
        "action_required": True,
        "interrupt_operator": True,
        "reason": "live_runtime_tunnel_unavailable",
        "source_generated_at": generated_at,
        "reversible_next_action": PROPERTYQUARRY_OPERATOR_NEXT_ACTIONS[
            "live_runtime_tunnel_unavailable"
        ],
        "notification_policy": "action_required_only",
        "consent_gate": {
            "required": True,
            "automatic_execution_allowed": False,
            "protected_operations": [
                "runtime_configuration_change",
                "deployment_or_restart",
            ],
        },
        "provider_quota_consumption_allowed": False,
    }


def scene_source_timestamp(
    packet: Mapping[str, Any],
    verifier: Mapping[str, Any],
    runtime_status: Mapping[str, Any],
) -> str:
    return normalized_source_timestamp(
        packet.get("source_receipt_generated_at"),
        packet.get("generated_at"),
        verifier.get("generated_at"),
        runtime_status.get("generated_at"),
    )


def build_source_evidence_posture(
    lanes: list[dict[str, Any]],
) -> dict[str, Any]:
    if not (
        len(lanes) == len(_SOURCE_EVIDENCE_LANES)
        and {str(row.get("lane") or "") for row in lanes}
        == set(_SOURCE_EVIDENCE_LANES)
    ):
        raise ValueError("source_evidence_lane_set_not_admissible")
    projected_lanes: list[dict[str, Any]] = []
    counts = {"current": 0, "stale": 0, "unavailable": 0}
    for row in lanes:
        lane = str(row.get("lane") or "")
        reason = str(row.get("reason") or "").strip()
        source_generated_at = str(
            row.get("observed_source_generated_at") or ""
        ).strip()
        if row.get("action_required") is True or row.get("clear_verified") is True:
            status = "current"
        elif reason in _STALE_SOURCE_REASONS:
            status = "stale"
        else:
            status = "unavailable"
        if source_generated_at:
            parsed = _parse_datetime(source_generated_at)
            if parsed is None:
                raise ValueError("source_evidence_timestamp_not_admissible")
            source_generated_at = parsed.isoformat()
        counts[status] += 1
        projected_lanes.append(
            {
                "lane": lane,
                "status": status,
                "reason": reason,
                "source_generated_at": source_generated_at,
                "source_artifacts": list(_SOURCE_EVIDENCE_LANES[lane]),
                "producer_authority": "external_receipt_producer",
                "producer_refresh_required": status != "current",
                "automatic_source_refresh_allowed": False,
            }
        )
    projected_lanes.sort(key=lambda row: str(row["lane"]))
    all_current = counts["current"] == len(_SOURCE_EVIDENCE_LANES)
    blocking_reason = ""
    if not all_current:
        blocking_reason = ",".join(
            f"{row['lane']}:{row['reason'] or row['status']}"
            for row in projected_lanes
            if row["status"] != "current"
        )
    return {
        "schema": SOURCE_EVIDENCE_SCHEMA,
        "status": "verified_current" if all_current else "waiting_for_fresh_sources",
        "blocking_reason": blocking_reason,
        "next_action": (
            "evaluate the current approved lane postures"
            if all_current
            else (
                "refresh the named producer-owned source receipts; the isolated stager will "
                "republish and the scheduler will reevaluate automatically"
                if counts["unavailable"] == 0
                else (
                    "inspect the named source producer contract and file health without calling "
                    "providers or sending; restore its receipt for automatic reevaluation"
                )
            )
        ),
        "recovery_mode": "none" if all_current else "await_producer_receipts",
        "progress": {
            "expected_lane_count": len(_SOURCE_EVIDENCE_LANES),
            "current_lane_count": counts["current"],
            "stale_lane_count": counts["stale"],
            "unavailable_lane_count": counts["unavailable"],
        },
        "lanes": projected_lanes,
        "automatic_source_refresh_allowed": False,
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "protected_operation_executed": False,
    }


def verify_source_evidence_posture(
    *,
    lanes: object,
    posture: object,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    expected_action_count: int | None = None,
) -> dict[str, Any]:
    if not (
        isinstance(lanes, list)
        and all(isinstance(row, dict) for row in lanes)
        and isinstance(posture, Mapping)
    ):
        raise ValueError("source_evidence_posture_not_admissible")
    source_lanes = [dict(row) for row in lanes]
    expected = build_source_evidence_posture(source_lanes)
    if dict(posture) != expected:
        raise ValueError("source_evidence_posture_not_admissible")
    action_lane_count = 0
    projected_by_lane = {
        str(row.get("lane") or ""): row
        for row in list(expected.get("lanes") or [])
        if isinstance(row, Mapping)
    }
    for row in source_lanes:
        lane = str(row.get("lane") or "")
        projected = projected_by_lane.get(lane)
        action_required = row.get("action_required")
        clear_verified = row.get("clear_verified")
        if not (
            isinstance(projected, Mapping)
            and str(row.get("reason") or "").strip()
            and isinstance(action_required, bool)
            and isinstance(clear_verified, bool)
        ):
            raise ValueError("source_evidence_lane_not_admissible")
        if action_required:
            action_lane_count += 1
        observed_timestamp = projected.get("source_generated_at")
        if projected.get("status") == "current":
            if not (
                row.get("source_status") == "ready"
                and action_required != clear_verified
                and source_timestamp_is_current(
                    observed_timestamp,
                    now=now,
                    max_age_seconds=max_age_seconds,
                )
            ):
                raise ValueError("source_evidence_current_lane_not_admissible")
            if action_required and normalized_source_timestamp(
                row.get("source_generated_at")
            ) != normalized_source_timestamp(observed_timestamp):
                raise ValueError("source_evidence_action_timestamp_mismatch")
        elif action_required or clear_verified:
            raise ValueError("source_evidence_noncurrent_lane_not_admissible")
        elif projected.get("status") == "stale":
            if (
                _parse_datetime(observed_timestamp) is None
                or source_timestamp_is_current(
                    observed_timestamp,
                    now=now,
                    max_age_seconds=max_age_seconds,
                )
            ):
                raise ValueError("source_evidence_stale_lane_not_admissible")
    if expected_action_count is not None:
        if (
            isinstance(expected_action_count, bool)
            or not isinstance(expected_action_count, int)
            or expected_action_count < 0
            or action_lane_count != expected_action_count
        ):
            raise ValueError("source_evidence_action_count_mismatch")
    return expected


def _strict_nonnegative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field}_invalid")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if result < 0 or result > 1_000_000:
        raise ValueError(f"{field}_invalid")
    return result


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(payload), indent=2, sort_keys=True, ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sanitized_gold_action(action: object) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise ValueError("gold_operator_action_missing")
    reason = str(action.get("reason") or "").strip()
    generated_at = str(action.get("source_generated_at") or "").strip()
    if (
        reason == "source_receipt_not_fresh"
        and _parse_datetime(generated_at) is not None
        and action.get("required") is False
        and action.get("interrupt_operator") is False
        and action.get("source_fresh") is False
        and action.get("notification_policy") == "suppress_stale_signal"
        and action.get("provider_quota_consumption_allowed") is False
    ):
        return {
            "required": False,
            "interrupt_operator": False,
            "source_fresh": False,
            "source_generated_at": normalized_source_timestamp(generated_at),
            "reason": "source_receipt_not_fresh",
            "notification_policy": "suppress_stale_signal",
            "provider_quota_consumption_allowed": False,
        }
    consent_gate = action.get("consent_gate")
    if not (
        reason in _ALLOWED_GOLD_REASONS
        and _parse_datetime(generated_at) is not None
        and action.get("required") is True
        and action.get("interrupt_operator") is True
        and action.get("source_fresh") is True
        and action.get("notification_policy") == "action_required_only"
        and action.get("provider_quota_consumption_allowed") is False
        and isinstance(consent_gate, dict)
        and consent_gate.get("required") is True
        and consent_gate.get("automatic_execution_allowed") is False
        and consent_gate.get("protected_operations")
        == ["runtime_configuration_change", "deployment_or_restart"]
    ):
        raise ValueError("gold_operator_action_not_admissible")
    return {
        "required": True,
        "interrupt_operator": True,
        "source_fresh": True,
        "source_generated_at": generated_at,
        "reason": reason,
        "notification_policy": "action_required_only",
        "provider_quota_consumption_allowed": False,
        "consent_gate": {
            "required": True,
            "automatic_execution_allowed": False,
            "protected_operations": [
                "runtime_configuration_change",
                "deployment_or_restart",
            ],
        },
    }


def sanitize_gold_receipt(payload: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    source = dict(payload)
    if str(source.get("schema") or "").strip() != GOLD_STATUS_SCHEMA:
        raise ValueError("gold_contract_not_admissible")
    status_value = str(source.get("status") or "").strip().lower()
    generated_at = str(source.get("generated_at") or "").strip()
    if status_value not in {"pass", "blocked"} or _parse_datetime(generated_at) is None:
        raise ValueError("gold_status_not_admissible")
    projection: dict[str, Any] = {
        "schema": GOLD_STATUS_SCHEMA,
        "status": status_value,
        "generated_at": generated_at,
    }
    surface = source.get("live_mobile_surfaces")
    if isinstance(surface, dict) and surface.get("route_probe_blocked") is True:
        projection["live_mobile_surfaces"] = {
            "route_probe_blocked": True,
            "operator_action": _sanitized_gold_action(surface.get("operator_action")),
        }
    action = propertyquarry_operator_action_summary(
        projection,
        now=now,
        max_source_age_seconds=DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    if action.get("action_required") is not True and str(action.get("reason") or "") not in {
        "gold_not_blocked",
        "no_pre_route_runtime_blocker",
        "source_receipt_not_fresh",
    }:
        raise ValueError("gold_projection_not_admissible")
    return projection


def _provider_name(value: object, *, field: str) -> str:
    provider = str(value or "").strip().lower()
    if provider not in _ALLOWED_PROVIDERS:
        raise ValueError(f"{field}_not_admissible")
    return provider


def sanitize_scene_receipts(
    *,
    packet: Mapping[str, Any],
    verifier: Mapping[str, Any],
    runtime_status: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    packet_source = dict(packet)
    verifier_source = dict(verifier)
    runtime_source = dict(runtime_status)
    if str(packet_source.get("contract_name") or "").strip() != scene_notify._PACKET_CONTRACT:
        raise ValueError("scene_packet_contract_not_admissible")
    if str(runtime_source.get("contract_name") or "").strip() != scene_notify._RUNTIME_CONTRACT:
        raise ValueError("scene_runtime_contract_not_admissible")
    source_generated_at = str(packet_source.get("source_receipt_generated_at") or "").strip()
    if _parse_datetime(source_generated_at) is None:
        raise ValueError("scene_source_timestamp_not_admissible")
    source_ref = "approved-signal://propertyquarry/scene-video-readiness"

    packet_rows: list[dict[str, Any]] = []
    observed_providers: set[str] = set()
    for raw_row in list(packet_source.get("providers") or []):
        if not isinstance(raw_row, dict):
            raise ValueError("scene_packet_provider_row_invalid")
        provider = _provider_name(raw_row.get("provider"), field="scene_packet_provider")
        if provider in observed_providers:
            raise ValueError("scene_packet_provider_duplicate")
        observed_providers.add(provider)
        expected = _strict_nonnegative_int(
            raw_row.get("expected_account_count"),
            field="expected_account_count",
        )
        runtime = _strict_nonnegative_int(
            raw_row.get("runtime_account_count"),
            field="runtime_account_count",
        )
        visible_gap = _strict_nonnegative_int(
            raw_row.get("visible_account_gap"),
            field="visible_account_gap",
        )
        if expected <= 0 or visible_gap != max(0, expected - runtime):
            raise ValueError("scene_packet_provider_counts_not_admissible")
        packet_rows.append(
            {
                "provider": provider,
                "expected_account_count": expected,
                "runtime_account_count": runtime,
                "visible_account_gap": visible_gap,
                "tracked_account_count": _strict_nonnegative_int(
                    raw_row.get("tracked_account_count") or 0,
                    field="tracked_account_count",
                ),
                "unavailable_account_count": _strict_nonnegative_int(
                    raw_row.get("unavailable_account_count") or 0,
                    field="unavailable_account_count",
                ),
                "credit_state": str(raw_row.get("credit_state") or "").strip().lower(),
                "credit_refresh_required": raw_row.get("credit_refresh_required") is True,
            }
        )
    if not packet_rows:
        raise ValueError("scene_packet_providers_missing")

    checked_providers = [
        _provider_name(value, field="scene_verifier_provider")
        for value in list(verifier_source.get("checked_providers") or [])
    ]
    verifier_projection = {
        "status": str(verifier_source.get("status") or "").strip().lower(),
        "generated_at": str(verifier_source.get("generated_at") or "").strip(),
        "provider_count": _strict_nonnegative_int(
            verifier_source.get("provider_count"),
            field="scene_verifier_provider_count",
        ),
        "checked_providers": checked_providers,
        "blockers": ["source_verifier_blocked"]
        if list(verifier_source.get("blockers") or [])
        else [],
    }

    runtime_summary_source = runtime_source.get("summary")
    if not isinstance(runtime_summary_source, dict):
        raise ValueError("scene_runtime_summary_invalid")
    action_providers = []
    for value in list(runtime_summary_source.get("action_required_providers") or []):
        provider = str(value or "").strip().lower()
        if provider in observed_providers and provider not in action_providers:
            action_providers.append(provider)
    runtime_rows: list[dict[str, Any]] = []
    runtime_provider_names: set[str] = set()
    for raw_row in list(runtime_source.get("providers") or []):
        if not isinstance(raw_row, dict):
            raise ValueError("scene_runtime_provider_row_invalid")
        provider = str(raw_row.get("provider") or raw_row.get("provider_key") or "").strip().lower()
        if provider not in observed_providers:
            continue
        if provider in runtime_provider_names:
            raise ValueError("scene_runtime_provider_duplicate")
        runtime_provider_names.add(provider)
        runtime_rows.append(
            {
                "provider": provider,
                "provider_key": provider,
                "attention_required": raw_row.get("attention_required") is True,
            }
        )

    packet_projection = {
        "contract_name": scene_notify._PACKET_CONTRACT,
        "generated_at": str(packet_source.get("generated_at") or "").strip(),
        "source_receipt": source_ref,
        "source_receipt_contract_name": scene_notify._READINESS_CONTRACT,
        "source_receipt_generated_at": source_generated_at,
        "providers": packet_rows,
    }
    runtime_projection = {
        "contract_name": scene_notify._RUNTIME_CONTRACT,
        "generated_at": str(runtime_source.get("generated_at") or "").strip(),
        "source_contract_name": scene_notify._READINESS_CONTRACT,
        "source_kind": "receipt_file",
        "source_ref": source_ref,
        "summary": {
            "action_required_count": len(action_providers),
            "action_required_providers": action_providers,
        },
        "providers": runtime_rows,
    }
    projections = {
        "scene_packet": packet_projection,
        "scene_verifier": verifier_projection,
        "scene_runtime_status": runtime_projection,
    }
    action = scene_notify.scene_video_operator_action_summary(
        packet=packet_projection,
        verifier=verifier_projection,
        runtime_status=runtime_projection,
        now=now,
        max_source_age_seconds=DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    if action.get("action_required") is not True and str(action.get("reason") or "") not in {
        "no_actionable_provider_refresh",
        "source_receipt_not_fresh",
    }:
        raise ValueError("scene_projection_not_admissible")
    return projections


def _normalized_revocation_source_lanes(
    lanes: object,
) -> list[dict[str, Any]]:
    if not isinstance(lanes, list):
        raise ValueError("source_revocation_lanes_not_admissible")
    normalized: list[dict[str, Any]] = []
    for raw in lanes:
        if not isinstance(raw, dict) or set(raw) != _REVOCATION_SOURCE_LANE_KEYS:
            raise ValueError("source_revocation_lane_not_admissible")
        lane = str(raw.get("lane") or "").strip()
        source_status = str(raw.get("source_status") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        action_required = raw.get("action_required")
        clear_verified = raw.get("clear_verified")
        source_generated_at = str(raw.get("source_generated_at") or "").strip()
        observed_source_generated_at = str(
            raw.get("observed_source_generated_at") or ""
        ).strip()
        if not (
            lane in _SOURCE_EVIDENCE_LANES
            and source_status in {"ready", "unavailable"}
            and reason
            and isinstance(action_required, bool)
            and isinstance(clear_verified, bool)
        ):
            raise ValueError("source_revocation_lane_not_admissible")
        if source_generated_at:
            normalized_timestamp = normalized_source_timestamp(source_generated_at)
            if not normalized_timestamp:
                raise ValueError("source_revocation_lane_timestamp_not_admissible")
            source_generated_at = normalized_timestamp
        if observed_source_generated_at:
            normalized_observed = normalized_source_timestamp(
                observed_source_generated_at
            )
            if not normalized_observed:
                raise ValueError("source_revocation_lane_timestamp_not_admissible")
            observed_source_generated_at = normalized_observed
        normalized.append(
            {
                "lane": lane,
                "source_status": source_status,
                "action_required": action_required,
                "clear_verified": clear_verified,
                "reason": reason,
                "source_generated_at": source_generated_at,
                "observed_source_generated_at": observed_source_generated_at,
            }
        )
    normalized.sort(key=lambda row: str(row["lane"]))
    if len(normalized) != len(_SOURCE_EVIDENCE_LANES):
        raise ValueError("source_revocation_lanes_not_admissible")
    return normalized


def build_revocation_manifest(
    *,
    source_lanes: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = _observed_now(now)
    normalized_lanes = _normalized_revocation_source_lanes(source_lanes)
    posture = build_source_evidence_posture(normalized_lanes)
    verify_source_evidence_posture(
        lanes=normalized_lanes,
        posture=posture,
        now=generated_at,
        max_age_seconds=DEFAULT_MAX_MANIFEST_AGE_SECONDS,
    )
    if int(dict(posture.get("progress") or {}).get("unavailable_lane_count") or 0) < 1:
        raise ValueError("source_revocation_requires_unavailable_lane")
    projected_by_lane = {
        str(row.get("lane") or ""): row
        for row in list(posture.get("lanes") or [])
        if isinstance(row, Mapping)
    }
    for lane in normalized_lanes:
        lane_name = str(lane["lane"])
        projected = projected_by_lane.get(lane_name)
        if isinstance(projected, Mapping) and projected.get("status") == "unavailable":
            if not (
                lane.get("source_status") == "unavailable"
                and lane.get("reason")
                == _REVOCATION_UNAVAILABLE_REASONS[lane_name]
                and lane.get("source_generated_at") == ""
                and lane.get("observed_source_generated_at") == ""
            ):
                raise ValueError("source_revocation_unavailable_lane_not_admissible")
    return {
        "schema": REVOCATION_SCHEMA,
        "generated_at": generated_at.isoformat(),
        "status": "revoked",
        "reason": "producer_source_unavailable",
        "revocation": {
            "decision": "revoked",
            "authority": "operator_owned_read_only_ingress",
            "policy": POLICY,
            "notification_policy": "action_required_only",
        },
        "source_lanes": normalized_lanes,
        "source_evidence_posture": posture,
        "publication": {
            "commit_file": "manifest.json",
            "commit_order": "revocation_manifest_only",
        },
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "protected_operation_executed": False,
    }


def verify_revocation_manifest(
    *,
    manifest: Mapping[str, Any],
    manifest_evidence: Mapping[str, Any],
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_MANIFEST_AGE_SECONDS,
) -> dict[str, Any]:
    def rejected(reason: str) -> dict[str, Any]:
        return {
            "approved": False,
            "reason": reason,
            "manifest_generated_at": str(manifest.get("generated_at") or "").strip(),
            "policy": "",
            "revocation_verified": False,
        }

    manifest_mode = manifest_evidence.get("mode")
    manifest_sha256 = str(manifest_evidence.get("sha256") or "").strip()
    if (
        manifest_evidence.get("status") != "ready"
        or isinstance(manifest_mode, bool)
        or not isinstance(manifest_mode, int)
        or manifest_mode & 0o022
        or not _SHA256_RE.fullmatch(manifest_sha256)
    ):
        return rejected("revocation_manifest_file_not_admissible")
    if str(manifest.get("schema") or "").strip() != REVOCATION_SCHEMA:
        return rejected("revocation_manifest_contract_not_admissible")
    generated_at = _parse_datetime(manifest.get("generated_at"))
    try:
        age_limit = float(max_age_seconds)
    except (TypeError, ValueError, OverflowError):
        return rejected("revocation_manifest_timestamp_invalid")
    if generated_at is None or not (
        math.isfinite(age_limit) and 60.0 <= age_limit <= 86400.0
    ):
        return rejected("revocation_manifest_timestamp_invalid")
    observed_now = _observed_now(now)
    age_seconds = (observed_now - generated_at).total_seconds()
    if not math.isfinite(age_seconds) or age_seconds < -30.0 or age_seconds > age_limit:
        return rejected("revocation_manifest_not_fresh")
    try:
        source_lanes = _normalized_revocation_source_lanes(
            manifest.get("source_lanes")
        )
        expected = build_revocation_manifest(
            source_lanes=source_lanes,
            now=generated_at,
        )
        if dict(manifest) != expected:
            raise ValueError("source_revocation_contract_mismatch")
        source_evidence_posture = verify_source_evidence_posture(
            lanes=source_lanes,
            posture=manifest.get("source_evidence_posture"),
            now=observed_now,
            max_age_seconds=age_limit,
        )
    except (TypeError, ValueError):
        return rejected("revocation_manifest_contract_not_admissible")
    return {
        "approved": False,
        "reason": "approved_projection_manifest_revoked",
        "manifest_generated_at": generated_at.isoformat(),
        "policy": POLICY,
        "revocation_verified": True,
        "revocation_manifest_sha256": manifest_sha256,
        "source_lanes": source_lanes,
        "source_evidence_posture": source_evidence_posture,
    }


def build_approval_manifest(
    *,
    signal_bytes: Mapping[str, bytes],
    source_evidence: Mapping[str, Mapping[str, Any]],
    source_generated_at: Mapping[str, str],
    source_contracts: Mapping[str, str],
    now: datetime | None = None,
) -> dict[str, Any]:
    if not (
        set(signal_bytes) == set(SIGNAL_FILENAMES)
        and set(source_evidence) == set(SIGNAL_FILENAMES)
        and set(source_generated_at) == set(SIGNAL_FILENAMES)
        and set(source_contracts) == set(SIGNAL_FILENAMES)
    ):
        raise ValueError("approved_signal_set_incomplete")
    generated_at = _observed_now(now).isoformat()
    signals: dict[str, dict[str, Any]] = {}
    for name, filename in SIGNAL_FILENAMES.items():
        payload = bytes(signal_bytes[name])
        source_row = dict(source_evidence.get(name) or {})
        source_sha256 = str(source_row.get("sha256") or "").strip().lower()
        source_timestamp = str(source_generated_at.get(name) or "").strip()
        source_contract = str(source_contracts.get(name) or "").strip()
        if not _SHA256_RE.fullmatch(source_sha256):
            raise ValueError(f"{name}_source_digest_invalid")
        if _parse_datetime(source_timestamp) is None:
            raise ValueError(f"{name}_source_timestamp_invalid")
        if source_contract != SOURCE_CONTRACTS[name]:
            raise ValueError(f"{name}_source_contract_invalid")
        signals[name] = {
            "filename": filename,
            "sha256": sha256_bytes(payload),
            "bytes": len(payload),
            "source_sha256": source_sha256,
            "source_generated_at": source_timestamp,
            "source_contract": source_contract,
        }
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "status": "approved_projection",
        "approval": {
            "decision": "approved_projection",
            "authority": "operator_owned_read_only_ingress",
            "policy": POLICY,
            "notification_policy": "action_required_only",
            "automatic_execution_allowed": False,
            "provider_quota_consumption_allowed": False,
            "protected_operations": list(PROTECTED_OPERATIONS),
        },
        "signals": signals,
        "publication": {
            "commit_file": "manifest.json",
            "commit_order": "signals_then_manifest",
        },
    }


def verify_approval_manifest(
    *,
    manifest: Mapping[str, Any],
    manifest_evidence: Mapping[str, Any],
    input_evidence: Mapping[str, Mapping[str, Any]],
    input_payloads: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_MANIFEST_AGE_SECONDS,
) -> dict[str, Any]:
    def rejected(reason: str) -> dict[str, Any]:
        return {
            "approved": False,
            "reason": reason,
            "manifest_generated_at": str(manifest.get("generated_at") or "").strip(),
            "policy": "",
        }

    manifest_schema = str(manifest.get("schema") or "").strip()
    if manifest_schema == REVOCATION_SCHEMA:
        return verify_revocation_manifest(
            manifest=manifest,
            manifest_evidence=manifest_evidence,
            now=now,
            max_age_seconds=max_age_seconds,
        )
    if manifest_schema != SCHEMA:
        return rejected("approval_manifest_contract_not_admissible")
    manifest_mode = manifest_evidence.get("mode")
    manifest_sha256 = str(manifest_evidence.get("sha256") or "").strip()
    if (
        manifest_evidence.get("status") != "ready"
        or isinstance(manifest_mode, bool)
        or not isinstance(manifest_mode, int)
        or manifest_mode & 0o022
        or not _SHA256_RE.fullmatch(manifest_sha256)
        or manifest_sha256 != sha256_bytes(canonical_json_bytes(manifest))
    ):
        return rejected("approval_manifest_file_not_admissible")
    if str(manifest.get("status") or "").strip() != "approved_projection":
        return rejected("approval_manifest_status_not_admissible")
    generated_at = _parse_datetime(manifest.get("generated_at"))
    try:
        age_limit = float(max_age_seconds)
    except (TypeError, ValueError, OverflowError):
        return rejected("approval_manifest_timestamp_invalid")
    if generated_at is None or not math.isfinite(age_limit) or not 60.0 <= age_limit <= 86400.0:
        return rejected("approval_manifest_timestamp_invalid")
    age_seconds = (_observed_now(now) - generated_at).total_seconds()
    if not math.isfinite(age_seconds) or age_seconds < -30.0 or age_seconds > age_limit:
        return rejected("approval_manifest_not_fresh")

    if not (
        isinstance(input_payloads, Mapping)
        and set(input_payloads) == set(SIGNAL_FILENAMES)
        and all(isinstance(input_payloads.get(name), Mapping) for name in SIGNAL_FILENAMES)
    ):
        return rejected("approved_signal_payloads_not_admissible")
    projected_payloads = {
        name: dict(input_payloads.get(name) or {}) for name in SIGNAL_FILENAMES
    }
    try:
        expected_gold = sanitize_gold_receipt(
            projected_payloads["gold_receipt"],
            now=_observed_now(now),
        )
        expected_scene = sanitize_scene_receipts(
            packet=projected_payloads["scene_packet"],
            verifier=projected_payloads["scene_verifier"],
            runtime_status=projected_payloads["scene_runtime_status"],
            now=_observed_now(now),
        )
    except (TypeError, ValueError):
        return rejected("approved_signal_payload_contract_not_admissible")
    if (
        projected_payloads["gold_receipt"] != expected_gold
        or any(
            projected_payloads[name] != expected_scene[name]
            for name in (
                "scene_packet",
                "scene_verifier",
                "scene_runtime_status",
            )
        )
    ):
        return rejected("approved_signal_payload_contract_not_admissible")

    signals = manifest.get("signals")
    if not isinstance(signals, dict) or set(signals) != set(SIGNAL_FILENAMES):
        return rejected("approval_signal_set_incomplete")
    canonical_signal_bytes: dict[str, bytes] = {}
    source_digests: dict[str, dict[str, str]] = {}
    for name, filename in SIGNAL_FILENAMES.items():
        row = signals.get(name)
        evidence = dict(input_evidence.get(name) or {})
        if not isinstance(row, dict):
            return rejected(f"{name}_approval_missing")
        expected_size = row.get("bytes")
        if (
            row.get("filename") != filename
            or not _SHA256_RE.fullmatch(str(row.get("sha256") or ""))
            or not _SHA256_RE.fullmatch(str(row.get("source_sha256") or ""))
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or not 1 <= expected_size <= 16 * 1024 * 1024
            or _parse_datetime(row.get("source_generated_at")) is None
            or row.get("source_contract") != SOURCE_CONTRACTS[name]
        ):
            return rejected(f"{name}_approval_not_admissible")
        if evidence.get("status") != "ready":
            return rejected(f"{name}_input_not_ready")
        if str(evidence.get("sha256") or "") != str(row.get("sha256") or ""):
            return rejected(f"{name}_hash_mismatch")
        if evidence.get("bytes") != expected_size:
            return rejected(f"{name}_size_mismatch")
        mode = evidence.get("mode")
        if isinstance(mode, bool) or not isinstance(mode, int) or mode & 0o022:
            return rejected(f"{name}_mode_not_admissible")
        canonical_payload = canonical_json_bytes(projected_payloads[name])
        if (
            sha256_bytes(canonical_payload) != str(evidence.get("sha256") or "")
            or len(canonical_payload) != evidence.get("bytes")
        ):
            return rejected(f"{name}_payload_not_canonical")
        canonical_signal_bytes[name] = canonical_payload
        source_digests[name] = {"sha256": str(row.get("source_sha256") or "")}
    try:
        expected_manifest = build_approval_manifest(
            signal_bytes=canonical_signal_bytes,
            source_evidence=source_digests,
            source_generated_at={
                name: str(projected_payloads[name].get("generated_at") or "").strip()
                for name in SIGNAL_FILENAMES
            },
            source_contracts=SOURCE_CONTRACTS,
            now=generated_at,
        )
    except (TypeError, ValueError):
        return rejected("approval_manifest_contract_not_admissible")
    if dict(manifest) != expected_manifest:
        return rejected("approval_manifest_contract_not_admissible")
    return {
        "approved": True,
        "reason": "approved_projection_manifest_verified",
        "manifest_generated_at": generated_at.isoformat(),
        "policy": POLICY,
    }
