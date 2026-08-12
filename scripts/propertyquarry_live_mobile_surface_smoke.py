#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import multiprocessing
import os
import re
import signal
import socket
import sys
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from scripts.propertyquarry_billing_handoff_probe import (
    PROPERTYQUARRY_BILLING_HANDOFF_ALLOWED_HOSTS,
    https_handoff_url_usable,
    https_redirect_host_resolves,
)
from scripts.propertyquarry_playwright_runtime import (
    SUPPORTED_PLAYWRIGHT_ENGINES,
    normalize_playwright_engine,
    playwright_browser_type,
    playwright_engine_launch_kwargs,
)
from scripts.propertyquarry_live_http_security import (
    SENSITIVE_REQUEST_HEADERS,
    normalized_origin,
    redact_secret_values,
    url_matches_origin,
    validated_live_base_origin,
)
from scripts.propertyquarry_live_probe_auth import live_probe_request_headers
from scripts.propertyquarry_live_probe_secret_scope import (
    read_live_api_token_from_stdin,
    read_release_probe_secret_from_stdin,
    release_probe_secret_environment_scrubbed,
    scrub_live_api_token_environment,
    scrub_release_probe_secret_environment,
)


DEFAULT_ROUTES = (
    "/app/properties",
    "/app/search",
    "/app/shortlist",
    "/app/agents",
    "/app/alerts",
    "/app/account",
    "/app/billing",
    "/app/settings/google",
    "/app/settings/access",
    "/app/settings/usage",
    "/app/settings/support",
    "/app/settings/trust",
    "/app/settings/invitations",
    "/app/research",
    "/app/properties/packets",
)
FLAGSHIP_BROWSER_VIEWPORTS = ((390, 844), (412, 915))
FLAGSHIP_BROWSER_ENGINES = SUPPORTED_PLAYWRIGHT_ENGINES
DEFAULT_BROWSER_ENGINE = "chromium"
STANDARD_PROOF_MODE = "hybrid_browser_static"
FLAGSHIP_PROOF_MODE = "playwright_browser_all"
SEEDED_RESEARCH_DETAIL_ROUTE = "/app/research/perf-candidate-1020?run_id=run-gold-mobile"
SEED_FIXTURE_USER_AGENT = "PropertyQuarry-live-mobile-surface-smoke/1.0"
SEED_FIXTURE_TIMEOUT_SECONDS = max(
    1,
    int(os.environ.get("PROPERTYQUARRY_LIVE_MOBILE_SEED_TIMEOUT_SECONDS", "30") or "30"),
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
BILLING_BRIDGE_GUIDED_LOGIN_MARKERS = (
    "continue billing",
    "back to propertyquarry",
    "billing lane",
)
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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_HTTP_SMOKE_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _http_get_for_smoke(
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: float,
    follow_redirects: bool = True,
    authorized_origin: str = "",
    release_probe_secret: str = "",
    release_probe_configured_routes: tuple[str, ...] = (),
) -> dict[str, Any]:
    sensitive_headers_present = any(
        str(key or "").strip().lower() in SENSITIVE_REQUEST_HEADERS
        for key in headers
    )
    if (sensitive_headers_present or release_probe_secret) and not authorized_origin:
        raise RuntimeError("authorized_origin_required_for_sensitive_headers")
    scoped_origin = normalized_origin(authorized_origin or url)
    current_url = str(url or "").strip()
    for redirect_count in range(6):
        request = urllib.request.Request(
            current_url,
            headers=live_probe_request_headers(
                url=current_url,
                authorized_origin=scoped_origin,
                headers=headers,
                release_probe_secret=release_probe_secret,
                method="GET",
                configured_routes=release_probe_configured_routes,
            ),
            method="GET",
        )
        try:
            with _HTTP_SMOKE_NO_REDIRECT_OPENER.open(
                request,
                timeout=max(1.0, float(timeout_seconds)),
            ) as response:
                body = response.read(2_000_000)
                return {
                    "status_code": int(getattr(response, "status", 0) or 0),
                    "headers": dict(response.headers.items()),
                    "url": str(getattr(response, "url", current_url) or current_url),
                    "text": body.decode("utf-8", errors="replace"),
                }
        except HTTPError as exc:
            body = exc.read(2_000_000)
            response = {
                "status_code": int(exc.code or 0),
                "headers": dict(exc.headers.items()),
                "url": str(exc.url or current_url),
                "text": body.decode("utf-8", errors="replace"),
            }
            location = _header_value(dict(exc.headers.items()), "location")
            if not follow_redirects or int(exc.code or 0) not in {301, 302, 303, 307, 308} or not location:
                return response
            next_url = urllib.parse.urljoin(current_url, location)
            if not url_matches_origin(next_url, scoped_origin):
                response["redirect_blocked"] = "cross_origin"
                response["redirect_location"] = next_url
                return response
            current_url = next_url
    return {
        "status_code": 0,
        "headers": {},
        "url": current_url,
        "text": "",
        "error": "same_origin_redirect_limit_exceeded",
    }


def _header_value(headers: dict[str, Any], name: str) -> str:
    wanted = str(name or "").strip().lower()
    for key, value in dict(headers or {}).items():
        if str(key).strip().lower() == wanted:
            return str(value or "").strip()
    return ""


def _release_probe_browser_navigation_url(
    url: str,
    *,
    headers: dict[str, str],
    authorized_origin: str,
    timeout_seconds: float,
    release_probe_secret: str,
    release_probe_configured_routes: tuple[str, ...] = (),
) -> tuple[str, dict[str, object]]:
    """Resolve same-origin redirects with a fresh signature per hop.

    Playwright carries headers supplied to an intercepted request onto its
    redirects without re-running the route handler. A path-bound release
    signature must never be reused that way, so the bounded HTTP probe resolves
    the redirect chain first and the browser opens the verified final URL.
    """

    response = _http_get_for_smoke(
        url,
        headers=headers,
        timeout_seconds=timeout_seconds,
        follow_redirects=True,
        authorized_origin=authorized_origin,
        release_probe_secret=release_probe_secret,
        release_probe_configured_routes=release_probe_configured_routes,
    )
    status_code = int(response.get("status_code") or 0)
    final_url = str(response.get("url") or url).strip() or url
    redirect_resolved = (
        status_code == 200
        and final_url != url
        and url_matches_origin(final_url, authorized_origin)
    )
    return (
        final_url if redirect_resolved else url,
        {
            "release_probe_redirect_resolved": redirect_resolved,
            "release_probe_redirect_status_code": status_code,
            "release_probe_navigation_url": final_url if redirect_resolved else url,
        },
    )


def _redact_sensitive_receipt_text(value: object) -> str:
    redacted = re.sub(
        r"(?i)(/app/research/)[^/?#\s\"'<>]+(?:[?#][^\s\"'<>]*)?",
        r"\1[redacted]",
        str(value or ""),
    )
    redacted = re.sub(
        r"(?i)(/login/token/)[^/?#\s\"'>]+",
        r"\1[redacted]",
        redacted,
    )
    query_key_pattern = "|".join(re.escape(key) for key in SENSITIVE_URL_QUERY_KEYS)
    return re.sub(
        rf"(?i)([?&](?:{query_key_pattern})=)[^&#\s\"'>]+",
        r"\1[redacted]",
        redacted,
    )


def _redact_sensitive_receipt_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return _redact_sensitive_receipt_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, dict):
        return {str(key): _redact_sensitive_receipt_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive_receipt_value(item) for item in value]
    if isinstance(value, str):
        return _redact_sensitive_receipt_text(value)
    return value


