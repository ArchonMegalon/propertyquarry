from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.product.property_fact_enrichment import property_fact_distance_specs
from app.product.property_research_packet_links import (
    PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
    PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
    load_property_research_packet_link,
    project_property_research_packet_links,
    refresh_property_research_packet_links_for_refs,
    sync_property_research_packet_run_memberships,
    upsert_property_research_packet_links,
)


_PROPERTY_SEARCH_RUN_TTL_SECONDS = 90 * 24 * 60 * 60
_PROPERTY_SEARCH_RUN_DB_CONNECT_RETRY_SECONDS = 45.0
_PROPERTY_SEARCH_RUN_DB_CONNECT_TIMEOUT_SECONDS = 3
_PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION = 3
_PROPERTY_SEARCH_RUN_COMPACT_DELIVERY_CANDIDATE_LIMIT = 256
_PROPERTY_SEARCH_RUN_COMPACT_UI_CANDIDATE_LIMIT = 40
_PROPERTY_SEARCH_RUN_COMPACT_SOURCE_LIMIT = 64
_PROPERTY_SEARCH_RUN_COMPACT_NESTED_LIST_LIMIT = 32
_PROPERTY_SEARCH_RUN_COMPACT_TEXT_LIMIT = 2048
_PROPERTY_SEARCH_ERASURE_KEY_DOMAIN = b"propertyquarry:property-search-erasure:v1\0"
_PROPERTY_SEARCH_DISTANCE_FACT_KEYS = tuple(
    dict.fromkeys(
        str(alias or "").strip()
        for spec in property_fact_distance_specs()
        for alias in list(spec.get("aliases") or [])
        if str(alias or "").strip()
    )
)
_PROPERTY_SEARCH_FACT_EVIDENCE_KEYS = (
    "provider",
    "method",
    "observed_at",
    "expires_at",
    "freshness",
    "confidence",
    "source_key",
    "observed_key",
    "listing_url",
    "source_fingerprint",
    "coordinate_basis",
    "coordinate_observed_at",
    "coordinate_precision",
    "coordinate_source",
    "coordinate_exact",
    "listing_latitude",
    "listing_longitude",
    "coordinate_digest",
    "query_endpoint_url",
    "query_url",
    "query_digest",
    "query_schema",
    "receipt_url",
    "provider_object_id",
    "provider_object_type",
    "provider_object_version",
    "provider_object_timestamp",
    "provider_object_changeset",
    "provider_observed_at",
    "provider_expires_at",
    "attestation_version",
    "provider_attestation",
    "poi_latitude",
    "poi_longitude",
    "poi_classification_tags",
)


