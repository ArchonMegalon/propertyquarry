#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html as html_lib
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_API_CONTAINER = "propertyquarry-api"
DEFAULT_RENDER_CONTAINER = "propertyquarry-render-tools"
_BROWSER_SHELL_VIEWER_BOOTSTRAP_WAIT_MS = 5_000
_BROWSER_SHELL_PROBE_TIMEOUT_SECONDS = 240
_LAYOUT_VIEWER_MIN_STAGING_OBJECTS_PER_ROUTE_STOP = 2
_LAYOUT_VIEWER_MIN_PROJECTED_STAGING_COVERAGE_PCT = 0.03
ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
for candidate in (ROOT, EA_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts.ensure_propertyquarry_render_bridge_runtime import build_render_bridge_runtime_receipt


def _run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)


def _docker_published_host_port(container: str, *, container_port: int = 8090) -> int | None:
    normalized_container = str(container or "").strip()
    if not normalized_container or not shutil.which("docker"):
        return None
    inspected = _run(["docker", "port", normalized_container, f"{int(container_port)}/tcp"], timeout=15)
    if inspected.returncode != 0:
        return None
    for raw_line in (inspected.stdout or "").splitlines():
        line = str(raw_line or "").strip()
        if not line:
            continue
        host_binding = line.split("->", 1)[-1].strip()
        if host_binding.startswith("[::]:"):
            host_binding = host_binding[5:]
        if ":" in host_binding:
            host_binding = host_binding.rsplit(":", 1)[-1].strip()
        try:
            published_port = int(host_binding)
        except Exception:
            continue
        if published_port > 0:
            return published_port
    return None


def _resolved_local_public_base_url(
    public_base_url: str,
    *,
    public_container: str,
    container_port: int = 8090,
) -> str:
    normalized_base_url = str(public_base_url or "").strip().rstrip("/")
    if not normalized_base_url:
        return ""
    parsed = urllib.parse.urlparse(normalized_base_url)
    host = str(parsed.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return normalized_base_url
    try:
        requested_port = int(parsed.port or 0)
    except ValueError:
        return normalized_base_url
    if requested_port != int(container_port):
        return normalized_base_url
    published_port = _docker_published_host_port(public_container, container_port=container_port)
    if not published_port or published_port == requested_port:
        return normalized_base_url
    if host == "::1":
        netloc = f"[::1]:{published_port}"
    else:
        netloc = f"{host}:{published_port}"
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc)).rstrip("/")


def _runtime_reconstruction_generation_timeout_seconds(container: str) -> int:
    raw_override = str(os.getenv("PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_GENERATION_TIMEOUT_SECONDS") or "").strip()
    if raw_override:
        try:
            return max(60, int(raw_override))
        except Exception:
            pass
    normalized_container = str(container or "").strip().lower()
    if "render-tools" in normalized_container:
        return 420
    return 180


def _timeout_stream_tail(exc: subprocess.TimeoutExpired, stream: str) -> str:
    if stream == "stdout":
        value = getattr(exc, "stdout", None)
        if value is None:
            value = getattr(exc, "output", None)
    else:
        value = getattr(exc, "stderr", None)
    return str(value or "")[-1000:]


def _generated_reconstruction_viewer_url(*, public_base_url: str, slug: str) -> str:
    normalized_base = str(public_base_url or "").strip().rstrip("/")
    normalized_slug = str(slug or "").strip().strip("/")
    if not normalized_base or not normalized_slug:
        return ""
    return f"{normalized_base}/tours/files/{urllib.parse.quote(normalized_slug, safe='')}/generated-reconstruction/viewer.html"


def _generated_reconstruction_canonical_url(*, public_base_url: str, slug: str) -> str:
    normalized_base = str(public_base_url or "").strip().rstrip("/")
    normalized_slug = str(slug or "").strip().strip("/")
    if not normalized_base or not normalized_slug:
        return ""
    return f"{normalized_base}/tours/{urllib.parse.quote(normalized_slug, safe='')}"


def _generated_reconstruction_model_url(*, public_base_url: str, slug: str) -> str:
    normalized_base = str(public_base_url or "").strip().rstrip("/")
    normalized_slug = str(slug or "").strip().strip("/")
    if not normalized_base or not normalized_slug:
        return ""
    return f"{normalized_base}/tours/files/{urllib.parse.quote(normalized_slug, safe='')}/generated-reconstruction/model.obj"


def _generated_reconstruction_payload_url(*, public_base_url: str, slug: str) -> str:
    normalized_base = str(public_base_url or "").strip().rstrip("/")
    normalized_slug = str(slug or "").strip().strip("/")
    if not normalized_base or not normalized_slug:
        return ""
    return f"{normalized_base}/tours/{urllib.parse.quote(normalized_slug, safe='')}.json"


def _host_public_tour_root() -> Path:
    return Path(str(os.getenv("EA_PUBLIC_TOUR_DIR") or "/docker/property/state/public_property_tours")).expanduser()


def _public_base_url_is_local(public_base_url: str) -> bool:
    parsed = urllib.parse.urlparse(str(public_base_url or "").strip())
    return str(parsed.hostname or "").strip().lower() in {"127.0.0.1", "localhost", "::1", "propertyquarry.com"}


def _sync_container_tour_to_host_root(container: str, *, slug: str, public_base_url: str) -> dict[str, object]:
    normalized_container = str(container or "").strip()
    normalized_slug = str(slug or "").strip().strip("/")
    if not normalized_container:
        return {"status": "blocked", "reason": "container_missing"}
    if not normalized_slug:
        return {"status": "blocked", "reason": "slug_missing"}
    if not _public_base_url_is_local(public_base_url):
        return {"status": "skipped", "reason": "public_base_url_not_local"}
    host_root = _host_public_tour_root()
    host_root.mkdir(parents=True, exist_ok=True)
    destination = host_root / normalized_slug
    with tempfile.TemporaryDirectory(prefix="propertyquarry-runtime-tour-sync-") as temp_dir:
        temp_path = Path(temp_dir)
        copied = _run(
            [
                "docker",
                "cp",
                f"{normalized_container}:/data/public_property_tours/{normalized_slug}",
                str(temp_path),
            ],
            timeout=120,
        )
        if copied.returncode != 0:
            return {
                "status": "failed",
                "reason": "docker_cp_failed",
                "host_root": str(host_root),
                "stdout_tail": str(copied.stdout or "")[-1000:],
                "stderr_tail": str(copied.stderr or "")[-1000:],
            }
        copied_bundle = temp_path / normalized_slug
        if not copied_bundle.is_dir():
            return {
                "status": "failed",
                "reason": "copied_bundle_missing",
                "host_root": str(host_root),
            }
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(copied_bundle, destination)
    return {
        "status": "pass",
        "host_root": str(host_root),
        "destination": str(destination),
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None

    def http_error_301(self, req, fp, code, msg, headers):  # type: ignore[no-untyped-def]
        return fp

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


def _http_probe(url: str, *, host_header: str = "") -> dict[str, object]:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    headers = {"User-Agent": "PropertyQuarry release gate"}
    normalized_host_header = str(host_header or "").strip()
    if normalized_host_header and str(parsed.hostname or "").strip().lower() in {"127.0.0.1", "localhost", "::1"}:
        headers["Host"] = normalized_host_header
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read(65_536)
            return {
                "status_code": int(response.getcode() or 0),
                "location": str(response.headers.get("location") or ""),
                "body_excerpt": body.decode("utf-8", errors="replace")[:65_536],
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(65_536)
        return {
            "status_code": int(exc.code or 0),
            "location": str(exc.headers.get("location") or ""),
            "body_excerpt": body.decode("utf-8", errors="replace")[:65_536],
        }
    except Exception as exc:
        return {"status_code": 0, "error": type(exc).__name__, "detail": str(exc)[:500]}


def _browser_url_and_args(base_url: str, host_header: str) -> tuple[str, list[str]]:
    parsed = urllib.parse.urlparse(str(base_url or "http://localhost:8097").strip().rstrip("/"))
    host = str(parsed.hostname or "").lower()
    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--autoplay-policy=no-user-gesture-required",
    ]
    if host_header and host in {"127.0.0.1", "localhost", "::1"}:
        target_host = str(host_header).strip().split(":", 1)[0]
        netloc = target_host
        if parsed.port:
            netloc = f"{target_host}:{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
        args.extend(
            [
                f"--host-resolver-rules=MAP {target_host} 127.0.0.1",
                "--no-proxy-server",
            ]
        )
    return urllib.parse.urlunparse(parsed).rstrip("/"), args


def _browser_shell_probe_timeout_seconds() -> int:
    raw_override = str(
        os.getenv("PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_BROWSER_SHELL_TIMEOUT_SECONDS") or ""
    ).strip()
    if raw_override:
        try:
            return max(1, int(raw_override))
        except Exception:
            pass
    return _BROWSER_SHELL_PROBE_TIMEOUT_SECONDS


def _browser_shell_probe_context() -> multiprocessing.context.BaseContext:
    if os.name == "posix":
        try:
            return multiprocessing.get_context("fork")
        except ValueError:
            pass
    return multiprocessing.get_context()


def _browser_shell_probe_worker(
    result_queue: multiprocessing.queues.Queue,
    kwargs: dict[str, Any],
    progress_path: str,
) -> None:
    if progress_path:
        os.environ["PROPERTYQUARRY_BROWSER_SHELL_PROGRESS_PATH"] = progress_path
    try:
        result_queue.put(_check_generated_reconstruction_browser_shell(**kwargs))
    except Exception as exc:
        result_queue.put(
            {
                "status": "failed",
                "reason": "browser_shell_probe_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "failures": ["browser_shell_probe_failed"],
            }
        )


def _terminate_browser_shell_probe_process(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        return
    process.terminate()
    process.join(5)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(5)


def _run_bounded_browser_shell_probe(**kwargs: Any) -> dict[str, object]:
    timeout_seconds = _browser_shell_probe_timeout_seconds()
    if timeout_seconds <= 0:
        return _check_generated_reconstruction_browser_shell(**kwargs)

    context = _browser_shell_probe_context()
    result_queue = context.Queue(maxsize=1)
    progress_file = tempfile.NamedTemporaryFile(
        prefix="propertyquarry-browser-shell-",
        suffix=".json",
        delete=False,
    )
    progress_path = progress_file.name
    progress_file.close()
    process = context.Process(
        target=_browser_shell_probe_worker,
        args=(result_queue, dict(kwargs), progress_path),
        daemon=True,
    )
    process.start()
    try:
        process.join(float(timeout_seconds))
        if process.is_alive():
            _terminate_browser_shell_probe_process(process)
            last_progress = _read_browser_shell_progress(progress_path)
            return {
                "status": "failed",
                "reason": "browser_shell_probe_timeout",
                "error": f"browser_shell_probe_timeout_after_{timeout_seconds}s",
                "timeout_seconds": timeout_seconds,
                "last_progress": last_progress,
                "failures": ["browser_shell_probe_timeout"],
            }
        try:
            result = result_queue.get_nowait()
        except Exception:
            result = None
        if isinstance(result, dict):
            return result
        if process.exitcode not in (0, None):
            return {
                "status": "failed",
                "reason": "browser_shell_probe_process_failed",
                "error": f"browser_shell_probe_exitcode_{process.exitcode}",
                "exitcode": process.exitcode,
                "failures": ["browser_shell_probe_process_failed"],
            }
        return {
            "status": "failed",
            "reason": "browser_shell_probe_result_missing",
            "error": "browser_shell_probe_result_missing",
            "timeout_seconds": timeout_seconds,
            "failures": ["browser_shell_probe_result_missing"],
        }
    finally:
        _terminate_browser_shell_probe_process(process)
        try:
            result_queue.close()
        except Exception:
            pass
        try:
            Path(progress_path).unlink(missing_ok=True)
        except Exception:
            pass


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


def _record_browser_shell_progress(stage: str, **details: object) -> None:
    progress_path = str(os.getenv("PROPERTYQUARRY_BROWSER_SHELL_PROGRESS_PATH") or "").strip()
    if not progress_path:
        return
    payload = {
        "stage": str(stage or "").strip(),
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        **details,
    }
    try:
        Path(progress_path).write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        pass


def _read_browser_shell_progress(progress_path: str) -> dict[str, object]:
    path = Path(str(progress_path or "").strip())
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def _reconstruction_render_metrics(page: Any) -> dict[str, object]:
    metrics = dict(
        page.evaluate(
            """() => {
                const debug = window.__pqReconstructionDebug;
                if (!debug || typeof debug.getRenderMetrics !== 'function') {
                    return { ready: false, reason: 'debug_hook_missing' };
                }
                return debug.getRenderMetrics();
            }"""
        )
        or {}
    )
    return {
        "ready": bool(metrics.get("ready")),
        "frame_count": float(metrics.get("frameCount") or 0),
        "wall_rect_count": float(metrics.get("wallRectCount") or 0),
        "wall_mesh_count": float(metrics.get("wallMeshCount") or 0),
        "cutaway_wall_count": float(metrics.get("cutawayWallCount") or 0),
        "hidden_cutaway_wall_count": float(metrics.get("hiddenCutawayWallCount") or 0),
        "visible_wall_count": float(metrics.get("visibleWallCount") or 0),
        "route_stop_count": float(metrics.get("routeStopCount") or 0),
        "active_route_index": float(metrics.get("activeRouteIndex") or 0),
        "view_mode": str(metrics.get("viewMode") or ""),
        "wall_opacity": float(metrics.get("wallOpacity") or 0),
        "wall_height_scale": float(metrics.get("wallHeightScale") or 0),
        "staging_object_count": float(metrics.get("stagingObjectCount") or 0),
        "visible_staging_object_count": float(metrics.get("visibleStagingObjectCount") or 0),
        "photo_panel_count": float(metrics.get("photoPanelCount") or 0),
        "loaded_photo_texture_count": float(metrics.get("loadedPhotoTextureCount") or 0),
        "visible_photo_panel_count": float(metrics.get("visiblePhotoPanelCount") or 0),
        "projected_coverage_pct": float(metrics.get("projectedCoveragePct") or 0),
        "projected_photo_coverage_pct": float(metrics.get("projectedPhotoCoveragePct") or 0),
        "projected_staging_coverage_pct": float(metrics.get("projectedStagingCoveragePct") or 0),
        "render_calls": float(metrics.get("renderCalls") or 0),
        "render_triangles": float(metrics.get("renderTriangles") or 0),
    }


def _wait_for_reconstruction_viewer_ready(page: Any, *, timeout: int = 30_000) -> None:
    page.wait_for_function(
        """() => {
            const canvas = document.querySelector('#viewport canvas');
            const debug = window.__pqReconstructionDebug;
            if (!canvas || !debug || typeof debug.getRenderMetrics !== 'function') {
                return false;
            }
            const metrics = debug.getRenderMetrics();
            return Boolean(
                metrics?.ready &&
                (
                    Number(metrics?.frameCount || 0) >= 1 ||
                    Number(metrics?.renderCalls || 0) > 0 ||
                    Number(metrics?.renderTriangles || 0) > 0
                )
            );
        }""",
        timeout=timeout,
    )


def _play_tour_video_without_waiting(page: Any) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const video = document.getElementById('tour-video');
                if (!video) return false;
                const playPromise = video.play();
                if (playPromise && typeof playPromise.catch === 'function') {
                    playPromise.catch(() => null);
                }
                return true;
            }"""
        )
    )


def _stabilize_generated_reconstruction_viewer_bootstrap(page: Any) -> None:
    page.wait_for_timeout(_BROWSER_SHELL_VIEWER_BOOTSTRAP_WAIT_MS)


def _freeze_tour_video_at_start(page: Any) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const video = document.getElementById('tour-video') || document.getElementById('flythrough-video');
                if (!video) return false;
                try {
                    video.pause();
                } catch (_error) {
                    // Some browser builds reject pause before metadata; the autoplay reset below still stabilizes the shell.
                }
                try {
                    video.autoplay = false;
                } catch (_error) {
                    // Ignore autoplay mutation failures and still force the timeline back to zero.
                }
                try {
                    video.removeAttribute('autoplay');
                } catch (_error) {
                    // Ignore attribute mutation failures and still force the timeline back to zero.
                }
                try {
                    video.currentTime = 0;
                } catch (_error) {
                    // Metadata may not be ready yet. The initial route selection is already asserted separately.
                }
                return true;
            }"""
        )
    )


