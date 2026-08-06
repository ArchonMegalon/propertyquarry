from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone
from typing import Callable

from app.services.property_billing import normalize_property_plan_key, property_commercial_snapshot, property_plan_has_unlimited_provider_results
from app.services.property_customer_copy import sanitize_property_marketing_copy, summarize_property_description_copy
from app.services.property_market_catalog import supported_currency_codes
from app.services.property_search_visibility import (
    filter_property_search_run_visibility,
)

from app.product.models import (
    PropertyBillingTruthSnapshot,
    PropertyPreferenceManagerSnapshot,
    PropertyResearchPacketSnapshot,
    PropertyRecurringWatchSnapshot,
    PropertySearchFormStateSnapshot,
    PropertyRunHealthSnapshot,
    PropertyRunLiveBoardSnapshot,
    PropertyRunReliabilitySnapshot,
    PropertyRunRepairSnapshot,
    PropertySearchAgentSelectionSnapshot,
    PropertySearchRunSnapshot,
    PropertyShortlistSnapshot,
    PropertyWorkbenchCandidateSnapshot,
)


def _build_scope_preview(
    scope_preview_builder: Callable[..., dict[str, object]],
    country: str,
    region: str,
    location: str,
    *,
    adjacent_area_radius_m: object = 0,
) -> dict[str, object]:
    radius_m = _previous_run_int(adjacent_area_radius_m)
    if radius_m > 0:
        try:
            return scope_preview_builder(
                country,
                region,
                location,
                adjacent_area_radius_m=radius_m,
            )
        except TypeError:
            return scope_preview_builder(country, region, location)
    return scope_preview_builder(country, region, location)


_GENERIC_SOURCE_FAMILIES = {
    "genossenschaften",
    "genossenschaften at",
    "community housing",
    "community providers",
    "developer projects",
}

_INTERNAL_RUN_STATUS_NOISE_TOKENS = (
    "could not load property search status",
    "checking run status",
    "suppressed_generic_listing_page",
    "starting property search run",
    "scoring enriched candidate",
    "ranking homes",
)

_STALE_ZERO_REVIEW_SEARCH_PAGES_RE = re.compile(
    r"\b(?P<found>\d+)\s+homes?\s+found\s*·\s*0\s+to\s+review\s*·\s*"
    r"(?P<done>\d+)\s*/\s*(?P<total>\d+)\s*(?P<unit>search pages|checks|providers|sources|sites)\b",
    re.IGNORECASE,
)

_DATABASE_PRESSURE_TOKENS = (
    "too many clients",
    "sorry, too many clients already",
    "remaining connection slots are reserved",
    "database_busy",
    "connection pool exhausted",
)

_RAW_PROVIDER_FAILURE_TOKENS = (
    "provider returned ",
    " while fetching ",
    "worker interrupted",
    "connection failed",
    "temporary fetch failed",
    "fetch failed",
    "timed out",
    "timeout",
    "traceback",
    "exception",
    " 401",
    " 403",
    " 404",
    " 429",
    " 500",
    " 502",
    " 503",
    "error code: 1010",
    "error code: 1015",
)

_HIDDEN_PROPERTY_ACTION_LABELS = {
    "full view",
    "open full view",
}


def _is_hidden_property_action_label(label: object) -> bool:
    return str(label or "").strip().lower() in _HIDDEN_PROPERTY_ACTION_LABELS


def _clean_property_action_rows(actions: object) -> list[dict[str, object]]:
    cleaned_actions: list[dict[str, object]] = []
    for action in list(actions or []):
        if not isinstance(action, dict):
            continue
        if _is_hidden_property_action_label(action.get("label") or action.get("action_label")):
            continue
        cleaned_actions.append(dict(action))
    return cleaned_actions


def _clean_property_shortlist_row(row: dict[str, object]) -> dict[str, object]:
    cleaned = dict(row)
    for key in ("description_text", "location_text", "summary", "fit_summary", "compare_reason"):
        if key in cleaned:
            if key == "description_text":
                cleaned[key] = summarize_property_description_copy(cleaned.get(key))
            else:
                cleaned[key] = sanitize_property_marketing_copy(cleaned.get(key))
    return cleaned


def _strip_hidden_property_action_slots(row: dict[str, object]) -> dict[str, object]:
    cleaned = dict(row)
    for prefix in ("", "secondary_", "tertiary_", "quaternary_"):
        label_key = f"{prefix}action_label"
        if not _is_hidden_property_action_label(cleaned.get(label_key)):
            continue
        for key in (
            f"{prefix}action_href",
            f"{prefix}action_label",
            f"{prefix}action_method",
            f"{prefix}action_value",
            f"{prefix}return_to",
        ):
            cleaned.pop(key, None)
    if isinstance(cleaned.get("actions"), list):
        cleaned["actions"] = _clean_property_action_rows(cleaned.get("actions"))
    return cleaned

_CUSTOMER_SAFE_FAILURE_PREFIXES = (
    "search paused",
    "search stopped before",
    "a replacement search is now checking the saved brief",
    "repair is retrying",
    "retrying ",
    "no source completed cleanly enough",
)

_ACTIVE_REPAIR_STATUSES = {"repairing", "queued", "pending", "assigned", "retrying", "existing"}
_ACTIVE_REPAIR_TASK_STATUSES = {"opened", "assigned", "running", "repairing", "pending", "existing", "queued"}
_TERMINAL_REPAIR_TASK_STATUSES = {"returned", "failed", "done", "resolved", "completed", "closed"}
_PROPERTY_RUN_DIRECT_ETA_RE = re.compile(
    r"^(?:about|under|less than|around|roughly)\s+\d+(?:[.,]\d+)?\s*"
    r"(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)"
    r"(?:\s+\d+(?:[.,]\d+)?\s*(?:m|min|mins|minute|minutes|h|hr|hrs|hour|hours))?$",
    flags=re.IGNORECASE,
)
_PROPERTY_RUN_BARE_ETA_RE = re.compile(
    r"^\d+(?:[.,]\d+)?\s*"
    r"(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)"
    r"(?:\s+\d+(?:[.,]\d+)?\s*(?:m|min|mins|minute|minutes|h|hr|hrs|hour|hours))?$",
    flags=re.IGNORECASE,
)
_PROPERTY_RUN_DELAYED_ETA_RE = re.compile(
    r"^delayed\s*[·\-]\s*\d+(?:[.,]\d+)?\s*"
    r"(?:m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s+so\s+far$",
    flags=re.IGNORECASE,
)
_PROPERTY_RUN_ALLOWED_ETA_PHRASES = {
    "new shortlist already ready",
    "start a fresh run after changing one provider or rule",
}
_PROPERTY_RUN_SUPPRESSED_RESOLUTION_RE = re.compile(r"\bsuppressed_[a-z0-9_]+\b", flags=re.IGNORECASE)
_PROPERTY_RUN_ETA_TOKEN_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(d|day|days|h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)\b",
    flags=re.IGNORECASE,
)
_PROPERTY_RUN_VISIBLE_EVENT_LIMIT = 10


def _positive_int(value: object, *, default: int = 0) -> int:
    try:
        parsed = int(float(value or 0))
    except Exception:
        parsed = 0
    return parsed if parsed > 0 else default


def property_run_public_eta_label(value: object) -> str:
    text = " ".join(str(value or "").replace("\u2026", "...").split()).strip()
    if not text:
        return ""
    if len(text) > 64:
        return ""
    normalized = text.lower().strip(" .")
    if normalized in _PROPERTY_RUN_ALLOWED_ETA_PHRASES:
        return text.strip(" .")
    duration_seconds = _property_run_eta_duration_seconds(text)
    if duration_seconds and duration_seconds < 120:
        return ""
    if _PROPERTY_RUN_DIRECT_ETA_RE.fullmatch(text) or _PROPERTY_RUN_BARE_ETA_RE.fullmatch(text) or _PROPERTY_RUN_DELAYED_ETA_RE.fullmatch(text):
        return text
    return ""


def _property_run_eta_duration_seconds(text: str) -> int:
    total = 0.0
    for raw_amount, raw_unit in _PROPERTY_RUN_ETA_TOKEN_RE.findall(str(text or "")):
        try:
            amount = float(str(raw_amount).replace(",", "."))
        except Exception:
            continue
        unit = str(raw_unit or "").strip().lower()
        if unit.startswith("d"):
            total += amount * 86400
        elif unit in {"h", "hr", "hrs", "hour", "hours"}:
            total += amount * 3600
        elif unit in {"m", "min", "mins", "minute", "minutes"}:
            total += amount * 60
        else:
            total += amount
    return int(total)


def _property_run_distance_subject(value: object) -> str:
    label = " ".join(str(value or "").replace("-", " ").split()).strip()
    label = re.sub(r"\s+(?:radius|distance)\b", "", label, flags=re.IGNORECASE).strip()
    if not label:
        return "Distance"
    return f"{label[:1].upper()}{label[1:]}"


def _property_run_distance_phase_label(
    *,
    filter_label: object,
    observed_distance_m: object = None,
    requested_distance_m: object = None,
    observed_place_name: object = "",
) -> str:
    observed = _positive_int(observed_distance_m)
    if observed <= 0:
        return ""
    subject = _property_run_distance_subject(filter_label)
    place_name = " ".join(str(observed_place_name or "").split()).strip()
    detail = f"{subject}: {place_name} is {observed} m away" if place_name else f"{subject}: {observed} m away"
    requested = _positive_int(requested_distance_m)
    if requested > 0:
        return f"{detail}. Limit {requested} m."
    return f"{detail}."


def _property_run_distance_phase_label_from_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    measured_match = re.search(
        r"(?:outside the relaxed|beyond the preferred)\s+(.+?)\s+radius(?:\s+for\s+.+?)?:\s*(\d+(?:[.,]\d+)?)\s*m\s+vs\s+(\d+(?:[.,]\d+)?)\s*m\b",
        text,
        flags=re.IGNORECASE,
    )
    if not measured_match:
        return ""
    return _property_run_distance_phase_label(
        filter_label=measured_match.group(1),
        observed_distance_m=str(measured_match.group(2) or "").replace(",", "."),
        requested_distance_m=str(measured_match.group(3) or "").replace(",", "."),
    )


def _property_run_source_near_miss_phase_label(source: dict[str, object]) -> str:
    near_misses = [dict(item) for item in list(source.get("filter_near_misses") or []) if isinstance(item, dict)]
    for near_miss in near_misses:
        label = _property_run_distance_phase_label(
            filter_label=near_miss.get("failed_filter_label") or near_miss.get("failed_filter_key"),
            observed_distance_m=near_miss.get("observed_distance_m"),
            requested_distance_m=near_miss.get("requested_distance_m"),
            observed_place_name=near_miss.get("observed_place_name"),
        )
        if label:
            return label
    return ""


def _property_run_source_summary_for_label(
    source_rows: list[dict[str, object]],
    source_label: object,
) -> dict[str, object]:
    needle = _canonical_property_run_source_label(source_label)
    if not needle:
        return {}
    needle_folded = needle.casefold()
    for row in source_rows:
        row_label = _canonical_property_run_source_label(row.get("source_label") or row.get("label") or "")
        if row_label == needle or row_label.casefold() == needle_folded:
            return row
    return {}


def normalized_property_search_goal(value: object) -> str:
    goal = str(value or "home").strip().lower() or "home"
    return goal if goal in {"home", "investment"} else "home"


def effective_property_listing_mode(
    preferences: dict[str, object] | None,
    *,
    fallback: str = "rent",
) -> str:
    payload = dict(preferences or {})
    if normalized_property_search_goal(payload.get("search_goal")) == "investment":
        return "buy"
    mode = str(payload.get("listing_mode") or fallback or "rent").strip().lower()
    return "buy" if mode == "buy" else "rent"


def property_mode_visibility_label(
    preferences: dict[str, object] | None,
    *,
    fallback: str = "rent",
) -> str:
    payload = dict(preferences or {})
    if normalized_property_search_goal(payload.get("search_goal")) == "investment":
        return "Investment"
    return "Buy" if effective_property_listing_mode(payload, fallback=fallback) == "buy" else "Rent"


def _property_search_ranked_candidates_from_sources(
    sources: list[dict[str, object]],
    *,
    limit: int | None = None,
) -> list[dict[str, object]]:
    ranked_candidates: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_label = str(source.get("source_label") or source.get("label") or "").strip()
        for candidate in list(source.get("research_candidates") or source.get("top_candidates") or []):
            if not isinstance(candidate, dict):
                continue
            candidate_row = dict(candidate)
            candidate_row.setdefault("source_label", source_label)
            candidate_key = str(
                candidate_row.get("candidate_ref")
                or candidate_row.get("source_ref")
                or candidate_row.get("property_url")
                or candidate_row.get("listing_id")
                or candidate_row.get("title")
                or ""
            ).strip()
            if candidate_key and candidate_key in seen_keys:
                continue
            if candidate_key:
                seen_keys.add(candidate_key)
            ranked_candidates.append(candidate_row)
    ranked_candidates.sort(key=lambda item: float(item.get("fit_score") or 0.0), reverse=True)
    for index, candidate_row in enumerate(ranked_candidates, start=1):
        candidate_row.setdefault("rank", index)
    if limit and limit > 0:
        return ranked_candidates[:limit]
    return ranked_candidates


def _property_summary_held_back_total(summary: dict[str, object]) -> int:
    return max(
        0,
        _positive_int(summary.get("held_back_total"))
        or _positive_int(summary.get("filtered_total"))
        or _positive_int(summary.get("filtered_out_total"))
        or (
            _positive_int(summary.get("filtered_floorplan_total"))
            + _positive_int(summary.get("filtered_area_total"))
            + _positive_int(summary.get("filtered_property_type_total"))
            + _positive_int(summary.get("filtered_availability_total"))
            + _positive_int(summary.get("filtered_generic_page_total"))
            + _positive_int(summary.get("filtered_listing_mode_total"))
        ),
    )


def _property_summary_ranked_candidates(summary: dict[str, object]) -> list[dict[str, object]]:
    ranked_candidates = [
        dict(row)
        for row in list(summary.get("ranked_candidates") or [])
        if isinstance(row, dict)
    ]
    if ranked_candidates:
        return ranked_candidates
    sources = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    if not sources:
        return []
    return _property_search_ranked_candidates_from_sources(sources)


def _property_summary_ranked_total(summary: dict[str, object]) -> int:
    ranked_candidates = _property_summary_ranked_candidates(summary)
    explicit_total = max(
        0,
        _positive_int(summary.get("ranked_total")),
        _positive_int(summary.get("ranked_candidate_total")),
        _positive_int(summary.get("results_total")),
        _positive_int(summary.get("survivor_total")),
        _positive_int(summary.get("high_fit_total")),
    )
    if ranked_candidates:
        return max(len(ranked_candidates), explicit_total)
    return explicit_total


def _property_run_listing_work_counts(
    summary: dict[str, object],
    *,
    status: str = "",
) -> dict[str, int]:
    def as_int(value: object) -> int:
        return _positive_int(value)

    source_rows = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    source_found = 0
    source_scanned = 0
    for row in source_rows:
        raw_total = as_int(row.get("raw_listing_total"))
        scanned_total = max(
            as_int(row.get("scanned_listing_total")),
            as_int(row.get("reviewed_listing_total")),
        )
        source_found += max(raw_total, scanned_total)
        source_scanned += scanned_total

    explicit_scanned = max(
        as_int(summary.get("scanned_listing_total")),
        as_int(summary.get("reviewed_listing_total")),
        source_scanned,
    )
    explicit_found = max(
        as_int(summary.get("found_listing_total")),
        as_int(summary.get("raw_listing_total")),
        source_found,
        explicit_scanned,
    )

    if explicit_found <= 0:
        # listing_total is a visible-result count in several paths. Use it only
        # when there is no stronger raw/scanned signal.
        explicit_found = as_int(summary.get("listing_total"))

    run_status = str(status or summary.get("status") or "").strip().lower()
    if (
        explicit_found > 0
        and explicit_scanned <= 0
        and run_status in {"processed", "completed", "completed_partial", "noop", "cancelled"}
    ):
        explicit_scanned = explicit_found

    scanned = min(explicit_scanned, explicit_found) if explicit_found > 0 else 0
    found = max(explicit_found, scanned)
    return {
        "found": found,
        "scanned": scanned,
        "to_review": max(0, found - scanned),
    }


def _property_run_normalize_listing_work_summary(
    summary: dict[str, object],
    *,
    status: str = "",
) -> dict[str, object]:
    normalized = dict(summary or {})
    counts = _property_run_listing_work_counts(normalized, status=status)
    normalized["found_listing_total"] = counts["found"]
    normalized["scanned_listing_total"] = counts["scanned"]
    normalized["to_review_listing_total"] = counts["to_review"]
    return normalized


def _property_run_listing_queue_label(found: int, to_review: int) -> str:
    if found <= 0:
        return "checking"
    if to_review > 0:
        return f"{found} homes found · {to_review} to review"
    return f"{found} homes found · all found homes are checked"


def normalize_property_search_run_snapshot(raw_run: dict[str, object]) -> dict[str, object]:
    original_payload = dict(raw_run or {})
    payload = {
        **original_payload,
        **PropertySearchRunSnapshot.from_dict(original_payload).to_dict(),
    }
    payload = filter_property_search_run_visibility(payload)
    summary = dict(payload.get("summary") or {}) if isinstance(payload.get("summary"), dict) else {}
    status = str(payload.get("status") or summary.get("status") or "").strip().lower()
    sources = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    if sources:
        ranked_candidates = _property_summary_ranked_candidates(summary)
        if not ranked_candidates:
            ranked_candidates = _property_search_ranked_candidates_from_sources(sources)
        if ranked_candidates:
            summary["ranked_candidates"] = ranked_candidates
        held_back_total = _property_summary_held_back_total(summary)
        if held_back_total > 0:
            summary.setdefault("held_back_total", held_back_total)
            summary.setdefault("filtered_total", held_back_total)
    if summary:
        summary = _property_run_normalize_listing_work_summary(summary, status=status)
        payload["summary"] = summary
    return payload


def property_run_status_copy(status_value: object, message_value: object = "") -> tuple[str, str]:
    status = str(status_value or "").strip().lower()
    message = str(message_value or "").strip()
    safe_message = property_run_customer_safe_status_detail(status, message)
    if status in {"processed", "completed"}:
        return ("Finished", "")
    if status == "completed_partial":
        return ("Finished with partial coverage", safe_message or "The shortlist is ready, but one or more providers finished degraded.")
    if status == "failed":
        return ("Search interrupted", safe_message or "The search stopped before ranking finished.")
    if status == "cancelled":
        return ("Stopped", safe_message or "This search was stopped before it finished.")
    if status == "noop":
        return ("No changes", safe_message or "The search finished without anything new to rank.")
    if status in {"queued", "starting"}:
        return ("Queued", safe_message)
    if status in {"running", "in_progress", "processing", "scanning"}:
        return ("Running", safe_message)
    label = status.replace("_", " ").title() if status else "Queued"
    return (label, safe_message)