def _property_search_principal_key(principal_id: object) -> str:
    normalized = str(principal_id or "").strip()
    if not normalized:
        return ""
    digest = hmac.new(
        _property_search_erasure_secret().encode("utf-8"),
        _PROPERTY_SEARCH_ERASURE_KEY_DOMAIN + normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def _property_search_erasure_secret() -> str:
    runtime_mode = str(
        os.getenv("EA_RUNTIME_MODE")
        or os.getenv("PROPERTYQUARRY_RUNTIME_MODE")
        or os.getenv("ENVIRONMENT")
        or ""
    ).strip().lower()
    dedicated_secret = str(
        os.getenv("PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET") or ""
    ).strip()
    if dedicated_secret:
        if (
            runtime_mode in {"prod", "production"}
            and len(dedicated_secret.encode("utf-8")) < 32
        ):
            raise RuntimeError("property_search_erasure_secret_too_short")
        return dedicated_secret
    if runtime_mode in {"prod", "production"}:
        raise RuntimeError("property_search_erasure_secret_required")
    compatibility_secret = str(
        os.getenv("PROPERTYQUARRY_PRIVACY_LOOKUP_SECRET")
        or os.getenv("EA_SIGNING_SECRET")
        or ""
    ).strip()
    if compatibility_secret:
        return compatibility_secret
    return "propertyquarry-local-property-search-erasure-v1"


def _property_search_erasure_key_id() -> str:
    return hashlib.sha256(
        (
            "propertyquarry:property-search-erasure-key-id:v1\0"
            + _property_search_erasure_secret()
        ).encode("utf-8")
    ).hexdigest()


def _is_property_search_account_erased_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        message = str(current or "").lower()
        diagnostic = getattr(current, "diag", None)
        primary = str(getattr(diagnostic, "message_primary", "") or "").lower()
        if "property_search_account_erased" in message or "property_search_account_erased" in primary:
            return True
        current = current.__cause__ or current.__context__
    return False


def _property_search_run_db_max_connections() -> int:
    raw_value = str(
        os.getenv("PROPERTYQUARRY_SEARCH_DB_MAX_CONNECTIONS")
        or os.getenv("EA_PROPERTY_SEARCH_DB_MAX_CONNECTIONS")
        or ""
    ).strip()
    if not raw_value:
        return 4
    try:
        parsed = int(raw_value)
    except Exception:
        return 4
    return max(2, min(parsed, 16))


_PROPERTY_SEARCH_RUN_DB_SEMAPHORE = threading.BoundedSemaphore(_property_search_run_db_max_connections())

_PROPERTY_SOURCE_LISTING_CACHE_LOCK = threading.Lock()
_PROPERTY_SOURCE_LISTING_CACHE: dict[str, dict[str, object]] = {}
_PROPERTY_SOURCE_LISTING_CACHE_VERSION = "property_source_listing_cache_v1"
_PROPERTY_SOURCE_LISTING_CACHE_SCHEMA_VERSION = 1
_PROPERTY_SOURCE_LISTING_CACHE_MAX_ENTRIES = 256
_PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = ""
_PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = 0.0
_PROPERTY_SOURCE_LISTING_CACHE_SCHEMA_LOCK = threading.Lock()
_PROPERTY_SOURCE_LISTING_CACHE_SCHEMA_READY = False

_PROPERTY_SEARCH_RUN_COMPACT_TOP_LEVEL_KEYS = (
    "run_id",
    "principal_id",
    "status",
    "progress",
    "stage",
    "stage_label",
    "current_step",
    "message",
    "created_at",
    "updated_at",
    "generated_at",
    "repair_parent_run_id",
    "repair_parent_run_ids",
    "active_search_agent_id",
    "selected_platforms",
    "provider_display_total",
    "source_variant_display_total",
    "property_search_preferences",
    "preferences",
)

_PROPERTY_SEARCH_RUN_COMPACT_PREFERENCE_DROP_KEYS = (
    "raw_preferences",
    "saved_shortlist_candidates",
    "search_agents",
    "preference_bundle",
)

_PROPERTY_SEARCH_RUN_COMPACT_SUMMARY_KEYS = (
    "status",
    "message",
    "progress",
    "stage",
    "stage_label",
    "current_plan_key",
    "current_plan_label",
    "research_depth",
    "max_results_per_source",
    "provider_total",
    "provider_display_total",
    "provider_group_total",
    "provider_workers",
    "sources_total",
    "source_total",
    "source_variant_total",
    "source_variant_display_total",
    "sources_completed",
    "sources_failed",
    "source_variant_completed_total",
    "source_variant_failed_total",
    "listing_total",
    "raw_listing_total",
    "scanned_listing_total",
    "reviewed_listing_total",
    "review_deferred_total",
    "ranked_total",
    "ranked_candidate_total",
    "results_total",
    "survivor_total",
    "filtered_total",
    "held_back_total",
    "filtered_out_total",
    "filtered_area_total",
    "filtered_location_total",
    "filtered_floorplan_total",
    "filtered_property_type_total",
    "filtered_availability_total",
    "filtered_generic_page_total",
    "filtered_listing_mode_total",
    "location_mismatch_total",
    "min_score",
    "score_demoted_total",
    "evaluating_candidate_total",
    "required_fact_research_candidate_total",
    "required_fact_research_attempted_total",
    "required_fact_research_resolved_total",
    "required_fact_research_pending_total",
    "required_fact_resolution_pending",
    "required_fact_resolution_exhausted",
    "required_fact_resolution_attempts",
    "required_fact_resolution_reason",
    "completion_reason",
    "status_without_required_fact_hold",
    "required_fact_hold_applied",
    "results_delivery_semantically_blocked",
    "results_delivery_blocked_reason",
    "blocked_required_facts",
    "required_fact_resolution_receipts",
    "eta_label",
    "eta_confidence_label",
    "started_at",
    "completed_at",
    "updated_at",
    "filtered_breakdown",
    "relaxation_suggestions",
    "repair_status",
    "repair_status_label",
    "repair_step_label",
    "repair_outcome_summary",
    "repair_attempt_count",
    "repair_replacement_run_id",
    "repair_replacement_status_url",
    "repair_resolved_total",
    "repair_receipts",
    "provider_repair_task_opened_total",
    "provider_repair_task_existing_total",
    "provider_repair_tasks",
    "can_auto_repair",
    "repair_parent_run_id",
    "repair_parent_run_ids",
    "eligible_tour_total",
    "pending_tour_total",
    "ready_tour_total",
    "blocked_tour_total",
    "hosted_tour_total",
    "timing_ms",
    "timing_receipts",
)

_PROPERTY_SEARCH_RUN_COMPACT_SOURCE_KEYS = (
    "source_url",
    "source_label",
    "source_scope_label",
    "platform",
    "provider_family",
    "provider_trust_tier",
    "source_access_level",
    "verification_required",
    "provider_filter_pushdown",
    "provider_cache",
    "listing_total",
    "reviewed_listing_total",
    "raw_listing_total",
    "scanned_listing_total",
    "review_created_total",
    "review_existing_total",
    "review_deferred_total",
    "high_fit_total",
    "filtered_property_type_total",
    "filtered_area_total",
    "filtered_availability_total",
    "filtered_floorplan_total",
    "filtered_generic_page_total",
    "filtered_listing_mode_total",
    "filtered_low_fit_total",
    "score_demoted_total",
    "floorplan_recovered_total",
    "provider_repair_task_opened_total",
    "provider_repair_task_existing_total",
    "provider_repair_tasks",
    "top_fit_score",
    "status",
    "state",
    "progress",
    "error",
    "timing_ms",
    "filter_near_miss_total",
    "filter_near_miss_notified_total",
    "filter_near_misses",
    "location_mismatch_candidate_total",
    "preview_prepared_total",
)

_PROPERTY_SEARCH_RUN_COMPACT_PROVIDER_CACHE_KEYS = (
    "status",
    "cache_key",
)

_PROPERTY_SEARCH_RUN_COMPACT_PROVIDER_REPAIR_TASK_KEYS = (
    "status",
    "filter_key",
    "human_task_id",
    "queue_item_ref",
    "resolution",
    "reason",
    "repair_owner",
    "repair_workflow",
)

_PROPERTY_SEARCH_RUN_COMPACT_DELIVERY_CANDIDATE_KEYS = (
    "candidate_ref",
    "research_candidate_ref",
    "title",
    "property_url",
    "review_url",
    "source_ref",
    "source_label",
    "source_url",
    "external_id",
    "listing_id",
    "listing_uuid",
    "source_virtual_tour_url",
    "floorplan_url",
    "floorplan_urls_json",
    "tour_url",
    "open_tour_url",
    "verified_tour_url",
    "vendor_tour_url",
    "generated_reconstruction_url",
    "layout_preview_url",
    "layout_preview_status",
    "tour_media_mode",
    "generated_reconstruction_kind",
    "generated_reconstruction_disclosure",
    "tour_status",
    "blocked_reason",
)

_PROPERTY_SEARCH_RUN_COMPACT_DELIVERY_FACT_KEYS = (
    "source_virtual_tour_url",
    "has_360",
    "has_floorplan",
    "floorplan_count",
    "floorplan_urls_json",
)

_PROPERTY_SEARCH_RUN_COMPACT_UI_CANDIDATE_KEYS = (
    "candidate_ref",
    "research_candidate_ref",
    "title",
    "property_url",
    "review_url",
    "source_ref",
    "source_label",
    "source_url",
    "platform",
    "provider_family",
    "provider_trust_tier",
    "external_id",
    "listing_id",
    "listing_uuid",
    "status",
    "review_status",
    "filter_status",
    "fit_score",
    "prefilter_score",
    "score",
    "fit_label",
    "price",
    "price_text",
    "monthly_rent",
    "purchase_price",
    "currency",
    "area_m2",
    "area_sqm",
    "rooms",
    "bedrooms",
    "bathrooms",
    "floor",
    "location",
    "address",
    "district",
    "postal_code",
    "property_type",
    "listing_mode",
    "availability",
    "description",
    "summary",
    "preview_image_url",
    "thumbnail_url",
    "image_url",
    "media_urls_json",
    "floorplan_url",
    "floorplan_urls_json",
    "source_virtual_tour_url",
    "tour_url",
    "vendor_tour_url",
    "generated_reconstruction_url",
    "generated_reconstruction_kind",
    "generated_reconstruction_disclosure",
    "tour_status",
    "blocked_reason",
    "property_facts",
    "property_facts_json",
    "assessment",
    "ranking_score",
    "score_projection",
    "score_state",
    "ranking_eligible",
    "evaluation_state",
    "fact_requirement_plan",
    "search_score_context",
    "score_provenance",
)

_PROPERTY_SEARCH_RUN_COMPACT_FACT_JOB_KEYS = (
    "job_id",
    "bundle_kind",
    "status",
    "attempt",
    "poll_after_ms",
    "created_at",
    "updated_at",
    "lease_token",
    "lease_expires_at",
    "retryable",
    "required_only",
    "score_recompute_required",
    "candidate_ref",
    "root_job_id",
    "source_fingerprint",
    "facts_digest",
    "preference_digest",
    "requirement_digest",
    "request_digest",
    "result_facts_digest",
    "all_facts_resolved",
    "fields",
    "score",
    "error",
    "provider_receipts",
)


def _coerce_non_negative_int(value: object, *, default: int = 0) -> int:
    try:
        return max(0, int(float(str(value or "").strip())))
    except Exception:
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _property_search_run_database_url() -> str:
    return str(os.environ.get("DATABASE_URL") or "").strip()


def _property_search_run_db_retry_seconds() -> float:
    raw_value = str(
        os.getenv("PROPERTYQUARRY_SEARCH_DB_CONNECT_RETRY_SECONDS")
        or os.getenv("EA_PROPERTY_SEARCH_DB_CONNECT_RETRY_SECONDS")
        or ""
    ).strip()
    if not raw_value:
        return _PROPERTY_SEARCH_RUN_DB_CONNECT_RETRY_SECONDS
    try:
        parsed = float(raw_value)
    except Exception:
        return _PROPERTY_SEARCH_RUN_DB_CONNECT_RETRY_SECONDS
    return max(0.0, min(parsed, 60.0))


def _property_search_run_db_connect_timeout_seconds() -> int:
    raw_value = str(
        os.getenv("PROPERTYQUARRY_SEARCH_DB_CONNECT_TIMEOUT_SECONDS")
        or os.getenv("EA_PROPERTY_SEARCH_DB_CONNECT_TIMEOUT_SECONDS")
        or ""
    ).strip()
    if not raw_value:
        return _PROPERTY_SEARCH_RUN_DB_CONNECT_TIMEOUT_SECONDS
    try:
        parsed = int(float(raw_value))
    except Exception:
        return _PROPERTY_SEARCH_RUN_DB_CONNECT_TIMEOUT_SECONDS
    return max(1, min(parsed, 30))


def _property_search_run_db_pressure_error(exc: BaseException) -> bool:
    lowered = str(exc or "").lower()
    return any(
        marker in lowered
        for marker in (
            "too many clients",
            "remaining connection slots are reserved",
            "sorry, too many clients already",
            "connection pool exhausted",
            "database_busy",
            "timeout expired",
            "connection timed out",
            "could not connect to server",
        )
    )


@contextlib.contextmanager
def _property_search_run_connect():  # type: ignore[no-untyped-def]
    database_url = _property_search_run_database_url()
    if not database_url:
        raise RuntimeError("database_url_missing")
    import psycopg

    retry_seconds = _property_search_run_db_retry_seconds()
    deadline = time.monotonic() + retry_seconds
    last_exc: BaseException | None = None
    while True:
        timeout = max(0.0, min(0.5, deadline - time.monotonic())) if retry_seconds > 0 else 0.0
        acquired = _PROPERTY_SEARCH_RUN_DB_SEMAPHORE.acquire(timeout=timeout)
        if not acquired:
            if retry_seconds <= 0 or time.monotonic() >= deadline:
                raise RuntimeError("database_busy: property search storage connection queue is full") from last_exc
            continue

        conn = None
        try:
            try:
                conn = psycopg.connect(
                    database_url,
                    autocommit=True,
                    connect_timeout=_property_search_run_db_connect_timeout_seconds(),
                )
            except Exception as exc:
                last_exc = exc
                if _property_search_run_db_pressure_error(exc) and retry_seconds > 0 and time.monotonic() < deadline:
                    time.sleep(0.25)
                    continue
                raise
            try:
                yield conn
            finally:
                if conn is not None:
                    conn.close()
            return
        finally:
            _PROPERTY_SEARCH_RUN_DB_SEMAPHORE.release()


def _property_search_compact_candidate_preview_url(candidate: object) -> str:
    if not isinstance(candidate, dict):
        return ""
    row = dict(candidate)
    facts = dict(row.get("property_facts") or {}) if isinstance(row.get("property_facts"), dict) else {}

    def _sequence(value: object) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            return [str(item or "").strip() for item in value if str(item or "").strip()]
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        return []

    explicit_candidates: list[str] = []
    for key in (
        "preview_image_url",
        "thumbnail_url",
        "image_url",
        "hero_image_url",
        "photo_url",
        "diorama_preview_url",
    ):
        explicit_candidates.extend(_sequence(row.get(key)))
        explicit_candidates.extend(_sequence(facts.get(key)))

    media_candidates: list[str] = []
    for key in ("media_urls_json", "photo_refs", "photo_urls", "image_urls", "images_json"):
        media_candidates.extend(_sequence(row.get(key)))
        media_candidates.extend(_sequence(facts.get(key)))
    for diag_key in ("gallery_floorplan_diagnostics", "floorplan_recovery_diagnostics"):
        diag = dict(facts.get(diag_key) or {}) if isinstance(facts.get(diag_key), dict) else {}
        for key in ("media_urls_json", "candidate_document_or_media_urls", "candidate_media_urls", "image_urls"):
            media_candidates.extend(_sequence(diag.get(key)))

    floorplan_candidates: list[str] = []
    for key in ("floorplan_urls_json", "floorplan_urls", "layout_urls_json", "layout_urls"):
        floorplan_candidates.extend(_sequence(row.get(key)))
        floorplan_candidates.extend(_sequence(facts.get(key)))
    floorplan_set = set(floorplan_candidates)

    def _usable_image_url(url: str) -> str:
        normalized = str(url or "").strip()
        if not normalized or normalized.lower().startswith("data:"):
            return ""
        try:
            parsed = urllib.parse.urlparse(normalized)
        except Exception:
            return ""
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        lowered = normalized.lower()
        if any(
            marker in lowered
            for marker in (
                "/myprofile/",
                "/login",
                "/register",
                "/logo/",
                "placeholder",
                "no-image",
                "coming-soon",
                "image-not-available",
                "plus-insider-locked",
                "avatar",
            )
        ):
            return ""
        image_path = urllib.parse.urlparse(lowered).path
        if image_path.endswith(".svg"):
            return ""
        if not image_path.endswith((".jpg", ".jpeg", ".png", ".webp")):
            return ""
        return normalized

    for url in explicit_candidates:
        cleaned = _usable_image_url(url)
        if cleaned:
            return cleaned
    for url in media_candidates:
        cleaned = _usable_image_url(url)
        if cleaned and cleaned not in floorplan_set and "_thumb" not in cleaned.lower():
            return cleaned
    for url in media_candidates:
        cleaned = _usable_image_url(url)
        if cleaned and cleaned not in floorplan_set:
            return cleaned
    for url in floorplan_candidates:
        cleaned = _usable_image_url(url)
        if cleaned:
            return cleaned
    return ""


def _property_search_run_canonicalize_record(record: dict[str, object]) -> dict[str, object]:
    payload = dict(record or {})
    summary = dict(payload.get("summary") or {}) if isinstance(payload.get("summary"), dict) else {}
    if not summary:
        return payload
    compact_only = (
        str(payload.get("payload_retention_status") or "").strip().lower()
        == "compact_only"
    )
    if compact_only:
        summary.pop("sources", None)

    def _coerce_non_negative_int(value: object, *, default: int = 0) -> int:
        try:
            return max(0, int(float(str(value or "").strip())))
        except Exception:
            return default

    blocked_statuses = {
        "dismissed",
        "filtered",
        "filtered_out",
        "hard_filtered",
        "maybe_false",
        "maybe_false_positive",
        "false_positive",
        "not_a_listing",
        "repair_only",
        "queued_for_repair",
    }
    hard_filter_reasons = {
        "area_mismatch",
        "availability_mismatch",
        "generic_listing_page",
        "listing_mode_mismatch",
        "location_mismatch",
        "location_scope",
        "outside_selected_area",
        "property_location_conflicts_with_active_search",
        "property_missing_concrete_location",
        "property_type_mismatch",
        "transaction_mismatch",
        "wrong_listing_mode",
        "wrong_property_type",
    }

    def _candidate_is_rankable(candidate: dict[str, object]) -> bool:
        ranking_eligible = candidate.get("ranking_eligible")
        if ranking_eligible is False or str(ranking_eligible or "").strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return False
        if str(candidate.get("score_state") or "").strip().lower() == "evaluating":
            return False
        evaluation_state = str(
            candidate.get("evaluation_state") or ""
        ).strip().lower()
        if evaluation_state.startswith("evaluating") or evaluation_state.startswith(
            "excluded"
        ):
            return False
        for field in ("status", "review_status", "candidate_status", "filter_status", "repair_status"):
            if str(candidate.get(field) or "").strip().lower() in blocked_statuses:
                return False
        for flag in (
            "maybe_false",
            "maybe_false_positive",
            "false_positive",
            "flagged_for_repair",
            "repair_only",
            "filtered_out",
            "hard_filtered",
            "not_a_listing",
        ):
            value = candidate.get(flag)
            if isinstance(value, bool) and value:
                return False
            if str(value or "").strip().lower() in {"1", "true", "yes", "on"}:
                return False
        if str(candidate.get("hard_filter_reason") or "").strip():
            return False
        filter_reason = str(candidate.get("filter_reason") or "").strip().lower()
        if filter_reason in hard_filter_reasons:
            return False
        return True

    def _candidate_identity(candidate: dict[str, object], source_label: str) -> str:
        explicit_ref = str(candidate.get("candidate_ref") or candidate.get("research_candidate_ref") or "").strip()
        if explicit_ref:
            return explicit_ref
        property_url = urllib.parse.urldefrag(str(candidate.get("property_url") or "").strip())[0]
        if property_url:
            return property_url
        review_url = str(candidate.get("review_url") or "").strip()
        if review_url:
            return review_url
        title = str(candidate.get("title") or "").strip()
        return "|".join(part for part in (source_label, title) if part).strip()

    def _merge_candidate_rows(current: dict[str, object], incoming: dict[str, object]) -> dict[str, object]:
        merged = dict(current)
        for key, value in incoming.items():
            if key == "property_facts":
                current_facts = dict(merged.get("property_facts") or {}) if isinstance(merged.get("property_facts"), dict) else {}
                incoming_facts = dict(value or {}) if isinstance(value, dict) else {}
                if incoming_facts:
                    current_facts.update(incoming_facts)
                    merged["property_facts"] = current_facts
                continue
            if merged.get(key) in (None, "", [], {}):
                merged[key] = value
        return merged

    sources = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    ranked_candidates = [
        dict(row)
        for row in list(summary.get("ranked_candidates") or [])
        if isinstance(row, dict) and _candidate_is_rankable(row)
    ]

    source_ranked_candidates: list[dict[str, object]] = []
    if sources:
        deduped_source_candidates: dict[str, dict[str, object]] = {}
        source_candidate_order: list[str] = []
        for source in sources:
            source_label = str(source.get("source_label") or source.get("label") or "").strip()
            source_top_candidates = source.get("top_candidates")
            source_candidate_values = (
                source_top_candidates
                if isinstance(source_top_candidates, (list, tuple))
                else source.get("research_candidates") or []
            )
            candidate_rows = [
                dict(row)
                for row in list(source_candidate_values)
                if isinstance(row, dict)
            ]
            candidate_total = 0
            for candidate in candidate_rows:
                if not _candidate_is_rankable(candidate):
                    continue
                candidate.setdefault("source_label", source_label)
                identity = _candidate_identity(candidate, source_label)
                if not identity:
                    continue
                if identity in deduped_source_candidates:
                    deduped_source_candidates[identity] = _merge_candidate_rows(deduped_source_candidates[identity], candidate)
                else:
                    deduped_source_candidates[identity] = dict(candidate)
                    source_candidate_order.append(identity)
                candidate_total += 1
            if candidate_total > 0:
                source["ranked_total"] = max(
                    _coerce_non_negative_int(source.get("ranked_total")),
                    _coerce_non_negative_int(source.get("listing_total")),
                    candidate_total,
                )
                source["listing_total"] = max(
                    _coerce_non_negative_int(source.get("listing_total")),
                    _coerce_non_negative_int(source.get("ranked_total")),
                    candidate_total,
                )
                source["scanned_listing_total"] = max(
                    _coerce_non_negative_int(source.get("scanned_listing_total")),
                    _coerce_non_negative_int(source.get("reviewed_listing_total")),
                    _coerce_non_negative_int(source.get("listing_total")),
                    candidate_total,
                )
                source["reviewed_listing_total"] = max(
                    _coerce_non_negative_int(source.get("reviewed_listing_total")),
                    _coerce_non_negative_int(source.get("scanned_listing_total")),
                    _coerce_non_negative_int(source.get("listing_total")),
                    candidate_total,
                )
        source_ranked_candidates = [deduped_source_candidates[key] for key in source_candidate_order if key in deduped_source_candidates]

    if not ranked_candidates and source_ranked_candidates:
        ranked_candidates = [dict(row) for row in source_ranked_candidates]
        summary["ranked_candidates"] = ranked_candidates
    elif not ranked_candidates and not source_ranked_candidates and sources:
        visible_review_total = 0
        for source in sources:
            review_visible_total = (
                _coerce_non_negative_int(source.get("review_created_total"))
                + _coerce_non_negative_int(source.get("review_existing_total"))
            )
            visible_review_total += review_visible_total
            source["listing_total"] = review_visible_total
            source["ranked_total"] = review_visible_total
            if review_visible_total <= 0 and "top_fit_score" in source:
                source["top_fit_score"] = 0.0
        for key in (
            "listing_total",
            "ranked_total",
            "ranked_candidate_total",
            "results_total",
            "survivor_total",
        ):
            summary[key] = visible_review_total

    raw_listing_total = sum(_coerce_non_negative_int(source.get("raw_listing_total")) for source in sources)
    scanned_listing_total = sum(
        max(
            _coerce_non_negative_int(source.get("scanned_listing_total")),
            _coerce_non_negative_int(source.get("reviewed_listing_total")),
            _coerce_non_negative_int(source.get("listing_total")),
        )
        for source in sources
    )
    score_demoted_total = sum(_coerce_non_negative_int(source.get("score_demoted_total")) for source in sources)
    if raw_listing_total > 0:
        summary["raw_listing_total"] = max(_coerce_non_negative_int(summary.get("raw_listing_total")), raw_listing_total)
    if scanned_listing_total > 0:
        summary["scanned_listing_total"] = max(
            _coerce_non_negative_int(summary.get("scanned_listing_total")),
            _coerce_non_negative_int(summary.get("reviewed_listing_total")),
            scanned_listing_total,
        )
        summary["reviewed_listing_total"] = max(
            _coerce_non_negative_int(summary.get("reviewed_listing_total")),
            _coerce_non_negative_int(summary.get("scanned_listing_total")),
            scanned_listing_total,
        )
    if score_demoted_total > 0:
        summary["score_demoted_total"] = max(_coerce_non_negative_int(summary.get("score_demoted_total")), score_demoted_total)
    elif _coerce_non_negative_int(summary.get("filtered_low_fit_total")) > 0:
        summary["score_demoted_total"] = max(
            _coerce_non_negative_int(summary.get("score_demoted_total")),
            _coerce_non_negative_int(summary.get("filtered_low_fit_total")),
        )

    ranked_candidate_total = len(ranked_candidates)
    if ranked_candidate_total > 0:
        hydrated_ranked_candidates: list[dict[str, object]] = []
        for candidate in ranked_candidates:
            row = dict(candidate)
            if not str(row.get("preview_image_url") or "").strip():
                preview_url = _property_search_compact_candidate_preview_url(row)
                if preview_url:
                    row["preview_image_url"] = preview_url
            hydrated_ranked_candidates.append(row)
        ranked_candidates = hydrated_ranked_candidates
        summary["ranked_candidates"] = ranked_candidates
        for key in (
            "ranked_total",
            "ranked_candidate_total",
            "results_total",
            "survivor_total",
            "listing_total",
            "scanned_listing_total",
            "reviewed_listing_total",
        ):
            summary[key] = max(_coerce_non_negative_int(summary.get(key)), ranked_candidate_total)

    held_back_total = _coerce_non_negative_int(summary.get("held_back_total"))
    filtered_total = _coerce_non_negative_int(summary.get("filtered_total"))
    if held_back_total > 0 and filtered_total <= 0:
        summary["filtered_total"] = held_back_total
    elif filtered_total > 0 and held_back_total <= 0:
        summary["held_back_total"] = filtered_total

    if sources:
        summary["sources"] = sources
    payload["summary"] = summary
    if not str(payload.get("status") or "").strip() and str(summary.get("status") or "").strip():
        payload["status"] = str(summary.get("status") or "").strip()
    return payload


def _compact_property_search_run_record(record: dict[str, object]) -> dict[str, object]:
    preserve_fact_candidate_state = False

    def _bounded_compact_value(
        value: object,
        *,
        depth: int = 0,
        max_depth: int = 3,
    ) -> object:
        if isinstance(value, str):
            return value[:_PROPERTY_SEARCH_RUN_COMPACT_TEXT_LIMIT]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, dict):
            if depth >= max_depth:
                return {}
            return {
                str(key)[:160]: _bounded_compact_value(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
                for key, item in list(value.items())[:_PROPERTY_SEARCH_RUN_COMPACT_NESTED_LIST_LIMIT]
            }
        if isinstance(value, (list, tuple, set)):
            if depth >= max_depth:
                return []
            return [
                _bounded_compact_value(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
                for item in list(value)[:_PROPERTY_SEARCH_RUN_COMPACT_NESTED_LIST_LIMIT]
            ]
        return str(value)[:_PROPERTY_SEARCH_RUN_COMPACT_TEXT_LIMIT]

    def _compact_provider_cache_row(value: object) -> dict[str, object]:
        payload = dict(value or {}) if isinstance(value, dict) else {}
        return {
            key: _bounded_compact_value(payload[key])
            for key in _PROPERTY_SEARCH_RUN_COMPACT_PROVIDER_CACHE_KEYS
            if key in payload
        }

    def _compact_fact_evidence_row(value: object) -> dict[str, object]:
        """Preserve the complete signed contract instead of positional slices."""
        payload = dict(value or {}) if isinstance(value, dict) else {}
        compact = {
            key: _bounded_compact_value(payload[key], max_depth=2)
            for key in _PROPERTY_SEARCH_FACT_EVIDENCE_KEYS
            if key in payload
        }
        tags = payload.get("poi_classification_tags")
        if isinstance(tags, dict):
            compact["poi_classification_tags"] = {
                str(key)[:160]: _bounded_compact_value(item, max_depth=1)
                for key, item in tags.items()
            }
        return compact

    def _compact_fact_evidence_map(value: object) -> dict[str, object]:
        payload = dict(value or {}) if isinstance(value, dict) else {}
        return {
            str(key)[:160]: _compact_fact_evidence_row(item)
            for key, item in payload.items()
            if isinstance(item, dict)
        }

    def _compact_fact_job_fields(value: object) -> list[object]:
        rows: list[object] = []
        for item in list(value or [])[:_PROPERTY_SEARCH_RUN_COMPACT_NESTED_LIST_LIMIT]:
            if not isinstance(item, dict):
                rows.append(_bounded_compact_value(item, max_depth=4))
                continue
            compact_item = dict(
                _bounded_compact_value(item, max_depth=4) or {}
            )
            provenance = item.get("provenance")
            if isinstance(provenance, dict):
                compact_item["provenance"] = _compact_fact_evidence_row(
                    provenance
                )
            rows.append(compact_item)
        return rows

    def _compact_provider_repair_task_row(value: object) -> dict[str, object]:
        payload = dict(value or {}) if isinstance(value, dict) else {}
        return {
            key: _bounded_compact_value(payload[key])
            for key in _PROPERTY_SEARCH_RUN_COMPACT_PROVIDER_REPAIR_TASK_KEYS
            if key in payload
        }

    def _compact_delivery_candidate_row(value: object) -> dict[str, object]:
        payload = dict(value or {}) if isinstance(value, dict) else {}
        compact_candidate = {
            key: _bounded_compact_value(payload[key])
            for key in _PROPERTY_SEARCH_RUN_COMPACT_DELIVERY_CANDIDATE_KEYS
            if payload.get(key) not in (None, "", [], {})
        }
        facts = dict(payload.get("property_facts") or {}) if isinstance(payload.get("property_facts"), dict) else {}
        compact_facts = {
            key: _bounded_compact_value(facts[key])
            for key in _PROPERTY_SEARCH_RUN_COMPACT_DELIVERY_FACT_KEYS
            if facts.get(key) not in (None, "", [], {})
        }
        if compact_facts:
            compact_candidate["property_facts"] = compact_facts
        return compact_candidate

    def _compact_ui_candidate_row(value: object) -> dict[str, object]:
        payload = dict(value or {}) if isinstance(value, dict) else {}
        compact_candidate = {
            key: _bounded_compact_value(payload[key])
            for key in _PROPERTY_SEARCH_RUN_COMPACT_UI_CANDIDATE_KEYS
            if payload.get(key) not in (None, "", [], {})
        }
        for facts_key in ("property_facts", "property_facts_json"):
            raw_facts = payload.get(facts_key)
            if not isinstance(raw_facts, dict):
                compact_candidate.pop(facts_key, None)
                continue
            fact_priority_keys = (
                "map_lat",
                "map_lng",
                "map_location_precision",
                "map_location_source",
                "map_coordinate_evidence",
                "property_fact_evidence",
                *_PROPERTY_SEARCH_DISTANCE_FACT_KEYS,
            )
            ordered_fact_items = [
                (key, raw_facts[key])
                for key in fact_priority_keys
                if key in raw_facts
            ]
            remaining_fact_items = [
                (key, item)
                for key, item in raw_facts.items()
                if key not in fact_priority_keys
            ]
            remaining_capacity = max(
                0,
                _PROPERTY_SEARCH_RUN_COMPACT_NESTED_LIST_LIMIT
                - len(ordered_fact_items),
            )
            ordered_fact_items.extend(
                remaining_fact_items[:remaining_capacity]
            )
            compact_facts = {
                str(key)[:160]: (
                    _compact_fact_evidence_map(item)
                    if key == "property_fact_evidence"
                    else _bounded_compact_value(item)
                )
                for key, item in ordered_fact_items
                if "diagnostic" not in str(key).strip().lower()
                and item not in (None, "", [], {})
            }
            if compact_facts:
                compact_candidate[facts_key] = compact_facts
            else:
                compact_candidate.pop(facts_key, None)
        if not str(compact_candidate.get("preview_image_url") or "").strip():
            preview_url = _property_search_compact_candidate_preview_url(payload)
            if preview_url:
                compact_candidate["preview_image_url"] = _bounded_compact_value(preview_url)
        return compact_candidate

    def _compact_fact_job_row(value: object) -> dict[str, object]:
        payload = dict(value or {}) if isinstance(value, dict) else {}
        return {
            key: (
                _compact_fact_job_fields(payload[key])
                if key == "fields"
                else _bounded_compact_value(
                    payload[key],
                    max_depth=4 if key == "provider_receipts" else 3,
                )
            )
            for key in _PROPERTY_SEARCH_RUN_COMPACT_FACT_JOB_KEYS
            if payload.get(key) not in (None, "", [], {})
        }

    def _candidate_has_delivery_signal(value: object) -> bool:
        payload = dict(value or {}) if isinstance(value, dict) else {}
        facts = dict(payload.get("property_facts") or {}) if isinstance(payload.get("property_facts"), dict) else {}
        return bool(
            str(payload.get("tour_status") or "").strip()
            or str(payload.get("tour_url") or "").strip()
            or str(payload.get("vendor_tour_url") or "").strip()
            or str(payload.get("generated_reconstruction_url") or "").strip()
            or str(payload.get("source_virtual_tour_url") or facts.get("source_virtual_tour_url") or "").strip()
            or payload.get("floorplan_url")
            or payload.get("floorplan_urls_json")
            or facts.get("has_360")
            or facts.get("has_floorplan")
            or facts.get("floorplan_count")
            or facts.get("floorplan_urls_json")
        )

    def _compact_source_row(value: object) -> dict[str, object]:
        payload = dict(value or {}) if isinstance(value, dict) else {}
        compact_row = {
            key: _bounded_compact_value(payload[key])
            for key in _PROPERTY_SEARCH_RUN_COMPACT_SOURCE_KEYS
            if key in payload
        }
        if isinstance(compact_row.get("provider_cache"), dict):
            compact_row["provider_cache"] = _compact_provider_cache_row(compact_row.get("provider_cache"))
        if isinstance(compact_row.get("provider_repair_tasks"), list):
            compact_row["provider_repair_tasks"] = [
                _compact_provider_repair_task_row(task)
                for task in compact_row.get("provider_repair_tasks") or []
                if isinstance(task, dict)
            ]
        if isinstance(compact_row.get("filter_near_misses"), list):
            compact_row["filter_near_misses"] = [
                {
                    key: item[key]
                    for key in (
                        "property_url",
                        "title",
                        "failed_filter_key",
                        "failed_filter_label",
                        "requested_distance_m",
                        "observed_distance_m",
                        "observed_place_name",
                        "prefilter_score",
                    )
                    if isinstance(item, dict) and key in item
                }
                for item in compact_row.get("filter_near_misses") or []
                if isinstance(item, dict)
            ]
        if preserve_fact_candidate_state:
            for candidate_key in (
                "research_candidates",
                "top_candidates",
                "evaluating_candidates",
            ):
                candidate_rows = payload.get(candidate_key)
                if isinstance(candidate_rows, (list, tuple)):
                    compact_row[candidate_key] = [
                        _compact_ui_candidate_row(row)
                        for row in list(candidate_rows)[:_PROPERTY_SEARCH_RUN_COMPACT_UI_CANDIDATE_LIMIT]
                        if isinstance(row, dict)
                    ]
        return compact_row

    payload = _property_search_run_canonicalize_record(dict(record or {}))
    compact = {
        key: _bounded_compact_value(payload[key])
        for key in _PROPERTY_SEARCH_RUN_COMPACT_TOP_LEVEL_KEYS
        if key in payload
    }
    for preference_key in ("property_search_preferences", "preferences"):
        if isinstance(compact.get(preference_key), dict):
            preferences = dict(compact[preference_key])
            for drop_key in _PROPERTY_SEARCH_RUN_COMPACT_PREFERENCE_DROP_KEYS:
                preferences.pop(drop_key, None)
            compact[preference_key] = preferences
    summary = dict(payload.get("summary") or {}) if isinstance(payload.get("summary"), dict) else {}
    preserve_fact_candidate_state = bool(
        summary.get("fact_enrichment_jobs")
        or summary.get("optional_fact_enrichment_candidate_jobs")
        or summary.get("required_fact_enrichment_candidate_jobs")
        or summary.get("fact_enrichment_candidate_jobs")
        or summary.get("required_fact_resolution_pending") is True
        or summary.get("required_fact_resolution_exhausted") is True
        or _coerce_non_negative_int(summary.get("evaluating_candidate_total")) > 0
    )
    if summary:
        compact_summary = {
            key: _bounded_compact_value(summary[key])
            for key in _PROPERTY_SEARCH_RUN_COMPACT_SUMMARY_KEYS
            if key in summary
        }
        compact_summary.setdefault(
            "required_fact_resolution_pending",
            bool(summary.get("required_fact_resolution_pending")),
        )
        compact_summary.setdefault(
            "required_fact_resolution_exhausted",
            bool(summary.get("required_fact_resolution_exhausted")),
        )
        compact_summary.setdefault(
            "evaluating_candidate_total",
            _coerce_non_negative_int(summary.get("evaluating_candidate_total")),
        )
        for candidate_key in (
            "ranked_candidates",
            "results",
            "top_candidates",
            "evaluating_candidates",
            "research_candidates",
        ):
            candidate_rows = summary.get(candidate_key)
            if isinstance(candidate_rows, (list, tuple)):
                compact_summary[candidate_key] = [
                    _compact_ui_candidate_row(row)
                    for row in list(candidate_rows)[:_PROPERTY_SEARCH_RUN_COMPACT_UI_CANDIDATE_LIMIT]
                    if isinstance(row, dict)
                ]
        raw_jobs = summary.get("fact_enrichment_jobs")
        if isinstance(raw_jobs, dict):
            compact_summary["fact_enrichment_jobs"] = {
                str(job_id)[:160]: _compact_fact_job_row(job)
                for job_id, job in list(raw_jobs.items())[:_PROPERTY_SEARCH_RUN_COMPACT_UI_CANDIDATE_LIMIT]
                if isinstance(job, dict)
            }
        for candidate_jobs_key in (
            "optional_fact_enrichment_candidate_jobs",
            "required_fact_enrichment_candidate_jobs",
            "fact_enrichment_candidate_jobs",
        ):
            raw_candidate_jobs = summary.get(candidate_jobs_key)
            if isinstance(raw_candidate_jobs, dict):
                compact_summary[candidate_jobs_key] = {
                    str(candidate_ref)[:160]: str(job_id)[:160]
                    for candidate_ref, job_id in list(raw_candidate_jobs.items())[:_PROPERTY_SEARCH_RUN_COMPACT_UI_CANDIDATE_LIMIT]
                    if str(candidate_ref or "").strip() and str(job_id or "").strip()
                }
        if isinstance(summary.get("sources"), list):
            compact_summary["sources"] = [
                _compact_source_row(row)
                for row in list(summary.get("sources") or [])[:_PROPERTY_SEARCH_RUN_COMPACT_SOURCE_LIMIT]
                if isinstance(row, dict)
            ]
        if compact_summary:
            compact["summary"] = compact_summary
        delivery_candidates: list[dict[str, object]] = []
        seen_delivery_candidates: set[str] = set()
        inferred_eligible = 0
        inferred_pending = 0
        inferred_ready = 0
        inferred_blocked = 0
        candidate_groups: list[object] = [
            summary.get("_delivery_candidates"),
            summary.get("top_candidates"),
        ]
        for source in list(summary.get("sources") or []):
            if not isinstance(source, dict):
                continue
            candidate_groups.append(source.get("top_candidates"))
        if not any(isinstance(group, (list, tuple)) and group for group in candidate_groups):
            candidate_groups.append(summary.get("ranked_candidates"))
        delivery_projection_truncated = bool(summary.get("_delivery_projection_truncated"))
        for candidate_group in candidate_groups:
            for candidate in list(candidate_group or []) if isinstance(candidate_group, (list, tuple)) else []:
                if not isinstance(candidate, dict) or not _candidate_has_delivery_signal(candidate):
                    continue
                compact_candidate = _compact_delivery_candidate_row(candidate)
                identity = "|".join(
                    str(compact_candidate.get(key) or "").strip()
                    for key in ("candidate_ref", "source_ref", "property_url", "review_url", "title")
                )
                if not identity.strip("|") or identity in seen_delivery_candidates:
                    continue
                seen_delivery_candidates.add(identity)
                inferred_eligible += 1
                candidate_status = str(compact_candidate.get("tour_status") or "").strip().lower()
                generated_urls = {
                    str(value or "").strip()
                    for value in (
                        compact_candidate.get("generated_reconstruction_url"),
                        compact_candidate.get("layout_preview_url"),
                    )
                    if str(value or "").strip()
                }
                tour_media_mode = str(
                    compact_candidate.get("tour_media_mode") or ""
                ).strip().lower()
                verified_tour_url = str(
                    compact_candidate.get("verified_tour_url") or ""
                ).strip()
                candidate_tour_url = str(
                    compact_candidate.get("tour_url") or ""
                ).strip()
                candidate_open_tour_url = str(
                    compact_candidate.get("open_tour_url") or ""
                ).strip()
                candidate_ready_urls = [
                    candidate_url
                    for field_name, candidate_url in (
                        ("verified_tour_url", verified_tour_url),
                        ("tour_url", candidate_tour_url),
                        ("open_tour_url", candidate_open_tour_url),
                    )
                    if candidate_url
                    and candidate_url not in generated_urls
                    and not (
                        tour_media_mode == "generated_reconstruction"
                        and field_name != "verified_tour_url"
                    )
                ]
                if candidate_status in {"blocked", "failed", "skipped", "not_applicable"} or str(
                    compact_candidate.get("blocked_reason") or ""
                ).strip():
                    inferred_blocked += 1
                elif candidate_status == "ready" and candidate_ready_urls:
                    inferred_ready += 1
                elif candidate_status == "ready":
                    compact_candidate["tour_status"] = "blocked"
                    compact_candidate.setdefault(
                        "blocked_reason",
                        "property_tour_rebuild_required",
                    )
                    inferred_blocked += 1
                else:
                    inferred_pending += 1
                if len(delivery_candidates) >= _PROPERTY_SEARCH_RUN_COMPACT_DELIVERY_CANDIDATE_LIMIT:
                    delivery_projection_truncated = True
                    continue
                delivery_candidates.append(compact_candidate)
        if delivery_candidates:
            compact_summary["_delivery_candidates"] = delivery_candidates
        if delivery_projection_truncated:
            compact_summary["_delivery_projection_truncated"] = True
            try:
                prior_projection_total = max(0, int(summary.get("_delivery_projection_total") or 0))
            except Exception:
                prior_projection_total = 0
            if prior_projection_total <= 0 and inferred_eligible <= len(delivery_candidates):
                prior_projection_total = len(delivery_candidates) + 1
            compact_summary["_delivery_projection_total"] = max(inferred_eligible, prior_projection_total)
        def _merge_inferred_count(key: str, inferred_value: int) -> None:
            try:
                existing_value = max(0, int(float(str(compact_summary.get(key) or "0").strip())))
            except Exception:
                existing_value = 0
            compact_summary[key] = max(existing_value, inferred_value)

        _merge_inferred_count("eligible_tour_total", inferred_eligible)
        _merge_inferred_count("pending_tour_total", inferred_pending)
        _merge_inferred_count("ready_tour_total", inferred_ready)
        _merge_inferred_count("blocked_tour_total", inferred_blocked)
        _merge_inferred_count("hosted_tour_total", inferred_ready)
        compact["summary"] = compact_summary
    if "run_id" not in compact and payload.get("run_id"):
        compact["run_id"] = payload.get("run_id")
    if "principal_id" not in compact and payload.get("principal_id"):
        compact["principal_id"] = payload.get("principal_id")
    if "status" not in compact and summary.get("status"):
        compact["status"] = summary.get("status")
    compact["compact_schema_version"] = _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION
    compact_summary = dict(compact.get("summary") or {}) if isinstance(compact.get("summary"), dict) else {}

    def _summary_count(key: str) -> int:
        try:
            return max(0, int(float(str(compact_summary.get(key) or "0").strip())))
        except Exception:
            return 0

    eligible_tour_total = _summary_count("eligible_tour_total")
    pending_tour_total = _summary_count("pending_tour_total")
    ready_tour_total = _summary_count("ready_tour_total")
    blocked_tour_total = _summary_count("blocked_tour_total")
    delivery_projection_total = _summary_count("_delivery_projection_total")
    compact["delivery_pending"] = bool(
        bool(compact_summary.get("required_fact_resolution_pending"))
        or pending_tour_total > 0
        or ready_tour_total + blocked_tour_total < eligible_tour_total
        or (
            bool(compact_summary.get("_delivery_projection_truncated"))
            and delivery_projection_total > eligible_tour_total
        )
    )
    return compact


def _property_search_run_compact_supports_delivery(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        version = int(record.get("compact_schema_version") or 0)
    except Exception:
        version = 0
    if version < _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION:
        return False
    summary = dict(record.get("summary") or {}) if isinstance(record.get("summary"), dict) else {}
    if bool(summary.get("_delivery_projection_truncated")):
        return False
    return all(
        key in summary
        for key in (
            "eligible_tour_total",
            "pending_tour_total",
            "ready_tour_total",
            "blocked_tour_total",
            "required_fact_resolution_pending",
            "required_fact_resolution_exhausted",
            "evaluating_candidate_total",
        )
    )


def _compact_property_search_run_record_with_row_timestamps(
    payload: object,
    *,
    created_at: object = "",
    updated_at: object = "",
    delivery_checked_at: object = "",
    compact_schema_version: object | None = None,
    delivery_pending: object | None = None,
    payload_retention_status: object = "",
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    normalized_payload = dict(payload or {})
    normalized_retention_status = str(payload_retention_status or "").strip()
    if normalized_retention_status:
        normalized_payload["payload_retention_status"] = normalized_retention_status
    compact = _property_search_run_canonicalize_record(normalized_payload)

    if compact_schema_version is not None:
        try:
            row_compact_schema_version = int(compact_schema_version or 0)
        except (TypeError, ValueError):
            row_compact_schema_version = -1
        try:
            embedded_compact_schema_version = int(compact.get("compact_schema_version") or 0)
        except (TypeError, ValueError):
            embedded_compact_schema_version = -1
        if row_compact_schema_version > 0 and (
            row_compact_schema_version != _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION
            or embedded_compact_schema_version != row_compact_schema_version
        ):
            compact = {
                key: compact[key]
                for key in ("run_id", "principal_id", "status", "created_at", "updated_at")
                if compact.get(key) not in (None, "")
            }
            compact.update(
                {
                    "compact_schema_version": 0,
                    "compact_contract_status": "repair_required",
                    "compact_contract_expected_version": _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
                    "compact_contract_row_version": row_compact_schema_version,
                    "compact_contract_embedded_version": embedded_compact_schema_version,
                    "delivery_pending": True,
                }
            )
        else:
            compact["delivery_pending"] = bool(delivery_pending)

    def _timestamp_text(value: object) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value or "").strip()

    row_created_at = _timestamp_text(created_at)
    row_updated_at = _timestamp_text(updated_at)
    row_delivery_checked_at = _timestamp_text(delivery_checked_at)
    if not str(compact.get("created_at") or "").strip() and row_created_at:
        compact["created_at"] = row_created_at
    if not str(compact.get("updated_at") or "").strip() and row_updated_at:
        compact["updated_at"] = row_updated_at
    if row_delivery_checked_at:
        compact["delivery_checked_at"] = row_delivery_checked_at
    summary = compact.get("summary")
    if isinstance(summary, dict):
        compact_summary = dict(summary)
        if not str(compact_summary.get("updated_at") or "").strip() and row_updated_at:
            compact_summary["updated_at"] = row_updated_at
        compact["summary"] = compact_summary
    return compact


def _compact_pruned_property_search_run_record(
    record: dict[str, object],
    *,
    pruned_at: str | None = None,
) -> dict[str, object]:
    compact = _compact_property_search_run_record(record)
    summary = compact.get("summary")
    if isinstance(summary, dict):
        compact_summary = dict(summary)
        compact_summary.pop("sources", None)
        compact["summary"] = compact_summary
    compact["payload_retention_status"] = "compact_only"
    compact["payload_pruned_at"] = str(pruned_at or _now_iso()).strip() or _now_iso()
    return compact


def _compact_property_search_run_json_sql() -> str:
    preference_drop_sql = " ".join(
        f"- '{key}'"
        for key in _PROPERTY_SEARCH_RUN_COMPACT_PREFERENCE_DROP_KEYS
    )
    top_objects = " ||\n                            ".join(
        (
            f"jsonb_build_object('{key}', (payload_json -> '{key}') {preference_drop_sql})"
            if key in {"property_search_preferences", "preferences"}
            else f"jsonb_build_object('{key}', payload_json -> '{key}')"
        )
        for key in _PROPERTY_SEARCH_RUN_COMPACT_TOP_LEVEL_KEYS
    )
    summary_objects = " ||\n                                    ".join(
        f"jsonb_build_object('{key}', payload_json #> '{{summary,{key}}}')"
        for key in _PROPERTY_SEARCH_RUN_COMPACT_SUMMARY_KEYS
    )
    return f"""
                    jsonb_strip_nulls(
                        (
                            {top_objects}
                        )
                        ||
                        jsonb_build_object(
                            'summary',
                            jsonb_strip_nulls(
                                {summary_objects}
                            )
                        )
                    )
                """


def _require_property_search_run_schema() -> None:
    database_url = _property_search_run_database_url()
    if not database_url:
        return
    from app.product.property_search_schema import require_property_search_schema_ready

    require_property_search_schema_ready(database_url)


def _property_search_run_transaction(conn: object):  # type: ignore[no-untyped-def]
    transaction = getattr(conn, "transaction", None)
    if callable(transaction):
        return transaction()
    # Storage always supplies psycopg connections; this compatibility branch is
    # reserved for existing in-memory test doubles that predate dual writes.
    return contextlib.nullcontext()


def _set_property_search_writer_contract(cursor: object) -> None:
    cursor.execute(  # type: ignore[attr-defined]
        """
        SELECT set_config(
                   'propertyquarry.property_search_writer_contract',
                   %s,
                   TRUE
               ),
               set_config(
                   'propertyquarry.property_search_erasure_key_id',
                   %s,
                   TRUE
               )
        """,
        (
            str(PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION),
            _property_search_erasure_key_id(),
        ),
    )


def _record_property_search_erasure_fence(
    cursor: object,
    *,
    principal_key: str,
    run_id: str = "",
) -> None:
    normalized_key = str(principal_key or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not normalized_key:
        raise ValueError("property_search_principal_key_required")
    cursor.execute(  # type: ignore[attr-defined]
        "SELECT property_search_assert_erasure_key()",
    )
    cursor.execute(  # type: ignore[attr-defined]
        """
        SELECT pg_advisory_xact_lock(
            hashtextextended('property_search_erasure:' || %s, 0)
        )
        """,
        (normalized_key,),
    )
    cursor.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO property_search_erasure_fences (
            principal_key,
            run_id,
            erased_at
        )
        VALUES (%s, %s, NOW())
        ON CONFLICT (principal_key, run_id) DO UPDATE
        SET erased_at = GREATEST(
            property_search_erasure_fences.erased_at,
            EXCLUDED.erased_at
        )
        """,
        (normalized_key, normalized_run_id),
    )


class PropertyAccountPublicationForbiddenError(RuntimeError):
    """An erased account cannot create or republish customer artifacts."""


@contextlib.contextmanager
def property_account_publication_authority(  # type: ignore[no-untyped-def]
    principal_id: object,
    *,
    run_id: object = "",
):
    """Hold account/run-erasure authority for the complete publication commit.

    The advisory lock is intentionally identical to the lock used when an
    account erasure fence is recorded.  Whichever transaction starts first is
    therefore ordered before the other: a completed publication is swept by
    the eraser, while a completed erasure makes every later publication fail.
    The yielded connection remains inside that same transaction so dependent
    receipts can be committed without releasing publication authority.
    """

    normalized_principal = str(principal_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not normalized_principal:
        raise ValueError("property_account_publication_principal_required")
    database_url = _property_search_run_database_url()
    if not database_url:
        runtime_mode = str(
            os.getenv("EA_RUNTIME_MODE")
            or os.getenv("PROPERTYQUARRY_RUNTIME_MODE")
            or os.getenv("ENVIRONMENT")
            or ""
        ).strip().lower()
        if runtime_mode in {"prod", "production"}:
            raise RuntimeError("property_account_publication_authority_unavailable")
        yield None
        return

    _require_property_search_run_schema()
    principal_key = _property_search_principal_key(normalized_principal)
    if not principal_key:
        raise ValueError("property_search_principal_key_required")
    with _property_search_run_connect() as conn:
        with _property_search_run_transaction(conn):
            with conn.cursor() as cur:
                _set_property_search_writer_contract(cur)
                cur.execute("SELECT property_search_assert_erasure_key()")
                cur.execute(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtextextended('property_search_erasure:' || %s, 0)
                    )
                    """,
                    (principal_key,),
                )
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM property_search_erasure_fences
                        WHERE principal_key = %s
                          AND (run_id = '' OR run_id = %s)
                    )
                    """,
                    (principal_key, normalized_run_id),
                )
                row = cur.fetchone()
                if not row or bool(row[0]):
                    raise PropertyAccountPublicationForbiddenError(
                        "property_account_publication_forbidden"
                    )
                yield conn


def _store_property_search_run_record(record: dict[str, object]) -> bool:
    if not _property_search_run_database_url():
        return True
    _require_property_search_run_schema()
    normalized_record = _property_search_run_canonicalize_record(dict(record or {}))
    run_id = str(normalized_record.get("run_id") or "").strip()
    principal_id = str(normalized_record.get("principal_id") or "").strip()
    if not run_id or not principal_id:
        return False
    principal_key = _property_search_principal_key(principal_id)
    if not principal_key:
        return False
    write_timestamp = _now_iso()
    normalized_record["created_at"] = (
        str(normalized_record.get("created_at") or write_timestamp).strip() or write_timestamp
    )
    normalized_record["updated_at"] = (
        str(normalized_record.get("updated_at") or write_timestamp).strip() or write_timestamp
    )
    from psycopg.types.json import Json

    compact_record = _compact_property_search_run_record(normalized_record)
    packet_links = project_property_research_packet_links(normalized_record)
    status_value = str(compact_record.get("status") or "").strip() or None
    try:
        with _property_search_run_connect() as conn:
            with _property_search_run_transaction(conn):
                with conn.cursor() as cur:
                    _set_property_search_writer_contract(cur)
                    cur.execute(
                        """
                    INSERT INTO property_search_runs (
                        run_id,
                        principal_id,
                        principal_key,
                        payload_json,
                        status,
                        compact_json,
                        compact_schema_version,
                        delivery_pending,
                        created_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (principal_id, run_id) DO UPDATE
                    SET principal_key = EXCLUDED.principal_key,
                        payload_json = EXCLUDED.payload_json,
                        status = EXCLUDED.status,
                        compact_json = EXCLUDED.compact_json,
                        compact_schema_version = EXCLUDED.compact_schema_version,
                        delivery_pending = EXCLUDED.delivery_pending,
                        delivery_checked_at = CASE
                            WHEN EXCLUDED.delivery_pending THEN NULL
                            ELSE property_search_runs.delivery_checked_at
                        END,
                        updated_at = EXCLUDED.updated_at
                    WHERE property_search_runs.payload_json IS DISTINCT FROM EXCLUDED.payload_json
                       OR property_search_runs.principal_key IS DISTINCT FROM EXCLUDED.principal_key
                       OR property_search_runs.status IS DISTINCT FROM EXCLUDED.status
                       OR property_search_runs.compact_json IS DISTINCT FROM EXCLUDED.compact_json
                       OR property_search_runs.compact_schema_version IS DISTINCT FROM EXCLUDED.compact_schema_version
                       OR property_search_runs.delivery_pending IS DISTINCT FROM EXCLUDED.delivery_pending
                    """,
                        (
                            run_id,
                            principal_id,
                            principal_key,
                            Json(normalized_record),
                            status_value,
                            Json(compact_record),
                            _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
                            bool(compact_record.get("delivery_pending", True)),
                            normalized_record["created_at"],
                            normalized_record["updated_at"],
                        ),
                    )
                    upsert_property_research_packet_links(cur, packet_links)
                    sync_property_research_packet_run_memberships(
                        cur,
                        principal_id=principal_id,
                        run_id=run_id,
                        links=packet_links,
                    )
    except Exception as exc:
        if _is_property_search_account_erased_error(exc):
            return False
        raise
    return True


def _compare_and_swap_property_search_run_record(
    *,
    principal_id: str,
    run_id: str,
    expected_record_sha256: str,
    updated_record: dict[str, object],
) -> dict[str, object]:
    """Atomically replace one exact run revision and its packet projections."""

    from app.product.property_search_tour_binding import property_search_run_record_sha256

    if not _property_search_run_database_url():
        return {"status": "durable_storage_required"}
    _require_property_search_run_schema()
    normalized_principal = str(principal_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    normalized_expected_sha256 = str(expected_record_sha256 or "").strip().lower()
    if (
        not normalized_principal
        or not normalized_run_id
        or len(normalized_expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in normalized_expected_sha256)
    ):
        return {"status": "invalid_request"}

    normalized_record = _property_search_run_canonicalize_record(dict(updated_record or {}))
    if (
        str(normalized_record.get("principal_id") or "").strip() != normalized_principal
        or str(normalized_record.get("run_id") or "").strip() != normalized_run_id
    ):
        return {"status": "identity_mismatch"}
    principal_key = _property_search_principal_key(normalized_principal)
    if not principal_key:
        return {"status": "identity_mismatch"}
    write_timestamp = _now_iso()
    normalized_record["created_at"] = (
        str(normalized_record.get("created_at") or write_timestamp).strip()
        or write_timestamp
    )
    normalized_record["updated_at"] = (
        str(normalized_record.get("updated_at") or write_timestamp).strip()
        or write_timestamp
    )
    compact_record = _compact_property_search_run_record(normalized_record)
    packet_links = project_property_research_packet_links(normalized_record)
    status_value = str(compact_record.get("status") or "").strip() or None
    from psycopg.types.json import Json

    try:
        with _property_search_run_connect() as conn:
            with _property_search_run_transaction(conn):
                with conn.cursor() as cur:
                    _set_property_search_writer_contract(cur)
                    cur.execute(
                        """
                        SELECT payload_json
                        FROM property_search_runs
                        WHERE principal_id = %s AND run_id = %s
                        FOR UPDATE
                        """,
                        (normalized_principal, normalized_run_id),
                    )
                    locked_row = cur.fetchone()
                    if not locked_row or not isinstance(locked_row[0], dict):
                        return {"status": "not_found"}
                    locked_record = _property_search_run_canonicalize_record(
                        dict(locked_row[0] or {})
                    )
                    locked_sha256 = property_search_run_record_sha256(locked_record)
                    if not hmac.compare_digest(
                        locked_sha256,
                        normalized_expected_sha256,
                    ):
                        return {
                            "status": "record_changed",
                            "record_sha256": locked_sha256,
                        }

                    cur.execute(
                        """
                        UPDATE property_search_runs
                        SET principal_key = %s,
                            payload_json = %s,
                            status = %s,
                            compact_json = %s,
                            compact_schema_version = %s,
                            delivery_pending = %s,
                            delivery_checked_at = CASE
                                WHEN %s THEN NULL
                                ELSE delivery_checked_at
                            END,
                            updated_at = %s
                        WHERE principal_id = %s AND run_id = %s
                        RETURNING payload_json
                        """,
                        (
                            principal_key,
                            Json(normalized_record),
                            status_value,
                            Json(compact_record),
                            _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
                            bool(compact_record.get("delivery_pending", True)),
                            bool(compact_record.get("delivery_pending", True)),
                            normalized_record["updated_at"],
                            normalized_principal,
                            normalized_run_id,
                        ),
                    )
                    persisted_row = cur.fetchone()
                    if not persisted_row or not isinstance(persisted_row[0], dict):
                        return {"status": "store_rejected"}
                    upsert_property_research_packet_links(cur, packet_links)
                    sync_property_research_packet_run_memberships(
                        cur,
                        principal_id=normalized_principal,
                        run_id=normalized_run_id,
                        links=packet_links,
                    )
                    persisted_record = _property_search_run_canonicalize_record(
                        dict(persisted_row[0] or {})
                    )
                    return {
                        "status": "applied",
                        "record": persisted_record,
                        "record_sha256": property_search_run_record_sha256(
                            persisted_record
                        ),
                    }
    except Exception as exc:
        if _is_property_search_account_erased_error(exc):
            return {"status": "store_rejected"}
        raise


def _store_property_search_run_compact_record(record: dict[str, object]) -> bool:
    """Refresh the bounded scheduler/UI projection without rewriting the full payload."""
    if not _property_search_run_database_url():
        return False
    _require_property_search_run_schema()
    normalized_record = _property_search_run_canonicalize_record(dict(record or {}))
    run_id = str(normalized_record.get("run_id") or "").strip()
    principal_id = str(normalized_record.get("principal_id") or "").strip()
    if not run_id or not principal_id:
        return False
    expected_updated_at = str(normalized_record.get("updated_at") or "").strip()
    if not expected_updated_at:
        return False
    expected_delivery_checked_at = str(normalized_record.get("delivery_checked_at") or "").strip() or None
    from psycopg.types.json import Json

    compact_record = _compact_property_search_run_record(normalized_record)
    with _property_search_run_connect() as conn:
        with _property_search_run_transaction(conn):
            with conn.cursor() as cur:
                _set_property_search_writer_contract(cur)
                cur.execute(
                    """
                UPDATE property_search_runs
                SET compact_json = %s,
                    compact_schema_version = %s,
                    delivery_pending = %s,
                    delivery_checked_at = CURRENT_TIMESTAMP
                WHERE run_id = %s AND principal_id = %s
                  AND updated_at = %s::timestamptz
                  AND delivery_checked_at IS NOT DISTINCT FROM %s::timestamptz
                RETURNING 1
                """,
                    (
                        Json(compact_record),
                        _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
                        bool(compact_record.get("delivery_pending", True)),
                        run_id,
                        principal_id,
                        expected_updated_at,
                        expected_delivery_checked_at,
                    ),
                )
                updated = cur.fetchone() is not None
                return updated


def _mark_property_search_run_delivery_checked(record: dict[str, object]) -> bool:
    """Advance the durable delivery fairness cursor without rewriting JSON."""
    if not _property_search_run_database_url():
        return False
    _require_property_search_run_schema()
    normalized_record = _property_search_run_canonicalize_record(dict(record or {}))
    run_id = str(normalized_record.get("run_id") or "").strip()
    principal_id = str(normalized_record.get("principal_id") or "").strip()
    expected_updated_at = str(normalized_record.get("updated_at") or "").strip()
    if not run_id or not principal_id or not expected_updated_at:
        return False
    expected_delivery_checked_at = str(normalized_record.get("delivery_checked_at") or "").strip() or None
    with _property_search_run_connect() as conn:
        with _property_search_run_transaction(conn):
            with conn.cursor() as cur:
                _set_property_search_writer_contract(cur)
                cur.execute(
                    """
                UPDATE property_search_runs
                SET delivery_checked_at = CURRENT_TIMESTAMP
                WHERE run_id = %s AND principal_id = %s
                  AND updated_at = %s::timestamptz
                  AND delivery_checked_at IS NOT DISTINCT FROM %s::timestamptz
                RETURNING 1
                    """,
                    (run_id, principal_id, expected_updated_at, expected_delivery_checked_at),
                )
                return cur.fetchone() is not None


def _load_property_search_run_record(*, run_id: str, principal_id: str) -> dict[str, object] | None:
    if not _property_search_run_database_url():
        return None
    _require_property_search_run_schema()
    normalized_run_id = str(run_id or "").strip()
    normalized_principal_id = str(principal_id or "").strip()
    if not normalized_run_id or not normalized_principal_id:
        return None
    with _property_search_run_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload_json FROM property_search_runs WHERE run_id = %s AND principal_id = %s",
                (normalized_run_id, normalized_principal_id),
            )
            row = cur.fetchone()
    if not row:
        return None
    return (
        _property_search_run_canonicalize_record(dict(row[0] or {}))
        if isinstance(row[0], dict)
        else None
    )


def _load_property_search_run_compact_record(*, run_id: str, principal_id: str) -> dict[str, object] | None:
    if not _property_search_run_database_url():
        return None
    _require_property_search_run_schema()
    normalized_run_id = str(run_id or "").strip()
    normalized_principal_id = str(principal_id or "").strip()
    if not normalized_run_id or not normalized_principal_id:
        return None
    with _property_search_run_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(
                    NULLIF(compact_json, '{}'::jsonb),
                    jsonb_build_object(
                        'run_id', to_jsonb(run_id),
                        'principal_id', to_jsonb(principal_id),
                        'status', to_jsonb(status),
                        'created_at', to_jsonb(created_at),
                        'updated_at', to_jsonb(updated_at)
                    )
                ),
                created_at,
                updated_at,
                delivery_checked_at,
                compact_schema_version,
                delivery_pending,
                payload_json->>'payload_retention_status'
                FROM property_search_runs
                WHERE run_id = %s AND principal_id = %s
                """,
                (normalized_run_id, normalized_principal_id),
            )
            row = cur.fetchone()
    if not row:
        return None
    return _compact_property_search_run_record_with_row_timestamps(
        row[0],
        created_at=row[1] if len(row) > 1 else "",
        updated_at=row[2] if len(row) > 2 else "",
        delivery_checked_at=row[3] if len(row) > 3 else "",
        compact_schema_version=row[4] if len(row) > 4 else None,
        delivery_pending=row[5] if len(row) > 5 else None,
        payload_retention_status=row[6] if len(row) > 6 else "",
    )


def _load_property_research_packet_link(
    *,
    principal_id: str,
    candidate_ref: str,
) -> dict[str, object] | None:
    if not _property_search_run_database_url():
        return None
    normalized_principal_id = str(principal_id or "").strip()
    normalized_candidate_ref = str(candidate_ref or "").strip()
    if not normalized_principal_id or not normalized_candidate_ref:
        return None
    _require_property_search_run_schema()
    with _property_search_run_connect() as conn:
        with conn.cursor() as cur:
            return load_property_research_packet_link(
                cur,
                principal_id=normalized_principal_id,
                candidate_ref=normalized_candidate_ref,
            )


def _load_property_research_packet_link_for_run(
    *,
    principal_id: str,
    run_id: str,
    candidate_ref: str,
) -> dict[str, object] | None:
    """Load one indexed packet only when it belongs to the requested run."""

    if not _property_search_run_database_url():
        return None
    normalized_principal_id = str(principal_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    normalized_candidate_ref = str(candidate_ref or "").strip()
    if not normalized_principal_id or not normalized_run_id or not normalized_candidate_ref:
        return None
    _require_property_search_run_schema()
    with _property_search_run_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM property_research_packet_run_memberships
                WHERE principal_id = %s
                  AND run_id = %s
                  AND candidate_ref = %s
                """,
                (
                    normalized_principal_id,
                    normalized_run_id,
                    normalized_candidate_ref,
                ),
            )
            if cur.fetchone() is None:
                return None
            return load_property_research_packet_link(
                cur,
                principal_id=normalized_principal_id,
                candidate_ref=normalized_candidate_ref,
            )


def _property_research_packet_index_coverage_complete() -> bool:
    """Return true only for the DB-backed, current writer/packet coverage receipt."""

    if not _property_search_run_database_url():
        return False
    _require_property_search_run_schema()
    with _property_search_run_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT coverage_status,
                       writer_contract_version,
                       packet_schema_version,
                       expected_membership_rows,
                       verified_membership_rows,
                       expected_distinct_tenant_refs,
                       verified_distinct_tenant_refs
                FROM property_research_packet_index_state
                WHERE singleton = TRUE
                """
            )
            row = cur.fetchone()
    if not row:
        return False
    return bool(
        str(row[0] or "").strip() == "complete"
        and int(row[1] or 0) == PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION
        and int(row[2] or 0) == PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION
        and int(row[3] or 0) == int(row[4] or 0)
        and int(row[5] or 0) == int(row[6] or 0)
    )


def _list_property_search_run_records(
    *,
    limit: int = 20,
    statuses: tuple[str, ...] = (),
    principal_id: str = "",
    admin: bool = False,
    lightweight: bool = False,
    delivery_work_only: bool = False,
    registry: dict[str, dict[str, object]] | None = None,
) -> tuple[dict[str, object], ...]:
    normalized_limit = max(int(limit or 0), 1)
    normalized_statuses = tuple(
        sorted({str(value or "").strip().lower() for value in statuses if str(value or "").strip()})
    )
    normalized_principal_id = str(principal_id or "").strip()
    if not normalized_principal_id and not admin:
        return ()
    if not _property_search_run_database_url():
        rows = [dict(value) for value in (registry or {}).values() if isinstance(value, dict)]
        if normalized_principal_id:
            rows = [row for row in rows if str(row.get("principal_id") or "").strip() == normalized_principal_id]
        if normalized_statuses:
            rows = [row for row in rows if str(row.get("status") or "").strip().lower() in normalized_statuses]
        if lightweight:
            rows = [_compact_property_search_run_record(row) for row in rows]
        if delivery_work_only:
            rows = [
                row
                for row in rows
                if str(row.get("compact_schema_version") or "") != str(_PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION)
                or bool(row.get("delivery_pending", True))
            ]
            rows.sort(
                key=lambda row: (
                    int(row.get("compact_schema_version") or 0),
                    1 if str(row.get("delivery_checked_at") or "").strip() else 0,
                    str(row.get("delivery_checked_at") or ""),
                    str(row.get("updated_at") or row.get("created_at") or ""),
                )
            )
        else:
            rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
        return tuple(rows[:normalized_limit])
    _require_property_search_run_schema()
    query = (
        """
        SELECT COALESCE(
            NULLIF(compact_json, '{}'::jsonb),
            jsonb_build_object(
                'run_id', to_jsonb(run_id),
                'principal_id', to_jsonb(principal_id),
                'status', to_jsonb(status),
                'created_at', to_jsonb(created_at),
                'updated_at', to_jsonb(updated_at)
            )
        ),
        created_at,
        updated_at,
        delivery_checked_at,
        compact_schema_version,
        delivery_pending,
        payload_json->>'payload_retention_status'
        FROM property_search_runs
        """
        if lightweight
        else "SELECT payload_json FROM property_search_runs"
    )
    params: list[object] = []
    where_clauses: list[str] = []
    if normalized_principal_id:
        where_clauses.append("principal_id = %s")
        params.append(normalized_principal_id)
    if normalized_statuses:
        where_clauses.append("status = ANY(%s)")
        params.append(list(normalized_statuses))
    if delivery_work_only:
        where_clauses.append(
            "(compact_schema_version <> %s "
            "OR NOT (compact_json @> jsonb_build_object("
            "'compact_schema_version', compact_schema_version)) "
            "OR delivery_pending)"
        )
        params.append(_PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION)
    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)
    if delivery_work_only:
        query += (
            " ORDER BY compact_schema_version ASC,"
            " delivery_checked_at ASC NULLS FIRST, updated_at ASC LIMIT %s"
        )
    else:
        query += " ORDER BY updated_at DESC LIMIT %s"
    params.append(normalized_limit)
    with _property_search_run_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    results: list[dict[str, object]] = []
    for row in rows:
        payload = row[0] if row else None
        compact = _compact_property_search_run_record_with_row_timestamps(
            payload,
            created_at=row[1] if len(row) > 1 else "",
            updated_at=row[2] if len(row) > 2 else "",
            delivery_checked_at=row[3] if len(row) > 3 else "",
            compact_schema_version=row[4] if len(row) > 4 else None,
            delivery_pending=row[5] if len(row) > 5 else None,
            payload_retention_status=row[6] if len(row) > 6 else "",
        )
        if compact is not None:
            results.append(compact)
    return tuple(results)


def _prune_property_search_run_records() -> None:
    if not _property_search_run_database_url():
        return
    retention_seconds = _property_search_run_retention_seconds()
    if retention_seconds <= 0:
        return
    _require_property_search_run_schema()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=retention_seconds)).isoformat()
    pruned_at = _now_iso()
    with _property_search_run_connect() as conn:
        with _property_search_run_transaction(conn):
            with conn.cursor() as cur:
                _set_property_search_writer_contract(cur)
                cur.execute(
                    """
                    DELETE FROM property_research_packet_run_memberships AS memberships
                    USING property_search_runs AS runs,
                          property_research_packet_links AS links
                    WHERE memberships.principal_id = runs.principal_id
                      AND memberships.run_id = runs.run_id
                      AND links.principal_id = memberships.principal_id
                      AND links.candidate_ref = memberships.candidate_ref
                      AND runs.updated_at < %s
                      AND links.retention_state <> 'legal_hold'
                    RETURNING memberships.principal_id, memberships.candidate_ref
                    """,
                    (cutoff,),
                )
                expired_memberships = tuple(cur.fetchall() or ())
                refs_by_principal: dict[str, set[str]] = {}
                for row in expired_memberships:
                    refs_by_principal.setdefault(str(row[0] or "").strip(), set()).add(
                        str(row[1] or "").strip()
                    )
                for membership_principal, candidate_refs in refs_by_principal.items():
                    refresh_property_research_packet_links_for_refs(
                        cur,
                        principal_id=membership_principal,
                        candidate_refs=candidate_refs,
                    )
                cur.execute(
                    f"""
                WITH stale_runs AS (
                    SELECT
                        principal_id,
                        run_id,
                        (COALESCE(NULLIF(compact_json, '{{}}'::jsonb), {_compact_property_search_run_json_sql()})
                         #- '{{summary,sources}}') AS compacted
                    FROM property_search_runs
                    WHERE updated_at < %s
                      AND COALESCE(payload_json->>'payload_retention_status', '') <> 'compact_only'
                )
                UPDATE property_search_runs AS runs
                SET compact_json = stale_runs.compacted,
                    payload_json = stale_runs.compacted || jsonb_build_object(
                        'payload_retention_status', 'compact_only',
                        'payload_pruned_at', %s::text
                    ),
                    status = COALESCE(runs.status, stale_runs.compacted->>'status')
                FROM stale_runs
                WHERE runs.principal_id = stale_runs.principal_id
                  AND runs.run_id = stale_runs.run_id
                """,
                    (cutoff, pruned_at),
                )


