from __future__ import annotations

from functools import lru_cache
import json
import re

import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.product.property_surface_state import build_property_run_reliability_snapshot
from app.services.property_artifact_contracts import required_artifact_receipt_rows
from app.services.property_customer_copy import sanitize_property_marketing_copy, summarize_property_description_copy
from app.services.property_market_catalog import supported_currency_codes
from app.services.public_url_safety import public_http_url_is_safe


_PROPERTY_POSTAL_LOCALITY_PATTERN = re.compile(
    r"\b(?P<code>[1-9]\d{3,4})\s+(?P<locality>[A-ZÄÖÜ][A-Za-zÄÖÜäöüß' .\-/]{1,60})",
    flags=re.IGNORECASE,
)
_PROPERTY_POSTAL_LOCALITY_STOPWORDS = re.compile(
    r"\s+(?:m(?:²|2)|sqm|zimmer|rooms?|eur|€|usd|gbp|chf|der\s+standard|willhaben|immobilien|real\s+estate)\b.*$",
    flags=re.IGNORECASE,
)
_PROPERTY_MEDIA_URL_RE = re.compile(r"https?://[^\s<>'\")]+", flags=re.IGNORECASE)
_PROPERTY_FLOORPLAN_MARKERS = (
    "floorplan",
    "floor-plan",
    "floor_plan",
    "floor plan",
    "grundriss",
    "lageplan",
    "plan_top",
    "plan top",
    "raumskizze",
    "wohnungsplan",
)
_PROPERTY_FLOORPLAN_ASSET_EXTENSIONS = (
    ".avif",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
)
_PROPERTY_FLOORPLAN_TRACKING_MARKERS = (
    "/analytics/",
    "/beacon/",
    "/logevent/",
    "/tracking/",
    "floorplan-click",
    "floorplan_click",
)
_PROPERTY_SOURCE_360_PROVIDER_HOSTS = (
    "matterport.com",
    "tourmkr.com",
    "eye-spy360.com",
    "ogulo.com",
    "aroundmedia.com",
    "immoviewer.com",
    "giraffe360.com",
    "panoee.com",
    "cloudpano.com",
    "kuula.co",
    "roundme.com",
    "teliportme.com",
    "vieweet.com",
    "youvisit.com",
    "peek3d.app",
    "3dlook.at",
    "feelestate.com",
    "3d.laendleanzeiger.at",
    "360.kalandra.at",
)
_PROPERTY_SOURCE_360_PATH_TOKENS = (
    "360",
    "360tour",
    "360tours",
    "3d-tour",
    "3d-tours",
    "3dtour",
    "3d-wohnung",
    "360grad",
    "360grad-tour",
    "virtualtour",
    "virtual-tour",
    "virtual-tours",
    "peek3d",
    "360.homestaging",
    "immobilien360",
    "panorama",
    "panoramas",
)
_PROPERTY_SOURCE_360_PATH_TOKEN_RE = re.compile(
    rf"(?:^|[^a-z0-9])(?:{'|'.join(re.escape(token) for token in _PROPERTY_SOURCE_360_PATH_TOKENS)})(?:$|[^a-z0-9])"
)
_PROPERTY_SOURCE_360_TRACKING_MARKERS = (
    "/analytics/",
    "/beacon/",
    "/logevent/",
    "/tracking/",
    "virtual-tour-click",
    "virtual_tour_click",
)
_PROPERTY_VISUAL_REASON_KEYS = frozenset(
    {
        "browseract_connector_unconfigured",
        "crezlo_property_tour_not_configured",
        "fit_below_threshold",
        "floorplan_assets_unavailable",
        "floorplan_missing",
        "gallery_assets_unavailable",
        "listing_360_media_missing",
        "listing_expired",
        "magicfit_insufficient_credits",
        "property_tour_delivery_failed",
        "property_tour_execution_failed",
        "property_tour_fallback_disabled",
        "property_tour_rebuild_required",
        "property_tour_video_delivery_failed",
        "provider_export_missing",
        "pure_360_assets_unavailable",
        "user_requested_visual_generation",
    }
)
_PROPERTY_FLOORPLAN_URL_KEYS = (
    "floorplan_preview_url",
    "floorplan_url",
    "floorplan_image_url",
    "floorplan_pdf_url",
    "floor_plan_url",
    "floor_plan_image_url",
    "grundriss_url",
    "grundriss_image_url",
    "layout_plan_url",
)
_PROPERTY_FLOORPLAN_CONTAINER_KEYS = (
    "floorplan_urls_json",
    "floorplan_urls",
    "floorplans",
    "floor_plan_urls",
    "grundriss_urls",
    "media_urls_json",
    "photo_urls_json",
    "image_urls_json",
    "photos",
    "images",
    "media",
    "documents",
    "attachments",
)
_PROPERTY_SOURCE_360_URL_KEYS = (
    "source_virtual_tour_url",
    "vendor_tour_url",
    "virtual_tour_url",
    "virtualtour_url",
    "virtual_tour_href",
    "external_virtual_tour_url",
    "provider_virtual_tour_url",
    "source_360_url",
    "tour_360_url",
    "three_d_tour_url",
    "threed_tour_url",
    "matterport_url",
    "ogulo_url",
    "immoviewer_url",
    "panorama_url",
    "panorama_source",
)


def _property_visual_reason_key(*values: object) -> str:
    """Return only customer-safe, governed visual-state reason identifiers."""

    for value in values:
        normalized = str(value or "").strip().lower()
        if normalized in _PROPERTY_VISUAL_REASON_KEYS:
            return normalized
    return ""


_PROPERTY_SOURCE_360_CONTAINER_KEYS = (
    "source_virtual_tour_urls",
    "vendor_tour_urls",
    "virtual_tour_urls",
    "tour_urls_json",
    "panorama_media_urls_json",
    "media_urls_json",
    "links",
    "media",
    "photos",
    "images",
)


def _property_postal_names_from_text(text: object) -> tuple[str, ...]:
    normalized_text = re.sub(r"\s+", " ", str(text or "").strip())
    if not normalized_text:
        return ()
    names: list[str] = []
    seen: set[str] = set()
    for match in _PROPERTY_POSTAL_LOCALITY_PATTERN.finditer(normalized_text):
        code = str(match.group("code") or "").strip()
        locality = _PROPERTY_POSTAL_LOCALITY_STOPWORDS.sub("", str(match.group("locality") or "").strip()).strip(" ,.;:-")
        if not code or not locality:
            continue
        label = f"{code} {locality}"
        key = label.casefold()
        if key not in seen:
            names.append(label)
            seen.add(key)
    return tuple(names)


def _property_postal_codes_from_text(text: object, *, require_locality: bool = True) -> tuple[str, ...]:
    if require_locality:
        return tuple(name.split(" ", 1)[0] for name in _property_postal_names_from_text(text))
    normalized_text = str(text or "")
    codes: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\b[1-9]\d{3,4}\b", normalized_text):
        code = match.group(0)
        if code not in seen:
            codes.append(code)
            seen.add(code)
    return tuple(codes)


def _property_decoded_url_path(parsed: urllib.parse.SplitResult | urllib.parse.ParseResult) -> str:
    decoded_path = str(parsed.path or "")
    for _index in range(4):
        next_decoded_path = urllib.parse.unquote(decoded_path)
        if next_decoded_path == decoded_path:
            return decoded_path
        decoded_path = next_decoded_path
    # A path that is still changing after the bounded canonicalization pass is
    # deliberately invalid. The control marker makes every structural check
    # fail closed instead of accepting an arbitrarily nested encoded path.
    if urllib.parse.unquote(decoded_path) != decoded_path:
        return "\x00"
    return decoded_path


def _property_url_path_is_structurally_safe(path: object) -> bool:
    normalized_path = str(path or "")
    return not (
        "\\" in normalized_path
        or normalized_path.startswith("//")
        or any(segment in {".", ".."} for segment in normalized_path.split("/"))
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized_path)
    )


def _property_url_is_web_or_local(value: object) -> bool:
    url = str(value or "").strip()
    if (
        not url
        or len(url) > 2048
        or "\\" in url
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in url)
    ):
        return False
    if url.startswith("/"):
        if url.startswith("//"):
            return False
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        if (
            parsed.scheme
            or parsed.netloc
            or not _property_url_path_is_structurally_safe(
                _property_decoded_url_path(parsed)
            )
        ):
            return False
        return True
    return public_http_url_is_safe(url)


def _property_media_url_values(value: object, *, context: str = "", depth: int = 0) -> tuple[tuple[str, str], ...]:
    if depth > 4 or value in (None, ""):
        return ()
    rows: list[tuple[str, str]] = []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text[:1] in {"[", "{"}:
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if parsed is not None:
                return _property_media_url_values(parsed, context=context, depth=depth + 1)
        for match in _PROPERTY_MEDIA_URL_RE.finditer(text):
            url = urllib.parse.urldefrag(match.group(0).strip().rstrip(".,;"))[0]
            if _property_url_is_web_or_local(url):
                rows.append((url, context))
        if _property_url_is_web_or_local(text):
            url = urllib.parse.urldefrag(text.rstrip(".,;"))[0]
            rows.append((url, context))
        return tuple(dict.fromkeys(rows))
    if isinstance(value, dict):
        label_parts = [context]
        for key in ("label", "title", "name", "type", "kind", "role", "caption", "alt", "description"):
            raw_label = str(value.get(key) or "").strip()
            if raw_label:
                label_parts.append(raw_label)
        local_context = " ".join(part for part in label_parts if part)
        for key, raw in value.items():
            key_context = f"{local_context} {key}".strip()
            rows.extend(_property_media_url_values(raw, context=key_context, depth=depth + 1))
        return tuple(dict.fromkeys(rows))
    if isinstance(value, (list, tuple, set)):
        for item in value:
            rows.extend(_property_media_url_values(item, context=context, depth=depth + 1))
        return tuple(dict.fromkeys(rows))
    return ()


def _property_source_360_url_looks_usable(
    value: object,
    *,
    context: str = "",
    allow_panorama_asset: bool = False,
) -> bool:
    url = str(value or "").strip()
    if not _property_url_is_web_or_local(url):
        return False
    lowered_url = url.lower()
    if any(marker in lowered_url for marker in _PROPERTY_SOURCE_360_TRACKING_MARKERS):
        return False
    parsed = urllib.parse.urlparse(url)
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    path = _property_decoded_url_path(parsed).strip().lower()
    if parsed.scheme in {"http", "https"} and (not host or parsed.username or parsed.password):
        return False
    if not _property_url_path_is_structurally_safe(path):
        return False
    if path.startswith("/app/research"):
        return False
    if host == "propertyquarry.com" or host.endswith(".propertyquarry.com"):
        return False
    provider_host = any(
        host == provider_domain or host.endswith(f".{provider_domain}")
        for provider_domain in _PROPERTY_SOURCE_360_PROVIDER_HOSTS
    )
    path_has_360_marker = bool(_PROPERTY_SOURCE_360_PATH_TOKEN_RE.search(path))
    if not (provider_host or path_has_360_marker):
        return False
    is_media_asset = path.endswith(
        (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".pdf", ".svg")
    )
    if not parsed.scheme and (not allow_panorama_asset or not is_media_asset):
        return False
    if is_media_asset and not allow_panorama_asset:
        return False
    return True