def property_run_customer_safe_status_detail(
    status_value: object,
    message_value: object = "",
    *,
    summary: dict[str, object] | None = None,
    prefer_repair_step: bool = False,
) -> str:
    status = str(status_value or "").strip().lower()
    summary_dict = dict(summary or {})
    raw_message = str(message_value or "").strip()
    message = _compact_run_message(raw_message) if raw_message else ""
    lowered = message.lower()
    customer_status = str(summary_dict.get("customer_status_message") or "").strip()
    replacement_run_id = str(summary_dict.get("repair_replacement_run_id") or "").strip()
    repair_step = str(summary_dict.get("repair_step_label") or "").strip()
    repair_status = str(summary_dict.get("repair_status_label") or summary_dict.get("repair_status") or "").strip()
    repair_flags = _repair_summary_flags(summary_dict)
    repair_reason = _resolved_customer_repair_reason(summary_dict, message_value=message)
    calm_repair_copy = _calm_customer_repair_copy(
        repair_reason,
        replacement_run_id=replacement_run_id,
        repair_active=bool(repair_flags.get("active")),
        repair_failed=bool(repair_flags.get("failed")),
    )
    if (
        status in {"queued", "starting", "running", "in_progress", "processing", "scanning"}
        and "provider" in lowered
        and "scann" in lowered
    ):
        return "Checking search pages."
    if _customer_status_is_internal_failure_copy(customer_status) and calm_repair_copy:
        customer_status = calm_repair_copy
    if customer_status and calm_repair_copy and customer_status.lower() == calm_repair_copy.lower():
        repair_reason = ""

    if any(token in lowered for token in _DATABASE_PRESSURE_TOKENS):
        return customer_status or "The search paused because the database was busy. PropertyQuarry is retrying it."
    if replacement_run_id:
        return customer_status or calm_repair_copy or "A fresh search is checking your saved brief."
    if any(token in lowered for token in _INTERNAL_RUN_STATUS_NOISE_TOKENS):
        return customer_status or calm_repair_copy or repair_reason
    if status == "failed" and lowered.startswith(_CUSTOMER_SAFE_FAILURE_PREFIXES):
        return customer_status or calm_repair_copy or _join_customer_sentences(message, repair_reason if repair_reason.lower() not in lowered else "")

    raw_failure_like = (
        status in {"failed", "error"}
        and (
            not message
            or any(token in lowered for token in _RAW_PROVIDER_FAILURE_TOKENS)
        )
    ) or any(token in lowered for token in _RAW_PROVIDER_FAILURE_TOKENS)

    if any(token in lowered for token in ("error code: 1010", "error code: 1015")):
        return customer_status or calm_repair_copy or "PropertyQuarry is retrying affected providers."

    if repair_step and raw_failure_like and prefer_repair_step:
        return customer_status or calm_repair_copy or "Refreshing affected providers."
    if customer_status and (status in {"failed", "completed_partial"} or raw_failure_like):
        return customer_status
    if repair_step and raw_failure_like:
        return calm_repair_copy or _join_customer_sentences(
            "PropertyQuarry is refreshing affected providers",
            "" if _repair_reason_points_to_provider_site_change(repair_reason) else repair_reason,
        )
    if raw_failure_like and repair_status:
        return calm_repair_copy or "PropertyQuarry is refreshing affected providers."
    if status == "failed" and raw_failure_like:
        return calm_repair_copy or "The search stopped before the shortlist settled."
    if status == "failed" and repair_flags.get("active"):
        return customer_status or calm_repair_copy or "PropertyQuarry is checking the saved search again."
    if status == "failed" and repair_flags.get("failed"):
        return customer_status or calm_repair_copy or "The search stopped before a ready shortlist was available."
    return customer_status or message


def _repair_tasks(summary: dict[str, object]) -> list[dict[str, object]]:
    return [dict(row) for row in list(summary.get("provider_repair_tasks") or []) if isinstance(row, dict)]


def _repair_receipts(summary: dict[str, object]) -> list[dict[str, object]]:
    return [dict(row) for row in list(summary.get("repair_receipts") or []) if isinstance(row, dict)]


def _repair_summary_flags(summary: dict[str, object]) -> dict[str, object]:
    repair_status = str(summary.get("repair_status") or "").strip().lower()
    repair_status_label = str(summary.get("repair_status_label") or "").strip().lower()
    replacement_run_id = str(summary.get("repair_replacement_run_id") or "").strip()
    tasks = _repair_tasks(summary)
    task_statuses = {
        str(task.get("status") or "").strip().lower()
        for task in tasks
        if str(task.get("status") or "").strip()
    }
    has_active_task = bool(task_statuses & _ACTIVE_REPAIR_TASK_STATUSES)
    has_terminal_task = bool(task_statuses & _TERMINAL_REPAIR_TASK_STATUSES)
    active = not replacement_run_id and (
        repair_status in _ACTIVE_REPAIR_STATUSES
        or repair_status_label in _ACTIVE_REPAIR_STATUSES
        or has_active_task
    )
    failed = not replacement_run_id and (
        repair_status == "failed"
        or repair_status_label == "repair failed"
        or (has_terminal_task and not has_active_task and repair_status not in _ACTIVE_REPAIR_STATUSES and repair_status_label not in _ACTIVE_REPAIR_STATUSES)
    )
    return {
        "active": active,
        "failed": failed,
        "replacement_run_id": replacement_run_id,
    }


def _latest_repair_reason(summary: dict[str, object]) -> str:
    receipts = _repair_receipts(summary)
    for receipt in reversed(receipts):
        reason = str(receipt.get("reason") or "").strip()
        if reason:
            return reason
    for task in _repair_tasks(summary):
        reason = str(task.get("reason") or "").strip()
        if reason:
            return reason
    return ""


def _customer_friendly_repair_reason(summary: dict[str, object]) -> str:
    receipts = _repair_receipts(summary)
    latest_receipt = receipts[-1] if receipts else {}
    resolution = str(latest_receipt.get("resolution") or "").strip().lower()
    reason = _latest_repair_reason(summary).strip().lower()
    combined = " ".join(part for part in (resolution, reason) if part).strip()
    if not combined:
        return ""
    if (
        resolution == "suppressed_source_fetch_forbidden"
        or "blocked or rejected automated source fetch" in reason
        or "403" in combined
        or "forbidden" in combined
    ):
        return "This site stopped returning a listing page PropertyQuarry can read."
    if resolution == "suppressed_generic_listing_page" or "generic marketing or overview page" in reason:
        return "This site opened an overview page instead of one concrete home."
    if resolution == "provider_quarantined_retry_budget_exhausted" or "manual_provider_patch_required" in combined:
        return "This site changed enough that the search could not recover it automatically yet."
    if resolution == "suppressed_missing_location" or "lacks a concrete location" in reason:
        return "This site no longer returned a clear location for the home."
    if resolution == "suppressed_missing_price" or "lacks a concrete price" in reason:
        return "This site no longer returned a confirmed price for the home."
    if resolution == "suppressed_location_scope" or "conflicts with the active search location" in reason:
        return "This site no longer matched the saved search area cleanly."
    if resolution in {"worker_exception_restart_required", "stale_run_restart_required"}:
        return "The retry restarted the search, but it still did not reach a ready shortlist."
    if "provider page" in combined or "webpage" in combined or "extract" in combined:
        return "This site changed and the current check could not confirm the listing reliably."
    return ""


def _resolved_customer_repair_reason(
    summary: dict[str, object],
    *,
    message_value: object = "",
) -> str:
    friendly = _customer_friendly_repair_reason(summary).strip()
    if friendly:
        return friendly
    latest_reason = _latest_repair_reason(summary).strip().lower()
    message = str(message_value or "").strip().lower()
    combined = " ".join(part for part in (latest_reason, message) if part).strip()
    if not combined:
        return ""
    if (
        "suppressed_generic_listing_page" in combined
        or "generic listing page" in combined
        or "overview page" in combined
    ):
        return "This site opened an overview page instead of one concrete home."
    if (
        "suppressed_missing_location" in combined
        or "missing location" in combined
        or "lacks a concrete location" in combined
    ):
        return "This site no longer returned a clear location for the home."
    if (
        "suppressed_missing_price" in combined
        or "missing price" in combined
        or "lacks a concrete price" in combined
    ):
        return "This site no longer returned a confirmed price for the home."
    if (
        "suppressed_location_scope" in combined
        or "location scope" in combined
        or "wrong area" in combined
        or "conflicts with the active search location" in combined
    ):
        return "This site no longer matched the saved search area cleanly."
    if (
        "403" in combined
        or "forbidden" in combined
        or "provider returned" in combined
        or "while fetching" in combined
        or "fetch failed" in combined
        or "temporary fetch failed" in combined
        or "timeout" in combined
        or "timed out" in combined
        or "blocked" in combined
        or "webpage" in combined
        or "provider page" in combined
        or "extract" in combined
    ):
        return "This site changed and the current check could not confirm the listing reliably."
    return ""


def _join_customer_sentences(*parts: object) -> str:
    rows: list[str] = []
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        rows.append(text if text.endswith((".", "!", "?")) else f"{text}.")
    return " ".join(rows).strip()


def _customer_status_is_internal_failure_copy(value: object) -> bool:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return False
    return lowered.startswith(
        (
            "no source completed cleanly enough",
            "the search could not confirm a ready shortlist from the available listing pages",
            "the search could not confirm a ready shortlist from the available "
            "source pages",
            "search failed before ranking",
            "search paused before a stable shortlist was ready",
        )
    )


def _repair_reason_points_to_provider_site_change(reason: object) -> bool:
    lowered = str(reason or "").strip().lower()
    if not lowered:
        return False
    return any(
        token in lowered
        for token in (
            "source stopped returning",
            "site stopped returning",
            "overview page",
            "clear location",
            "confirmed price",
            "matched the saved search area",
            "confirm the listing reliably",
        )
    )


def _calm_customer_repair_copy(
    repair_reason: object,
    *,
    replacement_run_id: object = "",
    repair_active: bool = False,
    repair_failed: bool = False,
) -> str:
    if replacement_run_id and _repair_reason_points_to_provider_site_change(repair_reason):
        return "A site changed, so a fresh search is already running."
    if replacement_run_id:
        return "A fresh search is checking your saved brief."
    if repair_active and _repair_reason_points_to_provider_site_change(repair_reason):
        return "One site changed, so PropertyQuarry is retrying it."
    if repair_failed and _repair_reason_points_to_provider_site_change(repair_reason):
        return "One site changed, so this search could not finish automatically."
    return ""


def _latest_repair_timestamp(summary: dict[str, object]) -> str:
    receipts = _repair_receipts(summary)
    candidate_values = [summary.get("repair_last_updated_at")]
    if receipts:
        candidate_values.append(receipts[-1].get("at"))
    for value in candidate_values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _format_property_repair_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed_utc = parsed.astimezone(timezone.utc)
    return parsed_utc.strftime("%b %-d, %Y %H:%M UTC")


def property_run_event_is_internal_noise(event: dict[str, object]) -> bool:
    step = str(event.get("step") or "").strip().lower()
    message = str(event.get("message") or "").strip().lower()
    resolution = str(event.get("resolution") or "").strip().lower()
    if step == "repair_receipt" and (
        "suppressed_generic_listing_page" in message
        or resolution == "suppressed_generic_listing_page"
    ):
        return True
    if step != "repair_receipt" and _PROPERTY_RUN_SUPPRESSED_RESOLUTION_RE.search(message):
        return True
    if step == "status_refresh" and (
        "could not load property search status" in message
        or "checking run status" in message
    ):
        return True
    if "starting property search run" in message:
        return True
    return False


def _property_run_progress_fallback_message(summary: dict[str, object]) -> str:
    listing_work = _property_run_listing_work_counts(summary)
    found = listing_work["found"]
    to_review = listing_work["to_review"]
    source_work = _property_run_source_work_counts(summary)
    provider_total = _positive_int(
        summary.get("provider_total")
        or summary.get("source_variant_total")
        or summary.get("sources_total")
    )
    if found > 0:
        source_label = _property_run_provider_check_label(summary)
        source_left_label = _property_run_source_work_left_label(summary)
        if source_label and source_work["open"] > 0:
            if to_review > 0:
                return f"{_property_run_listing_queue_label(found, to_review)} · {source_left_label or source_label}"
            return f"{found} homes found · {source_left_label or source_label}"
        return _property_run_listing_queue_label(found, to_review)
    if provider_total > 0:
        return "Preparing search."
    return "Preparing search."


def _property_run_source_status_counts(summary: dict[str, object]) -> dict[str, int]:
    source_rows = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    done_statuses = {"completed", "processed", "done", "success", "repaired", "skipped", "failed", "error"}
    active_statuses = {"running", "processing", "in_progress", "working", "starting", "warming", "repairing"}
    status = str(summary.get("status") or "").strip().lower()
    terminal = status in {"processed", "completed", "completed_partial", "failed", "noop", "cancelled"}
    done = 0
    active = 0
    failed = 0
    for row in source_rows:
        status = str(row.get("status") or row.get("state") or "").strip().lower()
        has_error = bool(row.get("error"))
        if status in {"failed", "error"} or has_error:
            failed += 1
        if status in done_statuses or has_error:
            done += 1
        elif status in active_statuses:
            active += 1
    source_total = _positive_int(
        summary.get("source_variant_total")
        or summary.get("sources_total"),
        default=len(source_rows),
    )
    provider_total = _positive_int(summary.get("provider_total"))
    total = max(source_total, provider_total, len(source_rows))
    if terminal:
        completed_total = _positive_int(summary.get("sources_completed"))
        done = min(total, max(done, completed_total, source_total, provider_total))
        active = 0
        if status in {"failed", "cancelled"} and failed <= 0:
            failed = max(0, total - done)
    queued = max(0, total - done - active)
    return {
        "total": total,
        "provider_total": provider_total,
        "source_total": source_total,
        "done": done,
        "active": active,
        "queued": queued,
        "failed": failed,
        "rows": len(source_rows),
    }


def _property_run_source_work_counts(summary: dict[str, object], *, status: str = "") -> dict[str, int]:
    source_rows = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    done_statuses = {"completed", "processed", "done", "success", "repaired", "skipped", "failed", "error"}
    active_statuses = {"running", "processing", "in_progress", "working", "starting", "warming", "repairing"}
    total = max(
        _positive_int(summary.get("source_variant_total") or summary.get("sources_total")),
        len(source_rows),
    )
    counted_done = 0
    active = 0
    failed = 0
    explicit_source_state_total = 0
    for row in source_rows:
        row_status = str(row.get("status") or row.get("state") or "").strip().lower()
        has_error = bool(row.get("error"))
        if row_status or has_error:
            explicit_source_state_total += 1
        if row_status in done_statuses or has_error:
            counted_done += 1
        elif row_status in active_statuses:
            active += 1
        if row_status in {"failed", "error"} or has_error:
            failed += 1
    reported_done = _positive_int(summary.get("sources_completed") or summary.get("completed_sources"))
    run_status = str(status or summary.get("status") or "").strip().lower()
    terminal_run = run_status in {"processed", "completed", "completed_partial", "noop", "cancelled"}
    # Materialized source rows are preseeded before execution. While a run is
    # active, their explicit states are stronger evidence than an aggregate
    # counter that may have counted every queued row as completed.
    done = counted_done if explicit_source_state_total and not terminal_run else max(reported_done, counted_done)
    if total > 0:
        active = min(active, max(0, total - done))
    if total > 0 and terminal_run:
        done = total
        active = 0
    waiting = max(0, total - done - active)
    return {
        "total": total,
        "done": done,
        "active": active,
        "waiting": waiting,
        "open": active + waiting,
        "failed": failed,
        "rows": len(source_rows),
    }


def _property_run_provider_check_label(summary: dict[str, object], *, status: str = "") -> str:
    source_work = _property_run_source_work_counts(summary, status=status)
    provider_total = _positive_int(
        summary.get("provider_display_total")
        or summary.get("provider_total")
        or summary.get("source_variant_total")
        or summary.get("sources_total")
    )
    total = max(source_work["total"], provider_total)
    if total > 0 and source_work["total"] > 0:
        done = min(total, max(0, source_work["done"]))
        unit = _property_run_source_unit_label(summary, total=total)
        return f"{done} / {total} {unit}"
    if total > 0:
        return f"{total} sites chosen"
    return ""


def _property_run_source_work_left_label(summary: dict[str, object], *, status: str = "") -> str:
    source_work = _property_run_source_work_counts(summary, status=status)
    provider_total = _positive_int(
        summary.get("provider_display_total")
        or summary.get("provider_total")
        or summary.get("source_variant_total")
        or summary.get("sources_total")
    )
    total = max(source_work["total"], provider_total)
    if total <= 0 or source_work["open"] <= 0:
        return ""
    done = min(total, max(0, source_work["done"]))
    left = max(0, total - done)
    if left <= 0:
        return ""
    unit = _property_run_source_unit_label(summary, total=total)
    return f"{left} {unit} left"


def _property_run_source_unit_label(summary: dict[str, object], *, total: int = 0) -> str:
    source_variant_total = _positive_int(summary.get("source_variant_total") or summary.get("sources_total"))
    provider_total = _positive_int(summary.get("provider_display_total") or summary.get("provider_total"))
    if total > 0 and provider_total > 0 and total <= provider_total:
        return "providers"
    if source_variant_total > 0 and provider_total > 0 and source_variant_total > provider_total:
        return "checks"
    if total > 0 and provider_total > 0 and total > provider_total:
        return "checks"
    return "providers"