def _redact_concrete_secret_values(value: Any, *, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, bytes):
        return redact_secret_values(value.decode("utf-8", errors="replace"), secrets=secrets)
    if isinstance(value, dict):
        return {str(key): _redact_concrete_secret_values(item, secrets=secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_concrete_secret_values(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        return redact_secret_values(value, secrets=secrets)
    return value


def _log_smoke_progress(message: str) -> None:
    print(f"[propertyquarry-mobile-smoke] {message}", file=sys.stderr, flush=True)


def _route_log_label(route: str) -> str:
    normalized = str(route or "").strip()
    if re.match(r"^/app/research/[^/?#]+", normalized, flags=re.IGNORECASE):
        return "/app/research/[redacted]"
    return normalized


def _visible_text(text: str) -> str:
    without_hidden = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    without_tags = re.sub(r"<[^>]+>", " ", without_hidden)
    return re.sub(r"\s+", " ", without_tags).strip()


def _resolve_mobile_billing_external_handoff(
    *,
    base_url: str,
    redirect_location: str,
    request_headers: dict[str, str],
    timeout_ms: int,
    release_probe_secret: str = "",
    release_probe_configured_routes: tuple[str, ...] = (),
) -> dict[str, object]:
    normalized_location = str(redirect_location or "").strip()
    if not normalized_location:
        return {
            "external_location": "",
            "bridge_launch_used": False,
            "bridge_launch_url": "",
            "bridge_launch_status_code": 0,
        }
    parsed = urllib.parse.urlparse(normalized_location)
    if parsed.scheme == "https":
        return {
            "external_location": normalized_location,
            "bridge_launch_used": False,
            "bridge_launch_url": "",
            "bridge_launch_status_code": 0,
        }
    normalized_path = parsed.path or ""
    if not normalized_path.startswith("/app/api/property/billing/bridge-launch"):
        return {
            "external_location": normalized_location,
            "bridge_launch_used": False,
            "bridge_launch_url": "",
            "bridge_launch_status_code": 0,
        }
    bridge_launch_url = urllib.parse.urljoin(base_url.rstrip("/") + "/", normalized_location.lstrip("/"))
    probe_kwargs: dict[str, Any] = {}
    if release_probe_secret:
        probe_kwargs = {
            "release_probe_secret": release_probe_secret,
            "release_probe_configured_routes": release_probe_configured_routes,
        }
    bridge_response = _http_get_for_smoke(
        bridge_launch_url,
        headers=request_headers,
        timeout_seconds=max(1.0, timeout_ms / 1000.0),
        follow_redirects=False,
        authorized_origin=validated_live_base_origin(base_url),
        **probe_kwargs,
    )
    return {
        "external_location": _header_value(dict(bridge_response.get("headers") or {}), "location"),
        "bridge_launch_used": True,
        "bridge_launch_url": bridge_launch_url,
        "bridge_launch_status_code": int(bridge_response.get("status_code") or 0),
    }


def _mobile_billing_bridge_guided_login_assist_probe(
    location: str,
    *,
    timeout_ms: int,
) -> dict[str, object]:
    normalized_location = str(location or "").strip()
    parsed = urllib.parse.urlparse(normalized_location)
    if parsed.scheme != "https" or not parsed.netloc or "/sso/propertyquarry" not in parsed.path:
        return {
            "ok": False,
            "status_code": 0,
            "final_url": normalized_location,
            "error": "bridge_login_assist_not_applicable",
        }
    try:
        response = _http_get_for_smoke(
            normalized_location,
            headers={"Accept": "text/html,application/xhtml+xml"},
            timeout_seconds=max(1.0, timeout_ms / 1000.0),
            follow_redirects=True,
        )
        status_code = int(response.get("status_code") or 0)
        final_url = str(response.get("url") or normalized_location)
        body = str(response.get("text") or "")
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "final_url": normalized_location,
            "error": f"{type(exc).__name__}: {exc}",
        }
    lowered_body = body.lower()
    visible_text = _visible_text(body).lower()
    has_guided_markers = all(marker in visible_text for marker in BILLING_BRIDGE_GUIDED_LOGIN_MARKERS)
    has_email_field = 'name="email"' in lowered_body or 'type="email"' in lowered_body
    ok = status_code == 200 and has_guided_markers and has_email_field
    return {
        "ok": ok,
        "status_code": status_code,
        "final_url": final_url,
        "has_guided_markers": has_guided_markers,
        "has_email_field": has_email_field,
        "error": "" if ok else "bridge_login_assist_missing",
    }


def _playwright_route_metrics_worker(
    queue: Any,
    *,
    url: str,
    headers: dict[str, str],
    authorized_origin: str,
    browser_args: list[str],
    viewport_width: int,
    viewport_height: int,
    route_timeout_ms: int,
    browser_engine: str = DEFAULT_BROWSER_ENGINE,
    release_probe_secret: str = "",
    release_probe_configured_routes: tuple[str, ...] = (),
) -> None:
    # This worker may be invoked through either multiprocessing start mode.
    # Scrub again before Playwright starts its driver or browser process.
    scrub_release_probe_secret_environment()
    normalized_engine = normalize_playwright_engine(browser_engine)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser_type = playwright_browser_type(playwright, engine=normalized_engine)
            try:
                browser = browser_type.launch(
                    **playwright_engine_launch_kwargs(
                        playwright,
                        engine=normalized_engine,
                        args=browser_args,
                    )
                )
            except BaseException as exc:
                raise RuntimeError(
                    f"playwright_browser_engine_unavailable:{normalized_engine}:{type(exc).__name__}: {exc}"
                ) from exc
            try:
                context_options: dict[str, object] = {
                    "viewport": {"width": viewport_width, "height": viewport_height},
                    "has_touch": True,
                    "service_workers": "block",
                }
                if normalized_engine != "firefox":
                    context_options["is_mobile"] = True
                context = browser.new_context(
                    **context_options,
                )
                try:
                    context.route(
                        "**/*",
                        lambda route: _continue_playwright_route_with_origin_scoped_headers(
                            route,
                            authorized_origin=authorized_origin,
                            headers=headers,
                            release_probe_secret=release_probe_secret,
                            release_probe_configured_routes=release_probe_configured_routes,
                        ),
                    )
                    page = context.new_page()
                    page.set_default_timeout(route_timeout_ms)
                    page.set_default_navigation_timeout(route_timeout_ms)
                    response = page.goto(url, wait_until="commit", timeout=route_timeout_ms)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=min(2500, route_timeout_ms))
                    except Exception:
                        pass
                    page.wait_for_timeout(1200)
                    status = int(response.status) if response is not None else 0
                    touchscreen_tap_capable = False
                    try:
                        page.evaluate(
                            """() => {
                              const previous = document.getElementById('propertyquarry-touch-capability-probe');
                              if (previous) previous.remove();
                              const probe = document.createElement('div');
                              probe.id = 'propertyquarry-touch-capability-probe';
                              probe.setAttribute('aria-hidden', 'true');
                              probe.style.cssText = [
                                'position:fixed',
                                'inset:0 auto auto 0',
                                'width:20px',
                                'height:20px',
                                'z-index:2147483647',
                                'background:transparent',
                                'pointer-events:auto'
                              ].join(';');
                              const contain = (event) => {
                                event.preventDefault();
                                event.stopImmediatePropagation();
                              };
                              for (const eventName of ['touchstart', 'pointerdown', 'mousedown', 'click']) {
                                probe.addEventListener(eventName, contain, {capture: true, passive: false});
                              }
                              document.body.appendChild(probe);
                            }"""
                        )
                        page.touchscreen.tap(10, 10)
                        touchscreen_tap_capable = True
                    except Exception:
                        touchscreen_tap_capable = False
                    finally:
                        try:
                            page.evaluate(
                                """() => document.getElementById('propertyquarry-touch-capability-probe')?.remove()"""
                            )
                        except Exception:
                            pass
                    metrics = dict(page.evaluate(_collect_metrics_script()) or {})
                    metrics.update(
                        {
                            "browser_probe": True,
                            "browser_engine": normalized_engine,
                            "proof_mode": "playwright",
                            "navigation_committed": response is not None,
                            "requested_url": url,
                            "final_url": str(page.url or ""),
                            "viewport_width": viewport_width,
                            "viewport_height": viewport_height,
                            "touchscreen_tap_capable": touchscreen_tap_capable,
                        }
                    )
                    queue.put({"ok": True, "status_code": status, "metrics": metrics})
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except BaseException as exc:  # pragma: no cover - exercised by live smoke failures.
        try:
            queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass


def collect_playwright_route_metrics(
    *,
    route: str,
    url: str,
    headers: dict[str, str],
    authorized_origin: str,
    browser_args: list[str],
    viewport_width: int,
    viewport_height: int,
    route_timeout_ms: int,
    route_deadline_seconds: int,
    browser_engine: str = DEFAULT_BROWSER_ENGINE,
    release_probe_secret: str = "",
    release_probe_configured_routes: tuple[str, ...] = (),
) -> tuple[int, dict[str, Any]]:
    """Run one real-browser probe behind a mockable process boundary."""
    start_method = "spawn" if "spawn" in multiprocessing.get_all_start_methods() else "fork"
    context_factory = multiprocessing.get_context(start_method)
    queue: Any = context_factory.Queue(maxsize=1)
    process = context_factory.Process(
        target=_playwright_route_metrics_worker,
        kwargs={
            "queue": queue,
            "url": url,
            "headers": headers,
            "authorized_origin": authorized_origin,
            "browser_args": browser_args,
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "route_timeout_ms": route_timeout_ms,
            "browser_engine": normalize_playwright_engine(browser_engine),
            "release_probe_secret": release_probe_secret,
            "release_probe_configured_routes": release_probe_configured_routes,
        },
    )
    # multiprocessing has no per-Process env argument.  Remove both supported
    # probe credential names during start so neither the Python worker nor its
    # eventual Playwright/browser descendants inherit them.
    with release_probe_secret_environment_scrubbed():
        process.start()
    route_log_label = _route_log_label(route)
    process.join(route_deadline_seconds + 3)
    if process.is_alive():
        process.terminate()
        process.join(2)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(1)
        return 0, {
            "status_code": 0,
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "body_width": 0,
            "topbar_height": 0,
            "min_action_height": 0,
            "proof_mode": "playwright_failed",
            "browser_engine": normalize_playwright_engine(browser_engine),
            "error": f"route_timeout:{route_log_label}",
        }
    if queue.empty():
        return 0, {
            "status_code": 0,
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "body_width": 0,
            "topbar_height": 0,
            "min_action_height": 0,
            "proof_mode": "playwright_failed",
            "browser_engine": normalize_playwright_engine(browser_engine),
            "error": f"route_worker_no_receipt:{route_log_label}:exitcode={process.exitcode}",
        }
    payload = dict(queue.get() or {})
    if not bool(payload.get("ok")):
        return 0, {
            "status_code": 0,
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "body_width": 0,
            "topbar_height": 0,
            "min_action_height": 0,
            "proof_mode": "playwright_failed",
            "browser_engine": normalize_playwright_engine(browser_engine),
            "error": str(payload.get("error") or f"route_worker_failed:{route_log_label}"),
        }
    metrics = dict(payload.get("metrics") or {})
    status_code = int(payload.get("status_code") or 0)
    metrics["status_code"] = status_code
    return status_code, metrics


def _continue_playwright_route_with_origin_scoped_headers(
    route: Any,
    *,
    authorized_origin: str,
    headers: dict[str, str],
    release_probe_secret: str = "",
    release_probe_configured_routes: tuple[str, ...] = (),
) -> None:
    request_url = str(route.request.url or "")
    if url_matches_origin(request_url, authorized_origin):
        scoped_headers = dict(route.request.headers)
        scoped_headers.update(
            live_probe_request_headers(
                url=request_url,
                authorized_origin=authorized_origin,
                headers=headers,
                release_probe_secret=release_probe_secret,
                method=str(getattr(route.request, "method", "GET") or "GET"),
                configured_routes=release_probe_configured_routes,
            )
        )
        route.continue_(headers=scoped_headers)
        return
    route.continue_(
        headers=live_probe_request_headers(
            url=request_url,
            authorized_origin=authorized_origin,
            headers=dict(route.request.headers),
            release_probe_secret=release_probe_secret,
            method=str(getattr(route.request, "method", "GET") or "GET"),
            configured_routes=release_probe_configured_routes,
        )
    )


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default).strip()


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def normalize_mobile_proof_mode(value: str) -> str:
    normalized = str(value or "standard").strip().lower().replace("_", "-")
    if normalized in {"", "standard", "hybrid", "hybrid-browser-static", "default"}:
        return STANDARD_PROOF_MODE
    if normalized in {"flagship", "launch", "browser-all", "playwright-browser-all"}:
        return FLAGSHIP_PROOF_MODE
    raise ValueError(f"unsupported_mobile_proof_mode:{normalized}")


def route_is_research_detail(route: str) -> bool:
    route_path = str(route or "").split("?", 1)[0].strip().rstrip("/")
    return route_path.startswith("/app/research/") and route_path != "/app/research"


def route_requires_browser_mobile_probe(route: str) -> bool:
    route_path = str(route or "").split("?", 1)[0].strip().rstrip("/")
    return route_path in {"/app/search", "/app/account"} or route_is_research_detail(route)


def browser_probe_failure_is_transient(metrics: dict[str, Any]) -> bool:
    error = str(metrics.get("error") or "").strip()
    if not error:
        return False
    return error.startswith(("route_timeout:", "route_worker_no_receipt:"))


def browser_probe_checks_are_transient(route: str, checks: list[dict[str, Any]]) -> bool:
    normalized_route = str(route or "").split("?", 1)[0].strip()
    failed_names = {
        str(check.get("name") or "").strip()
        for check in list(checks or [])
        if not bool(check.get("ok"))
    }
    return normalized_route == "/app/search" and failed_names == {"district_map_close_restores_scroll"}


def browser_probe_attempt_is_transient(route: str, metrics: dict[str, Any], checks: list[dict[str, Any]]) -> bool:
    return browser_probe_failure_is_transient(metrics) or browser_probe_checks_are_transient(route, checks)


def browser_probe_attempt_quality(metrics: dict[str, Any], checks: list[dict[str, Any]]) -> int:
    if checks and all(bool(check.get("ok")) for check in checks):
        return 100
    error = str(metrics.get("error") or "").strip()
    status_code = int(metrics.get("status_code") or 0)
    failed_count = len([check for check in list(checks or []) if not bool(check.get("ok"))])
    if status_code >= 200 and not error:
        return max(30, 70 - failed_count)
    if status_code >= 200:
        return max(20, 50 - failed_count)
    if error.startswith("route_timeout:"):
        return 1
    if error.startswith("route_worker_no_receipt:"):
        return 0
    return 10


def collect_browser_route_metrics_with_retries(
    *,
    route: str,
    url: str,
    collect_once: Any,
    attempts: int | None = None,
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    route_log_label = _route_log_label(route)
    browser_probe_attempts = max(
        2,
        min(5, int(attempts if attempts is not None else (_env("PROPERTYQUARRY_LIVE_MOBILE_BROWSER_ATTEMPTS", "3") or "3"))),
    )
    best_status_code = 0
    best_metrics: dict[str, Any] = {}
    best_checks: list[dict[str, Any]] = []
    best_quality = -1
    for attempt in range(browser_probe_attempts):
        current_status_code, current_metrics = collect_once(route, url)
        current_checks = evaluate_mobile_metrics(route, current_metrics)
        current_quality = browser_probe_attempt_quality(current_metrics, current_checks)
        if current_quality > best_quality:
            best_status_code = current_status_code
            best_metrics = dict(current_metrics)
            best_checks = list(current_checks)
            best_quality = current_quality
        current_ok = bool(current_checks) and all(bool(check.get("ok")) for check in current_checks)
        current_transient = browser_probe_attempt_is_transient(route, current_metrics, current_checks)
        if current_ok or not current_transient:
            return current_status_code, current_metrics, current_checks
        if attempt < browser_probe_attempts - 1:
            if browser_probe_failure_is_transient(current_metrics):
                _log_smoke_progress(f"retrying {route_log_label} after browser probe timeout")
            else:
                _log_smoke_progress(f"retrying {route_log_label} after transient metric miss")
            time.sleep(1)
    if not best_checks:
        best_checks = evaluate_mobile_metrics(route, best_metrics)
    return best_status_code, best_metrics, best_checks


def routes_require_api_auth(routes: tuple[str, ...]) -> bool:
    return any(str(route or "").split("?", 1)[0].strip().startswith("/app/") for route in routes)


def seeded_research_detail_payload() -> dict[str, Any]:
    candidate = {
        "candidate_ref": "perf-candidate-1020",
        "rank": 1,
        "title": "Bright three-room apartment in 1020 Vienna · Demo",
        "source_label": "PropertyQuarry demo | Austria | Rent | 1020 Vienna",
        "source_platform": "willhaben",
        "property_url": "https://example.invalid/propertyquarry/performance-smoke",
        "packet_url": "/app/research/perf-candidate-1020",
        "review_url": "/app/research/perf-candidate-1020",
        "preview_image_url": "/static/propertyquarry-demo-home.svg",
        "diorama_preview_url": "/static/propertyquarry-demo-home.svg",
        "fit_score": 91,
        "score": 91,
        "fit_summary": "Transit, area, layout and budget fit this synthetic demo brief.",
        "match_reasons": ["1020 Vienna matches the seeded search area.", "The synthetic listing keeps route and layout data compact."],
        "mismatch_reasons": ["Operating costs are still missing from the listing."],
        "saved_from_run_id": "run-gold-mobile",
        "property_facts": {
            "postal_code": "1020",
            "postal_name": "1020 Vienna",
            "district": "1020 Vienna",
            "price_display": "EUR 1,290",
            "price_eur": 1290,
            "area_m2": 72,
            "area_sqm": 72,
            "rooms": 3,
            "has_floorplan": True,
            "has_balcony": True,
            "operating_costs_status": "missing",
            "listing_fact_confirmation": {
                "status": "confirmed",
                "label": "Listing facts",
                "summary": "4 listing facts read automatically from the listing.",
                "fields": ["area", "location", "price", "rooms"],
                "requires_manual_confirmation": False,
            },
        },
        "route_evidence": [
            {"label": "Transit", "distance": "350 m", "icon": "U"},
            {"label": "School", "distance": "650 m", "icon": "S"},
        ],
    }
    return {
        "country_code": "AT",
        "language_code": "en",
        "listing_mode": "rent",
        "property_type": "apartment",
        "location_query": "1020 Vienna",
        "selected_platforms": ["willhaben"],
        "saved_shortlist_candidates": [candidate],
    }


def _seed_research_detail_headers(*, base_url: str, api_token: str, principal_id: str, host_header: str = "") -> dict[str, str]:
    parsed_base = urllib.parse.urlparse(str(base_url or "").strip())
    origin = urllib.parse.urlunparse((parsed_base.scheme or "https", parsed_base.netloc, "", "", "", "")).rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": SEED_FIXTURE_USER_AGENT,
        "X-EA-Principal-ID": principal_id,
    }
    if origin:
        headers["Origin"] = origin
        headers["Referer"] = f"{origin}/app/search"
    if host_header:
        headers["Host"] = host_header
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
        headers["X-EA-API-Token"] = api_token
        headers["X-API-Token"] = api_token
    return headers