def _delete_property_search_run_record(
    *,
    run_id: str,
    principal_id: str,
    registry: dict[str, dict[str, object]] | None = None,
) -> bool:
    normalized_run_id = str(run_id or "").strip()
    normalized_principal_id = str(principal_id or "").strip()
    if not normalized_run_id or not normalized_principal_id:
        return False
    if not _property_search_run_database_url():
        if registry is None:
            return False
        record = registry.get(normalized_run_id)
        if str(dict(record or {}).get("principal_id") or "").strip() != normalized_principal_id:
            return False
        return registry.pop(normalized_run_id, None) is not None
    _require_property_search_run_schema()
    principal_key = _property_search_principal_key(normalized_principal_id)
    if not principal_key:
        return False
    with _property_search_run_connect() as conn:
        with _property_search_run_transaction(conn):
            with conn.cursor() as cur:
                _set_property_search_writer_contract(cur)
                _record_property_search_erasure_fence(
                    cur,
                    principal_key=principal_key,
                    run_id=normalized_run_id,
                )
                cur.execute(
                    """
                    DELETE FROM property_search_work_jobs
                    WHERE principal_id = %s AND run_id = %s
                    """,
                    (normalized_principal_id, normalized_run_id),
                )
                cur.execute(
                    """
                    SELECT candidate_ref
                    FROM property_research_packet_run_memberships
                    WHERE principal_id = %s AND run_id = %s
                    FOR UPDATE
                    """,
                    (normalized_principal_id, normalized_run_id),
                )
                affected_refs = tuple(str(row[0] or "").strip() for row in list(cur.fetchall() or []))
                cur.execute(
                    "DELETE FROM property_search_runs WHERE run_id = %s AND principal_id = %s",
                    (normalized_run_id, normalized_principal_id),
                )
                deleted = bool(cur.rowcount)
                if deleted and affected_refs:
                    refresh_property_research_packet_links_for_refs(
                        cur,
                        principal_id=normalized_principal_id,
                        candidate_refs=affected_refs,
                    )
                return deleted


