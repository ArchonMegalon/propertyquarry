#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from property_tour_host_safety import TourHostSafetyError, safe_extract_tour_zip
except ModuleNotFoundError:
    from scripts.property_tour_host_safety import TourHostSafetyError, safe_extract_tour_zip

if __package__:
    from .property_tour_3dvista_provenance import (
        find_3dvista_provenance_receipt,
        load_json_object,
        safe_relpath as _safe_provenance_relpath,
        validate_3dvista_target_provenance,
    )
else:
    from property_tour_3dvista_provenance import (
        find_3dvista_provenance_receipt,
        load_json_object,
        safe_relpath as _safe_provenance_relpath,
        validate_3dvista_target_provenance,
    )


PROVIDERS = ("3dvista", "pano2vr", "krpano", "magicfit")
PROVIDER_DROP_LAYOUTS = {
    "3dvista": "<drop>/<slug>/3dvista/ or <drop>/3dvista/<slug>/ with a complete 3DVista export",
    "pano2vr": "<drop>/<slug>/pano2vr/ or <drop>/pano2vr/<slug>/ with a complete Pano2VR export",
    "krpano": "<drop>/<slug>/krpano/ or <drop>/krpano/<slug>/ with panorama.jpg/png/webp or cube-face-1..6",
    "magicfit": "<drop>/<slug>/magicfit/ or <drop>/magicfit/<slug>/ with magicfit-walkthrough.mp4 and magicfit-receipt.json",
}
REJECTION_ACTIONS = {
    "tour_manifest_missing": "create or import the base hosted tour bundle first so tour.json exists under the public tour directory",
    "3dvista_export_entry_unverified": "copy the complete 3DVista export; the entry or bundled runtime must contain tdvplayer, tdvplayerapi, or tourviewer markers",
    "3dvista_trial_branding_present": "replace the trial-branded 3DVista export with a licensed 3DVista VT Pro export",
    "3dvista_target_provenance_missing": "run create_3dvista_provenance_template.py, then complete 3dvista-target-provenance.json with exact slug/export binding, approved reuse, and dated human property/visual match",
    "3dvista_target_provenance_invalid": "replace 3dvista-target-provenance.json with a passing propertyquarry.3dvista_target_provenance.v1 receipt for these exact export bytes",
    "pano2vr_export_entry_unverified": "copy the complete Pano2VR export; the entry or bundled runtime must contain ggpkg, ggskin, pano.xml, or tour.js markers",
    "krpano_assets_missing": "copy a real 2:1 panorama named panorama.jpg/png/webp or six cube faces named cube-face-1..6",
    "magicfit_video_missing": "copy the playable MagicFit walkthrough as magicfit-walkthrough.mp4/mov/webm or walkthrough.mp4/mov/webm",
    "magicfit_video_unverified": "replace the placeholder with a playable MagicFit video stream with positive duration",
    "magicfit_receipt_missing": "copy the matching MagicFit render receipt as magicfit-receipt.json or receipt.json",
    "magicfit_receipt_invalid": "replace the MagicFit receipt with valid JSON containing provider=magicfit and the target slug/output",
    "magicfit_receipt_provider_mismatch": "use the MagicFit receipt for this walkthrough; provider must be magicfit",
    "magicfit_receipt_output_mismatch": "use the MagicFit receipt that names the exact walkthrough video file in this drop folder",
    "magicfit_receipt_output_invalid": "fix the MagicFit receipt output_file path so it resolves to the copied video",
    "magicfit_receipt_target_mismatch": "use a MagicFit receipt whose target_slug, tour_slug, property_slug, slug, or hosted URL matches this tour slug",
    "unsupported_provider": "use one of the supported providers: 3dvista, pano2vr, krpano, or magicfit",
}
MARKERS_BY_PROVIDER = {
    "3dvista": ("tdvplayer", "tdvplayerapi", "tourviewer"),
    "pano2vr": ("ggpkg", "ggskin", "pano.xml", "tour.js"),
}
FORBIDDEN_MARKERS_BY_PROVIDER = {
    "3dvista": ("created with the trial of 3dvista", "trial of 3dvista vt pro"),
}
ENTRY_NAMES = ("index.html", "index.htm", "tour.html", "virtualtour.html", "output/index.html")
TEXT_RUNTIME_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".json", ".xml"}
EXPORT_ARCHIVE_SUFFIXES = {".zip"}
PANORAMA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
MAX_MARKER_SCAN_BYTES = 1_000_000
MAX_MARKER_SCAN_FILES = 240
EQUIRECTANGULAR_MIN_RATIO = 1.9
EQUIRECTANGULAR_MAX_RATIO = 2.1
OPERATOR_DROP_LANE_SLUG = "_operator-import-lane"


