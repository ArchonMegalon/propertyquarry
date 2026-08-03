from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from app.domain.models import ToolInvocationRequest
from app.product.property_fact_enrichment import (
    PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION,
    property_fact_coordinate_digest,
    property_fact_input_digest,
    property_fact_issue_provider_attestation,
    property_fact_source_fingerprint,
)


PROPERTY_ONEMIN_EVALUATION_SCHEMA_VERSION = "propertyquarry.onemin-evaluation.v1"
PROPERTY_ONEMIN_OODA_SCHEMA_VERSION = "propertyquarry.onemin-ooda.v1"
PROPERTY_GOOGLE_MAPS_RESEARCH_SCHEMA_VERSION = (
    "propertyquarry.google-maps-browseract-distance.v1"
)

_RECOMMENDATIONS = {"shortlist", "consider", "hold", "reject"}
_TRAVEL_MODES = {"walking", "driving", "bicycling", "transit"}
_SECRET_KEY_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
)


def _env_flag(name: str, *, default: bool) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def property_onemin_evaluation_enabled() -> bool:
    return _env_flag("PROPERTYQUARRY_ONEMIN_EVALUATION_ENABLED", default=False)


def property_google_maps_ooda_enabled() -> bool:
    return _env_flag("PROPERTYQUARRY_ONEMIN_GOOGLE_MAPS_OODA_ENABLED", default=False)


def _bounded_text(value: object, *, limit: int) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _bounded_string_list(value: object, *, limit: int = 6) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        normalized = _bounded_text(item, limit=240)
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _finite_float(value: object, *, minimum: float, maximum: float) -> float | None:
    try:
        parsed = float(str(value or "").strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        return None
    return parsed


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _safe_json_value(value: object, *, depth: int = 0) -> object:
    if depth >= 3:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _bounded_text(value, limit=600)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, item in list(value.items())[:48]:
            key = str(raw_key or "").strip()[:80]
            lowered = key.lower()
            if not key or any(marker in lowered for marker in _SECRET_KEY_MARKERS):
                continue
            safe_item = _safe_json_value(item, depth=depth + 1)
            if safe_item not in (None, "", [], {}):
                result[key] = safe_item
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            safe_item
            for item in list(value)[:24]
            if (safe_item := _safe_json_value(item, depth=depth + 1))
            not in (None, "", [], {})
        ]
    return _bounded_text(value, limit=300)


