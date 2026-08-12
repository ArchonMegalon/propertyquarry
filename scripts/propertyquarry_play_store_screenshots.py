#!/usr/bin/env python3
"""Capture privacy-safe Google Play phone screenshots from the live app."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.propertyquarry_live_http_security import (
    url_matches_origin,
    validated_live_base_origin,
)
from scripts.propertyquarry_live_mobile_surface_smoke import (
    SEEDED_RESEARCH_DETAIL_ROUTE,
    _continue_playwright_route_with_origin_scoped_headers,
    _release_probe_browser_navigation_url,
)
from scripts.propertyquarry_live_probe_secret_scope import (
    read_release_probe_secret_from_stdin,
    scrub_release_probe_secret_environment,
)
from scripts.propertyquarry_playwright_runtime import playwright_engine_launch_browser


CSS_VIEWPORT_WIDTH = 360
CSS_VIEWPORT_HEIGHT = 640
DEVICE_SCALE_FACTOR = 3
PLAY_STORE_IMAGE_WIDTH = CSS_VIEWPORT_WIDTH * DEVICE_SCALE_FACTOR
PLAY_STORE_IMAGE_HEIGHT = CSS_VIEWPORT_HEIGHT * DEVICE_SCALE_FACTOR
MAX_PLAY_STORE_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_OUTPUT_DIR = ROOT / "mobile" / "store" / "graphics"
DEFAULT_RECEIPT_PATH = ROOT / "state" / "qa" / "propertyquarry-play-store-screenshots-current.json"
SCREENSHOTS = (
    {
        "id": "search",
        "route": "/app/search",
        "filename": "phone-search-1080x1920.png",
        "receipt_route": "/app/search",
    },
    {
        "id": "research",
        "route": SEEDED_RESEARCH_DETAIL_ROUTE,
        "filename": "phone-research-1080x1920.png",
        "receipt_route": "/app/research/[synthetic-release-probe]",
    },
)
ERROR_PAGE_MARKERS = (
    "internal server error",
    "service unavailable",
    "something went wrong",
    "this site can’t be reached",
    "this site can't be reached",
)


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"play_store_screenshot_not_png:{path.name}")
    return struct.unpack(">II", header[16:24])


def validate_play_store_screenshot(path: Path) -> dict[str, object]:
    width, height = png_dimensions(path)
    size_bytes = path.stat().st_size
    if (width, height) != (PLAY_STORE_IMAGE_WIDTH, PLAY_STORE_IMAGE_HEIGHT):
        raise ValueError(
            f"play_store_screenshot_dimensions_invalid:{path.name}:{width}x{height}"
        )
    if size_bytes <= 0 or size_bytes > MAX_PLAY_STORE_IMAGE_BYTES:
        raise ValueError(f"play_store_screenshot_size_invalid:{path.name}:{size_bytes}")
    return {
        "filename": path.name,
        "width": width,
        "height": height,
        "size_bytes": size_bytes,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _atomic_write_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _publish_screenshots(
    temporary_dir: Path,
    output_dir: Path,
    screenshot_records: list[dict[str, object]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for record in screenshot_records:
        filename = str(record["filename"])
        source = temporary_dir / filename
        destination = output_dir / filename
        os.chmod(source, 0o644)
        os.replace(source, destination)


def _capture_live_screenshots(
    *,
    base_origin: str,
    detail_route: str,
    release_probe_secret: str,
    output_dir: Path,
    timeout_ms: int,
) -> list[dict[str, object]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment failure.
        raise RuntimeError("playwright_not_installed") from exc

    configured_routes = (detail_route,)
    definitions = [dict(item) for item in SCREENSHOTS]
    definitions[1]["route"] = detail_route
    request_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    }

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".propertyquarry-play-store-screenshots-",
        dir=output_dir.parent,
    ) as temporary_name:
        temporary_dir = Path(temporary_name)
        records: list[dict[str, object]] = []
        with sync_playwright() as playwright:
            browser = playwright_engine_launch_browser(
                playwright,
                engine="chromium",
                args=["--disable-dev-shm-usage"],
            )
            try:
                context = browser.new_context(
                    viewport={"width": CSS_VIEWPORT_WIDTH, "height": CSS_VIEWPORT_HEIGHT},
                    device_scale_factor=DEVICE_SCALE_FACTOR,
                    has_touch=True,
                    is_mobile=True,
                    locale="de-AT",
                    color_scheme="light",
                    reduced_motion="reduce",
                    service_workers="block",
                )
                try:
                    context.route(
                        "**/*",
                        lambda route: _continue_playwright_route_with_origin_scoped_headers(
                            route,
                            authorized_origin=base_origin,
                            headers=request_headers,
                            release_probe_secret=release_probe_secret,
                            release_probe_configured_routes=configured_routes,
                        ),
                    )
                    page = context.new_page()
                    page.set_default_timeout(timeout_ms)
                    page.set_default_navigation_timeout(timeout_ms)
                    for definition in definitions:
                        route_path = str(definition["route"])
                        requested_url = f"{base_origin}{route_path}"
                        navigation_url, redirect_receipt = _release_probe_browser_navigation_url(
                            requested_url,
                            headers=request_headers,
                            authorized_origin=base_origin,
                            timeout_seconds=max(1.0, timeout_ms / 1000.0),
                            release_probe_secret=release_probe_secret,
                            release_probe_configured_routes=configured_routes,
                        )
                        response = page.goto(
                            navigation_url,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                        if response is None or int(response.status) != 200:
                            status = int(response.status) if response is not None else 0
                            raise RuntimeError(
                                f"play_store_screenshot_navigation_failed:{definition['id']}:{status}"
                            )
                        try:
                            page.wait_for_load_state("networkidle", timeout=min(10_000, timeout_ms))
                        except Exception:
                            pass
                        page.wait_for_timeout(1_000)
                        if not url_matches_origin(page.url, base_origin):
                            raise RuntimeError(
                                f"play_store_screenshot_cross_origin:{definition['id']}"
                            )
                        page.add_style_tag(
                            content="""
                                *, *::before, *::after {
                                  animation-duration: 0s !important;
                                  animation-delay: 0s !important;
                                  caret-color: transparent !important;
                                  scroll-behavior: auto !important;
                                  transition-duration: 0s !important;
                                }
                            """
                        )
                        page.evaluate(
                            """async () => {
                              if (document.fonts?.ready) await document.fonts.ready;
                              window.scrollTo(0, 0);
                            }"""
                        )
                        title = str(page.title() or "").strip()
                        body_text = str(page.locator("body").inner_text(timeout=timeout_ms) or "")
                        lowered_body = body_text.lower()
                        if not title or not body_text.strip():
                            raise RuntimeError(
                                f"play_store_screenshot_document_empty:{definition['id']}"
                            )
                        if any(marker in lowered_body for marker in ERROR_PAGE_MARKERS):
                            raise RuntimeError(
                                f"play_store_screenshot_error_page:{definition['id']}"
                            )
                        image_path = temporary_dir / str(definition["filename"])
                        page.screenshot(
                            path=str(image_path),
                            full_page=False,
                            animations="disabled",
                            caret="hide",
                            scale="device",
                        )
                        record = validate_play_store_screenshot(image_path)
                        record.update(
                            {
                                "id": str(definition["id"]),
                                "route": str(definition["receipt_route"]),
                                "status_code": int(response.status),
                                "title": title,
                                "same_origin": True,
                                "redirect_resolved": bool(
                                    redirect_receipt.get("release_probe_redirect_resolved")
                                ),
                            }
                        )
                        records.append(record)
                finally:
                    context.close()
            finally:
                browser.close()

        _publish_screenshots(temporary_dir, output_dir, records)
        return records


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-origin", default="https://propertyquarry.com")
    parser.add_argument("--detail-route", default=SEEDED_RESEARCH_DETAIL_ROUTE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--receipt-path", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--timeout-ms", type=int, default=45_000)
    parser.add_argument("--release-probe-secret-stdin", action="store_true")
    args = parser.parse_args(argv)
    if not args.release_probe_secret_stdin:
        parser.error("--release-probe-secret-stdin is required for live screenshots")
    if not 5_000 <= int(args.timeout_ms) <= 120_000:
        parser.error("--timeout-ms must be between 5000 and 120000")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    parser = argparse.ArgumentParser(add_help=False)
    release_probe_secret = read_release_probe_secret_from_stdin(parser, enabled=True)
    scrub_release_probe_secret_environment()
    base_origin = validated_live_base_origin(str(args.base_origin))
    records = _capture_live_screenshots(
        base_origin=base_origin,
        detail_route=str(args.detail_route),
        release_probe_secret=release_probe_secret,
        output_dir=Path(args.output_dir).resolve(),
        timeout_ms=int(args.timeout_ms),
    )
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass",
        "base_origin": base_origin,
        "browser_engine": "chromium",
        "source_identity": "synthetic-release-probe",
        "screenshots": records,
    }
    encoded_receipt = json.dumps(receipt, sort_keys=True)
    if release_probe_secret in encoded_receipt:
        raise RuntimeError("release_probe_secret_in_screenshot_receipt")
    _atomic_write_private_json(Path(args.receipt_path).resolve(), receipt)
    for record in records:
        print(
            f"{record['filename']} {record['width']}x{record['height']} "
            f"sha256={record['sha256']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
