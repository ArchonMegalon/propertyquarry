#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from property_tour_runtime_paths import preferred_public_tour_root, running_container_public_tour_dir


DEFAULT_DEMO_SLUG = "luxury-residence-with-breathtaking-skyline-views-danubeflats-vienna-layout-first-742df65557"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _check(name: str, ok: bool, **extra: object) -> dict[str, object]:
    return {"name": name, "ok": bool(ok), **extra}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _safe_relpath(value: object) -> str:
    raw_value = str(value or "")
    raw = raw_value.strip()
    if (
        not raw
        or raw != raw_value
        or raw.startswith("/")
        or "\\" in raw
        or "://" in raw
        or "\x00" in raw
    ):
        return ""
    path = PurePosixPath(raw)
    normalized = "/".join(path.parts)
    if (
        path.is_absolute()
        or normalized != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return ""
    return normalized


def _safe_slug(value: object) -> str:
    raw_value = str(value or "")
    raw = raw_value.strip()
    if (
        not raw
        or raw != raw_value
        or raw in {".", ".."}
        or "/" in raw
        or "\\" in raw
        or "://" in raw
        or "\x00" in raw
    ):
        return ""
    return raw


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _provider_proof_media_binding(receipt: dict[str, Any]) -> dict[str, Any]:
    provider_results = [
        dict(row)
        for row in list(receipt.get("provider_results") or [])
        if isinstance(row, dict)
        and str(row.get("provider") or "").strip().lower() == "magicfit"
        and str(row.get("status") or "").strip().lower() == "pass"
    ]
    provenance_rows = [
        dict(row)
        for row in list(receipt.get("provenance_index") or [])
        if isinstance(row, dict)
        and str(row.get("key") or "").strip().lower() == "magicfit"
        and str(row.get("kind") or "").strip().lower() == "media_provider"
        and str(row.get("role") or "").strip().lower() == "walkthrough_media_provider"
        and str(row.get("status") or "").strip().lower() == "pass"
        and row.get("media_authorship") is True
    ]
    provider_result = provider_results[0] if len(provider_results) == 1 else {}
    provenance_row = provenance_rows[0] if len(provenance_rows) == 1 else {}
    slug = _safe_slug(provider_result.get("slug"))
    video_relpath = _safe_relpath(provider_result.get("video_relpath"))
    video_sha256 = str(provider_result.get("video_sha256") or "").strip().lower()
    sha256_valid = len(video_sha256) == 64 and all(
        character in "0123456789abcdef" for character in video_sha256
    )
    binding = {
        "provider": "magicfit",
        "bundle_slug": slug,
        "video_relpath": video_relpath,
        "bundle_media_path": f"{slug}/{video_relpath}" if slug and video_relpath else "",
        "video_sha256": video_sha256,
    }
    provenance_matches = bool(provenance_row) and (
        _safe_slug(provenance_row.get("evidence_bundle_slug")) == slug
        and _safe_relpath(provenance_row.get("evidence_video_relpath")) == video_relpath
        and str(provenance_row.get("evidence_video_sha256") or "").strip().lower()
        == video_sha256
    )
    return {
        "valid": (
            receipt.get("contract_name") == "propertyquarry.walkthrough_provider_proof_gate.v1"
            and receipt.get("status") == "pass"
            and len(provider_results) == 1
            and len(provenance_rows) == 1
            and bool(slug)
            and bool(video_relpath)
            and sha256_valid
            and provenance_matches
        ),
        "provider_result_count": len(provider_results),
        "provenance_row_count": len(provenance_rows),
        "provenance_matches": provenance_matches,
        "binding": binding,
    }


def _timeout_seconds(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _run_json(command: list[str], *, timeout_seconds: float | None = None) -> dict[str, Any]:
    resolved_timeout = _timeout_seconds(timeout_seconds)
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=resolved_timeout,
        )
        payload = json.loads(completed.stdout or "{}")
    except subprocess.TimeoutExpired:
        timeout_label = int(resolved_timeout) if resolved_timeout and float(resolved_timeout).is_integer() else resolved_timeout
        return {
            "_error": f"subprocess_timeout:{timeout_label}s",
            "_timeout_seconds": resolved_timeout,
        }
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _video_metadata(path: Path, *, timeout_seconds: float | None = None) -> dict[str, object]:
    return _run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,nb_frames,r_frame_rate,duration",
            "-show_entries",
            "format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=timeout_seconds,
    )


