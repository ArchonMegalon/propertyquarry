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

try:
    from propertyquarry_walkthrough_quality_gate import _frame_delta_stats
except ModuleNotFoundError:
    from scripts.propertyquarry_walkthrough_quality_gate import _frame_delta_stats


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


def _probe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,nb_frames:format=duration,size",
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
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "avg_frame_rate": str(stream.get("avg_frame_rate") or ""),
        "nb_frames": int(stream.get("nb_frames") or 0),
        "duration_seconds": round(float(format_payload.get("duration") or 0.0), 3),
        "size_bytes": int(format_payload.get("size") or path.stat().st_size),
    }


def _fps(value: object) -> float:
    try:
        return float(Fraction(str(value or "0")))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _decode(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _check(name: str, ok: bool, **details: object) -> dict[str, object]:
    return {"name": name, "ok": bool(ok), **details}


def build_native_continuity_receipt(
    *,
    render_receipt: dict[str, Any],
    render_receipt_path: Path,
    output_path: Path,
    output_metadata: dict[str, object],
    delta_stats: dict[str, object],
    max_jump_delta: float,
    full_decode_verified: bool,
) -> dict[str, object]:
    source_proof = dict(render_receipt.get("native_extend_source_proof") or {})
    prefix_proof = dict(render_receipt.get("native_extend_prefix_proof") or {})
    source_metadata = dict(source_proof.get("source_metadata") or {})
    source_duration = float(source_metadata.get("duration_seconds") or 0.0)
    extension_seconds = float(render_receipt.get("duration_seconds_magicfit") or 0.0)
    output_duration = float(output_metadata.get("duration_seconds") or 0.0)
    transition_rows = [
        dict(row)
        for row in list(delta_stats.get("transition_boundary_samples") or [])
        if isinstance(row, dict)
    ]
    max_transition_delta = max(
        (float(row.get("delta") or 0.0) for row in transition_rows),
        default=0.0,
    )
    max_delta = float(delta_stats.get("max_delta") or 0.0)
    checks = [
        _check("provider_is_magicfit", str(render_receipt.get("provider_key") or "").lower() == "magicfit"),
        _check("provider_backend_is_magicfit", str(render_receipt.get("provider_backend_key") or "").lower() == "magicfit"),
        _check("render_completed", render_receipt.get("render_status") == "completed"),
        _check("provider_operation_is_native_extend", render_receipt.get("provider_operation") == "native_video_extend"),
        _check(
            "composition_is_provider_native_cumulative_extension",
            render_receipt.get("composition") == "provider_native_cumulative_extension",
        ),
        _check("postproduction_edits_absent", int(render_receipt.get("postproduction_edit_count") or 0) == 0),
        _check("render_output_contract_pass", render_receipt.get("output_contract_ok") is True),
        _check("source_proof_pass", source_proof.get("status") == "pass"),
        _check("prefix_proof_pass", prefix_proof.get("status") == "pass"),
        _check(
            "prefix_perceptual_similarity",
            float(prefix_proof.get("maximum_perceptual_normalized_rmse") or 1.0)
            <= float(prefix_proof.get("perceptual_rmse_limit") or 0.0),
            maximum_perceptual_normalized_rmse=float(
                prefix_proof.get("maximum_perceptual_normalized_rmse") or 1.0
            ),
        ),
        _check(
            "cumulative_duration_growth",
            source_duration > 0.0
            and extension_seconds > 0.0
            and output_duration + 0.75 >= source_duration + extension_seconds,
            source_duration_seconds=source_duration,
            extension_seconds=extension_seconds,
            output_duration_seconds=output_duration,
        ),
        _check("full_decode_verified", full_decode_verified),
        _check("all_native_frames_sampled", bool(delta_stats.get("ok"))),
        _check(
            "global_frame_jump_limit",
            bool(delta_stats.get("ok")) and max_delta <= max_jump_delta,
            maximum_delta=max_delta,
            maximum=max_jump_delta,
        ),
        _check("native_join_sample_present", bool(transition_rows)),
        _check(
            "native_join_frame_jump_limit",
            bool(transition_rows) and max_transition_delta <= max_jump_delta,
            maximum_transition_delta=max_transition_delta,
            maximum=max_jump_delta,
        ),
    ]
    failures = [str(row["name"]) for row in checks if not row.get("ok")]
    return {
        "contract_name": "propertyquarry.magicfit_native_continuity_gate.v1",
        "generated_at": _utc_now(),
        "status": "pass" if not failures else "fail",
        "provider": "magicfit",
        "render_receipt_path": str(render_receipt_path),
        "render_receipt_sha256": _sha256(render_receipt_path),
        "output_path": str(output_path),
        "output_sha256": _sha256(output_path),
        "output_metadata": output_metadata,
        "max_jump_delta": max_jump_delta,
        "frame_delta_stats": delta_stats,
        "check_count": len(checks),
        "failed_count": len(failures),
        "failures": failures,
        "checks": checks,
        "truth_boundary": (
            "This gate proves provider-native cumulative continuity and native-cadence frame smoothness. "
            "It does not prove room identity or complete route coverage without separate visual evidence."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a native cumulative MagicFit extension.")
    parser.add_argument("--render-receipt", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-jump-delta", type=float, default=18.0)
    parser.add_argument("--write", required=True)
    args = parser.parse_args()

    render_receipt_path = Path(args.render_receipt).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    if not render_receipt_path.is_file() or not output_path.is_file():
        raise RuntimeError("magicfit_native_continuity_artifact_missing")
    render_receipt = _load_json(render_receipt_path)
    declared_output = Path(str(render_receipt.get("output_file") or "")).expanduser().resolve()
    if declared_output != output_path:
        raise RuntimeError("magicfit_native_continuity_output_mismatch")
    output_metadata = _probe(output_path)
    fps = _fps(output_metadata.get("avg_frame_rate"))
    source_duration = float(
        dict(render_receipt.get("native_extend_source_proof") or {})
        .get("source_metadata", {})
        .get("duration_seconds", 0.0)
    )
    delta_stats = _frame_delta_stats(
        output_path,
        fps=fps,
        max_sampled_frames=max(900, int(output_metadata.get("nb_frames") or 0) + 5),
        transition_offsets_seconds=[source_duration],
        transition_seconds=0.0,
        timeout_seconds=300,
    )
    full_decode_verified = True
    try:
        _decode(output_path)
    except (OSError, subprocess.SubprocessError):
        full_decode_verified = False
    receipt = build_native_continuity_receipt(
        render_receipt=render_receipt,
        render_receipt_path=render_receipt_path,
        output_path=output_path,
        output_metadata=output_metadata,
        delta_stats=delta_stats,
        max_jump_delta=max(1.0, float(args.max_jump_delta)),
        full_decode_verified=full_decode_verified,
    )
    write_path = Path(args.write).expanduser().resolve()
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
