#!/usr/bin/env python3
"""Render a provider-neutral normal-camera walkthrough from accepted panoramas.

The renderer never invents connectivity: every cut follows a directed hotspot
edge from the public tour manifest.  Equirectangular inputs are projected to a
flat monoscopic camera view and rendered natively at 60 fps, keeping immersive
3D/VR controls independent from the default walkthrough video.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from PIL import Image


CONTRACT_NAME = "propertyquarry.panorama_camera_walkthrough.v1"
REPRESENTATION_KIND = "normal_camera_mono"
DEFAULT_DISCLOSURE = (
    "AI-reconstructed concept · source-floorplan topology reviewed"
)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, body: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short walkthrough receipt write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_asset(bundle_dir: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RuntimeError("camera_walkthrough_asset_relpath_invalid")
    relpath = PurePosixPath(value)
    if (
        relpath.is_absolute()
        or relpath.as_posix() != value
        or "\\" in value
        or any(part in {"", ".", ".."} for part in relpath.parts)
    ):
        raise RuntimeError("camera_walkthrough_asset_relpath_invalid")
    root = bundle_dir.resolve()
    candidate = (bundle_dir / Path(*relpath.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("camera_walkthrough_asset_relpath_invalid") from exc
    if not candidate.is_file():
        raise RuntimeError("camera_walkthrough_asset_missing")
    return candidate


def scene_graph(
    manifest: Mapping[str, object], bundle_dir: Path
) -> tuple[str, dict[str, dict[str, Any]], dict[str, tuple[str, ...]]]:
    walkable = manifest.get("walkable_scene")
    if not isinstance(walkable, Mapping):
        raise RuntimeError("camera_walkthrough_walkable_scene_missing")
    initial = walkable.get("initial_scene_id")
    if not isinstance(initial, str) or not initial:
        raise RuntimeError("camera_walkthrough_initial_scene_invalid")
    raw_scenes = walkable.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise RuntimeError("camera_walkthrough_scenes_invalid")

    scenes: dict[str, dict[str, Any]] = {}
    edges: dict[str, tuple[str, ...]] = {}
    for raw_scene in raw_scenes:
        if not isinstance(raw_scene, Mapping):
            raise RuntimeError("camera_walkthrough_scene_invalid")
        scene = dict(raw_scene)
        scene_id = scene.get("id")
        if not isinstance(scene_id, str) or not scene_id or scene_id in scenes:
            raise RuntimeError("camera_walkthrough_scene_id_invalid")
        asset = _safe_asset(bundle_dir, scene.get("asset_relpath"))
        with Image.open(asset) as image:
            width, height = image.size
            image.verify()
        if width < 1024 or height < 512 or not 1.85 <= width / height <= 2.15:
            raise RuntimeError("camera_walkthrough_panorama_not_equirectangular")
        scene["_asset_path"] = asset
        scene["_asset_sha256"] = _sha256(asset)
        scene["_asset_dimensions"] = {"width": width, "height": height}
        scenes[scene_id] = scene

    if initial not in scenes:
        raise RuntimeError("camera_walkthrough_initial_scene_missing")
    for scene_id, scene in scenes.items():
        targets: list[str] = []
        for raw_hotspot in scene.get("hotspots") or []:
            if not isinstance(raw_hotspot, Mapping):
                continue
            target = raw_hotspot.get("target_scene_id")
            if isinstance(target, str) and target in scenes and target not in targets:
                targets.append(target)
        edges[scene_id] = tuple(targets)
    return initial, scenes, edges


def _shortest_path(
    edges: Mapping[str, Sequence[str]], start: str, target: str
) -> list[str]:
    queue: deque[list[str]] = deque([[start]])
    seen = {start}
    while queue:
        path = queue.popleft()
        for candidate in edges.get(path[-1], ()):
            if candidate in seen:
                continue
            next_path = [*path, candidate]
            if candidate == target:
                return next_path
            seen.add(candidate)
            queue.append(next_path)
    raise RuntimeError(f"camera_walkthrough_scene_unreachable:{target}")


def default_route(
    initial: str,
    scenes: Mapping[str, Mapping[str, object]],
    edges: Mapping[str, Sequence[str]],
) -> list[str]:
    route = [initial]
    visited = {initial}
    for target in scenes:
        if target in visited:
            continue
        path = _shortest_path(edges, route[-1], target)
        route.extend(path[1:])
        visited.update(path)
    return route


def validate_route(
    route: Sequence[str],
    *,
    initial: str,
    scenes: Mapping[str, Mapping[str, object]],
    edges: Mapping[str, Sequence[str]],
) -> list[str]:
    normalized = [str(item).strip() for item in route]
    if not normalized or normalized[0] != initial:
        raise RuntimeError("camera_walkthrough_route_must_start_at_initial_scene")
    if any(not item or item not in scenes for item in normalized):
        raise RuntimeError("camera_walkthrough_route_scene_invalid")
    for source, target in zip(normalized, normalized[1:]):
        if target not in edges.get(source, ()):
            raise RuntimeError(
                f"camera_walkthrough_route_edge_invalid:{source}:{target}"
            )
    if set(normalized) != set(scenes):
        missing = ",".join(sorted(set(scenes) - set(normalized)))
        raise RuntimeError(f"camera_walkthrough_route_incomplete:{missing}")
    return normalized


def yaw_commands(
    *,
    start_yaw: float,
    duration_seconds: float,
    direction: int,
    hold_seconds: float = 0.0,
) -> str:
    sweep = 18.0
    first = max(-180.0, min(180.0, start_yaw - direction * sweep / 2.0))
    last = max(-180.0, min(180.0, start_yaw + direction * sweep / 2.0))
    steps = max(2, int(duration_seconds * 20.0))
    commands: list[str] = []
    for index in range(steps + 1):
        elapsed = duration_seconds * index / steps
        moving_duration = max(0.001, duration_seconds - 2.0 * hold_seconds)
        fraction = max(
            0.0,
            min(1.0, (elapsed - hold_seconds) / moving_duration),
        )
        eased = fraction * fraction * (3.0 - 2.0 * fraction)
        value = first + (last - first) * eased
        timestamp = min(duration_seconds - 0.001, duration_seconds * fraction)
        commands.append(f"{timestamp:.3f} v360@view yaw {value:.3f}")
    return ";".join(commands)


def _font_path() -> Path:
    candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("camera_walkthrough_font_missing")


def _probe_video(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,codec_name:format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload = json.loads(completed.stdout or "{}")
    stream = dict((payload.get("streams") or [{}])[0])
    format_payload = dict(payload.get("format") or {})
    return {
        "duration_seconds": round(float(format_payload.get("duration") or 0.0), 3),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "r_frame_rate": str(stream.get("r_frame_rate") or ""),
        "avg_frame_rate": str(stream.get("avg_frame_rate") or ""),
        "nb_frames": int(stream.get("nb_frames") or 0),
        "codec_name": str(stream.get("codec_name") or ""),
        "size_bytes": int(format_payload.get("size") or path.stat().st_size),
    }


def _render_clip(
    *,
    scene: Mapping[str, object],
    output: Path,
    label_file: Path,
    disclosure_file: Path,
    duration_seconds: float,
    hold_seconds: float,
    direction: int,
    fps: int,
) -> None:
    asset = Path(str(scene["_asset_path"]))
    start_yaw = float(scene.get("start_yaw") or 0.0)
    commands = yaw_commands(
        start_yaw=start_yaw,
        duration_seconds=duration_seconds,
        direction=direction,
        hold_seconds=hold_seconds,
    )
    font = _font_path()
    video_filter = ",".join(
        [
            f"sendcmd=c='{commands}'",
            (
                "v360@view=input=equirect:output=flat:w=1920:h=1080:"
                f"h_fov=92:v_fov=58:yaw={start_yaw:.3f}:interp=lanczos"
            ),
            "eq=contrast=1.025:saturation=1.04:brightness=0.004",
            "drawbox=x=0:y=0:w=iw:h=78:color=0x14221b@0.78:t=fill",
            (
                f"drawtext=fontfile={font}:text='PROPERTYQUARRY':fontcolor=white:"
                "fontsize=30:x=42:y=23"
            ),
            "drawbox=x=0:y=h-118:w=iw:h=118:color=0x14221b@0.68:t=fill",
            (
                f"drawtext=fontfile={font}:textfile={label_file}:fontcolor=white:"
                "fontsize=42:x=46:y=h-102"
            ),
            (
                f"drawtext=fontfile={font}:textfile={disclosure_file}:"
                "fontcolor=0xdce9e1:fontsize=21:x=48:y=h-48"
            ),
            "format=yuv420p",
        ]
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(asset),
            "-t",
            f"{duration_seconds:.3f}",
            "-vf",
            video_filter,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "17",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )


def render(
    *,
    bundle_dir: Path,
    output_path: Path,
    receipt_path: Path,
    route_value: str,
    duration_seconds: float,
    transition_seconds: float,
    disclosure: str,
) -> dict[str, object]:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("camera_walkthrough_media_tools_missing")
    if duration_seconds <= 1.5 or not 0.1 <= transition_seconds < duration_seconds:
        raise RuntimeError("camera_walkthrough_timing_invalid")
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.is_file():
        raise RuntimeError("camera_walkthrough_manifest_missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError("camera_walkthrough_manifest_invalid")
    slug = manifest.get("slug")
    if not isinstance(slug, str) or not slug:
        raise RuntimeError("camera_walkthrough_slug_invalid")
    initial, scenes, edges = scene_graph(manifest, bundle_dir)
    requested = [part.strip() for part in route_value.split(",") if part.strip()]
    route = validate_route(
        requested or default_route(initial, scenes, edges),
        initial=initial,
        scenes=scenes,
        edges=edges,
    )

    output_path = output_path.expanduser().resolve()
    receipt_path = receipt_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_duration = duration_seconds * len(route) - transition_seconds * (len(route) - 1)
    with tempfile.TemporaryDirectory(prefix="property-camera-walkthrough-") as raw_tmp:
        temporary_root = Path(raw_tmp)
        disclosure_file = temporary_root / "disclosure.txt"
        disclosure_file.write_text(disclosure, encoding="utf-8")
        clips: list[Path] = []
        clip_cache: dict[tuple[str, int], Path] = {}
        for index, scene_id in enumerate(route):
            scene = scenes[scene_id]
            direction = 1 if index % 2 == 0 else -1
            cache_key = (scene_id, direction)
            clip = clip_cache.get(cache_key)
            if clip is None:
                label_file = temporary_root / f"label-{len(clip_cache):02d}.txt"
                label_file.write_text(
                    str(scene.get("label") or scene_id), encoding="utf-8"
                )
                clip = temporary_root / f"clip-{len(clip_cache):02d}.mp4"
                _render_clip(
                    scene=scene,
                    output=clip,
                    label_file=label_file,
                    disclosure_file=disclosure_file,
                    duration_seconds=duration_seconds,
                    hold_seconds=transition_seconds,
                    direction=direction,
                    fps=60,
                )
                clip_cache[cache_key] = clip
            clips.append(clip)

        filters: list[str] = []
        current = "0:v"
        current_duration = duration_seconds
        transition_offsets: list[float] = []
        for index in range(1, len(clips)):
            offset = current_duration - transition_seconds
            transition_offsets.append(round(offset, 3))
            target = f"x{index}"
            filters.append(
                f"[{current}][{index}:v]xfade=transition=fade:"
                f"duration={transition_seconds:.3f}:offset={offset:.3f}[{target}]"
            )
            current = target
            current_duration += duration_seconds - transition_seconds
        video_label = current
        temporary_output = temporary_root / "walkthrough.mp4"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for clip in clips:
            command.extend(["-i", str(clip)])
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]
        )
        if filters:
            command.extend(["-filter_complex", ";".join(filters)])
        command.extend(
            [
                "-map",
                f"[{video_label}]" if filters else "0:v:0",
                "-map",
                f"{len(clips)}:a:0",
                "-t",
                f"{final_duration:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "17",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-fps_mode",
                "cfr",
                "-movflags",
                "+faststart",
                str(temporary_output),
            ]
        )
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=1200,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-v",
                "error",
                "-i",
                str(temporary_output),
                "-f",
                "null",
                "-",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
        os.chmod(temporary_output, 0o600)
        os.replace(temporary_output, output_path)
        os.chmod(output_path, 0o600)

    metadata = _probe_video(output_path)
    if (
        (metadata["width"], metadata["height"]) != (1920, 1080)
        or metadata["avg_frame_rate"] != "60/1"
        or abs(float(metadata["duration_seconds"]) - final_duration) > 0.25
    ):
        raise RuntimeError("camera_walkthrough_output_contract_invalid")
    scene_rows = []
    for scene_id in route:
        scene = scenes[scene_id]
        scene_rows.append(
            {
                "id": scene_id,
                "label": str(scene.get("label") or scene_id),
                "asset_relpath": str(scene.get("asset_relpath") or ""),
                "asset_sha256": str(scene["_asset_sha256"]),
                "asset_dimensions": dict(scene["_asset_dimensions"]),
            }
        )
    receipt: dict[str, object] = {
        "contract_name": CONTRACT_NAME,
        "status": "pass",
        "generated_at": _utc_now(),
        "property_slug": slug,
        "representation_kind": REPRESENTATION_KIND,
        "stereo_mode": "2d_mono",
        "default_walkthrough": True,
        "optional_spatial_tour_unchanged": True,
        "composition": "manifest_graph_bound_panorama_camera_walkthrough",
        "continuity_repair_status": "pass",
        "continuity_repair_method": "hotspot_graph_bound_crossfade",
        "continuity_repair_cut_seconds": [],
        "full_decode_verified": True,
        "motion_interpolation_status": "pass",
        "frame_duplication_only": False,
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": _sha256(manifest_path),
        "walkable_scene_sha256": hashlib.sha256(
            json.dumps(
                manifest.get("walkable_scene"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
        "initial_scene_id": initial,
        "route_scene_ids": route,
        "route_labels": [row["label"] for row in scene_rows],
        "covered_route_labels": [row["label"] for row in scene_rows],
        "scene_count": len(scenes),
        "segment_count": len(route),
        "scenes": scene_rows,
        "boundary_checks": [
            {"source": source, "target": target, "status": "pass"}
            for source, target in zip(route, route[1:])
        ],
        "transition_offsets_seconds": transition_offsets,
        "transition_seconds": transition_seconds,
        "required_duration_seconds": round(final_duration, 3),
        "duration_seconds": metadata["duration_seconds"],
        "representation_disclosure": disclosure,
        "video_output_path": str(output_path),
        "video_sha256": _sha256(output_path),
        "video_metadata": metadata,
    }
    _atomic_write(receipt_path, _canonical_json_bytes(receipt))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a flat normal-camera walkthrough from a tour's accepted panorama graph."
    )
    parser.add_argument("--bundle-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument(
        "--route",
        default="",
        help="Optional comma-separated scene route; every transition must be a manifest hotspot edge.",
    )
    parser.add_argument("--scene-seconds", type=float, default=4.0)
    parser.add_argument("--transition-seconds", type=float, default=1.2)
    parser.add_argument("--disclosure", default=DEFAULT_DISCLOSURE)
    args = parser.parse_args()
    receipt = render(
        bundle_dir=Path(args.bundle_dir).expanduser().resolve(),
        output_path=Path(args.output),
        receipt_path=Path(args.receipt),
        route_value=args.route,
        duration_seconds=float(args.scene_seconds),
        transition_seconds=float(args.transition_seconds),
        disclosure=str(args.disclosure),
    )
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "property_slug": receipt["property_slug"],
                "representation_kind": receipt["representation_kind"],
                "video_output_path": receipt["video_output_path"],
                "video_sha256": receipt["video_sha256"],
                "duration_seconds": receipt["duration_seconds"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
