from __future__ import annotations

import re
import urllib.parse
import json

from app.api.routes.landing_property_saved_searches import (
    build_agent_management_rows,
    select_property_search_agent,
)
from app.api.routes.landing_property_surface_contracts import (
    PropertyDecisionWorkbenchBriefContract,
    PropertyDecisionWorkbenchContract,
    PropertyDecisionWorkbenchRunContract,
    PropertySurfacePayloadContract,
    PropertySurfaceScope,
)
from app.api.routes.landing_property_workspace_helpers import (
    _artifact_receipt_rows,
    _candidate_detail_sections,
    _compact_provider_label,
    _delivery_proof_rows,
    _group_property_provider_options,
    _official_risk_posture_rows,
    _property_candidate_directions_url,
    _property_candidate_maps_url,
    _property_candidate_orientation_preview,
    _property_candidate_floorplan_url,
    _property_candidate_is_rankable,
    _property_candidate_preview_image,
    _property_candidate_route_evidence,
    _property_candidate_display_facts,
    _property_candidate_source_virtual_tour_url,
    _property_decoded_url_path,
    _property_postal_codes_from_text,
    _property_postal_names_from_text,
    _property_counterfactual_rows,
    _property_family_filters_active,
    _property_market_filter_capabilities,
    _property_progress_current_property_card,
    _property_progress_route_preview_rows,
    _property_run_reliability_summary,
    _property_search_guard_rows,
    _property_search_worker_slots,
    _property_suppression_rows,
    _property_url_path_is_structurally_safe,
    _property_visual_reason_key,
)
from app.product.property_surface_state import (
    build_property_empty_outcome_summary,
    build_property_preference_manager_snapshot,
    build_property_previous_run_summary,
    build_property_shortlist_snapshot,
    build_property_workbench_candidate_snapshot,
    effective_property_listing_mode,
    normalized_property_search_goal,
    property_mode_visibility_label,
    property_run_customer_safe_status_detail,
    property_run_customer_visible_events,
    property_run_public_eta_label,
)
from app.product.property_score_methodology import build_property_score_methodology
from app.product.property_delivery_governance import property_delivery_governance_rows
from app.product import property_tour_hosting
from app.product.service import (
    _PROPERTY_GENERATED_TOUR_STATUS_DETAIL,
    _PROPERTY_GENERATED_TOUR_STATUS_LABEL,
    _hosted_property_tour_telegram_preview_image_url_for_style,
    _hosted_property_visual_progress_snapshot,
    _hosted_property_visual_progress_stage_label,
    _property_visual_eta_label,
    _property_visual_layout_preview_url,
    _property_visual_progress_pct,
    _property_visual_ready_tour_url,
    _property_visual_terminal_status_for_reason,
    _property_visual_unavailable_detail,
)
from app.services.property_billing import normalize_property_plan_key
from app.services.public_url_safety import public_http_url_is_safe


_PROPERTY_PROPERTIES_FIRST_PAINT_RESULT_LIMIT = 24

_PROPERTY_WORKBENCH_CLIENT_IMAGE_EXTENSIONS = (
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
)
_PROPERTY_WORKBENCH_CLIENT_VIDEO_EXTENSIONS = (".m4v", ".mov", ".mp4", ".ogv", ".webm")
_PROPERTY_WORKBENCH_CLIENT_TRACKING_MARKERS = (
    "/analytics/",
    "/beacon/",
    "/collect/",
    "/events/",
    "/logevent/",
    "/tracking/",
    "360-click",
    "floorplan-click",
    "preview-click",
    "virtual-tour-click",
    "virtual_tour_click",
)
_PROPERTY_WORKBENCH_READY_VISUAL_STATUSES = {
    "available",
    "complete",
    "completed",
    "created",
    "existing",
    "ready",
    "source",
}


def _candidate_external_listing_url(
    candidate: dict[str, object],
    *,
    facts: dict[str, object] | None = None,
) -> str:
    resolved_facts = facts or (
        dict(candidate.get("property_facts") or {})
        if isinstance(candidate.get("property_facts"), dict)
        else {}
    )
    summary_text = " ".join(
        part
        for part in (
            str(candidate.get("title") or "").strip(),
            str(candidate.get("summary") or "").strip(),
            str(candidate.get("fit_summary") or "").strip(),
        )
        if part
    ).lower()
    concrete_signals = any(
        (
            resolved_facts.get("rooms"),
            resolved_facts.get("living_area_sqm"),
            resolved_facts.get("area_sqm"),
            resolved_facts.get("usable_area_sqm"),
            resolved_facts.get("price_eur"),
            resolved_facts.get("purchase_price_eur"),
            resolved_facts.get("buy_price_eur"),
            resolved_facts.get("rent_eur"),
            resolved_facts.get("monthly_rent_eur"),
            resolved_facts.get("warm_rent_eur"),
            resolved_facts.get("cold_rent_eur"),
            resolved_facts.get("exact_address"),
            resolved_facts.get("street_address"),
        )
    )
    for key in ("property_url", "source_url"):
        url = str(candidate.get(key) or "").strip()
        if not url:
            continue
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.strip().lower()
        path = parsed.path.strip().lower()
        if path.startswith("/app/"):
            continue
        if host.endswith("propertyquarry.com") and path.startswith("/app/"):
            continue
        if not concrete_signals and (
            "search candidate" in summary_text
            or "search-results page" in summary_text
            or "search results page" in summary_text
            or "/projects/" in path
        ):
            continue
        return url
    return ""


def _hosted_tour_unavailable_detail() -> str:
    return "For now, no usable 3D tour is available yet."


def _property_workbench_lightweight_image_url(value: object, *, max_data_url_chars: int = 4096) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if url.lower().startswith("data:") and len(url) > max_data_url_chars:
        return ""
    return url


def _property_workbench_lightweight_orientation_preview(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    preview = dict(value)
    for key in ("image_url", "thumb_image_url", "preview_image_url"):
        cleaned = _property_workbench_lightweight_image_url(preview.get(key))
        if cleaned:
            preview[key] = cleaned
        else:
            preview.pop(key, None)
    return preview


def _property_workbench_client_safe_web_or_local_url(value: object) -> str:
    url = str(value or "").strip()
    if (
        not url
        or len(url) > 2048
        or "\\" in url
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        return ""
    if url.startswith("/"):
        if url.startswith("//"):
            return ""
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return ""
        if (
            parsed.scheme
            or parsed.netloc
            or not _property_url_path_is_structurally_safe(
                _property_decoded_url_path(parsed)
            )
        ):
            return ""
        return url
    return url if public_http_url_is_safe(url) else ""


def _property_workbench_client_url_is_tracking(value: object) -> bool:
    url = str(value or "").strip()
    decoded_url = url
    for _index in range(3):
        next_decoded = urllib.parse.unquote(decoded_url)
        if next_decoded == decoded_url:
            break
        decoded_url = next_decoded
    lowered_url = decoded_url.lower()
    if any(marker in lowered_url for marker in _PROPERTY_WORKBENCH_CLIENT_TRACKING_MARKERS):
        return True
    try:
        parsed = urllib.parse.urlsplit(lowered_url)
    except ValueError:
        return True
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    path = str(parsed.path or "").strip().lower()
    return host == "api.willhaben.at" and path.startswith("/restapi/")


def _property_workbench_client_asset_url(value: object, *, kind: str) -> str:
    url = _property_workbench_client_safe_web_or_local_url(value)
    if not url or _property_workbench_client_url_is_tracking(url):
        return ""
    try:
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path or "").strip().lower()
    except ValueError:
        return ""
    extensions = (
        _PROPERTY_WORKBENCH_CLIENT_VIDEO_EXTENSIONS
        if str(kind or "").strip().lower() == "video"
        else _PROPERTY_WORKBENCH_CLIENT_IMAGE_EXTENSIONS
    )
    return url if path.endswith(extensions) else ""


def _property_workbench_client_image_url(value: object) -> str:
    return _property_workbench_client_asset_url(value, kind="image")


def _property_workbench_client_floorplan_url(value: object) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    validated = _property_candidate_floorplan_url(
        {"floorplan_url": url},
        facts={},
    )
    return url if validated == url else ""


def _property_workbench_client_image_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, object] = {}
    for key in ("image_url", "thumb_image_url", "preview_image_url"):
        cleaned = _property_workbench_client_image_url(dict(value).get(key))
        if cleaned:
            compact[key] = cleaned
    return compact


def _property_workbench_client_provider_viewer_url(value: object) -> str:
    url = _property_workbench_client_safe_web_or_local_url(value)
    if not url or _property_workbench_client_url_is_tracking(url):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    # Marker-only same-origin paths are not provider proof. First-party hosted
    # tours are admitted above through the verified hosting resolver; source
    # viewers must name a public provider directly.
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    recognized = _property_candidate_source_virtual_tour_url(
        {"source_virtual_tour_url": url},
        facts={},
    )
    return url if recognized == url else ""


def _property_workbench_candidate_ready_tour_url(
    candidate: dict[str, object],
    *,
    principal_id: str = "",
) -> str:
    raw = dict(candidate or {})
    raw_tour_payload = dict(raw.get("tour") or {}) if isinstance(raw.get("tour"), dict) else {}
    recognized_source_url = _property_candidate_source_virtual_tour_url(raw)
    generated_source_urls = {
        str(value or "").strip()
        for value in (
            raw.get("generated_reconstruction_url"),
            raw.get("layout_preview_url"),
            raw_tour_payload.get("generated_reconstruction_url"),
            raw_tour_payload.get("layout_preview_url"),
        )
        if str(value or "").strip()
    }
    candidates = (
        raw.get("tour_url"),
        raw.get("open_tour_url"),
        raw.get("verified_tour_url"),
        raw_tour_payload.get("url"),
        raw_tour_payload.get("tour_url"),
        raw_tour_payload.get("embed_url"),
        raw_tour_payload.get("open_tour_url"),
        raw_tour_payload.get("provider_url"),
        recognized_source_url,
    )
    seen: set[str] = set()
    for candidate_url in candidates:
        normalized_candidate_url = str(candidate_url or "").strip()
        if not normalized_candidate_url or normalized_candidate_url in seen:
            continue
        seen.add(normalized_candidate_url)
        if normalized_candidate_url in generated_source_urls or _property_visual_layout_preview_url(
            normalized_candidate_url
        ):
            continue
        safe_ready_tour_url = _property_workbench_client_safe_web_or_local_url(
            normalized_candidate_url
        )
        if not safe_ready_tour_url or _property_workbench_client_url_is_tracking(safe_ready_tour_url):
            continue
        # A raw provider URL is source evidence, never the public action. Only
        # a first-party hosted bundle with a verified 3DVista or Matterport
        # control may become the optional 3D-tour target.
        if property_tour_hosting._hosted_property_tour_reference_parts(safe_ready_tour_url) is None:
            continue
        provider = str(
            property_tour_hosting._hosted_property_tour_verified_provider(
                safe_ready_tour_url,
                principal_id=principal_id,
            )
            or ""
        ).strip().lower()
        if provider not in {"3dvista", "matterport"}:
            continue
        verified_hosted_url = str(
            property_tour_hosting._hosted_property_tour_verified_open_url(
                safe_ready_tour_url,
                principal_id=principal_id,
            )
            or ""
        ).strip()
        safe_hosted_url = _property_workbench_client_safe_web_or_local_url(verified_hosted_url)
        if (
            safe_hosted_url
            and not _property_workbench_client_url_is_tracking(safe_hosted_url)
            and property_tour_hosting._hosted_property_tour_reference_parts(safe_hosted_url)
            is not None
        ):
            return safe_hosted_url
    return ""


def _property_workbench_candidate_generated_layout_url(candidate: dict[str, object]) -> str:
    raw = dict(candidate or {})
    raw_tour_payload = dict(raw.get("tour") or {}) if isinstance(raw.get("tour"), dict) else {}
    generated_mode = bool(
        str(raw.get("tour_media_mode") or raw_tour_payload.get("tour_media_mode") or "").strip().lower()
        == "generated_reconstruction"
    )
    candidates = [
        raw.get("generated_reconstruction_url"),
        raw.get("layout_preview_url"),
        raw_tour_payload.get("generated_reconstruction_url"),
        raw_tour_payload.get("layout_preview_url"),
    ]
    if generated_mode:
        candidates.extend(
            (
                raw.get("open_tour_url"),
                raw.get("tour_url"),
                raw_tour_payload.get("open_tour_url"),
                raw_tour_payload.get("url"),
                raw_tour_payload.get("embed_url"),
            )
        )
    for candidate_url in candidates:
        normalized_candidate_url = str(candidate_url or "").strip()
        if not normalized_candidate_url:
            continue
        layout_preview_url = _property_visual_layout_preview_url(normalized_candidate_url)
        safe_layout_preview_url = _property_workbench_client_safe_web_or_local_url(
            layout_preview_url
        )
        if (
            safe_layout_preview_url
            and not _property_workbench_client_url_is_tracking(safe_layout_preview_url)
        ):
            return safe_layout_preview_url
    return ""


def _property_workbench_candidate_flythrough_url(
    candidate: dict[str, object],
    *,
    ready_tour_url: str,
    principal_id: str = "",
) -> str:
    raw = dict(candidate or {})
    raw_flythrough = dict(raw.get("flythrough") or {}) if isinstance(raw.get("flythrough"), dict) else {}
    raw_url = (
        raw.get("flythrough_url")
        or raw_flythrough.get("url")
        or raw_flythrough.get("embed_url")
        or raw_flythrough.get("provider_url")
    )
    raw_tour = dict(raw.get("tour") or {}) if isinstance(raw.get("tour"), dict) else {}
    hosted_sources = (
        ready_tour_url,
        raw_url,
        raw.get("tour_url"),
        raw.get("open_tour_url"),
        raw.get("verified_tour_url"),
        raw.get("generated_reconstruction_url"),
        raw.get("layout_preview_url"),
        raw_tour.get("url"),
        raw_tour.get("tour_url"),
        raw_tour.get("open_tour_url"),
        raw_tour.get("generated_reconstruction_url"),
        raw_tour.get("layout_preview_url"),
    )
    seen: set[str] = set()
    for hosted_source in hosted_sources:
        normalized_source = str(hosted_source or "").strip()
        if not normalized_source or normalized_source in seen:
            continue
        seen.add(normalized_source)
        # Never turn a loose video URL into customer-ready walkthrough copy.
        # The exact hosted bundle remains the only publication authority, but
        # it no longer depends on an optional spatial-provider tour being ready.
        resolved_url = property_tour_hosting._hosted_property_tour_walkthrough_open_url(
            normalized_source,
            raw_url,
            principal_id=principal_id,
        )
        resolved_url = _property_workbench_client_safe_web_or_local_url(resolved_url)
        if resolved_url and not _property_workbench_client_url_is_tracking(resolved_url):
            return resolved_url
    return ""


