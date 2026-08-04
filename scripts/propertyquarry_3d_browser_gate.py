#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
for candidate in (ROOT, EA_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

try:
    from scripts.propertyquarry_playwright_runtime import playwright_chromium_launch_kwargs
except ModuleNotFoundError:
    from propertyquarry_playwright_runtime import playwright_chromium_launch_kwargs  # type: ignore[no-redef]

from app.product.property_tour_hosting import persist_hosted_property_tour_browser_render_proof


DEFAULT_DEMO_SLUG = "luxury-residence-with-breathtaking-skyline-views-danubeflats-vienna-layout-first-742df65557"
DEFAULT_PROVIDERS = ("3dvista",)
FAILURE_PATTERNS = (
    "violates the following content security policy",
    "refused to display",
    "err_blocked_by_response",
    "webassembly.instantiate",
    "failed to fetch",
)
RUNTIME_3DVISTA_PROOF_PERSIST_SCRIPT = r"""
import json
import sys
from pathlib import Path

from app.product.property_tour_hosting import (
    _validate_hosted_property_tour_private_receipt_target,
    _write_hosted_property_tour_private_receipt_atomic,
)


def emit(status, **extra):
    print(json.dumps({"status": status, **extra}, ensure_ascii=True, sort_keys=True))


def main():
    try:
        encoded = sys.stdin.buffer.read(1_048_577)
        if len(encoded) > 1_048_576:
            emit("invalid_request", reason="request_too_large")
            return 2
        request = json.loads(encoded)
        slug = str(request.get("slug") or "").strip()
        proof = request.get("proof")
        if not slug or "/" in slug or ".." in slug or not isinstance(proof, dict):
            emit("invalid_request", reason="invalid_slug_or_proof")
            return 2
        if (
            str(proof.get("status") or "").strip().lower() != "pass"
            or proof.get("rendered_viewer") is not True
            or proof.get("interactive_viewer") is not True
        ):
            emit("invalid_request", reason="proof_not_pass_rendered_interactive")
            return 2

        root = Path("/data/public_property_tours").resolve()
        bundle = (root / slug).resolve()
        if root not in bundle.parents or not (bundle / "tour.json").is_file():
            emit("tour_bundle_not_found")
            return 3

        private_path = bundle / "tour.private.json"
        _validate_hosted_property_tour_private_receipt_target(private_path)
        private_payload = {}
        if private_path.is_file():
            loaded = json.loads(private_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                emit("private_receipt_invalid")
                return 4
            private_payload = dict(loaded)
        private_payload["three_d_vista_browser_render_proof"] = dict(proof)
        _write_hosted_property_tour_private_receipt_atomic(bundle, private_payload)
        emit("updated", provider="3dvista", slug=slug)
        return 0
    except Exception as exc:
        emit(
            "runtime_persistence_failed",
            reason=f"runtime_persistence_{type(exc).__name__.lower()}",
        )
        return 5


raise SystemExit(main())
""".strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _check(name: str, ok: bool, **extra: object) -> dict[str, object]:
    return {"name": name, "ok": bool(ok), **extra}


def _browser_url_and_args(base_url: str, host_header: str) -> tuple[str, list[str]]:
    parsed = urllib.parse.urlparse(str(base_url or "http://localhost:8097").strip().rstrip("/"))
    host = str(parsed.hostname or "").lower()
    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
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


def _is_report_only_csp_information(row: dict[str, str]) -> bool:
    text = str(row.get("text") or "").strip().lower()
    return (
        str(row.get("type") or "").strip().lower() == "info"
        and "violates the following content security policy" in text
        and "the policy is report-only" in text
    )


def _bad_console_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    bad: list[dict[str, str]] = []
    for row in messages:
        text = str(row.get("text") or "").lower()
        if _is_report_only_csp_information(row):
            continue
        if any(pattern in text for pattern in FAILURE_PATTERNS):
            bad.append(row)
    return bad


def _bad_responses(responses: list[dict[str, object]], *, browser_base_url: str) -> list[dict[str, object]]:
    parsed_base = urllib.parse.urlparse(browser_base_url)
    base_host = str(parsed_base.hostname or "").lower()
    bad: list[dict[str, object]] = []
    for row in responses:
        status = int(row.get("status") or 0)
        if status < 400:
            continue
        url = str(row.get("url") or "")
        parsed = urllib.parse.urlparse(url)
        host = str(parsed.hostname or "").lower()
        resource_type = str(row.get("resource_type") or "")
        if host == base_host or resource_type in {"document", "script", "fetch", "xhr"}:
            bad.append(row)
    return bad


def _bad_request_failures(request_failures: list[dict[str, str]], *, browser_base_url: str) -> list[dict[str, str]]:
    parsed_base = urllib.parse.urlparse(browser_base_url)
    base_host = str(parsed_base.hostname or "").lower()
    bad: list[dict[str, str]] = []
    for row in request_failures:
        url = str(row.get("url") or "")
        resource_type = str(row.get("resource_type") or "")
        failure = str(row.get("failure") or "")
        if "favicon" in url.lower():
            continue
        if resource_type == "media" and "net::ERR_ABORTED" in failure:
            continue
        parsed = urllib.parse.urlparse(url)
        host = str(parsed.hostname or "").lower()
        provider_media_suffix = PurePosixPath(parsed.path).suffix.lower()
        if (
            host == base_host
            and resource_type in {"fetch", "image", "xhr"}
            and "net::ERR_ABORTED" in failure
            and "/tours/3dvista/" in parsed.path
            and any(segment in parsed.path for segment in ("/media/", "/skin/"))
            and provider_media_suffix
            in {".avif", ".jpeg", ".jpg", ".png", ".webp"}
        ):
            # 3DVista cancels speculative image XHRs when the active panorama
            # supersedes them. HTTP >= 400 responses are still caught by
            # _bad_responses; other transport failures remain blockers here.
            continue
        if host != base_host and resource_type in {"image", "media", "font"}:
            continue
        if host == base_host or resource_type in {"document", "script", "fetch", "xhr"}:
            bad.append(row)
    return bad


def _frame_render_state(page: Any, *, provider: str) -> dict[str, object]:
    frames = [frame.url for frame in page.frames]
    iframe_srcs = page.locator("iframe").evaluate_all("(els) => els.map((node) => node.getAttribute('src') || '')")
    provider_frame_url = ""
    for frame_url in frames:
        if "/control/" in frame_url:
            continue
        if frame_url and frame_url != "about:blank":
            provider_frame_url = frame_url
            break
    state: dict[str, object] = {
        "frames": frames,
        "iframe_srcs": iframe_srcs,
        "provider_frame_url": provider_frame_url,
        "canvas_count": 0,
        "visible_canvas_count": 0,
        "loading_indicator_count": None,
        "frame_text": "",
        "same_origin_frame_inspected": False,
    }
    for frame in page.frames:
        if frame.url != provider_frame_url or not provider_frame_url:
            continue
        if provider_frame_url.startswith("http") and urllib.parse.urlparse(provider_frame_url).hostname not in {"propertyquarry.com", "localhost", "127.0.0.1"}:
            continue
        try:
            state["same_origin_frame_inspected"] = True
            state["canvas_count"] = frame.locator("canvas").count()
            state["visible_canvas_count"] = frame.locator("canvas:visible").count()
            state["loading_indicator_count"] = frame.get_by_text(
                "Loading virtual tour",
                exact=False,
            ).evaluate_all(
                """(nodes) => nodes.filter((node) => {
                  const style = getComputedStyle(node);
                  const box = node.getBoundingClientRect();
                  return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && Number(style.opacity || 1) > 0
                    && box.width > 0
                    && box.height > 0;
                }).length"""
            )
        except Exception as exc:
            state["frame_inspection_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    if provider == "matterport" and "my.matterport.com/show/" in provider_frame_url:
        state["external_embedded_target_ok"] = True
    return state


def _tour_shell_ux_state(page: Any) -> dict[str, object]:
    return dict(
        page.evaluate(
            """() => {
              const frame = document.querySelector('.provider-frame');
              const status = document.querySelector('[data-provider-status]');
              const visibleControls = Array.from(document.querySelectorAll('button, a[href]'))
                .filter((node) => {
                  const style = getComputedStyle(node);
                  const box = node.getBoundingClientRect();
                  return !node.hidden
                    && style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && box.width > 0
                    && box.height > 0;
                });
              const undersized = visibleControls
                .map((node) => {
                  const box = node.getBoundingClientRect();
                  return {
                    label: (node.getAttribute('aria-label') || node.textContent || '').trim(),
                    width: Math.round(box.width * 10) / 10,
                    height: Math.round(box.height * 10) / 10,
                  };
                })
                .filter((row) => row.width < 44 || row.height < 44);
              return {
                html_lang: document.documentElement.lang,
                main_count: document.querySelectorAll('main').length,
                frame_title: frame?.getAttribute('title') || '',
                frame_aria_label: frame?.getAttribute('aria-label') || '',
                status_role: status?.getAttribute('role') || '',
                body_scroll_width: document.body?.scrollWidth || 0,
                viewport_width: window.innerWidth,
                undersized_controls: undersized,
                reduced_motion: matchMedia('(prefers-reduced-motion: reduce)').matches,
                active_animation_count: document.getAnimations().filter((item) => item.playState === 'running').length,
              };
            }"""
        )
        or {}
    )


def _walkthrough_video_state(page: Any, *, timeout_ms: int) -> dict[str, object]:
    video = page.locator("#tour-video")
    if video.count() != 1:
        return {"error": "walkthrough_video_missing", "video_count": video.count()}

    video.evaluate("(node) => { node.muted = true; node.preload = 'metadata'; node.load(); }")
    metadata_error = ""
    try:
        page.wait_for_function(
            """() => {
              const video = document.querySelector('#tour-video');
              return Boolean(video?.currentSrc) && video.readyState >= HTMLMediaElement.HAVE_METADATA;
            }""",
            timeout=min(max(timeout_ms, 5_000), 20_000),
        )
        video.evaluate(
            """(node) => {
              const seekTarget = Math.min(7, Math.max(0, (Number(node.duration) || 0) / 2));
              if (seekTarget > 0) node.currentTime = seekTarget;
            }"""
        )
        page.wait_for_function(
            """() => {
              const video = document.querySelector('#tour-video');
              return Boolean(video) && !video.seeking && video.currentTime > 0;
            }""",
            timeout=min(max(timeout_ms, 5_000), 12_000),
        )
        page.wait_for_timeout(250)
    except Exception as exc:
        metadata_error = f"{type(exc).__name__}: {str(exc)[:240]}"

    return dict(
        page.evaluate(
            """() => {
              const video = document.querySelector('#tour-video');
              const box = video?.getBoundingClientRect();
              return {
                video_count: document.querySelectorAll('#tour-video').length,
                sources: Array.from(video?.querySelectorAll('source') || []).map((source) => ({
                  src: source.getAttribute('src') || '',
                  media: source.getAttribute('media') || '',
                  type: source.getAttribute('type') || '',
                })),
                current_src: video?.currentSrc || '',
                ready_state: video?.readyState || 0,
                duration_seconds: Number(video?.duration) || 0,
                current_time_seconds: Number(video?.currentTime) || 0,
                video_width: video?.videoWidth || 0,
                video_height: video?.videoHeight || 0,
                rendered_width: box?.width || 0,
                rendered_height: box?.height || 0,
                body_scroll_width: document.body?.scrollWidth || 0,
                viewport_width: window.innerWidth,
                mobile_media_matches: matchMedia('(max-width: 760px)').matches,
              };
            }"""
        )
        or {}
    ) | {"metadata_error": metadata_error}


def _walkthrough_gate_checks(
    *,
    response_ok: bool,
    response_status: int,
    provider_verified: bool,
    walkthrough_state: dict[str, object],
    slug: str,
    viewport_width: int,
) -> tuple[bool, list[dict[str, object]]]:
    video_count = walkthrough_state.get("video_count")
    explicitly_not_advertised = (
        walkthrough_state.get("error") == "walkthrough_video_missing"
        and video_count == 0
    )
    if explicitly_not_advertised:
        return False, [
            _check(
                "walkthrough_optional_when_not_advertised",
                bool(response_ok and provider_verified),
                status=int(response_status or 0),
                provider_verified=provider_verified,
                advertised=False,
                video_count=0,
                reason="accepted_walkthrough_not_advertised",
            )
        ]

    source_rows = [
        dict(row)
        for row in list(walkthrough_state.get("sources") or [])
        if isinstance(row, dict)
    ]
    mobile_expected = int(viewport_width) <= 760
    current_src_path = urllib.parse.urlparse(
        str(walkthrough_state.get("current_src") or "")
    ).path
    expected_mobile_suffix = "walkthrough-mobile-720p60.mp4"
    expected_desktop_suffix = (
        f"/tours/{urllib.parse.quote(str(slug or '').strip(), safe='')}/walkthrough"
    )
    return True, [
        _check(
            "walkthrough_control_page_ok",
            bool(response_ok),
            status=int(response_status or 0),
        ),
        _check(
            "walkthrough_responsive_sources_present",
            len(source_rows) == 2
            and str(source_rows[0].get("media") or "") == "(max-width: 760px)"
            and str(source_rows[0].get("src") or "").endswith(expected_mobile_suffix)
            and str(source_rows[1].get("src") or "").endswith(expected_desktop_suffix),
            sources=source_rows,
        ),
        _check(
            "walkthrough_current_source_matches_viewport",
            current_src_path.endswith(
                expected_mobile_suffix if mobile_expected else expected_desktop_suffix
            ),
            current_src=str(walkthrough_state.get("current_src") or ""),
            mobile_expected=mobile_expected,
        ),
        _check(
            "walkthrough_metadata_decoded",
            not str(walkthrough_state.get("metadata_error") or "")
            and int(walkthrough_state.get("ready_state") or 0) >= 1
            and float(walkthrough_state.get("duration_seconds") or 0) >= 30
            and int(walkthrough_state.get("video_width") or 0)
            == (1280 if mobile_expected else 1920)
            and int(walkthrough_state.get("video_height") or 0)
            == (720 if mobile_expected else 1080),
            state=walkthrough_state,
        ),
        _check(
            "walkthrough_responsive_layout",
            int(walkthrough_state.get("body_scroll_width") or 0)
            <= int(walkthrough_state.get("viewport_width") or 0) + 1
            and float(walkthrough_state.get("rendered_width") or 0)
            <= int(walkthrough_state.get("viewport_width") or 0) + 1,
            state=walkthrough_state,
        ),
    ]


def _exercise_provider_recovery(page: Any, *, provider: str, timeout_ms: int) -> dict[str, object]:
    recovery = page.locator("[data-provider-recovery]")
    retry = page.locator("[data-provider-retry]")
    direct = page.locator("[data-provider-direct]")
    if not recovery.count() or not retry.count() or not direct.count():
        return {
            "recovery_visible": False,
            "recovery_controls_ok": False,
            "direct_href": "",
            "retry_ready": False,
            "rendered_after_retry": False,
            "error": "provider_recovery_controls_missing",
        }
    page.evaluate("() => window.dispatchEvent(new Event('offline'))")
    page.wait_for_timeout(100)
    recovery_visible = recovery.is_visible()
    dimensions = retry.evaluate(
        """(button) => {
          const direct = document.querySelector('[data-provider-direct]');
          const buttonBox = button.getBoundingClientRect();
          const directBox = direct?.getBoundingClientRect();
          return {
            button_width: buttonBox.width,
            button_height: buttonBox.height,
            direct_width: directBox?.width || 0,
            direct_height: directBox?.height || 0,
          };
        }"""
    )
    recovery_controls_ok = all(
        float(dimensions.get(key) or 0) >= 44
        for key in ("button_width", "button_height", "direct_width", "direct_height")
    )
    direct_href = str(direct.get_attribute("href") or "")
    retry_ready = False
    rendered_after_retry = False
    error = ""
    try:
        retry.evaluate("(button) => button.click()")
        page.wait_for_function(
            "() => document.querySelector('.provider-frame-wrap')?.dataset.providerState === 'ready'",
            timeout=min(timeout_ms, 20_000),
        )
        retry_ready = True
        retry_state = _wait_for_provider_rendered(page, provider=provider, timeout_ms=timeout_ms)
        rendered_after_retry = _provider_rendered_ok(provider, retry_state)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    return {
        "recovery_visible": recovery_visible,
        "recovery_controls_ok": recovery_controls_ok,
        "direct_href": direct_href,
        "retry_ready": retry_ready,
        "rendered_after_retry": rendered_after_retry,
        "dimensions": dimensions,
        "error": error,
    }


def _provider_rendered_ok(provider: str, state: dict[str, object]) -> bool:
    provider_key = str(provider or "").strip().lower()
    frame_url = str(state.get("provider_frame_url") or "")
    if not frame_url or frame_url == "about:blank":
        return False
    text = str(state.get("frame_text") or "").lower()
    if provider_key in {"3dvista", "pano2vr"}:
        return (
            int(state.get("visible_canvas_count") or 0) > 0
            and state.get("loading_indicator_count") == 0
            and "loading virtual tour" not in text
        )
    if provider_key == "matterport":
        return "my.matterport.com/show/" in frame_url and bool(state.get("external_embedded_target_ok"))
    return True


def _exercise_provider_drag(page: Any, *, provider: str) -> dict[str, object]:
    if str(provider or "").strip().lower() != "3dvista":
        return {}
    frame = page.locator("iframe.provider-frame").first
    if frame.count() != 1 or not frame.is_visible():
        return {
            "drag_performed": False,
            "visual_changed": False,
            "error": "provider_frame_not_visible",
        }
    try:
        box = frame.bounding_box()
        if not box or float(box.get("width") or 0) < 120 or float(box.get("height") or 0) < 120:
            return {
                "drag_performed": False,
                "visual_changed": False,
                "error": "provider_frame_too_small",
                "box": box or {},
            }
        before = frame.screenshot(caret="hide", animations="disabled")
        center_x = float(box["x"]) + float(box["width"]) * 0.5
        center_y = float(box["y"]) + float(box["height"]) * 0.5
        page.mouse.move(center_x, center_y)
        page.mouse.down()
        page.mouse.move(
            center_x - max(80.0, float(box["width"]) * 0.28),
            center_y,
            steps=12,
        )
        page.mouse.up()
        page.wait_for_timeout(700)
        after = frame.screenshot(caret="hide", animations="disabled")
        return {
            "drag_performed": True,
            "visual_changed": before != after,
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "before_bytes": len(before),
            "after_bytes": len(after),
            "box": {
                "width": round(float(box["width"]), 1),
                "height": round(float(box["height"]), 1),
            },
            "error": "",
        }
    except Exception as exc:
        try:
            page.mouse.up()
        except Exception:
            pass
        return {
            "drag_performed": False,
            "visual_changed": False,
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


def _wait_for_provider_rendered(page: Any, *, provider: str, timeout_ms: int) -> dict[str, object]:
    poll_ms = 500
    if str(provider or "").strip().lower() == "matterport":
        page.wait_for_timeout(min(max(int(timeout_ms or 0), 1_000), 8_000))
    deadline = time.monotonic() + (max(1_000, min(int(timeout_ms or 0), 30_000)) / 1_000)
    state: dict[str, object] = {}
    while True:
        state = _frame_render_state(page, provider=provider)
        if _provider_rendered_ok(provider, state):
            return state
        remaining_ms = int((deadline - time.monotonic()) * 1_000)
        if remaining_ms <= 0:
            return state
        page.wait_for_timeout(min(poll_ms, remaining_ms))


def _provider_checks(receipt: dict[str, object], provider: str) -> list[dict[str, object]]:
    prefix = f"{str(provider or '').strip().lower()}_"
    return [
        dict(row)
        for row in list(receipt.get("checks") or [])
        if isinstance(row, dict) and str(row.get("name") or "").strip().lower().startswith(prefix)
    ]


def _candidate_public_tour_roots(
    *,
    public_roots: Iterable[str | Path] = (),
) -> list[Path]:
    candidates = [
        Path(root).expanduser()
        for root in public_roots
        if str(root or "").strip()
    ]
    seen: set[str] = set()
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def _runtime_container_name(configured_container: str = "") -> str:
    return str(configured_container or os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "").strip()


def _configured_runtime_container_user(inspect_stdout: str) -> str:
    try:
        inspected = json.loads(str(inspect_stdout or ""))
    except (TypeError, ValueError):
        return ""
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        return ""
    config = inspected[0].get("Config")
    if not isinstance(config, dict):
        return ""
    return str(config.get("User") or "").strip()


def _runtime_persistence_result(stdout: str) -> dict[str, object]:
    lines = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    if not lines:
        return {}
    try:
        payload = json.loads(lines[-1])
    except (TypeError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _persist_3dvista_browser_render_proof_in_runtime_container(
    slug: str,
    proof: dict[str, object],
    *,
    runtime_container: str = "",
) -> dict[str, object]:
    container = _runtime_container_name(runtime_container)
    if not container:
        return {"status": "runtime_container_not_configured", "slug": slug, "provider": "3dvista"}
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return {"status": "docker_unavailable", "slug": slug, "provider": "3dvista"}
    inspect_result = subprocess.run(
        [docker_bin, "inspect", container],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if inspect_result.returncode != 0:
        return {"status": "runtime_container_unavailable", "slug": slug, "provider": "3dvista", "container": container}
    runtime_user = _configured_runtime_container_user(inspect_result.stdout)
    if not runtime_user:
        return {
            "status": "runtime_container_user_not_configured",
            "slug": slug,
            "provider": "3dvista",
            "container": container,
        }
    request_json = json.dumps(
        {"slug": slug, "proof": proof},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    completed = subprocess.run(
        [
            docker_bin,
            "exec",
            "--interactive",
            "--user",
            runtime_user,
            container,
            "python",
            "-c",
            RUNTIME_3DVISTA_PROOF_PERSIST_SCRIPT,
        ],
        input=request_json,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    runtime_result = _runtime_persistence_result(completed.stdout)
    if (
        completed.returncode != 0
        or str(runtime_result.get("status") or "").strip().lower() != "updated"
    ):
        return {
            "status": "runtime_private_receipt_update_failed",
            "slug": slug,
            "provider": "3dvista",
            "container": container,
            "runtime_user": runtime_user,
            "returncode": int(completed.returncode),
            "runtime_result": runtime_result,
        }
    return {
        "status": "updated",
        "slug": slug,
        "provider": "3dvista",
        "container": container,
        "runtime_user": runtime_user,
    }


def _persistable_3dvista_browser_proof(receipt: dict[str, object]) -> dict[str, object]:
    provider_results = [
        dict(row)
        for row in list(receipt.get("provider_results") or [])
        if isinstance(row, dict) and str(row.get("provider") or "").strip().lower() == "3dvista"
    ]
    if not provider_results:
        return {}
    provider_result = provider_results[0]
    provider_checks = _provider_checks(receipt, "3dvista")
    provider_passed = str(provider_result.get("status") or "").strip().lower() == "pass"
    rendered = any(
        str(row.get("name") or "").strip().lower() == "3dvista_rendered_viewer" and row.get("ok") is True
        for row in provider_checks
    )
    interactive = any(
        str(row.get("name") or "").strip().lower() == "3dvista_drag_changes_view"
        and row.get("ok") is True
        for row in provider_checks
    )
    if not provider_passed or not rendered or not interactive:
        return {}
    proof = {
        "provider": "3dvista",
        "status": "pass",
        "rendered_viewer": True,
        "interactive_viewer": True,
        "checks": provider_checks,
        "generated_at": receipt.get("generated_at") or _utc_now(),
        "browser_gate_contract_name": receipt.get("contract_name") or "propertyquarry.3d_browser_gate.v1",
        "browser_base_url": str(receipt.get("browser_base_url") or receipt.get("base_url") or "").strip(),
        "control_url": f"/tours/{urllib.parse.quote(str(receipt.get('demo_slug') or '').strip(), safe='')}/control/3dvista",
    }
    state = provider_result.get("state")
    if isinstance(state, dict):
        frame_url = str(state.get("provider_frame_url") or "").strip()
        if frame_url:
            proof["provider_frame_url"] = frame_url
    return proof


def persist_3dvista_browser_render_proof_from_receipt(
    receipt: dict[str, object],
    *,
    runtime_container: str = "",
    public_roots: Iterable[str | Path] = (),
) -> dict[str, object]:
    requested_providers = {
        str(value or "").strip().lower()
        for value in list(receipt.get("providers") or [])
        if str(value or "").strip()
    }
    if "3dvista" not in requested_providers:
        return {"status": "provider_not_requested", "provider": "3dvista"}
    slug = str(receipt.get("demo_slug") or "").strip()
    if not slug:
        return {"status": "demo_slug_missing", "provider": "3dvista"}
    provider_results = [
        row
        for row in list(receipt.get("provider_results") or [])
        if isinstance(row, dict)
        and str(row.get("provider") or "").strip().lower() == "3dvista"
    ]
    if not provider_results:
        return {"status": "provider_result_missing", "provider": "3dvista", "slug": slug}
    proof = _persistable_3dvista_browser_proof(receipt)
    if not proof:
        return {
            "status": "provider_result_not_pass_rendered",
            "provider": "3dvista",
            "slug": slug,
        }
    persistence_roots = _candidate_public_tour_roots(
        public_roots=public_roots,
    )
    host_result = (
        persist_hosted_property_tour_browser_render_proof(
            slug=slug,
            provider="3dvista",
            proof=proof,
            public_roots=persistence_roots,
        )
        if persistence_roots
        else {"status": "public_tour_root_not_configured", "provider": "3dvista", "slug": slug}
    )
    container_result = _persist_3dvista_browser_render_proof_in_runtime_container(
        slug,
        proof,
        runtime_container=runtime_container,
    )
    container_status = str(container_result.get("status") or "").strip().lower()
    if container_status not in {
        "updated",
        "runtime_container_not_configured",
        "runtime_container_unavailable",
        "docker_unavailable",
    }:
        return {
            "status": "runtime_container_persistence_failed",
            "provider": "3dvista",
            "slug": slug,
            "host_result": host_result,
            "container_result": container_result,
        }
    host_status = str(host_result.get("status") or "").strip().lower()
    if not persistence_roots and not _runtime_container_name(runtime_container):
        return {
            "status": "persistence_target_not_configured",
            "provider": "3dvista",
            "slug": slug,
            "host_result": host_result,
            "container_result": container_result,
        }
    if host_status != "updated" and container_status != "updated":
        return {
            "status": "tour_bundle_not_found",
            "provider": "3dvista",
            "slug": slug,
            "host_result": host_result,
            "container_result": container_result,
        }
    return {
        "status": "updated",
        "provider": "3dvista",
        "slug": slug,
        "host_result": host_result,
        "container_result": container_result,
    }


def build_browser_gate_receipt(
    *,
    base_url: str,
    host_header: str,
    demo_slug: str,
    providers: list[str],
    timeout_ms: int,
    viewport_width: int = 1440,
    viewport_height: int = 1000,
    write_screenshots_dir: str = "",
) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - environment guard
        return {
            "contract_name": "propertyquarry.3d_browser_gate.v1",
            "generated_at": _utc_now(),
            "status": "fail",
            "checks": [_check("playwright_available", False, error=f"{type(exc).__name__}: {exc}")],
            "failed_count": 1,
        }

    browser_base_url, browser_args = _browser_url_and_args(base_url, host_header)
    slug = str(demo_slug or DEFAULT_DEMO_SLUG).strip()
    viewport = {
        "width": max(320, int(viewport_width or 1440)),
        "height": max(480, int(viewport_height or 1000)),
    }
    screenshot_dir = Path(write_screenshots_dir) if write_screenshots_dir else None
    if screenshot_dir:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, object]] = []
    provider_results: list[dict[str, object]] = []
    walkthrough_result: dict[str, object] = {}
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                **playwright_chromium_launch_kwargs(playwright, args=browser_args)
            )
        except Exception as exc:
            checks.append(
                _check(
                    "chromium_launchable",
                    False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            return {
                "contract_name": "propertyquarry.3d_browser_gate.v1",
                "generated_at": _utc_now(),
                "status": "fail",
                "browser_base_url": browser_base_url,
                "host_header": host_header,
                "demo_slug": slug,
                "providers": providers,
                "check_count": len(checks),
                "failed_count": len(checks),
                "checks": checks,
                "provider_results": provider_results,
            }
        context = browser.new_context(
            viewport=viewport,
            ignore_https_errors=True,
            reduced_motion="reduce",
        )
        try:
            for provider in providers:
                provider_key = str(provider or "").strip().lower()
                if provider_key not in DEFAULT_PROVIDERS:
                    continue
                page = context.new_page()
                console_messages: list[dict[str, str]] = []
                request_failures: list[dict[str, str]] = []
                responses: list[dict[str, object]] = []
                page.on(
                    "console",
                    lambda msg: console_messages.append({"type": msg.type, "text": msg.text[:1_000]}),
                )
                page.on(
                    "pageerror",
                    lambda exc: console_messages.append({"type": "pageerror", "text": str(exc)[:1_000]}),
                )
                page.on(
                    "requestfailed",
                    lambda req: request_failures.append(
                        {
                            "url": req.url,
                            "resource_type": req.resource_type,
                            "failure": str(req.failure or "")[:400],
                        }
                    ),
                )
                page.on(
                    "response",
                    lambda resp: responses.append(
                        {
                            "url": resp.url,
                            "status": resp.status,
                            "resource_type": resp.request.resource_type,
                            "content_type": str(resp.headers.get("content-type") or "")[:120],
                        }
                    ),
                )
                route = f"/tours/{urllib.parse.quote(slug, safe='')}/control/{provider_key}"
                url = f"{browser_base_url}{route}"
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(800)
                load_button = page.locator("#load-provider")
                clicked = False
                if load_button.count():
                    clicked = True
                    load_button.click(timeout=timeout_ms)
                state = _wait_for_provider_rendered(page, provider=provider_key, timeout_ms=timeout_ms)
                ux_state = _tour_shell_ux_state(page)
                interaction_state = _exercise_provider_drag(page, provider=provider_key)
                screenshot_error = ""
                screenshot_path = screenshot_dir / f"{provider_key}.png" if screenshot_dir else None
                if screenshot_dir:
                    try:
                        screenshot_path.unlink(missing_ok=True)
                        page.screenshot(
                            path=str(screenshot_path),
                            full_page=provider_key == "3dvista",
                            caret="hide",
                            timeout=min(timeout_ms, 30_000),
                        )
                    except Exception as exc:
                        screenshot_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                bad_console = _bad_console_messages(console_messages)
                bad_http = _bad_responses(responses, browser_base_url=browser_base_url)
                bad_request_failures = _bad_request_failures(request_failures, browser_base_url=browser_base_url)
                recovery_state = (
                    _exercise_provider_recovery(page, provider=provider_key, timeout_ms=timeout_ms)
                    if provider_key == "3dvista"
                    else {}
                )
                frame_url = str(state.get("provider_frame_url") or "")
                rendered_ok = _provider_rendered_ok(provider_key, state)
                load_button_required_ok = clicked or not load_button.count()
                provider_checks = [
                    _check(f"{provider_key}_control_page_ok", bool(response and response.ok), status=response.status if response else 0),
                    _check(
                        f"{provider_key}_direct_viewer_loaded",
                        load_button_required_ok,
                        clicked=clicked,
                        load_button_present=bool(load_button.count()),
                    ),
                    _check(f"{provider_key}_iframe_navigated", bool(frame_url and frame_url != "about:blank"), frame_url=frame_url),
                    _check(f"{provider_key}_no_browser_console_blockers", not bad_console, bad_console=bad_console[:8]),
                    _check(f"{provider_key}_no_request_failures", not bad_request_failures, request_failures=bad_request_failures[:8]),
                    _check(f"{provider_key}_no_bad_http_assets", not bad_http, bad_http=bad_http[:8]),
                    _check(f"{provider_key}_rendered_viewer", rendered_ok, state=state),
                    _check(
                        f"{provider_key}_accessible_shell",
                        ux_state.get("html_lang") == "en"
                        and int(ux_state.get("main_count") or 0) == 1
                        and bool(ux_state.get("frame_title"))
                        and bool(ux_state.get("frame_aria_label"))
                        and ux_state.get("status_role") == "status",
                        state=ux_state,
                    ),
                    _check(
                        f"{provider_key}_responsive_touch_shell",
                        int(ux_state.get("body_scroll_width") or 0) <= int(ux_state.get("viewport_width") or 0) + 1
                        and not list(ux_state.get("undersized_controls") or []),
                        state=ux_state,
                    ),
                    _check(
                        f"{provider_key}_reduced_motion_shell",
                        ux_state.get("reduced_motion") is True
                        and int(ux_state.get("active_animation_count") or 0) == 0,
                        state=ux_state,
                    ),
                ]
                if screenshot_dir:
                    provider_checks.append(
                        _check(
                            f"{provider_key}_screenshot_captured",
                            not screenshot_error,
                            path=str(screenshot_path),
                            error=screenshot_error,
                        )
                    )
                if provider_key == "3dvista":
                    provider_checks.extend(
                        [
                            _check(
                                "3dvista_drag_changes_view",
                                interaction_state.get("drag_performed") is True
                                and interaction_state.get("visual_changed") is True,
                                state=interaction_state,
                            ),
                            _check(
                                "3dvista_offline_recovery_visible",
                                recovery_state.get("recovery_visible") is True
                                and recovery_state.get("recovery_controls_ok") is True
                                and bool(recovery_state.get("direct_href")),
                                state=recovery_state,
                            ),
                            _check(
                                "3dvista_retry_restores_viewer",
                                recovery_state.get("retry_ready") is True
                                and recovery_state.get("rendered_after_retry") is True,
                                state=recovery_state,
                            ),
                        ]
                    )
                checks.extend(provider_checks)
                provider_results.append(
                    {
                        "provider": provider_key,
                        "url": url,
                        "clicked": clicked,
                        "state": state,
                        "ux_state": ux_state,
                        "recovery_state": recovery_state,
                        "bad_console_count": len(bad_console),
                        "bad_request_failure_count": len(bad_request_failures),
                        "bad_http_count": len(bad_http),
                        "status": "pass" if all(row["ok"] for row in provider_checks) else "fail",
                    }
                )
                page.close()

            walkthrough_page = context.new_page()
            walkthrough_url = (
                f"{browser_base_url}/tours/{urllib.parse.quote(slug, safe='')}/control/3dvista"
                "?pane=flythrough-pane"
            )
            walkthrough_response = walkthrough_page.goto(
                walkthrough_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            walkthrough_state = _walkthrough_video_state(walkthrough_page, timeout_ms=timeout_ms)
            provider_verified = any(
                str(row.get("provider") or "").strip().lower() == "3dvista"
                and str(row.get("status") or "").strip().lower() == "pass"
                for row in provider_results
            )
            walkthrough_advertised, walkthrough_checks = _walkthrough_gate_checks(
                response_ok=bool(walkthrough_response and walkthrough_response.ok),
                response_status=walkthrough_response.status if walkthrough_response else 0,
                provider_verified=provider_verified,
                walkthrough_state=walkthrough_state,
                slug=slug,
                viewport_width=int(viewport["width"]),
            )
            screenshot_error = ""
            walkthrough_screenshot_path = screenshot_dir / "walkthrough.png" if screenshot_dir else None
            if walkthrough_screenshot_path:
                try:
                    walkthrough_screenshot_path.unlink(missing_ok=True)
                    if walkthrough_advertised:
                        walkthrough_page.locator("#tour-video").screenshot(
                            path=str(walkthrough_screenshot_path),
                            caret="hide",
                            timeout=min(timeout_ms, 30_000),
                        )
                except Exception as exc:
                    screenshot_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            if walkthrough_screenshot_path and walkthrough_advertised:
                walkthrough_checks.append(
                    _check(
                        "walkthrough_screenshot_captured",
                        not screenshot_error,
                        path=str(walkthrough_screenshot_path),
                        error=screenshot_error,
                    )
                )
            checks.extend(walkthrough_checks)
            walkthrough_checks_pass = all(row["ok"] for row in walkthrough_checks)
            walkthrough_result = {
                "url": walkthrough_url,
                "advertised": walkthrough_advertised,
                "state": walkthrough_state,
                "status": (
                    "pass"
                    if walkthrough_advertised and walkthrough_checks_pass
                    else "not_applicable"
                    if walkthrough_checks_pass
                    else "fail"
                ),
            }
            if not walkthrough_advertised:
                walkthrough_result["reason"] = "accepted_walkthrough_not_advertised"
            walkthrough_page.close()
        finally:
            context.close()
            browser.close()
    failed = [row for row in checks if not row.get("ok")]
    return {
        "contract_name": "propertyquarry.3d_browser_gate.v1",
        "generated_at": _utc_now(),
        "status": "pass" if not failed else "fail",
        "base_url": str(base_url).strip(),
        "browser_base_url": browser_base_url,
        "host_header": host_header,
        "demo_slug": slug,
        "providers": providers,
        "viewport": viewport,
        "check_count": len(checks),
        "failed_count": len(failed),
        "checks": checks,
        "provider_results": provider_results,
        "walkthrough_result": walkthrough_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Hard browser-render gate for PropertyQuarry 3D tour controls.")
    parser.add_argument("--base-url", default=os.getenv("PROPERTYQUARRY_LIVE_BASE_URL", "http://localhost:8097"))
    parser.add_argument("--host-header", default=os.getenv("PROPERTYQUARRY_LIVE_HOST_HEADER", "propertyquarry.com"))
    parser.add_argument("--demo-slug", default=DEFAULT_DEMO_SLUG)
    parser.add_argument("--providers", default=",".join(DEFAULT_PROVIDERS))
    parser.add_argument("--timeout-ms", type=int, default=45_000)
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=1000)
    parser.add_argument("--screenshots-dir", default="")
    parser.add_argument(
        "--runtime-container",
        default=os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER", ""),
        help="Explicit runtime container whose tested 3DVista private proof may be updated.",
    )
    parser.add_argument(
        "--public-tour-root",
        action="append",
        default=[],
        help="Explicit public-tour root whose private proof may be updated. Repeatable.",
    )
    parser.add_argument("--write", default="_completion/smoke/property-live-3d-browser-gate-latest.json")
    args = parser.parse_args()
    providers = [item.strip().lower() for item in str(args.providers or "").split(",") if item.strip()]
    receipt = build_browser_gate_receipt(
        base_url=args.base_url,
        host_header=args.host_header,
        demo_slug=args.demo_slug,
        providers=providers or list(DEFAULT_PROVIDERS),
        timeout_ms=max(5_000, int(args.timeout_ms or 45_000)),
        viewport_width=max(320, int(args.viewport_width or 1440)),
        viewport_height=max(480, int(args.viewport_height or 1000)),
        write_screenshots_dir=args.screenshots_dir,
    )
    persistence = persist_3dvista_browser_render_proof_from_receipt(
        receipt,
        runtime_container=str(args.runtime_container or "").strip(),
        public_roots=tuple(args.public_tour_root or ()),
    )
    if "3dvista" in {item.strip().lower() for item in providers or list(DEFAULT_PROVIDERS)}:
        persist_ok = str(persistence.get("status") or "").strip().lower() == "updated"
        receipt.setdefault("checks", []).append(
            _check("3dvista_browser_render_proof_persisted", persist_ok, persistence=persistence)
        )
        receipt["proof_persistence"] = {"3dvista": persistence}
        failed = [row for row in list(receipt.get("checks") or []) if isinstance(row, dict) and not row.get("ok")]
        receipt["check_count"] = len(list(receipt.get("checks") or []))
        receipt["failed_count"] = len(failed)
        receipt["status"] = "pass" if not failed else "fail"
    output = json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
