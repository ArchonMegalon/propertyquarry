#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from scripts.propertyquarry_playwright_runtime import playwright_chromium_launch_kwargs
except ModuleNotFoundError:
    from propertyquarry_playwright_runtime import playwright_chromium_launch_kwargs  # type: ignore[no-redef]


_SDK_BOOTSTRAP_PATTERN = re.compile(
    r"https://static\.matterport\.com/showcase-sdk/bootstrap/[^\s\"']+/sdk\.js(?:\?[^\s\"']*)?"
)
_SENSITIVE_QUERY_PATTERN = re.compile(
    r"(?i)(applicationKey|sdkKey|access_token)=([^&#\s\"']+)"
)
_MOCK_SDK = r"""
  window.__PROPERTYQUARRY_MOCK_SDK_MOVES__ = [];
  window.MP_SDK = {
    connect: async function () {
      let observer = null;
      return {
        Camera: { TransitionType: { FLY: 'FLY' } },
        Sweep: {
          current: {
            subscribe: function (callback) { observer = callback; }
          },
          moveTo: async function (id, options) {
            await new Promise((resolve) => setTimeout(resolve, 50));
            window.__PROPERTYQUARRY_MOCK_SDK_MOVES__.push({
              id: String(id || ''),
              transition: String(options?.transition || ''),
              transitionTime: Number(options?.transitionTime || 0),
            });
            if (observer) observer({ id: String(id || '') });
            return String(id || '');
          }
        }
      };
    }
  };
"""
_FRAME_PROBE = r"""
  (() => {
    const intervals = [];
    const walkthroughEvents = [];
    window.__PROPERTYQUARRY_WALKTHROUGH_EVENTS__ = walkthroughEvents;
    window.addEventListener('propertyquarry:matterport-walkthrough', (event) => {
      const detail = event?.detail || {};
      walkthroughEvents.push({
        status: String(detail.status || ''),
        step_index: Number(detail.step_index || 0),
        route_node_count: Number(detail.route_node_count || 0),
      });
    });
    let previous = 0;
    const sample = (timestamp) => {
      if (previous > 0 && intervals.length < 18000) intervals.push(timestamp - previous);
      previous = timestamp;
      window.__PROPERTYQUARRY_FRAME_INTERVALS__ = intervals;
      requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  })();
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sanitize_text(value: object) -> str:
    return _SENSITIVE_QUERY_PATTERN.sub(r"\1=[redacted]", str(value or ""))[:1000]


def _safe_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    safe_query = []
    for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {"applicationkey", "sdkkey", "access_token"}:
            safe_query.append((key, "[redacted]"))
        else:
            safe_query.append((key, item))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(safe_query), "")
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return float(ordered[index])


def _frame_metrics(raw_intervals: object) -> dict[str, object]:
    intervals = [
        float(value)
        for value in list(raw_intervals or [])
        if isinstance(value, (int, float)) and 0.0 < float(value) <= 1000.0
    ]
    if not intervals:
        return {
            "sample_count": 0,
            "mean_fps": 0.0,
            "p95_frame_interval_ms": 0.0,
            "long_frame_count": 0,
        }
    mean_interval = sum(intervals) / len(intervals)
    return {
        "sample_count": len(intervals),
        "mean_fps": round(1000.0 / mean_interval, 2),
        "p95_frame_interval_ms": round(_percentile(intervals, 0.95), 2),
        "long_frame_count": sum(1 for interval in intervals if interval > 34.0),
    }


def _unexpected_console_errors(messages: list[str]) -> list[str]:
    unexpected: list[str] = []
    for message in messages:
        lowered = message.lower()
        if (
            "cross-origin-opener-policy header has been ignored" in lowered
            and "origin was untrustworthy" in lowered
        ):
            continue
        unexpected.append(message)
    return unexpected


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _mock_provider_route(route: Any) -> None:
    url = str(route.request.url or "")
    if _SDK_BOOTSTRAP_PATTERN.fullmatch(url):
        route.fulfill(status=200, content_type="application/javascript", body=_MOCK_SDK)
        return
    if url.startswith("https://my.matterport.com/show"):
        route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><html><body><main>Mock Matterport viewer</main></body></html>",
        )
        return
    route.continue_()


def run_gate(args: argparse.Namespace) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as error:
        raise RuntimeError("playwright_not_installed") from error

    console_errors: list[str] = []
    page_errors: list[str] = []
    started = time.monotonic()
    screenshot_path = Path(args.screenshot).expanduser().resolve()
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**playwright_chromium_launch_kwargs(playwright))
        context = browser.new_context(
            viewport={"width": args.viewport_width, "height": args.viewport_height},
            device_scale_factor=args.device_scale_factor,
            reduced_motion="no-preference",
        )
        context.add_init_script(_FRAME_PROBE)
        page = context.new_page()
        page.on(
            "console",
            lambda message: console_errors.append(_sanitize_text(message.text))
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: page_errors.append(_sanitize_text(error)))
        if args.mock_provider:
            page.route("**/*", _mock_provider_route)

        response = page.goto(
            args.url,
            wait_until="domcontentloaded",
            timeout=int(args.timeout_seconds * 1000),
        )
        if response is None or not response.ok:
            status = int(response.status) if response is not None else 0
            raise RuntimeError(f"walkthrough_http_status_{status}")
        page.wait_for_function(
            "['pass', 'fail', 'manual'].includes(window.__PROPERTYQUARRY_MATTERPORT_WALKTHROUGH__?.status)",
            timeout=int(args.timeout_seconds * 1000),
        )
        page.wait_for_timeout(250)
        browser_result = dict(
            page.evaluate(
                """() => {
                  const contractNode = document.getElementById('matterport-walkthrough');
                  const contract = JSON.parse(contractNode?.textContent || '{}');
                  return {
                    proof: window.__PROPERTYQUARRY_MATTERPORT_WALKTHROUGH__ || {},
                    mock_moves: window.__PROPERTYQUARRY_MOCK_SDK_MOVES__ || [],
                    contract,
                    frame_intervals: window.__PROPERTYQUARRY_FRAME_INTERVALS__ || [],
                    document_state: document.documentElement.dataset.matterportWalkthroughState || '',
                    title: document.title,
                    viewport: { width: window.innerWidth, height: window.innerHeight },
                    horizontal_overflow_px: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
                    provider_frame_bounds: (() => {
                      const rect = document.getElementById('provider-frame')?.getBoundingClientRect();
                      return rect ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height } : null;
                    })(),
                    overlay_bounds: (() => {
                      const rect = document.querySelector('.shell')?.getBoundingClientRect();
                      return rect ? { width: rect.width, height: rect.height } : null;
                    })(),
                    control: (() => {
                      const button = document.querySelector('[data-matterport-walkthrough-toggle]');
                      return button ? {
                        label: button.getAttribute('aria-label') || '',
                        disabled: Boolean(button.disabled),
                      } : null;
                    })(),
                    event_statuses: (window.__PROPERTYQUARRY_WALKTHROUGH_EVENTS__ || []).map((event) => event.status),
                  };
                }"""
            )
            or {}
        )
        page.screenshot(path=str(screenshot_path), full_page=False)
        final_url = _safe_url(page.url)
        context.close()
        browser.close()

    contract = dict(browser_result.get("contract") or {})
    proof = dict(browser_result.get("proof") or {})
    steps = [dict(step) for step in list(contract.get("steps") or []) if isinstance(step, dict)]
    expected_sweeps = [str(step.get("sweep_id") or "") for step in steps]
    covered_rooms = {str(step.get("room_id") or "") for step in steps if str(step.get("room_id") or "")}
    walkable_rooms = {str(room_id or "") for room_id in list(contract.get("walkable_room_ids") or [])}
    mock_moves = [dict(move) for move in list(browser_result.get("mock_moves") or []) if isinstance(move, dict)]
    arrived_sweeps = [str(value or "") for value in list(proof.get("arrived_sweeps") or [])]
    observed_sequence = [str(move.get("id") or "") for move in mock_moves] if args.mock_provider else arrived_sweeps

    console_errors = _unexpected_console_errors(console_errors)
    failures: list[str] = []
    if proof.get("status") != "pass":
        failures.append(f"walkthrough_status_{proof.get('status') or 'missing'}")
    if proof.get("transition") != "fly" or contract.get("transition") != "fly":
        failures.append("walkthrough_transition_not_fly")
    if len(steps) < 2:
        failures.append("walkthrough_route_too_short")
    if not walkable_rooms or not walkable_rooms.issubset(covered_rooms):
        failures.append("walkthrough_room_coverage_missing")
    if observed_sequence != expected_sweeps:
        failures.append("walkthrough_sweep_sequence_mismatch")
    if args.mock_provider and any(str(move.get("transition") or "") != "FLY" for move in mock_moves):
        failures.append("walkthrough_non_fly_move_observed")
    if int(browser_result.get("horizontal_overflow_px") or 0) > 1:
        failures.append("walkthrough_horizontal_overflow")
    control = dict(browser_result.get("control") or {})
    if control.get("label") != "Replay walkthrough" or bool(control.get("disabled")):
        failures.append("walkthrough_terminal_control_invalid")
    event_statuses = [str(status or "") for status in list(browser_result.get("event_statuses") or [])]
    if "running" not in event_statuses or "pass" not in event_statuses:
        failures.append("walkthrough_observability_events_missing")
    viewport = dict(browser_result.get("viewport") or {})
    frame_bounds = dict(browser_result.get("provider_frame_bounds") or {})
    viewport_width = float(viewport.get("width") or 0.0)
    viewport_height = float(viewport.get("height") or 0.0)
    if (
        viewport_width <= 0.0
        or viewport_height <= 0.0
        or abs(float(frame_bounds.get("x") or 0.0)) > 1.0
        or abs(float(frame_bounds.get("y") or 0.0)) > 1.0
        or float(frame_bounds.get("width") or 0.0) < viewport_width - 1.0
        or float(frame_bounds.get("height") or 0.0) < viewport_height - 1.0
    ):
        failures.append("walkthrough_provider_not_full_bleed")
    if page_errors:
        failures.append("walkthrough_page_error")
    if console_errors:
        failures.append("walkthrough_console_error")

    frame_metrics = _frame_metrics(browser_result.get("frame_intervals"))
    if not args.mock_provider:
        if int(frame_metrics["sample_count"]) < args.min_frame_samples:
            failures.append("walkthrough_frame_samples_insufficient")
        if float(frame_metrics["mean_fps"]) < args.min_mean_fps:
            failures.append("walkthrough_mean_fps_below_threshold")

    return {
        "contract_name": "propertyquarry.matterport_sdk_walkthrough_browser_gate.v1",
        "status": "pass" if not failures else "fail",
        "generated_at": _utc_now(),
        "url": final_url,
        "proof_mode": "mocked_sdk_transport" if args.mock_provider else "live_matterport_sdk",
        "viewport": viewport,
        "device_scale_factor": args.device_scale_factor,
        "duration_seconds": round(time.monotonic() - started, 3),
        "document_title": str(browser_result.get("title") or "")[:200],
        "document_state": str(browser_result.get("document_state") or ""),
        "model_sid": str(contract.get("model_sid") or ""),
        "route_node_count": len(steps),
        "walkable_room_count": len(walkable_rooms),
        "covered_room_count": len(covered_rooms & walkable_rooms),
        "transition": str(proof.get("transition") or ""),
        "arrived_sweep_count": len(arrived_sweeps),
        "observed_sweep_count": len(observed_sequence),
        "frame_metrics": frame_metrics,
        "horizontal_overflow_px": int(browser_result.get("horizontal_overflow_px") or 0),
        "provider_frame_bounds": frame_bounds,
        "overlay_bounds": browser_result.get("overlay_bounds") or {},
        "terminal_control": control,
        "event_statuses": event_statuses,
        "console_errors": console_errors[:20],
        "page_errors": page_errors[:20],
        "failures": failures,
        "screenshot_path": str(screenshot_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate PropertyQuarry's Matterport SDK walkthrough in a browser.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--screenshot", required=True)
    parser.add_argument("--mock-provider", action="store_true")
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=900)
    parser.add_argument("--device-scale-factor", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--min-frame-samples", type=int, default=120)
    parser.add_argument("--min-mean-fps", type=float, default=50.0)
    args = parser.parse_args()

    receipt_path = Path(args.receipt).expanduser().resolve()
    try:
        receipt = run_gate(args)
    except Exception as error:
        receipt = {
            "contract_name": "propertyquarry.matterport_sdk_walkthrough_browser_gate.v1",
            "status": "fail",
            "generated_at": _utc_now(),
            "url": _safe_url(args.url),
            "proof_mode": "mocked_sdk_transport" if args.mock_provider else "live_matterport_sdk",
            "failures": [_sanitize_text(error)],
        }
    _write_receipt(receipt_path, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
