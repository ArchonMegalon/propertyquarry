#!/usr/bin/env python3
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"json_object_required:{path}")
    return dict(payload)


def _probe_video(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames:format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout or "{}")
    streams = list(payload.get("streams") or [])
    stream = dict(streams[0]) if streams and isinstance(streams[0], dict) else {}
    format_payload = dict(payload.get("format") or {})
    metadata: dict[str, object] = {
        "duration_seconds": round(float(format_payload.get("duration") or 0.0), 3),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "size_bytes": int(format_payload.get("size") or path.stat().st_size),
        "r_frame_rate": str(stream.get("r_frame_rate") or ""),
        "avg_frame_rate": str(stream.get("avg_frame_rate") or ""),
        "nb_frames": int(stream.get("nb_frames") or 0),
    }
    if any(float(metadata[key]) <= 0 for key in ("duration_seconds", "width", "height", "size_bytes")):
        raise RuntimeError("walkthrough_video_probe_invalid")
    return metadata


def build_repair_filter(
    *,
    duration_seconds: float,
    cut_seconds: list[float],
    transition_seconds: float,
    output_fps: float = 30.0,
) -> tuple[str, str, float, list[float]]:
    duration = float(duration_seconds)
    transition = float(transition_seconds)
    cuts = sorted({round(float(value), 3) for value in cut_seconds})
    if duration <= 0.0 or transition <= 0.0 or output_fps <= 0.0 or not cuts:
        raise ValueError("invalid_continuity_repair_contract")
    fps_label = f"{float(output_fps):.6f}".rstrip("0").rstrip(".")
    boundaries = [0.0, *cuts, duration]
    piece_durations = [boundaries[index + 1] - boundaries[index] for index in range(len(boundaries) - 1)]
    if any(value <= transition for value in piece_durations):
        raise ValueError("continuity_repair_cut_spacing_too_short")

    filters: list[str] = []
    for index, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:], strict=True)):
        filters.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
            f"fps={fps_label},scale=1920:1080:force_original_aspect_ratio=decrease,"
            f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[p{index}]"
        )

    current_label = "p0"
    current_duration = piece_durations[0]
    repair_offsets: list[float] = []
    for index in range(1, len(piece_durations)):
        offset = current_duration - transition
        repair_offsets.append(round(offset, 3))
        output_label = f"x{index}"
        filters.append(
            f"[{current_label}][p{index}]xfade=transition=fade:duration={transition:.3f}:"
            f"offset={offset:.3f}[{output_label}]"
        )
        current_label = output_label
        current_duration += piece_durations[index] - transition
    return ";".join(filters), current_label, round(current_duration, 3), repair_offsets


def remap_transition_offsets(
    *,
    source_offsets: list[float],
    cut_seconds: list[float],
    transition_seconds: float,
    repair_offsets: list[float],
) -> list[float]:
    cuts = sorted(float(value) for value in cut_seconds)
    transition = float(transition_seconds)
    remapped = [
        round(float(offset) - transition * len([cut for cut in cuts if cut <= float(offset)]), 3)
        for offset in source_offsets
    ]
    return sorted({*remapped, *(round(float(value), 3) for value in repair_offsets)})


