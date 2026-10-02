#!/usr/bin/env python3
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from property_magicfit_delivery_contract import validate_magicfit_source_receipt
except ModuleNotFoundError:
    from scripts.property_magicfit_delivery_contract import (
        validate_magicfit_source_receipt,
    )


SOURCE_RECEIPT_HANDOFF_CONTRACT = (
    "propertyquarry.magicfit_source_receipt_handoff.v1"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"json_object_required:{path}")
    return dict(loaded)


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
    stream = dict(streams[0]) if streams else {}
    format_payload = dict(payload.get("format") or {})
    metadata = {
        "duration_seconds": round(float(format_payload.get("duration") or 0.0), 3),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "size_bytes": int(format_payload.get("size") or path.stat().st_size),
        "r_frame_rate": str(stream.get("r_frame_rate") or ""),
        "avg_frame_rate": str(stream.get("avg_frame_rate") or ""),
        "nb_frames": int(stream.get("nb_frames") or 0),
    }
    if (
        metadata["duration_seconds"] <= 0
        or metadata["width"] <= 0
        or metadata["height"] <= 0
        or metadata["size_bytes"] <= 0
    ):
        raise RuntimeError(f"video_probe_invalid:{path}")
    return metadata


def _decode_video(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _extract_frame(path: Path, output_path: Path, *, seconds: float) -> None:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if seconds > 0:
        command.extend(("-ss", f"{seconds:.3f}"))
    command.extend(("-i", str(path), "-frames:v", "1", str(output_path)))
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)


def _normalized_rmse(left: Path, right: Path) -> float:
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:
        raise RuntimeError(f"image_compare_dependencies_missing:{exc}") from exc
    with Image.open(left) as left_image, Image.open(right) as right_image:
        left_rgb = np.asarray(left_image.convert("RGB"), dtype=np.float32)
        right_rgb = np.asarray(right_image.convert("RGB"), dtype=np.float32)
    if left_rgb.shape != right_rgb.shape:
        raise RuntimeError("image_compare_dimensions_mismatch")
    normalized = float(np.sqrt(np.mean(np.square(left_rgb - right_rgb))) / 255.0)
    return round(normalized, 6)


def _fps(value: object) -> float:
    try:
        return float(Fraction(str(value or "0")))
    except (ValueError, ZeroDivisionError):
        return 0.0


def build_xfade_filter(
    durations: list[float],
    transition_seconds: float,
    *,
    output_fps: float = 30.0,
) -> tuple[str, str, float, list[float]]:
    if len(durations) < 2:
        raise ValueError("at_least_two_segments_required")
    transition = float(transition_seconds)
    if transition <= 0 or output_fps <= 0 or any(duration <= transition for duration in durations):
        raise ValueError("invalid_transition_duration")
    fps_label = f"{float(output_fps):.6f}".rstrip("0").rstrip(".")
    filters = [
        f"[{index}:v]settb=AVTB,fps={fps_label},scale=1920:1080:force_original_aspect_ratio=decrease,"
        f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v{index}]"
        for index in range(len(durations))
    ]
    current_label = "v0"
    current_duration = float(durations[0])
    offsets: list[float] = []
    for index in range(1, len(durations)):
        offset = current_duration - transition
        offsets.append(round(offset, 3))
        output_label = f"x{index}"
        filters.append(
            f"[{current_label}][v{index}]xfade=transition=fade:duration={transition:.3f}:"
            f"offset={offset:.3f}[{output_label}]"
        )
        current_label = output_label
        current_duration += float(durations[index]) - transition
    return ";".join(filters), current_label, round(current_duration, 3), offsets


def route_coverage_from_receipt(receipt: dict[str, Any]) -> tuple[list[str], list[str]]:
    for check in list(receipt.get("checks") or []):
        if not isinstance(check, dict) or check.get("name") != "walkthrough_room_coverage_complete" or check.get("ok") is not True:
            continue
        coverage = dict(check.get("coverage") or {})
        expected = [str(value).strip() for value in list(coverage.get("rooms_expected") or []) if str(value).strip()]
        visited = [str(value).strip() for value in list(coverage.get("rooms_visited") or []) if str(value).strip()]
        if expected and expected == visited and str(coverage.get("status") or "").lower() == "pass":
            return expected, visited
    raise RuntimeError("walkthrough_route_coverage_not_proven")


def _strict_source_receipt_identity(
    *, property_slug: str, hosted_walkthrough_video_url: str
) -> dict[str, object]:
    identity: dict[str, object] = {
        "provider": "magicfit",
        "provider_key": "magicfit",
        "provider_backend_key": "magicfit",
        "render_status": "completed",
        "target_slug": str(property_slug or "").strip(),
        "hosted_walkthrough_video_url": str(
            hosted_walkthrough_video_url or ""
        ).strip(),
    }
    try:
        validate_magicfit_source_receipt(
            identity,
            slug=str(property_slug or "").strip(),
        )
    except ValueError as exc:
        raise RuntimeError(
            "magicfit_strict_source_receipt_handoff_required"
        ) from exc
    return identity