def _default_drop_dir() -> Path:
    return Path(
        os.getenv("PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR")
        or os.getenv("PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR")
        or "/data/incoming_property_tours"
    ).expanduser()


def _artifact_dir() -> Path:
    return Path(os.getenv("EA_ARTIFACT_DIR") or "/data/artifacts").expanduser()


def _safe_slug(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/").strip("/")
    if not raw or "/" in raw or raw in {".", ".."} or ".." in raw:
        return ""
    return raw


def _is_operator_drop_lane_slug(value: object) -> bool:
    return _safe_slug(value) == OPERATOR_DROP_LANE_SLUG


def _provider_from_text(value: object, *, allow_embedded: bool = False) -> str:
    normalized = "".join(
        char
        for char in str(value or "").strip().lower()
        if char.isalnum()
    )
    if normalized in {"3dvista", "threedvista", "threevista"} or (
        allow_embedded and ("3dvista" in normalized or "threedvista" in normalized)
    ):
        return "3dvista"
    if normalized in {"pano2vr", "pano2v"} or (allow_embedded and "pano2vr" in normalized):
        return "pano2vr"
    if normalized == "krpano" or (allow_embedded and "krpano" in normalized):
        return "krpano"
    if normalized == "magicfit" or (allow_embedded and "magicfit" in normalized):
        return "magicfit"
    return ""


def _entry_candidates(export_dir: Path) -> Iterable[Path]:
    for name in ENTRY_NAMES:
        candidate = export_dir / name
        if candidate.is_file():
            yield candidate
    yield from sorted(export_dir.rglob("*.html"))
    yield from sorted(export_dir.rglob("*.htm"))


def _text_asset_has_markers(path: Path, markers: tuple[str, ...]) -> bool:
    if path.suffix.lower() not in TEXT_RUNTIME_SUFFIXES:
        return False
    try:
        if path.stat().st_size > MAX_MARKER_SCAN_BYTES:
            return False
        body = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(marker in body for marker in markers)


def _export_has_provider_markers(export_dir: Path, entry: Path, markers: tuple[str, ...]) -> bool:
    export_root = export_dir.resolve()
    candidates = [entry.resolve()]
    for candidate in sorted(export_root.rglob("*")):
        if len(candidates) >= MAX_MARKER_SCAN_FILES:
            break
        resolved = candidate.resolve()
        if candidate.is_file() and candidate.suffix.lower() in TEXT_RUNTIME_SUFFIXES and resolved not in candidates:
            candidates.append(resolved)
    for candidate in candidates:
        if export_root not in candidate.parents and candidate != export_root:
            continue
        if _text_asset_has_markers(candidate, markers):
            return True
    return False


def _export_has_forbidden_provider_markers(export_dir: Path, entry: Path, provider: str) -> bool:
    markers = FORBIDDEN_MARKERS_BY_PROVIDER.get(provider, ())
    if not markers:
        return False
    return _export_has_provider_markers(export_dir, entry, markers)


def _verified_entry(export_dir: Path, provider: str) -> tuple[Path | None, str]:
    markers = MARKERS_BY_PROVIDER[provider]
    seen: set[Path] = set()
    for entry in _entry_candidates(export_dir):
        resolved = entry.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if export_dir.resolve() not in resolved.parents:
            continue
        if _export_has_provider_markers(export_dir, resolved, markers):
            return resolved, resolved.relative_to(export_dir.resolve()).as_posix()
    return None, ""


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> Path:
    if not zip_path.is_file() or zip_path.suffix.lower() not in EXPORT_ARCHIVE_SUFFIXES:
        return target_dir
    try:
        return safe_extract_tour_zip(
            zip_path,
            target_dir,
            reason_prefix="property_tour_discovery_zip",
        )
    except TourHostSafetyError as exc:
        raise ValueError(str(exc)) from exc


def _verified_zip_entry(zip_path: Path, provider: str) -> tuple[str, str, str]:
    try:
        with tempfile.TemporaryDirectory(prefix=f"propertyquarry-{provider}-zip-") as tmp:
            export_dir = _safe_extract_zip(zip_path, Path(tmp))
            entry, entry_relpath = _verified_entry(export_dir, provider)
            if entry is None:
                return "", "", ""
            if _export_has_forbidden_provider_markers(export_dir, entry, provider):
                return "", "", f"{provider}_trial_branding_present"
            return str(zip_path), entry_relpath, ""
    except Exception:
        return "", "", ""


def _3dvista_provenance_rejection(
    *,
    export_dir: Path,
    slug: str,
    entry_relpath: str,
    export_zip: Path | None = None,
) -> tuple[str, Path | None, list[str]]:
    receipt_path = find_3dvista_provenance_receipt(export_dir)
    if receipt_path is None:
        return "3dvista_target_provenance_missing", None, ["receipt_missing"]
    receipt = load_json_object(receipt_path)
    if export_zip is None:
        _normalized, errors = validate_3dvista_target_provenance(
            receipt,
            target_slug=slug,
            export_dir=export_dir,
            entry_relpath=entry_relpath,
        )
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="propertyquarry-3dvista-provenance-") as tmp:
                extracted = _safe_extract_zip(export_zip, Path(tmp))
                _normalized, errors = validate_3dvista_target_provenance(
                    receipt,
                    target_slug=slug,
                    export_dir=extracted,
                    entry_relpath=entry_relpath,
                )
        except Exception:
            errors = ["local_export_unhashable"]
    if errors:
        return "3dvista_target_provenance_invalid", receipt_path, errors
    return "", receipt_path, []