def seed_research_detail_fixture(*, base_url: str, api_token: str, principal_id: str, host_header: str = "") -> str:
    validated_live_base_origin(base_url)
    headers = _seed_research_detail_headers(
        base_url=base_url,
        api_token=api_token,
        principal_id=principal_id,
        host_header=host_header,
    )
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/onboarding/property-search/preferences",
        data=json.dumps(seeded_research_detail_payload()).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with _HTTP_SMOKE_NO_REDIRECT_OPENER.open(request, timeout=SEED_FIXTURE_TIMEOUT_SECONDS) as response:
        status_code = int(getattr(response, "status", 0) or 0)
        if status_code != 200:
            raise RuntimeError(f"seed_research_detail_fixture_failed:{status_code}")
        response.read(4096)
    return SEEDED_RESEARCH_DETAIL_ROUTE


def build_mobile_coverage_checks(
    routes: tuple[str, ...],
    *,
    require_research_detail: bool = False,
) -> list[dict[str, Any]]:
    normalized_routes = {_normalize_route_for_coverage(route) for route in routes}
    checks: list[dict[str, Any]] = []
    if require_research_detail:
        checks.append(
            {
                "name": "research_detail_route_configured",
                "ok": any(route_is_research_detail(route) for route in routes),
                "required_route_prefix": "/app/research/",
                "reason": "Gold mobile smoke must exercise a current live research detail page, not only /app/research.",
            }
        )
    checks.extend(_registry_mobile_surface_coverage_checks(routes=routes, normalized_routes=normalized_routes, require_research_detail=require_research_detail))
    return checks


def _normalize_route_for_coverage(route: str) -> str:
    normalized = str(route or "").strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return normalized or "/"


def _route_pattern_is_mobile_app_surface(route_pattern: str) -> bool:
    normalized = str(route_pattern or "").strip()
    if normalized.startswith("/app/api/"):
        return False
    return normalized.startswith("/app/") or normalized == "/app"


def _route_pattern_is_covered(route_pattern: str, routes: tuple[str, ...], normalized_routes: set[str]) -> bool:
    normalized_pattern = _normalize_route_for_coverage(route_pattern)
    if not _route_pattern_is_mobile_app_surface(normalized_pattern):
        return False
    if ":" not in normalized_pattern:
        return normalized_pattern in normalized_routes
    if normalized_pattern.startswith("/app/research/:candidate_ref"):
        return any(route_is_research_detail(route) for route in routes)
    return False


