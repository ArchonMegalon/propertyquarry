from __future__ import annotations

import base64
import html
import hashlib
import io
import json
import math
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
from app.services.property_customer_copy import (
    normalize_property_fit_note,
    sanitize_property_marketing_copy,
    summarize_property_description_copy,
)
from app.product.property_location_research import (
    _property_research_boundary_record,
    _property_research_geojson_outer_rings,
)
from app.product.service import (
    _property_alert_fit_summary,
    _property_visible_mismatch_reasons,
)
from app.api.routes.landing_property_saved_searches import (
    build_agent_management_rows,
    build_property_search_agents,
    select_property_search_agent,
)
from app.api.routes.landing_property_workspace_helpers import (
    _property_candidate_display_facts,
)
from app.api.routes.landing_property_search_posture import (
    build_property_market_summary_items,
)
from app.api.routes.landing_property_shortlist_panel import (
    build_property_source_rows,
    build_property_shortlist_panel,
)
from app.product.property_surface_state import (
    build_property_empty_outcome_summary,
    build_property_previous_run_summary,
    build_property_search_form_state_snapshot,
    build_property_shortlist_snapshot,
    build_property_workbench_candidate_snapshot,
    effective_property_listing_mode,
    normalized_property_search_goal,
    property_mode_visibility_label,
)
from app.api.routes.landing_property_surface_contracts import (
    PropertyDecisionWorkbenchBriefContract,
    PropertyDecisionWorkbenchContract,
    PropertyDecisionWorkbenchRunContract,
    PropertySurfacePayloadContract,
    PropertySurfaceScope,
)

_PROPERTY_MAP_PREVIEW_RENDER_LOCK = threading.Lock()
_PROPERTY_MAP_PREVIEW_RENDER_IN_FLIGHT: set[str] = set()
_PROPERTY_MAP_PREVIEW_STYLE_VERSION = "flagship_map_v12_focus_card_contrast"
_PROPERTY_MAP_PREVIEW_SELECTED_FILL = (194, 42, 48, 46)
_PROPERTY_MAP_PREVIEW_COVERAGE_FILL = (194, 42, 48, 24)
_PROPERTY_MAP_PREVIEW_SECONDARY_FILL = (194, 42, 48, 24)
_PROPERTY_MAP_PREVIEW_SELECTED_STROKE = (132, 30, 36, 126)
_PROPERTY_MAP_PREVIEW_BOUNDARY_STROKE = (68, 62, 55, 72)
_PROPERTY_MAP_PREVIEW_HALO = (255, 250, 242, 112)
_PROPERTY_MAP_PREVIEW_ROUTE_STROKE = (193, 120, 34, 230)
_PROPERTY_MAP_PREVIEW_ROUTE_HALO = (255, 248, 241, 214)
_PROPERTY_MAP_PREVIEW_ROUTE_START_FILL = (255, 248, 241, 255)
_PROPERTY_MAP_PREVIEW_ROUTE_END_FILL = (72, 145, 92, 255)
_PROPERTY_MAP_PREVIEW_ROUTE_MARKER_STROKE = (126, 51, 33, 156)


PROPERTY_FURNITURE_STYLE_CATALOG: tuple[dict[str, str], ...] = (
    {
        "value": "warm_scandi",
        "label": "Warm Scandinavian",
        "badge": "Default",
        "detail": "The default: calm, bright, natural wood, realistic family-home staging.",
        "prompt": "warm Scandinavian staging, bright neutral textiles, light oak, clean storage, realistic family-home warmth",
        "example_tone": "linear-gradient(135deg, #f5efe3 0%, #d9c6a4 48%, #b7c4bd 100%)",
        "example_caption": "Light oak, linen, calm family warmth.",
    },
    {
        "value": "ikea_practical",
        "label": "IKEA practical",
        "badge": "Efficient",
        "detail": "Affordable modular storage, simple lines, rental-friendly, tidy and believable.",
        "prompt": "IKEA-inspired practical modular furniture, bright storage, simple rental-friendly pieces, realistic affordable staging",
        "example_tone": "linear-gradient(135deg, #f7f4e8 0%, #2f6fb3 54%, #f4c542 100%)",
        "example_caption": "Modular storage, bright, practical, clean.",
    },
    {
        "value": "urban_jungle",
        "label": "Urban jungle",
        "badge": "Lush",
        "detail": "Plants, tactile natural materials, softer light, lived-in but not cluttered.",
        "prompt": "urban jungle interior with healthy plants, rattan, warm wood, linen, soft daylight, lived-in but uncluttered",
        "example_tone": "linear-gradient(135deg, #e7dcc5 0%, #4d7c59 52%, #1f3d2b 100%)",
        "example_caption": "Plants, rattan, warm wood, soft daylight.",
    },
    {
        "value": "landhaus",
        "label": "Landhaus",
        "badge": "Classic",
        "detail": "Classic country-house warmth, wood, linen, ceramics, softer traditional details.",
        "prompt": "Austrian Landhaus country-home staging, warm timber, linen, ceramics, classic comfortable furniture, premium realistic finish",
        "example_tone": "linear-gradient(135deg, #ead8bd 0%, #9b6b43 50%, #6d7f52 100%)",
        "example_caption": "Austrian country warmth, timber, ceramics.",
    },
    {
        "value": "gilded_penthouse",
        "label": "Trump gold",
        "badge": "Playful luxe",
        "detail": "A tongue-in-cheek Trump-style gold, marble, brass, tower-lobby penthouse look with maximalist drama.",
        "prompt": "playful Trump-style gold maximalist penthouse staging with polished marble, brass, gold accents, oversized classical details, photorealistic but tasteful",
        "example_tone": "linear-gradient(135deg, #fff4bd 0%, #c79a31 43%, #2a2117 100%)",
        "example_caption": "Gold, marble, brass, maximalist tower drama.",
    },
)

from app.api.routes.landing_property_workspace_payload import (
    property_workspace_payload as build_property_workspace_payload,
)
from app.api.routes.landing_property_workspace_helpers import (
    _artifact_receipt_rows,
    _candidate_detail_sections,
    _compact_provider_label,
    _delivery_proof_rows,
    _group_property_provider_options,
    _official_risk_posture_rows,
    _property_candidate_directions_url,
    _property_candidate_is_rankable,
    _property_candidate_maps_url,
    _property_candidate_orientation_preview,
    _property_candidate_preview_image,
    _property_candidate_route_evidence,
    _property_counterfactual_rows,
    _property_family_filters_active,
    _property_market_filter_capabilities,
    _property_progress_route_preview_rows,
    _property_run_reliability_summary,
    _property_search_guard_rows,
    _property_search_worker_slots,
    _property_suppression_rows,
)
from app.services.property_market_catalog import currency_code_for_country


def _csv_values(value: object) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for raw in str(value or "").split(","):
        normalized = str(raw or "").strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        values.append(normalized)
    return values


def _normalize_property_type_values(value: object) -> list[str]:
    """Normalize property_type payloads from single, list, or comma-separated forms."""
    values: list[str] = []
    if isinstance(value, (list, tuple, set)):
        raw_values = [str(item or "") for item in value]
    elif isinstance(value, str) and "," in value:
        raw_values = [item.strip() for item in value.split(",")]
    else:
        raw_values = [str(value or "")]

    for item in raw_values:
        normalized = item.strip().lower()
        if not normalized:
            continue
        if normalized == "any" and len(raw_values) > 1:
            values = [value for value in values if value != "any"]
            continue
        if normalized not in values:
            values.append(normalized)

    if not values:
        values = ["any"]
    return values


_PROPERTY_PROVIDER_SUFFIX_MARKERS = (
    "willhaben",
    "immoscout",
    "immobilienscout",
    "immowelt",
    "idealista",
    "remax",
    "immobilien",
)

_PROPERTY_PROVIDER_MARKETING_PATTERNS = (
    r"\.?\s*Wählen Sie aus\s+\d[\d.,\s]*(?:Angeboten|Immobilien|Wohnungen|Häusern|Objekten).*?$",
    r"\.?\s*Immobilien suchen und finden auf\s+.*?$",
    r"\.?\s*Choose from\s+\d[\d.,\s]*(?:listings|properties|homes|offers).*?$",
    r"\.?\s*(?:Search|Find)\s+(?:homes|properties|real estate)\s+(?:on|at)\s+.*?$",
)

_PROPERTY_SENTENCE_CASE_STOPWORDS = {
    "am",
    "an",
    "and",
    "at",
    "auf",
    "bei",
    "by",
    "das",
    "de",
    "der",
    "des",
    "die",
    "for",
    "from",
    "im",
    "in",
    "mit",
    "of",
    "on",
    "oder",
    "the",
    "to",
    "und",
    "von",
}


def _property_candidate_copy_capitalize_first_alpha(value: str) -> str:
    for index, char in enumerate(value):
        if char.isalpha():
            return f"{value[:index]}{char.upper()}{value[index + 1:]}"
    return value


def _property_candidate_copy_sentence_case_fragment(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw_letters = [char for char in raw if char.isalpha()]
    raw_upper_ratio = (sum(1 for char in raw_letters if char.isupper()) / len(raw_letters)) if raw_letters else 0.0
    if raw_upper_ratio >= 0.8:
        tokens = re.split(r"(\s+)", raw)
        normalized_tokens: list[str] = []
        alpha_token_index = 0
        for token in tokens:
            if not token or token.isspace():
                normalized_tokens.append(token)
                continue
            leading_match = re.match(r"^[^A-Za-zÄÖÜäöüß]*", token)
            trailing_match = re.search(r"[^A-Za-zÄÖÜäöüß]*$", token)
            leading = leading_match.group(0) if leading_match else ""
            trailing = trailing_match.group(0) if trailing_match else ""
            end_index = len(token) - len(trailing) if trailing else len(token)
            core = token[len(leading):end_index]
            letters = [char for char in core if char.isalpha()]
            if not letters:
                normalized_tokens.append(token)
                continue
            lowered = core.lower()
            lowered_key = lowered.casefold()
            if len(letters) <= 2 and core.upper() == core and lowered_key not in _PROPERTY_SENTENCE_CASE_STOPWORDS:
                normalized_core = core
            else:
                normalized_core = lowered
                if alpha_token_index == 0 or lowered_key not in _PROPERTY_SENTENCE_CASE_STOPWORDS:
                    normalized_core = _property_candidate_copy_capitalize_first_alpha(normalized_core)
            normalized_tokens.append(f"{leading}{normalized_core}{trailing}")
            alpha_token_index += 1
        return "".join(normalized_tokens).strip()

    tokens = re.split(r"(\s+)", raw)
    normalized_tokens: list[str] = []
    for token in tokens:
        if not token or token.isspace():
            normalized_tokens.append(token)
            continue
        leading_match = re.match(r"^[^A-Za-zÄÖÜäöüß]*", token)
        trailing_match = re.search(r"[^A-Za-zÄÖÜäöüß]*$", token)
        leading = leading_match.group(0) if leading_match else ""
        trailing = trailing_match.group(0) if trailing_match else ""
        end_index = len(token) - len(trailing) if trailing else len(token)
        core = token[len(leading):end_index]
        letters = [char for char in core if char.isalpha()]
        if not letters:
            normalized_tokens.append(token)
            continue
        upper_ratio = (sum(1 for char in letters if char.isupper()) / len(letters)) if letters else 0.0
        if len(letters) >= 3 and upper_ratio >= 0.8:
            core = core.lower()
        normalized_tokens.append(f"{leading}{core}{trailing}")
    normalized = "".join(normalized_tokens).strip()
    return _property_candidate_copy_capitalize_first_alpha(normalized)


def _property_candidate_copy_strip_provider_marketing(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    if " - " in text:
        head, tail = text.rsplit(" - ", 1)
        tail_normalized = tail.strip().lower()
        if tail_normalized and (
            "." in tail_normalized or any(marker in tail_normalized for marker in _PROPERTY_PROVIDER_SUFFIX_MARKERS)
        ):
            text = head.strip()
    for pattern in _PROPERTY_PROVIDER_MARKETING_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
    had_promo_separators = bool(re.search(r"\s+\|\s+|\s+I\s+", text))
    text = re.sub(r"\s+\|\s+", " · ", text)
    text = re.sub(r"\s+I\s+", " · ", text)
    if " · " in text:
        text = " · ".join(
            fragment
            for fragment in (
                _property_candidate_copy_sentence_case_fragment(part)
                for part in re.split(r"\s*·\s*", text)
            )
            if fragment
        )
    text = text.strip(" -·|")
    if had_promo_separators and text and not re.search(r"[.!?]$", text):
        text = f"{text}."
    return text


def _clean_property_candidate_copy(value: object) -> str:
    text = _property_candidate_copy_strip_provider_marketing(value)
    if not text:
        return ""
    if re.match(r"^Personal fit \d+/100(?:\s*·.*)?$", text, flags=re.IGNORECASE):
        return ""
    noisy_exact = {
        "The listing does not provide a live 360 source, so remote screening has higher uncertainty.",
    }
    if text in noisy_exact:
        return ""
    replacements = {
        "Provider-ranked fallback candidate kept because strict personal-fit scoring produced no shortlist.": "Included because no stronger fit cleared the shortlist.",
        "· ask for clarification": "",
        "ask for clarification": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    if re.search(
        r"(?i)\b(?:chosen ahead of the next option because|ranked ahead of the next option because|it scored \d+(?:\.\d+)? points higher|next option)\b",
        text,
    ):
        normalized_fit_note = normalize_property_fit_note(text)
        if normalized_fit_note:
            text = normalized_fit_note
    pattern_replacements = (
        (r"(?i)\bCurrent ranking bar:\s*\d+\s*/\s*100\b[;:,-]?\s*", ""),
        (r"(?i)\bCurrent ranking bar:\s*", ""),
        (r"(?i)\beven below the (?:current|saved) ranking bar\b", "in the full list"),
        (r"(?i)\bbelow the (?:current|saved) ranking bar\b", "in the full list"),
        (r"(?i)\bbelow the current bar\b", "in the full list"),
        (r"(?i)\bTurn the ranking bar down or off\b", "Start a fresh search"),
        (r"(?i)\bLower the ranking bar or turn it off\b", "Start a fresh search"),
        (r"(?i)\bTurn bar off\b", "Refresh search"),
        (r"(?i)\bCurrent score filter:\s*\d+\s*/\s*100\b[;:,-]?\s*", ""),
        (r"(?i)\bCurrent score filter:\s*", ""),
        (r"(?i)\bCurrent score ceiling:\s*\d+\s*/\s*100\b[;:,-]?\s*", ""),
        (r"(?i)\bCurrent score ceiling:\s*", ""),
        (r"(?i)\beven below the (?:current|saved) score filter\b", "in the full list"),
        (r"(?i)\bbelow the (?:current|saved) score filter\b", "in the full list"),
        (r"(?i)\bbelow the current score ceiling\b", "in the full list"),
        (r"(?i)\bTurn the score filter down or off\b", "Start a fresh search"),
        (r"(?i)\bLower the score filter or turn it off\b", "Start a fresh search"),
    )
    for pattern, replacement in pattern_replacements:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"^(?:[.,;:\s]+)", "", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"([.?!]){2,}", r"\1", text)
    text = " ".join(text.split()).strip(" ,;:-")
    return text.strip()


def _clean_property_candidate_detail_copy(value: object) -> str:
    cleaned = _clean_property_candidate_copy(value)
    if not cleaned:
        return ""
    summarized = summarize_property_description_copy(cleaned)
    return summarized or cleaned


def _property_type_selection_allows_land(property_types: list[str]) -> bool:
    normalized = {str(item or "").strip().lower() for item in list(property_types or []) if str(item or "").strip()}
    if not normalized or "any" in normalized:
        return False
    return bool(normalized.intersection({"land", "baugrund", "grundstück", "grundstueck"}))


def _property_type_selection_is_land_only(property_types: list[str]) -> bool:
    normalized = {str(item or "").strip().lower() for item in list(property_types or []) if str(item or "").strip()}
    if not normalized or "any" in normalized:
        return False
    return normalized.issubset({"land", "baugrund", "grundstück", "grundstueck"})


def _property_customer_source_summary(source: dict[str, object]) -> dict[str, object]:
    source_row = dict(source or {})
    def _to_int(value: object) -> int:
        try:
            return max(0, int(float(str(value or "").strip())))
        except Exception:
            return 0
    return {
        "source_label": str(source_row.get("source_label") or source_row.get("platform") or "Provider").strip() or "Provider",
        "platform": str(source_row.get("platform") or "").strip(),
        "provider_family": str(source_row.get("provider_family") or "").strip(),
        "source_status": str(source_row.get("source_status") or source_row.get("status") or "Scanned").strip(),
        "status": str(source_row.get("status") or source_row.get("source_status") or "").strip(),
        "message": str(source_row.get("message") or "").strip(),
        "error": str(source_row.get("error") or "").strip(),
        "listing_total": _to_int(source_row.get("listing_total") or source_row.get("scanned_listing_total") or 0),
        "scanned_listing_total": _to_int(source_row.get("scanned_listing_total") or source_row.get("listing_total") or 0),
        "ranked_total": _to_int(source_row.get("ranked_total") or source_row.get("listing_total") or 0),
        "ranked_candidate_total": _to_int(source_row.get("ranked_candidate_total") or source_row.get("ranked_total") or 0),
        "high_fit_total": _to_int(source_row.get("high_fit_total") or 0),
        "filtered_low_fit_total": _to_int(source_row.get("filtered_low_fit_total") or 0),
        "score_demoted_total": _to_int(source_row.get("score_demoted_total") or source_row.get("filtered_low_fit_total") or 0),
        "filtered_floorplan_total": _to_int(source_row.get("filtered_floorplan_total") or 0),
        "location_mismatch_reason": str(source_row.get("location_mismatch_reason") or "").strip(),
        "location_mismatch_candidate_total": _to_int(source_row.get("location_mismatch_candidate_total") or 0),
        "provider_filter_pushdown": dict(source_row.get("provider_filter_pushdown") or {})
        if isinstance(source_row.get("provider_filter_pushdown"), dict)
        else {},
        "timing_ms": dict(source_row.get("timing_ms") or {})
        if isinstance(source_row.get("timing_ms"), dict)
        else {},
    }


def _property_customer_lightweight_image_url(value: object, *, max_data_url_chars: int = 4096) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if url.lower().startswith("data:") and len(url) > max_data_url_chars:
        return ""
    return url


def _property_customer_candidate_summary(
    candidate: dict[str, object],
    *,
    preferences: dict[str, object] | None = None,
) -> dict[str, object]:
    row = dict(candidate or {})
    facts = _property_candidate_display_facts(row)
    cleaned_title = _property_result_title_display(row.get("title") or row.get("property_url") or "Property")
    if cleaned_title:
        row["title"] = cleaned_title
    else:
        row.pop("title", None)
    cleaned_fit_summary = _clean_property_candidate_detail_copy(row.get("fit_summary"))
    if cleaned_fit_summary:
        row["fit_summary"] = cleaned_fit_summary
    else:
        row.pop("fit_summary", None)
    cleaned_compare_reason = _clean_property_candidate_detail_copy(row.get("compare_reason"))
    if cleaned_compare_reason:
        row["compare_reason"] = cleaned_compare_reason
    else:
        row.pop("compare_reason", None)
    cleaned_score_demotion_reason = _clean_property_candidate_copy(row.get("score_demotion_reason"))
    if cleaned_score_demotion_reason:
        row["score_demotion_reason"] = cleaned_score_demotion_reason
    else:
        row.pop("score_demotion_reason", None)
    raw_summary = str(row.get("summary") or "").strip()
    cleaned_summary = _clean_property_candidate_copy(raw_summary)
    if cleaned_summary:
        if cleaned_summary != raw_summary and (
            "|" in raw_summary
            or re.search(r"(?i)\b(?:wählen sie aus|immobilien suchen und finden|choose from|search homes|find homes)\b", raw_summary)
        ):
            cleaned_summary = summarize_property_description_copy(cleaned_summary)
        row["summary"] = cleaned_summary
    else:
        row.pop("summary", None)
    for fact_key in (
        "description",
        "description_text",
        "object_description",
        "listing_description",
        "summary",
        "location_description",
        "location_text",
        "micro_location_summary",
        "neighborhood_description",
    ):
        if fact_key not in facts:
            continue
        safe_fact_copy = _clean_property_candidate_detail_copy(facts.get(fact_key))
        if safe_fact_copy:
            facts[fact_key] = safe_fact_copy
        else:
            facts.pop(fact_key, None)
    if facts:
        row["property_facts"] = facts
    else:
        row.pop("property_facts", None)
    normalized_mismatch_reasons = _property_visible_mismatch_reasons(
        {"mismatch_reasons_json": list(row.get("mismatch_reasons") or [])},
        facts=facts,
        preferences=preferences,
        limit=6,
    )
    if normalized_mismatch_reasons:
        row["mismatch_reasons"] = normalized_mismatch_reasons
    else:
        row.pop("mismatch_reasons", None)
    raw_fit_summary = str(row.get("fit_summary") or "").strip()
    fit_summary_needs_refresh = (
        not raw_fit_summary
        or any(
            token in raw_fit_summary.casefold()
            for token in (
                "farther away than wished",
                "less convenient",
                "too close for avoid preference",
            )
        )
    )
    if fit_summary_needs_refresh:
        refreshed_fit_summary = _property_alert_fit_summary(
            {
                "fit_score": row.get("fit_score") or row.get("assessment_fit_score") or 0.0,
                "recommendation": row.get("recommendation") or "",
                "match_reasons_json": list(row.get("match_reasons") or []),
                "mismatch_reasons_json": list(candidate.get("mismatch_reasons") or []),
            },
            facts=facts,
            preferences=preferences,
        )
        refreshed_fit_summary = _clean_property_candidate_copy(refreshed_fit_summary)
        if refreshed_fit_summary:
            row["fit_summary"] = refreshed_fit_summary
        else:
            row.pop("fit_summary", None)
    for key in ("preview_image_url", "image_url", "thumb_image_url"):
        cleaned = _property_customer_lightweight_image_url(row.get(key))
        if cleaned:
            row[key] = cleaned
        else:
            row.pop(key, None)
    if isinstance(row.get("orientation_preview"), dict):
        preview = dict(row.get("orientation_preview") or {})
        for key in ("image_url", "thumb_image_url", "preview_image_url"):
            cleaned = _property_customer_lightweight_image_url(preview.get(key))
            if cleaned:
                preview[key] = cleaned
            else:
                preview.pop(key, None)
        row["orientation_preview"] = preview
    return row


def _property_customer_candidate_is_rankable(candidate: dict[str, object]) -> bool:
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
        "suppressed",
    }
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
        if isinstance(value, (int, float)) and value != 0:
            return False
        if str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}:
            return False
    if str(candidate.get("hard_filter_reason") or "").strip():
        return False
    filter_reason = str(candidate.get("filter_reason") or "").strip().lower()
    if filter_reason in hard_filter_reasons:
        return False
    facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
    has_location_signal = any(
        str(value or "").strip()
        for value in (
            candidate.get("location"),
            candidate.get("postal_name"),
            candidate.get("district"),
            candidate.get("street_address"),
            candidate.get("exact_address"),
            facts.get("location"),
            facts.get("postal_name"),
            facts.get("district"),
            facts.get("street_address"),
            facts.get("exact_address"),
            facts.get("city"),
            facts.get("address"),
        )
    ) or any(
        value not in (None, "", 0, 0.0)
        for value in (
            candidate.get("map_lat"),
            candidate.get("map_lng"),
            facts.get("map_lat"),
            facts.get("map_lng"),
        )
    )
    has_price_signal = any(
        value not in (None, "", 0, 0.0)
        for value in (
            candidate.get("price_eur"),
            candidate.get("purchase_price_eur"),
            candidate.get("buy_price_eur"),
            facts.get("price_eur"),
            facts.get("purchase_price_eur"),
            facts.get("buy_price_eur"),
        )
    ) or any(
        str(value or "").strip()
        for value in (
            candidate.get("price_display"),
            candidate.get("purchase_price_display"),
            candidate.get("buy_price_display"),
            facts.get("price_display"),
            facts.get("purchase_price_display"),
            facts.get("buy_price_display"),
        )
    )
    has_decision_signal = any(
        str(value or "").strip()
        for value in (
            candidate.get("fit_summary"),
            candidate.get("recommendation"),
            candidate.get("review_url"),
        )
    ) or bool(list(candidate.get("match_reasons") or []))
    if not has_location_signal and not has_price_signal and not has_decision_signal:
        return False
    return True


def _property_customer_run_summary(
    summary: dict[str, object],
    *,
    preferences: dict[str, object] | None = None,
) -> dict[str, object]:
    source_rows = [
        _property_customer_source_summary(row)
        for row in list(dict(summary or {}).get("sources") or [])
        if isinstance(row, dict)
    ]
    ranked_candidates = [
        _property_customer_candidate_summary(row, preferences=preferences)
        for row in list(dict(summary or {}).get("ranked_candidates") or [])
        if isinstance(row, dict) and _property_customer_candidate_is_rankable(row)
    ]
    clean = {
        key: value
        for key, value in dict(summary or {}).items()
        if key
        not in {
            "sources",
            "research_tasks",
            "provider_quality",
            "ranked_candidates",
        }
    }
    clean["sources"] = source_rows
    clean["ranked_candidates"] = ranked_candidates
    return clean


def _sanitize_platform_catalog_for_client(platform_catalog: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    sanitized: dict[str, list[dict[str, object]]] = {}
    for country_code, options in dict(platform_catalog or {}).items():
        country_key = str(country_code or "").strip()
        if not country_key:
            continue
        rows: list[dict[str, object]] = []
        for option in list(options or []):
            if not isinstance(option, dict):
                continue
            row: dict[str, object] = {
                "value": str(option.get("value") or "").strip(),
                "label": str(option.get("label") or option.get("value") or "").strip(),
                "family": str(option.get("family") or "").strip(),
            }
            option_country_code = str(option.get("country_code") or country_key).strip().upper()
            if option_country_code:
                row["country_code"] = option_country_code
            detail = str(option.get("detail") or option.get("description") or "").strip()
            normalized_detail = detail.lower()
            if detail and "floorplans " not in normalized_detail and "filters " not in normalized_detail:
                row["detail"] = detail
            homepage_url = str(option.get("homepage_url") or "").strip()
            if homepage_url:
                row["homepage_url"] = homepage_url
            availability_note = str(option.get("availability_note") or "").strip()
            if availability_note:
                row["availability_note"] = availability_note
            if option.get("search_ready") is False:
                row["search_ready"] = False
            rows.append(row)
        sanitized[country_key] = rows
    return sanitized


def _property_result_title_display(title: object) -> str:
    text = html.unescape(str(title or ""))
    text = text.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")
    text = text.replace('\\"', '"').replace("\\'", "'")
    text = " ".join(text.split()).strip(" \t\r\n\"'`.,;")
    if not text:
        return "Property"
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        path_bits = [part for part in parsed.path.split("/") if part and part not in {"projects", "project", "id"}]
        readable = " ".join(path_bits[-2:]).replace("-", " ").replace("_", " ").strip()
        if readable and not re.fullmatch(r"\d+", readable):
            return readable.title()
        return "Property listing"
    text = sanitize_property_marketing_copy(text)
    text = re.sub(r"\s+-\s+(willhaben|immobilienscout24|immoscout|immowelt|idealista|kleinanzeigen)\b.*$", "", text, flags=re.IGNORECASE).strip()
    trailing_patterns = (
        r",\s*\d+(?:[.,]\d+)?\s*m².*$",
        r",\s*(?:€|eur|usd|chf)\s*[0-9][0-9\.\,\s-]*(?:\([^)]*\))?.*$",
        r",\s*\([^)]*\)\s*$",
    )
    changed = True
    while changed and text:
        changed = False
        for pattern in trailing_patterns:
            updated = re.sub(pattern, "", text, flags=re.IGNORECASE).strip(" ,-")
            if updated != text:
                text = updated
                changed = True
    text = text.strip(" \t\r\n\"'`.,;")
    return text or "Property"


def _merge_option_catalog(
    base: list[dict[str, str]],
    selected_values: list[str],
) -> list[dict[str, object]]:
    values = {str(item.get("value") or "").strip().lower() for item in base if str(item.get("value") or "").strip()}
    merged = list(base)
    for value in selected_values:
        normalized = str(value or "").strip()
        if not normalized or normalized.lower() in values:
            continue
        merged.append({"value": normalized, "label": normalized})
        values.add(normalized.lower())
    return merged


def _split_known_and_custom_values(
    base: list[dict[str, str]],
    selected_values: list[str],
) -> tuple[list[str], list[str]]:
    known_values = {
        str(item.get("value") or "").strip().lower()
        for item in base
        if str(item.get("value") or "").strip()
    }
    known: list[str] = []
    custom: list[str] = []
    for value in selected_values:
        normalized = str(value or "").strip()
        if not normalized:
            continue
        if normalized.lower() in known_values:
            known.append(normalized)
        else:
            custom.append(normalized)
    return known, custom


def _scope_preview_layout(country_code: str, region_code: str, options: list[dict[str, str]]) -> list[dict[str, object]]:
    total = max(1, len(options))
    columns = 3 if total > 6 else 2
    rows = max(1, (total + columns - 1) // columns)
    cell_width = 100 / columns
    cell_height = 100 / rows
    grid_rows: list[dict[str, object]] = []
    for index, option in enumerate(options):
        column = index % columns
        row = index // columns
        grid_rows.append(
            {
                "value": str(option.get("value") or "").strip(),
                "label": str(option.get("label") or option.get("value") or "").strip(),
                "detail": str(option.get("detail") or "").strip(),
                "x": (column * cell_width) + 4,
                "y": (row * cell_height) + 8,
                "width": max(18.0, cell_width - 8),
                "height": max(16.0, cell_height - 12),
            }
        )
    return grid_rows


def _svg_to_data_url(svg: str) -> str:
    encoded = urllib.parse.quote(svg, safe=":/?&=,+-_.!~*'()")
    return f"data:image/svg+xml;charset=utf-8,{encoded}"


def _scope_layout_preview_data_url(
    *,
    country_code: str,
    region_code: str,
    normalized_query: str,
    market_label: str,
    layout_rows: list[dict[str, object]],
    selected_lookup: set[str],
) -> str:
    width = 640
    height = 368
    chips: list[str] = []
    for row in layout_rows[:18]:
        value = str(row.get("value") or "").strip().lower()
        label = html.escape(str(row.get("label") or row.get("value") or "").strip())
        if not value or not label:
            continue
        x = float(row.get("x") or 0.0) / 100.0 * width
        y = float(row.get("y") or 0.0) / 100.0 * height
        chip_width = max(92.0, min((float(row.get("width") or 24.0) / 100.0 * width), 188.0))
        chip_height = max(34.0, min((float(row.get("height") or 20.0) / 100.0 * height), 54.0))
        selected = value in selected_lookup
        fill = "#c73a43" if selected else "#f4ede4"
        stroke = "#8f1f29" if selected else "#d9ccbd"
        text_fill = "#fffaf6" if selected else "#3f3630"
        chips.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{chip_width:.1f}" height="{chip_height:.1f}" rx="10" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        chips.append(
            f'<text x="{x + 12:.1f}" y="{y + (chip_height / 2) + 5:.1f}" fill="{text_fill}" '
            f'font-family="Inter, Arial, sans-serif" font-size="15" font-weight="600">{label}</text>'
        )
    title = html.escape(normalized_query or market_label or "Search area")
    subtitle = html.escape(market_label or f"{region_code} · {country_code}")
    badge = html.escape(country_code or "")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        '<defs>'
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0%" stop-color="#f6f0e8"/>'
        '<stop offset="100%" stop-color="#efe5d8"/>'
        '</linearGradient>'
        '</defs>'
        f'<rect width="{width}" height="{height}" fill="url(#bg)"/>'
        '<rect x="18" y="18" width="604" height="332" rx="18" fill="rgba(255,255,255,0.48)" stroke="#ddd0c1" stroke-width="2"/>'
        f'<text x="34" y="52" fill="#2f2a25" font-family="Inter, Arial, sans-serif" font-size="25" font-weight="700">{title}</text>'
        f'<text x="34" y="78" fill="#72665b" font-family="Inter, Arial, sans-serif" font-size="15">{subtitle}</text>'
        f'<rect x="544" y="28" width="62" height="28" rx="14" fill="#ffffff" stroke="#d9ccbd" stroke-width="1.5"/>'
        f'<text x="575" y="47" text-anchor="middle" fill="#61554b" font-family="Inter, Arial, sans-serif" font-size="13" font-weight="700">{badge}</text>'
        + "".join(chips) +
        '</svg>'
    )
    return _svg_to_data_url(svg)