def _export_property_research_packet_data_for_principal(
    *,
    principal_id: str,
) -> tuple[dict[str, object], ...]:
    """Validated DSAR export of packet rows and their exact run memberships."""

    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal or not _property_search_run_database_url():
        return ()
    _require_property_search_run_schema()
    exported: list[dict[str, object]] = []
    with _property_search_run_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT candidate_ref
                FROM property_research_packet_links
                WHERE principal_id = %s
                ORDER BY candidate_ref ASC
                """,
                (normalized_principal,),
            )
            candidate_refs = tuple(str(row[0] or "").strip() for row in list(cur.fetchall() or []))
            for candidate_ref in candidate_refs:
                link = load_property_research_packet_link(
                    cur,
                    principal_id=normalized_principal,
                    candidate_ref=candidate_ref,
                    active_only=False,
                )
                if not isinstance(link, dict):
                    continue
                cur.execute(
                    """
                    SELECT run_id, observed_at, source_rank, packet_sha256
                    FROM property_research_packet_run_memberships
                    WHERE principal_id = %s AND candidate_ref = %s
                    ORDER BY observed_at ASC, run_id ASC
                    """,
                    (normalized_principal, candidate_ref),
                )
                memberships = [
                    {
                        "run_id": str(row[0] or ""),
                        "observed_at": row[1],
                        "source_rank": int(row[2] or 0),
                        "packet_sha256": str(row[3] or "").strip(),
                    }
                    for row in list(cur.fetchall() or [])
                ]
                exported.append(
                    {
                        **link,
                        "candidate_ref": candidate_ref,
                        "principal_id": normalized_principal,
                        "run_memberships": memberships,
                    }
                )
    return tuple(exported)


def _erase_property_search_account_data(
    *,
    principal_id: str = "",
    principal_ids: tuple[str, ...] = (),
) -> dict[str, int]:
    """Atomically erase tenant aliases while retaining explicit legal holds.

    Run memberships are erased with their runs.  A held materialized packet is
    deliberately retained without memberships as the evidence-only hold record;
    all other packet material is erased.
    """

    normalized_principals = tuple(
        sorted(
            {
                normalized
                for normalized in (
                    str(value or "").strip()
                    for value in (tuple(principal_ids or ()) + (principal_id,))
                )
                if normalized
            }
        )
    )
    empty_result = {
        "runs_deleted": 0,
        "work_jobs_deleted": 0,
        "packet_links_deleted": 0,
        "packet_links_legal_hold_retained": 0,
    }
    if not normalized_principals or not _property_search_run_database_url():
        return empty_result
    _require_property_search_run_schema()
    principal_keys = tuple(
        sorted(
            {
                key
                for key in (
                    _property_search_principal_key(value)
                    for value in normalized_principals
                )
                if key
            }
        )
    )
    if len(principal_keys) != len(normalized_principals):
        raise ValueError("property_search_principal_key_required")
    with _property_search_run_connect() as conn:
        with _property_search_run_transaction(conn):
            with conn.cursor() as cur:
                _set_property_search_writer_contract(cur)
                for principal_key in principal_keys:
                    _record_property_search_erasure_fence(
                        cur,
                        principal_key=principal_key,
                    )
                cur.execute(
                    """
                    DELETE FROM property_search_work_jobs
                    WHERE principal_id = ANY(%s)
                    """,
                    (list(normalized_principals),),
                )
                work_jobs_deleted = max(0, int(cur.rowcount or 0))
                cur.execute(
                    "DELETE FROM property_search_runs WHERE principal_id = ANY(%s)",
                    (list(normalized_principals),),
                )
                runs_deleted = max(0, int(cur.rowcount or 0))
                # Memberships cascade with runs. This defensive delete also covers
                # interrupted pre-contract rows before deleting their materialization.
                cur.execute(
                    """
                    DELETE FROM property_research_packet_run_memberships
                    WHERE principal_id = ANY(%s)
                    """,
                    (list(normalized_principals),),
                )
                cur.execute(
                    """
                    DELETE FROM property_research_packet_links
                    WHERE principal_id = ANY(%s)
                      AND retention_state <> 'legal_hold'
                    """,
                    (list(normalized_principals),),
                )
                packet_links_deleted = max(0, int(cur.rowcount or 0))
                cur.execute(
                    """
                    SELECT candidate_ref
                    FROM property_research_packet_links
                    WHERE principal_id = ANY(%s)
                      AND retention_state = 'legal_hold'
                    FOR SHARE
                    """,
                    (list(normalized_principals),),
                )
                packet_links_legal_hold_retained = len(tuple(cur.fetchall() or ()))
    return {
        "runs_deleted": runs_deleted,
        "work_jobs_deleted": work_jobs_deleted,
        "packet_links_deleted": packet_links_deleted,
        "packet_links_legal_hold_retained": packet_links_legal_hold_retained,
    }


def _property_search_run_retention_seconds() -> int:
    raw_value = str(os.getenv("EA_PROPERTY_SEARCH_RUN_RETENTION_SECONDS") or "").strip()
    if not raw_value:
        return _PROPERTY_SEARCH_RUN_TTL_SECONDS
    try:
        parsed = int(raw_value)
    except Exception:
        return _PROPERTY_SEARCH_RUN_TTL_SECONDS
    return max(0, min(parsed, 10 * 365 * 24 * 60 * 60))


def property_search_run_retention_policy() -> dict[str, str]:
    retention_seconds = _property_search_run_retention_seconds()
    return {
        "property_search_run_retention_status": "enabled" if retention_seconds > 0 else "disabled",
        "property_search_run_retention_seconds": str(retention_seconds),
        "property_search_run_retention_days": str(round(retention_seconds / 86400, 2)),
        "property_search_run_retention_env": "EA_PROPERTY_SEARCH_RUN_RETENTION_SECONDS",
        "property_search_run_retention_default_seconds": str(_PROPERTY_SEARCH_RUN_TTL_SECONDS),
    }


def _property_source_listing_cache_ttl_seconds() -> int:
    raw_value = str(os.getenv("EA_PROPERTY_SOURCE_LISTING_CACHE_TTL_SECONDS") or "").strip()
    if not raw_value:
        return 15 * 60
    try:
        parsed = int(raw_value)
    except Exception:
        return 15 * 60
    return max(0, min(parsed, 24 * 60 * 60))


def _property_source_listing_cache_stale_max_seconds() -> int:
    raw_value = str(os.getenv("EA_PROPERTY_SOURCE_LISTING_CACHE_STALE_MAX_SECONDS") or "").strip()
    if not raw_value:
        return 6 * 60 * 60
    try:
        parsed = int(raw_value)
    except Exception:
        return 6 * 60 * 60
    return max(0, min(parsed, 7 * 24 * 60 * 60))


def _property_source_listing_cache_path() -> Path | None:
    raw_value = str(os.getenv("EA_PROPERTY_SOURCE_LISTING_CACHE_PATH") or "").strip()
    if not raw_value or raw_value.lower() in {"0", "false", "no", "off", "disabled"}:
        return None
    return Path(raw_value).expanduser()


def _property_source_listing_cache_backend() -> str:
    raw_value = os.getenv("EA_PROPERTY_SOURCE_LISTING_CACHE_BACKEND")
    storage_backend = str(os.getenv("EA_STORAGE_BACKEND") or "").strip().lower()
    configured = str(raw_value or "").strip().lower()
    if configured not in {"", "auto", "memory", "file", "postgres"}:
        configured = "auto"
    if configured in {"memory", "file", "postgres"}:
        return configured
    if raw_value is None and storage_backend == "postgres" and _property_search_run_database_url():
        return "postgres"
    if configured == "auto" and _property_search_run_database_url():
        return "postgres"
    if raw_value is None and _property_source_listing_cache_path() is not None:
        return "file"
    if _property_source_listing_cache_path() is not None:
        return "file"
    return "memory"


def _property_source_listing_cache_key(*, source_url: str, source_spec: dict[str, object] | None = None) -> str:
    spec = dict(source_spec or {})
    configured = str(spec.get("provider_cache_key") or "").strip()
    if configured:
        return configured[:240]
    pushdown = dict(spec.get("provider_filter_pushdown") or {}) if isinstance(spec.get("provider_filter_pushdown"), dict) else {}
    pushdown_key = str(pushdown.get("cache_key") or "").strip()
    if pushdown_key:
        return pushdown_key[:240]
    return ""


def _property_source_listing_cache_normalize_row(raw_key: object, raw_row: object, *, now: float | None = None) -> dict[str, object]:
    cache_key = str(raw_key or "").strip()[:240]
    if not cache_key or not isinstance(raw_row, dict):
        return {}
    try:
        stored_at = float(raw_row.get("stored_at_epoch") or 0.0)
    except Exception:
        stored_at = 0.0
    effective_now = float(now or time.time())
    urls = [str(value or "").strip() for value in list(raw_row.get("listing_urls") or []) if str(value or "").strip()]
    if not urls:
        return {}
    return {
        "cache_key": cache_key,
        "source_url": urllib.parse.urldefrag(str(raw_row.get("source_url") or "").strip())[0],
        "listing_urls": urls[:250],
        "stored_at_epoch": stored_at or effective_now,
        "provider_filter_pushdown": dict(raw_row.get("provider_filter_pushdown") or {})
        if isinstance(raw_row.get("provider_filter_pushdown"), dict)
        else {},
    }


def _property_source_listing_cache_row_state(
    *,
    cache_key: str,
    row: dict[str, object],
    allow_stale: bool,
    persistence: str,
) -> tuple[tuple[str, ...], dict[str, object]]:
    now = time.time()
    ttl = _property_source_listing_cache_ttl_seconds()
    stale_max = _property_source_listing_cache_stale_max_seconds()
    try:
        stored_at = float(row.get("stored_at_epoch") or 0.0)
    except Exception:
        stored_at = 0.0
    age_seconds = max(0.0, now - stored_at)
    if not allow_stale and (ttl <= 0 or age_seconds > float(ttl)):
        return (), {}
    if allow_stale and (
        ttl <= 0
        or (age_seconds > float(ttl) and stale_max <= 0)
        or (stale_max > 0 and age_seconds > float(stale_max))
    ):
        return (), {}
    urls = tuple(str(value or "").strip() for value in list(row.get("listing_urls") or []) if str(value or "").strip())
    if not urls:
        return (), {}
    state = {
        "status": "stale_fallback" if ttl > 0 and age_seconds > float(ttl) else "hit",
        "cache_key": cache_key,
        "age_seconds": round(age_seconds, 2),
        "listing_total": len(urls),
        "persistence": persistence,
        "revalidation": "candidate_preview",
    }
    return urls, state


def _property_source_listing_cache_prune_locked() -> None:
    while len(_PROPERTY_SOURCE_LISTING_CACHE) > _PROPERTY_SOURCE_LISTING_CACHE_MAX_ENTRIES:
        oldest_key = min(
            _PROPERTY_SOURCE_LISTING_CACHE,
            key=lambda key: float(_PROPERTY_SOURCE_LISTING_CACHE.get(key, {}).get("stored_at_epoch") or 0.0),
        )
        _PROPERTY_SOURCE_LISTING_CACHE.pop(oldest_key, None)


def _property_source_listing_cache_snapshot_locked() -> dict[str, dict[str, object]]:
    now = time.time()
    retention_seconds = max(
        _property_source_listing_cache_ttl_seconds(),
        _property_source_listing_cache_stale_max_seconds(),
    )
    snapshot: dict[str, dict[str, object]] = {}
    for key, row in _PROPERTY_SOURCE_LISTING_CACHE.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        try:
            stored_at = float(row.get("stored_at_epoch") or 0.0)
        except Exception:
            stored_at = 0.0
        if retention_seconds > 0 and stored_at > 0.0 and now - stored_at > float(retention_seconds):
            continue
        urls = [str(value or "").strip() for value in list(row.get("listing_urls") or []) if str(value or "").strip()]
        if not urls:
            continue
        snapshot[normalized_key] = {
            "cache_key": normalized_key,
            "source_url": urllib.parse.urldefrag(str(row.get("source_url") or "").strip())[0],
            "listing_urls": urls[:250],
            "stored_at_epoch": stored_at or now,
            "provider_filter_pushdown": dict(row.get("provider_filter_pushdown") or {})
            if isinstance(row.get("provider_filter_pushdown"), dict)
            else {},
        }
    return dict(
        sorted(
            snapshot.items(),
            key=lambda item: float(item[1].get("stored_at_epoch") or 0.0),
            reverse=True,
        )[:_PROPERTY_SOURCE_LISTING_CACHE_MAX_ENTRIES]
    )


@contextlib.contextmanager
def _property_source_listing_cache_file_lock(path: Path):
    lock_path = path.with_name(f"{path.name}.lock")
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                handle.close()
            except Exception:
                pass


def _property_source_listing_cache_quarantine_corrupt_file(path: Path, *, reason: str) -> str:
    if not path.exists():
        return ""
    suffix = f"corrupt-{int(time.time())}-{uuid4().hex[:12]}"
    quarantine_path = path.with_name(f"{path.name}.{suffix}.json")
    try:
        path.replace(quarantine_path)
    except Exception:
        return ""
    return f"{quarantine_path}:{reason}"


def _ensure_property_source_listing_cache_schema() -> bool:
    global _PROPERTY_SOURCE_LISTING_CACHE_SCHEMA_READY
    if _PROPERTY_SOURCE_LISTING_CACHE_SCHEMA_READY:
        return True
    if not _property_search_run_database_url():
        return False
    with _PROPERTY_SOURCE_LISTING_CACHE_SCHEMA_LOCK:
        if _PROPERTY_SOURCE_LISTING_CACHE_SCHEMA_READY:
            return True
        try:
            _require_property_search_run_schema()
        except Exception:
            return False
        _PROPERTY_SOURCE_LISTING_CACHE_SCHEMA_READY = True
        return True


def _property_source_listing_cache_prune_postgres() -> None:
    if not _ensure_property_source_listing_cache_schema():
        return
    retention_seconds = max(
        _property_source_listing_cache_ttl_seconds(),
        _property_source_listing_cache_stale_max_seconds(),
    )
    try:
        with _property_search_run_connect() as conn:
            with conn.cursor() as cur:
                if retention_seconds > 0:
                    cur.execute(
                        "DELETE FROM property_source_listing_cache WHERE stored_at_epoch < %s",
                        (time.time() - float(retention_seconds),),
                    )
                cur.execute(
                    """
                    DELETE FROM property_source_listing_cache
                    WHERE cache_key IN (
                        SELECT cache_key
                        FROM property_source_listing_cache
                        ORDER BY stored_at_epoch DESC
                        OFFSET %s
                    )
                    """,
                    (_PROPERTY_SOURCE_LISTING_CACHE_MAX_ENTRIES,),
                )
    except Exception:
        return


def _property_source_listing_cache_get_postgres(cache_key: str) -> dict[str, object]:
    normalized_key = str(cache_key or "").strip()[:240]
    if not normalized_key or not _ensure_property_source_listing_cache_schema():
        return {}
    try:
        with _property_search_run_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cache_key, source_url, listing_urls, provider_filter_pushdown, stored_at_epoch
                    FROM property_source_listing_cache
                    WHERE cache_key = %s
                    """,
                    (normalized_key,),
                )
                row = cur.fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    return _property_source_listing_cache_normalize_row(
        row[0],
        {
            "source_url": row[1],
            "listing_urls": row[2],
            "provider_filter_pushdown": row[3],
            "stored_at_epoch": row[4],
        },
    )