def _property_floorplan_url_looks_usable(
    value: object,
    *,
    context: str = "",
    explicit: bool = False,
) -> bool:
    url = str(value or "").strip()
    if not _property_url_is_web_or_local(url):
        return False
    parsed = urllib.parse.urlparse(url)
    host = str(parsed.hostname or "").strip().lower()
    path = _property_decoded_url_path(parsed).strip().lower()
    lowered_url = url.lower()
    if parsed.scheme in {"http", "https"} and (not host or parsed.username or parsed.password):
        return False
    if not _property_url_path_is_structurally_safe(path):
        return False
    if any(marker in lowered_url for marker in _PROPERTY_FLOORPLAN_TRACKING_MARKERS):
        return False
    if path.startswith("/app/research") or (
        host in {"propertyquarry.com", "www.propertyquarry.com"}
        and path.startswith(("/app/", "/research/"))
    ):
        return False
    url_has_marker = any(marker in path for marker in _PROPERTY_FLOORPLAN_MARKERS)
    is_media_asset = path.endswith(_PROPERTY_FLOORPLAN_ASSET_EXTENSIONS)
    if explicit:
        return url_has_marker or is_media_asset
    context_has_marker = any(
        marker in str(context or "").strip().lower()
        for marker in _PROPERTY_FLOORPLAN_MARKERS
    )
    return url_has_marker or (context_has_marker and is_media_asset)


def _property_candidate_is_rankable(candidate: dict[str, object]) -> bool:
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
    status_fields = (
        "status",
        "review_status",
        "candidate_status",
        "filter_status",
        "repair_status",
    )
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
    for field in status_fields:
        if str(candidate.get(field) or "").strip().lower() in blocked_statuses:
            return False
    blocked_flags = (
        "maybe_false",
        "maybe_false_positive",
        "false_positive",
        "flagged_for_repair",
        "repair_only",
        "filtered_out",
        "hard_filtered",
        "not_a_listing",
    )
    for flag in blocked_flags:
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


def _property_candidate_display_facts(candidate: dict[str, object]) -> dict[str, object]:
    top_level_facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
    if isinstance(candidate.get("property_facts_json"), dict):
        top_level_facts = {**top_level_facts, **dict(candidate.get("property_facts_json") or {})}
    snapshot = dict(top_level_facts.get("listing_research_snapshot") or {}) if isinstance(top_level_facts.get("listing_research_snapshot"), dict) else {}
    merged = {**top_level_facts, **snapshot}

    for target_key, candidate_keys in (
        ("description", ("description_text", "description", "listing_description", "object_description", "summary")),
        ("location_description", ("location_text", "location_description", "micro_location_summary", "neighborhood_description")),
    ):
        if str(merged.get(target_key) or "").strip():
            continue
        for source_key in candidate_keys:
            fallback_value = str(candidate.get(source_key) or "").strip()
            if not fallback_value:
                continue
            merged[target_key] = fallback_value
            break

    def _normalized(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

    def _source_token(*keys: str) -> str:
        for key in keys:
            value = str(merged.get(key) or top_level_facts.get(key) or "").strip().lower()
            if value:
                return value
        return ""

    def _source_is_provisional(source: str) -> bool:
        normalized_source = str(source or "").strip().lower()
        if not normalized_source:
            return False
        return any(
            token in normalized_source
            for token in (
                "estimate",
                "estimated",
                "fallback",
                "inferred",
                "manual",
                "placeholder",
                "provisional",
                "synthetic",
            )
        )

    def _field_confirmation_allowed(field: str, *, source: str) -> bool:
        if _source_is_provisional(source):
            return False
        return not _source_is_provisional(
            _source_token(
                f"{field}_source",
                f"{field}_display_source",
                f"{field}_evidence_source",
                f"{field}_confirmation_source",
                f"{field}_status",
                f"{field}_verification_status",
            )
        )

    source_scope_location = str(top_level_facts.get("source_scope_location") or merged.get("source_scope_location") or "").strip()
    source_city = str(top_level_facts.get("source_city") or merged.get("source_city") or "").strip()
    source_postal_code = str(top_level_facts.get("source_postal_code") or merged.get("source_postal_code") or "").strip()
    source_scope_candidates = {
        _normalized(source_scope_location),
        _normalized(source_city),
    }
    if source_postal_code and source_city:
        source_scope_candidates.add(_normalized(f"{source_postal_code} {source_city}"))
    source_scope_candidates.discard("")

    listing_text = " ".join(
        part
        for part in (
            str(candidate.get("title") or "").strip(),
            str(candidate.get("listing_title") or "").strip(),
            str(candidate.get("summary") or "").strip(),
        )
        if part
    )
    direct_fact_sources: dict[str, str] = {}
    listing_postal_name = next(iter(_property_postal_names_from_text(listing_text)), "")
    listing_postal_code = listing_postal_name.split(" ", 1)[0] if listing_postal_name else ""
    if listing_postal_name and (
        not str(merged.get("postal_name") or "").strip()
        or _normalized(merged.get("postal_name")) in source_scope_candidates
        or (source_postal_code and listing_postal_code and source_postal_code != listing_postal_code)
    ):
        merged["postal_name"] = listing_postal_name
        for key in ("district", "location", "address", "city"):
            current = str(merged.get(key) or "").strip()
            if not current or _normalized(current) in source_scope_candidates or (source_postal_code and listing_postal_code and source_postal_code != listing_postal_code):
                merged[key] = listing_postal_name
        direct_fact_sources["location"] = "listing_text"

    if not any(
        merged.get(key) not in (None, "", 0, 0.0)
        for key in ("area_sqm", "area_m2", "living_area_m2", "living_area_sqm")
    ) and listing_text:
        try:
            from app.product.service import _property_extract_area_value

            listing_area_sqm = _property_extract_area_value(listing_text)
        except Exception:
            listing_area_sqm = None
        if isinstance(listing_area_sqm, float) and listing_area_sqm > 0.0:
            merged["area_m2"] = listing_area_sqm
            merged["area_source"] = "title_fallback"

    if not str(merged.get("price_display") or "").strip():
        fallback_price = ""
        price_source = ""
        for raw_value in (
            merged.get("rent_display"),
            merged.get("purchase_price_display"),
            merged.get("buy_price_display"),
            merged.get("price"),
            merged.get("rent"),
        ):
            fallback_price = str(raw_value or "").strip()
            if fallback_price:
                price_source = "provider_structured_fact"
                break
        if not fallback_price:
            for key in (
                "price_eur",
                "purchase_price_eur",
                "buy_price_eur",
                "rent_eur",
                "total_rent_eur",
                "monthly_rent_eur",
            ):
                raw_value = merged.get(key)
                try:
                    amount = float(raw_value)
                except Exception:
                    amount = 0.0
                if amount > 0:
                    currency_code = str(merged.get("currency_code") or "EUR").strip().upper() or "EUR"
                    fallback_price = f"{currency_code} {amount:,.0f}"
                    price_source = "provider_numeric_fact"
                    break
        if not fallback_price and listing_text:
            currency_pattern = "|".join(re.escape(code) for code in supported_currency_codes())
            for pattern in (
                r"(€\s?[0-9][0-9\.\s]*(?:,\d{1,2})?\s*,-?)",
                rf"((?:{currency_pattern})\s?[0-9][0-9\.,\s]*)",
            ):
                match = re.search(pattern, listing_text, flags=re.IGNORECASE)
                if match:
                    fallback_price = " ".join(str(match.group(1) or "").split()).strip(" ,")
                    price_source = "listing_text"
                    break
        if fallback_price:
            merged["price_display"] = fallback_price
            if _field_confirmation_allowed("price", source=price_source or "provider_fact"):
                direct_fact_sources["price"] = price_source or "provider_fact"
    elif any(str(merged.get(key) or "").strip() for key in ("price_display", "rent_display", "purchase_price_display", "buy_price_display")):
        if _field_confirmation_allowed("price", source="provider_structured_fact"):
            direct_fact_sources["price"] = "provider_structured_fact"
    elif any(merged.get(key) not in (None, "", 0, 0.0) for key in ("price_eur", "purchase_price_eur", "buy_price_eur", "rent_eur", "total_rent_eur", "monthly_rent_eur")):
        if _field_confirmation_allowed("price", source="provider_numeric_fact"):
            direct_fact_sources["price"] = "provider_numeric_fact"

    if not direct_fact_sources.get("area") and _field_confirmation_allowed("area", source="provider_structured_fact") and any(merged.get(key) not in (None, "", 0, 0.0) for key in ("area_sqm", "area_m2", "living_area_m2", "living_area_sqm")):
        direct_fact_sources["area"] = "provider_structured_fact"
    if _field_confirmation_allowed("rooms", source="provider_structured_fact") and any(merged.get(key) not in (None, "", 0, 0.0) for key in ("rooms", "room_count")):
        direct_fact_sources["rooms"] = "provider_structured_fact"
    if not direct_fact_sources.get("location") and _field_confirmation_allowed("location", source="provider_structured_fact") and any(str(merged.get(key) or "").strip() for key in ("exact_address", "street_address", "address", "postal_name", "district", "city")):
        direct_fact_sources["location"] = "provider_structured_fact"

    if direct_fact_sources:
        confirmed_fields = sorted(direct_fact_sources)
        merged["listing_fact_confirmation"] = {
            "status": "confirmed",
            "label": "Listing facts",
            "summary": f"{len(confirmed_fields)} listing fact{'s' if len(confirmed_fields) != 1 else ''} read automatically from the listing.",
            "fields": confirmed_fields,
            "sources": direct_fact_sources,
            "requires_manual_confirmation": False,
        }

    if not snapshot:
        return merged

    def _is_scope_placeholder(value: object) -> bool:
        normalized_value = _normalized(value)
        if not normalized_value:
            return False
        return normalized_value in source_scope_candidates

    for key in ("district", "location", "postal_name", "address", "street_address", "exact_address", "city"):
        snapshot_value = str(snapshot.get(key) or "").strip()
        top_value = str(top_level_facts.get(key) or "").strip()
        if snapshot_value and (not top_value or _is_scope_placeholder(top_value)):
            merged[key] = snapshot_value
    return merged


def _property_candidate_floorplan_url(
    candidate: dict[str, object],
    *,
    facts: dict[str, object] | None = None,
) -> str:
    resolved_facts = facts or _property_candidate_display_facts(candidate)
    sources: list[dict[str, object]] = [candidate, resolved_facts]
    snapshot = resolved_facts.get("listing_research_snapshot")
    if isinstance(snapshot, dict):
        sources.append(snapshot)
    for source in sources:
        for key in _PROPERTY_FLOORPLAN_URL_KEYS:
            for url, _context in _property_media_url_values(source.get(key), context=key):
                if _property_floorplan_url_looks_usable(url, explicit=True):
                    return url
        for key in _PROPERTY_FLOORPLAN_CONTAINER_KEYS:
            for url, context in _property_media_url_values(source.get(key), context=key):
                if _property_floorplan_url_looks_usable(url, context=context):
                    return url
    return ""


def _property_candidate_source_virtual_tour_url(
    candidate: dict[str, object],
    *,
    facts: dict[str, object] | None = None,
) -> str:
    resolved_facts = facts or _property_candidate_display_facts(candidate)
    sources: list[dict[str, object]] = [candidate, resolved_facts]
    snapshot = resolved_facts.get("listing_research_snapshot")
    if isinstance(snapshot, dict):
        sources.append(snapshot)
    for source in sources:
        for key in _PROPERTY_SOURCE_360_URL_KEYS:
            for url, context in _property_media_url_values(source.get(key), context=key):
                if _property_source_360_url_looks_usable(url, context=context):
                    return url
        for key in _PROPERTY_SOURCE_360_CONTAINER_KEYS:
            for url, context in _property_media_url_values(source.get(key), context=key):
                if _property_source_360_url_looks_usable(
                    url,
                    context=context,
                    allow_panorama_asset=True,
                ):
                    return url
        for url, context in _property_media_url_values(source.get("tour_url"), context="tour_url"):
            if _property_source_360_url_looks_usable(url, context=context):
                return url
    return ""


def _property_candidate_maps_url(candidate: dict[str, object]) -> str:
    facts = _property_candidate_display_facts(candidate)

    def _text(*values: object) -> str:
        return next((str(value or "").strip() for value in values if str(value or "").strip()), "")

    lat = _text(facts.get("map_lat"), facts.get("lat"), facts.get("latitude"))
    lng = _text(facts.get("map_lng"), facts.get("lng"), facts.get("lon"), facts.get("longitude"))
    if lat and lng:
        return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(f'{lat},{lng}', safe=',')}"
    address_lines = " ".join(str(item or "").strip() for item in list(facts.get("address_lines") or []) if str(item or "").strip())
    query = _text(
        facts.get("exact_address"),
        facts.get("street_address"),
        facts.get("address"),
        address_lines,
        facts.get("postal_name"),
        facts.get("location"),
        candidate.get("title"),
    )
    if not query:
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(query)}"


