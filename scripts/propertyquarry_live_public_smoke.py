#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


PUBLIC_DEMO_RUN_PATH = "/app/shortlist/run/public-demo-run"
OPAQUE_PUBLIC_SAMPLE_RUN_PATH = "/app/shortlist/run/0a89ead9e0b048288cca22d1aac54fa7"
PUBLIC_SAMPLE_RUN_PATHS = {PUBLIC_DEMO_RUN_PATH, OPAQUE_PUBLIC_SAMPLE_RUN_PATH}

PUBLIC_SITEMAP_ROUTES = (
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
)
PUBLIC_INFORMATION_ROUTES = (*PUBLIC_SITEMAP_ROUTES, "/register", "/sign-in")

DEFAULT_ROUTES = (
    *PUBLIC_INFORMATION_ROUTES,
    "/manifest.webmanifest",
    "/service-worker.js",
    "/robots.txt",
    "/sitemap.xml",
    "/app/properties",
    PUBLIC_DEMO_RUN_PATH,
    OPAQUE_PUBLIC_SAMPLE_RUN_PATH,
)

DEFAULT_BILLING_WORKER_ROUTES = (
    ("/", "https://propertyquarry.com/", "hero-redirect"),
    ("/account/upgrade", "https://propertyquarry.com/pricing", "pricing-redirect"),
)

PUBLIC_HTML_ROUTES = {
    *PUBLIC_INFORMATION_ROUTES,
    "/app/properties",
    PUBLIC_DEMO_RUN_PATH,
    OPAQUE_PUBLIC_SAMPLE_RUN_PATH,
}

FORBIDDEN_VISIBLE_INTERNAL_COPY = (
    "current best so far",
    "decision support",
    "dossier",
    "evidence",
    "magic fit",
    "magicfit",
    "no source completed",
    "packet",
    "proof",
    "provider webpage",
    "release checks",
    "run ranking",
    "source completed",
    "source trail",
    "suppressed_generic_listing_page",
    "verified",
)


def _compact_snippet(text: str, *, limit: int = 180) -> str:
    return re.sub(r"\s+", " ", str(text or "")[:limit]).strip()