def _property_source_listing_cache_put_postgres(row: dict[str, object]) -> bool:
    normalized = _property_source_listing_cache_normalize_row(row.get("cache_key"), row)
    if not normalized or not _ensure_property_source_listing_cache_schema():
        return False
    from psycopg.types.json import Json

    try:
        with _property_search_run_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO property_source_listing_cache (
                        cache_key,
                        source_url,
                        listing_urls,
                        provider_filter_pushdown,
                        stored_at_epoch,
                        stored_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (cache_key) DO UPDATE
                    SET source_url = EXCLUDED.source_url,
                        listing_urls = EXCLUDED.listing_urls,
                        provider_filter_pushdown = EXCLUDED.provider_filter_pushdown,
                        stored_at_epoch = EXCLUDED.stored_at_epoch,
                        stored_at = EXCLUDED.stored_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        normalized["cache_key"],
                        normalized["source_url"],
                        Json(list(normalized.get("listing_urls") or [])),
                        Json(dict(normalized.get("provider_filter_pushdown") or {})),
                        float(normalized.get("stored_at_epoch") or time.time()),
                    ),
                )
        _property_source_listing_cache_prune_postgres()
        return True
    except Exception:
        return False


def _property_source_listing_cache_persist_snapshot(snapshot: dict[str, dict[str, object]]) -> None:
    global _PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME, _PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH
    path = _property_source_listing_cache_path()
    if path is None:
        return
    now = time.time()
    retention_seconds = max(
        _property_source_listing_cache_ttl_seconds(),
        _property_source_listing_cache_stale_max_seconds(),
    )
    try:
        with _property_source_listing_cache_file_lock(path):
            merged_snapshot = dict(snapshot)
            try:
                existing_payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except Exception:
                _property_source_listing_cache_quarantine_corrupt_file(path, reason="persist_existing_json_invalid")
                existing_payload = {}
            existing_entries = existing_payload.get("entries") if isinstance(existing_payload, dict) else {}
            if isinstance(existing_entries, dict):
                for raw_key, raw_row in existing_entries.items():
                    cache_key = str(raw_key or "").strip()[:240]
                    if not cache_key or not isinstance(raw_row, dict):
                        continue
                    try:
                        stored_at = float(raw_row.get("stored_at_epoch") or 0.0)
                    except Exception:
                        stored_at = 0.0
                    if retention_seconds > 0 and stored_at > 0.0 and now - stored_at > float(retention_seconds):
                        continue
                    urls = [str(value or "").strip() for value in list(raw_row.get("listing_urls") or []) if str(value or "").strip()]
                    if not urls:
                        continue
                    existing_row = dict(merged_snapshot.get(cache_key) or {})
                    try:
                        existing_stored_at = float(existing_row.get("stored_at_epoch") or 0.0)
                    except Exception:
                        existing_stored_at = 0.0
                    if existing_row and existing_stored_at >= stored_at:
                        continue
                    merged_snapshot[cache_key] = {
                        "cache_key": cache_key,
                        "source_url": urllib.parse.urldefrag(str(raw_row.get("source_url") or "").strip())[0],
                        "listing_urls": urls[:250],
                        "stored_at_epoch": stored_at or now,
                        "provider_filter_pushdown": dict(raw_row.get("provider_filter_pushdown") or {})
                        if isinstance(raw_row.get("provider_filter_pushdown"), dict)
                        else {},
                    }
            merged_snapshot = dict(
                sorted(
                    merged_snapshot.items(),
                    key=lambda item: float(item[1].get("stored_at_epoch") or 0.0),
                    reverse=True,
                )[:_PROPERTY_SOURCE_LISTING_CACHE_MAX_ENTRIES]
            )
            payload = {
                "version": _PROPERTY_SOURCE_LISTING_CACHE_VERSION,
                "schema_version": _PROPERTY_SOURCE_LISTING_CACHE_SCHEMA_VERSION,
                "stored_at": _now_iso(),
                "stored_at_epoch": now,
                "entry_count": len(merged_snapshot),
                "max_entries": _PROPERTY_SOURCE_LISTING_CACHE_MAX_ENTRIES,
                "lock_strategy": "fcntl",
                "entries": merged_snapshot,
            }
            temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                temp_path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
                temp_path.replace(path)
                with _PROPERTY_SOURCE_LISTING_CACHE_LOCK:
                    _PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = str(path)
                    try:
                        _PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = float(path.stat().st_mtime)
                    except Exception:
                        _PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = 0.0
            finally:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception:
        return