def _property_run_count_label(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _property_run_detail_queue_message(
    summary: dict[str, object],
    *,
    status: str = "",
    message: object = "",
) -> str:
    listing_work = _property_run_listing_work_counts(summary, status=status)
    found_total = listing_work["found"]
    if found_total <= 0:
        return ""
    prepared_total = _positive_int(summary.get("review_created_total")) + _positive_int(summary.get("review_existing_total"))
    raw_message = str(message or summary.get("message") or "").strip().lower()
    waiting_on_pages = (
        prepared_total <= 0
        and (
            "review page preparation timed out" in raw_message
            or "reviewing candidate" in raw_message
            or "source_review_packet" == str(summary.get("current_step") or "").strip().lower()
        )
    )
    if not waiting_on_pages:
        return ""
    source_work = _property_run_source_work_counts(summary, status=status)
    source_label = _property_run_provider_check_label(summary, status=status)
    source_left_label = _property_run_source_work_left_label(summary, status=status)
    parts = [
        _property_run_count_label(found_total, "home") + " found",
        "home details are still loading",
    ]
    if source_work["open"] > 0 and source_label:
        parts.append(source_left_label or source_label)
    return " · ".join(parts)


def _property_run_synthetic_progress_events(
    payload: dict[str, object],
    *,
    summary: dict[str, object],
    status: str,
) -> list[dict[str, object]]:
    counts = _property_run_source_status_counts(summary)
    provider_total = counts["provider_total"] or counts["total"]
    listing_work = _property_run_listing_work_counts(summary, status=status)
    reviewed = listing_work["scanned"]
    raw_found = listing_work["found"]
    ranked = _positive_int(
        summary.get("ranked_total")
        or summary.get("shortlist_total")
        or summary.get("ranked_candidate_total")
        or len(list(summary.get("ranked_candidates") or []))
    )
    held_back = _positive_int(summary.get("held_back_total") or summary.get("filtered_total"))
    current_step = str(payload.get("current_step") or summary.get("current_step") or "status_refresh").strip() or "status_refresh"
    timestamp = str(payload.get("updated_at") or payload.get("generated_at") or "")
    events: list[dict[str, object]] = []

    def add(step: str, message: str) -> None:
        text = " ".join(str(message or "").split()).strip()
        if not text:
            return
        events.append(
            {
                "step": step,
                "status": status,
                "message": text,
                "created_at": timestamp,
            }
            )

    if provider_total > 0:
        add("sources_resolved", f"{_property_run_count_label(provider_total, 'provider')} selected for this search.")

    source_work = _property_run_source_work_counts(summary, status=status)
    if counts["rows"] or counts["done"] or counts["active"]:
        parts: list[str] = []
        if counts["done"]:
            parts.append(f"{counts['done']} checked")
        if counts["active"]:
            parts.append(f"{counts['active']} running")
        if counts["queued"]:
            parts.append(f"{counts['queued']} waiting")
        if counts["failed"]:
            parts.append(f"{counts['failed']} need follow-up")
        if parts:
            total_suffix = f" of {counts['total']}" if counts["total"] else ""
            unit = _property_run_source_unit_label(summary, total=counts["total"]).capitalize()
            add("source_search", f"{unit}: {', '.join(parts)}{total_suffix}.")
    elif provider_total > 0:
        add("source_started", "Looking for the first homes.")

    found_total = raw_found
    to_review = listing_work["to_review"]
    if found_total > 0:
        source_label = _property_run_provider_check_label(summary, status=status)
        source_left_label = _property_run_source_work_left_label(summary, status=status)
        if source_work["open"] > 0 and source_label:
            if to_review > 0:
                add("source_fetch", f"{_property_run_listing_queue_label(found_total, to_review)} · {source_left_label or source_label}.")
            else:
                add("source_fetch", f"{found_total} homes found · {source_left_label or source_label}.")
        else:
            add("source_fetch", f"{_property_run_listing_queue_label(found_total, to_review)}.")
    detail_queue_message = _property_run_detail_queue_message(summary, status=status, message=payload.get("message"))
    if detail_queue_message:
        add("source_review_packet", detail_queue_message + ".")

    if ranked > 0:
        add("source_shortlist", f"{_property_run_count_label(ranked, 'matching home')} ready.")
    elif held_back > 0:
        add("source_area_filter", f"{_property_run_count_label(held_back, 'home')} outside this search.")

    current_message = _property_run_current_progress_message(payload, summary=summary, status=status)
    add(current_step, current_message)
    return events


def _property_run_current_progress_message(
    payload: dict[str, object],
    *,
    summary: dict[str, object],
    status: str,
) -> str:
    raw_message = payload.get("message") or summary.get("message") or summary.get("status_note") or ""
    safe_message = property_run_customer_safe_status_detail(
        status,
        raw_message,
        summary=summary,
        prefer_repair_step=True,
    )
    repair_status = str(summary.get("repair_status") or summary.get("repair_status_label") or "").strip().lower()
    if safe_message and (status in {"failed", "completed_partial"} or repair_status):
        return safe_message
    detail_queue_message = _property_run_detail_queue_message(summary, status=status, message=raw_message)
    if detail_queue_message:
        return detail_queue_message
    if safe_message == "Checking search pages.":
        return safe_message

    step = str(payload.get("current_step") or summary.get("current_step") or "").strip().lower()
    if step == "provider_scan":
        return safe_message or "Checking search pages."
    live_board = build_property_run_live_board_snapshot(payload, plan_key="free")
    provider_label = _compact_property_provider_label(
        live_board.get("provider_full_label") or live_board.get("provider_label") or ""
    )
    fraction_label = str(live_board.get("fraction_label") or "").strip()
    phase_label = str(live_board.get("phase_label") or "").strip()
    aggregate_label = str(live_board.get("aggregate_label") or "").strip()
    generic_waiting_phase = phase_label.lower() in {
        "waiting for the first provider update.",
        "waiting for the first list.",
        "waiting for the first provider.",
    }

    if step in {"queued", "starting", "sources_resolved", "source_catalog_loading", "source_started"}:
        return _property_run_progress_fallback_message(summary)

    progress_parts: list[str] = []
    if provider_label and provider_label.lower() not in {"provider", "ready", "waiting", "preparing"}:
        progress_parts.append(provider_label)
    if fraction_label:
        progress_parts.append(fraction_label)
    if aggregate_label and aggregate_label.lower() not in {"checking"}:
        progress_parts.append(aggregate_label)

    if step in {"source_ranking", "source_shortlist", "source_assessing", "source_low_fit_ranked", "source_discovery_penalty"}:
        if phase_label.lower().startswith("shortlist ready"):
            return phase_label if not progress_parts else f"{phase_label} · {' · '.join(progress_parts)}"
        if progress_parts:
            return " · ".join(progress_parts)
    if step in {"source_fetching", "source_extracting", "source_preview_prepare", "source_previewing", "source_rank_prep"}:
        if progress_parts:
            return " · ".join(progress_parts)
    if step in {"shortlist_ready", "source_review_packet", "source_completed"} and phase_label and not generic_waiting_phase:
        return phase_label if not progress_parts else f"{phase_label} · {' · '.join(progress_parts)}"
    if phase_label and not generic_waiting_phase:
        return phase_label if not progress_parts else f"{phase_label} · {' · '.join(progress_parts)}"
    if progress_parts:
        return " · ".join(progress_parts)
    return safe_message or _property_run_progress_fallback_message(summary)


def _property_run_repair_receipt_message(receipt: dict[str, object]) -> str:
    stub_summary = {"repair_receipts": [receipt]}
    friendly = _resolved_customer_repair_reason(
        stub_summary,
        message_value=receipt.get("resolution") or receipt.get("reason") or "",
    )
    if _repair_reason_points_to_provider_site_change(friendly):
        return "A site changed, so it was skipped for now."
    resolution = str(receipt.get("resolution") or receipt.get("reason") or "repair updated").strip()
    if not resolution:
        return ""
    return "A site needed a retry."


def _property_run_event_resolution(event: dict[str, object]) -> str:
    resolution = str(event.get("resolution") or "").strip().lower()
    if resolution:
        return resolution
    message = str(event.get("message") or "").strip().lower()
    match = _PROPERTY_RUN_SUPPRESSED_RESOLUTION_RE.search(message)
    if match:
        return str(match.group(0) or "").strip().lower()
    return ""


def _customer_safe_repair_step_label(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    text = re.sub(r"\bRetrying\s+one\s+provider\b", "Refreshing one provider", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bRetrying\s+(\d+)\s+provider checks?\b",
        lambda match: f"Refreshing {match.group(1)} provider{'' if match.group(1) == '1' else 's'}",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bRetrying\s+(.+?)\s+provider checks?\.?$",
        lambda match: f"Refreshing {str(match.group(1) or '').strip()}.",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bprovider checks?\b", "providers", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsources\b", "providers", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsource\b", "provider", text, flags=re.IGNORECASE)
    return text.rstrip(".")


def _customer_safe_source_status_text(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    text = re.sub(r"\bRetrying\s+one\s+provider\b", "Refreshing one provider", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bRetrying\s+(\d+)\s+providers?\b",
        lambda match: f"Refreshing {match.group(1)} provider{'' if match.group(1) == '1' else 's'}",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bprovider checks?\b", "providers", text, flags=re.IGNORECASE)
    text = re.sub(r"\bprovider or rule\b", "provider or rule", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsources\b", "providers", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsource\b", "provider", text, flags=re.IGNORECASE)
    return text


def _property_run_customer_event_message(
    event: dict[str, object],
    *,
    summary: dict[str, object],
    status: str,
) -> str:
    step = str(event.get("step") or "").strip().lower()
    resolution = _property_run_event_resolution(event)
    raw_message = str(event.get("message") or "").strip()
    if step == "repair_receipt":
        if resolution == "suppressed_generic_listing_page":
            return ""
        if status not in {"failed", "completed_partial"}:
            return ""
        receipt = dict(event)
        if resolution and not str(receipt.get("resolution") or "").strip():
            receipt["resolution"] = resolution
        return _property_run_repair_receipt_message(receipt)
    safe_message = property_run_customer_safe_status_detail(
        event.get("status") or event.get("step") or status,
        raw_message,
        summary=summary,
        prefer_repair_step=True,
    )
    if resolution.startswith("suppressed_"):
        return _compact_run_message(safe_message)
    return _compact_run_message(safe_message or raw_message)


def property_run_customer_visible_events(
    *,
    run_payload: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    payload = dict(run_payload or {})
    summary = dict(payload.get("summary") or {}) if isinstance(payload.get("summary"), dict) else {}
    status = str(payload.get("status") or summary.get("status") or "in_progress").strip() or "in_progress"
    synthetic_events = _property_run_synthetic_progress_events(payload, summary=summary, status=status)

    def _dedup_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
        deduped_reversed: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for event in reversed(events):
            message = " ".join(str(event.get("message") or "").split()).strip()
            if not message:
                continue
            step = str(event.get("step") or "").strip().lower()
            key = (step, message.lower())
            if key in seen:
                continue
            row = dict(event)
            row["message"] = message
            seen.add(key)
            deduped_reversed.append(row)
        return list(reversed(deduped_reversed))

    def _current_progress_event() -> dict[str, object]:
        step = str(payload.get("current_step") or summary.get("current_step") or "status_refresh").strip() or "status_refresh"
        message = _property_run_current_progress_message(payload, summary=summary, status=status)
        return {
            "step": step,
            "status": status,
            "message": message,
            "created_at": str(payload.get("updated_at") or payload.get("generated_at") or ""),
        }

    existing_events = [dict(item) for item in list(payload.get("events") or []) if isinstance(item, dict)]
    if existing_events:
        visible_events: list[dict[str, object]] = []
        for event in existing_events:
            if property_run_event_is_internal_noise(event):
                continue
            row = dict(event)
            row["message"] = _property_run_customer_event_message(row, summary=summary, status=status)
            if not str(row.get("message") or "").strip():
                continue
            visible_events.append(row)
        current_event = _current_progress_event()
        if not visible_events:
            return _dedup_events([current_event])[-_PROPERTY_RUN_VISIBLE_EVENT_LIMIT:]
        if current_event.get("message"):
            latest_visible = visible_events[-1] if visible_events else {}
            latest_step = str(latest_visible.get("step") or "").strip().lower()
            latest_message = str(latest_visible.get("message") or "").strip().lower()
            current_step = str(current_event.get("step") or "").strip().lower()
            current_message = str(current_event.get("message") or "").strip().lower()
            if current_message and (current_step != latest_step or current_message != latest_message):
                visible_events.append(current_event)
        if visible_events:
            return _dedup_events(visible_events)[-_PROPERTY_RUN_VISIBLE_EVENT_LIMIT:]
    if status not in {"failed", "completed_partial"}:
        return _dedup_events(synthetic_events or [_current_progress_event()])[-_PROPERTY_RUN_VISIBLE_EVENT_LIMIT:]
    replacement_run_id = str(summary.get("repair_replacement_run_id") or "").strip()
    if replacement_run_id:
        return _dedup_events(
            [
                {
                    "step": "repair_status",
                    "status": str(summary.get("repair_status") or "repairing").strip() or "repairing",
                    "message": "A fresh search is checking your saved brief.",
                    "created_at": str(
                        summary.get("repair_last_updated_at")
                        or payload.get("updated_at")
                        or payload.get("generated_at")
                        or ""
                    ),
                }
            ]
        )[-_PROPERTY_RUN_VISIBLE_EVENT_LIMIT:]
    synthesized_events: list[dict[str, object]] = []
    repair_label = str(summary.get("repair_status_label") or summary.get("repair_status") or "").strip()
    repair_step = _customer_safe_repair_step_label(summary.get("repair_step_label"))
    if repair_label or repair_step:
        synthesized_events.append(
            {
                "step": "repair_status",
                "status": str(summary.get("repair_status") or "repairing").strip() or "repairing",
                "message": "Refreshing affected providers.",
                "created_at": str(summary.get("repair_last_updated_at") or payload.get("updated_at") or payload.get("generated_at") or ""),
            }
        )
    for receipt in [dict(item) for item in list(summary.get("repair_receipts") or []) if isinstance(item, dict)][-3:]:
        resolution = str(receipt.get("resolution") or receipt.get("reason") or "repair updated").strip()
        if resolution.strip().lower() == "suppressed_generic_listing_page":
            continue
        message = _property_run_repair_receipt_message(receipt)
        if not message:
            continue
        synthesized_events.append(
            {
                "step": "repair_receipt",
                "status": "repaired",
                "message": message,
                "created_at": str(receipt.get("at") or payload.get("updated_at") or payload.get("generated_at") or ""),
            }
        )
    return _dedup_events([*synthetic_events, *synthesized_events] or [_current_progress_event()])[-_PROPERTY_RUN_VISIBLE_EVENT_LIMIT:]


def build_property_run_health_snapshot(
    run_payload: dict[str, object],
    *,
    run_summary: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = dict(run_payload or {})
    summary = (
        dict(run_summary or {})
        if isinstance(run_summary, dict)
        else (dict(payload.get("summary") or {}) if isinstance(payload.get("summary"), dict) else {})
    )
    status = str(payload.get("status") or summary.get("status") or "not_started").strip().lower() or "not_started"
    raw_message = str(payload.get("message") or summary.get("message") or "").strip()
    customer_safe_message = property_run_customer_safe_status_detail(status, raw_message, summary=summary, prefer_repair_step=True)
    status_label, status_note = property_run_status_copy(status, customer_safe_message)
    if status in {"queued", "starting", "running", "in_progress", "processing", "scanning"}:
        status_note = _property_run_summary_message(payload, summary)
    held_back_total = _positive_int(summary.get("held_back_total"))
    if not held_back_total:
        held_back_total = (
            _positive_int(summary.get("filtered_floorplan_total"))
            + _positive_int(summary.get("filtered_area_total"))
            + _positive_int(summary.get("filtered_property_type_total"))
            + _positive_int(summary.get("filtered_availability_total"))
            + _positive_int(summary.get("filtered_generic_page_total"))
            + _positive_int(summary.get("filtered_listing_mode_total"))
        )
    filtered_total = _positive_int(summary.get("filtered_total"), default=held_back_total or 0)
    return PropertyRunHealthSnapshot(
        run_id=str(payload.get("run_id") or "").strip(),
        status=status,
        status_label=status_label,
        status_note=status_note,
        message=status_note or raw_message,
        progress=_positive_int(payload.get("progress")),
        status_url=str(payload.get("status_url") or "").strip(),
        eta_label=property_run_public_eta_label(payload.get("eta_label") or summary.get("eta_label")),
        in_progress=status not in {"processed", "completed", "completed_partial", "failed", "noop", "cancelled", "not_started", "not started"},
        source_total=_positive_int(summary.get("sources_total")),
        listing_total=_positive_int(summary.get("listing_total") or summary.get("raw_listing_total")),
        filtered_total=filtered_total,
        held_back_total=held_back_total,
        research_task_total=_positive_int(payload.get("research_task_total") or summary.get("research_task_total")),
        open_research_task_total=_positive_int(payload.get("open_research_task_total") or summary.get("open_research_task_total")),
        filled_research_task_total=_positive_int(payload.get("filled_research_task_total") or summary.get("filled_research_task_total")),
        dismissed_research_task_total=_positive_int(payload.get("dismissed_research_task_total") or summary.get("dismissed_research_task_total")),
    ).to_dict()


def build_property_run_repair_snapshot(
    run_payload: dict[str, object],
    *,
    results_total: int = 0,
) -> dict[str, object]:
    payload = dict(run_payload or {})
    summary = dict(payload.get("summary") or {}) if isinstance(payload.get("summary"), dict) else {}
    timing = dict(summary.get("timing_receipts") or {}) if isinstance(summary.get("timing_receipts"), dict) else {}
    status = str(payload.get("status") or summary.get("status") or "queued").strip().lower() or "queued"
    progress = max(0, min(100, _positive_int(payload.get("progress") or summary.get("progress"))))
    source_rows = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    failed_total = 0
    for row in source_rows:
        row_status = str(row.get("status") or row.get("state") or "").strip().lower()
        if row_status in {"failed", "error", "skipped"} or row.get("error"):
            failed_total += 1
    repair_step = str(summary.get("repair_step_label") or "").strip()
    if not repair_step and failed_total:
        repair_step = f"Refreshing {failed_total} provider{'s' if failed_total != 1 else ''}"
    next_useful_eta = str(summary.get("next_useful_update_eta_label") or "").strip()
    if not next_useful_eta and timing.get("first_shortlist_ready_at") and results_total > 0:
        next_useful_eta = "new shortlist already ready"
    next_useful_eta = property_run_public_eta_label(next_useful_eta)
    final_eta = property_run_public_eta_label(payload.get("eta_label") or summary.get("eta_label"))
    eta_confidence = str(summary.get("eta_confidence") or "").strip().lower()
    if not eta_confidence:
        if final_eta and progress >= 20:
            eta_confidence = "medium"
        elif final_eta:
            eta_confidence = "low"
        else:
            eta_confidence = "unknown"
    repair_flags = _repair_summary_flags(summary)
    latest_repair_reason = _resolved_customer_repair_reason(summary, message_value=payload.get("message"))
    calm_repair_copy = _calm_customer_repair_copy(
        latest_repair_reason,
        replacement_run_id=repair_flags.get("replacement_run_id"),
        repair_active=bool(repair_flags.get("active")),
        repair_failed=bool(repair_flags.get("failed")),
    )
    latest_repair_timestamp = _format_property_repair_timestamp(_latest_repair_timestamp(summary))
    repair_status = str(summary.get("repair_status") or "").strip().lower()
    if not repair_status:
        if status == "completed_partial":
            repair_status = "degraded"
        elif repair_flags["failed"]:
            repair_status = "failed"
        elif repair_flags["active"] or failed_total:
            repair_status = "repairing"
        else:
            repair_status = "stable"
    repair_status_label = str(summary.get("repair_status_label") or "").strip()
    if not repair_status_label:
        if repair_status == "repairing":
            repair_status_label = "Checking again"
        elif repair_status == "degraded":
            repair_status_label = "Partial coverage"
        elif repair_status == "failed":
            repair_status_label = "Needs attention"
        else:
            repair_status_label = "Stable"
    repair_outcome_summary = str(summary.get("repair_outcome_summary") or "").strip()
    if repair_outcome_summary:
        repair_outcome_summary = _customer_safe_source_status_text(repair_outcome_summary)
    if not repair_outcome_summary:
        if repair_flags["failed"]:
            repair_outcome_summary = (
                property_run_customer_safe_status_detail(status, payload.get("message") or "", summary=summary)
                or calm_repair_copy
                or "The search stopped before a ready shortlist was available."
            )
            if latest_repair_timestamp:
                repair_outcome_summary = f"{repair_outcome_summary} Last real update: {latest_repair_timestamp}."
        elif status == "completed_partial":
            repair_outcome_summary = "The shortlist is ready, but one or more providers finished with partial coverage."
        elif failed_total and results_total > 0:
            repair_outcome_summary = _join_customer_sentences(
                "Some providers are retrying, but the current shortlist is still available",
                latest_repair_reason,
            )
        elif failed_total:
            repair_outcome_summary = _join_customer_sentences(
                "Some providers are retrying before the shortlist can settle",
                latest_repair_reason,
            )
    if repair_flags["failed"] and not repair_step:
        repair_step = "The fresh check finished without a ready shortlist."
    if repair_flags["failed"] and not next_useful_eta:
        next_useful_eta = "start a fresh run after changing one site or rule"
    repair_outcome_summary = _customer_safe_source_status_text(repair_outcome_summary)
    return PropertyRunRepairSnapshot(
        repair_status=repair_status,
        repair_status_label=repair_status_label,
        repair_step_label=repair_step,
        repair_outcome_summary=repair_outcome_summary,
        repair_class=str(summary.get("repair_class") or "").strip(),
        repair_attempt_count=_positive_int(summary.get("repair_attempt_count")),
        eta_confidence_label=eta_confidence.title() if eta_confidence else "Unknown",
        next_useful_update_eta_label=next_useful_eta,
        can_auto_repair=bool(summary.get("can_auto_repair") or failed_total or repair_status in {"repairing", "degraded"}),
    ).to_dict()


def build_property_run_reliability_snapshot(
    run_payload: dict[str, object],
    *,
    results_total: int = 0,
) -> dict[str, object]:
    payload = dict(run_payload or {})
    summary = dict(payload.get("summary") or {}) if isinstance(payload.get("summary"), dict) else {}
    status = str(payload.get("status") or summary.get("status") or "queued").strip().lower() or "queued"
    progress = max(0, min(100, _positive_int(payload.get("progress") or summary.get("progress"))))
    message = str(payload.get("message") or "").strip()
    source_rows = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    source_total = max(0, _positive_int(summary.get("sources_total"), default=len(source_rows)))
    source_checked = len(source_rows)
    listing_work = _property_run_listing_work_counts(summary, status=status)
    listing_total = listing_work["found"]
    to_review_total = listing_work["to_review"]
    filtered_total = max(0, _property_summary_held_back_total(summary))
    failed_total = 0
    for row in source_rows:
        row_status = str(row.get("status") or row.get("state") or "").strip().lower()
        if row_status in {"failed", "error", "skipped"} or row.get("error"):
            failed_total += 1
    pending_total = max(0, source_total - source_checked)
    repair = build_property_run_repair_snapshot(payload, results_total=results_total)
    repair_reason = _resolved_customer_repair_reason(summary, message_value=message)
    repair_flags = _repair_summary_flags(summary)
    calm_repair_copy = _calm_customer_repair_copy(
        repair_reason,
        replacement_run_id=repair_flags.get("replacement_run_id"),
        repair_active=bool(repair_flags.get("active")),
        repair_failed=bool(repair_flags.get("failed")),
    )
    customer_status = ""
    if status in {"failed", "completed_partial"} or failed_total or str(summary.get("repair_status") or "").strip():
        customer_status = property_run_customer_safe_status_detail(status, message, summary=summary)
    if customer_status and _repair_reason_points_to_provider_site_change(repair_reason):
        customer_status = customer_status.replace(f" {repair_reason}", "").replace(repair_reason, "").strip()
    if _customer_status_is_internal_failure_copy(customer_status) and calm_repair_copy:
        customer_status = calm_repair_copy
    if not customer_status:
        customer_status = str(summary.get("customer_status_message") or "").strip()
    if not customer_status:
        if status in {"processed", "completed"}:
            customer_status = "Search finished cleanly."
        elif status == "completed_partial":
            customer_status = message or "Search finished with partial coverage after one or more providers needed retrying."
        elif status == "failed":
            if str(summary.get("repair_replacement_run_id") or "").strip():
                customer_status = _join_customer_sentences(
                    "A fresh search is checking your saved brief",
                    repair_reason,
                )
            elif calm_repair_copy:
                customer_status = calm_repair_copy
            elif repair_reason:
                customer_status = "PropertyQuarry is checking the saved search again."
            else:
                customer_status = message or "Search interrupted before the final pass completed."
        elif failed_total and results_total > 0:
            customer_status = _join_customer_sentences(
                "Some providers are retrying, but the current shortlist is still available",
                "" if _repair_reason_points_to_provider_site_change(repair_reason) else repair_reason,
            )
        elif failed_total:
            customer_status = _join_customer_sentences(
                "Some providers are retrying before the shortlist can settle",
                "" if _repair_reason_points_to_provider_site_change(repair_reason) else repair_reason,
            )
        elif results_total > 0:
            customer_status = "The strongest matches are already ready while the rest of the search finishes."
        elif source_checked > 0:
            customer_status = "Search providers are still running and the first shortlist is still building."
        else:
            customer_status = message or "Preparing search."
    customer_status = _customer_safe_source_status_text(customer_status)
    if status in {"processed", "completed"}:
        health_tone = "good"
        health_label = "Healthy"
    elif status == "completed_partial":
        health_tone = "warn"
        health_label = "Partial coverage"
    elif status == "failed":
        health_tone = "bad"
        health_label = "Interrupted"
    elif failed_total:
        health_tone = "warn"
        health_label = "Checking again"
    elif source_checked > 0 or progress > 0:
        health_tone = "good"
        health_label = "Working"
    else:
        health_tone = "idle"
        health_label = "Starting"
    coverage_label = ""
    if source_total:
        coverage_label = f"{source_checked}/{source_total} providers"
        if pending_total:
            coverage_label += f" · {pending_total} still running"
    result_label = ""
    if results_total > 0:
        result_label = f"{results_total} matching result{'s' if results_total != 1 else ''} ready"
    elif listing_total > 0:
        result_label = _property_run_listing_queue_label(listing_total, to_review_total)
    filtered_label = ""
    if filtered_total > 0:
        filtered_label = f"{filtered_total} outside this search"
    final_eta_label = property_run_public_eta_label(payload.get("eta_label") or summary.get("eta_label"))
    return PropertyRunReliabilitySnapshot(
        health_label=health_label,
        health_tone=health_tone,
        coverage_label=coverage_label,
        result_label=result_label,
        filtered_label=filtered_label,
        repair_step_label=str(repair.get("repair_step_label") or "").strip(),
        next_useful_update_eta_label=str(repair.get("next_useful_update_eta_label") or "").strip(),
        final_eta_label=final_eta_label,
        eta_confidence_label=str(repair.get("eta_confidence_label") or "Unknown").strip() or "Unknown",
        customer_status_message=customer_status,
        repair=repair,
    ).to_dict()


def _compact_run_message(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "Looking for the first homes."
    stale_zero_match = _STALE_ZERO_REVIEW_SEARCH_PAGES_RE.search(text)
    if stale_zero_match:
        found = _positive_int(stale_zero_match.group("found"))
        done = _positive_int(stale_zero_match.group("done"))
        total = _positive_int(stale_zero_match.group("total"))
        left = max(0, total - done)
        if left > 0:
            unit = "check" if left == 1 else "checks"
            return f"{found} homes found · {left} {unit} left"
        return f"{found} homes found · all found homes are checked"
    text = re.sub(
        r"^Reviewing homes\.\s+(\d+)\s+checked so far\.?$",
        r"\1 homes checked",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b(\d+)\s+homes?\s+reviewed\b", r"\1 homes found", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(\d+)\s+reviewed so far\b", r"\1 homes checked", text, flags=re.IGNORECASE)
    text = re.sub(r"\bleft to sort\b", "to review", text, flags=re.IGNORECASE)
    text = re.sub(r"\blists left\b", "checks waiting", text, flags=re.IGNORECASE)
    text = re.sub(r"\blists open\b", "checks still running", text, flags=re.IGNORECASE)
    text = re.sub(r"\blist update\b", "search update", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwaiting for the first provider update\.?\b", "Looking for the first homes.", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwaiting for the first provider\.?\b", "Looking for the first homes.", text, flags=re.IGNORECASE)
    candidate_match = re.search(r"(?:Reviewing(?: candidate)?|Scoring enriched candidate|Ranked|Scored)\s+(\d+)\s+(?:of)\s+(\d+)", text, flags=re.IGNORECASE)
    if candidate_match:
        return f"Checking home details {candidate_match.group(1)} / {candidate_match.group(2)}"
    shortlist_match = re.search(r"^Built shortlist of\s+\d+\s+listing\(s\)\s+for\s+(.+)\.$", text, flags=re.IGNORECASE)
    if shortlist_match:
        return "Shortlist ready"
    return text


def _canonical_property_run_source_label(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+with\s+\d+\s+raw listing candidate\(s\)\.?$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+with\s+\d+\s+listing candidate\(s\)\.?$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+with\s+\d+\s+listing preview\(s\)\.?$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+with\s+\d+\s+preview\(s\)\.?$", "", text, flags=re.IGNORECASE)
    return text.rstrip(". ").strip()


def _parse_property_run_message_info(value: object) -> dict[str, str]:
    text = str(value or "").strip()
    if not text:
        return {
            "raw": "",
            "fraction_label": "",
            "source_label": "",
            "phase_label": "Looking for the first homes.",
        }
    provider_load_match = re.search(r"^Loading provider page for\s+(.+?)\.?$", text, flags=re.IGNORECASE)
    if provider_load_match:
        return {
            "raw": text,
            "fraction_label": "",
            "source_label": _canonical_property_run_source_label(provider_load_match.group(1)),
            "phase_label": _compact_run_message(text),
        }
    provider_loaded_match = re.search(r"^Loaded provider page for\s+(.+?)\s+with\s+\d+\s+raw listing candidate\(s\)\.?$", text, flags=re.IGNORECASE)
    if provider_loaded_match:
        return {
            "raw": text,
            "fraction_label": "",
            "source_label": _canonical_property_run_source_label(provider_loaded_match.group(1)),
            "phase_label": _compact_run_message(text),
        }
    provider_failed_match = re.search(r"^Could not load provider page for\s+(.+?)\.?$", text, flags=re.IGNORECASE)
    if provider_failed_match:
        return {
            "raw": text,
            "fraction_label": "",
            "source_label": _canonical_property_run_source_label(provider_failed_match.group(1)),
            "phase_label": _compact_run_message(text),
        }
    compact_lines = [line.strip() for line in re.split(r"[\r\n]+", text) if str(line or "").strip()]
    for line in reversed(compact_lines):
        segments = [segment.strip() for segment in line.split("·") if segment.strip()]
        if len(segments) < 3:
            continue
        fraction_match = re.fullmatch(r"(\d+)\s*/\s*(\d+)", segments[-1])
        if not fraction_match:
            continue
        prefix_text = " ".join(segments[:-2]).lower()
        if not any(token in prefix_text for token in ("provider", "homes found", "homes reviewed", "to review", "left to sort", "checking")):
            continue
        return {
            "raw": text,
            "fraction_label": f"{fraction_match.group(1)} / {fraction_match.group(2)}",
            "source_label": _canonical_property_run_source_label(segments[-2]),
            "phase_label": _compact_run_message(line),
        }
    preview_match = re.search(r"^(?:Checking listing previews from|Prepared \d+ listing preview\(s\) from|Prepared preview queue for)\s+(.+?)\.?$", text, flags=re.IGNORECASE)
    if preview_match:
        return {
            "raw": text,
            "fraction_label": "",
            "source_label": _canonical_property_run_source_label(preview_match.group(1)),
            "phase_label": _compact_run_message(text),
        }
    source_match = re.search(r"\sfor\s+(.+?)\.?$", text, flags=re.IGNORECASE)
    candidate_match = re.search(r"^(Reviewing(?: candidate)?|Scoring enriched candidate|Ranked|Scored)\s+(\d+)\s+(?:of)\s+(\d+)", text, flags=re.IGNORECASE)
    if candidate_match:
        verb = str(candidate_match.group(1) or "").strip().lower()
        phase_label = (
            "Property fit"
            if verb.startswith("review")
            else ("Ranking" if verb.startswith("scor") else "Shortlist")
        )
        return {
            "raw": text,
            "fraction_label": f"{candidate_match.group(2)} / {candidate_match.group(3)}",
            "source_label": _canonical_property_run_source_label(source_match.group(1) if source_match else ""),
            "phase_label": phase_label,
        }
    enrich_match = re.search(r"^Enriching top\s+(\d+)\s+candidate\(s\)\s+out of\s+(\d+)\s+for\s+(.+?)\s+before final shortlist scoring\.?$", text, flags=re.IGNORECASE)
    if enrich_match:
        return {
            "raw": text,
            "fraction_label": f"{enrich_match.group(1)} / {enrich_match.group(2)}",
            "source_label": _canonical_property_run_source_label(enrich_match.group(3)),
            "phase_label": "Preparing shortlist",
        }
    shortlist_match = re.search(r"^Built shortlist of\s+(\d+)\s+listing\(s\)\s+for\s+(.+)\.$", text, flags=re.IGNORECASE)
    if shortlist_match:
        shortlist_total = str(shortlist_match.group(1) or "").strip() or "0"
        return {
            "raw": text,
            "fraction_label": "",
            "source_label": _canonical_property_run_source_label(shortlist_match.group(2)),
            "phase_label": f"Shortlist ready · {shortlist_total} home{'' if shortlist_total == '1' else 's'}",
        }
    prepared_match = re.search(r"^Prepared property page for\s+.+\.$", text, flags=re.IGNORECASE)
    if prepared_match:
        return {
            "raw": text,
            "fraction_label": "",
            "source_label": "",
            "phase_label": "Preparing property pages",
        }
    completed_match = re.search(r"^Completed scanning\s+(.+?)\.?$", text, flags=re.IGNORECASE)
    if completed_match:
        return {
            "raw": text,
            "fraction_label": "",
            "source_label": _canonical_property_run_source_label(completed_match.group(1)),
            "phase_label": "Site batch finished",
        }
    return {
        "raw": text,
        "fraction_label": "",
        "source_label": _canonical_property_run_source_label(source_match.group(1) if source_match else ""),
        "phase_label": _compact_run_message(text),
    }


def _latest_property_run_fraction_info(run_payload: dict[str, object]) -> dict[str, str]:
    current = _parse_property_run_message_info(run_payload.get("message"))
    if current.get("fraction_label"):
        return current
    events = list(run_payload.get("events") or [])
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        parsed = _parse_property_run_message_info(event.get("message"))
        if parsed.get("fraction_label"):
            return parsed
    return current


def _property_run_candidate_reason_label(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = re.search(r"candidate\s+(\d+)\s+of\s+(\d+)", text, flags=re.IGNORECASE)
    if not candidate:
        return ""
    ordinal = f"{candidate.group(1)}/{candidate.group(2)}"
    normalized = text.lower()
    concrete_distance_label = _property_run_distance_phase_label_from_text(text)
    if concrete_distance_label:
        return concrete_distance_label
    measured_distance_match = re.search(r"\b\d+(?:[.,]\d+)?\s*(?:m|meter|metre|km|kilometer|kilometre)\b", text, flags=re.IGNORECASE)
    if any(token in normalized for token in ("balcony", "terrace", "outdoor")) and any(
        token in normalized for token in ("missing", "none", "without", "absent", "no ")
    ):
        return f"Outdoor space is missing on candidate {ordinal}"
    positive_signals = (
        (("balcony", "terrace", "outdoor"), "Outdoor space found"),
        (("lift", "elevator", "barrier-free", "barrier free", "accessible"), "Access detail found"),
        (("operating cost", "monthly cost", "total cost", "betriebskosten"), "Cost detail found"),
        (("heating", "heizung"), "Heating detail found"),
        (("energy", "energy certificate", "energieausweis"), "Energy detail found"),
        (("internet", "broadband", "fiber", "fibre", "high-speed"), "Internet detail found"),
        (("floorplan", "layout"), "Layout detail found"),
        (("360", "matterport", "3dvista", "virtual tour", "live tour"), "Remote-view detail ready"),
        (("garage", "parking"), "Parking detail found"),
        (("commute", "transit", "subway", "u-bahn", "underground", "train"), "Transit detail found"),
        (("school", "volksschule"), "School fit found"),
        (("kindergarten", "childcare"), "Childcare fit found"),
        (("supermarket", "pharmacy", "bakery", "market", "errand"), "Daily errands nearby"),
        (("sunlight", "bright", "orientation", "south-facing"), "Light and orientation detail found"),
    )
    for tokens, label in positive_signals:
        if any(token in normalized for token in tokens) and any(token in normalized for token in ("found", "confirmed", "available", "evidence", "clear", "ready")):
            return f"{label} on candidate {ordinal}"
    soft_concern_labels = (
        (("operating cost", "monthly cost", "total cost", "betriebskosten", "price"), "Cost detail needs verification"),
        (("heating", "heizung"), "Heating detail needs verification"),
        (("energy", "energy certificate", "energieausweis"), "Energy detail needs verification"),
        (("internet", "broadband", "fiber", "fibre", "high-speed"), "Internet detail needs verification"),
        (("noise", "traffic noise", "nuisance"), "Noise detail needs verification"),
        (("flood", "water", "groundwater"), "Water risk needs a closer look"),
        (("air quality", "pollution", "emissions"), "Air quality needs verification"),
        (("crime", "safety"), "Safety detail needs verification"),
        (("parking", "garage"), "Parking detail needs verification"),
        (("winter", "driving"), "Winter access needs verification"),
        (("septic", "senkgrube"), "Wastewater detail needs verification"),
        (("sunlight", "orientation", "light"), "Light and orientation detail needs verification"),
    )
    for tokens, label in soft_concern_labels:
        if any(token in normalized for token in tokens) and any(
            token in normalized for token in ("missing", "unknown", "unclear", "risk", "burden", "verify", "verification")
        ):
            return f"{label} for candidate {ordinal}"
    soft_concern_tokens = (
        ("operating cost", "monthly cost", "total cost", "betriebskosten", "price"),
        ("heating", "heizung"),
        ("energy", "energy certificate", "energieausweis"),
        ("internet", "broadband", "fiber", "fibre", "high-speed"),
        ("noise", "traffic noise", "nuisance"),
        ("flood", "water", "groundwater"),
        ("air quality", "pollution", "emissions"),
        ("crime", "safety"),
        ("parking", "garage"),
        ("winter", "driving"),
        ("septic", "senkgrube"),
        ("sunlight", "orientation", "light"),
    )
    for tokens in soft_concern_tokens:
        if any(token in normalized for token in tokens) and any(
            token in normalized for token in ("missing", "unknown", "unclear", "risk", "burden", "verify", "verification")
        ):
            return ""
    if "district" in normalized or "postal" in normalized or "postcode" in normalized:
        if any(token in normalized for token in ("conflict", "mismatch", "outside", "wrong")):
            return f"Candidate {ordinal} is outside the selected area"
    if (
        ("school" in normalized or "kindergarten" in normalized)
        and any(token in normalized for token in ("safe", "safer", "good", "calm", "low traffic", "low-traffic"))
        and any(token in normalized for token in ("route", "way", "walk"))
    ):
        route_label = "Way to kindergarten" if "kindergarten" in normalized else "Way to school"
        return f"{route_label} looks calm for candidate {ordinal}"
    if (
        ("school" in normalized or "kindergarten" in normalized)
        and any(token in normalized for token in ("danger", "dangerous", "unsafe", "risk", "risky", "traffic"))
        and any(token in normalized for token in ("route", "way", "walk"))
    ):
        route_label = "Way to kindergarten" if "kindergarten" in normalized else "Way to school"
        return f"{route_label} needs a closer look for candidate {ordinal}"
    discovery_match = re.search(r"despite\s+a\s+(.+?)\s+miss", text, flags=re.IGNORECASE)
    if discovery_match:
        label = str(discovery_match.group(1) or "").strip()
        label = label[:1].upper() + label[1:] if label else "Preference"
        return f"{label} is missing on candidate {ordinal}"
    if "duplicate" in normalized or "already seen" in normalized or "same listing" in normalized:
        return f"Candidate {ordinal} matched existing property memory"
    if any(token in normalized for token in ("stale", "removed", "expired", "no longer available")):
        return f"Listing freshness changed for candidate {ordinal}; checking again"
    if any(token in normalized for token in ("repair", "extractor", "fetch failed", "provider patch")):
        return f"Checking candidate {ordinal} again"
    if any(token in normalized for token in ("price per sqm", "price per square", "€/m2", "eur/m2", "eur per m2")):
        if any(token in normalized for token in ("below", "under", "cheaper", "discount", "stronger than benchmark")):
            return f"Price per m2 looks good for candidate {ordinal}"
        if any(token in normalized for token in ("above", "over", "expensive", "premium", "higher than benchmark")):
            return f"Price per m2 looks high for candidate {ordinal}"
        return f"Price per m2 checked for candidate {ordinal}"
    if any(token in normalized for token in ("total monthly", "all-in cost", "warm rent", "monthly total")):
        if any(token in normalized for token in ("within", "fits", "fit", "under budget", "inside budget")):
            return f"Total monthly cost fits for candidate {ordinal}"
        if any(token in normalized for token in ("above", "over", "exceeds", "outside budget")):
            return f"Total monthly cost is above budget for candidate {ordinal}"
    if any(token in normalized for token in ("rooms", "room count", "layout shape", "floor plan shape", "floorplan shape")):
        if any(token in normalized for token in ("fits", "fit", "matches", "matched", "usable")):
            return f"Room layout works for candidate {ordinal}"
        if any(token in normalized for token in ("awkward", "unclear", "inefficient", "needs verification")):
            return f"Room layout needs a closer check for candidate {ordinal}"
    if any(token in normalized for token in ("bike route", "cycling", "bicycle")):
        if any(token in normalized for token in ("safe", "protected", "direct", "calm")):
            return f"Bike route looks practical for candidate {ordinal}"
        if any(token in normalized for token in ("unsafe", "traffic", "risky", "indirect")):
            return f"Bike route looks weak for candidate {ordinal}"
    if any(token in normalized for token in ("noise", "quiet", "street exposure")):
        if any(token in normalized for token in ("low", "quiet", "calm", "shielded")):
            return f"Noise looks low for candidate {ordinal}"
        if any(token in normalized for token in ("high", "loud", "exposed", "risk")):
            return f"Noise looks high for candidate {ordinal}"
    if any(token in normalized for token in ("flood", "water", "groundwater")):
        if any(token in normalized for token in ("clear", "low", "outside", "not in")):
            return f"Water risk looked clear for candidate {ordinal}"
        if any(token in normalized for token in ("risk", "burden", "inside", "unclear")):
            return f"Water risk still needs a look for candidate {ordinal}"
    if any(token in normalized for token in ("document", "energy certificate", "operating-cost statement", "betriebskosten statement")):
        if any(token in normalized for token in ("found", "available", "attached", "confirmed")):
            return f"Documents improved the read for candidate {ordinal}"
        if any(token in normalized for token in ("missing", "not attached", "unavailable")):
            return f"Documents are still missing for candidate {ordinal}"
    if ("school" in normalized or "kindergarten" in normalized) and any(
        token in normalized for token in ("close enough", "within", "near", "nearby", "fit", "matches", "matched")
    ):
        fit_label = "Kindergarten distance" if "kindergarten" in normalized else "School distance"
        return f"{fit_label} fits for candidate {ordinal}"
    if ("school" in normalized or "kindergarten" in normalized) and any(
        token in normalized for token in ("too far", "farther", "further", "beyond", "outside")
    ):
        if not measured_distance_match:
            return ""
        observed_match = re.search(r"\d+(?:[.,]\d+)?", measured_distance_match.group(0), flags=re.IGNORECASE)
        fit_label = "Kindergarten" if "kindergarten" in normalized else "School"
        return _property_run_distance_phase_label(
            filter_label=fit_label,
            observed_distance_m=str(observed_match.group(0) if observed_match else "").replace(",", "."),
        )
    if "commute" in normalized and any(token in normalized for token in ("within", "fast", "short", "fits", "fit", "matched")):
        return f"Commute fits for candidate {ordinal}"
    if "commute" in normalized and any(token in normalized for token in ("long", "slow", "longer", "beyond", "outside")):
        return f"Commute is longer than preferred for candidate {ordinal}"
    if any(token in normalized for token in ("supermarket", "pharmacy", "bakery", "market", "errand")) and any(
        token in normalized for token in ("far", "farther", "beyond", "outside")
    ):
        if not measured_distance_match:
            return f"Daily errands are farther than preferred for candidate {ordinal}"
    if "below" in normalized and ("/m2" in normalized or "area" in normalized):
        return f"Area was below the minimum for candidate {ordinal}"
    if "outside the move-in horizon" in normalized:
        return f"Move-in was outside the horizon for candidate {ordinal}"
    if "outside the selected target area" in normalized:
        return f"Location is outside the selected area for candidate {ordinal}"
    if "without enough barrier-free evidence" in normalized:
        return f"Barrier-free detail is missing for candidate {ordinal}"
    if "non-residential" in normalized:
        return f"Property type did not match for candidate {ordinal}"
    if "non-listing candidate" in normalized:
        return f"Candidate {ordinal} was a generic listing page"
    if "layout verification" in normalized or "floorplan" in normalized:
        if any(token in normalized for token in ("missing", "not attached", "unavailable", "without")):
            return f"Floor plan is missing for candidate {ordinal}"
        return ""
    return ""


def _latest_property_run_candidate_reason_label(run_payload: dict[str, object]) -> str:
    events = list(run_payload.get("events") or [])
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        label = _property_run_candidate_reason_label(event.get("message"))
        if label:
            return label
    return _property_run_candidate_reason_label(run_payload.get("message"))


def _compact_property_provider_label(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return "Provider"
    segments = [part.strip() for part in text.split("|") if str(part).strip()]
    if len(segments) > 1 and str(segments[0]).strip().lower() in _GENERIC_SOURCE_FAMILIES:
        text = segments[-1]
    else:
        for marker in ("|", "·", " — ", " – ", ":", "("):
            if marker in text:
                text = text.split(marker)[0].strip()
    words = [item for item in text.split(" ") if item]
    if len(words) > 3:
        text = " ".join(words[:3]).strip()
    if len(text) > 20 and len(words) >= 2:
        text = " ".join(words[:2]).strip()
    if len(text) > 20:
        text = f"{text[:17].rstrip()}..."
    return text or "Provider"


def _source_provider_group(source: dict[str, object]) -> str:
    for raw_key in (
        "provider_source_key",
        "source_provider_key",
        "provider_key",
        "platform",
        "provider_group",
        "provider_channel",
        "provider_family",
    ):
        value = str(source.get(raw_key) or "").strip().lower()
        if value:
            return value
    label = str(source.get("source_label") or source.get("label") or "").strip()
    return (label.split("|")[0] or label).strip().lower() or "provider"


def _run_snapshot_provider_identity_key(source: dict[str, object]) -> str:
    provider_source_key = str(source.get("provider_source_key") or source.get("source_provider_key") or "").strip()
    if provider_source_key:
        candidate = provider_source_key.split(":", 1)[0].strip().casefold()
        if candidate:
            return candidate
    for raw_key in ("provider_key", "platform", "provider_family", "label", "source_label"):
        value = str(source.get(raw_key) or "").strip()
        if raw_key in {"label", "source_label"} and "|" in value:
            value = value.split("|", 1)[0].strip()
        key = value.casefold()
        if key:
            return key
    return ""


def _run_snapshot_provider_total(source_rows: list[dict[str, object]]) -> int:
    provider_keys: set[str] = set()
    for source in source_rows:
        key = _run_snapshot_provider_identity_key(source)
        if key:
            provider_keys.add(key)
    return len(provider_keys)


def _property_run_provider_display_total(payload: dict[str, object], summary: dict[str, object]) -> int:
    explicit_display_total = _positive_int(payload.get("provider_display_total"))
    if explicit_display_total > 0:
        return explicit_display_total
    explicit_provider_total = _positive_int(summary.get("provider_total"))
    source_variant_total = _positive_int(summary.get("source_variant_total") or summary.get("sources_total"))
    selected_platforms = [
        str(value or "").strip().lower()
        for value in list(payload.get("selected_platforms") or dict(payload.get("brief") or {}).get("providers") or ())
        if str(value or "").strip()
    ]
    selected_provider_total = len(dict.fromkeys(selected_platforms))
    source_rows = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    inferred_provider_total = max(selected_provider_total, _run_snapshot_provider_total(source_rows))
    active_source_statuses = {"running", "processing", "in_progress", "working", "starting", "warming", "repairing"}
    has_active_source_rows = any(
        str(row.get("status") or row.get("state") or "").strip().lower() in active_source_statuses
        for row in source_rows
    )
    if inferred_provider_total > 0 and explicit_provider_total > inferred_provider_total:
        if has_active_source_rows:
            return explicit_provider_total
        if not source_variant_total or explicit_provider_total >= source_variant_total or explicit_provider_total > selected_provider_total * 3:
            return inferred_provider_total
    return max(explicit_provider_total, inferred_provider_total)


def _property_run_summary_message(payload: dict[str, object], summary: dict[str, object]) -> str:
    status = str(payload.get("status") or summary.get("status") or "").strip().lower()
    repair_status = str(summary.get("repair_status") or summary.get("repair_status_label") or "").strip().lower()
    replacement_run_id = str(summary.get("repair_replacement_run_id") or "").strip()
    raw_customer_status = str(summary.get("customer_status_message") or "").strip()
    repair_flags = _repair_summary_flags(summary)
    repair_reason = _resolved_customer_repair_reason(summary, message_value=payload.get("message") or "")
    calm_repair_copy = _calm_customer_repair_copy(
        repair_reason,
        replacement_run_id=replacement_run_id,
        repair_active=bool(repair_flags.get("active")),
        repair_failed=bool(repair_flags.get("failed")),
    )
    customer_status = calm_repair_copy if _customer_status_is_internal_failure_copy(raw_customer_status) and calm_repair_copy else raw_customer_status
    effective_repair_reason = "" if calm_repair_copy else repair_reason
    if replacement_run_id:
        return customer_status or calm_repair_copy or "A fresh search is checking your saved brief."
    if status == "failed" or repair_status in {"repairing", "repair failed", "degraded"}:
        if customer_status and effective_repair_reason and effective_repair_reason.lower() not in customer_status.lower():
            return _join_customer_sentences(customer_status, effective_repair_reason)
        if customer_status or calm_repair_copy or effective_repair_reason:
            return customer_status or calm_repair_copy or effective_repair_reason

    sources_total = _positive_int(summary.get("sources_total"))
    completed_sources = _positive_int(summary.get("sources_completed") or summary.get("completed_sources"))
    provider_display_total = _property_run_provider_display_total(payload, summary)
    source_variant_total = _positive_int(summary.get("source_variant_total"), default=sources_total)
    source_work = _property_run_source_work_counts(summary, status=status)
    listing_work = _property_run_listing_work_counts(summary, status=status)
    found_total = listing_work["found"]
    to_review_total = listing_work["to_review"]
    checked_label = _property_run_listing_queue_label(found_total, to_review_total)
    scan_label = checked_label if found_total > 0 else (
        f"{provider_display_total} sites chosen" if provider_display_total > 0 else checked_label
    )
    if found_total > 0 and source_work["open"] > 0:
        source_label = _property_run_provider_check_label(summary, status=status)
        source_left_label = _property_run_source_work_left_label(summary, status=status)
        scan_label = (
            _property_run_listing_queue_label(found_total, to_review_total)
            if to_review_total > 0
            else f"{found_total} homes found"
        )
        if source_label:
            scan_label = f"{scan_label} · {source_left_label or source_label}"
    no_floorplans = _positive_int(summary.get("filtered_floorplan_total"))
    current_step = str(payload.get("current_step") or "").strip().lower()
    packet_prepared = _positive_int(summary.get("review_created_total")) + _positive_int(summary.get("review_existing_total"))
    shortlist_ready = _property_summary_ranked_total(summary)
    detail_queue_message = _property_run_detail_queue_message(summary, status=status, message=payload.get("message"))
    if detail_queue_message:
        return detail_queue_message
    if current_step == "source_review_packet" and packet_prepared > 0:
        return f"{scan_label} · {packet_prepared} property pages prepared"
    if current_step == "source_shortlist" and shortlist_ready > 0:
        suffix = "" if shortlist_ready == 1 else "s"
        return f"{scan_label} · {shortlist_ready} matching home{suffix} ready"
    return f"{scan_label}{f' · {no_floorplans} floorplans pending' if no_floorplans > 0 else ''}"


def build_property_run_live_board_snapshot(
    run_payload: dict[str, object],
    *,
    plan_key: str = "free",
) -> dict[str, object]:
    payload = dict(run_payload or {})
    summary = dict(payload.get("summary") or {}) if isinstance(payload.get("summary"), dict) else {}
    status = str(payload.get("status") or summary.get("status") or "queued").strip().lower() or "queued"
    progress = max(0, min(100, _positive_int(payload.get("progress") or summary.get("progress"))))
    source_total = max(0, _positive_int(summary.get("sources_total")))
    source_rows = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    source_total = max(source_total, _positive_int(summary.get("source_variant_total")), len(source_rows))
    provider_display_total = _property_run_provider_display_total(payload, summary)
    if source_rows and provider_display_total > source_total:
        source_total = provider_display_total
    listing_work = _property_run_listing_work_counts(summary, status=status)
    found_total = listing_work["found"]
    to_review_total = listing_work["to_review"]
    waiting_on_floorplans = max(0, _positive_int(summary.get("filtered_floorplan_total")))
    packet_prepared = max(0, _positive_int(summary.get("review_created_total")) + _positive_int(summary.get("review_existing_total")))
    shortlist_ready = _property_summary_ranked_total(summary)

    live_info = _latest_property_run_fraction_info(payload)
    current_info = _parse_property_run_message_info(payload.get("message"))
    review_fraction_is_listing_work = False
    review_fraction = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", str(live_info.get("fraction_label") or ""))
    review_fraction_raw = str(live_info.get("raw") or current_info.get("raw") or "").strip()
    if review_fraction and re.search(
        r"\b(?:reviewing(?: candidate)?|scoring enriched candidate|ranked|scored)\b",
        review_fraction_raw,
        flags=re.IGNORECASE,
    ):
        reviewed_from_fraction = _positive_int(review_fraction.group(1))
        found_from_fraction = _positive_int(review_fraction.group(2))
        if found_from_fraction > 0 and (found_total <= 0 or found_from_fraction >= found_total):
            found_total = max(found_total, found_from_fraction)
            to_review_total = max(0, found_total - reviewed_from_fraction)
            review_fraction_is_listing_work = True
    active_source_label = _canonical_property_run_source_label(current_info.get("source_label") or live_info.get("source_label") or "")
    message_text = str(payload.get("message") or "").strip().lower()

    live_source_work = _property_run_source_work_counts(summary, status=status)
    live_source_left_label = _property_run_source_work_left_label(summary, status=status)
    if found_total > 0 and live_source_work["open"] > 0 and live_source_left_label and not review_fraction_is_listing_work:
        aggregate_label = (
            f"{_property_run_listing_queue_label(found_total, to_review_total)} · {live_source_left_label}"
            if to_review_total > 0
            else f"{found_total} homes found · {live_source_left_label}"
        )
    else:
        aggregate_label = _property_run_listing_queue_label(found_total, to_review_total)
    if waiting_on_floorplans > 0:
        aggregate_label += f" · {waiting_on_floorplans} floorplans pending"
    phase_label = str(current_info.get("phase_label") or "").strip() or "Looking for the first homes."
    if phase_label in {"Waiting for the first update.", "Waiting for the first site.", "Waiting for the first check."}:
        phase_label = "Looking for the first homes."
    active_source_summary = _property_run_source_summary_for_label(source_rows, active_source_label)
    source_near_miss_label = _property_run_source_near_miss_phase_label(active_source_summary)
    candidate_reason_label = _latest_property_run_candidate_reason_label(payload)
    if current_info.get("fraction_label") and source_near_miss_label:
        phase_label = source_near_miss_label
    elif current_info.get("fraction_label") and candidate_reason_label:
        phase_label = candidate_reason_label
    current_step = str(payload.get("current_step") or "").strip().lower()
    if phase_label in {"Waiting for the first update.", "Waiting for the first list.", "Waiting for the first check.", "Waiting for the first site.", "Looking for the first homes."} and packet_prepared > 0 and current_step == "source_review_packet":
        phase_label = f"{packet_prepared} property pages prepared"
    elif (
        current_step == "source_shortlist"
        and shortlist_ready > 0
        and (
            phase_label in {"Waiting for the first update.", "Waiting for the first list.", "Waiting for the first check.", "Waiting for the first site.", "Looking for the first homes.", "Shortlist ready"}
            or phase_label.lower().startswith("shortlist ready")
        )
    ):
        phase_label = f"{shortlist_ready} matching home{'s' if shortlist_ready != 1 else ''} ready"
    if (
        found_total > 0
        and to_review_total <= 0
        and live_source_left_label
        and (
            "0 to review" in phase_label.lower()
            or phase_label == aggregate_label
            or "details caught up" in phase_label.lower()
            or "all found homes are checked" in phase_label.lower()
        )
    ):
        phase_label = "Checking remaining checks"

    normalized_rows: list[dict[str, object]] = []
    for source in source_rows:
        row = dict(source)
        raw_status = str(row.get("status") or row.get("state") or "").strip().lower()
        row_label = _canonical_property_run_source_label(row.get("source_label") or row.get("label") or "")
        if not raw_status:
            row["status"] = "failed" if row.get("error") else "completed"
        if active_source_label and row_label == active_source_label and not message_text.startswith("completed scanning "):
            row["status"] = "failed" if row.get("error") else "running"
        normalized_rows.append(row)
    synthetic_active = []
    if active_source_label and not any(_canonical_property_run_source_label(row.get("source_label") or row.get("label") or "") == active_source_label for row in normalized_rows) and not message_text.startswith("completed scanning "):
        synthetic_active.append({"source_label": active_source_label, "label": active_source_label, "status": "running"})
    lane_rows = [*synthetic_active, *normalized_rows]
    running_rows = [
        row for row in lane_rows
        if str(row.get("status") or row.get("state") or "").strip().lower() in {"running", "processing", "in_progress", "working", "warming"}
    ]
    queued_rows = [
        row for row in lane_rows
        if str(row.get("status") or row.get("state") or "").strip().lower() in {"queued", "pending", "starting"}
    ]
    failed_rows = [
        row for row in lane_rows
        if str(row.get("status") or row.get("state") or "").strip().lower() in {"failed", "error", "skipped"} or row.get("error")
    ]
    completed_rows = [
        row for row in lane_rows
        if str(row.get("status") or row.get("state") or "").strip().lower() in {"completed", "processed", "done", "success", "repaired"}
    ]
    raw_worker_queue = [*running_rows, *queued_rows, *failed_rows, *completed_rows]
    seen_groups: set[str] = set()
    worker_queue: list[dict[str, object]] = []
    for row in raw_worker_queue:
        key = _source_provider_group(row)
        if key in seen_groups:
            continue
        seen_groups.add(key)
        worker_queue.append(row)
    normalized_plan_key = normalize_property_plan_key(plan_key)
    plan_cap = 4 if normalized_plan_key == "agent" else (2 if normalized_plan_key == "plus" else 1)
    provider_workers = dict(summary.get("provider_workers") or {}) if isinstance(summary.get("provider_workers"), dict) else {}
    configured_workers = _positive_int(provider_workers.get("worker_concurrency"))
    run_active = progress > 0 or status in {"queued", "in_progress", "running", "processing", "starting"}
    effective_worker_cap = max(1, min(4, max(configured_workers or plan_cap, len(worker_queue) if run_active else 0)))
    actual_worker_count = min(effective_worker_cap, len(worker_queue))
    worker_count = actual_worker_count if actual_worker_count > 0 else (1 if run_active else 0)
    worker_lanes: list[dict[str, object]] = []
    for index in range(worker_count):
        source = worker_queue[index] if index < len(worker_queue) else None
        raw_status = str((source or {}).get("status") or (source or {}).get("state") or "").strip().lower()
        progress_pct = _positive_int((source or {}).get("progress"))
        if progress_pct <= 0:
            if raw_status in {"completed", "processed", "done", "success", "failed", "error", "skipped"}:
                progress_pct = 100
            elif raw_status == "starting":
                progress_pct = 26
            elif raw_status == "warming":
                progress_pct = 42
            elif raw_status in {"running", "processing", "in_progress", "working"}:
                progress_pct = 58
            elif raw_status in {"queued", "pending"}:
                progress_pct = 18
            else:
                progress_pct = 0
        if source is None:
            status_label = "Starting" if run_active else "Idle"
        elif raw_status in {"completed", "processed", "done", "success", "repaired"}:
            status_label = "Done"
        elif raw_status in {"failed", "error"} or source.get("error"):
            status_label = "Fetch failed"
        elif raw_status == "starting":
            status_label = "Starting"
        elif raw_status == "warming":
            status_label = "Preparing"
        elif raw_status in {"running", "processing", "in_progress", "working"}:
            status_label = "Running"
        else:
            status_label = "Up next"
        if source is None:
            tone = "active" if status_label == "Starting" else "idle"
        elif progress_pct >= 100 and status_label == "Done":
            tone = "done"
        elif status_label in {"Running", "Starting", "Preparing"}:
            tone = "active"
        elif status_label == "Fetch failed":
            tone = "warn"
        else:
            tone = "queued"
        provider = str((source or {}).get("source_label") or (source or {}).get("label") or ("Preparing provider" if status_label == "Starting" else ("Waiting for a provider" if source_rows else "Ready when you start")))
        group_key = _source_provider_group(source or {})
        shard_count = max(0, len([row for row in raw_worker_queue if _source_provider_group(row) == group_key]) - 1) if source else 0
        worker_lanes.append(
            {
                "label": _compact_property_provider_label(provider) if source else ("Preparing" if status_label == "Starting" else ("Waiting" if source_rows else "Ready")),
                "provider": provider,
                "shard_count": shard_count,
                "status_label": status_label,
                "progress_pct": progress_pct if source else (max(4, min(progress or 4, 12)) if status_label == "Starting" else progress_pct),
                "tone": tone,
            }
        )
    source_chips = [
        {
            "label": str(source.get("source_label") or source.get("label") or "Source"),
            "tone": "warn" if source.get("error") else "good",
        }
        for source in source_rows[:8]
    ]
    provider_full_label = str(live_info.get("source_label") or (worker_queue[0].get("source_label") if worker_queue else "") or "").strip()
    provider_total = max(_positive_int(summary.get("provider_total")), provider_display_total)
    source_variant_total = _positive_int(summary.get("source_variant_total"), default=source_total)
    active_source_statuses = {"running", "processing", "in_progress", "working", "starting", "warming", "repairing"}
    has_active_source_rows = any(
        str(row.get("status") or row.get("state") or "").strip().lower() in active_source_statuses
        for row in source_rows
    )
    if provider_total > 0 and source_rows and not has_active_source_rows:
        inferred_provider_total = _run_snapshot_provider_total(source_rows)
        source_count_hint = max(source_total, source_variant_total, len(source_rows))
        if inferred_provider_total and (
            source_count_hint and source_total > 0
            and (
                provider_total > source_count_hint
                or (provider_total == source_count_hint and inferred_provider_total < provider_total)
            )
        ):
            provider_total = inferred_provider_total
    scan_total_label = (
        f"{provider_total} providers"
        if provider_total
        else (f"{source_total} checks" if source_total else "")
    )
    provider_label = _compact_property_provider_label(provider_full_label or scan_total_label)
    if not source_rows and source_total and provider_total and source_total > provider_total and source_total > 3:
        if source_total >= provider_total * 3:
            source_total = provider_total
        elif provider_total and source_total > provider_total + 2:
            source_total = provider_total
    fraction_label = str(live_info.get("fraction_label") or "").strip()
    if phase_label == "Site batch finished":
        completed_fraction = re.fullmatch(r"(\d+)\s*/\s*(\d+)", fraction_label)
        if completed_fraction:
            checked_total = completed_fraction.group(2)
            checked_unit = "home" if checked_total == "1" else "homes"
            fraction_label = f"{completed_fraction.group(1)} of {checked_total} {checked_unit} checked"
    if source_rows:
        unit = _property_run_source_unit_label(summary, total=source_total)
        source_count_label = fraction_label or f"{len(source_rows)}/{source_total} {unit}"
    else:
        unit = _property_run_source_unit_label(summary, total=source_total)
        source_count_label = fraction_label or ("waiting for providers" if source_total == 0 else f"0/{source_total} {unit}")
    summary_label = (
        f"{aggregate_label} · {provider_label} · {fraction_label}"
        if aggregate_label != "checking" and provider_full_label and fraction_label
        else (aggregate_label if aggregate_label != "checking" else (scan_total_label or aggregate_label))
    )
    return PropertyRunLiveBoardSnapshot(
        provider_label=provider_label,
        provider_full_label=provider_full_label,
        fraction_label=fraction_label,
        phase_label=phase_label,
        aggregate_label=aggregate_label,
        summary_label=summary_label,
        source_count_label=source_count_label,
        source_chips=source_chips,
        worker_lanes=worker_lanes,
    ).to_dict()


def build_property_billing_truth_snapshot(
    *,
    commercial: dict[str, object],
    default_billing_plan: str,
    billing_enabled_plans: list[str],
    billing_order_endpoints_by_plan: dict[str, str],
    fleet_digest: dict[str, object] | None = None,
) -> dict[str, object]:
    return PropertyBillingTruthSnapshot(
        current_plan_label=str(commercial.get("current_plan_label") or "Free").strip() or "Free",
        current_plan_key=str(commercial.get("current_plan_key") or "free").strip().lower() or "free",
        research_depth=str(commercial.get("research_depth") or "deep").strip() or "deep",
        max_platforms=int(commercial.get("max_platforms") or 0),
        max_results_per_source=int(commercial.get("max_results_per_source") or 0),
        checkout_enabled=bool(billing_enabled_plans),
        checkout_enabled_plans=tuple(billing_enabled_plans),
        order_endpoint=str(billing_order_endpoints_by_plan.get(default_billing_plan) or ""),
        order_endpoints_by_plan=dict(billing_order_endpoints_by_plan),
        fleet_digest=dict(fleet_digest or {}),
    ).to_dict()


def build_property_preference_manager_snapshot(
    *,
    person_id: str,
    raw_preference_nodes: list[dict[str, object]],
    include_full_manager: bool,
    schema: dict[str, object] | None = None,
) -> dict[str, object]:
    def _preference_value_label(value: object) -> str:
        if isinstance(value, list):
            return ", ".join(str(item).strip() for item in value if str(item).strip()) or "empty list"
        if isinstance(value, dict):
            return ", ".join(f"{key}: {item}" for key, item in value.items() if str(key).strip()) or "empty object"
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value if value is not None else "").strip() or "empty"

    def _preference_key_label(row: dict[str, object]) -> str:
        key = str(row.get("key") or "").strip().replace("_", " ")
        category = str(row.get("category") or "").strip().replace("_", " ")
        return (key or "Preference").title() + (f" ({category.title()})" if category else "")

    active_preference_total = sum(
        1
        for row in raw_preference_nodes
        if str(row.get("node_id") or "").strip()
        and str(row.get("status") or "").strip().lower() in {"", "active"}
    )
    nodes = (
        [
            {
                "node_id": str(row.get("node_id") or "").strip(),
                "domain": str(row.get("domain") or "").strip() or "willhaben",
                "category": str(row.get("category") or "").strip() or "soft_preference",
                "key": str(row.get("key") or "").strip(),
                "label": _preference_key_label(row),
                "value_label": _preference_value_label(row.get("value_json")),
                "value_json": row.get("value_json"),
                "strength": str(row.get("strength") or "medium").strip() or "medium",
                "confidence": row.get("confidence") or 0,
                "source_mode": str(row.get("source_mode") or "").strip(),
                "status": str(row.get("status") or "").strip().lower() or "active",
                "updated_at": str(row.get("updated_at") or "").strip(),
            }
            for row in raw_preference_nodes
            if str(row.get("node_id") or "").strip()
        ]
        if include_full_manager
        else []
    )
    if nodes:
        nodes.sort(key=lambda row: (str(row.get("status") or "") != "active", str(row.get("label") or "").lower()))
    active_nodes = (
        [row for row in nodes if str(row.get("status") or "") == "active"]
        if include_full_manager
        else [{"status": "active"} for _ in range(active_preference_total)]
    )
    return PropertyPreferenceManagerSnapshot(
        person_id=str(person_id or "self").strip() or "self",
        nodes=nodes,
        active_nodes=active_nodes,
        schema=dict(schema or {}) if include_full_manager else {},
        bundle_endpoint=f"/app/api/people/{urllib.parse.quote(str(person_id or 'self').strip() or 'self', safe='')}/preference-profile",
        node_endpoint=f"/app/api/people/{urllib.parse.quote(str(person_id or 'self').strip() or 'self', safe='')}/preference-profile/nodes",
        archive_endpoint_template=f"/app/api/people/{urllib.parse.quote(str(person_id or 'self').strip() or 'self', safe='')}/preference-profile/nodes/__NODE_ID__/archive",
    ).to_dict()


def build_property_search_form_state_snapshot(
    property_preferences: dict[str, object] | None,
    *,
    selected_listing_mode: str,
) -> dict[str, object]:
    preferences = dict(property_preferences or {})
    selected_country_code = str(preferences.get("country_code") or "AT").strip().upper() or "AT"
    selected_search_goal = normalized_property_search_goal(preferences.get("search_goal"))
    selected_investment_strategy = str(preferences.get("investment_strategy") or "best_overall").strip().lower() or "best_overall"
    if selected_investment_strategy not in {"best_overall", "cash_flow", "appreciation", "undervalued", "low_risk"}:
        selected_investment_strategy = "best_overall"
    selected_investment_research_mode = str(preferences.get("investment_research_mode") or "off").strip().lower() or "off"
    if selected_investment_research_mode not in {"off", "auto"}:
        selected_investment_research_mode = "off"
    property_is_investment_search = selected_search_goal == "investment"
    selected_school_stage_preferences = [
        str(item or "").strip()
        for item in list(preferences.get("school_stage_preferences") or [])
        if str(item or "").strip()
    ]
    school_evidence_controls_enabled = (
        not property_is_investment_search
        and (
            bool(selected_school_stage_preferences)
            or bool(preferences.get("require_school_evidence"))
        )
    )
    effective_listing_mode_value = effective_property_listing_mode(
        {
            **preferences,
            "search_goal": selected_search_goal,
            "listing_mode": selected_listing_mode,
        },
        fallback=selected_listing_mode,
    )
    show_investment_underwriting_controls = property_is_investment_search and selected_investment_research_mode != "off"
    show_lifestyle_research_controls = not property_is_investment_search and bool(preferences.get("enable_lifestyle_research"))
    show_community_validation_controls = bool(preferences.get("include_community_signals"))
    show_developer_project_stage_controls = bool(preferences.get("include_developer_project_signals"))
    show_public_housing_policy_controls = effective_listing_mode_value == "rent" and bool(preferences.get("include_public_housing_signals"))
    show_distressed_review_controls = effective_listing_mode_value == "buy" and bool(preferences.get("include_distressed_sale_signals"))
    show_search_agent_detail_controls = bool(preferences.get("search_agent_enabled"))
    show_preference_profile_controls = bool(preferences.get("use_stored_feedback_preferences", True))
    show_playground_importance_controls = not property_is_investment_search and bool(preferences.get("max_distance_to_playground_m"))
    show_library_importance_controls = not property_is_investment_search and bool(preferences.get("max_distance_to_library_m"))
    show_supermarket_importance_controls = bool(preferences.get("max_distance_to_supermarket_m"))

    def _bounded_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(float(str(preferences.get(name) or "").strip()))))
        except Exception:
            return default

    def _bounded_float(name: str, *, default: float, minimum: float, maximum: float) -> float:
        try:
            return max(minimum, min(maximum, float(str(preferences.get(name) or "").strip())))
        except Exception:
            return default

    return PropertySearchFormStateSnapshot(
        selected_country_code=selected_country_code,
        selected_search_goal=selected_search_goal,
        selected_listing_mode=effective_listing_mode_value,
        selected_investment_strategy=selected_investment_strategy,
        selected_investment_research_mode=selected_investment_research_mode,
        property_is_investment_search=property_is_investment_search,
        selected_school_stage_preferences=selected_school_stage_preferences,
        school_evidence_controls_enabled=school_evidence_controls_enabled,
        show_investment_underwriting_controls=show_investment_underwriting_controls,
        show_lifestyle_research_controls=show_lifestyle_research_controls,
        show_community_validation_controls=show_community_validation_controls,
        show_developer_project_stage_controls=show_developer_project_stage_controls,
        show_public_housing_policy_controls=show_public_housing_policy_controls,
        show_distressed_review_controls=show_distressed_review_controls,
        show_search_agent_detail_controls=show_search_agent_detail_controls,
        show_preference_profile_controls=show_preference_profile_controls,
        show_school_evidence_priority_controls=school_evidence_controls_enabled,
        show_playground_importance_controls=show_playground_importance_controls,
        show_library_importance_controls=show_library_importance_controls,
        show_supermarket_importance_controls=show_supermarket_importance_controls,
        min_gross_yield_pct=_bounded_int("min_gross_yield_pct", default=0, minimum=0, maximum=15),
        equity_available_eur=_bounded_int("equity_available_eur", default=0, minimum=0, maximum=5_000_000),
        loan_term_years=_bounded_int("loan_term_years", default=25, minimum=5, maximum=40),
        max_interest_rate_pct=_bounded_int("max_interest_rate_pct", default=0, minimum=0, maximum=12),
        min_dscr=_bounded_float("min_dscr", default=0.0, minimum=0.0, maximum=3.0),
        vacancy_reserve_pct=_bounded_int("vacancy_reserve_pct", default=4, minimum=0, maximum=25),
        capex_reserve_pct=_bounded_int("capex_reserve_pct", default=6, minimum=0, maximum=25),
    ).to_dict()


def _previous_run_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(float(str(value or "").strip())))
    except Exception:
        return default


def _previous_run_price_text(candidate: dict[str, object]) -> str:
    facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
    for raw_value in (
        candidate.get("price_display"),
        candidate.get("costs_display"),
        candidate.get("rent_display"),
        facts.get("price_display"),
        facts.get("rent_display"),
        facts.get("price"),
        facts.get("rent"),
    ):
        text = str(raw_value or "").strip()
        if text:
            return text
    for raw_value in (
        candidate.get("price_eur"),
        candidate.get("purchase_price_eur"),
        candidate.get("buy_price_eur"),
        facts.get("price_eur"),
        facts.get("purchase_price_eur"),
        facts.get("buy_price_eur"),
    ):
        try:
            amount = float(raw_value)
        except Exception:
            amount = 0.0
        if amount > 0:
            return f"EUR {amount:,.0f}"
    currency_pattern = "|".join(re.escape(code) for code in supported_currency_codes())
    for source_text in (
        candidate.get("summary"),
        candidate.get("title"),
    ):
        text = " ".join(str(source_text or "").split()).strip()
        if not text:
            continue
        for pattern in (
            r"(€\s?[0-9][0-9\.\s]*(?:,\d{1,2})?\s*,-?)",
            rf"((?:{currency_pattern})\s?[0-9][0-9\.,\s]*)",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return " ".join(str(match.group(1) or "").split()).strip(" ,")
    return ""


def build_property_previous_run_summary(
    raw_run: dict[str, object],
    *,
    include_scope_preview: bool,
    scope_preview_builder: Callable[..., dict[str, object]],
    compact_provider_label: Callable[[str], str],
    candidate_maps_url_builder: Callable[[dict[str, object]], str],
) -> dict[str, object]:
    summary = dict(raw_run.get("summary") or {}) if isinstance(raw_run.get("summary"), dict) else {}
    preferences_json = dict(raw_run.get("property_search_preferences") or raw_run.get("preferences") or {}) if isinstance(raw_run.get("property_search_preferences") or raw_run.get("preferences"), dict) else {}
    run_status = str(raw_run.get("status") or summary.get("status") or "queued").strip().lower()
    run_id_value = str(raw_run.get("run_id") or "").strip()
    is_old_snapshot = bool(
        raw_run.get("brief_preferences_stale")
        or raw_run.get("stale_run_snapshot")
        or summary.get("brief_preferences_stale")
        or str(summary.get("brief_snapshot_status") or "").strip().lower() == "old_run"
    )
    country = str(preferences_json.get("country_code") or summary.get("country_code") or "").strip().upper()
    region = str(preferences_json.get("region_code") or summary.get("region_code") or "").strip()
    location = str(preferences_json.get("location_query") or summary.get("location_query") or "").strip()
    mode = property_mode_visibility_label(
        {
            **summary,
            **preferences_json,
        },
        fallback=str(summary.get("listing_mode") or "rent"),
    )
    scope_parts = [part for part in (country, region, location) if part]
    ranked_candidates = [
        dict(row)
        for row in list(summary.get("ranked_candidates") or [])
        if isinstance(row, dict)
    ]
    top_candidates: list[dict[str, object]] = []
    for candidate in ranked_candidates[:3]:
        title = str(candidate.get("title") or "Property").strip() or "Property"
        source_label = str(candidate.get("source_label") or candidate.get("source_platform") or "Source").strip() or "Source"
        source_short_label = compact_provider_label(source_label)
        top_candidates.append(
            {
                "title": title,
                "source_label": source_label,
                "source_short_label": source_short_label,
                "fit_score": _previous_run_int(candidate.get("fit_score")),
                "detail": str(
                    candidate.get("compare_reason")
                    or candidate.get("fit_summary")
                    or (list(candidate.get("match_reasons") or [""])[0] if isinstance(candidate.get("match_reasons"), list) else "")
                    or "Open the finished search to see this home."
                ).strip(),
                "review_url": str(candidate.get("packet_url") or candidate.get("review_url") or "").strip(),
                "map_url": str(candidate.get("map_url") or candidate_maps_url_builder(candidate)).strip(),
                "price_display": _previous_run_price_text(candidate),
            }
        )
    held_back_total = max(
        0,
        _previous_run_int(summary.get("filtered_floorplan_total"))
        + _previous_run_int(summary.get("filtered_area_total"))
        + _previous_run_int(summary.get("filtered_property_type_total"))
        + _previous_run_int(summary.get("filtered_availability_total"))
        + _previous_run_int(summary.get("filtered_generic_page_total"))
        + _previous_run_int(summary.get("filtered_listing_mode_total"))
    )
    status_label, status_note = property_run_status_copy(
        raw_run.get("status") or summary.get("status"),
        property_run_customer_safe_status_detail(
            raw_run.get("status") or summary.get("status"),
            raw_run.get("message") or summary.get("message"),
            summary=summary,
            prefer_repair_step=True,
        ),
    )
    previous_filtered_total = _previous_run_int(
        summary.get("previous_filtered_total")
        or summary.get("filtered_total")
        or summary.get("held_back_total")
        or held_back_total
    )
    previous_ranked_total = _previous_run_int(
        summary.get("previous_ranked_total")
        or summary.get("ranked_total")
        or summary.get("ranked_candidate_total")
        or len(ranked_candidates)
    )
    if is_old_snapshot:
        status_label = "Older search"
        status_note = str(summary.get("brief_stale_message") or "").strip() or (
            "This search used an earlier brief. Start an updated search to refresh counts with the current saved brief."
        )
    scope_preview = (
        _build_scope_preview(
            scope_preview_builder,
            country,
            region,
            location,
            adjacent_area_radius_m=preferences_json.get("adjacent_area_radius_m") or summary.get("adjacent_area_radius_m"),
        )
        if include_scope_preview
        else {}
    )
    quoted_run_id = urllib.parse.quote(run_id_value, safe="") if run_id_value else ""
    href = f"/app/shortlist/run/{quoted_run_id}" if quoted_run_id else "/app/shortlist"
    return {
        "run_id": run_id_value,
        "agent_id": str(raw_run.get("active_search_agent_id") or preferences_json.get("active_search_agent_id") or "").strip(),
        "status": run_status,
        "status_label": status_label,
        "status_note": status_note,
        "title": location or region or country or "Saved search",
        "scope_label": " · ".join(scope_parts) or "No scope saved",
        "scope_preview": scope_preview,
        "scope_summary": str(scope_preview.get("summary") or location or region or country or "Search area").strip(),
        "mode_label": mode or "Search",
        "href": href,
        "full_href": href,
        "updated_at": str(raw_run.get("updated_at") or raw_run.get("generated_at") or "").strip(),
        "source_total": _previous_run_int(summary.get("sources_total")),
        "listing_total": _previous_run_int(summary.get("listing_total") or summary.get("raw_listing_total")),
        "ranked_total": len(ranked_candidates),
        "sent_total": _previous_run_int(summary.get("notified_total") or summary.get("watch_notified_total")),
        "held_back_total": 0 if is_old_snapshot else held_back_total,
        "previous_filtered_total": previous_filtered_total if is_old_snapshot else 0,
        "previous_ranked_total": previous_ranked_total if is_old_snapshot else 0,
        "is_old_snapshot": is_old_snapshot,
        "brief_snapshot_status": "old_run" if is_old_snapshot else "",
        "top_fit_score": _previous_run_int(summary.get("top_fit_score") or (top_candidates[0].get("fit_score") if top_candidates else 0)),
        "top_price_display": str((top_candidates[0].get("price_display") if top_candidates else "") or "").strip(),
        "top_candidates": top_candidates,
        "is_finished": run_status in {"processed", "completed", "failed", "noop", "cancelled"},
    }


def build_property_empty_outcome_summary(
    *,
    run_summary: dict[str, object],
    run_sources: list[dict[str, object]],
    run_status_value: str,
    run_message: str,
    counterfactual_rows: list[dict[str, object]],
    suppression_rows: list[dict[str, object]],
) -> dict[str, str]:
    is_old_snapshot = bool(
        run_summary.get("brief_preferences_stale")
        or str(run_summary.get("brief_snapshot_status") or "").strip().lower() == "old_run"
    )
    if is_old_snapshot:
        def _safe_int(*values: object) -> int:
            for value in values:
                try:
                    return max(0, int(float(str(value or "").strip())))
                except Exception:
                    continue
            return 0

        previous_filtered_total = _safe_int(
            run_summary.get("previous_filtered_total"),
            run_summary.get("filtered_total"),
            run_summary.get("held_back_total"),
        )
        previous_ranked_total = _safe_int(
            run_summary.get("previous_ranked_total"),
            run_summary.get("ranked_total"),
            run_summary.get("ranked_candidate_total"),
        )
        history_parts: list[str] = []
        if previous_ranked_total:
            history_parts.append(f"{previous_ranked_total} matches")
        if previous_filtered_total:
            history_parts.append(f"{previous_filtered_total} outside search")
        historical_detail = " · ".join(history_parts) if history_parts else "historical counts are kept with the older search"
        return {
            "happened": "This search used older search settings.",
            "still_worked": f"Older snapshot: {historical_detail}.",
            "next_move": "Start an updated search so the counts use your current search settings.",
            "active_rule": "Older search",
            "eta_feedback": str(run_summary.get("brief_stale_message") or "").strip() or "The current search settings have changed since this search finished.",
        }
    filtered_total = int(run_summary.get("filtered_total") or run_summary.get("held_back_total") or 0)
    score_demoted_total = int(
        run_summary.get("score_demoted_total")
        or run_summary.get("filtered_low_fit_total")
        or 0
    )
    legacy_ranking_gate_empty = score_demoted_total > 0 and filtered_total <= 0
    source_total = int(run_summary.get("sources_total") or len(run_sources) or 0)
    source_completed = int(
        run_summary.get("sources_completed")
        or len(
            [
                row
                for row in (run_sources or [])
                if str(row.get("status") or "").strip().lower() in {"completed", "processed", "ok"}
            ]
        )
        or 0
    )
    listing_total = int(run_summary.get("listing_total") or 0)
    raw_listing_total = int(run_summary.get("raw_listing_total") or run_summary.get("reviewed_listing_total") or 0)
    area_filtered_total = int(run_summary.get("filtered_area_total") or 0)
    location_mismatch_total = 0
    for source in run_sources or []:
        if not isinstance(source, dict):
            continue
        try:
            location_mismatch_total += max(0, int(float(source.get("location_mismatch_candidate_total") or 0)))
        except Exception:
            continue
    status_value = str(run_status_value or "").strip().lower()
    eta_label = str(run_summary.get("eta_label") or "").strip()
    repair_step_label = str(run_summary.get("repair_step_label") or "").strip()
    repair_status_label = str(run_summary.get("repair_status_label") or run_summary.get("repair_status") or "").strip()
    repair_flags = _repair_summary_flags(run_summary)
    replacement_run_id = str(repair_flags.get("replacement_run_id") or "").strip()
    repair_task_open = bool(repair_flags.get("active"))
    repair_failed = bool(repair_flags.get("failed"))
    latest_repair_reason = _resolved_customer_repair_reason(run_summary, message_value=run_message)
    calm_repair_copy = _calm_customer_repair_copy(
        latest_repair_reason,
        replacement_run_id=replacement_run_id,
        repair_active=repair_task_open,
        repair_failed=repair_failed,
    )
    latest_repair_update = _format_property_repair_timestamp(_latest_repair_timestamp(run_summary))
    safe_failed_message = property_run_customer_safe_status_detail(
        status_value,
        run_message,
        summary=run_summary,
    )
    database_pressure = any(token in str(run_message or "").lower() for token in _DATABASE_PRESSURE_TOKENS)
    if safe_failed_message.lower().startswith("search interrupted") or safe_failed_message.lower().startswith("the search stopped"):
        safe_failed_message = "The search stopped before a ready shortlist was available."
    strongest_relax = next((row for row in (counterfactual_rows or []) if row.get("adjustments")), {})
    active_rule = ""
    if strongest_relax:
        active_rule = str(strongest_relax.get("title") or strongest_relax.get("rule_label") or "").strip()
    elif suppression_rows:
        active_rule = str((suppression_rows[0] or {}).get("title") or "").strip()
    if status_value == "failed" and replacement_run_id:
        happened = calm_repair_copy or "A fresh search is checking your saved brief."
        if legacy_ranking_gate_empty:
            stopped_context = (
                f"{score_demoted_total} home{'s' if score_demoted_total != 1 else ''} still matched "
                "before the interruption. This page will move to the fresh search when it has a useful update."
            )
        else:
            stopped_context = "This page will move to the fresh search when it has a useful update."
    elif status_value == "failed":
        if repair_failed:
            happened = safe_failed_message or calm_repair_copy or "The search stopped before a ready shortlist was available."
            if legacy_ranking_gate_empty:
                stopped_context = (
                    f"{score_demoted_total} home{'s' if score_demoted_total != 1 else ''} still matched "
                    "the saved brief before the retry stopped."
                )
            elif source_total or listing_total:
                listing_label = f"{listing_total} listing{'s' if listing_total != 1 else ''}"
                stopped_context = (
                    f"Selected sites searched {listing_label}."
                    if listing_total > 0
                    else "The brief and chosen sites were saved, but none returned a match for the brief."
                )
            else:
                stopped_context = "The brief and chosen sites were saved, but none returned a match for the brief."
        elif source_total or listing_total:
            listing_label = f"{listing_total} listing{'s' if listing_total != 1 else ''}"
            if database_pressure:
                happened = safe_failed_message or "The search paused because the database was busy. PropertyQuarry is retrying it."
            elif repair_task_open or repair_step_label or repair_status_label:
                happened = safe_failed_message or calm_repair_copy or "PropertyQuarry is checking the saved search again."
            else:
                happened = "The search stopped before the shortlist settled."
            if source_completed <= 0 and listing_total <= 0:
                stopped_context = "PropertyQuarry started a fresh check before any listing inspection completed."
            elif legacy_ranking_gate_empty:
                stopped_context = (
                    f"{score_demoted_total} home{'s' if score_demoted_total != 1 else ''} still matched "
                    "the saved brief before retry took over."
                )
            else:
                stopped_context = f"Selected sites checked {listing_label}."
        else:
            if repair_task_open or repair_step_label or repair_status_label:
                happened = safe_failed_message or calm_repair_copy or "PropertyQuarry is checking the saved search again."
                stopped_context = "PropertyQuarry started a fresh check before any listing inspection completed."
            else:
                happened = safe_failed_message or "The search stopped before the shortlist settled."
                stopped_context = ""
    elif filtered_total > 0 and listing_total == 0 and (location_mismatch_total > 0 or area_filtered_total >= max(1, filtered_total // 2)):
        happened = "Nothing landed in the selected area yet."
        stopped_context = (
            f"{filtered_total} home{'s' if filtered_total != 1 else ''} stayed outside the brief; "
            "most were outside the selected area or were overview pages."
        )
    elif legacy_ranking_gate_empty:
        happened = "No homes were saved from this older run."
        stopped_context = (
            f"{score_demoted_total} home{'s' if score_demoted_total != 1 else ''} still matched in the earlier pass."
        )
    elif filtered_total > 0:
        happened = "No homes in scope yet."
    else:
        happened = "No homes in scope yet."
    if status_value == "failed" and replacement_run_id:
        still_worked = (
            f"The brief was saved; {score_demoted_total} home{'s' if score_demoted_total != 1 else ''} "
            "still matched before interruption while the fresh search is active."
            if legacy_ranking_gate_empty
            else "The brief was saved; the fresh search is active."
        )
    elif filtered_total > 0 and listing_total == 0 and (location_mismatch_total > 0 or area_filtered_total >= max(1, filtered_total // 2)):
        still_worked = (
            f"{raw_listing_total or filtered_total} home{'s' if (raw_listing_total or filtered_total) != 1 else ''} came back from the selected sites."
        )
    elif legacy_ranking_gate_empty:
        still_worked = (
            f"{score_demoted_total} home{'s' if score_demoted_total != 1 else ''} still matched the brief."
        )
    else:
        still_worked = (
            f"Selected sites checked {listing_total} listing{'s' if listing_total != 1 else ''}."
            if listing_total
            else "The brief and chosen sites were still saved."
        )
    if status_value == "failed" and replacement_run_id:
        next_move = (
            "Start a fresh search, or wait for the fresh check; this page switches when results are ready."
            if legacy_ranking_gate_empty
            else "Wait for the fresh check; this page switches when results are ready."
        )
    elif status_value == "failed" and repair_task_open:
        next_move = (
            "Start a fresh search, or wait for the fresh check; this page switches when results are ready."
            if legacy_ranking_gate_empty
            else "Wait for the fresh check; this page switches when results are ready."
        )
    elif status_value == "failed" and database_pressure:
        next_move = "Wait a moment, then reopen this search. PropertyQuarry retries when database capacity is free."
    elif status_value == "failed":
        next_move = (
            "Start a fresh search."
            if legacy_ranking_gate_empty
            else "Start a fresh search or change one site or requirement before retrying the same brief."
        )
    elif filtered_total > 0 and listing_total == 0 and (location_mismatch_total > 0 or area_filtered_total >= max(1, filtered_total // 2)):
        next_move = "Widen the selected districts or add a nearby radius; keep price and lifestyle preferences unchanged for the next pass."
    elif legacy_ranking_gate_empty:
        next_move = "Start a fresh search."
    else:
        next_move = (
            str(strongest_relax.get("detail") or "").strip()
            or (f"Relax {active_rule} first so the next search changes one rule at a time." if active_rule else "")
            or "Widen one rule first, then search again."
        )
    if status_value == "failed" and replacement_run_id:
        eta_feedback = stopped_context
    elif status_value == "failed" and repair_failed:
        eta_feedback = stopped_context
        if latest_repair_update:
            eta_feedback = f"{eta_feedback} Last real update: {latest_repair_update}.".strip()
    elif status_value == "failed" and repair_step_label:
        if repair_task_open:
            eta_feedback = stopped_context
        elif repair_step_label.lower().startswith("queued a generic"):
            eta_feedback = stopped_context
        else:
            eta_feedback = f"{repair_step_label}. {stopped_context}".strip()
    elif status_value == "failed" and repair_status_label:
        eta_feedback = stopped_context if repair_task_open else f"Check: {repair_status_label}. {stopped_context}".strip()
    elif status_value not in {"processed", "completed", "completed_partial", "noop", "cancelled"} and eta_label:
        eta_feedback = f"Estimated remaining time: {eta_label}."
    elif filtered_total > 0 and listing_total == 0 and (location_mismatch_total > 0 or area_filtered_total >= max(1, filtered_total // 2)):
        eta_feedback = stopped_context
    elif legacy_ranking_gate_empty:
        eta_feedback = "Start a fresh search to refresh this page."
    elif source_total:
        eta_feedback = "Change one rule and search again for a fresh read."
    elif status_value == "failed":
        eta_feedback = "A fresh check is queued; this page switches when results are ready."
    else:
        eta_feedback = "This search is complete; change one rule and search again for a fresh ETA."
    return {
        "happened": happened,
        "still_worked": still_worked,
        "next_move": next_move,
        "active_rule": active_rule,
        "eta_feedback": eta_feedback,
    }


def build_property_shortlist_snapshot(
    results: list[dict[str, object]],
    *,
    selected_candidate_ref: str = "",
) -> dict[str, object]:
    ordered_results = [
        _clean_property_shortlist_row(_strip_hidden_property_action_slots(row))
        for row in list(results or [])
        if isinstance(row, dict)
    ]
    selected = ordered_results[0] if ordered_results else {}
    normalized_selected_ref = str(selected_candidate_ref or "").strip()
    if normalized_selected_ref:
        for row in ordered_results:
            if str(row.get("candidate_ref") or "").strip() != normalized_selected_ref:
                continue
            selected = row
            break
    return PropertyShortlistSnapshot(
        results=ordered_results,
        selected=selected,
        selected_candidate_ref=str(selected.get("candidate_ref") or "").strip(),
        results_total=len(ordered_results),
        has_results=bool(ordered_results),
    ).to_dict()


def build_property_search_agent_selection_snapshot(
    property_search_agents: list[dict[str, object]],
    *,
    requested_agent_id: str,
    previous_runs: list[dict[str, object]],
    run_id: str,
) -> dict[str, object]:
    selected_agent = next(
        (
            agent
            for agent in property_search_agents
            if str(agent.get("agent_id") or "").strip() == str(requested_agent_id or "").strip()
        ),
        None,
    )
    if selected_agent is None:
        selected_agent = next(
            (agent for agent in property_search_agents if agent.get("is_active")),
            property_search_agents[0] if property_search_agents else None,
        )
    selected_agent_id = str((selected_agent or {}).get("agent_id") or "").strip()
    selected_agent_runs = [
        dict(row)
        for row in previous_runs
        if isinstance(row, dict)
        and (
            (selected_agent_id and str(row.get("agent_id") or "").strip() == selected_agent_id)
            or (
                selected_agent
                and not str(row.get("agent_id") or "").strip()
                and str(row.get("title") or "").strip() == str(selected_agent.get("location_query") or "").strip()
            )
        )
    ]
    latest_run = selected_agent_runs[0] if selected_agent_runs else {}
    open_href = ""
    edit_href = ""
    if selected_agent_id:
        open_href = f"/app/agents?agent_id={urllib.parse.quote(selected_agent_id, safe='')}"
        edit_href = f"/app/search?load_agent={urllib.parse.quote(selected_agent_id, safe='')}"
        if run_id:
            suffix = urllib.parse.quote(run_id, safe="")
            open_href = f"{open_href}&run_id={suffix}"
            edit_href = f"{edit_href}&run_id={suffix}"
    return PropertySearchAgentSelectionSnapshot(
        selected_agent=dict(selected_agent or {}),
        selected_agent_id=selected_agent_id,
        selected_agent_runs=selected_agent_runs,
        selected_agent_latest_run=dict(latest_run or {}),
        selected_agent_open_href=open_href,
        selected_agent_edit_href=edit_href,
    ).to_dict()


def build_property_workbench_candidate_snapshot(
    *,
    candidate_ref: str,
    rank: int,
    title: str,
    source_label: str,
    location_label: str,
    price_display: str,
    costs_display: str,
    price_per_sqm_display: str,
    layout_display: str,
    layout_verification_label: str,
    fit_score: int,
    fit_label: str,
    fit_summary: str,
    tour: dict[str, object],
    flythrough: dict[str, object] | None,
    orientation_preview: dict[str, object],
    ooda: dict[str, object],
    risk: dict[str, object],
    investment: dict[str, object],
    match_reasons: list[str],
    mismatch_reasons: list[str],
    review_page_neuronwriter: dict[str, object],
    packet_url: str,
    review_url: str,
    property_url: str,
    map_url: str,
    source_url: str,
    property_facts: dict[str, object],
    listing_fact_confirmation: dict[str, object],
    assessment: dict[str, object],
    objection_rows: list[dict[str, str]],
    timeline_rows: list[dict[str, str]],
    household_rows: list[dict[str, str]],
    risk_signal_rows: list[dict[str, str]],
    followup_rows: list[dict[str, str]],
    recent_change_rows: list[dict[str, str]],
    official_evidence_rows: list[dict[str, str]],
    official_posture_rows: list[dict[str, str]],
    object_rows: list[dict[str, str]],
    cost_rows: list[dict[str, str]],
    feature_values: list[dict[str, str]],
    description_text: str,
    location_text: str,
    energy_rows: list[dict[str, str]],
    household_alignment_score: int,
    household_alignment_label: str,
    floorplan_url: str = "",
    source_virtual_tour_url: str = "",
    vendor_tour_url: str = "",
    tour_url: str = "",
    verified_tour_url: str = "",
    open_tour_url: str = "",
    recovered_by_filter: bool = False,
    relaxed_filter_label: str = "",
    preview_image_url: str = "",
    diorama_preview_url: str = "",
    repair_flag_label: str = "",
    repair_flag_detail: str = "",
) -> dict[str, object]:
    return PropertyWorkbenchCandidateSnapshot(
        candidate_ref=str(candidate_ref or "").strip(),
        rank=max(1, int(rank or 1)),
        title=str(title or "Home").strip() or "Home",
        source_label=str(source_label or "").strip(),
        location_label=str(location_label or "").strip(),
        price_display=str(price_display or "").strip() or "n/a",
        costs_display=str(costs_display or "").strip(),
        price_per_sqm_display=str(price_per_sqm_display or "").strip(),
        layout_display=str(layout_display or "").strip() or "n/a",
        layout_verification_label=str(layout_verification_label or "").strip() or "unverified",
        fit_score=max(0, min(100, int(fit_score or 0))),
        fit_label=str(fit_label or "Home").strip() or "Home",
        fit_summary=str(fit_summary or "").strip(),
        tour=dict(tour or {}),
        flythrough=dict(flythrough or {}),
        orientation_preview=dict(orientation_preview or {}),
        ooda=dict(ooda or {}),
        risk=dict(risk or {}),
        investment=dict(investment or {}),
        match_reasons=[str(item).strip() for item in list(match_reasons or []) if str(item).strip()],
        mismatch_reasons=[str(item).strip() for item in list(mismatch_reasons or []) if str(item).strip()],
        review_page_neuronwriter=dict(review_page_neuronwriter or {}),
        packet_url=str(packet_url or "").strip(),
        review_url=str(review_url or "").strip(),
        property_url=str(property_url or "").strip(),
        map_url=str(map_url or "").strip(),
        source_url=str(source_url or "").strip(),
        floorplan_url=str(floorplan_url or "").strip(),
        source_virtual_tour_url=str(source_virtual_tour_url or "").strip(),
        vendor_tour_url=str(vendor_tour_url or "").strip(),
        tour_url=str(tour_url or "").strip(),
        verified_tour_url=str(verified_tour_url or "").strip(),
        open_tour_url=str(open_tour_url or "").strip(),
        property_facts=dict(property_facts or {}),
        listing_fact_confirmation=dict(listing_fact_confirmation or {}),
        assessment=dict(assessment or {}),
        objection_rows=[dict(row) for row in list(objection_rows or []) if isinstance(row, dict)],
        timeline_rows=[dict(row) for row in list(timeline_rows or []) if isinstance(row, dict)],
        household_rows=[dict(row) for row in list(household_rows or []) if isinstance(row, dict)],
        risk_signal_rows=[dict(row) for row in list(risk_signal_rows or []) if isinstance(row, dict)],
        followup_rows=[dict(row) for row in list(followup_rows or []) if isinstance(row, dict)],
        recent_change_rows=[dict(row) for row in list(recent_change_rows or []) if isinstance(row, dict)],
        official_evidence_rows=[dict(row) for row in list(official_evidence_rows or []) if isinstance(row, dict)],
        official_posture_rows=[dict(row) for row in list(official_posture_rows or []) if isinstance(row, dict)],
        object_rows=[dict(row) for row in list(object_rows or []) if isinstance(row, dict)],
        cost_rows=[dict(row) for row in list(cost_rows or []) if isinstance(row, dict)],
        feature_values=[dict(row) for row in list(feature_values or []) if isinstance(row, dict)],
        description_text=str(description_text or "").strip(),
        location_text=str(location_text or "").strip(),
        energy_rows=[dict(row) for row in list(energy_rows or []) if isinstance(row, dict)],
        household_alignment_score=max(0, int(household_alignment_score or 0)),
        household_alignment_label=str(household_alignment_label or "waiting").strip() or "waiting",
        recovered_by_filter=bool(recovered_by_filter),
        relaxed_filter_label=str(relaxed_filter_label or "").strip(),
        preview_image_url=str(preview_image_url or "").strip(),
        diorama_preview_url=str(diorama_preview_url or "").strip(),
        repair_flag_label=str(repair_flag_label or "").strip(),
        repair_flag_detail=str(repair_flag_detail or "").strip(),
    ).to_dict()


def build_property_research_packet_snapshot(
    *,
    title: str,
    summary: str,
    source_label: str,
    price: str,
    area: str,
    rooms: str,
    location: str,
    media: dict[str, object],
    preview_image: dict[str, object],
    gallery_items: list[dict[str, object]],
    location_preview: dict[str, object],
    actions: list[dict[str, object]],
    visual_status_line: str,
    source_ref: str,
    run_id: str,
    candidate_ref: str,
    overview_rows: list[dict[str, object]],
    sections: list[dict[str, object]],
    match_reasons: list[str],
    mismatch_reasons: list[str],
    score_rows: list[dict[str, object]],
    listing_rows: list[dict[str, object]],
    cost_rows: list[dict[str, object]],
    feature_values: list[dict[str, object]],
    description_text: str,
    location_text: str,
    energy_rows: list[dict[str, object]],
    missing_rows: list[dict[str, object]],
    decision_rows: list[dict[str, object]],
    official_evidence_rows: list[dict[str, object]],
    official_posture_rows: list[dict[str, object]],
    future_research_rows: list[dict[str, object]],
    evidence_overlay_rows: list[dict[str, object]],
    provenance_rows: list[dict[str, object]],
    timeline_rows: list[dict[str, object]],
    everyday_fit_rows: list[dict[str, object]],
    risk_fit_rows: list[dict[str, object]],
    investment_rows: list[dict[str, object]],
    investment_risk_rows: list[dict[str, object]],
    next_best_question: str,
    feedback: dict[str, object],
    neuronwriter: dict[str, object],
    objection_rows: list[dict[str, object]],
    household_rows: list[dict[str, object]],
    risk_signal_rows: list[dict[str, object]],
) -> dict[str, object]:
    if isinstance(preview_image, dict):
        preview_image_payload = str(preview_image.get("image_url") or preview_image.get("url") or "").strip()
    else:
        preview_image_payload = str(preview_image or "").strip()
    return PropertyResearchPacketSnapshot(
        research_title=str(title or "").strip() or "Property page",
        research_summary=str(summary or "").strip(),
        research_source_label=str(source_label or "").strip(),
        research_price=str(price or "").strip(),
        research_area=str(area or "").strip(),
        research_rooms=str(rooms or "").strip(),
        research_location=str(location or "").strip(),
        research_media=dict(media or {}),
        research_preview_image=preview_image_payload,
        research_gallery_items=[dict(row) for row in list(gallery_items or []) if isinstance(row, dict)],
        research_location_preview=dict(location_preview or {}),
        research_actions=_clean_property_action_rows(actions),
        research_visual_status_line=str(visual_status_line or "").strip(),
        research_source_ref=str(source_ref or "").strip(),
        research_run_id=str(run_id or "").strip(),
        research_candidate_ref=str(candidate_ref or "").strip(),
        research_overview_rows=[dict(row) for row in list(overview_rows or []) if isinstance(row, dict)],
        research_sections=[dict(row) for row in list(sections or []) if isinstance(row, dict)],
        research_match_reasons=[str(row).strip() for row in list(match_reasons or []) if str(row).strip()],
        research_mismatch_reasons=[str(row).strip() for row in list(mismatch_reasons or []) if str(row).strip()],
        research_score_rows=[dict(row) for row in list(score_rows or []) if isinstance(row, dict)],
        research_listing_rows=[dict(row) for row in list(listing_rows or []) if isinstance(row, dict)],
        research_cost_rows=[dict(row) for row in list(cost_rows or []) if isinstance(row, dict)],
        research_feature_values=[dict(row) for row in list(feature_values or []) if isinstance(row, dict)],
        research_description_text=str(description_text or "").strip(),
        research_location_text=str(location_text or "").strip(),
        research_energy_rows=[dict(row) for row in list(energy_rows or []) if isinstance(row, dict)],
        research_missing_rows=[dict(row) for row in list(missing_rows or []) if isinstance(row, dict)],
        research_decision_rows=[dict(row) for row in list(decision_rows or []) if isinstance(row, dict)],
        research_official_evidence_rows=[dict(row) for row in list(official_evidence_rows or []) if isinstance(row, dict)],
        research_official_posture_rows=[dict(row) for row in list(official_posture_rows or []) if isinstance(row, dict)],
        research_future_research_rows=[dict(row) for row in list(future_research_rows or []) if isinstance(row, dict)],
        research_evidence_overlay_rows=[dict(row) for row in list(evidence_overlay_rows or []) if isinstance(row, dict)],
        research_provenance_rows=[dict(row) for row in list(provenance_rows or []) if isinstance(row, dict)],
        research_timeline_rows=[dict(row) for row in list(timeline_rows or []) if isinstance(row, dict)],
        research_everyday_fit_rows=[dict(row) for row in list(everyday_fit_rows or []) if isinstance(row, dict)],
        research_risk_fit_rows=[dict(row) for row in list(risk_fit_rows or []) if isinstance(row, dict)],
        research_investment_rows=[dict(row) for row in list(investment_rows or []) if isinstance(row, dict)],
        research_investment_risk_rows=[dict(row) for row in list(investment_risk_rows or []) if isinstance(row, dict)],
        research_next_best_question=str(next_best_question or "").strip(),
        research_feedback=dict(feedback or {}),
        research_neuronwriter=dict(neuronwriter or {}),
        research_objection_rows=[dict(row) for row in list(objection_rows or []) if isinstance(row, dict)],
        research_household_rows=[dict(row) for row in list(household_rows or []) if isinstance(row, dict)],
        research_risk_signal_rows=[dict(row) for row in list(risk_signal_rows or []) if isinstance(row, dict)],
    ).to_dict()


def build_property_recurring_watch_snapshot(
    raw_agent: dict[str, object],
    *,
    property_preferences: dict[str, object],
    selected_platforms: list[str],
    selected_listing_mode: str,
    search_mode_requested: str,
    default_duration_days: int,
    default_notification_limit: int,
    default_notification_period: str,
    normalize_property_type_values: Callable[[object], list[str]],
    scope_preview_builder: Callable[..., dict[str, object]],
    safe_agent_load_payload: Callable[[dict[str, object]], dict[str, object]],
) -> dict[str, object]:
    saved_preferences = (
        dict(raw_agent.get("preferences_json") or {})
        if isinstance(raw_agent.get("preferences_json"), dict)
        else {}
    )
    agent_duration_days = _positive_int(raw_agent.get("duration_days"), default=default_duration_days)
    agent_duration_days = max(7, min(365, agent_duration_days or default_duration_days))
    agent_notification_limit = _positive_int(raw_agent.get("notification_limit"), default=default_notification_limit)
    agent_notification_limit = max(1, min(50, agent_notification_limit or default_notification_limit))
    agent_notification_period = str(raw_agent.get("notification_period") or default_notification_period).strip().lower()
    if agent_notification_period not in {"day", "week"}:
        agent_notification_period = default_notification_period
    agent_selected_platforms = (
        saved_preferences.get("selected_platforms")
        if isinstance(saved_preferences.get("selected_platforms"), list)
        else (raw_agent.get("selected_platforms") if isinstance(raw_agent.get("selected_platforms"), list) else selected_platforms)
    )
    agent_enabled = bool(raw_agent.get("enabled"))
    agent_search_goal = normalized_property_search_goal(
        saved_preferences.get("search_goal") or raw_agent.get("search_goal") or property_preferences.get("search_goal")
    )
    agent_listing_mode = effective_property_listing_mode(
        {
            **property_preferences,
            **saved_preferences,
            "search_goal": agent_search_goal,
            "listing_mode": saved_preferences.get("listing_mode") or raw_agent.get("listing_mode") or selected_listing_mode,
        },
        fallback=selected_listing_mode,
    )
    agent_country_code = str(saved_preferences.get("country_code") or raw_agent.get("country_code") or property_preferences.get("country_code") or "AT").strip().upper()
    agent_location_query = str(saved_preferences.get("location_query") or raw_agent.get("location_query") or property_preferences.get("location_query") or "").strip()
    agent_property_types = normalize_property_type_values(saved_preferences.get("property_type") or raw_agent.get("property_type") or property_preferences.get("property_type"))
    agent_region_code = str(saved_preferences.get("region_code") or raw_agent.get("region_code") or property_preferences.get("region_code") or "").strip().lower()
    agent_name = str(raw_agent.get("name") or "").strip()
    goal_mode_label = "Investment" if agent_search_goal == "investment" else ("Buy" if agent_listing_mode == "buy" else "Rent")
    if not agent_name:
        agent_name = f"{goal_mode_label} search · {agent_location_query or agent_country_code}"
    last_run_at = str(raw_agent.get("last_run_at") or "").strip()
    next_run_at = str(raw_agent.get("next_run_at") or "").strip()
    sent_in_current_window = _positive_int(raw_agent.get("sent_in_current_window"), default=0)
    remaining_notifications = max(agent_notification_limit - sent_in_current_window, 0)
    area_label = agent_location_query or agent_country_code or "No area saved"
    notification_label = f"{agent_notification_limit} per {('week' if agent_notification_period == 'week' else 'day')}"
    scope_preview = _build_scope_preview(
        scope_preview_builder,
        agent_country_code,
        agent_region_code,
        agent_location_query,
        adjacent_area_radius_m=(
            saved_preferences.get("adjacent_area_radius_m")
            or raw_agent.get("adjacent_area_radius_m")
            or property_preferences.get("adjacent_area_radius_m")
        ),
    )
    commercial_snapshot = property_commercial_snapshot(dict(property_preferences or {}))
    plan_key = str(commercial_snapshot.get("current_plan_key") or "free").strip().lower() or "free"
    try:
        plan_result_cap = int(commercial_snapshot.get("max_results_per_source") or 0)
    except Exception:
        plan_result_cap = 0
    base_load_payload = (
        safe_agent_load_payload(saved_preferences)
        if saved_preferences
        else {
            "country_code": agent_country_code,
            "region_code": agent_region_code,
            "location_query": agent_location_query,
            "property_type": agent_property_types,
            "search_mode": str(raw_agent.get("search_mode") or search_mode_requested or "strict").strip().lower() or "strict",
            "selected_platforms": list(agent_selected_platforms or []),
            "search_agent_enabled": agent_enabled,
            "search_agent_duration_days": agent_duration_days,
            "search_agent_notification_limit": agent_notification_limit,
            "search_agent_notification_period": agent_notification_period,
        }
    )
    if property_plan_has_unlimited_provider_results(plan_key, plan_result_cap):
        base_load_payload.pop("max_results_per_source", None)
    elif "max_results_per_source" in base_load_payload:
        try:
            base_load_payload["max_results_per_source"] = max(
                1,
                min(plan_result_cap or 10, int(float(str(base_load_payload.get("max_results_per_source") or "").strip()))),
            )
        except Exception:
            base_load_payload.pop("max_results_per_source", None)
    return PropertyRecurringWatchSnapshot(
        agent_id=str(raw_agent.get("agent_id") or "current").strip() or "current",
        name=agent_name,
        enabled=agent_enabled,
        is_active=bool(raw_agent.get("is_active")),
        status_label="Active" if agent_enabled else "Paused",
        duration_days=agent_duration_days,
        duration_label=(
            "1 week"
            if agent_duration_days == 7
            else "1 year"
            if agent_duration_days == 365
            else f"{agent_duration_days} days"
        ),
        notification_limit=agent_notification_limit,
        notification_period=agent_notification_period,
        notification_period_label="week" if agent_notification_period == "week" else "day",
        location_query=agent_location_query,
        listing_mode=agent_listing_mode,
        country_code=agent_country_code,
        region_code=agent_region_code,
        property_type=", ".join(agent_property_types),
        provider_count=len(agent_selected_platforms),
        last_run_label=last_run_at or "not run yet",
        next_run_label=next_run_at or ("waiting for scheduler" if agent_enabled else "paused"),
        sent_in_current_window=sent_in_current_window,
        remaining_notifications=remaining_notifications,
        area_label=area_label,
        scope_label=f"{goal_mode_label} · {area_label} · {agent_country_code}",
        scope_preview=scope_preview,
        notification_label=notification_label,
        run_label=f"Last: {last_run_at or 'not run yet'} · Next: {next_run_at or ('waiting for scheduler' if agent_enabled else 'paused')}",
        delivery_label=f"Sent {sent_in_current_window}/{agent_notification_limit} this {('week' if agent_notification_period == 'week' else 'day')}",
        load_payload={
            **base_load_payload,
            "search_goal": agent_search_goal,
            "listing_mode": agent_listing_mode,
        },
    ).to_dict()
