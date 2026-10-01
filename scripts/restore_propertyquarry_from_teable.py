from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ea"))

from app.repositories.onboarding_state_postgres import PostgresOnboardingStateRepository  # noqa: E402
from app.services.property_billing import normalize_property_commercial  # noqa: E402
from app.services.propertyquarry_teable_projection import (  # noqa: E402
    PROPERTYQUARRY_TEABLE_TABLE_NAMES,
    discover_propertyquarry_teable_table_config,
)


TEABLE_RESTORE_CONTRACT_VERSION = "propertyquarry.teable_restore_coverage.v1"
SUPPORTED_PROPERTY_NOTIFICATION_CHANNELS = {"email", "telegram", "whatsapp"}

RECOVERABLE_TEABLE_TABLES: tuple[str, ...] = (
    "propertyquarry_users",
    "propertyquarry_delivery_settings",
    "propertyquarry_subscriptions",
    "propertyquarry_preferences",
    "propertyquarry_search_agents",
    "propertyquarry_saved_shortlist",
    "propertyquarry_properties",
    "propertyquarry_property_evaluations",
    "propertyquarry_review_artifacts",
    "propertyquarry_shared_artifacts",
    "propertyquarry_research_tasks",
    "propertyquarry_decision_ledger",
    "propertyquarry_evidence_claims",
    "propertyquarry_agent_questions",
    "propertyquarry_documents",
)

INTENTIONALLY_LOSSY_TEABLE_TABLES: dict[str, str] = {
    "propertyquarry_tenants": "tenant metadata is recreated from the new host configuration",
    "propertyquarry_search_runs": "runs can be lost; saved results and review/share artifacts are restored",
    "propertyquarry_provider_sources": "provider crawl/source diagnostics are run-scoped and disposable",
}


def _jsonable(value: object) -> object:
    if isinstance(value, str):
        text = value.strip()
        if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
            try:
                return json.loads(text)
            except Exception:
                return value
    return value


def _table_config_from_env(*, base_url: str, api_key: str, base_id: str, base_name: str) -> dict[str, dict[str, object]]:
    raw = str(os.environ.get("PROPERTYQUARRY_TEABLE_TABLE_SYNC_CONFIG_JSON") or "").strip()
    if not raw:
        raw = str(os.environ.get("TEABLE_TABLE_SYNC_CONFIG_JSON") or "").strip()
    if not raw:
        discovered = discover_propertyquarry_teable_table_config(
            base_url=base_url,
            api_key=api_key,
            base_id=base_id,
            base_name=base_name,
        )
        if discovered:
            return discovered
        raise SystemExit("missing PROPERTYQUARRY_TEABLE_TABLE_SYNC_CONFIG_JSON or PROPERTYQUARRY_TEABLE_BASE_ID")
    try:
        loaded = json.loads(raw)
    except Exception as exc:
        raise SystemExit("invalid PROPERTYQUARRY_TEABLE_TABLE_SYNC_CONFIG_JSON") from exc
    if not isinstance(loaded, dict):
        raise SystemExit("invalid PROPERTYQUARRY_TEABLE_TABLE_SYNC_CONFIG_JSON")
    return {
        str(table_name or "").strip(): dict(config or {})
        for table_name, config in loaded.items()
        if str(table_name or "").strip() and isinstance(config, dict)
    }