def _property_source_listing_cache_load() -> None:
    global _PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME, _PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH
    path = _property_source_listing_cache_path()
    path_text = str(path) if path is not None else ""
    try:
        path_mtime = float(path.stat().st_mtime) if path is not None and path.exists() else 0.0
    except Exception:
        path_mtime = 0.0
    with _PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        if (
            _PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH == path_text
            and _PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME == path_mtime
        ):
            return
    if path is None or path_mtime <= 0.0:
        with _PROPERTY_SOURCE_LISTING_CACHE_LOCK:
            _PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = path_text
            _PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = path_mtime
        return
    try:
        with _property_source_listing_cache_file_lock(path):
            parsed = json.loads(path.read_text(encoding="utf-8"))
            try:
                loaded_mtime = float(path.stat().st_mtime)
            except Exception:
                loaded_mtime = path_mtime
    except Exception:
        try:
            with _property_source_listing_cache_file_lock(path):
                _property_source_listing_cache_quarantine_corrupt_file(path, reason="load_json_invalid")
        except Exception:
            pass
        with _PROPERTY_SOURCE_LISTING_CACHE_LOCK:
            _PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = path_text
            _PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = 0.0
        return
    entries = parsed.get("entries") if isinstance(parsed, dict) else {}
    if not isinstance(entries, dict):
        with _PROPERTY_SOURCE_LISTING_CACHE_LOCK:
            _PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = path_text
            _PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = loaded_mtime
        return
    loaded_rows: dict[str, dict[str, object]] = {}
    now = time.time()
    retention_seconds = max(
        _property_source_listing_cache_ttl_seconds(),
        _property_source_listing_cache_stale_max_seconds(),
    )
    for raw_key, raw_row in entries.items():
        cache_key = str(raw_key or "").strip()[:240]
        if not cache_key or not isinstance(raw_row, dict):
            continue
        try:
            stored_at = float(raw_row.get("stored_at_epoch") or 0.0)
        except Exception:
            stored_at = 0.0
        if retention_seconds > 0 and stored_at > 0.0 and now - stored_at > float(retention_seconds):
            continue
        urls = [str(value or "").strip() for value in list(raw_row.get("listing_urls") or []) if str(value or "").strip()]
        if not urls:
            continue
        loaded_rows[cache_key] = {
            "cache_key": cache_key,
            "source_url": urllib.parse.urldefrag(str(raw_row.get("source_url") or "").strip())[0],
            "listing_urls": urls[:250],
            "stored_at_epoch": stored_at or now,
            "provider_filter_pushdown": dict(raw_row.get("provider_filter_pushdown") or {})
            if isinstance(raw_row.get("provider_filter_pushdown"), dict)
            else {},
        }
    if not loaded_rows:
        with _PROPERTY_SOURCE_LISTING_CACHE_LOCK:
            _PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = path_text
            _PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = loaded_mtime
        return
    with _PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        for key, row in loaded_rows.items():
            existing = dict(_PROPERTY_SOURCE_LISTING_CACHE.get(key) or {})
            try:
                existing_stored_at = float(existing.get("stored_at_epoch") or 0.0)
            except Exception:
                existing_stored_at = 0.0
            if existing and existing_stored_at >= float(row.get("stored_at_epoch") or 0.0):
                continue
            _PROPERTY_SOURCE_LISTING_CACHE[key] = row
        _property_source_listing_cache_prune_locked()
        _PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = path_text
        _PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = loaded_mtime


