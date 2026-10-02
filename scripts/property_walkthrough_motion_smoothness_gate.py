#!/usr/bin/env python3
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames:format=duration,size",
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
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "avg_frame_rate": str(stream.get("avg_frame_rate") or ""),
        "r_frame_rate": str(stream.get("r_frame_rate") or ""),
        "nb_frames": int(stream.get("nb_frames") or 0),
        "duration_seconds": round(float(format_payload.get("duration") or 0.0), 3),
        "size_bytes": int(format_payload.get("size") or path.stat().st_size),
    }


def _fps(value: object) -> float:
    try:
        return float(Fraction(str(value or "0")))
    except (ValueError, ZeroDivisionError):
        return 0.0


def parse_window(value: str) -> tuple[float, float]:
    parts = [part.strip() for part in str(value or "").split(":")]
    if len(parts) != 2:
        raise ValueError("window_must_be_start_duration")
    start, duration = (float(part) for part in parts)
    if start < 0.0 or duration <= 0.0:
        raise ValueError("window_values_invalid")
    return round(start, 3), round(duration, 3)


def _decode_window(path: Path, *, start: float, duration: float, fps: float) -> np.ndarray:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(path),
            "-t",
            f"{duration:.3f}",
            "-vf",
            f"fps={fps:.4f},scale=160:90:flags=fast_bilinear,format=gray",
            "-an",
            "-sn",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    frame_size = 160 * 90
    raw = bytes(completed.stdout or b"")
    frame_count = len(raw) // frame_size
    if frame_count < 3:
        raise RuntimeError("motion_window_frames_missing")
    return np.frombuffer(raw[: frame_count * frame_size], dtype=np.uint8).reshape(frame_count, 90, 160)


def motion_metrics(frames: np.ndarray) -> dict[str, object]:
    deltas: list[float] = []
    motion_vectors: list[np.ndarray] = []
    for before, after in zip(frames[:-1], frames[1:], strict=True):
        deltas.append(float(cv2.absdiff(before, after).mean()))
        flow = cv2.calcOpticalFlowFarneback(before, after, None, 0.5, 3, 21, 3, 5, 1.2, 0)
        motion_vectors.append(np.median(flow.reshape(-1, 2), axis=0))
    vectors = np.asarray(motion_vectors)
    magnitudes = np.linalg.norm(vectors, axis=1)
    jerk = np.linalg.norm(np.diff(vectors, axis=0), axis=1)
    return {
        "frame_count": int(len(frames)),
        "adjacent_pair_count": len(deltas),
        "duplicate_ratio": round(sum(value < 0.1 for value in deltas) / max(len(deltas), 1), 4),
        "mean_frame_delta": round(float(np.mean(deltas)), 3),
        "p95_frame_delta": round(float(np.percentile(deltas, 95)), 3),
        "mean_motion_step": round(float(np.mean(magnitudes)), 3),
        "p95_motion_jerk": round(float(np.percentile(jerk, 95)), 3),
    }


def evaluate_window(
    source: dict[str, object],
    output: dict[str, object],
    *,
    max_jerk_ratio: float,
    max_frame_delta_ratio: float,
) -> dict[str, object]:
    def _ratio(output_key: str, source_key: str | None = None) -> float:
        denominator = float(source.get(source_key or output_key) or 0.0)
        return round(float(output.get(output_key) or 0.0) / denominator, 4) if denominator > 0.0 else 0.0

    jerk_ratio = _ratio("p95_motion_jerk")
    frame_delta_ratio = _ratio("p95_frame_delta")
    motion_step_ratio = _ratio("mean_motion_step")
    duplicate_increase = round(float(output.get("duplicate_ratio") or 0.0) - float(source.get("duplicate_ratio") or 0.0), 4)
    checks = {
        "p95_motion_jerk_reduced": jerk_ratio <= max_jerk_ratio,
        "p95_frame_delta_reduced": frame_delta_ratio <= max_frame_delta_ratio,
        "per_frame_motion_step_near_half": 0.25 <= motion_step_ratio <= 0.75,
        "duplicate_ratio_not_increased": duplicate_increase <= 0.02,
        "output_has_more_frames": int(output.get("frame_count") or 0) > int(source.get("frame_count") or 0),
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "p95_motion_jerk_ratio": jerk_ratio,
        "p95_frame_delta_ratio": frame_delta_ratio,
        "mean_motion_step_ratio": motion_step_ratio,
        "duplicate_ratio_increase": duplicate_increase,
    }


