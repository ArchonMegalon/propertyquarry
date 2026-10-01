#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.import_3dvista_export import (
    _entry_has_3dvista_markers,
    _export_has_trial_branding,
    _find_entry,
    _public_tour_dir,
    _safe_relpath,
)
from scripts.property_tour_governed_reservation import (
    require_dynamic_tour_slug,
)


DEFAULT_LOCAL_STORE_DIR = (
    Path.home()
    / ".wine/drive_c/users/tibor/AppData/Roaming/tdv.show/Local Store/tdvplayer_dir/default"
)
RUNTIME_REQUIRED_FILES = ("tdvplayer.js", "tdvplayer.json")
TRIAL_BLOCK_PATTERNS = (
    re.compile(
        r"(?is)<div[^>]*>\s*<span[^>]*>\s*created with the trial of 3dvista vt pro\s*</span>\s*</div>"
    ),
    re.compile(
        r"(?is)<div[^>]*>\s*<a[^>]*href\s*=\s*[\"']https://www\.3dvista\.com[\"'][^>]*>.*?</a>\s*</div>"
    ),
)
TRIAL_MARKER_PATTERN = re.compile(r"(?i)created with the trial of 3dvista vt pro")
BRAND_URL_PATTERN = re.compile(r"(?i)https://www\.3dvista\.com")
BRAND_TEXT_PATTERN = re.compile(r"(?i)www\.3dvista\.com")


def _load_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("tour_manifest_invalid")
    return payload


def _bundle_dir_for_slug(slug: str) -> Path:
    safe_slug = _safe_relpath(slug)
    if "/" in safe_slug or not safe_slug:
        raise SystemExit("invalid_tour_slug")
    try:
        require_dynamic_tour_slug(safe_slug)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    bundle_dir = (_public_tour_dir() / safe_slug).resolve()
    if not bundle_dir.is_dir():
        raise SystemExit("tour_bundle_missing")
    return bundle_dir


def _copy_runtime_tree(source_dir: Path, target_dir: Path) -> list[str]:
    copied: list[str] = []
    for item in sorted(source_dir.rglob("*")):
        if not item.is_file():
            continue
        relpath = item.relative_to(source_dir)
        target = target_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied.append(relpath.as_posix())
    return copied


def _copy_bundle_tree(source_dir: Path, target_dir: Path) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in list(target_dir.iterdir()):
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    copied_count = 0
    for item in sorted(source_dir.rglob("*")):
        relpath = item.relative_to(source_dir)
        target = target_dir / relpath
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied_count += 1
    return copied_count