def _property_search_worker_slots(run_summary: dict[str, object], *, plan_key: str) -> dict[str, object]:
    normalized_plan = str(plan_key or "free").strip().lower() or "free"
    slot_cap = {"free": 1, "plus": 2, "agent": 4}.get(normalized_plan, 1)
    provider_workers = dict(run_summary.get("provider_workers") or {}) if isinstance(run_summary.get("provider_workers"), dict) else {}
    configured_workers = max(0, int(provider_workers.get("worker_concurrency") or 0))
    source_rows = [dict(row) for row in list(run_summary.get("sources") or []) if isinstance(row, dict)]
    source_total = max(len(source_rows), int(run_summary.get("source_variant_total") or run_summary.get("sources_total") or 0))
    run_progress = max(0, min(100, int(run_summary.get("progress") or 0)))
    run_status = str(run_summary.get("status") or "").strip().lower()
    run_active = bool(run_progress > 0 or run_status in {"queued", "starting", "in_progress", "running", "processing", "scanning"})

    def _source_provider_group(source_row: dict[str, object]) -> str:
        for raw_key in (
            "provider_source_key",
            "source_provider_key",
            "provider_key",
            "platform",
            "provider_group",
            "provider_channel",
            "provider_family",
        ):
            value = str(source_row.get(raw_key) or "").strip().lower()
            if value:
                return value
        label = str(source_row.get("source_label") or source_row.get("label") or "").strip()
        if "|" in label:
            label = label.split("|", 1)[0].strip()
        return label.casefold() or "provider"

    def _source_progress(source_row: dict[str, object]) -> int:
        raw_status = str(source_row.get("status") or source_row.get("state") or "").strip().lower()
        repair_tasks = [dict(row) for row in list(source_row.get("provider_repair_tasks") or []) if isinstance(row, dict)]
        repair_status = str((repair_tasks[0] if repair_tasks else {}).get("status") or source_row.get("repair_status") or "").strip().lower()
        if raw_status in {"completed", "processed", "done", "success"}:
            return 100
        if raw_status == "repaired" or repair_status == "returned":
            return 100
        if raw_status == "repairing" or repair_status in {"pending", "assigned"}:
            return 72
        if raw_status in {"failed", "error", "skipped"} or source_row.get("error"):
            return 100
        try:
            explicit = int(float(str(source_row.get("progress") or "").strip()))
        except Exception:
            explicit = 0
        if explicit > 0:
            return max(0, min(explicit, 100))
        if raw_status == "warming":
            return 42
        if raw_status == "starting":
            return 26
        if raw_status in {"running", "processing", "in_progress", "working"}:
            return 58
        if raw_status in {"queued", "pending"}:
            return 18
        return 10

    def _source_status_label(source_row: dict[str, object]) -> str:
        raw_status = str(source_row.get("status") or source_row.get("state") or "").strip().lower()
        repair_tasks = [dict(row) for row in list(source_row.get("provider_repair_tasks") or []) if isinstance(row, dict)]
        repair_status = str((repair_tasks[0] if repair_tasks else {}).get("status") or source_row.get("repair_status") or "").strip().lower()
        if raw_status == "repaired" or repair_status == "returned":
            return "Back online"
        if raw_status == "repairing" or repair_status in {"pending", "assigned"}:
            return "Checking again"
        if raw_status in {"completed", "processed", "done", "success"}:
            return "Done"
        if raw_status in {"failed", "error"} or source_row.get("error"):
            return "Fetch failed"
        if raw_status == "warming":
            return "Preparing"
        if raw_status == "starting":
            return "Starting"
        if raw_status in {"running", "processing", "in_progress", "working"}:
            return "Running"
        if raw_status in {"queued", "pending"}:
            return "Up next"
        return "Waiting"

    running_sources = [
        row for row in source_rows
        if str(row.get("status") or row.get("state") or "").strip().lower() in {"running", "processing", "in_progress", "working", "warming", "starting", "repairing"}
    ]
    queued_sources = [
        row for row in source_rows
        if str(row.get("status") or row.get("state") or "").strip().lower() in {"queued", "pending"}
    ]
    completed_sources = [
        row for row in source_rows
        if str(row.get("status") or row.get("state") or "").strip().lower() in {"completed", "processed", "done", "success", "repaired"}
    ]
    failed_sources = [
        row for row in source_rows
        if str(row.get("status") or row.get("state") or "").strip().lower() in {"failed", "error", "skipped"} or row.get("error")
    ]
    queue = running_sources + queued_sources + failed_sources + completed_sources

    diversified_queue: list[dict[str, object]] = []
    seen_groups: set[str] = set()
    duplicate_counts: dict[str, int] = {}
    for source_row in queue:
        group_key = _source_provider_group(source_row)
        duplicate_counts[group_key] = duplicate_counts.get(group_key, 0) + 1
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        diversified_queue.append(source_row)
    for source_row in queue:
        group_key = _source_provider_group(source_row)
        if any(_source_provider_group(existing) == group_key for existing in diversified_queue):
            if source_row in diversified_queue:
                continue
        diversified_queue.append(source_row)
    queue = diversified_queue
    effective_worker_cap = max(1, min(4, max(configured_workers or slot_cap, len(queue) if run_active else 0)))

    actual_visible_workers = min(effective_worker_cap, len(queue))
    visible_workers = actual_visible_workers if actual_visible_workers > 0 else (1 if run_active else 0)
    active_provider_total = len(running_sources)
    checked_provider_total = len(completed_sources) + len(failed_sources)
    queued_provider_total = len(queued_sources)
    remaining_provider_total = max(0, source_total - active_provider_total - queued_provider_total - checked_provider_total)
    queued_provider_total += remaining_provider_total

    worker_rows: list[dict[str, object]] = []
    for index in range(visible_workers):
        source_row = queue[index] if index < len(queue) else {}
        source_label = str(source_row.get("source_label") or source_row.get("label") or "").strip()
        compact_label = _compact_provider_label(source_label)
        provider_group = _source_provider_group(source_row) if source_row else ""
        shard_count = max(0, int(duplicate_counts.get(provider_group, 0)) - 1) if provider_group else 0
        status_label = _source_status_label(source_row) if source_row else ("Starting" if run_active else "Idle")
        progress = _source_progress(source_row) if source_row else (max(8, min(run_progress, 24)) if status_label == "Starting" else 0)
        worker_rows.append(
            {
                "label": compact_label if source_row else ("Preparing search" if status_label == "Starting" else ("Waiting" if active_sources or source_rows else "Ready")),
                "provider": source_label or ("Preparing selected searches" if status_label == "Starting" else ("Waiting for selected searches" if active_sources or source_rows else "Ready when you start")),
                "shard_count": shard_count,
                "status_label": status_label,
                "progress_pct": progress,
                "tone": "done" if progress >= 100 and source_row and status_label in {"Done", "Back online"} else ("active" if status_label in {"Running", "Starting", "Preparing", "Checking again"} else ("queued" if status_label in {"Up next"} else "idle")),
            }
        )

    live_worker_total = sum(
        1
        for row in worker_rows
        if str(row.get("status_label") or "") in {"Running", "Starting", "Preparing", "Checking again"}
    )
    queued_worker_total = sum(1 for row in worker_rows if str(row.get("status_label") or "") == "Up next")
    display_active_total = active_provider_total
    display_includes_queued_lanes = run_active and live_worker_total <= 1 and queued_worker_total > 0
    if display_includes_queued_lanes:
        display_active_total = min(visible_workers, live_worker_total + queued_worker_total)

    if display_active_total > 0:
        headline = f"{display_active_total} list{'s' if display_active_total != 1 else ''} active"
    elif run_active and not source_rows:
        headline = "Preparing lists"
    elif queued_provider_total > 0:
        headline = "Preparing lists"
    elif checked_provider_total > 0:
        headline = f"{checked_provider_total} list{'s' if checked_provider_total != 1 else ''} checked"
    else:
        headline = "Lists ready"

    detail_parts: list[str] = []
    if display_includes_queued_lanes and live_worker_total > 0:
        detail_parts.append(f"{live_worker_total} live")
    if queued_provider_total > 0:
        detail_parts.append(f"{queued_provider_total} queued")
    if checked_provider_total > 0 and display_active_total > 0:
        detail_parts.append(f"{checked_provider_total} checked")
    detail = " · ".join(detail_parts)

    return {
        "plan_key": normalized_plan,
        "visible_workers": visible_workers,
        "slot_cap": slot_cap,
        "configured_workers": configured_workers,
        "headline": headline,
        "detail": detail,
        "workers": worker_rows,
        "upgrade_copy": "",
        "tooltip": "This shows which lists are running, queued, restored, or unavailable for this search. Other saved searches keep their own progress.",
    }


def _property_run_reliability_summary(
    run: dict[str, object],
    *,
    results_total: int = 0,
) -> dict[str, object]:
    return build_property_run_reliability_snapshot(run, results_total=results_total)


def _compact_provider_label(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return "Provider"
    for marker in ("|", "·", " — ", " – ", ":", "("):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    words = [part for part in text.split() if part]
    if len(words) > 3:
        text = " ".join(words[:3]).strip()
    if len(text) > 20 and len(words) >= 2:
        text = " ".join(words[:2]).strip()
    if len(text) > 20:
        text = f"{text[:17].rstrip()}..."
    return text or "Provider"


def _property_candidate_directions_url(
    candidate: dict[str, object],
    *,
    target_lat: object = "",
    target_lng: object = "",
    target_query: object = "",
    mode: str = "walking",
) -> str:
    facts = _property_candidate_display_facts(candidate)

    def _text(*values: object) -> str:
        return next((str(value or "").strip() for value in values if str(value or "").strip()), "")

    origin_lat = _text(facts.get("map_lat"), facts.get("lat"), facts.get("latitude"))
    origin_lng = _text(facts.get("map_lng"), facts.get("lng"), facts.get("lon"), facts.get("longitude"))
    address_lines = " ".join(str(item or "").strip() for item in list(facts.get("address_lines") or []) if str(item or "").strip())
    origin = (
        f"{origin_lat},{origin_lng}"
        if origin_lat and origin_lng
        else _text(facts.get("exact_address"), facts.get("street_address"), address_lines, facts.get("postal_name"), candidate.get("title"))
    )
    destination = f"{target_lat},{target_lng}" if _text(target_lat) and _text(target_lng) else _text(target_query)
    if not origin or not destination:
        return ""
    travel_mode = mode if mode in {"walking", "transit", "driving", "bicycling"} else "walking"
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={urllib.parse.quote(origin, safe=',')}"
        f"&destination={urllib.parse.quote(destination, safe=',')}"
        f"&travelmode={urllib.parse.quote(travel_mode)}"
    )