def _discover_export_zip(export_dir: Path, provider: str) -> tuple[Path | None, str, str]:
    preferred = [
        export_dir / f"{provider}.zip",
        export_dir / "export.zip",
        export_dir / "tour.zip",
    ]
    candidates = [path for path in preferred if path.is_file()] + [
        path for path in sorted(export_dir.glob("*.zip")) if path not in preferred
    ]
    rejection_reason = ""
    for candidate in candidates:
        _, entry_relpath, rejected_reason = _verified_zip_entry(candidate, provider)
        if rejected_reason:
            rejection_reason = rejected_reason
            continue
        if entry_relpath:
            return candidate, entry_relpath, ""
    return None, "", rejection_reason


def _discover_panorama(asset_dir: Path) -> Path | None:
    def is_real_equirectangular(candidate: Path) -> bool:
        if not candidate.is_file() or candidate.suffix.lower() not in PANORAMA_EXTENSIONS:
            return False
        try:
            from PIL import Image

            with Image.open(candidate) as image:
                width, height = int(image.width), int(image.height)
        except Exception:
            return False
        if width < 1024 or height < 512:
            return False
        ratio = width / height if height else 0
        return EQUIRECTANGULAR_MIN_RATIO <= ratio <= EQUIRECTANGULAR_MAX_RATIO

    for name in ("panorama.jpg", "panorama.jpeg", "panorama.png", "panorama.webp", "equirect.jpg", "equirect.jpeg", "equirect.png", "equirect.webp"):
        candidate = asset_dir / name
        if is_real_equirectangular(candidate):
            return candidate
    matches = [
        path
        for path in sorted(asset_dir.iterdir())
        if is_real_equirectangular(path) and "panorama" in path.stem.lower()
    ]
    return matches[0] if matches else None


def _discover_cube_faces(asset_dir: Path) -> list[Path]:
    faces: list[Path] = []
    for index in range(1, 7):
        face = next(
            (
                asset_dir / f"cube-face-{index}{suffix}"
                for suffix in sorted(PANORAMA_EXTENSIONS)
                if (asset_dir / f"cube-face-{index}{suffix}").is_file()
            ),
            None,
        )
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
    for candidate in (asset_dir / "magicfit-receipt.json", asset_dir / "receipt.json"):
        if candidate.is_file():
            return candidate
    matches = sorted(asset_dir.glob("*.json"))
    return matches[0] if matches else None


