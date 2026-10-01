#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DeliveryVariant:
    key: str
    width: int
    height: int
    crf: int


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


def parse_variant(value: str) -> DeliveryVariant:
    parts = [part.strip() for part in str(value or "").split(":")]
    if len(parts) != 4:
        raise ValueError("variant_must_be_key_width_height_crf")
    key = parts[0].lower()
    if not key or not key.replace("-", "").replace("_", "").isalnum():
        raise ValueError("variant_key_invalid")
    width, height, crf = (int(part) for part in parts[1:])
    if width < 640 or height < 360 or width % 2 or height % 2:
        raise ValueError("variant_dimensions_invalid")
    if crf < 0 or crf > 51:
        raise ValueError("variant_crf_invalid")
    return DeliveryVariant(key=key, width=width, height=height, crf=crf)


def motion_filter(variant: DeliveryVariant, *, fps: int = 60, source_fps: float = 0.0) -> str:
    if source_fps >= float(fps) - 0.01:
        return f"scale={variant.width}:{variant.height}:flags=lanczos,fps={fps}"
    return (
        f"scale={variant.width}:{variant.height}:flags=lanczos,"
        f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
    )


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
        timeout=30,
    )
    payload = json.loads(completed.stdout or "{}")
    streams = list(payload.get("streams") or [])
    stream = dict(streams[0]) if streams and isinstance(streams[0], dict) else {}
    format_payload = dict(payload.get("format") or {})
    return {
        "duration_seconds": round(float(format_payload.get("duration") or 0.0), 3),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "size_bytes": int(format_payload.get("size") or path.stat().st_size),
        "r_frame_rate": str(stream.get("r_frame_rate") or ""),
        "avg_frame_rate": str(stream.get("avg_frame_rate") or ""),
        "nb_frames": int(stream.get("nb_frames") or 0),
        "codec_name": str(stream.get("codec_name") or ""),
    }


def _fps(value: object) -> float:
    try:
        return float(Fraction(str(value or "0")))
    except (ValueError, ZeroDivisionError):
        return 0.0