def _property_family_filters_active(preferences: dict[str, object]) -> bool:
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


def _property_candidate_route_evidence(
    candidate: dict[str, object],
    property_preferences: dict[str, object] | None = None,
) -> list[dict[str, str]]:
    facts = _property_candidate_display_facts(candidate)

    family_filters_active = _property_family_filters_active(property_preferences or {})
    specs = (
        ("BOOK", "School", "nearest_school_m", "nearest_school_name", "nearest_school_lat", "nearest_school_lng", "transit", True),
        ("CART", "Supermarket", "nearest_supermarket_m", "nearest_supermarket_name", "nearest_supermarket_lat", "nearest_supermarket_lng", "walking", False),
        ("PLAY", "Playground", "nearest_playground_m", "nearest_playground_name", "nearest_playground_lat", "nearest_playground_lng", "walking", True),
        ("RX", "Pharmacy", "nearest_pharmacy_m", "nearest_pharmacy_name", "nearest_pharmacy_lat", "nearest_pharmacy_lng", "walking", False),
        ("U", "Transit", "nearest_subway_m", "nearest_subway_name", "nearest_subway_lat", "nearest_subway_lng", "transit", False),
    )
    rows: list[dict[str, str]] = []
    for icon, label, distance_key, name_key, lat_key, lng_key, mode, family_only in specs:
        if family_only and not family_filters_active:
            continue
        raw_distance = facts.get(distance_key)
        if raw_distance in (None, "", []):
            continue
        try:
            meters = int(float(raw_distance))
        except Exception:
            continue
        raw_place_name = str(facts.get(name_key) or "").strip()
        place_name = raw_place_name or f"Nearest {label.lower()}"
        row = {
            "icon": icon,
            "label": label,
            "distance": f"{meters} m",
            "detail": place_name,
            "mode": mode,
            "map_url": _property_candidate_directions_url(
                candidate,
                target_lat=facts.get(lat_key),
                target_lng=facts.get(lng_key),
                target_query=place_name,
                mode=mode,
            ),
        }
        rows.append(row)
    return rows[:4]


def _property_route_distance_label(meters: int) -> str:
    if meters >= 1000:
        kilometers = meters / 1000.0
        if kilometers >= 10 or abs(round(kilometers) - kilometers) < 0.05:
            return f"{round(kilometers):.0f} km"
        return f"{kilometers:.1f} km"
    return f"{meters} m"


def _property_route_preview_float(value: object) -> float | None:
    try:
        return float(str(value or "").strip())
    except Exception:
        return None


