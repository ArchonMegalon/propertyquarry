from __future__ import annotations

from decimal import Decimal, InvalidOperation
import os
import urllib.parse
from urllib.parse import urlparse
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from app.api.dependencies import RequestContext, get_container, get_request_context
from app.api.routes.product_api_contracts import (
    ChannelDigestDeliveryCreateIn,
    ChannelDigestDeliveryOut,
    ChannelLoopOut,
    GoogleLocationHistoryConnectCallbackOut,
    GoogleLocationHistoryConnectStartOut,
    GoogleLocationHistoryImportIn,
    GoogleLocationHistoryImportOut,
    GoogleLocationHistorySyncOut,
    GooglePhotosPickerSessionIn,
    GooglePhotosPickerSessionOut,
    GooglePhotosSignalSyncIn,
    GooglePhotosSignalSyncOut,
    GoogleSignalSyncOut,
    GoogleSignalSyncStatusOut,
    NoneverbiaSignalImportIn,
    NoneverbiaSignalImportOut,
    OneDriveDocumentQueryTelegramDeliveryOut,
    OfficeEventOut,
    OfficeEventResponse,
    OfficeSignalIn,
    OfficeSignalResultOut,
    PocketSignalCursorResetIn,
    PocketSignalCursorResetOut,
    PocketRecordingDetailOut,
    PocketRecordingAudioEnhanceOut,
    PocketRecordingSearchOut,
    PocketRecordingTelegramDeliveryOut,
    PocketRecordingQueryTelegramDeliveryOut,
    PocketSignalImportIn,
    PocketSignalImportOut,
    PocketSignalSyncOut,
    PropertyBillingCaptureIn,
    PropertyBillingCaptureOut,
    PropertyBillingCheckoutCreateIn,
    PropertyBillingCheckoutOut,
    PropertyFactEnrichmentOut,
    PropertyFactEnrichmentStartIn,
    PropertyScoutSyncOut,
    PropertySearchResearchTaskUpdateIn,
    PropertySearchRunStartIn,
    PropertySearchRunStartOut,
    PropertySearchRunStatusOut,
    SignalIngestEndpointCreateIn,
    SignalIngestEndpointOut,
    WillhabenPropertyTourIn,
    WillhabenPropertyTourOut,
    WebhookDeliveryOut,
    WebhookDeliveryResponse,
    WebhookOut,
    WebhookRegisterIn,
    WebhookResponse,
    WebhookTestResultOut,
    now_iso,
)
from app.api.routes.landing_property_research import _property_candidate_ref
from app.api.routes.landing_view_models import _property_customer_candidate_summary
from app.container import AppContainer
from app.observability import runtime_trace_context_from_mapping
from app.product.property_surface_state import (
    normalize_property_search_run_snapshot,
    property_run_customer_safe_status_detail,
    property_run_customer_visible_events,
    property_run_public_eta_label,
)
from app.product.property_search_storage import _property_search_compact_candidate_preview_url
from app.product.service import (
    _hosted_property_tour_telegram_preview_image_url_for_style,
    _property_visual_ready_tour_url,
    build_product_service,
)
from app.services.property_billing import (
    brilliant_directories_billing_webhook_receipt,
    capture_paypal_property_order,
    create_payfunnels_property_checkout,
    create_paypal_property_order,
    enforce_property_plan_limits,
    merge_property_commercial,
    paid_plan_expiry,
    property_billing_event_updates,
    payfunnels_configured,
    paypal_configured,
    property_plan_spec,
    reconcile_brilliant_directories_billing_event,
    verify_payfunnels_webhook_signature,
)
from app.services.property_market_catalog import (
    country_label as property_country_label,
    default_platforms_for_country_listing_mode as property_default_platforms_for_country_listing_mode,
    evidence_source_options as property_evidence_source_options,
    filter_selectable_property_platform_details as property_filter_selectable_property_platform_details,
    is_customer_search_country_code as property_is_customer_search_country_code,
    normalize_listing_mode as property_normalize_listing_mode,
    normalize_property_search_preferences as property_normalize_search_preferences,
    normalize_property_platform as property_normalize_platform,
    normalize_property_type as property_normalize_property_type,
    normalize_country_code as property_normalize_country_code,
    provider_options as property_provider_options,
    property_provider_for_platform,
    resolve_country_code as property_resolve_country_code,
    selectable_property_platform_keys as property_selectable_platform_keys,
)
from app.services.onboarding import strip_client_property_commercial_authority

router = APIRouter(prefix="/app/api", tags=["product"])
public_payfunnels_router = APIRouter(prefix="/app/api", tags=["product-billing"])


_PAYFUNNELS_TITLE_PRINCIPAL_RE = re.compile(r"pq_principal:([^|]+)")
_PAYFUNNELS_TITLE_ORDER_RE = re.compile(r"pq_order:([^|]+)")
_PAYFUNNELS_COMPLETED_EVENTS = {
    "payment.completed",
    "checkout.completed",
    "subscription.activated",
}
_PAYFUNNELS_COMPLETED_STATUSES = {"paid", "completed", "succeeded", "active"}
_PAYFUNNELS_FAILED_EVENTS = {
    "payment.failed",
    "checkout.failed",
    "payment.declined",
    "subscription.payment_failed",
}
_PAYFUNNELS_CANCELLED_EVENTS = {
    "checkout.cancelled",
    "checkout.canceled",
    "subscription.cancelled",
    "subscription.canceled",
}
_PAYFUNNELS_REFUNDED_EVENTS = {
    "payment.refunded",
    "refund.completed",
    "charge.refunded",
}
_PAYFUNNELS_FAILED_STATUSES = {"failed", "declined", "error"}
_PAYFUNNELS_CANCELLED_STATUSES = {"cancelled", "canceled", "voided"}
_PAYFUNNELS_REFUNDED_STATUSES = {"refunded", "partially_refunded"}
_PROPERTY_SEARCH_TERMINAL_STATUSES = {"processed", "completed", "completed_partial", "failed", "cancelled", "noop"}
_PROPERTY_SEARCH_LIGHTWEIGHT_SOURCE_LIMIT = 24


def _property_search_response_int(value: object) -> int:
    try:
        parsed = int(float(str(value or "").strip()))
    except Exception:
        parsed = 0
    return parsed if parsed > 0 else 0


def _property_search_response_scope(summary: dict[str, object], payload: dict[str, object]) -> tuple[str, str]:
    sources = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    for candidate in (
        summary.get("country_code"),
        payload.get("country_code"),
        dict(payload.get("property_search_preferences") or {}).get("country_code")
        if isinstance(payload.get("property_search_preferences"), dict)
        else "",
        dict(payload.get("preferences") or {}).get("country_code") if isinstance(payload.get("preferences"), dict) else "",
    ):
        country = property_resolve_country_code(candidate)
        if country:
            break
    else:
        country = ""
        for source in sources:
            pushdown = dict(source.get("provider_filter_pushdown") or {}) if isinstance(source.get("provider_filter_pushdown"), dict) else {}
            for section in ("applied", "requested"):
                section_payload = dict(pushdown.get(section) or {}) if isinstance(pushdown.get(section), dict) else {}
                country = property_resolve_country_code(section_payload.get("country_code")) or ""
                if country:
                    break
            if country:
                break
            platform = str(source.get("platform") or source.get("provider_key") or "").strip()
            provider = property_provider_for_platform(platform) if platform else None
            country = str(getattr(provider, "country_code", "") or "").strip().upper()
            if country:
                break
    for candidate in (
        summary.get("listing_mode"),
        payload.get("listing_mode"),
        dict(payload.get("property_search_preferences") or {}).get("listing_mode")
        if isinstance(payload.get("property_search_preferences"), dict)
        else "",
        dict(payload.get("preferences") or {}).get("listing_mode") if isinstance(payload.get("preferences"), dict) else "",
    ):
        mode_text = str(candidate or "").strip()
        if mode_text:
            return country, property_normalize_listing_mode(mode_text)
    for source in sources:
        pushdown = dict(source.get("provider_filter_pushdown") or {}) if isinstance(source.get("provider_filter_pushdown"), dict) else {}
        for section in ("applied", "requested"):
            section_payload = dict(pushdown.get(section) or {}) if isinstance(pushdown.get(section), dict) else {}
            mode_text = str(section_payload.get("listing_mode") or "").strip()
            if mode_text:
                return country, property_normalize_listing_mode(mode_text)
        scope_label = str(source.get("source_scope_label") or source.get("source_label") or "").strip().lower()
        if re.search(r"\b(buy|sale|purchase|kauf)\b", scope_label):
            return country, "buy"
        if re.search(r"\b(rent|miete|miet)\b", scope_label):
            return country, "rent"
    return country, "rent"


def _property_search_apply_response_display_totals(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload or {})
    summary = dict(normalized.get("summary") or {}) if isinstance(normalized.get("summary"), dict) else {}
    if not summary:
        return normalized
    selected_platforms = [
        str(value or "").strip().lower()
        for value in list(normalized.get("selected_platforms") or summary.get("selected_platforms") or [])
        if str(value or "").strip()
    ]
    selected_platform_count = len(dict.fromkeys(selected_platforms))
    explicit_display_total = max(
        _property_search_response_int(normalized.get("provider_display_total")),
        _property_search_response_int(summary.get("provider_display_total")),
    )
    display_total = max(
        explicit_display_total,
        _property_search_response_int(summary.get("provider_total")),
        selected_platform_count,
    )
    if selected_platform_count <= 0 and explicit_display_total <= 0:
        country, listing_mode = _property_search_response_scope(summary, normalized)
        if country:
            try:
                display_total = max(
                    display_total,
                    len(
                        property_selectable_platform_keys(
                            country_code=country,
                            listing_mode=listing_mode,
                            include_distressed_sale_signals=summary.get("include_distressed_sale_signals"),
                        )
                    ),
                )
            except Exception:
                pass
    source_display_total = max(
        _property_search_response_int(normalized.get("source_variant_display_total")),
        _property_search_response_int(summary.get("source_variant_display_total")),
        _property_search_response_int(summary.get("source_variant_total") or summary.get("sources_total")),
        display_total,
    )
    if display_total > 0:
        normalized["provider_display_total"] = display_total
        summary["provider_display_total"] = display_total
    if selected_platform_count > 0:
        normalized["selected_platform_count"] = selected_platform_count
        summary["selected_platform_count"] = selected_platform_count
    if source_display_total > 0:
        normalized["source_variant_display_total"] = source_display_total
        summary["source_variant_display_total"] = source_display_total
    normalized["summary"] = summary
    return normalized


def _payfunnels_title_value(pattern: re.Pattern[str], title: str) -> str:
    match = pattern.search(str(title or ""))
    if match is None:
        return ""
    return str(match.group(1) or "").strip()


