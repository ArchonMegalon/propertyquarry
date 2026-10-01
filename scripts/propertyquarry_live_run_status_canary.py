#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]

TERMINAL_STATUSES = {"processed", "completed", "completed_partial", "failed", "noop", "cancelled"}
STATUS_NOISE_TOKENS = (
    "could not load property search status",
    "checking run status",
    "suppressed_generic_listing_page",
    "starting property search run",
)
CUSTOMER_EVENT_LABELS = {
    "Preparing search",
    "Searching providers",
    "Checking details",
    "Checking listings",
    "Matching homes",
    "Ranking homes",
    "First shortlist",
    "Open property ready",
    "Applying hard rules",
    "List finished",
    "Sending update",
    "Recovery",
    "Update",
}
DEFAULT_SEARCH_PAYLOAD = {
    "selected_platforms": ["willhaben"],
    "property_preferences": {
        "country_code": "AT",
        "region_code": "vienna",
        "listing_mode": "rent",
        "property_type": ["apartment"],
        "location_query": "1020 Vienna",
        "selected_location_values": ["1020 Vienna"],
        "language_code": "en",
        "search_goal": "home",
        "search_mode": "strict",
        "max_price_eur": 1800,
        "min_area_m2": 55,
    },
    "max_results_per_source": 1,
    "force_refresh": False,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_file_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _api_headers(token: str, principal_id: str, *, accept: str) -> dict[str, str]:
    headers = {
        "User-Agent": "PropertyQuarry-live-run-status-canary/1.0",
        "Accept": accept,
        "Host": "propertyquarry.com",
        "X-EA-Principal-ID": principal_id,
    }
    token = str(token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-EA-API-Token"] = token
    return headers


def _request(
    method: str,
    url: str,
    *,
    token: str,
    principal_id: str,
    body: dict[str, Any] | None = None,
    timeout_seconds: float,
    accept: str,
    max_body_bytes: int = 220_000,
) -> dict[str, Any]:
    payload = None
    headers = _api_headers(token, principal_id, accept=accept)
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, method=method.upper(), data=payload, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body_bytes = response.read(max(16_384, int(max_body_bytes or 220_000)))
            return {
                "status_code": int(response.status),
                "headers": dict(response.headers.items()),
                "body": body_bytes,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "final_url": str(response.geturl()),
            }
    except urllib.error.HTTPError as exc:
        return {
            "status_code": int(exc.code),
            "headers": dict(exc.headers.items()),
            "body": exc.read(220_000),
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "final_url": str(exc.geturl()),
            "error": f"HTTPError: {exc}",
        }
    except Exception as exc:  # pragma: no cover - network/runtime failure path
        return {
            "status_code": 0,
            "headers": {},
            "body": b"",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "final_url": url,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _request_json(
    method: str,
    url: str,
    *,
    token: str,
    principal_id: str,
    body: dict[str, Any] | None = None,
    timeout_seconds: float,
) -> dict[str, Any]:
    response = _request(
        method,
        url,
        token=token,
        principal_id=principal_id,
        body=body,
        timeout_seconds=timeout_seconds,
        accept="application/json,text/html,*/*",
        max_body_bytes=220_000,
    )
    decoded = response.get("body", b"").decode("utf-8", errors="replace")
    try:
        payload = json.loads(decoded) if decoded else {}
    except Exception:
        payload = {}
    payload["_http"] = {
        "status_code": int(response.get("status_code") or 0),
        "duration_ms": int(response.get("duration_ms") or 0),
        "error": str(response.get("error") or "").strip(),
        "final_url": str(response.get("final_url") or "").strip(),
    }
    return payload


def _request_text(
    method: str,
    url: str,
    *,
    token: str,
    principal_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    response = _request(
        method,
        url,
        token=token,
        principal_id=principal_id,
        body=None,
        timeout_seconds=timeout_seconds,
        accept="text/html,application/json,*/*",
        max_body_bytes=700_000,
    )
    return {
        "status_code": int(response.get("status_code") or 0),
        "duration_ms": int(response.get("duration_ms") or 0),
        "error": str(response.get("error") or "").strip(),
        "final_url": str(response.get("final_url") or "").strip(),
        "text": response.get("body", b"").decode("utf-8", errors="replace"),
    }


def _contains_noise(value: object) -> bool:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return False
    if re.search(r"\b\d+\s+homes?\s+reviewed\b", lowered) or re.search(r"\b\d+\s+reviewed so far\b", lowered):
        return True
    return any(token in lowered for token in STATUS_NOISE_TOKENS)


def _strip_html(value: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))).strip()


def _extract_run_message(html_text: object) -> str:
    match = re.search(
        r'<[^>]+data-pqx-run-message[^>]*>(?P<message>.*?)</[^>]+>',
        str(html_text or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return _strip_html(match.group("message"))


def _extract_event_cards(html_text: object) -> list[dict[str, str]]:
    text = str(html_text or "")
    marker = 'data-pqx-run-events'
    marker_index = text.find(marker)
    if marker_index < 0:
        return []
    section_start = text.rfind("<div", 0, marker_index)
    if section_start < 0:
        section_start = marker_index
    section_end = text.find("</details>", marker_index)
    if section_end < 0:
        section_end = min(len(text), marker_index + 20_000)
    body = text[section_start:section_end]
    rows: list[dict[str, str]] = []
    for label, message in re.findall(
        r'<div class="pqx-event-card">\s*<strong>(.*?)</strong>\s*<span class="pqx-note">(.*?)</span>',
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        rows.append({"label": _strip_html(label), "message": _strip_html(message)})
    return rows


def _start_workspace(
    *,
    base_url: str,
    token: str,
    principal_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_json(
        "POST",
        f"{base_url.rstrip('/')}/v1/onboarding/start",
        token=token,
        principal_id=principal_id,
        body={
            "workspace_name": "PropertyQuarry live run status canary",
            "mode": "personal",
            "workspace_mode": "personal",
            "timezone": "Europe/Vienna",
            "region": "AT",
            "language": "en",
            "selected_channels": ["google"],
        },
        timeout_seconds=timeout_seconds,
    )


def _start_run(
    *,
    base_url: str,
    token: str,
    principal_id: str,
    body: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_json(
        "POST",
        f"{base_url.rstrip('/')}/app/api/property/search-runs",
        token=token,
        principal_id=principal_id,
        body=body,
        timeout_seconds=timeout_seconds,
    )


def _fetch_run_status(
    *,
    base_url: str,
    token: str,
    principal_id: str,
    run_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_json(
        "GET",
        f"{base_url.rstrip('/')}/app/api/property/search-runs/{urllib.parse.quote(run_id)}?lightweight=true",
        token=token,
        principal_id=principal_id,
        timeout_seconds=timeout_seconds,
    )


def _fetch_run_page(
    *,
    base_url: str,
    token: str,
    principal_id: str,
    run_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_text(
        "GET",
        f"{base_url.rstrip('/')}/app/properties?run_id={urllib.parse.quote(run_id)}",
        token=token,
        principal_id=principal_id,
        timeout_seconds=timeout_seconds,
    )


def _delete_run(
    *,
    base_url: str,
    token: str,
    principal_id: str,
    run_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_json(
        "DELETE",
        f"{base_url.rstrip('/')}/app/api/property/search-runs/{urllib.parse.quote(run_id)}",
        token=token,
        principal_id=principal_id,
        timeout_seconds=timeout_seconds,
    )


def build_live_run_status_canary_receipt(
    *,
    base_url: str,
    token: str,
    principal_id: str,
    timeout_seconds: float,
    poll_seconds: float,
    run_body: dict[str, Any] | None = None,
    workspace_starter: Callable[..., dict[str, Any]] | None = None,
    run_starter: Callable[..., dict[str, Any]] | None = None,
    status_fetcher: Callable[..., dict[str, Any]] | None = None,
    page_fetcher: Callable[..., dict[str, Any]] | None = None,
    run_deleter: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    workspace_runner = workspace_starter or _start_workspace
    start_runner = run_starter or _start_run
    status_reader = status_fetcher or _fetch_run_status
    page_reader = page_fetcher or _fetch_run_page
    delete_runner = run_deleter or _delete_run
    payload = dict(run_body or DEFAULT_SEARCH_PAYLOAD)
    receipt: dict[str, Any] = {
        "generated_at": _now_iso(),
        "base_url": base_url,
        "principal_id": principal_id,
        "timeout_seconds": float(timeout_seconds),
        "poll_seconds": float(poll_seconds),
        "run_payload": payload,
        "status": "failed",
    }

    workspace_result = workspace_runner(
        base_url=base_url,
        token=token,
        principal_id=principal_id,
        timeout_seconds=min(timeout_seconds, 30.0),
    )
    receipt["workspace_start"] = workspace_result

    start_result = start_runner(
        base_url=base_url,
        token=token,
        principal_id=principal_id,
        body=payload,
        timeout_seconds=min(timeout_seconds, 30.0),
    )
    receipt["run_start"] = start_result
    run_id = str(start_result.get("run_id") or "").strip()
    receipt["run_id"] = run_id

    observed_statuses: list[dict[str, Any]] = []
    latest_status_payload: dict[str, Any] = {}
    active_status_observed = str(start_result.get("status") or "").strip().lower() not in TERMINAL_STATUSES
    useful_event_present = False
    poll_deadline = time.monotonic() + max(float(timeout_seconds or 0.0), 1.0)
    if run_id:
        while time.monotonic() < poll_deadline:
            latest_status_payload = status_reader(
                base_url=base_url,
                token=token,
                principal_id=principal_id,
                run_id=run_id,
                timeout_seconds=min(25.0, timeout_seconds),
            )
            status_value = str(latest_status_payload.get("status") or "").strip().lower()
            current_step = str(latest_status_payload.get("current_step") or "").strip()
            run_message = str(latest_status_payload.get("message") or "").strip()
            events = [
                dict(row)
                for row in list(latest_status_payload.get("events") or [])
                if isinstance(row, dict)
            ]
            useful_events = [
                row
                for row in events
                if not _contains_noise(row.get("message"))
            ]
            observed_statuses.append(
                {
                    "status": status_value,
                    "current_step": current_step,
                    "message": run_message[:220],
                    "event_count": len(events),
                    "useful_event_count": len(useful_events),
                }
            )
            if status_value and status_value not in TERMINAL_STATUSES:
                active_status_observed = True
            if useful_events:
                useful_event_present = True
            if active_status_observed and useful_event_present:
                break
            if status_value in TERMINAL_STATUSES and useful_event_present:
                break
            time.sleep(max(1.0, float(poll_seconds or 1.0)))
    receipt["observed_statuses"] = observed_statuses
    receipt["status_payload"] = latest_status_payload

    page_result = (
        page_reader(
            base_url=base_url,
            token=token,
            principal_id=principal_id,
            run_id=run_id,
            timeout_seconds=min(timeout_seconds, 30.0),
        )
        if run_id
        else {"status_code": 0, "text": "", "error": "run_id_missing"}
    )
    receipt["run_page"] = {
        "status_code": int(page_result.get("status_code") or 0),
        "duration_ms": int(page_result.get("duration_ms") or 0),
        "error": str(page_result.get("error") or "").strip(),
        "final_url": str(page_result.get("final_url") or "").strip(),
    }
    page_text = str(page_result.get("text") or "")
    run_message_text = _extract_run_message(page_text)
    event_cards = _extract_event_cards(page_text)
    receipt["page_run_message"] = run_message_text
    receipt["page_event_cards"] = event_cards

    cleanup_result = (
        delete_runner(
            base_url=base_url,
            token=token,
            principal_id=principal_id,
            run_id=run_id,
            timeout_seconds=min(timeout_seconds, 20.0),
        )
        if run_id
        else {"deleted": False, "reason": "run_id_missing"}
    )
    receipt["cleanup"] = cleanup_result

    payload_events = [
        dict(row)
        for row in list(latest_status_payload.get("events") or [])
        if isinstance(row, dict)
    ]
    if run_id and (not run_message_text or not event_cards):
        page_deadline = time.monotonic() + min(20.0, max(4.0, float(timeout_seconds or 0.0) / 3.0))
        while time.monotonic() < page_deadline and (not run_message_text or not event_cards):
            time.sleep(max(1.0, float(poll_seconds or 1.0)))
            refreshed_page_result = page_reader(
                base_url=base_url,
                token=token,
                principal_id=principal_id,
                run_id=run_id,
                timeout_seconds=min(timeout_seconds, 30.0),
            )
            refreshed_page_text = str(refreshed_page_result.get("text") or "")
            refreshed_run_message_text = _extract_run_message(refreshed_page_text)
            refreshed_event_cards = _extract_event_cards(refreshed_page_text)
            page_result = refreshed_page_result
            if refreshed_run_message_text:
                run_message_text = refreshed_run_message_text
            if refreshed_event_cards:
                event_cards = refreshed_event_cards
            receipt["run_page"] = {
                "status_code": int(page_result.get("status_code") or 0),
                "duration_ms": int(page_result.get("duration_ms") or 0),
                "error": str(page_result.get("error") or "").strip(),
                "final_url": str(page_result.get("final_url") or "").strip(),
            }
            receipt["page_run_message"] = run_message_text
            receipt["page_event_cards"] = event_cards
            if run_message_text and event_cards:
                break

    checks = [
        {
            "name": "workspace_start_ok",
            "ok": int(dict(workspace_result.get("_http") or {}).get("status_code") or 0) == 200,
        },
        {
            "name": "run_start_ok",
            "ok": bool(run_id) and int(dict(start_result.get("_http") or {}).get("status_code") or 0) == 200,
        },
        {
            "name": "active_status_observed",
            "ok": active_status_observed,
        },
        {
            "name": "useful_event_present",
            "ok": useful_event_present and bool(payload_events),
        },
        {
            "name": "payload_event_noise_filtered",
            "ok": bool(payload_events) and not any(_contains_noise(row.get("message")) for row in payload_events),
        },
        {
            "name": "run_page_loaded",
            "ok": int(page_result.get("status_code") or 0) == 200,
        },
        {
            "name": "page_run_message_noise_free",
            "ok": bool(run_message_text) and not _contains_noise(run_message_text),
        },
        {
            "name": "page_event_cards_present",
            "ok": bool(event_cards),
        },
        {
            "name": "page_event_labels_customer_facing",
            "ok": bool(event_cards) and all(str(row.get("label") or "") in CUSTOMER_EVENT_LABELS for row in event_cards),
        },
        {
            "name": "page_event_messages_noise_free",
            "ok": bool(event_cards) and not any(_contains_noise(row.get("message")) for row in event_cards),
        },
        {
            "name": "cleanup_deleted",
            "ok": bool(cleanup_result.get("deleted")),
        },
    ]
    receipt["checks"] = checks
    receipt["failed_checks"] = [row["name"] for row in checks if not bool(row.get("ok"))]
    receipt["status"] = "pass" if not receipt["failed_checks"] else "fail"
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the live PropertyQuarry running-search status trail on a real in-progress run.")
    parser.add_argument("--api-base", default="http://127.0.0.1:8097")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--principal-id", default="")
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--write", default="")
    args = parser.parse_args()

    env_values = {**_env_file_values(Path(args.env_file)), **os.environ}
    token = str(env_values.get("EA_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("EA_API_TOKEN missing")
    principal_id = str(args.principal_id or "").strip() or f"pq-live-run-status-canary-{int(time.time())}"
    receipt = build_live_run_status_canary_receipt(
        base_url=str(args.api_base),
        token=token,
        principal_id=principal_id,
        timeout_seconds=max(15.0, float(args.timeout_seconds)),
        poll_seconds=max(1.0, float(args.poll_seconds)),
    )
    if args.write:
        output_path = Path(args.write)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