def repair(
    *,
    source_video_path: Path,
    source_receipt_path: Path,
    cut_seconds: list[float],
    transition_seconds: float,
    output_path: Path,
    state_path: Path,
    encoder_preset: str,
    encoder_crf: int,
    ffmpeg_timeout_seconds: float,
) -> dict[str, object]:
    if not source_video_path.is_file() or not source_receipt_path.is_file():
        raise RuntimeError("continuity_repair_source_missing")
    source = _load_json(source_receipt_path)
    if str(source.get("provider_key") or "").strip().lower() != "magicfit":
        raise RuntimeError("continuity_repair_source_not_magicfit")
    if str(source.get("render_status") or "").strip().lower() != "completed":
        raise RuntimeError("continuity_repair_source_incomplete")
    source_sha = _sha256(source_video_path)
    declared_source_sha = str(source.get("video_sha256") or "").strip()
    if declared_source_sha and declared_source_sha != source_sha:
        raise RuntimeError("continuity_repair_source_hash_mismatch")
    declared_source_path = Path(
        str(source.get("video_output_path") or source.get("output_file") or "")
    ).expanduser().resolve()
    if declared_source_path != source_video_path.resolve():
        raise RuntimeError("continuity_repair_source_path_mismatch")

    source_metadata = _probe_video(source_video_path)
    try:
        source_fps = float(Fraction(str(source_metadata.get("avg_frame_rate") or "0")))
    except (ValueError, ZeroDivisionError):
        source_fps = 0.0
    if source_fps <= 0.0:
        raise RuntimeError("continuity_repair_source_frame_rate_invalid")
    filter_graph, final_label, expected_duration, repair_offsets = build_repair_filter(
        duration_seconds=float(source_metadata["duration_seconds"]),
        cut_seconds=cut_seconds,
        transition_seconds=transition_seconds,
        output_fps=source_fps,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_video_path),
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-filter_complex",
        filter_graph,
        "-map",
        f"[{final_label}]",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        encoder_preset,
        "-crf",
        str(encoder_crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-t",
        f"{expected_duration:.3f}",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=max(60.0, float(ffmpeg_timeout_seconds)),
    )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(output_path), "-f", "null", "-"],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output_metadata = _probe_video(output_path)
    required_duration = float(source.get("required_duration_seconds") or 0.0)
    if required_duration > 0.0 and float(output_metadata["duration_seconds"]) + 0.25 < required_duration:
        raise RuntimeError("continuity_repaired_walkthrough_below_required_duration")
    try:
        output_fps = float(Fraction(str(output_metadata.get("avg_frame_rate") or "0")))
    except (ValueError, ZeroDivisionError):
        output_fps = 0.0
    if abs(output_fps - source_fps) > 0.01:
        raise RuntimeError("continuity_repair_frame_rate_mismatch")

    transition_offsets = remap_transition_offsets(
        source_offsets=[float(value) for value in list(source.get("transition_offsets_seconds") or [])],
        cut_seconds=cut_seconds,
        transition_seconds=transition_seconds,
        repair_offsets=repair_offsets,
    )
    payload: dict[str, object] = {
        **source,
        "continuity_repair_status": "pass",
        "continuity_repair_method": "verified_internal_hard_cut_crossfade",
        "continuity_repair_cut_seconds": sorted(round(float(value), 3) for value in cut_seconds),
        "continuity_repair_transition_offsets_seconds": repair_offsets,
        "continuity_repair_transition_seconds": float(transition_seconds),
        "source_composite_video_path": str(source_video_path),
        "source_composite_video_sha256": source_sha,
        "source_composite_receipt_path": str(source_receipt_path),
        "source_composite_receipt_sha256": _sha256(source_receipt_path),
        "transition_offsets_seconds": transition_offsets,
        "transition_seconds": float(transition_seconds),
        "encoder_preset": encoder_preset,
        "encoder_crf": int(encoder_crf),
        "ffmpeg_timeout_seconds": float(ffmpeg_timeout_seconds),
        "video_output_path": str(output_path),
        "output_file": str(output_path),
        "output_contract_ok": True,
        "video_sha256": _sha256(output_path),
        "output_metadata": output_metadata,
        "duration_seconds": float(output_metadata["duration_seconds"]),
        "full_decode_verified": True,
        "source_frame_rate": str(source_metadata.get("avg_frame_rate") or ""),
        "output_frame_rate": str(output_metadata.get("avg_frame_rate") or ""),
        "generated_at": _utc_now(),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair verified internal cuts in a MagicFit property walkthrough.")
    parser.add_argument("--video", required=True)
    parser.add_argument("--source-receipt", required=True)
    parser.add_argument("--cut-second", action="append", type=float, required=True)
    parser.add_argument("--transition-seconds", type=float, default=1.0)
    parser.add_argument("--out", required=True)
    parser.add_argument("--state-json", required=True)
    parser.add_argument("--encoder-preset", choices=("fast", "faster", "veryfast"), default="veryfast")
    parser.add_argument("--encoder-crf", type=int, default=19)
    parser.add_argument("--ffmpeg-timeout-seconds", type=float, default=1800.0)
    args = parser.parse_args()
    payload = repair(
        source_video_path=Path(args.video).expanduser().resolve(),
        source_receipt_path=Path(args.source_receipt).expanduser().resolve(),
        cut_seconds=list(args.cut_second),
        transition_seconds=max(0.1, float(args.transition_seconds)),
        output_path=Path(args.out).expanduser().resolve(),
        state_path=Path(args.state_json).expanduser().resolve(),
        encoder_preset=str(args.encoder_preset),
        encoder_crf=max(0, min(51, int(args.encoder_crf))),
        ffmpeg_timeout_seconds=max(60.0, float(args.ffmpeg_timeout_seconds)),
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