def _generated_reconstruction_layout_viewer_snapshot(page: Any) -> dict[str, object]:
    return dict(
        page.evaluate(
            """() => {
                const shell = document.querySelector('.layout-viewer-shell');
                const frame = document.getElementById('layout-viewer-frame');
                const shellDebug = window.__pqLayoutViewerShellDebug;
                const shellState = shellDebug && typeof shellDebug.getState === 'function'
                    ? shellDebug.getState()
                    : null;
                if (shellState && typeof shellState === 'object') {
                    const state = shellState.layoutViewerState && typeof shellState.layoutViewerState === 'object'
                        ? shellState.layoutViewerState
                        : {};
                    return {
                        lead_preview_src: String(document.getElementById('lead-preview-image')?.getAttribute('src') || '').trim(),
                        layout_viewer_present: Boolean(frame),
                        layout_viewer_ready: Boolean(shellState.ready),
                        layout_viewer_metrics_source: 'parent_shell_debug',
                        layout_viewer_route_button_count: Number(shellState.layoutViewerRouteButtonCount || 0),
                        layout_viewer_floorplan_stop_count: Number(shellState.layoutViewerFloorplanStopCount || 0),
                        layout_viewer_metrics_ready: Boolean(state.ready),
                        layout_viewer_route_stop_count: Number(state.routeStopCount || 0),
                        layout_viewer_active_route_index: Number(state.activeRouteIndex ?? -1),
                        layout_viewer_view_mode: String(state.viewMode || '').trim(),
                        layout_viewer_photo_panel_count: Number(state.photoPanelCount || 0),
                        layout_viewer_loaded_photo_texture_count: Number(state.loadedPhotoTextureCount || 0),
                        layout_viewer_visible_photo_panel_count: Number(state.visiblePhotoPanelCount || 0),
                        layout_viewer_cutaway_wall_count: Number(state.cutawayWallCount || 0),
                        layout_viewer_hidden_cutaway_wall_count: Number(state.hiddenCutawayWallCount || 0),
                        layout_viewer_wall_opacity: Number(state.wallOpacity || 0),
                        layout_viewer_wall_height_scale: Number(state.wallHeightScale || 0),
                        layout_viewer_staging_object_count: Number(state.stagingObjectCount || 0),
                        layout_viewer_visible_staging_object_count: Number(state.visibleStagingObjectCount || 0),
                        layout_viewer_projected_staging_coverage_pct: Number(state.projectedStagingCoveragePct || 0),
                        layout_viewer_render_calls: Number(state.renderCalls || 0),
                        layout_viewer_render_triangles: Number(state.renderTriangles || 0),
                    };
                }
                const doc = frame?.contentDocument;
                const debug = frame?.contentWindow?.__pqReconstructionDebug;
                const renderMetrics = debug && typeof debug.getRenderMetrics === 'function'
                    ? debug.getRenderMetrics()
                    : null;
                const liveState = debug && typeof debug.getLiveState === 'function'
                    ? debug.getLiveState()
                    : null;
                const metrics = renderMetrics && typeof renderMetrics === 'object'
                    ? { ...(liveState && typeof liveState === 'object' ? liveState : {}), ...renderMetrics }
                    : (liveState && typeof liveState === 'object' ? liveState : null);
                return {
                    lead_preview_src: String(document.getElementById('lead-preview-image')?.getAttribute('src') || '').trim(),
                    layout_viewer_present: Boolean(frame),
                    layout_viewer_ready: Boolean(shell && shell.classList.contains('is-ready')),
                    layout_viewer_metrics_source: renderMetrics && typeof renderMetrics === 'object' ? 'render_metrics' : (metrics ? 'live_state' : 'missing'),
                    layout_viewer_route_button_count: Number(doc?.querySelectorAll('.route-button').length || 0),
                    layout_viewer_floorplan_stop_count: Number(doc?.querySelectorAll('.floorplan-stop').length || 0),
                    layout_viewer_metrics_ready: Boolean(metrics && metrics.ready),
                    layout_viewer_route_stop_count: Number(metrics?.routeStopCount || 0),
                    layout_viewer_active_route_index: Number(metrics?.activeRouteIndex ?? -1),
                    layout_viewer_view_mode: String(metrics?.viewMode || '').trim(),
                    layout_viewer_photo_panel_count: Number(metrics?.photoPanelCount || 0),
                    layout_viewer_loaded_photo_texture_count: Number(metrics?.loadedPhotoTextureCount || 0),
                    layout_viewer_visible_photo_panel_count: Number(metrics?.visiblePhotoPanelCount || 0),
                    layout_viewer_cutaway_wall_count: Number(metrics?.cutawayWallCount || 0),
                    layout_viewer_hidden_cutaway_wall_count: Number(metrics?.hiddenCutawayWallCount || 0),
                    layout_viewer_wall_opacity: Number(metrics?.wallOpacity || 0),
                    layout_viewer_wall_height_scale: Number(metrics?.wallHeightScale || 0),
                    layout_viewer_staging_object_count: Number(metrics?.stagingObjectCount || 0),
                    layout_viewer_visible_staging_object_count: Number(metrics?.visibleStagingObjectCount || 0),
                    layout_viewer_projected_staging_coverage_pct: Number(metrics?.projectedStagingCoveragePct || 0),
                    layout_viewer_render_calls: Number(metrics?.renderCalls || 0),
                    layout_viewer_render_triangles: Number(metrics?.renderTriangles || 0),
                };
            }"""
        )
        or {}
    )


def _viewer_snapshot_int(snapshot: dict[str, object], key: str, *, default: int = 0) -> int:
    try:
        value = snapshot.get(key)
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _viewer_snapshot_float(snapshot: dict[str, object], key: str, *, default: float = 0.0) -> float:
    try:
        value = snapshot.get(key)
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _generated_reconstruction_layout_viewer_quality_failures(
    *,
    prefix: str,
    snapshot: dict[str, object],
    expected_route_stop_count: int,
    require_cutaway_view: bool = False,
) -> list[str]:
    failures: list[str] = []
    route_stop_count = max(
        _viewer_snapshot_int(snapshot, "layout_viewer_route_stop_count"),
        int(expected_route_stop_count or 0),
    )
    if route_stop_count <= 0:
        return failures
    required_staging_objects = route_stop_count * _LAYOUT_VIEWER_MIN_STAGING_OBJECTS_PER_ROUTE_STOP
    staging_object_count = _viewer_snapshot_int(snapshot, "layout_viewer_staging_object_count")
    visible_staging_object_count = _viewer_snapshot_int(snapshot, "layout_viewer_visible_staging_object_count")
    projected_staging_coverage_pct = _viewer_snapshot_float(
        snapshot,
        "layout_viewer_projected_staging_coverage_pct",
    )
    if staging_object_count < required_staging_objects:
        failures.append(f"{prefix}_layout_viewer_staging_object_count_low")
    if visible_staging_object_count < min(route_stop_count, max(staging_object_count, 0)):
        failures.append(f"{prefix}_layout_viewer_visible_staging_missing")
    if projected_staging_coverage_pct < _LAYOUT_VIEWER_MIN_PROJECTED_STAGING_COVERAGE_PCT:
        failures.append(f"{prefix}_layout_viewer_projected_staging_coverage_low")

    cutaway_wall_count = _viewer_snapshot_int(snapshot, "layout_viewer_cutaway_wall_count")
    hidden_cutaway_wall_count = _viewer_snapshot_int(snapshot, "layout_viewer_hidden_cutaway_wall_count")
    wall_height_scale = _viewer_snapshot_float(snapshot, "layout_viewer_wall_height_scale")
    view_mode = str(snapshot.get("layout_viewer_view_mode") or "").strip().lower()
    if cutaway_wall_count < 1:
        failures.append(f"{prefix}_layout_viewer_cutaway_wall_count_missing")
    if require_cutaway_view or view_mode in {"overview", "dollhouse"}:
        if hidden_cutaway_wall_count < 1:
            failures.append(f"{prefix}_layout_viewer_hidden_cutaway_wall_count_missing")
        if not (0.0 < wall_height_scale < 0.95):
            failures.append(f"{prefix}_layout_viewer_wall_height_scale_not_cutaway")
    return failures