def _load_bundle_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _public_bundle_provider_state(*, bundle_dir: Path, slug: str, provider: str) -> dict[str, str]:
    payload = _load_bundle_json(bundle_dir / "tour.json")
    private_payload = _load_bundle_json(bundle_dir / "tour.private.json")
    if provider == "3dvista":
        entry_relpath = str(payload.get("three_d_vista_entry_relpath") or private_payload.get("three_d_vista_entry_relpath") or "").strip()
        provider_url = str(private_payload.get("three_d_vista_url") or payload.get("three_d_vista_url") or "").strip()
        provenance = private_payload.get("three_d_vista_target_provenance") or payload.get("three_d_vista_target_provenance")
        if not isinstance(provenance, dict):
            return {}
        if entry_relpath:
            entry_parts = Path(entry_relpath).parts
            target_subdir = _safe_provenance_relpath(
                provenance.get("target_subdir") or (entry_parts[0] if len(entry_parts) > 1 else "")
            )
            if not target_subdir or not entry_relpath.startswith(f"{target_subdir}/"):
                return {}
            _normalized, errors = validate_3dvista_target_provenance(
                dict(provenance),
                target_slug=slug,
                export_dir=bundle_dir / target_subdir,
                entry_relpath=entry_relpath[len(target_subdir) + 1 :],
            )
            if errors:
                return {}
            return {
                "evidence": "public_bundle_3dvista_import",
                "entry_relpath": entry_relpath,
                "control_path": f"/tours/{slug}/control/3dvista",
            }
        if provider_url:
            _normalized, errors = validate_3dvista_target_provenance(
                dict(provenance),
                target_slug=slug,
                provider_url=provider_url,
            )
            if errors:
                return {}
            return {
                "evidence": "private_allowlisted_3dvista_url",
                "control_path": f"/tours/{slug}/control/3dvista",
            }
        return {}
    if provider == "pano2vr":
        entry_relpath = str(payload.get("pano2vr_entry_relpath") or private_payload.get("pano2vr_entry_relpath") or "").strip()
        if entry_relpath:
            return {
                "evidence": "public_bundle_pano2vr_import",
                "entry_relpath": entry_relpath,
                "control_path": f"/tours/{slug}/control/pano2vr",
            }
        return {}
    if provider == "krpano":
        imported = dict(payload.get("krpano_import") or {}) if isinstance(payload.get("krpano_import"), dict) else {}
        if imported:
            return {
                "evidence": "public_bundle_krpano_import",
                "control_path": f"/tours/{slug}/control/krpano",
            }
        return {}
    if provider == "magicfit":
        imported = dict(payload.get("magicfit_import") or {}) if isinstance(payload.get("magicfit_import"), dict) else {}
        if imported:
            target_relpath = str(imported.get("target_relpath") or "").strip()
            control_path = f"/tours/files/{slug}/{target_relpath}" if target_relpath else ""
            return {
                "evidence": "public_bundle_magicfit_import",
                "control_path": control_path,
            }
        return {}
    return {}


def _receipt_target_matches_slug(payload: dict[str, object], *, slug: str) -> bool:
    expected = str(slug or "").strip()
    if not expected:
        return False
    for key in ("target_slug", "tour_slug", "property_slug", "slug"):
        if str(payload.get(key) or "").strip() == expected:
            return True
    for key in ("property_url", "tour_url", "hosted_url", "public_url"):
        value = str(payload.get(key) or "").strip().rstrip("/")
        if value and value.rsplit("/", 1)[-1] == expected:
            return True
    return False


def _magicfit_receipt_rejection_reason(receipt: Path, *, video: Path, slug: str) -> str:
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except Exception:
        return "magicfit_receipt_invalid"
    if not isinstance(payload, dict):
        return "magicfit_receipt_invalid"
    if str(payload.get("provider") or "").strip().lower() != "magicfit":
        return "magicfit_receipt_provider_mismatch"
    output_file = str(payload.get("output_file") or "").strip()
    if output_file:
        try:
            if Path(output_file).expanduser().resolve() != video.resolve():
                return "magicfit_receipt_output_mismatch"
        except OSError:
            return "magicfit_receipt_output_invalid"
    if not _receipt_target_matches_slug(payload, slug=slug):
        return "magicfit_receipt_target_mismatch"
    return ""


def _relative_file_sample(export_dir: Path, *, limit: int = 8) -> list[str]:
    if not export_dir.is_dir():
        return []
    rows: list[str] = []
    for path in sorted(export_dir.rglob("*")):
        if len(rows) >= limit:
            break
        if not path.is_file() or path.name == "README.propertyquarry-export.txt":
            continue
        try:
            rows.append(path.relative_to(export_dir).as_posix())
        except ValueError:
            rows.append(path.name)
    return rows


def _file_count(export_dir: Path) -> int:
    if not export_dir.is_dir():
        return 0
    return sum(
        1
        for path in export_dir.rglob("*")
        if path.is_file() and path.name != "README.propertyquarry-export.txt"
    )


def _is_documentation_only_export_dir(export_dir: Path) -> bool:
    if not export_dir.is_dir():
        return False
    has_readme = any(
        path.is_file() and path.name == "README.propertyquarry-export.txt"
        for path in export_dir.rglob("*")
    )
    return has_readme and _file_count(export_dir) == 0


def _entry_candidate_sample(export_dir: Path, *, limit: int = 6) -> list[str]:
    if not export_dir.is_dir():
        return []
    rows: list[str] = []
    seen: set[str] = set()
    for entry in _entry_candidates(export_dir):
        if len(rows) >= limit:
            break
        try:
            relpath = entry.relative_to(export_dir).as_posix()
        except ValueError:
            relpath = entry.name
        if relpath not in seen:
            seen.add(relpath)
            rows.append(relpath)
    return rows