def _validate_segment_receipt(receipt_path: Path, segment_path: Path) -> dict[str, Any]:
    receipt = _load_json(receipt_path)
    if str(receipt.get("contract_name") or "") == "propertyquarry.walkthrough_delivery_variants.v1":
        if str(receipt.get("status") or "").lower() != "pass":
            raise RuntimeError(f"segment_delivery_receipt_failed:{receipt_path}")
        variants = [dict(row) for row in list(receipt.get("variants") or []) if isinstance(row, dict)]
        matches = [
            row
            for row in variants
            if Path(str(row.get("path") or "")).expanduser().resolve() == segment_path.resolve()
        ]
        if len(matches) != 1:
            raise RuntimeError(f"segment_delivery_variant_path_mismatch:{receipt_path}")
        variant = matches[0]
        if variant.get("full_decode_verified") is not True or variant.get("motion_interpolation_verified") is not True:
            raise RuntimeError(f"segment_delivery_variant_unverified:{receipt_path}")
        source_receipt_path = Path(str(receipt.get("source_receipt_path") or "")).expanduser().resolve()
        source_receipt = _load_json(source_receipt_path)
        return {
            "provider_key": "magicfit",
            "provider_backend_key": "magicfit",
            "render_status": "completed",
            "output_contract_ok": True,
            "output_file": str(segment_path),
            "page_url": str(receipt.get("source_provider_session_url") or source_receipt.get("page_url") or ""),
            "motion_interpolation_verified": True,
            "source_native_frame_rate": str(dict(receipt.get("source_metadata") or {}).get("avg_frame_rate") or ""),
            "continuity_repair_status": str(receipt.get("continuity_repair_status") or ""),
            "continuity_repair_method": str(receipt.get("continuity_repair_method") or ""),
            "continuity_repair_cut_seconds": list(receipt.get("continuity_repair_cut_seconds") or []),
            "continuity_repair_transition_offsets_seconds": list(
                receipt.get("continuity_repair_transition_offsets_seconds") or []
            ),
        }
    if str(receipt.get("provider_key") or receipt.get("provider") or "").lower() != "magicfit":
        raise RuntimeError(f"segment_provider_not_magicfit:{receipt_path}")
    if str(receipt.get("provider_backend_key") or "").lower() != "magicfit":
        raise RuntimeError(f"segment_backend_not_magicfit:{receipt_path}")
    if str(receipt.get("render_status") or "").lower() != "completed" or receipt.get("output_contract_ok") is not True:
        raise RuntimeError(f"segment_render_not_verified:{receipt_path}")
    declared = Path(str(receipt.get("output_file") or "")).expanduser().resolve()
    if declared != segment_path.resolve():
        raise RuntimeError(f"segment_receipt_path_mismatch:{receipt_path}")
    return receipt


