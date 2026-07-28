from __future__ import annotations

import contextlib
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import RequestContext, get_container, get_request_context
from app.api.routes.landing import (
    _console_shell_context,
    _render_public_template,
    app_shell,
)
from app.api.routes.landing_property_research import (
    _customer_facing_vendor_tour_url,
    _evidence_detail_rows,
    _object_detail_row,
    _property_candidate_ref,
    _property_distance_ooda_rows,
    _property_hosted_tour_disabled_fallback,
    _property_research_money_display,
    _property_rooms_display,
    _property_tour_media_payload,
    _property_tour_requestability,
    _render_console_object_detail,
)
from app.api.routes.landing_content import app_nav_groups_for_brand
from app.api.routes.landing_view_models import (
    PROPERTY_FURNITURE_STYLE_CATALOG,
    _clean_property_candidate_copy as _shared_clean_property_candidate_copy,
)
from app.container import AppContainer
from app.product import property_tour_hosting
from app.product.service import (
    _hosted_property_visual_progress_snapshot,
    _hosted_property_visual_progress_stage_label,
    _property_currency_code_from_facts,
    _property_visual_eta_label,
    _property_visual_progress_pct,
    _property_visual_unavailable_detail,
    build_product_service,
)
from app.services.property_customer_copy import summarize_property_description_copy
from app.services.public_branding import request_brand

router = APIRouter(tags=["landing"])


def _is_propertyquarry_request(request: Request) -> bool:
    return str(request_brand(request).get("key") or "").strip() == "propertyquarry"


def _raise_propertyquarry_object_detail_disabled(request: Request) -> None:
    if _is_propertyquarry_request(request):
        raise HTTPException(status_code=404, detail="propertyquarry_object_detail_not_available")


def _propertyquarry_handoff_task_allowed(task_type: str) -> bool:
    return str(task_type or "").strip() in {"delivery_followup", "property_alert_review", "property_tour_followup"}


def _google_delivery_action(reason: str, *, return_to: str) -> dict[str, str]:
    normalized = str(reason or "").strip()
    label = "Connect Google" if normalized in {"google_oauth_binding_not_found", "google_account_missing"} else "Reconnect Google"
    return {
        "label": label,
        "href": f"/app/actions/google/connect?return_to={urllib.parse.quote(return_to, safe='/?:=&')}",
        "method": "get",
    }


def _property_fit_label(assessment: dict[str, object]) -> str:
    fit_score = assessment.get("fit_score")
    recommendation = str(assessment.get("recommendation") or "").strip().replace("_", " ")
    try:
        score_text = f"{float(fit_score):.0f}/100" if fit_score not in (None, "") else "unscored"
    except (TypeError, ValueError):
        score_text = "unscored"
    if recommendation:
        return f"{score_text} · {recommendation}"
    return score_text


def _propertyquarry_clean_listing_copy(value: object) -> str:
    return _shared_clean_property_candidate_copy(value)


def _propertyquarry_layout_summary(facts: dict[str, object]) -> str:
    rooms_text = _property_rooms_display(facts)
    raw_area = facts.get("area_m2") or facts.get("living_area_m2") or facts.get("area_sqm")
    area_text = ""
    try:
        area_value = float(str(raw_area).replace(",", ".").strip()) if raw_area not in (None, "", []) else 0.0
        if area_value > 0:
            area_text = f"{area_value:.0f} m2" if float(area_value).is_integer() else f"{area_value:.1f} m2"
    except Exception:
        area_text = str(raw_area or "").strip()
    return " · ".join(part for part in (rooms_text, area_text) if part)


def _propertyquarry_price_summary(facts: dict[str, object]) -> str:
    currency_code = _property_currency_code_from_facts(facts)
    for key in (
        "price_display",
        "rent_display",
        "price",
        "price_eur",
        "total_rent_eur",
        "rent_eur",
        "monthly_rent_eur",
        "purchase_price_eur",
    ):
        value = _property_research_money_display(facts.get(key), currency_code=currency_code)
        if value:
            return value
    return ""


