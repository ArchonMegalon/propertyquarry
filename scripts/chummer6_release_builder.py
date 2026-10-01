#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import urllib.parse


DEFAULT_MANIFEST_CANDIDATES: tuple[Path, ...] = (
    Path("/docker/chummercomplete/chummer-hub-registry/.codex-studio/published/RELEASE_CHANNEL.generated.json"),
    Path("/docker/chummercomplete/chummer-hub-registry/.codex-studio/published/releases.json"),
    Path("/docker/chummercomplete/chummer.run-services/legacy/tooling/docker/Docker/Downloads/releases.json"),
    Path("/docker/chummer5a/Docker/Downloads/releases.json"),
)
DEFAULT_OUTPUT_PATH = Path("/docker/fleet/state/chummer6/chummer6_release_matrix.json")
DEFAULT_BASE_URL = "https://chummer.run"


def default_manifest_path() -> Path:
    configured = str(os.environ.get("CHUMMER6_RELEASE_MANIFEST_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    for candidate in DEFAULT_MANIFEST_CANDIDATES:
        if candidate.exists():
            return candidate
    return DEFAULT_MANIFEST_CANDIDATES[0]


def _read_json(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"release manifest must be a JSON object: {path}")
    return loaded


def _infer_platform(raw_platform: str, url: str) -> str:
    lowered = f"{raw_platform} {url}".lower()
    if "windows" in lowered or "-win-" in lowered:
        return "windows"
    if "macos" in lowered or "osx" in lowered:
        return "macos"
    if "linux" in lowered:
        return "linux"
    return "unknown"


def _infer_arch(raw_platform: str, url: str) -> str:
    lowered = f"{raw_platform} {url}".lower()
    if "arm64" in lowered or "apple silicon" in lowered:
        return "arm64"
    if "x64" in lowered or "amd64" in lowered or "intel" in lowered:
        return "x64"
    return "unknown"


def _infer_head(raw_platform: str, url: str) -> str:
    lowered = f"{raw_platform} {url}".lower()
    if "avalonia" in lowered:
        return "avalonia"
    if "blazor" in lowered:
        return "blazor"
    return "desktop"


def _infer_kind(url: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return {
        ".exe": "installer",
        ".msi": "installer",
        ".dmg": "dmg",
        ".pkg": "pkg",
        ".zip": "archive",
    }.get(suffix, "artifact")


def _normalized_artifact(item: dict[str, object], *, base_url: str) -> dict[str, object]:
    raw_platform = str(item.get("platformLabel") or item.get("platform") or "").strip()
    raw_url = str(item.get("url") or item.get("downloadUrl") or "").strip()
    raw_platform_id = str(item.get("platform") or "").strip().lower()
    raw_arch = str(item.get("arch") or "").strip().lower()
    raw_head = str(item.get("head") or "").strip().lower()
    raw_kind = str(item.get("kind") or "").strip().lower()
    return {
        "id": str(item.get("id") or item.get("artifactId") or "").strip(),
        "platform": raw_platform_id if raw_platform_id in {"windows", "macos", "linux", "unknown"} else _infer_platform(raw_platform, raw_url),
        "arch": raw_arch if raw_arch in {"x64", "arm64", "unknown"} else _infer_arch(raw_platform, raw_url),
        "head": raw_head if raw_head in {"avalonia", "blazor", "desktop"} else _infer_head(raw_platform, raw_url),
        "kind": raw_kind if raw_kind in {"installer", "dmg", "pkg", "portable", "archive", "artifact"} else _infer_kind(raw_url),
        "platform_label": raw_platform or "Preview build",
        "url": urllib.parse.urljoin(base_url, raw_url),
        "filename": str(item.get("fileName") or "").strip() or Path(urllib.parse.urlparse(raw_url).path).name,
        "sha256": str(item.get("sha256") or "").strip(),
        "sizeBytes": int(item.get("sizeBytes") or 0),
    }


def build_release_matrix(*, manifest_path: Path, base_url: str) -> dict[str, object]:
    payload = _read_json(manifest_path)
    downloads = payload.get("artifacts")
    if not isinstance(downloads, list):
        downloads = payload.get("downloads")
    if not isinstance(downloads, list):
        raise ValueError(f"release manifest is missing artifacts[] or downloads[]: {manifest_path}")
    artifacts = [
        _normalized_artifact(dict(item), base_url=base_url)
        for item in downloads
        if isinstance(item, dict)
    ]
    order = {"windows": 0, "macos": 1, "linux": 2, "unknown": 9}
    artifacts.sort(key=lambda row: (order.get(str(row.get("platform") or ""), 9), str(row.get("arch") or ""), str(row.get("head") or ""), str(row.get("kind") or ""), str(row.get("platform_label") or "")))
    preferred_kind_order = ("installer", "dmg", "pkg", "portable", "archive", "artifact")
    present_kinds = [str(item.get("kind") or "").strip() for item in artifacts if str(item.get("kind") or "").strip()]
    archive_only = bool(artifacts) and all(kind == "archive" for kind in present_kinds)
    primary_kind = next((kind for kind in preferred_kind_order if kind in present_kinds), "artifact")
    primary_consumer_ready = primary_kind in {"installer", "dmg", "pkg", "portable"}
    return {
        "version": str(payload.get("version") or "unknown").strip(),
        "channel": str(payload.get("channel") or payload.get("channelId") or "unknown").strip(),
        "publishedAt": str(payload.get("publishedAt") or "unknown").strip(),
        "source_manifest": str(manifest_path),
        "base_url": base_url,
        "archiveOnly": archive_only,
        "primaryArtifactKind": primary_kind,
        "primaryArtifactConsumerReady": primary_consumer_ready,
        "frontDoorDownloadPosture": "advanced_manual_preview_only" if archive_only else "preview_artifacts_available",
        "frontDoorPrimaryCtaEligible": primary_consumer_ready,
        "artifacts": artifacts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize the current Chummer6 desktop downloads manifest into a guide-facing release matrix.")
    parser.add_argument("--manifest", default=str(default_manifest_path()))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()

    manifest_path = Path(args.manifest).expanduser()
    output_path = Path(args.output).expanduser()
    matrix = build_release_matrix(manifest_path=manifest_path, base_url=str(args.base_url))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(matrix, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output_path), "artifacts": len(matrix.get("artifacts") or [])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