def _frame_delta_stats(
    path: Path,
    *,
    fps: float = 5.0,
    max_sampled_frames: int = 900,
    transition_offsets_seconds: list[float] | None = None,
    transition_seconds: float = 0.0,
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    try:
        from PIL import Image, ImageChops, ImageStat
    except Exception as exc:
        return {"ok": False, "error": f"PIL unavailable: {exc}"}
    try:
        import cv2
        import numpy as np
    except Exception as exc:
        return {"ok": False, "error": f"OpenCV unavailable: {exc}"}
    duration_seconds = 0.0
    metadata = _video_metadata(path, timeout_seconds=timeout_seconds)
    try:
        format_payload = dict(metadata.get("format") or {}) if isinstance(metadata.get("format"), dict) else {}
        streams = list(metadata.get("streams") or []) if isinstance(metadata.get("streams"), list) else []
        stream = dict(streams[0]) if streams and isinstance(streams[0], dict) else {}
        duration_seconds = float(format_payload.get("duration") or stream.get("duration") or 0.0)
    except Exception:
        duration_seconds = 0.0
    source_width = int(stream.get("width") or 160)
    source_height = int(stream.get("height") or 90)
    target_width = 160
    target_height = max(2, int(round(source_height * target_width / max(source_width, 1))))
    if target_height % 2:
        target_height += 1
    effective_fps = max(0.5, float(fps))
    if duration_seconds > 0.0 and max_sampled_frames > 0:
        effective_fps = min(effective_fps, max(0.5, float(max_sampled_frames) / duration_seconds))
    resolved_timeout = _timeout_seconds(timeout_seconds)
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-threads",
                "1",
                "-i",
                str(path),
                "-vf",
                f"fps={effective_fps:.4f},scale={target_width}:{target_height}:flags=fast_bilinear,format=rgb24",
                "-an",
                "-sn",
                "-frames:v",
                str(max_sampled_frames),
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            check=False,
            capture_output=True,
            timeout=resolved_timeout,
        )
    except subprocess.TimeoutExpired:
        timeout_label = int(resolved_timeout) if resolved_timeout and float(resolved_timeout).is_integer() else resolved_timeout
        return {
            "ok": False,
            "error": f"ffmpeg_frame_sampling_timeout:{timeout_label}s",
            "timeout_seconds": resolved_timeout,
        }
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace") if isinstance(completed.stderr, bytes) else str(completed.stderr or "")
        return {
            "ok": False,
            "error": "ffmpeg_frame_sampling_failed",
            "returncode": completed.returncode,
            "stderr": stderr.strip()[:400],
        }

    frame_size = target_width * target_height * 3
    frame_bytes = bytes(completed.stdout or b"")
    frame_count = len(frame_bytes) // frame_size if frame_size > 0 else 0
    boundaries: list[tuple[str, float]] = []
    transition_span = max(0.0, float(transition_seconds or 0.0))
    for index, raw_offset in enumerate(transition_offsets_seconds or []):
        try:
            offset = float(raw_offset)
        except (TypeError, ValueError):
            continue
        if offset <= 0.0 or (duration_seconds > 0.0 and offset >= duration_seconds):
            continue
        boundaries.append((f"transition_{index + 1}_start", offset))
        end = offset + transition_span
        if transition_span > 0.0 and (duration_seconds <= 0.0 or end < duration_seconds):
            boundaries.append((f"transition_{index + 1}_end", end))

    delta_rows: list[dict[str, object]] = []
    previous = None
    dis_refiner = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    for index in range(frame_count):
        start = index * frame_size
        image = Image.frombytes(
            "RGB",
            (target_width, target_height),
            frame_bytes[start : start + frame_size],
        )
        if previous is not None:
            from_seconds = (index - 1) / effective_fps
            to_seconds = index / effective_fps
            labels = [label for label, timestamp in boundaries if from_seconds <= timestamp <= to_seconds]
            raw_diff = ImageChops.difference(previous, image)
            raw_stat = ImageStat.Stat(raw_diff)
            before_rgb = np.asarray(previous, dtype=np.uint8)
            after_rgb = np.asarray(image, dtype=np.uint8)
            before_gray = cv2.cvtColor(before_rgb, cv2.COLOR_RGB2GRAY)
            after_gray = cv2.cvtColor(after_rgb, cv2.COLOR_RGB2GRAY)
            flow = cv2.calcOpticalFlowFarneback(
                before_gray,
                after_gray,
                None,
                0.5,
                3,
                21,
                3,
                5,
                1.2,
                0,
            )
            grid_x, grid_y = np.meshgrid(
                np.arange(target_width, dtype=np.float32),
                np.arange(target_height, dtype=np.float32),
            )
            map_x = grid_x + flow[..., 0]
            map_y = grid_y + flow[..., 1]
            aligned_after = cv2.remap(
                after_rgb,
                map_x,
                map_y,
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REFLECT,
            )
            compensated_diff = cv2.absdiff(before_rgb, aligned_after)
            valid = (
                (map_x >= 0.0)
                & (map_x <= float(target_width - 1))
                & (map_y >= 0.0)
                & (map_y <= float(target_height - 1))
            )
            compensated_delta = float(compensated_diff[valid].mean()) if bool(valid.any()) else float(compensated_diff.mean())
            flow_method = "farneback"
            if compensated_delta > 32.0:
                refined_flow = dis_refiner.calc(before_gray, after_gray, None)
                refined_map_x = grid_x + refined_flow[..., 0]
                refined_map_y = grid_y + refined_flow[..., 1]
                refined_aligned_after = cv2.remap(
                    after_rgb,
                    refined_map_x,
                    refined_map_y,
                    cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REFLECT,
                )
                refined_diff = cv2.absdiff(before_rgb, refined_aligned_after)
                refined_valid = (
                    (refined_map_x >= 0.0)
                    & (refined_map_x <= float(target_width - 1))
                    & (refined_map_y >= 0.0)
                    & (refined_map_y <= float(target_height - 1))
                )
                refined_delta = (
                    float(refined_diff[refined_valid].mean())
                    if bool(refined_valid.any())
                    else float(refined_diff.mean())
                )
                if refined_delta < compensated_delta:
                    flow = refined_flow
                    compensated_delta = refined_delta
                    flow_method = "dis_medium"
            delta_rows.append(
                {
                    "kind": "declared_transition_boundary" if labels else "local_cadence",
                    "label": ",".join(labels) if labels else f"cadence_{index:04d}",
                    "from_seconds": round(from_seconds, 3),
                    "to_seconds": round(to_seconds, 3),
                    "delta": round(compensated_delta, 3),
                    "raw_delta": round(sum(raw_stat.mean) / len(raw_stat.mean), 3),
                    "mean_flow_pixels": round(float(np.linalg.norm(flow, axis=2).mean()), 3),
                    "flow_method": flow_method,
                }
            )
        previous = image

    deltas = [float(row["delta"]) for row in delta_rows]
    top_rows = sorted(delta_rows, key=lambda row: float(row["delta"]), reverse=True)[:8]
    transition_rows = [
        row for row in delta_rows if row.get("kind") == "declared_transition_boundary"
    ]
    return {
        "ok": frame_count > 1,
        "sampling_mode": "local_cadence_and_declared_transition_boundaries",
        "delta_metric": "hybrid_motion_compensated_mean_absolute_rgb",
        "sampling_fps": round(effective_fps, 4),
        "sample_interval_seconds": round(1.0 / effective_fps, 4),
        "source_duration_seconds": round(duration_seconds, 3),
        "sampled_frame_count": frame_count,
        "delta_count": len(deltas),
        "local_delta_count": len([row for row in delta_rows if row.get("kind") == "local_cadence"]),
        "transition_boundary_delta_count": len(
            [row for row in delta_rows if row.get("kind") == "declared_transition_boundary"]
        ),
        "max_delta": max(deltas) if deltas else 0.0,
        "mean_delta": round(sum(deltas) / len(deltas), 3) if deltas else 0.0,
        "top_deltas": sorted(deltas, reverse=True)[:8],
        "top_delta_samples": top_rows,
        "transition_boundary_samples": transition_rows,
    }