def _runtime_version(runtime_dir: Path) -> int | None:
    version_path = runtime_dir / "tdvplayer.json"
    if not version_path.is_file():
        return None
    try:
        payload = json.loads(version_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    version = payload.get("version")
    if not isinstance(version, dict):
        return None
    minor = version.get("minor")
    return int(minor) if isinstance(minor, int) else None


def _sanitize_entry_html(entry_path: Path) -> bool:
    body = entry_path.read_text(encoding="utf-8", errors="replace")
    updated = body
    for pattern in TRIAL_BLOCK_PATTERNS:
        updated = pattern.sub("", updated)
    updated = TRIAL_MARKER_PATTERN.sub("", updated)
    updated = BRAND_URL_PATTERN.sub("#", updated)
    updated = BRAND_TEXT_PATTERN.sub("", updated)
    changed = updated != body
    if changed:
        entry_path.write_text(updated, encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh a PropertyQuarry 3DVista export with the delivered private-viewer runtime."
    )
    parser.add_argument("--slug", required=True, help="Existing PropertyQuarry public tour slug.")
    parser.add_argument(
        "--local-store-dir",
        default=str(DEFAULT_LOCAL_STORE_DIR),
        help="3DVista Local Store tdvplayer_dir/default path.",
    )
    parser.add_argument(
        "--export-root-relpath",
        default="",
        help="Optional export root relpath inside the tour bundle. Defaults to tour.json three_d_vista_export_root_relpath or 3dvista.",
    )
    parser.add_argument(
        "--entry-relpath",
        default="",
        help="Optional entry HTML relpath inside the tour bundle. Defaults to tour.json three_d_vista_entry_relpath or auto-discovery.",
    )
    parser.add_argument(
        "--source-project",
        default="propertyquarry",
        help="White-label source project label stored in tour.json proof metadata.",
    )
    parser.add_argument(
        "--vendor-delivered-date",
        default="",
        help="Optional YYYY-MM-DD date when the vendor confirmed the custom/private viewer was ready.",
    )
    parser.add_argument(
        "--mirror-target-root",
        action="append",
        default=[],
        help="Optional additional public-tour root to fully mirror the refreshed slug into.",
    )
    args = parser.parse_args()

    bundle_dir = _bundle_dir_for_slug(args.slug)
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.is_file():
        raise SystemExit("tour_manifest_missing")
    payload = _load_manifest(manifest_path)

    export_root_relpath = (
        _safe_relpath(args.export_root_relpath)
        or _safe_relpath(str(payload.get("three_d_vista_export_root_relpath") or ""))
        or "3dvista"
    )
    export_root = (bundle_dir / export_root_relpath).resolve()
    if bundle_dir not in export_root.parents or not export_root.is_dir():
        raise SystemExit("3dvista_export_root_missing")

    entry_relpath = _safe_relpath(args.entry_relpath) or _safe_relpath(
        str(payload.get("three_d_vista_entry_relpath") or "")
    )
    if entry_relpath:
        entry_path = (bundle_dir / entry_relpath).resolve()
        if bundle_dir not in entry_path.parents or not entry_path.is_file():
            raise SystemExit("3dvista_entry_missing")
    else:
        entry_path = _find_entry(export_root)
        entry_relpath = entry_path.relative_to(bundle_dir).as_posix()

    local_store_dir = Path(args.local_store_dir).expanduser().resolve()
    if not local_store_dir.is_dir():
        raise SystemExit("3dvista_local_store_missing")
    for filename in RUNTIME_REQUIRED_FILES:
        if not (local_store_dir / filename).is_file():
            raise SystemExit(f"3dvista_local_store_missing_{filename}")

    runtime_target_dir = (export_root / "lib").resolve()
    if export_root not in runtime_target_dir.parents and runtime_target_dir != export_root:
        raise SystemExit("invalid_runtime_target")
    runtime_target_dir.mkdir(parents=True, exist_ok=True)

    version_before = _runtime_version(runtime_target_dir)
    copied_assets = _copy_runtime_tree(local_store_dir, runtime_target_dir)
    html_sanitized = _sanitize_entry_html(entry_path)

    if not _entry_has_3dvista_markers(export_root, entry_path):
        raise SystemExit("3dvista_export_entry_unverified_after_refresh")
    if _export_has_trial_branding(export_root, entry_path):
        raise SystemExit("3dvista_trial_branding_still_present_after_refresh")

    version_after = _runtime_version(runtime_target_dir)
    proof = dict(payload.get("three_d_vista_white_label_proof") or {})
    if not isinstance(payload.get("three_d_vista_white_label_proof"), dict):
        proof = {}
    delivered_date = str(args.vendor_delivered_date or "").strip()
    refreshed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    proof.update(
        {
            "source_project": str(args.source_project or "propertyquarry").strip() or "propertyquarry",
            "source": "3dvista_private_viewer_runtime_refresh",
            "proof_kind": "local_private_viewer_runtime_refresh",
            "private_viewer_verified": True,
            "non_trial_export_verified": True,
            "trial_branding_present": False,
            "propertyquarry_tour_metadata": True,
            "trial_branding_checked": True,
            "player_runtime_version_before": version_before,
            "player_runtime_version_after": version_after,
            "runtime_refreshed_from": str(local_store_dir),
            "runtime_refreshed_at": refreshed_at,
            "entry_trial_markup_removed": html_sanitized,
        }
    )
    if delivered_date:
        proof["private_viewer_delivered_date"] = delivered_date

    payload["control_mode"] = "3dvista"
    payload["viewer_provider"] = "3dvista_vt_pro"
    payload["three_d_vista_entry_relpath"] = entry_relpath
    payload["three_d_vista_export_root_relpath"] = export_root_relpath
    import_payload = payload.get("three_d_vista_import")
    if not isinstance(import_payload, dict):
        import_payload = {}
    payload["three_d_vista_import"] = {
        **import_payload,
        "source": "3dvista_private_viewer_runtime_refresh",
        "entry_relpath": entry_relpath,
        "target_subdir": export_root_relpath,
        "source_project": proof["source_project"],
    }
    payload["three_d_vista_white_label_proof"] = proof
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    mirrored_roots: list[str] = []
    mirrored_file_counts: dict[str, int] = {}
    for raw_root in args.mirror_target_root:
        mirror_root = Path(str(raw_root or "").strip()).expanduser().resolve()
        if not str(raw_root or "").strip():
            continue
        mirror_bundle_dir = (mirror_root / _safe_relpath(args.slug)).resolve()
        if mirror_root not in mirror_bundle_dir.parents:
            raise SystemExit("invalid_mirror_target_root")
        if mirror_bundle_dir == bundle_dir:
            continue
        copied_count = _copy_bundle_tree(bundle_dir, mirror_bundle_dir)
        mirror_manifest_path = mirror_bundle_dir / "tour.json"
        if not mirror_manifest_path.is_file():
            raise SystemExit("mirror_tour_manifest_missing_after_copy")
        mirror_entry_path = (mirror_bundle_dir / entry_relpath).resolve()
        mirror_export_root = (mirror_bundle_dir / export_root_relpath).resolve()
        if not mirror_entry_path.is_file():
            raise SystemExit("mirror_3dvista_entry_missing_after_copy")
        if not _entry_has_3dvista_markers(mirror_export_root, mirror_entry_path):
            raise SystemExit("mirror_3dvista_export_entry_unverified_after_copy")
        if _export_has_trial_branding(mirror_export_root, mirror_entry_path):
            raise SystemExit("mirror_3dvista_trial_branding_still_present_after_copy")
        mirrored_roots.append(str(mirror_root))
        mirrored_file_counts[str(mirror_root)] = copied_count

    print(
        json.dumps(
            {
                "status": "refreshed",
                "slug": _safe_relpath(args.slug),
                "entry_relpath": entry_relpath,
                "runtime_assets_copied": len(copied_assets),
                "player_runtime_version_before": version_before,
                "player_runtime_version_after": version_after,
                "mirrored_roots": mirrored_roots,
                "mirrored_file_counts": mirrored_file_counts,
                "control_url": f"/tours/{_safe_relpath(args.slug)}/control/3dvista",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
