#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from app.services.property_market_catalog import (
    COUNTRIES,
    CUSTOMER_SEARCH_COUNTRY_ORDER,
    default_platforms_for_country_listing_mode,
    is_customer_search_country_code,
    provider_options,
    selectable_property_platform_keys,
)
from app.api.principal_identity import (
    PRINCIPAL_ASSERTION_AUDIENCE_HEADER,
    PRINCIPAL_ASSERTION_NONCE_HEADER,
    PRINCIPAL_ASSERTION_SIGNATURE_HEADER,
    PRINCIPAL_ASSERTION_TIMESTAMP_HEADER,
    principal_assertion_signature,
)
from scripts.propertyquarry_live_http_security import validated_live_base_origin
from scripts.propertyquarry_live_probe_auth import live_probe_request_headers
from scripts.propertyquarry_live_probe_secret_scope import (
    read_edge_assertion_secret_from_stdin,
    read_release_probe_secret_from_stdin,
    scrub_edge_assertion_secret_environment,
    scrub_release_probe_secret_environment,
)


_TARGET_CONTEXT_BY_COUNTRY = {
    "AT": ("1010 Vienna", "1020 Vienna"),
    "BE": ("Brussels", "Antwerp"),
    "CA": ("Toronto", "Vancouver"),
    "CR": ("San Jose", "Escazu"),
    "DE": ("Berlin", "Munich"),
    "CH": ("Zurich", "Geneva"),
    "IE": ("Dublin", "Cork"),
    "UK": ("London", "Manchester"),
    "AU": ("Sydney", "Melbourne"),
    "ES": ("Barcelona", "Madrid"),
    "IT": ("Milan", "Rome"),
    "FR": ("Paris", "Lyon"),
    "NL": ("Amsterdam", "Rotterdam"),
    "PT": ("Lisbon", "Porto"),
    "PL": ("Warsaw", "Krakow"),
    "SE": ("Stockholm", "Gothenburg"),
    "US": ("Brooklyn", "Austin"),
}

DOTENV_PATHS = (
    ROOT / ".env",
    ROOT / ".env.local",
)


class _WallClockTimeout(RuntimeError):
    pass


@contextlib.contextmanager
def _wall_clock_timeout(seconds: float):
    timeout_seconds = max(float(seconds or 0), 0.0)
    if timeout_seconds <= 0 or not hasattr(signal, "setitimer"):
        yield
        return

    def _raise_timeout(_signum, _frame) -> None:
        raise _WallClockTimeout(f"request exceeded {timeout_seconds:.1f}s wall-clock timeout")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def _enabled() -> bool:
    return str(os.getenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
        "live",
    }


