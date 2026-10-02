#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from scripts.propertyquarry_playwright_runtime import (
    SUPPORTED_PLAYWRIGHT_ENGINES,
    normalize_playwright_engine,
    playwright_browser_type,
    playwright_engine_launch_kwargs,
)


REQUIRED_FAILURE_STATES = (
    "not_found",
    "internal_error",
    "offline",
    "expired_session",
    "delayed",
    "quota_blocked",
    "payment_failed",
    "empty",
    "partial",
    "provider_blocked",
    "stale",
    "missing_packet",
    "missing_media",
)
DEFAULT_PRESERVATION_PROBE_ROUTE = "/v1/onboarding/property-search/preferences"
DEFAULT_SCENARIO_ROUTES = {
    "not_found": "/__propertyquarry_flagship_page_not_found__",
    "internal_error": "",
    "offline": "/app/search",
    "expired_session": "/app/search",
    "delayed": "",
    "quota_blocked": "",
    "payment_failed": "",
    "empty": "",
    "partial": "",
    "provider_blocked": "",
    "stale": "/app/shortlist?run_id=pq-flagship-missing-run",
    "missing_packet": "/app/research/pq-flagship-missing-home",
    "missing_media": "",
}
CALM_COPY_TOKENS = {
    "not_found": ("couldn\u2019t find", "couldn't find", "page not found"),
    "internal_error": ("something went wrong", "temporary interruption"),
    "offline": ("offline", "reconnect"),
    "expired_session": ("session ended", "sign in again"),
    "delayed": ("still running", "taking longer", "updates paused"),
    "quota_blocked": ("limit", "try again", "search is unchanged"),
    "payment_failed": ("payment", "billing", "access is unchanged"),
    "empty": ("nothing matched", "no matches", "change one thing"),
    "partial": ("partial results", "review the homes already found"),
    "provider_blocked": ("site changed", "could not finish", "retrying", "search is saved"),
    "stale": ("saved link was stale", "current workspace", "current search"),
    "missing_packet": ("not available right now", "being rebuilt", "shortlist"),
    "missing_media": ("media", "preview not available", "listing"),
}
FORBIDDEN_RAW_DIAGNOSTICS = (
    "traceback (most recent call last)",
    "internal server error",
    "failed to fetch",
    "networkerror",
    "network request failed",
    "403 forbidden",
    "401 unauthorized",
    "exception:",
    "stack trace",
)
REQUIRED_ROW_CHECKS = (
    "state_marker_visible",
    "calm_customer_copy",
    "useful_next_action",
    "semantic_status_contract",
    "raw_diagnostics_hidden",
    "scenario_transition_proven",
    "customer_data_preserved",
)
SENSITIVE_ROUTE_QUERY_KEYS = {
    "access_token",
    "code",
    "id_token",
    "key",
    "login_token",
    "refresh_token",
    "secret",
    "signature",
    "state",
    "token",
}