def _provider_drop_diagnostics(export_dir: Path, provider: str) -> dict[str, object]:
    if provider not in MARKERS_BY_PROVIDER:
        return {}
    marker_label = f"{provider}_runtime_marker"
    return {
        "file_count": _file_count(export_dir),
        "present_sample": _relative_file_sample(export_dir),
        "entry_candidates": _entry_candidate_sample(export_dir),
        "missing": [marker_label],
        "missing_markers": list(MARKERS_BY_PROVIDER[provider]),
    }


def _rejection_row(*, slug: str, provider: str, reason: str, export_dir: Path | None = None) -> dict[str, Any]:
    row = {
        "slug": slug,
        "provider": provider,
        "reason": reason,
        "action": REJECTION_ACTIONS.get(reason, "replace the dropped asset with a verified provider export and rerun discovery"),
        "drop_layout": PROVIDER_DROP_LAYOUTS.get(provider, "<drop>/<slug>/<provider>/"),
    }
    if export_dir is not None:
        row["drop_path"] = str(export_dir)
        row.update(_provider_drop_diagnostics(export_dir, provider))
    return row


def _resolved_existing_import_row(
    *,
    slug: str,
    provider: str,
    export_dir: Path,
    reason: str,
    live_state: dict[str, str],
) -> dict[str, Any]:
    row = _rejection_row(slug=slug, provider=provider, reason=reason, export_dir=export_dir)
    row["status"] = "already_imported_live_bundle"
    row["resolution"] = "provider already imported in the live tour bundle; archive or ignore stale drop assets unless you intend to replace the live import"
    row["live_evidence"] = str(live_state.get("evidence") or "").strip()
    row["live_control_path"] = str(live_state.get("control_path") or "").strip()
    if str(live_state.get("entry_relpath") or "").strip():
        row["live_entry_relpath"] = str(live_state.get("entry_relpath") or "").strip()
    return row


def _ignored_duplicate_drop_row(row: dict[str, Any]) -> dict[str, Any]:
    duplicate = dict(row)
    duplicate["status"] = "ignored_duplicate_drop"
    duplicate["resolution"] = "another drop folder for the same slug/provider is already importable in this discovery run"
    return duplicate


