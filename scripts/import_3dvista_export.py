#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

try:
    from property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
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
        bounded_env_int,
        bounded_lane_lock,
        require_bounded_file,
        require_bounded_tree,
        require_free_disk,
        safe_extract_tour_zip,
        tour_manifest_max_bytes,
    )

if __package__:
    from .property_tour_3dvista_provenance import (
        THREE_D_VISTA_PROVENANCE_FILENAMES,
        find_3dvista_provenance_receipt,
        export_tree_sha256,
        load_json_object,
        sha256_file,
        validate_3dvista_target_provenance,
    )
else:
    from property_tour_3dvista_provenance import (
        THREE_D_VISTA_PROVENANCE_FILENAMES,
        find_3dvista_provenance_receipt,
        export_tree_sha256,
        load_json_object,
        sha256_file,
        validate_3dvista_target_provenance,
    )

_3DVISTA_EXPORT_MARKERS = ("tdvplayer", "tdvplayerapi", "tourviewer")
_3DVISTA_TRIAL_MARKERS = (
    "created with the trial of 3dvista",
    "trial of 3dvista vt pro",
)
_TEXT_RUNTIME_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".json", ".xml"}
_MAX_MARKER_SCAN_BYTES = 1_000_000
_MAX_MARKER_SCAN_FILES = 240
_SOURCE_PROJECT_DEFAULT = "propertyquarry"
_PROPERTYQUARRY_SOURCE_PROJECTS = {"propertyquarry", "property-quarry", "propertyquarry.com", "property_quarry"}


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
        raise SystemExit(f"3dvista_entry_not_found:{explicit_entry}")
    preferred = (
        "index.html",
        "index.htm",
        "tour.html",
        "virtualtour.html",
    )
    for name in preferred:
        candidate = export_dir / name
        if candidate.is_file():
            return candidate.resolve()
    matches = sorted(export_dir.rglob("*.html")) + sorted(export_dir.rglob("*.htm"))
    if not matches:
        raise SystemExit("3dvista_export_entry_missing")
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


def _entry_has_3dvista_markers(export_dir: Path, entry: Path) -> bool:
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
        if _text_asset_has_markers(candidate, _3DVISTA_EXPORT_MARKERS):
            return True
    return False


def _export_has_trial_branding(export_dir: Path, entry: Path) -> bool:
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
        try:
            if candidate.stat().st_size > _MAX_MARKER_SCAN_BYTES:
                continue
            body = candidate.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if any(marker in body for marker in _3DVISTA_TRIAL_MARKERS):
            return True
    return False


