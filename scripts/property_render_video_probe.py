#!/usr/bin/env python3
from __future__ import annotations

import math
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any


PUBLIC_VIDEO_EXTENSIONS = frozenset({".m4v", ".mov", ".mp4", ".webm"})
VIDEO_PROBE_TIMEOUT_MS = 15_000
_CHROMIUM_OFFLINE_ARGS = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
    "--safebrowsing-disable-auto-update",
)


class PropertyRenderVideoProbeError(ValueError):
    """Stable, path-free rejection from the local render-video probe."""


def _fail(reason: str) -> None:
    raise PropertyRenderVideoProbeError(reason)


def probe_local_video(
    path: Path,
    *,
    timeout_ms: int = VIDEO_PROBE_TIMEOUT_MS,
    sync_playwright_factory: Callable[[], Any] | None = None,
    launch_browser: Callable[..., Any] | None = None,
) -> dict[str, object]:
    """Inspect one exact local video with the pinned render Chromium.

    The browser context is offline and permits only the canonical ``file:`` URL
    for the supplied asset. This keeps the render image's deliberate no-ffprobe
    contract while still proving that the bytes expose a real video stream and
    finite duration to the same media capability used by browser playback proof.
    """

    candidate = Path(path)
    if candidate.suffix.lower() not in PUBLIC_VIDEO_EXTENSIONS:
        _fail("extension_invalid")
    try:
        before = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError:
        _fail("file_missing")
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_size <= 0
        or resolved != candidate.absolute()
    ):
        _fail("file_invalid")

    if sync_playwright_factory is None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            _fail("playwright_unavailable")
        sync_playwright_factory = sync_playwright
    if launch_browser is None:
        try:
            from scripts.propertyquarry_playwright_runtime import (
                playwright_engine_launch_browser,
            )
        except ModuleNotFoundError:
            try:
                from propertyquarry_playwright_runtime import (  # type: ignore[no-redef]
                    playwright_engine_launch_browser,
                )
            except ModuleNotFoundError:
                _fail("playwright_launcher_unavailable")
        launch_browser = playwright_engine_launch_browser

    allowed_url = resolved.as_uri()
    bounded_timeout_ms = max(1_000, min(int(timeout_ms), 60_000))
    browser = None
    context = None
    try:
        with sync_playwright_factory() as playwright:
            browser = launch_browser(
                playwright,
                engine="chromium",
                args=list(_CHROMIUM_OFFLINE_ARGS),
            )
            context = browser.new_context(
                accept_downloads=False,
                offline=True,
                service_workers="block",
            )
            page = context.new_page()

            def route_local_asset(route: Any) -> None:
                if route.request.url == allowed_url:
                    route.continue_()
                else:
                    route.abort()

            page.route("**/*", route_local_asset)
            page.goto(allowed_url, wait_until="commit", timeout=bounded_timeout_ms)
            page.wait_for_function(
                """
                () => {
                  const video = document.querySelector("video");
                  return Boolean(video && (
                    video.error || (
                      Number.isFinite(video.duration) &&
                      video.duration > 0 &&
                      video.videoWidth > 0 &&
                      video.videoHeight > 0
                    )
                  ));
                }
                """,
                timeout=bounded_timeout_ms,
            )
            observed = page.evaluate(
                """
                () => {
                  const video = document.querySelector("video");
                  return {
                    duration_seconds: video ? video.duration : 0,
                    height: video ? video.videoHeight : 0,
                    media_error: video && video.error ? video.error.code : null,
                    ready_state: video ? video.readyState : 0,
                    width: video ? video.videoWidth : 0,
                  };
                }
                """
            )
            with suppress(Exception):
                context.close()
            context = None
            with suppress(Exception):
                browser.close()
            browser = None
    except PropertyRenderVideoProbeError:
        raise
    except Exception as exc:
        raise PropertyRenderVideoProbeError(
            f"browser_probe_failed:{type(exc).__name__}"
        ) from exc
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    try:
        after = candidate.lstat()
        after_resolved = candidate.resolve(strict=True)
    except OSError:
        _fail("file_changed")
    if (
        not stat.S_ISREG(after.st_mode)
        or stat.S_ISLNK(after.st_mode)
        or after_resolved != resolved
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    ):
        _fail("file_changed")
    if not isinstance(observed, dict):
        _fail("result_invalid")
    try:
        duration = float(observed.get("duration_seconds"))
        width = int(observed.get("width"))
        height = int(observed.get("height"))
        ready_state = int(observed.get("ready_state"))
    except (TypeError, ValueError, OverflowError):
        _fail("result_invalid")
    if (
        observed.get("media_error") is not None
        or not math.isfinite(duration)
        or duration <= 0.0
        or width <= 0
        or height <= 0
        or ready_state < 1
    ):
        _fail("video_stream_invalid")
    return {
        "duration_seconds": duration,
        "height": height,
        "size_bytes": before.st_size,
        "width": width,
    }
