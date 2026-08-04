from __future__ import annotations

import base64
import binascii
import contextlib
import concurrent.futures
import hashlib
import html
import hmac
import io
import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.api.dependencies import (
    RequestContext,
    _extract_token,
    _propertyquarry_identity_session_payload,
    _resolved_principal_id,
    _workspace_session_payload,
    browser_principal_override_allowed,
    get_cloudflare_access_identity,
    get_container,
    get_request_context,
    get_request_context_if_available,
    require_operator_context,
)
from app.api.routes.landing_content import (
    ADMIN_NAV_GROUPS,
    APP_NAV_GROUPS,
    app_nav_groups_for_brand,
    DOC_LINKS,
    FEATURE_CARDS,
    HOW_STEPS,
    LANDING_FAQS,
    PERSONAS,
    PRICING_TIERS,
    PRODUCT_MODULES,
    PUBLIC_NAV,
    PUBLIC_TRUST_PAGES,
    SIGN_IN_NOTES,
    TRUST_CARDS,
)
from app.api.routes.landing_property_surface_contracts import PropertySurfaceScope
from app.api.routes.landing_property_search_health import build_property_search_health_snapshot
from app.api.routes.landing_property_workspace_payload import (
    _property_workbench_client_candidate_payload,
)
from app.api.routes.landing_property_workspace_helpers import (
    _compact_provider_label,
    _property_candidate_source_virtual_tour_url,
)
from app.api.routes.landing_view_models import (
    PROPERTY_FURNITURE_STYLE_CATALOG,
    app_section_payload as _app_section_payload,
    channel_cards as _channel_cards,
    humanize as _humanize,
    list_rows as _list_rows,
    _clean_property_candidate_copy as _shared_clean_property_candidate_copy,
    _clean_property_candidate_detail_copy as _shared_clean_property_candidate_detail_copy,
    _normalized_property_distance_importance,
    _property_customer_candidate_summary,
    _property_result_title_display,
    property_workspace_payload as _property_workspace_payload,
)
from app.api.routes.landing_property_research import (
    _candidate_detail_sections,
    _evidence_detail_rows,
    _google_maps_embed_url,
    _object_detail_row,
    _official_risk_posture_rows,
    _property_distance_ooda_rows,
    _property_distance_ooda_rows_for_preferences,
    _property_distance_panel_rows,
    _property_candidate_orientation_preview,
    _property_candidate_preview_image,
    _property_candidate_ref,
    _property_enriched_candidate_facts,
    _property_fact_rows,
    _property_hosted_tour_disabled_fallback,
    _property_investment_research_rows,
    _property_lookup_candidate,
    _property_market_currency_display,
    _property_market_decimal_display,
    _property_market_display_context,
    _property_missing_fact_items,
    _property_packet_decision_rows,
    _property_packet_everyday_fit_rows,
    _property_packet_evidence_overlay_rows,
    _property_packet_future_research_rows,
    _property_packet_missing_rows,
    _property_packet_official_evidence_rows,
    _property_packet_official_posture_rows,
    _property_packet_provenance_rows,
    _property_packet_risk_fit_rows,
    _property_packet_score_rows,
    _property_normalized_mismatch_reasons,
    _property_research_gallery_items,
    _property_research_money_display,
    _property_review_detail_line,
    _property_rooms_research_relevant,
    _property_rooms_display,
    _property_tour_media_payload,
    _property_tour_detail_line,
)
from app.api.routes.admin_view_models import build_admin_section_payload as _build_admin_section_payload
from app.api.routes.workspace_view_models import workspace_section_payload as _workspace_section_payload
from app.container import AppContainer
from app.product.commercial import workspace_plan_for_mode
from app.product import property_tour_hosting
from app.product.privacy_lifecycle import build_property_account_privacy_lifecycle
from app.product.property_surface_state import (
    build_property_billing_truth_snapshot,
    build_property_research_packet_snapshot,
    build_property_run_health_snapshot,
    normalize_property_search_run_snapshot,
)
from app.product.property_score_methodology import (
    build_property_score_methodology,
    resolve_property_score_methodology_language,
)
from app.product.property_fact_enrichment import (
    PROPERTY_FACT_ENRICHMENT_SCHEMA_VERSION,
    property_fact_distance_specs,
    property_fact_requirement_plan,
    property_fact_score_projection,
)
from app.product.projections.common import compact_text
from app.product.service import (
    _hosted_property_visual_progress_snapshot,
    _hosted_property_visual_progress_stage_label,
    _hosted_property_tour_telegram_preview_image_url_for_style,
    _property_candidate_has_floorplan,
    _property_fact_value_is_weak,
    _property_scout_candidate_payload_from_preview,
    _property_scout_page_preview_with_timeout,
    _property_visual_terminal_status_for_reason,
    _property_visual_unavailable_detail,
    _property_visual_eta_label,
    _property_visual_progress_pct,
    _property_visual_ready_tour_url,
    build_product_service,
)
from app.services.cloudflare_access import CloudflareAccessIdentity
from app.services import google_oauth as google_oauth_service
from app.services import propertyquarry_google_identity
from app.services import public_analytics_consent as analytics_consent
from app.services.google_oauth import complete_google_oauth_callback
from app.services.property_billing import payfunnels_configured, property_commercial_snapshot
from app.services.property_curated_diorama import (
    build_curated_diorama_entry_index,
    build_curated_diorama_preview_index,
)
from app.services.property_market_catalog import (
    country_label as property_country_label,
    country_options as property_country_options,
    default_language_for_country,
    default_platforms_for_country_listing_mode,
    default_platforms_for_country,
    evidence_source_options as property_evidence_source_options,
    filter_selectable_property_platforms as property_filter_selectable_property_platforms,
    investment_strategy_label as property_investment_strategy_label,
    investment_strategy_options as property_investment_strategy_options,
    language_label as property_language_label,
    language_options as property_language_options,
    listing_mode_label as property_listing_mode_label,
    listing_mode_options as property_listing_mode_options,
    investment_research_mode_label as property_investment_research_mode_label,
    investment_research_mode_options as property_investment_research_mode_options,
    is_customer_search_country_code,
    normalize_country_code,
    normalize_listing_mode,
    normalize_property_platform,
    normalize_property_search_preferences,
    property_type_label as property_type_label_for_value,
    property_type_options as property_type_options_catalog,
    provider_options as property_provider_options,
    search_goal_label as property_search_goal_label,
    search_goal_options as property_search_goal_options,
    supported_currency_codes,
)
from app.services.onboarding import flatten_property_search_preferences_snapshot
from app.services.public_branding import request_brand
from app.services.public_clickrank import (
    clickrank_head_snippet as _clickrank_head_snippet,
    request_hostname as _request_hostname,
    request_path as _request_path,
)
from app.services.public_heyy_live_chat import heyy_live_chat_head_snippet as _heyy_live_chat_head_snippet
from app.services.public_rybbit import rybbit_head_snippet as _rybbit_head_snippet
from app.services.registration_email import email_delivery_enabled, property_notification_preview
from app.services.fliplink import build_fliplink_packet_service
from app.services import brilliant_directories as brilliant_directories_service
from app.settings import resolve_signing_secret


_PROPERTY_ACCOUNT_PREFERENCE_PROFILE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="pq-account-pref-prof",
)
_PROPERTY_BILLING_HANDOFF_VERIFICATION_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="pq-billing-handoff",
)
_PROPERTY_FIRST_PAINT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="pq-first-paint",
)
_PROPERTY_BILLING_HANDOFF_CACHE_LOCK = threading.Lock()
_PROPERTY_BILLING_HANDOFF_CACHE: dict[str, object] = {
    "hosted_url": "",
    "receipt": {},
    "expires_at": 0.0,
    "future": None,
}
_PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE_LOCK = threading.Lock()
_PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE: dict[str, object] = {
    "cache_key": "",
    "receipt": {},
    "expires_at": 0.0,
    "future": None,
}
_PROPERTYQUARRY_EXAMPLE_SHORTLIST_DIORAMA_VERSION = "20260724d2"
_PROPERTY_CURATED_DIORAMA_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "data" / "property_diorama_previews.json"
_PROPERTY_CURATED_DIORAMA_STATIC_ROOT = Path(__file__).resolve().parents[2] / "static"
_PROPERTY_ORIGINAL_TOUR_HANDOFF_PURPOSE = "property-original-tour-handoff-v1"
_PROPERTY_ORIGINAL_TOUR_HANDOFF_TTL_SECONDS = 15 * 60
_PROPERTY_ORIGINAL_TOUR_HANDOFF_MAX_TOKEN_CHARS = 1536


def _property_first_paint_timeout_seconds() -> float:
    raw_value = str(os.getenv("PROPERTYQUARRY_FIRST_PAINT_LOOKUP_TIMEOUT_SECONDS") or "1.2").strip()
    try:
        timeout_seconds = float(raw_value)
    except (TypeError, ValueError):
        timeout_seconds = 1.2
    if timeout_seconds <= 0:
        return 0.0
    return max(0.05, timeout_seconds)


def _property_first_paint_value(loader: Callable[[], Any], fallback: Any) -> Any:
    timeout_seconds = _property_first_paint_timeout_seconds()
    if timeout_seconds <= 0:
        return fallback
    future = _PROPERTY_FIRST_PAINT_EXECUTOR.submit(loader)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        return fallback
    except Exception:
        return fallback

router = APIRouter(tags=["landing"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))

_PROPERTYQUARRY_DEFAULT_UI_LOCALE = "en"
_PROPERTYQUARRY_SUPPORTED_UI_LOCALES = (_PROPERTYQUARRY_DEFAULT_UI_LOCALE,)
_PROPERTYQUARRY_RTL_UI_LOCALES: frozenset[str] = frozenset()


def _propertyquarry_normalize_ui_locale(value: object) -> str:
    normalized = str(value or "").strip().replace("_", "-").lower()
    if not normalized or len(normalized) > 35:
        return ""
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", normalized):
        return ""
    return normalized


def _propertyquarry_accept_language_tags(request: Request) -> list[str]:
    tags: list[str] = []
    for item in str(request.headers.get("accept-language") or "").split(","):
        tag = _propertyquarry_normalize_ui_locale(item.split(";", 1)[0])
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _propertyquarry_ui_locale_context(request: Request) -> dict[str, object]:
    """Resolve only locales whose complete UI copy is actually supported.

    Market/content language is deliberately not treated as UI translation. Until
    another locale is fully translated and added to the governed supported list,
    the English interface remains labelled English even for DE/AT/CR searches.
    """

    explicit = _propertyquarry_normalize_ui_locale(request.query_params.get("ui_locale"))
    cookie = _propertyquarry_normalize_ui_locale(request.cookies.get("propertyquarry_ui_locale"))
    accepted = _propertyquarry_accept_language_tags(request)
    requested = explicit or cookie or (accepted[0] if accepted else "")
    candidates = [explicit] if explicit else ([cookie] if cookie else accepted)
    selected = next(
        (candidate for candidate in candidates if candidate in _PROPERTYQUARRY_SUPPORTED_UI_LOCALES),
        _PROPERTYQUARRY_DEFAULT_UI_LOCALE,
    )
    return {
        "ui_locale": selected,
        "ui_locale_requested": requested,
        "ui_locale_fallback": bool(requested and requested != selected),
        "supported_ui_locales": list(_PROPERTYQUARRY_SUPPORTED_UI_LOCALES),
        "document_language": selected,
        "document_direction": "rtl" if selected in _PROPERTYQUARRY_RTL_UI_LOCALES else "ltr",
    }


@dataclass(frozen=True, slots=True)
class GovernedPropertyTourBridgeRuntime:
    lifecycle_store: property_tour_hosting.GovernedPropertyTourLifecycleStore
    source_authority_verifier: Callable[
        [Mapping[str, object], str],
        property_tour_hosting.VerifiedPropertyTourSourceAuthority,
    ]
    retention_policy_verifier: Callable[
        [Mapping[str, object], str],
        property_tour_hosting.GovernedPropertyTourRetentionPolicy,
    ] | None
    compose_audit: Callable[[Mapping[str, object]], Mapping[str, object]]
    publication_authority_verifier: Callable[
        [Mapping[str, object], Mapping[str, object], Mapping[str, object], str, str],
        property_tour_hosting.VerifiedGovernedPropertyTourPublication | None,
    ] | None = None
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def get_governed_property_tour_runtime(request: Request) -> GovernedPropertyTourBridgeRuntime:
    factory = getattr(request.app.state, "governed_property_tour_runtime_factory", None)
    if not callable(factory):
        raise HTTPException(status_code=503, detail="governed_property_tour_runtime_unconfigured")
    runtime = factory()
    if not isinstance(runtime, GovernedPropertyTourBridgeRuntime):
        raise HTTPException(status_code=503, detail="governed_property_tour_runtime_invalid")
    return runtime


def require_governed_property_tour_authenticated_context(
    context: RequestContext = Depends(get_request_context),
) -> RequestContext:
    principal_id = str(context.principal_id or "").strip()
    auth_source = str(context.auth_source or "").strip().lower()
    if (
        not principal_id
        or context.authenticated is not True
        or auth_source in {"", "anonymous", "loopback_no_auth"}
    ):
        raise HTTPException(status_code=401, detail="authentication_required")
    return context


def _governed_property_route_reason(error: Exception) -> str:
    reason = str(error).split(":", 1)[0].strip().lower()
    return reason if re.fullmatch(r"[a-z0-9_]{1,128}", reason) else "governed_property_tour_rejected"


async def _governed_property_raw_body(
    request: Request,
    *,
    allowed_fields: frozenset[str],
) -> dict[str, object]:
    try:
        payload = property_tour_hosting.parse_governed_property_tour_raw_json(await request.body())
    except property_tour_hosting.GovernedPropertyTourContractError as exc:
        raise HTTPException(status_code=422, detail=_governed_property_route_reason(exc)) from exc
    if set(payload).difference(allowed_fields):
        raise HTTPException(status_code=422, detail="unexpected_fields")
    return payload


def _governed_property_safe_status(
    lifecycle: Mapping[str, object],
    *,
    composition_digest: str = "",
    bridge_digest: str = "",
) -> dict[str, object]:
    raw_status = str(lifecycle.get("status") or "blocked")
    status = raw_status if raw_status in {"active", "active_private", "blocked", "deleted", "revoked", "withdrawn"} else "blocked"
    reason = str(lifecycle.get("reason") or "")
    if reason and not re.fullmatch(r"[a-z0-9_]{1,128}", reason):
        reason = "governed_property_tour_blocked"
    receipt_digest = str(lifecycle.get("integrity_digest") or lifecycle.get("lifecycle_receipt_digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", receipt_digest):
        receipt_digest = ""
    return {
        "contract_name": "propertyquarry.governed_spatial_lifecycle_projection.v1",
        "status": status,
        "reason": reason,
        "composition_digest": composition_digest if re.fullmatch(r"sha256:[0-9a-f]{64}", composition_digest) else "",
        "bridge_digest": bridge_digest if re.fullmatch(r"sha256:[0-9a-f]{64}", bridge_digest) else "",
        "lifecycle_receipt_digest": receipt_digest,
        "deleted": lifecycle.get("deleted") is True,
        "revoked": lifecycle.get("revoked") is True,
        "public_ready": False,
        "serving_allowed": False,
        "provider_details_exposed": False,
        "artifact_ref": "",
    }


@router.post("/app/api/property/governed-spatial/tours/{slug}/intake", include_in_schema=False)
async def governed_property_tour_intake(
    slug: str,
    request: Request,
    context: RequestContext = Depends(require_governed_property_tour_authenticated_context),
    runtime: GovernedPropertyTourBridgeRuntime = Depends(get_governed_property_tour_runtime),
) -> JSONResponse:
    payload = await _governed_property_raw_body(
        request,
        allowed_fields=frozenset(
            {
                "contract_name",
                "property_packet",
                "request_id",
                "idempotency_key",
                "style_pack_id",
                "product_event_ref",
                "retention_policy",
            }
        ),
    )
    if payload.get("contract_name") != "propertyquarry.governed_spatial_lifecycle_intake.v1":
        raise HTTPException(status_code=422, detail="intake_contract_invalid")
    property_packet = payload.get("property_packet")
    retention_policy = payload.get("retention_policy")
    if not isinstance(property_packet, Mapping) or not isinstance(retention_policy, Mapping):
        raise HTTPException(status_code=422, detail="property_packet_and_retention_policy_required")
    observed = runtime.now()
    if not callable(runtime.retention_policy_verifier):
        raise HTTPException(status_code=503, detail="retention_policy_authority_unconfigured")
    try:
        approved_policy = runtime.retention_policy_verifier(retention_policy, context.principal_id)
    except property_tour_hosting.GovernedPropertyTourContractError as exc:
        raise HTTPException(status_code=422, detail="retention_policy_not_approved") from exc
    if not isinstance(approved_policy, property_tour_hosting.GovernedPropertyTourRetentionPolicy):
        raise HTTPException(status_code=503, detail="retention_policy_authority_invalid")
    approved_policy_payload = approved_policy.as_payload()
    try:
        revalidated_policy = property_tour_hosting.GovernedPropertyTourRetentionPolicy.from_payload(
            approved_policy_payload,
            observed_at=observed,
        )
    except property_tour_hosting.GovernedPropertyTourContractError as exc:
        raise HTTPException(status_code=422, detail="retention_policy_not_current") from exc
    if revalidated_policy != approved_policy or dict(retention_policy) != approved_policy_payload:
        raise HTTPException(status_code=422, detail="retention_policy_binding_mismatch")
    try:
        source_authority = runtime.source_authority_verifier(property_packet, context.principal_id)
        bridge = property_tour_hosting.build_governed_property_tour_request(
            property_packet=property_packet,
            request_id=str(payload.get("request_id") or ""),
            idempotency_key=str(payload.get("idempotency_key") or ""),
            style_pack_id=str(payload.get("style_pack_id") or ""),
            product_event_ref=str(payload.get("product_event_ref") or ""),
            verified_source_authority=source_authority,
            observed_at=observed,
        )
    except property_tour_hosting.GovernedPropertyTourContractError as exc:
        raise HTTPException(status_code=422, detail=_governed_property_route_reason(exc)) from exc
    try:
        composition = runtime.compose_audit(bridge)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="compose_audit_rejected") from exc
    if not isinstance(composition, Mapping):
        raise HTTPException(status_code=409, detail="compose_audit_projection_invalid")
    composition_digest = str(composition.get("composition_digest") or "")
    composition_receipt_digest = str(composition.get("composition_receipt_digest") or "")
    zero_burn = (
        composition.get("status") == "accepted"
        and composition.get("audit_only") is True
        and composition.get("quota_mutated") is False
        and composition.get("provider_job_enqueued") is False
        and composition.get("executable") is False
        and re.fullmatch(r"sha256:[0-9a-f]{64}", composition_digest)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", composition_receipt_digest)
    )
    if not zero_burn:
        raise HTTPException(status_code=409, detail="compose_audit_zero_burn_contract_invalid")
    publication_authority = (
        runtime.publication_authority_verifier(
            bridge,
            composition,
            approved_policy_payload,
            context.principal_id,
            slug,
        )
        if runtime.publication_authority_verifier is not None
        else None
    )
    try:
        lifecycle = runtime.lifecycle_store.intake(
            slug=slug,
            policy_payload=approved_policy_payload,
            observed_at=observed,
            bridge_digest=str(bridge["bridge_digest"]),
            composition_digest=composition_digest,
            composition_receipt_digest=composition_receipt_digest,
            publication_authority=publication_authority,
            owner_principal_ref=context.principal_id,
            tenant_ref=str(property_packet.get("tenant_ref") or ""),
            subject_ref=str(property_packet.get("subject_ref") or ""),
        )
    except property_tour_hosting.GovernedPropertyTourContractError as exc:
        raise HTTPException(status_code=409, detail=_governed_property_route_reason(exc)) from exc
    except property_tour_hosting.GovernedPropertyTourIntegrityError as exc:
        raise HTTPException(status_code=503, detail="governed_property_lifecycle_integrity_blocked") from exc
    return JSONResponse(
        _governed_property_safe_status(
            lifecycle,
            composition_digest=composition_digest,
            bridge_digest=str(bridge["bridge_digest"]),
        )
    )


@router.get("/app/api/property/governed-spatial/tours/{slug}/status", include_in_schema=False)
def governed_property_tour_status(
    slug: str,
    context: RequestContext = Depends(require_governed_property_tour_authenticated_context),
    runtime: GovernedPropertyTourBridgeRuntime = Depends(get_governed_property_tour_runtime),
) -> JSONResponse:
    try:
        runtime.lifecycle_store.require_owner(slug=slug, owner_principal_ref=context.principal_id)
        lifecycle = runtime.lifecycle_store.enforce_retention(slug=slug, observed_at=runtime.now())
    except property_tour_hosting.GovernedPropertyTourContractError as exc:
        reason = _governed_property_route_reason(exc)
        if reason in {"governed_tour_scope_not_found", "governed_tour_scope_owner_mismatch"}:
            raise HTTPException(status_code=404, detail="governed_property_tour_not_found") from exc
        raise HTTPException(status_code=422, detail=reason) from exc
    except property_tour_hosting.GovernedPropertyTourIntegrityError as exc:
        raise HTTPException(status_code=503, detail="governed_property_lifecycle_integrity_blocked") from exc
    return JSONResponse(_governed_property_safe_status(lifecycle))


@router.post("/app/api/property/governed-spatial/tours/{slug}/privacy-closeout", include_in_schema=False)
async def governed_property_tour_privacy_closeout(
    slug: str,
    request: Request,
    context: RequestContext = Depends(require_governed_property_tour_authenticated_context),
    runtime: GovernedPropertyTourBridgeRuntime = Depends(get_governed_property_tour_runtime),
) -> JSONResponse:
    try:
        runtime.lifecycle_store.require_owner(slug=slug, owner_principal_ref=context.principal_id)
    except property_tour_hosting.GovernedPropertyTourContractError as exc:
        raise HTTPException(status_code=404, detail="governed_property_tour_not_found") from exc
    payload = await _governed_property_raw_body(
        request,
        allowed_fields=frozenset(
            {"contract_name", "action", "reason_digest", "cascade_evidence_digests", "legal_hold"}
        ),
    )
    if payload.get("contract_name") != "propertyquarry.governed_spatial_privacy_closeout.v1":
        raise HTTPException(status_code=422, detail="privacy_closeout_contract_invalid")
    evidence = payload.get("cascade_evidence_digests")
    if not isinstance(evidence, list):
        raise HTTPException(status_code=422, detail="cascade_evidence_digests_list_required")
    legal_hold = payload.get("legal_hold")
    if legal_hold is not None and not isinstance(legal_hold, Mapping):
        raise HTTPException(status_code=422, detail="legal_hold_object_required")
    try:
        lifecycle = runtime.lifecycle_store.closeout(
            slug=slug,
            action=str(payload.get("action") or ""),
            reason_digest=str(payload.get("reason_digest") or ""),
            observed_at=runtime.now(),
            cascade_evidence_digests=[str(value) for value in evidence],
            legal_hold=legal_hold,
        )
    except property_tour_hosting.GovernedPropertyTourContractError as exc:
        raise HTTPException(status_code=422, detail=_governed_property_route_reason(exc)) from exc
    except property_tour_hosting.GovernedPropertyTourIntegrityError as exc:
        raise HTTPException(status_code=503, detail="governed_property_lifecycle_integrity_blocked") from exc
    return JSONResponse(_governed_property_safe_status(lifecycle))


@lru_cache(maxsize=1)
def _property_workbench_script_asset() -> tuple[str, str]:
    body = templates.get_template("app/_property_workbench_script.html").render(surface_mode="search")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return body, f'"pq-workbench-{digest}"'


@lru_cache(maxsize=1)
def _property_search_loader_script_asset() -> tuple[str, str]:
    body = templates.get_template("app/_property_search_loader_script.html").render()
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return body, f'"pq-search-loader-{digest}"'


@lru_cache(maxsize=2)
def _property_workbench_css_asset(*, static_surface: bool = False) -> tuple[str, str]:
    source, _filename, _uptodate = templates.env.loader.get_source(  # type: ignore[union-attr]
        templates.env,
        "app/property_decision_workbench.html",
    )
    start_marker = "{# PQ_WORKBENCH_CSS_START"
    end_marker = "PQ_WORKBENCH_CSS_END #}"
    if start_marker not in source or end_marker not in source:
        return "", '"pq-workbench-css-missing"'
    raw_block = source.split(start_marker, 1)[1].split(end_marker, 1)[0]
    css_source = raw_block.split("<style>", 1)[1].rsplit("</style>", 1)[0] if "<style>" in raw_block and "</style>" in raw_block else raw_block
    body = templates.env.from_string(css_source).render(static_surface=static_surface)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    variant = "static" if static_surface else "app"
    return body, f'"pq-workbench-css-{variant}-{digest}"'


def _property_asset_version(etag: str) -> str:
    normalized = str(etag or "").strip().strip('"')
    return normalized.rsplit("-", 1)[-1] if "-" in normalized else normalized


def _property_workbench_script_asset_url() -> str:
    _body, etag = _property_workbench_script_asset()
    version = urllib.parse.quote(_property_asset_version(etag), safe="")
    return f"/app/assets/property-workbench.js?v={version}" if version else "/app/assets/property-workbench.js"


def _property_search_loader_script_asset_url() -> str:
    _body, etag = _property_search_loader_script_asset()
    version = urllib.parse.quote(_property_asset_version(etag), safe="")
    return f"/app/assets/property-search-loader.js?v={version}" if version else "/app/assets/property-search-loader.js"


def _property_workbench_css_asset_url(*, static_surface: bool = False) -> str:
    _body, etag = _property_workbench_css_asset(static_surface=bool(static_surface))
    version = urllib.parse.quote(_property_asset_version(etag), safe="")
    query = []
    if static_surface:
        query.append(("surface", "static"))
    if version:
        query.append(("v", version))
    query_string = urllib.parse.urlencode(query)
    return f"/app/assets/property-workbench.css?{query_string}" if query_string else "/app/assets/property-workbench.css"


def _property_workbench_asset_headers(*, etag: str, version: str = "") -> dict[str, str]:
    expected_version = _property_asset_version(etag)
    versioned = bool(str(version or "").strip()) and str(version or "").strip() == expected_version
    cache_control = (
        "public, max-age=86400, immutable"
        if versioned
        else "public, max-age=3600, stale-while-revalidate=86400"
    )
    return {
        "Cache-Control": cache_control,
        "ETag": etag,
    }


templates.env.globals["property_workbench_script_asset_url"] = _property_workbench_script_asset_url
templates.env.globals["property_search_loader_script_asset_url"] = _property_search_loader_script_asset_url
templates.env.globals["property_workbench_css_asset_url"] = _property_workbench_css_asset_url


def _property_brilliant_directories_billing_handoff(*, allow_verified_direct_handoff: bool = False) -> dict[str, object]:
    bridge_receipt = brilliant_directories_service.build_brilliant_directories_billing_sso_bridge_receipt()
    bridge_ready = bool(bridge_receipt.get("ready"))
    member_token_receipt = brilliant_directories_service.build_brilliant_directories_member_login_token_receipt()
    member_token_ready = bool(member_token_receipt.get("ready"))
    verification_receipt = _property_cached_direct_billing_verification_receipt() if allow_verified_direct_handoff else {}
    verified_billing_handoff = dict(verification_receipt.get("billing_handoff") or {})
    verified_handoff_url = str(verified_billing_handoff.get("url") or "").strip()
    verified_direct_handoff_ready = bool(
        allow_verified_direct_handoff
        and verified_handoff_url
        and verified_billing_handoff.get("configured") is True
        and verified_billing_handoff.get("host_resolves") is True
        and verified_billing_handoff.get("account_handoff_usable") is not False
    )
    bridge_href = "/app/api/property/billing/bridge-launch" if bridge_ready else ""
    launch_href = "/app/api/property/billing/bridge-launch" if member_token_ready else ""
    hosted_urls = brilliant_directories_service.brilliant_directories_billing_handoff_urls()
    if not hosted_urls:
        if verified_direct_handoff_ready:
            return {
                "available": True,
                "status": "ready",
                "hosted_href": verified_handoff_url,
                "open_href": verified_handoff_url,
                "bridge_href": bridge_href,
                "bridge_status": "ready" if bridge_ready else str(bridge_receipt.get("error") or "").strip(),
                "member_token_status": "ready" if member_token_ready else str(member_token_receipt.get("error") or "").strip(),
            }
        if member_token_ready:
            return {
                "available": True,
                "status": "member_token_ready",
                "open_href": launch_href,
                "bridge_href": bridge_href,
                "bridge_status": "ready" if bridge_ready else str(bridge_receipt.get("error") or "").strip(),
                "member_token_status": "ready",
            }
        if bridge_ready:
            return {
                "available": True,
                "status": "bridge_ready",
                "open_href": bridge_href,
                "bridge_href": bridge_href,
                "bridge_status": "ready",
                "member_token_status": str(member_token_receipt.get("error") or "").strip(),
            }
        return {
            "available": False,
            "status": "disabled",
            "bridge_href": bridge_href,
            "bridge_status": "ready" if bridge_ready else str(bridge_receipt.get("error") or "").strip(),
            "member_token_status": str(member_token_receipt.get("error") or "").strip(),
        }
    primary_hosted_url = hosted_urls[0]
    if verified_direct_handoff_ready:
        return {
            "available": True,
            "status": "ready",
            "hosted_href": verified_handoff_url,
            "open_href": verified_handoff_url,
            "bridge_href": bridge_href,
            "bridge_status": "ready" if bridge_ready else str(bridge_receipt.get("error") or "").strip(),
            "member_token_status": "ready" if member_token_ready else str(member_token_receipt.get("error") or "").strip(),
        }
    runtime_dns_check = str(os.getenv("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_RUNTIME_DNS_CHECK") or "1").strip().lower()
    if runtime_dns_check not in {"0", "false", "no", "off", "disabled"}:
        first_blocked_state: dict[str, object] | None = None
        primary_verification_pending = False
        for index, hosted_url in enumerate(hosted_urls):
            handoff_receipt = _property_cached_billing_handoff_receipt(hosted_url=hosted_url)
            if not handoff_receipt:
                if index == 0:
                    primary_verification_pending = True
                continue
            if bool(handoff_receipt.get("host_resolves")) and handoff_receipt.get("account_handoff_usable") is not False:
                response = {
                    "available": True,
                    "status": "ready",
                    "hosted_href": hosted_url,
                    "open_href": hosted_url,
                    "bridge_href": bridge_href,
                    "bridge_status": "ready" if bridge_ready else str(bridge_receipt.get("error") or "").strip(),
                }
                if index > 0:
                    response["preferred_href"] = primary_hosted_url
                    response["fallback_active"] = True
                return response
            if first_blocked_state is None:
                if not bool(handoff_receipt.get("host_resolves")):
                    first_blocked_state = {
                        "available": False,
                        "status": "unresolved",
                        "hosted_href": hosted_url,
                        "error": str(handoff_receipt.get("error") or "billing_handoff_host_unresolved"),
                        "bridge_href": bridge_href,
                        "bridge_status": "ready" if bridge_ready else str(bridge_receipt.get("error") or "").strip(),
                        "member_token_status": "ready" if member_token_ready else str(member_token_receipt.get("error") or "").strip(),
                    }
                elif member_token_ready:
                    first_blocked_state = {
                        "available": True,
                        "status": "member_token_ready",
                        "hosted_href": hosted_url,
                        "open_href": launch_href,
                        "bridge_href": bridge_href,
                        "bridge_status": "ready" if bridge_ready else str(bridge_receipt.get("error") or "").strip(),
                        "member_token_status": "ready",
                        "error": str(
                            handoff_receipt.get("account_handoff_error")
                            or "billing_handoff_requires_separate_login"
                        ),
                    }
                elif bridge_ready:
                    first_blocked_state = {
                        "available": True,
                        "status": "bridge_ready",
                        "hosted_href": hosted_url,
                        "open_href": bridge_href,
                        "bridge_href": bridge_href,
                        "bridge_status": "ready",
                        "member_token_status": str(member_token_receipt.get("error") or "").strip(),
                        "error": str(
                            handoff_receipt.get("account_handoff_error")
                            or "billing_handoff_requires_separate_login"
                        ),
                    }
                else:
                    first_blocked_state = {
                        "available": False,
                        "status": "login_required",
                        "hosted_href": hosted_url,
                        "open_href": "",
                        "bridge_href": bridge_href,
                        "bridge_status": str(bridge_receipt.get("error") or "").strip(),
                        "member_token_status": str(member_token_receipt.get("error") or "").strip(),
                        "error": str(
                            handoff_receipt.get("account_handoff_error")
                            or "billing_handoff_requires_separate_login"
                        ),
                    }
        if primary_verification_pending:
            if member_token_ready:
                return {
                    "available": True,
                    "status": "member_token_ready",
                    "hosted_href": primary_hosted_url,
                    "open_href": launch_href,
                    "bridge_href": bridge_href,
                    "bridge_status": "ready" if bridge_ready else str(bridge_receipt.get("error") or "").strip(),
                    "member_token_status": "ready",
                }
            if bridge_ready:
                return {
                    "available": True,
                    "status": "bridge_ready",
                    "hosted_href": primary_hosted_url,
                    "open_href": bridge_href,
                    "bridge_href": bridge_href,
                    "bridge_status": "ready",
                    "member_token_status": str(member_token_receipt.get("error") or "").strip(),
                }
            return {
                "available": False,
                "status": "verifying",
                "hosted_href": primary_hosted_url,
                "open_href": "",
                "bridge_href": bridge_href,
                "bridge_status": "ready" if bridge_ready else str(bridge_receipt.get("error") or "").strip(),
                "member_token_status": str(member_token_receipt.get("error") or "").strip(),
                "verification_pending": True,
                "error": "billing_handoff_verification_pending",
            }
        if first_blocked_state is not None:
            return first_blocked_state
    return {
        "available": True,
        "status": "ready",
        "hosted_href": primary_hosted_url,
        "open_href": primary_hosted_url,
        "bridge_href": bridge_href,
        "bridge_status": "ready" if bridge_ready else str(bridge_receipt.get("error") or "").strip(),
    }


def _property_billing_fallback_href() -> str:
    return "/app/account?billing=1#delivery"


def _property_billing_usable_open_href(handoff: dict[str, object] | None) -> str:
    state = dict(handoff or {})
    if not bool(state.get("available")):
        return ""
    status = str(state.get("status") or "").strip().lower()
    if status not in {"ready", "member_token_ready", "bridge_ready"}:
        return ""
    return str(state.get("open_href") or "").strip()


def _property_pricing_billing_link_copy(handoff: dict[str, object] | None = None) -> tuple[str, str]:
    status = str((handoff or {}).get("status") or "").strip().lower()
    if status in {"ready", "member_token_ready", "bridge_ready"}:
        return (
            "Open billing",
            "Opens with this account.",
        )
    if status in {"login_required", "unresolved", "verifying", "disabled"}:
        return (
            "Billing",
            "Opens here when ready.",
        )
    return (
        "Open billing",
        "Manage plan and billing.",
    )


def _google_sign_in_enabled(request: Request | None = None) -> bool:
    if request is not None and request_brand(request).get("key") != "propertyquarry":
        return False
    return propertyquarry_google_identity.propertyquarry_google_identity_configured()


def _facebook_sign_in_enabled() -> bool:
    enabled_flag = str(os.environ.get("PROPERTYQUARRY_ENABLE_FACEBOOK_SIGN_IN") or "").strip().lower()
    if enabled_flag in {"0", "false", "no", "off", "disabled"}:
        return False
    configured = all(
        str(os.environ.get(key) or "").strip()
        for key in (
            "EA_FACEBOOK_OAUTH_APP_ID",
            "EA_FACEBOOK_OAUTH_APP_SECRET",
            "EA_FACEBOOK_OAUTH_REDIRECT_URI",
            "EA_FACEBOOK_OAUTH_STATE_SECRET",
        )
    )
    if enabled_flag:
        return enabled_flag in {"1", "true", "yes", "on", "enabled"} and configured
    return configured


def _id_austria_sign_in_enabled() -> bool:
    from app.services.id_austria_oidc import id_austria_sign_in_configured

    return id_austria_sign_in_configured()


def _public_tours_enabled_for_examples() -> bool:
    raw_value = os.environ.get("PROPERTYQUARRY_ENABLE_PUBLIC_TOURS")
    if raw_value is None:
        raw_value = os.environ.get("EA_ENABLE_PUBLIC_TOURS")
    if raw_value is None:
        raw_value = os.environ.get("PROPERTYQUARRY_ENABLE_PUBLIC_SIDE_SURFACES")
    if raw_value is None:
        raw_value = os.environ.get("EA_ENABLE_PUBLIC_SIDE_SURFACES")
    return str(raw_value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _propertyquarry_public_app_base_url() -> str:
    return str(os.environ.get("EA_PUBLIC_APP_BASE_URL") or "https://propertyquarry.com").strip().rstrip("/")


def _propertyquarry_absolute_public_url(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        parsed = urllib.parse.urlparse(normalized)
    except Exception:
        parsed = urllib.parse.urlparse("")
    if parsed.scheme and parsed.netloc:
        return normalized
    public_app_base_url = _propertyquarry_public_app_base_url()
    if normalized.startswith("/"):
        return f"{public_app_base_url}{normalized}"
    return f"{public_app_base_url}/{normalized.lstrip('/')}"


def _propertyquarry_public_href(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        parsed = urllib.parse.urlparse(normalized)
        base = urllib.parse.urlparse(_propertyquarry_public_app_base_url())
    except Exception:
        return normalized
    if parsed.scheme and parsed.netloc and parsed.scheme == base.scheme and parsed.netloc == base.netloc:
        return urllib.parse.urlunparse(("", "", parsed.path or "/", "", parsed.query, parsed.fragment))
    return normalized


def _propertyquarry_verified_public_tour_href(value: object) -> str:
    tour_url = _propertyquarry_absolute_public_url(value)
    if not tour_url:
        return ""
    first_party_tour_href = property_tour_hosting._hosted_property_tour_first_party_open_url(tour_url)
    return _propertyquarry_public_href(first_party_tour_href) if first_party_tour_href else ""


def _property_account_preference_profile_timeout_seconds() -> float:
    raw_value = os.getenv("PROPERTYQUARRY_ACCOUNT_PREFERENCE_PROFILE_TIMEOUT_SECONDS", "0.9")
    try:
        timeout_seconds = float(raw_value)
    except Exception:
        timeout_seconds = 0.9
    if timeout_seconds <= 0:
        return 0.0
    return max(0.25, timeout_seconds)


def _property_account_preference_bundle(
    *,
    product,
    principal_id: str,
    person_id: str,
) -> dict[str, object]:
    timeout_seconds = _property_account_preference_profile_timeout_seconds()
    if timeout_seconds <= 0:
        return {}
    try:
        future = _PROPERTY_ACCOUNT_PREFERENCE_PROFILE_EXECUTOR.submit(
            product.get_preference_profile,
            principal_id=principal_id,
            person_id=person_id,
        )
        return dict(future.result(timeout=timeout_seconds))
    except Exception:
        return {}


def _property_billing_handoff_cache_ttl_seconds() -> float:
    raw_value = os.getenv("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_RUNTIME_DNS_CACHE_TTL_SECONDS", "300")
    try:
        ttl_seconds = float(raw_value)
    except Exception:
        ttl_seconds = 300.0
    if ttl_seconds <= 0:
        return 0.0
    return max(5.0, ttl_seconds)


def _property_billing_handoff_timeout_seconds() -> float:
    raw_value = os.getenv("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_RUNTIME_DNS_TIMEOUT_SECONDS", "0.75")
    try:
        timeout_seconds = float(raw_value)
    except Exception:
        timeout_seconds = 0.75
    if timeout_seconds <= 0:
        return 0.0
    return max(0.01, timeout_seconds)


def _property_billing_handoff_verification_receipt(hosted_url: str) -> dict[str, object]:
    if not str(hosted_url or "").strip():
        return {}
    receipt = brilliant_directories_service.build_brilliant_directories_billing_handoff_receipt(hosted_url)
    return dict(receipt or {})


def _property_billing_handoff_cache_key(hosted_url: str) -> str:
    normalized_hosted_url = str(hosted_url or "").strip()
    pytest_current_test = str(os.getenv("PYTEST_CURRENT_TEST") or "").strip()
    if pytest_current_test:
        return f"{normalized_hosted_url}::{pytest_current_test}"
    return normalized_hosted_url


def _property_cached_billing_handoff_receipt(*, hosted_url: str) -> dict[str, object]:
    cache_key = _property_billing_handoff_cache_key(hosted_url)
    now = time.monotonic()
    cached_receipt: dict[str, object] = {}
    cached_future: concurrent.futures.Future[dict[str, object]] | None = None
    with _PROPERTY_BILLING_HANDOFF_CACHE_LOCK:
        cached_hosted_url = str(_PROPERTY_BILLING_HANDOFF_CACHE.get("hosted_url") or "").strip()
        if cached_hosted_url != cache_key:
            _PROPERTY_BILLING_HANDOFF_CACHE["hosted_url"] = cache_key
            _PROPERTY_BILLING_HANDOFF_CACHE["receipt"] = {}
            _PROPERTY_BILLING_HANDOFF_CACHE["expires_at"] = 0.0
            _PROPERTY_BILLING_HANDOFF_CACHE["future"] = None
        cached_receipt = dict(_PROPERTY_BILLING_HANDOFF_CACHE.get("receipt") or {})
        expires_at = float(_PROPERTY_BILLING_HANDOFF_CACHE.get("expires_at") or 0.0)
        if cached_receipt and expires_at > now:
            return cached_receipt
        cached_future = _PROPERTY_BILLING_HANDOFF_CACHE.get("future")  # type: ignore[assignment]
        if cached_future is None:
            cached_future = _PROPERTY_BILLING_HANDOFF_VERIFICATION_EXECUTOR.submit(
                _property_billing_handoff_verification_receipt,
                hosted_url,
            )
            _PROPERTY_BILLING_HANDOFF_CACHE["future"] = cached_future
        elif cached_future.done():
            _PROPERTY_BILLING_HANDOFF_CACHE["future"] = None
    if cached_receipt:
        return cached_receipt
    timeout_seconds = _property_billing_handoff_timeout_seconds()
    if timeout_seconds <= 0 or cached_future is None:
        return {}
    try:
        resolved_receipt = dict(cached_future.result(timeout=timeout_seconds) or {})
    except concurrent.futures.TimeoutError:
        return cached_receipt if cached_receipt else {}
    except Exception:
        with _PROPERTY_BILLING_HANDOFF_CACHE_LOCK:
            current_future = _PROPERTY_BILLING_HANDOFF_CACHE.get("future")
            if current_future is cached_future:
                _PROPERTY_BILLING_HANDOFF_CACHE["future"] = None
        return {}
    ttl_seconds = _property_billing_handoff_cache_ttl_seconds()
    with _PROPERTY_BILLING_HANDOFF_CACHE_LOCK:
        current_future = _PROPERTY_BILLING_HANDOFF_CACHE.get("future")
        if current_future is cached_future:
            _PROPERTY_BILLING_HANDOFF_CACHE["future"] = None
        _PROPERTY_BILLING_HANDOFF_CACHE["receipt"] = dict(resolved_receipt)
        _PROPERTY_BILLING_HANDOFF_CACHE["expires_at"] = now + ttl_seconds if ttl_seconds > 0 else 0.0
    return resolved_receipt


def _property_cached_direct_billing_verification_receipt() -> dict[str, object]:
    cache_key = str(os.getenv("PYTEST_CURRENT_TEST") or "").strip() or "global"
    now = time.monotonic()
    cached_receipt: dict[str, object] = {}
    cached_future: concurrent.futures.Future[dict[str, object]] | None = None
    with _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE_LOCK:
        current_key = str(_PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE.get("cache_key") or "").strip()
        if current_key != cache_key:
            _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE["cache_key"] = cache_key
            _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE["receipt"] = {}
            _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE["expires_at"] = 0.0
            _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE["future"] = None
        cached_receipt = dict(_PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE.get("receipt") or {})
        expires_at = float(_PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE.get("expires_at") or 0.0)
        if cached_receipt and expires_at > now:
            return cached_receipt
        cached_future = _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE.get("future")  # type: ignore[assignment]
        if cached_future is None:
            cached_future = _PROPERTY_BILLING_HANDOFF_VERIFICATION_EXECUTOR.submit(
                brilliant_directories_service.build_brilliant_directories_verification_receipt,
            )
            _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE["future"] = cached_future
        elif cached_future.done():
            _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE["future"] = None
    if cached_receipt:
        return cached_receipt
    timeout_seconds = _property_billing_handoff_timeout_seconds()
    if timeout_seconds <= 0 or cached_future is None:
        return {}
    try:
        resolved_receipt = dict(cached_future.result(timeout=timeout_seconds) or {})
    except concurrent.futures.TimeoutError:
        return cached_receipt if cached_receipt else {}
    except Exception:
        with _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE_LOCK:
            current_future = _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE.get("future")
            if current_future is cached_future:
                _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE["future"] = None
        return {}
    ttl_seconds = _property_billing_handoff_cache_ttl_seconds()
    with _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE_LOCK:
        current_future = _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE.get("future")
        if current_future is cached_future:
            _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE["future"] = None
        _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE["receipt"] = dict(resolved_receipt)
        _PROPERTY_BILLING_DIRECT_VERIFICATION_CACHE["expires_at"] = now + ttl_seconds if ttl_seconds > 0 else 0.0
    return resolved_receipt


def _propertyquarry_normalize_public_tour_candidate(
    candidate: object,
    *,
    principal_id: str = "",
) -> dict[str, object] | object:
    if not isinstance(candidate, dict):
        return candidate
    candidate_row = dict(candidate)
    tour_payload = dict(candidate_row.get("tour") or {}) if isinstance(candidate_row.get("tour"), dict) else {}
    tour_url = _propertyquarry_absolute_public_url(
        candidate_row.get("tour_url")
        or candidate_row.get("verified_tour_url")
        or tour_payload.get("tour_url")
        or tour_payload.get("url")
    )
    open_tour_url = _propertyquarry_absolute_public_url(
        candidate_row.get("open_tour_url")
        or candidate_row.get("verified_tour_url")
        or tour_payload.get("open_tour_url")
        or tour_payload.get("embed_url")
        or tour_payload.get("url")
    )
    normalized_principal_id = str(principal_id or "").strip()
    ready_tour_url = (
        _property_visual_ready_tour_url(
            tour_url=tour_url,
            open_tour_url=open_tour_url,
            principal_id=normalized_principal_id,
        )
        if normalized_principal_id
        else _property_visual_ready_tour_url(
            tour_url=tour_url,
            open_tour_url=open_tour_url,
        )
    )
    if ready_tour_url:
        candidate_row["tour_url"] = ready_tour_url
        candidate_row["verified_tour_url"] = ready_tour_url
        candidate_row["open_tour_url"] = ready_tour_url
        tour_payload["url"] = ready_tour_url
        if str(tour_payload.get("embed_url") or "").strip():
            tour_payload["embed_url"] = ready_tour_url
        elif "embed_url" not in tour_payload:
            tour_payload["embed_url"] = ready_tour_url
        candidate_row["tour"] = tour_payload
    else:
        branded_tour_url = next(
            (
                value
                for value in (tour_url, open_tour_url)
                if property_tour_hosting._is_branded_public_tour_url(value)
            ),
            "",
        )
        if not branded_tour_url:
            return candidate_row
        disabled_fallback_tour = bool(
            _property_hosted_tour_disabled_fallback(branded_tour_url)
        )
        if not str(candidate_row.get("generated_reconstruction_url") or "").strip():
            candidate_row["generated_reconstruction_url"] = branded_tour_url
        candidate_row["tour_url"] = ""
        candidate_row["verified_tour_url"] = ""
        candidate_row["open_tour_url"] = ""
        if disabled_fallback_tour:
            candidate_row["tour_status"] = ""
        if tour_payload:
            for key in ("url", "tour_url", "open_tour_url", "embed_url"):
                if key in tour_payload:
                    tour_payload[key] = ""
            if disabled_fallback_tour:
                tour_payload["status"] = ""
            candidate_row["tour"] = tour_payload
    return candidate_row


def _propertyquarry_normalize_run_public_tour_targets(
    run_payload: dict[str, object],
    *,
    principal_id: str = "",
) -> dict[str, object]:
    if not isinstance(run_payload, dict) or not run_payload:
        return run_payload
    summary = dict(run_payload.get("summary") or {}) if isinstance(run_payload.get("summary"), dict) else {}
    if not summary:
        return run_payload

    ranked_candidates = [
        _propertyquarry_normalize_public_tour_candidate(candidate, principal_id=principal_id)
        for candidate in list(summary.get("ranked_candidates") or [])
    ]
    if ranked_candidates:
        summary["ranked_candidates"] = ranked_candidates
    sources: list[dict[str, object]] = []
    for source in list(summary.get("sources") or []):
        if not isinstance(source, dict):
            continue
        source_row = dict(source)
        top_candidates = [
            _propertyquarry_normalize_public_tour_candidate(candidate, principal_id=principal_id)
            for candidate in list(source_row.get("top_candidates") or [])
        ]
        if top_candidates:
            source_row["top_candidates"] = top_candidates
        sources.append(source_row)
    if sources:
        summary["sources"] = sources
    normalized_run = dict(run_payload)
    normalized_run["summary"] = summary
    return normalized_run


def _propertyquarry_backfill_candidate_from_cached_preview(
    *,
    candidate: object,
    preview_cache_index: dict[str, dict[str, object]],
    preview_lookup: Any,
) -> dict[str, object] | object:
    if not isinstance(candidate, dict):
        return candidate
    candidate_row = dict(candidate)
    property_url = urllib.parse.urldefrag(str(candidate_row.get("property_url") or "").strip())[0]
    if not property_url:
        return candidate_row

    cached_preview: dict[str, object] | None = None
    with contextlib.suppress(Exception):
        preview = preview_lookup(cache_index=preview_cache_index, property_url=property_url)
        if isinstance(preview, dict) and preview:
            cached_preview = dict(preview)
    if not cached_preview:
        return candidate_row

    existing_facts = dict(candidate_row.get("property_facts") or {}) if isinstance(candidate_row.get("property_facts"), dict) else {}
    if isinstance(candidate_row.get("property_facts_json"), dict):
        existing_facts = {**existing_facts, **dict(candidate_row.get("property_facts_json") or {})}
    preview_facts = _property_scout_candidate_payload_from_preview(property_url=property_url, preview=cached_preview)
    merged_facts = dict(existing_facts)
    changed = False

    for key, value in preview_facts.items():
        if _property_fact_value_is_weak(merged_facts.get(key)) and not _property_fact_value_is_weak(value):
            merged_facts[key] = list(value) if isinstance(value, tuple) else dict(value) if isinstance(value, dict) else value
            changed = True
    for key in ("media_urls_json", "floorplan_urls_json", "source_virtual_tour_url", "panorama_source"):
        value = cached_preview.get(key)
        if _property_fact_value_is_weak(merged_facts.get(key)) and not _property_fact_value_is_weak(value):
            merged_facts[key] = list(value) if isinstance(value, tuple) else dict(value) if isinstance(value, dict) else value
            changed = True

    current_title = str(candidate_row.get("title") or "").strip()
    preview_title = str(cached_preview.get("title") or "").strip()
    derived_property_title = _property_result_title_display(property_url)
    if preview_title and (
        not current_title
        or current_title == property_url
        or (derived_property_title and current_title == derived_property_title)
    ):
        candidate_row["title"] = preview_title
        changed = True
    current_summary = str(candidate_row.get("summary") or "").strip()
    preview_summary = str(cached_preview.get("summary") or "").strip()
    if preview_summary and not current_summary:
        candidate_row["summary"] = preview_summary
        changed = True

    for key in ("listing_id", "media_urls_json", "floorplan_urls_json", "source_virtual_tour_url", "panorama_source"):
        value = cached_preview.get(key)
        if _property_fact_value_is_weak(candidate_row.get(key)) and not _property_fact_value_is_weak(value):
            candidate_row[key] = list(value) if isinstance(value, tuple) else dict(value) if isinstance(value, dict) else value
            changed = True

    safe_source_virtual_tour_url = _property_candidate_source_virtual_tour_url(candidate_row, facts=merged_facts)
    if str(merged_facts.get("source_virtual_tour_url") or "").strip() != safe_source_virtual_tour_url:
        merged_facts["source_virtual_tour_url"] = safe_source_virtual_tour_url
        changed = True
    if str(candidate_row.get("source_virtual_tour_url") or "").strip() != safe_source_virtual_tour_url:
        candidate_row["source_virtual_tour_url"] = safe_source_virtual_tour_url
        changed = True

    if merged_facts:
        if changed or not isinstance(candidate_row.get("property_facts"), dict):
            candidate_row["property_facts"] = dict(merged_facts)
        if changed or not isinstance(candidate_row.get("property_facts_json"), dict):
            candidate_row["property_facts_json"] = dict(merged_facts)

    for key in ("has_floorplan", "floorplan_count", "has_360", "media_count"):
        value = merged_facts.get(key)
        if _property_fact_value_is_weak(candidate_row.get(key)) and not _property_fact_value_is_weak(value):
            candidate_row[key] = value

    return candidate_row


def _propertyquarry_preview_cache_index(
    product: object,
    *,
    limit: int = 4000,
) -> dict[str, dict[str, object]]:
    preview_cache_index_fn = getattr(product, "_property_public_preview_cache_index", None)
    if not callable(preview_cache_index_fn):
        return {}
    try:
        loaded = preview_cache_index_fn(limit=limit)
    except TypeError:
        loaded = preview_cache_index_fn()
    except Exception:
        return {}
    if isinstance(loaded, dict):
        return loaded
    try:
        return dict(loaded or {})
    except Exception:
        return {}


def _propertyquarry_preview_cache_scan_limit_for_run(run_payload: dict[str, object]) -> int:
    summary = dict(run_payload.get("summary") or {}) if isinstance(run_payload.get("summary"), dict) else {}
    property_urls: set[str] = set()
    candidate_lists: list[object] = [
        summary.get("ranked_candidates"),
        summary.get("results"),
        summary.get("top_candidates"),
    ]
    for source in list(summary.get("sources") or []):
        if isinstance(source, dict):
            candidate_lists.extend((source.get("top_candidates"), source.get("research_candidates")))
    for candidate_list in candidate_lists:
        if not isinstance(candidate_list, list):
            continue
        for candidate in candidate_list:
            if not isinstance(candidate, dict):
                continue
            property_url = urllib.parse.urldefrag(str(candidate.get("property_url") or "").strip())[0]
            if property_url:
                property_urls.add(property_url)
    estimated_candidates = max(len(property_urls), 1)
    return max(256, min(1024, estimated_candidates * 8))


def _propertyquarry_backfill_run_cached_preview_candidates(
    *,
    product: object,
    run_payload: dict[str, object],
) -> dict[str, object]:
    if not isinstance(run_payload, dict) or not run_payload:
        return run_payload
    summary = dict(run_payload.get("summary") or {}) if isinstance(run_payload.get("summary"), dict) else {}
    if not summary:
        return run_payload

    preview_lookup_fn = getattr(product, "_property_public_preview_cache_lookup", None)
    if not callable(preview_lookup_fn):
        return run_payload

    preview_cache_index = _propertyquarry_preview_cache_index(
        product,
        limit=_propertyquarry_preview_cache_scan_limit_for_run(run_payload),
    )
    if not preview_cache_index:
        return run_payload

    changed = False

    def _backfill_candidate_list(items: object) -> list[object]:
        nonlocal changed
        rows: list[object] = []
        for item in list(items or []):
            updated = _propertyquarry_backfill_candidate_from_cached_preview(
                candidate=item,
                preview_cache_index=preview_cache_index,
                preview_lookup=preview_lookup_fn,
            )
            if updated != item:
                changed = True
            rows.append(updated)
        return rows

    if "ranked_candidates" in summary:
        summary["ranked_candidates"] = _backfill_candidate_list(summary.get("ranked_candidates"))
    for key in ("results", "top_candidates"):
        if key in summary:
            summary[key] = _backfill_candidate_list(summary.get(key))

    sources: list[dict[str, object]] = []
    for source in list(summary.get("sources") or []):
        if not isinstance(source, dict):
            sources.append(source)
            continue
        source_row = dict(source)
        for key in ("top_candidates", "research_candidates"):
            if key in source_row:
                source_row[key] = _backfill_candidate_list(source_row.get(key))
        sources.append(source_row)
    if "sources" in summary:
        summary["sources"] = sources

    if not changed:
        return run_payload
    normalized_run = dict(run_payload)
    normalized_run["summary"] = summary
    return normalized_run


def _propertyquarry_prepare_run_payload(
    *,
    product: object,
    run_payload: dict[str, object],
    backfill_cached_previews: bool = True,
    principal_id: str = "",
) -> dict[str, object]:
    normalized_run = normalize_property_search_run_snapshot(
        _propertyquarry_normalize_run_public_tour_targets(
            run_payload,
            principal_id=principal_id,
        )
    )
    if backfill_cached_previews:
        normalized_run = _propertyquarry_backfill_run_cached_preview_candidates(product=product, run_payload=normalized_run)
    summary = dict(normalized_run.get("summary") or {}) if isinstance(normalized_run.get("summary"), dict) else {}
    if not summary:
        return normalized_run
    normalized_run_id = str(normalized_run.get("run_id") or summary.get("run_id") or "").strip()
    preferences = (
        dict(normalized_run.get("property_search_preferences") or {})
        if isinstance(normalized_run.get("property_search_preferences"), dict)
        else {}
    )

    def _normalize_candidate_list(items: object, *, default_source_label: str = "") -> list[object]:
        rows: list[object] = []
        for item in list(items or []):
            if not isinstance(item, dict):
                rows.append(item)
                continue
            source_seed = str(item.get("source_label") or default_source_label or "").strip()
            candidate_for_identity = dict(item)
            if source_seed and not str(candidate_for_identity.get("source_label") or "").strip():
                candidate_for_identity["source_label"] = source_seed
            stable_candidate_ref = _property_candidate_ref(candidate_for_identity)
            normalized_candidate = _property_customer_candidate_summary(candidate_for_identity, preferences=preferences)
            if isinstance(normalized_candidate, dict) and stable_candidate_ref:
                normalized_candidate.setdefault("candidate_ref", stable_candidate_ref)
            if isinstance(normalized_candidate, dict) and normalized_run_id:
                packet_href = _propertyquarry_candidate_first_party_packet_href(
                    normalized_candidate,
                    run_id=normalized_run_id,
                )
                if packet_href:
                    normalized_candidate["packet_url"] = packet_href
                    normalized_candidate.setdefault("detail_href", packet_href)
            if isinstance(normalized_candidate, dict):
                curated_diorama_entry = _property_curated_diorama_preview_entry(normalized_candidate)
                if curated_diorama_entry:
                    _property_apply_curated_diorama_preview(
                        normalized_candidate,
                        entry=curated_diorama_entry,
                    )
                elif not str(normalized_candidate.get("diorama_preview_url") or "").strip():
                    derived_diorama_preview_url = _property_candidate_diorama_preview_image(normalized_candidate)
                    if derived_diorama_preview_url:
                        normalized_candidate["diorama_preview_url"] = derived_diorama_preview_url
            rows.append(normalized_candidate)
        return rows

    for key in ("ranked_candidates", "results", "top_candidates"):
        if key in summary:
            summary[key] = _normalize_candidate_list(summary.get(key))
    sources: list[dict[str, object]] = []
    for source in list(summary.get("sources") or []):
        if not isinstance(source, dict):
            continue
        source_row = dict(source)
        source_label = str(source_row.get("source_label") or source_row.get("source_url") or "").strip()
        for key in ("top_candidates", "research_candidates"):
            if key in source_row:
                source_row[key] = _normalize_candidate_list(source_row.get(key), default_source_label=source_label)
        sources.append(source_row)
    if "sources" in summary:
        summary["sources"] = sources
    normalized_run["summary"] = summary
    return normalized_run


def _propertyquarry_candidate_needs_detailed_preview(candidate: object) -> bool:
    if not isinstance(candidate, dict):
        return False
    property_url = urllib.parse.urldefrag(str(candidate.get("property_url") or "").strip())[0]
    if not property_url:
        return False
    property_facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
    if isinstance(candidate.get("property_facts_json"), dict):
        property_facts = {**property_facts, **dict(candidate.get("property_facts_json") or {})}
    if _property_candidate_has_floorplan(
        property_url=property_url,
        title=str(candidate.get("title") or "").strip(),
        summary=str(candidate.get("summary") or "").strip(),
        property_facts=property_facts,
        preview=candidate,
    ):
        return False
    media_urls = list(candidate.get("media_urls_json") or property_facts.get("media_urls_json") or [])
    if media_urls:
        return True
    try:
        media_count = int(float(str(candidate.get("media_count") or property_facts.get("media_count") or 0).strip()))
    except Exception:
        media_count = 0
    return media_count > 0


def _propertyquarry_find_run_candidate(
    *,
    run_payload: dict[str, object],
    candidate_ref: str,
) -> dict[str, object] | None:
    normalized_candidate_ref = str(candidate_ref or "").strip()
    if not normalized_candidate_ref:
        return None
    summary = dict(run_payload.get("summary") or {}) if isinstance(run_payload.get("summary"), dict) else {}
    candidate_lists: list[object] = [
        summary.get("ranked_candidates"),
        summary.get("results"),
        summary.get("top_candidates"),
    ]
    for source in list(summary.get("sources") or []):
        if isinstance(source, dict):
            candidate_lists.extend((source.get("top_candidates"), source.get("research_candidates")))
    for candidate_list in candidate_lists:
        if not isinstance(candidate_list, list):
            continue
        for candidate in candidate_list:
            if isinstance(candidate, dict) and _property_candidate_ref(candidate) == normalized_candidate_ref:
                return dict(candidate)
    return None


def _propertyquarry_refresh_candidate_preview_if_needed(
    *,
    product: object,
    candidate: object,
    allow_network: bool = True,
) -> dict[str, object] | object:
    if not isinstance(candidate, dict):
        return candidate
    candidate_row = dict(candidate)
    property_url = urllib.parse.urldefrag(str(candidate_row.get("property_url") or "").strip())[0]
    if not property_url:
        return candidate_row
    preview_lookup_fn = getattr(product, "_property_public_preview_cache_lookup", None)
    preview_store_fn = getattr(product, "_property_public_preview_cache_store", None)
    if not callable(preview_lookup_fn) or not callable(preview_store_fn):
        return candidate_row

    preview_cache_index = _propertyquarry_preview_cache_index(product, limit=256)

    cached_preview: dict[str, object] | None = None
    with contextlib.suppress(Exception):
        preview = preview_lookup_fn(cache_index=preview_cache_index, property_url=property_url)
        if isinstance(preview, dict) and preview:
            cached_preview = dict(preview)
    if cached_preview:
        candidate_row = _propertyquarry_backfill_candidate_from_cached_preview(
            candidate=candidate_row,
            preview_cache_index=preview_cache_index,
            preview_lookup=preview_lookup_fn,
        )
        if not _propertyquarry_candidate_needs_detailed_preview(candidate_row):
            return candidate_row

    if not _propertyquarry_candidate_needs_detailed_preview(candidate_row):
        return candidate_row

    if not allow_network:
        return candidate_row

    with contextlib.suppress(Exception):
        detailed_preview = _property_scout_page_preview_with_timeout(property_url, prefer_fast=False)
        if isinstance(detailed_preview, dict) and detailed_preview:
            preview_store_fn(cache_index=preview_cache_index, property_url=property_url, preview=detailed_preview)
            candidate_row = _propertyquarry_backfill_candidate_from_cached_preview(
                candidate=candidate_row,
                preview_cache_index=preview_cache_index,
                preview_lookup=preview_lookup_fn,
            )
    return candidate_row


def _propertyquarry_refresh_run_candidate_preview_if_needed(
    *,
    product: object,
    run_payload: dict[str, object],
    candidate_ref: str,
    allow_network: bool = True,
    principal_id: str = "",
) -> dict[str, object]:
    normalized_candidate_ref = str(candidate_ref or "").strip()
    if not normalized_candidate_ref:
        return run_payload
    candidate = _propertyquarry_find_run_candidate(run_payload=run_payload, candidate_ref=normalized_candidate_ref)
    if not isinstance(candidate, dict):
        return run_payload
    refreshed_candidate = _propertyquarry_refresh_candidate_preview_if_needed(
        product=product,
        candidate=candidate,
        allow_network=allow_network,
    )
    if refreshed_candidate == candidate or not isinstance(refreshed_candidate, dict):
        return run_payload

    summary = dict(run_payload.get("summary") or {}) if isinstance(run_payload.get("summary"), dict) else {}
    if not summary:
        return run_payload

    changed = False

    def _replace_candidate_list(items: object) -> list[object]:
        nonlocal changed
        rows: list[object] = []
        for item in list(items or []):
            if isinstance(item, dict) and _property_candidate_ref(item) == normalized_candidate_ref:
                replacement = dict(refreshed_candidate)
                if str(item.get("source_label") or "").strip() and not str(replacement.get("source_label") or "").strip():
                    replacement["source_label"] = str(item.get("source_label") or "").strip()
                rows.append(replacement)
                changed = True
            else:
                rows.append(item)
        return rows

    for key in ("ranked_candidates", "results", "top_candidates"):
        if key in summary:
            summary[key] = _replace_candidate_list(summary.get(key))

    sources: list[dict[str, object]] = []
    for source in list(summary.get("sources") or []):
        if not isinstance(source, dict):
            sources.append(source)
            continue
        source_row = dict(source)
        for key in ("top_candidates", "research_candidates"):
            if key in source_row:
                source_row[key] = _replace_candidate_list(source_row.get(key))
        sources.append(source_row)
    if "sources" in summary:
        summary["sources"] = sources

    if not changed:
        return run_payload

    updated_run_payload = dict(run_payload)
    updated_run_payload["summary"] = summary
    return _propertyquarry_prepare_run_payload(
        product=product,
        run_payload=updated_run_payload,
        backfill_cached_previews=False,
        principal_id=principal_id,
    )


def _propertyquarry_example_media_cache_ttl_seconds() -> int:
    raw_value = str(os.environ.get("PROPERTYQUARRY_EXAMPLE_MEDIA_CACHE_TTL_SECONDS") or "300").strip()
    try:
        ttl_seconds = int(raw_value)
    except Exception:
        ttl_seconds = 300
    return max(ttl_seconds, 1)


def _propertyquarry_example_media_cache_bucket(*, now: float | None = None) -> int:
    timestamp = time.time() if now is None else float(now)
    return int(timestamp // _propertyquarry_example_media_cache_ttl_seconds())


def _propertyquarry_example_media_targets_scan(root: Path) -> dict[str, str]:
    if not root.exists() or not root.is_dir():
        return {}

    candidates: list[tuple[int, str, dict[str, str]]] = []
    for bundle_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest_path = bundle_dir / "tour.json"
        if not manifest_path.exists():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        scenes = list(payload.get("scenes") or [])
        has_floorplan_scene = any(
            isinstance(scene, dict) and str(scene.get("role") or "").strip().lower() == "floorplan"
            for scene in scenes
        )
        scene_strategy = str(payload.get("scene_strategy") or "").strip().lower()
        creation_mode = str(payload.get("creation_mode") or "").strip().lower()
        has_presentation_shape = has_floorplan_scene or scene_strategy != "photo_gallery_hosted" or "floorplan" in creation_mode
        if not has_presentation_shape:
            continue
        bundle_tour_href = str(payload.get("public_url") or payload.get("hosted_url") or "").strip()
        if not bundle_tour_href:
            continue
        slug = str(payload.get("slug") or bundle_dir.name).strip()
        # Keep first-party manifest routes relative while verifying them. Turning
        # `/tours/<slug>` into the current request origin can manufacture an
        # absolute HTTP URL in local/runtime probes, which the trusted-origin
        # validator correctly rejects. Bind the route to both manifest and
        # directory identity before any provider helper reads the bundle.
        bundle_tour_reference = bundle_tour_href
        reference_slug = property_tour_hosting._hosted_property_tour_slug_from_url(
            bundle_tour_reference
        )
        if slug != bundle_dir.name or reference_slug != slug:
            continue
        try:
            resolved_tour_href = property_tour_hosting._hosted_property_tour_verified_open_url(
                bundle_tour_reference
            )
        except Exception:
            # Example media is optional; an unreadable or partial bundle must
            # not break the public landing page or expose its filesystem path.
            continue
        tour_label = "3D tour available" if resolved_tour_href else ""
        if not resolved_tour_href:
            continue
        targets = {
            "demo_href": _propertyquarry_public_href(bundle_tour_reference),
            "tour_href": _propertyquarry_public_href(resolved_tour_href),
            "tour_label": tour_label or "3D tour available",
        }

        def _scene_preview_href(*roles: str) -> str:
            normalized_roles = {str(role or "").strip().lower() for role in roles if str(role or "").strip()}
            for scene in scenes:
                if not isinstance(scene, dict):
                    continue
                if str(scene.get("role") or "").strip().lower() not in normalized_roles:
                    continue
                asset_relpath = str(scene.get("asset_relpath") or scene.get("image_url") or "").strip()
                if not asset_relpath or asset_relpath.startswith(("http://", "https://", "/")):
                    continue
                asset_href = property_tour_hosting._hosted_public_tour_asset_url(
                    bundle_tour_reference,
                    slug=slug,
                    asset_relpath=asset_relpath,
                )
                if asset_href:
                    return asset_href
            return ""

        def _preview_asset_is_diorama_like(asset_href: object) -> bool:
            resolved = str(asset_href or "").strip()
            if not resolved:
                return False
            path = urllib.parse.urlparse(resolved).path.lower()
            filename = path.rsplit("/", 1)[-1]
            return any(
                marker in filename
                for marker in (
                    "diorama-preview",
                    "telegram-preview",
                    "scene-01",
                    "thumbnail",
                )
            )

        try:
            diorama_preview_href = _scene_preview_href("diorama")
            preview_image_href = diorama_preview_href
            if not preview_image_href:
                for candidate_preview_href in (
                    _hosted_property_tour_telegram_preview_image_url_for_style(bundle_tour_reference),
                    property_tour_hosting._hosted_property_tour_preview_image_url(bundle_tour_reference),
                    _scene_preview_href("photo", "hero", "staging"),
                ):
                    if _preview_asset_is_diorama_like(candidate_preview_href):
                        preview_image_href = candidate_preview_href
                        break
            walkthrough_open_href = property_tour_hosting._hosted_property_tour_walkthrough_open_url(
                bundle_tour_reference
            )
        except Exception:
            continue
        if preview_image_href:
            targets["preview_image_url"] = _propertyquarry_public_href(preview_image_href)
        if walkthrough_open_href:
            targets["walkthrough_href"] = _propertyquarry_public_href(walkthrough_open_href)
            targets["walkthrough_label"] = "Walkthrough available"
        score = 0
        score += 100 if targets.get("walkthrough_href") else 0
        score += 80 if "/control/3dvista" in targets.get("tour_href", "") else 0
        score += 40 if "/control/matterport" in targets.get("tour_href", "") else 0
        score += 30 if "/generated-reconstruction/" in targets.get("tour_href", "") else 0
        candidates.append((score, str(payload.get("display_title") or payload.get("title") or slug), targets))
    if candidates:
        candidates.sort(key=lambda row: (-row[0], row[1]))
        return candidates[0][2]
    return {}


@lru_cache(maxsize=32)
def _propertyquarry_example_media_targets_cached(root_path: str, cache_bucket: int) -> tuple[tuple[str, str], ...]:
    del cache_bucket
    try:
        targets = _propertyquarry_example_media_targets_scan(Path(root_path))
    except OSError:
        # Public-tour media is an optional enhancement. Cache the safe
        # fallback for this short bucket so a bad bundle cannot amplify I/O.
        targets = {}
    return tuple(
        sorted(
            (str(key or "").strip(), str(value or "").strip())
            for key, value in targets.items()
            if str(key or "").strip() and str(value or "").strip()
        )
    )


def _propertyquarry_example_media_targets() -> dict[str, str]:
    if not _public_tours_enabled_for_examples():
        return {}
    if not callable(getattr(property_tour_hosting, "_hosted_property_tour_verified_open_url", None)):
        return {}
    root = Path(str(os.environ.get("EA_PUBLIC_TOUR_DIR") or "/docker/property/state/public_property_tours")).expanduser()
    try:
        resolved_root = str(root.resolve())
    except OSError:
        resolved_root = str(root)
    return dict(
        _propertyquarry_example_media_targets_cached(
            resolved_root,
            _propertyquarry_example_media_cache_bucket(),
        )
    )


def _propertyquarry_example_scope_preview(
    candidate_key: str,
    *,
    title: str,
    fallback_image_path: str,
    image_url: object = "",
) -> dict[str, str]:
    fallback_href = _propertyquarry_public_href(fallback_image_path)
    if fallback_href:
        separator = "&" if "?" in fallback_href else "?"
        fallback_href = f"{fallback_href}{separator}v={_PROPERTYQUARRY_EXAMPLE_SHORTLIST_DIORAMA_VERSION}"
    resolved_image = fallback_href or _propertyquarry_public_href(image_url)
    if not resolved_image:
        return {}
    return {
        "image_url": resolved_image,
        "preview_image_url": resolved_image,
        "thumb_image_url": resolved_image,
        "alt": f"Illustrative diorama of {title}",
        "kind": "diorama",
        "candidate_key": str(candidate_key or "").strip(),
    }


def _propertyquarry_example_shortlist_href(candidate_key: str = "") -> str:
    normalized_key = str(candidate_key or "").strip()
    if not normalized_key:
        return "/app/example/shortlist"
    encoded_key = urllib.parse.quote(normalized_key, safe="")
    return f"/app/example/shortlist?candidate={encoded_key}#{encoded_key}"


def _propertyquarry_fast_ranked_run_href(run_id: str, *, full: bool = False) -> str:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return "/app/shortlist"
    encoded_run_id = urllib.parse.quote(normalized_run_id, safe="")
    return f"/app/shortlist/run/{encoded_run_id}"


def _propertyquarry_example_shortlist_rows() -> list[dict[str, object]]:
    example_media_targets = _propertyquarry_example_media_targets()
    danube_href = _propertyquarry_example_shortlist_href("danube-flats-demo")
    quiet_href = _propertyquarry_example_shortlist_href("quiet-layout-near-transit")
    value_href = _propertyquarry_example_shortlist_href("strong-price-open-risk")
    example_rows: list[dict[str, object]] = [
        {
            "candidate_key": "danube-flats-demo",
            "title": "Danube Flats demo",
            "detail": "Example home with a 3D tour and walkthrough.",
            "score": 84,
            "area_label": "1010 Vienna, 1020 Vienna",
            "price_label": "Premium example",
            "layout_label": "Bright high-rise layout",
            "href": danube_href,
            "detail_href": danube_href,
            "action_href": danube_href,
            "action_label": "Open property",
            "tour_href": example_media_targets.get("tour_href", ""),
            "walkthrough_href": example_media_targets.get("walkthrough_href", ""),
            "tour_label": example_media_targets.get("tour_label", "") if example_media_targets.get("tour_href", "") else "",
            "walkthrough_label": (
                example_media_targets.get("walkthrough_label", "") if example_media_targets.get("walkthrough_href", "") else ""
            ),
            "scope_preview": _propertyquarry_example_scope_preview(
                "danube-flats-demo",
                title="Danube Flats demo",
                fallback_image_path="/static/property/home/example-shortlist-diorama-1.webp",
                image_url=example_media_targets.get("preview_image_url", ""),
            ),
        },
        {
            "candidate_key": "quiet-layout-near-transit",
            "title": "Quiet layout near transit",
            "detail": "Good fit. Parking still unclear.",
            "score": 78,
            "area_label": "1040 Vienna, 1050 Vienna",
            "price_label": "Rent example",
            "layout_label": "Quiet street, transit nearby",
            "href": quiet_href,
            "detail_href": quiet_href,
            "action_href": quiet_href,
            "action_label": "Open property",
            "scope_preview": _propertyquarry_example_scope_preview(
                "quiet-layout-near-transit",
                title="Quiet layout near transit",
                fallback_image_path="/static/property/home/example-shortlist-diorama-2.webp",
            ),
        },
        {
            "candidate_key": "strong-price-open-risk",
            "title": "Strong price, open questions",
            "detail": "Strong price. Check the details.",
            "score": 72,
            "area_label": "1180 Vienna, 1190 Vienna",
            "price_label": "Value example",
            "layout_label": "Good price, more checks needed",
            "href": value_href,
            "detail_href": value_href,
            "action_href": value_href,
            "action_label": "Open property",
            "scope_preview": _propertyquarry_example_scope_preview(
                "strong-price-open-risk",
                title="Strong price, open questions",
                fallback_image_path="/static/property/home/example-shortlist-diorama-3.webp",
            ),
        },
    ]
    return example_rows


def _propertyquarry_payload_is_example_shortlist(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("run_id") or "").strip().lower() == "sample-homes":
        return True
    summary = dict(payload.get("summary") or {}) if isinstance(payload.get("summary"), dict) else {}
    for candidate in _propertyquarry_run_summary_candidates(summary):
        for key in ("packet_url", "detail_href", "review_url"):
            href = str(candidate.get(key) or "").strip()
            if href.startswith("/app/example/shortlist"):
                return True
    return False


def _propertyquarry_is_public_shortlist_entrypoint(run_id: object) -> bool:
    normalized_run_id = str(run_id or "").strip().lower()
    if not normalized_run_id:
        return False
    return normalized_run_id in {
        "public-demo-run",
        "shared-public-run",
        "0a89ead9e0b048288cca22d1aac54fa7",
    }


def _propertyquarry_run_payload_is_public_shortlist_entrypoint(run_payload: object) -> bool:
    if not isinstance(run_payload, dict):
        return False
    return _propertyquarry_is_public_shortlist_entrypoint(run_payload.get("run_id")) or _propertyquarry_payload_is_example_shortlist(
        run_payload
    )


def _propertyquarry_fast_ranked_run_sample_payload() -> dict[str, object]:
    ranked_candidates: list[dict[str, object]] = []
    for index, row in enumerate(_propertyquarry_example_shortlist_rows(), start=1):
        scope_preview = dict(row.get("scope_preview") or {}) if isinstance(row.get("scope_preview"), dict) else {}
        candidate_title = str(row.get("title") or f"Sample home {index}").strip() or f"Sample home {index}"
        preview_url = (
            str(scope_preview.get("preview_image_url") or "").strip()
            or str(scope_preview.get("image_url") or "").strip()
            or str(scope_preview.get("thumb_image_url") or "").strip()
        )
        diorama_alt = str(scope_preview.get("alt") or "").strip() or f"Illustrative diorama of {candidate_title}"
        candidate: dict[str, object] = {
            "candidate_ref": str(row.get("candidate_key") or f"sample-home-{index}").strip() or f"sample-home-{index}",
            "rank": index,
            "title": candidate_title,
            "summary": str(row.get("detail") or "Open the property to review the details.").strip(),
            "fit_summary": str(row.get("detail") or "Open the property to review the details.").strip(),
            "fit_score": int(row.get("score") or 0),
            "ranking_score": int(row.get("score") or 0),
            "packet_url": str(row.get("detail_href") or row.get("href") or "").strip(),
            "detail_href": str(row.get("detail_href") or row.get("href") or "").strip(),
            "location_label": str(row.get("area_label") or "").strip(),
            "price_display": str(row.get("price_label") or "").strip(),
            "diorama_preview_url": preview_url,
            "diorama_alt": diorama_alt,
            "diorama_representation": "illustrative",
            "diorama_scene": {
                "image_url": preview_url,
                "alt": diorama_alt,
                "representation": "illustrative",
            },
            "preview_image_url": preview_url,
            "source_label": "Sample home",
            "property_facts": {
                "postal_name": str(row.get("area_label") or "").strip(),
                "price_display": str(row.get("price_label") or "").strip(),
                "layout_display": str(row.get("layout_label") or "").strip(),
                "preview_image_url": preview_url,
            },
        }
        if str(row.get("tour_href") or "").strip():
            candidate["tour"] = {
                "url": str(row.get("tour_href") or "").strip(),
                "label": str(row.get("tour_label") or "3D tour available").strip() or "3D tour available",
                "detail": "Open the sample 3D tour.",
            }
        if str(row.get("walkthrough_href") or "").strip():
            candidate["flythrough"] = {
                "url": str(row.get("walkthrough_href") or "").strip(),
                "label": str(row.get("walkthrough_label") or "Walkthrough available").strip() or "Walkthrough available",
                "detail": "Open the sample walkthrough.",
            }
        ranked_candidates.append(candidate)
    return {
        "run_id": "sample-homes",
        "status": "completed",
        "summary": {
            "status": "completed",
            "ranked_candidates": ranked_candidates,
            "listing_total": len(ranked_candidates),
            "raw_listing_total": len(ranked_candidates),
            "reviewed_listing_total": len(ranked_candidates),
            "ranked_total": len(ranked_candidates),
        },
    }


def _propertyquarry_run_summary_candidates(summary: dict[str, object]) -> list[dict[str, object]]:
    ranked_candidates = [
        dict(candidate)
        for candidate in list(summary.get("ranked_candidates") or [])
        if isinstance(candidate, dict)
    ]
    if not ranked_candidates:
        for source in list(summary.get("sources") or []):
            if not isinstance(source, dict):
                continue
            source_label = str(source.get("source_label") or source.get("source_url") or "Source").strip()
            for candidate in list(source.get("top_candidates") or []):
                if not isinstance(candidate, dict):
                    continue
                candidate_row = dict(candidate)
                candidate_row.setdefault("source_label", source_label)
                ranked_candidates.append(candidate_row)
    ranked_candidates.sort(
        key=lambda candidate: (
            int(candidate.get("rank") or 9999),
            -float(candidate.get("ranking_score") or candidate.get("fit_score") or 0.0),
        )
    )
    return ranked_candidates


def _propertyquarry_run_scoped_property_href(href: object, *, run_id: str) -> str:
    resolved = str(href or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not resolved or not normalized_run_id or not resolved.startswith("/app/research/"):
        return resolved
    parsed = urllib.parse.urlsplit(resolved)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "run_id" for key, _value in query):
        query.append(("run_id", normalized_run_id))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def _propertyquarry_first_party_property_href(href: object, *, run_id: str) -> str:
    resolved = str(href or "").strip()
    if not resolved:
        return ""
    parsed = urllib.parse.urlsplit(resolved)
    first_party_hosts = {"propertyquarry.com", "www.propertyquarry.com"}
    if parsed.scheme in {"http", "https"}:
        host = str(parsed.netloc or "").strip().lower()
        if host not in first_party_hosts:
            return ""
        resolved = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))
    if not resolved.startswith("/app/research/"):
        return ""
    return _propertyquarry_run_scoped_property_href(resolved, run_id=run_id)


def _propertyquarry_candidate_first_party_packet_href(candidate: dict[str, object], *, run_id: str) -> str:
    for key in ("packet_url", "review_url", "detail_href"):
        href = _propertyquarry_first_party_property_href(candidate.get(key), run_id=run_id)
        if href:
            return href
    candidate_ref = str(candidate.get("candidate_ref") or _property_candidate_ref(candidate)).strip()
    if not candidate_ref:
        return ""
    return _propertyquarry_run_scoped_property_href(
        f"/app/research/{urllib.parse.quote(candidate_ref, safe='')}",
        run_id=run_id,
    )


def _propertyquarry_home_property_href(candidate: dict[str, object], *, run_id: str) -> str:
    packet_href = _propertyquarry_candidate_first_party_packet_href(candidate, run_id=run_id)
    if packet_href:
        return packet_href
    return str(candidate.get("property_url") or "").strip() or _propertyquarry_fast_ranked_run_href(run_id)


@lru_cache(maxsize=1)
def _property_curated_diorama_preview_index() -> dict[str, str]:
    try:
        payload = json.loads(_PROPERTY_CURATED_DIORAMA_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return build_curated_diorama_preview_index(
        payload,
        static_root=_PROPERTY_CURATED_DIORAMA_STATIC_ROOT,
    )


@lru_cache(maxsize=1)
def _property_curated_diorama_entry_index() -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(_PROPERTY_CURATED_DIORAMA_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return build_curated_diorama_entry_index(
        payload,
        static_root=_PROPERTY_CURATED_DIORAMA_STATIC_ROOT,
    )


def _property_curated_diorama_lookup_keys(candidate: dict[str, object]) -> tuple[str, ...]:
    candidate_ref = str(candidate.get("candidate_ref") or _property_candidate_ref(candidate) or "").strip().lower()
    listing_id = str(candidate.get("listing_id") or "").strip().lower()
    property_url = str(
        candidate.get("property_url")
        or candidate.get("listing_url")
        or candidate.get("source_url")
        or ""
    ).strip()
    if not listing_id and property_url:
        listing_match = re.search(r"-(\d{6,})(?:/)?(?:[?#].*)?$", property_url)
        if listing_match:
            listing_id = listing_match.group(1)
    return tuple(
        lookup_key
        for lookup_key in (f"candidate:{candidate_ref}", f"listing:{listing_id}")
        if lookup_key.rsplit(":", 1)[-1]
    )


def _property_curated_diorama_preview_entry(candidate: dict[str, object]) -> dict[str, object]:
    index = _property_curated_diorama_entry_index()
    for lookup_key in _property_curated_diorama_lookup_keys(candidate):
        if lookup_key in index:
            return dict(index[lookup_key])
    return {}


def _property_curated_diorama_preview_image(candidate: dict[str, object]) -> str:
    index = _property_curated_diorama_preview_index()
    for lookup_key in _property_curated_diorama_lookup_keys(candidate):
        if lookup_key in index:
            return index[lookup_key]
    return ""


def _property_curated_diorama_candidate_refs(candidate_ref: object) -> tuple[str, ...]:
    normalized_candidate_ref = str(candidate_ref or "").strip().lower()
    if not normalized_candidate_ref:
        return ()
    entry = _property_curated_diorama_entry_index().get(
        f"candidate:{normalized_candidate_ref}"
    )
    if not isinstance(entry, dict):
        return ()
    candidate_refs = entry.get("candidate_refs")
    if not isinstance(candidate_refs, list):
        return ()
    normalized_refs: list[str] = []
    for raw_candidate_ref in candidate_refs:
        normalized = str(raw_candidate_ref or "").strip().lower()
        if normalized and normalized not in normalized_refs:
            normalized_refs.append(normalized)
    if normalized_candidate_ref not in normalized_refs:
        return ()
    return tuple(normalized_refs)


def _property_resolve_scoped_curated_candidate_ref(
    *,
    requested_candidate_ref: object,
    property_context: dict[str, object],
) -> str:
    """Resolve a governed legacy alias only within the principal-scoped payload."""

    normalized_requested_ref = str(requested_candidate_ref or "").strip()
    if not normalized_requested_ref:
        return ""

    def _scoped_candidate_exists(candidate_ref: str) -> bool:
        if _property_lookup_candidate(
            property_context=property_context,
            candidate_ref=candidate_ref,
        ) is not None:
            return True
        for raw_candidate in list(
            property_context.get("saved_shortlist_candidates") or []
        ):
            if not isinstance(raw_candidate, dict):
                continue
            if _property_constant_text_equal(
                _property_candidate_ref(dict(raw_candidate)).lower(),
                candidate_ref.lower(),
            ):
                return True
        return False

    if _scoped_candidate_exists(normalized_requested_ref):
        return normalized_requested_ref
    for candidate_ref in _property_curated_diorama_candidate_refs(
        normalized_requested_ref
    ):
        if _property_constant_text_equal(
            candidate_ref.lower(),
            normalized_requested_ref.lower(),
        ):
            continue
        if _scoped_candidate_exists(candidate_ref):
            return candidate_ref
    return normalized_requested_ref


def _property_apply_curated_diorama_preview(
    candidate: dict[str, object],
    *,
    entry: dict[str, object],
) -> None:
    asset_url = str(entry.get("asset_url") or "").strip()
    if not asset_url:
        return
    title = _property_result_title_display(
        candidate.get("title")
        or candidate.get("property_url")
        or "this property"
    )
    representation = str(entry.get("representation") or "").strip().lower()
    alt = str(entry.get("alt") or "").strip()
    if not alt:
        alt = f"Illustrative hand-drawn diorama of {title}"
    source_basis = str(entry.get("source_basis") or "").strip()
    truth_boundary = str(entry.get("truth_boundary") or "").strip()
    preview_kind = str(entry.get("preview_kind") or "").strip()
    scene = (
        dict(candidate.get("diorama_scene") or {})
        if isinstance(candidate.get("diorama_scene"), dict)
        else {}
    )
    scene.update(
        {
            "image_url": asset_url,
            "alt": alt,
            "representation": representation,
            "source_basis": source_basis,
            "truth_boundary": truth_boundary,
            "preview_kind": preview_kind,
        }
    )
    candidate["diorama_preview_url"] = asset_url
    candidate["diorama_alt"] = alt
    candidate["diorama_representation"] = representation
    candidate["diorama_scene"] = scene


def _property_candidate_diorama_preview_image(candidate: dict[str, object]) -> str:
    direct_preview = (
        str(candidate.get("diorama_preview_url") or "").strip()
        or str(
            (
                dict(candidate.get("diorama_scene") or {})
                if isinstance(candidate.get("diorama_scene"), dict)
                else {}
            ).get("image_url")
            or ""
        ).strip()
    )
    if direct_preview:
        return direct_preview
    curated_preview = _property_curated_diorama_preview_image(candidate)
    if curated_preview:
        return curated_preview
    style_hint = str(candidate.get("diorama_style_hint") or "").strip()
    for raw_tour_url in (
        candidate.get("generated_reconstruction_url"),
        candidate.get("tour_url"),
        candidate.get("verified_tour_url"),
        candidate.get("open_tour_url"),
    ):
        normalized_tour_url = str(raw_tour_url or "").strip()
        if not normalized_tour_url or _property_hosted_tour_disabled_fallback(normalized_tour_url):
            continue
        for preview_url in (
            _hosted_property_tour_telegram_preview_image_url_for_style(
                normalized_tour_url,
                diorama_style_hint=style_hint,
            ),
            property_tour_hosting._hosted_property_tour_preview_image_url(normalized_tour_url),
        ):
            resolved = str(preview_url or "").strip()
            if resolved:
                return resolved
    return ""


def _propertyquarry_home_result_row(
    candidate: dict[str, object],
    *,
    run_id: str,
    principal_id: str = "",
) -> dict[str, object]:
    facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
    title = _property_result_title_display(candidate.get("title") or candidate.get("property_url") or "Property")
    detail = _shared_clean_property_candidate_detail_copy(
        candidate.get("fit_summary") or candidate.get("summary") or candidate.get("compare_reason") or ""
    )
    if not detail:
        detail = " · ".join(
            part
            for part in (
                str(facts.get("price_display") or facts.get("rent_display") or candidate.get("price_display") or "").strip(),
                str(candidate.get("location_label") or facts.get("postal_name") or facts.get("district") or "").strip(),
            )
            if part
        )
    preview_image = (
        str(candidate.get("diorama_preview_url") or "").strip()
        or str(
            (
                dict(candidate.get("diorama_scene") or {})
                if isinstance(candidate.get("diorama_scene"), dict)
                else {}
            ).get("image_url")
            or ""
        ).strip()
        or str(candidate.get("preview_image_url") or "").strip()
        or str(_property_candidate_preview_image(candidate) or "").strip()
        or str(
            (
                dict(candidate.get("orientation_preview") or {})
                if isinstance(candidate.get("orientation_preview"), dict)
                else {}
            ).get("image_url")
            or ""
        ).strip()
    )
    score_value = candidate.get("fit_score")
    if score_value in (None, "", []):
        score_value = candidate.get("ranking_score")
    try:
        score = max(0, min(100, int(round(float(score_value)))))
    except Exception:
        score = 0
    property_href = _propertyquarry_home_property_href(candidate, run_id=run_id)
    research_media = _property_tour_media_payload(candidate, principal_id=principal_id)
    return {
        "title": title,
        "detail": detail or "Open the property page to review the details.",
        "score": score,
        "href": property_href,
        "detail_href": property_href,
        "action_href": property_href,
        "action_label": "Open property",
        "tour_href": str(research_media.get("primary_href") or "").strip(),
        "tour_label": str(research_media.get("primary_label") or "Open 3D tour").strip() or "Open 3D tour",
        "walkthrough_href": str(research_media.get("walkthrough_href") or "").strip(),
        "walkthrough_label": str(research_media.get("walkthrough_label") or "Open walkthrough").strip() or "Open walkthrough",
        "scope_preview": {
            "image_url": preview_image,
            "alt": f"{title} preview",
        },
    }


def _propertyquarry_home_shortlist_panel(
    *,
    product: object | None,
    principal_id: str,
) -> dict[str, object]:
    example_shortlist = _propertyquarry_example_shortlist_rows()
    normalized_principal_id = str(principal_id or "").strip()
    if not normalized_principal_id:
        return {
            "mode": "example",
            "eyebrow": "Example shortlist",
            "caption": "Tap any row to open the example.",
            "tag": "Example",
            "heading": "Sample homes",
            "count_label": f"{len(example_shortlist)} examples",
            "rows": example_shortlist,
        }
    if product is None:
        return {
            "mode": "empty",
            "eyebrow": "Latest results",
            "caption": "Your finished shortlist appears here after the first completed search.",
            "tag": "Signed in",
            "heading": "Last results",
            "count_label": "No shortlist yet",
            "rows": [],
            "empty_title": "No finished shortlist yet",
            "empty_detail": "Start a search to fill this panel with your latest matched homes.",
            "empty_action_href": "/app/search",
            "empty_action_label": "Open search",
        }

    latest_run = _propertyquarry_latest_shortlist_run_with_results(
        product=product,
        principal_id=normalized_principal_id,
        allow_public_entrypoint_runs=False,
    )
    latest_run_id = str(latest_run.get("run_id") or "").strip()
    if not latest_run_id:
        return {
            "mode": "empty",
            "eyebrow": "Latest results",
            "caption": "Your finished shortlist appears here after the first completed search.",
            "tag": "Signed in",
            "heading": "Last results",
            "count_label": "No shortlist yet",
            "rows": [],
            "empty_title": "No finished shortlist yet",
            "empty_detail": "Start a search to fill this panel with your latest matched homes.",
            "empty_action_href": "/app/search",
            "empty_action_label": "Open search",
        }

    prepared_run = _propertyquarry_prepare_run_payload(
        product=product,
        run_payload=latest_run,
        backfill_cached_previews=False,
        principal_id=normalized_principal_id,
    )
    summary = dict(prepared_run.get("summary") or {}) if isinstance(prepared_run.get("summary"), dict) else {}
    ranked_candidates = _propertyquarry_run_summary_candidates(summary)
    rows = [
        _propertyquarry_home_result_row(
            candidate,
            run_id=latest_run_id,
            principal_id=normalized_principal_id,
        )
        for candidate in ranked_candidates[:3]
    ]
    rows = [row for row in rows if isinstance(row, dict)]
    total_candidates = max(
        len(ranked_candidates),
        int(summary.get("ranked_total") or 0),
    )
    if not rows:
        return {
            "mode": "empty",
            "eyebrow": "Latest results",
            "caption": "Your finished shortlist appears here after the first completed search.",
            "tag": "Signed in",
            "heading": "Last results",
            "count_label": "No shortlist yet",
            "rows": [],
            "empty_title": "No finished shortlist yet",
            "empty_detail": "Start a search to fill this panel with your latest matched homes.",
            "empty_action_href": "/app/search",
            "empty_action_label": "Open search",
        }
    return {
        "mode": "recent",
        "eyebrow": "Latest results",
        "caption": "Open the last finished shortlist.",
        "tag": "Signed in",
        "heading": "Last results",
        "count_label": f"{total_candidates or len(rows)} homes",
        "rows": rows,
        "run_href": _propertyquarry_fast_ranked_run_href(latest_run_id),
    }


def _propertyquarry_latest_shortlist_run_with_results(
    *,
    product: Any,
    principal_id: str,
    allow_public_entrypoint_runs: bool = True,
) -> dict[str, object]:
    run_candidates = _propertyquarry_recent_shortlist_runs(
        product=product,
        principal_id=principal_id,
    )
    result_candidates = [
        row
        for row in run_candidates
        if str(row.get("run_id") or "").strip() and _property_run_payload_has_shortlist_results(row)
    ]
    if not result_candidates:
        return {}
    if allow_public_entrypoint_runs:
        preferred_candidates = [
            row
            for row in result_candidates
            if not _propertyquarry_run_payload_is_public_shortlist_entrypoint(row)
        ]
        return preferred_candidates[0] if preferred_candidates else result_candidates[0]
    preferred_candidates = [
        row
        for row in result_candidates
        if not _propertyquarry_is_public_shortlist_entrypoint(str(row.get("run_id") or "").strip())
    ]
    return preferred_candidates[0] if preferred_candidates else {}


def _propertyquarry_latest_shortlist_results_target(
    *,
    product: Any,
    principal_id: str,
) -> str:
    latest_run = _propertyquarry_latest_shortlist_run_with_results(
        product=product,
        principal_id=principal_id,
    )
    latest_run_id = str(latest_run.get("run_id") or "").strip()
    if not latest_run_id or _propertyquarry_is_public_shortlist_entrypoint(latest_run_id):
        return ""
    return _propertyquarry_fast_ranked_run_href(latest_run_id)


def _propertyquarry_recent_shortlist_runs(
    *,
    product: Any,
    principal_id: str,
) -> list[dict[str, object]]:
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        return []

    def _list_runs(*, hydrate: bool | None) -> list[dict[str, object]]:
        if hydrate is None:
            rows = product.list_property_search_runs(principal_id=normalized_principal, limit=24) or []
        else:
            rows = product.list_property_search_runs(
                principal_id=normalized_principal,
                limit=24,
                hydrate=hydrate,
            ) or []
        return [dict(row) for row in list(rows) if isinstance(row, dict)]

    run_candidates: list[dict[str, object]] = []
    with contextlib.suppress(TypeError):
        run_candidates = _list_runs(hydrate=False)
    if not run_candidates:
        with contextlib.suppress(TypeError):
            run_candidates = _list_runs(hydrate=None)
    return run_candidates


def _propertyquarry_shortlist_run_is_recent(
    *,
    product: Any,
    principal_id: str,
    run_id: str,
) -> bool | None:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id or not str(principal_id or "").strip():
        return None
    run_candidates = _propertyquarry_recent_shortlist_runs(
        product=product,
        principal_id=principal_id,
    )
    if not run_candidates:
        return None
    return any(str(row.get("run_id") or "").strip() == normalized_run_id for row in run_candidates)


def _request_country_code(request: Request) -> str:
    for header in (
        "cf-ipcountry",
        "cloudfront-viewer-country",
        "x-vercel-ip-country",
        "x-country-code",
        "x-geo-country",
        "x-appengine-country",
    ):
        value = str(request.headers.get(header) or "").strip().upper()
        if value:
            return value
    return ""


def _request_is_austrian_ip(request: Request) -> bool:
    return _request_country_code(request) == "AT"


def _id_austria_sign_in_enabled_for_request(request: Request) -> bool:
    return _id_austria_sign_in_enabled() and _request_is_austrian_ip(request)


@router.get("/manifest.webmanifest", response_class=JSONResponse, include_in_schema=False)
def propertyquarry_web_manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "PropertyQuarry",
            "short_name": "PropertyQuarry",
            "description": "A focused property search and decision workspace.",
            "lang": "en",
            "dir": "ltr",
            "id": "/app/search",
            "start_url": "/app/search",
            "scope": "/",
            "display": "standalone",
            "display_override": ["standalone", "minimal-ui", "browser"],
            "background_color": "#f4f0e8",
            "theme_color": "#17211c",
            "orientation": "portrait-primary",
            "categories": ["productivity", "lifestyle"],
            "launch_handler": {"client_mode": "navigate-existing"},
            "prefer_related_applications": False,
            "icons": [
                {
                    "src": "/pwa-icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any maskable",
                },
                {
                    "src": "/pwa-icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": "/pwa-icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                }
            ],
            "shortcuts": [
                {
                    "name": "Search",
                    "short_name": "Search",
                    "description": "Open the property search brief.",
                    "url": "/app/search",
                    "icons": [{"src": "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png"}],
                },
                {
                    "name": "Results",
                    "short_name": "Results",
                    "description": "Open current matching homes.",
                    "url": "/app/properties",
                    "icons": [{"src": "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png"}],
                },
                {
                    "name": "Shortlist",
                    "short_name": "Shortlist",
                    "description": "Open saved shortlisted homes.",
                    "url": "/app/shortlist",
                    "icons": [{"src": "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png"}],
                },
                {
                    "name": "Saved Searches",
                    "short_name": "Saved",
                    "description": "Open saved search automation.",
                    "url": "/app/agents",
                    "icons": [{"src": "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png"}],
                },
            ],
        },
        media_type="application/manifest+json",
    )


@router.get("/pwa-icon.svg", response_class=Response, include_in_schema=False)
def propertyquarry_pwa_icon() -> Response:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="PropertyQuarry"><rect width="512" height="512" rx="96" fill="#17211c"/><path d="M144 336V188l112-64 112 64v148h-74v-92h-76v92z" fill="#f8faf7"/><path d="M171 354h170" stroke="#bd8b2f" stroke-width="24" stroke-linecap="round"/></svg>"""
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/favicon.ico", response_class=Response, include_in_schema=False)
def propertyquarry_favicon() -> Response:
    return propertyquarry_pwa_icon()


@lru_cache(maxsize=2)
def _propertyquarry_pwa_png_icon(size: int) -> bytes:
    from PIL import Image, ImageDraw

    normalized_size = 512 if int(size) >= 512 else 192
    scale = normalized_size / 512

    image = Image.new("RGBA", (normalized_size, normalized_size), "#17211c")
    draw = ImageDraw.Draw(image)
    radius = int(96 * scale)
    draw.rounded_rectangle(
        [0, 0, normalized_size, normalized_size],
        radius=radius,
        fill="#17211c",
    )
    house = [
        (144 * scale, 336 * scale),
        (144 * scale, 188 * scale),
        (256 * scale, 124 * scale),
        (368 * scale, 188 * scale),
        (368 * scale, 336 * scale),
        (294 * scale, 336 * scale),
        (294 * scale, 244 * scale),
        (218 * scale, 244 * scale),
        (218 * scale, 336 * scale),
    ]
    draw.polygon([(int(x), int(y)) for x, y in house], fill="#f8faf7")
    draw.line(
        [(int(171 * scale), int(354 * scale)), (int(341 * scale), int(354 * scale))],
        fill="#bd8b2f",
        width=max(8, int(24 * scale)),
        joint="curve",
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


@router.get("/pwa-icon-{size}.png", response_class=Response, include_in_schema=False)
def propertyquarry_pwa_png_icon(size: int) -> Response:
    if size not in {192, 512}:
        raise HTTPException(status_code=404, detail="pwa_icon_size_not_found")
    return Response(
        content=_propertyquarry_pwa_png_icon(size),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/service-worker.js", response_class=PlainTextResponse, include_in_schema=False)
def propertyquarry_service_worker() -> PlainTextResponse:
    return PlainTextResponse(
        """self.addEventListener('install', () => {
  self.skipWaiting();
});
self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});
self.addEventListener('fetch', () => {
  return;
});
""",
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"},
    )


@lru_cache(maxsize=1)
def _property_country_catalog_snapshot_json() -> str:
    country_rows = [dict(row) for row in property_country_options()]
    country_codes = [
        str(row.get("value") or "").strip()
        for row in country_rows
        if str(row.get("value") or "").strip()
    ]
    payload = {
        "platform_catalog_by_country": {
            country_code: property_provider_options(country_code=country_code)
            for country_code in country_codes
        },
        "platform_defaults_by_country_mode": {
            country_code: {
                mode: list(default_platforms_for_country_listing_mode(country_code, mode))
                for mode in ("rent", "buy")
            }
            for country_code in country_codes
        },
        "evidence_source_catalog_by_country": {
            country_code: property_evidence_source_options(country_code=country_code)
            for country_code in country_codes
        },
        "default_language_by_country": {
            country_code: default_language_for_country(country_code)
            for country_code in country_codes
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _property_country_catalog_snapshot() -> dict[str, object]:
    return dict(json.loads(_property_country_catalog_snapshot_json()))


def _property_customer_scoped_preferences(preferences: dict[str, object]) -> dict[str, object]:
    scoped = dict(preferences or {})
    selected_country = normalize_country_code(scoped.get("country_code"))
    if not is_customer_search_country_code(selected_country):
        removed_platforms = list(scoped.get("selected_platforms") or [])
        scoped["country_code"] = "AT"
        scoped["region_code"] = ""
        scoped["location_query"] = ""
        scoped["selected_platforms"] = []
        scoped["provider_selection_filter_applied"] = True
        scoped["provider_selection_filter_removed"] = removed_platforms
    return scoped


@lru_cache(maxsize=1)
def prewarm_property_search_surface_cache() -> bool:
    template_names = (
        "base_public.html",
        "base_console.html",
        "propertyquarry_home.html",
        "propertyquarry_example_shortlist.html",
        "sign_in.html",
        "pricing_page.html",
        "security_page.html",
        "app/property_decision_workbench.html",
        "app/_property_results_list.html",
        "app/_property_running_panel.html",
        "app/_property_search_agents_panel.html",
        "app/_property_selected_review_panel.html",
        "app/_property_account_panel.html",
        "app/_property_billing_panel.html",
        "app/_property_recent_run_strip.html",
        "app/property_research_detail.html",
        "app/object_detail.html",
    )
    for template_name in template_names:
        with contextlib.suppress(Exception):
            templates.env.get_template(template_name)
    with contextlib.suppress(Exception):
        _property_workbench_css_asset(static_surface=False)
        _property_workbench_css_asset(static_surface=True)
        _property_search_loader_script_asset()
        _propertyquarry_example_shortlist_rows()

    _property_country_catalog_snapshot_json()
    country_rows = [dict(row) for row in property_country_options()]
    country_codes = tuple(
        str(row.get("value") or "").strip()
        for row in country_rows
        if str(row.get("value") or "").strip()
    )
    with contextlib.suppress(Exception):
        from app.api.routes import landing_view_models

        landing_view_models._property_region_catalog_by_country(country_codes)
        landing_view_models._property_market_filter_capabilities_catalog(country_codes)
        landing_view_models._property_location_catalog_by_country_region(country_codes)
    return True


def prewarm_property_search_shell_cache(*, container: AppContainer, principal_id: str = "") -> bool:
    auth_settings = getattr(getattr(container, "settings", None), "auth", None)
    normalized_principal = (
        str(principal_id or "").strip()
        or str(getattr(auth_settings, "default_principal_id", "") or "").strip()
        or "local-user"
    )
    status = container.onboarding.status(principal_id=normalized_principal)
    property_context = _property_console_context(
        container=container,
        principal_id=normalized_principal,
        access_email="",
        status=status,
        surface_mode="search",
        defer_run_hydration=True,
    )
    property_context["surface_mode"] = "search"
    payload = _property_workspace_payload(
        "search",
        status=status,
        property_state=property_context,
    )
    templates.env.get_template("app/property_decision_workbench.html")
    return bool(payload)


def _faq_structured_data(rows: tuple[dict[str, str], ...]) -> dict[str, object]:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": str(row.get("question") or "").strip(),
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": str(row.get("answer") or "").strip(),
                },
            }
            for row in rows
            if str(row.get("question") or "").strip() and str(row.get("answer") or "").strip()
        ],
    }


PUBLIC_GUIDE_WIEN = {
    "kicker": "Guide",
    "title": "Wohnung kaufen in Wien: Checkliste fuer Besichtigung, Kosten und Risiken",
    "summary": "A calm PropertyQuarry guide to the checks that matter before you book a viewing, request documents, or move a flat into the real shortlist.",
    "band": (
        {"title": "What to clarify first", "body": "Price, Betriebskosten, floorplan, heating, and the everyday location read should be visible before emotional momentum takes over."},
        {"title": "What usually blocks good decisions", "body": "Missing floorplans, unclear reserve or operating costs, weak light, hidden noise, and no clear next question for the broker."},
    ),
    "sections": (
        {
            "eyebrow": "Before the viewing",
            "title": "The shortest useful checklist",
            "items": (
                "Request the floorplan and check whether the room layout actually fits the household.",
                "Separate cold rent or purchase price from operating costs and reserve exposure.",
                "Look for heating type, energy certificate, and anything that could change monthly cost.",
                "Check the wider area for daily needs, transit, schools, and obvious noise sources.",
            ),
        },
        {
            "eyebrow": "Ask next",
            "title": "Questions worth sending before you spend more time",
            "items": (
                "Can you send the current floorplan and the latest Betriebskosten breakdown?",
                "What has changed in the building or reserve fund in the last 12 months?",
                "Which rooms face the street and which face the courtyard?",
                "Are there any missing documents that would normally be shared before a viewing?",
            ),
        },
    ),
    "faqs": (
        {"question": "Why start with floorplan and costs?", "answer": "Because these two checks eliminate many false positives before the viewing consumes time."},
        {"question": "Should I trust the listing description alone?", "answer": "No. Listing copy is useful, but the next step should come from visible facts, not just tone."},
    ),
}

PUBLIC_MARKET_VIENNA = {
    "kicker": "Market",
    "title": "Vienna apartment search: what makes a shortlist usable",
    "summary": "A practical overview of how a Vienna search should move from portal clutter to a ranked list that is actually worth reviewing.",
    "band": (
        {"title": "Too many portals is not the real problem", "body": "The main problem is that good and weak homes are mixed together without a clear reason to open one first."},
        {"title": "The right shortlist is smaller than the crawl", "body": "A useful Vienna shortlist should carry fit reasons, missing facts, and the next question, not just tiles and filters."},
    ),
    "sections": (
        {
            "eyebrow": "How to read the market",
            "title": "What separates signal from noise",
            "items": (
                "Treat district fit, daily reachability, and household needs as first-class inputs, not afterthought filters.",
                "Keep missing floorplans visible instead of letting them silently distort the shortlist.",
                "Rank the strongest homes first and keep weaker near-misses available only as deliberate tradeoffs.",
            ),
        },
        {
            "eyebrow": "What to expect",
            "title": "What a premium search surface should show",
            "items": (
                "The strongest homes first.",
                "The one reason each home survived the cut.",
                "The main issue that still needs an answer.",
                "The next-best fallback if this one fails.",
            ),
        },
    ),
    "faqs": (
        {"question": "Why does district scope matter so much in Vienna?", "answer": "Because small district differences can change commute, noise, school fit, and price faster than listing text suggests."},
        {"question": "Why not just widen every search?", "answer": "Because a good shortlist should get sharper first. Broader reach only helps when the tradeoff is explicit."},
    ),
}


def _clean_property_candidate_copy(value: object) -> str:
    return _shared_clean_property_candidate_copy(value)


def _clean_property_candidate_detail_copy(value: object) -> str:
    return _shared_clean_property_candidate_detail_copy(value)


def _property_title_price_fallback(title: object) -> str:
    text = " ".join(str(title or "").split()).strip()
    if not text:
        return ""
    currency_pattern = "|".join(re.escape(code) for code in supported_currency_codes())
    for pattern in (
        r"(€\s?[0-9][0-9\.\s]*(?:,[0-9]{1,2})?\s*,-?)",
        rf"((?:{currency_pattern})\s?[0-9][0-9\.,\s]*)",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            raw = " ".join(str(match.group(1) or "").split()).strip(" ,")
            return _property_research_money_display(raw) or raw
    return ""


def _property_research_title_display(title: object) -> str:
    text = _property_result_title_display(title)
    return text if text and text != "Property" else "Property page"


def _property_research_compact_fact(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    if "under research" in text.lower():
        return ""
    return text

templates.env.globals["clickrank_head_snippet"] = lambda request=None: Markup(
    _clickrank_head_snippet(
        _request_hostname(request),
        _request_path(request),
        consent_granted=analytics_consent.analytics_consent_granted(request),
    )
)
templates.env.globals["rybbit_head_snippet"] = lambda request=None: Markup(_rybbit_head_snippet(request))
templates.env.globals["heyy_live_chat_head_snippet"] = lambda request=None: Markup(_heyy_live_chat_head_snippet(request))


@router.get("/robots.txt", include_in_schema=False, response_class=PlainTextResponse)
def robots_txt() -> PlainTextResponse:
    response = PlainTextResponse(
        "\n".join(
            (
                "User-agent: *",
                "Allow: /",
                "Disallow: /app/",
                "Disallow: /api/",
                "Disallow: /v1/",
                "Disallow: /auth/",
                "Disallow: /admin/",
                "Disallow: /workspace-access/",
                "Sitemap: https://propertyquarry.com/sitemap.xml",
                "",
            )
        )
    )
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response


@router.get("/sitemap.xml", include_in_schema=False, response_class=Response)
def sitemap_xml(request: Request) -> Response:
    brand = request_brand(request)
    base_url = str(brand.get("public_base_url") or "https://propertyquarry.com").strip().rstrip("/")
    urls = [
        "/",
        "/pricing",
        "/security",
        "/privacy",
        "/terms",
        "/support",
        "/imprint",
        "/cookies",
        "/subprocessors",
        "/refunds",
        "/disclaimers",
        "/integrations",
        "/docs",
        "/guides/wohnung-kaufen-wien-checkliste",
        "/markets/vienna",
        "/sign-in",
    ]
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path in urls:
        if path == "/sign-in":
            continue
        body.append(f"  <url><loc>{html.escape(base_url + path)}</loc></url>")
    body.append("</urlset>")
    response = Response("\n".join(body), media_type="application/xml")
    response.headers["X-Robots-Tag"] = "index, follow, max-image-preview:large"
    return response


@router.post("/privacy/analytics-consent", include_in_schema=False)
async def set_public_analytics_consent(request: Request) -> RedirectResponse:
    if not analytics_consent.consent_request_is_same_origin(request):
        raise HTTPException(status_code=403, detail="analytics_consent_origin_invalid")
    content_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="analytics_consent_form_required")
    raw_body = await request.body()
    if not raw_body or len(raw_body) > 4096:
        raise HTTPException(status_code=422, detail="analytics_consent_form_invalid")
    try:
        form = urllib.parse.parse_qs(
            raw_body.decode("utf-8", errors="strict"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=8,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="analytics_consent_form_invalid") from exc
    if any(len(values) != 1 for values in form.values()):
        raise HTTPException(status_code=422, detail="analytics_consent_form_invalid")
    if set(form).difference({"csrf_token", "decision", "return_to"}):
        raise HTTPException(status_code=422, detail="analytics_consent_form_invalid")
    submitted_token = str((form.get("csrf_token") or [""])[0]).strip()
    cookie_token = analytics_consent.consent_csrf_token_for_request(request)
    if (
        not analytics_consent.valid_consent_csrf_token(submitted_token)
        or not cookie_token
        or not hmac.compare_digest(submitted_token, cookie_token)
    ):
        raise HTTPException(status_code=403, detail="analytics_consent_csrf_invalid")
    decision = str((form.get("decision") or [""])[0]).strip().lower()
    if decision not in {"granted", "denied"}:
        raise HTTPException(status_code=422, detail="analytics_consent_decision_invalid")
    if analytics_consent.browser_privacy_signal_enabled(request):
        decision = "denied"
    return_to = _normalize_browser_return_to(
        str((form.get("return_to") or [""])[0]),
        default="/cookies",
    )
    response = RedirectResponse(return_to, status_code=303)
    response.set_cookie(
        analytics_consent.ANALYTICS_CONSENT_COOKIE,
        analytics_consent.ANALYTICS_CONSENT_GRANTED
        if decision == "granted"
        else analytics_consent.ANALYTICS_CONSENT_DENIED,
        **analytics_consent.consent_cookie_kwargs(request),
    )
    response.delete_cookie(
        analytics_consent.ANALYTICS_CONSENT_CSRF_COOKIE,
        path="/",
        secure=analytics_consent.request_uses_secure_scheme(request),
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response



def _expected_api_token(container: AppContainer) -> str:
    return str(container.settings.auth.api_token or "").strip()



def _default_principal_id(container: AppContainer) -> str:
    return str(container.settings.auth.default_principal_id or "").strip() or "local-user"



def _token_required(container: AppContainer) -> bool:
    mode = str(getattr(getattr(container.settings, "runtime", None), "mode", "dev") or "dev").strip().lower() or "dev"
    return mode == "prod" or bool(_expected_api_token(container))



def _form_value(form_data: dict[str, list[str]], key: str, default: str = "") -> str:
    values = form_data.get(key) or []
    return str(values[0] if values else default).strip()



def _form_values(form_data: dict[str, list[str]], key: str) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in (form_data.get(key) or []) if str(value).strip())


def _property_feedback_reference_candidates(candidate_ref: str, candidate: dict[str, object]) -> tuple[str, ...]:
    refs: list[str] = []
    for value in (
        candidate_ref,
        candidate.get("property_ref"),
        candidate.get("listing_id"),
        candidate.get("property_url"),
        candidate.get("review_url"),
    ):
        normalized = str(value or "").strip()
        if normalized and normalized not in refs:
            refs.append(normalized)
    return tuple(refs)


def _property_feedback_summary_has_signal(summary: dict[str, object]) -> bool:
    counts = dict(summary.get("counts") or {}) if isinstance(summary.get("counts"), dict) else {}
    decision_states = (
        dict(summary.get("decision_state_counts") or {})
        if isinstance(summary.get("decision_state_counts"), dict)
        else {}
    )
    household_review = dict(summary.get("household_review") or {}) if isinstance(summary.get("household_review"), dict) else {}
    if any(int(value or 0) > 0 for value in counts.values() if isinstance(value, (int, float)) or str(value or "").isdigit()):
        return True
    if any(int(value or 0) > 0 for value in decision_states.values() if isinstance(value, (int, float)) or str(value or "").isdigit()):
        return True
    for key in ("recent_feedback", "clusters", "risk_signal_candidates"):
        if list(summary.get(key) or []):
            return True
    if list(household_review.get("stakeholders") or []):
        return True
    for key in ("dealbreaker_count", "open_questions_count", "disagreement_count", "household_alignment_score"):
        try:
            if int(summary.get(key) or 0) > 0:
                return True
        except Exception:
            continue
    return False



def _property_lookup_indexed_candidate(
    product: Any,
    *,
    principal_id: str,
    access_email: str = "",
    candidate_ref: str,
    run_id: str = "",
) -> tuple[dict[str, object] | None, str]:
    """Load one bounded packet projection without hydrating its search run."""

    normalized_candidate_ref = str(candidate_ref or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not normalized_candidate_ref:
        return None, ""
    indexed_lookup = getattr(product, "get_property_research_packet_link", None)
    if not callable(indexed_lookup):
        return None, ""
    try:
        indexed_link = indexed_lookup(
            principal_id=principal_id,
            candidate_ref=normalized_candidate_ref,
            account_email=access_email,
        )
    except TypeError:
        try:
            indexed_link = indexed_lookup(
                principal_id=principal_id,
                candidate_ref=normalized_candidate_ref,
            )
        except Exception:
            return None, ""
    except Exception:
        return None, ""
    if not isinstance(indexed_link, dict):
        return None, ""
    indexed_candidate = indexed_link.get("candidate") or indexed_link.get("packet_json")
    if not isinstance(indexed_candidate, dict):
        return None, ""
    candidate = dict(indexed_candidate)
    if str(_property_candidate_ref(candidate) or "").strip() != normalized_candidate_ref:
        return None, ""
    packet_source_run_id = str(candidate.get("packet_source_run_id") or "").strip()
    matched_run_id = str(
        packet_source_run_id
        or indexed_link.get("last_run_id")
        or ""
    ).strip()
    if normalized_run_id and packet_source_run_id != normalized_run_id:
        return None, ""
    return candidate, matched_run_id


def _property_lookup_candidate_across_runs(
    product: Any,
    *,
    principal_id: str,
    access_email: str = "",
    candidate_ref: str,
    run_id: str = "",
    max_runs: int = 12,
) -> tuple[dict[str, object] | None, str]:
    normalized_run_id = str(run_id or "").strip()
    normalized_candidate_ref = str(candidate_ref or "").strip()
    if not normalized_candidate_ref:
        return None, ""
    indexed_clean_miss = False
    indexed_lookup_failed = False
    indexed_coverage_complete = False
    indexed_lookup = getattr(product, "get_property_research_packet_link", None)
    if callable(indexed_lookup):
        indexed_candidate, indexed_run_id = _property_lookup_indexed_candidate(
            product,
            principal_id=principal_id,
            access_email=access_email,
            candidate_ref=normalized_candidate_ref,
            run_id=normalized_run_id,
        )
        if indexed_candidate is not None:
            return indexed_candidate, indexed_run_id
        try:
            indexed_link = indexed_lookup(
                principal_id=principal_id,
                candidate_ref=normalized_candidate_ref,
                account_email=access_email,
            )
        except TypeError:
            try:
                indexed_link = indexed_lookup(
                    principal_id=principal_id,
                    candidate_ref=normalized_candidate_ref,
                )
            except Exception:
                indexed_link = False
                indexed_lookup_failed = True
        except Exception:
            indexed_link = False
            indexed_lookup_failed = True
        if indexed_link is None and not indexed_lookup_failed:
            indexed_clean_miss = True
            coverage_lookup = getattr(
                product,
                "property_research_packet_index_coverage_complete",
                None,
            )
            if callable(coverage_lookup):
                try:
                    indexed_coverage_complete = coverage_lookup() is True
                except Exception:
                    indexed_lookup_failed = True
                    indexed_coverage_complete = False
        elif indexed_link is not None:
            indexed_lookup_failed = True
    run_ids: list[str] = []
    listed_runs: list[dict[str, object]] = []
    compact_listing_supported = False
    incomplete_compact_run_ids: list[str] = []
    complete_compact_run_ids: set[str] = set()
    if normalized_run_id:
        run_ids.append(normalized_run_id)
    try:
        listed_runs = [
            dict(row)
            for row in list(
                product.list_property_search_runs(
                    principal_id=principal_id,
                    limit=max_runs,
                    hydrate=False,
                    account_email=access_email,
                )
                or []
            )
            if isinstance(row, dict)
        ]
        compact_listing_supported = True
    except TypeError:
        try:
            listed_runs = [
                dict(row)
                for row in list(
                    product.list_property_search_runs(
                        principal_id=principal_id,
                        limit=max_runs,
                        hydrate=False,
                    )
                    or []
                )
                if isinstance(row, dict)
            ]
            compact_listing_supported = True
        except TypeError:
            with contextlib.suppress(Exception):
                listed_runs = [
                    dict(row)
                    for row in list(product.list_property_search_runs(principal_id=principal_id, limit=max_runs) or [])
                    if isinstance(row, dict)
                ]
        except Exception:
            pass
    except Exception:
        pass

    for row in listed_runs:
        recent_run_id = str(row.get("run_id") or "").strip()
        if recent_run_id and recent_run_id not in run_ids:
            run_ids.append(recent_run_id)

    if compact_listing_supported and listed_runs:
        for compact_run in listed_runs:
            compact_run_id = str(compact_run.get("run_id") or "").strip()
            try:
                prepared_run = _propertyquarry_prepare_run_payload(
                    product=product,
                    run_payload=compact_run,
                    backfill_cached_previews=False,
                    principal_id=principal_id,
                )
            except Exception:
                if compact_run_id and compact_run_id not in incomplete_compact_run_ids:
                    incomplete_compact_run_ids.append(compact_run_id)
                continue
            candidate = _property_lookup_candidate(
                property_context={"run": prepared_run},
                candidate_ref=normalized_candidate_ref,
            )
            if candidate:
                prepared_summary = (
                    dict(prepared_run.get("summary") or {})
                    if isinstance(prepared_run.get("summary"), dict)
                    else {}
                )
                matched_run_id = str(
                    prepared_run.get("run_id")
                    or prepared_summary.get("run_id")
                    or compact_run.get("run_id")
                    or ""
                ).strip()
                return candidate, matched_run_id
            compact_summary = (
                dict(compact_run.get("summary") or {})
                if isinstance(compact_run.get("summary"), dict)
                else {}
            )
            compact_candidates = compact_summary.get("ranked_candidates")
            projection_complete = False
            if "ranked_total" in compact_summary and isinstance(compact_candidates, (list, tuple)):
                try:
                    ranked_total = int(compact_summary.get("ranked_total") or 0)
                except Exception:
                    ranked_total = -1
                projection_complete = ranked_total >= 0 and len(compact_candidates) >= ranked_total
            if projection_complete and compact_run_id:
                complete_compact_run_ids.add(compact_run_id)
            elif compact_run_id and compact_run_id not in incomplete_compact_run_ids:
                incomplete_compact_run_ids.append(compact_run_id)

        if (
            not normalized_run_id
            and indexed_clean_miss
            and indexed_coverage_complete
            and not indexed_lookup_failed
        ):
            return None, ""

    hydration_limit = min(max(int(max_runs or 0), 1), 3)
    fallback_run_ids: list[str] = []

    def _append_fallback_run_id(value: object) -> None:
        fallback_run_id = str(value or "").strip()
        if fallback_run_id and fallback_run_id not in fallback_run_ids:
            fallback_run_ids.append(fallback_run_id)

    if normalized_run_id and normalized_run_id not in complete_compact_run_ids:
        # Resolve the link's exact run first when it is absent from the compact
        # listing or its projection may be truncated.  A complete compact miss
        # is authoritative and must not trigger redundant full hydration.
        _append_fallback_run_id(normalized_run_id)
    if compact_listing_supported:
        # Compact projections are authoritative only when they explicitly say
        # they contain every ranked candidate.  Unknown/truncated projections
        # retain a small authorized legacy fallback so old links do not become
        # 404s, while avoiding the previous unbounded cross-run hydration.
        for incomplete_run_id in incomplete_compact_run_ids:
            _append_fallback_run_id(incomplete_run_id)
    else:
        # Older service/test doubles do not expose the compact-listing marker.
        # Their explicit run can be stale, so preserve the historical
        # cross-run recovery order while applying the same strict I/O bound.
        for recent_run_id in run_ids:
            _append_fallback_run_id(recent_run_id)
    for row_run_id in fallback_run_ids[:hydration_limit]:
        try:
            run_payload = dict(
                product.get_property_search_run_status(
                    principal_id=principal_id,
                    run_id=row_run_id,
                    account_email=access_email,
                )
                or {}
            )
        except TypeError:
            try:
                run_payload = dict(
                    product.get_property_search_run_status(
                        principal_id=principal_id,
                        run_id=row_run_id,
                    )
                    or {}
                )
            except Exception:
                continue
        except Exception:
            continue
        if not isinstance(run_payload, dict):
            continue
        run_payload = _propertyquarry_prepare_run_payload(
            product=product,
            run_payload=run_payload,
            principal_id=principal_id,
        )
        candidate = _property_lookup_candidate(
            property_context={"run": run_payload},
            candidate_ref=normalized_candidate_ref,
        )
        if candidate:
            return candidate, row_run_id
    return None, ""


def _property_compact_preference_overlay(payload: dict[str, object]) -> dict[str, object]:
    preferences = dict(payload or {})
    for heavy_key in (
        "raw_preferences",
        "saved_shortlist_candidates",
        "search_agents",
        "property_commercial",
        "preference_bundle",
    ):
        preferences.pop(heavy_key, None)
    return preferences


def _property_research_distance_keyword_state_selected(state: object) -> bool:
    normalized_state = str(state or "").strip().casefold()
    return bool(
        normalized_state
        and normalized_state not in {"0", "0.0", "false", "none", "null", "off", "neutral", "any"}
    )


def _property_research_distance_preference_entry_active(key: str, value: object) -> bool:
    normalized_key = str(key or "").strip()
    if not normalized_key:
        return False
    if normalized_key.startswith("max_distance_to_") and normalized_key.endswith("_importance"):
        return bool(
            _normalized_property_distance_importance(
                value,
                default="",
            )
        )
    if normalized_key.startswith("max_distance_to_"):
        try:
            return float(str(value or "").strip()) > 0
        except Exception:
            return False
    if normalized_key.startswith("prefer_") and normalized_key.endswith("_nearby"):
        return _property_research_distance_keyword_state_selected(value)
    if normalized_key in {"keywords", "avoid_keywords"}:
        return bool(str(value or "").strip())
    if normalized_key == "keyword_preferences" and isinstance(value, dict):
        return any(
            str(marker or "").strip() and _property_research_distance_keyword_state_selected(state)
            for marker, state in value.items()
        )
    if normalized_key == "keyword_preferences_json":
        raw_json = str(value or "").strip()
        if not raw_json:
            return False
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            parsed = {}
        return isinstance(parsed, dict) and any(
            str(marker or "").strip() and _property_research_distance_keyword_state_selected(state)
            for marker, state in parsed.items()
        )
    return False


def _property_research_distance_preference_overlay(
    payload: dict[str, object],
    *,
    active_only: bool = False,
) -> dict[str, object]:
    preferences = dict(payload or {})
    overlay: dict[str, object] = {}
    for key, value in preferences.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        if active_only and not _property_research_distance_preference_entry_active(normalized_key, value):
            continue
        if normalized_key.startswith("max_distance_to_"):
            overlay[normalized_key] = (
                _normalized_property_distance_importance(value, default="")
                if normalized_key.endswith("_importance")
                else value
            )
            continue
        if normalized_key.startswith("prefer_") and normalized_key.endswith("_nearby"):
            overlay[normalized_key] = value
            continue
        if normalized_key in {
            "keywords",
            "avoid_keywords",
            "keyword_preferences",
            "keyword_preferences_json",
        }:
            overlay[normalized_key] = value
    return overlay


def _property_research_has_distance_preferences(payload: dict[str, object] | None) -> bool:
    return bool(_property_research_distance_preference_overlay(dict(payload or {}), active_only=True))


def _property_research_location_scope_tokens(*values: object) -> set[str]:
    tokens: set[str] = set()

    def _ingest(raw_value: object) -> None:
        if isinstance(raw_value, (list, tuple, set)):
            for item in raw_value:
                _ingest(item)
            return
        text = str(raw_value or "").strip()
        if not text:
            return
        for part in [segment.strip() for segment in text.split(",") if segment.strip()]:
            normalized_part = re.sub(r"\bwien\b", "vienna", part.casefold())
            for code in re.findall(r"\b\d{4,5}\b", normalized_part):
                tokens.add(f"postal:{code}")
            normalized_text = re.sub(r"[^a-z0-9äöüß]+", "", normalized_part)
            if normalized_text:
                tokens.add(f"text:{normalized_text}")

    for value in values:
        _ingest(value)
    return tokens


def _property_research_timestamp_rank(value: object) -> float:
    raw_value = str(value or "").strip()
    if not raw_value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _property_research_search_agent_overlay_rank(
    *,
    score: int,
    index: int,
    agent: dict[str, object],
    agent_scope_tokens: set[str],
    scope_tokens: set[str],
    scope_overlap: set[str],
) -> tuple[float, ...]:
    exact_scope_match = int(bool(scope_overlap and agent_scope_tokens and scope_overlap == agent_scope_tokens))
    if scope_tokens and agent_scope_tokens:
        scope_delta = abs(len(agent_scope_tokens) - len(scope_tokens))
    else:
        scope_delta = 999
    return (
        float(score),
        float(exact_scope_match),
        float(-scope_delta),
        float(-len(agent_scope_tokens) if agent_scope_tokens else 0),
        _property_research_timestamp_rank(agent.get("last_run_at")),
        _property_research_timestamp_rank(agent.get("next_run_at")),
        float(int(bool(agent.get("is_active")))),
        float(int(bool(agent.get("enabled")))),
        float(-index),
    )


def _property_research_workspace_distance_overlay(
    *,
    workspace_preferences: dict[str, object],
    current_preferences: dict[str, object],
    run_preferences: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object]:
    if _property_research_has_distance_preferences(current_preferences) or _property_research_has_distance_preferences(run_preferences):
        return {}
    overlay = _property_research_distance_preference_overlay(workspace_preferences, active_only=True)
    if not overlay:
        return {}
    facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
    scope_tokens = _property_research_location_scope_tokens(
        run_preferences.get("selected_location_values"),
        run_preferences.get("location_query"),
        current_preferences.get("selected_location_values"),
        current_preferences.get("location_query"),
        candidate.get("location_label"),
        facts.get("postal_name"),
        facts.get("address"),
        facts.get("district"),
        facts.get("source_scope_location"),
    )
    workspace_country_code = normalize_country_code(workspace_preferences.get("country_code") or "")
    expected_country_code = normalize_country_code(
        run_preferences.get("country_code") or current_preferences.get("country_code") or workspace_preferences.get("country_code") or ""
    )
    if expected_country_code and workspace_country_code and workspace_country_code != expected_country_code:
        return {}
    workspace_listing_mode = normalize_listing_mode(workspace_preferences.get("listing_mode") or "")
    expected_listing_mode = normalize_listing_mode(
        run_preferences.get("listing_mode") or current_preferences.get("listing_mode") or workspace_preferences.get("listing_mode") or ""
    )
    if expected_listing_mode and workspace_listing_mode and workspace_listing_mode != expected_listing_mode:
        return {}
    workspace_scope_tokens = _property_research_location_scope_tokens(
        workspace_preferences.get("selected_location_values"),
        workspace_preferences.get("location_query"),
    )
    if scope_tokens and not workspace_scope_tokens:
        return {}
    if scope_tokens and workspace_scope_tokens and not (scope_tokens & workspace_scope_tokens):
        return {}
    return overlay


def _property_research_search_agent_distance_overlay(
    *,
    preferences: dict[str, object],
    run_preferences: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object]:
    if _property_research_has_distance_preferences(preferences) or _property_research_has_distance_preferences(run_preferences):
        return {}
    search_agents = [
        dict(row)
        for row in list(preferences.get("search_agents") or [])
        if isinstance(row, dict)
    ]
    if not search_agents:
        return {}
    facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
    scope_tokens = _property_research_location_scope_tokens(
        run_preferences.get("selected_location_values"),
        run_preferences.get("location_query"),
        preferences.get("selected_location_values"),
        preferences.get("location_query"),
        candidate.get("location_label"),
        facts.get("postal_name"),
        facts.get("address"),
        facts.get("district"),
        facts.get("source_scope_location"),
    )
    expected_country_code = normalize_country_code(
        run_preferences.get("country_code") or preferences.get("country_code") or ""
    )
    expected_listing_mode = normalize_listing_mode(
        run_preferences.get("listing_mode") or preferences.get("listing_mode") or ""
    )
    active_agent_id = str(
        run_preferences.get("active_search_agent_id")
        or preferences.get("active_search_agent_id")
        or ""
    ).strip()
    best_rank: tuple[float, ...] = ()
    best_overlay: dict[str, object] = {}
    for index, raw_agent in enumerate(search_agents):
        agent = dict(raw_agent)
        agent_preferences = (
            dict(agent.get("preferences_json") or {})
            if isinstance(agent.get("preferences_json"), dict)
            else {}
        )
        if not _property_research_has_distance_preferences(agent_preferences):
            continue
        overlay = _property_research_distance_preference_overlay(agent_preferences, active_only=True)
        agent_id = str(agent.get("agent_id") or "").strip()
        agent_country_code = normalize_country_code(
            agent_preferences.get("country_code") or agent.get("country_code") or ""
        )
        if expected_country_code and agent_country_code and agent_country_code != expected_country_code:
            continue
        agent_listing_mode = normalize_listing_mode(
            agent_preferences.get("listing_mode") or agent.get("listing_mode") or ""
        )
        if expected_listing_mode and agent_listing_mode and agent_listing_mode != expected_listing_mode:
            continue
        score = 1
        if agent_country_code and agent_country_code == expected_country_code:
            score += 8
        if agent_listing_mode and agent_listing_mode == expected_listing_mode:
            score += 4
        agent_scope_tokens = _property_research_location_scope_tokens(
            agent_preferences.get("selected_location_values"),
            agent_preferences.get("location_query"),
            agent.get("selected_location_values"),
            agent.get("location_query"),
        )
        scope_overlap = scope_tokens & agent_scope_tokens
        if scope_overlap:
            score += 20 + len(scope_overlap)
        elif scope_tokens and not (active_agent_id and agent_id and agent_id == active_agent_id):
            continue
        if active_agent_id and agent_id and agent_id == active_agent_id:
            score += 100
        if bool(agent.get("is_active")):
            score += 2
        if bool(agent.get("enabled")):
            score += 1
        rank = _property_research_search_agent_overlay_rank(
            score=score,
            index=index,
            agent=agent,
            agent_scope_tokens=agent_scope_tokens,
            scope_tokens=scope_tokens,
            scope_overlap=scope_overlap,
        )
        if rank > best_rank:
            best_rank = rank
            best_overlay = overlay
    return best_overlay


def _property_research_recent_run_distance_overlay(
    *,
    product: Any,
    principal_id: str,
    account_email: str,
    current_preferences: dict[str, object],
    workspace_preferences: dict[str, object],
    run_preferences: dict[str, object],
    candidate: dict[str, object],
    current_run_id: str,
) -> dict[str, object]:
    if (
        _property_research_has_distance_preferences(current_preferences)
        or _property_research_has_distance_preferences(workspace_preferences)
        or _property_research_has_distance_preferences(run_preferences)
    ):
        return {}
    facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
    scope_tokens = _property_research_location_scope_tokens(
        run_preferences.get("selected_location_values"),
        run_preferences.get("location_query"),
        current_preferences.get("selected_location_values"),
        current_preferences.get("location_query"),
        workspace_preferences.get("selected_location_values"),
        workspace_preferences.get("location_query"),
        candidate.get("location_label"),
        facts.get("postal_name"),
        facts.get("address"),
        facts.get("district"),
        facts.get("source_scope_location"),
    )
    if not scope_tokens:
        return {}
    expected_country_code = normalize_country_code(
        run_preferences.get("country_code")
        or current_preferences.get("country_code")
        or workspace_preferences.get("country_code")
        or ""
    )
    expected_listing_mode = normalize_listing_mode(
        run_preferences.get("listing_mode")
        or current_preferences.get("listing_mode")
        or workspace_preferences.get("listing_mode")
        or ""
    )
    current_active_agent_id = str(
        run_preferences.get("active_search_agent_id")
        or current_preferences.get("active_search_agent_id")
        or workspace_preferences.get("active_search_agent_id")
        or ""
    ).strip()
    recent_run_kwargs: dict[str, object] = {
        "principal_id": principal_id,
        "limit": 24,
        "hydrate": False,
        "account_email": account_email,
    }
    recent_runs: object = []
    for _attempt in range(3):
        try:
            recent_runs = product.list_property_search_runs(**recent_run_kwargs)
            break
        except TypeError as exc:
            match = re.search(r"unexpected keyword argument ['\"](?P<keyword>[^'\"]+)['\"]", str(exc))
            unexpected_keyword = str(match.group("keyword") or "").strip() if match else ""
            if unexpected_keyword not in {"account_email", "hydrate"} or unexpected_keyword not in recent_run_kwargs:
                return {}
            recent_run_kwargs.pop(unexpected_keyword, None)
        except Exception:
            return {}
    else:
        return {}
    normalized_current_run_id = str(current_run_id or "").strip()
    best_rank: tuple[float, ...] = ()
    best_overlay: dict[str, object] = {}
    for index, raw_run in enumerate(list(recent_runs or [])):
        if not isinstance(raw_run, dict):
            continue
        run_row = dict(raw_run)
        candidate_run_id = str(run_row.get("run_id") or "").strip()
        if normalized_current_run_id and candidate_run_id == normalized_current_run_id:
            continue
        candidate_preferences = (
            dict(run_row.get("property_search_preferences") or run_row.get("preferences") or {})
            if isinstance(run_row.get("property_search_preferences") or run_row.get("preferences"), dict)
            else {}
        )
        if not candidate_preferences:
            continue
        overlay = _property_research_distance_preference_overlay(candidate_preferences, active_only=True)
        if not overlay:
            continue
        candidate_country_code = normalize_country_code(candidate_preferences.get("country_code") or "")
        if expected_country_code and candidate_country_code and candidate_country_code != expected_country_code:
            continue
        candidate_listing_mode = normalize_listing_mode(candidate_preferences.get("listing_mode") or "")
        if expected_listing_mode and candidate_listing_mode and candidate_listing_mode != expected_listing_mode:
            continue
        candidate_scope_tokens = _property_research_location_scope_tokens(
            candidate_preferences.get("selected_location_values"),
            candidate_preferences.get("location_query"),
        )
        if not candidate_scope_tokens:
            continue
        scope_overlap = scope_tokens & candidate_scope_tokens
        if not scope_overlap:
            continue
        candidate_active_agent_id = str(candidate_preferences.get("active_search_agent_id") or "").strip()
        rank = (
            float(len(scope_overlap)),
            float(int(scope_overlap == scope_tokens)),
            float(int(scope_overlap == scope_tokens == candidate_scope_tokens)),
            float(int(bool(current_active_agent_id and candidate_active_agent_id and current_active_agent_id == candidate_active_agent_id))),
            float(-abs(len(candidate_scope_tokens) - len(scope_tokens))),
            _property_research_timestamp_rank(
                run_row.get("updated_at") or run_row.get("generated_at") or run_row.get("created_at")
            ),
            float(-index),
        )
        if rank > best_rank:
            best_rank = rank
            best_overlay = overlay
    return best_overlay


def _property_lookup_candidate_in_saved_shortlist(
    product: Any,
    *,
    principal_id: str,
    candidate_ref: str,
) -> dict[str, object] | None:
    normalized_candidate_ref = str(candidate_ref or "").strip()
    if not normalized_candidate_ref:
        return None
    try:
        candidates = list(product.list_property_saved_shortlist_candidates(principal_id=principal_id) or [])
    except Exception:
        candidates = []
    for row in candidates:
        if not isinstance(row, dict):
            continue
        candidate = dict(row)
        if _property_candidate_ref(candidate) == normalized_candidate_ref:
            return candidate
    return None


def _property_missing_packet_target(*, run_id: str = "", candidate_ref: str = "") -> str:
    normalized_run_id = str(run_id or "").strip()
    normalized_candidate_ref = str(candidate_ref or "").strip()
    query: dict[str, str] = {"packet_missing": "1"}
    if normalized_run_id:
        query["run_id"] = normalized_run_id
    if normalized_candidate_ref:
        query["missing_candidate_ref"] = normalized_candidate_ref
    return f"/app/shortlist?{urllib.parse.urlencode(query)}#results-list"


def _property_missing_packet_response(
    request: Request,
    *,
    container: AppContainer | None = None,
    principal_id: str = "",
    run_id: str = "",
    candidate_ref: str = "",
    allow_repair: bool = True,
) -> JSONResponse | HTMLResponse:
    normalized_run_id = str(run_id or "").strip()
    normalized_candidate_ref = str(candidate_ref or "").strip()
    target = _property_missing_packet_target(
        run_id=normalized_run_id,
        candidate_ref=normalized_candidate_ref,
    )
    poll_url = str(request.url.path or "").strip() or (
        f"/app/research/{urllib.parse.quote(normalized_candidate_ref, safe='')}"
    )
    if str(request.url.query or "").strip():
        poll_url = f"{poll_url}?{request.url.query}"
    queued_repair: object = (
        _property_queue_missing_research_packet_repair(
            container=container,
            principal_id=principal_id,
            run_id=normalized_run_id,
            candidate_ref=normalized_candidate_ref,
            recovery_url=target,
            include_receipt=True,
        )
        if allow_repair
        else ""
    )
    repair_receipt = dict(queued_repair) if isinstance(queued_repair, dict) else {
        "queue_item_ref": str(queued_repair or "").strip()
    }
    queue_item_ref = str(
        repair_receipt.get("queue_item_ref")
        or repair_receipt.get("human_task_id")
        or ""
    ).strip()
    replacement_run_id = str(repair_receipt.get("replacement_run_id") or "").strip()
    repair_status = str(repair_receipt.get("repair_status") or "").strip().lower()
    if not repair_status:
        repair_status = "queued" if queue_item_ref else ("read_only" if not allow_repair else "needs_rebuild")
    recovery_status = (
        "recovery_running"
        if replacement_run_id
        else ("recovery_queued" if queue_item_ref else "recovery_available")
    )
    message = (
        "PropertyQuarry is rebuilding this property page from the saved search brief."
        if replacement_run_id
        else (
            "PropertyQuarry queued this missing property page for recovery."
            if queue_item_ref
            else "That property page is not available right now. Your search and shortlist are still available."
        )
    )
    payload = {
        "status": recovery_status,
        "code": "property_research_packet_recovery",
        "message": message,
        "run_id": normalized_run_id,
        "candidate_ref": normalized_candidate_ref,
        "redirect_url": target,
        "fallback_url": target,
        "poll_url": poll_url,
        "poll_after_ms": 1600,
        "poll_enabled": bool(allow_repair),
        "repair_status": repair_status,
        "repair_reason": str(repair_receipt.get("reason") or "").strip(),
        "queue_item_ref": queue_item_ref,
        "replacement_run_id": replacement_run_id,
        "replacement_status_url": (
            f"/app/api/signals/property/search/run/{urllib.parse.quote(replacement_run_id, safe='')}"
            if replacement_run_id
            else ""
        ),
        "action_label": "Open shortlist",
    }
    accept_header = str(request.headers.get("accept") or "").lower()
    requested_with = str(request.headers.get("x-requested-with") or "").lower()
    if "application/json" in accept_header or requested_with == "xmlhttprequest":
        response: JSONResponse | HTMLResponse = JSONResponse(payload, status_code=202)
    else:
        response = _render_public_template(
            request,
            "app/property_research_recovery.html",
            page_title="Property page recovery | PropertyQuarry",
            brand=request_brand(request),
            recovery=payload,
            robots_directive="noindex, nofollow, noarchive",
        )
        response.status_code = 202
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


def _property_queue_missing_research_packet_repair(
    *,
    container: AppContainer | None,
    principal_id: str,
    run_id: str,
    candidate_ref: str,
    recovery_url: str,
    include_receipt: bool = False,
) -> str | dict[str, object]:
    normalized_principal = str(principal_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    normalized_candidate_ref = str(candidate_ref or "").strip()
    if container is None or not normalized_principal or not normalized_candidate_ref:
        return {} if include_receipt else ""
    try:
        repair = build_product_service(container)._open_property_provider_repair_task(
            principal_id=normalized_principal,
            property_url=(
                "propertyquarry://research-packet/"
                f"{urllib.parse.quote(normalized_run_id or 'unknown', safe='')}/"
                f"{urllib.parse.quote(normalized_candidate_ref, safe='')}"
            ),
            title=f"Missing property page {normalized_candidate_ref}",
            source_url=str(recovery_url or "").strip(),
            source_label="Property page",
            source_platform="propertyquarry",
            source_family="research_packet",
            filter_key="research_packet_missing",
            diagnostics={
                "failure_class": "research_packet_missing",
                "run_id": normalized_run_id,
                "candidate_ref": normalized_candidate_ref,
                "recovery_url": str(recovery_url or "").strip(),
                "reason": "candidate page was requested but could not be reconstructed from active run, cross-run lookup, or saved shortlist",
            },
            source_ref=f"property-research-packet:{normalized_run_id}:{normalized_candidate_ref}",
            run_id=normalized_run_id,
        )
        queue_item_ref = str(repair.get("queue_item_ref") or repair.get("human_task_id") or "").strip()
        if include_receipt:
            return {
                "queue_item_ref": queue_item_ref,
                "human_task_id": str(repair.get("human_task_id") or "").strip(),
                "repair_status": str(repair.get("repair_status") or repair.get("status") or "").strip(),
                "resolution": str(repair.get("resolution") or "").strip(),
                "reason": str(repair.get("reason") or "").strip(),
                "replacement_run_id": str(repair.get("replacement_run_id") or "").strip(),
            }
        return queue_item_ref
    except Exception:
        return {} if include_receipt else ""


def _principal_for_page(
    *,
    container: AppContainer,
    access_identity: CloudflareAccessIdentity | None,
    request: Request | None = None,
) -> str:
    if access_identity is not None:
        return access_identity.principal_id
    if request is not None:
        propertyquarry_identity_session = _propertyquarry_identity_session_payload(request, container)
        if propertyquarry_identity_session is not None:
            return str(propertyquarry_identity_session.get("principal_id") or "").strip()
        workspace_session = _workspace_session_payload(request, container)
        if workspace_session is not None:
            return str(workspace_session.get("principal_id") or "").strip()
        expected = _expected_api_token(container)
        if expected and hmac.compare_digest(_extract_token(request), expected):
            return _resolved_principal_id(
                request,
                container=container,
                authenticated=True,
                access_identity=None,
            )
        if str(request.headers.get("x-ea-principal-id") or "").strip():
            return _resolved_principal_id(
                request,
                container=container,
                authenticated=False,
                access_identity=None,
            )
    return ""



def _anonymous_onboarding_status() -> dict[str, object]:
    return {
        "principal_id": "",
        "status": "anonymous",
        "workspace": {"name": "PropertyQuarry"},
        "selected_channels": [],
        "privacy": {},
        "assistant_modes": [],
        "featured_domains": [],
        "storage_posture": {},
        "channels": {},
        "brief_preview": {},
        "next_step": "Sign in to start a workspace or view the current one.",
        "onboarding_id": "",
    }



def _load_status(
    *,
    container: AppContainer,
    access_identity: CloudflareAccessIdentity | None,
    request: Request | None = None,
    compact: bool = False,
) -> tuple[str, dict[str, object]]:
    principal_id = _principal_for_page(container=container, access_identity=access_identity, request=request)
    if not principal_id:
        return "", _anonymous_onboarding_status()
    if compact and hasattr(container.onboarding, "compact_status"):
        return principal_id, container.onboarding.compact_status(principal_id=principal_id)
    return principal_id, container.onboarding.status(principal_id=principal_id)


def _landing_public_home_requested(request: Request) -> bool:
    for key in ("home", "show_home", "public_home"):
        value = str(request.query_params.get(key) or "").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
    return False


def _landing_authenticated_principal(
    *,
    container: AppContainer,
    access_identity: CloudflareAccessIdentity | None,
    request: Request,
) -> str:
    if access_identity is not None:
        return str(access_identity.principal_id or "").strip()
    propertyquarry_identity_session = _propertyquarry_identity_session_payload(request, container)
    if propertyquarry_identity_session is not None:
        return str(propertyquarry_identity_session.get("principal_id") or "").strip()
    workspace_session = _workspace_session_payload(request, container)
    if workspace_session is not None:
        return str(workspace_session.get("principal_id") or "").strip()
    expected = _expected_api_token(container)
    if expected and hmac.compare_digest(_extract_token(request), expected):
        return _resolved_principal_id(
            request,
            container=container,
            authenticated=True,
            access_identity=None,
        )
    return ""


def _public_app_base_url(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-host") or "").strip().lower().rstrip(".")
    request_host = str(request.url.hostname or "").strip().lower().rstrip(".")
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").strip() or request.url.scheme
    effective_host = forwarded or request_host
    if effective_host in {"propertyquarry.com", "www.propertyquarry.com"}:
        host = forwarded or request_host
        return f"https://{host}"
    explicit = str(os.environ.get("EA_PUBLIC_APP_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if forwarded:
        forwarded_proto = _first_forwarded_https_or_first_token(forwarded_proto)
    if forwarded:
        return f"{forwarded_proto}://{forwarded}"
    return str(request.base_url).rstrip("/")



def _normalize_return_to_for_logout(*, return_to: str, request: Request) -> str:
    parsed_return_to = urllib.parse.urlparse(return_to)
    if parsed_return_to.scheme and parsed_return_to.netloc:
        return return_to
    if not return_to.startswith("/"):
        return _public_app_base_url(request)
    return f"{_public_app_base_url(request).rstrip('/')}{return_to}"


def _cloudflare_access_logout_url(
    request: Request,
    *,
    team_domain: str,
    return_to: str,
) -> str:
    normalized_team_domain = str(team_domain or "").strip().rstrip("/").lower()
    if "://" in normalized_team_domain:
        parsed_domain = urllib.parse.urlparse(normalized_team_domain)
        normalized_team_domain = str(parsed_domain.netloc or "").strip().lower().rstrip("/")
    if not normalized_team_domain:
        return return_to
    public_return_to = _normalize_return_to_for_logout(return_to=return_to, request=request)
    params = urllib.parse.urlencode({"return_to": public_return_to})
    return f"https://{normalized_team_domain}/cdn-cgi/access/logout?{params}"


def _normalize_browser_return_to(raw: str | None, *, default: str) -> str:
    value = str(raw or "")
    if (
        not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or len(urllib.parse.quote(value, safe="")) > 2048
    ):
        return default
    candidate = value
    for _ in range(4):
        if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
            return default
        if "\\" in candidate or candidate.startswith("//") or not candidate.startswith("/"):
            return default
        parsed = urllib.parse.urlparse(candidate)
        if parsed.scheme or parsed.netloc:
            return default
        decoded = urllib.parse.unquote(candidate)
        if decoded == candidate:
            return value
        candidate = decoded
    # Deeply nested encodings are unnecessary for browser destinations and
    # can conceal a protocol-relative or backslash-based cross-host target.
    return default


def _browser_return_to_with_params(return_to: str, **params: object) -> str:
    normalized = str(return_to or "").strip()
    if not normalized:
        return normalized
    parsed = urllib.parse.urlsplit(normalized)
    query_pairs = list(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None:
            continue
        query_pairs.append((str(key), str(value)))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query_pairs),
            parsed.fragment,
        )
    )


def _first_forwarded_https_or_first_token(raw: str) -> str:
    tokens = [token.strip().lower() for token in str(raw or "").split(",") if token.strip()]
    if "https" in tokens:
        return "https"
    if "wss" in tokens:
        return "wss"
    return tokens[0] if tokens else ""


def _browser_request_uses_secure_scheme(request: Request) -> bool:
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").strip().lower()
    normalized = _first_forwarded_https_or_first_token(forwarded_proto)
    if normalized:
        return normalized in {"https", "wss"}
    return str(request.url.scheme or "").strip().lower() == "https"


def _workspace_session_cookie_kwargs(request: Request, *, expires_at: str = "") -> dict[str, object]:
    host = str(request.headers.get("x-forwarded-host") or request.headers.get("host") or str(request.url.hostname or "") or "").strip()
    host = host.split(",", 1)[0].split(":", 1)[0].strip().lower().rstrip(".")
    domain = None
    if host.endswith(".propertyquarry.com") or host == "propertyquarry.com" or host == "www.propertyquarry.com":
        domain = ".propertyquarry.com"

    kwargs: dict[str, object] = {
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "secure": _browser_request_uses_secure_scheme(request),
    }
    if domain is not None:
        kwargs["domain"] = domain
    normalized_expires_at = str(expires_at or "").strip()
    if not normalized_expires_at:
        return kwargs
    try:
        expires_dt = datetime.fromisoformat(normalized_expires_at)
    except ValueError:
        return kwargs
    if expires_dt.tzinfo is None:
        expires_dt = expires_dt.replace(tzinfo=timezone.utc)
    max_age = max(int((expires_dt - datetime.now(timezone.utc)).total_seconds()), 0)
    kwargs["expires"] = expires_dt
    kwargs["max_age"] = max_age
    return kwargs


def _clear_workspace_session_cookie(response: RedirectResponse, request: Request) -> None:
    base_kwargs = {
        "path": "/",
        "httponly": True,
        "samesite": "lax",
    }
    host = str(request.headers.get("x-forwarded-host") or request.headers.get("host") or str(request.url.hostname or "") or "").strip()
    host = host.split(",", 1)[0].split(":", 1)[0].strip().lower().rstrip(".")
    clear_domains = [None]
    if host.endswith(".propertyquarry.com") or host == "propertyquarry.com" or host == "www.propertyquarry.com":
        clear_domains.append(".propertyquarry.com")

    for secure in (True, False):
        for clear_domain in clear_domains:
            for path in ("/", "/app"):
                response.delete_cookie(
                    "ea_workspace_session",
                    secure=secure,
                    domain=clear_domain,
                    path=path,
                    httponly=base_kwargs["httponly"],
                    samesite=base_kwargs["samesite"],
                )


def _signed_out_marker_cookie_kwargs(request: Request) -> dict[str, object]:
    kwargs = _workspace_session_cookie_kwargs(request)
    kwargs["httponly"] = True
    kwargs["max_age"] = 60 * 60 * 24 * 7
    return kwargs


def _clear_signed_out_marker_cookie(response: RedirectResponse, request: Request) -> None:
    host = str(request.headers.get("x-forwarded-host") or request.headers.get("host") or str(request.url.hostname or "") or "").strip()
    host = host.split(",", 1)[0].split(":", 1)[0].strip().lower().rstrip(".")
    clear_domains = [None]
    if host.endswith(".propertyquarry.com") or host == "propertyquarry.com" or host == "www.propertyquarry.com":
        clear_domains.append(".propertyquarry.com")
    secure = _browser_request_uses_secure_scheme(request)
    for clear_domain in clear_domains:
        for path in ("/", "/app"):
            response.delete_cookie(
                "ea_workspace_signed_out",
                secure=secure,
                domain=clear_domain,
                path=path,
                httponly=True,
                samesite="lax",
            )


def _shared_browser_fields(
    *,
    principal_id: str,
    access_identity: CloudflareAccessIdentity | None,
    container: AppContainer,
) -> str:
    token_field = ""
    if access_identity is None and _token_required(container):
        token_field = """
        <label for=\"api_token\">API token</label>
        <input id=\"api_token\" name=\"api_token\" type=\"password\" placeholder=\"required for browser setup on this host\">
        """
    if access_identity is not None:
        return f"""
        <input type=\"hidden\" name=\"principal_id\" value=\"{html.escape(principal_id)}\">
        {token_field}
        """
    if not browser_principal_override_allowed():
        return f"""
        {token_field}
        <p class=\"helper-note\">This browser can only finish setup for the default workspace on this deployment. Switching workspaces from the browser is disabled here.</p>
        """
    return f"""
    <label for=\"principal_id\">Workspace ID (advanced)</label>
    <input id=\"principal_id\" name=\"principal_id\" value=\"{html.escape(principal_id)}\" required>
    {token_field}
    """



def _browser_form_context(
    *,
    form_data: dict[str, list[str]],
    container: AppContainer,
    access_identity: CloudflareAccessIdentity | None,
) -> str:
    expected = _expected_api_token(container)
    if access_identity is None and _token_required(container):
        api_token = _form_value(form_data, "api_token", "")
        if not expected or not hmac.compare_digest(api_token, expected):
            raise HTTPException(status_code=401, detail="auth_required")
    if access_identity is not None:
        requested = _form_value(form_data, "principal_id", access_identity.principal_id)
        if requested and requested != access_identity.principal_id:
            raise HTTPException(status_code=403, detail="principal_scope_mismatch")
        return access_identity.principal_id
    default_principal = _default_principal_id(container)
    requested = _form_value(form_data, "principal_id", "")
    if browser_principal_override_allowed():
        return requested or default_principal
    if requested and requested != default_principal:
        raise HTTPException(status_code=403, detail="principal_override_not_allowed")
    return default_principal



def _public_context(
    *,
    request: Request,
    current_nav: str,
    page_title: str,
    principal_id: str,
    status: dict[str, object],
    access_identity: CloudflareAccessIdentity | None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    brand = request_brand(request)
    query = request.query_params
    explicit_public_home = (
        str(brand.get("key") or "").strip() == "propertyquarry"
        and _landing_public_home_requested(request)
    )
    signing_in_progress = False
    if explicit_public_home:
        signing_in_progress = False
    elif access_identity is not None or principal_id:
        signing_in_progress = True
    elif str(query.get("signing_in") or query.get("signing") or "").strip().lower() in {"1", "true", "yes"}:
        signing_in_progress = True
    elif str(query.get("code") or "").strip() and str(query.get("state") or "").strip():
        signing_in_progress = True
    public_base_url = str(brand.get("public_base_url") or "").strip().rstrip("/")
    request_path = str(getattr(request.url, "path", "") or "").strip() or "/"
    canonical_path = str((extra or {}).get("canonical_path") or request_path).strip() or "/"
    if not canonical_path.startswith("/"):
        canonical_path = f"/{canonical_path.lstrip('/')}"
    canonical_url = str((extra or {}).get("canonical_url") or "").strip() or f"{public_base_url}{canonical_path}"
    meta_description = str((extra or {}).get("meta_description") or "").strip()
    if not meta_description:
        meta_description = (
            "PropertyQuarry helps renters, buyers, and investors define a brief, rank the strongest homes, "
            "open the right ones, and decide faster."
        )
    og_title = str((extra or {}).get("og_title") or "").strip() or page_title
    og_description = str((extra or {}).get("og_description") or "").strip() or meta_description
    og_type = str((extra or {}).get("og_type") or "").strip() or "website"
    robots_directive = str((extra or {}).get("robots_directive") or "").strip() or "index, follow, max-image-preview:large"
    workspace = dict(status.get("workspace") or {})
    channels = dict(status.get("channels") or {})
    preview = dict(status.get("brief_preview") or {})
    selected_channels = [str(row) for row in (status.get("selected_channels") or []) if str(row).strip()]
    context: dict[str, object] = {
        **_propertyquarry_ui_locale_context(request),
        "page_title": page_title,
        "meta_description": meta_description,
        "canonical_url": canonical_url,
        "og_title": og_title,
        "og_description": og_description,
        "og_type": og_type,
        "robots_directive": robots_directive,
        "structured_data_json": (extra or {}).get("structured_data_json"),
        "brand": brand,
        "public_nav": PUBLIC_NAV,
        "current_nav": current_nav,
        "access_identity": access_identity,
        "principal_id": principal_id,
        "public_signed_in": bool(access_identity is not None or principal_id),
        "status": status,
        "workspace": workspace,
        "privacy": dict(status.get("privacy") or {}),
        "channels": channels,
        "channel_cards": _channel_cards(channels),
        "selected_channels_label": ", ".join(selected_channels) if selected_channels else "Google sign-in recommended",
        "signing_in_progress": signing_in_progress,
        "workspace_mode_label": _humanize(str(workspace.get("mode") or "personal")),
        "brief_headline": str(preview.get("headline") or "Turn your channels into a prioritized day."),
        "first_brief_items": _list_rows(
            preview.get("first_brief_preview") or preview.get("first_brief"),
            (
                "Connect Google sign-in if you want easier return access from the same account.",
                "Keep one reviewable property workflow before widening the channel footprint.",
                "Make approvals and memory rules explicit before automating actions.",
            ),
        ),
        "suggested_actions": _list_rows(
            preview.get("suggested_actions"),
            (
                "Turn the saved brief into a useful shortlist and research loop.",
                "Add more channels only after the first loop already feels useful.",
            ),
        ),
        "trust_notes": _list_rows(
            preview.get("trust_notes"),
            (
                "Each channel says clearly what the assistant can actually do today.",
                "Approvals and workspace memory stay visible product features, not hidden implementation details.",
            ),
        ),
        "top_contacts": _list_rows(preview.get("top_contacts"), ("No contact memory yet.",)),
        "top_themes": _list_rows(preview.get("top_themes"), ("No themes yet.",)),
    }
    if extra:
        context.update(extra)
    return context


def _workspace_plan(container: AppContainer, *, principal_id: str):
    status = container.onboarding.status(principal_id=principal_id)
    workspace = dict(status.get("workspace") or {})
    return workspace_plan_for_mode(str(workspace.get("mode") or "personal"))


def _account_nav_context(*, request: Request, context: RequestContext) -> dict[str, object]:
    if not context.principal_id:
        return {}
    if (
        str(request.cookies.get("ea_workspace_signed_out") or "").strip() == "1"
        and context.auth_source not in {"workspace_access_session", "cloudflare_access", "api_token"}
    ):
        return {}

    brand = request_brand(request)
    account_label = str(context.access_email or "").strip().lower()
    if not account_label:
        account_label = str(context.operator_id or "").strip() or "Account"
    menu_label = str(context.access_email or "").strip()
    if not menu_label:
        menu_label = str(context.operator_id or "").strip() or "Account"
    raw_sign_out_return_to = str(brand.get("public_base_url") or "/").strip()
    parsed_sign_out_return_to = urllib.parse.urlparse(raw_sign_out_return_to)
    sign_out_return_to = "/"
    if parsed_sign_out_return_to.path:
        sign_out_return_to = parsed_sign_out_return_to.path.strip() or "/"
    elif raw_sign_out_return_to.startswith("/"):
        sign_out_return_to = raw_sign_out_return_to
    elif not raw_sign_out_return_to:
        sign_out_return_to = "/"
    elif raw_sign_out_return_to != "/":
        sign_out_return_to = "/"

    run_id = str(request.query_params.get("run_id") or "").strip()

    def _with_run_suffix(path: str) -> str:
        if not run_id:
            return path
        parts = urllib.parse.urlsplit(path)
        query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not any(key == "run_id" for key, _value in query_pairs):
            query_pairs.append(("run_id", run_id))
        return urllib.parse.urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urllib.parse.urlencode(query_pairs),
                parts.fragment,
            )
        )

    if request_brand(request)["key"] == "propertyquarry":
        billing_handoff = _property_brilliant_directories_billing_handoff()
        billing_label, billing_detail = _property_pricing_billing_link_copy(billing_handoff)
        billing_open_href = _property_billing_usable_open_href(billing_handoff)
        if billing_open_href:
            billing_target = billing_open_href
        else:
            billing_target = "/app/billing"
    else:
        billing_target = "/app/billing"
        billing_label = "Billing account"
        billing_detail = "Manage billing from your account."
        if request.url.path == "/app/search":
            billing_target = _property_billing_fallback_href()

    billing_href = billing_target
    if not re.match(r"^https?://", billing_target, flags=re.IGNORECASE):
        billing_href = _with_run_suffix(billing_target)

    return {
        "label": account_label,
        "menu_label": menu_label,
        "profile_href": _with_run_suffix("/app/account#search-defaults"),
        "profile_label": "Search defaults",
        "billing_href": billing_href,
        "billing_label": billing_label,
        "billing_detail": billing_detail,
        "settings_href": _with_run_suffix("/app/account#connected-services"),
        "sign_out_action": "/app/actions/sign-out",
        "sign_out_return_to": sign_out_return_to,
    }



def _console_shell_context(
    *,
    request: Request,
    page_title: str,
    current_nav: str,
    context: RequestContext,
    console_title: str,
    console_summary: str,
    nav_groups: tuple[dict[str, object], ...],
    workspace_label: str,
    cards: list[dict[str, object]],
    stats: list[dict[str, str]],
    console_form: dict[str, object] | None = None,
) -> dict[str, object]:
    brand = request_brand(request)
    return {
        **_propertyquarry_ui_locale_context(request),
        "page_title": page_title,
        "brand": brand,
        "current_nav": current_nav,
        "nav_groups": nav_groups,
        "console_title": console_title,
        "console_summary": console_summary,
        "workspace_label": workspace_label,
        "cards": cards,
        "stats": stats,
        "console_form": console_form or {},
        "principal_id": context.principal_id,
        "access_email": context.access_email,
        "operator_id": context.operator_id,
        "account_nav": _account_nav_context(request=request, context=context),
    }



def _render_public_template(request: Request, template_name: str, **context: Any) -> HTMLResponse:
    context.setdefault("request", request)
    context.setdefault("brand", request_brand(request))
    context.update(_propertyquarry_ui_locale_context(request))
    existing_consent_csrf_token = analytics_consent.consent_csrf_token_for_request(request)
    consent_context = analytics_consent.consent_ui_context(
        request,
        csrf_token=existing_consent_csrf_token,
    )
    created_consent_csrf_token = ""
    if (
        consent_context.get("show_banner") is True
        or consent_context.get("show_settings") is True
    ) and not existing_consent_csrf_token:
        created_consent_csrf_token = analytics_consent.new_consent_csrf_token()
        consent_context = analytics_consent.consent_ui_context(
            request,
            csrf_token=created_consent_csrf_token,
        )
    context["analytics_consent"] = consent_context
    response = templates.TemplateResponse(request, template_name, context)
    if created_consent_csrf_token:
        response.set_cookie(
            analytics_consent.ANALYTICS_CONSENT_CSRF_COOKIE,
            created_consent_csrf_token,
            **analytics_consent.consent_csrf_cookie_kwargs(request),
        )
    if analytics_consent.consent_ui_allowed(request):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"
        vary_values = {
            token.strip()
            for token in str(response.headers.get("Vary") or "").split(",")
            if token.strip()
        }
        vary_values.update({"Cookie", "DNT", "Sec-GPC"})
        response.headers["Vary"] = ", ".join(sorted(vary_values, key=str.lower))
    response.headers["X-Robots-Tag"] = str(context.get("robots_directive") or "index, follow, max-image-preview:large")
    response.headers["Content-Language"] = str(context["document_language"])
    return response


def _render_secure_link_page(
    request: Request,
    *,
    page_title: str,
    current_nav: str,
    link_kicker: str,
    link_title: str,
    link_summary: str,
    link_detail_title: str,
    link_status_label: str,
    link_rows: list[dict[str, str]],
    primary_action_href: str,
    primary_action_label: str,
    primary_action_method: str = "get",
    primary_action_fields: dict[str, str] | None = None,
    secondary_action_href: str = "",
    secondary_action_label: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    response = _render_public_template(
        request,
        "workspace_link.html",
        **_public_context(
            request=request,
            current_nav=current_nav,
            page_title=page_title,
            principal_id="",
            status=_anonymous_onboarding_status(),
            access_identity=None,
            extra={
                "link_kicker": link_kicker,
                "link_title": link_title,
                "link_summary": link_summary,
                "link_detail_title": link_detail_title,
                "link_status_label": link_status_label,
                "link_rows": link_rows,
                "primary_action_href": primary_action_href,
                "primary_action_label": primary_action_label,
                "primary_action_method": str(primary_action_method or "get").strip().lower() or "get",
                "primary_action_fields": dict(primary_action_fields or {}),
                "secondary_action_href": secondary_action_href,
                "secondary_action_label": secondary_action_label,
                "robots_directive": "noindex, nofollow, noarchive, nosnippet",
            },
        ),
    )
    response.status_code = status_code
    return response


def _default_operator_id_for_browser(container: AppContainer, *, principal_id: str) -> str:
    operators = container.orchestrator.list_operator_profiles(principal_id=principal_id, status="active", limit=1)
    if not operators:
        return ""
    return str(operators[0].operator_id or "").strip()


def _app_live_feed(container: AppContainer, *, principal_id: str) -> dict[str, object]:
    approvals = container.orchestrator.list_pending_approvals_for_principal(
        principal_id=principal_id,
        limit=6,
    )
    human_tasks = container.orchestrator.list_human_tasks(
        principal_id=principal_id,
        status="pending",
        limit=6,
    )
    pending_delivery = container.channel_runtime.list_pending_delivery(
        limit=6,
        principal_id=principal_id,
    )
    return {
        "approvals": approvals,
        "human_tasks": human_tasks,
        "pending_delivery": pending_delivery,
    }


def _property_search_platform_catalog() -> tuple[dict[str, str], ...]:
    return tuple(property_provider_options(country_code="AT"))


def _property_account_settings_view_href(view: str, *, run_id: str = "") -> str:
    query_pairs: list[tuple[str, str]] = []
    normalized_run_id = str(run_id or "").strip()
    if normalized_run_id:
        query_pairs.append(("run_id", normalized_run_id))
    query_pairs.append(("settings_view", str(view or "account").strip() or "account"))
    query = urllib.parse.urlencode(query_pairs)
    return f"/app/account?{query}#connected-services" if query else "/app/account#connected-services"


def _property_google_customer_detail(raw_detail: str, *, connected: bool, token_status: str) -> str:
    detail = str(raw_detail or "").strip()
    lowered = detail.lower()
    internal_tokens = (
        "google oauth credentials are not configured",
        "set ea_google_oauth_client_id",
        "set ea_google_oauth_client_secret",
        "set ea_google_oauth_redirect_uri",
        "set ea_google_oauth_state_secret",
        "set ea_provider_secret_key",
    )
    if detail and not any(token in lowered for token in internal_tokens):
        return detail
    if connected and token_status and token_status not in {"active", "unknown"}:
        return "Reconnect Google to keep sign-in and return access working."
    if connected:
        return "Use Google to sign in with the same PropertyQuarry account."
    return "Google opens the same account and creates it if needed."


def _property_google_account_snapshot(
    *,
    container: AppContainer,
    principal_id: str,
    status: dict[str, object],
    run_id: str = "",
) -> dict[str, object]:
    raw_channels = dict(status.get("channels") or {}) if isinstance(status.get("channels"), dict) else {}
    google_channel = dict(raw_channels.get("google") or {}) if isinstance(raw_channels.get("google"), dict) else {}
    google_accounts = sorted(
        google_oauth_service.list_google_accounts(container=container, principal_id=principal_id),
        key=lambda account: (
            str(account.binding.binding_id or "").strip()
            != f"{account.binding.principal_id}:{google_oauth_service.GOOGLE_PROVIDER_KEY}",
            str(account.google_email or "").strip().lower(),
            str(account.binding.binding_id or "").strip(),
        ),
    )
    primary_account = next(
        (
            account
            for account in google_accounts
            if str(account.binding.binding_id or "").strip()
            == f"{account.binding.principal_id}:{google_oauth_service.GOOGLE_PROVIDER_KEY}"
        ),
        google_accounts[0] if google_accounts else None,
    )
    primary_email = str(
        getattr(primary_account, "google_email", "")
        or google_channel.get("connected_account_email")
        or google_channel.get("primary_account_email")
        or google_channel.get("account_email")
        or ""
    ).strip()
    connected_total = len(google_accounts)
    active_total = sum(
        1
        for account in google_accounts
        if str(account.binding.status or "").strip().lower() == "enabled"
        and str(account.token_status or "").strip().lower() != "revoked"
    )
    token_status = str(getattr(primary_account, "token_status", "") or "").strip().lower()
    connected = bool(connected_total or primary_email or str(google_channel.get("status") or "").strip().lower() == "connected")
    if not connected:
        status_label = "Ready to connect"
    elif token_status and token_status not in {"active", "unknown"}:
        status_label = "Needs reconnect"
    else:
        status_label = "Connected"
    detail = _property_google_customer_detail(
        str(google_channel.get("detail") or "").strip(),
        connected=connected,
        token_status=token_status,
    )
    connect_return_to = _property_account_settings_view_href("google", run_id=run_id)
    connect_href = "/app/actions/google/connect?" + urllib.parse.urlencode(
        {"return_to": connect_return_to, "scope_bundle": "identity"}
    )
    if not connected:
        action_label = "Connect Google"
    elif token_status and token_status not in {"active", "unknown"}:
        action_label = "Reconnect Google"
    else:
        action_label = "Connect another"
    account_rows = [
        {
            "email": str(account.google_email or "").strip(),
            "status_label": "Primary" if index == 0 else "Connected",
            "detail": (
                "Ready"
                if str(account.token_status or "").strip().lower() in {"active", "unknown", ""}
                else str(account.token_status or "").strip().replace("_", " ")
            ),
        }
        for index, account in enumerate(google_accounts[:3])
        if str(account.google_email or "").strip()
    ]
    return {
        "connected": connected,
        "primary_email": primary_email,
        "connected_total": connected_total,
        "active_total": active_total,
        "status_label": status_label,
        "detail": detail,
        "action_href": connect_href,
        "action_label": action_label,
        "accounts": account_rows,
    }


def _property_access_link_snapshot(
    *,
    product: Any,
    principal_id: str,
) -> dict[str, object]:
    sessions = [
        dict(item)
        for item in product.list_workspace_access_sessions(principal_id=principal_id, status="", limit=1000)
        if isinstance(item, dict)
    ]
    active_sessions = [
        item for item in sessions if str(item.get("status") or "").strip().lower() == "active"
    ][:50]
    revoked_sessions = [
        item for item in sessions if str(item.get("status") or "").strip().lower() == "revoked"
    ][:20]
    return {
        "active_total": len(active_sessions),
        "revoked_total": len(revoked_sessions),
        "rows": [
            {
                "session_id": str(item.get("session_id") or "").strip(),
                "email": str(item.get("email") or "unknown").strip(),
                "role_label": "Collaborator" if str(item.get("role") or "principal").strip() == "operator" else "Account owner",
                "expires_at": str(item.get("expires_at") or "").strip(),
                "access_url": str(item.get("access_url") or "").strip(),
            }
            for item in active_sessions[:3]
            if str(item.get("session_id") or "").strip()
        ],
    }


def _property_run_scope_snapshot(run_payload: dict[str, object]) -> dict[str, object]:
    payload = dict(run_payload or {})
    summary = dict(payload.get("summary") or {}) if isinstance(payload.get("summary"), dict) else {}
    preferences_json = (
        dict(payload.get("property_search_preferences") or payload.get("preferences") or {})
        if isinstance(payload.get("property_search_preferences") or payload.get("preferences"), dict)
        else {}
    )
    brief = dict(payload.get("brief") or {}) if isinstance(payload.get("brief"), dict) else {}
    selected_platforms: list[str] = []
    for raw_values in (
        preferences_json.get("selected_platforms"),
        payload.get("selected_platforms"),
        summary.get("selected_platforms"),
        brief.get("providers"),
    ):
        if not isinstance(raw_values, (list, tuple, set)):
            continue
        for item in raw_values:
            normalized = normalize_property_platform(item)
            if normalized and normalized != "all" and normalized not in selected_platforms:
                selected_platforms.append(normalized)
    return {
        "country_code": normalize_country_code(preferences_json.get("country_code") or summary.get("country_code") or ""),
        "region_code": str(preferences_json.get("region_code") or summary.get("region_code") or "").strip().lower(),
        "listing_mode": normalize_listing_mode(preferences_json.get("listing_mode") or summary.get("listing_mode") or ""),
        "location_query": str(preferences_json.get("location_query") or summary.get("location_query") or "").strip(),
        "selected_platforms": selected_platforms,
        "agent_id": str(
            payload.get("active_search_agent_id")
            or payload.get("agent_id")
            or preferences_json.get("active_search_agent_id")
            or summary.get("active_search_agent_id")
            or summary.get("agent_id")
            or ""
        ).strip(),
    }


_PROPERTY_RUN_HISTORY_SIGNATURE_KEYS = {
    "avoid_keywords",
    "country_code",
    "custom_location_query",
    "full_region_scope",
    "ganztag_required",
    "include_distressed_sale_signals",
    "investment_research_mode",
    "investment_strategy",
    "keywords",
    "listing_mode",
    "location_query",
    "max_area_m2",
    "max_price_eur",
    "max_rooms",
    "miete_mit_kaufoption",
    "min_area_m2",
    "min_price_eur",
    "min_rooms",
    "property_type",
    "region_code",
    "require_barrier_free",
    "require_energy_certificate",
    "require_floorplan",
    "search_goal",
    "selected_location_values",
    "subsidized_required",
    "wiener_wohnticket_available",
}


def _property_run_history_int(value: object) -> int:
    try:
        return max(0, int(float(str(value or "").strip())))
    except Exception:
        return 0


def _property_run_history_updated_at(run_payload: dict[str, object]) -> float:
    for raw_value in (
        run_payload.get("updated_at"),
        run_payload.get("generated_at"),
        run_payload.get("created_at"),
    ):
        text = str(raw_value or "").strip()
        if not text:
            continue
        normalized = text.replace("Z", "+00:00")
        with contextlib.suppress(Exception):
            return datetime.fromisoformat(normalized).timestamp()
    return 0.0


def _property_run_history_signature_value(value: object, *, depth: int = 0) -> object:
    if depth >= 5:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = re.sub(r"\s+", " ", value).strip()
        return text or None
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for raw_key in sorted(value.keys(), key=lambda item: str(item or "")):
            key = str(raw_key or "").strip()
            if not key:
                continue
            child = _property_run_history_signature_value(value.get(raw_key), depth=depth + 1)
            if child in (None, "", [], {}):
                continue
            normalized[key] = child
        return normalized or None
    if isinstance(value, (list, tuple, set)):
        normalized_items = [
            normalized
            for normalized in (
                _property_run_history_signature_value(item, depth=depth + 1)
                for item in value
            )
            if normalized not in (None, "", [], {})
        ]
        if not normalized_items:
            return None
        if all(not isinstance(item, (dict, list)) for item in normalized_items):
            serialized = sorted({json.dumps(item, sort_keys=True, ensure_ascii=True) for item in normalized_items})
            return [json.loads(item) for item in serialized]
        return normalized_items
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text or None


def _property_run_history_signature(run_payload: dict[str, object]) -> str:
    preferences_json = (
        dict(run_payload.get("property_search_preferences") or run_payload.get("preferences") or {})
        if isinstance(run_payload.get("property_search_preferences") or run_payload.get("preferences"), dict)
        else {}
    )
    signature_seed = {
        key: preferences_json.get(key)
        for key in _PROPERTY_RUN_HISTORY_SIGNATURE_KEYS
        if key in preferences_json
    }
    for scope_key, scope_value in _property_run_scope_snapshot(run_payload).items():
        if scope_key not in signature_seed or signature_seed.get(scope_key) in (None, "", [], {}):
            signature_seed[scope_key] = scope_value
    signature_seed.pop("agent_id", None)
    signature_seed.pop("selected_platforms", None)
    signature_payload = _property_run_history_signature_value(signature_seed) or _property_run_scope_snapshot(run_payload)
    return json.dumps(signature_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _property_run_history_display_sort_key(run_payload: dict[str, object]) -> tuple[int, int, int, int, float]:
    summary = dict(run_payload.get("summary") or {}) if isinstance(run_payload.get("summary"), dict) else {}
    ranked_candidates = [row for row in list(summary.get("ranked_candidates") or []) if isinstance(row, dict)]
    ranked_total = max(
        len(ranked_candidates),
        _property_run_history_int(summary.get("ranked_total")),
        _property_run_history_int(summary.get("ranked_candidate_total")),
        _property_run_history_int(summary.get("results_total")),
        _property_run_history_int(summary.get("survivor_total")),
    )
    visible_total = _property_run_history_int(summary.get("listing_total") or summary.get("raw_listing_total"))
    reviewed_total = _property_run_history_int(summary.get("reviewed_listing_total") or summary.get("scanned_listing_total"))
    return (
        1 if ranked_total > 0 else 0,
        ranked_total,
        max(visible_total, reviewed_total),
        reviewed_total,
        _property_run_history_updated_at(run_payload),
    )


def _property_distinct_recent_search_runs(
    raw_runs: list[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    grouped_runs: dict[str, list[dict[str, object]]] = {}
    group_order: list[str] = []
    for raw_run in raw_runs:
        run_id = str(raw_run.get("run_id") or "").strip()
        if not run_id:
            continue
        signature = _property_run_history_signature(raw_run)
        if signature not in grouped_runs:
            grouped_runs[signature] = []
            group_order.append(signature)
        grouped_runs[signature].append(dict(raw_run))

    distinct_runs: list[dict[str, object]] = []
    for signature in group_order:
        rows = grouped_runs.get(signature) or []
        if not rows:
            continue
        best_row = max(rows, key=_property_run_history_display_sort_key)
        distinct_runs.append(best_row)
        if len(distinct_runs) >= max(int(limit or 0), 1):
            break
    return distinct_runs


def _property_run_matches_saved_brief(
    run_payload: dict[str, object],
    *,
    preferences: dict[str, object],
    selected_platforms: set[str] | list[str] | tuple[str, ...],
    selected_agent_id: str = "",
) -> bool:
    current_country = normalize_country_code(preferences.get("country_code"))
    current_region = str(preferences.get("region_code") or "").strip().lower()
    current_listing_mode = normalize_listing_mode(preferences.get("listing_mode"))
    normalized_selected_platforms = {
        normalize_property_platform(item)
        for item in list(selected_platforms or [])
        if normalize_property_platform(item) and normalize_property_platform(item) != "all"
    }
    if not normalized_selected_platforms:
        normalized_selected_platforms = {
            normalize_property_platform(item)
            for item in list(preferences.get("selected_platforms") or [])
            if normalize_property_platform(item) and normalize_property_platform(item) != "all"
        }
    scope = _property_run_scope_snapshot(run_payload)
    run_country = str(scope.get("country_code") or "").strip().upper()
    run_region = str(scope.get("region_code") or "").strip().lower()
    run_listing_mode = str(scope.get("listing_mode") or "").strip().lower()
    run_selected_platforms = {
        normalize_property_platform(item)
        for item in list(scope.get("selected_platforms") or [])
        if normalize_property_platform(item) and normalize_property_platform(item) != "all"
    }
    run_agent_id = str(scope.get("agent_id") or "").strip()
    normalized_selected_agent_id = str(selected_agent_id or "").strip()
    if normalized_selected_agent_id and run_agent_id and run_agent_id != normalized_selected_agent_id:
        return False
    if current_country and run_country and run_country != current_country:
        return False
    if current_region and run_region and run_region != current_region:
        return False
    if current_listing_mode and run_listing_mode and run_listing_mode != current_listing_mode:
        return False
    if normalized_selected_platforms and run_selected_platforms and not (normalized_selected_platforms & run_selected_platforms):
        return False
    return True


def _property_console_context(
    *,
    container: AppContainer,
    principal_id: str,
    access_email: str = "",
    status: dict[str, object],
    run_id: str = "",
    selected_candidate_ref: str = "",
    selected_agent_id: str = "",
    surface_mode: str = "properties",
    force_recent_runs: bool = False,
    defer_run_hydration: bool = False,
) -> dict[str, object]:
    product = build_product_service(container)
    surface_scope = PropertySurfaceScope.for_section(surface_mode)
    prefer_saved_brief_only = surface_scope.section in {"search", "agents", "alerts", "account", "billing", "settings"}
    wants_run_state = surface_scope.wants_run_state
    wants_recent_runs = surface_scope.wants_recent_runs
    wants_recent_matches = surface_scope.wants_recent_matches
    wants_preference_profile = surface_scope.wants_preference_profile
    wants_learning_summary = surface_scope.wants_learning_summary
    wants_agent_views = surface_scope.wants_agent_views
    raw_property_preferences = dict(status.get("property_search_preferences") or {})
    try:
        merged_preference_seed = flatten_property_search_preferences_snapshot(raw_property_preferences)
    except ValueError:
        # Keep the canonical account fields usable if a malformed legacy raw
        # chain exceeds the bounded migration reader.  In particular, saved
        # agents and shortlist state must never depend on their old duplicate
        # copy inside raw_preferences.
        merged_preference_seed = {
            key: value
            for key, value in raw_property_preferences.items()
            if key != "raw_preferences"
        }
    if isinstance(raw_property_preferences.get("property_commercial"), dict) and not isinstance(
        merged_preference_seed.get("property_commercial"),
        dict,
    ):
        merged_preference_seed["property_commercial"] = dict(raw_property_preferences.get("property_commercial") or {})
    preferences = _property_customer_scoped_preferences(normalize_property_search_preferences(merged_preference_seed))
    selected_country = normalize_country_code(preferences.get("country_code"))
    commercial = property_commercial_snapshot(preferences)
    billing_order_endpoints_by_plan: dict[str, str] = {}
    billing_enabled_plans: list[str] = []
    for paid_plan in ("plus", "agent"):
        if payfunnels_configured(plan_key=paid_plan):
            billing_enabled_plans.append(paid_plan)
            billing_order_endpoints_by_plan[paid_plan] = "/app/api/signals/property/billing/checkout/order"
    default_billing_plan = billing_enabled_plans[0] if billing_enabled_plans else ""
    selected_platforms = {
        str(value or "").strip().lower()
        for value in (preferences.get("selected_platforms") or [])
        if str(value or "").strip()
    }
    selected_platforms = set(
        property_filter_selectable_property_platforms(
            selected_platforms,
            country_code=selected_country,
            listing_mode=normalize_listing_mode(preferences.get("listing_mode")),
            include_distressed_sale_signals=preferences.get("include_distressed_sale_signals"),
        )[0]
    )
    if not selected_platforms:
        selected_platforms = set(
            default_platforms_for_country_listing_mode(
                selected_country,
                preferences.get("listing_mode"),
                property_type=preferences.get("property_type"),
            )
        )
    country_provider_options = [dict(option) for option in property_provider_options(country_code=selected_country)]
    run_payload: dict[str, object] = {}
    raw_requested_run_payload: dict[str, object] = {}
    normalized_run_id = str(run_id or "").strip()
    explicit_run_requested = bool(normalized_run_id)
    raw_recent_search_runs: list[dict[str, object]] = []
    recent_search_runs: list[dict[str, object]] = []
    lightweight_active_run: dict[str, object] = {}
    active_run: dict[str, object] | None = None
    if defer_run_hydration and not normalized_run_id:
        wants_run_state = False
        wants_recent_runs = False
        wants_recent_matches = False
        wants_agent_views = False
    def _current_scope_compatible_run(row: object) -> bool:
        return isinstance(row, dict) and _property_run_matches_saved_brief(
            row,
            preferences=preferences,
            selected_platforms=selected_platforms,
            selected_agent_id=selected_agent_id,
    )
    should_load_recent_runs = (
        (
            wants_recent_runs
            or wants_agent_views
            or (surface_scope.section == "properties" and not normalized_run_id)
        )
        and (
            force_recent_runs
            or not (normalized_run_id and surface_scope.section in {"properties", "shortlist", "research"})
        )
    )
    if should_load_recent_runs:
        hydrate_recent_runs = surface_scope.section not in {"properties", "shortlist", "research", "search"}
        def _load_recent_search_runs() -> list[dict[str, object]]:
            try:
                return [
                    normalize_property_search_run_snapshot(dict(row))
                    for row in product.list_property_search_runs(
                        principal_id=principal_id,
                        limit=24,
                        hydrate=hydrate_recent_runs,
                        account_email=access_email,
                    )
                    if isinstance(row, dict)
                ]
            except TypeError:
                try:
                    return [
                        normalize_property_search_run_snapshot(dict(row))
                        for row in product.list_property_search_runs(
                            principal_id=principal_id,
                            limit=24,
                            hydrate=hydrate_recent_runs,
                        )
                        if isinstance(row, dict)
                    ]
                except TypeError:
                    try:
                        return [
                            normalize_property_search_run_snapshot(dict(row))
                            for row in product.list_property_search_runs(
                                principal_id=principal_id,
                                limit=24,
                            )
                            if isinstance(row, dict)
                        ]
                    except Exception:
                        return []
            except Exception:
                return []

        if not normalized_run_id and surface_scope.section in {"properties", "shortlist", "agents", "alerts"}:
            raw_recent_search_runs = list(_property_first_paint_value(_load_recent_search_runs, []))
        else:
            raw_recent_search_runs = _load_recent_search_runs()
        recent_search_run_limit = 24 if surface_scope.section == "search" else 8
        recent_search_runs = _property_distinct_recent_search_runs(
            raw_recent_search_runs,
            limit=recent_search_run_limit,
        )
    history_run_candidates = raw_recent_search_runs or recent_search_runs
    if wants_run_state and not normalized_run_id:
        terminal_statuses = {"processed", "completed", "completed_partial", "failed", "noop", "cancelled", "not started"}
        if surface_scope.section in {"properties", "shortlist"}:
            with contextlib.suppress(Exception):
                active_run = dict(
                    product.find_active_property_search_run(
                        principal_id=principal_id,
                        limit=8,
                        account_email=access_email,
                    )
                    or {}
                )
            if not active_run:
                with contextlib.suppress(TypeError):
                    active_run = dict(product.find_active_property_search_run(principal_id=principal_id, limit=8) or {})
            if isinstance(active_run, dict) and active_run and not _current_scope_compatible_run(active_run):
                active_run = None
        if (not isinstance(active_run, dict) or not active_run) and surface_scope.section == "properties":
            active_run = next(
                (
                    row
                    for row in history_run_candidates
                    if _current_scope_compatible_run(row)
                    and str(row.get("run_id") or "").strip()
                    and str(
                        row.get("status")
                        or (dict(row.get("summary") or {}) if isinstance(row.get("summary"), dict) else {}).get("status")
                        or ""
                    ).strip().lower()
                    not in terminal_statuses
                ),
                None,
            )
        if (not isinstance(active_run, dict) or not active_run) and surface_scope.section == "properties":
            active_run = next(
                (
                    row
                    for row in history_run_candidates
                    if _current_scope_compatible_run(row)
                    and str(row.get("run_id") or "").strip()
                    and _property_run_payload_has_shortlist_results(row)
                ),
                None,
            )
        if (not isinstance(active_run, dict) or not active_run) and surface_scope.section == "properties":
            active_run = next(
                (
                    row
                    for row in history_run_candidates
                    if _current_scope_compatible_run(row)
                    and str(row.get("run_id") or "").strip()
                    and str(
                        row.get("status")
                        or (dict(row.get("summary") or {}) if isinstance(row.get("summary"), dict) else {}).get("status")
                        or ""
                    ).strip().lower()
                    in {"processed", "completed", "completed_partial"}
                ),
                None,
            )
        if (not isinstance(active_run, dict) or not active_run) and surface_scope.section in {"properties", "research", "agents", "alerts"}:
            active_run = next(
                (
                    row
                    for row in history_run_candidates
                    if _current_scope_compatible_run(row)
                    and str(row.get("run_id") or "").strip()
                    and str(
                        row.get("status")
                        or (dict(row.get("summary") or {}) if isinstance(row.get("summary"), dict) else {}).get("status")
                        or ""
                    ).strip().lower()
                    not in terminal_statuses
                ),
                None,
            )
        if (not isinstance(active_run, dict) or not active_run) and surface_scope.section == "shortlist":
            active_run = next(
                (
                    row
                    for row in history_run_candidates
                    if _current_scope_compatible_run(row)
                    and str(row.get("run_id") or "").strip()
                    and _property_run_payload_has_shortlist_results(row)
                ),
                None,
            )
        if (not isinstance(active_run, dict) or not active_run) and surface_scope.section == "shortlist":
            active_run = next(
                (
                    row
                    for row in history_run_candidates
                    if _current_scope_compatible_run(row)
                    and str(row.get("run_id") or "").strip()
                    and str(
                        row.get("status")
                        or (dict(row.get("summary") or {}) if isinstance(row.get("summary"), dict) else {}).get("status")
                        or ""
                    ).strip().lower()
                    not in terminal_statuses
                ),
                None,
            )
        if isinstance(active_run, dict):
            normalized_run_id = str(active_run.get("run_id") or "").strip()
    if wants_run_state and normalized_run_id:
        active_summary_source = run_payload if run_payload else (active_run if isinstance(active_run, dict) else {})
        active_summary = dict(active_summary_source.get("summary") or {}) if isinstance(active_summary_source.get("summary"), dict) else {}
        should_hydrate_run_status = True
        if surface_scope.section == "search" and not active_summary.get("ranked_candidates"):
            should_hydrate_run_status = (
                not active_summary.get("sources")
                and not active_summary.get("sources_total")
                and not active_summary.get("reviewed_listing_total")
                and not active_summary.get("listing_total")
            )
        if surface_scope.section == "shortlist" and active_summary.get("ranked_candidates"):
            should_hydrate_run_status = False
        if should_hydrate_run_status:
            lightweight_run_status = surface_scope.section in {"shortlist", "research"}
            try:
                run_payload = dict(
                    product.get_property_search_run_status(
                        principal_id=principal_id,
                        run_id=normalized_run_id,
                        lightweight=lightweight_run_status,
                        account_email=access_email,
                    )
                    or {}
                )
            except TypeError:
                try:
                    run_payload = dict(
                        product.get_property_search_run_status(
                            principal_id=principal_id,
                            run_id=normalized_run_id,
                            lightweight=lightweight_run_status,
                        )
                        or {}
                    )
                except TypeError:
                    try:
                        run_payload = dict(
                            product.get_property_search_run_status(
                                principal_id=principal_id,
                                run_id=normalized_run_id,
                            )
                            or {}
                        )
                    except Exception:
                        run_payload = active_run if isinstance(active_run, dict) else {}
                except Exception:
                    run_payload = active_run if isinstance(active_run, dict) else {}
            except Exception:
                run_payload = active_run if isinstance(active_run, dict) else {}
        else:
            run_payload = dict(active_run or run_payload or {})
    if (
        surface_scope.section == "research"
        and normalized_run_id
        and _property_constant_text_equal(
            run_payload.get("run_id"),
            normalized_run_id,
        )
    ):
        # Preserve the already-fetched bounded exact-run projection only inside
        # the server-side context. Research first paint must never retain the
        # full search-run graph merely to resolve one candidate.
        raw_requested_run_payload = dict(run_payload)
    if run_payload and not prefer_saved_brief_only:
        run_preferences_payload = (
            dict(run_payload.get("property_search_preferences") or run_payload.get("preferences") or {})
            if isinstance(run_payload.get("property_search_preferences") or run_payload.get("preferences"), dict)
            else {}
        )
        run_summary_for_preferences = (
            dict(run_payload.get("summary") or {})
            if isinstance(run_payload.get("summary"), dict)
            else {}
        )
        run_brief_is_old_snapshot = bool(
            run_payload.get("brief_preferences_stale")
            or run_payload.get("stale_run_snapshot")
            or run_summary_for_preferences.get("brief_preferences_stale")
            or str(run_summary_for_preferences.get("brief_snapshot_status") or "").strip().lower() == "old_run"
        )
        if run_preferences_payload and not run_brief_is_old_snapshot:
            preferences = _property_customer_scoped_preferences(
                normalize_property_search_preferences(
                    {**preferences, **_property_compact_preference_overlay(run_preferences_payload)}
                )
            )
            selected_country = normalize_country_code(preferences.get("country_code"))
            commercial = property_commercial_snapshot(preferences)
            selected_platforms = {
                str(value or "").strip().lower()
                for value in (preferences.get("selected_platforms") or [])
                if str(value or "").strip()
            }
            selected_platforms = set(
                property_filter_selectable_property_platforms(
                    selected_platforms,
                    country_code=selected_country,
                    listing_mode=normalize_listing_mode(preferences.get("listing_mode")),
                    include_distressed_sale_signals=preferences.get("include_distressed_sale_signals"),
                )[0]
            )
            if not selected_platforms:
                selected_platforms = set(
                    default_platforms_for_country_listing_mode(
                        selected_country,
                        preferences.get("listing_mode"),
                        property_type=preferences.get("property_type"),
                    )
                )
            country_provider_options = [dict(option) for option in property_provider_options(country_code=selected_country)]
    run_payload = _propertyquarry_prepare_run_payload(
        product=product,
        run_payload=run_payload,
        backfill_cached_previews=surface_scope.section != "research",
        principal_id=principal_id,
    )
    if selected_candidate_ref and surface_scope.section in {"properties", "shortlist", "research"}:
        run_payload = _propertyquarry_refresh_run_candidate_preview_if_needed(
            product=product,
            run_payload=run_payload,
            candidate_ref=selected_candidate_ref,
            allow_network=surface_scope.section not in {"research", "shortlist"},
            principal_id=principal_id,
        )
    run_status_value = str(run_payload.get("status") or "").strip().lower()
    # First paint must stay fast; preference fine-tuning loads feedback explicitly.
    enrich_run_candidates_with_feedback = False
    if run_payload and enrich_run_candidates_with_feedback:
        packet_service = build_fliplink_packet_service(container)
        summary = dict(run_payload.get("summary") or {}) if isinstance(run_payload.get("summary"), dict) else {}
        ranked_candidates = [
            dict(row)
            for row in list(summary.get("ranked_candidates") or [])
            if isinstance(row, dict)
        ]
        if ranked_candidates:
            summary["ranked_candidates"] = [
                _property_enrich_candidate_feedback(
                    packet_service=packet_service,
                    principal_id=principal_id,
                    candidate=candidate,
                )
                for candidate in ranked_candidates
            ]
        else:
            sources = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
            for source in sources:
                source_label = str(source.get("source_label") or source.get("source_url") or "Source").strip()
                candidates = [dict(row) for row in list(source.get("top_candidates") or []) if isinstance(row, dict)]
                enriched_candidates: list[dict[str, object]] = []
                for candidate in candidates:
                    candidate.setdefault("source_label", source_label)
                    enriched_candidates.append(
                        _property_enrich_candidate_feedback(
                            packet_service=packet_service,
                            principal_id=principal_id,
                            candidate=candidate,
                        )
                    )
                source["top_candidates"] = enriched_candidates
            summary["sources"] = sources
        run_payload["summary"] = summary
    run_health = build_property_run_health_snapshot(
        run_payload,
        run_summary=dict(run_payload.get("summary") or {}) if isinstance(run_payload.get("summary"), dict) else {},
    ) if wants_run_state else {}

    recent_matches: list[dict[str, object]] = []
    learning_summary: dict[str, object] = {}
    preference_bundle: dict[str, object] = {}
    preference_person_id = str(preferences.get("preference_person_id") or "self").strip() or "self"
    if wants_recent_matches:
        try:
            for handoff in product.list_handoffs(principal_id=principal_id, limit=12, status=None):
                task_type = str(getattr(handoff, "task_type", "") or "").strip()
                if task_type not in {"property_tour_followup", "property_alert_review"}:
                    continue
                hosted_url = str(getattr(handoff, "tour_url", "") or "").strip()
                review_url = str(getattr(handoff, "editor_url", "") or "").strip()
                title = str(getattr(handoff, "summary", "") or "").strip() or str(getattr(handoff, "id", "") or "").strip() or "Property match"
                detail_parts = [
                    str(getattr(handoff, "delivery_reason", "") or "").strip(),
                    str(getattr(handoff, "counterparty", "") or "").strip(),
                    str(getattr(handoff, "blocked_reason", "") or "").strip(),
                ]
                detail = " | ".join(part for part in detail_parts if part) or "Recent property follow-up."
                row: dict[str, object] = {
                    "title": title,
                    "detail": detail,
                    "tag": "Hosted tour" if hosted_url else "Review",
                }
                if hosted_url:
                    row["action_href"] = hosted_url
                    row["action_method"] = "get"
                    row["action_label"] = "Open 3D tour"
                if review_url:
                    if hosted_url:
                        row["secondary_action_href"] = review_url
                        row["secondary_action_method"] = "get"
                        row["secondary_action_label"] = "Review brief"
                    else:
                        row["action_href"] = review_url
                        row["action_method"] = "get"
                        row["action_label"] = "Review brief"
                recent_matches.append(row)
                if len(recent_matches) >= 6:
                    break
        except Exception:
            recent_matches = []
    if wants_preference_profile:
        try:
            preference_bundle = _property_account_preference_bundle(
                product=product,
                principal_id=principal_id,
                person_id=preference_person_id,
            )
        except Exception:
            preference_bundle = {}
    if wants_learning_summary:
        try:
            learning_summary = dict(
                product.property_feedback_learning_summary(
                    principal_id=principal_id,
                    person_id=preference_person_id,
                    domain="willhaben",
                )
                or {}
            )
        except Exception:
            learning_summary = {}

    account_google: dict[str, object] = {}
    access_links: dict[str, object] = {}
    if surface_scope.section in {"account", "settings"}:
        with contextlib.suppress(Exception):
            account_google = _property_google_account_snapshot(
                container=container,
                principal_id=principal_id,
                status=status,
                run_id=normalized_run_id,
            )
        with contextlib.suppress(Exception):
            access_links = _property_access_link_snapshot(
                product=product,
                principal_id=principal_id,
            )

    billing_truth = build_property_billing_truth_snapshot(
        commercial=commercial,
        default_billing_plan=default_billing_plan,
        billing_enabled_plans=billing_enabled_plans,
        billing_order_endpoints_by_plan=billing_order_endpoints_by_plan,
    )
    search_health = build_property_search_health_snapshot({})
    if surface_scope.section == "search":
        diagnostics = _property_first_paint_value(
            lambda: product.workspace_diagnostics(principal_id=principal_id),
            {},
        )
        if isinstance(diagnostics, dict) and diagnostics:
            diagnostics = dict(diagnostics)
            if not isinstance(diagnostics.get("billing"), dict) or not diagnostics.get("billing"):
                diagnostics["billing"] = dict(billing_truth)
            search_health = build_property_search_health_snapshot(
                diagnostics,
                observed_at=datetime.now(timezone.utc).isoformat(),
            )
    country_catalog_snapshot = _property_country_catalog_snapshot()

    return {
        "_principal_id": str(principal_id or "").strip(),
        "_raw_requested_run": raw_requested_run_payload,
        "platform_options": country_provider_options,
        "platform_catalog_by_country": dict(country_catalog_snapshot.get("platform_catalog_by_country") or {}),
        "platform_defaults_by_country_mode": dict(country_catalog_snapshot.get("platform_defaults_by_country_mode") or {}),
        "evidence_source_catalog_by_country": dict(country_catalog_snapshot.get("evidence_source_catalog_by_country") or {}),
        "default_language_by_country": dict(country_catalog_snapshot.get("default_language_by_country") or {}),
        "country_options": property_country_options(),
        "language_options": property_language_options(),
        "listing_mode_options": property_listing_mode_options(),
        "search_goal_options": property_search_goal_options(),
        "investment_strategy_options": property_investment_strategy_options(),
        "investment_research_mode_options": property_investment_research_mode_options(),
        "property_type_options": property_type_options_catalog(),
        "country_label": property_country_label(selected_country),
        "language_label": property_language_label(preferences.get("language_code"), country_code=selected_country),
        "listing_mode_label": property_listing_mode_label(preferences.get("listing_mode")),
        "search_goal_label": property_search_goal_label(preferences.get("search_goal")),
        "investment_strategy_label": property_investment_strategy_label(preferences.get("investment_strategy")),
        "investment_research_mode_label": property_investment_research_mode_label(preferences.get("investment_research_mode")),
        "property_type_label": property_type_label_for_value(preferences.get("property_type")),
        "provider_total_for_country": len(country_provider_options),
        "preferences": preferences,
        "raw_preferences": raw_property_preferences,
        "selected_platforms": list(selected_platforms),
        "run": run_payload if wants_run_state else lightweight_active_run,
        "run_health": run_health,
        "recent_search_runs": recent_search_runs,
        "selected_agent_id": str(selected_agent_id or "").strip(),
        "recent_matches": recent_matches,
        "learning_summary": learning_summary,
        "preference_bundle": preference_bundle,
        "preference_person_id": preference_person_id,
        "account_google": account_google,
        "access_links": access_links,
        "start_endpoint": "/app/api/property/search-runs",
        "preferences_endpoint": "/v1/onboarding/property-search/preferences",
        "commercial": commercial,
        "billing_handoff": _property_brilliant_directories_billing_handoff(),
        "billing_checkout_enabled": bool(billing_truth.get("checkout_enabled")),
        "billing_checkout_enabled_plans": list(billing_truth.get("checkout_enabled_plans") or []),
        "billing_order_endpoint": str(billing_truth.get("order_endpoint") or ""),
        "billing_order_endpoints_by_plan": dict(billing_truth.get("order_endpoints_by_plan") or {}),
        "billing_truth": billing_truth,
        "search_health": search_health,
    }


def _property_run_payload_has_shortlist_results(run_payload: dict[str, object]) -> bool:
    if not isinstance(run_payload, dict) or not run_payload:
        return False
    summary = dict(run_payload.get("summary") or {}) if isinstance(run_payload.get("summary"), dict) else {}
    for key in ("ranked_candidates", "results", "top_candidates"):
        candidates = run_payload.get(key)
        if isinstance(candidates, list) and candidates:
            return True
        summary_candidates = summary.get(key)
        if isinstance(summary_candidates, list) and summary_candidates:
            return True
    sources = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    if any(list(source.get("top_candidates") or []) for source in sources):
        return True
    for key in ("ranked_total", "ranked_candidate_total", "results_total", "survivor_total"):
        raw_value = run_payload.get(key, summary.get(key))
        try:
            if int(raw_value or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _property_enrich_candidate_feedback(
    *,
    packet_service: Any,
    principal_id: str,
    candidate: dict[str, object],
) -> dict[str, object]:
    candidate_row = dict(candidate)
    feedback_summary: dict[str, object] = {}
    feedback_rows: list[dict[str, object]] = []
    for feedback_ref in _property_feedback_reference_candidates(
        _property_candidate_ref(candidate_row),
        candidate_row,
    ):
        try:
            summary_candidate = dict(packet_service.feedback_summary(principal_id=principal_id, property_ref=feedback_ref))
        except Exception:
            summary_candidate = {}
        if not _property_feedback_summary_has_signal(summary_candidate):
            continue
        try:
            rows_candidate = list(packet_service.list_structured_feedback(principal_id=principal_id, property_ref=feedback_ref))
        except Exception:
            rows_candidate = []
        if not feedback_summary:
            feedback_summary = summary_candidate
        if rows_candidate and not feedback_rows:
            feedback_rows = rows_candidate
        if feedback_summary and feedback_rows:
            break
    candidate_row["feedback_summary"] = feedback_summary
    candidate_row["feedback_rows"] = feedback_rows[:12]
    return candidate_row


@router.get("/", response_class=HTMLResponse)
def landing(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
    context: RequestContext = Depends(get_request_context_if_available),
) -> Response:
    brand = request_brand(request)
    context_authenticated_principal = str(context.principal_id or "").strip() if context.authenticated else ""
    authenticated_principal = _landing_authenticated_principal(
        container=container,
        access_identity=access_identity,
        request=request,
    ) or context_authenticated_principal
    if (
        str(brand.get("key") or "").strip() == "propertyquarry"
        and authenticated_principal
        and not _landing_public_home_requested(request)
    ):
        return RedirectResponse(str(brand.get("app_home") or "/app/search"), status_code=307)
    principal_id = context_authenticated_principal or _principal_for_page(
        container=container,
        access_identity=access_identity,
        request=request,
    )
    status = _anonymous_onboarding_status()
    if principal_id:
        status["workspace"] = {
            "name": (
                str(getattr(access_identity, "display_name", "") or "").strip()
                or "PropertyQuarry"
            ),
        }
    else:
        principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    commercial = property_commercial_snapshot(None)
    example_shortlist = _propertyquarry_example_shortlist_rows()
    home_shortlist = _propertyquarry_home_shortlist_panel(
        product=None,
        principal_id=authenticated_principal,
    )
    if str(brand.get("key") or "").strip() == "propertyquarry" and authenticated_principal:
        with contextlib.suppress(Exception):
            home_shortlist = _propertyquarry_home_shortlist_panel(
                product=build_product_service(container),
                principal_id=authenticated_principal,
            )
    return _render_public_template(
        request,
        "propertyquarry_home.html" if brand["key"] == "propertyquarry" else "marketing_home.html",
        **_public_context(
            request=request,
            current_nav="product",
            page_title=brand["name"],
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "feature_cards": FEATURE_CARDS,
                "how_steps": HOW_STEPS,
                "personas": PERSONAS,
                "product_modules": PRODUCT_MODULES,
                "trust_cards": TRUST_CARDS,
                "landing_faqs": LANDING_FAQS,
                "doc_links": DOC_LINKS,
                "plan_catalog": tuple(commercial.get("plan_catalog") or ()),
                "example_shortlist": example_shortlist,
                "home_shortlist": home_shortlist,
                "meta_description": "PropertyQuarry helps renters, buyers, and investors define a brief, rank the strongest homes, open the right ones, and decide faster.",
                "structured_data_json": [
                    {
                        "@context": "https://schema.org",
                        "@type": "SoftwareApplication",
                        "name": "PropertyQuarry",
                        "applicationCategory": "BusinessApplication",
                        "description": "A focused property search workspace for serious apartment and home decisions.",
                        "offers": {
                            "@type": "AggregateOffer",
                            "lowPrice": "0",
                            "priceCurrency": "EUR",
                        },
                    },
                    _faq_structured_data(LANDING_FAQS),
                ],
            },
        ),
    )


@router.get("/directory", response_class=HTMLResponse)
def property_directory_page() -> RedirectResponse:
    return RedirectResponse("/", status_code=307)


@router.get("/directory/profile/{profile_id}", response_class=HTMLResponse)
def property_directory_profile_page(profile_id: str) -> RedirectResponse:
    del profile_id
    return RedirectResponse("/", status_code=307)


@router.get("/product", response_class=HTMLResponse)
def product_page() -> RedirectResponse:
    return RedirectResponse("/", status_code=307)


@router.get("/app/api/property/landing-handoff", include_in_schema=False)
def propertyquarry_landing_handoff(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> dict[str, object]:
    principal_id = _principal_for_page(container=container, access_identity=access_identity, request=request)
    if not principal_id:
        return {"target": "/app/search", "signed_in": False}
    product = build_product_service(container)
    active_run = dict(product.find_active_property_search_run(principal_id=principal_id, limit=8) or {})
    if active_run:
        try:
            status = container.onboarding.status(principal_id=principal_id)
            raw_property_preferences = dict(status.get("property_search_preferences") or {})
            saved_preferences = _property_customer_scoped_preferences(
                normalize_property_search_preferences(
                    dict(raw_property_preferences.get("raw_preferences") or raw_property_preferences)
                )
            )
            saved_country = normalize_country_code(saved_preferences.get("country_code"))
            saved_platforms = {
                str(value or "").strip().lower()
                for value in list(saved_preferences.get("selected_platforms") or [])
                if str(value or "").strip()
            }
            saved_platforms = set(
                property_filter_selectable_property_platforms(
                    saved_platforms,
                    country_code=saved_country,
                    listing_mode=normalize_listing_mode(saved_preferences.get("listing_mode")),
                    include_distressed_sale_signals=saved_preferences.get("include_distressed_sale_signals"),
                )[0]
            )
            if not saved_platforms:
                saved_platforms = set(
                    default_platforms_for_country_listing_mode(
                        saved_country,
                        saved_preferences.get("listing_mode"),
                        property_type=saved_preferences.get("property_type"),
                    )
                )
            if not _property_run_matches_saved_brief(
                active_run,
                preferences=saved_preferences,
                selected_platforms=saved_platforms,
            ):
                active_run = {}
        except Exception:
            active_run = {}
    latest_results_target = _propertyquarry_latest_shortlist_results_target(
        product=product,
        principal_id=principal_id,
    )
    candidate_run_id = str(active_run.get("run_id") or "").strip()
    if candidate_run_id:
        response: dict[str, object] = {
            "target": f"/app/properties?run_id={urllib.parse.quote(candidate_run_id, safe='')}",
            "signed_in": True,
            "run_id": candidate_run_id,
            "status": str(active_run.get("status") or "").strip(),
        }
        if latest_results_target:
            response["latest_results_target"] = latest_results_target
        return response
    response = {"target": "/app/search", "signed_in": True}
    if latest_results_target:
        response["latest_results_target"] = latest_results_target
    return response


@router.get("/app/api/property/candidates/{candidate_ref}/preview-refresh", include_in_schema=False)
def propertyquarry_candidate_preview_refresh(
    candidate_ref: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context_if_available),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
    run_id: str = Query(default=""),
) -> JSONResponse:
    brand = request_brand(request)
    if str(brand.get("key") or "").strip() != "propertyquarry":
        raise HTTPException(status_code=404, detail="surface_not_found")
    normalized_candidate_ref = str(candidate_ref or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not normalized_candidate_ref or not normalized_run_id:
        raise HTTPException(status_code=400, detail="candidate_preview_refresh_requires_run")
    context_authenticated_principal = str(context.principal_id or "").strip() if context.authenticated else ""
    authenticated_principal = context_authenticated_principal or _principal_for_page(
        container=container,
        access_identity=access_identity,
        request=request,
    )
    if not authenticated_principal:
        raise HTTPException(status_code=401, detail="authentication_required")

    product = build_product_service(container)
    raw_run_payload: dict[str, object] = {}
    try:
        raw_run_payload = dict(
            product.get_property_search_run_status(
                principal_id=authenticated_principal,
                run_id=normalized_run_id,
                lightweight=True,
                account_email=str(context.access_email or "").strip(),
            )
            or {}
        )
    except TypeError:
        try:
            raw_run_payload = dict(
                product.get_property_search_run_status(
                    principal_id=authenticated_principal,
                    run_id=normalized_run_id,
                    lightweight=True,
                )
                or {}
            )
        except TypeError:
            raw_run_payload = dict(
                product.get_property_search_run_status(
                    principal_id=authenticated_principal,
                    run_id=normalized_run_id,
                )
                or {}
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="candidate_preview_refresh_unavailable") from exc

    if not raw_run_payload:
        raise HTTPException(status_code=404, detail="property_search_run_not_found")

    run_payload = _propertyquarry_prepare_run_payload(
        product=product,
        run_payload=raw_run_payload,
        backfill_cached_previews=False,
        principal_id=authenticated_principal,
    )
    refreshed_run_payload = _propertyquarry_refresh_run_candidate_preview_if_needed(
        product=product,
        run_payload=run_payload,
        candidate_ref=normalized_candidate_ref,
        allow_network=True,
        principal_id=authenticated_principal,
    )
    candidate = _propertyquarry_find_run_candidate(
        run_payload=refreshed_run_payload,
        candidate_ref=normalized_candidate_ref,
    )
    if not isinstance(candidate, dict):
        raise HTTPException(status_code=404, detail="property_candidate_not_found")
    return JSONResponse(
        {
            "run_id": normalized_run_id,
            "candidate_ref": normalized_candidate_ref,
            "candidate": _property_workbench_client_candidate_payload(
                candidate,
                preserve_preview_media_facts=True,
            ),
        }
    )


@router.get("/app/api/property/billing/commercial-lane", include_in_schema=False, response_class=HTMLResponse)
def property_billing_commercial_lane() -> HTMLResponse:
    handoff = _property_brilliant_directories_billing_handoff(allow_verified_direct_handoff=True)
    if not handoff.get("available"):
        return RedirectResponse(_property_billing_fallback_href(), status_code=303)
    open_href = str(handoff.get("open_href") or "").strip()
    return RedirectResponse(open_href or _property_billing_fallback_href(), status_code=303)


@router.get("/app/api/property/billing/bridge-launch", include_in_schema=False, response_class=HTMLResponse)
def property_billing_bridge_launch(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    if not (context.authenticated or context.principal_id):
        return RedirectResponse("/sign-in?current_session=missing", status_code=303)
    plan_key = ""
    with contextlib.suppress(Exception):
        status = container.onboarding.status(principal_id=context.principal_id)
        preferences = dict(status.get("property_search_preferences") or {})
        commercial = dict(preferences.get("property_commercial") or {})
        plan_key = str(commercial.get("active_plan_key") or commercial.get("plan_key") or "").strip()
    member_token_receipt = brilliant_directories_service.build_brilliant_directories_member_login_token_receipt()
    if member_token_receipt.get("ready"):
        try:
            login_url = brilliant_directories_service.build_brilliant_directories_member_login_token_handoff_url(
                principal_id=context.principal_id,
                access_email=context.access_email,
                plan_key=plan_key,
            )
        except RuntimeError:
            login_url = ""
        if login_url:
            return RedirectResponse(login_url, status_code=303)
    handoff = _property_brilliant_directories_billing_handoff()
    if not bool(handoff.get("available")) or str(handoff.get("status") or "").strip().lower() != "bridge_ready":
        return RedirectResponse(_property_billing_fallback_href(), status_code=303)
    try:
        bridge_url = brilliant_directories_service.build_brilliant_directories_billing_sso_bridge_launch_url(
            principal_id=context.principal_id,
            access_email=context.access_email,
            return_to=str(request.query_params.get("return_to") or "/app/account").strip() or "/app/account",
            plan_key=plan_key,
        )
    except RuntimeError:
        return RedirectResponse(_property_billing_fallback_href(), status_code=303)
    return RedirectResponse(bridge_url, status_code=303)


def _render_property_billing_handoff_page(
    request: Request,
    *,
    context: RequestContext,
    status: dict[str, object],
    access_identity: CloudflareAccessIdentity | None,
    iframe_src: str,
) -> HTMLResponse:
    response = _render_public_template(
        request,
        "property_billing_commercial_lane.html",
        **_public_context(
            request=request,
            current_nav="billing",
            page_title="PropertyQuarry Billing",
            principal_id=context.principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "robots_directive": "noindex, nofollow, noarchive, nosnippet",
                "billing_lane_iframe_src": iframe_src,
            },
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _render_property_billing_unavailable_page(
    request: Request,
    *,
    context: RequestContext,
    handoff: dict[str, object],
) -> HTMLResponse:
    status_value = str(handoff.get("status") or "").strip().lower()
    error_value = str(handoff.get("error") or "").strip().lower()
    if status_value == "unresolved" or "host_unresolved" in error_value or "cloudflare" in error_value:
        blocker_summary = "The billing account host is not ready yet."
        blocker_label = "Host not ready"
    elif status_value == "login_required" or "separate_login" in error_value:
        blocker_summary = "Billing still opens another sign-in, so PropertyQuarry is keeping it closed for now."
        blocker_label = "Separate login"
    else:
        blocker_summary = "The billing portal is still being connected."
        blocker_label = "Connecting"
    response = _render_secure_link_page(
        request,
        page_title="PropertyQuarry Billing",
        current_nav="billing",
        link_kicker="PropertyQuarry",
        link_title="Billing portal unavailable",
        link_summary=f"{blocker_summary} Your PropertyQuarry access stays active from the account page.",
        link_detail_title="Current billing status",
        link_status_label=blocker_label,
        link_rows=[
            {
                "label": "Current state",
                "value": blocker_summary,
                "detail": "PropertyQuarry keeps billing closed until the white-label handoff opens cleanly.",
            },
            {
                "label": "What stays active",
                "value": "PropertyQuarry access stays active from the account page.",
                "detail": "Search, shortlist, research, alerts, and account settings stay available.",
            },
        ],
        primary_action_href="/app/account?billing=1#delivery",
        primary_action_label="Open account",
        secondary_action_href="/app/search",
        secondary_action_label="Open search",
        status_code=503,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/integrations", response_class=HTMLResponse)
def integrations_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    return _render_public_template(
        request,
        "integrations_page.html",
        **_public_context(
            request=request,
            current_nav="integrations",
            page_title="PropertyQuarry Integrations",
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "meta_description": "See which PropertyQuarry integrations are live, which stay guided, and where manual review remains explicit.",
            },
        ),
    )


@router.get("/integrations/{channel_name}", response_class=HTMLResponse)
def integration_detail(
    channel_name: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    channels = dict(status.get("channels") or {})
    mapping = {
        "google": {
            "title": "Google sign-in",
            "eyebrow": "Google",
            "detail_points": (
                "Start with Google sign-in unless you already know you need broader workspace actions.",
                "PropertyQuarry only needs Google identity by default so the same account can return cleanly.",
                "Broader Gmail or Drive context stays an explicit upgrade path instead of the default.",
            ),
            "body_points": (
                "Explain permissions in plain language first and technical scopes second.",
                "Show a real connected account and a real first success instead of treating consent as the finish line.",
                "Keep Google as optional account access, not as the center of the product story.",
            ),
        },
        "telegram": {
            "title": "Telegram",
            "eyebrow": "Telegram",
            "detail_points": (
                "Personal identity linking and official bot installation are separate decisions.",
                "Login alone does not imply generic history import.",
                "Future-only, import-later, and manual-forward are distinct promises and stay distinct in the UI.",
            ),
            "body_points": (
                "Ask first whether this is a personal Telegram setup or a bot rollout.",
                "Record where PropertyQuarry should operate: DM, groups, or channels.",
                "Treat the bot as the durable operating surface once installed and ready.",
            ),
        },
        "whatsapp": {
            "title": "WhatsApp",
            "eyebrow": "WhatsApp",
            "detail_points": (
                "Business onboarding and export intake are separate supported paths.",
                "The assistant does not promise generic automated history download outside those paths.",
                "Live messaging and manual history intake stay visibly distinct in the product contract.",
            ),
            "body_points": (
                "Use Business onboarding for the long-term live assistant path.",
                "Use export intake for personal or unsupported cases without pretending it is live sync.",
                "Keep media inclusion, history source, and future live sync as separate explicit choices.",
            ),
        },
    }
    current = mapping.get(channel_name)
    if current is None:
        raise HTTPException(status_code=404, detail="integration_not_found")
    channel = dict(channels.get(channel_name) or {})
    return _render_public_template(
        request,
        "channel_detail.html",
        **_public_context(
            request=request,
            current_nav="integrations",
            page_title=f"PropertyQuarry {current['title']}",
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "channel": channel,
                "channel_title": current["title"],
                "channel_eyebrow": current["eyebrow"],
                "detail_points": current["detail_points"],
                "body_points": current["body_points"],
                "meta_description": f"Understand how PropertyQuarry handles {current['title']} access, review, and supported actions.",
            },
        ),
    )


def _how_it_works_page(
    request: Request,
    *,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    return _render_public_template(
        request,
        "security_page.html",
        **_public_context(
            request=request,
            current_nav="how-it-works",
            page_title="PropertyQuarry How It Works",
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "how_steps": HOW_STEPS,
                "trust_cards": TRUST_CARDS,
                "meta_description": "How PropertyQuarry turns one search brief into matching homes, clear comparison, focused research, and a saved decision.",
            },
        ),
    )


@router.get("/how-it-works", response_class=HTMLResponse)
def how_it_works_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    return _how_it_works_page(request, container=container, access_identity=access_identity)


@router.get("/how-it-works/score", response_class=HTMLResponse)
def how_it_works_score_page(
    request: Request,
    language: str = Query(default="", max_length=16),
    country: str = Query(default="", max_length=8),
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    workspace = dict(status.get("workspace") or {}) if isinstance(status.get("workspace"), dict) else {}
    country_code = str(country or workspace.get("country_code") or workspace.get("region") or "AT").strip() or "AT"
    language_code = resolve_property_score_methodology_language(
        language_code=language or "",
        country_code=country_code,
        accept_language=request.headers.get("accept-language") or "",
        fallback_language_code=workspace.get("language") or "",
    )
    score_methodology = build_property_score_methodology(
        language_code=language_code,
        country_code=country_code,
        accept_language=request.headers.get("accept-language") or "",
    )
    return _render_public_template(
        request,
        "score_methodology_viewer.html",
        **_public_context(
            request=request,
            current_nav="how-it-works",
            page_title="PropertyQuarry Score Methodology",
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "score_methodology": score_methodology,
                "pdf_url": (
                    f"/v1/integrations/fliplink/documents/score-methodology.pdf?language={urllib.parse.quote(language_code)}"
                    f"&country={urllib.parse.quote(country_code)}"
                ),
                "meta_description": "See how PropertyQuarry handles must-haves, preferences, missing details, and fit.",
            },
        ),
    )


@router.get("/security", response_class=HTMLResponse)
def security_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    return _render_public_template(
        request,
        "security_page.html",
        **_public_context(
            request=request,
            current_nav="security",
            page_title="PropertyQuarry Security",
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "security_view": True,
                "meta_description": "How PropertyQuarry keeps searches private, sharing explicit, account access narrow, and data controls visible.",
            },
        ),
    )


@router.get("/data-deletion", response_class=HTMLResponse)
def data_deletion_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
    context: RequestContext = Depends(get_request_context_if_available),
) -> HTMLResponse:
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    authenticated_principal = str(context.principal_id or "").strip()
    privacy_request: dict[str, object] = {}
    if authenticated_principal:
        with contextlib.suppress(Exception):
            privacy_request = build_property_account_privacy_lifecycle(container).latest(
                principal_id=authenticated_principal,
            )
    return _render_public_template(
        request,
        "data_deletion.html",
        **_public_context(
            request=request,
            current_nav="security",
            page_title="PropertyQuarry Data Deletion",
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "contact_email": "property@propertyquarry.com",
                "privacy_authenticated": bool(authenticated_principal),
                "privacy_request": privacy_request,
                "account_export_href": "/app/api/property/account/export?download=1",
                "account_href": "/app/account#data-export",
                "meta_description": "How to request deletion of account, search, connected-channel, and generated property data held by PropertyQuarry.",
            },
        ),
    )


@router.get("/user-data-deletion", response_class=HTMLResponse)
def user_data_deletion_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
    context: RequestContext = Depends(get_request_context_if_available),
) -> HTMLResponse:
    return data_deletion_page(
        request=request,
        container=container,
        access_identity=access_identity,
        context=context,
    )


@router.get("/pricing", response_class=HTMLResponse)
def pricing_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
    context: RequestContext = Depends(get_request_context_if_available),
) -> Response:
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    commercial = property_commercial_snapshot(None)
    checkout_enabled_plans: list[str] = []
    checkout_order_endpoints_by_plan: dict[str, str] = {}
    for paid_plan in ("plus", "agent"):
        if payfunnels_configured(plan_key=paid_plan):
            checkout_enabled_plans.append(paid_plan)
            checkout_order_endpoints_by_plan[paid_plan] = "/app/api/signals/property/billing/checkout/order"
    checkout_session_ready = bool(
        access_identity is not None
        or principal_id
        or _workspace_session_payload(request, container) is not None
    )
    account_nav = _account_nav_context(request=request, context=context)
    pricing_signed_in_billing_href = str(account_nav.get("billing_href") or _property_billing_fallback_href()).strip() or _property_billing_fallback_href()
    pricing_signed_in_billing_ready = False
    pricing_signed_in_billing_label = "Open billing"
    pricing_signed_in_billing_detail = "Manage plan and billing."
    if checkout_session_ready and request_brand(request)["key"] == "propertyquarry":
        billing_handoff = _property_brilliant_directories_billing_handoff(allow_verified_direct_handoff=True)
        verified_billing_href = _property_billing_usable_open_href(billing_handoff)
        pricing_signed_in_billing_href = (
            verified_billing_href
            or pricing_signed_in_billing_href
        )
        pricing_signed_in_billing_ready = bool(verified_billing_href)
        (
            pricing_signed_in_billing_label,
            pricing_signed_in_billing_detail,
        ) = _property_pricing_billing_link_copy(billing_handoff)
    return _render_public_template(
        request,
        "pricing_page.html",
        **_public_context(
            request=request,
            current_nav="pricing",
            page_title="PropertyQuarry Pricing",
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "pricing_tiers": PRICING_TIERS,
                "plan_catalog": tuple(commercial.get("plan_catalog") or ()),
                "pricing_checkout_enabled_plans": checkout_enabled_plans,
                "pricing_order_endpoints_by_plan": checkout_order_endpoints_by_plan,
                "pricing_checkout_session_ready": checkout_session_ready,
                "pricing_signed_in_billing_href": pricing_signed_in_billing_href,
                "pricing_signed_in_billing_ready": pricing_signed_in_billing_ready,
                "pricing_signed_in_billing_label": pricing_signed_in_billing_label,
                "pricing_signed_in_billing_detail": pricing_signed_in_billing_detail,
                "meta_description": "Choose a PropertyQuarry plan by site coverage, research depth, 3D options, and the quality of the property page.",
                "structured_data_json": [
                    {
                        "@context": "https://schema.org",
                        "@type": "Product",
                        "name": "PropertyQuarry",
                        "description": "Property search, matching homes, and clear follow-up for renters, buyers, and investors.",
                        "offers": [
                            {
                                "@type": "Offer",
                                "name": str(plan.get("display_name") or ""),
                                "price": str(plan.get("monthly_price_eur") or 0),
                                "priceCurrency": "EUR",
                            }
                            for plan in tuple(commercial.get("plan_catalog") or ())
                        ],
                    },
                    _faq_structured_data(
                        (
                            {
                                "question": "When should I stay on Free?",
                                "answer": "Stay on Free when the main question is whether the matching homes and the full details are useful at all.",
                            },
                            {
                                "question": "What does Plus change?",
                                "answer": "Plus gives a denser shortlist and deeper detail on a tighter set of listing sites.",
                            },
                            {
                                "question": "When does Agent make sense?",
                                "answer": "Agent is for ongoing property search work that needs full site breadth, denser shortlists, and deeper research at the same time.",
                            },
                        )
                    ),
                ],
            },
        ),
    )


@router.get("/docs", response_class=HTMLResponse)
def docs_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    return _render_public_template(
        request,
        "docs_page.html",
        **_public_context(
            request=request,
            current_nav="docs",
            page_title="PropertyQuarry Docs",
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "doc_links": DOC_LINKS,
                "meta_description": "Read the public documentation for PropertyQuarry features, workflows, and product boundaries.",
            },
        ),
    )


def _render_public_trust_page(
    *,
    page_key: str,
    request: Request,
    container: AppContainer,
    access_identity: CloudflareAccessIdentity | None,
) -> HTMLResponse:
    page = PUBLIC_TRUST_PAGES.get(page_key)
    if page is None:
        raise HTTPException(status_code=404, detail="public_trust_page_not_found")
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    editorial_cta_href = ""
    editorial_cta_label = ""
    editorial_cta_event = ""
    editorial_secondary_cta_href = ""
    editorial_secondary_cta_label = ""
    editorial_secondary_cta_event = ""
    if page_key == "support":
        editorial_cta_href = (
            "/app/support"
            if principal_id
            else "/sign-in?signing_in=1&return_to=%2Fapp%2Fsupport"
        )
        editorial_cta_label = "Open account support" if principal_id else "Sign in to attach account context"
        editorial_cta_event = "support_open_authenticated"
        editorial_secondary_cta_href = "mailto:property@propertyquarry.com?subject=PropertyQuarry%20support"
        editorial_secondary_cta_label = "Email support"
        editorial_secondary_cta_event = "support_email"
    elif page_key == "imprint":
        editorial_cta_href = "/support"
        editorial_cta_label = "Open support"
        editorial_cta_event = "imprint_open_support"
    return _render_public_template(
        request,
        "public_editorial_page.html",
        **_public_context(
            request=request,
            current_nav=str(page.get("nav") or page_key),
            page_title=f"PropertyQuarry {page['title']}",
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "canonical_path": str(page["path"]),
                "meta_description": str(page["summary"]),
                "editorial_kicker": page["kicker"],
                "editorial_title": page["title"],
                "editorial_summary": page["summary"],
                "editorial_cta_href": editorial_cta_href,
                "editorial_cta_label": editorial_cta_label,
                "editorial_cta_event": editorial_cta_event,
                "editorial_secondary_cta_href": editorial_secondary_cta_href,
                "editorial_secondary_cta_label": editorial_secondary_cta_label,
                "editorial_secondary_cta_event": editorial_secondary_cta_event,
                "editorial_band": page["band"],
                "editorial_sections": page["sections"],
                "editorial_faqs": page["faqs"],
                "structured_data_json": [
                    {
                        "@context": "https://schema.org",
                        "@type": "WebPage",
                        "name": f"PropertyQuarry {page['title']}",
                        "description": str(page["summary"]),
                        "publisher": {"@type": "Organization", "name": "PropertyQuarry"},
                    },
                ],
            },
        ),
    )


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    return _render_public_trust_page(
        page_key="privacy",
        request=request,
        container=container,
        access_identity=access_identity,
    )


@router.get("/terms", response_class=HTMLResponse)
def terms_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    return _render_public_trust_page(
        page_key="terms",
        request=request,
        container=container,
        access_identity=access_identity,
    )


@router.get("/imprint", response_class=HTMLResponse)
def imprint_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    return _render_public_trust_page(
        page_key="imprint",
        request=request,
        container=container,
        access_identity=access_identity,
    )


@router.get("/support", response_class=HTMLResponse)
def support_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    return _render_public_trust_page(
        page_key="support",
        request=request,
        container=container,
        access_identity=access_identity,
    )


@router.get("/cookies", response_class=HTMLResponse)
def cookies_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    return _render_public_trust_page(
        page_key="cookies",
        request=request,
        container=container,
        access_identity=access_identity,
    )


@router.get("/subprocessors", response_class=HTMLResponse)
def subprocessors_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    return _render_public_trust_page(
        page_key="subprocessors",
        request=request,
        container=container,
        access_identity=access_identity,
    )


@router.get("/refunds", response_class=HTMLResponse)
def refunds_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    return _render_public_trust_page(
        page_key="refunds",
        request=request,
        container=container,
        access_identity=access_identity,
    )


@router.get("/disclaimers", response_class=HTMLResponse)
def disclaimers_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    return _render_public_trust_page(
        page_key="disclaimers",
        request=request,
        container=container,
        access_identity=access_identity,
    )


@router.get("/guides/wohnung-kaufen-wien-checkliste", response_class=HTMLResponse)
def guide_wien_checklist_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    page = PUBLIC_GUIDE_WIEN
    return _render_public_template(
        request,
        "public_editorial_page.html",
        **_public_context(
            request=request,
            current_nav="docs",
            page_title=str(page["title"]),
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "canonical_path": "/guides/wohnung-kaufen-wien-checkliste",
                "meta_description": str(page["summary"]),
                "editorial_kicker": page["kicker"],
                "editorial_title": page["title"],
                "editorial_summary": page["summary"],
                "editorial_cta_href": "/register",
                "editorial_cta_label": "Open PropertyQuarry",
                "editorial_cta_event": "guide_open_propertyquarry",
                "editorial_band": page["band"],
                "editorial_sections": page["sections"],
                "editorial_faqs": page["faqs"],
                "structured_data_json": [
                    {
                        "@context": "https://schema.org",
                        "@type": "Article",
                        "headline": str(page["title"]),
                        "description": str(page["summary"]),
                        "author": {"@type": "Organization", "name": "PropertyQuarry"},
                        "publisher": {"@type": "Organization", "name": "PropertyQuarry"},
                    },
                    _faq_structured_data(page["faqs"]),
                ],
            },
        ),
    )


@router.get("/markets/vienna", response_class=HTMLResponse)
def market_vienna_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    page = PUBLIC_MARKET_VIENNA
    return _render_public_template(
        request,
        "public_editorial_page.html",
        **_public_context(
            request=request,
            current_nav="product",
            page_title=str(page["title"]),
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "canonical_path": "/markets/vienna",
                "meta_description": str(page["summary"]),
                "editorial_kicker": page["kicker"],
                "editorial_title": page["title"],
                "editorial_summary": page["summary"],
                "editorial_cta_href": "/register",
                "editorial_cta_label": "Start a Vienna search",
                "editorial_cta_event": "market_start_search",
                "editorial_band": page["band"],
                "editorial_sections": page["sections"],
                "editorial_faqs": page["faqs"],
                "structured_data_json": [
                    {
                        "@context": "https://schema.org",
                        "@type": "Article",
                        "headline": str(page["title"]),
                        "description": str(page["summary"]),
                        "author": {"@type": "Organization", "name": "PropertyQuarry"},
                        "publisher": {"@type": "Organization", "name": "PropertyQuarry"},
                    },
                    _faq_structured_data(page["faqs"]),
                ],
            },
        ),
    )


@router.api_route("/sign-in", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
def sign_in_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
    request_context: RequestContext = Depends(get_request_context_if_available),
) -> HTMLResponse:
    brand = request_brand(request)
    sign_in_return_to = _normalize_browser_return_to(
        request.query_params.get("return_to"),
        default=str(brand.get("app_home") or "/app/search"),
    )
    principal_id, status = _load_status(
        container=container,
        access_identity=access_identity,
        request=request,
        compact=brand["key"] == "propertyquarry",
    )
    if (
        not principal_id
        and request_context.authenticated is True
        and str(request_context.auth_source or "").strip()
        == "propertyquarry_release_probe"
    ):
        principal_id = str(request_context.principal_id or "").strip()
        if principal_id:
            status = (
                container.onboarding.compact_status(principal_id=principal_id)
                if brand["key"] == "propertyquarry"
                and hasattr(container.onboarding, "compact_status")
                else container.onboarding.status(principal_id=principal_id)
            )
    link_status = str(request.query_params.get("link_status") or "").strip()
    link_email = str(request.query_params.get("link_email") or "").strip()
    link_error = str(request.query_params.get("link_error") or "").strip()
    google_error = str(request.query_params.get("google_error") or "").strip()
    google_prefill_email = str(request.query_params.get("google_prefill_email") or "").strip()
    facebook_error = str(request.query_params.get("facebook_error") or "").strip()
    id_austria_error = str(request.query_params.get("id_austria_error") or "").strip()
    session_expired = str(request.query_params.get("session") or "").strip().lower() == "expired"
    connected_provider = ""
    if str(request.query_params.get("google_connected") or "").strip() == "1":
        connected_provider = "Google"
    elif str(request.query_params.get("facebook_connected") or "").strip() == "1":
        connected_provider = "Facebook"
    elif str(request.query_params.get("id_austria_connected") or "").strip() == "1":
        connected_provider = "ID Austria"
    id_austria_configured = _id_austria_sign_in_enabled()
    id_austria_country_allowed = _request_is_austrian_ip(request)
    id_austria_visible = id_austria_country_allowed or bool(id_austria_error)
    return _render_public_template(
        request,
        "sign_in.html",
        **_public_context(
            request=request,
            current_nav="sign-in",
            page_title=f"Sign in to {request_brand(request)['name']}",
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={
                "sign_in_notes": SIGN_IN_NOTES,
                "sign_in_link_enabled": email_delivery_enabled(),
                "sign_in_link_status": link_status,
                "sign_in_link_email": link_email,
                "sign_in_google_prefill_email": google_prefill_email,
                "sign_in_link_error": link_error,
                "sign_in_google_error": google_error,
                "sign_in_facebook_error": facebook_error,
                "sign_in_id_austria_error": id_austria_error,
                "sign_in_connected_provider": connected_provider,
                "sign_in_google_enabled": _google_sign_in_enabled(request),
                "sign_in_facebook_enabled": _facebook_sign_in_enabled(),
                "sign_in_id_austria_configured": id_austria_configured,
                "sign_in_id_austria_country_allowed": id_austria_country_allowed,
                "sign_in_id_austria_visible": id_austria_visible,
                "sign_in_id_austria_enabled": id_austria_configured and id_austria_country_allowed,
                "sign_in_session_expired": session_expired,
                "sign_in_return_to": sign_in_return_to,
                "sign_in_return_to_encoded": urllib.parse.quote(sign_in_return_to, safe=""),
                "robots_directive": "noindex, nofollow, noarchive, nosnippet",
            },
        ),
    )


@router.api_route("/sign-in/current-session", methods=["GET", "HEAD"], response_model=None, include_in_schema=False)
def sign_in_current_session(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> RedirectResponse:
    principal_id = _landing_authenticated_principal(
        container=container,
        access_identity=access_identity,
        request=request,
    )
    if principal_id:
        return RedirectResponse(str(request_brand(request).get("app_home") or "/app/search"), status_code=303)
    return RedirectResponse("/sign-in?current_session=missing", status_code=303)


@router.post("/sign-in/email-link")
async def sign_in_email_link(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> RedirectResponse:
    form_data = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    email = _form_value(form_data, "email", "").lower()
    return_to = _normalize_browser_return_to(
        _form_value(form_data, "return_to", ""),
        default="/app/settings/access",
    )
    product = build_product_service(container)
    try:
        result = product.request_workspace_sign_in_email_links(
            email=email,
            base_url=_public_app_base_url(request),
            default_target=return_to,
        )
    except ValueError as exc:
        return RedirectResponse(
            "/sign-in?"
            + urllib.parse.urlencode(
                {
                    "link_status": "invalid",
                    "link_email": email,
                    "link_error": str(exc or "workspace_sign_in_email_invalid"),
                    "return_to": return_to,
                }
            ),
            status_code=303,
        )
    except RuntimeError as exc:
        return RedirectResponse(
            "/sign-in?"
            + urllib.parse.urlencode(
                {
                    "link_status": "failed",
                    "link_email": email,
                    "link_error": str(exc or "workspace_sign_in_email_delivery_not_configured"),
                    "return_to": return_to,
                }
            ),
            status_code=303,
        )
    query = {
        "link_status": "submitted",
        "link_email": str(result.get("email") or email).strip().lower(),
        "return_to": return_to,
    }
    return RedirectResponse("/sign-in?" + urllib.parse.urlencode(query), status_code=303)


@router.api_route("/sign-in/google", methods=["GET", "POST"], response_model=None, include_in_schema=False)
async def sign_in_google(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> RedirectResponse:
    return_to = _normalize_browser_return_to(
        request.query_params.get("return_to"),
        default=str(request_brand(request).get("app_home") or "/app/search"),
    )
    identity_binding = str(
        request.query_params.get("identity_binding") or ""
    ).strip()
    if request_brand(request).get("key") != "propertyquarry":
        return RedirectResponse(
            "/sign-in?"
            + urllib.parse.urlencode(
                {
                    "google_error": "google_oauth_propertyquarry_host_required",
                    "return_to": return_to,
                }
            ),
            status_code=303,
        )
    try:
        identity_config = propertyquarry_google_identity.load_propertyquarry_google_identity_config()
        packet = propertyquarry_google_identity.build_propertyquarry_google_identity_start(
            redirect_uri=identity_config.redirect_uri,
            return_to=return_to,
            expected_email_binding=identity_binding,
        )
    except RuntimeError as exc:
        return RedirectResponse(
            "/sign-in?"
            + urllib.parse.urlencode(
                {
                    "google_error": str(exc or "google_oauth_not_ready"),
                    "return_to": return_to,
                }
            ),
            status_code=303,
        )
    response = RedirectResponse(str(packet.auth_url), status_code=303)
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    secure_cookie = (
        request.url.scheme == "https"
        or forwarded_proto == "https"
        or str(request.url.hostname or "").strip().lower().rstrip(".")
        in {"propertyquarry.com", "www.propertyquarry.com"}
    )
    response.set_cookie(
        propertyquarry_google_identity.GOOGLE_IDENTITY_FLOW_COOKIE_NAME,
        packet.flow_nonce,
        max_age=packet.max_age_seconds,
        path="/google/callback",
        secure=secure_cookie,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.api_route("/sign-in/facebook", methods=["GET", "POST"], response_model=None, include_in_schema=False)
async def sign_in_facebook(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> RedirectResponse:
    from app.services.facebook_oauth import build_facebook_oauth_start

    return_to = _normalize_browser_return_to(
        request.query_params.get("return_to"),
        default=str(request_brand(request).get("app_home") or "/app/search"),
    )
    if not _facebook_sign_in_enabled():
        return RedirectResponse(
            "/sign-in?"
            + urllib.parse.urlencode(
                {
                    "facebook_error": "facebook_sign_in_disabled",
                    "return_to": return_to,
                }
            ),
            status_code=303,
        )
    try:
        packet = build_facebook_oauth_start(
            principal_id="",
            redirect_uri_override=f"{_public_app_base_url(request)}/facebook/callback",
            return_to=return_to,
            browser_source="sign_in",
        )
    except RuntimeError as exc:
        return RedirectResponse(
            "/sign-in?"
            + urllib.parse.urlencode(
                {
                    "facebook_error": str(exc or "facebook_oauth_not_ready"),
                    "return_to": return_to,
                }
            ),
            status_code=303,
        )
    return RedirectResponse(str(packet.auth_url), status_code=303)


@router.api_route("/sign-in/id-austria", methods=["GET", "POST"], response_model=None, include_in_schema=False)
async def sign_in_id_austria(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> RedirectResponse:
    from app.services.id_austria_oidc import build_id_austria_oidc_start

    return_to = _normalize_browser_return_to(
        request.query_params.get("return_to"),
        default=str(request_brand(request).get("app_home") or "/app/search"),
    )
    if not _request_is_austrian_ip(request):
        return RedirectResponse(
            "/sign-in?"
            + urllib.parse.urlencode(
                {
                    "id_austria_error": "id_austria_austria_ip_required",
                    "return_to": return_to,
                }
            ),
            status_code=303,
        )
    try:
        packet = build_id_austria_oidc_start(
            principal_id="",
            redirect_uri_override=f"{_public_app_base_url(request)}/id-austria/callback",
            return_to=return_to,
            browser_source="sign_in",
        )
    except RuntimeError as exc:
        return RedirectResponse(
            "/sign-in?"
            + urllib.parse.urlencode(
                {
                    "id_austria_error": str(exc or "id_austria_oidc_not_ready"),
                    "return_to": return_to,
                }
            ),
            status_code=303,
        )
    return RedirectResponse(str(packet.auth_url), status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    register_return_to = _normalize_browser_return_to(
        request.query_params.get("return_to"),
        default=str(request_brand(request).get("app_home") or "/app/search"),
    )
    principal_id, status = _load_status(container=container, access_identity=access_identity, request=request)
    if principal_id:
        build_product_service(container).record_surface_event(
            principal_id=principal_id,
            event_type="activation_opened",
            surface="register",
        )
    return _render_public_template(
        request,
        "register.html",
        **_public_context(
            request=request,
            current_nav="product",
            page_title="Set up your PropertyQuarry account" if request_brand(request)["key"] == "propertyquarry" else "Start your workspace",
            principal_id=principal_id,
            status=status,
            access_identity=access_identity,
            extra={"register_return_to": register_return_to},
        ),
    )


@router.api_route("/workspace-invites/{token}", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
def workspace_invite_preview(
    token: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> HTMLResponse:
    product = build_product_service(container)
    invite = product.preview_workspace_invitation(token=token)
    if invite is None:
        return _render_secure_link_page(
            request,
            page_title="Workspace invite unavailable",
            current_nav="sign-in",
            link_kicker="Invite unavailable",
            link_title="This workspace invite is no longer valid.",
            link_summary="Ask the workspace owner to send a fresh invitation or use a current sign-in link if you already have access.",
            link_detail_title="Details",
            link_status_label="Invite unavailable",
            link_rows=[
                {"label": "Invite status", "value": "Unavailable", "detail": "The invite may be expired, revoked, or already replaced."},
                {"label": "Next step", "value": "Request a fresh invite", "detail": "Use sign in if you already have another secure link."},
            ],
            primary_action_href="/sign-in",
            primary_action_label="Request new sign-in link",
            secondary_action_href="/sign-in",
            secondary_action_label="Sign in",
            status_code=404,
        )
    access_url = str(invite.get("access_url") or "").strip()
    if access_url:
        response = RedirectResponse(access_url, status_code=303)
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
        return response
    return _render_secure_link_page(
        request,
        page_title="Review workspace invite",
        current_nav="sign-in",
        link_kicker="Workspace invitation",
        link_title="Review this workspace invite before you join.",
        link_summary="This secure invite opens one workspace. Accept it when you are ready to join.",
        link_detail_title="Invite details",
        link_status_label=str(invite.get("status") or "pending").replace("_", " ").title(),
        link_rows=[
            {"label": "Email", "value": str(invite.get("email") or "Unknown"), "detail": ""},
            {"label": "Role", "value": str(invite.get("role") or "operator").replace("_", " ").title(), "detail": ""},
            {
                "label": "Expires",
                "value": str(invite.get("expires_at") or "Not recorded")[:19] or "Not recorded",
                "detail": "Accept before the invite expires so the workspace can issue access cleanly.",
            },
        ],
        primary_action_href=f"/workspace-invites/{urllib.parse.quote(token, safe='')}/accept",
        primary_action_label="Accept invitation",
        secondary_action_href="/sign-in",
        secondary_action_label="Return through existing access",
    )


@router.api_route("/workspace-access/{token}", methods=["GET", "HEAD"], response_model=None, include_in_schema=False)
def workspace_access_session(
    token: str,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    product = build_product_service(container)
    brand = request_brand(request)
    actor = str(request.headers.get("X-EA-Operator-ID") or request.headers.get("X-EA-Principal-ID") or "").strip()
    session = product.open_workspace_access_session(token=token, actor=actor)
    if session is None:
        return _render_secure_link_page(
            request,
            page_title="Sign-in link unavailable",
            current_nav="sign-in",
            link_kicker="Secure link expired",
            link_title="This sign-in link is no longer valid.",
            link_summary="Request a fresh sign-in link or use another secure workspace path such as an invite or SSO.",
            link_detail_title="What to do next",
            link_status_label="Link expired",
            link_rows=[
                {"label": "Link state", "value": "Expired or revoked", "detail": "Secure workspace links rotate and eventually expire."},
                {"label": "Recovery", "value": "Request a new link", "detail": "Use the same inbox that already has account access."},
            ],
            primary_action_href="/sign-in",
            primary_action_label="Request new sign-in link",
            secondary_action_href="/sign-in",
            secondary_action_label="Sign in",
            status_code=404,
        )
    target = _normalize_browser_return_to(
        request.query_params.get("return_to") or str(session.get("default_target") or "").strip(),
        default=str(session.get("default_target") or brand.get("app_home") or "/app/search"),
    )
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        "ea_workspace_session",
        str(session.get("access_token") or "").strip(),
        **_workspace_session_cookie_kwargs(request, expires_at=str(session.get("expires_at") or "").strip()),
    )
    _clear_signed_out_marker_cookie(response, request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response


def _browser_request_origin(request: Request) -> tuple[str, str, int] | None:
    raw_origin = str(request.headers.get("origin") or "").strip()
    raw_referer = str(request.headers.get("referer") or "").strip()
    source = raw_origin or raw_referer
    if not source or source.lower() == "null" or any(
        character.isspace() for character in source
    ):
        return None
    parsed = urllib.parse.urlsplit(source)
    scheme = str(parsed.scheme or "").strip().lower()
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if scheme not in {"http", "https"} or not hostname:
        return None
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, hostname, int(port)


def _effective_request_origin(request: Request) -> tuple[str, str, int] | None:
    scheme = str(
        request.headers.get("x-forwarded-proto") or request.url.scheme or ""
    ).split(",", 1)[0].strip().lower()
    authority = str(
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
        or ""
    ).split(",", 1)[0].strip()
    if scheme not in {"http", "https"} or not authority:
        return None
    parsed = urllib.parse.urlsplit(f"{scheme}://{authority}")
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        return None
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, hostname, int(port)


def _require_same_origin_browser_post(request: Request) -> None:
    if _browser_request_origin(request) != _effective_request_origin(request):
        raise HTTPException(status_code=403, detail="sign_out_origin_invalid")


@router.post("/app/actions/sign-out", response_model=None, include_in_schema=False)
async def app_sign_out(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> RedirectResponse:
    _require_same_origin_browser_post(request)
    body = urllib.parse.parse_qs(
        (await request.body()).decode("utf-8", errors="ignore"),
        keep_blank_values=True,
    )
    public_base = str(request_brand(request).get("public_base_url") or "/").strip() or "/"
    query_params = request.query_params
    return_to = _normalize_browser_return_to(query_params.get("return_to", "") or _form_value(body, "return_to", public_base), default=public_base)
    if not return_to:
        return_to = public_base
    propertyquarry_identity_cookie_supplied = bool(
        str(
            request.cookies.get(
                propertyquarry_google_identity.GOOGLE_IDENTITY_COOKIE_NAME
            )
            or ""
        ).strip()
    )
    propertyquarry_identity_session = _propertyquarry_identity_session_payload(
        request,
        container,
    )
    if propertyquarry_identity_cookie_supplied:
        actor = "browser"
        if isinstance(propertyquarry_identity_session, dict):
            actor = str(
                propertyquarry_identity_session.get("email")
                or propertyquarry_identity_session.get("principal_id")
                or actor
            ).strip()
        try:
            propertyquarry_google_identity.revoke_propertyquarry_identity_session(
                token=str(
                    request.cookies.get(
                        propertyquarry_google_identity.GOOGLE_IDENTITY_COOKIE_NAME
                    )
                    or ""
                ).strip(),
                database_url=str(
                    getattr(container.settings, "database_url", "") or ""
                ).strip(),
                actor=actor,
            )
        except Exception:
            pass
        response = RedirectResponse(return_to, status_code=303)
        response.delete_cookie(
            propertyquarry_google_identity.GOOGLE_IDENTITY_COOKIE_NAME,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        _clear_workspace_session_cookie(response, request)
        response.set_cookie(
            "ea_workspace_signed_out",
            "1",
            **_signed_out_marker_cookie_kwargs(request),
        )
        response.headers["X-Robots-Tag"] = (
            "noindex, nofollow, noarchive, nosnippet"
        )
        return response

    context = get_request_context(request, container)
    workspace_session = _workspace_session_payload(request, container)
    actor = str(
        context.operator_id
        or context.access_email
        or context.principal_id
        or "browser"
    ).strip()
    if isinstance(workspace_session, dict):
        product = build_product_service(container)
        session_id = str(workspace_session.get("session_id") or "").strip()
        principal_id = str(
            workspace_session.get("principal_id") or context.principal_id or ""
        ).strip()
        if session_id and principal_id:
            try:
                product.revoke_workspace_access_session(
                    principal_id=principal_id,
                    session_id=session_id,
                    actor=actor,
                )
            except Exception:
                pass
    if (
        context.auth_source == "cloudflare_access"
        and str(container.settings.auth.cf_access_team_domain or "").strip()
    ):
        return_to = _cloudflare_access_logout_url(
            request,
            team_domain=str(
                container.settings.auth.cf_access_team_domain or ""
            ).strip(),
            return_to=return_to,
        )

    response = RedirectResponse(return_to, status_code=303)
    _clear_workspace_session_cookie(response, request)
    response.set_cookie(
        "ea_workspace_signed_out",
        "1",
        **_signed_out_marker_cookie_kwargs(request),
    )
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response


@router.api_route("/workspace-invites/{token}/accept", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
def workspace_invite_accept(
    token: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    product = build_product_service(container)
    actor = str(
        getattr(access_identity, "email", "")
        or request.headers.get("X-EA-Operator-ID")
        or request.headers.get("X-EA-Principal-ID")
        or "workspace_invite"
    ).strip() or "workspace_invite"
    try:
        invite = product.accept_workspace_invitation(token=token, accepted_by=actor)
    except ValueError as exc:
        if str(exc or "").strip() == "operator_seat_limit_reached":
            return _render_secure_link_page(
                request,
                page_title="Invite cannot be accepted",
                current_nav="sign-in",
                link_kicker="Workspace full",
                link_title="This workspace cannot add another operator right now.",
                link_summary="The workspace is at its current seat limit. Ask the owner to free a seat or upgrade the plan before retrying.",
                link_detail_title="Why acceptance stopped",
                link_status_label="Seat limit reached",
                link_rows=[
                    {"label": "Invite status", "value": "Pending", "detail": "The invite is still valid, but the workspace needs room before it can be accepted."},
                    {"label": "Next step", "value": "Contact the workspace owner", "detail": "They can free a seat or expand the plan and resend access."},
                ],
                primary_action_href="/sign-in",
                primary_action_label="Return to sign in",
                secondary_action_href="/sign-in",
                secondary_action_label="Sign in",
                status_code=409,
            )
        raise
    if invite is None:
        return _render_secure_link_page(
            request,
            page_title="Workspace invite unavailable",
            current_nav="sign-in",
            link_kicker="Invite unavailable",
            link_title="This workspace invite is no longer valid.",
            link_summary="Ask the workspace owner to send a fresh invitation or use another secure workspace link if you already have access.",
            link_detail_title="Details",
            link_status_label="Invite unavailable",
            link_rows=[
                {"label": "Invite state", "value": "Unavailable", "detail": "The invite may be expired, revoked, or already used."},
                {"label": "Next step", "value": "Request a fresh invite", "detail": "A new secure link will reopen the correct workspace."},
            ],
            primary_action_href="/sign-in",
            primary_action_label="Request new sign-in link",
            secondary_action_href="/sign-in",
            secondary_action_label="Sign in",
            status_code=404,
        )
    access_url = str(invite.get("access_url") or "").strip()
    if access_url:
        response = RedirectResponse(access_url, status_code=303)
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
        return response
    return _render_secure_link_page(
        request,
        page_title="Workspace invite accepted",
        current_nav="sign-in",
        link_kicker="Invitation accepted",
        link_title="Your workspace invite was accepted.",
        link_summary="Continue through sign in if you need another secure access link for this workspace.",
        link_detail_title="Accepted access",
        link_status_label=str(invite.get("status") or "accepted").replace("_", " ").title(),
        link_rows=[
            {"label": "Email", "value": str(invite.get("email") or "Workspace teammate"), "detail": ""},
            {"label": "Role", "value": str(invite.get("role") or "operator").replace("_", " ").title(), "detail": ""},
        ],
        primary_action_href="/sign-in",
        primary_action_label="Continue to sign in",
        secondary_action_href=str(request_brand(request).get("app_home") or "/app/search"),
        secondary_action_label="Open search",
    )


@router.get("/get-started", response_class=HTMLResponse)
def get_started() -> RedirectResponse:
    return RedirectResponse("/sign-in?signing_in=1", status_code=307)


def _property_original_tour_authenticated(context: RequestContext) -> bool:
    return bool(
        context.authenticated is True
        and str(context.auth_source or "").strip().lower()
        not in {
            "",
            "anonymous",
            "loopback_no_auth",
            "propertyquarry_release_probe",
        }
    )


def _property_original_tour_handoff_secret(container: AppContainer) -> bytes:
    configured = str(
        getattr(getattr(container.settings, "auth", None), "signing_secret", "")
        or ""
    ).strip()
    if not configured:
        return b""
    return resolve_signing_secret(
        container.settings,
        purpose=_PROPERTY_ORIGINAL_TOUR_HANDOFF_PURPOSE,
    ).encode("utf-8")


def _property_original_tour_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _property_constant_text_equal(left: object, right: object) -> bool:
    return hmac.compare_digest(
        str(left or "").encode("utf-8"),
        str(right or "").encode("utf-8"),
    )


def _property_original_tour_b64decode(value: str, *, max_bytes: int) -> bytes | None:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > max_bytes * 2
        or re.fullmatch(r"[A-Za-z0-9_-]+", normalized) is None
    ):
        return None
    try:
        decoded = base64.b64decode(
            normalized + ("=" * (-len(normalized) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, TypeError, ValueError):
        return None
    return decoded if len(decoded) <= max_bytes else None


def _property_candidate_current_url(candidate: Mapping[str, object]) -> str:
    return str(
        candidate.get("property_url")
        or candidate.get("listing_url")
        or candidate.get("source_url")
        or candidate.get("url")
        or ""
    ).strip()


def _property_original_tour_candidate_target(
    candidate: Mapping[str, object],
    *,
    principal_id: str,
) -> str:
    """Resolve only explicitly supported Matterport evidence for one candidate."""

    normalized_candidate = dict(candidate)
    raw_tour = (
        dict(normalized_candidate.get("tour") or {})
        if isinstance(normalized_candidate.get("tour"), dict)
        else {}
    )
    property_facts = (
        dict(normalized_candidate.get("property_facts") or {})
        if isinstance(normalized_candidate.get("property_facts"), dict)
        else {}
    )
    evidence_references: tuple[object, ...] = (
        raw_tour.get("provider_url"),
        normalized_candidate.get("vendor_tour_url"),
        normalized_candidate.get("tour_url"),
        raw_tour.get("url"),
        raw_tour.get("embed_url"),
        normalized_candidate.get("source_virtual_tour_url"),
        normalized_candidate.get("source_virtual_tour_origin"),
        raw_tour.get("source_virtual_tour_url"),
        raw_tour.get("source_virtual_tour_origin"),
        property_facts.get("source_virtual_tour_url"),
        property_facts.get("source_virtual_tour_origin"),
    )
    normalized_principal_id = str(principal_id or "").strip()
    for evidence_reference in evidence_references:
        canonical = property_tour_hosting._canonical_matterport_tour_url(
            evidence_reference
        )
        if canonical:
            return canonical
        payload = property_tour_hosting._hosted_property_tour_payload_for_url(
            evidence_reference,
            principal_id=normalized_principal_id,
        )
        if not payload:
            continue
        bundle_principal_id = str(payload.get("principal_id") or "").strip()
        if (
            not bundle_principal_id
            or not normalized_principal_id
            or not _property_constant_text_equal(
                bundle_principal_id,
                normalized_principal_id,
            )
        ):
            continue
        for provider_candidate in (
            payload.get("source_virtual_tour_url"),
            payload.get("source_virtual_tour_origin"),
            payload.get("matterport_url"),
        ):
            canonical = property_tour_hosting._canonical_matterport_tour_url(
                provider_candidate
            )
            if canonical:
                return canonical
    return ""


def _property_original_tour_handoff_href(
    *,
    container: AppContainer,
    principal_id: str,
    run_id: str,
    candidate_ref: str,
    candidate: Mapping[str, object],
    now_epoch: int | None = None,
) -> str:
    secret = _property_original_tour_handoff_secret(container)
    normalized_principal_id = str(principal_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    normalized_candidate_ref = str(candidate_ref or "").strip()
    current_property_url = _property_candidate_current_url(candidate)
    if (
        not secret
        or not normalized_principal_id
        or not normalized_run_id
        or not normalized_candidate_ref
        or not current_property_url
        or len(normalized_run_id) > 256
        or len(normalized_candidate_ref) > 256
        or not _property_constant_text_equal(
            _property_candidate_ref(dict(candidate)),
            normalized_candidate_ref,
        )
        or not _property_original_tour_candidate_target(
            candidate,
            principal_id=normalized_principal_id,
        )
    ):
        return ""
    issued_at = int(time.time() if now_epoch is None else now_epoch)
    claims = {
        "v": 1,
        "p": hashlib.sha256(normalized_principal_id.encode("utf-8")).hexdigest(),
        "r": normalized_run_id,
        "c": normalized_candidate_ref,
        "u": hashlib.sha256(current_property_url.encode("utf-8")).hexdigest(),
        "e": issued_at + _PROPERTY_ORIGINAL_TOUR_HANDOFF_TTL_SECONDS,
    }
    payload = json.dumps(
        claims,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded_payload = _property_original_tour_b64encode(payload)
    signature = hmac.new(secret, encoded_payload.encode("ascii"), hashlib.sha256).digest()
    token = f"{encoded_payload}.{_property_original_tour_b64encode(signature)}"
    if len(token) > _PROPERTY_ORIGINAL_TOUR_HANDOFF_MAX_TOKEN_CHARS:
        return ""
    return f"/app/research-tour-handoff/{token}"


def _property_original_tour_handoff_claims(
    *,
    container: AppContainer,
    principal_id: str,
    token: str,
    now_epoch: int | None = None,
) -> dict[str, object]:
    normalized_token = str(token or "").strip()
    secret = _property_original_tour_handoff_secret(container)
    if (
        not secret
        or not normalized_token
        or len(normalized_token) > _PROPERTY_ORIGINAL_TOUR_HANDOFF_MAX_TOKEN_CHARS
        or normalized_token.count(".") != 1
    ):
        return {}
    encoded_payload, encoded_signature = normalized_token.split(".", 1)
    supplied_signature = _property_original_tour_b64decode(
        encoded_signature,
        max_bytes=64,
    )
    if supplied_signature is None or len(supplied_signature) != hashlib.sha256().digest_size:
        return {}
    try:
        signature_message = encoded_payload.encode("ascii")
    except UnicodeEncodeError:
        return {}
    expected_signature = hmac.new(
        secret,
        signature_message,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected_signature, supplied_signature):
        return {}
    payload = _property_original_tour_b64decode(encoded_payload, max_bytes=1024)
    if payload is None:
        return {}
    try:
        claims = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(claims, dict) or set(claims) != {"v", "p", "r", "c", "u", "e"}:
        return {}
    normalized_principal_id = str(principal_id or "").strip()
    principal_digest = str(claims.get("p") or "")
    property_digest = str(claims.get("u") or "")
    run_id = str(claims.get("r") or "").strip()
    candidate_ref = str(claims.get("c") or "").strip()
    try:
        expires_at = int(claims.get("e"))
        version = int(claims.get("v"))
    except (TypeError, ValueError, OverflowError):
        return {}
    current_epoch = int(time.time() if now_epoch is None else now_epoch)
    expected_principal_digest = hashlib.sha256(
        normalized_principal_id.encode("utf-8")
    ).hexdigest()
    if (
        version != 1
        or not normalized_principal_id
        or not run_id
        or not candidate_ref
        or len(run_id) > 256
        or len(candidate_ref) > 256
        or re.fullmatch(r"[0-9a-f]{64}", principal_digest) is None
        or re.fullmatch(r"[0-9a-f]{64}", property_digest) is None
        or not _property_constant_text_equal(
            principal_digest,
            expected_principal_digest,
        )
        or expires_at < current_epoch
        or expires_at > current_epoch + _PROPERTY_ORIGINAL_TOUR_HANDOFF_TTL_SECONDS
    ):
        return {}
    return {
        "run_id": run_id,
        "candidate_ref": candidate_ref,
        "property_url_sha256": property_digest,
    }


def _property_research_media_with_original_tour_handoff(
    research_media: Mapping[str, object],
    *,
    handoff_href: str,
) -> dict[str, object]:
    normalized_media = dict(research_media)
    normalized_handoff_href = str(handoff_href or "").strip()
    if not normalized_handoff_href:
        return normalized_media
    original_tour_updates: dict[str, object] = {
        "vendor_ready": True,
        "vendor_tour_href": normalized_handoff_href,
        "show_status_line": True,
    }
    existing_first_party_tour = bool(
        normalized_media.get("hosted_ready")
        or normalized_media.get("generated_reconstruction_ready")
        or normalized_media.get("ai_360_ready")
    )
    if existing_first_party_tour:
        original_tour_updates.update(
            {
                "tertiary_href": normalized_handoff_href,
                "tertiary_label": "Open original tour",
            }
        )
    else:
        original_tour_updates.update(
            {
                "status_label": "Original tour available",
                "status_detail": (
                    "The original tour is available through a secure PropertyQuarry link. "
                    "The in-page 3D viewer is still unavailable."
                ),
                "primary_href": normalized_handoff_href,
                "primary_label": "Open original tour",
                "provider_label": "Original 3D tour",
                "provider_key": "matterport",
            }
        )
    return {
        **normalized_media,
        **original_tour_updates,
    }


def _property_original_tour_candidate_from_exact_run_payload(
    *,
    run_payload: Mapping[str, object],
    principal_id: str,
    run_id: str,
    candidate_ref: str,
) -> dict[str, object] | None:
    normalized_principal_id = str(principal_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    normalized_candidate_ref = str(candidate_ref or "").strip()
    returned_run_id = str(run_payload.get("run_id") or "").strip()
    returned_principal_id = str(run_payload.get("principal_id") or "").strip()
    if (
        not normalized_principal_id
        or not normalized_run_id
        or not normalized_candidate_ref
        or not returned_run_id
        or not _property_constant_text_equal(returned_run_id, normalized_run_id)
        or not returned_principal_id
        or not _property_constant_text_equal(
            returned_principal_id,
            normalized_principal_id,
        )
    ):
        return None
    candidate = _property_lookup_candidate(
        property_context={"run": dict(run_payload)},
        candidate_ref=normalized_candidate_ref,
    )
    if candidate is None or not _property_constant_text_equal(
        _property_candidate_ref(candidate),
        normalized_candidate_ref,
    ):
        return None
    return candidate


def _property_original_tour_exact_run_candidate(
    *,
    product: Any,
    principal_id: str,
    account_email: str,
    run_id: str,
    candidate_ref: str,
) -> dict[str, object] | None:
    normalized_principal_id = str(principal_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    normalized_candidate_ref = str(candidate_ref or "").strip()
    if (
        not normalized_principal_id
        or not normalized_run_id
        or not normalized_candidate_ref
    ):
        return None
    indexed_candidate, _matched_run_id = _property_lookup_indexed_candidate(
        product,
        principal_id=normalized_principal_id,
        access_email=str(account_email or "").strip(),
        candidate_ref=normalized_candidate_ref,
        run_id=normalized_run_id,
    )
    if indexed_candidate is not None:
        return indexed_candidate
    try:
        run_payload = dict(
            product.get_property_search_run_status(
                principal_id=normalized_principal_id,
                run_id=normalized_run_id,
                lightweight=True,
                account_email=str(account_email or "").strip(),
            )
            or {}
        )
    except TypeError:
        try:
            run_payload = dict(
                product.get_property_search_run_status(
                    principal_id=normalized_principal_id,
                    run_id=normalized_run_id,
                    lightweight=True,
                )
                or {}
            )
        except Exception:
            return None
    except Exception:
        return None
    return _property_original_tour_candidate_from_exact_run_payload(
        run_payload=run_payload,
        principal_id=normalized_principal_id,
        run_id=normalized_run_id,
        candidate_ref=normalized_candidate_ref,
    )


def _property_original_tour_unavailable_response() -> PlainTextResponse:
    return PlainTextResponse(
        "Original tour unavailable.",
        status_code=404,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet",
        },
    )


@router.get("/app", response_class=HTMLResponse)
def app_root(request: Request) -> RedirectResponse:
    return RedirectResponse(str(request_brand(request).get("app_home") or "/app/search"), status_code=307)


@router.get("/app/research-tour-handoff/{token}", include_in_schema=False)
def property_original_tour_handoff(
    token: str,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> Response:
    if not _property_original_tour_authenticated(context):
        return _property_original_tour_unavailable_response()
    claims = _property_original_tour_handoff_claims(
        container=container,
        principal_id=context.principal_id,
        token=token,
    )
    run_id = str(claims.get("run_id") or "").strip()
    candidate_ref = str(claims.get("candidate_ref") or "").strip()
    if not run_id or not candidate_ref:
        return _property_original_tour_unavailable_response()
    product = build_product_service(container)
    candidate = _property_original_tour_exact_run_candidate(
        product=product,
        principal_id=context.principal_id,
        account_email=context.access_email,
        run_id=run_id,
        candidate_ref=candidate_ref,
    )
    if candidate is None:
        return _property_original_tour_unavailable_response()
    current_property_url = _property_candidate_current_url(candidate)
    current_property_digest = hashlib.sha256(
        current_property_url.encode("utf-8")
    ).hexdigest()
    if (
        not current_property_url
        or not _property_constant_text_equal(
            current_property_digest,
            str(claims.get("property_url_sha256") or ""),
        )
    ):
        return _property_original_tour_unavailable_response()
    target = _property_original_tour_candidate_target(
        candidate,
        principal_id=context.principal_id,
    )
    if not target:
        return _property_original_tour_unavailable_response()
    response = RedirectResponse(target, status_code=303)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response


@router.get("/app/research/{run_id}/{candidate_ref}", response_class=HTMLResponse)
def property_research_packet_legacy(
    run_id: str,
    candidate_ref: str,
    request: Request,
) -> RedirectResponse:
    query = list(request.query_params.multi_items())
    merged: dict[str, str] = {str(key): str(value) for key, value in query}
    merged["run_id"] = str(run_id or "").strip()
    target = f"/app/research/{urllib.parse.quote(str(candidate_ref or '').strip(), safe='')}"
    encoded_query = urllib.parse.urlencode(list(merged.items()))
    if encoded_query:
        target = f"{target}?{encoded_query}"
    return RedirectResponse(target, status_code=307)


@router.get("/app/research/{candidate_ref}", response_class=HTMLResponse)
def property_research_packet(
    candidate_ref: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
    run_id: str = Query(default=""),
    investment: int = Query(default=0),
) -> HTMLResponse:
    property_brand = request_brand(request)["key"] == "propertyquarry"
    release_probe_read_only = (
        str(context.auth_source or "").strip() == "propertyquarry_release_probe"
    )
    status = (
        container.onboarding.compact_status(principal_id=context.principal_id)
        if property_brand and hasattr(container.onboarding, "compact_status")
        else container.onboarding.status(principal_id=context.principal_id)
    )
    product = build_product_service(container)
    property_context = _property_console_context(
        container=container,
        principal_id=context.principal_id,
        access_email=context.access_email,
        status=status,
        run_id=run_id,
        selected_candidate_ref=candidate_ref,
        surface_mode="research",
        defer_run_hydration=release_probe_read_only,
    )
    raw_requested_run_value = property_context.pop("_raw_requested_run", {})
    raw_requested_run_payload = (
        dict(raw_requested_run_value)
        if isinstance(raw_requested_run_value, dict)
        else {}
    )
    normalized_candidate_ref = str(candidate_ref or "").strip()
    requested_run_id = str(run_id or "").strip()
    resolved_run_id = requested_run_id
    candidate, indexed_run_id = _property_lookup_indexed_candidate(
        product,
        principal_id=context.principal_id,
        access_email=context.access_email,
        candidate_ref=normalized_candidate_ref,
        run_id=requested_run_id,
    )
    candidate_from_exact_index = bool(
        candidate is not None
        and requested_run_id
        and _property_constant_text_equal(indexed_run_id, requested_run_id)
    )
    if candidate is None:
        candidate = _property_lookup_candidate(
            property_context=property_context,
            candidate_ref=normalized_candidate_ref,
        )
    elif not resolved_run_id:
        resolved_run_id = indexed_run_id
    if candidate is None:
        candidate = _property_lookup_candidate_in_saved_shortlist(
            product,
            principal_id=context.principal_id,
            candidate_ref=normalized_candidate_ref,
        )
        saved_shortlist_run_id = str(candidate.get("saved_from_run_id") or "").strip() if isinstance(candidate, dict) else ""
        if candidate is not None and saved_shortlist_run_id and saved_shortlist_run_id != resolved_run_id:
            resolved_run_id = saved_shortlist_run_id
            try:
                property_context["run"] = _propertyquarry_prepare_run_payload(
                    product=product,
                    run_payload=dict(
                        product.get_property_search_run_status(
                            principal_id=context.principal_id,
                            run_id=resolved_run_id,
                            account_email=context.access_email,
                        )
                        or {}
                    ),
                    backfill_cached_previews=False,
                    principal_id=context.principal_id,
                )
            except TypeError:
                with contextlib.suppress(Exception):
                    property_context["run"] = _propertyquarry_prepare_run_payload(
                        product=product,
                        run_payload=dict(
                            product.get_property_search_run_status(
                                principal_id=context.principal_id,
                                run_id=resolved_run_id,
                            )
                            or {}
                        ),
                        backfill_cached_previews=False,
                        principal_id=context.principal_id,
                    )
            except Exception:
                pass
    if candidate is None:
        candidate, matched_run_id = _property_lookup_candidate_across_runs(
            product,
            principal_id=context.principal_id,
            access_email=context.access_email,
            candidate_ref=normalized_candidate_ref,
            run_id=resolved_run_id,
        )
        if candidate is not None and matched_run_id and matched_run_id != resolved_run_id:
            resolved_run_id = matched_run_id
            try:
                property_context["run"] = _propertyquarry_prepare_run_payload(
                    product=product,
                    run_payload=dict(
                        product.get_property_search_run_status(
                            principal_id=context.principal_id,
                            run_id=resolved_run_id,
                            account_email=context.access_email,
                        )
                        or {}
                    ),
                    backfill_cached_previews=False,
                    principal_id=context.principal_id,
                )
            except TypeError:
                with contextlib.suppress(Exception):
                    property_context["run"] = _propertyquarry_prepare_run_payload(
                        product=product,
                        run_payload=dict(
                            product.get_property_search_run_status(
                                principal_id=context.principal_id,
                                run_id=resolved_run_id,
                            )
                            or {}
                        ),
                        backfill_cached_previews=False,
                        principal_id=context.principal_id,
                    )
            except Exception:
                pass
    if candidate is not None:
        candidate = _propertyquarry_refresh_candidate_preview_if_needed(
            product=product,
            candidate=candidate,
            allow_network=False,
        )
    if candidate is not None and not resolved_run_id:
        resolved_run_id = str(candidate.get("saved_from_run_id") or "").strip()
    effective_run_id = str(resolved_run_id or "").strip()
    research_route_recovery = (
        {
            "title": "Opened the current match",
            "detail": "This link moved, so PropertyQuarry opened the matching home from your latest search.",
            "requested_run_id": requested_run_id,
            "run_id": effective_run_id,
            "action_href": f"/app/shortlist?run_id={urllib.parse.quote(effective_run_id, safe='')}",
            "action_label": "Open search results",
        }
        if requested_run_id and effective_run_id and effective_run_id != requested_run_id
        else {}
    )
    if candidate is None:
        return _property_missing_packet_response(
            request,
            container=container,
            principal_id=context.principal_id,
            run_id=resolved_run_id,
            candidate_ref=normalized_candidate_ref,
            allow_repair=not release_probe_read_only,
        )
    workspace = dict(status.get("workspace") or {})
    assessment = dict(candidate.get("assessment") or {})
    preferences = dict(property_context.get("preferences") or {})
    workspace_preferences = (
        dict(status.get("property_search_preferences") or {})
        if isinstance(status.get("property_search_preferences"), dict)
        else {}
    )
    run_preferences_payload = (
        dict(property_context.get("run", {}).get("property_search_preferences") or property_context.get("run", {}).get("preferences") or {})
        if isinstance(property_context.get("run"), dict)
        and isinstance(
            property_context.get("run", {}).get("property_search_preferences") or property_context.get("run", {}).get("preferences"),
            dict,
        )
        else {}
    )
    workspace_distance_overlay = _property_research_workspace_distance_overlay(
        workspace_preferences=workspace_preferences,
        current_preferences=preferences,
        run_preferences=run_preferences_payload,
        candidate=candidate,
    )
    if workspace_distance_overlay:
        preferences = {
            **preferences,
            **workspace_distance_overlay,
        }
    search_agent_overlay_preferences = {
        **workspace_preferences,
        **preferences,
    }
    search_agent_distance_overlay = _property_research_search_agent_distance_overlay(
        preferences=search_agent_overlay_preferences,
        run_preferences=run_preferences_payload,
        candidate=candidate,
    )
    if search_agent_distance_overlay:
        preferences = {
            **preferences,
            **search_agent_distance_overlay,
        }
    recent_run_distance_overlay = _property_research_recent_run_distance_overlay(
        product=product,
        principal_id=context.principal_id,
        account_email=context.access_email,
        current_preferences=preferences,
        workspace_preferences=workspace_preferences,
        run_preferences=run_preferences_payload,
        candidate=candidate,
        current_run_id=effective_run_id,
    )
    if recent_run_distance_overlay:
        preferences = {
            **preferences,
            **recent_run_distance_overlay,
        }
    if run_preferences_payload:
        preferences = {
            **preferences,
            **_property_research_distance_preference_overlay(run_preferences_payload, active_only=True),
        }
    facts = _property_enriched_candidate_facts(
        candidate=candidate,
        preferences=preferences,
        # Market formatting follows the candidate's exact run, not mutable
        # workspace defaults. An absent run market therefore fails closed
        # unless the listing facts themselves carry an explicit country.
        market_preferences=run_preferences_payload,
        force_source_research_for_selected_distances=bool(recent_run_distance_overlay),
        allow_source_research=False,
        allow_location_hint_research=False,
    )
    fact_preferences = dict(preferences)
    candidate_requirement_plan = [
        dict(row)
        for row in list(candidate.get("fact_requirement_plan") or [])
        if isinstance(row, dict)
    ]
    required_fact_keys = [
        str(row.get("key") or "").strip()
        for row in candidate_requirement_plan
        if str(row.get("priority") or "").strip().lower() == "required"
        and str(row.get("key") or "").strip()
    ]
    lazy_fact_keys = [
        str(row.get("key") or "").strip()
        for row in candidate_requirement_plan
        if str(row.get("priority") or "lazy").strip().lower() != "required"
        and str(row.get("key") or "").strip()
    ]
    if required_fact_keys:
        fact_preferences["required_fact_keys"] = required_fact_keys
    if lazy_fact_keys:
        fact_preferences["lazy_fact_keys"] = lazy_fact_keys
    fact_plan = property_fact_requirement_plan(
        facts=facts,
        preferences=fact_preferences,
        include_resolved=True,
        property_url=(
            candidate.get("property_url")
            or candidate.get("url")
            or candidate.get("source_url")
            or facts.get("property_url")
            or facts.get("source_url")
            or ""
        ),
    )
    fact_candidate = {
        **dict(candidate),
        "property_facts": facts,
    }
    projected_fact_fields: list[dict[str, object]] = []
    for raw_row in fact_plan:
        row = dict(raw_row)
        priority = str(row.get("priority") or "lazy").strip().lower() or "lazy"
        state = str(row.get("state") or "unknown").strip().lower() or "unknown"
        if state != "resolved":
            row["value"] = None
            row["display_value"] = ""
            row["evidence"] = {}
        if priority == "required" and state != "resolved":
            row["state"] = "unavailable"
            row["error"] = {
                "code": "required_fact_not_resolved_during_search",
                "message": (
                    "This required distance was not verified during search. "
                    "Run the search again to verify it before ranking."
                ),
                "retry_after_seconds": 0,
            }
        projected_fact_fields.append(row)
    lazy_fact_pending = any(
        str(row.get("priority") or "lazy").strip().lower() != "required"
        and str(row.get("state") or "unknown").strip().lower()
        in {"unknown", "stale", "queued", "running"}
        for row in projected_fact_fields
    )
    fact_enrichment: dict[str, object] = {
        "schema_version": PROPERTY_FACT_ENRICHMENT_SCHEMA_VERSION,
        "job_id": "",
        "bundle_kind": "optional-geo-v1",
        "status": "idle",
        "attempt": 0,
        "poll_after_ms": 1200,
        "updated_at": "",
        "fields": projected_fact_fields,
        "score": property_fact_score_projection(
            candidate=fact_candidate,
            plan=fact_plan,
            preferences=fact_preferences,
        ),
        "retryable": False,
        "run_id": effective_run_id,
        "candidate_ref": normalized_candidate_ref,
        "status_url": "",
        "start_url": "",
    }
    fact_enrichment_requests_allowed = (
        context.authenticated is True
        and str(context.auth_source or "").strip().lower()
        not in {"", "anonymous", "loopback_no_auth"}
        and not release_probe_read_only
    )
    if effective_run_id:
        fact_endpoint = (
            f"/app/api/signals/property/search/run/{urllib.parse.quote(effective_run_id, safe='')}"
            f"/candidates/{urllib.parse.quote(normalized_candidate_ref, safe='')}/fact-enrichment"
        )
        if fact_enrichment_requests_allowed:
            fact_enrichment["status_url"] = fact_endpoint
            if lazy_fact_pending:
                fact_enrichment["start_url"] = fact_endpoint
        try:
            persisted_fact_enrichment = product.get_property_candidate_fact_enrichment(
                principal_id=context.principal_id,
                run_id=effective_run_id,
                candidate_ref=normalized_candidate_ref,
            )
        except Exception:
            persisted_fact_enrichment = None
        if isinstance(persisted_fact_enrichment, dict):
            persisted_fields_by_key = {
                str(row.get("key") or "").strip(): dict(row)
                for row in list(persisted_fact_enrichment.get("fields") or [])
                if isinstance(row, dict) and str(row.get("key") or "").strip()
            }
            persisted_bundle_kind = str(
                persisted_fact_enrichment.get("bundle_kind") or ""
            ).strip().lower()
            merged_fact_fields: list[dict[str, object]] = []
            for projected_row in projected_fact_fields:
                key = str(projected_row.get("key") or "").strip()
                persisted_row = persisted_fields_by_key.get(key)
                if persisted_row is None:
                    merged_fact_fields.append(dict(projected_row))
                    continue
                priority = str(
                    projected_row.get("priority") or "lazy"
                ).strip().lower() or "lazy"
                persisted_state = str(
                    persisted_row.get("state") or "unknown"
                ).strip().lower() or "unknown"
                genuine_required_job = persisted_bundle_kind == "required-geo-v1"
                if (
                    priority == "required"
                    and not genuine_required_job
                    and persisted_state != "resolved"
                ):
                    merged_fact_fields.append(dict(projected_row))
                    continue
                merged_row = {**projected_row, **persisted_row}
                merged_row["key"] = key
                merged_row["priority"] = priority
                merged_fact_fields.append(merged_row)
            fact_enrichment.update(persisted_fact_enrichment)
            fact_enrichment["fields"] = merged_fact_fields
            fact_enrichment["status_url"] = fact_endpoint if fact_enrichment_requests_allowed else ""
            fact_enrichment["start_url"] = (
                fact_endpoint
                if fact_enrichment_requests_allowed and lazy_fact_pending
                else ""
            )
    presentation_facts = dict(facts)
    fact_specs_by_key = {
        str(spec.get("key") or "").strip(): spec
        for spec in property_fact_distance_specs()
        if str(spec.get("key") or "").strip()
    }
    presentation_evidence = (
        dict(presentation_facts.get("property_fact_evidence") or {})
        if isinstance(presentation_facts.get("property_fact_evidence"), dict)
        else {}
    )
    for row in fact_plan:
        if str(row.get("state") or "unknown").strip().lower() == "resolved":
            continue
        key = str(row.get("key") or "").strip()
        spec = fact_specs_by_key.get(key, {})
        aliases = {
            key,
            str(row.get("source_key") or "").strip(),
            *(
                str(alias or "").strip()
                for alias in list(spec.get("aliases") or [])
            ),
        }
        for alias in aliases:
            if not alias:
                continue
            presentation_facts.pop(alias, None)
            presentation_evidence.pop(alias, None)
            prefix = alias[:-2] if alias.endswith("_m") else alias
            for suffix in (
                "name",
                "source",
                "lat",
                "lng",
                "latitude",
                "longitude",
                "distance_method",
            ):
                presentation_facts.pop(f"{prefix}_{suffix}", None)
    if presentation_evidence:
        presentation_facts["property_fact_evidence"] = presentation_evidence
    else:
        presentation_facts.pop("property_fact_evidence", None)
    facts = presentation_facts
    candidate = {
        **dict(candidate),
        "property_facts": facts,
    }
    commercial = dict(property_context.get("commercial") or {})
    match_reasons = [
        _clean_property_candidate_detail_copy(item)
        for item in list(candidate.get("match_reasons") or [])
        if _clean_property_candidate_detail_copy(item)
    ]
    mismatch_reasons = _property_normalized_mismatch_reasons(
        [_clean_property_candidate_copy(item) for item in list(candidate.get("mismatch_reasons") or []) if _clean_property_candidate_copy(item)],
        facts=facts,
        preferences=preferences,
    )
    fit_summary = _clean_property_candidate_detail_copy(candidate.get("fit_summary") or candidate.get("detail") or "")
    if not fit_summary:
        fit_summary = _clean_property_candidate_detail_copy(candidate.get("summary") or "")
    review_url = str(candidate.get("review_url") or "").strip()
    tour_url = str(candidate.get("tour_url") or "").strip()
    if tour_url and _property_hosted_tour_disabled_fallback(tour_url):
        candidate = dict(candidate)
        candidate["tour_url"] = ""
        if str(candidate.get("tour_status") or "").strip().lower() in {"created", "repairing"}:
            candidate["tour_status"] = ""
        tour_url = ""
    property_url = str(
        candidate.get("property_url")
        or candidate.get("listing_url")
        or candidate.get("source_url")
        or candidate.get("url")
        or ""
    ).strip()
    source_label = str(candidate.get("source_label") or "Property scout").strip() or "Property scout"
    display_source_label = _compact_provider_label(source_label) or source_label
    title = str(candidate.get("title") or property_url or "Property page").strip() or "Property page"
    display_title = _property_research_title_display(title)
    run_target = (
        f"/app/research/{candidate_ref}"
        + (f"?run_id={urllib.parse.quote(resolved_run_id, safe='')}" if str(resolved_run_id or "").strip() else "")
    )
    preference_person_id = str(preferences.get("preference_person_id") or "self").strip() or "self"
    packet_score_rows = _property_packet_score_rows(
        facts=facts,
        preferences=preferences,
        match_reasons=match_reasons,
        mismatch_reasons=mismatch_reasons,
    )
    everyday_fit_rows = _property_packet_everyday_fit_rows(
        facts=facts,
        preferences=preferences,
    )
    selected_distance_copy_scope = "current"
    if recent_run_distance_overlay:
        selected_distance_copy_scope = "recent"
    elif workspace_distance_overlay or search_agent_distance_overlay:
        selected_distance_copy_scope = "workspace"
    selected_distance_rows, selected_distance_copy = _property_distance_panel_rows(
        facts=facts,
        preferences=preferences,
        single_selected_distance_filter=True,
        copy_scope=selected_distance_copy_scope,
    )
    risk_fit_rows = _property_packet_risk_fit_rows(
        facts=facts,
        preferences=preferences,
    )
    missing_rows = _property_packet_missing_rows(
        facts=facts,
        preferences=preferences,
    )
    decision_rows = _property_packet_decision_rows(
        candidate=candidate,
        match_reasons=match_reasons,
        mismatch_reasons=mismatch_reasons,
        missing_rows=missing_rows,
        facts=facts,
        preferences=preferences,
    )
    provenance_rows = _property_packet_provenance_rows(facts)
    official_evidence_rows = _property_packet_official_evidence_rows(facts)
    official_posture_rows = _property_packet_official_posture_rows(facts)
    future_research_rows = _property_packet_future_research_rows(facts)
    evidence_overlay_rows = _property_packet_evidence_overlay_rows(facts=facts, candidate=candidate)
    investment_rows, investment_risk_rows = _property_investment_research_rows(
        property_url=property_url,
        facts=facts,
        preferences=preferences,
        commercial=commercial,
        requested=bool(int(investment or 0)),
    )
    ooda_summary_rows = [
        _object_detail_row("Best point", _clean_property_candidate_copy(match_reasons[0]), "Match")
        if match_reasons
        else _object_detail_row("Best point", fit_summary or "This home fits your search.", "Match"),
        _object_detail_row(
            "Best reason to act",
            _clean_property_candidate_detail_copy(str(decision_rows[0].get("detail") or fit_summary).strip())
            or "The current page sees enough signal to keep this home open.",
            "Summary",
        ),
        _object_detail_row("Main concern", _clean_property_candidate_copy(mismatch_reasons[0]), "Risk")
        if mismatch_reasons
        else _object_detail_row("Main concern", "Some details are still missing, so treat this as a property page, not a final decision.", "Risk"),
        _object_detail_row("Next step", str(candidate.get("tag") or candidate.get("recommendation") or "Home").strip() or "Home", "Decision"),
    ]
    for item in _property_missing_fact_items(facts):
        if str(item.get("status") or "").strip().lower() == "filled":
            continue
        if str(item.get("field") or "").strip().lower() == "rooms" and not _property_rooms_research_relevant(preferences):
            continue
        ooda = dict(item.get("ooda") or {}) if isinstance(item.get("ooda"), dict) else {}
        ooda_summary_rows.append(
            _object_detail_row(
                str(item.get("label") or item.get("field") or "Missing fact").strip(),
                str(ooda.get("orient") or ooda.get("act") or item.get("evidence") or "We are still checking this detail.").strip(),
                "To check",
            )
        )
    ooda_summary_rows.extend(_property_distance_ooda_rows_for_preferences(facts, preferences))
    onemin_evaluation = (
        dict(candidate.get("onemin_evaluation") or {})
        if isinstance(candidate.get("onemin_evaluation"), dict)
        else dict(fact_enrichment.get("onemin_evaluation") or {})
        if isinstance(fact_enrichment.get("onemin_evaluation"), dict)
        else {}
    )
    onemin_judgment = (
        dict(onemin_evaluation.get("judgment") or {})
        if isinstance(onemin_evaluation.get("judgment"), dict)
        else {}
    )
    onemin_ooda = (
        dict(onemin_evaluation.get("ooda") or {})
        if isinstance(onemin_evaluation.get("ooda"), dict)
        else {}
    )
    onemin_rows: list[dict[str, object]] = []
    if (
        str(onemin_evaluation.get("status") or "").strip().lower() == "succeeded"
        and onemin_evaluation.get("manager_routed") is True
    ):
        recommendation = str(
            onemin_judgment.get("recommendation") or "consider"
        ).strip().replace("_", " ").title()
        try:
            confidence_percent = max(
                0,
                min(100, int(round(float(onemin_judgment.get("confidence") or 0) * 100))),
            )
        except (TypeError, ValueError):
            confidence_percent = 0
        summary_copy = str(onemin_judgment.get("summary") or "").strip()
        if summary_copy:
            onemin_rows.append(
                _object_detail_row(
                    "1minAI judgment",
                    summary_copy,
                    f"{recommendation} · {confidence_percent}% confidence",
                )
            )
        for strength in list(onemin_judgment.get("strengths") or [])[:2]:
            if str(strength or "").strip():
                onemin_rows.append(
                    _object_detail_row("Strength", str(strength).strip(), "1minAI")
                )
        for risk in list(onemin_judgment.get("risks") or [])[:2]:
            if str(risk or "").strip():
                onemin_rows.append(
                    _object_detail_row("Watch-out", str(risk).strip(), "1minAI")
                )
        for action in list(onemin_ooda.get("actions") or [])[:2]:
            if not isinstance(action, dict):
                continue
            action_status = str(action.get("status") or "planned").strip().lower()
            distance = action.get("observed_distance_m")
            place_name = str(action.get("place_name") or "").strip()
            if action_status == "verified" and distance not in (None, ""):
                action_detail = (
                    f"Google Maps verified {place_name or 'the matching place'} "
                    f"at {int(distance):,} m straight-line distance."
                )
            else:
                blockers = ", ".join(
                    str(item).replace("_", " ")
                    for item in list(action.get("blockers") or [])[:2]
                    if str(item or "").strip()
                )
                action_detail = blockers or str(action.get("reason") or "Research planned.").strip()
            onemin_rows.append(
                _object_detail_row(
                    str(action.get("label") or action.get("fact_key") or "Maps research").strip(),
                    action_detail,
                    f"Google Maps · {action_status.replace('_', ' ').title()}",
                )
            )
    investment_run_target = run_target + ("&investment=1" if "?" in run_target else "?investment=1")
    try:
        feedback_suggestions = dict(product.property_feedback_suggestions(property_facts=facts, assessment=assessment or candidate))
    except Exception:
        feedback_suggestions = {"negative": [], "positive": []}
    # Keep the initial property page fast and content-first.
    # Detailed collaboration history belongs behind explicit follow-up actions,
    # not on the critical render path for the mobile/desktop property page.
    feedback_summary: dict[str, object] = {}
    property_timeline_rows: list[dict[str, object]] = []
    structured_feedback_rows: list[dict[str, object]] = []
    objection_clusters = [
        _object_detail_row(
            str(cluster.get("theme") or "feedback").replace("_", " ").title(),
            str(cluster.get("summary") or "Feedback summary is waiting for a recorded reason.").strip(),
            str(cluster.get("severity") or "medium").strip().title(),
        )
        for cluster in list(feedback_summary.get("clusters") or [])[:4]
        if isinstance(cluster, dict)
    ]
    if not objection_clusters:
        objection_clusters = [
            _object_detail_row(
                "No saved concerns yet",
                "Saved concerns from later reviews will appear here.",
                "Waiting",
            )
        ]
    household_review = dict(feedback_summary.get("household_review") or {}) if isinstance(feedback_summary.get("household_review"), dict) else {}
    household_rows = [
        _object_detail_row(
            str(row.get("stakeholder_label") or "Stakeholder").strip(),
            str(row.get("reason") or "Household reason is waiting for a recorded decision.").strip(),
            str(row.get("decision") or "maybe").replace("_", " ").title(),
        )
        for row in list(household_review.get("stakeholders") or [])[:4]
        if isinstance(row, dict)
    ]
    if not household_rows:
        household_rows = [
            _object_detail_row(
                "No household notes yet",
                "Shared reactions can be saved here later.",
                "Waiting",
            )
        ]
    risk_signal_rows = [
        _object_detail_row(
            str(row.get("theme") or "risk").replace("_", " ").title(),
            str(row.get("summary") or "Nothing flagged yet.").strip(),
            str(row.get("reason_key") or "signal").replace("_", " ").title(),
        )
        for row in list(feedback_summary.get("risk_signal_candidates") or [])[:4]
        if isinstance(row, dict)
    ]
    if not risk_signal_rows:
        risk_signal_rows = [
            _object_detail_row(
                "No shared watch-out yet",
                "If later reviews surface a recurring issue, it will show up here.",
                "Waiting",
            )
        ]
    next_best_question = str(household_review.get("next_best_question") or "").strip()
    timeline_rows = [
        _object_detail_row(
            str(row.get("event_type") or "packet_update").replace("_", " ").title(),
            str(row.get("summary") or "Property state updated.").strip(),
            str(row.get("created_at") or "").replace("T", " ").replace("+00:00", " UTC")[:32] or "Timeline",
        )
        for row in property_timeline_rows[:6]
        if isinstance(row, dict)
    ]
    if not timeline_rows:
        timeline_rows = [
            _object_detail_row("Shortlist state", "This property page is ready for a decision and agent follow-up.", "Now"),
            _object_detail_row(
                "Tour state",
                _property_tour_detail_line(candidate, principal_id=context.principal_id),
                "3D tour",
            ),
            _object_detail_row("Feedback state", "No timeline events are recorded yet. The first saved decision will start the visible timeline.", "Waiting"),
        ]
    latest_magic_fit_scene: dict[str, object] = {}
    agent_question_rows = [
        _object_detail_row(
            f"Question {index + 1}",
            str(row.get("question") or "").strip(),
            str(row.get("status") or "suggested").replace("_", " ").title(),
        )
        for index, row in enumerate(list(feedback_suggestions.get("agent_questions") or [])[:5])
        if isinstance(row, dict) and str(row.get("question") or "").strip()
    ]
    if not agent_question_rows:
        agent_question_rows = [
            _object_detail_row(
                "No next question yet",
                "When a missing detail or open issue is saved, the next question will appear here.",
                "Waiting",
            )
        ]
    followup_rows = [
        {
            "feedback_id": str(row.get("feedback_id") or "").strip(),
            "title": str(row.get("text") or row.get("category") or "Follow-up").strip(),
            "detail": str(row.get("followup_note") or row.get("stakeholder_label") or row.get("stakeholder_id") or "").strip(),
            "tag": str(row.get("followup_status") or "suggested").replace("_", " ").title(),
        }
        for row in structured_feedback_rows
        if isinstance(row, dict) and str(row.get("category") or "").strip() == "question"
    ]
    if not followup_rows:
        followup_rows = [
            {
                "feedback_id": "",
                "title": "No follow-up yet",
                "detail": "Add a question here when you want to keep something open.",
                "tag": "Waiting",
            }
        ]
    detail_sections = _candidate_detail_sections(facts)
    research_run_payload = dict(property_context.get("run") or {}) if isinstance(property_context.get("run"), dict) else {}
    market_display = _property_market_display_context(
        facts=facts,
        observed_at=(
            research_run_payload.get("updated_at")
            or research_run_payload.get("generated_at")
            or research_run_payload.get("created_at")
            or status.get("updated_at")
            or status.get("generated_at")
            or candidate.get("updated_at")
            or candidate.get("generated_at")
            or ""
        ),
    )
    market_currency_code = str(market_display.get("currency_code") or facts.get("currency_code") or "EUR").strip()
    localized_price_summary = ""
    for market_price_value in (
        facts.get("price_eur"),
        facts.get("warm_rent_eur"),
        facts.get("cold_rent_eur"),
    ):
        if market_price_value in (None, "", []):
            continue
        localized_price_summary = _property_market_currency_display(
            market_price_value,
            facts=facts,
            currency_code=market_currency_code,
        )
        if localized_price_summary:
            break
    price_summary = str(
        localized_price_summary
        or facts.get("price_display")
        or facts.get("rent_display")
        or facts.get("price")
        or _property_research_money_display(facts.get("price_eur"), currency_code=market_currency_code)
        or ""
    ).strip()
    if not price_summary or price_summary.lower() == "n/a":
        price_summary = _property_title_price_fallback(title)
    area_value = facts.get("area_m2") or facts.get("living_area_m2") or ""
    area_summary = _property_market_decimal_display(area_value, facts=facts) or str(area_value).strip()
    rooms_value = facts.get("rooms") or facts.get("room_count") or ""
    rooms_summary = _property_market_decimal_display(rooms_value, facts=facts) or _property_research_compact_fact(
        facts.get("rooms_label") or rooms_value
    )
    localized_object_values = {
        "Living area": f"{area_summary} m²" if area_summary else "",
        "Rooms": rooms_summary,
    }
    detail_sections = {
        **detail_sections,
        "object_rows": [
            {
                **dict(row),
                "value": localized_object_values.get(str(row.get("label") or ""), str(row.get("value") or "")),
            }
            for row in list(detail_sections.get("object_rows") or [])
            if isinstance(row, dict)
        ],
        "cost_rows": [
            {
                **dict(row),
                "value": price_summary if str(row.get("label") or "") == "Rent / price" else str(row.get("value") or ""),
            }
            for row in list(detail_sections.get("cost_rows") or [])
            if isinstance(row, dict)
        ],
    }
    location_summary = str(
        candidate.get("location_label")
        or facts.get("district")
        or facts.get("postal_name")
        or facts.get("address")
        or source_label
        or ""
    ).strip()
    headline_summary_parts = [
        part
        for part in (
            price_summary,
            f"{area_summary} m²" if area_summary else "",
            rooms_summary,
            location_summary,
        )
        if str(part or "").strip()
    ]
    object_summary = " · ".join(headline_summary_parts) or display_source_label
    if price_summary and not any(str(row.get("label") or "").strip().lower() == "rent / price" for row in list(detail_sections.get("cost_rows") or [])):
        detail_sections["cost_rows"] = [{"label": "Rent / price", "value": price_summary}] + list(detail_sections.get("cost_rows") or [])
    research_media = _property_tour_media_payload(
        candidate,
        principal_id=context.principal_id,
    )
    original_tour_handoff_href = ""
    if (
        requested_run_id
        and effective_run_id
        and _property_constant_text_equal(effective_run_id, requested_run_id)
        and _property_original_tour_authenticated(context)
    ):
        original_tour_candidate = (
            dict(candidate)
            if candidate_from_exact_index
            else _property_original_tour_candidate_from_exact_run_payload(
                run_payload=raw_requested_run_payload,
                principal_id=context.principal_id,
                run_id=requested_run_id,
                candidate_ref=normalized_candidate_ref,
            )
        )
        if original_tour_candidate is not None:
            original_tour_handoff_href = _property_original_tour_handoff_href(
                container=container,
                principal_id=context.principal_id,
                run_id=requested_run_id,
                candidate_ref=normalized_candidate_ref,
                candidate=original_tour_candidate,
            )
    if original_tour_handoff_href:
        # A legacy Matterport source remains unavailable for local embedding;
        # only an authenticated, run-scoped first-party handoff is exposed.
        research_media = _property_research_media_with_original_tour_handoff(
            research_media,
            handoff_href=original_tour_handoff_href,
        )
    # From this boundary onward, raw persisted tour targets are diagnostic
    # inputs only. Ready, progress, embed, and action state must use the
    # canonical route returned by the verified tour contract.
    tour_url = str(research_media.get("canonical_tour_url") or "").strip()
    orientation_preview = _property_candidate_orientation_preview(candidate)
    preview_image = _property_candidate_preview_image(candidate)
    diorama_preview_image = _property_candidate_diorama_preview_image(candidate)
    if not diorama_preview_image:
        generated_reconstruction_href = str(research_media.get("generated_reconstruction_href") or "").strip()
        if generated_reconstruction_href and not _property_hosted_tour_disabled_fallback(generated_reconstruction_href):
            diorama_style_hint = str(candidate.get("diorama_style_hint") or "").strip()
            diorama_preview_image = (
                _hosted_property_tour_telegram_preview_image_url_for_style(
                    generated_reconstruction_href,
                    diorama_style_hint=diorama_style_hint,
                )
                or property_tour_hosting._hosted_property_tour_preview_image_url(generated_reconstruction_href)
            )
    hero_preview_image = diorama_preview_image or preview_image
    gallery_items = _property_research_gallery_items(
        candidate=candidate,
        facts=facts,
        preview_image=preview_image,
        latest_magic_fit_scene=latest_magic_fit_scene if isinstance(latest_magic_fit_scene, dict) else None,
    )
    location_preview = {
        "image_url": str(orientation_preview.get("image_url") or "").strip(),
        "map_url": str(orientation_preview.get("map_url") or "").strip(),
        "embed_url": _google_maps_embed_url(orientation_preview.get("map_url")),
        "title": str(orientation_preview.get("title") or location_summary or "Wider area").strip(),
        "alt": str(orientation_preview.get("alt") or f"Map around {location_summary or display_source_label}").strip(),
        "caption": str(orientation_preview.get("caption") or "").strip(),
        "district_rows": list(orientation_preview.get("district_rows") or []),
    }
    location_preview_image_url = str(location_preview.get("image_url") or "").strip()
    if gallery_items:
        seen_gallery_urls: set[str] = set()
        filtered_gallery_items: list[dict[str, object]] = []
        for item in gallery_items:
            if not isinstance(item, dict):
                continue
            item_url = str(item.get("url") or "").strip()
            if not item_url:
                continue
            if item_url == location_preview_image_url or item_url in seen_gallery_urls:
                continue
            seen_gallery_urls.add(item_url)
            filtered_gallery_items.append(dict(item))
        gallery_items = filtered_gallery_items
    flythrough_url = str(research_media.get("walkthrough_href") or "").strip()
    hosted_tour_ready = bool(research_media.get("hosted_ready"))
    vendor_tour_ready = bool(research_media.get("vendor_ready"))
    vendor_tour_action_href = str(research_media.get("vendor_tour_href") or "").strip()
    generated_reconstruction_ready = bool(research_media.get("generated_reconstruction_ready"))
    ai_360_ready = bool(research_media.get("ai_360_ready"))
    generated_reconstruction_action_href = str(research_media.get("generated_reconstruction_href") or "").strip()
    tour_action_href = str(research_media.get("primary_href") or "").strip() if hosted_tour_ready else ""
    tour_status_value = (
        research_media.get("tour_status")
        if "tour_status" in research_media
        else candidate.get("tour_status")
    )
    tour_status = str(tour_status_value or "").strip().lower()
    tour_reason = str(research_media.get("tour_reason_key") or "").strip()
    tour_requestable = bool(research_media.get("tour_requestable"))
    flythrough_status = str(
        research_media.get("walkthrough_status")
        if "walkthrough_status" in research_media
        else candidate.get("flythrough_status")
    ).strip().lower()
    terminal_tour_status = _property_visual_terminal_status_for_reason(
        request_kind="tour",
        reason=tour_reason,
    )
    if terminal_tour_status and not tour_url and tour_status in {"", "queued", "pending", "processing", "running", "in_progress", "started", "rendering", "repairing"}:
        tour_status = terminal_tour_status
    flythrough_reason = str(candidate.get("flythrough_reason") or "").strip()
    flythrough_status_detail = str(research_media.get("walkthrough_status_detail") or "").strip()
    terminal_flythrough_status = _property_visual_terminal_status_for_reason(
        request_kind="flythrough",
        reason=flythrough_reason,
    )
    if terminal_flythrough_status and not flythrough_url and flythrough_status in {"", "queued", "pending", "processing", "running", "in_progress", "started", "rendering", "repairing"}:
        flythrough_status = terminal_flythrough_status
    eta_raw = str(candidate.get("tour_eta_minutes") or "").strip()
    flythrough_eta_raw = str(candidate.get("flythrough_eta_minutes") or "").strip()
    tour_requested_at = str(candidate.get("tour_requested_at") or "").strip()
    flythrough_requested_at = str(candidate.get("flythrough_requested_at") or "").strip()
    tour_status_updated_at = str(candidate.get("tour_status_updated_at") or "").strip()
    flythrough_status_updated_at = str(candidate.get("flythrough_status_updated_at") or "").strip()
    try:
        tour_progress_pct = int(float(str(
            research_media.get("tour_progress_pct")
            or candidate.get("tour_progress_pct")
            or 0
        ).strip()))
    except Exception:
        tour_progress_pct = 0
    try:
        flythrough_progress_pct = int(float(str(
            research_media.get("walkthrough_progress_pct")
            if "walkthrough_progress_pct" in research_media
            else candidate.get("flythrough_progress_pct")
            or 0
        ).strip()))
    except Exception:
        flythrough_progress_pct = 0
    tour_eta_label = str(research_media.get("tour_eta_label") or "").strip() or _property_visual_eta_label(
        request_kind="tour",
        status=tour_status,
        eta_minutes=eta_raw,
        requested_at=tour_requested_at,
        status_updated_at=tour_status_updated_at,
    )
    flythrough_eta_label = str(research_media.get("walkthrough_eta_label") or "").strip() or _property_visual_eta_label(
        request_kind="flythrough",
        status=flythrough_status,
        eta_minutes=flythrough_eta_raw,
        requested_at=flythrough_requested_at,
        status_updated_at=flythrough_status_updated_at,
    )
    if tour_progress_pct <= 0:
        tour_progress_pct = _property_visual_progress_pct(
            request_kind="tour",
            status=tour_status,
            ready_url=tour_url,
            eta_minutes=eta_raw,
            requested_at=tour_requested_at,
            status_updated_at=tour_status_updated_at,
        )
    if flythrough_progress_pct <= 0:
        flythrough_progress_pct = _property_visual_progress_pct(
            request_kind="flythrough",
            status=flythrough_status,
            ready_url=flythrough_url,
            eta_minutes=flythrough_eta_raw,
            requested_at=flythrough_requested_at,
            status_updated_at=flythrough_status_updated_at,
        )
    hero_actions: list[dict[str, object]] = []
    if property_url:
        hero_actions.append({"href": property_url, "label": "Open listing", "external": True})
    if hosted_tour_ready and tour_action_href:
        hero_actions.append({"href": tour_action_href, "label": str(research_media.get("primary_label") or "Open 3D tour").strip(), "external": False})
    elif generated_reconstruction_ready and generated_reconstruction_action_href:
        hero_actions.append(
            {
                "href": generated_reconstruction_action_href,
                "label": str(
                    research_media.get("generated_reconstruction_label")
                    or ("Open AI 360 tour" if ai_360_ready else "Open AI-generated 3D tour")
                ).strip(),
                "external": False,
                "kind": "ai_360_reconstruction" if ai_360_ready else "generated_reconstruction",
                "status_detail": str(
                    research_media.get("generated_reconstruction_status_detail")
                    or (
                        "AI reconstruction based on property photos; not a captured 360 or measured survey."
                        if ai_360_ready
                        else (
                            "AI-generated from the floor plan and listing photos; "
                            "not a captured 360° tour."
                        )
                    )
                ).strip(),
            }
        )
    elif vendor_tour_ready and vendor_tour_action_href:
        hero_actions.append(
            {
                "href": vendor_tour_action_href,
                "label": str(research_media.get("primary_label") or "Open original tour").strip(),
                "external": False,
            }
        )
    elif tour_url and not hosted_tour_ready and property_url and tour_requestable:
        hero_actions.append({"kind": "tour", "label": "Request 3D tour", "property_url": property_url, "state": "idle", "progress_pct": 0, "eta_label": "", "status_detail": "A real 3D tour is not available yet."})
    elif tour_status in {"queued", "pending"} and property_url:
        hero_actions.append({"kind": "tour", "label": "3D tour queued", "property_url": property_url, "state": "pending", "progress_pct": max(tour_progress_pct, 14), "eta_label": tour_eta_label, "status_detail": "Still queued." if tour_eta_label.startswith("delayed") else "Queued."})
    elif tour_status in {"processing", "running", "in_progress", "started", "rendering", "repairing"} and property_url:
        hero_actions.append({"kind": "tour", "label": "3D tour rendering", "property_url": property_url, "state": "rendering", "progress_pct": max(tour_progress_pct, 58), "eta_label": tour_eta_label, "status_detail": "Still rendering." if tour_eta_label.startswith("delayed") else "Rendering."})
    elif tour_status in {"blocked", "failed", "skipped", "not_applicable"} and property_url and tour_requestable:
        hero_actions.append(
            {
                "kind": "tour",
                "label": "Retry 3D tour" if tour_status in {"blocked", "failed"} else "Request 3D tour",
                "property_url": property_url,
                "state": tour_status or "blocked",
                "progress_pct": 0,
                "eta_label": "",
                "status_detail": str(research_media.get("status_detail") or "Tour not available yet.").strip(),
            }
        )
    elif property_url and tour_requestable:
        hero_actions.append({"kind": "tour", "label": "Request 3D tour", "property_url": property_url, "state": "idle", "progress_pct": 0, "eta_label": "", "status_detail": "Use the floor plan and photos to build one."})
    if (
        (hosted_tour_ready or generated_reconstruction_ready)
        and vendor_tour_ready
        and vendor_tour_action_href
        and not any(
            str(action.get("href") or "").strip() == vendor_tour_action_href
            for action in hero_actions
        )
    ):
        hero_actions.append(
            {
                "href": vendor_tour_action_href,
                "label": "Open original tour",
                "external": False,
            }
        )
    if flythrough_url:
        hero_actions.append({"href": flythrough_url, "label": "Open walkthrough", "external": False})
    elif flythrough_status in {"queued", "pending"} and property_url:
        hero_actions.append({"kind": "flythrough", "label": "Walkthrough queued", "property_url": property_url, "state": "pending", "progress_pct": max(flythrough_progress_pct, 18), "eta_label": flythrough_eta_label, "status_detail": flythrough_status_detail or ("Still queued." if flythrough_eta_label.startswith("delayed") else "Queued.")})
    elif flythrough_status in {"processing", "running", "in_progress", "started"} and property_url:
        hero_actions.append({"kind": "flythrough", "label": "Walkthrough rendering", "property_url": property_url, "state": "rendering", "progress_pct": max(flythrough_progress_pct, 64), "eta_label": flythrough_eta_label, "status_detail": flythrough_status_detail or ("Still rendering." if flythrough_eta_label.startswith("delayed") else "Rendering.")})
    elif flythrough_status in {"blocked", "failed", "skipped", "not_applicable"} and property_url:
        hero_actions.append(
            {
                "kind": "flythrough",
                "label": "Retry walkthrough" if flythrough_status in {"blocked", "failed"} else "Request walkthrough",
                "property_url": property_url,
                "state": flythrough_status or "blocked",
                "progress_pct": 0,
                "eta_label": "",
                "status_detail": flythrough_status_detail or _property_visual_unavailable_detail(request_kind="flythrough", reason=flythrough_reason),
            }
        )
    elif property_url:
        hero_actions.append({"kind": "flythrough", "label": "Request walkthrough", "property_url": property_url, "state": "idle", "progress_pct": 0, "eta_label": "", "status_detail": "Build from source material."})
    if str(candidate.get("packet_url") or review_url or "").strip():
        hero_actions.append({"href": str(candidate.get("packet_url") or review_url or "").strip(), "label": "Copy page link", "copy": True})
    visual_status_line = ""
    if hosted_tour_ready and tour_url:
        visual_status_line = str(research_media.get("status_detail") or "3D tour available.").strip()
    elif generated_reconstruction_ready:
        visual_status_line = str(
            research_media.get("generated_reconstruction_status_detail")
            or research_media.get("status_detail")
            or "AI-generated 3D tour is ready."
        ).strip()
    elif flythrough_url:
        visual_status_line = str(research_media.get("walkthrough_status_detail") or "Walkthrough is ready.").strip()
    elif flythrough_status in {"queued", "pending"}:
        visual_status_line = flythrough_status_detail or "Walkthrough queued."
    elif flythrough_status in {"processing", "running", "in_progress", "started"}:
        visual_status_line = flythrough_status_detail or "Walkthrough rendering."
    elif tour_status in {"queued", "pending"}:
        visual_status_line = "3D tour queued."
    elif tour_status in {"processing", "running", "in_progress", "started", "rendering", "repairing"}:
        visual_status_line = "3D tour rendering."
    elif tour_status in {"blocked", "failed", "skipped", "not_applicable"} or not tour_requestable:
        visual_status_line = str(
            research_media.get("status_detail")
            or "A real 3D tour needs a floor plan or verified 360 source."
        ).strip()
    overview_rows = [
        {"label": "Price", "value": price_summary or "Price on request"},
        {"label": "Area", "value": f"{area_summary} m²" if area_summary else "Not listed"},
        {"label": "Rooms", "value": rooms_summary or "Not listed"},
        {"label": "Location", "value": location_summary or display_source_label},
    ]
    if detail_sections.get("feature_values"):
        overview_rows.append({"label": "Highlights", "value": ", ".join(list(detail_sections.get("feature_values") or [])[:4])})
    research_sections: list[dict[str, object]] = [
        {
            "eyebrow": "At a glance",
            "title": "What stands out",
            "items": ooda_summary_rows[:6],
        },
    ]
    if onemin_rows:
        research_sections.insert(
            0,
            {
                "eyebrow": "1minAI via EA 1min Manager",
                "title": "AI property judgment",
                "copy": (
                    "A qualitative advisory read grounded in verified facts. "
                    "The deterministic PropertyQuarry fit score remains authoritative."
                ),
                "items": onemin_rows[:7],
            },
        )
    if packet_score_rows:
        research_sections.append(
            {
                "eyebrow": "Highlights",
                "title": "What stands out",
                "items": packet_score_rows[:5],
            }
        )
    research_sections.extend(
        [
            {
                "eyebrow": "Details",
                "title": "Details",
                "items": [_object_detail_row(str(row.get("label") or "").strip(), str(row.get("value") or "").strip(), "Listing") for row in list(detail_sections.get("object_rows") or [])],
            },
            {
                "eyebrow": "Costs",
                "title": "Price and running costs",
                "items": [_object_detail_row(str(row.get("label") or "").strip(), str(row.get("value") or "").strip(), "Listing") for row in list(detail_sections.get("cost_rows") or [])],
            },
            {
                "eyebrow": "About the home",
                "title": "About the home",
                "copy": str(detail_sections.get("description_text") or "").strip(),
                "items": [],
            },
            {
                "eyebrow": "Area",
                "title": "Daily life nearby",
                "copy": str(detail_sections.get("location_text") or "").strip(),
                "items": everyday_fit_rows[:6],
            },
            {
                "eyebrow": "Energy",
                "title": "Energy and heating",
                "items": [_object_detail_row(str(row.get("label") or "").strip(), str(row.get("value") or "").strip(), "Listing") for row in list(detail_sections.get("energy_rows") or [])],
            },
            {
                "eyebrow": "Still open",
                "title": "Still open",
                "items": (missing_rows + investment_risk_rows)[:10]
                or [_object_detail_row("Nothing major stands out yet", "A few quick checks may still be worth it, but nothing major stands out right now.", "Clear")],
            },
            {
                "eyebrow": "Next step",
                "title": "Next step",
                "items": decision_rows,
            },
            {
                "eyebrow": "Map and context",
                "title": "Map and context",
                "items": (official_evidence_rows[:4] + official_posture_rows[:3] + future_research_rows[:3] + provenance_rows[:3])
                or [_object_detail_row("No local notes yet", "Map layers and nearby context have not been added yet.", "Waiting")],
            },
            {
                "eyebrow": "Follow-up",
                "title": "Next to check",
                "items": agent_question_rows + ([_object_detail_row("Next household question", next_best_question, "Next")] if next_best_question else []),
            },
        ]
    )
    if timeline_rows:
        research_sections.append(
            {
                "eyebrow": "Updates",
                "title": "Updates",
                "items": timeline_rows,
            }
        )
    if str(preferences.get("listing_mode") or "").strip().lower() == "buy":
        research_sections.insert(
            7,
            {
                "eyebrow": "Investment",
                "title": "Investment view",
                "items": investment_rows
                or [_object_detail_row("Investment research is off", "Run the buy-side research pass if you need yield, reserve, and document-risk context.", "Optional")],
            },
        )
    feedback_assessment = dict(assessment or {})
    if not feedback_assessment:
        feedback_assessment = {
            "object_id": property_url,
            "object_type": "listing",
            "recommendation": str(candidate.get("recommendation") or candidate.get("tag") or "").strip(),
            "fit_score": candidate.get("fit_score") or candidate.get("score") or "",
            "confidence": candidate.get("confidence") or "",
            "match_reasons_json": match_reasons,
            "mismatch_reasons_json": mismatch_reasons,
            "unknowns_json": list(facts.get("unknowns_json") or candidate.get("unknowns_json") or []),
        }
    feedback_payload = {
        "person_id": preference_person_id,
        "profile_href": f"/app/properties" + (f"?run_id={urllib.parse.quote(effective_run_id, safe='')}" if effective_run_id else ""),
        "suggestions": feedback_suggestions,
        "property_url": property_url,
        "packet_href": f"/app/research/{urllib.parse.quote(candidate_ref, safe='')}" + (f"?run_id={urllib.parse.quote(effective_run_id, safe='')}" if effective_run_id else ""),
        "property_title": display_title,
        "property_facts": facts,
        "assessment": feedback_assessment,
        "investment_context": investment_rows + investment_risk_rows,
        "followup_rows": followup_rows,
        "magic_fit_scene": latest_magic_fit_scene,
        "property_slug": str(candidate_ref or "").strip(),
        "save_endpoint": f"/app/api/people/{urllib.parse.quote(preference_person_id, safe='')}/preference-profile/property-feedback",
        "clippy_endpoint": "/app/api/property/decision-copilot",
        "magic_fit_create_endpoint": "/app/api/property/magic-fit-scenes",
        "magic_fit_upload_endpoint": "/app/api/property/magic-fit-reference-files",
        "google_photos_session_endpoint": "/app/api/signals/google/photos/session",
        "google_photos_session_status_endpoint_template": "/app/api/signals/google/photos/session/__SESSION_ID__",
        "structured_feedback_endpoint": "/app/api/property-feedback",
        "followup_status_endpoint_template": "/app/api/property-feedback/__FEEDBACK_ID__/followup-status",
    }
    review_page_neuronwriter = dict(candidate.get("review_page_neuronwriter") or {})
    research_visual_default_style = str(PROPERTY_FURNITURE_STYLE_CATALOG[0]["value"]).strip() or "warm_scandi"
    research_visual_priority_queue_active = str(commercial.get("current_plan_key") or "").strip().lower() in {"plus", "agent"}
    research_snapshot = build_property_research_packet_snapshot(
        title=display_title,
        summary=object_summary,
        source_label=display_source_label,
        price=price_summary or "Price on request",
        area=f"{area_summary} m²" if area_summary else "",
        rooms=rooms_summary,
        location=location_summary or display_source_label,
        media=research_media,
        preview_image=hero_preview_image,
        gallery_items=gallery_items,
        location_preview=location_preview,
        actions=hero_actions,
        visual_status_line=visual_status_line,
        source_ref=str(candidate.get("source_ref") or "").strip(),
        run_id=effective_run_id,
        candidate_ref=str(candidate_ref or "").strip(),
        overview_rows=overview_rows,
        sections=research_sections,
        match_reasons=match_reasons,
        mismatch_reasons=mismatch_reasons,
        score_rows=packet_score_rows,
        listing_rows=list(detail_sections.get("object_rows") or []),
        cost_rows=list(detail_sections.get("cost_rows") or []),
        feature_values=list(detail_sections.get("feature_values") or []),
        description_text=str(detail_sections.get("description_text") or "").strip(),
        location_text=str(detail_sections.get("location_text") or "").strip(),
        energy_rows=list(detail_sections.get("energy_rows") or []),
        missing_rows=missing_rows,
        decision_rows=decision_rows,
        official_evidence_rows=official_evidence_rows,
        official_posture_rows=official_posture_rows,
        future_research_rows=future_research_rows,
        evidence_overlay_rows=evidence_overlay_rows,
        provenance_rows=provenance_rows,
        timeline_rows=timeline_rows,
        everyday_fit_rows=everyday_fit_rows,
        risk_fit_rows=risk_fit_rows,
        investment_rows=investment_rows,
        investment_risk_rows=investment_risk_rows,
        next_best_question=next_best_question,
        feedback=feedback_payload,
        neuronwriter=review_page_neuronwriter,
        objection_rows=objection_clusters,
        household_rows=household_rows,
        risk_signal_rows=risk_signal_rows,
    )
    return _render_public_template(
        request,
        "app/property_research_detail.html",
        **{
            **_console_shell_context(
                request=request,
                page_title=f"PropertyQuarry {display_title}",
                current_nav="research",
                context=context,
                console_title=display_title,
                console_summary=display_source_label,
                nav_groups=app_nav_groups_for_brand(request_brand(request)["key"]),
                workspace_label=str(workspace.get("name") or "PropertyQuarry"),
                cards=[],
                stats=[{"label": row["label"], "value": row["value"]} for row in overview_rows[:4]],
            ),
            **research_snapshot,
            "research_hero_preview_image": hero_preview_image,
            "research_hero_preview_kind": "diorama" if diorama_preview_image else ("photo" if preview_image else ""),
            "research_selected_distance_rows": selected_distance_rows,
            "research_selected_distance_copy": selected_distance_copy,
            "research_route_recovery": research_route_recovery,
            "research_fact_enrichment": fact_enrichment,
            "research_market": market_display,
            "research_visual_style_catalog": [dict(row) for row in PROPERTY_FURNITURE_STYLE_CATALOG],
            "research_default_visual_style": research_visual_default_style,
            "research_visual_priority_queue_active": research_visual_priority_queue_active,
        },
    )


@router.get("/app/properties/notifications/preview", response_class=HTMLResponse)
def property_notification_preview_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
    template: str = Query(default="search_results_ready"),
) -> HTMLResponse:
    del container, context
    templates_available = (
        "search_results_ready",
        "property_match",
        "tour_ready",
        "investment_research_ready",
        "workspace_invitation",
        "workspace_access",
        "google_connect",
        "market_ready",
    )
    selected = str(template or "search_results_ready").strip().lower() or "search_results_ready"
    if selected not in templates_available:
        selected = "search_results_ready"
    preview = property_notification_preview(selected)
    options = "".join(
        f'<option value="{html.escape(key)}" {"selected" if key == selected else ""}>{html.escape(key.replace("_", " "))}</option>'
        for key in templates_available
    )
    body = f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>PropertyQuarry notification preview</title>
      {_clickrank_head_snippet(_request_hostname(request), _request_path(request))}
      <style>
        body {{ margin:0; background:#f6f3ee; color:#242321; font:14px/1.6 Arial,Helvetica,sans-serif; }}
        .wrap {{ max-width:1280px; margin:0 auto; padding:24px; display:grid; gap:18px; }}
        .panel {{ background:#fffdf8; border:1px solid #ded6c8; border-radius:18px; padding:20px; }}
        .grid {{ display:grid; grid-template-columns:320px 1fr; gap:18px; }}
        .meta {{ color:#6c675f; font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
        h1,h2 {{ margin:0 0 12px; }}
        select {{ width:100%; min-height:42px; border:1px solid #b9ad9a; border-radius:10px; padding:0 12px; background:#fffdf8; }}
        pre {{ white-space:pre-wrap; word-break:break-word; background:#fdf9f1; border:1px solid #ded6c8; border-radius:12px; padding:14px; }}
        iframe {{ width:100%; min-height:720px; border:1px solid #ded6c8; border-radius:12px; background:#fff; }}
        .actions a {{ color:#825818; text-decoration:underline; text-underline-offset:3px; font-weight:700; }}
        @media (max-width: 980px) {{ .grid {{ grid-template-columns:1fr; }} iframe {{ min-height:560px; }} }}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="panel">
          <div class="meta">PropertyQuarry notification system</div>
          <h1>Email preview</h1>
          <div class="actions"><a href="/app/alerts">Back to alerts</a></div>
        </div>
        <div class="grid">
          <div class="panel">
            <form method="get" action="/app/properties/notifications/preview">
              <label class="meta" for="notification-template">Template</label>
              <select id="notification-template" name="template">{options}</select>
            </form>
            <script>document.querySelector('select[name="template"]')?.addEventListener('change', (event) => event.target.form.submit());</script>
            <div style="height:18px"></div>
            <div class="meta">Subject</div>
            <h2>{html.escape(str(preview.get("subject") or ""))}</h2>
            <div class="meta">Preheader</div>
            <p>{html.escape(str(preview.get("preheader") or ""))}</p>
            <div class="meta">Plain text</div>
            <pre>{html.escape(str(preview.get("text") or ""))}</pre>
          </div>
          <div class="panel">
            <div class="meta">HTML render</div>
            <iframe title="PropertyQuarry notification HTML preview" sandbox="" srcdoc="{html.escape(str(preview.get('html') or ''))}"></iframe>
          </div>
        </div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(body)


@router.get("/app/example/shortlist", response_class=HTMLResponse, include_in_schema=False)
def propertyquarry_example_shortlist_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
    candidate: str = Query(default=""),
) -> HTMLResponse:
    brand = request_brand(request)
    if str(brand.get("key") or "").strip() != "propertyquarry":
        return RedirectResponse("/", status_code=307)
    authenticated_principal = _landing_authenticated_principal(
        container=container,
        access_identity=access_identity,
        request=request,
    )
    rows = _propertyquarry_example_shortlist_rows()
    selected_key = str(candidate or "").strip() or str((rows[0] if rows else {}).get("candidate_key") or "")
    selected = next(
        (
            dict(row)
            for row in rows
            if str(row.get("candidate_key") or "").strip() == selected_key
        ),
        dict(rows[0] if rows else {}),
    )
    start_search_href = "/app/search" if authenticated_principal else "/sign-in?return_to=%2Fapp%2Fsearch"
    return _render_public_template(
        request,
        "propertyquarry_example_shortlist.html",
        **_public_context(
            request=request,
            current_nav="product",
            page_title="PropertyQuarry Example Shortlist",
            principal_id=authenticated_principal,
            status=_anonymous_onboarding_status(),
            access_identity=access_identity,
            extra={
                "example_rows": rows,
                "selected_example": selected,
                "start_search_href": start_search_href,
                "meta_description": "A minimal PropertyQuarry example shortlist showing how sample homes open before you start your own search.",
                "canonical_path": "/app/example/shortlist",
                "robots_directive": "noindex, follow",
            },
        ),
    )


@router.get("/app/assets/property-workbench.js", include_in_schema=False)
def propertyquarry_workbench_script_asset(
    request: Request,
    v: str = Query(default=""),
) -> Response:
    brand = request_brand(request)
    if str(brand.get("key") or "").strip() != "propertyquarry":
        raise HTTPException(status_code=404, detail="asset_not_found")
    body, etag = _property_workbench_script_asset()
    headers = _property_workbench_asset_headers(etag=etag, version=v)
    if str(request.headers.get("if-none-match") or "").strip() == etag:
        return Response(
            status_code=304,
            headers=headers,
        )
    return Response(
        body,
        media_type="application/javascript; charset=utf-8",
        headers=headers,
    )


@router.get("/app/assets/property-search-loader.js", include_in_schema=False)
def propertyquarry_search_loader_script_asset(
    request: Request,
    v: str = Query(default=""),
) -> Response:
    brand = request_brand(request)
    if str(brand.get("key") or "").strip() != "propertyquarry":
        raise HTTPException(status_code=404, detail="asset_not_found")
    body, etag = _property_search_loader_script_asset()
    headers = _property_workbench_asset_headers(etag=etag, version=v)
    if str(request.headers.get("if-none-match") or "").strip() == etag:
        return Response(
            status_code=304,
            headers=headers,
        )
    return Response(
        body,
        media_type="application/javascript; charset=utf-8",
        headers=headers,
    )


@router.get("/app/assets/property-workbench.css", include_in_schema=False)
def propertyquarry_workbench_style_asset(
    request: Request,
    surface: str = Query(default=""),
    v: str = Query(default=""),
) -> Response:
    brand = request_brand(request)
    if str(brand.get("key") or "").strip() != "propertyquarry":
        raise HTTPException(status_code=404, detail="asset_not_found")
    static_surface = str(surface or "").strip().lower() in {"static", "account", "billing", "agents", "alerts"}
    body, etag = _property_workbench_css_asset(static_surface=static_surface)
    if not body:
        raise HTTPException(status_code=404, detail="asset_not_found")
    headers = _property_workbench_asset_headers(etag=etag, version=v)
    if str(request.headers.get("if-none-match") or "").strip() == etag:
        return Response(
            status_code=304,
            headers=headers,
        )
    return Response(
        body,
        media_type="text/css; charset=utf-8",
        headers=headers,
    )


@router.get("/app/shortlist/run/{run_id}", response_class=HTMLResponse, include_in_schema=False)
def propertyquarry_fast_ranked_run_page(
    run_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context_if_available),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    brand = request_brand(request)
    if str(brand.get("key") or "").strip() != "propertyquarry":
        return RedirectResponse("/app/shortlist", status_code=307)
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id or len(normalized_run_id) > 160 or not re.fullmatch(r"[A-Za-z0-9._:-]+", normalized_run_id):
        raise HTTPException(status_code=404, detail="property_search_run_not_found")
    encoded_run_id = urllib.parse.quote(normalized_run_id, safe="")
    context_authenticated_principal = str(context.principal_id or "").strip() if context.authenticated else ""
    authenticated_principal = _landing_authenticated_principal(
        container=container,
        access_identity=access_identity,
        request=request,
    ) or context_authenticated_principal or str(request.headers.get("x-ea-principal-id") or "").strip()
    status_url = f"/app/api/signals/property/search/run/{encoded_run_id}?lightweight=1" if authenticated_principal else ""
    full_href = _propertyquarry_fast_ranked_run_href(normalized_run_id, full=True) if authenticated_principal else "/app/shortlist"
    initial_run_payload: dict[str, Any] = {}
    if authenticated_principal:
        product = build_product_service(container)
        if _propertyquarry_is_public_shortlist_entrypoint(normalized_run_id):
            latest_run = _propertyquarry_latest_shortlist_run_with_results(
                product=product,
                principal_id=authenticated_principal,
                allow_public_entrypoint_runs=False,
            )
            latest_run_id = str(latest_run.get("run_id") or "").strip()
            if latest_run_id and latest_run_id != normalized_run_id:
                return RedirectResponse(_propertyquarry_fast_ranked_run_href(latest_run_id), status_code=303)
        raw_initial_run_payload: dict[str, Any] = {}
        sample_payload_requested = False
        requested_run_is_recent: bool | None = None
        with contextlib.suppress(Exception):
            try:
                raw_initial_run_payload = dict(
                    product.get_property_search_run_status(
                        principal_id=authenticated_principal,
                        run_id=normalized_run_id,
                        lightweight=True,
                        account_email=str(context.access_email or "").strip(),
                    ) or {}
                )
            except TypeError:
                try:
                    raw_initial_run_payload = dict(
                        product.get_property_search_run_status(
                            principal_id=authenticated_principal,
                            run_id=normalized_run_id,
                            lightweight=True,
                        )
                        or {}
                    )
                except TypeError:
                    raw_initial_run_payload = dict(
                        product.get_property_search_run_status(
                            principal_id=authenticated_principal,
                            run_id=normalized_run_id,
                        )
                        or {}
                    )
            initial_run_payload = _propertyquarry_prepare_run_payload(
                product=product,
                run_payload=raw_initial_run_payload,
                principal_id=authenticated_principal,
            )
            sample_payload_requested = _propertyquarry_payload_is_example_shortlist(initial_run_payload or raw_initial_run_payload)
            if sample_payload_requested:
                raw_initial_run_payload = {}
                initial_run_payload = {}
                status_url = ""
                full_href = "/app/shortlist"
            elif raw_initial_run_payload:
                requested_run_is_recent = _propertyquarry_shortlist_run_is_recent(
                    product=product,
                    principal_id=authenticated_principal,
                    run_id=normalized_run_id,
                )
        if not raw_initial_run_payload or sample_payload_requested:
            latest_run = _propertyquarry_latest_shortlist_run_with_results(
                product=product,
                principal_id=authenticated_principal,
                allow_public_entrypoint_runs=False,
            )
            latest_run_id = str(latest_run.get("run_id") or "").strip()
            if latest_run_id and latest_run_id != normalized_run_id:
                return RedirectResponse(_propertyquarry_fast_ranked_run_href(latest_run_id), status_code=303)
            if latest_run_id == normalized_run_id:
                initial_run_payload = _propertyquarry_prepare_run_payload(
                    product=product,
                    run_payload=latest_run,
                    principal_id=authenticated_principal,
                )
        elif requested_run_is_recent is False:
            latest_run = _propertyquarry_latest_shortlist_run_with_results(
                product=product,
                principal_id=authenticated_principal,
                allow_public_entrypoint_runs=False,
            )
            latest_run_id = str(latest_run.get("run_id") or "").strip()
            if latest_run_id and latest_run_id != normalized_run_id:
                return RedirectResponse(_propertyquarry_fast_ranked_run_href(latest_run_id), status_code=303)
    else:
        initial_run_payload = _propertyquarry_fast_ranked_run_sample_payload()
    page_kicker = "Search results" if authenticated_principal else "Sample homes"
    page_heading = "Matching homes" if authenticated_principal else "Sample homes"
    page_subtitle = (
        "Opening the homes for this search."
        if authenticated_principal
        else "Preview example homes before signing in. Sign in to open your latest real shortlist."
    )
    nav_links = (
        [
            {"label": "Search", "href": "/app/search"},
            {"label": "History", "href": "/app/agents#search-history"},
            {"label": "Account", "href": "/app/account"},
        ]
        if authenticated_principal
        else [
            {"label": "Sign in", "href": "/sign-in"},
            {"label": "Sample shortlist", "href": "/app/example/shortlist"},
            {"label": "Start search", "href": "/sign-in?return_to=%2Fapp%2Fsearch"},
        ]
    )
    meta_description = (
        "A fast PropertyQuarry results view that loads matching homes for a completed search without waiting for the full workbench."
        if authenticated_principal
        else "A safe PropertyQuarry sample shortlist showing how example homes open before you sign in."
    )
    return _render_public_template(
        request,
        "app/property_ranked_run_fast.html",
        **_public_context(
            request=request,
            current_nav="product",
            page_title="PropertyQuarry Matching Homes",
            principal_id=authenticated_principal,
            status=_anonymous_onboarding_status(),
            access_identity=access_identity,
            extra={
                "run_id": normalized_run_id,
                "run_status_url": status_url,
                "initial_run_payload": initial_run_payload,
                "full_shortlist_href": full_href,
                "search_href": "/app/search" if authenticated_principal else "/sign-in?return_to=%2Fapp%2Fsearch",
                "history_href": "/app/agents#search-history" if authenticated_principal else "/app/example/shortlist",
                "account_href": "/app/account" if authenticated_principal else "/sign-in",
                "page_kicker": page_kicker,
                "page_heading": page_heading,
                "page_subtitle": page_subtitle,
                "nav_links": nav_links,
                "account_nav": _account_nav_context(request=request, context=context),
                "auth_handoff_url": "/app/api/property/landing-handoff" if not authenticated_principal else "",
                "meta_description": meta_description,
                "canonical_path": f"/app/shortlist/run/{encoded_run_id}",
                "robots_directive": "noindex, nofollow",
            },
        ),
    )


@router.get("/app/{section}", response_class=HTMLResponse)
def app_shell(
    section: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
    run_id: str = Query(default=""),
    candidate: str = Query(default=""),
    agent_id: str = Query(default=""),
    load_agent: str = Query(default=""),
    run_agent: str = Query(default=""),
    packet_missing: str = Query(default=""),
    missing_candidate_ref: str = Query(default=""),
    stale_run: str = Query(default=""),
    missing_run_id: str = Query(default=""),
    full: str = Query(default=""),
) -> HTMLResponse:
    brand = request_brand(request)
    property_brand = brand["key"] == "propertyquarry"
    release_probe_read_only = (
        str(context.auth_source or "").strip() == "propertyquarry_release_probe"
    )
    nav_groups = app_nav_groups_for_brand(brand["key"])
    allowed = {item["href"].rstrip("/").rsplit("/", 1)[-1] for group in nav_groups for item in group["items"]}
    if property_brand:
        allowed.add("research")
    legacy_redirects = {
        "briefing": "/app/queue",
        "inbox": "/app/queue",
        "follow-ups": "/app/commitments",
        "memory": "/app/people",
        "contacts": "/app/evidence",
        "activity": "/admin/office",
        "channels": "/app/settings",
        "automation": "/app/settings",
        "automations": "/app/settings",
    }
    property_legacy_redirects = {
        "today": "/app/properties",
        "queue": "/app/shortlist",
        "commitments": "/app/account",
        "people": "/app/account",
        "evidence": "/app/account",
        "activity": "/app/account",
        "channel-loop": "/app/account",
        "automation": "/app/agents",
        "automations": "/app/agents",
        "channels": _property_billing_fallback_href(),
        "profile": "/app/account",
        "settings": "/app/account",
        "usage": "/app/settings/usage",
        "support": "/app/settings/support",
        "trust": "/app/settings/trust",
        "google": "/app/settings/google",
        "access": "/app/settings/access",
        "invitations": "/app/settings/invitations",
        "outcomes": "/app/settings/outcomes",
        "plan": "/app/settings/plan",
    }
    allowed.update(legacy_redirects)
    allowed.update({"today", "queue", "commitments", "people", "evidence", "activity", "channel-loop"})
    if property_brand:
        allowed.update({"properties", "search", "shortlist", "agents", "alerts", "billing", "account"})
        legacy_redirects.update(property_legacy_redirects)
        allowed.update(property_legacy_redirects)
    else:
        allowed.update(
            {
                "today",
                "queue",
                "commitments",
                "people",
                "evidence",
                "properties",
                "settings",
                "search",
                "channel-loop",
            }
        )
    if section not in allowed:
        raise HTTPException(status_code=404, detail="app_section_not_found")
    if section in legacy_redirects:
        target = legacy_redirects[section]
        query = str(request.url.query or "").strip()
        if query:
            target = f"{target}?{query}"
        return RedirectResponse(target, status_code=307)
    resolved_section = section
    current_nav = section
    property_surface_aliases = {
        "properties": "properties",
        "search": "search",
        "shortlist": "shortlist",
        "research": "research",
        "agents": "agents",
        "alerts": "alerts",
        "account": "account",
        "billing": "billing",
    }
    status = container.onboarding.status(principal_id=context.principal_id)
    if property_brand and resolved_section == "billing" and release_probe_read_only:
        return _render_property_billing_unavailable_page(
            request,
            context=context,
            handoff={
                "status": "release_probe_read_only",
                "error": "release_probe_read_only",
            },
        )
    normalized_run_id = str(run_id or "").strip()
    requested_agent_id = str(load_agent or run_agent or agent_id or "").strip()
    if property_brand and resolved_section in {"properties", "shortlist", "research"} and normalized_run_id:
        product = build_product_service(container)
        route_run_lightweight = resolved_section in {"shortlist", "research"}
        try:
            route_run = dict(
                product.get_property_search_run_status(
                    principal_id=context.principal_id,
                    run_id=normalized_run_id,
                    lightweight=route_run_lightweight,
                    account_email=context.access_email,
                )
                or {}
            )
        except TypeError:
            try:
                route_run = dict(
                    product.get_property_search_run_status(
                        principal_id=context.principal_id,
                        run_id=normalized_run_id,
                        lightweight=route_run_lightweight,
                    )
                    or {}
                )
            except TypeError:
                route_run = dict(
                    product.get_property_search_run_status(
                        principal_id=context.principal_id,
                        run_id=normalized_run_id,
                    )
                    or {}
                )
        if not route_run:
            if resolved_section == "properties":
                target_query = str(request.url.query or "").strip()
                target = "/app/search"
                if target_query:
                    target = f"{target}?{target_query}"
                return RedirectResponse(target, status_code=307)
            if str(context.auth_source or "").strip() == "workspace_access_session":
                route_run = {}
            else:
                query_pairs = [
                    (key, value)
                    for key, value in urllib.parse.parse_qsl(str(request.url.query or ""), keep_blank_values=True)
                    if key not in {"run_id", "stale_run", "missing_run_id"}
                ]
                query_pairs.insert(0, ("stale_run", "1"))
                query_pairs.insert(1, ("missing_run_id", normalized_run_id))
                target_query = urllib.parse.urlencode(query_pairs)
                target = request.url.path
                if target_query:
                    target = f"{target}?{target_query}"
                return RedirectResponse(target, status_code=303)
        explicit_full_view = str(full or "").strip().lower() in {"1", "true", "yes"}
        if resolved_section in {"properties", "shortlist"}:
            route_run_status = str(route_run.get("status") or "").strip().lower()
            route_run_summary = dict(route_run.get("summary") or {}) if isinstance(route_run.get("summary"), dict) else {}
            route_ranked_candidates = list(route_run_summary.get("ranked_candidates") or route_run.get("ranked_candidates") or [])
            route_sources = list(route_run_summary.get("sources") or route_run.get("sources") or [])
            if (
                resolved_section == "properties"
                and not explicit_full_view
                and route_run_status in {"in_progress", "running", "processing", "scanning", "starting"}
                and not route_ranked_candidates
                and not route_sources
                and not route_run_summary.get("reviewed_listing_total")
            ):
                target_query = str(request.url.query or "").strip()
                target = "/app/search"
                if target_query:
                    target = f"{target}?{target_query}"
                return RedirectResponse(target, status_code=307)
            replacement_run_id = str(route_run_summary.get("repair_replacement_run_id") or "").strip()
            if route_run_status == "failed" and replacement_run_id and replacement_run_id != normalized_run_id:
                query_pairs = [
                    (key, value)
                    for key, value in urllib.parse.parse_qsl(str(request.url.query or ""), keep_blank_values=True)
                    if key != "run_id"
                ]
                query_pairs.insert(0, ("run_id", replacement_run_id))
                target_query = urllib.parse.urlencode(query_pairs)
                return RedirectResponse(f"{request.url.path}?{target_query}", status_code=303)
    if resolved_section == "channel-loop":
        workspace = dict(status.get("workspace") or {})
        product = build_product_service(container)
        pack = product.channel_loop_pack(
            principal_id=context.principal_id,
            operator_id=str(context.operator_id or "").strip(),
        )
        product.record_surface_event(
            principal_id=context.principal_id,
            event_type="channel_loop_opened",
            surface="channel_loop",
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
        stats = [
            {"label": "Memo items", "value": str(int(dict(pack.get("stats") or {}).get("memo_items") or 0))},
            {"label": "Pending drafts", "value": str(int(dict(pack.get("stats") or {}).get("pending_drafts") or 0))},
            {"label": "Commitments", "value": str(int(dict(pack.get("stats") or {}).get("open_commitments") or 0))},
            {"label": "Handoffs", "value": str(int(dict(pack.get("stats") or {}).get("open_handoffs") or 0))},
            {"label": "Decisions", "value": str(int(dict(pack.get("stats") or {}).get("open_decisions") or 0))},
        ]
        return _render_public_template(
            request,
            "console_shell.html",
            **_console_shell_context(
                request=request,
                page_title=f"{request_brand(request)['name']} Inline Loop",
                current_nav="today",
                context=context,
                console_title=str(pack.get("headline") or "Inline loop"),
                console_summary=str(pack.get("summary") or "Clear the compact office loop."),
                nav_groups=nav_groups,
                workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
                cards=[
                    {
                        "eyebrow": "Inline loop",
                        "title": str(pack.get("headline") or "Inline loop"),
                        "body": str(pack.get("summary") or "Clear the compact office loop."),
                        "items": list(pack.get("items") or []),
                    },
                    *[
                        {
                            "eyebrow": "Channel digest",
                            "title": str(digest.get("headline") or "Channel digest"),
                            "body": " ".join(
                                part
                                for part in (
                                    str(digest.get("summary") or "").strip(),
                                    str(digest.get("preview_text") or "").strip(),
                                )
                                if part
                            ),
                            "items": list(digest.get("items") or []),
                        }
                        for digest in list(pack.get("digests") or [])
                    ],
                ],
                stats=stats,
            ),
        )
    property_sections = {"properties", "search", "shortlist", "research", "agents", "alerts", "account", "billing"} if property_brand else set()
    core_sections = {"today", "queue", "commitments", "people", "evidence", "activity"}
    if not property_brand:
        core_sections.add("settings")
    if resolved_section in core_sections:
        product = build_product_service(container)
        surface_event = {
            "today": "memo_opened",
            "queue": "queue_opened",
            "commitments": "commitment_ledger_opened",
            "people": "people_graph_opened",
            "evidence": "evidence_opened",
            "activity": "operator_queue_opened",
            "settings": "rules_opened",
        }.get(resolved_section)
        if surface_event:
            product.record_surface_event(
                principal_id=context.principal_id,
                event_type=surface_event,
                surface=resolved_section,
                actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
            )
        diagnostics = product.workspace_diagnostics(principal_id=context.principal_id)
        outcomes = product.workspace_outcomes(principal_id=context.principal_id) if resolved_section == "settings" else None
        payload = _workspace_section_payload(
            resolved_section,
            product.workspace_snapshot(
                principal_id=context.principal_id,
                operator_id=str(context.operator_id or "").strip(),
            ),
            diagnostics,
            outcomes,
            operator_id=str(context.operator_id or "").strip(),
            brand_key=request_brand(request)["key"],
        )
    else:
        property_payload_section = property_surface_aliases.get(resolved_section, resolved_section) if property_brand else resolved_section
        product = build_product_service(container)
        if (
            property_brand
            and resolved_section == "properties"
            and not normalized_run_id
        ):
            has_live_property_run = False
            try:
                active_candidate = product.find_active_property_search_run(
                    principal_id=context.principal_id,
                    limit=8,
                    account_email=context.access_email,
                )
            except TypeError:
                active_candidate = product.find_active_property_search_run(principal_id=context.principal_id, limit=8)
            except Exception:
                active_candidate = {}
            if isinstance(active_candidate, dict) and str(active_candidate.get("run_id") or "").strip():
                has_live_property_run = True
            if not has_live_property_run:
                try:
                    recent_candidates = product.list_property_search_runs(
                        principal_id=context.principal_id,
                        limit=24,
                        hydrate=False,
                        account_email=context.access_email,
                    )
                except TypeError:
                    try:
                        recent_candidates = product.list_property_search_runs(
                            principal_id=context.principal_id,
                            limit=24,
                            hydrate=False,
                        )
                    except TypeError:
                        recent_candidates = product.list_property_search_runs(principal_id=context.principal_id, limit=24)
                except Exception:
                    recent_candidates = []
                terminal_statuses = {"processed", "completed", "completed_partial", "failed", "noop", "cancelled", "not started"}
                for row in list(recent_candidates or []):
                    if not isinstance(row, dict) or not str(row.get("run_id") or "").strip():
                        continue
                    row_summary = dict(row.get("summary") or {}) if isinstance(row.get("summary"), dict) else {}
                    row_status = str(row.get("status") or row_summary.get("status") or "").strip().lower()
                    if row_status and row_status not in terminal_statuses:
                        has_live_property_run = True
                        break
            if not has_live_property_run:
                query = str(request.url.query or "").strip()
                target = "/app/search"
                if query:
                    target = f"{target}?{query}"
                return RedirectResponse(target, status_code=307)
        property_context = (
            _property_console_context(
                container=container,
                principal_id=context.principal_id,
                access_email=context.access_email,
                status=status,
                run_id=run_id,
                selected_candidate_ref=candidate,
                selected_agent_id=requested_agent_id,
                surface_mode=resolved_section,
                force_recent_runs=str(full or "").strip().lower() in {"1", "true", "yes"},
                defer_run_hydration=(
                    release_probe_read_only
                    or (
                        not normalized_run_id
                        and resolved_section in {"properties", "agents", "alerts", "account", "billing", "settings"}
                        and str(full or "").strip().lower() not in {"1", "true", "yes"}
                    )
                ),
            )
            if resolved_section in property_sections or resolved_section == "properties"
            else None
        )
        if property_context is not None and property_brand:
            property_context["surface_mode"] = current_nav
            property_context["requested_run_id"] = normalized_run_id
            if str(stale_run or "").strip().lower() in {"1", "true", "yes"}:
                recovered_run_id = str(missing_run_id or "").strip()
                route_action_labels = {
                    "search": "Open search",
                    "shortlist": "Open shortlist",
                    "research": "Open research",
                    "properties": "Open results",
                }
                property_context["route_recovery"] = {
                    "title": "Opened the current workspace",
                    "detail": "That saved link was stale, so PropertyQuarry opened the current search workspace.",
                    "run_id": recovered_run_id,
                    "action_href": str(request.url.path or "").strip() or "/app/search",
                    "action_label": route_action_labels.get(current_nav, "Stay here"),
                    "tone": "warn",
                }
            if (
                not release_probe_read_only
                and str(packet_missing or "").strip().lower() in {"1", "true", "yes"}
            ):
                missing_ref = str(missing_candidate_ref or "").strip()
                recovery_query = {"packet_missing": "1"}
                if normalized_run_id:
                    recovery_query["run_id"] = normalized_run_id
                if missing_ref:
                    recovery_query["missing_candidate_ref"] = missing_ref
                recovery_href = f"/app/shortlist?{urllib.parse.urlencode(recovery_query)}#results-list"
                repair_task_ref = _property_queue_missing_research_packet_repair(
                    container=container,
                    principal_id=context.principal_id,
                    run_id=normalized_run_id,
                    candidate_ref=missing_ref,
                    recovery_url=recovery_href,
                )
                property_context["packet_recovery"] = {
                    "title": "Property page is being rebuilt",
                    "detail": (
                        "This property page is queued for refresh. The shortlist stays available while "
                        "PropertyQuarry rebuilds the page."
                        if repair_task_ref
                        else "That property page is not available right now. The shortlist stays available while the page is refreshed."
                    ),
                    "candidate_ref": missing_ref,
                    "run_id": normalized_run_id,
                    "queue_item_ref": repair_task_ref,
                    "action_href": recovery_href,
                    "action_label": "Stay on shortlist",
                    "tone": "warn",
                }
            resolved_property_section = PropertySurfaceScope.for_section(resolved_section).section
            if resolved_property_section == "shortlist" or (
                resolved_property_section == "properties" and bool(normalized_run_id)
            ):
                context_run = dict((property_context or {}).get("run") or {}) if isinstance(property_context, dict) else {}
                context_run_has_results = _property_run_payload_has_shortlist_results(context_run)
                def _load_saved_shortlist_candidates() -> list[dict[str, object]]:
                    try:
                        return [
                            _propertyquarry_normalize_public_tour_candidate(candidate)
                            for candidate in product.list_property_saved_shortlist_candidates(
                                principal_id=context.principal_id,
                                status=status,
                            )
                        ]
                    except Exception:
                        return []

                if resolved_property_section == "shortlist" and context_run_has_results:
                    property_context["saved_shortlist_candidates"] = []
                elif resolved_property_section == "shortlist":
                    property_context["saved_shortlist_candidates"] = list(
                        _property_first_paint_value(_load_saved_shortlist_candidates, [])
                    )
                else:
                    property_context["saved_shortlist_candidates"] = [
                        _propertyquarry_normalize_public_tour_candidate(candidate)
                        for candidate in _load_saved_shortlist_candidates()
                    ]
            if PropertySurfaceScope.for_section(resolved_section).wants_credit_digest:
                fleet_digest = product.cached_fleet_digest_payload(
                    principal_id=context.principal_id,
                    operator_key=str(context.operator_id or "").strip(),
                )
                digests = [dict(fleet_digest)] if fleet_digest else []
                property_context["channel_digests"] = digests
                property_context["fleet_digest"] = dict(fleet_digest or {})
                billing_truth = dict(property_context.get("billing_truth") or {})
                if billing_truth:
                    billing_truth["fleet_digest"] = dict(property_context.get("fleet_digest") or {})
                    property_context["billing_truth"] = billing_truth
        if (
            not release_probe_read_only
            and (resolved_section in property_sections or resolved_section == "properties")
            and current_nav != "search"
        ):
            with contextlib.suppress(Exception):
                product.record_surface_event(
                    principal_id=context.principal_id,
                    event_type=f"{current_nav}_opened",
                    surface=current_nav,
                    actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
                )
        if property_brand and resolved_section in property_sections:
            if property_context is not None and property_payload_section in {"properties", "shortlist"}:
                property_context["selected_candidate_ref"] = _property_resolve_scoped_curated_candidate_ref(
                    requested_candidate_ref=candidate,
                    property_context=property_context,
                )
            payload = _property_workspace_payload(
                property_payload_section,
                status=status,
                property_state=property_context or {},
            )
            if current_nav == "search":
                payload["title"] = "Search"
                payload["summary"] = "Build the brief, run the sweep, review the matching homes, and open the property that deserves a decision."
            elif current_nav == "account":
                payload["title"] = "Account"
                payload["summary"] = "Notifications, plan, access, and data."
            elif current_nav == "billing":
                payload["title"] = "Billing"
                payload["summary"] = "Current plan and billing."
        else:
            payload = _app_section_payload(
                resolved_section,
                status,
                live_feed=_app_live_feed(container, principal_id=context.principal_id),
                property_context=property_context,
            )
    workspace = dict(status.get("workspace") or {})
    if property_brand and resolved_section in property_sections:
        billing_handoff = dict((property_context or {}).get("billing_handoff") or {}) if property_context else {}
        billing_open_href = _property_billing_usable_open_href(billing_handoff)
        if current_nav == "billing":
            if billing_open_href:
                return RedirectResponse(billing_open_href, status_code=303)
            return RedirectResponse(_property_billing_fallback_href(), status_code=303)
        property_template = "app/property_decision_workbench.html"
        return _render_public_template(
            request,
            property_template,
            **{
                **_console_shell_context(
                    request=request,
                    page_title=f"{request_brand(request)['name']} {payload['title']}",
                    current_nav=current_nav,
                    context=context,
                    console_title=str(payload["title"]),
                    console_summary=str(payload["summary"]),
                    nav_groups=nav_groups,
                    workspace_label=str(request_brand(request)["name"] or "PropertyQuarry"),
                    cards=list(payload.get("cards") or []),
                    stats=list(payload["stats"]),
                    console_form=dict(payload.get("console_form") or {}),
                ),
                **payload,
                "search_health": dict((property_context or {}).get("search_health") or {}),
                "facebook_sign_in_enabled": _facebook_sign_in_enabled(),
                "id_austria_sign_in_enabled": _id_austria_sign_in_enabled_for_request(request),
            },
        )
    return _render_public_template(
        request,
        "console_shell.html",
        **_console_shell_context(
            request=request,
            page_title=f"{request_brand(request)['name']} {payload['title']}",
            current_nav=current_nav,
            context=context,
            console_title=str(payload["title"]),
            console_summary=str(payload["summary"]),
            nav_groups=nav_groups,
            workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
            cards=list(payload["cards"]),
            stats=list(payload["stats"]),
            console_form=dict(payload.get("console_form") or {}),
        ),
    )


@router.get("/admin", response_class=HTMLResponse)
def admin_root() -> RedirectResponse:
    return RedirectResponse("/admin/policies", status_code=307)


@router.get("/admin/{section}", response_class=HTMLResponse)
def admin_shell(
    section: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
    _: None = Depends(require_operator_context),
) -> HTMLResponse:
    allowed = {row["key"] for group in ADMIN_NAV_GROUPS for row in group["items"]}
    if section not in allowed:
        raise HTTPException(status_code=404, detail="admin_section_not_found")
    operator_id = str(context.operator_id or "").strip()
    if not operator_id and context.auth_source == "loopback_no_auth":
        operator_id = _default_operator_id_for_browser(container, principal_id=context.principal_id)
    payload = _build_admin_section_payload(
        section,
        container=container,
        principal_id=context.principal_id,
        operator_id=operator_id,
    )
    return _render_public_template(
        request,
        "console_shell.html",
        **_console_shell_context(
            request=request,
            page_title=f"{request_brand(request)['name']} Admin {payload['title']}",
            current_nav=section,
            context=context,
            console_title=str(payload["title"]),
            console_summary=str(payload["summary"]),
            nav_groups=ADMIN_NAV_GROUPS,
            workspace_label="Operator Center",
            cards=list(payload["cards"]),
            stats=list(payload["stats"]),
        ),
    )


@router.get("/setup")
def legacy_setup_redirect() -> RedirectResponse:
    return RedirectResponse("/register", status_code=307)


@router.get("/demo/brief")
def legacy_brief_redirect() -> RedirectResponse:
    return RedirectResponse("/app/queue", status_code=307)


@router.get("/channels/google")
def legacy_google_channel_redirect() -> RedirectResponse:
    return RedirectResponse("/integrations/google", status_code=307)


@router.get("/channels/telegram")
def legacy_telegram_channel_redirect() -> RedirectResponse:
    return RedirectResponse("/integrations/telegram", status_code=307)


@router.get("/channels/whatsapp")
def legacy_whatsapp_channel_redirect() -> RedirectResponse:
    return RedirectResponse("/integrations/whatsapp", status_code=307)


@router.get("/app/commitments/candidates/{candidate_id}", response_class=HTMLResponse)
def commitment_candidate_review(
    candidate_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    brand = request_brand(request)
    nav_groups = app_nav_groups_for_brand(brand["key"])
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    candidate = product.get_commitment_candidate(principal_id=context.principal_id, candidate_id=candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="commitment_candidate_not_found")
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="commitment_candidate_opened",
        surface=f"candidate:{candidate_id}",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    return _render_public_template(
        request,
        "app/commitment_candidate_review.html",
        **{
            **_console_shell_context(
                request=request,
                page_title=f"{brand['name']} Review {candidate.title}",
                current_nav="queue",
                context=context,
                console_title="Review extracted commitment",
                console_summary="Edit the wording, due date, or ownership before this enters the commitment ledger.",
                nav_groups=nav_groups,
                workspace_label=str(workspace.get("name") or brand["workspace_label"]),
                cards=[],
                stats=[
                    {"label": "Confidence", "value": f"{int(candidate.confidence * 100)}%"},
                    {"label": "Counterparty", "value": candidate.counterparty or "None"},
                    {"label": "Suggested due", "value": candidate.suggested_due_at[:10] if candidate.suggested_due_at else "Open"},
                    {"label": "Status", "value": candidate.status.title()},
                ],
            ),
            "candidate": candidate,
        },
    )
