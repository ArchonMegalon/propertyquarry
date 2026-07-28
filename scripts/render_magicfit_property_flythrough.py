#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

try:
    from property_magicfit_env import load_magicfit_env
    from propertyquarry_playwright_runtime import playwright_chromium_launch_kwargs
except ModuleNotFoundError:
    from scripts.property_magicfit_env import load_magicfit_env
    from scripts.propertyquarry_playwright_runtime import playwright_chromium_launch_kwargs

try:
    from property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        bounded_lane_lock,
        require_bounded_file,
        require_free_disk,
        tour_asset_max_bytes,
    )
except ModuleNotFoundError:
    from scripts.property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        bounded_lane_lock,
        require_bounded_file,
        require_free_disk,
        tour_asset_max_bytes,
    )

MAGICFIT_HOME_URL = "https://magicfit.pushowl.com/home"
MAGICFIT_VIDEO_URL = "https://magicfit.pushowl.com/agents/generate?mode=video"
VIDEO_URL_RE = re.compile(r"https://(?:cdn\.pushowl\.com|media\.powlcdn\.com)/magicfit/[^\"'\s<>]+?\.(?:mp4|webm)(?:[^\"'\s<>]*)?")
NEGATIVE = ", ".join(
    [
        "no storyboard",
        "no slideshow",
        "no empty unfurnished flat",
        "no abrupt cut before final 240 degree sweep",
        "no ending cut before sweep completes",
        "no fade-out before final sweep",
        "no cartoon",
        "no toy diorama",
        "no visible text",
        "no watermark",
        "no broken geometry",
        "no sterile showroom look",
    ]
)
ASPECT_CURRENT_OPTIONS = (
    "Portrait (9:16)",
    "Landscape (16:9)",
    "Square (1:1)",
    "Landscape (4:3)",
    "Portrait (3:4)",
    "Cinematic (21:9)",
    "9:16",
    "16:9",
    "1:1",
    "4:3",
    "3:4",
    "21:9",
)


def _render_output_max_bytes() -> int:
    return bounded_env_int(
        "PROPERTYQUARRY_MAGICFIT_RENDER_MAX_OUTPUT_BYTES",
        default=tour_asset_max_bytes(),
        minimum=1024 * 1024,
        maximum=2 * 1024 * 1024 * 1024,
    )


def _render_retry_limit() -> int:
    return bounded_env_int(
        "PROPERTYQUARRY_MAGICFIT_RENDER_MAX_RETRIES",
        default=3,
        minimum=1,
        maximum=3,
    )


def _bounded_attempts(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 1
    return max(1, min(parsed, _render_retry_limit()))


def load_env() -> None:
    load_magicfit_env()


def arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a MagicFit property flythrough clip.")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--aspect-label", default="Landscape (16:9)")
    parser.add_argument("--timeout-minutes", type=int, default=18)
    parser.add_argument("--model-label", default="")
    parser.add_argument("--state-json", default="")
    parser.add_argument("--first-frame", default="", help="Optional image file to upload as MagicFit first-frame reference.")
    parser.add_argument("--extend-session-url", default="", help="Optional MagicFit session URL whose newest visible video should be continued.")
    parser.add_argument(
        "--extend-library-video-url",
        default="",
        help="MagicFit-hosted source video to select in native Extend mode.",
    )
    parser.add_argument(
        "--extend-source-file",
        default="",
        help="Local copy of --extend-library-video-url used for cumulative-duration and prefix proof.",
    )
    parser.add_argument(
        "--extend-source-receipt",
        default="",
        help="MagicFit receipt proving the hosted and local native Extend source are the same output.",
    )
    parser.add_argument("--property-slug", default="", help="PropertyQuarry tour/property slug this walkthrough is being rendered for.")
    parser.add_argument("--property-title", default="", help="Human property title this walkthrough is being rendered for.")
    parser.add_argument("--property-url", default="", help="Source or hosted property URL this walkthrough is being rendered for.")
    parser.add_argument(
        "--storage-state",
        default=os.environ.get("PROPERTYQUARRY_MAGICFIT_STORAGE_STATE", "state/runtime/magicfit-browser-storage.json"),
        help="Provider-local Playwright storage state used to avoid repeated MagicFit logins.",
    )
    return parser


def magicfit_duration(seconds: int) -> int:
    allowed = [4, 6, 8, 10, 12, 15]
    return min(allowed, key=lambda candidate: (abs(candidate - seconds), -candidate))


def collect_video_urls(text: str) -> list[str]:
    return list(dict.fromkeys(url.replace("\\u0026", "&").rstrip("),]") for url in VIDEO_URL_RE.findall(text or "")))


def url_timestamp(url: str) -> int:
    match = re.search(r"/magicfit/(\d+)-", url)
    if match is None:
        return 0
    try:
        return int(match.group(1))
    except Exception:
        return 0


def choose_newest_video(urls: set[str], baseline: set[str], submitted_at_ms: int) -> str:
    candidates: list[tuple[int, str]] = []
    for url in urls:
        if url in baseline:
            continue
        if "/ik-thumbnail." in url:
            continue
        timestamp = url_timestamp(url)
        if timestamp and timestamp < submitted_at_ms - 120000:
            continue
        candidates.append((timestamp, url))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates else ""