def normalize_browser_engines(engines: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_engine in engines or SUPPORTED_PLAYWRIGHT_ENGINES:
        engine = normalize_playwright_engine(raw_engine)
        if engine not in normalized:
            normalized.append(engine)
    return tuple(normalized)


def normalize_scenario_routes(routes: dict[str, str] | None) -> dict[str, str]:
    supplied = routes or {}
    return {
        state: str(supplied.get(state, DEFAULT_SCENARIO_ROUTES[state]) or "").strip()
        for state in REQUIRED_FAILURE_STATES
    }


def _scenario_route_error(route: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(route or ""))
    except ValueError:
        return "invalid_url"
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "relative_path_required"
    sensitive_keys = {
        str(key or "").strip().lower()
        for key, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    } & SENSITIVE_ROUTE_QUERY_KEYS
    if sensitive_keys:
        return "sensitive_query_forbidden:" + ",".join(sorted(sensitive_keys))
    return ""


def _receipt_scenario_routes(routes: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for state, route in routes.items():
        parsed = urllib.parse.urlsplit(str(route or ""))
        query = urllib.parse.urlencode(
            [
                (key, "[redacted]" if str(key or "").strip().lower() in SENSITIVE_ROUTE_QUERY_KEYS else value)
                for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        safe[state] = urllib.parse.urlunsplit(("", "", parsed.path, query, parsed.fragment))
    return safe


def _origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or ""))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _preservation_snapshot(
    context: Any,
    *,
    base_url: str,
    route: str,
    headers: dict[str, str],
    timeout_ms: int,
) -> dict[str, Any]:
    """Return non-sensitive proof of a substantive, canonical JSON snapshot."""

    try:
        response = context.request.get(
            str(base_url or "").rstrip("/") + "/" + str(route or "").lstrip("/"),
            headers={**headers, "Accept": "application/json"},
            fail_on_status_code=False,
            timeout=timeout_ms,
        )
        status_code = int(response.status)
        raw_body = bytes(response.body())
        payload = response.json()
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        substantive = (
            (isinstance(payload, dict) and bool(payload))
            or (isinstance(payload, list) and bool(payload))
            or (isinstance(payload, str) and bool(payload.strip()))
            or (isinstance(payload, (int, float)) and not isinstance(payload, bool))
        )
        return {
            "ok": status_code == 200 and substantive,
            "status_code": status_code,
            "body_bytes": len(raw_body),
            "canonical_bytes": len(canonical),
            "sha256": hashlib.sha256(canonical).hexdigest(),
            "error": "" if substantive else "preservation_payload_not_substantive",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "body_bytes": 0,
            "canonical_bytes": 0,
            "sha256": "",
            "error": type(exc).__name__,
        }


def _continue_with_auth_state(
    route: Any,
    *,
    authorized_origin: str,
    headers: dict[str, str],
    auth_enabled: dict[str, bool],
) -> None:
    if _origin(str(route.request.url or "")) != authorized_origin or not auth_enabled["value"]:
        route.continue_()
        return
    merged = dict(route.request.headers)
    merged.update(headers)
    route.continue_(headers=merged)


def _marker_observation(page: Any, *, state: str) -> dict[str, Any]:
    marker = page.locator(f'[data-pq-failure-state="{state}"]:visible').first
    if marker.count() == 0:
        return {
            "marker_visible": False,
            "observed_state": "",
            "copy": "",
            "action_text": "",
            "action_href": "",
            "semantic_status": False,
        }
    copy = " ".join(str(marker.inner_text() or "").split())[:1_200]
    action = marker.locator('[data-pq-next-action]:visible, a[href]:visible, button:not([disabled]):visible').first
    action_text = ""
    action_href = ""
    action_kind = ""
    if action.count() > 0:
        action_text = " ".join(str(action.inner_text() or "").split())[:180]
        action_href = str(action.get_attribute("href") or "")[:400]
        action_kind = str(action.evaluate("node => String(node.tagName || '').toLowerCase()") or "")
    semantics = dict(
        marker.evaluate(
            """
            node => ({
              role: String(node.getAttribute('role') || ''),
              ariaLive: String(node.getAttribute('aria-live') || ''),
              labelled: Boolean(node.getAttribute('aria-label') || node.getAttribute('aria-labelledby')),
            })
            """
        )
        or {}
    )
    semantic_status = (
        str(semantics.get("role") or "") in {"alert", "status"}
        or str(semantics.get("ariaLive") or "") in {"polite", "assertive"}
    )
    return {
        "marker_visible": True,
        "observed_state": state,
        "copy": copy,
        "action_text": action_text,
        "action_href": action_href,
        "action_kind": action_kind,
        "semantic_status": semantic_status,
    }


def collect_failure_state_browser_rows(
    *,
    base_url: str,
    scenario_routes: dict[str, str],
    browser_engine: str,
    headers: dict[str, str],
    preservation_probe_route: str,
    timeout_ms: int,
) -> list[dict[str, Any]]:
    from playwright.sync_api import sync_playwright

    engine = normalize_playwright_engine(browser_engine)
    normalized_base = str(base_url or "").rstrip("/")
    authorized_origin = _origin(normalized_base)
    auth_enabled = {"value": True}
    rows: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser_type = playwright_browser_type(playwright, engine=engine)
        browser = browser_type.launch(
            **playwright_engine_launch_kwargs(
                playwright,
                engine=engine,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        )
        try:
            context = browser.new_context(service_workers="block", viewport={"width": 1280, "height": 900})
            context.route(
                "**/*",
                lambda route: _continue_with_auth_state(
                    route,
                    authorized_origin=authorized_origin,
                    headers=headers,
                    auth_enabled=auth_enabled,
                ),
            )
            try:
                for state in REQUIRED_FAILURE_STATES:
                    route = str(scenario_routes.get(state) or "").strip()
                    if not route:
                        continue
                    page = context.new_page()
                    page.set_default_timeout(timeout_ms)
                    page.set_default_navigation_timeout(timeout_ms)
                    requested_url = normalized_base + "/" + route.lstrip("/")
                    status_code = 0
                    transition_proven = True
                    error = ""
                    preservation_before = _preservation_snapshot(
                        context,
                        base_url=normalized_base,
                        route=preservation_probe_route,
                        headers=headers,
                        timeout_ms=timeout_ms,
                    )
                    try:
                        auth_enabled["value"] = True
                        response = page.goto(requested_url, wait_until="domcontentloaded", timeout=timeout_ms)
                        status_code = int(response.status) if response is not None else 0
                        if state == "offline":
                            context.set_offline(True)
                            page.evaluate("window.dispatchEvent(new Event('offline'))")
                            page.wait_for_timeout(150)
                            transition_proven = bool(page.evaluate("navigator.onLine === false"))
                        elif state == "expired_session":
                            initial_path = urllib.parse.urlsplit(str(page.url or "")).path
                            auth_enabled["value"] = False
                            context.clear_cookies()
                            expired_response = page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                            status_code = int(expired_response.status) if expired_response is not None else 0
                            final_url = urllib.parse.urlsplit(str(page.url or ""))
                            transition_proven = (
                                initial_path.startswith("/app")
                                and final_url.path == "/sign-in"
                                and urllib.parse.parse_qs(final_url.query).get("session") == ["expired"]
                            )
                        page.wait_for_timeout(100)
                        observation = _marker_observation(page, state=state)
                    except Exception as exc:
                        observation = _marker_observation(page, state=state)
                        transition_proven = False
                        error = f"{type(exc).__name__}: {exc}"
                    finally:
                        if state == "offline":
                            context.set_offline(False)
                        auth_enabled["value"] = True
                    preservation_after = _preservation_snapshot(
                        context,
                        base_url=normalized_base,
                        route=preservation_probe_route,
                        headers=headers,
                        timeout_ms=timeout_ms,
                    )
                    preservation_same = (
                        preservation_before.get("ok") is True
                        and preservation_after.get("ok") is True
                        and bool(preservation_before.get("sha256"))
                        and preservation_before.get("sha256") == preservation_after.get("sha256")
                    )
                    observation.update(
                        {
                            "state": state,
                            "route": route,
                            "requested_url": requested_url,
                            "final_url": str(page.url or ""),
                            "status_code": status_code,
                            "browser_engine": engine,
                            "transition_proven": transition_proven,
                            "customer_data_preserved": preservation_same,
                            "preservation_probe": {
                                "before": preservation_before,
                                "after": preservation_after,
                                "same_digest": preservation_same,
                            },
                            "error": error,
                        }
                    )
                    rows.append(observation)
                    page.close()
            finally:
                context.close()
        finally:
            browser.close()
    return rows


def evaluate_failure_state_observation(observation: dict[str, Any]) -> list[dict[str, Any]]:
    state = str(observation.get("state") or "").strip()
    copy = str(observation.get("copy") or "")
    lowered = copy.lower()
    expected_copy = CALM_COPY_TOKENS.get(state, ())
    status_code = int(observation.get("status_code") or 0)
    status_contract = True
    if state == "not_found":
        status_contract = status_code == 404
    elif state == "internal_error":
        status_contract = status_code == 500
    transition_proven = observation.get("transition_proven") is True and status_contract
    return [
        {
            "name": "state_marker_visible",
            "ok": observation.get("marker_visible") is True and observation.get("observed_state") == state,
        },
        {
            "name": "calm_customer_copy",
            "ok": bool(copy) and any(token in lowered for token in expected_copy),
            "expected_any": list(expected_copy),
        },
        {
            "name": "useful_next_action",
            "ok": bool(str(observation.get("action_text") or "").strip())
            and (
                bool(str(observation.get("action_href") or "").strip())
                or str(observation.get("action_kind") or "").strip().lower() == "button"
            ),
        },
        {"name": "semantic_status_contract", "ok": observation.get("semantic_status") is True},
        {
            "name": "raw_diagnostics_hidden",
            "ok": not any(marker in lowered for marker in FORBIDDEN_RAW_DIAGNOSTICS),
        },
        {"name": "scenario_transition_proven", "ok": transition_proven},
        {
            "name": "customer_data_preserved",
            "ok": observation.get("customer_data_preserved") is True,
        },
    ]


def build_failure_state_receipt(
    *,
    base_url: str,
    scenario_routes: dict[str, str] | None = None,
    browser_engines: tuple[str, ...] = SUPPORTED_PLAYWRIGHT_ENGINES,
    api_token: str = "",
    principal_id: str = "pq-failure-state-gate",
    preservation_probe_route: str = DEFAULT_PRESERVATION_PROBE_ROUTE,
    timeout_ms: int = 30_000,
    collect_rows: Callable[..., list[dict[str, Any]]] = collect_failure_state_browser_rows,
) -> dict[str, Any]:
    routes = normalize_scenario_routes(scenario_routes)
    receipt_routes = _receipt_scenario_routes(routes)
    engines = normalize_browser_engines(browser_engines)
    missing_routes = [state for state in REQUIRED_FAILURE_STATES if not routes[state]]
    invalid_routes = {
        state: error
        for state, route in routes.items()
        if route and (error := _scenario_route_error(route))
    }
    normalized_preservation_route = str(preservation_probe_route or "").strip()
    preservation_route_error = (
        "required"
        if not normalized_preservation_route
        else _scenario_route_error(normalized_preservation_route)
    )
    headers = {"X-EA-Principal-ID": principal_id, "Accept": "text/html,application/xhtml+xml"}
    if api_token:
        headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "X-EA-API-Token": api_token,
                "X-API-Token": api_token,
            }
        )
    proof_mode = "playwright_browser_all" if collect_rows is collect_failure_state_browser_rows else "contract_mock"
    if missing_routes or invalid_routes or preservation_route_error:
        return {
            "status": "blocked",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "base_url": str(base_url or "").rstrip("/"),
            "proof_mode": proof_mode,
            "required_browser_engines": list(engines),
            "required_failure_states": list(REQUIRED_FAILURE_STATES),
            "scenario_routes": receipt_routes,
            "preservation_probe_route": _receipt_scenario_routes(
                {"preservation": normalized_preservation_route}
            )["preservation"],
            "expected_sample_count": 0,
            "observed_sample_count": 0,
            "failed_count": 1,
            "checks": [
                {
                    "name": "required_failure_scenarios_configured",
                    "ok": False,
                    "missing_states": missing_routes,
                    "invalid_routes": invalid_routes,
                    "preservation_probe_route_error": preservation_route_error,
                }
            ],
            "rows": [],
            "engine_failures": [],
            "notes": [
                "The gate did not open a browser because required pre-provisioned scenario routes were missing.",
                "No provider responses are mocked and no search runs are created by this gate.",
            ],
        }
    raw_rows: list[dict[str, Any]] = []
    engine_failures: list[dict[str, str]] = []
    for engine in engines:
        try:
            raw_rows.extend(
                collect_rows(
                    base_url=base_url,
                    scenario_routes=routes,
                    browser_engine=engine,
                    headers=headers,
                    preservation_probe_route=normalized_preservation_route,
                    timeout_ms=timeout_ms,
                )
            )
        except Exception as exc:
            engine_failures.append({"browser_engine": engine, "error": f"{type(exc).__name__}: {exc}"})
    rows: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        row = dict(raw_row)
        checks = evaluate_failure_state_observation(row)
        row["checks"] = checks
        row["ok"] = all(check.get("ok") is True for check in checks)
        rows.append(row)
    expected_samples = {
        (engine, state)
        for engine in engines
        for state in REQUIRED_FAILURE_STATES
        if routes[state]
    }
    observed_samples = {
        (str(row.get("browser_engine") or ""), str(row.get("state") or ""))
        for row in rows
    }
    missing_samples = sorted(expected_samples - observed_samples)
    preserved_samples = {
        (str(row.get("browser_engine") or ""), str(row.get("state") or ""))
        for row in rows
        if row.get("customer_data_preserved") is True
    }
    missing_preservation_samples = sorted(expected_samples - preserved_samples)
    checks = [
        {
            "name": "required_failure_scenarios_configured",
            "ok": not missing_routes,
            "missing_states": missing_routes,
        },
        {
            "name": "browser_state_engine_matrix_complete",
            "ok": not missing_samples and not engine_failures,
            "missing_samples": [
                {"browser_engine": engine, "state": state}
                for engine, state in missing_samples
            ],
            "engine_failures": engine_failures,
        },
        {
            "name": "no_provider_response_mocking",
            "ok": proof_mode == "playwright_browser_all",
            "applicable_to_flagship": True,
        },
        {
            "name": "customer_data_preservation_matrix_complete",
            "ok": not missing_preservation_samples,
            "missing_samples": [
                {"browser_engine": engine, "state": state}
                for engine, state in missing_preservation_samples
            ],
        },
    ]
    failed_rows = [row for row in rows if row.get("ok") is not True]
    failed_checks = [check for check in checks[:2] if check.get("ok") is not True]
    status = "pass" if not failed_rows and not failed_checks else "fail"
    if missing_routes and not rows:
        status = "blocked"
    receipt = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": str(base_url or "").rstrip("/"),
        "proof_mode": proof_mode,
        "required_browser_engines": list(engines),
        "required_failure_states": list(REQUIRED_FAILURE_STATES),
        "scenario_routes": receipt_routes,
        "preservation_probe_route": _receipt_scenario_routes(
            {"preservation": normalized_preservation_route}
        )["preservation"],
        "expected_sample_count": len(expected_samples),
        "observed_sample_count": len(observed_samples),
        "failed_count": len(failed_rows) + len(failed_checks),
        "checks": checks,
        "rows": rows,
        "engine_failures": engine_failures,
        "notes": [
            "The gate is read-only and never creates runs or mocks provider responses.",
            "Internal-error, delayed, quota, payment, empty, partial, provider-blocked, and missing-media routes must point to pre-provisioned safe canaries.",
            "Each sample proves the authenticated preference snapshot is substantive and has the same canonical SHA-256 before and after; receipt bodies are never emitted.",
            "Injected collectors are marked contract_mock and cannot satisfy flagship proof.",
        ],
    }
    serialized = json.dumps(receipt, sort_keys=True)
    if api_token and api_token in serialized:
        raise RuntimeError("failure_state_receipt_secret_leak")
    return receipt