def _copy_export(export_dir: Path, target_dir: Path) -> None:
    source_budget = require_bounded_tree(
        export_dir,
        reason_prefix="3dvista_export",
    )
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    require_free_disk(
        target_dir.parent,
        reason_prefix="3dvista_import",
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
            if item.name in THREE_D_VISTA_PROVENANCE_FILENAMES:
                continue
            target = staging_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
        require_bounded_tree(staging_dir, reason_prefix="3dvista_export")
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
        require_bounded_tree(target_dir, reason_prefix="3dvista_export")
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


def _write_json_private(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def _attach_existing_target_provenance(
    *,
    slug: str,
    bundle_dir: Path,
    manifest_path: Path,
    target_subdir: str,
    explicit_entry: str,
    provenance_receipt: str,
) -> int:
    """Bind a reviewed receipt to an already imported export without recopying it."""

    target_dir = (bundle_dir / target_subdir).resolve()
    if bundle_dir.resolve() not in target_dir.parents or not target_dir.is_dir():
        raise SystemExit("3dvista_existing_export_missing")
    try:
        require_bounded_tree(target_dir, reason_prefix="3dvista_existing_export")
        require_bounded_file(
            manifest_path,
            reason_prefix="tour_manifest",
            maximum_bytes=tour_manifest_max_bytes(),
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"invalid_tour_manifest:{type(exc).__name__}") from exc
    if not isinstance(manifest, dict):
        raise SystemExit("invalid_tour_manifest")

    configured_entry = _safe_relpath(
        explicit_entry or str(manifest.get("three_d_vista_entry_relpath") or "")
    )
    prefix = f"{target_subdir}/"
    if configured_entry.startswith(prefix):
        entry_rel_to_export = configured_entry[len(prefix) :]
    elif explicit_entry:
        entry_rel_to_export = configured_entry
    else:
        raise SystemExit("3dvista_existing_entry_invalid")
    if not entry_rel_to_export:
        raise SystemExit("3dvista_existing_entry_invalid")
    entry = _find_entry(target_dir, entry_rel_to_export)
    if not _entry_has_3dvista_markers(target_dir, entry):
        raise SystemExit("3dvista_export_entry_unverified")
    if _export_has_trial_branding(target_dir, entry):
        raise SystemExit("3dvista_trial_branding_present")

    provenance_path = (
        Path(provenance_receipt).expanduser().resolve()
        if str(provenance_receipt or "").strip()
        else find_3dvista_provenance_receipt(target_dir)
    )
    if provenance_path is None or not provenance_path.is_file():
        raise SystemExit("3dvista_target_provenance_missing")
    try:
        require_bounded_file(
            provenance_path,
            reason_prefix="3dvista_target_provenance",
            maximum_bytes=bounded_env_int(
                "PROPERTYQUARRY_TOUR_PROVENANCE_MAX_BYTES",
                default=1024 * 1024,
                minimum=1_024,
                maximum=8 * 1024 * 1024,
            ),
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc
    raw_provenance = load_json_object(provenance_path)
    normalized_provenance, provenance_errors = validate_3dvista_target_provenance(
        raw_provenance,
        target_slug=slug,
        export_dir=target_dir,
        entry_relpath=entry_rel_to_export,
    )
    if provenance_errors:
        raise SystemExit(
            f"3dvista_target_provenance_invalid:{','.join(provenance_errors)}"
        )
    tree_digest_before = export_tree_sha256(target_dir)
    if not tree_digest_before:
        raise SystemExit("3dvista_existing_export_unhashable")
    # Recompute after receipt validation so an export replacement cannot be
    # silently attached to the digest validated above.
    tree_digest_stable = export_tree_sha256(target_dir)
    if tree_digest_stable != tree_digest_before:
        raise SystemExit("3dvista_existing_export_changed")

    private_path = bundle_dir / "tour.private.json"
    private_payload = load_json_object(private_path)
    normalized_provenance["source_receipt_sha256"] = sha256_file(provenance_path)
    normalized_provenance["attached_at"] = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    normalized_provenance["target_subdir"] = target_subdir
    private_payload["three_d_vista_target_provenance"] = normalized_provenance
    _write_json_private(private_path, private_payload)

    tree_digest_after = export_tree_sha256(target_dir)
    if tree_digest_after != tree_digest_before:
        private_payload.pop("three_d_vista_target_provenance", None)
        _write_json_private(private_path, private_payload)
        raise SystemExit("3dvista_existing_export_changed")
    print(
        json.dumps(
            {
                "status": "provenance_attached",
                "slug": slug,
                "entry_relpath": f"{target_subdir}/{entry_rel_to_export}",
                "control_url": f"/tours/{slug}/control/3dvista",
                "export_tree_sha256": tree_digest_after,
                "provenance_schema": normalized_provenance["schema"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> Path:
    if zip_path.suffix.lower() != ".zip":
        raise SystemExit("3dvista_export_zip_invalid")
    try:
        return safe_extract_tour_zip(
            zip_path,
            target_dir,
            reason_prefix="3dvista_export_zip",
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc


def _main_unlocked() -> int:
    parser = argparse.ArgumentParser(description="Import a 3DVista VT Pro export into a PropertyQuarry public tour bundle.")
    parser.add_argument("--slug", required=True, help="Existing PropertyQuarry public tour slug.")
    parser.add_argument("--export-dir", default="", help="Directory exported by 3DVista VT Pro.")
    parser.add_argument("--export-zip", default="", help="Zip file exported by 3DVista VT Pro.")
    parser.add_argument("--entry", default="", help="Optional entry HTML path relative to export-dir.")
    parser.add_argument("--target-subdir", default="3dvista", help="Subdirectory inside the tour bundle.")
    parser.add_argument(
        "--attach-provenance-only",
        action="store_true",
        help=(
            "Validate and attach a property-bound receipt to the existing target export "
            "without recopying export bytes."
        ),
    )
    parser.add_argument(
        "--provenance-receipt",
        default="",
        help=(
            "Private property-bound review receipt. If omitted for a directory export, "
            "3dvista-target-provenance.json is discovered at the export root."
        ),
    )
    parser.add_argument(
        "--source-project",
        default=(os.getenv("THREE_D_VISTA_WHITE_LABEL_SOURCE_PROJECT") or _SOURCE_PROJECT_DEFAULT),
        help="Optional white-label source project label used for verifier proof checks.",
    )
    args = parser.parse_args()

    slug = _safe_relpath(args.slug)
    if "/" in slug or not slug:
        raise SystemExit("invalid_tour_slug")
    has_export_dir = bool(str(args.export_dir or "").strip())
    has_export_zip = bool(str(args.export_zip or "").strip())
    if args.attach_provenance_only:
        if has_export_dir or has_export_zip:
            raise SystemExit("3dvista_attach_provenance_rejects_export_source")
    elif has_export_dir == has_export_zip:
        raise SystemExit("3dvista_requires_export_dir_or_zip")

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
        require_free_disk(
            bundle_dir,
            reason_prefix="3dvista_import",
            expected_write_bytes=1024 * 1024,
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc
    target_subdir = _safe_relpath(args.target_subdir or "3dvista") or "3dvista"
    target_dir = (bundle_dir / target_subdir).resolve()
    if bundle_dir.resolve() not in target_dir.parents:
        raise SystemExit("invalid_3dvista_target")

    if args.attach_provenance_only:
        return _attach_existing_target_provenance(
            slug=slug,
            bundle_dir=bundle_dir,
            manifest_path=manifest_path,
            target_subdir=target_subdir,
            explicit_entry=str(args.entry or ""),
            provenance_receipt=str(args.provenance_receipt or ""),
        )

    with contextlib.ExitStack() as stack:
        if str(args.export_zip or "").strip():
            temp_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="propertyquarry-3dvista-")))
            export_dir = _safe_extract_zip(Path(args.export_zip).expanduser().resolve(), temp_dir)
        else:
            export_dir = Path(args.export_dir).expanduser().resolve()
        if not export_dir.is_dir():
            raise SystemExit("3dvista_export_dir_missing")
        try:
            require_bounded_tree(export_dir, reason_prefix="3dvista_export")
        except TourHostSafetyError as exc:
            raise SystemExit(str(exc)) from exc
        entry = _find_entry(export_dir, args.entry)
        if not _entry_has_3dvista_markers(export_dir, entry):
            raise SystemExit("3dvista_export_entry_unverified")
        trial_branding_present = _export_has_trial_branding(export_dir, entry)
        if trial_branding_present:
            raise SystemExit("3dvista_trial_branding_present")
        entry_rel_to_export = entry.relative_to(export_dir).as_posix()
        provenance_path = (
            Path(args.provenance_receipt).expanduser().resolve()
            if str(args.provenance_receipt or "").strip()
            else find_3dvista_provenance_receipt(export_dir)
        )
        if provenance_path is None or not provenance_path.is_file():
            raise SystemExit("3dvista_target_provenance_missing")
        try:
            require_bounded_file(
                provenance_path,
                reason_prefix="3dvista_target_provenance",
                maximum_bytes=bounded_env_int(
                    "PROPERTYQUARRY_TOUR_PROVENANCE_MAX_BYTES",
                    default=1024 * 1024,
                    minimum=1_024,
                    maximum=8 * 1024 * 1024,
                ),
            )
        except TourHostSafetyError as exc:
            raise SystemExit(str(exc)) from exc
        raw_provenance = load_json_object(provenance_path)
        normalized_provenance, provenance_errors = validate_3dvista_target_provenance(
            raw_provenance,
            target_slug=slug,
            export_dir=export_dir,
            entry_relpath=entry_rel_to_export,
        )
        if provenance_errors:
            raise SystemExit(f"3dvista_target_provenance_invalid:{','.join(provenance_errors)}")
        provenance_receipt_sha256 = sha256_file(provenance_path)
        _copy_export(export_dir, target_dir)

    entry_relpath = f"{target_subdir}/{entry_rel_to_export}"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["control_mode"] = "3dvista"
    payload["viewer_provider"] = "3dvista_vt_pro"
    payload["three_d_vista_entry_relpath"] = entry_relpath
    payload["three_d_vista_export_root_relpath"] = target_subdir
    for private_key in (
        "three_d_vista_import",
        "three_d_vista_white_label_proof",
        "three_d_vista_browser_render_proof",
        "three_d_vista_target_provenance",
        "three_d_vista_url",
        "threedvista_url",
        "3dvista_url",
    ):
        payload.pop(private_key, None)
    source_project = str(args.source_project or "").strip() or _SOURCE_PROJECT_DEFAULT
    propertyquarry_source = source_project.lower() in _PROPERTYQUARRY_SOURCE_PROJECTS
    private_path = bundle_dir / "tour.private.json"
    private_payload = load_json_object(private_path)
    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    private_payload["three_d_vista_import"] = {
        "source": "3dvista_vt_pro_export",
        "entry_relpath": entry_relpath,
        "target_subdir": target_subdir,
        "source_project": source_project,
    }
    private_payload["three_d_vista_white_label_proof"] = {
        "source_project": source_project,
        "source": "3dvista_import_script",
        "proof_kind": "local_vt_pro_export",
        "non_trial_export_verified": True,
        "trial_branding_present": False,
        "propertyquarry_tour_metadata": propertyquarry_source,
        "trial_branding_checked": True,
        "imported_at": imported_at,
        "target_subdir": target_subdir,
    }
    normalized_provenance["source_receipt_sha256"] = provenance_receipt_sha256
    normalized_provenance["imported_at"] = imported_at
    normalized_provenance["target_subdir"] = target_subdir
    private_payload["three_d_vista_target_provenance"] = normalized_provenance
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_json_private(private_path, private_payload)
    print(
        json.dumps(
            {
                "status": "imported",
                "slug": slug,
                "entry_relpath": entry_relpath,
                "control_url": f"/tours/{slug}/control/3dvista",
                "provenance_schema": normalized_provenance["schema"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    try:
        with bounded_lane_lock("3dvista-import"):
            return _main_unlocked()
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
