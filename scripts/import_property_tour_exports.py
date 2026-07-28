#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        bounded_lane_lock,
        require_bounded_file,
        run_bounded_subprocess,
        tour_manifest_max_bytes,
    )
except ModuleNotFoundError:
    from scripts.property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        bounded_lane_lock,
        require_bounded_file,
        run_bounded_subprocess,
        tour_manifest_max_bytes,
    )


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_BY_PROVIDER = {
    "3dvista": "import_3dvista_export.py",
    "pano2vr": "import_pano2vr_export.py",
    "krpano": "import_krpano_walkable_scene.py",
    "magicfit": "import_magicfit_walkthrough.py",
}
PANORAMA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}


def _safe_provider(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("_", "").replace("-", "")
    if normalized in {"3dvista", "threevista", "threedvista", "3d-vista".replace("-", "")}:
        return "3dvista"
    if normalized in {"pano2vr", "pano2v"}:
        return "pano2vr"
    if normalized == "krpano":
        return "krpano"
    if normalized == "magicfit":
        return "magicfit"
    return ""


def _safe_slug(value: object) -> str:
    slug = str(value or "").strip()
    if not slug or "/" in slug or "\\" in slug or ".." in slug:
        return ""
    return slug


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        require_bounded_file(
            path,
            reason_prefix="import_manifest",
            maximum_bytes=tour_manifest_max_bytes(),
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"import_manifest_invalid:{type(exc).__name__}") from exc
    rows = payload.get("imports") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise SystemExit("import_manifest_missing_imports")
    maximum_rows = bounded_env_int(
        "PROPERTYQUARRY_TOUR_IMPORT_MAX_ROWS",
        default=64,
        minimum=1,
        maximum=256,
    )
    if len(rows) > maximum_rows:
        raise SystemExit("import_manifest_row_limit")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _first_existing_file(*values: object) -> Path | None:
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if candidate.is_file():
            return candidate
    return None


def _discover_panorama(asset_dir: Path) -> Path | None:
    for name in ("panorama.jpg", "panorama.jpeg", "panorama.png", "panorama.webp", "equirect.jpg", "equirect.jpeg", "equirect.png", "equirect.webp"):
        candidate = asset_dir / name
        if candidate.is_file():
            return candidate
    matches = [path for path in sorted(asset_dir.iterdir()) if path.is_file() and path.suffix.lower() in PANORAMA_EXTENSIONS and "panorama" in path.stem.lower()]
    return matches[0] if matches else None


def _discover_cube_faces(asset_dir: Path) -> list[Path]:
    faces: list[Path] = []
    for index in range(1, 7):
        face = next((asset_dir / f"cube-face-{index}{suffix}" for suffix in sorted(PANORAMA_EXTENSIONS) if (asset_dir / f"cube-face-{index}{suffix}").is_file()), None)
        if face is None:
            return []
        faces.append(face)
    return faces


def _discover_video(asset_dir: Path) -> Path | None:
    preferred = [
        asset_dir / f"magicfit-walkthrough{suffix}"
        for suffix in sorted(VIDEO_EXTENSIONS)
    ] + [
        asset_dir / f"walkthrough{suffix}"
        for suffix in sorted(VIDEO_EXTENSIONS)
    ]
    for candidate in preferred:
        if candidate.is_file():
            return candidate
    matches = [path for path in sorted(asset_dir.iterdir()) if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS]
    return matches[0] if matches else None


def _discover_receipt(asset_dir: Path) -> Path | None:
    preferred = (asset_dir / "magicfit-receipt.json", asset_dir / "receipt.json")
    for candidate in preferred:
        if candidate.is_file():
            return candidate
    matches = sorted(asset_dir.glob("*.json"))
    return matches[0] if matches else None


def _artifact_dir() -> Path:
    return Path(os.getenv("EA_ARTIFACT_DIR") or "/data/artifacts").expanduser().resolve()


def _run_import(row: dict[str, Any], *, env: dict[str, str]) -> dict[str, Any]:
    provider = _safe_provider(row.get("provider"))
    slug = _safe_slug(row.get("slug"))
    export_dir = Path(str(row.get("asset_dir") or row.get("export_dir") or "")).expanduser()
    export_zip = Path(str(row.get("export_zip") or row.get("zip") or "")).expanduser()
    result: dict[str, Any] = {
        "provider": provider or str(row.get("provider") or "").strip(),
        "slug": slug or str(row.get("slug") or "").strip(),
        "status": "failed",
    }
    if not provider:
        result["error"] = "unsupported_provider"
        return result
    if not slug:
        result["error"] = "invalid_tour_slug"
        return result
    if provider in {"3dvista", "pano2vr"} and export_zip.is_file():
        pass
    elif not export_dir.is_dir():
        result["error"] = "asset_dir_missing"
        return result
    script = ROOT / "scripts" / SCRIPT_BY_PROVIDER[provider]
    cmd = [sys.executable, str(script), "--slug", slug]
    if provider in {"3dvista", "pano2vr"}:
        if export_zip.is_file():
            cmd.extend(["--export-zip", str(export_zip.resolve())])
        else:
            cmd.extend(["--export-dir", str(export_dir.resolve())])
        entry = str(row.get("entry") or "").strip()
        if entry:
            cmd.extend(["--entry", entry])
        if provider == "3dvista":
            provenance_root = export_dir if str(row.get("asset_dir") or row.get("export_dir") or "").strip() else export_zip.parent
            provenance = _first_existing_file(
                row.get("provenance_receipt"),
                row.get("target_provenance_receipt"),
                provenance_root / "3dvista-target-provenance.json",
                provenance_root / "3dvista-provenance.json",
                provenance_root / "provenance.json",
            )
            if provenance is None:
                result["error"] = "3dvista_target_provenance_missing"
                return result
            cmd.extend(["--provenance-receipt", str(provenance.resolve())])
    elif provider == "krpano":
        panorama = _first_existing_file(row.get("panorama"), row.get("panorama_path")) or _discover_panorama(export_dir)
        cube_faces = [
            path
            for path in [
                _first_existing_file(row.get(f"cube_face_{index}"), row.get(f"cube_face_{index}_path"))
                for index in range(1, 7)
            ]
            if path is not None
        ]
        if panorama is not None:
            cmd.extend(["--panorama", str(panorama.resolve())])
        elif len(cube_faces) == 6:
            for face in cube_faces:
                cmd.extend(["--cube-face", str(face.resolve())])
        else:
            discovered_faces = _discover_cube_faces(export_dir)
            if len(discovered_faces) == 6:
                for face in discovered_faces:
                    cmd.extend(["--cube-face", str(face.resolve())])
            else:
                result["error"] = "krpano_assets_missing"
                return result
    elif provider == "magicfit":
        video = _first_existing_file(row.get("video_path"), row.get("video")) or _discover_video(export_dir)
        receipt = _first_existing_file(row.get("source_receipt"), row.get("receipt_path"), row.get("receipt")) or _discover_receipt(export_dir)
        if video is None:
            result["error"] = "magicfit_video_missing"
            return result
        cmd.extend(["--video-path", str(video.resolve())])
        if receipt is not None:
            cmd.extend(["--source-receipt", str(receipt.resolve())])
    target_subdir = str(row.get("target_subdir") or "").strip()
    if target_subdir and provider in {"3dvista", "pano2vr", "krpano"}:
        cmd.extend(["--target-subdir", target_subdir])
    target_relpath = str(row.get("target_relpath") or "").strip()
    if target_relpath and provider == "magicfit":
        cmd.extend(["--target-relpath", target_relpath])
    timeout_seconds = bounded_env_int(
        "PROPERTYQUARRY_TOUR_IMPORT_TIMEOUT_SECONDS",
        default=120,
        minimum=5,
        maximum=900,
    )
    maximum_output_bytes = bounded_env_int(
        "PROPERTYQUARRY_TOUR_IMPORT_MAX_OUTPUT_BYTES",
        default=1024 * 1024,
        minimum=16 * 1024,
        maximum=8 * 1024 * 1024,
    )
    try:
        completed = run_bounded_subprocess(
            cmd,
            cwd=ROOT,
            env=env,
            timeout_seconds=timeout_seconds,
            maximum_output_bytes=maximum_output_bytes,
        )
    except TourHostSafetyError as exc:
        result["error"] = str(exc)
        return result
    result["returncode"] = completed.returncode
    if completed.returncode != 0:
        result["error"] = (completed.stderr or completed.stdout or "import_failed").strip().splitlines()[-1][:240]
        return result
    try:
        imported = json.loads(completed.stdout or "{}")
    except Exception:
        imported = {}
    result["status"] = "imported"
    result["control_url"] = str(imported.get("control_url") or f"/tours/{slug}/control/{provider}")
    result["entry_relpath"] = str(imported.get("entry_relpath") or "")
    result["video_relpath"] = str(imported.get("video_relpath") or "")
    result["asset_count"] = int(imported.get("asset_count") or 0)
    return result


def build_import_receipt(manifest_path: Path, *, public_tour_dir: Path | None = None) -> dict[str, Any]:
    imports = _load_manifest(manifest_path)
    env = dict(os.environ)
    if public_tour_dir is not None:
        env["EA_PUBLIC_TOUR_DIR"] = str(public_tour_dir.expanduser().resolve())
    try:
        with bounded_lane_lock("batch-import"):
            # Deliberately sequential: one provider subprocess at a time bounds
            # CPU, memory and disk amplification regardless of manifest size.
            rows = [_run_import(row, env=env) for row in imports]
    except TourHostSafetyError as exc:
        rows = [
            {
                "provider": _safe_provider(row.get("provider")) or str(row.get("provider") or "").strip(),
                "slug": _safe_slug(row.get("slug")) or str(row.get("slug") or "").strip(),
                "status": "failed",
                "error": str(exc),
            }
            for row in imports
        ]
    failed = [row for row in rows if row.get("status") != "imported"]
    return {
        "status": "pass" if not failed else "fail",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "manifest": str(manifest_path.resolve()),
        "public_tour_dir": env.get("EA_PUBLIC_TOUR_DIR") or "",
        "import_count": len(rows),
        "imported_count": len(rows) - len(failed),
        "failed_count": len(failed),
        "host_safety": {
            "concurrency_limit": 1,
            "retry_limit": 0,
            "subprocess_timeout_seconds": bounded_env_int(
                "PROPERTYQUARRY_TOUR_IMPORT_TIMEOUT_SECONDS",
                default=120,
                minimum=5,
                maximum=900,
            ),
            "subprocess_output_max_bytes": bounded_env_int(
                "PROPERTYQUARRY_TOUR_IMPORT_MAX_OUTPUT_BYTES",
                default=1024 * 1024,
                minimum=16 * 1024,
                maximum=8 * 1024 * 1024,
            ),
        },
        "imports": rows,
        "notes": [
            "Each row delegates to the hardened single-export or asset importer for its provider.",
            "3DVista rows require a private target-bound provenance receipt for the exact tour and export bytes.",
            "Receipts include slugs, provider keys, control URLs, and relative entry paths only; raw provider credentials and private listing data are not required.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-import verified PropertyQuarry tour exports and walkthrough assets.")
    parser.add_argument("--manifest", required=True, help="JSON file: {'imports': [{'slug','provider','export_dir', optional 'entry','target_subdir'}]}.")
    parser.add_argument("--public-tour-dir", default="", help="Override EA_PUBLIC_TOUR_DIR for this run.")
    parser.add_argument("--write", default="", help="Receipt path. Defaults to EA_ARTIFACT_DIR/property-tour-export-imports.json.")
    args = parser.parse_args()
    manifest_path = Path(args.manifest).expanduser().resolve()
    if not manifest_path.is_file():
        raise SystemExit("import_manifest_missing")
    receipt = build_import_receipt(
        manifest_path,
        public_tour_dir=Path(args.public_tour_dir) if str(args.public_tour_dir or "").strip() else None,
    )
    write_path = Path(args.write).expanduser().resolve() if str(args.write or "").strip() else _artifact_dir() / "property-tour-export-imports.json"
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("status", "import_count", "imported_count", "failed_count", "imports")}, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
