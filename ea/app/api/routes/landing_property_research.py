from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from datetime import datetime
from typing import Any

from app.api.routes.landing_property_workspace_helpers import (
    _candidate_detail_sections,
    _official_risk_posture_rows,
    _property_candidate_display_facts,
    _property_candidate_orientation_preview,
    _property_candidate_preview_image,
    _property_candidate_floorplan_url,
    _property_candidate_source_virtual_tour_url,
    _property_visual_reason_key,
)
from app.product.property_location_research import property_school_context_summary
from app.product.property_evidence_overlays import build_property_evidence_overlay_rows
from app.product.projections.common import compact_text
from app.product.service import (
    _hosted_property_visual_progress_snapshot,
    _hosted_property_visual_progress_stage_label,
    _property_apply_location_hint_research,
    _property_currency_code_from_facts,
    _property_cooling_corridor_match_reason,
    _property_enrich_missing_fact_research,
    _property_investment_area_sqm,
    _property_investment_underwriting_payload,
    _property_investment_location_seed,
    _property_investment_price_eur,
    _property_investment_research_snapshot,
    _merge_property_facts_with_source_research,
    _property_money_amount_label,
    _safe_provider_live_360_url,
    _property_visual_eta_label,
    _property_visual_progress_pct,
    _property_visual_terminal_status_for_reason,
    _property_visual_unavailable_detail,
)
from app.product import property_tour_hosting
from app.services.property_customer_copy import sanitize_property_marketing_copy, summarize_property_description_copy
from app.services.property_locale import (
    PropertyLocaleError,
    format_market_currency,
    format_market_datetime,
    format_market_decimal,
    resolve_market_locale,
)
from app.services.property_market_catalog import supported_currency_codes


_PROPERTY_TOUR_PENDING_STATES = frozenset(
    {"queued", "pending", "processing", "running", "in_progress", "started", "rendering", "repairing"}
)
_PROPERTY_TOUR_TERMINAL_STATES = frozenset({"blocked", "failed", "skipped", "not_applicable"})
_PROPERTY_TOUR_TERMINAL_SOURCE_GAP_REASONS = frozenset(
    {
        "floorplan_assets_unavailable",
        "floorplan_missing",
        "gallery_assets_unavailable",
        "listing_360_media_missing",
        "listing_expired",
        "property_tour_fallback_disabled",
        "property_tour_rebuild_required",
        "provider_export_missing",
        "pure_360_assets_unavailable",
    }
)
_PROPERTY_AI_360_DISCLOSURE = (
    "AI reconstruction based on property photos; not a captured 360 or measured survey."
)
_PROPERTY_AI_360_PUBLIC_HOSTS = frozenset({"propertyquarry.com", "www.propertyquarry.com"})
_PROPERTY_AI_360_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _property_market_country_code(values: dict[str, object]) -> str:
    """Return only an explicit country covered by the governed locale catalog."""

    for key in (
        "market_country_code",
        "country_code",
        "property_country_code",
        "source_country_code",
    ):
        country_code = str(values.get(key) or "").strip().upper()
        if not country_code:
            continue
        try:
            return resolve_market_locale(country_code).country_code
        except PropertyLocaleError:
            return ""
    return ""


def _property_market_fraction_digits(value: object) -> int:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0
    return 0 if numeric.is_integer() else 1


def _property_market_decimal_display(
    value: object,
    *,
    facts: dict[str, object],
) -> str:
    country_code = _property_market_country_code(facts)
    if not country_code:
        return ""
    try:
        return format_market_decimal(
            value,
            country_code=country_code,
            fraction_digits=_property_market_fraction_digits(value),
        )
    except PropertyLocaleError:
        return ""


def _property_market_currency_display(
    value: object,
    *,
    facts: dict[str, object],
    currency_code: str,
) -> str:
    country_code = _property_market_country_code(facts)
    if not country_code:
        return ""
    try:
        return format_market_currency(
            value,
            country_code=country_code,
            currency_code=currency_code,
            fraction_digits=0,
        )
    except PropertyLocaleError:
        return ""


def _property_market_display_context(
    *,
    facts: dict[str, object],
    observed_at: object = "",
) -> dict[str, str]:
    country_code = _property_market_country_code(facts)
    if not country_code:
        return {}
    try:
        spec = resolve_market_locale(country_code)
    except PropertyLocaleError:
        return {}
    locale_labels = {
        "de-AT": "Deutsch (Österreich)",
        "de-DE": "Deutsch (Deutschland)",
        "es-CR": "Español (Costa Rica)",
    }
    supported_currencies = set(supported_currency_codes())
    explicit_currency_code = next(
        (
            str(facts.get(key) or "").strip().upper()
            for key in ("price_currency", "currency_code", "currency")
            if str(facts.get(key) or "").strip().upper() in supported_currencies
        ),
        "",
    )
    has_explicit_eur_amount = any(
        key.endswith("_eur") and value not in (None, "", [])
        for key, value in facts.items()
    )
    currency_code = explicit_currency_code or ("EUR" if has_explicit_eur_amount else spec.currency_code)
    result = {
        "country_code": spec.country_code,
        "locale": spec.locale,
        "locale_label": locale_labels.get(spec.locale, spec.locale),
        "currency_code": currency_code or spec.currency_code,
        "timezone": spec.timezone,
        "updated_at_iso": "",
        "updated_at_display": "",
    }
    parsed: datetime | None = None
    if isinstance(observed_at, datetime):
        parsed = observed_at
    else:
        raw = str(observed_at or "").strip()
        if raw and len(raw) <= 80:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
    if parsed is None or parsed.tzinfo is None:
        return result
    try:
        result["updated_at_display"] = format_market_datetime(
            parsed,
            country_code=spec.country_code,
            requested_locale=spec.locale,
        )
    except PropertyLocaleError:
        return result
    result["updated_at_iso"] = parsed.isoformat()
    return result


def _object_detail_row(
    title: str,
    detail: str,
    tag: str,
    href: str = "",
    action_href: str = "",
    action_label: str = "",
    action_value: str = "",
    action_method: str = "",
    return_to: str = "",
    secondary_action_href: str = "",
    secondary_action_label: str = "",
    secondary_action_value: str = "",
    secondary_action_method: str = "",
    secondary_return_to: str = "",
    tertiary_action_href: str = "",
    tertiary_action_label: str = "",
    tertiary_action_value: str = "",
    tertiary_action_method: str = "",
    tertiary_return_to: str = "",
    quaternary_action_href: str = "",
    quaternary_action_label: str = "",
    quaternary_action_value: str = "",
    quaternary_action_method: str = "",
    quaternary_return_to: str = "",
) -> dict[str, str]:
    row = {
        "title": str(title or "").strip(),
        "detail": str(detail or "").strip(),
        "tag": str(tag or "").strip(),
    }
    if href:
        row["href"] = href
    if action_href:
        row["action_href"] = action_href
    if action_label:
        row["action_label"] = action_label
    if action_value:
        row["action_value"] = action_value
    if action_method:
        row["action_method"] = action_method
    if return_to:
        row["return_to"] = return_to
    if secondary_action_href:
        row["secondary_action_href"] = secondary_action_href
    if secondary_action_label:
        row["secondary_action_label"] = secondary_action_label
    if secondary_action_value:
        row["secondary_action_value"] = secondary_action_value
    if secondary_action_method:
        row["secondary_action_method"] = secondary_action_method
    if secondary_return_to:
        row["secondary_return_to"] = secondary_return_to
    if tertiary_action_href:
        row["tertiary_action_href"] = tertiary_action_href
    if tertiary_action_label:
        row["tertiary_action_label"] = tertiary_action_label
    if tertiary_action_value:
        row["tertiary_action_value"] = tertiary_action_value
    if tertiary_action_method:
        row["tertiary_action_method"] = tertiary_action_method
    if tertiary_return_to:
        row["tertiary_return_to"] = tertiary_return_to
    if quaternary_action_href:
        row["quaternary_action_href"] = quaternary_action_href
    if quaternary_action_label:
        row["quaternary_action_label"] = quaternary_action_label
    if quaternary_action_value:
        row["quaternary_action_value"] = quaternary_action_value
    if quaternary_action_method:
        row["quaternary_action_method"] = quaternary_action_method
    if quaternary_return_to:
        row["quaternary_return_to"] = quaternary_return_to
    return row


def _evidence_detail_rows(items) -> list[dict[str, str]]:  # type: ignore[no-untyped-def]
    rows: list[dict[str, str]] = []
    for item in items or ():
        rows.append(
            _object_detail_row(
                str(getattr(item, "note", "") or getattr(item, "ref", "") or "Linked source"),
                str(getattr(item, "ref", "") or "No source linked."),
                str(getattr(item, "source_type", "") or "Source"),
            )
        )
    if rows:
        return rows
    return [_object_detail_row("No linked sources yet", "Nothing extra is attached here yet.", "Pending")]


def _render_console_object_detail(
    *,
    request: Request,
    context: RequestContext,
    workspace_label: str,
    page_title: str,
    current_nav: str,
    console_title: str,
    console_summary: str,
    object_kind: str,
    object_title: str,
    object_summary: str,
    object_meta: list[dict[str, str]],
    object_media: dict[str, object] | None = None,
    object_ooda_kicker: str = "",
    object_ooda_title: str = "",
    object_ooda_copy: str = "",
    object_ooda_rows: list[dict[str, str]] | None = None,
    object_sidebar_kicker: str = "",
    object_sidebar_title: str,
    object_sidebar_copy: str,
    object_sidebar_rows: list[dict[str, str]],
    object_sidebar_default_open: bool = False,
    object_sections: list[dict[str, object]],
    object_sidebar_form: dict[str, object] | None = None,
    object_feedback: dict[str, object] | None = None,
) -> HTMLResponse:
    from app.api.routes.landing import (
        _console_shell_context,
        _render_public_template,
    )
    from app.api.routes.landing_content import app_nav_groups_for_brand
    from app.services.public_branding import request_brand

    return _render_public_template(
        request,
        "app/object_detail.html",
        **{
            **_console_shell_context(
                request=request,
                page_title=page_title,
                current_nav=current_nav,
                context=context,
                console_title=console_title,
                console_summary=console_summary,
                nav_groups=app_nav_groups_for_brand(request_brand(request)["key"]),
                workspace_label=workspace_label,
                cards=[],
                stats=[{"label": item["label"], "value": item["value"]} for item in object_meta],
            ),
            "object_kind": object_kind,
            "object_title": object_title,
            "object_summary": object_summary,
            "object_meta": object_meta,
            "object_media": object_media or {},
            "object_ooda_kicker": object_ooda_kicker,
            "object_ooda_title": object_ooda_title,
            "object_ooda_copy": object_ooda_copy,
            "object_ooda_rows": object_ooda_rows or [],
            "object_sidebar_kicker": object_sidebar_kicker,
            "object_sidebar_title": object_sidebar_title,
            "object_sidebar_copy": object_sidebar_copy,
            "object_sidebar_rows": object_sidebar_rows,
            "object_sidebar_default_open": object_sidebar_default_open,
            "object_sections": object_sections,
            "object_sidebar_form": object_sidebar_form or {},
            "object_feedback": object_feedback or {},
        },
    )