def _video_sidecar_payload(payload: dict[str, Any], *, bundle: Path | None) -> dict[str, Any]:
    sidecar_relpath = str(payload.get("video_sidecar_relpath") or "").strip().lstrip("/")
    if bundle is None or not sidecar_relpath:
        return {}
    sidecar_path = bundle / sidecar_relpath
    return _load_json(sidecar_path) if sidecar_path.is_file() else {}


def _continuity_sampling_context(payload: dict[str, Any], *, bundle: Path) -> dict[str, object]:
    sidecar = _video_sidecar_payload(payload, bundle=bundle)
    source = sidecar or payload
    offsets: list[float] = []
    for raw_offset in list(source.get("transition_offsets_seconds") or []):
        try:
            offset = float(raw_offset)
        except (TypeError, ValueError):
            continue
        if offset > 0.0:
            offsets.append(offset)
    try:
        transition_span = max(0.0, float(source.get("transition_seconds") or 0.0))
    except (TypeError, ValueError):
        transition_span = 0.0
    return {
        "source": "video_sidecar" if sidecar else "tour_manifest",
        "transition_offsets_seconds": offsets,
        "transition_seconds": transition_span,
    }


def _coverage_from_payload(payload: dict[str, Any], *, bundle: Path | None = None) -> dict[str, Any]:
    for key in (
        "walkthrough_coverage_proof",
        "magicfit_walkthrough_coverage",
        "walkthrough_quality_receipt",
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            return dict(value)
    magicfit = payload.get("magicfit_import")
    if isinstance(magicfit, dict):
        for key in ("coverage_proof", "walkthrough_coverage_proof", "quality_receipt"):
            value = magicfit.get(key)
            if isinstance(value, dict):
                return dict(value)
    sidecar = _video_sidecar_payload(payload, bundle=bundle)
    if sidecar:
        for key in (
            "walkthrough_coverage_proof",
            "magicfit_walkthrough_coverage",
            "walkthrough_quality_receipt",
        ):
            value = sidecar.get(key)
            if isinstance(value, dict):
                return dict(value)
        route_labels = sidecar.get("route_labels") or sidecar.get("covered_route_labels") or []
        covered = sidecar.get("covered_route_labels") or sidecar.get("route_labels") or []
        if isinstance(route_labels, list) and isinstance(covered, list) and route_labels and covered:
            expected = [str(item).strip() for item in route_labels if str(item).strip()]
            visited = [str(item).strip() for item in covered if str(item).strip()]
            return {
                "status": "pass" if set(expected).issubset(set(visited)) else "fail",
                "source": "video_sidecar_route_labels",
                "segments_expected": expected,
                "segments_visited": visited,
                "coverage_segments": [
                    {"segment": label, "index": index + 1}
                    for index, label in enumerate(visited)
                ],
            }
    return {}


def _coverage_ok(coverage: dict[str, Any]) -> tuple[bool, dict[str, object]]:
    expected = (
        coverage.get("rooms_expected")
        or coverage.get("expected_rooms")
        or coverage.get("segments_expected")
        or coverage.get("expected_segments")
        or []
    )
    visited = (
        coverage.get("rooms_visited")
        or coverage.get("visited_rooms")
        or coverage.get("segments_visited")
        or coverage.get("visited_segments")
        or []
    )
    if isinstance(expected, str):
        expected = [expected]
    if isinstance(visited, str):
        visited = [visited]
    expected_set = {str(item).strip().lower() for item in expected if str(item).strip()}
    visited_set = {str(item).strip().lower() for item in visited if str(item).strip()}
    missing = sorted(expected_set - visited_set)
    status = str(coverage.get("status") or coverage.get("result") or "").strip().lower()
    raw_segments = (
        coverage.get("room_segments")
        or coverage.get("scene_segments")
        or coverage.get("coverage_segments")
        or coverage.get("segments")
        or []
    )
    segment_count = len([row for row in list(raw_segments) if isinstance(row, dict)])
    ok = status == "pass" and bool(expected_set) and not missing and segment_count >= len(expected_set)
    return ok, {
        "status": status,
        "rooms_expected": sorted(expected_set),
        "rooms_visited": sorted(visited_set),
        "missing_rooms": missing,
        "room_segment_count": segment_count,
    }


def _candidate_has_publish_signal(payload: dict[str, Any]) -> bool:
    provider_key = str(
        payload.get("video_provider")
        or payload.get("video_provider_key")
        or payload.get("video_render_provider")
        or ""
    ).strip().lower()
    coverage = _coverage_from_payload(payload)
    generated_video_providers = {
        "magicfit",
        "onemin_i2v",
        "ea_one_manager_onemin_i2v",
        "poppy_ai",
        "propertyquarry_generated_reconstruction",
    }
    if provider_key in generated_video_providers:
        return bool(coverage)
    if provider_key:
        return True
    if str(payload.get("video_coverage_proof") or "").strip():
        return True
    return bool(coverage)


def _selected_walkthrough_payload(
    payload: dict[str, Any],
    *,
    force_generated_reconstruction: bool = False,
) -> dict[str, Any]:
    active = dict(payload)
    generated = payload.get("generated_reconstruction")
    generated_candidate: dict[str, Any] = {}
    if isinstance(generated, dict):
        generated_video_relpath = str(generated.get("walkthrough_video_relpath") or "").strip()
        if generated_video_relpath:
            generated_candidate = {
                **active,
                "_walkthrough_candidate": "generated_reconstruction",
                "video_relpath": generated_video_relpath,
                "video_provider": "propertyquarry_generated_reconstruction",
                "video_sidecar_relpath": str(generated.get("walkthrough_sidecar_relpath") or "").strip(),
                "video_coverage_proof": "boundary_verified_frame_continuation",
            }
            coverage = generated.get("walkthrough_coverage_proof")
            if isinstance(coverage, dict):
                generated_candidate["walkthrough_coverage_proof"] = dict(coverage)
    if force_generated_reconstruction and generated_candidate:
        return generated_candidate
    active["_walkthrough_candidate"] = "published_video"
    if generated_candidate and (not str(active.get("video_relpath") or "").strip() or not _candidate_has_publish_signal(active)):
        return generated_candidate
    return active


def _provider_bound_walkthrough_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "_walkthrough_candidate": "provider_proof_media",
    }