def _property_workbench_candidate_diorama_preview_url(candidate: dict[str, object]) -> str:
    raw = dict(candidate or {})
    direct_preview = _property_workbench_client_image_url(
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
        or raw.get("tour_runtime_url")
        or raw.get("tour_url")
        or raw.get("verified_tour_url")
        or raw_tour_payload.get("url")
        or raw_tour_payload.get("tour_url")
        or ""
    ).strip()
    raw_open_tour_url = str(
        raw.get("open_tour_url")
        or raw.get("verified_tour_url")
        or raw_tour_payload.get("embed_url")
        or raw_tour_payload.get("open_tour_url")
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
    return _property_workbench_client_image_url(preview_url)


_PROPERTY_WORKBENCH_CLIENT_FACT_KEYS = (
    "price_display",
    "rent_display",
    "price_per_sqm_display",
    "currency_code",
    "price",
    "rent",
    "price_eur",
    "rent_eur",
    "total_rent_eur",
    "monthly_rent_eur",
    "purchase_price_eur",
    "area_sqm",
    "rooms",
    "floor",
    "has_floorplan",
    "floorplan_count",
    "exact_address",
    "street_address",
    "address",
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
    "nearest_school_lat",
    "nearest_school_lng",
    "nearest_supermarket_m",
    "nearest_supermarket_name",
    "nearest_supermarket_lat",
    "nearest_supermarket_lng",
    "nearest_playground_m",
    "nearest_playground_name",
    "nearest_playground_lat",
    "nearest_playground_lng",
    "nearest_pharmacy_m",
    "nearest_pharmacy_name",
    "nearest_pharmacy_lat",
    "nearest_pharmacy_lng",
    "nearest_subway_m",
    "nearest_subway_name",
    "nearest_subway_lat",
    "nearest_subway_lng",
)


_PROPERTY_WORKBENCH_CLIENT_EVIDENCE_TEXT_KEYS = (
    "source_label",
    "title",
    "freshness_label",
    "summary",
    "availability",
    "verification_state",
    "confidence",
)


def _property_workbench_client_evidence_text(value: object, *, max_chars: int = 240) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    text = " ".join(str(value or "").split()).strip()
    return text[:max_chars]


def _property_workbench_client_evidence_url(value: object) -> str:
    url = str(value or "").strip()
    if (
        not url
        or len(url) > 2048
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or not public_http_url_is_safe(url)
    ):
        return ""
    return url


def _property_workbench_client_evidence_rows(
    value: object,
    *,
    max_rows: int,
    categorized: bool,
) -> list[dict[str, object]]:
    source_rows: object = value
    if isinstance(value, dict):
        source_rows = next(
            (
                value.get(key)
                for key in ("sources", "mentions", "articles", "items", "rows")
                if isinstance(value.get(key), (list, tuple))
            ),
            (),
        )
    if not isinstance(source_rows, (list, tuple)):
        return []
    rows: list[dict[str, object]] = []
    for item in source_rows:
        if not isinstance(item, dict):
            continue
        raw = dict(item)
        compact: dict[str, object] = {}
        if categorized:
            risk_key = _property_workbench_client_evidence_text(
                raw.get("risk_key") or raw.get("key"),
                max_chars=64,
            ).lower()
            if risk_key and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", risk_key):
                compact["risk_key"] = risk_key
        else:
            category = _property_workbench_client_evidence_text(raw.get("category"), max_chars=64)
            if category:
                compact["category"] = category
        for key in _PROPERTY_WORKBENCH_CLIENT_EVIDENCE_TEXT_KEYS:
            text = _property_workbench_client_evidence_text(raw.get(key))
            if text:
                compact[key] = text
        url = _property_workbench_client_evidence_url(
            raw.get("url")
            or raw.get("source_url")
            or raw.get("href")
            or raw.get("original_url")
            or raw.get("article_url")
        )
        if url:
            compact["url"] = url
        if compact:
            rows.append(compact)
        if len(rows) >= max_rows:
            break
    return rows


def _property_workbench_client_facts(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    facts = dict(value)
    compact = {
        key: facts.get(key)
        for key in _PROPERTY_WORKBENCH_CLIENT_FACT_KEYS
        if facts.get(key) not in (None, "", [], {})
    }
    official_rows = _property_workbench_client_evidence_rows(
        facts.get("official_risk_evidence"),
        max_rows=12,
        categorized=True,
    )
    if official_rows:
        compact["official_risk_evidence"] = {"sources": official_rows}
    media_rows = _property_workbench_client_evidence_rows(
        facts.get("media_attention"),
        max_rows=8,
        categorized=False,
    )
    if media_rows:
        compact["media_attention"] = media_rows
    return compact


def _property_workbench_client_tour_payload(
    value: object,
    *,
    fallback_reason: object = "",
    request_kind: str = "tour",
    validated_url: str = "",
    validated_provider_url: str = "",
) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    raw = dict(value)
    compact: dict[str, object] = {
        key: raw.get(key)
        for key in (
            "status",
            "provider",
            "progress_pct",
            "provider_key",
        )
        if raw.get(key) not in (None, "", [], {})
    }
    reason_key = _property_visual_reason_key(
        raw.get("reason_key"),
        raw.get("blocked_reason"),
        raw.get("reason"),
        fallback_reason,
    )
    if reason_key:
        compact["reason_key"] = reason_key
    normalized_kind = "flythrough" if str(request_kind or "").strip().lower() == "flythrough" else "tour"
    if normalized_kind == "flythrough":
        compact["media_kind"] = "camera_walkthrough"
    status = str(raw.get("status") or "").strip().lower()
    generated_reconstruction_ready = bool(
        normalized_kind == "tour"
        and (
            str(raw.get("tour_media_mode") or "").strip().lower() == "generated_reconstruction"
            or str(raw.get("provider_key") or "").strip().lower() == "generated_reconstruction"
            or str(raw.get("label") or raw.get("control_label") or "").strip()
            == "Open AI-generated 3D tour"
        )
    )
    if generated_reconstruction_ready:
        compact["tour_media_mode"] = "generated_reconstruction"
    safe_validated_url = _property_workbench_client_safe_web_or_local_url(validated_url)
    safe_provider_url = (
        _property_workbench_client_provider_viewer_url(validated_provider_url)
        if normalized_kind == "tour"
        else ""
    )
    if safe_validated_url and not _property_workbench_client_url_is_tracking(safe_validated_url):
        compact["url"] = safe_validated_url
        compact["embed_url"] = safe_validated_url
        status = "ready"
        compact["status"] = "ready"
        if normalized_kind == "flythrough":
            compact["customer_claim_ready"] = True
        elif not generated_reconstruction_ready:
            generated_reconstruction_ready = False
            compact.pop("tour_media_mode", None)
    if safe_provider_url:
        compact["provider_url"] = safe_provider_url
    if generated_reconstruction_ready and safe_validated_url:
        status = "ready"
        compact["status"] = "ready"
    if status in _PROPERTY_WORKBENCH_READY_VISUAL_STATUSES and not (safe_validated_url or safe_provider_url):
        status = "unavailable"
        compact["status"] = status
    safe_labels = {
        "3D tour available",
        "3D tour queued",
        "3D tour rendering",
        "3D tour unavailable",
        "Layout tour available",
        "Open 3D tour",
        "Open AI-generated 3D tour",
        "Open layout tour",
        "Open original tour",
        "Original tour",
        "Request 3D tour",
        "Request walkthrough",
        "Retry 3D tour",
        "Retry walkthrough",
        "Camera walkthrough available",
        "Camera walkthrough queued",
        "Camera walkthrough rendering",
        "Walkthrough available",
        "Walkthrough queued",
        "Walkthrough rendering",
    }
    for key in ("label", "control_label"):
        label = str(raw.get(key) or "").strip()
        if label in safe_labels:
            compact[key] = label
    if safe_validated_url:
        compact["label"] = (
            "Camera walkthrough available"
            if normalized_kind == "flythrough"
            else (
                "Open AI-generated 3D tour"
                if generated_reconstruction_ready
                else "3D tour available"
            )
        )
        compact.pop("control_label", None)
    provider_label = str(raw.get("provider_label") or "").strip()
    if provider_label in {
        "3D tour",
        "AI-generated 3D tour",
        "Layout tour",
        "Original tour",
        "Camera walkthrough",
        "Walkthrough",
    }:
        compact["provider_label"] = provider_label
    eta_label = str(raw.get("eta_label") or "").strip().lower()
    active_statuses = {"queued", "pending", "processing", "running", "in_progress", "started", "rendering", "repairing"}
    safe_eta_label = re.fullmatch(r"(?:about [1-9]\d{0,2} min|queued|rendering|refreshing)", eta_label)
    if status in active_statuses and safe_eta_label:
        compact["eta_label"] = eta_label
    if status in {"ready", "created", "existing", "source"}:
        compact["status_detail"] = (
            "Camera walkthrough is ready."
            if normalized_kind == "flythrough"
            else (
                _PROPERTY_GENERATED_TOUR_STATUS_DETAIL
                if generated_reconstruction_ready
                else ("3D tour is ready." if status != "source" else "Original tour is available.")
            )
        )
    elif status in {"queued", "pending"}:
        compact["status_detail"] = "Camera walkthrough queued." if normalized_kind == "flythrough" else "Queued."
    elif status in {"processing", "running", "in_progress", "started", "rendering", "repairing"}:
        compact["status_detail"] = "Camera walkthrough rendering." if normalized_kind == "flythrough" else "Rendering."
    elif status in {"blocked", "failed", "skipped", "not_applicable", "unavailable", "missing"}:
        terminal_detail = (
            {
                "property_tour_video_delivery_failed": "The camera walkthrough could not be published. You can try again.",
            }.get(reason_key, "Camera walkthrough not available yet.")
            if normalized_kind == "flythrough"
            else {
                "listing_expired": "The source listing has expired, and no captured source is available for a 3D tour.",
                "property_tour_delivery_failed": "The 3D tour could not be published. You can try again.",
                "property_tour_execution_failed": "The 3D tour could not be built from the available source media. You can try again.",
            }.get(reason_key, "Tour not available yet.")
        )
        compact["status_detail"] = terminal_detail
    return compact


def _property_workbench_client_run_summary_payload(summary: dict[str, object]) -> dict[str, object]:
    """Project nested run candidates through the customer-safe workbench contract."""

    compact = dict(summary or {})
    candidate_keys = (
        "ranked_candidates",
        "shortlist_candidates",
        "top_candidates",
        "research_candidates",
        "candidates",
    )
    for key in candidate_keys:
        if isinstance(compact.get(key), list):
            compact[key] = [
                _property_workbench_client_candidate_payload(row)
                for row in compact.get(key) or []
                if isinstance(row, dict)
            ]
    safe_sources: list[dict[str, object]] = []
    for source in list(compact.get("sources") or []):
        if not isinstance(source, dict):
            continue
        safe_source = dict(source)
        for key in candidate_keys:
            if isinstance(safe_source.get(key), list):
                safe_source[key] = [
                    _property_workbench_client_candidate_payload(row)
                    for row in safe_source.get(key) or []
                    if isinstance(row, dict)
                ]
        safe_sources.append(safe_source)
    if "sources" in compact:
        compact["sources"] = safe_sources
    return compact


def _property_workbench_client_route_rows(value: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in list(value or [])[:4]:
        if not isinstance(row, dict):
            continue
        raw = dict(row)
        rows.append(
            {
                key: raw.get(key)
                for key in ("label", "distance", "map_url", "icon")
                if raw.get(key) not in (None, "", [], {})
            }
        )
    return rows


def _property_workbench_client_candidate_payload(
    candidate: dict[str, object],
    *,
    preserve_preview_media_facts: bool = False,
    principal_id: str = "",
) -> dict[str, object]:
    raw = dict(candidate or {})
    facts = _property_workbench_client_facts(raw.get("property_facts"))
    raw_facts = dict(raw.get("property_facts") or {}) if isinstance(raw.get("property_facts"), dict) else {}
    if preserve_preview_media_facts:
        raw_floorplans = raw_facts.get("floorplan_urls_json")
        if raw_floorplans in (None, "", [], {}):
            raw_floorplans = raw.get("floorplan_urls_json")
        if isinstance(raw_floorplans, tuple):
            raw_floorplans = list(raw_floorplans)
        if not isinstance(raw_floorplans, list):
            raw_floorplans = []
        safe_floorplans = list(
            dict.fromkeys(
                url
                for url in (
                    _property_workbench_client_floorplan_url(item)
                    for item in list(raw_floorplans or [])[:24]
                )
                if url
            )
        )
        if safe_floorplans:
            facts["floorplan_urls_json"] = safe_floorplans

        raw_media = raw_facts.get("media_urls_json")
        if raw_media in (None, "", [], {}):
            raw_media = raw.get("media_urls_json")
        if isinstance(raw_media, tuple):
            raw_media = list(raw_media)
        if not isinstance(raw_media, list):
            raw_media = []
        safe_media = list(
            dict.fromkeys(
                url
                for url in (
                    _property_workbench_client_image_url(item)
                    for item in list(raw_media or [])[:24]
                )
                if url
            )
        )
        if safe_media:
            facts["media_urls_json"] = safe_media
    derived_facts = _property_candidate_display_facts(raw)
    compact: dict[str, object] = {
        key: raw.get(key)
        for key in (
            "candidate_ref",
            "rank",
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
            "selected_via_link",
            "budget_revalidated",
            "revalidated_from_old_brief",
            "packet_url",
            "property_url",
            "source_url",
            "listing_url",
            "map_url",
            "tour_status",
            "tour_provider",
            "flythrough_status",
        )
        if raw.get(key) not in (None, "", [], {})
    }
    derived_floorplan_url = _property_candidate_floorplan_url(raw, facts=derived_facts)
    if derived_floorplan_url:
        compact["floorplan_url"] = derived_floorplan_url
    ready_tour_url = _property_workbench_candidate_ready_tour_url(
        raw,
        principal_id=principal_id,
    )
    generated_layout_url = _property_workbench_candidate_generated_layout_url(raw)
    if ready_tour_url:
        compact["tour_url"] = ready_tour_url
        compact["verified_tour_url"] = ready_tour_url
        compact["open_tour_url"] = ready_tour_url
    elif generated_layout_url:
        compact["tour_url"] = ""
        compact["verified_tour_url"] = ""
        compact["open_tour_url"] = generated_layout_url
        compact["generated_reconstruction_url"] = generated_layout_url
        compact["layout_preview_url"] = generated_layout_url
        compact["layout_preview_status"] = "ready"
        compact["tour_media_mode"] = "generated_reconstruction"
        if str(compact.get("tour_status") or "").strip().lower() in _PROPERTY_WORKBENCH_READY_VISUAL_STATUSES:
            compact["tour_status"] = "blocked"
    elif str(compact.get("tour_status") or "").strip().lower() in _PROPERTY_WORKBENCH_READY_VISUAL_STATUSES:
        compact["tour_status"] = "unavailable"
    flythrough_url = _property_workbench_candidate_flythrough_url(
        raw,
        ready_tour_url=ready_tour_url,
        principal_id=principal_id,
    )
    if flythrough_url:
        compact["flythrough_url"] = flythrough_url
    elif str(compact.get("flythrough_status") or "").strip().lower() in _PROPERTY_WORKBENCH_READY_VISUAL_STATUSES:
        compact["flythrough_status"] = "unavailable"
    preview_image_url = _property_workbench_client_image_url(raw.get("preview_image_url"))
    if preview_image_url:
        compact["preview_image_url"] = preview_image_url
    orientation_preview = _property_workbench_client_image_payload(raw.get("orientation_preview"))
    if orientation_preview:
        compact["orientation_preview"] = orientation_preview
    diorama_scene = _property_workbench_client_image_payload(raw.get("diorama_scene"))
    if diorama_scene:
        compact["diorama_scene"] = diorama_scene
    diorama_preview_url = _property_workbench_candidate_diorama_preview_url(raw)
    if diorama_preview_url:
        compact["diorama_preview_url"] = diorama_preview_url
    tour_payload = _property_workbench_client_tour_payload(
        raw.get("tour") if isinstance(raw.get("tour"), dict) else {},
        fallback_reason=raw.get("blocked_reason") or raw.get("tour_reason"),
        validated_url=ready_tour_url,
        validated_provider_url="",
    )
    if tour_payload:
        if generated_layout_url:
            # The captured/provider tour remains blocked, but the disclosed
            # AI layout visual itself is ready and has a safe first-party URL.
            tour_payload["status"] = "ready"
            tour_payload["status_detail"] = (
                "AI-generated from the floor plan and listing photos. "
                "It is an interactive spatial aid, not a captured 360° tour."
            )
        compact["tour"] = tour_payload
    flythrough_payload = _property_workbench_client_tour_payload(
        raw.get("flythrough") if isinstance(raw.get("flythrough"), dict) else {},
        fallback_reason=raw.get("flythrough_reason"),
        request_kind="flythrough",
        validated_url=flythrough_url,
    )
    if flythrough_payload:
        compact["flythrough"] = flythrough_payload
    if facts:
        compact["property_facts"] = facts
    match_reasons = [str(item).strip() for item in list(raw.get("match_reasons") or []) if str(item).strip()]
    if match_reasons:
        compact["match_reasons"] = match_reasons[:3]
    route_rows = _property_workbench_client_route_rows(raw.get("route_evidence"))
    if route_rows:
        compact["route_evidence"] = route_rows
    return compact


_PROPERTY_MANAGEMENT_CANDIDATE_KEYS = (
    "candidate_ref",
    "source_ref",
    "title",
    "summary",
    "detail",
    "fit_summary",
    "compare_reason",
    "recommendation",
    "source_label",
    "source_short_label",
    "source_platform",
    "location_label",
    "price_display",
    "rent_display",
    "layout_display",
    "fit_score",
    "personal_fit_score",
    "fit_label",
    "score",
    "ranking_score",
    "confidence",
    "status",
    "status_label",
    "status_detail",
    "tag",
    "match_reasons",
    "mismatch_reasons",
)

_PROPERTY_MANAGEMENT_FACT_KEYS = (
    "price_display",
    "rent_display",
    "price_per_sqm_display",
    "currency_code",
    "price",
    "rent",
    "price_eur",
    "rent_eur",
    "total_rent_eur",
    "monthly_rent_eur",
    "purchase_price_eur",
    "area_sqm",
    "area_m2",
    "living_area_m2",
    "rooms",
    "floor",
    "postal_name",
    "district",
    "city",
)


def _property_management_safe_scalar(value: object) -> object | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    text_value = str(value or "").strip()
    if not text_value:
        return None
    lowered = text_value.casefold()
    if (
        "://" in lowered
        or lowered.startswith(("data:", "blob:", "file:", "javascript:", "/app/", "/v1/"))
    ):
        return None
    return text_value


def _property_management_candidate_payload(candidate: dict[str, object]) -> dict[str, object]:
    raw_candidate = dict(candidate or {})
    client_candidate = _property_workbench_client_candidate_payload(candidate)
    compact: dict[str, object] = {}
    for key in _PROPERTY_MANAGEMENT_CANDIDATE_KEYS:
        value = raw_candidate.get(key)
        if key in {"match_reasons", "mismatch_reasons"}:
            rows = [
                safe_value
                for item in list(value or [])
                if (safe_value := _property_management_safe_scalar(item)) is not None
            ]
            if rows:
                compact[key] = rows[:3]
            continue
        safe_value = _property_management_safe_scalar(value)
        if safe_value is not None:
            compact[key] = safe_value
    raw_facts = (
        dict(client_candidate.get("property_facts") or {})
        if isinstance(client_candidate.get("property_facts"), dict)
        else {}
    )
    safe_facts = {
        key: safe_value
        for key in _PROPERTY_MANAGEMENT_FACT_KEYS
        if (safe_value := _property_management_safe_scalar(raw_facts.get(key))) is not None
    }
    if safe_facts:
        compact["property_facts"] = safe_facts
    return compact


def _property_management_route_payload(route: dict[str, object]) -> dict[str, object]:
    compact: dict[str, object] = {}
    for key in ("title", "label", "detail", "mode_label", "route_distance_m", "route_duration_min"):
        safe_value = _property_management_safe_scalar(route.get(key))
        if safe_value is not None:
            compact[key] = safe_value
    return compact


def _property_management_recent_packet_payload(packet: dict[str, object]) -> dict[str, object]:
    compact: dict[str, object] = {}
    for key in ("title", "detail", "tag"):
        safe_value = _property_management_safe_scalar(packet.get(key))
        if safe_value is not None:
            compact[key] = safe_value
    return compact


def _property_management_run_summary_payload(summary: dict[str, object]) -> dict[str, object]:
    raw_summary = dict(summary or {})
    compact: dict[str, object] = {}
    for key in (
        "status",
        "listing_total",
        "raw_listing_total",
        "found_listing_total",
        "reviewed_listing_total",
        "scanned_listing_total",
        "to_review_listing_total",
        "ranked_total",
        "ranked_candidate_total",
        "filtered_total",
        "held_back_total",
        "score_demoted_total",
        "filtered_low_fit_total",
        "provider_total",
        "provider_display_total",
        "source_variant_total",
        "source_variant_display_total",
        "sources_total",
        "sources_completed",
        "completed_sources",
        "notified_total",
        "current_plan_key",
        "current_step",
        "repair_status",
        "repair_status_label",
    ):
        safe_value = _property_management_safe_scalar(raw_summary.get(key))
        if safe_value is not None:
            compact[key] = safe_value
    safe_sources: list[dict[str, object]] = []
    for source in list(raw_summary.get("sources") or []):
        if not isinstance(source, dict):
            continue
        safe_source: dict[str, object] = {}
        for key in (
            "source_label",
            "platform",
            "provider_family",
            "status",
            "source_status",
            "status_label",
            "listing_total",
            "scanned_listing_total",
            "ranked_total",
            "ranked_candidate_total",
            "high_fit_total",
            "filtered_low_fit_total",
        ):
            safe_value = _property_management_safe_scalar(source.get(key))
            if safe_value is not None:
                safe_source[key] = safe_value
        if safe_source:
            safe_sources.append(safe_source)
    if safe_sources:
        compact["sources"] = safe_sources
    return compact


def _property_workbench_client_run_payload(run_payload: dict[str, object]) -> dict[str, object]:
    raw_run = dict(run_payload or {})
    raw_summary = dict(raw_run.get("summary") or {}) if isinstance(raw_run.get("summary"), dict) else {}
    summary_keys = (
        "status",
        "listing_total",
        "raw_listing_total",
        "found_listing_total",
        "reviewed_listing_total",
        "scanned_listing_total",
        "to_review_listing_total",
        "ranked_total",
        "ranked_candidate_total",
        "filtered_total",
        "held_back_total",
        "score_demoted_total",
        "filtered_low_fit_total",
        "provider_total",
        "provider_display_total",
        "source_variant_total",
        "source_variant_display_total",
        "sources_total",
        "sources_completed",
        "completed_sources",
        "current_plan_key",
        "max_results_per_source",
        "current_step",
        "repair_status",
        "repair_status_label",
        "repair_replacement_run_id",
    )
    compact_summary = {
        key: raw_summary.get(key)
        for key in summary_keys
        if raw_summary.get(key) not in (None, "", [], {})
    }
    safe_run_message = property_run_customer_safe_status_detail(
        raw_run.get("status") or raw_summary.get("status"),
        raw_run.get("message") or raw_summary.get("message") or "",
        summary=raw_summary,
        prefer_repair_step=True,
    )
    compact_run = {
        key: raw_run.get(key)
        for key in (
            "run_id",
            "status",
            "status_label",
            "progress",
            "status_url",
            "eta_label",
            "current_step",
            "provider_display_total",
            "source_variant_display_total",
            "selected_platform_count",
            "filtered_total",
            "held_back_total",
            "score_demoted_total",
        )
        if raw_run.get(key) not in (None, "", [], {})
    }
    if safe_run_message:
        compact_run["message"] = safe_run_message
    elif raw_run.get("message") not in (None, "", [], {}):
        compact_run["message"] = str(raw_run.get("message") or raw_summary.get("message") or "").strip()
    if compact_summary:
        compact_run["summary"] = compact_summary
    events = [dict(row) for row in list(raw_run.get("events") or []) if isinstance(row, dict)]
    if events:
        compact_run["events"] = events[-10:]
    route_previews = [dict(row) for row in list(raw_run.get("route_previews") or []) if isinstance(row, dict)]
    if route_previews:
        compact_run["route_previews"] = route_previews[:3]
    current_property = dict(raw_run.get("current_property") or {}) if isinstance(raw_run.get("current_property"), dict) else {}
    if current_property:
        compact_current_property = _property_workbench_client_candidate_payload(current_property)
        for key in ("status_label", "status_detail"):
            if current_property.get(key) not in (None, "", [], {}):
                compact_current_property[key] = current_property.get(key)
        detail_url = str(current_property.get("detail_url") or "").strip()
        if detail_url.startswith("/app/research/") and not detail_url.startswith("//"):
            compact_current_property["detail_url"] = detail_url
        if compact_current_property:
            compact_run["current_property"] = compact_current_property
    reliability = dict(raw_run.get("reliability") or {}) if isinstance(raw_run.get("reliability"), dict) else {}
    if reliability:
        compact_run["reliability"] = {
            key: reliability.get(key)
            for key in ("health_label", "health_tone", "customer_status_message", "coverage_label", "result_label")
            if reliability.get(key) not in (None, "", [], {})
        }
    return compact_run


def _compact_property_account_status(status: dict[str, object]) -> dict[str, object]:
    """Keep authenticated account UI state without carrying raw preference/run blobs."""
    raw_status = dict(status or {})
    raw_workspace = dict(raw_status.get("workspace") or {}) if isinstance(raw_status.get("workspace"), dict) else {}
    raw_channels = dict(raw_status.get("channels") or {}) if isinstance(raw_status.get("channels"), dict) else {}
    raw_delivery_preferences = (
        dict(raw_status.get("delivery_preferences") or {})
        if isinstance(raw_status.get("delivery_preferences"), dict)
        else {}
    )
    raw_property_notifications = (
        dict(raw_delivery_preferences.get("property_notifications") or {})
        if isinstance(raw_delivery_preferences.get("property_notifications"), dict)
        else {}
    )
    raw_telegram_bot = (
        dict(raw_property_notifications.get("telegram_bot") or {})
        if isinstance(raw_property_notifications.get("telegram_bot"), dict)
        else {}
    )
    raw_telegram_channel = (
        dict(raw_channels.get("telegram") or {})
        if isinstance(raw_channels.get("telegram"), dict)
        else {}
    )
    raw_channel_bot = (
        dict(raw_telegram_channel.get("product_bot") or {})
        if isinstance(raw_telegram_channel.get("product_bot"), dict)
        else {}
    )
    raw_google_channel = (
        dict(raw_channels.get("google") or {})
        if isinstance(raw_channels.get("google"), dict)
        else {}
    )

    def _copy_keys(source: dict[str, object], keys: tuple[str, ...]) -> dict[str, object]:
        return {key: source.get(key) for key in keys if source.get(key) not in (None, "", [], {})}

    telegram_bot = _copy_keys(
        {**raw_channel_bot, **raw_telegram_bot},
        ("display_handle", "connect_url", "status_label", "status"),
    )
    property_notifications = _copy_keys(
        raw_property_notifications,
        (
            "preferred_channel",
            "selected_channels",
            "whatsapp_ai_support_phone",
            "email_enabled",
            "telegram_enabled",
            "whatsapp_enabled",
        ),
    )
    if telegram_bot:
        property_notifications["telegram_bot"] = telegram_bot

    compact_channels: dict[str, object] = {}
    if raw_google_channel:
        compact_channels["google"] = _copy_keys(
            raw_google_channel,
            (
                "status",
                "status_label",
                "connected_account_email",
                "primary_account_email",
                "account_email",
            ),
        )
    if raw_telegram_channel or telegram_bot:
        compact_channels["telegram"] = {
            **_copy_keys(raw_telegram_channel, ("status", "status_label")),
            **({"product_bot": telegram_bot} if telegram_bot else {}),
        }
    for channel_key in ("email", "whatsapp"):
        raw_channel = raw_channels.get(channel_key)
        if isinstance(raw_channel, dict):
            compact_channels[channel_key] = _copy_keys(raw_channel, ("status", "status_label"))

    return {
        "workspace": _copy_keys(raw_workspace, ("name", "timezone")),
        "channels": compact_channels,
        "delivery_preferences": {"property_notifications": property_notifications},
    }


def _property_distance_evidence_row(
    facts: dict[str, object],
    *,
    label: str,
    distance_keys: tuple[str, ...],
    name_keys: tuple[str, ...] = (),
    source_keys: tuple[str, ...] = (),
) -> dict[str, str]:
    raw_value: object = None
    for key in distance_keys:
        candidate = facts.get(key)
        if candidate not in (None, "", []):
            raw_value = candidate
            break
    if raw_value in (None, "", []):
        return {}
    try:
        meters = int(float(raw_value))
    except Exception:
        return {}
    if meters <= 0:
        return {}

    name = ""
    for key in name_keys:
        value = str(facts.get(key) or "").strip()
        if value:
            name = value
            break
    source = ""
    for key in source_keys:
        value = str(facts.get(key) or "").strip()
        if value:
            source = value
            break

    bike_minutes = max(1, int(round(float(meters) / 330.0)))
    value = f"{meters} m"
    title = f"{label}: {name}" if name else label
    detail_parts = [f"about {bike_minutes} min by bike"]
    if source:
        detail_parts.append(f"source: {source}")
    inline = f"{label} {name} {value}" if name else f"{label} {value}"
    return {
        "label": label,
        "title": title,
        "value": value,
        "detail": " | ".join(detail_parts),
        "inline": f"{inline} | {bike_minutes} min bike",
    }


_PROPERTY_DISTANCE_EVIDENCE_SPECS: tuple[dict[str, object], ...] = (
    {
        "label": "Playground",
        "distance_keys": ("nearest_playground_m", "distance_playground_m"),
        "name_keys": ("nearest_playground_name", "playground_name"),
        "source_keys": ("nearest_playground_source", "playground_source"),
        "family_only": True,
    },
    {
        "label": "Library",
        "distance_keys": ("nearest_library_m",),
        "name_keys": ("nearest_library_name", "library_name"),
        "source_keys": ("nearest_library_source", "library_source"),
        "family_only": True,
    },
    {
        "label": "Zoo",
        "distance_keys": ("nearest_zoo_m",),
        "name_keys": ("nearest_zoo_name", "zoo_name"),
        "source_keys": ("nearest_zoo_source", "zoo_source"),
        "family_only": True,
    },
    {
        "label": "Pharmacy",
        "distance_keys": ("nearest_pharmacy_m", "distance_pharmacy_m"),
        "name_keys": ("nearest_pharmacy_name", "pharmacy_name"),
        "source_keys": ("nearest_pharmacy_source", "pharmacy_source"),
        "family_only": False,
    },
    {
        "label": "Medical care",
        "distance_keys": ("nearest_medical_care_m",),
        "name_keys": ("nearest_medical_care_name", "medical_care_name"),
        "source_keys": ("nearest_medical_care_source", "medical_care_source"),
        "family_only": True,
    },
    {
        "label": "Supermarket",
        "distance_keys": ("nearest_supermarket_m", "distance_supermarket_m"),
        "name_keys": ("nearest_supermarket_name", "supermarket_name"),
        "source_keys": ("nearest_supermarket_source", "supermarket_source"),
        "family_only": False,
    },
    {
        "label": "Market",
        "distance_keys": ("nearest_market_m",),
        "name_keys": ("nearest_market_name", "market_name"),
        "source_keys": ("nearest_market_source", "market_source"),
        "family_only": False,
    },
    {
        "label": "Baumarkt",
        "distance_keys": ("nearest_hardware_store_m",),
        "name_keys": ("nearest_hardware_store_name", "hardware_store_name"),
        "source_keys": ("nearest_hardware_store_source", "hardware_store_source"),
        "family_only": False,
    },
    {
        "label": "Starbucks",
        "distance_keys": ("nearest_starbucks_m",),
        "name_keys": ("nearest_starbucks_name", "starbucks_name"),
        "source_keys": ("nearest_starbucks_source", "starbucks_source"),
        "family_only": False,
    },
    {
        "label": "Fitness",
        "distance_keys": ("nearest_fitness_center_m",),
        "name_keys": ("nearest_fitness_center_name", "fitness_center_name"),
        "source_keys": ("nearest_fitness_center_source", "fitness_center_source"),
        "family_only": False,
    },
    {
        "label": "Run or green space",
        "distance_keys": ("nearest_running_m",),
        "name_keys": ("nearest_running_name", "running_route_name", "nearest_green_space_name"),
        "source_keys": ("nearest_running_source", "running_route_source", "nearest_green_space_source"),
        "family_only": False,
    },
    {
        "label": "Straßenbahn / Bus",
        "distance_keys": ("nearest_tram_bus_m", "nearest_transit_m"),
        "name_keys": ("nearest_tram_bus_name", "nearest_transit_name", "transit_stop_name"),
        "source_keys": ("nearest_tram_bus_source", "nearest_transit_source", "transit_source"),
        "family_only": False,
    },
    {
        "label": "Underground",
        "distance_keys": ("nearest_subway_m", "distance_underground_m"),
        "name_keys": ("nearest_subway_name", "subway_station_name"),
        "source_keys": ("nearest_subway_source", "subway_source"),
        "family_only": False,
    },
)


def _property_distance_evidence_rows(
    facts: dict[str, object],
    *,
    include_family_only: bool,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for spec in _PROPERTY_DISTANCE_EVIDENCE_SPECS:
        if bool(spec.get("family_only")) and not include_family_only:
            continue
        row = _property_distance_evidence_row(
            facts,
            label=str(spec.get("label") or "").strip(),
            distance_keys=tuple(str(item) for item in spec.get("distance_keys", ()) if str(item).strip()),
            name_keys=tuple(str(item) for item in spec.get("name_keys", ()) if str(item).strip()),
            source_keys=tuple(str(item) for item in spec.get("source_keys", ()) if str(item).strip()),
        )
        if row:
            rows.append(row)
    return rows


def _compact_property_run_payload_for_template(run_payload: dict[str, object]) -> dict[str, object]:
    raw_run = dict(run_payload or {})
    raw_summary = dict(raw_run.get("summary") or {}) if isinstance(raw_run.get("summary"), dict) else {}
    compact_summary = {
        key: raw_summary.get(key)
        for key in (
            "status",
            "listing_total",
            "reviewed_listing_total",
            "ranked_total",
            "filtered_total",
            "held_back_total",
            "provider_total",
            "source_variant_total",
            "sources_total",
        )
        if raw_summary.get(key) not in (None, "", [], {})
    }
    return {
        key: raw_run.get(key)
        for key in ("run_id", "status", "status_label", "progress", "message", "status_url", "eta_label")
        if raw_run.get(key) not in (None, "", [], {})
    } | ({"summary": compact_summary} if compact_summary else {})


def _property_provider_identity_key(source_spec: dict[str, object]) -> str:
    provider_source_key = str(source_spec.get("provider_source_key") or source_spec.get("source_provider_key") or "").strip()
    if provider_source_key:
        candidate = provider_source_key.split(":", 1)[0].strip().casefold()
        if candidate:
            return candidate
    for key in ("provider_key", "platform", "provider_family", "label", "source_label"):
        raw_value = str(source_spec.get(key) or "").strip()
        if key in {"label", "source_label"} and "|" in raw_value:
            raw_value = raw_value.split("|", 1)[0].strip()
        normalized = raw_value.lower()
        if normalized:
            return normalized
    return ""


def _property_provider_total(source_rows: list[dict[str, object]]) -> int:
    provider_keys: set[str] = set()
    for row in source_rows:
        key = _property_provider_identity_key(row)
        if key:
            provider_keys.add(key)
    return len(provider_keys)


def _property_source_scope_mismatch_notice(
    *,
    preferences: dict[str, object],
    run_summary: dict[str, object],
    source_rows: list[dict[str, object]],
) -> dict[str, object]:
    if not bool(preferences.get("full_region_scope")):
        return {}
    target_label = str(preferences.get("location_query") or "").strip()
    if not target_label:
        return {}

    def _scope_key(value: object) -> str:
        normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
        if normalized in {"wien", "vienna"}:
            return "vienna"
        return normalized

    target_key = _scope_key(target_label)
    if not target_key:
        return {}
    source_locations: list[str] = []
    for source in source_rows:
        pushdown = dict(source.get("provider_filter_pushdown") or {}) if isinstance(source.get("provider_filter_pushdown"), dict) else {}
        for bucket_name in ("applied", "requested", "attempted"):
            bucket = dict(pushdown.get(bucket_name) or {}) if isinstance(pushdown.get(bucket_name), dict) else {}
            location = str(bucket.get("location_query") or "").strip()
            if location and location not in source_locations:
                source_locations.append(location)
        source_location = str(source.get("location_query") or "").strip()
        if source_location and source_location not in source_locations:
            source_locations.append(source_location)
    mismatched_locations = [
        location
        for location in source_locations
        if _scope_key(location)
        and _scope_key(location) != target_key
        and not (
            re.search(r"\b[1-9]\d{3,4}\b", target_key)
            and _scope_key(location) in target_key
        )
    ]
    if not mismatched_locations:
        return {}
    try:
        previous_total = max(
            int(float(run_summary.get("filtered_total") or 0)),
            int(float(run_summary.get("held_back_total") or 0)),
        )
    except Exception:
        previous_total = 0
    checked_label = ", ".join(mismatched_locations[:3])
    return {
        "title": "Search used an older area",
        "rule_key": "Brief scope changed",
        "detail": (
            f"This search checked {checked_label}. Your search is {target_label}. "
            "Start an updated search so the results are fetched for the right area."
        ),
        "tag": "Updated brief",
        "affected_total": 0,
        "previous_filtered_total": previous_total,
        "action_label": "Start updated search",
    }


def _property_run_brief_is_old_snapshot(
    *,
    run_payload: dict[str, object],
    run_summary: dict[str, object],
) -> bool:
    return bool(
        run_payload.get("brief_preferences_stale")
        or run_payload.get("stale_run_snapshot")
        or run_summary.get("brief_preferences_stale")
        or str(run_summary.get("brief_snapshot_status") or "").strip().lower() == "old_run"
    )


def _property_old_brief_snapshot_notice(
    *,
    run_summary: dict[str, object],
) -> dict[str, object]:
    if not bool(
        run_summary.get("brief_preferences_stale")
        or str(run_summary.get("brief_snapshot_status") or "").strip().lower() == "old_run"
    ):
        return {}
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
    message = str(run_summary.get("brief_stale_message") or "").strip() or (
        "This search used an earlier brief. Start an updated search to refresh counts "
        "with your current budget, area, lists, and requirements."
    )
    return {
        "title": "Search used an earlier brief",
        "rule_key": "Older search snapshot",
        "detail": message,
        "tag": "Older search",
        "affected_total": 0,
        "previous_filtered_total": previous_filtered_total,
        "previous_ranked_total": previous_ranked_total,
        "action_label": str(run_summary.get("brief_stale_action_label") or "Start updated search").strip() or "Start updated search",
    }


def property_workspace_payload(
    section: str,
    *,
    status: dict[str, object],
    property_state: dict[str, object],
) -> dict[str, object]:
    from app.api.routes.landing_view_models import (
        _clean_property_candidate_copy,
        _clean_property_candidate_detail_copy,
        _csv_values,
        _normalize_property_type_values,
        _property_customer_run_summary,
        _property_candidate_ref,
        _property_preference_schema,
        _property_result_title_display,
        _property_scope_preview,
        _property_scope_preview_map_only,
        _sanitize_platform_catalog_for_client,
        app_section_payload,
        humanize,
        row_item,
        string_rows,
    )
    from app.api.routes.landing_property_research import _property_normalized_mismatch_reasons
    from app.services.property_market_catalog import (
        currency_code_for_country,
        default_timezone_for_country,
        supported_currency_codes,
    )
    from app.services.property_customer_copy import summarize_property_description_copy

    surface_scope = PropertySurfaceScope.for_section(section)
    normalized_section = surface_scope.section
    workspace_principal_id = str(property_state.get("_principal_id") or "").strip()
    wants_recent_runs = surface_scope.wants_recent_runs
    wants_search_runs = surface_scope.wants_search_runs
    wants_agent_views = surface_scope.wants_agent_views
    wants_credit_digest = surface_scope.wants_credit_digest
    wants_run_views = surface_scope.wants_run_views
    # Account first paint must stay compact. Full preference editing belongs
    # to the dedicated search/settings flow, not the mobile account surface.
    wants_full_preference_manager = False
    base = app_section_payload("properties", status, live_feed=(), property_context=property_state)
    cards = list(base.get("cards") or [])
    cards_by_eyebrow = {
        str(card.get("eyebrow") or "").strip().lower(): dict(card)
        for card in cards
        if isinstance(card, dict)
    }
    cards_by_title = {
        str(card.get("title") or "").strip().lower(): dict(card)
        for card in cards
        if isinstance(card, dict)
    }
    property_form = dict(base.get("console_form") or {})
    property_meta = dict(property_form.get("meta") or {})
    property_search_agents = [
        dict(agent)
        for agent in list(property_meta.get("search_agents") or [])
        if isinstance(agent, dict)
    ]
    property_search_agent = next((agent for agent in property_search_agents if agent.get("is_active")), property_search_agents[0] if property_search_agents else {})
    requested_property_agent_id = str(property_state.get("selected_agent_id") or "").strip()

    def _compact_scope_preview_payload(row: dict[str, object]) -> None:
        scope_preview = dict(row.get("scope_preview") or {})
        if not scope_preview:
            return
        compact_rows = []
        for preview_row in list(scope_preview.get("district_rows") or []):
            if not isinstance(preview_row, dict):
                continue
            compact_rows.append(
                {
                    "label": str(preview_row.get("label") or "").strip(),
                    "selected": bool(preview_row.get("selected")),
                }
            )
        scope_preview["district_rows"] = compact_rows
        scope_preview["district_overlay_svg"] = ""
        row["scope_preview"] = scope_preview

    provider_options = []
    for field in list(property_form.get("schema") or []):
        if not isinstance(field, dict):
            continue
        if str(field.get("name") or "").strip() != "selected_platforms":
            continue
        provider_options = [dict(option) for option in list(field.get("options") or []) if isinstance(option, dict)]
        break
    commercial = dict(property_state.get("commercial") or {})
    billing_truth = dict(property_state.get("billing_truth") or {})
    billing_handoff = dict(property_state.get("billing_handoff") or {})
    billing_handoff_available = bool(billing_handoff.get("available"))
    billing_handoff_status = str(billing_handoff.get("status") or "").strip().lower()
    billing_handoff_bridge_only = billing_handoff_status in {"bridge_ready", "member_token_ready"}
    billing_primary_action_label = "Open billing" if billing_handoff_available else "Billing"
    saved_property_preferences = dict(property_state.get("preferences") or {})
    preference_bundle = dict(property_state.get("preference_bundle") or {})
    if (wants_agent_views or (wants_search_runs and requested_property_agent_id)) and not property_search_agents:
        raw_saved_agents = [
            dict(agent)
            for agent in list(saved_property_preferences.get("search_agents") or [])
            if isinstance(agent, dict)
        ]
        normalized_saved_agents: list[dict[str, object]] = []
        for index, raw_agent in enumerate(raw_saved_agents):
            agent_row = dict(raw_agent)
            agent_id = str(agent_row.get("agent_id") or "current").strip() or "current"
            scope_label = str(
                agent_row.get("scope_label")
                or agent_row.get("location_query")
                or agent_row.get("area_label")
                or "No scope saved"
            ).strip() or "No scope saved"
            enabled = bool(agent_row.get("enabled"))
            is_active = bool(agent_row.get("is_active"))
            if not is_active and requested_property_agent_id and agent_id == requested_property_agent_id:
                is_active = True
            if not is_active and not requested_property_agent_id and index == 0:
                is_active = True
            agent_row.setdefault("agent_id", agent_id)
            agent_row.setdefault("scope_label", scope_label)
            agent_row.setdefault("status_label", "Active" if enabled else "Paused")
            agent_row.setdefault("delivery_label", "Set a daily or weekly cap.")
            agent_row.setdefault("notification_label", "Budget")
            agent_row.setdefault("run_label", "Waiting for the first search.")
            agent_row["is_active"] = is_active
            normalized_saved_agents.append(agent_row)
        if normalized_saved_agents:
            property_search_agents = normalized_saved_agents
            property_search_agent = next(
                (agent for agent in property_search_agents if agent.get("is_active")),
                property_search_agents[0],
            )
    for agent in property_search_agents:
        _compact_scope_preview_payload(agent)
    raw_preference_nodes = (
        [
            dict(row)
            for row in list(preference_bundle.get("preference_nodes") or [])
            if isinstance(row, dict)
        ]
        if preference_bundle
        else []
    )
    workspace = dict(status.get("workspace") or {})
    channels = dict(status.get("channels") or {})
    google = dict(channels.get("google") or {})
    current_plan_label = str(billing_truth.get("current_plan_label") or commercial.get("current_plan_label") or "Free").strip() or "Free"
    try:
        current_platform_cap = int(
            billing_truth.get("max_platforms")
            if billing_truth.get("max_platforms") is not None
            else (commercial.get("max_platforms") if commercial.get("max_platforms") is not None else 3)
        )
    except Exception:
        current_platform_cap = 3
    search_posture_card = cards_by_eyebrow.get("search brief") or cards_by_eyebrow.get("search posture", {})
    market_coverage_card = cards_by_eyebrow.get("market coverage", {})
    shortlist_card = cards_by_eyebrow.get("shortlist", {})
    run_card = cards_by_eyebrow.get("search status", {})
    learning_card = cards_by_eyebrow.get("learning loop", {})
    recent_matches_card = cards_by_eyebrow.get("recent matches", {})
    shortlist_candidates = [
        dict(candidate)
        for candidate in list(property_meta.get("shortlist_candidates") or [])
        if isinstance(candidate, dict)
    ]
    def _workspace_candidate_ref(candidate: dict[str, object]) -> str:
        restored_ref = str(candidate.get("_selected_candidate_ref") or "").strip().lower()
        if (
            bool(candidate.get("_explicitly_selected_source_candidate"))
            and re.fullmatch(r"[0-9a-f]{16}", restored_ref) is not None
        ):
            return restored_ref
        return _property_candidate_ref(candidate)

    def _shortlist_identity(candidate: dict[str, object]) -> str:
        facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
        for value in (
            candidate.get("_selected_candidate_ref"),
            candidate.get("candidate_ref"),
            candidate.get("property_ref"),
            candidate.get("property_url"),
            candidate.get("source_url"),
            candidate.get("review_url"),
            facts.get("listing_url"),
            candidate.get("source_ref"),
            candidate.get("listing_id"),
        ):
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        title = str(candidate.get("title") or "").strip()
        return title.casefold() if title else ""

    def _dedupe_shortlist_candidates(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        deduped: list[dict[str, object]] = []
        seen: set[str] = set()
        anonymous_index = 0
        for row in rows:
            candidate = dict(row)
            identity = _shortlist_identity(candidate)
            if not identity:
                anonymous_index += 1
                identity = f"row:{anonymous_index}"
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(candidate)
        return deduped
    if normalized_section in {"properties", "search", "shortlist", "research", "agents", "account", "settings", "billing"}:
        trimmed_meta = dict(property_meta)
        if normalized_section in {"properties", "search", "shortlist", "research", "account", "settings", "billing"}:
            trimmed_meta.pop("search_agent", None)
            trimmed_meta.pop("search_agents", None)
        trimmed_meta.pop("initial_run", None)
        trimmed_meta.pop("shortlist_candidates", None)
        property_form["meta"] = trimmed_meta
        property_meta = trimmed_meta
    run_payload = dict(property_state.get("run") or {})
    run_property_preferences = dict(run_payload.get("property_search_preferences") or {}) if isinstance(run_payload.get("property_search_preferences"), dict) else {}
    run_summary_for_preferences = (
        dict(run_payload.get("summary") or {})
        if isinstance(run_payload.get("summary"), dict)
        else {}
    )
    run_brief_is_old_snapshot = _property_run_brief_is_old_snapshot(
        run_payload=run_payload,
        run_summary=run_summary_for_preferences,
    )
    property_preferences = (
        dict(saved_property_preferences)
        if normalized_section in {"search", "agents", "alerts", "account", "billing", "settings"} or run_brief_is_old_snapshot
        else {**saved_property_preferences, **run_property_preferences}
    )
    preference_person_id = str(property_state.get("preference_person_id") or property_preferences.get("preference_person_id") or "self").strip() or "self"
    brief_preferences_payload = dict(property_preferences)
    for heavy_key in (
        "raw_preferences",
        "saved_shortlist_candidates",
        "search_agents",
        "property_commercial",
        "preference_bundle",
    ):
        brief_preferences_payload.pop(heavy_key, None)
    if normalized_section in {"agents", "account", "settings", "billing"}:
        static_brief_keys = {
            "country_code",
            "region_code",
            "location_query",
            "listing_mode",
            "property_type",
            "property_types",
            "furniture_style",
            "search_goal",
            "investment_strategy",
            "keywords",
            "selected_platforms",
        }
        brief_preferences_payload = {
            key: value
            for key, value in brief_preferences_payload.items()
            if key in static_brief_keys
        }
    run_health = dict(property_state.get("run_health") or {})
    packet_recovery = dict(property_state.get("packet_recovery") or {})
    route_recovery = dict(property_state.get("route_recovery") or {})
    selected_candidate_ref = str(
        property_state.get("selected_candidate_ref") or ""
    ).strip()
    run_events = property_run_customer_visible_events(run_payload=run_payload)
    raw_run_summary = dict(run_payload.get("summary") or {})
    run_summary = _property_customer_run_summary(raw_run_summary, preferences=property_preferences)
    run_status_value = str(run_health.get("status") or run_payload.get("status") or "").strip().lower()
    raw_run_message = str(run_health.get("message") or run_payload.get("message") or "").strip()
    safe_run_message = property_run_customer_safe_status_detail(
        run_status_value,
        raw_run_message,
        summary=run_summary,
        prefer_repair_step=True,
    )
    run_eta_label = property_run_public_eta_label(
        run_health.get("eta_label") or run_summary.get("eta_label") or run_payload.get("eta_label")
    )
    run_next_useful_eta_label = property_run_public_eta_label(run_summary.get("next_useful_update_eta_label"))
    run_summary = dict(run_summary)
    if run_eta_label:
        run_summary["eta_label"] = run_eta_label
    else:
        run_summary.pop("eta_label", None)
    if run_next_useful_eta_label:
        run_summary["next_useful_update_eta_label"] = run_next_useful_eta_label
    else:
        run_summary.pop("next_useful_update_eta_label", None)

    def _plan_label_from_key(plan_key: object) -> str:
        normalized_plan_key = normalize_property_plan_key(plan_key or "free")
        return {
            "free": "Free",
            "plus": "Plus",
            "agent": "Agent",
        }.get(normalized_plan_key, normalized_plan_key.replace("_", " ").title() or "Free")

    ambient_plan_key = normalize_property_plan_key(billing_truth.get("current_plan_key") or commercial.get("current_plan_key") or "free")
    run_plan_key = normalize_property_plan_key(run_summary.get("current_plan_key") or "")
    effective_run_plan_key = run_plan_key or ambient_plan_key or "free"
    effective_run_plan_label = str(run_summary.get("current_plan_label") or "").strip()
    if not effective_run_plan_label:
        if effective_run_plan_key == ambient_plan_key and current_plan_label:
            effective_run_plan_label = current_plan_label
        else:
            effective_run_plan_label = _plan_label_from_key(effective_run_plan_key)
    effective_run_research_depth = str(run_summary.get("research_depth") or billing_truth.get("research_depth") or commercial.get("research_depth") or "standard").strip() or "standard"
    run_payload = {
        **run_payload,
        "summary": run_summary,
        "message": str(run_health.get("status_note") or safe_run_message or run_health.get("message") or run_payload.get("message") or "").strip(),
        "eta_label": run_eta_label,
        "events": run_events,
    }

    def _run_event_customer_label(event: dict[str, object]) -> str:
        step = str(event.get("step") or "").strip().lower()
        if step in {"status_refresh", "search", "provider_search", "source_search"}:
            return "Search update"
        if step in {"source_detail_check", "listing_detail_check", "source_fetch", "source_fetch_page"}:
            return "Checking details"
        if step == "source_shortlist":
            return "Matching homes"
        if step == "source_review_packet":
            return "Property page ready"
        if step.startswith("repair"):
            return "Source follow-up"
        return "Update"

    def _compact_property_surface_run_summary(summary: dict[str, object]) -> dict[str, object]:
        safe_summary = dict(summary)
        safe_summary.pop("ranked_candidates", None)
        safe_summary.pop("candidates", None)
        safe_summary.pop("shortlist_candidates", None)
        safe_sources: list[dict[str, object]] = []
        for source in list(safe_summary.get("sources") or []):
            if not isinstance(source, dict):
                continue
            safe_source = dict(source)
            safe_source.pop("top_candidates", None)
            safe_source.pop("ranked_candidates", None)
            safe_sources.append(safe_source)
        safe_summary["sources"] = safe_sources
        return safe_summary

    management_surface = normalized_section in {"agents", "account", "settings", "billing"}
    compact_summary_surface = management_surface or normalized_section == "properties"
    run_summary_for_surface = (
        _property_management_run_summary_payload(run_summary)
        if management_surface
        else (_compact_property_surface_run_summary(run_summary) if normalized_section == "properties" else run_summary)
    )
    run_payload_for_surface = {**run_payload, "summary": run_summary_for_surface} if compact_summary_surface else run_payload
    run_sources = [dict(row) for row in list(run_summary.get("sources") or []) if isinstance(row, dict)]
    raw_run_sources = [dict(row) for row in list(raw_run_summary.get("sources") or []) if isinstance(row, dict)]
    old_brief_snapshot_notice = _property_old_brief_snapshot_notice(run_summary=run_summary)
    source_scope_mismatch_notice = (
        {}
        if old_brief_snapshot_notice
        else _property_source_scope_mismatch_notice(
            preferences=property_preferences,
            run_summary=run_summary,
            source_rows=run_sources,
        )
    )
    if old_brief_snapshot_notice or source_scope_mismatch_notice:
        stale_notice = dict(old_brief_snapshot_notice or source_scope_mismatch_notice)
        stale_scope_message = str(stale_notice.get("detail") or "").strip()
        run_summary = {
            **run_summary,
            ("brief_stale_notice" if old_brief_snapshot_notice else "brief_scope_mismatch"): stale_notice,
            "previous_filtered_total": stale_notice.get("previous_filtered_total") or 0,
            "previous_ranked_total": stale_notice.get("previous_ranked_total") or run_summary.get("previous_ranked_total") or 0,
            "filtered_total": 0,
            "held_back_total": 0,
            "score_demoted_total": 0,
            "filtered_low_fit_total": 0,
        }
        run_payload = {
            **run_payload,
            "summary": run_summary,
            "message": stale_scope_message or str(run_payload.get("message") or "").strip(),
        }
        run_health = {
            **run_health,
            "filtered_total": 0,
            "held_back_total": 0,
            "score_demoted_total": 0,
            "filtered_low_fit_total": 0,
            "message": stale_scope_message or str(run_health.get("message") or "").strip(),
            "status_note": stale_scope_message or str(run_health.get("status_note") or "").strip(),
            "status_label": "Older search" if old_brief_snapshot_notice else str(run_health.get("status_label") or "").strip(),
        }
        run_summary_for_surface = (
            _property_management_run_summary_payload(run_summary)
            if management_surface
            else (_compact_property_surface_run_summary(run_summary) if normalized_section == "properties" else run_summary)
        )
        run_payload_for_surface = {**run_payload_for_surface, "summary": run_summary_for_surface}
        if stale_scope_message:
            run_payload_for_surface["message"] = stale_scope_message
    def _active_run_ranked_candidate_is_presentable(candidate: dict[str, object]) -> bool:
        if _property_candidate_is_rankable(candidate):
            return True
        if str(candidate.get("hard_filter_reason") or "").strip():
            return False
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
        for flag in ("maybe_false", "maybe_false_positive", "false_positive", "filtered_out", "hard_filtered", "not_a_listing"):
            value = candidate.get(flag)
            if isinstance(value, bool) and value:
                return False
            if isinstance(value, (int, float)) and value != 0:
                return False
            if str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}:
                return False
        has_stable_identity = any(
            str(candidate.get(key) or "").strip()
            for key in ("candidate_ref", "source_ref", "packet_url", "review_url", "property_url")
        )
        if not has_stable_identity:
            return False
        if bool(candidate.get("budget_revalidated") or candidate.get("revalidated_from_old_brief")):
            return True
        if normalized_section == "properties" and str(candidate.get("title") or "").strip():
            return True
        return any(
            str(candidate.get(key) or "").strip()
            for key in ("fit_score", "recommendation", "fit_summary")
        ) or bool(list(candidate.get("match_reasons") or []))

    ranked_candidates = [
        {**dict(candidate), "_active_run_ranked": True}
        for candidate in list(raw_run_summary.get("ranked_candidates") or [])
        if isinstance(candidate, dict)
        and _active_run_ranked_candidate_is_presentable(candidate)
    ]
    synthesized_ranked_candidates: list[dict[str, object]] = []
    if not ranked_candidates:
        for source in raw_run_sources:
            source_label = str(source.get("source_label") or source.get("label") or "").strip()
            for candidate in [dict(row) for row in list(source.get("top_candidates") or []) if isinstance(row, dict)]:
                if not _property_candidate_is_rankable(candidate):
                    continue
                candidate["_active_run_ranked"] = True
                candidate.setdefault("source_label", source_label)
                synthesized_ranked_candidates.append(candidate)
        synthesized_ranked_candidates.sort(key=lambda item: float(item.get("fit_score") or 0.0), reverse=True)
    explicit_run_surface = bool(str(run_payload.get("run_id") or "").strip())
    active_run_candidates = ranked_candidates or synthesized_ranked_candidates
    explicitly_selected_source_candidate: dict[str, object] = {}
    if selected_candidate_ref and not any(
        _workspace_candidate_ref(candidate) == selected_candidate_ref
        for candidate in active_run_candidates
    ):
        for source in raw_run_sources:
            source_label = str(
                source.get("source_label") or source.get("label") or ""
            ).strip()
            for candidate_key in (
                "top_candidates",
                "research_candidates",
                "evaluating_candidates",
            ):
                for raw_candidate in list(source.get(candidate_key) or []):
                    if not isinstance(raw_candidate, dict):
                        continue
                    candidate = dict(raw_candidate)
                    if _workspace_candidate_ref(candidate) != selected_candidate_ref:
                        continue
                    candidate.setdefault("source_label", source_label)
                    candidate["_explicitly_selected_source_candidate"] = True
                    explicitly_selected_source_candidate = candidate
                    break
                if explicitly_selected_source_candidate:
                    break
            if explicitly_selected_source_candidate:
                break
    if normalized_section == "search" and explicit_run_surface and not active_run_candidates:
        active_run_candidates = [
            {**dict(candidate), "_active_run_ranked": True}
            for candidate in list(raw_run_summary.get("ranked_candidates") or [])
            if isinstance(candidate, dict)
        ]
    if normalized_section in {"properties", "search", "shortlist", "research"} and active_run_candidates:
        if explicit_run_surface:
            shortlist_candidates = list(active_run_candidates)
        else:
            shortlist_candidates = _dedupe_shortlist_candidates([*active_run_candidates, *shortlist_candidates])
    elif not shortlist_candidates:
        shortlist_candidates = list(active_run_candidates)
    if explicitly_selected_source_candidate:
        shortlist_candidates = _dedupe_shortlist_candidates(
            [*shortlist_candidates, explicitly_selected_source_candidate]
        )
    if not compact_summary_surface and active_run_candidates and not list(run_summary_for_surface.get("ranked_candidates") or []):
        run_summary_for_surface = {
            **dict(run_summary_for_surface),
            "ranked_candidates": [dict(candidate) for candidate in active_run_candidates],
        }
        run_payload_for_surface = {**run_payload_for_surface, "summary": run_summary_for_surface}
    if (
        compact_summary_surface
        and normalized_section == "properties"
        and active_run_candidates
        and any(
            bool(candidate.get("budget_revalidated") or candidate.get("revalidated_from_old_brief"))
            for candidate in active_run_candidates
        )
    ):
        run_summary_for_surface = {
            **dict(run_summary_for_surface),
            "ranked_candidates": [dict(candidate) for candidate in active_run_candidates],
        }
        run_payload_for_surface = {**run_payload_for_surface, "summary": run_summary_for_surface}

    def _normalize_verified_candidate_tour(candidate: dict[str, object]) -> dict[str, object]:
        normalized = dict(candidate)
        tour_payload = dict(normalized.get("tour") or {}) if isinstance(normalized.get("tour"), dict) else {}
        raw_generated_reconstruction_url = str(
            normalized.get("generated_reconstruction_url")
            or normalized.get("layout_preview_url")
            or tour_payload.get("generated_reconstruction_url")
            or tour_payload.get("layout_preview_url")
            or ""
        ).strip()
        raw_tour_url = str(
            normalized.get("tour_runtime_url")
            or normalized.get("tour_url")
            or normalized.get("verified_tour_url")
            or tour_payload.get("tour_url")
            or tour_payload.get("url")
            or tour_payload.get("embed_url")
            or ""
        ).strip()
        raw_open_tour_url = str(
            normalized.get("open_tour_url")
            or normalized.get("verified_tour_url")
            or tour_payload.get("open_tour_url")
            or tour_payload.get("url")
            or tour_payload.get("embed_url")
            or ""
        ).strip()
        if not raw_generated_reconstruction_url and raw_tour_url:
            raw_tour_layout_preview_url = _property_visual_layout_preview_url(raw_tour_url)
            if raw_tour_layout_preview_url:
                raw_generated_reconstruction_url = raw_tour_url
                raw_tour_url = ""
        generated_layout_preview_url = _property_visual_layout_preview_url(
            raw_generated_reconstruction_url
        )
        if generated_layout_preview_url and raw_open_tour_url in {
            raw_generated_reconstruction_url,
            generated_layout_preview_url,
        }:
            raw_open_tour_url = ""
        if not raw_tour_url and not raw_open_tour_url and not generated_layout_preview_url:
            if tour_payload:
                normalized["tour"] = tour_payload
            return normalized
        normalized["tour_runtime_url"] = (
            raw_tour_url
            or raw_open_tour_url
            or raw_generated_reconstruction_url
        )
        ready_tour_url = _property_workbench_candidate_ready_tour_url(
            {
                **normalized,
                "tour_url": raw_tour_url,
                "open_tour_url": raw_open_tour_url,
                "generated_reconstruction_url": raw_generated_reconstruction_url,
                "layout_preview_url": generated_layout_preview_url,
                "tour": tour_payload,
            },
            principal_id=workspace_principal_id,
        )
        if ready_tour_url:
            normalized["tour_url"] = ready_tour_url
            normalized["verified_tour_url"] = ready_tour_url
            normalized["open_tour_url"] = ready_tour_url
            tour_payload["url"] = ready_tour_url
            if str(tour_payload.get("embed_url") or "").strip():
                tour_payload["embed_url"] = ready_tour_url
            elif "embed_url" not in tour_payload:
                tour_payload["embed_url"] = ready_tour_url
            normalized["tour"] = tour_payload
        elif generated_layout_preview_url:
            normalized["tour_url"] = ""
            normalized["verified_tour_url"] = ""
            normalized["open_tour_url"] = generated_layout_preview_url
            normalized["generated_reconstruction_url"] = raw_generated_reconstruction_url
            normalized["layout_preview_url"] = generated_layout_preview_url
            normalized["layout_preview_status"] = "ready"
            normalized["tour_media_mode"] = "generated_reconstruction"
            if str(normalized.get("tour_status") or "").strip().lower() in _PROPERTY_WORKBENCH_READY_VISUAL_STATUSES:
                normalized["tour_status"] = "blocked"
            tour_payload.update(
                {
                    "status": "ready",
                    "label": _PROPERTY_GENERATED_TOUR_STATUS_LABEL,
                    "control_label": _PROPERTY_GENERATED_TOUR_STATUS_LABEL,
                    "status_detail": _PROPERTY_GENERATED_TOUR_STATUS_DETAIL,
                    "url": generated_layout_preview_url,
                    "embed_url": generated_layout_preview_url,
                    "generated_reconstruction_url": raw_generated_reconstruction_url,
                    "layout_preview_url": generated_layout_preview_url,
                    "layout_preview_status": "ready",
                    "tour_media_mode": "generated_reconstruction",
                    "provider_key": "generated_reconstruction",
                    "provider_label": "AI-generated 3D tour",
                }
            )
            normalized["tour"] = tour_payload
        elif raw_tour_url:
            from app.product import property_tour_hosting

            if property_tour_hosting._is_branded_public_tour_url(raw_tour_url):  # type: ignore[attr-defined]
                normalized["tour_url"] = ""
                normalized["verified_tour_url"] = ""
                normalized["open_tour_url"] = ""
                if tour_payload:
                    tour_payload["url"] = ""
                    if "embed_url" in tour_payload:
                        tour_payload["embed_url"] = ""
                    normalized["tour"] = tour_payload
            elif tour_payload:
                normalized["tour"] = tour_payload
        elif tour_payload:
            normalized["open_tour_url"] = raw_open_tour_url
            normalized["tour_url"] = ""
            normalized["tour"] = tour_payload
        return normalized

    shortlist_candidates = [_normalize_verified_candidate_tour(candidate) for candidate in shortlist_candidates]
    selected_locations = _csv_values(property_preferences.get("location_query"))
    selected_keywords = _csv_values(property_preferences.get("keywords"))
    selected_search_goal = normalized_property_search_goal(property_preferences.get("search_goal"))
    property_is_investment_search = selected_search_goal == "investment"
    effective_listing_mode = effective_property_listing_mode(
        {
            **property_preferences,
            "search_goal": selected_search_goal,
        },
        fallback=str(property_preferences.get("listing_mode") or "rent"),
    )
    mode_visibility_label = property_mode_visibility_label(
        {
            **property_preferences,
            "search_goal": selected_search_goal,
            "listing_mode": effective_listing_mode,
        },
        fallback=effective_listing_mode,
    )
    property_search_goal_label = "Find an investment" if property_is_investment_search else "Find a home"
    property_investment_strategy_label = (
        {
            "cash_flow": "Cash flow",
            "appreciation": "Appreciation",
            "undervalued": "Undervalued",
            "low_risk": "Low risk",
        }.get(str(property_preferences.get("investment_strategy") or "").strip().lower(), "Best overall opportunity")
        if property_is_investment_search
        else ""
    )

    def _adjacent_radius_m_for_preview() -> int:
        raw_radius_m = property_preferences.get("adjacent_area_radius_m")
        try:
            radius_m = int(float(str(raw_radius_m or "").strip()))
        except Exception:
            radius_m = 0
        raw_radius_value = property_preferences.get("adjacent_area_radius_value")
        if raw_radius_value not in (None, ""):
            try:
                radius_value = float(str(raw_radius_value or "").strip())
            except Exception:
                radius_value = 0.0
            radius_unit = str(property_preferences.get("adjacent_area_radius_unit") or "m").strip().lower()
            if radius_value > 0:
                radius_m = int(round(radius_value * 1000.0)) if radius_unit == "km" else int(round(radius_value))
        return max(0, min(radius_m, 20_000))

    current_scope_preview: dict[str, object] = {}
    if normalized_section == "search":
        preview_location_query = ", ".join(selected_locations) or str(property_preferences.get("location_query") or "").strip()
        if not preview_location_query and bool(property_preferences.get("full_region_scope")):
            preview_location_query = str(property_preferences.get("region_code") or property_state.get("region_label") or "").strip()
        try:
            current_scope_preview = dict(
                _property_scope_preview_map_only(
                    str(property_preferences.get("country_code") or "AT").strip().upper() or "AT",
                    str(property_preferences.get("region_code") or "").strip().lower(),
                    preview_location_query,
                    adjacent_area_radius_m=_adjacent_radius_m_for_preview(),
                )
                or {}
            )
        except Exception:
            current_scope_preview = {}
    available_platform_values = {
        str(option.get("value") or "").strip().lower()
        for option in provider_options
        if str(option.get("value") or "").strip()
    }
    has_platform_catalog = len(available_platform_values) > 0
    normalized_platforms: list[str] = []
    for value in list(property_preferences.get("selected_platforms") or property_state.get("selected_platforms") or []):
        normalized = str(value or "").strip()
        normalized_lower = normalized.lower()
        if not normalized:
            continue
        if has_platform_catalog and normalized_lower not in available_platform_values:
            continue
        if normalized_lower in normalized_platforms:
            continue
        normalized_platforms.append(normalized_lower)
    selected_platforms = normalized_platforms
    sources_total_rows = [dict(row) for row in list(run_summary.get("sources") or []) if isinstance(row, dict)]
    run_source_variant_total = max(
        int(run_summary.get("source_variant_total") or run_summary.get("sources_total") or 0),
        len(sources_total_rows),
    )
    run_provider_total = int(run_summary.get("provider_total") or 0)
    run_provider_display_total = run_provider_total
    if sources_total_rows:
        inferred_run_provider_total = _property_provider_total(sources_total_rows)
        source_total_hint = max(len(sources_total_rows), run_source_variant_total)
        if inferred_run_provider_total:
            if (
                run_provider_total <= 0
                or run_provider_total > source_total_hint
                or (run_provider_total == source_total_hint and inferred_run_provider_total < run_provider_total)
            ):
                run_provider_display_total = inferred_run_provider_total
    if run_provider_display_total <= 0 and selected_platforms:
        run_provider_display_total = len(selected_platforms)
    if selected_platforms:
        run_provider_display_total = max(run_provider_display_total, len(selected_platforms))
    run_provider_display_total = max(run_provider_display_total, 0)
    run_payload_for_surface = {
        **run_payload_for_surface,
        "provider_display_total": run_provider_display_total,
        "source_variant_display_total": run_source_variant_total,
        "selected_platform_count": len(selected_platforms),
    }
    selected_country_code = str(property_preferences.get("country_code") or property_state.get("country_code") or "AT").strip().upper() or "AT"
    workspace_currency_code = currency_code_for_country(selected_country_code) or "EUR"
    workspace_timezone = str(workspace.get("timezone") or default_timezone_for_country(selected_country_code) or "UTC").strip() or "UTC"
    supported_currency_pattern = "|".join(re.escape(code) for code in supported_currency_codes())
    supported_currency_strip_pattern = re.compile(rf"\b(?:{supported_currency_pattern})\b", flags=re.IGNORECASE)
    run_has_explicit_listing_context = bool(
        run_property_preferences
        or str(raw_run_summary.get("listing_mode") or "").strip()
        or str(raw_run_summary.get("search_goal") or "").strip()
    )
    run_has_explicit_scope_context = bool(
        run_property_preferences
        or str(raw_run_summary.get("country_code") or "").strip()
        or str(raw_run_summary.get("region_code") or "").strip()
        or str(raw_run_summary.get("location_query") or "").strip()
    )
    review_scope_locations = selected_locations if run_has_explicit_scope_context else []
    if old_brief_snapshot_notice or source_scope_mismatch_notice:
        stale_notice = dict(old_brief_snapshot_notice or source_scope_mismatch_notice)
        stale_scope_action = {**stale_notice, "adjustments": {}}
        suppression_rows = [stale_notice]
        counterfactual_rows = [stale_scope_action]
    else:
        suppression_rows = _property_suppression_rows(
            run_summary=run_summary,
            source_rows=run_sources,
            preferences=property_preferences,
            include_soft=False,
        )
        counterfactual_rows = _property_counterfactual_rows(
            preferences=property_preferences,
            raw_preferences=dict(property_state.get("raw_preferences") or {}),
            run_summary=run_summary,
            provider_options=provider_options,
            current_platform_cap=current_platform_cap,
            currency_code=workspace_currency_code,
        )
    delivery_proof_rows = _delivery_proof_rows(run_summary)
    artifact_receipt_rows = _artifact_receipt_rows(run_summary)
    run_id = str(run_payload.get("run_id") or "").strip()
    run_suffix = f"?run_id={run_id}" if run_id else ""
    signed_in_billing_href = str(billing_handoff.get("open_href") or "").strip() or f"/app/billing{run_suffix}"
    search_posture_items = list(search_posture_card.get("items") or [])
    fleet_digest = dict(billing_truth.get("fleet_digest") or property_state.get("fleet_digest") or {}) if wants_credit_digest else {}
    fleet_digest_items = [
        row_item(
            str(item.get("title") or "Status update"),
            str(item.get("detail") or item.get("value") or "").strip() or str(fleet_digest.get("preview_text") or "Update pending"),
            str(item.get("tag") or "Status"),
        )
        for item in list(fleet_digest.get("items") or [])[:4]
        if isinstance(item, dict)
    ] if wants_credit_digest else []
    fleet_digest_summary = str(fleet_digest.get("summary") or fleet_digest.get("preview_text") or "").strip() if wants_credit_digest else ""
    def _local_int(value: object) -> int:
        try:
            if value not in (None, ""):
                return int(float(value))
        except Exception:
            pass
        return 0

    def _run_homes_checked_total(summary: dict[str, object] | None) -> int:
        summary_row = dict(summary or {})
        return _local_int(
            summary_row.get("reviewed_listing_total")
            or summary_row.get("raw_listing_total")
            or summary_row.get("listing_total")
        )

    def _run_outcome_compact_detail(run_row: dict[str, object] | None) -> str:
        row = dict(run_row or {})
        return (
            f"Ranked {_local_int(row.get('ranked_total'))}"
            f" | Sent {_local_int(row.get('sent_total'))}"
            f" | Outside this search {_local_int(row.get('held_back_total'))}"
        )

    fleet_digest_stats = dict(fleet_digest.get("stats") or {}) if isinstance(fleet_digest.get("stats"), dict) else {}
    active_fleet_lanes = _local_int(fleet_digest_stats.get("active_lanes"))
    queued_fleet_lanes = _local_int(fleet_digest_stats.get("queued_lanes"))
    failed_fleet_lanes = _local_int(fleet_digest_stats.get("failed_lanes"))
    stalled_fleet_lanes = _local_int(fleet_digest_stats.get("stalled_lanes"))
    repair_truth_rows = [
        row_item(
            "Source follow-up",
            " · ".join(
                part
                for part in (
                    f"{active_fleet_lanes} checking now" if active_fleet_lanes else "",
                    f"{queued_fleet_lanes} queued" if queued_fleet_lanes else "",
                    f"{failed_fleet_lanes} still blocked" if failed_fleet_lanes else "",
                    f"{stalled_fleet_lanes} waiting" if stalled_fleet_lanes else "",
                )
                if part
            )
            or "No list follow-up is active right now.",
            "Watching",
        ),
        row_item(
            "Latest note",
            fleet_digest_summary or "The next list update will appear here.",
            "Updates",
        ),
    ]
    packet_ready_total = 0
    tour_ready_total = 0

    run_status_note = str(run_health.get("status_note") or "").strip()
    run_message = str(run_status_note or safe_run_message or run_health.get("message") or run_payload.get("message") or "").strip()
    run_status_label = str(run_health.get("status_label") or "").strip() or "Queued"
    if isinstance(run_card, dict):
        run_card_copy = dict(run_card)
        raw_run_card_body = str(run_card_copy.get("body") or run_card_copy.get("detail") or "").strip()
        if raw_run_card_body:
            safe_run_card_body = property_run_customer_safe_status_detail(
                run_status_value,
                raw_run_card_body,
                summary=run_summary,
                prefer_repair_step=True,
            )
            if safe_run_card_body and safe_run_card_body != raw_run_card_body:
                run_card_copy["body"] = safe_run_card_body
                run_card = run_card_copy
    run_in_progress = bool(run_id and bool(run_health.get("in_progress")))
    progress_current_property = _property_progress_current_property_card(
        run_summary=run_summary,
        run_id=run_id,
    ) if wants_run_views else {}
    progress_route_previews = _property_progress_route_preview_rows(
        run_summary=run_summary,
        property_preferences=property_preferences,
    ) if wants_run_views else []
    search_worker_state = _property_search_worker_slots(run_summary, plan_key=effective_run_plan_key) if wants_run_views else []

    def _run_count(value: object, default: int = 0) -> int:
        try:
            return max(0, int(float(str(value or "").strip())))
        except Exception:
            return default

    open_research_task_total = _run_count(run_health.get("open_research_task_total") or run_payload.get("open_research_task_total") or raw_run_summary.get("open_research_task_total"))
    filled_research_task_total = _run_count(run_health.get("filled_research_task_total") or run_payload.get("filled_research_task_total") or raw_run_summary.get("filled_research_task_total"))
    dismissed_research_task_total = _run_count(run_health.get("dismissed_research_task_total") or run_payload.get("dismissed_research_task_total") or raw_run_summary.get("dismissed_research_task_total"))
    research_task_total = _run_count(run_health.get("research_task_total") or run_payload.get("research_task_total") or raw_run_summary.get("research_task_total"))

    scope_preview_builder = _property_scope_preview_map_only if normalized_section == "agents" else _property_scope_preview
    previous_search_runs = [
        build_property_previous_run_summary(
            dict(row),
            include_scope_preview=normalized_section != "agents" and index < 6,
            scope_preview_builder=scope_preview_builder,
            compact_provider_label=_compact_provider_label,
            candidate_maps_url_builder=_property_candidate_maps_url,
        )
        for index, row in enumerate(list(property_state.get("recent_search_runs") or []))
        if isinstance(row, dict) and str(row.get("run_id") or "").strip()
    ] if (wants_recent_runs or wants_agent_views) else []
    for previous_run in previous_search_runs:
        _compact_scope_preview_payload(previous_run)
        if normalized_section in {"agents", "account", "settings", "billing"}:
            previous_run["top_candidates"] = [
                safe_candidate
                for candidate in list(previous_run.get("top_candidates") or [])
                if isinstance(candidate, dict)
                and (safe_candidate := _property_management_candidate_payload(candidate))
            ]
    if wants_search_runs or wants_agent_views:
        selected_agent_context = select_property_search_agent(
            property_search_agents,
            requested_agent_id=requested_property_agent_id,
            previous_runs=previous_search_runs,
            run_id=run_id,
        )
    else:
        selected_agent_context = {
            "selected_agent": {},
            "selected_agent_id": "",
            "selected_agent_runs": [],
            "selected_agent_latest_run": {},
            "selected_agent_open_href": "",
            "selected_agent_edit_href": "",
        }
    selected_agent = selected_agent_context["selected_agent"]
    selected_agent_id = selected_agent_context["selected_agent_id"]
    selected_agent_runs = selected_agent_context["selected_agent_runs"]
    selected_agent_latest_run = selected_agent_context["selected_agent_latest_run"]
    selected_agent_open_href = selected_agent_context["selected_agent_open_href"]
    selected_agent_edit_href = selected_agent_context["selected_agent_edit_href"]
    include_search_agent_payload = normalized_section != "search" or bool(requested_property_agent_id and selected_agent_id)

    preference_manager = build_property_preference_manager_snapshot(
        person_id=preference_person_id,
        raw_preference_nodes=raw_preference_nodes,
        include_full_manager=wants_full_preference_manager,
        schema=_property_preference_schema() if wants_full_preference_manager else {},
    )
    if normalized_section == "search" and not run_id:
        decision_workbench = PropertyDecisionWorkbenchContract(
            run=PropertyDecisionWorkbenchRunContract(
                run_id="",
                status="not_started",
                status_label=str(dict(property_state.get("search_health") or {}).get("label") or "Unavailable"),
                progress=0,
                message="",
                status_url="",
                summary={},
                filtered_total=0,
                held_back_total=0,
                events=[],
                worker_state=[],
                reliability={},
                research_task_total=0,
                open_research_task_total=0,
                filled_research_task_total=0,
                dismissed_research_task_total=0,
                provider_display_total=run_provider_display_total,
                source_variant_display_total=run_source_variant_total,
                selected_platform_count=len(selected_platforms),
                route_previews=[],
                current_property={},
            ),
            brief=PropertyDecisionWorkbenchBriefContract(
                country=str(property_state.get("country_label") or "Market"),
                search_goal=selected_search_goal,
                search_goal_label=property_search_goal_label,
                mode=mode_visibility_label,
                investment_strategy_label=property_investment_strategy_label if property_is_investment_search else "",
                region=str(property_state.get("region_label") or property_preferences.get("region_code") or "").strip(),
                areas=selected_locations,
                priorities=selected_keywords,
                providers=selected_platforms,
                plan=effective_run_plan_label,
                plan_key=effective_run_plan_key,
                research_depth=effective_run_research_depth,
            ),
            brief_preferences=brief_preferences_payload,
            endpoints={
                "preferences": str(property_meta.get("preferences_endpoint") or "").strip(),
                "start": str(property_meta.get("start_endpoint") or "").strip(),
                "billing_order": str(property_meta.get("billing_order_endpoint") or "").strip(),
                "delete_run_template": "/app/api/property/search-runs/__RUN_ID__",
            },
            counterfactual_rows=counterfactual_rows,
            recent_packets=[],
            previous_search_runs=previous_search_runs,
            current_scope_preview=current_scope_preview,
            search_agents=(
                property_search_agents
                if include_search_agent_payload
                else []
            ),
            search_agent=(
                property_search_agent
                if include_search_agent_payload
                else {}
            ),
            results=[],
            search_guard_rows=[],
            suppression_rows=[],
            delivery_proof_rows=[],
            artifact_receipt_rows=[],
            research_tasks=[],
            research_task_counts={"total": 0, "open": 0, "filled": 0, "dismissed": 0},
            selected_candidate_ref="",
            selected={},
            empty_outcome={},
            packet_recovery=packet_recovery,
            route_recovery=route_recovery,
            show_brief_default=True,
        )
        return PropertySurfacePayloadContract(
            title="Search",
            summary="Set the market, filters, site mix, and what matters before launching the next search.",
            stats=list(base.get("stats") or []),
            current_plan_label=current_plan_label,
            run_payload={},
            run_summary={},
            preference_manager=preference_manager,
            decision_workbench=decision_workbench,
            extras={
                "hero_kicker": "Search",
                "hero_title": "Shape the next property search.",
                "hero_summary": "Brief, sites, priorities.",
                "hero_actions": [],
                "hero_highlights": [
                    {
                        "label": "Areas",
                        "value": str(len(selected_locations) or 0),
                        "detail": ", ".join(selected_locations[:3]) or "Choose the target areas.",
                        "href": f"/app/search{run_suffix}",
                    },
                    {
                        "label": "Priorities",
                        "value": str(len(selected_keywords) or 0),
                        "detail": ", ".join(selected_keywords[:3]) or "Record what should matter most.",
                        "href": f"/app/search{run_suffix}",
                    },
                    {
                        "label": "Providers",
                        "value": str(len(selected_platforms) or 0),
                        "detail": "Sites chosen for the next search.",
                        "href": f"/app/search{run_suffix}",
                    },
                ],
                "primary_cards": [card for card in (search_posture_card, market_coverage_card) if card],
                "secondary_cards": [],
                "console_form": property_form,
                "show_brief_form": True,
                "show_run_panel": False,
                "show_shortlist_cards": False,
                "show_results_table": False,
                "results_table_headers": [],
                "results_table_rows": [],
            },
        ).to_dict()

    def _tour_source_gap_detail(candidate: dict[str, object]) -> str:
        raw_tour = dict(candidate.get("tour") or {}) if isinstance(candidate.get("tour"), dict) else {}
        blocked_reason = _property_visual_reason_key(
            candidate.get("blocked_reason"),
            candidate.get("tour_reason"),
            raw_tour.get("reason_key"),
            raw_tour.get("blocked_reason"),
            raw_tour.get("reason"),
        )
        if blocked_reason:
            reason_map = {
                "listing_360_media_missing": "3D tour not ready yet. This listing still needs a floorplan or usable 360 source.",
                "listing_expired": "The source listing has expired, and no captured source is available for a 3D tour.",
                "pure_360_assets_unavailable": "3D tour not ready yet. The source media could not be opened reliably enough to rebuild it.",
                "property_tour_fallback_disabled": "3D tour not ready yet. A floorplan or usable 360 source is still missing.",
                "property_tour_execution_failed": "The 3D tour could not be built from the available source media. You can try again.",
                "property_tour_delivery_failed": "The 3D tour could not be published. You can try again.",
            }
            if blocked_reason in reason_map:
                return reason_map[blocked_reason]
        facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}

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

        if _false_flag(facts.get("has_floorplan")) or _zero_count("floorplan_count", "floorplans_count"):
            return "3D tour not ready yet. This listing still needs a floorplan or usable 360 source."
        if _false_flag(facts.get("has_360")) or _zero_count("media_count", "image_count"):
            return "3D tour not ready yet. More usable room media is still needed."
        return "Tour not available yet."

    def _candidate_fact_line(candidate: dict[str, object]) -> str:
        facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
        parts: list[str] = []
        price_value = str(
            facts.get("price_display")
            or facts.get("rent_display")
            or facts.get("price")
            or facts.get("price_eur")
            or ""
        ).strip()
        rooms_value = str(facts.get("rooms") or facts.get("room_count") or "").strip()
        area_value = str(
            facts.get("area_display")
            or facts.get("living_area_display")
            or facts.get("usable_area_display")
            or facts.get("area_m2")
            or facts.get("living_area_m2")
            or facts.get("area_sqm")
            or ""
        ).strip()
        if price_value:
            parts.append(price_value)
        if rooms_value:
            parts.append(f"{rooms_value} rooms")
        if area_value:
            parts.append(area_value if "m2" in area_value.lower() or "m²" in area_value.lower() else f"{area_value} m2")
        return " | ".join(parts)

    def _area_display(facts: dict[str, object]) -> str:
        for key in (
            "area_display",
            "living_area_display",
            "usable_area_display",
            "wohnflaeche_display",
            "wohnfläche_display",
        ):
            value = str(facts.get(key) or "").strip()
            if value:
                return value
        for key in (
            "area_m2",
            "area_sqm",
            "living_area_m2",
            "living_area_sqm",
            "usable_area_m2",
            "wohnflaeche_m2",
            "wohnfläche_m2",
        ):
            value = str(facts.get(key) or "").strip()
            if value:
                return f"{value} m2"
        return ""

    def _floorplan_url(facts: dict[str, object], *, candidate: dict[str, object] | None = None) -> str:
        return _property_candidate_floorplan_url(candidate or {"property_facts": facts}, facts=facts)

    def _obvious_listing_mode_mismatch(facts: dict[str, object], *, listing_mode: str) -> bool:
        normalized_mode = str(listing_mode or "").strip().lower()
        if normalized_mode == "buy":
            has_buy_price = isinstance(_property_investment_price_eur(facts), float)
            has_rent_signal = any(
                facts.get(key)
                for key in (
                    "rent_display",
                    "warm_rent_display",
                    "cold_rent_display",
                    "total_rent_display",
                    "warm_rent_eur",
                    "cold_rent_eur",
                    "total_rent_eur",
                    "rent_eur",
                    "gesamtmiete_display",
                )
            )
            return bool(has_rent_signal and not has_buy_price)
        if normalized_mode == "rent":
            has_buy_signal = isinstance(_property_investment_price_eur(facts), float)
            has_rent_price = any(
                facts.get(key)
                for key in ("rent_display", "warm_rent_display", "cold_rent_display", "total_rent_display", "rent_eur", "total_rent_eur")
            )
            return bool(has_buy_signal and not has_rent_price)
        return False

    def _tour_status_line(candidate: dict[str, object]) -> str:
        facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
        provider_tour_url = _property_candidate_source_virtual_tour_url(candidate, facts=facts)
        try:
            from app.product import property_tour_hosting

            verified_tour_url = property_tour_hosting._hosted_property_tour_first_party_open_url(candidate.get("tour_url"))  # type: ignore[attr-defined]
        except Exception:
            verified_tour_url = ""
        if verified_tour_url:
            return "Ready | On this page"
        if provider_tour_url:
            return "Ready | Original 360"
        status = str(candidate.get("tour_status") or "").strip().lower()
        eta_minutes = int(candidate.get("tour_eta_minutes") or 0) if str(candidate.get("tour_eta_minutes") or "").strip() else 0
        if status in {"queued", "pending"}:
            return f"Queued | ETA about {eta_minutes or 10} min"
        if status in {"processing", "running", "in_progress", "started"}:
            return f"Rendering | ETA about {eta_minutes or 5} min"
        if status in {"created", "existing"}:
            return "Ready"
        if status in {"blocked", "failed", "skipped", "not_applicable"}:
            return f"Blocked | {_tour_source_gap_detail(candidate)}"
        blocked_reason = str(candidate.get("blocked_reason") or "").strip()
        if blocked_reason:
            return f"Blocked | {blocked_reason.replace('_', ' ')}"
        return f"Unavailable | {_tour_source_gap_detail(candidate)}"

    _pending_visual_states = {"queued", "pending", "processing", "running", "in_progress", "started", "rendering", "repairing"}

    def _visual_runtime_payload(
        candidate: dict[str, object],
        *,
        request_kind: str,
        status: object,
        ready_url: object = "",
        eta_minutes: object = "",
        reason: object = "",
    ) -> dict[str, object]:
        normalized_kind = "flythrough" if str(request_kind or "").strip().lower() == "flythrough" else "tour"
        normalized_status = str(status or "").strip().lower()
        ready_href = str(ready_url or "").strip()
        normalized_reason = str(reason or "").strip()
        progress_tour_url = str(candidate.get("tour_runtime_url") or candidate.get("tour_url") or "").strip()
        live_progress = (
            _hosted_property_visual_progress_snapshot(progress_tour_url, request_kind=normalized_kind)
            if progress_tour_url
            else {}
        )
        live_progress_status = str(live_progress.get("status") or "").strip().lower()
        live_progress_reason = str(live_progress.get("reason") or "").strip()
        live_progress_detail = str(live_progress.get("detail") or "").strip()
        if normalized_reason:
            terminal_status = _property_visual_terminal_status_for_reason(
                request_kind=normalized_kind,
                reason=normalized_reason,
            )
            if terminal_status and not ready_href and normalized_status in _pending_visual_states:
                normalized_status = terminal_status
                eta_minutes = ""
        elif live_progress_reason:
            normalized_reason = live_progress_reason
            terminal_status = _property_visual_terminal_status_for_reason(
                request_kind=normalized_kind,
                reason=normalized_reason,
            )
            if terminal_status and not ready_href and normalized_status in _pending_visual_states:
                normalized_status = terminal_status
                eta_minutes = ""
        requested_at_key = "flythrough_requested_at" if normalized_kind == "flythrough" else "tour_requested_at"
        updated_at_key = "flythrough_status_updated_at" if normalized_kind == "flythrough" else "tour_status_updated_at"
        progress_key = "flythrough_progress_pct" if normalized_kind == "flythrough" else "tour_progress_pct"
        requested_at = str(candidate.get(requested_at_key) or "").strip()
        status_updated_at = str(candidate.get(updated_at_key) or "").strip()
        if live_progress_status and not ready_href and normalized_status in _pending_visual_states:
            normalized_status = live_progress_status
        if str(live_progress.get("updated_at") or "").strip():
            status_updated_at = str(live_progress.get("updated_at") or "").strip()
        try:
            progress_pct = (
                int(float(str(candidate.get(progress_key) or "").strip()))
                if str(candidate.get(progress_key) or "").strip()
                else 0
            )
        except Exception:
            progress_pct = 0
        try:
            live_progress_pct = (
                int(float(str(live_progress.get("progress_pct") or "").strip()))
                if str(live_progress.get("progress_pct") or "").strip()
                else 0
            )
        except Exception:
            live_progress_pct = 0
        eta_label = _property_visual_eta_label(
            request_kind=normalized_kind,
            status=normalized_status,
            eta_minutes=eta_minutes,
            requested_at=requested_at,
            status_updated_at=status_updated_at,
        )
        if live_progress_status in _pending_visual_states and not ready_href:
            eta_label = _hosted_property_visual_progress_stage_label(live_progress) or ""
        if progress_pct <= 0:
            progress_pct = _property_visual_progress_pct(
                request_kind=normalized_kind,
                status=normalized_status,
                ready_url=ready_href,
                eta_minutes=eta_minutes,
                requested_at=requested_at,
                status_updated_at=status_updated_at,
            )
        if live_progress_pct > 0 and not ready_href and normalized_status not in {"blocked", "failed", "skipped", "not_applicable"}:
            progress_pct = live_progress_pct
        if normalized_status == "repairing" and not ready_href:
            progress_pct = max(progress_pct, 72 if normalized_kind == "flythrough" else 68)
            eta_label = "refreshing"
        payload_status = "processing" if normalized_status in {"rendering", "repairing"} else normalized_status
        return {
            "status": payload_status,
            "progress_pct": progress_pct,
            "eta_label": eta_label,
            "requested_at": requested_at,
            "status_updated_at": status_updated_at,
            "status_detail": live_progress_detail,
        }

    def _distance_line(candidate: dict[str, object]) -> str:
        facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
        family_filters_active = _property_family_filters_active(property_preferences)
        rows = _property_distance_evidence_rows(facts, include_family_only=family_filters_active)
        return " · ".join(str(row.get("inline") or "").strip() for row in rows[:3] if str(row.get("inline") or "").strip())

    results_table_rows = []
    workbench_results: list[dict[str, object]] = []
    admitted_shortlist_candidates: list[dict[str, object]] = []

    def _money_per_sqm_line(facts: dict[str, object]) -> str:
        raw_price = facts.get("price_eur") or facts.get("purchase_price_eur")
        raw_area = facts.get("area_m2") or facts.get("living_area_m2")
        try:
            price = float(raw_price)
            area = float(raw_area)
        except Exception:
            return ""
        if price <= 0 or area <= 0:
            return ""
        return f"{workspace_currency_code} {price / area:,.0f}/m2"

    def _missing_fact_items(facts: dict[str, object]) -> list[dict[str, object]]:
        research = facts.get("missing_fact_research")
        if not isinstance(research, dict):
            return []
        items = research.get("items")
        if not isinstance(items, list):
            return []
        return [dict(item) for item in items if isinstance(item, dict)]

    def _missing_fact_item(facts: dict[str, object], field: str) -> dict[str, object]:
        normalized = str(field or "").strip()
        for item in _missing_fact_items(facts):
            if str(item.get("field") or "").strip() == normalized:
                return item
        return {}

    def _rooms_layout_part(facts: dict[str, object]) -> str:
        label = str(facts.get("rooms_label") or "").strip()
        if "under research" in label.lower():
            label = ""
        if label:
            return label
        raw_value = facts.get("rooms") or facts.get("room_count")
        if raw_value:
            return f"{raw_value} rooms"
        item = _missing_fact_item(facts, "rooms")
        if item:
            display_value = str(item.get("display_value") or "").strip()
            if "under research" in display_value.lower():
                return ""
            return display_value
        return ""

    def _risk_summary(candidate: dict[str, object], facts: dict[str, object]) -> dict[str, str]:
        mismatch = _property_normalized_mismatch_reasons(
            [_clean_property_candidate_copy(item) for item in list(candidate.get("mismatch_reasons") or []) if _clean_property_candidate_copy(item)],
            facts=facts,
            preferences=property_preferences,
        )
        missing: list[str] = []
        provider_tour_url = _property_candidate_source_virtual_tour_url(candidate, facts=facts)
        if not str(candidate.get("tour_url") or provider_tour_url or "").strip():
            tour_status = str(candidate.get("tour_status") or "").strip().lower()
            if tour_status in {"blocked", "failed", "skipped", "not_applicable"}:
                missing.append("floorplan/360 source media")
            else:
                missing.append("360 pending")
        if not (facts.get("street_address") or facts.get("address")):
            missing.append("address")
        if not (facts.get("heating") or facts.get("heating_type")):
            missing.append("heating")
        if bool(facts.get("air_quality_risk")):
            missing.append("air quality")
        if bool(facts.get("crime_risk")):
            missing.append("crime risk")
        if bool(facts.get("parking_pressure_risk")):
            missing.append("parking pressure")
        if bool(facts.get("drinking_water_risk")):
            missing.append("water quality")
        if bool(facts.get("cesspit_risk")):
            missing.append("Senkgrube or septic burden")
        if bool(facts.get("winter_access_risk")):
            missing.append("winter access")
        if bool(facts.get("flood_risk")):
            missing.append("flood exposure")
        for item in _missing_fact_items(facts):
            if str(item.get("status") or "").strip().lower() != "filled":
                missing.append(str(item.get("label") or item.get("field") or "research fact").strip())
        if mismatch:
            return {"level": "medium", "summary": mismatch[0]}
        if len(missing) >= 2:
            return {"level": "medium", "summary": "Missing " + ", ".join(missing[:3])}
        if missing:
            return {"level": "low", "summary": "Missing " + missing[0]}
        return {"level": "low", "summary": "No major open issue flagged yet."}

    def _candidate_ooda_rows(candidate: dict[str, object], facts: dict[str, object]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        family_filters_active = _property_family_filters_active(property_preferences)
        rows.extend(
            {
                "label": str(row.get("label") or "").strip(),
                "value": str(row.get("title") or row.get("value") or "").strip(),
                "detail": str(row.get("detail") or "").strip(),
            }
            for row in _property_distance_evidence_rows(facts, include_family_only=family_filters_active)
        )
        match_reasons = [_clean_property_candidate_copy(item) for item in list(candidate.get("match_reasons") or []) if _clean_property_candidate_copy(item)]
        mismatch_reasons = _property_normalized_mismatch_reasons(
            [_clean_property_candidate_copy(item) for item in list(candidate.get("mismatch_reasons") or []) if _clean_property_candidate_copy(item)],
            facts=facts,
            preferences=property_preferences,
        )
        rows.insert(
            0,
            {
                "label": "Decide",
                "value": str(candidate.get("recommendation") or candidate.get("tag") or "Home").strip().replace("_", " ").title(),
                "detail": match_reasons[0] if match_reasons else (mismatch_reasons[0] if mismatch_reasons else "Open the home for more detail."),
            },
        )
        for item in _missing_fact_items(facts):
            if str(item.get("status") or "").strip().lower() == "filled":
                continue
            ooda = dict(item.get("ooda") or {}) if isinstance(item.get("ooda"), dict) else {}
            label = str(item.get("label") or item.get("field") or "Missing fact").strip()
            rows.append(
                {
                    "label": "Research",
                    "value": str(item.get("display_value") or label).strip(),
                    "detail": str(ooda.get("act") or item.get("evidence") or "We are still checking this detail.").strip(),
                }
            )
        for risk_key, label, detail in (
            ("air_quality_risk", "Risk", "Air quality needs a closer look for this micro-location."),
            ("crime_risk", "Risk", "Safety patterns need a closer look for this quarter."),
            ("parking_pressure_risk", "Risk", "Parking pressure still needs clarification if no garage is included."),
            ("heat_resilience_risk", "Check", "Summer heat resilience needs a closer look for this home, including shade, cooling, and local cooling corridors."),
            ("drinking_water_risk", "Risk", "Water source and groundwater burden need a closer look."),
            ("cesspit_risk", "Risk", "Senkgrube or septic burden needs a closer look."),
            ("winter_access_risk", "Risk", "Winter driving access needs a closer look."),
            ("flood_risk", "Risk", "Flood and runoff exposure need a closer look."),
        ):
            if bool(facts.get(risk_key)):
                rows.append({"label": label, "value": risk_key.replace("_", " ").title(), "detail": detail})
        return rows[:6]

    def _candidate_objection_rows(candidate: dict[str, object], facts: dict[str, object]) -> list[dict[str, str]]:
        mismatch_reasons = _property_normalized_mismatch_reasons(
            [_clean_property_candidate_copy(item) for item in list(candidate.get("mismatch_reasons") or []) if _clean_property_candidate_copy(item)],
            facts=facts,
            preferences=property_preferences,
        )
        rows: list[dict[str, str]] = []
        feedback_summary = dict(candidate.get("feedback_summary") or {}) if isinstance(candidate.get("feedback_summary"), dict) else {}
        for reason in mismatch_reasons[:3]:
            rows.append({"title": "Main caution", "detail": reason, "tag": "Watch"})
        for cluster in list(feedback_summary.get("clusters") or [])[:2]:
            if not isinstance(cluster, dict):
                continue
            rows.append(
                {
                    "title": str(cluster.get("theme") or "feedback").replace("_", " ").title(),
                    "detail": str(cluster.get("summary") or "Feedback summary is waiting for a recorded reason.").strip(),
                    "tag": str(cluster.get("severity") or "Risk").replace("_", " ").title(),
                }
            )
        if not str(candidate.get("tour_url") or _property_candidate_source_virtual_tour_url(candidate, facts=facts) or "").strip():
            rows.append({"title": "360 gap", "detail": _tour_source_gap_detail(candidate), "tag": "Review"})
        for item in _missing_fact_items(facts)[:2]:
            if str(item.get("status") or "").strip().lower() == "filled":
                continue
            rows.append(
                {
                    "title": str(item.get("label") or item.get("field") or "Missing fact").strip(),
                    "detail": str(item.get("evidence") or item.get("display_value") or "Still under research.").strip(),
                    "tag": "Research",
                }
            )
        for risk_key, title, detail in (
            ("air_quality_risk", "Air quality", "Pollution burden and recurring exposure need a closer look."),
            ("crime_risk", "Safety", "The quarter-level safety pattern needs a closer look."),
            ("parking_pressure_risk", "Parking pressure", "Street-parking burden needs a closer look where no garage is included."),
            ("heat_resilience_risk", "Summer heat", "Check whether the home can stay cooler through longer heat periods using climate, floor, orientation, cooling, shade, facade shading, and local cooling corridors."),
            ("drinking_water_risk", "Water quality", "Drinking-water source and groundwater burden need a closer look."),
            ("cesspit_risk", "Senkgrube or septic", "Recurring cost, smell, or maintenance burden need a closer look."),
            ("winter_access_risk", "Winter access", "Snow, slope, and seasonal driveability need a closer look."),
            ("flood_risk", "Flood exposure", "Historic flooding and runoff exposure still need verification."),
        ):
            if bool(facts.get(risk_key)):
                rows.append({"title": title, "detail": detail, "tag": "Risk"})
        austria_notes = [
            str(note or "").strip()
            for note in list(facts.get("austria_preference_notes") or [])
            if str(note or "").strip()
        ]
        austria_notes.sort(
            key=lambda note: (
                0
                if any(
                    token in note.lower()
                    for token in ("flowing water", "cooling-corridor", "summer heat", "local summer cooling")
                )
                else 1
            )
        )
        for note in austria_notes[:2]:
            detail = str(note or "").strip()
            if detail:
                rows.append({"title": "Austria fit rule", "detail": detail.capitalize(), "tag": "Eligibility"})
        if not rows:
            rows.append({"title": "No open issue yet", "detail": "No explicit blocker is attached to this home yet.", "tag": "Clear"})
        return rows[:4]

    def _candidate_timeline_rows(candidate: dict[str, object], facts: dict[str, object]) -> list[dict[str, str]]:
        rows = [
            {
                "title": "Found on list",
                "detail": str(candidate.get("source_label") or "List").strip() or "List",
                "tag": "Found",
            },
            {
                "title": "Fit",
                "detail": _clean_property_candidate_detail_copy(candidate.get("fit_summary") or "")
                or _clean_property_candidate_copy(candidate.get("recommendation") or "Home selected for review."),
                "tag": "Fit",
            },
            {
                "title": "360 state",
                "detail": str(candidate.get("tour_url") or _property_candidate_source_virtual_tour_url(candidate, facts=facts) or _tour_status_line(candidate)).strip(),
                "tag": "360",
            },
        ]
        pending_missing = [
            str(item.get("label") or item.get("field") or "Missing fact").strip()
            for item in _missing_fact_items(facts)
            if str(item.get("status") or "").strip().lower() != "filled"
        ]
        if pending_missing:
            rows.append(
                {
                    "title": "Open details",
                    "detail": ", ".join(pending_missing[:3]),
                    "tag": "Research",
                }
            )
        if str(candidate.get("packet_url") or "").strip():
            rows.append(
                {
                    "title": "Property page ready",
                    "detail": "The property page is ready for household or advisor follow-up.",
                    "tag": "Page",
                }
            )
        feedback_summary = dict(candidate.get("feedback_summary") or {}) if isinstance(candidate.get("feedback_summary"), dict) else {}
        household = dict(feedback_summary.get("household_review") or {}) if isinstance(feedback_summary.get("household_review"), dict) else {}
        if int(feedback_summary.get("household_alignment_score") or 0) > 0:
            rows.append(
                {
                    "title": "Household alignment",
                    "detail": f"{int(feedback_summary.get('household_alignment_score') or 0)}/100 · {str(household.get('alignment_label') or feedback_summary.get('family_alignment') or 'waiting').replace('_', ' ')}",
                    "tag": "Household",
                }
            )
        return rows[:5]

    def _candidate_household_rows(candidate: dict[str, object]) -> list[dict[str, str]]:
        feedback_summary = dict(candidate.get("feedback_summary") or {}) if isinstance(candidate.get("feedback_summary"), dict) else {}
        household = dict(feedback_summary.get("household_review") or {}) if isinstance(feedback_summary.get("household_review"), dict) else {}
        rows = [
            {
                "title": str(row.get("stakeholder_label") or "Stakeholder").strip(),
                "detail": str(row.get("reason") or "Household reason is waiting for a recorded decision.").strip(),
                "tag": str(row.get("decision") or "maybe").replace("_", " ").title(),
            }
            for row in list(household.get("stakeholders") or [])[:4]
            if isinstance(row, dict)
        ]
        if not rows:
            rows.append({"title": "No household votes yet", "detail": "Shared reactions will appear here after a property page decision is recorded.", "tag": "Waiting"})
        return rows

    def _candidate_risk_signal_rows(candidate: dict[str, object]) -> list[dict[str, str]]:
        feedback_summary = dict(candidate.get("feedback_summary") or {}) if isinstance(candidate.get("feedback_summary"), dict) else {}
        rows = [
            {
                "title": str(row.get("theme") or "shared note").replace("_", " ").title(),
                "detail": str(row.get("summary") or "A shared note was recorded for this home.").strip(),
                "tag": "Shared",
            }
            for row in list(feedback_summary.get("risk_signal_candidates") or [])[:3]
            if isinstance(row, dict)
        ]
        if not rows:
            rows.append({"title": "No shared note yet", "detail": "A shared watch-out appears here once feedback stays consistent.", "tag": "Pending"})
        return rows

    def _candidate_followup_rows(candidate: dict[str, object]) -> list[dict[str, str]]:
        feedback_rows = [dict(row) for row in list(candidate.get("feedback_rows") or []) if isinstance(row, dict)]
        rows = [
            {
                "feedback_id": str(row.get("feedback_id") or "").strip(),
                "title": str(row.get("text") or row.get("category") or "Follow-up").strip(),
                "detail": str(row.get("followup_note") or row.get("stakeholder_label") or row.get("stakeholder_id") or "").strip(),
                "tag": str(row.get("followup_status") or "suggested").replace("_", " ").title(),
            }
            for row in feedback_rows
            if str(row.get("category") or "").strip() == "question"
        ]
        if not rows:
            rows.append({"feedback_id": "", "title": "No tracked question yet", "detail": "Use Clippy or Ask agent next to start a tracked follow-up.", "tag": "Waiting"})
        return rows[:4]

    def _candidate_recent_change_rows(candidate: dict[str, object]) -> list[dict[str, str]]:
        timeline_rows = [dict(row) for row in list(candidate.get("timeline_rows") or []) if isinstance(row, dict)]
        rows = [
            {
                "title": str(row.get("title") or "Update").strip(),
                "detail": str(row.get("detail") or "Property state updated.").strip(),
                "tag": str(row.get("tag") or "Changed").strip(),
            }
            for row in timeline_rows[:3]
            if str(row.get("detail") or row.get("title") or "").strip()
        ]
        if not rows:
            rows.append({"title": "No new updates yet", "detail": "The visible timeline will summarize what changed after the first decision, shared page, or follow-up update.", "tag": "Waiting"})
        return rows

    def _visual_provider_label(value: object) -> str:
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

    def _tour_payload(candidate: dict[str, object]) -> dict[str, object]:
        raw_tour_payload = dict(candidate.get("tour") or {}) if isinstance(candidate.get("tour"), dict) else {}
        tour_url = str(
            candidate.get("tour_url")
            or candidate.get("verified_tour_url")
            or raw_tour_payload.get("tour_url")
            or raw_tour_payload.get("url")
            or raw_tour_payload.get("embed_url")
            or ""
        ).strip()
        runtime_tour_url = str(
            candidate.get("tour_runtime_url")
            or candidate.get("open_tour_url")
            or candidate.get("verified_tour_url")
            or raw_tour_payload.get("open_tour_url")
            or raw_tour_payload.get("url")
            or raw_tour_payload.get("embed_url")
            or raw_tour_payload.get("provider_url")
            or ""
        ).strip()
        property_facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
        if not tour_url:
            try:
                from app.product import property_tour_hosting

                tour_url = str(
                    property_tour_hosting._existing_hosted_property_tour_url_for_identity(  # type: ignore[attr-defined]
                        property_url=candidate.get("property_url"),
                        source_ref=candidate.get("source_ref"),
                        external_id=(
                            candidate.get("external_id")
                            or candidate.get("listing_id")
                            or property_facts.get("external_id")
                            or property_facts.get("listing_id")
                            or ""
                        ),
                        principal_id=workspace_principal_id,
                    )
                    or ""
                ).strip()
            except Exception:
                tour_url = ""
        runtime_tour_url = runtime_tour_url or tour_url
        provider_tour_url = _property_candidate_source_virtual_tour_url(candidate, facts=property_facts)
        status = str(candidate.get("tour_status") or raw_tour_payload.get("status") or "").strip().lower()
        eta_minutes_raw = str(candidate.get("tour_eta_minutes") or raw_tour_payload.get("eta_minutes") or "").strip()
        try:
            eta_minutes: object = max(0, int(float(eta_minutes_raw))) if eta_minutes_raw else ""
        except (TypeError, ValueError):
            eta_minutes = ""
        reason = str(
            candidate.get("blocked_reason")
            or candidate.get("tour_reason")
            or raw_tour_payload.get("reason_key")
            or raw_tour_payload.get("blocked_reason")
            or raw_tour_payload.get("reason")
            or ""
        ).strip()
        reason_key = _property_visual_reason_key(reason)
        terminal_status = _property_visual_terminal_status_for_reason(request_kind="tour", reason=reason)
        if terminal_status and status in _pending_visual_states:
            status = terminal_status
            eta_minutes = ""
        provider_key = ""
        provider_label = ""
        ready_tour_url = _property_visual_ready_tour_url(
            tour_url=tour_url,
            open_tour_url=runtime_tour_url,
            principal_id=workspace_principal_id,
        )
        if ready_tour_url:
            try:
                from app.product import property_tour_hosting

                provider_key = property_tour_hosting._hosted_property_tour_verified_provider(  # type: ignore[attr-defined]
                    tour_url or ready_tour_url,
                    principal_id=workspace_principal_id,
                )
            except Exception:
                provider_key = ""
            provider_label = _visual_provider_label(provider_key) if provider_key else "3D tour"
            visual_runtime = _visual_runtime_payload(
                candidate,
                request_kind="tour",
                status="ready",
                ready_url=ready_tour_url,
            )
            return {
                "status": "ready",
                "label": "3D tour available",
                "url": ready_tour_url,
                "embed_url": ready_tour_url,
                "eta_label": visual_runtime["eta_label"],
                "progress_pct": visual_runtime["progress_pct"],
                "provider_label": provider_label,
                "provider_key": provider_key,
                "status_detail": "3D tour is ready.",
                "recovery_label": "",
                "control_label": "Open 3D tour",
            }
        if tour_url:
            try:
                from app.product import property_tour_hosting

                verified_tour_url = property_tour_hosting._hosted_property_tour_first_party_open_url(  # type: ignore[attr-defined]
                    tour_url,
                    principal_id=workspace_principal_id,
                )
                provider_key = property_tour_hosting._hosted_property_tour_verified_provider(  # type: ignore[attr-defined]
                    tour_url,
                    principal_id=workspace_principal_id,
                )
            except Exception:
                verified_tour_url = ""
                provider_key = ""
            provider_label = _visual_provider_label(provider_key) if provider_key else "3D tour"
            ready_tour_url = verified_tour_url
            if ready_tour_url:
                verified_provider_keys = {"matterport", "3dvista"}
                status_detail = "3D tour is ready."
                visual_runtime = _visual_runtime_payload(
                    candidate,
                    request_kind="tour",
                    status="ready",
                    ready_url=ready_tour_url,
                )
                return {
                    "status": "ready",
                    "label": "3D tour available",
                    "url": ready_tour_url,
                    "embed_url": verified_tour_url,
                    "eta_label": visual_runtime["eta_label"],
                    "progress_pct": visual_runtime["progress_pct"],
                    "provider_label": provider_label,
                    "provider_key": provider_key,
                    "status_detail": status_detail,
                    "recovery_label": "",
                    "control_label": "Open 3D tour",
                }
            return {
                "status": "blocked",
                "label": "3D tour unavailable",
                "url": "",
                "embed_url": "",
                "eta_label": "A live 3D tour is not available for this listing yet.",
                "progress_pct": 0,
                "provider_label": provider_label,
                "provider_key": provider_key,
                "status_detail": _hosted_tour_unavailable_detail(),
                "recovery_label": "Request a 3D tour",
                "control_label": "",
            }
        if runtime_tour_url:
            try:
                from app.product import property_tour_hosting

                runtime_is_branded_tour = property_tour_hosting._is_branded_public_tour_url(runtime_tour_url)  # type: ignore[attr-defined]
                provider_key = (
                    property_tour_hosting._hosted_property_tour_verified_provider(runtime_tour_url)  # type: ignore[attr-defined]
                    if runtime_is_branded_tour
                    else ""
                )
            except Exception:
                runtime_is_branded_tour = False
                provider_key = ""
            runtime_host = str(urllib.parse.urlparse(runtime_tour_url).hostname or "").strip().lower()
            if runtime_is_branded_tour and runtime_host and not runtime_host.endswith("propertyquarry.com"):
                provider_label = _visual_provider_label(provider_key) if provider_key else "3D tour"
                return {
                    "status": "blocked",
                    "label": "3D tour unavailable",
                    "url": "",
                    "embed_url": "",
                    "eta_label": _hosted_tour_unavailable_detail(),
                    "progress_pct": 0,
                    "provider_label": provider_label,
                    "provider_key": provider_key,
                    "status_detail": _hosted_tour_unavailable_detail(),
                    "recovery_label": "Request a 3D tour",
                    "control_label": "",
                }
        if provider_tour_url:
            try:
                from app.product import property_tour_hosting

                provider_key = property_tour_hosting._property_tour_provider_host_kind(provider_tour_url)  # type: ignore[attr-defined]
            except Exception:
                provider_key = ""
            provider_label = _visual_provider_label(provider_key) if provider_key else "Original tour"
            visual_runtime = _visual_runtime_payload(
                candidate,
                request_kind="tour",
                status="ready",
                ready_url=provider_tour_url,
            )
            return {
                "status": "source",
                "label": "Original tour",
                "url": provider_tour_url,
                "embed_url": provider_tour_url,
                "eta_label": "Original tour",
                "progress_pct": visual_runtime["progress_pct"],
                "provider_label": provider_label,
                "provider_key": provider_key,
                "status_detail": "Original tour is available from the listing; no in-page 3D tour is ready yet.",
                "recovery_label": "",
                "control_label": "Open 3D tour",
            }
        if status in {"queued", "pending"}:
            visual_runtime = _visual_runtime_payload(
                candidate,
                request_kind="tour",
                status=status,
                eta_minutes=eta_minutes,
                reason=reason,
            )
            return {
                "status": visual_runtime["status"],
                "label": "3D tour queued",
                "url": "",
                "embed_url": "",
                "eta_label": visual_runtime["eta_label"],
                "progress_pct": visual_runtime["progress_pct"],
                "provider_label": "",
                "provider_key": "",
                "status_detail": str(visual_runtime.get("status_detail") or "").strip() or ("Tour request is still queued. Taking longer than usual." if str(visual_runtime["eta_label"]).startswith("delayed") else "Tour request queued."),
                "recovery_label": "",
                "control_label": "",
            }
        if status in {"processing", "running", "in_progress", "started", "rendering"}:
            visual_runtime = _visual_runtime_payload(
                candidate,
                request_kind="tour",
                status=status,
                eta_minutes=eta_minutes,
                reason=reason,
            )
            return {
                "status": visual_runtime["status"],
                "label": "3D tour rendering",
                "url": "",
                "embed_url": "",
                "eta_label": visual_runtime["eta_label"],
                "progress_pct": visual_runtime["progress_pct"],
                "provider_label": "",
                "provider_key": "",
                "status_detail": str(visual_runtime.get("status_detail") or "").strip() or ("Tour is still rendering. Taking longer than usual." if str(visual_runtime["eta_label"]).startswith("delayed") else "Tour is rendering now."),
                "recovery_label": "",
                "control_label": "",
            }
        if status == "repairing":
            visual_runtime = _visual_runtime_payload(
                candidate,
                request_kind="tour",
                status=status,
                eta_minutes=eta_minutes,
            )
            return {
                "status": visual_runtime["status"],
                "label": "3D tour refresh running",
                "url": "",
                "embed_url": "",
                "eta_label": visual_runtime["eta_label"],
                "progress_pct": visual_runtime["progress_pct"],
                "provider_label": "",
                "provider_key": "",
                "status_detail": str(visual_runtime.get("status_detail") or "").strip() or "Tour is being refreshed.",
                "recovery_label": "Automatic repair",
                "control_label": "",
            }
        if status in {"blocked", "failed", "skipped", "not_applicable"}:
            visual_runtime = _visual_runtime_payload(
                candidate,
                request_kind="tour",
                status=status,
                eta_minutes=eta_minutes,
                reason=_tour_source_gap_detail(candidate),
            )
            terminal_label = "Retry 3D tour" if status in {"blocked", "failed"} else "Request 3D tour"
            terminal_detail = _tour_source_gap_detail(candidate)
            return {
                "status": "blocked",
                "label": terminal_label,
                "url": "",
                "embed_url": "",
                "eta_label": _tour_source_gap_detail(candidate),
                "progress_pct": 0,
                "provider_label": "",
                "provider_key": "",
                "status_detail": terminal_detail,
                "recovery_label": terminal_label,
                "control_label": "",
                "reason_key": reason_key,
            }
        gap_detail = _tour_source_gap_detail(candidate)
        return {
            "status": "missing",
            "label": "3D tour unavailable",
            "url": "",
            "embed_url": "",
            "eta_label": gap_detail,
            "progress_pct": 0,
            "provider_label": "",
            "provider_key": "",
            "status_detail": gap_detail,
            "recovery_label": "",
            "control_label": "",
        }

    def _flythrough_payload(candidate: dict[str, object]) -> dict[str, object]:
        raw_flythrough_payload = dict(candidate.get("flythrough") or {}) if isinstance(candidate.get("flythrough"), dict) else {}
        flythrough_url = str(candidate.get("flythrough_url") or "").strip()
        status = str(candidate.get("flythrough_status") or raw_flythrough_payload.get("status") or "").strip().lower()
        reason = str(candidate.get("flythrough_reason") or raw_flythrough_payload.get("reason") or "").strip()
        terminal_status = _property_visual_terminal_status_for_reason(request_kind="flythrough", reason=reason)
        if terminal_status and status in _pending_visual_states:
            status = terminal_status
        provider = str(candidate.get("flythrough_provider") or "").strip()
        provider_label = "Walkthrough" if provider else ""
        try:
            from app.product import property_tour_hosting

            open_flythrough_url = property_tour_hosting._hosted_property_tour_walkthrough_open_url(  # type: ignore[attr-defined]
                candidate.get("tour_url"),
                flythrough_url,
                principal_id=workspace_principal_id,
            )
        except Exception:
            open_flythrough_url = ""
        if open_flythrough_url:
            open_url = open_flythrough_url
            visual_runtime = _visual_runtime_payload(
                candidate,
                request_kind="flythrough",
                status="ready",
                ready_url=open_url,
            )
            ready_detail = "Walkthrough available"
            return {
                "status": "ready",
                "label": "Open walkthrough",
                "url": open_url,
                "detail": ready_detail,
                "progress_pct": visual_runtime["progress_pct"],
                "eta_label": visual_runtime["eta_label"],
                "provider_label": provider_label,
                "provider_key": provider,
                "customer_claim_ready": True,
                "status_detail": "Walkthrough is ready.",
                "recovery_label": "",
            }
        if status in {"queued", "pending"}:
            visual_runtime = _visual_runtime_payload(
                candidate,
                request_kind="flythrough",
                status=status,
                eta_minutes=str(candidate.get("flythrough_eta_minutes") or raw_flythrough_payload.get("eta_minutes") or "").strip(),
                reason=reason,
            )
            return {
                "status": visual_runtime["status"],
                "label": "Walkthrough queued",
                "url": "",
                "detail": "Queued after your request.",
                "progress_pct": visual_runtime["progress_pct"],
                "eta_label": visual_runtime["eta_label"],
                "provider_label": provider_label,
                "provider_key": provider,
                "status_detail": str(visual_runtime.get("status_detail") or "").strip() or (
                    "Walkthrough is still queued behind the current visual batch."
                    if str(visual_runtime["eta_label"]).startswith("delayed")
                    else "Walkthrough is queued behind the current visual batch."
                ),
                "recovery_label": "",
            }
        if status in {"processing", "running", "in_progress", "started", "rendering"}:
            visual_runtime = _visual_runtime_payload(
                candidate,
                request_kind="flythrough",
                status=status,
                eta_minutes=str(candidate.get("flythrough_eta_minutes") or raw_flythrough_payload.get("eta_minutes") or "").strip(),
                reason=reason,
            )
            return {
                "status": visual_runtime["status"],
                "label": "Walkthrough in progress",
                "url": "",
                "detail": "Rendering after your request.",
                "progress_pct": visual_runtime["progress_pct"],
                "eta_label": visual_runtime["eta_label"],
                "provider_label": provider_label,
                "provider_key": provider,
                "status_detail": str(visual_runtime.get("status_detail") or "").strip() or ("Walkthrough is still rendering. Taking longer than usual." if str(visual_runtime["eta_label"]).startswith("delayed") else "Walkthrough is rendering now."),
                "recovery_label": "",
            }
        if status == "repairing":
            visual_runtime = _visual_runtime_payload(
                candidate,
                request_kind="flythrough",
                status=status,
                eta_minutes=str(candidate.get("flythrough_eta_minutes") or raw_flythrough_payload.get("eta_minutes") or "").strip(),
                reason=reason,
            )
            return {
                "status": visual_runtime["status"],
                "label": "Walkthrough in progress",
                "url": "",
                "detail": "The request stalled, so PropertyQuarry restarted the background render.",
                "progress_pct": visual_runtime["progress_pct"],
                "eta_label": visual_runtime["eta_label"],
                "provider_label": provider_label,
                "provider_key": provider,
                "status_detail": str(visual_runtime.get("status_detail") or "").strip() or "Walkthrough is being refreshed.",
                "recovery_label": "Automatic repair",
            }
        if status in {"blocked", "failed", "skipped", "not_applicable"}:
            visual_runtime = _visual_runtime_payload(
                candidate,
                request_kind="flythrough",
                status=status,
                eta_minutes=str(candidate.get("flythrough_eta_minutes") or raw_flythrough_payload.get("eta_minutes") or "").strip(),
                reason=reason,
            )
            unavailable_detail = (
                str(raw_flythrough_payload.get("status_detail") or raw_flythrough_payload.get("detail") or "").strip()
                or str(visual_runtime.get("status_detail") or "").strip()
                or _property_visual_unavailable_detail(request_kind="flythrough", reason=reason)
            )
            raw_terminal_label = str(raw_flythrough_payload.get("label") or "").strip()
            terminal_label = raw_terminal_label if "not ready" in raw_terminal_label.lower() else "Walkthrough not ready"
            recovery_label = (
                str(raw_flythrough_payload.get("recovery_label") or "").strip()
                or (raw_terminal_label if raw_terminal_label and raw_terminal_label != terminal_label else "")
                or ("Retry walkthrough" if status in {"blocked", "failed"} else "Request walkthrough")
            )
            return {
                "status": "blocked",
                "label": terminal_label,
                "url": "",
                "detail": unavailable_detail,
                "progress_pct": 0,
                "eta_label": "",
                "provider_label": provider_label,
                "provider_key": provider,
                "status_detail": unavailable_detail,
                "recovery_label": recovery_label,
            }
        return {
            "status": "missing",
            "label": "",
            "url": "",
            "detail": "",
            "progress_pct": 0,
            "eta_label": "",
            "provider_label": provider_label,
            "provider_key": provider,
            "status_detail": "",
            "recovery_label": "",
        }

    def _fit_score_value(candidate: dict[str, object], facts: dict[str, object]) -> int:
        assessment = dict(candidate.get("assessment") or {}) if isinstance(candidate.get("assessment"), dict) else {}
        assessment = assessment or (dict(facts.get("personal_fit_assessment") or {}) if isinstance(facts.get("personal_fit_assessment"), dict) else {})
        for raw_value in (
            candidate.get("fit_score"),
            candidate.get("assessment_fit_score"),
            assessment.get("adjusted_fit_score"),
            assessment.get("fit_score"),
        ):
            if raw_value in (None, ""):
                continue
            try:
                return max(0, min(100, int(round(float(raw_value)))))
            except Exception:
                continue
        return 0

    def _normalized_money_text(text: str) -> str:
        upper_text = text.upper()
        currency = next((code for code in supported_currency_codes() if code in upper_text), "")
        if not currency and "€" in text:
            currency = "EUR"
        money_match = re.search(r"[0-9][0-9\.\,\s]*(?:[,.][0-9]{1,2})?", text)
        if not money_match:
            return text if currency else ""
        number_text = money_match.group(0).replace(" ", "").strip(".,")
        if "." in number_text and "," in number_text:
            number_text = number_text.replace(".", "").replace(",", ".")
        elif "," in number_text:
            integer_part, decimal_part = number_text.rsplit(",", 1)
            number_text = integer_part + decimal_part if len(decimal_part) == 3 else integer_part + "." + decimal_part
        elif number_text.count(".") > 1:
            number_text = number_text.replace(".", "")
        elif "." in number_text:
            integer_part, decimal_part = number_text.rsplit(".", 1)
            if len(decimal_part) == 3 and integer_part.isdigit():
                number_text = integer_part + decimal_part
        try:
            amount = float(number_text)
        except Exception:
            return text if currency else ""
        if amount <= 0:
            return ""
        return f"{currency or workspace_currency_code} {amount:,.0f}".replace(",", ",")

    def _money_display(value: object) -> str:
        if value in (None, "", []):
            return ""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ""
            if supported_currency_strip_pattern.search(text) or "€" in text:
                return _normalized_money_text(text)
            try:
                value = float(text.replace(",", "."))
            except Exception:
                return text
        if isinstance(value, (int, float)):
            amount = float(value)
            if abs(amount) >= 1000:
                formatted = f"{amount:,.0f}".replace(",", ",")
                return f"{workspace_currency_code} {formatted}"
            if amount:
                return f"{workspace_currency_code} {amount:.0f}"
        return ""

    def _money_numeric_value(value: object) -> float | None:
        if value in (None, "", []):
            return None
        if isinstance(value, (int, float)):
            amount = float(value)
            return amount if amount > 0.0 else None
        text = str(value or "").strip()
        if not text:
            return None
        normalized = _normalized_money_text(text) if (supported_currency_strip_pattern.search(text) or "€" in text) else text
        cleaned = supported_currency_strip_pattern.sub("", normalized).replace("€", "").replace(",", "").strip()
        try:
            amount = float(cleaned)
        except Exception:
            return None
        return amount if amount > 0.0 else None

    def _property_investment_price_eur(facts: dict[str, object]) -> float | None:
        for key in ("purchase_price_eur", "buy_price_eur", "price_eur", "price_numeric", "kaufpreis_eur"):
            value = _money_numeric_value(facts.get(key))
            if isinstance(value, float) and value > 0.0:
                return value
        return None

    def _candidate_costs_line(facts: dict[str, object], *, listing_mode: str, price_line: str) -> str:
        normalized_mode = str(listing_mode or "").strip().lower()
        for key in (
            "operating_costs_display",
            "operating_costs_monthly_display",
            "betriebskosten_display",
            "betriebskosten_monatlich_display",
            "service_charges_display",
            "additional_costs_display",
            "side_costs_display",
            "monthly_costs_display",
            "warm_rent_display",
            "cold_rent_display",
            "total_rent_display",
            "gesamtmiete_display",
        ):
            value = str(facts.get(key) or "").strip()
            if value:
                return value
        for key in (
            "operating_costs_monthly",
            "operating_costs_monthly_eur",
            "operating_costs",
            "service_charges_eur",
            "additional_costs_eur",
            "side_costs_eur",
            "betriebskosten_eur",
            "betriebskosten_monatlich_eur",
            "monthly_operating_costs_eur",
        ):
            value = _money_display(facts.get(key))
            if value:
                return f"Costs {value}/mo" if normalized_mode == "buy" else f"Costs {value}"
        if normalized_mode == "rent":
            warm_rent = _money_display(facts.get("warm_rent_eur") or facts.get("warm_rent"))
            cold_rent = _money_display(facts.get("cold_rent_eur") or facts.get("cold_rent"))
            total_rent = _money_display(facts.get("total_rent_eur") or facts.get("rent_eur"))
            if warm_rent and cold_rent and warm_rent != cold_rent:
                return f"Cold {cold_rent} · Warm {warm_rent}"
            if total_rent and total_rent != price_line:
                return f"Monthly total {total_rent}"
            if warm_rent and warm_rent != price_line:
                return f"Warm rent {warm_rent}"
            if cold_rent and cold_rent != price_line:
                return f"Cold rent {cold_rent}"
            return "Operating costs not listed"
        price_per_sqm = _money_per_sqm_line(facts)
        if price_per_sqm:
            return price_per_sqm
        return "Running costs not listed"

    def _title_price_fallback(title: object) -> str:
        text = " ".join(str(title or "").split()).strip()
        if not text:
            return ""
        patterns = [
            r"(€\s?[0-9][0-9\.\s]*(?:,[0-9]{1,2})?\s*,-?)",
            rf"((?:{supported_currency_pattern})\s?[0-9][0-9\.,\s]*)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                raw = " ".join(str(match.group(1) or "").split()).strip(" ,")
                return _normalized_money_text(raw) or raw
        return ""

    def _candidate_price_signal(
        facts: dict[str, object],
        *,
        listing_mode: str,
        title: object,
    ) -> str:
        normalized_mode = str(listing_mode or "").strip().lower()
        if normalized_mode == "buy":
            for key in (
                "price_display",
                "purchase_price_display",
                "buy_price_display",
                "price_eur",
                "purchase_price_eur",
                "buy_price_eur",
            ):
                value = _money_display(facts.get(key)) if key.endswith("_eur") else str(facts.get(key) or "").strip()
                if value:
                    return value
        else:
            for key in (
                "price_display",
                "rent_display",
                "monthly_rent_display",
                "warm_rent_display",
                "cold_rent_display",
                "total_rent_display",
                "price_eur",
                "rent_eur",
                "monthly_rent_eur",
                "warm_rent_eur",
                "cold_rent_eur",
                "total_rent_eur",
            ):
                value = _money_display(facts.get(key)) if key.endswith("_eur") else str(facts.get(key) or "").strip()
                if value:
                    return value
        return _title_price_fallback(title)

    def _candidate_is_generic_listing_page(
        candidate: dict[str, object],
        facts: dict[str, object],
    ) -> bool:
        title = " ".join(str(candidate.get("title") or "").split()).strip().lower()
        url = str(candidate.get("property_url") or "").strip().lower()
        concrete_signals = any(
            (
                facts.get("rooms"),
                facts.get("living_area_sqm"),
                facts.get("area_sqm"),
                facts.get("usable_area_sqm"),
                facts.get("price_eur"),
                facts.get("purchase_price_eur"),
                facts.get("buy_price_eur"),
                facts.get("rent_eur"),
                facts.get("monthly_rent_eur"),
                facts.get("warm_rent_eur"),
                facts.get("cold_rent_eur"),
                facts.get("exact_address"),
                facts.get("street_address"),
            )
        )
        if concrete_signals:
            return False
        generic_title_markers = (
            "immobiliensuche",
            "bestandsobjekte",
            "projekte",
            "projekte in bau",
            "projekte in planung",
            "gemeindewohnungen",
            "angebote",
            "overview",
            "suche",
            "wohnungen",
            "projektdetail",
            "immobilien",
            "projektentwickler",
            "architekturwettbewerbe",
        )
        generic_url_markers = (
            "/suche",
            "/projekte",
            "/projekt/",
            "/angebote",
            "/immobilien/",
            "/immobilien",
            "/overview",
            "/bestandsobjekte",
            "/gemeindewohnungen",
        )
        return any(marker in title for marker in generic_title_markers) or any(marker in url for marker in generic_url_markers)

    def _candidate_is_non_residential(
        candidate: dict[str, object],
        facts: dict[str, object],
    ) -> bool:
        text = " ".join(
            part for part in (
                str(candidate.get("title") or "").strip(),
                str(candidate.get("summary") or "").strip(),
                str(candidate.get("property_url") or "").strip(),
                str(facts.get("property_type") or "").strip(),
            ) if part
        ).lower()
        non_res_markers = (
            "lager",
            "storage",
            "garage",
            "stellplatz",
            "parkplatz",
            "büro",
            "buero",
            "office",
            "gewerbe",
            "geschäftslokal",
            "geschaeftslokal",
            "retail",
            "shop",
            "local",
        )
        residential_markers = ("wohnung", "apartment", "flat", "haus", "house", "penthouse", "garden apartment")
        return any(marker in text for marker in non_res_markers) and not any(marker in text for marker in residential_markers)

    def _candidate_matches_selected_postal_scope(
        candidate: dict[str, object],
        facts: dict[str, object],
        *,
        selected_locations: list[str],
    ) -> bool:
        requested_postal_codes = {
            code
            for value in selected_locations
            for code in _property_postal_codes_from_text(value, require_locality=False)
        }
        if not requested_postal_codes:
            return True
        listing_text = " ".join(
            part for part in (
                str(candidate.get("title") or "").strip(),
                str(candidate.get("summary") or "").strip(),
            ) if part
        )
        listing_postal_codes = set(_property_postal_codes_from_text(listing_text, require_locality=True))
        if listing_postal_codes:
            return bool(listing_postal_codes & requested_postal_codes)
        concrete_text = " ".join(
            part for part in (
                str(candidate.get("title") or "").strip(),
                str(candidate.get("summary") or "").strip(),
                str(candidate.get("property_url") or "").strip(),
                str(facts.get("district") or "").strip(),
                str(facts.get("postal_name") or "").strip(),
                str(facts.get("street_address") or "").strip(),
                str(facts.get("exact_address") or "").strip(),
            ) if part
        )
        found_postal_codes = set(_property_postal_codes_from_text(concrete_text, require_locality=False))
        if not found_postal_codes:
            return True
        return bool(found_postal_codes & requested_postal_codes)

    def _candidate_has_concrete_location_signal(
        candidate: dict[str, object],
        facts: dict[str, object],
    ) -> bool:
        if any(
            str(facts.get(key) or "").strip()
            for key in ("district", "postal_name", "street_address", "exact_address", "address")
        ):
            return True
        text = " ".join(
            part
            for part in (
                str(candidate.get("title") or "").strip(),
                str(candidate.get("summary") or "").strip(),
            )
            if part
        )
        return bool(_property_postal_codes_from_text(text, require_locality=True))

    def _candidate_conflicts_selected_locality(
        candidate: dict[str, object],
        facts: dict[str, object],
        *,
        selected_locations: list[str],
    ) -> bool:
        requested_postal_codes = {
            code
            for value in selected_locations
            for code in _property_postal_codes_from_text(value)
            if code
        }
        requested_localities = {
            str(label.split(" ", 1)[1] or "").strip().casefold()
            for value in selected_locations
            for label in _property_postal_names_from_text(value)
            if " " in label
        }
        if not requested_localities:
            return False
        candidate_text = " ".join(
            part
            for part in (
                str(candidate.get("title") or "").strip(),
                str(candidate.get("summary") or "").strip(),
                str(facts.get("district") or "").strip(),
                str(facts.get("postal_name") or "").strip(),
                str(facts.get("street_address") or "").strip(),
                str(facts.get("exact_address") or "").strip(),
                str(facts.get("address") or "").strip(),
            )
            if part
        )
        candidate_localities = {
            str(label.split(" ", 1)[1] or "").strip().casefold()
            for label in _property_postal_names_from_text(candidate_text)
            if " " in label
        }
        candidate_postal_codes = {code for code in _property_postal_codes_from_text(candidate_text) if code}
        if requested_postal_codes and candidate_postal_codes and not requested_postal_codes.isdisjoint(candidate_postal_codes):
            return False
        if not candidate_localities:
            return False
        return requested_localities.isdisjoint(candidate_localities)

    def _candidate_is_shortlist_admissible(
        candidate: dict[str, object],
        facts: dict[str, object],
        *,
        listing_mode: str,
        selected_locations: list[str],
    ) -> bool:
        if bool(candidate.get("_explicitly_selected_source_candidate")):
            return True
        active_run_ranked = bool(candidate.get("_active_run_ranked"))
        source_family = str(candidate.get("source_family") or facts.get("source_family") or "").strip().lower()
        has_price_signal = bool(_candidate_price_signal(facts, listing_mode=listing_mode, title=candidate.get("title")))
        has_area_signal = bool(
            str(facts.get("living_area_sqm") or "").strip()
            or str(facts.get("area_sqm") or "").strip()
            or str(facts.get("usable_area_sqm") or "").strip()
        )
        if _candidate_is_non_residential(candidate, facts):
            return False
        if _candidate_is_generic_listing_page(candidate, facts):
            return False
        if active_run_ranked:
            return not _candidate_conflicts_selected_locality(
                candidate,
                facts,
                selected_locations=selected_locations,
            )
        if not active_run_ranked:
            if not _candidate_has_concrete_location_signal(candidate, facts):
                return False
            if not _candidate_matches_selected_postal_scope(candidate, facts, selected_locations=selected_locations):
                return False
            if not has_price_signal:
                return False
        if source_family == "developer_projects" and not has_price_signal and not has_area_signal:
            return False
        has_core_signal = bool(
            has_price_signal
            or str(facts.get("rooms") or "").strip()
            or has_area_signal
            or str(candidate.get("tour_url") or "").strip()
            or str(_property_candidate_source_virtual_tour_url(candidate, facts=facts) or "").strip()
            or str(_floorplan_url(facts, candidate=candidate) or "").strip()
        )
        if not has_core_signal:
            return False
        return True

    def _candidate_repair_flag(
        candidate: dict[str, object],
        facts: dict[str, object],
        *,
        listing_mode: str,
        selected_locations: list[str],
    ) -> tuple[str, str]:
        if str(candidate.get("flythrough_raw_status") or "").strip().lower() == "failed" and str(candidate.get("flythrough_url") or "").strip():
            return ("Repair flagged", "Renderer reported failed even though a hosted walkthrough exists.")
        return ("", "")

    first_paint_candidates = [] if management_surface else list(shortlist_candidates)
    if normalized_section in {"properties", "shortlist"}:
        selected_first_paint_candidate = next(
            (
                candidate
                for candidate in first_paint_candidates
                if _workspace_candidate_ref(candidate) == selected_candidate_ref
            ),
            None,
        )
        first_paint_candidates = first_paint_candidates[
            :_PROPERTY_PROPERTIES_FIRST_PAINT_RESULT_LIMIT
        ]
        if (
            selected_first_paint_candidate is not None
            and not any(
                _workspace_candidate_ref(candidate) == selected_candidate_ref
                for candidate in first_paint_candidates
            )
        ):
            first_paint_candidates = [
                *first_paint_candidates[
                    : max(_PROPERTY_PROPERTIES_FIRST_PAINT_RESULT_LIMIT - 1, 0)
                ],
                selected_first_paint_candidate,
            ]
    search_surface_fallback_candidates: list[tuple[dict[str, object], dict[str, object], str]] = []

    def _append_workbench_candidate(
        candidate: dict[str, object],
        facts: dict[str, object],
        *,
        provider_tour_url: str,
    ) -> None:
        admitted_shortlist_candidates.append(candidate)
        price_line = str(
            facts.get("price_display")
            or facts.get("rent_display")
            or facts.get("price_eur")
            or ""
        ).strip()
        parsed_buy_price = _money_numeric_value(facts.get("price_eur"))
        if effective_listing_mode == "buy":
            suspicious_display = _money_numeric_value(price_line) if price_line else None
            if isinstance(parsed_buy_price, float) and parsed_buy_price >= 1000.0 and (
                not price_line
                or not isinstance(suspicious_display, float)
                or suspicious_display < 1000.0
            ):
                price_line = _money_display(parsed_buy_price)
        if not price_line or price_line.lower() == "n/a":
            price_line = _title_price_fallback(candidate.get("title") or "")
        if not price_line:
            price_line = "n/a"
        fit_score = _fit_score_value(candidate, facts)
        layout_parts = [_rooms_layout_part(facts), _area_display(facts)]
        floorplan_url = _floorplan_url(facts, candidate=candidate)
        layout_verified = bool(
            facts.get("has_floorplan")
            or facts.get("floorplan_count")
            or facts.get("floorplans_count")
            or facts.get("floorplan_urls_json")
            or facts.get("floorplan_urls")
            or floorplan_url
        )
        packet_url = str(candidate.get("packet_url") or "").strip()
        review_url = str(candidate.get("review_url") or "").strip()
        if not packet_url and "/app/research/" in review_url:
            packet_url = review_url
        map_url = str(candidate.get("map_url") or "").strip() or _property_candidate_maps_url(candidate)
        tour_status_line = _tour_status_line(candidate)
        ooda_detail = _distance_line(candidate)
        if bool(candidate.get("_explicitly_selected_source_candidate")):
            candidate_ref = _workspace_candidate_ref(candidate)
        else:
            candidate_ref = (
                str(packet_url or "")
                .split("/app/research/", 1)[-1]
                .split("?", 1)[0]
                if "/app/research/" in packet_url
                else _workspace_candidate_ref(candidate)
            )
        if not packet_url and candidate_ref:
            packet_url = f"/app/research/{candidate_ref}"
            if run_id:
                packet_url = f"{packet_url}?run_id={urllib.parse.quote(run_id, safe='')}"
        packet_label = "Property page" if packet_url else "Pending"
        tour_payload = _tour_payload(candidate)
        ooda_rows = _candidate_ooda_rows(candidate, facts)
        risk_payload = _risk_summary(candidate, facts)
        match_reasons = [_clean_property_candidate_copy(item) for item in list(candidate.get("match_reasons") or []) if _clean_property_candidate_copy(item)]
        mismatch_reasons = _property_normalized_mismatch_reasons(
            [_clean_property_candidate_copy(item) for item in list(candidate.get("mismatch_reasons") or []) if _clean_property_candidate_copy(item)],
            facts=facts,
            preferences=property_preferences,
        )
        detail_sections = _candidate_detail_sections(facts)
        candidate_investment = dict(candidate.get("investment") or {}) if isinstance(candidate.get("investment"), dict) else {}
        investment_headline_fallback = (
            "Numbers are still being built from the listing."
            if effective_listing_mode == "buy"
            else ""
        )
        investment_payload = {
            "enabled": effective_listing_mode == "buy",
            "price_per_sqm": _money_per_sqm_line(facts),
            "headline": str(candidate_investment.get("headline") or investment_headline_fallback).strip(),
            "gross_yield_display": str(candidate_investment.get("gross_yield_display") or "").strip(),
            "net_yield_display": str(candidate_investment.get("net_yield_display") or "").strip(),
            "cap_rate_display": str(candidate_investment.get("cap_rate_display") or "").strip(),
            "cash_on_cash_display": str(candidate_investment.get("cash_on_cash_display") or "").strip(),
            "dscr_display": str(candidate_investment.get("dscr_display") or "").strip(),
            "market_delta_display": str(candidate_investment.get("market_delta_display") or "").strip(),
            "expected_rent_display": str(candidate_investment.get("expected_rent_display") or "").strip(),
            "confidence_label": str(candidate_investment.get("confidence_label") or "").strip(),
            "feed_status_label": str(candidate_investment.get("feed_status_label") or "").strip(),
            "feed_status_detail": str(candidate_investment.get("feed_status_detail") or "").strip(),
            "score": candidate_investment.get("score"),
            "score_display": str(candidate_investment.get("score_display") or "").strip(),
            "underwriting_summary": str(candidate_investment.get("underwriting_summary") or "").strip(),
            "strategy": str(candidate_investment.get("strategy") or "").strip(),
            "dimensions": [dict(item) for item in list(candidate_investment.get("dimensions") or []) if isinstance(item, dict)][:7],
            "reasons": [str(item).strip() for item in list(candidate_investment.get("reasons") or []) if str(item).strip()][:3],
            "blockers": [str(item).strip() for item in list(candidate_investment.get("blockers") or []) if str(item).strip()][:3],
        }
        orientation_preview = _property_workbench_lightweight_orientation_preview(
            _property_candidate_orientation_preview(candidate)
        )
        repair_flag_label, repair_flag_detail = _candidate_repair_flag(
            candidate,
            facts,
            listing_mode=effective_listing_mode,
            selected_locations=review_scope_locations,
        )
        external_listing_url = _candidate_external_listing_url(candidate, facts=facts)
        fit_summary = _clean_property_candidate_detail_copy(candidate.get("fit_summary") or "")
        if not fit_summary:
            fit_summary = summarize_property_description_copy(_clean_property_candidate_copy(candidate.get("summary") or ""))
        diorama_preview_url = _property_workbench_candidate_diorama_preview_url(candidate)
        ready_tour_url = str(tour_payload.get("url") or "").strip()
        ready_tour_status = str(tour_payload.get("status") or "").strip().lower()
        workbench_candidate = build_property_workbench_candidate_snapshot(
                candidate_ref=candidate_ref,
                rank=len(workbench_results) + 1,
                title=_property_result_title_display(candidate.get("title") or "Home"),
                recovered_by_filter=bool(candidate.get("recovered_by_filter") or candidate.get("counterfactual_recovered")),
                relaxed_filter_label=str(candidate.get("relaxed_filter_label") or candidate.get("counterfactual_label") or "").strip(),
                preview_image_url=_property_workbench_lightweight_image_url(
                    candidate.get("preview_image_url") or _property_candidate_preview_image(candidate)
                ),
                diorama_preview_url=diorama_preview_url,
                source_label=_compact_provider_label(candidate.get("source_label") or ""),
                location_label=str(facts.get("district") or facts.get("postal_name") or facts.get("city") or facts.get("address") or "").strip(),
                price_display=price_line,
                costs_display=_candidate_costs_line(
                    facts,
                    listing_mode=effective_listing_mode,
                    price_line=price_line,
                ),
                price_per_sqm_display=investment_payload["price_per_sqm"],
                layout_display=" | ".join(part for part in layout_parts if part) or "n/a",
                layout_verification_label="verified" if layout_verified else "unverified",
                fit_score=fit_score,
                fit_label=str(candidate.get("recommendation") or candidate.get("tag") or "Home").strip().replace("_", " ").title(),
                fit_summary=fit_summary,
                tour=tour_payload,
                flythrough=_flythrough_payload(candidate),
                orientation_preview=orientation_preview,
                ooda={
                    "summary": ooda_detail or (match_reasons[0] if match_reasons else fit_summary or "Open the home for the details."),
                    "rows": ooda_rows,
                },
                risk=risk_payload,
                investment=investment_payload,
                match_reasons=match_reasons,
                mismatch_reasons=mismatch_reasons,
                review_page_neuronwriter=dict(candidate.get("review_page_neuronwriter") or {}) if isinstance(candidate.get("review_page_neuronwriter"), dict) else {},
                packet_url=packet_url,
                review_url=str(candidate.get("review_url") or "").strip(),
                property_url=external_listing_url,
                map_url=map_url,
                source_url=external_listing_url,
                floorplan_url=floorplan_url,
                source_virtual_tour_url=provider_tour_url,
                vendor_tour_url=provider_tour_url,
                tour_url=ready_tour_url,
                verified_tour_url=ready_tour_url if ready_tour_status == "ready" else "",
                open_tour_url=ready_tour_url if ready_tour_status == "ready" else "",
                property_facts=facts,
                listing_fact_confirmation=dict(facts.get("listing_fact_confirmation") or {}) if isinstance(facts.get("listing_fact_confirmation"), dict) else {},
                assessment=dict(candidate.get("assessment") or {}) if isinstance(candidate.get("assessment"), dict) else {},
                objection_rows=_candidate_objection_rows(candidate, facts),
                timeline_rows=_candidate_timeline_rows(candidate, facts),
                household_rows=_candidate_household_rows(candidate),
                risk_signal_rows=_candidate_risk_signal_rows(candidate),
                followup_rows=_candidate_followup_rows(candidate),
                recent_change_rows=_candidate_recent_change_rows(candidate),
                official_evidence_rows=[
                    {
                        "title": str(row.get("label") or row.get("risk_key") or "Local context").strip(),
                        "detail": " | ".join(
                            part
                            for part in (
                                str(row.get("source_label") or row.get("provider") or "").strip(),
                                str(row.get("summary") or "").strip(),
                                f"Next: {str(row.get('required_next_step') or '').strip()}" if str(row.get("required_next_step") or "").strip() else "",
                            )
                            if part
                        ) or "Official source linked for this risk check.",
                        "tag": " · ".join(
                            part
                            for part in (
                                str(row.get("availability") or "").replace("_", " ").title(),
                                str(row.get("verification_state") or "").replace("_", " ").title(),
                                str(row.get("confidence") or "").replace("_", " ").title(),
                            )
                            if part
                        ),
                    }
                    for row in list(dict(facts.get("official_risk_evidence") or {}).get("sources") or [])[:4]
                    if isinstance(row, dict)
                ],
                official_posture_rows=_official_risk_posture_rows(
                    dict(facts.get("official_risk_evidence") or {})
                    if isinstance(facts.get("official_risk_evidence"), dict)
                    else {}
                ),
                object_rows=detail_sections["object_rows"],
                cost_rows=detail_sections["cost_rows"],
                feature_values=detail_sections["feature_values"],
                description_text=detail_sections["description_text"],
                location_text=detail_sections["location_text"],
                energy_rows=detail_sections["energy_rows"],
                household_alignment_score=int(dict(candidate.get("feedback_summary") or {}).get("household_alignment_score") or 0) if isinstance(candidate.get("feedback_summary"), dict) else 0,
                household_alignment_label=str(dict(candidate.get("feedback_summary") or {}).get("family_alignment") or "waiting") if isinstance(candidate.get("feedback_summary"), dict) else "waiting",
                repair_flag_label=repair_flag_label,
                repair_flag_detail=repair_flag_detail,
            )
        if bool(candidate.get("_explicitly_selected_source_candidate")):
            workbench_candidate["selected_via_link"] = True
        workbench_results.append(workbench_candidate)
        tour_payload = _tour_payload(candidate)
        results_table_rows.append(
            {
                "cells": [
                    {"title": "Open 3D tour" if str(tour_payload.get("url") or "").strip() else tour_status_line, "detail": "Hosted 3D tour" if str(tour_payload.get("url") or "").strip() else "", "href": str(tour_payload.get("url") or "").strip()},
                    {
                        "title": f"#{len(results_table_rows) + 1} {_property_result_title_display(candidate.get('title') or 'Home') or 'Home'}",
                        "detail": str(candidate.get("source_label") or "").strip(),
                    },
                    {
                        "title": str(candidate.get("recommendation") or candidate.get("tag") or "Home").strip().replace("_", " ").title(),
                        "detail": (
                            _clean_property_candidate_detail_copy(candidate.get("fit_summary") or "")
                            or summarize_property_description_copy(_clean_property_candidate_copy(candidate.get("summary") or ""))
                        ),
                    },
                    {"title": "Open map" if map_url else "Map pending", "detail": "", "href": map_url},
                    {"title": price_line, "detail": ""},
                    {"title": " | ".join(part for part in layout_parts if part) or "n/a", "detail": ""},
                    {"title": ooda_detail or "Property page explains the neighbourhood fit.", "detail": "", "href": packet_url},
                    {"title": packet_label, "detail": packet_url or str(candidate.get("property_url") or "").strip(), "href": packet_url},
                ],
                "packet_url": packet_url,
                "tour_url": str(tour_payload.get("url") or "").strip(),
                "map_url": map_url,
                "source_url": external_listing_url,
            }
        )

    for candidate in first_paint_candidates:
        facts = _property_candidate_display_facts(candidate)
        provider_tour_url = _property_candidate_source_virtual_tour_url(candidate, facts=facts)
        if (
            not bool(candidate.get("_active_run_ranked"))
            and run_has_explicit_listing_context
            and _obvious_listing_mode_mismatch(facts, listing_mode=effective_listing_mode)
        ):
            continue
        if not _candidate_is_shortlist_admissible(
            candidate,
            facts,
            listing_mode=effective_listing_mode,
            selected_locations=review_scope_locations,
        ):
            if normalized_section == "search" and bool(candidate.get("_active_run_ranked")) and not review_scope_locations:
                search_surface_fallback_candidates.append((candidate, facts, provider_tour_url))
            continue
        _append_workbench_candidate(
            candidate,
            facts,
            provider_tour_url=provider_tour_url,
        )

    if not workbench_results and search_surface_fallback_candidates:
        for candidate, facts, provider_tour_url in search_surface_fallback_candidates:
            _append_workbench_candidate(
                candidate,
                facts,
                provider_tour_url=provider_tour_url,
            )

    packet_ready_total = sum(
        1
        for candidate in admitted_shortlist_candidates
        if str(candidate.get("packet_url") or candidate.get("review_url") or "").strip()
    )
    tour_ready_total = sum(
        1
        for candidate in admitted_shortlist_candidates
        if str(_tour_payload(candidate).get("status") or "").strip() == "ready"
    )
    if not compact_summary_surface:
        run_summary_for_surface = dict(run_summary_for_surface)
        admitted_identities = {
            identity
            for identity in (_shortlist_identity(candidate) for candidate in admitted_shortlist_candidates)
            if identity
        }
        def _surface_candidate_with_normalized_mismatches(candidate: dict[str, object]) -> dict[str, object]:
            candidate_row = dict(candidate)
            candidate_facts = _property_candidate_display_facts(candidate_row)
            candidate_row["mismatch_reasons"] = _property_normalized_mismatch_reasons(
                [_clean_property_candidate_copy(item) for item in list(candidate_row.get("mismatch_reasons") or []) if _clean_property_candidate_copy(item)],
                facts=candidate_facts,
                preferences=property_preferences,
            )
            return candidate_row
        if "ranked_candidates" in run_summary_for_surface:
            raw_surface_ranked_candidates = [
                _surface_candidate_with_normalized_mismatches(dict(candidate))
                for candidate in list(run_summary_for_surface.get("ranked_candidates") or [])
                if isinstance(candidate, dict)
            ]
            if admitted_identities:
                held_back_ranked_candidates = [
                    candidate
                    for candidate in raw_surface_ranked_candidates
                    if _shortlist_identity(candidate) not in admitted_identities
                ]
                run_summary_for_surface["ranked_candidates"] = [
                    candidate
                    for candidate in raw_surface_ranked_candidates
                    if _shortlist_identity(candidate) in admitted_identities
                ]
                if held_back_ranked_candidates:
                    run_summary_for_surface["held_back_ranked_total"] = len(held_back_ranked_candidates)
                    run_summary_for_surface["held_back_ranked_reason"] = "outside_selected_area_or_hard_scope"
            elif normalized_section == "shortlist" and raw_surface_ranked_candidates:
                run_summary_for_surface["ranked_candidates"] = []
                run_summary_for_surface["held_back_ranked_total"] = len(raw_surface_ranked_candidates)
                run_summary_for_surface["held_back_ranked_reason"] = "outside_selected_area_or_hard_scope"
            else:
                run_summary_for_surface["ranked_candidates"] = raw_surface_ranked_candidates
        if "sources" in run_summary_for_surface:
            surface_sources: list[dict[str, object]] = []
            for source in list(run_summary_for_surface.get("sources") or []):
                if not isinstance(source, dict):
                    continue
                source_row = dict(source)
                top_candidates = [
                    _surface_candidate_with_normalized_mismatches(dict(candidate))
                    for candidate in list(source_row.get("top_candidates") or [])
                    if isinstance(candidate, dict)
                    and (not admitted_identities or _shortlist_identity(candidate) in admitted_identities)
                ]
                if source_row.get("top_candidates") is not None:
                    source_row["top_candidates"] = top_candidates
                surface_sources.append(source_row)
            run_summary_for_surface["sources"] = surface_sources
        run_payload_for_surface = {**run_payload_for_surface, "summary": run_summary_for_surface}

    hero_actions = {
        "properties": [
            {"href": f"/app/shortlist{run_suffix}", "label": "Open shortlist", "tone": "primary"},
            {"href": f"/app/search{run_suffix}", "label": "Edit brief"},
            {"href": f"/app/agents{run_suffix}", "label": "Saved searches"},
        ],
        "shortlist": [
            {"href": f"/app/properties{run_suffix}", "label": "Open run", "tone": "primary"},
            {"href": f"/app/search{run_suffix}", "label": "Refine search"},
            {"href": f"/app/agents{run_suffix}", "label": "Saved searches"},
        ],
        "research": [
            {"href": f"/app/shortlist{run_suffix}", "label": "Open shortlist", "tone": "primary"},
            {"href": f"/app/properties{run_suffix}", "label": "Open results"},
            {"href": f"/app/alerts{run_suffix}", "label": "Alerts"},
        ],
        "profile": [
            {"href": f"/app/properties{run_suffix}", "label": "Open results", "tone": "primary"},
            {"href": f"/app/shortlist{run_suffix}", "label": "Open shortlist"},
            {"href": f"/app/account{run_suffix}#search-defaults", "label": "Search defaults"},
        ],
        "alerts": [
            {"href": f"/app/properties{run_suffix}", "label": "Open results", "tone": "primary"},
            {"href": f"/app/agents{run_suffix}", "label": "Saved searches"},
            {"href": f"/app/account{run_suffix}#delivery", "label": "Delivery"},
        ],
        "agents": [
            {"href": f"/app/search{run_suffix}", "label": "New search", "tone": "primary"},
            {"href": selected_agent_edit_href or f"/app/search{run_suffix}", "label": "Edit brief"},
            {"href": f"/app/shortlist{run_suffix}", "label": "Open shortlist"},
        ],
        "billing": [
            {
                "href": signed_in_billing_href,
                "label": billing_primary_action_label if billing_handoff.get("available") else "Billing",
                "tone": "primary",
            },
            {"href": f"/app/properties{run_suffix}", "label": "Open run"},
            {"href": "/how-it-works", "label": "Open guide"},
        ],
        "settings": [
            {"href": f"/app/properties{run_suffix}", "label": "Open results", "tone": "primary"},
            {"href": "/how-it-works", "label": "How it works"},
            {"href": signed_in_billing_href, "label": billing_primary_action_label or "Billing"},
        ],
    }
    hero_highlights = {
        "properties": [
            {
                "label": "Market",
                "value": str(property_state.get("country_label") or "Market"),
                "detail": str(search_posture_items[0].get("detail") or "").strip() if search_posture_items else "",
                "href": f"/app/properties{run_suffix}",
            },
            {"label": "Areas", "value": str(len(selected_locations) or 0), "detail": ", ".join(selected_locations[:3]) or "Choose the target areas.", "href": f"/app/properties{run_suffix}"},
            {"label": "Priorities", "value": str(len(selected_keywords) or 0), "detail": ", ".join(selected_keywords[:3]) or "Record what should matter most.", "href": f"/app/properties{run_suffix}"},
            {"label": "Providers", "value": str(len(selected_platforms) or 0), "detail": "Sites chosen for the next search.", "href": f"/app/properties{run_suffix}"},
        ],
        "shortlist": [
            {"label": "Homes", "value": str(len(admitted_shortlist_candidates)), "detail": "Ranked properties worth direct review now.", "href": f"/app/shortlist{run_suffix}"},
            {"label": "Pages", "value": str(packet_ready_total), "detail": "Hosted property pages ready before the source listing.", "href": f"/app/research{run_suffix}"},
            {"label": "3D tours", "value": str(tour_ready_total), "detail": "Hosted or embedded tours already available.", "href": f"/app/research{run_suffix}"},
            {"label": "Search status", "value": run_status_label, "detail": run_message or "Latest search status.", "href": f"/app/properties{run_suffix}"},
        ],
        "research": [
            {"label": "Pages", "value": str(packet_ready_total), "detail": "Hosted property pages ready for inspection.", "href": f"/app/research{run_suffix}"},
            {"label": "Tours", "value": str(tour_ready_total), "detail": "Homes already backed by a live tour or original 360.", "href": f"/app/research{run_suffix}"},
            {"label": "Homes checked", "value": str(_run_homes_checked_total(run_summary)), "detail": "Homes checked in the latest search.", "href": f"/app/properties{run_suffix}"},
            {"label": "Search status", "value": run_status_label, "detail": run_message or "Latest research pass.", "href": f"/app/properties{run_suffix}"},
        ],
        "profile": [
            {"label": "Areas", "value": str(len(selected_locations) or 0), "detail": ", ".join(selected_locations[:3]) or "No areas saved yet.", "href": f"/app/search{run_suffix}"},
            {"label": "Priorities", "value": str(len(selected_keywords) or 0), "detail": ", ".join(selected_keywords[:3]) or "No search brief saved yet.", "href": f"/app/search{run_suffix}"},
            {"label": "Providers", "value": str(len(selected_platforms) or 0), "detail": "Current active site set.", "href": f"/app/properties{run_suffix}"},
            {"label": "Plan", "value": current_plan_label, "detail": str(commercial.get("research_depth") or "deep") + " research", "href": signed_in_billing_href},
        ],
        "alerts": [
            {"label": "Delivered", "value": str(len(recent_matches_card.get("items") or [])), "detail": "Hosted pages already sent.", "href": f"/app/alerts{run_suffix}"},
            {"label": "Recent updates", "value": str(len(run_events[-4:])), "detail": "Latest search updates.", "href": f"/app/alerts{run_suffix}"},
            {"label": "Providers", "value": str(len(selected_platforms) or 0), "detail": "Sites chosen for saved-search alerts.", "href": f"/app/properties{run_suffix}"},
            {"label": "Search status", "value": run_status_label, "detail": run_message or "Latest saved-search sweep.", "href": f"/app/properties{run_suffix}"},
        ],
        "agents": [
            {"label": "Saved searches", "value": str(len(property_search_agents)), "detail": "Recurring briefs available for editing and starting again.", "href": f"/app/agents{run_suffix}"},
            {"label": "Active", "value": str(sum(1 for agent in property_search_agents if agent.get("enabled"))), "detail": "Agents allowed to send matching updates.", "href": f"/app/agents{run_suffix}"},
            {"label": "Delivery", "value": str(property_search_agent.get("notification_label") or "Set per agent"), "detail": "Each recurring search ranks down to the allowed message budget.", "href": f"/app/agents{run_suffix}"},
            {"label": "Reports", "value": "Alerts", "detail": "Digests, status notes, and market watches use the saved delivery channel.", "href": f"/app/agents{run_suffix}"},
        ],
        "billing": [
            {"label": "Plan", "value": current_plan_label, "detail": "Active plan.", "href": signed_in_billing_href},
            {"label": "Depth", "value": str(commercial.get("research_depth") or "deep").title(), "detail": "Research depth for each property.", "href": signed_in_billing_href},
            {"label": "Providers", "value": str(commercial.get("max_platforms") or "Multi"), "detail": "Search-site access for the active plan.", "href": signed_in_billing_href},
            {"label": "Saved searches", "value": ("Unlimited" if int(commercial.get("search_agent_limit") or 0) <= 0 else str(commercial.get("search_agent_limit") or 1)), "detail": "Briefs that can keep running in the background.", "href": signed_in_billing_href},
        ],
        "settings": [
            {"label": "Identity", "value": "Google" if str(google.get("connected_account_email") or "").strip() else "Local", "detail": str(google.get("connected_account_email") or "Sign-in without widening scope."), "href": "/app/account?settings_view=google#connected-services"},
            {"label": "Account", "value": str(workspace.get("name") or "PropertyQuarry"), "detail": workspace_timezone, "href": "/app/account#search-defaults"},
            {"label": "Plan", "value": current_plan_label, "detail": str(commercial.get("research_depth") or "deep") + " research", "href": signed_in_billing_href},
            {"label": "Areas", "value": str(len(selected_locations) or 0), "detail": ", ".join(selected_locations[:2]) or "Saved search areas.", "href": f"/app/properties{run_suffix}"},
        ],
    }

    current_surface_path = {
        "properties": "/app/properties",
        "search": "/app/search",
        "shortlist": "/app/shortlist",
        "research": "/app/research",
        "profile": "/app/account",
        "alerts": "/app/alerts",
        "agents": "/app/agents",
        "billing": "/app/billing",
        "account": "/app/account",
        "settings": "/app/account",
    }.get(normalized_section, "")

    def _href_targets_current_surface(href: object) -> bool:
        href_value = str(href or "").strip()
        if not href_value or not current_surface_path:
            return False
        parsed_href = urllib.parse.urlparse(href_value)
        if parsed_href.fragment:
            return False
        target_path = str(parsed_href.path or "").rstrip("/") or "/"
        active_path = current_surface_path.rstrip("/") or "/"
        return target_path == active_path

    def _strip_current_surface_href(item: dict[str, object]) -> dict[str, object]:
        cleaned = dict(item)
        if _href_targets_current_surface(cleaned.get("href")):
            cleaned.pop("href", None)
        return cleaned

    def _is_redundant_property_action_label(label: object) -> bool:
        return str(label or "").strip().lower() in {"full view", "open full view"}

    def _strip_current_surface_actions_from_item(item: dict[str, object]) -> dict[str, object]:
        cleaned = dict(item)
        for href_key, label_key, method_key in (
            ("action_href", "action_label", "action_method"),
            ("secondary_action_href", "secondary_action_label", "secondary_action_method"),
            ("tertiary_action_href", "tertiary_action_label", "tertiary_action_method"),
            ("quaternary_action_href", "quaternary_action_label", "quaternary_action_method"),
        ):
            if _href_targets_current_surface(cleaned.get(href_key)) or _is_redundant_property_action_label(cleaned.get(label_key)):
                cleaned.pop(href_key, None)
                cleaned.pop(label_key, None)
                cleaned.pop(method_key, None)
        return cleaned

    def _strip_current_surface_actions_from_cards(cards: object) -> list[dict[str, object]]:
        cleaned_cards: list[dict[str, object]] = []
        for raw_card in list(cards or []):
            if not isinstance(raw_card, dict):
                continue
            cleaned_card = dict(raw_card)
            cleaned_items = []
            for raw_item in list(cleaned_card.get("items") or []):
                if isinstance(raw_item, dict):
                    cleaned_items.append(_strip_current_surface_actions_from_item(raw_item))
                else:
                    cleaned_items.append(raw_item)
            cleaned_card["items"] = cleaned_items
            cleaned_cards.append(cleaned_card)
        return cleaned_cards

    hero_actions = {
        section_key: [
            dict(action)
            for action in list(actions or [])
            if str(action.get("href") or "").strip()
            and (section_key != normalized_section or not _href_targets_current_surface(action.get("href")))
        ]
        for section_key, actions in hero_actions.items()
    }
    hero_highlights = {
        section_key: [
            _strip_current_surface_href(dict(highlight))
            if section_key == normalized_section
            else dict(highlight)
            for highlight in list(highlights or [])
            if isinstance(highlight, dict)
        ]
        for section_key, highlights in hero_highlights.items()
    }
    preference_rows = [
        row_item(
            "Account",
            str(workspace.get("name") or "PropertyQuarry"),
            "Account",
        ),
        row_item(
            "Google sign-in",
            str(google.get("connected_account_email") or google.get("status") or "Not connected"),
            "Connection",
        ),
        row_item(
            "Timezone",
            workspace_timezone,
            "Preference",
        ),
        row_item(
            "Active plan",
            current_plan_label,
            "Plan",
        ),
    ]
    settings_connection_rows = [
        row_item(
            "Google sign-in",
            "Identity-only return access. PropertyQuarry keeps this separate from inbox sync.",
            "Connection",
        ),
        row_item(
            "Notification delivery",
            "Good matches can leave through Email, Telegram, or WhatsApp once the shortlist is credible enough to notify.",
            "Alerts",
        ),
        row_item(
            "Account settings",
            "Billing, saved defaults, and account details stay easy to find.",
            "Control",
        ),
    ]
    delivery_channel_keys = set(
        str(channel or "").strip().lower()
        for channel in list(property_preferences.get("alert_channels") or [])
        if str(channel or "").strip()
    )
    delivery_channel_keys.update(
        key
        for key, value in channels.items()
        if key in {"email", "telegram", "whatsapp"} and isinstance(value, dict) and str(value.get("status") or "").strip().lower() in {"enabled", "active", "guided_manual", "export_planned"}
    )
    delivery_governance_rows = [
        row_item(
            str(row.get("title") or "Delivery"),
            str(row.get("detail") or "").strip(),
            str(row.get("tag") or "").strip(),
        )
        for row in property_delivery_governance_rows(sorted(delivery_channel_keys))
    ]
    delivery_route_label = (
        str((selected_agent or {}).get("notification_label") or "").strip()
        or str(property_search_agent.get("notification_label") or "").strip()
        or "No alert route saved yet"
    )
    delivery_cap_label = (
        str((selected_agent or {}).get("delivery_label") or "").strip()
        or str(property_search_agent.get("delivery_label") or "").strip()
        or "Set a daily or weekly cap"
    )
    delivery_recovery_label = (
        str(repair_truth_rows[0].get("detail") or "").strip()
        if repair_truth_rows
        else "Delivery retries stay visible here."
    )
    alerts_rows = list(recent_matches_card.get("items") or []) + [
        row_item(
            _run_event_customer_label(event),
            str(event.get("message") or "No further detail.").strip() or "No further detail.",
            "Live",
        )
        for event in run_events[-4:]
        if isinstance(event, dict)
    ]
    alerts_rows.insert(
        0,
        row_item(
            "Notifications",
            f"{delivery_route_label} | {delivery_cap_label}. New matches and follow-ups show here.",
            "Delivery",
        ),
    )
    alerts_rows.insert(
        1,
        row_item(
            "Pause or resume",
            "Channels stay opt-in. Reply STOP to pause alerts and START to resume them.",
            "STOP/START",
        ),
    )
    alerts_rows.insert(
        2,
        row_item(
            "Source follow-up",
            delivery_recovery_label,
            "Watching" if repair_truth_rows else "Quiet",
        ),
    )
    if not alerts_rows:
        alerts_rows = [
            row_item(
                "No page has been sent yet",
                "The first sent page or run update will appear here once the shortlist is ready.",
                "Quiet",
            )
        ]
    plan_catalog = [dict(plan) for plan in list(commercial.get("plan_catalog") or []) if isinstance(plan, dict)]
    current_plan_key = normalize_property_plan_key(commercial.get("current_plan_key") or "free")
    current_plan_spec = next((plan for plan in plan_catalog if str(plan.get("plan_key") or "").strip().lower() == current_plan_key), {})
    current_platform_cap = int(current_plan_spec.get("max_platforms") or commercial.get("max_platforms") or 0)
    current_search_agent_limit = int(current_plan_spec.get("search_agent_limit") or commercial.get("search_agent_limit") or 0)
    current_search_agent_limit_label = (
        "unlimited saved searches"
        if current_search_agent_limit <= 0
        else f"{current_search_agent_limit} saved search{'es' if current_search_agent_limit != 1 else ''}"
    )
    commercial_state = dict(commercial.get("property_commercial") or {})
    commercial_status = str(commercial_state.get("status") or "").strip().lower()
    has_active_paid_plan = current_plan_key in {"plus", "agent"} and commercial_status in {"active", "trialing"}
    payment_status_detail = (
        "Available"
        if bool(property_state.get("billing_checkout_enabled"))
        else ("Access active" if has_active_paid_plan else "Billing not active yet")
    )
    payment_status_tag = (
        "Ready"
        if bool(property_state.get("billing_checkout_enabled"))
        else ("Active" if has_active_paid_plan else "Inactive")
    )
    billing_account_title = (
        "Billing"
        if billing_handoff_available
        else ("Plan status" if has_active_paid_plan else "Billing")
    )
    billing_account_detail = (
        "Opens with this account."
        if billing_handoff_available and billing_handoff_bridge_only
        else (
            "Open billing."
            if billing_handoff_available
        else (
            "Waiting for direct account access."
            if billing_handoff_status == "bridge_ready"
            else (
            "Waiting for direct account access."
            if billing_handoff_status == "login_required"
            else (
                "Waiting for the billing host."
                if billing_handoff_status == "unresolved"
                else (
            "Access stays active. Billing appears here when connected."
            if has_active_paid_plan
            else (
                "Upgrade when you need more research."
                if bool(property_state.get("billing_checkout_enabled"))
                else "Payments are not enabled for this workspace yet."
            )
                )
            )
            )
        )
        )
    )
    billing_account_action_label = (
        billing_primary_action_label
        if billing_handoff_available
        else ""
    )
    billing_account_action_href = signed_in_billing_href if billing_account_action_label else ""
    billing_rows = [
        row_item(
            "Current plan",
            f"{current_plan_label} | {str(commercial.get('research_depth') or 'deep')} research",
            "Plan",
        ),
        row_item(
            "Coverage",
            f"{commercial.get('max_platforms') or 'Multi'} lists | {current_search_agent_limit_label}",
            "Limits",
        ),
        row_item(
            "Account",
            payment_status_detail,
            payment_status_tag,
        ),
    ]
    pending_plan_key = str(commercial_state.get("pending_plan_key") or "").strip()
    pending_order_id = str(commercial_state.get("pending_order_id") or "").strip()
    last_payment_status = str(commercial_state.get("last_payment_status") or "").strip().replace("_", " ")
    last_billing_event_type = str(commercial_state.get("last_billing_event_type") or "").strip().replace("_", " ")
    last_payment_amount = str(commercial_state.get("last_payment_amount_eur") or "").strip()
    payment_failure_statuses = {
        "declined",
        "denied",
        "failed",
        "payment failed",
        "subscription payment failed",
    }
    billing_payment_failed = (
        commercial_status in {"payment_failed", "payment failed"}
        or last_payment_status.lower() in payment_failure_statuses
        or last_billing_event_type.lower() in payment_failure_statuses
    )
    billing_failure_state = (
        {
            "state": "payment_failed",
            "title": "Payment needs attention",
            "detail": (
                "The latest payment did not complete. Your existing account access and saved work are unchanged."
            ),
            "action_href": signed_in_billing_href if billing_handoff_available else "/support",
            "action_label": "Review billing" if billing_handoff_available else "Get billing help",
        }
        if billing_payment_failed
        else {}
    )
    if pending_plan_key and pending_order_id:
        billing_rows.append(
            row_item(
                "Payment pending",
                f"{pending_plan_key.title()} checkout is waiting for payment confirmation.",
                "Pending",
            )
        )
    elif last_payment_status:
        payment_detail = last_payment_status.title()
        if last_payment_amount:
            payment_detail = f"{payment_detail} | EUR {last_payment_amount}"
        if last_billing_event_type:
            payment_detail = f"{payment_detail} | {last_billing_event_type}"
        billing_rows.append(
            row_item(
                "Latest payment",
                payment_detail,
                "Recorded",
            )
        )
    billing_payment_rows = []
    if last_payment_status:
        latest_payment_detail = last_payment_status.title()
        if last_payment_amount:
            latest_payment_detail = f"{latest_payment_detail} | EUR {last_payment_amount}"
        if last_billing_event_type:
            latest_payment_detail = f"{latest_payment_detail} | {last_billing_event_type}"
        billing_payment_rows.append(
            row_item(
                "Latest payment",
                latest_payment_detail,
                "Recorded",
            )
        )
    if commercial.get("active_until"):
        billing_rows.append(
            row_item(
                "Access window",
                str(commercial.get("active_until") or "").strip(),
                "Status",
            )
        )
    billing_upgrade_rows = []
    for plan in plan_catalog:
        plan_key = str(plan.get("plan_key") or "").strip().lower()
        if not plan_key or plan_key == current_plan_key:
            continue
        platform_cap = int(plan.get("max_platforms") or 0)
        search_agent_limit = int(plan.get("search_agent_limit") or 0)
        delta_parts = [
            f"{platform_cap} platforms" if platform_cap else "",
            (
                "unlimited saved searches"
                if search_agent_limit <= 0
                else f"{search_agent_limit} saved search{'es' if search_agent_limit != 1 else ''}"
            ),
            f"{str(plan.get('research_depth') or '').strip()} research".strip() if str(plan.get("research_depth") or "").strip() else "",
        ]
        improvement_parts = []
        if platform_cap > current_platform_cap:
            improvement_parts.append(f"+{platform_cap - current_platform_cap} more portals")
        elif platform_cap < current_platform_cap:
            improvement_parts.append(f"{current_platform_cap - platform_cap} fewer platforms, but a tighter list set")
        if search_agent_limit <= 0 and current_search_agent_limit > 0:
            improvement_parts.append("unlimited saved searches")
        elif search_agent_limit > current_search_agent_limit:
            improvement_parts.append(f"+{search_agent_limit - current_search_agent_limit} more saved searches")
        billing_upgrade_rows.append(
            row_item(
                str(plan.get("display_name") or "Plan"),
                " | ".join(part for part in delta_parts if part) + (
                    f" | {'; '.join(improvement_parts)}" if improvement_parts else ""
                ),
                str(plan.get("checkout_label") or "Plan"),
            )
        )
    if not billing_upgrade_rows:
        billing_upgrade_rows = [
            row_item(
                "No live upgrade catalog available",
                "Plan options are not loaded yet. Your current plan still applies.",
                "Catalog",
            )
        ]
    billing_decision_rows = [
        row_item(
            "Stay on the current tier",
            "Use the current plan until a real run needs broader list coverage or deeper research.",
            "Decision",
        ),
        row_item(
            "Move tiers for a concrete reason",
            "Upgrade when the current caps block a real search run, not because the feature grid sounds bigger.",
            "Decision",
        ),
    ]
    if current_plan_key == "free":
        billing_decision_rows.append(
            row_item(
                "First paid move",
                "Plus adds deeper review on a wider list set; Agent opens every supported list.",
                "Next tier",
            )
        )
    elif current_plan_key == "plus":
        billing_decision_rows.append(
            row_item(
                "When to jump to Agent",
                "Move when the search needs both full list coverage and the heaviest research depth at the same time.",
                "Next tier",
            )
        )
    else:
        billing_decision_rows.append(
            row_item(
                "Agent plan",
                "The focus here is not another upgrade. It is making sure deeper research is actually useful.",
                "Current tier",
            )
        )
    billing_history_rows = []
    billing_events = [
        dict(event)
        for event in list(commercial_state.get("billing_events_json") or [])
        if isinstance(event, dict)
    ]
    invoice_handoffs_by_event = {
        str(row.get("event_id") or "").strip(): dict(row)
        for row in list(commercial.get("invoice_handoffs") or [])
        if isinstance(row, dict) and str(row.get("event_id") or "").strip()
    }
    for event in list(reversed(billing_events))[:5]:
        event_type = str(event.get("event_type") or "billing event").strip().replace("_", " ").replace(".", " ")
        event_status = str(event.get("payment_status") or "").strip().replace("_", " ")
        event_plan = str(event.get("plan_key") or "").strip().title()
        event_amount = str(event.get("amount_eur") or "").strip()
        event_handoff = invoice_handoffs_by_event.get(str(event.get("event_id") or "").strip(), {})
        event_invoice_id = str(event_handoff.get("invoice_id") or event.get("invoice_id") or "").strip()
        event_accounting_status = str(event_handoff.get("state") or event.get("accounting_status") or "").strip().replace("_", " ")
        event_vat = str(event_handoff.get("vat_amount_eur") or event.get("vat_amount_eur") or "").strip()
        event_vat_rate = str(event_handoff.get("vat_rate") or event.get("vat_rate") or "").strip()
        event_when = str(event.get("recorded_at") or "").strip()[:16].replace("T", " ")
        detail_parts = [part for part in (event_status.title(), f"EUR {event_amount}" if event_amount else "", event_when) if part]
        if event_invoice_id:
            detail_parts.append(f"Invoice {event_invoice_id}")
        elif event_accounting_status:
            detail_parts.append(event_accounting_status.title())
        if event_vat:
            detail_parts.append(f"VAT EUR {event_vat}")
        elif event_vat_rate:
            detail_parts.append(f"VAT {event_vat_rate}")
        billing_history_rows.append(
            row_item(
                event_type.title(),
                " | ".join(detail_parts) or "Recorded by the billing webhook.",
                event_plan or "Payment",
            )
        )
    if not billing_history_rows:
        billing_history_rows.append(
            row_item(
                "No payment history yet",
                "Payment events will appear here after a payment, cancellation, refund, or failed attempt.",
                "History",
            )
        )
    billing_history_rows.extend(
        [
            {
                **row_item(
                    "Cancellation and refunds",
                    "Policy, refund handling, and failed-payment recovery live on the public refund page.",
                    "Policy",
                ),
                "action_href": "/refunds",
                "action_label": "Open policy",
            },
            row_item(
                "Invoices",
                "Invoice and VAT document details appear here after the billing account returns them.",
                "Invoice",
            ),
        ]
    )
    research_rows = []
    for candidate in admitted_shortlist_candidates[:6]:
        title = _property_result_title_display(candidate.get("title") or "Property page")
        reasons = [
            _clean_property_candidate_copy(item)
            for item in list(candidate.get("match_reasons") or [])[:2]
            if _clean_property_candidate_copy(item)
        ]
        candidate_facts = _property_candidate_display_facts(candidate)
        mismatches = _property_normalized_mismatch_reasons(
            [_clean_property_candidate_copy(item) for item in list(candidate.get("mismatch_reasons") or []) if _clean_property_candidate_copy(item)],
            facts=candidate_facts,
            preferences=property_preferences,
            limit=2,
        )
        detail_parts = []
        external_listing_url = _candidate_external_listing_url(candidate)
        tour_url = str(_tour_payload(candidate).get("url") or "").strip()
        fit_summary = _clean_property_candidate_detail_copy(candidate.get("fit_summary") or "")
        if not fit_summary:
            fit_summary = summarize_property_description_copy(_clean_property_candidate_copy(candidate.get("summary") or ""))
        if fit_summary:
            detail_parts.append(fit_summary)
        if reasons:
            detail_parts.append("; ".join(str(reason).strip() for reason in reasons if str(reason).strip()))
        if mismatches:
            detail_parts.append("Risks: " + "; ".join(str(reason).strip() for reason in mismatches if str(reason).strip()))
        research_rows.append(
            {
                "title": title,
                "detail": " | ".join(part for part in detail_parts if part) or "Open the property page to review fit and open questions.",
                "tag": str(candidate.get("tag") or candidate.get("recommendation") or "Page").strip() or "Page",
                "action_href": str(candidate.get("packet_url") or candidate.get("review_url") or candidate.get("tour_url") or candidate.get("property_url") or "").strip(),
                "action_method": "get",
                "action_label": "Open property",
                "secondary_action_href": str(external_listing_url or tour_url or "").strip(),
                "secondary_action_method": "get" if (external_listing_url or tour_url) else "",
                "secondary_action_label": "Open listing" if external_listing_url else ("Open 3D tour" if tour_url else ""),
            }
        )
    if not research_rows:
        research_rows = list(recent_matches_card.get("items") or []) or [
            row_item(
                "Research pages have not been opened yet",
                "As soon as a run finishes with credible matches, the strongest candidates will be promoted into hosted property pages here.",
                "First page",
            )
        ]
    saved_search_rows = [
        {
            "title": "Current saved search",
            "detail": " | ".join(
                part for part in (
                    str(property_state.get("country_label") or "").strip(),
                    f"{len(selected_locations)} target area(s)" if selected_locations else "",
                    f"{len(selected_platforms)} provider(s)" if selected_platforms else "",
                ) if part
            ) or "No saved search brief yet.",
            "tag": "Saved",
            "action_href": f"/app/search{run_suffix}",
            "action_method": "get",
            "action_label": "Open results",
        },
        {
            "title": "Latest search",
            "detail": run_message or "Open results to launch or monitor the next sweep.",
            "tag": run_status_label,
            "action_href": f"/app/properties{run_suffix}",
            "action_method": "get",
            "action_label": "Open results",
        },
        {
            "title": "Delivery path",
            "detail": "Email, Telegram, and WhatsApp stay quiet until the shortlist is credible enough to notify.",
            "tag": "Alerts",
            "action_href": "/app/account?billing=1#delivery",
            "action_method": "get",
            "action_label": "Review delivery",
        },
    ]
    agent_management_rows = build_agent_management_rows(property_search_agents, run_id=run_id)
    if not agent_management_rows:
        agent_management_rows = [
            row_item(
                "No saved search yet",
                "Create one from search, then return here to edit, pause, or review its notification budget.",
                "First search",
            )
        ]
    editable_search_defaults_items = [
        {
            "title": "Search defaults",
            "detail": "Market, areas, lists, budget, and what matters are edited in the Search workflow.",
            "tag": "Editable",
            "action_href": f"/app/properties{run_suffix}",
            "action_method": "get",
            "action_label": "Edit search",
        }
    ]

    sections: dict[str, dict[str, object]] = {
        "properties": {
            "title": "Search",
            "summary": (
                "Review the final results table."
                if run_status_value in {"processed", "completed"} and results_table_rows
                else (
                    "Keep coverage, site health, and the next useful update visible while the search is active."
                    if run_in_progress
                    else "This surface is for search health, partial coverage, and the last completed sweep."
                )
            ),
            "hero_kicker": "Search",
            "hero_title": (
                "Review the finished search in one table."
                if run_status_value in {"processed", "completed"} and results_table_rows
                else ("Keep this search visible until the shortlist is ready." if run_in_progress else "No search is active right now.")
            ),
            "hero_summary": (
                "Coverage and pages."
                if run_status_value in {"processed", "completed"} and results_table_rows
                else (
                    "Health and coverage."
                    if run_in_progress
                    else "No active search."
                )
            ),
            "hero_actions": [{"href": f"/app/properties{run_suffix}", "label": "Open search"}, {"href": f"/app/shortlist{run_suffix}", "label": "Open shortlist"}] if run_in_progress else (hero_actions["properties"] if not (run_status_value in {"processed", "completed"} and results_table_rows) else [
                {"href": f"/app/search{run_suffix}", "label": "Refine search", "tone": "primary"},
                {"href": f"/app/shortlist{run_suffix}", "label": "Open shortlist"},
                {"href": f"/app/agents{run_suffix}", "label": "Saved searches"},
            ]),
            "hero_highlights": [
                {"label": "Search status", "value": run_status_label, "detail": run_message or "Current live search status."},
                (
                    {
                        "label": "Lists",
                        "value": str(run_provider_display_total),
                        "detail": "Selected lists are checking the chosen areas.",
                    }
                    if run_provider_display_total > 0
                    and run_source_variant_total > run_provider_display_total
                    else {
                        "label": "Search scope",
                        "value": "Selected",
                        "detail": "Checking the saved brief.",
                    }
                ),
                {"label": "Homes checked", "value": str(_run_homes_checked_total(run_summary)), "detail": "Homes checked so far."},
            ] if run_in_progress else (hero_highlights["properties"] if not (run_status_value in {"processed", "completed"} and results_table_rows) else [
                {"label": "Results", "value": str(len(results_table_rows)), "detail": "Final matching homes in this search."},
                {"label": "Pages", "value": str(packet_ready_total), "detail": "Hosted property pages ready now."},
                {"label": "3D tours", "value": str(tour_ready_total), "detail": "Hosted tours available right now."},
            ]),
            "primary_cards": [] if (run_status_value in {"processed", "completed"} and results_table_rows) or run_in_progress else [search_posture_card, market_coverage_card],
            "secondary_cards": [] if run_status_value in {"processed", "completed"} and results_table_rows else ([run_card] if run_in_progress else [run_card, recent_matches_card]),
            "console_form": property_form,
            "show_brief_form": not ((run_status_value in {"processed", "completed"} and results_table_rows) or run_in_progress),
            "show_run_panel": run_in_progress,
            "show_shortlist_cards": False,
            "show_results_table": run_status_value in {"processed", "completed"} and bool(results_table_rows),
            "results_table_headers": ["360", "Home", "Fit", "Map", "Price", "Layout", "Summary", "Review"],
            "results_table_rows": results_table_rows,
        },
        "shortlist": {
            "title": "Shortlist",
            "summary": "Use one calm results table for the strongest homes and open the full property page only when a card deserves it.",
            "hero_kicker": "Shortlist",
            "hero_title": "Review the best homes before you open deeper property pages.",
            "hero_summary": "Best matches first.",
            "hero_actions": hero_actions["shortlist"],
            "hero_highlights": hero_highlights["shortlist"],
            "primary_cards": [shortlist_card],
            "secondary_cards": [run_card, market_coverage_card],
            "console_form": {},
            "show_brief_form": False,
            "show_shortlist_cards": True,
        },
        "research": {
            "title": "Research",
            "summary": "Turn matching homes into clean property pages with maps, 3D tours, and follow-ups.",
            "hero_kicker": "Research pages",
            "hero_title": "Open the strongest property pages first.",
            "hero_summary": "Fit, open details, maps, and tours where they exist.",
            "hero_actions": hero_actions["research"],
            "hero_highlights": hero_highlights["research"],
            "primary_cards": [
                {
                    "eyebrow": "Research pages",
                    "title": "Open the strongest property pages first",
                    "body": "Hosted property pages and 3D tours stay primary. Source links remain secondary.",
                    "items": research_rows,
                }
            ],
            "secondary_cards": [recent_matches_card, run_card],
            "console_form": {},
            "show_brief_form": False,
            "show_shortlist_cards": False,
        },
        "profile": {
            "title": "Profile Learning",
            "summary": "Show what the product learned, what should be quieter next time, and which requirements remain explicit.",
            "hero_kicker": "Profile learning",
            "hero_title": "Make the learning loop visible and editable.",
            "hero_summary": "Likes, dislikes, and requirements must survive beyond one search. This is where future searches become personal instead of repeating the same weak matches.",
            "hero_actions": hero_actions["profile"],
            "hero_highlights": hero_highlights["profile"],
            "primary_cards": [learning_card],
            "secondary_cards": [
                {
                    "eyebrow": "Saved brief",
                    "title": "Current profile state",
                    "body": "The saved search brief should be easy to inspect without reopening the full workflow.",
                    "items": list(search_posture_card.get("items") or []),
                },
                {
                    "eyebrow": "Account",
                    "title": "Who this profile belongs to",
                    "body": "Identity and connection state stay narrow and explicit on PropertyQuarry.",
                    "items": preference_rows,
                },
            ],
            "console_form": {},
            "show_brief_form": False,
            "show_shortlist_cards": False,
        },
        "alerts": {
            "title": "Alerts",
            "summary": "See what was sent, what is waiting, and what needs attention.",
            "hero_kicker": "Alerts",
            "hero_title": "Sent pages and updates.",
            "hero_summary": "Sent pages, notifications, and search updates in one place.",
            "hero_actions": hero_actions["alerts"],
            "hero_highlights": hero_highlights["alerts"],
            "primary_cards": [
                {
                    "eyebrow": "Recent activity",
                    "title": "Recent pages and updates",
                    "body": "Sent pages, replies, and search updates.",
                    "items": alerts_rows,
                }
            ],
            "secondary_cards": [
                {
                    "eyebrow": "Saved search",
                    "title": "Saved search",
                    "body": "The search behind these alerts stays easy to review and change.",
                    "items": saved_search_rows,
                },
                {
                    "eyebrow": "Alerts",
                    "title": "Notifications",
                    "body": "Pick only the channels you actually want to hear from.",
                    "items": delivery_governance_rows,
                },
                run_card,
            ],
            "console_form": {},
            "show_brief_form": False,
            "show_shortlist_cards": False,
        },
        "agents": {
            "title": "Saved searches",
            "summary": "Review saved searches, start them again, and open recent results.",
            "hero_kicker": "Saved searches",
            "hero_title": "Saved searches.",
            "hero_summary": f"{sum(1 for agent in property_search_agents if agent.get('enabled'))} active | {len(property_search_agents)} saved.",
            "hero_actions": hero_actions["agents"],
            "hero_highlights": hero_highlights["agents"],
            "primary_cards": [
                {
                    "eyebrow": "Selected search",
                    "title": str((selected_agent or {}).get("name") or "Open a saved search"),
                    "body": (
                        ""
                        if selected_agent
                        else ""
                    ),
                    "items": (
                        [
                            {
                                "title": "Scope",
                                "detail": str((selected_agent or {}).get("scope_label") or "No scope saved"),
                                "tag": str((selected_agent or {}).get("status_label") or "Idle"),
                                "action_href": selected_agent_open_href or f"/app/agents{run_suffix}",
                                "action_method": "get",
                                "action_label": "Open watch",
                                "secondary_action_href": selected_agent_edit_href or f"/app/search{run_suffix}",
                                "secondary_action_method": "get",
                                "secondary_action_label": "Edit",
                            },
                            row_item("Notification cap", str((selected_agent or {}).get("delivery_label") or "Set a daily or weekly cap."), str((selected_agent or {}).get("notification_label") or "Budget")),
                            row_item("Schedule", str((selected_agent or {}).get("run_label") or "Waiting for the first search."), "Timing"),
                            row_item(
                                "Latest finished run",
                                (
                                    _run_outcome_compact_detail(selected_agent_latest_run)
                                    if selected_agent_latest_run
                                    else "No finished search yet."
                                ),
                                str((selected_agent_latest_run or {}).get("status_label") or "Waiting"),
                            ),
                        ]
                    ),
                },
                {
                    "eyebrow": "Saved searches",
                    "title": "Saved searches",
                    "body": "",
                    "items": agent_management_rows,
                }
            ],
            "secondary_cards": [
                {
                    "eyebrow": "Alerts",
                    "title": "Alerts",
                    "body": "",
                    "items": [
                        row_item("Notification cap", str((selected_agent or {}).get("delivery_label") or "Set a daily or weekly cap."), str((selected_agent or {}).get("notification_label") or "Budget")),
                        row_item("Updates", "Daily and weekly alerts use the chosen notification channel.", "Alerts"),
                        row_item(
                            "Latest outcome",
                            (
                                _run_outcome_compact_detail(selected_agent_latest_run)
                                if selected_agent_latest_run
                                else "No finished recurring run has produced a delivery summary yet."
                            ),
                            str((selected_agent_latest_run or {}).get("status_label") or "Waiting"),
                        ),
                    ],
                },
                {
                    "eyebrow": "Recovery",
                    "title": "Recovery",
                    "body": "",
                    "items": repair_truth_rows + (
                        fleet_digest_items[:2]
                        if fleet_digest_items
                        else [row_item("Next check", "If a source slips, the next saved-search run will show whether it came back cleanly.", "Watching")]
                    ),
                },
                {
                    "eyebrow": "Limits",
                    "title": "Limits",
                    "body": "",
                    "items": [
                        row_item("Free", "1 active saved search.", "Plan"),
                        row_item("Plus", "3 active saved searches.", "Plan"),
                        row_item("Agent", "Unlimited saved searches.", "Plan"),
                    ],
                },
                {
                    "eyebrow": "Latest outcomes",
                    "title": "Recent searches",
                    "body": "",
                    "items": (
                        [
                            {
                                "title": str(run.get("title") or "Saved search"),
                                "detail": f"{str(run.get('status_label') or 'Search').strip()} | {_run_outcome_compact_detail(run)}",
                                "tag": str(run.get("top_fit_score") or 0),
                                "action_href": str(run.get("href") or ""),
                                "action_method": "get",
                                "action_label": "Open results",
                            }
                            for run in (selected_agent_runs[:3] if selected_agent_runs else previous_search_runs[:3])
                        ]
                        or [row_item("No finished search yet", "The first completed search will show matches, updates, and homes just outside the search here.", "Waiting")]
                    ),
                },
                run_card,
            ],
            "console_form": {},
            "show_brief_form": False,
            "show_shortlist_cards": False,
        },
        "billing": {
            "title": "Billing",
            "failure_state": billing_failure_state,
            "summary": "Plan, payments, and invoices.",
            "hero_kicker": "Billing",
            "hero_title": "Plan and payments.",
            "hero_summary": "Plan, payments, and invoices.",
            "hero_actions": hero_actions["billing"],
            "hero_highlights": hero_highlights["billing"],
            "primary_cards": [
                {
                    "eyebrow": "Plan",
                    "title": "Current access",
                    "body": "",
                    "items": billing_rows,
                },
                {
                    "eyebrow": "Account",
                    "title": billing_account_title,
                    "body": "",
                    "items": [
                        *billing_payment_rows,
                        {
                            **row_item(
                                billing_account_title,
                                billing_account_detail,
                                "Ready" if billing_handoff_available else ("Active" if has_active_paid_plan else "Decision"),
                            ),
                            "action_href": billing_account_action_href,
                            "action_method": "get",
                            "action_label": billing_account_action_label,
                        },
                    ],
                },
            ],
            "secondary_cards": [
                {
                    "eyebrow": "Upgrade",
                    "title": "Tier changes",
                    "body": "",
                    "items": billing_upgrade_rows,
                },
                {
                    "eyebrow": "History",
                    "title": "Account events",
                    "body": "",
                    "items": billing_history_rows,
                },
            ],
            "console_form": {},
            "show_brief_form": False,
            "show_shortlist_cards": False,
            "show_billing_cards": True,
        },
        "account": {
            "title": "Account",
            "failure_state": billing_failure_state,
            "summary": "Search, notifications, plan, and access.",
            "hero_kicker": "Account",
            "hero_title": "Account.",
            "hero_summary": "Search, notifications, plan, and access.",
            "hero_actions": [],
            "hero_highlights": [
                {"label": "Identity", "value": "Google" if str(google.get("connected_account_email") or "").strip() else "Local", "detail": str(google.get("connected_account_email") or "Sign-in without widening scope."), "href": "/app/account?settings_view=google#connected-services"},
                {"label": "Plan", "value": current_plan_label, "detail": str(commercial.get("research_depth") or "deep") + " research", "href": signed_in_billing_href},
                {"label": "Saved searches", "value": str(len(property_search_agents)), "detail": "Recurring searches ready to edit or start again.", "href": f"/app/agents{run_suffix}"},
                {"label": "Areas", "value": str(len(selected_locations) or 0), "detail": ", ".join(selected_locations[:2]) or "Saved search areas.", "href": f"/app/search{run_suffix}"},
            ],
            "primary_cards": [
                {
                    "id": "settings",
                    "eyebrow": "Connections",
                    "title": "Sign-in and access",
                    "body": "",
                    "items": preference_rows + settings_connection_rows,
                },
                {
                    "id": "plans",
                    "eyebrow": "Plan",
                    "title": "Current access",
                    "body": "",
                    "items": billing_rows,
                },
                {
                    "id": "profile",
                    "eyebrow": "Saved defaults",
                    "title": "Search defaults",
                    "body": "",
                    "items": editable_search_defaults_items,
                },
                {
                    "id": "delivery",
                    "eyebrow": "Delivery",
                    "title": "Notifications",
                    "body": "",
                    "items": [
                        row_item("Recurring searches", f"{len(property_search_agents)} saved searches ready to edit or start again.", "Saved searches"),
                        row_item("Delivery", "Digests and recurring market watches use your saved-search settings and chosen channel.", "Reports"),
                        row_item("Return access", str(google.get("connected_account_email") or "Sign-in without widening scope."), "Identity"),
                    ],
                },
                {
                    "eyebrow": "Next change",
                    "title": "Edit",
                    "body": "",
                    "items": [
                        row_item("Search", "Change areas, requirements, lists, or shortlist depth.", "Search"),
                        row_item("Plan", "Open the billing account when the current allowance blocks a real run.", "Plan"),
                        row_item("How it works", "Privacy, sharing, and search basics.", "Guide"),
                    ],
                },
            ],
            "secondary_cards": [{
                "eyebrow": "Links",
                "title": "Public pages",
                "body": "",
                "items": [
                    {
                        "title": "How it works",
                        "detail": "Privacy, sharing, and search basics.",
                        "tag": "Guide",
                        "action_href": "/how-it-works",
                        "action_method": "get",
                        "action_label": "Open guide",
                    },
                ],
            }],
            "console_form": {},
            "show_brief_form": False,
            "show_shortlist_cards": False,
        },
        "settings": {
            "title": "Account",
            "summary": "Search, notifications, plan, and access.",
            "hero_kicker": "Account",
            "hero_title": "Account.",
            "hero_summary": "Search, notifications, plan, and access.",
            "hero_actions": [
                {"href": f"/app/search{run_suffix}", "label": "Edit search", "tone": "primary"},
                {"href": f"/app/agents{run_suffix}", "label": "Saved searches"},
                {"href": signed_in_billing_href, "label": billing_primary_action_label or "Billing"},
            ],
            "hero_highlights": [
                {"label": "Identity", "value": "Google" if str(google.get("connected_account_email") or "").strip() else "Local", "detail": str(google.get("connected_account_email") or "Sign-in without widening scope."), "href": "/app/account?settings_view=google#connected-services"},
                {"label": "Plan", "value": current_plan_label, "detail": str(commercial.get("research_depth") or "deep") + " research", "href": signed_in_billing_href},
                {"label": "Saved searches", "value": str(len(property_search_agents)), "detail": "Recurring searches ready to edit or start again.", "href": f"/app/agents{run_suffix}"},
                {"label": "Areas", "value": str(len(selected_locations) or 0), "detail": ", ".join(selected_locations[:2]) or "Saved search areas.", "href": f"/app/search{run_suffix}"},
            ],
            "primary_cards": [
                {
                    "id": "settings",
                    "eyebrow": "Connections",
                    "title": "Sign-in and access",
                    "body": "",
                    "items": preference_rows + settings_connection_rows,
                },
                {
                    "id": "profile",
                    "eyebrow": "Saved defaults",
                    "title": "Search defaults",
                    "body": "",
                    "items": editable_search_defaults_items,
                },
                {
                    "eyebrow": "Next change",
                    "title": "Edit",
                    "body": "",
                    "items": [
                        row_item("Search brief", "Go back to Search when the market, list mix, or shortlist depth needs adjustment.", "Search"),
                        row_item("Plan", "Open the billing account when the current allowance blocks a real run.", "Plan"),
                        row_item("How it works", "Privacy, sharing, and search basics.", "Guide"),
                    ],
                },
            ],
            "secondary_cards": [billing_rows and {
                "id": "plans",
                "eyebrow": "Plan",
                "title": "Billing",
                "body": "",
                "items": billing_rows,
            } or {}, {
                "eyebrow": "Links",
                "title": "Public pages",
                "body": "",
                "items": [
                    {
                        "title": "Billing",
                        "detail": billing_account_detail or "Open the billing portal when it is connected.",
                        "tag": "Public",
                        "action_href": signed_in_billing_href,
                        "action_method": "get",
                        "action_label": billing_account_action_label or "Billing",
                    },
                    {
                        "title": "How it works",
                        "detail": "Privacy, sharing, and search basics.",
                        "tag": "Guide",
                        "action_href": "/how-it-works",
                        "action_method": "get",
                        "action_label": "Open guide",
                    },
                ],
            }],
            "console_form": {},
            "show_brief_form": False,
            "show_shortlist_cards": False,
        },
    }

    payload = dict(sections.get(section, sections["properties"]))
    payload["primary_cards"] = _strip_current_surface_actions_from_cards(payload.get("primary_cards"))
    payload["secondary_cards"] = _strip_current_surface_actions_from_cards(payload.get("secondary_cards"))
    payload["account_status"] = _compact_property_account_status(status)
    if isinstance(property_state.get("account_google"), dict) and property_state.get("account_google"):
        payload["account_status"]["google_account"] = dict(property_state.get("account_google") or {})
    if isinstance(property_state.get("access_links"), dict) and property_state.get("access_links"):
        payload["account_status"]["access_links"] = dict(property_state.get("access_links") or {})
    shortlist_snapshot = build_property_shortlist_snapshot(
        workbench_results,
        selected_candidate_ref=selected_candidate_ref,
    )
    workbench_results = [dict(row) for row in list(shortlist_snapshot.get("results") or []) if isinstance(row, dict)]
    selected_result = dict(shortlist_snapshot.get("selected") or {})
    score_methodology = build_property_score_methodology(
        language_code=property_preferences.get("language_code"),
        country_code=selected_country_code,
        candidate=selected_result,
    )
    run_health_summary = dict(run_health or {})
    workbench_filtered_total = int(
        run_health_summary.get("filtered_total")
        or run_health_summary.get("held_back_total")
        or run_summary.get("filtered_total")
        or run_summary.get("held_back_total")
        or 0
    )
    if workbench_filtered_total <= 0 and suppression_rows:
        workbench_filtered_total = sum(
            max(int(float((row or {}).get("affected_total") or 0)), 0)
            for row in suppression_rows
            if isinstance(row, dict) and (row.get("rule_key") or "").strip() != "Below fit threshold"
        )
    workbench_score_demoted_total = int(
        run_health_summary.get("score_demoted_total")
        or run_health_summary.get("filtered_low_fit_total")
        or run_summary.get("score_demoted_total")
        or run_summary.get("filtered_low_fit_total")
        or 0
    )
    workbench_held_back_total = int(
        run_health_summary.get("held_back_total")
        or run_summary.get("held_back_total")
        or workbench_filtered_total
        or 0
    )
    if management_surface:
        surface_workbench_results = [
            safe_candidate
            for candidate in workbench_results
            if isinstance(candidate, dict)
            and (safe_candidate := _property_management_candidate_payload(candidate))
        ]
        surface_selected_result = _property_management_candidate_payload(selected_result) if selected_result else {}
        surface_progress_current_property = (
            _property_management_candidate_payload(progress_current_property)
            if progress_current_property
            else {}
        )
        surface_progress_route_previews = [
            safe_route
            for route in progress_route_previews
            if isinstance(route, dict)
            and (safe_route := _property_management_route_payload(route))
        ]
        client_results = [dict(candidate) for candidate in surface_workbench_results]
        client_selected = dict(surface_selected_result or (client_results[0] if client_results else {}))
    else:
        surface_workbench_results = workbench_results
        surface_selected_result = selected_result
        surface_progress_current_property = progress_current_property
        surface_progress_route_previews = progress_route_previews
        client_results = []
        for candidate in surface_workbench_results:
            if not isinstance(candidate, dict):
                continue
            candidate_ref = str(candidate.get("candidate_ref") or "").strip()
            client_results.append(
                _property_workbench_client_candidate_payload(
                    candidate,
                    preserve_preview_media_facts=bool(selected_candidate_ref) and candidate_ref == selected_candidate_ref,
                    principal_id=workspace_principal_id,
                )
            )
        client_selected = (
            _property_workbench_client_candidate_payload(
                surface_selected_result,
                preserve_preview_media_facts=True,
                principal_id=workspace_principal_id,
            )
            if surface_selected_result
            else (client_results[0] if client_results else {})
        )
    client_run_summary = _property_workbench_client_run_summary_payload(run_summary_for_surface)
    client_run = _property_workbench_client_run_payload(
        {
            **run_payload_for_surface,
            "summary": client_run_summary,
            "message": run_message,
            "events": run_events[-10:],
            "route_previews": surface_progress_route_previews,
            "current_property": surface_progress_current_property,
            "provider_display_total": run_provider_display_total,
            "source_variant_display_total": run_source_variant_total,
            "selected_platform_count": len(selected_platforms),
            "filtered_total": workbench_filtered_total,
            "held_back_total": workbench_held_back_total,
            "score_demoted_total": workbench_score_demoted_total,
        }
    )
    surface_recent_packets = [
        {
            "title": str(item.get("title") or item.get("label") or "Property page").strip(),
            "detail": (
                property_run_customer_safe_status_detail(
                    str(item.get("tag") or item.get("title") or "").strip().lower().replace(" ", "_"),
                    str(item.get("detail") or "").strip(),
                    summary=raw_run_summary,
                    prefer_repair_step=True,
                )
                or str(item.get("detail") or "").strip()
            ),
            "tag": str(item.get("tag") or "Page").strip(),
            "url": str(item.get("action_href") or "").strip(),
        }
        for item in list(recent_matches_card.get("items") or [])[:5]
        if isinstance(item, dict)
    ]
    if management_surface:
        surface_recent_packets = [
            safe_packet
            for packet in surface_recent_packets
            if (safe_packet := _property_management_recent_packet_payload(packet))
        ]
    decision_workbench = PropertyDecisionWorkbenchContract(
        run=PropertyDecisionWorkbenchRunContract(
            run_id=run_id,
            status=run_status_value or "not_started",
            status_label=run_status_label,
            progress=int(run_health.get("progress") or run_payload.get("progress") or 0),
            message=run_message,
            status_url=str(run_health.get("status_url") or run_payload.get("status_url") or "").strip(),
            filtered_total=workbench_filtered_total,
            score_demoted_total=workbench_score_demoted_total,
            held_back_total=workbench_held_back_total,
            summary=client_run_summary,
            events=run_events[-10:],
            worker_state=search_worker_state,
            reliability=_property_run_reliability_summary(
                {
                    "status": run_status_value or "not_started",
                    "progress": int(run_health.get("progress") or run_payload.get("progress") or 0),
                    "message": run_message,
                    "eta_label": run_eta_label,
                    "summary": client_run_summary,
                },
                results_total=int(shortlist_snapshot.get("results_total") or len(workbench_results)),
            ),
            research_task_total=research_task_total,
            open_research_task_total=open_research_task_total,
            filled_research_task_total=filled_research_task_total,
            dismissed_research_task_total=dismissed_research_task_total,
            provider_display_total=run_provider_display_total,
            source_variant_display_total=run_source_variant_total,
            selected_platform_count=len(selected_platforms),
            route_previews=surface_progress_route_previews,
            current_property=surface_progress_current_property,
        ),
        brief=PropertyDecisionWorkbenchBriefContract(
            country=str(property_state.get("country_label") or "Market"),
            search_goal=selected_search_goal,
            search_goal_label=property_search_goal_label,
            mode=mode_visibility_label,
            investment_strategy_label=property_investment_strategy_label if property_is_investment_search else "",
            region=str(property_state.get("region_label") or property_preferences.get("region_code") or "").strip(),
            areas=selected_locations,
            priorities=selected_keywords,
            providers=selected_platforms,
            plan=effective_run_plan_label,
            plan_key=effective_run_plan_key,
            research_depth=effective_run_research_depth,
        ),
        brief_preferences=brief_preferences_payload,
        endpoints={
            "preferences": str(property_meta.get("preferences_endpoint") or "").strip(),
            "start": str(property_meta.get("start_endpoint") or "").strip(),
            "billing_order": str(property_meta.get("billing_order_endpoint") or "").strip(),
            "delete_run_template": "/app/api/property/search-runs/__RUN_ID__",
        },
        counterfactual_rows=counterfactual_rows,
        recent_packets=surface_recent_packets,
        previous_search_runs=previous_search_runs,
        current_scope_preview=current_scope_preview,
        search_agents=(
            property_search_agents
            if include_search_agent_payload
            else []
        ),
        search_agent=(
            property_search_agent
            if include_search_agent_payload
            else {}
        ),
        results=surface_workbench_results,
        client_results=client_results,
        client_selected=client_selected,
        client_run=client_run,
        search_guard_rows=[],
        suppression_rows=suppression_rows,
        delivery_proof_rows=delivery_proof_rows,
        artifact_receipt_rows=artifact_receipt_rows,
        score_methodology=score_methodology,
        research_tasks=[],
        research_task_counts={
            "total": research_task_total,
            "open": open_research_task_total,
            "filled": filled_research_task_total,
            "dismissed": dismissed_research_task_total,
        },
        selected_candidate_ref=str(
            surface_selected_result.get("candidate_ref")
            or (shortlist_snapshot.get("selected_candidate_ref") if not management_surface else "")
            or ""
        ).strip(),
        selected=surface_selected_result,
        empty_outcome=(
            {}
            if bool(shortlist_snapshot.get("has_results"))
            else build_property_empty_outcome_summary(
                run_summary=run_summary,
                run_sources=run_sources,
                run_status_value=run_status_value,
                run_message=run_message,
                counterfactual_rows=counterfactual_rows,
                suppression_rows=suppression_rows,
            )
        ),
        packet_recovery=packet_recovery,
        route_recovery=route_recovery,
        failure_state=dict(payload.get("failure_state") or {}),
        show_brief_default=not (run_in_progress or (run_status_value in {"processed", "completed"} and bool(shortlist_snapshot.get("has_results")))),
    )
    payload["billing_handoff"] = billing_handoff
    contract = PropertySurfacePayloadContract(
        title=str(payload.get("title") or ""),
        summary=str(payload.get("summary") or ""),
        stats=list(base.get("stats") or []),
        current_plan_label=current_plan_label,
        run_payload=_compact_property_run_payload_for_template(run_payload_for_surface),
        run_summary=client_run_summary,
        preference_manager=preference_manager,
        decision_workbench=decision_workbench,
        extras={
            key: value
            for key, value in payload.items()
            if key not in {"title", "summary"}
        },
    )
    return contract.to_dict()
