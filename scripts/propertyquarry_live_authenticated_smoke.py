#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from scripts.propertyquarry_billing_handoff_probe import (
    NoRedirectHandler as _NoRedirectHandler,
    PROPERTYQUARRY_BILLING_HANDOFF_ALLOWED_HOSTS,
    header_value as _header_value,
    https_handoff_url_usable as _shared_https_handoff_url_usable,
    https_redirect_host_resolves as _https_redirect_host_resolves,
    no_proxy_opener as _no_proxy_opener,
)
from scripts.propertyquarry_live_http_security import (
    normalized_origin,
    redact_secret_values,
    url_matches_origin,
    validated_live_base_origin,
)
from scripts.propertyquarry_live_probe_auth import live_probe_request_headers
from scripts.propertyquarry_live_probe_secret_scope import (
    read_release_probe_secret_from_stdin,
    scrub_release_probe_secret_environment,
)


DEFAULT_ROUTES = (
    "/app/account",
    "/app/billing",
    "/sign-in",
)
AUTHENTICATED_SHARED_FAST_RUN_PATH = "/app/shortlist/run/0a89ead9e0b048288cca22d1aac54fa7"
PUBLIC_SHORTLIST_ENTRYPOINT_RUN_IDS = {
    "0a89ead9e0b048288cca22d1aac54fa7",
    "public-demo-run",
    "shared-public-run",
}
DEFAULT_LIVE_ROUTES = (
    *DEFAULT_ROUTES,
    AUTHENTICATED_SHARED_FAST_RUN_PATH,
)
MAX_RESPONSE_BODY_BYTES = 900_000
SENSITIVE_URL_QUERY_KEYS = (
    "access_token",
    "code",
    "id_token",
    "login_token",
    "pq_bridge",
    "refresh_token",
    "state",
    "token",
)

FORBIDDEN_CUSTOMER_NOISE = (
    "billing truth",
    "plan and limits",
    "refresh delivery",
    "repair status checked",
    "what happened",
    "what still worked",
    "main blocker",
    "best next move",
)
BILLING_FAIL_CLOSED_MARKERS = (
    "billing portal unavailable",
    "propertyquarry access stays active",
)
BILLING_FAIL_CLOSED_STATE_MARKERS = (
    "billing portal is still being connected",
    "still opens another sign-in",
    "billing account host is not ready yet",
)
BILLING_LOCAL_BOARD_MARKERS = (
    "open pricing",
    "view plans",
    "compare plans",
    "plus checkout",
    "billing history",
    "current commercial state",
    "plan posture",
)

BILLING_BRIDGE_GUIDED_LOGIN_MARKERS = (
    "continue billing",
    "back to propertyquarry",
    "billing lane",
)


