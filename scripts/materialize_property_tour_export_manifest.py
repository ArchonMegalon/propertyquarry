#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_property_tour_exports import (  # noqa: E402
    _discover_cube_faces,
    _discover_panorama,
    _discover_receipt,
    _discover_video,
    _magicfit_receipt_rejection_reason,
    _verified_entry,
    _video_has_playable_stream,
)
from property_tour_runtime_paths import preferred_public_tour_root  # noqa: E402
from verify_property_tour_controls import build_property_tour_control_receipt
from verify_property_tour_controls import _load_cli_env_defaults as _load_tour_control_env_defaults


IMPORTABLE_PROVIDERS = ("3dvista", "pano2vr", "krpano", "magicfit")
PROVIDER_ENTRY_MARKERS = {
    "3dvista": "index.html/index.htm containing tdvplayer, tdvplayerapi, or tourviewer runtime markers",
    "pano2vr": "index.html/index.htm containing ggpkg, ggskin, pano.xml, or tour.js runtime markers",
    "krpano": "one real 2:1 captured/exported equirectangular panorama named panorama.jpg/png/webp, or six real captured/exported square cubemap face images named cube-face-1..6",
    "magicfit": "a playable MagicFit MP4/MOV/WebM plus the matching MagicFit render receipt JSON",
}
PROVIDER_DROP_CHECKLISTS = {
    "3dvista": (
        "Copy the complete 3DVista export folder into this directory, or drop one verified 3DVista .zip export here.",
        "Accepted entry files: index.html, index.htm, tour.html, virtualtour.html, or output/index.html.",
        "The entry HTML must contain a 3DVista runtime marker: tdvplayer, tdvplayerapi, or tourviewer.",
        "Keep sibling JS/CSS/media folders next to the entry file; the importer copies the whole export tree.",
        "If using a zip, keep the export tree intact inside the archive; unsafe paths and placeholder entries are rejected.",
    ),
    "pano2vr": (
        "Copy the complete Pano2VR output folder into this directory, or drop one verified Pano2VR .zip export here.",
        "Accepted entry files: index.html, index.htm, tour.html, virtualtour.html, or output/index.html.",
        "The entry HTML must contain a Pano2VR runtime marker: ggpkg, ggskin, pano.xml, or tour.js.",
        "Keep generated tiles, skin files, XML, JS, and media folders next to the entry file.",
        "If using a zip, keep the export tree intact inside the archive; unsafe paths and placeholder entries are rejected.",
    ),
    "krpano": (
        "Copy exactly one real captured/exported 2:1 panorama named panorama.jpg, panorama.jpeg, panorama.png, or panorama.webp, or provide a real captured/exported six-face cubemap.",
        "Cubemap filenames must be cube-face-1.jpg/png/webp through cube-face-6.jpg/png/webp.",
        "Panoramas must be at least 1024x512 and close to a 2:1 ratio; cube faces must be square and at least 512x512.",
        "Do not use generated square patches, listing-photo crops, or fallback cube placeholders as tour evidence.",
        "Set KRPANO_LICENSE_DOMAIN=propertyquarry.com and KRPANO_LICENSE_KEY before importing.",
    ),
    "magicfit": (
        "Copy a playable MagicFit walkthrough video named magicfit-walkthrough.mp4/mov/webm or walkthrough.mp4/mov/webm.",
        "Copy the matching MagicFit receipt as magicfit-receipt.json or receipt.json.",
        "The video must have a real video stream and positive duration; signature-only placeholder files are rejected.",
        "The receipt should identify provider=magicfit and the target slug/output file used to produce the video.",
    ),
}


def _tour_root() -> Path:
    return preferred_public_tour_root(
        configured_root=os.getenv("EA_PUBLIC_TOUR_DIR") or "",
        repo_root=Path.cwd() if (Path.cwd() / "docker-compose.property.yml").is_file() else Path(__file__).resolve().parents[1],
        fallback_root="/docker/property/state/public_property_tours",
        runtime_container=os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "",
    )


