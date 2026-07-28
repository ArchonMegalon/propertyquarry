#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ea"))

from app.api.app import create_app
from app.api.routes.landing import prewarm_property_search_shell_cache, prewarm_property_search_surface_cache
from app.product.service import ProductService, _now_iso


DEFAULT_ROUTE_BUDGET_MS = {
    "/sign-in": 1200,
    "/app/search": 1200,
    "/app/agents": 1200,
    "/app/properties": 1200,
    "/app/shortlist": 1200,
    "/app/alerts": 1200,
    "/app/account": 1200,
    "/app/billing": 1200,
    "/app/settings/google": 1200,
    "/app/settings/access": 1200,
    "/app/settings/usage": 1200,
    "/app/settings/support": 1200,
    "/app/settings/trust": 1200,
    "/app/settings/invitations": 1200,
}
DEFAULT_SEARCH_COMPRESSED_MAX_BYTES = 240_000
PERFORMANCE_SMOKE_LOCATION_QUERY = "1020 Vienna"
PERFORMANCE_SMOKE_MIN_AREA_M2 = 60
PERFORMANCE_SMOKE_MAX_PRICE_EUR = 1600

FORBIDDEN_CUSTOMER_NOISE = (
    "billing truth",
    "plan and limits",
    "refresh delivery",
    "repair status checked",
    "what still worked",
    "main blocker",
    "best next move",
    "search posture",
    "account posture",
    "latest run posture",
    "saved posture",
    "billing posture",
    "plan and billing posture",
    "energy posture",
    "running-cost posture",
    "authority posture",
    "governed review",
    "workspace diagnostics bundle",
    "open bundle",
    "support posture",
    "runtime posture",
    "provider posture",
    "channel receipt",
    "install receipt",
    "support bundle",
    "export bundle",
    "outcome posture",
    "follow-up artifacts",
    "proof of value",
    "operator center",
)

FORBIDDEN_VISIBLE_INTERNAL_COPY = (
    "current best so far",
    "decision support",
    "dossier",
    "evidence",
    "magic fit",
    "magicfit",
    "no source completed",
    "proof",
    "provider webpage",
    "run ranking",
    "source completed",
    "suppressed_generic_listing_page",
    "verified",
)

SHARED_TOP_NAV_LABELS = (
    "Search",
    "Shortlist",
    "Research",
    "Account",
)

BILLING_FAIL_CLOSED_STATE_MARKERS = (
    "billing portal is still being connected",
    "still opens another sign-in",
    "billing account host is not ready yet",
)

ALLOWED_RYBBIT_APP_EVENTS = {
    "pq.search.started",
    "pq.search.results_viewed",
    "pq.search.agent_created",
    "pq.search.agent_updated",
    "pq.search.agent_notification_sent",
    "pq.search.suppressed_viewed",
    "pq.property.opened",
    "pq.property.map_opened",
    "pq.dossier.opened",
    "pq.tour.opened",
    "pq.flythrough.opened",
    "pq.decision.saved",
    "pq.reason.selected",
    "pq.agent_question.created",
    "pq.document.requested",
    "pq.packet.shared",
    "pq.email.clicked",
}

ALLOWED_RYBBIT_ATTRIBUTE_NAMES = {
    "data-rybbit-event",
    "data-rybbit-prop-cta-key",
    "data-rybbit-prop-surface",
}

FORBIDDEN_RYBBIT_PAYLOAD_TOKENS = (
    "candidate_ref",
    "data-rybbit-prop-candidate",
    "email",
    "exact_address",
    "listing_id",
    "listing_url",
    "phone",
    "principal",
    "property_url",
    "run_id",
    "saved_search_id",
    "selected_platform_count",
    "signed",
    "telegram",
)
PROVIDER_FREE_ENV_PREFIXES = (
    "BROWSERACT_",
    "EA_ENV_TEABLE_",
    "EA_GEMINI_VORTEX_SLOT_",
    "GOOGLE_API_KEY_FALLBACK_",
    "ONEMIN_AI_API_KEY",
    "PROPERTYQUARRY_TEABLE_",
    "TEABLE_",
)
PROVIDER_FREE_ENV_NAMES = {
    "AI_MAGICX_API_KEY",
    "DATABASE_URL",
    "EA_GEMINI_VORTEX_COMMAND",
    "EA_RESPONSES_MAGICX_API_KEY",
    "ONEMIN_DIRECT_API_KEYS_JSON",
    "ONEMIN_DIRECT_API_KEYS_JSON_FILE",
}

FORBIDDEN_BILLING_SURFACE_TOKENS = (
    "accounting lane",
    "billing truth",
    "billing history",
    "brilliant directories",
    "brilliantdirectories",
    "commercial truth",
    "compare plans",
    "invoice handoff",
    "invoices",
    "open pricing",
    "view plans",
    "payfunnels",
    "payfunnels/order",
    "plan and limits",
    "plan and payments",
    "plan unit",
    "your plan",
)
FORBIDDEN_COMPARE_CARD_TOKENS = (
    "compare cards",
    "prd-compare",
    "Decision support",
    "The next-best properties from this run",
    "Other ranked homes from this run",
)

CONTENT_FIRST_MOBILE_PATHS = {
    "/app/agents",
    "/app/alerts",
    "/app/account",
    "/app/billing",
}

SETTINGS_MOBILE_PATHS = {
    "/app/settings/google",
    "/app/settings/access",
    "/app/settings/usage",
    "/app/settings/support",
    "/app/settings/trust",
    "/app/settings/invitations",
}