def _env_value(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _compact_snippet(text: str, *, limit: int = 180) -> str:
    return re.sub(r"\s+", " ", str(text or "")[:limit]).strip()


def _decode_body(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def _json_safe(value: object, *, secrets: tuple[str, ...] = ()) -> object:
    if isinstance(value, bytes):
        return redact_secret_values(
            _redact_sensitive_receipt_text(_decode_body(value)),
            secrets=secrets,
        )
    if isinstance(value, dict):
        return {str(key): _json_safe(item, secrets=secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        return redact_secret_values(_redact_sensitive_receipt_text(value), secrets=secrets)
    return value


def _redact_sensitive_receipt_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)(/login/token/)[^/?#\s\"'>]+",
        r"\1[redacted]",
        str(value or ""),
    )
    query_key_pattern = "|".join(re.escape(key) for key in SENSITIVE_URL_QUERY_KEYS)
    return re.sub(
        rf"(?i)([?&](?:{query_key_pattern})=)[^&#\s\"'>]+",
        r"\1[redacted]",
        redacted,
    )


def _visible_text(text: str) -> str:
    without_hidden = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    without_tags = re.sub(r"<[^>]+>", " ", without_hidden)
    return re.sub(r"\s+", " ", without_tags).strip()


def _https_handoff_url_usable(
    location: str,
    *,
    timeout_seconds: float = 8.0,
    _visited_urls: tuple[str, ...] = (),
) -> dict[str, object]:
    return _shared_https_handoff_url_usable(
        location,
        timeout_seconds=timeout_seconds,
        visited_urls=_visited_urls,
        allowed_hosts=PROPERTYQUARRY_BILLING_HANDOFF_ALLOWED_HOSTS,
    )


def _security_header_checks(*, headers: dict[str, object]) -> list[tuple[str, bool]]:
    csp = _header_value(headers, "Content-Security-Policy")
    permissions = _header_value(headers, "Permissions-Policy")
    return [
        ("security_csp", "default-src 'self'" in csp and "frame-ancestors 'self'" in csp),
        ("security_nosniff", _header_value(headers, "X-Content-Type-Options").lower() == "nosniff"),
        ("security_referrer_policy", _header_value(headers, "Referrer-Policy") == "strict-origin-when-cross-origin"),
        ("security_permissions_policy", "camera=()" in permissions and "microphone=()" in permissions),
    ]


def _is_internal_billing_account_fallback(location: str) -> bool:
    parsed = urllib.parse.urlparse(str(location or "").strip())
    return not parsed.scheme and (parsed.path or "").startswith("/app/account")


def _is_public_shortlist_entrypoint_path(location: str) -> bool:
    parsed = urllib.parse.urlparse(str(location or "").strip())
    parts = [part for part in str(parsed.path or "").split("/") if part]
    if len(parts) < 4 or parts[:3] != ["app", "shortlist", "run"]:
        return False
    return parts[3].strip().lower() in PUBLIC_SHORTLIST_ENTRYPOINT_RUN_IDS


def _is_non_public_shortlist_run_path(location: str) -> bool:
    parsed = urllib.parse.urlparse(str(location or "").strip())
    parts = [part for part in str(parsed.path or "").split("/") if part]
    if len(parts) < 4 or parts[:3] != ["app", "shortlist", "run"]:
        return False
    return parts[3].strip().lower() not in PUBLIC_SHORTLIST_ENTRYPOINT_RUN_IDS


def _resolve_billing_external_handoff(
    *,
    base_url: str,
    location: str,
    fetcher: Callable[[str, float], dict[str, object]],
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_location = str(location or "").strip()
    if not normalized_location:
        return {
            "external_location": "",
            "bridge_launch_used": False,
            "bridge_launch_url": "",
            "bridge_launch_status_code": 0,
            "bridge_launch_error": "",
        }
    bridge_launch_url = urllib.parse.urljoin(base_url.rstrip("/") + "/", normalized_location)
    parsed_bridge_launch = urllib.parse.urlparse(bridge_launch_url)
    if (
        (parsed_bridge_launch.path or "") != "/app/api/property/billing/bridge-launch"
        or not url_matches_origin(bridge_launch_url, validated_live_base_origin(base_url))
    ):
        return {
            "external_location": normalized_location,
            "bridge_launch_used": False,
            "bridge_launch_url": "",
            "bridge_launch_status_code": 0,
            "bridge_launch_error": "",
        }
    bridge_result = fetcher(bridge_launch_url, timeout_seconds)
    bridge_headers = dict(bridge_result.get("headers") or {})
    bridge_location = _header_value(bridge_headers, "Location")
    return {
        "external_location": str(bridge_location or "").strip(),
        "bridge_launch_used": True,
        "bridge_launch_url": bridge_launch_url,
        "bridge_launch_status_code": int(bridge_result.get("status_code") or 0),
        "bridge_launch_error": str(bridge_result.get("error") or "").strip(),
        "bridge_launch_result": bridge_result,
    }


def _billing_bridge_guided_login_assist_probe(location: str, *, timeout_seconds: float) -> dict[str, object]:
    normalized_location = str(location or "").strip()
    parsed = urllib.parse.urlparse(normalized_location)
    if parsed.scheme != "https" or not parsed.netloc or "/sso/propertyquarry" not in parsed.path:
        return {
            "ok": False,
            "status_code": 0,
            "final_url": normalized_location,
            "error": "bridge_login_assist_not_applicable",
        }
    cookie_jar = http.cookiejar.CookieJar()
    opener = _no_proxy_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    request = urllib.request.Request(
        normalized_location,
        headers={
            "User-Agent": "PropertyQuarry-live-authenticated-smoke/1.0",
            "Accept": "text/html,application/json,*/*",
        },
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status_code = int(response.status)
            final_url = str(response.geturl())
            body = _decode_body(response.read(MAX_RESPONSE_BODY_BYTES))
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        final_url = str(exc.geturl())
        body = _decode_body(exc.read(MAX_RESPONSE_BODY_BYTES))
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "final_url": normalized_location,
            "error": f"{type(exc).__name__}: {exc}",
        }
    visible_text = _visible_text(body).lower()
    has_guided_markers = all(marker in visible_text for marker in BILLING_BRIDGE_GUIDED_LOGIN_MARKERS)
    has_email_field = 'name="email"' in body.lower() or 'type="email"' in body.lower()
    ok = status_code == 200 and has_guided_markers and has_email_field
    return {
        "ok": ok,
        "status_code": status_code,
        "final_url": final_url,
        "has_guided_markers": has_guided_markers,
        "has_email_field": has_email_field,
        "error": "" if ok else "bridge_login_assist_missing",
    }


def fetch_url(
    url: str,
    *,
    timeout_seconds: float,
    api_token: str,
    principal_id: str,
    country_code: str,
    release_probe_secret: str = "",
    release_probe_configured_routes: tuple[str, ...] = (),
    authorized_origin: str = "",
) -> dict[str, object]:
    headers = {
        "User-Agent": "PropertyQuarry-live-authenticated-smoke/1.0",
        "Accept": "text/html,application/json,*/*",
        "Host": "propertyquarry.com",
        "X-EA-Principal-ID": principal_id,
        "cf-ipcountry": country_code,
    }
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
        headers["X-EA-API-Token"] = api_token
    headers = live_probe_request_headers(
        url=url,
        authorized_origin=(
            str(authorized_origin or "").strip()
            or (normalized_origin(url) if not release_probe_secret else "")
        ),
        headers=headers,
        release_probe_secret=release_probe_secret,
        method="GET",
        configured_routes=release_probe_configured_routes,
    )
    request = urllib.request.Request(url, headers=headers)
    opener = _no_proxy_opener(_NoRedirectHandler)
    started = datetime.now(timezone.utc)
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            ended = datetime.now(timezone.utc)
            return {
                "status_code": int(response.status),
                "final_url": str(response.geturl()),
                "headers": dict(response.headers.items()),
                "body": response.read(MAX_RESPONSE_BODY_BYTES),
                "duration_ms": int((ended - started).total_seconds() * 1000),
            }
    except urllib.error.HTTPError as exc:
        ended = datetime.now(timezone.utc)
        return {
            "status_code": int(exc.code),
            "final_url": str(exc.geturl()),
            "headers": dict(exc.headers.items()),
            "body": exc.read(MAX_RESPONSE_BODY_BYTES),
            "duration_ms": int((ended - started).total_seconds() * 1000),
        }
    except Exception as exc:
        ended = datetime.now(timezone.utc)
        return {
            "status_code": 0,
            "final_url": url,
            "headers": {},
            "body": b"",
            "duration_ms": int((ended - started).total_seconds() * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _route_checks(*, path: str, text: str, expected_plan_label: str) -> list[tuple[str, bool]]:
    visible_text = _visible_text(text)
    lowered_visible = visible_text.lower()
    checks: list[tuple[str, bool]] = []
    if path == "/app/account":
        account_logout_count = len(re.findall(r">Log out<", text))
        checks.extend(
            (
                ("account_heading", "<h2>Account</h2>" in text or ">Account<" in text),
                ("account_notifications", "<h2>Notifications</h2>" in text),
                ("account_notification_form", 'action="/app/api/property/account/notifications"' in text),
                ("account_notification_email_channel", 'name="notification_channels" value="email"' in text),
                ("account_notification_telegram_channel", 'name="notification_channels" value="telegram"' in text),
                ("account_notification_whatsapp_channel", 'name="notification_channels" value="whatsapp"' in text),
                ("account_notification_primary_route", 'name="preferred_channel"' in text),
                ("account_notification_whatsapp_phone", 'name="whatsapp_ai_support_phone"' in text),
                ("account_notification_save_action", "Save notifications" in visible_text),
                ("account_paid_plan", f"<h2>{expected_plan_label}</h2>" in text if expected_plan_label else True),
                (
                    "account_logout_strip",
                    "pqx-account-logout-strip" in text
                    and (
                        "Current session" in text
                        or "Signed-in session" in text
                        or "Session · Signed in" in visible_text
                        or "Signed in" in visible_text
                    ),
                ),
                ("account_single_logout", account_logout_count == 1),
                ("account_no_customer_noise", not any(noise in lowered_visible for noise in FORBIDDEN_CUSTOMER_NOISE)),
            )
        )
    elif path == "/app/billing":
        checks.extend(
            (
                (
                    "billing_fail_closed_recovery",
                    all(marker in lowered_visible for marker in BILLING_FAIL_CLOSED_MARKERS)
                    and any(marker in lowered_visible for marker in BILLING_FAIL_CLOSED_STATE_MARKERS),
                ),
                ("billing_no_self_link", 'href="/app/billing"' not in text),
                (
                    "billing_local_board_deleted",
                    not any(marker in lowered_visible for marker in BILLING_LOCAL_BOARD_MARKERS),
                ),
                ("billing_no_customer_noise", not any(noise in lowered_visible for noise in FORBIDDEN_CUSTOMER_NOISE)),
            )
        )
    elif path == "/sign-in":
        logout_count = len(re.findall(r">Log out<", text))
        checks.extend(
            (
                ("sign_in_current_session", "Open current session" in visible_text or "Open search" in visible_text),
                ("sign_in_single_logout", logout_count == 1),
                (
                    "sign_in_no_unavailable_auth_copy",
                    "temporarily unavailable" not in lowered_visible
                    and "email delivery is unavailable" not in lowered_visible
                    and "config_missing" not in lowered_visible,
                ),
                ("sign_in_no_double_logout", logout_count <= 1),
            )
        )
    elif path == AUTHENTICATED_SHARED_FAST_RUN_PATH:
        checks.extend(
            (
                ("authenticated_fast_run_heading", "Matching homes" in visible_text),
                ("authenticated_fast_run_no_sample_copy", "Sample homes" not in visible_text),
                ("authenticated_fast_run_no_example_shortlist", "/app/example/shortlist" not in text),
                ("authenticated_fast_run_no_sign_in_copy", "Use email or one of the sign-in options below." not in visible_text),
                (
                    "authenticated_fast_run_open_property_or_empty_state",
                    "Open property" in visible_text or "No matching homes yet" in visible_text,
                ),
            )
        )
    return checks


def _billing_readiness_from_route_row(
    route_row: dict[str, object],
    *,
    expected_plan_label: str,
) -> dict[str, object]:
    passed_checks = {
        str(check.get("name") or "")
        for check in list(route_row.get("checks") or [])
        if isinstance(check, dict) and check.get("ok") is True
    }
    available_checks = {
        "billing_external_handoff",
        "billing_external_handoff_resolves",
        "billing_external_handoff_usable",
        "billing_no_second_login",
    }
    if available_checks.issubset(passed_checks):
        state = "available"
        reason = "no_second_login_handoff_verified"
    elif passed_checks & {"billing_internal_account_fallback", "billing_bridge_guided_login_assist"}:
        state = "degraded"
        reason = "account_fallback_or_second_login_required"
    elif "billing_fail_closed_recovery" in passed_checks:
        state = "unavailable"
        reason = "fail_closed_recovery_only"
    elif "billing_external_handoff" in passed_checks:
        state = "degraded"
        reason = "external_handoff_not_proven_usable"
    else:
        state = "unavailable"
        reason = "billing_route_not_available"
    normalized_plan = str(expected_plan_label or "").strip().lower()
    paid_persona = bool(normalized_plan and not normalized_plan.startswith("free"))
    return {
        "state": state,
        "available": state == "available",
        "paid_persona": paid_persona,
        "required_for_paid_persona": paid_persona,
        "compatibility_ok": route_row.get("ok") is True,
        "flagship_ok": state == "available",
        "reason": reason,
    }


def build_live_authenticated_smoke_receipt(
    *,
    base_url: str,
    api_token: str,
    principal_id: str,
    release_probe_secret: str = "",
    expected_plan_label: str = "",
    country_code: str = "AT",
    timeout_seconds: float = 8.0,
    retry_count: int = 2,
    retry_backoff_seconds: float = 0.75,
    routes: tuple[str, ...] = DEFAULT_ROUTES,
    fetcher: Callable[[str, float], dict[str, object]] | None = None,
    billing_handoff_resolver: Callable[[str, int], object] = socket.getaddrinfo,
    billing_handoff_dns_target: str = "",
    billing_handoff_checker: Callable[[str, float], dict[str, object]] | None = None,
    billing_bridge_assist_checker: Callable[[str, float], dict[str, object]] | None = None,
) -> dict[str, object]:
    try:
        authorized_origin = validated_live_base_origin(base_url)
    except ValueError as exc:
        return {
            "base_url": base_url,
            "principal_id": principal_id,
            "expected_plan_label": expected_plan_label,
            "country_code": country_code,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "route_count": 0,
            "failed_count": 1,
            "status": "blocked",
            "checks": [{"name": "live_base_origin_safe", "ok": False, "reason": str(exc)}],
            "notes": ["Authenticated live probes require an exact HTTPS origin; HTTP is allowed only on loopback."],
        }
    checks: list[dict[str, object]] = []
    failures = 0

    def _default_fetcher(url: str, timeout: float) -> dict[str, object]:
        return fetch_url(
            url,
            timeout_seconds=timeout,
            api_token=api_token,
            principal_id=principal_id,
            country_code=country_code,
            release_probe_secret=release_probe_secret,
            release_probe_configured_routes=(AUTHENTICATED_SHARED_FAST_RUN_PATH,),
            authorized_origin=authorized_origin,
        )

    effective_fetcher = fetcher or _default_fetcher
    if billing_handoff_checker is not None:
        effective_billing_handoff_checker = billing_handoff_checker
    elif fetcher is None:
        effective_billing_handoff_checker = lambda location, timeout: _https_handoff_url_usable(location, timeout_seconds=timeout)
    else:
        effective_billing_handoff_checker = lambda _location, _timeout: {"ok": True, "status_code": 0, "error": "skipped_offline_fetcher"}
    if billing_bridge_assist_checker is not None:
        effective_billing_bridge_assist_checker = billing_bridge_assist_checker
    elif fetcher is None:
        effective_billing_bridge_assist_checker = (
            lambda location, timeout: _billing_bridge_guided_login_assist_probe(location, timeout_seconds=timeout)
        )
    else:
        effective_billing_bridge_assist_checker = (
            lambda _location, _timeout: {"ok": False, "status_code": 0, "error": "skipped_offline_fetcher"}
        )
    for path in routes:
        url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
        attempts: list[dict[str, object]] = []
        max_attempts = max(1, int(retry_count) + 1)
        for attempt_index in range(max_attempts):
            result = effective_fetcher(url, timeout_seconds)
            attempts.append(result)
            status_code = int(result.get("status_code") or 0)
            transient_failure = bool(result.get("error")) or status_code == 0 or status_code >= 500
            if not transient_failure or attempt_index >= max_attempts - 1:
                break
            sleep(max(0.0, float(retry_backoff_seconds)) * float(attempt_index + 1))
        result = attempts[-1]
        status_code = int(result.get("status_code") or 0)
        headers = dict(result.get("headers") or {})
        body = bytes(result.get("body") or b"")
        text = _decode_body(body)
        bridge_login_assist_probe: dict[str, object] = {}
        if path == "/app/billing" and status_code in {303, 307}:
            location = _header_value(headers, "Location")
            if _is_internal_billing_account_fallback(location):
                route_checks = [
                    ("status_ok", True),
                    *_security_header_checks(headers=headers),
                    ("billing_internal_account_fallback", True),
                    ("billing_local_board_deleted", True),
                    ("billing_no_customer_noise", True),
                ]
                billing_handoff_probe = {}
                billing_handoff_resolution = {}
            else:
                billing_handoff_resolution = _resolve_billing_external_handoff(
                    base_url=base_url,
                    location=location,
                    fetcher=effective_fetcher,
                    timeout_seconds=timeout_seconds,
                )
                external_location = str(billing_handoff_resolution.get("external_location") or "").strip()
                bridge_launch_ok = (
                    not billing_handoff_resolution.get("bridge_launch_used")
                    or (
                        int(billing_handoff_resolution.get("bridge_launch_status_code") or 0) in {303, 307}
                        and not str(billing_handoff_resolution.get("bridge_launch_error") or "").strip()
                    )
                )
                if _is_internal_billing_account_fallback(external_location):
                    billing_handoff_probe = {}
                    route_checks = [
                        ("status_ok", True),
                        *_security_header_checks(headers=headers),
                        ("billing_bridge_launch", bridge_launch_ok),
                        ("billing_internal_account_fallback", True),
                        ("billing_local_board_deleted", True),
                        ("billing_no_customer_noise", True),
                    ]
                else:
                    billing_handoff_probe = effective_billing_handoff_checker(external_location, timeout_seconds)
                    bridge_login_assist_probe = {}
                    billing_handoff_error = str(billing_handoff_probe.get("error") or "").strip()
                    billing_no_second_login = billing_handoff_error != "handoff_url_requires_separate_login"
                    if (
                        billing_handoff_probe.get("ok") is not True
                        and not billing_no_second_login
                        and str(urllib.parse.urlparse(external_location).path or "").strip().startswith("/sso/propertyquarry")
                    ):
                        bridge_login_assist_probe = effective_billing_bridge_assist_checker(external_location, timeout_seconds)
                    external_handoff_usable = bool(billing_handoff_probe.get("ok"))
                    route_checks = [
                        ("status_ok", True),
                        *_security_header_checks(headers=headers),
                        ("billing_bridge_launch", bridge_launch_ok),
                        ("billing_external_handoff", external_location.startswith("https://") and "/app/billing" not in external_location),
                        (
                            "billing_external_handoff_resolves",
                            _https_redirect_host_resolves(
                                external_location,
                                billing_handoff_resolver,
                                expected_cname_target=billing_handoff_dns_target,
                            ),
                        ),
                        ("billing_external_handoff_usable", external_handoff_usable),
                        ("billing_no_second_login", billing_no_second_login),
                        ("billing_local_board_deleted", True),
                        ("billing_no_customer_noise", True),
                    ]
                    if bridge_login_assist_probe:
                        route_checks.append(
                            (
                                "billing_bridge_guided_login_assist",
                                bool(bridge_login_assist_probe.get("ok")),
                            )
                        )
        elif path == "/app/billing" and status_code == 503:
            route_checks = [
                ("status_ok", True),
                *_security_header_checks(headers=headers),
                *_route_checks(path=path, text=text, expected_plan_label=expected_plan_label),
            ]
        elif path == AUTHENTICATED_SHARED_FAST_RUN_PATH and status_code in {303, 307}:
            location = _header_value(headers, "Location")
            route_checks = [
                ("status_ok", True),
                *_security_header_checks(headers=headers),
                ("authenticated_fast_run_redirects_to_latest", _is_non_public_shortlist_run_path(location)),
                ("authenticated_fast_run_not_public_entrypoint_redirect", not _is_public_shortlist_entrypoint_path(location)),
            ]
        else:
            route_checks = [
                *(
                    [("status_ok", status_code == 200)]
                    if path in {"/app/account", "/app/billing", "/sign-in", AUTHENTICATED_SHARED_FAST_RUN_PATH}
                    else []
                ),
                *_security_header_checks(headers=headers),
                *_route_checks(path=path, text=text, expected_plan_label=expected_plan_label),
            ]
        ok = all(passed for _, passed in route_checks) and not result.get("error")
        if not ok:
            failures += 1
        route_row: dict[str, object] = {
                "path": path,
                "status_code": status_code,
                "ok": ok,
                "final_url": str(result.get("final_url") or url),
                "duration_ms": int(result.get("duration_ms") or 0),
                "attempt_count": len(attempts),
                "content_type": _header_value(headers, "Content-Type"),
                "checks": [{"name": name, "ok": passed} for name, passed in route_checks],
                "snippet": _compact_snippet(text),
                "error": str(result.get("error") or ""),
                **(
                    {
                        "billing_handoff_probe": billing_handoff_probe,
                        "billing_handoff_resolution": billing_handoff_resolution,
                        **({"billing_bridge_login_assist_probe": bridge_login_assist_probe} if bridge_login_assist_probe else {}),
                    }
                    if path == "/app/billing" and status_code in {303, 307}
                    else {}
                ),
            }
        checks.append(route_row)
    billing_route_row = next(
        (
            row
            for row in checks
            if isinstance(row, dict) and str(row.get("path") or "").strip() == "/app/billing"
        ),
        {},
    )
    billing_readiness = _billing_readiness_from_route_row(
        billing_route_row,
        expected_plan_label=expected_plan_label,
    )
    if billing_route_row:
        billing_route_row["billing_readiness"] = dict(billing_readiness)
    return _json_safe(
        {
        "base_url": base_url,
        "principal_id": principal_id,
        "expected_plan_label": expected_plan_label,
        "country_code": country_code,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "route_count": len(routes),
        "failed_count": failures,
        "status": "pass" if failures == 0 else "fail",
        "billing_readiness": billing_readiness,
        "checks": checks,
        "notes": [
            "This smoke is authenticated and read-only.",
            "It verifies paid customer surfaces: account, billing, sign-in state, and the shared shortlist URL does not leak sample homes to signed-in users.",
        ],
        },
        secrets=(api_token, release_probe_secret),
    )


def main() -> int:
    if len(os.sys.argv) > 1 and os.sys.argv[1] in {"--help", "-h"}:
        print(
            "Usage:\n"
            "  python3 scripts/propertyquarry_live_authenticated_smoke.py [--base-url <url>] [--principal-id <id>] [--route <path>]... [--write <path>]\n\n"
            "Smokes the authenticated PropertyQuarry runtime surfaces using EA_API_TOKEN."
        )
        return 0
    parser = argparse.ArgumentParser(description="PropertyQuarry authenticated live runtime smoke.")
    parser.add_argument("--base-url", default=_env_value("PROPERTYQUARRY_LIVE_SMOKE_BASE_URL") or "http://localhost:8097")
    parser.add_argument("--principal-id", default=_env_value("PROPERTYQUARRY_LIVE_SMOKE_PRINCIPAL_ID") or _env_value("EA_PRINCIPAL_ID") or "cf-email:tibor.girschele@gmail.com")
    parser.add_argument("--expected-plan-label", default=_env_value("PROPERTYQUARRY_LIVE_SMOKE_PLAN_LABEL") or "Agent")
    parser.add_argument("--country-code", default=_env_value("PROPERTYQUARRY_LIVE_SMOKE_COUNTRY_CODE") or "AT")
    parser.add_argument("--api-token", default=_env_value("EA_API_TOKEN"))
    parser.add_argument(
        "--release-probe-secret-stdin",
        action="store_true",
        help="Read the protected release-probe credential once from bounded stdin.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--retry-count", type=int, default=2)
    parser.add_argument("--retry-backoff-seconds", type=float, default=0.75)
    parser.add_argument("--route", action="append", default=[], help="Route to smoke. Defaults to customer surfaces plus the shared shortlist URL.")
    parser.add_argument("--write", default="", help="Optional JSON receipt output path.")
    parser.add_argument(
        "--billing-handoff-dns-target",
        default=_env_value("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BILLING_DNS_TARGET"),
    )
    args = parser.parse_args()
    release_probe_secret = read_release_probe_secret_from_stdin(
        parser,
        enabled=bool(args.release_probe_secret_stdin),
    )
    scrub_release_probe_secret_environment()

    if not release_probe_secret and not str(args.api_token or "").strip():
        raise SystemExit(
            "--release-probe-secret-stdin or EA_API_TOKEN is required for authenticated live smoke."
        )

    receipt = build_live_authenticated_smoke_receipt(
        base_url=str(args.base_url),
        api_token=str(args.api_token),
        principal_id=str(args.principal_id),
        release_probe_secret=release_probe_secret,
        expected_plan_label=str(args.expected_plan_label),
        country_code=str(args.country_code),
        timeout_seconds=float(args.timeout_seconds),
        retry_count=int(args.retry_count),
        retry_backoff_seconds=float(args.retry_backoff_seconds),
        routes=tuple(args.route or DEFAULT_LIVE_ROUTES),
        billing_handoff_dns_target=str(args.billing_handoff_dns_target),
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        write_path = Path(args.write)
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