def _propertyquarry_meta_rows(facts: dict[str, object]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    price_text = _propertyquarry_price_summary(facts)
    if price_text:
        rows.append({"label": "Price", "value": price_text})
    layout_text = _propertyquarry_layout_summary(facts)
    if layout_text:
        rows.append({"label": "Size", "value": layout_text})
    area_text = str(
        facts.get("address")
        or facts.get("postal_name")
        or facts.get("location_label")
        or facts.get("district")
        or ""
    ).strip()
    if area_text:
        rows.append({"label": "Where", "value": area_text})
    return rows


def _propertyquarry_summary_line(facts: dict[str, object]) -> str:
    address_text = str(
        facts.get("address")
        or facts.get("postal_name")
        or facts.get("location_label")
        or facts.get("district")
        or ""
    ).strip()
    layout_text = _propertyquarry_layout_summary(facts)
    price_text = _propertyquarry_price_summary(facts)
    return " · ".join(part for part in (address_text, layout_text, price_text) if part)


def _handoff_customer_status(
    *,
    handoff,
    delivery_followup_open: bool,
    property_tour_followup_open: bool,
    retry_detail: str,
) -> tuple[str, str]:
    resolution = str(handoff.resolution or "").strip().lower()
    delivery_reason = str(handoff.delivery_reason or "").strip().lower()
    task_type = str(handoff.task_type or "").strip().lower()
    if task_type == "delivery_followup":
        if resolution == "sent":
            return "Sent", "Delivery was recorded as sent."
        if resolution == "waiting_on_principal":
            return "Waiting on you", "Delivery is paused until you choose the next step."
        if resolution == "reauth_needed" or delivery_reason.startswith("google_"):
            return "Reconnect Google", "Google access needs attention before delivery can continue."
        if resolution == "failed":
            return "Still blocked", "Delivery is still blocked and needs another path."
        if delivery_followup_open:
            return "Needs another try", retry_detail or "The prepared send needs another attempt."
    if task_type == "property_tour_followup":
        if str(handoff.tour_url or "").strip():
            return "3D tour available", "The 3D tour is available to review."
        if property_tour_followup_open:
            return "3D tour in progress", "The 3D tour is still being prepared."
    return "Open follow-up", "This page keeps only the next useful action and the supporting links."


def _handoff_property_visual_actions(
    *,
    candidate: dict[str, object],
    media_payload: dict[str, object],
    tour_request_enabled: bool = True,
    flythrough_request_enabled: bool = True,
) -> list[dict[str, object]]:
    property_url = str(candidate.get("property_url") or candidate.get("listing_url") or "").strip()
    if not property_url:
        return []
    source_ref = str(candidate.get("source_ref") or "").strip()
    run_id = str(candidate.get("run_id") or "").strip()
    candidate_ref = str(candidate.get("candidate_ref") or "").strip() or _property_candidate_ref(candidate)
    base = {
        "property_url": property_url,
        "source_ref": source_ref,
        "run_id": run_id,
        "candidate_ref": candidate_ref,
    }
    pending_states = {"queued", "pending"}
    running_states = {"processing", "running", "in_progress", "started", "rendering", "repairing"}
    terminal_states = {"blocked", "failed", "skipped", "not_applicable"}
    actions: list[dict[str, object]] = []
    tour_url = str(candidate.get("tour_url") or "").strip()
    hosted_tour_ready = bool(media_payload.get("hosted_ready"))
    tour_status = str(candidate.get("tour_status") or "").strip().lower()
    tour_reason = str(candidate.get("blocked_reason") or candidate.get("tour_reason") or "").strip()
    tour_eta_raw = str(candidate.get("tour_eta_minutes") or "").strip()
    tour_requested_at = str(candidate.get("tour_requested_at") or "").strip()
    tour_status_updated_at = str(candidate.get("tour_status_updated_at") or "").strip()
    try:
        tour_progress_pct = int(float(str(candidate.get("tour_progress_pct") or "").strip())) if str(candidate.get("tour_progress_pct") or "").strip() else 0
    except Exception:
        tour_progress_pct = 0
    if tour_progress_pct <= 0:
        tour_progress_pct = _property_visual_progress_pct(
            request_kind="tour",
            status=tour_status,
            ready_url=tour_url,
            eta_minutes=tour_eta_raw,
            requested_at=tour_requested_at,
            status_updated_at=tour_status_updated_at,
        )
    tour_eta_label = _property_visual_eta_label(
        request_kind="tour",
        status=tour_status,
        eta_minutes=tour_eta_raw,
        requested_at=tour_requested_at,
        status_updated_at=tour_status_updated_at,
    )
    if not hosted_tour_ready:
        if tour_url:
            if tour_request_enabled:
                actions.append(
                    {
                        **base,
                        "kind": "tour",
                        "label": "Request 3D tour",
                        "state": "idle",
                        "progress_pct": 0,
                        "eta_label": "",
                        "status_detail": "A real 3D tour is not available yet.",
                        "poll_after_seconds": 0,
                    }
                )
        elif tour_status in pending_states:
            actions.append(
                {
                    **base,
                    "kind": "tour",
                    "label": "3D tour queued",
                    "state": "pending",
                    "progress_pct": max(tour_progress_pct, 14),
                    "eta_label": tour_eta_label,
                    "status_detail": (
                        "Still queued."
                        if tour_eta_label.startswith("delayed")
                        else f"Queued{f' · {tour_eta_label}' if tour_eta_label else ''}."
                    ),
                    "poll_after_seconds": 10,
                }
            )
        elif tour_status in running_states:
            actions.append(
                {
                    **base,
                    "kind": "tour",
                    "label": "3D tour rendering",
                    "state": "rendering",
                    "progress_pct": max(tour_progress_pct, 58),
                    "eta_label": tour_eta_label,
                    "status_detail": (
                        "Still rendering."
                        if tour_eta_label.startswith("delayed")
                        else f"Rendering{f' · {tour_eta_label}' if tour_eta_label else ''}."
                    ),
                    "poll_after_seconds": 10,
                }
            )
        elif tour_status in terminal_states:
            if tour_request_enabled:
                actions.append(
                    {
                        **base,
                        "kind": "tour",
                        "label": "Retry 3D tour" if tour_status in {"blocked", "failed"} else "Request 3D tour",
                        "state": tour_status or "blocked",
                        "progress_pct": 0,
                        "eta_label": "",
                        "status_detail": _property_visual_unavailable_detail(request_kind="tour", reason=tour_reason),
                        "poll_after_seconds": 0,
                    }
                )
        elif tour_request_enabled:
            actions.append(
                {
                    **base,
                    "kind": "tour",
                    "label": "Request 3D tour",
                    "state": "idle",
                    "progress_pct": 0,
                    "eta_label": "",
                    "status_detail": "Use the floor plan and photos to build one.",
                    "poll_after_seconds": 0,
                }
            )

    walkthrough_ready = bool(str(media_payload.get("walkthrough_href") or "").strip())
    flythrough_status = str(candidate.get("flythrough_status") or "").strip().lower()
    flythrough_reason = str(candidate.get("flythrough_reason") or "").strip()
    flythrough_eta_raw = str(candidate.get("flythrough_eta_minutes") or "").strip()
    flythrough_requested_at = str(candidate.get("flythrough_requested_at") or "").strip()
    flythrough_status_updated_at = str(candidate.get("flythrough_status_updated_at") or "").strip()
    try:
        flythrough_progress_pct = int(float(str(candidate.get("flythrough_progress_pct") or "").strip())) if str(candidate.get("flythrough_progress_pct") or "").strip() else 0
    except Exception:
        flythrough_progress_pct = 0
    if flythrough_progress_pct <= 0:
        flythrough_progress_pct = _property_visual_progress_pct(
            request_kind="flythrough",
            status=flythrough_status,
            ready_url=str(candidate.get("flythrough_url") or "").strip(),
            eta_minutes=flythrough_eta_raw,
            requested_at=flythrough_requested_at,
            status_updated_at=flythrough_status_updated_at,
        )
    flythrough_eta_label = _property_visual_eta_label(
        request_kind="flythrough",
        status=flythrough_status,
        eta_minutes=flythrough_eta_raw,
        requested_at=flythrough_requested_at,
        status_updated_at=flythrough_status_updated_at,
    )
    live_walkthrough_progress = _hosted_property_visual_progress_snapshot(tour_url, request_kind="flythrough") if tour_url else {}
    live_walkthrough_detail = str(live_walkthrough_progress.get("detail") or "").strip()
    try:
        live_walkthrough_progress_pct = int(float(str(live_walkthrough_progress.get("progress_pct") or "").strip())) if str(live_walkthrough_progress.get("progress_pct") or "").strip() else 0
    except Exception:
        live_walkthrough_progress_pct = 0
    if (
        live_walkthrough_progress_pct > 0
        and not walkthrough_ready
        and flythrough_status in pending_states.union(running_states)
    ):
        flythrough_progress_pct = max(flythrough_progress_pct, live_walkthrough_progress_pct)
        stage_label = _hosted_property_visual_progress_stage_label(live_walkthrough_progress) or ""
        if stage_label:
            flythrough_eta_label = stage_label
    if not walkthrough_ready:
        if flythrough_status in pending_states:
            actions.append(
                {
                    **base,
                    "kind": "flythrough",
                    "label": "Walkthrough queued",
                    "state": "pending",
                    "progress_pct": max(flythrough_progress_pct, 18),
                    "eta_label": flythrough_eta_label,
                    "status_detail": live_walkthrough_detail or ("Queued." if not flythrough_eta_label.startswith("delayed") else "Still queued."),
                    "poll_after_seconds": 10,
                    "walkthrough_provider": "magicfit",
                }
            )
        elif flythrough_status in running_states:
            actions.append(
                {
                    **base,
                    "kind": "flythrough",
                    "label": "Walkthrough rendering",
                    "state": "rendering",
                    "progress_pct": max(flythrough_progress_pct, 64),
                    "eta_label": flythrough_eta_label,
                    "status_detail": live_walkthrough_detail or ("Rendering." if not flythrough_eta_label.startswith("delayed") else "Still rendering."),
                    "poll_after_seconds": 10,
                    "walkthrough_provider": "magicfit",
                }
            )
        elif flythrough_status in terminal_states:
            if flythrough_request_enabled:
                actions.append(
                    {
                        **base,
                        "kind": "flythrough",
                        "label": "Retry walkthrough" if flythrough_status in {"blocked", "failed"} else "Request walkthrough",
                        "state": flythrough_status or "blocked",
                        "progress_pct": 0,
                        "eta_label": "",
                        "status_detail": live_walkthrough_detail or _property_visual_unavailable_detail(request_kind="flythrough", reason=flythrough_reason),
                        "poll_after_seconds": 0,
                        "walkthrough_provider": "magicfit",
                    }
                )
        elif flythrough_request_enabled:
            actions.append(
                {
                    **base,
                    "kind": "flythrough",
                    "label": "Request walkthrough",
                    "state": "idle",
                    "progress_pct": 0,
                    "eta_label": "",
                    "status_detail": "Use the floor plan and photos to build one.",
                    "poll_after_seconds": 0,
                    "walkthrough_provider": "magicfit",
                }
            )
    return actions


def _propertyquarry_candidate_visual_request_enabled(candidate: dict[str, object]) -> bool:
    raw_tour = dict(candidate.get("tour") or {}) if isinstance(candidate.get("tour"), dict) else {}
    return bool(
        _property_tour_requestability(
            candidate,
            raw_tour_payload=raw_tour,
        ).get("tour_requestable")
    )


def _merge_propertyquarry_live_visual_status(
    *,
    product,
    principal_id: str,
    candidate: dict[str, object],
) -> dict[str, object]:
    merged = dict(candidate or {})
    property_url = str(merged.get("property_url") or merged.get("listing_url") or "").strip()
    if not product or not str(principal_id or "").strip() or not property_url:
        return merged
    request_args = {
        "principal_id": str(principal_id or "").strip(),
        "run_id": str(merged.get("run_id") or "").strip(),
        "candidate_ref": str(merged.get("candidate_ref") or "").strip(),
        "source_ref": str(merged.get("source_ref") or "").strip(),
        "property_url": property_url,
    }
    with contextlib.suppress(Exception):
        tour_status = product.get_property_visual_request_status(request_kind="tour", **request_args)
        if isinstance(tour_status, dict):
            tour_url = str(tour_status.get("tour_url") or "").strip()
            if tour_url:
                merged["tour_url"] = tour_url
            generated_reconstruction_url = str(tour_status.get("generated_reconstruction_url") or "").strip()
            if generated_reconstruction_url:
                merged["generated_reconstruction_url"] = generated_reconstruction_url
            layout_preview_url = str(tour_status.get("layout_preview_url") or "").strip()
            if layout_preview_url:
                merged["layout_preview_url"] = layout_preview_url
            merged["layout_preview_status"] = str(
                tour_status.get("layout_preview_status") or merged.get("layout_preview_status") or ""
            ).strip().lower()
            merged["tour_status"] = str(tour_status.get("tour_status") or tour_status.get("status") or merged.get("tour_status") or "").strip()
            merged["blocked_reason"] = str(tour_status.get("blocked_reason") or merged.get("blocked_reason") or "").strip()
            for failure_key in ("failure_stage", "error_code", "diagnostic_sha256"):
                merged[failure_key] = str(tour_status.get(failure_key) or merged.get(failure_key) or "").strip()
            merged["tour_progress_pct"] = str(tour_status.get("progress_pct") or merged.get("tour_progress_pct") or "").strip()
            merged["tour_status_updated_at"] = str(tour_status.get("generated_at") or merged.get("tour_status_updated_at") or "").strip()
    with contextlib.suppress(Exception):
        flythrough_status = product.get_property_visual_request_status(request_kind="flythrough", **request_args)
        if isinstance(flythrough_status, dict):
            flythrough_url = str(flythrough_status.get("flythrough_url") or "").strip()
            if flythrough_url:
                merged["flythrough_url"] = flythrough_url
            merged["flythrough_status"] = str(
                flythrough_status.get("flythrough_status") or flythrough_status.get("status") or merged.get("flythrough_status") or ""
            ).strip()
            merged["flythrough_reason"] = str(
                flythrough_status.get("blocked_reason") or flythrough_status.get("flythrough_reason") or merged.get("flythrough_reason") or ""
            ).strip()
            merged["flythrough_progress_pct"] = str(
                flythrough_status.get("progress_pct") or merged.get("flythrough_progress_pct") or ""
            ).strip()
            merged["flythrough_status_updated_at"] = str(
                flythrough_status.get("generated_at") or merged.get("flythrough_status_updated_at") or ""
            ).strip()
    return merged


def _propertyquarry_handoff_identity_value(
    *,
    handoff,
    input_json: dict[str, object],
    primary_candidate: dict[str, object],
    key: str,
) -> str:
    return str(
        input_json.get(key)
        or primary_candidate.get(key)
        or getattr(handoff, key, "")
        or ""
    ).strip()


def _propertyquarry_handoff_property_url(
    *,
    handoff,
    input_json: dict[str, object],
    primary_candidate: dict[str, object],
) -> str:
    return str(
        getattr(handoff, "property_url", "")
        or input_json.get("property_url")
        or primary_candidate.get("property_url")
        or primary_candidate.get("listing_url")
        or ""
    ).strip()


def _propertyquarry_handoff_tour_url(
    *,
    handoff,
    input_json: dict[str, object],
    primary_candidate: dict[str, object],
    principal_id: str = "",
) -> str:
    direct_url = str(
        getattr(handoff, "tour_url", "")
        or input_json.get("tour_url")
        or primary_candidate.get("tour_url")
        or ""
    ).strip()
    if direct_url:
        if not property_tour_hosting._is_branded_public_tour_url(direct_url):
            return direct_url
        if property_tour_hosting._hosted_property_tour_first_party_open_url(
            direct_url,
            principal_id=principal_id,
        ):
            return direct_url
        direct_payload = property_tour_hosting._hosted_property_tour_payload_for_url(
            direct_url,
            principal_id=principal_id,
        )
        if direct_payload:
            if property_tour_hosting._property_tour_payload_is_disabled_fallback(direct_payload):
                return ""
        return ""
    existing_url = property_tour_hosting._existing_hosted_property_tour_url_for_identity(
        property_url=_propertyquarry_handoff_property_url(
            handoff=handoff,
            input_json=input_json,
            primary_candidate=primary_candidate,
        ),
        source_ref=_propertyquarry_handoff_identity_value(
            handoff=handoff,
            input_json=input_json,
            primary_candidate=primary_candidate,
            key="source_ref",
        ),
        external_id=_propertyquarry_handoff_identity_value(
            handoff=handoff,
            input_json=input_json,
            primary_candidate=primary_candidate,
            key="external_id",
        ),
        principal_id=principal_id,
    )
    if not existing_url:
        return ""
    if property_tour_hosting._hosted_property_tour_first_party_open_url(
        existing_url,
        principal_id=principal_id,
    ):
        return existing_url
    existing_payload = property_tour_hosting._hosted_property_tour_payload_for_url(
        existing_url,
        principal_id=principal_id,
    )
    if existing_payload:
        if property_tour_hosting._property_tour_payload_is_disabled_fallback(existing_payload):
            return ""
    return ""


def _propertyquarry_handoff_generated_reconstruction_url(
    *,
    handoff,
    input_json: dict[str, object],
    primary_candidate: dict[str, object],
    principal_id: str = "",
) -> str:
    direct_url = str(
        getattr(handoff, "tour_url", "")
        or input_json.get("tour_url")
        or primary_candidate.get("tour_url")
        or ""
    ).strip()
    candidate_urls = [direct_url] if direct_url else []
    existing_url = property_tour_hosting._existing_hosted_property_tour_url_for_identity(
        property_url=_propertyquarry_handoff_property_url(
            handoff=handoff,
            input_json=input_json,
            primary_candidate=primary_candidate,
        ),
        source_ref=_propertyquarry_handoff_identity_value(
            handoff=handoff,
            input_json=input_json,
            primary_candidate=primary_candidate,
            key="source_ref",
        ),
        external_id=_propertyquarry_handoff_identity_value(
            handoff=handoff,
            input_json=input_json,
            primary_candidate=primary_candidate,
            key="external_id",
        ),
        principal_id=principal_id,
    )
    if existing_url and existing_url not in candidate_urls:
        candidate_urls.append(existing_url)
    for candidate_url in candidate_urls:
        if not candidate_url or not property_tour_hosting._is_branded_public_tour_url(candidate_url):
            continue
        payload = property_tour_hosting._hosted_property_tour_payload_for_url(
            candidate_url,
            principal_id=principal_id,
        )
        if not payload or not isinstance(payload, dict):
            continue
        generated_reconstruction = payload.get("generated_reconstruction")
        if not isinstance(generated_reconstruction, dict):
            continue
        provider = str(generated_reconstruction.get("provider") or "").strip().lower()
        if provider == "propertyquarry_generated_reconstruction":
            return candidate_url
    return ""


def _append_propertyquarry_media_item(
    items: list[dict[str, str]],
    *,
    href: str,
    label: str,
    kind: str,
) -> None:
    normalized_href = str(href or "").strip()
    if not normalized_href or any(row.get("href") == normalized_href for row in items):
        return
    items.append(
        {
            "src": normalized_href,
            "href": normalized_href,
            "label": str(label or "Photo").strip(),
            "kind": str(kind or "photo").strip(),
        }
    )


def _propertyquarry_media_carousel_items(candidate: dict[str, object]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for key in ("image_url", "thumbnail_url", "preview_image_url", "primary_image_url"):
        _append_propertyquarry_media_item(
            items,
            href=str(candidate.get(key) or "").strip(),
            label="Photo",
            kind="photo",
        )
    for key in ("image_urls", "photo_urls", "photos"):
        raw_values = candidate.get(key)
        if not isinstance(raw_values, (list, tuple)):
            continue
        for raw_value in raw_values:
            if isinstance(raw_value, dict):
                image_url = str(raw_value.get("url") or raw_value.get("src") or raw_value.get("href") or "").strip()
            else:
                image_url = str(raw_value or "").strip()
            _append_propertyquarry_media_item(
                items,
                href=image_url,
                label="Photo",
                kind="photo",
            )
    return items


def _propertyquarry_handoff_media_payload(
    *,
    handoff_ref: str,
    handoff,
    input_json: dict[str, object],
    primary_candidate: dict[str, object],
    product=None,
    principal_id: str = "",
) -> dict[str, object]:
    property_url = _propertyquarry_handoff_property_url(
        handoff=handoff,
        input_json=input_json,
        primary_candidate=primary_candidate,
    )
    tour_url = _propertyquarry_handoff_tour_url(
        handoff=handoff,
        input_json=input_json,
        primary_candidate=primary_candidate,
        principal_id=principal_id,
    )
    property_facts = dict(input_json.get("property_facts_json") or {}) if isinstance(input_json.get("property_facts_json"), dict) else {}
    if isinstance(primary_candidate.get("property_facts"), dict):
        property_facts = {**dict(primary_candidate.get("property_facts") or {}), **property_facts}
    media_candidate = {
        **primary_candidate,
        "title": str(input_json.get("title") or handoff.summary or primary_candidate.get("title") or "").strip(),
        "property_url": property_url,
        "listing_url": property_url or str(primary_candidate.get("listing_url") or "").strip(),
        "review_url": f"/app/handoffs/{handoff_ref}",
        "tour_url": tour_url,
        "generated_reconstruction_url": str(
            input_json.get("generated_reconstruction_url")
            or primary_candidate.get("generated_reconstruction_url")
            or _propertyquarry_handoff_generated_reconstruction_url(
                handoff=handoff,
                input_json=input_json,
                primary_candidate=primary_candidate,
                principal_id=principal_id,
            )
            or ""
        ).strip(),
        "vendor_tour_url": str(input_json.get("vendor_tour_url") or primary_candidate.get("vendor_tour_url") or "").strip(),
        "tour_status": str(input_json.get("tour_status") or primary_candidate.get("tour_status") or ("ready" if tour_url else "")).strip(),
        "blocked_reason": str(
            input_json.get("blocked_reason")
            or primary_candidate.get("blocked_reason")
            or getattr(handoff, "delivery_reason", "")
            or ""
        ).strip(),
        "flythrough_url": str(input_json.get("flythrough_url") or primary_candidate.get("flythrough_url") or "").strip(),
        "flythrough_status": str(input_json.get("flythrough_status") or primary_candidate.get("flythrough_status") or "").strip(),
        "flythrough_reason": str(input_json.get("flythrough_reason") or primary_candidate.get("flythrough_reason") or "").strip(),
        "source_ref": str(input_json.get("source_ref") or primary_candidate.get("source_ref") or "").strip(),
        "run_id": str(input_json.get("run_id") or primary_candidate.get("run_id") or "").strip(),
        "candidate_ref": str(input_json.get("candidate_ref") or primary_candidate.get("candidate_ref") or "").strip(),
        "property_facts": property_facts,
        "source_virtual_tour_url": str(
            input_json.get("source_virtual_tour_url")
            or primary_candidate.get("source_virtual_tour_url")
            or property_facts.get("source_virtual_tour_url")
            or ""
        ).strip(),
        "floorplan_urls_json": list(
            input_json.get("floorplan_urls_json")
            or primary_candidate.get("floorplan_urls_json")
            or property_facts.get("floorplan_urls_json")
            or []
        ),
        "tour_eta_minutes": input_json.get("tour_eta_minutes") or primary_candidate.get("tour_eta_minutes") or "",
        "tour_requested_at": input_json.get("tour_requested_at") or primary_candidate.get("tour_requested_at") or "",
        "tour_status_updated_at": input_json.get("tour_status_updated_at") or primary_candidate.get("tour_status_updated_at") or "",
        "tour_progress_pct": input_json.get("tour_progress_pct") or primary_candidate.get("tour_progress_pct") or "",
        "flythrough_eta_minutes": input_json.get("flythrough_eta_minutes") or primary_candidate.get("flythrough_eta_minutes") or "",
        "flythrough_requested_at": input_json.get("flythrough_requested_at") or primary_candidate.get("flythrough_requested_at") or "",
        "flythrough_status_updated_at": input_json.get("flythrough_status_updated_at") or primary_candidate.get("flythrough_status_updated_at") or "",
        "flythrough_progress_pct": input_json.get("flythrough_progress_pct") or primary_candidate.get("flythrough_progress_pct") or "",
    }
    media_candidate = _merge_propertyquarry_live_visual_status(
        product=product,
        principal_id=principal_id,
        candidate=media_candidate,
    )
    if _property_hosted_tour_disabled_fallback(media_candidate.get("tour_url")):
        media_candidate["tour_url"] = ""
        if str(media_candidate.get("tour_status") or "").strip().lower() in {
            "",
            "ready",
            "created",
            "existing",
            "completed",
            "published",
        }:
            media_candidate["tour_status"] = "blocked"
    media_payload = dict(_property_tour_media_payload(media_candidate))
    resolved_tour_url = str(media_candidate.get("tour_url") or "").strip()
    ready_tour_href = ""
    if resolved_tour_url:
        if property_tour_hosting._is_branded_public_tour_url(resolved_tour_url):
            ready_tour_href = str(
                property_tour_hosting._hosted_property_tour_verified_open_url(
                    resolved_tour_url,
                    principal_id=principal_id,
                )
                or ""
            ).strip()
        else:
            ready_tour_href = resolved_tour_url
    if ready_tour_href:
        media_payload["primary_href"] = ready_tour_href
        media_payload["primary_label"] = "Open 3D tour"
        media_payload["status_label"] = "3D tour available"
        media_payload["status_detail"] = (
            "3D tour is ready."
            if property_tour_hosting._is_branded_public_tour_url(resolved_tour_url)
            else "3D tour is ready to review."
        )
    elif property_url and str(media_payload.get("primary_href") or "").strip() == f"/app/handoffs/{handoff_ref}":
        media_payload["primary_href"] = property_url
        media_payload["primary_label"] = "Open listing"
    current_handoff_href = f"/app/handoffs/{handoff_ref}"
    if str(media_payload.get("secondary_href") or "").strip() == current_handoff_href:
        media_payload["secondary_href"] = ""
        media_payload["secondary_label"] = ""
    if str(media_payload.get("primary_href") or "").strip() == property_url and property_url:
        media_payload["primary_label"] = "Open listing"
    primary_href = str(media_payload.get("primary_href") or "").strip()
    secondary_href = str(media_payload.get("secondary_href") or "").strip()
    tertiary_href = str(media_payload.get("tertiary_href") or "").strip()
    if secondary_href and secondary_href == primary_href:
        media_payload["secondary_href"] = ""
        media_payload["secondary_label"] = ""
        secondary_href = ""
    if tertiary_href and tertiary_href in {primary_href, secondary_href}:
        media_payload["tertiary_href"] = ""
        media_payload["tertiary_label"] = ""
    media_payload["generated_reconstruction_ready"] = False
    media_payload["generated_reconstruction_href"] = ""
    media_payload["carousel_items"] = _propertyquarry_media_carousel_items(media_candidate)
    tour_request_enabled = _propertyquarry_candidate_visual_request_enabled(media_candidate)
    flythrough_request_enabled = bool(str(media_candidate.get("tour_url") or "").strip()) or tour_request_enabled
    request_actions = _handoff_property_visual_actions(
        candidate=media_candidate,
        media_payload=media_payload,
        tour_request_enabled=tour_request_enabled,
        flythrough_request_enabled=flythrough_request_enabled,
    )
    active_action = next(
        (
            action
            for action in request_actions
            if str(action.get("state") or "").strip().lower() in {
                "pending",
                "queued",
                "rendering",
                "processing",
                "running",
                "in_progress",
                "started",
                "blocked",
                "failed",
                "skipped",
                "not_applicable",
                "unavailable",
            }
        ),
        None,
    )
    media_payload["request_actions"] = request_actions
    media_payload["style_catalog"] = [dict(row) for row in PROPERTY_FURNITURE_STYLE_CATALOG]
    media_payload["request_status_label"] = str(active_action.get("label") or "").strip() if active_action else ""
    media_payload["request_status_detail"] = str(active_action.get("status_detail") or "").strip() if active_action else ""
    media_payload["request_status_eta"] = str(active_action.get("eta_label") or "").strip() if active_action else ""
    media_payload["request_status_progress_pct"] = int(active_action.get("progress_pct") or 0) if active_action else 0
    return media_payload


@router.get("/app/people", response_class=HTMLResponse)
def people_root(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    return app_shell("people", request, container, context)


@router.get("/app/people/{person_id}", response_class=HTMLResponse)
def person_detail(
    person_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    _raise_propertyquarry_object_detail_disabled(request)
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    detail = product.get_person_detail(
        principal_id=context.principal_id,
        person_id=person_id,
        operator_id=str(context.operator_id or "").strip(),
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="person_not_found")
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="people_opened",
        surface=f"people:{person_id}",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    return _render_public_template(
        request,
        "app/people_detail.html",
        **{
            **_console_shell_context(
                request=request,
                page_title=f"PropertyQuarry {detail.profile.display_name}",
                current_nav="people",
                context=context,
                console_title=detail.profile.display_name,
                console_summary="Relationship context, open loops, current drafts, and evidence tied to one person.",
                nav_groups=app_nav_groups_for_brand(request_brand(request)["key"]),
                workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
                cards=[],
                stats=[
                    {"label": "Open loops", "value": str(detail.profile.open_loops_count)},
                    {"label": "Commitments", "value": str(len(detail.commitments))},
                    {"label": "Drafts", "value": str(len(detail.drafts))},
                    {"label": "Evidence", "value": str(len(detail.evidence_refs))},
                ],
            ),
            "person": detail.profile,
            "detail": detail,
        },
    )


@router.get("/app/commitment-items/{commitment_ref:path}", response_class=HTMLResponse)
def commitment_detail(
    commitment_ref: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    _raise_propertyquarry_object_detail_disabled(request)
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    commitment = product.get_commitment(principal_id=context.principal_id, commitment_ref=commitment_ref)
    if commitment is None:
        raise HTTPException(status_code=404, detail="commitment_not_found")
    history = product.get_commitment_history(principal_id=context.principal_id, commitment_ref=commitment_ref, limit=8)
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="commitment_opened",
        surface=f"commitment:{commitment_ref}",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title=f"PropertyQuarry {commitment.statement}",
        current_nav="queue",
        console_title=commitment.statement,
        console_summary="Commitment source, owner, due date, risk, and recent activity.",
        object_kind="Commitment ledger",
        object_title=commitment.statement,
        object_summary=f"{commitment.counterparty or 'Office loop'} · {commitment.status.replace('_', ' ')}",
        object_meta=[
            {"label": "Owner", "value": str(commitment.owner or "office").replace("_", " ").title()},
            {"label": "Counterparty", "value": commitment.counterparty or "Unknown"},
            {"label": "Due", "value": str(commitment.due_at or "")[:10] or "No due date"},
            {"label": "Risk", "value": str(commitment.risk_level or "normal").title()},
        ],
        object_sidebar_title="Commitment status",
        object_sidebar_copy="A commitment stays visible until it is closed, deferred, dropped, or reopened with a reason.",
        object_sidebar_rows=[
            _object_detail_row("Source", str(commitment.source_type or "manual").replace("_", " ").title(), "Source"),
            _object_detail_row("Source ref", commitment.source_ref or "No source ref attached.", "Reference"),
            _object_detail_row("Last activity", str(commitment.last_activity_at or "")[:10] or "Unknown", "Activity"),
            _object_detail_row("Resolution code", commitment.resolution_code or "No resolution code recorded.", "Code"),
            _object_detail_row("Resolution reason", commitment.resolution_reason or "No resolution reason recorded.", "Reason"),
        ],
        object_sections=[
            {
                "eyebrow": "Sources",
                "title": "Linked sources",
                "items": _evidence_detail_rows(commitment.proof_refs),
            },
            {
                "eyebrow": "History",
                "title": "Recent ledger activity",
                "items": [
                    _object_detail_row(
                        str(item.event_type or "history").replace("_", " ").title(),
                        item.detail or "Ledger event recorded.",
                        str(item.created_at or "")[:10] or "Event",
                    )
                    for item in history
                ] or [_object_detail_row("No history yet", "No commitment history rows were recorded.", "History")],
            },
        ],
        object_sidebar_form={
            "action": f"/app/actions/queue/{commitment_ref}/resolve",
            "method": "post",
            "eyebrow": "Lifecycle",
            "title": "Update commitment state",
            "copy": "Use explicit lifecycle states when the promise is waiting on another party, already scheduled, or needs a dated defer instead of a vague open loop.",
            "submit_label": "Update commitment",
            "fields": [
                {"type": "hidden", "name": "return_to", "value": f"/app/commitment-items/{commitment_ref}"},
                {
                    "type": "select",
                    "name": "action",
                    "label": "Action",
                    "options": [
                        {"value": "close", "label": "Close", "selected": str(commitment.status or "").strip().lower() == "completed"},
                        {"value": "defer", "label": "Defer"},
                        {"value": "wait", "label": "Waiting on external", "selected": str(commitment.status or "").strip().lower() == "waiting_on_external"},
                        {"value": "schedule", "label": "Scheduled", "selected": str(commitment.status or "").strip().lower() == "scheduled"},
                        {"value": "drop", "label": "Drop"},
                        {"value": "reopen", "label": "Reopen", "selected": str(commitment.status or "").strip().lower() == "open"},
                    ],
                },
                {
                    "type": "text",
                    "name": "reason_code",
                    "label": "Reason code",
                    "value": commitment.resolution_code or "",
                    "placeholder": "scheduled, waiting_on_external, deferred",
                },
                {
                    "type": "text",
                    "name": "due_at",
                    "label": "Due at",
                    "value": commitment.due_at or "",
                    "placeholder": "YYYY-MM-DDTHH:MM:SS+00:00",
                },
                {
                    "type": "textarea",
                    "name": "reason",
                    "label": "Reason",
                    "value": commitment.resolution_reason or "",
                    "placeholder": "Why the commitment moved into this lifecycle state.",
                },
            ],
        },
    )


@router.get("/app/decisions/{decision_ref}", response_class=HTMLResponse)
def decision_detail(
    decision_ref: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    _raise_propertyquarry_object_detail_disabled(request)
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    decision = product.get_decision(principal_id=context.principal_id, decision_ref=decision_ref)
    if decision is None:
        raise HTTPException(status_code=404, detail="decision_not_found")
    history = product.get_decision_history(principal_id=context.principal_id, decision_ref=decision_ref, limit=8)
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="decision_opened",
        surface=f"decision:{decision_ref}",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title=f"PropertyQuarry {decision.title}",
        current_nav="queue",
        console_title=decision.title,
        console_summary="Decision context, ownership, deadline pressure, and linked sources.",
        object_kind="Decision queue",
        object_title=decision.title,
        object_summary=decision.summary or "This decision is open in the office loop.",
        object_meta=[
            {"label": "Priority", "value": str(decision.priority or "normal").title()},
            {"label": "Type", "value": str(decision.decision_type or "office_decision").replace("_", " ").title()},
            {"label": "Owner", "value": str(decision.owner_role or "office").replace("_", " ").title()},
            {"label": "Deadline", "value": str(decision.due_at or "")[:10] or "No due date"},
            {"label": "Status", "value": str(decision.status or "open").replace("_", " ").title()},
            {"label": "Schedule", "value": str(decision.sla_status or "unscheduled").replace("_", " ").title()},
        ],
        object_sidebar_title="Current state",
        object_sidebar_copy="Owner, timing, next step, and linked notes stay in one place.",
        object_sidebar_rows=[
            _object_detail_row("Recommendation", decision.recommendation or "No recommendation projected yet.", "Recommend"),
            _object_detail_row("Next action", decision.next_action or "No next action projected yet.", "Next"),
            _object_detail_row("Impact", decision.impact_summary or "Impact has not been projected yet.", "Impact"),
            _object_detail_row("Rationale", decision.rationale or "No rationale projected yet.", "Why"),
            _object_detail_row("Sources attached", f"{len(decision.evidence_refs or [])} linked sources attached to this decision.", "Sources"),
            _object_detail_row("Schedule", str(decision.sla_status or "unscheduled").replace("_", " ").title(), "Timing"),
            _object_detail_row("Timing", decision.summary or "This decision is still active.", "Priority"),
        ],
        object_sections=[
            {
                "eyebrow": "Summary",
                "title": "Next step",
                "items": [
                    _object_detail_row(
                        decision.title,
                        decision.recommendation or decision.summary or "Review this decision with its current evidence and owner context.",
                        str(decision.priority or "normal").title(),
                    ),
                    _object_detail_row("Options", ", ".join(decision.options or ()) or "No explicit options projected.", "Options"),
                    _object_detail_row("Next action", decision.next_action or "No next action projected yet.", "Next"),
                    _object_detail_row("Impact", decision.impact_summary or "No projected downstream impact yet.", "Impact"),
                    _object_detail_row("Related commitments", ", ".join(decision.related_commitment_ids or ()) or "No linked commitments.", "Commitment"),
                    _object_detail_row("Related threads", ", ".join(decision.linked_thread_ids or ()) or "No linked threads.", "Thread"),
                    _object_detail_row("Related people", ", ".join(decision.related_people or ()) or "No linked people.", "People"),
                    _object_detail_row("Resolution note", decision.resolution_reason or "No explicit resolution note yet.", "Resolution"),
                ],
            },
            {
                "eyebrow": "Sources",
                "title": "Linked sources",
                "items": _evidence_detail_rows(decision.evidence_refs),
            },
            {
                "eyebrow": "Decision history",
                "title": "Recent decision history",
                "items": [
                    _object_detail_row(
                        str(item.event_type or "history").replace("_", " ").title(),
                        " · ".join(
                            part
                            for part in (
                                str(item.actor or "").strip(),
                                str(item.detail or "").strip(),
                            )
                            if part
                        )
                        or "Decision event recorded.",
                        str(item.created_at or "")[:10] or "History",
                    )
                    for item in history
                ] or [_object_detail_row("No decision history yet", "No decision events were recorded yet.", "History")],
            },
        ],
        object_sidebar_form={
            "action": f"/app/actions/queue/{decision_ref}/resolve",
            "method": "post",
            "eyebrow": "Decision lifecycle",
            "title": "Update decision state",
            "copy": "Resolve the choice, escalate it to the principal, or reopen it without leaving the decision detail surface.",
            "submit_label": "Update decision",
            "fields": [
                {"type": "hidden", "name": "return_to", "value": f"/app/decisions/{decision_ref}"},
                {
                    "type": "select",
                    "name": "action",
                    "label": "Action",
                    "options": [
                        {"value": "resolve", "label": "Resolve", "selected": str(decision.status or "").strip().lower() != "decided"},
                        {"value": "escalate", "label": "Escalate to principal"},
                        {"value": "reopen", "label": "Reopen", "selected": str(decision.status or "").strip().lower() == "decided"},
                    ],
                },
                {
                    "type": "text",
                    "name": "due_at",
                    "label": "Decision deadline",
                    "value": decision.due_at or "",
                    "placeholder": "YYYY-MM-DDTHH:MM:SS+00:00",
                },
                {
                    "type": "textarea",
                    "name": "reason",
                    "label": "Reason",
                    "value": decision.resolution_reason or "",
                    "placeholder": "Why this decision was resolved, escalated, or reopened.",
                },
            ],
        },
    )


@router.get("/app/deadlines/{deadline_ref:path}", response_class=HTMLResponse)
def deadline_detail(
    deadline_ref: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    _raise_propertyquarry_object_detail_disabled(request)
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    deadline = product.get_deadline(principal_id=context.principal_id, deadline_ref=deadline_ref)
    if deadline is None:
        raise HTTPException(status_code=404, detail="deadline_not_found")
    history = product.get_deadline_history(principal_id=context.principal_id, deadline_ref=deadline_ref, limit=8)
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="deadline_opened",
        surface=f"deadline:{deadline_ref}",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title=f"PropertyQuarry {deadline.title}",
        current_nav="queue",
        console_title=deadline.title,
        console_summary="Deadline window timing, status, and recent queue movement.",
        object_kind="Deadline window",
        object_title=deadline.title,
        object_summary=deadline.summary or "Deadline window is active in the office loop.",
        object_meta=[
            {"label": "Priority", "value": str(deadline.priority or "normal").title()},
            {"label": "Start", "value": str(deadline.start_at or "")[:10] or "No start time"},
            {"label": "End", "value": str(deadline.end_at or "")[:10] or "No end time"},
            {"label": "Status", "value": str(deadline.status or "open").replace("_", " ").title()},
        ],
        object_sidebar_title="Deadline pressure",
        object_sidebar_copy="Deadline windows stay explorable and resolvable instead of hiding as generic queue pressure.",
        object_sidebar_rows=[
            _object_detail_row("Current note", deadline.summary or "No deadline note recorded.", "Note"),
            _object_detail_row("Priority", str(deadline.priority or "normal").title(), "Priority"),
            _object_detail_row("Window start", str(deadline.start_at or "")[:10] or "No start time recorded.", "Start"),
            _object_detail_row("Window end", str(deadline.end_at or "")[:10] or "No end time recorded.", "End"),
            _object_detail_row("Status", str(deadline.status or "open").replace("_", " ").title(), "Status"),
        ],
        object_sections=[
            {
                "eyebrow": "Window",
                "title": "Current deadline",
                "items": [
                    _object_detail_row(deadline.title, deadline.summary or "Deadline window is active in the office loop.", str(deadline.priority or "normal").title()),
                    _object_detail_row("Start", str(deadline.start_at or "")[:19] or "No start time recorded.", "Start"),
                    _object_detail_row("End", str(deadline.end_at or "")[:19] or "No end time recorded.", "End"),
                    _object_detail_row("Status", str(deadline.status or "open").replace("_", " ").title(), "Status"),
                ],
            },
            {
                "eyebrow": "History",
                "title": "Recent deadline history",
                "items": [
                    _object_detail_row(
                        str(item.event_type or "history").replace("_", " ").title(),
                        " · ".join(
                            part
                            for part in (
                                str(item.actor or "").strip(),
                                str(item.detail or "").strip(),
                            )
                            if part
                        )
                        or "Deadline event recorded.",
                        str(item.created_at or "")[:10] or "History",
                    )
                    for item in history
                ] or [_object_detail_row("No deadline history yet", "No deadline events were recorded yet.", "History")],
            },
        ],
        object_sidebar_form={
            "action": f"/app/actions/queue/{deadline_ref}/resolve",
            "method": "post",
            "eyebrow": "Deadline lifecycle",
            "title": "Update deadline state",
            "copy": "Resolve the current window or reopen it with a new end time without leaving the deadline detail surface.",
            "submit_label": "Update deadline",
            "fields": [
                {"type": "hidden", "name": "return_to", "value": f"/app/deadlines/{deadline_ref}"},
                {
                    "type": "select",
                    "name": "action",
                    "label": "Action",
                    "options": [
                        {"value": "resolve", "label": "Resolve", "selected": str(deadline.status or "").strip().lower() != "elapsed"},
                        {"value": "reopen", "label": "Reopen", "selected": str(deadline.status or "").strip().lower() == "elapsed"},
                    ],
                },
                {
                    "type": "text",
                    "name": "due_at",
                    "label": "Window end",
                    "value": deadline.end_at or "",
                    "placeholder": "YYYY-MM-DDTHH:MM:SS+00:00",
                },
                {
                    "type": "textarea",
                    "name": "reason",
                    "label": "Reason",
                    "value": deadline.summary or "",
                    "placeholder": "Why this deadline window was resolved or reopened.",
                },
            ],
        },
    )


@router.get("/app/handoffs/{handoff_ref:path}", response_class=HTMLResponse)
def handoff_detail(
    handoff_ref: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    handoff = product.get_handoff(principal_id=context.principal_id, handoff_ref=handoff_ref)
    if handoff is None:
        raise HTTPException(status_code=404, detail="handoff_not_found")
    if _is_propertyquarry_request(request) and not _propertyquarry_handoff_task_allowed(str(handoff.task_type or "")):
        raise HTTPException(status_code=404, detail="propertyquarry_object_detail_not_available")
    task_id = handoff.id.split(":", 1)[1] if handoff.id.startswith("human_task:") else handoff.id
    history_rows = container.orchestrator.list_human_task_assignment_history(task_id, principal_id=context.principal_id, limit=8)
    send_error = str(request.query_params.get("send_error") or "").strip()
    send_status = str(request.query_params.get("send_status") or "").strip()
    delivery_followup_open = (
        str(handoff.task_type or "").strip() == "delivery_followup"
        and str(handoff.status or "").strip() in {"pending", "claimed"}
        and str(handoff.resolution or "").strip() != "sent"
    )
    property_tour_followup_open = (
        str(handoff.task_type or "").strip() == "property_tour_followup"
        and str(handoff.status or "").strip() in {"open", "pending", "claimed"}
    )
    if send_error:
        retry_detail = send_error
    elif send_status == "sent" or str(handoff.resolution or "").strip() == "sent":
        retry_detail = "Retry send completed."
    elif delivery_followup_open:
        retry_detail = "Try the stored approved draft again after reconnecting Google."
    else:
        retry_detail = "Retry send is no longer needed for this handoff."
    google_delivery_action = (
        _google_delivery_action(str(handoff.delivery_reason or ""), return_to=f"/app/handoffs/{handoff_ref}")
        if delivery_followup_open and str(handoff.delivery_reason or "").strip().startswith("google_")
        else {}
    )
    manual_resolution_secondary_value = "reauth_needed" if str(handoff.delivery_reason or "").strip().startswith("google_") else "failed"
    manual_resolution_secondary_label = (
        "Needs reauth" if manual_resolution_secondary_value == "reauth_needed" else "Unable to send"
    )
    resolved_manual_detail = {
        "sent": "Manual send was recorded for this handoff.",
        "reauth_needed": "Google access still needs reauth before this handoff can proceed.",
        "failed": "This handoff was marked unable to send.",
        "waiting_on_principal": "Waiting on principal input before delivery can continue.",
    }.get(str(handoff.resolution or "").strip(), "")
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="handoff_opened",
        surface=f"handoff:{handoff_ref}",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    task = container.orchestrator.fetch_human_task(task_id, principal_id=context.principal_id)
    if str(handoff.task_type or "").strip() == "property_alert_review" and task is not None:
        input_json = dict(getattr(task, "input_json", {}) or {})
        assessment = dict(input_json.get("personal_fit_assessment") or {})
        candidate_properties = [
            dict(item)
            for item in list(input_json.get("candidate_properties") or [])
            if isinstance(item, dict)
        ]
        primary_candidate = dict(candidate_properties[0]) if candidate_properties else {}
        property_facts = (
            dict(input_json.get("property_facts_json") or {})
            if isinstance(input_json.get("property_facts_json"), dict)
            else {}
        )
        if isinstance(primary_candidate.get("property_facts"), dict):
            property_facts = {**dict(primary_candidate.get("property_facts") or {}), **property_facts}
        review_path = f"/app/handoffs/{handoff_ref}"
        primary_tour_url = _propertyquarry_handoff_tour_url(
            handoff=handoff,
            input_json=input_json,
            primary_candidate=primary_candidate,
            principal_id=context.principal_id,
        )
        property_url = _propertyquarry_handoff_property_url(
            handoff=handoff,
            input_json=input_json,
            primary_candidate=primary_candidate,
        )
        media_candidate = {
            **primary_candidate,
            "title": str(input_json.get("title") or handoff.summary or primary_candidate.get("title") or "").strip(),
            "review_url": review_path,
            "tour_url": primary_tour_url,
            "vendor_tour_url": str(input_json.get("vendor_tour_url") or primary_candidate.get("vendor_tour_url") or "").strip(),
            "tour_status": str(input_json.get("tour_status") or primary_candidate.get("tour_status") or "").strip(),
            "blocked_reason": str(input_json.get("blocked_reason") or primary_candidate.get("blocked_reason") or "").strip(),
        }
        match_reasons = [str(item).strip() for item in list(assessment.get("match_reasons_json") or []) if str(item).strip()]
        mismatch_reasons = [str(item).strip() for item in list(assessment.get("mismatch_reasons_json") or []) if str(item).strip()]
        unknowns = [str(item).strip() for item in list(assessment.get("unknowns_json") or []) if str(item).strip()]
        review_page_neuronwriter = (
            dict(input_json.get("review_page_neuronwriter") or {})
            if isinstance(input_json.get("review_page_neuronwriter"), dict)
            else {}
        )
        review_questions = [
            str(item).strip()
            for item in list(review_page_neuronwriter.get("questions") or review_page_neuronwriter.get("headings") or [])
            if str(item).strip()
        ]
        next_review_question = (
            review_questions[0]
            if review_questions
            else "Confirm the missing facts before this property moves forward."
        )
        clean_title = _propertyquarry_clean_listing_copy(
            primary_candidate.get("listing_title")
            or input_json.get("title")
            or handoff.summary
            or "Property review"
        ) or "Property review"
        clean_summary = summarize_property_description_copy(
            input_json.get("summary")
            or primary_candidate.get("summary")
            or handoff.summary
            or ""
        )
        object_summary = _propertyquarry_summary_line(property_facts)
        object_meta = _propertyquarry_meta_rows(property_facts)
        why_consider_detail = match_reasons[0] if match_reasons else (
            clean_summary or "This home lines up well enough with your search for a closer look."
        )
        check_details: list[str] = []
        for detail in (mismatch_reasons[:1] + unknowns[:1]):
            if detail and detail not in check_details:
                check_details.append(detail)
        if not check_details and next_review_question:
            check_details.append(next_review_question)
        next_step_detail = (
            "Open the 3D tour or the listing, then decide whether this deserves a viewing."
            if primary_tour_url
            else "Open the listing and photos, then decide whether this deserves a viewing."
        )
        ooda_rows = [
            _object_detail_row("Why it works", why_consider_detail, ""),
            _object_detail_row(
                "Check before deciding",
                " ".join(check_details) if check_details else "No clear blocker is standing out yet.",
                "",
            ),
            _object_detail_row(
                "Next step",
                next_step_detail,
                "",
                href=property_url or review_path,
                secondary_action_href=primary_tour_url or "",
                secondary_action_label="Open 3D tour" if primary_tour_url else "",
                secondary_action_method="get" if primary_tour_url else "",
            ),
        ]
        ooda_rows.extend(_property_distance_ooda_rows(property_facts))
        return _render_console_object_detail(
            request=request,
            context=context,
            workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
            page_title=f"PropertyQuarry {clean_title}",
            current_nav="research",
            console_title=clean_title,
            console_summary="",
            object_kind="Home",
            object_title=clean_title,
            object_summary=object_summary or (clean_summary if clean_summary != clean_title else ""),
            object_media=_propertyquarry_handoff_media_payload(
                handoff_ref=handoff_ref,
                handoff=handoff,
                input_json=input_json,
                primary_candidate=primary_candidate,
                product=product,
                principal_id=context.principal_id,
            ),
            object_meta=object_meta,
            object_ooda_kicker="Quick read",
            object_ooda_title="Why choose it",
            object_ooda_copy="",
            object_ooda_rows=ooda_rows,
            object_sidebar_title="",
            object_sidebar_copy="",
            object_sidebar_rows=[],
            object_sections=[],
        )
    if not str(context.operator_id or "").strip():
        input_json = dict(getattr(task, "input_json", {}) or {}) if task is not None else {}
        customer_status_label, customer_status_detail = _handoff_customer_status(
            handoff=handoff,
            delivery_followup_open=delivery_followup_open,
            property_tour_followup_open=property_tour_followup_open,
            retry_detail=retry_detail,
        )
        tour_url = str(handoff.tour_url or input_json.get("tour_url") or "").strip()
        vendor_tour_url = _customer_facing_vendor_tour_url(input_json.get("vendor_tour_url"))
        property_url = str(handoff.property_url or input_json.get("property_url") or "").strip()
        media_candidate = {
            "title": handoff.summary,
            "tour_url": tour_url,
            "vendor_tour_url": vendor_tour_url,
            "property_url": property_url,
            "review_url": f"/app/handoffs/{handoff_ref}",
            "tour_status": str(input_json.get("tour_status") or ("ready" if tour_url else ("blocked" if property_tour_followup_open else ""))).strip(),
            "blocked_reason": str(input_json.get("blocked_reason") or handoff.delivery_reason or "").strip(),
            "flythrough_url": str(input_json.get("flythrough_url") or "").strip(),
            "flythrough_status": str(input_json.get("flythrough_status") or "").strip(),
            "flythrough_reason": str(input_json.get("flythrough_reason") or "").strip(),
            "source_ref": str(input_json.get("source_ref") or "").strip(),
            "run_id": str(input_json.get("run_id") or "").strip(),
            "candidate_ref": str(input_json.get("candidate_ref") or "").strip(),
            "tour_eta_minutes": input_json.get("tour_eta_minutes") or "",
            "tour_requested_at": input_json.get("tour_requested_at") or "",
            "tour_status_updated_at": input_json.get("tour_status_updated_at") or "",
            "tour_progress_pct": input_json.get("tour_progress_pct") or "",
            "flythrough_eta_minutes": input_json.get("flythrough_eta_minutes") or "",
            "flythrough_requested_at": input_json.get("flythrough_requested_at") or "",
            "flythrough_status_updated_at": input_json.get("flythrough_status_updated_at") or "",
            "flythrough_progress_pct": input_json.get("flythrough_progress_pct") or "",
        }
        property_facts = (
            dict(input_json.get("property_facts_json") or {})
            if isinstance(input_json.get("property_facts_json"), dict)
            else {}
        )
        if isinstance(media_candidate.get("property_facts"), dict):
            property_facts = {**dict(media_candidate.get("property_facts") or {}), **property_facts}
        if str(handoff.task_type or "").strip() == "property_tour_followup" and property_url:
            clean_title = _propertyquarry_clean_listing_copy(input_json.get("title") or handoff.summary or "Property") or "Property"
            media_payload = _propertyquarry_handoff_media_payload(
                handoff_ref=handoff_ref,
                handoff=handoff,
                input_json=input_json,
                primary_candidate=media_candidate,
                product=product,
                principal_id=context.principal_id,
            )
            object_summary = _propertyquarry_summary_line(property_facts) or customer_status_label
            object_meta = _propertyquarry_meta_rows(property_facts)
            request_kind = str(input_json.get("request_kind") or "").strip().lower()
            request_label = "Walkthrough" if request_kind == "flythrough" else "3D tour"
            visual_status_detail = customer_status_detail
            if not visual_status_detail:
                visual_status_detail = (
                    str(media_payload.get("request_status_detail") or "").strip()
                    or str(media_payload.get("status_detail") or "").strip()
                )
            next_step_detail = (
                "Open the 3D tour and decide whether this home deserves a viewing."
                if tour_url
                else "Open the listing while the visual is being prepared."
            )
            ooda_rows = [
                _object_detail_row(
                    request_label,
                    visual_status_detail or customer_status_detail,
                    "",
                ),
                _object_detail_row(
                    "Next",
                    next_step_detail,
                    "",
                    href=tour_url or property_url or f"/app/handoffs/{handoff_ref}",
                    secondary_action_href=property_url if tour_url and property_url else "",
                    secondary_action_label="Open listing" if tour_url and property_url else "",
                    secondary_action_method="get" if tour_url and property_url else "",
                ),
            ]
            ooda_rows.extend(_property_distance_ooda_rows(property_facts))
            return _render_console_object_detail(
                request=request,
                context=context,
                workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
                page_title=f"PropertyQuarry {clean_title}",
                current_nav="research",
                console_title=clean_title,
                console_summary="",
                object_kind="Home",
                object_title=clean_title,
                object_summary=object_summary,
                object_media=media_payload,
                object_meta=object_meta,
                object_ooda_kicker="Quick read",
                object_ooda_title="What stands out",
                object_ooda_copy="",
                object_ooda_rows=ooda_rows,
                object_sidebar_title="",
                object_sidebar_copy="",
                object_sidebar_rows=[],
                object_sections=[],
            )
        next_step_detail = customer_status_detail
        next_step_label = "Next"
        primary_action_href = ""
        primary_action_label = ""
        secondary_action_href = ""
        secondary_action_label = ""
        tertiary_action_href = ""
        tertiary_action_label = ""
        if delivery_followup_open:
            primary_action_href = f"/app/actions/handoffs/{handoff_ref}/retry-send"
            primary_action_label = "Try again"
            secondary_action_href = str(google_delivery_action.get("href") or "")
            secondary_action_label = str(google_delivery_action.get("label") or "")
            tertiary_action_href = f"/app/actions/handoffs/{handoff_ref}/complete"
            tertiary_action_label = "Mark sent"
        elif property_tour_followup_open:
            primary_action_href = f"/app/actions/handoffs/{handoff_ref}/recreate"
            primary_action_label = "Request 3D tour"
        elif tour_url:
            primary_action_href = tour_url
            primary_action_label = "Open 3D tour"
        elif property_url:
            primary_action_href = property_url
            primary_action_label = "Open listing"
        return _render_console_object_detail(
            request=request,
            context=context,
            workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
            page_title=f"PropertyQuarry {handoff.summary}",
            current_nav="queue",
            console_title=handoff.summary,
            console_summary="",
            object_kind="Follow-up",
            object_title=handoff.summary,
            object_summary=customer_status_label,
            object_media=_propertyquarry_handoff_media_payload(
                handoff_ref=handoff_ref,
                handoff=handoff,
                input_json=input_json,
                primary_candidate=media_candidate,
                product=product,
                principal_id=context.principal_id,
            ) if property_url else {},
            object_meta=[
                {"label": "Status", "value": customer_status_label},
                {"label": "Type", "value": str(handoff.task_type or "follow_up").replace("_", " ").title()},
                {"label": "Due", "value": str(handoff.due_time or "")[:10] or "No due date"},
            ],
            object_sidebar_title="Open now",
            object_sidebar_copy="",
            object_sidebar_rows=[
                _object_detail_row("Status", customer_status_detail, "Status"),
                _object_detail_row(
                    next_step_label,
                    next_step_detail,
                    "Action",
                    href=primary_action_href,
                    action_href=primary_action_href if primary_action_href.startswith("/app/actions/") else "",
                    action_label=primary_action_label if primary_action_href.startswith("/app/actions/") else "",
                    action_method="post" if primary_action_href.startswith("/app/actions/") else "",
                    return_to=f"/app/handoffs/{handoff_ref}" if primary_action_href.startswith("/app/actions/") else "",
                    secondary_action_href=secondary_action_href,
                    secondary_action_label=secondary_action_label,
                    secondary_action_method="get" if secondary_action_href else "",
                    secondary_return_to=f"/app/handoffs/{handoff_ref}" if secondary_action_href else "",
                    tertiary_action_href=tertiary_action_href,
                    tertiary_action_label=tertiary_action_label,
                    tertiary_action_value="sent" if tertiary_action_href else "",
                    tertiary_action_method="post" if tertiary_action_href else "",
                    tertiary_return_to=f"/app/handoffs/{handoff_ref}" if tertiary_action_href else "",
                ),
                *(
                    [
                        _object_detail_row(
                            "Property tour",
                            tour_url or "The 3D tour is not ready yet.",
                            "Tour",
                            href=tour_url or vendor_tour_url,
                            secondary_action_href=vendor_tour_url if tour_url and vendor_tour_url and vendor_tour_url != tour_url else "",
                            secondary_action_label="Open original tour" if tour_url and vendor_tour_url and vendor_tour_url != tour_url else "",
                            secondary_action_method="get" if tour_url and vendor_tour_url and vendor_tour_url != tour_url else "",
                        )
                    ]
                    if (tour_url or vendor_tour_url or property_tour_followup_open)
                    else []
                ),
                *(
                    [
                        _object_detail_row(
                            "Listing",
                            property_url,
                            "Listing",
                            href=property_url,
                            secondary_action_href=property_url,
                            secondary_action_label="Open listing",
                            secondary_action_method="get",
                        )
                    ]
                    if property_url
                    else []
                ),
            ],
            object_sections=[],
        )
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title=f"PropertyQuarry {handoff.summary}",
        current_nav="settings",
        console_title=handoff.summary,
        console_summary="Assignment state, escalation pressure, linked sources, and recent routing history.",
        object_kind="Handoffs",
        object_title=handoff.summary,
        object_summary=f"{handoff.owner or 'Office'} · {handoff.status.replace('_', ' ')}",
        object_meta=[
            {"label": "Owner", "value": handoff.owner or "Unassigned"},
            {"label": "Due", "value": str(handoff.due_time or "")[:10] or "No due date"},
            {"label": "Task", "value": str(handoff.task_type or "handoff").replace("_", " ").title()},
            {"label": "Escalation", "value": str(handoff.escalation_status or "normal").title()},
            {"label": "Status", "value": str(handoff.status or "pending").replace("_", " ").title()},
        ],
        object_sidebar_title="Current state",
        object_sidebar_copy="This page shows who owns it, what is blocked, and what happens next.",
        object_sidebar_rows=[
            _object_detail_row("Queue item", handoff.queue_item_ref or "No queue item ref attached.", "Queue"),
            _object_detail_row("Draft ref", handoff.draft_ref or "No draft is attached to this handoff.", "Draft"),
            _object_detail_row("Recipient", handoff.recipient_email or "No recipient metadata attached.", "Recipient"),
            _object_detail_row("Delivery reason", handoff.delivery_reason or "No delivery blocker is attached.", "Reason"),
            _object_detail_row(
                "Retry send in EA",
                retry_detail,
                "Action",
                href=f"/app/handoffs/{handoff_ref}",
                action_href=f"/app/actions/handoffs/{handoff_ref}/retry-send" if delivery_followup_open else "",
                action_label="Retry send" if delivery_followup_open else "",
                action_method="post" if delivery_followup_open else "",
                return_to=f"/app/handoffs/{handoff_ref}" if delivery_followup_open else "",
            ),
            _object_detail_row(
                str(google_delivery_action.get("label") or "Google delivery action"),
                "Repair Google access for this workspace before retrying delivery."
                if google_delivery_action
                else "No Google reconnect is required for this handoff.",
                "Google",
                href="/app/settings/google" if google_delivery_action else "",
                action_href=str(google_delivery_action.get("href") or ""),
                action_label=str(google_delivery_action.get("label") or ""),
                action_method=str(google_delivery_action.get("method") or ""),
                return_to=f"/app/handoffs/{handoff_ref}" if google_delivery_action else "",
            ),
            _object_detail_row(
                "Manual resolution",
                resolved_manual_detail
                or "Record the real delivery outcome when a person finished the send outside EA or needs to keep the blocker visible.",
                "Resolution",
                href=f"/app/handoffs/{handoff_ref}",
                action_href=f"/app/actions/handoffs/{handoff_ref}/complete" if delivery_followup_open else "",
                action_label="Mark sent" if delivery_followup_open else "",
                action_value="sent" if delivery_followup_open else "",
                action_method="post" if delivery_followup_open else "",
                return_to=f"/app/handoffs/{handoff_ref}" if delivery_followup_open else "",
                secondary_action_href=f"/app/actions/handoffs/{handoff_ref}/complete" if delivery_followup_open else "",
                secondary_action_label=manual_resolution_secondary_label if delivery_followup_open else "",
                secondary_action_value=manual_resolution_secondary_value if delivery_followup_open else "",
                secondary_action_method="post" if delivery_followup_open else "",
                secondary_return_to=f"/app/handoffs/{handoff_ref}" if delivery_followup_open else "",
                tertiary_action_href=f"/app/actions/handoffs/{handoff_ref}/complete" if delivery_followup_open else "",
                tertiary_action_label="Waiting on principal" if delivery_followup_open else "",
                tertiary_action_value="waiting_on_principal" if delivery_followup_open else "",
                tertiary_action_method="post" if delivery_followup_open else "",
                tertiary_return_to=f"/app/handoffs/{handoff_ref}" if delivery_followup_open else "",
            ),
            _object_detail_row(
                "Property tour followup",
                "Re-run tour generation and retry delivery after you fix the blocker."
                if property_tour_followup_open
                else "Tour actions are available only for property-tour followups.",
                "Tour",
                href=handoff.tour_url or "",
                action_href=f"/app/actions/handoffs/{handoff_ref}/recreate" if property_tour_followup_open else "",
                action_label="Recreate tour" if property_tour_followup_open else "",
                action_method="post" if property_tour_followup_open else "",
                return_to=f"/app/handoffs/{handoff_ref}" if property_tour_followup_open else "",
            ),
            _object_detail_row("Sources attached", f"{len(handoff.evidence_refs or [])} linked sources attached to this follow-up.", "Sources"),
            _object_detail_row("Assignment state", str(handoff.status or "pending").replace("_", " "), "Status"),
        ],
        object_sections=[
            {
                "eyebrow": "Sources",
                "title": "Linked sources",
                "items": _evidence_detail_rows(handoff.evidence_refs),
            },
            {
                "eyebrow": "Routing history",
                "title": "Recent assignment events",
                "items": [
                    _object_detail_row(
                        str(getattr(item, "event_name", "") or "assignment").replace("_", " ").title(),
                        " · ".join(
                            part
                            for part in (
                                str(getattr(item, "assigned_operator_id", "") or "").strip(),
                                str(getattr(item, "assigned_by_actor_id", "") or "").strip(),
                                str(getattr(item, "assignment_source", "") or "").strip(),
                            )
                            if part
                        ) or "Assignment event recorded.",
                        str(getattr(item, "created_at", "") or "")[:10] or "Event",
                    )
                    for item in history_rows
                ] or [_object_detail_row("No routing history yet", "No assignment changes were recorded yet.", "History")],
            },
        ],
    )


@router.get("/app/threads/{thread_ref}", response_class=HTMLResponse)
def thread_detail(
    thread_ref: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    thread = product.get_thread(principal_id=context.principal_id, thread_ref=thread_ref)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread_not_found")
    history = product.get_thread_history(principal_id=context.principal_id, thread_ref=thread_ref, limit=8)
    send_error = str(request.query_params.get("send_error") or "").strip()
    send_status = str(request.query_params.get("send_status") or "").strip()
    linked_handoff_ref = next(
        (str(ref.ref_id or "").strip() for ref in thread.evidence_refs if str(ref.ref_id or "").strip().startswith("human_task:")),
        "",
    )
    linked_handoff = (
        product.get_handoff(principal_id=context.principal_id, handoff_ref=linked_handoff_ref)
        if linked_handoff_ref
        else None
    )
    if _is_propertyquarry_request(request) and not (
        linked_handoff is not None and _propertyquarry_handoff_task_allowed(str(linked_handoff.task_type or ""))
    ):
        raise HTTPException(status_code=404, detail="propertyquarry_object_detail_not_available")
    delivery_followup_open = (
        linked_handoff is not None
        and str(linked_handoff.task_type or "").strip() == "delivery_followup"
        and str(linked_handoff.status or "").strip() in {"pending", "claimed"}
        and str(linked_handoff.resolution or "").strip() != "sent"
    )
    resume_followup_available = (
        not delivery_followup_open
        and str(thread.status or "").strip() in {"waiting_on_principal", "reauth_needed", "delivery_failed"}
    )
    property_tour_followup_open = (
        linked_handoff is not None
        and str(linked_handoff.task_type or "").strip() == "property_tour_followup"
        and str(linked_handoff.status or "").strip() in {"open", "pending", "claimed"}
    )
    delivery_reason = (
        str(linked_handoff.delivery_reason or "").strip()
        if linked_handoff is not None
        else str(thread.summary or "").strip()
    )
    thread_google_action = (
        _google_delivery_action(delivery_reason, return_to=f"/app/threads/{thread_ref}")
        if (delivery_followup_open or str(thread.status or "").strip() in {"delivery_followup", "reauth_needed"})
        and delivery_reason.startswith("google_")
        else {}
    )
    if send_error:
        retry_detail = send_error
    elif send_status == "sent" or str(thread.status or "").strip() == "sent" or str(getattr(linked_handoff, "resolution", "") or "").strip() == "sent":
        retry_detail = "Retry send completed."
    elif send_status == "resumed":
        retry_detail = "Delivery handoff was reopened for this thread."
    elif delivery_followup_open:
        retry_detail = "Try the stored approved draft again after reconnecting Google."
    elif resume_followup_available:
        retry_detail = "Resume the blocked delivery handoff so it can be retried or closed from the thread context."
    else:
        retry_detail = "Retry send is no longer needed for this thread."
    manual_resolution_secondary_value = "reauth_needed" if delivery_reason.startswith("google_") else "failed"
    manual_resolution_secondary_label = (
        "Needs reauth" if manual_resolution_secondary_value == "reauth_needed" else "Unable to send"
    )
    resolved_manual_detail = {
        "sent": "Manual send was recorded for this thread.",
        "reauth_needed": "Google access still needs reauth before this thread can proceed.",
        "failed": "This thread was marked unable to send.",
        "waiting_on_principal": "Waiting on principal input before delivery can continue.",
    }.get(str(getattr(linked_handoff, "resolution", "") or thread.status or "").strip(), "")
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="thread_opened",
        surface=f"thread:{thread_ref}",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title=f"PropertyQuarry {thread.title}",
        current_nav="queue",
        console_title=thread.title,
        console_summary="Conversation state, related drafts, linked commitments, and decision context.",
        object_kind="Conversation thread",
        object_title=thread.title,
        object_summary=thread.summary or "This thread is part of the current office loop.",
        object_meta=[
            {"label": "Channel", "value": str(thread.channel or "unknown").title()},
            {"label": "Status", "value": str(thread.status or "open").replace("_", " ").title()},
            {"label": "Last activity", "value": str(thread.last_activity_at or "")[:10] or "Unknown"},
            {"label": "People", "value": str(len(thread.counterparties or []))},
        ],
        object_sidebar_title="Thread context",
        object_sidebar_copy="A conversation stays connected to the work it creates: drafts, commitments, decisions, and evidence.",
        object_sidebar_rows=[
            _object_detail_row("Counterparties", " · ".join(thread.counterparties or []) or "No counterparties projected.", "People"),
            _object_detail_row("Drafts", ", ".join(thread.draft_ids or []) or "No active draft ids.", "Drafts"),
            _object_detail_row("Commitments", ", ".join(thread.related_commitment_ids or []) or "No linked commitments yet.", "Ledger"),
            _object_detail_row(
                "Delivery handoff",
                linked_handoff.summary if linked_handoff is not None else "No linked delivery handoff is attached to this thread.",
                "Handoff",
                href=f"/app/handoffs/{linked_handoff_ref}" if linked_handoff_ref else "",
            ),
            _object_detail_row(
                "Property tour followup",
                "Recreate the property-tour handoff once the connector/binding issue is fixed."
                if property_tour_followup_open
                else "No property-tour followup action is available for this thread.",
                "Tour",
                href=linked_handoff.tour_url if linked_handoff is not None else "",
                action_href=(
                    f"/app/actions/handoffs/{linked_handoff_ref}/recreate"
                    if linked_handoff_ref and property_tour_followup_open
                    else ""
                ),
                action_label="Recreate tour" if property_tour_followup_open else "",
                action_method="post" if property_tour_followup_open else "",
                return_to=f"/app/threads/{thread_ref}" if property_tour_followup_open else "",
            ),
            _object_detail_row(
                "Retry send in EA",
                retry_detail,
                "Action",
                href=f"/app/handoffs/{linked_handoff_ref}" if linked_handoff_ref else "",
                action_href=(
                    f"/app/actions/handoffs/{linked_handoff_ref}/retry-send"
                    if delivery_followup_open
                    else f"/app/actions/threads/{thread_ref}/resume-delivery"
                    if resume_followup_available
                    else ""
                ),
                action_label="Retry send" if delivery_followup_open else "Resume handoff" if resume_followup_available else "",
                action_method="post" if delivery_followup_open or resume_followup_available else "",
                return_to=f"/app/threads/{thread_ref}" if delivery_followup_open or resume_followup_available else "",
                secondary_action_href=f"/app/handoffs/{linked_handoff_ref}" if linked_handoff_ref else "",
                secondary_action_label="Open handoff" if linked_handoff_ref else "",
                secondary_action_method="get" if linked_handoff_ref else "",
            ),
            _object_detail_row(
                str(thread_google_action.get("label") or "Google delivery action"),
                "Repair Google access before retrying the send path attached to this thread."
                if thread_google_action
                else "No Google reconnect is required for this thread.",
                "Google",
                href="/app/settings/google" if thread_google_action else "",
                action_href=str(thread_google_action.get("href") or ""),
                action_label=str(thread_google_action.get("label") or ""),
                action_method=str(thread_google_action.get("method") or ""),
                return_to=f"/app/threads/{thread_ref}" if thread_google_action else "",
            ),
            _object_detail_row(
                "Manual resolution",
                resolved_manual_detail
                or "Record the real delivery outcome when a person finished the send outside EA or needs to keep the blocker visible.",
                "Resolution",
                href=f"/app/handoffs/{linked_handoff_ref}" if linked_handoff_ref else "",
                action_href=f"/app/actions/handoffs/{linked_handoff_ref}/complete" if delivery_followup_open else "",
                action_label="Mark sent" if delivery_followup_open else "",
                action_value="sent" if delivery_followup_open else "",
                action_method="post" if delivery_followup_open else "",
                return_to=f"/app/threads/{thread_ref}" if delivery_followup_open else "",
                secondary_action_href=f"/app/actions/handoffs/{linked_handoff_ref}/complete" if delivery_followup_open else "",
                secondary_action_label=manual_resolution_secondary_label if delivery_followup_open else "",
                secondary_action_value=manual_resolution_secondary_value if delivery_followup_open else "",
                secondary_action_method="post" if delivery_followup_open else "",
                secondary_return_to=f"/app/threads/{thread_ref}" if delivery_followup_open else "",
                tertiary_action_href=f"/app/actions/handoffs/{linked_handoff_ref}/complete" if delivery_followup_open else "",
                tertiary_action_label="Waiting on principal" if delivery_followup_open else "",
                tertiary_action_value="waiting_on_principal" if delivery_followup_open else "",
                tertiary_action_method="post" if delivery_followup_open else "",
                tertiary_return_to=f"/app/threads/{thread_ref}" if delivery_followup_open else "",
            ),
        ],
        object_sections=[
            {
                "eyebrow": "Decision links",
                "title": "Related office work",
                "items": [
                    _object_detail_row("Related decisions", ", ".join(thread.related_decision_ids or []) or "No linked decisions.", "Decision"),
                    _object_detail_row("Related commitments", ", ".join(thread.related_commitment_ids or []) or "No linked commitments.", "Commitment"),
                    _object_detail_row("Draft queue", ", ".join(thread.draft_ids or []) or "No active drafts.", "Draft"),
                ],
            },
            {
                "eyebrow": "Sources",
                "title": "Linked sources",
                "items": _evidence_detail_rows(thread.evidence_refs),
            },
            {
                "eyebrow": "Thread history",
                "title": "Recent thread history",
                "items": [
                    _object_detail_row(
                        str(item.event_type or "history").replace("_", " ").title(),
                        " · ".join(
                            part
                            for part in (
                                str(item.detail or "").strip(),
                                str(item.created_at or "")[:19] if item.created_at else "",
                            )
                            if part
                        )
                        or "Thread activity was recorded.",
                        str(item.actor or "thread").strip() or "Thread",
                    )
                    for item in history
                ] or [_object_detail_row("No thread history yet", "No thread events were recorded yet.", "History")],
            },
        ],
    )


@router.get("/app/evidence/{evidence_ref}", response_class=HTMLResponse)
def evidence_detail(
    evidence_ref: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    _raise_propertyquarry_object_detail_disabled(request)
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    evidence = product.get_evidence(
        principal_id=context.principal_id,
        evidence_ref=evidence_ref,
        operator_id=str(context.operator_id or "").strip(),
    )
    if evidence is None:
        raise HTTPException(status_code=404, detail="evidence_not_found")
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="evidence_opened",
        surface=f"evidence:{evidence_ref}",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    href_value = str(evidence.href or "").strip()
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title=f"PropertyQuarry {evidence.label}",
        current_nav="people",
        console_title=evidence.label,
        console_summary="Evidence provenance, source type, and the objects that currently depend on it.",
        object_kind="Evidence",
        object_title=evidence.label,
        object_summary=evidence.summary or "This evidence ref supports one or more projected product objects.",
        object_meta=[
            {"label": "Source type", "value": str(evidence.source_type or "unknown").replace("_", " ").title()},
            {"label": "Linked objects", "value": str(len(evidence.related_object_refs or []))},
            {"label": "Reference", "value": "External link" if href_value else "Embedded"},
            {"label": "Status", "value": "Available"},
        ],
        object_sidebar_title="Provenance",
        object_sidebar_copy="Evidence explains why the product surfaced something and what objects currently depend on that fact.",
        object_sidebar_rows=[
            _object_detail_row("Reference", href_value or "No external URL attached to this evidence row.", "Link"),
            _object_detail_row("Related objects", ", ".join(evidence.related_object_refs or []) or "No linked objects yet.", "Objects"),
            _object_detail_row("Source label", evidence.label, "Evidence"),
        ],
        object_sections=[
            {
                "eyebrow": "Evidence summary",
                "title": "What this evidence says",
                "items": [_object_detail_row(evidence.label, evidence.summary or "No summary projected.", str(evidence.source_type or "evidence").title())],
            },
            {
                "eyebrow": "Dependencies",
                "title": "Objects linked to this evidence",
                "items": [
                    _object_detail_row(ref, "This product object currently references the evidence row.", "Linked")
                    for ref in (evidence.related_object_refs or [])
                ]
                or [_object_detail_row("No linked objects", "Nothing else points at this evidence yet.", "Pending")],
            },
        ],
    )


@router.get("/app/rules/{rule_id}", response_class=HTMLResponse)
def rule_detail(
    rule_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    _raise_propertyquarry_object_detail_disabled(request)
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    rule = product.get_rule(principal_id=context.principal_id, rule_id=rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule_not_found")
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="rules_opened",
        surface=f"rule:{rule_id}",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    simulated_effect = str(rule.simulated_effect or "").strip()
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title=f"PropertyQuarry {rule.label}",
        current_nav="settings",
        console_title=rule.label,
        console_summary="Rule scope, current value, impact, and whether changes need approval.",
        object_kind="Rules",
        object_title=rule.label,
        object_summary=rule.summary or "This rule shapes how the assistant reads, drafts, sends, remembers, or escalates work.",
        object_meta=[
            {"label": "Scope", "value": str(rule.scope or "workspace").replace("_", " ").title()},
            {"label": "Status", "value": str(rule.status or "active").replace("_", " ").title()},
            {"label": "Current value", "value": str(rule.current_value or "Not set")},
            {"label": "Approval", "value": "Required" if rule.requires_approval else "Direct save"},
        ],
        object_sidebar_title="Rule effect",
        object_sidebar_copy="Rules stay legible in product language: what they change, who they affect, and whether the change needs approval.",
        object_sidebar_rows=[
            _object_detail_row("Impact", rule.impact or "No impact summary projected yet.", "Impact"),
            _object_detail_row("Simulation", simulated_effect or "Run a simulation in Settings before changing this rule.", "Simulate"),
            _object_detail_row("Change control", "Approval gate applies." if rule.requires_approval else "Directly editable in the current plan.", "Governance"),
        ],
        object_sections=[
            {
                "eyebrow": "Rule summary",
                "title": "Current state",
                "items": [
                    _object_detail_row(rule.label, rule.summary or "No rule summary projected.", str(rule.status or "active").title()),
                    _object_detail_row("Current value", str(rule.current_value or "Not set"), "Value"),
                ],
            },
            {
                "eyebrow": "Simulation",
                "title": "Expected effect",
                "items": [
                    _object_detail_row("Preview", simulated_effect or "Use the rules surface to simulate this rule before saving changes.", "Effect")
                ],
            },
        ],
    )
