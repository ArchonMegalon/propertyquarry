from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path


FFMPEG_BIN = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


def ffmpeg_bin() -> str:
    candidate = Path(FFMPEG_BIN)
    if candidate.exists():
        return str(candidate)
    raise RuntimeError("ffmpeg_unavailable:ffmpeg executable not found")


def run_ffmpeg(args: list[str]) -> None:
    completed = subprocess.run(
        [ffmpeg_bin(), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "").strip()
    raise RuntimeError(f"ffmpeg_failed:{detail[:500]}")


def srt_timestamp(seconds: float) -> str:
    millis = max(0, int(round(float(seconds) * 1000.0)))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def write_srt(entries: list[dict[str, object]], target_path: Path) -> Path:
    lines: list[str] = []
    for index, entry in enumerate(entries, start=1):
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        start_seconds = float(entry.get("start_seconds") or 0.0)
        end_seconds = float(entry.get("end_seconds") or start_seconds + 2.0)
        lines.extend(
            [
                str(index),
                f"{srt_timestamp(start_seconds)} --> {srt_timestamp(end_seconds)}",
                text,
                "",
            ]
        )
    target_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return target_path


def compose_slideshow_video(
    image_paths: list[Path],
    output_path: Path,
    *,
    subtitle_lines: list[str] | None = None,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
    scene_seconds: float = 2.8,
    transition_seconds: float = 0.35,
    subtitle_srt_path: Path | None = None,
) -> dict[str, object]:
    normalized_images = [Path(path).expanduser() for path in image_paths if Path(path).expanduser().exists()]
    if not normalized_images:
        raise RuntimeError("slideshow_images_missing")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    transition_seconds = max(0.0, float(transition_seconds))
    scene_seconds = max(1.2, float(scene_seconds))
    fps = max(12, int(fps))
    width = max(640, int(width))
    height = max(360, int(height))
    total_duration = scene_seconds * len(normalized_images)
    subtitle_entries: list[dict[str, object]] = []
    if subtitle_lines:
        current = 0.0
        for raw in subtitle_lines[: len(normalized_images)]:
            text = str(raw or "").strip()
            subtitle_entries.append(
                {
                    "text": text,
                    "start_seconds": current,
                    "end_seconds": current + scene_seconds - 0.05,
                }
            )
            current += scene_seconds
    if subtitle_entries and subtitle_srt_path is not None:
        write_srt(subtitle_entries, subtitle_srt_path)

    input_args: list[str] = []
    filter_parts: list[str] = []
    clip_label = ""
    per_clip_duration = scene_seconds + transition_seconds
    zoom_frames = max(1, int(math.ceil(per_clip_duration * fps)))
    for index, image_path in enumerate(normalized_images):
        input_args.extend(["-loop", "1", "-t", f"{per_clip_duration:.3f}", "-i", str(image_path)])
        filter_parts.append(
            (
                f"[{index}:v]"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"format=yuv420p,setsar=1,"
                f"zoompan=z='min(zoom+0.00045,1.08)':d={zoom_frames}:s={width}x{height}:fps={fps}"
                f"[v{index}]"
            )
        )
    if len(normalized_images) == 1:
        clip_label = "[v0]"
    else:
        clip_label = "[v0]"
        for index in range(1, len(normalized_images)):
            output_label = f"[x{index}]"
            offset_seconds = scene_seconds * index
            filter_parts.append(
                f"{clip_label}[v{index}]xfade=transition=fade:duration={transition_seconds:.3f}:offset={offset_seconds:.3f}{output_label}"
            )
            clip_label = output_label
    if subtitle_entries and subtitle_srt_path is not None and subtitle_srt_path.exists():
        escaped_subtitle_path = str(subtitle_srt_path).replace("\\", "\\\\").replace(":", "\\:")
        filter_parts.append(f"{clip_label}subtitles='{escaped_subtitle_path}'[outv]")
        clip_label = "[outv]"
    run_ffmpeg(
        [
            "-y",
            *input_args,
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            clip_label,
            "-t",
            f"{total_duration:.3f}",
            "-r",
            str(fps),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            "-c:v",
            "libx264",
            str(output_path),
        ]
    )
    return {
        "path": str(output_path),
        "duration_seconds": total_duration,
        "scene_count": len(normalized_images),
        "subtitle_count": len([entry for entry in subtitle_entries if str(entry.get("text") or "").strip()]),
    }


def transcode_video(input_path: Path, output_path: Path, *, fps: int = 30) -> Path:
    normalized_input = Path(input_path).expanduser()
    if not normalized_input.exists():
        raise RuntimeError(f"video_input_missing:{normalized_input}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(normalized_input),
            "-r",
            str(max(12, int(fps))),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            "-c:v",
            "libx264",
            str(output_path),
        ]
    )
    return output_path


def transcode_video_webm(input_path: Path, output_path: Path, *, fps: int = 30, crf: int = 32) -> Path:
    normalized_input = Path(input_path).expanduser()
    if not normalized_input.exists():
        raise RuntimeError(f"video_input_missing:{normalized_input}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(normalized_input),
            "-r",
            str(max(12, int(fps))),
            "-an",
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "0",
            "-crf",
            str(max(18, int(crf))),
            str(output_path),
        ]
    )
    return output_path