def _latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    scale = 2.0 ** zoom
    tile_x = (lon + 180.0) / 360.0 * scale
    tile_y = (1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * scale
    return tile_x, tile_y


def _tile_to_lonlat(tile_x: float, tile_y: float, zoom: int) -> tuple[float, float]:
    scale = 2.0 ** zoom
    lon = (tile_x / scale) * 360.0 - 180.0
    n = math.pi - (2.0 * math.pi * tile_y / scale)
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat


def _tile_crop_geo_bounds(
    *,
    center_lat: float,
    center_lon: float,
    zoom: int,
    width: int = 640,
    height: int = 368,
    tile_size: int = 256,
    tile_span: int = 4,
) -> tuple[float, float, float, float]:
    tile_x, tile_y = _latlon_to_tile(center_lat, center_lon, zoom)
    tile_origin_x = int(math.floor(tile_x)) - (tile_span // 2)
    tile_origin_y = int(math.floor(tile_y)) - (tile_span // 2)
    center_x = int(round((tile_x - tile_origin_x) * tile_size))
    center_y = int(round((tile_y - tile_origin_y) * tile_size))
    canvas_size = tile_size * tile_span
    left = max(0, min(canvas_size - width, center_x - (width // 2)))
    top = max(0, min(canvas_size - height, center_y - (height // 2)))
    west, north = _tile_to_lonlat(tile_origin_x + (left / tile_size), tile_origin_y + (top / tile_size), zoom)
    east, south = _tile_to_lonlat(
        tile_origin_x + ((left + width) / tile_size),
        tile_origin_y + ((top + height) / tile_size),
        zoom,
    )
    return west, south, east, north


def _mercator_fraction_y(lat: float) -> float:
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    return (1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0


def _map_preview_cache_root() -> Path:
    root = Path(str(os.environ.get("EA_ARTIFACTS_DIR") or "/tmp/ea_artifacts")).resolve() / "map_previews"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _map_preview_cache_path_for_key(cache_key: dict[str, object]) -> Path:
    versioned_key = dict(cache_key)
    versioned_key.setdefault("style_version", _PROPERTY_MAP_PREVIEW_STYLE_VERSION)
    versioned_key["_tile_network_mode"] = "enabled" if _property_map_tile_network_enabled() else "disabled"
    normalized_key = json.dumps(versioned_key, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha1(normalized_key.encode("utf-8")).hexdigest()
    return _map_preview_cache_root() / f"{digest}.png"


def _flagship_map_backdrop(image: Image.Image) -> Image.Image:
    """Keep OSM readable under the selected-area layer without turning noisy."""
    softened = image.convert("RGB")
    softened = softened.filter(ImageFilter.GaussianBlur(radius=0.65))
    softened = ImageEnhance.Color(softened).enhance(0.44)
    softened = ImageEnhance.Contrast(softened).enhance(0.78)
    softened = ImageEnhance.Brightness(softened).enhance(1.01)
    return softened


def _draw_flagship_preview_polygon(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    *,
    fill: tuple[int, int, int, int],
    stroke: tuple[int, int, int, int] = _PROPERTY_MAP_PREVIEW_SELECTED_STROKE,
) -> None:
    draw.polygon(points, fill=fill)
    draw.line(points + [points[0]], fill=_PROPERTY_MAP_PREVIEW_HALO, width=4, joint="curve")
    draw.line(points + [points[0]], fill=stroke, width=2, joint="curve")


def _short_map_preview_label(value: object, *, limit: int = 18) -> str:
    label = re.sub(r"\s+", " ", str(value or "").split(",", 1)[0]).strip()
    if len(label) <= limit:
        return label
    return f"{label[: max(1, limit - 3)].rstrip()}..."


def _draw_flagship_preview_label_marker(
    draw: ImageDraw.ImageDraw,
    *,
    center: tuple[float, float],
    label: object,
    width: int,
    height: int,
) -> None:
    text = _short_map_preview_label(label)
    if not text:
        return
    font = ImageFont.load_default()
    x = max(24.0, min(float(width) - 24.0, float(center[0])))
    y = max(24.0, min(float(height) - 24.0, float(center[1])))
    draw.ellipse((x - 12, y - 12, x + 12, y + 12), fill=(194, 42, 48, 205))
    draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(255, 249, 239, 245))

    text_box = draw.textbbox((0, 0), text, font=font)
    text_width = max(1, int(text_box[2] - text_box[0]))
    text_height = max(1, int(text_box[3] - text_box[1]))
    box_width = min(max(text_width + 20, 58), 156)
    box_height = max(text_height + 12, 25)
    left = x + 15 if x <= width * 0.58 else x - 15 - box_width
    top = y - (box_height / 2)
    left = max(10.0, min(float(width) - box_width - 10.0, left))
    top = max(10.0, min(float(height) - box_height - 10.0, top))
    box = (left, top, left + box_width, top + box_height)
    line_end_x = box[0] if x <= width * 0.58 else box[2]
    draw.line((x, y, line_end_x, top + (box_height / 2)), fill=(132, 30, 36, 118), width=2)
    draw.rounded_rectangle(box, radius=10, fill=(255, 249, 239, 232), outline=(132, 30, 36, 132), width=1)
    draw.text((left + 10, top + ((box_height - text_height) / 2) - 1), text, fill=(68, 45, 39, 235), font=font)


def _draw_flagship_preview_focus_card(
    draw: ImageDraw.ImageDraw,
    *,
    pin: tuple[float, float],
    label: object,
    width: int,
    height: int,
) -> None:
    marker_x, marker_y = pin
    ring_radius = max(48, min(width, height) // 7)
    halo_radius = ring_radius + 18
    draw.ellipse(
        (marker_x - halo_radius, marker_y - halo_radius, marker_x + halo_radius, marker_y + halo_radius),
        fill=(193, 53, 53, 18),
        outline=(132, 30, 36, 60),
        width=2,
    )
    draw.ellipse(
        (marker_x - ring_radius, marker_y - ring_radius, marker_x + ring_radius, marker_y + ring_radius),
        outline=(132, 30, 36, 108),
        width=3,
    )
    tick = max(8, ring_radius // 5)
    for dx, dy in ((0, -ring_radius), (ring_radius, 0), (0, ring_radius), (-ring_radius, 0)):
        draw.line(
            (marker_x + dx, marker_y + dy, marker_x + dx * 1.12, marker_y + dy * 1.12),
            fill=(132, 30, 36, 118),
            width=2,
        )
        draw.line(
            (
                marker_x + dx - (tick if dy else 0),
                marker_y + dy - (tick if dx else 0),
                marker_x + dx + (tick if dy else 0),
                marker_y + dy + (tick if dx else 0),
            ),
            fill=(255, 248, 241, 110),
            width=1,
        )

    font = ImageFont.load_default()
    header = "Search focus"
    text = _short_map_preview_label(label, limit=24) or "Selected area"
    header_box = draw.textbbox((0, 0), header, font=font)
    text_box = draw.textbbox((0, 0), text, font=font)
    content_width = max(header_box[2] - header_box[0], text_box[2] - text_box[0])
    box_width = min(max(int(content_width) + 26, 142), 226)
    box_height = 40
    left = 18
    top = max(16, min(height - box_height - 16, int(marker_y + ring_radius + 18)))
    if top + box_height > height - 16:
        top = 16
    draw.rounded_rectangle(
        (left, top, left + box_width, top + box_height),
        radius=12,
        fill=(255, 249, 239, 222),
        outline=(132, 30, 36, 118),
        width=1,
    )
    draw.text((left + 12, top + 7), header, fill=(132, 30, 36, 210), font=font)
    draw.text((left + 12, top + 21), text, fill=(68, 45, 39, 238), font=font)


def _positive_preview_int(value: object, *, default: int = 0) -> int:
    try:
        parsed = int(float(str(value or "").strip()))
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _preview_radius_px(
    radius_m: int,
    preview_bounds: tuple[float, float, float, float],
    *,
    width: float,
    height: float,
) -> int:
    if radius_m <= 0:
        return 0
    west, south, east, north = preview_bounds
    center_lat = (south + north) / 2.0
    meters_per_lon_degree = max(111_320.0 * math.cos(math.radians(center_lat)), 1.0)
    meters_per_lat_degree = 110_540.0
    lon_meters = max(abs(east - west) * meters_per_lon_degree, 1.0)
    lat_meters = max(abs(north - south) * meters_per_lat_degree, 1.0)
    px_per_meter = max(width / lon_meters, height / lat_meters)
    return max(2, min(120, int(round(radius_m * px_per_meter))))


def _draw_flagship_preview_coverage(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    *,
    radius_px: int,
) -> None:
    if radius_px <= 0:
        return
    draw.line(
        points + [points[0]],
        fill=_PROPERTY_MAP_PREVIEW_COVERAGE_FILL,
        width=max(2, radius_px * 2),
        joint="curve",
    )


def _cached_local_map_overview_png_path(
    cache_path: Path,
    *,
    overlay_rows: list[dict[str, object]] | None = None,
    boundary_paths: list[str] | None = None,
    pin: tuple[float, float] | None = None,
    focus_label: object = None,
    width: int = 640,
    height: int = 368,
) -> Path:
    if cache_path.exists():
        return cache_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), color=(239, 234, 225))
    draw = ImageDraw.Draw(image, "RGBA")
    road = (205, 196, 183, 185)
    road_light = (255, 253, 247, 94)
    park = (204, 218, 193, 145)
    water = (193, 212, 219, 145)
    draw.rectangle((0, 0, width, height), fill=(239, 234, 225, 255))
    draw.polygon(
        [
            (0, int(height * 0.10)),
            (int(width * 0.28), 0),
            (int(width * 0.72), 0),
            (width, int(height * 0.18)),
            (width, int(height * 0.36)),
            (int(width * 0.64), int(height * 0.27)),
            (int(width * 0.34), int(height * 0.34)),
            (0, int(height * 0.26)),
        ],
        fill=park,
    )
    draw.polygon(
        [
            (0, int(height * 0.74)),
            (int(width * 0.22), int(height * 0.66)),
            (int(width * 0.48), int(height * 0.76)),
            (width, int(height * 0.68)),
            (width, height),
            (0, height),
        ],
        fill=water,
    )
    for offset in range(-width // 2, width + width // 3, max(120, width // 4)):
        draw.line([(offset, 0), (offset + int(width * 0.42), height)], fill=road, width=max(9, width // 44))
        draw.line([(offset, 0), (offset + int(width * 0.42), height)], fill=road_light, width=max(3, width // 150))
    for y in range(int(height * 0.20), height, max(70, height // 4)):
        draw.line([(0, y), (width, y - int(height * 0.09))], fill=road, width=max(7, width // 54))
        draw.line([(0, y), (width, y - int(height * 0.09))], fill=road_light, width=max(2, width // 180))
    for path in boundary_paths or []:
        numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", path)]
        points = list(zip(numbers[0::2], numbers[1::2]))
        if len(points) >= 3:
            draw.line(points + [points[0]], fill=_PROPERTY_MAP_PREVIEW_HALO, width=4, joint="curve")
            draw.line(points + [points[0]], fill=_PROPERTY_MAP_PREVIEW_BOUNDARY_STROKE, width=2, joint="curve")
    for index, row in enumerate(overlay_rows or []):
        path = str(row.get("path") or "").strip()
        if not path:
            continue
        numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", path)]
        points = list(zip(numbers[0::2], numbers[1::2]))
        if len(points) < 3:
            continue
        _draw_flagship_preview_coverage(
            draw,
            points,
            radius_px=_positive_preview_int(row.get("coverage_radius_px")),
        )
    for index, row in enumerate(overlay_rows or []):
        path = str(row.get("path") or "").strip()
        if not path:
            continue
        numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", path)]
        points = list(zip(numbers[0::2], numbers[1::2]))
        if len(points) < 3:
            continue
        fill = _PROPERTY_MAP_PREVIEW_SELECTED_FILL if bool(row.get("selected")) else _PROPERTY_MAP_PREVIEW_SECONDARY_FILL
        _draw_flagship_preview_polygon(draw, points, fill=fill)
    if pin:
        marker_x, marker_y = pin
        _draw_flagship_preview_focus_card(
            draw,
            pin=pin,
            label=focus_label,
            width=width,
            height=height,
        )
        draw.ellipse((marker_x - 18, marker_y - 18, marker_x + 18, marker_y + 18), fill=(207, 53, 53, 58))
        draw.polygon(
            [
                (marker_x, marker_y - 18),
                (marker_x - 12, marker_y - 1),
                (marker_x, marker_y + 19),
                (marker_x + 12, marker_y - 1),
            ],
            fill=(197, 40, 40, 255),
        )
        draw.ellipse((marker_x - 5, marker_y - 10, marker_x + 5, marker_y), fill=(255, 248, 241, 255))
    image = _flagship_map_backdrop(image)
    tmp_path = cache_path.with_suffix(".tmp.png")
    image.save(tmp_path, format="PNG", optimize=True)
    tmp_path.replace(cache_path)
    return cache_path


def _property_map_tile_network_enabled() -> bool:
    return str(os.environ.get("PROPERTYQUARRY_MAP_TILE_NETWORK_ENABLED") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _fetch_property_map_tile(
    url: str,
    *,
    timeout_seconds: float = 6.0,
    opener: Callable[..., Any] | None = None,
) -> bytes:
    if not _property_map_tile_network_enabled():
        raise urllib.error.URLError("property_map_tile_network_disabled")
    request = urllib.request.Request(url, headers={"User-Agent": "PropertyQuarry/1.0"})
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=timeout_seconds) as response:
        return response.read()


def _schedule_cached_preview_render(
    *,
    cache_key: dict[str, object],
    center_lat: float,
    center_lon: float,
    zoom: int,
    overlay_rows: list[dict[str, object]] | None = None,
    boundary_paths: list[str] | None = None,
    pin: tuple[float, float] | None = None,
    draw_overlay: bool = True,
    width: int = 640,
    height: int = 368,
) -> Path:
    cache_path = _map_preview_cache_path_for_key(cache_key)
    if cache_path.exists():
        return cache_path
    cache_id = cache_path.stem
    with _PROPERTY_MAP_PREVIEW_RENDER_LOCK:
        if cache_id in _PROPERTY_MAP_PREVIEW_RENDER_IN_FLIGHT:
            return cache_path
        _PROPERTY_MAP_PREVIEW_RENDER_IN_FLIGHT.add(cache_id)
    tile_fetcher = _fetch_property_map_tile

    def _render() -> None:
        try:
            _cached_preview_png_path(
                cache_key=cache_key,
                center_lat=center_lat,
                center_lon=center_lon,
                zoom=zoom,
                overlay_rows=overlay_rows,
                boundary_paths=boundary_paths,
                pin=pin,
                draw_overlay=draw_overlay,
                width=width,
                height=height,
                tile_fetcher=tile_fetcher,
            )
        finally:
            with _PROPERTY_MAP_PREVIEW_RENDER_LOCK:
                _PROPERTY_MAP_PREVIEW_RENDER_IN_FLIGHT.discard(cache_id)

    threading.Thread(target=_render, name=f"property-map-preview-{cache_id[:8]}", daemon=True).start()
    return cache_path


def _png_file_to_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _preview_zoom_for_bounds(
    bounds: tuple[float, float, float, float],
    *,
    fit_bounds: tuple[float, float, float, float] | None = None,
    width: int = 640,
    height: int = 368,
    min_zoom: int = 3,
    max_zoom: int = 16,
    min_margin_px: float = 16.0,
) -> int:
    west, south, east, north = bounds
    lon_span = max(abs(east - west), 0.0005)
    world_width = 256.0
    zoom_x = math.log2((360.0 * width) / (lon_span * world_width))
    mercator_north = _mercator_fraction_y(north)
    mercator_south = _mercator_fraction_y(south)
    y_span = max(abs(mercator_south - mercator_north), 0.000001)
    zoom_y = math.log2(height / (y_span * world_width))
    base_zoom = int(max(min_zoom, min(max_zoom, math.floor(min(zoom_x, zoom_y) - 0.05))))
    center_lon = (west + east) / 2.0
    center_lat = (south + north) / 2.0
    fit_west, fit_south, fit_east, fit_north = fit_bounds or bounds
    rect_points = [(fit_west, fit_south), (fit_east, fit_south), (fit_east, fit_north), (fit_west, fit_north)]
    for zoom in range(min(max_zoom, base_zoom + 2), min_zoom - 1, -1):
        preview_bounds = _tile_crop_geo_bounds(
            center_lat=center_lat,
            center_lon=center_lon,
            zoom=zoom,
            width=width,
            height=height,
        )
        path, _ = _project_lonlat_to_preview_path(rect_points, preview_bounds, width=float(width), height=float(height))
        numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", path)]
        xs = numbers[0::2]
        ys = numbers[1::2]
        if not xs or not ys:
            continue
        if (
            min(xs) >= min_margin_px
            and max(xs) <= width - min_margin_px
            and min(ys) >= min_margin_px
            and max(ys) <= height - min_margin_px
        ):
            return zoom
    return base_zoom


def _cached_preview_png_path(
    *,
    cache_key: dict[str, object],
    center_lat: float,
    center_lon: float,
    zoom: int,
    overlay_rows: list[dict[str, object]] | None = None,
    boundary_paths: list[str] | None = None,
    pin: tuple[float, float] | None = None,
    draw_overlay: bool = True,
    width: int = 640,
    height: int = 368,
    tile_fetcher: Callable[..., bytes] | None = None,
) -> Path:
    cache_path = _map_preview_cache_path_for_key(cache_key)
    if cache_path.exists():
        return cache_path

    tile_x, tile_y = _latlon_to_tile(center_lat, center_lon, zoom)
    tile_size = 256
    tile_span = 4
    tile_origin_x = int(math.floor(tile_x)) - (tile_span // 2)
    tile_origin_y = int(math.floor(tile_y)) - (tile_span // 2)
    canvas = Image.new("RGB", (tile_size * tile_span, tile_size * tile_span), color=(242, 236, 225))
    fetch_tile = tile_fetcher or _fetch_property_map_tile
    for dx in range(tile_span):
        for dy in range(tile_span):
            x_index = tile_origin_x + dx
            y_index = tile_origin_y + dy
            url = f"https://tile.openstreetmap.org/{zoom}/{x_index}/{y_index}.png"
            try:
                tile_bytes = fetch_tile(url, timeout_seconds=6.0)
                tile_image = Image.open(io.BytesIO(tile_bytes)).convert("RGB")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                tile_image = Image.new("RGB", (tile_size, tile_size), color=(242, 236, 225))
            canvas.paste(tile_image, (dx * tile_size, dy * tile_size))
    center_x = int(round((tile_x - tile_origin_x) * tile_size))
    center_y = int(round((tile_y - tile_origin_y) * tile_size))
    left = max(0, min(canvas.width - width, center_x - (width // 2)))
    top = max(0, min(canvas.height - height, center_y - (height // 2)))
    cropped = _flagship_map_backdrop(canvas.crop((left, top, left + width, top + height)))
    if not draw_overlay and pin:
        cropped = ImageEnhance.Contrast(cropped).enhance(1.12)
        cropped = ImageEnhance.Color(cropped).enhance(0.58)
    draw = ImageDraw.Draw(cropped, "RGBA")

    for path in boundary_paths or []:
        numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", path)]
        points = list(zip(numbers[0::2], numbers[1::2]))
        if len(points) < 3:
            continue
        draw.line(points + [points[0]], fill=_PROPERTY_MAP_PREVIEW_HALO, width=4, joint="curve")
        draw.line(points + [points[0]], fill=_PROPERTY_MAP_PREVIEW_BOUNDARY_STROKE, width=2, joint="curve")
    if draw_overlay:
        for row in overlay_rows or []:
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            path_kind = str(row.get("path_kind") or "").strip().lower()
            if path_kind == "line":
                continue
            numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", path)]
            points = list(zip(numbers[0::2], numbers[1::2]))
            if len(points) < 3:
                continue
            _draw_flagship_preview_coverage(
                draw,
                points,
                radius_px=_positive_preview_int(row.get("coverage_radius_px")),
            )
        for index, row in enumerate(overlay_rows or []):
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", path)]
            points = list(zip(numbers[0::2], numbers[1::2]))
            path_kind = str(row.get("path_kind") or "").strip().lower()
            if path_kind == "line":
                if len(points) < 2:
                    continue
                halo_width = max(_positive_preview_int(row.get("halo_width_px")) or 13, 5)
                stroke_width = max(_positive_preview_int(row.get("stroke_width_px")) or 7, 3)
                draw.line(points, fill=_PROPERTY_MAP_PREVIEW_ROUTE_HALO, width=halo_width, joint="curve")
                draw.line(points, fill=_PROPERTY_MAP_PREVIEW_ROUTE_STROKE, width=stroke_width, joint="curve")
                if bool(row.get("show_endpoint_markers")):
                    start_x, start_y = points[0]
                    end_x, end_y = points[-1]
                    draw.ellipse(
                        (start_x - 6, start_y - 6, start_x + 6, start_y + 6),
                        fill=_PROPERTY_MAP_PREVIEW_ROUTE_START_FILL,
                        outline=_PROPERTY_MAP_PREVIEW_ROUTE_MARKER_STROKE,
                        width=2,
                    )
                    draw.ellipse(
                        (end_x - 6, end_y - 6, end_x + 6, end_y + 6),
                        fill=_PROPERTY_MAP_PREVIEW_ROUTE_END_FILL,
                        outline=_PROPERTY_MAP_PREVIEW_ROUTE_MARKER_STROKE,
                        width=2,
                    )
                continue
            if len(points) < 3:
                continue
            selected = bool(row.get("selected"))
            fill = _PROPERTY_MAP_PREVIEW_SELECTED_FILL if selected else _PROPERTY_MAP_PREVIEW_SECONDARY_FILL
            stroke = _PROPERTY_MAP_PREVIEW_SELECTED_STROKE if selected else (132, 30, 36, 118)
            _draw_flagship_preview_polygon(draw, points, fill=fill, stroke=stroke)
        for row in overlay_rows or []:
            if not bool(row.get("show_label_marker")):
                continue
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            path_kind = str(row.get("path_kind") or "").strip().lower()
            if path_kind == "line":
                continue
            numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", path)]
            points = list(zip(numbers[0::2], numbers[1::2]))
            if len(points) < 3:
                continue
            center = (
                sum(float(point[0]) for point in points) / len(points),
                sum(float(point[1]) for point in points) / len(points),
            )
            _draw_flagship_preview_label_marker(
                draw,
                center=center,
                label=row.get("label"),
                width=width,
                height=height,
            )
    if pin:
        marker_x, marker_y = pin
        if not draw_overlay:
            _draw_flagship_preview_focus_card(
                draw,
                pin=pin,
                label=cache_key.get("query") or cache_key.get("label") or cache_key.get("kind"),
                width=width,
                height=height,
            )
        draw.ellipse((marker_x - 18, marker_y - 18, marker_x + 18, marker_y + 18), fill=(207, 53, 53, 58))
        draw.polygon(
            [
                (marker_x, marker_y - 18),
                (marker_x - 12, marker_y - 1),
                (marker_x, marker_y + 19),
                (marker_x + 12, marker_y - 1),
            ],
            fill=(197, 40, 40, 255),
        )
        draw.ellipse((marker_x - 5, marker_y - 10, marker_x + 5, marker_y), fill=(255, 248, 241, 255))

    cropped.save(cache_path, format="PNG", optimize=True)
    return cache_path


def _cached_preview_data_url(
    *,
    cache_key: dict[str, object],
    center_lat: float,
    center_lon: float,
    zoom: int,
    overlay_rows: list[dict[str, object]] | None = None,
    boundary_paths: list[str] | None = None,
    pin: tuple[float, float] | None = None,
    draw_overlay: bool = True,
    width: int = 640,
    height: int = 368,
) -> str:
    cache_path = _cached_preview_png_path(
        cache_key=cache_key,
        center_lat=center_lat,
        center_lon=center_lon,
        zoom=zoom,
        overlay_rows=overlay_rows,
        boundary_paths=boundary_paths,
        pin=pin,
        draw_overlay=draw_overlay,
        width=width,
        height=height,
    )
    return _png_file_to_data_url(cache_path)


def _cached_preview_image_url(
    *,
    cache_key: dict[str, object],
    center_lat: float,
    center_lon: float,
    zoom: int,
    overlay_rows: list[dict[str, object]] | None = None,
    boundary_paths: list[str] | None = None,
    pin: tuple[float, float] | None = None,
    draw_overlay: bool = True,
    width: int = 640,
    height: int = 368,
    materialize: str = "sync",
) -> str:
    if str(materialize or "sync").strip().lower() == "async":
        cache_path = _schedule_cached_preview_render(
            cache_key=cache_key,
            center_lat=center_lat,
            center_lon=center_lon,
            zoom=zoom,
            overlay_rows=overlay_rows,
            boundary_paths=boundary_paths,
            pin=pin,
            draw_overlay=draw_overlay,
            width=width,
            height=height,
        )
    else:
        cache_path = _cached_preview_png_path(
            cache_key=cache_key,
            center_lat=center_lat,
            center_lon=center_lon,
            zoom=zoom,
            overlay_rows=overlay_rows,
            boundary_paths=boundary_paths,
            pin=pin,
            draw_overlay=draw_overlay,
            width=width,
            height=height,
        )
    return f"/app/api/property/map-previews/{cache_path.stem}.png"


@lru_cache(maxsize=96)
def _openstreetmap_static_preview_data_url(lat_key: int, lon_key: int, zoom: int = 13) -> str:
    lat = lat_key / 10000.0
    lon = lon_key / 10000.0
    return _cached_preview_data_url(
        cache_key={"kind": "point", "lat_key": lat_key, "lon_key": lon_key, "zoom": zoom},
        center_lat=lat,
        center_lon=lon,
        zoom=zoom,
        pin=(320.0, 184.0),
    )


@lru_cache(maxsize=96)
def _forward_geocode_preview_point(query: str) -> tuple[float, float] | None:
    normalized = str(query or "").strip()
    if not normalized:
        return None
    request = urllib.request.Request(
        "https://nominatim.openstreetmap.org/search?"
        f"format=jsonv2&limit=1&q={urllib.parse.quote(normalized)}",
        headers={"User-Agent": "PropertyQuarry/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list) or not payload:
        return None
    row = payload[0]
    if not isinstance(row, dict):
        return None
    try:
        return float(row.get("lat") or 0.0), float(row.get("lon") or 0.0)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=128)
def _nominatim_boundary_record(query: str) -> dict[str, object]:
    return dict(_property_research_boundary_record(query) or {})


_VIENNA_DISTRICT_PREVIEW_BOUNDS: dict[str, tuple[str, tuple[float, float, float, float]]] = {
    "1010": ("Innere Stadt", (16.356, 48.202, 16.379, 48.216)),
    "1020": ("Leopoldstadt", (16.365, 48.197, 16.456, 48.235)),
    "1030": ("Landstrasse", (16.366, 48.176, 16.423, 48.212)),
    "1040": ("Wieden", (16.360, 48.185, 16.381, 48.199)),
    "1050": ("Margareten", (16.342, 48.179, 16.368, 48.194)),
    "1060": ("Mariahilf", (16.340, 48.190, 16.361, 48.203)),
    "1070": ("Neubau", (16.335, 48.196, 16.356, 48.211)),
    "1080": ("Josefstadt", (16.340, 48.207, 16.358, 48.218)),
    "1090": ("Alsergrund", (16.342, 48.216, 16.371, 48.236)),
    "1100": ("Favoriten", (16.340, 48.135, 16.420, 48.185)),
    "1110": ("Simmering", (16.405, 48.140, 16.500, 48.185)),
    "1120": ("Meidling", (16.295, 48.160, 16.350, 48.190)),
    "1130": ("Hietzing", (16.215, 48.150, 16.325, 48.225)),
    "1140": ("Penzing", (16.210, 48.185, 16.330, 48.250)),
    "1150": ("Rudolfsheim-Fuenfhaus", (16.315, 48.188, 16.345, 48.210)),
    "1160": ("Ottakring", (16.285, 48.205, 16.335, 48.230)),
    "1170": ("Hernals", (16.285, 48.220, 16.335, 48.245)),
    "1180": ("Waehring", (16.300, 48.225, 16.360, 48.250)),
    "1190": ("Doebling", (16.300, 48.240, 16.380, 48.310)),
    "1200": ("Brigittenau", (16.350, 48.220, 16.410, 48.260)),
    "1210": ("Floridsdorf", (16.360, 48.250, 16.500, 48.330)),
    "1220": ("Donaustadt", (16.420, 48.180, 16.580, 48.320)),
    "1230": ("Liesing", (16.240, 48.120, 16.340, 48.180)),
}


@lru_cache(maxsize=1)
def _vienna_district_boundary_records() -> dict[str, dict[str, object]]:
    path = Path(__file__).resolve().parents[2] / "data" / "vienna_district_boundaries_simplified.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = dict(payload.get("districts") or {}) if isinstance(payload, dict) else {}
    records: dict[str, dict[str, object]] = {}
    for postal_code, row in rows.items():
        if not isinstance(row, dict):
            continue
        key = str(postal_code or "").strip()
        bounds_raw = row.get("bounds")
        rings_raw = row.get("rings")
        if not re.fullmatch(r"1[0-2]\d0", key):
            continue
        if not isinstance(bounds_raw, list) or len(bounds_raw) != 4:
            continue
        if not isinstance(rings_raw, list) or not rings_raw:
            continue
        try:
            bounds = tuple(float(value) for value in bounds_raw)
        except (TypeError, ValueError):
            continue
        rings: list[list[list[float]]] = []
        for raw_ring in rings_raw:
            if not isinstance(raw_ring, list) or len(raw_ring) < 4:
                continue
            ring: list[list[float]] = []
            for raw_point in raw_ring:
                if not isinstance(raw_point, list) or len(raw_point) < 2:
                    continue
                try:
                    ring.append([float(raw_point[0]), float(raw_point[1])])
                except (TypeError, ValueError):
                    continue
            if len(ring) >= 4:
                rings.append(ring)
        if not rings:
            continue
        records[key] = {
            "name": str(row.get("name") or _VIENNA_DISTRICT_PREVIEW_BOUNDS.get(key, ("", ()))[0] or key).strip(),
            "bounds": bounds,
            "rings": rings,
        }
    return records


_PROPERTY_SCOPE_REGION_PREVIEW_CENTERS: dict[str, dict[str, tuple[float, float]]] = {
    "AT": {
        "country": (47.5162, 14.5501),
        "austria": (47.5162, 14.5501),
        "vienna": (48.2082, 16.3738),
        "wien": (48.2082, 16.3738),
        "lower_austria": (48.2042, 16.3266),
        "niederosterreich": (48.2042, 16.3266),
        "upper_austria": (48.3000, 14.2837),
        "upperaustria": (48.3000, 14.2837),
        "tyrol": (47.2538, 11.6015),
        "tyroler_alpen": (47.2538, 11.6015),
        "salzburg": (47.8095, 13.0550),
        "vorarlberg": (47.2331, 9.6000),
        "carinthia": (46.6242, 14.3037),
        "carinthia_region": (46.6242, 14.3037),
        "burgenland": (47.8436, 16.5484),
    },
    "DE": {
        "country": (51.1657, 10.4515),
    },
    "BE": {
        "country": (50.5039, 4.4699),
    },
    "CA": {
        "country": (56.1304, -106.3468),
    },
    "CH": {
        "country": (46.8182, 8.2275),
    },
    "CR": {
        "country": (9.7489, -83.7534),
    },
    "IE": {
        "country": (53.1424, -7.6921),
    },
    "AU": {
        "country": (-25.2744, 133.7751),
    },
    "ES": {
        "country": (40.4637, -3.7492),
    },
    "IT": {
        "country": (41.8719, 12.5674),
    },
    "FR": {
        "country": (46.2276, 2.2137),
    },
    "PT": {
        "country": (39.3999, -8.2245),
    },
    "PL": {
        "country": (51.9194, 19.1451),
    },
    "SE": {
        "country": (60.1282, 18.6435),
    },
    "NL": {
        "country": (52.1326, 5.2913),
    },
    "UK": {
        "country": (55.3781, -3.4360),
    },
    "US": {
        "country": (39.8283, -98.5795),
    },
}


def _property_scope_fallback_point(
    country_code: str,
    region_code: str,
    normalized_query: str,
) -> tuple[float, float] | None:
    from app.services.property_market_catalog import normalize_country_code

    normalized_country = normalize_country_code(country_code, default="")
    if not normalized_country:
        normalized_country = str(country_code or "").strip().upper()
    else:
        normalized_country = normalized_country.upper()

    normalized_region = str(region_code or "").strip().lower()
    normalized_region_compact = re.sub(r"[^a-z0-9]+", "", normalized_region)
    region_alias_map = {
        "upperaustria": "upper_austria",
        "loweraustria": "lower_austria",
        "oberosterreich": "upper_austria",
        "niederosterreich": "lower_austria",
        "carinthiaregion": "carinthia_region",
    }
    normalized_region_alias = region_alias_map.get(normalized_region_compact, normalized_region_compact)
    region_candidates = {
        normalized_region,
        normalized_region_compact,
        normalized_region_alias,
    }
    if "_" in normalized_region_alias:
        region_candidates.add(normalized_region_alias.replace("_", "-"))
    if normalized_region_alias and " " not in normalized_region_alias:
        region_candidates.add(normalized_region_alias.replace("_", " "))

    if normalized_country == "AT":
        match = re.search(r"\b(1[0-2]\d0)\b", str(normalized_query or ""))
        if match:
            postal_code = match.group(1)
            district = _VIENNA_DISTRICT_PREVIEW_BOUNDS.get(postal_code)
            if district is not None:
                _, bounds = district
                west, south, east, north = bounds
                return ((south + north) / 2.0), ((west + east) / 2.0)

    row_centers = _PROPERTY_SCOPE_REGION_PREVIEW_CENTERS.get(normalized_country)
    if row_centers:
        normalized_row_centers = {re.sub(r"[^a-z0-9]+", "", str(key or "").lower()): value for key, value in row_centers.items()}
        for candidate in region_candidates:
            if not candidate:
                continue
            if candidate in row_centers:
                return row_centers[candidate]
            mapped = normalized_row_centers.get(candidate)
            if mapped is not None:
                return mapped
        for key in ("country", "all"):
            if key in row_centers:
                return row_centers[key]

    fallback = _PROPERTY_SCOPE_REGION_PREVIEW_CENTERS.get(normalized_country, {}).get("country")
    if fallback is not None:
        return fallback
    return (48.8566, 2.3522)


def _local_scope_boundary_record(query: str, *, country_code: str, region_code: str) -> dict[str, object]:
    if str(country_code or "").strip().upper() != "AT":
        return {}
    if str(region_code or "").strip().lower() not in {"vienna", "wien"}:
        return {}
    match = re.search(r"\b(1[0-2]\d0)\b", str(query or ""))
    if not match:
        return {}
    postal_code = match.group(1)
    precise_district = _vienna_district_boundary_records().get(postal_code)
    if precise_district:
        district_name = str(precise_district.get("name") or postal_code).strip()
        bounds = precise_district.get("bounds")
        rings = precise_district.get("rings")
        if isinstance(bounds, tuple) and isinstance(rings, list) and rings:
            west, south, east, north = bounds
            geojson: dict[str, object]
            if len(rings) == 1:
                geojson = {"type": "Polygon", "coordinates": [rings[0]]}
            else:
                geojson = {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings]}
            return {
                "display_name": f"{district_name}, {postal_code} Vienna",
                "bounds": bounds,
                "geojson": geojson,
                "lat": (south + north) / 2.0,
                "lon": (west + east) / 2.0,
            }
    district = _VIENNA_DISTRICT_PREVIEW_BOUNDS.get(postal_code)
    if not district:
        return {}
    district_name, bounds = district
    west, south, east, north = bounds
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {
        "display_name": f"{district_name}, {postal_code} Vienna",
        "bounds": bounds,
        "geojson": {"type": "Polygon", "coordinates": [ring]},
        "lat": (south + north) / 2.0,
        "lon": (west + east) / 2.0,
    }


def _geojson_outer_rings(geojson: dict[str, object]) -> list[list[tuple[float, float]]]:
    return list(_property_research_geojson_outer_rings(geojson))


def _union_geo_bounds(bounds_rows: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    if not bounds_rows:
        return None
    west = min(row[0] for row in bounds_rows)
    south = min(row[1] for row in bounds_rows)
    east = max(row[2] for row in bounds_rows)
    north = max(row[3] for row in bounds_rows)
    if west == east:
        east += 0.01
        west -= 0.01
    if south == north:
        north += 0.01
        south -= 0.01
    return west, south, east, north


def _project_lonlat_to_preview_path(
    points: list[tuple[float, float]],
    bounds: tuple[float, float, float, float],
    *,
    width: float = 296.0,
    height: float = 160.0,
) -> tuple[str, tuple[float, float]]:
    west, south, east, north = bounds
    lon_span = max(east - west, 0.000001)
    lat_span = max(north - south, 0.000001)
    projected: list[tuple[float, float]] = []
    for lon, lat in points:
        x = ((lon - west) / lon_span) * width
        y = height - (((lat - south) / lat_span) * height)
        projected.append((x, y))
    if not projected:
        return "", (0.0, 0.0)
    commands = [f"M{projected[0][0]:.1f} {projected[0][1]:.1f}"]
    commands.extend(f"L{x:.1f} {y:.1f}" for x, y in projected[1:])
    commands.append("Z")
    centroid_x = sum(point[0] for point in projected) / len(projected)
    centroid_y = sum(point[1] for point in projected) / len(projected)
    return " ".join(commands), (centroid_x, centroid_y)


def _project_lonlat_to_preview_polyline(
    points: list[tuple[float, float]],
    bounds: tuple[float, float, float, float],
    *,
    width: float = 296.0,
    height: float = 160.0,
) -> tuple[str, tuple[float, float]]:
    west, south, east, north = bounds
    lon_span = max(east - west, 0.000001)
    lat_span = max(north - south, 0.000001)
    projected: list[tuple[float, float]] = []
    for lon, lat in points:
        x = ((lon - west) / lon_span) * width
        y = height - (((lat - south) / lat_span) * height)
        projected.append((x, y))
    if len(projected) < 2:
        return "", (0.0, 0.0)
    commands = [f"M{projected[0][0]:.1f} {projected[0][1]:.1f}"]
    commands.extend(f"L{x:.1f} {y:.1f}" for x, y in projected[1:])
    centroid_x = sum(point[0] for point in projected) / len(projected)
    centroid_y = sum(point[1] for point in projected) / len(projected)
    return " ".join(commands), (centroid_x, centroid_y)


def _expand_geo_bounds(
    bounds: tuple[float, float, float, float],
    *,
    padding_ratio: float = 0.12,
) -> tuple[float, float, float, float]:
    west, south, east, north = bounds
    lon_span = max(east - west, 0.000001)
    lat_span = max(north - south, 0.000001)
    base_padding_factor = max(1.0, max(lon_span, lat_span) / max(1e-6, min(lon_span, lat_span)))
    min_pad = 0.0035 * min(base_padding_factor, 1.6)
    lon_pad = min(max(lon_span * padding_ratio, min_pad), lon_span * 0.22)
    lat_pad = min(max(lat_span * padding_ratio, min_pad), lat_span * 0.22)
    return west - lon_pad, south - lat_pad, east + lon_pad, north + lat_pad


def _preview_query_with_context(value: str, country_code: str, region_code: str) -> str:
    label = str(value or "").strip()
    region = str(region_code or "").strip().replace("_", " ")
    country = str(country_code or "").strip().upper()
    if not label:
        return ""
    parts = [label]
    lowered = label.lower()
    if region and region.lower() not in lowered:
        parts.append(region.title())
    if country and country.lower() not in lowered:
        parts.append(country)
    return ", ".join(part for part in parts if part)


def _context_preview_query(country_code: str, region_code: str, location_query: str, selected_labels: list[str]) -> str:
    if location_query and len(_csv_values(location_query)) <= 1:
        return _preview_query_with_context(location_query, country_code, region_code)
    region = str(region_code or "").strip().replace("_", " ")
    if region:
        return _preview_query_with_context(region, country_code, "")
    if selected_labels:
        return _preview_query_with_context(selected_labels[0], country_code, "")
    return _preview_query_with_context(location_query, country_code, region_code)


def _build_scope_boundary_preview(
    *,
    country_code: str,
    region_code: str,
    normalized_query: str,
    selected_labels: list[str],
    selected_values: list[str],
    option_lookup: dict[str, str],
    market_label: str,
    adjacent_area_radius_m: int = 0,
    allow_remote_lookup: bool = True,
    materialize_preview: str = "sync",
    padding_ratio: float = 0.12,
) -> dict[str, object]:
    adjacent_area_radius_m = max(0, min(_positive_preview_int(adjacent_area_radius_m), 20_000))
    queries = [
        _preview_query_with_context(option_lookup.get(value.lower(), value), country_code, region_code)
        for value in selected_values
        if str(value or "").strip()
    ]
    if not queries and normalized_query:
        queries = [_preview_query_with_context(normalized_query, country_code, region_code)]
    rows: list[dict[str, object]] = []
    bounds_rows: list[tuple[float, float, float, float]] = []
    for query in queries[:12]:
        record = _local_scope_boundary_record(query, country_code=country_code, region_code=region_code)
        if not record and allow_remote_lookup:
            record = _nominatim_boundary_record(query)
        if not record:
            continue
        bounds = record.get("bounds")
        if isinstance(bounds, tuple) and len(bounds) == 4:
            bounds_rows.append(bounds)
        rings = _geojson_outer_rings(dict(record.get("geojson") or {}))
        label = str(record.get("display_name") or query).split(",")[0].strip() or query
        rows.append({"label": label, "bounds": bounds, "rings": rings, "selected": True})
    if not rows:
        return {}

    context_record = (
        _nominatim_boundary_record(_context_preview_query(country_code, region_code, normalized_query, selected_labels))
        if allow_remote_lookup
        else {}
    )
    boundary_paths: list[str] = []
    context_bounds = context_record.get("bounds") if isinstance(context_record.get("bounds"), tuple) else None
    union_bounds = _union_geo_bounds(bounds_rows)
    if not union_bounds:
        return {}
    lon_span = abs(float(union_bounds[2]) - float(union_bounds[0]))
    lat_span = abs(float(union_bounds[3]) - float(union_bounds[1]))
    multi_area_overview = len(rows) > 1 and (lon_span > 1.0 or lat_span > 1.0)
    radius_padding_degrees = (adjacent_area_radius_m / 111_000.0) if adjacent_area_radius_m > 0 else 0.0
    render_bounds = _expand_geo_bounds(union_bounds, padding_ratio=padding_ratio)
    if radius_padding_degrees > 0:
        render_bounds = (
            render_bounds[0] - radius_padding_degrees,
            render_bounds[1] - radius_padding_degrees,
            render_bounds[2] + radius_padding_degrees,
            render_bounds[3] + radius_padding_degrees,
        )

    center_lon = (render_bounds[0] + render_bounds[2]) / 2.0
    center_lat = (render_bounds[1] + render_bounds[3]) / 2.0
    fit_bounds = (
        union_bounds[0] - radius_padding_degrees,
        union_bounds[1] - radius_padding_degrees,
        union_bounds[2] + radius_padding_degrees,
        union_bounds[3] + radius_padding_degrees,
    ) if radius_padding_degrees > 0 else union_bounds
    zoom = _preview_zoom_for_bounds(render_bounds, fit_bounds=fit_bounds)
    preview_bounds = _tile_crop_geo_bounds(center_lat=center_lat, center_lon=center_lon, zoom=zoom, width=640, height=368)
    coverage_radius_px = _preview_radius_px(
        adjacent_area_radius_m,
        preview_bounds,
        width=640.0,
        height=368.0,
    )

    district_rows: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        rings = row.get("rings") if isinstance(row.get("rings"), list) else []
        if rings:
            path, _ = _project_lonlat_to_preview_path(rings[0], preview_bounds, width=640.0, height=368.0)
        else:
            bounds = row.get("bounds") if isinstance(row.get("bounds"), tuple) else None
            if not bounds:
                continue
            west, south, east, north = bounds
            rect_points = [(west, south), (east, south), (east, north), (west, north)]
            path, _ = _project_lonlat_to_preview_path(rect_points, preview_bounds, width=640.0, height=368.0)
        if not path:
            continue
        overlay_row = {
            "label": str(row.get("label") or f"Area {index + 1}").strip(),
            "selected": True,
            "path": path,
            **({"show_label_marker": True} if multi_area_overview else {}),
            **({"coverage_radius_px": coverage_radius_px} if coverage_radius_px else {}),
        }
        district_rows.append(overlay_row)

    if not district_rows:
        return {}

    if context_bounds:
        for ring in _geojson_outer_rings(dict(context_record.get("geojson") or {}))[:1]:
            boundary_path, _ = _project_lonlat_to_preview_path(ring, preview_bounds, width=640.0, height=368.0)
            if boundary_path:
                boundary_paths.append(boundary_path)

    image_url = _cached_preview_image_url(
        cache_key={
            "kind": "scope",
            "country": country_code,
            "region": region_code,
            "query": normalized_query,
            "areas": [row["label"] for row in district_rows],
            "zoom": zoom,
            "overlay_mode": "svg_tile_crop_v6",
            "render_bounds_source": "selected_areas",
            "adjacent_area_radius_m": adjacent_area_radius_m,
            "coverage_radius_px": coverage_radius_px,
            "materialize": str(materialize_preview or "sync").strip().lower(),
            "label_markers": multi_area_overview,
        },
        center_lat=center_lat,
        center_lon=center_lon,
        zoom=zoom,
        overlay_rows=district_rows,
        boundary_paths=boundary_paths,
        draw_overlay=True,
        materialize=materialize_preview,
    )
    return {
        "image_url": image_url,
        "alt": f"Search area preview for {normalized_query or market_label}",
        "summary": ", ".join(selected_labels[:2]) if selected_labels else (normalized_query or market_label),
        "count_label": "",
        "market_label": market_label,
        "district_rows": district_rows,
        "district_overlay_svg": "",
        "preview_kind": "osm_district_overlay",
        "has_district_overlay": True,
    }


def _property_scope_point_preview(
    *,
    country_code: str,
    region_code: str,
    normalized_query: str,
    market_label: str,
    allow_remote_lookup: bool = True,
) -> dict[str, object]:
    query = _context_preview_query(country_code, region_code, normalized_query, [normalized_query] if normalized_query else [])
    zoom = 16
    point: tuple[float, float] | None = None
    point_queries = [
        query,
        _preview_query_with_context(region_code, country_code, ""),
        _preview_query_with_context(country_code, "", ""),
    ]
    preview_kind = "osm_point_fallback"
    if allow_remote_lookup:
        for point_query in point_queries:
            if not str(point_query or "").strip():
                continue
            point = _forward_geocode_preview_point(point_query)
            if point is not None:
                break
    if point is None:
        fallback_point = _property_scope_fallback_point(country_code, region_code, normalized_query)
        if fallback_point is None:
            return {}
        point = fallback_point
    if point is None:
        return {}
    lat, lon = point
    lat_key = int(round(lat * 10000))
    lon_key = int(round(lon * 10000))
    return {
        "image_url": _cached_preview_image_url(
            cache_key={
                "kind": "scope-point",
                "country": country_code,
                "region": region_code,
                "query": normalized_query or market_label,
                "lat_key": lat_key,
                "lon_key": lon_key,
                "zoom": zoom,
                "overlay_mode": "pin_v1",
            },
            center_lat=lat,
            center_lon=lon,
            zoom=zoom,
            pin=(320.0, 184.0),
            draw_overlay=False,
        ),
        "alt": f"Search area preview for {normalized_query or market_label}",
        "summary": normalized_query or market_label,
        "count_label": "",
        "market_label": market_label,
        "district_rows": [],
        "district_overlay_svg": "",
        "preview_kind": preview_kind,
        "has_district_overlay": False,
    }


def _property_scope_preview(
    country_code: str,
    region_code: str,
    location_query: str,
    *,
    adjacent_area_radius_m: int = 0,
) -> dict[str, object]:
    normalized_country = str(country_code or "").strip().upper()
    normalized_region = str(region_code or "").strip().lower()
    normalized_query = str(location_query or "").strip()
    option_rows = _property_location_options(normalized_country, normalized_region)
    layout_rows = _scope_preview_layout(normalized_country, normalized_region, option_rows)
    option_lookup = {
        str(option.get("value") or "").strip().lower(): str(option.get("label") or option.get("value") or "").strip()
        for option in option_rows
        if str(option.get("value") or "").strip()
    }
    selected_values = _csv_values(normalized_query)
    selected_lookup = {value.lower() for value in selected_values}
    if normalized_country == "AT" and normalized_region == "vienna" and normalized_query.lower() in {"vienna", "wien"}:
        selected_lookup = {
            str(row.get("value") or "").strip().lower()
            for row in layout_rows
            if str(row.get("value") or "").strip()
        }
    elif not selected_lookup and normalized_query:
        if normalized_query.lower() in option_lookup:
            selected_lookup = {normalized_query.lower()}
        elif normalized_region and normalized_query.lower() == normalized_region:
            selected_lookup = {
                str(row.get("value") or "").strip().lower()
                for row in layout_rows
                if str(row.get("value") or "").strip()
            }
    selected_labels = [
        option_lookup.get(value.lower(), value)
        for value in selected_values
        if str(value or "").strip()
    ]
    market_label_parts = [part for part in (normalized_region.replace("_", " ").title(), normalized_country) if part]
    market_label = " · ".join(market_label_parts) or "Search area"
    preview = _build_scope_boundary_preview(
        country_code=normalized_country,
        region_code=normalized_region,
        normalized_query=normalized_query,
        selected_labels=selected_labels,
        selected_values=selected_values,
        option_lookup=option_lookup,
        market_label=market_label,
        adjacent_area_radius_m=adjacent_area_radius_m,
    )
    if preview:
        return preview

    if not selected_values and not normalized_query:
        point_preview = _property_scope_point_preview(
            country_code=normalized_country,
            region_code=normalized_region,
            normalized_query=normalized_query,
            market_label=market_label,
        )
        if point_preview:
            return point_preview

    fallback_rows = _merge_option_catalog(option_rows, selected_values)
    fallback_layout = _scope_preview_layout(normalized_country, normalized_region, fallback_rows)
    if fallback_layout:
        if not selected_lookup and selected_values:
            selected_lookup = {str(value or "").strip().lower() for value in selected_values if str(value or "").strip()}
        return {
            "image_url": _scope_layout_preview_data_url(
                country_code=normalized_country,
                region_code=normalized_region,
                normalized_query=normalized_query,
                market_label=market_label,
                layout_rows=fallback_layout,
                selected_lookup=selected_lookup,
            ),
            "alt": f"Search area preview for {normalized_query or market_label}",
            "summary": ", ".join(selected_labels[:2]) if selected_labels else (normalized_query or market_label),
            "count_label": "",
            "market_label": market_label,
            "district_rows": [],
            "district_overlay_svg": "",
            "preview_kind": "local_district_layout",
            "has_district_overlay": False,
        }

    point_preview = _property_scope_point_preview(
        country_code=normalized_country,
        region_code=normalized_region,
        normalized_query=normalized_query,
        market_label=market_label,
    )
    if point_preview:
        return point_preview

    return _property_scope_fallback_layout_preview(
        country_code=normalized_country,
        region_code=normalized_region,
        normalized_query=normalized_query,
        selected_labels=selected_labels,
        market_label=market_label,
    )


def _property_scope_fallback_layout_preview(
    *,
    country_code: str,
    region_code: str,
    normalized_query: str,
    selected_labels: list[str],
    market_label: str,
) -> dict[str, object]:
    fallback_label = normalized_query or market_label or "Search area"
    fallback_layout = _scope_preview_layout(
        country_code,
        region_code,
        [{"value": "scope", "label": fallback_label, "detail": ""}],
    )
    return {
        "image_url": _scope_layout_preview_data_url(
            country_code=country_code,
            region_code=region_code,
            normalized_query=fallback_label,
            market_label=market_label,
            layout_rows=fallback_layout,
            selected_lookup={"scope"},
        ),
        "alt": f"Search area preview for {fallback_label}",
        "summary": ", ".join(selected_labels[:2]) if selected_labels else fallback_label,
        "count_label": "",
        "market_label": market_label,
        "district_rows": [],
        "district_overlay_svg": "",
        "preview_kind": "fallback_layout",
        "has_district_overlay": False,
    }


def _property_scope_map_pending_preview(
    *,
    country_code: str,
    region_code: str,
    normalized_query: str,
    market_label: str,
    selected_labels: list[str] | None = None,
) -> dict[str, object]:
    label = normalized_query or market_label or "Search area"
    image_url = ""
    fallback_point = _property_scope_fallback_point(country_code, region_code, normalized_query)
    if fallback_point is not None:
        lat, lon = fallback_point
        lat_key = int(round(lat * 10000))
        lon_key = int(round(lon * 10000))
        image_url = _cached_preview_image_url(
            cache_key={
                "kind": "scope-pending",
                "country": str(country_code or "").strip().upper(),
                "region": str(region_code or "").strip().lower(),
                "query": label,
                "lat_key": lat_key,
                "lon_key": lon_key,
                "zoom": 12,
                "overlay_mode": "pending_pin_v1",
            },
            center_lat=lat,
            center_lon=lon,
            zoom=12,
            pin=(320.0, 184.0),
            draw_overlay=False,
            materialize="async",
        )
    return {
        "image_url": image_url,
        "alt": f"Search area preview for {label}",
        "summary": ", ".join(list(selected_labels or [])[:2]) if selected_labels else label,
        "count_label": "",
        "market_label": market_label,
        "district_rows": [],
        "district_overlay_svg": "",
        "preview_kind": "osm_map_pending",
        "has_district_overlay": False,
    }


def _property_scope_preview_fast(country_code: str, region_code: str, location_query: str) -> dict[str, object]:
    normalized_country = str(country_code or "").strip().upper()
    normalized_region = str(region_code or "").strip().lower()
    normalized_query = str(location_query or "").strip()
    option_rows = _property_location_options(normalized_country, normalized_region)
    selected_values = _csv_values(normalized_query)
    option_lookup = {
        str(option.get("value") or "").strip().lower(): str(option.get("label") or option.get("value") or "").strip()
        for option in option_rows
        if str(option.get("value") or "").strip()
    }
    selected_lookup = {value.lower() for value in selected_values}
    if normalized_country == "AT" and normalized_region == "vienna" and normalized_query.lower() in {"vienna", "wien"}:
        selected_lookup = {
            str(row.get("value") or "").strip().lower()
            for row in _scope_preview_layout(normalized_country, normalized_region, option_rows)
            if str(row.get("value") or "").strip()
        }
    fallback_rows = _merge_option_catalog(option_rows, selected_values)
    fallback_layout = _scope_preview_layout(normalized_country, normalized_region, fallback_rows)
    market_label_parts = [part for part in (normalized_region.replace("_", " ").title(), normalized_country) if part]
    market_label = " | ".join(market_label_parts) or "Search area"
    selected_labels = [option_lookup.get(value.lower(), value) for value in selected_values if str(value or "").strip()]
    if not selected_values and not normalized_query:
        point_preview = _property_scope_point_preview(
            country_code=normalized_country,
            region_code=normalized_region,
            normalized_query=normalized_query,
            market_label=market_label,
        )
        if point_preview:
            return point_preview

    if fallback_layout:
        return {
            "image_url": _scope_layout_preview_data_url(
                country_code=normalized_country,
                region_code=normalized_region,
                normalized_query=normalized_query,
                market_label=market_label,
                layout_rows=fallback_layout,
                selected_lookup=selected_lookup,
            ),
            "alt": f"Search area preview for {normalized_query or market_label}",
            "summary": ", ".join(selected_labels[:2]) if selected_labels else (normalized_query or market_label),
            "count_label": "",
            "market_label": market_label,
            "district_rows": [
                {
                    "label": str(row.get("label") or row.get("value") or "").strip(),
                    "selected": str(row.get("value") or "").strip().lower() in selected_lookup,
                }
                for row in fallback_layout
                if str(row.get("label") or row.get("value") or "").strip()
            ],
            "district_overlay_svg": "",
            "preview_kind": "fast_district_layout",
            "has_district_overlay": False,
        }
    point_preview = _property_scope_point_preview(
        country_code=normalized_country,
        region_code=normalized_region,
        normalized_query=normalized_query,
        market_label=market_label,
    )
    if point_preview:
        return point_preview
    return _property_scope_fallback_layout_preview(
        country_code=normalized_country,
        region_code=normalized_region,
        normalized_query=normalized_query,
        selected_labels=selected_labels,
        market_label=market_label,
    )


def _property_scope_preview_map_only(
    country_code: str,
    region_code: str,
    location_query: str,
    *,
    adjacent_area_radius_m: int = 0,
) -> dict[str, object]:
    """Automation thumbnails must be real map previews, never local diagram thumbnails."""
    normalized_country = str(country_code or "").strip().upper()
    normalized_region = str(region_code or "").strip().lower()
    normalized_query = str(location_query or "").strip()
    market_label_parts = [part for part in (normalized_region.replace("_", " ").title(), normalized_country) if part]
    market_label = " · ".join(market_label_parts) or "Search area"
    option_rows = _property_location_options(normalized_country, normalized_region)
    selected_values = _csv_values(normalized_query)
    option_lookup = {
        str(option.get("value") or "").strip().lower(): str(option.get("label") or option.get("value") or "").strip()
        for option in option_rows
        if str(option.get("value") or "").strip()
    }
    selected_labels = [
        option_lookup.get(value.lower(), value)
        for value in selected_values
        if str(value or "").strip()
    ]
    try:
        boundary_preview = _build_scope_boundary_preview(
            country_code=normalized_country,
            region_code=normalized_region,
            normalized_query=normalized_query,
            selected_labels=selected_labels,
            selected_values=selected_values,
            option_lookup=option_lookup,
            market_label=market_label,
            adjacent_area_radius_m=adjacent_area_radius_m,
            allow_remote_lookup=False,
            materialize_preview="async",
            padding_ratio=0.19,
        )
    except Exception:
        boundary_preview = {}
    if boundary_preview:
        image_url = str(dict(boundary_preview).get("image_url") or "").strip()
        preview_kind = str(dict(boundary_preview).get("preview_kind") or "").strip()
        if image_url.startswith("/app/api/property/map-previews/") and preview_kind == "osm_district_overlay":
            return dict(boundary_preview)
    return _property_scope_map_pending_preview(
        country_code=normalized_country,
        region_code=normalized_region,
        normalized_query=normalized_query,
        market_label=market_label,
        selected_labels=selected_labels,
    )


def _property_preference_schema() -> dict[str, object]:
    from app.api.routes.product_api_contracts import _PROPERTY_PREFERENCE_VALUE_SPECS

    category_labels = {
        "constraint": "Hard rule",
        "soft_preference": "Preference",
        "aversion": "Avoid",
    }
    value_hints = {
        "bool": "Leave empty for yes, or enter true/false.",
        "positive_number": "Enter a number.",
        "text_list": "Enter comma-separated values.",
    }
    categories: dict[str, dict[str, object]] = {}
    for category, key in sorted(_PROPERTY_PREFERENCE_VALUE_SPECS):
        value_kind = str(_PROPERTY_PREFERENCE_VALUE_SPECS[(category, key)])
        bucket = categories.setdefault(
            category,
            {
                "label": category_labels.get(category, category.replace("_", " ").title()),
                "keys": [],
            },
        )
        bucket["keys"].append(
            {
                "key": key,
                "label": key.replace("_", " ").title(),
                "value_kind": value_kind,
                "hint": value_hints.get(value_kind, "Enter a value."),
            }
        )
    return {"categories": categories}


@lru_cache(maxsize=32)
def _property_region_options_cached(country_code: str) -> tuple[tuple[str, str, str], ...]:
    from app.services.property_market_catalog import normalize_country_code, region_options_for_country

    catalogs: dict[str, list[dict[str, str]]] = {
        "AT": [
            {"value": "vienna", "label": "Vienna", "detail": "Wien and the close commuter ring"},
            {"value": "austria", "label": "All Austria", "detail": "Nationwide Austrian search"},
            {"value": "lower_austria", "label": "Lower Austria", "detail": "St. Poelten, Baden, Krems, Wiener Neustadt"},
            {"value": "upper_austria", "label": "Upper Austria", "detail": "Linz, Wels, Steyr"},
            {"value": "styria", "label": "Styria", "detail": "Graz and the southern corridor"},
            {"value": "salzburg", "label": "Salzburg", "detail": "City and surroundings"},
            {"value": "tyrol", "label": "Tyrol", "detail": "Innsbruck and Tyrolean centres"},
            {"value": "vorarlberg", "label": "Vorarlberg", "detail": "Bregenz, Dornbirn, Feldkirch"},
            {"value": "carinthia", "label": "Carinthia", "detail": "Klagenfurt and Villach"},
            {"value": "burgenland", "label": "Burgenland", "detail": "Eisenstadt and the eastern commuter belt"},
        ],
    }
    normalized_country = normalize_country_code(country_code)
    if normalized_country in catalogs:
        rows = catalogs[normalized_country]
    else:
        rows = region_options_for_country(normalized_country)
    return tuple(
        (
            str(row.get("value") or ""),
            str(row.get("label") or ""),
            str(row.get("detail") or ""),
        )
        for row in rows
    )


def _property_region_options(country_code: str) -> list[dict[str, str]]:
    return [
        {"value": value, "label": label, "detail": detail}
        for value, label, detail in _property_region_options_cached(country_code)
    ]


@lru_cache(maxsize=128)
def _property_location_options_cached(country_code: str, region_code: str = "") -> tuple[tuple[str, str, str], ...]:
    from app.services.property_market_catalog import location_options_for_country_region, normalize_country_code

    austria_catalogs: dict[str, list[dict[str, str]]] = {
        "austria": [
            {"value": "Österreich", "label": "All Austria", "detail": "Nationwide"},
            {"value": "Niederösterreich", "label": "Lower Austria", "detail": "State-wide"},
            {"value": "Oberösterreich", "label": "Upper Austria", "detail": "State-wide"},
            {"value": "Steiermark", "label": "Styria", "detail": "State-wide"},
            {"value": "Salzburg", "label": "Salzburg", "detail": "State-wide"},
            {"value": "Kärnten", "label": "Carinthia", "detail": "State-wide"},
            {"value": "Burgenland", "label": "Burgenland", "detail": "State-wide"},
            {"value": "Tirol", "label": "Tyrol", "detail": "State-wide"},
            {"value": "Vorarlberg", "label": "Vorarlberg", "detail": "State-wide"},
        ],
        "vienna": [
            {"value": "1010 Vienna", "label": "1010 Vienna", "detail": "Innere Stadt"},
            {"value": "1020 Vienna", "label": "1020 Vienna", "detail": "Leopoldstadt"},
            {"value": "1030 Vienna", "label": "1030 Vienna", "detail": "Landstrasse"},
            {"value": "1040 Vienna", "label": "1040 Vienna", "detail": "Wieden"},
            {"value": "1050 Vienna", "label": "1050 Vienna", "detail": "Margareten"},
            {"value": "1060 Vienna", "label": "1060 Vienna", "detail": "Mariahilf"},
            {"value": "1070 Vienna", "label": "1070 Vienna", "detail": "Neubau"},
            {"value": "1080 Vienna", "label": "1080 Vienna", "detail": "Josefstadt"},
            {"value": "1090 Vienna", "label": "1090 Vienna", "detail": "Alsergrund"},
            {"value": "1100 Vienna", "label": "1100 Vienna", "detail": "Favoriten"},
            {"value": "1110 Vienna", "label": "1110 Vienna", "detail": "Simmering"},
            {"value": "1120 Vienna", "label": "1120 Vienna", "detail": "Meidling"},
            {"value": "1130 Vienna", "label": "1130 Vienna", "detail": "Hietzing"},
            {"value": "1140 Vienna", "label": "1140 Vienna", "detail": "Penzing"},
            {"value": "1150 Vienna", "label": "1150 Vienna", "detail": "Rudolfsheim-Fuenfhaus"},
            {"value": "1160 Vienna", "label": "1160 Vienna", "detail": "Ottakring"},
            {"value": "1170 Vienna", "label": "1170 Vienna", "detail": "Hernals"},
            {"value": "1180 Vienna", "label": "1180 Vienna", "detail": "Waehring"},
            {"value": "1190 Vienna", "label": "1190 Vienna", "detail": "Doebling"},
            {"value": "1200 Vienna", "label": "1200 Vienna", "detail": "Brigittenau"},
            {"value": "1210 Vienna", "label": "1210 Vienna", "detail": "Floridsdorf"},
            {"value": "1220 Vienna", "label": "1220 Vienna", "detail": "Donaustadt"},
            {"value": "1230 Vienna", "label": "1230 Vienna", "detail": "Liesing"},
            {"value": "Klosterneuburg", "label": "Klosterneuburg", "detail": "Vienna outskirts"},
            {"value": "Mödling", "label": "Mödling", "detail": "South of Vienna"},
            {"value": "Purkersdorf", "label": "Purkersdorf", "detail": "West of Vienna"},
        ],
        "lower_austria": [
            {"value": "Niederösterreich", "label": "All Lower Austria", "detail": "State-wide"},
            {"value": "St. Poelten", "label": "St. Poelten", "detail": "Capital of Lower Austria"},
            {"value": "Krems", "label": "Krems", "detail": "Wachau corridor"},
            {"value": "Baden", "label": "Baden", "detail": "South of Vienna"},
            {"value": "Wiener Neustadt", "label": "Wiener Neustadt", "detail": "Southern rail corridor"},
            {"value": "Tulln", "label": "Tulln", "detail": "North-west of Vienna"},
        ],
        "upper_austria": [
            {"value": "Linz", "label": "Linz", "detail": "Capital of Upper Austria"},
            {"value": "Wels", "label": "Wels", "detail": "Central Upper Austria"},
            {"value": "Steyr", "label": "Steyr", "detail": "Industrial corridor"},
        ],
        "styria": [
            {"value": "Graz", "label": "Graz", "detail": "Capital of Styria"},
            {"value": "Leoben", "label": "Leoben", "detail": "Upper Styrian centre"},
            {"value": "Kapfenberg", "label": "Kapfenberg", "detail": "North of Graz corridor"},
        ],
        "salzburg": [
            {"value": "Salzburg", "label": "Salzburg", "detail": "City-wide"},
            {"value": "Hallein", "label": "Hallein", "detail": "South of Salzburg"},
        ],
        "tyrol": [
            {"value": "Innsbruck", "label": "Innsbruck", "detail": "City-wide"},
            {"value": "Hall in Tirol", "label": "Hall in Tirol", "detail": "East of Innsbruck"},
        ],
        "vorarlberg": [
            {"value": "Dornbirn", "label": "Dornbirn", "detail": "Rheintal centre"},
            {"value": "Bregenz", "label": "Bregenz", "detail": "Lake Constance"},
            {"value": "Feldkirch", "label": "Feldkirch", "detail": "Southern Vorarlberg"},
        ],
        "carinthia": [
            {"value": "Klagenfurt", "label": "Klagenfurt", "detail": "Capital of Carinthia"},
            {"value": "Villach", "label": "Villach", "detail": "West Carinthia"},
        ],
        "burgenland": [
            {"value": "Eisenstadt", "label": "Eisenstadt", "detail": "Capital of Burgenland"},
            {"value": "Neusiedl am See", "label": "Neusiedl am See", "detail": "North Burgenland"},
        ],
    }
    catalogs: dict[str, list[dict[str, str]]] = {
        "AT": list(austria_catalogs.get(str(region_code or "").strip().lower() or "vienna", austria_catalogs["vienna"])),
        "DE": [
            {"value": "Berlin Mitte", "label": "Berlin Mitte", "detail": "Central Berlin"},
            {"value": "Berlin Prenzlauer Berg", "label": "Berlin Prenzlauer Berg", "detail": "Family-friendly"},
            {"value": "Berlin Charlottenburg", "label": "Berlin Charlottenburg", "detail": "West Berlin"},
            {"value": "Munich", "label": "Munich", "detail": "City-wide"},
            {"value": "Hamburg", "label": "Hamburg", "detail": "City-wide"},
        ],
        "ES": [
            {"value": "Barcelona", "label": "Barcelona", "detail": "City-wide"},
            {"value": "Eixample", "label": "Eixample", "detail": "Central Barcelona"},
            {"value": "Madrid", "label": "Madrid", "detail": "City-wide"},
            {"value": "Valencia", "label": "Valencia", "detail": "City-wide"},
        ],
        "IT": [
            {"value": "Milan", "label": "Milan", "detail": "City-wide"},
            {"value": "Rome", "label": "Rome", "detail": "City-wide"},
            {"value": "Bologna", "label": "Bologna", "detail": "City-wide"},
        ],
        "FR": [
            {"value": "Paris", "label": "Paris", "detail": "City-wide"},
            {"value": "Lyon", "label": "Lyon", "detail": "City-wide"},
            {"value": "Marseille", "label": "Marseille", "detail": "City-wide"},
        ],
        "NL": [
            {"value": "Amsterdam", "label": "Amsterdam", "detail": "City-wide"},
            {"value": "Rotterdam", "label": "Rotterdam", "detail": "City-wide"},
            {"value": "Utrecht", "label": "Utrecht", "detail": "City-wide"},
        ],
        "UK": [
            {"value": "London", "label": "London", "detail": "City-wide"},
            {"value": "Manchester", "label": "Manchester", "detail": "City-wide"},
            {"value": "Bristol", "label": "Bristol", "detail": "City-wide"},
        ],
        "US": [
            {"value": "Brooklyn", "label": "Brooklyn", "detail": "New York City"},
            {"value": "Queens", "label": "Queens", "detail": "New York City"},
            {"value": "Jersey City", "label": "Jersey City", "detail": "New Jersey"},
            {"value": "San Francisco", "label": "San Francisco", "detail": "Bay Area"},
            {"value": "Boston", "label": "Boston", "detail": "City-wide"},
        ],
    }
    normalized_country = normalize_country_code(country_code)
    if normalized_country in catalogs:
        rows = catalogs[normalized_country]
    else:
        rows = location_options_for_country_region(normalized_country, region_code)
    return tuple(
        (
            str(row.get("value") or ""),
            str(row.get("label") or ""),
            str(row.get("detail") or ""),
        )
        for row in rows
    )


@lru_cache(maxsize=1)
def _vienna_district_map_option_records() -> dict[str, dict[str, str]]:
    records = _vienna_district_boundary_records()
    bounds_rows = [
        bounds
        for row in records.values()
        if isinstance((bounds := row.get("bounds")), tuple) and len(bounds) == 4
    ]
    render_bounds = _union_geo_bounds(bounds_rows)
    if render_bounds is None:
        return {}
    render_bounds = _expand_geo_bounds(render_bounds, padding_ratio=0.05)
    rows: dict[str, dict[str, str]] = {}
    for postal_code, row in records.items():
        rings = row.get("rings")
        if not isinstance(rings, list) or not rings:
            continue
        path_parts: list[str] = []
        label_points: list[tuple[float, float]] = []
        for raw_ring in rings[:2]:
            if not isinstance(raw_ring, list):
                continue
            points: list[tuple[float, float]] = []
            for raw_point in raw_ring:
                if not isinstance(raw_point, list) or len(raw_point) < 2:
                    continue
                try:
                    points.append((float(raw_point[0]), float(raw_point[1])))
                except (TypeError, ValueError):
                    continue
            if len(points) < 4:
                continue
            path, centroid = _project_lonlat_to_preview_path(points, render_bounds, width=360.0, height=286.0)
            if path:
                path_parts.append(path)
                label_points.append(centroid)
        if not path_parts:
            continue
        if label_points:
            label_x = sum(point[0] for point in label_points) / len(label_points)
            label_y = sum(point[1] for point in label_points) / len(label_points)
        else:
            label_x, label_y = 180.0, 143.0
        rows[str(postal_code)] = {
            "map_path": " ".join(path_parts),
            "map_label_x": f"{label_x:.1f}",
            "map_label_y": f"{label_y:.1f}",
            "map_source": "OpenStreetMap-derived Vienna district boundaries",
        }
    return rows


def _property_location_options(country_code: str, region_code: str = "") -> list[dict[str, str]]:
    from app.services.property_market_catalog import normalize_country_code

    normalized_country = normalize_country_code(country_code)
    normalized_region = str(region_code or "").strip().lower()
    district_geometry = (
        _vienna_district_map_option_records()
        if normalized_country == "AT" and normalized_region in {"vienna", "wien"}
        else {}
    )
    rows: list[dict[str, str]] = []
    for value, label, detail in _property_location_options_cached(country_code, region_code):
        option = {"value": value, "label": label, "detail": detail}
        match = re.search(r"\b(1[0-2]\d0)\b", value)
        if match:
            option.update(district_geometry.get(match.group(1), {}))
        rows.append(option)
    return rows


@lru_cache(maxsize=1)
def _property_keyword_options_cached() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (
            str(row["value"]),
            str(row["label"]),
            str(row["detail"]),
        )
        for row in [
        {"value": "lift", "label": "Lift", "detail": "Elevator in the building"},
        {"value": "barrier-free", "label": "Barrier-free", "detail": "Wheelchair accessible or step-free"},
        {"value": "balcony", "label": "Balcony", "detail": "Outdoor private space"},
        {"value": "terrace", "label": "Terrace", "detail": "Large outdoor space"},
        {"value": "klimaanlage", "label": "Klimaanlage", "detail": "Active cooling mentioned in the listing"},
        {"value": "dachgeschosswohnung", "label": "Dachgeschoßwohnung", "detail": "Top-floor or attic apartment signal"},
        {"value": "baugrund", "label": "Building plot", "detail": "Land / building plot"},
        {"value": "seezugang", "label": "Lake access", "detail": "Lake access or lakeside potential"},
        {"value": "wasserzugang", "label": "Water access", "detail": "Access to water"},
        {"value": "family", "label": "Family-friendly", "detail": "Good fit for children"},
        {"value": "playground nearby", "label": "Playground", "detail": "Walkable play options"},
        {"value": "library nearby", "label": "Library", "detail": "Books, study, and rainy-day backup"},
        {"value": "zoo nearby", "label": "Zoo", "detail": "Family weekend-life signal"},
        {"value": "public pool nearby", "label": "Public pool", "detail": "Swimming and sport access"},
        {"value": "medical care nearby", "label": "Medical care", "detail": "Doctors, clinics, and hospitals"},
        {"value": "supermarket nearby", "label": "Supermarket", "detail": "Daily errands close by"},
        {"value": "market nearby", "label": "Market", "detail": "Produce markets and district-life errands"},
        {"value": "Baumarkt nearby", "label": "Hardware store", "detail": "DIY and practical errands"},
        {"value": "shopping center nearby", "label": "Shopping center", "detail": "Bad-weather fallback for errands"},
        {"value": "flaniermeile nearby", "label": "Promenade", "detail": "Walkable city-life access"},
        {"value": "theatre nearby", "label": "Theatre", "detail": "Culture and evening-life access"},
        {"value": "pharmacy nearby", "label": "Pharmacy", "detail": "Healthcare basics"},
        {"value": "underground nearby", "label": "Underground", "detail": "Fast transit access"},
        {"value": "good air quality", "label": "Good air quality", "detail": "Treat air burden as a real quality signal"},
        {"value": "klimaerwaermungsfit", "label": "Stays cool in summer", "detail": "Can the home stay cool during longer heat waves?"},
        {"value": "avoid noise-risk area", "label": "Avoid noise-risk area", "detail": "Treat official noise burden as a genuine location risk"},
        {"value": "high-speed internet", "label": "High-speed internet", "detail": "Broadband quality matters for the final call"},
        {"value": "low crime area", "label": "Low crime area", "detail": "Treat quarter-level safety burden as a real signal"},
        {"value": "water and groundwater check", "label": "Water source and groundwater check", "detail": "Research water source and groundwater burden"},
        {"value": "parking pressure check", "label": "Parking pressure check", "detail": "Check parking situation if the listing has no garage"},
        {"value": "avoid septic risk", "label": "Avoid septic risk", "detail": "Avoid Senkgrube or septic burden"},
        {"value": "winter driving check", "label": "Winter driving check", "detail": "Check seasonal driving and slope burden"},
        {"value": "avoid flood-risk area", "label": "Avoid flood-risk area", "detail": "Treat flooding and runoff exposure as a real risk"},
        {"value": "no gas", "label": "No gas heating", "detail": "Avoid gas-based systems"},
        {"value": "district heating", "label": "District heating", "detail": "Prefer Fernwärme"},
        {"value": "parking", "label": "Parking", "detail": "Car-friendly"},
        {"value": "pets allowed", "label": "Pets allowed", "detail": "Pet-friendly rules"},
        {"value": "quiet", "label": "Quiet", "detail": "Lower street noise"},
        {"value": "bright", "label": "Bright", "detail": "Good natural light"},
        ]
    )


def _localized_property_ui_text(localized: object, language_code: object, fallback: object) -> str:
    text = str(fallback or "").strip()
    if not isinstance(localized, dict) or not localized:
        return text
    normalized = str(language_code or "").strip().lower().replace("_", "-")
    candidates: list[str] = []
    if normalized:
        candidates.append(normalized)
        if "-" in normalized:
            candidates.append(normalized.split("-", 1)[0])
    for candidate in (*candidates, "de", "en"):
        value = str(localized.get(candidate) or "").strip()
        if value:
            return value
    for value in localized.values():
        value = str(value or "").strip()
        if value:
            return value
    return text


def _property_heat_resilience_copy(language_code: object) -> dict[str, object]:
    labels = {
        "de": "Bleibt im Sommer kühl",
        "en": "Stays cool in summer",
        "es": "Se mantiene fresca en verano",
        "fr": "Reste frais en ete",
        "it": "Resta fresca in estate",
        "nl": "Blijft koel in de zomer",
        "pt": "Mantem-se fresca no verao",
        "pl": "Pozostaje chlodne latem",
        "sv": "Haller sig sval pa sommaren",
    }
    details = {
        "de": "Kann die Wohnung auch bei längeren Hitzeperioden kühl bleiben?",
        "en": "Can the home stay cool during longer heat waves?",
        "es": "Puede mantenerse fresca la vivienda durante olas de calor largas?",
        "fr": "Le logement peut-il rester frais pendant de longues periodes de chaleur?",
        "it": "La casa resta fresca durante lunghe ondate di caldo?",
        "nl": "Kan de woning koel blijven tijdens langere hitteperiodes?",
        "pt": "A casa consegue manter-se fresca em ondas de calor prolongadas?",
        "pl": "Czy mieszkanie pozostaje chlodne podczas dlugich fal upalu?",
        "sv": "Kan bostaden halla sig sval under langre varmeperioder?",
    }
    tooltips = {
        "de": "Prueft Sommerhitze, Dachgeschoss, grosse suedseitige Fenster, heisse Stadtlagen, Klimaanlage, Altbau, Schatten, Baeume, lokale Kaelteschneisen an fliessendem Wasser und Aussenjalousien als Score-Signal.",
        "en": "Checks heat waves, top-floor risk, large south-facing windows, hotter city areas, cooling, old-building thermal mass, shade, trees, nearby flowing-water cooling corridors, and external blinds.",
        "es": "Comprueba olas de calor, riesgo de atico, grandes ventanas al sur, zonas urbanas mas calientes, aire acondicionado, muros gruesos, sombra, corredores de enfriamiento junto al agua en movimiento y persianas exteriores.",
        "fr": "Verifie chaleur estivale, risque de dernier etage, grandes fenetres au sud, secteurs urbains plus chauds, climatisation, murs epais, ombre, couloirs de fraicheur pres de l eau courante et stores exterieurs.",
        "it": "Controlla ondate di calore, rischio ultimo piano, grandi finestre a sud, zone urbane piu calde, climatizzazione, muri spessi, ombra, corridoi di raffrescamento vicino ad acqua corrente e schermature esterne.",
        "nl": "Controleert hittegolven, risico van bovenste verdieping, grote zuidramen, warmere stadsdelen, koeling, dikke muren, schaduw, koele corridors langs stromend water en buitenzonwering.",
        "pt": "Verifica ondas de calor, risco de ultimo andar, grandes janelas a sul, zonas urbanas mais quentes, ar condicionado, paredes espessas, sombra, corredores de arrefecimento junto de agua corrente e estores exteriores.",
        "pl": "Sprawdza fale upalow, ryzyko ostatniego pietra, duze okna od poludnia, gorace dzielnice, klimatyzacje, grube sciany, cien, korytarze chlodzace przy plynacej wodzie i rolety zewnetrzne.",
        "sv": "Kontrollerar varmeboljor, risk pa oversta vaningen, stora sodervanda fonster, varmare stadsdelar, kylning, tjocka vaggar, skugga, svalkande korridorer vid rinnande vatten och utvandiga solskydd.",
    }
    return {
        "label": _localized_property_ui_text(labels, language_code, "Stays cool in summer"),
        "detail": _localized_property_ui_text(details, language_code, "Can the home stay cool during longer heat waves?"),
        "tooltip": _localized_property_ui_text(
            tooltips,
            language_code,
            "Checks heat waves, top-floor risk, large south-facing windows, hotter city areas, cooling, old-building thermal mass, shade, trees, nearby flowing-water cooling corridors, and external blinds.",
        ),
        "label_i18n": labels,
        "detail_i18n": details,
        "tooltip_i18n": tooltips,
    }


def _property_keyword_options() -> list[dict[str, str]]:
    daily_life_keywords = {
        "playground nearby",
        "library nearby",
        "zoo nearby",
        "public pool nearby",
        "medical care nearby",
        "supermarket nearby",
        "market nearby",
        "Baumarkt nearby",
        "shopping center nearby",
        "flaniermeile nearby",
        "theatre nearby",
        "pharmacy nearby",
        "underground nearby",
    }
    risk_evidence_keywords = {
        "good air quality",
        "klimaerwaermungsfit",
        "avoid noise-risk area",
        "high-speed internet",
        "low crime area",
        "water and groundwater check",
        "parking pressure check",
        "avoid septic risk",
        "winter driving check",
        "avoid flood-risk area",
    }
    preference_options = {
        "playground nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "library nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "zoo nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "public pool nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "medical care nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "supermarket nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "market nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "Baumarkt nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "shopping center nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "flaniermeile nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "theatre nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "pharmacy nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "underground nearby": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "good air quality": [
            {"value": "any", "label": "Neutral"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "klimaerwaermungsfit": [
            {"value": "any", "label": "Neutral"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "klimaanlage": [
            {"value": "any", "label": "Neutral"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "dachgeschosswohnung": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "avoid noise-risk area": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "must_have", "label": "Must avoid"},
        ],
        "high-speed internet": [
            {"value": "any", "label": "Neutral"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "low crime area": [
            {"value": "any", "label": "Neutral"},
            {"value": "nice_to_have", "label": "Nice to have"},
            {"value": "important", "label": "Strong wish"},
            {"value": "must_have", "label": "Must have"},
        ],
        "water and groundwater check": [
            {"value": "any", "label": "Neutral"},
            {"value": "important", "label": "Detailed check"},
            {"value": "must_have", "label": "Required check"},
        ],
        "parking pressure check": [
            {"value": "any", "label": "Neutral"},
            {"value": "low", "label": "Low"},
            {"value": "medium", "label": "Medium"},
            {"value": "high", "label": "High"},
        ],
        "avoid septic risk": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "must_have", "label": "Must avoid"},
        ],
        "winter driving check": [
            {"value": "any", "label": "Neutral"},
            {"value": "important", "label": "Detailed check"},
            {"value": "must_have", "label": "Required check"},
        ],
        "avoid flood-risk area": [
            {"value": "any", "label": "Neutral"},
            {"value": "avoid", "label": "Avoid"},
            {"value": "must_have", "label": "Must avoid"},
        ],
    }
    default_distance_options = [
        {"value": "100", "label": "100 m"},
        {"value": "250", "label": "250 m"},
        {"value": "500", "label": "500 m"},
        {"value": "1000", "label": "1 km"},
        {"value": "2000", "label": "2 km"},
        {"value": "5000", "label": "5 km"},
    ]
    long_distance_options = [
        {"value": "250", "label": "250 m"},
        {"value": "500", "label": "500 m"},
        {"value": "1000", "label": "1 km"},
        {"value": "2000", "label": "2 km"},
        {"value": "5000", "label": "5 km"},
        {"value": "7000", "label": "7 km"},
    ]
    distance_options = {
        "market nearby": long_distance_options,
        "Baumarkt nearby": long_distance_options,
        "shopping center nearby": long_distance_options,
        "flaniermeile nearby": long_distance_options,
        "theatre nearby": long_distance_options,
    }
    heat_resilience_copy = _property_heat_resilience_copy("de")
    localized_details = {
        "klimaerwaermungsfit": dict(heat_resilience_copy.get("detail_i18n") or {}),
        "klimaanlage": {
            "de": "Bevorzugt Wohnungen mit ausdrücklich genannter Klimaanlage oder aktiver Kühlung.",
            "en": "Prefers homes where air conditioning or active cooling is explicitly mentioned.",
        },
        "dachgeschosswohnung": {
            "de": "Bewertet Dachgeschoßwohnungen als weichen Wunsch oder Malus, nicht als versteckten Ausschluss.",
            "en": "Treats attic or top-floor apartments as a soft wish or penalty, not as a hidden exclusion.",
        },
    }
    localized_labels = {
        "klimaerwaermungsfit": dict(heat_resilience_copy.get("label_i18n") or {}),
        "klimaanlage": {
            "de": "Klimaanlage",
            "en": "Air conditioning",
        },
        "dachgeschosswohnung": {
            "de": "Dachgeschoßwohnung",
            "en": "Top-floor apartment",
        },
    }
    localized_tooltips = {
        "klimaerwaermungsfit": dict(heat_resilience_copy.get("tooltip_i18n") or {}),
        "klimaanlage": {
            "de": "Hebt Inserate mit Klimaanlage, Splitgerät oder anderer aktiver Kühlung im Ranking. Fehlende Angabe senkt nur leicht, solange es kein Must-have ist.",
            "en": "Raises listings with air conditioning, split units, or other active cooling. Missing evidence only lowers the rank lightly unless marked must-have.",
        },
        "dachgeschosswohnung": {
            "de": "Kann als Wunsch oder Vermeiden-Regel gesetzt werden. Bei Sommerhitze zählt Dachgeschoß zusätzlich als Risiko, sofern Kühlung oder Verschattung nicht dagegen sprechen.",
            "en": "Can be set as a wish or avoid rule. For summer heat, top-floor homes also count as a risk unless cooling or shade offsets it.",
        },
    }
    return [
        {
            "value": value,
            "label": label,
            "display_key": re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-"),
            "detail": detail,
            **({"label_i18n": localized_labels[value]} if value in localized_labels else {}),
            **({"detail_i18n": localized_details[value]} if value in localized_details else {}),
            **({"tooltip_i18n": localized_tooltips[value]} if value in localized_tooltips else {}),
            "group": "daily_life" if value in daily_life_keywords else ("risk_evidence" if value in risk_evidence_keywords else "home_basics"),
            **({"preference_options": preference_options[value]} if value in preference_options else {}),
            **({"distance_options": distance_options.get(value, default_distance_options)} if value in {"playground nearby", "library nearby", "zoo nearby", "public pool nearby", "medical care nearby", "supermarket nearby", "market nearby", "Baumarkt nearby", "shopping center nearby", "flaniermeile nearby", "theatre nearby", "pharmacy nearby", "underground nearby"} else {}),
        }
        for value, label, detail in _property_keyword_options_cached()
    ]


def _property_school_preference_options(
    *,
    selected_school_stage_preferences: list[str],
    require_school_evidence: bool,
    school_evidence_priority: str,
    property_preferences: dict[str, object] | None = None,
) -> list[dict[str, str]]:
    selected = {str(item or "").strip().lower() for item in selected_school_stage_preferences if str(item or "").strip()}
    preferences = dict(property_preferences or {})
    evidence_priority = str(school_evidence_priority or "any").strip().lower()
    if require_school_evidence and evidence_priority == "very_important":
        selected_state = "must_have"
    elif require_school_evidence and evidence_priority == "important":
        selected_state = "important"
    elif require_school_evidence:
        selected_state = "important"
    else:
        selected_state = "nice_to_have"
    distance_fields = {
        "kindergarten": "max_distance_to_kindergarten_m",
        "ganztags_volksschule": "max_distance_to_ganztags_volksschule_m",
        "halbtags_volksschule": "max_distance_to_halbtags_volksschule_m",
    }
    importance_fields = {
        "kindergarten": "max_distance_to_kindergarten_importance",
        "ganztags_volksschule": "max_distance_to_ganztags_volksschule_importance",
        "halbtags_volksschule": "max_distance_to_halbtags_volksschule_importance",
    }
    distance_options = [
        {"value": "100", "label": "100 m"},
        {"value": "250", "label": "250 m"},
        {"value": "500", "label": "500 m"},
        {"value": "1000", "label": "1 km"},
        {"value": "2000", "label": "2 km"},
        {"value": "5000", "label": "5 km"},
    ]

    def school_option(value: str, label: str, detail: str) -> dict[str, object]:
        state = selected_state if value in selected else "any"
        option: dict[str, object] = {
            "value": value,
            "label": label,
            "detail": detail,
            "state": state,
        }
        distance_field = distance_fields.get(value)
        importance_field = importance_fields.get(value)
        if distance_field and importance_field:
            raw_distance = preferences.get(distance_field)
            stored_distance_active = False
            try:
                parsed_distance = int(float(raw_distance)) if raw_distance not in (None, "") else 0
                stored_distance_active = parsed_distance > 0
                distance_state = str(parsed_distance) if stored_distance_active else "500"
            except Exception:
                distance_state = "500"
            stored_importance = str(preferences.get(importance_field) or "").strip().lower()
            if state == "any" and stored_importance in {"nice_to_have", "important", "must_have"}:
                option["state"] = stored_importance
            elif state == "any" and stored_distance_active:
                option["state"] = "important"
            option.update(
                {
                    "distance_options": distance_options,
                    "distance_state": distance_state,
                    "distance_field": distance_field,
                    "importance_field": importance_field,
                }
            )
        return option

    return [
        school_option("kindergarten", "Kindergarten", "General kindergarten coverage"),
        {
            "value": "public_kindergarten",
            "label": "Public kindergarten",
            "detail": "Municipal childcare coverage",
            "state": selected_state if "public_kindergarten" in selected else "any",
        },
        {
            "value": "private_kindergarten",
            "label": "Private kindergarten",
            "detail": "Private childcare coverage",
            "state": selected_state if "private_kindergarten" in selected else "any",
        },
        {
            "value": "volksschule",
            "label": "Volksschule",
            "detail": "Primary school coverage",
            "state": selected_state if "volksschule" in selected else "any",
        },
        school_option("ganztags_volksschule", "Ganztagsvolksschule", "Full-day primary school coverage"),
        school_option("halbtags_volksschule", "Halbtagsvolksschule", "Half-day primary school coverage"),
        {
            "value": "gymnasium",
            "label": "Gymnasium",
            "detail": "Secondary academic-track coverage",
            "state": selected_state if "gymnasium" in selected else "any",
        },
    ]


@lru_cache(maxsize=8)
def _property_region_catalog_by_country_cached(country_values: tuple[str, ...]) -> tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...]:
    return tuple(
        (
            country_code,
            tuple(
                (row["value"], row["label"], row["detail"])
                for row in _property_region_options(country_code)
            ),
        )
        for country_code in country_values
        if country_code
    )


def _property_region_catalog_by_country(country_values: tuple[str, ...]) -> dict[str, list[dict[str, str]]]:
    return {
        country_code: [
            {"value": value, "label": label, "detail": detail}
            for value, label, detail in rows
        ]
        for country_code, rows in _property_region_catalog_by_country_cached(country_values)
    }


@lru_cache(maxsize=8)
def _property_market_filter_capabilities_catalog_cached(
    country_values: tuple[str, ...],
) -> tuple[tuple[str, tuple[tuple[str, tuple[tuple[str, bool], ...]], ...]], ...]:
    return tuple(
        (
            country_code,
            tuple(
                (
                    str(region.get("value") or ""),
                    tuple(sorted(_property_market_filter_capabilities(country_code, str(region.get("value") or "")).items())),
                )
                for region in _property_region_options(country_code)
            ),
        )
        for country_code in country_values
        if country_code
    )


def _property_market_filter_capabilities_catalog(country_values: tuple[str, ...]) -> dict[str, dict[str, dict[str, bool]]]:
    return {
        country_code: {
            region_code: {key: bool(value) for key, value in capability_rows}
            for region_code, capability_rows in region_rows
        }
        for country_code, region_rows in _property_market_filter_capabilities_catalog_cached(country_values)
    }


@lru_cache(maxsize=8)
def _property_location_catalog_by_country_region_cached(
    country_values: tuple[str, ...],
) -> tuple[tuple[str, tuple[tuple[str, tuple[tuple[tuple[str, str], ...], ...]], ...]], ...]:
    option_keys = ("value", "label", "detail", "map_path", "map_label_x", "map_label_y", "map_source")
    return tuple(
        (
            country_code,
            tuple(
                (
                    str(region.get("value") or ""),
                    tuple(
                        tuple(
                            (key, str(row.get(key) or ""))
                            for key in option_keys
                            if str(row.get(key) or "").strip()
                        )
                        for row in _property_location_options(country_code, str(region.get("value") or ""))
                    ),
                )
                for region in _property_region_options(country_code)
            ),
        )
        for country_code in country_values
        if country_code
    )


def _property_location_catalog_by_country_region(country_values: tuple[str, ...]) -> dict[str, dict[str, list[dict[str, str]]]]:
    return {
        country_code: {
            region_code: [
                {key: value for key, value in option_row}
                for option_row in location_rows
            ]
            for region_code, location_rows in region_rows
        }
        for country_code, region_rows in _property_location_catalog_by_country_region_cached(country_values)
    }


def humanize(value: str) -> str:
    return str(value or "").strip().replace("_", " ") or "unknown"


def status_tone(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"connected", "ready_to_connect", "ready_for_brief", "completed", "started", "available"}:
        return "good"
    if normalized in {"planned_business", "export_planned", "guided_manual", "bot_link_requested", "export_intake_complete", "import_acknowledged", "in_progress"}:
        return "warn"
    if normalized in {"credentials_missing", "planned_not_available", "not_selected", "anonymous"}:
        return "muted"
    return "muted"


def list_rows(values: object, fallback: tuple[str, ...]) -> list[str]:
    rows: list[str] = []
    if isinstance(values, (list, tuple, set)):
        for value in values:
            normalized = str(value or "").strip()
            if normalized:
                rows.append(normalized)
    elif values:
        normalized = str(values).strip()
        if normalized:
            rows.append(normalized)
    return rows or [str(row) for row in fallback]


def row_item(title: str, detail: str, tag: str) -> dict[str, str]:
    return {"title": title, "detail": detail, "tag": tag}


def string_rows(values: object, fallback: tuple[str, ...], *, tag: str, detail: str) -> list[dict[str, str]]:
    return [row_item(value, detail, tag) for value in list_rows(values, fallback)]


def _compact_when(value: str | None, fallback: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return fallback
    if "T" in normalized:
        return normalized.split("T", 1)[0]
    return normalized


def _property_candidate_ref(candidate: dict[str, object]) -> str:
    explicit_ref = str(candidate.get("candidate_ref") or candidate.get("research_candidate_ref") or "").strip()
    if explicit_ref:
        return explicit_ref
    raw = "|".join(
        str(candidate.get(key) or "").strip()
        for key in ("title", "property_url", "review_url", "source_ref", "source_label")
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def approval_rows(values: object) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        reason = str(getattr(value, "reason", "") or "").strip()
        action_json = dict(getattr(value, "requested_action_json", {}) or {})
        action_name = humanize(str(action_json.get("action") or action_json.get("event_type") or "review"))
        title = reason or f"{action_name.capitalize()} needs approval"
        detail = " · ".join(
            part
            for part in (
                "Pending approval",
                action_name if action_name and action_name != "review" else "",
                f"Expires {_compact_when(getattr(value, 'expires_at', None), 'soon')}"
                if getattr(value, "expires_at", None)
                else "",
            )
            if part
        )
        rows.append(row_item(title, detail or "Pending approval", "Approval"))
    return rows


def human_task_rows(values: object) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        raw_title = str(getattr(value, "brief", "") or "").strip()
        task_type = str(getattr(value, "task_type", "") or "follow_up")
        fallback_title = "Commitment" if task_type == "follow_up" else humanize(task_type).capitalize()
        title = raw_title or fallback_title
        priority = humanize(str(getattr(value, "priority", "") or "open"))
        role_required = humanize(str(getattr(value, "role_required", "") or "review"))
        why_human = str(getattr(value, "why_human", "") or "").strip()
        due_label = _compact_when(getattr(value, "sla_due_at", None), "")
        detail = " · ".join(
            part
            for part in (
                f"{priority.capitalize()} priority" if priority else "",
                role_required if role_required and role_required != "review" else "",
                f"Due {due_label}" if due_label else "",
                why_human if why_human else "",
            )
            if part
        )
        rows.append(row_item(title, detail or "Waiting on human review", "Task"))
    return rows


def delivery_rows(values: object) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        recipient = str(getattr(value, "recipient", "") or "").strip()
        channel = humanize(str(getattr(value, "channel", "") or "delivery")).capitalize()
        title = recipient or f"{channel} delivery"
        attempt_count = int(getattr(value, "attempt_count", 0) or 0)
        next_attempt_at = _compact_when(getattr(value, "next_attempt_at", None), "")
        last_error = str(getattr(value, "last_error", "") or "").strip()
        detail = " · ".join(
            part
            for part in (
                channel,
                f"Attempt {attempt_count + 1}",
                f"Retry {next_attempt_at}" if next_attempt_at else "",
                last_error[:80] if last_error else "",
            )
            if part
        )
        rows.append(row_item(title, detail or "Queued for delivery", "Queued"))
    return rows


def channel_cards(channels: dict[str, Any]) -> list[dict[str, str]]:
    ordered = (
        ("google", "Google sign-in", "/integrations/google"),
        ("telegram", "Telegram", "/integrations/telegram"),
        ("whatsapp", "WhatsApp", "/integrations/whatsapp"),
    )
    cards: list[dict[str, str]] = []
    for key, label, href in ordered:
        channel = dict(channels.get(key) or {})
        cards.append(
            {
                "label": label,
                "href": href,
                "status": humanize(str(channel.get("status") or "not_selected")),
                "tone": status_tone(str(channel.get("status") or "not_selected")),
                "detail": str(channel.get("detail") or "Not configured yet."),
                "summary": str(channel.get("bundle_summary") or channel.get("history_import_posture") or ""),
            }
        )
    return cards


def app_section_payload(
    section: str,
    status: dict[str, object],
    *,
    live_feed: dict[str, object] | None = None,
    property_context: dict[str, object] | None = None,
) -> dict[str, object]:
    workspace = dict(status.get("workspace") or {})
    privacy = dict(status.get("privacy") or {})
    delivery_preferences = dict(status.get("delivery_preferences") or {})
    morning_memo = dict(delivery_preferences.get("morning_memo") or {})
    preview = dict(status.get("brief_preview") or {})
    channels = dict(status.get("channels") or {})
    cards = channel_cards(channels)
    selected = [str(value) for value in (status.get("selected_channels") or []) if str(value).strip()]
    live = dict(live_feed or {})
    approvals = list(live.get("approvals") or [])
    human_tasks = list(live.get("human_tasks") or [])
    pending_delivery = list(live.get("pending_delivery") or [])
    status_label = humanize(str(status.get("status") or "draft"))
    ready_channels = sum(1 for card in cards if card["tone"] == "good")
    selected_count = len(selected) or len([card for card in cards if card["status"] != "not selected"]) or 0
    stats = [
        {"label": "Reviews", "value": str(len(approvals))},
        {"label": "Follow-ups", "value": str(len(human_tasks))},
        {"label": "Queued alerts", "value": str(len(pending_delivery))},
        {
            "label": "Channels ready",
            "value": f"{ready_channels}/{selected_count}" if selected_count else str(ready_channels),
        },
    ]
    first_brief = list_rows(
        preview.get("first_brief_preview") or preview.get("first_brief"),
        ("Connect Google sign-in if you want a faster return path and account access without another sign-up.",),
    )
    suggested = list_rows(preview.get("suggested_actions"), ("Finish onboarding and create the first saved search.",))
    trust_notes = list_rows(preview.get("trust_notes"), ("Keep retention and sharing settings explicit.",))
    people = list_rows(preview.get("top_contacts"), ("No collaborators added yet.",))
    themes = list_rows(preview.get("top_themes"), ("No themes surfaced yet.",))
    approvals_items = approval_rows(approvals)
    human_task_items = human_task_rows(human_tasks)
    pending_delivery_items = delivery_rows(pending_delivery)
    live_queue = (approvals_items + human_task_items)[:6]
    privacy_lines = [
        f"Retention: {humanize(str(privacy.get('retention_mode') or 'not set'))}",
        f"Prepared messages: {'allowed' if privacy.get('allow_drafts') else 'manual only'}",
        f"Action suggestions: {'allowed' if privacy.get('allow_action_suggestions') else 'off'}",
        f"Scheduled emails: {'allowed' if privacy.get('allow_auto_briefs') else 'off'}",
    ]
    if privacy.get("allow_auto_briefs"):
        privacy_lines.append(
            "Email schedule: "
            + " · ".join(
                part
                for part in (
                    humanize(str(morning_memo.get("cadence") or "daily_morning")),
                    f"{morning_memo.get('delivery_time_local') or '08:00'} {morning_memo.get('timezone') or workspace.get('timezone') or 'UTC'}",
                    str(morning_memo.get("resolved_recipient_email") or "waiting for recipient"),
                )
                if str(part or "").strip()
            )
        )
    channel_lines = [f"{card['label']}: {card['status']} — {card['detail']}" for card in cards]
    channel_items = [row_item(card["label"], card["detail"], card["status"]) for card in cards]
    identity_posture_items = [
        row_item(
            "Keep identity boring",
            "Return through a secure email link, invite, or SSO before widening channel setup.",
            "Recommended",
        ),
        row_item(
            "Connect Google for return access",
            "Treat Google as optional account access first; only widen scopes later if the product truly needs them.",
            "Linked",
        ),
        row_item(
            "Link messaging channels later",
            "Treat Telegram and WhatsApp as optional linked channels, not the workspace core.",
            "Linked",
        ),
        row_item(
            "Keep automation bounded",
            "Reviews, follow-ups, and queued alerts stay explicit instead of hiding behind automation copy.",
            "Guardrail",
        ),
    ]
    follow_up_context_items = [
        row_item(title, "Keep the property, question, or deadline attached to the follow-up.", "Context")
        for title in trust_notes
    ]
    property_state = dict(property_context or {})
    surface_scope = PropertySurfaceScope.for_section(str(property_state.get("surface_mode") or "properties"))
    property_run = dict(property_state.get("run") or {})
    property_run_preferences = (
        dict(property_run.get("property_search_preferences") or property_run.get("preferences") or {})
        if isinstance(property_run.get("property_search_preferences") or property_run.get("preferences"), dict)
        else {}
    )
    property_saved_preferences = dict(property_state.get("preferences") or {})
    property_preferences = (
        property_saved_preferences
        if surface_scope.section in {"search", "agents", "alerts", "account", "billing", "settings"}
        else {
            **property_saved_preferences,
            **property_run_preferences,
        }
    )
    if isinstance(property_run.get("summary"), dict):
        property_run["summary"] = _property_customer_run_summary(
            dict(property_run.get("summary") or {}),
            preferences=property_preferences,
        )
    property_summary = dict(property_run.get("summary") or {})
    saved_shortlist_candidates = [
        dict(row)
        for row in list(property_state.get("saved_shortlist_candidates") or [])
        if isinstance(row, dict)
    ]
    if surface_scope.section in {"properties", "shortlist"} and saved_shortlist_candidates:
        from app.product.service import (
            _property_candidate_matches_requested_location,
            _property_search_location_hints,
        )

        requested_run_id = str(property_state.get("requested_run_id") or "").strip()
        active_run_id = str(property_run.get("run_id") or "").strip()
        explicit_run_scope = bool(requested_run_id)
        if explicit_run_scope and active_run_id:
            saved_shortlist_candidates = [
                dict(row)
                for row in saved_shortlist_candidates
                if str(dict(row).get("saved_from_run_id") or "").strip() == active_run_id
            ]
        current_ranked = [
            dict(row)
            for row in list(property_summary.get("ranked_candidates") or [])
            if isinstance(row, dict)
        ]
        location_hints = _property_search_location_hints(property_run_preferences)
        hard_scope_country = str(property_run_preferences.get("country_code") or "").strip()
        hard_scope_region = str(property_run_preferences.get("region_code") or "").strip()
        enforce_saved_location_scope = bool(location_hints)

        def _saved_shortlist_ref(candidate: dict[str, object]) -> str:
            facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
            for value in (
                candidate.get("property_ref"),
                candidate.get("property_url"),
                candidate.get("source_url"),
                facts.get("listing_url"),
                candidate.get("source_ref"),
                candidate.get("candidate_ref"),
            ):
                normalized = str(value or "").strip()
                if normalized:
                    return normalized
            return ""

        def _saved_shortlist_in_scope(candidate: dict[str, object]) -> bool:
            if not enforce_saved_location_scope:
                return True
            facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
            for key in ("location", "postal_name", "district", "exact_address", "street_address"):
                if not facts.get(key) and candidate.get("location_label"):
                    facts[key] = candidate.get("location_label")
            return _property_candidate_matches_requested_location(
                location_hints=location_hints,
                property_url=str(candidate.get("property_url") or candidate.get("source_url") or "").strip(),
                title=str(candidate.get("title") or "").strip(),
                summary=str(candidate.get("summary") or candidate.get("fit_summary") or "").strip(),
                property_facts=facts,
                country_code=hard_scope_country,
                region_code=hard_scope_region,
            )

        merged_ranked: list[dict[str, object]] = []
        seen_refs: set[str] = set()
        merged_input = [*(("current", row) for row in current_ranked), *(("saved", row) for row in saved_shortlist_candidates)]
        for origin, candidate in merged_input:
            candidate_row = dict(candidate)
            if origin == "saved" and not _saved_shortlist_in_scope(candidate_row):
                continue
            candidate_ref = _saved_shortlist_ref(candidate_row)
            if candidate_ref and candidate_ref in seen_refs:
                continue
            if candidate_ref:
                seen_refs.add(candidate_ref)
                candidate_row["property_ref"] = candidate_ref
            merged_ranked.append(candidate_row)
        for index, candidate_row in enumerate(merged_ranked, start=1):
            candidate_row["rank"] = index
        if merged_ranked:
            property_summary["ranked_candidates"] = merged_ranked
            property_run["summary"] = property_summary
    property_country_label = str(property_state.get("country_label") or "Market")
    property_language_label = str(property_state.get("language_label") or "Deutsch")
    property_listing_mode_label = str(property_state.get("listing_mode_label") or "Rent")
    property_search_goal_label = str(property_state.get("search_goal_label") or "Find a home")
    property_investment_strategy_label = str(property_state.get("investment_strategy_label") or "Best overall opportunity")
    property_investment_research_mode_label = str(property_state.get("investment_research_mode_label") or "Off")
    property_type_label = str(property_state.get("property_type_label") or "Any type")
    property_provider_total_for_country = int(property_state.get("provider_total_for_country") or 0)
    selected_listing_mode = str(property_preferences.get("listing_mode") or "rent").strip().lower() or "rent"
    try:
        property_available_within_years_value = max(
            0,
            min(10, int(float(str(property_preferences.get("available_within_years") or "").strip()))),
        )
    except Exception:
        property_available_within_years_value = 0
    selected_region_code = str(property_preferences.get("region_code") or "").strip().lower()
    selected_full_region_scope = bool(property_preferences.get("full_region_scope"))
    country_options = [dict(option) for option in list(property_state.get("country_options") or []) if isinstance(option, dict)]
    language_options = [dict(option) for option in list(property_state.get("language_options") or []) if isinstance(option, dict)]
    listing_mode_options = [dict(option) for option in list(property_state.get("listing_mode_options") or []) if isinstance(option, dict)]
    search_goal_options = [dict(option) for option in list(property_state.get("search_goal_options") or []) if isinstance(option, dict)]
    investment_strategy_options = [dict(option) for option in list(property_state.get("investment_strategy_options") or []) if isinstance(option, dict)]
    investment_research_mode_options = [dict(option) for option in list(property_state.get("investment_research_mode_options") or []) if isinstance(option, dict)]
    property_type_options = [
        dict(option)
        for option in list(property_state.get("property_type_options") or [])
        if isinstance(option, dict) and str(option.get("value") or "").strip().lower() != "any"
    ]
    selected_platforms = {
        str(value or "").strip()
        for value in (property_state.get("selected_platforms") or [])
        if str(value or "").strip()
    }
    selected_property_type_values = _normalize_property_type_values(property_preferences.get("property_type"))
    search_form_state = build_property_search_form_state_snapshot(
        property_preferences,
        selected_listing_mode=selected_listing_mode,
    )
    selected_country_code = str(search_form_state.get("selected_country_code") or "AT").strip().upper() or "AT"
    selected_currency_code = currency_code_for_country(selected_country_code) or "EUR"
    selected_search_goal = str(search_form_state.get("selected_search_goal") or "home").strip().lower() or "home"
    selected_investment_strategy = str(search_form_state.get("selected_investment_strategy") or "best_overall").strip().lower() or "best_overall"
    selected_investment_research_mode = str(search_form_state.get("selected_investment_research_mode") or "off").strip().lower() or "off"
    property_is_investment_search = bool(search_form_state.get("property_is_investment_search"))
    selected_school_stage_preferences = [
        str(item or "").strip()
        for item in list(search_form_state.get("selected_school_stage_preferences") or [])
        if str(item or "").strip()
    ]
    school_evidence_controls_enabled = bool(search_form_state.get("school_evidence_controls_enabled"))
    selected_listing_mode = str(search_form_state.get("selected_listing_mode") or selected_listing_mode or "rent").strip().lower() or "rent"
    property_listing_mode_label = property_mode_visibility_label(
        {
            **property_preferences,
            "search_goal": selected_search_goal,
            "listing_mode": selected_listing_mode,
        },
        fallback=selected_listing_mode,
    )
    show_investment_underwriting_controls = bool(search_form_state.get("show_investment_underwriting_controls"))
    show_lifestyle_research_controls = bool(search_form_state.get("show_lifestyle_research_controls"))
    show_community_validation_controls = bool(search_form_state.get("show_community_validation_controls"))
    show_developer_project_stage_controls = bool(search_form_state.get("show_developer_project_stage_controls"))
    show_public_housing_policy_controls = bool(search_form_state.get("show_public_housing_policy_controls"))
    show_distressed_review_controls = bool(search_form_state.get("show_distressed_review_controls"))
    show_search_agent_detail_controls = bool(search_form_state.get("show_search_agent_detail_controls"))
    show_preference_profile_controls = bool(search_form_state.get("show_preference_profile_controls"))
    show_school_evidence_priority_controls = bool(search_form_state.get("show_school_evidence_priority_controls"))
    show_playground_importance_controls = bool(search_form_state.get("show_playground_importance_controls"))
    show_library_importance_controls = bool(search_form_state.get("show_library_importance_controls"))
    show_supermarket_importance_controls = bool(search_form_state.get("show_supermarket_importance_controls"))
    min_gross_yield_pct = int(search_form_state.get("min_gross_yield_pct") or 0)
    equity_available_eur = int(search_form_state.get("equity_available_eur") or 0)
    loan_term_years = int(search_form_state.get("loan_term_years") or 25)
    max_interest_rate_pct = int(search_form_state.get("max_interest_rate_pct") or 0)
    min_dscr = float(search_form_state.get("min_dscr") or 0.0)
    vacancy_reserve_pct = int(search_form_state.get("vacancy_reserve_pct") or 4)
    capex_reserve_pct = int(search_form_state.get("capex_reserve_pct") or 6)
    platform_options = [
        dict(option)
        for option in list(property_state.get("platform_options") or [])
        if isinstance(option, dict)
    ]
    evidence_source_rows = [
        dict(option)
        for option in list(property_state.get("evidence_source_rows") or [])
        if isinstance(option, dict)
    ]
    try:
        from app.services.property_market_catalog import provider_options as property_provider_options
        from app.services.property_market_catalog import filter_selectable_property_platforms as property_filter_selectable_property_platforms

        known_values = {
            str(option.get("value") or "").strip().lower()
            for option in platform_options
            if str(option.get("value") or "").strip()
        }
        for option in property_provider_options(country_code=selected_country_code):
            value = str(option.get("value") or "").strip()
            if not value or value.lower() in known_values:
                continue
            platform_options.append(dict(option))
            known_values.add(value.lower())
    except Exception:
        pass
    available_platform_values = {
        str(option.get("value") or "").strip()
        for option in platform_options
        if str(option.get("value") or "").strip() and bool(option.get("search_ready", True))
    }
    selectable_platform_values = set(
        property_filter_selectable_property_platforms(
            tuple(available_platform_values),
            country_code=selected_country_code,
            listing_mode=selected_listing_mode,
            include_distressed_sale_signals=property_preferences.get("include_distressed_sale_signals"),
        )[0]
    )
    if selectable_platform_values:
        platform_options = [
            dict(option)
            for option in platform_options
            if str(option.get("value") or "").strip() in selectable_platform_values
        ]
        available_platform_values = {
            str(option.get("value") or "").strip()
            for option in platform_options
            if str(option.get("value") or "").strip() and bool(option.get("search_ready", True))
        }
    selected_platforms = {
        value
        for value in selected_platforms
        if value in available_platform_values
    }
    if not evidence_source_rows:
        try:
            from app.services.property_market_catalog import evidence_source_options as property_evidence_source_options

            evidence_source_rows = [
                dict(option)
                for option in property_evidence_source_options(country_code=selected_country_code)
                if isinstance(option, dict)
            ]
        except Exception:
            evidence_source_rows = []
    raw_selected_location_values = property_preferences.get("selected_location_values")
    if isinstance(raw_selected_location_values, (list, tuple, set)):
        selected_location_values = [
            str(item or "").strip()
            for item in raw_selected_location_values
            if str(item or "").strip()
        ]
    else:
        selected_location_values = _csv_values(property_preferences.get("location_query"))
    selected_keyword_values = _csv_values(property_preferences.get("keywords"))
    selected_avoid_keyword_values = _csv_values(property_preferences.get("avoid_keywords"))
    raw_keyword_preference_map: dict[str, str] = {}
    if isinstance(property_preferences.get("keyword_preferences"), dict):
        raw_keyword_preference_map.update(
            {
                str(key or "").strip().lower(): str(value or "").strip().lower()
                for key, value in dict(property_preferences.get("keyword_preferences") or {}).items()
                if str(key or "").strip() and str(value or "").strip()
            }
        )
    raw_keyword_preference_json = str(property_preferences.get("keyword_preferences_json") or "").strip()
    if raw_keyword_preference_json:
        try:
            parsed_keyword_preference_json = json.loads(raw_keyword_preference_json)
        except Exception:
            parsed_keyword_preference_json = {}
        if isinstance(parsed_keyword_preference_json, dict):
            raw_keyword_preference_map.update(
                {
                    str(key or "").strip().lower(): str(value or "").strip().lower()
                    for key, value in dict(parsed_keyword_preference_json or {}).items()
                    if str(key or "").strip() and str(value or "").strip()
                }
            )
    region_options = _property_region_options(str(property_preferences.get("country_code") or "AT"))
    if not selected_region_code and region_options:
        selected_region_code = str(region_options[0].get("value") or "").strip().lower()
    selected_region_label = selected_region_code.replace("_", " ").title() if selected_region_code else "area"
    if selected_region_code and not selected_location_values:
        try:
            from app.services.property_market_catalog import region_label_for_country_region
            selected_region_label = region_label_for_country_region(
                str(property_preferences.get("country_code") or "AT"),
                selected_region_code,
            )
        except Exception:
            selected_region_label = selected_region_code.replace("_", " ").title()
        if str(property_preferences.get("location_query") or "").strip().lower() == selected_region_label.strip().lower():
            selected_full_region_scope = True
    location_options = _property_location_options(
        str(property_preferences.get("country_code") or "AT"),
        selected_region_code,
    )
    selected_language_code = str(property_preferences.get("language_code") or "de").strip().lower() or "de"
    keyword_options = []
    for option in _property_keyword_options():
        localized_option = dict(option)
        localized_option["label"] = _localized_property_ui_text(
            localized_option.get("label_i18n"),
            selected_language_code,
            localized_option.get("label"),
        )
        localized_option["detail"] = _localized_property_ui_text(
            localized_option.get("detail_i18n"),
            selected_language_code,
            localized_option.get("detail"),
        )
        has_explicit_tooltip = bool(localized_option.get("tooltip_i18n")) or bool(
            str(localized_option.get("tooltip") or "").strip()
        )
        localized_tooltip = _localized_property_ui_text(
            localized_option.get("tooltip_i18n"),
            selected_language_code,
            localized_option.get("tooltip") or "",
        )
        if has_explicit_tooltip and localized_tooltip:
            localized_option["tooltip"] = localized_tooltip
        keyword_options.append(localized_option)
    school_preference_options = _property_school_preference_options(
        selected_school_stage_preferences=selected_school_stage_preferences,
        require_school_evidence=bool(property_preferences.get("require_school_evidence")),
        school_evidence_priority=str(property_preferences.get("school_evidence_priority") or "any"),
        property_preferences=property_preferences,
    )
    selected_location_values, custom_location_values = _split_known_and_custom_values(location_options, selected_location_values)
    selected_keyword_values, custom_keyword_values = _split_known_and_custom_values(keyword_options, selected_keyword_values)
    show_land_keywords = _property_type_selection_allows_land(selected_property_type_values)
    land_only_search = _property_type_selection_is_land_only(selected_property_type_values)
    dwelling_only_keywords = {
        "lift",
        "barrier-free",
        "balcony",
        "terrace",
        "klimaanlage",
        "dachgeschosswohnung",
        "no gas",
        "district heating",
        "bright",
    }
    keyword_preference_options: list[dict[str, object]] = []
    nearby_keyword_distance_fields = {
        "playground nearby": "max_distance_to_playground_m",
        "library nearby": "max_distance_to_library_m",
        "zoo nearby": "max_distance_to_zoo_m",
        "public pool nearby": "max_distance_to_public_pool_m",
        "medical care nearby": "max_distance_to_medical_care_m",
        "supermarket nearby": "max_distance_to_supermarket_m",
        "market nearby": "max_distance_to_market_m",
        "Baumarkt nearby": "max_distance_to_hardware_store_m",
        "shopping center nearby": "max_distance_to_shopping_center_m",
        "flaniermeile nearby": "max_distance_to_shopping_street_m",
        "theatre nearby": "max_distance_to_theatre_m",
        "pharmacy nearby": "max_distance_to_medical_care_m",
        "underground nearby": "max_distance_to_subway_m",
    }
    nearby_keyword_importance_fields = {
        "playground nearby": "max_distance_to_playground_importance",
        "library nearby": "max_distance_to_library_importance",
        "zoo nearby": "max_distance_to_zoo_importance",
        "public pool nearby": "max_distance_to_public_pool_importance",
        "medical care nearby": "max_distance_to_medical_care_importance",
        "supermarket nearby": "max_distance_to_supermarket_importance",
        "market nearby": "max_distance_to_market_importance",
        "Baumarkt nearby": "max_distance_to_hardware_store_importance",
        "shopping center nearby": "max_distance_to_shopping_center_importance",
        "flaniermeile nearby": "max_distance_to_shopping_street_importance",
        "theatre nearby": "max_distance_to_theatre_importance",
        "pharmacy nearby": "max_distance_to_medical_care_importance",
        "underground nearby": "max_distance_to_subway_importance",
    }
    for option in keyword_options:
        option_value = str(option.get("value") or "").strip()
        state = str(raw_keyword_preference_map.get(option_value.lower()) or "").strip().lower()
        distance_state = "500"
        if option_value == "playground nearby":
            if state not in {"avoid", "nice_to_have", "important", "must_have"}:
                playground_distance = property_preferences.get("max_distance_to_playground_m")
                playground_importance = str(property_preferences.get("max_distance_to_playground_importance") or "").strip().lower()
                try:
                    playground_distance = int(float(playground_distance)) if playground_distance not in (None, "") else 0
                except Exception:
                    playground_distance = 0
                if option_value in selected_avoid_keyword_values:
                    state = "avoid"
                elif playground_importance in {"must_have", "important", "nice_to_have"}:
                    state = playground_importance
                else:
                    state = "any"
        elif option_value == "underground nearby":
            if state not in {"avoid", "nice_to_have", "important", "must_have"}:
                subway_distance = property_preferences.get("max_distance_to_subway_m")
                try:
                    subway_distance = int(float(subway_distance)) if subway_distance not in (None, "") else 0
                except Exception:
                    subway_distance = 0
                if option_value in selected_avoid_keyword_values:
                    state = "avoid"
                elif subway_distance:
                    if subway_distance <= 250:
                        state = "must_have"
                    elif subway_distance <= 500:
                        state = "important"
                    else:
                        state = "nice_to_have"
                else:
                    state = "any"
        elif option_value in {"library nearby", "zoo nearby", "public pool nearby", "medical care nearby", "supermarket nearby", "market nearby", "Baumarkt nearby", "shopping center nearby", "flaniermeile nearby", "theatre nearby", "pharmacy nearby"}:
            if state not in {"avoid", "nice_to_have", "important", "must_have"}:
                distance_field = nearby_keyword_distance_fields.get(option_value) or ""
                importance_field = nearby_keyword_importance_fields.get(option_value) or ""
                stored_distance = property_preferences.get(distance_field) if distance_field else None
                try:
                    stored_distance = int(float(stored_distance)) if stored_distance not in (None, "") else 0
                except Exception:
                    stored_distance = 0
                stored_importance = str(property_preferences.get(importance_field) or "").strip().lower() if importance_field else ""
                if option_value in selected_avoid_keyword_values:
                    state = "avoid"
                elif stored_importance in {"must_have", "important", "nice_to_have"}:
                    state = stored_importance
                elif stored_distance:
                    if stored_distance <= 250:
                        state = "must_have"
                    elif stored_distance <= 500:
                        state = "important"
                    else:
                        state = "nice_to_have"
                else:
                    state = "any"
        elif option_value == "good air quality":
            if state not in {"nice_to_have", "important", "must_have"}:
                state = "important" if bool(property_preferences.get("prefer_good_air_quality")) else "any"
        elif option_value == "klimaerwaermungsfit":
            if state not in {"nice_to_have", "important", "must_have"}:
                state = "important" if bool(property_preferences.get("prefer_heat_resilient_home")) else "any"
        elif option_value == "klimaanlage":
            if state not in {"nice_to_have", "important", "must_have"}:
                state = "important" if bool(property_preferences.get("prefer_air_conditioning")) else "any"
        elif option_value == "dachgeschosswohnung":
            if state not in {"avoid", "nice_to_have", "important", "must_have"}:
                if bool(property_preferences.get("avoid_attic_apartment")):
                    state = "avoid"
                elif bool(property_preferences.get("prefer_attic_apartment")):
                    state = "important"
                else:
                    state = "any"
        elif option_value == "avoid noise-risk area":
            if state not in {"avoid", "must_have"}:
                state = "avoid" if bool(property_preferences.get("avoid_noise_risk_area")) else "any"
        elif option_value == "high-speed internet":
            if state not in {"nice_to_have", "important", "must_have"}:
                state = "must_have" if bool(property_preferences.get("require_high_speed_internet")) else "any"
        elif option_value == "low crime area":
            if state not in {"nice_to_have", "important", "must_have"}:
                state = "important" if bool(property_preferences.get("prefer_low_crime_area")) else "any"
        elif option_value == "water and groundwater check":
            if state not in {"important", "must_have"}:
                state = "important" if bool(property_preferences.get("require_drinking_water_quality_research")) else "any"
        elif option_value == "parking pressure check":
            if state == "important":
                state = "medium"
            elif state == "must_have":
                state = "high"
            elif state not in {"low", "medium", "high"}:
                stored_pressure = str(
                    property_preferences.get("parking_pressure_preference")
                    or property_preferences.get("parking_pressure_tolerance")
                    or ""
                ).strip().lower()
                if stored_pressure in {"low", "medium", "high"}:
                    state = stored_pressure
                else:
                    state = "medium" if bool(property_preferences.get("require_parking_pressure_check")) else "any"
        elif option_value == "avoid septic risk":
            if state not in {"avoid", "must_have"}:
                state = "avoid" if bool(property_preferences.get("avoid_cesspit_or_septic_risk")) else "any"
        elif option_value == "winter driving check":
            if state not in {"important", "must_have"}:
                state = "important" if bool(property_preferences.get("require_winter_access_research")) else "any"
        elif option_value == "avoid flood-risk area":
            if state not in {"avoid", "must_have"}:
                state = "avoid" if bool(property_preferences.get("avoid_flood_risk_area")) else "any"
        elif option_value == "barrier-free":
            if state not in {"avoid", "nice_to_have", "important", "must_have"}:
                if option_value in selected_avoid_keyword_values:
                    state = "avoid"
                elif bool(property_preferences.get("require_barrier_free")):
                    state = "must_have"
                else:
                    state = "any"
        elif state not in {"avoid", "nice_to_have", "important", "must_have"}:
            if option_value in selected_avoid_keyword_values:
                state = "avoid"
            else:
                state = "any"
        if option_value in nearby_keyword_distance_fields:
            stored_distance = property_preferences.get(nearby_keyword_distance_fields[option_value])
            try:
                stored_distance = int(float(stored_distance)) if stored_distance not in (None, "") else 0
            except Exception:
                stored_distance = 0
            if stored_distance in {100, 250, 500, 1000, 2000, 5000, 7000}:
                distance_state = str(stored_distance)
        keyword_preference_options.append(
            {
                **option,
                "state": state,
                "distance_state": distance_state,
                "hidden": (option_value == "baugrund" and not show_land_keywords) or (option_value in dwelling_only_keywords and land_only_search),
            }
        )
    custom_location_query = str(property_preferences.get("custom_location_query") or ", ".join(custom_location_values)).strip()
    custom_keywords = str(property_preferences.get("custom_keywords") or ", ".join(custom_keyword_values)).strip()
    adjacent_area_radius_unit = str(property_preferences.get("adjacent_area_radius_unit") or "m").strip().lower()
    if adjacent_area_radius_unit not in {"m", "km"}:
        adjacent_area_radius_unit = "m"
    try:
        adjacent_area_radius_value = float(property_preferences.get("adjacent_area_radius_value"))
    except Exception:
        try:
            stored_adjacent_area_radius_m = float(property_preferences.get("adjacent_area_radius_m") or 0.0)
        except Exception:
            stored_adjacent_area_radius_m = 0.0
        adjacent_area_radius_value = stored_adjacent_area_radius_m / 1000.0 if adjacent_area_radius_unit == "km" else stored_adjacent_area_radius_m
    if adjacent_area_radius_unit == "km":
        adjacent_area_radius_value = max(0.0, min(adjacent_area_radius_value, 1000.0))
        adjacent_area_radius_step = 1
    else:
        adjacent_area_radius_value = max(0.0, min(adjacent_area_radius_value, 1000.0))
        adjacent_area_radius_step = 25
    property_selected_platform_labels = [
        str(option.get("label") or option.get("value") or "").strip()
        for option in platform_options
        if str(option.get("value") or "").strip() in selected_platforms
    ]
    property_market_summary_items = build_property_market_summary_items(
        row_item=row_item,
        currency_code=selected_currency_code,
        property_country_label=property_country_label,
        property_language_label=property_language_label,
        property_search_goal_label=property_search_goal_label,
        property_type_label=property_type_label,
        property_listing_mode_label=property_listing_mode_label,
        property_is_investment_search=property_is_investment_search,
        show_investment_underwriting_controls=show_investment_underwriting_controls,
        property_investment_strategy_label=property_investment_strategy_label,
        min_gross_yield_pct=min_gross_yield_pct,
        equity_available_eur=equity_available_eur,
        min_dscr=min_dscr,
        property_investment_research_mode_label=property_investment_research_mode_label,
        property_available_within_years_value=property_available_within_years_value,
        property_preferences=property_preferences,
        custom_keywords=custom_keywords,
        show_lifestyle_research_controls=show_lifestyle_research_controls,
        show_developer_project_stage_controls=show_developer_project_stage_controls,
        show_public_housing_policy_controls=show_public_housing_policy_controls,
        show_distressed_review_controls=show_distressed_review_controls,
    )
    property_platform_rows = [
        row_item(
            str(option.get("label") or option.get("value") or "Provider"),
            "Included in this search." if str(option.get("value") or "").strip() in selected_platforms else "Available to add to this search.",
            "Selected" if str(option.get("value") or "").strip() in selected_platforms else "Available",
        )
        for option in platform_options
    ]
    property_recent_matches = [
        dict(item)
        for item in list(property_state.get("recent_matches") or [])
        if isinstance(item, dict)
    ] if surface_scope.wants_recent_matches else []
    property_event_rows = [
        row_item(
            str(event.get("step") or "Update").replace("_", " ").capitalize(),
            str(event.get("message") or "No message").strip(),
            str(event.get("status") or "queued").replace("_", " "),
        )
        for event in list(property_run.get("events") or [])[-6:]
        if isinstance(event, dict)
    ]
    active_run_id = str(property_run.get("run_id") or "").strip()

    def _packet_url_for_candidate(candidate: dict[str, object], *, source_label: str) -> str:
        candidate_for_ref = dict(candidate)
        candidate_for_ref.setdefault("source_label", source_label)
        packet_ref = _property_candidate_ref(candidate_for_ref)
        packet_url = f"/app/research/{packet_ref}"
        if active_run_id:
            packet_url = f"{packet_url}?run_id={active_run_id}"
        return packet_url

    enriched_sources: list[dict[str, object]] = []
    def _candidate_priority_reason(match_reasons: list[str], mismatch_reasons: list[str], fit_summary: str) -> str:
        def _is_tour_only(text: str) -> bool:
            lowered = str(text or "").strip().lower()
            return bool(lowered) and any(marker in lowered for marker in ("360", "panorama", "virtual tour", "remote review"))

        preferred_match = next((item for item in match_reasons if item and not _is_tour_only(item)), "")
        if preferred_match:
            return f"Strong fit because: {preferred_match}"
        preferred_risk = next((item for item in mismatch_reasons if item and not _is_tour_only(item)), "")
        if preferred_risk:
            return f"Watch-out first: {preferred_risk}"
        if fit_summary and not _is_tour_only(fit_summary):
            return fit_summary
        if match_reasons:
            return "Strong fit because it stayed closest to the brief on the available facts; the tour helped visual review but did not decide the result on its own."
        return ""

    if surface_scope.wants_run_views:
        for source in list(property_summary.get("sources") or []):
            if not isinstance(source, dict):
                continue
            source_row = dict(source)
            source_label = str(source_row.get("source_label") or source_row.get("source_url") or "Source").strip()
            source_row["display_source_label"] = _compact_provider_label(source_label)
            enriched_candidates: list[dict[str, object]] = []
            for candidate in list(source_row.get("top_candidates") or []):
                if not isinstance(candidate, dict):
                    continue
                candidate_row = dict(candidate)
                candidate_row.setdefault("source_label", source_label)
                candidate_row.setdefault("source_short_label", _compact_provider_label(source_label))
                if not str(candidate_row.get("packet_url") or "").strip():
                    candidate_row["packet_url"] = _packet_url_for_candidate(candidate_row, source_label=source_label)
                enriched_candidates.append(candidate_row)
            source_row["top_candidates"] = enriched_candidates
            enriched_sources.append(source_row)
        if enriched_sources:
            property_summary["sources"] = enriched_sources
            ranked_candidates = [
                dict(row)
                for row in list(property_summary.get("ranked_candidates") or [])
                if isinstance(row, dict)
            ]
            if not ranked_candidates:
                seen_candidates: set[str] = set()
                for source_row in enriched_sources:
                    source_label = str(source_row.get("source_label") or source_row.get("source_url") or "Source").strip()
                    for candidate in list(source_row.get("top_candidates") or []):
                        if not isinstance(candidate, dict):
                            continue
                        candidate_row = dict(candidate)
                        if not _property_candidate_is_rankable(candidate_row):
                            continue
                        candidate_key = str(candidate_row.get("source_ref") or candidate_row.get("property_url") or candidate_row.get("listing_id") or "").strip()
                        if candidate_key and candidate_key in seen_candidates:
                            continue
                        if candidate_key:
                            seen_candidates.add(candidate_key)
                        candidate_row.setdefault("source_label", source_label)
                        candidate_row.setdefault("source_short_label", _compact_provider_label(source_label))
                        ranked_candidates.append(candidate_row)
            ranked_candidates.sort(key=lambda item: float(item.get("fit_score") or 0.0), reverse=True)
            for index, candidate_row in enumerate(ranked_candidates, start=1):
                candidate_row["rank"] = index
                candidate_row.setdefault("map_url", _property_candidate_maps_url(candidate_row))
                candidate_row.setdefault("preview_image_url", _property_candidate_preview_image(candidate_row))
                candidate_row.setdefault("route_evidence", _property_candidate_route_evidence(candidate_row, property_preferences))
                if not str(candidate_row.get("packet_url") or "").strip():
                    candidate_row["packet_url"] = _packet_url_for_candidate(
                    candidate_row,
                    source_label=str(candidate_row.get("source_label") or "Source"),
                )
            property_summary["ranked_candidates"] = ranked_candidates
            property_run["summary"] = property_summary

    property_source_rows = build_property_source_rows(property_summary=property_summary)
    property_shortlist_rows, property_shortlist_cards = build_property_shortlist_panel(
        property_summary=property_summary,
        property_preferences=property_preferences,
        active_run_id=active_run_id,
        wants_run_views=surface_scope.wants_run_views,
        clean_candidate_copy=_clean_property_candidate_copy,
        candidate_priority_reason=_candidate_priority_reason,
        property_candidate_ref=_property_candidate_ref,
    )
    property_learning_summary = dict(property_state.get("learning_summary") or {})
    property_learning_rows = [
        row_item(entry, "Learned positive preference from explicit filters or listing feedback.", "Learnt")
        for entry in list(property_learning_summary.get("likes") or [])[:4]
        if str(entry or "").strip()
    ]
    property_learning_rows.extend(
        row_item(entry, "Negative preference that should suppress future shortlist candidates.", "Avoid")
        for entry in list(property_learning_summary.get("dislikes") or [])[:4]
        if str(entry or "").strip()
    )
    property_learning_rows.extend(
        row_item(entry, "Hard rule that should fail or demote mismatching listings.", "Rule")
        for entry in list(property_learning_summary.get("hard_rules") or [])[:3]
        if str(entry or "").strip()
    )
    property_recent_feedback_rows = [
        row_item(
            str(entry.get("reaction") or "feedback").strip().title(),
            " | ".join(
                part
                for part in (
                    ", ".join(str(item or "").strip() for item in list(entry.get("reasons") or [])[:3] if str(item or "").strip()),
                    str(entry.get("note") or "").strip(),
                    str(entry.get("recorded_at") or "").strip()[:10],
                )
                if part
            )
            or "Structured feedback recorded.",
            "Feedback",
        )
        for entry in list(property_learning_summary.get("recent_feedback") or [])[:4]
        if isinstance(entry, dict)
    ]
    property_plan_catalog = [
        dict(plan)
        for plan in list(property_state.get("commercial", {}).get("plan_catalog") or [])
        if isinstance(plan, dict)
    ]
    def _positive_int(value: object, *, default: int = 0) -> int:
        try:
            parsed = int(float(str(value or "").strip()))
        except Exception:
            return default
        return max(0, parsed)

    def _currency_short(value: int) -> str:
        if value >= 1_000_000:
            return f"{selected_currency_code} {value // 1_000_000}M"
        if value >= 1_000:
            return f"{selected_currency_code} {value // 1_000}k"
        return f"{selected_currency_code} {value}"

    property_price_value = _positive_int(property_preferences.get("max_price_eur"))
    property_price_range_presets = {
        "rent": {"max": 6000, "step": 100, "scaleMaxLabel": _currency_short(6000)},
        "buy": {"max": 2_000_000, "step": 25_000, "scaleMaxLabel": _currency_short(2_000_000)},
        "any": {"max": 2_000_000, "step": 25_000, "scaleMaxLabel": _currency_short(2_000_000)},
    }
    property_price_preset = property_price_range_presets.get(selected_listing_mode) or property_price_range_presets["rent"]
    property_price_slider_max = max(int(property_price_preset["max"]), property_price_value)
    property_price_slider_step = int(property_price_preset["step"])
    property_min_rooms_value = min(8, _positive_int(property_preferences.get("min_rooms")))
    property_min_area_value = min(250, _positive_int(property_preferences.get("min_area_m2")))
    property_available_within_years_value = min(10, _positive_int(property_preferences.get("available_within_years")))
    market_filter_capabilities = _property_market_filter_capabilities(
        str(property_preferences.get("country_code") or "AT"),
        selected_region_code,
    )
    property_search_agent_enabled = bool(property_preferences.get("search_agent_enabled"))
    property_search_agent_duration_days = _positive_int(property_preferences.get("search_agent_duration_days"), default=30)
    property_search_agent_duration_days = max(7, min(365, property_search_agent_duration_days or 30))
    property_search_agent_notification_limit = _positive_int(property_preferences.get("search_agent_notification_limit"), default=5)
    property_search_agent_notification_limit = max(1, min(50, property_search_agent_notification_limit or 5))
    property_search_agent_notification_period = str(property_preferences.get("search_agent_notification_period") or "day").strip().lower()
    if property_search_agent_notification_period not in {"day", "week"}:
        property_search_agent_notification_period = "day"
    property_search_mode_requested = str(property_preferences.get("search_mode") or "strict").strip().lower()
    if property_search_mode_requested not in {"strict", "discovery"}:
        property_search_mode_requested = "strict"
    if surface_scope.wants_agent_views or (
        surface_scope.section == "search" and str(property_state.get("selected_agent_id") or "").strip()
    ):
        search_agent_scope_preview_builder = (
            _property_scope_preview_map_only
            if surface_scope.section == "agents"
            else _property_scope_preview
        )
        property_search_agents, property_search_agent = build_property_search_agents(
            property_preferences,
            selected_platforms=selected_platforms,
            selected_listing_mode=selected_listing_mode,
            search_mode_requested=property_search_mode_requested,
            default_duration_days=property_search_agent_duration_days,
            default_notification_limit=property_search_agent_notification_limit,
            default_notification_period=property_search_agent_notification_period,
            normalize_property_type_values=_normalize_property_type_values,
            scope_preview_builder=search_agent_scope_preview_builder,
        )
    else:
        property_search_agents, property_search_agent = [], {}
    property_search_mode = property_search_mode_requested
    property_run_for_defaults = dict(property_state.get("run") or {})
    property_run_summary_for_defaults = dict(property_run_for_defaults.get("summary") or {})
    property_run_status_for_defaults = str(property_run_for_defaults.get("status") or "").strip().lower()
    property_ranked_total_for_defaults = _positive_int(
        property_run_summary_for_defaults.get("ranked_total"),
        default=len(
            [
                row
                for row in list(property_run_summary_for_defaults.get("ranked_candidates") or [])
                if isinstance(row, dict)
            ]
        ),
    )
    if property_search_mode == "strict" and property_run_status_for_defaults in {"processed", "completed"} and property_ranked_total_for_defaults < 6:
        property_search_mode = "discovery"
    country_codes = tuple(
        str(option.get("value") or "").strip()
        for option in country_options
        if str(option.get("value") or "").strip()
    )
    region_catalog_by_country = _property_region_catalog_by_country(country_codes)
    market_filter_capabilities_by_country_region = _property_market_filter_capabilities_catalog(country_codes)
    location_catalog_by_country_region = _property_location_catalog_by_country_region(country_codes)
    property_form = {
        "variant": "property_search",
        "title": "Start a premium market search",
        "eyebrow": "Property search",
        "copy": "Set the market, shape the shortlist, choose the listing sites, then launch one visible search with ranking, property pages, and client-ready alerts.",
        "submit_label": "Launch search",
        "fields": [
            {
                "type": "select",
                "name": "search_goal",
                "label": "What are you looking for?",
                "value": selected_search_goal,
                "options": search_goal_options,
                "tooltip": "Choose Find a home for lifestyle fit, or Find an investment for yield, value, risk, and execution ranking.",
                "step": "search",
            },
            {
                "type": "select",
                "name": "country_code",
                "label": "Country",
                "value": str(property_preferences.get("country_code") or "AT"),
                "options": country_options,
                "step": "search",
            },
            {
                "type": "select",
                "name": "listing_mode",
                "label": "Search mode",
                "value": selected_listing_mode,
                "options": listing_mode_options,
                "tooltip": "Home searches can look at rent or buy. Investment searches use buy mode automatically.",
                "step": "search",
                "hidden": property_is_investment_search,
            },
            {
                "type": "checkbox_group",
                "name": "property_type",
                "label": "Property type",
                "values": selected_property_type_values,
                "options": property_type_options,
                "step": "what",
            },
            {
                "type": "select",
                "name": "investment_research_mode",
                "label": "Investment research",
                "value": str(property_preferences.get("investment_research_mode") or "off"),
                "options": investment_research_mode_options,
                "hidden": not property_is_investment_search,
                "tooltip": "Choose whether the investment sweep should stay ranking-only or add yield, pricing, and risk context before the full property page.",
                "step": "search",
            },
            {
                "type": "select",
                "name": "investment_strategy",
                "label": "Investment strategy",
                "value": selected_investment_strategy,
                "options": investment_strategy_options,
                "hidden": not show_investment_underwriting_controls,
                "tooltip": "Choose the thesis first. Cash flow weights yield highest. Appreciation weights area pricing and upside. Low risk penalizes unclear or messy deals.",
                "step": "search",
            },
            {
                "type": "range",
                "name": "min_gross_yield_pct",
                "label": "Minimum gross yield",
                "value": str(min_gross_yield_pct),
                "min": "0",
                "max": "15",
                "visual_max": "15",
                "range_step": "1",
                "format": "percent_cap",
                "empty_label": "Any yield",
                "scale_min_label": "Any",
                "scale_max_label": "15%",
                "hidden": not show_investment_underwriting_controls,
                "tooltip": "Use this as a hard floor for expected gross yield when enough rent evidence exists. Unknown yields stay visible but rank lower.",
                "step": "search",
            },
            {
                "type": "range",
                "name": "equity_available_eur",
                "label": "Equity available",
                "value": str(equity_available_eur),
                "min": "0",
                "max": "1000000",
                "visual_max": "1000000",
                "range_step": "25000",
                "format": "currency_eur",
                "currency_code": selected_currency_code,
                "empty_label": "Model leverage automatically",
                "scale_min_label": "Auto",
                "scale_max_label": _currency_short(1_000_000),
                "hidden": not show_investment_underwriting_controls,
                "tooltip": "Use this when you want debt coverage and cash-on-cash yield to reflect your real equity instead of the default leverage assumption.",
                "step": "search",
            },
            {
                "type": "range",
                "name": "loan_term_years",
                "label": "Loan term",
                "value": str(loan_term_years),
                "min": "5",
                "max": "40",
                "visual_max": "40",
                "range_step": "1",
                "format": "loan_term_years",
                "scale_min_label": "5y",
                "scale_max_label": "40y",
                "hidden": not show_investment_underwriting_controls,
                "tooltip": "This drives the modeled annual debt service behind DSCR and cash-on-cash yield.",
                "step": "search",
            },
            {
                "type": "range",
                "name": "max_interest_rate_pct",
                "label": "Rate assumption ceiling",
                "value": str(max_interest_rate_pct),
                "min": "0",
                "max": "12",
                "visual_max": "12",
                "range_step": "1",
                "format": "percent_cap",
                "empty_label": "Live or fallback rate",
                "scale_min_label": "Auto",
                "scale_max_label": "12%",
                "hidden": not show_investment_underwriting_controls,
                "tooltip": "Use this when you want the financing model to stay conservative even if a live feed returns a softer rate.",
                "step": "search",
            },
            {
                "type": "range",
                "name": "min_dscr",
                "label": "Minimum debt coverage",
                "value": str(int(round(min_dscr * 100)) if min_dscr > 0 else 0),
                "min": "0",
                "max": "250",
                "visual_max": "250",
                "range_step": "5",
                "format": "dscr_hundredths",
                "empty_label": "Any DSCR",
                "scale_min_label": "Any",
                "scale_max_label": "2.50x",
                "hidden": not show_investment_underwriting_controls,
                "tooltip": "A DSCR floor lets you exclude deals that do not cover their modeled annual debt service cleanly enough.",
                "step": "search",
            },
            {
                "type": "range",
                "name": "vacancy_reserve_pct",
                "label": "Vacancy reserve",
                "value": str(vacancy_reserve_pct),
                "min": "0",
                "max": "25",
                "visual_max": "25",
                "range_step": "1",
                "format": "percent_cap",
                "empty_label": "Feed or market default",
                "scale_min_label": "Auto",
                "scale_max_label": "25%",
                "hidden": not show_investment_underwriting_controls,
                "tooltip": "This reserve reduces the rent roll before NOI and DSCR are calculated.",
                "step": "search",
            },
            {
                "type": "range",
                "name": "capex_reserve_pct",
                "label": "Capex reserve",
                "value": str(capex_reserve_pct),
                "min": "0",
                "max": "25",
                "visual_max": "25",
                "range_step": "1",
                "format": "percent_cap",
                "empty_label": "Feed or market default",
                "scale_min_label": "Auto",
                "scale_max_label": "25%",
                "hidden": not show_investment_underwriting_controls,
                "tooltip": "This reserve keeps the underwriting honest when the listing looks cheap but long-run upkeep is still unresolved.",
                "step": "search",
            },
            {
                "type": "select",
                "name": "region_code",
                "label": "State or metro area",
                "value": selected_region_code,
                "options": region_options,
                "step": "search",
            },
            {
                "type": "checkbox",
                "name": "full_region_scope",
                "label": f"Use all {selected_region_label}" if selected_region_label else "Use full area",
                "value": "true",
                "checked": selected_full_region_scope,
                "step": "search",
            },
            {
                "type": "checkbox_group",
                "name": "location_query",
                "label": "Target areas",
                "options": location_options,
                "values": selected_location_values,
                "hidden": selected_full_region_scope,
                "step": "search",
            },
            {
                "type": "range",
                "name": "adjacent_area_radius_value",
                "label": "How far outside the selected areas",
                "value": int(adjacent_area_radius_value) if float(adjacent_area_radius_value).is_integer() else round(adjacent_area_radius_value, 1),
                "min": 0,
                "max": 1000,
                "range_step": adjacent_area_radius_step,
                "format": "distance_outside_area",
                "empty_label": "District only",
                "scale_min_label": "0",
                "scale_max_label": f"1000 {adjacent_area_radius_unit}",
                "step": "search",
                "tooltip": "Allow homes just outside the selected districts or areas when they are still nearby.",
                "unit_field": "adjacent_area_radius_unit",
                "meter_step": 25,
                "km_step": 1,
            },
            {
                "type": "select",
                "name": "adjacent_area_radius_unit",
                "label": "Unit",
                "value": adjacent_area_radius_unit,
                "options": [
                    {"value": "m", "label": "Meters"},
                    {"value": "km", "label": "Kilometers"},
                ],
                "step": "search",
            },
            {
                "type": "text",
                "name": "custom_location_query",
                "label": "Add areas manually",
                "value": custom_location_query,
                "placeholder": "Free text for areas not covered by the checklist",
                "tooltip": "Use this only when the district or area is not already available as a visible checkbox.",
                "step": "search",
            },
            {
                "type": "checkbox",
                "name": "investment_require_floorplan",
                "label": "Only keep deals with a floorplan",
                "value": "true",
                "checked": bool(property_preferences.get("investment_require_floorplan") or property_preferences.get("require_floorplan")),
                "tooltip": "Use this for cleaner underwriting. Listings without a layout stay out of the final investment shortlist.",
                "step": "what",
                "hidden": not show_investment_underwriting_controls or land_only_search,
            },
            {
                "type": "checkbox",
                "name": "investment_require_legal_clarity",
                "label": "Exclude legal complexity",
                "value": "true",
                "checked": bool(property_preferences.get("investment_require_legal_clarity")),
                "tooltip": "Exclude auctions, leasehold-style structures, and other legally messy deals when you want a cleaner shortlist first.",
                "step": "what",
                "hidden": not show_investment_underwriting_controls,
            },
            {
                "type": "checkbox",
                "name": "investment_require_tenant_clarity",
                "label": "Exclude unclear tenant status",
                "value": "true",
                "checked": bool(property_preferences.get("investment_require_tenant_clarity")),
                "tooltip": "Penalize or exclude listings that do not make occupancy or rentability clear enough for a fast investment read.",
                "step": "what",
                "hidden": not show_investment_underwriting_controls,
            },
            {
                "type": "checkbox",
                "name": "investment_avoid_major_renovation",
                "label": "Exclude heavy renovation candidates",
                "value": "true",
                "checked": bool(property_preferences.get("investment_avoid_major_renovation")),
                "tooltip": "Exclude listings whose own text suggests major renovation, core refurbishment, or fixer-upper condition.",
                "step": "what",
                "hidden": not show_investment_underwriting_controls,
            },
            {
                "type": "checkbox_group",
                "name": "selected_platforms",
                "label": "Search sources",
                "options": platform_options,
                "option_groups": _group_property_provider_options(platform_options),
                "values": list(selected_platforms),
                "step": "providers",
            },
            {
                "type": "select",
                "name": "search_mode",
                "label": "Result mode",
                "value": property_search_mode,
                "options": [
                    {"value": "strict", "label": "Strict shortlist"},
                    {"value": "discovery", "label": "Discovery pass"},
                ],
                "tooltip": (
                    "Strict shortlist keeps your must-haves. Discovery pass keeps the same area and selected lists, "
                    "but treats school, family, and entertainment distance misses as fit tradeoffs instead of hiding them."
                ),
                "step": "providers",
            },
            {
                "type": "checkbox",
                "name": "use_flatbee_reputation_penalty",
                "label": "Quiet noisy Flatbee results",
                "value": "true",
                "checked": bool(property_preferences.get("use_flatbee_reputation_penalty", True)),
                "tooltip": "Keep Flatbee available in broad searches, but place likely duplicates and low-quality results lower.",
                "step": "providers",
                "advanced_panel": "provider_policies",
            },
            {
                "type": "checkbox",
                "name": "include_broker_direct_sources",
                "label": "Makler-direkt Quellen",
                "value": "true",
                "checked": bool(property_preferences.get("include_broker_direct_sources")),
                "tooltip": "Track Makler-direkt sources such as Kalandra and other broker-owned pages as a distinct source family, separate from marketplaces and cooperatives.",
                "step": "providers",
                "advanced_panel": "provider_policies",
            },
            {
                "type": "checkbox",
                "name": "include_community_signals",
                "label": "Facebook / Telegram Hinweise",
                "value": "true",
                "checked": bool(property_preferences.get("include_community_signals")),
                "tooltip": "Include Facebook groups, Telegram hints, Flatbee-style community leads, and other off-market signals, but keep them separately verifiable.",
                "step": "providers",
                "advanced_panel": "provider_policies",
            },
            {
                "type": "checkbox",
                "name": "require_manual_validation_for_community",
                "label": "Manual validation for Facebook / Telegram leads",
                "value": "true",
                "checked": bool(property_preferences.get("require_manual_validation_for_community")),
                "tooltip": "Community-sourced hits should stay separate until a human confirms identity, freshness, and legitimacy.",
                "step": "providers",
                "advanced_panel": "provider_policies",
                "hidden": not show_community_validation_controls,
            },
            {
                "type": "checkbox",
                "name": "include_developer_project_signals",
                "label": "Developer project signals",
                "value": "true",
                "checked": bool(property_preferences.get("include_developer_project_signals")),
                "tooltip": "Track early-stage project and launch signals from Bauträger and premarket project sites.",
                "step": "providers",
                "advanced_panel": "provider_policies",
            },
            {
                "type": "checkbox",
                "name": "include_public_housing_signals",
                "label": "Public housing signals",
                "value": "true",
                "checked": bool(property_preferences.get("include_public_housing_signals")),
                "tooltip": "Track municipal, public housing, and Wohnservice-like sources separately from commercial marketplaces.",
                "step": "providers",
                "advanced_panel": "provider_policies",
                "hidden": selected_listing_mode != "rent",
            },
            {
                "type": "checkbox",
                "name": "wiener_wohnticket_available",
                "label": "Wiener Wohn-Ticket available",
                "value": "true",
                "checked": bool(property_preferences.get("wiener_wohnticket_available")),
                "tooltip": "Only treat Vienna municipal and subsidized opportunities as fully usable when a Wiener Wohn-Ticket is already available.",
                "step": "providers",
                "advanced_panel": "provider_policies",
                "hidden": not show_public_housing_policy_controls,
            },
            {
                "type": "checkbox",
                "name": "subsidized_required",
                "label": "Subsidized or cooperative supply only",
                "value": "true",
                "checked": bool(property_preferences.get("subsidized_required")),
                "tooltip": "Bias the search toward geforderte, cooperative, and municipal supply instead of private-market inventory.",
                "step": "providers",
                "advanced_panel": "provider_policies",
                "hidden": not show_public_housing_policy_controls,
            },
            {
                "type": "checkbox",
                "name": "miete_mit_kaufoption",
                "label": "Prefer Miete mit Kaufoption",
                "value": "true",
                "checked": bool(property_preferences.get("miete_mit_kaufoption")),
                "tooltip": "Keep lease-to-own style cooperative offers visible as their own eligibility-sensitive group.",
                "step": "providers",
                "advanced_panel": "provider_policies",
                "hidden": not show_public_housing_policy_controls,
            },
            {
                "type": "range",
                "name": "eigenmittel_max_eur",
                "label": "Max Eigenmittel",
                "value": str(property_preferences.get("eigenmittel_max_eur") or 0),
                "min": "0",
                "max": "150000",
                "visual_max": "150000",
                "range_step": "1000",
                "format": "currency_eur",
                "currency_code": selected_currency_code,
                "empty_label": "Any Eigenmittel",
                "scale_min_label": "Any",
                "scale_max_label": _currency_short(150_000),
                "tooltip": "Treat cooperative or subsidized offers above this financing contribution as a weaker fit instead of hiding them completely.",
                "step": "providers",
                "advanced_panel": "provider_policies",
                "hidden": not show_public_housing_policy_controls,
            },
            {
                "type": "range",
                "name": "application_window_days",
                "label": "Application window",
                "value": str(property_preferences.get("application_window_days") or 0),
                "min": "0",
                "max": "90",
                "visual_max": "90",
                "range_step": "1",
                "format": "days",
                "empty_label": "Any application window",
                "scale_min_label": "Any",
                "scale_max_label": "90 days",
                "tooltip": "Keep short registration windows visible as an urgency signal when cooperative or subsidized stock is scarce.",
                "step": "providers",
                "advanced_panel": "provider_policies",
                "hidden": not show_public_housing_policy_controls,
            },
            {
                "type": "checkbox",
                "name": "include_distressed_sale_signals",
                "label": "Court and auction listings",
                "value": "true",
                "checked": bool(property_preferences.get("include_distressed_sale_signals")),
                "tooltip": "Keep court-published, auction, and forced-sale listings visible as a separate source family.",
                "step": "providers",
                "advanced_panel": "provider_policies",
                "hidden": selected_listing_mode != "buy",
            },
            {
                "type": "keyword_priority_group",
                "name": "keywords",
                "label": "What matters",
                "options": keyword_preference_options,
                "school_preference_options": school_preference_options,
                "step": "children",
            },
            {
                "type": "text",
                "name": "custom_keywords",
                "label": "Custom priorities",
                "value": custom_keywords,
                "placeholder": "Add something not listed above",
                "tooltip": "If the same custom preference is requested three times, it should be promoted into this user's default catalog. If many users request the same thing, it should become available for everyone.",
                "step": "children",
            },
            {
                "type": "checkbox",
                "name": "enable_building_risk_research",
                "label": "Building and operating-cost research",
                "value": "true",
                "checked": bool(property_preferences.get("enable_building_risk_research")),
                "tooltip": "Investigate reserve fund, renovation pressure, energy risk, special levies, and operating-cost exposure.",
                "step": "research",
                "advanced_panel": "research_scope",
            },
            {
                "type": "checkbox",
                "name": "enable_market_supply_research",
                "label": "Market supply and exit research",
                "value": "true",
                "checked": bool(property_preferences.get("enable_market_supply_research")),
                "tooltip": "Investigate developer pipeline, competing supply, target-demand depth, and exit liquidity.",
                "step": "research",
                "advanced_panel": "research_scope",
            },
            {
                "type": "checkbox",
                "name": "enable_location_risk_research",
                "label": "Micro-location risk research",
                "value": "true",
                "checked": bool(property_preferences.get("enable_location_risk_research")),
                "tooltip": "Investigate safety, schools, clinics, daily-life access, pollution, flood, heat, and nuisance burden.",
                "step": "research",
                "advanced_panel": "research_scope",
            },
            {
                "type": "checkbox_group",
                "name": "school_stage_preferences",
                "label": "Children and school needs",
                "options": [
                    {"value": "kindergarten", "label": "Kindergarten"},
                    {"value": "public_kindergarten", "label": "Öffentlicher Kindergarten"},
                    {"value": "private_kindergarten", "label": "Privater Kindergarten"},
                    {"value": "volksschule", "label": "Volksschule"},
                    {"value": "ganztags_volksschule", "label": "Ganztagsvolksschule"},
                    {"value": "halbtags_volksschule", "label": "Halbtagsvolksschule"},
                    {"value": "gymnasium", "label": "Gymnasium"},
                ],
                "values": list(property_preferences.get("school_stage_preferences") or []),
                "step": "children",
                "hidden": True,
            },
            {
                "type": "select",
                "name": "school_evidence_priority",
                "label": "School detail level",
                "value": str(property_preferences.get("school_evidence_priority") or "any"),
                "options": [
                    {"value": "any", "label": "Any"},
                    {"value": "important", "label": "Important"},
                    {"value": "very_important", "label": "Very important"},
                ],
                "step": "children",
                "hidden": True,
            },
            {
                "type": "checkbox",
                "name": "require_school_evidence",
                "label": "Require school data",
                "value": "true",
                "checked": bool(property_preferences.get("require_school_evidence")),
                "tooltip": "Use official school data instead of inferring too much from generic map proximity.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "range",
                "name": "max_distance_to_kindergarten_m",
                "label": "Kindergarten radius",
                "value": str(property_preferences.get("max_distance_to_kindergarten_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Distance preference for kindergarten access. Ranking keeps missing details visible instead of hiding them.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "select",
                "name": "max_distance_to_kindergarten_importance",
                "label": "Kindergarten importance",
                "value": str(property_preferences.get("max_distance_to_kindergarten_importance") or "important"),
                "options": [
                    {"value": "must_have", "label": "Must have"},
                    {"value": "important", "label": "Important"},
                    {"value": "nice_to_have", "label": "Nice to have"},
                ],
                "tooltip": "Controls how strongly kindergarten distance affects ranking and adaptive radius relaxation.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "range",
                "name": "max_distance_to_ganztags_volksschule_m",
                "label": "Ganztagsvolksschule radius",
                "value": str(property_preferences.get("max_distance_to_ganztags_volksschule_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Distance preference for full-day primary school access.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "select",
                "name": "max_distance_to_ganztags_volksschule_importance",
                "label": "Ganztagsvolksschule importance",
                "value": str(property_preferences.get("max_distance_to_ganztags_volksschule_importance") or "important"),
                "options": [
                    {"value": "must_have", "label": "Must have"},
                    {"value": "important", "label": "Important"},
                    {"value": "nice_to_have", "label": "Nice to have"},
                ],
                "tooltip": "Controls how strongly full-day primary school distance affects ranking.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "range",
                "name": "max_distance_to_halbtags_volksschule_m",
                "label": "Halbtagsvolksschule radius",
                "value": str(property_preferences.get("max_distance_to_halbtags_volksschule_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Distance preference for half-day primary school access.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "select",
                "name": "max_distance_to_halbtags_volksschule_importance",
                "label": "Halbtagsvolksschule importance",
                "value": str(property_preferences.get("max_distance_to_halbtags_volksschule_importance") or "important"),
                "options": [
                    {"value": "must_have", "label": "Must have"},
                    {"value": "important", "label": "Important"},
                    {"value": "nice_to_have", "label": "Nice to have"},
                ],
                "tooltip": "Controls how strongly half-day primary school distance affects ranking.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "range",
                "name": "max_distance_to_playground_m",
                "label": "Playground radius",
                "value": str(property_preferences.get("max_distance_to_playground_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Distance preference for playground access. If good matches are scarce, PropertyQuarry relaxes this radius and marks the gap instead of returning nothing.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "select",
                "name": "max_distance_to_playground_importance",
                "label": "Importance",
                "value": str(property_preferences.get("max_distance_to_playground_importance") or "important"),
                "options": [
                    {"value": "must_have", "label": "Must have"},
                    {"value": "important", "label": "Important"},
                    {"value": "nice_to_have", "label": "Nice to have"},
                ],
                "tooltip": "Controls how strongly playground distance affects ranking and adaptive radius relaxation.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "range",
                "name": "max_distance_to_library_m",
                "label": "Library radius",
                "value": str(property_preferences.get("max_distance_to_library_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Distance preference for a public library or comparable Bücherei. Sparse searches relax this radius before returning an empty shortlist.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "select",
                "name": "max_distance_to_library_importance",
                "label": "Library importance",
                "value": str(property_preferences.get("max_distance_to_library_importance") or "nice_to_have"),
                "options": [
                    {"value": "must_have", "label": "Must have"},
                    {"value": "important", "label": "Important"},
                    {"value": "nice_to_have", "label": "Nice to have"},
                ],
                "tooltip": "Controls how strongly library distance affects ranking and adaptive radius relaxation.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "range",
                "name": "max_distance_to_zoo_m",
                "label": "Max distance to zoo",
                "value": str(property_preferences.get("max_distance_to_zoo_m") or 0),
                "min": "0",
                "max": "7000",
                "visual_max": "7000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "7 km",
                "tooltip": "Optional family and weekend-life signal. Only keep listings within this distance of a zoo or Tiergarten.",
                "step": "children",
                "availability_key": "family_zoo",
                "disabled_reason": "No practical zoo or Tiergarten signal is configured for this market yet.",
                "hidden": True,
            },
            {
                "type": "checkbox",
                "name": "enable_commute_research",
                "label": "Commute reality research",
                "value": "true",
                "checked": bool(property_preferences.get("enable_commute_research")),
                "tooltip": "Check actual travel times at realistic times of day instead of relying only on straight-line distance.",
                "step": "reachability",
            },
            {
                "type": "text",
                "name": "commute_destination",
                "label": "Primary destination",
                "value": str(property_preferences.get("commute_destination") or ""),
                "placeholder": "Workplace, university, Oma, or another key address",
                "step": "reachability",
                "advanced_panel": "commute",
            },
            {
                "type": "text",
                "name": "additional_reachability_targets",
                "label": "Additional destinations",
                "value": str(property_preferences.get("additional_reachability_targets") or ""),
                "placeholder": "Comma-separated: office, grandma, club, doctor",
                "step": "reachability",
                "advanced_panel": "commute",
            },
            {
                "type": "checkbox_group",
                "name": "preferred_reachability_modes",
                "label": "Reachability modes",
                "options": [
                    {"value": "public_transit", "label": "Public transit"},
                    {"value": "bike", "label": "Bike"},
                    {"value": "car", "label": "Car"},
                    {"value": "walk", "label": "Walk"},
                ],
                "values": list(property_preferences.get("preferred_reachability_modes") or []),
                "step": "reachability",
                "advanced_panel": "commute",
            },
            {
                "type": "range",
                "name": "max_commute_minutes_transit",
                "label": "Max commute by transit",
                "value": str(property_preferences.get("max_commute_minutes_transit") or 0),
                "min": "0",
                "max": "180",
                "visual_max": "180",
                "range_step": "5",
                "format": "minutes",
                "empty_label": "Any transit commute",
                "scale_min_label": "Any",
                "scale_max_label": "180 min",
                "tooltip": "Maximum acceptable public-transit commute time.",
                "step": "reachability",
                "advanced_panel": "commute",
            },
            {
                "type": "range",
                "name": "max_commute_minutes_drive",
                "label": "Max commute by car",
                "value": str(property_preferences.get("max_commute_minutes_drive") or 0),
                "min": "0",
                "max": "180",
                "visual_max": "180",
                "range_step": "5",
                "format": "minutes",
                "empty_label": "Any driving commute",
                "scale_min_label": "Any",
                "scale_max_label": "180 min",
                "tooltip": "Maximum acceptable driving commute time.",
                "step": "reachability",
                "advanced_panel": "commute",
            },
            {
                "type": "range",
                "name": "max_commute_minutes_bike",
                "label": "Max commute by bike",
                "value": str(property_preferences.get("max_commute_minutes_bike") or 0),
                "min": "0",
                "max": "180",
                "visual_max": "180",
                "range_step": "5",
                "format": "minutes",
                "empty_label": "Any cycling commute",
                "scale_min_label": "Any",
                "scale_max_label": "180 min",
                "tooltip": "Maximum acceptable cycling commute time.",
                "step": "reachability",
                "advanced_panel": "commute",
            },
            {
                "type": "range",
                "name": "max_commute_minutes_walk",
                "label": "Max commute by foot",
                "value": str(property_preferences.get("max_commute_minutes_walk") or 0),
                "min": "0",
                "max": "180",
                "visual_max": "180",
                "range_step": "5",
                "format": "minutes",
                "empty_label": "Any walking commute",
                "scale_min_label": "Any",
                "scale_max_label": "180 min",
                "tooltip": "Maximum acceptable walking time for adult destinations.",
                "step": "reachability",
                "advanced_panel": "commute",
            },
            {
                "type": "checkbox_group",
                "name": "desired_project_stages",
                "label": "Accepted project stages",
                "options": [
                    {"value": "existing", "label": "Existing"},
                    {"value": "under_construction", "label": "Under construction"},
                    {"value": "planned", "label": "Planned"},
                    {"value": "waitlist", "label": "Waitlist"},
                    {"value": "pre_registration", "label": "Pre-registration"},
                ],
                "values": list(property_preferences.get("desired_project_stages") or []),
                "step": "research",
                "hidden": not show_developer_project_stage_controls,
            },
            {
                "type": "checkbox",
                "name": "apply_unknowns_penalty",
                "label": "Penalize unknowns in ranking",
                "value": "true",
                "checked": bool(property_preferences.get("apply_unknowns_penalty")),
                "tooltip": "Keep strong unknown-heavy listings visible if they fit, but rank better-known candidates above them.",
                "step": "research",
            },
            {
                "type": "checkbox",
                "name": "enable_action_readiness_research",
                "label": "Next steps",
                "value": "true",
                "checked": bool(property_preferences.get("enable_action_readiness_research")),
                "tooltip": "Show the next questions, documents, and follow-ups for serious matches.",
                "step": "research",
            },
            {
                "type": "checkbox",
                "name": "require_energy_certificate",
                "label": "Require energy certificate",
                "value": "true",
                "checked": bool(property_preferences.get("require_energy_certificate")),
                "tooltip": "Treat a missing Energieausweis as a material gap, especially for Austrian buy and cooperative checks.",
                "step": "research",
                "hidden": land_only_search,
            },
            {
                "type": "checkbox",
                "name": "require_operating_cost_statement",
                "label": "Require operating costs",
                "value": "true",
                "checked": bool(property_preferences.get("require_operating_cost_statement")),
                "tooltip": "Keep Betriebskosten and recurring costs visible before a property is treated as ready for pursuit.",
                "step": "research",
                "hidden": land_only_search,
            },
            {
                "type": "checkbox",
                "name": "enable_auction_legal_review",
                "label": "Court and auction review",
                "value": "true",
                "checked": bool(property_preferences.get("enable_auction_legal_review")),
                "tooltip": "Keep court-sale and auction listings separate from normal homes and flag them for extra legal review.",
                "step": "research",
                "hidden": not show_distressed_review_controls,
            },
            {
                "type": "checkbox",
                "name": "enable_lifestyle_research",
                "label": "Freizeit und Alltag",
                "value": "true",
                "checked": bool(property_preferences.get("enable_lifestyle_research")),
                "tooltip": "Track lifestyle distance signals like Starbucks and fitness centers separately from hard investment or family-risk criteria.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "text",
                "name": "university_name",
                "label": "University focus",
                "value": str(property_preferences.get("university_name") or ""),
                "placeholder": "University of Vienna, WU, TU Wien",
                "step": "children",
                "hidden": not show_lifestyle_research_controls,
            },
            {
                "type": "range",
                "name": "max_distance_to_university_m",
                "label": "Max distance to university",
                "value": str(property_preferences.get("max_distance_to_university_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Keep university proximity visible as a livability and investment signal. Use the university name above for a target campus or institution.",
                "step": "children",
                "hidden": not show_lifestyle_research_controls,
            },
            {
                "type": "range",
                "name": "max_distance_to_starbucks_m",
                "label": "Max distance to Starbucks",
                "value": str(property_preferences.get("max_distance_to_starbucks_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Optional fun filter. Only keep listings within this distance of the nearest Starbucks.",
                "step": "children",
                "hidden": not show_lifestyle_research_controls,
            },
            {
                "type": "range",
                "name": "max_distance_to_fitness_center_m",
                "label": "Max distance to fitness center",
                "value": str(property_preferences.get("max_distance_to_fitness_center_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Optional fun filter. Only keep listings within this distance of the nearest fitness center or gym.",
                "step": "children",
                "hidden": not show_lifestyle_research_controls,
            },
            {
                "type": "range",
                "name": "max_distance_to_cinema_m",
                "label": "Max distance to cinema",
                "value": str(property_preferences.get("max_distance_to_cinema_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Optional fun filter. Only keep listings within this distance of the nearest cinema.",
                "step": "children",
                "hidden": not show_lifestyle_research_controls,
            },
            {
                "type": "range",
                "name": "max_distance_to_bouldering_m",
                "label": "Max distance to bouldering gym",
                "value": str(property_preferences.get("max_distance_to_bouldering_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Optional fun filter. Only keep listings within this distance of the nearest bouldering or climbing gym.",
                "step": "children",
                "hidden": not show_lifestyle_research_controls,
            },
            {
                "type": "range",
                "name": "max_distance_to_dog_park_m",
                "label": "Max distance to dog park",
                "value": str(property_preferences.get("max_distance_to_dog_park_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Optional fun filter. Only keep listings within this distance of the nearest dog park or dog exercise area.",
                "step": "children",
                "hidden": not show_lifestyle_research_controls,
            },
            {
                "type": "range",
                "name": "max_distance_to_good_cafe_m",
                "label": "Max distance to good cafe",
                "value": str(property_preferences.get("max_distance_to_good_cafe_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Optional fun filter. Only keep listings within this distance of the nearest cafe-quality proxy.",
                "step": "children",
                "hidden": not show_lifestyle_research_controls,
            },
            {
                "type": "range",
                "name": "max_distance_to_supermarket_m",
                "label": "Supermarket radius",
                "value": str(property_preferences.get("max_distance_to_supermarket_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Distance preference for everyday groceries. If good matches are scarce, this radius is relaxed and reported instead of hiding every result.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "select",
                "name": "max_distance_to_supermarket_importance",
                "label": "Supermarket importance",
                "value": str(property_preferences.get("max_distance_to_supermarket_importance") or "important"),
                "options": [
                    {"value": "must_have", "label": "Must have"},
                    {"value": "important", "label": "Important"},
                    {"value": "nice_to_have", "label": "Nice to have"},
                ],
                "tooltip": "Controls how strongly supermarket distance affects ranking and adaptive radius relaxation.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "range",
                "name": "max_distance_to_market_m",
                "label": "Max distance to market",
                "value": str(property_preferences.get("max_distance_to_market_m") or 0),
                "min": "0",
                "max": "5000",
                "visual_max": "5000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "5 km",
                "tooltip": "Optional district-life filter. Covers produce markets and walkable market streets like Naschmarkt.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "range",
                "name": "max_distance_to_hardware_store_m",
                "label": "Max distance to hardware store",
                "value": str(property_preferences.get("max_distance_to_hardware_store_m") or 0),
                "min": "0",
                "max": "7000",
                "visual_max": "7000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "7 km",
                "tooltip": "Useful for renovation and everyday practical access. Tracks DIY and hardware-store distance.",
                "step": "children",
            },
            {
                "type": "range",
                "name": "max_distance_to_shopping_center_m",
                "label": "Max distance to shopping center",
                "value": str(property_preferences.get("max_distance_to_shopping_center_m") or 0),
                "min": "0",
                "max": "7000",
                "visual_max": "7000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "7 km",
                "tooltip": "Tracks larger shopping centers for errands and bad-weather convenience.",
                "step": "children",
            },
            {
                "type": "range",
                "name": "max_distance_to_shopping_street_m",
                "label": "Max distance to promenade",
                "value": str(property_preferences.get("max_distance_to_shopping_street_m") or 0),
                "min": "0",
                "max": "7000",
                "visual_max": "7000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "7 km",
                "tooltip": "Tracks pedestrian-heavy shopping streets and promenade zones for strolling and city-life fit.",
                "step": "children",
            },
            {
                "type": "range",
                "name": "max_distance_to_theatre_m",
                "label": "Max distance to theatre",
                "value": str(property_preferences.get("max_distance_to_theatre_m") or 0),
                "min": "0",
                "max": "7000",
                "visual_max": "7000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "7 km",
                "tooltip": "Optional culture filter. Only keep listings within this distance of a theatre.",
                "step": "children",
            },
            {
                "type": "range",
                "name": "max_distance_to_public_pool_m",
                "label": "Max distance to public pool",
                "value": str(property_preferences.get("max_distance_to_public_pool_m") or 0),
                "min": "0",
                "max": "7000",
                "visual_max": "7000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "7 km",
                "tooltip": "Useful for family leisure and everyday sport access. Tracks public swimming pools.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "range",
                "name": "max_distance_to_medical_care_m",
                "label": "Max distance to doctors and hospitals",
                "value": str(property_preferences.get("max_distance_to_medical_care_m") or 0),
                "min": "0",
                "max": "7000",
                "visual_max": "7000",
                "range_step": "50",
                "format": "meters_cap",
                "empty_label": "Any distance",
                "scale_min_label": "Any",
                "scale_max_label": "7 km",
                "tooltip": "Tracks proximity to doctors, health centers, clinics, and hospitals. Stronger signal when children or elder-care logistics matter.",
                "step": "children",
                "hidden": True,
            },
            {
                "type": "checkbox",
                "name": "prefer_good_air_quality",
                "label": "Good air quality matters",
                "value": "true",
                "checked": bool(property_preferences.get("prefer_good_air_quality")),
                "tooltip": "Treat poor air quality as a risk signal in deep research and ranking.",
                "step": "children",
            },
            {
                "type": "checkbox",
                "name": "prefer_heat_resilient_home",
                "label": _property_heat_resilience_copy(selected_language_code)["label"],
                "value": "true",
                "checked": bool(property_preferences.get("prefer_heat_resilient_home")),
                "tooltip": _property_heat_resilience_copy(selected_language_code)["tooltip"],
                "step": "children",
            },
            {
                "type": "checkbox",
                "name": "avoid_noise_risk_area",
                "label": "Avoid noise-risk area",
                "value": "true",
                "checked": bool(property_preferences.get("avoid_noise_risk_area")),
                "tooltip": "Use official Austrian noise maps and route exposure signals as ranking penalties or suppression reasons.",
                "step": "children",
            },
            {
                "type": "checkbox",
                "name": "require_high_speed_internet",
                "label": "Require high-speed internet",
                "value": "true",
                "checked": bool(property_preferences.get("require_high_speed_internet")),
                "tooltip": "Promote listings with strong broadband coverage when home-office viability matters.",
                "step": "children",
            },
            {
                "type": "checkbox",
                "name": "prefer_low_crime_area",
                "label": "Low crime area matters",
                "value": "true",
                "checked": bool(property_preferences.get("prefer_low_crime_area")),
                "tooltip": "Treat crime burden and safety pattern as a genuine risk factor in deep research.",
                "step": "children",
            },
            {
                "type": "checkbox",
                "name": "require_drinking_water_quality_research",
                "label": "Research water source and groundwater burden",
                "value": "true",
                "checked": bool(property_preferences.get("require_drinking_water_quality_research")),
                "tooltip": "Ask deep research to investigate Hochquellwasser versus groundwater dependency and any public burden signals.",
                "step": "children",
            },
            {
                "type": "checkbox",
                "name": "require_parking_pressure_check",
                "label": "Check parking situation if no garage",
                "value": "true",
                "checked": bool(property_preferences.get("require_parking_pressure_check")),
                "tooltip": "If the listing has no garage, deep research should investigate general street-parking pressure and paid-parking burden.",
                "step": "children",
            },
            {
                "type": "checkbox",
                "name": "avoid_cesspit_or_septic_risk",
                "label": "Avoid Senkgrube or septic risk",
                "value": "true",
                "checked": bool(property_preferences.get("avoid_cesspit_or_septic_risk")),
                "tooltip": "Treat cesspit or septic dependence, costs, and smell burden as a risk that must be clarified.",
                "step": "children",
            },
            {
                "type": "checkbox",
                "name": "require_winter_access_research",
                "label": "Check winter driving conditions",
                "value": "true",
                "checked": bool(property_preferences.get("require_winter_access_research")),
                "tooltip": "For more remote properties, deep research should investigate winter snow access, slope, and seasonal driving constraints.",
                "step": "children",
            },
            {
                "type": "checkbox",
                "name": "avoid_flood_risk_area",
                "label": "Avoid flood-risk area",
                "value": "true",
                "checked": bool(property_preferences.get("avoid_flood_risk_area")),
                "tooltip": "Treat historic flooding, runoff, and river or drainage exposure as a serious location risk in deep research.",
                "step": "children",
            },
            {
                "type": "checkbox",
                "name": "enable_trust_risk_scoring",
                "label": "Check listing quality",
                "value": "true",
                "checked": bool(property_preferences.get("enable_trust_risk_scoring")),
                "tooltip": "Check duplicate, stale, and scam risk rather than treating every listing site equally.",
                "step": "children",
            },
            {
                "type": "range",
                "name": "max_price_eur",
                "label": "Max budget",
                "value": str(property_price_value),
                "min": "0",
                "max": str(property_price_slider_max),
                "visual_max": str(property_price_slider_max),
                "range_step": str(property_price_slider_step),
                "format": "currency_eur",
                "currency_code": selected_currency_code,
                "empty_label": "Any budget",
                "scale_min_label": "No max",
                "scale_max_label": _currency_short(property_price_slider_max),
                "tooltip": "Set a hard budget ceiling. Leave it at Any budget when you want PropertyQuarry to rank first and filter price later.",
                "range_preset": "listing_mode_price",
                "range_presets": property_price_range_presets,
                "step": "what",
            },
            {
                "type": "range",
                "name": "min_rooms",
                "label": "Min rooms",
                "value": str(property_min_rooms_value),
                "min": "0",
                "max": "8",
                "visual_max": "8",
                "range_step": "1",
                "format": "rooms",
                "empty_label": "Any rooms",
                "scale_min_label": "Any",
                "scale_max_label": "8+ rooms",
                "tooltip": "Minimum room count. Keep this open when layout quality matters more than the advertised room number.",
                "step": "what",
                "hidden": land_only_search,
            },
            {
                "type": "range",
                "name": "min_area_m2",
                "label": "Min area",
                "value": str(property_min_area_value),
                "min": "0",
                "max": "250",
                "visual_max": "250",
                "range_step": "5",
                "format": "area_m2",
                "empty_label": "Any size",
                "scale_min_label": "Any",
                "scale_max_label": "250+ m2",
                "tooltip": "Minimum usable area. Larger minimums reduce weak matches but can make sparse auction or cooperative listings disappear.",
                "step": "what",
            },
            {
                "type": "range",
                "name": "available_within_years",
                "label": "Move-in deadline",
                "value": str(property_available_within_years_value),
                "min": "0",
                "max": "10",
                "visual_max": "10",
                "range_step": "1",
                "format": "availability_years",
                "empty_label": "Any delivery date",
                "scale_min_label": "Any",
                "scale_max_label": "10 years",
                "tooltip": "Filter for listings or projects that should be ready within the selected number of years. Useful for cooperative and planned development sign-ups.",
                "step": "what",
            },
            {
                "type": "checkbox",
                "name": "search_agent_enabled",
                "label": "Save as recurring search",
                "value": "true",
                "checked": property_search_agent_enabled,
                "tooltip": "Save these settings as a recurring search that keeps watching the market. Disable this checkbox to keep the settings as a one-off brief only.",
                "step": "providers",
            },
            {
                "type": "range",
                "name": "search_agent_duration_days",
                "label": "Recurring search duration",
                "value": str(property_search_agent_duration_days),
                "min": "7",
                "max": "365",
                "visual_max": "365",
                "range_step": "7",
                "format": "agent_duration_days",
                "scale_min_label": "1 week",
                "scale_mid_label": "6 months",
                "scale_max_label": "1 year",
                "tooltip": "How long this recurring search should stay active before it expires or needs review.",
                "step": "providers",
                "hidden": not show_search_agent_detail_controls,
            },
            {
                "type": "range",
                "name": "search_agent_notification_limit",
                "label": "Notification budget",
                "value": str(property_search_agent_notification_limit),
                "min": "1",
                "max": "50",
                "visual_max": "50",
                "range_step": "1",
                "format": "notification_count",
                "scale_min_label": "1",
                "scale_mid_label": "25",
                "scale_max_label": "50",
                "tooltip": "Maximum Telegram property alerts to send in the selected period. If more matches exist, PropertyQuarry ranks them and sends only the best ones.",
                "step": "providers",
                "hidden": not show_search_agent_detail_controls,
            },
            {
                "type": "select",
                "name": "search_agent_notification_period",
                "label": "Notification period",
                "value": property_search_agent_notification_period,
                "options": [
                    {"value": "day", "label": "Per day"},
                    {"value": "week", "label": "Per week"},
                ],
                "tooltip": "Choose whether the notification budget resets daily or weekly.",
                "step": "providers",
                "hidden": not show_search_agent_detail_controls,
            },
            {
                "type": "checkbox",
                "name": "require_floorplan",
                "label": "Serious listings only - floor plan required",
                "value": "true",
                "checked": bool(property_preferences.get("require_floorplan")),
                "step": "providers",
                "hidden": land_only_search,
            },
            {
                "type": "checkbox",
                "name": "force_refresh",
                "label": "Refresh listings",
                "value": "true",
                "checked": bool(property_preferences.get("force_refresh")),
                "step": "providers",
            },
        ],
        "meta": {
            "preferences_endpoint": str(property_state.get("preferences_endpoint") or ""),
            "start_endpoint": str(property_state.get("start_endpoint") or ""),
            "run_id": str(property_run.get("run_id") or ""),
            "initial_run": property_run,
            "platform_catalog_by_country": _sanitize_platform_catalog_for_client(
                dict(property_state.get("platform_catalog_by_country") or {})
            ),
            "default_language_by_country": dict(property_state.get("default_language_by_country") or {}),
            "region_catalog_by_country": region_catalog_by_country,
            "market_filter_capabilities_by_country_region": market_filter_capabilities_by_country_region,
            "market_filter_capabilities": market_filter_capabilities,
            "location_catalog_by_country_region": location_catalog_by_country_region,
            "supports_full_region_scope": True,
            "commercial": dict(property_state.get("commercial") or {}),
            "furniture_style_catalog": [dict(row) for row in PROPERTY_FURNITURE_STYLE_CATALOG],
            "billing_checkout_enabled": bool(property_state.get("billing_checkout_enabled")),
            "billing_checkout_enabled_plans": list(property_state.get("billing_checkout_enabled_plans") or []),
            "billing_order_endpoint": str(property_state.get("billing_order_endpoint") or ""),
            "billing_order_endpoints_by_plan": dict(property_state.get("billing_order_endpoints_by_plan") or {}),
            "feedback_person_id": str(property_preferences.get("preference_person_id") or "self"),
            "search_agent": property_search_agent,
            "search_agents": property_search_agents,
            "search_mode_persisted": str(property_preferences.get("search_mode") or "").strip().lower() == "discovery",
            "search_agent_update_endpoint_template": "/v1/onboarding/property-search/agents/__AGENT_ID__",
            "shortlist_candidates": property_shortlist_cards,
            "wizard_steps": [
                {
                    "key": "search",
                    "label": "Where",
                    "detail": "Country, market, and target areas.",
                },
                {
                    "key": "what",
                    "label": "What",
                    "detail": "Type, budget, size, move-in.",
                },
                {
                    "key": "children",
                    "label": "What matters",
                    "detail": "Daily-life priorities that shape ranking.",
                },
                {
                    "key": "reachability",
                    "label": "Reachability",
                    "detail": "Destinations, travel modes, and time limits that change the ranking.",
                },
                {
                    "key": "research",
                    "label": "Research depth",
                    "detail": "Risk, supply, and how much detail each strong match should carry.",
                },
                {
                    "key": "providers",
                    "label": "Providers",
                    "detail": "Choose providers.",
                },
            ],
        },
    }
    mapping: dict[str, dict[str, object]] = {
        "today": {
            "title": "Today",
            "summary": str(
                preview.get("headline")
                or status.get("next_step")
                or "Start with the current search, review the strongest homes, and keep follow-ups visible."
            ),
            "cards": [
                {
                    "eyebrow": "Today",
                    "title": "What needs action now",
                    "body": "Start with real property decisions instead of a generic dashboard.",
                    "items": live_queue
                    or string_rows(
                        first_brief,
                        ("Connect Google sign-in if you want easier return access from the same account.",),
                        tag="Next",
                        detail="This is the shortest path to a useful search session.",
                    ),
                },
                {
                    "eyebrow": "Alerts",
                    "title": "What is queued",
                    "body": "Queued alerts stay visible and easy to stop.",
                    "items": pending_delivery_items
                    or string_rows(
                        suggested,
                        ("No queued alerts yet.",),
                        tag="Review",
                        detail="Once an alert or follow-up is ready, it will show up here.",
                    ),
                },
                {
                    "eyebrow": "Search signal",
                    "title": "What is shaping the search",
                    "body": "The current search stays visible and tied to real results.",
                    "items": string_rows(first_brief, ("No search items yet.",), tag="Search", detail="Use this to decide what to review first."),
                },
                {
                    "eyebrow": "Identity and channels",
                    "title": "Keep setup boring and useful",
                    "body": "Identity stays simple. Channels widen coverage only after the first search works.",
                    "items": identity_posture_items,
                },
            ],
        },
        "queue": {
            "title": "Decisions",
            "summary": str(preview.get("headline") or "Turn search activity into clear decisions: pursue, maybe, dismiss, or follow up."),
            "cards": [
                {
                    "eyebrow": "Decision signal",
                    "title": "What changed",
                    "body": "The queue explains what changed, why it matters, and what decision belongs next.",
                    "items": string_rows(first_brief, ("No search items yet.",), tag="Search", detail="This is the current ranked search signal."),
                },
                {
                    "eyebrow": "Themes",
                    "title": "Recurring topics",
                    "body": "Themes help review results without reopening every property.",
                    "items": string_rows(themes, ("No themes surfaced yet.",), tag="Theme", detail="This theme is active in the current search."),
                },
                {
                    "eyebrow": "Open reviews",
                    "title": "What the queue clears",
                    "body": "A useful queue ends in a property decision or a clear follow-up.",
                    "items": live_queue
                    or string_rows(
                        suggested,
                        ("No live review items yet.",),
                        tag="Queue",
                        detail="Once the search starts moving, review items appear here.",
                    ),
                },
                {
                    "eyebrow": "Collaborators",
                    "title": "People attached to decisions",
                    "body": "People only matter here when they are tied to a property decision.",
                    "items": string_rows(people, ("No collaborators added yet.",), tag="Person", detail="This person is active in the current search."),
                },
            ],
        },
        "commitments": {
            "title": "Follow-ups",
            "summary": "Questions, viewings, and notes only matter when they move a property decision forward.",
            "cards": [
                {
                    "eyebrow": "Follow-up pressure",
                    "title": "What is in motion",
                    "body": "This page shows which property questions are active and which decisions are waiting.",
                    "items": live_queue
                    or string_rows(
                        suggested,
                        ("No live follow-ups yet.",),
                        tag="Follow-up",
                        detail="Once questions or alerts exist, they will appear here.",
                    ),
                },
                {
                    "eyebrow": "Queued alerts",
                    "title": "What is waiting",
                    "body": "Alerts and property questions stay visible before they leave the account.",
                    "items": pending_delivery_items
                    or string_rows(
                        channel_lines,
                        ("No alert queue yet.",),
                        tag="Ready",
                        detail="Connected channels determine which alerts can be sent.",
                    ),
                },
                {
                    "eyebrow": "Priority",
                    "title": "What will bubble up next",
                    "body": "Follow-ups are ordered by search priority and deadlines, not noise.",
                    "items": string_rows(first_brief, ("No priorities surfaced yet.",), tag="Search", detail="This is the current signal for the follow-up queue."),
                },
            ],
        },
        "people": {
            "title": "People",
            "summary": "Keep collaborators tied to concrete property decisions, notes, and outcomes.",
            "cards": [
                {"eyebrow": "Collaborators", "title": "Who matters right now", "items": string_rows(people, ("No collaborators added yet.",), tag="Person", detail="These people are attached to the current search.")},
                {"eyebrow": "Shared themes", "title": "What keeps recurring", "items": string_rows(themes, ("No themes surfaced yet.",), tag="Theme", detail="Recurring themes stay available in the workspace.")},
                {"eyebrow": "Settings", "title": "What the account may keep", "items": string_rows(privacy_lines, ("No retention policy set yet.",), tag="Setting", detail="These settings define what the workspace retains.")},
            ],
        },
        "evidence": {
            "title": "Why this appears",
            "summary": "A plain explanation of which property signal, source, context, or rule put an item in front of you.",
            "cards": [
                {"eyebrow": "Search details", "title": "Why items surfaced", "items": string_rows(first_brief, ("No details surfaced yet.",), tag="Detail", detail="This is one of the signals behind the current view.")},
                {"eyebrow": "Settings", "title": "What keeps results explainable", "items": string_rows(trust_notes, ("No settings notes yet.",), tag="Rule", detail="These settings explain the product behavior.")},
                {"eyebrow": "Details", "title": "Where the details came from", "items": channel_items},
            ],
        },
        "channels": {
            "title": "Channels",
            "summary": "Channels widen coverage. They never redefine the product core or become the main story of the workspace.",
            "cards": [
                {"eyebrow": "Google", "title": cards[0]["label"], "items": [cards[0]["detail"], cards[0]["summary"] or "Google sign-in is the recommended first connection."]},
                {"eyebrow": "Telegram", "title": cards[1]["label"], "items": [cards[1]["detail"], cards[1]["summary"] or "Personal identity and bot install stay distinct."]},
                {"eyebrow": "WhatsApp", "title": cards[2]["label"], "items": [cards[2]["detail"], cards[2]["summary"] or "Business onboarding and export intake stay separate."]},
            ],
        },
        "automations": {
            "title": "Settings",
            "summary": "Settings stay understandable: alerts, prepared messages, retention, and sharing.",
            "cards": [
                {"eyebrow": "Account rules", "title": "Current rules", "items": privacy_lines},
                {"eyebrow": "Suggested changes", "title": "What to unlock next", "items": suggested},
                {"eyebrow": "Guardrails", "title": "Why these rules exist", "items": trust_notes},
            ],
        },
        "activity": {
            "title": "Activity",
            "summary": "Activity shows what changed, what was sent, and which setting allowed it.",
            "cards": [
                {"eyebrow": "Account", "title": "Current state", "items": string_rows([f"Status: {status_label}", f"Setup state: {status.get('onboarding_id') or 'not started'}", f"Next step: {status.get('next_step') or 'None'}"], ("No account state yet.",), tag="State", detail="This is the current account status.")},
                {"eyebrow": "Channels", "title": "Recent changes", "items": channel_items},
                {"eyebrow": "Settings", "title": "Why this feed matters", "items": string_rows(trust_notes, ("No settings notes yet.",), tag="Context", detail="This keeps the activity feed understandable.")},
            ],
        },
        "settings": {
            "title": "Settings",
            "summary": "Settings stay boring and explicit once the first useful search already exists.",
            "cards": [
                {"eyebrow": "Account", "title": "Current account settings", "items": string_rows([f"Name: {workspace.get('name') or 'PropertyQuarry'}", f"Mode: {humanize(str(workspace.get('mode') or 'personal'))}", f"Timezone: {workspace.get('timezone') or 'unspecified'}", f"Region: {workspace.get('region') or 'unspecified'}"], ("No account settings yet.",), tag="Account", detail="These are the current PropertyQuarry defaults.")},
                {"eyebrow": "Privacy", "title": "Product behavior", "items": string_rows(privacy_lines, ("No privacy rules set yet.",), tag="Rule", detail="These controls shape what the product may do.")},
                {"eyebrow": "Channels", "title": "Selected linked channels", "items": channel_items},
            ],
        },
        "properties": {
            "title": "Properties",
            "summary": (
                str(property_run.get("message") or "").strip()
                or "Run a dedicated cross-platform property search, keep the progress visible, and surface live 3D-tour matches instead of raw listing noise."
            ),
            "cards": [
                {
                    "eyebrow": "Search brief",
                    "title": "What this search is optimizing for",
                    "body": "The brief stays explicit: market, research language, target location, property shape, and who the ranking is trying to satisfy.",
                    "items": property_market_summary_items
                    + [
                        row_item(
                            "Active sites",
                            ", ".join(property_selected_platform_labels) if property_selected_platform_labels else "No sites saved yet.",
                            "Profile",
                        ),
                    ],
                },
                {
                    "eyebrow": "Market coverage",
                    "title": "Which listing sites this country unlocks",
                    "body": "Each market switches the site catalog. The saved selection should be a deliberate subset, not a hard-coded Austria-only list.",
                    "items": [
                        row_item(
                            "Country coverage",
                            f"{property_country_label} | {property_provider_total_for_country or len(platform_options)} supported sites",
                            "Coverage",
                        ),
                        row_item(
                            "Selected now",
                            str(len(property_selected_platform_labels) or 0),
                            "Selection",
                        ),
                    ] + (property_platform_rows[:4] if property_platform_rows else []),
                },
                {
                    "eyebrow": "Shortlist",
                    "title": "Ranked review desk",
                    "body": "The strongest matches stay review-ready: fit, risk, 360 status, property page, and the next useful action are visible before technical collection details.",
                    "items": property_shortlist_rows
                    or property_recent_matches
                    or [
                        row_item(
                            "First shortlist still pending",
                            "Launch the first search to see matching homes with property pages, tours, and plain fit reasons.",
                            "First search",
                        )
                    ],
                },
                {
                    "eyebrow": "Search status",
                    "title": "Current search",
                    "body": str(property_run.get("message") or "Start a search to see site-by-site progress, shortlisted 3D tours, and what is ready."),
                    "items": property_source_rows
                    or property_event_rows
                    or [
                        row_item(
                            "No live search in flight",
                            "Save the brief, then launch the first dedicated search to show site-by-site progress and shortlist formation here.",
                            "Ready",
                        )
                    ],
                },
                {
                    "eyebrow": "Learning loop",
                    "title": "What the product has learned from feedback",
                    "body": "Paid research only gets stronger if the system remembers what helped, what failed, and which requirements should suppress future noise.",
                    "items": property_learning_rows
                    or property_recent_feedback_rows
                    or [
                        row_item(
                            "Preference memory is still clean",
                            "Record feedback on packets and shortlists to teach future searches what to favor, what to suppress, and which requirements should stay strict.",
                            "Learning",
                        )
                    ],
                },
                {
                    "eyebrow": "Recent matches",
                    "title": "Hosted pages already delivered",
                    "body": "Strong matches should resolve to branded hosted property pages, not source links alone.",
                    "items": property_recent_matches
                    or property_event_rows
                    or [
                        row_item(
                            "No hosted follow-up has left the desk yet",
                            "The first credible packet, hosted page, or review follow-up will appear here once a candidate is strong enough to share.",
                            "Outbound",
                        )
                    ],
                },
            ],
            "stats": [
                {"label": "Country", "value": property_country_label},
                {"label": "Providers", "value": str(len(property_selected_platform_labels) or 0)},
                {
                    "label": "Lists used",
                    "value": str(int(property_summary.get("source_variant_total") or property_summary.get("sources_total") or 0)),
                },
                {"label": "Listings", "value": str(int(property_summary.get("listing_total") or 0))},
                {"label": "Hosted tours", "value": str(int(property_summary.get("tour_created_total") or 0) + int(property_summary.get("tour_existing_total") or 0))},
            ],
            "console_form": property_form,
        },
    }
    payload = dict(mapping[section])
    payload.setdefault("stats", stats)
    return payload


def property_workspace_payload(
    section: str,
    *,
    status: dict[str, object],
    property_state: dict[str, object],
) -> dict[str, object]:
    return build_property_workspace_payload(
        section,
        status=status,
        property_state=property_state,
    )


def admin_section_payload(section: str) -> dict[str, object]:
    mapping: dict[str, dict[str, object]] = {
        "policies": {
            "title": "Policies",
            "summary": "Operator-only controls for approval rules, task contracts, and promoted skills.",
            "cards": [
                {"eyebrow": "Policy", "title": "Runtime policy endpoints", "items": ["/v1/policy", "/v1/tasks/contracts", "/v1/skills"]},
                {"eyebrow": "Why it matters", "title": "Keep the product shell separate", "items": ["Buyers see the assistant workflow.", "Admins see the policy plane."]},
            ],
        },
        "providers": {
            "title": "Providers",
            "summary": "Bindings, 1min state, and control-plane views belong here, not in the main buyer navigation.",
            "cards": [
                {"eyebrow": "Provider APIs", "title": "Registry and health", "items": ["/v1/providers/registry", "/v1/providers/states", "/v1/providers/onemin/aggregate"]},
                {"eyebrow": "Operational focus", "title": "What this surface is for", "items": ["Capacity admission", "Binding state", "Runway and burn"]},
            ],
        },
        "audit-trail": {
            "title": "Audit Trail",
            "summary": "Evidence, telemetry, and delivery state stay visible to admins without leaking into the public product story.",
            "cards": [
                {"eyebrow": "Audit", "title": "Trace surfaces", "items": ["/v1/evidence", "/v1/delivery/pending"]},
                {"eyebrow": "Goal", "title": "What this surface needs", "items": ["Receipts", "Execution state", "Delivery confirmations"]},
            ],
        },
        "operators": {
            "title": "Operators",
            "summary": "Admin identity, backlog, and approval work stay in the admin surface.",
            "cards": [
                {"eyebrow": "Human review", "title": "Admin endpoints", "items": ["/v1/human/tasks"]},
                {"eyebrow": "Trust boundary", "title": "Why this is separate", "items": ["Admin identity is separate from the customer workspace surface.", "Audit trails depend on trusted admin records."]},
            ],
        },
        "api": {
            "title": "Runtime",
            "summary": "The admin center belongs behind the admin surface, not on public product pages.",
            "cards": [
                {"eyebrow": "OpenAPI", "title": "Schemas and entrypoints", "items": ["/openapi.json", "/v1/plans/compile", "/v1/rewrite", "/v1/responses"]},
                {"eyebrow": "Docs", "title": "Reference material", "items": ["README", "ARCHITECTURE_MAP", "CI smoke suite"]},
            ],
        },
    }
    payload = mapping[section]
    return {
        "stats": [
            {"label": "Surface", "value": "admin"},
            {"label": "Access", "value": "admin-only"},
            {"label": "Audience", "value": "admins"},
            {"label": "Goal", "value": "admin center"},
        ],
        **payload,
    }