def _env_value(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def _dotenv_value(name: str, *, dotenv_paths: tuple[Path, ...] = DOTENV_PATHS) -> str:
    normalized_name = str(name or "").strip()
    if not normalized_name:
        return ""
    for path in dotenv_paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() != normalized_name:
                continue
            normalized_value = value.strip().strip('"').strip("'")
            if normalized_value:
                return normalized_value
    return ""


def _default_api_token(*, dotenv_paths: tuple[Path, ...] = DOTENV_PATHS) -> str:
    return (
        _env_value("PROPERTYQUARRY_LIVE_API_TOKEN")
        or _env_value("EA_API_TOKEN")
        or _dotenv_value("PROPERTYQUARRY_LIVE_API_TOKEN", dotenv_paths=dotenv_paths)
        or _dotenv_value("EA_API_TOKEN", dotenv_paths=dotenv_paths)
    )


def _default_principal_id() -> str:
    return str(
        os.getenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_PRINCIPAL_ID")
        or os.getenv("EA_PRINCIPAL_ID")
        or "cf-email:person@example.test"
    ).strip()


def _request_headers(
    *,
    user_agent: str,
    principal_id: str,
    api_token: str,
    host_header: str = "propertyquarry.com",
    content_type: str = "",
    method: str = "GET",
    url: str = "",
    edge_assertion_secret: str = "",
    edge_assertion_audience: str = "",
) -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json,text/html,*/*",
        "Host": host_header,
        "X-EA-Principal-ID": principal_id,
    }
    if content_type:
        headers["Content-Type"] = content_type
    normalized_token = str(api_token or "").strip()
    if normalized_token:
        headers["Authorization"] = f"Bearer {normalized_token}"
        headers["X-EA-API-Token"] = normalized_token
        headers["X-API-Token"] = normalized_token
    normalized_assertion_secret = str(edge_assertion_secret or "").strip()
    normalized_assertion_audience = str(edge_assertion_audience or "").strip()
    if bool(normalized_assertion_secret) != bool(normalized_assertion_audience):
        raise ValueError("edge_assertion_configuration_incomplete")
    if normalized_assertion_secret:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("edge_assertion_url_required")
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(24)
        normalized_method = str(method or "GET").strip().upper()
        headers[PRINCIPAL_ASSERTION_TIMESTAMP_HEADER] = str(timestamp)
        headers[PRINCIPAL_ASSERTION_NONCE_HEADER] = nonce
        headers[PRINCIPAL_ASSERTION_AUDIENCE_HEADER] = normalized_assertion_audience
        headers[PRINCIPAL_ASSERTION_SIGNATURE_HEADER] = principal_assertion_signature(
            secret=normalized_assertion_secret,
            method=normalized_method,
            path=str(parsed.path or "/"),
            query_string=str(parsed.query or ""),
            principal_id=str(principal_id or "").strip(),
            timestamp=timestamp,
            nonce=nonce,
            audience=normalized_assertion_audience,
        )
    return headers


def _dry_run() -> bool:
    return str(os.getenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
        "live",
    }


def _execute_search_matrix_enabled() -> bool:
    return str(os.getenv("PROPERTYQUARRY_LIVE_PROVIDER_SEARCH_E2E") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
        "live",
    }


def _effective_search_run_timeout_seconds(
    timeout_seconds: float,
    *,
    enabled: bool,
    dry_run: bool,
    search_run_timeout_seconds: float | None = None,
) -> float:
    normalized_timeout = max(float(timeout_seconds or 0), 0.0)
    if search_run_timeout_seconds is not None:
        normalized_search_timeout = max(float(search_run_timeout_seconds or 0), 0.0)
        if normalized_search_timeout > 0:
            return normalized_search_timeout
    if normalized_timeout <= 0:
        return 0.0
    if enabled and not dry_run:
        return max(normalized_timeout, _default_live_search_run_timeout_seconds())
    return normalized_timeout


def _default_live_search_run_timeout_seconds() -> float:
    for key in (
        "PROPERTYQUARRY_LIVE_PROVIDER_SEARCH_RUN_TIMEOUT_SECONDS",
        "PROPERTYQUARRY_DEPLOY_PROVIDER_SEARCH_RUN_TIMEOUT_SECONDS",
    ):
        raw_value = str(os.getenv(key) or "").strip()
        if not raw_value:
            continue
        try:
            parsed = float(raw_value)
        except Exception:
            continue
        if parsed > 0:
            return parsed
    return 60.0


def _all_search_ready_country_codes() -> tuple[str, ...]:
    countries: list[str] = []
    for code in CUSTOMER_SEARCH_COUNTRY_ORDER:
        normalized = str(code or "").strip().upper()
        if not normalized or not is_customer_search_country_code(normalized):
            continue
        if _search_ready_provider_options(normalized):
            countries.append(normalized)
    return tuple(dict.fromkeys(countries))


def _all_search_ready_countries_enabled() -> bool:
    return str(os.getenv("PROPERTYQUARRY_LIVE_PROVIDER_ALL_SEARCH_READY_COUNTRIES") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
        "all",
    }


def _target_context_for_country(country_code: str) -> dict[str, object]:
    normalized = str(country_code or "").strip().upper()
    locations = _TARGET_CONTEXT_BY_COUNTRY.get(normalized)
    if not locations:
        for country in COUNTRIES:
            if str(country.code or "").strip().upper() != normalized:
                continue
            locations = tuple(
                value.strip()
                for value in str(country.location_placeholder or "").split(",")
                if value.strip()
            )[:2]
            break
    if not locations:
        locations = (normalized,)
    if normalized == "CR":
        return {
            "location_query": ", ".join(locations),
            "selected_location_values": list(locations),
            "soft_keywords": {
                "pool": "nice_to_have",
                "supermarket nearby": "nice_to_have",
                "good internet": "nice_to_have",
            },
            "soft_distances": {
                "max_distance_to_supermarket_importance": "nice_to_have",
                "max_distance_to_supermarket_m": 1800,
            },
            "max_price_eur": 2600,
            "min_area_m2": 80,
        }
    return {
        "location_query": ", ".join(locations),
        "selected_location_values": list(locations),
        "soft_keywords": {
            "balcony": "nice_to_have",
            "lift": "nice_to_have",
            "playground nearby": "nice_to_have",
        },
        "soft_distances": {
            "max_distance_to_playground_importance": "nice_to_have",
            "max_distance_to_playground_m": 1000,
            "max_distance_to_supermarket_importance": "nice_to_have",
            "max_distance_to_supermarket_m": 900,
        },
        "max_price_eur": 2900,
        "min_area_m2": 60,
    }


def _agent_unlimited_commercial_state() -> dict[str, object]:
    return {
        "active_plan_key": "agent",
        "status": "active",
        "active_until": "2999-01-01T00:00:00+00:00",
    }


def _selectable_provider_keys_for_mode(country_code: str, listing_mode: str) -> tuple[str, ...]:
    return tuple(
        str(value or "").strip()
        for value in selectable_property_platform_keys(
            country_code=str(country_code or "").strip().upper(),
            listing_mode=str(listing_mode or "").strip().lower() or "rent",
        )
        if str(value or "").strip()
    )


def _listing_mode_for_provider(country_code: str, provider_key: str) -> str:
    normalized_provider = str(provider_key or "").strip()
    for listing_mode in ("rent", "buy"):
        if normalized_provider in _selectable_provider_keys_for_mode(country_code, listing_mode):
            return listing_mode
    return "rent"


def _first_selectable_provider(country_code: str, listing_mode: str = "rent") -> dict[str, object]:
    normalized_country = str(country_code or "").strip().upper()
    selectable = set(_selectable_provider_keys_for_mode(normalized_country, listing_mode))
    for option in _search_ready_provider_options(normalized_country):
        if str(option.get("value") or "").strip() in selectable:
            return dict(option)
    options = _search_ready_provider_options(normalized_country)
    return dict(options[0]) if options else {}


def _targeted_search_payload(*, country_code: str, provider_key: str, mode: str, listing_mode: str = "rent") -> dict[str, object]:
    context = _target_context_for_country(country_code)
    normalized_mode = str(mode or "").strip().lower()
    soft_mode = normalized_mode == "targeted_soft_filters"
    normalized_listing_mode = str(listing_mode or "").strip().lower() or "rent"
    preferences: dict[str, object] = {
        "country_code": str(country_code or "").strip().upper(),
        "listing_mode": normalized_listing_mode,
        "search_goal": "home",
        "location_query": str(context["location_query"]),
        "selected_location_values": list(context["selected_location_values"]),
        "language_code": "en",
        "preference_person_id": "self",
        "search_mode": "discovery" if soft_mode else "strict",
        "property_commercial": _agent_unlimited_commercial_state(),
    }
    if soft_mode:
        preferences.update(
            {
                "keyword_preferences": dict(context["soft_keywords"]),
                "keyword_preferences_json": json.dumps(dict(context["soft_keywords"]), sort_keys=True),
                "min_area_m2": context["min_area_m2"],
                "max_price_eur": context["max_price_eur"],
                **dict(context["soft_distances"]),
            }
        )
    return {
        "selected_platforms": [str(provider_key or "").strip()],
        "property_preferences": preferences,
        "force_refresh": True,
        "dispatch_only": True,
    }


def _target_context_country_scope_ok(country_code: str, location_query: object, selected_location_values: object) -> bool:
    normalized_country = str(country_code or "").strip().upper()
    expected_locations = {
        str(value or "").strip().lower()
        for value in _TARGET_CONTEXT_BY_COUNTRY.get(normalized_country, ())
        if str(value or "").strip()
    }
    if not expected_locations:
        return True
    observed_locations = {
        str(value or "").strip().lower()
        for value in list(selected_location_values or [])
        if str(value or "").strip()
    }
    raw_query = str(location_query or "").strip().lower()
    if normalized_country != "AT" and "vienna" in raw_query:
        return False
    return bool(observed_locations) and observed_locations.issubset(expected_locations) and any(
        location in raw_query for location in observed_locations
    )


def _search_ready_provider_options(country_code: str) -> list[dict[str, object]]:
    return [
        dict(option)
        for option in provider_options(country_code=country_code)
        if bool(option.get("search_ready")) and not bool(option.get("coming_soon"))
    ]


def _select_targeted_provider_options(
    *,
    countries: Iterable[str],
    provider_keys: Iterable[str] = (),
    max_providers: int = 0,
) -> dict[str, list[dict[str, object]]]:
    normalized_countries = tuple(
        dict.fromkeys(str(country or "").strip().upper() for country in countries if str(country or "").strip())
    )
    requested_provider_keys = tuple(
        dict.fromkeys(str(provider_key or "").strip() for provider_key in provider_keys if str(provider_key or "").strip())
    )
    allowed_provider_keys = set(requested_provider_keys)
    remaining_global_limit = max(0, int(max_providers or 0))
    limit_enabled = remaining_global_limit > 0
    selected: dict[str, list[dict[str, object]]] = {}
    for country in normalized_countries:
        country_rows: list[dict[str, object]] = []
        for option in _search_ready_provider_options(country):
            provider_key = str(option.get("value") or "").strip()
            if not provider_key:
                continue
            if allowed_provider_keys and provider_key not in allowed_provider_keys:
                continue
            if limit_enabled and remaining_global_limit <= 0:
                break
            country_rows.append(dict(option))
            if limit_enabled:
                remaining_global_limit -= 1
        selected[country] = country_rows
    return selected


def _post_search_run_payload(
    *,
    base_url: str,
    payload: dict[str, object],
    timeout_seconds: float,
    api_token: str = "",
    principal_id: str = "",
    edge_assertion_secret: str = "",
    edge_assertion_audience: str = "",
) -> dict[str, object]:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", "app/api/property/search-runs")
    normalized_api_token = str(api_token or "").strip() or _default_api_token()
    normalized_principal_id = str(principal_id or "").strip() or _default_principal_id()
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            **_request_headers(
                user_agent="PropertyQuarry-live-provider-search-matrix/1.0",
                principal_id=normalized_principal_id,
                api_token=normalized_api_token,
                content_type="application/json",
                method="POST",
                url=url,
                edge_assertion_secret=edge_assertion_secret,
                edge_assertion_audience=edge_assertion_audience,
            ),
            "X-PropertyQuarry-Dispatch-Probe": "1",
        },
    )
    with _wall_clock_timeout(timeout_seconds):
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(220_000)
    return json.loads(body.decode("utf-8", errors="replace"))


def _first_foreign_search_ready_provider(country_code: str) -> dict[str, object]:
    normalized_country = str(country_code or "").strip().upper()
    for foreign_country in _all_search_ready_country_codes():
        if foreign_country == normalized_country:
            continue
        provider = _first_selectable_provider(foreign_country, "rent")
        if provider:
            return provider
    return {}


def _cross_country_sanitization_payload(
    *,
    country_code: str,
    local_provider_key: str,
    foreign_provider_key: str,
    listing_mode: str = "rent",
) -> dict[str, object]:
    context = _target_context_for_country(country_code)
    return {
        "selected_platforms": [str(foreign_provider_key or "").strip(), str(local_provider_key or "").strip()],
        "property_preferences": {
            "country_code": str(country_code or "").strip().upper(),
            "listing_mode": str(listing_mode or "").strip().lower() or "rent",
            "search_goal": "home",
            "location_query": str(context["location_query"]),
            "selected_location_values": list(context["selected_location_values"]),
            "language_code": "en",
            "preference_person_id": "self",
            "search_mode": "strict",
            "property_commercial": _agent_unlimited_commercial_state(),
        },
        "force_refresh": True,
        "dispatch_only": True,
    }


def _build_cross_country_sanitization_checks(
    *,
    countries: Iterable[str],
    base_url: str,
    enabled: bool,
    dry_run: bool,
    timeout_seconds: float,
    search_executor: Callable[[dict[str, object], float], dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    executor = search_executor or (lambda payload, timeout: _post_search_run_payload(base_url=base_url, payload=payload, timeout_seconds=timeout))
    rows: list[dict[str, object]] = []
    for country in countries:
        normalized_country = str(country or "").strip().upper()
        local_provider = _first_selectable_provider(normalized_country, "rent")
        foreign_provider = _first_foreign_search_ready_provider(normalized_country)
        local_provider_key = str(local_provider.get("value") or "").strip()
        foreign_provider_key = str(foreign_provider.get("value") or "").strip()
        listing_mode = _listing_mode_for_provider(normalized_country, local_provider_key)
        row: dict[str, object] = {
            "country_code": normalized_country,
            "listing_mode": listing_mode,
            "local_provider": local_provider_key,
            "local_provider_country_code": str(local_provider.get("country_code") or "").strip().upper(),
            "foreign_provider": foreign_provider_key,
            "foreign_provider_country_code": str(foreign_provider.get("country_code") or "").strip().upper(),
            "endpoint": "/app/api/property/search-runs",
            "status": "skipped" if not enabled else "dry_run" if dry_run else "planned",
        }
        if not local_provider_key or not foreign_provider_key:
            row.update(
                {
                    "status": "skipped_no_cross_country_case",
                    "sanitization_ok": True,
                }
            )
            rows.append(row)
            continue
        payload = _cross_country_sanitization_payload(
            country_code=normalized_country,
            local_provider_key=local_provider_key,
            foreign_provider_key=foreign_provider_key,
            listing_mode=listing_mode,
        )
        row["requested_platforms"] = list(payload.get("selected_platforms") or [])
        if enabled and not dry_run:
            try:
                response = dict(executor(payload, timeout_seconds) or {})
                selected_platforms = [
                    str(value or "").strip()
                    for value in list(response.get("selected_platforms") or [])
                    if str(value or "").strip()
                ]
                summary = dict(response.get("summary") or {})
                removed = [
                    str(value or "").strip()
                    for value in list(summary.get("provider_country_filter_removed") or [])
                    if str(value or "").strip()
                ]
                sanitization_ok = (
                    local_provider_key in selected_platforms
                    and foreign_provider_key not in selected_platforms
                    and bool(summary.get("provider_country_filter_applied"))
                    and foreign_provider_key in removed
                )
                row.update(
                    {
                        "status": "pass" if sanitization_ok else "fail",
                        "run_id": str(response.get("run_id") or "").strip(),
                        "runtime_status": str(response.get("status") or "").strip().lower(),
                        "sanitized_platforms": selected_platforms,
                        "removed_platforms": removed,
                        "summary_filter_applied": bool(summary.get("provider_country_filter_applied")),
                        "sanitization_ok": sanitization_ok,
                    }
                )
            except Exception as exc:
                row.update(
                    {
                        "status": "fail",
                        "error": f"{type(exc).__name__}: {exc}",
                        "sanitization_ok": False,
                    }
                )
        rows.append(row)
    return rows


def _fetch_search_run_status_payload(
    *,
    base_url: str,
    status_url: str,
    run_id: str,
    timeout_seconds: float,
    api_token: str = "",
    principal_id: str = "",
    edge_assertion_secret: str = "",
    edge_assertion_audience: str = "",
) -> dict[str, object]:
    raw_status_url = str(status_url or "").strip()
    if raw_status_url:
        url = urllib.parse.urljoin(base_url.rstrip("/") + "/", raw_status_url.lstrip("/"))
    else:
        url = urllib.parse.urljoin(base_url.rstrip("/") + "/", f"app/api/property/search-runs/{urllib.parse.quote(str(run_id or '').strip())}?lightweight=true")
    normalized_api_token = str(api_token or "").strip() or _default_api_token()
    normalized_principal_id = str(principal_id or "").strip() or _default_principal_id()
    request = urllib.request.Request(
        url,
        method="GET",
        headers=_request_headers(
            user_agent="PropertyQuarry-live-provider-search-status/1.0",
            principal_id=normalized_principal_id,
            api_token=normalized_api_token,
            method="GET",
            url=url,
            edge_assertion_secret=edge_assertion_secret,
            edge_assertion_audience=edge_assertion_audience,
        ),
    )
    with _wall_clock_timeout(timeout_seconds):
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(220_000)
    return json.loads(body.decode("utf-8", errors="replace"))


def _build_targeted_provider_search_matrix(
    *,
    countries: Iterable[str],
    base_url: str,
    enabled: bool,
    dry_run: bool,
    execute: bool,
    timeout_seconds: float,
    provider_keys: Iterable[str] = (),
    max_providers: int = 0,
    search_executor: Callable[[dict[str, object], float], dict[str, object]] | None = None,
    status_fetcher: Callable[[str, str, float], dict[str, object]] | None = None,
    checkpoint_writer: Callable[[list[dict[str, object]]], None] | None = None,
    resume_rows: Iterable[dict[str, object]] = (),
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    provider_rows_by_country = _select_targeted_provider_options(
        countries=countries,
        provider_keys=provider_keys,
        max_providers=max_providers,
    )
    executor = search_executor or (lambda payload, timeout: _post_search_run_payload(base_url=base_url, payload=payload, timeout_seconds=timeout))
    status_reader = status_fetcher or (
        lambda run_id, status_url, timeout: _fetch_search_run_status_payload(
            base_url=base_url,
            run_id=run_id,
            status_url=status_url,
            timeout_seconds=timeout,
        )
    )
    resumed_by_key = {
        (
            str(row.get("country_code") or "").strip().upper(),
            str(row.get("provider") or "").strip(),
            str(row.get("mode") or "").strip(),
        ): dict(row)
        for row in resume_rows
        if str(row.get("status") or "").strip().lower() == "pass"
    }
    for country in countries:
        normalized_country = str(country or "").strip().upper()
        for option in list(provider_rows_by_country.get(normalized_country) or []):
            provider_key = str(option.get("value") or "").strip()
            provider_country = str(option.get("country_code") or "").strip().upper()
            if not provider_key:
                continue
            listing_mode = _listing_mode_for_provider(normalized_country, provider_key)
            selectable_for_mode = provider_key in _selectable_provider_keys_for_mode(normalized_country, listing_mode)
            for mode in ("targeted_no_soft_filters", "targeted_soft_filters"):
                payload = _targeted_search_payload(
                    country_code=normalized_country,
                    provider_key=provider_key,
                    mode=mode,
                    listing_mode=listing_mode,
                )
                preferences = dict(payload.get("property_preferences") or {})
                target_context_country_scope_ok = _target_context_country_scope_ok(
                    normalized_country,
                    preferences.get("location_query"),
                    preferences.get("selected_location_values"),
                )
                soft_filter_fields = sorted(
                    key
                    for key in preferences
                    if key.startswith("max_distance_to_") or key in {"keyword_preferences", "keyword_preferences_json", "min_area_m2", "max_price_eur"}
                )
                row: dict[str, object] = {
                    "country_code": normalized_country,
                    "provider": provider_key,
                    "provider_country_code": provider_country,
                    "provider_label": str(option.get("label") or provider_key).strip(),
                    "provider_family": str(option.get("family") or "").strip(),
                    "listing_mode": listing_mode,
                    "mode": mode,
                    "endpoint": "/app/api/property/search-runs",
                    "selected_platforms": [provider_key],
                    "location_query": str(preferences.get("location_query") or "").strip(),
                    "selected_location_values": list(preferences.get("selected_location_values") or []),
                    "search_mode": str(preferences.get("search_mode") or "").strip(),
                    "soft_filter_fields": soft_filter_fields,
                    "soft_filters_present": bool(soft_filter_fields),
                    "agent_unlimited_results": "max_results_per_source" not in payload and "max_results_per_source" not in preferences,
                    "target_context_country_scope_ok": target_context_country_scope_ok,
                    "payload_contract_ok": (
                        payload.get("selected_platforms") == [provider_key]
                        and provider_country == normalized_country
                        and preferences.get("country_code") == normalized_country
                        and preferences.get("listing_mode") == listing_mode
                        and selectable_for_mode
                        and preferences.get("property_commercial") == _agent_unlimited_commercial_state()
                        and target_context_country_scope_ok
                        and (bool(soft_filter_fields) if mode == "targeted_soft_filters" else not bool(soft_filter_fields))
                    ),
                    "status": "skipped" if not enabled else "dry_run" if dry_run else "planned",
                }
                resumed_row = resumed_by_key.get((normalized_country, provider_key, mode))
                if enabled and not dry_run and execute and resumed_row is not None:
                    row.update(
                        {
                            key: value
                            for key, value in resumed_row.items()
                            if key
                            in {
                                "status",
                                "run_id",
                                "status_url",
                                "runtime_status",
                                "status_probe_ok",
                                "status_probe_status",
                                "status_probe_candidate_count",
                            }
                        }
                    )
                    row["resumed_from_checkpoint"] = True
                    rows.append(row)
                    if checkpoint_writer is not None:
                        checkpoint_writer([dict(item) for item in rows])
                    continue
                if enabled and not dry_run and execute:
                    try:
                        response = dict(executor(payload, timeout_seconds) or {})
                        run_id = str(response.get("run_id") or "").strip()
                        status_url = str(response.get("status_url") or "").strip()
                        response_status = str(response.get("status") or "").strip().lower()
                        accepted = bool(run_id and status_url and response_status in {"queued", "in_progress", "processed", "completed", "completed_partial"})
                        status_payload: dict[str, object] = {}
                        status_probe_ok = False
                        status_probe_status = ""
                        if accepted:
                            status_payload = dict(status_reader(run_id, status_url, timeout_seconds) or {})
                            status_probe_run_id = str(status_payload.get("run_id") or "").strip()
                            status_probe_status = str(status_payload.get("status") or "").strip().lower()
                            status_probe_ok = (
                                status_probe_run_id == run_id
                                and status_probe_status in {"queued", "in_progress", "processed", "completed", "completed_partial", "failed"}
                            )
                        row.update(
                            {
                                "status": "pass" if accepted and status_probe_ok and row["payload_contract_ok"] else "fail",
                                "run_id": run_id,
                                "status_url": status_url,
                                "runtime_status": response_status,
                                "status_probe_ok": status_probe_ok,
                                "status_probe_status": status_probe_status,
                                "status_probe_candidate_count": int(status_payload.get("candidate_count") or 0) if status_payload else 0,
                            }
                        )
                    except Exception as exc:
                        row.update(
                            {
                                "status": "fail",
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                rows.append(row)
                if checkpoint_writer is not None:
                    checkpoint_writer([dict(item) for item in rows])
    return rows


def _targeted_search_matrix_summary(
    rows: list[dict[str, object]],
    *,
    countries: Iterable[str],
    execute_requested: bool,
    enabled: bool,
    dry_run: bool,
    provider_keys: Iterable[str] = (),
    max_providers: int = 0,
) -> dict[str, object]:
    normalized_countries = tuple(dict.fromkeys(str(country or "").strip().upper() for country in countries if str(country or "").strip()))
    selected_provider_scope = _select_targeted_provider_options(
        countries=normalized_countries,
        provider_keys=provider_keys,
        max_providers=max_providers,
    )
    full_provider_scope = {
        country: _search_ready_provider_options(country)
        for country in normalized_countries
    }
    selected_provider_keys_by_country = {
        country: {
            str(row.get("value") or "").strip()
            for row in list(selected_provider_scope.get(country) or [])
            if str(row.get("value") or "").strip()
        }
        for country in normalized_countries
    }
    full_provider_keys_by_country = {
        country: {
            str(row.get("value") or "").strip()
            for row in list(full_provider_scope.get(country) or [])
            if str(row.get("value") or "").strip()
        }
        for country in normalized_countries
    }
    status_counts: dict[str, int] = {}
    providers_by_country: dict[str, set[str]] = {country: set() for country in normalized_countries}
    modes_by_provider: dict[tuple[str, str], set[str]] = {}
    passed_modes_by_provider: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        status = str(row.get("status") or "").strip().lower() or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        country = str(row.get("country_code") or "").strip().upper()
        provider = str(row.get("provider") or "").strip()
        mode = str(row.get("mode") or "").strip()
        if country and provider:
            providers_by_country.setdefault(country, set()).add(provider)
            modes_by_provider.setdefault((country, provider), set()).add(mode)
            if status == "pass":
                passed_modes_by_provider.setdefault((country, provider), set()).add(mode)
    expected_modes = {"targeted_no_soft_filters", "targeted_soft_filters"}
    missing_mode_pairs = [
        {
            "country_code": country,
            "provider": provider,
            "missing_modes": sorted(expected_modes - modes),
        }
        for (country, provider), modes in sorted(modes_by_provider.items())
        if modes != expected_modes
    ]
    missing_passed_mode_pairs = [
        {
            "country_code": country,
            "provider": provider,
            "missing_passed_modes": sorted(expected_modes - passed_modes_by_provider.get((country, provider), set())),
        }
        for (country, provider), modes in sorted(modes_by_provider.items())
        if modes == expected_modes and passed_modes_by_provider.get((country, provider), set()) != expected_modes
    ]
    strict_rows = [row for row in rows if str(row.get("mode") or "") == "targeted_no_soft_filters"]
    soft_rows = [row for row in rows if str(row.get("mode") or "") == "targeted_soft_filters"]
    executed = bool(execute_requested and enabled and not dry_run)
    provider_scope_filtered = any(
        selected_provider_keys_by_country.get(country, set()) != full_provider_keys_by_country.get(country, set())
        for country in normalized_countries
    )
    reported_missing_passed_mode_pairs = missing_passed_mode_pairs if executed else []
    executed_rows = [
        row
        for row in rows
        if str(row.get("status") or "").strip().lower() in {"pass", "fail"}
    ] if executed else []
    resumed_rows = [row for row in executed_rows if bool(row.get("resumed_from_checkpoint"))]
    accepted_dispatch_rows = [
        row
        for row in executed_rows
        if str(row.get("run_id") or "").strip()
        and str(row.get("status_url") or "").strip()
        and str(row.get("runtime_status") or "").strip().lower()
        in {"queued", "in_progress", "processed", "completed", "completed_partial"}
    ]
    status_readback_ok_rows = [row for row in executed_rows if bool(row.get("status_probe_ok"))]
    failed_cases = [
        {
            "country_code": str(row.get("country_code") or "").strip().upper(),
            "provider": str(row.get("provider") or "").strip(),
            "provider_country_code": str(row.get("provider_country_code") or "").strip().upper(),
            "mode": str(row.get("mode") or "").strip(),
            "status": str(row.get("status") or "").strip().lower(),
            "runtime_status": str(row.get("runtime_status") or "").strip().lower(),
            "status_probe_status": str(row.get("status_probe_status") or "").strip().lower(),
            "status_probe_ok": bool(row.get("status_probe_ok")),
            "payload_contract_ok": bool(row.get("payload_contract_ok")),
            "target_context_country_scope_ok": bool(row.get("target_context_country_scope_ok")),
            **({"error": str(row.get("error") or "").strip()} if str(row.get("error") or "").strip() else {}),
        }
        for row in rows
        if str(row.get("status") or "").strip().lower() == "fail"
    ][:25]
    return {
        "country_codes": list(normalized_countries),
        "case_count": len(rows),
        "provider_scope_filtered": provider_scope_filtered,
        "selected_provider_keys": list(
            dict.fromkeys(str(provider_key or "").strip() for provider_key in provider_keys if str(provider_key or "").strip())
        ),
        "max_providers": max(0, int(max_providers or 0)),
        "provider_count_by_country": {
            country: len(providers_by_country.get(country, set()))
            for country in normalized_countries
        },
        "selected_provider_scope_count_by_country": {
            country: len(selected_provider_keys_by_country.get(country, set()))
            for country in normalized_countries
        },
        "full_search_ready_provider_count_by_country": {
            country: len(full_provider_keys_by_country.get(country, set()))
            for country in normalized_countries
        },
        "strict_case_count": len(strict_rows),
        "soft_filter_case_count": len(soft_rows),
        "status_counts": dict(sorted(status_counts.items())),
        "execution_requested": bool(execute_requested),
        "executed": executed,
        "executed_case_count": len(executed_rows),
        "resumed_case_count": len(resumed_rows),
        "passed_case_count": status_counts.get("pass", 0),
        "failed_case_count": status_counts.get("fail", 0),
        "failed_cases": failed_cases,
        "failed_case_sample_count": len(failed_cases),
        "failed_case_sample_limit": 25,
        "planned_case_count": status_counts.get("planned", 0),
        "dry_run_case_count": status_counts.get("dry_run", 0),
        "skipped_case_count": status_counts.get("skipped", 0),
        "dispatch_accepted_count": len(accepted_dispatch_rows),
        "dispatch_acceptance_complete": (not executed) or len(accepted_dispatch_rows) == len(rows),
        "status_readback_required": executed,
        "status_readback_case_count": len(executed_rows),
        "status_readback_ok_count": len(status_readback_ok_rows),
        "status_readback_complete": (not executed) or len(status_readback_ok_rows) == len(rows),
        "all_search_ready_providers_covered": (
            not provider_scope_filtered
            and all(
                providers_by_country.get(country, set()) == full_provider_keys_by_country.get(country, set())
                for country in normalized_countries
            )
            and not missing_mode_pairs
        ),
        "selected_provider_scope_covered": all(
            providers_by_country.get(country, set()) == selected_provider_keys_by_country.get(country, set())
            for country in normalized_countries
        ) and not missing_mode_pairs,
        "missing_mode_pairs": missing_mode_pairs,
        "all_search_ready_provider_modes_passed": (not executed) or not reported_missing_passed_mode_pairs,
        "missing_passed_mode_pairs": reported_missing_passed_mode_pairs[:25],
        "missing_passed_mode_pair_count": len(reported_missing_passed_mode_pairs),
        "missing_passed_mode_pair_sample_limit": 25,
        "payload_contracts_ok": all(bool(row.get("payload_contract_ok")) for row in rows) if rows else True,
        "provider_country_scope_ok": all(
            str(row.get("provider_country_code") or "").strip().upper() == str(row.get("country_code") or "").strip().upper()
            for row in rows
        ) if rows else True,
        "target_context_country_scope_ok": all(bool(row.get("target_context_country_scope_ok")) for row in rows) if rows else True,
        "agent_unlimited_results_ok": all(bool(row.get("agent_unlimited_results")) for row in rows) if rows else True,
        "strict_without_soft_filters_ok": all(not bool(row.get("soft_filters_present")) for row in strict_rows) if strict_rows else True,
        "soft_filters_present_ok": all(bool(row.get("soft_filters_present")) for row in soft_rows) if soft_rows else True,
    }


def _fetch_provider_payload(
    *,
    base_url: str,
    country_code: str,
    timeout_seconds: float,
    api_token: str = "",
    principal_id: str = "",
    release_probe_secret: str = "",
    edge_assertion_secret: str = "",
    edge_assertion_audience: str = "",
) -> dict[str, object]:
    params = urllib.parse.urlencode({"country": country_code})
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", f"app/api/property/providers?{params}")
    normalized_release_probe_secret = str(release_probe_secret or "").strip()
    normalized_api_token = (
        ""
        if normalized_release_probe_secret
        else str(api_token or "").strip() or _default_api_token()
    )
    normalized_principal_id = str(principal_id or "").strip() or _default_principal_id()
    headers = _request_headers(
        user_agent="PropertyQuarry-live-provider-smoke/1.0",
        principal_id=normalized_principal_id,
        api_token=normalized_api_token,
        method="GET",
        url=url,
        edge_assertion_secret=edge_assertion_secret,
        edge_assertion_audience=edge_assertion_audience,
    )
    if normalized_release_probe_secret:
        headers = live_probe_request_headers(
            url=url,
            authorized_origin=validated_live_base_origin(base_url),
            headers=headers,
            release_probe_secret=normalized_release_probe_secret,
            method="GET",
        )
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(220_000)
    except urllib.error.HTTPError as exc:
        exc.read(220_000)
        auth_suffix = ""
        if int(exc.code or 0) == 401:
            auth_suffix = (
                ": release probe missing or rejected"
                if normalized_release_probe_secret
                else ": api token missing or rejected"
                if not normalized_api_token
                else ""
            )
        raise RuntimeError(f"provider_catalog_http_{int(exc.code or 0)}{auth_suffix}") from exc
    return json.loads(body.decode("utf-8", errors="replace"))


def build_live_provider_smoke_receipt(
    *,
    countries: Iterable[str] = ("AT", "CR"),
    base_url: str = "http://localhost:8097",
    timeout_seconds: float = 8.0,
    search_run_timeout_seconds: float | None = None,
    provider_keys: Iterable[str] = (),
    max_providers: int = 0,
    fetcher: Callable[[str, float], dict[str, object]] | None = None,
    execute_search_matrix: bool | None = None,
    execute_cross_country_sanitization: bool = True,
    all_search_ready_countries: bool | None = None,
    search_executor: Callable[[dict[str, object], float], dict[str, object]] | None = None,
    status_fetcher: Callable[[str, str, float], dict[str, object]] | None = None,
    checkpoint_path: str | Path = "",
    resume_checkpoint_path: str | Path = "",
    api_token: str = "",
    principal_id: str = "",
    release_probe_secret: str = "",
    edge_assertion_secret: str = "",
    edge_assertion_audience: str = "",
) -> dict[str, object]:
    requested_countries = tuple(dict.fromkeys(str(country or "").strip().upper() for country in countries if str(country or "").strip()))
    all_search_ready_scope = _all_search_ready_countries_enabled() if all_search_ready_countries is None else bool(all_search_ready_countries)
    normalized_countries = _all_search_ready_country_codes() if all_search_ready_scope else requested_countries
    if not normalized_countries:
        normalized_countries = ("AT", "CR")
    enabled = _enabled()
    dry_run = _dry_run()
    normalized_release_probe_secret = str(release_probe_secret or "").strip()
    normalized_api_token = (
        ""
        if normalized_release_probe_secret
        else str(api_token or "").strip() or _default_api_token()
    )
    normalized_principal_id = str(principal_id or "").strip() or _default_principal_id()
    normalized_edge_assertion_secret = str(edge_assertion_secret or "").strip()
    normalized_edge_assertion_audience = str(edge_assertion_audience or "").strip()
    if bool(normalized_edge_assertion_secret) != bool(normalized_edge_assertion_audience):
        raise ValueError("edge_assertion_configuration_incomplete")
    if normalized_release_probe_secret and normalized_edge_assertion_secret:
        raise ValueError("release_probe_and_edge_assertion_modes_conflict")
    should_execute_search_matrix = _execute_search_matrix_enabled() if execute_search_matrix is None else bool(execute_search_matrix)
    if normalized_release_probe_secret and (
        should_execute_search_matrix or execute_cross_country_sanitization
    ):
        raise ValueError(
            "release_probe_mode_is_read_only; disable search-matrix execution and cross-country sanitization"
        )
    effective_search_run_timeout_seconds = _effective_search_run_timeout_seconds(
        timeout_seconds,
        enabled=enabled,
        dry_run=dry_run,
        search_run_timeout_seconds=search_run_timeout_seconds,
    )
    checks: list[dict[str, object]] = []
    effective_fetcher = fetcher or (
        lambda country, timeout: _fetch_provider_payload(
            base_url=base_url,
            country_code=country,
            timeout_seconds=timeout,
            api_token=normalized_api_token,
            principal_id=normalized_principal_id,
            release_probe_secret=normalized_release_probe_secret,
            edge_assertion_secret=normalized_edge_assertion_secret,
            edge_assertion_audience=normalized_edge_assertion_audience,
        )
    )
    for country in normalized_countries:
        options = provider_options(country_code=country)
        expected_provider_values = {
            str(option.get("value") or "").strip()
            for option in options
            if str(option.get("value") or "").strip()
        }
        defaults = set(default_platforms_for_country_listing_mode(country, "rent"))
        row = {
            "country_code": country,
            "status": "skipped" if not enabled else "dry_run" if dry_run else "ready_for_live_probe",
            "provider_count": len(options),
            "default_provider_count": len(defaults),
            "default_providers_present": sorted(
                str(option.get("value") or "")
                for option in options
                if str(option.get("value") or "") in defaults
            ),
            "requires_filter_pushdown_receipt": True,
            "requires_floorplan_receipt": True,
            "requires_location_boundary_receipt": True,
        }
        if enabled and not dry_run:
            try:
                payload = dict(effective_fetcher(country, timeout_seconds) or {})
                providers = [dict(item) for item in list(payload.get("providers") or []) if isinstance(item, dict)]
                runtime_provider_values = {
                    str(item.get("value") or "").strip()
                    for item in providers
                    if str(item.get("value") or "").strip()
                }
                runtime_provider_country_mismatches = sorted(
                    {
                        str(item.get("value") or "").strip()
                        for item in providers
                        if str(item.get("value") or "").strip()
                        and str(item.get("country_code") or "").strip().upper()
                        and str(item.get("country_code") or "").strip().upper() != country
                    }
                )
                runtime_defaults = {
                    str(item or "").strip()
                    for item in list(payload.get("default_platforms") or [])
                    if str(item or "").strip()
                }
                runtime_provider_count_ok = len(runtime_provider_values) == len(options)
                runtime_provider_set_ok = runtime_provider_values == expected_provider_values
                runtime_missing_providers = sorted(expected_provider_values - runtime_provider_values)
                runtime_unexpected_providers = sorted(runtime_provider_values - expected_provider_values)
                runtime_defaults_present_ok = runtime_defaults == defaults
                runtime_country_code = str(payload.get("country_code") or "").strip().upper()
                runtime_listing_mode = str(payload.get("listing_mode") or "").strip().lower()
                runtime_property_type = str(payload.get("property_type") or "").strip().lower()
                expected_runtime_defaults = set(default_platforms_for_country_listing_mode(country, runtime_listing_mode or "rent"))
                runtime_contract_ok = (
                    runtime_provider_count_ok
                    and runtime_provider_set_ok
                    and runtime_defaults == expected_runtime_defaults
                    and not runtime_provider_country_mismatches
                    and runtime_country_code == country
                    and runtime_listing_mode in {"rent", "buy"}
                    and bool(runtime_property_type)
                )
                runtime_defaults_present_ok = runtime_defaults == expected_runtime_defaults
                row.update(
                    {
                        "status": "pass" if runtime_contract_ok else "fail",
                        "runtime_provider_count": len(runtime_provider_values),
                        "runtime_provider_set_ok": runtime_provider_set_ok,
                        "runtime_missing_providers": runtime_missing_providers,
                        "runtime_unexpected_providers": runtime_unexpected_providers,
                        "runtime_default_provider_count": len(runtime_defaults),
                        "runtime_default_providers_present": sorted(value for value in runtime_defaults if value in runtime_provider_values),
                        "runtime_country_code": runtime_country_code,
                        "runtime_listing_mode": runtime_listing_mode,
                        "runtime_property_type": runtime_property_type,
                        "runtime_provider_count_ok": runtime_provider_count_ok,
                        "runtime_defaults_present_ok": runtime_defaults_present_ok,
                        "runtime_provider_country_scope_ok": not runtime_provider_country_mismatches,
                        "runtime_provider_country_mismatches": runtime_provider_country_mismatches,
                    }
                )
            except Exception as exc:
                row.update(
                    {
                        "status": "fail",
                        "error": f"{type(exc).__name__}: {exc}",
                        "runtime_provider_count_ok": False,
                        "runtime_defaults_present_ok": False,
                        "runtime_provider_country_scope_ok": False,
                    }
                )
        checks.append(row)
    checkpoint_target = Path(checkpoint_path) if str(checkpoint_path or "").strip() else None
    resume_rows: list[dict[str, object]] = []
    resume_source = ""
    resume_target = Path(resume_checkpoint_path) if str(resume_checkpoint_path or "").strip() else None
    if resume_target is not None and resume_target.exists():
        try:
            resume_payload = json.loads(resume_target.read_text(encoding="utf-8"))
            resume_rows = [
                dict(row)
                for row in list(resume_payload.get("targeted_search_matrix") or [])
                if isinstance(row, dict)
            ]
            resume_source = str(resume_target)
        except Exception:
            resume_rows = []
            resume_source = ""

    def _write_checkpoint(rows: list[dict[str, object]]) -> None:
        if checkpoint_target is None:
            return
        summary = _targeted_search_matrix_summary(
            rows,
            countries=normalized_countries,
            execute_requested=should_execute_search_matrix,
            enabled=enabled,
            dry_run=dry_run,
            provider_keys=provider_keys,
            max_providers=max_providers,
        )
        partial_receipt = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "enabled": enabled,
            "dry_run": dry_run,
            "base_url": base_url,
            "resume_source": resume_source,
            "checks": checks,
            "targeted_search_matrix": rows,
            "targeted_search_matrix_summary": summary,
            "targeted_search_matrix_count": len(rows),
            "targeted_search_matrix_status": "running",
            "targeted_search_matrix_executed": should_execute_search_matrix and enabled and not dry_run,
            "provider_catalog_timeout_seconds": timeout_seconds,
            "search_run_timeout_seconds": effective_search_run_timeout_seconds,
            "checkpoint": True,
            "complete": False,
            "notes": [
                "Checkpoint receipt written after a targeted provider search row completed.",
                "A final complete receipt overwrites this file when the run finishes.",
            ],
        }
        checkpoint_target.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_target.write_text(json.dumps(partial_receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    search_matrix = _build_targeted_provider_search_matrix(
        countries=normalized_countries,
        base_url=base_url,
        enabled=enabled,
        dry_run=dry_run,
        execute=should_execute_search_matrix,
        timeout_seconds=effective_search_run_timeout_seconds,
        provider_keys=provider_keys,
        max_providers=max_providers,
        search_executor=search_executor
        or (
            lambda payload, timeout: _post_search_run_payload(
                base_url=base_url,
                payload=payload,
                timeout_seconds=timeout,
                api_token=normalized_api_token,
                principal_id=normalized_principal_id,
                edge_assertion_secret=normalized_edge_assertion_secret,
                edge_assertion_audience=normalized_edge_assertion_audience,
            )
        ),
        status_fetcher=status_fetcher
        or (
            lambda run_id, status_url, timeout: _fetch_search_run_status_payload(
                base_url=base_url,
                run_id=run_id,
                status_url=status_url,
                timeout_seconds=timeout,
                api_token=normalized_api_token,
                principal_id=normalized_principal_id,
                edge_assertion_secret=normalized_edge_assertion_secret,
                edge_assertion_audience=normalized_edge_assertion_audience,
            )
        ),
        checkpoint_writer=_write_checkpoint if should_execute_search_matrix and enabled and not dry_run else None,
        resume_rows=resume_rows if should_execute_search_matrix and enabled and not dry_run else (),
    )
    cross_country_sanitization_checks = _build_cross_country_sanitization_checks(
        countries=normalized_countries,
        base_url=base_url,
        enabled=enabled and execute_cross_country_sanitization,
        dry_run=dry_run,
        timeout_seconds=effective_search_run_timeout_seconds,
        search_executor=search_executor
        or (
            lambda payload, timeout: _post_search_run_payload(
                base_url=base_url,
                payload=payload,
                timeout_seconds=timeout,
                api_token=normalized_api_token,
                principal_id=normalized_principal_id,
                edge_assertion_secret=normalized_edge_assertion_secret,
                edge_assertion_audience=normalized_edge_assertion_audience,
            )
        ),
    )
    search_matrix_summary = _targeted_search_matrix_summary(
        search_matrix,
        countries=normalized_countries,
        execute_requested=should_execute_search_matrix,
        enabled=enabled,
        dry_run=dry_run,
        provider_keys=provider_keys,
        max_providers=max_providers,
    )
    statuses = {str(row.get("status") or "").strip().lower() for row in checks}
    search_statuses = {str(row.get("status") or "").strip().lower() for row in search_matrix}
    sanitization_statuses = {str(row.get("status") or "").strip().lower() for row in cross_country_sanitization_checks}
    sanitization_ok = all(bool(row.get("sanitization_ok", True)) for row in cross_country_sanitization_checks)
    if "fail" in statuses or "fail" in search_statuses or "fail" in sanitization_statuses or not sanitization_ok:
        status = "fail"
    elif enabled and not dry_run and statuses == {"pass"} and search_statuses == {"pass"} and sanitization_statuses == {"pass"}:
        status = "pass"
    elif enabled and not dry_run and statuses == {"pass"} and search_statuses == {"planned"}:
        status = "blocked_targeted_search_matrix_not_executed"
    elif not enabled:
        status = "skipped"
    elif dry_run:
        status = "dry_run"
    else:
        status = "ready_for_live_probe"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "enabled": enabled,
        "dry_run": dry_run,
        "base_url": base_url,
        "provider_catalog_timeout_seconds": timeout_seconds,
        "search_run_timeout_seconds": effective_search_run_timeout_seconds,
        "api_token_configured": bool(normalized_api_token),
        "edge_assertion_configured": bool(normalized_edge_assertion_secret),
        "resume_source": resume_source,
        "country_scope": "all_search_ready" if all_search_ready_scope else "explicit",
        "checks": checks,
        "targeted_search_matrix": search_matrix,
        "targeted_search_matrix_summary": search_matrix_summary,
        "targeted_search_matrix_count": len(search_matrix),
        "cross_country_sanitization_executed": bool(
            execute_cross_country_sanitization and enabled and not dry_run
        ),
        "cross_country_sanitization_checks": cross_country_sanitization_checks,
        "cross_country_sanitization_summary": {
            "case_count": len(cross_country_sanitization_checks),
            "status_counts": {
                status_key: sum(1 for row in cross_country_sanitization_checks if str(row.get("status") or "").strip().lower() == status_key)
                for status_key in sorted(sanitization_statuses)
            },
            "sanitization_ok": sanitization_ok,
        },
        "targeted_search_matrix_status": (
            "fail"
            if "fail" in search_statuses
            else "pass"
            if search_statuses == {"pass"}
            else "skipped"
            if search_statuses == {"skipped"}
            else "dry_run"
            if search_statuses == {"dry_run"}
            else "planned"
        ),
        "targeted_search_matrix_executed": should_execute_search_matrix and enabled and not dry_run,
        "checkpoint": False,
        "complete": True,
        "notes": [
            "Live crawling is disabled unless PROPERTYQUARRY_LIVE_PROVIDER_SMOKE=1.",
            "Dry-run mode proves provider catalog, default provider, floorplan, and filter-pushdown contracts without crawling.",
            "Live mode probes the runtime provider catalog endpoint and checks provider/default-provider parity.",
            "The targeted search matrix covers every search-ready provider with one strict no-soft-filter payload and one discovery soft-filter payload.",
            "Use --provider and --max-providers to stage smaller real E2E slices while the receipt still reports whether the full search-ready catalog was covered.",
            "When the targeted matrix is executed, each accepted dispatch must also return a readable search-run status receipt.",
            "The cross-country sanitization probe deliberately submits a foreign provider with a local provider and expects the runtime to remove the foreign provider before dispatch.",
            "Use --no-cross-country-sanitization for a read-only runtime catalog probe that submits no search runs.",
            "Use --resume-from with a checkpoint/final receipt to reuse passed targeted search rows and rerun only missing or failed cases.",
            "Set PROPERTYQUARRY_LIVE_PROVIDER_ALL_SEARCH_READY_COUNTRIES=1 or pass --all-search-ready-countries to expand the matrix to every customer-search country with search-ready providers.",
            "Set PROPERTYQUARRY_LIVE_PROVIDER_SEARCH_E2E=1 with live mode to execute the targeted search matrix against /app/api/property/search-runs.",
            "Search-run dispatch and status-readback probes use a longer live timeout budget than provider-catalog reads because runtime acceptance can be slower than catalog rendering.",
        ],
    }


def main() -> int:
    if len(os.sys.argv) > 1 and os.sys.argv[1] in {"--help", "-h"}:
        print(
            "Usage:\n"
            "  python3 scripts/property_live_provider_smoke.py [--base-url <url>] [--country <code>]...\n\n"
            "Builds the PropertyQuarry provider smoke receipt in skipped, dry-run, or live runtime mode."
        )
        return 0
    parser = argparse.ArgumentParser(description="PropertyQuarry live provider smoke receipt.")
    parser.add_argument("--country", action="append", default=[], help="Country code to include. Defaults to AT and CR.")
    parser.add_argument(
        "--all-search-ready-countries",
        action="store_true",
        help="Include every country that has at least one search-ready provider. Ignored when --country is also supplied.",
    )
    parser.add_argument("--write", default="", help="Optional JSON receipt output path.")
    parser.add_argument("--resume-from", default="", help="Optional checkpoint/final receipt whose passed targeted search rows should be reused.")
    parser.add_argument("--base-url", default=os.getenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_BASE_URL") or "http://localhost:8097")
    parser.add_argument("--api-token", default=_default_api_token(), help="Optional API token. Defaults to PROPERTYQUARRY_LIVE_API_TOKEN or EA_API_TOKEN from env/.env.")
    parser.add_argument(
        "--release-probe-secret-stdin",
        action="store_true",
        help="Read the protected read-only release-probe credential once from bounded stdin.",
    )
    parser.add_argument(
        "--edge-assertion-secret-stdin",
        action="store_true",
        help="Read the protected principal-assertion HMAC secret once from bounded stdin.",
    )
    parser.add_argument(
        "--edge-assertion-audience",
        default="",
        help="Trusted edge principal-assertion audience (non-secret).",
    )
    parser.add_argument("--principal-id", default=_default_principal_id(), help="Optional principal id for authenticated live probes.")
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument(
        "--search-run-timeout-seconds",
        type=float,
        default=None,
        help="Optional timeout for search-run dispatch and status readback. Defaults to max(--timeout-seconds, PROPERTYQUARRY_LIVE_PROVIDER_SEARCH_RUN_TIMEOUT_SECONDS / PROPERTYQUARRY_DEPLOY_PROVIDER_SEARCH_RUN_TIMEOUT_SECONDS / 60) in live mode.",
    )
    parser.add_argument("--provider", action="append", default=[], help="Provider key to include in the targeted matrix. Repeatable.")
    parser.add_argument("--max-providers", type=int, default=0, help="Optional global cap for how many search-ready providers to include in the targeted matrix scope.")
    matrix_group = parser.add_mutually_exclusive_group()
    matrix_group.add_argument(
        "--execute-search-matrix",
        action="store_true",
        help="Execute every targeted provider search case. Requires live mode and non-dry-run mode.",
    )
    matrix_group.add_argument(
        "--no-execute-search-matrix",
        action="store_true",
        help="Do not execute targeted provider search cases, regardless of environment.",
    )
    parser.add_argument(
        "--no-cross-country-sanitization",
        action="store_true",
        help="Probe runtime provider catalogs without submitting cross-country search-run sanitization cases.",
    )
    args = parser.parse_args()
    if args.release_probe_secret_stdin and args.edge_assertion_secret_stdin:
        parser.error("release-probe and edge-assertion stdin modes are mutually exclusive")
    release_probe_secret = read_release_probe_secret_from_stdin(
        parser,
        enabled=bool(args.release_probe_secret_stdin),
    )
    edge_assertion_secret = read_edge_assertion_secret_from_stdin(
        parser,
        enabled=bool(args.edge_assertion_secret_stdin),
    )
    scrub_release_probe_secret_environment()
    scrub_edge_assertion_secret_environment()
    if bool(edge_assertion_secret) != bool(str(args.edge_assertion_audience or "").strip()):
        parser.error(
            "--edge-assertion-secret-stdin and --edge-assertion-audience must be supplied together"
        )
    if release_probe_secret and not (
        args.no_execute_search_matrix and args.no_cross_country_sanitization
    ):
        parser.error(
            "--release-probe-secret-stdin requires --no-execute-search-matrix "
            "and --no-cross-country-sanitization"
        )
    execute_search_matrix = None
    if args.execute_search_matrix:
        execute_search_matrix = True
    elif args.no_execute_search_matrix:
        execute_search_matrix = False
    try:
        if args.all_search_ready_countries and not args.country:
            os.environ["PROPERTYQUARRY_LIVE_PROVIDER_ALL_SEARCH_READY_COUNTRIES"] = "1"
        receipt = build_live_provider_smoke_receipt(
            countries=tuple(args.country or (() if args.all_search_ready_countries else CUSTOMER_SEARCH_COUNTRY_ORDER)),
            base_url=str(args.base_url),
            timeout_seconds=float(args.timeout_seconds),
            search_run_timeout_seconds=(
                None if args.search_run_timeout_seconds is None else float(args.search_run_timeout_seconds)
            ),
            provider_keys=tuple(args.provider or []),
            max_providers=int(args.max_providers or 0),
            execute_search_matrix=execute_search_matrix,
            execute_cross_country_sanitization=not args.no_cross_country_sanitization,
            all_search_ready_countries=bool(args.all_search_ready_countries and not args.country),
            checkpoint_path=args.write,
            resume_checkpoint_path=args.resume_from,
            api_token=str(args.api_token or "").strip(),
            principal_id=str(args.principal_id or "").strip(),
            release_probe_secret=release_probe_secret,
            edge_assertion_secret=edge_assertion_secret,
            edge_assertion_audience=str(args.edge_assertion_audience or "").strip(),
        )
    except KeyboardInterrupt:
        if args.write and Path(args.write).exists():
            print(f"interrupted: checkpoint receipt retained at {args.write}", file=sys.stderr)
            return 130
        raise
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        write_path = Path(args.write)
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if str(receipt.get("status") or "").strip().lower() in {
        "pass",
        "dry_run",
        "skipped",
        "blocked_targeted_search_matrix_not_executed",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
