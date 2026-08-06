from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Mapping


PROPERTY_SEARCH_SUPPRESSIONS_CONTRACT = "propertyquarry.search_suppressions.v1"
_PROPERTY_SEARCH_SUPPRESSIONS_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "property_search_suppressions.json"
)
_LISTING_ID_FROM_URL = re.compile(r"-(\d{6,})(?:/)?(?:[?#].*)?$")
_CANDIDATE_LIST_KEYS = (
    "ranked_candidates",
    "_delivery_candidates",
    "research_candidates",
    "top_candidates",
)
_VISIBLE_TOTAL_KEYS = (
    "listing_total",
    "ranked_total",
    "ranked_candidate_total",
    "results_total",
    "survivor_total",
)


@lru_cache(maxsize=1)
def property_search_suppression_index() -> dict[str, frozenset[str]]:
    try:
        payload = json.loads(
            _PROPERTY_SEARCH_SUPPRESSIONS_PATH.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {"candidate_refs": frozenset(), "listing_ids": frozenset()}
    if (
        not isinstance(payload, Mapping)
        or payload.get("contract_name") != PROPERTY_SEARCH_SUPPRESSIONS_CONTRACT
    ):
        return {"candidate_refs": frozenset(), "listing_ids": frozenset()}
    candidate_refs: set[str] = set()
    listing_ids: set[str] = set()
    for raw_entry in list(payload.get("entries") or []):
        if not isinstance(raw_entry, Mapping):
            continue
        if str(raw_entry.get("status") or "").strip().lower() != "suppressed":
            continue
        if str(raw_entry.get("scope") or "").strip().lower() != "all_workspaces":
            continue
        candidate_refs.update(
            str(value or "").strip().lower()
            for value in list(raw_entry.get("candidate_refs") or [])
            if str(value or "").strip()
        )
        listing_ids.update(
            str(value or "").strip().lower()
            for value in list(raw_entry.get("listing_ids") or [])
            if str(value or "").strip()
        )
    return {
        "candidate_refs": frozenset(candidate_refs),
        "listing_ids": frozenset(listing_ids),
    }


def property_search_candidate_is_suppressed(candidate: object) -> bool:
    if not isinstance(candidate, Mapping):
        return False
    suppression_index = property_search_suppression_index()
    candidate_refs = suppression_index["candidate_refs"]
    listing_ids = suppression_index["listing_ids"]
    candidate_ref_values = {
        str(candidate.get(key) or "").strip().lower()
        for key in ("candidate_ref", "source_ref")
        if str(candidate.get(key) or "").strip()
    }
    if candidate_ref_values & candidate_refs:
        return True
    listing_id = str(candidate.get("listing_id") or "").strip().lower()
    if listing_id and listing_id in listing_ids:
        return True
    property_url = str(
        candidate.get("property_url")
        or candidate.get("listing_url")
        or candidate.get("source_url")
        or ""
    ).strip()
    listing_match = _LISTING_ID_FROM_URL.search(property_url)
    return bool(listing_match and listing_match.group(1).lower() in listing_ids)


def _filter_candidate_rows(value: object) -> tuple[list[dict[str, object]], int]:
    filtered: list[dict[str, object]] = []
    removed = 0
    for raw_candidate in list(value or []):
        if not isinstance(raw_candidate, Mapping):
            continue
        candidate = dict(raw_candidate)
        if property_search_candidate_is_suppressed(candidate):
            removed += 1
            continue
        filtered.append(candidate)
    return filtered, removed


def _subtract_visible_totals(payload: dict[str, object], removed: int) -> None:
    if removed <= 0:
        return
    for key in _VISIBLE_TOTAL_KEYS:
        if key not in payload:
            continue
        try:
            current = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            continue
        payload[key] = max(0, current - removed)


def filter_property_search_run_visibility(
    raw_run: dict[str, object],
) -> dict[str, object]:
    payload = dict(raw_run or {})
    summary = (
        dict(payload.get("summary") or {})
        if isinstance(payload.get("summary"), Mapping)
        else {}
    )
    removed_ranked = 0
    removed_delivery = 0
    removed_sources = 0
    for key in _CANDIDATE_LIST_KEYS:
        if key not in summary:
            continue
        filtered, removed = _filter_candidate_rows(summary.get(key))
        summary[key] = filtered
        if key == "ranked_candidates":
            removed_ranked += removed
        elif key == "_delivery_candidates":
            removed_delivery += removed

    sources: list[dict[str, object]] = []
    for raw_source in list(summary.get("sources") or []):
        if not isinstance(raw_source, Mapping):
            continue
        source = dict(raw_source)
        source_removed = 0
        for key in _CANDIDATE_LIST_KEYS:
            if key not in source:
                continue
            filtered, removed = _filter_candidate_rows(source.get(key))
            source[key] = filtered
            source_removed = max(source_removed, removed)
        removed_sources = max(removed_sources, source_removed)
        _subtract_visible_totals(source, source_removed)
        sources.append(source)
    if "sources" in summary:
        summary["sources"] = sources

    removed_visible = max(removed_ranked, removed_delivery, removed_sources)
    _subtract_visible_totals(summary, removed_visible)
    if removed_visible > 0:
        summary["owner_suppressed_total"] = int(
            summary.get("owner_suppressed_total") or 0
        ) + removed_visible
    if summary:
        payload["summary"] = summary

    for key in _CANDIDATE_LIST_KEYS:
        if key not in payload:
            continue
        filtered, _removed = _filter_candidate_rows(payload.get(key))
        payload[key] = filtered
    return payload