def _decode_body(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def _visible_text(text: str) -> str:
    without_hidden = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    without_tags = re.sub(r"<[^>]+>", " ", without_hidden)
    return re.sub(r"\s+", " ", without_tags).strip()


def _header_value(headers: dict[str, object], name: str) -> str:
    normalized_name = str(name or "").strip().lower()
    for key, value in headers.items():
        if str(key or "").strip().lower() == normalized_name:
            return str(value or "").strip()
    return ""


def _security_header_checks(*, path: str, final_url: str, headers: dict[str, object]) -> list[tuple[str, bool]]:
    html_like_paths = {
        *PUBLIC_INFORMATION_ROUTES,
        "/app/properties",
        *PUBLIC_SAMPLE_RUN_PATHS,
    }
    if path not in html_like_paths:
        return []
    csp = _header_value(headers, "Content-Security-Policy")
    permissions = _header_value(headers, "Permissions-Policy")
    parsed_final = urllib.parse.urlparse(str(final_url or ""))
    checks = [
        ("security_csp", "default-src 'self'" in csp and "frame-ancestors 'self'" in csp),
        ("security_nosniff", _header_value(headers, "X-Content-Type-Options").lower() == "nosniff"),
        ("security_referrer_policy", _header_value(headers, "Referrer-Policy") == "strict-origin-when-cross-origin"),
        ("security_permissions_policy", "camera=()" in permissions and "microphone=()" in permissions),
    ]
    if parsed_final.scheme == "https":
        checks.append(
            (
                "security_hsts",
                _header_value(headers, "Strict-Transport-Security").lower().startswith("max-age=31536000"),
            )
        )
    return checks


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def fetch_url(url: str, *, timeout_seconds: float, follow_redirects: bool = True) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PropertyQuarry-live-smoke/1.0",
            "Accept": "text/html,application/json,*/*",
        },
    )
    started = time.perf_counter()
    try:
        opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=timeout_seconds) as response:
            return {
                "status_code": int(response.status),
                "final_url": str(response.geturl()),
                "headers": dict(response.headers.items()),
                "body": response.read(220_000),
                "duration_ms": round((time.perf_counter() - started) * 1000),
            }
    except urllib.error.HTTPError as exc:
        return {
            "status_code": int(exc.code),
            "final_url": str(exc.geturl()),
            "headers": dict(exc.headers.items()),
            "body": exc.read(220_000),
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return {
            "status_code": 0,
            "final_url": url,
            "headers": {},
            "body": b"",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _route_checks(*, path: str, status_code: int, final_url: str, text: str) -> list[tuple[str, bool]]:
    checks: list[tuple[str, bool]] = []
    visible_text = _visible_text(text)
    lowered_visible = visible_text.lower()
    if path in PUBLIC_HTML_ROUTES:
        route_forbidden_terms = tuple(
            term
            for term in FORBIDDEN_VISIBLE_INTERNAL_COPY
            if not (path == "/integrations" and term == "verified")
        )
        checks.append(
            (
                "no_visible_internal_proof_copy",
                not any(term in lowered_visible for term in route_forbidden_terms),
            )
        )
    if path in PUBLIC_INFORMATION_ROUTES:
        checks.extend(
            (
                ("contains_propertyquarry", "PropertyQuarry" in text),
                ("no_chummer_copy", "chummer" not in text.lower()),
                ("no_generic_ea_copy", "Executive Assistant" not in text and "Morning Memo" not in text),
            )
        )
    if path == "/":
        checks.append(("home_has_main_copy", "Search once. See the right homes. Decide faster." in text))
        checks.append(("home_no_visible_proof_noise", "proof" not in lowered_visible))
        checks.append(("home_no_legacy_proof_component", "pq-proof" not in text.lower()))
    elif path == "/security":
        checks.extend(
            (
                (
                    "security_route_copy",
                    "Clear requirements. Calmer search." in text
                    and "Fit guide" in text
                    and "Must-haves decide what belongs" in text
                    and "/how-it-works/score" in text
                    and "You choose what is shared" in text,
                ),
                ("security_old_proof_copy_removed", "release-side proof" not in text.lower()),
                ("security_no_internal_release_copy", "Release checks and security review" not in text),
            )
        )
    elif path == "/pricing":
        checks.extend(
            (
                (
                    "pricing_minimal_copy",
                    "Pricing" in text
                    and ("Start free" in text or ("Free" in visible_text and "Open search" in visible_text)),
                ),
                ("pricing_old_noise_removed", "Choose the lane that matches the real search workload" not in text),
                ("pricing_subtitle_removed", "Choose by sources, shortlist size, and research depth." not in text),
            )
        )
    elif path == "/integrations":
        checks.extend(
            (
                ("integrations_page_substance", "Connect only what improves the property workflow." in text),
                ("integrations_page_next_action", "View details" in visible_text),
            )
        )
    elif path == "/docs":
        checks.extend(
            (
                ("docs_page_substance", "Understand PropertyQuarry without reading the plumbing." in text),
                ("docs_page_next_action", "Open" in visible_text),
            )
        )
    elif path == "/guides/wohnung-kaufen-wien-checkliste":
        checks.extend(
            (
                ("guide_page_substance", "Wohnung kaufen in Wien" in text and "The shortest useful checklist" in text),
                ("guide_page_next_action", "Open PropertyQuarry" in visible_text),
            )
        )
    elif path == "/markets/vienna":
        checks.extend(
            (
                ("market_page_substance", "Vienna apartment search" in text and "What separates signal from noise" in text),
                ("market_page_next_action", "Start a Vienna search" in visible_text),
            )
        )
    elif path == "/sign-in":
        google_href = bool(re.search(r'href="/sign-in/google(?:\?[^\"]*)?"', text))
        facebook_href = bool(re.search(r'href="/sign-in/facebook(?:\?[^\"]*)?"', text))
        id_austria_href = bool(re.search(r'href="/sign-in/id-austria(?:\?[^\"]*)?"', text))
        google_active = google_href and "Continue with Google" in text
        google_unavailable = not google_href and "Google unavailable" in text
        facebook_active = facebook_href and "Continue with Facebook" in text
        facebook_unavailable = not facebook_href and "Facebook unavailable" in text
        google_hidden = not google_href and "Google unavailable" not in text
        facebook_hidden = not facebook_href and "Facebook unavailable" not in text
        google_identity_only = (
            not google_active
            or (
                "Google only verifies your identity for a PropertyQuarry-local session." in text
                and "Identity only" in text
            )
        )
        signed_in_state = 'action="/app/actions/sign-out"' in text
        email_link_available = 'action="/sign-in/email-link"' in text
        connected_provider_active = google_active or facebook_active or id_austria_href
        checks.extend(
            (
                (
                    "sign_in_minimal_copy",
                    (
                        (
                            signed_in_state
                            and "Continue your property search." in text
                        )
                        or (
                            not signed_in_state
                            and "Use a secure email link if your address already has access." in text
                            and (not email_link_available or "Create an account with email." in text)
                        )
                    )
                    and google_identity_only
                    and "Google?" not in text
                    and "Facebook?" not in text,
                ),
                (
                    "sign_in_connected_identity_creates_account",
                    signed_in_state
                    or not connected_provider_active
                    or (
                        "Sign-in providers verify who you are, then open the same local account or create it if needed."
                        in text
                    ),
                ),
                (
                    "sign_in_no_unavailable_auth_copy",
                    "temporarily unavailable" not in lowered_visible
                    and "email delivery is unavailable" not in lowered_visible
                    and "config_missing" not in lowered_visible,
                ),
                ("sign_in_no_rollout_language", "verified rollout" not in lowered_visible and "invite only" not in lowered_visible),
                ("sign_in_no_waitlist_language", "waitlist" not in lowered_visible and "request access" not in lowered_visible),
                ("sign_in_google_state", google_active or google_unavailable or google_hidden),
                ("sign_in_facebook_state", facebook_active or facebook_unavailable or facebook_hidden),
                (
                    "sign_in_google_feedback",
                    (not google_active) or 'data-submitting-label="Opening Google..."' in text,
                ),
            )
        )
        if facebook_href:
            checks.extend(
                (
                    ("sign_in_facebook_control", facebook_href),
                    ("sign_in_facebook_feedback", 'data-submitting-label="Opening Facebook..."' in text),
                )
            )
    elif path in {"/privacy", "/terms", "/support", "/imprint", "/cookies", "/subprocessors", "/refunds", "/disclaimers"}:
        expected_by_path = {
            "/privacy": ("Privacy", "Public tours should use a narrow public manifest"),
            "/terms": ("Terms", "Generated or embedded tours help screening"),
            "/support": ("Support", "wrong-area matches"),
            "/imprint": ("Imprint", "How to reach PropertyQuarry"),
            "/cookies": ("Cookies and Analytics", "essential cookies"),
            "/subprocessors": ("Subprocessors", "Service partner registry"),
            "/refunds": ("Refunds and Cancellation", "failed payment recovery"),
            "/disclaimers": ("Disclaimers", "Generated visualization"),
        }
        expected = expected_by_path[path]
        checks.extend(
            (
                ("trust_page_title", expected[0] in text),
                ("trust_page_substance", expected[1] in text),
                ("trust_page_no_placeholder", "Before public paid launch" not in text and "Replace placeholder" not in text),
                (
                    "trust_page_no_operator_secret_copy",
                    "credentials" not in visible_text.lower()
                    and "provider login" not in visible_text.lower()
                    and "api key" not in visible_text.lower(),
                ),
            )
        )
    elif path == "/manifest.webmanifest":
        try:
            manifest_payload = json.loads(text)
        except Exception:
            manifest_payload = {}
        icons = manifest_payload.get("icons") if isinstance(manifest_payload, dict) else []
        icon_rows = [dict(row) for row in icons if isinstance(row, dict)] if isinstance(icons, list) else []
        shortcuts = manifest_payload.get("shortcuts") if isinstance(manifest_payload, dict) else []
        shortcut_urls = {
            str(row.get("url") or "").strip()
            for row in shortcuts
            if isinstance(row, dict)
        } if isinstance(shortcuts, list) else set()
        checks.extend(
            (
                ("manifest_name", isinstance(manifest_payload, dict) and manifest_payload.get("name") == "PropertyQuarry"),
                (
                    "manifest_language_direction",
                    isinstance(manifest_payload, dict)
                    and manifest_payload.get("lang") == "en"
                    and manifest_payload.get("dir") == "ltr",
                ),
                ("manifest_id", isinstance(manifest_payload, dict) and manifest_payload.get("id") == "/app/search"),
                ("manifest_start_url", isinstance(manifest_payload, dict) and manifest_payload.get("start_url") == "/app/search"),
                ("manifest_display_scope", isinstance(manifest_payload, dict) and manifest_payload.get("display") == "standalone" and manifest_payload.get("scope") == "/"),
                (
                    "manifest_display_override",
                    isinstance(manifest_payload, dict)
                    and "standalone" in list(manifest_payload.get("display_override") or []),
                ),
                (
                    "manifest_launch_handler",
                    isinstance(manifest_payload, dict)
                    and dict(manifest_payload.get("launch_handler") or {}).get("client_mode") == "navigate-existing",
                ),
                (
                    "manifest_related_apps_disabled",
                    isinstance(manifest_payload, dict)
                    and manifest_payload.get("prefer_related_applications") is False,
                ),
                (
                    "manifest_maskable_icon",
                    any(str(row.get("src") or "") == "/pwa-icon.svg" and "maskable" in str(row.get("purpose") or "") for row in icon_rows),
                ),
                (
                    "manifest_png_install_icons",
                    {
                        (str(row.get("src") or ""), str(row.get("sizes") or ""), str(row.get("type") or ""))
                        for row in icon_rows
                    }
                    >= {
                        ("/pwa-icon-192.png", "192x192", "image/png"),
                        ("/pwa-icon-512.png", "512x512", "image/png"),
                    },
                ),
                (
                    "manifest_core_shortcuts",
                    {"/app/search", "/app/properties", "/app/shortlist", "/app/agents"}.issubset(shortcut_urls),
                ),
            )
        )
    elif path == "/service-worker.js":
        checks.append(("service_worker_no_cache_api", "caches." not in text and "skipWaiting" in text))
    elif path == "/robots.txt":
        checks.append(("robots_sitemap", "Sitemap: https://propertyquarry.com/sitemap.xml" in text))
    elif path == "/sitemap.xml":
        parsed_final = urllib.parse.urlparse(str(final_url or ""))
        sitemap_origin = f"{parsed_final.scheme}://{parsed_final.netloc}" if parsed_final.scheme and parsed_final.netloc else "https://propertyquarry.com"
        observed_origins = (sitemap_origin, "https://propertyquarry.com")
        missing_sitemap_routes = [
            route
            for route in PUBLIC_SITEMAP_ROUTES
            if not any(f"<loc>{origin}{route}</loc>" in text for origin in observed_origins)
        ]
        checks.append(("sitemap_all_public_information_routes", not missing_sitemap_routes))
    elif path == "/app/properties":
        checks.extend(
            (
                (
                    "app_boundary",
                    status_code in {200, 401, 403}
                    or "/sign-in" in final_url
                    or "/app/search" in final_url
                    or "/app/properties" in final_url,
                ),
                ("not_public_home_leak", "Search once. See the right homes. Decide faster." not in text),
            )
        )
    elif path in PUBLIC_SAMPLE_RUN_PATHS:
        checks.extend(
            (
                ("public_fast_run_sample_copy", "Sample homes" in text),
                (
                    "public_fast_run_open_property",
                    "Open property" in visible_text and "/app/example/shortlist?candidate=" in text,
                ),
                (
                    "public_fast_run_diorama_payload",
                    bool(
                        re.search(
                            r"(?:telegram-preview|diorama-preview|scene-01|example-shortlist-(?:home|diorama)-1)\.(?:png|jpe?g|webp)",
                            text,
                            re.IGNORECASE,
                        )
                    ),
                ),
                ("public_fast_run_not_sign_in", "Use email or one of the sign-in options below." not in visible_text),
            )
        )
    return checks


def _redirect_location(result: dict[str, object]) -> str:
    headers = dict(result.get("headers") or {})
    return _header_value(headers, "Location")


def _oauth_redirect_row(
    *,
    path: str,
    result: dict[str, object],
    checks: list[tuple[str, bool]],
) -> dict[str, object]:
    status_code = int(result.get("status_code") or 0)
    final_url = str(result.get("final_url") or "")
    headers = dict(result.get("headers") or {})
    location = _redirect_location(result)
    ok = status_code in {302, 303, 307, 308} and bool(location) and all(value for _, value in checks)
    return {
        "path": path,
        "status_code": status_code,
        "final_url": final_url,
        "duration_ms": int(result.get("duration_ms") or 0),
        "content_type": str(headers.get("Content-Type") or ""),
        "x_robots_tag": str(headers.get("X-Robots-Tag") or ""),
        "ok": ok,
        "checks": [{"name": name, "ok": value} for name, value in checks],
        "error": str(result.get("error") or ""),
        "snippet": _compact_snippet(location),
    }


def _billing_worker_row(
    *,
    billing_base_url: str,
    path: str,
    expected_location: str,
    expected_branch: str,
    timeout_seconds: float,
    redirect_fetcher: Callable[[str, float], dict[str, object]],
) -> dict[str, object]:
    normalized_billing_base = str(billing_base_url or "").strip().rstrip("/")
    normalized_path = "/" + str(path or "").strip().lstrip("/")
    url = f"{normalized_billing_base}{normalized_path}"
    result = redirect_fetcher(url, timeout_seconds)
    status_code = int(result.get("status_code") or 0)
    headers = dict(result.get("headers") or {})
    location = _redirect_location(result)
    worker = _header_value(headers, "X-PQ-Billing-Worker")
    branch = _header_value(headers, "X-PQ-Billing-Worker-Branch")
    robots = _header_value(headers, "X-Robots-Tag")
    checks = [
        ("billing_worker_redirect_status", status_code in {301, 302, 303, 307, 308}),
        ("billing_worker_location", location.rstrip("/") == str(expected_location or "").strip().rstrip("/")),
        ("billing_worker_header", worker == "propertyquarry-billing-handoff"),
        ("billing_worker_branch", branch == expected_branch),
        ("billing_worker_noindex", "noindex" in robots.lower() and "nofollow" in robots.lower()),
    ]
    return {
        "path": f"billing:{normalized_path}",
        "status_code": status_code,
        "final_url": str(result.get("final_url") or url),
        "duration_ms": int(result.get("duration_ms") or 0),
        "content_type": str(headers.get("Content-Type") or ""),
        "x_robots_tag": robots,
        "ok": all(value for _, value in checks),
        "checks": [{"name": name, "ok": value} for name, value in checks],
        "error": str(result.get("error") or ""),
        "snippet": _compact_snippet(location or _decode_body(result.get("body") if isinstance(result.get("body"), bytes) else b"")),
    }


def _billing_worker_rows(
    *,
    billing_base_url: str,
    timeout_seconds: float,
    redirect_fetcher: Callable[[str, float], dict[str, object]],
) -> list[dict[str, object]]:
    normalized_billing_base = str(billing_base_url or "").strip().rstrip("/")
    if not normalized_billing_base or normalized_billing_base.lower() in {"0", "false", "off", "none", "skip"}:
        return []
    return [
        _billing_worker_row(
            billing_base_url=normalized_billing_base,
            path=path,
            expected_location=expected_location,
            expected_branch=expected_branch,
            timeout_seconds=timeout_seconds,
            redirect_fetcher=redirect_fetcher,
        )
        for path, expected_location, expected_branch in DEFAULT_BILLING_WORKER_ROUTES
    ]


def _sign_in_provider_redirect_checks(
    *,
    normalized_base: str,
    sign_in_text: str,
    timeout_seconds: float,
    redirect_fetcher: Callable[[str, float], dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if 'href="/sign-in/google"' in sign_in_text:
        path = "/sign-in/google"
        result = redirect_fetcher(f"{normalized_base}{path}", timeout_seconds)
        location = _redirect_location(result)
        parsed = urllib.parse.urlparse(location)
        query = urllib.parse.parse_qs(parsed.query)
        scopes = set(str(query.get("scope", [""])[0]).replace(",", " ").split())
        rows.append(
            _oauth_redirect_row(
                path=path,
                result=result,
                checks=[
                    ("google_redirect_host", parsed.scheme == "https" and parsed.netloc == "accounts.google.com"),
                    ("google_identity_scope", {"openid", "email", "profile"}.issubset(scopes)),
                    ("google_callback_uri", str(query.get("redirect_uri", [""])[0]).endswith("/google/callback")),
                    ("google_state_present", bool(query.get("state", [""])[0])),
                ],
            )
        )
    if 'href="/sign-in/facebook"' in sign_in_text:
        path = "/sign-in/facebook"
        result = redirect_fetcher(f"{normalized_base}{path}", timeout_seconds)
        location = _redirect_location(result)
        parsed = urllib.parse.urlparse(location)
        query = urllib.parse.parse_qs(parsed.query)
        scope_text = str(query.get("scope", [""])[0])
        scopes = set(scope_text.replace(",", " ").split())
        rows.append(
            _oauth_redirect_row(
                path=path,
                result=result,
                checks=[
                    ("facebook_redirect_host", parsed.scheme == "https" and parsed.netloc == "www.facebook.com"),
                    ("facebook_public_profile_scope", scopes == {"public_profile"}),
                    ("facebook_no_email_scope", "email" not in scopes and "email" not in scope_text),
                    ("facebook_callback_uri", str(query.get("redirect_uri", [""])[0]).endswith("/facebook/callback")),
                    ("facebook_state_present", bool(query.get("state", [""])[0])),
                ],
            )
        )
    if 'href="/sign-in/id-austria"' in sign_in_text:
        path = "/sign-in/id-austria"
        result = redirect_fetcher(f"{normalized_base}{path}", timeout_seconds)
        location = _redirect_location(result)
        parsed = urllib.parse.urlparse(location)
        query = urllib.parse.parse_qs(parsed.query)
        scopes = set(str(query.get("scope", [""])[0]).replace(",", " ").split())
        rows.append(
            _oauth_redirect_row(
                path=path,
                result=result,
                checks=[
                    ("id_austria_redirect_host", parsed.scheme == "https" and parsed.netloc.endswith("id-austria.gv.at")),
                    ("id_austria_authorize_path", parsed.path.endswith("/authorize")),
                    ("id_austria_identity_scope", {"openid", "profile"}.issubset(scopes)),
                    ("id_austria_callback_uri", str(query.get("redirect_uri", [""])[0]).endswith("/id-austria/callback")),
                    ("id_austria_state_present", bool(query.get("state", [""])[0])),
                ],
            )
        )
    return rows


def build_live_public_smoke_receipt(
    *,
    base_url: str = "https://propertyquarry.com",
    routes: tuple[str, ...] = DEFAULT_ROUTES,
    billing_base_url: str = "",
    timeout_seconds: float = 12.0,
    fetcher: Callable[[str, float], dict[str, object]] | None = None,
) -> dict[str, object]:
    normalized_base = str(base_url or "").strip().rstrip("/") or "https://propertyquarry.com"
    configured_routes = tuple("/" + str(route or "").strip().lstrip("/") for route in routes)
    configured_route_paths = {route.split("?", 1)[0] for route in configured_routes}
    require_all_public_information_routes = configured_routes == DEFAULT_ROUTES
    missing_public_information_routes = [
        route for route in PUBLIC_INFORMATION_ROUTES if route not in configured_route_paths
    ]
    fetch = fetcher or (lambda url, timeout: fetch_url(url, timeout_seconds=timeout))
    redirect_fetch = fetcher or (lambda url, timeout: fetch_url(url, timeout_seconds=timeout, follow_redirects=False))
    checks: list[dict[str, object]] = []
    sign_in_text = ""
    for raw_path in configured_routes:
        path = "/" + str(raw_path or "").strip().lstrip("/")
        url = f"{normalized_base}{path}"
        result = fetch(url, timeout_seconds)
        body = result.get("body")
        body_bytes = body if isinstance(body, bytes) else str(body or "").encode("utf-8")
        text = _decode_body(body_bytes)
        status_code = int(result.get("status_code") or 0)
        final_url = str(result.get("final_url") or url)
        headers = dict(result.get("headers") or {})
        if path == "/sign-in":
            sign_in_text = text
        route_checks = _route_checks(path=path, status_code=status_code, final_url=final_url, text=text)
        route_checks.extend(_security_header_checks(path=path, final_url=final_url, headers=headers))
        ok = status_code < 500 and status_code > 0 and all(value for _, value in route_checks)
        checks.append(
            {
                "path": path,
                "status_code": status_code,
                "final_url": final_url,
                "duration_ms": int(result.get("duration_ms") or 0),
                "content_type": str(headers.get("Content-Type") or ""),
                "x_robots_tag": str(headers.get("X-Robots-Tag") or ""),
                "ok": ok,
                "checks": [{"name": name, "ok": value} for name, value in route_checks],
                "error": str(result.get("error") or ""),
                "snippet": _compact_snippet(text),
            }
        )
    if sign_in_text:
        checks.extend(
            _sign_in_provider_redirect_checks(
                normalized_base=normalized_base,
                sign_in_text=sign_in_text,
                timeout_seconds=timeout_seconds,
                redirect_fetcher=redirect_fetch,
            )
        )
    checks.extend(
        _billing_worker_rows(
            billing_base_url=billing_base_url,
            timeout_seconds=timeout_seconds,
            redirect_fetcher=redirect_fetch,
        )
    )
    failed = [row for row in checks if not bool(row.get("ok"))]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": normalized_base,
        "status": (
            "pass"
            if not failed and (not require_all_public_information_routes or not missing_public_information_routes)
            else "fail"
        ),
        "configured_routes": list(configured_routes),
        "required_public_information_routes": list(PUBLIC_INFORMATION_ROUTES),
        "require_all_public_information_routes": require_all_public_information_routes,
        "missing_public_information_routes": missing_public_information_routes,
        "route_count": len(checks),
        "failed_count": len(failed) + (
            1 if require_all_public_information_routes and missing_public_information_routes else 0
        ),
        "checks": checks,
        "notes": [
            "This smoke is public and non-mutating.",
            "It verifies every sitemap/legal/support/docs/integrations/guide/market page, PropertyQuarry public copy, PWA assets, SEO files, the app auth boundary, and the public billing worker handoff.",
        ],
    }


def main() -> int:
    if len(os.sys.argv) > 1 and os.sys.argv[1] in {"--help", "-h"}:
        print(
            "Usage:\n"
            "  python3 scripts/propertyquarry_live_public_smoke.py [--base-url <url>] [--route <path>]... [--write <path>]\n\n"
            "Smokes the public PropertyQuarry deployment without mutating data."
        )
        return 0
    parser = argparse.ArgumentParser(description="Smoke the public PropertyQuarry deployment without mutating data.")
    parser.add_argument("--base-url", default="https://propertyquarry.com")
    parser.add_argument(
        "--billing-base-url",
        default=os.getenv("PROPERTYQUARRY_PUBLIC_BILLING_BASE_URL", "https://billing.propertyquarry.com"),
        help="Billing worker base URL to smoke with GET redirect checks. Use 'skip' to disable.",
    )
    parser.add_argument("--route", action="append", default=[], help="Route to smoke. Defaults to core public routes.")
    parser.add_argument("--timeout-seconds", type=float, default=12.0)
    parser.add_argument("--write", default="", help="Optional JSON receipt output path.")
    args = parser.parse_args()
    receipt = build_live_public_smoke_receipt(
        base_url=args.base_url,
        routes=tuple(args.route or DEFAULT_ROUTES),
        billing_base_url=args.billing_base_url,
        timeout_seconds=args.timeout_seconds,
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        write_path = Path(args.write)
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