def render_variants(
    *,
    source_video_path: Path,
    source_receipt_path: Path,
    variants: list[DeliveryVariant],
    output_dir: Path,
    stem: str,
    state_path: Path,
    encoder_preset: str,
    ffmpeg_timeout_seconds: float,
) -> dict[str, object]:
    if not source_video_path.is_file() or not source_receipt_path.is_file():
        raise RuntimeError("motion_variant_source_missing")
    if not variants:
        raise RuntimeError("motion_variant_required")
    if len({variant.key for variant in variants}) != len(variants):
        raise RuntimeError("motion_variant_key_duplicate")
    source = _load_json(source_receipt_path)
    source_sha = _sha256(source_video_path)
    declared_source_sha = str(source.get("video_sha256") or "").strip()
    if declared_source_sha and declared_source_sha != source_sha:
        raise RuntimeError("motion_variant_source_hash_mismatch")
    declared_source_path = Path(
        str(source.get("video_output_path") or source.get("output_file") or "")
    ).expanduser().resolve()
    if declared_source_path != source_video_path.resolve():
        raise RuntimeError("motion_variant_source_path_mismatch")
    source_is_continuity_master = (
        source.get("full_decode_verified") is True
        and str(source.get("continuity_repair_status") or "").strip().lower() == "pass"
    )
    source_is_magicfit_segment = (
        str(source.get("provider_key") or source.get("provider") or "").strip().lower() == "magicfit"
        and str(source.get("provider_backend_key") or "").strip().lower() == "magicfit"
        and str(source.get("render_status") or "").strip().lower() == "completed"
        and source.get("output_contract_ok") is True
    )
    if not source_is_continuity_master and not source_is_magicfit_segment:
        raise RuntimeError("motion_variant_source_unverified")

    source_metadata = _probe_video(source_video_path)
    source_fps = _fps(source_metadata.get("avg_frame_rate"))
    if source_fps <= 0.0:
        raise RuntimeError("motion_variant_source_fps_invalid")
    source_already_interpolated = source_fps >= 59.99
    if source_already_interpolated and str(source.get("motion_interpolation_status") or "").lower() != "pass":
        raise RuntimeError("motion_variant_source_interpolation_unverified")
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for variant in variants:
        output_path = output_dir / f"{stem}-{variant.key}.mp4"
        source_dimensions_match = (
            int(source_metadata.get("width") or 0),
            int(source_metadata.get("height") or 0),
        ) == (variant.width, variant.height)
        copied_without_reencode = source_already_interpolated and source_dimensions_match
        if copied_without_reencode:
            shutil.copy2(source_video_path, output_path)
        else:
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source_video_path),
                "-vf",
                motion_filter(variant, source_fps=source_fps),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                encoder_preset,
                "-crf",
                str(variant.crf),
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
            timeout=600,
        )
        metadata = _probe_video(output_path)
        if (int(metadata["width"]), int(metadata["height"])) != (variant.width, variant.height):
            raise RuntimeError(f"motion_variant_dimensions_mismatch:{variant.key}")
        if abs(_fps(metadata["avg_frame_rate"]) - 60.0) > 0.01:
            raise RuntimeError(f"motion_variant_fps_mismatch:{variant.key}")
        if abs(float(metadata["duration_seconds"]) - float(source_metadata["duration_seconds"])) > 0.25:
            raise RuntimeError(f"motion_variant_duration_mismatch:{variant.key}")
        rows.append(
            {
                "key": variant.key,
                "purpose": "mobile" if variant.width <= 1280 else "desktop",
                "path": str(output_path),
                "sha256": _sha256(output_path),
                "metadata": metadata,
                "encoder": "source_copy" if copied_without_reencode else "libx264",
                "encoder_preset": encoder_preset,
                "encoder_crf": variant.crf,
                "full_decode_verified": True,
                "motion_interpolation_verified": True,
                "delivery_stage_frame_synthesis": not source_already_interpolated,
            }
        )

    provider_key = "propertyquarry_core_gold" if source_is_continuity_master else "magicfit"
    payload: dict[str, object] = {
        "contract_name": "propertyquarry.walkthrough_delivery_variants.v1",
        "status": "pass",
        "provider_key": provider_key,
        "provider_backend_key": provider_key,
        "source_video_path": str(source_video_path),
        "source_video_sha256": source_sha,
        "source_receipt_path": str(source_receipt_path),
        "source_receipt_sha256": _sha256(source_receipt_path),
        "source_metadata": source_metadata,
        "source_kind": "continuity_master" if source_is_continuity_master else "magicfit_segment",
        "source_provider_session_url": str(source.get("page_url") or ""),
        "continuity_repair_status": str(source.get("continuity_repair_status") or "not_required"),
        "continuity_repair_method": str(source.get("continuity_repair_method") or ""),
        "continuity_repair_cut_seconds": list(source.get("continuity_repair_cut_seconds") or []),
        "continuity_repair_transition_offsets_seconds": list(
            source.get("continuity_repair_transition_offsets_seconds") or []
        ),
        "motion_interpolation": {
            "engine": "source_motion_interpolated_60fps" if source_already_interpolated else "ffmpeg_minterpolate",
            "output_fps": 60,
            "frame_synthesis": True,
            "delivery_stage_frame_synthesis": not source_already_interpolated,
            "mi_mode": "mci",
            "mc_mode": "aobmc",
            "me_mode": "bidir",
            "variable_size_block_motion_compensation": True,
            "frame_duplication_only": False,
        },
        "variant_count": len(rows),
        "variants": rows,
        "generated_at": _utc_now(),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Render true 60 fps PropertyQuarry walkthrough delivery variants.")
    parser.add_argument("--source-video", required=True)
    parser.add_argument("--source-receipt", required=True)
    parser.add_argument("--variant", action="append", required=True, help="key:width:height:crf")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--stem", default="walkthrough-smooth-60fps")
    parser.add_argument("--state-json", required=True)
    parser.add_argument("--encoder-preset", choices=("fast", "faster", "veryfast"), default="veryfast")
    parser.add_argument("--ffmpeg-timeout-seconds", type=float, default=5400.0)
    args = parser.parse_args()
    payload = render_variants(
        source_video_path=Path(args.source_video).expanduser().resolve(),
        source_receipt_path=Path(args.source_receipt).expanduser().resolve(),
        variants=[parse_variant(value) for value in args.variant],
        output_dir=Path(args.output_dir).expanduser().resolve(),
        stem=str(args.stem or "walkthrough-smooth-60fps").strip(),
        state_path=Path(args.state_json).expanduser().resolve(),
        encoder_preset=str(args.encoder_preset),
        ffmpeg_timeout_seconds=max(60.0, float(args.ffmpeg_timeout_seconds)),
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
