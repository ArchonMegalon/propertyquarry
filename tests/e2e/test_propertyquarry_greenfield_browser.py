from __future__ import annotations

import base64
import hashlib
import io
import html
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, expect, sync_playwright

Config = uvicorn.Config
Server = uvicorn.Server

from app.api.app import create_app
from app.api.routes import landing as landing_routes
from app.api.routes.public_tours import _tour_control_panorama_html
from app.property_distance_preferences import (
    property_distance_importance_key,
    property_distance_preference_keys,
)
from app.product import property_evidence_overlays as evidence_overlays
from app.product.models import HandoffNote
from app.product.service import ProductService
from app.services.property_market_catalog import selectable_property_platform_keys
from app.services.public_tour_release_policy import (
    PUBLIC_TOUR_GENERATED_VIEWER_RELEASE_CONTRACT,
)
from scripts import property_reconstruction_styles as reconstruction_styles
from scripts.property_tour_3dvista_provenance import (
    THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
    export_tree_sha256,
)
from scripts.propertyquarry_playwright_runtime import (
    normalize_playwright_engine,
    playwright_engine_launch_browser,
)


_PROPERTYQUARRY_CORE_BROWSER_ENGINE_ENV = "PROPERTYQUARRY_CORE_BROWSER_ENGINE"
_PROPERTYQUARRY_CHROMIUM_LAUNCH_ARGS = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--no-proxy-server",
    "--autoplay-policy=no-user-gesture-required",
)
_CATALOG_DISTANCE_RADIUS_KEYS = property_distance_preference_keys(
    canonical_only=True
)
_CATALOG_DISTANCE_IMPORTANCE_KEYS = tuple(
    property_distance_importance_key(key)
    for key in _CATALOG_DISTANCE_RADIUS_KEYS
)
_CANONICAL_DISTANCE_IMPORTANCE_VALUES = (
    "must_have",
    "strong_wish",
    "nice_to_have",
    "avoid",
)


def _configured_propertyquarry_browser_engine() -> str:
    return normalize_playwright_engine(
        os.environ.get(_PROPERTYQUARRY_CORE_BROWSER_ENGINE_ENV)
    )


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_for_http(base_url: str, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/sign-in", timeout=2.0) as response:
                if int(getattr(response, "status", 0) or 0) == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise AssertionError(f"server at {base_url} did not become ready in time")


def _wait_for_what_matters_interaction_stabilization(page: Page) -> None:
    # What-matters controls restore their interaction snapshot immediately,
    # across two animation frames, and once more on a 64 ms late tail. Keep
    # synthetic test navigation from racing that product-visible sequence.
    page.evaluate(
        """
        () => new Promise((resolve) => {
          window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => window.setTimeout(resolve, 80));
          });
        })
        """
    )


def _stop_uvicorn_server(*, server: Server, thread: threading.Thread, label: str) -> None:
    server.should_exit = True
    thread.join(timeout=10.0)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=5.0)
    assert not thread.is_alive(), f"{label} uvicorn thread did not stop"


def _write_floorplan_png(path: Path) -> None:
    image = Image.new("RGB", (1280, 900), (248, 246, 242))
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 1200, 820), outline=(42, 42, 42), width=8)
    draw.line((420, 80, 420, 820), fill=(58, 58, 58), width=6)
    draw.line((80, 410, 1200, 410), fill=(58, 58, 58), width=6)
    draw.rectangle((450, 120, 1110, 360), outline=(148, 68, 48), width=6)
    draw.rectangle((110, 120, 380, 360), outline=(73, 108, 170), width=6)
    draw.rectangle((110, 450, 380, 770), outline=(73, 108, 170), width=6)
    draw.rectangle((450, 450, 1110, 770), outline=(148, 68, 48), width=6)
    draw.text((150, 210), "Entry", fill=(30, 30, 30))
    draw.text((690, 220), "Living", fill=(30, 30, 30))
    draw.text((160, 600), "Bath", fill=(30, 30, 30))
    draw.text((720, 600), "Bedroom", fill=(30, 30, 30))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _write_cube_face_png(path: Path, *, label: str, fill: tuple[int, int, int]) -> None:
    image = Image.new("RGB", (1024, 1024), fill)
    draw = ImageDraw.Draw(image)
    draw.rectangle((36, 36, 988, 988), outline=(248, 245, 240), width=14)
    draw.text((84, 104), "PropertyQuarry", fill=(255, 255, 255))
    draw.text((84, 180), label, fill=(250, 247, 242))
    draw.rectangle((120, 280, 904, 760), outline=(255, 255, 255), width=10)
    draw.rectangle((180, 340, 500, 720), fill=(235, 231, 224))
    draw.rectangle((548, 340, 840, 720), fill=(198, 166, 122))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _remote_png_bytes(
    *,
    label: str,
    size: tuple[int, int] = (1600, 1100),
    fill: tuple[int, int, int] = (228, 220, 210),
) -> bytes:
    image = Image.new("RGB", size, fill)
    draw = ImageDraw.Draw(image)
    draw.rectangle((56, 56, size[0] - 56, size[1] - 56), outline=(248, 245, 240), width=14)
    draw.rectangle((88, 88, size[0] - 88, 250), fill=(30, 31, 34))
    draw.text((126, 136), "PropertyQuarry", fill=(248, 245, 240))
    draw.text((126, 186), label, fill=(233, 221, 206))
    draw.rectangle((124, 328, 976, 916), fill=(239, 234, 227), outline=(164, 104, 66), width=8)
    draw.rectangle((1034, 328, 1446, 610), fill=(181, 194, 164))
    draw.rectangle((1034, 650, 1446, 916), fill=(215, 194, 166))
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    return payload.getvalue()


def _visual_map_preview_png_bytes() -> bytes:
    """Build stable map-like artwork for screenshots without runtime cache or network state."""

    image = Image.new("RGB", (1024, 640), (226, 230, 223))
    draw = ImageDraw.Draw(image)

    draw.polygon(
        (
            (0, 420),
            (170, 356),
            (356, 374),
            (540, 318),
            (740, 338),
            (1024, 246),
            (1024, 640),
            (0, 640),
        ),
        fill=(203, 218, 201),
    )
    draw.line(
        ((-30, 132), (168, 170), (334, 154), (518, 224), (700, 204), (1054, 308)),
        fill=(176, 207, 221),
        width=82,
        joint="curve",
    )

    blocks = (
        (58, 42, 202, 116),
        (248, 38, 390, 118),
        (432, 48, 578, 132),
        (626, 40, 786, 128),
        (838, 52, 980, 150),
        (54, 252, 210, 356),
        (276, 260, 414, 360),
        (480, 286, 632, 388),
        (702, 286, 858, 386),
        (86, 464, 232, 570),
        (304, 430, 458, 552),
        (536, 454, 686, 572),
        (770, 434, 942, 564),
    )
    for left, top, right, bottom in blocks:
        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=10,
            fill=(238, 235, 226),
            outline=(193, 190, 181),
            width=3,
        )

    roads = (
        ((-20, 232), (1044, 420)),
        ((144, -20), (238, 660)),
        ((402, -20), (494, 660)),
        ((690, -20), (610, 660)),
        ((900, -20), (826, 660)),
    )
    for start, end in roads:
        draw.line((start, end), fill=(250, 248, 242), width=28)
        draw.line((start, end), fill=(207, 203, 194), width=3)

    draw.ellipse((474, 246, 550, 322), fill=(244, 241, 233), outline=(176, 75, 69), width=5)
    draw.ellipse((496, 268, 528, 300), fill=(194, 54, 54), outline=(255, 255, 255), width=5)

    payload = io.BytesIO()
    image.save(payload, format="PNG")
    return payload.getvalue()


def _stub_remote_png(
    context: BrowserContext,
    *,
    url_pattern: str,
    label: str,
    size: tuple[int, int] = (1600, 1100),
) -> None:
    body = _remote_png_bytes(label=label, size=size)
    context.route(
        url_pattern,
        lambda route: route.fulfill(
            status=200,
            content_type="image/png",
            body=body,
        ),
    )


def _stub_visual_map_previews(context: BrowserContext) -> None:
    """Keep the screenshot matrix representative while production preview routes stay untouched."""

    body = _visual_map_preview_png_bytes()
    context.route(
        "**/app/api/property/map-previews/*.png",
        lambda route: route.fulfill(
            status=200,
            content_type="image/png",
            headers={
                "Cache-Control": "no-store, max-age=0, no-transform",
                "Content-Encoding": "identity",
                "X-Property-Map-Preview-State": "ready",
            },
            body=body,
        ),
    )


def _write_h264_flythrough(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=#d8d0c4:s=1280x720:d=3",
        "-vf",
        (
            "drawbox=x=0:y=0:w=iw:h=110:color=#181a1dcc:t=fill,"
            "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            "text='PropertyQuarry Flythrough':fontcolor=white:fontsize=34:x=48:y=40,"
            "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
            "text='Best-match route':fontcolor=#ece6df:fontsize=20:x=52:y=82,"
            "drawbox=x='120+220*t':y='250+30*sin(t*2)':w=820:h=210:color=#a46842:t=6,"
            "drawbox=x='170+220*t':y='300+28*sin(t*2)':w=330:h=110:color=#5a7bb2:t=fill,"
            "drawbox=x='560+220*t':y='300+20*sin(t*2)':w=250:h=110:color=#e7e3dc:t=fill,"
            "drawbox=x='845+220*t':y='300+14*sin(t*2)':w=90:h=110:color=#88a36f:t=fill"
        ),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(path),
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _video_frame_brightness(page: Page) -> float:
    return float(
        page.evaluate(
            """() => {
                const video = document.getElementById('flythrough-video') || document.getElementById('tour-video');
                if (!video || !video.videoWidth || !video.videoHeight) return 0;
                const canvas = document.createElement('canvas');
                canvas.width = Math.min(video.videoWidth, 160);
                canvas.height = Math.min(video.videoHeight, 90);
                const ctx = canvas.getContext('2d');
                if (!ctx) return 0;
                ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
                const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
                let total = 0;
                for (let index = 0; index < data.length; index += 4) {
                    total += (data[index] + data[index + 1] + data[index + 2]) / 3;
                }
                return total / (data.length / 4);
            }"""
        )
    )


def _unexpected_console_errors(messages: list[str]) -> list[str]:
    filtered: list[str] = []
    for message in messages:
        normalized = str(message or "").strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if "cross-origin-opener-policy header has been ignored" in lowered and "origin was untrustworthy" in lowered:
            continue
        filtered.append(normalized)
    return filtered


def _is_expected_vendor_report_only_inline_script_console_error(message: str) -> bool:
    return str(message or "").strip().lower().startswith(
        "[report only] refused to execute a script because its hash, its nonce, "
        "or 'unsafe-inline' does not appear in the script-src directive"
    )


def _assert_public_tour_reference_media_decoded(page: Page) -> set[str]:
    images = page.locator("#media-grid img")
    image_count = images.count()
    assert image_count > 0
    for index in range(image_count):
        images.nth(index).scroll_into_view_if_needed(timeout=10_000)
        page.wait_for_function(
            """(imageIndex) => {
                const image = document.querySelectorAll('#media-grid img')[imageIndex];
                return Boolean(image && image.complete);
            }""",
            arg=index,
            timeout=10_000,
        )
    states = page.evaluate(
        """() => Array.from(document.querySelectorAll('#media-grid img')).map((image) => ({
            src: String(image.currentSrc || image.src || ''),
            complete: Boolean(image.complete),
            naturalWidth: Number(image.naturalWidth || 0),
            naturalHeight: Number(image.naturalHeight || 0),
        }))"""
    )
    assert isinstance(states, list)
    assert len(states) == image_count
    assert all(isinstance(state, dict) for state in states), states
    assert states and all(
        bool(state.get("complete"))
        and int(state.get("naturalWidth") or 0) > 0
        and int(state.get("naturalHeight") or 0) > 0
        for state in states
    ), states
    decoded_urls = {str(state.get("src") or "").strip() for state in states}
    assert "" not in decoded_urls, states
    return decoded_urls


def _unexpected_generated_photo_request_failures(
    failures: list[str],
    *,
    base_url: str,
    slug: str,
    decoded_urls: set[str],
) -> list[str]:
    expected_origin = urllib.parse.urlsplit(base_url)
    expected_path = re.compile(
        rf"^/tours/files/{re.escape(slug)}/generated-reconstruction/photo-[0-9]{{2}}\.png$"
    )
    unexpected: list[str] = []
    for failure in failures:
        request_url, separator, failure_detail = str(failure or "").partition(" :: ")
        parsed = urllib.parse.urlsplit(request_url)
        is_expected_firefox_retry = (
            separator
            and failure_detail.strip() == "NS_BINDING_ABORTED"
            and parsed.scheme == expected_origin.scheme
            and parsed.netloc == expected_origin.netloc
            and expected_path.fullmatch(parsed.path) is not None
            and not parsed.query
            and not parsed.fragment
            and request_url in decoded_urls
        )
        if not is_expected_firefox_retry:
            unexpected.append(str(failure or ""))
    return unexpected


def _generated_reconstruction_layout_viewer_state(page: Page) -> dict[str, object]:
    state = page.evaluate(
        """() => {
            const shell = document.querySelector('.layout-viewer-shell');
            const frame = document.getElementById('layout-viewer-frame');
            const doc = frame?.contentDocument;
            const debug = frame?.contentWindow?.__pqReconstructionDebug;
            const metrics = debug && typeof debug.getRenderMetrics === 'function'
              ? debug.getRenderMetrics()
              : null;
            return {
              shellReady: Boolean(shell && shell.classList.contains('is-ready')),
              routeLabel: String(doc?.getElementById('viewer-route-label')?.textContent || '').trim(),
              routePosition: String(doc?.getElementById('viewer-route-position')?.textContent || '').trim(),
              focusReadout: String(doc?.getElementById('viewer-focus-readout')?.textContent || '').trim(),
              renderReadout: String(doc?.getElementById('viewer-render-readout')?.textContent || '').trim(),
              metrics: metrics && typeof metrics === 'object' ? metrics : {},
            };
        }"""
    )
    assert isinstance(state, dict)
    return state


def _wait_for_generated_reconstruction_layout_viewer_ready(
    page: Page,
    *,
    timeout_ms: int = 20_000,
) -> dict[str, object]:
    # The generated viewer is intentionally lazy. Firefox will correctly leave
    # the below-the-fold iframe at about:blank until a user reaches the section,
    # so the proof must exercise that real interaction before awaiting WebGL.
    frame = page.locator("#layout-viewer-frame")
    expect(frame).to_be_visible(timeout=timeout_ms)
    frame.scroll_into_view_if_needed(timeout=timeout_ms)
    page.wait_for_function(
        """() => {
            const shell = document.querySelector('.layout-viewer-shell');
            const frame = document.getElementById('layout-viewer-frame');
            const doc = frame?.contentDocument;
            const debug = frame?.contentWindow?.__pqReconstructionDebug;
            if (!shell || !doc || !debug || typeof debug.getRenderMetrics !== 'function') {
              return false;
            }
            const metrics = debug.getRenderMetrics();
            return shell.classList.contains('is-ready')
              && String(doc.getElementById('viewer-render-readout')?.textContent || '').trim() === 'Ready'
              && Boolean(metrics && metrics.ready)
              && Number(metrics.frameCount || 0) >= 2
              && Number(metrics.renderCalls || 0) > 0
              && Number(metrics.renderTriangles || 0) > 0;
        }""",
        timeout=timeout_ms,
    )
    return _generated_reconstruction_layout_viewer_state(page)


def _choose_research_visual_style(
    page: Page,
    *,
    label: str = "Urban jungle",
    accept_external_processing: bool = False,
) -> None:
    dialog = page.locator("[data-prd-visual-style-dialog]").first
    expect(dialog).to_be_visible(timeout=5000)
    expect(page.locator("[data-prd-visual-status]")).to_contain_text("Choose a style", timeout=5000)
    option = dialog.locator("[data-prd-style-option]", has_text=label).first
    expect(option).to_be_visible()
    option.click()
    expect(option).to_have_attribute("aria-pressed", "true")
    expect(option.locator("[data-prd-style-selected-label]")).to_be_visible()
    expect(dialog.locator("[data-prd-style-current]")).to_contain_text(f"{label} selected")
    expect(page.locator("[data-prd-visual-status]")).to_contain_text("selected", timeout=5000)
    confirm = dialog.locator("[data-prd-style-confirm]").first
    expect(confirm).to_contain_text(label)
    accepted_consent_prompts: list[str] = []
    if accept_external_processing:
        def _accept_governed_external_processing(dialog_event) -> None:
            message = str(dialog_event.message or "").strip()
            assert dialog_event.type == "confirm"
            assert "governed external rendering service" in message
            assert "remains private" in message
            accepted_consent_prompts.append(message)
            dialog_event.accept()

        page.once("dialog", _accept_governed_external_processing)
    confirm.click()
    if accept_external_processing:
        assert len(accepted_consent_prompts) == 1


def _choose_workbench_visual_style(page: Page, *, label: str = "Urban jungle") -> None:
    dialog = page.locator("[data-pqx-visual-style-dialog]").first
    status = page.locator("[data-pw-visual-request-status]").first
    expect(dialog).to_be_visible(timeout=5000)
    expect(status).to_contain_text("Choose a style", timeout=5000)
    option = dialog.locator("[data-pqx-visual-style-option]", has_text=label).first
    expect(option).to_be_visible()
    option.click()
    expect(option).to_have_attribute("aria-pressed", "true")
    expect(option.locator("[data-pqx-visual-style-selected-label]")).to_be_visible()
    expect(dialog.locator("[data-pqx-visual-style-current]")).to_contain_text(f"{label} selected")
    expect(status).to_contain_text("selected", timeout=5000)
    confirm = dialog.locator("[data-pqx-visual-style-confirm]").first
    expect(confirm).to_contain_text(label)
    confirm.click()


def _choose_object_visual_style(page: Page, *, label: str = "Urban jungle") -> None:
    dialog = page.locator("[data-object-visual-style-dialog]").first
    expect(dialog).to_be_visible(timeout=5000)
    expect(page.locator("[data-object-visual-status]")).to_contain_text("Choose a style", timeout=5000)
    option = dialog.locator("[data-object-visual-style-option]", has_text=label).first
    expect(option).to_be_visible()
    option.click()
    expect(option).to_have_attribute("aria-pressed", "true")
    expect(option.locator("[data-object-visual-style-selected-label]")).to_be_visible()
    expect(dialog.locator("[data-object-visual-style-current]")).to_contain_text(f"{label} selected")
    expect(page.locator("[data-object-visual-status]")).to_contain_text("selected", timeout=5000)
    confirm = dialog.locator("[data-object-visual-style-confirm]").first
    expect(confirm).to_contain_text(label)
    confirm.click()


def _click_visible_what_matters_action(page: Page, action: str) -> None:
    selector = f'[data-pqx-{action}-what-matters]'
    buttons = page.locator(selector)
    for index in range(buttons.count()):
        candidate = buttons.nth(index)
        if candidate.is_visible():
            candidate.click()
            return
    mobile_menu = page.locator(".pqx-what-matters-mobile-menu").first
    if mobile_menu.count():
        summary = mobile_menu.locator("summary").first
        if summary.is_visible():
            summary.click()
            for index in range(buttons.count()):
                candidate = buttons.nth(index)
                if candidate.is_visible():
                    candidate.click()
            return
    raise AssertionError(f"No visible What Matters {action} control was available.")


def _settled_checked_provider_values(
    page: Page,
    *,
    attempts: int = 12,
    pause_ms: int = 200,
    min_settle_ms: int = 1200,
) -> list[str]:
    snapshot_script = """
        () => {
          const form = document.querySelector('[data-console-form-variant="property_search"]');
          const countryCode = String(form?.querySelector('[name="country_code"]')?.value || 'AT').trim().toUpperCase();
          const searchGoal = String(form?.querySelector('[name="search_goal"]')?.value || 'home').trim().toLowerCase();
          const listingModeField = String(form?.querySelector('[name="listing_mode"]')?.value || 'rent').trim().toLowerCase() || 'rent';
          const listingMode = searchGoal === 'investment' ? 'buy' : listingModeField;
          const seen = new Set();
          const values = [];
          form?.querySelectorAll('input[name="selected_platforms"]:checked').forEach((node) => {
            const value = String(node.value || '').trim();
            if (!value || seen.has(value)) return;
            const providerCountry = String(node.getAttribute('data-country-code') || '').trim().toUpperCase();
            if (providerCountry && providerCountry !== countryCode) return;
            const rawModes = String(node.getAttribute('data-listing-modes') || '').trim().toLowerCase();
            if (rawModes) {
              const supportedModes = rawModes.split(',').map((item) => String(item || '').trim()).filter(Boolean);
              if (!supportedModes.includes(listingMode)) return;
            }
            seen.add(value);
            values.push(value);
          });
          return values;
        }
    """
    previous: list[str] | None = None
    for attempt in range(max(1, attempts)):
        current = page.evaluate(snapshot_script)
        assert isinstance(current, list)
        elapsed_ms = attempt * pause_ms
        if previous is not None and current == previous and elapsed_ms >= max(0, min_settle_ms):
            return current
        previous = current
        if attempt < attempts - 1:
            page.wait_for_timeout(pause_ms)
    assert previous is not None
    return previous


@pytest.fixture()
def propertyquarry_browser_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> Iterator[dict[str, object]]:
    from tests.product_test_helpers import build_product_client, start_workspace

    cleanup = ExitStack()
    request.addfinalizer(cleanup.close)
    monkeypatch.setenv("EMAILIT_API_KEY", "propertyquarry-browser-test-emailit-key")
    monkeypatch.setenv("PROPERTYQUARRY_LEGACY_PDF_RENDERER_ALLOW", "1")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "paypal-client")
    monkeypatch.setenv("PAYPAL_SECRET", "paypal-secret")
    monkeypatch.setenv("FLIPLINK_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setattr(
        landing_routes,
        "_property_brilliant_directories_billing_handoff",
        lambda allow_verified_direct_handoff=False: {
            "available": False,
            "status": "disabled",
            "open_href": "",
            "bridge_href": "",
            "bridge_status": "",
            "member_token_status": "",
        },
    )
    bundle_root = tmp_path / "public_tours"
    slug = "altbau-u6"
    bundle_dir = bundle_root / slug
    bundle_dir.mkdir(parents=True, exist_ok=True)
    _write_floorplan_png(bundle_dir / "floorplan-01.png")
    _write_h264_flythrough(bundle_dir / "tour.mp4")
    _write_cube_face_png(bundle_dir / "scene-01.png", label="Living room", fill=(108, 82, 59))
    three_d_vista_dir = bundle_dir / "3dvista"
    three_d_vista_dir.mkdir()
    (three_d_vista_dir / "index.htm").write_text(
        "<!doctype html><html><body><div id='tour-viewer'>3D tour ready</div>"
        "<script>window.TDVPlayer = { ready: true };</script></body></html>",
        encoding="utf-8",
    )
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "three_d_vista_target_provenance": {
                    "schema": THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
                    "status": "pass",
                    "provider": "3dvista",
                    "target_slug": slug,
                    "target_subdir": "3dvista",
                    "artifact": {
                        "kind": "local_export",
                        "sha256": export_tree_sha256(three_d_vista_dir),
                        "entry_relpath": "index.htm",
                    },
                    "authorization": {
                        "status": "approved",
                        "reference": "pytest:propertyquarry-browser-fixture",
                    },
                    "review": {
                        "property_match": "pass",
                        "visual_match": "pass",
                        "reviewed_by": "propertyquarry-e2e-fixture",
                        "reviewed_at": "2026-07-08T07:00:00Z",
                    },
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Altbau near U6",
                "display_title": "Altbau near U6",
                "hosted_url": f"/tours/{slug}",
                "public_url": f"/tours/{slug}",
                "brand_name": "PropertyQuarry",
                "scene_strategy": "layout_first",
                "creation_mode": "hosted_listing_tour",
                "video_relpath": "tour.mp4",
                "video_provider": "manual_upload",
                "three_d_vista_entry_relpath": "3dvista/index.htm",
                "three_d_vista_import": {"source_project": "propertyquarry"},
                "three_d_vista_white_label_proof": {
                    "source_project": "propertyquarry",
                    "private_viewer_verified": True,
                    "non_trial_export_verified": True,
                    "propertyquarry_tour_metadata": True,
                    "trial_branding_checked": True,
                    "trial_branding_present": False,
                },
                "three_d_vista_browser_render_proof": {
                    "provider": "3dvista",
                    "status": "pass",
                    "rendered_viewer": True,
                },
                "scenes": [
                    {
                        "scene_id": "panorama-1",
                        "name": "Living room anchor",
                        "role": "photo",
                        "asset_relpath": "scene-01.png",
                        "image_url": "scene-01.png",
                        "mime_type": "image/png",
                    },
                    {
                        "scene_id": "floorplan-1",
                        "name": "Main floorplan",
                        "role": "floorplan",
                        "asset_relpath": "floorplan-01.png",
                        "mime_type": "image/png",
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(bundle_root))
    monkeypatch.setenv("EA_PUBLIC_APP_BASE_URL", "https://propertyquarry.com")
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PUBLIC_TOURS", "1")
    client = build_product_client(principal_id="pq-greenfield-browser")
    cleanup.callback(client.close)
    start_workspace(client, mode="personal", workspace_name="Property Office")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "DE",
            "language_code": "de",
            "listing_mode": "buy",
            "property_type": "apartment",
            "location_query": "Berlin",
            "keywords": "lift family balcony",
            "selected_platforms": ["immoscout_de", "immowelt"],
            "preference_person_id": "elisabeth",
            "max_results_per_source": 4,
        },
    )
    assert stored.status_code == 200, stored.text

    run_status_calls: dict[str, int] = {}

    def _fake_empty_run_status(*, run_id: str, principal_id: str, processed: bool) -> dict[str, object]:
        return {
            "run_id": run_id,
            "principal_id": principal_id,
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "status": "processed" if processed else "in_progress",
            "generated_at": "2026-06-05T07:00:00+00:00",
            "updated_at": "2026-06-05T07:01:00+00:00" if processed else "2026-06-05T07:00:15+00:00",
            "progress": 100 if processed else 12,
            "message": "No strong matches met the selected threshold." if processed else "Scanning cooperative providers.",
            "summary": {
                "sources_total": 2,
                "listing_total": 14 if processed else 3,
                "filtered_low_fit_total": 14 if processed else 0,
                "high_fit_total": 0,
                "high_match_min_score": 60,
                "tour_created_total": 0,
                "tour_existing_total": 0,
                "sources": [
                    {
                        "source_label": "Genossenschaften Austria",
                        "status": "scanned",
                        "listing_total": 8 if processed else 2,
                        "high_fit_total": 0,
                        "filtered_low_fit_total": 8 if processed else 0,
                    },
                    {
                        "source_label": "Justiz Edikte Austria",
                        "status": "scanned",
                        "listing_total": 6 if processed else 1,
                        "high_fit_total": 0,
                        "filtered_low_fit_total": 6 if processed else 0,
                    },
                ],
            },
            "events": [
                {"step": "sources_resolved", "message": "Resolved 2 provider(s) for scanning.", "status": "in_progress"},
                {
                    "step": "completed" if processed else "provider_scan",
                    "message": "No strong matches met the selected threshold." if processed else "Scanning cooperative providers.",
                    "status": "processed" if processed else "in_progress",
                },
            ],
        }

    def _fake_run_status(self, *, principal_id: str, run_id: str):
        assert principal_id == "pq-greenfield-browser"
        if run_id == "run-always-active":
            return _fake_empty_run_status(run_id=run_id, principal_id=principal_id, processed=False)
        if run_id == "run-active-empty":
            calls = run_status_calls.get(run_id, 0)
            run_status_calls[run_id] = calls + 1
            return _fake_empty_run_status(run_id=run_id, principal_id=principal_id, processed=calls > 0)
        return {
            "run_id": run_id,
            "principal_id": principal_id,
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "status": "processed",
            "progress": 100,
            "message": "Property scouting run completed.",
            "held_back_total": 2,
            "summary": {
                "sources_total": 2,
                "listing_total": 7,
                "held_back_total": 2,
                "tour_created_total": 1,
                "tour_existing_total": 1,
                "sources": [
                    {
                        "source_label": "ImmoScout24 Germany",
                        "listing_total": 4,
                        "high_fit_total": 2,
                        "tour_created_total": 1,
                        "notified_total": 1,
                        "top_candidates": [
                            {
                                "title": "Altbau near U6",
                                "property_url": "https://www.immobilienscout24.de/expose/altbau-u6",
                                "fit_summary": "Personal fit 92/100 · shortlist · Lift and transit fit.",
                                "recommendation": "shortlist",
                                "review_url": "/app/handoffs/human_task:review-1",
                                "tour_url": "/tours/altbau-u6/control/3dvista",
                                "match_reasons": ["Lift and transit fit."],
                                "mismatch_reasons": [],
                                "property_facts": {
                                    "price_display": "EUR 420,000",
                                    "price_eur": 420000.0,
                                    "rooms": 3,
                                    "area_m2": 78,
                                    "postal_name": "Berlin Mitte",
                                    "country_code": "DE",
                                    "map_lat": 52.52,
                                    "map_lng": 13.405,
                                    "nearest_supermarket_m": 280,
                                    "nearest_pharmacy_m": 410,
                                    "nearest_playground_m": 520,
                                    "nearest_subway_m": 1200,
                                    "traffic_density_label": "Moderate traffic",
                                    "noise_level_label": "Calm side street",
                                    "school_atlas_progression_summary": "Nearby schools connect well to Gymnasium and AHS.",
                                    "official_risk_evidence": {
                                        "sources": [
                                            {
                                                "risk_key": "heat_resilience",
                                                "source_label": "Berlin climate map",
                                                "title": "Summer heat",
                                                "url": "https://example.test/berlin-heat",
                                                "freshness_label": "city data",
                                            },
                                            {
                                                "risk_key": "cooling_corridor",
                                                "source_label": "Berlin climate map",
                                                "title": "Cooling corridor",
                                                "url": "https://example.test/berlin-cooling",
                                                "freshness_label": "city data",
                                            },
                                            {
                                                "risk_key": "fiber_coverage",
                                                "source_label": "Breitbandatlas",
                                                "title": "Fiber coverage",
                                                "url": "https://example.test/berlin-fiber",
                                                "freshness_label": "atlas",
                                            },
                                            {
                                                "risk_key": "school_context",
                                                "source_label": "Berlin school atlas",
                                                "title": "School context",
                                                "url": "https://example.test/berlin-schools",
                                                "freshness_label": "atlas",
                                            },
                                            {
                                                "risk_key": "crime_risk",
                                                "source_label": "District report",
                                                "title": "Area safety",
                                                "url": "https://example.test/berlin-safety",
                                                "freshness_label": "district",
                                            },
                                        ],
                                    },
                                    "media_attention": [
                                        {
                                            "source_label": "Tagesspiegel",
                                            "title": "U6 station upgrade",
                                            "url": "https://example.test/u6-upgrade",
                                            "freshness_label": "3 days ago",
                                        }
                                    ],
                                },
                            },
                            {
                                "title": "Family flat near Tiergarten",
                                "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                                "fit_summary": "Personal fit 87/100 · shortlist · Larger layout and quieter block.",
                                "recommendation": "shortlist",
                                "review_url": "/app/handoffs/human_task:review-2",
                                "tour_url": "",
                                "match_reasons": ["Larger layout and quieter block."],
                                "mismatch_reasons": ["No 360 tour yet."],
                                "property_facts": {
                                    "price_display": "EUR 465,000",
                                    "price_eur": 465000.0,
                                    "rooms": 4,
                                    "area_m2": 92,
                                    "postal_name": "Berlin Tiergarten",
                                    "floorplan_urls_json": [
                                        "https://assets.example.test/family-tiergarten-floorplan.png"
                                    ],
                                },
                            },
                            {
                                "title": "Listing URL only loft",
                                "listing_url": "https://www.immobilienscout24.de/expose/listing-url-only-loft",
                                "fit_summary": "Personal fit 84/100 · shortlist · Source adapter only supplied listing_url.",
                                "recommendation": "shortlist",
                                "review_url": "/app/handoffs/human_task:review-listing-only",
                                "tour_url": "",
                                "match_reasons": ["Valid listing URL is available."],
                                "mismatch_reasons": ["No 360 tour yet."],
                                "property_facts": {
                                    "price_display": "EUR 455,000",
                                    "price_eur": 455000.0,
                                    "rooms": 3,
                                    "area_m2": 82,
                                    "postal_name": "Berlin Charlottenburg",
                                },
                            },
                        ],
                    }
                ],
            },
            "events": [
                {"step": "sources_resolved", "message": "Resolved 2 provider(s) for scanning.", "status": "in_progress"},
                {"step": "completed", "message": "Property scouting run completed.", "status": "processed"},
            ],
        }

    def _fake_handoffs(self, *, principal_id: str, limit: int = 20, operator_id: str = "", status: str | None = "pending"):
        assert principal_id == "pq-greenfield-browser"
        return (
            HandoffNote(
                id="human_task:tour-1",
                queue_item_ref="queue:tour-1",
                summary="Hosted 3D page for Auhofstrasse shortlist",
                owner="office",
                due_time=None,
                escalation_status="high",
                task_type="property_tour_followup",
                delivery_reason="Lift, playground and subway fit the profile.",
                property_url="https://www.kalandra.at/objekt/14997053",
                tour_url="/tours/auhofstrasse-14997053",
            ),
        )

    def _fake_persist_decision_loop(self, *, principal_id: str, person_id: str, snapshot: object) -> dict[str, object]:
        assert principal_id == "pq-greenfield-browser"
        return {
            "persisted": True,
            "decision_id": str(getattr(getattr(snapshot, "decision", None), "decision_id", "")),
            "person_id": person_id,
            "source": "browser_fixture",
        }

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_run_status)
    monkeypatch.setattr(ProductService, "list_handoffs", _fake_handoffs)
    monkeypatch.setattr(ProductService, "_persist_property_decision_loop", _fake_persist_decision_loop)

    app = create_app()
    test_client = TestClient(app, base_url="https://propertyquarry.com")
    cleanup.callback(test_client.close)
    test_client.headers.update({"X-EA-Principal-ID": "pq-greenfield-browser", "host": "propertyquarry.com"})

    port = _free_port()
    browser_base_url = f"http://propertyquarry.localhost:{port}"
    monkeypatch.setenv("EA_PUBLIC_APP_BASE_URL", browser_base_url)
    config = Config(app=app, host="127.0.0.1", port=port, log_level="warning")
    server = Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    cleanup.callback(
        _stop_uvicorn_server,
        server=server,
        thread=thread,
        label="propertyquarry browser fixture",
    )
    local_base_url = f"http://127.0.0.1:{port}"
    _wait_for_http(local_base_url)
    yield {"base_url": browser_base_url, "client": test_client, "bundle_root": bundle_root}


@pytest.fixture()
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        engine = _configured_propertyquarry_browser_engine()
        try:
            browser = playwright_engine_launch_browser(
                playwright,
                engine=engine,
                args=list(_PROPERTYQUARRY_CHROMIUM_LAUNCH_ARGS),
            )
        except Exception as exc:
            raise RuntimeError(f"playwright_browser_engine_unavailable:{engine}:{type(exc).__name__}: {exc}") from exc
        try:
            yield browser
        finally:
            browser.close()


def _new_context(
    browser: Browser,
    *,
    mobile: bool = False,
    width: int | None = None,
    height: int | None = None,
    locale: str = "en-US",
) -> BrowserContext:
    return browser.new_context(
        viewport={
            "width": width if width is not None else (430 if mobile else 1440),
            "height": height if height is not None else (932 if mobile else 1100),
        },
        extra_http_headers={"X-EA-Principal-ID": "pq-greenfield-browser"},
        locale=locale,
        # Keep route-backed fixtures deterministic across browser engines. WebKit
        # otherwise lets an installed service worker consume intercepted media
        # and API requests before Playwright's context routes can observe them.
        service_workers="block",
    )


def _issue_browser_workspace_session(
    *,
    client: TestClient,
    context: BrowserContext,
    base_url: str,
) -> str:
    session_response = client.post(
        "/app/api/access-sessions",
        json={
            "email": "alice@example.com",
            "role": "principal",
            "display_name": "Alice",
            "expires_in_hours": 4,
        },
    )
    assert session_response.status_code == 200, session_response.text
    access_token = session_response.json().get("access_token", "")
    assert isinstance(access_token, str) and access_token.strip(), session_response.text
    context.add_cookies(
        [
            {
                "name": "ea_workspace_session",
                "value": access_token,
                "url": base_url,
            }
        ]
    )
    return access_token


def _new_public_context(
    browser: Browser,
    *,
    mobile: bool = False,
    width: int | None = None,
    height: int | None = None,
) -> BrowserContext:
    return browser.new_context(
        viewport={
            "width": width if width is not None else (430 if mobile else 1440),
            "height": height if height is not None else (932 if mobile else 1000),
        },
    )


def _assert_no_horizontal_overflow(page: Page) -> None:
    overflow = page.evaluate(
        """() => ({
            innerWidth: window.innerWidth,
            scrollWidth: document.documentElement.scrollWidth,
            bodyScrollWidth: document.body ? document.body.scrollWidth : 0,
            offenders: Array.from(document.querySelectorAll('body *'))
              .map((node) => {
                const rect = node.getBoundingClientRect();
                return {
                  tag: node.tagName,
                  className: String(node.className || ''),
                  text: String(node.textContent || '').trim().slice(0, 90),
                  left: Math.round(rect.left),
                  right: Math.round(rect.right),
                  width: Math.round(rect.width),
                };
              })
              .filter((row) => row.right > window.innerWidth + 1 || row.left < -1)
              .slice(0, 8),
        })"""
    )
    assert overflow["scrollWidth"] <= overflow["innerWidth"] + 1, overflow
    assert overflow["bodyScrollWidth"] <= overflow["innerWidth"] + 1, overflow


def test_propertyquarry_ai_panorama_mobile_hotspot_labels_stay_inside_viewport(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    def _panorama_data_url(
        fill: tuple[int, int, int],
        *,
        size: tuple[int, int] = (1024, 512),
    ) -> str:
        image = Image.new("RGB", size, fill)
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            (30, 30, size[0] - 30, size[1] - 30),
            outline=(248, 245, 240),
            width=10,
        )
        draw.line(
            (size[0] // 2, 30, size[0] // 2, size[1] - 30),
            fill=(151, 111, 77),
            width=8,
        )
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    disclosure = "AI-reconstructed from listing photos; not a captured 360 or measured survey."
    top_label = "Upper landing"
    bottom_label = "Lower landing"
    floorplan_label = "Floor plan edge"
    center_label = "Center option A"
    second_center_label = "Center option B"
    long_label = "Upstairs dining gallery with a deliberately long mobile navigation label that remains fully accessible"
    document = _tour_control_panorama_html(
        {
            "title": "Mobile hotspot containment",
            "display_title": "Mobile hotspot containment",
        },
        panorama_spec={
            "representation_kind": "ai_reconstruction",
            "representation_disclosure": disclosure,
            "initial_scene_id": "hall",
            "scenes": [
                {
                    "id": "hall",
                    "label": "Main level · Hall and stair",
                    "image_url": _panorama_data_url((226, 220, 210)),
                    "start_yaw": 65,
                    "start_pitch": 0,
                    "start_fov": 72,
                    "hotspots": [
                        {
                            "target": "upper-room",
                            "label": top_label,
                            "yaw": 65,
                            "pitch": 35,
                        },
                        {
                            "target": "lower-room",
                            "label": bottom_label,
                            "yaw": 65,
                            "pitch": -35,
                        },
                        {
                            "target": "floorplan-room",
                            "label": floorplan_label,
                            "yaw": 76,
                            "pitch": -17,
                        },
                        {
                            "target": "center-room",
                            "label": center_label,
                            "yaw": 65,
                            "pitch": 0,
                        },
                        {
                            "target": "second-center-room",
                            "label": second_center_label,
                            "yaw": 65,
                            "pitch": 0,
                        },
                        {
                            "target": "long-label-room",
                            "label": long_label,
                            "yaw": 65,
                            "pitch": 0,
                        },
                    ],
                },
                *[
                    {
                        "id": scene_id,
                        "label": scene_label,
                        "image_url": _panorama_data_url(fill),
                        "start_yaw": 0,
                        "start_pitch": 0,
                        "start_fov": 72,
                        "hotspots": [],
                    }
                    for scene_id, scene_label, fill in (
                        ("upper-room", "Upper destination", (205, 216, 226)),
                        ("lower-room", "Lower destination", (214, 205, 226)),
                        ("floorplan-room", "Floor plan destination", (226, 210, 205)),
                        ("center-room", "Center destination A", (205, 226, 212)),
                        ("second-center-room", "Center destination B", (226, 224, 205)),
                        ("long-label-room", "Long-label destination", (210, 220, 226)),
                    )
                ],
            ],
            "floorplan_url": _panorama_data_url((245, 242, 236), size=(512, 512)),
            "spatial_model": {},
        },
        provider_label="PropertyQuarry AI 360",
        viewer_name="propertyquarry-ai-panorama",
        nonce="mobile-hotspot-test-nonce",
    )
    three_module = """
      export const SRGBColorSpace = 'srgb';
      export const MathUtils = { degToRad: value => Number(value) * Math.PI / 180 };
      const normalizedAngle = value => Math.atan2(Math.sin(value), Math.cos(value));
      export class Vector3 {
        constructor(x = 0, y = 0, z = 0) { this.set(x, y, z); }
        set(x, y, z) { this.x = x; this.y = y; this.z = z; return this; }
        clone() { return new Vector3(this.x, this.y, this.z); }
        normalize() {
          const length = Math.hypot(this.x, this.y, this.z) || 1;
          this.x /= length; this.y /= length; this.z /= length;
          return this;
        }
        dot(other) { return this.x * other.x + this.y * other.y + this.z * other.z; }
        project(camera) {
          const normalized = this.clone().normalize();
          const target = camera._forward.clone().normalize();
          const vectorYaw = Math.atan2(-normalized.z, -normalized.x);
          const cameraYaw = Math.atan2(-target.z, -target.x);
          const vectorPitch = Math.asin(normalized.y);
          const cameraPitch = Math.asin(target.y);
          const verticalScale = Math.tan((camera.fov * Math.PI / 180) / 2);
          this.x = Math.tan(normalizedAngle(vectorYaw - cameraYaw)) / (verticalScale * camera.aspect);
          this.y = Math.tan(vectorPitch - cameraPitch) / verticalScale;
          this.z = 0;
          return this;
        }
      }
      class Positioned {
        constructor() { this.position = new Vector3(); this.rotation = { y: 0 }; this.userData = {}; }
      }
      export class PerspectiveCamera extends Positioned {
        constructor(fov, aspect) { super(); this.fov = fov; this.aspect = aspect; this._forward = new Vector3(0, 0, -1); }
        lookAt(target) { this._forward = target.clone().normalize(); }
        getWorldDirection(target) { return target.set(this._forward.x, this._forward.y, this._forward.z); }
        updateProjectionMatrix() {}
      }
      export class WebGLRenderer {
        constructor() {
          this.domElement = document.createElement('canvas');
          this.capabilities = { getMaxAnisotropy: () => 1 };
        }
        setPixelRatio() {}
        setSize(width, height) {
          this.domElement.width = width; this.domElement.height = height;
          this.domElement.style.width = `${width}px`; this.domElement.style.height = `${height}px`;
        }
        render() {}
        setAnimationLoop(callback) {
          const tick = () => { callback(); this._frame = requestAnimationFrame(tick); };
          tick();
        }
      }
      export class Scene { add() {} }
      export class Group { add() {} }
      export class SphereGeometry { scale() {} }
      export class BoxGeometry {}
      export class MeshBasicMaterial { constructor(values = {}) { Object.assign(this, values); this.color = { setHex() {} }; } }
      export class MeshStandardMaterial extends MeshBasicMaterial {}
      export class Mesh extends Positioned { constructor(geometry, material) { super(); this.geometry = geometry; this.material = material; } }
      export class Color { constructor(value) { this.value = value; } }
      export class Fog { constructor(color, near, far) { this.color = color; this.near = near; this.far = far; } }
      export class HemisphereLight extends Positioned {}
      export class DirectionalLight extends Positioned {}
      export class Raycaster { setFromCamera() {} intersectObjects() { return []; } }
      export class Vector2 { constructor(x = 0, y = 0) { this.x = x; this.y = y; } }
      export class TextureLoader {
        setCrossOrigin() {}
        load(_url, onLoad) { queueMicrotask(() => onLoad({ dispose() {} })); }
      }
    """

    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_public_context(browser, mobile=True, width=390, height=844)
    page = context.new_page()
    page_errors: list[str] = []
    failed_requests: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("requestfailed", lambda request: failed_requests.append(request.url))
    try:
        test_path = "/__renderer_mobile_hotspot_test"
        page.route(
            f"**{test_path}",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=document,
            ),
        )
        page.route(
            "**/tours/runtime/three-*.module.js",
            lambda route: route.fulfill(
                status=200,
                content_type="text/javascript; charset=utf-8",
                body=three_module,
            ),
        )
        response = page.goto(f"{base_url}{test_path}", wait_until="domcontentloaded")
        assert response is not None and response.ok
        try:
            page.locator("#viewer canvas").wait_for(state="visible", timeout=15_000)
        except PlaywrightTimeoutError:
            pytest.fail(
                f"panorama renderer did not start; page_errors={page_errors}, "
                f"failed_requests={failed_requests}"
            )
        page.locator("#status").wait_for(state="hidden", timeout=60_000)

        def _layout() -> dict[str, object]:
            return page.evaluate(
                """() => {
                  const row = element => {
                    const rect = element.getBoundingClientRect();
                    return {
                      label: element.textContent.trim(),
                      left: rect.left, right: rect.right,
                      top: rect.top, bottom: rect.bottom,
                      width: rect.width, height: rect.height,
                    };
                  };
                  const visible = element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return !element.hidden && style.display !== 'none' && style.visibility !== 'hidden'
                      && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
                  };
                  const layer = document.getElementById('hotspots');
                  const safeStyle = getComputedStyle(layer);
                  const safe = side => Math.max(0, parseFloat(safeStyle.getPropertyValue(`--pq-safe-${side}`)) || 0);
                  const viewport = window.visualViewport;
                  const left = viewport && Number.isFinite(viewport.offsetLeft) ? viewport.offsetLeft : 0;
                  const top = viewport && Number.isFinite(viewport.offsetTop) ? viewport.offsetTop : 0;
                  const width = viewport && Number.isFinite(viewport.width) ? viewport.width : innerWidth;
                  const height = viewport && Number.isFinite(viewport.height) ? viewport.height : innerHeight;
                  const obstacleSelectors = {
                    identity: '.identity', top_actions: '.top-actions', zoom_controls: '.zoom-controls',
                    scene_rail: '.scene-rail:not([hidden])',
                    floorplan: '.floorplan:not([hidden]):not(.collapsed)',
                  };
                  const obstacles = {};
                  for (const [name, selector] of Object.entries(obstacleSelectors)) {
                    const element = document.querySelector(selector);
                    if (element && visible(element)) obstacles[name] = row(element);
                  }
                  return {
                    bounds: {
                      left: Math.max(0, left) + safe('left') + 10,
                      right: Math.min(innerWidth, left + width) - safe('right') - 10,
                      top: Math.max(0, top) + safe('top') + 10,
                      bottom: Math.min(innerHeight, top + height) - safe('bottom') - 10,
                    },
                    hotspots: [...document.querySelectorAll('.hotspot')].filter(visible).map(row),
                    obstacles,
                  };
                }"""
            )

        def _intersects(left: dict[str, object], right: dict[str, object], gap: float = 0) -> bool:
            return (
                float(left["left"]) < float(right["right"]) + gap
                and float(left["right"]) > float(right["left"]) - gap
                and float(left["top"]) < float(right["bottom"]) + gap
                and float(left["bottom"]) > float(right["top"]) - gap
            )

        def _assert_collision_free(expected_labels: set[str]) -> dict[str, object]:
            layout = _layout()
            bounds = dict(layout["bounds"])
            hotspots = [dict(row) for row in list(layout["hotspots"])]
            obstacles = {name: dict(row) for name, row in dict(layout["obstacles"]).items()}
            labels = {str(row["label"]) for row in hotspots}
            assert expected_labels <= labels, {"missing": expected_labels - labels, "layout": layout}
            for hotspot_row in hotspots:
                assert float(hotspot_row["left"]) >= float(bounds["left"]) - 0.5, layout
                assert float(hotspot_row["right"]) <= float(bounds["right"]) + 0.5, layout
                assert float(hotspot_row["top"]) >= float(bounds["top"]) - 0.5, layout
                assert float(hotspot_row["bottom"]) <= float(bounds["bottom"]) + 0.5, layout
                for obstacle_name, obstacle in obstacles.items():
                    assert not _intersects(hotspot_row, obstacle, 7), {
                        "hotspot": hotspot_row,
                        "obstacle_name": obstacle_name,
                        "obstacle": obstacle,
                    }
            for index, hotspot_row in enumerate(hotspots):
                for other in hotspots[index + 1 :]:
                    assert not _intersects(hotspot_row, other, 7), {
                        "hotspot": hotspot_row,
                        "other": other,
                    }
            return layout

        page.locator("#hotspots").evaluate(
            """element => {
              element.style.setProperty('--pq-safe-top', '24px');
              element.style.setProperty('--pq-safe-right', '18px');
              element.style.setProperty('--pq-safe-bottom', '22px');
              element.style.setProperty('--pq-safe-left', '16px');
            }"""
        )
        map_toggle = page.locator("#map-toggle")
        map_toggle.click()
        expect(map_toggle).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#floorplan")).to_be_visible()
        page.wait_for_timeout(300)

        expected = {
            top_label,
            bottom_label,
            floorplan_label,
            center_label,
            second_center_label,
            long_label,
        }
        portrait_layout = _assert_collision_free(expected)
        assert "floorplan" in dict(portrait_layout["obstacles"]), portrait_layout

        long_hotspot = page.locator('.hotspot[data-target-scene-id="long-label-room"]')
        expect(long_hotspot).to_be_visible()
        expect(long_hotspot).to_have_accessible_name(long_label)
        assert long_hotspot.text_content() == long_label
        assert long_hotspot.evaluate("element => getComputedStyle(element).textOverflow") == "ellipsis"
        assert long_hotspot.evaluate("element => element.scrollWidth > element.clientWidth") is True

        center_hotspot = page.locator('.hotspot[data-target-scene-id="center-room"]')
        expect(center_hotspot).to_have_accessible_name(center_label)
        center_hotspot.click()
        expect(page.locator("#scene-title")).to_have_text("Center destination A")
        expect(page.locator("#announcer")).to_have_text("Now viewing Center destination A")

        page.locator('.scene-button[data-scene-id="hall"]').click()
        expect(page.locator("#scene-title")).to_have_text("Main level · Hall and stair")
        map_toggle.click()
        expect(map_toggle).to_have_attribute("aria-pressed", "false")
        page.set_viewport_size({"width": 667, "height": 375})
        page.locator("#hotspots").evaluate(
            """element => {
              element.style.setProperty('--pq-safe-top', '18px');
              element.style.setProperty('--pq-safe-right', '18px');
              element.style.setProperty('--pq-safe-bottom', '20px');
              element.style.setProperty('--pq-safe-left', '16px');
            }"""
        )
        page.wait_for_timeout(300)
        landscape_layout = _assert_collision_free({top_label, bottom_label})
        landscape_bounds = dict(landscape_layout["bounds"])
        assert float(landscape_bounds["left"]) >= 26
        assert float(landscape_bounds["right"]) <= 639
        assert float(landscape_bounds["top"]) >= 28
        assert float(landscape_bounds["bottom"]) <= 345
        expect(long_hotspot).not_to_be_visible()
        expect(long_hotspot).to_have_attribute("aria-hidden", "true")
        expect(long_hotspot).to_have_attribute("tabindex", "-1")
        assert long_hotspot.text_content() == long_label

        assert page.locator("body").get_attribute("data-viewer") == "propertyquarry-ai-panorama"
        assert disclosure in page.locator(".identity span").inner_text()
        assert not page_errors
        assert not failed_requests
    finally:
        context.close()


def _assert_no_viewport_or_text_cutoff(page: Page) -> None:
    report = page.evaluate(
        """() => {
          const root = document.documentElement;
          const viewportWidth = root.clientWidth;
          const horizontalDocumentOverflow = root.scrollWidth - viewportWidth;
          const selectors = [
            'button', 'a', 'label', 'legend', 'summary',
            'h1', 'h2', 'h3', 'h4', '[role="button"]',
            '.pq-copy', '.pqx-note', '.pqx-small', '.prd-row'
          ];
          const offenders = [];
          for (const element of document.querySelectorAll(selectors.join(','))) {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            if (
              rect.width <= 0 ||
              rect.height <= 0 ||
              style.visibility === 'hidden' ||
              style.display === 'none'
            ) continue;
            const intentionallyVisuallyHidden =
              rect.width <= 2 &&
              rect.height <= 2 &&
              style.position === 'absolute' &&
              ['hidden', 'clip'].includes(style.overflow);
            if (intentionallyVisuallyHidden) continue;
            const closedDetails = element.closest('details:not([open])');
            if (closedDetails && !element.closest('summary')) continue;
            const text = (element.textContent || element.getAttribute('aria-label') || '').trim();
            if (!text) continue;
            let withinHorizontalScroller = false;
            for (
              let ancestor = element.parentElement;
              ancestor && ancestor !== document.body;
              ancestor = ancestor.parentElement
            ) {
              const ancestorStyle = getComputedStyle(ancestor);
              if (
                ancestor.scrollWidth > ancestor.clientWidth + 2 &&
                ['auto', 'scroll'].includes(ancestorStyle.overflowX)
              ) {
                withinHorizontalScroller = true;
                break;
              }
            }
            const outsideViewport =
              !withinHorizontalScroller &&
              (rect.left < -1 || rect.right > viewportWidth + 1);
            const clippedX =
              element.scrollWidth > element.clientWidth + 2 &&
              ['hidden', 'clip'].includes(style.overflowX);
            const clippedY =
              element.scrollHeight > element.clientHeight + 2 &&
              ['hidden', 'clip'].includes(style.overflowY);
            if (outsideViewport || clippedX || clippedY) {
              offenders.push({
                tag: element.tagName,
                text: text.slice(0, 120),
                outsideViewport,
                clippedX,
                clippedY,
                withinHorizontalScroller,
                left: Math.round(rect.left),
                right: Math.round(rect.right),
                clientWidth: element.clientWidth,
                scrollWidth: element.scrollWidth,
                clientHeight: element.clientHeight,
                scrollHeight: element.scrollHeight,
              });
            }
          }
          return {
            viewportWidth,
            documentScrollWidth: root.scrollWidth,
            horizontalDocumentOverflow,
            offenders: offenders.slice(0, 30),
          };
        }"""
    )
    assert int(report["horizontalDocumentOverflow"]) <= 1, report
    if report["offenders"]:
        raise AssertionError(json.dumps(report, indent=2, ensure_ascii=False))


def _active_property_setup_step(page: Page) -> str:
    return str(
        page.evaluate(
            """() => document.querySelector(
              '[data-console-form-variant="property_search"]'
            )?.dataset.propertyActiveStep || ''"""
        )
    )


def test_propertyquarry_every_search_setup_button_works_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        response = page.goto(f"{base_url}/app/properties", wait_until="networkidle")
        assert response is not None and response.ok

        theme_toggle = page.locator("[data-pqx-theme-toggle]")
        expect(theme_toggle).to_have_text("Dark mode")
        theme_toggle.click()
        expect(theme_toggle).to_have_text("Light mode")
        theme_toggle.click()
        expect(theme_toggle).to_have_text("Dark mode")

        scope_preview = page.locator("[data-pqx-scope-open]").first
        scope_preview.click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).not_to_be_visible()
        page.wait_for_function(
            """() => Boolean(document.querySelector(
              '[data-console-form-variant="property_search"]'
            )?.dataset.propertyActiveStep)"""
        )

        step_names = ("search", "what", "children", "reachability", "research", "providers")
        for step_name in step_names:
            page.locator(f'[data-property-step-trigger="{step_name}"]').click()
            assert _active_property_setup_step(page) == step_name

        page.locator('[data-property-step-trigger="what"]').click()
        page.locator("[data-property-step-back]").click()
        assert _active_property_setup_step(page) == "search"
        page.locator("[data-property-step-next]").click()
        assert _active_property_setup_step(page) == "what"

        page.locator('[data-property-step-trigger="search"]').click()
        page.locator('[data-location-mode-button="map"]').click()
        expect(page.locator('[data-location-mode-button="map"]')).to_have_attribute(
            "aria-pressed", "true"
        )
        page.locator('[data-location-mode-button="list"]').click()
        expect(page.locator('[data-location-mode-button="list"]')).to_have_attribute(
            "aria-pressed", "true"
        )

        all_areas = page.locator('[data-checkbox-group-select-all="location_query"]')
        clear_areas = page.locator('[data-checkbox-group-clear-all="location_query"]')
        all_areas.click()
        assert page.locator('input[name="location_query"]:checked').count() > 0
        clear_areas.click()
        assert page.locator('input[name="location_query"]:checked').count() == 0

        page.locator('[data-location-mode-button="map"]').click()
        page.locator("[data-location-map-open]").click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.keyboard.press("Escape")

        page.locator('[data-property-step-trigger="what"]').click()
        tooltip_triggers = page.locator("[data-tooltip-trigger]:visible")
        assert tooltip_triggers.count() > 0
        for index in range(tooltip_triggers.count()):
            trigger = tooltip_triggers.nth(index)
            trigger.click()
            expect(trigger).to_have_attribute("data-tooltip-open", "true")
            trigger.click()

        page.locator('[data-property-step-trigger="children"]').click()
        page.locator("[data-pqx-save-what-matters]:visible").click()
        page.locator("[data-pqx-load-what-matters]:visible").click()

        page.locator('[data-property-step-trigger="providers"]').click()
        provider_select_all = page.locator(
            '[data-checkbox-group-select-all="selected_platforms"]'
        )
        provider_select_all.click()
        checked_provider_total = page.locator(
            'input[name="selected_platforms"]:checked'
        ).count()
        assert checked_provider_total > 0

        provider_groups = page.locator("[data-provider-group-panel]")
        assert provider_groups.count() > 0
        for index in range(provider_groups.count()):
            group = provider_groups.nth(index)
            group.locator("summary").click()
            add_family = group.get_by_role("button", name="Add family")
            clear_family = group.get_by_role("button", name="Clear family")
            if add_family.is_visible():
                add_family.click()
            if clear_family.is_visible():
                clear_family.click()
            if group.get_attribute("open") is not None:
                group.locator("summary").click()

        page.locator("[data-property-save-top]").click()
        expect(page.locator("[data-property-inline-status]")).not_to_be_empty()
        _assert_no_viewport_or_text_cutoff(page)
        assert page_errors == []
    finally:
        context.close()


def test_propertyquarry_browser_locales_do_not_clip(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    browser_locales = (
        ("en-US", "en"),
        ("de-AT", "de-AT"),
        ("de-DE", "de-DE"),
        ("es-CR", "es-CR"),
        ("fr-FR", "en"),
        ("it-IT", "en"),
        ("nl-NL", "en"),
        ("pt-PT", "en"),
        ("pl-PL", "en"),
        ("sv-SE", "en"),
    )
    step_names = ("search", "what", "children", "reachability", "research", "providers")
    for mobile in (False, True):
        for browser_locale, expected_document_language in browser_locales:
            context = _new_context(browser, mobile=mobile, locale=browser_locale)
            page = context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            try:
                response = page.goto(
                    f"{base_url}/app/properties",
                    wait_until="networkidle",
                )
                assert response is not None and response.ok
                assert page.evaluate("() => navigator.language") == browser_locale
                document_language = str(
                    page.locator("html").get_attribute("lang") or ""
                )
                assert document_language == expected_document_language
                page.wait_for_function(
                    """() => Boolean(document.querySelector(
                      '[data-console-form-variant="property_search"]'
                    )?.dataset.propertyActiveStep)"""
                )

                for step_name in step_names:
                    page.locator(f'[data-property-step-trigger="{step_name}"]').click()
                    assert _active_property_setup_step(page) == step_name
                    _assert_no_viewport_or_text_cutoff(page)
                assert page_errors == [], {
                    "browser_locale": browser_locale,
                    "mobile": mobile,
                    "page_errors": page_errors,
                }
            finally:
                context.close()


def test_propertyquarry_active_search_board_stays_localized_after_browser_hydration(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _active_run_status(
        self,
        *,
        principal_id: str,
        run_id: str,
        **_kwargs,
    ) -> dict[str, object]:
        return {
            "generated_at": "2026-07-29T08:30:00+00:00",
            "created_at": "2026-07-29T08:29:00+00:00",
            "updated_at": "2026-07-29T08:30:00+00:00",
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "status_url": f"/app/api/property/search-runs/{run_id}",
            "selected_platforms": ["willhaben", "immoscout_at", "immobilien_at"],
            "progress": 3,
            "current_step": "sources_resolved",
            "message": "Untranslated future provider status.",
            "provider_display_total": 3,
            "source_variant_display_total": 0,
            "selected_platform_count": 3,
            "summary": {
                "status": "in_progress",
                "progress": 3,
                "provider_display_total": 3,
                "provider_total": 3,
                "sources_total": 3,
                "sources_completed": 0,
                "found_listing_total": 54,
                "to_review_listing_total": 54,
                "ranked_candidates": [],
            },
            "events": [
                {
                    "step": "queued",
                    "message": "Queued for execution.",
                },
                {
                    "step": "source_assessing",
                    "message": (
                        "Checking fit for Kleine Mietwohnung in Spielberg "
                        "(25 of 30) from Willhaben."
                    ),
                },
                {
                    "step": "source_preview_prepare",
                    "message": "Prepared 20 of 30 listing preview(s) from Willhaben.",
                },
                {
                    "step": "source_search",
                    "message": "Untranslated future provider event.",
                }
            ],
            "research_tasks": [],
        }

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _active_run_status)
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(
        browser,
        mobile=False,
        width=1440,
        height=900,
        locale="de-AT",
    )
    page: Page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        response = page.goto(
            f"{base_url}/app/properties?run_id=run-active-localized",
            wait_until="networkidle",
        )
        assert response is not None and response.ok
        board = page.locator("[data-pqx-progress-board]").first
        expect(board).to_be_visible()
        expect(board.locator("[data-pqx-progress-board-title]")).to_have_text("Suche läuft")
        expect(board.locator("[data-pqx-radar-count]")).to_have_text("0 / 3 Quellen")
        expect(board.locator("[data-pqx-progress-eta]")).to_have_text("3% · ca. 5 Min.")
        expect(board).to_contain_text("54 Immobilien gefunden")
        expect(page.locator("[data-pqx-run-message]")).to_have_text(
            "54 Immobilien gefunden · 54 zu prüfen · 3 Quellen offen"
        )
        expect(page.locator("[data-pqx-run-events]")).to_contain_text(
            "Zur Ausführung eingeplant."
        )
        expect(page.locator("[data-pqx-run-events]")).to_contain_text(
            "Eignung wird geprüft: Kleine Mietwohnung in Spielberg "
            "(25 von 30) von Willhaben."
        )
        fit_event = page.locator("[data-pqx-run-events] .pqx-event-card").filter(
            has_text="Kleine Mietwohnung in Spielberg"
        )
        expect(fit_event.locator("strong")).to_have_text("Eignung")
        expect(page.locator("[data-pqx-run-events]")).to_contain_text(
            "20 von 30 Inseratsvorschauen von Willhaben vorbereitet."
        )
        expect(page.locator("[data-pqx-run-events]")).to_contain_text(
            "Suche läuft weiter."
        )
        expect(page.locator("[data-pqx-run-summary]")).to_contain_text("3 Quellen offen")
        expect(board).to_contain_text("Immobilien")
        expect(board).to_contain_text("Zu prüfen")
        expect(board).to_contain_text("Geprüft")
        expect(page.locator("[data-pqx-theme-toggle]")).to_have_text("Dunkelmodus")
        browser_alerts = page.locator("[data-pqx-browser-alerts]")
        if browser_alerts.is_visible():
            expect(browser_alerts).to_have_text("Browser-Benachrichtigungen aus")
        expect(page.locator("[data-pqx-running-details] summary")).to_contain_text(
            "Aktualisierungen"
        )
        body_text = page.locator("body").inner_text()
        for leaked_copy in (
            "Searching",
            "sites chosen",
            "Waiting for homes.",
            "sources left",
            "Prepared 20 of 30 listing preview(s) from Willhaben.",
            "about 5 min",
            "Queued for execution.",
            "Checking fit for",
            "Untranslated future provider",
            "Property fit",
            "Browser alerts off",
            "Dark mode",
        ):
            assert leaked_copy not in body_text
        _assert_no_viewport_or_text_cutoff(page)
        assert page_errors == []
    finally:
        context.close()


def test_propertyquarry_processed_results_stay_localized_and_unclipped_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = {
        "rank": 1,
        "candidate_ref": "localized-result-1",
        "title": "Helle Gartenwohnung",
        "location_label": "8052 Graz",
        "price_display": "€ 690",
        "layout_display": "2 rooms | 80.0 m2",
        "fit_score": 56,
        "personal_fit_score": 56,
        "fit_summary": "It is meaningfully cheaper.",
        "source_url": "https://example.com/listing",
        "property_url": "https://example.com/listing",
        "floorplan_url": "https://example.com/floorplan.jpg",
        "tour_status": "unavailable",
        "property_facts": {
            "rooms": 2,
            "area_sqm": 80,
            "monthly_total_eur": 690,
        },
    }
    second_candidate = {
        **candidate,
        "rank": 2,
        "candidate_ref": "localized-result-2",
        "title": "Ruhige Wohnung am Park",
        "layout_display": "3.0 rooms | 67.0 m2",
        "floorplan_url": "https://example.com/floorplan-2.jpg",
    }
    ranked_candidates = [candidate, second_candidate]

    def _processed_run_status(
        self,
        *,
        principal_id: str,
        run_id: str,
        **_kwargs,
    ) -> dict[str, object]:
        return {
            "generated_at": "2026-07-29T09:00:00+00:00",
            "created_at": "2026-07-29T08:55:00+00:00",
            "updated_at": "2026-07-29T09:00:00+00:00",
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "processed",
            "progress": 100,
            "current_step": "results_finalizing",
            "message": "This search used an earlier brief.",
            "provider_display_total": 1,
            "selected_platform_count": 1,
            "summary": {
                "status": "processed",
                "progress": 100,
                "provider_display_total": 1,
                "provider_total": 1,
                "sources_total": 1,
                "sources_completed": 1,
                "found_listing_total": 1,
                "scanned_listing_total": 1,
                "to_review_listing_total": 0,
                "ranked_candidates": ranked_candidates,
            },
            "ranked_candidates": ranked_candidates,
            "events": [],
            "research_tasks": [],
        }

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _processed_run_status)
    base_url = str(propertyquarry_browser_server["base_url"])
    cases = (
        (
            "de-AT",
            False,
            1440,
            900,
            (
                "Gespeicherte Seiten",
                "Suche bearbeiten",
                "passende Immobilien",
                "2 Zimmer | 80.0 m2",
                "Eignung 56",
                "Deutlich günstiger.",
                "Angebot öffnen",
                "Größter Vorteil",
                "Zu beachten",
                "Preisniveau",
                "Suche anpassen",
                "Besichtigung angefragt",
                "Unterlagen angefragt",
                "Kaufangebot erwägen",
                "Archiviert",
                "Premium-Marktbericht",
                "Zur Kasse",
                "Bisher am besten",
                "Anbieter",
                "Warum sie passt",
                "Visualisierungen",
                "2 Treffer",
            ),
        ),
        (
            "es-CR",
            True,
            390,
            844,
            (
                "Páginas guardadas",
                "Editar búsqueda",
                "propiedades adecuadas",
                "2 habitaciones | 80.0 m2",
                "Compatibilidad 56",
                "Es considerablemente más económica.",
                "Abrir anuncio",
                "Punto fuerte",
                "Tenga en cuenta",
                "Nivel de precio",
                "Ajustar búsqueda",
                "Visita solicitada",
                "Documentos solicitados",
                "Candidata para oferta",
                "Archivada",
                "Informe de mercado premium",
                "Ir al pago",
                "La mejor hasta ahora",
                "Proveedor",
                "Por qué funciona",
                "Visualizaciones",
                "2 coincidencias",
            ),
        ),
    )
    for locale, mobile, width, height, expected_copy in cases:
        context = _new_context(
            browser,
            mobile=mobile,
            width=width,
            height=height,
            locale=locale,
        )
        page = context.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            response = page.goto(
                f"{base_url}/app/properties?run_id=run-processed-localized",
                wait_until="domcontentloaded",
            )
            assert response is not None and response.ok
            expect(page.locator("#results-list")).to_be_visible()
            for copy in expected_copy:
                expect(page.locator("body")).to_contain_text(copy)
            body_text = page.locator("body").inner_text()
            for leaked_copy in (
                "Saved pages",
                "Edit search",
                "matching homes",
                "2 rooms",
                "Fit 56",
                "It is meaningfully cheaper.",
                "Tour not available yet.",
                "Open listing",
                "Media preview not available.",
                "Best part",
                "Keep in mind",
                "Price level",
                "Adjust search",
                "Viewing requested",
                "Documents requested",
                "Offer candidate",
                "Premium market report",
                "Open checkout",
                "Best so far",
                "Provider",
                "Why it works",
                "Things to watch",
                "Visuals",
                "2 matches",
            ):
                assert leaked_copy not in body_text
            _assert_no_viewport_or_text_cutoff(page)
            assert page_errors == []
        finally:
            context.close()


def _visible_button_action_keys(page: Page) -> set[str]:
    return set(
        page.locator("button:visible:not([disabled])").evaluate_all(
            """buttons => buttons.flatMap((button) =>
              [...button.attributes]
                .map((attribute) => attribute.name)
                .filter((name) =>
                  name.startsWith('data-') &&
                  ![
                    'data-candidate-ref',
                    'data-pqx-scope-alt',
                    'data-pqx-scope-caption',
                    'data-pqx-scope-image',
                    'data-pqx-scope-overlay',
                    'data-pqx-scope-title',
                    'data-pqx-scope-lightbox-bound',
                    'data-pqx-deferred-preview-host',
                    'data-pqx-shortlist-diorama',
                    'data-prd-map-overlay',
                    'data-prd-map-src',
                    'data-prd-map-title',
                  ].includes(name)
                )
            )"""
        )
    )


def _best_match_packet_url(page: Page, *, base_url: str) -> str:
    packet_path = page.locator(
        "[data-workbench-row]",
        has_text="Altbau near U6",
    ).first.get_attribute("data-candidate-packet-url")
    assert packet_path
    return packet_path if packet_path.startswith("http") else f"{base_url}{packet_path}"


def test_propertyquarry_every_results_and_research_button_works_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    context.grant_permissions(
        ["clipboard-read", "clipboard-write", "notifications"],
        origin=base_url,
    )
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    shortlist_url = f"{base_url}/app/shortlist?run_id=run-42&full=1"
    try:
        response = page.goto(shortlist_url, wait_until="networkidle")
        assert response is not None and response.ok
        assert _visible_button_action_keys(page) == {
            "data-pqx-theme-toggle",
            "data-pqx-browser-alerts",
            "data-pqx-atlas-open",
            "data-pqx-scope-open",
            "data-workbench-select-candidate",
            "data-pw-remove-row",
            "data-pw-finetune-toggle",
            "data-pw-decision-state",
        }

        theme_toggle = page.locator("[data-pqx-theme-toggle]")
        theme_toggle.click()
        expect(theme_toggle).to_have_text("Light mode")
        theme_toggle.click()

        browser_alerts = page.locator("[data-pqx-browser-alerts]")
        expect(browser_alerts).to_be_visible()
        browser_alerts.click()
        expect(browser_alerts).to_have_text(re.compile(r"Browser alerts (on|enabled)"))

        atlas_buttons = page.locator("[data-pqx-atlas-open]:visible")
        atlas_refs = atlas_buttons.evaluate_all(
            "buttons => buttons.map((button) => button.getAttribute('data-candidate-ref'))"
        )
        assert len(atlas_refs) == 3
        for candidate_ref in atlas_refs:
            page.locator(
                f'[data-pqx-atlas-open][data-candidate-ref="{candidate_ref}"]'
            ).click()
            expect(page.get_by_role("dialog")).to_be_visible()
            page.keyboard.press("Escape")

        floorplan_preview = page.locator("[data-pqx-scope-open]:visible")
        expect(floorplan_preview).to_have_count(1)
        floorplan_preview.click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.keyboard.press("Escape")

        candidate_refs = page.locator(
            "[data-workbench-select-candidate]:visible"
        ).evaluate_all(
            "buttons => buttons.map((button) => button.getAttribute('data-candidate-ref'))"
        )
        assert len(candidate_refs) == 3
        for candidate_ref in candidate_refs:
            page.locator(
                f'[data-workbench-select-candidate][data-candidate-ref="{candidate_ref}"]'
            ).click()
            expect(page).to_have_url(re.compile(rf"candidate={re.escape(str(candidate_ref))}"))
            response = page.goto(shortlist_url, wait_until="networkidle")
            assert response is not None and response.ok

        remove_refs = page.locator("[data-pw-remove-row]:visible").evaluate_all(
            "buttons => buttons.map((button) => button.getAttribute('data-candidate-ref'))"
        )
        assert len(remove_refs) == 3
        for candidate_ref in remove_refs:
            row = page.locator(
                f'[data-workbench-row][data-candidate-ref="{candidate_ref}"]'
            )
            with page.expect_response("**/app/api/property-feedback") as feedback_response:
                page.locator(
                    f'[data-pw-remove-row][data-candidate-ref="{candidate_ref}"]'
                ).click()
            assert feedback_response.value.ok
            expect(row).not_to_be_visible()
            response = page.goto(shortlist_url, wait_until="networkidle")
            assert response is not None and response.ok

        fine_tune = page.locator("[data-pw-finetune-toggle]")
        fine_tune.click()
        expect(fine_tune).to_have_attribute("aria-expanded", "true")

        decision_buttons = page.locator(
            "[data-pw-decision-state]:visible:not([data-pw-remove-row])"
        )
        decision_states = decision_buttons.evaluate_all(
            "buttons => buttons.map((button) => button.getAttribute('data-pw-decision-state'))"
        )
        assert decision_states == [
            "viewing_requested",
            "documents_requested",
            "offer_candidate",
            "archived",
        ]
        for decision_state in decision_states:
            button = page.locator(
                f'[data-pw-decision-state="{decision_state}"]:visible:not([data-pw-remove-row])'
            )
            with page.expect_response("**/app/api/property-feedback") as feedback_response:
                button.click()
            assert feedback_response.value.ok
            expect(button).to_have_attribute("aria-pressed", "true")

        response = page.goto(shortlist_url, wait_until="networkidle")
        assert response is not None and response.ok
        packet_url = _best_match_packet_url(page, base_url=base_url)
        response = page.goto(packet_url, wait_until="networkidle")
        assert response is not None and response.ok
        assert _visible_button_action_keys(page) == {
            "data-prd-copy-link",
            "data-prd-map-open",
            "data-object-feedback-reaction",
            "data-object-feedback-save",
            "data-object-feedback-open-advanced",
        }

        copy_link = page.locator("[data-prd-copy-link]")
        copy_link.click()
        copied = page.evaluate("() => navigator.clipboard.readText()")
        assert "/app/research/" in str(copied)

        page.locator("[data-prd-map-open]").click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.keyboard.press("Escape")

        feedback_reactions = ("like", "maybe", "dislike", "hide")
        for reaction in feedback_reactions:
            reaction_button = page.locator(
                f'[data-object-feedback-reaction="{reaction}"]'
            )
            reaction_button.click()
            expect(reaction_button).to_have_attribute("aria-pressed", "true")

        page.locator("[data-object-feedback-save]").click()
        expect(page.locator("[data-object-feedback-status]")).not_to_be_empty()
        page.locator("[data-object-feedback-open-advanced]").click()
        expect(page.locator("[data-object-feedback-advanced]")).to_be_visible()

        _assert_no_viewport_or_text_cutoff(page)
        assert page_errors == []
    finally:
        context.close()


def test_propertyquarry_google_continue_initiates_google_redirect_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID", "propertyquarry-browser-client")
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET", "propertyquarry-browser-secret")
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI", f"{base_url}/google/callback")
    monkeypatch.setenv(
        "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
        "propertyquarry-browser-state-secret-with-enough-entropy",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
        "propertyquarry-browser-session-secret-with-enough-entropy",
    )
    context = _new_public_context(browser, mobile=False, width=1440, height=900)
    page = context.new_page()
    google_redirects: list[dict[str, object]] = []

    def _capture_google_start(route) -> None:  # type: ignore[no-untyped-def]
        response = route.fetch(max_redirects=0)
        google_redirects.append(
            {
                "status": response.status,
                "location": str(response.headers.get("location") or ""),
            }
        )
        route.fulfill(
            status=200,
            content_type="text/html",
            body="<title>Google handoff captured</title><h1>Google handoff captured</h1>",
        )

    page.route(re.compile(r"/sign-in/google\?"), _capture_google_start)
    try:
        response = page.goto(
            f"{base_url}/sign-in?return_to=%2Fapp%2Fproperties%3Frun_id%3Drun-browser&session=expired",
            wait_until="networkidle",
        )
        assert response is not None and response.ok
        continue_with_google = page.get_by_role("link", name="Continue with Google")
        expect(continue_with_google).to_be_visible()

        continue_with_google.click()

        expect(page.get_by_role("heading", name="Google handoff captured")).to_be_visible()
        assert len(google_redirects) == 1
        assert google_redirects[0]["status"] == 303
        location = str(google_redirects[0]["location"])
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        assert query["redirect_uri"] == [f"{base_url}/google/callback"]
        assert query["scope"] == ["openid email profile"]
        assert query["state"][0].startswith("pqg1.")
    finally:
        context.close()


def test_propertyquarry_expired_session_without_google_has_visible_recovery_not_dead_focus(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    for name in (
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
        "PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI",
        "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    context = _new_public_context(browser, mobile=False, width=1440, height=900)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/sign-in?session=expired", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.get_by_text("Google sign-in is temporarily unavailable.")).to_be_visible()
        expect(page.get_by_role("link", name="Check Google again")).to_be_visible()
        assert page.get_by_role("link", name="Continue with Google").count() == 0
        focus_only = page.locator("[data-focus-sign-in-options]")
        if focus_only.count():
            assert page.locator('form[action="/sign-in/email-link"] input[type="email"]').count() == 1

        page.get_by_role("link", name="Check Google again").click()
        page.wait_for_url(re.compile(r"/sign-in\?return_to=.*#sign-in-options$"))
        assert page.url.startswith(f"{base_url}/sign-in?")
    finally:
        context.close()


def _property_fact_browser_field(
    *,
    key: str,
    label: str,
    state: str,
    priority: str,
    value: int | None = None,
    error_message: str = "",
) -> dict[str, object]:
    resolved = state == "resolved" and value is not None
    return {
        "key": key,
        "label": label,
        "state": state,
        "priority": priority,
        "affects_score": True,
        "value": value if resolved else None,
        "display_value": f"{value} m" if resolved else "",
        "provenance": (
            {
                "provider": "openstreetmap_overpass",
                "method": "straight_line_osm",
                "observed_at": "2026-07-21T10:00:00+00:00",
                "expires_at": "2026-07-22T10:00:00+00:00",
                "freshness": "fresh",
                "confidence": 0.74,
                "source_key": key,
                "source_fingerprint": "sha256:browser-proof",
            }
            if resolved
            else {}
        ),
        "error": (
            {
                "code": "fact_not_available_from_current_sources",
                "message": error_message,
                "retry_after_seconds": 1,
            }
            if error_message
            else {}
        ),
    }


def _property_fact_browser_fields(
    *,
    state: str,
    required: bool,
    resolved: bool = False,
    error_message: str = "",
) -> list[dict[str, object]]:
    priorities = {
        "nearest_supermarket_m": "required" if required else "lazy",
        "nearest_playground_m": "required" if required else "lazy",
        "nearest_pharmacy_m": "lazy",
        "nearest_medical_care_m": "required" if required else "lazy",
        "nearest_subway_m": "lazy",
    }
    values = {
        "nearest_supermarket_m": 280,
        "nearest_playground_m": 420,
        "nearest_pharmacy_m": 510,
        "nearest_medical_care_m": 640,
        "nearest_subway_m": 760,
    }
    labels = {
        "nearest_supermarket_m": "Supermarket distance",
        "nearest_playground_m": "Playground distance",
        "nearest_pharmacy_m": "Pharmacy distance",
        "nearest_medical_care_m": "Medical-care distance",
        "nearest_subway_m": "Underground distance",
    }
    return [
        _property_fact_browser_field(
            key=key,
            label=labels[key],
            state="resolved" if resolved else state,
            priority=priority,
            value=values[key] if resolved else None,
            error_message=error_message,
        )
        for key, priority in priorities.items()
    ]


def _property_fact_browser_payload(
    *,
    status_url: str,
    status: str,
    fields: list[dict[str, object]],
    score_state: str,
    previous_score: int | None,
    current_score: int | None,
    delta: int | None,
    ranking_eligible: bool,
    retryable: bool = False,
    bundle_kind: str = "optional-geo-v1",
    job_id: str = "pfe_browser_proof",
) -> dict[str, object]:
    return {
        "schema_version": "propertyquarry.fact-enrichment.v1",
        "job_id": job_id,
        "bundle_kind": bundle_kind,
        "run_id": "run-42",
        "candidate_ref": "browser-candidate",
        "status": status,
        "status_url": status_url,
        "attempt": 1,
        "poll_after_ms": 250,
        "updated_at": "2026-07-21T10:00:00+00:00",
        "retryable": retryable,
        "fields": fields,
        "score": {
            "state": score_state,
            "previous": previous_score,
            "current": current_score,
            "delta": delta,
            "changed_reasons": ["Nearby necessities updated from current map sources."] if delta else [],
            "ranking_eligible": ranking_eligible,
            "algorithm_version": "propertyquarry.fact-score-state.v1",
            "facts_digest": "sha256:browser-facts",
            "preference_digest": "sha256:browser-preferences",
        },
    }


def _open_family_research_packet(page: Page, *, base_url: str) -> None:
    response = page.goto(
        f"{base_url}/app/properties?run_id=run-42&full=1",
        wait_until="domcontentloaded",
    )
    assert response is not None and response.ok
    row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
    expect(row).to_be_visible(timeout=5000)
    row.click()
    selected_panel = page.get_by_role("region", name="Selected property")
    open_property = selected_panel.get_by_role("link", name="Open property").first
    expect(open_property).to_be_visible(timeout=5000)
    href = str(open_property.get_attribute("href") or "").strip()
    assert href
    response = page.goto(urllib.parse.urljoin(base_url, href), wait_until="domcontentloaded")
    assert response is not None and response.ok


def _open_authenticated_property_fact_session(
    page: Page,
    *,
    base_url: str,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    client = propertyquarry_browser_server["client"]
    issued = client.post(
        "/app/api/access-sessions",
        json={
            "email": "property-fact-browser@example.com",
            "role": "principal",
            "display_name": "Property fact browser",
            "expires_in_hours": 1,
        },
    )
    assert issued.status_code == 200, issued.text
    access_url = str(issued.json().get("access_url") or "").strip()
    assert access_url.startswith("/workspace-access/")
    response = page.goto(urllib.parse.urljoin(base_url, access_url), wait_until="domcontentloaded")
    assert response is not None and response.ok


def test_propertyquarry_missing_required_distances_resolve_in_place_and_finalize_score(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    allow_resolution = {"value": False}
    observed_posts: list[dict[str, object]] = []
    poll_count = {"value": 0}

    def _fact_route(route) -> None:
        request = route.request
        status_url = request.url
        if request.method == "POST":
            payload = request.post_data_json
            observed_posts.append(payload if isinstance(payload, dict) else {})
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    _property_fact_browser_payload(
                        status_url=status_url,
                        status="queued",
                        fields=_property_fact_browser_fields(state="queued", required=True),
                        score_state="evaluating",
                        previous_score=72,
                        current_score=None,
                        delta=None,
                        ranking_eligible=False,
                    )
                ),
            )
            return
        poll_count["value"] += 1
        if not allow_resolution["value"]:
            payload = _property_fact_browser_payload(
                status_url=status_url,
                status="running",
                fields=_property_fact_browser_fields(state="running", required=True),
                score_state="evaluating",
                previous_score=72,
                current_score=None,
                delta=None,
                ranking_eligible=False,
            )
        else:
            payload = _property_fact_browser_payload(
                status_url=status_url,
                status="succeeded",
                fields=_property_fact_browser_fields(state="resolved", required=True, resolved=True),
                score_state="final",
                previous_score=72,
                current_score=76,
                delta=4,
                ranking_eligible=True,
            )
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/fact-enrichment", _fact_route)
    try:
        _open_authenticated_property_fact_session(
            page,
            base_url=base_url,
            propertyquarry_browser_server=propertyquarry_browser_server,
        )
        _open_family_research_packet(page, base_url=base_url)
        root = page.locator("[data-property-fact-enrichment]")
        supermarket = root.locator('[data-property-fact-key="nearest_supermarket_m"]')
        playground = root.locator('[data-property-fact-key="nearest_playground_m"]')
        score = root.locator("[data-property-score-state]")

        expect(root).to_have_attribute("aria-busy", "true")
        expect(supermarket).to_have_attribute("data-property-fact-priority", "required")
        expect(supermarket).to_have_attribute("data-property-fact-state", re.compile(r"queued|running"))
        expect(playground).to_have_attribute("data-property-fact-state", re.compile(r"queued|running"))
        expect(supermarket.locator("[data-property-fact-spinner]")).to_be_visible()
        expect(score).to_have_attribute("data-score-state", "evaluating")
        expect(score).to_have_attribute("data-ranking-eligible", "false")
        expect(score).to_contain_text("Evaluating score")
        expect(score).to_contain_text("Rank pending until required facts are checked")
        assert observed_posts == [{"retry_failed": False}]

        supermarket.evaluate("node => { node.dataset.browserIdentity = 'stable-row'; }")
        focus_target = page.locator("[data-object-feedback-save]")
        focus_target.focus()
        assert focus_target.evaluate("node => document.activeElement === node") is True
        allow_resolution["value"] = True

        expect(supermarket).to_have_attribute("data-property-fact-state", "resolved", timeout=5000)
        expect(playground).to_have_attribute("data-property-fact-state", "resolved", timeout=5000)
        expect(supermarket).to_have_attribute("data-browser-identity", "stable-row")
        expect(supermarket.locator("[data-property-fact-value]")).to_have_text("280 m")
        expect(playground.locator("[data-property-fact-value]")).to_have_text("420 m")
        expect(supermarket.locator("[data-property-fact-spinner]")).to_be_hidden()
        expect(supermarket.locator("[data-property-fact-provenance]")).to_contain_text(
            "Straight-line estimate · OpenStreetMap"
        )
        expect(root).to_have_attribute("aria-busy", "false")
        expect(score).to_have_attribute("data-score-state", "final")
        expect(score).to_have_attribute("data-ranking-eligible", "true")
        expect(score.locator("[data-property-score-value]")).to_have_text("76 / 100")
        expect(score.locator("[data-property-score-delta]")).to_have_text("+4 from 72")
        expect(score.locator("[data-property-score-reason]")).to_contain_text(
            "Nearby necessities updated from current map sources"
        )
        expect(root.locator("[data-property-fact-live]")).to_contain_text(
            "Score updated from 72 to 76, up 4 points",
            timeout=5000,
        )
        assert focus_target.evaluate("node => document.activeElement === node") is True
        assert poll_count["value"] >= 1
    finally:
        context.close()


@pytest.mark.parametrize(
    ("optional_terminal_status", "optional_terminal_field_state"),
    (
        ("succeeded", "resolved"),
        ("terminal_error", "unavailable"),
    ),
    ids=("optional-success", "optional-exhausted"),
)
def test_propertyquarry_required_to_optional_fact_transition_is_single_flight(
    monkeypatch: pytest.MonkeyPatch,
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    optional_terminal_status: str,
    optional_terminal_field_state: str,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    release_required = {"value": False}
    optional_started = {"value": False}
    optional_poll_count = {"value": 0}
    get_count = {"value": 0}
    observed_posts: list[dict[str, object]] = []

    def _mixed_fields(
        *,
        required_state: str,
        lazy_state: str,
        lazy_error: str = "",
    ) -> list[dict[str, object]]:
        templates = _property_fact_browser_fields(
            state="unknown",
            required=True,
        )
        resolved_by_key = {
            str(row["key"]): row
            for row in _property_fact_browser_fields(
                state="resolved",
                required=True,
                resolved=True,
            )
        }
        fields: list[dict[str, object]] = []
        for template in templates:
            priority = str(template["priority"])
            state = required_state if priority == "required" else lazy_state
            resolved = resolved_by_key[str(template["key"])]
            fields.append(
                _property_fact_browser_field(
                    key=str(template["key"]),
                    label=str(template["label"]),
                    state=state,
                    priority=priority,
                    value=(
                        int(resolved["value"])
                        if state == "resolved"
                        else None
                    ),
                    error_message=(
                        lazy_error
                        if priority == "lazy" and state == "unavailable"
                        else ""
                    ),
                )
            )
        return fields

    def _payload(
        *,
        status_url: str,
        status: str,
        fields: list[dict[str, object]],
        bundle_kind: str,
    ) -> dict[str, object]:
        required_active = bundle_kind == "required-geo-v1" and status in {
            "queued",
            "running",
        }
        return _property_fact_browser_payload(
            status_url=status_url,
            status=status,
            fields=fields,
            score_state="evaluating" if required_active else "provisional",
            previous_score=72,
            current_score=None if required_active else 76,
            delta=None if required_active else 4,
            ranking_eligible=not required_active,
            bundle_kind=bundle_kind,
            job_id=(
                "pfe_browser_required"
                if bundle_kind == "required-geo-v1"
                else "pfe_browser_optional"
            ),
        )

    def _initial_required(
        self,
        *,
        principal_id: str,
        run_id: str,
        candidate_ref: str,
    ) -> dict[str, object]:
        assert principal_id == "pq-greenfield-browser"
        status_url = (
            f"/app/api/signals/property/search/run/{run_id}/candidates/"
            f"{candidate_ref}/fact-enrichment"
        )
        return _payload(
            status_url=status_url,
            status="running",
            fields=_mixed_fields(required_state="running", lazy_state="unknown"),
            bundle_kind="required-geo-v1",
        )

    monkeypatch.setattr(
        ProductService,
        "get_property_candidate_fact_enrichment",
        _initial_required,
    )

    def _fact_route(route) -> None:
        request = route.request
        status_url = request.url
        if request.method == "POST":
            posted = request.post_data_json
            observed_posts.append(posted if isinstance(posted, dict) else {})
            optional_started["value"] = True
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    _payload(
                        status_url=status_url,
                        status="queued",
                        fields=_mixed_fields(
                            required_state="resolved",
                            lazy_state="queued",
                        ),
                        bundle_kind="optional-geo-v1",
                    )
                ),
            )
            return

        get_count["value"] += 1
        if not release_required["value"]:
            payload = _payload(
                status_url=status_url,
                status="running",
                fields=_mixed_fields(required_state="running", lazy_state="unknown"),
                bundle_kind="required-geo-v1",
            )
        elif not optional_started["value"]:
            payload = _payload(
                status_url=status_url,
                status="idle",
                fields=_mixed_fields(required_state="resolved", lazy_state="unknown"),
                bundle_kind="optional-geo-v1",
            )
        else:
            optional_poll_count["value"] += 1
            if optional_poll_count["value"] == 1:
                payload = _payload(
                    status_url=status_url,
                    status="running",
                    fields=_mixed_fields(
                        required_state="resolved",
                        lazy_state="running",
                    ),
                    bundle_kind="optional-geo-v1",
                )
            else:
                payload = _payload(
                    status_url=status_url,
                    status=optional_terminal_status,
                    fields=_mixed_fields(
                        required_state="resolved",
                        lazy_state=optional_terminal_field_state,
                        lazy_error="Current map sources exhausted all three checks.",
                    ),
                    bundle_kind="optional-geo-v1",
                )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/fact-enrichment", _fact_route)
    try:
        _open_authenticated_property_fact_session(
            page,
            base_url=base_url,
            propertyquarry_browser_server=propertyquarry_browser_server,
        )
        _open_family_research_packet(page, base_url=base_url)
        root = page.locator("[data-property-fact-enrichment]")
        supermarket = root.locator(
            '[data-property-fact-key="nearest_supermarket_m"]'
        )
        pharmacy = root.locator(
            '[data-property-fact-key="nearest_pharmacy_m"]'
        )

        expect(root).to_have_attribute("aria-busy", "true")
        expect(supermarket).to_have_attribute(
            "data-property-fact-state",
            "running",
        )
        expect(supermarket.locator("[data-property-fact-spinner]")).to_be_visible()
        supermarket.evaluate(
            "node => { node.dataset.browserIdentity = 'required-to-optional'; }"
        )
        assert observed_posts == []

        release_required["value"] = True
        expect(pharmacy).to_have_attribute(
            "data-property-fact-state",
            re.compile(r"queued|running|resolved|unavailable"),
            timeout=5000,
        )
        expect(pharmacy).to_have_attribute(
            "data-property-fact-state",
            optional_terminal_field_state,
            timeout=5000,
        )
        expect(supermarket).to_have_attribute(
            "data-browser-identity",
            "required-to-optional",
        )
        expect(supermarket).to_have_attribute(
            "data-property-fact-state",
            "resolved",
        )
        expect(root).to_have_attribute("aria-busy", "false")
        expect(pharmacy.locator("[data-property-fact-spinner]")).to_be_hidden()
        assert observed_posts == [{"retry_failed": False}]

        if optional_terminal_status == "succeeded":
            expect(pharmacy.locator("[data-property-fact-value]")).to_have_text(
                "510 m"
            )
        else:
            expect(pharmacy.locator("[data-property-fact-status]")).to_contain_text(
                "exhausted all three checks"
            )

        settled_get_count = get_count["value"]
        page.wait_for_timeout(700)
        assert get_count["value"] == settled_get_count
        assert observed_posts == [{"retry_failed": False}]
    finally:
        context.close()


def test_propertyquarry_lazy_distance_error_retries_without_blocking_provisional_rank(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    page.emulate_media(reduced_motion="reduce")
    allow_resolution = {"value": False}
    observed_posts: list[dict[str, object]] = []

    def _fact_route(route) -> None:
        request = route.request
        status_url = request.url
        if request.method == "POST":
            posted = request.post_data_json
            observed_posts.append(posted if isinstance(posted, dict) else {})
            retrying = bool(observed_posts[-1].get("retry_failed"))
            payload = _property_fact_browser_payload(
                status_url=status_url,
                status="queued" if retrying else "retryable_error",
                fields=_property_fact_browser_fields(
                    state="queued" if retrying else "retryable_error",
                    required=False,
                    error_message="Map sources timed out. Try again." if not retrying else "",
                ),
                score_state="provisional",
                previous_score=72,
                current_score=72,
                delta=0,
                ranking_eligible=True,
                retryable=not retrying,
            )
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        payload = _property_fact_browser_payload(
            status_url=status_url,
            status="succeeded" if allow_resolution["value"] else "running",
            fields=_property_fact_browser_fields(
                state="resolved" if allow_resolution["value"] else "running",
                required=False,
                resolved=allow_resolution["value"],
            ),
            score_state="final" if allow_resolution["value"] else "provisional",
            previous_score=72,
            current_score=74 if allow_resolution["value"] else 72,
            delta=2 if allow_resolution["value"] else 0,
            ranking_eligible=True,
        )
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/fact-enrichment", _fact_route)
    try:
        _open_authenticated_property_fact_session(
            page,
            base_url=base_url,
            propertyquarry_browser_server=propertyquarry_browser_server,
        )
        _open_family_research_packet(page, base_url=base_url)
        root = page.locator("[data-property-fact-enrichment]")
        supermarket = root.locator('[data-property-fact-key="nearest_supermarket_m"]')
        score = root.locator("[data-property-score-state]")
        retry = supermarket.locator("[data-property-fact-retry]")

        expect(supermarket).to_have_attribute("data-property-fact-state", "retryable_error")
        expect(supermarket.locator("[data-property-fact-status]")).to_contain_text("Map sources timed out")
        expect(retry).to_be_visible()
        expect(retry).to_have_attribute("aria-label", "Retry Supermarket distance")
        expect(score).to_have_attribute("data-score-state", "provisional")
        expect(score).to_have_attribute("data-ranking-eligible", "true")
        expect(score).to_contain_text("Can rank while nice-to-have facts are checked")
        expect(root.locator("[data-property-fact-live]")).to_contain_text("Retry is available")

        retry.click()
        expect(supermarket).to_have_attribute("data-property-fact-state", re.compile(r"queued|running"))
        spinner = supermarket.locator("[data-property-fact-spinner]")
        expect(spinner).to_be_visible()
        assert spinner.evaluate("node => getComputedStyle(node).animationName") == "none"
        assert observed_posts == [{"retry_failed": False}, {"retry_failed": True}]

        allow_resolution["value"] = True
        expect(supermarket).to_have_attribute("data-property-fact-state", "resolved", timeout=5000)
        expect(retry).to_be_hidden()
        expect(root).to_have_attribute("aria-busy", "false")
        expect(score).to_have_attribute("data-score-state", "final")
        expect(score.locator("[data-property-score-value]")).to_have_text("74 / 100")
        expect(score.locator("[data-property-score-delta]")).to_have_text("+2 from 72")
    finally:
        context.close()


def test_propertyquarry_authenticated_reopen_resumes_retryable_facts_after_backoff(
    monkeypatch: pytest.MonkeyPatch,
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    observed_posts: list[dict[str, object]] = []
    allow_resolution = {"value": False}

    def _persisted_retryable(self, *, principal_id: str, run_id: str, candidate_ref: str):
        assert principal_id == "pq-greenfield-browser"
        status_url = (
            f"/app/api/signals/property/search/run/{run_id}/candidates/"
            f"{candidate_ref}/fact-enrichment"
        )
        return _property_fact_browser_payload(
            status_url=status_url,
            status="retryable_error",
            fields=_property_fact_browser_fields(
                state="retryable_error",
                required=False,
                error_message="Map sources timed out. Retrying shortly.",
            ),
            score_state="provisional",
            previous_score=72,
            current_score=72,
            delta=0,
            ranking_eligible=True,
            retryable=True,
        )

    monkeypatch.setattr(
        ProductService,
        "get_property_candidate_fact_enrichment",
        _persisted_retryable,
    )

    def _fact_route(route) -> None:
        request = route.request
        status_url = request.url
        if request.method == "POST":
            posted = request.post_data_json
            observed_posts.append(posted if isinstance(posted, dict) else {})
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    _property_fact_browser_payload(
                        status_url=status_url,
                        status="queued",
                        fields=_property_fact_browser_fields(state="queued", required=False),
                        score_state="provisional",
                        previous_score=72,
                        current_score=72,
                        delta=0,
                        ranking_eligible=True,
                    )
                ),
            )
            return
        payload = _property_fact_browser_payload(
            status_url=status_url,
            status="succeeded" if allow_resolution["value"] else "running",
            fields=_property_fact_browser_fields(
                state="resolved" if allow_resolution["value"] else "running",
                required=False,
                resolved=allow_resolution["value"],
            ),
            score_state="final" if allow_resolution["value"] else "provisional",
            previous_score=72,
            current_score=74 if allow_resolution["value"] else 72,
            delta=2 if allow_resolution["value"] else 0,
            ranking_eligible=True,
        )
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/fact-enrichment", _fact_route)
    try:
        _open_authenticated_property_fact_session(
            page,
            base_url=base_url,
            propertyquarry_browser_server=propertyquarry_browser_server,
        )
        _open_family_research_packet(page, base_url=base_url)
        root = page.locator("[data-property-fact-enrichment]")
        supermarket = root.locator('[data-property-fact-key="nearest_supermarket_m"]')

        expect(supermarket).to_have_attribute("data-property-fact-state", "retryable_error")
        expect(supermarket.locator("[data-property-fact-status]")).to_contain_text(
            "Retrying shortly"
        )
        page.wait_for_timeout(400)
        assert observed_posts == []

        expect(supermarket).to_have_attribute(
            "data-property-fact-state",
            re.compile(r"queued|running"),
            timeout=3000,
        )
        expect(supermarket.locator("[data-property-fact-spinner]")).to_be_visible()
        expect(root).to_have_attribute("aria-busy", "true")
        assert observed_posts == [{"retry_failed": True}]

        allow_resolution["value"] = True
        expect(supermarket).to_have_attribute("data-property-fact-state", "resolved", timeout=5000)
        expect(supermarket.locator("[data-property-fact-spinner]")).to_be_hidden()
        expect(root).to_have_attribute("aria-busy", "false")
        assert observed_posts == [{"retry_failed": True}]
    finally:
        context.close()


def _assert_propertyquarry_billing_redirect_lands_on_account(page: Page) -> None:
    expect(page.locator("body", has_text="Billing portal unavailable")).to_have_count(0)
    assert "/app/account" in page.url
    expect(page.get_by_role("button", name="Log out")).to_be_visible()
    expect(page.get_by_role("link", name="Billing")).to_be_visible()
    body_text = page.evaluate("() => document.body.innerText")
    assert "Billing history" not in body_text
    assert "Cancellation and refunds" not in body_text
    assert "When to upgrade" not in body_text


def test_propertyquarry_research_market_formats_are_visible_in_declared_locales(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    base_url = str(propertyquarry_browser_server["base_url"])
    cases = (
        {
            "country_code": "AT",
            "language_code": "de",
            "locale": "de-AT",
            "locale_label": "Deutsch (Österreich)",
            "timezone": "Europe/Vienna",
            "currency_code": "EUR",
            "platform": "willhaben",
            "location": "1070 Vienna",
            "price": "€ 1.234.567",
            "updated": "19.07.2026, 14:00",
            "updated_label": "Recherche aktualisiert",
        },
        {
            "country_code": "DE",
            "language_code": "de",
            "locale": "de-DE",
            "locale_label": "Deutsch (Deutschland)",
            "timezone": "Europe/Berlin",
            "currency_code": "EUR",
            "platform": "immoscout_de",
            "location": "10115 Berlin",
            "price": "1.234.567 €",
            "updated": "19.07.2026, 14:00",
            "updated_label": "Recherche aktualisiert",
        },
        {
            "country_code": "CR",
            "language_code": "es",
            "locale": "es-CR",
            "locale_label": "Español (Costa Rica)",
            "timezone": "America/Costa_Rica",
            "currency_code": "CRC",
            "platform": "encuentra24_cr",
            "location": "10101 San José",
            "price": "₡1 234 567",
            "updated": "19/07/2026, 06:00 a. m.",
            "updated_label": "Investigación actualizada",
        },
    )
    cases_by_run_id = {f"run-market-{str(case['country_code']).lower()}": case for case in cases}

    def _market_run_status(self, *, principal_id: str, run_id: str):
        assert principal_id == "pq-greenfield-browser"
        case = cases_by_run_id[run_id]
        candidate_ref = f"market-{str(case['country_code']).lower()}"
        candidate = {
            "candidate_ref": candidate_ref,
            "title": f"{case['country_code']} locale home",
            "property_url": f"https://example.test/{candidate_ref}",
            "fit_score": 91,
            "match_reasons": ["Explicit market-locale fixture."],
            "property_facts": {
                "price_eur": 1234567,
                "currency_code": case["currency_code"],
                "area_m2": 78.5,
                "rooms": 3.5,
                "postal_name": case["location"],
            },
        }
        return {
            "run_id": run_id,
            "principal_id": principal_id,
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "status": "processed",
            "progress": 100,
            "message": "Property scouting run completed.",
            # Prove market presentation follows the candidate's immutable run
            # identity even when the account has since changed its saved brief.
            "brief_preferences_stale": True,
            "generated_at": "2026-07-19T11:58:00Z",
            "updated_at": "2026-07-19T12:00:00Z",
            "property_search_preferences": {
                "country_code": case["country_code"],
                "language_code": case["language_code"],
                "listing_mode": "buy",
                "search_goal": "home",
                "location_query": case["location"],
                "selected_platforms": [case["platform"]],
            },
            "summary": {
                "status": "processed",
                "ranked_candidates": [candidate],
                "sources": [
                    {
                        "source_label": f"{case['country_code']} locale fixture",
                        "top_candidates": [candidate],
                    }
                ],
            },
            "events": [{"step": "completed", "message": "Property scouting run completed.", "status": "processed"}],
        }

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _market_run_status)
    for mobile in (False, True):
        context = _new_context(
            browser,
            mobile=mobile,
            width=None if mobile else 1280,
            height=None if mobile else 900,
        )
        try:
            _issue_browser_workspace_session(
                client=client,
                context=context,
                base_url=base_url,
            )
            for index, case in enumerate(cases):
                workspace_case = cases[(index + 1) % len(cases)]
                stored = client.post(
                    "/v1/onboarding/property-search/preferences",
                    json={
                        "country_code": workspace_case["country_code"],
                        "language_code": workspace_case["language_code"],
                        "listing_mode": "buy",
                        "search_goal": "home",
                        "location_query": workspace_case["location"],
                        "selected_platforms": [workspace_case["platform"]],
                    },
                )
                assert stored.status_code == 200, stored.text
                market_key = str(case["country_code"]).lower()
                page = context.new_page()
                try:
                    response = page.goto(
                        f"{base_url}/app/research/market-{market_key}?run_id=run-market-{market_key}&lang={case['locale']}",
                        wait_until="networkidle",
                    )
                    assert response is not None and response.ok
                    assert page.locator("html").get_attribute("lang") == case["locale"]
                    market_context = page.locator("[data-property-market-context]")
                    expect(market_context).to_be_visible()
                    assert market_context.get_attribute(
                        "data-property-market-country"
                    ) == case["country_code"]
                    assert market_context.get_attribute(
                        "data-property-market-locale"
                    ) == case["locale"]
                    assert market_context.get_attribute(
                        "data-property-market-currency"
                    ) == case["currency_code"]
                    assert market_context.get_attribute(
                        "data-property-market-timezone"
                    ) == case["timezone"]
                    expect(
                        page.locator("[data-property-market-locale-label]")
                    ).to_have_text(str(case["locale_label"]))
                    expect(page.locator("[data-property-market-updated]")).to_have_text(
                        str(case["updated"])
                    )
                    expect(market_context).to_contain_text(
                        str(case["updated_label"])
                    )
                    assert (
                        page.locator(".prd-price").inner_text().replace("\u00a0", " ")
                        == case["price"]
                    )
                    hero_facts = page.locator(
                        ".prd-headline-panel .prd-facts"
                    ).inner_text().replace("\u00a0", " ")
                    assert "78,5 m²" in hero_facts
                    assert "3,5" in hero_facts
                    assert "1,234,567" not in page.locator(
                        "[data-property-research-detail]"
                    ).inner_text()
                    _assert_no_viewport_or_text_cutoff(page)
                finally:
                    page.close()
        finally:
            context.close()


def test_propertyquarry_public_home_and_sign_in_capture_polish_screenshots(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    desktop = _new_public_context(browser, mobile=False, width=1440, height=820)
    mobile = _new_public_context(browser, mobile=True)
    signed_in = _new_public_context(browser, mobile=False, width=1440, height=820)
    try:
        desktop_page = desktop.new_page()
        response = desktop_page.goto(f"{base_url}/?home=1", wait_until="networkidle")
        assert response is not None and response.ok
        expect(desktop_page.get_by_role("heading", name="Search once. See the right homes. Decide faster.")).to_be_visible()
        expect(desktop_page.get_by_text("Example shortlist")).to_be_visible()
        expect(desktop_page.get_by_text("3 examples")).to_be_visible()
        expect(desktop_page.get_by_text("Must-haves stay clear")).to_be_visible()
        expect(desktop_page.get_by_text("Preferences shape fit")).to_be_visible()
        _assert_no_horizontal_overflow(desktop_page)
        desktop_home_metrics = desktop_page.evaluate(
            """() => ({
                innerHeight: window.innerHeight,
                scrollHeight: document.scrollingElement ? document.scrollingElement.scrollHeight : 0,
                footerVisible: !!(document.querySelector('footer') && getComputedStyle(document.querySelector('footer')).display !== 'none'),
            })"""
        )
        assert desktop_home_metrics["scrollHeight"] <= desktop_home_metrics["innerHeight"] + 1, desktop_home_metrics
        assert desktop_home_metrics["footerVisible"] is False
        desktop_home = tmp_path / "propertyquarry-public-home-desktop.png"
        desktop_page.screenshot(path=str(desktop_home), full_page=True)
        assert desktop_home.exists()

        mobile_page = mobile.new_page()
        response = mobile_page.goto(f"{base_url}/?home=1", wait_until="networkidle")
        assert response is not None and response.ok
        expect(mobile_page.get_by_role("heading", name="Search once. See the right homes. Decide faster.")).to_be_visible()
        expect(mobile_page.locator(".pq-hero-copy .btn.primary", has_text="Open search")).to_be_visible()
        expect(mobile_page.locator(".topbar .nav")).to_be_hidden()
        expect(mobile_page.locator(".mobile-nav")).to_be_hidden()
        expect(mobile_page.locator(".topbar .actions .btn", has_text="Sign in")).to_be_visible()
        mobile_header_metrics = mobile_page.evaluate(
            """() => {
                const action = document.querySelector('.topbar .actions .btn');
                const brand = document.querySelector('.topbar .brand');
                const actionRect = action ? action.getBoundingClientRect() : null;
                const brandRect = brand ? brand.getBoundingClientRect() : null;
                return {
                    actionRight: actionRect ? actionRect.right : 0,
                    actionLeft: actionRect ? actionRect.left : 0,
                    brandLeft: brandRect ? brandRect.left : 0,
                    viewportWidth: window.innerWidth,
                };
            }"""
        )
        assert mobile_header_metrics["brandLeft"] >= 0
        assert mobile_header_metrics["actionLeft"] >= 0
        assert mobile_header_metrics["actionRight"] <= mobile_header_metrics["viewportWidth"] + 1
        _assert_no_horizontal_overflow(mobile_page)
        mobile_home = tmp_path / "propertyquarry-public-home-mobile.png"
        mobile_page.screenshot(path=str(mobile_home), full_page=True)
        assert mobile_home.exists()

        _issue_browser_workspace_session(client=client, context=signed_in, base_url=base_url)
        signed_page = signed_in.new_page()
        response = signed_page.goto(f"{base_url}/?home=1", wait_until="networkidle")
        assert response is not None and response.ok
        expect(signed_page.get_by_role("link", name="Open search").first).to_be_visible()
        expect(signed_page.get_by_role("link", name="Open latest results")).to_be_visible()
        expect(signed_page.locator(".topbar form[action='/app/actions/sign-out'] button", has_text="Log out")).to_be_visible()
        assert signed_page.get_by_text("Log out", exact=True).count() == 1
        assert signed_page.locator(".pq-hero-copy form[action='/app/actions/sign-out']").count() == 0
        response = signed_page.goto(f"{base_url}/sign-in", wait_until="networkidle")
        assert response is not None and response.ok
        expect(signed_page.get_by_role("link", name="Open search").first).to_be_visible()
        assert signed_page.get_by_role("link", name="Open current session").count() == 0
        assert signed_page.get_by_text("Log out", exact=True).count() == 1
        response = signed_page.goto(f"{base_url}/pricing", wait_until="networkidle")
        assert response is not None and response.ok
        expect(signed_page.get_by_role("heading", name="Free")).to_be_visible()
        assert signed_page.get_by_text("Create account", exact=True).count() == 0
        assert signed_page.get_by_text("Sign in to start", exact=True).count() == 0
        assert signed_page.get_by_text("Sign in to upgrade", exact=True).count() == 0

        response = desktop_page.goto(f"{base_url}/sign-in", wait_until="networkidle")
        assert response is not None and response.ok
        expect(desktop_page.get_by_role("heading", name="Continue your property search.")).to_be_visible()
        expect(
            desktop_page.get_by_text(
                "Use a secure email link if your address already has access. You can also continue with an available provider."
            )
        ).to_be_visible()
        assert desktop_page.get_by_role("link", name="Open current session").count() == 0
        assert (
            desktop_page.get_by_role("button", name="Email sign-in").count()
            or desktop_page.get_by_role("button", name="Send secure sign-in link").count()
            or desktop_page.get_by_text("Email sign-in is not available right now.").count()
        )
        google_link = desktop_page.get_by_role("link", name="Continue with Google")
        assert desktop_page.get_by_role("button", name="Google unavailable").count() == 0
        facebook_link = desktop_page.get_by_role("link", name="Continue with Facebook")
        assert desktop_page.get_by_role("button", name="Facebook unavailable").count() == 0
        if google_link.count():
            desktop_page.evaluate(
                """() => {
                    const google = document.querySelector('a[href="/sign-in/google"]');
                    google?.addEventListener('click', (event) => event.preventDefault(), { capture: true });
                }"""
            )
            google_link.click(no_wait_after=True)
            opening_google = desktop_page.get_by_role("link", name="Opening Google...")
            expect(opening_google).to_be_visible()
            assert opening_google.get_attribute("aria-busy") == "true"
        response = desktop_page.goto(f"{base_url}/sign-in", wait_until="networkidle")
        assert response is not None and response.ok
        _assert_no_horizontal_overflow(desktop_page)
        desktop_sign_in_metrics = desktop_page.evaluate(
            """() => {
                const panel = document.querySelector('.access-panel');
                const shell = document.querySelector('.access-shell');
                const intro = document.querySelector('.access-intro');
                const entry = document.querySelector('.access-entry');
                const heading = document.querySelector('.access-panel h1');
                const panelRect = panel ? panel.getBoundingClientRect() : null;
                const introRect = intro ? intro.getBoundingClientRect() : null;
                const entryRect = entry ? entry.getBoundingClientRect() : null;
                const headingRect = heading ? heading.getBoundingClientRect() : null;
                return {
                    panelWidth: panelRect ? panelRect.width : 0,
                    introWidth: introRect ? introRect.width : 0,
                    entryWidth: entryRect ? entryRect.width : 0,
                    shellColumns: shell ? window.getComputedStyle(shell).gridTemplateColumns.split(' ').filter(Boolean).length : 0,
                    headingHeight: headingRect ? headingRect.height : 0,
                };
            }"""
        )
        assert desktop_sign_in_metrics["panelWidth"] >= 780, desktop_sign_in_metrics
        assert desktop_sign_in_metrics["shellColumns"] == 2, desktop_sign_in_metrics
        assert desktop_sign_in_metrics["introWidth"] >= 260, desktop_sign_in_metrics
        assert desktop_sign_in_metrics["entryWidth"] >= 300, desktop_sign_in_metrics
        assert desktop_sign_in_metrics["headingHeight"] <= 270, desktop_sign_in_metrics
        sign_in_shot = tmp_path / "propertyquarry-sign-in-desktop.png"
        desktop_page.screenshot(path=str(sign_in_shot), full_page=True)
        assert sign_in_shot.exists()

        response = mobile_page.goto(f"{base_url}/sign-in", wait_until="networkidle")
        assert response is not None and response.ok
        expect(mobile_page.get_by_role("heading", name="Continue your property search.")).to_be_visible()
        assert mobile_page.get_by_role("link", name="Open current session").count() == 0
        assert (
            mobile_page.get_by_role("button", name="Email sign-in").count()
            or mobile_page.get_by_role("button", name="Send secure sign-in link").count()
            or mobile_page.get_by_text("Email sign-in is not available right now.").count()
        )
        _assert_no_horizontal_overflow(mobile_page)
        mobile_sign_in_shot = tmp_path / "propertyquarry-sign-in-mobile.png"
        mobile_page.screenshot(path=str(mobile_sign_in_shot), full_page=True)
        assert mobile_sign_in_shot.exists()

        response = mobile_page.goto(f"{base_url}/pricing", wait_until="networkidle")
        assert response is not None and response.ok
        expect(mobile_page.get_by_role("heading", name="Free")).to_be_visible()
        expect(mobile_page.get_by_role("heading", name="Plus")).to_be_visible()
        expect(mobile_page.get_by_role("heading", name="Agent")).to_be_visible()
        expect(mobile_page.get_by_text("3D tour scope").first).to_be_visible()
        expect(mobile_page.get_by_text("Selected homes", exact=True)).to_be_visible()
        expect(mobile_page.get_by_text("Shortlist, opt-in", exact=True)).to_be_visible()
        expect(mobile_page.get_by_text("Every match, opt-in", exact=True)).to_be_visible()
        _assert_no_horizontal_overflow(mobile_page)
        mobile_pricing_shot = tmp_path / "propertyquarry-pricing-mobile.png"
        mobile_page.screenshot(path=str(mobile_pricing_shot), full_page=True)
        assert mobile_pricing_shot.exists()
    finally:
        desktop.close()
        mobile.close()
        signed_in.close()


def test_propertyquarry_sign_in_desktop_uses_balanced_two_column_entry_surface(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_public_context(browser, mobile=False, width=1440, height=900)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/sign-in?signing_in=1", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.get_by_role("heading", name="Continue your property search.")).to_be_visible()
        assert page.get_by_role("link", name="Open current session").count() == 0
        assert (
            page.get_by_role("button", name="Email sign-in").count()
            or page.get_by_role("button", name="Send secure sign-in link").count()
            or page.get_by_text("Email sign-in is not available right now.").count()
        )
        if page.get_by_role("link", name="Continue with Google").count() or page.get_by_role("link", name="Continue with Facebook").count() or page.get_by_role("link", name="Continue with ID Austria").count():
            expect(page.get_by_text("Sign-in providers verify who you are, then open the same local account or create it if needed.")).to_be_visible()
        else:
            assert page.get_by_text("Sign-in providers verify who you are, then open the same local account or create it if needed.").count() == 0
            assert page.get_by_text("Trusted device").count() == 0
            assert page.get_by_text("Private hardware access stays limited to approved devices.").count() == 0
        _assert_no_horizontal_overflow(page)
        metrics = page.evaluate(
            """() => {
                const panel = document.querySelector('.access-panel');
                const shell = document.querySelector('.access-shell');
                const intro = document.querySelector('.access-intro');
                const entry = document.querySelector('.access-entry');
                const heading = document.querySelector('.access-panel h1');
                const panelRect = panel ? panel.getBoundingClientRect() : null;
                const introRect = intro ? intro.getBoundingClientRect() : null;
                const entryRect = entry ? entry.getBoundingClientRect() : null;
                const headingRect = heading ? heading.getBoundingClientRect() : null;
                return {
                    panelWidth: panelRect ? panelRect.width : 0,
                    introWidth: introRect ? introRect.width : 0,
                    entryWidth: entryRect ? entryRect.width : 0,
                    shellColumns: shell ? window.getComputedStyle(shell).gridTemplateColumns.split(' ').filter(Boolean).length : 0,
                    headingHeight: headingRect ? headingRect.height : 0,
                };
            }"""
        )
        assert metrics["panelWidth"] >= 780, metrics
        assert metrics["shellColumns"] == 2, metrics
        assert metrics["introWidth"] >= 260, metrics
        assert metrics["entryWidth"] >= 300, metrics
        assert metrics["headingHeight"] <= 270, metrics
    finally:
        context.close()


def test_propertyquarry_sign_in_missing_current_session_hides_saved_session_cta(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_public_context(browser, mobile=False, width=1440, height=900)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/sign-in?current_session=missing", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.get_by_text("You are signed out.")).to_be_visible()
        assert page.get_by_role("link", name="Open current session").count() == 0
        email_sign_in_button = page.get_by_role("button", name="Send secure sign-in link")
        if email_sign_in_button.count():
            expect(email_sign_in_button).to_be_visible()
        else:
            expect(page.get_by_text("Email sign-in is not available right now.")).to_be_visible()
    finally:
        context.close()


def test_propertyquarry_expired_session_next_action_moves_keyboard_focus_to_sign_in_options(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_public_context(browser, mobile=False, width=1440, height=900)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/sign-in?session=expired", wait_until="networkidle")
        assert response is not None and response.ok
        next_action = page.get_by_role("button", name="Show sign-in options")
        sign_in_options = page.get_by_role("region", name="Sign-in options")
        expect(next_action).to_be_visible()
        expect(sign_in_options).to_be_visible()

        next_action.focus()
        expect(next_action).to_be_focused()
        page.keyboard.press("Enter")

        expect(sign_in_options).to_be_focused()
        assert page.evaluate("() => window.location.hash") == "#sign-in-options"
    finally:
        context.close()


def test_propertyquarry_home_example_media_links_open_real_public_tour_targets(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    slug = "altbau-u6"
    public_context = _new_public_context(browser, mobile=False, width=1440, height=820)
    try:
        page = public_context.new_page()
        response = page.goto(f"{base_url}/?home=1", wait_until="networkidle")
        assert response is not None and response.ok
        tour_href = page.get_by_role("link", name="3D tour available").get_attribute("href")
        walkthrough_href = page.get_by_role("link", name="Walkthrough available").get_attribute("href")
        assert tour_href == f"/tours/{slug}/control/3dvista"
        assert walkthrough_href == f"/tours/{slug}?pane=flythrough-pane&autoplay=1"
        assert "#tour-preview" not in page.content()
        assert "#walkthrough-preview" not in page.content()
    finally:
        public_context.close()


def _assert_property_shell_visual_gates(page: Page, *, max_appbar_height: int) -> None:
    appbar = page.locator(".pq-appbar, .pqx-topbar").first
    if appbar.count() == 0:
        return
    appbar_box = appbar.bounding_box()
    assert appbar_box is not None
    assert appbar_box["height"] <= max_appbar_height
    offenders = page.evaluate(
        """
        () => {
          const boxSelectors = ['.pq-appbar', '.pqx-topbar', '.pq-hero', '.pq-card', '.pq-shortlist-card', '.object-panel', '.pqx-panel', '.pqx-card', '.pqx-table-card'];
          const textSelectors = ['h1', 'h2', 'h3', 'p', 'small', '.pq-copy', '.pq-shortlist-meta', '.pq-shortlist-copy', '.object-copy', '.pqx-note', '.pqx-small'];
          const rows = [];
          for (const box of document.querySelectorAll(boxSelectors.join(','))) {
            const boxRect = box.getBoundingClientRect();
            if (boxRect.width <= 0 || boxRect.height <= 0) continue;
            for (const child of box.querySelectorAll(textSelectors.join(','))) {
              const closedDetails = child.closest('details:not([open])');
              if (closedDetails && !child.closest('summary')) continue;
              const childRect = child.getBoundingClientRect();
              if (childRect.width <= 0 || childRect.height <= 0) continue;
              if (
                childRect.left < boxRect.left - 1 ||
                childRect.right > boxRect.right + 1 ||
                childRect.top < boxRect.top - 1 ||
                childRect.bottom > boxRect.bottom + 1
              ) {
                rows.push({
                  box: box.className,
                  text: (child.textContent || '').trim().slice(0, 100),
                  deltaRight: Math.round(childRect.right - boxRect.right),
                  deltaBottom: Math.round(childRect.bottom - boxRect.bottom),
                });
              }
            }
          }
          return rows;
        }
        """
    )
    assert offenders == []


def _assert_mobile_topnav_tap_targets(page: Page) -> None:
    metrics = page.evaluate(
        """
        () => {
          const legacyDock = document.querySelector('[data-property-mobile-dock], .pq-mobile-nav');
          const nav = document.querySelector('[data-property-console-topnav], .pqx-primary-nav, .prd-primary-nav, .pq-pack-nav');
          if (!nav) return { navVisible: false, legacyDockVisible: Boolean(legacyDock), targets: [] };
          const navRect = nav.getBoundingClientRect();
          const targetNodes = Array.from(nav.querySelectorAll('a, button, span[aria-current="page"], span.is-active'));
          return {
            navVisible: navRect.width > 0 && navRect.height > 0,
            legacyDockVisible: Boolean(legacyDock && legacyDock.getBoundingClientRect().height > 0),
            navLeft: Math.round(navRect.left),
            navRight: Math.round(navRect.right),
            viewportWidth: window.innerWidth,
            targets: targetNodes.map((node) => {
              const rect = node.getBoundingClientRect();
              const style = window.getComputedStyle(node);
              return {
                text: String(node.textContent || node.getAttribute('aria-label') || '').trim().slice(0, 60),
                visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none',
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                left: Math.round(rect.left),
                right: Math.round(rect.right),
              };
            }).filter((row) => row.visible && row.right > 0 && row.left < window.innerWidth),
          };
        }
        """
    )
    assert metrics["navVisible"] is True, metrics
    assert metrics["legacyDockVisible"] is False, metrics
    assert metrics["navLeft"] >= -1, metrics
    assert metrics["navRight"] <= metrics["viewportWidth"] + 1, metrics
    assert len(metrics["targets"]) >= 2, metrics
    undersized = [
        target
        for target in metrics["targets"]
        if target["height"] < 40 or target["width"] < 44 or max(target["left"], 0) >= min(target["right"], metrics["viewportWidth"])
    ]
    assert undersized == []


def _assert_mobile_surface_motor_accessible(page: Page) -> None:
    _assert_no_horizontal_overflow(page)
    metrics = page.evaluate(
        """
        () => {
          const topbar = document.querySelector('[data-property-research-topnav]');
          const selectors = [
            '.pqx-check',
            '.pqx-mode-button',
            '.pqx-button',
            '.pqx-link-button',
            '.console-action',
            'button',
            'summary',
            'select',
            'textarea',
            'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])'
          ];
          const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
          const offenders = [];
          for (const node of nodes) {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            const hiddenByClosedDetails = node.closest('details:not([open])') && !node.closest('summary');
            if (
              hiddenByClosedDetails ||
              rect.width <= 0 ||
              rect.height <= 0 ||
              style.visibility === 'hidden' ||
              style.display === 'none'
            ) {
              continue;
            }
            const text = String(node.textContent || node.getAttribute('aria-label') || node.getAttribute('name') || node.tagName || '').trim();
            const relaxed = node.matches('.pqx-tooltip, .pqx-account-menu-form button');
            const allowedHorizontalRail = node.closest('[data-property-mobile-step-rail], .pqx-primary-nav, .prd-primary-nav');
            if (allowedHorizontalRail && (rect.left < -1 || rect.right > window.innerWidth + 1)) {
              continue;
            }
            const minHeight = relaxed ? 40 : 44;
            const minWidth = relaxed ? 40 : 44;
            if (rect.height < minHeight || rect.width < minWidth || rect.left < -1 || rect.right > window.innerWidth + 1) {
              offenders.push({
                tag: node.tagName.toLowerCase(),
                className: String(node.className || '').slice(0, 80),
                text: text.slice(0, 80),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                left: Math.round(rect.left),
                right: Math.round(rect.right),
              });
            }
          }
          return {
            topbarVisible: Boolean(topbar && topbar.getBoundingClientRect().height > 0),
            viewportWidth: window.innerWidth,
            bodyWidth: document.documentElement.scrollWidth,
            offenderCount: offenders.length,
            offenders: offenders.slice(0, 12),
          };
        }
        """
    )
    assert metrics["topbarVisible"] is True, metrics
    assert metrics["bodyWidth"] <= metrics["viewportWidth"] + 1, metrics
    assert metrics["offenders"] == [], metrics


def _collect_clickable_targets(page: Page) -> dict[str, object]:
    return page.evaluate(
        """
        () => {
          const ids = new Set(
            Array.from(document.querySelectorAll('[id]'))
              .map((node) => String(node.getAttribute('id') || ''))
              .filter(Boolean)
          );
          const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            if (rect.width <= 0 || rect.height <= 0) {
              return false;
            }
            if (style.display === 'none' || style.visibility === 'hidden') {
              return false;
            }
            return rect.left > -1 && rect.top > -1 && rect.right <= window.innerWidth + 1 && rect.bottom <= window.innerHeight + 1;
          };
          const hasDataAttr = (node) => Array.from((node.attributes || []))
            .some((attribute) => String(attribute?.name || '').startsWith('data-'));

          const nodes = Array.from(
            document.querySelectorAll('a, button, input:not([type="hidden"]), summary, [role="button"], [role="link"]')
          );
          const targets = [];
          for (const node of nodes) {
            if (!visible(node)) continue;
            if (node.tagName.toLowerCase() === 'summary' && !node.closest('details')) {
              continue;
            }
            const tag = node.tagName.toLowerCase();
            const role = String(node.getAttribute('role') || '').toLowerCase();
            const type = String(node.getAttribute('type') || '').toLowerCase();
            const href = tag === 'a' ? String(node.getAttribute('href') || '').trim() : '';
            const absHref = href ? new URL(href, location.href).href : '';
            const text = String(
              (node.textContent || node.getAttribute('aria-label') || node.getAttribute('title') || '')
            ).trim();
            const rect = node.getBoundingClientRect();
            const disabled = (
              node.hasAttribute('disabled')
              || String(node.getAttribute('aria-disabled')).toLowerCase() === 'true'
            );
            const hasOnclick = Boolean(String(node.getAttribute('onclick') || '').trim());
            const popovertarget = String(node.getAttribute('popovertarget') || '').trim();
            const ariaControls = String(node.getAttribute('aria-controls') || '').trim();
            const action = String(node.getAttribute('action') || '').trim();
            const formaction = String(node.getAttribute('formaction') || '').trim();
            const formMethod = String(node.getAttribute('formmethod') || '').trim().toLowerCase() || null;
            const actionMethod = formMethod || null;
            const hasDataHandler = hasDataAttr(node);
            const ariaPressed = String(node.getAttribute('aria-pressed') || '').trim();
            const target = String(node.getAttribute('target') || '').trim();
            const rel = String(node.getAttribute('rel') || '').trim();
            const detailsSummary = tag === 'summary' && Boolean(node.closest('details'));
            targets.push({
              tag,
              role,
              type,
              text: text.slice(0, 120),
              href,
              absHref,
              target,
              rel,
              disabled,
              hasOnclick,
              popovertarget,
              ariaControls,
              ariaPressed,
              hasDataHandler,
              hasActionHandler: hasDataHandler || hasOnclick || Boolean(popovertarget) || Boolean(ariaControls),
              hasFormAction: Boolean(formaction),
              hasAction: Boolean(action),
              formaction,
              formMethod: actionMethod,
              hasActionTarget: Boolean(formaction || action),
              rectLeft: Math.round(rect.left),
              rectTop: Math.round(rect.top),
              rectWidth: Math.round(rect.width),
              rectHeight: Math.round(rect.height),
              isSubmit: tag === 'button' && (type === 'submit' || !type),
              isDetailsSummary: detailsSummary,
            });
          }
          return {
            viewportWidth: window.innerWidth,
            viewportHeight: window.innerHeight,
            targetCount: targets.length,
            visibleIdSet: Array.from(ids),
            targets,
          };
        }
        """
    )


def _assert_surface_click_targets_are_hydrated(
    page: Page,
    base_url: str,
    route: str,
    *,
    max_budget_ms: int,
) -> None:
    start = time.perf_counter()
    route_response = page.goto(f"{base_url}{route}", wait_until="domcontentloaded")
    duration_ms = int((time.perf_counter() - start) * 1000)
    assert route_response is not None and route_response.ok, route
    assert duration_ms <= max_budget_ms, {"route": route, "duration_ms": duration_ms, "budget_ms": max_budget_ms}

    payload = _collect_clickable_targets(page)
    assert payload["targetCount"] >= 1, route
    known_ids = set(map(str, payload["visibleIdSet"]))
    metrics = page.evaluate(
        """
        () => ({
          bodyWidth: Math.max(document.documentElement.scrollWidth || 0, document.body ? document.body.scrollWidth : 0),
          viewportWidth: window.innerWidth,
        })
        """
    )
    assert int(metrics["bodyWidth"]) <= int(metrics["viewportWidth"]) + 1, metrics
    base_components = urllib.parse.urlsplit(base_url)
    assert base_components.scheme and base_components.netloc, base_url
    base_origin = f"{base_components.scheme}://{base_components.netloc}"
    local_allowed_statuses = {200, 201, 202, 204, 301, 302, 303, 307, 308, 401, 403, 405, 422}
    for row in payload["targets"]:
        if not isinstance(row, dict):
            continue
        tag = str(row.get("tag") or "").lower()
        role = str(row.get("role") or "").lower()
        disabled = bool(row.get("disabled"))
        text = str(row.get("text") or "").strip()
        href = str(row.get("href") or "").strip()
        abs_href = str(row.get("absHref") or "").strip()
        has_data = bool(row.get("hasDataHandler"))
        has_handler = bool(row.get("hasActionHandler"))
        has_action_target = bool(row.get("hasActionTarget"))
        popover = str(row.get("popovertarget") or "").strip()
        aria_controls = str(row.get("ariaControls") or "").strip()
        is_submit = bool(row.get("isSubmit"))
        input_type = str(row.get("type") or "").lower()
        is_summary = bool(row.get("isDetailsSummary"))

        if disabled:
            continue
        if tag == "a" or role == "link":
            assert href, f"missing href on clickable anchor on {route}: {text}"
            lower_href = href.lower()
            assert not lower_href.startswith("javascript:"), f"javascript href on {route}: {text}"
            assert not lower_href.startswith("mailto:") or "@" in href, f"bad mailto href on {route}: {text}"
            if href.startswith("#"):
                assert href != "#", f"hash-only href on {route}: {text}"
                if href == "#pqx-filtered-breakdown":
                    continue
                if has_data or has_handler:
                    continue
                assert href[1:] in known_ids, f"missing hash target on {route}: {text} -> {href}"
                continue
            if abs_href.startswith("tel:") or abs_href.startswith("sms:") or abs_href.startswith("mailto:"):
                continue
            if abs_href.startswith(base_origin):
                parsed = urllib.parse.urlsplit(abs_href)
                probe_skip_non_app_paths = {
                    "",
                    "/",
                    "/how-it-works",
                    "/how-it-works/score",
                    "/product",
                    "/docs",
                    "/integrations",
                    "/privacy",
                    "/terms",
                    "/support",
                    "/imprint",
                    "/cookies",
                    "/subprocessors",
                    "/refunds",
                    "/disclaimers",
                    "/data-deletion",
                    "/register",
                    "/sign-in",
                    "/pricing",
                    "/sign-out",
                }
                probe_skip_path_prefixes = (
                    "/static/",
                    "/assets/",
                    "/images/",
                    "/media/",
                    "/files/",
                    "/tours/",
                    "/app/",
                    "/api/",
                )
                probe_should_skip = parsed.path in {"", "/"} and (
                    not parsed.query or "home=" in parsed.query or parsed.query.startswith("home=")
                )
                if (
                    probe_should_skip
                    or parsed.path in probe_skip_non_app_paths
                    or parsed.path.startswith(probe_skip_path_prefixes)
                ):
                    continue
                probe_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
                try:
                    probe = page.request.get(probe_url, fail_on_status_code=False, timeout=5000)
                except Exception:
                    assert False, {
                        "route": route,
                        "href": abs_href,
                        "error": "link request timeout",
                    }
                assert probe.status in local_allowed_statuses, {
                    "route": route,
                    "href": abs_href,
                    "status": probe.status,
                }
            continue
        if tag == "summary":
            if is_summary:
                continue
            assert has_handler or popover or aria_controls, f"unbound summary on {route}: {text}"
            continue
        if tag == "input":
            if input_type in {"checkbox", "radio", "file", ""}:
                continue
            if input_type in {"button", "submit", "image"}:
                assert has_data or bool(row.get("hasOnclick") or has_handler) or has_action_target, f"unbound input[{input_type}] on {route}: {text}"
            continue
        if tag == "button" and not is_submit:
            assert has_data or has_handler or has_action_target or row.get("formMethod"), f"unbound button on {route}: {text}"


def _goto_with_browser_budget(page: Page, url: str, *, wait_until: str, budget_ms: int) -> tuple[object, int]:
    started = time.perf_counter()
    response = page.goto(url, wait_until=wait_until)
    duration_ms = int((time.perf_counter() - started) * 1000)
    assert response is not None and response.ok
    assert duration_ms <= budget_ms, {"url": url, "duration_ms": duration_ms, "budget_ms": budget_ms}
    return response, duration_ms


def _assert_visible_component_contrast(page: Page, selectors: list[str], *, minimum_ratio: float) -> None:
    offenders = page.evaluate(
        """
        ({ selectors, minimumRatio }) => {
          const parseColor = (value) => {
            const match = String(value || '').match(/rgba?\\(([^)]+)\\)/);
            if (!match) return null;
            const parts = match[1].split(',').map((part) => Number.parseFloat(part.trim()));
            if (parts.length < 3) return null;
            return { r: parts[0], g: parts[1], b: parts[2], a: parts.length >= 4 ? parts[3] : 1 };
          };
          const channel = (value) => {
            const scaled = value / 255;
            return scaled <= 0.03928 ? scaled / 12.92 : Math.pow((scaled + 0.055) / 1.055, 2.4);
          };
          const luminance = (color) => 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b);
          const contrast = (first, second) => {
            const a = luminance(first);
            const b = luminance(second);
            const lighter = Math.max(a, b);
            const darker = Math.min(a, b);
            return (lighter + 0.05) / (darker + 0.05);
          };
          const effectiveBackground = (node) => {
            let current = node;
            while (current && current.nodeType === Node.ELEMENT_NODE) {
              const color = parseColor(window.getComputedStyle(current).backgroundColor);
              if (color && color.a > 0.01) return color;
              current = current.parentElement;
            }
            return parseColor(window.getComputedStyle(document.body).backgroundColor);
          };
          const rows = [];
          for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
              const rect = node.getBoundingClientRect();
              if (rect.width <= 0 || rect.height <= 0) continue;
              const style = window.getComputedStyle(node);
              const text = parseColor(style.color);
              const background = effectiveBackground(node);
              if (!text || !background) continue;
              const ratio = contrast(text, background);
              if (ratio < minimumRatio) {
                rows.push({
                  selector,
                  text: (node.textContent || '').trim().slice(0, 80),
                  ratio: Math.round(ratio * 100) / 100,
                  color: style.color,
                  background: window.getComputedStyle(node).backgroundColor,
                });
              }
            }
          }
          return rows;
        }
        """,
        {"selectors": selectors, "minimumRatio": minimum_ratio},
    )
    assert offenders == []


def _assert_dark_mode_surfaces_stay_readable(page: Page, selectors: list[str]) -> None:
    offenders = page.evaluate(
        """
        ({ selectors }) => {
          const parseColor = (value) => {
            const match = String(value || '').match(/rgba?\\(([^)]+)\\)/);
            if (!match) return null;
            const parts = match[1].split(',').map((part) => Number.parseFloat(part.trim()));
            if (parts.length < 3) return null;
            return { r: parts[0], g: parts[1], b: parts[2], a: parts.length >= 4 ? parts[3] : 1 };
          };
          const light = (color) => color && color.a >= 0.65 && color.r >= 242 && color.g >= 242 && color.b >= 238;
          const lightBackgroundImage = (value) => {
            const colors = String(value || '').match(/rgba?\\([^)]+\\)/g) || [];
            return colors.some((entry) => light(parseColor(entry)));
          };
          const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width >= 8 && rect.height >= 8 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const effectiveBackground = (node) => {
            let current = node;
            while (current && current.nodeType === Node.ELEMENT_NODE) {
              const color = parseColor(window.getComputedStyle(current).backgroundColor);
              if (color && color.a > 0.08) return color;
              current = current.parentElement;
            }
            return parseColor(window.getComputedStyle(document.body).backgroundColor);
          };
          const rows = [];
          const nodesBySelector = new Map();
          for (const selector of ['body *', ...selectors]) {
            for (const node of document.querySelectorAll(selector)) {
              const matchedSelectors = nodesBySelector.get(node) || [];
              matchedSelectors.push(selector);
              nodesBySelector.set(node, matchedSelectors);
            }
          }
          for (const [node, matchedSelectors] of nodesBySelector.entries()) {
              if (!visible(node)) continue;
              if (node.closest('svg, img, picture, video, canvas, [data-pqx-map-thumbnail]')) continue;
              const style = window.getComputedStyle(node);
              const background = effectiveBackground(node);
              const color = parseColor(style.color);
              const text = (node.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 96);
              const selector = matchedSelectors.find((entry) => entry !== 'body *') || node.tagName.toLowerCase();
              if (light(background) || lightBackgroundImage(style.backgroundImage)) {
                rows.push({
                  selector,
                  text,
                  background: style.backgroundColor,
                  backgroundImage: style.backgroundImage,
                  effectiveBackground: `rgba(${Math.round(background.r)}, ${Math.round(background.g)}, ${Math.round(background.b)}, ${background.a})`,
                  color: style.color,
                });
              }
              if (text && light(background) && light(color)) {
                rows.push({
                  selector: `${selector} (light text on light surface)`,
                  text,
                  background: style.backgroundColor,
                  effectiveBackground: `rgba(${Math.round(background.r)}, ${Math.round(background.g)}, ${Math.round(background.b)}, ${background.a})`,
                  color: style.color,
                });
              }
          }
          return rows;
        }
        """,
        {"selectors": selectors},
    )
    assert offenders == []


def _assert_research_packet_360_first(page: Page, *, min_stage_height: int, max_stage_height: int | None = None) -> None:
    media = page.locator("[data-object-media-stage]").first
    home_panel = page.locator("[data-prd-home-panel]").first
    assert media.is_visible()
    assert home_panel.is_visible()
    media_box = media.bounding_box()
    home_panel_box = home_panel.bounding_box()
    assert media_box is not None
    assert home_panel_box is not None
    vertical_gap = media_box["y"] - home_panel_box["y"]
    if vertical_gap < -4:
        pass
    else:
        assert abs(vertical_gap) <= 4
        assert media_box["x"] < home_panel_box["x"]
    assert media_box["height"] >= min_stage_height
    if max_stage_height is not None:
        assert media_box["height"] <= max_stage_height


def test_propertyquarry_greenfield_workspace_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda err: page_errors.append(str(err)))
    try:
        response = page.goto(
            f"{base_url}/app/shortlist?run_id=run-42&full=1",
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        assert response is not None and response.ok
        content = page.content()
        assert 'data-property-spa-shell' in content
        assert 'data-property-mobile-dock' not in content
        assert 'data-property-decision-workbench' in content
        assert 'data-pq-greenfield-shell' in content
        assert 'data-pq-theater' in content
        assert 'data-workbench-results-table' in content
        expect(page.locator("[data-workbench-row]:visible").first).to_be_visible(timeout=5000)
        expect(page.locator("[data-workbench-row]").first).to_be_visible()
        expect(page.locator("[data-workbench-row][data-candidate-packet-url]").first).to_be_visible()
        expect(page.locator(".pqx-results-summary-link").first).to_be_visible()
        expect(page.locator(".pqx-results-summary-link").first).to_contain_text(
            re.compile(r"saved homes|matching homes|saved opportunities|matching opportunities", re.I)
        )
        assert "Altbau near U6" in content
        assert "Family flat near Tiergarten" in content
        expect(page.locator("[data-workbench-row]", has_text="Altbau near U6").first.get_by_role("link", name="3D tour")).to_be_visible()
        assert page.locator("body", has_text="Open property").is_visible()
        _assert_property_shell_visual_gates(page, max_appbar_height=92)

        before_url = page.url
        family_row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        selected_candidate_ref = str(family_row.get_attribute("data-candidate-ref") or "").strip()
        assert selected_candidate_ref
        family_row.click()
        selected_panel = page.get_by_role("region", name="Selected property")
        expect(selected_panel.locator("[data-pw-title]")).to_contain_text("Family flat near Tiergarten")
        before_parts = urllib.parse.urlsplit(before_url)
        selected_parts = urllib.parse.urlsplit(page.url)
        assert (selected_parts.scheme, selected_parts.netloc, selected_parts.path) == (
            before_parts.scheme,
            before_parts.netloc,
            before_parts.path,
        )
        assert urllib.parse.parse_qs(selected_parts.query).get("candidate") == [selected_candidate_ref]
        expect(selected_panel.get_by_role("link", name="Open property").first).to_have_attribute(
            "href",
            re.compile(r"/app/research/"),
        )
    finally:
        context.close()


def test_propertyquarry_completed_run_opens_fast_ranked_shell_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    try:
        _, duration_ms = _goto_with_browser_budget(
            page,
            f"{base_url}/app/shortlist/run/run-42",
            wait_until="networkidle",
            budget_ms=3600,
        )
        assert "/app/shortlist/run/run-42" in page.url
        expect(page.locator("[data-pq-fast-ranked-run]")).to_be_visible()
        expect(page.locator("[data-property-decision-workbench]")).to_have_count(0)
        fast_rows = page.locator("[data-pq-fast-list] .pq-fast-row:not(.pq-fast-skeleton)")
        fast_rows.first.wait_for(timeout=5000)
        assert fast_rows.count() >= 2
        expect(page.get_by_role("link", name="Altbau near U6").first).to_have_attribute(
            "href",
            re.compile(r"/app/research/[^?]+\?run_id=run-42"),
        )
        expect(page.get_by_role("link", name="Open property").first).to_be_visible()
        expect(page.get_by_role("link", name="Full view")).to_have_count(0)
        resource_urls = page.evaluate(
            "() => performance.getEntriesByType('resource').map((entry) => entry.name)"
        )
        assert all("/app/assets/property-workbench." not in str(url) for url in resource_urls)
        assert duration_ms > 0
    finally:
        context.close()


def test_propertyquarry_fast_ranked_shell_uses_generated_diorama_from_ready_layout_on_first_paint(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    generated_reconstruction_url = "https://propertyquarry.com/tours/fast-generated-layout"
    diorama_url = "https://cdn.example.test/fast-generated-diorama.png"
    photo_url = "https://cdn.example.test/fast-generated-photo.jpg"
    original_get_run_status = ProductService.get_property_search_run_status
    original_list_runs = ProductService.list_property_search_runs

    def _fake_run_status(self, *, principal_id: str, run_id: str, lightweight: bool = False, account_email: str = ""):
        if principal_id == "pq-greenfield-browser" and run_id == "run-fast-generated-layout":
            return {
                "run_id": run_id,
                "principal_id": principal_id,
                "status_url": f"/app/api/signals/property/search/run/{run_id}",
                "status": "processed",
                "progress": 100,
                "message": "Generated layout shortlist ready.",
                "summary": {
                    "status": "processed",
                    "ranked_candidates": [
                        {
                            "candidate_ref": "fast-generated-layout-flat",
                            "title": "Generated layout shortlist flat",
                            "property_url": "https://www.immobilienscout24.de/expose/generated-layout-shortlist-flat",
                            "source_label": "ImmoScout24",
                            "tour_url": generated_reconstruction_url,
                            "preview_image_url": photo_url,
                            "property_facts": {
                                "price_display": "EUR 2,050",
                                "postal_name": "1020 Vienna",
                                "preview_image_url": photo_url,
                                "image_url": photo_url,
                                "media_urls_json": [photo_url],
                            },
                        }
                    ],
                },
            }
        try:
            return original_get_run_status(
                self,
                principal_id=principal_id,
                run_id=run_id,
                lightweight=lightweight,
                account_email=account_email,
            )
        except TypeError:
            try:
                return original_get_run_status(
                    self,
                    principal_id=principal_id,
                    run_id=run_id,
                    lightweight=lightweight,
                )
            except TypeError:
                return original_get_run_status(self, principal_id=principal_id, run_id=run_id)

    def _fake_list_runs(self, *, principal_id: str, limit: int = 24, hydrate: bool = False):
        if principal_id == "pq-greenfield-browser":
            return [
                {
                    "run_id": "run-fast-generated-layout",
                    "status": "processed",
                    "summary": {
                        "status": "processed",
                        "ranked_candidates": [
                            {
                                "candidate_ref": "fast-generated-layout-flat",
                                "title": "Generated layout shortlist flat",
                                "property_url": "https://www.immobilienscout24.de/expose/generated-layout-shortlist-flat",
                            }
                        ],
                    },
                }
            ]
        return original_list_runs(self, principal_id=principal_id, limit=limit, hydrate=hydrate)

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_run_status)
    monkeypatch.setattr(ProductService, "list_property_search_runs", _fake_list_runs)
    monkeypatch.setattr(
        landing_routes,
        "_property_visual_ready_tour_url",
        lambda *, tour_url="", open_tour_url="", principal_id="": (
            generated_reconstruction_url
            if str(tour_url or open_tour_url).strip() == generated_reconstruction_url
            else ""
        ),
    )
    monkeypatch.setattr(
        landing_routes,
        "_hosted_property_tour_telegram_preview_image_url_for_style",
        lambda tour_url, *, diorama_style_hint="": diorama_url if tour_url == generated_reconstruction_url else "",
    )

    context = _new_context(browser, mobile=False, width=1440, height=900)
    _stub_remote_png(context, url_pattern="**/fast-generated-diorama.png", label="Fast generated diorama")
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/shortlist/run/run-fast-generated-layout", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.locator("[data-pq-fast-ranked-run]")).to_be_visible()
        first_row = page.locator("[data-pq-fast-list] .pq-fast-row:not(.pq-fast-skeleton)").first
        first_row.wait_for(timeout=5000)
        expect(first_row).to_contain_text("Generated layout shortlist flat")
        expect(first_row.locator(".pq-fast-thumb")).to_have_attribute("data-visual-kind", "diorama")
        expect(first_row.locator(".pq-fast-thumb img")).to_have_attribute("src", diorama_url)
    finally:
        context.close()


def test_propertyquarry_public_shared_fast_ranked_run_shows_sample_homes_with_diorama_and_open_property(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    shared_run_id = "0a89ead9e0b048288cca22d1aac54fa7"
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/shortlist/run/{shared_run_id}", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.locator("[data-pq-fast-ranked-run]")).to_be_visible()
        expect(page.get_by_role("heading", name="Sample homes")).to_be_visible()
        fast_rows = page.locator("[data-pq-fast-list] .pq-fast-row:not(.pq-fast-skeleton)")
        fast_rows.first.wait_for(timeout=5000)
        first_row = fast_rows.first
        expect(first_row).to_contain_text("Danube Flats demo")
        first_diorama = first_row.locator(".pq-fast-thumb img")
        expect(first_diorama).to_have_attribute(
            "src",
            re.compile(r"(?:telegram-preview|diorama-preview|scene-01)\.(?:png|jpg)|example-shortlist-diorama-1\.webp"),
        )
        expect(first_diorama).to_have_attribute("alt", "Illustrative diorama of Danube Flats demo")
        assert first_diorama.evaluate("(node) => node.complete && node.naturalWidth > 0")
        expect(first_row.locator(".pq-fast-diorama-badge")).to_have_text("Illustrative")
        expect(first_row.locator(".pq-fast-actions").get_by_role("link", name="Open property")).to_have_attribute(
            "href",
            re.compile(r"/app/example/shortlist\?candidate=danube-flats-demo"),
        )
        expect(page.locator("body")).not_to_contain_text(
            re.compile(r"Use email or one of the sign-in options below", re.I),
            timeout=1000,
        )
    finally:
        context.close()


def test_propertyquarry_authenticated_shared_fast_ranked_run_opens_latest_results(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    shared_run_id = "0a89ead9e0b048288cca22d1aac54fa7"
    original_get_run_status = ProductService.get_property_search_run_status
    original_list_runs = ProductService.list_property_search_runs

    def _fake_shared_run_status(self, *, principal_id: str, run_id: str):
        if principal_id == "pq-greenfield-browser" and run_id == shared_run_id:
            return {
                "run_id": "sample-homes",
                "principal_id": principal_id,
                "status_url": f"/app/api/signals/property/search/run/{run_id}",
                "status": "completed",
                "progress": 100,
                "message": "Sample shortlist ready.",
                "summary": {
                    "status": "completed",
                    "ranked_candidates": [
                        {
                            "candidate_ref": "sample-home-1",
                            "title": "Danube Flats demo",
                            "packet_url": "/app/example/shortlist?candidate=danube-flats-demo#danube-flats-demo",
                            "diorama_preview_url": "/static/property/home/example-shortlist-home-1.png",
                        }
                    ],
                },
            }
        return original_get_run_status(self, principal_id=principal_id, run_id=run_id)

    def _fake_list_runs(self, *, principal_id: str, limit: int = 24, hydrate: bool = False):
        if principal_id == "pq-greenfield-browser":
            assert limit == 24
            return [
                {
                    "run_id": "run-42",
                    "status": "processed",
                    "summary": {
                        "status": "processed",
                        "ranked_candidates": [
                            {
                                "candidate_ref": "review-1",
                                "title": "Altbau near U6",
                                "packet_url": "/app/handoffs/human_task:review-1",
                                "property_url": "https://www.immobilienscout24.de/expose/altbau-u6",
                            }
                        ],
                    },
                }
            ]
        return original_list_runs(self, principal_id=principal_id, limit=limit, hydrate=hydrate)

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_shared_run_status)
    monkeypatch.setattr(ProductService, "list_property_search_runs", _fake_list_runs)

    context = _new_public_context(browser, mobile=False, width=1440, height=900)
    _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/shortlist/run/{shared_run_id}", wait_until="networkidle")
        assert response is not None and response.ok
        assert page.url.endswith("/app/shortlist/run/run-42")
        expect(page.locator("[data-pq-fast-ranked-run]")).to_be_visible()
        expect(page.get_by_role("heading", name="Matching homes")).to_be_visible()
        expect(page.locator("[data-pq-fast-list] .pq-fast-row:not(.pq-fast-skeleton)").first).to_contain_text("Altbau near U6")
        expect(page.get_by_role("link", name="Open property").first).to_be_visible()
        expect(page.get_by_role("link", name="Sample shortlist")).to_have_count(0)
        expect(page.locator("body")).not_to_contain_text("Sample homes", timeout=1000)
        expect(page.locator("a[href*=\"/app/example/shortlist\"]")).to_have_count(0)
    finally:
        context.close()


def test_propertyquarry_search_loads_versioned_workbench_assets_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible", timeout=5000)
        resource_urls = page.evaluate(
            "() => performance.getEntriesByType('resource').map((entry) => entry.name)"
        )
        assert any("/app/assets/property-workbench.css?v=" in str(url) for url in resource_urls)
        assert all("/app/assets/property-workbench.css?surface=static" not in str(url) for url in resource_urls)
        loader = page.locator('script[src*="/app/assets/property-search-loader.js?v="]').first
        loader_src = loader.get_attribute("src")
        assert loader_src and "/app/assets/property-search-loader.js?v=" in loader_src
        assert loader.get_attribute("async") is not None
        assert "/app/assets/property-workbench.js?v=" in str(loader.get_attribute("data-workbench-src") or "")
        assert all("/app/assets/property-workbench.js" not in str(url) for url in resource_urls)
    finally:
        context.close()


def test_propertyquarry_result_thumbnail_opens_lazy_evidence_atlas(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page: Page = context.new_page()
    indexed_requests: list[str] = []
    page.on(
        "request",
        lambda request: indexed_requests.append(request.url)
        if any(marker in request.url.lower() for marker in ("newspaper", "article-index", "provider-coverage", "evidence-index"))
        else None,
    )
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        row = page.locator("[data-workbench-row]", has_text="Altbau near U6").first
        row.wait_for()
        before_url = page.url
        row.locator("[data-pqx-atlas-open]").click()
        atlas = page.locator("[data-pqx-evidence-atlas]")
        expect(atlas).to_be_visible()
        expect(atlas.locator("[data-pqx-evidence-atlas-title]")).to_contain_text("Altbau near U6")
        expect(atlas.locator("[data-pqx-evidence-atlas-footer]")).to_contain_text("Showing available layers.")
        expect(atlas.locator("[data-pqx-evidence-map-detail]")).to_contain_text("Switch layers.")
        expect(atlas.get_by_text("Teable", exact=False)).to_have_count(0)
        expect(atlas.get_by_text("Postgres", exact=False)).to_have_count(0)
        expect(atlas.get_by_role("button", name="Media")).to_be_visible()
        expect(atlas.get_by_role("button", name="Fiber")).to_be_visible()
        expect(atlas.get_by_role("button", name="Visuals")).to_be_visible()
        assert page.url == before_url
        atlas.get_by_role("button", name="Media").click()
        media_card = atlas.locator("[data-pqx-evidence-card='media']")
        expect(media_card).to_be_visible()
        expect(media_card.get_by_text("1 local article mention.")).to_be_visible()
        expect(media_card.get_by_role("link", name="Open page")).to_be_visible()
        atlas.get_by_role("button", name="Fiber").click()
        fiber_card = atlas.locator("[data-pqx-evidence-card='fiber']")
        expect(fiber_card).to_be_visible()
        expect(fiber_card.get_by_text("Fiber is reported nearby. Final availability still depends on the exact address.")).to_be_visible()
        expect(fiber_card.get_by_role("link", name="Open page")).to_be_visible()
        metrics = page.evaluate(
            """() => {
              const atlas = document.querySelector('[data-pqx-evidence-atlas]');
              const card = document.querySelector('[data-pqx-evidence-atlas-card]');
              const rail = document.querySelector('[data-pqx-evidence-layer-rail]');
              const rect = card ? card.getBoundingClientRect() : null;
              const railStyle = rail ? window.getComputedStyle(rail) : null;
              return {
                bodyWidth: document.documentElement.scrollWidth,
                viewportWidth: window.innerWidth,
                atlasOpen: Boolean(atlas && atlas.open),
                cardRight: rect ? rect.right : 0,
                cardBottom: rect ? rect.bottom : 0,
                viewportHeight: window.innerHeight,
                railOverflowX: railStyle ? railStyle.overflowX : '',
              };
            }"""
        )
        assert metrics["atlasOpen"] is True
        assert metrics["bodyWidth"] <= metrics["viewportWidth"] + 1
        assert metrics["cardRight"] <= metrics["viewportWidth"] + 1
        assert metrics["cardBottom"] <= metrics["viewportHeight"] + 1
        assert metrics["railOverflowX"] in {"auto", "scroll"}
        assert indexed_requests == []
    finally:
        context.close()


def test_propertyquarry_dark_mode_keeps_shortlist_cards_readable(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=1100)
    context.add_init_script("window.localStorage.setItem('propertyquarry.theme', 'dark');")
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.locator("html")).to_have_attribute("data-pq-theme", "dark")
        page.locator("[data-workbench-row]:visible").first.wait_for(timeout=5000)
        _assert_no_horizontal_overflow(page)
        _assert_visible_component_contrast(
            page,
            [
                ".pqx-card",
                ".pqx-result",
                ".pqx-result-fact",
                ".pqx-result-open",
                ".pqx-progress-button",
                ".pqx-pill",
                ".pqx-event-card",
                ".pqx-source-card",
                ".pqx-route-preview-card",
                ".pqx-empty",
            ],
            minimum_ratio=3.0,
        )
        assert page.locator("[data-pqx-filtered-open]:visible").count() == 0
        screenshot_path = tmp_path / "propertyquarry-shortlist-dark-mode.png"
        page.screenshot(path=str(screenshot_path), full_page=False, animations="disabled", caret="hide")
        assert screenshot_path.exists() and screenshot_path.stat().st_size > 20_000
    finally:
        context.close()


def test_propertyquarry_dark_mode_covers_public_and_management_surfaces(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    public_selectors = [
        ".topbar",
        ".mobile-nav a",
        ".panel",
        ".summary",
        ".preview-top",
        ".preview-main > section",
        ".nav-card",
        ".nav-card a",
        ".mini-card",
        ".pill",
        ".tier-card",
        ".tier-card *",
        ".access-panel",
        ".access-panel *",
        ".access-card",
        ".access-card *",
        ".access-feedback",
        ".auth-provider-card",
        ".auth-provider-card *",
        ".auth-provider-icon",
        ".auth-provider-card .btn",
        ".btn",
    ]
    app_selectors = [
        ".pqx-topbar",
        ".pqx-shell *",
        ".pqx-primary-nav a",
        ".pqx-run-chip",
        ".pqx-button:not(.primary)",
        ".pqx-link-button:not(.primary)",
        ".pqx-card",
        ".pqx-workflow-step",
        ".pqx-disclosure-summary",
        ".pqx-field input:not([type='checkbox'])",
        ".pqx-field select",
        ".pqx-what-matters-panel",
        ".pqx-pref-row",
        ".pqx-account-card",
        ".pqx-account-channel-option",
        ".pqx-account-channel-detail",
        ".pqx-account-channel-detail input",
        ".pqx-billing-card",
        ".pqx-automation-card",
        ".pqx-automation-thumbnail",
        ".pqx-automation-summary-card",
        ".pqx-result",
        ".pqx-result-fact",
        ".pqx-progress-button",
        ".pqx-reliability-strip",
        ".pqx-source-progress",
        ".pqx-empty",
        ".pq-pack-shell",
        ".pq-pack-topbar",
        ".pq-pack-button",
        ".pq-pack-panel",
        ".pq-pack-stat",
        ".pq-pack-summary-card",
        ".pq-pack-card",
        ".pq-pack-decision-card",
        ".pq-pack-input",
        ".pq-pack-check",
        ".pq-pack-pill",
        ".pq-pack-feedback",
        ".pq-pack-review-actions textarea",
        ".pq-pack-empty",
    ]

    public_context = _new_public_context(browser, mobile=False, width=1366, height=960)
    public_context.add_init_script("window.localStorage.setItem('propertyquarry.theme', 'dark');")
    try:
        public_page = public_context.new_page()
        for route, screenshot_name in (
            ("/sign-in", "propertyquarry-sign-in-dark-surfaces.png"),
            ("/sign-in?signing_in=1", "propertyquarry-sign-in-loading-dark-surfaces.png"),
            ("/register", "propertyquarry-register-dark-surfaces.png"),
            ("/", "propertyquarry-root-dark-surfaces.png"),
            ("/?home=1", "propertyquarry-home-dark-surfaces.png"),
            ("/pricing", "propertyquarry-pricing-dark-surfaces.png"),
            ("/product", "propertyquarry-product-dark-surfaces.png"),
            ("/docs", "propertyquarry-docs-dark-surfaces.png"),
            ("/integrations", "propertyquarry-integrations-dark-surfaces.png"),
            ("/privacy", "propertyquarry-privacy-dark-surfaces.png"),
            ("/terms", "propertyquarry-terms-dark-surfaces.png"),
            ("/support", "propertyquarry-support-dark-surfaces.png"),
            ("/imprint", "propertyquarry-imprint-dark-surfaces.png"),
            ("/cookies", "propertyquarry-cookies-dark-surfaces.png"),
            ("/subprocessors", "propertyquarry-subprocessors-dark-surfaces.png"),
            ("/refunds", "propertyquarry-refunds-dark-surfaces.png"),
            ("/disclaimers", "propertyquarry-disclaimers-dark-surfaces.png"),
        ):
            response = public_page.goto(f"{base_url}{route}", wait_until="networkidle")
            assert response is not None and response.ok
            expect(public_page.locator("html")).to_have_attribute("data-pq-theme", "dark")
            _assert_dark_mode_surfaces_stay_readable(public_page, public_selectors)
            public_shot = tmp_path / screenshot_name
            public_page.screenshot(path=str(public_shot), full_page=False, animations="disabled", caret="hide")
            assert public_shot.exists() and public_shot.stat().st_size > 20_000
    finally:
        public_context.close()

    app_context = _new_context(browser, mobile=False, width=1366, height=960)
    app_context.add_init_script("window.localStorage.setItem('propertyquarry.theme', 'dark');")
    _issue_browser_workspace_session(client=client, context=app_context, base_url=base_url)
    try:
        page = app_context.new_page()
        for route, screenshot_name in (
            ("/app/search", "propertyquarry-search-dark-surfaces.png"),
            ("/app/properties", "propertyquarry-properties-dark-surfaces.png"),
            ("/app/account", "propertyquarry-account-dark-surfaces.png"),
            ("/app/billing", "propertyquarry-billing-dark-surfaces.png"),
            ("/app/alerts", "propertyquarry-alerts-dark-surfaces.png"),
            ("/app/agents", "propertyquarry-agents-dark-surfaces.png"),
            ("/app/shortlist?run_id=run-42&full=1", "propertyquarry-shortlist-management-dark-surfaces.png"),
            ("/app/settings/plan", "propertyquarry-settings-plan-dark-surfaces.png"),
            ("/app/settings/usage", "propertyquarry-settings-usage-dark-surfaces.png"),
            ("/app/settings/support", "propertyquarry-settings-support-dark-surfaces.png"),
            ("/app/settings/trust", "propertyquarry-settings-trust-dark-surfaces.png"),
            ("/app/settings/google", "propertyquarry-settings-google-dark-surfaces.png"),
            ("/app/settings/access", "propertyquarry-settings-access-dark-surfaces.png"),
            ("/app/settings/invitations", "propertyquarry-settings-invitations-dark-surfaces.png"),
            ("/app/settings/outcomes", "propertyquarry-settings-outcomes-dark-surfaces.png"),
            ("/app/properties/packets", "propertyquarry-packets-dark-surfaces.png"),
        ):
            response = page.goto(f"{base_url}{route}", wait_until="networkidle")
            assert response is not None
            if route == "/app/billing":
                assert response.ok
                _assert_no_horizontal_overflow(page)
                _assert_propertyquarry_billing_redirect_lands_on_account(page)
            else:
                assert response.ok
                expect(page.locator("html")).to_have_attribute("data-pq-theme", "dark")
                _assert_dark_mode_surfaces_stay_readable(page, app_selectors)
            screenshot = tmp_path / screenshot_name
            page.screenshot(path=str(screenshot), full_page=False, animations="disabled", caret="hide")
            assert screenshot.exists() and screenshot.stat().st_size > 20_000
    finally:
        app_context.close()


def test_propertyquarry_score_viewer_renders_compact_public_iframe(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_public_context(browser, mobile=False, width=1366, height=960)
    context.add_init_script("window.localStorage.setItem('propertyquarry.theme', 'dark');")
    try:
        page = context.new_page()
        response = page.goto(f"{base_url}/how-it-works/score?language=de&country=AT", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.locator("html")).to_have_attribute("data-pq-theme", "dark")
        page.locator(".pqx-score-viewer-shell").wait_for(state="visible")
        expect(page.locator(".pqx-score-viewer-frame iframe")).to_have_attribute(
            "src",
            re.compile(r"/v1/integrations/fliplink/documents/score-methodology\.pdf\?language=de&country=AT"),
        )
        _assert_no_horizontal_overflow(page)
        _assert_dark_mode_surfaces_stay_readable(
            page,
            [
                ".pqx-score-viewer-shell",
                ".pqx-score-viewer-card",
                ".pqx-score-viewer-meta span",
                ".pqx-score-viewer-frame",
            ],
        )
        screenshot_path = tmp_path / "propertyquarry-score-viewer-dark.png"
        page.screenshot(path=str(screenshot_path), full_page=False, animations="disabled", caret="hide")
        assert screenshot_path.exists() and screenshot_path.stat().st_size > 20_000
    finally:
        context.close()


def test_propertyquarry_greenfield_workspace_is_mobile_usable(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        content = page.content()
        assert 'data-property-decision-workbench' in content
        assert 'data-pq-greenfield-shell' in content
        assert 'data-property-mobile-dock' not in content
        page.locator("[data-workbench-row]:visible").first.wait_for(timeout=5000)
        if page.locator('[data-workbench-mobile-mode="results"]').count():
            assert page.locator('[data-workbench-mobile-mode="results"]').is_visible()
            assert page.locator('[data-workbench-mobile-mode="property"]').is_visible()
            mode_box = page.locator('[data-workbench-mobile-mode="results"]').bounding_box()
            assert mode_box is not None and mode_box["width"] <= 430
        _assert_mobile_topnav_tap_targets(page)
        _assert_property_shell_visual_gates(page, max_appbar_height=130)
        family_row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        family_ref = str(family_row.get_attribute("data-candidate-ref") or "").strip()
        assert family_ref
        family_row.locator(".pqx-result-title").click()
        expect(page.locator("[data-pw-title]").first).to_have_text("Family flat near Tiergarten")
        current_url = urllib.parse.urlparse(page.url)
        current_query = urllib.parse.parse_qs(current_url.query)
        assert current_url.path == "/app/shortlist"
        assert current_query["run_id"] == ["run-42"]
        assert current_query["candidate"] == [family_ref]
    finally:
        context.close()


def test_propertyquarry_all_customer_app_surfaces_are_motor_accessible_on_phone(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page: Page = context.new_page()
    routes = [
        "/app/search",
        "/app/shortlist?run_id=run-42&full=1",
        "/app/alerts",
        "/app/account",
        "/app/billing",
        "/app/settings/google",
        "/app/settings/access",
        "/app/settings/usage",
        "/app/settings/support",
        "/app/settings/trust",
        "/app/settings/invitations",
    ]
    try:
        for route in routes:
            response = page.goto(f"{base_url}{route}", wait_until="domcontentloaded")
            assert response is not None, route
            if route == "/app/billing":
                assert response.ok, route
                _assert_no_horizontal_overflow(page)
                _assert_propertyquarry_billing_redirect_lands_on_account(page)
                logout_button = page.get_by_role("button", name="Log out")
                box = logout_button.bounding_box()
                assert box is not None and box["height"] >= 44 and box["width"] >= 44
                continue
            assert response.ok, route
            page.locator("[data-property-research-topnav]").wait_for(state="visible", timeout=5000)
            _assert_mobile_surface_motor_accessible(page)
            _assert_mobile_topnav_tap_targets(page)

        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="domcontentloaded")
        assert response is not None and response.ok
        packet_href = page.locator('a[href*="/app/research/"]').first.get_attribute("href")
        assert packet_href
        response = page.goto(packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator("[data-property-research-detail]").wait_for(state="visible", timeout=5000)
        _assert_mobile_surface_motor_accessible(page)
    finally:
        context.close()


def test_propertyquarry_ui_behavior_audit_collects_no_dead_clickables_and_keeps_surface_load_fast(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    audited_routes = [
        ("/app/search", 22000),
        ("/app/properties?run_id=run-42&full=1", 14000),
        ("/app/shortlist?run_id=run-42&full=1", 14000),
        ("/app/research/altbau-u6?run_id=run-42", 13000),
        ("/app/alerts", 9000),
        ("/app/agents", 9000),
        ("/app/account", 9000),
        ("/app/settings/plan", 9000),
        ("/app/settings/access", 9000),
        ("/app/settings/usage", 9000),
        ("/app/settings/support", 9000),
        ("/app/settings/trust", 9000),
        ("/app/settings/invitations", 9000),
        ("/app/settings/outcomes", 9000),
        ("/app/billing", 9000),
    ]
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        data_deletion_response = client.get("/data-deletion")
        assert data_deletion_response.status_code == 200
        for route, max_budget_ms in audited_routes:
            _assert_surface_click_targets_are_hydrated(page, base_url, route, max_budget_ms=max_budget_ms)
            if route.startswith("/app/billing"):
                expect(page.get_by_role("button", name="Log out")).to_be_visible()
        mobile_context = _new_context(browser, mobile=True, width=390, height=844)
        mobile_page: Page = mobile_context.new_page()
        try:
            for route in ["/app/search", "/app/shortlist?run_id=run-42&full=1", "/app/properties?run_id=run-42&full=1"]:
                _assert_surface_click_targets_are_hydrated(
                    mobile_page,
                    base_url,
                    route,
                    max_budget_ms=12000,
                )
        finally:
            mobile_context.close()
    finally:
        context.close()


def test_propertyquarry_soft_only_empty_state_uses_neutral_empty_state_copy(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
        response = page.goto(f"{base_url}/app/properties?run_id=run-active-empty", wait_until="networkidle")
        assert response is not None and response.ok
        page.wait_for_selector("[data-pqx-empty-results]", timeout=7000)
        expect(page.locator("[data-pqx-empty-results]")).to_contain_text(re.compile(r"Nothing matched yet|Try this|Adjust search", re.I), timeout=5000)
        expect(page.locator("[data-pqx-empty-results]")).not_to_contain_text(re.compile(r"below the shortlist score|Lower shortlist score|Review scored homes", re.I), timeout=5000)
    finally:
        context.close()


def test_propertyquarry_repair_states_use_calm_browser_copy_on_properties_surface(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    original_get_run_status = ProductService.get_property_search_run_status

    def _fake_repair_surface_run_status(self, *, principal_id: str, run_id: str):
        if principal_id == "pq-greenfield-browser" and run_id == "run-failed-repair-browser":
            return {
                "run_id": run_id,
                "principal_id": principal_id,
                "status_url": f"/app/api/signals/property/search/run/{run_id}",
                "status": "failed",
                "progress": 100,
                "message": "Provider returned 403 while fetching Willhaben.",
                "summary": {
                    "sources_total": 1,
                    "provider_total": 1,
                    "listing_total": 0,
                    "tour_created_total": 0,
                    "tour_existing_total": 0,
                    "repair_status": "repairing",
                    "repair_status_label": "Repairing",
                    "repair_step_label": "Retrying Willhaben provider check",
                    "sources": [],
                },
                "events": [
                    {"step": "source_fetching", "message": "Fetching source page for Willhaben.", "status": "in_progress"},
                    {"step": "failed", "message": "Provider returned 403 while fetching Willhaben.", "status": "failed"},
                ],
            }
        if principal_id == "pq-greenfield-browser" and run_id == "run-failed-terminal-browser":
            return {
                "run_id": run_id,
                "principal_id": principal_id,
                "status_url": f"/app/api/signals/property/search/run/{run_id}",
                "status": "failed",
                "progress": 100,
                "message": "Provider returned 403 while fetching Encuentra24.",
                "summary": {
                    "sources_total": 1,
                    "provider_total": 1,
                    "listing_total": 0,
                    "tour_created_total": 0,
                    "tour_existing_total": 0,
                    "repair_status": "failed",
                    "repair_status_label": "Needs attention",
                    "customer_status_message": "The search could not confirm a ready shortlist from the available source pages.",
                    "repair_last_updated_at": "2026-06-27T00:07:07+00:00",
                    "repair_receipts": [
                        {
                            "resolution": "suppressed_source_fetch_forbidden",
                            "reason": "provider blocked or rejected automated source fetch",
                            "at": "2026-06-27T00:07:07+00:00",
                        }
                    ],
                    "sources": [],
                },
                "events": [
                    {"step": "source_fetching", "message": "Fetching source page for Encuentra24.", "status": "in_progress"},
                    {"step": "failed", "message": "Provider returned 403 while fetching Encuentra24.", "status": "failed"},
                ],
            }
        return original_get_run_status(self, principal_id=principal_id, run_id=run_id)

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_repair_surface_run_status)

    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/properties?run_id=run-failed-repair-browser", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.wait_for_selector("[data-pqx-empty-results]", timeout=7000)
        expect(page.locator(".pqx-shell")).to_have_attribute("data-pqx-state", "empty_results")
        expect(page.locator("[data-pqx-empty-results] h1")).to_contain_text(
            "PropertyQuarry is checking the saved search again."
        )
        expect(page.locator("[data-pqx-empty-results] .pqx-empty-outcome-line")).to_contain_text(
            "PropertyQuarry started a fresh check before any listing inspection completed."
        )
        visible_text = page.locator("body").inner_text()
        assert "Repair took over before any listing inspection completed." not in visible_text
        assert "Provider returned 403 while fetching Willhaben." not in visible_text
        assert "Retrying Willhaben provider check" not in visible_text
        assert "Repair is queued for the interrupted provider checks." not in visible_text
        assert "Auto-repair is queued and will retry" not in visible_text
        assert "updates quietly every 10s" not in visible_text
        assert "Checking repair status automatically every 10s." not in visible_text
        assert "Check repair status" not in visible_text
        assert "More options" in visible_text
        assert "Edit search" in visible_text
        _assert_no_horizontal_overflow(page)
        _assert_property_shell_visual_gates(page, max_appbar_height=92)

        response = page.goto(f"{base_url}/app/properties?run_id=run-failed-terminal-browser", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.wait_for_selector("[data-pqx-empty-results]", timeout=7000)
        expect(page.locator(".pqx-shell")).to_have_attribute("data-pqx-state", "empty_results")
        expect(page.locator("[data-pqx-empty-results] h1")).to_contain_text(
            "One site changed, so this search could not finish automatically."
        )
        expect(page.locator("[data-pqx-empty-results] .pqx-empty-outcome-line")).to_contain_text(
            "Last real update: Jun 27, 2026 00:07 UTC."
        )
        terminal_text = page.locator("body").inner_text()
        assert "Provider returned 403 while fetching Encuentra24." not in terminal_text
        assert "Wait for repair" not in terminal_text
        assert "Repair is retrying the saved search." not in terminal_text
        assert "This source stopped returning a usable listing page." not in terminal_text
        _assert_no_horizontal_overflow(page)
        _assert_property_shell_visual_gates(page, max_appbar_height=92)
    finally:
        context.close()


def test_propertyquarry_search_goal_toggle_keeps_underwriting_controls_hidden_until_enabled_in_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda err: page_errors.append(str(err)))
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok
        investment_mode = page.locator('[data-property-field-name="investment_research_mode"]').first
        listing_mode = page.locator('[data-property-field-name="listing_mode"]').first
        investment_strategy = page.locator('[data-property-field-name="investment_strategy"]').first
        investment_equity = page.locator('[data-property-field-name="equity_available_eur"]').first
        investment_loan_term = page.locator('[data-property-field-name="loan_term_years"]').first
        investment_rate = page.locator('[data-property-field-name="max_interest_rate_pct"]').first
        investment_dscr = page.locator('[data-property-field-name="min_dscr"]').first
        investment_vacancy = page.locator('[data-property-field-name="vacancy_reserve_pct"]').first
        investment_floorplan = page.locator('[data-property-field-name="investment_require_floorplan"]').first
        public_housing = page.locator('[data-property-field-name="include_public_housing_signals"]').first
        wohnticket = page.locator('[data-property-field-name="wiener_wohnticket_available"]').first
        distressed = page.locator('[data-property-field-name="include_distressed_sale_signals"]').first
        assert investment_mode.evaluate("(node) => node.hidden") is True
        assert investment_strategy.evaluate("(node) => node.hidden") is True
        assert investment_equity.evaluate("(node) => node.hidden") is True
        assert investment_loan_term.evaluate("(node) => node.hidden") is True
        assert investment_rate.evaluate("(node) => node.hidden") is True
        assert investment_dscr.evaluate("(node) => node.hidden") is True
        assert investment_vacancy.evaluate("(node) => node.hidden") is True
        assert investment_floorplan.evaluate("(node) => node.hidden") is True

        page.locator('select[name="search_goal"]').select_option("investment")

        page.wait_for_function(
            """
            () => {
              const mode = document.querySelector('[data-property-field-name="investment_research_mode"]');
              const listingMode = document.querySelector('[data-property-field-name="listing_mode"]');
              const strategy = document.querySelector('[data-property-field-name="investment_strategy"]');
              const equity = document.querySelector('[data-property-field-name="equity_available_eur"]');
              const loanTerm = document.querySelector('[data-property-field-name="loan_term_years"]');
              const rate = document.querySelector('[data-property-field-name="max_interest_rate_pct"]');
              const dscr = document.querySelector('[data-property-field-name="min_dscr"]');
              const vacancy = document.querySelector('[data-property-field-name="vacancy_reserve_pct"]');
              const floorplan = document.querySelector('[data-property-field-name="investment_require_floorplan"]');
              const publicHousing = document.querySelector('[data-property-field-name="include_public_housing_signals"]');
              const wohnticket = document.querySelector('[data-property-field-name="wiener_wohnticket_available"]');
              return Boolean(
                mode && listingMode && strategy && equity && loanTerm && rate && dscr && vacancy && floorplan && publicHousing && wohnticket
                && !mode.hidden
                && listingMode.hidden
                && strategy.hidden
                && equity.hidden
                && loanTerm.hidden
                && rate.hidden
                && dscr.hidden
                && vacancy.hidden
                && floorplan.hidden
                && publicHousing.hidden
                && wohnticket.hidden
              );
            }
            """
        )
        assert listing_mode.evaluate("(node) => node.hidden") is True
        assert investment_mode.evaluate("(node) => node.hidden") is False
        assert investment_strategy.evaluate("(node) => node.hidden") is True
        assert investment_equity.evaluate("(node) => node.hidden") is True
        assert investment_loan_term.evaluate("(node) => node.hidden") is True
        assert investment_rate.evaluate("(node) => node.hidden") is True
        assert investment_dscr.evaluate("(node) => node.hidden") is True
        assert investment_vacancy.evaluate("(node) => node.hidden") is True
        assert investment_floorplan.evaluate("(node) => node.hidden") is True
        assert public_housing.evaluate("(node) => node.hidden") is True
        assert wohnticket.evaluate("(node) => node.hidden") is True

        page.locator('select[name="investment_research_mode"]').select_option("auto")

        page.wait_for_function(
            """
            () => {
              const strategy = document.querySelector('[data-property-field-name="investment_strategy"]');
              const equity = document.querySelector('[data-property-field-name="equity_available_eur"]');
              const loanTerm = document.querySelector('[data-property-field-name="loan_term_years"]');
              const rate = document.querySelector('[data-property-field-name="max_interest_rate_pct"]');
              const dscr = document.querySelector('[data-property-field-name="min_dscr"]');
              const vacancy = document.querySelector('[data-property-field-name="vacancy_reserve_pct"]');
              const floorplan = document.querySelector('[data-property-field-name="investment_require_floorplan"]');
              return Boolean(strategy && equity && loanTerm && rate && dscr && vacancy && floorplan && !strategy.hidden && !equity.hidden && !loanTerm.hidden && !rate.hidden && !dscr.hidden && !vacancy.hidden && floorplan.hidden);
            }
            """
        )
        assert investment_strategy.evaluate("(node) => node.hidden") is False
        assert investment_equity.evaluate("(node) => node.hidden") is False
        assert investment_loan_term.evaluate("(node) => node.hidden") is False
        assert investment_rate.evaluate("(node) => node.hidden") is False
        assert investment_dscr.evaluate("(node) => node.hidden") is False
        assert investment_vacancy.evaluate("(node) => node.hidden") is False
        assert investment_floorplan.evaluate("(node) => node.hidden") is True

        page.locator('[data-property-step-next]').click()

        page.wait_for_function(
            """
            () => {
              const floorplan = document.querySelector('[data-property-field-name="investment_require_floorplan"]');
              return Boolean(floorplan && !floorplan.hidden);
            }
            """
        )
        assert investment_floorplan.evaluate("(node) => node.hidden") is False

        page.locator('[data-property-step-trigger="search"]').click()
        page.wait_for_function(
            """
            () => {
              const searchGoal = document.querySelector('select[name="search_goal"]');
              return Boolean(searchGoal && !searchGoal.hidden && searchGoal.offsetParent !== null);
            }
            """
        )
        page.locator('select[name="search_goal"]').select_option("home")
        page.locator('select[name="listing_mode"]').select_option("rent")

        page.wait_for_function(
            """
            () => {
              const mode = document.querySelector('[data-property-field-name="investment_research_mode"]');
              const strategy = document.querySelector('[data-property-field-name="investment_strategy"]');
              const equity = document.querySelector('[data-property-field-name="equity_available_eur"]');
              const loanTerm = document.querySelector('[data-property-field-name="loan_term_years"]');
              const rate = document.querySelector('[data-property-field-name="max_interest_rate_pct"]');
              const dscr = document.querySelector('[data-property-field-name="min_dscr"]');
              const vacancy = document.querySelector('[data-property-field-name="vacancy_reserve_pct"]');
              const floorplan = document.querySelector('[data-property-field-name="investment_require_floorplan"]');
              const distressed = document.querySelector('[data-property-field-name="include_distressed_sale_signals"]');
              const progress = document.querySelector('[data-property-step-progress]');
              const workflowStep = [...document.querySelectorAll('[data-property-step-trigger] strong')].map((node) => node.textContent || '');
                  return Boolean(
                    mode && strategy && equity && loanTerm && rate && dscr && vacancy && floorplan && distressed && progress
                    && mode.hidden
                    && strategy.hidden
                    && equity.hidden
                && loanTerm.hidden
                && rate.hidden
                && dscr.hidden
                    && vacancy.hidden
                    && floorplan.hidden
                    && distressed.hidden
                    && String(progress.textContent || '').includes('Where')
                    && workflowStep.includes('What')
                    && workflowStep.includes('What matters')
                    && workflowStep.includes('Reachability')
                    && workflowStep.includes('Research depth')
                  );
                }
                """
        )
        assert investment_mode.evaluate("(node) => node.hidden") is True
        assert investment_strategy.evaluate("(node) => node.hidden") is True
        assert investment_equity.evaluate("(node) => node.hidden") is True
        assert investment_loan_term.evaluate("(node) => node.hidden") is True
        assert investment_rate.evaluate("(node) => node.hidden") is True
        assert investment_dscr.evaluate("(node) => node.hidden") is True
        assert investment_vacancy.evaluate("(node) => node.hidden") is True
        assert investment_floorplan.evaluate("(node) => node.hidden") is True
        assert distressed.evaluate("(node) => node.hidden") is True
    finally:
        context.close()


def test_propertyquarry_workbench_tracks_household_and_followup_state_in_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    property_ref = "https://www.immobilienscout24.de/expose/altbau-u6"
    first = client.post(
        "/app/api/property-feedback",
        json={
            "stakeholder_id": "family-mara",
            "stakeholder_label": "Mara",
            "property_ref": property_ref,
            "category": "question",
            "sentiment": "neutral",
            "importance": 4,
            "text": "Can the agent confirm the operating costs?",
            "source": "clippy_agent_brief",
            "followup_status": "asked",
        },
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/app/api/property-feedback",
        json={
            "stakeholder_id": "family-jonas",
            "stakeholder_label": "Jonas",
            "property_ref": property_ref,
            "category": "dealbreaker",
            "sentiment": "negative",
            "importance": 5,
            "text": "Street noise still feels risky.",
            "source": "packet",
            "decision_state": "rejected",
        },
    )
    assert second.status_code == 200, second.text

    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        packet_path = page.locator("[data-workbench-row]", has_text="Altbau near U6").first.get_attribute("data-candidate-packet-url")
        assert packet_path
        response = page.goto(f"{base_url}{packet_path}?run_id=run-42" if "?" not in packet_path else f"{base_url}{packet_path}", wait_until="networkidle")
        assert response is not None and response.ok
        assert page.locator("body", has_text="At a glance").is_visible()
        page.locator("[data-object-feedback-open-advanced]").click()
        expect(page.locator("[data-object-followups]")).to_contain_text("Can the agent confirm the operating costs?")
        page.locator(".prd-decision-advanced summary", has_text="Why it works and next step").click()
        answered_button = page.locator('[data-object-followups] [data-object-followup-action="answered"]:visible').first
        expect(answered_button).to_be_visible()
        answered_button.scroll_into_view_if_needed()
        with page.expect_response("**/app/api/property-feedback/*/followup-status") as update_response_info:
            answered_button.click()
        update_response = update_response_info.value
        assert update_response.ok, update_response.text()
        page.reload(wait_until="networkidle")
        page.locator("[data-object-feedback-open-advanced]").click()
        expect(page.locator("[data-object-followups]")).to_contain_text("Can the agent confirm the operating costs?")
        assert page.locator("body", has_text="Notes").is_visible()
    finally:
        context.close()


def test_propertyquarry_packet_tracks_followup_state_in_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    property_ref = "https://www.immobilienscout24.de/expose/altbau-u6"
    seeded = client.post(
        "/app/api/property-feedback",
        json={
            "stakeholder_id": "family-mara",
            "stakeholder_label": "Mara",
            "property_ref": property_ref,
            "category": "question",
            "sentiment": "neutral",
            "importance": 4,
            "text": "Can the agent confirm the operating costs?",
            "source": "clippy_agent_brief",
            "followup_status": "asked",
        },
    )
    assert seeded.status_code == 200, seeded.text

    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        packet_path = page.locator("[data-workbench-row]", has_text="Altbau near U6").first.get_attribute("data-candidate-packet-url")
        assert packet_path
        response = page.goto(f"{base_url}{packet_path}?run_id=run-42" if "?" not in packet_path else f"{base_url}{packet_path}", wait_until="networkidle")
        assert response is not None and response.ok
        page.locator("[data-object-feedback-open-advanced]").click()
        expect(page.locator("[data-object-followups]")).to_contain_text("Can the agent confirm the operating costs?")
        page.locator(".prd-decision-advanced summary", has_text="Why it works and next step").click()
        answered_button = page.locator('[data-object-followups] [data-object-followup-action="answered"]:visible').first
        expect(answered_button).to_be_visible()
        answered_button.scroll_into_view_if_needed()
        with page.expect_response("**/app/api/property-feedback/*/followup-status") as update_response_info:
            answered_button.click()
        update_response = update_response_info.value
        assert update_response.ok, update_response.text()
        expect(page.locator("[data-object-followup-status]")).to_have_text("Follow-up marked answered.")
        page.reload(wait_until="networkidle")
        page.locator("[data-object-feedback-open-advanced]").click()
        expect(page.locator("[data-object-followups]")).to_contain_text("Answered")
    finally:
        context.close()


def test_propertyquarry_decision_to_clippy_to_packet_followup_flow_in_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda err: page_errors.append(str(err)))
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        candidate_ref = page.locator("[data-workbench-row]", has_text="Altbau near U6").first.get_attribute("data-candidate-ref")
        packet_path = page.locator("[data-workbench-row]", has_text="Altbau near U6").first.get_attribute("data-candidate-packet-url")
        assert candidate_ref
        assert packet_path

        response = page.goto(f"{base_url}{packet_path}?run_id=run-42" if "?" not in packet_path else f"{base_url}{packet_path}", wait_until="networkidle")
        assert response is not None and response.ok
        with page.expect_response("**/preference-profile/property-feedback") as save_response_info:
            page.get_by_role("button", name="No", exact=True).click()
            page.locator("[data-object-feedback-save]").click()
        save_response = save_response_info.value
        assert save_response.ok, save_response.text()
        page.locator("[data-object-feedback-open-advanced]").click()
        expect(page.locator(".prd-decision-advanced")).to_have_attribute("open", "")
        page.locator(".prd-decision-advanced summary", has_text="Why it works and next step").click()
        assert page.locator("[data-object-followups]").is_visible()
        assert page.locator("body", has_text="At a glance").is_visible()
        assert page.locator("body", has_text="Next step").is_visible()
        assert page.locator("body", has_text="Adjust next search").is_visible()
    finally:
        context.close()


def test_propertyquarry_active_run_auto_polls_notifies_and_renders_empty_result_desk(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "1020 Vienna",
            "selected_platforms": ["willhaben"],
        },
    )
    assert stored.status_code == 200, stored.text
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    page.add_init_script(
        """
        (() => {
          try { Object.defineProperty(window, 'isSecureContext', { get: () => true, configurable: true }); } catch (_err) {}
          window.localStorage.setItem('propertyquarry.browserNotifications.enabled', '1');
          class FakeNotification {
            static permission = 'granted';
            static requestPermission = async () => 'granted';
            constructor(title, options) {
              window.localStorage.setItem('pq-test-notification-title', String(title || ''));
              window.localStorage.setItem('pq-test-notification-body', String((options && options.body) || ''));
              this.close = () => {};
            }
          }
          try { Object.defineProperty(window, 'Notification', { value: FakeNotification, configurable: true }); }
          catch (_err) { window.Notification = FakeNotification; }
        })();
        """
    )
    try:
        response = page.goto(f"{base_url}/app/properties?run_id=run-active-empty", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.wait_for_function(
            "window.localStorage.getItem('propertyquarry.browserNotifications.run.run-active-empty.processed') === '1'",
            timeout=7000,
        )
        page.wait_for_selector("[data-pqx-empty-results]", timeout=7000)
        expect(page.locator("[data-pqx-empty-results]")).to_be_visible()
        assert page.locator("body", has_text=re.compile("Nothing matched yet|Try this|Adjust search", re.I)).is_visible()
        assert page.locator("body", has_text=re.compile("Lower shortlist score|below the shortlist score", re.I)).count() == 0
        assert page.locator('[data-pqx-empty-results] [data-pqx-filter-slider][data-pqx-filter-field="min_match_score"]').count() == 0
        assert page.locator('[data-pqx-empty-results] [data-pqx-counterfactual-action-kind="ranking_bar"]').count() == 0
        assert page.evaluate("window.localStorage.getItem('pq-test-notification-title')") == "PropertyQuarry results are ready"
        assert "0 matching homes ready." in str(page.evaluate("window.localStorage.getItem('pq-test-notification-body')"))
        _assert_property_shell_visual_gates(page, max_appbar_height=92)
    finally:
        context.close()


def test_propertyquarry_stale_terminal_zero_first_paint_reconciles_to_authoritative_live_run(
    monkeypatch: pytest.MonkeyPatch,
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    selected_platforms = [
        "encuentra24_cr",
        "re_cr_mls",
        "realtor_cr",
        "properstar_cr",
        "coldwellbanker_cr",
        "century21_cr",
        "remax_cr",
        "theagency_cr",
        "krain_cr",
        "desarrollos_cr",
    ]
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "CR",
            "language_code": "es",
            "listing_mode": "buy",
            "property_type": "apartment",
            "region_code": "costa_rica",
            "location_query": "Costa Rica",
            "selected_platforms": selected_platforms,
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )
    assert stored.status_code == 200, stored.text
    status_calls = {"full": 0, "lightweight": 0}

    def _fake_run_status(
        self,
        *,
        principal_id: str,
        run_id: str,
        lightweight: bool = False,
        account_email: str = "",
    ):
        assert principal_id == "pq-greenfield-browser"
        assert run_id == "run-stale-terminal-zero"
        assert account_email == ""
        common = {
            "run_id": run_id,
            "principal_id": principal_id,
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "property_search_preferences": {
                "country_code": "CR",
                "listing_mode": "buy",
                "property_type": "apartment",
                "region_code": "costa_rica",
                "location_query": "Costa Rica",
                "selected_platforms": selected_platforms,
            },
        }
        if lightweight:
            status_calls["lightweight"] += 1
            return {
                **common,
                "status": "in_progress",
                "progress": 44,
                "message": "Checking Costa Rica sources.",
                    "summary": {
                        "status": "in_progress",
                        "provider_total": 10,
                        "provider_display_total": 10,
                    "source_variant_total": 10,
                    "sources_total": 10,
                    "sources_completed": 10,
                    "listing_total": 10,
                    "reviewed_listing_total": 10,
                    "ranked_candidates": [],
                    "sources": [
                        *[
                            {"source_label": f"Costa Rica source {index}", "status": "completed"}
                            for index in range(4)
                        ],
                        {"source_label": "Costa Rica source 4", "status": "in_progress"},
                        {"source_label": "Costa Rica source 5", "status": "warming"},
                        *[
                            {"source_label": f"Costa Rica source {index}", "status": "queued"}
                            for index in range(6, 10)
                        ],
                    ],
                },
                "events": [
                    {
                        "step": "provider_scan",
                        "message": "Checking Costa Rica sources.",
                        "status": "in_progress",
                    }
                ],
            }
        status_calls["full"] += 1
        return {
            **common,
            "status": "processed",
            "progress": 100,
            "message": "Property scouting run completed.",
                "summary": {
                    "status": "processed",
                    "provider_total": 10,
                    "provider_display_total": 10,
                "source_variant_total": 10,
                "sources_total": 10,
                "sources_completed": 10,
                "listing_total": 10,
                "ranked_candidates": [],
                "sources": [],
            },
            "events": [],
        }

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_run_status)
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        response = page.goto(
            f"{base_url}/app/search?run_id=run-stale-terminal-zero",
            wait_until="domcontentloaded",
        )
        assert response is not None and response.ok
        page.wait_for_selector("[data-pqx-reconciled-running]", timeout=7000)
        expect(page.locator("[data-property-decision-workbench]")).to_have_attribute("data-pqx-state", "running")
        expect(page.locator("[data-pqx-reconciled-running]")).to_be_visible()
        expect(page.locator("[data-pqx-progress-board]")).to_be_visible()
        expect(page.locator("[data-pqx-radar-count]")).to_contain_text("4 / 10")
        expect(page.locator("[data-pqx-empty-results]")).to_have_count(0)
        expect(page.locator("[data-pqx-run-status]")).not_to_have_text("Finished")
        assert status_calls["full"] >= 1
        assert status_calls["lightweight"] >= 1
        assert page_errors == []
    finally:
        context.close()


def test_propertyquarry_browser_alert_button_toggles_enabled_state(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    page.add_init_script(
        """
        (() => {
          try { Object.defineProperty(window, 'isSecureContext', { get: () => true, configurable: true }); } catch (_err) {}
          window.localStorage.setItem('propertyquarry.browserNotifications.enabled', '1');
          window.localStorage.setItem('pq-test-notification-permission-requests', '0');
          class FakeNotification {
            static permission = 'granted';
            static requestPermission = async () => {
              const current = Number(window.localStorage.getItem('pq-test-notification-permission-requests') || '0');
              window.localStorage.setItem('pq-test-notification-permission-requests', String(current + 1));
              return 'granted';
            };
            constructor() {
              this.close = () => {};
            }
          }
          try { Object.defineProperty(window, 'Notification', { value: FakeNotification, configurable: true }); }
          catch (_err) { window.Notification = FakeNotification; }
        })();
        """
    )
    try:
        response = page.goto(f"{base_url}/app/properties?run_id=run-active-empty", wait_until="domcontentloaded")
        assert response is not None and response.ok
        browser_alerts = page.get_by_role("button", name="Browser alerts on")
        expect(browser_alerts).to_be_visible()
        browser_alerts.click()
        expect(page.get_by_role("button", name="Browser alerts off")).to_be_visible()
        assert page.evaluate("window.localStorage.getItem('propertyquarry.browserNotifications.enabled')") is None
        assert page.evaluate("window.localStorage.getItem('pq-test-notification-permission-requests')") == "0"
        page.get_by_role("button", name="Browser alerts off").click()
        expect(page.get_by_role("button", name="Browser alerts on")).to_be_visible()
        assert page.evaluate("window.localStorage.getItem('propertyquarry.browserNotifications.enabled')") == "1"
        assert page.evaluate("window.localStorage.getItem('pq-test-notification-permission-requests')") == "0"
    finally:
        context.close()


def test_propertyquarry_running_progress_panel_fits_the_first_viewport(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    screenshot_path = tmp_path / "property_running_progress_desktop.png"
    try:
        response = page.goto(f"{base_url}/app/properties?run_id=run-always-active", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.wait_for_selector('[data-pqx-screenfit-target="run-progress"]', timeout=5000)
        assert page.locator('[data-pqx-screenfit-target="run-progress"] :is(h1, h2)').first.is_visible()
        progress_target = page.locator('[data-pqx-screenfit-target="run-progress"]').first
        progress_text = progress_target.inner_text().strip()
        assert progress_text
        assert "source update" not in progress_text.lower()
        assert "source lanes" not in progress_text.lower()
        assert "provider" not in progress_text.lower()
        assert re.search(r"\bsources?\b|\bsearch pages?\b", progress_text, flags=re.IGNORECASE)
        page.screenshot(path=str(screenshot_path), full_page=False)
        layout = page.evaluate(
            """
            () => {
              const target = document.querySelector('[data-pqx-screenfit-target="run-progress"]');
              const board = document.querySelector('[data-pqx-progress-board]');
              if (!target) return null;
              const targetBox = target.getBoundingClientRect();
              const boardBox = board ? board.getBoundingClientRect() : targetBox;
              return {
                viewportHeight: window.innerHeight,
                viewportWidth: window.innerWidth,
                targetBottom: Math.round(targetBox.bottom),
                targetRight: Math.round(targetBox.right),
                boardBottom: Math.round(boardBox.bottom),
                boardRight: Math.round(boardBox.right),
                targetFitsViewport: targetBox.bottom <= window.innerHeight + 2 && targetBox.right <= window.innerWidth + 2,
                boardFitsViewport: boardBox.bottom <= window.innerHeight + 2 && boardBox.right <= window.innerWidth + 2,
                etaLabel: (document.querySelector('[data-pqx-progress-eta]')?.textContent || '').trim(),
              };
            }
            """
        )
        assert layout is not None
        assert layout["targetFitsViewport"] is True
        assert layout["boardFitsViewport"] is True
        _assert_property_shell_visual_gates(page, max_appbar_height=92)
    finally:
        context.close()


def test_propertyquarry_setup_header_stays_minimal_and_single_row(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    context = _new_context(browser, mobile=False)
    _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
    page: Page = context.new_page()
    screenshot_path = tmp_path / "propertyquarry-setup-header.png"
    try:
        response = page.goto(f"{base_url}/app/properties", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.wait_for_selector(".pqx-topbar", timeout=5000)
        page.screenshot(path=str(screenshot_path), full_page=False)
        assert page.locator("[data-property-pulse-strip]").count() == 0
        expect(page.get_by_role("navigation", name="PropertyQuarry sections")).to_be_visible()
        assert page.locator(".pqx-account-menu > summary").count() == 1
        assert page.locator(".pqx-account-menu > summary").inner_text().strip() != "Account"
        _assert_property_shell_visual_gates(page, max_appbar_height=84)
    finally:
        context.close()


def test_propertyquarry_workspace_sign_out_works_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    context = _new_context(browser, mobile=False)
    _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        account_summary = page.locator(".pqx-account-menu > summary")
        expect(account_summary).to_be_visible()

        account_summary.click()
        logout_button = page.locator(".pqx-account-menu-form button", has_text="Log out")
        expect(logout_button).to_be_visible()

        logout_button.click()
        page.wait_for_load_state("domcontentloaded")
        assert page.url.startswith(f"{base_url}") or page.url.startswith("http://propertyquarry.com:")
        assert page.url != f"{base_url}/app/shortlist?run_id=run-42&full=1"

        # Signed-out pages should no longer expose the account menu.
        page.wait_for_timeout(100)
        assert page.locator(".pqx-account-menu > summary").count() == 0
    finally:
        context.close()


def test_propertyquarry_account_mobile_exposes_direct_logout(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    context = _new_context(browser, mobile=True, width=390, height=844)
    _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/account", wait_until="networkidle")
        assert response is not None and response.ok
        logout_form = page.locator("[data-account-page-sign-out]")
        expect(logout_form).to_be_visible()
        logout_button = logout_form.get_by_role("button", name="Log out")
        expect(logout_button).to_be_visible()
        metrics = logout_button.evaluate(
            """(button) => {
              const rect = button.getBoundingClientRect();
              return {
                top: rect.top,
                bottom: rect.bottom,
                width: rect.width,
                height: rect.height,
                viewportWidth: window.innerWidth,
                bodyWidth: document.documentElement.scrollWidth,
                label: button.textContent || '',
              };
            }"""
        )
        assert metrics["height"] >= 48, metrics
        assert metrics["width"] >= 160, metrics
        assert metrics["bodyWidth"] <= metrics["viewportWidth"] + 1, metrics
        assert "Log out" in metrics["label"]

        logout_button.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(100)
        assert page.locator("[data-account-page-sign-out]").count() == 0
        assert page.locator(".pqx-account-menu > summary").count() == 0
    finally:
        context.close()


def test_propertyquarry_account_notifications_save_multi_channel_preferences_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    context = _new_context(browser, mobile=False)
    access_token = _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/account", wait_until="networkidle")
        assert response is not None and response.ok
        delivery_card = page.locator("#delivery")
        expect(delivery_card).to_be_visible()
        delivery_card.evaluate("(node) => { node.open = true; }")

        email_checkbox = delivery_card.locator('input[name="notification_channels"][value="email"]')
        telegram_checkbox = delivery_card.locator('input[name="notification_channels"][value="telegram"]')
        whatsapp_checkbox = delivery_card.locator('input[name="notification_channels"][value="whatsapp"]')
        whatsapp_phone = delivery_card.locator("#whatsappAiSupportPhone")

        expect(email_checkbox).to_be_checked()
        telegram_checkbox.uncheck()
        whatsapp_checkbox.check()
        whatsapp_phone.fill("+43 660 0000000")

        with page.expect_navigation(wait_until="networkidle"):
            delivery_card.get_by_role("button", name="Save").click()

        assert "/app/account?notifications_saved=1#delivery" in page.url
        expect(email_checkbox).to_be_checked()
        expect(telegram_checkbox).not_to_be_checked()
        expect(whatsapp_checkbox).to_be_checked()
        expect(whatsapp_phone).to_have_value("+436600000000")

        original_workspace_cookie = client.cookies.get("ea_workspace_session")
        client.cookies.set("ea_workspace_session", access_token)
        export = client.get("/app/api/property/account/export")
        if original_workspace_cookie:
            client.cookies.set("ea_workspace_session", original_workspace_cookie)
        else:
            client.cookies.pop("ea_workspace_session", None)
        assert export.status_code == 200
        preferences = export.json()["delivery_preferences"]["property_notifications"]
        assert preferences["selected_channels"] == ["email", "whatsapp"]
        assert preferences["preferred_channel"] == "email"
        assert preferences["whatsapp_ai_support_phone"] == "+436600000000"
    finally:
        context.close()


def test_propertyquarry_account_notifications_have_dedicated_phone_layout(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    context = _new_context(browser, mobile=True, width=390, height=844)
    _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/account?billing=1#delivery", wait_until="networkidle")
        assert response is not None and response.ok
        delivery_card = page.locator("#delivery")
        expect(delivery_card).to_be_visible()
        delivery_card.evaluate("(node) => { node.open = true; }")
        delivery_card.locator('input[name="notification_channels"][value="whatsapp"]').check()

        metrics = page.evaluate(
            """() => {
                const options = document.querySelector('.pqx-account-channel-options');
                const rows = Array.from(document.querySelectorAll('.pqx-account-channel-option'));
                const details = Array.from(document.querySelectorAll('.pqx-account-channel-detail'))
                    .filter((node) => {
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    });
                const inputs = details.flatMap((detail) => Array.from(detail.querySelectorAll('input')));
                const rowRects = rows.map((row) => row.getBoundingClientRect());
                const detailRects = details.map((detail) => detail.getBoundingClientRect());
                const inputRects = inputs.map((input) => input.getBoundingClientRect());
                return {
                    viewportWidth: window.innerWidth,
                    columns: options ? window.getComputedStyle(options).gridTemplateColumns.split(' ').filter(Boolean).length : 0,
                    rowCount: rows.length,
                    minRowHeight: Math.min(...rowRects.map((rect) => rect.height)),
                    maxRowRight: Math.max(...rowRects.map((rect) => rect.right)),
                    visibleDetailCount: details.length,
                    maxDetailRight: Math.max(...detailRects.map((rect) => rect.right)),
                    minInputHeight: Math.min(...inputRects.map((rect) => rect.height)),
                    maxInputRight: Math.max(...inputRects.map((rect) => rect.right)),
                    bodyWidth: document.documentElement.scrollWidth,
                };
            }"""
        )
        assert metrics["columns"] == 1, metrics
        assert metrics["rowCount"] >= 3, metrics
        assert metrics["minRowHeight"] >= 52, metrics
        assert metrics["visibleDetailCount"] >= 2, metrics
        assert metrics["minInputHeight"] >= 48, metrics
        assert metrics["bodyWidth"] <= metrics["viewportWidth"] + 1, metrics
        assert metrics["maxRowRight"] <= metrics["viewportWidth"] + 1, metrics
        assert metrics["maxDetailRight"] <= metrics["viewportWidth"] + 1, metrics
        assert metrics["maxInputRight"] <= metrics["viewportWidth"] + 1, metrics
    finally:
        context.close()


@pytest.mark.parametrize("route", ["/app/account", "/app/alerts"])
def test_propertyquarry_mobile_generic_folds_only_keep_one_panel_open(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    route: str,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    context = _new_context(browser, mobile=True, width=390, height=844)
    _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}{route}", wait_until="networkidle")
        assert response is not None and response.ok

        metrics = page.evaluate(
            """() => {
                const folds = Array.from(document.querySelectorAll('details.pqx-mobile-fold'));
                const initialOpenCount = folds.filter((node) => node.open).length;
                folds.forEach((node) => { node.open = false; });
                if (folds.length >= 2) {
                    const summaries = folds.map((node) => node.querySelector('summary')).filter(Boolean);
                    summaries[0]?.click();
                    summaries[1]?.click();
                }
                return {
                    foldCount: folds.length,
                    initialOpenCount,
                    openCount: folds.filter((node) => node.open).length,
                    secondOpen: Boolean(folds[1]?.open),
                    labels: folds.map((node) => (
                        node.querySelector('summary strong, summary h2')?.textContent || ''
                    ).trim()).filter(Boolean),
                };
            }"""
        )
        assert metrics["foldCount"] >= 2, metrics
        assert metrics["initialOpenCount"] == 0, metrics
        assert metrics["openCount"] == 1, metrics
        assert metrics["secondOpen"] is True, metrics
    finally:
        context.close()


def test_propertyquarry_mobile_shortlist_keeps_one_focus_and_opens_the_property_page(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page: Page = context.new_page()
    screenshot_path = tmp_path / "propertyquarry-shortlist-mobile-single-focus.png"
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok

        metrics = page.evaluate(
            """() => {
                const panel = document.querySelector('.pqx-mobile-property-panel');
                const panelStyle = panel ? window.getComputedStyle(panel) : null;
                const rows = Array.from(document.querySelectorAll('[data-workbench-row]'));
                const firstRow = rows[0] || null;
                const firstRowRect = firstRow ? firstRow.getBoundingClientRect() : null;
                const actionRow = firstRow?.querySelector('.pqx-result-actions');
                const visibleActions = actionRow
                  ? Array.from(actionRow.querySelectorAll(':scope > :is(a,button)')).filter((node) => {
                      const rect = node.getBoundingClientRect();
                      const style = window.getComputedStyle(node);
                      return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    })
                  : [];
                return {
                  viewportWidth: window.innerWidth,
                  panelDisplay: panelStyle ? panelStyle.display : '',
                  panelVisible: Boolean(panel && panel.getBoundingClientRect().width > 0 && panel.getBoundingClientRect().height > 0),
                  rowCount: rows.length,
                  firstRowRight: Math.round(firstRowRect?.right || 0),
                  firstRowHeight: Math.round(firstRowRect?.height || 0),
                  actionCount: visibleActions.length,
                  actionLabels: visibleActions.map((node) => (node.textContent || '').trim()).filter(Boolean),
                  actionHeights: visibleActions.map((node) => Math.round(node.getBoundingClientRect().height)),
                  bodyWidth: document.documentElement.scrollWidth,
                };
            }"""
        )
        assert metrics["panelDisplay"] == "none", metrics
        assert metrics["panelVisible"] is False, metrics
        assert metrics["rowCount"] >= 3, metrics
        assert metrics["firstRowRight"] <= metrics["viewportWidth"] + 1, metrics
        assert metrics["bodyWidth"] <= metrics["viewportWidth"] + 1, metrics
        assert metrics["firstRowHeight"] >= 120, metrics
        assert metrics["actionLabels"] == ["Review", "Open property", "Remove"], metrics
        assert metrics["actionHeights"] and min(metrics["actionHeights"]) >= 44, metrics
        page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled", caret="hide")
        assert screenshot_path.exists() and screenshot_path.stat().st_size > 20_000

        altbau_row = page.locator("[data-workbench-row]", has_text="Altbau near U6").first
        altbau_ref = str(altbau_row.get_attribute("data-candidate-ref") or "").strip()
        assert altbau_ref
        altbau_row.locator(".pqx-result-title").click()
        expect(page.locator("[data-pw-title]").first).to_have_text("Altbau near U6")
        selected_url = urllib.parse.urlparse(page.url)
        selected_query = urllib.parse.parse_qs(selected_url.query)
        assert selected_url.path == "/app/shortlist"
        assert selected_query["run_id"] == ["run-42"]
        assert selected_query["candidate"] == [altbau_ref]
        assert page.evaluate("window.history.state?.propertyquarryCandidate || ''") == altbau_ref

        altbau_row.get_by_role("link", name="Open property", exact=True).click()
        page.wait_for_url(re.compile(r".*/app/research/[^?]+.*"), wait_until="commit", timeout=5000)
        expect(page.locator("[data-property-research-detail]")).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("route", ["/app/settings/google", "/app/settings/support", "/app/settings/access", "/app/settings/invitations"])
def test_propertyquarry_mobile_settings_disclosures_keep_one_panel_open(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    route: str,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    context = _new_context(browser, mobile=True, width=390, height=844)
    _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}{route}", wait_until="networkidle")
        assert response is not None and response.ok

        metrics = page.evaluate(
            """() => {
                const settingsDisclosures = Array.from(document.querySelectorAll('.is-settings-surface details.object-disclosure'));
                const accountFolds = Array.from(document.querySelectorAll('details.pqx-mobile-fold'));
                const surfaceMode = settingsDisclosures.length >= 2 ? 'settings' : (accountFolds.length >= 2 ? 'account' : 'missing');
                const disclosures = surfaceMode === 'settings' ? settingsDisclosures : accountFolds;
                const labelFor = (node) => (
                    node?.querySelector('summary strong, summary h2')?.textContent || ''
                ).trim();
                const visibleSummaryTags = disclosures
                    .map((node) => node.querySelector('summary .object-tag'))
                    .filter((node) => node instanceof HTMLElement)
                    .filter((node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    });
                const openBefore = disclosures.filter((node) => node.open);
                const firstTarget = disclosures.find((node) => labelFor(node)) || null;
                const secondTarget = disclosures.find((node) => labelFor(node) && node !== firstTarget) || null;
                firstTarget?.querySelector('summary')?.click();
                secondTarget?.querySelector('summary')?.click();
                const openAfter = disclosures.filter((node) => node.open);
                return {
                    disclosureCount: disclosures.length,
                    surfaceMode,
                    initialOpenCount: openBefore.length,
                    firstLabel: labelFor(firstTarget),
                    secondLabel: labelFor(secondTarget),
                    finalOpenCount: openAfter.length,
                    finalOpenLabel: labelFor(openAfter[0] || null),
                    labels: disclosures.map((node) => labelFor(node)).filter(Boolean),
                    sidebarLabelPresent: disclosures.some((node) => node.classList.contains('object-sidebar-disclosure')),
                    summaryTagVisible: visibleSummaryTags.length > 0,
                };
            }"""
        )
        assert metrics["disclosureCount"] >= 2, metrics
        if metrics["surfaceMode"] == "account":
            assert metrics["initialOpenCount"] <= 1, metrics
        else:
            assert metrics["initialOpenCount"] == 0, metrics
        assert metrics["firstLabel"], metrics
        if route == "/app/settings/google" and metrics["surfaceMode"] == "account":
            assert metrics["firstLabel"] == "Notifications", metrics
        elif route == "/app/settings/google":
            assert metrics["firstLabel"] == "Google sign-in", metrics
        if route == "/app/settings/access" and metrics["surfaceMode"] == "account":
            assert metrics["firstLabel"] == "Notifications", metrics
        elif route == "/app/settings/access":
            assert metrics["firstLabel"] in {"Create an access link", "Live access links"}, metrics
        if route == "/app/settings/invitations":
            assert metrics["firstLabel"] == "Invite another person", metrics
        assert metrics["finalOpenCount"] == 1, metrics
        assert metrics["secondLabel"], metrics
        assert metrics["finalOpenLabel"] == metrics["secondLabel"], metrics
        assert metrics["summaryTagVisible"] is False, metrics
        assert (
            metrics["surfaceMode"] == "account"
            or metrics["sidebarLabelPresent"] is True
            or route in {"/app/settings/access", "/app/settings/invitations"}
        ), metrics
    finally:
        context.close()


def test_propertyquarry_account_and_billing_hide_redundant_top_actions(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    context = _new_context(browser, mobile=False)
    _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/account", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.get_by_role("button", name="Dark mode")).to_be_visible()
        assert page.get_by_role("button", name=re.compile("Browser alerts", re.I)).count() == 0
        assert page.locator(".pqx-top-actions").get_by_role("link", name="Review").count() == 0
        assert page.locator(".pqx-top-actions").get_by_role("link", name="Edit search").count() == 0
        expect(page.locator(".pqx-account-menu")).to_have_count(0)
        expect(page.get_by_role("button", name="Log out")).to_be_visible()
        expect(page.get_by_role("link", name="Billing")).to_be_visible()

        response = page.goto(f"{base_url}/app/billing", wait_until="networkidle")
        assert response is not None
        if response.ok:
            _assert_propertyquarry_billing_redirect_lands_on_account(page)
            assert page.locator(".pqx-top-actions").get_by_role("link", name="Review").count() == 0
            assert page.locator(".pqx-top-actions").get_by_role("link", name="Edit search").count() == 0
            expect(page.locator(".pqx-account-menu")).to_have_count(0)
        else:
            assert response.status == 503
            expect(page.locator("body", has_text="Billing portal unavailable")).to_be_visible()
            expect(page.get_by_role("link", name="Open account")).to_be_visible()
            expect(page.get_by_role("link", name="Open search").first).to_be_visible()
            assert page.locator(".pqx-top-actions").get_by_role("link", name="Review").count() == 0
            assert page.locator(".pqx-top-actions").get_by_role("link", name="Edit search").count() == 0
            expect(page.locator(".pqx-account-menu")).to_have_count(0)
    finally:
        context.close()


def test_propertyquarry_signed_in_surfaces_prefer_verified_external_billing_handoff_in_live_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    monkeypatch.setattr(
        landing_routes,
        "_property_brilliant_directories_billing_handoff",
        lambda allow_verified_direct_handoff=False: {
            "available": True,
            "status": "ready",
            "open_href": "https://billing.propertyquarry.com/account",
        },
    )
    context = _new_context(browser, mobile=False)
    _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/pricing", wait_until="networkidle")
        assert response is not None and response.ok
        pricing_hrefs = page.evaluate(
            """() => Array.from(document.querySelectorAll('a'))
              .filter((node) => ['Open billing', 'Open billing account', 'Billing account', 'Billing'].includes((node.textContent || '').trim()))
              .map((node) => node.getAttribute('href') || '')"""
        )
        assert "https://billing.propertyquarry.com/account" in pricing_hrefs
        assert "/app/account?billing=1#delivery" not in pricing_hrefs
        pricing_billing_links = page.get_by_role("link", name=re.compile("open billing|billing account", re.I))
        assert pricing_billing_links.count() >= 1
        expect(pricing_billing_links.first).to_be_visible()
        assert page.get_by_text("Continue billing sign-in", exact=True).count() == 0

        response = page.goto(f"{base_url}/app/account", wait_until="networkidle")
        assert response is not None and response.ok
        account_hrefs = page.evaluate(
            """() => Array.from(document.querySelectorAll('a'))
              .filter((node) => ['Open billing', 'Open billing account', 'Billing account', 'Billing'].includes((node.textContent || '').trim()))
              .map((node) => node.getAttribute('href') || '')"""
        )
        assert "https://billing.propertyquarry.com/account" in account_hrefs
        assert "/app/account?billing=1#delivery" not in account_hrefs
        account_billing_links = page.get_by_role("link", name=re.compile("open billing|billing account|billing", re.I))
        assert account_billing_links.count() >= 1
        expect(account_billing_links.first).to_be_visible()
        assert page.get_by_text("Continue billing sign-in", exact=True).count() == 0
    finally:
        context.close()


def test_propertyquarry_what_matters_section_renders_as_comboboxes_in_live_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    desktop = _new_context(browser, mobile=False, width=1440, height=1400)
    mobile = _new_context(browser, mobile=True)
    desktop_shot = tmp_path / "propertyquarry-what-matters-desktop.png"
    mobile_shot = tmp_path / "propertyquarry-what-matters-mobile.png"
    try:
        desktop_page = desktop.new_page()
        console_errors: list[str] = []
        desktop_page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        response = desktop_page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        desktop_page.locator('[data-property-step-trigger="children"]').click()
        section = desktop_page.locator('[data-property-what-matters-panel]')
        section.wait_for(state="visible")
        assert section.locator("select").count() >= 8
        assert section.locator('input[type="checkbox"]').count() == 0
        assert " nearby" not in section.inner_text().lower()
        expect(desktop_page.locator('[data-property-field-name="use_stored_feedback_preferences"]')).to_have_count(0)
        expect(desktop_page.locator('[data-property-field-name="preference_person_id"]')).to_have_count(0)
        for field_name in (
            "require_school_evidence",
            "school_stage_preferences",
            "max_distance_to_hardware_store_m",
            "max_distance_to_shopping_center_m",
            "avoid_noise_risk_area",
            "require_high_speed_internet",
        ):
            expect(desktop_page.locator(f'[data-property-field-name="{field_name}"]')).to_be_hidden()
        baugrund_row = section.locator('[data-keyword-priority-row][data-keyword-value="baugrund"]')
        expect(baugrund_row).to_be_hidden()
        desktop_page.locator('[data-property-step-trigger="what"]').click()
        desktop_page.locator('input[name="property_type"][value="land"]').check()
        desktop_page.locator('[data-property-step-trigger="children"]').click()
        expect(baugrund_row).to_be_visible()
        distance_row = section.locator('[data-keyword-priority-row]:has([data-keyword-distance-select])').first
        distance_row.evaluate(
            """
            (node) => {
              const group = node?.closest?.('details[data-what-matters-group]');
              if (group instanceof HTMLDetailsElement) group.open = true;
              node?.scrollIntoView?.({ block: 'center', inline: 'nearest' });
            }
            """
        )
        distance_preference = distance_row.locator('[data-keyword-preference-select]')
        distance_select = distance_row.locator('[data-keyword-distance-select]')
        distance_preference.select_option("nice_to_have")
        expect(distance_select).to_be_enabled()
        assert distance_row.evaluate("node => node.dataset.keywordDistanceEnabled") == "true"
        assert distance_row.evaluate("node => node.closest('[data-what-matters-group]')?.dataset.activeDistanceRows") == "true"
        preference_box = distance_preference.bounding_box()
        distance_box = distance_select.bounding_box()
        assert preference_box and 108 <= preference_box["width"] <= 180
        assert distance_box and 80 <= distance_box["width"] <= 180
        school_parent = section.locator('[data-school-priority-row][data-school-value="volksschule"] [data-school-preference-select]')
        school_detail = section.locator('[data-school-priority-row][data-school-parent-value="volksschule"]').first
        expect(school_detail).to_be_hidden()
        school_parent.select_option("nice_to_have")
        expect(school_detail).to_be_visible()
        assert school_parent.evaluate("node => node.closest('[data-school-priority-row]')?.dataset.schoolFamilyActive") == "true"
        assert school_detail.evaluate("node => node.dataset.schoolParentActive") == "true"
        actionable_console_errors = [
            message for message in console_errors
            if "Cross-Origin-Opener-Policy header has been ignored" not in message
        ]
        assert actionable_console_errors == []
        section.screenshot(path=str(desktop_shot))
        assert desktop_shot.exists()

        mobile_page = mobile.new_page()
        response = mobile_page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        mobile_page.locator('[data-property-step-trigger="children"]').click()
        mobile_section = mobile_page.locator('[data-property-what-matters-panel]')
        mobile_section.wait_for(state="visible")
        assert mobile_section.locator("select").count() >= 8
        assert mobile_section.locator('input[type="checkbox"]').count() == 0
        assert " nearby" not in mobile_section.inner_text().lower()
        assert mobile_section.get_by_text("Custom priorities", exact=True).count() == 0
        assert mobile_section.get_by_text("Check listing quality", exact=True).count() == 0
        save_button_count = mobile_section.locator('[data-pqx-save-what-matters]').count()
        load_button_count = mobile_section.locator('[data-pqx-load-what-matters]').count()
        assert save_button_count >= 1
        assert load_button_count >= 1
        assert any(
            locator.is_visible()
            for locator in (
                mobile_section.locator('[data-pqx-what-matters-mobile-tools]'),
                mobile_section.locator('.pqx-actions'),
            )
        )
        mobile_metrics = mobile_section.evaluate(
            """
            (panel) => {
              const groups = Array.from(panel.querySelectorAll('[data-what-matters-group]'));
              const initialOpenGroupCount = groups.filter((node) => node.open).length;
              if (!initialOpenGroupCount && groups[0]) {
                groups[0].setAttribute('open', '');
              }
              const visibleRows = Array.from(
                panel.querySelectorAll(
                  '[data-keyword-priority-row], [data-school-priority-row], .pqx-what-matters-head, .pqx-what-matters-group-summary'
                )
              ).filter((node) => node.offsetParent !== null);
              const panelRect = panel.getBoundingClientRect();
              const lastBottom = visibleRows.reduce((bottom, node) => {
                const rect = node.getBoundingClientRect();
                return Math.max(bottom, rect.bottom);
              }, panelRect.top);
              const rowWidths = visibleRows.map((node) => {
                const rect = node.getBoundingClientRect();
                return {
                  width: rect.width,
                  scrollWidth: node.scrollWidth,
                };
              });
              const firstList = panel.querySelector('[data-what-matters-group][open] .pqx-pref-list');
              const firstListStyle = firstList ? window.getComputedStyle(firstList) : null;
              return {
                panelHeight: panelRect.height,
                panelWidth: panelRect.width,
                panelScrollWidth: panel.scrollWidth,
                bottomGap: panelRect.bottom - lastBottom,
                rowWidths,
                initialOpenGroupCount,
                firstListOverflowY: firstListStyle ? firstListStyle.overflowY : '',
                firstListScrolls: firstList ? firstList.scrollHeight > firstList.clientHeight + 2 : false,
              };
            }
            """
        )
        assert float(mobile_metrics["panelHeight"]) <= 1600.0, mobile_metrics
        assert float(mobile_metrics["panelScrollWidth"]) <= float(mobile_metrics["panelWidth"]) + 1.0, mobile_metrics
        assert int(mobile_metrics["initialOpenGroupCount"]) <= 1, mobile_metrics
        assert mobile_metrics["firstListOverflowY"] in {"visible", "auto", "scroll"}, mobile_metrics
        if mobile_metrics["firstListOverflowY"] in {"auto", "scroll"}:
            assert mobile_metrics["firstListScrolls"] is True, mobile_metrics
        else:
            assert mobile_metrics["firstListScrolls"] is False, mobile_metrics
        for row_metric in mobile_metrics["rowWidths"]:
            assert float(row_metric["scrollWidth"]) <= float(row_metric["width"]) + 1.0, row_metric
        mobile_section.scroll_into_view_if_needed()
        mobile_page.screenshot(path=str(mobile_shot))
        assert mobile_shot.exists()
    finally:
        desktop.close()
        mobile.close()


def test_propertyquarry_search_wizard_steps_replace_visible_controls_without_accumulating(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1360, height=1000)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        expect(page.locator('[data-property-start-top]')).to_be_visible()
        expect(page.locator('[data-property-step-nav]')).to_be_visible()
        expected_fields = {
            "search": "country_code",
            "what": "property_type",
            "children": "keywords",
            "reachability": "enable_commute_research",
            "research": None,
            "providers": "selected_platforms",
        }
        for step, field_name in expected_fields.items():
            page.locator(f'[data-property-step-trigger="{step}"]').click()
            page.wait_for_function(
                """(step) => document.querySelector('[data-console-form-variant="property_search"]')?.dataset.propertyActiveStep === step""",
                arg=step,
            )
            visible_steps = page.evaluate(
                """
                () => Array.from(document.querySelectorAll('.pqx-field[data-property-field-step]'))
                  .filter((node) => node.offsetParent !== null && !node.hidden)
                  .map((node) => node.getAttribute('data-property-field-step'))
                  .filter(Boolean)
                """
            )
            assert visible_steps, step
            assert set(visible_steps) == {step}, {"clicked": step, "visible_steps": visible_steps}
            if field_name:
                expect(page.locator(f'[data-property-field-name="{field_name}"]')).to_be_visible()
            assert page.locator(".pqx-workflow-step.active").count() == 1
            nav_box = page.locator('[data-property-step-nav]').bounding_box()
            assert nav_box is not None
            assert nav_box["y"] >= 0
            assert nav_box["y"] < 220
        page.locator('[data-property-step-trigger="search"]').click()
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.locator('[data-property-step-next]').click()
        page.wait_for_function(
            """() => document.querySelector('[data-console-form-variant="property_search"]')?.dataset.propertyActiveStep === 'what'"""
        )
        nav_box = page.locator('[data-property-step-nav]').bounding_box()
        assert nav_box is not None
        assert nav_box["y"] >= 0
        assert nav_box["y"] < 220
    finally:
        context.close()


def test_propertyquarry_search_wizard_step_navigation_is_keyboard_operable(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1360, height=1000)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")

        reached_step = ""
        for _ in range(50):
            page.keyboard.press("Tab")
            reached_step = str(
                page.evaluate(
                    "() => document.activeElement?.getAttribute('data-property-step-trigger') || ''"
                )
            )
            if reached_step == "what":
                break
        assert reached_step == "what"

        focused_button = page.locator('[data-property-step-trigger="what"]')
        focus_style = focused_button.evaluate(
            """
            (node) => {
              const style = window.getComputedStyle(node);
              return {
                outlineStyle: style.outlineStyle,
                outlineWidth: style.outlineWidth,
                boxShadow: style.boxShadow,
              };
            }
            """
        )
        assert focus_style["outlineStyle"] != "none" or focus_style["boxShadow"] != "none"

        page.keyboard.press("Enter")
        page.wait_for_function(
            """() => document.querySelector('[data-console-form-variant="property_search"]')?.dataset.propertyActiveStep === 'what'"""
        )
        expect(page.locator('[data-property-step-trigger="what"]')).to_have_attribute("aria-current", "step")
        expect(page.locator('[data-property-step-trigger="search"]')).not_to_have_attribute("aria-current", "step")
        expect(page.locator('[data-property-field-name="property_type"]')).to_be_visible()

        page.locator('[data-property-step-next]').focus()
        page.keyboard.press("Space")
        page.wait_for_function(
            """() => document.querySelector('[data-console-form-variant="property_search"]')?.dataset.propertyActiveStep === 'children'"""
        )
        expect(page.locator('[data-property-step-trigger="children"]')).to_have_attribute("aria-current", "step")
        expect(page.locator('[data-property-field-name="keywords"]')).to_be_visible()
    finally:
        context.close()


def test_propertyquarry_what_matters_distance_comboboxes_expand_without_clipping(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    desktop = _new_context(browser, mobile=False, width=1120, height=1200)
    mobile = _new_context(browser, mobile=True, width=390, height=844)
    desktop_shot = tmp_path / "propertyquarry-what-matters-distance-desktop.png"
    mobile_shot = tmp_path / "propertyquarry-what-matters-distance-mobile.png"

    def _assert_distance_rows_fit(page: Page, screenshot_path: Path) -> None:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-property-step-trigger="children"]').click()
        page.wait_for_function(
            "() => document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'children'"
        )
        for field_name in (
            "max_distance_to_market_m",
            "max_distance_to_hardware_store_m",
            "max_distance_to_shopping_center_m",
            "max_distance_to_theatre_m",
            "avoid_flood_risk_area",
        ):
            expect(page.locator(f'[data-property-field-name="{field_name}"]')).to_be_hidden()
        if int(page.evaluate("window.innerWidth")) <= 760:
            playground_row = page.locator('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
            playground_row.evaluate(
                """
                (node) => {
                  node.closest('details[data-what-matters-group]')?.setAttribute('open', '');
                  node?.scrollIntoView?.({ block: 'center', inline: 'nearest' });
                }
                """
            )
            before_anchor = playground_row.evaluate(
                """
                (node) => ({
                  top: node.getBoundingClientRect().top,
                  scrollY: window.scrollY,
                  documentTop: node.getBoundingClientRect().top + window.scrollY,
                })
                """
            )
            playground_row.locator("[data-keyword-preference-select]").select_option("nice_to_have")
            page.wait_for_function(
                """
                () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
                  ?.getAttribute('data-keyword-distance-enabled') === 'true'
                """
            )
            page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            after_anchor = playground_row.evaluate(
                """
                (node) => ({
                  top: node.getBoundingClientRect().top,
                  scrollY: window.scrollY,
                  documentTop: node.getBoundingClientRect().top + window.scrollY,
                })
                """
            )
            assert abs(float(after_anchor["documentTop"]) - float(before_anchor["documentTop"])) <= 1.0, {
                "before": before_anchor,
                "after": after_anchor,
            }
            assert abs(float(after_anchor["scrollY"]) - float(before_anchor["scrollY"])) <= 48.0, {
                "before": before_anchor,
                "after": after_anchor,
            }
        keyword_states = (
            ("playground nearby", "nice_to_have"),
            ("Baumarkt nearby", "strong_wish"),
            ("shopping center nearby", "strong_wish"),
            ("flaniermeile nearby", "strong_wish"),
            ("theatre nearby", "strong_wish"),
        )
        for keyword, state in keyword_states:
            row = page.locator(f'[data-keyword-priority-row][data-keyword-value="{keyword}"]')
            row.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
            row.locator("[data-keyword-preference-select]").select_option(state)
            expect(row.locator("[data-keyword-distance-select]")).to_be_enabled()
            expect(row).to_have_attribute("data-preference-state", state)
        school_parent = page.locator('[data-school-priority-row][data-school-value="volksschule"]')
        school_parent.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
        school_parent.locator("[data-school-preference-select]").select_option("strong_wish")
        expect(school_parent).to_have_attribute("data-preference-state", "strong_wish")
        school_child = page.locator('[data-school-priority-row][data-school-value="ganztags_volksschule"]')
        expect(school_child).to_be_visible()
        expect(school_child).to_have_attribute("data-school-parent-active", "true")
        expect(school_parent).to_have_attribute("data-school-family-active", "true")
        school_distance_rows = (
            ("kindergarten", "nice_to_have", ""),
            ("ganztags_volksschule", "strong_wish", "volksschule"),
            ("halbtags_volksschule", "nice_to_have", "volksschule"),
        )
        kindergarten_parent = page.locator('[data-school-priority-row][data-school-value="kindergarten"]')
        kindergarten_parent.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
        kindergarten_parent.locator("[data-school-preference-select]").select_option("strong_wish")
        for value, state, parent_value in school_distance_rows:
            row = page.locator(f'[data-school-priority-row][data-school-value="{value}"]')
            row.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
            expect(row).to_be_visible()
            if parent_value:
                expect(row).to_have_attribute("data-school-parent-active", "true")
            row.locator("[data-school-preference-select]").select_option(state)
            expect(row.locator("[data-school-distance-select]")).to_be_enabled()
            expect(row).to_have_attribute("data-school-distance-enabled", "true")
            distance_field = row.locator("[data-school-distance-select]").get_attribute("data-distance-field")
            assert distance_field == f"max_distance_to_{value}_m"
        section = page.locator('[data-what-matters-group="daily_life"]')
        expect(section).to_have_attribute("data-active-distance-rows", "true")
        active_distance_row = page.locator('[data-keyword-priority-row][data-keyword-value="Baumarkt nearby"]')
        active_distance_row.scroll_into_view_if_needed()
        section.scroll_into_view_if_needed()
        section.screenshot(path=str(screenshot_path))
        assert screenshot_path.exists()
        group_overflow = page.evaluate(
            """
            () => {
              const group = document.querySelector('[data-what-matters-group="daily_life"]');
              const list = group?.querySelector('.pqx-pref-list');
              const groupRect = group?.getBoundingClientRect();
              const listRect = list?.getBoundingClientRect();
              const offenders = Array.from(group?.querySelectorAll('*') || [])
                .map((node) => {
                  const rect = node.getBoundingClientRect();
                  return {
                    tag: node.tagName,
                    cls: node.className || '',
                    attr: node.getAttribute('data-keyword-value') || node.getAttribute('data-school-value') || '',
                    width: rect.width,
                    rightOver: groupRect ? rect.right - groupRect.right : 0,
                    scrollWidth: node.scrollWidth || 0,
                    clientWidth: node.clientWidth || 0,
                  };
                })
                .filter((item) => item.rightOver > 1 || item.scrollWidth > item.clientWidth + 1)
                .sort((a, b) => Math.max(b.rightOver, b.scrollWidth - b.clientWidth) - Math.max(a.rightOver, a.scrollWidth - a.clientWidth))
                .slice(0, 8);
              return {
                groupWidth: groupRect ? groupRect.width : 0,
                groupScrollWidth: group ? group.scrollWidth : 0,
                listWidth: listRect ? listRect.width : 0,
                listScrollWidth: list ? list.scrollWidth : 0,
                listComputed: list ? {
                  display: getComputedStyle(list).display,
                  overflowX: getComputedStyle(list).overflowX,
                  paddingLeft: getComputedStyle(list).paddingLeft,
                  paddingRight: getComputedStyle(list).paddingRight,
                  boxSizing: getComputedStyle(list).boxSizing,
                  width: getComputedStyle(list).width,
                } : {},
                children: Array.from(list?.children || []).slice(0, 8).map((node) => {
                  const rect = node.getBoundingClientRect();
                  const style = getComputedStyle(node);
                  return {
                    tag: node.tagName,
                    cls: node.className || '',
                    attr: node.getAttribute('data-keyword-value') || node.getAttribute('data-school-value') || '',
                    width: rect.width,
                    scrollWidth: node.scrollWidth || 0,
                    clientWidth: node.clientWidth || 0,
                    marginLeft: style.marginLeft,
                    marginRight: style.marginRight,
                    boxSizing: style.boxSizing,
                    display: style.display,
                    paddingLeft: style.paddingLeft,
                    paddingRight: style.paddingRight,
                  };
                }),
                offenders,
              };
            }
            """
        )
        assert float(group_overflow["groupScrollWidth"]) <= float(group_overflow["groupWidth"]) + 1.0, json.dumps(group_overflow)
        assert float(group_overflow["listScrollWidth"]) <= float(group_overflow["listWidth"]) + 1.0, json.dumps(group_overflow)
        rows = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('[data-keyword-priority-row][data-keyword-distance-enabled="true"]'))
              .filter((row) => row.offsetParent !== null)
              .map((row) => {
                const rowRect = row.getBoundingClientRect();
                return {
                  value: row.getAttribute('data-keyword-value') || '',
                  rowWidth: rowRect.width,
                  rowScrollWidth: row.scrollWidth,
                  controls: Array.from(row.querySelectorAll('select')).map((select) => {
                    const rect = select.getBoundingClientRect();
                    return {
                      name: select.getAttribute('name') || '',
                      width: rect.width,
                      left: rect.left - rowRect.left,
                      right: rowRect.right - rect.right,
                    };
                  }),
                };
              })
            """
        )
        assert len(rows) >= 4
        inner_width = int(page.evaluate("window.innerWidth"))
        group_width = float(section.bounding_box()["width"] or 0)
        if inner_width >= 900:
            assert group_width >= 680.0
        for row in rows:
            assert float(row["rowScrollWidth"]) <= float(row["rowWidth"]) + 1.0, row
            if inner_width >= 900:
                assert float(row["rowWidth"]) >= 320.0, row
            for control in row["controls"]:
                assert float(control["width"]) >= 90.0, row
                if inner_width >= 900:
                    assert float(control["width"]) <= 126.0, row
                assert float(control["left"]) >= -1.0, row
                assert float(control["right"]) >= -1.0, row
        school_rows = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('[data-school-priority-row][data-school-distance-enabled="true"]'))
              .filter((row) => row.offsetParent !== null)
              .map((row) => {
                const rowRect = row.getBoundingClientRect();
                const preference = row.querySelector('[data-school-preference-select]');
                const distance = row.querySelector('[data-school-distance-select]');
                const preferenceRect = preference?.getBoundingClientRect();
                const distanceRect = distance?.getBoundingClientRect();
                return {
                  value: row.getAttribute('data-school-value') || '',
                  rowWidth: rowRect.width,
                  rowScrollWidth: row.scrollWidth,
                  preferenceValue: preference?.value || '',
                  distanceDisabled: Boolean(distance?.disabled),
                  distanceField: distance?.getAttribute('data-distance-field') || '',
                  preferenceWidth: preferenceRect ? preferenceRect.width : 0,
                  distanceWidth: distanceRect ? distanceRect.width : 0,
                  preferenceLeft: preferenceRect ? preferenceRect.left - rowRect.left : -999,
                  distanceRight: distanceRect ? rowRect.right - distanceRect.right : -999,
                };
              })
            """
        )
        school_by_value = {row["value"]: row for row in school_rows}
        for value in ("kindergarten", "ganztags_volksschule", "halbtags_volksschule"):
            row = school_by_value.get(value)
            assert row, school_rows
            assert row["distanceDisabled"] is False, row
            assert row["distanceField"] == f"max_distance_to_{value}_m", row
            assert float(row["rowScrollWidth"]) <= float(row["rowWidth"]) + 1.0, row
            assert float(row["preferenceWidth"]) >= 90.0, row
            assert float(row["distanceWidth"]) >= 90.0, row
            if inner_width >= 900:
                assert float(row["preferenceWidth"]) <= 126.0, row
                assert float(row["distanceWidth"]) <= 126.0, row
            assert float(row["preferenceLeft"]) >= -1.0, row
            assert float(row["distanceRight"]) >= -1.0, row
        playground_clip = page.evaluate(
            """
            () => {
              const row = document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]');
              const preference = row?.querySelector('[data-keyword-preference-select]');
              const select = row?.querySelector('[data-keyword-distance-select]');
              const list = row?.closest('.pqx-pref-list');
              row?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
              const rowRect = row?.getBoundingClientRect();
              const preferenceRect = preference?.getBoundingClientRect();
              const selectRect = select?.getBoundingClientRect();
              const listRect = list?.getBoundingClientRect();
              return {
                rowTop: rowRect ? rowRect.top : 0,
                rowBottom: rowRect ? rowRect.bottom : 0,
                preferenceWidth: preferenceRect ? preferenceRect.width : 0,
                preferenceRight: preferenceRect && rowRect ? rowRect.right - preferenceRect.right : -999,
                preferenceValue: preference?.value || '',
                selectWidth: selectRect ? selectRect.width : 0,
                selectTop: selectRect ? selectRect.top : 0,
                selectBottom: selectRect ? selectRect.bottom : 0,
                selectRight: selectRect && rowRect ? rowRect.right - selectRect.right : -999,
                listTop: listRect ? listRect.top : 0,
                listBottom: listRect ? listRect.bottom : 0,
                viewportWidth: window.innerWidth,
              };
            }
            """
        )
        assert playground_clip["preferenceValue"] == "nice_to_have", playground_clip
        assert playground_clip["selectTop"] >= playground_clip["rowTop"] - 1, playground_clip
        assert playground_clip["selectBottom"] <= playground_clip["rowBottom"] + 1, playground_clip
        assert playground_clip["selectTop"] >= playground_clip["listTop"] - 1, playground_clip
        assert playground_clip["selectBottom"] <= playground_clip["listBottom"] + 1, playground_clip
        if int(playground_clip["viewportWidth"]) >= 900:
            assert 90 <= float(playground_clip["preferenceWidth"]) <= 126, playground_clip
            assert 90 <= float(playground_clip["selectWidth"]) <= 126, playground_clip
            assert float(playground_clip["preferenceRight"]) >= -1.0, playground_clip
            assert float(playground_clip["selectRight"]) >= -1.0, playground_clip
        if int(page.evaluate("window.innerWidth")) <= 760:
            playground_viewport = page.evaluate(
                """
                () => {
                  const row = document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]');
                  const preference = row?.querySelector('[data-keyword-preference-select]');
                  const distance = row?.querySelector('[data-keyword-distance-select]');
                  const group = row?.closest('[data-what-matters-group]');
                  row?.scrollIntoView({ block: 'center', inline: 'nearest' });
                  const rowRect = row?.getBoundingClientRect();
                  const distanceRect = distance?.getBoundingClientRect();
                  return {
                    preferenceValue: preference?.value || '',
                    distanceDisabled: Boolean(distance?.disabled),
                    distanceHeight: distanceRect ? distanceRect.height : 0,
                    distanceLeft: distanceRect ? distanceRect.left : -999,
                    distanceRight: distanceRect ? distanceRect.right : 999,
                    distanceTop: distanceRect ? distanceRect.top : -999,
                    distanceBottom: distanceRect ? distanceRect.bottom : 999,
                    rowTop: rowRect ? rowRect.top : -999,
                    rowBottom: rowRect ? rowRect.bottom : 999,
                    viewportWidth: window.innerWidth,
                    viewportHeight: window.innerHeight,
                    bottomSafeTop: window.innerHeight,
                    groupOpen: Boolean(group?.open),
                    groupActive: group?.getAttribute('data-active-distance-rows') || '',
                    groupMobileActive: group?.getAttribute('data-mobile-distance-control-active') || '',
                  };
                }
                """
            )
            assert playground_viewport["preferenceValue"] == "nice_to_have", playground_viewport
            assert playground_viewport["distanceDisabled"] is False, playground_viewport
            assert float(playground_viewport["distanceHeight"]) >= 44.0, playground_viewport
            assert float(playground_viewport["distanceLeft"]) >= -1.0, playground_viewport
            assert float(playground_viewport["distanceRight"]) <= float(playground_viewport["viewportWidth"]) + 1.0, playground_viewport
            assert float(playground_viewport["distanceTop"]) >= 0.0, playground_viewport
            assert float(playground_viewport["distanceBottom"]) <= float(playground_viewport["bottomSafeTop"]) - 4.0, playground_viewport
            assert playground_viewport["groupOpen"] is True, playground_viewport
            assert playground_viewport["groupActive"] == "true", playground_viewport
            assert playground_viewport["groupMobileActive"] == "true", playground_viewport

    try:
        _assert_distance_rows_fit(desktop.new_page(), desktop_shot)
        _assert_distance_rows_fit(mobile.new_page(), mobile_shot)
    finally:
        desktop.close()
        mobile.close()


def test_propertyquarry_mobile_what_matters_select_changes_keep_current_group_stable(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        page.locator('[data-property-step-trigger="children"]').click()
        page.wait_for_function(
            "() => document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'children'"
        )

        capture_keyword_state = """
            (keywordValue) => {
              const row = document.querySelector(`[data-keyword-priority-row][data-keyword-value="${keywordValue}"]`);
              const group = row?.closest('details[data-what-matters-group]');
              const home = document.querySelector('details[data-what-matters-group="home_basics"]');
              const daily = document.querySelector('details[data-what-matters-group="daily_life"]');
              const risk = document.querySelector('details[data-what-matters-group="risk_evidence"]');
              const form = document.querySelector('[data-console-form-variant="property_search"]');
              const rect = row?.getBoundingClientRect();
              return {
                activeStep: String(form?.dataset.propertyActiveStep || ''),
                groupKey: String(group?.getAttribute('data-what-matters-group') || ''),
                groupOpen: Boolean(group?.open),
                homeOpen: Boolean(home?.open),
                dailyOpen: Boolean(daily?.open),
                riskOpen: Boolean(risk?.open),
                rowTop: rect ? rect.top : 0,
                scrollY: Number(window.scrollY || window.pageYOffset || 0),
              };
            }
        """
        capture_school_state = """
            (schoolValue) => {
              const row = document.querySelector(`[data-school-priority-row][data-school-value="${schoolValue}"]`);
              const group = row?.closest('details[data-what-matters-group]');
              const form = document.querySelector('[data-console-form-variant="property_search"]');
              const rect = row?.getBoundingClientRect();
              return {
                activeStep: String(form?.dataset.propertyActiveStep || ''),
                groupKey: String(group?.getAttribute('data-what-matters-group') || ''),
                groupOpen: Boolean(group?.open),
                rowTop: rect ? rect.top : 0,
                scrollY: Number(window.scrollY || window.pageYOffset || 0),
              };
            }
        """
        open_row_group = """
            (node) => {
              const group = node?.closest?.('details[data-what-matters-group]');
              if (!(group instanceof HTMLDetailsElement)) return;
              group.open = true;
              node?.scrollIntoView?.({ block: 'center', inline: 'nearest' });
            }
        """

        playground_row = page.locator('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        playground_row.evaluate(open_row_group)
        expect(playground_row).to_be_visible()
        before_keyword = page.evaluate(capture_keyword_state, "playground nearby")
        playground_preference = playground_row.locator("[data-keyword-preference-select]")
        playground_preference.select_option("nice_to_have")
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
              ?.getAttribute('data-keyword-distance-enabled') === 'true'
            """
        )
        _wait_for_what_matters_interaction_stabilization(page)
        after_keyword = page.evaluate(capture_keyword_state, "playground nearby")
        assert after_keyword["activeStep"] == "children", after_keyword
        assert after_keyword["groupKey"] == "daily_life", after_keyword
        assert after_keyword["groupOpen"] is True, after_keyword
        assert abs(float(after_keyword["rowTop"]) - float(before_keyword["rowTop"])) <= 72.0, {
            "before": before_keyword,
            "after": after_keyword,
        }
        assert abs(float(after_keyword["scrollY"]) - float(before_keyword["scrollY"])) <= 72.0, {
            "before": before_keyword,
            "after": after_keyword,
        }
        playground_distance = playground_row.locator("[data-keyword-distance-select]")
        playground_distance.select_option("500")
        expect(playground_distance).to_have_value("500")
        _wait_for_what_matters_interaction_stabilization(page)
        after_keyword_distance = page.evaluate(capture_keyword_state, "playground nearby")
        assert after_keyword_distance["activeStep"] == "children", after_keyword_distance
        assert after_keyword_distance["groupKey"] == "daily_life", after_keyword_distance
        assert after_keyword_distance["groupOpen"] is True, after_keyword_distance
        assert abs(float(after_keyword_distance["rowTop"]) - float(after_keyword["rowTop"])) <= 72.0, {
            "before": after_keyword,
            "after": after_keyword_distance,
        }
        assert abs(float(after_keyword_distance["scrollY"]) - float(after_keyword["scrollY"])) <= 72.0, {
            "before": after_keyword,
            "after": after_keyword_distance,
        }

        kindergarten_row = page.locator('[data-school-priority-row][data-school-value="kindergarten"]')
        kindergarten_row.evaluate(open_row_group)
        expect(kindergarten_row).to_be_visible()
        before_school = page.evaluate(capture_school_state, "kindergarten")
        kindergarten_preference = kindergarten_row.locator("[data-school-preference-select]")
        kindergarten_preference.select_option("strong_wish")
        page.wait_for_function(
            """
            () => document.querySelector('[data-school-priority-row][data-school-value="kindergarten"]')
              ?.getAttribute('data-school-distance-enabled') === 'true'
            """
        )
        _wait_for_what_matters_interaction_stabilization(page)
        after_school = page.evaluate(capture_school_state, "kindergarten")
        assert after_school["activeStep"] == "children", after_school
        assert after_school["groupKey"] == before_school["groupKey"], {
            "before": before_school,
            "after": after_school,
        }
        assert after_school["groupOpen"] is True, after_school
        assert abs(float(after_school["rowTop"]) - float(before_school["rowTop"])) <= 72.0, {
            "before": before_school,
            "after": after_school,
        }
        assert abs(float(after_school["scrollY"]) - float(before_school["scrollY"])) <= 72.0, {
            "before": before_school,
            "after": after_school,
        }
        kindergarten_distance = kindergarten_row.locator("[data-school-distance-select]")
        kindergarten_distance.select_option("500")
        expect(kindergarten_distance).to_have_value("500")
        _wait_for_what_matters_interaction_stabilization(page)
        after_school_distance = page.evaluate(capture_school_state, "kindergarten")
        assert after_school_distance["activeStep"] == "children", after_school_distance
        assert after_school_distance["groupKey"] == before_school["groupKey"], {
            "before": before_school,
            "after": after_school_distance,
        }
        assert after_school_distance["groupOpen"] is True, after_school_distance
        assert abs(float(after_school_distance["rowTop"]) - float(after_school["rowTop"])) <= 72.0, {
            "before": after_school,
            "after": after_school_distance,
        }
        assert abs(float(after_school_distance["scrollY"]) - float(after_school["scrollY"])) <= 72.0, {
            "before": after_school,
            "after": after_school_distance,
        }

        parking_row = page.locator('[data-keyword-priority-row][data-keyword-value="parking pressure check"]')
        parking_group = parking_row.locator("xpath=ancestor::details[@data-what-matters-group]")
        parking_row.evaluate(open_row_group)
        expect(parking_group).to_have_attribute("open", "")
        expect(parking_row).to_be_visible()
        before_parking = page.evaluate(capture_keyword_state, "parking pressure check")
        assert before_parking["riskOpen"] is True, before_parking
        parking_preference = parking_row.locator("[data-keyword-preference-select]")
        parking_preference.select_option("low")
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="parking pressure check"]')
              ?.getAttribute('data-preference-state') === 'low'
            """
        )
        _wait_for_what_matters_interaction_stabilization(page)
        after_parking = page.evaluate(capture_keyword_state, "parking pressure check")
        assert after_parking["activeStep"] == "children", after_parking
        assert after_parking["groupKey"] == "risk_evidence", after_parking
        assert after_parking["groupOpen"] is True, after_parking
        assert after_parking["homeOpen"] is False, after_parking
        assert after_parking["dailyOpen"] is False, after_parking
        assert after_parking["riskOpen"] is True, after_parking
        assert abs(float(after_parking["rowTop"]) - float(before_parking["rowTop"])) <= 72.0, {
            "before": before_parking,
            "after": after_parking,
        }
    finally:
        context.close()


def test_propertyquarry_mobile_stale_what_matters_restore_does_not_override_new_scroll(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        page.locator('[data-property-step-trigger="children"]').click()
        page.wait_for_function(
            "() => document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'children'"
        )

        playground_row = page.locator('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        playground_row.evaluate(
            """
            (node) => {
              const group = node?.closest?.('details[data-what-matters-group]');
              if (group instanceof HTMLDetailsElement) group.open = true;
              node?.scrollIntoView?.({ block: 'center', inline: 'nearest' });
            }
            """
        )
        playground_row.locator("[data-keyword-preference-select]").select_option("nice_to_have")
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
              ?.getAttribute('data-keyword-distance-enabled') === 'true'
            """
        )
        page.wait_for_timeout(120)

        state = page.evaluate(
            """
            () => new Promise((resolve) => {
              const form = document.querySelector('[data-console-form-variant="property_search"]');
              const playground = document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]');
              const preference = playground?.querySelector('[data-keyword-preference-select]');
              const distance = playground?.querySelector('[data-keyword-distance-select]');
              const kindergarten = document.querySelector('[data-school-priority-row][data-school-value="kindergarten"]');
              const group = kindergarten?.closest?.('details[data-what-matters-group]');
              const panel = kindergarten?.closest?.('[data-property-what-matters-panel]');
              const owner = panel?.querySelector?.('.pqx-advanced-panel-grid');
              if (
                !(form instanceof HTMLFormElement)
                || !(preference instanceof HTMLSelectElement)
                || !(distance instanceof HTMLSelectElement)
                || !(kindergarten instanceof HTMLElement)
                || !(group instanceof HTMLDetailsElement)
                || !(owner instanceof HTMLElement)
              ) {
                resolve({ error: 'missing_test_target' });
                return;
              }
              const capture = () => {
                const rect = kindergarten.getBoundingClientRect();
                return {
                  activeStep: String(form.dataset.propertyActiveStep || ''),
                  groupOpen: group.open,
                  rowTop: Number(rect.top || 0),
                  windowScrollY: Number(window.scrollY || window.pageYOffset || 0),
                  ownerScrollTop: Number(owner.scrollTop || 0),
                  preferenceFocused: document.activeElement === preference,
                };
              };
              group.open = true;
              distance.value = '2000';
              distance.dispatchEvent(new Event('change', { bubbles: true }));
              const restoreBaseline = capture();
              preference.focus({ preventScroll: true });
              window.dispatchEvent(new WheelEvent('wheel', { deltaY: 240 }));
              const intentBaseline = capture();
              window.setTimeout(() => {
                kindergarten.scrollIntoView({ block: 'center', inline: 'nearest' });
                const before = capture();
                window.setTimeout(() => resolve({ restoreBaseline, intentBaseline, before, after: capture() }), 180);
              }, 48);
            })
            """
        )
        assert state.get("error") is None, state
        restore_baseline = state["restoreBaseline"]
        intent_baseline = state["intentBaseline"]
        before = state["before"]
        after = state["after"]
        assert abs(float(intent_baseline["windowScrollY"]) - float(restore_baseline["windowScrollY"])) <= 2.0, state
        assert abs(float(intent_baseline["ownerScrollTop"]) - float(restore_baseline["ownerScrollTop"])) <= 2.0, state
        assert (
            abs(float(before["windowScrollY"]) - float(restore_baseline["windowScrollY"])) > 20.0
            or abs(float(before["ownerScrollTop"]) - float(restore_baseline["ownerScrollTop"])) > 20.0
        ), state
        assert after["activeStep"] == "children", state
        assert after["groupOpen"] is True, state
        assert after["preferenceFocused"] is True, state
        assert abs(float(after["rowTop"]) - float(before["rowTop"])) <= 2.0, state
        assert abs(float(after["windowScrollY"]) - float(before["windowScrollY"])) <= 2.0, state
        assert abs(float(after["ownerScrollTop"]) - float(before["ownerScrollTop"])) <= 2.0, state
    finally:
        context.close()


def test_propertyquarry_mobile_repeated_what_matters_combo_changes_keep_one_stable_group(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        page.locator('[data-property-step-trigger="children"]').click()
        page.wait_for_function(
            "() => document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'children'"
        )

        capture_state = """
            (selector) => {
              const row = document.querySelector(selector);
              const group = row?.closest('details[data-what-matters-group]');
              const home = document.querySelector('details[data-what-matters-group="home_basics"]');
              const daily = document.querySelector('details[data-what-matters-group="daily_life"]');
              const risk = document.querySelector('details[data-what-matters-group="risk_evidence"]');
              const form = document.querySelector('[data-console-form-variant="property_search"]');
              const rect = row?.getBoundingClientRect();
              return {
                activeStep: String(form?.dataset.propertyActiveStep || ''),
                groupKey: String(group?.getAttribute('data-what-matters-group') || ''),
                groupOpen: Boolean(group?.open),
                homeOpen: Boolean(home?.open),
                dailyOpen: Boolean(daily?.open),
                riskOpen: Boolean(risk?.open),
                rowTop: rect ? rect.top : 0,
                scrollY: Number(window.scrollY || window.pageYOffset || 0),
              };
            }
        """
        open_row_group = """
            (node) => {
              const group = node?.closest?.('details[data-what-matters-group]');
              if (!(group instanceof HTMLDetailsElement)) return;
              group.open = true;
              node?.scrollIntoView?.({ block: 'center', inline: 'nearest' });
            }
        """

        def _assert_daily_group_stable(before_state: dict[str, object], after_state: dict[str, object], tolerance: float = 84.0) -> None:
            assert after_state["activeStep"] == "children", after_state
            assert after_state["groupKey"] == "daily_life", after_state
            assert after_state["groupOpen"] is True, after_state
            assert after_state["dailyOpen"] is True, after_state
            assert after_state["homeOpen"] is False, after_state
            assert after_state["riskOpen"] is False, after_state
            assert abs(float(after_state["rowTop"]) - float(before_state["rowTop"])) <= tolerance, {
                "before": before_state,
                "after": after_state,
            }
            assert abs(float(after_state["scrollY"]) - float(before_state["scrollY"])) <= tolerance, {
                "before": before_state,
                "after": after_state,
            }

        playground_row = page.locator('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        playground_row.evaluate(open_row_group)
        expect(playground_row).to_be_visible()

        before_playground = page.evaluate(capture_state, '[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        playground_preference = playground_row.locator("[data-keyword-preference-select]")
        playground_distance = playground_row.locator("[data-keyword-distance-select]")

        playground_preference.select_option("nice_to_have")
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
              ?.getAttribute('data-keyword-distance-enabled') === 'true'
            """
        )
        after_playground_pref_1 = page.evaluate(capture_state, '[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        _assert_daily_group_stable(before_playground, after_playground_pref_1)

        playground_preference.select_option("strong_wish")
        after_playground_pref_2 = page.evaluate(capture_state, '[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        _assert_daily_group_stable(after_playground_pref_1, after_playground_pref_2)

        playground_distance.select_option("2000")
        expect(playground_distance).to_have_value("2000")
        after_playground_distance = page.evaluate(capture_state, '[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        _assert_daily_group_stable(after_playground_pref_2, after_playground_distance)

        kindergarten_row = page.locator('[data-school-priority-row][data-school-value="kindergarten"]')
        kindergarten_row.scroll_into_view_if_needed()
        expect(kindergarten_row).to_be_visible()
        before_kindergarten = page.evaluate(capture_state, '[data-school-priority-row][data-school-value="kindergarten"]')
        kindergarten_preference = kindergarten_row.locator("[data-school-preference-select]")
        kindergarten_distance = kindergarten_row.locator("[data-school-distance-select]")

        kindergarten_preference.select_option("strong_wish")
        page.wait_for_function(
            """
            () => document.querySelector('[data-school-priority-row][data-school-value="kindergarten"]')
              ?.getAttribute('data-school-distance-enabled') === 'true'
            """
        )
        after_kindergarten_pref = page.evaluate(capture_state, '[data-school-priority-row][data-school-value="kindergarten"]')
        _assert_daily_group_stable(before_kindergarten, after_kindergarten_pref, tolerance=96.0)

        kindergarten_distance.select_option("1000")
        expect(kindergarten_distance).to_have_value("1000")
        after_kindergarten_distance = page.evaluate(capture_state, '[data-school-priority-row][data-school-value="kindergarten"]')
        _assert_daily_group_stable(after_kindergarten_pref, after_kindergarten_distance, tolerance=96.0)
    finally:
        context.close()


def test_propertyquarry_mobile_what_matters_height_resize_keeps_current_group(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        page.locator('[data-property-step-trigger="children"]').click()
        page.wait_for_function(
            "() => document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'children'"
        )

        playground_row = page.locator('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        page.evaluate(
            """
            () => {
              const home = document.querySelector('details[data-what-matters-group="home_basics"]');
              const daily = document.querySelector('details[data-what-matters-group="daily_life"]');
              if (home instanceof HTMLDetailsElement) home.open = true;
              if (daily instanceof HTMLDetailsElement) daily.open = true;
            }
            """
        )
        playground_row.scroll_into_view_if_needed()
        playground_row.locator("[data-keyword-preference-select]").focus()

        capture_state = """
            () => {
              const row = document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]');
              const home = document.querySelector('details[data-what-matters-group="home_basics"]');
              const daily = document.querySelector('details[data-what-matters-group="daily_life"]');
              const active = document.activeElement?.closest?.('details[data-what-matters-group]');
              const rect = row?.getBoundingClientRect();
              return {
                rowTop: rect ? rect.top : 0,
                scrollY: Number(window.scrollY || window.pageYOffset || 0),
                homeOpen: Boolean(home?.open),
                dailyOpen: Boolean(daily?.open),
                focusedGroup: String(active?.getAttribute?.('data-what-matters-group') || ''),
              };
            }
        """

        before_state = page.evaluate(capture_state)
        assert before_state["dailyOpen"] is True, before_state
        assert before_state["focusedGroup"] == "daily_life", before_state

        page.set_viewport_size({"width": 390, "height": 760})
        page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        mid_state = page.evaluate(capture_state)
        assert mid_state["dailyOpen"] is True, mid_state

        page.set_viewport_size({"width": 390, "height": 844})
        page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        after_state = page.evaluate(capture_state)
        assert after_state["dailyOpen"] is True, after_state
        assert abs(float(after_state["rowTop"]) - float(before_state["rowTop"])) <= 140.0, {
            "before": before_state,
            "after": after_state,
        }
        assert abs(float(after_state["scrollY"]) - float(before_state["scrollY"])) <= 180.0, {
            "before": before_state,
            "after": after_state,
        }
    finally:
        context.close()


def test_propertyquarry_what_matters_summary_counts_follow_live_changes(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1360, height=1000)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        page.locator('[data-property-step-trigger="children"]').click()
        page.wait_for_function(
            "() => document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'children'"
        )

        header_chip = page.locator('[data-pqx-what-matters-count]').first
        more_chip = page.locator('[data-pqx-what-matters-more-count]').first
        expect(header_chip).to_have_text("Optional")
        expect(more_chip).to_have_text("Optional")

        playground_row = page.locator('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        playground_row.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
        playground_row.locator("[data-keyword-preference-select]").select_option("nice_to_have")
        expect(header_chip).to_have_text("1 active")

        kindergarten_row = page.locator('[data-school-priority-row][data-school-value="kindergarten"]')
        kindergarten_row.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
        kindergarten_row.locator("[data-school-preference-select]").select_option("strong_wish")
        expect(header_chip).to_have_text("2 active")

        page.locator('[data-pqx-what-matters-more] summary').click()
        custom_keywords = page.locator('[name="custom_keywords"]').first
        custom_keywords.fill("tree-lined street")
        expect(more_chip).to_have_text("1 active")
    finally:
        context.close()


def test_propertyquarry_mobile_what_matters_load_keeps_current_group_and_scroll(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        page.locator('[data-property-step-trigger="children"]').click()
        page.wait_for_function(
            "() => document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'children'"
        )

        open_row_group = """
            (node) => {
              const group = node?.closest?.('details[data-what-matters-group]');
              if (!(group instanceof HTMLDetailsElement)) return;
              group.open = true;
              node?.scrollIntoView?.({ block: 'center', inline: 'nearest' });
            }
        """
        capture_keyword_state = """
            (keywordValue) => {
              const row = document.querySelector(`[data-keyword-priority-row][data-keyword-value="${keywordValue}"]`);
              const group = row?.closest('details[data-what-matters-group]');
              const form = document.querySelector('[data-console-form-variant="property_search"]');
              const rect = row?.getBoundingClientRect();
              return {
                activeStep: String(form?.dataset.propertyActiveStep || ''),
                groupKey: String(group?.getAttribute('data-what-matters-group') || ''),
                groupOpen: Boolean(group?.open),
                rowTop: rect ? rect.top : 0,
                scrollY: Number(window.scrollY || window.pageYOffset || 0),
              };
            }
        """

        playground_row = page.locator('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        playground_row.evaluate(open_row_group)
        expect(playground_row).to_be_visible()
        preference = playground_row.locator("[data-keyword-preference-select]")
        distance = playground_row.locator("[data-keyword-distance-select]")

        preference.select_option("nice_to_have")
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
              ?.getAttribute('data-keyword-distance-enabled') === 'true'
            """
        )
        distance.select_option("500")
        expect(distance).to_have_value("500")
        _click_visible_what_matters_action(page, "save")
        expect(page.locator('[data-property-inline-status]').first).to_contain_text("Saved what matters.")

        playground_row.evaluate(open_row_group)
        expect(playground_row).to_be_visible()
        preference = playground_row.locator("[data-keyword-preference-select]")
        distance = playground_row.locator("[data-keyword-distance-select]")
        preference.select_option("strong_wish")
        distance.select_option("2000")
        expect(distance).to_have_value("2000")
        before_load = page.evaluate(capture_keyword_state, "playground nearby")

        _click_visible_what_matters_action(page, "load")
        expect(page.locator('[data-property-inline-status]').first).to_contain_text("Loaded saved what matters.")
        expect(preference).to_have_value("nice_to_have")
        expect(distance).to_have_value("500")

        after_load = page.evaluate(capture_keyword_state, "playground nearby")
        assert after_load["activeStep"] == "children", after_load
        assert after_load["groupKey"] == "daily_life", after_load
        assert after_load["groupOpen"] is True, after_load
        assert abs(float(after_load["rowTop"]) - float(before_load["rowTop"])) <= 72.0, {
            "before": before_load,
            "after": after_load,
        }
        assert abs(float(after_load["scrollY"]) - float(before_load["scrollY"])) <= 72.0, {
            "before": before_load,
            "after": after_load,
        }
    finally:
        context.close()


def test_propertyquarry_desktop_what_matters_select_changes_preserve_open_groups(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1360, height=1000)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        page.locator('[data-property-step-trigger="children"]').click()
        page.wait_for_function(
            "() => document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'children'"
        )

        playground_row = page.locator('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        playground_row.evaluate(
            """
            (node) => {
              const dailyLife = node?.closest?.('details[data-what-matters-group]');
              const home = document.querySelector('details[data-what-matters-group="home_basics"]');
              const risk = document.querySelector('details[data-what-matters-group="risk_evidence"]');
              if (home instanceof HTMLDetailsElement) home.open = false;
              if (risk instanceof HTMLDetailsElement) risk.open = false;
              if (dailyLife instanceof HTMLDetailsElement) dailyLife.open = true;
              node.scrollIntoView({ block: 'center', inline: 'nearest' });
            }
            """
        )
        expect(playground_row).to_be_visible()
        before_state = page.evaluate(
            """
            () => {
              const row = document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]');
              const home = document.querySelector('details[data-what-matters-group="home_basics"]');
              const dailyLife = document.querySelector('details[data-what-matters-group="daily_life"]');
              const risk = document.querySelector('details[data-what-matters-group="risk_evidence"]');
              const panel = row?.closest?.('[data-property-what-matters-panel]');
              const form = row?.closest?.('form');
              const overflowAnchorFor = (node) => node instanceof Element
                ? window.getComputedStyle(node).getPropertyValue('overflow-anchor').trim()
                : '';
              return {
                rowTop: row?.getBoundingClientRect().top || 0,
                scrollY: Number(window.scrollY || window.pageYOffset || 0),
                homeOpen: Boolean(home?.open),
                dailyLifeOpen: Boolean(dailyLife?.open),
                riskOpen: Boolean(risk?.open),
                supportsOverflowAnchor: window.CSS?.supports?.('overflow-anchor', 'none') === true,
                panelOverflowAnchor: overflowAnchorFor(panel),
                htmlOverflowAnchor: overflowAnchorFor(document.documentElement),
                bodyOverflowAnchor: overflowAnchorFor(document.body),
                formOverflowAnchor: overflowAnchorFor(form),
              };
            }
            """
        )
        assert before_state["homeOpen"] is False, before_state
        assert before_state["dailyLifeOpen"] is True, before_state
        assert before_state["riskOpen"] is False, before_state
        if before_state["supportsOverflowAnchor"]:
            assert before_state["panelOverflowAnchor"] == "none", before_state
            assert before_state["htmlOverflowAnchor"] != "none", before_state
            assert before_state["bodyOverflowAnchor"] != "none", before_state
            assert before_state["formOverflowAnchor"] != "none", before_state

        # Hosted Chromium can apply a focus/layout scroll after the change
        # handler returns. Model that browser-owned drift without user input.
        page.evaluate(
            """
            () => {
              const preference = document.querySelector(
                '[data-keyword-priority-row][data-keyword-value="playground nearby"] [data-keyword-preference-select]'
              );
              const scrollRange = document.createElement('div');
              scrollRange.style.height = '480px';
              scrollRange.setAttribute('aria-hidden', 'true');
              document.body.appendChild(scrollRange);
              preference?.addEventListener('change', () => {
                window.setTimeout(() => {
                  window.scrollBy({ top: 140, left: 0, behavior: 'instant' });
                }, 0);
              }, { once: true });
            }
            """
        )
        playground_row.locator("[data-keyword-preference-select]").select_option("nice_to_have")
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
              ?.getAttribute('data-keyword-distance-enabled') === 'true'
            """
        )
        # The anchor restorer intentionally completes through two animation frames
        # and a final 64 ms correction after the control-state marker flips.
        page.evaluate(
            """
            () => new Promise((resolve) => {
              window.requestAnimationFrame(() => {
                window.requestAnimationFrame(() => {
                  window.setTimeout(resolve, 80);
                });
              });
            })
            """
        )
        after_state = page.evaluate(
            """
            () => {
              const row = document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]');
              const home = document.querySelector('details[data-what-matters-group="home_basics"]');
              const dailyLife = document.querySelector('details[data-what-matters-group="daily_life"]');
              const risk = document.querySelector('details[data-what-matters-group="risk_evidence"]');
              return {
                rowTop: row?.getBoundingClientRect().top || 0,
                scrollY: Number(window.scrollY || window.pageYOffset || 0),
                homeOpen: Boolean(home?.open),
                dailyLifeOpen: Boolean(dailyLife?.open),
                riskOpen: Boolean(risk?.open),
              };
            }
            """
        )
        assert after_state["homeOpen"] is False, after_state
        assert after_state["dailyLifeOpen"] is True, after_state
        assert after_state["riskOpen"] is False, after_state
        assert abs(float(after_state["rowTop"]) - float(before_state["rowTop"])) <= 72.0, {
            "before": before_state,
            "after": after_state,
        }
        assert abs(float(after_state["scrollY"]) - float(before_state["scrollY"])) <= 72.0, {
            "before": before_state,
            "after": after_state,
        }
    finally:
        context.close()


@pytest.mark.parametrize("initiator", ("pointer", "keyboard"))
def test_propertyquarry_desktop_initiated_what_matters_change_restores_delayed_browser_drift(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    initiator: str,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1360, height=1000)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        page.locator('[data-property-step-trigger="children"]').click()
        page.wait_for_function(
            "() => document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'children'"
        )

        before_state = page.evaluate(
            """
            () => {
              const row = document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]');
              const preference = row?.querySelector('[data-keyword-preference-select]');
              const dailyLife = row?.closest?.('details[data-what-matters-group]');
              const home = document.querySelector('details[data-what-matters-group="home_basics"]');
              const risk = document.querySelector('details[data-what-matters-group="risk_evidence"]');
              if (!(row instanceof HTMLElement) || !(preference instanceof HTMLSelectElement)) return { error: 'missing_test_target' };
              if (home instanceof HTMLDetailsElement) home.open = false;
              if (risk instanceof HTMLDetailsElement) risk.open = false;
              if (dailyLife instanceof HTMLDetailsElement) dailyLife.open = true;
              row.scrollIntoView({ block: 'center', inline: 'nearest' });
              const scrollRange = document.createElement('div');
              scrollRange.style.height = '480px';
              scrollRange.setAttribute('aria-hidden', 'true');
              document.body.appendChild(scrollRange);
              preference.addEventListener('change', () => {
                window.setTimeout(() => window.scrollBy({ top: 140, left: 0, behavior: 'instant' }), 0);
              }, { once: true });
              return {
                rowTop: Number(row.getBoundingClientRect().top || 0),
                scrollY: Number(window.scrollY || window.pageYOffset || 0),
              };
            }
            """
        )
        assert before_state.get("error") is None, before_state

        page.evaluate(
            """
            (initiator) => {
              const preference = document.querySelector(
                '[data-keyword-priority-row][data-keyword-value="playground nearby"] [data-keyword-preference-select]'
              );
              if (!(preference instanceof HTMLSelectElement)) return;
              if (initiator === 'pointer') {
                const pointer = { bubbles: true, pointerId: 1, pointerType: 'mouse', isPrimary: true };
                preference.dispatchEvent(new PointerEvent('pointerdown', pointer));
                preference.focus({ preventScroll: true });
                preference.dispatchEvent(new PointerEvent('pointerup', pointer));
              } else {
                preference.focus({ preventScroll: true });
                preference.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'ArrowDown' }));
              }
              preference.value = 'nice_to_have';
              preference.dispatchEvent(new Event('input', { bubbles: true }));
              preference.dispatchEvent(new Event('change', { bubbles: true }));
            }
            """,
            initiator,
        )
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
              ?.getAttribute('data-keyword-distance-enabled') === 'true'
            """
        )
        _wait_for_what_matters_interaction_stabilization(page)
        after_state = page.evaluate(
            """
            () => {
              const row = document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]');
              const preference = row?.querySelector('[data-keyword-preference-select]');
              return {
                rowTop: Number(row?.getBoundingClientRect().top || 0),
                scrollY: Number(window.scrollY || window.pageYOffset || 0),
                distanceEnabled: row?.getAttribute('data-keyword-distance-enabled') === 'true',
                preferenceFocused: document.activeElement === preference,
              };
            }
            """
        )
        assert after_state["distanceEnabled"] is True, after_state
        assert after_state["preferenceFocused"] is True, after_state
        assert abs(float(after_state["rowTop"]) - float(before_state["rowTop"])) <= 72.0, {
            "before": before_state,
            "after": after_state,
        }
        assert abs(float(after_state["scrollY"]) - float(before_state["scrollY"])) <= 72.0, {
            "before": before_state,
            "after": after_state,
        }
    finally:
        context.close()


def test_propertyquarry_what_matters_save_and_load_round_trip_visible_keyword_controls(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1360, height=1000)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        catalog_distance_contract = page.evaluate(
            """
            () => JSON.parse(String(document.querySelector(
              '[data-property-distance-preference-contract]'
            )?.textContent || '[]'))
            """
        )
        assert len(catalog_distance_contract) == len(_CATALOG_DISTANCE_RADIUS_KEYS)
        assert all(
            row[0] == expected_distance_key
            and row[2] in _CANONICAL_DISTANCE_IMPORTANCE_VALUES
            and (
                row[1] is None
                or isinstance(row[1], int)
                and 0 <= row[1] <= 7000
            )
            for row, expected_distance_key in zip(
                catalog_distance_contract,
                _CATALOG_DISTANCE_RADIUS_KEYS,
                strict=True,
            )
        ), catalog_distance_contract
        assert tuple(
            property_distance_importance_key(str(row[0]))
            for row in catalog_distance_contract
        ) == _CATALOG_DISTANCE_IMPORTANCE_KEYS
        rendered_catalog_importance_contract = page.evaluate(
            """
            (expectedNames) => Array.from(document.querySelectorAll('select'))
              .filter((node) => expectedNames.includes(node.getAttribute('name')))
              .map((node) => ({
                name: node.getAttribute('name'),
                values: Array.from(node.options).map((option) => option.value),
              }))
            """,
            list(_CATALOG_DISTANCE_IMPORTANCE_KEYS),
        )
        assert rendered_catalog_importance_contract
        assert all(
            row["name"] in _CATALOG_DISTANCE_IMPORTANCE_KEYS
            and row["values"] == list(_CANONICAL_DISTANCE_IMPORTANCE_VALUES)
            for row in rendered_catalog_importance_contract
        ), rendered_catalog_importance_contract
        visible_distance_importance_contract = page.evaluate(
            """
            () => Array.from(document.querySelectorAll(
              '[data-keyword-priority-row], [data-school-priority-row]'
            )).flatMap((row) => {
              const hasDistance = row.querySelector(
                '[data-keyword-distance-select], [data-school-distance-select]'
              );
              const select = row.querySelector(
                '[data-keyword-preference-select], [data-school-preference-select]'
              );
              if (!hasDistance || !(select instanceof HTMLSelectElement)) return [];
              return [{
                label: row.getAttribute('aria-label') || '',
                values: Array.from(select.options).map((option) => option.value),
              }];
            })
            """
        )
        assert visible_distance_importance_contract
        for row in visible_distance_importance_contract:
            values = list(row["values"])
            assert "important" not in values, row
            assert set(values) - {"any"} == set(
                _CANONICAL_DISTANCE_IMPORTANCE_VALUES
            ), row
        set_select_value = """
            (node, value) => {
              if (!(node instanceof HTMLSelectElement)) return;
              node.value = value;
              node.dispatchEvent(new Event('input', { bubbles: true }));
              node.dispatchEvent(new Event('change', { bubbles: true }));
            }
        """
        ensure_children_step = """
            () => {
              const form = document.querySelector('[data-console-form-variant="property_search"]');
              if (!form) return false;
              const current = String(form.dataset.propertyActiveStep || '').trim();
              if (current === 'children') return true;
              const trigger = document.querySelector('[data-property-step-trigger="children"]');
              if (!(trigger instanceof HTMLElement)) return false;
              trigger.click();
              return false;
            }
        """
        open_details_group = """
            (node) => {
              const group = node?.closest?.('details[data-what-matters-group]');
              if (group instanceof HTMLDetailsElement) group.open = true;
            }
        """

        def _open_playground_controls() -> tuple[object, object, object]:
            page.wait_for_function(ensure_children_step)
            row = page.locator('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
            row.evaluate(open_details_group)
            expect(row).to_be_visible()
            row.scroll_into_view_if_needed()
            return row, row.locator("[data-keyword-preference-select]"), row.locator("[data-keyword-distance-select]")

        def _open_market_controls() -> tuple[object, object, object]:
            page.wait_for_function(ensure_children_step)
            row = page.locator('[data-keyword-priority-row][data-keyword-value="market nearby"]')
            row.evaluate(open_details_group)
            expect(row).to_be_visible()
            row.scroll_into_view_if_needed()
            return row, row.locator("[data-keyword-preference-select]"), row.locator("[data-keyword-distance-select]")

        def _open_subway_controls() -> tuple[object, object, object]:
            page.wait_for_function(ensure_children_step)
            row = page.locator('[data-keyword-priority-row][data-keyword-value="underground nearby"]')
            row.evaluate(open_details_group)
            expect(row).to_be_visible()
            row.scroll_into_view_if_needed()
            return row, row.locator("[data-keyword-preference-select]"), row.locator("[data-keyword-distance-select]")

        def _open_pharmacy_controls() -> tuple[object, object, object]:
            page.wait_for_function(ensure_children_step)
            row = page.locator('[data-keyword-priority-row][data-keyword-value="pharmacy nearby"]')
            row.evaluate(open_details_group)
            expect(row).to_be_visible()
            row.scroll_into_view_if_needed()
            return row, row.locator("[data-keyword-preference-select]"), row.locator("[data-keyword-distance-select]")

        def _open_kindergarten_controls() -> tuple[object, object, object]:
            page.wait_for_function(ensure_children_step)
            row = page.locator('[data-school-priority-row][data-school-value="kindergarten"]')
            row.evaluate(open_details_group)
            expect(row).to_be_visible()
            row.scroll_into_view_if_needed()
            return row, row.locator("[data-school-preference-select]"), row.locator("[data-school-distance-select]")

        playground_row, preference, distance = _open_playground_controls()
        market_row, market_preference, market_distance = _open_market_controls()
        subway_row, subway_preference, subway_distance = _open_subway_controls()
        pharmacy_row, pharmacy_preference, pharmacy_distance = _open_pharmacy_controls()
        kindergarten_row, kindergarten_preference, kindergarten_distance = _open_kindergarten_controls()
        parking_pressure = page.locator('[data-keyword-priority-row][data-keyword-value="parking pressure check"] [data-keyword-preference-select]')
        low_crime = page.locator('[data-keyword-priority-row][data-keyword-value="low crime area"] [data-keyword-preference-select]')
        flood_risk = page.locator('[data-keyword-priority-row][data-keyword-value="avoid flood-risk area"] [data-keyword-preference-select]')

        preference.select_option("nice_to_have")
        expect(preference).to_have_value("nice_to_have")
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
              ?.getAttribute('data-preference-state') === 'nice_to_have'
            """
        )
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
              ?.getAttribute('data-keyword-distance-enabled') === 'true'
            """
        )
        page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        distance.evaluate(set_select_value, "500")
        expect(distance).to_have_value("500")
        market_preference.evaluate(set_select_value, "strong_wish")
        expect(market_preference).to_have_value("strong_wish")
        market_distance.evaluate(set_select_value, "1000")
        expect(market_distance).to_have_value("1000")
        subway_preference.evaluate(set_select_value, "strong_wish")
        expect(subway_preference).to_have_value("strong_wish")
        subway_distance.evaluate(set_select_value, "500")
        expect(subway_distance).to_have_value("500")
        pharmacy_preference.evaluate(set_select_value, "nice_to_have")
        expect(pharmacy_preference).to_have_value("nice_to_have")
        pharmacy_distance.evaluate(set_select_value, "500")
        expect(pharmacy_distance).to_have_value("500")
        kindergarten_preference.evaluate(set_select_value, "must_have")
        expect(kindergarten_preference).to_have_value("must_have")
        kindergarten_distance.evaluate(set_select_value, "500")
        expect(kindergarten_distance).to_have_value("500")
        parking_pressure.evaluate(set_select_value, "high")
        expect(parking_pressure).to_have_value("high")
        low_crime.evaluate(set_select_value, "strong_wish")
        expect(low_crime).to_have_value("strong_wish")
        flood_risk.evaluate(set_select_value, "avoid")
        expect(flood_risk).to_have_value("avoid")
        _click_visible_what_matters_action(page, "save")
        expect(page.locator('[data-property-inline-status]').first).to_contain_text("Saved what matters.")

        playground_row, preference, distance = _open_playground_controls()
        market_row, market_preference, market_distance = _open_market_controls()
        subway_row, subway_preference, subway_distance = _open_subway_controls()
        pharmacy_row, pharmacy_preference, pharmacy_distance = _open_pharmacy_controls()
        kindergarten_row, kindergarten_preference, kindergarten_distance = _open_kindergarten_controls()
        parking_pressure = page.locator('[data-keyword-priority-row][data-keyword-value="parking pressure check"] [data-keyword-preference-select]')
        low_crime = page.locator('[data-keyword-priority-row][data-keyword-value="low crime area"] [data-keyword-preference-select]')
        flood_risk = page.locator('[data-keyword-priority-row][data-keyword-value="avoid flood-risk area"] [data-keyword-preference-select]')
        preference.select_option("strong_wish")
        expect(preference).to_have_value("strong_wish")
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
              ?.getAttribute('data-preference-state') === 'strong_wish'
            """
        )
        distance.evaluate(set_select_value, "2000")
        expect(distance).to_have_value("2000")
        market_preference.evaluate(set_select_value, "avoid")
        expect(market_preference).to_have_value("avoid")
        market_distance.evaluate(set_select_value, "500")
        expect(market_distance).to_have_value("500")
        subway_preference.evaluate(set_select_value, "nice_to_have")
        expect(subway_preference).to_have_value("nice_to_have")
        subway_distance.evaluate(set_select_value, "1000")
        expect(subway_distance).to_have_value("1000")
        pharmacy_preference.evaluate(set_select_value, "avoid")
        expect(pharmacy_preference).to_have_value("avoid")
        pharmacy_distance.evaluate(set_select_value, "2000")
        expect(pharmacy_distance).to_have_value("2000")
        kindergarten_preference.evaluate(set_select_value, "nice_to_have")
        expect(kindergarten_preference).to_have_value("nice_to_have")
        kindergarten_distance.evaluate(set_select_value, "2000")
        expect(kindergarten_distance).to_have_value("2000")
        parking_pressure.evaluate(set_select_value, "low")
        expect(parking_pressure).to_have_value("low")
        low_crime.evaluate(set_select_value, "any")
        expect(low_crime).to_have_value("any")
        flood_risk.evaluate(set_select_value, "any")
        expect(flood_risk).to_have_value("any")

        _click_visible_what_matters_action(page, "load")
        expect(page.locator('[data-property-inline-status]').first).to_contain_text("Loaded saved what matters.")
        expect(preference).to_have_value("nice_to_have")
        expect(distance).to_have_value("500")
        expect(market_preference).to_have_value("strong_wish")
        expect(market_distance).to_have_value("1000")
        expect(subway_preference).to_have_value("strong_wish")
        expect(subway_distance).to_have_value("500")
        expect(pharmacy_preference).to_have_value("nice_to_have")
        expect(pharmacy_distance).to_have_value("500")
        expect(kindergarten_preference).to_have_value("must_have")
        expect(kindergarten_distance).to_have_value("500")
        expect(parking_pressure).to_have_value("high")
        expect(low_crime).to_have_value("strong_wish")
        expect(flood_risk).to_have_value("avoid")

        playground_row, preference, distance = _open_playground_controls()
        market_row, market_preference, market_distance = _open_market_controls()
        subway_row, subway_preference, subway_distance = _open_subway_controls()
        pharmacy_row, pharmacy_preference, pharmacy_distance = _open_pharmacy_controls()
        kindergarten_row, kindergarten_preference, kindergarten_distance = _open_kindergarten_controls()
        parking_pressure = page.locator('[data-keyword-priority-row][data-keyword-value="parking pressure check"] [data-keyword-preference-select]')
        low_crime = page.locator('[data-keyword-priority-row][data-keyword-value="low crime area"] [data-keyword-preference-select]')
        flood_risk = page.locator('[data-keyword-priority-row][data-keyword-value="avoid flood-risk area"] [data-keyword-preference-select]')
        preference.select_option("must_have")
        expect(preference).to_have_value("must_have")
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
              ?.getAttribute('data-preference-state') === 'must_have'
            """
        )
        page.wait_for_function(
            """
            () => document.querySelector('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
              ?.getAttribute('data-keyword-distance-enabled') === 'true'
            """
        )
        page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        distance.evaluate(set_select_value, "1000")
        expect(distance).to_have_value("1000")
        market_preference.evaluate(set_select_value, "strong_wish")
        expect(market_preference).to_have_value("strong_wish")
        market_distance.evaluate(set_select_value, "2000")
        expect(market_distance).to_have_value("2000")
        subway_preference.evaluate(set_select_value, "nice_to_have")
        expect(subway_preference).to_have_value("nice_to_have")
        subway_distance.evaluate(set_select_value, "250")
        expect(subway_distance).to_have_value("250")
        pharmacy_preference.evaluate(set_select_value, "strong_wish")
        expect(pharmacy_preference).to_have_value("strong_wish")
        pharmacy_distance.evaluate(set_select_value, "250")
        expect(pharmacy_distance).to_have_value("250")
        kindergarten_preference.evaluate(set_select_value, "avoid")
        expect(kindergarten_preference).to_have_value("avoid")
        kindergarten_distance.evaluate(set_select_value, "1000")
        expect(kindergarten_distance).to_have_value("1000")
        parking_pressure.evaluate(set_select_value, "medium")
        expect(parking_pressure).to_have_value("medium")
        low_crime.evaluate(set_select_value, "strong_wish")
        expect(low_crime).to_have_value("strong_wish")
        flood_risk.evaluate(set_select_value, "avoid")
        expect(flood_risk).to_have_value("avoid")
        _click_visible_what_matters_action(page, "save")
        expect(page.locator('[data-property-inline-status]').first).to_contain_text("Saved what matters.")

        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        page.locator('[data-property-step-trigger="children"]').click()
        page.wait_for_function(
            """() => document.querySelector('[data-console-form-variant="property_search"]')?.dataset.propertyActiveStep === 'children'"""
        )
        reloaded_row = page.locator('[data-keyword-priority-row][data-keyword-value="playground nearby"]')
        reloaded_row.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
        expect(reloaded_row.locator("[data-keyword-preference-select]")).to_have_value("must_have")
        expect(reloaded_row.locator("[data-keyword-distance-select]")).to_have_value("1000")
        reloaded_market_row = page.locator('[data-keyword-priority-row][data-keyword-value="market nearby"]')
        reloaded_market_row.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
        expect(reloaded_market_row.locator("[data-keyword-preference-select]")).to_have_value("strong_wish")
        expect(reloaded_market_row.locator("[data-keyword-distance-select]")).to_have_value("2000")
        reloaded_subway_row = page.locator('[data-keyword-priority-row][data-keyword-value="underground nearby"]')
        reloaded_subway_row.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
        expect(reloaded_subway_row.locator("[data-keyword-preference-select]")).to_have_value("nice_to_have")
        expect(reloaded_subway_row.locator("[data-keyword-distance-select]")).to_have_value("250")
        reloaded_pharmacy_row = page.locator('[data-keyword-priority-row][data-keyword-value="pharmacy nearby"]')
        reloaded_pharmacy_row.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
        expect(reloaded_pharmacy_row.locator("[data-keyword-preference-select]")).to_have_value("strong_wish")
        expect(reloaded_pharmacy_row.locator("[data-keyword-distance-select]")).to_have_value("250")
        untouched_theatre_row = page.locator('[data-keyword-priority-row][data-keyword-value="theatre nearby"]')
        untouched_theatre_row.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
        expect(untouched_theatre_row.locator("[data-keyword-preference-select]")).to_have_value("any")
        expect(untouched_theatre_row.locator("[data-keyword-distance-select]")).to_be_disabled()
        reloaded_kindergarten_row = page.locator('[data-school-priority-row][data-school-value="kindergarten"]')
        reloaded_kindergarten_row.evaluate("node => node.closest('details[data-what-matters-group]')?.setAttribute('open', '')")
        expect(reloaded_kindergarten_row.locator("[data-school-preference-select]")).to_have_value("avoid")
        expect(reloaded_kindergarten_row.locator("[data-school-distance-select]")).to_have_value("1000")
        expect(page.locator('[data-keyword-priority-row][data-keyword-value="parking pressure check"] [data-keyword-preference-select]')).to_have_value("medium")
        expect(page.locator('[data-keyword-priority-row][data-keyword-value="low crime area"] [data-keyword-preference-select]')).to_have_value("strong_wish")
        expect(page.locator('[data-keyword-priority-row][data-keyword-value="avoid flood-risk area"] [data-keyword-preference-select]')).to_have_value("avoid")
        stored_distance_contract = page.evaluate(
            """
            async () => {
              const response = await fetch('/v1/onboarding/property-search/preferences', {
                credentials: 'same-origin',
                headers: { accept: 'application/json' },
              });
              const payload = await response.json();
              const preferences = payload.property_search_preferences || {};
              return {
                theatreDistancePresent: Object.hasOwn(preferences, 'max_distance_to_theatre_m'),
                theatreImportancePresent: Object.hasOwn(preferences, 'max_distance_to_theatre_importance'),
                orphanImportanceKeys: Object.keys(preferences).filter((key) => {
                  if (!/^max_distance_to_[a-z0-9_]+_importance$/.test(key)) return false;
                  const distanceKey = key.replace(/_importance$/, '_m');
                  return !(Number(preferences[distanceKey]) > 0);
                }),
              };
            }
            """
        )
        assert stored_distance_contract == {
            "theatreDistancePresent": False,
            "theatreImportancePresent": False,
            "orphanImportanceKeys": [],
        }
    finally:
        context.close()


def test_propertyquarry_search_where_step_keeps_area_selection_visible(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=1200)
    screenshot_path = tmp_path / "propertyquarry-where-step-areas.png"
    try:
        page = context.new_page()
        response = page.goto(f"{base_url}/app/search", wait_until="domcontentloaded")
        assert response is not None and response.ok
        location_field = page.locator('[data-property-field-name="location_query"]')
        location_field.wait_for(state="visible")
        assert location_field.locator('input[name="location_query"]').count() >= 1
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        location_field.screenshot(path=str(screenshot_path))
        assert screenshot_path.exists()
    finally:
        context.close()


def test_propertyquarry_running_progress_panel_fits_the_first_mobile_viewport(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/properties?run_id=run-always-active", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.wait_for_selector('[data-pqx-screenfit-target="run-progress"]', timeout=5000)
        assert page.locator('[data-pqx-screenfit-target="run-progress"] :is(h1, h2)').first.is_visible()
        progress_target = page.locator('[data-pqx-screenfit-target="run-progress"]').first
        progress_text = progress_target.inner_text().strip()
        assert progress_text
        assert "source update" not in progress_text.lower()
        assert "source lanes" not in progress_text.lower()
        assert re.search(r"\bproviders?\b|\bsources?\b|\bsearch pages?\b", progress_text, flags=re.IGNORECASE)
        layout = page.evaluate(
            """
            () => {
              const target = document.querySelector('[data-pqx-screenfit-target="run-progress"]');
              const board = document.querySelector('[data-pqx-progress-board]');
              if (!target) return null;
              const targetBox = target.getBoundingClientRect();
              const boardBox = board ? board.getBoundingClientRect() : targetBox;
              return {
                viewportHeight: window.innerHeight,
                viewportWidth: window.innerWidth,
                targetBottom: Math.round(targetBox.bottom),
                targetRight: Math.round(targetBox.right),
                boardBottom: Math.round(boardBox.bottom),
                boardRight: Math.round(boardBox.right),
                targetFitsViewport: targetBox.bottom <= window.innerHeight + 2 && targetBox.right <= window.innerWidth + 2,
                boardFitsViewport: boardBox.bottom <= window.innerHeight + 2 && boardBox.right <= window.innerWidth + 2,
                etaLabel: (document.querySelector('[data-pqx-progress-eta]')?.textContent || '').trim(),
              };
            }
            """
        )
        assert layout is not None
        assert layout["targetBottom"] <= layout["viewportHeight"] + 112
        assert layout["boardBottom"] <= layout["viewportHeight"] + 112
        _assert_property_shell_visual_gates(page, max_appbar_height=130)
    finally:
        context.close()


def test_propertyquarry_mobile_empty_results_keep_extra_relax_options_behind_more_trigger(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/properties?run_id=run-active-empty", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.wait_for_selector("[data-pqx-empty-results]", timeout=7000)
        visible_actions = page.locator('[data-pqx-counterfactuals] > .pqx-suppression-grid > .pqx-suppression-item:visible')
        expect(visible_actions).to_have_count(1)
        inline_slider_count = page.locator('[data-pqx-counterfactuals] [data-pqx-filter-slider]:visible').count()
        assert inline_slider_count in {0, 1}
        more_trigger = page.get_by_role("button", name=re.compile(r"More options", re.I))
        if more_trigger.count():
            expect(more_trigger).to_be_visible()
            more_trigger.click()
            dialog = page.locator("[data-pqx-filtered-dialog]")
            expect(dialog).to_be_visible()
            expect(dialog).to_contain_text(re.compile(r"Stretch the size rule|Provider overview page|Include nearby districts", re.I))
        else:
            assert visible_actions.first.inner_text().strip()
            expect(page.locator("[data-pqx-filtered-dialog]")).to_be_hidden()
    finally:
        context.close()


def test_propertyquarry_shortlist_and_research_surfaces_do_not_bleed_text(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    screenshot_path = tmp_path / "property_research_detail_first_screen.png"
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        assert page.locator(".pqx-results-summary-row", has_text=re.compile(r"(saved|matching) (homes|opportunities)", re.I)).is_visible()
        _assert_property_shell_visual_gates(page, max_appbar_height=92)

        packet_href = page.locator('a[href*="/app/research/"]').first.get_attribute("href")
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"
        response = page.goto(packet_url, wait_until="domcontentloaded")
        assert response is not None and response.ok
        assert page.locator(".prd-media-frame").is_visible()
        assert "Open the space before you read the rest" not in page.content()
        _assert_research_packet_360_first(page, min_stage_height=190, max_stage_height=380)
        page.wait_for_load_state("networkidle")
        body_box = page.locator(".prd-body").first.bounding_box()
        assert body_box is not None
        viewport_height = page.viewport_size["height"] if page.viewport_size else 900
        assert body_box["y"] < viewport_height
        first_screen = page.evaluate(
            """
            () => {
              const hero = document.querySelector('[data-pqx-screenfit-target="research-detail-hero"]');
              const body = document.querySelector('.prd-body');
              const media = document.querySelector('.prd-media-frame');
              const actions = document.querySelector('.prd-actions');
              const gallery = document.querySelector('.prd-hero-gallery');
              const heroRect = hero ? hero.getBoundingClientRect() : null;
              const bodyRect = body ? body.getBoundingClientRect() : null;
              const mediaRect = media ? media.getBoundingClientRect() : null;
              const actionsRect = actions ? actions.getBoundingClientRect() : null;
              const galleryRect = gallery ? gallery.getBoundingClientRect() : null;
              return {
                viewportHeight: window.innerHeight,
                heroBottom: heroRect ? Math.round(heroRect.bottom) : 0,
                bodyTop: bodyRect ? Math.round(bodyRect.top) : 0,
                mediaHeight: mediaRect ? Math.round(mediaRect.height) : 0,
                actionsBottom: actionsRect ? Math.round(actionsRect.bottom) : 0,
                galleryBottom: galleryRect ? Math.round(galleryRect.bottom) : 0,
              };
            }
            """
        )
        assert first_screen["heroBottom"] <= first_screen["viewportHeight"] + 1
        assert first_screen["bodyTop"] <= first_screen["viewportHeight"] - 32
        assert first_screen["mediaHeight"] <= 380
        assert first_screen["actionsBottom"] <= first_screen["viewportHeight"] + 1
        if first_screen["galleryBottom"]:
            assert first_screen["galleryBottom"] <= first_screen["viewportHeight"] + 1
        page.screenshot(path=str(screenshot_path), full_page=False, animations="disabled", caret="hide")
        assert screenshot_path.exists() and screenshot_path.stat().st_size > 20_000
        assert page.locator("[data-prd-home-panel]").first.is_visible()
        decision_fit = page.evaluate(
            """
            () => {
              const panel = document.querySelector('[data-object-feedback]');
              const note = document.querySelector('[data-object-feedback-note]');
              const tune = Array.from(document.querySelectorAll('.prd-feedback-details'))
                .find((node) => (node.textContent || '').includes('Fine-tune preferences'));
              const rect = panel ? panel.getBoundingClientRect() : null;
              const noteRect = note ? note.getBoundingClientRect() : null;
              return {
                viewportHeight: window.innerHeight,
                panelHeight: rect ? Math.round(rect.height) : 0,
                noteHeight: noteRect ? Math.round(noteRect.height) : 0,
                fineTunePresent: Boolean(tune),
                fineTuneOpen: tune ? Boolean(tune.open) : false,
              };
            }
            """
            )
        assert 0 < decision_fit["panelHeight"] <= decision_fit["viewportHeight"] - 96
        assert decision_fit["noteHeight"] <= 86
        assert decision_fit["fineTuneOpen"] is False
        _assert_property_shell_visual_gates(page, max_appbar_height=92)

        page.evaluate(
            """
            () => {
              window.localStorage.setItem('propertyquarry.theme', 'dark');
              document.documentElement.setAttribute('data-pq-theme', 'dark');
            }
            """
        )
        page.wait_for_timeout(80)
        dark_backgrounds = page.evaluate(
            """
            () => {
              const selectors = ['.pq-appbar', '.prd-panel', '.prd-media-frame', '.prd-media-badge'];
              return selectors
                .filter((selector) => document.querySelector(selector))
                .map((selector) => {
                  const element = document.querySelector(selector);
                  return [selector, window.getComputedStyle(element).backgroundColor];
                });
            }
            """
        )
        assert dark_backgrounds
        for selector, background in dark_backgrounds:
            assert not str(background).startswith(("rgb(255", "rgba(255")), f"{selector} stayed light in dark mode: {background}"
        _assert_dark_mode_surfaces_stay_readable(
            page,
            [
                ".pq-appbar",
                ".prd-hero",
                ".prd-hero *",
                ".prd-panel",
                ".prd-panel *",
                ".prd-body",
                ".prd-body *",
                ".prd-actions",
                ".prd-actions *",
                ".prd-gallery-card",
                ".prd-gallery-card *",
            ],
        )
        dark_screenshot_path = tmp_path / "property_research_detail_first_screen_dark.png"
        page.screenshot(path=str(dark_screenshot_path), full_page=False, animations="disabled", caret="hide")
        assert dark_screenshot_path.exists() and dark_screenshot_path.stat().st_size > 20_000
    finally:
        context.close()


def test_propertyquarry_shortlist_and_research_have_browser_performance_budget(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    try:
        _, shortlist_ms = _goto_with_browser_budget(
            page,
            f"{base_url}/app/shortlist?run_id=run-42&full=1",
            wait_until="networkidle",
            budget_ms=3200,
        )
        assert page.locator(".pqx-results-summary-row", has_text=re.compile(r"(saved|matching) (homes|opportunities)", re.I)).is_visible()
        expect(page.locator("[data-property-app-shell]")).to_be_visible()
        expect(page.locator('a[href*="/app/research/"]').first).to_be_visible()
        _assert_property_shell_visual_gates(page, max_appbar_height=92)

        packet_href = page.locator('a[href*="/app/research/"]').first.get_attribute("href")
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"
        _, research_ms = _goto_with_browser_budget(
            page,
            packet_url,
            wait_until="networkidle",
            budget_ms=3600,
        )
        expect(page.locator("[data-property-research-detail]")).to_be_visible()
        expect(page.locator("[data-research-ranking-list]")).to_have_count(0)
        expect(page.locator(".prd-media-frame")).to_be_visible()
        _assert_research_packet_360_first(page, min_stage_height=190, max_stage_height=380)
        _assert_no_horizontal_overflow(page)
        assert shortlist_ms > 0 and research_ms > 0
    finally:
        context.close()


def test_propertyquarry_research_detail_is_mobile_optimized_and_visuals_are_opt_in(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls = 0

    def _capture_visual_request(route) -> None:
        request = route.request
        payload = request.post_data_json
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-06-21T10:00:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Listing URL only loft",
                    "request_kind": visual_requests[-1].get("request_kind", "flythrough"),
                    "tour_url": "",
                    "flythrough_url": "",
                    "flythrough_status": "pending",
                    "status_label": "Walkthrough queued",
                    "status_detail": "Walkthrough is queued after your request.",
                    "eta_label": "about 10 min",
                    "progress_pct": 18,
                    "poll_after_seconds": 2,
                    "delivery_status": "skipped",
                    "blocked_reason": "",
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    def _capture_visual_status(route) -> None:
        nonlocal visual_status_polls
        visual_status_polls += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-06-21T10:00:03+00:00",
                    "status": "ready" if visual_status_polls >= 1 else "processing",
                    "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/listing-url-only-loft",
                    "title": "Listing URL only loft",
                    "request_kind": "flythrough",
                    "tour_url": "",
                    "tour_status": "pending",
                    "flythrough_url": "https://propertyquarry.com/tours/files/listing-url-only-loft/walkthrough.mp4",
                    "flythrough_status": "ready",
                    "status_label": "Open walkthrough",
                    "status_detail": "Walkthrough is ready.",
                    "eta_label": "",
                    "progress_pct": 100,
                    "poll_after_seconds": 0,
                    "source_ref": "willhaben:listing-url-only-loft",
                    "run_id": "run-42",
                    "candidate_ref": "listing-url-only-loft",
                }
            ),
        )

    page.route("**/app/api/signals/property/visual-status?**", _capture_visual_status)
    screenshot_path = tmp_path / "property-research-detail-mobile.png"
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        row = page.locator("[data-workbench-row]", has_text="Listing URL only loft").first
        row.wait_for(timeout=5000)
        packet_href = str(row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"

        response = page.goto(packet_url, wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.locator("[data-property-research-detail]")).to_be_visible()
        expect(page.locator(".prd-media-frame")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile("Request walkthrough", re.I))).to_be_visible()
        expect(page.get_by_role("button", name="Copy link")).to_be_hidden()
        assert visual_requests == []
        _assert_no_horizontal_overflow(page)
        layout = page.evaluate(
            """
            () => {
              const hero = document.querySelector('[data-pqx-screenfit-target="research-detail-hero"]');
              const body = document.querySelector('.prd-body');
                const media = document.querySelector('.prd-media-frame');
                const actions = document.querySelector('.prd-actions');
                const headline = document.querySelector('.prd-headline');
                const headlineRead = document.querySelector('.prd-headline-read');
                const visualConsole = document.querySelector('.prd-visual-console');
                const summaryGrid = document.querySelector('.prd-headline-read .prd-summary-grid');
                const summaryBoxes = Array.from(document.querySelectorAll('.prd-headline-read .prd-summary-box'));
              const shell = document.querySelector('[data-property-research-detail]');
              const feedback = document.querySelector('[data-object-feedback]');
              const sections = document.querySelector('.prd-sections');
              const decision = document.querySelector('.prd-decision-workspace');
              const secondaryDetails = Array.from(document.querySelectorAll('[data-prd-mobile-secondary]'));
              const fineTune = Array.from(document.querySelectorAll('.prd-feedback-details'))
                .find((node) => (node.textContent || '').includes('Fine-tune preferences'));
              const optionalNote = Array.from(document.querySelectorAll('.prd-feedback-details'))
                .find((node) => (node.querySelector('summary')?.textContent || '').trim() === 'Add an optional note');
              const nextStepDrawer = Array.from(document.querySelectorAll('.prd-feedback-details'))
                .find((node) => (node.querySelector('summary')?.textContent || '').trim() === 'Next step');
              const advancedSummary = document.querySelector('.prd-decision-advanced > summary');
              const advancedSummaryRect = advancedSummary ? advancedSummary.getBoundingClientRect() : null;
              const advancedSummaryStyle = advancedSummary ? getComputedStyle(advancedSummary) : null;
              const tuneButton = document.querySelector('[data-object-feedback-open-advanced]');
              const tuneButtonRect = tuneButton ? tuneButton.getBoundingClientRect() : null;
              const savebar = document.querySelector('.prd-decision-savebar');
                const heroRect = hero ? hero.getBoundingClientRect() : null;
                const bodyRect = body ? body.getBoundingClientRect() : null;
                const mediaRect = media ? media.getBoundingClientRect() : null;
                const headlineRect = headline ? headline.getBoundingClientRect() : null;
                const headlineReadRect = headlineRead ? headlineRead.getBoundingClientRect() : null;
                const headlineReadStyle = headlineRead ? getComputedStyle(headlineRead) : null;
                const visualConsoleRect = visualConsole ? visualConsole.getBoundingClientRect() : null;
                const summaryGridStyle = summaryGrid ? getComputedStyle(summaryGrid) : null;
              const summaryBoxRects = summaryBoxes.map((card) => card.getBoundingClientRect());
              const actionsStyle = actions ? getComputedStyle(actions) : null;
              const shellRect = shell ? shell.getBoundingClientRect() : null;
              const feedbackRect = feedback ? feedback.getBoundingClientRect() : null;
              const feedbackStyle = feedback ? getComputedStyle(feedback) : null;
              const sectionsRect = sections ? sections.getBoundingClientRect() : null;
              const decisionRect = decision ? decision.getBoundingClientRect() : null;
              const savebarRect = savebar ? savebar.getBoundingClientRect() : null;
              return {
                viewportWidth: window.innerWidth,
                viewportHeight: window.innerHeight,
                shellWidth: shellRect ? shellRect.width : 0,
                heroWidth: heroRect ? heroRect.width : 0,
                heroBottom: heroRect ? Math.round(heroRect.bottom) : 0,
                bodyTop: bodyRect ? Math.round(bodyRect.top) : 0,
                    mediaHeight: mediaRect ? Math.round(mediaRect.height) : 0,
                    headlineTop: headlineRect ? Math.round(headlineRect.top) : 0,
                    headlineReadVisible: Boolean(
                      headlineReadRect
                      && headlineReadRect.width > 0
                      && headlineReadRect.height > 0
                      && headlineReadStyle
                      && headlineReadStyle.display !== 'none'
                      && headlineReadStyle.visibility !== 'hidden'
                    ),
                    visualConsoleTop: visualConsoleRect ? Math.round(visualConsoleRect.top) : 0,
                    actionsDisplay: actionsStyle ? actionsStyle.display : '',
                actionsColumns: actionsStyle ? actionsStyle.gridTemplateColumns : '',
                summaryDisplay: summaryGridStyle ? summaryGridStyle.display : '',
                summaryColumns: summaryGridStyle ? summaryGridStyle.gridTemplateColumns.split(' ').filter(Boolean).length : 0,
                summaryScrollWidth: summaryGrid ? summaryGrid.scrollWidth : 0,
                summaryClientWidth: summaryGrid ? summaryGrid.clientWidth : 0,
                summaryBoxCount: summaryBoxes.length,
                summaryShortestCard: Math.min(...summaryBoxRects.map((rect) => Math.round(rect.height))),
                summaryTallestCard: Math.max(0, ...summaryBoxRects.map((rect) => Math.round(rect.height))),
                summaryMaxRight: Math.max(0, ...summaryBoxRects.map((rect) => Math.round(rect.right))),
                decisionTop: decisionRect ? Math.round(decisionRect.top) : 0,
                sectionsTop: sectionsRect ? Math.round(sectionsRect.top) : 0,
                secondarySectionCount: secondaryDetails.length,
                closedSecondarySections: secondaryDetails.filter((node) => !node.open).length,
                visibleSecondarySummaries: secondaryDetails.filter((node) => {
                  const summary = node.querySelector('summary');
                  if (!summary) return false;
                  const rect = summary.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0 && getComputedStyle(summary).display !== 'none';
                }).length,
                feedbackHeight: feedbackRect ? Math.round(feedbackRect.height) : 0,
                feedbackBottom: feedbackRect ? Math.round(feedbackRect.bottom) : 0,
                feedbackOverflowY: feedbackStyle ? feedbackStyle.overflowY : '',
                feedbackScrolls: feedback ? feedback.scrollHeight > feedback.clientHeight + 2 : false,
                fineTuneExists: Boolean(fineTune),
                fineTuneOpen: fineTune ? Boolean(fineTune.open) : false,
                advancedSummaryVisible: Boolean(
                  advancedSummaryRect
                  && advancedSummaryRect.width > 0
                  && advancedSummaryRect.height > 0
                  && advancedSummaryStyle
                  && advancedSummaryStyle.display !== 'none'
                  && advancedSummaryStyle.visibility !== 'hidden'
                ),
                tuneButtonVisible: Boolean(
                  tuneButtonRect
                  && tuneButtonRect.width > 0
                  && tuneButtonRect.height > 0
                ),
                optionalNoteExists: Boolean(optionalNote),
                nextStepDrawerExists: Boolean(nextStepDrawer),
                savebarBottom: savebarRect ? Math.round(savebarRect.bottom) : 0,
              };
            }
            """
        )
        assert layout["shellWidth"] <= layout["viewportWidth"] + 1
        assert layout["heroWidth"] <= layout["viewportWidth"] + 1
        assert layout["heroBottom"] > 0
        assert layout["bodyTop"] > layout["heroBottom"]
        assert 150 <= layout["mediaHeight"] <= 320
        assert 0 < layout["headlineTop"] < layout["visualConsoleTop"]
        assert layout["headlineReadVisible"] is False
        assert layout["actionsDisplay"] == "grid"
        assert "px" in layout["actionsColumns"]
        assert layout["summaryDisplay"] == "grid"
        if layout["headlineReadVisible"]:
            assert 1 <= layout["summaryColumns"] <= 2
            assert layout["summaryScrollWidth"] <= layout["summaryClientWidth"] + 1
        assert layout["summaryBoxCount"] == 4
        if layout["summaryShortestCard"] > 0:
            assert layout["summaryShortestCard"] >= 68
            assert layout["summaryTallestCard"] <= 112
        assert layout["summaryMaxRight"] <= layout["viewportWidth"] + 1
        assert 0 < layout["decisionTop"] < layout["sectionsTop"]
        assert layout["secondarySectionCount"] >= 2
        assert layout["closedSecondarySections"] >= 2
        assert layout["visibleSecondarySummaries"] >= 2
        assert 150 <= layout["feedbackHeight"] <= min(420, layout["viewportHeight"] - 54)
        assert 0 < layout["savebarBottom"] <= layout["feedbackBottom"] + 1
        assert layout["feedbackOverflowY"] == "visible"
        assert layout["feedbackScrolls"] is False
        assert layout["fineTuneOpen"] is False
        assert layout["advancedSummaryVisible"] is False
        assert layout["tuneButtonVisible"] is True
        assert layout["optionalNoteExists"] is False
        assert layout["nextStepDrawerExists"] is False
        page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled", caret="hide")
        assert screenshot_path.exists() and screenshot_path.stat().st_size > 20_000

        request_button = page.get_by_role("button", name=re.compile("Request walkthrough", re.I)).first
        request_button.click()
        _choose_research_visual_style(page, accept_external_processing=True)
        page.wait_for_timeout(500)
        assert len(visual_requests) == 1
        payload = visual_requests[0]
        assert payload["request_kind"] == "flythrough"
        assert payload["auto_deliver"] is False
        assert payload["allow_floorplan_only"] is True
        assert payload["external_processing_consent_granted"] is True
        assert "urban jungle" in str(payload["diorama_style_hint"]).lower()
        assert payload["run_id"] == "run-42"
        assert str(payload["property_url"]).endswith("/listing-url-only-loft")
        expect(page.locator("[data-prd-visual-status]")).to_contain_text("queued after your request")
        expect(page.locator("[data-prd-visual-eta]")).to_contain_text("about 10 min")
        rail_fill = page.locator("[data-prd-visual-progress]").first.get_attribute("style") or ""
        assert "18%" in rail_fill
        poll_deadline = time.monotonic() + 5.0
        while visual_status_polls < 1 and time.monotonic() < poll_deadline:
            page.wait_for_timeout(100)
        assert visual_status_polls >= 1
        expect(page.locator("[data-prd-visual-status]")).to_contain_text("Walkthrough is ready.")
        updated_button = page.get_by_role("button", name=re.compile("Open walkthrough", re.I)).first
        expect(updated_button).to_be_visible()
        updated_href = str(updated_button.get_attribute("data-pw-visual-href") or "").strip()
        assert updated_href
        assert updated_href.endswith("/tours/listing-url-only-loft?pane=flythrough-pane&autoplay=1")
    finally:
        context.close()


def test_propertyquarry_visual_request_does_not_invent_eta_before_backend_supplies_one(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []

    def _capture_visual_request(route) -> None:
        payload = route.request.post_data_json or {}
        visual_requests.append(payload if isinstance(payload, dict) else {})
        time.sleep(0.8)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-06-21T10:00:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Listing URL only loft",
                    "request_kind": visual_requests[-1].get("request_kind", "flythrough"),
                    "tour_url": "",
                    "flythrough_url": "",
                    "flythrough_status": "pending",
                    "status_label": "Walkthrough queued",
                    "status_detail": "Walkthrough is queued after your request.",
                    "eta_label": "",
                    "progress_pct": 18,
                    "poll_after_seconds": 0,
                    "delivery_status": "skipped",
                    "blocked_reason": "",
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        row = page.locator("[data-workbench-row]", has_text="Listing URL only loft").first
        row.wait_for(timeout=5000)
        packet_href = str(row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"
        response = page.goto(packet_url, wait_until="domcontentloaded")
        assert response is not None and response.ok
        request_button = page.get_by_role("button", name=re.compile("Request walkthrough", re.I)).first
        request_button.click()
        _choose_research_visual_style(page, accept_external_processing=True)
        page.wait_for_timeout(900)
        assert visual_requests
        assert visual_requests[-1]["external_processing_consent_granted"] is True
        assert "urban jungle" in str(visual_requests[-1].get("diorama_style_hint") or "").lower()
        expect(page.locator("[data-prd-visual-status]")).to_contain_text("queued after your request", timeout=5000)
        assert (page.locator("[data-prd-visual-eta]").inner_text() or "").strip() == ""
    finally:
        context.close()


def test_propertyquarry_3d_tour_request_is_user_initiated_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls = 0
    visual_status_queries: list[dict[str, str]] = []
    console_errors: list[str] = []
    post_identity = {
        "run_id": "run-42-canonical",
        "candidate_ref": "family-tiergarten-canonical",
        "source_ref": "immobilienscout24:family-tiergarten-canonical",
        "property_url": "https://canonical.example.test/properties/family-tiergarten",
    }
    pending_identity = {
        "run_id": "run-42-rendering",
        "candidate_ref": "family-tiergarten-rendering",
        "source_ref": "immobilienscout24:family-tiergarten-rendering",
        "property_url": "https://canonical.example.test/properties/family-tiergarten-rendering",
    }

    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    def _capture_visual_request(route) -> None:
        payload = route.request.post_data_json or {}
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-02T10:00:00+00:00",
                    "status": "created",
                    "property_url": post_identity["property_url"],
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_url": "",
                    "tour_status": "pending",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "3D tour queued",
                    "status_detail": "3D tour is queued after your request.",
                    "eta_label": "about 10 min",
                    "progress_pct": 16,
                    "poll_after_seconds": 1,
                    "delivery_status": "queued",
                    "blocked_reason": "",
                    "source_ref": post_identity["source_ref"],
                    "run_id": post_identity["run_id"],
                    "candidate_ref": post_identity["candidate_ref"],
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)

    def _capture_visual_status(route) -> None:
        nonlocal visual_status_polls
        visual_status_polls += 1
        parsed_query = urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query)
        visual_status_queries.append({key: str(values[-1]) for key, values in parsed_query.items() if values})
        if visual_status_polls == 1:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "generated_at": "2026-07-02T10:00:02+00:00",
                        "status": "processing",
                        "property_url": pending_identity["property_url"],
                        "title": "Family flat near Tiergarten",
                        "request_kind": "tour",
                        "tour_url": "",
                        "tour_status": "processing",
                        "flythrough_url": "",
                        "flythrough_status": "",
                        "status_label": "3D tour rendering",
                        "status_detail": "3D tour is rendering.",
                        "eta_label": "about 5 min",
                        "progress_pct": 58,
                        "poll_after_seconds": 1,
                        "source_ref": pending_identity["source_ref"],
                        "run_id": pending_identity["run_id"],
                        "candidate_ref": pending_identity["candidate_ref"],
                    }
                ),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-02T10:00:03+00:00",
                    "status": "ready",
                    "property_url": pending_identity["property_url"],
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_url": f"{base_url}/tours/altbau-u6/control/3dvista",
                    "tour_status": "ready",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "Open 3D tour",
                    "status_detail": "3D tour is ready.",
                    "eta_label": "",
                    "progress_pct": 100,
                    "poll_after_seconds": 0,
                    "source_ref": pending_identity["source_ref"],
                    "run_id": pending_identity["run_id"],
                    "candidate_ref": pending_identity["candidate_ref"],
                }
            ),
        )

    page.route("**/app/api/signals/property/visual-status?**", _capture_visual_status)
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok

        family_row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        expect(family_row).to_be_visible()
        packet_href = str(family_row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"

        response = page.goto(packet_url, wait_until="domcontentloaded")
        assert response is not None and response.ok
        request_button = page.get_by_role("button", name="Request 3D tour").first
        expect(request_button).to_be_visible()

        request_button.click()
        _choose_research_visual_style(page)
        expect(page.locator("[data-prd-visual-status]")).to_contain_text("queued after your request", timeout=5000)

        assert len(visual_requests) == 1
        payload = visual_requests[0]
        assert payload["request_kind"] == "tour"
        assert payload["auto_deliver"] is False
        assert payload["allow_floorplan_only"] is True
        assert payload["walkthrough_provider_key"] == ""
        assert "urban jungle" in str(payload["diorama_style_hint"] or "").lower()
        assert payload["run_id"] == "run-42"
        assert str(payload["property_url"]).endswith("/family-tiergarten")
        expect(page.locator("[data-prd-visual-eta]")).to_contain_text("about 10 min", timeout=5000)
        rail_fill = page.locator("[data-prd-visual-progress]").first.get_attribute("style") or ""
        assert "16%" in rail_fill or "100%" in rail_fill

        expect(page.locator("[data-prd-visual-status]")).to_contain_text("3D tour is ready.", timeout=15_000)
        assert visual_status_polls >= 2
        assert visual_status_queries[0] == {
            "run_id": post_identity["run_id"],
            "request_kind": "tour",
            "candidate_ref": post_identity["candidate_ref"],
            "source_ref": post_identity["source_ref"],
            "property_url": post_identity["property_url"],
        }
        assert visual_status_queries[1] == {
            "run_id": pending_identity["run_id"],
            "request_kind": "tour",
            "candidate_ref": pending_identity["candidate_ref"],
            "source_ref": pending_identity["source_ref"],
            "property_url": pending_identity["property_url"],
        }
        updated_button = page.get_by_role("button", name="Open 3D tour").first
        expect(updated_button).to_be_visible()
        updated_href = str(updated_button.get_attribute("data-pw-visual-href") or "").strip()
        assert updated_href
        assert updated_href.endswith("/tours/altbau-u6/control/3dvista")
        with page.expect_navigation(wait_until="domcontentloaded"):
            updated_button.click()
        page.locator("h1").wait_for()
        assert page.locator("body", has_text="Altbau near U6").is_visible()
        assert page.locator(".badge").inner_text().lower() == "3d tour"
        provider_frame = page.frame_locator("#provider-frame")
        expect(provider_frame.locator("#tour-viewer")).to_have_text("3D tour ready")
        assert provider_frame.locator("body").evaluate(
            "() => Boolean(window.TDVPlayer && window.TDVPlayer.ready)"
        )
        assert page.locator("#load-provider").count() == 0
        expected_provider_src = "/tours/3dvista/altbau-u6/3dvista/index.htm"
        expect(page.locator("#provider-frame")).to_have_attribute("src", expected_provider_src, timeout=5000)
        expect(page.locator("#provider-frame")).to_have_attribute("data-src", expected_provider_src)
        response = page.goto(packet_url, wait_until="domcontentloaded")
        assert response is not None and response.ok
        expect(page.locator("[data-property-research-detail]")).to_be_visible()
        expect(page.locator("[data-prd-visual-status]")).to_contain_text("3D tour is ready.", timeout=5000)
        expect(page.get_by_role("button", name="Open 3D tour").first).to_be_visible()
        expect(page.get_by_role("button", name="Request 3D tour")).to_have_count(0)
        noisy_console_errors = [
            message
            for message in console_errors
            if (
                "decode" in message.lower()
                or "media" in message.lower()
                or "refused" in message.lower()
                or "failed to load resource" in message.lower()
            )
            and not _is_expected_vendor_report_only_inline_script_console_error(message)
        ]
        assert not noisy_console_errors, noisy_console_errors
    finally:
        context.close()


def test_propertyquarry_visual_status_poll_stops_and_refreshes_without_duplicate_request(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls = 0

    def _capture_visual_request(route) -> None:
        payload = route.request.post_data_json or {}
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-02T10:00:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_url": "",
                    "tour_status": "pending",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "3D tour queued",
                    "status_detail": "3D tour is queued after your request.",
                    "eta_label": "about 10 min",
                    "progress_pct": 16,
                    "poll_after_seconds": 1,
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    def _fail_visual_status(route) -> None:
        nonlocal visual_status_polls
        visual_status_polls += 1
        route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps({"detail": "visual status temporarily unavailable"}),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    page.route("**/app/api/signals/property/visual-status?**", _fail_visual_status)
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        family_row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        expect(family_row).to_be_visible()
        packet_href = str(family_row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"

        response = page.goto(packet_url, wait_until="domcontentloaded")
        assert response is not None and response.ok
        request_button = page.get_by_role("button", name="Request 3D tour").first
        expect(request_button).to_be_visible()
        request_button.click()
        _choose_research_visual_style(page)

        status = page.locator("[data-prd-visual-status]")
        expect(status).to_contain_text("Could not refresh 3D tour status", timeout=8_000)
        refresh_button = page.get_by_role("button", name="Refresh 3D tour status").first
        expect(refresh_button).to_be_visible()
        expect(refresh_button).to_be_enabled()
        assert len(visual_requests) == 1
        assert visual_status_polls == 3

        page.wait_for_timeout(1_200)
        assert visual_status_polls == 3
        with page.expect_response("**/app/api/signals/property/visual-status?**"):
            refresh_button.click()
        expect(status).to_contain_text("Could not refresh 3D tour status", timeout=3_000)
        assert visual_status_polls == 4
        assert len(visual_requests) == 1
    finally:
        context.close()


def test_propertyquarry_research_detail_never_shows_fake_open_tour_for_generated_reconstruction_status(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls = 0

    def _capture_visual_request(route) -> None:
        payload = route.request.post_data_json or {}
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-08T10:00:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_url": "",
                    "tour_status": "pending",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "3D tour queued",
                    "status_detail": "3D tour is queued after your request.",
                    "eta_label": "about 10 min",
                    "progress_pct": 16,
                    "poll_after_seconds": 1,
                    "delivery_status": "queued",
                    "blocked_reason": "",
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    def _capture_visual_status(route) -> None:
        nonlocal visual_status_polls
        visual_status_polls += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-08T10:00:03+00:00",
                    "status": "ready",
                    "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_url": "https://propertyquarry.com/tours/generated-layout-tiergarten",
                    "open_tour_url": "",
                    "tour_status": "ready",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "Open 3D tour",
                    "status_detail": "3D tour is ready.",
                    "eta_label": "",
                    "progress_pct": 100,
                    "poll_after_seconds": 0,
                    "source_ref": "immobilienscout24:family-tiergarten",
                    "run_id": "run-42",
                    "candidate_ref": "family-tiergarten",
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    page.route("**/app/api/signals/property/visual-status?**", _capture_visual_status)
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok

        family_row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        expect(family_row).to_be_visible()
        packet_href = str(family_row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"

        response = page.goto(packet_url, wait_until="networkidle")
        assert response is not None and response.ok
        page.wait_for_timeout(400)
        expect(page.get_by_role("button", name="Open 3D tour")).to_have_count(0)
        fallback_button = page.get_by_role("button", name=re.compile("(Request|Retry) 3D tour", re.I)).first
        expect(fallback_button).to_be_visible()
        assert str(fallback_button.get_attribute("data-pw-visual-href") or "").strip() == ""

        fallback_button.click()
        _choose_research_visual_style(page)
        poll_deadline = time.monotonic() + 5.0
        while visual_status_polls < 1 and time.monotonic() < poll_deadline:
            page.wait_for_timeout(100)
        assert len(visual_requests) == 1
        assert visual_requests[0]["request_kind"] == "tour"
        assert visual_status_polls >= 1
        expect(page.get_by_role("button", name="Open 3D tour")).to_have_count(0)
        refreshed_button = page.get_by_role("button", name=re.compile("(Request|Retry) 3D tour", re.I)).first
        expect(refreshed_button).to_be_visible()
        assert str(refreshed_button.get_attribute("data-pw-visual-href") or "").strip() == ""
    finally:
        context.close()


def test_propertyquarry_research_detail_promotes_ready_generated_reconstruction_status_to_open_layout_tour(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    bundle_root = Path(str(propertyquarry_browser_server["bundle_root"]))
    _write_generated_reconstruction_public_launch_fixture(bundle_root, slug="generated-layout-tiergarten")
    context = _new_context(browser, mobile=True, width=390, height=844)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls = 0
    generated_layout_url = f"{base_url}/tours/generated-layout-tiergarten"

    def _capture_visual_request(route) -> None:
        payload = route.request.post_data_json or {}
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-08T10:00:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_url": "",
                    "tour_status": "pending",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "3D tour queued",
                    "status_detail": "3D tour is queued after your request.",
                    "eta_label": "about 10 min",
                    "progress_pct": 16,
                    "poll_after_seconds": 1,
                    "delivery_status": "queued",
                    "blocked_reason": "",
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    def _capture_visual_status(route) -> None:
        nonlocal visual_status_polls
        visual_status_polls += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-08T10:00:03+00:00",
                    "status": "ready",
                    "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_media_mode": "generated_reconstruction",
                    "provider_key": "generated_reconstruction",
                    "tour_url": generated_layout_url,
                    "open_tour_url": "",
                    "generated_reconstruction_url": generated_layout_url,
                    "tour_status": "ready",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "Open AI-generated 3D tour",
                    "status_detail": (
                        "AI-generated from the floor plan and listing photos. "
                        "It is an interactive spatial aid, not a captured 360° tour."
                    ),
                    "eta_label": "",
                    "progress_pct": 100,
                    "poll_after_seconds": 0,
                    "source_ref": "immobilienscout24:family-tiergarten",
                    "run_id": "run-42",
                    "candidate_ref": "family-tiergarten",
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    page.route("**/app/api/signals/property/visual-status?**", _capture_visual_status)
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok

        family_row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        expect(family_row).to_be_visible()
        packet_href = str(family_row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"

        response = page.goto(packet_url, wait_until="networkidle")
        assert response is not None and response.ok
        page.wait_for_timeout(400)
        fallback_button = page.get_by_role("button", name=re.compile("(Request|Retry) 3D tour", re.I)).first
        expect(fallback_button).to_be_visible()
        fallback_button.click()
        _choose_research_visual_style(page)
        poll_deadline = time.monotonic() + 5.0
        while visual_status_polls < 1 and time.monotonic() < poll_deadline:
            page.wait_for_timeout(100)

        assert len(visual_requests) == 1
        assert visual_requests[0]["request_kind"] == "tour"
        assert visual_status_polls >= 1
        expect(page.locator("[data-prd-visual-status]")).to_contain_text(
            (
                "AI-generated from the floor plan and listing photos. "
                "It is an interactive spatial aid, not a captured 360° tour."
            ),
            timeout=5000,
        )
        expect(page.get_by_role("button", name=re.compile("(Request|Retry) 3D tour", re.I))).to_have_count(0)
        ready_button = page.get_by_role("button", name="Open AI-generated 3D tour").first
        expect(ready_button).to_be_visible()
        assert str(ready_button.get_attribute("data-pw-visual-href") or "").strip() == generated_layout_url

        with page.expect_navigation(wait_until="domcontentloaded"):
            ready_button.click()
        _assert_generated_reconstruction_v4_viewer(
            page,
            slug="generated-layout-tiergarten",
            style_id="urban_jungle",
        )
        public_text = page.locator("body").inner_text().lower()
        assert "generated reconstruction" in public_text
        assert "room route" in public_text
        assert "urban jungle" in public_text
        assert "tour unavailable" not in public_text
    finally:
        context.close()


def test_propertyquarry_research_detail_surfaces_generated_diorama_and_layout_tour_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import landing_property_research

    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)

    candidate = {
        "title": "Generated reconstruction loft",
        "summary": "EUR 1,950 · 76 m² · 1020 Wien",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/generated-reconstruction-loft",
        "source_ref": "willhaben:generated-reconstruction-loft",
        "tour_status": "ready",
        "tour_media_mode": "generated_reconstruction",
        "provider_key": "generated_reconstruction",
        "tour_url": "https://propertyquarry.com/tours/generated-reconstruction-loft",
        "generated_reconstruction_url": "https://propertyquarry.com/tours/generated-reconstruction-loft",
        "property_facts": {
            "price_eur": 1950.0,
            "area_m2": 76.0,
            "postal_name": "1020 Wien",
        },
    }

    def _fake_run_status(self, *, principal_id: str, run_id: str):
        if principal_id == "pq-greenfield-browser" and run_id == "run-generated-reconstruction-browser":
            return {
                "run_id": run_id,
                "principal_id": principal_id,
                "status_url": f"/app/api/signals/property/search/run/{run_id}",
                "status": "processed",
                "progress": 100,
                "message": "done",
                "summary": {
                    "sources_total": 1,
                    "listing_total": 1,
                    "ranked_candidates": [candidate],
                    "sources": [
                        {
                            "source_label": "Willhaben | Austria | Rent | 1020 Vienna",
                            "listing_total": 1,
                            "top_candidates": [candidate],
                        }
                    ],
                },
                "events": [],
            }
        return original_get_run_status(self, principal_id=principal_id, run_id=run_id)

    original_get_run_status = ProductService.get_property_search_run_status
    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_run_status)
    monkeypatch.setattr(landing_property_research, "_property_investment_research_snapshot", lambda **kwargs: {})

    validated_tour_principals: list[tuple[str, str]] = []

    def _verified_tour_url(_url: str, *, principal_id: str = "") -> str:
        validated_tour_principals.append(("verified", principal_id))
        assert principal_id == "pq-greenfield-browser"
        return ""

    def _generated_tour_url(_url: str, *, principal_id: str = "") -> str:
        validated_tour_principals.append(("generated", principal_id))
        assert principal_id == "pq-greenfield-browser"
        return f"{base_url}/tours/generated-reconstruction-loft"

    monkeypatch.setattr(
        landing_property_research.property_tour_hosting,
        "_hosted_property_tour_verified_open_url",
        _verified_tour_url,
    )
    monkeypatch.setattr(
        landing_property_research.property_tour_hosting,
        "_hosted_property_tour_first_party_open_url",
        _generated_tour_url,
    )
    monkeypatch.setattr(
        landing_property_research.property_tour_hosting,
        "_hosted_property_tour_generated_reconstruction_bundle_ready",
        lambda _url: True,
    )
    monkeypatch.setattr(
        landing_routes,
        "_hosted_property_tour_telegram_preview_image_url_for_style",
        lambda _tour_url, *, diorama_style_hint="": "https://cdn.example.test/generated-reconstruction-diorama.png",
    )

    packet_ref = landing_property_research._property_candidate_ref(
        {
            **candidate,
            "source_label": "Willhaben | Austria | Rent | 1020 Vienna",
        }
    )

    bundle_root = Path(str(propertyquarry_browser_server["bundle_root"]))
    _write_generated_reconstruction_public_launch_fixture(bundle_root, slug="generated-reconstruction-loft")
    context = _new_context(browser, mobile=True, width=390, height=844)
    _stub_remote_png(
        context,
        url_pattern="https://cdn.example.test/generated-reconstruction-diorama.png",
        label="Generated reconstruction diorama",
    )
    page: Page = context.new_page()
    try:
        _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
        response = page.goto(
            f"{base_url}/app/research/{packet_ref}?run_id=run-generated-reconstruction-browser",
            wait_until="networkidle",
        )
        assert response is not None and response.ok
        assert {kind for kind, _principal_id in validated_tour_principals} == {"verified", "generated"}
        assert {principal_id for _kind, principal_id in validated_tour_principals} == {"pq-greenfield-browser"}
        expect(page.locator("[data-property-research-detail]")).to_be_visible()
        expect(page.locator("[data-prd-hero-image]")).to_have_attribute(
            "src",
            "https://cdn.example.test/generated-reconstruction-diorama.png",
        )
        generated_tour_card = page.locator("[data-prd-visual-card='generated_reconstruction']")
        expect(generated_tour_card).to_be_visible()
        expect(generated_tour_card).to_contain_text("AI-generated 3D tour")
        expect(page.locator(".prd-status-badge")).to_contain_text("AI-generated 3D tour available")
        expect(page.locator("[data-prd-visual-status]")).to_contain_text(
            (
                "AI-generated from the floor plan and listing photos. "
                "It is an interactive spatial aid, not a captured 360° tour."
            )
        )
        expect(page.get_by_role("button", name=re.compile("Request 3D tour", re.I))).to_have_count(0)
        _assert_no_horizontal_overflow(page)
        open_generated_tour = page.get_by_role("link", name="Open AI-generated 3D tour").first
        expect(open_generated_tour).to_have_attribute(
            "href",
            re.compile(r".*/tours/generated-reconstruction-loft$"),
        )
        with page.expect_navigation(wait_until="domcontentloaded"):
            open_generated_tour.click()
        _assert_generated_reconstruction_v4_viewer(
            page,
            slug="generated-reconstruction-loft",
            style_id="urban_jungle",
        )
        public_text = page.locator("body").inner_text().lower()
        assert "generated reconstruction" in public_text
        assert "room route" in public_text
        assert "urban jungle" in public_text
        assert "tour unavailable" not in public_text
        _assert_no_horizontal_overflow(page)
    finally:
        context.close()


def test_propertyquarry_blocked_3d_tour_can_be_retried_from_research_packet_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    original_get_run_status = ProductService.get_property_search_run_status

    blocked_candidate = {
        "title": "Retryable tour loft",
        "summary": "EUR 1,890 · 73 m² · 1070 Wien",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/retryable-tour-loft",
        "source_ref": "willhaben:retryable-tour-loft",
        "tour_status": "blocked",
        "blocked_reason": "property_tour_execution_failed",
        "tour_url": "",
        "flythrough_status": "",
        "flythrough_url": "",
        "fit_summary": "Personal fit 82/100 · shortlist · Floorplan and light fit.",
        "recommendation": "shortlist",
        "match_reasons": ["Floorplan and light fit."],
        "mismatch_reasons": [],
        "property_facts": {
            "price_display": "EUR 1,890",
            "price_eur": 1890.0,
            "rooms": 3,
            "area_m2": 73.0,
            "postal_name": "1070 Wien",
            "has_floorplan": True,
            "floorplan_urls_json": [
                "https://assets.example.test/retryable-tour-loft-floorplan.png"
            ],
        },
    }

    def _fake_retryable_run_status(self, *, principal_id: str, run_id: str):
        if principal_id == "pq-greenfield-browser" and run_id == "run-retry-tour-browser":
            return {
                "run_id": run_id,
                "principal_id": principal_id,
                "status_url": f"/app/api/signals/property/search/run/{run_id}",
                "status": "processed",
                "progress": 100,
                "message": "Property scouting run completed.",
                "summary": {
                    "sources_total": 1,
                    "listing_total": 1,
                    "ranked_candidates": [blocked_candidate],
                    "sources": [
                        {
                            "source_label": "Willhaben | Austria | Rent | 1070 Vienna",
                            "listing_total": 1,
                            "high_fit_total": 1,
                            "top_candidates": [blocked_candidate],
                        }
                    ],
                },
                "events": [],
            }
        return original_get_run_status(self, principal_id=principal_id, run_id=run_id)

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_retryable_run_status)

    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls = 0
    console_errors: list[str] = []

    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    def _capture_visual_request(route) -> None:
        payload = route.request.post_data_json or {}
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-02T11:00:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Retryable tour loft",
                    "request_kind": "tour",
                    "tour_url": "",
                    "tour_status": "pending",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "3D tour queued",
                    "status_detail": "3D tour is queued after your request.",
                    "eta_label": "about 6 min",
                    "progress_pct": 24,
                    "poll_after_seconds": 2,
                    "delivery_status": "queued",
                    "blocked_reason": "",
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)

    def _capture_visual_status(route) -> None:
        nonlocal visual_status_polls
        visual_status_polls += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-02T11:00:03+00:00",
                    "status": "ready",
                    "property_url": blocked_candidate["property_url"],
                    "title": blocked_candidate["title"],
                    "request_kind": "tour",
                    "tour_url": f"{base_url}/tours/altbau-u6/control/3dvista",
                    "tour_status": "ready",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "Open 3D tour",
                    "status_detail": "3D tour is ready.",
                    "eta_label": "",
                    "progress_pct": 100,
                    "poll_after_seconds": 0,
                    "source_ref": str(blocked_candidate["source_ref"]),
                    "run_id": "run-retry-tour-browser",
                    "candidate_ref": "retryable-tour-loft",
                }
            ),
        )

    page.route("**/app/api/signals/property/visual-status?**", _capture_visual_status)
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-retry-tour-browser&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        row = page.locator("[data-workbench-row]", has_text="Retryable tour loft").first
        expect(row).to_be_visible()
        packet_href = str(row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"

        response = page.goto(packet_url, wait_until="commit")
        assert response is not None and response.ok
        retry_button = page.get_by_role("button", name="Retry 3D tour").first
        expect(retry_button).to_be_visible()
        visible_text = page.locator("body").inner_text()
        assert "property_tour_execution_failed" not in visible_text

        retry_button.click()
        _choose_research_visual_style(page, label="Warm Scandinavian")

        assert len(visual_requests) == 1
        payload = visual_requests[0]
        assert payload["request_kind"] == "tour"
        assert payload["run_id"] == "run-retry-tour-browser"
        assert payload["allow_floorplan_only"] is True
        assert "warm scandinavian" in str(payload["diorama_style_hint"] or "").lower()
        expect(page.locator("[data-prd-visual-status]")).to_contain_text("queued after your request", timeout=5000)
        expect(page.locator("[data-prd-visual-eta]")).to_contain_text("about 6 min", timeout=5000)
        rail_fill = page.locator("[data-prd-visual-progress]").first.get_attribute("style") or ""
        assert "24%" in rail_fill or "100%" in rail_fill

        expect(page.locator("[data-prd-visual-status]")).to_contain_text("3D tour is ready.", timeout=15_000)
        assert visual_status_polls >= 1
        updated_button = page.get_by_role("button", name="Open 3D tour").first
        expect(updated_button).to_be_visible()
        with page.expect_navigation(wait_until="domcontentloaded"):
            updated_button.click()
        page.locator("h1").wait_for()
        assert page.locator("body", has_text="Altbau near U6").is_visible()
        assert page.locator("#load-provider").count() == 0
        expected_provider_src = "/tours/3dvista/altbau-u6/3dvista/index.htm"
        expect(page.locator(".provider-frame")).to_have_attribute("src", expected_provider_src, timeout=5000)
        expect(page.locator(".provider-frame")).to_have_attribute("data-src", expected_provider_src)
        noisy_console_errors = [
            message
            for message in console_errors
            if (
                "decode" in message.lower()
                or "media" in message.lower()
                or "refused" in message.lower()
                or "failed to load resource" in message.lower()
            )
            and not _is_expected_vendor_report_only_inline_script_console_error(message)
        ]
        assert not noisy_console_errors, noisy_console_errors
    finally:
        context.close()


def test_propertyquarry_expired_flat_preview_explains_3d_unavailable_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    original_get_run_status = ProductService.get_property_search_run_status
    expired_candidate = {
        "title": "Expired flat-preview apartment",
        "summary": "EUR 1,590 · 68 m² · 1020 Wien",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/expired-flat-preview",
        "source_ref": "willhaben:expired-flat-preview",
        "preview_image_url": "/test-assets/expired-flat-preview.png",
        "tour": {
            "status": "blocked",
            "reason_key": "listing_expired",
            "status_detail": "The source listing is no longer available.",
        },
        "fit_summary": "Personal fit 78/100 · shortlist · Cached listing preview.",
        "recommendation": "shortlist",
        "match_reasons": ["Cached listing preview."],
        "mismatch_reasons": [],
        "property_facts": {
            "price_display": "EUR 1,590",
            "price_eur": 1590.0,
            "rooms": 3,
            "area_m2": 68.0,
            "postal_name": "1020 Wien",
            "has_floorplan": False,
            "has_360": False,
            "media_urls_json": ["/test-assets/expired-flat-preview.png"],
        },
    }

    def _fake_expired_run_status(self, *, principal_id: str, run_id: str):
        if principal_id == "pq-greenfield-browser" and run_id == "run-expired-flat-preview-browser":
            return {
                "run_id": run_id,
                "principal_id": principal_id,
                "status_url": f"/app/api/signals/property/search/run/{run_id}",
                "status": "processed",
                "progress": 100,
                "message": "Property scouting run completed.",
                "summary": {
                    "sources_total": 1,
                    "listing_total": 1,
                    "ranked_candidates": [expired_candidate],
                    "sources": [
                        {
                            "source_label": "Willhaben | Austria | Rent | 1020 Vienna",
                            "listing_total": 1,
                            "high_fit_total": 1,
                            "top_candidates": [expired_candidate],
                        }
                    ],
                },
                "events": [],
            }
        return original_get_run_status(self, principal_id=principal_id, run_id=run_id)

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_expired_run_status)

    context = _new_context(browser, mobile=True, width=390, height=844)
    _stub_remote_png(
        context,
        url_pattern="**/test-assets/expired-flat-preview.png",
        label="Expired flat-preview apartment",
    )
    page: Page = context.new_page()
    try:
        response = page.goto(
            f"{base_url}/app/shortlist?run_id=run-expired-flat-preview-browser&full=1",
            wait_until="networkidle",
        )
        assert response is not None and response.ok
        row = page.locator("[data-workbench-row]", has_text="Expired flat-preview apartment").first
        expect(row).to_be_visible()
        packet_href = str(row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"

        response = page.goto(packet_url, wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.locator("[data-property-research-detail]")).to_be_visible()
        expect(page.locator("[data-prd-hero-image]")).to_be_visible()
        expect(page.locator("[data-prd-hero-image]")).to_have_attribute(
            "src",
            "/test-assets/expired-flat-preview.png",
        )
        visible_text = page.locator("body").inner_text()
        normalized_visible_text = " ".join(visible_text.split())
        assert (
            "The source listing has expired. No floor plan or real 360 source was captured, "
            "so the cached preview cannot become a 3D tour."
        ) in normalized_visible_text
        expect(page.locator("[data-pw-visual-request='tour']")).to_have_count(0)
        expect(page.get_by_role("button", name=re.compile(r"(?:Request|Retry) 3D tour", re.I))).to_have_count(0)
        expect(page.get_by_role("link", name="Open 3D tour")).to_have_count(0)
        assert "listing_expired" not in visible_text
        _assert_no_horizontal_overflow(page)
    finally:
        context.close()


def test_propertyquarry_handoff_3d_tour_request_is_user_initiated_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    container = client.app.state.container
    original_get_handoff = ProductService.get_handoff
    handoff_input = {
        "title": "Family flat near Tiergarten",
        "summary": "Larger layout and quieter block.",
        "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
        "run_id": "run-handoff-tour-browser",
        "candidate_ref": "handoff-family-tiergarten",
        "source_ref": "immobilienscout24:family-tiergarten",
        "personal_fit_assessment": {
            "match_reasons_json": ["Larger layout and quieter block."],
            "mismatch_reasons_json": [],
        },
        "property_facts_json": {
            "price_display": "EUR 465,000",
            "price_eur": 465000.0,
            "rooms": 4,
            "area_m2": 92,
            "postal_name": "Berlin Tiergarten",
        },
        "candidate_properties": [
            {
                "title": "Family flat near Tiergarten",
                "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                "source_ref": "immobilienscout24:family-tiergarten",
                "run_id": "run-handoff-tour-browser",
                "candidate_ref": "handoff-family-tiergarten",
                "tour_url": "",
                "flythrough_url": "",
                "floorplan_url": "https://example.test/floorplan-family-tiergarten.png",
                "property_facts": {
                    "price_display": "EUR 465,000",
                    "price_eur": 465000.0,
                    "rooms": 4,
                    "area_m2": 92,
                    "postal_name": "Berlin Tiergarten",
                },
            }
        ],
    }

    def _fake_get_handoff(self, *, principal_id: str, handoff_ref: str):
        if principal_id == "pq-greenfield-browser" and handoff_ref == "human_task:review-2":
            return HandoffNote(
                id=handoff_ref,
                queue_item_ref="queue:review-2",
                summary="Family flat near Tiergarten",
                owner="office",
                due_time=None,
                escalation_status="high",
                status="pending",
                task_type="property_alert_review",
                property_url="https://www.immobilienscout24.de/expose/family-tiergarten",
                tour_url="",
                source_ref="immobilienscout24:family-tiergarten",
            )
        return original_get_handoff(self, principal_id=principal_id, handoff_ref=handoff_ref)

    monkeypatch.setattr(ProductService, "get_handoff", _fake_get_handoff)
    monkeypatch.setattr(
        container.orchestrator,
        "fetch_human_task",
        lambda task_id, principal_id: type("TaskStub", (), {"input_json": handoff_input})(),
    )
    monkeypatch.setattr(
        container.orchestrator,
        "list_human_task_assignment_history",
        lambda task_id, principal_id, limit=8: (),
    )

    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls = 0
    visual_status_ready = {"value": False}

    def _capture_visual_request(route) -> None:
        payload = route.request.post_data_json or {}
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-02T12:00:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_url": "",
                    "tour_status": "pending",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "3D tour queued",
                    "status_detail": "3D tour is queued after your request.",
                    "eta_label": "about 8 min",
                    "progress_pct": 18,
                    "poll_after_seconds": 2,
                    "delivery_status": "queued",
                    "blocked_reason": "",
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    def _capture_visual_status(route) -> None:
        nonlocal visual_status_polls
        visual_status_polls += 1
        if not visual_status_ready["value"]:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "generated_at": "2026-07-02T12:00:02+00:00",
                        "status": "processing",
                        "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                        "title": "Family flat near Tiergarten",
                        "request_kind": "tour",
                        "tour_url": "",
                        "tour_status": "pending",
                        "status_label": "3D tour queued",
                        "status_detail": "3D tour is queued after your request.",
                        "eta_label": "about 8 min",
                        "progress_pct": 18,
                        "poll_after_seconds": 1,
                        "source_ref": "immobilienscout24:family-tiergarten",
                        "run_id": "run-handoff-tour-browser",
                        "candidate_ref": "handoff-family-tiergarten",
                    }
                ),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-02T12:00:03+00:00",
                    "status": "ready",
                    "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_url": f"{base_url}/tours/altbau-u6/control/3dvista",
                    "tour_status": "ready",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "Open 3D tour",
                    "status_detail": "3D tour is ready.",
                    "eta_label": "",
                    "progress_pct": 100,
                    "poll_after_seconds": 0,
                    "source_ref": "immobilienscout24:family-tiergarten",
                    "run_id": "run-handoff-tour-browser",
                    "candidate_ref": "handoff-family-tiergarten",
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    page.route("**/app/api/signals/property/visual-status?**", _capture_visual_status)
    try:
        response = page.goto(f"{base_url}/app/handoffs/human_task:review-2", wait_until="networkidle")
        assert response is not None and response.ok
        request_button = page.get_by_role("button", name="Request 3D tour").first
        expect(request_button).to_be_visible()

        request_button.click()
        _choose_object_visual_style(page, label="Warm Scandinavian")
        page.wait_for_timeout(750)

        assert len(visual_requests) == 1
        payload = visual_requests[0]
        assert payload["request_kind"] == "tour"
        assert payload["auto_deliver"] is False
        assert payload["allow_floorplan_only"] is True
        assert payload["walkthrough_provider_key"] == ""
        assert payload["run_id"] == "run-handoff-tour-browser"
        assert payload["candidate_ref"] == "handoff-family-tiergarten"
        assert "warm scandinavian" in str(payload["diorama_style_hint"] or "").lower()
        expect(page.locator("[data-object-visual-status]")).to_contain_text("queued after your request", timeout=5000)
        expect(page.locator("[data-object-visual-eta]")).to_contain_text("about 8 min", timeout=5000)
        rail_fill = page.locator("[data-object-visual-progress]").first.get_attribute("style") or ""
        assert "18%" in rail_fill or "100%" in rail_fill

        visual_status_ready["value"] = True
        expect(page.locator("[data-object-visual-status]")).to_contain_text("3D tour is ready.", timeout=5000)
        assert visual_status_polls >= 1
        updated_button = page.get_by_role("button", name="Open 3D tour").first
        expect(updated_button).to_be_visible()
        updated_href = str(updated_button.get_attribute("data-obj-visual-href") or "").strip()
        assert updated_href.endswith("/tours/altbau-u6/control/3dvista")
        with page.expect_navigation(wait_until="domcontentloaded"):
            updated_button.click()
        page.locator("h1").wait_for()
        assert page.locator("body", has_text="Altbau near U6").is_visible()
        assert page.locator("#load-provider").count() == 0
        expected_provider_src = "/tours/3dvista/altbau-u6/3dvista/index.htm"
        expect(page.locator(".provider-frame")).to_have_attribute("src", expected_provider_src, timeout=5000)
        expect(page.locator(".provider-frame")).to_have_attribute("data-src", expected_provider_src)
    finally:
        context.close()


def test_propertyquarry_handoff_generated_layout_tour_request_promotes_to_open_layout_tour_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    bundle_root = Path(str(propertyquarry_browser_server["bundle_root"]))
    generated_layout_slug = "generated-layout-handoff-review-2"
    generated_layout_url = f"{base_url}/tours/{generated_layout_slug}"
    _write_generated_reconstruction_public_launch_fixture(
        bundle_root,
        slug=generated_layout_slug,
        style_id="warm_scandi",
    )
    client = propertyquarry_browser_server["client"]
    container = client.app.state.container
    original_get_handoff = ProductService.get_handoff
    handoff_input = {
        "title": "Family flat near Tiergarten",
        "summary": "Larger layout and quieter block.",
        "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
        "run_id": "run-handoff-layout-tour-browser",
        "candidate_ref": "handoff-layout-tiergarten",
        "source_ref": "immobilienscout24:family-tiergarten",
        "personal_fit_assessment": {
            "match_reasons_json": ["Larger layout and quieter block."],
            "mismatch_reasons_json": [],
        },
        "property_facts_json": {
            "price_display": "EUR 465,000",
            "price_eur": 465000.0,
            "rooms": 4,
            "area_m2": 92,
            "postal_name": "Berlin Tiergarten",
        },
        "candidate_properties": [
            {
                "title": "Family flat near Tiergarten",
                "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                "source_ref": "immobilienscout24:family-tiergarten",
                "run_id": "run-handoff-layout-tour-browser",
                "candidate_ref": "handoff-layout-tiergarten",
                "tour_url": "",
                "flythrough_url": "",
                "floorplan_url": "https://example.test/floorplan-family-tiergarten.png",
                "property_facts": {
                    "price_display": "EUR 465,000",
                    "price_eur": 465000.0,
                    "rooms": 4,
                    "area_m2": 92,
                    "postal_name": "Berlin Tiergarten",
                },
            }
        ],
    }

    def _fake_get_handoff(self, *, principal_id: str, handoff_ref: str):
        if principal_id == "pq-greenfield-browser" and handoff_ref == "human_task:review-2":
            return HandoffNote(
                id=handoff_ref,
                queue_item_ref="queue:review-2",
                summary="Family flat near Tiergarten",
                owner="office",
                due_time=None,
                escalation_status="high",
                status="pending",
                task_type="property_alert_review",
                property_url="https://www.immobilienscout24.de/expose/family-tiergarten",
                tour_url="",
                source_ref="immobilienscout24:family-tiergarten",
            )
        return original_get_handoff(self, principal_id=principal_id, handoff_ref=handoff_ref)

    monkeypatch.setattr(ProductService, "get_handoff", _fake_get_handoff)
    monkeypatch.setattr(
        container.orchestrator,
        "fetch_human_task",
        lambda task_id, principal_id: type("TaskStub", (), {"input_json": handoff_input})(),
    )
    monkeypatch.setattr(
        container.orchestrator,
        "list_human_task_assignment_history",
        lambda task_id, principal_id, limit=8: (),
    )

    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls = 0

    def _capture_visual_request(route) -> None:
        payload = route.request.post_data_json or {}
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-08T12:00:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_url": "",
                    "tour_status": "pending",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "3D tour queued",
                    "status_detail": "3D tour is queued after your request.",
                    "eta_label": "about 8 min",
                    "progress_pct": 18,
                    "poll_after_seconds": 2,
                    "delivery_status": "queued",
                    "blocked_reason": "",
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    def _capture_visual_status(route) -> None:
        nonlocal visual_status_polls
        visual_status_polls += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-08T12:00:03+00:00",
                    "status": "blocked",
                    "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_media_mode": "generated_reconstruction",
                    "provider_key": "generated_reconstruction",
                    "tour_url": "",
                    "open_tour_url": "",
                    "verified_tour_url": "",
                    "generated_reconstruction_url": generated_layout_url,
                    "layout_preview_url": generated_layout_url,
                    "layout_preview_status": "ready",
                    "tour_status": "blocked",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "Open AI-generated 3D tour",
                    "status_detail": (
                        "AI-generated from the floor plan and listing photos. "
                        "It is an interactive spatial aid, not a captured 360° tour."
                    ),
                    "eta_label": "",
                    "progress_pct": 0,
                    "poll_after_seconds": 0,
                    "blocked_reason": "listing_360_media_missing",
                    "source_ref": "immobilienscout24:family-tiergarten",
                    "run_id": "run-handoff-layout-tour-browser",
                    "candidate_ref": "handoff-layout-tiergarten",
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    page.route("**/app/api/signals/property/visual-status?**", _capture_visual_status)
    try:
        response = page.goto(f"{base_url}/app/handoffs/human_task:review-2", wait_until="networkidle")
        assert response is not None and response.ok
        request_button = page.get_by_role("button", name="Request 3D tour").first
        expect(request_button).to_be_visible()

        request_button.click()
        _choose_object_visual_style(page, label="Warm Scandinavian")
        page.wait_for_timeout(750)

        assert len(visual_requests) == 1
        payload = visual_requests[0]
        assert payload["request_kind"] == "tour"
        assert payload["auto_deliver"] is False
        assert payload["allow_floorplan_only"] is True
        assert payload["walkthrough_provider_key"] == ""
        assert payload["run_id"] == "run-handoff-layout-tour-browser"
        assert payload["candidate_ref"] == "handoff-layout-tiergarten"
        assert "warm scandinavian" in str(payload["diorama_style_hint"] or "").lower()
        expect(page.locator("[data-object-visual-status]")).to_contain_text("queued after your request", timeout=5000)

        expect(page.locator("[data-object-visual-status]")).to_contain_text(
            (
                "AI-generated from the floor plan and listing photos. "
                "It is an interactive spatial aid, not a captured 360° tour."
            ),
            timeout=5000,
        )
        assert visual_status_polls >= 1
        expect(page.locator("[data-object-visual-label]")).to_have_text(
            "AI-generated 3D tour",
            timeout=5000,
        )
        updated_button = page.get_by_role("button", name="Open AI-generated 3D tour").first
        expect(updated_button).to_be_visible()
        updated_href = str(updated_button.get_attribute("data-obj-visual-href") or "").strip()
        assert updated_href == generated_layout_url
        with page.expect_navigation(wait_until="domcontentloaded"):
            updated_button.click()
        _assert_generated_reconstruction_v4_viewer(
            page,
            slug=generated_layout_slug,
            style_id="warm_scandi",
        )
        public_text = page.locator("body").inner_text().lower()
        assert "generated reconstruction" in public_text
        assert "room route" in public_text
        assert "warm scandinavian" in public_text
        assert "tour unavailable" not in public_text
    finally:
        context.close()


def test_propertyquarry_setup_wizard_changes_visible_controls_and_collapses_all_vienna(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        page.set_default_timeout(10000)
        response = page.goto(f"{base_url}/app/properties", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        page.locator('[data-property-field-name="country_code"]').wait_for(state="visible")
        assert page.locator('[data-property-field-name="country_code"]').is_visible()
        assert page.locator('[data-property-field-name="location_query"]').is_visible()

        page.select_option('select[name="country_code"]', "AT")
        assert page.locator('[data-property-field-name="region_code"]').is_visible()
        assert page.locator('[data-property-field-name="location_query"]').is_visible()

        page.get_by_role("button", name="Select all areas", exact=True).click()
        total_areas = page.locator('input[name="location_query"]').count()
        assert total_areas > 0
        assert page.locator('input[name="location_query"]:checked').count() == total_areas
        assert page.locator('input[name="full_region_scope"]').is_checked()

        page.locator('[data-checkbox-group-clear-all="location_query"]').click()
        assert page.locator('[data-property-field-name="location_query"]').is_visible()
        assert page.locator('input[name="location_query"]:checked').count() == 0
        assert page.locator('input[name="full_region_scope"]').is_checked() is False

        page.locator("[data-property-step-next]").click()
        assert page.locator('[data-property-field-name="country_code"]').is_hidden()
        assert page.locator('[data-property-field-name="region_code"]').is_hidden()
        assert page.locator('[data-property-field-name="location_query"]').is_hidden()
        assert page.locator('[data-property-field-name="property_type"]').is_visible()

        page.locator('[data-property-step-trigger="children"]').click()
        assert page.locator('[data-property-field-name="school_stage_preferences"]').count() >= 1
        assert page.locator('[data-property-what-matters-panel]').is_visible()
        assert page.locator('details[data-property-advanced-panel="children"]').count() == 0
        assert page.locator('details[data-property-advanced-panel="children_distances"]').count() == 0

        page.locator('[data-property-step-trigger="search"]').click()
        page.select_option('select[name="region_code"]', "lower_austria")
        page.locator('[data-property-step-trigger="children"]').click()

        page.locator('[data-property-step-trigger="reachability"]').click()
        page.locator('input[name="enable_commute_research"]').wait_for(state="visible")
        assert page.locator('input[name="enable_commute_research"]').is_visible()
        assert page.locator('details[data-property-advanced-panel="commute"]').is_hidden()
        page.locator('input[name="enable_commute_research"]').check()
        assert page.locator('details[data-property-advanced-panel="commute"]').is_visible()
        page.locator('details[data-property-advanced-panel="commute"] summary').click()
        assert page.locator('[data-property-field-name="commute_destination"]').is_visible()
        assert page.locator('[data-property-field-name="preferred_reachability_modes"]').is_visible()
        page.locator('input[name="enable_commute_research"]').uncheck()
        assert page.locator('details[data-property-advanced-panel="commute"]').is_hidden()

        page.locator('[data-property-step-trigger="children"]').click()
        assert page.locator('[data-property-what-matters-panel]').is_visible()
        page.locator('details[data-what-matters-group]').evaluate_all(
            "groups => groups.forEach((group) => { group.open = true; })"
        )
        assert page.locator('[data-keyword-priority-row][data-keyword-value="market nearby"]').is_visible()
        assert page.locator('[data-keyword-priority-row][data-keyword-value="good air quality"]').is_visible()
        assert page.locator('[data-keyword-priority-row][data-keyword-value="avoid flood-risk area"]').is_visible()

        page.locator('[data-property-step-trigger="providers"]').click()
        assert page.locator('label', has_text="Willhaben").count() >= 1
        assert page.locator('label', has_text="ImmoScout24 Austria").count() >= 1
        assert page.locator('label', has_text="Zillow").count() == 0
        assert page.locator('input[name="min_match_score"]').count() == 0
        assert page.locator('[data-range-value-for="min_match_score"]').count() == 0
        floorplan_filter = page.locator('input[name="require_floorplan"]')
        assert floorplan_filter.is_visible()
        assert page.locator('label', has_text="Serious listings only").count() >= 1
    finally:
        context.close()


def test_propertyquarry_search_setup_fits_desktop_viewport_and_captures_screenshots(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    desktop = _new_context(browser, mobile=False, width=1600, height=1200)
    mobile = _new_context(browser, mobile=True)
    desktop_shot = tmp_path / "property_search_setup_desktop.png"
    mobile_shot = tmp_path / "property_search_setup_mobile.png"
    try:
        desktop_page = desktop.new_page()
        response = desktop_page.goto(f"{base_url}/app/properties", wait_until="networkidle")
        assert response is not None and response.ok
        desktop_page.locator("[data-workbench-brief-drawer]").wait_for(state="visible")
        desktop_page.screenshot(path=str(desktop_shot), full_page=True)
        metrics = desktop_page.evaluate(
            """() => {
                const drawer = document.querySelector('[data-workbench-brief-drawer]');
                const workflow = document.querySelector('.pqx-workflow');
                const propertyType = document.querySelector('[data-property-field-name="property_type"]');
                const drawerRect = drawer ? drawer.getBoundingClientRect() : null;
                const workflowRect = workflow ? workflow.getBoundingClientRect() : null;
                const propertyTypeRect = propertyType ? propertyType.getBoundingClientRect() : null;
                return {
                    innerHeight: window.innerHeight,
                    scrollHeight: document.scrollingElement ? document.scrollingElement.scrollHeight : 0,
                    drawerBottom: drawerRect ? drawerRect.bottom : 0,
                    workflowHeight: workflowRect ? workflowRect.height : 0,
                    propertyTypeHeight: propertyTypeRect ? propertyTypeRect.height : 0,
                    propertyTypeBottom: propertyTypeRect ? propertyTypeRect.bottom : 0,
                };
            }"""
        )
        assert metrics["drawerBottom"] <= metrics["innerHeight"] + 1
        assert metrics["scrollHeight"] <= metrics["innerHeight"] + 1
        assert metrics["workflowHeight"] <= 84
        assert metrics["propertyTypeHeight"] <= 170
        assert metrics["propertyTypeBottom"] <= metrics["innerHeight"] + 1
        assert desktop_shot.exists()

        mobile_page = mobile.new_page()
        response = mobile_page.goto(f"{base_url}/app/properties", wait_until="networkidle")
        assert response is not None and response.ok
        mobile_page.locator("[data-workbench-brief-drawer]").wait_for(state="visible")
        mobile_page.select_option('select[name="country_code"]', "AT")
        mobile_page.select_option('select[name="region_code"]', "vienna")
        mobile_page.wait_for_function(
            """() => document.querySelector('[data-property-field-name="location_query"]')?.dataset.locationMapAvailable === 'true'"""
        )
        mobile_page.screenshot(path=str(mobile_shot), full_page=True)
        assert mobile_shot.exists()
        mobile_metrics = mobile_page.evaluate(
            """() => {
                const rail = document.querySelector('[data-property-mobile-step-rail]');
                const legacyDock = document.querySelector('[data-property-mobile-action-dock], [data-property-mobile-dock], .pq-mobile-nav');
                const drawer = document.querySelector('[data-workbench-brief-drawer]');
                const result = document.querySelector('[data-workbench-row]');
                const thumb = result?.querySelector('.pqx-thumb');
                const topbar = document.querySelector('.pqx-topbar');
                const topbarRect = topbar ? topbar.getBoundingClientRect() : null;
                const topnav = topbar?.querySelector('.pqx-primary-nav') || null;
                const topnavRect = topnav ? topnav.getBoundingClientRect() : null;
                const topnavFirst = topnav?.querySelector('a, span') || null;
                const topnavFirstRect = topnavFirst ? topnavFirst.getBoundingClientRect() : null;
                const topLaunch = topbar?.querySelector('[data-property-start-top]') || null;
                const topLaunchRect = topLaunch ? topLaunch.getBoundingClientRect() : null;
                const locationField = document.querySelector('[data-property-field-name="location_query"]');
                const customLocationField = document.querySelector('[data-property-field-name="custom_location_query"]');
                const spilloverValueField = document.querySelector('[data-property-field-name="adjacent_area_radius_value"]');
                const spilloverUnitField = document.querySelector('[data-property-field-name="adjacent_area_radius_unit"]');
                const stepActions = document.querySelector('.pqx-step-head-actions');
                const stepActionsRect = stepActions ? stepActions.getBoundingClientRect() : null;
                const stepProgress = stepActions?.querySelector('[data-property-step-progress]') || null;
                const stepProgressRect = stepProgress ? stepProgress.getBoundingClientRect() : null;
                const stepNextButton = stepActions?.querySelector('[data-property-step-next]') || null;
                const stepNextRect = stepNextButton ? stepNextButton.getBoundingClientRect() : null;
                const stepSaveButton = stepActions?.querySelector('[data-property-save-top]') || null;
                const stepSaveRect = stepSaveButton ? stepSaveButton.getBoundingClientRect() : null;
                const stepStatus = stepActions?.querySelector('[data-property-launch-status]') || null;
                const stepStatusRect = stepStatus ? stepStatus.getBoundingClientRect() : null;
                const firstField = document.querySelector('.pqx-shell[data-pqx-surface="search"] .pqx-form-body > .pqx-field:not([hidden])');
                const firstFieldRect = firstField ? firstField.getBoundingClientRect() : null;
                const drawerScrollTopInitial = drawer ? drawer.scrollTop : 0;
                const areaRows = Array.from(locationField?.querySelectorAll('[data-pqx-check-grid="location_query"] .pqx-check') || []);
                const areaGrid = locationField?.querySelector('[data-pqx-check-grid="location_query"]') || null;
                const areaSummary = document.querySelector('[data-location-selected-summary]');
                const areaMapButton = locationField?.querySelector('[data-location-mode-button="map"]') || null;
                const areaListButton = locationField?.querySelector('[data-location-mode-button="list"]') || null;
                const areaMapButtonRect = areaMapButton ? areaMapButton.getBoundingClientRect() : null;
                const areaListButtonRect = areaListButton ? areaListButton.getBoundingClientRect() : null;
                const areaMapLaunch = locationField?.querySelector('[data-location-map-launch]') || null;
                const areaMapOpen = locationField?.querySelector('[data-location-map-open]') || null;
                const areaMapOpenRect = areaMapOpen ? areaMapOpen.getBoundingClientRect() : null;
                const areaSelectAllButton = locationField?.querySelector('[data-checkbox-group-select-all="location_query"]') || null;
                const areaClearButton = locationField?.querySelector('[data-checkbox-group-clear-all="location_query"]') || null;
                const areaDialog = locationField?.querySelector('[data-location-map-dialog]') || null;
                const availableScrollY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
                const requestedScrollY = Math.min(220, availableScrollY);
                window.scrollTo(0, requestedScrollY);
                const pageScrollBeforeMap = Math.round(window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0);
                if (areaMapButton) areaMapButton.click();
                const areaModeInMapValue = locationField ? String(locationField.getAttribute('data-location-mode') || '') : '';
                const areaMapAvailableValue = locationField ? String(locationField.getAttribute('data-location-map-available') || '') : '';
                const areaGridDisplayInMapValue = areaGrid ? window.getComputedStyle(areaGrid).display : '';
                const areaMapLaunchDisplayValue = areaMapLaunch ? window.getComputedStyle(areaMapLaunch).display : '';
                const customLocationDisplayInMapInitial = customLocationField ? window.getComputedStyle(customLocationField).display : '';
                const spilloverValueDisplayInMapInitial = spilloverValueField ? window.getComputedStyle(spilloverValueField).display : '';
                const spilloverUnitDisplayInMapInitial = spilloverUnitField ? window.getComputedStyle(spilloverUnitField).display : '';
                const areaSelectAllDisplayInMapValue = areaSelectAllButton ? window.getComputedStyle(areaSelectAllButton).display : '';
                const areaClearDisplayInMapValue = areaClearButton ? window.getComputedStyle(areaClearButton).display : '';
                if (areaMapOpen) areaMapOpen.click();
                const areaMap = areaDialog?.querySelector('[data-location-map-picker]') || null;
                const areaMapViewport = areaDialog?.querySelector('[data-location-map-viewport]') || null;
                const areaDistricts = Array.from(areaDialog?.querySelectorAll('[data-location-map-district]') || []);
                const areaZoomButtons = Array.from(areaDialog?.querySelectorAll('[data-location-map-zoom]') || []);
                const areaCloseButtons = Array.from(areaDialog?.querySelectorAll('[data-location-map-close]') || []);
                const dialogLockOpen = document.documentElement.dataset.pqxLocationMapOpen === 'true';
                const htmlOverflowOpen = document.documentElement.style.overflow || '';
                const bodyOverflowOpen = document.body.style.overflow || '';
                const bodyPositionOpen = document.body.style.position || '';
                const bodyTopOpen = document.body.style.top || '';
                const railStyle = rail ? window.getComputedStyle(rail) : null;
                const summaryStyle = areaSummary ? window.getComputedStyle(areaSummary) : null;
                const areaMapDisplayValue = areaMap ? window.getComputedStyle(areaMap).display : '';
                const resultRect = result ? result.getBoundingClientRect() : null;
                const thumbRect = thumb ? thumb.getBoundingClientRect() : null;
                const mapRect = areaMap ? areaMap.getBoundingClientRect() : null;
                const mapViewportRect = areaMapViewport ? areaMapViewport.getBoundingClientRect() : null;
                const firstDistrict = areaDistricts[0] || null;
                if (firstDistrict) firstDistrict.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                const selectedDistrict = document.querySelector('[data-location-map-district].is-selected');
                const selectedInput = document.querySelector('input[name="location_query"]:checked');
                const selectedFill = selectedDistrict ? window.getComputedStyle(selectedDistrict).fill : '';
                const customLocationDisplayInMapSelected = customLocationField ? window.getComputedStyle(customLocationField).display : '';
                const firstDistrictPath = firstDistrict ? String(firstDistrict.getAttribute('d') || '') : '';
                const mapLayer = areaDialog?.querySelector('[data-location-map-layer]') || null;
                const zoomToggle = areaDialog?.querySelector('[data-location-map-zoom="reset"]') || null;
                const initialTransform = String(mapLayer?.getAttribute('transform') || '');
                if (zoomToggle) zoomToggle.click();
                const zoomedTransform = String(mapLayer?.getAttribute('transform') || '');
                if (zoomToggle) zoomToggle.click();
                const parseScale = (transform) => {
                    const match = String(transform || '').match(/scale\\(([^)]+)\\)/i);
                    const value = match ? Number(match[1]) : 0;
                    return Number.isFinite(value) ? value : 0;
                };
                let areaPinchZoomChanged = false;
                if (areaMapViewport && mapLayer && typeof PointerEvent === 'function') {
                    const viewportRect = areaMapViewport.getBoundingClientRect();
                    const centerX = viewportRect.left + viewportRect.width / 2;
                    const centerY = viewportRect.top + viewportRect.height / 2;
                    const beforePinch = String(mapLayer.getAttribute('transform') || '');
                    const dispatchPinchPointer = (type, pointerId, clientX, clientY, isPrimary) => {
                        areaMapViewport.dispatchEvent(new PointerEvent(type, {
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
                    const afterPinch = String(mapLayer.getAttribute('transform') || '');
                    areaPinchZoomChanged = parseScale(afterPinch) > parseScale(beforePinch) + 0.08;
                }
                const dialogWasOpen = Boolean(areaDialog && areaDialog.open);
                const closeButton = areaDialog?.querySelector('[data-location-map-close]') || null;
                if (closeButton) closeButton.click();
                const dialogLockAfterClose = document.documentElement.dataset.pqxLocationMapOpen === 'true';
                const bodyOverflowAfterClose = document.body.style.overflow || '';
                const htmlOverflowAfterClose = document.documentElement.style.overflow || '';
                const bodyPositionAfterClose = document.body.style.position || '';
                const pageScrollAfterClose = Math.round(window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0);
                const spilloverValueDisplayAfterClose = spilloverValueField ? window.getComputedStyle(spilloverValueField).display : '';
                const spilloverUnitDisplayAfterClose = spilloverUnitField ? window.getComputedStyle(spilloverUnitField).display : '';
                if (areaListButton) areaListButton.click();
                const customLocationDisplayAfterList = customLocationField ? window.getComputedStyle(customLocationField).display : '';
                const areaSelectAllDisplayAfterListValue = areaSelectAllButton ? window.getComputedStyle(areaSelectAllButton).display : '';
                const areaClearDisplayAfterListValue = areaClearButton ? window.getComputedStyle(areaClearButton).display : '';
                const areaRowsAfterList = Array.from(locationField?.querySelectorAll('[data-pqx-check-grid="location_query"] .pqx-check') || []);
                const areaRects = areaRowsAfterList.map((node) => node.getBoundingClientRect());
                const areaStyle = areaRowsAfterList[0] ? window.getComputedStyle(areaRowsAfterList[0]) : null;
                const areaGridStyle = areaGrid ? window.getComputedStyle(areaGrid) : null;
                if (areaGrid) {
                    areaGrid.scrollTop = areaGrid.scrollHeight;
                }
                const lastAreaRect = areaRowsAfterList.length ? areaRowsAfterList[areaRowsAfterList.length - 1].getBoundingClientRect() : null;
                const scrolledGridRect = areaGrid ? areaGrid.getBoundingClientRect() : null;
                return {
                    bodyWidth: document.documentElement.scrollWidth,
                    viewportWidth: window.innerWidth,
                    topbarRight: topbarRect ? topbarRect.right : 0,
                    topnavScrollWidth: topnav ? topnav.scrollWidth : 0,
                    topnavClientWidth: topnav ? topnav.clientWidth : 0,
                    topnavFirstLeft: topnavFirstRect ? topnavFirstRect.left : 0,
                    topLaunchWidth: topLaunchRect ? topLaunchRect.width : 0,
                    topLaunchRight: topLaunchRect ? topLaunchRect.right : 0,
                    drawerScrollTopBeforeInteraction: drawerScrollTopInitial,
                    railOverflowX: railStyle ? railStyle.overflowX : '',
                    railDisplay: railStyle ? railStyle.display : '',
                    railGridColumns: railStyle ? railStyle.gridTemplateColumns : '',
                    railScrollWidth: rail ? rail.scrollWidth : 0,
                    railClientWidth: rail ? rail.clientWidth : 0,
                    railPosition: railStyle ? railStyle.position : '',
                    railLabelsFit: rail
                        ? Array.from(rail.querySelectorAll('[data-property-step-trigger]')).every((button) => {
                            const label = button.querySelector('strong');
                            if (!label) return false;
                            const buttonRect = button.getBoundingClientRect();
                            const labelRect = label.getBoundingClientRect();
                            return labelRect.left >= buttonRect.left - 1
                                && labelRect.right <= buttonRect.right + 1
                                && labelRect.top >= buttonRect.top - 1
                                && labelRect.bottom <= buttonRect.bottom + 1;
                        })
                        : false,
                    legacyDockVisible: Boolean(legacyDock && legacyDock.getBoundingClientRect().height > 0),
                    viewportHeight: window.innerHeight,
                    resultWidth: resultRect ? resultRect.width : 0,
                    thumbWidth: thumbRect ? thumbRect.width : 0,
                    areaModeInMap: areaModeInMapValue,
                    areaMapButtonText: areaMapButton ? areaMapButton.textContent || '' : '',
                    areaListButtonText: areaListButton ? areaListButton.textContent || '' : '',
                    areaMapButtonHeight: areaMapButtonRect ? areaMapButtonRect.height : 0,
                    areaListButtonHeight: areaListButtonRect ? areaListButtonRect.height : 0,
                    stepActionsHeight: stepActionsRect ? stepActionsRect.height : 0,
                    stepProgressWidth: stepProgressRect ? stepProgressRect.width : 0,
                    stepProgressHeight: stepProgressRect ? stepProgressRect.height : 0,
                    stepNextWidth: stepNextRect ? stepNextRect.width : 0,
                    stepSaveWidth: stepSaveRect ? stepSaveRect.width : 0,
                    stepSaveHeight: stepSaveRect ? stepSaveRect.height : 0,
                    stepActionsBottom: stepActionsRect ? stepActionsRect.bottom : 0,
                    stepStatusTop: stepStatusRect ? stepStatusRect.top : 0,
                    stepSaveTop: stepSaveRect ? stepSaveRect.top : 0,
                    firstFieldTopBeforeInteraction: firstFieldRect ? firstFieldRect.top : 0,
                    areaMapAvailable: areaMapAvailableValue,
                    areaGridDisplayInMap: areaGridDisplayInMapValue,
                    areaMapLaunchDisplay: areaMapLaunchDisplayValue,
                    customLocationDisplayInMapInitial,
                    spilloverValueDisplayInMapInitial,
                    spilloverUnitDisplayInMapInitial,
                    areaMapOpenText: areaMapOpen ? areaMapOpen.textContent || '' : '',
                    areaMapOpenHeight: areaMapOpenRect ? areaMapOpenRect.height : 0,
                    areaSelectAllDisplayInMap: areaSelectAllDisplayInMapValue,
                    areaClearDisplayInMap: areaClearDisplayInMapValue,
                    areaDialogOpen: dialogWasOpen,
                    areaCloseButtonCount: areaCloseButtons.length,
                    dialogLockOpen,
                    htmlOverflowOpen,
                    bodyOverflowOpen,
                    bodyPositionOpen,
                    bodyTopOpen,
                    dialogLockAfterClose,
                    bodyOverflowAfterClose,
                    htmlOverflowAfterClose,
                    bodyPositionAfterClose,
                    pageScrollBeforeMap,
                    pageScrollAfterClose,
                    areaRowCount: areaRowsAfterList.length,
                    areaRowMinHeight: areaRects.length ? Math.min(...areaRects.map((rect) => rect.height)) : 0,
                    areaRowsClearOfViewport: areaRects.filter((rect) => rect.top >= 0 && rect.bottom <= window.innerHeight - 4).length,
                    areaRowMaxRight: areaRects.length ? Math.max(...areaRects.map((rect) => rect.right)) : 0,
                    areaRowGridColumns: areaStyle ? areaStyle.gridTemplateColumns : '',
                    areaRowBorderRadius: areaStyle ? areaStyle.borderRadius : '',
                    areaModeAfterList: locationField ? String(locationField.getAttribute('data-location-mode') || '') : '',
                    areaGridDisplayAfterList: areaGridStyle ? areaGridStyle.display : '',
                    areaSelectAllDisplayAfterList: areaSelectAllDisplayAfterListValue,
                    areaClearDisplayAfterList: areaClearDisplayAfterListValue,
                    areaGridOverflowY: areaGridStyle ? areaGridStyle.overflowY : '',
                    areaGridColumns: areaGridStyle ? areaGridStyle.gridTemplateColumns : '',
                    areaGridColumnCount: areaGridStyle ? areaGridStyle.gridTemplateColumns.split(' ').filter(Boolean).length : 0,
                    areaSummaryDisplay: summaryStyle ? summaryStyle.display : '',
                    areaSummaryText: areaSummary ? areaSummary.textContent || '' : '',
                    areaMapDisplay: areaMapDisplayValue,
                    areaMapHeight: mapRect ? mapRect.height : 0,
                    areaMapRight: mapRect ? mapRect.right : 0,
                    areaMapViewportHeight: mapViewportRect ? mapViewportRect.height : 0,
                    areaDistrictCount: areaDistricts.length,
                    areaZoomButtonCount: areaZoomButtons.length,
                    areaButtonZoomChanged: Boolean(zoomToggle && mapLayer && zoomedTransform !== initialTransform && zoomedTransform.includes('scale(')),
                    areaPinchZoomChanged,
                    firstDistrictPathLooksReal: firstDistrictPath.startsWith('M') && firstDistrictPath.split('L').length >= 8 && !firstDistrictPath.includes(' Q '),
                    selectedDistrictFill: selectedFill,
                    customLocationDisplayInMapSelected,
                    selectedMapMatchesInput: Boolean(selectedDistrict && selectedInput && selectedDistrict.getAttribute('data-location-value') === selectedInput.value),
                    spilloverValueDisplayAfterClose,
                    spilloverUnitDisplayAfterClose,
                    customLocationDisplayAfterList,
                    areaGridScrolls: areaGrid ? areaGrid.scrollHeight > areaGrid.clientHeight + 2 : false,
                    areaGridBottomAfterScroll: scrolledGridRect ? scrolledGridRect.bottom : 0,
                    lastAreaBottomAfterScroll: lastAreaRect ? lastAreaRect.bottom : 0,
                    lastAreaTopAfterScroll: lastAreaRect ? lastAreaRect.top : 0,
                };
            }"""
        )
        assert mobile_metrics["bodyWidth"] <= mobile_metrics["viewportWidth"] + 1
        assert mobile_metrics["topbarRight"] <= mobile_metrics["viewportWidth"] + 1
        assert mobile_metrics["topnavScrollWidth"] >= mobile_metrics["topnavClientWidth"]
        assert mobile_metrics["topnavFirstLeft"] >= -1
        assert mobile_metrics["topLaunchWidth"] <= 1
        assert mobile_metrics["drawerScrollTopBeforeInteraction"] <= 2
        assert mobile_metrics["railDisplay"] == "grid"
        assert len(str(mobile_metrics["railGridColumns"]).split()) == 3
        assert mobile_metrics["railOverflowX"] == "visible"
        assert mobile_metrics["railScrollWidth"] <= mobile_metrics["railClientWidth"] + 1
        assert mobile_metrics["railPosition"] == "sticky"
        assert mobile_metrics["railLabelsFit"] is True
        assert mobile_metrics["legacyDockVisible"] is False
        assert mobile_metrics["resultWidth"] <= mobile_metrics["viewportWidth"] + 1
        if mobile_metrics["thumbWidth"]:
            assert 84 <= mobile_metrics["thumbWidth"] <= 96
        assert mobile_metrics["areaModeInMap"] == "map"
        assert mobile_metrics["areaMapButtonText"].strip() == "Map"
        assert mobile_metrics["areaListButtonText"].strip() == "List"
        assert mobile_metrics["areaMapButtonHeight"] >= 52
        assert mobile_metrics["areaListButtonHeight"] >= 52
        assert mobile_metrics["stepActionsHeight"] <= 112
        assert mobile_metrics["stepNextWidth"] >= 96
        assert mobile_metrics["stepProgressWidth"] <= 1
        assert mobile_metrics["stepProgressHeight"] <= 1
        assert mobile_metrics["stepSaveWidth"] <= 1
        assert mobile_metrics["stepSaveHeight"] <= 1
        assert mobile_metrics["firstFieldTopBeforeInteraction"] >= mobile_metrics["stepActionsBottom"] + 4
        assert mobile_metrics["stepStatusTop"] == 0 or mobile_metrics["stepSaveTop"] <= mobile_metrics["stepStatusTop"]
        assert mobile_metrics["areaMapAvailable"] == "true"
        assert mobile_metrics["areaGridDisplayInMap"] == "none"
        assert mobile_metrics["areaMapLaunchDisplay"] != "none"
        assert mobile_metrics["customLocationDisplayInMapInitial"] == "none"
        assert mobile_metrics["spilloverValueDisplayInMapInitial"] == "none"
        assert mobile_metrics["spilloverUnitDisplayInMapInitial"] == "none"
        assert "Open district map" in mobile_metrics["areaMapOpenText"]
        assert mobile_metrics["areaMapOpenHeight"] >= 60
        assert mobile_metrics["areaSelectAllDisplayInMap"] == "none"
        assert mobile_metrics["areaClearDisplayInMap"] == "none"
        assert mobile_metrics["areaDialogOpen"] is True
        assert mobile_metrics["areaCloseButtonCount"] >= 1
        assert mobile_metrics["dialogLockOpen"] is True
        assert mobile_metrics["htmlOverflowOpen"] == "hidden"
        assert mobile_metrics["bodyOverflowOpen"] == "hidden"
        assert mobile_metrics["bodyPositionOpen"] == "fixed"
        assert float(str(mobile_metrics["bodyTopOpen"]).replace("px", "") or 0) <= 0
        assert mobile_metrics["dialogLockAfterClose"] is False
        assert mobile_metrics["bodyOverflowAfterClose"] != "hidden"
        assert mobile_metrics["htmlOverflowAfterClose"] != "hidden"
        assert mobile_metrics["bodyPositionAfterClose"] != "fixed"
        assert abs(float(mobile_metrics["pageScrollAfterClose"]) - float(mobile_metrics["pageScrollBeforeMap"])) <= 2.0
        assert mobile_metrics["areaRowCount"] >= 6
        assert mobile_metrics["areaRowMinHeight"] >= 48
        assert mobile_metrics["areaRowsClearOfViewport"] >= 2
        assert mobile_metrics["areaRowMaxRight"] <= mobile_metrics["viewportWidth"] + 1
        assert mobile_metrics["areaModeAfterList"] == "list"
        assert mobile_metrics["areaGridDisplayAfterList"] != "none"
        assert mobile_metrics["areaSelectAllDisplayAfterList"] != "none"
        assert mobile_metrics["areaClearDisplayAfterList"] != "none"
        assert mobile_metrics["areaGridColumnCount"] == 1
        assert "34px" in mobile_metrics["areaRowGridColumns"]
        assert mobile_metrics["areaRowBorderRadius"] != "0px"
        assert mobile_metrics["areaSummaryDisplay"] != "none"
        assert "district" in mobile_metrics["areaSummaryText"].lower()
        assert mobile_metrics["areaMapDisplay"] == "grid"
        assert mobile_metrics["areaMapHeight"] >= 340
        assert mobile_metrics["areaMapRight"] <= mobile_metrics["viewportWidth"] + 1
        assert mobile_metrics["areaMapViewportHeight"] >= 260
        assert mobile_metrics["areaDistrictCount"] >= 6
        assert mobile_metrics["areaZoomButtonCount"] == 3
        assert mobile_metrics["areaButtonZoomChanged"] is True
        assert mobile_metrics["areaPinchZoomChanged"] is True
        assert mobile_metrics["firstDistrictPathLooksReal"] is True
        assert mobile_metrics["customLocationDisplayInMapSelected"] == "none"
        assert mobile_metrics["selectedMapMatchesInput"] is True
        assert "209" in mobile_metrics["selectedDistrictFill"] or "rgb" in mobile_metrics["selectedDistrictFill"]
        assert mobile_metrics["spilloverValueDisplayAfterClose"] != "none"
        assert mobile_metrics["spilloverUnitDisplayAfterClose"] != "none"
        assert mobile_metrics["customLocationDisplayAfterList"] != "none"
        assert mobile_metrics["areaGridOverflowY"] in {"auto", "scroll"}
        assert mobile_metrics["areaGridBottomAfterScroll"] <= mobile_metrics["viewportHeight"] + 1
        assert mobile_metrics["lastAreaBottomAfterScroll"] <= mobile_metrics["viewportHeight"] + 1
        assert mobile_metrics["lastAreaTopAfterScroll"] >= 0
        _assert_no_horizontal_overflow(mobile_page)
    finally:
        desktop.close()
        mobile.close()


def test_propertyquarry_desktop_district_map_click_selects_shape_and_zoom_toggles(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=860)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok
        page.select_option('select[name="country_code"]', "AT")
        page.select_option('select[name="region_code"]', "vienna")
        page.wait_for_function(
            """() => document.querySelector('[data-property-field-name="location_query"]')?.dataset.locationMapAvailable === 'true'"""
        )
        page.locator('[data-location-mode-button="map"]').click()
        page.locator("[data-location-map-open]").click()
        district = page.locator("[data-location-map-district]").first
        district.wait_for(state="visible")
        value = str(district.get_attribute("data-location-value") or "")
        assert value
        before_checked = page.evaluate(
            """(value) => {
              const input = Array.from(document.querySelectorAll('input[name="location_query"]'))
                .find((node) => String(node.value || '').trim() === String(value || '').trim());
              return Boolean(input && input.checked);
            }""",
            value,
        )
        box = district.bounding_box()
        assert box is not None
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_function(
            """([value, beforeChecked]) => {
              const input = Array.from(document.querySelectorAll('input[name="location_query"]'))
                .find((node) => String(node.value || '').trim() === String(value || '').trim());
              return Boolean(input) && input.checked !== beforeChecked;
            }""",
            arg=[value, before_checked],
        )
        assert district.evaluate("(node) => node.classList.contains('is-selected')") is (not before_checked)

        layer = page.locator("[data-location-map-layer]")
        initial_transform = str(layer.get_attribute("transform") or "")
        zoom_toggle = page.locator('[data-location-map-zoom="reset"]')
        zoom_toggle.click()
        page.wait_for_function(
            """(initialTransform) => {
              const layer = document.querySelector('[data-location-map-layer]');
              return layer && String(layer.getAttribute('transform') || '') !== initialTransform;
            }""",
            arg=initial_transform,
        )
        zoomed_transform = str(layer.get_attribute("transform") or "")
        assert "scale(" in zoomed_transform and zoomed_transform != initial_transform
        expect(zoom_toggle).to_have_text(re.compile("fit", re.I))
        zoom_toggle.click()
        expect(zoom_toggle).to_have_text(re.compile("zoom", re.I))
        assert "scale(1)" in str(layer.get_attribute("transform") or "")
    finally:
        context.close()


def test_propertyquarry_search_desktop_wheel_scroll_recovers_from_bottom(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=620)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok
        drawer = page.locator("[data-workbench-brief-drawer]")
        drawer.wait_for(state="visible")
        box = drawer.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        max_scroll = float(drawer.evaluate("(node) => Math.max(0, node.scrollHeight - node.clientHeight)"))
        assert max_scroll <= 2.0
        page.evaluate("() => window.scrollTo(0, 0)")

        for _ in range(8):
            page.mouse.wheel(0, 720)
            page.wait_for_timeout(25)
        bottom_scroll = float(page.evaluate("() => window.scrollY"))
        assert bottom_scroll >= 24.0

        for _ in range(8):
            page.mouse.wheel(0, -720)
            page.wait_for_timeout(25)
        recovered_scroll = float(page.evaluate("() => window.scrollY"))
        assert recovered_scroll <= max(8.0, bottom_scroll * 0.35)
    finally:
        context.close()


def test_propertyquarry_search_launch_strips_cross_country_provider_selection(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    observed: dict[str, object] = {}
    try:
        def _capture_preferences(route):
            observed["preferences"] = route.request.post_data_json
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "saved"}),
            )

        def _capture_run(route):
            observed["run"] = route.request.post_data_json
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"run_id": "run-cross-country-provider"}),
            )

        page.route("**/v1/onboarding/property-search/preferences", _capture_preferences)
        page.route("**/app/api/property/search-runs**", _capture_run)
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok
        page.select_option('select[name="country_code"]', "AT")
        page.evaluate(
            """
            () => {
              const grid = document.querySelector('[data-pqx-check-grid="selected_platforms"]');
              const stale = document.createElement('input');
              stale.type = 'checkbox';
              stale.name = 'selected_platforms';
              stale.value = 'otodom';
              stale.checked = true;
              stale.setAttribute('data-country-code', 'PL');
              grid?.appendChild(stale);
            }
            """
        )
        page.locator("[data-property-start-top]").click()
        expect(page).to_have_url(re.compile("run-cross-country-provider"), timeout=10000)

        preferences = observed.get("preferences")
        run = observed.get("run")
        assert isinstance(preferences, dict)
        assert isinstance(run, dict)
        assert preferences["country_code"] == "AT"
        assert "otodom" not in preferences.get("selected_platforms", [])
        assert "otodom" not in run.get("selected_platforms", [])
        assert "otodom" not in run.get("property_preferences", {}).get("selected_platforms", [])
    finally:
        context.close()


def test_propertyquarry_search_launch_preserves_checked_provider_set(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    observed: dict[str, object] = {}
    try:
        def _capture_preferences(route):
            observed["preferences"] = route.request.post_data_json
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "saved"}),
            )

        def _capture_run(route):
            observed["run"] = route.request.post_data_json
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"run_id": "run-provider-preserve"}),
            )

        page.route("**/v1/onboarding/property-search/preferences", _capture_preferences)
        page.route("**/app/api/property/search-runs**", _capture_run)
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok

        checked_before_launch = page.evaluate(
            """
            () => [...document.querySelectorAll('input[name="selected_platforms"]:checked')]
              .map((node) => String(node.value || '').trim())
              .filter(Boolean)
            """
        )
        eligible_checked_before_launch = _settled_checked_provider_values(page)
        plan_cap = page.evaluate(
            """
            () => {
              const metaNode = document.querySelector('[data-property-workspace-meta]');
              const meta = metaNode ? JSON.parse(metaNode.getAttribute('data-property-workspace-meta') || '{}') : {};
              return Number(meta?.commercial?.max_platforms ?? 0) || 0;
            }
            """
        )
        assert isinstance(checked_before_launch, list)
        assert isinstance(eligible_checked_before_launch, list)
        assert len(checked_before_launch) > 1
        assert len(eligible_checked_before_launch) > 1
        assert isinstance(plan_cap, (int, float))

        page.locator("[data-property-start-top]").click()
        expect(page).to_have_url(re.compile("run-provider-preserve"), timeout=10000)

        preferences = observed.get("preferences")
        run = observed.get("run")
        assert isinstance(preferences, dict)
        assert isinstance(run, dict)
        effective_expected = eligible_checked_before_launch[: int(plan_cap)] if int(plan_cap) > 0 else eligible_checked_before_launch
        assert preferences.get("selected_platforms") == effective_expected
        assert run.get("selected_platforms") == effective_expected
        assert run.get("property_preferences", {}).get("selected_platforms") == effective_expected
    finally:
        context.close()


def test_propertyquarry_search_launch_reuses_idempotency_key_after_lost_response(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    observed_keys: list[str] = []
    try:
        def _save_preferences(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "saved"}),
            )

        def _start_run(route):
            observed_keys.append(str(route.request.headers.get("idempotency-key") or ""))
            if len(observed_keys) == 1:
                route.abort("connectionreset")
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"run_id": "run-idempotent-retry"}),
            )

        page.route("**/v1/onboarding/property-search/preferences", _save_preferences)
        page.route("**/app/api/property/search-runs**", _start_run)
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok

        launch = page.locator("[data-property-start-top]")
        launch.click()
        expect(page.locator("[data-property-inline-error]").first).to_contain_text(
            "connection dropped",
            timeout=10000,
        )
        expect(launch).to_be_enabled()
        launch.click()
        expect(page).to_have_url(re.compile("run-idempotent-retry"), timeout=10000)

        assert len(observed_keys) == 2
        assert observed_keys[0]
        assert observed_keys[0] == observed_keys[1]
        assert len(observed_keys[0]) <= 240
    finally:
        context.close()


def test_propertyquarry_search_launch_turns_quota_rejection_into_retryable_calm_state(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    observed_keys: list[str] = []
    try:
        page.route(
            "**/v1/onboarding/property-search/preferences",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "saved"}),
            ),
        )

        def _start_run(route):
            observed_keys.append(str(route.request.headers.get("idempotency-key") or ""))
            if len(observed_keys) == 1:
                route.fulfill(
                    status=429,
                    content_type="application/json",
                    headers={"retry-after": "1"},
                    body=json.dumps(
                        {
                            "error": {
                                "code": "ingress_cost_quota_exceeded",
                                "message": "account request cost quota exceeded",
                                "details": {"retry_after_seconds": 1},
                                "correlation_id": "quota-browser-receipt",
                            }
                        }
                    ),
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"run_id": "run-quota-retry"}),
            )

        page.route("**/app/api/property/search-runs**", _start_run)
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok

        page.locator("[data-property-start-top]").click()
        marker = page.locator('[data-pq-failure-state="quota_blocked"]:visible').first
        expect(marker).to_contain_text("temporary request limit", timeout=10000)
        expect(marker).to_contain_text("saved search is unchanged")
        retry = marker.locator("[data-pq-next-action]")
        expect(retry).to_be_enabled()
        retry.click()
        expect(page).to_have_url(re.compile("run-quota-retry"), timeout=10000)

        assert len(observed_keys) == 2
        assert observed_keys[0]
        assert observed_keys[0] == observed_keys[1]
    finally:
        context.close()


@pytest.mark.parametrize(
    ("mobile", "locale"),
    [
        (False, "de-AT"),
        (True, "de-AT"),
        (True, "es-CR"),
    ],
)
def test_propertyquarry_search_launch_resumes_active_run_in_localized_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    mobile: bool,
    locale: str,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(
        browser,
        mobile=mobile,
        width=390 if mobile else 1440,
        height=844 if mobile else 900,
        locale=locale,
    )
    page: Page = context.new_page()
    launch_requests: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        page.route(
            "**/v1/onboarding/property-search/preferences",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "saved"}),
            ),
        )

        def _resume_active_run(route) -> None:
            launch_requests.append(route.request.method)
            route.fulfill(
                status=200,
                content_type="application/json",
                headers={
                    "cache-control": "no-store",
                    "x-property-search-resumed": "true",
                },
                body=json.dumps(
                    {
                        "generated_at": "2026-07-29T08:00:00+00:00",
                        "run_id": "run-active-resume",
                        "status": "in_progress",
                        "status_url": "/app/api/property/search-runs/run-active-resume",
                        "resumed": True,
                        "resume_url": "/app/properties?run_id=run-active-resume",
                    }
                ),
            )

        page.route("**/app/api/property/search-runs**", _resume_active_run)
        page.route(
            "**/app/properties?run_id=run-active-resume",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="<html><body><main>Active search resumed</main></body></html>",
            ),
        )
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok
        assert page_errors == []
        page.wait_for_function(
            """() => document.querySelector(
              '[data-property-decision-workbench]'
            )?.dataset.pqWorkbenchController === 'loaded'"""
        )
        expected_provider_label = "Portale" if locale == "de-AT" else "Portales"
        expect(
            page.locator('[data-property-step-trigger="providers"] strong')
        ).to_have_text(expected_provider_label)

        if mobile:
            mobile_layout = page.evaluate(
                """() => {
                  const rail = document.querySelector('[data-property-mobile-step-rail]');
                  const next = document.querySelector('[data-property-step-next]:not([hidden])');
                  const actions = next?.closest('.pqx-step-head-actions');
                  const railStyle = rail ? getComputedStyle(rail) : null;
                  const nextRect = next?.getBoundingClientRect() || null;
                  const actionsRect = actions?.getBoundingClientRect() || null;
                  const labelsFit = rail
                    ? [...rail.querySelectorAll('[data-property-step-trigger]')].every((button) => {
                        const label = button.querySelector('strong');
                        if (!label) return false;
                        const buttonRect = button.getBoundingClientRect();
                        const labelRect = label.getBoundingClientRect();
                        return labelRect.left >= buttonRect.left - 1
                          && labelRect.right <= buttonRect.right + 1
                          && labelRect.top >= buttonRect.top - 1
                          && labelRect.bottom <= buttonRect.bottom + 1;
                      })
                    : false;
                  return {
                    railDisplay: railStyle?.display || '',
                    railColumns: railStyle?.gridTemplateColumns || '',
                    railScrollWidth: rail?.scrollWidth || 0,
                    railClientWidth: rail?.clientWidth || 0,
                    labelsFit,
                    nextWidth: nextRect?.width || 0,
                    actionsWidth: actionsRect?.width || 0,
                  };
                }"""
            )
            assert mobile_layout["railDisplay"] == "grid"
            assert len(str(mobile_layout["railColumns"]).split()) == 3
            assert mobile_layout["railScrollWidth"] <= mobile_layout["railClientWidth"] + 1
            assert mobile_layout["labelsFit"] is True
            assert mobile_layout["nextWidth"] >= mobile_layout["actionsWidth"] - 2
            page.locator('[data-property-step-trigger="providers"]').click()
            page.wait_for_function(
                """() => document.querySelector(
                  '[data-console-form-variant="property_search"]'
                )?.dataset.propertyActiveStep === 'providers'"""
            )
            launch = page.locator("[data-property-step-next]:visible")
            expect(launch).to_have_text("Suche" if locale == "de-AT" else "Buscar")
        else:
            launch = page.locator("[data-property-start-top]")
            expect(launch).to_have_attribute("aria-label", "Suche starten")
        expect(launch).to_be_visible()
        launch_box = launch.evaluate(
            """(button) => ({
              clientWidth: button.clientWidth,
              scrollWidth: button.scrollWidth,
              clientHeight: button.clientHeight,
              scrollHeight: button.scrollHeight,
            })"""
        )
        assert launch_box["scrollWidth"] <= launch_box["clientWidth"] + 1
        assert launch_box["scrollHeight"] <= launch_box["clientHeight"] + 1

        page.evaluate(
            """() => {
              sessionStorage.removeItem('pq-launch-copy-receipt');
              const record = () => {
                const rows = Array.from(document.querySelectorAll(
                  '[data-property-top-launch-status], [data-property-launch-status], [data-property-inline-status]'
                ))
                  .map((node) => String(node.textContent || '').trim())
                  .filter(Boolean);
                if (rows.length) {
                  const existing = JSON.parse(sessionStorage.getItem('pq-launch-copy-receipt') || '[]');
                  sessionStorage.setItem(
                    'pq-launch-copy-receipt',
                    JSON.stringify([...existing, ...rows])
                  );
                }
              };
              new MutationObserver(record).observe(document.body, {
                subtree: true,
                childList: true,
                characterData: true,
              });
            }"""
        )
        launch.click()
        expect(page).to_have_url(re.compile(r"/app/properties\?run_id=run-active-resume"), timeout=10000)
        expect(page.get_by_text("Active search resumed")).to_be_visible()
        launch_copy_receipt = page.evaluate(
            "() => JSON.parse(sessionStorage.getItem('pq-launch-copy-receipt') || '[]')"
        )
        expected_opening_copy = (
            "Laufende Suche wird geöffnet…"
            if locale == "de-AT"
            else "Abriendo la búsqueda activa…"
        )
        assert expected_opening_copy in launch_copy_receipt

        assert launch_requests == ["POST"]
        assert console_errors == []
        assert page_errors == []
    finally:
        context.close()


def test_propertyquarry_search_launch_continues_when_preference_save_times_out(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900, locale="de-AT")
    page: Page = context.new_page()
    preference_requests = 0
    launch_requests: list[dict[str, object]] = []
    try:
        def _stall_preferences(_route) -> None:
            nonlocal preference_requests
            preference_requests += 1
            # Leave the request pending so the browser exercises the real
            # client timeout instead of a synthetic HTTP failure.

        def _resume_active_run(route) -> None:
            launch_requests.append(route.request.post_data_json or {})
            route.fulfill(
                status=200,
                content_type="application/json",
                headers={"x-property-search-resumed": "true"},
                body=json.dumps(
                    {
                        "run_id": "run-save-timeout-resume",
                        "status": "in_progress",
                        "status_url": "/app/api/property/search-runs/run-save-timeout-resume",
                        "resumed": True,
                    }
                ),
            )

        page.route("**/v1/onboarding/property-search/preferences", _stall_preferences)
        page.route("**/app/api/property/search-runs**", _resume_active_run)
        page.route(
            "**/app/properties?run_id=run-save-timeout-resume",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="<html><body><main>Active search resumed after save timeout</main></body></html>",
            ),
        )
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok
        page.wait_for_function(
            """() => document.querySelector(
              '[data-property-decision-workbench]'
            )?.dataset.pqWorkbenchController === 'loaded'"""
        )

        launch = page.locator("[data-property-start-top]")
        expect(launch).to_be_enabled()
        launch.click()
        expect(page).to_have_url(
            re.compile(r"/app/properties\?run_id=run-save-timeout-resume"),
            timeout=25_000,
        )
        expect(page.get_by_text("Active search resumed after save timeout")).to_be_visible()

        assert preference_requests == 1
        assert len(launch_requests) == 1
        assert launch_requests[0].get("property_preferences")
        assert launch_requests[0].get("selected_platforms")
    finally:
        context.close()


def test_propertyquarry_app_search_launch_uses_any_property_type_when_all_boxes_are_clear(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    observed: dict[str, object] = {}
    try:
        def _capture_preferences(route):
            observed["preferences"] = route.request.post_data_json
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "saved"}),
            )

        def _capture_run(route):
            observed["run"] = route.request.post_data_json
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"run_id": "run-any-property-type"}),
            )

        page.route("**/v1/onboarding/property-search/preferences", _capture_preferences)
        page.route("**/app/api/property/search-runs**", _capture_run)
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok
        page.eval_on_selector_all(
            'input[name="property_type"]:checked',
            """(nodes) => {
              nodes.forEach((node) => node.click());
              return nodes.length;
            }""",
        )
        expect(page.locator('input[name="property_type"]:checked')).to_have_count(0)

        page.locator("[data-property-start-top]").click()
        expect(page).to_have_url(re.compile("run-any-property-type"), timeout=10000)

        preferences = observed.get("preferences")
        run = observed.get("run")
        assert isinstance(preferences, dict)
        assert isinstance(run, dict)
        assert preferences["property_type"] == ["any"]
        assert run.get("property_preferences", {}).get("property_type") == ["any"]
    finally:
        context.close()


def test_propertyquarry_country_switch_replaces_out_of_market_provider_selection(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    client = propertyquarry_browser_server["client"]
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "DE",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "Berlin",
            "selected_platforms": ["immoscout_de", "immowelt"],
        },
    )
    assert stored.status_code == 200, stored.text

    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok
        checked_before = page.locator('input[name="selected_platforms"]:checked')
        expect(checked_before).to_have_count(2)
        checked_before_values = page.eval_on_selector_all(
            'input[name="selected_platforms"]:checked',
            "(nodes) => nodes.map((node) => ({ value: node.value, country: node.getAttribute('data-country-code') || '' }))",
        )
        assert {row["value"] for row in checked_before_values} == {"immoscout_de", "immowelt"}

        page.select_option('select[name="country_code"]', "AT")
        page.wait_for_function(
            """
            () => {
              const checked = Array.from(document.querySelectorAll('input[name="selected_platforms"]:checked'));
              if (!checked.length) return false;
              const values = checked.map((node) => String(node.value || '').trim());
              return values.includes('willhaben') && !values.includes('immoscout_de') && !values.includes('immowelt');
            }
            """
        )

        checked_after = page.locator('input[name="selected_platforms"]:checked')
        expect(checked_after).to_have_count(3)
        checked_values = page.eval_on_selector_all(
            'input[name="selected_platforms"]:checked',
            "(nodes) => nodes.map((node) => ({ value: node.value, country: node.getAttribute('data-country-code') || '' }))",
        )
        assert "willhaben" in {row["value"] for row in checked_values}
        assert "immoscout_de" not in {row["value"] for row in checked_values}
        assert "immowelt" not in {row["value"] for row in checked_values}
        assert {str(row["country"] or "").upper() for row in checked_values} == {"AT"}
    finally:
        context.close()


def test_propertyquarry_automation_page_uses_compact_card_cockpit(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    client = propertyquarry_browser_server["client"]
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "1020 Vienna",
            "selected_platforms": ["willhaben", "derstandard_at"],
            "active_search_agent_id": "watch-1020",
            "search_agents": [
                {
                    "agent_id": "watch-1020",
                    "name": "Leopoldstadt rent watch",
                    "enabled": True,
                    "country_code": "AT",
                    "region_code": "vienna",
                    "location_query": "1020 Vienna",
                    "listing_mode": "rent",
                    "property_type": "apartment",
                    "duration_days": 90,
                    "notification_limit": 3,
                    "notification_period": "day",
                    "last_run_at": "2026-06-18T08:00:00+02:00",
                    "next_run_at": "2026-06-19T08:00:00+02:00",
                    "sent_in_current_window": 1,
                    "preferences_json": {
                        "country_code": "AT",
                        "region_code": "vienna",
                        "location_query": "1020 Vienna",
                        "listing_mode": "rent",
                        "property_type": "apartment",
                        "selected_platforms": ["willhaben", "derstandard_at"],
                    },
                },
                {
                    "agent_id": "watch-1130",
                    "name": "Hietzing buy watch",
                    "enabled": False,
                    "country_code": "AT",
                    "region_code": "vienna",
                    "location_query": "1130 Vienna",
                    "listing_mode": "buy",
                    "property_type": "apartment",
                    "duration_days": 180,
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
        },
    )
    assert stored.status_code == 200, stored.text

    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, width=1440, height=900)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/agents", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.locator("[data-property-search-agent-grid]")).to_be_visible()
        screenshot_path = tmp_path / "automation-premium-cockpit.png"
        page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled", caret="hide")
        assert screenshot_path.exists() and screenshot_path.stat().st_size > 20_000

        layout = page.evaluate(
            """
            () => {
              const cards = [...document.querySelectorAll('.pqx-automation-card')];
              const board = document.querySelector('[data-property-search-agent-management]');
              const grid = document.querySelector('[data-property-search-agent-grid]');
              const cardBoxes = cards.map((card) => card.getBoundingClientRect());
              return {
                cardCount: cards.length,
                thumbnailCount: document.querySelectorAll('.pqx-automation-thumbnail').length,
                overlayThumbCount: document.querySelectorAll('.pqx-automation-thumbnail[data-scope-overlay="true"]').length,
                osmThumbCount: document.querySelectorAll('.pqx-automation-thumbnail[data-scope-preview-kind="osm_district_overlay"]').length,
                previewUrlCount: [...document.querySelectorAll('.pqx-automation-thumbnail img')]
                  .filter((img) => {
                    const src = String(img.getAttribute('data-map-preview-src') || img.getAttribute('src') || '');
                    return src.startsWith('/app/api/property/map-previews/') && src.endsWith('.png');
                  }).length,
                refreshedPreviewCount: [...document.querySelectorAll('.pqx-automation-thumbnail img')]
                  .filter((img) => String(img.getAttribute('src') || '').startsWith('blob:') && img.getAttribute('data-map-preview-ready') === 'true').length,
                nonOverlayPreviewKinds: [...document.querySelectorAll('.pqx-automation-thumbnail')]
                  .map((thumb) => String(thumb.getAttribute('data-scope-preview-kind') || ''))
                  .filter((kind) => kind !== 'osm_district_overlay'),
                deleteCount: document.querySelectorAll('.pqx-automation-delete[data-search-agent-action="delete"]').length,
                formCount: document.querySelectorAll('form.pqx-form').length,
                tableCount: document.querySelectorAll('.pqx-automation-table').length,
                horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
                boardBottom: board?.getBoundingClientRect().bottom || 0,
                gridBottom: grid?.getBoundingClientRect().bottom || 0,
                viewportHeight: window.innerHeight,
                maxCardHeight: Math.max(...cardBoxes.map((box) => box.height)),
                maxCardRight: Math.max(...cardBoxes.map((box) => box.right)),
              };
            }
            """
        )
        assert layout["cardCount"] == 2
        assert layout["thumbnailCount"] == 2
        assert layout["overlayThumbCount"] == 2
        assert layout["osmThumbCount"] == 2
        assert layout["previewUrlCount"] == 2
        assert 0 <= layout["refreshedPreviewCount"] <= 2
        assert layout["nonOverlayPreviewKinds"] == []
        assert layout["deleteCount"] == 2
        assert layout["formCount"] == 0
        assert layout["tableCount"] == 0
        assert layout["horizontalOverflow"] is False
        assert layout["gridBottom"] <= layout["viewportHeight"]
        assert layout["boardBottom"] <= layout["viewportHeight"]
        assert layout["maxCardHeight"] <= 210
        assert layout["maxCardRight"] <= 1440

        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator(".pqx-automation-thumbnail").first.click()
        assert "/app/search" in page.url
        assert "load_agent=watch-1020" in page.url
    finally:
        context.close()


def test_propertyquarry_secondary_surfaces_have_phone_specific_layout(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    client = propertyquarry_browser_server["client"]
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "1020 Vienna",
            "selected_platforms": ["willhaben", "derstandard_at"],
            "search_agents": [
                {
                    "agent_id": "watch-1020-mobile",
                    "name": "Leopoldstadt mobile watch",
                    "enabled": True,
                    "country_code": "AT",
                    "region_code": "vienna",
                    "location_query": "1020 Vienna",
                    "listing_mode": "rent",
                    "property_type": "apartment",
                    "duration_days": 90,
                    "notification_limit": 3,
                    "notification_period": "day",
                    "preferences_json": {
                        "country_code": "AT",
                        "region_code": "vienna",
                        "location_query": "1020 Vienna",
                        "listing_mode": "rent",
                        "property_type": "apartment",
                        "selected_platforms": ["willhaben", "derstandard_at"],
                    },
                },
            ],
        },
    )
    assert stored.status_code == 200, stored.text

    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    routes = [
        ("/app/shortlist?run_id=run-42&full=1", "Shortlist", "propertyquarry-shortlist-mobile.png"),
        ("/app/agents", "Saved searches", "propertyquarry-agents-mobile.png"),
        ("/app/alerts", "Alerts", "propertyquarry-alerts-mobile.png"),
        ("/app/account", "Account", "propertyquarry-account-mobile.png"),
        ("/app/billing", "Billing", "propertyquarry-billing-mobile.png"),
        ("/app/settings/google", "Google", "propertyquarry-google-settings-mobile.png"),
        ("/app/settings/access", "Access", "propertyquarry-access-settings-mobile.png"),
        ("/app/settings/usage", "Usage", "propertyquarry-usage-settings-mobile.png"),
        ("/app/settings/support", "Support", "propertyquarry-support-settings-mobile.png"),
        ("/app/settings/trust", "Trust", "propertyquarry-trust-settings-mobile.png"),
        ("/app/settings/invitations", "Invitations", "propertyquarry-invitations-settings-mobile.png"),
    ]
    try:
        page = context.new_page()
        for route, mobile_mode_name, screenshot_name in routes:
            response = page.goto(f"{base_url}{route}", wait_until="networkidle")
            assert response is not None
            _assert_no_horizontal_overflow(page)
            if route == "/app/billing":
                assert response.ok
                _assert_propertyquarry_billing_redirect_lands_on_account(page)
                expect(page.locator("[data-property-mobile-dock]")).to_have_count(0)
                expect(page.locator('nav[aria-label="PropertyQuarry sections"]')).to_have_count(0)
                expect(page.locator("[data-pqx-launch-top]")).to_have_count(0)
                screenshot = tmp_path / screenshot_name
                page.screenshot(path=str(screenshot), full_page=True, animations="disabled", caret="hide")
                assert screenshot.exists() and screenshot.stat().st_size > 14_000
                continue
            assert response.ok
            _assert_property_shell_visual_gates(page, max_appbar_height=130)

            expect(page.locator("[data-property-mobile-dock]")).to_have_count(0)
            expect(page.locator('nav[aria-label="PropertyQuarry sections"]').first).to_be_visible()
            if route in {"/app/agents", "/app/alerts", "/app/account"} or route.startswith("/app/shortlist"):
                expect(page.locator(".pqx-topbar")).to_be_visible()
            if page.get_by_role("button", name=mobile_mode_name).count():
                expect(page.get_by_role("button", name=mobile_mode_name)).to_be_visible()
            elif route == "/app/settings/trust":
                expect(page.get_by_role("heading", name=re.compile(r"Review the latest search", re.I))).to_be_visible()
            else:
                expect(page.locator("body", has_text=mobile_mode_name)).to_be_visible()
            _assert_mobile_topnav_tap_targets(page)
            expect(page.locator("[data-pqx-launch-top]")).to_have_count(0)
            density = page.evaluate(
                """() => {
                    const visibleCards = Array.from(document.querySelectorAll('.pqx-card, .pqx-panel, .pqx-result, .pqx-account-action-card, .pqx-billing-card, .pqx-billing-summary-card, .pqx-automation-card'))
                        .filter((node) => {
                            const rect = node.getBoundingClientRect();
                            const style = window.getComputedStyle(node);
                            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                        });
                    const heavyShadows = visibleCards.filter((node) => window.getComputedStyle(node).boxShadow !== 'none');
                    const appbar = document.querySelector('.pqx-topbar');
                    const brand = document.querySelector('.pqx-brand');
                    const themeToggle = document.querySelector('[data-pqx-theme-toggle]');
                    const accountSummary = document.querySelector('.pqx-account-menu summary');
                    const logoutButton = document.querySelector('[data-account-page-sign-out] button, .pqx-account-menu button[type="submit"]');
                    return {
                        cardCount: visibleCards.length,
                        heavyShadowCount: heavyShadows.length,
                        appbarHeight: appbar ? Math.round(appbar.getBoundingClientRect().height) : 0,
                        brandVisible: brand ? window.getComputedStyle(brand).display !== 'none' && brand.getBoundingClientRect().width > 0 : false,
                        themeVisible: themeToggle ? window.getComputedStyle(themeToggle).display !== 'none' && themeToggle.getBoundingClientRect().width > 0 : false,
                        accountSummaryVisible: accountSummary ? accountSummary.getBoundingClientRect().width > 0 : false,
                        logoutVisible: logoutButton ? logoutButton.getBoundingClientRect().width > 0 : false,
                    };
                }"""
            )
            assert density["cardCount"] <= 18
            assert density["heavyShadowCount"] <= 2
            assert density["appbarHeight"] <= 112
            assert density["brandVisible"] is False
            assert density["themeVisible"] is False
            if route in {"/app/agents", "/app/alerts", "/app/account"}:
                assert density["appbarHeight"] <= 60
            elif route.startswith("/app/settings/") or route == "/app/properties/packets":
                assert density["appbarHeight"] <= 56
            elif route.startswith("/app/shortlist"):
                assert density["appbarHeight"] <= 58
            if route in {"/app/agents", "/app/alerts"} or route.startswith("/app/shortlist"):
                assert density["accountSummaryVisible"] is True

            if route.startswith("/app/shortlist"):
                expect(page.locator("body", has_text=re.compile(r"Shortlist|No shortlist yet|Ranked homes", re.I))).to_be_visible()
                expect(page.locator("[data-property-mobile-dock]")).to_have_count(0)
                expect(page.locator("body", has_text=re.compile(r"Search|Research|Properties", re.I))).to_be_visible()
            elif route == "/app/agents":
                expect(page.locator("[data-property-search-agent-grid]")).to_be_visible()
                expect(page.locator(".pqx-automation-thumbnail").first).to_be_visible()
            elif route == "/app/alerts":
                expect(page.locator("body", has_text="Alerts")).to_be_visible()
                expect(page.locator("body", has_text="Notifications")).to_be_visible()
                alerts_mobile_metrics = page.evaluate(
                    """() => {
                        const rows = Array.from(document.querySelectorAll('.pqx-shell[data-pqx-surface="alerts"] .pqx-pref-row'));
                        const rowRects = rows.map((row) => row.getBoundingClientRect());
                        return {
                            viewportWidth: window.innerWidth,
                            rowCount: rows.length,
                            minRowHeight: Math.min(...rowRects.map((rect) => rect.height)),
                            maxRowRight: Math.max(0, ...rowRects.map((rect) => rect.right)),
                            multiColumnRows: rows.filter((row) => window.getComputedStyle(row).gridTemplateColumns.split(' ').filter(Boolean).length > 1).length,
                            actionColumns: Array.from(document.querySelectorAll('.pqx-shell[data-pqx-surface="alerts"] .pqx-pref-row .pqx-actions'))
                                .map((node) => window.getComputedStyle(node).gridTemplateColumns.split(' ').filter(Boolean).length),
                        };
                    }"""
                )
                assert alerts_mobile_metrics["rowCount"] >= 2
                assert alerts_mobile_metrics["minRowHeight"] >= 58
                assert alerts_mobile_metrics["multiColumnRows"] == 0
                assert alerts_mobile_metrics["maxRowRight"] <= alerts_mobile_metrics["viewportWidth"] + 1
                assert all(columns <= 1 for columns in alerts_mobile_metrics["actionColumns"])
            elif route == "/app/account":
                expect(page.locator("body", has_text="Notifications")).to_be_visible()
                expect(page.get_by_role("link", name="Billing")).to_be_visible()
                expect(page.locator("body", has_text="Access links")).to_be_visible()
                expect(page.locator("body", has_text="Export account data")).to_be_visible()
                expect(page.locator("[data-account-page-sign-out] button")).to_be_visible()
                assert density["logoutVisible"] is True
            elif route == "/app/settings/google":
                expect(page.locator("body", has_text="Google sign-in")).to_be_visible()
                expect(page.locator("body", has_text=re.compile(r"Connect Google|Add Google account", re.I))).to_be_visible()
                expect(page.locator("body", has_text=re.compile(r"account", re.I))).to_be_visible()
            elif route == "/app/settings/access":
                expect(page.locator("body", has_text=re.compile(r"Access|Sign-in and access", re.I))).to_be_visible()
                expect(page.locator("body", has_text=re.compile(r"Invite|access", re.I))).to_be_visible()
            elif route == "/app/settings/usage":
                expect(page.locator("body", has_text="Usage")).to_be_visible()
                expect(page.locator("body", has_text=re.compile(r"activation|usage", re.I))).to_be_visible()
            elif route == "/app/settings/support":
                expect(page.locator("body", has_text="Support at a glance")).to_be_visible()
                expect(page.locator("body", has_text="See what failed, what still works, and the next useful action.")).to_be_visible()
            elif route == "/app/settings/trust":
                expect(page.locator("body", has_text=re.compile(r"Search health|site health", re.I))).to_be_visible()
                expect(page.locator("body", has_text=re.compile(r"settings|retention|status", re.I))).to_be_visible()
            else:
                expect(page.locator("body", has_text=re.compile(r"Invite|Invitations", re.I))).to_be_visible()
                expect(page.locator("body", has_text=re.compile(r"access|collaborator|share", re.I))).to_be_visible()

            screenshot_path = tmp_path / screenshot_name
            page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled", caret="hide")
            assert screenshot_path.exists() and screenshot_path.stat().st_size > 16_000
    finally:
        context.close()


def test_propertyquarry_mobile_dark_mode_covers_secondary_surfaces(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "1020 Vienna",
            "selected_platforms": ["willhaben", "derstandard_at"],
            "search_agents": [
                {
                    "agent_id": "watch-1020-mobile-dark",
                    "name": "Leopoldstadt dark mobile watch",
                    "enabled": True,
                    "country_code": "AT",
                    "region_code": "vienna",
                    "location_query": "1020 Vienna",
                    "listing_mode": "rent",
                    "property_type": "apartment",
                    "duration_days": 90,
                    "notification_limit": 3,
                    "notification_period": "day",
                    "preferences_json": {
                        "country_code": "AT",
                        "region_code": "vienna",
                        "location_query": "1020 Vienna",
                        "listing_mode": "rent",
                        "property_type": "apartment",
                        "selected_platforms": ["willhaben", "derstandard_at"],
                    },
                },
            ],
        },
    )
    assert stored.status_code == 200, stored.text

    selectors = [
        ".pqx-topbar",
        ".pqx-shell *",
        ".pqx-primary-nav a",
        ".pqx-run-chip",
        ".pqx-button:not(.primary)",
        ".pqx-link-button:not(.primary)",
        ".pqx-card",
        ".pqx-field input:not([type='checkbox'])",
        ".pqx-field select",
        ".pqx-account-card",
        ".pqx-account-channel-option",
        ".pqx-account-channel-detail",
        ".pqx-account-channel-detail input",
        ".pqx-billing-card",
        ".pqx-automation-card",
        ".pqx-automation-thumbnail",
        ".pqx-empty",
        ".pqx-bottom-nav",
        ".pq-pack-shell",
        ".pq-pack-button",
        ".pq-pack-panel",
        ".pq-pack-card",
        ".pq-pack-input",
        ".pq-pack-pill",
        ".pq-pack-empty",
    ]

    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    context.add_init_script("window.localStorage.setItem('propertyquarry.theme', 'dark');")
    _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
    routes = [
        ("/app/search", "propertyquarry-search-mobile-dark.png"),
        ("/app/agents", "propertyquarry-agents-mobile-dark.png"),
        ("/app/account", "propertyquarry-account-mobile-dark.png"),
        ("/app/billing", "propertyquarry-billing-mobile-dark.png"),
        ("/app/settings/google", "propertyquarry-settings-google-mobile-dark.png"),
        ("/app/settings/access", "propertyquarry-settings-access-mobile-dark.png"),
        ("/app/settings/outcomes", "propertyquarry-settings-outcomes-mobile-dark.png"),
        ("/app/properties/packets", "propertyquarry-packets-mobile-dark.png"),
    ]
    try:
        page = context.new_page()
        for route, screenshot_name in routes:
            response = page.goto(f"{base_url}{route}", wait_until="networkidle")
            assert response is not None
            _assert_no_horizontal_overflow(page)
            if route == "/app/billing":
                assert response.ok
                _assert_propertyquarry_billing_redirect_lands_on_account(page)
            else:
                assert response.ok
                expect(page.locator("html")).to_have_attribute("data-pq-theme", "dark")
                _assert_dark_mode_surfaces_stay_readable(page, selectors)
                if route != "/app/properties/packets":
                    _assert_property_shell_visual_gates(page, max_appbar_height=130)
            screenshot_path = tmp_path / screenshot_name
            page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled", caret="hide")
            assert screenshot_path.exists() and screenshot_path.stat().st_size > 14_000
    finally:
        context.close()


def test_propertyquarry_mobile_settings_surfaces_keep_consistent_top_navigation(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    routes = [
        ("/app/settings/plan", "propertyquarry-settings-plan-mobile-topnav.png"),
        ("/app/settings/google", "propertyquarry-settings-google-mobile-topnav.png"),
        ("/app/settings/access", "propertyquarry-settings-access-mobile-topnav.png"),
        ("/app/settings/outcomes", "propertyquarry-settings-outcomes-mobile-topnav.png"),
    ]
    expected_labels = ["Search", "Shortlist", "Research", "Account"]
    try:
        page = context.new_page()
        for route, screenshot_name in routes:
            response = page.goto(f"{base_url}{route}", wait_until="networkidle")
            assert response is not None and response.ok
            _assert_no_horizontal_overflow(page)
            _assert_property_shell_visual_gates(page, max_appbar_height=150)
            top_nav = page.locator("[data-property-console-topnav]").first
            expect(top_nav).to_be_visible()
            nav_labels = top_nav.locator("a, span").evaluate_all(
                "(nodes) => nodes.map((node) => node.textContent.trim()).filter(Boolean)"
            )
            assert nav_labels == expected_labels
            expect(top_nav.locator('[aria-current="page"]')).to_have_text("Account")
            metrics = top_nav.evaluate(
                """(node) => {
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    const active = node.querySelector('[aria-current="page"]');
                    const activeRect = active ? active.getBoundingClientRect() : null;
                    return {
                        display: style.display,
                        overflowX: style.overflowX,
                        width: rect.width,
                        viewportWidth: window.innerWidth,
                        activeTop: activeRect ? activeRect.top : 0,
                        activeBottom: activeRect ? activeRect.bottom : 0,
                        navTop: rect.top,
                        navBottom: rect.bottom,
                    };
                }"""
            )
            assert metrics["display"] == "flex"
            assert metrics["overflowX"] in {"auto", "scroll"}
            assert metrics["width"] <= metrics["viewportWidth"] + 1
            assert metrics["activeTop"] >= metrics["navTop"] - 1
            assert metrics["activeBottom"] <= metrics["navBottom"] + 1
            screenshot_path = tmp_path / screenshot_name
            page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled", caret="hide")
            assert screenshot_path.exists() and screenshot_path.stat().st_size > 16_000
    finally:
        context.close()


def test_propertyquarry_setup_summary_tiles_do_not_clip_and_sideframe_stays_compact(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/properties", wait_until="networkidle")
        assert response is not None and response.ok
        page.locator(".pqx-setup").wait_for(state="visible")
        page.evaluate(
            """
            () => {
              const list = document.querySelector('[data-pqx-previous-searches]');
              if (!list || list.querySelector('[data-pqx-layout-gate-card]')) return;
              const card = document.createElement('article');
              card.className = 'pqx-previous-search';
              card.setAttribute('data-pqx-previous-search-card', '');
              card.setAttribute('data-pqx-layout-gate-card', '');
              const longTitle = 'Review apartment alert: Ruhige und moderne 2-Zimmer Wohnung - sehr gepflegt - optimale Raumaufteilung / Top Innenhoflage mit extra langem Titel fuer Layout Gate';
              const scopeSvg = encodeURIComponent(`
                <svg xmlns="http://www.w3.org/2000/svg" width="320" height="184" viewBox="0 0 320 184" fill="none">
                  <rect width="320" height="184" rx="18" fill="#1E1A15"/>
                  <rect x="12" y="12" width="296" height="160" rx="14" fill="#F3ECDD"/>
                  <rect x="38" y="34" width="44" height="24" rx="8" fill="#D7D0C3" stroke="#BBAF9B"/>
                  <rect x="92" y="42" width="66" height="42" rx="10" fill="#D44F4F" fill-opacity="0.42" stroke="#F2A3A3" stroke-width="2"/>
                  <rect x="166" y="62" width="58" height="34" rx="10" fill="#D7D0C3" stroke="#BBAF9B"/>
                  <rect x="122" y="98" width="76" height="32" rx="10" fill="#D7D0C3" stroke="#BBAF9B"/>
                </svg>`);
              card.innerHTML = `
                <div class="pqx-previous-scope-preview">
                  <div class="pqx-previous-district-hotspots" aria-hidden="true">
                    <span
                      class="pqx-previous-district-hotspot"
                      aria-hidden="true"
                      style="position:absolute; left: 31.1%; top: 26.25%; width: 22.297%; height: 26.25%;"></span>
                    <span
                      class="pqx-previous-district-hotspot is-selected"
                      aria-hidden="true"
                      style="position:absolute; left: 56.081%; top: 38.75%; width: 19.595%; height: 21.25%;"></span>
                  </div>
                  <button
                    class="pqx-previous-scope-trigger"
                    type="button"
                    data-pqx-scope-open
                    data-pqx-scope-image="data:image/svg+xml;utf8,${scopeSvg}"
                    data-pqx-scope-alt="Search area preview for 1020 Vienna"
                    data-pqx-scope-title="${longTitle}"
                    data-pqx-scope-caption="1020 Vienna, 1030 Vienna">
                    <img class="pqx-previous-scope-image" data-pqx-scope-preview src="data:image/svg+xml;utf8,${scopeSvg}" alt="Search area preview for 1020 Vienna">
                  </button>
                  <div class="pqx-previous-scope-hover" aria-hidden="true">
                    <img src="data:image/svg+xml;utf8,${scopeSvg}" alt="Search area preview for 1020 Vienna">
                    <span class="pqx-note">1020 Vienna, 1030 Vienna</span>
                  </div>
                  <div class="pqx-previous-scope-caption">
                    <span class="pqx-note">1020 Vienna, 1030 Vienna</span>
                  </div>
                </div>
                <div class="pqx-previous-body">
                  <div class="pqx-previous-search-head">
                    <div>
                      <strong class="pqx-previous-title">${longTitle}</strong>
                      <span class="pqx-note">Buy · Vienna · AT</span>
                    </div>
                  </div>
                  <div class="pqx-previous-metrics">
                    <span><b>4</b> ranked</span>
                    <span><b>2</b> sent</span>
                    <span>top match</span>
                  </div>
                  <div class="pqx-note">Best match is still below the shortlist threshold.</div>
                </div>
                <div class="pqx-previous-actions">
                  <a class="pqx-link-button primary" href="#">Open</a>
                  <button class="pqx-link-button subtle" type="button">Delete</button>
                </div>`;
              list.prepend(card);
            }
            """
        )

        layout = page.evaluate(
            """
            () => {
              const stage = document.querySelector('.pqx-setup');
              const intro = document.querySelector('.pqx-setup-intro');
              const command = document.querySelector('.pqx-command');
              const rows = Array.from(document.querySelectorAll('.pqx-setup-intro .pqx-dashboard-row'));
              const previousCards = Array.from(document.querySelectorAll('[data-pqx-previous-search-card]'));
              const previews = Array.from(document.querySelectorAll('[data-pqx-scope-preview]'));
              const overflowRows = rows.filter((node) => node.scrollWidth > node.clientWidth + 1 || node.scrollHeight > node.clientHeight + 1);
              const strongOverflow = rows
                .map((node) => node.querySelector('strong'))
                .filter(Boolean)
                .filter((node) => node.scrollWidth > node.clientWidth + 1 || node.scrollHeight > node.clientHeight + 1);
              const previousCardHorizontalOverflow = previousCards.filter((node) => {
                const hoveredOverlay = node.querySelector('.pqx-previous-scope-hover');
                const visibleWidth = hoveredOverlay ? hoveredOverlay.getBoundingClientRect().width : 0;
                return node.scrollWidth > node.clientWidth + Math.max(1, visibleWidth);
              });
              const previewFailures = previews.filter((node) => !(node.complete && node.naturalWidth > 0 && node.clientHeight > 0));
              const previousTitleVisibleOverflow = previousCards
                .flatMap((card) => {
                  const cardBox = card.getBoundingClientRect();
                  return Array.from(card.querySelectorAll('.pqx-previous-title')).map((node) => ({
                    cardBox,
                    nodeBox: node.getBoundingClientRect(),
                  }));
                })
                .filter(({ cardBox, nodeBox }) => nodeBox.left < cardBox.left - 1 || nodeBox.right > cardBox.right + 1);
              const previousPreviewPlacementFailures = previousCards
                .map((card) => {
                  const preview = card.querySelector('.pqx-previous-scope-preview');
                  const body = card.querySelector('.pqx-previous-body');
                  if (!preview || !body) return true;
                  return preview.getBoundingClientRect().top > body.getBoundingClientRect().top + 1;
                })
                .filter(Boolean);
              const stageBox = stage?.getBoundingClientRect();
              const introBox = intro?.getBoundingClientRect();
              const commandBox = command?.getBoundingClientRect();
              return {
                stageWidth: stageBox?.width || 0,
                introWidth: introBox?.width || 0,
                commandWidth: commandBox?.width || 0,
                overflowRowCount: overflowRows.length,
                strongOverflowCount: strongOverflow.length,
                previousCardCount: previousCards.length,
                previousCardHorizontalOverflowCount: previousCardHorizontalOverflow.length,
                previewFailureCount: previewFailures.length,
                previousTitleVisibleOverflowCount: previousTitleVisibleOverflow.length,
                previousPreviewPlacementFailureCount: previousPreviewPlacementFailures.length,
                visibleRowLabels: rows.map((node) => (node.querySelector('span')?.textContent || '').trim()),
                visibleRowValues: rows.map((node) => (node.querySelector('strong')?.textContent || '').trim()),
              };
            }
            """
        )

        assert layout["stageWidth"] > 0
        assert layout["introWidth"] / layout["stageWidth"] <= 0.36
        assert layout["commandWidth"] / layout["stageWidth"] >= 0.58
        assert layout["overflowRowCount"] == 0
        assert layout["strongOverflowCount"] == 0
        assert layout["previousCardCount"] >= 1
        assert layout["previousCardHorizontalOverflowCount"] == 0
        assert layout["previewFailureCount"] == 0
        assert layout["previousTitleVisibleOverflowCount"] == 0
        assert layout["previousPreviewPlacementFailureCount"] == 0
        assert "Saved searches" in set(layout["visibleRowLabels"])
        assert len([value for value in layout["visibleRowValues"] if value]) >= 2
        page.locator('[data-pqx-layout-gate-card] .pqx-previous-scope-trigger[data-pqx-scope-open]').evaluate(
            "(node) => node.click()"
        )
        page.locator('[data-pqx-scope-lightbox]').wait_for(state="visible")
        lightbox_state = page.evaluate(
            """
            () => {
              const dialog = document.querySelector('[data-pqx-scope-lightbox]');
              const image = dialog?.querySelector('[data-pqx-scope-lightbox-image]');
              return {
                open: Boolean(dialog && dialog.open),
                width: image?.naturalWidth || 0,
                src: image?.getAttribute('src') || '',
              };
            }
            """
        )
        assert lightbox_state["open"] is True, lightbox_state
        assert lightbox_state["width"] > 0
        assert lightbox_state["src"].startswith("data:image/svg+xml")
        _assert_property_shell_visual_gates(page, max_appbar_height=92)
    finally:
        context.close()


def test_propertyquarry_setup_wizard_numeric_sliders_are_mobile_friendly(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/properties", wait_until="networkidle")
        assert response is not None and response.ok
        form = page.locator('[data-console-form-variant="property_search"]').first
        form.wait_for(state="visible")
        expect(form).to_have_attribute("data-property-active-step", "search")
        page.locator('[data-property-step-trigger="what"]').click()
        page.wait_for_function("document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'what'")

        expected_search_sliders = {
            "max_price_eur": "Any budget",
            "min_rooms": "Any rooms",
            "min_area_m2": "Any size",
        }
        for name, empty_label in expected_search_sliders.items():
            shell = page.locator(f'[data-range-control="{name}"]')
            slider = shell.locator('input[type="range"]')
            assert shell.is_visible()
            assert slider.is_visible()
            assert slider.get_attribute("data-range-empty-label") == empty_label
            shell_box = shell.bounding_box()
            slider_box = slider.bounding_box()
            assert shell_box is not None and shell_box["width"] <= 430
            assert slider_box is not None and slider_box["height"] >= 32

        price_slider = page.locator('input[name="max_price_eur"]')
        assert price_slider.get_attribute("max") == "6000"
        assert page.locator('[data-range-value-for="max_price_eur"]').inner_text().strip() == "Any budget"
        page.locator('[data-property-step-trigger="search"]').click()
        page.wait_for_function("document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'search'")
        page.select_option('select[name="listing_mode"]', "buy")
        page.locator('[data-property-step-trigger="what"]').click()
        page.wait_for_function("document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'what'")
        assert price_slider.get_attribute("max") == "2000000"
        assert page.locator('[data-range-control="max_price_eur"] [data-range-scale-max]').inner_text().strip() == "EUR 2M"

        page.locator('[data-property-step-trigger="providers"]').click()
        page.wait_for_function("document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'providers'")
        assert page.locator('input[name="max_results_per_source"]').count() == 0
        assert page.locator('input[name="min_match_score"]').count() == 0
        _assert_property_shell_visual_gates(page, max_appbar_height=130)
    finally:
        context.close()


def test_propertyquarry_mobile_provider_family_controls_select_and_clear_cleanly(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page: Page = context.new_page()
    screenshot_path = tmp_path / "propertyquarry-mobile-provider-family-controls.png"
    try:
        response = page.goto(f"{base_url}/app/properties", wait_until="networkidle")
        assert response is not None and response.ok
        form = page.locator('[data-console-form-variant="property_search"]').first
        form.wait_for(state="visible")
        expect(form).to_have_attribute("data-property-active-step", "search")
        page.locator('[data-property-step-trigger="providers"]').click()
        page.wait_for_function("document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'providers'")
        _assert_no_horizontal_overflow(page)

        expected_provider_cap = page.locator('[data-console-form-variant="property_search"]').evaluate(
            """(form) => {
              const raw = String(document.querySelector('[data-property-workspace-meta]')?.getAttribute('data-property-workspace-meta') || '').trim();
              const meta = raw ? JSON.parse(raw) : {};
              return Number(meta?.commercial?.max_platforms || 0);
            }"""
        )
        assert isinstance(expected_provider_cap, int)
        assert expected_provider_cap > 0

        first_provider_family = page.locator('[data-provider-group-panel]').first
        first_provider_family.locator("summary").scroll_into_view_if_needed()
        first_provider_family.locator("summary").click()

        add_button = first_provider_family.get_by_role("button", name="Add family")
        clear_button = first_provider_family.get_by_role("button", name="Clear family")
        expect(add_button).to_be_visible()
        expect(clear_button).to_be_visible()

        button_metrics = page.evaluate(
            """() => {
              const add = [...document.querySelectorAll('[data-provider-group-panel] button')]
                .find((node) => (node.textContent || '').trim() === 'Add family');
              const clear = [...document.querySelectorAll('[data-provider-group-panel] button')]
                .find((node) => (node.textContent || '').trim() === 'Clear family');
              const addRect = add ? add.getBoundingClientRect() : null;
              const clearRect = clear ? clear.getBoundingClientRect() : null;
              return {
                viewportWidth: window.innerWidth,
                viewportHeight: window.innerHeight,
                addRight: addRect ? addRect.right : 0,
                clearRight: clearRect ? clearRect.right : 0,
                addBottom: addRect ? addRect.bottom : 0,
                clearBottom: clearRect ? clearRect.bottom : 0,
              };
            }"""
        )
        assert button_metrics["addRight"] <= button_metrics["viewportWidth"] + 1
        assert button_metrics["clearRight"] <= button_metrics["viewportWidth"] + 1
        assert button_metrics["addBottom"] <= button_metrics["viewportHeight"] + 80
        assert button_metrics["clearBottom"] <= button_metrics["viewportHeight"] + 80

        family_provider_count = first_provider_family.locator('input[name="selected_platforms"]:not([disabled])').count()
        assert family_provider_count > 0

        add_button.click()
        checked_family_provider_count = first_provider_family.locator('input[name="selected_platforms"]:checked').count()
        checked_total_after_family = page.locator('input[name="selected_platforms"]:checked').count()
        assert checked_family_provider_count == min(family_provider_count, expected_provider_cap)
        assert checked_total_after_family == checked_family_provider_count

        clear_button.click()
        expect(first_provider_family.locator('input[name="selected_platforms"]:checked')).to_have_count(0)
        expect(page.locator('input[name="selected_platforms"]:checked')).to_have_count(0)

        page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled", caret="hide")
        assert screenshot_path.exists() and screenshot_path.stat().st_size > 16_000
        _assert_property_shell_visual_gates(page, max_appbar_height=130)
    finally:
        context.close()


def test_propertyquarry_start_failure_explains_backend_reason(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        def _delayed_failure(route):
            time.sleep(1.2)
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({"detail": "property_plan_upgrade_required:plus"}),
            )

        page.route(
            "**/v1/onboarding/property-search/preferences",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "saved"}),
            ),
        )
        page.route("**/app/api/property/search-runs**", _delayed_failure)
        response = page.goto(f"{base_url}/app/properties", wait_until="networkidle")
        assert response is not None and response.ok
        page.select_option('select[name="country_code"]', "AT")
        page.get_by_role("button", name="Select all areas", exact=True).click()
        page.locator('[data-property-step-trigger="providers"]').click()
        expect(page.locator('[data-console-form-variant="property_search"]')).to_have_attribute(
            "data-property-active-step",
            "providers",
        )
        page.locator("[data-property-start-top]").click()
        page.wait_for_function(
            """
            () => {
              const button = document.querySelector('[data-property-start-top]');
              const inlineError = document.querySelector('[data-property-inline-error]');
              const sawLoading = Boolean(
                button
                && button.getAttribute('aria-busy') === 'true'
                && button.getAttribute('data-pqx-loading') === 'true'
                && String(button.textContent || '').includes('Launching...')
              );
              const sawBackendFailure = Boolean(
                inlineError
                && String(inlineError.textContent || '').includes('Upgrade required for this search')
              );
              return sawLoading || sawBackendFailure;
            }
            """
        )
        inline_error = page.locator("[data-property-inline-error]")
        expect(inline_error).to_contain_text("Upgrade required for this search")
        expect(inline_error).to_contain_text("plus plan")
        launch_button = page.locator("[data-property-start-top]")
        expect(launch_button).to_have_attribute("aria-busy", "false")
        expect(launch_button.locator(".pqx-top-start-label-full")).to_have_text("Launch search")
        expect(launch_button.locator(".pqx-top-start-label-short")).to_have_text("Search")
    finally:
        context.close()


def test_propertyquarry_agent_can_launch_all_austria_providers_from_real_browser(
    monkeypatch: pytest.MonkeyPatch,
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    selected_platforms = list(
        selectable_property_platform_keys(country_code="AT", listing_mode="rent")
    )
    assert 24 < len(selected_platforms) <= 64
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "en",
            "listing_mode": "rent",
            "property_type": ["apartment"],
            "region_code": "vienna",
            "location_query": "Vienna",
            "selected_location_values": ["1020 Vienna"],
            "selected_platforms": selected_platforms,
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )
    assert stored.status_code == 200, stored.text

    monkeypatch.setattr(
        ProductService,
        "sync_direct_property_scout",
        lambda self, **_kwargs: {
            "generated_at": "2026-07-29T12:30:00+00:00",
            "status": "processed",
            "sources_total": len(selected_platforms),
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "email_notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        },
    )

    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.locator('input[name="selected_platforms"]:checked')).to_have_count(
            len(selected_platforms)
        )

        with page.expect_response("**/app/api/property/search-runs") as start_response:
            page.locator("[data-property-start-top]").click()

        assert start_response.value.status == 202
        page.wait_for_url(
            re.compile(r".*/app/properties\?run_id=.*"),
            timeout=10_000,
        )
        assert "Could not start property search" not in page.locator("body").inner_text()
    finally:
        context.close()


def test_propertyquarry_launch_posts_real_start_payload_and_shows_run_status(
    monkeypatch: pytest.MonkeyPatch,
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    observed: dict[str, object] = {}

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["principal_id"] = principal_id
        observed["selected_platforms"] = tuple(selected_platforms)
        observed["property_search_preferences"] = dict(property_search_preferences or {})
        observed["max_results_per_source"] = max_results_per_source
        if callable(progress_callback):
            progress_callback(
                step="sources_resolved",
                message="Resolved 1 source for launch smoke.",
                status="in_progress",
                steps_delta=1,
                summary_updates={"sources_total": 1},
            )
        return {
            "generated_at": "2026-06-05T00:00:00+00:00",
            "status": "processed",
            "sources_total": 1,
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "email_notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/properties", wait_until="networkidle")
        assert response is not None and response.ok
        page.select_option('select[name="country_code"]', "AT")
        page.get_by_role("button", name="Select all areas", exact=True).click()
        page.locator('[data-property-step-trigger="children"]').click()
        page.wait_for_function("document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'children'")
        assert page.locator('[data-property-field-name="enable_family_mode"]').count() == 0
        page.locator('details[data-what-matters-group]').evaluate_all(
            "groups => groups.forEach((group) => { group.open = true; })"
        )
        page.locator('[data-keyword-priority-row][data-keyword-value="good air quality"] [data-keyword-preference-select]').select_option("strong_wish")
        page.locator('[data-keyword-priority-row][data-keyword-value="low crime area"] [data-keyword-preference-select]').select_option("strong_wish")
        page.locator('[data-keyword-priority-row][data-keyword-value="parking pressure check"] [data-keyword-preference-select]').select_option("high")
        page.locator('[data-keyword-priority-row][data-keyword-value="water and groundwater check"] [data-keyword-preference-select]').select_option("must_have")
        page.locator('[data-keyword-priority-row][data-keyword-value="avoid septic risk"] [data-keyword-preference-select]').select_option("avoid")
        page.locator('[data-keyword-priority-row][data-keyword-value="winter driving check"] [data-keyword-preference-select]').select_option("must_have")
        page.locator('[data-keyword-priority-row][data-keyword-value="avoid flood-risk area"] [data-keyword-preference-select]').select_option("avoid")
        page.locator('[data-keyword-priority-row][data-keyword-value="market nearby"] [data-keyword-preference-select]').select_option("strong_wish")
        page.locator('[data-keyword-priority-row][data-keyword-value="market nearby"] [data-keyword-distance-select]').select_option("1000")
        page.locator('[data-keyword-priority-row][data-keyword-value="Baumarkt nearby"] [data-keyword-preference-select]').select_option("strong_wish")
        page.locator('[data-keyword-priority-row][data-keyword-value="Baumarkt nearby"] [data-keyword-distance-select]').select_option("2000")
        page.locator('[data-keyword-priority-row][data-keyword-value="shopping center nearby"] [data-keyword-preference-select]').select_option("avoid")
        page.locator('[data-keyword-priority-row][data-keyword-value="shopping center nearby"] [data-keyword-distance-select]').select_option("500")
        page.locator('[data-keyword-priority-row][data-keyword-value="library nearby"] [data-keyword-preference-select]').select_option("nice_to_have")
        page.locator('[data-keyword-priority-row][data-keyword-value="library nearby"] [data-keyword-distance-select]').select_option("1000")
        page.locator('[data-keyword-priority-row][data-keyword-value="medical care nearby"] [data-keyword-preference-select]').select_option("strong_wish")
        page.locator('[data-keyword-priority-row][data-keyword-value="medical care nearby"] [data-keyword-distance-select]').select_option("1000")
        page.locator('[data-property-step-trigger="providers"]').click()
        page.wait_for_function("document.querySelector('[data-console-form-variant=\"property_search\"]')?.dataset.propertyActiveStep === 'providers'")
        providerCount = page.locator('input[name="selected_platforms"]:not([disabled])').count()
        expectedProviderCap = page.locator('[data-console-form-variant="property_search"]').evaluate(
            """(form) => {
              const raw = String(document.querySelector('[data-property-workspace-meta]')?.getAttribute('data-property-workspace-meta') || '').trim();
              const meta = raw ? JSON.parse(raw) : {};
              return Number(meta?.commercial?.max_platforms || 0);
            }"""
        )
        assert isinstance(expectedProviderCap, int)
        assert expectedProviderCap > 0
        allSourcesButton = page.locator('[data-checkbox-group-select-all="selected_platforms"]')
        assert allSourcesButton.is_visible()
        assert f"Select {expectedProviderCap} of {providerCount}" in allSourcesButton.inner_text()
        firstProviderFamily = page.locator('[data-provider-group-panel]').first
        firstProviderFamily.locator("summary").click()
        assert firstProviderFamily.get_by_role("button", name="Add family").is_visible()
        assert firstProviderFamily.get_by_role("button", name="Clear family").is_visible()
        familyProviderCount = firstProviderFamily.locator('input[name="selected_platforms"]:not([disabled])').count()
        assert familyProviderCount > 0
        firstProviderFamily.get_by_role("button", name="Add family").click()
        checkedFamilyProviderCount = firstProviderFamily.locator('input[name="selected_platforms"]:checked').count()
        checkedTotalAfterFamily = page.locator('input[name="selected_platforms"]:checked').count()
        assert checkedFamilyProviderCount == min(familyProviderCount, expectedProviderCap)
        assert checkedTotalAfterFamily == checkedFamilyProviderCount
        allSourcesButton.click()
        checkedProviderCount = page.locator('input[name="selected_platforms"]:checked').count()
        assert providerCount > checkedProviderCount
        assert checkedProviderCount == expectedProviderCap
        assert page.locator('[data-provider-group-panel][open]').count() >= 1
        assert page.locator('[data-property-inline-status]', has_text=f"Selected {expectedProviderCap} of {providerCount} providers").is_visible()
        page.locator('input[name="require_floorplan"]').check()

        with page.expect_response("**/app/api/property/search-runs") as start_response:
            page.locator("[data-property-start-top]").click()
        response = start_response.value
        assert response.ok
        try:
            page.wait_for_url(re.compile(r".*/app/(properties|shortlist)\?run_id=.*"), timeout=10000)
        except Exception:
            page.wait_for_load_state("domcontentloaded")
        run_id = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query).get("run_id", [""])[0]
        assert run_id
        page.wait_for_selector(
            "[data-pqx-finished-compare], [data-pqx-empty-results], [data-pqx-screenfit-target=\"run-progress\"], [data-workbench-results-table], [data-pqx-mobile-panel=\"results\"]",
            timeout=10000,
        )
        assert "Could not start property search" not in page.locator("body").inner_text()
        deadline = time.time() + 5.0
        while "property_search_preferences" not in observed and time.time() < deadline:
            time.sleep(0.05)
        assert observed["principal_id"] == "pq-greenfield-browser"
        preferences = dict(observed["property_search_preferences"])
        assert preferences["country_code"] == "AT"
        assert preferences["region_code"] == "vienna"
        assert preferences["full_region_scope"] is True
        assert preferences["location_query"] == "Vienna"
        assert preferences["prefer_good_air_quality"] is True
        assert preferences["prefer_low_crime_area"] is True
        assert preferences["parking_pressure_preference"] == "high"
        assert preferences["require_parking_pressure_check"] is True
        assert preferences["require_drinking_water_quality_research"] is True
        assert preferences["avoid_cesspit_or_septic_risk"] is True
        assert preferences["require_winter_access_research"] is True
        assert preferences["avoid_flood_risk_area"] is True
        assert preferences["max_distance_to_market_m"] == 1000
        assert preferences["max_distance_to_market_importance"] == "strong_wish"
        assert preferences["max_distance_to_hardware_store_m"] == 2000
        assert preferences["max_distance_to_hardware_store_importance"] == "strong_wish"
        assert preferences["max_distance_to_shopping_center_m"] == 500
        assert preferences["max_distance_to_shopping_center_importance"] == "avoid"
        assert preferences["max_distance_to_medical_care_m"] == 1000
        assert preferences["max_distance_to_medical_care_importance"] == "strong_wish"
        assert preferences["max_distance_to_library_m"] == 1000
        assert preferences["max_distance_to_library_importance"] == "nice_to_have"
        assert preferences.get("min_match_score", 0) in {0, 0.0}
        assert preferences["require_floorplan"] is True
        assert len(observed["selected_platforms"]) == 3
        assert page.locator("body", has_text="Altbau near U6").is_visible()
        assert page.locator("body", has_text="Open property").is_visible()
        tour_link = page.locator("[data-workbench-row]", has_text="Altbau near U6").first.get_by_role("link", name="3D tour")
        expect(tour_link).to_be_visible()
        assert str(tour_link.get_attribute("href") or "").endswith("/tours/altbau-u6/control/3dvista")
        page.locator("[data-workbench-row]", has_text="Altbau near U6").locator(".pqx-result-title").click()
        selected_query = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
        assert urllib.parse.urlparse(page.url).path == "/app/properties"
        assert selected_query.get("run_id", [""])[0] == run_id
        assert selected_query.get("candidate", [""])[0]
        assert page.locator("body", has_text="Altbau near U6").is_visible()

        bottom_launch_page = context.new_page()
        response = bottom_launch_page.goto(
            f"{base_url}/app/properties",
            wait_until="networkidle",
        )
        assert response is not None and response.ok
        bottom_launch_page.select_option('select[name="country_code"]', "AT")
        bottom_launch_page.get_by_role(
            "button", name="Select all areas", exact=True
        ).click()
        bottom_launch_page.locator('[data-property-step-trigger="providers"]').click()
        bottom_launch_page.locator(
            '[data-checkbox-group-select-all="selected_platforms"]'
        ).click()
        with bottom_launch_page.expect_response(
            "**/app/api/property/search-runs"
        ) as bottom_start_response:
            bottom_launch_page.locator("[data-property-step-next]:visible").click()
        assert bottom_start_response.value.ok
        bottom_launch_page.wait_for_url(
            re.compile(r".*/app/(properties|shortlist)\?run_id=.*"),
            timeout=10000,
        )
        assert "Could not start property search" not in bottom_launch_page.locator(
            "body"
        ).inner_text()
    finally:
        context.close()


def test_propertyquarry_best_match_opens_hosted_3d_tour_and_flythrough_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    console_errors: list[str] = []
    failed_requests: list[str] = []
    tour_file_responses: list[str] = []
    walkthrough_responses: list[str] = []
    error_responses: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("requestfailed", lambda request: failed_requests.append(f"{request.url} :: {request.failure}"))
    page.on(
        "response",
        lambda response: tour_file_responses.append(f"{response.status} {response.url}")
        if "/tours/files/" in response.url
        else None,
    )
    page.on(
        "response",
        lambda response: error_responses.append(f"{response.status} {response.url}")
        if response.status >= 400
        else None,
    )
    page.on(
        "response",
        lambda response: walkthrough_responses.append(f"{response.status} {response.url}")
        if response.url.split("?", 1)[0] == f"{base_url}/tours/altbau-u6/walkthrough"
        else None,
    )
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="domcontentloaded")
        assert response is not None and response.ok
        best_match = page.locator("[data-workbench-row]", has_text="Altbau near U6").first
        best_match.wait_for()
        expect(best_match.get_by_role("link", name="3D tour")).to_be_visible()
        expect(best_match.get_by_role("link", name="Open 3D tour")).to_have_count(0)
        open_3d_tour = best_match.get_by_role("link", name="3D tour")
        tour_url = str(open_3d_tour.get_attribute("href") or "").strip()
        assert tour_url.endswith("/tours/altbau-u6/control/3dvista")
        tour_entry = tour_url if tour_url.startswith("http") else f"{base_url}{tour_url}"
        page.wait_for_load_state("networkidle")
        response = page.goto(f"{tour_entry}?pane=floorplan-pane", wait_until="networkidle")
        assert response is not None and response.ok
        page.locator("h1").wait_for()
        assert page.locator("body", has_text="Altbau near U6").is_visible()
        page.locator('#role-filter button[data-role="floorplan"]').click()
        assert page.locator("#stage-role").inner_text().lower() == "floorplan"
        assert page.locator("#stage-image").is_visible()
        try:
            page.wait_for_function(
                "() => { const image = document.getElementById('stage-image'); return image && !image.hidden && image.complete && image.naturalWidth >= 1000; }",
                timeout=5000,
            )
        except PlaywrightTimeoutError as exc:
            image_state = page.evaluate(
                "() => { const image = document.getElementById('stage-image'); return image ? {src: image.src, complete: image.complete, naturalWidth: image.naturalWidth, hidden: image.hidden} : null; }"
            )
            raise AssertionError(
                f"Floorplan image did not decode. state={image_state}; responses={tour_file_responses}; failed={failed_requests}"
            ) from exc
        natural_width = page.evaluate("() => document.getElementById('stage-image')?.naturalWidth || 0")
        assert natural_width >= 1000

        expected_floorplan_switch_abort = (
            f"{base_url}/tours/files/altbau-u6/scene-01.png :: NS_BINDING_ABORTED"
        )
        assert failed_requests.count(expected_floorplan_switch_abort) <= 1
        assert [
            failure
            for failure in failed_requests
            if failure != expected_floorplan_switch_abort
        ] == [], f"floorplan responses={error_responses}; failed={failed_requests}"
        response = page.goto(f"{tour_entry}?pane=flythrough-pane&autoplay=1", wait_until="domcontentloaded")
        assert response is not None and response.ok
        video = page.locator("#flythrough-video, #tour-video").first
        video.wait_for()
        assert video.is_visible()
        page.evaluate(
            """() => {
                const video = document.getElementById('flythrough-video') || document.getElementById('tour-video');
                return video?.play?.()?.catch(() => null) || null;
            }"""
        )
        page.wait_for_timeout(1800)
        state = page.evaluate(
            """() => {
                const video = document.getElementById('flythrough-video') || document.getElementById('tour-video');
                return video ? {
                    currentTime: video.currentTime,
                    duration: video.duration,
                    readyState: video.readyState,
                    videoWidth: video.videoWidth,
                } : null;
            }"""
        )
        assert state is not None
        assert state["readyState"] >= 2
        assert state["videoWidth"] >= 640
        assert state["currentTime"] > 0.2
        assert state["duration"] >= 2.5
        assert _video_frame_brightness(page) > 10.0
        source_type = page.evaluate(
            """() => {
                const video = document.getElementById('flythrough-video') || document.getElementById('tour-video');
                return video?.querySelector('source')?.getAttribute('type') || '';
            }"""
        )
        assert source_type == "video/mp4"
        noisy_console_errors = [
            message
            for message in console_errors
            if (
                "decode" in message.lower()
                or "media" in message.lower()
                or "refused" in message.lower()
            )
            and not _is_expected_vendor_report_only_inline_script_console_error(message)
        ]
        assert not noisy_console_errors, f"console={noisy_console_errors}; responses={error_responses}; failed={failed_requests}"
        assert error_responses == []
        assert any(
            200 <= int(response.split(" ", 1)[0]) < 400
            for response in walkthrough_responses
        ), f"walkthrough={walkthrough_responses}; failed={failed_requests}"
        walkthrough_url = f"{base_url}/tours/altbau-u6/walkthrough"
        expected_navigation_abort_details = {
            "net::ERR_ABORTED",
            "NS_BINDING_ABORTED",
            "Load request cancelled",
        }
        navigation_aborts = [
            failure
            for failure in failed_requests
            if failure.partition(" :: ")[0] == walkthrough_url
            and failure.partition(" :: ")[2] in expected_navigation_abort_details
        ]
        assert len(navigation_aborts) <= 1, navigation_aborts
        assert failed_requests.count(expected_floorplan_switch_abort) <= 1
        assert [
            failure
            for failure in failed_requests
            if failure not in navigation_aborts
            and failure != expected_floorplan_switch_abort
        ] == []
    finally:
        context.close()


def _assert_generated_reconstruction_v4_viewer(
    page: Page,
    *,
    slug: str,
    style_id: str,
) -> None:
    expected_style = reconstruction_styles.reconstruction_style(
        style_id,
        style_id=style_id,
    )
    expected_style_scene = reconstruction_styles.build_style_scene(
        expected_style,
        route_stop_count=6,
    )
    assert urllib.parse.urlparse(page.url).path == (
        f"/tours/viewer/{slug}/generated-reconstruction/viewer.html"
    )
    viewer_shell = page.locator("[data-pq-reconstruction-viewer]")
    expect(viewer_shell).to_be_visible()
    expect(viewer_shell).to_have_attribute(
        "data-pq-preview-kind",
        "styled-3d-reconstruction",
    )
    expect(viewer_shell).to_have_attribute(
        "data-pq-style-id",
        str(expected_style["id"]),
    )
    expect(viewer_shell).to_have_attribute(
        "data-pq-style-signature",
        str(expected_style["signature"]),
    )
    expect(viewer_shell).to_have_attribute(
        "data-pq-style-scene-signature",
        str(expected_style_scene["scene_signature"]),
    )
    assert reconstruction_styles.FLOORPLAN_DISPLAY_MODE == (
        "reference_toggle_default_off"
    )
    expect(viewer_shell).to_have_attribute(
        "data-pq-floorplan-display-mode",
        "reference_toggle_default_off",
    )
    page.wait_for_function(
        "() => Boolean(window.__pqReconstructionDebug?.getRenderMetrics?.().ready)",
        timeout=10_000,
    )
    render_metrics = page.evaluate(
        "() => window.__pqReconstructionDebug.getRenderMetrics()"
    )
    assert int(render_metrics.get("renderCalls") or 0) > 0
    assert int(render_metrics.get("renderTriangles") or 0) > 0


def _write_generated_reconstruction_public_launch_fixture(
    bundle_root: Path,
    *,
    slug: str,
    style_id: str = "urban_jungle",
) -> tuple[list[str], list[str]]:
    bundle_dir = bundle_root / slug
    reconstruction_dir = bundle_dir / "generated-reconstruction"
    reconstruction_dir.mkdir(parents=True, exist_ok=True)
    viewer_vendor_dir = reconstruction_dir / "vendor"
    orbit_controls_dir = viewer_vendor_dir / "examples" / "jsm" / "controls"
    orbit_controls_dir.mkdir(parents=True, exist_ok=True)
    (viewer_vendor_dir / "three.module.js").write_text(
        'export const REVISION = "propertyquarry-browser-fixture";\n',
        encoding="utf-8",
    )
    (orbit_controls_dir / "OrbitControls.js").write_text(
        "export class OrbitControls {}\n",
        encoding="utf-8",
    )
    route_labels = [
        "entry/hall",
        "living area",
        "sleeping area",
        "balcony/terrace",
        "living area detail 2",
        "balcony/terrace return",
    ]
    walkthrough_route_labels = [
        "entry/hall",
        "living area",
        "sleeping area",
        "balcony/terrace",
        "living area detail 2",
        "balcony/terrace return",
        "balcony detail 2",
    ]
    selected_style = reconstruction_styles.reconstruction_style(
        style_id,
        style_id=style_id,
    )
    style_scene = reconstruction_styles.build_style_scene(
        selected_style,
        route_stop_count=len(route_labels),
    )
    style_contract_fields = {
        "viewer_version": reconstruction_styles.GENERATED_RECONSTRUCTION_VIEWER_VERSION,
        "style_contract_version": reconstruction_styles.STYLE_SCENE_CONTRACT_VERSION,
        "style_id": selected_style["id"],
        "style_label": selected_style["label"],
        "style_signature": selected_style["signature"],
        "style_scene_signature": style_scene["scene_signature"],
        "style_evidence_status": "ready",
        "styled_scene_instance_count": len(style_scene["instances"]),
        "style_cue_kinds": list(style_scene["required_cues"]),
        "floorplan_display_mode": reconstruction_styles.FLOORPLAN_DISPLAY_MODE,
    }
    disclosure = (
        "Generated interactive reconstruction from supplied listing material. "
        "It is a styled layout aid, not a captured or provider-verified 3D scan."
    )
    walkable_scene = {
        "kind": "generated_reconstruction_layout",
        "route": [
            {
                "label": label,
                "focus": {"x": 1.0 + (index * 0.9), "y": 1.45, "z": 0.5 + ((index % 2) * 0.35)},
                "camera": {"x": -1.25 + (index * 0.95), "y": 1.7, "z": 2.25 - (index * 0.12)},
            }
            for index, label in enumerate(route_labels)
        ],
        "rooms": [
            {
                "label": label,
                "position": {"x": -0.4 + (index * 0.95), "y": 0.0, "z": 0.3 + ((index % 2) * 0.4)},
                "focus": {"x": 1.0 + (index * 0.9), "y": 1.45, "z": 0.5 + ((index % 2) * 0.35)},
            }
            for index, label in enumerate(route_labels)
        ],
    }
    coverage_proof = {
        "status": "pass",
        "segments_expected": walkthrough_route_labels,
        "segments_visited": walkthrough_route_labels,
        "coverage_segments": [
            {"segment": "entry/hall", "start": 0.0, "end": 0.4},
            {"segment": "living area", "start": 0.4, "end": 0.8},
            {"segment": "sleeping area", "start": 0.8, "end": 1.2},
            {"segment": "balcony/terrace", "start": 1.2, "end": 1.6},
            {"segment": "living area detail 2", "start": 1.6, "end": 2.0},
            {"segment": "balcony/terrace return", "start": 2.0, "end": 2.4},
            {"segment": "balcony detail 2", "start": 2.4, "end": 3.0},
        ],
    }
    _write_floorplan_png(reconstruction_dir / "source-floorplan.png")
    _write_h264_flythrough(reconstruction_dir / "generated-walkthrough.mp4")
    _write_cube_face_png(bundle_dir / "diorama-preview.png", label="Generated diorama", fill=(128, 98, 76))
    photo_specs = [
        ("photo-01.png", "Entry", (89, 92, 104)),
        ("photo-02.png", "Living", (122, 88, 72)),
        ("photo-03.png", "Bedroom", (78, 104, 86)),
        ("photo-04.png", "Balcony", (110, 120, 82)),
        ("photo-05.png", "Living detail", (138, 104, 84)),
    ]
    for filename, label, fill in photo_specs:
        _write_cube_face_png(reconstruction_dir / filename, label=label, fill=fill)
    viewer_route_labels_json = json.dumps(route_labels, ensure_ascii=False)
    viewer_document = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Layout preview | PropertyQuarry</title>
    <style>
      :root {{ color-scheme: light; }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background:
          radial-gradient(circle at 18% 14%, rgba(255,255,255,.92), transparent 24%),
          linear-gradient(160deg, #f3e8d8 0%, #ddc7a9 54%, #b79973 100%);
        color: #17130c;
        font-family: Aptos, ui-sans-serif, system-ui, sans-serif;
      }}
      main {{
        width: min(880px, calc(100vw - 32px));
        min-height: min(72vh, 620px);
        display: grid;
        gap: 18px;
        align-content: start;
        padding: 28px;
        border-radius: 28px;
        border: 1px solid rgba(54, 42, 27, .12);
        background: rgba(255, 252, 245, .82);
        box-shadow: 0 24px 70px rgba(68, 47, 24, .12);
      }}
      .eyebrow {{
        color: #766d5e;
        font-size: 12px;
        font-weight: 700;
        letter-spacing: .12em;
        text-transform: uppercase;
      }}
      h1 {{
        margin: 0;
        font-family: Georgia, ui-serif, serif;
        font-size: clamp(30px, 4vw, 48px);
        line-height: .94;
        letter-spacing: -.05em;
      }}
      p {{
        margin: 0;
        color: #5d5141;
        line-height: 1.45;
      }}
      .viewer-stage {{
        display: grid;
        gap: 14px;
        padding: 18px;
        border-radius: 24px;
        border: 1px solid rgba(54, 42, 27, .1);
        background: linear-gradient(180deg, rgba(255,255,255,.74), rgba(248,242,232,.84));
      }}
      .viewer-route-label {{
        font-family: Georgia, ui-serif, serif;
        font-size: clamp(24px, 3vw, 34px);
        line-height: .96;
        letter-spacing: -.04em;
      }}
      .viewer-chip-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }}
      .viewer-chip {{
        min-height: 30px;
        display: inline-flex;
        align-items: center;
        padding: 0 11px;
        border-radius: 999px;
        background: rgba(167,124,43,.14);
        color: #6c4c16;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: .08em;
        text-transform: uppercase;
      }}
      .viewer-grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
      }}
      .viewer-card {{
        padding: 14px 15px;
        border-radius: 18px;
        border: 1px solid rgba(54, 42, 27, .08);
        background: rgba(255,255,255,.64);
      }}
      .viewer-card b {{
        display: block;
        margin-bottom: 4px;
        color: #766d5e;
        font-size: 11px;
        letter-spacing: .08em;
        text-transform: uppercase;
      }}
      @media (max-width: 720px) {{
        main {{ width: calc(100vw - 18px); padding: 18px; }}
        .viewer-grid {{ grid-template-columns: 1fr; }}
      }}
    </style>
  </head>
  <body>
    <main
      data-pq-reconstruction-viewer
      data-pq-preview-kind="styled-3d-reconstruction"
      data-pq-verified-provider-capture="false"
      data-pq-style-id="{selected_style["id"]}"
      data-pq-style-signature="{selected_style["signature"]}"
      data-pq-style-scene-signature="{style_scene["scene_signature"]}"
      data-pq-floorplan-display-mode="{reconstruction_styles.FLOORPLAN_DISPLAY_MODE}"
    >
      <div class="eyebrow">PropertyQuarry layout viewer</div>
      <h1>Generated reconstruction viewer</h1>
      <p>Styled {html.escape(str(selected_style["label"]))} fixture for the room route viewer. It exposes the same debug hooks the public launch uses to prove route sync and readiness.</p>
      <section class="viewer-stage">
        <div class="viewer-chip-row">
          <span class="viewer-chip" id="viewer-route-position">Stop 1 / {len(route_labels)}</span>
          <span class="viewer-chip" id="viewer-route-mode">Layout route</span>
        </div>
        <strong class="viewer-route-label" id="viewer-route-label">{html.escape(route_labels[0])}</strong>
        <div class="viewer-grid">
          <div class="viewer-card">
            <b>Current focus</b>
            <span id="viewer-focus-readout">{html.escape(route_labels[0])}</span>
          </div>
          <div class="viewer-card">
            <b>Render status</b>
            <span id="viewer-render-readout">Loading route renderer</span>
          </div>
        </div>
      </section>
    </main>
    <script type="module">
      import * as THREE from './vendor/three.module.js';
      import {{ OrbitControls }} from './vendor/examples/jsm/controls/OrbitControls.js';
      window.__pqFixtureViewerModules = {{
        three: Boolean(THREE.REVISION),
        orbitControls: typeof OrbitControls === 'function',
      }};
    </script>
    <script>
      const routeLabels = {viewer_route_labels_json};
      const routePosition = document.getElementById('viewer-route-position');
      const routeLabel = document.getElementById('viewer-route-label');
      const focusReadout = document.getElementById('viewer-focus-readout');
      const renderReadout = document.getElementById('viewer-render-readout');
      let activeIndex = 0;
      const metrics = {{
        ready: false,
        frameCount: 0,
        renderCalls: 0,
        renderTriangles: 0,
      }};
      function updateRoute(index) {{
        const normalized = Math.max(0, Math.min(routeLabels.length - 1, Number(index) || 0));
        activeIndex = normalized;
        const label = String(routeLabels[normalized] || `Stop ${{normalized + 1}}`).trim();
        routePosition.textContent = `Stop ${{normalized + 1}} / ${{routeLabels.length}}`;
        routeLabel.textContent = label;
        focusReadout.textContent = label;
      }}
      function markReady() {{
        metrics.ready = true;
        metrics.frameCount = 3;
        metrics.renderCalls = 4;
        metrics.renderTriangles = 2048;
        renderReadout.textContent = 'Ready';
      }}
      window.__pqReconstructionDebug = {{
        setRouteView(index) {{
          updateRoute(index);
          return activeIndex;
        }},
        getRenderMetrics() {{
          return {{ ...metrics }};
        }},
      }};
      updateRoute(0);
      window.requestAnimationFrame(() => window.requestAnimationFrame(markReady));
    </script>
  </body>
</html>
"""
    (reconstruction_dir / "viewer.html").write_text(
        viewer_document,
        encoding="utf-8",
    )
    (reconstruction_dir / "model.obj").write_text("o propertyquarry_generated_layout\nv 0 0 0\n", encoding="utf-8")
    (reconstruction_dir / "model.mtl").write_text("newmtl walls\nKd 0.8 0.8 0.8\n", encoding="utf-8")
    (reconstruction_dir / "generated-walkthrough.quality.json").write_text(
        json.dumps(
            {
                "route_labels": walkthrough_route_labels,
                "covered_route_labels": walkthrough_route_labels,
                "walkthrough_coverage_proof": coverage_proof,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (reconstruction_dir / "reconstruction.json").write_text(
        json.dumps(
            {
                "provider": "propertyquarry_generated_reconstruction",
                "schema": "propertyquarry.generated-reconstruction.v1",
                "verified_provider_capture": False,
                "satisfies_verified_tour_gate": False,
                "requested_style": selected_style,
                "style_scene": style_scene,
                "room_dimensions_m": {"width": 8.0, "depth": 5.5, "height": 2.8},
                "geometry": {"wall_rect_count": 8},
                "floorplan": {
                    "source_path": (
                        "property://ArchonMegalon/propertyquarry/e2e/"
                        "source-floorplan.png"
                    ),
                },
                "walkable_scene": walkable_scene,
                "route_labels": route_labels,
                "walkthrough_route_labels": walkthrough_route_labels,
                "viewer": {
                    "relpath": "viewer.html",
                    "version": reconstruction_styles.GENERATED_RECONSTRUCTION_VIEWER_VERSION,
                    "style_id": selected_style["id"],
                    "style_signature": selected_style["signature"],
                    "style_scene_signature": style_scene["scene_signature"],
                    "floorplan_display_mode": reconstruction_styles.FLOORPLAN_DISPLAY_MODE,
                    "sha256": hashlib.sha256(
                        viewer_document.encode("utf-8")
                    ).hexdigest(),
                },
                "walkthrough": {
                    "status": "generated",
                    "coverage_proof": coverage_proof,
                },
                "model": {
                    "glb_export": {
                        "status": "failed",
                        "reason": "not_required_for_browser_fixture",
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    asset_specs = [
        (
            "generated-reconstruction/viewer.html",
            "text/html",
            "viewer_document",
        ),
        (
            "generated-reconstruction/reconstruction.json",
            "application/json",
            "reconstruction_manifest",
        ),
        (
            "generated-reconstruction/source-floorplan.png",
            "image/png",
            "floorplan_texture",
        ),
        (
            "generated-reconstruction/vendor/three.module.js",
            "text/javascript",
            "viewer_module",
        ),
        (
            "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js",
            "text/javascript",
            "viewer_module",
        ),
        *[
            (
                f"generated-reconstruction/{filename}",
                "image/png",
                "photo_texture",
            )
            for filename, _label, _fill in photo_specs
        ],
    ]
    asset_bindings = []
    for relpath, mime_type, role in asset_specs:
        content = (bundle_dir / relpath).read_bytes()
        asset_bindings.append(
            {
                "path": relpath,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "mime_type": mime_type,
                "role": role,
            }
        )
    scenes = [
        {
            "scene_id": "floorplan-1",
            "name": "Route floorplan",
            "role": "floorplan",
            "asset_relpath": "generated-reconstruction/source-floorplan.png",
            "mime_type": "image/png",
        }
    ]
    scenes.extend(
        {
            "scene_id": f"photo-{index}",
            "name": label,
            "role": "photo",
            "asset_relpath": f"generated-reconstruction/{filename}",
            "mime_type": "image/png",
        }
        for index, (filename, label, _fill) in enumerate(photo_specs, start=1)
    )
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Generated reconstruction public launch",
                "display_title": "Generated reconstruction public launch",
                "hosted_url": f"https://propertyquarry.com/tours/{slug}",
                "public_url": f"https://propertyquarry.com/tours/{slug}",
                "diorama_preview_relpath": "diorama-preview.png",
                "preview_relpath": "diorama-preview.png",
                "photo_count": len(photo_specs),
                "media": {"source_photos": {"count": len(photo_specs)}},
                "public_assets": [
                    {
                        "path": "generated-reconstruction/vendor/three.module.js",
                        "privacy_class": "generated_reconstruction_public",
                        "role": "generated_reconstruction_viewer_asset",
                        "mime_type": "text/javascript",
                    },
                    {
                        "path": "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js",
                        "privacy_class": "generated_reconstruction_public",
                        "role": "generated_reconstruction_viewer_asset",
                        "mime_type": "text/javascript",
                    },
                ],
                "generated_reconstruction": {
                    "provider": "propertyquarry_generated_reconstruction",
                    **style_contract_fields,
                    "viewer_relpath": "generated-reconstruction/viewer.html",
                    "manifest_relpath": "generated-reconstruction/reconstruction.json",
                    "model_relpath": "generated-reconstruction/model.obj",
                    "material_relpath": "generated-reconstruction/model.mtl",
                    "glb_model_relpath": "",
                    "glb_export_status": "failed",
                    "floorplan_relpath": "generated-reconstruction/source-floorplan.png",
                    "photo_relpaths": [
                        "generated-reconstruction/photo-01.png",
                        "generated-reconstruction/photo-02.png",
                        "generated-reconstruction/photo-03.png",
                        "generated-reconstruction/photo-04.png",
                        "generated-reconstruction/photo-05.png",
                    ],
                    "photo_reference_panel_count": len(photo_specs),
                    "route_labels": route_labels,
                    "room_stop_count": len(route_labels),
                    "walkthrough_route_labels": walkthrough_route_labels,
                    "walkthrough_stop_count": len(walkthrough_route_labels),
                    "walkthrough_video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
                    "walkthrough_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
                    "walkthrough_coverage_proof": coverage_proof,
                    "walkable_scene": walkable_scene,
                    "capture_mode": False,
                    "synthetic": True,
                    "verified_provider_capture": False,
                    "satisfies_verified_tour_gate": False,
                    "disclosure": disclosure,
                },
                "generated_viewer_release": {
                    "contract": PUBLIC_TOUR_GENERATED_VIEWER_RELEASE_CONTRACT,
                    "status": "ready",
                    "provider": "propertyquarry_generated_reconstruction",
                    "viewer_relpath": "generated-reconstruction/viewer.html",
                    "asset_bindings": asset_bindings,
                    "browser_receipt_sha256": "1" * 64,
                    "source_provenance_receipt_sha256": "2" * 64,
                    "publication_authority_receipt_sha256": "3" * 64,
                    "security_review_receipt_sha256": "4" * 64,
                    "accessibility_review_receipt_sha256": "5" * 64,
                    "browser_interaction_verified": True,
                    "visual_quality_review_passed": True,
                    "security_review_passed": True,
                    "accessibility_review_passed": True,
                    "source_provenance_verified": True,
                    "publication_authority_verified": True,
                    "public_activation_authority": True,
                    "capture_mode": False,
                    "synthetic": True,
                    "verified_provider_capture": False,
                    "satisfies_verified_tour_gate": False,
                    "release_revision": "property-layout-e2e-style-proof-v4",
                    "disclosure": disclosure,
                    "revoked": False,
                    "disqualified": False,
                },
                "video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
                "video_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
                "video_provider": "propertyquarry_generated_reconstruction",
                "video_provider_key": "propertyquarry_generated_reconstruction",
                "video_coverage_proof": "boundary_verified_frame_continuation",
                "scenes": scenes,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return route_labels, walkthrough_route_labels


def test_propertyquarry_generated_reconstruction_public_launch_renders_honest_shell_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    bundle_root = Path(str(propertyquarry_browser_server["bundle_root"]))
    slug = "generated-reconstruction-public-e2e"
    route_labels, _ = _write_generated_reconstruction_public_launch_fixture(bundle_root, slug=slug)

    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    console_errors: list[str] = []
    failed_requests: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: console_errors.append(str(exc)))
    page.on("requestfailed", lambda request: failed_requests.append(f"{request.url} :: {request.failure}"))
    try:
        response = page.goto(f"{base_url}/tours/{slug}", wait_until="domcontentloaded")
        assert response is not None
        assert response.status == 200
        assert urllib.parse.urlparse(page.url).path.endswith(
            f"/tours/viewer/{slug}/generated-reconstruction/viewer.html"
        )
        viewer_shell = page.locator("[data-pq-reconstruction-viewer]")
        assert viewer_shell.count() == 1
        assert viewer_shell.get_attribute("data-pq-preview-kind") == "styled-3d-reconstruction"
        assert viewer_shell.get_attribute("data-pq-style-id") == "urban_jungle"
        assert (
            viewer_shell.get_attribute("data-pq-floorplan-display-mode")
            == reconstruction_styles.FLOORPLAN_DISPLAY_MODE
        )
        body_text = page.inner_text("body").lower()
        assert "generated reconstruction" in body_text
        assert "room route" in body_text
        assert "urban jungle" in body_text
        page.wait_for_function(
            "() => Boolean(window.__pqReconstructionDebug?.getRenderMetrics?.().ready)",
            timeout=10_000,
        )
        initial_metrics = page.evaluate(
            "() => window.__pqReconstructionDebug.getRenderMetrics()"
        )
        assert bool(initial_metrics.get("ready"))
        assert int(initial_metrics.get("frameCount") or 0) >= 2
        assert int(initial_metrics.get("renderCalls") or 0) > 0
        assert int(initial_metrics.get("renderTriangles") or 0) > 0
        target_index = len(route_labels) - 1
        target_position = f"Stop {target_index + 1} / {len(route_labels)}"
        target_label = route_labels[target_index]
        page.evaluate(
            "(index) => window.__pqReconstructionDebug.setRouteView(index)",
            target_index,
        )
        page.wait_for_function(
            """([expectedLabel, expectedPosition]) => {
                return String(document.getElementById('viewer-route-label')?.textContent || '').trim() === expectedLabel
                  && String(document.getElementById('viewer-route-position')?.textContent || '').trim() === expectedPosition
                  && String(document.getElementById('viewer-focus-readout')?.textContent || '').trim() === expectedLabel;
            }""",
            arg=[target_label, target_position],
            timeout=10_000,
        )
        preview_page = context.new_page()
        preview_page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        preview_page.on("pageerror", lambda exc: console_errors.append(str(exc)))
        preview_response = preview_page.goto(f"{base_url}/tours/{slug}/layout-preview", wait_until="domcontentloaded")
        assert preview_response is not None
        assert preview_response.status == 200
        assert urllib.parse.urlparse(preview_page.url).path.endswith(
            f"/tours/viewer/{slug}/generated-reconstruction/viewer.html"
        )
        assert (
            preview_page.locator("[data-pq-reconstruction-viewer]").get_attribute(
                "data-pq-style-id"
            )
            == "urban_jungle"
        )
        preview_page.wait_for_function(
            "() => Boolean(window.__pqReconstructionDebug?.getRenderMetrics?.().ready)",
            timeout=10_000,
        )
        unexpected_console_errors = [
            message
            for message in console_errors
            if "404 (not found)" not in message.lower() and "cross-origin-opener-policy" not in message.lower()
        ]
        assert not _unexpected_console_errors(unexpected_console_errors), unexpected_console_errors
        assert not failed_requests, failed_requests
    finally:
        context.close()


def test_propertyquarry_generated_reconstruction_public_launch_is_mobile_safe(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    bundle_root = Path(str(propertyquarry_browser_server["bundle_root"]))
    slug = "generated-reconstruction-public-mobile"
    route_labels, _ = _write_generated_reconstruction_public_launch_fixture(bundle_root, slug=slug)

    context = _new_context(browser, mobile=True, width=390, height=844)
    page: Page = context.new_page()
    console_errors: list[str] = []
    failed_requests: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("requestfailed", lambda request: failed_requests.append(f"{request.url} :: {request.failure}"))
    try:
        response = page.goto(f"{base_url}/tours/{slug}", wait_until="domcontentloaded")
        assert response is not None
        assert response.status == 200
        assert urllib.parse.urlparse(page.url).path.endswith(
            f"/tours/viewer/{slug}/generated-reconstruction/viewer.html"
        )
        body_text = page.inner_text("body").lower()
        assert "generated reconstruction" in body_text
        assert "room route" in body_text
        assert "urban jungle" in body_text
        viewer_shell = page.locator("[data-pq-reconstruction-viewer]")
        assert viewer_shell.get_attribute("data-pq-style-id") == "urban_jungle"
        page.wait_for_function(
            "() => Boolean(window.__pqReconstructionDebug?.getRenderMetrics?.().ready)",
            timeout=10_000,
        )
        page.evaluate(
            "(index) => window.__pqReconstructionDebug.setRouteView(index)",
            len(route_labels) - 1,
        )
        assert page.locator("#viewer-route-label").inner_text() == route_labels[-1]
        mobile_layout = page.evaluate(
            """() => {
                const body = document.body;
                const shell = document.querySelector('[data-pq-reconstruction-viewer]');
                const stage = document.querySelector('.viewer-stage');
                const shellBox = shell?.getBoundingClientRect();
                const stageBox = stage?.getBoundingClientRect();
                return {
                    viewportWidth: window.innerWidth,
                    scrollWidth: body.scrollWidth,
                    shellWidth: shellBox?.width || -1,
                    stageWidth: stageBox?.width || -1,
                };
            }"""
        )
        assert mobile_layout["scrollWidth"] <= mobile_layout["viewportWidth"] + 1, mobile_layout
        assert 0 < mobile_layout["shellWidth"] <= mobile_layout["viewportWidth"], mobile_layout
        assert 0 < mobile_layout["stageWidth"] <= mobile_layout["shellWidth"], mobile_layout
        unexpected_console_errors = [
            message
            for message in console_errors
            if "404 (not found)" not in message.lower() and "cross-origin-opener-policy" not in message.lower()
        ]
        assert not _unexpected_console_errors(unexpected_console_errors), unexpected_console_errors
        assert not failed_requests, failed_requests
    finally:
        context.close()


def test_propertyquarry_walkthrough_request_is_user_initiated_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls = 0
    console_errors: list[str] = []

    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    def _capture_visual_request(route) -> None:
        request = route.request
        payload = request.post_data_json
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-06-19T10:00:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Family flat near Tiergarten",
                    "request_kind": "flythrough",
                    "tour_url": "",
                    "flythrough_url": "",
                    "flythrough_status": "pending",
                    "status_label": "Walkthrough queued",
                    "status_detail": "Walkthrough is queued after your request and will appear here when it is ready.",
                    "eta_label": "about 10 min",
                    "progress_pct": 18,
                    "poll_after_seconds": 1,
                    "delivery_status": "skipped",
                    "blocked_reason": "",
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    def _capture_visual_status(route) -> None:
        nonlocal visual_status_polls
        visual_status_polls += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-02T10:00:03+00:00",
                    "status": "ready",
                    "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                    "title": "Family flat near Tiergarten",
                    "request_kind": "flythrough",
                    "tour_url": "",
                    "tour_status": "pending",
                    "flythrough_url": f"{base_url}/tours/altbau-u6?pane=flythrough-pane&autoplay=1",
                    "flythrough_status": "ready",
                    "status_label": "Open walkthrough",
                    "status_detail": "Walkthrough is ready.",
                    "eta_label": "",
                    "progress_pct": 100,
                    "poll_after_seconds": 0,
                    "source_ref": "immobilienscout24:family-tiergarten",
                    "run_id": "run-42",
                    "candidate_ref": "family-tiergarten",
                }
            ),
        )

    page.route("**/app/api/signals/property/visual-status?**", _capture_visual_status)
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        page.locator("[data-workbench-row]").first.wait_for(timeout=5000)
        assert visual_requests == []

        family_row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        packet_href = str(family_row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"
        response = page.goto(packet_url, wait_until="domcontentloaded")
        assert response is not None and response.ok
        request_button = page.get_by_role("button", name="Request walkthrough")
        expect(request_button).to_be_visible()
        request_button.click()
        _choose_research_visual_style(page, accept_external_processing=True)
        page.wait_for_timeout(750)
        assert visual_requests, page.locator("body").inner_text()[:1000]
        assert "urban jungle" in str(visual_requests[-1].get("diorama_style_hint") or "").lower()
        body_after_request = page.locator("body").inner_text()
        assert (
            "Walkthrough queued" in body_after_request
            or "Preparing walkthrough" in body_after_request
        ), body_after_request[:2000]

        assert len(visual_requests) == 1
        payload = visual_requests[0]
        assert payload["request_kind"] == "flythrough"
        assert payload["auto_deliver"] is False
        assert payload["allow_floorplan_only"] is True
        assert payload["external_processing_consent_granted"] is True
        current_visual_status = page.locator("[data-prd-visual-status]").inner_text().strip().lower()
        assert (
            "queued after your request" in current_visual_status
            or "is ready." in current_visual_status
        ), current_visual_status
        rail_fill = page.locator("[data-prd-visual-progress]").first.get_attribute("style") or ""
        assert "18%" in rail_fill or "100%" in rail_fill
        page.wait_for_timeout(1300)
        assert visual_status_polls >= 1
        expect(page.locator("[data-prd-visual-status]")).to_contain_text("Walkthrough is ready.")
        updated_button = page.get_by_role("button", name=re.compile("Open walkthrough", re.I)).first
        expect(updated_button).to_be_visible()
        updated_href = str(updated_button.get_attribute("data-pw-visual-href") or "").strip()
        assert updated_href
        assert updated_href.endswith("/tours/altbau-u6?pane=flythrough-pane&autoplay=1")
        with page.expect_navigation(wait_until="domcontentloaded"):
            updated_button.click()
        video = page.locator("#flythrough-video, #tour-video").first
        video.wait_for()
        assert video.is_visible()
        page.evaluate(
            """() => {
                const video = document.getElementById('flythrough-video') || document.getElementById('tour-video');
                return video?.play?.()?.catch(() => null) || null;
            }"""
        )
        page.wait_for_timeout(1800)
        state = page.evaluate(
            """() => {
                const video = document.getElementById('flythrough-video') || document.getElementById('tour-video');
                return video ? {
                    currentTime: video.currentTime,
                    duration: video.duration,
                    readyState: video.readyState,
                    videoWidth: video.videoWidth,
                } : null;
            }"""
        )
        assert state is not None
        assert state["readyState"] >= 2
        assert state["videoWidth"] >= 640
        assert state["currentTime"] > 0.2
        assert state["duration"] >= 2.5
        assert _video_frame_brightness(page) > 10.0
        noisy_console_errors = [
            message
            for message in console_errors
            if "decode" in message.lower()
            or "media" in message.lower()
            or "refused" in message.lower()
            or "failed to load resource" in message.lower()
        ]
        assert not noisy_console_errors, noisy_console_errors
        assert payload["run_id"] == "run-42"
        assert str(payload["property_url"]).endswith("/family-tiergarten")
    finally:
        context.close()


def test_propertyquarry_ready_tour_rail_stays_on_tour_while_walkthrough_queue_is_open(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls: dict[str, int] = {"tour": 0, "flythrough": 0}

    def _capture_visual_request(route) -> None:
        payload = route.request.post_data_json or {}
        visual_requests.append(payload if isinstance(payload, dict) else {})
        request_kind = str(visual_requests[-1].get("request_kind") or "").strip().lower()
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                (
                    {
                        "generated_at": "2026-07-03T10:00:00+00:00",
                        "status": "created",
                        "property_url": visual_requests[-1].get("property_url", ""),
                        "title": "Family flat near Tiergarten",
                        "request_kind": "tour",
                        "tour_url": "",
                        "tour_status": "pending",
                        "flythrough_url": "",
                        "flythrough_status": "",
                        "status_label": "3D tour queued",
                        "status_detail": "3D tour is queued after your request.",
                        "eta_label": "about 10 min",
                        "progress_pct": 16,
                        "poll_after_seconds": 1,
                        "source_ref": visual_requests[-1].get("source_ref", ""),
                        "run_id": visual_requests[-1].get("run_id", ""),
                        "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                    }
                    if request_kind == "tour"
                    else {
                        "generated_at": "2026-07-03T10:00:00+00:00",
                        "status": "created",
                        "property_url": visual_requests[-1].get("property_url", ""),
                        "title": "Family flat near Tiergarten",
                        "request_kind": "flythrough",
                        "tour_url": f"{base_url}/tours/altbau-u6/control/3dvista",
                        "tour_status": "ready",
                        "flythrough_url": "",
                        "flythrough_status": "queued",
                        "status_label": "Walkthrough queued",
                        "status_detail": "Priority queue active. Queued.",
                        "eta_label": "about 10 min",
                        "progress_pct": 18,
                        "poll_after_seconds": 1,
                        "source_ref": visual_requests[-1].get("source_ref", ""),
                        "run_id": visual_requests[-1].get("run_id", ""),
                        "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                    }
                )
            ),
        )

    def _capture_visual_status(route) -> None:
        query = urllib.parse.urlparse(route.request.url).query
        request_kind = urllib.parse.parse_qs(query).get("request_kind", ["tour"])[0].strip().lower() or "tour"
        visual_status_polls[request_kind] = visual_status_polls.get(request_kind, 0) + 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                (
                    {
                        "generated_at": "2026-07-03T10:00:03+00:00",
                        "status": "ready",
                        "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                        "title": "Family flat near Tiergarten",
                        "request_kind": "tour",
                        "tour_url": f"{base_url}/tours/altbau-u6/control/3dvista",
                        "tour_status": "ready",
                        "flythrough_url": "",
                        "flythrough_status": "",
                        "status_label": "Open 3D tour",
                        "status_detail": "3D tour is ready.",
                        "eta_label": "",
                        "progress_pct": 100,
                        "poll_after_seconds": 0,
                        "source_ref": "immobilienscout24:family-tiergarten",
                        "run_id": "run-42",
                        "candidate_ref": "family-tiergarten",
                    }
                    if request_kind == "tour"
                    else {
                        "generated_at": "2026-07-03T10:00:03+00:00",
                        "status": "queued",
                        "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                        "title": "Family flat near Tiergarten",
                        "request_kind": "flythrough",
                        "tour_url": f"{base_url}/tours/altbau-u6/control/3dvista",
                        "tour_status": "ready",
                        "flythrough_url": "",
                        "flythrough_status": "queued",
                        "status_label": "Walkthrough queued",
                        "status_detail": "Priority queue active. Queued.",
                        "eta_label": "about 10 min",
                        "progress_pct": 18,
                        "poll_after_seconds": 1,
                        "source_ref": "immobilienscout24:family-tiergarten",
                        "run_id": "run-42",
                        "candidate_ref": "family-tiergarten",
                    }
                )
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    page.route("**/app/api/signals/property/visual-status?**", _capture_visual_status)
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok

        family_row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        expect(family_row).to_be_visible()
        packet_href = str(family_row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"

        response = page.goto(packet_url, wait_until="domcontentloaded")
        assert response is not None and response.ok
        tour_button = page.get_by_role("button", name="Request 3D tour").first
        expect(tour_button).to_be_visible()
        tour_button.click()
        _choose_research_visual_style(page)
        expect(page.locator("[data-prd-visual-status]")).to_contain_text("3D tour is ready.", timeout=10000)
        expect(page.get_by_role("button", name="Open 3D tour").first).to_be_visible()

        request_button = page.get_by_role("button", name="Request walkthrough").first
        expect(request_button).to_be_visible()

        request_button.click()
        _choose_research_visual_style(page, accept_external_processing=True)
        page.wait_for_timeout(300)
        assert len(visual_requests) == 2
        assert visual_requests[0]["request_kind"] == "tour"
        assert visual_requests[1]["request_kind"] == "flythrough"
        assert visual_requests[1]["external_processing_consent_granted"] is True
        assert "urban jungle" in str(visual_requests[1].get("diorama_style_hint") or "").lower()

        expect(page.locator("[data-prd-visual-label]")).to_contain_text(re.compile("3D tour", re.I), timeout=5000)
        expect(page.locator("[data-prd-visual-status]")).to_contain_text("3D tour is ready.", timeout=5000)
        page.wait_for_timeout(1300)
        assert visual_status_polls["tour"] >= 1
        assert visual_status_polls["flythrough"] >= 1
        expect(page.locator("[data-prd-visual-label]")).to_contain_text(re.compile("3D tour", re.I), timeout=5000)
        expect(page.locator("[data-prd-visual-status]")).to_contain_text("3D tour is ready.", timeout=5000)
        expect(page.get_by_role("button", name="Open 3D tour").first).to_be_visible()
        expect(page.get_by_role("button", name=re.compile("Walkthrough queued", re.I)).first).to_be_visible()
        rail_kind = str(page.locator("[data-prd-visual-rail]").first.get_attribute("data-prd-visual-kind") or "").strip().lower()
        assert rail_kind == "tour"
    finally:
        context.close()


def test_propertyquarry_results_surface_keeps_desktop_selection_inline_before_opening_research(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/properties?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        expect(row).to_be_visible(timeout=5000)
        before_url = page.url
        selected_candidate_ref = str(row.get_attribute("data-candidate-ref") or "").strip()
        assert selected_candidate_ref
        row.click()
        selected_panel = page.get_by_role("region", name="Selected property")
        expect(selected_panel.locator("[data-pw-title]")).to_contain_text("Family flat near Tiergarten")
        before_parts = urllib.parse.urlsplit(before_url)
        selected_parts = urllib.parse.urlsplit(page.url)
        assert (selected_parts.scheme, selected_parts.netloc, selected_parts.path) == (
            before_parts.scheme,
            before_parts.netloc,
            before_parts.path,
        )
        assert urllib.parse.parse_qs(selected_parts.query).get("candidate") == [selected_candidate_ref]
        expect(selected_panel.get_by_role("link", name="Open property").first).to_have_attribute(
            "href",
            re.compile(r"/app/research/"),
        )
    finally:
        context.close()


def test_propertyquarry_results_surface_promotes_ready_generated_layout_tour_inline_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_get_run_status = ProductService.get_property_search_run_status

    def _inline_visual_request_ready_run_status(self, *, principal_id: str, run_id: str):
        payload = original_get_run_status(self, principal_id=principal_id, run_id=run_id)
        ranked_candidates = list(((payload or {}).get("summary") or {}).get("ranked_candidates") or [])
        for candidate in ranked_candidates:
            if str(candidate.get("property_url") or "").endswith("/family-tiergarten"):
                candidate["floorplan_url"] = "https://cdn.example.test/floorplan-family-tiergarten.png"
        for source in list(((payload or {}).get("summary") or {}).get("sources") or []):
            for candidate in list((source or {}).get("top_candidates") or []):
                if str(candidate.get("property_url") or "").endswith("/family-tiergarten"):
                    candidate["floorplan_url"] = "https://cdn.example.test/floorplan-family-tiergarten.png"
        return payload

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _inline_visual_request_ready_run_status)

    base_url = str(propertyquarry_browser_server["base_url"])
    bundle_root = Path(str(propertyquarry_browser_server["bundle_root"]))
    generated_layout_slug = "generated-layout-tiergarten-inline"
    generated_layout_url = f"{base_url}/tours/{generated_layout_slug}"
    _write_generated_reconstruction_public_launch_fixture(bundle_root, slug=generated_layout_slug)
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls = 0

    def _capture_visual_request(route) -> None:
        payload = route.request.post_data_json or {}
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-10T10:00:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_url": "",
                    "tour_status": "pending",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "3D tour queued",
                    "status_detail": "3D tour is queued after your request.",
                    "eta_label": "about 10 min",
                    "progress_pct": 16,
                    "poll_after_seconds": 1,
                    "delivery_status": "queued",
                    "blocked_reason": "",
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    def _capture_visual_status(route) -> None:
        nonlocal visual_status_polls
        visual_status_polls += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-10T10:00:03+00:00",
                    "status": "ready",
                    "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                    "title": "Family flat near Tiergarten",
                    "request_kind": "tour",
                    "tour_media_mode": "generated_reconstruction",
                    "provider_key": "generated_reconstruction",
                    "tour_url": generated_layout_url,
                    "open_tour_url": "",
                    "generated_reconstruction_url": generated_layout_url,
                    "tour_status": "ready",
                    "flythrough_url": "",
                    "flythrough_status": "",
                    "status_label": "Open AI-generated 3D tour",
                    "status_detail": (
                        "AI-generated from the floor plan and listing photos. "
                        "It is an interactive spatial aid, not a captured 360° tour."
                    ),
                    "eta_label": "",
                    "progress_pct": 100,
                    "poll_after_seconds": 0,
                    "source_ref": "immobilienscout24:family-tiergarten",
                    "run_id": "run-42",
                    "candidate_ref": "family-tiergarten",
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    page.route("**/app/api/signals/property/visual-status?**", _capture_visual_status)
    try:
        response = page.goto(f"{base_url}/app/properties?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok

        row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        expect(row).to_be_visible(timeout=5000)
        before_url = page.url
        row.click()

        selected_panel = page.get_by_role("region", name="Selected property")
        expect(selected_panel.locator("[data-pw-title]")).to_contain_text("Family flat near Tiergarten")
        assert page.url.startswith(before_url)
        assert "candidate=" in page.url

        selected_panel.locator("[data-pw-actions] details summary").first.click()
        request_button = selected_panel.get_by_role("button", name="Request 3D tour").first
        expect(request_button).to_be_visible()
        request_button.click()
        _choose_workbench_visual_style(page)

        status_node = selected_panel.locator("[data-pw-visual-request-status]").first
        expect(status_node).to_contain_text(
            (
                "AI-generated from the floor plan and listing photos. "
                "It is an interactive spatial aid, not a captured 360° tour."
            ),
            timeout=5000,
        )
        assert len(visual_requests) == 1
        payload = visual_requests[0]
        assert payload["request_kind"] == "tour"
        assert payload["auto_deliver"] is False
        assert payload["allow_floorplan_only"] is True
        assert payload["run_id"] == "run-42"
        assert str(payload["property_url"]).endswith("/family-tiergarten")
        assert "urban jungle" in str(payload["diorama_style_hint"] or "").lower()
        assert visual_status_polls >= 1
        selected_panel.locator("[data-pw-actions] details summary").first.click()
        ready_button = selected_panel.get_by_role("button", name="Open AI-generated 3D tour").first
        expect(ready_button).to_be_visible()
        assert str(ready_button.get_attribute("data-pw-visual-ready-url") or "").strip() == generated_layout_url

        with page.expect_navigation(wait_until="domcontentloaded"):
            ready_button.click()
        _assert_generated_reconstruction_v4_viewer(
            page,
            slug=generated_layout_slug,
            style_id="urban_jungle",
        )
        public_text = page.locator("body").inner_text().lower()
        assert "generated reconstruction" in public_text
        assert "room route" in public_text
        assert "urban jungle" in public_text
        assert "tour unavailable" not in public_text
    finally:
        context.close()


def test_propertyquarry_visual_request_uses_listing_url_fallback_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    visual_requests: list[dict[str, object]] = []

    def _capture_visual_request(route) -> None:
        request = route.request
        payload = request.post_data_json
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-06-19T10:00:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Listing URL only loft",
                    "request_kind": "flythrough",
                    "tour_url": "",
                    "flythrough_url": "",
                    "flythrough_status": "pending",
                    "status_label": "Walkthrough queued",
                    "status_detail": "Walkthrough is queued after your request.",
                    "delivery_status": "skipped",
                    "blocked_reason": "",
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="networkidle")
        assert response is not None and response.ok
        listing_only_row = page.locator("[data-workbench-row]", has_text="Listing URL only loft").first
        listing_only_row.wait_for(timeout=5000)
        assert visual_requests == []

        packet_href = str(listing_only_row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_href
        packet_url = packet_href if packet_href.startswith("http") else f"{base_url}{packet_href}"
        response = page.goto(packet_url, wait_until="networkidle")
        assert response is not None and response.ok
        request_button = page.get_by_role("button", name="Request walkthrough")
        expect(request_button).to_be_visible()
        assert str(request_button.get_attribute("data-property-url") or "").endswith("/listing-url-only-loft")
        request_button.click()
        _choose_research_visual_style(page, accept_external_processing=True)
        page.wait_for_timeout(750)

        assert len(visual_requests) == 1
        payload = visual_requests[0]
        assert payload["request_kind"] == "flythrough"
        assert payload["auto_deliver"] is False
        assert payload["allow_floorplan_only"] is True
        assert payload["external_processing_consent_granted"] is True
        assert "urban jungle" in str(payload["diorama_style_hint"]).lower()
        assert payload["run_id"] == "run-42"
        assert str(payload["property_url"]).endswith("/listing-url-only-loft")
        body_after_request = page.locator("body").inner_text()
        assert "Walkthrough queued" in body_after_request or "Preparing walkthrough" in body_after_request
    finally:
        context.close()


def test_propertyquarry_austria_region_selection_keeps_region_specific_area_choices(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/properties?autolocate=1", wait_until="networkidle")
        assert response is not None and response.ok

        page.select_option('select[name="country_code"]', "AT")
        page.select_option('select[name="region_code"]', "salzburg")
        page.wait_for_timeout(250)
        assert page.locator('select[name="region_code"]').input_value() == "salzburg"
        page.locator('[data-property-field-name="location_query"]').wait_for(state="visible")
        assert page.locator('label.pqx-check', has_text='Hallein').count() >= 1

        page.select_option('select[name="region_code"]', "lower_austria")
        page.wait_for_timeout(250)
        assert page.locator('select[name="region_code"]').input_value() == "lower_austria"
        page.locator('[data-property-field-name="location_query"]').wait_for(state="visible")
        assert page.locator('label.pqx-check', has_text='Baden').count() >= 1
    finally:
        context.close()


def test_propertyquarry_packet_dashboard_supports_real_browser_share_and_replication_actions(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    publication = client.post(
        "/app/api/properties/listing-browser-packet/packets/render",
        json={
            "packet_kind": "family_review",
            "privacy_mode": "family_review",
            "fliplink_format": "flipbook_3d",
            "property_payload": {
                "title": "Family flat near Augarten",
                "property_url": "https://www.willhaben.at/iad/immobilien/d/demo",
                "match_reasons": ["Floorplan and family fit."],
                "floorplan_refs": ["https://packets.propertyquarry.com/assets/floorplan.pdf"],
                "photo_refs": ["https://packets.propertyquarry.com/assets/photo.jpg"],
                "property_facts": {
                    "rooms": 3,
                    "area_m2": 84,
                    "street_address": "Private Street 4",
                    "map_lat": 48.2,
                    "map_lng": 16.3,
                    "has_floorplan": True,
                    "postal_name": "1020 Wien",
                },
                "public_preference_snapshot": {"prefer_balcony": True},
            },
        },
    )
    assert publication.status_code == 200, publication.text
    publication_id = publication.json()["publication"]["publication_id"]
    feedback_one = client.post(
        "/app/api/property-feedback",
        json={
            "stakeholder_id": "family-mara",
            "stakeholder_label": "Mara",
            "property_ref": "listing-browser-packet",
            "publication_id": publication_id,
            "category": "question",
            "sentiment": "neutral",
            "importance": 4,
            "text": "Can the agent confirm the operating costs?",
            "followup_status": "asked",
        },
    )
    assert feedback_one.status_code == 200, feedback_one.text
    feedback_two = client.post(
        "/app/api/property-feedback",
        json={
            "stakeholder_id": "family-jonas",
            "stakeholder_label": "Jonas",
            "property_ref": "listing-browser-packet",
            "publication_id": publication_id,
            "category": "dealbreaker",
            "sentiment": "negative",
            "importance": 5,
            "text": "Street noise is a blocker.",
            "decision_state": "rejected",
        },
    )
    assert feedback_two.status_code == 200, feedback_two.text
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/properties/packets", wait_until="networkidle")
        assert response is not None and response.ok
        assert page.locator("[data-property-packets-dashboard]").is_visible()
        assert page.locator("body", has_text="Share property pages and keep the replies together.").is_visible()
        assert page.locator("body", has_text="Reactions").is_visible()
        assert page.locator("body", has_text="Notes").is_visible()
        assert page.locator("body", has_text="Can the agent confirm the operating costs?").is_visible()
        page.locator('[data-packet-share-form] input[name="recipient_name"]').first.fill("Anna")
        page.locator('[data-packet-share-form] input[name="recipient_email"]').first.fill("anna@example.com")
        page.locator('[data-packet-share-form] input[name="relationship"]').first.fill("Sister")
        with page.expect_response("**/app/api/properties/packets/*/shares") as share_response_info:
            page.locator('[data-packet-share-form] button[type="submit"]').first.click()
        share_response = share_response_info.value
        assert share_response.ok, share_response.text()

        page.locator('[data-fliplink-analytics-form] input[name="views"]').first.fill("8")
        page.locator('[data-fliplink-analytics-form] input[name="unique_visitors"]').first.fill("2")
        page.locator('[data-fliplink-analytics-form] input[name="average_time_seconds"]').first.fill("55")
        with page.expect_response("**/app/api/properties/packets/*/fliplink/analytics-snapshot") as analytics_response_info:
            page.locator('[data-fliplink-analytics-form] button[type="submit"]').first.click()
        analytics_response = analytics_response_info.value
        assert analytics_response.ok, analytics_response.text()

        with page.expect_response("**/app/api/properties/packets/*/republish") as republish_response_info:
            page.locator(f'[data-republish-publication][data-publication-id="{publication_id}"]').click()
        republish_response = republish_response_info.value
        assert republish_response.ok, republish_response.text()

        page.reload(wait_until="networkidle")
        assert page.locator("body", has_text="Anna · family · link").is_visible()
        assert page.locator("[data-analytics-summary]", has_text="8 views").is_visible()
        assert page.locator("[data-analytics-summary]", has_text="2 visitors").is_visible()
        assert page.locator("body", has_text="Page ideas").is_visible()
        assert page.locator("body", has_text="What changed").is_visible()
        assert page.locator("body", has_text="Street noise is a blocker.").is_visible()

        preview_expectations = {
            "search_results_ready": ("PropertyQuarry found 2 strong matches", "Open shortlist"),
            "property_match": ("Property match: Altbau near U6", "No — tell us why"),
            "tour_ready": ("Apartment tour ready: Family flat near Augarten", "No — tell us why"),
            "investment_research_ready": ("Investment research ready", "Pass — too risky"),
            "workspace_invitation": ("Mara invited you to PropertyQuarry", "Open invite"),
            "workspace_access": ("Your access link for PropertyQuarry account", "Open access link"),
            "google_connect": ("Connect Google to PropertyQuarry account", "Connect Google"),
            "market_ready": ("PropertyQuarry market ready: Vienna", "Open PropertyQuarry"),
        }
        for template_key, (subject_text, cta_text) in preview_expectations.items():
            response = page.goto(f"{base_url}/app/properties/notifications/preview?template={template_key}", wait_until="networkidle")
            assert response is not None and response.ok
            assert page.locator("body", has_text="Email preview").is_visible()
            assert page.locator("body", has_text=subject_text).is_visible()
            assert page.frame_locator("iframe").locator("body", has_text=cta_text).is_visible()
    finally:
        context.close()


def test_propertyquarry_workbench_candidate_history_stays_in_place(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    route = f"{base_url}/app/shortlist?run_id=run-42&full=1"

    desktop_context = _new_context(browser, mobile=False)
    desktop_page: Page = desktop_context.new_page()
    try:
        response = desktop_page.goto(route, wait_until="domcontentloaded")
        assert response is not None and response.ok
        expect(desktop_page.locator("[data-property-decision-workbench]")).to_be_visible()
        initial_path = urllib.parse.urlparse(desktop_page.url).path
        initial_navigation_count = desktop_page.evaluate("performance.getEntriesByType('navigation').length")
        assert initial_path == "/app/shortlist"
        assert initial_navigation_count == 1

        family_row = desktop_page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        loft_row = desktop_page.locator("[data-workbench-row]", has_text="Listing URL only loft").first
        family_ref = family_row.get_attribute("data-candidate-ref")
        loft_ref = loft_row.get_attribute("data-candidate-ref")
        assert family_ref
        assert loft_ref

        family_review = family_row.get_by_role(
            "button",
            name="Review Family flat near Tiergarten",
        )
        expect(family_review).to_be_visible()
        family_review.focus()
        expect(family_review).to_be_focused()
        family_review.press("Enter")
        expect(desktop_page.locator("[data-pw-title]").first).to_have_text("Family flat near Tiergarten")
        expect(family_row).to_have_attribute("aria-current", "true")
        expect(loft_row).to_have_attribute("aria-current", "false")
        assert urllib.parse.urlparse(desktop_page.url).path == initial_path
        assert urllib.parse.parse_qs(urllib.parse.urlparse(desktop_page.url).query)["candidate"] == [family_ref]
        assert desktop_page.evaluate("performance.getEntriesByType('navigation').length") == initial_navigation_count

        loft_review = loft_row.get_by_role(
            "button",
            name="Review Listing URL only loft",
        )
        loft_review.focus()
        expect(loft_review).to_be_focused()
        loft_review.press("Space")
        expect(desktop_page.locator("[data-pw-title]").first).to_have_text("Listing URL only loft")
        expect(family_row).to_have_attribute("aria-current", "false")
        expect(loft_row).to_have_attribute("aria-current", "true")
        assert urllib.parse.urlparse(desktop_page.url).path == initial_path
        assert urllib.parse.parse_qs(urllib.parse.urlparse(desktop_page.url).query)["candidate"] == [loft_ref]
        assert desktop_page.evaluate("performance.getEntriesByType('navigation').length") == initial_navigation_count

        desktop_page.go_back()
        expect(desktop_page.locator("[data-pw-title]").first).to_have_text("Family flat near Tiergarten")
        assert urllib.parse.parse_qs(urllib.parse.urlparse(desktop_page.url).query)["candidate"] == [family_ref]
        desktop_page.go_forward()
        expect(desktop_page.locator("[data-pw-title]").first).to_have_text("Listing URL only loft")
        assert urllib.parse.parse_qs(urllib.parse.urlparse(desktop_page.url).query)["candidate"] == [loft_ref]
        assert desktop_page.evaluate("performance.getEntriesByType('navigation').length") == initial_navigation_count
        desktop_page.reload(wait_until="domcontentloaded")
        expect(desktop_page.locator("[data-pw-title]").first).to_have_text("Listing URL only loft")
        assert urllib.parse.parse_qs(urllib.parse.urlparse(desktop_page.url).query)["candidate"] == [loft_ref]
    finally:
        desktop_context.close()

    mobile_context = _new_context(browser, mobile=True)
    mobile_page: Page = mobile_context.new_page()
    try:
        response = mobile_page.goto(route, wait_until="domcontentloaded")
        assert response is not None and response.ok
        family_row = mobile_page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        family_ref = family_row.get_attribute("data-candidate-ref")
        assert family_ref
        family_row.get_by_role(
            "button",
            name="Review Family flat near Tiergarten",
        ).click()
        expect(mobile_page.locator("[data-pw-title]").first).to_have_text("Family flat near Tiergarten")
        assert urllib.parse.urlparse(mobile_page.url).path == "/app/shortlist"
        assert urllib.parse.parse_qs(urllib.parse.urlparse(mobile_page.url).query)["candidate"] == [family_ref]
        assert mobile_page.evaluate("performance.getEntriesByType('navigation').length") == 1
    finally:
        mobile_context.close()


def test_propertyquarry_flagship_operating_loop_in_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    property_ref = "https://www.immobilienscout24.de/expose/altbau-u6"
    seeded = client.post(
        "/app/api/property-feedback",
        json={
            "stakeholder_id": "advisor-anna",
            "stakeholder_label": "Anna",
            "property_ref": property_ref,
            "category": "concern",
            "sentiment": "negative",
            "importance": 4,
            "text": "Operating costs still need proof before a viewing.",
            "source": "advisor_packet_review",
            "decision_state": "documents_requested",
        },
    )
    assert seeded.status_code == 200, seeded.text

    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False)
    page: Page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/shortlist?run_id=run-42&full=1", wait_until="domcontentloaded")
        assert response is not None and response.ok
        expect(page.locator("[data-property-decision-workbench]")).to_be_visible()
        expect(page.locator("[data-workbench-row]").first).to_be_visible()
        expect(page.locator(".pqx-results-summary-link").first).to_be_visible()
        candidate_ref = page.locator("[data-workbench-row]", has_text="Altbau near U6").first.get_attribute("data-candidate-ref")
        packet_path = page.locator("[data-workbench-row]", has_text="Altbau near U6").first.get_attribute("data-candidate-packet-url")
        assert candidate_ref
        assert packet_path
        response = page.goto(
            f"{base_url}{packet_path}?run_id=run-42" if "?" not in packet_path else f"{base_url}{packet_path}",
            wait_until="domcontentloaded",
        )
        assert response is not None and response.ok
        with page.expect_response("**/preference-profile/property-feedback") as save_response_info:
            page.get_by_role("button", name="No", exact=True).click()
            page.locator("[data-object-feedback-save]").click()
        assert save_response_info.value.ok
        assert page.locator("body", has_text="Question 1").is_visible()
        packet_render = client.post(
            f"/app/api/properties/{urllib.parse.quote(property_ref, safe='')}/packets/render",
            json={
                "packet_kind": "family_review",
                "privacy_mode": "family_review",
                "fliplink_format": "flipbook_3d",
                "property_payload": {
                    "title": "Altbau near U6",
                    "property_url": property_ref,
                    "match_reasons": ["Lift and transit fit."],
                    "photo_refs": ["https://packets.propertyquarry.com/assets/photo.jpg"],
                    "property_facts": {
                        "rooms": 3,
                        "area_m2": 78,
                        "street_address": "Private Street 4",
                        "map_lat": 48.2,
                        "map_lng": 16.3,
                        "has_floorplan": True,
                        "postal_name": "1020 Wien",
                    },
                },
            },
        )
        assert packet_render.status_code == 200, packet_render.text
        response = page.goto(f"{base_url}/app/properties/packets", wait_until="domcontentloaded")
        assert response is not None and response.ok
        assert page.locator("body", has_text="Reactions").is_visible()
        assert page.locator("body", has_text="What changed").is_visible()
        share_form = page.locator('[data-packet-share-form]').first
        share_form.locator('input[name="recipient_name"]').fill("Anna")
        share_form.locator('input[name="recipient_email"]').fill("anna@example.com")
        share_form.locator('input[name="relationship"]').fill("Advisor")
        share_form.locator('select[name="audience_type"]').select_option("advisor")
        with page.expect_response("**/app/api/properties/packets/*/shares") as share_response_info:
            share_form.locator('button[type="submit"]').click()
        assert share_response_info.value.ok
        response = page.goto(
            f"{base_url}/app/properties/notifications/preview?template=property_match",
            wait_until="domcontentloaded",
        )
        assert response is not None and response.ok
        no_link = page.frame_locator("iframe").get_by_role("link", name="No — tell us why")
        href = no_link.get_attribute("href")
        assert href and "decision=no" in href and "clippy=1" in href
        packet_url = f"{base_url}{packet_path}"
        separator = "&" if "?" in packet_url else "?"
        response = page.goto(
            f"{packet_url}{separator}run_id=run-42&decision=no&clippy=1&prompt=What%20is%20the%20strongest%20blocker%20here%3F",
            wait_until="domcontentloaded",
        )
        assert response is not None and response.ok
        expect(page.locator("[data-property-research-detail]")).to_be_visible()
        assert page.locator("body", has_text="Decision shortcut loaded from the email or shared link.").is_visible()
        assert page.locator("body", has_text="Question loaded from the email or shared link.").is_visible()
        assert page.locator("body", has_text="Follow-up").is_visible()
    finally:
        context.close()


def test_propertyquarry_mobile_shortlist_prerenders_first_cards_and_preserves_diorama_artwork(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page: Page = context.new_page()
    try:
        response = page.goto(
            f"{base_url}/app/shortlist?run_id=run-42&full=1",
            wait_until="networkidle",
        )
        assert response is not None and response.ok
        cards = page.locator("[data-workbench-row]")
        expect(cards.nth(1)).to_contain_text("Family flat near Tiergarten")
        first_cards = cards.evaluate_all(
            """(nodes) => nodes.slice(0, 8).map((node) => ({
              contentVisibility: getComputedStyle(node).contentVisibility,
              text: String(node.textContent || '').trim(),
              surface: node.closest('[data-pqx-surface]')?.getAttribute('data-pqx-surface') || '',
              childIndex: Array.from(node.parentElement?.children || []).indexOf(node) + 1,
            }))"""
        )
        assert len(first_cards) >= 2
        assert all(item["contentVisibility"] == "visible" for item in first_cards), first_cards
        assert all(item["text"] for item in first_cards)

        diorama = cards.first.locator(".pqx-result-diorama:not(.is-placeholder)").first
        expect(diorama).to_be_visible()
        media_fit = diorama.evaluate(
            """(node) => {
              const image = node.querySelector('img');
              const style = getComputedStyle(node);
              const imageStyle = image ? getComputedStyle(image) : null;
              return {
                aspectRatio: style.aspectRatio,
                imageObjectFit: imageStyle?.objectFit || '',
                imageClientWidth: image?.clientWidth || 0,
                imageScrollWidth: image?.scrollWidth || 0,
                imageClientHeight: image?.clientHeight || 0,
                imageScrollHeight: image?.scrollHeight || 0,
              };
            }"""
        )
        assert media_fit["aspectRatio"] == "16 / 10"
        assert media_fit["imageObjectFit"] == "contain"
        assert media_fit["imageScrollWidth"] <= media_fit["imageClientWidth"] + 1
        assert media_fit["imageScrollHeight"] <= media_fit["imageClientHeight"] + 1
        _assert_no_horizontal_overflow(page)
    finally:
        context.close()


_PROPERTYQUARRY_VISUAL_CASES: tuple[tuple[str, tuple[int, int]], ...] = (
    ("public-home.desktop", (1440, 820)),
    ("public-home.mobile", (430, 932)),
    ("sign-in.desktop", (1440, 820)),
    ("sign-in.mobile", (430, 932)),
    ("search-setup.desktop", (1600, 1200)),
    ("search-setup.mobile", (430, 932)),
    ("results.desktop", (1440, 900)),
    ("results.mobile", (390, 844)),
    ("research-no-tour.mobile", (390, 844)),
    ("empty-results.desktop", (1440, 900)),
    ("offline.mobile", (390, 844)),
)


def _propertyquarry_visual_output_dir() -> Path:
    configured = str(os.environ.get("PROPERTYQUARRY_VISUAL_ACTUAL_DIR", "")).strip()
    if not configured:
        pytest.skip("PROPERTYQUARRY_VISUAL_ACTUAL_DIR is required for visual baseline capture")

    output_dir = Path(configured).expanduser()
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    for candidate in (output_dir, *output_dir.parents):
        if candidate.is_symlink():
            raise AssertionError(f"visual output path must not contain symlinks: {candidate}")

    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise AssertionError(f"visual output path must be a real directory: {output_dir}")

    expected_names = {f"{case_id}.png" for case_id, _viewport in _PROPERTYQUARRY_VISUAL_CASES}
    for child in output_dir.iterdir():
        if child.is_symlink():
            raise AssertionError(f"visual output entries must not be symlinks: {child}")
        if child.name not in expected_names or not child.is_file():
            raise AssertionError(f"unexpected visual output entry: {child}")
    return output_dir


def _new_propertyquarry_visual_context(
    browser: Browser,
    *,
    viewport: tuple[int, int],
    authenticated: bool,
) -> BrowserContext:
    assert browser.browser_type.name == "chromium", "visual baselines must be captured with Chromium"
    kwargs: dict[str, object] = {
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "locale": "en-US",
        "timezone_id": "UTC",
        "device_scale_factor": 1,
        "reduced_motion": "reduce",
        "color_scheme": "light",
        "service_workers": "block",
    }
    if authenticated:
        kwargs["extra_http_headers"] = {"X-EA-Principal-ID": "pq-greenfield-browser"}
    context = browser.new_context(**kwargs)
    _stub_visual_map_previews(context)
    return context


def _assert_public_mobile_locale_is_in_flow(page: Page) -> None:
    metrics = page.evaluate(
        """() => {
            const panel = document.querySelector(
                '[data-pq-localization-placement="floating"]'
            );
            const footer = document.querySelector('footer');
            if (!(panel instanceof HTMLElement) || !(footer instanceof HTMLElement)) {
                return null;
            }
            const panelRect = panel.getBoundingClientRect();
            const footerRect = footer.getBoundingClientRect();
            return {
                position: getComputedStyle(panel).position,
                panelTop: panelRect.top,
                panelBottom: panelRect.bottom,
                footerBottom: footerRect.bottom,
                documentHeight: document.documentElement.scrollHeight,
            };
        }"""
    )
    assert isinstance(metrics, dict), metrics
    assert metrics["position"] == "relative", metrics
    assert float(metrics["panelTop"]) >= float(metrics["footerBottom"]) - 1, metrics
    assert float(metrics["panelBottom"]) <= float(metrics["documentHeight"]) + 1, metrics


def _wait_for_propertyquarry_visual_stability(page: Page) -> None:
    page.wait_for_load_state("domcontentloaded")
    page.add_style_tag(
        content="""
            *, *::before, *::after {
                animation-delay: 0s !important;
                animation-duration: 0s !important;
                caret-color: transparent !important;
                transition: none !important;
            }
            html { scroll-behavior: auto !important; }
        """
    )
    page.evaluate(
        """async () => {
            if (document.fonts && document.fonts.ready) {
                await document.fonts.ready;
            }
            window.scrollTo(0, 0);
            const pendingImages = Array.from(document.images).filter((image) => {
                if (image.complete) return false;
                const rect = image.getBoundingClientRect();
                return rect.bottom > 0 && rect.right > 0
                    && rect.top < window.innerHeight && rect.left < window.innerWidth;
            });
            const imagesReady = Promise.all(pendingImages.map((image) => new Promise((resolve) => {
                image.addEventListener('load', resolve, { once: true });
                image.addEventListener('error', resolve, { once: true });
            })));
            let imageTimeoutId = 0;
            const imageTimeout = new Promise((_, reject) => {
                imageTimeoutId = window.setTimeout(
                    () => reject(new Error('visible images did not settle within 10 seconds')),
                    10000,
                );
            });
            try {
                await Promise.race([imagesReady, imageTimeout]);
            } finally {
                window.clearTimeout(imageTimeoutId);
            }
            await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        }"""
    )


def _capture_propertyquarry_visual_case(
    page: Page,
    *,
    output_dir: Path,
    case_id: str,
    viewport: tuple[int, int],
) -> None:
    assert page.viewport_size == {"width": viewport[0], "height": viewport[1]}
    _wait_for_propertyquarry_visual_stability(page)
    payload = page.screenshot(
        animations="disabled",
        caret="hide",
        full_page=False,
        scale="css",
        type="png",
    )
    assert isinstance(payload, bytes)
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.size == viewport

    target = output_dir / f"{case_id}.png"
    if target.is_symlink():
        raise AssertionError(f"visual output file must not be a symlink: {target}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    assert target.stat().st_size == len(payload)


@pytest.mark.skipif(
    not str(os.environ.get("PROPERTYQUARRY_VISUAL_ACTUAL_DIR", "")).strip(),
    reason="PROPERTYQUARRY_VISUAL_ACTUAL_DIR is required for visual baseline capture",
)
def test_propertyquarry_deterministic_visual_baseline_capture_matrix(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    output_dir = _propertyquarry_visual_output_dir()
    base_url = str(propertyquarry_browser_server["base_url"])

    def capture_public(case_id: str, viewport: tuple[int, int], path: str, heading: str) -> None:
        context = _new_propertyquarry_visual_context(browser, viewport=viewport, authenticated=False)
        page = context.new_page()
        try:
            response = page.goto(f"{base_url}{path}", wait_until="networkidle")
            assert response is not None and response.ok
            expect(page.get_by_role("heading", name=heading)).to_be_visible()
            if viewport[0] <= 430:
                _assert_public_mobile_locale_is_in_flow(page)
            _capture_propertyquarry_visual_case(
                page,
                output_dir=output_dir,
                case_id=case_id,
                viewport=viewport,
            )
        finally:
            context.close()

    capture_public(
        "public-home.desktop",
        (1440, 820),
        "/?home=1",
        "Search once. See the right homes. Decide faster.",
    )
    capture_public(
        "public-home.mobile",
        (430, 932),
        "/?home=1",
        "Search once. See the right homes. Decide faster.",
    )
    capture_public("sign-in.desktop", (1440, 820), "/sign-in", "Continue your property search.")
    capture_public("sign-in.mobile", (430, 932), "/sign-in", "Continue your property search.")

    def capture_authenticated(
        case_id: str,
        viewport: tuple[int, int],
        path: str,
        ready_selector: str,
    ) -> None:
        context = _new_propertyquarry_visual_context(browser, viewport=viewport, authenticated=True)
        page = context.new_page()
        try:
            response = page.goto(f"{base_url}{path}", wait_until="domcontentloaded")
            assert response is not None and response.ok
            expect(page.locator(ready_selector).first).to_be_visible(timeout=15_000)
            _capture_propertyquarry_visual_case(
                page,
                output_dir=output_dir,
                case_id=case_id,
                viewport=viewport,
            )
        finally:
            context.close()

    capture_authenticated(
        "search-setup.desktop",
        (1600, 1200),
        "/app/search",
        '[data-console-form-variant="property_search"]',
    )
    capture_authenticated(
        "search-setup.mobile",
        (430, 932),
        "/app/search",
        '[data-console-form-variant="property_search"]',
    )
    capture_authenticated(
        "results.desktop",
        (1440, 900),
        "/app/shortlist/run/run-42",
        "main",
    )
    capture_authenticated(
        "results.mobile",
        (390, 844),
        "/app/shortlist?run_id=run-42&full=1",
        "[data-property-decision-workbench]",
    )

    research_context = _new_propertyquarry_visual_context(
        browser,
        viewport=(390, 844),
        authenticated=True,
    )
    research_page = research_context.new_page()
    try:
        response = research_page.goto(
            f"{base_url}/app/shortlist?run_id=run-42&full=1",
            wait_until="domcontentloaded",
        )
        assert response is not None and response.ok
        family_row = research_page.locator(
            "[data-workbench-row]",
            has_text="Family flat near Tiergarten",
        ).first
        expect(family_row).to_be_visible(timeout=15_000)
        packet_path = family_row.get_attribute("data-candidate-packet-url")
        assert packet_path
        separator = "&" if "?" in packet_path else "?"
        response = research_page.goto(
            f"{base_url}{packet_path}{separator}run_id=run-42",
            wait_until="domcontentloaded",
        )
        assert response is not None and response.ok
        expect(research_page.locator("[data-property-research-detail]")).to_be_visible(timeout=15_000)
        expect(research_page.get_by_role("heading", name="Family flat near Tiergarten", exact=True)).to_be_visible()
        assert research_page.get_by_text("Open 3D tour", exact=True).count() == 0
        request_or_retry = research_page.get_by_role(
            "button",
            name=re.compile(r"(?:request|retry).*?(?:3d|tour)|(?:3d|tour).*?(?:request|retry)", re.I),
        )
        if request_or_retry.count() == 0:
            request_or_retry = research_page.get_by_role(
                "link",
                name=re.compile(r"(?:request|retry).*?(?:3d|tour)|(?:3d|tour).*?(?:request|retry)", re.I),
            )
        expect(request_or_retry.first).to_be_visible()
        _capture_propertyquarry_visual_case(
            research_page,
            output_dir=output_dir,
            case_id="research-no-tour.mobile",
            viewport=(390, 844),
        )
    finally:
        research_context.close()

    empty_context = _new_propertyquarry_visual_context(
        browser,
        viewport=(1440, 900),
        authenticated=True,
    )
    empty_page = empty_context.new_page()
    try:
        response = empty_page.goto(
            f"{base_url}/app/shortlist/run/run-active-empty",
            wait_until="domcontentloaded",
        )
        assert response is not None and response.ok
        expect(empty_page.get_by_role("heading", name="No matching homes yet", exact=True)).to_be_visible(
            timeout=15_000
        )
        _capture_propertyquarry_visual_case(
            empty_page,
            output_dir=output_dir,
            case_id="empty-results.desktop",
            viewport=(1440, 900),
        )
    finally:
        empty_context.close()

    offline_context = _new_propertyquarry_visual_context(
        browser,
        viewport=(390, 844),
        authenticated=True,
    )
    offline_page = offline_context.new_page()
    try:
        response = offline_page.goto(
            f"{base_url}/app/search?continuous_ux_state=offline",
            wait_until="domcontentloaded",
        )
        assert response is not None and response.ok
        offline_context.set_offline(True)
        offline_page.evaluate("window.dispatchEvent(new Event('offline'))")
        expect(offline_page.locator('[data-pq-failure-state="offline"]')).to_be_visible(timeout=15_000)
        _capture_propertyquarry_visual_case(
            offline_page,
            output_dir=output_dir,
            case_id="offline.mobile",
            viewport=(390, 844),
        )
    finally:
        offline_context.set_offline(False)
        offline_context.close()

    actual_names = {path.name for path in output_dir.iterdir()}
    expected_names = {f"{case_id}.png" for case_id, _viewport in _PROPERTYQUARRY_VISUAL_CASES}
    assert actual_names == expected_names


def test_propertyquarry_research_evidence_states_and_links_render_in_real_browser(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evidence_overlays,
        "_now",
        lambda: datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
    )
    property_url = "https://www.immobilienscout24.de/expose/altbau-u6"
    article_url = "https://news.example.test/article/altbau-u6"
    rollup_path = tmp_path / "browser-evidence-rollups.json"
    rollup_path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "layer_key": "media_attention",
                        "match": {"property_url": property_url},
                        "ui_state": "verified",
                        "summary": "12 local articles in the last 90 days, mostly transport and development.",
                        "source_name": "Public media index",
                        "source_url": "https://news.example.test/search/altbau-u6",
                        "article_url": article_url,
                        "cache_updated_at": "2026-07-17T00:00:00+00:00",
                        "source_updated_at": "2026-07-16T00:00:00+00:00",
                        "source_checked_at": "2026-07-17T00:00:00+00:00",
                        "source_temporality": "current_feed",
                        "media_source_class": "independent_press",
                        "independent_press": True,
                        "uncertainty_label": "topic aggregate",
                    },
                    {
                        "layer_key": "fiber_broadband",
                        "match": {"property_url": property_url},
                        "ui_state": "stale",
                        "summary": "The official broadband grid snapshot is waiting for an update.",
                        "source_name": "Official broadband grid",
                        "source_url": "https://broadband.example.test/altbau-u6",
                        "cache_updated_at": "2025-01-01T00:00:00+00:00",
                        "source_updated_at": "2024-12-31T00:00:00+00:00",
                        "source_temporality": "reference",
                        "reference_period": "2024",
                        "uncertainty_label": "area grid",
                    },
                ]
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROPERTYQUARRY_EVIDENCE_OVERLAY_READ_MODEL", "file")
    monkeypatch.setenv("PROPERTYQUARRY_EVIDENCE_OVERLAY_ROLLUP_PATH", str(rollup_path))

    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=1000)
    page: Page = context.new_page()
    try:
        response = page.goto(
            f"{base_url}/app/shortlist?run_id=run-42&full=1",
            wait_until="networkidle",
        )
        assert response is not None and response.ok
        packet_path = page.locator(
            "[data-workbench-row]",
            has_text="Altbau near U6",
        ).first.get_attribute("data-candidate-packet-url")
        assert packet_path
        packet_url = packet_path if packet_path.startswith("http") else f"{base_url}{packet_path}"

        response = page.goto(packet_url, wait_until="networkidle")
        assert response is not None and response.ok
        detail = page.locator("[data-property-research-detail]")
        expect(detail).to_be_visible()
        map_context = detail.locator("details.prd-collapse", has_text="Map and context").first
        expect(map_context.locator("summary")).to_be_visible()
        map_context.locator("summary").click()
        media_row = map_context.locator(".prd-row", has_text="Media attention")
        expect(media_row).to_contain_text("12 local articles in the last 90 days")
        expect(media_row).to_contain_text("Ready")
        broadband_row = map_context.locator(".prd-row", has_text="Fiber and broadband")
        expect(broadband_row).to_contain_text("official broadband grid snapshot is waiting for an update")
        expect(broadband_row).to_contain_text("Stale")
        environmental_row = map_context.locator(".prd-row", has_text="Environmental quality")
        expect(environmental_row).to_contain_text("Unavailable")
        expect(environmental_row.locator("a")).to_have_count(0)
        article_link = media_row.get_by_role(
            "link",
            name="Open source for Media attention (opens in new tab)",
        )
        expect(article_link).to_be_visible()
        expect(article_link).to_have_attribute("href", article_url)
        expect(article_link).to_have_attribute("target", "_blank")
        expect(article_link).to_have_attribute("rel", "noopener noreferrer")
    finally:
        context.close()