def _property_route_preview_profile(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    return {
        "walking": "foot",
        "driving": "car",
        "bicycling": "bike",
    }.get(normalized, "")


@lru_cache(maxsize=192)
def _property_route_preview_geometry(
    *,
    origin_lat_key: int,
    origin_lng_key: int,
    target_lat_key: int,
    target_lng_key: int,
    mode: str,
) -> dict[str, object]:
    profile = _property_route_preview_profile(mode)
    if not profile:
        return {}
    origin_lat = origin_lat_key / 10000.0
    origin_lng = origin_lng_key / 10000.0
    target_lat = target_lat_key / 10000.0
    target_lng = target_lng_key / 10000.0
    if abs(origin_lat - target_lat) < 0.00001 and abs(origin_lng - target_lng) < 0.00001:
        return {}
    request = urllib.request.Request(
        "https://router.project-osrm.org/route/v1/"
        f"{urllib.parse.quote(profile)}/"
        f"{origin_lng:.5f},{origin_lat:.5f};{target_lng:.5f},{target_lat:.5f}"
        "?overview=full&geometries=geojson&steps=false",
        headers={"User-Agent": "PropertyQuarry/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=4.0) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return {}
    routes = list(payload.get("routes") or []) if isinstance(payload, dict) else []
    route = routes[0] if routes and isinstance(routes[0], dict) else {}
    geometry = dict(route.get("geometry") or {}) if isinstance(route.get("geometry"), dict) else {}
    coordinates = list(geometry.get("coordinates") or []) if isinstance(geometry.get("coordinates"), list) else []
    points: list[tuple[float, float]] = []
    for raw_point in coordinates:
        if not isinstance(raw_point, (list, tuple)) or len(raw_point) < 2:
            continue
        try:
            points.append((float(raw_point[0]), float(raw_point[1])))
        except (TypeError, ValueError):
            continue
    if len(points) < 2:
        return {}
    try:
        distance_m = int(round(float(route.get("distance") or 0.0)))
    except Exception:
        distance_m = 0
    try:
        duration_min = max(0, int(round(float(route.get("duration") or 0.0) / 60.0)))
    except Exception:
        duration_min = 0
    return {
        "points": tuple(points),
        "distance_m": distance_m,
        "duration_min": duration_min,
    }


def _property_route_preview_media(
    *,
    origin_lat: object,
    origin_lng: object,
    target_lat: object = "",
    target_lng: object = "",
    target_query: str = "",
    mode: str,
    label: str,
    title: str,
) -> dict[str, object]:
    origin_lat_value = _property_route_preview_float(origin_lat)
    origin_lng_value = _property_route_preview_float(origin_lng)
    target_lat_value = _property_route_preview_float(target_lat)
    target_lng_value = _property_route_preview_float(target_lng)
    if origin_lat_value is None or origin_lng_value is None:
        return {}
    if target_lat_value is None or target_lng_value is None:
        if not str(target_query or "").strip():
            return {}
        from app.api.routes import landing_view_models

        geocoded = landing_view_models._forward_geocode_preview_point(str(target_query or "").strip())
        if geocoded is None:
            return {}
        target_lat_value, target_lng_value = geocoded
    if target_lat_value is None or target_lng_value is None:
        return {}

    route = _property_route_preview_geometry(
        origin_lat_key=int(round(origin_lat_value * 10000.0)),
        origin_lng_key=int(round(origin_lng_value * 10000.0)),
        target_lat_key=int(round(target_lat_value * 10000.0)),
        target_lng_key=int(round(target_lng_value * 10000.0)),
        mode=mode,
    )
    points = [
        (float(point[0]), float(point[1]))
        for point in list(route.get("points") or [])
        if isinstance(point, (list, tuple)) and len(point) == 2
    ]
    if len(points) < 2:
        return {}

    from app.api.routes import landing_view_models

    west = min(float(point[0]) for point in points)
    south = min(float(point[1]) for point in points)
    east = max(float(point[0]) for point in points)
    north = max(float(point[1]) for point in points)
    fit_bounds = (west, south, east, north)
    render_bounds = landing_view_models._expand_geo_bounds(fit_bounds, padding_ratio=0.18)
    center_lon = (render_bounds[0] + render_bounds[2]) / 2.0
    center_lat = (render_bounds[1] + render_bounds[3]) / 2.0
    zoom = landing_view_models._preview_zoom_for_bounds(
        render_bounds,
        fit_bounds=fit_bounds,
        width=296,
        height=160,
        max_zoom=16,
    )
    preview_bounds = landing_view_models._tile_crop_geo_bounds(
        center_lat=center_lat,
        center_lon=center_lon,
        zoom=zoom,
        width=296,
        height=160,
    )
    line_path, _ = landing_view_models._project_lonlat_to_preview_polyline(
        points,
        preview_bounds,
        width=296.0,
        height=160.0,
    )
    compact_preview_bounds = landing_view_models._tile_crop_geo_bounds(
        center_lat=center_lat,
        center_lon=center_lon,
        zoom=zoom,
        width=144,
        height=68,
    )
    compact_preview_path, _ = landing_view_models._project_lonlat_to_preview_polyline(
        points,
        compact_preview_bounds,
        width=144.0,
        height=68.0,
    )
    if not line_path or not compact_preview_path:
        return {}
    title_label = str(title or label or "route").strip() or "route"
    mode_label = str(mode or "walking").strip().lower()
    image_url = landing_view_models._cached_preview_image_url(
        cache_key={
            "kind": "route-preview",
            "mode": mode_label,
            "label": str(label or "").strip(),
            "title": title_label,
            "origin_lat_key": int(round(origin_lat_value * 10000.0)),
            "origin_lng_key": int(round(origin_lng_value * 10000.0)),
            "target_lat_key": int(round(target_lat_value * 10000.0)),
            "target_lng_key": int(round(target_lng_value * 10000.0)),
            "zoom": zoom,
            "width": 296,
            "height": 160,
            "overlay_mode": "route_line_v1",
            "route_distance_m": int(route.get("distance_m") or 0),
            "route_duration_min": int(route.get("duration_min") or 0),
        },
        center_lat=center_lat,
        center_lon=center_lon,
        zoom=zoom,
        overlay_rows=[
            {
                "path": line_path,
                "path_kind": "line",
                "selected": True,
                "show_endpoint_markers": True,
                "stroke_width_px": 7,
                "halo_width_px": 13,
            }
        ],
        draw_overlay=True,
        width=296,
        height=160,
    )
    return {
        "preview_path": compact_preview_path,
        "preview_image_url": image_url,
        "preview_alt": f"{str(label or 'Route').strip()} route to {title_label}",
        "route_distance_m": int(route.get("distance_m") or 0),
        "route_duration_min": int(route.get("duration_min") or 0),
    }

def _property_progress_route_preview_rows(
    *,
    run_summary: dict[str, object],
    property_preferences: dict[str, object],
) -> list[dict[str, str]]:
    candidate, _is_best_so_far = _property_progress_primary_candidate(run_summary)
    if not candidate:
        return []
    facts = _property_candidate_display_facts(candidate)

    origin_lat = facts.get("map_lat") or facts.get("lat") or facts.get("latitude")
    origin_lng = facts.get("map_lng") or facts.get("lng") or facts.get("lon") or facts.get("longitude")
    rows: list[dict[str, str]] = []
    family_filters_active = _property_family_filters_active(property_preferences)

    commute_destination = str(property_preferences.get("commute_destination") or "").strip()
    if bool(property_preferences.get("enable_commute_research")) and commute_destination:
        commute_specs = (
            ("transit", "Transit", int(property_preferences.get("max_commute_minutes_transit") or 0)),
            ("driving", "Car", int(property_preferences.get("max_commute_minutes_drive") or 0)),
            ("bicycling", "Bike", int(property_preferences.get("max_commute_minutes_bike") or 0)),
            ("walking", "Foot", int(property_preferences.get("max_commute_minutes_walk") or 0)),
        )
        selected_mode, mode_label, mode_minutes = next(
            ((mode, label, minutes) for mode, label, minutes in commute_specs if minutes > 0),
            ("transit", "Transit", 0),
        )
        route_preview = _property_route_preview_media(
            origin_lat=origin_lat,
            origin_lng=origin_lng,
            target_query=commute_destination,
            mode=selected_mode,
            label="Your route",
            title=commute_destination,
        )
        detail = (
            f"{mode_label} <= {mode_minutes} min"
            if mode_minutes > 0
            else f"{mode_label} route from the property"
        )
        mode_display = mode_label
        if int(route_preview.get("route_distance_m") or 0) > 0:
            detail = f"{_property_route_distance_label(int(route_preview.get('route_distance_m') or 0))} route"
        if int(route_preview.get("route_duration_min") or 0) > 0:
            mode_display = f"{mode_label} {int(route_preview.get('route_duration_min') or 0)} min"
        row = {
            "title": commute_destination,
            "label": "Your route",
            "detail": detail,
            "mode_label": mode_display,
            "map_url": _property_candidate_directions_url(
                candidate,
                target_query=commute_destination,
                mode=selected_mode,
            ),
        }
        for key in ("preview_path", "preview_image_url", "preview_alt"):
            if route_preview.get(key) not in (None, "", [], {}):
                row[key] = route_preview.get(key)
        rows.append(row)

    route_specs = (
        ("School", "nearest_school_m", "nearest_school_name", "nearest_school_lat", "nearest_school_lng", "transit", True),
        ("Supermarket", "nearest_supermarket_m", "nearest_supermarket_name", "nearest_supermarket_lat", "nearest_supermarket_lng", "walking", False),
        ("Playground", "nearest_playground_m", "nearest_playground_name", "nearest_playground_lat", "nearest_playground_lng", "walking", True),
        ("Pharmacy", "nearest_pharmacy_m", "nearest_pharmacy_name", "nearest_pharmacy_lat", "nearest_pharmacy_lng", "walking", False),
        ("Underground", "nearest_subway_m", "nearest_subway_name", "nearest_subway_lat", "nearest_subway_lng", "transit", False),
    )
    for label, distance_key, name_key, lat_key, lng_key, mode, family_only in route_specs:
        if family_only and not family_filters_active:
            continue
        raw_distance = facts.get(distance_key)
        if raw_distance in (None, "", []):
            continue
        try:
            meters = int(float(raw_distance))
        except Exception:
            continue
        raw_place_name = str(facts.get(name_key) or "").strip()
        place_name = raw_place_name or f"Nearest {label.lower()}"
        route_preview = _property_route_preview_media(
            origin_lat=origin_lat,
            origin_lng=origin_lng,
            target_lat=facts.get(lat_key),
            target_lng=facts.get(lng_key),
            target_query=place_name,
            mode=mode,
            label=label,
            title=place_name,
        )
        detail = f"{meters} m from the property"
        if int(route_preview.get("route_distance_m") or 0) > 0:
            distance_label = _property_route_distance_label(int(route_preview.get("route_distance_m") or 0))
            detail = f"{distance_label} on foot" if mode == "walking" else f"{distance_label} route"
        mode_display = "Transit" if mode == "transit" else "Walk"
        if int(route_preview.get("route_duration_min") or 0) > 0:
            mode_display = (
                f"Walk {int(route_preview.get('route_duration_min') or 0)} min"
                if mode == "walking"
                else f"{mode_display} {int(route_preview.get('route_duration_min') or 0)} min"
            )
        row = {
            "title": place_name,
            "label": label,
            "detail": detail,
            "mode_label": mode_display,
            "map_url": _property_candidate_directions_url(
                candidate,
                target_lat=facts.get(lat_key),
                target_lng=facts.get(lng_key),
                target_query=place_name,
                mode=mode,
            ),
        }
        for key in ("preview_path", "preview_image_url", "preview_alt"):
            if route_preview.get(key) not in (None, "", [], {}):
                row[key] = route_preview.get(key)
        rows.append(row)
        if len(rows) >= 3:
            break
    return rows[:3]


def _property_progress_primary_candidate(run_summary: dict[str, object]) -> tuple[dict[str, object], bool]:
    ranked_candidates = [
        dict(row)
        for row in list(run_summary.get("ranked_candidates") or [])
        if isinstance(row, dict) and _property_candidate_is_rankable(row)
    ]
    if ranked_candidates:
        return ranked_candidates[0], True

    collected: list[dict[str, object]] = []
    for source in [dict(row) for row in list(run_summary.get("sources") or []) if isinstance(row, dict)]:
        source_label = str(source.get("source_label") or source.get("label") or source.get("platform") or "").strip()
        for candidate in [dict(row) for row in list(source.get("top_candidates") or []) if isinstance(row, dict)]:
            if not _property_candidate_is_rankable(candidate):
                continue
            candidate.setdefault("source_label", source_label)
            collected.append(candidate)
    collected.sort(key=lambda item: float(item.get("ranking_score") or item.get("fit_score") or 0.0), reverse=True)
    if collected:
        return collected[0], False
    return {}, False


def _property_progress_current_property_card(
    *,
    run_summary: dict[str, object],
    run_id: str = "",
) -> dict[str, object]:
    candidate, is_best_so_far = _property_progress_primary_candidate(run_summary)
    if not candidate:
        return {}
    facts = _property_candidate_display_facts(candidate)
    title = sanitize_property_marketing_copy(
        str(candidate.get("title") or candidate.get("property_url") or "Property").strip()
    ) or "Property"
    location_label = _first_fact_text(
        facts,
        "postal_name",
        "district",
        "city",
        "exact_address",
        "street_address",
        "address",
    )
    layout_display = str(candidate.get("layout_display") or "").strip()
    if not layout_display:
        area_value = facts.get("area_m2") or facts.get("area_sqm")
        rooms_value = facts.get("rooms")
        layout_parts = [
            f"{rooms_value} rooms" if str(rooms_value or "").strip() else "",
            f"{area_value} m2" if str(area_value or "").strip() else "",
        ]
        layout_display = " | ".join(part for part in layout_parts if part)
    detail = summarize_property_description_copy(
        str(candidate.get("compare_reason") or candidate.get("fit_summary") or candidate.get("summary") or "").strip()
    )
    if not detail:
        detail = location_label or layout_display or str(candidate.get("price_display") or "").strip()
    map_url = (
        str(candidate.get("map_url") or "").strip()
        or _property_candidate_maps_url(candidate)
    )
    detail_url = str(
        candidate.get("packet_url") or candidate.get("review_url") or ""
    ).strip()
    if not (
        detail_url.startswith("/app/research/")
        and not detail_url.startswith("//")
    ):
        candidate_ref = str(
            candidate.get("candidate_ref")
            or candidate.get("listing_id")
            or candidate.get("external_id")
            or candidate.get("source_ref")
            or ""
        ).strip()
        detail_url = (
            f"/app/research/{urllib.parse.quote(candidate_ref, safe='')}"
            if candidate_ref
            else ""
        )
        normalized_run_id = str(run_id or "").strip()
        if detail_url and normalized_run_id:
            detail_url += (
                "?run_id="
                + urllib.parse.quote(normalized_run_id, safe="")
            )
    orientation_preview = _property_progress_map_preview(
        candidate,
        facts=facts,
        fallback_map_url=map_url,
        fallback_label=location_label or title,
    )
    card = {
        "status_label": "Best so far" if is_best_so_far else "Current property",
        "status_detail": detail,
        "title": title,
        "source_label": _compact_provider_label(candidate.get("source_label") or candidate.get("source_platform") or ""),
        "location_label": location_label,
        "price_display": str(candidate.get("price_display") or facts.get("price_display") or facts.get("rent_display") or "").strip(),
        "layout_display": layout_display,
        "map_url": map_url,
        "detail_url": detail_url,
    }
    diorama_scene = (
        dict(candidate.get("diorama_scene") or {})
        if isinstance(candidate.get("diorama_scene"), dict)
        else {}
    )
    diorama_preview_url = str(
        candidate.get("diorama_preview_url")
        or diorama_scene.get("image_url")
        or diorama_scene.get("preview_image_url")
        or ""
    ).strip()
    if diorama_preview_url:
        card["diorama_preview_url"] = diorama_preview_url
        card["diorama_alt"] = str(
            candidate.get("diorama_alt")
            or diorama_scene.get("alt")
            or f"Illustrative diorama of {title}"
        ).strip()
        card["diorama_representation"] = str(
            candidate.get("diorama_representation")
            or diorama_scene.get("representation")
            or ""
        ).strip()
        if diorama_scene:
            card["diorama_scene"] = diorama_scene
    else:
        card["diorama_status"] = "preparing"
    if orientation_preview:
        card["orientation_preview"] = orientation_preview
    return {
        key: value
        for key, value in card.items()
        if value not in (None, "", [], {})
    }


def _property_progress_map_preview(
    candidate: dict[str, object],
    *,
    facts: dict[str, object],
    fallback_map_url: str,
    fallback_label: str,
) -> dict[str, object]:
    existing = dict(candidate.get("orientation_preview") or {}) if isinstance(candidate.get("orientation_preview"), dict) else {}
    if str(existing.get("image_url") or existing.get("thumb_image_url") or "").strip():
        return existing
    try:
        lat = float(facts.get("map_lat") or facts.get("lat") or facts.get("latitude") or 0.0)
    except Exception:
        lat = 0.0
    try:
        lng = float(facts.get("map_lng") or facts.get("lng") or facts.get("lon") or facts.get("longitude") or 0.0)
    except Exception:
        lng = 0.0
    if not (lat or lng):
        return {}
    from app.api.routes import landing_view_models

    image_url = landing_view_models._cached_preview_image_url(
        cache_key={
            "kind": "current-property-progress-point",
            "label": fallback_label,
            "lat_key": int(round(lat * 10000.0)),
            "lon_key": int(round(lng * 10000.0)),
            "zoom": 15,
            "overlay_mode": "pin_v1",
        },
        center_lat=lat,
        center_lon=lng,
        zoom=15,
        pin=(320.0, 184.0),
        draw_overlay=False,
        materialize="async",
    )
    return {
        "image_url": image_url,
        "thumb_image_url": image_url,
        "alt": f"Map around {fallback_label or 'the property'}",
        "map_url": fallback_map_url,
    }


def _property_candidate_preview_image(candidate: dict[str, object]) -> str:
    facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
    for key in (
        "preview_image_url",
        "thumbnail_url",
        "image_url",
        "hero_image_url",
    ):
        value = str(candidate.get(key) or facts.get(key) or "").strip()
        if value.startswith(("https://", "/")) and "diorama-preview" not in value and "telegram-preview" not in value:
            return value
    for key in ("media_urls_json", "photo_urls_json", "image_urls_json"):
        values = facts.get(key) or candidate.get(key)
        if isinstance(values, (list, tuple)):
            for value in values:
                normalized = str(value or "").strip()
                if normalized.startswith(("https://", "/")):
                    return normalized
    orientation_preview = _property_candidate_orientation_preview(candidate)
    for key in ("thumb_image_url", "image_url"):
        normalized = str(orientation_preview.get(key) or "").strip()
        if normalized:
            return normalized
    return ""


def _property_candidate_orientation_preview(candidate: dict[str, object]) -> dict[str, object]:
    from app.api.routes import landing_view_models

    facts = _property_candidate_display_facts(candidate)
    title = str(candidate.get("title") or "").strip()
    summary = str(candidate.get("summary") or "").strip()
    combined_text = " | ".join(part for part in (title, summary) if part)
    if not any(str(facts.get(key) or "").strip() for key in ("postal_name", "district", "city", "address", "street_address", "exact_address")):
        postal_name = next(iter(_property_postal_names_from_text(combined_text)), "")
        if postal_name:
            facts["postal_name"] = postal_name
            facts.setdefault("address", postal_name)
    label = str(
        facts.get("district")
        or facts.get("postal_name")
        or facts.get("city")
        or facts.get("address")
        or "Wider area"
    ).strip() or "Wider area"
    context_label = str(
        facts.get("postal_name")
        or facts.get("city")
        or facts.get("state")
        or facts.get("region")
        or label
    ).strip() or label
    country_code = str(
        facts.get("country_code")
        or facts.get("country")
        or candidate.get("country_code")
        or candidate.get("country")
        or ""
    ).strip()
    region_code = str(
        facts.get("city")
        or facts.get("postal_name")
        or facts.get("state")
        or facts.get("region")
        or ""
    ).strip().lower().replace(" ", "_")
    try:
        lat = float(facts.get("map_lat") or facts.get("lat") or 0.0)
    except Exception:
        lat = 0.0
    try:
        lng = float(facts.get("map_lng") or facts.get("lng") or 0.0)
    except Exception:
        lng = 0.0
    map_url = str(candidate.get("map_url") or "").strip() or _property_candidate_maps_url(candidate)
    if not (lat or lng):
        geocoded = landing_view_models._forward_geocode_preview_point(label)
        if geocoded:
            lat, lng = geocoded
    selected_labels: list[str] = []
    for raw_value in (facts.get("district"), facts.get("postal_name"), facts.get("city"), facts.get("address")):
        value = str(raw_value or "").strip()
        if value and value.casefold() not in {item.casefold() for item in selected_labels}:
            selected_labels.append(value)
    option_lookup = {item.casefold(): item for item in selected_labels}
    boundary_preview = landing_view_models._build_scope_boundary_preview(
        country_code=country_code.upper(),
        region_code=region_code,
        normalized_query=context_label,
        selected_labels=selected_labels[:1] or [label],
        selected_values=selected_labels[:1] or [label],
        option_lookup=option_lookup,
        market_label=context_label or label,
    )
    thumb_image_url = ""
    if lat or lng:
        thumb_image_url = landing_view_models._cached_preview_image_url(
            cache_key={
                "kind": "candidate-point",
                "country": country_code.upper(),
                "region": region_code,
                "query": context_label or label,
                "lat_key": int(round(lat * 10000.0)),
                "lon_key": int(round(lng * 10000.0)),
                "zoom": 15,
                "overlay_mode": "pin_v1",
            },
            center_lat=lat,
            center_lon=lng,
            zoom=15,
            pin=(320.0, 184.0),
            draw_overlay=False,
        )
    if thumb_image_url:
        image_url = thumb_image_url
    elif boundary_preview:
        image_url = str(boundary_preview.get("image_url") or "").strip()
    else:
        image_url = ""
    alt = f"Wider area around {label}"
    caption = str(boundary_preview.get("summary") or "Open a larger area map").strip() if boundary_preview else "Open a larger area map"
    return {
        "image_url": image_url,
        "thumb_image_url": thumb_image_url or image_url,
        "alt": alt,
        "title": label,
        "caption": caption,
        "map_url": map_url,
        "district_rows": list(boundary_preview.get("district_rows") or []) if boundary_preview else [],
    }


def _first_fact_text(facts: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = facts.get(key)
        if value in (None, "", []):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _bool_fact_text(facts: dict[str, object], *keys: str, label: str) -> str:
    for key in keys:
        value = facts.get(key)
        if isinstance(value, bool):
            return label if value else ""
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "ja", "available", "present"}:
            return label
    return ""


def _candidate_detail_sections(facts: dict[str, object]) -> dict[str, object]:
    object_rows = [
        ("Type", _first_fact_text(facts, "object_type", "property_type", "asset_type")),
        ("Building", _first_fact_text(facts, "building_type", "bautyp")),
        ("Condition", _first_fact_text(facts, "condition", "zustand")),
        ("Living area", _first_fact_text(facts, "area_display", "area_label") or (f"{facts.get('area_m2') or facts.get('area_sqm')} m2" if (facts.get("area_m2") or facts.get("area_sqm")) else "")),
        ("Rooms", _first_fact_text(facts, "rooms_display") or str(facts.get("rooms") or "").strip()),
        ("Floor", _first_fact_text(facts, "floor", "floor_label", "stockwerk")),
        ("Available", _first_fact_text(facts, "available_from", "available", "verfuegbar", "verfügbar")),
        ("Term", _first_fact_text(facts, "lease_term", "befristung")),
        ("Heating", _first_fact_text(facts, "heating", "heating_type")),
    ]
    cost_rows = [
        ("Rent / price", _first_fact_text(facts, "price_display", "rent_display")),
        ("Operating costs", _first_fact_text(facts, "operating_costs_display", "operating_costs_monthly_display")),
        ("Additional costs", _first_fact_text(facts, "additional_costs_display", "side_costs_display", "service_charges_display")),
        ("Deposit", _first_fact_text(facts, "deposit_display", "kaution_display")),
        ("Commission", _first_fact_text(facts, "commission_display", "maklerprovision_display")),
    ]
    feature_values = [
        _bool_fact_text(facts, "has_fitted_kitchen", "kitchen", label="Kitchen"),
        _bool_fact_text(facts, "has_cellar", "cellar", "keller", label="Cellar"),
        _bool_fact_text(facts, "has_garage", "garage", label="Garage"),
        _bool_fact_text(facts, "barrier_free", "accessible", label="Barrier-free"),
        _bool_fact_text(facts, "furnished", "has_furniture", label="Furnished"),
        _bool_fact_text(facts, "has_lift", "lift", "elevator", label="Lift"),
        _bool_fact_text(facts, "has_parking", "parking", label="Parking"),
        _bool_fact_text(facts, "has_storage_room", "storage_room", "abstellraum", label="Storage room"),
        _bool_fact_text(facts, "has_balcony", "balcony", label="Balcony"),
        _bool_fact_text(facts, "has_terrace", "terrace", label="Terrace"),
        _bool_fact_text(facts, "has_garden", "garden", label="Garden"),
        _bool_fact_text(facts, "has_loggia", "loggia", label="Loggia"),
    ]
    description_text = summarize_property_description_copy(
        _first_fact_text(facts, "description", "object_description", "listing_description", "summary")
    )
    location_text = sanitize_property_marketing_copy(
        _first_fact_text(facts, "location_description", "lage", "neighborhood_description", "micro_location_summary")
    )
    energy_rows = [
        ("HWB", _first_fact_text(facts, "hwb", "hwb_kwh_m2_year")),
        ("HWB class", _first_fact_text(facts, "hwb_class", "hwb_energieklasse")),
        ("fGEE", _first_fact_text(facts, "f_gee", "fgee")),
        ("fGEE class", _first_fact_text(facts, "f_gee_class", "fgee_energieklasse")),
        ("Heating", _first_fact_text(facts, "heating", "heating_type")),
    ]
    return {
        "object_rows": [{"label": label, "value": value} for label, value in object_rows if str(value or "").strip()],
        "cost_rows": [{"label": label, "value": value} for label, value in cost_rows if str(value or "").strip()],
        "feature_values": [value for value in feature_values if value],
        "description_text": description_text,
        "location_text": location_text,
        "energy_rows": [{"label": label, "value": value} for label, value in energy_rows if str(value or "").strip()],
    }


def _group_property_provider_options(options: list[dict[str, object]]) -> list[dict[str, object]]:
    family_order = {
        "marketplace": 0,
        "core_portal": 0,
        "classified": 0,
        "shared_housing": 1,
        "corporate_landlord": 2,
        "municipal_housing": 3,
        "broker_direct": 1,
        "cooperative": 2,
        "public_housing": 3,
        "developer_projects": 4,
        "distressed_sales": 5,
        "community_signals": 6,
        "community_meta": 7,
    }
    family_headings = {
        "marketplace": ("Core marketplaces", "Primary broad-market search groups for this country."),
        "core_portal": ("Core portals", "Primary broad-market search groups for this country."),
        "classified": ("Classifieds", "Private and long-tail inventory with weaker structure and more duplicate risk."),
        "shared_housing": ("Shared housing", "Rooms, WG, sublet, and student-friendly sources that should not pollute standard family-home search."),
        "corporate_landlord": ("Direct landlords", "Large landlord-direct inventory that often carries better availability and operating details."),
        "municipal_housing": ("Municipal housing", "City-owned or public-sector housing supply with eligibility and application rules."),
        "broker_direct": ("Broker direct", "Broker-owned inventory and direct provider feeds."),
        "cooperative": ("Cooperatives", "Genossenschaften and cooperative housing sources."),
        "public_housing": ("Public housing", "Municipal and public-housing-adjacent sources."),
        "developer_projects": ("Developer projects", "New-build and launch pipeline sources."),
        "distressed_sales": ("Court and auction", "Court-published and auction-style listings that need extra legal review."),
        "community_signals": ("Community signals", "Facebook, Telegram, and other lightly sourced off-market hints."),
        "community_meta": ("Long-tail sources", "Smaller or harder-to-verify sources that can still surface useful homes."),
    }
    grouped: dict[str, list[dict[str, object]]] = {}
    for option in options:
        family = str(option.get("family") or "marketplace").strip() or "marketplace"
        grouped.setdefault(family, []).append(option)
    rows: list[dict[str, object]] = []
    for family, items in sorted(grouped.items(), key=lambda pair: (family_order.get(pair[0], 99), pair[0])):
        title, detail = family_headings.get(
            family,
            (str(family).replace("_", " ").title(), "Grouped by source family for a cleaner market setup."),
        )
        rows.append(
            {
                "key": family,
                "title": title,
                "detail": detail,
                "options": sorted(
                    items,
                    key=lambda item: (
                        str(item.get("trust_tier") or "").strip() != "trusted",
                        str(item.get("trust_tier") or "").strip() == "watch",
                        str(item.get("label") or "").strip().lower(),
                    ),
                ),
            }
        )
    return rows


@lru_cache(maxsize=128)
def _property_market_filter_capabilities_cached(country_code: str, region_code: str) -> tuple[tuple[str, bool], ...]:
    country = str(country_code or "").strip().upper() or "AT"
    region = str(region_code or "").strip().lower()
    defaults: dict[str, bool] = {"family_zoo": True}
    if country == "AT":
        regional = {
            "vienna": {"family_zoo": True},
            "salzburg": {"family_zoo": True},
            "styria": {"family_zoo": True},
            "upper_austria": {"family_zoo": False},
            "lower_austria": {"family_zoo": False},
            "burgenland": {"family_zoo": False},
            "carinthia": {"family_zoo": False},
            "tyrol": {"family_zoo": False},
            "vorarlberg": {"family_zoo": False},
        }
        rows = {**defaults, **regional.get(region, {"family_zoo": False})}
        return tuple(sorted(rows.items()))
    if country == "DE":
        regional = {
            "berlin": {"family_zoo": True},
            "hamburg": {"family_zoo": True},
            "munich": {"family_zoo": True},
            "cologne": {"family_zoo": True},
            "frankfurt": {"family_zoo": True},
        }
        rows = {**defaults, **regional.get(region, defaults)}
        return tuple(sorted(rows.items()))
    if country in {"UK", "FR", "ES", "IT", "NL", "BE", "CH"}:
        return tuple(sorted(defaults.items()))
    return tuple(sorted(defaults.items()))


def _property_market_filter_capabilities(country_code: str, region_code: str) -> dict[str, bool]:
    return {
        key: bool(value)
        for key, value in _property_market_filter_capabilities_cached(country_code, region_code)
    }


def _property_search_guard_rows(
    *,
    preferences: dict[str, object],
    run_summary: dict[str, object],
    source_rows: list[dict[str, object]],
) -> list[dict[str, str]]:
    target_parts = [
        str(preferences.get("location_query") or "").strip(),
        str(preferences.get("region_code") or "").strip().replace("_", " ").title(),
        str(preferences.get("country_code") or "").strip().upper(),
    ]
    target_label = " · ".join(dict.fromkeys(part for part in target_parts if part))
    rows: list[dict[str, str]] = [
        {
            "title": "Target area guard",
            "detail": (
                f"Target: {target_label}. Outside-area candidates are suppressed before any filter-relaxation prompt."
                if target_label
                else "No narrow target area is set. Country-wide or broad-region results may appear."
            ),
            "tag": "Location",
        }
    ]
    outside_total = 0
    weak_filter_sources: list[str] = []
    no_plan_total = 0
    for source in source_rows:
        try:
            outside_total += max(int(float(source.get("location_mismatch_candidate_total") or 0)), 0)
        except Exception:
            pass
        try:
            no_plan_total += max(int(float(source.get("filtered_floorplan_total") or 0)), 0)
        except Exception:
            pass
        pushdown = dict(source.get("provider_filter_pushdown") or {}) if isinstance(source.get("provider_filter_pushdown"), dict) else {}
        if str(pushdown.get("filter_strength") or "").strip() == "weak_search_then_post_filter":
            weak_filter_sources.append(str(source.get("source_label") or source.get("platform") or "Provider").strip())
    if outside_total:
        rows.append(
            {
                "title": "Outside-area results suppressed",
                "detail": f"{outside_total} home{' was' if outside_total == 1 else 's were'} outside the selected area.",
                "tag": "Suppressed",
            }
        )
    held_back = 0
    try:
        held_back = max(int(float(run_summary.get("notification_budget_suppressed_total") or 0)), 0)
    except Exception:
        held_back = 0
    if held_back:
        rows.append(
            {
                "title": "Alert budget applied",
                "detail": f"{held_back} lower-ranked candidate{' was' if held_back == 1 else 's were'} kept in the table instead of sent as messages.",
                "tag": "Messages",
            }
        )
    if weak_filter_sources:
        rows.append(
            {
                "title": "Source filters are limited",
                "detail": f"{', '.join(weak_filter_sources[:3])} could not apply every filter directly, so PropertyQuarry checked the listings after reading them.",
                "tag": "Source",
            }
        )
    if no_plan_total:
        rows.append(
            {
                "title": "Floorplan detail",
                "detail": f"{no_plan_total} home{' still needs' if no_plan_total == 1 else 's still need'} a clear floorplan.",
                "tag": "Open",
            }
        )
    return rows[:5]


def _property_suppression_rows(
    *,
    run_summary: dict[str, object],
    source_rows: list[dict[str, object]],
    preferences: dict[str, object] | None = None,
    include_soft: bool = False,
) -> list[dict[str, object]]:
    effective_preferences = dict(preferences or {})
    counters: dict[str, int] = {
        "Outside selected area": 0,
        "Property type mismatch": 0,
        "Wrong transaction type": 0,
        "Overview page": 0,
        "Floorplan still missing": 0,
        "Outside area/size rule": 0,
        "Availability mismatch": 0,
        "Alert budget": 0,
    }
    source_labels: dict[str, set[str]] = {key: set() for key in counters}
    field_map = (
        ("Outside selected area", "location_mismatch_candidate_total"),
        ("Property type mismatch", "filtered_property_type_total"),
        ("Wrong transaction type", "filtered_listing_mode_total"),
        ("Overview page", "filtered_generic_page_total"),
        ("Floorplan still missing", "filtered_floorplan_total"),
        ("Outside area/size rule", "filtered_area_total"),
        ("Availability mismatch", "filtered_availability_total"),
        ("Alert budget", "notification_budget_suppressed_total"),
    )
    for source in source_rows:
        source_label = str(source.get("source_label") or source.get("platform") or "Provider").strip() or "Provider"
        for label, field_name in field_map:
            try:
                value = max(int(float(source.get(field_name) or 0)), 0)
            except Exception:
                value = 0
            if value:
                counters[label] += value
                source_labels[label].add(source_label)
    try:
        summary_budget = max(int(float(run_summary.get("notification_budget_suppressed_total") or 0)), 0)
    except Exception:
        summary_budget = 0
    if summary_budget > counters["Alert budget"]:
        counters["Alert budget"] = summary_budget
    summary_field_map = (
        ("Outside selected area", "filtered_location_total"),
        ("Property type mismatch", "filtered_property_type_total"),
        ("Wrong transaction type", "filtered_listing_mode_total"),
        ("Overview page", "filtered_generic_page_total"),
        ("Floorplan still missing", "filtered_floorplan_total"),
        ("Outside area/size rule", "filtered_area_total"),
        ("Availability mismatch", "filtered_availability_total"),
    )
    for label, field_name in summary_field_map:
        if counters[label] > 0:
            continue
        try:
            counters[label] = max(int(float(run_summary.get(field_name) or 0)), 0)
        except Exception:
            counters[label] = 0
    action_map = {
        "Outside selected area": "Add nearby districts first instead of opening the full market.",
        "Property type mismatch": "Allow this property type temporarily if you want mixed options in this round.",
        "Wrong transaction type": "Keep this strict. Rent and buy must stay separate, so mismatched homes are retried separately.",
        "Overview page": "Keep this strict. Overview, news, or competition pages are skipped.",
        "Floorplan still missing": "These homes are still being checked for a floorplan in photos, PDFs, downloads, and 360 media.",
        "Outside area/size rule": "Stretch the size or area rule only if the shortlist feels too thin.",
        "Availability mismatch": "Loosen the move-in timing if the date is flexible.",
        "Alert budget": "Raise the daily alert limit if you want more saved-search notifications.",
    }
    title_map = {
        "Outside selected area": "Include nearby districts",
        "Property type mismatch": "Widen property-type rule",
        "Floorplan still missing": "Include homes while floorplans are still being checked",
        "Outside area/size rule": "Stretch the size rule",
        "Availability mismatch": "Loosen move-in timing",
        "Alert budget": "Raise the alert limit",
    }
    action_label_map = {
        "Outside selected area": "Set nearby radius",
        "Property type mismatch": "Relax property type",
        "Floorplan still missing": "Show held-back homes",
        "Outside area/size rule": "Relax size",
        "Availability mismatch": "Edit move-in timing",
        "Alert budget": "Raise alerts",
    }

    def _positive_int(value: object) -> int:
        try:
            return max(0, int(float(str(value or "").strip())))
        except Exception:
            return 0

    min_area_m2 = _positive_int(effective_preferences.get("min_area_m2"))
    max_area_m2 = _positive_int(effective_preferences.get("max_area_m2"))
    available_within_years = _positive_int(effective_preferences.get("available_within_years"))
    adjacent_radius_m = _positive_int(effective_preferences.get("adjacent_area_radius_m"))
    location_query = ", ".join(str(item).strip() for item in str(effective_preferences.get("location_query") or "").split(",") if str(item).strip())

    rows: list[dict[str, str]] = []
    for label, total in counters.items():
        if total <= 0:
            continue
        providers = ", ".join(sorted(source_labels[label])[:3])
        rule_detail = ""
        if label == "Outside area/size rule":
            size_parts: list[str] = []
            if min_area_m2 > 0:
                size_parts.append(f"min {min_area_m2} m²")
            if max_area_m2 > 0:
                size_parts.append(f"max {max_area_m2} m²")
            if size_parts:
                rule_detail = f" Current size rule: {' · '.join(size_parts)}."
        elif label == "Availability mismatch" and available_within_years > 0:
            rule_detail = f" Current move-in window: within {available_within_years} year{'s' if available_within_years != 1 else ''}."
        elif label == "Outside selected area":
            area_parts: list[str] = []
            if location_query:
                area_parts.append(location_query)
            if adjacent_radius_m > 0:
                area_parts.append(f"{adjacent_radius_m} m spillover")
            if area_parts:
                rule_detail = f" Current area rule: {' · '.join(area_parts)}."
        rows.append(
            {
                "title": title_map.get(label, label),
                "rule_key": label,
                "detail": f"{total} candidate{' was' if total == 1 else 's were'} filtered out.{rule_detail} {action_map[label]}",
                "tag": providers or "Search rule",
                "affected_total": total,
                "action_label": action_label_map.get(label, "Review rule"),
            }
        )
    if not rows:
        aggregate_filtered_total = 0
        for field_name in ("filtered_total", "held_back_total", "filtered_out_total"):
            try:
                aggregate_filtered_total = max(aggregate_filtered_total, int(float(run_summary.get(field_name) or 0)))
            except Exception:
                continue
        if aggregate_filtered_total > 0:
            rows.append(
                {
                    "title": "Filtered by this search",
                    "rule_key": "Aggregate filtered",
                    "detail": f"{aggregate_filtered_total} candidates were held back by this search. Open the active filters to inspect what is still strict.",
                    "tag": "Search rule",
                    "affected_total": aggregate_filtered_total,
                    "action_label": "Adjust filters",
                }
            )
    return rows[:8]


def _delivery_proof_rows(run_summary: dict[str, object]) -> list[dict[str, str]]:
    try:
        telegram_sent = max(int(float(run_summary.get("telegram_sent_total") or run_summary.get("notified_total") or 0)), 0)
    except Exception:
        telegram_sent = 0
    try:
        tour_total = max(int(float(run_summary.get("tour_created_total") or 0)) + int(float(run_summary.get("tour_existing_total") or 0)), 0)
    except Exception:
        tour_total = 0
    try:
        packet_total = max(int(float(run_summary.get("packet_created_total") or run_summary.get("review_created_total") or 0)), 0)
    except Exception:
        packet_total = 0
    writing_status = str(run_summary.get("dossier_writer_neuronwriter_status") or run_summary.get("writing_status") or "ready").strip()
    if not writing_status:
        writing_status = "ready"
    return [
        {
            "title": f"Writing: {writing_status}",
            "detail": "Pages, messages, and links stay short, private, and tied to the facts already found.",
            "tag": "Clean",
        },
        {
            "title": "Message links",
            "detail": "Messages use titled links instead of long URLs.",
            "tag": "Clean links",
        },
        {
            "title": "Files ready",
            "detail": f"{packet_total} saved page{'s' if packet_total != 1 else ''}, {tour_total} tour{'s' if tour_total != 1 else ''}, {telegram_sent} sent update{'s' if telegram_sent != 1 else ''}.",
            "tag": "Saved",
        },
    ]


def _artifact_receipt_rows(run_summary: dict[str, object]) -> list[dict[str, str]]:
    rows = [dict(row) for row in required_artifact_receipt_rows()]
    try:
        telegram_sent = max(int(float(run_summary.get("telegram_sent_total") or run_summary.get("notified_total") or 0)), 0)
    except Exception:
        telegram_sent = 0
    try:
        tour_total = max(int(float(run_summary.get("tour_created_total") or 0)) + int(float(run_summary.get("tour_existing_total") or 0)), 0)
    except Exception:
        tour_total = 0
    try:
        flythrough_total = max(int(float(run_summary.get("flythrough_rendered_total") or 0)) + int(float(run_summary.get("flythrough_existing_total") or 0)), 0)
    except Exception:
        flythrough_total = 0
    rows.append(
            {
                "title": "Current outputs",
                "detail": f"{tour_total} 3D tour{'s' if tour_total != 1 else ''}, {flythrough_total} walkthrough video{'s' if flythrough_total != 1 else ''}, {telegram_sent} sent update{'s' if telegram_sent != 1 else ''}.",
                "tag": "Saved",
            }
        )
    repair_receipts = [dict(row) for row in list(run_summary.get("repair_receipts") or []) if isinstance(row, dict)]
    if repair_receipts:
        latest = repair_receipts[-1]
        latest_resolution = str(latest.get("resolution") or "checked").replace("_", " ").strip()
        latest_detail = (
            f"Checked {len(repair_receipts)} time{'s' if len(repair_receipts) != 1 else ''}. "
            f"Last result: {latest_resolution}."
        )
        rows.append(
            {
                "title": "Latest check",
                "detail": latest_detail,
                "tag": "Update",
            }
        )
    return rows


def _official_risk_posture_rows(official: dict[str, object]) -> list[dict[str, str]]:
    rows = [dict(row) for row in list(official.get("sources") or []) if isinstance(row, dict)]
    if not rows:
        return []
    total = len(rows)
    official_total = 0
    partial_total = 0
    gap_total = 0
    flagged_total = 0
    review_total = 0
    verified_total = 0
    low_conf_total = 0
    verified_states = {"verified", "confirmed", "cleared"}
    for row in rows:
        availability = str(row.get("availability") or "").strip().lower()
        verification_state = str(row.get("verification_state") or "").strip().lower()
        confidence = str(row.get("confidence") or "").strip().lower()
        if availability == "official_dataset":
            official_total += 1
        elif availability == "partial_official":
            partial_total += 1
        if availability in {"municipal_gap", "source_gap"} or verification_state == "source_gap":
            gap_total += 1
        if verification_state == "flagged":
            flagged_total += 1
        if verification_state not in verified_states:
            review_total += 1
        if verification_state in verified_states:
            verified_total += 1
        if confidence == "low":
            low_conf_total += 1
    if gap_total:
        headline = "Some public data is missing"
        headline_detail = f"{gap_total} check(s) still depend on municipality-specific data."
        headline_tag = "Open"
    elif flagged_total:
        headline = "Public sources identified, checks still open"
        headline_detail = f"{flagged_total} check(s) still need one final look."
        headline_tag = "Open"
    elif review_total:
        headline = "Public sources identified, checks still open"
        headline_detail = f"{review_total} check(s) still need a clear answer."
        headline_tag = "Open"
    else:
        headline = "Public data verified"
        headline_detail = "Every identified source has been explicitly checked and no gaps remain open."
        headline_tag = "Ready"
    next_steps: list[str] = []
    for row in rows:
        verification_state = str(row.get("verification_state") or "").strip().lower()
        availability = str(row.get("availability") or "").strip().lower()
        required_next_step = str(row.get("required_next_step") or "").strip()
        if verification_state in verified_states and availability not in {"municipal_gap", "source_gap"}:
            continue
        if required_next_step and required_next_step not in next_steps:
            next_steps.append(required_next_step)
    source_label = "source" if total == 1 else "sources"
    coverage_parts = [f"{total} public {source_label} identified", f"{official_total} official", f"{partial_total} partial", f"{gap_total} gaps"]
    verification_parts = [f"{verified_total} checked", f"{flagged_total} flagged", f"{review_total} still open"]
    ready = verified_total == total and not gap_total and not review_total
    response = [
        {"title": headline, "detail": headline_detail, "tag": headline_tag},
        {"title": "Coverage", "detail": " | ".join(coverage_parts), "tag": str(official.get("country_code") or "").strip() or "Market"},
        {
            "title": "Checked",
            "detail": " | ".join(verification_parts),
            "tag": f"{low_conf_total} thin detail" if ready and low_conf_total else ("Ready" if ready else "Open"),
        },
    ]
    if next_steps:
        response.append(
            {
                "title": "Next check",
                "detail": " | ".join(next_steps[:2]),
                "tag": "Open",
            }
        )
    updated_at = str(official.get("updated_at") or "").strip()
    if updated_at:
        response.append(
            {
                "title": "Data snapshot",
                "detail": updated_at.replace("T", " ").replace("+00:00", " UTC"),
                "tag": "Snapshot",
            }
        )
    return response


def _property_counterfactual_rows(
    *,
    preferences: dict[str, object],
    raw_preferences: dict[str, object] | None,
    run_summary: dict[str, object],
    provider_options: list[dict[str, object]],
    current_platform_cap: int,
    currency_code: str = "EUR",
) -> list[dict[str, object]]:
    def _sanitize_counterfactual_row(row: dict[str, object]) -> dict[str, object]:
        item = dict(row)
        title = str(item.get("title") or "").strip()
        detail = str(item.get("detail") or "").strip()
        action_label = str(item.get("action_label") or "").strip()
        lowered_title = title.lower()
        if "duplicate listing" in lowered_title or "wrong property type" in lowered_title:
            return {}
        if title.lower() in {"pending layout proof", "missing floorplan evidence", "floorplan still missing"}:
            item["title"] = "Floorplan still missing"
            if detail:
                item["detail"] = detail.replace("layout proof", "floorplan detail").replace("floorplan evidence", "floorplan detail")
            if action_label.lower() == "run layout recovery":
                item["action_label"] = "Recover floorplans"
        return item

    rows: list[dict[str, object]] = [
        _sanitize_counterfactual_row(dict(row))
        for row in list(run_summary.get("search_broaden_suggestions") or [])
        if isinstance(row, dict) and str(row.get("title") or "").strip()
    ]

    def _positive_int(value: object, default: int = 0) -> int:
        try:
            return max(0, int(float(str(value or "").strip())))
        except Exception:
            return default

    def _has_explicit_numeric_filter(source: dict[str, object] | None, key: str) -> bool:
        raw_source = dict(source or {})
        nested = raw_source.get("raw_preferences")
        if isinstance(nested, dict):
            raw_source = dict(nested)
        if key not in raw_source:
            return False
        value = raw_source.get(key)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        try:
            return int(float(str(value).strip())) > 0
        except Exception:
            return False

    def _sum_source_total(field_name: str) -> int:
        total = 0
        for source in list(run_summary.get("sources") or []):
            if not isinstance(source, dict):
                continue
            try:
                total += max(0, int(float(source.get(field_name) or 0)))
            except Exception:
                continue
        return total

    outside_area_or_size_total = _sum_source_total("filtered_area_total")
    outside_selected_area_total = _sum_source_total("location_mismatch_candidate_total")

    filtered_floorplan_total = _positive_int(run_summary.get("filtered_floorplan_total"), 0)
    if bool(preferences.get("require_floorplan")) and filtered_floorplan_total > 0:
        rows.append(
            {
                "title": "Include homes while floorplans are still being checked",
                "detail": f"{filtered_floorplan_total} listing(s) are being held back while we look for a floorplan. Use this only for a wider look, then turn the rule back on.",
                "tag": "Research",
                "action_label": "Show held-back homes",
                "adjustments": {"require_floorplan": False},
                "affected_total": filtered_floorplan_total,
            }
        )

    country_code = str(preferences.get("country_code") or "").strip().upper()
    region_code = str(preferences.get("region_code") or "").strip().lower()
    full_region_scope = bool(preferences.get("full_region_scope"))
    if country_code and region_code and not full_region_scope:
        try:
            from app.services.property_market_catalog import region_label_for_country_region
            region_label = region_label_for_country_region(country_code, region_code)
        except Exception:
            region_label = region_code.replace("_", " ").title()
        rows.append(
            {
                "title": "Add homes near the selected districts",
                "detail": f"Include homes within a small radius of the selected districts when they sit in adjacent parts of {region_label}.",
                "tag": "Area",
                "action_label": "Set nearby radius",
                "adjustments": {"full_region_scope": True, "location_query": region_label, "custom_location_query": "", "adjacent_area_radius_m": 750},
                "slider": {
                    "kind": "radius",
                    "field": "adjacent_area_radius_m",
                    "label": "Nearby radius",
                    "min": 250,
                    "max": 2000,
                    "step": 250,
                    "value": 750,
                    "unit": "m",
                },
                "affected_total": outside_selected_area_total,
            }
        )

    selected_platforms = [
        str(value).strip()
        for value in list(preferences.get("selected_platforms") or [])
        if str(value).strip()
    ]
    cap = max(0, int(current_platform_cap or 0))
    available_platforms = [
        str(option.get("value") or "").strip()
        for option in provider_options
        if str(option.get("value") or "").strip()
    ]
    widened_platforms = list(dict.fromkeys([*selected_platforms, *available_platforms]))
    if cap > 0:
        widened_platforms = widened_platforms[:cap]
    if len(widened_platforms) > len(selected_platforms):
        rows.append(
            {
                "title": f"Check {len(widened_platforms)} sites instead of {len(selected_platforms)}",
                "detail": "Use the rest of the site allowance on the current plan before widening the brief itself.",
                "tag": "Sites",
                "action_label": "Use all sites",
                "adjustments": {"selected_platforms": widened_platforms},
                "affected_total": 0,
            }
        )

    current_budget = _positive_int(preferences.get("max_price_eur"), 0)
    explicit_budget = _has_explicit_numeric_filter(raw_preferences, "max_price_eur")
    if current_budget > 0 and explicit_budget:
        next_budget = current_budget + max(25000, int(round(current_budget * 0.1)))
        max_budget = current_budget + max(100000, int(round(current_budget * 0.35)))
        budget_currency = str(currency_code or "EUR").strip().upper() or "EUR"
        rows.append(
            {
                "title": "Raise the budget once",
                "detail": "Use one wider price pass to see whether budget pressure is the real blocker.",
                "tag": "Budget",
                "action_label": f"Raise to {budget_currency} {next_budget:,}",
                "adjustments": {"max_price_eur": next_budget},
                "slider": {
                    "kind": "budget",
                    "field": "max_price_eur",
                    "label": "Budget ceiling",
                    "min": current_budget,
                    "max": max_budget,
                    "step": 5000,
                    "value": next_budget,
                    "unit": budget_currency,
                },
                "affected_total": outside_area_or_size_total,
            }
        )

    strict_distance_keys = [
        "max_distance_to_market_m",
        "max_distance_to_hardware_store_m",
        "max_distance_to_medical_care_m",
        "max_distance_to_library_m",
        "max_distance_to_public_pool_m",
        "max_distance_to_theatre_m",
    ]
    strict_distance_count = sum(1 for key in strict_distance_keys if _positive_int(preferences.get(key), 0) > 0)
    if strict_distance_count >= 2:
        relaxed_adjustments: dict[str, object] = {}
        for key in strict_distance_keys:
            current_value = _positive_int(preferences.get(key), 0)
            if current_value > 0:
                relaxed_adjustments[key] = int(round(current_value * 1.35))
        rows.append(
            {
                "title": "Stretch the everyday distance limits",
                "detail": "Keep the same lifestyle intent but widen the walking distance enough to recover borderline homes.",
                "tag": "Alltag",
                "action_label": "Stretch distance",
                "adjustments": relaxed_adjustments,
                "affected_total": outside_area_or_size_total,
            }
        )

    deduped: list[dict[str, object]] = []
    seen_titles: set[str] = set()
    for row in rows:
        title = str(row.get("title") or "").strip().lower()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        deduped.append(row)
    if not deduped:
        deduped.append(
            {
                "title": "Reopen the brief with broader constraints",
                "detail": "Keep the same market, but reopen the brief so you can widen sites or relax one hard filter before the next search.",
                "tag": "Reset",
                "action_label": "Reopen brief",
                "adjustments": {},
            }
        )
    return deduped[:5]