def _property_candidate_ref(candidate: dict[str, object]) -> str:
    explicit_ref = str(candidate.get("candidate_ref") or candidate.get("research_candidate_ref") or "").strip()
    if explicit_ref:
        return explicit_ref
    raw = "|".join(
        str(candidate.get(key) or "").strip()
        for key in ("title", "property_url", "review_url", "source_ref", "source_label")
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _property_candidate_resolution_priority(candidate: dict[str, object]) -> tuple[int, int, int]:
    tour_url = str(candidate.get("tour_url") or "").strip()
    flythrough_url = str(candidate.get("flythrough_url") or "").strip()
    tour_status = str(candidate.get("tour_status") or "").strip().lower()
    flythrough_status = str(candidate.get("flythrough_status") or "").strip().lower()
    ready_score = int(bool(tour_url)) + int(bool(flythrough_url))
    active_score = int(tour_status == "ready") + int(flythrough_status == "ready")
    if active_score <= 0:
        active_score += int(tour_status in {"created", "completed", "published"}) + int(
            flythrough_status in {"created", "completed", "published"}
        )
    payload_score = sum(
        1
        for key in ("packet_url", "review_url", "property_url", "vendor_tour_url", "summary", "fit_summary")
        if str(candidate.get(key) or "").strip()
    )
    payload_score += len(dict(candidate.get("property_facts") or {})) if isinstance(candidate.get("property_facts"), dict) else 0
    return (ready_score, active_score, payload_score)


def _property_merge_candidate_rows(candidates: list[dict[str, object]]) -> dict[str, object]:
    if not candidates:
        return {}
    ranked = sorted(candidates, key=_property_candidate_resolution_priority, reverse=True)
    base = dict(ranked[0])
    merged_facts: dict[str, object] = {}
    for candidate in reversed(ranked):
        facts = candidate.get("property_facts")
        if isinstance(facts, dict):
            merged_facts.update(dict(facts))
    for candidate in ranked[1:]:
        for key, value in candidate.items():
            if key == "property_facts":
                continue
            if base.get(key) in (None, "", [], {}):
                base[key] = value
    if merged_facts:
        if isinstance(base.get("property_facts"), dict):
            merged_facts = {**merged_facts, **dict(base.get("property_facts") or {})}
        base["property_facts"] = merged_facts
    return base


def _property_shortlist_candidates_from_context(property_context: dict[str, object]) -> list[dict[str, object]]:
    run_payload = dict(property_context.get("run") or {})
    run_summary = dict(run_payload.get("summary") or {})
    run_id = str(run_payload.get("run_id") or "").strip()
    packet_candidates: dict[str, list[dict[str, object]]] = {}

    def _append_candidate(candidate_row: dict[str, object], source_label: str) -> None:
        candidate_row = dict(candidate_row)
        candidate_row.setdefault("source_label", source_label)
        candidate_row.setdefault(
            "property_facts",
            dict(candidate_row.get("property_facts") or {}) if isinstance(candidate_row.get("property_facts"), dict) else {},
        )
        packet_ref = _property_candidate_ref(
            {
                "candidate_ref": str(candidate_row.get("candidate_ref") or candidate_row.get("research_candidate_ref") or "").strip(),
                "title": str(candidate_row.get("title") or "").strip(),
                "property_url": str(candidate_row.get("property_url") or "").strip(),
                "review_url": str(candidate_row.get("review_url") or "").strip(),
                "source_ref": str(candidate_row.get("source_ref") or "").strip(),
                "source_label": source_label,
            }
        )
        candidate_row.setdefault("candidate_ref", packet_ref)
        packet_url = f"/app/research/{packet_ref}"
        if run_id:
            packet_url = f"{packet_url}?run_id={urllib.parse.quote(run_id, safe='')}"
        candidate_row.setdefault("packet_url", packet_url)
        packet_candidates.setdefault(packet_ref, []).append(candidate_row)

    for candidate in list(run_summary.get("ranked_candidates") or []):
        if not isinstance(candidate, dict):
            continue
        candidate_row = dict(candidate)
        source_label = str(candidate_row.get("source_label") or candidate_row.get("source_url") or "Source").strip()
        _append_candidate(candidate_row, source_label)
    for source in list(run_summary.get("sources") or []):
        if not isinstance(source, dict):
            continue
        source_label = str(source.get("source_label") or source.get("source_url") or "Source").strip()
        for key in ("top_candidates", "research_candidates", "evaluating_candidates"):
            for candidate in list(source.get(key) or []):
                if not isinstance(candidate, dict):
                    continue
                _append_candidate(dict(candidate), source_label)
    merged_rows: list[dict[str, object]] = []
    for packet_ref, rows in packet_candidates.items():
        merged = _property_merge_candidate_rows(rows)
        if not merged:
            continue
        merged.setdefault("candidate_ref", packet_ref)
        packet_url = str(merged.get("packet_url") or "").strip()
        if not packet_url:
            packet_url = f"/app/research/{packet_ref}"
            if run_id:
                packet_url = f"{packet_url}?run_id={urllib.parse.quote(run_id, safe='')}"
            merged["packet_url"] = packet_url
        merged_rows.append(merged)
    return merged_rows


def _property_lookup_candidate(
    *,
    property_context: dict[str, object],
    candidate_ref: str,
) -> dict[str, object] | None:
    normalized_ref = str(candidate_ref or "").strip()
    if not normalized_ref:
        return None
    summary = dict(dict(property_context.get("run") or {}).get("summary") or {})
    matches: list[dict[str, object]] = []
    for candidate in list(summary.get("ranked_candidates") or []):
        if not isinstance(candidate, dict):
            continue
        candidate_row = dict(candidate)
        if _property_candidate_ref(candidate_row) == normalized_ref:
            matches.append(candidate_row)
    for source in list(summary.get("sources") or []):
        if not isinstance(source, dict):
            continue
        source_label = str(source.get("source_label") or source.get("source_url") or "Source").strip()
        for key in ("top_candidates", "research_candidates"):
            for raw_candidate in list(source.get(key) or []):
                if not isinstance(raw_candidate, dict):
                    continue
                candidate = dict(raw_candidate)
                candidate.setdefault("source_label", source_label)
                if _property_candidate_ref(candidate) == normalized_ref:
                    matches.append(candidate)
    for candidate in _property_shortlist_candidates_from_context(property_context):
        if not isinstance(candidate, dict):
            continue
        candidate_row = dict(candidate)
        if _property_candidate_ref(candidate_row) == normalized_ref:
            matches.append(candidate_row)
    if not matches:
        return None
    return _property_merge_candidate_rows(matches)


def _property_enriched_candidate_facts(
    *,
    candidate: dict[str, object],
    preferences: dict[str, object] | None = None,
    market_preferences: dict[str, object] | None = None,
    force_source_research_for_selected_distances: bool = False,
    allow_source_research: bool = True,
    allow_location_hint_research: bool = True,
) -> dict[str, object]:
    facts = _property_candidate_display_facts(candidate)
    title = str(candidate.get("title") or "").strip()
    summary = str(candidate.get("summary") or "").strip()
    if allow_location_hint_research:
        facts = _property_apply_location_hint_research(
            facts=facts,
            title=title,
            summary=summary,
        )
    if not any(
        str(facts.get(key) or "").strip()
        for key in ("description", "description_text", "object_description", "listing_description", "summary")
    ):
        fallback_description = summarize_property_description_copy(title or summary)
        if fallback_description:
            facts["description"] = fallback_description
    text = " | ".join(part for part in (title, summary) if part)
    if text:
        parsed_price_eur = _property_investment_price_eur(facts)
        supported_currencies = set(supported_currency_codes())
        current_currency_code = str(facts.get("currency_code") or facts.get("price_currency") or "").strip().upper()
        needs_price_value = not isinstance(parsed_price_eur, float)
        needs_currency_code = current_currency_code not in supported_currencies
        if needs_price_value or needs_currency_code:
            currency_pattern = "|".join(re.escape(code) for code in supported_currency_codes())
            price_match = re.search(
                rf"(?:(?P<symbol>€|£|CHF|USD|CAD|AUD)|(?P<code>{currency_pattern}))\s*([\d\.\s]+(?:,\d+)?)",
                text,
                flags=re.IGNORECASE,
            )
            if price_match:
                raw_amount = str(price_match.group(3) or "").strip().replace(" ", "")
                normalized_amount = raw_amount.replace(".", "").replace(",", ".")
                try:
                    title_price_value = float(normalized_amount)
                    if needs_price_value or not isinstance(facts.get("price_eur"), (int, float)):
                        facts["price_eur"] = title_price_value
                    raw_currency = str(price_match.group("code") or price_match.group("symbol") or "").strip().upper()
                    symbol_currency = {"€": "EUR", "£": "GBP"}.get(raw_currency, raw_currency)
                    if needs_currency_code and symbol_currency in supported_currencies:
                        facts["currency_code"] = symbol_currency
                    currency_code = _property_currency_code_from_facts(facts)
                    facts.setdefault(
                        "price_display",
                        compact_text(price_match.group(0), fallback=_property_money_amount_label(title_price_value, currency_code=currency_code), limit=120),
                    )
                except Exception:
                    pass
        if not isinstance(_property_investment_area_sqm(facts), float):
            area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*m[²2]", text, flags=re.IGNORECASE)
            if area_match:
                try:
                    facts["area_m2"] = float(str(area_match.group(1) or "").replace(",", "."))
                except Exception:
                    pass
        if "rooms" not in facts and "room_count" not in facts:
            rooms_match = re.search(r"(\d+(?:[.,]\d+)?)\s*[- ]?Zimmer", text, flags=re.IGNORECASE)
            if rooms_match:
                try:
                    facts["rooms"] = float(str(rooms_match.group(1) or "").replace(",", "."))
                except Exception:
                    pass
        if "postal_name" not in facts and "address" not in facts and "district" not in facts:
            postal_match = re.search(r"\((\d{4}\s+[A-Za-zÄÖÜäöüß][^)]*)\)", text)
            if postal_match:
                postal_name = str(postal_match.group(1) or "").strip()[:160]
                if postal_name:
                    facts["postal_name"] = postal_name
                    facts.setdefault("address", postal_name)
    facts = _property_enrich_missing_fact_research(
        facts=facts,
        property_url=str(candidate.get("property_url") or "").strip(),
        title=title,
        summary=summary,
        source_label=str(candidate.get("source_label") or "").strip(),
    )
    normalized_preferences = dict(preferences or {})
    normalized_market_preferences = (
        normalized_preferences
        if market_preferences is None
        else dict(market_preferences)
    )
    market_country_code = _property_market_country_code(normalized_market_preferences)
    if market_country_code:
        facts.setdefault("market_country_code", market_country_code)
    property_url = str(candidate.get("property_url") or "").strip()
    selected_distance_rows = _property_selected_distance_rows(
        facts=facts,
        preferences=normalized_preferences,
    )
    available_distance_rows = _property_available_nearby_distance_rows(facts=facts)
    research_snapshot = (
        dict(facts.get("listing_research_snapshot") or {})
        if isinstance(facts.get("listing_research_snapshot"), dict)
        else {}
    )
    location_hint_attempted = bool(
        facts.get("location_hint_research_attempted")
        or research_snapshot.get("location_hint_research_attempted")
    )
    needs_distance_backfill = (
        selected_distance_rows
        and any(str(row.get("tag") or "").strip().casefold() == "to check" for row in selected_distance_rows)
    )
    needs_nearby_retry = not selected_distance_rows and not available_distance_rows
    should_force_selected_distance_refresh = bool(
        force_source_research_for_selected_distances and selected_distance_rows
    )
    if (
        allow_source_research
        and
        property_url
        and (
            should_force_selected_distance_refresh
            or ((needs_distance_backfill and not location_hint_attempted) or needs_nearby_retry)
        )
    ):
        facts = _merge_property_facts_with_source_research(
            property_url=property_url,
            property_facts=facts,
        )
    return facts


def _property_missing_fact_items(facts: dict[str, object]) -> list[dict[str, object]]:
    research = facts.get("missing_fact_research")
    if not isinstance(research, dict):
        return []
    items = research.get("items")
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def _property_missing_fact_item(facts: dict[str, object], field: str) -> dict[str, object]:
    normalized = str(field or "").strip()
    for item in _property_missing_fact_items(facts):
        if str(item.get("field") or "").strip() == normalized:
            return item
    return {}


def _property_rooms_research_relevant(preferences: dict[str, object]) -> bool:
    raw_value = preferences.get("min_rooms")
    if raw_value in (None, "", [], {}, False):
        return False
    try:
        return float(raw_value) > 0
    except Exception:
        return False


def _property_rooms_display(facts: dict[str, object]) -> str:
    label = str(facts.get("rooms_label") or "").strip()
    if "under research" in label.lower():
        return ""
    if label:
        return label
    raw_value = facts.get("rooms") or facts.get("room_count")
    if raw_value not in (None, "", []):
        localized_value = _property_market_decimal_display(raw_value, facts=facts)
        return f"{localized_value or raw_value} rooms"
    item = _property_missing_fact_item(facts, "rooms")
    if item:
        display_value = str(item.get("display_value") or "").strip()
        if "under research" in display_value.lower():
            return ""
        return display_value
    return ""


def _property_fact_rows(facts: dict[str, object]) -> list[dict[str, str]]:
    currency_code = _property_currency_code_from_facts(facts)
    labels = {
        "price_eur": "Price",
        "warm_rent_eur": "Warm rent",
        "cold_rent_eur": "Cold rent",
        "area_m2": "Area",
        "rooms": "Rooms",
        "bedrooms": "Bedrooms",
        "bathrooms": "Bathrooms",
        "floor": "Floor",
        "has_lift": "Lift",
        "heating_type": "Heating",
        "energy_class": "Energy class",
        "distance_supermarket_m": "Supermarket",
        "distance_playground_m": "Playground",
        "nearest_playground_m": "Playground",
        "nearest_library_m": "Library",
        "nearest_zoo_m": "Zoo",
        "distance_pharmacy_m": "Pharmacy",
        "nearest_pharmacy_m": "Pharmacy",
        "nearest_market_m": "Market",
        "nearest_hardware_store_m": "Baumarkt",
        "nearest_shopping_center_m": "Shopping center",
        "nearest_shopping_street_m": "Flaniermeile",
        "nearest_theatre_m": "Theatre",
        "nearest_public_pool_m": "Public pool",
        "nearest_medical_care_m": "Medical care",
        "distance_underground_m": "Underground",
        "nearest_subway_m": "Underground",
        "nearest_supermarket_m": "Supermarket",
        "nearest_tram_bus_m": "Straßenbahn / Bus",
        "address": "Address",
    }
    rows: list[dict[str, str]] = []
    for key, label in labels.items():
        value = facts.get(key)
        if value in (None, "", []):
            continue
        text = str(value).strip()
        if key.endswith("_eur"):
            localized_money = _property_market_currency_display(
                value,
                facts=facts,
                currency_code=currency_code,
            )
            if localized_money:
                text = localized_money
            else:
                try:
                    text = _property_money_amount_label(float(str(value).replace(",", "").strip()), currency_code=currency_code)
                except Exception:
                    text = f"{currency_code} {text}"
        elif key.endswith("_m"):
            localized_value = _property_market_decimal_display(value, facts=facts)
            text = f"{localized_value or text} m"
        elif key == "area_m2":
            localized_value = _property_market_decimal_display(value, facts=facts)
            text = f"{localized_value} m²" if localized_value else f"{text} m2"
        elif key in {"rooms", "bedrooms", "bathrooms", "floor"}:
            localized_value = _property_market_decimal_display(value, facts=facts)
            text = localized_value or text
        elif isinstance(value, bool):
            text = "Yes" if value else "No"
        rows.append(_object_detail_row(label, text, "Fact"))
    return rows


def _property_distance_metric(facts: dict[str, object], *keys: str) -> int | None:
    for key in keys:
        raw_value = facts.get(key)
        if raw_value in (None, "", []):
            continue
        try:
            meters = int(float(raw_value))
        except Exception:
            continue
        if meters > 0:
            return meters
    return None


def _property_bike_minutes_label(meters: int) -> str:
    minutes = max(1, int(round(float(meters) / 330.0)))
    return f"about {minutes} min by bike"


def _property_family_context_active(preferences: dict[str, object]) -> bool:
    if bool(preferences.get("enable_family_mode")):
        return True
    school_stage_preferences = preferences.get("school_stage_preferences")
    if isinstance(school_stage_preferences, (list, tuple, set)) and any(str(item).strip() for item in school_stage_preferences):
        return True
    keywords = {
        str(value).strip().lower()
        for value in str(preferences.get("keywords") or "").split(",")
        if str(value).strip()
    }
    return bool(
        {"family", "playground nearby", "library nearby", "public pool nearby", "medical care nearby"} & keywords
    )


def _property_distance_ooda_rows(facts: dict[str, object]) -> list[dict[str, str]]:
    return _property_distance_ooda_rows_for_preferences(facts, {})


def _property_distance_ooda_rows_for_preferences(
    facts: dict[str, object],
    preferences: dict[str, object],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    family_context = _property_family_context_active(preferences)
    distance_specs = (
        ("Playground", ("distance_playground_m", "nearest_playground_m"), "Family", "walking", True),
        ("Library", ("nearest_library_m",), "Family", "walking", True),
        ("Zoo", ("nearest_zoo_m",), "Family", "bicycling", True),
        ("Pharmacy", ("distance_pharmacy_m", "nearest_pharmacy_m"), "Errands", "walking"),
        ("Medical care", ("nearest_medical_care_m",), "Family", "walking", True),
        ("Market", ("nearest_market_m",), "District life", "walking"),
        ("Baumarkt", ("nearest_hardware_store_m",), "Practical", "bicycling"),
        ("Shopping center", ("nearest_shopping_center_m",), "Errands", "bicycling"),
        ("Flaniermeile", ("nearest_shopping_street_m",), "City life", "walking"),
        ("Theatre", ("nearest_theatre_m",), "Culture", "walking"),
        ("Public pool", ("nearest_public_pool_m",), "Family", "bicycling", True),
        ("Run or green space", ("nearest_running_m",), "Daily life", "walking"),
        ("Supermarket", ("distance_supermarket_m", "nearest_supermarket_m"), "Errands", "walking"),
        ("Straßenbahn / Bus", ("nearest_tram_bus_m", "nearest_transit_m"), "Transit", "walking"),
        ("Underground", ("distance_underground_m", "nearest_subway_m"), "Transit", "walking"),
    )
    normalized_distance_specs: tuple[tuple[str, tuple[str, ...], str, str, bool], ...] = tuple(
        item if len(item) == 5 else (item[0], item[1], item[2], item[3], False)
        for item in distance_specs
    )
    for label, keys, tag, travelmode, family_only in normalized_distance_specs:
        if family_only and not family_context:
            continue
        meters = _property_distance_metric(facts, *keys)
        if meters is None:
            continue
        rows.append(
            _object_detail_row(
                f"Nearest {label.lower()}",
                f"{meters:,} m away | {_property_bike_minutes_label(meters)}".replace(",", " "),
                tag,
            )
        )
    return rows


def _property_truthy_media_flag(value: object) -> bool:
    if value is True:
        return True
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _property_tour_requestability(
    candidate: dict[str, object],
    *,
    raw_tour_payload: dict[str, object] | None = None,
    principal_id: str = "",
) -> dict[str, object]:
    raw_tour = dict(raw_tour_payload or {})
    facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
    status = str(candidate.get("tour_status") or raw_tour.get("status") or "").strip().lower()
    raw_reason = str(
        candidate.get("blocked_reason")
        or candidate.get("tour_reason")
        or raw_tour.get("reason_key")
        or raw_tour.get("blocked_reason")
        or raw_tour.get("reason")
        or ""
    ).strip()
    reason_key = _property_visual_reason_key(raw_reason)
    terminal_status = _property_visual_terminal_status_for_reason(
        request_kind="tour",
        reason=raw_reason,
    )
    if terminal_status and status in {"", *_PROPERTY_TOUR_PENDING_STATES}:
        status = terminal_status
    tour_url = str(
        candidate.get("tour_url")
        or raw_tour.get("url")
        or raw_tour.get("embed_url")
        or ""
    ).strip()
    verified_tour = bool(
        _property_tour_verified_open_url(tour_url, principal_id=principal_id)
        if tour_url
        else ""
    )
    vendor_tour = bool(
        _customer_facing_vendor_tour_url(
            candidate.get("vendor_tour_url") or raw_tour.get("provider_url"),
            principal_id=principal_id,
        )
    )
    source_virtual_tour_url = _safe_provider_live_360_url(
        _property_candidate_source_virtual_tour_url(candidate, facts=facts)
    )
    floorplan_url = _property_candidate_floorplan_url(candidate, facts=facts)
    has_floorplan = bool(floorplan_url)
    has_real_360 = bool(source_virtual_tour_url)
    source_eligible = bool(verified_tour or vendor_tour or has_floorplan or has_real_360)
    listing_state = str(
        candidate.get("listing_status")
        or candidate.get("source_status")
        or facts.get("listing_status")
        or facts.get("source_status")
        or ""
    ).strip().lower()
    listing_expired = reason_key == "listing_expired" or listing_state in {
        "expired",
        "gone",
        "inactive",
        "removed",
        "unavailable",
    }
    terminal_source_gap = bool(
        not source_eligible
        and (listing_expired or reason_key in _PROPERTY_TOUR_TERMINAL_SOURCE_GAP_REASONS)
    )
    tour_requestable = source_eligible
    return {
        "tour_status": status,
        "tour_reason_key": reason_key,
        "tour_source_eligible": source_eligible,
        "tour_requestable": tour_requestable,
        "tour_terminal_source_gap": terminal_source_gap,
    }


def _property_tour_source_gap_detail(candidate: dict[str, object]) -> str:
    raw_tour = dict(candidate.get("tour") or {}) if isinstance(candidate.get("tour"), dict) else {}
    facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
    blocked_reason = _property_visual_reason_key(
        candidate.get("blocked_reason"),
        candidate.get("tour_reason"),
        raw_tour.get("reason_key"),
        raw_tour.get("blocked_reason"),
        raw_tour.get("reason"),
    )
    captured_source_available = bool(
        _property_tour_requestability(candidate, raw_tour_payload=raw_tour).get(
            "tour_source_eligible"
        )
    )
    if blocked_reason:
        if captured_source_available and blocked_reason in _PROPERTY_TOUR_TERMINAL_SOURCE_GAP_REASONS:
            return "A real 3D tour is not available yet, but captured source media can support a retry."
        reason_map = {
            "listing_expired": (
                "The source listing has expired. No floor plan or real 360 source was captured, "
                "so the cached preview cannot become a 3D tour."
            ),
            "listing_360_media_missing": (
                "No floor plan or real 360 source was captured. "
                "Flat listing photos alone cannot be presented as a 3D tour."
            ),
            "pure_360_assets_unavailable": (
                "No verified 360 source is available. Flat listing photos alone cannot be presented as a 3D tour."
            ),
            "floorplan_assets_unavailable": "A usable floor plan was not captured for this listing.",
            "floorplan_missing": "A usable floor plan was not captured for this listing.",
            "provider_export_missing": "A verified 3D-tour export has not been provided for this listing.",
            "property_tour_fallback_disabled": "A real 3D tour needs a floor plan or verified 360 source.",
            "property_tour_rebuild_required": "A real 3D tour is not available for this listing yet.",
            "property_tour_execution_failed": (
                "The 3D tour could not be built from the available source media. You can try again."
            ),
            "property_tour_delivery_failed": "The 3D tour could not be published. You can try again.",
            "crezlo_property_tour_not_configured": "The 3D-tour renderer is temporarily unavailable.",
        }
        if blocked_reason in reason_map:
            return reason_map[blocked_reason]

    def _false_flag(value: object) -> bool:
        return str(value or "").strip().lower() in {"0", "false", "no", "none", "null"}

    def _zero_count(*keys: str) -> bool:
        for key in keys:
            raw_value = facts.get(key)
            if raw_value in (None, ""):
                continue
            try:
                return float(str(raw_value).strip()) <= 0.0
            except Exception:
                continue
        return False

    if _property_truthy_media_flag(facts.get("has_floorplan")) or _property_truthy_media_flag(facts.get("has_360")):
        return "A real 3D tour is not available for this listing yet."
    if _false_flag(facts.get("has_floorplan")) or _zero_count("floorplan_count", "floorplans_count"):
        return "No floor plan or real 360 source was captured. Flat listing photos alone cannot be presented as a 3D tour."
    if _false_flag(facts.get("has_360")) or _zero_count("media_count", "image_count"):
        return "No verified 360 source was captured for this listing."
    return "A real 3D tour needs a floor plan or verified 360 source."


def _property_tour_retired_control_target(value: object) -> bool:
    """Identify retired first-party controls that must never reach a customer."""

    normalized = str(value or "").strip()
    if not normalized:
        return False
    try:
        parsed = urllib.parse.urlsplit(normalized)
    except (TypeError, ValueError):
        return True
    path = str(parsed.path or "")
    for _ in range(4):
        decoded = urllib.parse.unquote(path)
        if decoded == path:
            break
        path = decoded
    path = path.replace("\\", "/")
    normalized_parts: list[str] = []
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if normalized_parts:
                normalized_parts.pop()
            continue
        normalized_parts.append(part.lower())
    return normalized_parts[-2:] == ["control", "matterport"]


def _property_tour_verified_open_url(tour_url: object, *, principal_id: str = "") -> str:
    normalized_principal_id = str(principal_id or "").strip()
    resolved_url = (
        property_tour_hosting._hosted_property_tour_verified_open_url(
            tour_url,
            principal_id=normalized_principal_id,
        )
        if normalized_principal_id
        else property_tour_hosting._hosted_property_tour_verified_open_url(tour_url)
    )
    normalized_url = str(resolved_url or "").strip()
    if _property_tour_retired_control_target(normalized_url):
        return ""
    return normalized_url


def _property_tour_verified_provider(tour_url: object, *, principal_id: str = "") -> str:
    normalized_principal_id = str(principal_id or "").strip()
    resolved_provider = (
        property_tour_hosting._hosted_property_tour_verified_provider(
            tour_url,
            principal_id=normalized_principal_id,
        )
        if normalized_principal_id
        else property_tour_hosting._hosted_property_tour_verified_provider(tour_url)
    )
    return str(resolved_provider or "").strip()


def _property_tour_first_party_open_url(tour_url: object, *, principal_id: str = "") -> str:
    normalized_principal_id = str(principal_id or "").strip()
    resolved_url = (
        property_tour_hosting._hosted_property_tour_first_party_open_url(
            tour_url,
            principal_id=normalized_principal_id,
        )
        if normalized_principal_id
        else property_tour_hosting._hosted_property_tour_first_party_open_url(tour_url)
    )
    normalized_url = str(resolved_url or "").strip()
    if _property_tour_retired_control_target(normalized_url):
        return ""
    return normalized_url


def _property_ai_360_control_href(
    source_url: object,
    open_url: object,
) -> str:
    """Return a relative control route only for one canonical first-party tour."""

    def _tour_slug(value: object) -> str:
        raw = str(value or "").strip()
        if not raw or raw.startswith("//") or any(character.isspace() for character in raw):
            return ""
        try:
            parsed = urllib.parse.urlsplit(raw)
            parsed_port = parsed.port
        except (TypeError, ValueError):
            return ""
        if parsed.query or parsed.fragment:
            return ""
        if parsed.scheme or parsed.netloc:
            if (
                parsed.scheme.lower() != "https"
                or str(parsed.hostname or "").lower() not in _PROPERTY_AI_360_PUBLIC_HOSTS
                or parsed.username
                or parsed.password
                or parsed_port not in (None, 443)
            ):
                return ""
        elif not raw.startswith("/"):
            return ""
        path = str(parsed.path or "")
        if urllib.parse.unquote(path) != path:
            return ""
        match = re.fullmatch(
            r"/tours/(?P<slug>[A-Za-z0-9][A-Za-z0-9._-]{0,127})(?:/control)?/?",
            path,
        )
        if not match:
            return ""
        slug = str(match.group("slug") or "")
        if not _PROPERTY_AI_360_SLUG_RE.fullmatch(slug) or slug in {".", ".."} or ".." in slug:
            return ""
        return slug

    source_slug = _tour_slug(source_url)
    open_slug = _tour_slug(open_url)
    if not source_slug or open_slug != source_slug:
        return ""
    return f"/tours/{source_slug}/control"


def _property_hosted_tour_ready(tour_url: str, *, principal_id: str = "") -> bool:
    return bool(_property_tour_verified_open_url(tour_url, principal_id=principal_id))


def _property_hosted_tour_disabled_fallback(tour_url: object) -> bool:
    payload = property_tour_hosting._hosted_property_tour_payload_for_url(tour_url)
    return bool(payload and property_tour_hosting._property_tour_payload_is_disabled_fallback(payload))


def _hosted_tour_rebuild_detail() -> str:
    return "A real 3D tour is not available for this listing yet."


def _property_visual_provider_label(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    label_map = {
        "matterport": "3D tour",
        "3dvista": "3D tour",
        "threedvista": "3D tour",
        "three_d_vista": "3D tour",
        "pano2vr": "3D tour",
        "pano_2_vr": "3D tour",
        "krpano": "3D tour",
        "magicfit": "Walkthrough",
        "mootion": "Walkthrough",
        "omagic": "Walkthrough",
        "magic": "Walkthrough",
        "ea_one_manager_onemin_i2v": "Walkthrough",
        "onemin_i2v": "Walkthrough",
        "poppy_ai": "Walkthrough",
    }
    if normalized in label_map:
        return label_map[normalized]
    return "3D tour" if normalized else ""


def _customer_facing_vendor_tour_url(value: object, *, principal_id: str = "") -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    verified_open_url = _property_tour_verified_open_url(normalized, principal_id=principal_id)
    verified_provider = _property_tour_verified_provider(normalized, principal_id=principal_id).lower()
    if verified_open_url and verified_provider in {"matterport", "3dvista"}:
        return verified_open_url
    return ""


def _property_tour_media_payload(
    candidate: dict[str, object],
    *,
    principal_id: str = "",
) -> dict[str, object]:
    normalized_principal_id = str(principal_id or "").strip()
    raw_tour_payload = dict(candidate.get("tour") or {}) if isinstance(candidate.get("tour"), dict) else {}
    tour_url = str(
        candidate.get("tour_url")
        or raw_tour_payload.get("url")
        or raw_tour_payload.get("embed_url")
        or ""
    ).strip()
    untrusted_tour_target = bool(
        tour_url
        and not property_tour_hosting._hosted_property_tour_slug_from_url(tour_url)
    )
    if untrusted_tour_target:
        tour_url = ""
    disabled_fallback_tour = bool(
        tour_url and _property_hosted_tour_disabled_fallback(tour_url)
    )
    if disabled_fallback_tour:
        tour_url = ""
    vendor_tour_url = _customer_facing_vendor_tour_url(
        candidate.get("vendor_tour_url") or raw_tour_payload.get("provider_url"),
        principal_id=normalized_principal_id,
    )
    review_url = str(candidate.get("review_url") or "").strip()
    requestability = _property_tour_requestability(
        candidate,
        raw_tour_payload=raw_tour_payload,
        principal_id=normalized_principal_id,
    )
    status = str(requestability.get("tour_status") or "").strip().lower()
    if disabled_fallback_tour:
        status = ""
    reason_key = str(requestability.get("tour_reason_key") or "").strip()
    classification_reason = str(
        candidate.get("blocked_reason")
        or candidate.get("tour_reason")
        or raw_tour_payload.get("reason_key")
        or raw_tour_payload.get("blocked_reason")
        or raw_tour_payload.get("reason")
        or ""
    ).strip()
    terminal_status = _property_visual_terminal_status_for_reason(
        request_kind="tour",
        reason=classification_reason,
    )
    if terminal_status and not tour_url and status in {"", *_PROPERTY_TOUR_PENDING_STATES}:
        status = terminal_status
    eta_raw = str(candidate.get("tour_eta_minutes") or raw_tour_payload.get("eta_minutes") or "").strip()
    requested_at = str(candidate.get("tour_requested_at") or "").strip()
    status_updated_at = str(candidate.get("tour_status_updated_at") or "").strip()
    eta_minutes = 0
    if eta_raw:
        try:
            eta_minutes = int(float(eta_raw))
        except Exception:
            eta_minutes = 0
    eta_label = _property_visual_eta_label(
        request_kind="tour",
        status=status,
        eta_minutes=eta_minutes if eta_minutes > 0 else "",
        requested_at=requested_at,
        status_updated_at=status_updated_at,
    )
    try:
        tour_progress_pct = int(float(str(
            candidate.get("tour_progress_pct")
            or raw_tour_payload.get("progress_pct")
            or 0
        ).strip()))
    except (TypeError, ValueError):
        tour_progress_pct = 0
    tour_progress_pct = max(0, min(100, tour_progress_pct))
    hosted_tour_ready = _property_hosted_tour_ready(
        tour_url,
        principal_id=normalized_principal_id,
    )
    verified_tour_href = (
        _property_tour_verified_open_url(tour_url, principal_id=normalized_principal_id)
        if hosted_tour_ready
        else ""
    )
    verified_tour_provider = (
        _property_tour_verified_provider(tour_url, principal_id=normalized_principal_id)
        if hosted_tour_ready
        else ""
    )
    generated_reconstruction_source_url = str(candidate.get("generated_reconstruction_url") or "").strip()
    generated_reconstruction_kind = (
        property_tour_hosting._hosted_property_tour_reconstruction_kind(
            generated_reconstruction_source_url,
            principal_id=normalized_principal_id,
        )
        if generated_reconstruction_source_url
        else ""
    )
    if (
        generated_reconstruction_source_url
        and not generated_reconstruction_kind
        and property_tour_hosting._hosted_property_tour_generated_reconstruction_bundle_ready(
            generated_reconstruction_source_url
        )
    ):
        generated_reconstruction_kind = "layout_preview"
    if (
        not generated_reconstruction_source_url
        and tour_url
    ):
        generated_reconstruction_kind = (
            property_tour_hosting._hosted_property_tour_reconstruction_kind(
                tour_url,
                principal_id=normalized_principal_id,
            )
        )
        if (
            not generated_reconstruction_kind
            and property_tour_hosting._hosted_property_tour_generated_reconstruction_bundle_ready(
                tour_url
            )
        ):
            generated_reconstruction_kind = "layout_preview"
        if generated_reconstruction_kind:
            generated_reconstruction_source_url = tour_url
    generated_reconstruction_href = ""
    if generated_reconstruction_source_url and not _property_hosted_tour_disabled_fallback(generated_reconstruction_source_url):
        generated_reconstruction_href = _property_tour_first_party_open_url(
            generated_reconstruction_source_url,
            principal_id=normalized_principal_id,
        )
    ai_360_embed_href = ""
    if generated_reconstruction_kind == "ai_panorama_360":
        ai_360_embed_href = _property_ai_360_control_href(
            generated_reconstruction_source_url,
            generated_reconstruction_href,
        )
        generated_reconstruction_href = ai_360_embed_href
    generated_reconstruction_ready = bool(generated_reconstruction_href)
    ai_360_ready = bool(ai_360_embed_href)
    open_tour_href = verified_tour_href
    embed_href = ai_360_embed_href or (verified_tour_href if hosted_tour_ready else "")
    unbacked_available_status = bool(
        status
        in {
            "available",
            "complete",
            "completed",
            "published",
            "ready",
            "source",
            "succeeded",
            "success",
        }
        and not (open_tour_href or generated_reconstruction_href or vendor_tour_url)
    )
    if unbacked_available_status:
        status = "unavailable"
    verified_walkthrough_href = property_tour_hosting._hosted_property_tour_walkthrough_open_url(
        tour_url,
        candidate.get("flythrough_url"),
    )
    walkthrough_ready = bool(verified_walkthrough_href)
    walkthrough_status = (
        ""
        if untrusted_tour_target
        else str(candidate.get("flythrough_status") or "").strip().lower()
    )
    walkthrough_reason = str(candidate.get("flythrough_reason") or "").strip()
    live_walkthrough_progress = _hosted_property_visual_progress_snapshot(
        tour_url,
        request_kind="flythrough",
    ) if tour_url else {}
    live_walkthrough_detail = str(live_walkthrough_progress.get("detail") or "").strip()
    terminal_walkthrough_status = _property_visual_terminal_status_for_reason(
        request_kind="flythrough",
        reason=walkthrough_reason,
    )
    if (
        terminal_walkthrough_status
        and not walkthrough_ready
        and walkthrough_status in {"", "queued", "pending", "processing", "running", "in_progress", "started", "rendering", "repairing"}
    ):
        walkthrough_status = terminal_walkthrough_status
    elif (
        str(live_walkthrough_progress.get("status") or "").strip().lower()
        and not walkthrough_ready
        and walkthrough_status in {"", "queued", "pending", "processing", "running", "in_progress", "started", "rendering", "repairing"}
    ):
        walkthrough_status = str(live_walkthrough_progress.get("status") or "").strip().lower()
    walkthrough_eta_raw = str(candidate.get("flythrough_eta_minutes") or "").strip()
    walkthrough_requested_at = str(candidate.get("flythrough_requested_at") or "").strip()
    walkthrough_status_updated_at = str(candidate.get("flythrough_status_updated_at") or "").strip()
    walkthrough_eta_label = _property_visual_eta_label(
        request_kind="flythrough",
        status=walkthrough_status,
        eta_minutes=walkthrough_eta_raw,
        requested_at=walkthrough_requested_at,
        status_updated_at=walkthrough_status_updated_at,
    )
    try:
        walkthrough_progress_pct = int(float(str(candidate.get("flythrough_progress_pct") or "").strip()))
    except (TypeError, ValueError):
        walkthrough_progress_pct = 0
    if walkthrough_progress_pct <= 0:
        walkthrough_progress_pct = _property_visual_progress_pct(
            request_kind="flythrough",
            status=walkthrough_status,
            ready_url=verified_walkthrough_href,
            eta_minutes=walkthrough_eta_raw,
            requested_at=walkthrough_requested_at,
            status_updated_at=walkthrough_status_updated_at,
        )
    try:
        live_walkthrough_progress_pct = int(float(str(live_walkthrough_progress.get("progress_pct") or "").strip()))
    except (TypeError, ValueError):
        live_walkthrough_progress_pct = 0
    if (
        live_walkthrough_progress_pct > 0
        and not walkthrough_ready
        and walkthrough_status in _PROPERTY_TOUR_PENDING_STATES
    ):
        walkthrough_progress_pct = live_walkthrough_progress_pct
        walkthrough_eta_label = _hosted_property_visual_progress_stage_label(live_walkthrough_progress) or walkthrough_eta_label
    walkthrough_progress_pct = max(0, min(100, walkthrough_progress_pct))
    vendor_tour_provider = (
        _property_tour_verified_provider(vendor_tour_url, principal_id=normalized_principal_id)
        if vendor_tour_url
        else ""
    )
    walkthrough_provider = str(candidate.get("flythrough_provider") or "").strip()
    if hosted_tour_ready:
        status_label = "3D tour available"
        status_detail = "3D tour is ready."
    elif ai_360_ready:
        status_label = "AI 360 tour available"
        status_detail = _PROPERTY_AI_360_DISCLOSURE
    elif generated_reconstruction_ready:
        status_label = "AI-generated 3D tour available"
        status_detail = (
            "AI-generated from the floor plan and listing photos. "
            "It is an interactive spatial aid, not a captured 360° tour."
        )
    elif tour_url:
        status_label = "3D tour unavailable"
        status_detail = _hosted_tour_rebuild_detail()
    elif vendor_tour_url:
        status_label = "Original tour available"
        status_detail = "The original tour is available. Open it directly while the in-page 3D tour is still missing."
    elif disabled_fallback_tour:
        status_label = "3D tour unavailable"
        status_detail = _property_tour_source_gap_detail(candidate)
    elif status in {"queued", "pending"}:
        status_label = "3D tour queued"
        status_detail = (
            "Still queued."
            if eta_label.startswith("delayed")
            else "Queued."
        )
    elif status in {"processing", "running", "in_progress", "started", "rendering", "repairing"}:
        status_label = "3D tour rendering"
        status_detail = (
            "Still rendering."
            if eta_label.startswith("delayed")
            else "Rendering."
        )
    elif status in {"blocked", "failed", "skipped", "not_applicable"}:
        status_label = "3D tour unavailable"
        status_detail = _property_tour_source_gap_detail(candidate)
    else:
        status_label = "3D tour unavailable"
        status_detail = _property_tour_source_gap_detail(candidate)
    return {
        "status_label": status_label,
        "status_detail": status_detail,
        "tour_status": status,
        "tour_reason_key": reason_key,
        "tour_source_eligible": bool(requestability.get("tour_source_eligible")),
        "tour_requestable": bool(requestability.get("tour_requestable")),
        "tour_terminal_source_gap": bool(requestability.get("tour_terminal_source_gap")),
        "tour_eta_label": eta_label,
        "tour_progress_pct": tour_progress_pct,
        "embed_href": embed_href,
        "canonical_tour_url": verified_tour_href,
        "has_live_viewer": bool(embed_href),
        "hosted_ready": hosted_tour_ready,
        "vendor_ready": bool(vendor_tour_url),
        "vendor_tour_href": vendor_tour_url,
        "generated_reconstruction_ready": generated_reconstruction_ready,
        "generated_reconstruction_href": generated_reconstruction_href,
        "generated_reconstruction_kind": generated_reconstruction_kind,
        "ai_360_ready": ai_360_ready,
        "ai_360_disclosure": (
            _PROPERTY_AI_360_DISCLOSURE
            if ai_360_ready
            else ""
        ),
        "generated_reconstruction_label": (
            "Open AI 360 tour"
            if ai_360_ready
            else ("Open AI-generated 3D tour" if generated_reconstruction_ready else "")
        ),
        "generated_reconstruction_status_detail": (
            _PROPERTY_AI_360_DISCLOSURE
            if ai_360_ready
            else (
                "AI-generated from the floor plan and listing photos. "
                "It is an interactive spatial aid, not a captured 360° tour."
            )
            if generated_reconstruction_ready
            else ""
        ),
        "show_status_line": bool(
            hosted_tour_ready
            or generated_reconstruction_ready
            or tour_url
            or vendor_tour_url
            or status in _PROPERTY_TOUR_PENDING_STATES
            or status in _PROPERTY_TOUR_TERMINAL_STATES
            or status == "unavailable"
        ),
        "primary_href": open_tour_href or generated_reconstruction_href or (vendor_tour_url or review_url),
        "primary_label": (
            "Open 3D tour"
            if open_tour_href
            else (
                "Open AI-generated 3D tour"
                if generated_reconstruction_href and not ai_360_ready
                else (
                    "Open AI 360 tour"
                    if generated_reconstruction_href
                    else ("Open original tour" if vendor_tour_url else ("Open property" if review_url else ""))
                )
            )
        ),
        "secondary_href": review_url,
        "secondary_label": "Open property" if review_url else "",
        "tertiary_href": vendor_tour_url if hosted_tour_ready and vendor_tour_url and vendor_tour_url != tour_url else "",
        "tertiary_label": "Open original tour" if hosted_tour_ready and vendor_tour_url and vendor_tour_url != tour_url else "",
        "walkthrough_href": verified_walkthrough_href,
        "provider_label": (
            "3D tour"
            if open_tour_href or vendor_tour_url
            else (
                "AI 360 reconstruction"
                if ai_360_ready
                else ("AI-generated 3D tour" if generated_reconstruction_href else "")
            )
        ),
        "provider_key": (
            verified_tour_provider
            or vendor_tour_provider
            or (
                "propertyquarry_ai_360"
                if ai_360_ready
                else ("propertyquarry_generated_reconstruction" if generated_reconstruction_ready else "")
            )
        ),
        "walkthrough_provider_label": "Walkthrough" if walkthrough_provider or walkthrough_ready else "",
        "walkthrough_provider_key": walkthrough_provider,
        "walkthrough_status_detail": (
            "Walkthrough is ready."
            if walkthrough_ready
            else (
                live_walkthrough_detail
                if live_walkthrough_detail and walkthrough_status in {"queued", "pending", "processing", "running", "in_progress", "started", "rendering", "repairing", "blocked", "failed", "skipped", "not_applicable"}
                else (
                (
                    "Queued."
                    if walkthrough_status in {"queued", "pending"}
                    else (
                        "Rendering."
                        if walkthrough_status in {"processing", "running", "in_progress", "started", "rendering"}
                        else ""
                    )
                )
                if walkthrough_status in {"queued", "pending", "processing", "running", "in_progress", "started", "rendering"}
                else (
                _property_visual_unavailable_detail(request_kind="flythrough", reason=walkthrough_reason)
                if walkthrough_status in {"blocked", "failed", "skipped", "not_applicable"}
                else ""
                )
                )
            )
        ),
        "walkthrough_status": walkthrough_status,
        "walkthrough_progress_pct": walkthrough_progress_pct,
        "walkthrough_eta_label": walkthrough_eta_label,
    }


def _property_tour_detail_line(
    candidate: dict[str, object],
    *,
    principal_id: str = "",
) -> str:
    normalized_principal_id = str(principal_id or "").strip()
    tour_url = str(candidate.get("tour_url") or "").strip()
    vendor_tour_url = _customer_facing_vendor_tour_url(
        candidate.get("vendor_tour_url"),
        principal_id=normalized_principal_id,
    )
    first_party_open_url = _property_tour_first_party_open_url(
        tour_url,
        principal_id=normalized_principal_id,
    )
    if str(first_party_open_url or "").strip():
        if (
            property_tour_hosting._hosted_property_tour_reconstruction_kind(
                tour_url,
                principal_id=normalized_principal_id,
            )
            == "ai_panorama_360"
        ):
            return "Open the AI 360 reconstruction on PropertyQuarry. It is photo-derived, not a captured survey."
        if property_tour_hosting._hosted_property_tour_generated_reconstruction_bundle_ready(tour_url):
            return "Open the layout preview on PropertyQuarry. Captured 3D media is not available yet."
        return "Open the 3D tour on PropertyQuarry."
    if vendor_tour_url:
        return "An original tour exists, but the in-page 3D tour is not ready yet."
    return _property_tour_source_gap_detail(candidate)


def _property_research_money_display(value: object, *, currency_code: str = "EUR") -> str:
    resolved_currency_code = str(currency_code or "EUR").strip().upper() or "EUR"
    if value in (None, "", []):
        return ""
    if isinstance(value, str):
        text = " ".join(value.split()).strip()
        if not text:
            return ""
        supported_pattern = "|".join(re.escape(code) for code in supported_currency_codes())
        code_match = re.search(rf"\b({supported_pattern})\b", text, flags=re.IGNORECASE)
        currency = str(code_match.group(1) or "").upper() if code_match else ("EUR" if "€" in text else ("GBP" if "£" in text else ""))
        money_match = re.search(r"[0-9][0-9\.\,\s]*(?:[,.][0-9]{1,2})?", text)
        if currency and money_match:
            number_text = money_match.group(0).replace(" ", "").strip(".,")
            if "." in number_text and "," in number_text:
                number_text = number_text.replace(".", "").replace(",", ".")
            elif "," in number_text:
                integer_part, decimal_part = number_text.rsplit(",", 1)
                number_text = integer_part + decimal_part if len(decimal_part) == 3 else integer_part + "." + decimal_part
            elif number_text.count(".") > 1:
                number_text = number_text.replace(".", "")
            try:
                amount = float(number_text)
                if amount > 0:
                    return f"{currency} {amount:,.0f}".replace(",", ",")
            except Exception:
                return text
        if currency:
            return text
        try:
            value = float(text.replace(",", "."))
        except Exception:
            return text
    if isinstance(value, (int, float)):
        amount = float(value)
        if amount <= 0:
            return ""
        return f"{resolved_currency_code} {amount:,.0f}".replace(",", ",")
    return ""


def _google_maps_embed_url(map_url: object) -> str:
    normalized = str(map_url or "").strip()
    if not normalized:
        return ""
    parsed = urllib.parse.urlparse(normalized)
    query = urllib.parse.parse_qs(parsed.query or "")
    if parsed.netloc.endswith("google.com") and parsed.path.startswith("/maps/search"):
        raw_query = next(iter(query.get("query") or []), "").strip()
        if raw_query:
            return f"https://www.google.com/maps?q={urllib.parse.quote(raw_query, safe=',')}&output=embed"
    return ""


def _property_research_gallery_items(
    *,
    candidate: dict[str, object],
    facts: dict[str, object],
    preview_image: str,
    latest_magic_fit_scene: dict[str, object] | None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    def _append(url: object, *, label: str, kind: str) -> None:
        normalized = str(url or "").strip()
        if not normalized or normalized in seen:
            return
        if not normalized.startswith(("https://", "http://", "/")):
            return
        seen.add(normalized)
        rows.append({"url": normalized, "label": label, "kind": kind})

    _append(preview_image, label="Lead photo", kind="photo")
    for key in ("media_urls_json", "photo_urls_json", "image_urls_json"):
        values = facts.get(key) or candidate.get(key)
        if not isinstance(values, (list, tuple)):
            continue
        for index, value in enumerate(values[:8], start=1):
            _append(value, label=f"Photo {index}", kind="photo")

    if isinstance(latest_magic_fit_scene, dict):
        _append(
            latest_magic_fit_scene.get("image_url"),
            label=str(latest_magic_fit_scene.get("scene_type") or "Lifestyle still").replace("_", " ").title(),
            kind="diorama",
        )
    return rows[:8]


def _property_review_detail_line(candidate: dict[str, object]) -> str:
    review_url = str(candidate.get("review_url") or "").strip()
    if review_url:
        return "Open the home."
    return "No property page yet."


def _property_packet_provenance_rows(facts: dict[str, object]) -> list[dict[str, str]]:
    labels = {
        "street_address": "Address",
        "exact_address": "Exact address",
        "address": "Address",
        "has_lift": "Lift",
        "heating_type": "Heating",
        "energy_class": "Energy class",
        "distance_supermarket_m": "Supermarket",
        "nearest_supermarket_m": "Supermarket",
        "distance_playground_m": "Playground",
        "nearest_playground_m": "Playground",
        "distance_pharmacy_m": "Pharmacy",
        "nearest_pharmacy_m": "Pharmacy",
        "distance_underground_m": "Underground",
        "nearest_subway_m": "Underground",
    }
    research_snapshot = dict(facts.get("listing_research_snapshot") or {}) if isinstance(facts.get("listing_research_snapshot"), dict) else {}
    research_meta = dict(facts.get("listing_research_meta") or {}) if isinstance(facts.get("listing_research_meta"), dict) else {}
    rows: list[dict[str, str]] = []
    for key, label in labels.items():
        raw_value = facts.get(key)
        if raw_value in (None, "", []):
            continue
        if isinstance(raw_value, bool):
            value = "Yes" if raw_value else "No"
        elif isinstance(raw_value, (int, float)) and key.endswith("_m"):
            value = f"{int(raw_value)} m"
        else:
            value = str(raw_value).strip()
        if not value:
            continue
        provenance = "Checked" if key in research_snapshot else "Provided"
        if key in {"street_address", "exact_address", "address"} and ("map_lat" in research_snapshot or "map_lng" in research_snapshot):
            provenance = "Estimated"
        detail = value
        strategy = str(research_meta.get("strategy") or "").strip()
        if provenance == "Researched" and strategy:
            detail = f"{detail} | via {strategy.replace('_', ' ')}"
        rows.append(_object_detail_row(label, detail, provenance))
    return rows


def _property_packet_official_evidence_rows(facts: dict[str, object]) -> list[dict[str, str]]:
    official = dict(facts.get("official_risk_evidence") or {}) if isinstance(facts.get("official_risk_evidence"), dict) else {}
    rows: list[dict[str, str]] = []
    for row in list(official.get("sources") or [])[:6]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("label") or row.get("risk_key") or "Local data").strip()
        source_label = str(row.get("source_label") or row.get("provider") or "Local dataset").strip()
        authority = str(row.get("authority_label") or row.get("provider") or "").strip()
        summary = str(row.get("summary") or "").strip()
        availability = str(row.get("availability") or "official_dataset").replace("_", " ").title()
        verification = str(row.get("verification_state") or "needs_review").replace("_", " ").title()
        next_step = str(row.get("required_next_step") or "").strip()
        scope = str(row.get("coverage_scope") or "").replace("_", " ").strip()
        detail = " | ".join(part for part in (authority, source_label, summary, f"Scope: {scope}" if scope else "") if part)
        if next_step:
            detail = f"{detail} | Next: {next_step}" if detail else next_step
        rows.append(
            _object_detail_row(
                title,
                detail or "Public data is attached for this check.",
                " · ".join(part for part in (availability, verification) if part),
                href=str(row.get("source_url") or "").strip(),
            )
        )
    return rows


def _property_packet_official_posture_rows(facts: dict[str, object]) -> list[dict[str, str]]:
    official = dict(facts.get("official_risk_evidence") or {}) if isinstance(facts.get("official_risk_evidence"), dict) else {}
    rows: list[dict[str, str]] = []
    for row in _official_risk_posture_rows(official):
        rows.append(
            _object_detail_row(
                str(row.get("title") or "Area checks").strip(),
                str(row.get("detail") or "").strip() or "No public area check is attached yet.",
                str(row.get("tag") or "Open").strip() or "Open",
            )
        )
    return rows


def _property_packet_future_research_rows(facts: dict[str, object]) -> list[dict[str, str]]:
    future = dict(facts.get("future_change_research") or {}) if isinstance(facts.get("future_change_research"), dict) else {}
    rows: list[dict[str, str]] = []
    school_quality = property_school_context_summary(future)
    school_progression = str(future.get("school_atlas_progression_summary") or "").strip()
    school_evidence_type = str(future.get("school_atlas_evidence_type") or "").strip().replace("_", " ")
    school_source_url = str(future.get("school_atlas_source_url") or "").strip()
    if school_quality:
        rows.append(_object_detail_row("School context", school_quality, school_evidence_type.title() or "School data", href=school_source_url))
    if school_progression:
        rows.append(_object_detail_row("Gymnasium progression", school_progression, school_evidence_type.title() or "School data", href=school_source_url))
    selected_school = dict(future.get("school_atlas_selected_school") or {}) if isinstance(future.get("school_atlas_selected_school"), dict) else {}
    if selected_school:
        selected_label = " | ".join(
            part for part in (
                str(selected_school.get("name") or "").strip(),
                str(selected_school.get("type") or "").strip(),
                f"{int(float(selected_school.get('distance_m') or 0))} m" if selected_school.get("distance_m") not in (None, "", []) else "",
            ) if part
        )
        if selected_label:
            rows.append(_object_detail_row("Nearest selected school", selected_label, "School"))
    top_destinations = [
        str(item.get("name") or "").strip()
        for item in list(future.get("school_atlas_top_secondary_destinations") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    if top_destinations:
        rows.append(_object_detail_row("Top next schools", ", ".join(top_destinations[:3]), "Path"))
    planning_confidence = str(future.get("planning_confidence") or "").strip()
    if planning_confidence:
        rows.append(_object_detail_row("Planning clarity", planning_confidence, "Planning"))
    investment_impact = str(future.get("investment_impact") or "").strip()
    if investment_impact:
        rows.append(_object_detail_row("Long-term impact", investment_impact.replace("_", " ").title(), "Impact"))
    return rows


def _property_packet_evidence_overlay_rows(
    *,
    facts: dict[str, object],
    candidate: dict[str, object],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for overlay in build_property_evidence_overlay_rows(facts=facts, candidate=candidate):
        source_url = str(overlay.get("source_url") or "").strip()
        article_url = str(overlay.get("article_url") or "").strip()
        rows.append(
            _object_detail_row(
                str(overlay.get("title") or "Area layer").strip(),
                str(overlay.get("detail") or "No area layer is available yet.").strip(),
                str(overlay.get("tag") or "Open").strip(),
                href=article_url or source_url,
            )
        )
    return rows


def _property_packet_score_rows(
    *,
    facts: dict[str, object],
    preferences: dict[str, object],
    match_reasons: list[str],
    mismatch_reasons: list[str],
) -> list[dict[str, str]]:
    def _positive_float(value: object) -> float | None:
        try:
            parsed = float(str(value or "").strip())
        except Exception:
            return None
        return parsed if parsed > 0.0 else None

    rows: list[dict[str, str]] = []
    selected_locations = {str(value).strip().lower() for value in str(preferences.get("location_query") or "").split(",") if str(value).strip()}
    fact_address = str(facts.get("address") or facts.get("postal_name") or "").strip()
    if fact_address:
        fits_location = any(token in fact_address.lower() for token in selected_locations) if selected_locations else True
        rows.append(
            _object_detail_row(
                "Location fit",
                fact_address,
                "Strong" if fits_location and selected_locations else ("Check" if selected_locations else "Location"),
            )
        )
    budget_limit_eur = next(
        (
            amount
            for amount in (
                _positive_float(preferences.get("max_price_eur")),
                _positive_float(preferences.get("max_total_price_eur")),
                _positive_float(preferences.get("max_purchase_price_eur")),
                _positive_float(preferences.get("max_rent_eur")),
                _positive_float(preferences.get("max_total_rent_eur")),
                _positive_float(preferences.get("max_monthly_rent_eur")),
            )
            if amount is not None
        ),
        None,
    )
    price_amount_eur = _property_investment_price_eur(facts)
    if budget_limit_eur is not None and isinstance(price_amount_eur, float):
        currency_code = _property_currency_code_from_facts(facts)
        price_label = _property_money_amount_label(price_amount_eur, currency_code=currency_code)
        budget_label = _property_money_amount_label(budget_limit_eur, currency_code=currency_code)
        if price_amount_eur <= budget_limit_eur:
            rows.append(
                _object_detail_row(
                    "Budget signal",
                    f"{price_label} is within the {budget_label} budget ceiling.",
                    "Strong",
                )
            )
        else:
            over_budget_label = _property_money_amount_label(price_amount_eur - budget_limit_eur, currency_code=currency_code)
            rows.append(
                _object_detail_row(
                    "Budget signal",
                    f"{price_label} is {over_budget_label} above the {budget_label} budget ceiling.",
                    "Risk",
                )
            )
    area_value = str(facts.get("area_m2") or facts.get("living_area_m2") or "").strip()
    rooms_value = _property_rooms_display(facts)
    if area_value or rooms_value:
        detail = " | ".join(
            part for part in (
                rooms_value,
                f"{area_value} m2" if area_value else "",
            ) if part
        )
        rows.append(_object_detail_row("Layout signal", detail, "Layout"))
    if match_reasons:
        rows.append(_object_detail_row("Best fit signal", match_reasons[0], "Positive"))
    if mismatch_reasons:
        rows.append(_object_detail_row("Main caution", mismatch_reasons[0], "Risk"))
    return rows


def _property_packet_missing_rows(
    *,
    facts: dict[str, object],
    preferences: dict[str, object],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    wanted_keywords = {
        str(value).strip().lower()
        for value in str(preferences.get("keywords") or "").split(",")
        if str(value).strip()
    }
    family_context = _property_family_context_active(preferences)

    def _open_check_detail(*, title: str, primary_key: str) -> str:
        normalized_key = str(primary_key or "").strip().lower()
        explicit = {
            "address": "Exact address not listed yet.",
            "heating_type": "Heating type not listed yet.",
            "has_lift": "Lift status not listed yet.",
            "nearest_supermarket_m": "Supermarket distance not listed yet.",
            "distance_supermarket_m": "Supermarket distance not listed yet.",
            "nearest_playground_m": "Playground distance not listed yet.",
            "distance_playground_m": "Playground distance not listed yet.",
            "nearest_library_m": "Library distance not listed yet.",
            "nearest_zoo_m": "Zoo distance not listed yet.",
            "nearest_pharmacy_m": "Pharmacy distance not listed yet.",
            "distance_pharmacy_m": "Pharmacy distance not listed yet.",
            "nearest_medical_care_m": "Doctor or hospital distance not listed yet.",
            "nearest_market_m": "Market distance not listed yet.",
            "nearest_hardware_store_m": "Baumarkt distance not listed yet.",
            "nearest_shopping_center_m": "Shopping-center distance not listed yet.",
            "nearest_shopping_street_m": "Shopping-street distance not listed yet.",
            "nearest_theatre_m": "Theatre distance not listed yet.",
            "nearest_public_pool_m": "Public-pool distance not listed yet.",
            "nearest_subway_m": "Underground distance not listed yet.",
            "nearest_transit_m": "Transit distance not listed yet.",
            "distance_underground_m": "Underground distance not listed yet.",
            "air_quality_risk": "Air-quality read not listed yet.",
            "crime_risk": "Safety read not listed yet.",
            "parking_pressure_risk": "Parking-pressure read not listed yet.",
            "drinking_water_risk": "Water-source read not listed yet.",
            "cesspit_risk": "Septic read not listed yet.",
            "winter_access_risk": "Winter-access read not listed yet.",
            "flood_risk": "Flood read not listed yet.",
        }
        if normalized_key in explicit:
            return explicit[normalized_key]
        normalized_title = str(title or "").strip().lower()
        if not normalized_title:
            return "Details not listed yet."
        return f"{normalized_title.title()} not listed yet."

    def _has_any_fact_value(keys: str | tuple[str, ...]) -> bool:
        key_group = (keys,) if isinstance(keys, str) else tuple(keys)
        return any(facts.get(key) not in (None, "", []) for key in key_group)

    def _preference_value_present(key: str) -> bool:
        raw_value = preferences.get(key)
        if raw_value in (None, "", [], {}, False):
            return False
        if isinstance(raw_value, str):
            normalized = raw_value.strip().lower()
            if normalized in {"0", "0.0", "false", "off", "none", "null", "neutral", "any"}:
                return False
            return True
        if isinstance(raw_value, (int, float)):
            return float(raw_value) > 0
        return True

    def _distance_check_requested(
        *,
        preference_keys: tuple[str, ...] = (),
        keyword_markers: tuple[str, ...] = (),
        family_only: bool = False,
    ) -> bool:
        if any(_preference_value_present(key) for key in preference_keys):
            return True
        if any(marker in wanted_keywords for marker in keyword_markers):
            return True
        return family_only and family_context

    distance_request_specs: dict[str, dict[str, object]] = {
        "nearest_supermarket_m": {
            "preference_keys": ("max_distance_to_supermarket_m",),
            "keyword_markers": ("supermarket nearby",),
        },
        "distance_supermarket_m": {
            "preference_keys": ("max_distance_to_supermarket_m",),
            "keyword_markers": ("supermarket nearby",),
        },
        "nearest_playground_m": {
            "preference_keys": ("max_distance_to_playground_m",),
            "keyword_markers": ("playground nearby",),
            "family_only": True,
        },
        "distance_playground_m": {
            "preference_keys": ("max_distance_to_playground_m",),
            "keyword_markers": ("playground nearby",),
            "family_only": True,
        },
        "nearest_library_m": {
            "preference_keys": ("max_distance_to_library_m",),
            "keyword_markers": ("library nearby",),
            "family_only": True,
        },
        "nearest_zoo_m": {
            "preference_keys": ("max_distance_to_zoo_m",),
            "keyword_markers": ("zoo nearby",),
            "family_only": True,
        },
        "nearest_pharmacy_m": {
            "preference_keys": ("max_distance_to_medical_care_m",),
            "keyword_markers": ("pharmacy nearby", "medical care nearby"),
            "family_only": True,
        },
        "distance_pharmacy_m": {
            "preference_keys": ("max_distance_to_medical_care_m",),
            "keyword_markers": ("pharmacy nearby", "medical care nearby"),
            "family_only": True,
        },
        "nearest_medical_care_m": {
            "preference_keys": ("max_distance_to_medical_care_m",),
            "keyword_markers": ("medical care nearby", "pharmacy nearby"),
            "family_only": True,
        },
        "nearest_market_m": {
            "preference_keys": ("max_distance_to_market_m",),
            "keyword_markers": ("market nearby",),
        },
        "nearest_hardware_store_m": {
            "preference_keys": ("max_distance_to_hardware_store_m",),
            "keyword_markers": ("baumarkt nearby",),
        },
        "nearest_shopping_center_m": {
            "preference_keys": ("max_distance_to_shopping_center_m",),
            "keyword_markers": ("shopping center nearby",),
        },
        "nearest_shopping_street_m": {
            "preference_keys": ("max_distance_to_shopping_street_m",),
            "keyword_markers": ("flaniermeile nearby",),
        },
        "nearest_theatre_m": {
            "preference_keys": ("max_distance_to_theatre_m",),
            "keyword_markers": ("theatre nearby",),
        },
        "nearest_public_pool_m": {
            "preference_keys": ("max_distance_to_public_pool_m",),
            "keyword_markers": ("public pool nearby",),
            "family_only": True,
        },
        "nearest_subway_m": {
            "preference_keys": ("max_distance_to_subway_m",),
            "keyword_markers": ("underground nearby",),
        },
        "nearest_transit_m": {
            "preference_keys": ("max_distance_to_subway_m",),
            "keyword_markers": ("underground nearby",),
        },
        "distance_underground_m": {
            "preference_keys": ("max_distance_to_subway_m",),
            "keyword_markers": ("underground nearby",),
        },
    }

    missing_fact_specs = [
        (("address", "exact_address", "street_address", "postal_name"), "Exact address", "Needed for precise neighbourhood checks and revisit logistics."),
        ("heating_type", "Heating type", "Needed to confirm if the building avoids the wrong heating setup."),
        ("has_lift", "Lift status", "Needed because access and daily usability often decide the shortlist."),
        (("nearest_supermarket_m", "distance_supermarket_m"), "Supermarket distance", "Needed to validate daily-errand convenience."),
        (("nearest_playground_m", "distance_playground_m"), "Playground distance", "Needed if the search is family-oriented."),
        ("nearest_library_m", "Library distance", "Needed for family, study, and child logistics when that criterion matters."),
        ("nearest_zoo_m", "Zoo distance", "Needed when zoo or Tiergarten access matters for family routines."),
        (("nearest_pharmacy_m", "distance_pharmacy_m"), "Pharmacy distance", "Needed to confirm basic services nearby."),
        ("nearest_medical_care_m", "Doctors and hospitals", "Needed when family, elder-care, or health resilience matter."),
        ("nearest_market_m", "Market distance", "Needed if district-life quality or produce-market access matters."),
        ("nearest_hardware_store_m", "Baumarkt distance", "Needed when renovation or practical errand access matters."),
        ("nearest_shopping_center_m", "Shopping-center distance", "Needed when broad bad-weather errand access matters."),
        ("nearest_shopping_street_m", "Flaniermeile distance", "Needed when promenade and walkable city life matter."),
        ("nearest_theatre_m", "Theatre distance", "Needed when cultural access matters."),
        ("nearest_public_pool_m", "Public-pool distance", "Needed when family or swimming access matters."),
        (("nearest_subway_m", "nearest_transit_m", "distance_underground_m"), "Underground distance", "Needed to validate fast transit access."),
        ("air_quality_risk", "Air-quality risk", "Needed to understand pollution burden and respiratory comfort."),
        ("crime_risk", "Crime pattern", "Needed to understand practical safety burden in the quarter."),
        ("parking_pressure_risk", "Parking pressure", "Needed when there is no garage and street parking might be difficult."),
        ("drinking_water_risk", "Water source and groundwater burden", "Needed to understand whether water quality or source dependence is a real concern."),
        ("cesspit_risk", "Senkgrube or septic burden", "Needed to understand recurring costs, maintenance, and smell risk."),
        ("winter_access_risk", "Winter driving access", "Needed to understand snow, slope, and seasonal access constraints."),
        ("flood_risk", "Flood exposure", "Needed to understand historic flooding, runoff, and zone risk."),
    ]
    for key, title, detail in missing_fact_specs:
        if _has_any_fact_value(key):
            continue
        primary_key = key[0] if isinstance(key, tuple) else key
        distance_request = distance_request_specs.get(primary_key)
        if distance_request and not _distance_check_requested(
            preference_keys=tuple(distance_request.get("preference_keys") or ()),
            keyword_markers=tuple(distance_request.get("keyword_markers") or ()),
            family_only=bool(distance_request.get("family_only")),
        ):
            continue
        if primary_key == "heating_type" and not ({"no gas", "district heating"} & wanted_keywords):
            continue
        if primary_key == "air_quality_risk" and not bool(preferences.get("prefer_good_air_quality")):
            continue
        if primary_key == "crime_risk" and not bool(preferences.get("prefer_low_crime_area")):
            continue
        if primary_key == "parking_pressure_risk" and not bool(preferences.get("require_parking_pressure_check")):
            continue
        if primary_key == "drinking_water_risk" and not bool(preferences.get("require_drinking_water_quality_research")):
            continue
        if primary_key == "cesspit_risk" and not bool(preferences.get("avoid_cesspit_or_septic_risk")):
            continue
        if primary_key == "winter_access_risk" and not bool(preferences.get("require_winter_access_research")):
            continue
        if primary_key == "flood_risk" and not bool(preferences.get("avoid_flood_risk_area")):
            continue
        severity = "Critical" if primary_key in {"address", "heating_type", "has_lift"} else "Important"
        rows.append(_object_detail_row(title, _open_check_detail(title=title, primary_key=primary_key), severity))
    for item in _property_missing_fact_items(facts):
        if str(item.get("status") or "").strip().lower() == "filled":
            continue
        if str(item.get("field") or "").strip().lower() == "rooms" and not _property_rooms_research_relevant(preferences):
            continue
        label = str(item.get("label") or item.get("field") or "Missing fact").strip()
        ooda = dict(item.get("ooda") or {}) if isinstance(item.get("ooda"), dict) else {}
        detail = str(ooda.get("act") or item.get("evidence") or "We are still checking this detail.").strip()
        rows.append(_object_detail_row(label, detail, "To check"))
    return rows


def _property_research_distance_detail(
    facts: dict[str, object],
    *,
    distance_key: str,
    name_keys: tuple[str, ...],
    source_keys: tuple[str, ...],
) -> str:
    raw_value = facts.get(distance_key)
    if raw_value in (None, "", []):
        return ""
    try:
        meters = int(float(raw_value))
    except Exception:
        return ""
    if meters <= 0:
        return ""
    name = next((str(facts.get(key) or "").strip() for key in name_keys if str(facts.get(key) or "").strip()), "")
    source = next((str(facts.get(key) or "").strip() for key in source_keys if str(facts.get(key) or "").strip()), "")
    subject = name or "nearest option"
    parts = [f"{subject}: {meters} m away"]
    if source:
        parts.append(f"source: {source}")
    return " | ".join(parts)


_PROPERTY_DISTANCE_MISMATCH_SPECS: tuple[dict[str, object], ...] = (
    {
        "label": "supermarket",
        "tokens": ("supermarket",),
        "distance_keys": ("nearest_supermarket_m", "distance_supermarket_m"),
        "name_keys": ("nearest_supermarket_name", "supermarket_name"),
        "preference_keys": ("max_distance_to_supermarket_m",),
        "importance_keys": ("max_distance_to_supermarket_importance",),
    },
    {
        "label": "playground",
        "tokens": ("playground",),
        "distance_keys": ("nearest_playground_m", "distance_playground_m"),
        "name_keys": ("nearest_playground_name", "playground_name"),
        "preference_keys": ("max_distance_to_playground_m",),
        "importance_keys": ("max_distance_to_playground_importance",),
    },
    {
        "label": "library",
        "tokens": ("library",),
        "distance_keys": ("nearest_library_m",),
        "name_keys": ("nearest_library_name", "library_name"),
        "preference_keys": ("max_distance_to_library_m",),
        "importance_keys": ("max_distance_to_library_importance",),
    },
    {
        "label": "zoo",
        "tokens": ("zoo",),
        "distance_keys": ("nearest_zoo_m",),
        "name_keys": ("nearest_zoo_name", "zoo_name"),
        "preference_keys": ("max_distance_to_zoo_m",),
        "importance_keys": ("max_distance_to_zoo_importance",),
    },
    {
        "label": "pharmacy",
        "tokens": ("pharmacy",),
        "distance_keys": ("nearest_pharmacy_m", "distance_pharmacy_m"),
        "name_keys": ("nearest_pharmacy_name", "pharmacy_name"),
        "preference_keys": ("max_distance_to_medical_care_m",),
        "importance_keys": ("max_distance_to_medical_care_importance",),
    },
    {
        "label": "medical care",
        "tokens": ("medical care", "doctor", "hospital"),
        "distance_keys": ("nearest_medical_care_m",),
        "name_keys": ("nearest_medical_care_name", "medical_care_name"),
        "preference_keys": ("max_distance_to_medical_care_m",),
        "importance_keys": ("max_distance_to_medical_care_importance",),
    },
    {
        "label": "market",
        "tokens": ("market",),
        "distance_keys": ("nearest_market_m",),
        "name_keys": ("nearest_market_name", "market_name"),
        "preference_keys": ("max_distance_to_market_m",),
        "importance_keys": ("max_distance_to_market_importance",),
    },
    {
        "label": "Baumarkt",
        "tokens": ("baumarkt", "hardware store"),
        "distance_keys": ("nearest_hardware_store_m",),
        "name_keys": ("nearest_hardware_store_name", "hardware_store_name"),
        "preference_keys": ("max_distance_to_hardware_store_m",),
        "importance_keys": ("max_distance_to_hardware_store_importance",),
    },
    {
        "label": "shopping center",
        "tokens": ("shopping center", "shopping-center"),
        "distance_keys": ("nearest_shopping_center_m",),
        "name_keys": ("nearest_shopping_center_name", "shopping_center_name"),
        "preference_keys": ("max_distance_to_shopping_center_m",),
        "importance_keys": ("max_distance_to_shopping_center_importance",),
    },
    {
        "label": "Flaniermeile",
        "tokens": ("flaniermeile", "shopping street", "promenade"),
        "distance_keys": ("nearest_shopping_street_m",),
        "name_keys": ("nearest_shopping_street_name", "shopping_street_name"),
        "preference_keys": ("max_distance_to_shopping_street_m",),
        "importance_keys": ("max_distance_to_shopping_street_importance",),
    },
    {
        "label": "theatre",
        "tokens": ("theatre", "theater"),
        "distance_keys": ("nearest_theatre_m",),
        "name_keys": ("nearest_theatre_name", "theatre_name"),
        "preference_keys": ("max_distance_to_theatre_m",),
        "importance_keys": ("max_distance_to_theatre_importance",),
    },
    {
        "label": "public pool",
        "tokens": ("public pool", "pool"),
        "distance_keys": ("nearest_public_pool_m",),
        "name_keys": ("nearest_public_pool_name", "public_pool_name"),
        "preference_keys": ("max_distance_to_public_pool_m",),
        "importance_keys": ("max_distance_to_public_pool_importance",),
    },
    {
        "label": "underground",
        "tokens": ("underground", "subway", "u-bahn", "transit"),
        "distance_keys": ("nearest_subway_m", "nearest_transit_m", "distance_underground_m"),
        "name_keys": ("nearest_subway_name", "subway_station_name", "nearest_transit_name", "transit_stop_name"),
        "preference_keys": ("max_distance_to_subway_m",),
        "importance_keys": ("max_distance_to_subway_importance",),
    },
    {
        "label": "kindergarten",
        "tokens": ("kindergarten",),
        "distance_keys": ("nearest_kindergarten_m",),
        "name_keys": ("nearest_kindergarten_name", "kindergarten_name"),
        "preference_keys": ("max_distance_to_kindergarten_m",),
        "importance_keys": ("max_distance_to_kindergarten_importance",),
    },
    {
        "label": "school",
        "tokens": ("school", "volksschule"),
        "distance_keys": ("nearest_school_m",),
        "name_keys": ("nearest_school_name", "school_name"),
        "preference_keys": (
            "max_distance_to_school_m",
            "max_distance_to_ganztags_volksschule_m",
            "max_distance_to_halbtags_volksschule_m",
        ),
        "importance_keys": (
            "max_distance_to_school_importance",
            "max_distance_to_ganztags_volksschule_importance",
            "max_distance_to_halbtags_volksschule_importance",
        ),
    },
)


def _property_positive_distance_value(
    facts: dict[str, object],
    distance_keys: tuple[str, ...],
) -> int | None:
    for key in distance_keys:
        raw_value = facts.get(key)
        if raw_value in (None, "", []):
            continue
        try:
            meters = int(float(raw_value))
        except Exception:
            continue
        if meters > 0:
            return meters
    return None


def _property_positive_preference_distance(
    preferences: dict[str, object],
    preference_keys: tuple[str, ...],
) -> int | None:
    for key in preference_keys:
        raw_value = preferences.get(key)
        if raw_value in (None, "", [], {}, False):
            continue
        try:
            meters = int(float(raw_value))
        except Exception:
            continue
        if meters > 0:
            return meters
    return None


def _property_csv_tokens(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        parts = [str(item or "").strip() for item in value if str(item or "").strip()]
    else:
        parts = [
            str(item or "").strip()
            for item in str(value or "").replace(";", ",").split(",")
            if str(item or "").strip()
        ]
    return {part.casefold() for part in parts if part}


def _property_ordered_keyword_markers(preferences: dict[str, object]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()

    def _append_raw_keyword_order(raw_value: object) -> None:
        text = str(raw_value or "").strip()
        if not text:
            return
        for token in text.replace(";", ",").split(","):
            normalized = str(token or "").strip().casefold()
            if not normalized or normalized in seen:
                continue
            ordered.append(normalized)
            seen.add(normalized)

    def _append_markers_from_preference_object(value: object) -> None:
        if not isinstance(value, dict):
            raw_json = str(value or "").strip()
            if not raw_json:
                return
            try:
                parsed = json.loads(raw_json)
            except json.JSONDecodeError:
                return
            if not isinstance(parsed, dict):
                return
            value = parsed
        for marker in value.keys():
            normalized = str(marker or "").strip().casefold()
            if not normalized or normalized in seen:
                continue
            ordered.append(normalized)
            seen.add(normalized)

    _append_raw_keyword_order(preferences.get("keywords"))
    _append_raw_keyword_order(preferences.get("avoid_keywords"))
    _append_markers_from_preference_object(preferences.get("keyword_preferences"))
    _append_markers_from_preference_object(preferences.get("keyword_preferences_json"))

    return tuple(ordered)


def _property_ordered_explicit_keyword_markers(preferences: dict[str, object]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()

    for raw_value in (preferences.get("keywords"), preferences.get("avoid_keywords")):
        text = str(raw_value or "").strip()
        if not text:
            continue
        for token in text.replace(";", ",").split(","):
            normalized = str(token or "").strip().casefold()
            if not normalized or normalized in seen:
                continue
            ordered.append(normalized)
            seen.add(normalized)
    return tuple(ordered)


def _property_keyword_preference_states(preferences: dict[str, object]) -> dict[str, str]:
    source: dict[str, object] = {}
    raw_value = preferences.get("keyword_preferences")
    raw_json = str(preferences.get("keyword_preferences_json") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            source.update(parsed)
    if isinstance(raw_value, dict):
        source.update(raw_value)
    return {
        str(key or "").strip().casefold(): str(value or "").strip().casefold()
        for key, value in dict(source or {}).items()
        if str(key or "").strip() and str(value or "").strip()
    }


def _property_preference_value_is_selected(value: object) -> bool:
    if value in (None, "", [], {}, False):
        return False
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"", "0", "0.0", "false", "off", "none", "null", "neutral", "any"}:
            return False
        return True
    if isinstance(value, (int, float)):
        return float(value) > 0
    return True


def _property_selected_distance_filter_active(
    preferences: dict[str, object],
    *,
    preference_keys: tuple[str, ...],
    importance_keys: tuple[str, ...],
    legacy_boolean_keys: tuple[str, ...],
    keyword_markers: tuple[str, ...],
    keyword_tokens: set[str],
    keyword_states: dict[str, str],
    allow_preference_only: bool = True,
) -> bool:
    normalized_markers = {str(marker or "").strip().casefold() for marker in keyword_markers if str(marker or "").strip()}
    selected_marker_states = {marker: keyword_states.get(marker) for marker in keyword_tokens}
    has_explicit_selected_keyword_state = any(str(value or "").strip() for value in selected_marker_states.values())
    if has_explicit_selected_keyword_state:
        return any(_property_preference_value_is_selected(keyword_states.get(marker)) for marker in normalized_markers)

    explicit_marker_states = {marker: keyword_states.get(marker) for marker in normalized_markers}
    has_explicit_marker_state = any(str(value or "").strip() for value in explicit_marker_states.values())
    has_explicit_marker_preference = any(
        _property_preference_value_is_selected(value)
        for value in explicit_marker_states.values()
        if str(value or "").strip()
    )
    if has_explicit_marker_state and normalized_markers:
        return has_explicit_marker_preference

    if allow_preference_only and preference_keys and _property_positive_preference_distance(preferences, preference_keys) is not None:
        return True
    if allow_preference_only and any(_property_preference_value_is_selected(preferences.get(key)) for key in importance_keys):
        return True
    if any(_property_preference_value_is_selected(preferences.get(key)) for key in legacy_boolean_keys):
        return True
    if normalized_markers & keyword_tokens:
        return True
    for marker in normalized_markers:
        if _property_preference_value_is_selected(keyword_states.get(marker)):
            return True
    return False


def _property_keyword_markers_selected(
    markers: tuple[str, ...],
    *,
    keyword_tokens: set[str],
    keyword_states: dict[str, str],
) -> bool:
    normalized_markers = {str(marker or "").strip().casefold() for marker in markers if str(marker or "").strip()}
    if normalized_markers & keyword_tokens:
        return True
    return any(_property_preference_value_is_selected(keyword_states.get(marker)) for marker in normalized_markers)


_PROPERTY_RESEARCH_DISTANCE_ROW_SPECS: tuple[dict[str, object], ...] = (
    {
        "title": "Supermarket",
        "label": "supermarket",
        "distance_keys": ("nearest_supermarket_m", "distance_supermarket_m"),
        "name_keys": ("nearest_supermarket_name", "supermarket_name"),
        "source_keys": ("nearest_supermarket_source", "supermarket_source"),
        "preference_keys": ("max_distance_to_supermarket_m",),
        "importance_keys": ("max_distance_to_supermarket_importance",),
        "legacy_boolean_keys": ("prefer_supermarket_nearby",),
        "keyword_markers": ("supermarket nearby",),
        "tag": "Errands",
    },
    {
        "title": "Playground",
        "label": "playground",
        "distance_keys": ("nearest_playground_m", "distance_playground_m"),
        "name_keys": ("nearest_playground_name", "playground_name"),
        "source_keys": ("nearest_playground_source", "playground_source"),
        "preference_keys": ("max_distance_to_playground_m",),
        "importance_keys": ("max_distance_to_playground_importance",),
        "legacy_boolean_keys": ("prefer_playgrounds_nearby",),
        "keyword_markers": ("playground nearby",),
        "tag": "Family",
    },
    {
        "title": "Library",
        "label": "library",
        "distance_keys": ("nearest_library_m",),
        "name_keys": ("nearest_library_name", "library_name"),
        "source_keys": ("nearest_library_source", "library_source"),
        "preference_keys": ("max_distance_to_library_m",),
        "importance_keys": ("max_distance_to_library_importance",),
        "legacy_boolean_keys": ("prefer_libraries_nearby",),
        "keyword_markers": ("library nearby",),
        "tag": "Family",
    },
    {
        "title": "Zoo",
        "label": "zoo",
        "distance_keys": ("nearest_zoo_m",),
        "name_keys": ("nearest_zoo_name", "zoo_name"),
        "source_keys": ("nearest_zoo_source", "zoo_source"),
        "preference_keys": ("max_distance_to_zoo_m",),
        "importance_keys": ("max_distance_to_zoo_importance",),
        "legacy_boolean_keys": ("prefer_zoos_nearby",),
        "keyword_markers": ("zoo nearby",),
        "tag": "Family",
    },
    {
        "title": "Public pool",
        "label": "public pool",
        "distance_keys": ("nearest_public_pool_m",),
        "name_keys": ("nearest_public_pool_name", "public_pool_name"),
        "source_keys": ("nearest_public_pool_source", "public_pool_source"),
        "preference_keys": ("max_distance_to_public_pool_m",),
        "importance_keys": ("max_distance_to_public_pool_importance",),
        "legacy_boolean_keys": ("prefer_public_pool_nearby",),
        "keyword_markers": ("public pool nearby",),
        "tag": "Family",
    },
    {
        "title": "Pharmacy",
        "label": "pharmacy",
        "distance_keys": ("nearest_pharmacy_m", "distance_pharmacy_m"),
        "name_keys": ("nearest_pharmacy_name", "pharmacy_name"),
        "source_keys": ("nearest_pharmacy_source", "pharmacy_source"),
        "preference_keys": ("max_distance_to_medical_care_m",),
        "importance_keys": ("max_distance_to_medical_care_importance",),
        "legacy_boolean_keys": ("prefer_pharmacy_nearby",),
        "keyword_markers": ("pharmacy nearby",),
        "tag": "Health",
        "allow_preference_only": False,
    },
    {
        "title": "Medical care",
        "label": "medical care",
        "distance_keys": ("nearest_medical_care_m",),
        "name_keys": ("nearest_medical_care_name", "medical_care_name"),
        "source_keys": ("nearest_medical_care_source", "medical_care_source"),
        "preference_keys": ("max_distance_to_medical_care_m",),
        "importance_keys": ("max_distance_to_medical_care_importance",),
        "legacy_boolean_keys": ("prefer_medical_care_nearby",),
        "keyword_markers": ("medical care nearby",),
        "tag": "Health",
        "skip_if_keyword_markers_selected": ("pharmacy nearby",),
    },
    {
        "title": "Market",
        "label": "market",
        "distance_keys": ("nearest_market_m",),
        "name_keys": ("nearest_market_name", "market_name"),
        "source_keys": ("nearest_market_source", "market_source"),
        "preference_keys": ("max_distance_to_market_m",),
        "importance_keys": ("max_distance_to_market_importance",),
        "legacy_boolean_keys": ("prefer_markets_nearby",),
        "keyword_markers": ("market nearby",),
        "tag": "District life",
    },
    {
        "title": "Hardware store",
        "label": "hardware store",
        "distance_keys": ("nearest_hardware_store_m",),
        "name_keys": ("nearest_hardware_store_name", "hardware_store_name"),
        "source_keys": ("nearest_hardware_store_source", "hardware_store_source"),
        "preference_keys": ("max_distance_to_hardware_store_m",),
        "importance_keys": ("max_distance_to_hardware_store_importance",),
        "legacy_boolean_keys": ("prefer_hardware_store_nearby",),
        "keyword_markers": ("baumarkt nearby", "hardware store nearby"),
        "tag": "Practical",
    },
    {
        "title": "Shopping center",
        "label": "shopping center",
        "distance_keys": ("nearest_shopping_center_m",),
        "name_keys": ("nearest_shopping_center_name", "shopping_center_name"),
        "source_keys": ("nearest_shopping_center_source", "shopping_center_source"),
        "preference_keys": ("max_distance_to_shopping_center_m",),
        "importance_keys": ("max_distance_to_shopping_center_importance",),
        "legacy_boolean_keys": ("prefer_shopping_center_nearby",),
        "keyword_markers": ("shopping center nearby",),
        "tag": "Errands",
    },
    {
        "title": "Promenade",
        "label": "promenade",
        "distance_keys": ("nearest_shopping_street_m",),
        "name_keys": ("nearest_shopping_street_name", "shopping_street_name"),
        "source_keys": ("nearest_shopping_street_source", "shopping_street_source"),
        "preference_keys": ("max_distance_to_shopping_street_m",),
        "importance_keys": ("max_distance_to_shopping_street_importance",),
        "legacy_boolean_keys": ("prefer_shopping_street_nearby",),
        "keyword_markers": ("flaniermeile nearby", "shopping street nearby", "promenade nearby"),
        "tag": "City life",
    },
    {
        "title": "Theatre",
        "label": "theatre",
        "distance_keys": ("nearest_theatre_m",),
        "name_keys": ("nearest_theatre_name", "theatre_name"),
        "source_keys": ("nearest_theatre_source", "theatre_source"),
        "preference_keys": ("max_distance_to_theatre_m",),
        "importance_keys": ("max_distance_to_theatre_importance",),
        "legacy_boolean_keys": ("prefer_theatre_nearby",),
        "keyword_markers": ("theatre nearby",),
        "tag": "Culture",
    },
    {
        "title": "Underground",
        "label": "underground",
        "distance_keys": ("nearest_subway_m", "nearest_transit_m", "distance_underground_m"),
        "name_keys": ("nearest_subway_name", "subway_station_name", "nearest_transit_name", "transit_stop_name"),
        "source_keys": ("nearest_subway_source", "subway_source", "nearest_transit_source", "transit_source"),
        "preference_keys": ("max_distance_to_subway_m",),
        "importance_keys": ("max_distance_to_subway_importance",),
        "legacy_boolean_keys": ("prefer_subway_nearby",),
        "keyword_markers": ("underground nearby",),
        "tag": "Transit",
    },
    {
        "title": "Kindergarten",
        "label": "kindergarten",
        "distance_keys": ("nearest_kindergarten_m",),
        "name_keys": ("nearest_kindergarten_name", "kindergarten_name"),
        "source_keys": ("nearest_kindergarten_source", "kindergarten_source"),
        "preference_keys": ("max_distance_to_kindergarten_m",),
        "importance_keys": ("max_distance_to_kindergarten_importance",),
        "keyword_markers": ("kindergarten nearby",),
        "tag": "Family",
    },
    {
        "title": "School",
        "label": "school",
        "distance_keys": ("nearest_school_m",),
        "name_keys": ("nearest_school_name", "school_name"),
        "source_keys": ("nearest_school_source", "school_source"),
        "preference_keys": (
            "max_distance_to_school_m",
            "max_distance_to_ganztags_volksschule_m",
            "max_distance_to_halbtags_volksschule_m",
        ),
        "importance_keys": (
            "max_distance_to_school_importance",
            "max_distance_to_ganztags_volksschule_importance",
            "max_distance_to_halbtags_volksschule_importance",
        ),
        "keyword_markers": ("school nearby", "volksschule nearby"),
        "tag": "Family",
    },
    {
        "title": "University",
        "label": "university",
        "distance_keys": ("nearest_university_m",),
        "name_keys": ("nearest_university_name", "university_name"),
        "source_keys": ("nearest_university_source", "university_source"),
        "preference_keys": ("max_distance_to_university_m",),
        "importance_keys": ("max_distance_to_university_importance",),
        "tag": "Lifestyle",
    },
    {
        "title": "Starbucks",
        "label": "Starbucks",
        "distance_keys": ("nearest_starbucks_m",),
        "name_keys": ("nearest_starbucks_name", "starbucks_name"),
        "source_keys": ("nearest_starbucks_source", "starbucks_source"),
        "preference_keys": ("max_distance_to_starbucks_m",),
        "importance_keys": ("max_distance_to_starbucks_importance",),
        "tag": "Lifestyle",
    },
    {
        "title": "Fitness",
        "label": "fitness",
        "distance_keys": ("nearest_fitness_center_m",),
        "name_keys": ("nearest_fitness_center_name", "fitness_center_name"),
        "source_keys": ("nearest_fitness_center_source", "fitness_center_source"),
        "preference_keys": ("max_distance_to_fitness_center_m",),
        "importance_keys": ("max_distance_to_fitness_center_importance",),
        "tag": "Lifestyle",
    },
    {
        "title": "Cinema",
        "label": "cinema",
        "distance_keys": ("nearest_cinema_m",),
        "name_keys": ("nearest_cinema_name", "cinema_name"),
        "source_keys": ("nearest_cinema_source", "cinema_source"),
        "preference_keys": ("max_distance_to_cinema_m",),
        "importance_keys": ("max_distance_to_cinema_importance",),
        "tag": "Lifestyle",
    },
    {
        "title": "Bouldering",
        "label": "bouldering",
        "distance_keys": ("nearest_bouldering_m",),
        "name_keys": ("nearest_bouldering_name", "bouldering_name"),
        "source_keys": ("nearest_bouldering_source", "bouldering_source"),
        "preference_keys": ("max_distance_to_bouldering_m",),
        "importance_keys": ("max_distance_to_bouldering_importance",),
        "tag": "Lifestyle",
    },
    {
        "title": "Dog park",
        "label": "dog park",
        "distance_keys": ("nearest_dog_park_m",),
        "name_keys": ("nearest_dog_park_name", "dog_park_name"),
        "source_keys": ("nearest_dog_park_source", "dog_park_source"),
        "preference_keys": ("max_distance_to_dog_park_m",),
        "importance_keys": ("max_distance_to_dog_park_importance",),
        "tag": "Lifestyle",
    },
    {
        "title": "Good cafe",
        "label": "good cafe",
        "distance_keys": ("nearest_good_cafe_m",),
        "name_keys": ("nearest_good_cafe_name", "good_cafe_name"),
        "source_keys": ("nearest_good_cafe_source", "good_cafe_source"),
        "preference_keys": ("max_distance_to_good_cafe_m",),
        "importance_keys": ("max_distance_to_good_cafe_importance",),
        "tag": "Lifestyle",
    },
)


def _property_selected_distance_rows(
    *,
    facts: dict[str, object],
    preferences: dict[str, object],
) -> list[dict[str, str]]:
    keyword_tokens = _property_csv_tokens(preferences.get("keywords")) | _property_csv_tokens(preferences.get("avoid_keywords"))
    keyword_states = _property_keyword_preference_states(preferences)
    ordered_markers = _property_ordered_keyword_markers(preferences)
    marker_position = {marker: index for index, marker in enumerate(ordered_markers)}
    rows: list[tuple[int, int, dict[str, str]]] = []
    seen_titles: set[str] = set()
    for spec_index, spec in enumerate(_PROPERTY_RESEARCH_DISTANCE_ROW_SPECS):
        title = str(spec.get("title") or "").strip()
        if not title or title.casefold() in seen_titles:
            continue
        preference_keys = tuple(str(key) for key in spec.get("preference_keys", ()) if str(key).strip())
        importance_keys = tuple(str(key) for key in spec.get("importance_keys", ()) if str(key).strip())
        legacy_boolean_keys = tuple(str(key) for key in spec.get("legacy_boolean_keys", ()) if str(key).strip())
        keyword_markers = tuple(str(key) for key in spec.get("keyword_markers", ()) if str(key).strip())
        if (
            spec.get("skip_if_keyword_markers_selected")
            and not _property_keyword_markers_selected(
                keyword_markers,
                keyword_tokens=keyword_tokens,
                keyword_states=keyword_states,
            )
            and _property_keyword_markers_selected(
                tuple(str(key) for key in spec.get("skip_if_keyword_markers_selected", ()) if str(key).strip()),
                keyword_tokens=keyword_tokens,
                keyword_states=keyword_states,
            )
        ):
            continue
        if not _property_selected_distance_filter_active(
            preferences,
            preference_keys=preference_keys,
            importance_keys=importance_keys,
            legacy_boolean_keys=legacy_boolean_keys,
            keyword_markers=keyword_markers,
            keyword_tokens=keyword_tokens,
            keyword_states=keyword_states,
            allow_preference_only=bool(spec.get("allow_preference_only", True)),
        ):
            continue
        label = str(spec.get("label") or title).strip().lower()
        distance_keys = tuple(str(key) for key in spec.get("distance_keys", ()) if str(key).strip())
        meters = _property_positive_distance_value(facts, distance_keys)
        requested_limit = _property_positive_preference_distance(preferences, preference_keys)
        if meters is not None:
            place_name = _property_first_fact_text(
                facts,
                tuple(str(key) for key in spec.get("name_keys", ()) if str(key).strip()),
            )
            source = _property_first_fact_text(
                facts,
                tuple(str(key) for key in spec.get("source_keys", ()) if str(key).strip()),
            )
            subject = f"Nearest {label}"
            if place_name:
                subject = f"{subject}: {place_name}"
            detail = f"{subject} is {meters} m away"
            if requested_limit is not None:
                detail = f"{detail}; selected limit {requested_limit} m"
            if source:
                detail = f"{detail} | source: {source}"
            tag = str(spec.get("tag") or "Distance").strip() or "Distance"
        else:
            detail = f"Nearest {label} distance is not listed yet"
            if requested_limit is not None:
                detail = f"{detail}; selected limit {requested_limit} m"
            detail = f"{detail}."
            tag = "To check"
        if not detail.endswith("."):
            detail = f"{detail}."
        marker_positions: list[int] = []
        for marker in keyword_markers:
            normalized_marker = str(marker or "").strip().casefold()
            if normalized_marker in marker_position:
                marker_positions.append(marker_position[normalized_marker])
        preferred_position = min(marker_positions) if marker_positions else len(ordered_markers) + spec_index + 1
        rows.append((preferred_position, spec_index, _object_detail_row(title, detail, tag)))
        seen_titles.add(title.casefold())
    rows.sort(key=lambda item: (item[0], item[1]))
    return [_object_detail_row for _position, _spec_index, _object_detail_row in rows]


def _property_available_nearby_distance_rows(
    *,
    facts: dict[str, object],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen_titles: set[str] = set()
    for spec in _PROPERTY_RESEARCH_DISTANCE_ROW_SPECS:
        title = str(spec.get("title") or "").strip()
        if not title or title.casefold() in seen_titles:
            continue
        meters = _property_positive_distance_value(
            facts,
            tuple(str(key) for key in spec.get("distance_keys", ()) if str(key).strip()),
        )
        if meters is None:
            continue
        label = str(spec.get("label") or title).strip().lower()
        place_name = _property_first_fact_text(
            facts,
            tuple(str(key) for key in spec.get("name_keys", ()) if str(key).strip()),
        )
        source = _property_first_fact_text(
            facts,
            tuple(str(key) for key in spec.get("source_keys", ()) if str(key).strip()),
        )
        subject = f"Nearest {label}"
        if place_name:
            subject = f"{subject}: {place_name}"
        detail = f"{subject} is {meters} m away"
        if source:
            detail = f"{detail} | source: {source}"
        if not detail.endswith("."):
            detail = f"{detail}."
        rows.append(_object_detail_row(title, detail, str(spec.get("tag") or "Distance").strip() or "Distance"))
        seen_titles.add(title.casefold())
    return rows


def _property_distance_panel_copy(
    *,
    selected_rows: list[dict[str, str]],
    single_selected_distance_filter: bool = False,
    copy_scope: str = "current",
) -> str:
    if not selected_rows:
        return ""
    normalized_scope = str(copy_scope or "current").strip().lower()
    if single_selected_distance_filter and len(selected_rows) == 1:
        title = str(selected_rows[0].get("title") or "").strip()
        subject = f"{title} distance" if title else "Selected nearby distance"
        if normalized_scope == "recent":
            return f"{subject} from your latest matching search."
        if normalized_scope == "workspace":
            return f"{subject} saved on this workspace."
        return f"{subject} from this search."
    if normalized_scope == "recent":
        return "Distances for the nearby filters from your latest matching search."
    if normalized_scope == "workspace":
        return "Distances for the nearby filters saved on this workspace."
    return "Distances for the nearby filters in this search."


def _property_keyword_state_priority(value: object) -> int:
    normalized = str(value or "").strip().casefold()
    if normalized in {"must_have", "required"}:
        return 4
    if normalized == "important":
        return 3
    if normalized in {"nice_to_have", "prefer", "preferred"}:
        return 2
    if _property_preference_value_is_selected(value):
        return 1
    return 0


def _property_primary_selected_distance_row(
    *,
    selected_rows: list[dict[str, str]],
    preferences: dict[str, object],
) -> dict[str, str]:
    if not selected_rows:
        return {}
    if len(selected_rows) == 1:
        return dict(selected_rows[0])
    explicit_markers = _property_ordered_explicit_keyword_markers(preferences)
    explicit_marker_position = {marker: index for index, marker in enumerate(explicit_markers)}
    keyword_states = _property_keyword_preference_states(preferences)
    spec_by_title = {
        str(spec.get("title") or "").strip().casefold(): spec
        for spec in _PROPERTY_RESEARCH_DISTANCE_ROW_SPECS
        if str(spec.get("title") or "").strip()
    }
    title_order = {
        str(spec.get("title") or "").strip().casefold(): index
        for index, spec in enumerate(_PROPERTY_RESEARCH_DISTANCE_ROW_SPECS)
        if str(spec.get("title") or "").strip()
    }

    def _row_rank(row: dict[str, str]) -> tuple[int, int, int, int]:
        title_key = str(row.get("title") or "").strip().casefold()
        spec = spec_by_title.get(title_key) or {}
        keyword_markers = tuple(str(marker or "").strip().casefold() for marker in spec.get("keyword_markers", ()) if str(marker or "").strip())
        explicit_positions = [explicit_marker_position[marker] for marker in keyword_markers if marker in explicit_marker_position]
        explicit_position = min(explicit_positions) if explicit_positions else len(explicit_marker_position) + len(title_order)
        state_priority = max((_property_keyword_state_priority(keyword_states.get(marker)) for marker in keyword_markers), default=0)
        spec_index = title_order.get(title_key, len(title_order))
        has_explicit_priority = 1 if explicit_positions else 0
        return (
            -has_explicit_priority,
            explicit_position,
            -state_priority,
            spec_index,
        )

    return dict(min(selected_rows, key=_row_rank))


def _property_distance_panel_rows(
    *,
    facts: dict[str, object],
    preferences: dict[str, object],
    single_selected_distance_filter: bool = False,
    copy_scope: str = "current",
) -> tuple[list[dict[str, str]], str]:
    selected_rows = _property_selected_distance_rows(
        facts=facts,
        preferences=preferences,
    )
    if single_selected_distance_filter and selected_rows:
        primary_row = _property_primary_selected_distance_row(
            selected_rows=selected_rows,
            preferences=preferences,
        )
        selected_rows = [primary_row] if primary_row else []
    if selected_rows:
        return selected_rows, _property_distance_panel_copy(
            selected_rows=selected_rows,
            single_selected_distance_filter=single_selected_distance_filter,
            copy_scope=copy_scope,
        )
    return [], ""


def _property_first_fact_text(
    facts: dict[str, object],
    keys: tuple[str, ...],
) -> str:
    for key in keys:
        value = str(facts.get(key) or "").strip()
        if value:
            return value
    return ""


def _property_distance_mismatch_reason_detail(
    reason: object,
    *,
    facts: dict[str, object],
    preferences: dict[str, object],
) -> str:
    text = " ".join(str(reason or "").split()).strip()
    if not text:
        return ""
    normalized = text.casefold()
    for spec in _PROPERTY_DISTANCE_MISMATCH_SPECS:
        tokens = tuple(str(token).casefold() for token in spec.get("tokens", ()) if str(token).strip())
        if not tokens or not any(token in normalized for token in tokens):
            continue
        meters = _property_positive_distance_value(
            facts,
            tuple(str(key) for key in spec.get("distance_keys", ()) if str(key).strip()),
        )
        if meters is None:
            return ""
        place_name = _property_first_fact_text(
            facts,
            tuple(str(key) for key in spec.get("name_keys", ()) if str(key).strip()),
        )
        requested = _property_positive_preference_distance(
            preferences,
            tuple(str(key) for key in spec.get("preference_keys", ()) if str(key).strip()),
        )
        importance = _property_first_fact_text(
            preferences,
            tuple(str(key) for key in spec.get("importance_keys", ()) if str(key).strip()),
        ).casefold()
        label = str(spec.get("label") or "place").strip().lower()
        subject = f"Nearest {label}"
        if place_name:
            subject = f"{subject}: {place_name}"
        if "avoid" in importance or "avoid preference" in normalized or "too close" in normalized:
            if requested is not None:
                return f"{subject} is {meters} m away; you asked to keep it farther than {requested} m."
            return f"{subject} is {meters} m away."
        if requested is not None:
            return f"{subject} is {meters} m away; your limit was {requested} m."
        return f"{subject} is {meters} m away."
    return text


def _property_normalized_mismatch_reasons(
    mismatch_reasons: list[object],
    *,
    facts: dict[str, object],
    preferences: dict[str, object],
    limit: int = 4,
) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for item in list(mismatch_reasons or [])[: max(limit, 0) or 0]:
        detail = _property_distance_mismatch_reason_detail(
            item,
            facts=facts,
            preferences=preferences,
        )
        detail = " ".join(str(detail or "").split()).strip()
        if not detail:
            continue
        dedupe_key = detail.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rows.append(detail)
    return rows


def _property_human_join(parts: list[str]) -> str:
    cleaned = [str(part or "").strip() for part in parts if str(part or "").strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])}, and {cleaned[-1]}"


def _property_positive_distance_fact_row(
    *,
    facts: dict[str, object],
    preferences: dict[str, object],
    title: str,
    label: str,
    distance_keys: tuple[str, ...],
    name_keys: tuple[str, ...],
    preference_keys: tuple[str, ...],
    default_limit_m: int,
    tag: str,
    suffix: str = "",
) -> dict[str, str] | None:
    meters = _property_positive_distance_value(facts, distance_keys)
    if meters is None:
        return None
    requested_limit = _property_positive_preference_distance(preferences, preference_keys)
    effective_limit = requested_limit or int(default_limit_m or 0)
    if effective_limit <= 0 or meters > effective_limit:
        return None
    place_name = _property_first_fact_text(facts, name_keys)
    subject = place_name or f"The nearest {label.lower()}"
    qualifier = "only" if meters <= max(250, int(round(float(effective_limit) * 0.5))) else "about"
    detail = f"{subject} is {qualifier} {meters} m away"
    if suffix:
        detail = f"{detail} {suffix}"
    return _object_detail_row(title, f"{detail}.", tag)


def _property_packet_positive_fact_rows(
    *,
    facts: dict[str, object],
    preferences: dict[str, object],
    match_reasons: list[str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    family_context = _property_family_context_active(preferences)
    cooling_reason = _property_cooling_corridor_match_reason(facts)
    if cooling_reason:
        rows.append(_object_detail_row("Summer heat", cooling_reason, "Climate"))

    highlight_specs: tuple[dict[str, object], ...] = (
        {
            "title": "Supermarket",
            "label": "supermarket",
            "distance_keys": ("nearest_supermarket_m", "distance_supermarket_m"),
            "name_keys": ("nearest_supermarket_name", "supermarket_name"),
            "preference_keys": ("max_distance_to_supermarket_m",),
            "default_limit_m": 700,
            "tag": "Errands",
            "suffix": "for daily errands",
            "show_when": True,
        },
        {
            "title": "Playground",
            "label": "playground",
            "distance_keys": ("nearest_playground_m", "distance_playground_m"),
            "name_keys": ("nearest_playground_name", "playground_name"),
            "preference_keys": ("max_distance_to_playground_m",),
            "default_limit_m": 500,
            "tag": "Family",
            "suffix": "for an easy family stop",
            "show_when": family_context or bool(preferences.get("max_distance_to_playground_m")),
        },
        {
            "title": "Kindergarten",
            "label": "kindergarten",
            "distance_keys": ("nearest_kindergarten_m",),
            "name_keys": ("nearest_kindergarten_name", "kindergarten_name"),
            "preference_keys": ("max_distance_to_kindergarten_m",),
            "default_limit_m": 650,
            "tag": "Family",
            "suffix": "for the daily drop-off",
            "show_when": family_context or bool(preferences.get("max_distance_to_kindergarten_m")),
        },
        {
            "title": "School",
            "label": "school",
            "distance_keys": ("nearest_school_m",),
            "name_keys": ("nearest_school_name", "school_name"),
            "preference_keys": (
                "max_distance_to_school_m",
                "max_distance_to_ganztags_volksschule_m",
                "max_distance_to_halbtags_volksschule_m",
            ),
            "default_limit_m": 900,
            "tag": "Family",
            "suffix": "for the school route",
            "show_when": family_context
            or bool(preferences.get("max_distance_to_school_m"))
            or bool(preferences.get("max_distance_to_ganztags_volksschule_m"))
            or bool(preferences.get("max_distance_to_halbtags_volksschule_m")),
        },
        {
            "title": "Underground",
            "label": "underground",
            "distance_keys": ("nearest_subway_m", "distance_underground_m"),
            "name_keys": ("nearest_subway_name", "subway_station_name"),
            "preference_keys": ("max_distance_to_subway_m",),
            "default_limit_m": 900,
            "tag": "Transit",
            "suffix": "for the commute",
            "show_when": bool(preferences.get("max_distance_to_subway_m")),
        },
    )
    seen_details: set[str] = set()
    for spec in highlight_specs:
        if not bool(spec.get("show_when")):
            continue
        row = _property_positive_distance_fact_row(
            facts=facts,
            preferences=preferences,
            title=str(spec.get("title") or "").strip(),
            label=str(spec.get("label") or "").strip(),
            distance_keys=tuple(str(key) for key in spec.get("distance_keys", ()) if str(key).strip()),
            name_keys=tuple(str(key) for key in spec.get("name_keys", ()) if str(key).strip()),
            preference_keys=tuple(str(key) for key in spec.get("preference_keys", ()) if str(key).strip()),
            default_limit_m=int(spec.get("default_limit_m") or 0),
            tag=str(spec.get("tag") or "Positive").strip(),
            suffix=str(spec.get("suffix") or "").strip(),
        )
        if row is None:
            continue
        dedupe_key = str(row.get("detail") or "").strip().casefold()
        if dedupe_key in seen_details:
            continue
        seen_details.add(dedupe_key)
        rows.append(row)
    if not rows and match_reasons:
        rows.append(_object_detail_row("Strong signal", match_reasons[0], "Positive"))
    return rows[:3]


def _property_packet_missing_summary_row(
    missing_rows: list[dict[str, str]],
) -> dict[str, str] | None:
    critical_titles = [
        str(row.get("title") or "").strip()
        for row in missing_rows
        if str(row.get("tag") or "").strip().lower() == "critical" and str(row.get("title") or "").strip()
    ]
    if critical_titles:
        summary = _property_human_join(critical_titles[:3])
        verb = "is" if len(critical_titles[:3]) == 1 else "are"
        return _object_detail_row("Next question", f"Next question: {summary} {verb} not listed yet.", "High")
    important_titles = [
        str(row.get("title") or "").strip()
        for row in missing_rows
        if str(row.get("tag") or "").strip().lower() == "important" and str(row.get("title") or "").strip()
    ]
    if important_titles:
        summary = _property_human_join(important_titles[:3])
        return _object_detail_row("Next question", f"Next question: {summary}.", "Medium")
    return None


def _property_packet_everyday_fit_rows(
    *,
    facts: dict[str, object],
    preferences: dict[str, object],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    family_context = _property_family_context_active(preferences)
    everyday_specs = (
        (
            "nearest_supermarket_m",
            "Supermarket",
            "Errands",
            False,
            ("nearest_supermarket_name", "supermarket_name"),
            ("nearest_supermarket_source", "supermarket_source"),
        ),
        (
            "nearest_playground_m",
            "Playground",
            "Family",
            True,
            ("nearest_playground_name", "playground_name"),
            ("nearest_playground_source", "playground_source"),
        ),
        ("nearest_library_m", "Library", "Family", True, ("nearest_library_name", "library_name"), ("nearest_library_source", "library_source")),
        ("nearest_zoo_m", "Zoo", "Family", True, ("nearest_zoo_name", "zoo_name"), ("nearest_zoo_source", "zoo_source")),
        (
            "nearest_medical_care_m",
            "Medical care",
            "Family",
            True,
            ("nearest_medical_care_name", "medical_care_name"),
            ("nearest_medical_care_source", "medical_care_source"),
        ),
        ("nearest_market_m", "Market", "District life", False, ("nearest_market_name", "market_name"), ("nearest_market_source", "market_source")),
        (
            "nearest_hardware_store_m",
            "Baumarkt",
            "Practical",
            False,
            ("nearest_hardware_store_name", "hardware_store_name"),
            ("nearest_hardware_store_source", "hardware_store_source"),
        ),
        (
            "nearest_shopping_center_m",
            "Shopping center",
            "Errands",
            False,
            ("nearest_shopping_center_name", "shopping_center_name"),
            ("nearest_shopping_center_source", "shopping_center_source"),
        ),
        (
            "nearest_shopping_street_m",
            "Flaniermeile",
            "City life",
            False,
            ("nearest_shopping_street_name", "shopping_street_name"),
            ("nearest_shopping_street_source", "shopping_street_source"),
        ),
        ("nearest_theatre_m", "Theatre", "Culture", False, ("nearest_theatre_name", "theatre_name"), ("nearest_theatre_source", "theatre_source")),
        (
            "nearest_public_pool_m",
            "Public pool",
            "Family",
            True,
            ("nearest_public_pool_name", "public_pool_name"),
            ("nearest_public_pool_source", "public_pool_source"),
        ),
        (
            "nearest_subway_m",
            "Underground",
            "Transit",
            False,
            ("nearest_subway_name", "subway_station_name"),
            ("nearest_subway_source", "subway_source"),
        ),
    )
    for key, title, tag, family_only, name_keys, source_keys in everyday_specs:
        if family_only and not family_context:
            continue
        detail = _property_research_distance_detail(
            facts,
            distance_key=key,
            name_keys=name_keys,
            source_keys=source_keys,
        )
        if not detail:
            continue
        rows.append(_object_detail_row(title, detail, tag))
    if bool(preferences.get("enable_commute_research")):
        commute_rows: list[str] = []
        for key, label in (
            ("max_commute_minutes_transit", "Transit"),
            ("max_commute_minutes_bike", "Bike"),
            ("max_commute_minutes_drive", "Car"),
            ("max_commute_minutes_walk", "Walk"),
        ):
            try:
                minutes = int(float(preferences.get(key) or 0))
            except Exception:
                minutes = 0
            if minutes > 0:
                commute_rows.append(f"{label} <= {minutes} min")
        if commute_rows:
            rows.append(_object_detail_row("Commute fit", " | ".join(commute_rows), "Reachability"))
    return rows


def _property_packet_risk_fit_rows(
    *,
    facts: dict[str, object],
    preferences: dict[str, object],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for flag, title, detail in (
        ("air_quality_risk", "Air quality", "Pollution and respiratory comfort need a closer look."),
        ("crime_risk", "Safety", "The local safety pattern needs a closer look."),
        ("parking_pressure_risk", "Parking pressure", "Street parking needs a closer look when no garage is included."),
        ("drinking_water_risk", "Water quality", "Water source and groundwater context need a closer look."),
        ("cesspit_risk", "Senkgrube or septic", "Recurring cost, maintenance, and smell burden need a closer look."),
        ("winter_access_risk", "Winter access", "Snow, slope, and seasonal driveability need a closer look."),
        ("flood_risk", "Flood exposure", "Historic flooding, runoff, or zone risk need a closer look."),
    ):
        if bool(facts.get(flag)):
            rows.append(_object_detail_row(title, detail, "Risk"))
    if bool(preferences.get("prefer_good_air_quality")) and not bool(facts.get("air_quality_risk")):
        rows.append(_object_detail_row("Air-quality check", "The brief asks for good air quality, so the local burden still needs a closer look.", "To check"))
    if bool(preferences.get("prefer_low_crime_area")) and not bool(facts.get("crime_risk")):
        rows.append(_object_detail_row("Safety check", "The brief asks for a lower-crime area, so the quarter pattern still needs a closer look.", "To check"))
    if bool(preferences.get("require_parking_pressure_check")) and not bool(facts.get("garage")) and not bool(facts.get("parking_pressure_risk")):
        rows.append(_object_detail_row("Parking check", "No garage is listed, so evening street parking still needs a closer look.", "To check"))
    if bool(preferences.get("require_drinking_water_quality_research")) and not bool(facts.get("drinking_water_risk")):
        rows.append(_object_detail_row("Water-source check", "The brief asks for water-source and groundwater context.", "To check"))
    if bool(preferences.get("avoid_cesspit_or_septic_risk")) and not bool(facts.get("cesspit_risk")):
        rows.append(_object_detail_row("Senkgrube check", "The brief asks to avoid Senkgrube or septic burden, so the infrastructure should be checked.", "To check"))
    if bool(preferences.get("require_winter_access_research")) and not bool(facts.get("winter_access_risk")):
        rows.append(_object_detail_row("Winter-access check", "The brief asks for snow and slope driveability context.", "To check"))
    if bool(preferences.get("avoid_flood_risk_area")) and not bool(facts.get("flood_risk")):
        rows.append(_object_detail_row("Flood check", "The brief asks to avoid flood exposure, so runoff and flood-zone history should be checked.", "To check"))
    return rows


def _property_packet_decision_rows(
    *,
    candidate: dict[str, object],
    match_reasons: list[str],
    mismatch_reasons: list[str],
    missing_rows: list[dict[str, str]],
    facts: dict[str, object] | None = None,
    preferences: dict[str, object] | None = None,
) -> list[dict[str, str]]:
    fact_payload = facts if isinstance(facts, dict) else {}
    preference_payload = preferences if isinstance(preferences, dict) else {}
    recommendation_key = str(candidate.get("recommendation") or candidate.get("tag") or "candidate").replace("_", " ").strip().lower()
    recommendation = {
        "shortlist": "Keep it high on the shortlist",
        "review": "Keep it in review",
        "candidate": "Keep it under review",
        "mention": "Check it after the top homes",
        "view if compelling": "Clear up the missing details before spending more time on it",
        "ask for clarification": "Clear up the missing details before spending more time on it",
        "reject": "Drop it unless new details change the file",
        "drop": "Drop it unless new details change the file",
    }.get(recommendation_key, recommendation_key.title() or "Keep it under review")
    rows = _property_packet_positive_fact_rows(
        facts=fact_payload,
        preferences=preference_payload,
        match_reasons=match_reasons,
    )
    if mismatch_reasons:
        rows.append(_object_detail_row("Watch out", mismatch_reasons[0], "Risk"))
    else:
        missing_summary_row = _property_packet_missing_summary_row(missing_rows)
        if missing_summary_row is not None:
            rows.append(missing_summary_row)
    rows.append(_object_detail_row("Next", recommendation, "Action"))
    return rows


def _property_investment_research_access_level(preferences: dict[str, object], commercial: dict[str, object], *, requested: bool) -> str:
    if str(preferences.get("listing_mode") or "").strip().lower() != "buy":
        return "off"
    if not requested:
        return "off"
    level = str(commercial.get("investment_research_level") or "none").strip().lower() or "none"
    return level


def _property_investment_risk_rows(facts: dict[str, object], snapshot: dict[str, object]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not str(facts.get("street_address") or "").strip():
        rows.append(_object_detail_row("Exact address missing", "Neighbourhood and comparison reads stay thinner until the exact address is available.", "High"))
    if not str(facts.get("heating_type") or "").strip():
        rows.append(_object_detail_row("Heating type still unknown", "Yield assumptions can be wrong if the heating setup drives renovation or tenant demand risk.", "Medium"))
    occupancy = str(facts.get("occupancy_status") or "").strip().lower()
    if occupancy:
        rows.append(_object_detail_row("Occupancy", str(facts.get("occupancy_status") or "").strip(), "Risk" if any(token in occupancy for token in ("occup", "vermiet", "bewohn", "uthyrd", "zamieszk")) else "Watch"))
    payback_years = snapshot.get("payback_years")
    if isinstance(payback_years, (int, float)) and float(payback_years) > 35.0:
        rows.append(_object_detail_row("Long payback horizon", f"Estimated payback is about {float(payback_years):.1f} years at current rent assumptions.", "Medium"))
    return rows


def _property_investment_context_rows(
    facts: dict[str, object],
    preferences: dict[str, object],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    risk_rows: list[dict[str, str]] = []
    listing_mode = str(preferences.get("listing_mode") or "").strip().lower()
    provider_group = str(facts.get("provider_group") or "").strip().lower()
    provider_channel = str(facts.get("provider_channel") or "").strip()
    marketing_type = str(facts.get("marketing_type") or "").strip()
    availability_label = str(facts.get("availability_label") or facts.get("move_in") or "").strip()
    court = str(facts.get("court") or "").strip()
    court_file_reference = str(facts.get("court_file_reference") or "").strip()
    valuation_display = str(facts.get("valuation_display") or "").strip()
    reserve_display = str(facts.get("reserve_price_display") or "").strip()
    occupancy = str(facts.get("occupancy_status") or "").strip()
    registration_count = 0
    try:
        registration_count = int(float(facts.get("registration_count") or 0))
    except Exception:
        registration_count = 0

    if provider_group == "genossenschaften_at":
        provider_label = provider_channel.replace("_", " ").strip().title() if provider_channel else "Genossenschaften"
        rows.append(_object_detail_row("Source", f"{provider_label} cooperative listing.", "Source"))
        if marketing_type:
            rows.append(_object_detail_row("Offer posture", marketing_type, "Source"))
            if listing_mode == "buy" and marketing_type.lower().startswith("miet"):
                risk_rows.append(
                    _object_detail_row(
                        "Rental cooperative listing",
                        "This candidate is coming through a rental/cooperative listing while the brief is in buy mode. Treat the numbers as weak until the acquisition path is clear.",
                        "High",
                    )
                )
        if availability_label:
            rows.append(_object_detail_row("Delivery timing", availability_label, "Timing"))
        if registration_count > 0:
            rows.append(_object_detail_row("Applicant pressure", f"{registration_count:,} registrations or applicants were visible on the source page.", "Demand"))
            if registration_count >= 10000:
                risk_rows.append(_object_detail_row("Extremely high applicant pressure", "Competition on this cooperative listing is already very high, so practical conversion odds may be weak even if the fit looks decent.", "High"))
            elif registration_count >= 1000:
                risk_rows.append(_object_detail_row("High applicant pressure", "Competition on this cooperative listing is already meaningful. Keep conversion risk in mind before overvaluing the headline fit.", "Medium"))

    if court or court_file_reference or valuation_display or reserve_display:
        if court:
            rows.append(_object_detail_row("Court process", court, "Auction"))
        if court_file_reference:
            rows.append(_object_detail_row("Case reference", court_file_reference, "Auction"))
        if valuation_display:
            rows.append(_object_detail_row("Judicial valuation", valuation_display, "Auction"))
        if reserve_display:
            rows.append(_object_detail_row("Reserve or deposit", reserve_display, "Auction"))
        risk_rows.append(
            _object_detail_row(
                "Judicial sale diligence",
                "This candidate is coming from a judicial or foreclosure source. Check occupancy, legal encumbrances, and auction terms before treating the apparent discount as real.",
                "High",
            )
        )
        if occupancy:
            rows.append(_object_detail_row("Recorded occupancy", occupancy, "Auction"))

    return rows, risk_rows


def _property_investment_research_rows(
    *,
    property_url: str,
    facts: dict[str, object],
    preferences: dict[str, object],
    commercial: dict[str, object],
    requested: bool,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    access_level = _property_investment_research_access_level(preferences, commercial, requested=requested)
    if access_level == "off":
        return [], []
    if access_level == "none":
        return [
            _object_detail_row(
                "Upgrade required",
                "Investment numbers are reserved for paid investment tiers. The current free tier does not run the buy-side calculation.",
                "Locked",
            )
        ], []
    context_rows, context_risk_rows = _property_investment_context_rows(facts, preferences)
    current_price_eur = _property_investment_price_eur(facts)
    current_area_sqm = _property_investment_area_sqm(facts)
    currency_code = _property_currency_code_from_facts(facts)
    location_seed = _property_investment_location_seed(facts, preferences)
    if not isinstance(current_price_eur, float) or not isinstance(current_area_sqm, float) or not location_seed:
        return context_rows + [
            _object_detail_row(
                "Investment research is waiting on core facts",
                "The packet still needs a credible buy price, area, and location before comp and yield work can run.",
                "Pending",
            )
        ], context_risk_rows
    selected_platforms = ",".join(str(value or "").strip() for value in (preferences.get("selected_platforms") or []) if str(value or "").strip())
    snapshot = _property_investment_research_snapshot(
        property_url=property_url,
        country_code=str(preferences.get("country_code") or "").strip() or "AT",
        location_query=location_seed,
        selected_platforms_csv=selected_platforms,
        current_price_eur=current_price_eur,
        current_area_sqm=current_area_sqm,
        research_level=access_level,
    )
    if not snapshot:
        return context_rows + [
            _object_detail_row(
                "Investment research could not build a benchmark yet",
                "No usable market samples were recovered from the current provider set for this location.",
                "Pending",
            )
        ], context_risk_rows
    rows: list[dict[str, str]] = context_rows + [
        _object_detail_row(
            "Current price base",
            (
                f"{_property_money_amount_label(current_price_eur, currency_code=currency_code)} over {current_area_sqm:.1f} m2 "
                f"({_property_money_amount_label(float(snapshot.get('current_price_per_sqm_eur') or 0.0), currency_code=currency_code)}/m2)"
            ),
            "Base",
        ),
        _object_detail_row("Comparable buy samples", f"{int(snapshot.get('buy_sample_count') or 0)} listings", "Comps"),
        _object_detail_row("Comparable rent samples", f"{int(snapshot.get('rent_sample_count') or 0)} listings", "Comps"),
    ]
    underwriting = _property_investment_underwriting_payload(
        title=str(facts.get("listing_title") or facts.get("title") or property_url).strip() or property_url,
        summary=summarize_property_description_copy(facts.get("summary") or facts.get("description_text") or ""),
        facts=facts,
        preferences=preferences,
        snapshot=snapshot,
    )
    if underwriting:
        rows.append(
            _object_detail_row(
                "Investment score",
                f"{underwriting.get('score_display') or ''} | {underwriting.get('underwriting_summary') or 'Partial details'}",
                str(underwriting.get("score_bucket_label") or "Mixed"),
            )
        )
        external_model = dict(underwriting.get("external_model") or {}) if isinstance(underwriting.get("external_model"), dict) else {}
        if external_model:
            rows.append(
                _object_detail_row(
                    "External data",
                    " | ".join(
                        part
                        for part in (
                            str(underwriting.get("feed_status_label") or "").strip(),
                            str(underwriting.get("feed_status_detail") or "").strip(),
                        )
                        if part
                    ) or "External data is still being prepared from the listing details.",
                    str(external_model.get("status_label") or "Mixed"),
                )
            )
    market_buy = snapshot.get("market_buy_per_sqm_eur")
    delta_pct = snapshot.get("market_buy_delta_pct")
    if isinstance(market_buy, (int, float)):
        detail = f"Market buy benchmark is about {_property_money_amount_label(float(market_buy), currency_code=currency_code)}/m2."
        if isinstance(delta_pct, (int, float)):
            direction = "below" if float(delta_pct) < 0 else "above"
            detail = f"{detail} This listing sits {abs(float(delta_pct)):.1f}% {direction} that benchmark."
        rows.append(_object_detail_row("Buy-side benchmark", detail, "Value"))
    expected_rent = snapshot.get("expected_monthly_rent_eur")
    gross_yield = snapshot.get("gross_yield_pct")
    payback_years = snapshot.get("payback_years")
    if isinstance(expected_rent, (int, float)):
        rows.append(
            _object_detail_row(
                "Expected monthly rent",
                f"About {_property_money_amount_label(float(expected_rent), currency_code=currency_code)} ({currency_code} {float(snapshot.get('market_rent_per_sqm_eur') or 0.0):.2f}/m2)",
                "Yield",
            )
        )
    if isinstance(gross_yield, (int, float)):
        rows.append(_object_detail_row("Gross yield", f"About {float(gross_yield):.2f}% before vacancy, tax, and capex.", "Yield"))
    net_yield = underwriting.get("net_yield_pct")
    if isinstance(net_yield, (int, float)):
        rows.append(_object_detail_row("Net yield", f"About {float(net_yield):.2f}% after tax, opex, vacancy, and capex reserves.", "Yield"))
    cap_rate = underwriting.get("cap_rate_pct")
    if isinstance(cap_rate, (int, float)):
        rows.append(_object_detail_row("Cap rate", f"About {float(cap_rate):.2f}% on current acquisition cost assumptions.", "Yield"))
    dscr = underwriting.get("dscr")
    if isinstance(dscr, (int, float)):
        rows.append(_object_detail_row("Debt coverage", f"About {float(dscr):.2f}x on the current financing model.", "Financing"))
    if isinstance(payback_years, (int, float)):
        rows.append(_object_detail_row("Payback horizon", f"About {float(payback_years):.1f} years on gross rent assumptions.", "Yield"))
    for dimension in list(underwriting.get("dimensions") or [])[:7]:
        if not isinstance(dimension, dict):
            continue
        rows.append(
            _object_detail_row(
                str(dimension.get("label") or "Underwriting dimension").strip(),
                f"{int(float(dimension.get('score') or 0))}/100 | {str(dimension.get('tooltip') or '').strip()}",
                str(dimension.get("bucket_label") or "Mixed").strip(),
            )
        )
    if access_level == "preview":
        rows.append(_object_detail_row("Preview tier limit", "Plus only returns the benchmark headline. Agent unlocks the fuller risk and diligence pass.", "Upgrade"))
        return rows, context_risk_rows
    external_model = dict(underwriting.get("external_model") or {}) if isinstance(underwriting.get("external_model"), dict) else {}
    if external_model:
        financing = dict(external_model.get("financing") or {}) if isinstance(external_model.get("financing"), dict) else {}
        taxes = dict(external_model.get("taxes") or {}) if isinstance(external_model.get("taxes"), dict) else {}
        operating = dict(external_model.get("operating_costs") or {}) if isinstance(external_model.get("operating_costs"), dict) else {}
        if isinstance(external_model.get("acquisition_costs_eur"), (int, float)):
            rows.append(_object_detail_row("Acquisition cost base", f"About {_property_money_amount_label(float(external_model.get('acquisition_costs_eur')), currency_code=currency_code)} including transfer tax and registry fees.", "Base"))
        if isinstance(taxes.get("property_transfer_tax_pct"), (int, float)):
            rows.append(_object_detail_row("Transfer tax model", f"{float(taxes.get('property_transfer_tax_pct')):.2f}% transfer tax and {float(taxes.get('land_registry_fee_pct') or 0.0):.2f}% registry fee.", str(taxes.get("source_label") or "Tax model")))
        if isinstance(operating.get("annual_operating_costs_eur"), (int, float)):
            rows.append(_object_detail_row("Operating cost model", f"About {_property_money_amount_label(float(operating.get('annual_operating_costs_eur')), currency_code=currency_code)} per year ({float(operating.get('operating_cost_ratio_pct') or 0.0):.1f}% of rent when rent is known).", str(operating.get("source_label") or "Operating cost model")))
        if isinstance(financing.get("interest_rate_pct"), (int, float)):
            rows.append(_object_detail_row("Financing model", f"{float(financing.get('interest_rate_pct')):.2f}% over {int(float(financing.get('loan_term_years') or 0))} years with about {_property_money_amount_label(float(financing.get('annual_debt_service_eur') or 0.0), currency_code=currency_code)} annual debt service.", str(financing.get("source_label") or "Financing model")))
    risk_rows = context_risk_rows + _property_investment_risk_rows(facts, snapshot)
    if isinstance(snapshot.get("buy_samples"), list) and snapshot["buy_samples"]:
        top_buy = snapshot["buy_samples"][0]
        rows.append(_object_detail_row("Closest buy comp", f"{top_buy.get('title')} | {top_buy.get('per_sqm_eur')} {currency_code}/m2 via {top_buy.get('source_label')}", "Comp"))
    if isinstance(snapshot.get("rent_samples"), list) and snapshot["rent_samples"]:
        top_rent = snapshot["rent_samples"][0]
        rows.append(_object_detail_row("Closest rent comp", f"{top_rent.get('title')} | {top_rent.get('per_sqm_eur')} {currency_code}/m2 via {top_rent.get('source_label')}", "Comp"))
    return rows, risk_rows