def _evidence_rows(plan: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw_row in list(plan)[:32]:
        row = dict(raw_row)
        provenance = (
            dict(row.get("provenance") or {})
            if isinstance(row.get("provenance"), Mapping)
            else {}
        )
        state = str(row.get("state") or "unknown").strip().lower()
        rows.append(
            {
                "key": _bounded_text(row.get("key"), limit=80),
                "label": _bounded_text(row.get("label"), limit=120),
                "priority": (
                    "required"
                    if str(row.get("priority") or "").strip().lower()
                    == "required"
                    else "lazy"
                ),
                "state": state,
                "value": row.get("value") if state == "resolved" else None,
                "provider": _bounded_text(
                    provenance.get("provider") or row.get("provider"),
                    limit=80,
                ),
                "receipt_url": _bounded_text(
                    provenance.get("receipt_url"), limit=800
                ),
                "query_digest": _bounded_text(
                    provenance.get("query_digest"), limit=80
                ),
            }
        )
    return rows


def property_onemin_evaluation_input(
    *,
    candidate: Mapping[str, object],
    facts: Mapping[str, object],
    preferences: Mapping[str, object],
    plan: Sequence[Mapping[str, object]],
    score: Mapping[str, object],
) -> dict[str, object]:
    fact_rows = _evidence_rows(plan)
    unresolved = [
        str(row.get("key") or "").strip()
        for row in fact_rows
        if str(row.get("state") or "") != "resolved"
    ]
    resolved_fact_keys = {
        str(row.get("key") or "").strip()
        for row in fact_rows
        if str(row.get("state") or "") == "resolved"
    }
    compact_facts = {
        key: _safe_json_value(value)
        for key, value in list(dict(facts).items())[:160]
        if (
            key in resolved_fact_keys
            or key
            in {
                "address",
                "area_m2",
                "area_sqm",
                "currency",
                "district",
                "floor",
                "listing_mode",
                "location",
                "map_lat",
                "map_lng",
                "map_location_precision",
                "monthly_rent",
                "postal_code",
                "price",
                "purchase_price",
                "rooms",
            }
        )
        and _safe_json_value(value) not in (None, "", [], {})
    }
    compact_preferences = _safe_json_value(dict(preferences))
    return {
        "schema_version": PROPERTY_ONEMIN_EVALUATION_SCHEMA_VERSION,
        "candidate": {
            "candidate_ref": _bounded_text(
                candidate.get("candidate_ref")
                or candidate.get("research_candidate_ref"),
                limit=160,
            ),
            "title": _bounded_text(candidate.get("title"), limit=240),
            "source_label": _bounded_text(candidate.get("source_label"), limit=120),
            "property_url": urllib.parse.urldefrag(
                str(
                    candidate.get("property_url")
                    or candidate.get("listing_url")
                    or ""
                ).strip()
            )[0][:1200],
            "description": _bounded_text(
                candidate.get("description") or candidate.get("summary"), limit=1200
            ),
        },
        "verified_facts": compact_facts,
        "fact_evidence": fact_rows,
        "unresolved_fact_keys": unresolved,
        "preferences": compact_preferences,
        "deterministic_score": {
            "state": _bounded_text(score.get("state"), limit=32),
            "current": score.get("current"),
            "ranking_eligible": bool(score.get("ranking_eligible")),
            "algorithm_version": _bounded_text(
                score.get("algorithm_version"), limit=96
            ),
        },
    }


def property_onemin_evaluation_needs_refresh(
    *,
    candidate: Mapping[str, object],
    facts: Mapping[str, object],
    preferences: Mapping[str, object],
    plan: Sequence[Mapping[str, object]],
    score: Mapping[str, object],
) -> bool:
    if not property_onemin_evaluation_enabled():
        return False
    # Do not enqueue assessment-only work that cannot execute. A later request
    # will naturally refresh once the shared EA 1min Manager is active.
    from app.services.onemin_manager import active_onemin_manager

    if active_onemin_manager() is None:
        return False
    existing = (
        dict(candidate.get("onemin_evaluation") or {})
        if isinstance(candidate.get("onemin_evaluation"), Mapping)
        else {}
    )
    packet_input = property_onemin_evaluation_input(
        candidate=candidate,
        facts=facts,
        preferences=preferences,
        plan=plan,
        score=score,
    )
    return not (
        str(existing.get("schema_version") or "")
        == PROPERTY_ONEMIN_EVALUATION_SCHEMA_VERSION
        and str(existing.get("status") or "") == "succeeded"
        and str(existing.get("input_digest") or "")
        == property_fact_input_digest(packet_input)
    )


def _evaluation_prompt() -> str:
    return """
Return one JSON object only, with this exact shape:
{
  "recommendation": "shortlist|consider|hold|reject",
  "confidence": 0.0,
  "summary": "concise property judgment",
  "strengths": ["..."],
  "risks": ["..."],
  "evidence_keys": ["verified fact keys only"],
  "missing_fact_keys": ["unresolved keys only"],
  "research_actions": [
    {
      "fact_key": "one unresolved lazy distance key",
      "reason": "why it matters to the stated preference",
      "travel_mode": "walking|driving|bicycling|transit",
      "priority": 1
    }
  ]
}

Evaluate the home against the supplied preferences. Treat only resolved facts with
provider receipts as evidence. Never invent a value, distance, address, amenity,
price, room, or source. Do not produce or revise a numeric fit score. If evidence is
missing, say so and propose at most two bounded Google Maps research actions for
unresolved lazy distance keys. Required facts and the deterministic score remain
authoritative.
""".strip()


def _normalize_research_actions(
    value: object,
    *,
    plan: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    eligible = {
        str(row.get("key") or "").strip(): dict(row)
        for row in plan
        if str(row.get("key") or "").strip()
        and str(row.get("priority") or "lazy").strip().lower() == "lazy"
        and str(row.get("state") or "unknown").strip().lower() != "resolved"
        and str(row.get("key") or "").strip().endswith("_m")
    }
    actions: list[dict[str, object]] = []
    rows = list(value) if isinstance(value, (list, tuple)) else []
    for raw_action in rows:
        if not isinstance(raw_action, Mapping):
            continue
        fact_key = str(raw_action.get("fact_key") or "").strip()
        if fact_key not in eligible or any(
            str(existing.get("fact_key") or "") == fact_key for existing in actions
        ):
            continue
        travel_mode = str(raw_action.get("travel_mode") or "walking").strip().lower()
        if travel_mode not in _TRAVEL_MODES:
            travel_mode = "walking"
        try:
            priority = max(1, min(int(raw_action.get("priority") or len(actions) + 1), 9))
        except (TypeError, ValueError):
            priority = len(actions) + 1
        actions.append(
            {
                "action_id": "pqo_"
                + hashlib.sha256(fact_key.encode("utf-8")).hexdigest()[:16],
                "fact_key": fact_key,
                "label": _bounded_text(eligible[fact_key].get("label"), limit=120),
                "provider": "google_maps_browseract",
                "work_type": "research",
                "reason": _bounded_text(raw_action.get("reason"), limit=240),
                "travel_mode": travel_mode,
                "priority": priority,
                "status": "planned",
            }
        )
        if len(actions) >= 2:
            break
    return sorted(actions, key=lambda row: int(row.get("priority") or 9))


def _safe_failure_code(value: object) -> str:
    normalized = re.sub(
        r"[^a-z0-9_]", "_", str(value or "").strip().lower()
    ).strip("_")
    return normalized[:96] or "onemin_evaluation_unavailable"


def run_property_onemin_evaluation(
    *,
    tool_execution: object,
    principal_id: str,
    run_id: str,
    candidate_ref: str,
    candidate: Mapping[str, object],
    facts: Mapping[str, object],
    preferences: Mapping[str, object],
    plan: Sequence[Mapping[str, object]],
    score: Mapping[str, object],
    existing: Mapping[str, object] | None = None,
) -> dict[str, object]:
    packet_input = property_onemin_evaluation_input(
        candidate=candidate,
        facts=facts,
        preferences=preferences,
        plan=plan,
        score=score,
    )
    input_digest = property_fact_input_digest(packet_input)
    previous = dict(existing or {})
    if (
        str(previous.get("schema_version") or "")
        == PROPERTY_ONEMIN_EVALUATION_SCHEMA_VERSION
        and str(previous.get("input_digest") or "") == input_digest
        and str(previous.get("status") or "") == "succeeded"
    ):
        return {**previous, "cache_hit": True}
    if not property_onemin_evaluation_enabled():
        return {
            "schema_version": PROPERTY_ONEMIN_EVALUATION_SCHEMA_VERSION,
            "status": "disabled",
            "input_digest": input_digest,
            "manager_routed": False,
            "cache_hit": False,
            "judgment": {},
            "ooda": {
                "schema_version": PROPERTY_ONEMIN_OODA_SCHEMA_VERSION,
                "phase": "observe",
                "actions": [],
            },
            "receipt": {},
            "error": {"code": "onemin_evaluation_disabled"},
        }
    from app.services.onemin_manager import active_onemin_manager

    if active_onemin_manager() is None:
        return {
            "schema_version": PROPERTY_ONEMIN_EVALUATION_SCHEMA_VERSION,
            "status": "unavailable",
            "input_digest": input_digest,
            "manager_routed": False,
            "cache_hit": False,
            "judgment": {},
            "ooda": {
                "schema_version": PROPERTY_ONEMIN_OODA_SCHEMA_VERSION,
                "phase": "observe",
                "actions": [],
            },
            "receipt": {},
            "error": {"code": "onemin_manager_unavailable"},
        }
    try:
        result = tool_execution.execute_invocation(
            ToolInvocationRequest(
                session_id=f"property-evaluation:{run_id}"[:160],
                step_id=f"onemin:{candidate_ref}"[:160],
                tool_name="provider.onemin.code_generate",
                action_kind="property.evaluate",
                payload_json={
                    "model": str(
                        os.getenv("PROPERTYQUARRY_ONEMIN_EVALUATION_MODEL")
                        or "deepseek-chat"
                    ).strip(),
                    "instructions": (
                        "You are PropertyQuarry's evidence-bound property analyst "
                        "and OODA research planner."
                    ),
                    "goal": (
                        "Assess this property qualitatively and select the next "
                        "safe research actions for missing soft filters."
                    ),
                    "context_pack": packet_input,
                    "prompt": _evaluation_prompt(),
                },
                context_json={
                    "principal_id": str(principal_id or "").strip(),
                    "suppress_telegram_delivery": True,
                },
            )
        )
    except Exception as exc:
        return {
            "schema_version": PROPERTY_ONEMIN_EVALUATION_SCHEMA_VERSION,
            "status": "unavailable",
            "input_digest": input_digest,
            "manager_routed": True,
            "cache_hit": False,
            "judgment": {},
            "ooda": {
                "schema_version": PROPERTY_ONEMIN_OODA_SCHEMA_VERSION,
                "phase": "observe",
                "actions": [],
            },
            "receipt": {},
            "error": {
                "code": _safe_failure_code(str(exc).split(":", 1)[0]),
            },
        }
    output = dict(result.output_json or {})
    structured = (
        dict(output.get("structured_output_json") or {})
        if isinstance(output.get("structured_output_json"), Mapping)
        else {}
    )
    recommendation = str(structured.get("recommendation") or "hold").strip().lower()
    if recommendation not in _RECOMMENDATIONS:
        recommendation = "hold"
    confidence = _finite_float(
        structured.get("confidence"), minimum=0.0, maximum=1.0
    )
    confidence = confidence if confidence is not None else 0.0
    allowed_evidence_keys = {
        str(row.get("key") or "").strip()
        for row in plan
        if str(row.get("state") or "").strip().lower() == "resolved"
    }
    unresolved_keys = {
        str(row.get("key") or "").strip()
        for row in plan
        if str(row.get("state") or "").strip().lower() != "resolved"
    }
    evidence_keys = [
        key
        for key in _bounded_string_list(structured.get("evidence_keys"), limit=12)
        if key in allowed_evidence_keys
    ]
    missing_fact_keys = [
        key
        for key in _bounded_string_list(structured.get("missing_fact_keys"), limit=12)
        if key in unresolved_keys
    ]
    actions = _normalize_research_actions(
        structured.get("research_actions"), plan=plan
    )
    evaluated_at = datetime.now(timezone.utc).isoformat()
    raw_receipt = dict(result.receipt_json or {})
    provider_account = _bounded_text(
        raw_receipt.get("provider_account_name")
        or output.get("provider_account_name"),
        limit=120,
    )
    provider_slot = _bounded_text(
        raw_receipt.get("provider_key_slot") or output.get("provider_key_slot"),
        limit=80,
    )
    return {
        "schema_version": PROPERTY_ONEMIN_EVALUATION_SCHEMA_VERSION,
        "status": "succeeded",
        "input_digest": input_digest,
        "manager_routed": True,
        "cache_hit": False,
        "evaluated_at": evaluated_at,
        "judgment": {
            "recommendation": recommendation,
            "confidence": confidence,
            "summary": _bounded_text(structured.get("summary"), limit=600),
            "strengths": _bounded_string_list(structured.get("strengths")),
            "risks": _bounded_string_list(structured.get("risks")),
            "evidence_keys": evidence_keys,
            "missing_fact_keys": missing_fact_keys,
        },
        "ooda": {
            "schema_version": PROPERTY_ONEMIN_OODA_SCHEMA_VERSION,
            "phase": "decide" if actions else "observe",
            "actions": actions,
        },
        "receipt": {
            "tool_name": "provider.onemin.code_generate",
            "action_kind": "property.evaluate",
            "provider": "1minAI",
            "provider_backend": _bounded_text(
                raw_receipt.get("provider_backend")
                or output.get("provider_backend")
                or "1min",
                limit=80,
            ),
            "provider_account_name": provider_account,
            "provider_key_slot": provider_slot,
            "model": _bounded_text(result.model_name or output.get("model"), limit=120),
            "tokens_in": max(0, int(result.tokens_in or 0)),
            "tokens_out": max(0, int(result.tokens_out or 0)),
            "manager_routed": True,
            "input_digest": input_digest,
            "evaluated_at": evaluated_at,
        },
        "error": {},
    }


def _google_maps_host(value: object) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
    except ValueError:
        return False
    host = str(parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "google.com" or host.endswith(".google.com")
    ) and parsed.path.startswith("/maps")


def _google_maps_query_url(
    *,
    latitude: float,
    longitude: float,
    destination: str,
    travel_mode: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "api": "1",
            "origin": f"{latitude:.8f},{longitude:.8f}",
            "destination": destination,
            "travelmode": travel_mode,
        }
    )
    return f"https://www.google.com/maps/dir/?{query}"


def _distance_m(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> int:
    radius_m = 6_371_000.0
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = math.radians(latitude_b - latitude_a)
    delta_lon = math.radians(longitude_b - longitude_a)
    arc = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2.0) ** 2
    )
    return int(
        round(
            2.0
            * radius_m
            * math.atan2(math.sqrt(arc), math.sqrt(max(0.0, 1.0 - arc)))
        )
    )