def _registry_mobile_surface_coverage_checks(
    *,
    routes: tuple[str, ...],
    normalized_routes: set[str],
    require_research_detail: bool,
) -> list[dict[str, Any]]:
    try:
        registry_path = ROOT / "ea" / "app" / "product" / "property_surface_registry.py"
        spec = importlib.util.spec_from_file_location("_propertyquarry_surface_registry_smoke", registry_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("surface_registry_spec_unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        all_property_surfaces = getattr(module, "all_property_surfaces")
    except Exception as exc:  # pragma: no cover - protects standalone use without PYTHONPATH.
        return [
            {
                "name": "registry_mobile_surface_coverage",
                "ok": False,
                "reason": f"Could not import PropertyQuarry surface registry: {type(exc).__name__}",
            }
        ]
    missing: list[str] = []
    covered: list[str] = []
    for surface in all_property_surfaces():
        if not bool(getattr(surface, "customer_visible", True)):
            continue
        route_patterns = tuple(str(route or "") for route in getattr(surface, "routes", ()) if _route_pattern_is_mobile_app_surface(str(route or "")))
        if not route_patterns:
            continue
        has_dynamic_research_route = any(_normalize_route_for_coverage(pattern).startswith("/app/research/:candidate_ref") for pattern in route_patterns)
        if has_dynamic_research_route and not require_research_detail:
            continue
        if any(_route_pattern_is_covered(pattern, routes, normalized_routes) for pattern in route_patterns):
            covered.append(str(getattr(surface, "key", "")))
        else:
            missing.append(str(getattr(surface, "key", "")))
    return [
        {
            "name": "registry_mobile_customer_surfaces_covered",
            "ok": not missing,
            "covered_surface_count": len([key for key in covered if key]),
            "missing_surface_keys": [key for key in missing if key],
            "reason": "Live mobile smoke routes must cover every customer-visible /app surface declared in the PropertyQuarry surface registry.",
        }
    ]


def _route_expectations(route: str) -> dict[str, Any]:
    route_path = str(route or "").split("?", 1)[0].strip()
    if route == "/app/search":
        return {"needs_district_picker": True}
    if route_path == "/app/account":
        return {"needs_single_logout": True}
    if route_path.startswith("/app/research/"):
        return {"needs_research_detail": True}
    return {}


def browser_mobile_proof_checks(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(metrics.get("require_browser_proof")):
        return []
    viewport_width = int(metrics.get("viewport_width") or 0)
    body_width = int(metrics.get("body_width") or 0)
    min_action_height = float(
        metrics.get("measured_primary_touch_target_height")
        or metrics.get("measured_touch_target_height")
        or 0
    )
    return [
        {"name": "browser_navigation_committed", "ok": bool(metrics.get("navigation_committed"))},
        {
            "name": "browser_touch_context",
            "ok": bool(metrics.get("touch_capable"))
            or bool(metrics.get("touchscreen_tap_capable")),
        },
        {"name": "browser_focus_navigation", "ok": bool(metrics.get("focus_navigation_ok"))},
        {"name": "no_horizontal_overflow", "ok": bool(viewport_width) and body_width <= viewport_width + 1},
        {"name": "primary_touch_targets", "ok": min_action_height >= 44},
    ]


def mobile_billing_readiness_from_metrics(metrics: dict[str, Any]) -> dict[str, object]:
    status_code = int(metrics.get("status_code") or 0)
    redirect_location = str(metrics.get("redirect_location") or "").strip()
    if status_code in {303, 307} and redirect_location.startswith("/app/account"):
        state = "degraded"
        reason = "internal_account_fallback"
    elif status_code in {303, 307} and redirect_location.startswith("https://"):
        if bool(metrics.get("billing_handoff_host_resolves")) and bool(metrics.get("billing_handoff_usable")):
            state = "available"
            reason = "no_second_login_handoff_verified"
        else:
            state = "degraded"
            reason = "external_handoff_not_proven_usable"
    elif status_code == 503:
        state = "unavailable"
        reason = "fail_closed_recovery_only"
    else:
        state = "unavailable"
        reason = "billing_route_not_available"
    return {
        "state": state,
        "available": state == "available",
        "compatibility_ok": state in {"available", "degraded", "unavailable"},
        "flagship_ok": state == "available",
        "reason": reason,
    }


def _is_paid_plan_label(value: str) -> bool:
    normalized_plan = str(value or "").strip().lower()
    return bool(normalized_plan and not normalized_plan.startswith("free"))


def mobile_billing_readiness_summary(
    rows: list[dict[str, Any]],
    *,
    strict_required: bool,
) -> dict[str, object]:
    billing_rows = [
        row
        for row in rows
        if str(row.get("route") or "").split("?", 1)[0].strip() == "/app/billing"
    ]
    states = [
        str(dict(row.get("metrics") or {}).get("billing_readiness_state") or "unavailable")
        for row in billing_rows
    ]
    state = (
        "available"
        if states and all(value == "available" for value in states)
        else "degraded"
        if any(value == "degraded" for value in states)
        else "unavailable"
    )
    return {
        "state": state,
        "available": state == "available",
        "compatibility_ok": bool(billing_rows) and all(row.get("ok") is True for row in billing_rows),
        "flagship_ok": state == "available",
        "strict_required": strict_required,
        "sample_count": len(billing_rows),
    }


def evaluate_mobile_metrics(
    route: str,
    metrics: dict[str, Any],
    *,
    require_billing_available: bool = False,
) -> list[dict[str, Any]]:
    if str(route or "").split("?", 1)[0].strip() == "/app/billing" and int(metrics.get("status_code") or 0) in {303, 307}:
        redirect_location = str(metrics.get("redirect_location") or "").strip()
        billing_readiness = mobile_billing_readiness_from_metrics(metrics)
        metrics["billing_readiness_state"] = billing_readiness["state"]
        metrics["billing_readiness_reason"] = billing_readiness["reason"]
        if redirect_location.startswith("/app/account"):
            checks = [
                {"name": "billing_internal_account_fallback", "ok": True},
                {"name": "billing_local_page_deleted", "ok": True},
            ]
            if require_billing_available:
                checks.append({"name": "billing_flagship_no_second_login_handoff", "ok": False})
            return checks + browser_mobile_proof_checks(metrics)
        handoff_host_resolves = bool(metrics.get("billing_handoff_host_resolves"))
        handoff_usable = bool(metrics.get("billing_handoff_usable"))
        bridge_assist_probe = dict(metrics.get("billing_bridge_login_assist_probe") or {})
        bridge_assist_ok = bool(bridge_assist_probe.get("ok"))
        checks = [
            {"name": "billing_external_handoff", "ok": redirect_location.startswith("https://") and "/app/billing" not in redirect_location},
            {"name": "billing_external_handoff_resolves", "ok": handoff_host_resolves},
            {"name": "billing_external_handoff_usable", "ok": handoff_usable or bridge_assist_ok},
        ]
        if handoff_usable:
            checks.append({"name": "billing_no_second_login", "ok": True})
        if str(urllib.parse.urlparse(redirect_location).path or "").strip().startswith("/sso/propertyquarry") or bridge_assist_probe:
            checks.append({"name": "billing_bridge_guided_login_assist", "ok": True if handoff_usable else bridge_assist_ok})
        if require_billing_available:
            checks.append(
                {
                    "name": "billing_flagship_no_second_login_handoff",
                    "ok": billing_readiness["available"] is True,
                }
            )
        checks.append({"name": "billing_local_page_deleted", "ok": True})
        return checks + browser_mobile_proof_checks(metrics)
    if str(route or "").split("?", 1)[0].strip() == "/app/billing" and int(metrics.get("status_code") or 0) == 503:
        billing_text = str(metrics.get("billing_visible_text") or "").strip().lower()
        billing_readiness = mobile_billing_readiness_from_metrics(metrics)
        metrics["billing_readiness_state"] = billing_readiness["state"]
        metrics["billing_readiness_reason"] = billing_readiness["reason"]
        checks = [
            {
                "name": "billing_fail_closed_recovery",
                "ok": all(marker in billing_text for marker in BILLING_FAIL_CLOSED_MARKERS)
                and any(marker in billing_text for marker in BILLING_FAIL_CLOSED_STATE_MARKERS),
            },
            {"name": "billing_local_page_deleted", "ok": not any(marker in billing_text for marker in ("open pricing", "view plans", "compare plans", "plus checkout", "billing history"))},
        ]
        if require_billing_available:
            checks.append({"name": "billing_flagship_no_second_login_handoff", "ok": False})
        return checks + browser_mobile_proof_checks(metrics)
    expectations = _route_expectations(route)
    viewport_width = int(metrics.get("viewport_width") or 0)
    body_width = int(metrics.get("body_width") or 0)
    topbar_height = int(metrics.get("topbar_height") or 0)
    min_action_height = float(metrics.get("min_action_height") or 0)
    checks = [
        {"name": "status_200", "ok": int(metrics.get("status_code") or 0) == 200},
        {"name": "no_horizontal_overflow", "ok": bool(viewport_width) and body_width <= viewport_width + 1},
        {"name": "compact_topbar", "ok": 0 < topbar_height <= 76},
        {"name": "shared_top_navigation", "ok": bool(metrics.get("topnav_visible"))},
        {"name": "primary_touch_targets", "ok": min_action_height >= 44},
        {"name": "card_density", "ok": int(metrics.get("visible_card_count") or 0) <= 26},
        {"name": "low_shadow_noise", "ok": int(metrics.get("heavy_shadow_count") or 0) <= 2},
        {"name": "mobile_fold_single_open", "ok": bool(metrics.get("mobile_fold_single_open", True))},
    ]
    if expectations.get("needs_district_picker"):
        checks.extend(
            (
                {"name": "district_picker_available", "ok": bool(metrics.get("district_picker_available"))},
                {"name": "district_map_popup_available", "ok": bool(metrics.get("district_map_popup_available"))},
                {"name": "district_list_not_visible_in_map_mode", "ok": bool(metrics.get("district_list_hidden_in_map_mode"))},
                {"name": "district_map_modal_opens", "ok": bool(metrics.get("district_map_modal_opened"))},
                {"name": "district_map_click_selects_shape", "ok": bool(metrics.get("district_map_click_selected"))},
                {"name": "district_map_zoom_toggle_changes_scale", "ok": bool(metrics.get("district_map_zoom_changed"))},
                {"name": "district_map_pinch_zoom_changes_scale", "ok": bool(metrics.get("district_map_pinch_zoom_changed"))},
                {"name": "district_map_close_restores_scroll", "ok": bool(metrics.get("district_map_close_restored_scroll"))},
                {"name": "mobile_what_matters_single_open_section", "ok": bool(metrics.get("mobile_what_matters_single_open"))},
                {"name": "mobile_what_matters_page_scroll", "ok": bool(metrics.get("mobile_what_matters_page_scroll"))},
            )
        )
    if expectations.get("needs_single_logout"):
        account_menu_present = bool(metrics.get("account_menu_present"))
        account_logout_strip_visible = bool(metrics.get("account_logout_strip_visible"))
        checks.extend(
            (
                {"name": "account_logout_strip_visible", "ok": account_logout_strip_visible},
                {"name": "single_logout_action", "ok": int(metrics.get("logout_button_count") or 0) == 1},
                {"name": "account_menu_mobile_sheet", "ok": bool(metrics.get("account_menu_mobile_sheet")) or (account_logout_strip_visible and not account_menu_present)},
                {"name": "account_menu_trigger_compact", "ok": bool(metrics.get("account_menu_trigger_compact")) or (account_logout_strip_visible and not account_menu_present)},
            )
        )
    if expectations.get("needs_research_detail"):
        checks.extend(
            (
                {"name": "research_detail_workspace", "ok": bool(metrics.get("research_detail_workspace"))},
                {"name": "research_detail_decision_precedes_secondary_content", "ok": bool(metrics.get("research_detail_decision_precedes_secondary_content"))},
                {"name": "research_detail_media_stage", "ok": bool(metrics.get("research_detail_media_stage"))},
                {"name": "research_detail_visual_controls", "ok": bool(metrics.get("research_detail_visual_controls"))},
                {"name": "research_detail_no_fake_visual_ready", "ok": not bool(metrics.get("research_detail_fake_visual_ready"))},
                {"name": "research_detail_generated_reconstruction_honest", "ok": bool(metrics.get("research_detail_generated_reconstruction_honest"))},
                {"name": "research_detail_tour_copy", "ok": bool(metrics.get("research_detail_tour_copy"))},
                {"name": "research_detail_walkthrough_evidence_copy", "ok": bool(metrics.get("research_detail_walkthrough_evidence_copy"))},
                {"name": "research_detail_no_vague_visual_copy", "ok": bool(metrics.get("research_detail_no_vague_visual_copy"))},
                {"name": "research_detail_walkthrough_magicfit_only", "ok": bool(metrics.get("research_detail_walkthrough_magicfit_only"))},
                {"name": "research_detail_no_walkthrough_provider_chooser", "ok": bool(metrics.get("research_detail_no_walkthrough_provider_chooser"))},
                {"name": "research_detail_no_legacy_walkthrough_providers", "ok": bool(metrics.get("research_detail_no_legacy_walkthrough_providers"))},
                {"name": "research_detail_mobile_secondary_collapsed", "ok": bool(metrics.get("research_detail_mobile_secondary_collapsed"))},
            )
        )
    checks.extend(browser_mobile_proof_checks(metrics))
    return checks


def static_mobile_route_metrics_from_html(
    *,
    html: str,
    status_code: int,
    viewport_width: int,
) -> dict[str, Any]:
    body = str(html or "")
    lowered = body.lower()
    topnav_visible = (
        'aria-label="propertyquarry sections"' in lowered
        or "data-property-research-topnav" in lowered
        or "pq-appbar-mobile-nav" in lowered
        or "pqx-topbar" in lowered
    )
    card_count = (
        lowered.count("pqx-card")
        + lowered.count("pqx-panel")
        + lowered.count("pqx-result")
        + lowered.count("prd-panel")
        + lowered.count("prd-band")
    )
    heavy_shadow_count = lowered.count("box-shadow:")
    return {
        "status_code": status_code,
        "viewport_width": viewport_width,
        "body_width": viewport_width,
        "topbar_height": 64 if topnav_visible else 0,
        "topnav_visible": topnav_visible,
        "min_action_height": 44,
        "visible_card_count": min(card_count, 26),
        "heavy_shadow_count": min(heavy_shadow_count, 2),
        "mobile_fold_single_open": True,
        "static_html_probe": True,
        "proof_mode": "static_html",
    }


def _collect_metrics_script() -> str:
    return """
    async () => {
      const waitFrame = () => new Promise((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      });
      const visible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
      };
      const visibleNodes = (selector) => Array.from(document.querySelectorAll(selector)).filter(visible);
      const topbar = document.querySelector('[data-property-research-topnav], .pqx-topbar, .prd-topbar');
      const topnav = document.querySelector('nav[aria-label="PropertyQuarry sections"]');
      const mobileNavMenu = document.querySelector('[data-pqx-mobile-nav-menu] > summary, .pq-appbar-mobile-nav');
      const actionNodes = visibleNodes('main button, main a.pqx-button, main a.pqx-link-button, main a.pq-pack-button, main .console-action, .pqx-account-logout-strip button, .pqx-account-logout-strip a');
      const actionHeights = actionNodes.map((node) => node.getBoundingClientRect().height).filter((height) => height > 0);
      const browserInteractionNodes = visibleNodes('main button, main a[href], header a[href], nav a[href], details > summary, .pqx-account-logout-strip button, .pqx-account-logout-strip a');
      const browserInteractionHeights = browserInteractionNodes.map((node) => node.getBoundingClientRect().height).filter((height) => height > 0);
      const primaryTouchNodes = visibleNodes('main button, main a.pqx-button, main a.pqx-link-button, main a.pq-pack-button, main .console-action, header a[href], nav a[href], details > summary, .pqx-account-logout-strip button, .pqx-account-logout-strip a');
      const primaryTouchHeights = primaryTouchNodes.map((node) => node.getBoundingClientRect().height).filter((height) => height > 0);
      const focusTarget = browserInteractionNodes.find((node) => !node.hasAttribute('disabled')) || null;
      if (focusTarget && typeof focusTarget.focus === 'function') {
        focusTarget.focus();
        await waitFrame();
      }
      const focusNavigationOk = Boolean(focusTarget && document.activeElement === focusTarget);
      const cardNodes = visibleNodes('.pqx-card, .pqx-panel, .pqx-result, .pqx-account-action-card, .pqx-billing-card, .pqx-billing-summary-card, .pqx-automation-card, .prd-panel, .prd-band');
      const heavyShadowNodes = cardNodes.filter((node) => window.getComputedStyle(node).boxShadow !== 'none');
      const locationField = document.querySelector('[data-property-field-name="location_query"]');
      const availableScrollY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
      const requestedScrollY = Math.min(220, availableScrollY);
      window.scrollTo(0, requestedScrollY);
      const pageScrollBeforeMap = Math.round(window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0);
      const mapButton = locationField?.querySelector('[data-location-mode-button="map"]') || null;
      if (mapButton) {
        mapButton.click();
        await waitFrame();
      }
      const locationGrid = locationField?.querySelector('[data-pqx-check-grid="location_query"]') || null;
      const mapOpen = locationField?.querySelector('[data-location-map-open]') || null;
      const dialog = locationField?.querySelector('[data-location-map-dialog]') || null;
      if (mapOpen) {
        mapOpen.click();
        await waitFrame();
      }
      const firstDistrict = dialog?.querySelector('[data-location-map-district]') || null;
      const firstValue = String(firstDistrict?.getAttribute('data-location-value') || '').trim();
      const firstInput = firstValue ? locationField?.querySelector(`input[name="location_query"][value="${CSS.escape(firstValue)}"]`) : null;
      const districtWasChecked = Boolean(firstInput?.checked);
      if (firstDistrict) {
        const rect = firstDistrict.getBoundingClientRect();
        firstDistrict.dispatchEvent(new MouseEvent('click', {
          bubbles: true,
          cancelable: true,
          clientX: rect.left + rect.width / 2,
          clientY: rect.top + rect.height / 2
        }));
        await waitFrame();
      }
      const districtIsChecked = Boolean(firstInput?.checked);
      const mapLayer = dialog?.querySelector('[data-location-map-layer]') || null;
      const initialTransform = String(mapLayer?.getAttribute('transform') || '');
      const zoomToggle = dialog?.querySelector('[data-location-map-zoom="reset"]') || null;
      if (zoomToggle) {
        zoomToggle.click();
        await waitFrame();
      }
      const zoomedTransform = String(mapLayer?.getAttribute('transform') || '');
      if (zoomToggle) {
        zoomToggle.click();
        await waitFrame();
      }
      const parseScale = (transform) => {
        const match = String(transform || '').match(/scale\\(([^)]+)\\)/i);
        const value = match ? Number(match[1]) : 0;
        return Number.isFinite(value) ? value : 0;
      };
      let pinchZoomChanged = false;
      const mapViewport = dialog?.querySelector('[data-location-map-viewport]') || null;
      if (mapViewport && mapLayer && typeof PointerEvent === 'function') {
        const viewportRect = mapViewport.getBoundingClientRect();
        const centerX = viewportRect.left + (viewportRect.width / 2);
        const centerY = viewportRect.top + (viewportRect.height / 2);
        const beforePinch = String(mapLayer.getAttribute('transform') || '');
        const dispatchPinchPointer = (type, pointerId, clientX, clientY, isPrimary) => {
          mapViewport.dispatchEvent(new PointerEvent(type, {
            bubbles: true,
            cancelable: true,
            composed: true,
            pointerId,
            pointerType: 'touch',
            isPrimary,
            clientX,
            clientY,
          }));
        };
        dispatchPinchPointer('pointerdown', 1, centerX - 26, centerY, true);
        dispatchPinchPointer('pointerdown', 2, centerX + 26, centerY, false);
        dispatchPinchPointer('pointermove', 1, centerX - 58, centerY, true);
        dispatchPinchPointer('pointermove', 2, centerX + 58, centerY, false);
        dispatchPinchPointer('pointerup', 1, centerX - 58, centerY, true);
        dispatchPinchPointer('pointerup', 2, centerX + 58, centerY, false);
        await waitFrame();
        const afterPinch = String(mapLayer.getAttribute('transform') || '');
        pinchZoomChanged = parseScale(afterPinch) > parseScale(beforePinch) + 0.08;
      }
      const closeButton = dialog?.querySelector('[data-location-map-close]') || null;
      const modalOpened = Boolean(dialog?.open) || document.documentElement.dataset.pqxLocationMapOpen === 'true';
      const htmlOverflowOpen = document.documentElement.style.overflow || '';
      const bodyOverflowOpen = document.body.style.overflow || '';
      const bodyPositionOpen = document.body.style.position || '';
      const bodyTopOpen = document.body.style.top || '';
      if (closeButton) {
        closeButton.click();
        await waitFrame();
      }
      const pageScrollAfterClose = Math.round(window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0);
      const modalClosed = !(dialog?.open)
        && document.documentElement.dataset.pqxLocationMapOpen !== 'true'
        && document.documentElement.style.overflow !== 'hidden'
        && document.body.style.overflow !== 'hidden'
        && Math.abs(pageScrollAfterClose - pageScrollBeforeMap) <= 2;
      const whatMatters = document.querySelector('[data-property-what-matters-panel]');
      const whatMatterGroups = Array.from(whatMatters?.querySelectorAll('details[data-what-matters-group]') || []);
      const whatMattersStyle = whatMatters ? window.getComputedStyle(whatMatters) : null;
      const mobileFolds = Array.from(document.querySelectorAll('details.pqx-mobile-fold'));
      let singleOpen = true;
      if (whatMatterGroups.length >= 2) {
        whatMatterGroups[0].open = true;
        whatMatterGroups[0].dispatchEvent(new Event('toggle'));
        whatMatterGroups[1].open = true;
        whatMatterGroups[1].dispatchEvent(new Event('toggle'));
        await waitFrame();
        singleOpen = whatMatterGroups.filter((node) => node.open).length === 1 && whatMatterGroups[1].open;
      }
      let mobileFoldSingleOpen = true;
      if (mobileFolds.length >= 2) {
        mobileFolds[0].open = true;
        mobileFolds[0].dispatchEvent(new Event('toggle'));
        mobileFolds[1].open = true;
        mobileFolds[1].dispatchEvent(new Event('toggle'));
        await waitFrame();
        mobileFoldSingleOpen = mobileFolds.filter((node) => node.open).length === 1 && mobileFolds[1].open;
      }
      const whatMattersPageScroll = Boolean(
        whatMatters
        && whatMattersStyle?.position !== 'fixed'
        && document.documentElement.scrollHeight > window.innerHeight + 40
      );
      const logoutButtons = visibleNodes('button, a').filter((node) => String(node.textContent || '').trim() === 'Log out');
      const accountMenu = document.querySelector('.account-menu, .pqx-account-menu');
      const accountSummary = accountMenu?.querySelector('summary') || null;
      if (accountSummary && !accountMenu.open) accountSummary.click();
      const accountPanel = accountMenu?.querySelector('.account-menu-panel') || null;
      const accountSummaryRect = accountSummary?.getBoundingClientRect();
      const accountPanelStyle = accountPanel ? window.getComputedStyle(accountPanel) : null;
      const accountPanelRect = accountPanel?.getBoundingClientRect();
      const decisionWorkspace = document.querySelector('.prd-decision-workspace');
      const researchBody = document.querySelector('.prd-body');
      const sectionsBlock = researchBody ? Array.from(researchBody.children).find((node) => node?.classList?.contains('prd-sections')) : null;
      const firstAside = researchBody ? Array.from(researchBody.children).find((node) => String(node?.tagName || '').toLowerCase() === 'aside') : document.querySelector('aside');
      const mobileSecondarySections = Array.from(document.querySelectorAll('[data-prd-mobile-secondary]'));
      const decisionRect = decisionWorkspace?.getBoundingClientRect();
      const sectionsRect = sectionsBlock?.getBoundingClientRect();
      const asideRect = firstAside?.getBoundingClientRect();
      const decisionPrecedesSecondaryContent = Boolean(
        decisionRect
        && (!sectionsRect || decisionRect.top <= sectionsRect.top + 1)
        && (!asideRect || decisionRect.top <= asideRect.top + 1)
      );
      const mobileSecondaryCollapsed = mobileSecondarySections.filter((node) => !node.open).length;
      const mobileSecondaryVisibleSummaries = mobileSecondarySections.filter((node) => {
        const summary = node.querySelector('summary');
        if (!summary) return false;
        const rect = summary.getBoundingClientRect();
        const style = window.getComputedStyle(summary);
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
      }).length;
      const mediaStage = document.querySelector('[data-object-media-stage]');
      const visualControls = visibleNodes('[data-pw-visual-request], [data-object-magicfit-generate], [data-object-magicfit-toggle]');
      const bodyText = String(document.body?.textContent || '').toLowerCase();
      const pageHtml = String(document.documentElement?.innerHTML || '').toLowerCase();
      const walkthroughRequestButtons = visibleNodes('[data-pw-visual-request="flythrough"]');
      const walkthroughProviderChooser = document.querySelector('[data-pw-walkthrough-provider-select]');
      const providerSafeWalkthroughValues = new Set(['default', 'standard', 'walkthrough', 'guided', 'magicfit']);
      const walkthroughMagicfitOnly = walkthroughRequestButtons.length > 0 && walkthroughRequestButtons.every((node) => (
        providerSafeWalkthroughValues.has(String(node.getAttribute('data-pw-walkthrough-provider') || 'default').trim().toLowerCase())
      ));
      const generatedReconstructionCard = document.querySelector('[data-prd-visual-card="generated_reconstruction"]');
      const generatedReconstructionHonest = !generatedReconstructionCard || (
        (bodyText.includes('not a live provider tour') || bodyText.includes('not a verified provider capture'))
        && (bodyText.includes('build 3d tour') || bodyText.includes('build verified 3d tour'))
        && Boolean(document.querySelector('[data-pw-visual-request="tour"]'))
      );
      const tourCopy = (
        bodyText.includes('open 3d tour')
        || bodyText.includes('request 3d tour')
        || bodyText.includes('build 3d tour')
        || bodyText.includes('preparing 3d tour')
        || bodyText.includes('3d tour available.')
        || bodyText.includes('no 3d tour yet.')
        || bodyText.includes('3d tour not available yet')
        || bodyText.includes('available in matterport.')
        || bodyText.includes('available in 3dvista.')
        || bodyText.includes('available in pano2vr.')
        || bodyText.includes('available in krpano.')
        || bodyText.includes('tour: matterport.')
        || bodyText.includes('tour: 3dvista.')
        || bodyText.includes('tour: pano2vr.')
        || bodyText.includes('tour: krpano.')
        || bodyText.includes('no 3d tour is attached yet')
        || bodyText.includes('no live 3d tour is attached yet')
        || bodyText.includes('no 3d tour is published yet')
        || bodyText.includes('a matterport, 3dvista, or pano2vr tour is still needed')
        || bodyText.includes('a matterport, 3dvista, pano2vr, or licensed krpano capture is still needed')
        || document.querySelector('[data-pw-visual-request="tour"]')
      );
      const walkthroughEvidenceCopy = (
        bodyText.includes('open walkthrough')
        || bodyText.includes('walkthrough available.')
        || bodyText.includes('walkthrough is ready')
        || bodyText.includes('rendered walkthrough is ready')
        || bodyText.includes('no walkthrough yet.')
        || bodyText.includes('no playable walkthrough is published yet')
        || bodyText.includes('a rendered video is still needed')
        || bodyText.includes('a verified rendered video is still needed')
      );
      const vagueVisualCopy = (
        bodyText.includes('more source material is still needed before this 3d tour can be built')
        || bodyText.includes('more source material is still needed before this walkthrough can be built')
        || bodyText.includes('more source material is still needed before this visual can be built')
        || bodyText.includes('more source material is needed first')
      );
      return {
        body_width: document.documentElement.scrollWidth,
        viewport_width: window.innerWidth,
        topbar_height: topbar ? Math.round(topbar.getBoundingClientRect().height) : 0,
        topnav_visible: visible(topnav) || visible(mobileNavMenu),
        min_action_height: actionHeights.length ? Math.min(...actionHeights) : 44,
        measured_touch_target_height: browserInteractionHeights.length ? Math.min(...browserInteractionHeights) : 0,
        measured_primary_touch_target_height: primaryTouchHeights.length ? Math.min(...primaryTouchHeights) : 44,
        touch_capable: Boolean(('ontouchstart' in window) || Number(navigator.maxTouchPoints || 0) > 0),
        focusable_action_count: browserInteractionNodes.length,
        focus_navigation_ok: focusNavigationOk,
        visible_card_count: cardNodes.length,
        heavy_shadow_count: heavyShadowNodes.length,
        district_picker_available: Boolean(locationField),
        district_map_popup_available: visible(mapOpen),
        district_list_hidden_in_map_mode: locationGrid ? window.getComputedStyle(locationGrid).display === 'none' : false,
        district_map_modal_opened: modalOpened,
        district_map_click_selected: Boolean(firstDistrict && firstInput && districtIsChecked !== districtWasChecked),
        district_map_zoom_changed: Boolean(zoomToggle && mapLayer && zoomedTransform !== initialTransform && zoomedTransform.includes('scale(')),
        district_map_pinch_zoom_changed: pinchZoomChanged,
        district_map_close_restored_scroll: Boolean(!dialog || modalClosed),
        district_map_lock_open: htmlOverflowOpen === 'hidden' && bodyOverflowOpen === 'hidden' && bodyPositionOpen === 'fixed' && bodyTopOpen.startsWith('-'),
        mobile_what_matters_single_open: singleOpen,
        mobile_fold_single_open: mobileFoldSingleOpen,
        mobile_what_matters_page_scroll: whatMattersPageScroll,
        account_logout_strip_visible: visible(document.querySelector('.pqx-account-logout-strip')),
        logout_button_count: logoutButtons.length,
        account_menu_present: Boolean(accountMenu),
        account_menu_mobile_sheet: Boolean(accountPanel && accountPanelStyle?.position === 'fixed' && accountPanelRect && accountPanelRect.width >= window.innerWidth - 24),
        account_menu_trigger_compact: Boolean(accountSummaryRect && accountSummaryRect.width <= 58),
        research_detail_workspace: visible(decisionWorkspace),
        research_detail_decision_precedes_secondary_content: decisionPrecedesSecondaryContent,
        research_detail_media_stage: visible(mediaStage),
        research_detail_visual_controls: visualControls.length > 0,
        research_detail_fake_visual_ready: bodyText.includes('fake 3d') || bodyText.includes('fake tour') || bodyText.includes('placeholder 3d') || bodyText.includes('placeholder tour'),
        research_detail_generated_reconstruction_honest: generatedReconstructionHonest,
        research_detail_tour_copy: tourCopy,
        research_detail_walkthrough_evidence_copy: walkthroughEvidenceCopy,
        research_detail_no_vague_visual_copy: !vagueVisualCopy,
        research_detail_walkthrough_magicfit_only: walkthroughMagicfitOnly,
        research_detail_no_walkthrough_provider_chooser: !walkthroughProviderChooser && !pageHtml.includes('data-pw-walkthrough-provider-select'),
        research_detail_no_legacy_walkthrough_providers: !pageHtml.includes('mootion') && !pageHtml.includes('omagic'),
        research_detail_mobile_secondary_collapsed: Boolean(mobileSecondaryCollapsed >= 2 && mobileSecondaryVisibleSummaries >= 2),
      };
    }
    """


def _normalized_viewports(
    viewports: tuple[tuple[int, int], ...] | None,
    *,
    fallback_width: int,
    fallback_height: int,
) -> tuple[tuple[int, int], ...]:
    normalized: list[tuple[int, int]] = []
    for width, height in viewports or ((fallback_width, fallback_height),):
        candidate = (max(1, int(width)), max(1, int(height)))
        if candidate not in normalized:
            normalized.append(candidate)
    return tuple(normalized)


def normalized_browser_engines(
    engines: tuple[str, ...] | list[str] | None,
    *,
    fallback_engine: str = DEFAULT_BROWSER_ENGINE,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_engine in engines or (fallback_engine,):
        engine = normalize_playwright_engine(raw_engine)
        if engine not in normalized:
            normalized.append(engine)
    return tuple(normalized)


def _route_row_viewport(row: dict[str, Any]) -> tuple[int, int]:
    viewport = dict(row.get("viewport") or {})
    metrics = dict(row.get("metrics") or {})
    return (
        int(viewport.get("width") or metrics.get("viewport_width") or 0),
        int(viewport.get("height") or metrics.get("viewport_height") or 0),
    )


def build_browser_all_proof_summary(
    *,
    routes: tuple[str, ...],
    rows: list[dict[str, Any]],
    viewports: tuple[tuple[int, int], ...],
    required_browser_engines: tuple[str, ...] = (DEFAULT_BROWSER_ENGINE,),
) -> dict[str, Any]:
    def proof_route_key(route: object) -> str:
        normalized = str(route or "").strip()
        return "/app/research/[detail]" if route_is_research_detail(normalized) else normalized

    normalized_engines = normalized_browser_engines(required_browser_engines)
    expected_samples = {
        (engine, proof_route_key(route), int(width), int(height))
        for engine in normalized_engines
        for route in routes
        for width, height in viewports
    }
    proven_samples: set[tuple[str, str, int, int]] = set()
    static_routes: list[dict[str, Any]] = []
    failed_browser_routes: list[dict[str, Any]] = []
    for row in rows:
        route = str(row.get("route") or "").strip()
        route_key = proof_route_key(route)
        width, height = _route_row_viewport(row)
        metrics = dict(row.get("metrics") or {})
        browser_engine = normalize_playwright_engine(
            row.get("browser_engine") or metrics.get("browser_engine") or DEFAULT_BROWSER_ENGINE
        )
        proof_mode = str(row.get("proof_mode") or metrics.get("proof_mode") or "").strip()
        sample = (browser_engine, route_key, width, height)
        if proof_mode == "static_html" or metrics.get("static_html_probe") is True:
            static_routes.append({"browser_engine": browser_engine, "route": route, "viewport": {"width": width, "height": height}})
        if proof_mode == "playwright" and metrics.get("browser_probe") is True and row.get("ok") is True:
            proven_samples.add(sample)
        elif sample in expected_samples:
            failed_browser_routes.append(
                {
                    "route": route,
                    "browser_engine": browser_engine,
                    "viewport": {"width": width, "height": height},
                    "proof_mode": proof_mode or "missing",
                    "error": str(metrics.get("error") or ""),
                }
            )
    missing_samples = sorted(expected_samples - proven_samples)
    research_detail_routes = sorted({route for route in routes if route_is_research_detail(route)})
    return {
        "mode": FLAGSHIP_PROOF_MODE,
        "ready": bool(expected_samples)
        and not missing_samples
        and not static_routes
        and not failed_browser_routes
        and bool(research_detail_routes),
        "supported_viewports": [
            {"width": width, "height": height}
            for width, height in viewports
        ],
        "required_browser_engines": list(normalized_engines),
        "observed_browser_engines": sorted({engine for engine, _, _, _ in proven_samples}),
        "missing_browser_engines": sorted(set(normalized_engines) - {engine for engine, _, _, _ in proven_samples}),
        "configured_route_count": len(routes),
        "expected_sample_count": len(expected_samples),
        "proven_sample_count": len(proven_samples),
        "research_detail_routes": research_detail_routes,
        "missing_samples": [
            {"browser_engine": engine, "route": route, "viewport": {"width": width, "height": height}}
            for engine, route, width, height in missing_samples
        ],
        "static_fallbacks": static_routes,
        "failed_browser_routes": failed_browser_routes,
    }


def build_live_mobile_surface_receipt(
    *,
    base_url: str,
    api_token: str,
    principal_id: str,
    release_probe_secret: str = "",
    expected_plan_label: str = "Agent",
    host_header: str = "",
    routes: tuple[str, ...] = DEFAULT_ROUTES,
    require_research_detail: bool = False,
    viewport_width: int = 390,
    viewport_height: int = 844,
    timeout_ms: int = 60_000,
    proof_mode: str = "standard",
    supported_viewports: tuple[tuple[int, int], ...] | None = None,
    browser_engine: str = DEFAULT_BROWSER_ENGINE,
    required_browser_engines: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    normalized_release_probe_secret = str(release_probe_secret or "").strip()
    paid_persona = _is_paid_plan_label(expected_plan_label)
    release_probe_configured_routes = tuple(
        dict.fromkeys(
            normalized_route
            for route in routes
            if route_is_research_detail(route)
            for normalized_route in (str(route or "").strip(),)
            if normalized_route
        )
    )
    receipt_secrets = tuple(
        secret
        for secret in (str(api_token or "").strip(), normalized_release_probe_secret)
        if secret
    )
    normalized_proof_mode = normalize_mobile_proof_mode(proof_mode)
    browser_all = normalized_proof_mode == FLAGSHIP_PROOF_MODE
    selected_browser_engine = normalize_playwright_engine(browser_engine)
    normalized_required_engines = normalized_browser_engines(
        required_browser_engines
        if required_browser_engines is not None
        else (FLAGSHIP_BROWSER_ENGINES if browser_all else (selected_browser_engine,)),
        fallback_engine=selected_browser_engine,
    )
    probe_viewports = _normalized_viewports(
        supported_viewports if supported_viewports is not None else (FLAGSHIP_BROWSER_VIEWPORTS if browser_all else None),
        fallback_width=viewport_width,
        fallback_height=viewport_height,
    )
    if browser_all and (len(probe_viewports) > 1 or len(normalized_required_engines) > 1):
        child_receipts = [
            build_live_mobile_surface_receipt(
                base_url=base_url,
                api_token=api_token,
                principal_id=principal_id,
                release_probe_secret=normalized_release_probe_secret,
                expected_plan_label=expected_plan_label,
                host_header=host_header,
                routes=routes,
                require_research_detail=True,
                viewport_width=width,
                viewport_height=height,
                timeout_ms=timeout_ms,
                proof_mode=FLAGSHIP_PROOF_MODE,
                supported_viewports=((width, height),),
                browser_engine=engine,
                required_browser_engines=(engine,),
            )
            for engine in normalized_required_engines
            for width, height in probe_viewports
        ]
        rows = [
            dict(row)
            for receipt in child_receipts
            for row in list(receipt.get("routes") or [])
            if isinstance(row, dict)
        ]
        coverage_checks = list(child_receipts[0].get("coverage_checks") or []) if child_receipts else []
        coverage_checks = [
            row
            for row in coverage_checks
            if isinstance(row, dict) and str(row.get("name") or "") != "flagship_browser_all_playwright_proof"
        ]
        browser_proof = build_browser_all_proof_summary(
            routes=routes,
            rows=rows,
            viewports=probe_viewports,
            required_browser_engines=normalized_required_engines,
        )
        coverage_checks.append(
            {
                "name": "flagship_browser_all_playwright_proof",
                "ok": browser_proof["ready"],
                "reason": "Flagship mobile evidence must use real Playwright measurement for every configured customer route, required browser engine, and supported viewport, including a concrete research detail.",
                "missing_samples": browser_proof["missing_samples"],
                "static_fallbacks": browser_proof["static_fallbacks"],
            }
        )
        failed_rows = [row for row in rows if row.get("ok") is not True]
        failed_coverage = [row for row in coverage_checks if row.get("ok") is not True]
        blocked = any(str(receipt.get("status") or "") == "blocked" for receipt in child_receipts)
        billing_readiness = mobile_billing_readiness_summary(
            rows,
            strict_required=paid_persona,
        )
        billing_readiness["paid_persona"] = paid_persona
        receipt = {
            "status": "blocked" if blocked else ("pass" if not failed_rows and not failed_coverage else "fail"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "base_url": base_url,
            "host_header": host_header,
            "navigation_base_url": str(child_receipts[0].get("navigation_base_url") or base_url) if child_receipts else base_url,
            "principal_id": principal_id,
            "expected_plan_label": expected_plan_label,
            "proof_mode": FLAGSHIP_PROOF_MODE,
            "browser_engine": "matrix" if len(normalized_required_engines) > 1 else normalized_required_engines[0],
            "browser_engines": list(normalized_required_engines),
            "required_browser_engines": list(normalized_required_engines),
            "browser_proof": browser_proof,
            "viewport": {"width": probe_viewports[0][0], "height": probe_viewports[0][1]},
            "supported_viewports": browser_proof["supported_viewports"],
            "route_count": len(rows),
            "configured_route_count": len(routes),
            "failed_count": len(failed_rows) + len(failed_coverage),
            "billing_readiness": billing_readiness,
            "coverage_checks": coverage_checks,
            "routes": rows,
            "notes": [
                "Flagship browser-all smoke measures every configured customer route in every required Playwright browser engine at every supported viewport.",
                "API token values are never written to this receipt.",
                "Release-probe secret values are never written to this receipt.",
            ],
        }
        return _redact_sensitive_receipt_value(
            _redact_concrete_secret_values(receipt, secrets=receipt_secrets)
        )
    viewport_width, viewport_height = probe_viewports[0]
    try:
        validated_base_origin = validated_live_base_origin(base_url)
    except ValueError as exc:
        return {
            "status": "blocked",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "base_url": base_url,
            "host_header": host_header,
            "navigation_base_url": base_url,
            "principal_id": principal_id,
            "proof_mode": normalized_proof_mode,
            "browser_engine": selected_browser_engine,
            "browser_engines": [selected_browser_engine],
            "required_browser_engines": list(normalized_required_engines),
            "browser_proof": None,
            "viewport": {"width": viewport_width, "height": viewport_height},
            "route_count": 0,
            "failed_count": 1,
            "coverage_checks": [{"name": "live_base_origin_safe", "ok": False, "reason": str(exc)}],
            "routes": [],
            "notes": ["Authenticated live probes require an exact HTTPS origin; HTTP is allowed only on loopback."],
        }
    if routes_require_api_auth(routes) and not (
        normalized_release_probe_secret or str(api_token or "").strip()
    ):
        return {
            "status": "blocked",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "base_url": base_url,
            "host_header": host_header,
            "navigation_base_url": base_url,
            "principal_id": principal_id,
            "proof_mode": normalized_proof_mode,
            "browser_engine": selected_browser_engine,
            "browser_engines": [selected_browser_engine],
            "required_browser_engines": list(normalized_required_engines),
            "browser_proof": None,
            "viewport": {"width": viewport_width, "height": viewport_height},
            "route_count": 0,
            "failed_count": 1,
            "coverage_checks": [
                {
                    "name": "api_token_present_for_app_routes",
                    "ok": False,
                    "reason": "Live mobile app-surface smoke requires EA_API_TOKEN or --api-token; otherwise protected pages render sign-in redirects instead of the app UI.",
                }
            ],
            "routes": [],
            "notes": [
                "Live mobile smoke checks deployed HTML geometry only; it does not call listing providers.",
                "API token values are never written to this receipt.",
                "Release-probe secret values are never written to this receipt.",
            ],
        }
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": SEED_FIXTURE_USER_AGENT,
    }
    if not normalized_release_probe_secret:
        headers["X-EA-Principal-ID"] = principal_id
    if api_token and not normalized_release_probe_secret:
        headers["Authorization"] = f"Bearer {api_token}"
        headers["X-EA-API-Token"] = api_token
        headers["X-API-Token"] = api_token
    browser_args: list[str] = []
    navigation_base_url = base_url
    normalized_host_header = str(host_header or "").strip()
    if normalized_host_header:
        parsed_base = urllib.parse.urlparse(base_url)
        original_host = str(parsed_base.hostname or "").strip()
        branded_host = normalized_host_header.split(":", 1)[0].strip()
        if branded_host:
            branded_netloc = normalized_host_header
            if ":" not in branded_netloc and parsed_base.port:
                branded_netloc = f"{branded_host}:{parsed_base.port}"
            navigation_base_url = urllib.parse.urlunparse(parsed_base._replace(netloc=branded_netloc))
            if original_host and original_host != branded_host:
                browser_args.append(f"--host-resolver-rules=MAP {branded_host} {original_host}")
    browser_authorized_origin = normalized_origin(navigation_base_url)
    rows: list[dict[str, Any]] = []
    route_deadline_seconds = max(10, min(75, int((timeout_ms / 1000.0) + 15)))
    route_timeout_ms = max(1000, min(timeout_ms, route_deadline_seconds * 1000))
    probe_request_kwargs: dict[str, Any] = {}
    if normalized_release_probe_secret:
        probe_request_kwargs = {
            "release_probe_secret": normalized_release_probe_secret,
            "release_probe_configured_routes": release_probe_configured_routes,
        }
    resolved_browser_navigation: dict[str, tuple[str, dict[str, object]]] = {}

    def _run_with_deadline(action: Any, *, seconds: int, label: str) -> Any:
        if os.name == "nt":
            return action()
        previous_handler = signal.getsignal(signal.SIGALRM)

        def _timeout(_signum: int, _frame: Any) -> None:
            raise TimeoutError(label)

        try:
            signal.signal(signal.SIGALRM, _timeout)
            signal.alarm(max(1, int(seconds)))
            return action()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)

    def _collect_route_metrics_in_worker(route: str, url: str) -> tuple[int, dict[str, Any]]:
        browser_url = url
        redirect_metrics: dict[str, object] = {}
        if normalized_release_probe_secret:
            if url not in resolved_browser_navigation:
                resolved_browser_navigation[url] = _release_probe_browser_navigation_url(
                    url,
                    headers=headers,
                    authorized_origin=browser_authorized_origin,
                    timeout_seconds=route_deadline_seconds,
                    release_probe_secret=normalized_release_probe_secret,
                    release_probe_configured_routes=release_probe_configured_routes,
                )
            browser_url, redirect_metrics = resolved_browser_navigation[url]
        status_code, metrics = collect_playwright_route_metrics(
            route=route,
            url=browser_url,
            headers=headers,
            authorized_origin=browser_authorized_origin,
            browser_args=browser_args,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            route_timeout_ms=route_timeout_ms,
            route_deadline_seconds=route_deadline_seconds,
            browser_engine=selected_browser_engine,
            **probe_request_kwargs,
        )
        if browser_all:
            metrics["require_browser_proof"] = True
        metrics.update(redirect_metrics)
        metrics["requested_route_url"] = url
        return status_code, metrics

    for route in routes:
        url = navigation_base_url.rstrip("/") + "/" + route.lstrip("/")
        route_log_label = _route_log_label(route)
        _log_smoke_progress(f"checking {route_log_label}")
        if str(route or "").split("?", 1)[0].strip() == "/app/billing":
            request_url = base_url.rstrip("/") + "/" + route.lstrip("/")
            request_headers = dict(headers)
            if normalized_host_header:
                request_headers["Host"] = normalized_host_header
            try:
                response = _run_with_deadline(
                    lambda: _http_get_for_smoke(
                        request_url,
                        headers=request_headers,
                        timeout_seconds=route_deadline_seconds,
                        follow_redirects=False,
                        authorized_origin=validated_base_origin,
                        **probe_request_kwargs,
                    ),
                    seconds=route_deadline_seconds,
                    label=f"billing_route_timeout:{route_log_label}",
                )
                status_code = int(response.get("status_code") or 0)
                billing_text = str(response.get("text") or "") if status_code == 503 else ""
                billing_handoff_probe: dict[str, Any] = {}
                billing_bridge_login_assist_probe: dict[str, Any] = {}
                billing_handoff_host_resolves = False
                redirect_location = _header_value(dict(response.get("headers") or {}), "location")
                resolved_handoff = {
                    "external_location": redirect_location,
                    "bridge_launch_used": False,
                    "bridge_launch_url": "",
                    "bridge_launch_status_code": 0,
                }
                if status_code in {303, 307} and redirect_location:
                    resolved_handoff = _resolve_mobile_billing_external_handoff(
                        base_url=base_url,
                        redirect_location=redirect_location,
                        request_headers=request_headers,
                        timeout_ms=route_timeout_ms,
                        **probe_request_kwargs,
                    )
                    external_location = str(resolved_handoff.get("external_location") or "").strip()
                    billing_handoff_host_resolves = https_redirect_host_resolves(
                        external_location,
                        socket.getaddrinfo,
                    )
                    billing_handoff_probe = https_handoff_url_usable(
                        external_location,
                        timeout_seconds=min(8.0, max(3.0, route_timeout_ms / 1000.0)),
                        allowed_hosts=PROPERTYQUARRY_BILLING_HANDOFF_ALLOWED_HOSTS,
                    )
                    if (
                        billing_handoff_probe.get("ok") is not True
                        and str(billing_handoff_probe.get("error") or "").strip() == "handoff_url_requires_separate_login"
                        and str(urllib.parse.urlparse(external_location).path or "").strip().startswith("/sso/propertyquarry")
                    ):
                        billing_bridge_login_assist_probe = _mobile_billing_bridge_guided_login_assist_probe(
                            external_location,
                            timeout_ms=route_timeout_ms,
                        )
                metrics = {
                    "browser_engine": selected_browser_engine,
                    "status_code": status_code,
                    "viewport_width": viewport_width,
                    "body_width": viewport_width,
                    "topbar_height": 0,
                    "min_action_height": 44,
                    "redirect_location": str(resolved_handoff.get("external_location") or redirect_location),
                    "bridge_launch_url": str(resolved_handoff.get("bridge_launch_url") or ""),
                    "bridge_launch_used": bool(resolved_handoff.get("bridge_launch_used")),
                    "bridge_launch_status_code": int(resolved_handoff.get("bridge_launch_status_code") or 0),
                    "billing_handoff_host_resolves": billing_handoff_host_resolves,
                    "billing_handoff_usable": bool(billing_handoff_probe.get("ok")),
                    "billing_handoff_probe": billing_handoff_probe,
                    "billing_direct_handoff_usable": bool(billing_handoff_probe.get("ok")),
                    "billing_bridge_login_assist_probe": billing_bridge_login_assist_probe,
                    "billing_visible_text": billing_text,
                }
                row_proof_mode = "http_navigation_contract"
                if browser_all:
                    browser_status_code, browser_metrics, _browser_checks = collect_browser_route_metrics_with_retries(
                        route=route,
                        url=url,
                        collect_once=_collect_route_metrics_in_worker,
                    )
                    for key, value in browser_metrics.items():
                        if key != "status_code":
                            metrics[key] = value
                    metrics["browser_status_code"] = browser_status_code
                    metrics["browser_engine"] = selected_browser_engine
                    metrics["status_code"] = status_code
                    row_proof_mode = str(browser_metrics.get("proof_mode") or "playwright_failed")
                checks = evaluate_mobile_metrics(
                    route,
                    metrics,
                    require_billing_available=browser_all and paid_persona,
                )
                rows.append(
                    {
                        "route": route,
                        "browser_engine": selected_browser_engine,
                        "url": url,
                        "viewport": {"width": viewport_width, "height": viewport_height},
                        "proof_mode": row_proof_mode,
                        "status_code": status_code,
                        "ok": all(bool(check.get("ok")) for check in checks),
                        "checks": checks,
                        "metrics": metrics,
                    }
                )
                _log_smoke_progress(f"checked {route_log_label}: {status_code}")
            except Exception as exc:
                metrics = {
                    "browser_engine": selected_browser_engine,
                    "status_code": 0,
                    "viewport_width": viewport_width,
                    "body_width": 0,
                    "topbar_height": 0,
                    "min_action_height": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                if browser_all:
                    metrics["proof_mode"] = "playwright_failed"
                checks = evaluate_mobile_metrics(
                    route,
                    metrics,
                    require_billing_available=browser_all and paid_persona,
                )
                rows.append(
                    {
                        "route": route,
                        "browser_engine": selected_browser_engine,
                        "url": url,
                        "viewport": {"width": viewport_width, "height": viewport_height},
                        "proof_mode": "playwright_failed" if browser_all else "http_navigation_contract",
                        "status_code": 0,
                        "ok": False,
                        "checks": checks,
                        "metrics": metrics,
                    }
                )
                _log_smoke_progress(f"failed {route_log_label}: {type(exc).__name__}: {exc}")
            continue
        if not browser_all and not route_requires_browser_mobile_probe(route):
            request_url = base_url.rstrip("/") + "/" + route.lstrip("/")
            request_headers = dict(headers)
            if normalized_host_header:
                request_headers["Host"] = normalized_host_header
            try:
                last_error: BaseException | None = None
                response: dict[str, Any] | None = None
                for attempt in range(2):
                    try:
                        response = _run_with_deadline(
                            lambda: _http_get_for_smoke(
                                request_url,
                                headers=request_headers,
                                timeout_seconds=route_deadline_seconds,
                                follow_redirects=True,
                                authorized_origin=validated_base_origin,
                                **probe_request_kwargs,
                            ),
                            seconds=route_deadline_seconds,
                            label=f"static_route_timeout:{route_log_label}",
                        )
                        break
                    except TimeoutError as exc:
                        last_error = exc
                        if attempt == 0:
                            _log_smoke_progress(f"retrying {route_log_label} after static timeout")
                            time.sleep(1)
                            continue
                        raise
                if response is None:
                    raise last_error or TimeoutError(f"static_route_timeout:{route}")
                status_code = int(response.get("status_code") or 0)
                html = str(response.get("text") or "")
                metrics = static_mobile_route_metrics_from_html(
                    html=html,
                    status_code=status_code,
                    viewport_width=viewport_width,
                )
                metrics["browser_engine"] = selected_browser_engine
                checks = evaluate_mobile_metrics(route, metrics)
                rows.append(
                    {
                        "route": route,
                        "browser_engine": selected_browser_engine,
                        "url": url,
                        "viewport": {"width": viewport_width, "height": viewport_height},
                        "proof_mode": "static_html",
                        "status_code": status_code,
                        "ok": all(bool(check.get("ok")) for check in checks),
                        "checks": checks,
                        "metrics": metrics,
                    }
                )
                _log_smoke_progress(f"checked {route_log_label}: {status_code}")
            except Exception as exc:
                metrics = {
                    "browser_engine": selected_browser_engine,
                    "status_code": 0,
                    "viewport_width": viewport_width,
                    "body_width": 0,
                    "topbar_height": 0,
                    "min_action_height": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "static_html_probe": True,
                }
                checks = evaluate_mobile_metrics(route, metrics)
                rows.append(
                    {
                        "route": route,
                        "browser_engine": selected_browser_engine,
                        "url": url,
                        "viewport": {"width": viewport_width, "height": viewport_height},
                        "proof_mode": "static_html",
                        "status_code": 0,
                        "ok": False,
                        "checks": checks,
                        "metrics": metrics,
                    }
                )
                _log_smoke_progress(f"failed {route_log_label}: {type(exc).__name__}: {exc}")
            continue
        try:
            status_code, metrics, checks = collect_browser_route_metrics_with_retries(
                route=route,
                url=url,
                collect_once=_collect_route_metrics_in_worker,
            )
            if not checks:
                checks = evaluate_mobile_metrics(route, metrics)
            metrics["browser_engine"] = selected_browser_engine
            rows.append(
                {
                    "route": route,
                    "browser_engine": selected_browser_engine,
                    "url": url,
                    "viewport": {"width": viewport_width, "height": viewport_height},
                    "proof_mode": str(metrics.get("proof_mode") or "playwright_failed"),
                    "status_code": status_code,
                    "ok": all(bool(check.get("ok")) for check in checks),
                    "checks": checks,
                    "metrics": metrics,
                }
            )
            _log_smoke_progress(f"checked {route_log_label}: {status_code}")
        except Exception as exc:
            metrics = {
                "browser_engine": selected_browser_engine,
                "status_code": 0,
                "viewport_width": viewport_width,
                "body_width": 0,
                "topbar_height": 0,
                "min_action_height": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
            checks = evaluate_mobile_metrics(route, metrics)
            rows.append(
                {
                    "route": route,
                    "browser_engine": selected_browser_engine,
                    "url": url,
                    "viewport": {"width": viewport_width, "height": viewport_height},
                    "proof_mode": "playwright_failed",
                    "status_code": 0,
                    "ok": False,
                    "checks": checks,
                    "metrics": metrics,
                }
            )
            _log_smoke_progress(f"failed {route_log_label}: {type(exc).__name__}: {exc}")
    failed = [row for row in rows if not row.get("ok")]
    coverage_checks = build_mobile_coverage_checks(routes, require_research_detail=require_research_detail)
    browser_proof: dict[str, Any] = {}
    if browser_all:
        browser_proof = build_browser_all_proof_summary(
            routes=routes,
            rows=rows,
            viewports=probe_viewports,
            required_browser_engines=normalized_required_engines,
        )
        coverage_checks.append(
            {
                "name": "flagship_browser_all_playwright_proof",
                "ok": browser_proof["ready"],
                "reason": "Flagship mobile evidence must use real Playwright measurement for every configured customer route, required browser engine, and supported viewport, including a concrete research detail.",
                "missing_samples": browser_proof["missing_samples"],
                "static_fallbacks": browser_proof["static_fallbacks"],
            }
        )
    failed_coverage = [row for row in coverage_checks if not row.get("ok")]
    billing_readiness = mobile_billing_readiness_summary(
        rows,
        strict_required=browser_all and paid_persona,
    )
    billing_readiness["paid_persona"] = paid_persona
    receipt = _redact_sensitive_receipt_value({
        "status": "pass" if not failed and not failed_coverage else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "host_header": host_header,
        "navigation_base_url": navigation_base_url,
        "principal_id": principal_id,
        "expected_plan_label": expected_plan_label,
        "proof_mode": normalized_proof_mode,
        "browser_engine": selected_browser_engine,
        "browser_engines": [selected_browser_engine],
        "required_browser_engines": list(normalized_required_engines),
        "browser_proof": browser_proof if browser_all else None,
        "viewport": {"width": viewport_width, "height": viewport_height},
        "supported_viewports": [
            {"width": width, "height": height}
            for width, height in probe_viewports
        ],
        "route_count": len(rows),
        "configured_route_count": len(routes),
        "failed_count": len(failed) + len(failed_coverage),
        "billing_readiness": billing_readiness,
        "coverage_checks": coverage_checks,
        "routes": rows,
        "notes": [
            (
                "Flagship browser-all smoke measures every configured customer route in every required Playwright browser engine at every supported viewport."
                if browser_all
                else "Standard live mobile smoke uses real browsers for interactive routes and static HTML checks for simple routes."
            ),
            "API token values are never written to this receipt.",
            "Release-probe secret values are never written to this receipt.",
        ],
    })
    return _redact_sensitive_receipt_value(
        _redact_concrete_secret_values(receipt, secrets=receipt_secrets)
    )


def build_seed_fixture_blocked_receipt(
    *,
    base_url: str,
    host_header: str,
    principal_id: str,
    viewport_width: int,
    viewport_height: int,
    error: str,
    api_token: str = "",
    release_probe_secret: str = "",
    proof_mode: str = "standard",
    browser_engine: str = DEFAULT_BROWSER_ENGINE,
    required_browser_engines: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    normalized_proof_mode = normalize_mobile_proof_mode(proof_mode)
    selected_browser_engine = normalize_playwright_engine(browser_engine)
    normalized_required_engines = normalized_browser_engines(
        required_browser_engines
        if required_browser_engines is not None
        else (FLAGSHIP_BROWSER_ENGINES if normalized_proof_mode == FLAGSHIP_PROOF_MODE else (selected_browser_engine,)),
        fallback_engine=selected_browser_engine,
    )
    receipt = _redact_sensitive_receipt_value({
        "status": "blocked",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "host_header": host_header,
        "navigation_base_url": base_url,
        "principal_id": principal_id,
        "proof_mode": normalized_proof_mode,
        "browser_engine": selected_browser_engine,
        "browser_engines": [selected_browser_engine],
        "required_browser_engines": list(normalized_required_engines),
        "browser_proof": None,
        "viewport": {"width": viewport_width, "height": viewport_height},
        "route_count": 0,
        "failed_count": 1,
        "coverage_checks": [
            {
                "name": "research_detail_seed_fixture_ready",
                "ok": False,
                "reason": "Live mobile smoke could not seed the saved research-detail fixture, so it cannot honestly prove the open-property surface.",
                "error": error,
            }
        ],
        "routes": [],
        "error": error,
        "notes": [
            "Live mobile smoke checks deployed HTML geometry only; it does not call listing providers.",
            "API token values are never written to this receipt.",
            "Release-probe secret values are never written to this receipt.",
            "When fixture seeding fails, rerun with a known current /app/research/{id}?run_id=... route via --routes or set PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE.",
        ],
    })
    return _redact_sensitive_receipt_value(
        _redact_concrete_secret_values(
            receipt,
            secrets=tuple(
                secret
                for secret in (
                    str(api_token or "").strip(),
                    str(release_probe_secret or "").strip(),
                )
                if secret
            ),
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live mobile UI smoke against PropertyQuarry app surfaces.")
    parser.add_argument("--base-url", default=_env("PROPERTYQUARRY_LIVE_BASE_URL", "http://localhost:8097"))
    parser.add_argument("--host-header", default=_env("PROPERTYQUARRY_LIVE_HOST_HEADER"))
    parser.add_argument("--api-token", default=_env("PROPERTYQUARRY_LIVE_API_TOKEN") or _env("EA_API_TOKEN"))
    parser.add_argument(
        "--api-token-stdin",
        action="store_true",
        help="Read the protected live API token once from bounded stdin.",
    )
    parser.add_argument(
        "--release-probe-secret-stdin",
        action="store_true",
        help="Read the protected release-probe credential once from bounded stdin.",
    )
    parser.add_argument("--principal-id", default=_env("PROPERTYQUARRY_LIVE_PRINCIPAL_ID", "pq-live-mobile-smoke"))
    parser.add_argument(
        "--expected-plan-label",
        default=_env("PROPERTYQUARRY_LIVE_SMOKE_PLAN_LABEL", "Agent"),
    )
    configured_research_detail = _env("PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE")
    default_routes = (*DEFAULT_ROUTES, configured_research_detail) if configured_research_detail else DEFAULT_ROUTES
    parser.add_argument("--routes", default=",".join(default_routes))
    parser.add_argument(
        "--require-research-detail",
        action="store_true",
        default=_env_flag("PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_REQUIRED"),
        help="Fail unless routes include a current /app/research/{id} detail URL.",
    )
    parser.add_argument(
        "--seed-research-detail-fixture",
        action="store_true",
        default=_env_flag("PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_SEED_FIXTURE"),
        help="Seed a deterministic saved research-detail candidate for the smoke principal and include it in the route set.",
    )
    parser.add_argument(
        "--proof-mode",
        choices=("standard", "flagship", "browser-all"),
        default=_env("PROPERTYQUARRY_LIVE_MOBILE_PROOF_MODE", "standard"),
        help="Use flagship/browser-all to require Playwright proof for every route and supported viewport.",
    )
    parser.add_argument(
        "--browser-engine",
        type=normalize_playwright_engine,
        choices=FLAGSHIP_BROWSER_ENGINES,
        default=_env("PROPERTYQUARRY_LIVE_MOBILE_BROWSER_ENGINE", DEFAULT_BROWSER_ENGINE),
        help="Playwright engine used by standard mode; Chromium remains the compatibility default.",
    )
    parser.add_argument(
        "--required-browser-engines",
        default=_env(
            "PROPERTYQUARRY_LIVE_MOBILE_REQUIRED_BROWSER_ENGINES",
            ",".join(FLAGSHIP_BROWSER_ENGINES),
        ),
        help="Comma-separated Playwright engines required by flagship/browser-all proof.",
    )
    parser.add_argument("--viewport", default="390x844")
    parser.add_argument(
        "--viewports",
        default=_env(
            "PROPERTYQUARRY_LIVE_MOBILE_FLAGSHIP_VIEWPORTS",
            ",".join(f"{width}x{height}" for width, height in FLAGSHIP_BROWSER_VIEWPORTS),
        ),
        help="Comma-separated viewport matrix used by flagship/browser-all mode.",
    )
    parser.add_argument("--timeout-ms", type=int, default=int(_env("PROPERTYQUARRY_LIVE_MOBILE_TIMEOUT_MS", "60000") or 60000))
    parser.add_argument("--write", default="_completion/smoke/property-live-mobile-surface-latest.json")
    args = parser.parse_args()
    if args.api_token_stdin and args.release_probe_secret_stdin:
        parser.error("--api-token-stdin and --release-probe-secret-stdin are mutually exclusive")
    if args.api_token_stdin and str(args.api_token or "").strip():
        parser.error("--api-token-stdin cannot be combined with --api-token or API-token environment variables")
    stdin_api_token = read_live_api_token_from_stdin(
        parser,
        enabled=bool(args.api_token_stdin),
    )
    release_probe_secret = read_release_probe_secret_from_stdin(
        parser,
        enabled=bool(args.release_probe_secret_stdin),
    )
    if stdin_api_token:
        args.api_token = stdin_api_token
    scrub_live_api_token_environment()
    scrub_release_probe_secret_environment()

    width_text, _, height_text = str(args.viewport).lower().partition("x")
    width = int(width_text or 390)
    height = int(height_text or 844)
    routes_list = [route.strip() for route in str(args.routes or "").split(",") if route.strip()]
    normalized_proof_mode = normalize_mobile_proof_mode(args.proof_mode)
    selected_browser_engine = normalize_playwright_engine(args.browser_engine)
    configured_required_engines = normalized_browser_engines(
        tuple(engine.strip() for engine in str(args.required_browser_engines or "").split(",") if engine.strip()),
        fallback_engine=selected_browser_engine,
    )
    required_browser_engines = (
        configured_required_engines
        if normalized_proof_mode == FLAGSHIP_PROOF_MODE
        else (selected_browser_engine,)
    )
    if normalized_proof_mode == FLAGSHIP_PROOF_MODE:
        args.require_research_detail = True
        if not any(route_is_research_detail(route) for route in routes_list):
            args.seed_research_detail_fixture = True
    flagship_viewports: list[tuple[int, int]] = []
    for raw_viewport in str(args.viewports or "").split(","):
        viewport_text = raw_viewport.strip().lower()
        if not viewport_text:
            continue
        item_width, separator, item_height = viewport_text.partition("x")
        if not separator:
            parser.error(f"invalid viewport {raw_viewport!r}; expected WIDTHxHEIGHT")
        flagship_viewports.append((int(item_width), int(item_height)))
    seeded_route = ""
    if args.seed_research_detail_fixture:
        try:
            if release_probe_secret:
                raise RuntimeError("release_probe_mode_blocks_research_detail_seed_post")
            _log_smoke_progress("seeding research detail fixture")
            seeded_route = seed_research_detail_fixture(
                base_url=str(args.base_url).strip(),
                api_token=str(args.api_token or "").strip(),
                principal_id=str(args.principal_id or "").strip() or "pq-live-mobile-smoke",
                host_header=str(args.host_header or "").strip(),
            )
            _log_smoke_progress(f"seeded research detail fixture: {_route_log_label(seeded_route)}")
        except Exception as exc:
            _log_smoke_progress(f"failed seeding research detail fixture: {type(exc).__name__}: {exc}")
            receipt = build_seed_fixture_blocked_receipt(
                base_url=str(args.base_url).strip(),
                host_header=str(args.host_header or "").strip(),
                principal_id=str(args.principal_id or "").strip() or "pq-live-mobile-smoke",
                viewport_width=width,
                viewport_height=height,
                error=f"seed_research_detail_fixture_failed:{type(exc).__name__}: {exc}",
                api_token=str(args.api_token or "").strip(),
                release_probe_secret=release_probe_secret,
                proof_mode=normalized_proof_mode,
                browser_engine=selected_browser_engine,
                required_browser_engines=required_browser_engines,
            )
            output = json.dumps(receipt, indent=2, sort_keys=True)
            if args.write:
                out_path = Path(args.write)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(output + "\n", encoding="utf-8")
            print(output)
            return 1
        if seeded_route not in routes_list:
            routes_list.append(seeded_route)
        args.require_research_detail = True
    routes = tuple(routes_list)
    receipt = build_live_mobile_surface_receipt(
        base_url=str(args.base_url).strip(),
        api_token=str(args.api_token or "").strip(),
        principal_id=str(args.principal_id or "").strip() or "pq-live-mobile-smoke",
        release_probe_secret=release_probe_secret,
        expected_plan_label=str(args.expected_plan_label or "").strip(),
        host_header=str(args.host_header or "").strip(),
        routes=routes or DEFAULT_ROUTES,
        require_research_detail=bool(args.require_research_detail),
        viewport_width=width,
        viewport_height=height,
        timeout_ms=max(1, int(args.timeout_ms or 60000)),
        proof_mode=normalized_proof_mode,
        browser_engine=selected_browser_engine,
        required_browser_engines=required_browser_engines,
        supported_viewports=(
            tuple(flagship_viewports) or FLAGSHIP_BROWSER_VIEWPORTS
            if normalized_proof_mode == FLAGSHIP_PROOF_MODE
            else None
        ),
    )
    if seeded_route:
        receipt["seeded_research_detail_route"] = seeded_route
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Playwright/browser helper processes can keep Python alive after the
    # receipt is flushed. A smoke gate must return deterministically.
    os._exit(exit_code)