def _property_source_listing_cache_get(cache_key: str, *, allow_stale: bool = False) -> tuple[tuple[str, ...], dict[str, object]]:
    normalized_key = str(cache_key or "").strip()
    if not normalized_key:
        return (), {}
    backend = _property_source_listing_cache_backend()
    if backend == "postgres":
        postgres_row = _property_source_listing_cache_get_postgres(normalized_key)
        if postgres_row:
            with _PROPERTY_SOURCE_LISTING_CACHE_LOCK:
                _PROPERTY_SOURCE_LISTING_CACHE[normalized_key] = postgres_row
            return _property_source_listing_cache_row_state(
                cache_key=normalized_key,
                row=postgres_row,
                allow_stale=allow_stale,
                persistence="postgres",
            )
    if backend == "file":
        _property_source_listing_cache_load()
    with _PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        row = dict(_PROPERTY_SOURCE_LISTING_CACHE.get(normalized_key) or {})
    if not row:
        return (), {}
    return _property_source_listing_cache_row_state(
        cache_key=normalized_key,
        row=row,
        allow_stale=allow_stale,
        persistence=backend if backend in {"file", "memory"} else "memory",
    )


def _property_source_listing_cache_put(
    cache_key: str,
    *,
    source_url: str,
    listing_urls: tuple[str, ...],
    source_spec: dict[str, object] | None = None,
) -> dict[str, object]:
    normalized_key = str(cache_key or "").strip()
    if not normalized_key:
        return {"status": "disabled", "cache_key": "", "listing_total": len(listing_urls)}
    urls = tuple(str(value or "").strip() for value in listing_urls if str(value or "").strip())
    spec = dict(source_spec or {})
    row = {
        "cache_key": normalized_key,
        "source_url": urllib.parse.urldefrag(str(source_url or "").strip())[0],
        "listing_urls": list(urls[:250]),
        "stored_at_epoch": time.time(),
        "provider_filter_pushdown": dict(spec.get("provider_filter_pushdown") or {})
        if isinstance(spec.get("provider_filter_pushdown"), dict)
        else {},
    }
    with _PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        _PROPERTY_SOURCE_LISTING_CACHE[normalized_key] = row
        _property_source_listing_cache_prune_locked()
        snapshot = _property_source_listing_cache_snapshot_locked()
    backend = _property_source_listing_cache_backend()
    persisted_backend = backend
    if backend == "file":
        _property_source_listing_cache_persist_snapshot(snapshot)
    elif backend == "postgres":
        persisted_backend = "postgres" if _property_source_listing_cache_put_postgres(row) else "memory"
    return {
        "status": "stored",
        "cache_key": normalized_key,
        "listing_total": len(urls),
        "persistence": persisted_backend,
        "ttl_seconds": _property_source_listing_cache_ttl_seconds(),
    }