def _classification_for_google_result(
    *,
    spec: Mapping[str, object],
    category: str,
    place_name: str,
) -> dict[str, str]:
    haystack = f"{category} {place_name}".casefold()
    search_label = str(spec.get("search_label") or "").strip().casefold()
    aliases = {
        "underground": ("subway", "metro", "u-bahn", "underground"),
        "medical care": ("doctor", "clinic", "hospital", "medical"),
        "shopping center": ("shopping centre", "shopping center", "mall"),
        "hardware store": ("hardware", "baumarkt", "do it yourself"),
        "fitness center": ("fitness", "gym"),
        "good cafe": ("cafe", "coffee"),
    }
    markers = aliases.get(search_label, (search_label,))
    if not search_label or not any(marker and marker in haystack for marker in markers):
        return {}
    for raw_criterion in tuple(spec.get("poi_keys") or ()):
        criterion = str(raw_criterion or "").strip().lower()
        if "=" not in criterion or "~=" in criterion:
            continue
        key, value = criterion.split("=", 1)
        if key and value:
            return {key: value}
    return {}


def _browser_receipt(
    *,
    action: Mapping[str, object],
    query_url: str,
    final_url: str,
    place_name: str,
    evidence_text: str,
    status: str,
    blockers: Sequence[str] = (),
) -> dict[str, object]:
    completed = []
    if status == "verified":
        completed = [
            "opened coordinate-bound Google Maps query",
            "verified visible place identity and category",
            "captured destination coordinates and final review URL",
        ]
    return {
        "site": "google.com",
        "account_ref": "ea-governed-browser-binding",
        "work_type": "research",
        "task_summary": (
            f"Verify {str(action.get('label') or action.get('fact_key') or 'distance')} "
            "for the exact property coordinates."
        ),
        "requested_actions": [
            "open exact Google Maps query",
            "inspect the nearest matching place",
            "capture visible category, coordinates, and final URL",
        ],
        "completed_actions": completed,
        "context_used": [
            f"fact_key:{str(action.get('fact_key') or '')}",
            f"travel_mode:{str(action.get('travel_mode') or 'walking')}",
        ],
        "quality_gate": (
            f"pass: {place_name} matched the requested place category"
            if status == "verified"
            else "fail: exact place evidence was not verified"
        ),
        "staged_items": [],
        "final_surface_url": final_url,
        "total_visible": "",
        "notification_policy": "action_required_only",
        "stop_condition": (
            "comparison_ready_for_user_decision"
            if status == "verified"
            else "quality_gate_failed"
        ),
        "irreversible_actions_attempted": [],
        "blockers": list(blockers),
        "evidence": {
            "query_url": query_url,
            "visible_text_sha256": (
                "sha256:"
                + hashlib.sha256(evidence_text.encode("utf-8")).hexdigest()
                if evidence_text
                else ""
            ),
        },
    }


