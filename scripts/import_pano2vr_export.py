#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

try:
    from property_tour_host_safety import (
        TourHostSafetyError,
        bounded_lane_lock,
        require_bounded_file,
        require_bounded_tree,
        require_free_disk,
        safe_extract_tour_zip,
        tour_manifest_max_bytes,
    )
except ModuleNotFoundError:
    from scripts.property_tour_host_safety import (
        TourHostSafetyError,
        bounded_lane_lock,
        require_bounded_file,
        require_bounded_tree,
        require_free_disk,
        safe_extract_tour_zip,
        tour_manifest_max_bytes,
    )

_PANO2VR_EXPORT_MARKERS = ("ggpkg", "ggskin", "pano.xml", "tour.js")
_TEXT_RUNTIME_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".json", ".xml"}
_MAX_MARKER_SCAN_BYTES = 1_000_000
_MAX_MARKER_SCAN_FILES = 240


def _public_tour_dir() -> Path:
    return Path(os.getenv("EA_PUBLIC_TOUR_DIR") or "/data/public_property_tours").expanduser().resolve()


def _safe_relpath(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
    return "/".join(parts)


def _find_entry(export_dir: Path, explicit_entry: str = "") -> Path:
    if explicit_entry:
        candidate = (export_dir / _safe_relpath(explicit_entry)).resolve()
        if export_dir in candidate.parents and candidate.is_file():
            return candidate
        raise SystemExit(f"pano2vr_entry_not_found:{explicit_entry}")
    preferred = (
        "index.html",
        "index.htm",
        "tour.html",
        "output/index.html",
    )
    for name in preferred:
        candidate = export_dir / name
        if candidate.is_file():
            return candidate.resolve()
    matches = sorted(export_dir.rglob("*.html")) + sorted(export_dir.rglob("*.htm"))
    if not matches:
        raise SystemExit("pano2vr_export_entry_missing")
    return matches[0].resolve()


def _text_asset_has_markers(path: Path, markers: tuple[str, ...]) -> bool:
    if path.suffix.lower() not in _TEXT_RUNTIME_SUFFIXES:
        return False
    try:
        if path.stat().st_size > _MAX_MARKER_SCAN_BYTES:
            return False
        body = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(marker in body for marker in markers)


def _entry_has_pano2vr_markers(export_dir: Path, entry: Path) -> bool:
    export_root = export_dir.resolve()
    candidates = [entry.resolve()]
    for candidate in sorted(export_root.rglob("*")):
        if len(candidates) >= _MAX_MARKER_SCAN_FILES:
            break
        if candidate.is_file() and candidate.suffix.lower() in _TEXT_RUNTIME_SUFFIXES and candidate.resolve() not in candidates:
            candidates.append(candidate.resolve())
    for candidate in candidates:
        if export_root not in candidate.parents and candidate != export_root:
            continue
        if _text_asset_has_markers(candidate, _PANO2VR_EXPORT_MARKERS):
            return True
    return False


def _copy_export(export_dir: Path, target_dir: Path) -> None:
    source_budget = require_bounded_tree(
        export_dir,
        reason_prefix="pano2vr_export",
    )
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    require_free_disk(
        target_dir.parent,
        reason_prefix="pano2vr_import",
        expected_write_bytes=int(source_budget["total_bytes"]),
    )
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{target_dir.name}.import-",
            dir=target_dir.parent,
        )
    )
    backup_dir = target_dir.parent / f".{target_dir.name}.backup-{uuid4().hex}"
    target_moved = False
    staging_published = False
    try:
        for item in export_dir.iterdir():
            target = staging_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            elif item.is_file():
                shutil.copy2(item, target)
        require_bounded_tree(staging_dir, reason_prefix="pano2vr_export")
        for root, directory_names, filenames in os.walk(staging_dir, followlinks=False):
            Path(root).chmod(0o755)
            for directory_name in directory_names:
                (Path(root) / directory_name).chmod(0o755)
            for filename in filenames:
                (Path(root) / filename).chmod(0o644)
        if target_dir.exists() or target_dir.is_symlink():
            os.replace(target_dir, backup_dir)
            target_moved = True
        os.replace(staging_dir, target_dir)
        staging_published = True
        require_bounded_tree(target_dir, reason_prefix="pano2vr_export")
        if target_moved:
            shutil.rmtree(backup_dir)
            target_moved = False
    except Exception:
        if staging_published and target_dir.exists():
            shutil.rmtree(target_dir)
        if target_moved and backup_dir.exists():
            os.replace(backup_dir, target_dir)
            target_moved = False
        raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if backup_dir.exists() and not target_moved:
            shutil.rmtree(backup_dir, ignore_errors=True)


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> Path:
    if zip_path.suffix.lower() != ".zip":
        raise SystemExit("pano2vr_export_zip_invalid")
    try:
        return safe_extract_tour_zip(
            zip_path,
            target_dir,
            reason_prefix="pano2vr_export_zip",
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc


def _main_unlocked() -> int:
    parser = argparse.ArgumentParser(description="Import a Pano2VR export into a PropertyQuarry public tour bundle.")
    parser.add_argument("--slug", required=True, help="Existing PropertyQuarry public tour slug.")
    parser.add_argument("--export-dir", default="", help="Directory exported by Pano2VR.")
    parser.add_argument("--export-zip", default="", help="Zip file exported by Pano2VR.")
    parser.add_argument("--entry", default="", help="Optional entry HTML path relative to export-dir.")
    parser.add_argument("--target-subdir", default="pano2vr", help="Subdirectory inside the tour bundle.")
    args = parser.parse_args()

    slug = _safe_relpath(args.slug)
    if "/" in slug or not slug:
        raise SystemExit("invalid_tour_slug")
    if bool(str(args.export_dir or "").strip()) == bool(str(args.export_zip or "").strip()):
        raise SystemExit("pano2vr_requires_export_dir_or_zip")

    bundle_dir = _public_tour_dir() / slug
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.is_file():
        raise SystemExit("tour_manifest_missing")
    try:
        require_bounded_file(
            manifest_path,
            reason_prefix="tour_manifest",
            maximum_bytes=tour_manifest_max_bytes(),
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc
    target_subdir = _safe_relpath(args.target_subdir or "pano2vr") or "pano2vr"
    target_dir = (bundle_dir / target_subdir).resolve()
    if bundle_dir.resolve() not in target_dir.parents:
        raise SystemExit("invalid_pano2vr_target")

    with contextlib.ExitStack() as stack:
        if str(args.export_zip or "").strip():
            temp_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="propertyquarry-pano2vr-")))
            export_dir = _safe_extract_zip(Path(args.export_zip).expanduser().resolve(), temp_dir)
        else:
            export_dir = Path(args.export_dir).expanduser().resolve()
        if not export_dir.is_dir():
            raise SystemExit("pano2vr_export_dir_missing")
        try:
            require_bounded_tree(export_dir, reason_prefix="pano2vr_export")
        except TourHostSafetyError as exc:
            raise SystemExit(str(exc)) from exc
        entry = _find_entry(export_dir, args.entry)
        if not _entry_has_pano2vr_markers(export_dir, entry):
            raise SystemExit("pano2vr_export_entry_unverified")
        _copy_export(export_dir, target_dir)
        entry_rel_to_export = entry.relative_to(export_dir).as_posix()

    entry_relpath = f"{target_subdir}/{entry_rel_to_export}"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["control_mode"] = "pano2vr"
    payload["viewer_provider"] = "pano2vr"
    payload["pano2vr_entry_relpath"] = entry_relpath
    payload["pano2vr_import"] = {
        "source": "pano2vr_export",
        "entry_relpath": entry_relpath,
        "target_subdir": target_subdir,
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "imported",
                "slug": slug,
                "entry_relpath": entry_relpath,
                "control_url": f"/tours/{slug}/control/pano2vr",
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    try:
        with bounded_lane_lock("pano2vr-import"):
            return _main_unlocked()
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