def compose(
    *,
    segments: list[Path],
    segment_receipts: list[Path],
    coverage_receipt_path: Path | None,
    route_labels_override: list[str] | None,
    output_path: Path,
    state_path: Path,
    required_duration_seconds: float,
    transition_seconds: float,
    boundary_rmse_limit: float,
    encoder_preset: str,
    ffmpeg_timeout_seconds: float,
    output_fps: float,
    property_slug: str,
    property_title: str,
    hosted_walkthrough_video_url: str,
) -> dict[str, object]:
    source_identity = _strict_source_receipt_identity(
        property_slug=property_slug,
        hosted_walkthrough_video_url=hosted_walkthrough_video_url,
    )
    if len(segments) != len(segment_receipts) or len(segments) < 2:
        raise RuntimeError("segment_and_receipt_count_mismatch")
    metadata = [_probe_video(path) for path in segments]
    if any((row["width"], row["height"]) != (1920, 1080) for row in metadata):
        raise RuntimeError("segment_dimensions_not_1920x1080")
    source_frame_rates = [_fps(row.get("avg_frame_rate")) for row in metadata]
    if any(value <= 0.0 for value in source_frame_rates):
        raise RuntimeError("segment_frame_rate_invalid")
    if max(source_frame_rates) - min(source_frame_rates) > 0.01:
        raise RuntimeError("segment_frame_rate_mismatch")
    resolved_output_fps = float(output_fps) if output_fps > 0.0 else source_frame_rates[0]
    validated_receipts = [
        _validate_segment_receipt(receipt_path, segment_path)
        for segment_path, receipt_path in zip(segments, segment_receipts, strict=True)
    ]
    route_labels = [str(value).strip() for value in list(route_labels_override or []) if str(value).strip()]
    coverage_source = "visual_reviewed_segment_contact_sheets" if route_labels else "walkthrough_quality_receipt"
    if route_labels:
        covered_route_labels = list(route_labels)
    elif coverage_receipt_path is not None:
        coverage_receipt = _load_json(coverage_receipt_path)
        route_labels, covered_route_labels = route_coverage_from_receipt(coverage_receipt)
    else:
        raise RuntimeError("walkthrough_route_coverage_missing")

    boundary_checks: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="propertyquarry-magicfit-boundaries-") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        for index in range(len(segments) - 1):
            left = temp_dir / f"boundary-{index + 1:02d}-last.png"
            right = temp_dir / f"boundary-{index + 2:02d}-first.png"
            _extract_frame(segments[index], left, seconds=max(float(metadata[index]["duration_seconds"]) - 0.5, 0.0))
            _extract_frame(segments[index + 1], right, seconds=0.0)
            rmse = _normalized_rmse(left, right)
            boundary_checks.append(
                {
                    "from_segment": index + 1,
                    "to_segment": index + 2,
                    "normalized_rmse": rmse,
                    "limit": float(boundary_rmse_limit),
                    "status": "pass" if rmse <= boundary_rmse_limit else "fail",
                }
            )
    failed_boundaries = [row for row in boundary_checks if row["status"] != "pass"]
    if failed_boundaries:
        raise RuntimeError(f"magicfit_boundary_verification_failed:{failed_boundaries}")

    durations = [float(row["duration_seconds"]) for row in metadata]
    filter_graph, final_label, expected_duration, offsets = build_xfade_filter(
        durations,
        transition_seconds,
        output_fps=resolved_output_fps,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for segment in segments:
        command.extend(("-i", str(segment)))
    command.extend(
        (
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-filter_complex",
            filter_graph,
            "-map",
            f"[{final_label}]",
            "-map",
            f"{len(segments)}:a:0",
            "-c:v",
            "libx264",
            "-preset",
            encoder_preset,
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-t",
            f"{expected_duration:.3f}",
            "-movflags",
            "+faststart",
            str(output_path),
        )
    )
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=max(60.0, float(ffmpeg_timeout_seconds)),
    )
    output_metadata = _probe_video(output_path)
    _decode_video(output_path)
    if abs(_fps(output_metadata.get("avg_frame_rate")) - resolved_output_fps) > 0.01:
        raise RuntimeError("magicfit_composite_frame_rate_mismatch")
    if float(output_metadata["duration_seconds"]) + 0.25 < float(required_duration_seconds):
        raise RuntimeError("magicfit_composite_below_required_duration")

    segment_output_starts: list[float] = []
    current_start = 0.0
    for index, duration in enumerate(durations):
        segment_output_starts.append(round(current_start, 3))
        current_start += duration - (float(transition_seconds) if index < len(durations) - 1 else 0.0)
    continuity_repairs: list[dict[str, object]] = []
    repair_transition_offsets: list[float] = []
    for index, receipt in enumerate(validated_receipts):
        if str(receipt.get("continuity_repair_status") or "").lower() != "pass":
            continue
        local_offsets = [
            float(value)
            for value in list(receipt.get("continuity_repair_transition_offsets_seconds") or [])
        ]
        global_offsets = [round(segment_output_starts[index] + value, 3) for value in local_offsets]
        repair_transition_offsets.extend(global_offsets)
        continuity_repairs.append(
            {
                "segment_index": index + 1,
                "method": str(receipt.get("continuity_repair_method") or ""),
                "cut_seconds": list(receipt.get("continuity_repair_cut_seconds") or []),
                "local_transition_offsets_seconds": local_offsets,
                "global_transition_offsets_seconds": global_offsets,
                "status": "pass",
            }
        )
    all_transition_offsets = sorted({*offsets, *repair_transition_offsets})
    segment_motion_interpolation_verified = all(
        receipt.get("motion_interpolation_verified") is True for receipt in validated_receipts
    )

    payload: dict[str, object] = {
        **source_identity,
        "contract_name": SOURCE_RECEIPT_HANDOFF_CONTRACT,
        "status": "source_receipt_ready_for_pending_import",
        "acceptance_status": "pending",
        "launch_eligible": False,
        "operator_handoff_required": True,
        "operator_handoff": {
            "next_command": "import_magicfit_walkthrough.py",
            "command_argv": [
                "python",
                "scripts/import_magicfit_walkthrough.py",
                "--slug",
                property_slug,
                "--video-path",
                str(output_path),
                "--source-receipt",
                str(state_path),
            ],
            "publishes_public_media": False,
            "resulting_status": "staged_pending_delivery_acceptance",
        },
        "property_slug": property_slug,
        "property_title": property_title,
        "composition": "boundary_verified_frame_continuation",
        "segment_count": len(segments),
        "segments": [
            {
                "index": index + 1,
                "path": str(path),
                "sha256": _sha256(path),
                "metadata": segment_metadata,
                "receipt_path": str(receipt_path),
                "receipt_sha256": _sha256(receipt_path),
            }
            for index, (path, receipt_path, segment_metadata, receipt) in enumerate(
                zip(segments, segment_receipts, metadata, validated_receipts, strict=True)
            )
        ],
        "boundary_checks": boundary_checks,
        "transition_seconds": float(transition_seconds),
        "transition_offsets_seconds": all_transition_offsets,
        "composition_transition_offsets_seconds": offsets,
        "continuity_repairs": continuity_repairs,
        "continuity_repair_status": "pass" if continuity_repairs else "not_required",
        "motion_interpolation_status": "pass" if segment_motion_interpolation_verified else "not_required",
        "motion_interpolation_method": (
            "native_segment_bidirectional_motion_compensation_before_60fps_composition"
            if segment_motion_interpolation_verified
            else ""
        ),
        "native_source_frame_rates": [
            str(receipt.get("source_native_frame_rate") or "") for receipt in validated_receipts
        ],
        "output_frame_rate": f"{resolved_output_fps:g}",
        "source_frame_rates": [str(row.get("avg_frame_rate") or "") for row in metadata],
        "encoder_preset": encoder_preset,
        "encoder_crf": 18,
        "ffmpeg_timeout_seconds": float(ffmpeg_timeout_seconds),
        "route_labels": route_labels,
        "covered_route_labels": covered_route_labels,
        "route_coverage_source": coverage_source,
        "coverage_receipt_path": str(coverage_receipt_path) if coverage_receipt_path is not None else "",
        "coverage_receipt_sha256": _sha256(coverage_receipt_path) if coverage_receipt_path is not None else "",
        "output_file": str(output_path),
        "video_output_path": str(output_path),
        "video_sha256": _sha256(output_path),
        "output_metadata": output_metadata,
        "duration_seconds": float(output_metadata["duration_seconds"]),
        "required_duration_seconds": float(required_duration_seconds),
        "full_decode_verified": True,
        "generated_at": _utc_now(),
    }
    try:
        validate_magicfit_source_receipt(payload, slug=property_slug)
    except ValueError as exc:
        raise RuntimeError("magicfit_source_receipt_handoff_invalid") from exc
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Compose and verify a multi-segment MagicFit property walkthrough.")
    parser.add_argument("--segment", action="append", required=True)
    parser.add_argument("--segment-receipt", action="append", required=True)
    parser.add_argument("--coverage-receipt", default="")
    parser.add_argument("--route-label", action="append", default=[])
    parser.add_argument("--out", required=True)
    parser.add_argument("--state-json", required=True)
    parser.add_argument("--required-duration", type=float, default=65.0)
    parser.add_argument("--transition-seconds", type=float, default=1.0)
    parser.add_argument("--boundary-rmse-limit", type=float, default=0.2)
    parser.add_argument("--encoder-preset", choices=("medium", "fast", "faster", "veryfast"), default="fast")
    parser.add_argument("--ffmpeg-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--output-fps", type=float, default=0.0, help="0 preserves the common native segment cadence")
    parser.add_argument("--property-slug", required=True)
    parser.add_argument("--property-title", default="")
    parser.add_argument(
        "--hosted-walkthrough-video-url",
        required=True,
        help=(
            "Approved MagicFit CDN URL for these exact composed bytes; this "
            "does not publish or accept the walkthrough."
        ),
    )
    args = parser.parse_args()
    payload = compose(
        segments=[Path(value).expanduser().resolve() for value in args.segment],
        segment_receipts=[Path(value).expanduser().resolve() for value in args.segment_receipt],
        coverage_receipt_path=Path(args.coverage_receipt).expanduser().resolve() if args.coverage_receipt else None,
        route_labels_override=list(args.route_label or []),
        output_path=Path(args.out).expanduser().resolve(),
        state_path=Path(args.state_json).expanduser().resolve(),
        required_duration_seconds=max(1.0, float(args.required_duration)),
        transition_seconds=max(0.01, float(args.transition_seconds)),
        boundary_rmse_limit=max(0.0, float(args.boundary_rmse_limit)),
        encoder_preset=str(args.encoder_preset),
        ffmpeg_timeout_seconds=max(60.0, float(args.ffmpeg_timeout_seconds)),
        output_fps=max(0.0, float(args.output_fps)),
        property_slug=str(args.property_slug or "").strip(),
        property_title=str(args.property_title or "").strip(),
        hosted_walkthrough_video_url=str(
            args.hosted_walkthrough_video_url or ""
        ).strip(),
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