def _generated_reconstruction_layout_viewer_state_matches(
    snapshot: dict[str, object],
    *,
    expected_route_stop_count: int,
    expected_photo_count: int,
    expected_active_route_index: int | None = None,
) -> bool:
    active_route_index = snapshot.get("layout_viewer_active_route_index")
    if snapshot.get("layout_viewer_ready") is not True:
        return False
    if snapshot.get("layout_viewer_metrics_ready") is not True:
        return False
    if int(snapshot.get("layout_viewer_route_stop_count") or 0) < int(expected_route_stop_count or 0):
        return False
    if int(snapshot.get("layout_viewer_route_button_count") or 0) < int(expected_route_stop_count or 0):
        return False
    if int(snapshot.get("layout_viewer_floorplan_stop_count") or 0) < int(expected_route_stop_count or 0):
        return False
    if (
        float(snapshot.get("layout_viewer_render_calls") or 0) <= 0
        and float(snapshot.get("layout_viewer_render_triangles") or 0) <= 0
    ):
        return False
    if int(expected_photo_count or 0) > 0:
        photo_panel_count = int(snapshot.get("layout_viewer_photo_panel_count") or 0)
        if photo_panel_count < int(expected_photo_count or 0):
            return False
        loaded_photo_texture_count = int(snapshot.get("layout_viewer_loaded_photo_texture_count") or 0)
        if loaded_photo_texture_count > 0 and loaded_photo_texture_count < min(int(expected_photo_count or 0), photo_panel_count):
            return False
    if _generated_reconstruction_layout_viewer_quality_failures(
        prefix="state",
        snapshot=snapshot,
        expected_route_stop_count=expected_route_stop_count,
    ):
        return False
    if expected_active_route_index is not None and int(-1 if active_route_index is None else active_route_index) != int(expected_active_route_index):
        return False
    return True


def _wait_for_generated_reconstruction_layout_viewer_state(
    page: Any,
    *,
    expected_route_stop_count: int,
    expected_photo_count: int,
    expected_active_route_index: int | None = None,
    timeout: int = 30_000,
) -> dict[str, object]:
    deadline = time.monotonic() + max(0.1, (timeout / 1000.0))
    last_snapshot: dict[str, object] = {}
    while time.monotonic() <= deadline:
        last_snapshot = _generated_reconstruction_layout_viewer_snapshot(page)
        if _generated_reconstruction_layout_viewer_state_matches(
            last_snapshot,
            expected_route_stop_count=expected_route_stop_count,
            expected_photo_count=expected_photo_count,
            expected_active_route_index=expected_active_route_index,
        ):
            return dict(last_snapshot)
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if remaining_ms <= 0:
            break
        page.wait_for_timeout(min(250, remaining_ms))
    raise TimeoutError(
        "generated_reconstruction_layout_viewer_state_timeout:"
        f"route_index={expected_active_route_index if expected_active_route_index is not None else 'any'}:"
        f"snapshot={json.dumps(last_snapshot, ensure_ascii=True, sort_keys=True)[:1200]}"
    )


def _wait_for_generated_reconstruction_layout_viewer_active_route(
    page: Any,
    *,
    expected_active_route_index: int,
    timeout: int = 30_000,
) -> None:
    deadline = time.monotonic() + max(0.1, (timeout / 1000.0))
    last_snapshot: dict[str, object] = {}
    while time.monotonic() <= deadline:
        last_snapshot = dict(
            page.evaluate(
                """() => {
                    const shellDebug = window.__pqLayoutViewerShellDebug;
                    const shellState = shellDebug && typeof shellDebug.getState === 'function'
                        ? shellDebug.getState()
                        : null;
                    if (shellState && typeof shellState === 'object') {
                        return {
                            layout_viewer_present: Boolean(document.getElementById('layout-viewer-frame')),
                            layout_viewer_ready: Boolean(shellState.ready),
                            layout_viewer_active_route_index: Number(shellState.syncedRouteIndex ?? -1),
                            layout_viewer_metrics_ready: Number(shellState.syncedRouteIndex ?? -1) >= 0,
                        };
                    }
                    const frame = document.getElementById('layout-viewer-frame');
                    const debug = frame?.contentWindow?.__pqReconstructionDebug;
                    const state = debug && typeof debug.getLiveState === 'function' ? debug.getLiveState() : null;
                    return {
                        layout_viewer_present: Boolean(frame),
                        layout_viewer_ready: Boolean(document.querySelector('.layout-viewer-shell')?.classList.contains('is-ready')),
                        layout_viewer_active_route_index: Number(state?.activeRouteIndex ?? -1),
                        layout_viewer_metrics_ready: Boolean(state?.ready),
                    };
                }"""
            )
            or {}
        )
        if (
            last_snapshot.get("layout_viewer_ready") is True
            and last_snapshot.get("layout_viewer_metrics_ready") is True
            and int(last_snapshot.get("layout_viewer_active_route_index") or -1) == int(expected_active_route_index)
        ):
            return
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if remaining_ms <= 0:
            break
        page.wait_for_timeout(min(750, remaining_ms))
    raise TimeoutError(
        "generated_reconstruction_layout_viewer_active_route_timeout:"
        f"route_index={expected_active_route_index}:"
        f"snapshot={json.dumps(last_snapshot, ensure_ascii=True, sort_keys=True)[:800]}"
    )


_LAYOUT_VIEWER_PROOF_KEYS = (
    "layout_viewer_ready",
    "layout_viewer_metrics_source",
    "layout_viewer_route_button_count",
    "layout_viewer_floorplan_stop_count",
    "layout_viewer_metrics_ready",
    "layout_viewer_route_stop_count",
    "layout_viewer_active_route_index",
    "layout_viewer_view_mode",
    "layout_viewer_photo_panel_count",
    "layout_viewer_loaded_photo_texture_count",
    "layout_viewer_visible_photo_panel_count",
    "layout_viewer_cutaway_wall_count",
    "layout_viewer_hidden_cutaway_wall_count",
    "layout_viewer_wall_opacity",
    "layout_viewer_wall_height_scale",
    "layout_viewer_staging_object_count",
    "layout_viewer_visible_staging_object_count",
    "layout_viewer_projected_staging_coverage_pct",
    "layout_viewer_render_calls",
    "layout_viewer_render_triangles",
)


def _wait_for_generated_reconstruction_layout_preview_shell(page: Any, *, timeout: int = 45_000) -> None:
    page.wait_for_function(
        """() => {
            const shell = document.querySelector('.shell[data-launch-mode="layout_preview"]');
            const viewerShell = document.querySelector('.layout-viewer-shell');
            const frame = document.getElementById('layout-viewer-frame');
            const leadPreview = document.getElementById('lead-preview-image');
            return Boolean(shell && viewerShell && frame && leadPreview);
        }""",
        timeout=timeout,
    )


def _layout_preview_viewer_probe_from_launch(launch_shell: dict[str, object]) -> dict[str, object]:
    snapshot = {key: launch_shell.get(key) for key in _LAYOUT_VIEWER_PROOF_KEYS if key in launch_shell}
    if "layout_viewer_ready" not in snapshot:
        snapshot["layout_viewer_ready"] = False
    if "layout_viewer_metrics_ready" not in snapshot:
        snapshot["layout_viewer_metrics_ready"] = False
    snapshot["layout_viewer_present"] = True
    snapshot["layout_preview_viewer_proof_source"] = "launch_shell"
    return snapshot