def _service_generated_reconstruction_slug(receipt: dict[str, Any]) -> str:
    for key in ("slug", "demo_slug"):
        value = str(receipt.get(key) or "").strip()
        if value:
            return value
    details = dict(receipt.get("details") or {}) if isinstance(receipt.get("details"), dict) else {}
    for key in ("slug", "demo_slug"):
        value = str(details.get(key) or "").strip()
        if value:
            return value
    for key in ("tour_url", "viewer_url"):
        raw_url = str(receipt.get(key) or details.get(key) or "").strip()
        if not raw_url:
            continue
        parsed = urllib.parse.urlparse(raw_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "tours":
            return urllib.parse.unquote(parts[1]).strip()
    return ""


def _selected_walkthrough_slug(
    *,
    requested_slug: str,
    service_generated_reconstruction_receipt: dict[str, Any],
) -> tuple[str, str, str]:
    requested = str(requested_slug or "").strip()
    service_slug = _service_generated_reconstruction_slug(service_generated_reconstruction_receipt)
    if service_slug and (not requested or requested == DEFAULT_DEMO_SLUG):
        return service_slug, service_slug, "service_generated_reconstruction_receipt"
    if requested:
        return requested, service_slug, ("requested_demo_slug" if requested != DEFAULT_DEMO_SLUG else "default_demo_slug")
    if service_slug:
        return service_slug, service_slug, "service_generated_reconstruction_receipt"
    return DEFAULT_DEMO_SLUG, "", "default_demo_slug"


def _resolve_walkthrough_tour_root(
    configured_root: str,
    *,
    slug: str,
    force_generated_reconstruction: bool = False,
) -> Path:
    configured_path = Path(configured_root or "state/public_property_tours").expanduser()
    try:
        resolved_configured_path = configured_path.resolve()
    except OSError:
        resolved_configured_path = configured_path
    if (resolved_configured_path / slug / "tour.json").is_file():
        return resolved_configured_path
    repo_root = Path(__file__).resolve().parents[1]
    runtime_container = str(os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "").strip()
    if not runtime_container:
        return resolved_configured_path
    preferred_root = preferred_public_tour_root(
        configured_root=configured_path,
        repo_root=repo_root,
        runtime_container=runtime_container,
    )
    runtime_volume_root = running_container_public_tour_dir(runtime_container)
    candidates: list[Path] = []
    for candidate in (configured_path, runtime_volume_root, preferred_root):
        if candidate is None:
            continue
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved not in candidates:
            candidates.append(resolved)
    scored: list[tuple[int, float, int, Path]] = []
    for index, candidate_root in enumerate(candidates):
        manifest_path = candidate_root / slug / "tour.json"
        if not manifest_path.is_file():
            continue
        payload = _selected_walkthrough_payload(
            _load_json(manifest_path),
            force_generated_reconstruction=force_generated_reconstruction,
        )
        video_relpath = _safe_relpath(payload.get("video_relpath"))
        video_path = (candidate_root / slug / video_relpath).resolve() if video_relpath else candidate_root / "__missing__"
        bundle_dir = (candidate_root / slug).resolve()
        has_video = int(
            bool(video_relpath)
            and bundle_dir in video_path.parents
            and video_path.is_file()
        )
        try:
            manifest_mtime = float(manifest_path.stat().st_mtime)
        except OSError:
            manifest_mtime = 0.0
        scored.append((has_video, manifest_mtime, -index, candidate_root))
    if scored:
        return max(scored)[3]
    return preferred_root.resolve()


def _copy_runtime_bundle_to_temp_root(slug: str, *, container_name: str = "") -> tempfile.TemporaryDirectory[str] | None:
    docker_bin = shutil.which("docker")
    if not docker_bin or not slug:
        return None
    normalized_container = str(
        container_name or os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or ""
    ).strip()
    if not normalized_container:
        return None
    tmp_root = tempfile.TemporaryDirectory(prefix="pq-walkthrough-runtime-bundle-")
    destination_root = Path(tmp_root.name)
    completed = subprocess.run(
        [
            docker_bin,
            "cp",
            f"{normalized_container}:/data/public_property_tours/{slug}",
            str(destination_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    copied_manifest = destination_root / slug / "tour.json"
    if completed.returncode != 0 or not copied_manifest.is_file():
        tmp_root.cleanup()
        return None
    return tmp_root


def build_walkthrough_quality_receipt(
    *,
    tour_root: str,
    demo_slug: str,
    max_jump_delta: float,
    min_duration_seconds: float,
    ffprobe_timeout_seconds: float = 20.0,
    frame_sample_timeout_seconds: float = 45.0,
    service_generated_reconstruction_receipt_path: str = "",
    provider_proof_receipt_path: str = "",
    release_commit_sha: str = "",
    image_digest: str = "",
) -> dict[str, object]:
    service_receipt_path = Path(str(service_generated_reconstruction_receipt_path or "").strip()) if str(service_generated_reconstruction_receipt_path or "").strip() else None
    service_receipt = _load_json(service_receipt_path) if service_receipt_path is not None and service_receipt_path.is_file() else {}
    provider_receipt_path = Path(str(provider_proof_receipt_path or "").strip()) if str(provider_proof_receipt_path or "").strip() else None
    provider_receipt = _load_json(provider_receipt_path) if provider_receipt_path is not None and provider_receipt_path.is_file() else {}
    provider_binding_details = _provider_proof_media_binding(provider_receipt)
    expected_provider_binding = dict(provider_binding_details.get("binding") or {})
    provider_binding_required = provider_receipt_path is not None
    requested_slug = str(demo_slug or DEFAULT_DEMO_SLUG).strip()
    if provider_binding_required:
        slug = _safe_slug(expected_provider_binding.get("bundle_slug"))
        service_slug = _service_generated_reconstruction_slug(service_receipt)
        selection_source = "provider_proof_receipt"
    else:
        slug, service_slug, selection_source = _selected_walkthrough_slug(
            requested_slug=requested_slug,
            service_generated_reconstruction_receipt=service_receipt,
        )
    temp_root: tempfile.TemporaryDirectory[str] | None = None
    force_generated_reconstruction = (
        not provider_binding_required and bool(service_slug) and slug == service_slug
    )
    lookup_slug = slug or "__invalid_provider_binding__"
    root = (
        Path(tour_root or "state/public_property_tours").expanduser().resolve()
        if provider_binding_required
        else _resolve_walkthrough_tour_root(
            tour_root,
            slug=lookup_slug,
            force_generated_reconstruction=force_generated_reconstruction,
        )
    )
    bundle = root / lookup_slug
    bundle_path_safe = bundle.parent == root and not bundle.is_symlink()
    manifest_payload = _load_json(bundle / "tour.json") if bundle_path_safe else {}
    payload = (
        _provider_bound_walkthrough_payload(manifest_payload)
        if provider_binding_required
        else _selected_walkthrough_payload(
            manifest_payload,
            force_generated_reconstruction=force_generated_reconstruction,
        )
    )
    initial_video_relpath = _safe_relpath(payload.get("video_relpath"))
    initial_video_path = (bundle / initial_video_relpath).resolve() if initial_video_relpath else bundle / "__missing__"
    initial_inside_bundle = bool(
        bundle_path_safe
        and initial_video_relpath
        and bundle.resolve() in initial_video_path.parents
    )
    if (
        not manifest_payload
        or not initial_video_relpath
        or not initial_inside_bundle
        or not initial_video_path.is_file()
    ) and slug and not provider_binding_required:
        temp_root = _copy_runtime_bundle_to_temp_root(slug)
        if temp_root is not None:
            root = Path(temp_root.name)
            bundle = root / slug
            manifest_payload = _load_json(bundle / "tour.json")
            payload = _selected_walkthrough_payload(
                manifest_payload,
                force_generated_reconstruction=force_generated_reconstruction,
            )
    checks: list[dict[str, object]] = []
    if provider_binding_required:
        checks.extend(
            (
                _check(
                    "walkthrough_provider_proof_receipt_present",
                    bool(provider_receipt),
                    receipt_path=str(provider_receipt_path),
                ),
                _check(
                    "walkthrough_provider_magicfit_result_unique",
                    int(provider_binding_details.get("provider_result_count") or 0) == 1,
                    provider_result_count=int(
                        provider_binding_details.get("provider_result_count") or 0
                    ),
                ),
                _check(
                    "walkthrough_provider_magicfit_provenance_unique",
                    int(provider_binding_details.get("provenance_row_count") or 0) == 1,
                    provenance_row_count=int(
                        provider_binding_details.get("provenance_row_count") or 0
                    ),
                ),
                _check(
                    "walkthrough_provider_media_provenance_matches",
                    provider_binding_details.get("valid") is True,
                    expected_binding=expected_provider_binding,
                ),
                _check(
                    "walkthrough_provider_selection_unambiguous",
                    service_receipt_path is None,
                    service_generated_reconstruction_receipt_path=(
                        str(service_receipt_path) if service_receipt_path is not None else ""
                    ),
                ),
            )
        )
    if service_receipt_path is not None:
        checks.append(
            _check(
                "service_generated_reconstruction_receipt_present",
                bool(service_receipt),
                receipt_path=str(service_receipt_path),
            )
        )
        checks.append(
            _check(
                "service_generated_reconstruction_bundle_selected",
                bool(service_slug) and slug == service_slug,
                service_generated_reconstruction_slug=service_slug,
                selected_slug=slug,
                selection_source=selection_source,
            )
        )
    raw_video_relpath = str(payload.get("video_relpath") or "")
    video_relpath = _safe_relpath(raw_video_relpath)
    video_path = (bundle / video_relpath).resolve() if video_relpath else Path()
    inside_bundle = bool(
        bundle_path_safe
        and video_relpath
        and bundle.resolve() in video_path.parents
    )
    checks.append(
        _check(
            "walkthrough_bundle_path_inside_tour_root",
            bundle_path_safe,
            bundle_path=str(bundle),
            tour_root=str(root),
        )
    )
    checks.append(_check("tour_manifest_present", bool(manifest_payload), manifest=str(bundle / "tour.json")))
    if service_receipt_path is not None:
        checks.append(
            _check(
                "walkthrough_candidate_matches_service_generated_reconstruction",
                str(payload.get("_walkthrough_candidate") or "").strip() == "generated_reconstruction",
                walkthrough_candidate=str(payload.get("_walkthrough_candidate") or "").strip(),
            )
        )
    checks.append(
        _check(
            "walkthrough_video_declared",
            bool(video_relpath),
            video_relpath=video_relpath,
            raw_video_relpath=raw_video_relpath,
        )
    )
    checks.append(
        _check(
            "walkthrough_video_path_inside_bundle",
            inside_bundle,
            video_path=str(video_path),
        )
    )
    checks.append(_check("walkthrough_video_file_present", bool(inside_bundle and video_path.is_file()), video_path=str(video_path)))

    actual_video_sha256 = (
        _sha256(video_path) if inside_bundle and video_path.is_file() else ""
    )
    provider_media_binding = (
        {
            "provider": "magicfit",
            "bundle_slug": slug,
            "video_relpath": video_relpath,
            "bundle_media_path": f"{slug}/{video_relpath}" if slug and video_relpath else "",
            "video_sha256": actual_video_sha256,
        }
        if provider_binding_required
        else {}
    )
    if provider_binding_required:
        checks.extend(
            (
                _check(
                    "walkthrough_provider_media_path_matches",
                    bool(slug)
                    and slug == str(expected_provider_binding.get("bundle_slug") or "")
                    and video_relpath
                    == str(expected_provider_binding.get("video_relpath") or ""),
                    expected_binding=expected_provider_binding,
                    actual_binding=provider_media_binding,
                ),
                _check(
                    "walkthrough_provider_media_sha256_matches",
                    bool(actual_video_sha256)
                    and actual_video_sha256
                    == str(expected_provider_binding.get("video_sha256") or ""),
                    expected_video_sha256=str(
                        expected_provider_binding.get("video_sha256") or ""
                    ),
                    actual_video_sha256=actual_video_sha256,
                ),
            )
        )

    metadata = (
        _video_metadata(video_path, timeout_seconds=ffprobe_timeout_seconds)
        if inside_bundle and video_path.is_file()
        else {}
    )
    format_payload = dict(metadata.get("format") or {}) if isinstance(metadata.get("format"), dict) else {}
    streams = list(metadata.get("streams") or []) if isinstance(metadata.get("streams"), list) else []
    stream = dict(streams[0]) if streams and isinstance(streams[0], dict) else {}
    metadata_error = str(metadata.get("_error") or "").strip()
    checks.append(
        _check(
            "walkthrough_video_metadata_available",
            bool(metadata) and not metadata_error,
            metadata_error=metadata_error,
        )
    )
    try:
        duration = float(format_payload.get("duration") or stream.get("duration") or 0)
    except Exception:
        duration = 0.0
    checks.append(
        _check(
            "walkthrough_duration_floor",
            duration >= min_duration_seconds,
            duration_seconds=round(duration, 3),
            min_duration_seconds=min_duration_seconds,
        )
    )

    coverage = _coverage_from_payload(payload, bundle=bundle)
    coverage_ok, coverage_summary = _coverage_ok(coverage)
    checks.append(_check("walkthrough_room_coverage_receipt_present", bool(coverage), coverage=coverage_summary))
    checks.append(_check("walkthrough_room_coverage_complete", coverage_ok, coverage=coverage_summary))

    continuity_context = _continuity_sampling_context(payload, bundle=bundle)
    frame_delta_kwargs: dict[str, object] = {"timeout_seconds": frame_sample_timeout_seconds}
    transition_offsets = list(continuity_context.get("transition_offsets_seconds") or [])
    if transition_offsets:
        frame_delta_kwargs.update(
            {
                "transition_offsets_seconds": transition_offsets,
                "transition_seconds": float(continuity_context.get("transition_seconds") or 0.0),
            }
        )
    delta_stats = (
        _frame_delta_stats(video_path, **frame_delta_kwargs)
        if inside_bundle and video_path.is_file()
        else {"ok": False}
    )
    max_delta = float(delta_stats.get("max_delta") or 0)
    checks.append(_check("walkthrough_frame_samples_available", bool(delta_stats.get("ok")), frame_delta_stats=delta_stats))
    checks.append(
        _check(
            "walkthrough_frame_jump_limit",
            bool(delta_stats.get("ok")) and max_delta <= max_jump_delta,
            frame_delta_stats=delta_stats,
            max_jump_delta=max_jump_delta,
        )
    )
    failed = [row for row in checks if not row.get("ok")]
    try:
        return {
            "contract_name": "propertyquarry.walkthrough_quality_gate.v1",
            "generated_at": _utc_now(),
            "release_commit_sha": str(release_commit_sha or "").strip().lower(),
            "image_digest": str(image_digest or "").strip().lower(),
            "status": "pass" if not failed else "fail",
            "tour_root": str(root),
            "demo_slug": slug,
            "requested_demo_slug": requested_slug,
            "selection_source": selection_source,
            "service_generated_reconstruction_slug": service_slug,
            "service_generated_reconstruction_receipt_path": str(service_receipt_path) if service_receipt_path is not None else "",
            "provider_proof_receipt_path": str(provider_receipt_path) if provider_receipt_path is not None else "",
            "provider_proof_receipt_sha256": (
                _sha256(provider_receipt_path)
                if provider_receipt_path is not None and provider_receipt_path.is_file()
                else ""
            ),
            "video_relpath": video_relpath,
            "video_sha256": actual_video_sha256,
            "provider_media_binding": provider_media_binding,
            "walkthrough_candidate": str(payload.get("_walkthrough_candidate") or "").strip(),
            "continuity_sampling_context": continuity_context,
            "check_count": len(checks),
            "failed_count": len(failed),
            "checks": checks,
            "notes": [
                "A video file alone is not walkthrough readiness.",
                "Gold requires explicit room coverage proof and a frame-delta continuity check.",
            ],
        }
    finally:
        if temp_root is not None:
            temp_root.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="Hard walkthrough quality gate for PropertyQuarry.")
    parser.add_argument("--tour-root", default=os.getenv("EA_PUBLIC_TOUR_DIR", "state/public_property_tours"))
    parser.add_argument("--demo-slug", default=DEFAULT_DEMO_SLUG)
    parser.add_argument("--max-jump-delta", type=float, default=42.0)
    parser.add_argument("--min-duration-seconds", type=float, default=30.0)
    parser.add_argument(
        "--ffprobe-timeout-seconds",
        type=float,
        default=float(os.getenv("PROPERTYQUARRY_WALKTHROUGH_QUALITY_FFPROBE_TIMEOUT_SECONDS", "20") or 20),
    )
    parser.add_argument(
        "--frame-sample-timeout-seconds",
        type=float,
        default=float(os.getenv("PROPERTYQUARRY_WALKTHROUGH_QUALITY_FRAME_SAMPLE_TIMEOUT_SECONDS", "45") or 45),
    )
    parser.add_argument("--service-generated-reconstruction-receipt", default="")
    parser.add_argument("--provider-proof-receipt", default="")
    parser.add_argument("--release-commit-sha", default="")
    parser.add_argument("--image-digest", default="")
    parser.add_argument("--write", default="_completion/smoke/property-live-walkthrough-quality-latest.json")
    args = parser.parse_args()
    receipt = build_walkthrough_quality_receipt(
        tour_root=args.tour_root,
        demo_slug=args.demo_slug,
        max_jump_delta=max(1.0, float(args.max_jump_delta or 42.0)),
        min_duration_seconds=max(1.0, float(args.min_duration_seconds or 30.0)),
        ffprobe_timeout_seconds=max(1.0, float(args.ffprobe_timeout_seconds or 20.0)),
        frame_sample_timeout_seconds=max(1.0, float(args.frame_sample_timeout_seconds or 45.0)),
        service_generated_reconstruction_receipt_path=str(args.service_generated_reconstruction_receipt or "").strip(),
        provider_proof_receipt_path=str(args.provider_proof_receipt or "").strip(),
        release_commit_sha=args.release_commit_sha,
        image_digest=args.image_digest,
    )
    output = json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