def _repair_manifest_rows(rejected: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in rejected:
        provider = str(row.get("provider") or "").strip().lower()
        slug = str(row.get("slug") or "").strip()
        reason = str(row.get("reason") or "").strip()
        drop_path = str(row.get("drop_path") or "").strip()
        if not provider or not slug or not reason:
            continue
        command = ""
        if provider == "3dvista" and drop_path:
            command = (
                f"python /app/scripts/import_3dvista_export.py --slug {slug} --export-dir {drop_path} "
                f"--provenance-receipt {drop_path}/3dvista-target-provenance.json"
            )
        elif provider == "pano2vr" and drop_path:
            command = f"python /app/scripts/import_pano2vr_export.py --slug {slug} --export-dir {drop_path}"
        elif provider == "krpano" and drop_path:
            command = f"python /app/scripts/import_krpano_walkable_scene.py --slug {slug} --panorama {drop_path}/panorama.jpg"
        elif provider == "magicfit" and drop_path:
            command = (
                "python /app/scripts/import_magicfit_walkthrough.py "
                f"--slug {slug} --video-path {drop_path}/magicfit-walkthrough.mp4 "
                f"--source-receipt {drop_path}/magicfit-receipt.json"
            )
        repair_row: dict[str, Any] = {
            "slug": slug,
            "provider": provider,
            "status": "waiting_for_verified_assets",
            "reason": reason,
            "drop_path": drop_path,
            "required_action": str(row.get("action") or "").strip(),
            "drop_layout": str(row.get("drop_layout") or "").strip(),
            "import_command_after_assets_arrive": command,
        }
        for key in (
            "file_count",
            "present_sample",
            "entry_candidates",
            "missing",
            "missing_markers",
            "provenance_errors",
            "provenance_receipt",
        ):
            if key in row:
                repair_row[key] = row[key]
        rows.append(repair_row)
    return rows


def _video_has_playable_stream(path: Path) -> bool:
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    try:
        header = path.read_bytes()[:64]
    except OSError:
        return False
    if path.suffix.lower() in {".mp4", ".m4v", ".mov"} and b"ftyp" not in header[:32]:
        return False
    if path.suffix.lower() == ".webm" and not header.startswith(b"\x1aE\xdf\xa3"):
        return False
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return True
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type,duration:format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return False
    if completed.returncode != 0:
        return False
    try:
        payload = json.loads(completed.stdout or "{}")
    except Exception:
        return False
    streams = [row for row in list(payload.get("streams") or []) if isinstance(row, dict)]
    if not any(str(row.get("codec_type") or "").strip().lower() == "video" for row in streams):
        return False
    durations: list[float] = []
    if isinstance(payload.get("format"), dict):
        with contextlib.suppress(Exception):
            durations.append(float(payload["format"].get("duration")))
    for row in streams:
        with contextlib.suppress(Exception):
            durations.append(float(row.get("duration")))
    return bool(durations and max(durations) > 0.0)


def _candidate_layouts(drop_dir: Path) -> list[tuple[str, str, Path]]:
    rows: list[tuple[str, str, Path]] = []
    if not drop_dir.is_dir():
        return rows
    for slug_dir in sorted(path for path in drop_dir.iterdir() if path.is_dir()):
        if _provider_from_text(slug_dir.name):
            continue
        slug = _safe_slug(slug_dir.name)
        if not slug or _is_operator_drop_lane_slug(slug):
            continue
        for provider in PROVIDERS:
            provider_dir = slug_dir / provider
            if provider_dir.is_dir() and not _is_documentation_only_export_dir(provider_dir):
                rows.append((slug, provider, provider_dir.resolve()))
        for export_dir in sorted(path for path in slug_dir.iterdir() if path.is_dir()):
            provider = _provider_from_text(export_dir.name, allow_embedded=True)
            if provider and not _is_documentation_only_export_dir(export_dir):
                rows.append((slug, provider, export_dir.resolve()))
    for provider_dir in sorted(path for path in drop_dir.iterdir() if path.is_dir()):
        provider = _provider_from_text(provider_dir.name)
        if not provider:
            continue
        for slug_dir in sorted(path for path in provider_dir.iterdir() if path.is_dir()):
            slug = _safe_slug(slug_dir.name)
            if slug and not _is_operator_drop_lane_slug(slug) and not _is_documentation_only_export_dir(slug_dir):
                rows.append((slug, provider, slug_dir.resolve()))
    deduped: dict[tuple[str, str, str], tuple[str, str, Path]] = {}
    for slug, provider, export_dir in rows:
        deduped[(slug, provider, str(export_dir))] = (slug, provider, export_dir)
    return list(deduped.values())


def _evaluate_candidate_layout(*, slug: str, provider: str, export_dir: Path, manifest_path: Path) -> tuple[str, dict[str, Any]]:
    if not manifest_path.is_file():
        return "rejected", _rejection_row(slug=slug, provider=provider, reason="tour_manifest_missing", export_dir=export_dir)
    if provider in {"3dvista", "pano2vr"}:
        entry, entry_relpath = _verified_entry(export_dir, provider)
        export_zip: Path | None = None
        zip_rejection_reason = ""
        if entry is None:
            export_zip, entry_relpath, zip_rejection_reason = _discover_export_zip(export_dir, provider)
        if entry is None and export_zip is None:
            return (
                "rejected",
                _rejection_row(
                    slug=slug,
                    provider=provider,
                    reason=zip_rejection_reason or f"{provider}_export_entry_unverified",
                    export_dir=export_dir,
                ),
            )
        if entry is not None and _export_has_forbidden_provider_markers(export_dir, entry, provider):
            return "rejected", _rejection_row(slug=slug, provider=provider, reason=f"{provider}_trial_branding_present", export_dir=export_dir)
        provenance_path: Path | None = None
        if provider == "3dvista":
            provenance_reason, provenance_path, provenance_errors = _3dvista_provenance_rejection(
                export_dir=export_dir,
                slug=slug,
                entry_relpath=entry_relpath,
                export_zip=export_zip,
            )
            if provenance_reason:
                rejected = _rejection_row(
                    slug=slug,
                    provider=provider,
                    reason=provenance_reason,
                    export_dir=export_dir,
                )
                rejected["provenance_errors"] = provenance_errors
                if provenance_path is not None:
                    rejected["provenance_receipt"] = str(provenance_path)
                return "rejected", rejected
        row = {
            "slug": slug,
            "provider": provider,
            "export_dir": str(export_dir),
            "entry": entry_relpath,
        }
        if export_zip is not None:
            row["export_zip"] = str(export_zip)
        if provenance_path is not None:
            row["provenance_receipt"] = str(provenance_path)
        return "import", row
    if provider == "krpano":
        panorama = _discover_panorama(export_dir)
        cube_faces = _discover_cube_faces(export_dir)
        if panorama is None and len(cube_faces) != 6:
            return "rejected", _rejection_row(slug=slug, provider=provider, reason="krpano_assets_missing", export_dir=export_dir)
        row = {
            "slug": slug,
            "provider": provider,
            "asset_dir": str(export_dir),
        }
        if panorama is not None:
            row["panorama"] = str(panorama)
        elif len(cube_faces) == 6:
            for index, face in enumerate(cube_faces, start=1):
                row[f"cube_face_{index}"] = str(face)
        return "import", row
    if provider == "magicfit":
        video = _discover_video(export_dir)
        receipt = _discover_receipt(export_dir)
        if video is None:
            return "rejected", _rejection_row(slug=slug, provider=provider, reason="magicfit_video_missing", export_dir=export_dir)
        if not _video_has_playable_stream(video):
            return "rejected", _rejection_row(slug=slug, provider=provider, reason="magicfit_video_unverified", export_dir=export_dir)
        if receipt is None:
            return "rejected", _rejection_row(slug=slug, provider=provider, reason="magicfit_receipt_missing", export_dir=export_dir)
        receipt_rejection_reason = _magicfit_receipt_rejection_reason(receipt, video=video, slug=slug)
        if receipt_rejection_reason:
            return "rejected", _rejection_row(slug=slug, provider=provider, reason=receipt_rejection_reason, export_dir=export_dir)
        return (
            "import",
            {
                "slug": slug,
                "provider": provider,
                "asset_dir": str(export_dir),
                "video": str(video),
                "receipt": str(receipt),
            },
        )
    return "rejected", _rejection_row(slug=slug, provider=provider, reason="unsupported_provider", export_dir=export_dir)


def build_discovery_receipt(*, drop_dir: Path, public_tour_dir: Path | None = None) -> dict[str, Any]:
    public_root = (public_tour_dir or Path(os.getenv("EA_PUBLIC_TOUR_DIR") or "/data/public_property_tours")).expanduser()
    imports: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    resolved_existing_imports: list[dict[str, Any]] = []
    ignored_duplicate_drop_rows: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[Path]] = {}
    for slug, provider, export_dir in _candidate_layouts(drop_dir.expanduser()):
        grouped.setdefault((slug, provider), []).append(export_dir)
    for (slug, provider), export_dirs in grouped.items():
        manifest_path = public_root / slug / "tour.json"
        bundle_dir = manifest_path.parent
        group_imports: list[dict[str, Any]] = []
        group_rejected: list[dict[str, Any]] = []
        for export_dir in export_dirs:
            kind, row = _evaluate_candidate_layout(slug=slug, provider=provider, export_dir=export_dir, manifest_path=manifest_path)
            if kind == "import":
                group_imports.append(row)
            else:
                group_rejected.append(row)
        if group_imports:
            imports.extend(group_imports)
            ignored_duplicate_drop_rows.extend(_ignored_duplicate_drop_row(row) for row in group_rejected)
            continue
        live_state = _public_bundle_provider_state(bundle_dir=bundle_dir, slug=slug, provider=provider)
        if live_state:
            resolved_existing_imports.extend(
                _resolved_existing_import_row(
                    slug=slug,
                    provider=provider,
                    export_dir=Path(str(row.get("drop_path") or "")),
                    reason=str(row.get("reason") or "").strip(),
                    live_state=live_state,
                )
                for row in group_rejected
            )
            continue
        rejected.extend(group_rejected)
    status = "ready" if imports or resolved_existing_imports else "blocked_no_verified_exports"
    repair_manifest = _repair_manifest_rows(rejected)
    return {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "drop_dir": str(drop_dir.expanduser()),
        "public_tour_dir": str(public_root),
        "import_count": len(imports),
        "rejected_count": len(rejected),
        "resolved_existing_import_count": len(resolved_existing_imports),
        "ignored_duplicate_drop_count": len(ignored_duplicate_drop_rows),
        "imports": imports,
        "rejected": rejected,
        "resolved_existing_imports": resolved_existing_imports,
        "ignored_duplicate_drop_rows": ignored_duplicate_drop_rows,
        "repair_count": len(repair_manifest),
        "repair_manifest": repair_manifest,
        "import_manifest": {"imports": imports},
        "notes": [
            "This discovery step does not publish tours. It only emits rows accepted by the hardened import_property_tour_exports.py importer.",
            "Documentation-only provider folders that contain only README.propertyquarry-export.txt are intentionally ignored here so they do not appear as broken exports.",
            "3DVista and Pano2VR placeholders are rejected unless the entry or bundled local runtime files contain provider markers.",
            "3DVista rows additionally require a private target-bound provenance receipt for the exact slug and export bytes; provider samples and unapproved reuse fail closed.",
            "krpano rows require a real panorama/cubemap candidate; MagicFit rows require a playable video stream and receipt candidate before import.",
            "Rejected duplicate drop folders are ignored when another folder for the same slug/provider is already importable in this run.",
            "Rejected rows are resolved instead of repaired when the provider is already imported in the live tour bundle for the same slug.",
            "Rejected rows are also emitted in repair_manifest so status/UI repair can show exact missing assets without treating placeholders as verified tours.",
        ],
    }