def run_property_google_maps_ooda(
    *,
    tool_execution: object,
    principal_id: str,
    run_id: str,
    candidate_ref: str,
    property_url: str,
    facts: Mapping[str, object],
    plan: Sequence[Mapping[str, object]],
    specs: Sequence[Mapping[str, object]],
    evaluation: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    packet = dict(evaluation or {})
    ooda = dict(packet.get("ooda") or {})
    actions = [
        dict(row)
        for row in list(ooda.get("actions") or [])[:2]
        if isinstance(row, Mapping)
    ]
    if (
        not property_google_maps_ooda_enabled()
        or str(packet.get("status") or "") != "succeeded"
        or not actions
    ):
        return {}, packet
    latitude = _finite_float(facts.get("map_lat"), minimum=-90.0, maximum=90.0)
    longitude = _finite_float(
        facts.get("map_lng"), minimum=-180.0, maximum=180.0
    )
    precision = str(facts.get("map_location_precision") or "").strip().lower()
    if latitude is None or longitude is None or precision not in {
        "address",
        "building",
        "entrance",
        "rooftop",
    }:
        ooda["phase"] = "orient"
        ooda["actions"] = [
            {
                **action,
                "status": "blocked",
                "blockers": ["exact_listing_coordinates_required"],
            }
            for action in actions
        ]
        packet["ooda"] = ooda
        return {}, packet
    plan_by_key = {
        str(row.get("key") or "").strip(): dict(row)
        for row in plan
        if str(row.get("key") or "").strip()
    }
    specs_by_key = {
        str(spec.get("key") or "").strip(): dict(spec)
        for spec in specs
        if str(spec.get("key") or "").strip()
    }
    research: dict[str, object] = {}
    research_evidence: dict[str, object] = {}
    completed_actions: list[dict[str, object]] = []
    max_actions_raw = str(
        os.getenv("PROPERTYQUARRY_ONEMIN_GOOGLE_MAPS_MAX_ACTIONS") or "2"
    ).strip()
    try:
        max_actions = max(1, min(int(max_actions_raw), 2))
    except (TypeError, ValueError):
        max_actions = 2
    for action in actions[:max_actions]:
        fact_key = str(action.get("fact_key") or "").strip()
        row = plan_by_key.get(fact_key, {})
        spec = specs_by_key.get(fact_key, {})
        if (
            not row
            or not spec
            or str(row.get("priority") or "lazy").strip().lower() != "lazy"
            or str(row.get("state") or "unknown").strip().lower() == "resolved"
        ):
            completed_actions.append(
                {**action, "status": "skipped", "blockers": ["action_no_longer_needed"]}
            )
            continue
        search_label = _bounded_text(spec.get("search_label"), limit=80)
        travel_mode = str(action.get("travel_mode") or "walking").strip().lower()
        if travel_mode not in _TRAVEL_MODES:
            travel_mode = "walking"
        destination = f"nearest {search_label} near {latitude:.8f},{longitude:.8f}"
        query_url = _google_maps_query_url(
            latitude=latitude,
            longitude=longitude,
            destination=destination,
            travel_mode=travel_mode,
        )
        payload: dict[str, object] = {
            "service_name": "google_maps_distance_research",
            "requested_fields": [
                "fact_key",
                "place_name",
                "place_category",
                "place_id",
                "destination_latitude",
                "destination_longitude",
                "final_surface_url",
                "visible_text",
            ],
            "instructions": (
                "Open the supplied Google Maps directions URL. For the nearest "
                f"visible place matching {search_label!r}, return the exact fact_key, "
                "place name, visible Maps category, Google place id, destination "
                "coordinates, final Google Maps URL, and compact visible evidence text. "
                "Do not estimate or infer missing fields. Do not click any irreversible control."
            ),
            "account_hints_json": {
                "query_url": query_url,
                "fact_key": fact_key,
                "listing_latitude": latitude,
                "listing_longitude": longitude,
                "travel_mode": travel_mode,
            },
        }
        run_url = str(
            os.getenv("PROPERTYQUARRY_GOOGLE_MAPS_BROWSERACT_RUN_URL") or ""
        ).strip()
        binding_id = str(
            os.getenv("PROPERTYQUARRY_GOOGLE_MAPS_BROWSERACT_BINDING_ID") or ""
        ).strip()
        if run_url:
            payload["run_url"] = run_url
        if binding_id:
            payload["binding_id"] = binding_id
        try:
            result = tool_execution.execute_invocation(
                ToolInvocationRequest(
                    session_id=f"property-maps:{run_id}"[:160],
                    step_id=f"maps:{candidate_ref}:{fact_key}"[:160],
                    tool_name="provider.browseract.account_facts",
                    action_kind="property.fact.research",
                    payload_json=payload,
                    context_json={
                        "principal_id": str(principal_id or "").strip(),
                        "suppress_telegram_delivery": True,
                    },
                )
            )
        except Exception:
            browser_receipt = _browser_receipt(
                action=action,
                query_url=query_url,
                final_url="",
                place_name="",
                evidence_text="",
                status="unavailable",
                blockers=("browser_runtime_unavailable",),
            )
            completed_actions.append(
                {
                    **action,
                    "status": "unavailable",
                    "blockers": ["browser_runtime_unavailable"],
                    "browser_receipt": browser_receipt,
                }
            )
            continue
        output = dict(result.output_json or {})
        found = (
            dict(output.get("facts_json") or {})
            if isinstance(output.get("facts_json"), Mapping)
            else {}
        )
        place_name = _bounded_text(found.get("place_name"), limit=160)
        category = _bounded_text(found.get("place_category"), limit=160)
        place_id = _bounded_text(found.get("place_id"), limit=120)
        poi_latitude = _finite_float(
            found.get("destination_latitude"), minimum=-90.0, maximum=90.0
        )
        poi_longitude = _finite_float(
            found.get("destination_longitude"), minimum=-180.0, maximum=180.0
        )
        final_url = _bounded_text(found.get("final_surface_url"), limit=1800)
        visible_text = _bounded_text(found.get("visible_text"), limit=1200)
        classification = _classification_for_google_result(
            spec=spec,
            category=category,
            place_name=place_name,
        )
        exact_key = str(found.get("fact_key") or "").strip() == fact_key
        receipt_binds_place = bool(place_id and place_id in urllib.parse.unquote(final_url))
        valid = bool(
            exact_key
            and place_name
            and classification
            and poi_latitude is not None
            and poi_longitude is not None
            and _google_maps_host(final_url)
            and receipt_binds_place
        )
        if not valid:
            browser_receipt = _browser_receipt(
                action=action,
                query_url=query_url,
                final_url=final_url,
                place_name=place_name,
                evidence_text=visible_text,
                status="unavailable",
                blockers=("candidate_quality_failed",),
            )
            completed_actions.append(
                {
                    **action,
                    "status": "unavailable",
                    "blockers": ["candidate_quality_failed"],
                    "browser_receipt": browser_receipt,
                }
            )
            continue
        observed_at = datetime.now(timezone.utc)
        expires_at = observed_at + timedelta(hours=24)
        observed_distance = _distance_m(
            latitude, longitude, float(poi_latitude), float(poi_longitude)
        )
        if observed_distance <= 0:
            completed_actions.append(
                {**action, "status": "unavailable", "blockers": ["candidate_quality_failed"]}
            )
            continue
        evidence: dict[str, object] = {
            "provider": "google_maps_browseract",
            "method": "straight_line_google_maps",
            "observed_at": observed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "freshness": "fresh",
            "confidence": 0.9,
            "source_key": fact_key,
            "observed_key": fact_key,
            "listing_url": urllib.parse.urldefrag(str(property_url or "").strip())[0],
            "source_fingerprint": property_fact_source_fingerprint(property_url),
            "coordinate_basis": "candidate_listing_coordinates",
            "coordinate_observed_at": observed_at.isoformat(),
            "coordinate_precision": precision,
            "coordinate_source": str(
                facts.get("map_location_source") or "listing"
            ).strip()[:120],
            "coordinate_exact": True,
            "listing_latitude": latitude,
            "listing_longitude": longitude,
            "coordinate_digest": property_fact_coordinate_digest(latitude, longitude),
            "query_url": query_url,
            "query_digest": "sha256:"
            + hashlib.sha256(query_url.encode("utf-8")).hexdigest(),
            "query_schema": PROPERTY_GOOGLE_MAPS_RESEARCH_SCHEMA_VERSION,
            "receipt_url": final_url,
            "provider_object_id": place_id,
            "provider_object_type": "place",
            "provider_object_version": "1",
            "provider_object_timestamp": observed_at.isoformat(),
            "provider_object_changeset": "",
            "provider_observed_at": observed_at.isoformat(),
            "provider_expires_at": expires_at.isoformat(),
            "poi_latitude": poi_latitude,
            "poi_longitude": poi_longitude,
            "poi_classification_tags": classification,
            "attestation_version": PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION,
        }
        evidence["provider_attestation"] = property_fact_issue_provider_attestation(
            evidence,
            observed_value=observed_distance,
        )
        research[fact_key] = observed_distance
        research[f"{fact_key.removesuffix('_m')}_name"] = place_name
        research[f"{fact_key.removesuffix('_m')}_source"] = "Google Maps"
        research_evidence[fact_key] = evidence
        browser_receipt = _browser_receipt(
            action=action,
            query_url=query_url,
            final_url=final_url,
            place_name=place_name,
            evidence_text=visible_text,
            status="verified",
        )
        completed_actions.append(
            {
                **action,
                "status": "verified",
                "observed_distance_m": observed_distance,
                "place_name": place_name,
                "provider_receipt": {
                    "provider": "google_maps_browseract",
                    "query_digest": evidence["query_digest"],
                    "receipt_url": final_url,
                    "provider_object_id": place_id,
                    "coordinate_digest": evidence["coordinate_digest"],
                },
                "browser_receipt": browser_receipt,
            }
        )
    if research_evidence:
        research["property_fact_geo_evidence"] = research_evidence
    ooda["phase"] = (
        "act"
        if any(str(row.get("status") or "") == "verified" for row in completed_actions)
        else "orient"
    )
    ooda["actions"] = completed_actions
    packet["ooda"] = ooda
    return research, packet


def property_onemin_safe_public_packet(value: object) -> dict[str, object]:
    packet = dict(value or {}) if isinstance(value, Mapping) else {}
    status = str(packet.get("status") or "unavailable").strip().lower()
    if status not in {"disabled", "unavailable", "succeeded"}:
        status = "unavailable"
    judgment = (
        dict(packet.get("judgment") or {})
        if isinstance(packet.get("judgment"), Mapping)
        else {}
    )
    recommendation = str(judgment.get("recommendation") or "").strip().lower()
    if recommendation not in _RECOMMENDATIONS:
        recommendation = ""
    confidence = _finite_float(
        judgment.get("confidence"), minimum=0.0, maximum=1.0
    )
    ooda = (
        dict(packet.get("ooda") or {})
        if isinstance(packet.get("ooda"), Mapping)
        else {}
    )
    safe_actions: list[dict[str, object]] = []
    for raw_action in list(ooda.get("actions") or [])[:2]:
        if not isinstance(raw_action, Mapping):
            continue
        browser_receipt = (
            dict(raw_action.get("browser_receipt") or {})
            if isinstance(raw_action.get("browser_receipt"), Mapping)
            else {}
        )
        provider_receipt = (
            dict(raw_action.get("provider_receipt") or {})
            if isinstance(raw_action.get("provider_receipt"), Mapping)
            else {}
        )
        safe_actions.append(
            {
                "action_id": _bounded_text(raw_action.get("action_id"), limit=40),
                "fact_key": _bounded_text(raw_action.get("fact_key"), limit=80),
                "label": _bounded_text(raw_action.get("label"), limit=120),
                "provider": "google_maps_browseract",
                "work_type": "research",
                "reason": _bounded_text(raw_action.get("reason"), limit=240),
                "travel_mode": (
                    str(raw_action.get("travel_mode") or "walking").strip().lower()
                    if str(raw_action.get("travel_mode") or "walking")
                    .strip()
                    .lower()
                    in _TRAVEL_MODES
                    else "walking"
                ),
                "priority": _bounded_int(
                    raw_action.get("priority"),
                    default=1,
                    minimum=1,
                    maximum=9,
                ),
                "status": (
                    str(raw_action.get("status") or "planned").strip().lower()
                    if str(raw_action.get("status") or "planned").strip().lower()
                    in {"planned", "verified", "unavailable", "blocked", "skipped"}
                    else "unavailable"
                ),
                "observed_distance_m": (
                    _bounded_int(
                        raw_action.get("observed_distance_m"),
                        default=1,
                        minimum=1,
                        maximum=5_000_000,
                    )
                    if raw_action.get("observed_distance_m") not in (None, "", 0)
                    else None
                ),
                "place_name": _bounded_text(raw_action.get("place_name"), limit=160),
                "blockers": _bounded_string_list(
                    raw_action.get("blockers"), limit=6
                ),
                "provider_receipt": {
                    "provider": _bounded_text(
                        provider_receipt.get("provider"), limit=80
                    ),
                    "query_digest": _bounded_text(
                        provider_receipt.get("query_digest"), limit=80
                    ),
                    "receipt_url": _bounded_text(
                        provider_receipt.get("receipt_url"), limit=1800
                    ),
                    "provider_object_id": _bounded_text(
                        provider_receipt.get("provider_object_id"), limit=120
                    ),
                    "coordinate_digest": _bounded_text(
                        provider_receipt.get("coordinate_digest"), limit=80
                    ),
                },
                "browser_receipt": {
                    "site": _bounded_text(browser_receipt.get("site"), limit=120),
                    "account_ref": _bounded_text(
                        browser_receipt.get("account_ref"), limit=160
                    ),
                    "work_type": "research",
                    "task_summary": _bounded_text(
                        browser_receipt.get("task_summary"), limit=300
                    ),
                    "quality_gate": _bounded_text(
                        browser_receipt.get("quality_gate"), limit=300
                    ),
                    "requested_actions": _bounded_string_list(
                        browser_receipt.get("requested_actions"), limit=6
                    ),
                    "completed_actions": _bounded_string_list(
                        browser_receipt.get("completed_actions"), limit=6
                    ),
                    "context_used": _bounded_string_list(
                        browser_receipt.get("context_used"), limit=6
                    ),
                    "staged_items": _bounded_string_list(
                        browser_receipt.get("staged_items"), limit=6
                    ),
                    "final_surface_url": _bounded_text(
                        browser_receipt.get("final_surface_url"), limit=1800
                    ),
                    "total_visible": _bounded_text(
                        browser_receipt.get("total_visible"), limit=80
                    ),
                    "notification_policy": "action_required_only",
                    "stop_condition": _bounded_text(
                        browser_receipt.get("stop_condition"), limit=80
                    ),
                    "irreversible_actions_attempted": [],
                    "blockers": _bounded_string_list(
                        browser_receipt.get("blockers"), limit=6
                    ),
                    "evidence": {
                        "query_url": _bounded_text(
                            dict(browser_receipt.get("evidence") or {}).get(
                                "query_url"
                            )
                            if isinstance(browser_receipt.get("evidence"), Mapping)
                            else "",
                            limit=1800,
                        ),
                        "visible_text_sha256": _bounded_text(
                            dict(browser_receipt.get("evidence") or {}).get(
                                "visible_text_sha256"
                            )
                            if isinstance(browser_receipt.get("evidence"), Mapping)
                            else "",
                            limit=80,
                        ),
                    },
                },
            }
        )
    receipt = (
        dict(packet.get("receipt") or {})
        if isinstance(packet.get("receipt"), Mapping)
        else {}
    )
    error = (
        dict(packet.get("error") or {})
        if isinstance(packet.get("error"), Mapping)
        else {}
    )
    return {
        "schema_version": PROPERTY_ONEMIN_EVALUATION_SCHEMA_VERSION,
        "status": status,
        "input_digest": (
            str(packet.get("input_digest") or "").strip()
            if re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(packet.get("input_digest") or "").strip(),
            )
            else property_fact_input_digest({})
        ),
        "manager_routed": packet.get("manager_routed") is True,
        "cache_hit": packet.get("cache_hit") is True,
        "evaluated_at": _bounded_text(packet.get("evaluated_at"), limit=64),
        "judgment": {
            "recommendation": recommendation,
            "confidence": confidence if confidence is not None else 0.0,
            "summary": _bounded_text(judgment.get("summary"), limit=600),
            "strengths": _bounded_string_list(judgment.get("strengths")),
            "risks": _bounded_string_list(judgment.get("risks")),
            "evidence_keys": [
                re.sub(r"[^a-z0-9_]", "_", key.lower())[:80]
                for key in _bounded_string_list(judgment.get("evidence_keys"), limit=12)
            ],
            "missing_fact_keys": [
                re.sub(r"[^a-z0-9_]", "_", key.lower())[:80]
                for key in _bounded_string_list(
                    judgment.get("missing_fact_keys"), limit=12
                )
            ],
        },
        "ooda": {
            "schema_version": PROPERTY_ONEMIN_OODA_SCHEMA_VERSION,
            "phase": (
                str(ooda.get("phase") or "observe").strip().lower()
                if str(ooda.get("phase") or "observe").strip().lower()
                in {"observe", "orient", "decide", "act"}
                else "observe"
            ),
            "actions": safe_actions,
        },
        "receipt": {
            "tool_name": "provider.onemin.code_generate",
            "action_kind": "property.evaluate",
            "provider": "1minAI",
            "provider_backend": _bounded_text(
                receipt.get("provider_backend"), limit=80
            ),
            "provider_account_name": _bounded_text(
                receipt.get("provider_account_name"), limit=120
            ),
            "provider_key_slot": _bounded_text(
                receipt.get("provider_key_slot"), limit=80
            ),
            "model": _bounded_text(receipt.get("model"), limit=120),
            "tokens_in": _bounded_int(
                receipt.get("tokens_in"),
                default=0,
                minimum=0,
                maximum=10_000_000,
            ),
            "tokens_out": _bounded_int(
                receipt.get("tokens_out"),
                default=0,
                minimum=0,
                maximum=10_000_000,
            ),
            "manager_routed": receipt.get("manager_routed") is True,
            "input_digest": _bounded_text(
                receipt.get("input_digest"), limit=80
            ),
            "evaluated_at": _bounded_text(
                receipt.get("evaluated_at"), limit=64
            ),
        },
        "error": {
            "code": _safe_failure_code(error.get("code")) if error else "",
        },
    }