def _asset_text(client: TestClient, path: str) -> str:
    try:
        response = client.get(path, headers={"host": "propertyquarry.com", "accept-encoding": "identity"})
    except Exception:
        return ""
    if response.status_code != 200:
        return ""
    return response.text or ""


def _visible_text(body: str) -> str:
    without_hidden = re.sub(
        r"<script.*?</script>|<style.*?</style>|<template.*?</template>",
        " ",
        str(body or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    without_tags = re.sub(r"<[^>]+>", " ", without_hidden)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _workbench_css_path_for_route(path: str, body: str) -> str:
    match = re.search(r'href="(?P<href>/app/assets/property-workbench\.css[^"]*)"', body)
    if match:
        return str(match.group("href") or "").strip()
    normalized_path = str(path or "").split("?", 1)[0]
    if normalized_path in CONTENT_FIRST_MOBILE_PATHS:
        return "/app/assets/property-workbench.css?surface=static"
    return "/app/assets/property-workbench.css"


def _has_css_min_height_at_least(css_body: str, minimum_px: int = 44) -> bool:
    for value in re.findall(r"min-height\s*:\s*(\d+)px", css_body, flags=re.IGNORECASE):
        try:
            if int(value) >= minimum_px:
                return True
        except ValueError:
            continue
    return False


def _mobile_surface_contract_checks(path: str, body: str, *, css_body: str = "") -> list[dict[str, object]]:
    normalized_path = str(path or "").split("?", 1)[0]
    surface_markup = f'data-pqx-surface="{normalized_path.rsplit("/", 1)[-1]}"'
    if normalized_path == "/sign-in":
        return [
            {
                "name": "mobile_viewport_meta",
                "ok": 'name="viewport"' in body and "width=device-width" in body,
            },
            {
                "name": "public_auth_surface",
                "ok": "data-property-public-page" in body or "PropertyQuarry" in body,
            },
        ]
    if not normalized_path.startswith("/app/"):
        return []
    nav_missing = [label for label in SHARED_TOP_NAV_LABELS if label not in body]
    checks = [
        {
            "name": "mobile_viewport_meta",
            "ok": 'name="viewport"' in body and "width=device-width" in body,
        },
        {
            "name": "shared_top_navigation",
            "ok": "data-property-research-topnav" in body and not nav_missing,
            "detail": ", ".join(nav_missing[:5]),
        },
        {
            "name": "property_app_shell",
            "ok": "data-property-app-shell" in body and "data-pq-greenfield-shell" in body,
        },
    ]
    if normalized_path in CONTENT_FIRST_MOBILE_PATHS:
        checks.extend(
            (
                {
                    "name": "mobile_content_first_surface",
                    "ok": 'data-pqx-mobile-panel="brief"' in body and "pqx-brief-drawer-panel" in body,
                },
                {
                    "name": "mobile_static_switch_suppressed",
                    "ok": ".pqx-shell[data-pqx-surface=\"account\"] .pqx-mobile-switch" in css_body
                    and ".pqx-shell[data-pqx-surface=\"billing\"] .pqx-mobile-switch" in css_body
                    and ".pqx-shell[data-pqx-surface=\"alerts\"] .pqx-mobile-switch" in css_body,
                },
            )
        )
    elif normalized_path in SETTINGS_MOBILE_PATHS:
        checks.append(
            {
                "name": "mobile_settings_surface",
                "ok": (
                    "data-property-research-topnav" in body
                    and (
                        "/app/settings/" in body
                        or "/app/account?settings_view=" in body
                        or surface_markup in body
                    )
                ),
            }
        )
    else:
        checks.extend(
            (
                {
                    "name": "mobile_top_navigation_only",
                    "ok": "data-property-mobile-dock" not in body and "class=\"pq-mobile-nav\"" not in body,
                },
                {
                    "name": "mobile_top_navigation_touch_targets",
                    "ok": (
                        "data-property-research-topnav" in body
                        and _has_css_min_height_at_least(css_body, 44)
                    ),
                },
            )
        )
    return checks


def _allowed_billing_handoff_hosts() -> set[str]:
    urls = [
        "https://billing.propertyquarry.test/",
        "https://billing.propertyquarry.com/",
        str(os.environ.get("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BILLING_URL") or ""),
        str(os.environ.get("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BASE_URL") or ""),
    ]
    urls.extend(
        part.strip()
        for part in str(os.environ.get("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BILLING_FALLBACK_URLS") or "").split(",")
        if part.strip()
    )
    hosts: set[str] = set()
    for raw_url in urls:
        parsed = urlparse(str(raw_url or "").strip())
        if parsed.scheme == "https" and parsed.netloc:
            hosts.add(parsed.netloc.lower())
    return hosts


def _billing_handoff_redirect_ok(*, path: str, status_code: int, location: str) -> tuple[bool, str]:
    if path != "/app/billing" or status_code not in {303, 307}:
        return False, ""
    parsed = urlparse(str(location or "").strip())
    host = parsed.netloc.lower()
    if parsed.scheme != "https" or not host:
        return False, host
    return host in _allowed_billing_handoff_hosts(), host


def _billing_internal_account_fallback_ok(*, path: str, status_code: int, location: str) -> bool:
    if path != "/app/billing" or status_code not in {303, 307}:
        return False
    parsed = urlparse(str(location or "").strip())
    return not parsed.scheme and (parsed.path or "").startswith("/app/account")


def _rybbit_surface_contract_checks(path: str, body: str) -> list[dict[str, object]]:
    normalized_path = str(path or "").split("?", 1)[0]
    if not normalized_path.startswith("/app/"):
        return []
    attr_matches = re.findall(r"(data-rybbit-[^=\s>]+)=([\"'])(.*?)\2", body, flags=re.IGNORECASE | re.DOTALL)
    attrs = [
        (str(name or "").strip().lower(), " ".join(str(value or "").split()).strip())
        for name, _quote, value in attr_matches
    ]
    attr_names = {name for name, _value in attrs}
    event_values = [value for name, value in attrs if name == "data-rybbit-event"]
    serialized_attrs = " ".join(f"{name}={value}" for name, value in attrs).lower()
    forbidden_hits = [token for token in FORBIDDEN_RYBBIT_PAYLOAD_TOKENS if token in serialized_attrs]
    unknown_events = [value for value in event_values if value not in ALLOWED_RYBBIT_APP_EVENTS]
    unknown_attrs = [name for name in sorted(attr_names) if name not in ALLOWED_RYBBIT_ATTRIBUTE_NAMES]
    return [
        {
            "name": "rybbit_no_identify",
            "ok": "rybbit.identify" not in body and "analytics_principal_id" not in body,
        },
        {
            "name": "rybbit_taxonomy_events_only",
            "ok": not unknown_events and 'data-rybbit-event="property_' not in body,
            "detail": ", ".join(unknown_events[:5]),
        },
        {
            "name": "rybbit_allowed_attributes_only",
            "ok": not unknown_attrs,
            "detail": ", ".join(unknown_attrs[:5]),
        },
        {
            "name": "rybbit_no_private_payload",
            "ok": not forbidden_hits,
            "detail": ", ".join(forbidden_hits[:5]),
        },
    ]


def _route_budget_for(path: str, *, route_budget_ms: int) -> int:
    normalized_path = str(path or "").split("?", 1)[0]
    default_budget = int(DEFAULT_ROUTE_BUDGET_MS.get(normalized_path, route_budget_ms))
    return min(default_budget, int(route_budget_ms))


def _search_compressed_max_bytes() -> int:
    raw_value = str(os.environ.get("PROPERTYQUARRY_SEARCH_COMPRESSED_MAX_BYTES") or "").strip()
    if not raw_value:
        return DEFAULT_SEARCH_COMPRESSED_MAX_BYTES
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_SEARCH_COMPRESSED_MAX_BYTES
    return value if value > 0 else DEFAULT_SEARCH_COMPRESSED_MAX_BYTES


def _response_content_length(response: object) -> int:
    headers = getattr(response, "headers", {})
    raw_value = str(headers.get("content-length") or "").strip()
    if raw_value:
        try:
            return int(raw_value)
        except ValueError:
            pass
    return len(getattr(response, "content", b"") or b"")


def _reset_authenticated_performance_smoke_env() -> None:
    for key in list(os.environ.keys()):
        if key in PROVIDER_FREE_ENV_NAMES or key.startswith(PROVIDER_FREE_ENV_PREFIXES):
            os.environ.pop(key, None)


def _seed_workspace(client: TestClient) -> None:
    response = client.post(
        "/v1/onboarding/start",
        json={
            "workspace_name": "PropertyQuarry Performance Smoke",
            "mode": "personal",
            "workspace_mode": "personal",
            "timezone": "Europe/Vienna",
            "region": "AT",
            "language": "en",
            "selected_channels": ["google"],
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"workspace_seed_failed:{response.status_code}:{response.text[:280]}")


def _property_preferences_payload(*, saved_candidates: list[dict[str, object]] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "country_code": "AT",
        "region_code": "vienna",
        "language_code": "de",
        "listing_mode": "rent",
        "property_type": "apartment",
        "location_query": PERFORMANCE_SMOKE_LOCATION_QUERY,
        "min_area_m2": PERFORMANCE_SMOKE_MIN_AREA_M2,
        "max_price_eur": PERFORMANCE_SMOKE_MAX_PRICE_EUR,
        "selected_platforms": ["willhaben", "derstandard_at"],
        "active_search_agent_id": "perf-watch-1020",
        "search_agents": [
            {
                "agent_id": "perf-watch-1020",
                "name": "Leopoldstadt rent watch",
                "enabled": True,
                "country_code": "AT",
                "region_code": "vienna",
                "location_query": PERFORMANCE_SMOKE_LOCATION_QUERY,
                "listing_mode": "rent",
                "property_type": "apartment",
                "min_area_m2": PERFORMANCE_SMOKE_MIN_AREA_M2,
                "max_price_eur": PERFORMANCE_SMOKE_MAX_PRICE_EUR,
                "notification_limit": 3,
                "notification_period": "day",
                "preferences_json": {
                    "country_code": "AT",
                    "region_code": "vienna",
                    "location_query": PERFORMANCE_SMOKE_LOCATION_QUERY,
                    "listing_mode": "rent",
                    "property_type": "apartment",
                    "min_area_m2": PERFORMANCE_SMOKE_MIN_AREA_M2,
                    "max_price_eur": PERFORMANCE_SMOKE_MAX_PRICE_EUR,
                    "selected_platforms": ["willhaben", "derstandard_at"],
                },
            },
            {
                "agent_id": "perf-watch-1130",
                "name": "Hietzing buy watch",
                "enabled": False,
                "country_code": "AT",
                "region_code": "vienna",
                "location_query": "1130 Vienna",
                "listing_mode": "buy",
                "property_type": "apartment",
                "notification_limit": 5,
                "notification_period": "week",
                "preferences_json": {
                    "country_code": "AT",
                    "region_code": "vienna",
                    "location_query": "1130 Vienna",
                    "listing_mode": "buy",
                    "property_type": "apartment",
                    "selected_platforms": ["willhaben"],
                },
            },
        ],
    }
    if saved_candidates is not None:
        payload["saved_shortlist_candidates"] = saved_candidates
    return payload


def _seed_saved_agents(client: TestClient, *, saved_candidates: list[dict[str, object]] | None = None) -> None:
    response = client.post(
        "/v1/onboarding/property-search/preferences",
        json=_property_preferences_payload(saved_candidates=saved_candidates),
    )
    if response.status_code != 200:
        raise RuntimeError(f"saved_agents_seed_failed:{response.status_code}:{response.text[:280]}")


def _synthetic_candidate(*, saved_from_run_id: str = "") -> dict[str, object]:
    candidate = {
        "candidate_ref": "perf-candidate-1020",
        "rank": 1,
        "title": "Performance smoke apartment in 1020 Vienna",
        "source_label": "Willhaben | Austria | Rent | 1020 Vienna",
        "source_platform": "willhaben",
        "property_url": "https://example.invalid/propertyquarry/performance-smoke",
        "packet_url": "/app/research/perf-candidate-1020",
        "review_url": "/app/research/perf-candidate-1020",
        "fit_score": 91,
        "score": 91,
        "fit_summary": "Transit, area, layout and budget fit the seeded brief.",
        "match_reasons": ["1020 Vienna matches the seeded search area.", "The synthetic listing keeps route and layout data compact."],
        "mismatch_reasons": ["Operating costs are still missing from the listing."],
        "property_facts": {
            "postal_code": "1020",
            "postal_name": "1020 Vienna",
            "district": "1020 Vienna",
            "street_address": "Nordbahnstrasse 32",
            "exact_address": "Nordbahnstrasse 32, 1020 Vienna, Austria",
            "map_lat": 48.22317,
            "map_lng": 16.39594,
            "map_location_precision": "address",
            "location_hint_research_attempted": True,
            "price_display": "EUR 1,290",
            "price_eur": 1290,
            "area_m2": 72,
            "area_sqm": 72,
            "rooms": 3,
            "has_floorplan": True,
            "has_balcony": True,
            "operating_costs_status": "missing",
            "nearest_playground_m": 310,
            "nearest_playground_name": "Rudolfspark Spielplatz",
            "nearest_playground_source": "OpenStreetMap",
            "nearest_supermarket_m": 280,
            "nearest_supermarket_name": "BILLA Praterstern",
            "nearest_supermarket_source": "OpenStreetMap",
            "nearest_pharmacy_m": 640,
            "nearest_pharmacy_name": "Apotheke Nordbahn",
            "nearest_pharmacy_source": "OpenStreetMap",
            "nearest_subway_m": 350,
            "nearest_subway_name": "Praterstern",
            "nearest_subway_source": "OpenStreetMap",
            "nearest_tram_bus_m": 180,
            "nearest_tram_bus_name": "Nordbahnstrasse",
            "nearest_tram_bus_source": "OpenStreetMap",
            "nearest_flowing_water_m": 890,
            "nearest_flowing_water_name": "Donaukanal",
            "nearest_flowing_water_kind": "canal",
            "nearest_flowing_water_source": "OpenStreetMap",
            "listing_fact_confirmation": {
                "status": "confirmed",
                "label": "Listing facts",
                "summary": "4 listing facts read automatically from the listing.",
                "fields": ["area", "location", "price", "rooms"],
                "sources": {
                    "area": "provider_structured_fact",
                    "location": "provider_structured_fact",
                    "price": "provider_structured_fact",
                    "rooms": "provider_structured_fact",
                },
                "requires_manual_confirmation": False,
            },
        },
        "route_evidence": [
            {"label": "Transit", "distance": "350 m", "icon": "U"},
            {"label": "School", "distance": "650 m", "icon": "S"},
        ],
    }
    if saved_from_run_id:
        candidate["saved_from_run_id"] = saved_from_run_id
    return candidate


def _synthetic_search_result(*args: object, **kwargs: object) -> dict[str, object]:
    progress_callback = kwargs.get("progress_callback")
    if callable(progress_callback):
        progress_callback(
            step="sources_resolved",
            message="Resolved synthetic performance smoke source.",
            status="in_progress",
            steps_delta=1,
            summary_updates={"sources_total": 1, "source_variant_total": 1, "provider_total": 1},
        )
    candidate = _synthetic_candidate()
    return {
        "generated_at": _now_iso(),
        "status": "processed",
        "sources_total": 1,
        "source_variant_total": 1,
        "provider_total": 1,
        "listing_total": 1,
        "raw_listing_total": 1,
        "reviewed_listing_total": 1,
        "ranked_total": 1,
        "filtered_total": 0,
        "held_back_total": 0,
        "review_created_total": 1,
        "review_existing_total": 0,
        "notified_total": 0,
        "email_notified_total": 0,
        "tour_created_total": 0,
        "tour_existing_total": 0,
        "high_fit_total": 1,
        "watch_notified_total": 0,
        "ranked_candidates": [candidate],
        "top_candidates": [candidate],
        "sources": [
            {
                "source_label": "Willhaben | Austria | Rent | 1020 Vienna",
                "platform": "willhaben",
                "status": "completed",
                "listing_total": 1,
                "ranked_total": 1,
            }
        ],
    }


def _start_synthetic_run(client: TestClient) -> str:
    original: Callable[..., dict[str, object]] = ProductService.sync_direct_property_scout
    ProductService.sync_direct_property_scout = _synthetic_search_result  # type: ignore[method-assign]
    try:
        response = client.post(
            "/app/api/property/search-runs",
            json={
                "selected_platforms": ["willhaben"],
                "property_preferences": {
                    "country_code": "AT",
                    "region_code": "vienna",
                    "listing_mode": "rent",
                    "property_type": ["apartment"],
                    "location_query": PERFORMANCE_SMOKE_LOCATION_QUERY,
                    "min_area_m2": PERFORMANCE_SMOKE_MIN_AREA_M2,
                    "max_price_eur": PERFORMANCE_SMOKE_MAX_PRICE_EUR,
                },
                "max_results_per_source": 1,
            },
        )
        if response.status_code != 202:
            raise RuntimeError(f"synthetic_run_start_failed:{response.status_code}:{response.text[:280]}")
        run_id = str(response.json().get("run_id") or "").strip()
        if not run_id:
            raise RuntimeError("synthetic_run_missing_run_id")
        for _ in range(160):
            status = client.get(f"/app/api/property/search-runs/{run_id}")
            if status.status_code == 200 and str(status.json().get("status") or "").lower() in {"processed", "completed"}:
                _seed_saved_agents(client, saved_candidates=[_synthetic_candidate(saved_from_run_id=run_id)])
                return run_id
            time.sleep(0.025)
        raise RuntimeError(f"synthetic_run_timeout:{run_id}")
    finally:
        ProductService.sync_direct_property_scout = original  # type: ignore[method-assign]


def _open_workspace_access_session(client: TestClient) -> None:
    response = client.post(
        "/app/api/access-sessions",
        json={
            "email": "performance-smoke@propertyquarry.test",
            "role": "principal",
            "display_name": "Performance Smoke",
            "expires_in_hours": 24,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"access_session_seed_failed:{response.status_code}:{response.text[:280]}")
    access_url = str(response.json().get("access_url") or "").strip()
    if not access_url:
        raise RuntimeError("access_session_seed_failed:missing_access_url")
    client.headers.pop("X-EA-Principal-ID", None)
    opened = client.get(access_url, follow_redirects=False)
    if opened.status_code != 303 or not client.cookies.get("ea_workspace_session"):
        raise RuntimeError(f"access_session_open_failed:{opened.status_code}:{opened.text[:280]}")


def _request_measured_route(client: TestClient, path: str) -> tuple[object, int]:
    request_headers = {
        "host": "propertyquarry.com",
        "accept-encoding": "gzip" if path == "/app/search" else "identity",
    }
    started = time.perf_counter()
    response = client.get(
        path,
        headers=request_headers,
        follow_redirects=not (path.startswith("/app/research/") or path == "/app/billing"),
    )
    duration_ms = round((time.perf_counter() - started) * 1000)
    return response, duration_ms


def _measure_route(client: TestClient, path: str, *, budget_ms: int) -> dict[str, object]:
    response, duration_ms = _request_measured_route(client, path)
    first_duration_ms = duration_ms
    attempt_durations_ms = [duration_ms]
    attempt_count = 1
    if duration_ms > budget_ms:
        retry_response, retry_duration_ms = _request_measured_route(client, path)
        attempt_durations_ms.append(retry_duration_ms)
        attempt_count = 2
        if retry_duration_ms < duration_ms:
            response = retry_response
            duration_ms = retry_duration_ms
    body = response.text or ""
    lowered_body = body.lower()
    visible_body = _visible_text(body)
    lowered_visible_body = visible_body.lower()
    css_body = ""
    if path != "/app/billing":
        css_body = _asset_text(client, _workbench_css_path_for_route(path, body))
    billing_redirect_location = str(response.headers.get("location") or "").strip()
    billing_handoff_redirect_ok, billing_redirect_host = _billing_handoff_redirect_ok(
        path=path,
        status_code=response.status_code,
        location=billing_redirect_location,
    )
    billing_internal_account_fallback_ok = _billing_internal_account_fallback_ok(
        path=path,
        status_code=response.status_code,
        location=billing_redirect_location,
    )
    noise_hits = [
        phrase
        for phrase in FORBIDDEN_CUSTOMER_NOISE
        if phrase in lowered_body
    ]
    visible_internal_hits = [
        phrase
        for phrase in FORBIDDEN_VISIBLE_INTERNAL_COPY
        if phrase in lowered_visible_body
    ]
    billing_fail_closed_ok = path == "/app/billing" and response.status_code == 503
    checks = [
        {
            "name": "status_ok",
            "ok": response.status_code == 200 or billing_handoff_redirect_ok or billing_internal_account_fallback_ok or billing_fail_closed_ok,
        },
        {"name": "under_budget", "ok": duration_ms <= budget_ms},
        {"name": "contains_propertyquarry", "ok": "PropertyQuarry" in body or billing_handoff_redirect_ok or billing_internal_account_fallback_ok},
        {"name": "no_generic_ea_copy", "ok": "Executive Assistant" not in body and "Morning Memo" not in body},
        {"name": "no_customer_jargon", "ok": not noise_hits, "detail": ", ".join(noise_hits[:5])},
        {
            "name": "no_visible_internal_proof_copy",
            "ok": not visible_internal_hits,
            "detail": ", ".join(visible_internal_hits[:5]),
        },
    ]
    if billing_handoff_redirect_ok:
        checks.append(
            {
                "name": "billing_external_handoff_redirect",
                "ok": True,
                "location_host": billing_redirect_host,
            }
        )
    elif billing_internal_account_fallback_ok:
        checks.append(
            {
                "name": "billing_internal_account_fallback",
                "ok": True,
                "location": billing_redirect_location,
            }
        )
    elif not billing_fail_closed_ok:
        checks.extend(_mobile_surface_contract_checks(path, body, css_body=css_body))
        checks.extend(_rybbit_surface_contract_checks(path, body))
    if path == "/app/search":
        content_encoding = str(response.headers.get("content-encoding") or "").strip().lower()
        vary_header = str(response.headers.get("vary") or "").strip().lower()
        compressed_bytes = _response_content_length(response)
        compressed_max_bytes = _search_compressed_max_bytes()
        checks.extend(
            (
                {
                    "name": "search_gzip_delivery",
                    "ok": "gzip" in content_encoding,
                    "content_encoding": content_encoding or "missing",
                },
                {
                    "name": "search_gzip_vary_accept_encoding",
                    "ok": "accept-encoding" in vary_header,
                    "vary": vary_header or "missing",
                },
                {
                    "name": "search_compressed_payload_under_budget",
                    "ok": 0 < compressed_bytes <= compressed_max_bytes,
                    "compressed_bytes": compressed_bytes,
                    "max_bytes": compressed_max_bytes,
                },
                {
                    "name": "what_matters_distance_controls_compact",
                    "ok": (
                        "grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 320px));" in css_body
                        and "justify-content: start;" in css_body
                        and "max-width: 150px;" in css_body
                        and "grid-template-columns: minmax(0, 1fr) minmax(104px, 110px) minmax(96px, 100px);" in css_body
                        and "grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));" not in css_body
                    ),
                },
                {
                    "name": "what_matters_school_distance_controls",
                    "ok": (
                        'name="school_distance__kindergarten"' in body
                        and 'name="school_distance__ganztags_volksschule"' in body
                        and 'name="school_distance__halbtags_volksschule"' in body
                        and 'data-distance-field="max_distance_to_kindergarten_m"' in body
                        and 'data-distance-field="max_distance_to_ganztags_volksschule_m"' in body
                        and 'data-distance-field="max_distance_to_halbtags_volksschule_m"' in body
                    ),
                },
            )
        )
    if path == "/app/agents":
        checks.extend(
            (
                {
                    "name": "agent_cards",
                    "ok": (
                        "Leopoldstadt rent watch" in body
                        and "Hietzing buy watch" in body
                        and "1 active" in visible_body
                        and "2 saved" in visible_body
                    ),
                },
                {"name": "map_only_thumbnails", "ok": "osm_district_overlay" in body and "Map preview unavailable" not in body},
            )
        )
    if path.startswith("/app/properties") or path.startswith("/app/shortlist"):
        compare_hits = [token for token in FORBIDDEN_COMPARE_CARD_TOKENS if token in body]
        has_ranked_results_shell = (
            ('data-workbench-results-table' in body and "pqx-rank" in body)
            or ('data-pqx-ranked-candidates' in body and "pqx-rank" in body)
            or ('data-pq-fast-ranked-run' in body and "ranked homes" in lowered_body)
        )
        checks.extend(
            (
                {
                    "name": "results_ranking_only_no_compare_cards",
                    "ok": has_ranked_results_shell and not compare_hits,
                    "detail": ", ".join(compare_hits[:5]),
                },
                {
                    "name": "results_ranked_not_compare_copy",
                    "ok": has_ranked_results_shell and (
                        "ranked homes" in lowered_body
                        or "ranked opportunities" in lowered_body
                        or "shortlisted homes" in lowered_body
                        or "matching homes" in lowered_body
                        or "matching opportunities" in lowered_body
                        or "saved homes" in lowered_body
                        or "best matches" in lowered_body
                        or " matches" in lowered_body
                    ),
                },
            )
        )
    if path.startswith("/app/research/"):
        unevidenced_visual_ready = (
            'data-prd-visual-card="tour"' in body
            and 'data-prd-visual-card="walkthrough"' in body
            and 'data-pw-visual-state="ready"' in body
            and ("Request 3D tour" in body or "Request walkthrough" in body)
        )
        research_css_anchor = body.find(".prd-topbar")
        mobile_css_start = body.find("@media (max-width: 760px)", research_css_anchor if research_css_anchor >= 0 else 0)
        mobile_css_end = body.find("</style>", mobile_css_start) if mobile_css_start >= 0 else -1
        mobile_detail_css = body[mobile_css_start:mobile_css_end] if mobile_css_start >= 0 and mobile_css_end > mobile_css_start else ""
        compare_hits = [token for token in FORBIDDEN_COMPARE_CARD_TOKENS if token in body]
        checks.extend(
            (
                {"name": "research_candidate", "ok": "Performance smoke apartment in 1020 Vienna" in body},
                {"name": "media_requests_explicit", "ok": "Request" in body and "tour" in body.lower()},
                {"name": "research_visual_cards_present", "ok": 'data-prd-visual-card="tour"' in body and 'data-prd-visual-card="walkthrough"' in body},
                {"name": "research_visual_requests_honest", "ok": 'data-pw-visual-request="tour"' in body and 'data-pw-visual-request="flythrough"' in body and 'data-pw-visual-state="idle"' in body},
                {"name": "research_no_fake_visual_ready", "ok": not unevidenced_visual_ready},
                {"name": "research_listing_facts", "ok": "Listing facts" in body and "read automatically from the listing" in body},
                {"name": "research_listed_price_signal", "ok": "Budget signal" in body and "EUR 1,290" in body},
                {
                    "name": "research_ranking_only_no_compare_cards",
                    "ok": "Performance smoke apartment in 1020 Vienna" in body and not compare_hits,
                    "detail": ", ".join(compare_hits[:5]),
                },
                {
                    "name": "research_mobile_open_property_compact_layout",
                    "ok": (
                        ".prd-hero {\n      grid-template-columns: minmax(0, 1fr);\n      gap: 6px;" in mobile_detail_css
                        and (
                            (
                                ".prd-current-read {\n      display: grid;" in mobile_detail_css
                                and ".prd-current-read .prd-summary-grid {\n      grid-template-columns: minmax(0, 1fr);" in mobile_detail_css
                                and ".prd-current-read .prd-summary-box {\n      min-height: 68px;" in mobile_detail_css
                            )
                            or ".prd-current-read {\n      display: none;" in mobile_detail_css
                        )
                        and (
                            ".prd-media-frame {\n      height: min(46vw, 176px);" in mobile_detail_css
                            or ".prd-media-frame {\n      height: min(42vw, 160px);" in mobile_detail_css
                        )
                    ),
                },
                {
                    "name": "research_mobile_visual_frame_compact",
                    "ok": (
                        ".prd-media-frame.prd-media-frame-live {\n      height: min(58vw, 224px);" in mobile_detail_css
                        and ".prd-media-gradient,\n    .prd-media-caption {\n      display: none;" in mobile_detail_css
                    ),
                },
            )
        )
    if path.startswith("/app/alerts"):
        checks.extend(
            (
                {"name": "alerts_heading", "ok": "Alerts" in body},
                {"name": "delivery_controls", "ok": "Delivery rules" in body or "Notifications" in body},
            )
        )
    if path == "/app/billing" and not billing_handoff_redirect_ok and not billing_internal_account_fallback_ok:
        billing_noise_hits = [token for token in FORBIDDEN_BILLING_SURFACE_TOKENS if token in lowered_body]
        checks.extend(
            (
                {
                    "name": "billing_fail_closed_recovery",
                    "ok": response.status_code == 503
                    and "billing portal unavailable" in lowered_body
                    and "propertyquarry access stays active" in lowered_body
                    and any(marker in lowered_body for marker in BILLING_FAIL_CLOSED_STATE_MARKERS),
                },
                {"name": "billing_local_board_deleted", "ok": not billing_noise_hits, "detail": ", ".join(billing_noise_hits[:5])},
            )
        )
    if path == "/app/account":
        checks.extend(
            (
                {"name": "account_direct_logout_strip", "ok": "pqx-account-logout-strip" in body and "Current session" in body},
                {"name": "account_single_logout_action", "ok": body.count('data-account-page-sign-out') == 1 and body.count(">Log out</button>") == 1},
                {"name": "account_no_top_dropdown_duplicate_logout", "ok": '<form class="pqx-account-menu-form"' not in body},
                {
                    "name": "account_logout_mobile_target",
                    "ok": ".pqx-account-logout-strip-form .pqx-link-button" in css_body
                    and (
                        "min-height: 46px;" in css_body
                        or "min-height: 48px;" in css_body
                        or "min-height: 52px;" in css_body
                        or "min-height: 56px;" in css_body
                    ),
                },
                {
                    "name": "notification_destination_controls",
                    "ok": all(token in body for token in ("Email", "Telegram", "WhatsApp"))
                    and (
                        "Destination mix" in body
                        or "Strong matches can land in more than one place." in body
                        or "Where matches arrive" in body
                    ),
                },
                {
                    "name": "notification_primary_channel_controls",
                    "ok": ("Primary response lane" in body or "Primary route" in body)
                    and "Save notifications" in body,
                },
                {
                    "name": "notification_opt_in_copy",
                    "ok": ("Strong matches and watch hits" in body or "Strong matches go to every selected channel." in body)
                    and ("Near-miss follow-up prompts" in body or "Near-miss follow-up stays Telegram-only when Telegram is primary." in body),
                },
                {"name": "notification_secret_safe", "ok": "telegram-secret-token" not in body and "raw_delivery_receipts" not in body},
                {"name": "account_notifications", "ok": "<h2>Notifications</h2>" in body},
                {"name": "account_notification_form", "ok": 'action="/app/api/property/account/notifications"' in body},
                {"name": "account_notification_email_channel", "ok": 'name="notification_channels" value="email"' in body},
                {"name": "account_notification_telegram_channel", "ok": 'name="notification_channels" value="telegram"' in body},
                {"name": "account_notification_whatsapp_channel", "ok": 'name="notification_channels" value="whatsapp"' in body},
                {"name": "account_notification_primary_route", "ok": 'name="preferred_channel"' in body},
                {"name": "account_notification_whatsapp_phone", "ok": 'name="whatsapp_ai_support_phone"' in body},
                {"name": "account_notification_save_action", "ok": "Save notifications" in body},
            )
        )
    if path == "/app/settings/google":
        checks.extend(
            (
                {"name": "google_settings_heading", "ok": "Google sign-in" in body or "PropertyQuarry Google connection" in body},
                {"name": "implicit_account_creation_copy", "ok": "Continue with Google" in body or "Google sign-in" in body},
            )
        )
    if path == "/app/settings/access":
        checks.extend(
            (
                {"name": "access_settings_heading", "ok": "Access" in body or "Identity and return access" in body},
                {"name": "account_access_controls", "ok": "Invite" in body or "access" in lowered_body},
            )
        )
    if path == "/app/settings/usage":
        checks.extend(
            (
                {"name": "usage_settings_heading", "ok": "Usage and activation" in body},
                {"name": "usage_metrics_visible", "ok": "Searches opened" in body or "activation" in lowered_body},
            )
        )
    if path == "/app/settings/support":
        checks.extend(
            (
                {"name": "support_settings_heading", "ok": "Support" in body or "Support and recovery" in body},
                {"name": "support_recovery_controls", "ok": "recovery" in lowered_body or "support" in lowered_body},
            )
        )
    if path == "/app/settings/trust":
        checks.extend(
            (
                {"name": "trust_settings_heading", "ok": "Search health" in body or "Reliability" in body or "Trust" in body},
                {"name": "trust_evidence_visible", "ok": "source health" in lowered_body or "list health" in lowered_body},
            )
        )
    if path == "/app/settings/invitations":
        checks.extend(
            (
                {"name": "invitations_settings_heading", "ok": "Invitations" in body},
                {"name": "invitation_controls_visible", "ok": "Invite" in body or "invitation" in lowered_body},
            )
        )
    if path == "/sign-in":
        secure_email_access_copy = "use a secure email link if your address already has access." in lowered_visible_body
        safe_provider_copy = "continue with an available provider." in lowered_visible_body
        checks.extend(
            (
                {
                    "name": "connected_identity_implicit_account_creation",
                    "ok": secure_email_access_copy and safe_provider_copy,
                },
                {
                    "name": "connected_identity_copy_is_customer_safe",
                    "ok": (
                        "oauth_config_missing" not in lowered_body
                        and "callback setup" not in lowered_body
                        and secure_email_access_copy
                        and safe_provider_copy
                    ),
                },
            )
        )
    return {
        "path": path,
        "status_code": response.status_code,
        "duration_ms": duration_ms,
        "first_duration_ms": first_duration_ms,
        "attempt_durations_ms": attempt_durations_ms,
        "attempt_count": attempt_count,
        "budget_ms": budget_ms,
        "ok": all(bool(row["ok"]) for row in checks),
        "checks": checks,
    }


def build_authenticated_performance_receipt(*, route_budget_ms: int = 1200) -> dict[str, object]:
    _reset_authenticated_performance_smoke_env()
    os.environ["EA_STORAGE_BACKEND"] = "memory"
    os.environ.pop("EA_LEDGER_BACKEND", None)
    os.environ.pop("DATABASE_URL", None)
    # Keep prod-mode startup valid even when this smoke runs outside the live container.
    os.environ["EA_RUNTIME_MODE"] = "dev"
    os.environ["EA_API_TOKEN"] = "performance-smoke-local-token"
    os.environ["PROPERTYQUARRY_MAP_TILE_NETWORK_ENABLED"] = "0"
    os.environ["PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES"] = "1"
    os.environ["EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER"] = "1"
    os.environ["PROPERTYQUARRY_BRILLIANT_DIRECTORIES_ENABLED"] = "1"
    os.environ["PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_ENABLED"] = "1"
    os.environ["PROPERTYQUARRY_BRILLIANT_DIRECTORIES_DISABLED"] = "0"
    os.environ["PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BASE_URL"] = "https://billing.propertyquarry.test"
    os.environ["PROPERTYQUARRY_BRILLIANT_DIRECTORIES_ALLOWED_HOSTS"] = "billing.propertyquarry.test"
    os.environ["PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BILLING_URL"] = "https://billing.propertyquarry.test/account"
    os.environ["PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY"] = "performance-smoke-local-key"
    api_token = str(os.environ.get("EA_API_TOKEN") or "").strip()
    principal_id = "pq-auth-performance-smoke"
    app = create_app()
    prewarm_property_search_surface_cache()
    with TestClient(app, base_url="https://propertyquarry.com") as client:
        client.headers.update(
            {
                "X-EA-Principal-ID": principal_id,
                "X-EA-API-Token": api_token,
                "Authorization": f"Bearer {api_token}",
                "host": "propertyquarry.com",
            }
        )
        _seed_workspace(client)
        _seed_saved_agents(client)
        prewarm_property_search_shell_cache(container=app.state.container, principal_id=principal_id)
        run_id = _start_synthetic_run(client)
        _open_workspace_access_session(client)
        for warm_path in (
            "/app/search",
            "/app/agents",
            f"/app/properties?run_id={run_id}",
            f"/app/shortlist?run_id={run_id}",
        ):
            _request_measured_route(client, warm_path)
        routes = [
            "/sign-in",
            "/app/search",
            "/app/agents",
            f"/app/properties?run_id={run_id}",
            f"/app/shortlist?run_id={run_id}",
            f"/app/research/perf-candidate-1020?run_id={run_id}",
            f"/app/alerts?run_id={run_id}",
            "/app/account",
            "/app/billing",
            "/app/settings/google",
            "/app/settings/access",
            "/app/settings/usage",
            "/app/settings/support",
            "/app/settings/trust",
            "/app/settings/invitations",
        ]
        rows = [
            _measure_route(client, route, budget_ms=_route_budget_for(route, route_budget_ms=route_budget_ms))
            for route in routes
        ]
    failed = [row for row in rows if not row.get("ok")]
    return {
        "status": "pass" if not failed else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "principal_id": principal_id,
        "run_id": run_id,
        "route_count": len(rows),
        "failed_count": len(failed),
        "routes": rows,
        "notes": [
            "This smoke is local, authenticated, provider-free and non-networked.",
            "It guards warmed startup route budgets for sign-in, search, agents, results, research, alerts, account, billing, and settings surfaces.",
            "It also asserts shared top navigation, viewport metadata, app shell, no legacy mobile bottom dock, and content-first mobile layouts for static account/billing/settings surfaces.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run authenticated PropertyQuarry route performance smoke.")
    parser.add_argument("--route-budget-ms", type=int, default=1200)
    parser.add_argument("--write", default="", help="Optional JSON receipt output path.")
    args = parser.parse_args()
    receipt = build_authenticated_performance_receipt(route_budget_ms=max(1, int(args.route_budget_ms or 1200)))
    body = json.dumps(receipt, indent=2, sort_keys=True)
    if str(args.write or "").strip():
        out_path = Path(str(args.write)).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body + "\n", encoding="utf-8")
    print(body)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # The smoke boots the full app and can leave non-daemon provider/testclient
    # helper threads alive during interpreter shutdown. The receipt is complete
    # once flushed, so fail/exit deterministically instead of hanging CI.
    os._exit(exit_code)