def _scenario_routes_from_environment() -> dict[str, str]:
    routes = dict(DEFAULT_SCENARIO_ROUTES)
    for state in REQUIRED_FAILURE_STATES:
        env_name = f"PROPERTYQUARRY_FAILURE_{state.upper()}_ROUTE"
        configured = str(os.environ.get(env_name) or "").strip()
        if configured:
            routes[state] = configured
    return routes


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the read-only PropertyQuarry browser failure-state gate.")
    parser.add_argument("--base-url", default=os.environ.get("PROPERTYQUARRY_FAILURE_BASE_URL", "http://127.0.0.1:8097"))
    parser.add_argument(
        "--browser-engines",
        default=os.environ.get("PROPERTYQUARRY_FAILURE_BROWSER_ENGINES", ",".join(SUPPORTED_PLAYWRIGHT_ENGINES)),
    )
    parser.add_argument("--api-token", default=os.environ.get("PROPERTYQUARRY_FAILURE_API_TOKEN") or os.environ.get("EA_API_TOKEN", ""))
    parser.add_argument("--principal-id", default=os.environ.get("PROPERTYQUARRY_FAILURE_PRINCIPAL_ID", "pq-failure-state-gate"))
    parser.add_argument(
        "--preservation-probe-route",
        default=os.environ.get(
            "PROPERTYQUARRY_FAILURE_PRESERVATION_PROBE_ROUTE",
            DEFAULT_PRESERVATION_PROBE_ROUTE,
        ),
    )
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    parser.add_argument("--write", default="_completion/smoke/property-live-failure-states-latest.json")
    for state in REQUIRED_FAILURE_STATES:
        parser.add_argument(f"--{state.replace('_', '-')}-route", default=None)
    args = parser.parse_args()
    routes = _scenario_routes_from_environment()
    for state in REQUIRED_FAILURE_STATES:
        override = getattr(args, f"{state}_route")
        if override is not None:
            routes[state] = str(override or "").strip()
    try:
        engines = normalize_browser_engines(
            tuple(value.strip() for value in str(args.browser_engines or "").split(",") if value.strip())
        )
    except ValueError as exc:
        parser.error(str(exc))
    receipt = build_failure_state_receipt(
        base_url=str(args.base_url or "").strip(),
        scenario_routes=routes,
        browser_engines=engines,
        api_token=str(args.api_token or "").strip(),
        principal_id=str(args.principal_id or "").strip() or "pq-failure-state-gate",
        preservation_probe_route=str(args.preservation_probe_route or "").strip(),
        timeout_ms=max(1_000, int(args.timeout_ms)),
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        output_path = Path(args.write)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