def _payfunnels_field_value(payload: dict[str, object], label: str) -> str:
    target = str(label or "").strip().lower()
    if not target:
        return ""
    fields = payload.get("additionalFields")
    if not isinstance(fields, list):
        return ""
    for item in fields:
        if not isinstance(item, dict):
            continue
        item_label = str(item.get("label") or item.get("name") or "").strip().lower()
        if item_label != target:
            continue
        for key in ("hiddenFieldValue", "value", "fieldValue"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
    return ""


def _payfunnels_money_cents(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("EUR", "").replace("€", "").strip().replace(" ", "")
    if "," in normalized and "." in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")
    try:
        amount = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None
    return int((amount * Decimal("100")).quantize(Decimal("1")))


def _payfunnels_plan_amount_matches(*, paid_amount: object, expected_amount: object) -> bool:
    paid_cents = _payfunnels_money_cents(paid_amount)
    expected_cents = _payfunnels_money_cents(expected_amount)
    if paid_cents is None:
        return True
    return expected_cents is not None and paid_cents == expected_cents


def _public_base_url(request: Request) -> str:
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").strip().lower().rstrip(".")
    request_host = str(request.url.hostname or "").strip().lower().rstrip(".")
    effective_host = forwarded_host or request_host
    if effective_host in {"propertyquarry.com", "www.propertyquarry.com"}:
        explicit_property = (
            str(os.environ.get("PROPERTYQUARRY_PUBLIC_BASE_URL") or "").strip().rstrip("/")
            or str(os.environ.get("EA_PROPERTY_PUBLIC_BASE_URL") or "").strip().rstrip("/")
        )
        if explicit_property:
            return explicit_property
        return f"https://{effective_host}"
    explicit = str(os.environ.get("EA_PUBLIC_APP_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    redirect_uri = str(os.environ.get("EA_GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
    if redirect_uri:
        parsed = urlparse(redirect_uri)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return str(request.base_url).rstrip("/")


def _property_preferences(container: AppContainer, *, principal_id: str) -> dict[str, object]:
    state = container.onboarding.status(principal_id=principal_id)
    preferences = dict(state.get("property_search_preferences") or {})
    raw_preferences = dict(preferences.get("raw_preferences") or {})
    merged = raw_preferences or preferences
    if isinstance(preferences.get("property_commercial"), dict) and not isinstance(
        merged.get("property_commercial"),
        dict,
    ):
        merged = dict(merged)
        merged["property_commercial"] = dict(preferences.get("property_commercial") or {})
    return merged


def _save_property_preferences(
    container: AppContainer,
    *,
    principal_id: str,
    property_preferences: dict[str, object],
) -> dict[str, object]:
    return container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json=property_preferences,
        trusted_commercial_update=True,
    )


def _property_search_status_url(run_id: object, *, canonical: bool) -> str:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return ""
    if canonical:
        return f"/app/api/property/search-runs/{normalized_run_id}"
    return f"/app/api/signals/property/search/run/{normalized_run_id}"


def _property_search_source_row_status(row: dict[str, object]) -> str:
    return str(row.get("status") or row.get("state") or "").strip().lower()


def _property_search_compact_source_rows(summary: dict[str, object]) -> dict[str, object]:
    normalized = dict(summary or {})
    source_rows = [dict(row) for row in list(normalized.get("sources") or []) if isinstance(row, dict)]
    if not source_rows:
        return normalized
    existing_counts = normalized.get("source_status_counts")
    existing_status_counts = dict(existing_counts) if isinstance(existing_counts, dict) else {}
    status_counts: dict[str, int] = {}
    for row in source_rows:
        status = _property_search_source_row_status(row) or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
    normalized.setdefault("sources_untrimmed_total", len(source_rows))
    terminal = str(normalized.get("status") or "").strip().lower() in _PROPERTY_SEARCH_TERMINAL_STATUSES
    terminal_status = "completed_partial" if str(normalized.get("status") or "").strip().lower() == "completed_partial" else "completed"
    active_terminal_statuses = {"queued", "pending", "starting", "warming", "running", "processing", "in_progress", "working", "repairing"}
    if terminal:
        existing_has_active = any(str(key).strip().lower() in active_terminal_statuses for key in existing_status_counts)
        existing_total = sum(int(value or 0) for value in existing_status_counts.values() if isinstance(value, int))
        if existing_status_counts and not existing_has_active and existing_total >= len(source_rows):
            normalized["source_status_counts"] = existing_status_counts
        else:
            terminal_counts: dict[str, int] = {}
            for row in source_rows:
                status = _property_search_source_row_status(row) or terminal_status
                if status in active_terminal_statuses:
                    status = terminal_status
                terminal_counts[status] = terminal_counts.get(status, 0) + 1
            try:
                untrimmed_total = int(float(str(normalized.get("sources_untrimmed_total") or "0").strip() or "0"))
            except Exception:
                untrimmed_total = 0
            if untrimmed_total > len(source_rows) and set(terminal_counts) <= {terminal_status}:
                terminal_counts = {terminal_status: untrimmed_total}
            normalized["source_status_counts"] = terminal_counts
    else:
        normalized.setdefault("source_status_counts", status_counts)
    priority_statuses = {"failed", "error", "skipped", "completed", "processed", "done", "success", "repaired"}

    def row_weight(row: dict[str, object]) -> tuple[int, int]:
        row_status = _property_search_source_row_status(row)
        def as_int(value: object) -> int:
            try:
                return max(0, int(float(str(value or "0").strip() or "0")))
            except Exception:
                return 0
        activity = (
            as_int(row.get("ranked_total") or row.get("high_fit_total"))
            + as_int(row.get("listing_total") or row.get("raw_listing_total"))
            + as_int(row.get("reviewed_listing_total") or row.get("scanned_listing_total"))
            + as_int(row.get("preview_prepared_total"))
        )
        priority = 2 if row_status in priority_statuses or row.get("error") else (1 if activity > 0 else 0)
        return priority, activity

    trimmed = sorted(source_rows, key=row_weight, reverse=True)[:_PROPERTY_SEARCH_LIGHTWEIGHT_SOURCE_LIMIT]
    if terminal:
        cleaned_rows: list[dict[str, object]] = []
        for row in trimmed:
            cleaned = dict(row)
            row_status = _property_search_source_row_status(cleaned)
            if row_status in active_terminal_statuses:
                cleaned["status"] = terminal_status
                cleaned["source_status"] = "Partial coverage" if terminal_status == "completed_partial" else "Checked"
            cleaned_rows.append(cleaned)
        trimmed = cleaned_rows
    normalized["sources"] = trimmed
    normalized["sources_trimmed"] = len(source_rows) > len(trimmed)
    return normalized


_PROPERTY_SEARCH_LIGHTWEIGHT_CANDIDATE_FACT_KEYS = (
    "price_display",
    "rent_display",
    "price_per_sqm_display",
    "currency_code",
    "price_eur",
    "rent_eur",
    "total_rent_eur",
    "monthly_rent_eur",
    "purchase_price_eur",
    "area_sqm",
    "living_area_sqm",
    "rooms",
    "floor",
    "has_floorplan",
    "floorplan_count",
    "postal_name",
    "district",
    "city",
    "map_lat",
    "map_lng",
    "lat",
    "lng",
    "latitude",
    "longitude",
    "nearest_school_m",
    "nearest_school_name",
    "nearest_supermarket_m",
    "nearest_supermarket_name",
    "nearest_playground_m",
    "nearest_playground_name",
    "nearest_subway_m",
    "nearest_subway_name",
)


def _property_search_lightweight_image_url(value: object) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if url.lower().startswith("data:"):
        return ""
    return url


def _property_search_lightweight_image_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    raw = dict(value)
    compact: dict[str, object] = {}
    for key in ("image_url", "thumb_image_url", "preview_image_url"):
        cleaned = _property_search_lightweight_image_url(raw.get(key))
        if cleaned:
            compact[key] = cleaned
    return compact


def _property_search_lightweight_tour_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    raw = dict(value)
    return {
        key: raw.get(key)
        for key in (
            "status",
            "url",
            "embed_url",
            "provider_url",
            "provider",
            "label",
            "detail",
            "eta_label",
            "progress_pct",
        )
        if raw.get(key) not in (None, "", [], {})
    }


def _property_search_candidate_diorama_preview_url(candidate: dict[str, object]) -> str:
    raw = dict(candidate or {})
    direct_preview = _property_search_lightweight_image_url(
        raw.get("diorama_preview_url")
        or (
            dict(raw.get("diorama_scene") or {})
            if isinstance(raw.get("diorama_scene"), dict)
            else {}
        ).get("image_url")
        or (
            dict(raw.get("diorama_scene") or {})
            if isinstance(raw.get("diorama_scene"), dict)
            else {}
        ).get("preview_image_url")
    )
    if direct_preview:
        return direct_preview
    style_hint = str(raw.get("diorama_style_hint") or "").strip()
    raw_tour_payload = dict(raw.get("tour") or {}) if isinstance(raw.get("tour"), dict) else {}
    raw_tour_url = str(
        raw.get("generated_reconstruction_url")
        or raw.get("tour_url")
        or raw.get("verified_tour_url")
        or raw_tour_payload.get("tour_url")
        or raw_tour_payload.get("url")
        or ""
    ).strip()
    raw_open_tour_url = str(
        raw.get("open_tour_url")
        or raw.get("verified_tour_url")
        or raw_tour_payload.get("open_tour_url")
        or raw_tour_payload.get("embed_url")
        or ""
    ).strip()
    ready_tour_url = _property_visual_ready_tour_url(
        tour_url=raw_tour_url,
        open_tour_url=raw_open_tour_url,
    )
    if not ready_tour_url:
        return ""
    preview_url = _hosted_property_tour_telegram_preview_image_url_for_style(
        ready_tour_url,
        diorama_style_hint=style_hint,
    )
    if not preview_url:
        try:
            from app.product import property_tour_hosting

            preview_url = property_tour_hosting._hosted_property_tour_preview_image_url(ready_tour_url)  # type: ignore[attr-defined]
        except Exception:
            preview_url = ""
    return _property_search_lightweight_image_url(preview_url)


def _property_search_lightweight_candidate_payload(
    candidate: dict[str, object],
    *,
    run_id: str,
    index: int,
) -> dict[str, object]:
    raw = _property_customer_candidate_summary(dict(candidate or {}))
    candidate_ref = str(raw.get("candidate_ref") or "").strip()
    if not candidate_ref:
        candidate_ref = _property_candidate_ref(
            {
                "title": str(raw.get("title") or "").strip(),
                "property_url": str(raw.get("property_url") or "").strip(),
                "review_url": str(raw.get("review_url") or "").strip(),
                "source_ref": str(raw.get("source_ref") or "").strip(),
                "source_label": str(raw.get("source_label") or raw.get("source_url") or "").strip(),
            }
        )
    if not candidate_ref:
        candidate_ref = f"candidate-{index}"
    packet_url = str(raw.get("packet_url") or "").strip()
    review_url = str(raw.get("review_url") or "").strip()
    if not packet_url and review_url.startswith("/app/research/"):
        packet_url = review_url
    if not packet_url and candidate_ref:
        packet_url = f"/app/research/{urllib.parse.quote(candidate_ref, safe='')}"
        if run_id:
            packet_url = f"{packet_url}?run_id={urllib.parse.quote(run_id, safe='')}"
    compact: dict[str, object] = {
        "candidate_ref": candidate_ref,
        "rank": raw.get("rank") or index,
    }
    if packet_url:
        compact["packet_url"] = packet_url
    for key in (
        "title",
        "source_label",
        "source_platform",
        "source_ref",
        "location_label",
        "price_display",
        "costs_display",
        "rent_display",
        "price_per_sqm_display",
        "layout_display",
        "fit_score",
        "personal_fit_score",
        "fit_label",
        "fit_summary",
        "summary",
        "compare_reason",
        "property_url",
        "source_url",
        "listing_url",
        "map_url",
        "floorplan_url",
        "source_virtual_tour_url",
        "vendor_tour_url",
        "tour_url",
        "tour_status",
        "tour_provider",
        "flythrough_url",
        "flythrough_status",
        "diorama_preview_url",
    ):
        value = raw.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    preview_image_url = _property_search_lightweight_image_url(raw.get("preview_image_url"))
    if not preview_image_url:
        preview_image_url = _property_search_lightweight_image_url(_property_search_compact_candidate_preview_url(raw))
    if preview_image_url:
        compact["preview_image_url"] = preview_image_url
    orientation_preview = _property_search_lightweight_image_payload(raw.get("orientation_preview"))
    if orientation_preview:
        compact["orientation_preview"] = orientation_preview
    diorama_scene = _property_search_lightweight_image_payload(raw.get("diorama_scene"))
    if diorama_scene:
        compact["diorama_scene"] = diorama_scene
    diorama_preview_url = _property_search_candidate_diorama_preview_url(raw)
    if diorama_preview_url:
        compact["diorama_preview_url"] = diorama_preview_url
    tour_payload = _property_search_lightweight_tour_payload(raw.get("tour"))
    if tour_payload:
        compact["tour"] = tour_payload
    flythrough_payload = _property_search_lightweight_tour_payload(raw.get("flythrough"))
    if flythrough_payload:
        compact["flythrough"] = flythrough_payload
    facts = dict(raw.get("property_facts") or {}) if isinstance(raw.get("property_facts"), dict) else {}
    compact_facts = {
        key: facts.get(key)
        for key in _PROPERTY_SEARCH_LIGHTWEIGHT_CANDIDATE_FACT_KEYS
        if facts.get(key) not in (None, "", [], {})
    }
    if compact_facts:
        compact["property_facts"] = compact_facts
    match_reasons = [str(item).strip() for item in list(raw.get("match_reasons") or []) if str(item).strip()]
    if match_reasons:
        compact["match_reasons"] = match_reasons[:3]
    return compact


def _property_search_payload_with_status_url(payload: dict[str, object], *, canonical: bool) -> dict[str, object]:
    copied = dict(payload or {})
    summary = dict(copied.get("summary") or {}) if isinstance(copied.get("summary"), dict) else {}
    fallback_timestamp = str(
        copied.get("updated_at")
        or summary.get("updated_at")
        or copied.get("generated_at")
        or copied.get("created_at")
        or now_iso()
    ).strip()
    if not str(copied.get("generated_at") or "").strip():
        copied["generated_at"] = fallback_timestamp
    if not str(copied.get("created_at") or "").strip():
        copied["created_at"] = str(copied.get("generated_at") or fallback_timestamp).strip()
    if not str(copied.get("updated_at") or "").strip():
        copied["updated_at"] = fallback_timestamp
    copied.setdefault("status", "queued")
    copied.setdefault("progress", 0)
    copied.setdefault("message", "")
    status = str(copied.get("status") or summary.get("status") or "").strip().lower()
    if status in _PROPERTY_SEARCH_TERMINAL_STATUSES:
        copied["progress"] = 100
        summary["progress"] = 100
        summary["progress_percent"] = 100
        copied.pop("eta_label", None)
        copied.pop("eta_seconds", None)
        summary.pop("eta_label", None)
        summary.pop("eta_seconds", None)
        summary.pop("next_useful_update_eta_label", None)
    else:
        sanitized_eta = property_run_public_eta_label(copied.get("eta_label") or summary.get("eta_label"))
        if sanitized_eta:
            copied["eta_label"] = sanitized_eta
            summary["eta_label"] = sanitized_eta
        else:
            copied.pop("eta_label", None)
            summary.pop("eta_label", None)
        sanitized_next_eta = property_run_public_eta_label(summary.get("next_useful_update_eta_label"))
        if sanitized_next_eta:
            summary["next_useful_update_eta_label"] = sanitized_next_eta
        else:
            summary.pop("next_useful_update_eta_label", None)
    copied["summary"] = summary
    copied.setdefault("events", list(copied.get("events") or []))
    run_id = str(copied.get("run_id") or "").strip()
    if not run_id:
        return copied
    if canonical or not str(copied.get("status_url") or "").strip():
        copied["status_url"] = _property_search_status_url(run_id, canonical=canonical)
    return copied


def _sanitize_property_search_run_platforms(
    *,
    property_preferences: dict[str, object],
    selected_platforms: list[str] | tuple[str, ...],
) -> tuple[dict[str, object], tuple[str, ...]]:
    raw_preferences = dict(property_preferences or {})
    normalized_preferences = property_normalize_search_preferences(dict(property_preferences or {}))
    raw_country_code = str(raw_preferences.get("country_code") or "").strip()
    resolved_raw_country_code = property_resolve_country_code(raw_country_code) if raw_country_code else ""
    if raw_country_code and not resolved_raw_country_code:
        raise ValueError("unsupported_property_market")
    country_code = resolved_raw_country_code or property_normalize_country_code(normalized_preferences.get("country_code"))
    country_code = country_code or "AT"
    if not property_is_customer_search_country_code(country_code):
        raise ValueError("unsupported_property_market")
    listing_mode = property_normalize_listing_mode(normalized_preferences.get("listing_mode"))
    requested = tuple(
        dict.fromkeys(
            property_normalize_platform(item)
            for item in (selected_platforms or normalized_preferences.get("selected_platforms") or [])
            if property_normalize_platform(item) and property_normalize_platform(item) != "all"
        )
    )
    kept, removed_details = property_filter_selectable_property_platform_details(
        requested,
        country_code=country_code,
        listing_mode=listing_mode,
        include_distressed_sale_signals=normalized_preferences.get("include_distressed_sale_signals"),
    )
    if selected_platforms and any(str(row.get("reason") or "").strip() == "unknown_provider" for row in removed_details):
        raise ValueError("unsupported_property_provider")
    for numeric_key in (
        "min_price_eur",
        "max_price_eur",
        "min_rooms",
        "min_area_m2",
        "available_within_years",
        "max_commute_minutes_transit",
        "max_commute_minutes_drive",
        "max_commute_minutes_bike",
        "max_commute_minutes_walk",
    ):
        if numeric_key not in raw_preferences:
            continue
        try:
            explicit_value = float(str(raw_preferences.get(numeric_key) or "0").strip())
        except Exception:
            continue
        if explicit_value <= 0:
            normalized_preferences[numeric_key] = 0
    normalized_preferences["country_code"] = country_code
    normalized_preferences["listing_mode"] = listing_mode
    normalized_preferences["selected_platforms"] = list(kept)
    if removed_details:
        normalized_preferences["provider_country_filter_applied"] = True
        normalized_preferences["provider_country_filter_removed"] = [
            str(row.get("platform") or "").strip()
            for row in removed_details
            if str(row.get("platform") or "").strip()
        ]
        normalized_preferences["provider_country_filter_removed_details"] = [dict(row) for row in removed_details]
    return normalized_preferences, kept


def _start_property_search_run_payload(
    *,
    body: PropertySearchRunStartIn,
    request: Request,
    container: AppContainer,
    context: RequestContext,
) -> dict[str, object]:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "property_search").strip()
    merged_preferences = _property_preferences(container, principal_id=context.principal_id)
    merged_preferences.update(
        strip_client_property_commercial_authority(dict(body.property_preferences))
    )
    merged_preferences.pop("max_results_per_source", None)
    merged_preferences, sanitized_platforms = _sanitize_property_search_run_platforms(
        property_preferences=merged_preferences,
        selected_platforms=tuple(body.selected_platforms),
    )
    enforce_property_plan_limits(
        property_preferences=merged_preferences,
        selected_platforms=sanitized_platforms,
        max_results_per_source=None,
    )
    start_kwargs: dict[str, object] = {
        "principal_id": context.principal_id,
        "actor": actor,
        # Preserve the explicit request for the service trust boundary. The
        # route has already rejected unknown providers and uses the sanitized
        # set for entitlement checks, while the service must see cross-country
        # removals itself so its durable run receipt can record them honestly.
        "selected_platforms": tuple(body.selected_platforms) or sanitized_platforms,
        "property_search_preferences": merged_preferences,
        "force_refresh": bool(body.force_refresh),
        "max_results_per_source": None,
        "dispatch_only": bool(body.dispatch_only),
        "dispatch_probe_ack_only": (
            bool(body.dispatch_only)
            and str(request.headers.get("X-PropertyQuarry-Dispatch-Probe") or "").strip().lower()
            in {"1", "true", "yes", "on"}
        ),
    }
    requested_idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    if requested_idempotency_key:
        start_kwargs["idempotency_key"] = requested_idempotency_key[:240]
    request_trace = runtime_trace_context_from_mapping(
        {
            "traceparent": str(getattr(request.state, "traceparent", "") or ""),
            "trace_id": str(getattr(request.state, "trace_id", "") or ""),
            "span_id": str(getattr(request.state, "span_id", "") or ""),
            "parent_span_id": str(getattr(request.state, "parent_span_id", "") or ""),
        }
    )
    if request_trace is not None:
        start_kwargs["trace_context"] = {
            **request_trace.as_mapping(),
            "correlation_id": str(getattr(request.state, "correlation_id", "") or ""),
        }
    return service.start_property_search_run(
        **start_kwargs,
    )


def _property_search_start_runtime_http_status(exc: RuntimeError) -> int:
    detail = str(exc or "").strip()
    if detail in {
        "property_search_work_database_url_required",
        "property_search_work_enqueue_failed",
        "property_search_work_idempotency_state_missing",
        "property_search_run_persistence_failed",
    }:
        return 503
    return 409


def _property_search_run_status_payload(
    *,
    run_id: str,
    container: AppContainer,
    context: RequestContext,
    lightweight: bool = False,
) -> dict[str, object]:
    service = build_product_service(container)
    try:
        payload = service.get_property_search_run_status(
            principal_id=context.principal_id,
            run_id=run_id,
            lightweight=lightweight,
            account_email=str(context.access_email or "").strip(),
        )
    except TypeError:
        try:
            payload = service.get_property_search_run_status(
                principal_id=context.principal_id,
                run_id=run_id,
                lightweight=lightweight,
            )
        except TypeError:
            payload = service.get_property_search_run_status(
                principal_id=context.principal_id,
                run_id=run_id,
            )
    if not payload:
        raise HTTPException(status_code=404, detail="property_search_run_not_found")
    normalized = dict(payload)
    summary = dict(normalized.get("summary") or {}) if isinstance(normalized.get("summary"), dict) else {}
    fallback_timestamp = str(
        normalized.get("updated_at")
        or summary.get("updated_at")
        or normalized.get("generated_at")
        or normalized.get("created_at")
        or ""
    ).strip()
    if fallback_timestamp and not str(normalized.get("updated_at") or "").strip():
        normalized["updated_at"] = fallback_timestamp
    if fallback_timestamp and not str(normalized.get("created_at") or "").strip():
        normalized["created_at"] = str(normalized.get("generated_at") or fallback_timestamp).strip()
    if not str(normalized.get("generated_at") or "").strip():
        normalized["generated_at"] = str(normalized.get("updated_at") or normalized.get("created_at") or "")
    status_value = str(normalized.get("status") or summary.get("status") or "").strip().lower()
    if status_value in _PROPERTY_SEARCH_TERMINAL_STATUSES:
        normalized["progress"] = 100
        normalized.pop("eta_label", None)
        normalized.pop("eta_seconds", None)
        summary["status"] = status_value
        summary["progress"] = 100
        summary["progress_percent"] = 100
        summary.pop("eta_label", None)
        summary.pop("eta_seconds", None)
        summary.pop("next_useful_update_eta_label", None)
    normalized["summary"] = summary
    normalized = normalize_property_search_run_snapshot(normalized)
    summary = dict(normalized.get("summary") or {}) if isinstance(normalized.get("summary"), dict) else {}
    raw_progress_message = str(normalized.get("message") or "").strip()
    normalized["message"] = property_run_customer_safe_status_detail(
        normalized.get("status") or summary.get("status"),
        raw_progress_message,
        summary=summary,
        prefer_repair_step=True,
    )
    if lightweight:
        summary = _property_search_compact_source_rows(summary)
    normalized["summary"] = summary
    event_payload = dict(normalized)
    if raw_progress_message:
        event_payload["message"] = raw_progress_message
    normalized["events"] = property_run_customer_visible_events(run_payload=event_payload)
    if summary:
        score_demoted_total = int(summary.get("score_demoted_total") or summary.get("filtered_low_fit_total") or 0)
        if score_demoted_total > 0:
            summary["score_demoted_total"] = score_demoted_total
        ranked_candidates = [dict(row) for row in list(summary.get("ranked_candidates") or []) if isinstance(row, dict)]
        if not ranked_candidates:
            synthesized: list[dict[str, object]] = []
            for source in [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]:
                for candidate in [dict(row) for row in list(source.get("top_candidates") or []) if isinstance(row, dict)]:
                    candidate.setdefault("source_label", str(source.get("source_label") or source.get("label") or "").strip())
                    synthesized.append(candidate)
            synthesized.sort(key=lambda item: float(item.get("fit_score") or 0.0), reverse=True)
            for index, candidate in enumerate(synthesized, start=1):
                candidate.setdefault("rank", index)
            if synthesized:
                summary["ranked_candidates"] = synthesized
        ranked_candidates = [dict(row) for row in list(summary.get("ranked_candidates") or []) if isinstance(row, dict)]
        for index, candidate in enumerate(ranked_candidates, start=1):
            candidate_ref = str(candidate.get("candidate_ref") or "").strip()
            if not candidate_ref:
                candidate_ref = _property_candidate_ref(
                    {
                        "title": str(candidate.get("title") or "").strip(),
                        "property_url": str(candidate.get("property_url") or "").strip(),
                        "review_url": str(candidate.get("review_url") or "").strip(),
                        "source_ref": str(candidate.get("source_ref") or "").strip(),
                        "source_label": str(candidate.get("source_label") or candidate.get("source_url") or "").strip(),
                    }
                )
            if not candidate_ref:
                candidate_ref = f"candidate-{index}"
            candidate["candidate_ref"] = candidate_ref
            candidate.setdefault("rank", index)
            packet_url = str(candidate.get("packet_url") or "").strip()
            review_url = str(candidate.get("review_url") or "").strip()
            if not packet_url and review_url.startswith("/app/research/"):
                packet_url = review_url
            if not packet_url and candidate_ref:
                packet_url = f"/app/research/{urllib.parse.quote(candidate_ref, safe='')}"
                if run_id:
                    packet_url = f"{packet_url}?run_id={urllib.parse.quote(run_id, safe='')}"
            if packet_url:
                candidate["packet_url"] = packet_url
        if ranked_candidates:
            if lightweight:
                ranked_candidates = [
                    _property_search_lightweight_candidate_payload(candidate, run_id=run_id, index=index)
                    for index, candidate in enumerate(ranked_candidates, start=1)
                ]
            summary["ranked_candidates"] = ranked_candidates
        held_back_total = int(
            summary.get("held_back_total")
            or summary.get("filtered_total")
            or (
                int(summary.get("filtered_area_total") or 0)
                + int(summary.get("filtered_property_type_total") or 0)
                + int(summary.get("filtered_floorplan_total") or 0)
                + int(summary.get("filtered_availability_total") or 0)
                + int(summary.get("filtered_generic_page_total") or 0)
                + int(summary.get("filtered_listing_mode_total") or 0)
            )
            or 0
        )
        if held_back_total > 0:
            summary.setdefault("held_back_total", held_back_total)
            summary.setdefault("filtered_total", held_back_total)
        if lightweight:
            summary = _property_search_compact_source_rows(summary)
        normalized["summary"] = summary
    return _property_search_apply_response_display_totals(normalized)


def _update_property_search_research_task_payload(
    *,
    run_id: str,
    task_id: str,
    body: PropertySearchResearchTaskUpdateIn,
    container: AppContainer,
    context: RequestContext,
) -> dict[str, object]:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "property_research").strip()
    try:
        payload = service.update_property_search_research_task(
            principal_id=context.principal_id,
            run_id=run_id,
            task_id=task_id,
            action=body.action,
            value=body.value,
            note=body.note,
            actor=actor,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not payload:
        raise HTTPException(status_code=404, detail="property_search_run_not_found")
    return dict(payload)


def _property_billing_return_path(plan_key: str) -> str:
    return f"/app/api/signals/property/billing/paypal/return?plan_key={plan_key}"


def _property_billing_cancel_path(plan_key: str) -> str:
    return f"/app/api/signals/property/billing/paypal/cancel?plan_key={plan_key}"


@router.get("/events", response_model=OfficeEventResponse)
def get_office_events(
    limit: int = Query(default=50, ge=1, le=200),
    event_type: str = Query(default=""),
    channel: str = Query(default=""),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> OfficeEventResponse:
    service = build_product_service(container)
    items = service.list_office_events(
        principal_id=context.principal_id,
        limit=limit,
        event_type=event_type,
        channel=channel,
    )
    return OfficeEventResponse(generated_at=now_iso(), items=[OfficeEventOut(**item) for item in items], total=len(items))


@router.post("/signals/ingest", response_model=OfficeSignalResultOut)
def ingest_office_signal(
    body: OfficeSignalIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> OfficeSignalResultOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    payload = service.ingest_office_signal(
        principal_id=context.principal_id,
        signal_type=body.signal_type,
        channel=body.channel,
        title=body.title,
        summary=body.summary,
        text=body.text,
        source_ref=body.source_ref,
        external_id=body.external_id,
        counterparty=body.counterparty,
        stakeholder_id=body.stakeholder_id,
        due_at=body.due_at,
        payload=body.payload,
        actor=actor,
    )
    return OfficeSignalResultOut(**payload)


@router.post("/signals/willhaben/property-tour", response_model=WillhabenPropertyTourOut)
def create_willhaben_property_tour(
    body: WillhabenPropertyTourIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WillhabenPropertyTourOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.request_property_visual_asset(
            principal_id=context.principal_id,
            account_email=str(context.access_email or "").strip(),
            property_url=body.property_url,
            request_kind=body.request_kind,
            recipient_email=body.recipient_email,
            variant_key=body.variant_key,
            binding_id=body.binding_id,
            source_ref=body.source_ref,
            external_id=body.external_id,
            run_id=body.run_id,
            candidate_ref=body.candidate_ref,
            auto_deliver=body.auto_deliver,
            allow_floorplan_only=body.allow_floorplan_only,
            diorama_style_hint=body.diorama_style_hint,
            actor=actor,
            walkthrough_provider_key=body.walkthrough_provider_key,
            external_processing_consent_granted=body.external_processing_consent_granted,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WillhabenPropertyTourOut(**payload)


@router.get("/signals/property/visual-status", response_model=WillhabenPropertyTourOut)
def get_property_visual_status(
    run_id: str = Query(min_length=1),
    request_kind: str = Query(default="tour"),
    candidate_ref: str = Query(default=""),
    source_ref: str = Query(default=""),
    property_url: str = Query(default=""),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WillhabenPropertyTourOut:
    service = build_product_service(container)
    try:
        payload = service.get_property_visual_request_status(
            principal_id=context.principal_id,
            account_email=str(context.access_email or "").strip(),
            run_id=run_id,
            request_kind=request_kind,
            candidate_ref=candidate_ref,
            source_ref=source_ref,
            property_url=property_url,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail in {"property_visual_status_run_missing", "property_visual_status_candidate_missing"}:
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    return WillhabenPropertyTourOut(**payload)


@router.post("/signals/pocket/upload-url", response_model=SignalIngestEndpointOut)
def create_pocket_signal_upload_url(
    request: Request,
    body: SignalIngestEndpointCreateIn | None = None,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> SignalIngestEndpointOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    spec = body or SignalIngestEndpointCreateIn()
    payload = service.issue_signal_ingest_endpoint(
        principal_id=context.principal_id,
        channel="pocket",
        signal_type=spec.signal_type,
        label=spec.label,
        counterparty=spec.counterparty,
        base_url=_public_base_url(request),
        actor=actor,
    )
    return SignalIngestEndpointOut(**payload)


@router.post("/signals/pocket/import-local", response_model=PocketSignalImportOut)
def import_pocket_saved_links_from_local_path(
    body: PocketSignalImportIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PocketSignalImportOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.import_pocket_saved_links_from_local_path(
            principal_id=context.principal_id,
            path=body.path,
            counterparty=body.counterparty,
            actor=actor,
        )
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 404 if detail == "pocket_import_path_not_found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return PocketSignalImportOut(**payload)


@router.post("/signals/noneverbia/import-local", response_model=NoneverbiaSignalImportOut)
def import_noneverbia_meetings_from_local_path(
    body: NoneverbiaSignalImportIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> NoneverbiaSignalImportOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.import_noneverbia_meetings_from_local_path(
            principal_id=context.principal_id,
            path=body.path,
            counterparty=body.counterparty,
            actor=actor,
        )
    except RuntimeError as exc:
        detail = str(exc)
        if detail == "noneverbia_import_path_not_found":
            status_code = 404
        elif detail == "noneverbia_import_path_not_allowed":
            status_code = 403
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return NoneverbiaSignalImportOut(**payload)


@router.post("/signals/google/location-history/import", response_model=GoogleLocationHistoryImportOut)
def import_google_location_history(
    body: GoogleLocationHistoryImportIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> GoogleLocationHistoryImportOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.import_google_location_history(
            principal_id=context.principal_id,
            actor=actor,
            path=body.path,
        )
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 404 if detail == "google_location_history_import_path_not_found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return GoogleLocationHistoryImportOut(**payload)


@router.post("/signals/google/location-history/connect-start", response_model=GoogleLocationHistoryConnectStartOut)
def start_google_location_history_connect(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> GoogleLocationHistoryConnectStartOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    payload = service.start_google_location_history_connect(
        principal_id=context.principal_id,
        actor=actor,
        redirect_uri_override=f"{_public_base_url(request)}/google/callback",
    )
    return GoogleLocationHistoryConnectStartOut(**payload)


@router.get("/signals/google/location-history/callback", response_model=GoogleLocationHistoryConnectCallbackOut)
def complete_google_location_history_connect(
    code: str = Query(..., min_length=1),
    state: str = Query(..., min_length=1),
    container: AppContainer = Depends(get_container),
) -> GoogleLocationHistoryConnectCallbackOut:
    service = build_product_service(container)
    try:
        payload = service.complete_google_location_history_connect(
            code=code,
            state=state,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return GoogleLocationHistoryConnectCallbackOut(**payload)


@router.post("/signals/google/location-history/sync", response_model=GoogleLocationHistorySyncOut)
def sync_google_location_history_portability(
    force: bool = Query(default=False),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> GoogleLocationHistorySyncOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.sync_google_location_history_portability(
            principal_id=context.principal_id,
            actor=actor,
            force=force,
        )
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 409 if detail == "google_location_history_binding_not_found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return GoogleLocationHistorySyncOut(**payload)


@router.post("/signals/pocket/sync", response_model=PocketSignalSyncOut)
def sync_pocket_recordings(
    limit: int = Query(default=5, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PocketSignalSyncOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.sync_pocket_recordings(
            principal_id=context.principal_id,
            actor=actor,
            limit=limit,
        )
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 429 if detail.startswith("pocket_api_http_429:") else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return PocketSignalSyncOut(**payload)


@router.post("/signals/pocket/backfill", response_model=PocketSignalSyncOut)
def backfill_pocket_recordings(
    limit: int = Query(default=0, ge=0, le=250),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PocketSignalSyncOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.backfill_pocket_recordings(
            principal_id=context.principal_id,
            actor=actor,
            limit=limit,
        )
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 429 if detail.startswith("pocket_api_http_429:") else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return PocketSignalSyncOut(**payload)


@router.post("/signals/pocket/reset-cursor", response_model=PocketSignalCursorResetOut)
def reset_pocket_recording_sync_cursor(
    body: PocketSignalCursorResetIn | None = None,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PocketSignalCursorResetOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    payload = service.reset_pocket_recording_sync_cursor(
        principal_id=context.principal_id,
        actor=actor,
        reason=str((body.reason if body is not None else "") or "").strip(),
    )
    return PocketSignalCursorResetOut(**payload)


@router.get("/signals/pocket/recordings/search", response_model=PocketRecordingSearchOut)
def search_pocket_recordings(
    q: str = Query(default=""),
    before: str = Query(default=""),
    after: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PocketRecordingSearchOut:
    service = build_product_service(container)
    payload = service.search_pocket_recordings(
        principal_id=context.principal_id,
        actor=str(context.operator_id or context.access_email or context.principal_id or "office_api").strip(),
        query=q,
        before=before,
        after=after,
        limit=limit,
    )
    return PocketRecordingSearchOut(**payload)


@router.get("/signals/pocket/recordings/{recording_id}", response_model=PocketRecordingDetailOut)
def get_pocket_recording_detail(
    recording_id: str,
    prefer_audio_fallback: bool = Query(default=False),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PocketRecordingDetailOut:
    service = build_product_service(container)
    try:
        payload = service.get_pocket_recording_detail(
            recording_id=recording_id,
            prefer_audio_fallback=prefer_audio_fallback,
            principal_id=context.principal_id,
            actor=str(context.operator_id or context.access_email or context.principal_id or "office_api").strip(),
        )
    except RuntimeError as exc:
        detail = str(exc)
        if detail == "pocket_recording_not_found":
            status_code = 404
        elif detail.startswith("pocket_api_http_429:"):
            status_code = 429
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return PocketRecordingDetailOut(**payload)


@router.post("/signals/pocket/recordings/{recording_id}/retranscribe", response_model=PocketRecordingDetailOut)
def retranscribe_pocket_recording(
    recording_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PocketRecordingDetailOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.retranscribe_pocket_recording(
            principal_id=context.principal_id,
            actor=actor,
            recording_id=recording_id,
        )
    except RuntimeError as exc:
        detail = str(exc)
        if detail == "pocket_recording_not_found":
            status_code = 404
        elif detail.startswith("pocket_api_http_429:"):
            status_code = 429
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return PocketRecordingDetailOut(**payload)


@router.post("/signals/pocket/recordings/{recording_id}/deliver-telegram", response_model=PocketRecordingTelegramDeliveryOut)
def deliver_pocket_recording_to_telegram(
    recording_id: str,
    enhanced: bool = Query(default=False),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PocketRecordingTelegramDeliveryOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.deliver_pocket_recording_to_telegram(
            principal_id=context.principal_id,
            actor=actor,
            recording_id=recording_id,
            prefer_enhanced=enhanced,
        )
    except RuntimeError as exc:
        detail = str(exc)
        if detail == "pocket_recording_not_found":
            status_code = 404
        elif detail.startswith("pocket_api_http_429:"):
            status_code = 429
        elif detail in {"telegram_binding_not_found", "pocket_recording_audio_unavailable"}:
            status_code = 409
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return PocketRecordingTelegramDeliveryOut(**payload)


@router.post("/signals/pocket/recordings/{recording_id}/enhance-audio", response_model=PocketRecordingAudioEnhanceOut)
def enhance_pocket_recording_audio(
    recording_id: str,
    force: bool = Query(default=False),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PocketRecordingAudioEnhanceOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.enhance_pocket_recording_audio(
            principal_id=context.principal_id,
            actor=actor,
            recording_id=recording_id,
            force=force,
        )
    except RuntimeError as exc:
        detail = str(exc)
        if detail == "pocket_recording_not_found":
            status_code = 404
        elif detail.startswith("pocket_api_http_429:"):
            status_code = 429
        elif detail in {"pocket_recording_audio_unavailable", "pocket_recording_audio_archive_missing", "ffmpeg_unavailable"}:
            status_code = 409
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return PocketRecordingAudioEnhanceOut(**payload)


@router.post("/signals/pocket/recordings/deliver-telegram", response_model=PocketRecordingQueryTelegramDeliveryOut)
def deliver_pocket_recording_search_to_telegram(
    q: str = Query(default=""),
    before: str = Query(default=""),
    after: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PocketRecordingQueryTelegramDeliveryOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.deliver_pocket_recording_search_to_telegram(
            principal_id=context.principal_id,
            actor=actor,
            query=q,
            before=before,
            after=after,
            limit=limit,
        )
    except RuntimeError as exc:
        detail = str(exc)
        if detail == "pocket_recording_search_match_not_found":
            status_code = 404
        elif detail.startswith("pocket_api_http_429:"):
            status_code = 429
        elif detail in {"telegram_binding_not_found", "pocket_recording_audio_unavailable"}:
            status_code = 409
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return PocketRecordingQueryTelegramDeliveryOut(**payload)


@router.post("/signals/onedrive/documents/deliver-telegram", response_model=OneDriveDocumentQueryTelegramDeliveryOut)
def deliver_onedrive_document_search_to_telegram(
    q: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> OneDriveDocumentQueryTelegramDeliveryOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "office_api").strip()
    try:
        payload = service.deliver_onedrive_document_search_to_telegram(
            principal_id=context.principal_id,
            actor=actor,
            query=q,
            limit=limit,
        )
    except RuntimeError as exc:
        detail = str(exc)
        if detail == "onedrive_document_search_match_not_found":
            status_code = 404
        elif detail == "telegram_binding_not_found":
            status_code = 409
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return OneDriveDocumentQueryTelegramDeliveryOut(**payload)


@router.post("/signals/google/sync", response_model=GoogleSignalSyncOut)
def sync_google_workspace_signals(
    email_limit: int = Query(default=5, ge=0, le=25),
    calendar_limit: int = Query(default=5, ge=0, le=25),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> GoogleSignalSyncOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "google_sync").strip()
    try:
        payload = service.sync_google_workspace_signals(
            principal_id=context.principal_id,
            actor=actor,
            email_limit=email_limit,
            calendar_limit=calendar_limit,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return GoogleSignalSyncOut(**payload)


@router.post("/signals/google/willhaben-sync", response_model=GoogleSignalSyncOut)
@router.post("/signals/google/property-sync", response_model=GoogleSignalSyncOut)
def sync_google_willhaben_signals(
    email_limit: int = Query(default=10, ge=0, le=50),
    account_email: str = Query(default=""),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> GoogleSignalSyncOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "google_sync").strip()
    try:
        payload = service.sync_google_willhaben_signals(
            principal_id=context.principal_id,
            actor=actor,
            account_email=account_email,
            email_limit=email_limit,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return GoogleSignalSyncOut(**payload)


@router.post("/signals/property/scout", response_model=PropertyScoutSyncOut)
def sync_direct_property_scout(
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyScoutSyncOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "property_scout").strip()
    try:
        payload = service.sync_direct_property_scout(
            principal_id=context.principal_id,
            actor=actor,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PropertyScoutSyncOut(**payload)


@router.post("/signals/property/search/run", response_model=PropertySearchRunStartOut, status_code=202)
def start_property_search_run(
    body: PropertySearchRunStartIn,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertySearchRunStartOut:
    try:
        payload = _start_property_search_run_payload(
            body=body,
            request=request,
            container=container,
            context=context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=_property_search_start_runtime_http_status(exc), detail=str(exc)) from exc
    return PropertySearchRunStartOut(**_property_search_payload_with_status_url(dict(payload), canonical=False))


@router.post("/property/search-runs", response_model=PropertySearchRunStartOut, status_code=202)
def start_property_search_run_v2(
    body: PropertySearchRunStartIn,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertySearchRunStartOut:
    try:
        payload = _start_property_search_run_payload(
            body=body,
            request=request,
            container=container,
            context=context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=_property_search_start_runtime_http_status(exc), detail=str(exc)) from exc
    return PropertySearchRunStartOut(**_property_search_payload_with_status_url(dict(payload), canonical=True))


@router.get("/property/providers", response_model=dict[str, object])
def get_property_providers(
    country: str = Query(default="AT", min_length=1, max_length=12),
    listing_mode: str = Query(default="rent", min_length=1, max_length=24),
    property_type: str = Query(default="any", min_length=1, max_length=24),
) -> dict[str, object]:
    country_code = property_normalize_country_code(country)
    if not property_is_customer_search_country_code(country_code):
        raise HTTPException(status_code=400, detail="unsupported_property_market")
    normalized_listing_mode = property_normalize_listing_mode(listing_mode)
    normalized_property_type = property_normalize_property_type(property_type)
    return {
        "generated_at": now_iso(),
        "country_code": country_code,
        "country_label": property_country_label(country_code),
        "listing_mode": normalized_listing_mode,
        "property_type": normalized_property_type,
        "default_platforms": list(
            property_default_platforms_for_country_listing_mode(
                country_code,
                normalized_listing_mode,
                property_type=normalized_property_type,
            )
        ),
        "providers": [dict(row) for row in property_provider_options(country_code=country_code)],
        "evidence_sources": [dict(row) for row in property_evidence_source_options(country_code=country_code)],
    }


@router.post("/signals/property/billing/paypal/order", response_model=PropertyBillingCheckoutOut)
def create_property_billing_order(
    body: PropertyBillingCheckoutCreateIn,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyBillingCheckoutOut:
    if not paypal_configured():
        raise HTTPException(status_code=409, detail="paypal_not_configured")
    try:
        spec = property_plan_spec(body.plan_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if spec.plan_key == "free":
        raise HTTPException(status_code=400, detail="property_plan_free_does_not_require_checkout")
    base_url = _public_base_url(request)
    try:
        order = create_paypal_property_order(
            principal_id=context.principal_id,
            plan_key=spec.plan_key,
            return_url=f"{base_url}{_property_billing_return_path(spec.plan_key)}",
            cancel_url=f"{base_url}{_property_billing_cancel_path(spec.plan_key)}",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    updated = merge_property_commercial(
        _property_preferences(container, principal_id=context.principal_id),
        updates={
            "pending_order_id": str(order.get("order_id") or ""),
            "pending_plan_key": spec.plan_key,
            "pending_approval_url": str(order.get("approve_url") or ""),
            "last_payment_status": str(order.get("status") or ""),
            "plan_source": "paypal",
        },
    )
    _save_property_preferences(container, principal_id=context.principal_id, property_preferences=updated)
    return PropertyBillingCheckoutOut(
        generated_at=now_iso(),
        plan_key=spec.plan_key,
        order_id=str(order.get("order_id") or ""),
        approve_url=str(order.get("approve_url") or ""),
        status=str(order.get("status") or ""),
        amount_eur=str(order.get("amount_eur") or spec.amount_eur),
    )


def _create_property_billing_order_payfunnels(
    body: PropertyBillingCheckoutCreateIn,
    request: Request,
    container: AppContainer,
    context: RequestContext,
) -> PropertyBillingCheckoutOut:
    try:
        spec = property_plan_spec(body.plan_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if spec.plan_key == "free":
        raise HTTPException(status_code=400, detail="property_plan_free_does_not_require_checkout")
    if not payfunnels_configured(plan_key=spec.plan_key):
        raise HTTPException(status_code=409, detail="payfunnels_not_configured")
    base_url = _public_base_url(request)
    try:
        checkout = create_payfunnels_property_checkout(
            principal_id=context.principal_id,
            plan_key=spec.plan_key,
            return_url=f"{base_url}/app/api/signals/property/billing/payfunnels/return?plan_key={spec.plan_key}",
            cancel_url=f"{base_url}/app/api/signals/property/billing/payfunnels/cancel?plan_key={spec.plan_key}",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    updated = merge_property_commercial(
        _property_preferences(container, principal_id=context.principal_id),
        updates={
            "pending_order_id": str(checkout.get("order_id") or ""),
            "pending_plan_key": spec.plan_key,
            "pending_approval_url": str(checkout.get("approve_url") or ""),
            "last_payment_status": str(checkout.get("status") or ""),
            "plan_source": "payfunnels",
        },
    )
    _save_property_preferences(container, principal_id=context.principal_id, property_preferences=updated)
    return PropertyBillingCheckoutOut(
        generated_at=now_iso(),
        plan_key=spec.plan_key,
        order_id=str(checkout.get("order_id") or ""),
        approve_url=str(checkout.get("approve_url") or ""),
        status=str(checkout.get("status") or ""),
        amount_eur=str(checkout.get("amount_eur") or spec.amount_eur),
    )


def _property_billing_checkout_provider(*, plan_key: str) -> str:
    if payfunnels_configured(plan_key=plan_key):
        return "payfunnels"
    if paypal_configured():
        return "paypal"
    return ""


@router.post("/signals/property/billing/checkout/order", response_model=PropertyBillingCheckoutOut)
def create_property_billing_checkout_order(
    body: PropertyBillingCheckoutCreateIn,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyBillingCheckoutOut:
    provider = _property_billing_checkout_provider(plan_key=body.plan_key)
    if provider == "payfunnels":
        return _create_property_billing_order_payfunnels(
            body=body,
            request=request,
            container=container,
            context=context,
        )
    if provider == "paypal":
        return create_property_billing_order(
            body=body,
            request=request,
            container=container,
            context=context,
        )
    raise HTTPException(status_code=409, detail="property_billing_not_configured")


@router.post("/signals/property/billing/payfunnels/order", response_model=PropertyBillingCheckoutOut, include_in_schema=False)
def create_property_billing_order_payfunnels(
    body: PropertyBillingCheckoutCreateIn,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyBillingCheckoutOut:
    return _create_property_billing_order_payfunnels(body=body, request=request, container=container, context=context)


@router.post("/signals/property/billing/paypal/capture", response_model=PropertyBillingCaptureOut)
def capture_property_billing_order(
    body: PropertyBillingCaptureIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyBillingCaptureOut:
    try:
        spec = property_plan_spec(body.plan_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if spec.plan_key == "free":
        raise HTTPException(status_code=400, detail="property_plan_free_does_not_require_checkout")
    try:
        captured = capture_paypal_property_order(order_id=body.order_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    active_until = paid_plan_expiry(plan_key=spec.plan_key)
    updated = merge_property_commercial(
        _property_preferences(container, principal_id=context.principal_id),
        updates={
            "active_plan_key": spec.plan_key,
            "status": "active",
            "active_until": active_until,
            "last_order_id": str(captured.get("order_id") or ""),
            "last_capture_id": str(captured.get("capture_id") or ""),
            "last_payment_status": str(captured.get("payment_status") or ""),
            "last_payment_amount_eur": str(captured.get("amount_eur") or spec.amount_eur),
            "last_payer_email": str(captured.get("payer_email") or ""),
            "captured_at": now_iso(),
            "pending_order_id": "",
            "pending_plan_key": "",
            "pending_approval_url": "",
            "plan_source": "paypal",
        },
    )
    _save_property_preferences(container, principal_id=context.principal_id, property_preferences=updated)
    return PropertyBillingCaptureOut(
        generated_at=now_iso(),
        order_id=str(captured.get("order_id") or body.order_id),
        plan_key=spec.plan_key,
        capture_id=str(captured.get("capture_id") or ""),
        payment_status=str(captured.get("payment_status") or ""),
        payer_email=str(captured.get("payer_email") or ""),
        amount_eur=str(captured.get("amount_eur") or spec.amount_eur),
        active_until=active_until,
        current_plan_key=spec.plan_key,
    )


@router.get("/signals/property/billing/paypal/return", include_in_schema=False)
def capture_property_billing_order_return(
    token: str = Query(default=""),
    plan_key: str = Query(default=""),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    order_id = str(token or "").strip()
    if not order_id:
        raise HTTPException(status_code=400, detail="paypal_order_id_required")
    capture_property_billing_order(
        PropertyBillingCaptureIn(order_id=order_id, plan_key=plan_key or "free"),
        container=container,
        context=context,
    )
    return RedirectResponse(f"/app/properties?billing=success&plan={plan_key}", status_code=303)


@router.get("/signals/property/billing/payfunnels/return", include_in_schema=False)
def payfunnels_property_billing_return(
    plan_key: str = Query(default=""),
) -> RedirectResponse:
    query = urllib.parse.urlencode({"billing": "pending_confirmation", "plan": plan_key})
    return RedirectResponse(f"/app/properties?{query}", status_code=303)


@router.get("/signals/property/billing/payfunnels/cancel", include_in_schema=False)
def payfunnels_property_billing_cancel(
    plan_key: str = Query(default=""),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    updated = merge_property_commercial(
        _property_preferences(container, principal_id=context.principal_id),
        updates={
            "status": "free",
            "pending_order_id": "",
            "pending_plan_key": "",
            "pending_approval_url": "",
            "last_payment_status": "cancelled",
            "plan_source": "payfunnels",
        },
    )
    _save_property_preferences(container, principal_id=context.principal_id, property_preferences=updated)
    query = urllib.parse.urlencode({"billing": "cancelled", "plan": plan_key})
    return RedirectResponse(f"/app/properties?{query}", status_code=303)


@public_payfunnels_router.post("/signals/property/billing/payfunnels/webhook")
async def payfunnels_property_billing_webhook(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    body_bytes = await request.body()
    signature = str(request.headers.get("x-payfunnels-signature") or "").strip()
    if not verify_payfunnels_webhook_signature(body_bytes=body_bytes, signature=signature):
        raise HTTPException(status_code=401, detail="payfunnels_signature_invalid")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="payfunnels_webhook_invalid_json") from exc
    metadata = dict(payload.get("metadata") or {})
    invoice_title = str(payload.get("invoiceTitle") or payload.get("title") or "").strip()
    title_principal = _payfunnels_title_value(_PAYFUNNELS_TITLE_PRINCIPAL_RE, invoice_title)
    title_order = _payfunnels_title_value(_PAYFUNNELS_TITLE_ORDER_RE, invoice_title)
    field_principal = _payfunnels_field_value(payload, "pq_principal")
    field_order = _payfunnels_field_value(payload, "pq_order")
    field_plan = _payfunnels_field_value(payload, "pq_plan")
    principal_id = str(
        metadata.get("principal_id")
        or payload.get("principal_id")
        or payload.get("client_reference_id")
        or field_principal
        or urllib.parse.unquote(title_principal)
        or ""
    ).strip()
    plan_key = str(metadata.get("plan_key") or payload.get("plan_key") or field_plan or "").strip().lower()
    if not plan_key and "plus" in invoice_title.lower():
        plan_key = "plus"
    if not plan_key and "agent" in invoice_title.lower():
        plan_key = "agent"
    order_id = str(
        payload.get("order_id")
        or payload.get("checkout_id")
        or payload.get("external_id")
        or field_order
        or urllib.parse.unquote(title_order)
        or payload.get("chargeId")
        or payload.get("invoiceId")
        or metadata.get("order_id")
        or ""
    ).strip()
    payment_status = str(payload.get("payment_status") or payload.get("status") or "").strip().lower()
    payer_email = str(
        payload.get("payer_email")
        or payload.get("customerEmail")
        or dict(payload.get("customer") or {}).get("email")
        or ""
    ).strip()
    amount_eur = str(payload.get("amount_eur") or payload.get("amount") or payload.get("chargeAmount") or "").strip()
    event_type = str(payload.get("event_type") or payload.get("event") or "").strip().lower()
    event_id = str(
        payload.get("event_id")
        or payload.get("eventId")
        or payload.get("id")
        or payload.get("webhook_id")
        or payload.get("chargeId")
        or payload.get("invoiceId")
        or ""
    ).strip()
    invoice_id = str(
        payload.get("invoice_id")
        or payload.get("invoiceId")
        or payload.get("invoice")
        or dict(payload.get("invoice") or {}).get("id")
        or ""
    ).strip()
    invoice_payload = dict(payload.get("invoice") or {}) if isinstance(payload.get("invoice"), dict) else {}
    invoice_url = str(
        payload.get("invoice_url")
        or payload.get("invoiceUrl")
        or payload.get("invoiceURL")
        or payload.get("invoicePdfUrl")
        or invoice_payload.get("url")
        or invoice_payload.get("pdf_url")
        or ""
    ).strip()
    invoice_status = str(payload.get("invoice_status") or payload.get("invoiceStatus") or invoice_payload.get("status") or "").strip()
    currency = str(payload.get("currency") or payload.get("currencyCode") or payload.get("chargeCurrency") or "EUR").strip()
    net_amount_eur = str(payload.get("net_amount_eur") or payload.get("netAmount") or payload.get("subtotalAmount") or "").strip()
    vat_amount_eur = str(payload.get("vat_amount_eur") or payload.get("taxAmount") or payload.get("vatAmount") or "").strip()
    vat_rate = str(payload.get("vat_rate") or payload.get("taxRate") or payload.get("vatRate") or "").strip()
    if not principal_id or not plan_key or not order_id:
        raise HTTPException(status_code=400, detail="payfunnels_webhook_missing_fields")
    try:
        spec = property_plan_spec(plan_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not _payfunnels_plan_amount_matches(paid_amount=amount_eur, expected_amount=spec.amount_eur):
        raise HTTPException(status_code=409, detail="payfunnels_amount_mismatch")
    preferences_before = _property_preferences(container, principal_id=principal_id)
    pending_commercial = dict(preferences_before.get("property_commercial") or {})
    pending_order_id = str(pending_commercial.get("pending_order_id") or "").strip()
    pending_plan_key = str(pending_commercial.get("pending_plan_key") or "").strip().lower()
    last_order_id = str(pending_commercial.get("last_order_id") or "").strip()
    active_plan_key = str(pending_commercial.get("active_plan_key") or "").strip().lower()
    completed = payment_status in _PAYFUNNELS_COMPLETED_STATUSES or event_type in _PAYFUNNELS_COMPLETED_EVENTS
    failed = payment_status in _PAYFUNNELS_FAILED_STATUSES or event_type in _PAYFUNNELS_FAILED_EVENTS
    cancelled = payment_status in _PAYFUNNELS_CANCELLED_STATUSES or event_type in _PAYFUNNELS_CANCELLED_EVENTS
    refunded = payment_status in _PAYFUNNELS_REFUNDED_STATUSES or event_type in _PAYFUNNELS_REFUNDED_EVENTS
    if completed and (not pending_order_id or not pending_plan_key):
        if last_order_id == order_id and active_plan_key == spec.plan_key:
            return {
                "status": "ok",
                "idempotent": True,
                "principal_id": principal_id,
                "plan_key": spec.plan_key,
                "current_plan_key": spec.plan_key,
                "payment_status": payment_status or event_type or "completed",
            }
        raise HTTPException(status_code=409, detail="payfunnels_pending_checkout_required")
    if pending_order_id and pending_order_id != order_id:
        raise HTTPException(status_code=409, detail="payfunnels_order_mismatch")
    if pending_plan_key and pending_plan_key != spec.plan_key:
        raise HTTPException(status_code=409, detail="payfunnels_plan_mismatch")
    if completed:
        active_until = paid_plan_expiry(plan_key=spec.plan_key)
        event_updates = property_billing_event_updates(
            pending_commercial,
            provider="payfunnels",
            event_type=event_type or payment_status or "payment.completed",
            event_id=event_id,
            plan_key=spec.plan_key,
            order_id=order_id,
            invoice_id=invoice_id,
            invoice_url=invoice_url,
            invoice_status=invoice_status,
            accounting_status="invoice_pending" if invoice_id else "invoice_not_provided",
            payment_status=payment_status or event_type or "completed",
            currency=currency or "EUR",
            amount_eur=amount_eur or spec.amount_eur,
            net_amount_eur=net_amount_eur,
            vat_amount_eur=vat_amount_eur,
            vat_rate=vat_rate,
        )
        updated = merge_property_commercial(
            preferences_before,
            updates={
                "active_plan_key": spec.plan_key,
                "status": "active",
                "active_until": active_until,
                "last_order_id": order_id,
                "last_capture_id": order_id,
                "last_payment_status": payment_status or event_type or "completed",
                "last_payment_amount_eur": amount_eur or spec.amount_eur,
                "last_payer_email": payer_email,
                "captured_at": now_iso(),
                "pending_order_id": "",
                "pending_plan_key": "",
                "pending_approval_url": "",
                "plan_source": "payfunnels",
                **event_updates,
            },
        )
        _save_property_preferences(container, principal_id=principal_id, property_preferences=updated)
        return {
            "status": "ok",
            "principal_id": principal_id,
            "plan_key": spec.plan_key,
            "current_plan_key": spec.plan_key,
            "payment_status": payment_status or event_type or "completed",
        }
    if failed or cancelled or refunded:
        event_label = event_type or payment_status or ("refunded" if refunded else ("cancelled" if cancelled else "failed"))
        event_updates = property_billing_event_updates(
            pending_commercial,
            provider="payfunnels",
            event_type=event_label,
            event_id=event_id,
            plan_key=spec.plan_key,
            order_id=order_id,
            invoice_id=invoice_id,
            invoice_url=invoice_url,
            invoice_status=invoice_status,
            accounting_status="invoice_pending" if invoice_id else "invoice_not_provided",
            payment_status=payment_status or event_label,
            currency=currency or "EUR",
            amount_eur=amount_eur or spec.amount_eur,
            net_amount_eur=net_amount_eur,
            vat_amount_eur=vat_amount_eur,
            vat_rate=vat_rate,
        )
        updates: dict[str, object] = {
            "last_order_id": order_id,
            "last_capture_id": order_id,
            "last_payment_status": payment_status or event_label,
            "last_payment_amount_eur": amount_eur or spec.amount_eur,
            "last_payer_email": payer_email,
            "pending_order_id": "",
            "pending_plan_key": "",
            "pending_approval_url": "",
            "plan_source": "payfunnels",
            **event_updates,
        }
        if refunded:
            updates["active_plan_key"] = "free"
            updates["active_until"] = ""
            updates["status"] = "refunded"
        elif cancelled:
            updates["status"] = "cancelled"
        else:
            updates["status"] = "payment_failed"
        updated = merge_property_commercial(preferences_before, updates=updates)
        _save_property_preferences(container, principal_id=principal_id, property_preferences=updated)
        return {
            "status": "recorded",
            "principal_id": principal_id,
            "plan_key": spec.plan_key,
            "current_plan_key": str(updated.get("property_commercial", {}).get("active_plan_key") or "free"),
            "payment_status": payment_status or event_label,
        }
    return {
        "status": "ignored",
        "principal_id": principal_id,
        "plan_key": spec.plan_key,
        "payment_status": payment_status or event_type or "pending",
    }


@public_payfunnels_router.post("/signals/property/billing/brilliant-directories/webhook")
async def brilliant_directories_property_billing_webhook(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    body_bytes = await request.body()
    signature = str(
        request.headers.get("x-brilliant-directories-signature")
        or request.headers.get("x-propertyquarry-signature")
        or ""
    ).strip()
    timestamp = str(
        request.headers.get("x-brilliant-directories-timestamp")
        or request.headers.get("x-propertyquarry-webhook-timestamp")
        or ""
    ).strip()
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="brilliant_directories_webhook_invalid_json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="brilliant_directories_webhook_invalid_json")
    metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
    principal_id = str(
        metadata.get("principal_id")
        or payload.get("principal_id")
        or payload.get("client_reference_id")
        or payload.get("customer_id")
        or ""
    ).strip()
    if not principal_id:
        raise HTTPException(status_code=400, detail="brilliant_directories_principal_required")
    preferences_before = _property_preferences(container, principal_id=principal_id)
    commercial_before = dict(preferences_before.get("property_commercial") or {})
    receipt = brilliant_directories_billing_webhook_receipt(
        commercial_before,
        payload=payload,
        body_bytes=body_bytes,
        signature=signature,
        timestamp=timestamp,
    )
    if not bool(receipt.get("signature_verified")):
        raise HTTPException(status_code=401, detail="brilliant_directories_signature_invalid")
    if str(receipt.get("status") or "") == "accepted_advisory_receipt":
        event_updates = dict(receipt.get("billing_event_updates") or {})
        if event_updates:
            updated = merge_property_commercial(preferences_before, updates=event_updates)
            _save_property_preferences(container, principal_id=principal_id, property_preferences=updated)
    preferences_after = _property_preferences(container, principal_id=principal_id)
    commercial_after = dict(preferences_after.get("property_commercial") or {})
    return {
        "status": str(receipt.get("status") or ""),
        "provider": "brilliant_directories",
        "principal_id": principal_id,
        "event_id": str(receipt.get("event_id") or ""),
        "event_type": str(receipt.get("event_type") or ""),
        "advisory_only": True,
        "entitlement_mutation_allowed": False,
        "local_reconciliation_required": True,
        "current_plan_key": str(commercial_after.get("active_plan_key") or "free"),
        "replayed": bool(receipt.get("replayed")),
    }


@router.post("/signals/property/billing/brilliant-directories/reconcile")
async def reconcile_brilliant_directories_property_billing_event(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="brilliant_directories_reconciliation_invalid_json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="brilliant_directories_reconciliation_invalid_json")
    requested_principal = str(payload.get("principal_id") or context.principal_id).strip()
    if requested_principal != context.principal_id:
        raise HTTPException(status_code=403, detail="principal_scope_mismatch")
    event_id = str(payload.get("event_id") or "").strip()
    decision = str(payload.get("decision") or "").strip().lower()
    note = str(payload.get("note") or "").strip()
    preferences_before = _property_preferences(container, principal_id=context.principal_id)
    commercial_before = dict(preferences_before.get("property_commercial") or {})
    try:
        receipt = reconcile_brilliant_directories_billing_event(
            commercial_before,
            event_id=event_id,
            decision=decision,
            reconciled_by=context.operator_id or context.principal_id,
            note=note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    updates = dict(receipt.get("updates") or {})
    updated = merge_property_commercial(preferences_before, updates=updates)
    _save_property_preferences(container, principal_id=context.principal_id, property_preferences=updated)
    preferences_after = _property_preferences(container, principal_id=context.principal_id)
    commercial_after = dict(preferences_after.get("property_commercial") or {})
    public_receipt = {key: value for key, value in receipt.items() if key != "updates"}
    public_receipt["principal_id"] = context.principal_id
    public_receipt["current_plan_key"] = str(commercial_after.get("active_plan_key") or "free")
    public_receipt["local_reconciliation_recorded"] = True
    return public_receipt


@router.get("/signals/property/billing/paypal/cancel", include_in_schema=False)
def cancel_property_billing_order_return(
    plan_key: str = Query(default=""),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    updated = merge_property_commercial(
        _property_preferences(container, principal_id=context.principal_id),
        updates={
            "status": "free",
            "pending_order_id": "",
            "pending_plan_key": "",
            "pending_approval_url": "",
            "last_payment_status": "cancelled",
            "plan_source": "paypal",
        },
    )
    _save_property_preferences(container, principal_id=context.principal_id, property_preferences=updated)
    return RedirectResponse(f"/app/properties?billing=cancelled&plan={plan_key}", status_code=303)


def _require_property_fact_enrichment_context(
    *,
    context: RequestContext,
) -> None:
    principal_id = str(context.principal_id or "").strip()
    auth_source = str(context.auth_source or "").strip().lower()
    if (
        not principal_id
        or context.authenticated is not True
        or auth_source in {"", "anonymous", "loopback_no_auth"}
    ):
        raise HTTPException(status_code=401, detail="authentication_required")


def _require_property_fact_same_origin(
    request: Request,
    *,
    context: RequestContext,
) -> None:
    content_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="application_json_required")
    fetch_site = str(request.headers.get("sec-fetch-site") or "").strip().lower()
    if fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="same_origin_required")
    origin = str(request.headers.get("origin") or "").strip()
    auth_source = str(context.auth_source or "").strip().lower()
    non_ambient_auth = auth_source == "api_token" or "service" in auth_source
    if not origin and non_ambient_auth:
        return
    if not origin:
        raise HTTPException(status_code=403, detail="same_origin_required")
    supplied = urllib.parse.urlsplit(origin)
    configured_base_url = next(
        (
            str(os.environ.get(key) or "").strip()
            for key in (
                "PROPERTYQUARRY_PUBLIC_BASE_URL",
                "EA_PUBLIC_BASE_URL",
                "EA_APP_PUBLIC_BASE_URL",
            )
            if str(os.environ.get(key) or "").strip()
        ),
        str(request.base_url),
    )
    expected = urllib.parse.urlsplit(configured_base_url)
    supplied_origin = (supplied.scheme.lower(), supplied.netloc.lower())
    expected_origin = (expected.scheme.lower(), expected.netloc.lower())
    if (
        supplied.username is not None
        or supplied.password is not None
        or supplied.path not in {"", "/"}
        or supplied.query
        or supplied.fragment
        or supplied.scheme.lower() not in {"http", "https"}
        or supplied_origin != expected_origin
    ):
        raise HTTPException(status_code=403, detail="same_origin_required")


@router.get("/signals/property/search/run/{run_id}", response_model=PropertySearchRunStatusOut)
def property_search_run_status(
    run_id: str,
    lightweight: bool = Query(default=False),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertySearchRunStatusOut:
    payload = _property_search_run_status_payload(
        run_id=run_id,
        container=container,
        context=context,
        lightweight=lightweight,
    )
    return PropertySearchRunStatusOut(**_property_search_payload_with_status_url(payload, canonical=False))


@router.get(
    "/signals/property/search/run/{run_id}/candidates/{candidate_ref}/fact-enrichment",
    response_model=PropertyFactEnrichmentOut,
)
def property_candidate_fact_enrichment_status(
    run_id: str,
    candidate_ref: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyFactEnrichmentOut:
    _require_property_fact_enrichment_context(context=context)
    payload = build_product_service(container).get_property_candidate_fact_enrichment(
        principal_id=context.principal_id,
        run_id=run_id,
        candidate_ref=candidate_ref,
    )
    if not isinstance(payload, dict):
        raise HTTPException(status_code=404, detail="property_candidate_not_found")
    return PropertyFactEnrichmentOut(**payload)


@router.post(
    "/signals/property/search/run/{run_id}/candidates/{candidate_ref}/fact-enrichment",
    response_model=PropertyFactEnrichmentOut,
    status_code=202,
)
def start_property_candidate_fact_enrichment(
    run_id: str,
    candidate_ref: str,
    body: PropertyFactEnrichmentStartIn,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertyFactEnrichmentOut:
    _require_property_fact_enrichment_context(context=context)
    _require_property_fact_same_origin(request, context=context)
    try:
        payload = build_product_service(container).start_property_candidate_fact_enrichment(
            principal_id=context.principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            retry_failed=bool(body.retry_failed),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="property_candidate_not_found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="property_fact_enrichment_unavailable") from exc
    return PropertyFactEnrichmentOut(**payload)


@router.get("/property/search-runs/{run_id}", response_model=PropertySearchRunStatusOut)
def property_search_run_status_v2(
    run_id: str,
    lightweight: bool = Query(default=False),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertySearchRunStatusOut:
    payload = _property_search_run_status_payload(
        run_id=run_id,
        container=container,
        context=context,
        lightweight=lightweight,
    )
    return PropertySearchRunStatusOut(**_property_search_payload_with_status_url(payload, canonical=True))


@router.get("/property/search-runs/{run_id}/events", response_model=dict[str, object])
def property_search_run_events_v2(
    run_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    payload = _property_search_run_status_payload(
        run_id=run_id,
        container=container,
        context=context,
    )
    return {
        "generated_at": now_iso(),
        "run_id": str(payload.get("run_id") or run_id).strip(),
        "status": str(payload.get("status") or "").strip(),
        "status_url": _property_search_status_url(payload.get("run_id") or run_id, canonical=True),
        "events": [dict(item) for item in list(payload.get("events") or []) if isinstance(item, dict)],
    }


@router.delete("/property/search-runs", response_model=dict[str, object])
def clear_property_search_runs_v2(
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_product_service(container)
    try:
        result = service.clear_property_search_runs(
            principal_id=context.principal_id,
            account_email=str(context.access_email or "").strip(),
        )
    except TypeError:
        result = service.clear_property_search_runs(principal_id=context.principal_id)
    return {
        "generated_at": now_iso(),
        "principal_id": context.principal_id,
        "deleted_count": int(result.get("deleted_count") or 0),
        "run_ids": list(result.get("run_ids") or []),
    }


@router.post("/property/search-runs/clear")
def clear_property_search_runs_form(
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    service = build_product_service(container)
    try:
        result = service.clear_property_search_runs(
            principal_id=context.principal_id,
            account_email=str(context.access_email or "").strip(),
        )
    except TypeError:
        result = service.clear_property_search_runs(principal_id=context.principal_id)
    deleted_count = int(result.get("deleted_count") or 0)
    return RedirectResponse(
        url=f"/app/account?history_cleared={deleted_count}#data-export",
        status_code=303,
    )


@router.delete("/property/search-runs/{run_id}", response_model=dict[str, object])
def delete_property_search_run_v2(
    run_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    service = build_product_service(container)
    try:
        deleted = service.delete_property_search_run(
            principal_id=context.principal_id,
            run_id=run_id,
            account_email=str(context.access_email or "").strip(),
        )
    except TypeError:
        deleted = service.delete_property_search_run(
            principal_id=context.principal_id,
            run_id=run_id,
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="property_search_run_not_found")
    return {
        "generated_at": now_iso(),
        "run_id": str(run_id or "").strip(),
        "deleted": True,
    }


@router.post("/signals/property/search/run/{run_id}/research-tasks/{task_id:path}", response_model=PropertySearchRunStatusOut)
def update_property_search_research_task(
    run_id: str,
    task_id: str,
    body: PropertySearchResearchTaskUpdateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertySearchRunStatusOut:
    payload = _update_property_search_research_task_payload(
        run_id=run_id,
        task_id=task_id,
        body=body,
        container=container,
        context=context,
    )
    return PropertySearchRunStatusOut(**_property_search_payload_with_status_url(payload, canonical=False))


@router.post("/property/search-runs/{run_id}/research-tasks/{task_id:path}", response_model=PropertySearchRunStatusOut)
def update_property_search_research_task_v2(
    run_id: str,
    task_id: str,
    body: PropertySearchResearchTaskUpdateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PropertySearchRunStatusOut:
    payload = _update_property_search_research_task_payload(
        run_id=run_id,
        task_id=task_id,
        body=body,
        container=container,
        context=context,
    )
    return PropertySearchRunStatusOut(**_property_search_payload_with_status_url(payload, canonical=True))


@router.post("/signals/google/photos/session", response_model=GooglePhotosPickerSessionOut)
def create_google_photos_picker_session(
    body: GooglePhotosPickerSessionIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> GooglePhotosPickerSessionOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "google_photos").strip()
    try:
        payload = service.create_google_photos_picker_session(
            principal_id=context.principal_id,
            actor=actor,
            account_email=body.account_email,
            binding_id=body.binding_id,
            max_item_count=body.max_item_count,
            autoclose=body.autoclose,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return GooglePhotosPickerSessionOut(**payload)


@router.get("/signals/google/photos/session/{session_id}", response_model=GooglePhotosPickerSessionOut)
def get_google_photos_picker_session(
    session_id: str,
    account_email: str = Query(default=""),
    binding_id: str = Query(default=""),
    autoclose: bool = Query(default=True),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> GooglePhotosPickerSessionOut:
    service = build_product_service(container)
    try:
        payload = service.get_google_photos_picker_session(
            principal_id=context.principal_id,
            session_id=session_id,
            account_email=account_email,
            binding_id=binding_id,
            autoclose=autoclose,
        )
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 404 if detail == "google_photos_picker_session_not_found" else 409
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return GooglePhotosPickerSessionOut(**payload)


@router.post("/signals/google/photos/sync", response_model=GooglePhotosSignalSyncOut)
def sync_google_photos_signals(
    body: GooglePhotosSignalSyncIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> GooglePhotosSignalSyncOut:
    service = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "google_photos").strip()
    try:
        payload = service.sync_google_photos_signals(
            principal_id=context.principal_id,
            actor=actor,
            session_id=body.session_id,
            account_email=body.account_email,
            binding_id=body.binding_id,
            max_items=body.max_items,
            delete_session=body.delete_session,
        )
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 404 if detail == "google_photos_picker_session_not_found" else 409
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return GooglePhotosSignalSyncOut(**payload)


@router.get("/signals/google/status", response_model=GoogleSignalSyncStatusOut)
def get_google_signal_sync_status(
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> GoogleSignalSyncStatusOut:
    service = build_product_service(container)
    return GoogleSignalSyncStatusOut(**service.google_signal_sync_status(principal_id=context.principal_id))


@router.get("/webhooks", response_model=WebhookResponse)
def get_webhooks(
    limit: int = Query(default=50, ge=1, le=200),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WebhookResponse:
    service = build_product_service(container)
    items = service.list_webhooks(principal_id=context.principal_id, limit=limit)
    return WebhookResponse(generated_at=now_iso(), items=[WebhookOut(**item) for item in items], total=len(items))


@router.post("/webhooks", response_model=WebhookOut)
def register_webhook(
    body: WebhookRegisterIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WebhookOut:
    service = build_product_service(container)
    payload = service.register_webhook(
        principal_id=context.principal_id,
        label=body.label,
        target_url=body.target_url,
        event_types=tuple(body.event_types),
        status=body.status,
    )
    return WebhookOut(**payload)


@router.get("/webhooks/deliveries", response_model=WebhookDeliveryResponse)
def get_webhook_deliveries(
    webhook_id: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WebhookDeliveryResponse:
    service = build_product_service(container)
    items = service.list_webhook_deliveries(
        principal_id=context.principal_id,
        webhook_id=webhook_id,
        limit=limit,
    )
    return WebhookDeliveryResponse(generated_at=now_iso(), items=[WebhookDeliveryOut(**item) for item in items], total=len(items))


@router.post("/webhooks/{webhook_id}/test", response_model=WebhookTestResultOut)
def test_webhook(
    webhook_id: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> WebhookTestResultOut:
    service = build_product_service(container)
    payload = service.test_webhook(principal_id=context.principal_id, webhook_id=webhook_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="webhook_not_found")
    return WebhookTestResultOut(webhook=WebhookOut(**payload["webhook"]), delivery=WebhookDeliveryOut(**payload["delivery"]))


@router.get("/channel-loop", response_model=ChannelLoopOut)
def get_channel_loop(
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> ChannelLoopOut:
    service = build_product_service(container)
    service.record_surface_event(
        principal_id=context.principal_id,
        event_type="channel_loop_opened",
        surface="channel_loop_api",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    return ChannelLoopOut(
        **service.channel_loop_pack(
            principal_id=context.principal_id,
            operator_id=str(context.operator_id or "").strip(),
        )
    )


@router.get("/channel-loop/{digest_key}/plain", response_class=PlainTextResponse)
def get_channel_digest_plain(
    digest_key: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> PlainTextResponse:
    service = build_product_service(container)
    text = service.channel_digest_text(
        principal_id=context.principal_id,
        digest_key=digest_key,
        operator_id=str(context.operator_id or "").strip(),
        base_url=_public_base_url(request),
    )
    if str(digest_key or "").strip().lower() == "memo" and text.startswith("Today digest"):
        text = text.replace("Today digest", "Morning memo digest", 1)
    if not text:
        raise HTTPException(status_code=404, detail="channel_digest_not_found")
    service.record_surface_event(
        principal_id=context.principal_id,
        event_type="channel_digest_plain_opened",
        surface=f"channel_digest_{digest_key}_plain_api",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    return PlainTextResponse(text)


@router.post("/channel-loop/{digest_key}/deliveries", response_model=ChannelDigestDeliveryOut)
def create_channel_digest_delivery(
    digest_key: str,
    body: ChannelDigestDeliveryCreateIn,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> ChannelDigestDeliveryOut:
    service = build_product_service(container)
    payload = service.issue_channel_digest_delivery(
        principal_id=context.principal_id,
        digest_key=digest_key,
        recipient_email=body.recipient_email,
        role=body.role,
        display_name=body.display_name,
        operator_id=body.operator_id,
        delivery_channel=body.delivery_channel,
        expires_in_hours=body.expires_in_hours,
        base_url=_public_base_url(request),
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="channel_digest_not_found")
    return ChannelDigestDeliveryOut(**payload)