def _handoff_markdown_path(write_path: Path) -> Path:
    name = write_path.name
    if name.endswith(".json"):
        return write_path.with_name(f"{name[:-5]}.handoff.md")
    return write_path.with_suffix(f"{write_path.suffix}.handoff.md" if write_path.suffix else ".handoff.md")


def _tour_export_handoff_markdown(receipt: dict[str, Any]) -> str:
    lines = [
        "# PropertyQuarry Tour Export Handoff",
        "",
        "Gold remains blocked until real provider assets are copied into the drop folders and pass the hardened importers.",
        "",
        f"- Discovery status: `{receipt.get('status')}`",
        f"- Drop root: `{receipt.get('drop_dir')}`",
        f"- Public tour root: `{receipt.get('public_tour_dir')}`",
        f"- Importable rows now: `{receipt.get('import_count')}`",
        f"- Rejected rows now: `{receipt.get('rejected_count')}`",
        "",
        "Do not copy placeholder HTML, flat photos, or fake videos. The importer must see real provider runtime markers or playable media.",
        "",
    ]
    rejected = [row for row in list(receipt.get("rejected") or []) if isinstance(row, dict)]
    if rejected:
        lines.extend(["## Rejected Drops", ""])
    for row in rejected:
        provider = str(row.get("provider") or "").strip()
        slug = str(row.get("slug") or "").strip()
        drop_path = str(row.get("drop_path") or "").strip()
        file_count = row.get("file_count")
        present_sample = ", ".join(str(item) for item in list(row.get("present_sample") or [])) or "none"
        entry_candidates = ", ".join(str(item) for item in list(row.get("entry_candidates") or [])) or "none"
        missing = ", ".join(str(item) for item in list(row.get("missing") or [])) or str(row.get("reason") or "")
        markers = ", ".join(str(item) for item in list(row.get("missing_markers") or [])) or "provider-specific runtime/media evidence"
        lines.extend(
            [
                f"### {provider} · {slug}",
                "",
                f"- Drop path: `{drop_path}`",
                f"- Reason: `{row.get('reason')}`",
                f"- Files found: `{file_count if file_count is not None else 'n/a'}`",
                f"- Present sample: `{present_sample}`",
                f"- Entry candidates: `{entry_candidates}`",
                f"- Missing: `{missing}`",
                f"- Required markers/evidence: `{markers}`",
                f"- Required action: {row.get('action')}",
                f"- Expected layout: `{row.get('drop_layout')}`",
                "",
            ]
        )
    repair_rows = [row for row in list(receipt.get("repair_manifest") or []) if isinstance(row, dict)]
    if repair_rows:
        lines.extend(["## Commands After Assets Arrive", ""])
        for row in repair_rows:
            command = str(row.get("import_command_after_assets_arrive") or "").strip()
            if command:
                lines.append(f"- `{command}`")
        lines.append("")
    lines.extend(
        [
            "After importing, rerun:",
            "",
            "`python /app/scripts/verify_property_tour_controls.py --tour-root /data/public_property_tours --require-all-provider-modes --summary-only`",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover verified 3DVista/Pano2VR export folders and emit an import manifest.")
    parser.add_argument("--drop-dir", default=str(_default_drop_dir()))
    parser.add_argument("--public-tour-dir", default="")
    parser.add_argument("--write", default="")
    parser.add_argument("--manifest-write", default="")
    parser.add_argument("--fail-on-blocked", action="store_true")
    args = parser.parse_args()
    receipt = build_discovery_receipt(
        drop_dir=Path(args.drop_dir),
        public_tour_dir=Path(args.public_tour_dir) if str(args.public_tour_dir or "").strip() else None,
    )
    write_path = Path(args.write).expanduser() if str(args.write or "").strip() else _artifact_dir() / "property-tour-export-discovery.json"
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _handoff_markdown_path(write_path).write_text(_tour_export_handoff_markdown(receipt), encoding="utf-8")
    if str(args.manifest_write or "").strip():
        manifest_path = Path(args.manifest_write).expanduser()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(receipt["import_manifest"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("status", "import_count", "rejected_count", "imports", "rejected")}, indent=2, sort_keys=True))
    if receipt["status"] == "ready":
        return 0
    return 2 if args.fail_on_blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