def _request_json(*, method: str, url: str, api_key: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "PropertyQuarryTeableRestore/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = str(exc)[:500]
        raise SystemExit(f"Teable HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise SystemExit(f"Teable request failed: {str(exc)[:500]}") from exc
    if not payload.strip():
        return {}
    try:
        loaded = json.loads(payload)
    except Exception as exc:
        raise SystemExit("Teable returned invalid JSON") from exc
    return dict(loaded or {})


def fetch_teable_projection_records(
    *,
    base_url: str,
    api_key: str,
    table_config: dict[str, dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    records_by_table: dict[str, list[dict[str, object]]] = {}
    normalized_base = str(base_url or "https://app.teable.ai").strip().rstrip("/")
    for table_name in PROPERTYQUARRY_TEABLE_TABLE_NAMES:
        config = dict(table_config.get(table_name) or {})
        table_id = str(config.get("table_id") or "").strip()
        field_key_type = str(config.get("field_key_type") or "name").strip() or "name"
        if not table_id:
            records_by_table[table_name] = []
            continue
        rows: list[dict[str, object]] = []
        skip = 0
        take = 1000
        while True:
            query = urllib.parse.urlencode(
                {
                    "fieldKeyType": field_key_type,
                    "cellFormat": "json",
                    "take": take,
                    "skip": skip,
                }
            )
            payload = _request_json(
                method="GET",
                url=f"{normalized_base}/api/table/{urllib.parse.quote(table_id)}/record?{query}",
                api_key=api_key,
            )
            records = [dict(item) for item in payload.get("records") or [] if isinstance(item, dict)]
            for record in records:
                fields = {
                    str(key or "").strip(): _jsonable(value)
                    for key, value in dict(record.get("fields") or {}).items()
                    if str(key or "").strip()
                }
                if fields:
                    rows.append(fields)
            if len(records) < take:
                break
            skip += take
        records_by_table[table_name] = rows
    return records_by_table


def _first_row(rows: list[dict[str, object]], *, principal_id: str = "") -> dict[str, object]:
    for row in rows:
        if principal_id and str(row.get("principal_id") or "").strip() != principal_id:
            continue
        return dict(row)
    return {}


def _coerce_list(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        parsed = _jsonable(value)
        if isinstance(parsed, list):
            return parsed
        if value.strip():
            return [value.strip()]
    return []


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    parsed = _jsonable(value)
    if isinstance(parsed, dict):
        return dict(parsed)
    return {}


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "disabled", ""}:
        return False
    return False


def _stable_ref(value: object, *, prefix: str) -> str:
    raw = str(value or "").strip()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _projection_alias(value: object, *, prefix: str) -> str:
    raw = str(value or "").strip() or f"{prefix}:unknown"
    return _stable_ref(raw, prefix=prefix)


def _principal_tokens(principal_id: str) -> set[str]:
    normalized = str(principal_id or "").strip()
    if not normalized:
        return set()
    return {normalized, _projection_alias(normalized, prefix="principal")}


def _matches_principal(row: dict[str, object], *, principal_id: str) -> bool:
    tokens = _principal_tokens(principal_id)
    if not tokens:
        return False
    row_principal = str(row.get("principal_id") or "").strip()
    return row_principal in tokens


def _restored_person_id(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("person:"):
        return "self"
    return text


def _created_at(value: object) -> str:
    text = str(value or "").strip()
    if text:
        return text
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _subscription_commercial_from_rows(
    *,
    principal_id: str,
    records_by_table: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    row = _first_row(records_by_table.get("propertyquarry_subscriptions", []), principal_id=principal_id)
    if not row:
        return {}
    commercial = _coerce_dict(row.get("commercial_json"))
    current_plan_key = str(row.get("current_plan_key") or commercial.get("active_plan_key") or "").strip().lower()
    if current_plan_key:
        commercial["active_plan_key"] = current_plan_key
    for source_key, target_key in (
        ("status", "status"),
        ("active_until", "active_until"),
        ("pending_plan_key", "pending_plan_key"),
        ("plan_source", "plan_source"),
        ("last_order_id", "last_order_id"),
        ("last_capture_id", "last_capture_id"),
        ("last_payment_status", "last_payment_status"),
        ("last_payment_amount_eur", "last_payment_amount_eur"),
        ("captured_at", "captured_at"),
    ):
        value = row.get(source_key)
        if value not in (None, ""):
            commercial[target_key] = value
    return normalize_property_commercial(commercial)


def _lookup_keys_for_row(row: dict[str, object]) -> tuple[str, ...]:
    keys: list[str] = []
    for field in ("property_ref", "property_url"):
        value = str(row.get(field) or "").strip()
        if value:
            keys.append(f"{field}:{value}")
    return tuple(dict.fromkeys(keys))


def _review_artifact_index(
    *,
    principal_id: str,
    records_by_table: dict[str, list[dict[str, object]]],
) -> dict[str, dict[str, object]]:
    index: dict[str, dict[str, object]] = {}
    for row in records_by_table.get("propertyquarry_review_artifacts", []):
        if not _matches_principal(row, principal_id=principal_id):
            continue
        artifact = dict(row)
        for key in _lookup_keys_for_row(artifact):
            existing = index.get(key, {})
            if not existing or str(artifact.get("review_url") or artifact.get("tour_url") or "").strip():
                index[key] = artifact
    return index


def _shared_artifact_index(
    *,
    principal_id: str,
    records_by_table: dict[str, list[dict[str, object]]],
) -> dict[str, dict[str, dict[str, object]]]:
    index: dict[str, dict[str, dict[str, object]]] = {}
    for row in records_by_table.get("propertyquarry_shared_artifacts", []):
        if not _matches_principal(row, principal_id=principal_id):
            continue
        artifact = dict(row)
        artifact_kind = str(artifact.get("artifact_kind") or "").strip()
        artifact_url = str(artifact.get("artifact_url") or "").strip()
        if not artifact_kind or not artifact_url:
            continue
        for key in _lookup_keys_for_row(artifact):
            index.setdefault(key, {})[artifact_kind] = artifact
    return index


def _research_tasks_from_rows(
    *,
    principal_id: str,
    records_by_table: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in records_by_table.get("propertyquarry_research_tasks", []):
        if not _matches_principal(row, principal_id=principal_id):
            continue
        task_id = str(row.get("task_id") or "").strip()
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        task_json = _coerce_dict(row.get("task_json"))
        task = {
            "task_id": task_id,
            "status": str(row.get("status") or task_json.get("status") or "").strip(),
            "field_key": str(row.get("field_key") or task_json.get("field_key") or task_json.get("key") or "").strip(),
            "label": str(row.get("label") or task_json.get("label") or "").strip(),
            "question": str(row.get("question") or task_json.get("question") or "").strip(),
            "property_ref": str(row.get("property_ref") or task_json.get("property_ref") or "").strip(),
            "property_url": str(row.get("property_url") or task_json.get("property_url") or "").strip(),
            "value": str(row.get("value") or task_json.get("value") or "").strip(),
            "note": str(row.get("note") or task_json.get("note") or "").strip(),
            "run_id": str(row.get("run_id") or task_json.get("run_id") or "").strip(),
            "task_json": task_json,
        }
        for key, value in task_json.items():
            task.setdefault(str(key), value)
        tasks.append(task)
    tasks.sort(key=lambda item: (str(item.get("status") or ""), str(item.get("task_id") or "")))
    return tasks[:500]


def _research_task_index(tasks: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    index: dict[str, list[dict[str, object]]] = {}
    for task in tasks:
        for key in _lookup_keys_for_row(task):
            index.setdefault(key, []).append(task)
    return index


def _merge_candidate_artifacts(
    candidate: dict[str, object],
    *,
    review_artifacts: dict[str, dict[str, object]],
    shared_artifacts: dict[str, dict[str, dict[str, object]]] | None = None,
    research_tasks: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    restored = dict(candidate)
    shared_artifacts = dict(shared_artifacts or {})
    lookup_keys = _lookup_keys_for_row(restored)
    artifact: dict[str, object] = {}
    for key in lookup_keys:
        artifact = review_artifacts.get(key, {})
        if artifact:
            break
    if artifact:
        artifact_json = _coerce_dict(artifact.get("artifact_json"))
        for key, value in artifact_json.items():
            restored.setdefault(str(key), value)
        for source_key, target_key in (
            ("review_url", "review_url"),
            ("review_status", "review_status"),
            ("review_task_id", "review_task_id"),
            ("review_task_status", "review_task_status"),
            ("review_reused", "review_reused"),
            ("queue_item_ref", "queue_item_ref"),
            ("recommended_task_key", "recommended_task_key"),
            ("tour_url", "tour_url"),
            ("tour_status", "tour_status"),
            ("tour_blocked_reason", "tour_blocked_reason"),
            ("preference_person_id", "preference_person_id"),
        ):
            value = artifact.get(source_key)
            if value not in (None, "") and not restored.get(target_key):
                restored[target_key] = value
        if restored.get("tour_blocked_reason") and not restored.get("blocked_reason"):
            restored["blocked_reason"] = restored["tour_blocked_reason"]
    artifact_targets = {
        "review": ("review_url", "review_status"),
        "packet": ("packet_url", "packet_status"),
        "public_packet": ("public_packet_url", "packet_status"),
        "tour": ("tour_url", "tour_status"),
        "walkthrough": ("walkthrough_url", "walkthrough_status"),
        "video": ("video_url", "video_status"),
    }
    for key in lookup_keys:
        for artifact_kind, shared in shared_artifacts.get(key, {}).items():
            target_url, target_status = artifact_targets.get(str(artifact_kind or ""), ("", ""))
            if not target_url:
                continue
            artifact_url = str(shared.get("artifact_url") or "").strip()
            if artifact_url and not restored.get(target_url):
                restored[target_url] = artifact_url
            artifact_status = str(shared.get("artifact_status") or "").strip()
            if artifact_status and not restored.get(target_status):
                restored[target_status] = artifact_status
    attached_tasks: list[dict[str, object]] = []
    seen_task_ids: set[str] = set()
    for key in lookup_keys:
        for task in research_tasks.get(key, []):
            task_id = str(task.get("task_id") or "").strip()
            if not task_id or task_id in seen_task_ids:
                continue
            seen_task_ids.add(task_id)
            attached_tasks.append(dict(task))
    if attached_tasks and not isinstance(restored.get("research_tasks"), list):
        restored["research_tasks"] = attached_tasks[:50]
        restored["research_task_total"] = len(attached_tasks)
        restored["open_research_task_total"] = sum(
            1
            for task in attached_tasks
            if str(task.get("status") or "").strip().lower() not in {"done", "filled", "resolved", "dismissed"}
        )
    return restored


def _saved_candidates_from_evaluations(
    *,
    principal_id: str,
    records_by_table: dict[str, list[dict[str, object]]],
    review_artifacts: dict[str, dict[str, object]] | None = None,
    shared_artifacts: dict[str, dict[str, dict[str, object]]] | None = None,
    research_tasks: dict[str, list[dict[str, object]]] | None = None,
) -> list[dict[str, object]]:
    review_artifacts = dict(review_artifacts or {})
    shared_artifacts = dict(shared_artifacts or {})
    research_tasks = dict(research_tasks or {})
    properties = {
        str(row.get("property_ref") or "").strip(): dict(row)
        for row in records_by_table.get("propertyquarry_properties", [])
        if str(row.get("property_ref") or "").strip()
    }
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in records_by_table.get("propertyquarry_property_evaluations", []):
        if not _matches_principal(row, principal_id=principal_id):
            continue
        property_ref = str(row.get("property_ref") or "").strip()
        if not property_ref or property_ref in seen:
            continue
        seen.add(property_ref)
        property_row = properties.get(property_ref, {})
        facts = _coerce_dict(row.get("facts_json") or property_row.get("facts_json"))
        candidate = {
            "property_ref": property_ref,
            "candidate_ref": property_ref,
            "property_url": str(row.get("property_url") or property_row.get("property_url") or "").strip(),
            "listing_id": str(property_row.get("listing_id") or "").strip(),
            "title": str(property_row.get("title") or "").strip(),
            "source_label": str(row.get("source_label") or property_row.get("source_label") or "").strip(),
            "fit_score": row.get("fit_score"),
            "recommendation": str(row.get("recommendation") or "").strip(),
            "fit_summary": str(row.get("fit_summary") or "").strip(),
            "review_url": str(row.get("review_url") or "").strip(),
            "tour_url": str(row.get("tour_url") or "").strip(),
            "tour_status": str(row.get("tour_status") or "").strip(),
            "saved_from_run_id": str(row.get("run_id") or "").strip(),
            "property_facts": facts,
        }
        candidate = _merge_candidate_artifacts(
            candidate,
            review_artifacts=review_artifacts,
            shared_artifacts=shared_artifacts,
            research_tasks=research_tasks,
        )
        candidates.append(candidate)
    candidates.sort(key=lambda item: float(item.get("fit_score") or 0), reverse=True)
    return candidates[:200]


def _saved_candidates_from_saved_shortlist(
    *,
    principal_id: str,
    records_by_table: dict[str, list[dict[str, object]]],
    review_artifacts: dict[str, dict[str, object]] | None = None,
    shared_artifacts: dict[str, dict[str, dict[str, object]]] | None = None,
    research_tasks: dict[str, list[dict[str, object]]] | None = None,
) -> list[dict[str, object]]:
    review_artifacts = dict(review_artifacts or {})
    shared_artifacts = dict(shared_artifacts or {})
    research_tasks = dict(research_tasks or {})
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in records_by_table.get("propertyquarry_saved_shortlist", []):
        if not _matches_principal(row, principal_id=principal_id):
            continue
        property_ref = str(row.get("property_ref") or "").strip()
        if not property_ref or property_ref in seen:
            continue
        seen.add(property_ref)
        facts = _coerce_dict(row.get("facts_json"))
        candidate = {
            "property_ref": property_ref,
            "candidate_ref": str(row.get("candidate_ref") or property_ref).strip(),
            "property_url": str(row.get("property_url") or "").strip(),
            "listing_id": str(row.get("listing_id") or "").strip(),
            "title": str(row.get("title") or "").strip(),
            "source_label": str(row.get("source_label") or "").strip(),
            "fit_score": row.get("fit_score"),
            "rank": row.get("rank"),
            "review_url": str(row.get("review_url") or "").strip(),
            "tour_url": str(row.get("tour_url") or "").strip(),
            "tour_status": str(row.get("tour_status") or "").strip(),
            "saved_from_run_id": str(row.get("saved_from_run_id") or "").strip(),
            "saved_at": str(row.get("saved_at") or "").strip(),
            "property_facts": facts,
        }
        candidate_json = _coerce_dict(row.get("candidate_json"))
        if candidate_json:
            for key, value in candidate_json.items():
                candidate.setdefault(str(key), value)
        candidate = _merge_candidate_artifacts(
            candidate,
            review_artifacts=review_artifacts,
            shared_artifacts=shared_artifacts,
            research_tasks=research_tasks,
        )
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            float(item.get("fit_score") or 0),
            -float(item.get("rank") or 9999),
        ),
        reverse=True,
    )
    return candidates[:200]


def _search_agents_from_rows(
    *,
    principal_id: str,
    records_by_table: dict[str, list[dict[str, object]]],
) -> tuple[list[dict[str, object]], str]:
    agents: list[dict[str, object]] = []
    active_agent_id = ""
    seen: set[str] = set()
    for row in records_by_table.get("propertyquarry_search_agents", []):
        if not _matches_principal(row, principal_id=principal_id):
            continue
        agent_id = str(row.get("agent_id") or "").strip()
        if not agent_id or agent_id in seen:
            continue
        seen.add(agent_id)
        preferences_json = _coerce_dict(row.get("preferences_json"))
        selected_platforms = _coerce_list(row.get("selected_platforms_json") or preferences_json.get("selected_platforms"))
        agent = {
            "agent_id": agent_id,
            "name": str(row.get("name") or preferences_json.get("name") or "Saved search").strip() or "Saved search",
            "enabled": bool(row.get("enabled", True)),
            "is_active": bool(row.get("is_active")),
            "country_code": str(row.get("country_code") or preferences_json.get("country_code") or "").strip(),
            "region_code": str(row.get("region_code") or preferences_json.get("region_code") or "").strip(),
            "location_query": str(row.get("location_query") or preferences_json.get("location_query") or "").strip(),
            "listing_mode": str(row.get("listing_mode") or preferences_json.get("listing_mode") or "").strip(),
            "property_type": str(row.get("property_type") or preferences_json.get("property_type") or "").strip(),
            "selected_platforms": [str(value or "").strip() for value in selected_platforms if str(value or "").strip()],
            "duration_days": row.get("duration_days"),
            "notification_limit": row.get("notification_limit"),
            "notification_period": str(row.get("notification_period") or "").strip(),
            "sent_in_current_window": row.get("sent_in_current_window"),
            "last_run_at": str(row.get("last_run_at") or "").strip(),
            "next_run_at": str(row.get("next_run_at") or "").strip(),
            "preferences_json": preferences_json,
        }
        for key, value in preferences_json.items():
            agent.setdefault(str(key), value)
        agents.append(agent)
        if bool(row.get("is_active")) and not active_agent_id:
            active_agent_id = agent_id
    if not active_agent_id and agents:
        active_agent_id = str(agents[0].get("agent_id") or "").strip()
        agents[0]["is_active"] = True
    return agents[:200], active_agent_id


def _decision_loop_rows_from_records(
    *,
    principal_id: str,
    records_by_table: dict[str, list[dict[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    restored: dict[str, list[dict[str, object]]] = {
        "propertyquarry_decision_ledger": [],
        "propertyquarry_evidence_claims": [],
        "propertyquarry_agent_questions": [],
        "propertyquarry_documents": [],
    }
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        return restored
    for row in records_by_table.get("propertyquarry_decision_ledger", []):
        if not _matches_principal(row, principal_id=normalized_principal):
            continue
        restored["propertyquarry_decision_ledger"].append(
            {
                "decision_id": str(row.get("decision_id") or "").strip(),
                "principal_id": normalized_principal,
                "person_id": _restored_person_id(row.get("person_id")),
                "property_ref": str(row.get("property_ref") or "").strip(),
                "decision_state": str(row.get("decision_state") or "reviewing").strip() or "reviewing",
                "reason_keys_json": _coerce_list(row.get("reason_keys_json")),
                "source": str(row.get("source") or "system").strip() or "system",
                "actor": str(row.get("actor") or "teable_restore").strip() or "teable_restore",
                "confidence": row.get("confidence") if row.get("confidence") not in (None, "") else 0.7,
                "supersedes_decision_id": str(row.get("supersedes_decision_id") or "").strip(),
                "learning_applied": bool(row.get("learning_applied")),
                "aggregate_candidate": bool(row.get("aggregate_candidate")),
                "created_at": _created_at(row.get("created_at")),
            }
        )
    for row in records_by_table.get("propertyquarry_evidence_claims", []):
        if not _matches_principal(row, principal_id=normalized_principal):
            continue
        restored["propertyquarry_evidence_claims"].append(
            {
                "claim_id": str(row.get("claim_id") or "").strip(),
                "principal_id": normalized_principal,
                "person_id": _restored_person_id(row.get("person_id")),
                "property_ref": str(row.get("property_ref") or "").strip(),
                "decision_id": str(row.get("decision_id") or "").strip(),
                "claim_type": str(row.get("claim_type") or "fact").strip() or "fact",
                "claim_text": str(row.get("claim_text") or row.get("text") or "").strip(),
                "source_type": str(row.get("source_type") or "teable_restore").strip() or "teable_restore",
                "source_ref": str(row.get("source_ref") or "").strip(),
                "confidence": str(row.get("confidence") or "medium").strip() or "medium",
                "verification_state": str(row.get("verification_state") or "unclear").strip() or "unclear",
                "privacy_class": str(row.get("privacy_class") or "owner_private").strip() or "owner_private",
                "allowed_outputs_json": _coerce_list(row.get("allowed_outputs_json")),
                "expires_at": str(row.get("expires_at") or "").strip(),
                "created_at": _created_at(row.get("created_at")),
            }
        )
    for row in records_by_table.get("propertyquarry_agent_questions", []):
        if not _matches_principal(row, principal_id=normalized_principal):
            continue
        restored["propertyquarry_agent_questions"].append(
            {
                "task_id": str(row.get("task_id") or "").strip(),
                "principal_id": normalized_principal,
                "person_id": _restored_person_id(row.get("person_id")),
                "property_ref": str(row.get("property_ref") or "").strip(),
                "decision_id": str(row.get("decision_id") or "").strip(),
                "question_text": str(row.get("question_text") or "").strip(),
                "reason_key": str(row.get("reason_key") or "").strip(),
                "source_claim_id": str(row.get("source_claim_id") or "").strip(),
                "status": str(row.get("status") or "drafted").strip() or "drafted",
                "answer_source": str(row.get("answer_source") or "").strip(),
                "updated_claim_id": str(row.get("updated_claim_id") or "").strip(),
                "created_at": _created_at(row.get("created_at")),
            }
        )
    for row in records_by_table.get("propertyquarry_documents", []):
        if not _matches_principal(row, principal_id=normalized_principal):
            continue
        restored["propertyquarry_documents"].append(
            {
                "document_id": str(row.get("document_id") or "").strip(),
                "principal_id": normalized_principal,
                "person_id": _restored_person_id(row.get("person_id")),
                "property_ref": str(row.get("property_ref") or "").strip(),
                "decision_id": str(row.get("decision_id") or "").strip(),
                "document_type": str(row.get("document_type") or "").strip(),
                "source": str(row.get("source") or "").strip(),
                "privacy_class": str(row.get("privacy_class") or "owner_private").strip() or "owner_private",
                "verification_state": str(row.get("verification_state") or "missing").strip() or "missing",
                "extracted_claims_json": _coerce_list(row.get("extracted_claims_json")),
                "missing_pages_json": _coerce_list(row.get("missing_pages_json")),
                "redaction_state": str(row.get("redaction_state") or "not_started").strip() or "not_started",
                "linked_risks_json": _coerce_list(row.get("linked_risks_json")),
                "created_at": _created_at(row.get("created_at")),
            }
        )
    identity_fields = {
        "propertyquarry_decision_ledger": "decision_id",
        "propertyquarry_evidence_claims": "claim_id",
        "propertyquarry_agent_questions": "task_id",
        "propertyquarry_documents": "document_id",
    }
    return {
        table_name: [
            row
            for row in rows
            if str(row.get(identity_fields[table_name]) or "").strip()
        ]
        for table_name, rows in restored.items()
    }


def build_restore_bundle(
    *,
    principal_id: str,
    records_by_table: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        raise ValueError("principal_id_required")
    user = _first_row(records_by_table.get("propertyquarry_users", []), principal_id=normalized_principal)
    delivery = _first_row(records_by_table.get("propertyquarry_delivery_settings", []), principal_id=normalized_principal)
    preferences_row = _first_row(records_by_table.get("propertyquarry_preferences", []), principal_id=normalized_principal)
    preferences = _coerce_dict(preferences_row.get("preferences_json"))
    preferences.setdefault("country_code", str(preferences_row.get("country_code") or "").strip())
    preferences.setdefault("listing_mode", str(preferences_row.get("listing_mode") or "").strip())
    preferences.setdefault("property_type", str(preferences_row.get("property_type") or "").strip())
    preferences.setdefault("location_query", str(preferences_row.get("location_query") or "").strip())
    if not preferences.get("selected_platforms"):
        preferences["selected_platforms"] = _coerce_list(preferences_row.get("selected_platforms_json"))
    restored_commercial = _subscription_commercial_from_rows(
        principal_id=normalized_principal,
        records_by_table=records_by_table,
    )
    if restored_commercial and not isinstance(preferences.get("property_commercial"), dict):
        preferences["property_commercial"] = restored_commercial
    restored_agents, restored_active_agent_id = _search_agents_from_rows(
        principal_id=normalized_principal,
        records_by_table=records_by_table,
    )
    if restored_agents and not isinstance(preferences.get("search_agents"), list):
        preferences["search_agents"] = restored_agents
    if restored_active_agent_id and not str(preferences.get("active_search_agent_id") or "").strip():
        preferences["active_search_agent_id"] = restored_active_agent_id
    restored_review_artifacts = _review_artifact_index(
        principal_id=normalized_principal,
        records_by_table=records_by_table,
    )
    restored_shared_artifacts = _shared_artifact_index(
        principal_id=normalized_principal,
        records_by_table=records_by_table,
    )
    restored_research_tasks = _research_tasks_from_rows(
        principal_id=normalized_principal,
        records_by_table=records_by_table,
    )
    restored_research_task_index = _research_task_index(restored_research_tasks)
    saved_candidates = list(preferences.get("saved_shortlist_candidates") or [])
    if not saved_candidates:
        saved_candidates = _saved_candidates_from_saved_shortlist(
            principal_id=normalized_principal,
            records_by_table=records_by_table,
            review_artifacts=restored_review_artifacts,
            shared_artifacts=restored_shared_artifacts,
            research_tasks=restored_research_task_index,
        )
    if not saved_candidates:
        saved_candidates = _saved_candidates_from_evaluations(
            principal_id=normalized_principal,
            records_by_table=records_by_table,
            review_artifacts=restored_review_artifacts,
            shared_artifacts=restored_shared_artifacts,
            research_tasks=restored_research_task_index,
        )
    if saved_candidates:
        preferences["saved_shortlist_candidates"] = saved_candidates
    if restored_research_tasks and not isinstance(preferences.get("restored_research_tasks"), list):
        preferences["restored_research_tasks"] = restored_research_tasks
    delivery_settings_json = _coerce_dict(delivery.get("settings_json"))
    selected_channels = [
        channel
        for value in _coerce_list(
            delivery.get("selected_channels_json")
            or delivery_settings_json.get("selected_channels")
            or user.get("selected_channels_json")
        )
        if (channel := str(value or "").strip().lower()) in SUPPORTED_PROPERTY_NOTIFICATION_CHANNELS
    ]
    preferred_channel = str(
        delivery.get("preferred_channel")
        or delivery_settings_json.get("preferred_channel")
        or "email"
    ).strip().lower() or "email"
    if preferred_channel not in SUPPORTED_PROPERTY_NOTIFICATION_CHANNELS:
        preferred_channel = "email"
    if preferred_channel and preferred_channel not in selected_channels:
        selected_channels.append(preferred_channel)
    for channel, enabled_field in (
        ("email", "email_enabled"),
        ("telegram", "telegram_enabled"),
        ("whatsapp", "whatsapp_enabled"),
    ):
        if (
            _coerce_bool(delivery.get(enabled_field))
            or _coerce_bool(delivery_settings_json.get(enabled_field))
        ) and channel not in selected_channels:
            selected_channels.append(channel)
    whatsapp_phone = str(delivery.get("whatsapp_ai_support_phone") or "").strip()
    if whatsapp_phone and "whatsapp" not in selected_channels:
        selected_channels.append("whatsapp")
    email_enabled = "email" in selected_channels or preferred_channel == "email"
    telegram_enabled = "telegram" in selected_channels or preferred_channel == "telegram"
    whatsapp_enabled = "whatsapp" in selected_channels or preferred_channel == "whatsapp" or bool(whatsapp_phone)
    telegram_bot_status = str(
        delivery.get("telegram_bot_status")
        or delivery_settings_json.get("telegram_bot_status")
        or ""
    ).strip()
    whatsapp_opt_in = (
        _coerce_bool(delivery.get("whatsapp_notification_opt_in"))
        or _coerce_bool(delivery_settings_json.get("whatsapp_notification_opt_in"))
    )
    property_notifications = {
        "preferred_channel": preferred_channel,
        "preferred_label": str(
            delivery.get("preferred_label")
            or delivery_settings_json.get("preferred_label")
            or preferred_channel.title()
        ).strip(),
        "notification_scope": str(
            delivery.get("notification_scope")
            or delivery_settings_json.get("notification_scope")
            or "scout_updates"
        ).strip(),
        "email_enabled": email_enabled,
        "telegram_enabled": telegram_enabled,
        "telegram_bot": {"status_label": telegram_bot_status} if telegram_bot_status else {},
        "whatsapp_enabled": whatsapp_enabled,
        "whatsapp_notification_opt_in": whatsapp_opt_in,
        "whatsapp_ai_support_phone": whatsapp_phone,
        "whatsapp_ai_support_phone_last4": "".join(ch for ch in whatsapp_phone if ch.isdigit())[-4:],
        "whatsapp_ai_support_status": "ready" if whatsapp_phone else "missing",
        "whatsapp_ai_support_purpose": str(
            delivery.get("whatsapp_ai_support_purpose")
            or delivery_settings_json.get("whatsapp_ai_support_purpose")
            or ""
        ).strip(),
        "signal_status": str(
            delivery.get("signal_status")
            or delivery_settings_json.get("signal_status")
            or "coming_soon"
        ).strip(),
    }
    decision_loop_rows = _decision_loop_rows_from_records(
        principal_id=normalized_principal,
        records_by_table=records_by_table,
    )
    decision_loop_counts = {
        table_name: len(rows)
        for table_name, rows in decision_loop_rows.items()
    }
    return {
        "contract_name": "propertyquarry.teable_restore_bundle.v1",
        "principal_id": normalized_principal,
        "status": "ready",
        "onboarding_state": {
            "workspace_name": str(user.get("workspace_name") or "PropertyQuarry").strip() or "PropertyQuarry",
            "workspace_mode": str(user.get("workspace_mode") or "personal").strip() or "personal",
            "region": str(user.get("region") or "").strip(),
            "language": str(user.get("language") or "").strip(),
            "timezone": str(user.get("timezone") or "").strip(),
            "selected_channels": sorted(set(selected_channels)),
            "property_search_preferences_json": preferences,
            "channel_preferences_json": {"property_notifications": property_notifications},
            "status": "completed",
        },
        "saved_result_count": len(saved_candidates),
        "subscription_restored": bool(restored_commercial),
        "review_artifact_count": len({
            str(row.get("projection_id") or row.get("property_ref") or row.get("property_url") or "").strip()
            for row in records_by_table.get("propertyquarry_review_artifacts", [])
            if _matches_principal(dict(row), principal_id=normalized_principal)
        }),
        "shared_artifact_count": len({
            str(row.get("projection_id") or row.get("artifact_url") or "").strip()
            for row in records_by_table.get("propertyquarry_shared_artifacts", [])
            if _matches_principal(dict(row), principal_id=normalized_principal)
        }),
        "research_task_count": len(restored_research_tasks),
        "decision_loop_counts": decision_loop_counts,
        "decision_loop_rows": decision_loop_rows,
        "source_tables": {
            table_name: len(records_by_table.get(table_name) or [])
            for table_name in PROPERTYQUARRY_TEABLE_TABLE_NAMES
        },
    }


def apply_restore_bundle(*, bundle: dict[str, object], database_url: str) -> dict[str, object]:
    state = _coerce_dict(bundle.get("onboarding_state"))
    principal_id = str(bundle.get("principal_id") or "").strip()
    if not principal_id:
        raise ValueError("principal_id_required")
    repo = PostgresOnboardingStateRepository(database_url)
    saved = repo.upsert_state(
        principal_id=principal_id,
        workspace_name=str(state.get("workspace_name") or "PropertyQuarry"),
        workspace_mode=str(state.get("workspace_mode") or "personal"),
        region=str(state.get("region") or ""),
        language=str(state.get("language") or ""),
        timezone=str(state.get("timezone") or ""),
        selected_channels=tuple(str(value or "").strip().lower() for value in _coerce_list(state.get("selected_channels"))),
        property_search_preferences_json=_coerce_dict(state.get("property_search_preferences_json")),
        channel_preferences_json=_coerce_dict(state.get("channel_preferences_json")),
        status=str(state.get("status") or "completed"),
    )
    decision_loop_result = apply_decision_loop_restore_bundle(bundle=bundle, database_url=database_url)
    return {
        "status": "applied",
        "principal_id": saved.principal_id,
        "selected_channels": list(saved.selected_channels),
        "saved_result_count": bundle.get("saved_result_count"),
        "decision_loop_restored": decision_loop_result,
    }


def apply_decision_loop_restore_bundle(*, bundle: dict[str, object], database_url: str) -> dict[str, object]:
    decision_loop_rows = _coerce_dict(bundle.get("decision_loop_rows"))
    if not decision_loop_rows:
        return {
            "propertyquarry_decision_ledger": 0,
            "propertyquarry_evidence_claims": 0,
            "propertyquarry_agent_questions": 0,
            "propertyquarry_documents": 0,
        }
    from app.repositories.property_decision_loop_postgres import PostgresPropertyDecisionLoopRepository

    repo = PostgresPropertyDecisionLoopRepository(database_url)
    decision_rows = [dict(row) for row in _coerce_list(decision_loop_rows.get("propertyquarry_decision_ledger")) if isinstance(row, dict)]
    evidence_rows = [dict(row) for row in _coerce_list(decision_loop_rows.get("propertyquarry_evidence_claims")) if isinstance(row, dict)]
    question_rows = [dict(row) for row in _coerce_list(decision_loop_rows.get("propertyquarry_agent_questions")) if isinstance(row, dict)]
    document_rows = [dict(row) for row in _coerce_list(decision_loop_rows.get("propertyquarry_documents")) if isinstance(row, dict)]
    with repo._connect() as conn:  # noqa: SLF001 - restore script uses the repository connection and schema setup.
        with conn.cursor() as cur:
            for row in decision_rows:
                if not str(row.get("decision_id") or "").strip():
                    continue
                cur.execute(
                    """
                    INSERT INTO property_decision_ledger (
                        decision_id, principal_id, person_id, property_ref, decision_state,
                        reason_keys_json, source, actor, confidence, supersedes_decision_id,
                        learning_applied, aggregate_candidate, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (decision_id) DO UPDATE
                    SET principal_id = EXCLUDED.principal_id,
                        person_id = EXCLUDED.person_id,
                        property_ref = EXCLUDED.property_ref,
                        decision_state = EXCLUDED.decision_state,
                        reason_keys_json = EXCLUDED.reason_keys_json,
                        learning_applied = EXCLUDED.learning_applied,
                        aggregate_candidate = EXCLUDED.aggregate_candidate
                    """,
                    (
                        str(row.get("decision_id") or "").strip(),
                        str(row.get("principal_id") or "").strip(),
                        str(row.get("person_id") or "self").strip() or "self",
                        str(row.get("property_ref") or "").strip(),
                        str(row.get("decision_state") or "reviewing").strip() or "reviewing",
                        repo._json_value(_coerce_list(row.get("reason_keys_json"))),  # noqa: SLF001
                        str(row.get("source") or "system").strip() or "system",
                        str(row.get("actor") or "teable_restore").strip() or "teable_restore",
                        float(row.get("confidence") or 0.7),
                        str(row.get("supersedes_decision_id") or "").strip(),
                        bool(row.get("learning_applied")),
                        bool(row.get("aggregate_candidate")),
                        _created_at(row.get("created_at")),
                    ),
                )
            for row in evidence_rows:
                if not str(row.get("claim_id") or "").strip():
                    continue
                cur.execute(
                    """
                    INSERT INTO property_evidence_claims (
                        claim_id, principal_id, person_id, property_ref, decision_id, claim_type,
                        text, source_type, source_ref, confidence, verification_state, privacy_class,
                        allowed_outputs_json, expires_at, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (claim_id) DO UPDATE
                    SET principal_id = EXCLUDED.principal_id,
                        person_id = EXCLUDED.person_id,
                        property_ref = EXCLUDED.property_ref,
                        decision_id = EXCLUDED.decision_id,
                        text = EXCLUDED.text,
                        verification_state = EXCLUDED.verification_state,
                        allowed_outputs_json = EXCLUDED.allowed_outputs_json
                    """,
                    (
                        str(row.get("claim_id") or "").strip(),
                        str(row.get("principal_id") or "").strip(),
                        str(row.get("person_id") or "self").strip() or "self",
                        str(row.get("property_ref") or "").strip(),
                        str(row.get("decision_id") or "").strip(),
                        str(row.get("claim_type") or "fact").strip() or "fact",
                        str(row.get("claim_text") or "").strip(),
                        str(row.get("source_type") or "teable_restore").strip() or "teable_restore",
                        str(row.get("source_ref") or "").strip(),
                        str(row.get("confidence") or "medium").strip() or "medium",
                        str(row.get("verification_state") or "unclear").strip() or "unclear",
                        str(row.get("privacy_class") or "owner_private").strip() or "owner_private",
                        repo._json_value(_coerce_list(row.get("allowed_outputs_json"))),  # noqa: SLF001
                        str(row.get("expires_at") or "").strip(),
                        _created_at(row.get("created_at")),
                    ),
                )
            for row in question_rows:
                if not str(row.get("task_id") or "").strip():
                    continue
                cur.execute(
                    """
                    INSERT INTO property_agent_question_tasks (
                        task_id, principal_id, person_id, property_ref, decision_id, question_text,
                        reason_key, source_claim_id, status, answer_source, updated_claim_id, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (task_id) DO UPDATE
                    SET principal_id = EXCLUDED.principal_id,
                        person_id = EXCLUDED.person_id,
                        property_ref = EXCLUDED.property_ref,
                        decision_id = EXCLUDED.decision_id,
                        question_text = EXCLUDED.question_text,
                        status = EXCLUDED.status,
                        updated_claim_id = EXCLUDED.updated_claim_id
                    """,
                    (
                        str(row.get("task_id") or "").strip(),
                        str(row.get("principal_id") or "").strip(),
                        str(row.get("person_id") or "self").strip() or "self",
                        str(row.get("property_ref") or "").strip(),
                        str(row.get("decision_id") or "").strip(),
                        str(row.get("question_text") or "").strip(),
                        str(row.get("reason_key") or "").strip(),
                        str(row.get("source_claim_id") or "").strip(),
                        str(row.get("status") or "drafted").strip() or "drafted",
                        str(row.get("answer_source") or "").strip(),
                        str(row.get("updated_claim_id") or "").strip(),
                        _created_at(row.get("created_at")),
                    ),
                )
            for row in document_rows:
                if not str(row.get("document_id") or "").strip():
                    continue
                cur.execute(
                    """
                    INSERT INTO property_documents (
                        document_id, principal_id, person_id, property_ref, decision_id, document_type,
                        source, privacy_class, verification_state, extracted_claims_json,
                        missing_pages_json, redaction_state, linked_risks_json, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (document_id) DO UPDATE
                    SET principal_id = EXCLUDED.principal_id,
                        person_id = EXCLUDED.person_id,
                        property_ref = EXCLUDED.property_ref,
                        decision_id = EXCLUDED.decision_id,
                        verification_state = EXCLUDED.verification_state,
                        extracted_claims_json = EXCLUDED.extracted_claims_json,
                        redaction_state = EXCLUDED.redaction_state,
                        linked_risks_json = EXCLUDED.linked_risks_json
                    """,
                    (
                        str(row.get("document_id") or "").strip(),
                        str(row.get("principal_id") or "").strip(),
                        str(row.get("person_id") or "self").strip() or "self",
                        str(row.get("property_ref") or "").strip(),
                        str(row.get("decision_id") or "").strip(),
                        str(row.get("document_type") or "").strip(),
                        str(row.get("source") or "").strip(),
                        str(row.get("privacy_class") or "owner_private").strip() or "owner_private",
                        str(row.get("verification_state") or "missing").strip() or "missing",
                        repo._json_value(_coerce_list(row.get("extracted_claims_json"))),  # noqa: SLF001
                        repo._json_value(_coerce_list(row.get("missing_pages_json"))),  # noqa: SLF001
                        str(row.get("redaction_state") or "not_started").strip() or "not_started",
                        repo._json_value(_coerce_list(row.get("linked_risks_json"))),  # noqa: SLF001
                        _created_at(row.get("created_at")),
                    ),
                )
    return {
        "propertyquarry_decision_ledger": len(decision_rows),
        "propertyquarry_evidence_claims": len(evidence_rows),
        "propertyquarry_agent_questions": len(question_rows),
        "propertyquarry_documents": len(document_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore PropertyQuarry account/results state from Teable projections.")
    parser.add_argument("--principal-id", required=True)
    parser.add_argument("--base-url", default=os.environ.get("TEABLE_BASE_URL") or "https://app.teable.ai")
    parser.add_argument("--base-id", default=os.environ.get("PROPERTYQUARRY_TEABLE_BASE_ID") or os.environ.get("TEABLE_BASE_ID") or "")
    parser.add_argument("--base-name", default=os.environ.get("PROPERTYQUARRY_TEABLE_TENANT_NAME") or "PropertyQuarry")
    parser.add_argument("--api-key", default=os.environ.get("PROPERTYQUARRY_TEABLE_API_KEY") or os.environ.get("TEABLE_API_KEY") or "")
    parser.add_argument("--output", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL") or "")
    args = parser.parse_args()
    api_key = str(args.api_key or "").strip()
    if not api_key:
        raise SystemExit("missing TEABLE_API_KEY")
    records = fetch_teable_projection_records(
        base_url=str(args.base_url or "https://app.teable.ai"),
        api_key=api_key,
        table_config=_table_config_from_env(
            base_url=str(args.base_url or "https://app.teable.ai"),
            api_key=api_key,
            base_id=str(args.base_id or ""),
            base_name=str(args.base_name or "PropertyQuarry"),
        ),
    )
    bundle = build_restore_bundle(principal_id=str(args.principal_id), records_by_table=records)
    if args.output:
        Path(args.output).write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.apply:
        database_url = str(args.database_url or "").strip()
        if not database_url:
            raise SystemExit("missing DATABASE_URL for --apply")
        result = apply_restore_bundle(bundle=bundle, database_url=database_url)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(json.dumps(bundle, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