def _artifact_dir() -> Path:
    configured = str(os.getenv("EA_ARTIFACT_DIR") or os.getenv("EA_ARTIFACTS_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    repo_root = Path.cwd() if (Path.cwd() / "docker-compose.property.yml").is_file() else Path(__file__).resolve().parents[1]
    repo_local = (repo_root / "_completion" / "property_tour_exports").resolve()
    data_artifacts = Path("/data/artifacts").expanduser()
    if data_artifacts.is_dir():
        return data_artifacts.resolve()
    return repo_local


def _incoming_root() -> Path:
    configured = str(os.getenv("PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    repo_state = Path.cwd() / "state" / "incoming_property_tours"
    if Path.cwd().name == "property" and (Path.cwd() / "docker-compose.property.yml").is_file():
        return repo_state.resolve()
    return Path("/data/incoming_property_tours").expanduser().resolve()


def _provider_target_subdir(provider: str) -> str:
    if provider == "3dvista":
        return "3dvista"
    if provider == "pano2vr":
        return "pano2vr"
    if provider == "krpano":
        return "krpano"
    if provider == "magicfit":
        return "magicfit"
    return provider


def _default_missing_action(provider: str) -> str:
    if provider == "3dvista":
        return "run import_3dvista_export.py with a verified 3DVista export or add an allowlisted 3dvista.com URL"
    if provider == "pano2vr":
        return "run import_pano2vr_export.py with a verified Pano2VR export"
    if provider == "krpano":
        return "run import_krpano_walkable_scene.py with a real captured/exported 2:1 panorama or real captured/exported cubemap faces and krpano license env"
    if provider == "magicfit":
        return "run import_magicfit_walkthrough.py with a playable MagicFit video and matching render receipt"
    return ""


def _default_missing_reason(provider: str) -> str:
    if provider == "3dvista":
        return "missing_3dvista_export"
    if provider == "pano2vr":
        return "missing_pano2vr_export"
    if provider == "krpano":
        return "missing_krpano_walkable_scene"
    if provider == "magicfit":
        return "missing_magicfit_walkthrough"
    return ""


def _provider_import_example(row: dict[str, str]) -> str:
    provider = str(row.get("provider") or "").strip().lower()
    slug = str(row.get("slug") or "").strip()
    export_dir = str(row.get("export_dir") or row.get("asset_dir") or "").strip()
    if provider in {"3dvista", "pano2vr"}:
        return f"python /app/scripts/import_{provider}_export.py --slug {slug} --export-dir {export_dir}  # or --export-zip {export_dir}/export.zip"
    if provider == "krpano":
        return f"python /app/scripts/import_krpano_walkable_scene.py --slug {slug} --panorama {export_dir}/panorama.jpg"
    if provider == "magicfit":
        return f"python /app/scripts/import_magicfit_walkthrough.py --slug {slug} --video-path {export_dir}/magicfit-walkthrough.mp4 --source-receipt {export_dir}/magicfit-receipt.json"
    return ""


def _drop_readme_body(*, row: dict[str, Any], provider: str, slug: str, drop_status: dict[str, Any], manifest: dict[str, Any]) -> str:
    return "\n".join(
        [
            "PropertyQuarry provider export drop folder",
            "",
            f"Slug: {slug}",
            f"Title: {str(row.get('title') or slug).strip()}",
            f"Provider: {provider}",
            f"Current verified controls: {str(row.get('current_control_providers') or 'none').strip()}",
            f"Expected entry: {PROVIDER_ENTRY_MARKERS[provider]}",
            f"Current drop status: {drop_status.get('status')}",
            f"Missing now: {', '.join(list(drop_status.get('missing') or [])) or 'nothing'}",
            "",
            "Checklist:",
            *[f"- {item}" for item in PROVIDER_DROP_CHECKLISTS[provider]],
            "",
            "Copy the real provider export or asset contents into this directory.",
            "Do not copy placeholder HTML, flat listing photos, generated cube fallbacks, or fake videos; the importers reject unverified entries.",
            "",
            f"Single-provider dry import example: {_provider_import_example({**row, 'provider': provider, 'slug': slug})}",
            "",
            f"After exports are copied, run: {manifest.get('next_command')}",
            "Then rerun: python /app/scripts/verify_property_tour_controls.py --tour-root /data/public_property_tours --require-all-provider-modes --summary-only",
            "Core Gold requires one provider-authentic customer tour: either a fresh topology-verified Matterport capture or a licensed, verified multi-node 3DVista export. A 3DVista walkable claim must contain multiple spatial panorama nodes and PropertyQuarry-specific license/provenance evidence. MagicFit is prepared here for the separate Advanced Visual Gold lane; Magic and OMagic remain governed scene-video providers outside this import folder.",
            "Advanced Visual Gold must stay unavailable until every claimed provider has exact receipt, playback, quota, privacy, and isolation evidence.",
            "Pano2VR is an optional/internal export lane and must stay hidden unless a clean verified export is deliberately enabled.",
            "",
        ]
    )


def _fallback_readme_paths(*, slug: str, provider: str) -> list[Path]:
    relative = Path("property-tour-export-drop-readmes") / slug / provider / "README.propertyquarry-export.txt"
    repo_local = Path.cwd() / "_completion" / "property_tour_exports" / "drop-readmes" / slug / provider / "README.propertyquarry-export.txt"
    paths = [_artifact_dir() / relative, repo_local]
    deduped: list[Path] = []
    for path in paths:
        resolved = path.expanduser()
        if resolved not in deduped:
            deduped.append(resolved)
    return deduped


def _write_first_available_fallback_readme(*, slug: str, provider: str, body: str) -> tuple[Path, str]:
    errors: list[str] = []
    for path in _fallback_readme_paths(slug=slug, provider=provider):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            return path, ""
        except OSError as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
    return _fallback_readme_paths(slug=slug, provider=provider)[0], "; ".join(errors)


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


def _drop_preflight(row: dict[str, str]) -> dict[str, Any]:
    provider = str(row.get("provider") or "").strip().lower()
    slug = str(row.get("slug") or "").strip()
    export_dir = Path(str(row.get("export_dir") or row.get("asset_dir") or "")).expanduser().resolve()
    file_sample = _relative_file_sample(export_dir)
    file_count = 0
    if export_dir.is_dir():
        file_count = sum(
            1
            for path in export_dir.rglob("*")
            if path.is_file() and path.name != "README.propertyquarry-export.txt"
        )
    status: dict[str, Any] = {
        "slug": slug,
        "provider": provider,
        "export_dir": str(export_dir),
        "status": "waiting_for_assets",
        "file_count": file_count,
        "present_sample": file_sample,
        "missing": [],
        "accepted_entry": "",
    }
    if not export_dir.is_dir():
        status["missing"] = ["drop_folder"]
        return status
    if provider in {"3dvista", "pano2vr"}:
        entry, entry_relpath = _verified_entry(export_dir, provider)
        if entry is None:
            status["missing"] = [f"{provider}_verified_runtime_entry"]
            return status
        status["status"] = "ready_for_import"
        status["accepted_entry"] = entry_relpath
        return status
    if provider == "krpano":
        panorama = _discover_panorama(export_dir)
        cube_faces = _discover_cube_faces(export_dir)
        if panorama is None and len(cube_faces) != 6:
            status["missing"] = ["krpano_real_panorama_or_real_cubemap_faces"]
            return status
        status["status"] = "ready_for_import"
        status["accepted_entry"] = panorama.name if panorama is not None else "real-cubemap-face-1..6"
        return status
    if provider == "magicfit":
        video = _discover_video(export_dir)
        receipt = _discover_receipt(export_dir)
        if video is None:
            status["missing"] = ["magicfit_walkthrough_video"]
            return status
        if not _video_has_playable_stream(video):
            status["missing"] = ["magicfit_playable_video_stream"]
            status["accepted_entry"] = video.name
            return status
        if receipt is None:
            status["missing"] = ["magicfit_render_receipt"]
            status["accepted_entry"] = video.name
            return status
        receipt_rejection = _magicfit_receipt_rejection_reason(receipt, video=video, slug=slug)
        if receipt_rejection:
            status["missing"] = [receipt_rejection]
            status["accepted_entry"] = video.name
            return status
        status["status"] = "ready_for_import"
        status["accepted_entry"] = video.name
        status["receipt"] = receipt.name
        return status
    status["missing"] = ["supported_provider"]
    return status


def build_drop_status_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in list(manifest.get("imports") or []):
        if isinstance(row, dict):
            rows.append(_drop_preflight({str(key): str(value) for key, value in row.items()}))
    return rows


def _drop_status_summary(drop_status: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "ready_for_import": 0,
        "waiting_for_assets": 0,
        "other": 0,
    }
    for row in drop_status:
        status = str(row.get("status") or "").strip().lower()
        if status == "ready_for_import":
            summary["ready_for_import"] += 1
        elif status == "waiting_for_assets":
            summary["waiting_for_assets"] += 1
        else:
            summary["other"] += 1
    return summary


def _manifest_status(*, imports: list[dict[str, str]], selected_providers: set[str], drop_status: list[dict[str, Any]]) -> str:
    if not selected_providers:
        return "pass"
    if not imports:
        return "blocked_no_import_targets"
    summary = _drop_status_summary(drop_status)
    if summary["ready_for_import"] == 0:
        return "waiting_for_verified_assets"
    if summary["ready_for_import"] < len(imports):
        return "partial_ready_for_import"
    return "ready_for_import"


def _tour_sort_key(tour: dict[str, Any]) -> tuple[int, int, str]:
    controls = [row for row in list(tour.get("controls") or []) if isinstance(row, dict)]
    is_ready = str(tour.get("status") or "").strip().lower() == "ready"
    return (0 if is_ready else 1, -len(controls), str(tour.get("slug") or ""))


def _missing_import_targets(receipt: dict[str, Any], *, providers: set[str], incoming_root: Path, limit_per_provider: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    counts = {provider: 0 for provider in providers}
    tours = [tour for tour in list(receipt.get("tours") or []) if isinstance(tour, dict)]
    for tour in sorted(tours, key=_tour_sort_key):
        if not isinstance(tour, dict):
            continue
        slug = str(tour.get("slug") or "").strip()
        if not slug:
            continue
        missing_rows = [row for row in list(tour.get("missing_evidence") or []) if isinstance(row, dict)]
        for provider in list(tour.get("supplemental_missing_provider_modes") or []):
            if provider not in {str(row.get("provider") or "").strip().lower() for row in missing_rows}:
                missing_rows.append(
                    {
                        "provider": provider,
                        "reason": _default_missing_reason(provider),
                        "action": _default_missing_action(provider),
                    }
                )
        if not missing_rows:
            missing_rows = [
                {
                    "provider": provider,
                    "reason": _default_missing_reason(provider),
                    "action": _default_missing_action(provider),
                }
                for provider in list(tour.get("missing_provider_modes") or [])
            ]
        current_control_providers = sorted(
            {
                str(control.get("provider") or "").strip().lower()
                for control in list(tour.get("controls") or [])
                if isinstance(control, dict) and str(control.get("provider") or "").strip()
            }
        )
        for missing in missing_rows:
            provider = str(missing.get("provider") or "").strip().lower()
            if provider not in providers or provider not in IMPORTABLE_PROVIDERS:
                continue
            if counts.get(provider, 0) >= limit_per_provider:
                continue
            export_dir = incoming_root / slug / _provider_target_subdir(provider)
            rows.append(
                {
                    "slug": slug,
                    "title": str(tour.get("title") or slug).strip(),
                    "provider": provider,
                    "export_dir": str(export_dir),
                    "asset_dir": str(export_dir),
                    "entry": "",
                    "target_subdir": _provider_target_subdir(provider),
                    "reason": str(missing.get("reason") or ""),
                    "action": str(missing.get("action") or ""),
                    "current_control_providers": ",".join(current_control_providers),
                }
            )
            counts[provider] = counts.get(provider, 0) + 1
    return rows


def build_export_manifest(
    *,
    tour_root: Path | None = None,
    incoming_root: Path | None = None,
    providers: set[str] | None = None,
    limit_per_provider: int = 1,
) -> dict[str, Any]:
    _load_tour_control_env_defaults()
    resolved_tour_root = (tour_root or _tour_root()).expanduser().resolve()
    resolved_incoming_root = (incoming_root or _incoming_root()).expanduser().resolve()
    receipt = build_property_tour_control_receipt(
        tour_root=resolved_tour_root,
        require_all_provider_modes=True,
    )
    requested_providers = providers or set(IMPORTABLE_PROVIDERS)
    requested_providers = {provider for provider in requested_providers if provider in IMPORTABLE_PROVIDERS}
    missing_modes = set(str(provider) for provider in list(receipt.get("missing_provider_modes") or []))
    missing_evidence_providers: set[str] = set()
    supplemental_missing_by_tour: dict[str, set[str]] = {}
    supplemental_missing_providers: set[str] = set()
    for tour in list(receipt.get("tours") or []):
        if not isinstance(tour, dict):
            continue
        slug = str(tour.get("slug") or "").strip()
        if not slug:
            continue
        for row in list(tour.get("missing_evidence") or []):
            if not isinstance(row, dict):
                continue
            provider = str(row.get("provider") or "").strip().lower()
            if provider in IMPORTABLE_PROVIDERS:
                missing_evidence_providers.add(provider)
        controls = [row for row in list(tour.get("controls") or []) if isinstance(row, dict)]
        ready_control_providers = {
            str(control.get("provider") or "").strip().lower()
            for control in controls
            if str(control.get("status") or "").strip().lower() == "ready"
        }
        for provider in requested_providers:
            if provider in ready_control_providers:
                continue
            supplemental_missing_by_tour.setdefault(slug, set()).add(provider)
            supplemental_missing_providers.add(provider)
    for tour in list(receipt.get("tours") or []):
        if not isinstance(tour, dict):
            continue
        slug = str(tour.get("slug") or "").strip()
        if slug and supplemental_missing_by_tour.get(slug):
            tour["supplemental_missing_provider_modes"] = sorted(supplemental_missing_by_tour[slug])
    selected_providers = {
        provider
        for provider in requested_providers
        if provider in missing_modes or provider in missing_evidence_providers or provider in supplemental_missing_providers
    }
    imports = _missing_import_targets(
        receipt,
        providers=selected_providers,
        incoming_root=resolved_incoming_root,
        limit_per_provider=max(1, int(limit_per_provider or 1)),
    )
    drop_status = build_drop_status_rows({"imports": imports})
    drop_status_summary = _drop_status_summary(drop_status)
    status = _manifest_status(imports=imports, selected_providers=selected_providers, drop_status=drop_status)
    return {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "tour_root": str(resolved_tour_root),
        "incoming_root": str(resolved_incoming_root),
        "core_required_provider_modes": list(receipt.get("core_required_provider_modes") or []),
        "advanced_visual_required_provider_modes": list(
            receipt.get("advanced_visual_required_provider_modes") or []
        ),
        "core_missing_provider_modes": list(receipt.get("core_missing_provider_modes") or []),
        "advanced_visual_missing_provider_modes": list(
            receipt.get("advanced_visual_missing_provider_modes") or []
        ),
        "operator_missing_provider_modes": list(
            receipt.get("operator_missing_provider_modes") or missing_modes
        ),
        "missing_provider_modes": sorted(missing_modes),
        "providers": sorted(selected_providers),
        "import_count": len(imports),
        "imports": imports,
        "drop_status": drop_status,
        "drop_status_summary": drop_status_summary,
        "next_command": "python /app/scripts/import_property_tour_exports.py --manifest /data/artifacts/property-tour-export-import-manifest.json --write /data/artifacts/property-tour-export-import-receipt.json",
        "notes": [
            "Copy each real provider export or asset into its export_dir before running the import command.",
            "Do not place placeholder HTML, flat photos, generated cube fallbacks, or fake videos in these directories; the hardened importers reject unverified entries.",
            "After import, run verify_property_tour_controls.py with --require-all-provider-modes for the combined operator envelope; Core Gold and Advanced Visual Gold status remain separately reported.",
        ],
    }


def prepare_export_drop_dirs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = [
        dict(row)
        for row in list(manifest.get("imports") or [])
        if isinstance(row, dict)
    ]
    prepared_providers = {
        str(row.get("provider") or "").strip().lower()
        for row in rows
        if str(row.get("provider") or "").strip().lower() in IMPORTABLE_PROVIDERS
    }
    incoming_root = Path(str(manifest.get("incoming_root") or _incoming_root())).expanduser().resolve()
    for provider in IMPORTABLE_PROVIDERS:
        if provider in prepared_providers:
            continue
        export_dir = incoming_root / "_operator-import-lane" / _provider_target_subdir(provider)
        rows.append(
            {
                "slug": "_operator-import-lane",
                "title": "Operator import lane",
                "provider": provider,
                "export_dir": str(export_dir),
                "asset_dir": str(export_dir),
                "entry": "",
                "target_subdir": _provider_target_subdir(provider),
                "reason": "provider_import_lane_ready",
                "action": _default_missing_action(provider),
                "current_control_providers": "",
            }
        )
    for row in rows:
        if not isinstance(row, dict):
            continue
        export_dir = Path(str(row.get("export_dir") or "")).expanduser().resolve()
        provider = str(row.get("provider") or "").strip().lower()
        slug = str(row.get("slug") or "").strip()
        if provider not in IMPORTABLE_PROVIDERS or not slug:
            continue
        drop_status = _drop_preflight({str(key): str(value) for key, value in row.items()})
        readme_path = export_dir / "README.propertyquarry-export.txt"
        artifact_readme_path = _fallback_readme_paths(slug=slug, provider=provider)[0]
        readme_write_error = ""
        artifact_readme_write_error = ""
        active_readme_path = readme_path
        readme_body = _drop_readme_body(row=row, provider=provider, slug=slug, drop_status=drop_status, manifest=manifest)
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
            readme_path.write_text(readme_body, encoding="utf-8")
        except OSError as exc:
            readme_write_error = f"{type(exc).__name__}: {exc}"
            artifact_readme_path, artifact_readme_write_error = _write_first_available_fallback_readme(
                slug=slug,
                provider=provider,
                body=readme_body,
            )
            active_readme_path = artifact_readme_path
        prepared.append(
            {
                **row,
                "readme": str(active_readme_path),
                "artifact_readme": str(artifact_readme_path if readme_write_error else active_readme_path),
                "readme_write_error": readme_write_error,
                "artifact_readme_write_error": artifact_readme_write_error,
                "drop_status": drop_status,
            }
        )
    return prepared


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize PropertyQuarry provider export import targets.")
    parser.add_argument("--tour-root", type=Path, help="Public tour bundle root.")
    parser.add_argument("--incoming-root", type=Path, help="Root where provider exports will be dropped.")
    parser.add_argument(
        "--provider",
        action="append",
        choices=IMPORTABLE_PROVIDERS,
        dest="providers",
        help="Limit the manifest to one or more importable providers.",
    )
    parser.add_argument("--limit-per-provider", type=int, default=1)
    parser.add_argument("--prepare-dirs", action="store_true", help="Create drop folders and operator README files.")
    parser.add_argument("--write", type=Path, help="Optional manifest receipt path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = build_export_manifest(
        tour_root=args.tour_root,
        incoming_root=args.incoming_root,
        providers=set(args.providers) if args.providers else None,
        limit_per_provider=max(1, int(args.limit_per_provider or 1)),
    )
    if args.prepare_dirs:
        manifest["prepared_drop_dirs"] = prepare_export_drop_dirs(manifest)
    body = json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True)
    if args.write:
        output = args.write.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(body + "\n", encoding="utf-8")
    sys.stdout.write(body + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