def download(url: str, out_path: Path) -> None:
    maximum_bytes = _render_output_max_bytes()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    require_free_disk(
        out_path.parent,
        reason_prefix="magicfit_render",
        expected_write_bytes=maximum_bytes,
        env_name="PROPERTYQUARRY_MAGICFIT_RENDER_MIN_FREE_BYTES",
    )
    temporary_path: Path | None = None
    total = 0
    try:
        with requests.get(url, timeout=(15, 120), stream=True) as response:
            response.raise_for_status()
            content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if content_type and content_type not in {"video/mp4", "video/webm", "application/octet-stream"}:
                raise RuntimeError("magicfit_video_content_type_invalid")
            raw_length = str(response.headers.get("content-length") or "").strip()
            if raw_length:
                try:
                    declared_length = int(raw_length)
                except ValueError as exc:
                    raise RuntimeError("magicfit_video_content_length_invalid") from exc
                if declared_length <= 0 or declared_length > maximum_bytes:
                    raise RuntimeError("magicfit_video_download_too_large")
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=out_path.parent,
                prefix=f".{out_path.name}.",
                suffix=".download",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > maximum_bytes:
                        raise RuntimeError("magicfit_video_download_too_large")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if total <= 0:
            raise RuntimeError("magicfit_video_download_empty")
        temporary_path.chmod(0o600)
        os.replace(temporary_path, out_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def video_metadata(path: Path) -> dict[str, object]:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration,size",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        payload = json.loads(completed.stdout or "{}")
        streams = list(payload.get("streams") or []) if isinstance(payload, dict) else []
        stream = dict(streams[0]) if streams and isinstance(streams[0], dict) else {}
        format_payload = dict(payload.get("format") or {}) if isinstance(payload, dict) else {}
        return {
            "ok": True,
            "duration_seconds": round(float(format_payload.get("duration") or 0.0), 3),
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "size_bytes": int(format_payload.get("size") or path.stat().st_size),
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "duration_seconds": 0.0,
            "width": 0,
            "height": 0,
            "size_bytes": 0,
            "error": f"{type(exc).__name__}: {exc}"[:240],
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_private_state_receipt(path: Path, payload: dict[str, object]) -> None:
    """Atomically persist the full provider receipt without exposing it on stdout."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def operator_safe_render_summary(
    payload: dict[str, object],
    *,
    private_receipt_written: bool,
) -> dict[str, object]:
    """Return a strict allowlist suitable for stdout and parent-process logs."""

    metadata = (
        dict(payload.get("output_metadata") or {})
        if isinstance(payload.get("output_metadata"), dict)
        else {}
    )

    def _bounded_int(value: object, *, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        return max(0, min(parsed, maximum))

    def _bounded_float(value: object, *, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if not math.isfinite(parsed):
            return 0.0
        return round(max(0.0, min(parsed, maximum)), 3)

    return {
        "schema": "ea.governed_spatial_render_stdout.v1",
        "status": (
            "completed"
            if str(payload.get("render_status") or "").strip().lower() == "completed"
            else "failed"
        ),
        "artifact_kind": "continuous_walkthrough",
        "target_bound": bool(str(payload.get("target_slug") or "").strip()),
        "output_contract_ok": payload.get("output_contract_ok") is True,
        "duration_seconds": _bounded_float(
            metadata.get("duration_seconds"),
            maximum=24 * 60 * 60,
        ),
        "width": _bounded_int(metadata.get("width"), maximum=32_768),
        "height": _bounded_int(metadata.get("height"), maximum=32_768),
        "size_bytes": _bounded_int(
            metadata.get("size_bytes"),
            maximum=2 * 1024 * 1024 * 1024,
        ),
        "private_receipt_written": bool(private_receipt_written),
    }


def output_contract_matches(*, metadata: dict[str, object], duration_seconds: int, aspect_label: str) -> bool:
    if metadata.get("ok") is not True:
        return False
    duration = float(metadata.get("duration_seconds") or 0.0)
    width = int(metadata.get("width") or 0)
    height = int(metadata.get("height") or 0)
    duration_ok = duration + 0.25 >= float(duration_seconds) and duration <= float(duration_seconds) + 2.0
    normalized_aspect = str(aspect_label or "").strip().lower()
    if "landscape" in normalized_aspect or normalized_aspect in {"16:9", "4:3", "21:9"}:
        aspect_ok = width > height
    elif "portrait" in normalized_aspect or normalized_aspect in {"9:16", "3:4"}:
        aspect_ok = height > width
    elif "square" in normalized_aspect or normalized_aspect == "1:1":
        aspect_ok = width > 0 and height > 0 and abs(width - height) <= max(width, height) * 0.05
    else:
        aspect_ok = width > 0 and height > 0
    return duration_ok and aspect_ok


def extension_output_contract_matches(
    *,
    metadata: dict[str, object],
    source_metadata: dict[str, object],
    extension_seconds: int,
) -> bool:
    if metadata.get("ok") is not True or source_metadata.get("ok") is not True:
        return False
    source_duration = float(source_metadata.get("duration_seconds") or 0.0)
    output_duration = float(metadata.get("duration_seconds") or 0.0)
    expected_duration = source_duration + float(extension_seconds)
    dimensions_match = (
        int(metadata.get("width") or 0),
        int(metadata.get("height") or 0),
    ) == (
        int(source_metadata.get("width") or 0),
        int(source_metadata.get("height") or 0),
    )
    return (
        source_duration > 0.0
        and output_duration + 0.75 >= expected_duration
        and output_duration <= expected_duration + 2.0
        and dimensions_match
    )


def _extract_video_frame(video_path: Path, frame_path: Path, *, seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{max(seconds, 0.0):.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(frame_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _frame_rmse_metrics(left_path: Path, right_path: Path) -> dict[str, float]:
    import numpy as np
    from PIL import Image, ImageFilter

    with Image.open(left_path) as left_image, Image.open(right_path) as right_image:
        left_rgb = left_image.convert("RGB")
        right_rgb = right_image.convert("RGB")
        left = np.asarray(left_rgb, dtype=np.float32)
        right = np.asarray(right_rgb, dtype=np.float32)
        left_perceptual = np.asarray(
            left_rgb.resize((96, 54), Image.Resampling.LANCZOS).filter(
                ImageFilter.GaussianBlur(radius=1.2)
            ),
            dtype=np.float32,
        )
        right_perceptual = np.asarray(
            right_rgb.resize((96, 54), Image.Resampling.LANCZOS).filter(
                ImageFilter.GaussianBlur(radius=1.2)
            ),
            dtype=np.float32,
        )
    if left.shape != right.shape:
        raise RuntimeError("magicfit_extension_prefix_dimensions_mismatch")
    return {
        "raw_normalized_rmse": round(
            float(np.sqrt(np.mean(np.square(left - right))) / 255.0),
            6,
        ),
        "perceptual_normalized_rmse": round(
            float(
                np.sqrt(np.mean(np.square(left_perceptual - right_perceptual)))
                / 255.0
            ),
            6,
        ),
    }


def verify_extension_prefix(
    source_path: Path,
    output_path: Path,
    *,
    source_duration_seconds: float,
    rmse_limit: float = 0.08,
) -> dict[str, object]:
    sample_times = sorted(
        {
            round(max(0.25, source_duration_seconds * fraction), 3)
            for fraction in (0.1, 0.35, 0.65, 0.9)
        }
    )
    samples: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="propertyquarry-magicfit-extend-prefix-") as raw_dir:
        temp_dir = Path(raw_dir)
        for index, seconds in enumerate(sample_times):
            source_frame = temp_dir / f"source-{index:02d}.png"
            output_frame = temp_dir / f"output-{index:02d}.png"
            _extract_video_frame(source_path, source_frame, seconds=seconds)
            _extract_video_frame(output_path, output_frame, seconds=seconds)
            metrics = _frame_rmse_metrics(source_frame, output_frame)
            perceptual_rmse = float(metrics["perceptual_normalized_rmse"])
            samples.append(
                {
                    "seconds": seconds,
                    **metrics,
                    "perceptual_rmse_limit": float(rmse_limit),
                    "status": "pass" if perceptual_rmse <= rmse_limit else "fail",
                }
            )
    passed = bool(samples) and all(row["status"] == "pass" for row in samples)
    return {
        "status": "pass" if passed else "fail",
        "sample_count": len(samples),
        "perceptual_rmse_limit": float(rmse_limit),
        "maximum_raw_normalized_rmse": max(
            (float(row["raw_normalized_rmse"]) for row in samples),
            default=1.0,
        ),
        "maximum_perceptual_normalized_rmse": max(
            (float(row["perceptual_normalized_rmse"]) for row in samples),
            default=1.0,
        ),
        "samples": samples,
    }


def set_input_value(locator, value: str) -> None:
    try:
        locator.fill(value, timeout=5000)
    except PlaywrightTimeoutError:
        locator.evaluate(
            """(node, nextValue) => {
                node.scrollIntoView({ block: 'center', inline: 'nearest' });
                node.focus();
                const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                if (descriptor && typeof descriptor.set === 'function') {
                    descriptor.set.call(node, nextValue);
                } else {
                    node.value = nextValue;
                }
                node.dispatchEvent(new Event("input", { bubbles: true }));
                node.dispatchEvent(new Event("change", { bubbles: true }));
            }""",
            value,
        )
    current_value = ""
    with contextlib.suppress(Exception):
        current_value = str(locator.input_value(timeout=3000) or "")
    if current_value != value:
        raise RuntimeError("magicfit_input_fill_unverified")


def persist_storage_state(page, path: Path | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(path))
    path.chmod(0o600)


def goto_with_retries(page, url: str, *, attempts: int = 3) -> None:
    attempts = _bounded_attempts(attempts)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            return
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            page.wait_for_timeout(2000 * attempt)
    if last_error is not None:
        raise last_error


def select_generator_mode(page, mode: str, *, attempts: int = 3) -> None:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"video", "extend"}:
        raise RuntimeError("magicfit_generator_mode_invalid")
    label = "Extend" if normalized_mode == "extend" else "Video"
    expected_text = (
        "Select a video above to extend" if normalized_mode == "extend" else "First Frame"
    )
    attempts = _bounded_attempts(attempts)
    for attempt in range(1, attempts + 1):
        locator = page.get_by_role("button", name=label, exact=True)
        try:
            locator.wait_for(state="attached", timeout=30_000)
            locator.evaluate("node => node.click()")
        except Exception:
            pass
        deadline = time.time() + 20
        while time.time() < deadline:
            if expected_text in visible_body_text(page):
                return
            page.wait_for_timeout(1_000)
        if attempt < attempts:
            goto_with_retries(page, MAGICFIT_VIDEO_URL)
            page.wait_for_timeout(5_000)
    raise RuntimeError(f"magicfit_generator_mode_unavailable:{normalized_mode}")


def maybe_login(page, *, storage_state_path: Path | None = None) -> None:
    print("magicfit: open home", flush=True)
    body = ""
    for page_attempt in range(1, 4):
        goto_with_retries(page, MAGICFIT_HOME_URL)
        deadline = time.time() + 30
        while time.time() < deadline and not body:
            page.wait_for_timeout(1000)
            body = visible_body_text(page)
        if body:
            break
        if page_attempt < 3:
            page.wait_for_timeout(2000 * page_attempt)
    if not body:
        write_debug_snapshot(page, poll_count=0, label="home-unreadable")
        raise RuntimeError("magicfit_home_unreadable")
    if not re.search(r"login|sign in|email|password", body, re.I):
        persist_storage_state(page, storage_state_path)
        print("magicfit: already logged in", flush=True)
        return
    email = (os.environ.get("PROPERTYQUARRY_MAGICFIT_EMAIL") or os.environ.get("MAGICFIT_EMAIL") or "").strip()
    password = (os.environ.get("PROPERTYQUARRY_MAGICFIT_PASSWORD") or os.environ.get("MAGICFIT_PASSWORD") or "").strip()
    if not email or not password:
        raise RuntimeError("magicfit_credentials_missing")
    email_field = page.locator("input[type=email], input[name*=email i], input[placeholder*=email i]").first
    if email_field.count():
        set_input_value(email_field, email)
    password_field = page.locator("input[type=password]").first
    if password_field.count():
        set_input_value(password_field, password)
    login_confirmed = False
    for attempt in range(1, 4):
        submit = page.get_by_role("button", name=re.compile(r"sign in|login|continue|submit", re.I)).first
        if submit.count():
            try:
                submit.evaluate("node => node.click()")
            except Exception:
                with contextlib.suppress(Exception):
                    submit.click(timeout=10000, force=True, no_wait_after=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            password_visible = False
            with contextlib.suppress(Exception):
                password_visible = bool(password_field.count() and password_field.is_visible(timeout=500))
            if not password_visible:
                login_confirmed = True
                break
        if login_confirmed:
            break
        if attempt < 3:
            with contextlib.suppress(Exception):
                set_input_value(email_field, email)
                set_input_value(password_field, password)
    if not login_confirmed:
        write_debug_snapshot(page, poll_count=0, label="login-failed")
        raise RuntimeError("magicfit_login_not_confirmed")
    page.wait_for_timeout(3000)
    persist_storage_state(page, storage_state_path)
    print("magicfit: login submitted", flush=True)


def select_button(page, current_text: str, option_text: str) -> None:
    try:
        page.get_by_role("button", name=current_text).last.click(timeout=10000)
        page.wait_for_timeout(500)
        page.get_by_text(option_text, exact=True).last.click(timeout=10000)
        page.wait_for_timeout(500)
    except PlaywrightTimeoutError:
        pass


def option_label_candidates(option_text: str) -> list[str]:
    normalized = str(option_text or "").strip()
    candidates = [normalized] if normalized else []
    ratio_match = re.search(r"\b(?:1:1|3:4|4:3|9:16|16:9|21:9)\b", normalized)
    if ratio_match and ratio_match.group(0) not in candidates:
        candidates.append(ratio_match.group(0))
    return candidates


def visible_current_option(page, option_text: str) -> bool:
    for candidate_text in option_label_candidates(option_text):
        locator = page.get_by_role("button", name=candidate_text, exact=True)
        with contextlib.suppress(Exception):
            for index in range(locator.count()):
                if locator.nth(index).is_visible(timeout=1000):
                    return True
    return False


def click_visible_option_text(page, option_text: str) -> bool:
    ratio_match = re.fullmatch(r"(\d+)\s*:\s*(\d+)", str(option_text or "").strip())
    name_pattern = (
        re.compile(rf"^\s*{ratio_match.group(1)}\s*:\s*{ratio_match.group(2)}\s*$")
        if ratio_match
        else re.compile(rf"^\s*{re.escape(str(option_text or '').strip())}\s*$")
    )
    locators = (
        page.get_by_role("menuitem", name=name_pattern),
        page.get_by_role("option", name=name_pattern),
        page.get_by_role("button", name=name_pattern),
        page.get_by_text(name_pattern),
    )
    for locator in locators:
        with contextlib.suppress(Exception):
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if candidate.is_visible(timeout=500):
                    candidate.click(timeout=4000, force=True)
                    return True
    return False


def wait_for_current_option(page, option_text: str, *, timeout_ms: int = 5000) -> bool:
    deadline = time.time() + max(timeout_ms, 0) / 1000.0
    while time.time() < deadline:
        if visible_current_option(page, option_text):
            return True
        page.wait_for_timeout(250)
    return visible_current_option(page, option_text)


def select_option_from_known_current(page, *, current_options: list[str], option_text: str) -> bool:
    if visible_current_option(page, option_text):
        return True
    option_candidates = option_label_candidates(option_text)
    for _attempt in range(3):
        opened = False
        for current_text in current_options:
            locator = page.get_by_role("button", name=current_text, exact=True)
            with contextlib.suppress(Exception):
                for index in range(locator.count() - 1, -1, -1):
                    current = locator.nth(index)
                    if not current.is_visible(timeout=500):
                        continue
                    current.click(timeout=4000, force=True)
                    opened = True
                    break
            if opened:
                break
        if opened:
            page.wait_for_timeout(1000)
            for candidate_text in option_candidates:
                if not click_visible_option_text(page, candidate_text):
                    continue
                if wait_for_current_option(page, option_text):
                    return True
        with contextlib.suppress(Exception):
            page.keyboard.press("Escape")
        page.wait_for_timeout(1000)
    return visible_current_option(page, option_text)


def prompt_input_locator(page):
    selectors = [
        '[contenteditable="true"][role="textbox"]',
        'textarea[placeholder*="describe" i]',
        '[role="textbox"]',
        '[contenteditable="true"]',
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        with contextlib.suppress(Exception):
            if locator.count():
                return locator
    return page.locator('[contenteditable="true"][role="textbox"]').first


def fill_prompt(page, prompt: str) -> None:
    box = prompt_input_locator(page)
    deadline = time.time() + 30
    while time.time() < deadline:
        with contextlib.suppress(Exception):
            if box.count() and box.is_visible(timeout=1000):
                break
        page.wait_for_timeout(1000)
        box = prompt_input_locator(page)
    else:
        raise RuntimeError("magicfit_prompt_input_missing")
    box.evaluate(
        """(node) => {
            node.scrollIntoView({ block: 'center', inline: 'nearest' });
            node.focus();
            node.textContent = '';
        }"""
    )
    page.wait_for_timeout(200)
    box.click(timeout=10000, force=True)
    page.keyboard.type(prompt, delay=1)
    page.wait_for_timeout(800)


def wait_for_submit_ready(page, *, timeout_ms: int = 180_000):
    submit = page.locator("form button[type=submit]").last
    deadline = time.time() + max(timeout_ms, 0) / 1000.0
    poll_count = 0
    while time.time() < deadline:
        poll_count += 1
        with contextlib.suppress(Exception):
            if submit.is_visible(timeout=1_000) and submit.is_enabled(timeout=1_000):
                return submit
        raise_if_credit_blocked(page)
        if poll_count == 1 or poll_count % 6 == 0:
            write_debug_snapshot(page, poll_count=poll_count, label="submit-wait")
        page.wait_for_timeout(2_000)
    write_debug_snapshot(page, poll_count=poll_count, label="submit-not-ready")
    raise RuntimeError("magicfit_submit_not_ready_after_upload")


def upload_first_frame(page, image_path: Path) -> None:
    if not image_path.is_file():
        raise RuntimeError(f"magicfit_first_frame_missing:{image_path}")
    require_bounded_file(
        image_path,
        reason_prefix="magicfit_first_frame",
        maximum_bytes=bounded_env_int(
            "PROPERTYQUARRY_MAGICFIT_FIRST_FRAME_MAX_BYTES",
            default=25 * 1024 * 1024,
            minimum=64 * 1024,
            maximum=100 * 1024 * 1024,
        ),
    )
    before_images = 0
    with contextlib.suppress(Exception):
        before_images = int(page.locator("img").count())
    try:
        page.get_by_role("button", name=re.compile(r"first frame", re.I)).first.click(timeout=5000, force=True)
        page.wait_for_timeout(500)
    except PlaywrightTimeoutError:
        pass
    uploaded = False
    try:
        with page.expect_file_chooser(timeout=4000) as chooser_info:
            page.get_by_role("button", name=re.compile(r"first frame|upload", re.I)).first.click(timeout=4000, force=True)
        chooser_info.value.set_files(str(image_path))
        uploaded = True
    except Exception:
        file_input = page.locator("input[type=file][accept*='image']").first
        if not file_input.count():
            page.get_by_text("Upload", exact=True).first.click(timeout=10000, force=True)
            page.wait_for_timeout(1000)
            file_input = page.locator("input[type=file][accept*='image']").first
        file_input.set_input_files(str(image_path))
        uploaded = True
    page.wait_for_timeout(8000)
    remove_visible = False
    after_images = before_images
    with contextlib.suppress(Exception):
        remove_visible = bool(page.get_by_role("button", name=re.compile(r"remove", re.I)).first.is_visible(timeout=1500))
    with contextlib.suppress(Exception):
        after_images = int(page.locator("img").count())
    body = ""
    with contextlib.suppress(Exception):
        body = page.locator("body").inner_text(timeout=3000)
    if not uploaded or (not remove_visible and after_images <= before_images and "First Frame" not in body):
        raise RuntimeError("magicfit_first_frame_upload_unverified")


def upload_extend_video(page, video_path: Path, *, timeout_ms: int = 90_000) -> dict[str, object]:
    if not video_path.is_file():
        raise RuntimeError(f"magicfit_extend_video_missing:{video_path}")
    require_bounded_file(
        video_path,
        reason_prefix="magicfit_extend_video",
        maximum_bytes=_render_output_max_bytes(),
    )
    before_videos = 0
    with contextlib.suppress(Exception):
        before_videos = int(page.locator("video").count())
    asset_picker_opened = False
    try:
        page.get_by_text("Upload", exact=True).first.click(timeout=10_000, force=True)
        page.get_by_text(re.compile(r"Click or drag to upload Video", re.I)).wait_for(
            state="visible", timeout=10_000
        )
        asset_picker_opened = True
    except Exception:
        asset_picker_opened = False
    if not asset_picker_opened:
        raise RuntimeError("magicfit_extend_asset_picker_unavailable")

    uploaded = False
    try:
        with page.expect_file_chooser(timeout=10_000) as chooser_info:
            page.get_by_text(re.compile(r"Click or drag to upload Video", re.I)).click(
                timeout=10_000, force=True
            )
        chooser_info.value.set_files(str(video_path))
        uploaded = True
    except Exception:
        file_input = page.locator("input[type=file][accept*='video']").first
        if file_input.count():
            file_input.set_input_files(str(video_path))
            uploaded = True
    if not uploaded:
        raise RuntimeError("magicfit_extend_video_upload_control_missing")

    deadline = time.time() + max(timeout_ms, 1_000) / 1_000.0
    latest_body = ""
    after_videos = before_videos
    while time.time() < deadline:
        page.wait_for_timeout(1_000)
        latest_body = visible_body_text(page)
        with contextlib.suppress(Exception):
            after_videos = int(page.locator("video").count())
        placeholder_visible = "Select a video above to extend" in latest_body
        if after_videos > before_videos and not placeholder_visible:
            return {
                "status": "pass",
                "source_path": str(video_path),
                "source_size_bytes": video_path.stat().st_size,
                "video_elements_before": before_videos,
                "video_elements_after": after_videos,
                "placeholder_cleared": True,
            }
    raise RuntimeError(
        "magicfit_extend_video_upload_unverified:"
        f"video_elements={after_videos}:placeholder={'Select a video above to extend' in latest_body}"
    )


def provider_asset_path(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.path:
        raise ValueError("magicfit_provider_asset_url_invalid")
    return parsed.path


def validate_extend_source(
    *,
    video_url: str,
    source_path: Path,
    receipt_path: Path,
) -> dict[str, object]:
    if not source_path.is_file():
        raise RuntimeError(f"magicfit_extend_source_missing:{source_path}")
    if not receipt_path.is_file():
        raise RuntimeError(f"magicfit_extend_source_receipt_missing:{receipt_path}")
    require_bounded_file(
        source_path,
        reason_prefix="magicfit_extend_source",
        maximum_bytes=_render_output_max_bytes(),
    )
    require_bounded_file(
        receipt_path,
        reason_prefix="magicfit_extend_source_receipt",
        maximum_bytes=bounded_env_int(
            "PROPERTYQUARRY_MAGICFIT_RECEIPT_MAX_BYTES",
            default=1024 * 1024,
            minimum=1_024,
            maximum=8 * 1024 * 1024,
        ),
    )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("magicfit_extend_source_receipt_invalid") from exc
    if not isinstance(receipt, dict):
        raise RuntimeError("magicfit_extend_source_receipt_object_required")
    if str(receipt.get("provider_key") or receipt.get("provider") or "").strip().lower() != "magicfit":
        raise RuntimeError("magicfit_extend_source_provider_mismatch")
    if str(receipt.get("provider_backend_key") or "").strip().lower() != "magicfit":
        raise RuntimeError("magicfit_extend_source_backend_mismatch")
    if str(receipt.get("render_status") or "").strip().lower() != "completed":
        raise RuntimeError("magicfit_extend_source_render_incomplete")
    if receipt.get("output_contract_ok") is not True:
        raise RuntimeError("magicfit_extend_source_contract_failed")
    declared_path = Path(str(receipt.get("output_file") or "")).expanduser().resolve()
    if declared_path != source_path.resolve():
        raise RuntimeError("magicfit_extend_source_file_mismatch")
    declared_url = str(
        receipt.get("video_output_url")
        or receipt.get("hosted_walkthrough_video_url")
        or ""
    ).strip()
    if provider_asset_path(declared_url) != provider_asset_path(video_url):
        raise RuntimeError("magicfit_extend_source_url_mismatch")
    metadata = video_metadata(source_path)
    if metadata.get("ok") is not True:
        raise RuntimeError("magicfit_extend_source_metadata_failed")
    return {
        "status": "pass",
        "source_file": str(source_path),
        "source_sha256": sha256_file(source_path),
        "source_receipt": str(receipt_path),
        "source_receipt_sha256": sha256_file(receipt_path),
        "source_asset_path": provider_asset_path(video_url),
        "source_metadata": metadata,
    }


def select_extend_library_video(
    page,
    video_url: str,
    *,
    timeout_ms: int = 90_000,
) -> dict[str, object]:
    target_path = provider_asset_path(video_url)
    page.get_by_text("Upload", exact=True).first.click(timeout=10_000, force=True)
    library_tab = page.get_by_text("My Library", exact=True).last
    library_tab.wait_for(state="visible", timeout=20_000)
    try:
        library_tab.click(timeout=10_000, force=True, no_wait_after=True)
    except Exception:
        if "IMAGE TO VIDEO" not in visible_body_text(page):
            raise

    deadline = time.time() + max(timeout_ms, 1_000) / 1_000.0
    while time.time() < deadline:
        if "IMAGE TO VIDEO" in visible_body_text(page):
            break
        page.wait_for_timeout(1_000)
    else:
        raise RuntimeError("magicfit_library_assets_unavailable")
    matched_index = -1
    while time.time() < deadline:
        matched_index = int(
            page.locator("video").evaluate_all(
                """(nodes, path) => nodes.findIndex(node => {
                    try {
                        return new URL(node.currentSrc || node.src || '', window.location.href).pathname === path;
                    } catch (_) {
                        return false;
                    }
                })""",
                target_path,
            )
        )
        if matched_index >= 0:
            break
        page.wait_for_timeout(1_000)
    if matched_index < 0:
        raise RuntimeError(f"magicfit_library_video_not_found:{target_path}")

    selected = page.locator("video").nth(matched_index)
    selected.evaluate(
        """node => {
            node.scrollIntoView({ block: 'center', inline: 'nearest' });
            node.click();
        }"""
    )
    deadline = time.time() + max(timeout_ms, 1_000) / 1_000.0
    latest_body = ""
    while time.time() < deadline:
        page.wait_for_timeout(1_000)
        latest_body = visible_body_text(page)
        if "Select a video above to extend" not in latest_body and "Select Asset" not in latest_body:
            return {
                "status": "pass",
                "source_kind": "magicfit_provider_library_video",
                "source_asset_path": target_path,
                "matched_library_index": matched_index,
                "placeholder_cleared": True,
            }
    raise RuntimeError(
        "magicfit_library_video_selection_unverified:"
        f"placeholder={'Select a video above to extend' in latest_body}:"
        f"asset_picker={'Select Asset' in latest_body}"
    )


def load_session_video_for_extend(page, session_url: str) -> None:
    page.goto(session_url, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(6000)
    videos = page.locator("video")
    selected = False
    for index in range(videos.count()):
        candidate = videos.nth(index)
        box = candidate.bounding_box()
        if box and box.get("width", 0) > 50 and box.get("height", 0) > 50:
            candidate.click(timeout=10000, force=True)
            page.wait_for_timeout(1500)
            selected = True
            break
    if not selected:
        raise RuntimeError("magicfit_extend_source_video_not_visible")
    try:
        page.get_by_role("button", name=re.compile(r"^Tweak$", re.I)).last.click(timeout=10000, force=True)
        page.wait_for_timeout(5000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("magicfit_extend_tweak_unavailable") from exc
    # Tweak opens the selected MagicFit result in the composer with the existing
    # source attached. For video outputs it exposes First Frame / Last Frame
    # controls; keep that state instead of switching to the top-level Extend tab,
    # which expects a separate source selection.


def collect_visible_video_urls(page) -> set[str]:
    urls: set[str] = set()
    html = page.content()
    urls.update(collect_video_urls(html))
    try:
        video_urls = page.locator("video").evaluate_all("(nodes) => nodes.map((v) => v.currentSrc || v.src).filter(Boolean)")
    except Exception:
        video_urls = []
    for url in list(video_urls or []):
        if "magicfit" in str(url):
            urls.add(str(url))
    return urls


def write_debug_snapshot(page, *, poll_count: int, label: str = "poll") -> None:
    debug_dir_raw = str(os.environ.get("MAGICFIT_DEBUG_DIR") or "").strip()
    if not debug_dir_raw:
        return
    debug_dir = Path(debug_dir_raw).expanduser()
    debug_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{label}-{poll_count:03d}"
    with contextlib.suppress(Exception):
        body_text = str(page.locator("body").inner_text(timeout=5000) or "")[:200_000]
        (debug_dir / f"{prefix}.txt").write_text(body_text, encoding="utf-8", errors="replace")
    with contextlib.suppress(Exception):
        (debug_dir / f"{prefix}.url.txt").write_text(str(page.url or ""), encoding="utf-8")
    if str(os.environ.get("MAGICFIT_DEBUG_SCREENSHOTS") or "").strip() != "1":
        return
    with contextlib.suppress(Exception):
        page.screenshot(
            path=str(debug_dir / f"{prefix}.png"),
            full_page=False,
            animations="disabled",
            caret="hide",
            timeout=10000,
        )


def visible_body_text(page) -> str:
    with contextlib.suppress(Exception):
        return str(page.locator("body").inner_text(timeout=5000) or "")
    return ""


def raise_if_credit_blocked(page) -> None:
    body_text = visible_body_text(page)
    if re.search(r"\bnot enough credits\b|\binsufficient credits\b|\bbuy more credits\b", body_text, flags=re.IGNORECASE):
        raise RuntimeError("magicfit_not_enough_credits")


def _run_unlocked() -> int:
    load_env()
    args = arg_parser().parse_args()
    out_path = Path(args.out).resolve()
    maximum_timeout_minutes = bounded_env_int(
        "PROPERTYQUARRY_MAGICFIT_RENDER_MAX_TIMEOUT_MINUTES",
        default=30,
        minimum=1,
        maximum=60,
    )
    requested_timeout_minutes = int(args.timeout_minutes or 0)
    if not 1 <= requested_timeout_minutes <= maximum_timeout_minutes:
        raise RuntimeError("magicfit_timeout_out_of_bounds")
    requested_duration = int(args.duration or 0)
    if not 1 <= requested_duration <= 15:
        raise RuntimeError("magicfit_duration_out_of_bounds")
    raw_prompt = str(args.prompt or "").strip()
    prompt_max_characters = bounded_env_int(
        "PROPERTYQUARRY_MAGICFIT_PROMPT_MAX_CHARACTERS",
        default=6_000,
        minimum=256,
        maximum=20_000,
    )
    if not raw_prompt or len(raw_prompt) > prompt_max_characters:
        raise RuntimeError("magicfit_prompt_size_invalid")
    require_free_disk(
        out_path.parent,
        reason_prefix="magicfit_render",
        expected_write_bytes=_render_output_max_bytes(),
        env_name="PROPERTYQUARRY_MAGICFIT_RENDER_MIN_FREE_BYTES",
    )
    require_free_disk(
        Path(tempfile.gettempdir()),
        reason_prefix="magicfit_render_temp",
        expected_write_bytes=1024 * 1024 * 1024,
        env_name="PROPERTYQUARRY_MAGICFIT_RENDER_MIN_FREE_BYTES",
    )
    state_path = Path(args.state_json).resolve() if args.state_json else None
    first_frame_path = Path(args.first_frame).expanduser().resolve() if args.first_frame else None
    if first_frame_path is not None:
        require_bounded_file(
            first_frame_path,
            reason_prefix="magicfit_first_frame",
            maximum_bytes=bounded_env_int(
                "PROPERTYQUARRY_MAGICFIT_FIRST_FRAME_MAX_BYTES",
                default=25 * 1024 * 1024,
                minimum=64 * 1024,
                maximum=100 * 1024 * 1024,
            ),
        )
    storage_state_path = Path(args.storage_state).expanduser().resolve() if args.storage_state else None
    native_extend_url = str(args.extend_library_video_url or "").strip()
    native_source_path = (
        Path(args.extend_source_file).expanduser().resolve() if args.extend_source_file else None
    )
    native_source_receipt_path = (
        Path(args.extend_source_receipt).expanduser().resolve()
        if args.extend_source_receipt
        else None
    )
    native_extend_requested = bool(native_extend_url)
    if native_extend_requested != bool(native_source_path and native_source_receipt_path):
        raise RuntimeError("magicfit_native_extend_source_contract_incomplete")
    if native_extend_requested and (args.extend_session_url or first_frame_path is not None):
        raise RuntimeError("magicfit_native_extend_source_conflict")
    native_source_proof = (
        validate_extend_source(
            video_url=native_extend_url,
            source_path=native_source_path,
            receipt_path=native_source_receipt_path,
        )
        if native_extend_requested
        and native_source_path is not None
        and native_source_receipt_path is not None
        else None
    )
    prompt = f"{raw_prompt} Global constraints: {NEGATIVE}."
    provider_duration = magicfit_duration(requested_duration)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            **playwright_chromium_launch_kwargs(playwright, args=["--no-sandbox"])
        )
        context_options: dict[str, object] = {
            "viewport": {"width": 1440, "height": 1100},
            "accept_downloads": True,
        }
        if storage_state_path is not None and storage_state_path.is_file():
            context_options["storage_state"] = str(storage_state_path)
        try:
            context = browser.new_context(**context_options)
        except Exception:
            context_options.pop("storage_state", None)
            context = browser.new_context(**context_options)
        page = context.new_page()
        try:
            maybe_login(page, storage_state_path=storage_state_path)
            source_selection: dict[str, object] | None = None
            if args.extend_session_url:
                print("magicfit: open extend source session", flush=True)
                load_session_video_for_extend(page, args.extend_session_url)
            else:
                print("magicfit: open video generator", flush=True)
                goto_with_retries(page, MAGICFIT_VIDEO_URL)
                page.wait_for_timeout(5000)
                select_generator_mode(page, "extend" if native_extend_requested else "video")
                if native_extend_requested:
                    print("magicfit: select native extend source", flush=True)
                    source_selection = select_extend_library_video(page, native_extend_url)
            write_debug_snapshot(page, poll_count=0, label="generator-open")
            baseline = set(list(collect_visible_video_urls(page))[:1_000])
            print(f"magicfit: baseline urls={len(baseline)}", flush=True)
            model_selected = None
            if args.model_label:
                model_selected = select_option_from_known_current(
                    page,
                    current_options=[
                        "Seedance 2.0 Fast",
                        "Seedance 2.0",
                        "Kling 3.0",
                        "Kling 2.6 Pro",
                        "Kling O1",
                        "VEO 3.1 Fast",
                        "VEO 3.1",
                        "Veo 3.1 Fast",
                        "Veo 3.1",
                    ],
                    option_text=args.model_label,
                )
                print(f"magicfit: model configured selected={bool(model_selected)}", flush=True)
                if not model_selected:
                    raise RuntimeError("magicfit_model_selection_unverified")
            aspect_selected: object = "source_locked"
            if not native_extend_requested:
                aspect_selected = select_option_from_known_current(
                    page,
                    current_options=list(ASPECT_CURRENT_OPTIONS),
                    option_text=args.aspect_label,
                )
            print(f"magicfit: aspect configured selected={bool(aspect_selected)}", flush=True)
            if aspect_selected is False:
                raise RuntimeError("magicfit_aspect_selection_unverified")
            duration_selected = select_option_from_known_current(
                page,
                current_options=["4s", "5s", "6s", "7s", "8s", "9s", "10s", "11s", "12s", "13s", "14s", "15s"],
                option_text=f"{provider_duration}s",
            )
            print(f"magicfit: duration_target={provider_duration}s selected={duration_selected}", flush=True)
            if not duration_selected:
                raise RuntimeError("magicfit_duration_selection_unverified")
            write_debug_snapshot(page, poll_count=0, label="generator-configured")
            if first_frame_path is not None:
                print("magicfit: upload first frame", flush=True)
                upload_first_frame(page, first_frame_path)
            fill_prompt(page, prompt)
            write_debug_snapshot(page, poll_count=0, label="ready-to-submit")
            events: deque[dict[str, object]] = deque(maxlen=200)
            seen_urls: set[str] = set()

            def handle_response(response) -> None:
                url = response.url
                if "magicfit" not in url and "pushowl" not in url:
                    return
                item = {
                    "method": response.request.method,
                    "status": response.status,
                    "url": url,
                    "content_type": response.headers.get("content-type", ""),
                }
                events.append(item)
                if re.search(r"(?:cdn\.pushowl\.com|media\.powlcdn\.com)/magicfit/.*\.(mp4|webm)(?:$|\?)", url):
                    if len(seen_urls) < 1_000:
                        seen_urls.add(url)
                body = ""
                raw_length = str(response.headers.get("content-length") or "").strip()
                if re.search(r"json|script|text", item["content_type"], re.I) and raw_length.isdigit() and int(raw_length) <= 1024 * 1024:
                    try:
                        body = response.text()
                    except Exception:
                        body = ""
                if body:
                    for found_url in collect_video_urls(body):
                        if len(seen_urls) >= 1_000:
                            break
                        seen_urls.add(found_url)

            page.on("response", handle_response)
            submitted_at_ms = int(time.time() * 1000)
            submit = wait_for_submit_ready(page)
            print("magicfit: submit job", flush=True)
            submit.click(timeout=30000)
            page.wait_for_timeout(3000)
            write_debug_snapshot(page, poll_count=0, label="submitted")
            raise_if_credit_blocked(page)
            deadline = time.time() + requested_timeout_minutes * 60
            video_url = ""
            poll_count = 0
            while time.time() < deadline and not video_url:
                page.wait_for_timeout(10000)
                poll_count += 1
                raise_if_credit_blocked(page)
                for found_url in collect_visible_video_urls(page):
                    if len(seen_urls) >= 1_000:
                        break
                    seen_urls.add(found_url)
                video_url = choose_newest_video(seen_urls, baseline, submitted_at_ms)
                print(f"magicfit: poll={poll_count} seen_urls={len(seen_urls)} found={bool(video_url)}", flush=True)
                if poll_count <= 3 or poll_count % 12 == 0:
                    write_debug_snapshot(page, poll_count=poll_count)
            if not video_url:
                write_debug_snapshot(page, poll_count=poll_count, label="failed")
                raise RuntimeError("magicfit_video_url_not_found")
            print("magicfit: download clip", flush=True)
            download(video_url, out_path)
            output_metadata = video_metadata(out_path)
            extension_prefix_proof: dict[str, object] | None = None
            if native_extend_requested and native_source_proof is not None and native_source_path is not None:
                extension_prefix_proof = verify_extension_prefix(
                    native_source_path,
                    out_path,
                    source_duration_seconds=float(
                        dict(native_source_proof.get("source_metadata") or {}).get(
                            "duration_seconds"
                        )
                        or 0.0
                    ),
                )
                output_contract_ok = extension_output_contract_matches(
                    metadata=output_metadata,
                    source_metadata=dict(native_source_proof.get("source_metadata") or {}),
                    extension_seconds=provider_duration,
                ) and extension_prefix_proof.get("status") == "pass"
            else:
                output_contract_ok = output_contract_matches(
                    metadata=output_metadata,
                    duration_seconds=provider_duration,
                    aspect_label=args.aspect_label,
                )
            payload = {
                "provider": "magicfit",
                "provider_key": "magicfit",
                "provider_backend_key": "magicfit",
                "render_status": "completed",
                "provider_operation": (
                    "native_video_extend"
                    if native_extend_requested
                    else "image_to_video_or_tweak"
                ),
                "composition": (
                    "provider_native_cumulative_extension"
                    if native_extend_requested
                    else "provider_native_clip"
                ),
                "postproduction_edit_count": 0,
                "video_output_url": video_url,
                "hosted_walkthrough_video_url": video_url,
                "output_file": str(out_path),
                "target_slug": str(args.property_slug or "").strip(),
                "property_slug": str(args.property_slug or "").strip(),
                "property_title": str(args.property_title or "").strip(),
                "property_url": str(args.property_url or "").strip(),
                "duration_seconds_requested": requested_duration,
                "duration_seconds_magicfit": provider_duration,
                "aspect_label": args.aspect_label,
                "model_label": str(args.model_label or "").strip(),
                "model_selection_reported": model_selected,
                "aspect_selection_reported": aspect_selected,
                "duration_selection_reported": duration_selected,
                "native_extend_source_selection": source_selection,
                "native_extend_source_proof": native_source_proof,
                "native_extend_prefix_proof": extension_prefix_proof,
                "output_metadata": output_metadata,
                "output_contract_ok": output_contract_ok,
                "prompt": prompt,
                "page_url": page.url,
                "events_tail": list(events)[-80:],
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if state_path is not None:
                write_private_state_receipt(state_path, payload)
            if not output_contract_ok:
                raise RuntimeError("magicfit_output_contract_mismatch")
            print(
                json.dumps(
                    operator_safe_render_summary(
                        payload,
                        private_receipt_written=state_path is not None,
                    ),
                    sort_keys=True,
                )
            )
            return 0
        finally:
            context.close()
            browser.close()


def run() -> int:
    with bounded_lane_lock("magicfit-render"):
        return _run_unlocked()


if __name__ == "__main__":
    raise SystemExit(run())
