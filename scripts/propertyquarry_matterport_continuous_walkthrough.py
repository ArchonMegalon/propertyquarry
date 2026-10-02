#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import signal
import subprocess
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


DEFAULT_ROUTE = (
    ROOT
    / "_completion/smoke/propertyquarry-candidate-ux-20260710/matterport-continuous-route.json"
)
DEFAULT_TOPOLOGY = (
    ROOT
    / "_completion/smoke/propertyquarry-candidate-ux-20260710/matterport-topology-raw.json"
)
DEFAULT_SCREENSHOT = (
    ROOT
    / "_completion/smoke/propertyquarry-candidate-ux-20260710/screenshots"
    / "matterport-continuous-route-probe.png"
)
DEFAULT_RECEIPT = (
    ROOT
    / "_completion/smoke/propertyquarry-candidate-ux-20260710"
    / "matterport-continuous-route-browser-proof.json"
)
DEFAULT_STORAGE_STATE = (
    ROOT
    / "_completion/smoke/propertyquarry-candidate-ux-20260710"
    / "matterport-public-browser-storage-state.json"
)
EVENT_NAMES = {"pano_viewed", "room_visited"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _selected_fields(value: object) -> dict[str, object]:
    allowed = {
        "event",
        "event_name",
        "name",
        "type",
        "pano_id",
        "room_id",
        "room_index",
        "room_count",
        "model_id",
        "model_sid",
        "timestamp",
        "timestamp_ms",
    }
    selected: dict[str, object] = {}
    if isinstance(value, list):
        for item in value:
            selected.update(_selected_fields(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in allowed and not isinstance(item, (dict, list)):
                selected[lowered] = item
            if isinstance(item, (dict, list)):
                selected.update(_selected_fields(item))
    return selected


def _walk_json(value: object) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    if isinstance(value, list):
        for item in value:
            matches.extend(_walk_json(item))
        return matches
    if not isinstance(value, dict):
        return matches

    lowered = {str(key).lower(): item for key, item in value.items()}
    names = {
        str(lowered.get(key) or "").strip().lower()
        for key in ("event", "event_name", "name", "type")
    }
    if names & EVENT_NAMES:
        matches.append(_selected_fields(value))
    for item in value.values():
        matches.extend(_walk_json(item))
    return matches


def _analytics_events(post_data: str) -> list[dict[str, object]]:
    text = str(post_data or "")
    if not any(name in text.lower() for name in EVENT_NAMES):
        return []
    candidates = [text]
    try:
        decoded = urllib.parse.unquote_plus(text)
    except Exception:
        decoded = text
    if decoded != text:
        candidates.append(decoded)
    matches: list[dict[str, object]] = []
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        matches.extend(_walk_json(payload))
    if matches:
        unique: dict[str, dict[str, object]] = {}
        for match in matches:
            unique[json.dumps(match, sort_keys=True, default=str)] = match
        return list(unique.values())
    return [{"raw_event_marker": name} for name in sorted(EVENT_NAMES) if name in decoded.lower()]


def _route_url(
    route: dict[str, object],
    *,
    play_mode: str,
    start_selector: str,
    start_rotation: str,
    start_value: str,
) -> str:
    model_sid = str(route.get("model_sid") or "").strip()
    nodes = list(route.get("route") or [])
    if not model_sid or not nodes or not isinstance(nodes[0], dict):
        raise RuntimeError("matterport_route_contract_invalid")
    first = dict(nodes[0])
    selector_fields = {"id": "id", "index": "index", "uuid": "sweep_uuid"}
    selected = str(start_value or first.get(selector_fields[start_selector]) or "")
    start = str(int(selected) + 1) if start_selector == "index" else selected
    query = urllib.parse.urlencode(
        {
            "m": model_sid,
            "play": play_mode,
            "qs": "1",
            "brand": "0",
            "help": "0",
            "hl": "0",
            "ss": start,
            "sr": start_rotation,
        }
    )
    return f"https://my.matterport.com/show/?{query}"


def _canvas_state(page: Any) -> dict[str, object]:
    return dict(
        page.evaluate(
            """() => {
              const canvases = Array.from(document.querySelectorAll('canvas'));
              const visible = canvases.filter((node) => {
                const box = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return box.width > 0 && box.height > 0
                  && style.display !== 'none' && style.visibility !== 'hidden';
              });
              const largest = visible.sort((a, b) => {
                const aa = a.getBoundingClientRect();
                const bb = b.getBoundingClientRect();
                return (bb.width * bb.height) - (aa.width * aa.height);
              })[0];
              const box = largest?.getBoundingClientRect();
              return {
                canvas_count: canvases.length,
                visible_canvas_count: visible.length,
                largest_canvas: box ? {
                  x: Math.round(box.x), y: Math.round(box.y),
                  width: Math.round(box.width), height: Math.round(box.height),
                } : null,
                title: document.title,
                body_text: (document.body?.innerText || '').slice(0, 1000),
                controls: Array.from(document.querySelectorAll('button, [role="button"], input'))
                  .filter((node) => {
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                      && style.display !== 'none' && style.visibility !== 'hidden';
                  })
                  .map((node) => ({
                    tag: node.tagName.toLowerCase(),
                    aria_label: node.getAttribute('aria-label') || '',
                    title: node.getAttribute('title') || '',
                    name: node.getAttribute('name') || '',
                    type: node.getAttribute('type') || '',
                    value: node.tagName === 'INPUT' ? (node.value || '').slice(0, 300) : '',
                    text: (node.textContent || '').trim().slice(0, 120),
                  })).slice(0, 100),
              };
            }"""
        )
        or {}
    )


def _bearing(source: dict[str, object], target: dict[str, object]) -> float:
    source_position = dict(source.get("position") or {})
    target_position = dict(target.get("position") or {})
    return math.atan2(
        float(target_position.get("y") or 0.0) - float(source_position.get("y") or 0.0),
        float(target_position.get("x") or 0.0) - float(source_position.get("x") or 0.0),
    )


def _signed_angle(target: float, current: float) -> float:
    return (target - current + math.pi) % (2 * math.pi) - math.pi


def _turn_hold_ms(angle: float, *, slope: float, intercept: float) -> int:
    magnitude = abs(float(angle))
    if magnitude <= intercept:
        return 80
    return max(80, round(((magnitude - intercept) / slope) * 1000))


def _focus_canvas(page: Any) -> None:
    page.locator("canvas").first.evaluate(
        """(node) => {
          node.setAttribute('tabindex', '-1');
          node.focus({preventScroll: true});
        }"""
    )


def _activate_canvas(page: Any) -> None:
    canvas = page.locator("canvas").first
    box = canvas.bounding_box()
    if not box:
        raise RuntimeError("matterport_canvas_bounds_unavailable")
    page.mouse.click(
        float(box["x"]) + float(box["width"]) / 2,
        float(box["y"]) + float(box["height"]) * 0.2,
    )


def _capture_page_png(page: Any, context: Any, screenshot_path: Path) -> None:
    session = context.new_cdp_session(page)
    try:
        result = session.send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
            },
        )
    finally:
        session.detach()
    encoded = str(result.get("data") or "")
    if not encoded:
        raise RuntimeError("matterport_screenshot_data_missing")
    screenshot_path.write_bytes(base64.b64decode(encoded))


def _hold_key(page: Any, key: str, *, duration_ms: int, input_mode: str) -> None:
    if input_mode == "x11":
        x11_key = {"ArrowUp": "Up", "ArrowDown": "Down"}.get(key, key)
        subprocess.run(["xdotool", "keydown", x11_key], check=True)
        page.wait_for_timeout(duration_ms)
        subprocess.run(["xdotool", "keyup", x11_key], check=True)
        return
    page.keyboard.down(key)
    page.wait_for_timeout(duration_ms)
    page.keyboard.up(key)


def _activate_x11_page(page: Any) -> None:
    window_ids = subprocess.check_output(
        ["xdotool", "search", "--onlyvisible", "--name", page.title()],
        text=True,
    ).split()
    if not window_ids:
        raise RuntimeError("matterport_x11_window_not_found")
    subprocess.run(
        ["xdotool", "windowfocus", "--sync", window_ids[-1]],
        check=True,
    )
    geometry = dict(
        page.evaluate(
            """() => ({
              screenX: window.screenX,
              screenY: window.screenY,
              outerWidth: window.outerWidth,
              outerHeight: window.outerHeight,
              innerWidth: window.innerWidth,
              innerHeight: window.innerHeight,
            })"""
        )
        or {}
    )
    inner_width = float(geometry.get("innerWidth") or 0)
    inner_height = float(geometry.get("innerHeight") or 0)
    if inner_width <= 0 or inner_height <= 0:
        raise RuntimeError("matterport_x11_window_geometry_invalid")
    horizontal_chrome = max(0.0, float(geometry.get("outerWidth") or 0) - inner_width)
    vertical_chrome = max(0.0, float(geometry.get("outerHeight") or 0) - inner_height)
    x = float(geometry.get("screenX") or 0) + horizontal_chrome / 2 + inner_width / 2
    y = float(geometry.get("screenY") or 0) + vertical_chrome + inner_height * 0.2
    subprocess.run(
        ["xdotool", "mousemove", "--sync", str(round(x)), str(round(y)), "click", "1"],
        check=True,
    )


def _goto_with_retries(page: Any, url: str, *, timeout_ms: int, attempts: int = 3) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except Exception as error:
            last_error = error
            if attempt == attempts:
                break
            page.wait_for_timeout(attempt * 1000)
    raise RuntimeError("matterport_initial_navigation_failed") from last_error


def _drag_canvas_horizontally(page: Any, pixels: float, *, duration_ms: int) -> None:
    canvas = page.locator("canvas").first
    box = canvas.bounding_box()
    if not box:
        raise RuntimeError("matterport_canvas_bounds_unavailable")
    usable_width = max(1.0, float(box["width"]) * 0.7)
    if abs(pixels) > usable_width:
        raise RuntimeError("matterport_mouse_turn_exceeds_canvas")
    center_y = float(box["y"]) + float(box["height"]) / 2
    if pixels >= 0:
        start_x = float(box["x"]) + float(box["width"]) * 0.15
    else:
        start_x = float(box["x"]) + float(box["width"]) * 0.85
    page.mouse.move(start_x, center_y)
    page.mouse.down()
    page.mouse.move(
        start_x + pixels,
        center_y,
        steps=max(2, round(max(16, duration_ms) / 16)),
    )
    page.mouse.up()


def _drag_canvas_by_angle(
    page: Any,
    angle: float,
    *,
    pixels_per_radian: float,
    duration_ms_per_radian: float,
) -> tuple[float, int, int]:
    canvas = page.locator("canvas").first
    box = canvas.bounding_box()
    if not box:
        raise RuntimeError("matterport_canvas_bounds_unavailable")
    total_pixels = -float(angle) * pixels_per_radian
    max_segment_pixels = max(1.0, float(box["width"]) * 0.7)
    segment_count = max(1, math.ceil(abs(total_pixels) / max_segment_pixels))
    segment_pixels = total_pixels / segment_count
    total_duration_ms = max(30, round(abs(float(angle)) * duration_ms_per_radian))
    segment_duration_ms = max(30, round(total_duration_ms / segment_count))
    for segment in range(segment_count):
        _drag_canvas_horizontally(page, segment_pixels, duration_ms=segment_duration_ms)
        if segment + 1 < segment_count:
            page.wait_for_timeout(50)
    return total_pixels, total_duration_ms, segment_count


def probe_start(
    *,
    route_path: Path,
    topology_path: Path,
    storage_state_path: Path,
    receipt_path: Path,
    screenshot_path: Path,
    play_mode: str,
    start_selector: str,
    start_rotation: str,
    start_value: str,
    turn_key: str,
    turn_hold_ms: int,
    mouse_turn_pixels: float,
    mouse_turn_duration_ms: int,
    move_key: str,
    key_hold_ms: int,
    navigation_click_x: float | None,
    navigation_click_y: float | None,
    open_share: bool,
    target_sweeps: list[str],
    route_actions_path: Path | None,
    max_route_actions: int,
    world_heading_radians: float,
    turn_slope_radians_per_second: float,
    turn_intercept_radians: float,
    turn_deadband_radians: float,
    route_turn_mode: str,
    mouse_pixels_per_radian: float,
    mouse_duration_ms_per_radian: float,
    renderer_mode: str,
    headed: bool,
    input_mode: str,
    require_all_walkable_rooms: bool,
    timeout_seconds: float,
) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    route = json.loads(route_path.read_text(encoding="utf-8"))
    topology = json.loads(topology_path.read_text(encoding="utf-8"))
    locations = {
        str(location.get("id") or ""): dict(location)
        for location in list(topology.get("locations") or [])
        if isinstance(location, dict) and location.get("id")
    }
    route_actions: list[dict[str, object]] = []
    if route_actions_path is not None:
        loaded_actions = json.loads(route_actions_path.read_text(encoding="utf-8"))
        if isinstance(loaded_actions, dict):
            loaded_actions = loaded_actions.get("actions")
        if not isinstance(loaded_actions, list):
            raise RuntimeError("matterport_route_actions_invalid")
        route_actions = [dict(item) for item in loaded_actions if isinstance(item, dict)]
        if max_route_actions > 0:
            route_actions = route_actions[:max_route_actions]
        target_sweeps = [str(item.get("target_sweep_id") or "") for item in route_actions]
        if not target_sweeps or any(not item for item in target_sweeps):
            raise RuntimeError("matterport_route_action_target_missing")
    nodes = list(route.get("route") or [])
    selector_fields = {"id": "id", "index": "index", "uuid": "sweep_uuid"}
    selected_value = str(start_value or dict(nodes[0]).get(selector_fields[start_selector]) or "")
    expected_start = next(
        (
            dict(node)
            for node in nodes
            if isinstance(node, dict)
            and (
                ""
                if node.get(selector_fields[start_selector]) is None
                else str(node.get(selector_fields[start_selector]))
            )
            == selected_value
        ),
        {},
    )
    if not expected_start:
        for location in locations.values():
            pano = dict(location.get("pano") or {})
            candidate = {
                "id": location.get("id"),
                "index": location.get("index"),
                "neighbors": list(location.get("neighbors") or []),
                "position": dict(location.get("position") or {}),
                "room_id": dict(location.get("room") or {}).get("id"),
                "sweep_uuid": pano.get("sweepUuid"),
            }
            candidate_selector = candidate.get(selector_fields[start_selector])
            if (
                "" if candidate_selector is None else str(candidate_selector)
            ) == selected_value:
                expected_start = candidate
                break
    if not expected_start:
        raise RuntimeError("matterport_route_start_not_found")
    url = _route_url(
        route,
        play_mode=play_mode,
        start_selector=start_selector,
        start_rotation=start_rotation,
        start_value=selected_value,
    )
    events: list[dict[str, object]] = []
    marked_requests: list[dict[str, object]] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    main_frame_navigations: list[dict[str, object]] = []
    route_steps: list[dict[str, object]] = []
    screenshot_error = ""
    started = time.monotonic()

    def write_checkpoint(phase: str, **details: object) -> None:
        checkpoint: dict[str, object] = {
            "contract_name": "propertyquarry.matterport_continuous_walkthrough_probe.v1",
            "status": "incomplete",
            "phase": phase,
            "generated_at": _utc_now(),
            "model_sid": route.get("model_sid"),
            "renderer_mode": renderer_mode,
            "target_sweeps": target_sweeps,
            "route_steps": route_steps,
            "pano_events": [event for event in events if event.get("pano_id")],
            "room_events": [event for event in events if event.get("room_id")],
            "main_frame_navigations": main_frame_navigations,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        checkpoint.update(details)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(checkpoint, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    with sync_playwright() as playwright:
        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--autoplay-policy=no-user-gesture-required",
        ]
        if renderer_mode == "swiftshader":
            launch_args.extend(["--use-gl=angle", "--use-angle=swiftshader"])
        launch_kwargs = playwright_chromium_launch_kwargs(playwright, args=launch_args)
        launch_kwargs["headless"] = not headed
        browser = playwright.chromium.launch(**launch_kwargs)
        context_options: dict[str, object] = {
            "viewport": {"width": 1920, "height": 1080},
            "device_scale_factor": 1,
            "locale": "en-US",
        }
        if storage_state_path.is_file():
            context_options["storage_state"] = str(storage_state_path)
        context = browser.new_context(
            **context_options,
        )
        page = context.new_page()

        def on_request(request: Any) -> None:
            post_data = str(request.post_data or "")
            extracted = _analytics_events(post_data)
            if not extracted:
                return
            now_ms = round((time.monotonic() - started) * 1000)
            for event in extracted:
                events.append({"elapsed_ms": now_ms, **event})
            marked_requests.append(
                {
                    "elapsed_ms": now_ms,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "url": _safe_url(request.url),
                    "events": extracted,
                }
            )

        page.on("request", on_request)
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "framenavigated",
            lambda frame: main_frame_navigations.append(
                {
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "url": _safe_url(frame.url),
                }
            )
            if frame == page.main_frame
            else None,
        )
        _goto_with_retries(page, url, timeout_ms=int(timeout_seconds * 1000))
        try:
            page.locator("canvas").first.wait_for(state="visible", timeout=int(timeout_seconds * 1000))
        except Exception:
            pass
        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(storage_state_path))
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            page.wait_for_timeout(500)
            if any(
                str(event.get("pano_id") or "") == str(expected_start.get("id") or "")
                for event in events
            ):
                break
        page.wait_for_timeout(1500)
        write_checkpoint("viewer_ready", final_url=page.url)
        turn_pressed_at_ms: int | None = None
        move_event_start = len([event for event in events if event.get("pano_id")])
        move_pressed_at_ms: int | None = None
        if target_sweeps:
            if input_mode != "x11":
                _focus_canvas(page)
                page.wait_for_timeout(250)
            current_id = str(expected_start.get("id") or "")
            current_heading = float(world_heading_radians)
            action_rows = route_actions or [
                {"target_sweep_id": target_id} for target_id in target_sweeps
            ]
            for ordinal, action in enumerate(action_rows, start=1):
                target_id = str(action.get("target_sweep_id") or "")
                source = locations.get(current_id) or {}
                target = locations.get(str(target_id)) or {}
                neighbors = [str(item) for item in list(source.get("neighbors") or [])]
                graph_edge_declared = bool(source and target and str(target_id) in neighbors)
                if not graph_edge_declared:
                    route_steps.append(
                        {
                            "ordinal": ordinal,
                            "source_sweep_id": current_id,
                            "expected_sweep_id": str(target_id),
                            "status": "fail",
                            "error": "target_is_not_a_declared_neighbor",
                        }
                    )
                    break
                target_heading = (
                    float(action["yaw_radians"])
                    if "yaw_radians" in action
                    else _bearing(source, target)
                )
                turn_angle = _signed_angle(target_heading, current_heading)
                route_turn_key = "l" if turn_angle > 0 else "j"
                route_turn_hold_ms = 0
                route_turn_drag_pixels = 0.0
                route_turn_duration_ms = 0
                route_turn_segment_count = 0
                turn_started_ms: int | None = None
                if abs(turn_angle) > turn_deadband_radians:
                    turn_started_ms = round((time.monotonic() - started) * 1000)
                    if input_mode != "x11" or route_turn_mode == "mouse":
                        _focus_canvas(page)
                    else:
                        _activate_x11_page(page)
                    if route_turn_mode == "mouse":
                        (
                            route_turn_drag_pixels,
                            route_turn_duration_ms,
                            route_turn_segment_count,
                        ) = _drag_canvas_by_angle(
                            page,
                            turn_angle,
                            pixels_per_radian=mouse_pixels_per_radian,
                            duration_ms_per_radian=mouse_duration_ms_per_radian,
                        )
                    else:
                        route_turn_hold_ms = _turn_hold_ms(
                            turn_angle,
                            slope=turn_slope_radians_per_second,
                            intercept=turn_intercept_radians,
                        )
                        _hold_key(
                            page,
                            route_turn_key,
                            duration_ms=route_turn_hold_ms,
                            input_mode=input_mode,
                        )
                    page.wait_for_timeout(500)

                before_move_count = len([event for event in events if event.get("pano_id")])
                step_started_ms = round((time.monotonic() - started) * 1000)
                navigation = str(action.get("navigation") or "forward").strip().lower()
                click_x = action.get("click_x")
                click_y = action.get("click_y")
                if navigation == "click":
                    if click_x is None or click_y is None:
                        raise RuntimeError("matterport_route_action_click_missing")
                    page.mouse.click(float(click_x), float(click_y))
                elif navigation == "forward":
                    if input_mode != "x11":
                        _activate_canvas(page)
                        page.wait_for_timeout(100)
                    else:
                        _activate_x11_page(page)
                        page.wait_for_timeout(100)
                    _hold_key(
                        page,
                        "ArrowUp",
                        duration_ms=key_hold_ms,
                        input_mode=input_mode,
                    )
                else:
                    raise RuntimeError("matterport_route_action_navigation_invalid")
                move_deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < move_deadline:
                    page.wait_for_timeout(250)
                    if len([event for event in events if event.get("pano_id")]) > before_move_count:
                        break
                page.wait_for_timeout(1000)
                step_events = [event for event in events if event.get("pano_id")][before_move_count:]
                actual_id = str(step_events[-1].get("pano_id") or "") if step_events else ""
                exact = actual_id == str(target_id)
                route_steps.append(
                    {
                        "ordinal": ordinal,
                        "source_sweep_id": current_id,
                        "expected_sweep_id": str(target_id),
                        "actual_sweep_id": actual_id,
                        "graph_edge_declared": graph_edge_declared,
                        "target_heading_radians": round(target_heading, 5),
                        "navigation": navigation,
                        "click": (
                            {"x": float(click_x), "y": float(click_y)}
                            if navigation == "click"
                            else None
                        ),
                        "turn_angle_radians": round(turn_angle, 5),
                        "turn_mode": route_turn_mode,
                        "turn_key": route_turn_key if route_turn_hold_ms else "",
                        "turn_hold_ms": route_turn_hold_ms,
                        "turn_drag_pixels": round(route_turn_drag_pixels, 3),
                        "turn_duration_ms": route_turn_duration_ms,
                        "turn_segment_count": route_turn_segment_count,
                        "turn_started_ms": turn_started_ms,
                        "move_started_ms": step_started_ms,
                        "arrival_elapsed_ms": step_events[-1].get("elapsed_ms") if step_events else None,
                        "status": "pass" if exact else "fail",
                    }
                )
                write_checkpoint("route_action_completed", final_url=page.url)
                if not exact:
                    break
                current_id = actual_id
                current_heading = target_heading
        elif turn_key:
            if input_mode != "x11":
                _focus_canvas(page)
                page.wait_for_timeout(250)
            else:
                _activate_x11_page(page)
                page.wait_for_timeout(250)
            turn_pressed_at_ms = round((time.monotonic() - started) * 1000)
            _hold_key(page, turn_key, duration_ms=turn_hold_ms, input_mode=input_mode)
            page.wait_for_timeout(750)
        elif mouse_turn_pixels:
            _focus_canvas(page)
            page.wait_for_timeout(250)
            turn_pressed_at_ms = round((time.monotonic() - started) * 1000)
            _drag_canvas_horizontally(
                page,
                mouse_turn_pixels,
                duration_ms=mouse_turn_duration_ms,
            )
            page.wait_for_timeout(750)
        navigation_click_requested = (
            navigation_click_x is not None and navigation_click_y is not None
        )
        if (move_key or navigation_click_requested) and not target_sweeps:
            if input_mode != "x11":
                canvas = page.locator("canvas").first
                box = canvas.bounding_box()
                if box and not navigation_click_requested:
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    page.wait_for_timeout(250)
            elif not navigation_click_requested:
                _activate_x11_page(page)
                page.wait_for_timeout(250)
            move_pressed_at_ms = round((time.monotonic() - started) * 1000)
            if navigation_click_requested:
                page.mouse.click(float(navigation_click_x), float(navigation_click_y))
            else:
                _hold_key(page, move_key, duration_ms=key_hold_ms, input_mode=input_mode)
            move_deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < move_deadline:
                page.wait_for_timeout(250)
                if len([event for event in events if event.get("pano_id")]) > move_event_start:
                    break
            page.wait_for_timeout(1000)
        canvas_state = _canvas_state(page)
        share_state: dict[str, object] = {}
        if open_share:
            page.get_by_role("button", name="Share this Space").click()
            page.wait_for_timeout(750)
            location_checkbox = page.get_by_role("checkbox", name="Link to this location")
            if location_checkbox.count() == 1 and not location_checkbox.is_checked():
                location_checkbox.check()
                page.wait_for_timeout(500)
            share_state = _canvas_state(page)
        write_checkpoint(
            "screenshot_capture",
            canvas_state=canvas_state,
            final_url=page.url,
            screenshot_path=str(screenshot_path),
        )
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.unlink(missing_ok=True)
        try:
            _capture_page_png(page, context, screenshot_path)
        except Exception as error:
            screenshot_error = str(error)
        final_url = page.url
        write_checkpoint(
            "browser_shutdown",
            canvas_state=canvas_state,
            screenshot_path=str(screenshot_path),
            screenshot_error=screenshot_error,
            final_url=final_url,
        )
        context.close()
        browser.close()

    pano_events = [event for event in events if event.get("pano_id")]
    room_events = [event for event in events if event.get("room_id")]
    expected_id = str(expected_start.get("id") or "")
    observed_ids = [str(event.get("pano_id") or "") for event in pano_events]
    move_pano_events = pano_events[move_event_start:]
    observed_room_ids = {str(event.get("room_id") or "") for event in room_events}
    walkable_room_ids = {str(item) for item in list(route.get("walkable_room_ids") or [])}
    exact_route_completed = (
        len(route_steps) == len(target_sweeps)
        and all(step.get("status") == "pass" for step in route_steps)
    ) if target_sweeps else True
    checks = {
        "route_contract_passes": str(route.get("status") or "") == "pass",
        "route_declares_zero_internal_edits": all(
            int(route.get(key) or 0) == 0 for key in ("cut_count", "dissolve_count", "teleport_count")
        ),
        "canvas_rendered": int(canvas_state.get("visible_canvas_count") or 0) > 0,
        "screenshot_captured": screenshot_path.is_file() and not screenshot_error,
        "browser_shutdown_cleanly": True,
        "expected_start_sweep_observed": expected_id in observed_ids,
        "move_arrival_observed": (
            bool(move_pano_events)
            if (move_key or (navigation_click_x is not None and navigation_click_y is not None))
            else True
        ),
        "exact_route_completed": exact_route_completed,
        "single_browser_document": len(main_frame_navigations) == 1,
        "all_walkable_rooms_visited": (
            walkable_room_ids.issubset(observed_room_ids)
            if require_all_walkable_rooms
            else True
        ),
    }
    receipt = {
        "contract_name": "propertyquarry.matterport_continuous_walkthrough_probe.v1",
        "status": "pass" if all(checks.values()) else "fail",
        "generated_at": _utc_now(),
        "mode": "route_driver" if target_sweeps else "start_probe",
        "checks": checks,
        "route_path": str(route_path),
        "storage_state_path": str(storage_state_path),
        "route_contract_name": route.get("contract_name"),
        "model_sid": route.get("model_sid"),
        "walkable_room_count": route.get("walkable_room_count"),
        "expected_start": expected_start,
        "start_selector": start_selector,
        "play_mode": play_mode,
        "start_rotation": start_rotation,
        "start_value": selected_value,
        "turn_key": turn_key,
        "turn_hold_ms": turn_hold_ms,
        "turn_pressed_at_ms": turn_pressed_at_ms,
        "mouse_turn_pixels": mouse_turn_pixels,
        "mouse_turn_duration_ms": mouse_turn_duration_ms,
        "move_key": move_key,
        "navigation_click": (
            {"x": navigation_click_x, "y": navigation_click_y}
            if navigation_click_x is not None and navigation_click_y is not None
            else None
        ),
        "key_hold_ms": key_hold_ms,
        "move_pressed_at_ms": move_pressed_at_ms,
        "move_pano_events": move_pano_events,
        "target_sweeps": target_sweeps,
        "route_actions_path": str(route_actions_path) if route_actions_path else "",
        "route_steps": route_steps,
        "world_heading_radians": world_heading_radians,
        "turn_model": {
            "route_turn_mode": route_turn_mode,
            "slope_radians_per_second": turn_slope_radians_per_second,
            "intercept_radians": turn_intercept_radians,
            "deadband_radians": turn_deadband_radians,
            "mouse_pixels_per_radian": mouse_pixels_per_radian,
            "mouse_duration_ms_per_radian": mouse_duration_ms_per_radian,
        },
        "renderer_mode": renderer_mode,
        "headed": headed,
        "input_mode": input_mode,
        "requested_url": url,
        "final_url": final_url,
        "canvas_state": canvas_state,
        "share_state": share_state,
        "pano_events": pano_events,
        "room_events": room_events,
        "observed_walkable_room_ids": sorted(observed_room_ids & walkable_room_ids),
        "missing_walkable_room_ids": sorted(walkable_room_ids - observed_room_ids),
        "main_frame_navigations": main_frame_navigations,
        "marked_requests": marked_requests,
        "console_errors": console_errors[-20:],
        "page_errors": page_errors[-20:],
        "screenshot_path": str(screenshot_path),
        "screenshot_error": screenshot_error,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def _watchdog_failure_receipt(
    *,
    receipt_path: Path,
    timeout_seconds: float,
    stderr: str,
) -> dict[str, object]:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        receipt = {
            "contract_name": "propertyquarry.matterport_continuous_walkthrough_probe.v1",
            "generated_at": _utc_now(),
            "route_steps": [],
        }
    checks = dict(receipt.get("checks") or {})
    checks["browser_shutdown_cleanly"] = False
    checks["worker_completed"] = False
    receipt.update(
        {
            "status": "fail",
            "phase": "worker_timeout",
            "checks": checks,
            "watchdog": {
                "timeout_seconds": timeout_seconds,
                "stderr_tail": stderr[-4000:],
            },
        }
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def _run_with_watchdog(*, receipt_path: Path, timeout_seconds: float) -> int:
    receipt_path.unlink(missing_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--internal-worker"]
    worker = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = worker.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(worker.pid, signal.SIGTERM)
        try:
            _, stderr = worker.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(worker.pid, signal.SIGKILL)
            _, stderr = worker.communicate()
        receipt = _watchdog_failure_receipt(
            receipt_path=receipt_path,
            timeout_seconds=timeout_seconds,
            stderr=stderr or "",
        )
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 1

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        receipt = {
            "contract_name": "propertyquarry.matterport_continuous_walkthrough_probe.v1",
            "status": "fail",
            "generated_at": _utc_now(),
            "phase": "worker_exit_without_receipt",
            "checks": {"worker_completed": False},
            "watchdog": {
                "return_code": worker.returncode,
                "stderr_tail": (stderr or "")[-4000:],
            },
        }
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        receipt["watchdog"] = {
            "return_code": worker.returncode,
            "worker_completed": True,
            "stderr_tail": (stderr or "")[-4000:],
        }
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt.get("status") == "pass" and worker.returncode == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive and prove a cut-free Matterport route through every walkable room."
    )
    parser.add_argument("--route", default=str(DEFAULT_ROUTE))
    parser.add_argument("--topology", default=str(DEFAULT_TOPOLOGY))
    parser.add_argument("--storage-state", default=str(DEFAULT_STORAGE_STATE))
    parser.add_argument("--write", default=str(DEFAULT_RECEIPT))
    parser.add_argument("--screenshot", default=str(DEFAULT_SCREENSHOT))
    parser.add_argument("--start-selector", choices=("index", "id", "uuid"), default="uuid")
    parser.add_argument("--play-mode", choices=("0", "1"), default="0")
    parser.add_argument("--start-rotation", default="0,0")
    parser.add_argument("--start-value", default="")
    parser.add_argument("--turn-key", default="")
    parser.add_argument("--turn-hold-ms", type=int, default=0)
    parser.add_argument("--mouse-turn-pixels", type=float, default=0.0)
    parser.add_argument("--mouse-turn-duration-ms", type=int, default=750)
    parser.add_argument("--move-key", default="")
    parser.add_argument("--key-hold-ms", type=int, default=350)
    parser.add_argument("--navigation-click-x", type=float)
    parser.add_argument("--navigation-click-y", type=float)
    parser.add_argument("--open-share", action="store_true")
    parser.add_argument("--target-sweep", action="append", default=[])
    parser.add_argument("--route-actions")
    parser.add_argument("--max-route-actions", type=int, default=0)
    parser.add_argument("--world-heading-radians", type=float, default=0.0)
    parser.add_argument("--turn-slope-radians-per-second", type=float, default=0.359)
    parser.add_argument("--turn-intercept-radians", type=float, default=0.111)
    parser.add_argument("--turn-deadband-radians", type=float, default=0.12)
    parser.add_argument("--route-turn-mode", choices=("keyboard", "mouse"), default="keyboard")
    parser.add_argument("--mouse-pixels-per-radian", type=float, default=1081.081)
    parser.add_argument("--mouse-duration-ms-per-radian", type=float, default=1200.0)
    parser.add_argument("--renderer-mode", choices=("auto", "swiftshader"), default="auto")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--input-mode", choices=("playwright", "x11"), default="playwright")
    parser.add_argument("--require-all-walkable-rooms", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--internal-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    receipt_path = Path(args.write).expanduser().resolve()
    if not args.internal_worker:
        return _run_with_watchdog(
            receipt_path=receipt_path,
            timeout_seconds=max(30.0, float(args.worker_timeout_seconds)),
        )
    receipt = probe_start(
        route_path=Path(args.route).expanduser().resolve(),
        topology_path=Path(args.topology).expanduser().resolve(),
        storage_state_path=Path(args.storage_state).expanduser().resolve(),
        receipt_path=receipt_path,
        screenshot_path=Path(args.screenshot).expanduser().resolve(),
        play_mode=str(args.play_mode),
        start_selector=args.start_selector,
        start_rotation=str(args.start_rotation),
        start_value=str(args.start_value),
        turn_key=str(args.turn_key),
        turn_hold_ms=max(30, int(args.turn_hold_ms)) if args.turn_key else 0,
        mouse_turn_pixels=float(args.mouse_turn_pixels),
        mouse_turn_duration_ms=max(30, int(args.mouse_turn_duration_ms)),
        move_key=str(args.move_key),
        key_hold_ms=max(30, int(args.key_hold_ms)),
        navigation_click_x=args.navigation_click_x,
        navigation_click_y=args.navigation_click_y,
        open_share=bool(args.open_share),
        target_sweeps=[str(item) for item in args.target_sweep],
        route_actions_path=(
            Path(args.route_actions).expanduser().resolve() if args.route_actions else None
        ),
        max_route_actions=max(0, int(args.max_route_actions)),
        world_heading_radians=float(args.world_heading_radians),
        turn_slope_radians_per_second=max(0.01, float(args.turn_slope_radians_per_second)),
        turn_intercept_radians=max(0.0, float(args.turn_intercept_radians)),
        turn_deadband_radians=max(0.0, float(args.turn_deadband_radians)),
        route_turn_mode=str(args.route_turn_mode),
        mouse_pixels_per_radian=max(1.0, float(args.mouse_pixels_per_radian)),
        mouse_duration_ms_per_radian=max(30.0, float(args.mouse_duration_ms_per_radian)),
        renderer_mode=str(args.renderer_mode),
        headed=bool(args.headed),
        input_mode=str(args.input_mode),
        require_all_walkable_rooms=bool(args.require_all_walkable_rooms),
        timeout_seconds=max(5.0, float(args.timeout_seconds)),
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