def _extract_html_text(raw_html: str, pattern: str) -> str:
    match = re.search(pattern, raw_html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    value = re.sub(r"<[^>]+>", " ", str(match.group(1) or ""))
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


def _extract_html_attr(raw_html: str, pattern: str) -> str:
    match = re.search(pattern, raw_html, flags=re.IGNORECASE | re.DOTALL)
    return html_lib.unescape(str(match.group(1) or "").strip()) if match else ""


def _generated_reconstruction_layout_preview_snapshot_from_html(
    *,
    public_base_url: str,
    slug: str,
    host_header: str,
) -> dict[str, object]:
    normalized_base = str(public_base_url or "").strip().rstrip("/")
    encoded_slug = urllib.parse.quote(str(slug or "").strip(), safe="")
    url = f"{normalized_base}/tours/{encoded_slug}/layout-preview?browser_shell_probe=1"
    request = urllib.request.Request(url, headers={"User-Agent": "PropertyQuarry browser shell preview probe"})
    normalized_host = str(host_header or "").strip()
    if normalized_host:
        request.add_header("Host", normalized_host)
    with urllib.request.urlopen(request, timeout=45) as response:
        status_code = int(response.status)
        raw_html = response.read().decode("utf-8", errors="replace")
    media_grid_html = _extract_html_text(raw_html, r'(<[^>]+id=["\']media-grid["\'][\s\S]*?</(?:section|div)>)')
    return {
        "url": url,
        "status_code": status_code,
        "launch_mode": _extract_html_attr(raw_html, r'<[^>]+class=["\'][^"\']*\bshell\b[^"\']*["\'][^>]+data-launch-mode=["\']([^"\']+)["\']'),
        "heading_present": "hero-main" in raw_html and "eyebrow" in raw_html,
        "hero_eyebrow_text": _extract_html_text(raw_html, r'<[^>]+class=["\'][^"\']*\beyebrow\b[^"\']*["\'][^>]*>(.*?)</[^>]+>'),
        "generated_reconstruction_present": "generated reconstruction" in raw_html.lower(),
        "primary_cta_href": _extract_html_attr(raw_html, r'<a[^>]+class=["\'][^"\']*\bbtn\b[^"\']*\bprimary\b[^"\']*["\'][^>]+href=["\']([^"\']+)["\']'),
        "secondary_cta_href": _extract_html_attr(raw_html, r'<a[^>]+class=["\'][^"\']*\bbtn\b[^"\']*\bsecondary\b[^"\']*["\'][^>]+href=["\']([^"\']+)["\']'),
        "video_source": _extract_html_attr(raw_html, r'<source[^>]+src=["\']([^"\']+)["\']'),
        "lead_preview_src": _extract_html_attr(raw_html, r'<img[^>]+id=["\']lead-preview-image["\'][^>]+src=["\']([^"\']+)["\']'),
        "media_grid_present": 'id="media-grid"' in raw_html or "id='media-grid'" in raw_html,
        "media_card_count": len(re.findall(r'<[^>]+class=["\'][^"\']*\bmedia-card\b', raw_html, flags=re.IGNORECASE)),
        "route_action_count": len(re.findall(r'<[^>]+class=["\'][^"\']*\broute-action\b', raw_html, flags=re.IGNORECASE)),
        "media_role_count": len(re.findall(r'<[^>]+class=["\'][^"\']*\bmedia-role\b', raw_html, flags=re.IGNORECASE)),
        "media_grid_text": media_grid_html,
        "reference_focus_present": 'id="reference-shell"' in raw_html or "id='reference-shell'" in raw_html,
    }


def _generated_reconstruction_browser_shell_layout_failures(
    *,
    slug: str,
    launch_shell: dict[str, object],
    layout_preview: dict[str, object],
    expected_route_stop_count: int,
    expected_photo_count: int,
) -> list[str]:
    def _int_value(value: object, *, default: int = -1) -> int:
        try:
            if value is None or value == "":
                return default
            return int(value)
        except Exception:
            return default

    failures: list[str] = []
    encoded_slug = urllib.parse.quote(str(slug or "").strip(), safe="")
    expected_diorama_suffix = f"/tours/files/{encoded_slug}/diorama-preview.png"

    if not str(launch_shell.get("lead_preview_src") or "").strip().endswith(expected_diorama_suffix):
        failures.append("launch_shell_lead_preview_not_diorama")
    if launch_shell.get("layout_viewer_ready") is not True:
        failures.append("launch_shell_layout_viewer_not_ready")
    if int(launch_shell.get("layout_viewer_route_button_count") or 0) != expected_route_stop_count:
        failures.append("launch_shell_layout_viewer_route_button_count_wrong")
    if int(launch_shell.get("layout_viewer_floorplan_stop_count") or 0) != expected_route_stop_count:
        failures.append("launch_shell_layout_viewer_floorplan_stop_count_wrong")
    if int(launch_shell.get("layout_viewer_route_stop_count") or 0) != expected_route_stop_count:
        failures.append("launch_shell_layout_viewer_route_stop_count_wrong")
    if expected_photo_count > 0 and int(launch_shell.get("layout_viewer_photo_panel_count") or 0) != expected_photo_count:
        failures.append("launch_shell_layout_viewer_photo_panel_count_wrong")
    if expected_photo_count > 0 and int(launch_shell.get("layout_viewer_loaded_photo_texture_count") or 0) < expected_photo_count:
        failures.append("launch_shell_layout_viewer_photo_textures_incomplete")
    if expected_route_stop_count > 0 and _int_value(launch_shell.get("layout_viewer_active_route_index")) != 0:
        failures.append("launch_shell_layout_viewer_initial_route_wrong")
    failures.extend(
        _generated_reconstruction_layout_viewer_quality_failures(
            prefix="launch_shell",
            snapshot=launch_shell,
            expected_route_stop_count=expected_route_stop_count,
            require_cutaway_view=str(launch_shell.get("layout_viewer_view_mode") or "").strip().lower()
            in {"overview", "dollhouse"},
        )
    )
    if (
        expected_route_stop_count > 1
        and "layout_viewer_active_route_index_after_route_click" in launch_shell
        and _int_value(launch_shell.get("layout_viewer_active_route_index_after_route_click")) != 1
    ):
        failures.append("launch_shell_layout_viewer_route_click_sync_wrong")
    if (
        expected_route_stop_count > 2
        and "layout_viewer_active_route_index_after_last_route_click" in launch_shell
        and _int_value(launch_shell.get("layout_viewer_active_route_index_after_last_route_click")) != expected_route_stop_count - 1
    ):
        failures.append("launch_shell_layout_viewer_last_route_sync_wrong")
    if (
        expected_route_stop_count > 2
        and "layout_viewer_active_route_index_after_timeupdate_sync" in launch_shell
        and _int_value(launch_shell.get("layout_viewer_active_route_index_after_timeupdate_sync")) != expected_route_stop_count - 1
    ):
        failures.append("launch_shell_layout_viewer_timeupdate_sync_wrong")

    if not str(layout_preview.get("lead_preview_src") or "").strip().endswith(expected_diorama_suffix):
        failures.append("layout_preview_lead_preview_not_diorama")
    if layout_preview.get("layout_viewer_ready") is not True:
        failures.append("layout_preview_layout_viewer_not_ready")
    if int(layout_preview.get("layout_viewer_route_button_count") or 0) != expected_route_stop_count:
        failures.append("layout_preview_layout_viewer_route_button_count_wrong")
    if int(layout_preview.get("layout_viewer_floorplan_stop_count") or 0) != expected_route_stop_count:
        failures.append("layout_preview_layout_viewer_floorplan_stop_count_wrong")
    if int(layout_preview.get("layout_viewer_route_stop_count") or 0) != expected_route_stop_count:
        failures.append("layout_preview_layout_viewer_route_stop_count_wrong")
    if expected_photo_count > 0 and int(layout_preview.get("layout_viewer_photo_panel_count") or 0) != expected_photo_count:
        failures.append("layout_preview_layout_viewer_photo_panel_count_wrong")
    if expected_photo_count > 0 and int(layout_preview.get("layout_viewer_loaded_photo_texture_count") or 0) < expected_photo_count:
        failures.append("layout_preview_layout_viewer_photo_textures_incomplete")
    if expected_route_stop_count > 0 and _int_value(layout_preview.get("layout_viewer_active_route_index")) != 0:
        failures.append("layout_preview_layout_viewer_initial_route_wrong")
    failures.extend(
        _generated_reconstruction_layout_viewer_quality_failures(
            prefix="layout_preview",
            snapshot=layout_preview,
            expected_route_stop_count=expected_route_stop_count,
            require_cutaway_view=str(layout_preview.get("layout_viewer_view_mode") or "").strip().lower()
            in {"overview", "dollhouse"},
        )
    )
    return failures


def _normalized_shell_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _generated_reconstruction_shell_variant_failures(
    *,
    shell_name: str,
    snapshot: dict[str, object],
) -> list[str]:
    contracts = {
        "launch_shell": {
            "launch_mode": "tour_public_launch",
            "hero_eyebrow_text": "propertyquarry styled 3d reconstruction",
            "primary_cta_href": "#layout-viewer",
            "secondary_cta_href": "#walkthrough",
            "heading_failure": "launch_shell_missing_heading",
        },
        "layout_preview": {
            "launch_mode": "layout_preview",
            "hero_eyebrow_text": "propertyquarry styled 3d reconstruction",
            "primary_cta_href": "#layout-viewer",
            "secondary_cta_href": "#walkthrough",
            "heading_failure": "layout_preview_heading_wrong",
        },
    }
    contract = contracts.get(str(shell_name or "").strip())
    if contract is None:
        raise ValueError(f"unsupported_shell_variant:{shell_name}")
    failures: list[str] = []
    if str(snapshot.get("launch_mode") or "").strip() != str(contract.get("launch_mode") or ""):
        failures.append(f"{shell_name}_launch_mode_wrong")
    if _normalized_shell_text(snapshot.get("hero_eyebrow_text")) != str(contract.get("hero_eyebrow_text") or ""):
        failures.append(str(contract.get("heading_failure") or f"{shell_name}_heading_wrong"))
    if str(snapshot.get("primary_cta_href") or "").strip() != str(contract.get("primary_cta_href") or ""):
        failures.append(f"{shell_name}_primary_cta_wrong")
    if str(snapshot.get("secondary_cta_href") or "").strip() != str(contract.get("secondary_cta_href") or ""):
        failures.append(f"{shell_name}_secondary_cta_wrong")
    return failures


def _check_generated_reconstruction_browser_shell(
    *,
    public_base_url: str,
    slug: str,
    host_header: str,
    expected_route_stop_count: int,
    expected_photo_count: int,
    expected_route_labels: list[str] | None = None,
) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - environment guard
        return {
            "status": "failed",
            "reason": "playwright_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }

    browser_base_url, browser_args = _browser_url_and_args(public_base_url, host_header)
    encoded_slug = urllib.parse.quote(str(slug or "").strip(), safe="")
    launch_url = f"{browser_base_url}/tours/{encoded_slug}?browser_shell_probe=1"
    layout_preview_url = f"{browser_base_url}/tours/{encoded_slug}/layout-preview?browser_shell_probe=1"
    console_errors: list[str] = []
    page_errors: list[str] = []
    launch_shell: dict[str, object] = {"url": launch_url}
    layout_preview: dict[str, object] = {"url": layout_preview_url}
    failures: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            ignore_https_errors=True,
        )
        try:
            page = context.new_page()
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            def _shell_snapshot(target_page: Any) -> dict[str, object]:
                return dict(
                    target_page.evaluate(
                        """() => {
                            const text = (selector) => {
                                const node = document.querySelector(selector);
                                return node && typeof node.textContent === 'string' ? node.textContent.trim() : '';
                            };
                            const attr = (selector, name) => {
                                const node = document.querySelector(selector);
                                return node ? (node.getAttribute(name) || '') : '';
                            };
                            const cards = Array.from(document.querySelectorAll('#media-grid .media-card'));
                            const routeActions = Array.from(document.querySelectorAll('.route-action'));
                            const mediaRoles = Array.from(document.querySelectorAll('#media-grid .media-role'));
                            const mediaGrid = document.querySelector('#media-grid');
                            const shellText = [
                                text('.hero-main'),
                                text('.disclosure'),
                                text('#layout-viewer'),
                                text('#reference-focus'),
                            ].join(' ');
                            return {
                                launch_mode: attr('.shell', 'data-launch-mode'),
                                heading_present: Boolean(text('.hero-main .eyebrow')),
                                hero_eyebrow_text: text('.hero-main .eyebrow'),
                                generated_reconstruction_present: shellText.toLowerCase().includes('generated reconstruction'),
                                primary_cta_href: attr('.btn.primary', 'href'),
                                secondary_cta_href: attr('.btn.secondary', 'href'),
                                video_source: attr('#tour-video source', 'src'),
                                media_grid_present: Boolean(mediaGrid),
                                media_card_count: cards.length,
                                route_action_count: routeActions.length,
                                media_role_count: mediaRoles.length,
                                media_grid_text: mediaGrid && typeof mediaGrid.textContent === 'string' ? mediaGrid.textContent.trim() : '',
                                reference_floorplan_href: cards.length ? (cards[0].getAttribute('data-target') || '') : '',
                                reference_focus_present: Boolean(document.querySelector('#reference-shell')),
                                reference_focus_name: text('#reference-focus-name'),
                                reference_focus_href: attr('#reference-focus-open', 'href'),
                                walkthrough_stop_name: text('#walkthrough-stop-name'),
                                walkthrough_stop_position: text('#walkthrough-stop-position'),
                                walkthrough_stop_mode: text('#walkthrough-stop-mode'),
                            };
                        }"""
                    )
                    or {}
                )

            def _click_selector_index(target_page: Any, selector: str, index: int) -> dict[str, object]:
                return dict(
                    target_page.evaluate(
                        """({ selector, index }) => {
                            const nodes = Array.from(document.querySelectorAll(selector));
                            const resolvedIndex = Number.isFinite(Number(index)) ? Number(index) : -1;
                            const node = nodes[resolvedIndex];
                            if (!node) {
                                return {
                                    clicked: false,
                                    reason: 'node_missing',
                                    selector,
                                    index: resolvedIndex,
                                    count: nodes.length,
                                };
                            }
                            const before = node.getAttribute('data-route-index') || node.getAttribute('data-target') || '';
                            window.setTimeout(() => {
                                try {
                                    if (typeof node.click === 'function') {
                                        node.click();
                                    } else {
                                        node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                                    }
                                } catch (_error) {
                                    // The state wait after this scheduled click records the observable failure.
                                }
                            }, 0);
                            return {
                                clicked: true,
                                scheduled: true,
                                selector,
                                index: resolvedIndex,
                                count: nodes.length,
                                marker: before,
                            };
                        }""",
                        {"selector": selector, "index": index},
                    )
                    or {}
                )

            _record_browser_shell_progress("launch_goto_start", url=launch_url)
            launch_response = page.goto(launch_url, wait_until="commit", timeout=45_000)
            _record_browser_shell_progress("launch_goto_done", status_code=int(launch_response.status) if launch_response is not None else 0)
            _stabilize_generated_reconstruction_viewer_bootstrap(page)
            _record_browser_shell_progress("launch_wait_viewer_start")
            launch_viewer_snapshot = _wait_for_generated_reconstruction_layout_viewer_state(
                page,
                expected_route_stop_count=expected_route_stop_count,
                expected_photo_count=expected_photo_count,
                expected_active_route_index=None,
                timeout=45_000,
            )
            _record_browser_shell_progress("launch_wait_viewer_done")
            _freeze_tour_video_at_start(page)
            if expected_route_stop_count > 0:
                _record_browser_shell_progress("launch_wait_initial_route_start")
                launch_viewer_snapshot = _wait_for_generated_reconstruction_layout_viewer_state(
                    page,
                    expected_route_stop_count=expected_route_stop_count,
                    expected_photo_count=expected_photo_count,
                    expected_active_route_index=0,
                    timeout=20_000,
                )
                _record_browser_shell_progress("launch_wait_initial_route_done")
            launch_status_code = int(launch_response.status) if launch_response is not None else 0
            _record_browser_shell_progress("launch_snapshot_start")
            _record_browser_shell_progress("launch_shell_snapshot_start")
            launch_shell_snapshot = _shell_snapshot(page)
            _record_browser_shell_progress("launch_shell_snapshot_done")
            launch_shell = {
                "url": launch_url,
                "status_code": launch_status_code,
                **launch_shell_snapshot,
                **launch_viewer_snapshot,
            }
            _record_browser_shell_progress("launch_snapshot_done")
            media_card_count = int(launch_shell.get("media_card_count") or 0)
            route_action_count = int(launch_shell.get("route_action_count") or 0)
            if media_card_count:
                launch_shell["reference_focus_name_after_click"] = str(launch_shell.get("reference_focus_name") or "")
                launch_shell["walkthrough_stop_name_after_click"] = str(launch_shell.get("walkthrough_stop_name") or "")
            if route_action_count > 1:
                _record_browser_shell_progress("launch_route_click_start", route_index=1)
                launch_shell["route_click_result"] = _click_selector_index(page, ".route-action", 1)
                _record_browser_shell_progress("launch_route_click_scheduled", route_index=1)
                page.wait_for_timeout(1_250)
                after_route_click_snapshot = {
                    **_shell_snapshot(page),
                    "layout_viewer_active_route_index": page.evaluate(
                        """() => {
                            const active = document.querySelector('.route-action.is-active');
                            return active ? Number(active.getAttribute('data-route-index') || 0) : -1;
                        }"""
                    ),
                }
                launch_shell["reference_focus_name_after_route_click"] = str(after_route_click_snapshot.get("reference_focus_name") or "")
                launch_shell["reference_focus_href_after_route_click"] = str(after_route_click_snapshot.get("reference_focus_href") or "")
                launch_shell["walkthrough_stop_name_after_route_click"] = str(after_route_click_snapshot.get("walkthrough_stop_name") or "")
                launch_shell["walkthrough_stop_position_after_route_click"] = str(after_route_click_snapshot.get("walkthrough_stop_position") or "")
                launch_shell["walkthrough_stop_mode_after_route_click"] = str(after_route_click_snapshot.get("walkthrough_stop_mode") or "")
                after_route_click_viewer_index = int(after_route_click_snapshot.get("layout_viewer_active_route_index") or -1)
                if after_route_click_viewer_index >= 0:
                    launch_shell["layout_viewer_active_route_index_after_route_click"] = after_route_click_viewer_index
                _record_browser_shell_progress("launch_route_click_done", route_index=1)
            if route_action_count:
                last_index = route_action_count - 1
                if last_index > 1:
                    _record_browser_shell_progress("launch_last_route_click_start", route_index=last_index)
                    launch_shell["last_route_click_result"] = _click_selector_index(page, ".route-action", last_index)
                    _record_browser_shell_progress("launch_last_route_click_scheduled", route_index=last_index)
                    page.wait_for_timeout(750)
                    after_last_route_click_snapshot = _shell_snapshot(page)
                    launch_shell["reference_focus_name_after_last_route_click"] = str(after_last_route_click_snapshot.get("reference_focus_name") or "")
                    launch_shell["reference_focus_href_after_last_route_click"] = str(after_last_route_click_snapshot.get("reference_focus_href") or "")
                    launch_shell["walkthrough_stop_name_after_last_route_click"] = str(after_last_route_click_snapshot.get("walkthrough_stop_name") or "")
                    launch_shell["walkthrough_stop_position_after_last_route_click"] = str(after_last_route_click_snapshot.get("walkthrough_stop_position") or "")
                    launch_shell["walkthrough_stop_mode_after_last_route_click"] = str(after_last_route_click_snapshot.get("walkthrough_stop_mode") or "")
                    _record_browser_shell_progress("launch_last_route_click_done", route_index=last_index)
                    timeupdate_synced = page.evaluate(
                        """() => {
                            const video = document.getElementById('tour-video');
                            const routes = Array.from(document.querySelectorAll('.route-action'));
                            const last = routes[routes.length - 1];
                            if (!video || !last) return false;
                            if (Number(video.readyState || 0) < 2 || video.seeking) return false;
                            const start = Number(last.getAttribute('data-seek-start') || 0);
                            video.currentTime = Number.isFinite(start) ? start : 0;
                            video.dispatchEvent(new Event('seeking'));
                            video.dispatchEvent(new Event('timeupdate'));
                            return true;
                        }"""
                    )
                    if timeupdate_synced:
                        after_timeupdate_snapshot = _shell_snapshot(page)
                        launch_shell["reference_focus_name_after_timeupdate_sync"] = str(after_timeupdate_snapshot.get("reference_focus_name") or "")
                        launch_shell["reference_focus_href_after_timeupdate_sync"] = str(after_timeupdate_snapshot.get("reference_focus_href") or "")
                        launch_shell["walkthrough_stop_name_after_timeupdate_sync"] = str(after_timeupdate_snapshot.get("walkthrough_stop_name") or "")
                        launch_shell["walkthrough_stop_position_after_timeupdate_sync"] = str(after_timeupdate_snapshot.get("walkthrough_stop_position") or "")
                        launch_shell["walkthrough_stop_mode_after_timeupdate_sync"] = str(after_timeupdate_snapshot.get("walkthrough_stop_mode") or "")
                        launch_shell["active_route_index_after_timeupdate_sync"] = page.evaluate(
                            """() => {
                                const active = document.querySelector('.route-action.is-active');
                                return active ? Number(active.getAttribute('data-route-index') || 0) : -1;
                            }"""
                        )
            _play_tour_video_without_waiting(page)
            page.wait_for_timeout(1_800)
            _record_browser_shell_progress("launch_video_state_start")
            launch_shell["video_state"] = page.evaluate(
                """() => {
                    const video = document.getElementById('flythrough-video') || document.getElementById('tour-video');
                    return video ? {
                        currentTime: Number(video.currentTime || 0),
                        duration: Number(video.duration || 0),
                        readyState: Number(video.readyState || 0),
                        videoWidth: Number(video.videoWidth || 0),
                    } : null;
                }"""
            )
            _record_browser_shell_progress("launch_video_state_done")
            try:
                page.close()
            except Exception:
                pass

            _record_browser_shell_progress("preview_http_snapshot_start", url=layout_preview_url)
            layout_preview_snapshot = _generated_reconstruction_layout_preview_snapshot_from_html(
                public_base_url=public_base_url,
                slug=slug,
                host_header=host_header,
            )
            _record_browser_shell_progress(
                "preview_http_snapshot_done",
                status_code=int(layout_preview_snapshot.get("status_code") or 0),
            )
            layout_preview = {
                "url": layout_preview_url,
                **layout_preview_snapshot,
                **_layout_preview_viewer_probe_from_launch(launch_shell),
            }
        except Exception as exc:
            failures.append("browser_shell_probe_failed")
            return {
                "status": "failed",
                "reason": "browser_shell_probe_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "browser_base_url": browser_base_url,
                "launch_shell": launch_shell,
                "layout_preview": layout_preview,
                "console_errors": _unexpected_console_errors(console_errors),
                "page_errors": page_errors,
                "failures": failures,
            }
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    launch_shell_errors = _unexpected_console_errors(console_errors)
    video_state = dict(launch_shell.get("video_state") or {}) if isinstance(launch_shell.get("video_state"), dict) else {}
    if int(launch_shell.get("status_code") or 0) != 200:
        failures.append("launch_shell_not_ok")
    if launch_shell.get("generated_reconstruction_present") is not True:
        failures.append("launch_shell_missing_generated_reconstruction_label")
    if not str(launch_shell.get("video_source") or "").endswith(f"/tours/{slug}/walkthrough"):
        failures.append("launch_shell_video_source_wrong")
    if launch_shell.get("media_grid_present") is not True:
        failures.append("launch_shell_media_grid_missing")
    if int(launch_shell.get("route_action_count") or 0) != expected_route_stop_count:
        failures.append("launch_shell_route_action_count_wrong")
    if launch_shell.get("reference_focus_present") is not True:
        failures.append("launch_shell_reference_focus_missing")
    if int(launch_shell.get("media_role_count") or 0) != 0:
        failures.append("launch_shell_media_role_badges_present")
    if "map" in str(launch_shell.get("media_grid_text") or "").lower():
        failures.append("launch_shell_media_grid_map_label_present")
    if not str(launch_shell.get("reference_focus_name") or "").strip():
        failures.append("launch_shell_reference_focus_name_missing")
    if not str(launch_shell.get("walkthrough_stop_name") or "").strip():
        failures.append("launch_shell_walkthrough_stop_name_missing")
    if not str(launch_shell.get("walkthrough_stop_position") or "").strip():
        failures.append("launch_shell_walkthrough_stop_position_missing")
    if not str(launch_shell.get("walkthrough_stop_mode") or "").strip():
        failures.append("launch_shell_walkthrough_stop_mode_missing")
    if not str(launch_shell.get("reference_focus_name_after_click") or "").strip():
        failures.append("launch_shell_reference_focus_not_interactive")
    if expected_route_stop_count > 1 and not str(launch_shell.get("reference_focus_name_after_route_click") or "").strip():
        failures.append("launch_shell_route_actions_not_interactive")
    normalized_route_labels = [str(label or "").strip() for label in list(expected_route_labels or []) if str(label or "").strip()]
    normalized_walkthrough_position = str(launch_shell.get("walkthrough_stop_position") or "").strip().lower()
    normalized_walkthrough_mode = str(launch_shell.get("walkthrough_stop_mode") or "").strip().lower()
    normalized_walkthrough_position_after_route_click = str(launch_shell.get("walkthrough_stop_position_after_route_click") or "").strip().lower()
    normalized_walkthrough_mode_after_route_click = str(launch_shell.get("walkthrough_stop_mode_after_route_click") or "").strip().lower()
    normalized_walkthrough_position_after_last_route_click = str(launch_shell.get("walkthrough_stop_position_after_last_route_click") or "").strip().lower()
    normalized_walkthrough_mode_after_last_route_click = str(launch_shell.get("walkthrough_stop_mode_after_last_route_click") or "").strip().lower()
    timeupdate_sync_available = "active_route_index_after_timeupdate_sync" in launch_shell
    normalized_walkthrough_position_after_timeupdate_sync = str(launch_shell.get("walkthrough_stop_position_after_timeupdate_sync") or "").strip().lower()
    normalized_walkthrough_mode_after_timeupdate_sync = str(launch_shell.get("walkthrough_stop_mode_after_timeupdate_sync") or "").strip().lower()
    if normalized_route_labels and str(launch_shell.get("reference_focus_name") or "").strip() != normalized_route_labels[0]:
        failures.append("launch_shell_initial_route_focus_wrong")
    if normalized_route_labels and str(launch_shell.get("walkthrough_stop_name") or "").strip() != normalized_route_labels[0]:
        failures.append("launch_shell_initial_walkthrough_stop_wrong")
    if expected_route_stop_count and normalized_walkthrough_position != f"stop 1 / {expected_route_stop_count}":
        failures.append("launch_shell_initial_walkthrough_position_wrong")
    if expected_photo_count > 0 and normalized_walkthrough_mode != "photo cue":
        failures.append("launch_shell_initial_walkthrough_mode_wrong")
    if len(normalized_route_labels) > 1 and str(launch_shell.get("reference_focus_name_after_route_click") or "").strip() != normalized_route_labels[1]:
        failures.append("launch_shell_route_action_focus_wrong")
    if len(normalized_route_labels) > 1 and str(launch_shell.get("walkthrough_stop_name_after_route_click") or "").strip() != normalized_route_labels[1]:
        failures.append("launch_shell_route_action_walkthrough_stop_wrong")
    if len(normalized_route_labels) > 1 and normalized_walkthrough_position_after_route_click != f"stop 2 / {expected_route_stop_count}":
        failures.append("launch_shell_route_action_walkthrough_position_wrong")
    if len(normalized_route_labels) > 1 and expected_photo_count > 1 and normalized_walkthrough_mode_after_route_click != "photo cue":
        failures.append("launch_shell_route_action_walkthrough_mode_wrong")
    if len(normalized_route_labels) > expected_photo_count:
        if str(launch_shell.get("reference_focus_name_after_last_route_click") or "").strip() != normalized_route_labels[-1]:
            failures.append("launch_shell_route_overflow_focus_label_wrong")
        if str(launch_shell.get("reference_focus_href_after_last_route_click") or "").strip() != str(launch_shell.get("reference_floorplan_href") or "").strip():
            failures.append("launch_shell_route_overflow_not_falling_back_to_floorplan")
        if str(launch_shell.get("walkthrough_stop_name_after_last_route_click") or "").strip() != normalized_route_labels[-1]:
            failures.append("launch_shell_route_overflow_walkthrough_stop_wrong")
        if normalized_walkthrough_position_after_last_route_click != f"stop {len(normalized_route_labels)} / {expected_route_stop_count}":
            failures.append("launch_shell_route_overflow_walkthrough_position_wrong")
        if normalized_walkthrough_mode_after_last_route_click != "floorplan cue":
            failures.append("launch_shell_route_overflow_walkthrough_mode_wrong")
        if timeupdate_sync_available:
            if str(launch_shell.get("reference_focus_name_after_timeupdate_sync") or "").strip() != normalized_route_labels[-1]:
                failures.append("launch_shell_timeupdate_sync_focus_label_wrong")
            if str(launch_shell.get("reference_focus_href_after_timeupdate_sync") or "").strip() != str(launch_shell.get("reference_floorplan_href") or "").strip():
                failures.append("launch_shell_timeupdate_sync_not_falling_back_to_floorplan")
            if str(launch_shell.get("walkthrough_stop_name_after_timeupdate_sync") or "").strip() != normalized_route_labels[-1]:
                failures.append("launch_shell_timeupdate_sync_walkthrough_stop_wrong")
            if normalized_walkthrough_position_after_timeupdate_sync != f"stop {len(normalized_route_labels)} / {expected_route_stop_count}":
                failures.append("launch_shell_timeupdate_sync_walkthrough_position_wrong")
            if normalized_walkthrough_mode_after_timeupdate_sync != "floorplan cue":
                failures.append("launch_shell_timeupdate_sync_walkthrough_mode_wrong")
    if normalized_route_labels and timeupdate_sync_available:
        if int(launch_shell.get("active_route_index_after_timeupdate_sync") or -1) != len(normalized_route_labels) - 1:
            failures.append("launch_shell_timeupdate_sync_route_wrong")
    if video_state:
        if float(video_state.get("currentTime") or 0) <= 0.2:
            failures.append("launch_shell_video_not_advancing")
        if float(video_state.get("duration") or 0) < 20.0:
            failures.append("launch_shell_video_duration_short")
        if int(video_state.get("readyState") or 0) < 1:
            failures.append("launch_shell_video_not_ready")
        if int(video_state.get("videoWidth") or 0) < 640:
            failures.append("launch_shell_video_width_small")
    else:
        failures.append("launch_shell_video_state_missing")
    if int(layout_preview.get("status_code") or 0) != 200:
        failures.append("layout_preview_not_ok")
    if layout_preview.get("generated_reconstruction_present") is not True:
        failures.append("layout_preview_missing_generated_reconstruction_label")
    if not str(layout_preview.get("video_source") or "").endswith(f"/tours/{slug}/walkthrough"):
        failures.append("layout_preview_video_source_wrong")
    if int(layout_preview.get("route_action_count") or 0) != expected_route_stop_count:
        failures.append("layout_preview_route_action_count_wrong")
    if int(layout_preview.get("media_card_count") or 0) < expected_photo_count:
        failures.append("layout_preview_media_cards_incomplete")
    if layout_preview.get("reference_focus_present") is not True:
        failures.append("layout_preview_reference_focus_missing")
    failures.extend(
        _generated_reconstruction_shell_variant_failures(
            shell_name="launch_shell",
            snapshot=launch_shell,
        )
    )
    failures.extend(
        _generated_reconstruction_shell_variant_failures(
            shell_name="layout_preview",
            snapshot=layout_preview,
        )
    )
    failures.extend(
        _generated_reconstruction_browser_shell_layout_failures(
            slug=slug,
            launch_shell=launch_shell,
            layout_preview=layout_preview,
            expected_route_stop_count=expected_route_stop_count,
            expected_photo_count=expected_photo_count,
        )
    )
    if launch_shell_errors:
        failures.append("browser_shell_console_errors_present")
    if page_errors:
        failures.append("browser_shell_page_errors_present")
    return {
        "status": "pass" if not failures else "failed",
        "browser_base_url": browser_base_url,
        "launch_shell": launch_shell,
        "layout_preview": layout_preview,
        "console_errors": launch_shell_errors,
        "page_errors": page_errors,
        "failures": failures,
    }


def _label_list(value: object) -> list[str]:
    return [str(item).strip() for item in list(value or []) if str(item).strip()]


def _looks_like_generic_route_label(value: object) -> bool:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return bool(normalized) and bool(re.fullmatch(r"room stop(?: \d+)?", normalized))


def _labels_contain_keyword(labels: list[str], pattern: str) -> bool:
    regex = re.compile(pattern, flags=re.IGNORECASE)
    return any(regex.search(label) for label in labels)


def _coverage_proof_covers_walkthrough_route(
    coverage_proof: dict[str, object],
    walkthrough_route_labels: list[str],
) -> bool:
    expected = _label_list(coverage_proof.get("segments_expected") or coverage_proof.get("expected_segments"))
    visited = _label_list(coverage_proof.get("segments_visited") or coverage_proof.get("visited_segments"))
    segments = [
        dict(row)
        for row in list(
            coverage_proof.get("coverage_segments")
            or coverage_proof.get("segments")
            or coverage_proof.get("room_segments")
            or []
        )
        if isinstance(row, dict)
    ]
    segment_labels = _label_list(
        row.get("segment") or row.get("label") or row.get("room") or row.get("name")
        for row in segments
    )
    return (
        str(coverage_proof.get("status") or "").strip().lower() == "pass"
        and bool(walkthrough_route_labels)
        and expected == walkthrough_route_labels
        and visited == walkthrough_route_labels
        and segment_labels == walkthrough_route_labels
        and len(segments) >= len(walkthrough_route_labels)
    )


def _check_generated_reconstruction_public_contract(
    *,
    public_base_url: str,
    slug: str,
    host_header: str = "",
) -> dict[str, object]:
    viewer_url = _generated_reconstruction_viewer_url(public_base_url=public_base_url, slug=slug)
    canonical_url = _generated_reconstruction_canonical_url(public_base_url=public_base_url, slug=slug)
    model_url = _generated_reconstruction_model_url(public_base_url=public_base_url, slug=slug)
    payload_url = _generated_reconstruction_payload_url(public_base_url=public_base_url, slug=slug)
    if not viewer_url or not canonical_url or not model_url or not payload_url:
        return {"status": "skipped", "reason": "public_base_url_missing"}

    viewer = _http_probe(viewer_url, host_header=host_header)
    canonical = _http_probe(canonical_url, host_header=host_header)
    public_payload = _http_probe(payload_url, host_header=host_header)
    model = _http_probe(model_url, host_header=host_header)
    expected_control_prefix = f"{urllib.parse.urlparse(canonical_url).path}/control/"
    failures: list[str] = []
    viewer_status = int(viewer.get("status_code") or 0)
    viewer_location = str(viewer.get("location") or "")
    viewer_redirect_path = urllib.parse.urlparse(viewer_location).path if viewer_location else ""
    viewer_body_lower = str(viewer.get("body_excerpt") or "").lower()
    canonical_path = urllib.parse.urlparse(canonical_url).path
    if viewer_status in {302, 307}:
        if not (viewer_redirect_path.startswith(expected_control_prefix) or viewer_redirect_path == canonical_path):
            failures.append("viewer_redirect_target_wrong")
    elif viewer_status == 200:
        if (
            "styled 3d reconstruction | propertyquarry" not in viewer_body_lower
            or "id=\"viewport\"" not in viewer_body_lower
        ):
            failures.append("viewer_not_routed_to_clean_shell")
    else:
        failures.append("viewer_not_routed_to_clean_shell")
    canonical_body = str(canonical.get("body_excerpt") or "")
    canonical_status = int(canonical.get("status_code") or 0)
    canonical_location = str(canonical.get("location") or "")
    canonical_redirect_path = urllib.parse.urlparse(canonical_location).path if canonical_location else ""
    if canonical_status in {302, 307}:
        if not canonical_redirect_path.startswith(expected_control_prefix):
            failures.append("canonical_redirect_target_wrong")
    elif canonical_status == 200:
        canonical_body_lower = canonical_body.lower()
        if "propertyquarry styled 3d reconstruction" not in canonical_body_lower:
            failures.append("canonical_missing_shell_heading")
        if "generated reconstruction" not in canonical_body_lower:
            failures.append("canonical_missing_generated_reconstruction_disclosure")
        if "tour unavailable" in canonical_body_lower:
            failures.append("canonical_leaks_unavailable_copy")
    else:
        failures.append("canonical_not_shell_or_control")
    canonical_body_raw = str(canonical.get("body_excerpt") or "")
    canonical_body_lower = canonical_body_raw.lower()
    if "generated-reconstruction/viewer.html" in canonical_body_raw and "layout-viewer-shell" not in canonical_body_lower:
        failures.append("canonical_leaks_fake_viewer_url")
    model_status = int(model.get("status_code") or 0)
    model_body = str(model.get("body_excerpt") or "")
    if model_status == 200:
        if (
            "o propertyquarry_generated_layout" not in model_body
            or "mtllib model.mtl" not in model_body
            or "\nv " not in model_body
            or "\nf " not in model_body
        ):
            failures.append("model_not_public_generated_layout")
    elif model_status != 410:
        failures.append("model_not_public_generated_layout")
    private_key_markers = {
        "principal_id",
        "listing_url",
        "property_url",
        "source_ref",
        "external_id",
        "recipient_email",
        "exact_address",
        "address_lines",
        "map_lat",
        "map_lng",
    }
    private_value_markers = (
        "owner@example.test",
        "willhaben:runtime-reconstruction-smoke",
        "runtime-reconstruction-smoke:",
    )
    public_payload_body = str(public_payload.get("body_excerpt") or "")
    public_payload_status = int(public_payload.get("status_code") or 0)
    if public_payload_status != 200:
        failures.append("public_payload_not_ok")
    leaked_markers: list[str] = []
    if public_payload_status == 200:
        try:
            public_payload_json = json.loads(public_payload_body)
        except Exception:
            public_payload_json = None
            failures.append("public_payload_invalid_json")

        def _collect_private_payload_markers(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized_key = str(key or "").strip().lower()
                    if normalized_key in private_key_markers:
                        leaked_markers.append(normalized_key)
                    _collect_private_payload_markers(child)
            elif isinstance(value, list):
                for child in value:
                    _collect_private_payload_markers(child)
            elif isinstance(value, str):
                normalized_value = value.lower()
                for marker in private_value_markers:
                    if marker in normalized_value:
                        leaked_markers.append(marker)

        if public_payload_json is not None:
            _collect_private_payload_markers(public_payload_json)
    canonical_body_lower = canonical_body_raw.lower()
    leaked_markers.extend(
        marker for marker in private_value_markers if marker in canonical_body_lower
    )
    leaked_markers = sorted(set(leaked_markers))
    if leaked_markers:
        failures.append("public_payload_private_markers_present")
    return {
        "status": "pass" if not failures else "failed",
        "failures": failures,
        "viewer_url": viewer_url,
        "canonical_url": canonical_url,
        "payload_url": payload_url,
        "model_url": model_url,
        "host_header": str(host_header or "").strip(),
        "viewer": viewer,
        "canonical": canonical,
        "public_payload": public_payload,
        "model": model,
        "private_marker_leaks": leaked_markers,
    }


def build_runtime_reconstruction_receipt(
    *,
    container: str,
    slug: str,
    public_base_url: str = "",
    require_browser: bool = False,
    require_public_contract: bool = False,
    require_browser_shell: bool = False,
    host_header: str = "",
    require_glb: bool = False,
    ensure_render_bridge_runtime: bool = True,
) -> dict[str, object]:
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if not shutil.which("docker"):
        return {"status": "blocked", "reason": "docker_missing", "generated_at": started_at}
    render_bridge_runtime: dict[str, object] = {"status": "skipped", "reason": "render_bridge_runtime_ensure_disabled"}
    if ensure_render_bridge_runtime:
        render_bridge_runtime = build_render_bridge_runtime_receipt(
            container=str(
                os.getenv("PROPERTYQUARRY_RENDER_CONTAINER_NAME")
                or DEFAULT_RENDER_CONTAINER
            ).strip(),
            service=str(
                os.getenv("PROPERTYQUARRY_RENDER_SERVICE")
                or DEFAULT_RENDER_CONTAINER
            ).strip(),
            compose_file=str(os.getenv("PROPERTYQUARRY_COMPOSE_FILE") or "docker-compose.property.yml").strip(),
            compose_project_name=(
                str(os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or os.getenv("COMPOSE_PROJECT_NAME") or "").strip()
            ),
        )
        if render_bridge_runtime.get("status") != "pass":
            return {
                "status": "blocked" if render_bridge_runtime.get("status") == "blocked" else "failed",
                "reason": "render_bridge_runtime_unavailable",
                "generated_at": started_at,
                "container": container,
                "slug": slug,
                "render_bridge_runtime": render_bridge_runtime,
            }

    generation_timeout_seconds = _runtime_reconstruction_generation_timeout_seconds(container)
    setup_script = f"""
import json
import os
from pathlib import Path
import shutil
from PIL import Image, ImageDraw
from app.product import service as product_service

slug = {slug!r}
src = Path('/tmp') / f'propertyquarry-runtime-reconstruction-{{slug}}'
shutil.rmtree(Path('/data/public_property_tours') / slug, ignore_errors=True)
shutil.rmtree(src, ignore_errors=True)
src.mkdir(parents=True, exist_ok=True)
title = f'Runtime reconstruction smoke {{slug}}'
listing_id = f'runtime-reconstruction-smoke-{{slug}}'
property_url = (
    'https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/'
    f'runtime-reconstruction-smoke-{{slug}}'
)
source_ref = f'willhaben:runtime-reconstruction-smoke:{{slug}}'
external_id = f'runtime-reconstruction-smoke:{{slug}}'

def write_floorplan(path: Path) -> None:
    image = Image.new('RGB', (1200, 800), color=(248, 244, 235))
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 1120, 720), outline=(42, 36, 28), width=12)
    draw.line((620, 80, 620, 720), fill=(42, 36, 28), width=8)
    draw.line((80, 420, 620, 420), fill=(42, 36, 28), width=8)
    draw.line((320, 80, 320, 420), fill=(42, 36, 28), width=8)
    draw.line((620, 250, 1120, 250), fill=(42, 36, 28), width=8)
    draw.line((870, 250, 870, 720), fill=(42, 36, 28), width=8)
    draw.rectangle((118, 118, 282, 382), outline=(73, 108, 170), width=6)
    draw.rectangle((358, 118, 582, 382), outline=(148, 68, 48), width=6)
    draw.rectangle((666, 118, 826, 212), outline=(73, 108, 170), width=6)
    draw.rectangle((910, 296, 1082, 680), outline=(148, 68, 48), width=6)
    draw.rectangle((666, 466, 826, 680), outline=(73, 108, 170), width=6)
    image.save(path, format='JPEG')

def write_photo(path: Path, color: tuple[int, int, int]) -> None:
    image = Image.new('RGB', (900, 700), color=color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 100, 820, 620), outline=(255, 255, 255), width=8)
    image.save(path, format='JPEG')

floorplan = src / 'floorplan.jpg'
write_floorplan(floorplan)
photo_specs = [
    ('living.jpg', (126, 108, 82)),
    ('living-detail.jpg', (132, 118, 92)),
    ('sleeping.jpg', (98, 104, 122)),
    ('balcony.jpg', (116, 126, 98)),
    ('kitchen.jpg', (86, 104, 112)),
]
asset_map = {{
    'https://img.example.test/floorplan.jpg': floorplan,
}}
for name, color in photo_specs:
    path = src / name
    write_photo(path, color)
    asset_map[f'https://img.example.test/{{name}}'] = path

os.environ['EA_PUBLIC_TOUR_DIR'] = '/data/public_property_tours'
os.environ['PROPERTYQUARRY_RECONSTRUCTION_TIMEOUT_SECONDS'] = {str(generation_timeout_seconds)!r}
os.environ['PROPERTYQUARRY_RECONSTRUCTION_WALKTHROUGH_SECONDS_PER_STOP'] = '5'
os.environ['PROPERTYQUARRY_RECONSTRUCTION_FFMPEG_TIMEOUT_SECONDS'] = '420'
product_service._download_property_reconstruction_image = lambda url, target_dir, *, stem: asset_map.get(str(url or '').strip())
materialized_slug = product_service._make_hosted_property_tour_slug(
    title=title,
    listing_id=listing_id,
    property_url=property_url,
    variant_key='layout_first',
)
shutil.rmtree(Path('/data/public_property_tours') / materialized_slug, ignore_errors=True)
payload = product_service._write_generated_reconstruction_property_tour_bundle(
    principal_id='property-tour-runtime-smoke',
    title=title,
    listing_id=listing_id,
    property_url=property_url,
    variant_key='layout_first',
    media_urls=[
        'https://img.example.test/living.jpg',
        'https://img.example.test/living-detail.jpg',
        'https://img.example.test/sleeping.jpg',
        'https://img.example.test/balcony.jpg',
        'https://img.example.test/kitchen.jpg',
    ],
    floorplan_urls=['https://img.example.test/floorplan.jpg'],
    property_facts_json={{
        'rooms': 4,
        'description': 'Apartment with balcony and separate kitchen.',
        'has_floorplan': True,
        'has_balcony': True,
        'has_terrace': True,
    }},
    source_host='www.willhaben.at',
    source_ref=source_ref,
    external_id=external_id,
    recipient_email='owner@example.test',
    diorama_style_hint='Ikea',
)
print(json.dumps({{'slug': payload.get('slug')}}, sort_keys=True))
"""
    try:
        generated = _run(
            ["docker", "exec", container, "python", "-c", setup_script],
            timeout=generation_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "failed",
            "generated_at": started_at,
            "container": container,
            "slug": slug,
            "render_bridge_runtime": render_bridge_runtime,
            "reason": "runtime_reconstruction_command_timeout",
            "timeout_seconds": generation_timeout_seconds,
            "stdout_tail": _timeout_stream_tail(exc, "stdout"),
            "stderr_tail": _timeout_stream_tail(exc, "stderr"),
        }
    if generated.returncode != 0:
        return {
            "status": "failed",
            "generated_at": started_at,
            "container": container,
            "slug": slug,
            "render_bridge_runtime": render_bridge_runtime,
            "reason": "runtime_reconstruction_command_failed",
            "timeout_seconds": generation_timeout_seconds,
            "returncode": generated.returncode,
            "stdout_tail": (generated.stdout or "")[-1000:],
            "stderr_tail": (generated.stderr or "")[-1000:],
        }
    generated_slug = str(slug or "").strip()
    try:
        generated_setup_payload = json.loads((generated.stdout or "").strip().splitlines()[-1])
    except Exception as exc:
        return {
            "status": "failed",
            "generated_at": started_at,
            "container": container,
            "slug": slug,
            "render_bridge_runtime": render_bridge_runtime,
            "reason": "runtime_reconstruction_generation_unparseable",
            "error": type(exc).__name__,
            "stdout_tail": (generated.stdout or "")[-1000:],
        }
    if isinstance(generated_setup_payload, dict):
        generated_slug = str(generated_setup_payload.get("slug") or generated_slug).strip() or generated_slug

    inspect_script = f"""
import json
from pathlib import Path
slug = {generated_slug!r}
base = Path('/data/public_property_tours') / slug
manifest = json.loads((base / 'tour.json').read_text(encoding='utf-8'))
receipt = json.loads((base / 'generated-reconstruction' / 'reconstruction.json').read_text(encoding='utf-8'))
paths = {{
  'viewer': base / 'generated-reconstruction' / 'viewer.html',
  'obj': base / 'generated-reconstruction' / 'model.obj',
  'mtl': base / 'generated-reconstruction' / 'model.mtl',
  'glb': base / 'generated-reconstruction' / 'model.glb',
  'receipt': base / 'generated-reconstruction' / 'reconstruction.json',
  'walkthrough_video': base / 'generated-reconstruction' / 'generated-walkthrough.mp4',
  'walkthrough_sidecar': base / 'generated-reconstruction' / 'generated-walkthrough.quality.json',
}}
generated = manifest.get('generated_reconstruction') if isinstance(manifest.get('generated_reconstruction'), dict) else {{}}
walkthrough = receipt.get('walkthrough') if isinstance(receipt.get('walkthrough'), dict) else {{}}
walkable_scene = receipt.get('walkable_scene') if isinstance(receipt.get('walkable_scene'), dict) else {{}}
print(json.dumps({{
  'slug': slug,
  'manifest_generated_reconstruction': generated,
  'receipt_provider': receipt.get('provider'),
  'verified_provider_capture': receipt.get('verified_provider_capture'),
  'satisfies_verified_tour_gate': receipt.get('satisfies_verified_tour_gate'),
  'glb_export_status': (receipt.get('model') or {{}}).get('glb_export', {{}}).get('status'),
  'photo_count': len(list(receipt.get('photos') or [])),
  'route_labels': list(receipt.get('route_labels') or []),
  'walkthrough_route_labels': list(receipt.get('walkthrough_route_labels') or []),
  'walkable_scene_route_labels': [row.get('label') for row in list(walkable_scene.get('route') or []) if isinstance(row, dict)],
  'generated_room_stop_count': generated.get('room_stop_count'),
  'generated_walkthrough_stop_count': generated.get('walkthrough_stop_count'),
  'generated_photo_reference_panel_count': generated.get('photo_reference_panel_count'),
  'receipt_photo_reference_panel_count': len(list(receipt.get('photo_reference_panels') or [])),
  'walkthrough_status': walkthrough.get('status'),
  'walkthrough_coverage_proof': walkthrough.get('coverage_proof') or {{}},
  'paths': {{key: {{'exists': value.is_file(), 'size_bytes': value.stat().st_size if value.exists() else 0}} for key, value in paths.items()}},
}}, sort_keys=True))
"""
    inspected = _run(
        ["docker", "exec", container, "python", "-c", inspect_script],
        timeout=30,
    )
    if inspected.returncode != 0:
        return {
            "status": "failed",
            "generated_at": started_at,
            "container": container,
            "slug": slug,
            "render_bridge_runtime": render_bridge_runtime,
            "reason": "runtime_reconstruction_inspection_failed",
            "stdout_tail": (inspected.stdout or "")[-1000:],
            "stderr_tail": (inspected.stderr or "")[-1000:],
        }
    try:
        details = json.loads((inspected.stdout or "").strip().splitlines()[-1])
    except Exception as exc:
        return {
            "status": "failed",
            "generated_at": started_at,
            "container": container,
            "slug": slug,
            "render_bridge_runtime": render_bridge_runtime,
            "reason": "runtime_reconstruction_inspection_unparseable",
            "error": type(exc).__name__,
            "stdout_tail": (inspected.stdout or "")[-1000:],
        }

    paths = details.get("paths") if isinstance(details.get("paths"), dict) else {}
    generated_reconstruction = (
        details.get("manifest_generated_reconstruction")
        if isinstance(details.get("manifest_generated_reconstruction"), dict)
        else {}
    )
    required_path_keys = ("viewer", "obj", "mtl", "receipt", *(() if not require_glb else ("glb",)))
    generated_slug = str(details.get("slug") or generated_slug or slug).strip() or str(slug or "").strip()
    required_paths_ok = all(bool((paths.get(key) or {}).get("exists")) for key in required_path_keys)
    glb_non_empty = int((paths.get("glb") or {}).get("size_bytes") or 0) > 0
    honest_disclosure_ok = (
        details.get("receipt_provider") == "propertyquarry_generated_reconstruction"
        and details.get("verified_provider_capture") is False
        and details.get("satisfies_verified_tour_gate") is False
        and generated_reconstruction.get("verified_provider_capture") is False
        and generated_reconstruction.get("satisfies_verified_tour_gate") is False
    )
    glb_manifest_ok = (
        details.get("glb_export_status") == "generated"
        and generated_reconstruction.get("glb_export_status") == "generated"
        and str(generated_reconstruction.get("glb_model_relpath") or "").endswith("/model.glb")
    )
    glb_capability_ok = bool(glb_manifest_ok or not require_glb)
    route_labels = _label_list(details.get("route_labels"))
    walkthrough_route_labels = _label_list(details.get("walkthrough_route_labels"))
    walkable_scene_route_labels = _label_list(details.get("walkable_scene_route_labels"))
    photo_count = max(0, int(details.get("photo_count") or 0))
    detail_required = photo_count > len(route_labels)
    expected_walkthrough_stop_count = max(len(route_labels), photo_count)
    has_arrival_context = _labels_contain_keyword(route_labels, r"\b(entry|hall|foyer|vorraum|flur|stair(?:case)?|treppe)\b")
    has_living_context = _labels_contain_keyword(route_labels, r"\b(living|wohn)\b")
    has_bedroom_context = _labels_contain_keyword(route_labels, r"\b(sleep(?:ing)?|bedroom|schlaf)\b")
    has_outdoor_context = _labels_contain_keyword(route_labels, r"\b(balcony|terrace|loggia|balkon|terrasse)\b")
    coverage_proof = (
        dict(details.get("walkthrough_coverage_proof") or {})
        if isinstance(details.get("walkthrough_coverage_proof"), dict)
        else {}
    )
    route_label_quality_ok = (
        len(route_labels) >= 4
        and not any(_looks_like_generic_route_label(label) for label in route_labels)
        and has_living_context
        and (has_arrival_context or has_bedroom_context)
        and (has_bedroom_context or has_outdoor_context)
        and walkable_scene_route_labels == route_labels
        and int(details.get("generated_room_stop_count") or 0) == len(route_labels)
    )
    walkthrough_label_quality_ok = (
        len(walkthrough_route_labels) >= expected_walkthrough_stop_count
        and len(walkthrough_route_labels) >= len(route_labels)
        and not any(_looks_like_generic_route_label(label) for label in walkthrough_route_labels)
        and (not detail_required or any("detail" in label.lower() for label in walkthrough_route_labels))
        and int(details.get("generated_walkthrough_stop_count") or 0) == len(walkthrough_route_labels)
        and int(details.get("generated_photo_reference_panel_count") or 0) == photo_count
        and int(details.get("receipt_photo_reference_panel_count") or 0) == photo_count
    )
    walkthrough_generated_ok = (
        str(details.get("walkthrough_status") or "").strip().lower() == "generated"
        and bool((paths.get("walkthrough_video") or {}).get("exists"))
        and bool((paths.get("walkthrough_sidecar") or {}).get("exists"))
        and _coverage_proof_covers_walkthrough_route(coverage_proof, walkthrough_route_labels)
    )
    resolved_public_base_url = _resolved_local_public_base_url(
        public_base_url,
        public_container=str(os.getenv("PROPERTYQUARRY_API_CONTAINER_NAME") or "propertyquarry-api").strip(),
    )
    host_public_tour_sync: dict[str, object] = {"status": "skipped", "reason": "public_base_url_missing"}
    if resolved_public_base_url and (require_browser or require_public_contract or require_browser_shell):
        host_public_tour_sync = _sync_container_tour_to_host_root(
            container,
            slug=generated_slug,
            public_base_url=resolved_public_base_url,
        )
    viewer_url = _generated_reconstruction_viewer_url(public_base_url=resolved_public_base_url, slug=generated_slug)
    public_contract_receipt: dict[str, object] = {}
    public_contract_ok = True
    require_public_contract_flag = bool(require_browser or require_public_contract)
    if resolved_public_base_url:
        if require_public_contract_flag:
            public_contract_receipt = _check_generated_reconstruction_public_contract(
                public_base_url=resolved_public_base_url,
                slug=generated_slug,
                host_header=host_header,
            )
            public_contract_ok = public_contract_receipt.get("status") == "pass"
    elif require_public_contract_flag:
        public_contract_ok = False
        public_contract_receipt = {"status": "blocked", "reason": "public_base_url_missing"}
    browser_shell_receipt: dict[str, object] = {}
    browser_shell_ok = True
    if resolved_public_base_url:
        if require_browser_shell:
            browser_shell_receipt = _run_bounded_browser_shell_probe(
                public_base_url=resolved_public_base_url,
                slug=generated_slug,
                host_header=host_header,
                expected_route_stop_count=len(route_labels),
                expected_photo_count=photo_count,
                expected_route_labels=route_labels,
            )
            browser_shell_ok = browser_shell_receipt.get("status") == "pass"
    elif require_browser_shell:
        browser_shell_ok = False
        browser_shell_receipt = {"status": "blocked", "reason": "public_base_url_missing"}
    status = (
        "pass"
        if (
            required_paths_ok
            and honest_disclosure_ok
            and glb_capability_ok
            and route_label_quality_ok
            and walkthrough_label_quality_ok
            and walkthrough_generated_ok
            and public_contract_ok
            and browser_shell_ok
        )
        else "failed"
    )
    return {
        "status": status,
        "generated_at": started_at,
        "container": container,
        "requested_slug": str(slug or "").strip(),
        "slug": generated_slug,
        "render_bridge_runtime": render_bridge_runtime,
        "host_public_tour_sync": host_public_tour_sync,
        "resolved_public_base_url": resolved_public_base_url,
        "viewer_url": viewer_url,
        "duration_seconds": round(time.time() - datetime.fromisoformat(started_at).timestamp(), 3),
        "required_paths_ok": required_paths_ok,
        "glb_non_empty": glb_non_empty,
        "honest_disclosure_ok": honest_disclosure_ok,
        "glb_manifest_ok": glb_manifest_ok,
        "glb_required": bool(require_glb),
        "glb_capability_ok": glb_capability_ok,
        "route_label_quality_ok": route_label_quality_ok,
        "walkthrough_label_quality_ok": walkthrough_label_quality_ok,
        "walkthrough_generated_ok": walkthrough_generated_ok,
        "public_route_contract_ok": public_contract_ok,
        "public_route_contract": public_contract_receipt,
        "browser_shell_ok": browser_shell_ok,
        "browser_shell": browser_shell_receipt,
        "details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke the deployed PropertyQuarry runtime generated reconstruction path.")
    parser.add_argument(
        "--container",
        default=os.getenv("PROPERTYQUARRY_API_CONTAINER_NAME") or DEFAULT_API_CONTAINER,
    )
    parser.add_argument("--slug", default="runtime-reconstruction-smoke")
    parser.add_argument("--public-base-url", default=os.getenv("PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_PUBLIC_BASE_URL") or "")
    parser.add_argument(
        "--require-browser",
        action="store_true",
        help="Deprecated name; now requires the public generated-reconstruction first-party shell contract.",
    )
    parser.add_argument(
        "--require-public-contract",
        action="store_true",
        help="Require public routes to serve only the first-party generated-reconstruction shell/control contract and deny raw model access.",
    )
    parser.add_argument(
        "--require-browser-shell",
        action="store_true",
        help="Require a real browser proof of the first-party generated-reconstruction launch shell and layout preview.",
    )
    parser.add_argument(
        "--host-header",
        default=os.getenv("PROPERTYQUARRY_LIVE_HOST_HEADER") or "propertyquarry.com",
        help="Host header/domain to map when public-base-url targets localhost or 127.0.0.1.",
    )
    parser.add_argument("--require-glb", action="store_true")
    parser.add_argument(
        "--skip-render-bridge-runtime-ensure",
        action="store_true",
        help="Skip the compose/runtime bootstrap that ensures the render bridge container exists before the smoke runs.",
    )
    parser.add_argument("--write", default="_completion/tours/property-runtime-reconstruction-smoke-current.json")
    parser.add_argument("--fail-on-error", action="store_true")
    args = parser.parse_args()

    receipt = build_runtime_reconstruction_receipt(
        container=args.container,
        slug=args.slug,
        public_base_url=args.public_base_url,
        require_browser=bool(args.require_browser),
        require_public_contract=bool(args.require_public_contract),
        require_browser_shell=bool(args.require_browser_shell),
        host_header=str(args.host_header or "").strip(),
        require_glb=bool(args.require_glb),
        ensure_render_bridge_runtime=not bool(args.skip_render_bridge_runtime_ensure),
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    if args.fail_on_error and receipt.get("status") != "pass":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