def build_receipt(
    *,
    source_path: Path,
    output_path: Path,
    windows: list[tuple[float, float]],
    max_jerk_ratio: float,
    max_frame_delta_ratio: float,
) -> dict[str, object]:
    if not source_path.is_file() or not output_path.is_file():
        raise RuntimeError("motion_smoothness_artifact_missing")
    source_metadata = _probe(source_path)
    output_metadata = _probe(output_path)
    source_fps = _fps(source_metadata["avg_frame_rate"])
    output_fps = _fps(output_metadata["avg_frame_rate"])
    window_rows: list[dict[str, object]] = []
    for start, duration in windows:
        if start + duration > min(
            float(source_metadata["duration_seconds"]),
            float(output_metadata["duration_seconds"]),
        ) + 0.01:
            raise RuntimeError("motion_smoothness_window_out_of_range")
        source_metrics = motion_metrics(
            _decode_window(source_path, start=start, duration=duration, fps=source_fps)
        )
        output_metrics = motion_metrics(
            _decode_window(output_path, start=start, duration=duration, fps=output_fps)
        )
        evaluation = evaluate_window(
            source_metrics,
            output_metrics,
            max_jerk_ratio=max_jerk_ratio,
            max_frame_delta_ratio=max_frame_delta_ratio,
        )
        window_rows.append(
            {
                "start_seconds": start,
                "duration_seconds": duration,
                "source": source_metrics,
                "output": output_metrics,
                **evaluation,
            }
        )
    checks = {
        "source_uses_supported_native_cadence": any(abs(source_fps - value) <= 0.01 for value in (24.0, 25.0, 30.0)),
        "output_is_true_60fps": abs(output_fps - 60.0) <= 0.01,
        "duration_preserved": abs(
            float(output_metadata["duration_seconds"]) - float(source_metadata["duration_seconds"])
        ) <= 0.25,
        "all_stress_windows_pass": bool(window_rows) and all(row["status"] == "pass" for row in window_rows),
    }
    return {
        "contract_name": "propertyquarry.walkthrough_motion_smoothness_gate.v1",
        "status": "pass" if all(checks.values()) else "fail",
        "generated_at": _utc_now(),
        "checks": checks,
        "thresholds": {
            "max_p95_motion_jerk_ratio": max_jerk_ratio,
            "max_p95_frame_delta_ratio": max_frame_delta_ratio,
            "max_duplicate_ratio_increase": 0.02,
            "mean_motion_step_ratio_range": [0.25, 0.75],
        },
        "source_path": str(source_path),
        "source_sha256": _sha256(source_path),
        "source_metadata": source_metadata,
        "output_path": str(output_path),
        "output_sha256": _sha256(output_path),
        "output_metadata": output_metadata,
        "windows": window_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure PropertyQuarry 60 fps walkthrough motion smoothness.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--window", action="append", required=True, help="start:duration")
    parser.add_argument("--max-jerk-ratio", type=float, default=0.65)
    parser.add_argument("--max-frame-delta-ratio", type=float, default=0.75)
    parser.add_argument("--write", required=True)
    args = parser.parse_args()
    receipt = build_receipt(
        source_path=Path(args.source).expanduser().resolve(),
        output_path=Path(args.output).expanduser().resolve(),
        windows=[parse_window(value) for value in args.window],
        max_jerk_ratio=max(0.01, float(args.max_jerk_ratio)),
        max_frame_delta_ratio=max(0.01, float(args.max_frame_delta_ratio)),
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    out_path = Path(args.write).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
