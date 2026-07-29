#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

try:
    from scripts.property_magicfit_public_eligibility import (
        evaluate_magicfit_public_eligibility,
        magicfit_provider_declared as _shared_magicfit_provider_declared,
    )
    from scripts.property_magicfit_secure_io import (
        MagicFitSecureIOError,
        read_stable_bounded_prefix,
    )
    from scripts.property_magicfit_delivery_contract import (
        PENDING_POINTER_RELPATH,
    )
    from scripts.property_tour_3dvista_provenance import (
        safe_relpath as _safe_provenance_relpath,
        validate_3dvista_target_provenance,
    )
    from scripts.property_tour_panorama_provenance import (
        KRPANO_SPATIAL_PROVENANCE_KEY,
        PANO2VR_SPATIAL_PROVENANCE_KEY,
        asset_set_sha256 as _panorama_asset_set_sha256,
        export_tree_sha256 as _panorama_export_tree_sha256,
        panorama_asset_relpaths,
        panorama_walkable_required,
        pano2vr_export_topology,
        safe_relpath as _safe_panorama_provenance_relpath,
        validate_panorama_spatial_provenance,
        walkable_scene_topology,
    )
    from scripts.property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        require_bounded_file,
        require_bounded_tree,
        tour_manifest_max_bytes,
    )
    from scripts.property_tour_runtime_paths import (
        best_tour_root as _best_tour_root,
        manifest_count as _manifest_count,
        preferred_public_tour_root,
        running_container_public_tour_dir as _running_container_public_tour_dir,
    )
except ModuleNotFoundError:
    from property_magicfit_public_eligibility import (  # type: ignore[no-redef]
        evaluate_magicfit_public_eligibility,
        magicfit_provider_declared as _shared_magicfit_provider_declared,
    )
    from property_magicfit_secure_io import (  # type: ignore[no-redef]
        MagicFitSecureIOError,
        read_stable_bounded_prefix,
    )
    from property_magicfit_delivery_contract import (  # type: ignore[no-redef]
        PENDING_POINTER_RELPATH,
    )
    from property_tour_3dvista_provenance import (  # type: ignore[no-redef]
        safe_relpath as _safe_provenance_relpath,
        validate_3dvista_target_provenance,
    )
    from property_tour_panorama_provenance import (  # type: ignore[no-redef]
        KRPANO_SPATIAL_PROVENANCE_KEY,
        PANO2VR_SPATIAL_PROVENANCE_KEY,
        asset_set_sha256 as _panorama_asset_set_sha256,
        export_tree_sha256 as _panorama_export_tree_sha256,
        panorama_asset_relpaths,
        panorama_walkable_required,
        pano2vr_export_topology,
        safe_relpath as _safe_panorama_provenance_relpath,
        validate_panorama_spatial_provenance,
        walkable_scene_topology,
    )
    from property_tour_host_safety import (  # type: ignore[no-redef]
        TourHostSafetyError,
        bounded_env_int,
        require_bounded_file,
        require_bounded_tree,
        tour_manifest_max_bytes,
    )
    from property_tour_runtime_paths import (  # type: ignore[no-redef]
        best_tour_root as _best_tour_root,
        manifest_count as _manifest_count,
        preferred_public_tour_root,
        running_container_public_tour_dir as _running_container_public_tour_dir,
    )


PROVIDER_MODES = ("matterport", "3dvista", "pano2vr", "krpano", "magicfit")
CORE_REQUIRED_PROVIDER_MODES = ("3dvista",)
ADVANCED_VISUAL_REQUIRED_PROVIDER_MODES = ("magicfit",)
# Compatibility envelope for operator tooling that historically treated the
# interactive tour and walkthrough lanes as one Gold requirement.
PUBLIC_REQUIRED_PROVIDER_MODES = (
    *CORE_REQUIRED_PROVIDER_MODES,
    *ADVANCED_VISUAL_REQUIRED_PROVIDER_MODES,
)


def _normalize_gold_scope(value: str) -> str:
    normalized = str(value or "core").strip().lower().replace("-", "_")
    if normalized == "core":
        return "core"
    if normalized in {"advanced", "advanced_visual"}:
        return "advanced_visual"
    raise ValueError(f"unsupported_propertyquarry_gold_scope:{normalized}")


def _required_provider_modes_for_scope(value: str) -> tuple[str, ...]:
    scope = _normalize_gold_scope(value)
    if scope == "advanced_visual":
        return (
            *CORE_REQUIRED_PROVIDER_MODES,
            *ADVANCED_VISUAL_REQUIRED_PROVIDER_MODES,
        )
    return CORE_REQUIRED_PROVIDER_MODES
OPTIONAL_PROVIDER_CONFIGURED_BLOCKER_REASONS = {
    "krpano": {
        "missing_krpano_license_environment",
        "walkable_scene_asset_missing_or_not_360",
        "krpano_spatial_provenance_missing_or_invalid",
    },
    "pano2vr": {
        "pano2vr_entry_missing_or_not_verified",
        "pano2vr_spatial_provenance_missing_or_invalid",
    },
}
PROVIDER_DELIVERY_REQUIREMENTS = {
    "matterport": [
        "An allowlisted Matterport model URL backed by a fresh model-publication receipt",
        "At least three available connected sweeps spanning two rooms with a declared navigation graph",
        "A passing first-party browser/security review proving that the route serves the exact approved model",
    ],
    "3dvista": [
        "A verified non-trial 3DVista VT Pro export or allowlisted hosted 3dvista.com tour URL",
        "A PropertyQuarry import/control manifest with tdvplayer runtime evidence",
        "A private target-bound receipt covering authorization, exact export bytes, and human property/visual match",
    ],
    "pano2vr": [
        "A verified Pano2VR export containing index.html, pano.xml, and pano2vr_player.js",
        "A PropertyQuarry import/control manifest for the exported tour bundle",
        "A private exact-byte receipt proving authorized spatial capture, observed scene topology, and dated human property/visual review",
    ],
    "krpano": [
        "A configured krpano license environment for the PropertyQuarry domain",
        "A real walkable_scene backed by equirectangular panorama or cubemap assets",
        "A private exact-asset receipt proving authorized spatial capture, observed scene topology, and dated human property/visual review",
    ],
    "magicfit": [
        "A receipt-backed MagicFit walkthrough video for a PropertyQuarry property",
        "A local playable video asset or live-probed allowlisted hosted video URL",
        "A PropertyQuarry manifest with provider=magicfit and playback evidence",
    ],
}
PROVIDER_WHITE_LABEL_REQUIREMENTS = {
    "3dvista": [
        "A delivered 3DVista Private Viewer bundle for propertyquarry.com, or",
        "A verified non-trial VT Pro export/control URL with no trial branding and PropertyQuarry-owned tour metadata",
        "A private target-bound receipt that proves the viewer is the exact authorized PropertyQuarry tour, not a provider sample or Chummer RunSite/Horizon demo",
    ],
    "matterport": [
        "A PropertyQuarry-hosted control route wrapping the allowlisted Matterport model",
        "Public-safe metadata that does not leak the raw Matterport model ID in receipts",
    ],
    "pano2vr": [
        "A PropertyQuarry-hosted Pano2VR export bundle with local control route",
        "No vendor-trial or sample-tour markers in the exported viewer shell",
    ],
    "krpano": [
        "A configured PropertyQuarry-domain krpano license",
        "A PropertyQuarry-hosted scene/control route for real panorama assets",
    ],
    "magicfit": [
        "A PropertyQuarry-hosted playback/control route for the receipt-backed walkthrough",
        "Public-safe manifest metadata for generated walkthrough media",
    ],
}
PUBLIC_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
PANORAMA_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
TEXT_RUNTIME_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".json", ".xml"}
MAX_MARKER_SCAN_BYTES = 1_000_000
MAX_MARKER_SCAN_FILES = 240
THREE_D_VISTA_FORBIDDEN_PREMIUM_MARKERS = (
    "created with the trial of 3dvista",
    "trial of 3dvista vt pro",
)
KRPANO_FORBIDDEN_SCENE_STRATEGIES = {"generated_listing_summary", "photo_gallery_hosted", "floorplan_hosted", "pure_360_cube"}
KRPANO_FORBIDDEN_CREATION_MODES = {"hosted_listing_fallback", "hosted_photo_gallery_tour"}
EQUIRECTANGULAR_MIN_RATIO = 1.9
EQUIRECTANGULAR_MAX_RATIO = 2.1
CLI_ENV_KEYS = {"KRPANO_LICENSE_DOMAIN", "KRPANO_LICENSE_KEY"}
THREE_D_VISTA_WHITE_LABEL_SOURCE_PROJECT_TOKENS = ("propertyquarry", "property-quarry", "propertyquarry.com", "property_quarry")
THREE_D_VISTA_CHUMMER_SOURCE_TOKENS = ("chummer", "horizon", "runsite")
MAGICFIT_PENDING_POINTER_RELPATH = PENDING_POINTER_RELPATH


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"nonfinite:{value}")


def _tour_root() -> Path:
    return preferred_public_tour_root(
        configured_root=os.getenv("EA_PUBLIC_TOUR_DIR") or "",
        repo_root=Path(__file__).resolve().parents[1],
        fallback_root="/docker/property/state/public_property_tours",
        runtime_container=os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "",
    )


def _runtime_container_name() -> str:
    return str(os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "propertyquarry-api").strip() or "propertyquarry-api"


def _snapshot_runtime_container_public_tours(container_name: str = "") -> tuple[Path | None, tempfile.TemporaryDirectory[str] | None]:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return None, None
    normalized_container = str(container_name or _runtime_container_name()).strip()
    if not normalized_container:
        return None, None
    temp_dir = tempfile.TemporaryDirectory(prefix="propertyquarry-public-tours-")
    try:
        completed = subprocess.run(
            [docker_bin, "cp", f"{normalized_container}:/data/public_property_tours/.", temp_dir.name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        temp_dir.cleanup()
        return None, None
    if completed.returncode != 0:
        temp_dir.cleanup()
        return None, None
    return Path(temp_dir.name).resolve(), temp_dir


def _resolve_tour_root(
    *,
    tour_root: Path | None,
    live_probe: bool,
) -> tuple[Path, str, tempfile.TemporaryDirectory[str] | None]:
    if tour_root is not None:
        return tour_root.expanduser().resolve(), "explicit", None
    if live_probe:
        runtime_root = _running_container_public_tour_dir(_runtime_container_name())
        if runtime_root is not None:
            return runtime_root.expanduser().resolve(), "runtime_container", None
        runtime_snapshot_root, runtime_snapshot_handle = _snapshot_runtime_container_public_tours(_runtime_container_name())
        if runtime_snapshot_root is not None and runtime_snapshot_handle is not None:
            return runtime_snapshot_root, "runtime_container_snapshot", runtime_snapshot_handle
    return _tour_root().expanduser().resolve(), "preferred", None


def _load_cli_env_defaults() -> None:
    candidate_paths = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[1] / ".env",
    ]
    for env_path in candidate_paths:
        if not env_path.is_file():
            continue
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key not in CLI_ENV_KEYS or os.getenv(key):
                continue
            os.environ[key] = value.strip().strip('"').strip("'")
        break


def _safe_asset_relpath(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        return ""
    parts = [part for part in raw.split("/") if part and part not in {".", ".."}]
    if not parts:
        return ""
    return "/".join(parts)


def _canonical_asset_relpath(value: object) -> str:
    """Return a safe path only when the supplied spelling is already canonical."""

    if not isinstance(value, str) or not value or value != value.strip():
        return ""
    if "\\" in value or value.startswith("/"):
        return ""
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return ""
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        return ""
    normalized = _safe_asset_relpath(value)
    if normalized != value or PurePosixPath(value).as_posix() != value:
        return ""
    return normalized


def _safe_http_url(value: object, *, allowed_hosts: Iterable[str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return ""
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    for allowed in allowed_hosts:
        allowed_host = str(allowed or "").strip().lower().rstrip(".")
        if host == allowed_host or host.endswith(f".{allowed_host}"):
            return raw
    return ""


def _has_key(payload: dict[str, object], *keys: str) -> bool:
    return any(key in payload for key in keys)


def _normalize_three_d_vista_source_project(raw: object) -> str:
    candidate = str(raw or "").strip().lower()
    if not candidate:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", "-", candidate).strip("-")
    if any(token in normalized for token in THREE_D_VISTA_WHITE_LABEL_SOURCE_PROJECT_TOKENS):
        return "propertyquarry"
    if any(token in normalized for token in THREE_D_VISTA_CHUMMER_SOURCE_TOKENS):
        return "chummer-runsite-horizon"
    return normalized


def _three_d_vista_white_label_proof_project(payload: dict[str, object]) -> str:
    raw_payload = payload.get("three_d_vista_white_label_proof")
    if isinstance(raw_payload, dict):
        for candidate in (
            raw_payload.get("source_project"),
            raw_payload.get("project"),
            raw_payload.get("tenant"),
            raw_payload.get("project_id"),
            raw_payload.get("source"),
            raw_payload.get("workspace"),
        ):
            normalized = _normalize_three_d_vista_source_project(candidate)
            if normalized:
                return normalized
        nested = raw_payload.get("white_label")
        if isinstance(nested, dict):
            for candidate in (
                nested.get("source_project"),
                nested.get("project"),
                nested.get("tenant"),
                nested.get("project_id"),
                nested.get("source"),
                nested.get("workspace"),
            ):
                normalized = _normalize_three_d_vista_source_project(candidate)
                if normalized:
                    return normalized

    import_payload = payload.get("three_d_vista_import")
    if isinstance(import_payload, dict):
        for candidate in (
            import_payload.get("source_project"),
            import_payload.get("tenant"),
            import_payload.get("project"),
            import_payload.get("source"),
        ):
            normalized = _normalize_three_d_vista_source_project(candidate)
            if normalized:
                return normalized
    return ""


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "verified", "ready", "pass"}


def _three_d_vista_white_label_evidence(payload: dict[str, object]) -> dict[str, object]:
    raw_payload = payload.get("three_d_vista_white_label_proof")
    proof_payload = raw_payload if isinstance(raw_payload, dict) else {}
    source_project = _three_d_vista_white_label_proof_project(payload)
    proof_kind = str(proof_payload.get("proof_kind") or proof_payload.get("kind") or "").strip().lower()
    private_viewer_verified = _truthy(
        proof_payload.get("private_viewer_verified")
        or proof_payload.get("private_viewer_delivered")
        or proof_payload.get("propertyquarry_private_viewer_verified")
    )
    non_trial_export_verified = _truthy(
        proof_payload.get("non_trial_export_verified")
        or proof_payload.get("licensed_export_verified")
        or proof_payload.get("vt_pro_export_verified")
    )
    propertyquarry_tour_metadata = _truthy(
        proof_payload.get("propertyquarry_tour_metadata")
        or proof_payload.get("propertyquarry_owned_tour_metadata")
        or proof_payload.get("property_tour_metadata_verified")
    )
    trial_branding_checked = _truthy(proof_payload.get("trial_branding_checked"))
    ready_basis = ""
    if source_project == "propertyquarry" and private_viewer_verified:
        ready_basis = "propertyquarry_private_viewer"
    elif (
        source_project == "propertyquarry"
        and non_trial_export_verified
        and propertyquarry_tour_metadata
    ):
        ready_basis = "propertyquarry_non_trial_vt_pro_export"
    return {
        "source_project": source_project,
        "proof_kind": proof_kind,
        "private_viewer_verified": private_viewer_verified,
        "non_trial_export_verified": non_trial_export_verified,
        "propertyquarry_tour_metadata": propertyquarry_tour_metadata,
        "trial_branding_checked": trial_branding_checked,
        "ready_basis": ready_basis,
    }


def _three_d_vista_private_viewer_ready(payload: dict[str, object]) -> bool:
    raw_payload = payload.get("three_d_vista_white_label_proof")
    proof_payload = raw_payload if isinstance(raw_payload, dict) else {}
    evidence = _three_d_vista_white_label_evidence(payload)
    return (
        evidence.get("source_project") == "propertyquarry"
        and bool(evidence.get("non_trial_export_verified"))
        and bool(evidence.get("propertyquarry_tour_metadata"))
        and bool(evidence.get("trial_branding_checked"))
        and not _truthy(proof_payload.get("trial_branding_present"))
    )


def _three_d_vista_target_provenance_errors(
    bundle_dir: Path,
    payload: dict[str, object],
) -> list[str]:
    raw_provenance = payload.get("three_d_vista_target_provenance")
    if not isinstance(raw_provenance, dict):
        return ["receipt_missing"]
    slug = str(payload.get("slug") or bundle_dir.name).strip()
    artifact = (
        dict(raw_provenance.get("artifact") or {})
        if isinstance(raw_provenance.get("artifact"), dict)
        else {}
    )
    evidence_kind = str(artifact.get("kind") or "").strip().lower()
    entry_relpath = _three_d_vista_entry_relpath(payload)
    provider_url = ""
    for key in ("three_d_vista_url", "threedvista_url", "3dvista_url", "source_virtual_tour_url", "crezlo_public_url"):
        provider_url = _safe_http_url(payload.get(key), allowed_hosts=("3dvista.com",))
        if provider_url:
            break

    export_dir: Path | None = None
    expected_export_entry = ""
    if evidence_kind == "local_export":
        imported = (
            dict(payload.get("three_d_vista_import") or {})
            if isinstance(payload.get("three_d_vista_import"), dict)
            else {}
        )
        entry_parts = PurePosixPath(entry_relpath).parts if entry_relpath else ()
        target_subdir = _safe_provenance_relpath(
            raw_provenance.get("target_subdir")
            or imported.get("target_subdir")
            or (entry_parts[0] if len(entry_parts) > 1 else "")
        )
        if not target_subdir:
            return ["target_subdir_missing"]
        candidate = (bundle_dir / target_subdir).resolve()
        bundle_root = bundle_dir.resolve()
        if bundle_root not in candidate.parents:
            return ["target_subdir_invalid"]
        export_dir = candidate
        prefix = f"{target_subdir}/"
        if not entry_relpath.startswith(prefix):
            return ["entry_outside_target_subdir"]
        expected_export_entry = entry_relpath[len(prefix) :]

    _normalized, errors = validate_3dvista_target_provenance(
        dict(raw_provenance),
        target_slug=slug,
        export_dir=export_dir,
        entry_relpath=expected_export_entry,
        provider_url=provider_url,
    )
    return errors


def _three_d_vista_target_provenance_ready(bundle_dir: Path, payload: dict[str, object]) -> bool:
    return not _three_d_vista_target_provenance_errors(bundle_dir, payload)


def _three_d_vista_browser_render_ready(payload: dict[str, object]) -> bool:
    for key in (
        "three_d_vista_browser_render_proof",
        "threedvista_browser_render_proof",
        "3dvista_browser_render_proof",
        "browser_render_proof",
    ):
        proof = payload.get(key)
        if not isinstance(proof, dict):
            continue
        provider = str(proof.get("provider") or proof.get("viewer_provider") or "3dvista").strip().lower()
        if provider not in {"3dvista", "3d_vista", "three_d_vista"}:
            continue
        status = str(proof.get("status") or proof.get("result") or "").strip().lower()
        if status not in {"pass", "ready", "rendered"}:
            continue
        if _truthy(proof.get("rendered_viewer") or proof.get("viewer_rendered") or proof.get("browser_rendered")):
            return True
        checks = list(proof.get("checks") or [])
        if checks and all(isinstance(row, dict) and row.get("ok") is True for row in checks):
            return True
    return False


def _three_d_vista_cross_project_warning(source_projects: set[str]) -> str:
    non_property_projects = sorted(
        project for project in source_projects if project and project != "propertyquarry"
    )
    base_warning = (
        "Chummer RunSite/Horizon white-label readiness is reusable process evidence only; it is not PropertyQuarry tour proof."
    )
    if non_property_projects:
        return f"{base_warning} Observed white-label sources: {', '.join(non_property_projects)}."
    return f"{base_warning} A PropertyQuarry-owned white-label proof is still required."


def _provider_3d_white_label_status(
    *,
    provider: str,
    provider_counts: dict[str, int],
    missing: list[str],
    evidence_rows: list[dict[str, object]],
) -> tuple[str, list[str], str, dict[str, object]]:
    if provider != "3dvista":
        status = "ready" if provider not in missing and provider_counts.get(provider, 0) > 0 else "blocked"
        return status, (list(PROVIDER_WHITE_LABEL_REQUIREMENTS[provider]) if status == "blocked" else []), "", {}

    source_projects = {
        str(row.get("source_project") or "").strip()
        for row in evidence_rows
        if str(row.get("source_project") or "").strip()
    }
    ready_rows = [row for row in evidence_rows if str(row.get("ready_basis") or "").strip()]
    proof_basis = {
        "source_projects": sorted(source_projects),
        "ready_basis": sorted({str(row.get("ready_basis") or "").strip() for row in ready_rows if str(row.get("ready_basis") or "").strip()}),
        "private_viewer_verified": any(bool(row.get("private_viewer_verified")) for row in evidence_rows),
        "non_trial_export_verified": any(bool(row.get("non_trial_export_verified")) for row in evidence_rows),
        "propertyquarry_tour_metadata": any(bool(row.get("propertyquarry_tour_metadata")) for row in evidence_rows),
        "trial_branding_checked": any(bool(row.get("trial_branding_checked")) for row in evidence_rows),
    }
    if provider in missing:
        return "blocked", list(PROVIDER_WHITE_LABEL_REQUIREMENTS[provider]), _three_d_vista_cross_project_warning(source_projects), proof_basis
    if ready_rows:
        return "ready", [], "", proof_basis
    return "review_required", list(PROVIDER_WHITE_LABEL_REQUIREMENTS[provider]), _three_d_vista_cross_project_warning(source_projects), proof_basis


def _pano2vr_entry_relpath(payload: dict[str, object]) -> str:
    for key in ("pano2vr_entry_relpath", "pano2vr_export_entry_relpath"):
        relpath = _safe_asset_relpath(payload.get(key))
        if relpath:
            return relpath
    return ""


def _pano2vr_export_root_relpath(payload: dict[str, object]) -> str:
    for key in (
        "pano2vr_export_root_relpath",
        "pano2vr_root_relpath",
    ):
        relpath = _safe_panorama_provenance_relpath(payload.get(key))
        if relpath:
            return relpath
    import_payload = payload.get("pano2vr_import")
    if isinstance(import_payload, dict):
        relpath = _safe_panorama_provenance_relpath(
            import_payload.get("target_subdir") or import_payload.get("export_root_relpath")
        )
        if relpath:
            return relpath
    entry_relpath = _pano2vr_entry_relpath(payload)
    entry_parts = PurePosixPath(entry_relpath).parts if entry_relpath else ()
    if len(entry_parts) > 1:
        return PurePosixPath(*entry_parts[:-1]).as_posix()
    return ""


def _panorama_hash_limits() -> tuple[int, int, int]:
    return (
        bounded_env_int(
            "PROPERTYQUARRY_PANORAMA_MAX_HASH_FILES",
            default=5_000,
            minimum=1,
            maximum=20_000,
        ),
        bounded_env_int(
            "PROPERTYQUARRY_PANORAMA_MAX_HASH_BYTES",
            default=512 * 1024 * 1024,
            minimum=1_024,
            maximum=2 * 1024 * 1024 * 1024,
        ),
        bounded_env_int(
            "PROPERTYQUARRY_PANORAMA_MAX_FILE_BYTES",
            default=256 * 1024 * 1024,
            minimum=1_024,
            maximum=512 * 1024 * 1024,
        ),
    )


def _panorama_spatial_provenance_errors(
    bundle_dir: Path,
    payload: dict[str, object],
    *,
    provider: str,
) -> list[str]:
    normalized_provider = str(provider or "").strip().lower()
    key = (
        PANO2VR_SPATIAL_PROVENANCE_KEY
        if normalized_provider == "pano2vr"
        else KRPANO_SPATIAL_PROVENANCE_KEY
        if normalized_provider == "krpano"
        else ""
    )
    raw_receipt = payload.get(key) if key else None
    if not isinstance(raw_receipt, dict):
        return ["receipt_missing"]
    slug = str(payload.get("slug") or bundle_dir.name).strip()
    bundle_root = bundle_dir.resolve()
    maximum_files, maximum_total_bytes, maximum_file_bytes = (
        _panorama_hash_limits()
    )
    try:
        if normalized_provider == "pano2vr":
            entry_relpath = _pano2vr_entry_relpath(payload)
            export_root_relpath = _pano2vr_export_root_relpath(payload)
            if not entry_relpath or not export_root_relpath:
                return ["pano2vr_export_root_missing"]
            export_dir = (bundle_root / export_root_relpath).resolve()
            if export_dir == bundle_root or bundle_root not in export_dir.parents:
                return ["pano2vr_export_root_invalid"]
            entry_prefix = f"{export_root_relpath}/"
            if not entry_relpath.startswith(entry_prefix):
                return ["pano2vr_entry_outside_export_root"]
            expected_entry = entry_relpath[len(entry_prefix) :]
            require_bounded_tree(
                export_dir,
                reason_prefix="panorama_pano2vr_export",
                maximum_files=maximum_files,
                maximum_total_bytes=maximum_total_bytes,
                maximum_file_bytes=maximum_file_bytes,
                maximum_depth=24,
            )
            artifact_sha256 = _panorama_export_tree_sha256(
                export_dir,
                maximum_files=maximum_files,
                maximum_total_bytes=maximum_total_bytes,
                maximum_file_bytes=maximum_file_bytes,
            )
            observed_topology = pano2vr_export_topology(export_dir)
            artifact_kind = "local_export"
            observed_projection = ""
        elif normalized_provider == "krpano":
            asset_relpaths = panorama_asset_relpaths(payload)
            if not asset_relpaths:
                return ["panorama_assets_missing"]
            artifact_sha256 = _panorama_asset_set_sha256(
                bundle_root,
                asset_relpaths,
                maximum_files=maximum_files,
                maximum_total_bytes=maximum_total_bytes,
                maximum_file_bytes=maximum_file_bytes,
            )
            observed_topology = walkable_scene_topology(payload)
            artifact_kind = "panorama_assets"
            expected_entry = ""
            walkable_scene = payload.get("walkable_scene")
            projection = (
                str(
                    walkable_scene.get("projection")
                    or walkable_scene.get("type")
                    or ""
                )
                .strip()
                .lower()
                if isinstance(walkable_scene, dict)
                else ""
            )
            observed_projection = {
                "panorama": "equirectangular",
                "cube": "cubemap",
            }.get(projection, projection)
        else:
            return ["provider_invalid"]
    except (OSError, RuntimeError, ValueError, TourHostSafetyError):
        return ["artifact_unhashable_or_topology_invalid"]
    if not artifact_sha256:
        return ["artifact_unhashable_or_empty"]
    _normalized, errors = validate_panorama_spatial_provenance(
        dict(raw_receipt),
        provider=normalized_provider,
        target_slug=slug,
        artifact_kind=artifact_kind,
        artifact_sha256=artifact_sha256,
        entry_relpath=expected_entry,
        observed_projection=observed_projection,
        observed_topology=observed_topology,
        walkable_required=panorama_walkable_required(payload),
    )
    return errors


def _panorama_spatial_provenance_ready(
    bundle_dir: Path,
    payload: dict[str, object],
    *,
    provider: str,
) -> bool:
    return not _panorama_spatial_provenance_errors(
        bundle_dir,
        payload,
        provider=provider,
    )


def _three_d_vista_entry_relpath(payload: dict[str, object]) -> str:
    for key in ("three_d_vista_entry_relpath", "threedvista_entry_relpath", "3dvista_entry_relpath"):
        relpath = _safe_asset_relpath(payload.get(key))
        if relpath:
            return relpath
    import_payload = payload.get("three_d_vista_import")
    if isinstance(import_payload, dict):
        relpath = _safe_asset_relpath(import_payload.get("entry_relpath"))
        if relpath:
            return relpath
    return ""


def _magicfit_video_relpath(payload: dict[str, object]) -> str:
    for key in ("video_relpath", "flythrough_video_relpath", "magicfit_video_relpath"):
        relpath = _canonical_asset_relpath(payload.get(key))
        if relpath and PurePosixPath(relpath).suffix.lower() in PUBLIC_VIDEO_EXTENSIONS:
            return relpath
    return ""


def _magicfit_video_url(payload: dict[str, object]) -> str:
    if not _magicfit_provider_declared(payload):
        return ""
    return _safe_http_url(payload.get("video_url"), allowed_hosts=("propertyquarry.com", "myexternalbrain.com"))


def _magicfit_provider_declared(payload: dict[str, object]) -> bool:
    return _shared_magicfit_provider_declared(payload)


def _magicfit_delivery_receipt_disqualified(
    bundle_dir: Path,
    payload: dict[str, object],
) -> bool:
    shared = evaluate_magicfit_public_eligibility(bundle_dir, payload)
    return bool(shared.declared and not shared.eligible)


def _magicfit_pending_delivery_present(bundle_dir: Path) -> bool:
    """Surface staged operator work without treating it as public evidence."""

    pending_path = bundle_dir / MAGICFIT_PENDING_POINTER_RELPATH
    try:
        return (
            pending_path.is_file()
            and not pending_path.is_symlink()
            and pending_path.stat().st_size > 0
        )
    except OSError:
        return False


def _file_exists(bundle_dir: Path, relpath: str) -> bool:
    return _local_asset_path(bundle_dir, relpath) is not None


def _local_asset_path(bundle_dir: Path, relpath: str) -> Path | None:
    if not relpath:
        return None
    try:
        bundle_root = bundle_dir.resolve()
        candidate = (bundle_dir / relpath).resolve()
        if bundle_root not in candidate.parents or not candidate.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _local_image_dimensions(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return (0, 0)


def _local_equirectangular_image_ready(bundle_dir: Path, relpath: str) -> bool:
    if not relpath or PurePosixPath(relpath).suffix.lower() not in PANORAMA_IMAGE_EXTENSIONS:
        return False
    candidate = _local_asset_path(bundle_dir, relpath)
    if candidate is None:
        return False
    width, height = _local_image_dimensions(candidate)
    if width < 1024 or height < 512:
        return False
    ratio = width / height if height else 0
    return EQUIRECTANGULAR_MIN_RATIO <= ratio <= EQUIRECTANGULAR_MAX_RATIO


def _local_cube_face_ready(bundle_dir: Path, relpath: str) -> bool:
    if not relpath or PurePosixPath(relpath).suffix.lower() not in PANORAMA_IMAGE_EXTENSIONS:
        return False
    candidate = _local_asset_path(bundle_dir, relpath)
    if candidate is None:
        return False
    width, height = _local_image_dimensions(candidate)
    if width < 512 or height < 512:
        return False
    ratio = width / height if height else 0
    return 0.9 <= ratio <= 1.1


def _krpano_scene_has_real_360_asset(
    bundle_dir: Path,
    scene: dict[str, object],
    *,
    default_projection: str = "",
) -> bool:
    projection = str(
        scene.get("projection")
        or scene.get("type")
        or default_projection
        or ""
    ).strip().lower()
    if projection and projection not in {"equirectangular", "panorama", "cubemap", "cube"}:
        return False
    for key in ("panorama_relpath", "equirect_relpath", "image_relpath", "asset_relpath"):
        relpath = _safe_asset_relpath(scene.get(key))
        if _local_equirectangular_image_ready(bundle_dir, relpath):
            return True
    cube_faces = scene.get("cube_faces")
    if isinstance(cube_faces, dict):
        values = list(cube_faces.values())
    elif isinstance(cube_faces, list):
        values = cube_faces
    else:
        values = []
    face_relpaths = [_safe_asset_relpath(value) for value in values]
    valid_faces = [
        relpath
        for relpath in face_relpaths
        if _local_cube_face_ready(bundle_dir, relpath)
    ]
    return len(valid_faces) >= 6


def _walkable_scene_has_real_360_asset(bundle_dir: Path, payload: dict[str, object]) -> bool:
    scene_strategy = str(payload.get("scene_strategy") or "").strip().lower()
    creation_mode = str(payload.get("creation_mode") or "").strip().lower()
    if scene_strategy in KRPANO_FORBIDDEN_SCENE_STRATEGIES or creation_mode in KRPANO_FORBIDDEN_CREATION_MODES:
        return False
    walkable_scene = payload.get("walkable_scene")
    if not isinstance(walkable_scene, dict) or not walkable_scene:
        return False
    default_projection = str(
        walkable_scene.get("projection") or walkable_scene.get("type") or ""
    ).strip().lower()
    raw_scenes = walkable_scene.get("scenes")
    if isinstance(raw_scenes, dict):
        scene_rows = [row for row in raw_scenes.values() if isinstance(row, dict)]
    elif isinstance(raw_scenes, list):
        scene_rows = [row for row in raw_scenes if isinstance(row, dict)]
    else:
        scene_rows = []
    if scene_rows:
        return all(
            _krpano_scene_has_real_360_asset(
                bundle_dir,
                scene,
                default_projection=default_projection,
            )
            for scene in scene_rows
        )
    return _krpano_scene_has_real_360_asset(
        bundle_dir,
        walkable_scene,
        default_projection=default_projection,
    )


def _ffprobe_video_markers(target: str | Path) -> dict[str, object]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"ffprobe_available": False}
    try:
        result = subprocess.run(
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
                str(target),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return {"ffprobe_available": True, "ffprobe_error": f"{type(exc).__name__}: {exc}"}
    if result.returncode != 0:
        return {"ffprobe_available": True, "ffprobe_error": (result.stderr or "ffprobe_failed")[:200]}
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception as exc:
        return {"ffprobe_available": True, "ffprobe_error": f"json_{type(exc).__name__}"}
    streams = [row for row in list(payload.get("streams") or []) if isinstance(row, dict)]
    has_video_stream = any(str(row.get("codec_type") or "").strip().lower() == "video" for row in streams)
    durations: list[float] = []
    for value in [payload.get("format", {}).get("duration") if isinstance(payload.get("format"), dict) else None]:
        try:
            durations.append(float(value))
        except Exception:
            pass
    for row in streams:
        try:
            durations.append(float(row.get("duration")))
        except Exception:
            pass
    duration_seconds = max(durations) if durations else 0.0
    return {
        "ffprobe_available": True,
        "video_stream": has_video_stream,
        "duration_seconds": round(duration_seconds, 3),
        "duration_positive": duration_seconds > 0.0,
    }


def _local_text_assets_for_html_entry(bundle_dir: Path, relpath: str) -> list[Path]:
    if not relpath:
        return []
    bundle_root = bundle_dir.resolve()
    entry = (bundle_dir / relpath).resolve()
    if bundle_root not in entry.parents or not entry.is_file():
        return []
    if PurePosixPath(relpath).suffix.lower() not in {".html", ".htm"}:
        return []
    scan_root = entry.parent
    candidates = [entry]
    for candidate in sorted(scan_root.rglob("*")):
        if len(candidates) >= MAX_MARKER_SCAN_FILES:
            break
        resolved = candidate.resolve()
        if (
            candidate.is_file()
            and candidate.suffix.lower() in TEXT_RUNTIME_SUFFIXES
            and bundle_root in resolved.parents
            and resolved not in candidates
        ):
            candidates.append(resolved)
    return candidates


def _local_html_asset_has_marker(bundle_dir: Path, relpath: str, *, markers: Iterable[str]) -> bool:
    normalized_markers = tuple(str(marker or "").strip().lower() for marker in markers if str(marker or "").strip())
    if not normalized_markers:
        return False
    for candidate in _local_text_assets_for_html_entry(bundle_dir, relpath):
        try:
            if candidate.stat().st_size > MAX_MARKER_SCAN_BYTES:
                continue
            body = candidate.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if any(marker in body for marker in normalized_markers):
            return True
    return False


def _local_html_asset_has_forbidden_marker(bundle_dir: Path, relpath: str, *, markers: Iterable[str]) -> bool:
    return _local_html_asset_has_marker(bundle_dir, relpath, markers=markers)


def _local_video_asset_is_playable(bundle_dir: Path, relpath: str) -> bool:
    if not relpath:
        return False
    candidate = _local_asset_path(bundle_dir, relpath)
    if candidate is None:
        return False
    suffix = PurePosixPath(relpath).suffix.lower()
    try:
        snapshot = read_stable_bounded_prefix(
            candidate,
            reason="property_tour_video_prefix_invalid",
            maximum_bytes=2 * 1024 * 1024 * 1024,
            prefix_bytes=64,
        )
        header = snapshot.prefix
    except (MagicFitSecureIOError, OSError):
        return False
    if len(header) < 12:
        return False
    signature_ok = False
    if suffix in {".mp4", ".m4v", ".mov"}:
        signature_ok = b"ftyp" in header[:32]
    elif suffix == ".webm":
        signature_ok = header.startswith(b"\x1aE\xdf\xa3")
    if not signature_ok:
        return False
    markers = _ffprobe_video_markers(candidate)
    if not markers.get("ffprobe_available"):
        return True
    return bool(markers.get("video_stream") and markers.get("duration_positive"))


def _magicfit_local_video_ready(bundle_dir: Path, payload: dict[str, object]) -> bool:
    eligibility = evaluate_magicfit_public_eligibility(bundle_dir, payload)
    return (
        eligibility.declared
        and eligibility.eligible
        and _local_video_asset_is_playable(bundle_dir, eligibility.video_relpath)
    )


def _tour_payload_is_disabled_fallback(payload: dict[str, object]) -> bool:
    scene_strategy = str(payload.get("scene_strategy") or "").strip().lower()
    creation_mode = str(payload.get("creation_mode") or "").strip().lower()
    control_mode = str(payload.get("control_mode") or "").strip().lower()
    if scene_strategy in {"generated_listing_summary", "photo_gallery_hosted", "floorplan_hosted", "pure_360_cube"}:
        return True
    if creation_mode == "hosted_listing_fallback":
        return True
    if control_mode in {"walkable_3d", "internal_walkable_3d"}:
        return True
    scenes = [dict(row) for row in (payload.get("scenes") or []) if isinstance(row, dict)]
    return any(str(scene.get("role") or "").strip() == "generated_overview" for scene in scenes)


def _load_provider_receipt(bundle_dir: Path) -> dict[str, object]:
    receipt_path = bundle_dir / "tour.private.json"
    if not receipt_path.is_file():
        return {}
    try:
        require_bounded_file(
            receipt_path,
            reason_prefix="private_tour_receipt",
            maximum_bytes=tour_manifest_max_bytes(),
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TourHostSafetyError):
        return {}
    if not isinstance(receipt, dict):
        return {}
    allowed_keys = {
        "crezlo_public_url",
        KRPANO_SPATIAL_PROVENANCE_KEY,
        "pano2vr_entry_relpath",
        "pano2vr_export_entry_relpath",
        "pano2vr_export_root_relpath",
        "pano2vr_root_relpath",
        PANO2VR_SPATIAL_PROVENANCE_KEY,
        "source_virtual_tour_url",
        "source_virtual_tour_origin",
        "three_d_vista_browser_render_proof",
        "three_d_vista_import",
        "three_d_vista_target_provenance",
        "three_d_vista_white_label_proof",
        "three_d_vista_url",
        "threedvista_url",
        "matterport_model_publication",
        "matterport_url",
        "matterport_walkthrough",
    }
    return {key: receipt.get(key) for key in allowed_keys if str(receipt.get(key) or "").strip()}


def _payload_with_private_provider_receipt(bundle_dir: Path, payload: dict[str, object]) -> dict[str, object]:
    receipt = _load_provider_receipt(bundle_dir)
    if not receipt:
        return payload
    return {**payload, **receipt}


def _matterport_public_control_ready(payload: dict[str, object]) -> bool:
    provider_url = ""
    for key in ("matterport_url", "source_virtual_tour_url"):
        provider_url = _safe_http_url(
            payload.get(key),
            allowed_hosts=("matterport.com",),
        )
        if provider_url:
            break
    if not provider_url:
        return False
    parsed_url = urllib.parse.urlparse(provider_url)
    model_sid = ""
    host = str(parsed_url.hostname or "").strip().lower().rstrip(".")
    if host == "discover.matterport.com" and parsed_url.path.startswith("/space/"):
        model_sid = parsed_url.path.rsplit("/", 1)[-1].strip()
    elif host == "my.matterport.com" and parsed_url.path.startswith("/models/"):
        model_sid = parsed_url.path.rsplit("/", 1)[-1].strip()
    elif host == "my.matterport.com" and parsed_url.path.rstrip("/") == "/show":
        model_sid = next(
            (
                value
                for key, value in urllib.parse.parse_qsl(
                    parsed_url.query,
                    keep_blank_values=False,
                )
                if key == "m"
            ),
            "",
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", model_sid):
        return False
    publication = payload.get("matterport_model_publication")
    if not isinstance(publication, dict):
        return False
    if (
        str(publication.get("status") or "").strip().lower() != "pass"
        or publication.get("model_available") is not True
        or str(publication.get("model_sid") or "").strip() != model_sid
    ):
        return False
    try:
        checked_at = datetime.fromisoformat(
            str(publication.get("checked_at") or "").replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        proof_valid_until = datetime.fromisoformat(
            str(
                publication.get("proof_valid_until")
                or publication.get("asset_valid_until")
                or ""
            ).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        enabled_sweep_count = int(publication.get("enabled_sweep_count") or 0)
        available_sweep_count = int(publication.get("available_sweep_count") or 0)
        connected_component_count = int(
            publication.get("connected_component_count") or 0
        )
        room_count = int(publication.get("room_count") or 0)
        navigation_edge_count = int(
            publication.get("navigation_edge_count") or 0
        )
    except (TypeError, ValueError):
        return False
    now = datetime.now(timezone.utc)
    source_sha256 = str(publication.get("source_sha256") or "").strip().lower()
    return bool(
        now - timedelta(hours=24) <= checked_at <= now + timedelta(minutes=5)
        and proof_valid_until > now
        and enabled_sweep_count >= 3
        and available_sweep_count >= 3
        and connected_component_count == 1
        and room_count >= 2
        and navigation_edge_count >= enabled_sweep_count - 1
        and re.fullmatch(r"[a-f0-9]{64}", source_sha256)
    )


def _provider_missing_evidence(bundle_dir: Path, payload: dict[str, object]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if _tour_payload_is_disabled_fallback(payload):
        return rows

    matterport_candidate = any(
        str(payload.get(key) or "").strip()
        for key in ("matterport_url", "source_virtual_tour_url", "crezlo_public_url")
    )
    matterport_url_ready = any(
        _safe_http_url(payload.get(key), allowed_hosts=("matterport.com",))
        for key in ("matterport_url", "source_virtual_tour_url", "crezlo_public_url")
    )
    if matterport_url_ready and _matterport_public_control_ready(payload):
        reason = ""
        action = ""
    elif matterport_url_ready:
        reason = "matterport_model_publication_missing_or_invalid"
        action = (
            "attach a fresh matching model-publication receipt proving at least "
            "three connected sweeps, two rooms, and a sufficient navigation graph"
        )
    elif matterport_candidate:
        reason = "matterport_url_not_allowlisted_or_invalid"
        action = "replace with a public Matterport URL on my.matterport.com or matterport.com"
    else:
        reason = "missing_matterport_url"
        action = "add matterport_url or source_virtual_tour_url from a real Matterport model"
    if reason:
        rows.append({"provider": "matterport", "reason": reason, "action": action})

    three_d_vista_entry = _three_d_vista_entry_relpath(payload)
    three_d_vista_url_ready = any(
        _safe_http_url(payload.get(key), allowed_hosts=("3dvista.com",))
        for key in ("three_d_vista_url", "threedvista_url", "3dvista_url", "source_virtual_tour_url", "crezlo_public_url")
    )
    three_d_vista_entry_ready = _local_html_asset_has_marker(
        bundle_dir,
        three_d_vista_entry,
        markers=("tdvplayer", "tdvplayerapi", "tourviewer"),
    )
    three_d_vista_trial_branded = bool(three_d_vista_entry_ready) and _local_html_asset_has_forbidden_marker(
        bundle_dir,
        three_d_vista_entry,
        markers=THREE_D_VISTA_FORBIDDEN_PREMIUM_MARKERS,
    )
    if three_d_vista_trial_branded:
        rows.append(
            {
                "provider": "3dvista",
                "reason": "3dvista_trial_branding_present",
                "action": "replace the trial-branded 3DVista export with a licensed 3DVista VT Pro export",
            }
        )
    elif not (three_d_vista_url_ready or three_d_vista_entry_ready):
        if three_d_vista_entry:
            reason = "3dvista_entry_missing_or_not_verified"
            action = (
                "import a licensed 3DVista export whose entry HTML contains runtime markers, then attach "
                "private target-bound approval and property/visual review provenance"
            )
        elif _has_key(payload, "three_d_vista_url", "threedvista_url", "3dvista_url"):
            reason = "3dvista_placeholder_field_empty_or_unusable"
            action = (
                "replace the empty 3DVista placeholder with an allowlisted 3dvista.com URL or licensed export, then attach "
                "private target-bound approval and property/visual review provenance"
            )
        else:
            reason = "missing_3dvista_export"
            action = (
                "provide a licensed 3DVista export or allowlisted 3dvista.com URL, then attach private "
                "target-bound approval and property/visual review provenance"
            )
        rows.append({"provider": "3dvista", "reason": reason, "action": action})
    elif not _three_d_vista_target_provenance_ready(bundle_dir, payload):
        rows.append(
            {
                "provider": "3dvista",
                "reason": "3dvista_target_provenance_missing_or_invalid",
                "action": (
                    "attach a private propertyquarry.3dvista_target_provenance.v1 receipt that binds "
                    "the exact slug and export bytes to approved reuse plus a dated property/visual match"
                ),
            }
        )
    elif not _three_d_vista_private_viewer_ready(payload):
        rows.append(
            {
                "provider": "3dvista",
                "reason": "3dvista_private_viewer_proof_missing",
                "action": "attach PropertyQuarry private-viewer proof before exposing the 3DVista control route",
            }
        )
    elif not _three_d_vista_browser_render_ready(payload):
        rows.append(
            {
                "provider": "3dvista",
                "reason": "3dvista_browser_render_proof_missing",
                "action": "live-probe the 3DVista control route in a browser and persist a passing render proof",
            }
        )

    pano2vr_entry = _pano2vr_entry_relpath(payload)
    pano2vr_entry_ready = _local_html_asset_has_marker(
        bundle_dir,
        pano2vr_entry,
        markers=("ggpkg", "ggskin", "pano.xml", "tour.js"),
    )
    if not pano2vr_entry_ready:
        if pano2vr_entry:
            reason = "pano2vr_entry_missing_or_not_verified"
            action = "import a real Pano2VR export whose entry HTML contains Pano2VR runtime markers"
        elif _has_key(payload, "pano2vr_entry_relpath", "pano2vr_export_entry_relpath", "pano2vr_export_root_relpath", "pano2vr_root_relpath"):
            reason = "pano2vr_placeholder_field_empty_or_unusable"
            action = "replace the empty Pano2VR placeholder field with a verified local Pano2VR export entry"
        else:
            reason = "missing_pano2vr_export"
            action = "run import_pano2vr_export.py with a verified Pano2VR export"
        rows.append({"provider": "pano2vr", "reason": reason, "action": action})
    elif not _panorama_spatial_provenance_ready(
        bundle_dir,
        payload,
        provider="pano2vr",
    ):
        rows.append(
            {
                "provider": "pano2vr",
                "reason": "pano2vr_spatial_provenance_missing_or_invalid",
                "action": (
                    "attach a private propertyquarry.panorama_spatial_provenance.v1 receipt that binds the exact "
                    "Pano2VR export bytes and observed topology to approved reuse plus dated human property, visual, "
                    "and spatial-capture review"
                ),
            }
        )

    krpano_license_ready = bool(os.getenv("KRPANO_LICENSE_DOMAIN") and os.getenv("KRPANO_LICENSE_KEY"))
    krpano_asset_ready = _walkable_scene_has_real_360_asset(bundle_dir, payload)
    if not (krpano_license_ready and krpano_asset_ready):
        if not isinstance(payload.get("walkable_scene"), dict):
            reason = "missing_walkable_scene"
            action = "generate or import a real walkable_scene before enabling the licensed krpano control"
        elif not krpano_license_ready:
            reason = "missing_krpano_license_environment"
            action = "set KRPANO_LICENSE_DOMAIN and KRPANO_LICENSE_KEY for the property runtime"
        else:
            reason = "walkable_scene_asset_missing_or_not_360"
            action = "attach a real local equirectangular panorama or six cube-face assets before enabling krpano"
        rows.append({"provider": "krpano", "reason": reason, "action": action})
    elif not _panorama_spatial_provenance_ready(
        bundle_dir,
        payload,
        provider="krpano",
    ):
        rows.append(
            {
                "provider": "krpano",
                "reason": "krpano_spatial_provenance_missing_or_invalid",
                "action": (
                    "attach a private propertyquarry.panorama_spatial_provenance.v1 receipt that binds the exact "
                    "panorama assets and observed topology to approved reuse plus dated human property, visual, "
                    "and spatial-capture review"
                ),
            }
        )

    magicfit_relpath = _magicfit_video_relpath(payload)
    magicfit_url = _magicfit_video_url(payload)
    if not _magicfit_local_video_ready(bundle_dir, payload):
        provider = str(
            payload.get("video_provider")
            or payload.get("video_provider_key")
            or payload.get("video_render_provider")
            or ""
        ).strip().lower()
        if _magicfit_delivery_receipt_disqualified(
            bundle_dir, payload
        ) or _magicfit_pending_delivery_present(bundle_dir):
            reason = "magicfit_walkthrough_disqualified"
            action = (
                "complete delivery acceptance for the exact active MagicFit video, "
                "or render and import a replacement MagicFit walkthrough if review rejects it"
            )
        elif provider and provider != "magicfit":
            reason = "walkthrough_provider_not_magicfit"
            action = "render and import a MagicFit walkthrough with provider=magicfit"
        elif magicfit_url:
            reason = "magicfit_remote_video_needs_live_probe"
            action = "run verify_property_tour_controls.py with --live-probe or import the MagicFit video as a local playable asset"
        elif magicfit_relpath:
            reason = "magicfit_video_missing_or_unplayable"
            action = "run import_magicfit_walkthrough.py with a receipt-backed playable MP4/M4V/MOV/WebM"
        else:
            reason = "missing_magicfit_walkthrough"
            action = "render and import a receipt-backed playable MagicFit walkthrough"
        rows.append({"provider": "magicfit", "reason": reason, "action": action})

    return rows


def _missing_evidence_blocks_public_tour(row: dict[str, str]) -> bool:
    provider = str(row.get("provider") or "").strip().lower()
    return provider in PUBLIC_REQUIRED_PROVIDER_MODES


def _surface_optional_missing_evidence(row: dict[str, str], payload: dict[str, object]) -> bool:
    provider = str(row.get("provider") or "").strip().lower()
    if provider == "krpano":
        walkable_scene = payload.get("walkable_scene")
        if not isinstance(walkable_scene, dict):
            return False
        scene_strategy = str(payload.get("scene_strategy") or "").strip().lower()
        creation_mode = str(payload.get("creation_mode") or "").strip().lower()
        return bool(walkable_scene) or scene_strategy == "walkable_panorama" or creation_mode == "hosted_walkable_360"
    if provider == "pano2vr":
        return bool(_pano2vr_entry_relpath(payload)) or _has_key(
            payload,
            "pano2vr_entry_relpath",
            "pano2vr_export_entry_relpath",
            "pano2vr_export_root_relpath",
            "pano2vr_root_relpath",
        )
    return False


def _control_candidates(*, slug: str, bundle_dir: Path, payload: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if _tour_payload_is_disabled_fallback(payload):
        return rows

    if _matterport_public_control_ready(payload):
        rows.append(
            {
                "provider": "matterport",
                "status": "ready",
                "control_path": f"/tours/{slug}/control/matterport",
                "evidence": "topology_verified_matterport_model_publication",
            }
        )

    three_d_vista_url = ""
    for key in ("three_d_vista_url", "threedvista_url", "3dvista_url", "source_virtual_tour_url", "crezlo_public_url"):
        three_d_vista_url = _safe_http_url(payload.get(key), allowed_hosts=("3dvista.com",))
        if three_d_vista_url:
            break
    three_d_vista_entry = _three_d_vista_entry_relpath(payload)
    three_d_vista_entry_ready = _local_html_asset_has_marker(
        bundle_dir,
        three_d_vista_entry,
        markers=("tdvplayer", "tdvplayerapi", "tourviewer"),
    )
    three_d_vista_trial_branded = bool(three_d_vista_entry_ready) and _local_html_asset_has_forbidden_marker(
        bundle_dir,
        three_d_vista_entry,
        markers=THREE_D_VISTA_FORBIDDEN_PREMIUM_MARKERS,
    )
    three_d_vista_private_ready = _three_d_vista_private_viewer_ready(payload)
    three_d_vista_target_ready = _three_d_vista_target_provenance_ready(bundle_dir, payload)
    three_d_vista_browser_ready = _three_d_vista_browser_render_ready(payload)
    if three_d_vista_private_ready and three_d_vista_target_ready and (
        three_d_vista_url or (three_d_vista_entry_ready and not three_d_vista_trial_branded)
    ):
        rows.append(
            {
                "provider": "3dvista",
                "status": "ready" if three_d_vista_browser_ready else "probe_required",
                "control_path": f"/tours/{slug}/control/3dvista",
                "evidence": "allowlisted_3dvista_url" if three_d_vista_url else "local_3dvista_export_entry",
            }
        )

    pano2vr_entry = _pano2vr_entry_relpath(payload)
    pano2vr_entry_ready = _local_html_asset_has_marker(
        bundle_dir,
        pano2vr_entry,
        markers=("ggpkg", "ggskin", "pano.xml", "tour.js"),
    )
    if pano2vr_entry_ready and _panorama_spatial_provenance_ready(
        bundle_dir,
        payload,
        provider="pano2vr",
    ):
        rows.append(
            {
                "provider": "pano2vr",
                "status": "ready",
                "control_path": f"/tours/{slug}/control/pano2vr",
                "evidence": "provenance_bound_pano2vr_spatial_export",
            }
        )

    if (
        os.getenv("KRPANO_LICENSE_DOMAIN")
        and os.getenv("KRPANO_LICENSE_KEY")
        and _walkable_scene_has_real_360_asset(bundle_dir, payload)
        and _panorama_spatial_provenance_ready(
            bundle_dir,
            payload,
            provider="krpano",
        )
    ):
        rows.append(
            {
                "provider": "krpano",
                "status": "ready",
                "control_path": f"/tours/{slug}/control/krpano",
                "evidence": "provenance_bound_licensed_krpano_spatial_scene",
            }
        )

    magicfit_relpath = _magicfit_video_relpath(payload)
    magicfit_url = _magicfit_video_url(payload)
    magicfit_disqualified = _magicfit_delivery_receipt_disqualified(bundle_dir, payload)
    if _magicfit_local_video_ready(bundle_dir, payload):
        rows.append(
            {
                "provider": "magicfit",
                "status": "ready",
                "control_path": f"/tours/files/{slug}/{magicfit_relpath}",
                "evidence": "local_magicfit_playable_video",
            }
        )
    elif magicfit_url and not magicfit_disqualified:
        rows.append(
            {
                "provider": "magicfit",
                "status": "probe_required",
                "control_path": "",
                "evidence": "allowlisted_magicfit_video_url_pending_probe",
                "_probe_url": magicfit_url,
            }
        )
    return rows


def _summarize_provider_blockers(reason_counts: dict[str, dict[str, dict[str, object]]]) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for provider in PROVIDER_MODES:
        rows = []
        for reason, payload in sorted(
            reason_counts.get(provider, {}).items(),
            key=lambda item: (-int(item[1].get("count") or 0), item[0]),
        ):
            rows.append(
                {
                    "reason": reason,
                    "count": int(payload.get("count") or 0),
                    "action": str(payload.get("action") or "").strip(),
                }
            )
        summary[provider] = {
            "blocked_count": sum(int(row["count"]) for row in rows),
            "reasons": rows,
        }
    return summary


def _next_provider_action(
    provider: str, provider_blockers: dict[str, dict[str, object]]
) -> str:
    blocker = provider_blockers.get(provider)
    if isinstance(blocker, dict):
        reasons = blocker.get("reasons")
        if isinstance(reasons, list):
            for row in reasons:
                if isinstance(row, dict):
                    action = str(row.get("action") or "").strip()
                    if action:
                        return action
    return {
        "matterport": (
            "provide a fresh matching Matterport model-publication receipt proving "
            "a connected multi-room capture"
        ),
        "3dvista": (
            "provide a licensed 3DVista export or allowlisted 3dvista.com URL plus private target-bound "
            "approval and property/visual review provenance"
        ),
        "pano2vr": "import a verified Pano2VR export",
        "krpano": "provide a real walkable_scene and krpano license environment",
        "magicfit": "import a receipt-backed playable MagicFit walkthrough video",
    }[provider]


def _provider_delivery_contracts(
    *,
    tours: list[dict[str, object]],
    provider_counts: dict[str, int],
    provider_blockers: dict[str, dict[str, object]],
    missing_provider_modes: list[str],
    provider_ready_controls: dict[str, list[dict[str, object]]],
    three_d_vista_white_label_evidence: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    missing = set(missing_provider_modes)
    contracts: dict[str, dict[str, object]] = {}
    for provider in PROVIDER_MODES:
        ready_controls = list(provider_ready_controls.get(provider) or [])
        blocker = dict(provider_blockers.get(provider) or {})
        reasons = [dict(row) for row in list(blocker.get("reasons") or []) if isinstance(row, dict)]
        blocked_reason = (
            str(reasons[0].get("reason") or "").strip()
            if reasons
            else (f"missing_{provider}_evidence" if provider in missing else "")
        )
        (
            white_label_status,
            white_label_required_to_white_label,
            white_label_warning,
            white_label_proof_basis,
        ) = _provider_3d_white_label_status(
            provider=provider,
            provider_counts=provider_counts,
            missing=missing_provider_modes,
            evidence_rows=three_d_vista_white_label_evidence if provider == "3dvista" else [],
        )
        ready_payload = {
            "provider": provider,
            "ready_count": int(provider_counts.get(provider) or 0),
            "sample_controls": ready_controls[:5],
            "manifest_url": "",
        }
        provider_is_ready = (
            provider not in missing
            and int(provider_counts.get(provider) or 0) > 0
        )
        surface_blocker = provider in missing or (
            provider == "matterport" and not provider_is_ready
        )
        contracts[provider] = {
            "schema": "propertyquarry.tour_delivery_contract.v1",
            "provider": provider,
            "status": "ready" if provider_is_ready else "blocked",
            "ready_payload": ready_payload,
            "blocked_reason": blocked_reason if surface_blocker else "",
            "required_to_send": (
                list(PROVIDER_DELIVERY_REQUIREMENTS[provider])
                if surface_blocker
                else []
            ),
            "white_label_contract": {
                "schema": "propertyquarry.tour_white_label_contract.v1",
                "provider": provider,
                "status": white_label_status,
                "required_to_white_label": white_label_required_to_white_label,
                "source_project": "propertyquarry",
                "cross_project_warning": white_label_warning if provider == "3dvista" else "",
                "proof_basis": white_label_proof_basis if provider == "3dvista" else {},
            },
            "notes": [
                "Public-safe contract only; raw external provider URLs and private listing fields are intentionally omitted.",
                "The viewer presents tour media only. PropertyQuarry remains source of truth for listing facts, ranking, evidence, pricing, entitlement, and customer decisions.",
            ],
        }
    return contracts


def _blocked_control_reason(payload: dict[str, object]) -> str:
    scene_strategy = str(payload.get("scene_strategy") or "").strip().lower()
    creation_mode = str(payload.get("creation_mode") or "").strip().lower()
    if scene_strategy == "photo_gallery_hosted" or creation_mode == "hosted_photo_gallery_tour":
        return "gallery_only_not_3d"
    if scene_strategy == "pure_360_cube":
        return "generated_cube_not_verified_3d"
    if creation_mode in {"hosted_listing_fallback", "generated_listing_summary"}:
        return "listing_summary_not_verified_3d"
    return "missing_verified_provider_control"


def _probe_url(url: str, *, timeout_seconds: float, provider: str = "", host_header: str = "") -> dict[str, object]:
    normalized_provider = str(provider or "").strip().lower()
    request_headers = {"User-Agent": "PropertyQuarry-tour-control-verifier/1.0"}
    if str(host_header or "").strip():
        request_headers["Host"] = str(host_header).strip()
    if normalized_provider == "magicfit":
        request_headers["Accept"] = "video/mp4,video/webm,video/*;q=0.9,*/*;q=0.1"
    request = urllib.request.Request(url, method="GET", headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if normalized_provider == "magicfit":
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                sample = response.read(64)
                suffix = PurePosixPath(urllib.parse.urlparse(url).path).suffix.lower()
                signature_ok = (
                    (suffix in {".mp4", ".m4v", ".mov"} and b"ftyp" in sample[:32])
                    or (suffix == ".webm" and sample.startswith(b"\x1aE\xdf\xa3"))
                )
                ffprobe_markers = _ffprobe_video_markers(url)
                playback_markers = {
                    "video_content_type": content_type.startswith("video/"),
                    "video_signature": signature_ok,
                }
                if ffprobe_markers.get("ffprobe_available"):
                    playback_markers["video_stream"] = bool(ffprobe_markers.get("video_stream"))
                    playback_markers["duration_positive"] = bool(ffprobe_markers.get("duration_positive"))
                return {
                    "http_status": int(getattr(response, "status", 0) or 0),
                    "content_type": content_type,
                    "playback_markers": playback_markers,
                    "ffprobe": ffprobe_markers,
                }
            body = response.read(80_000).decode("utf-8", errors="replace")
            body_lower = body.lower()
            has_3d_shell = "3d tour" in body_lower
            return {
                "http_status": int(getattr(response, "status", 0) or 0),
                "body_markers": {
                    "matterport": has_3d_shell and "my.matterport.com/show/" in body_lower,
                    "3dvista": has_3d_shell
                    and (
                        "/tours/3dvista/" in body_lower
                        or "3dvista.com" in body_lower
                    ),
                    "pano2vr": has_3d_shell and "/tours/pano2vr/" in body_lower,
                    "krpano": "krpano" in body and "krpano-license" in body,
                },
            }
    except urllib.error.HTTPError as exc:
        payload = {"http_status": int(exc.code), "error": str(exc.reason or exc)}
        try:
            body = exc.read(8_000).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if body:
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {}
            error_block = dict(parsed.get("error") or {}) if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict) else {}
            error_code = str(error_block.get("code") or "").strip()
            if error_code:
                payload["error_code"] = error_code
        return payload
    except Exception as exc:
        return {"http_status": 0, "error": f"{type(exc).__name__}: {exc}"}


def _optional_hidden_probe_is_acceptable(control: dict[str, object], probe: dict[str, object]) -> bool:
    provider = str(control.get("provider") or "").strip().lower()
    if provider not in {"pano2vr", "krpano"}:
        return False
    if int(probe.get("http_status") or 0) != 404:
        return False
    if str(probe.get("error_code") or "").strip() != "tour_control_panorama_export_hidden":
        return False
    evidence = str(control.get("evidence") or "").strip().lower()
    return evidence in {
        "provenance_bound_pano2vr_spatial_export",
        "provenance_bound_licensed_krpano_spatial_scene",
    }


def build_property_tour_control_receipt(
    *,
    tour_root: Path | None = None,
    base_url: str = "",
    host_header: str = "",
    live_probe: bool = False,
    timeout_seconds: float = 5.0,
    require_all_provider_modes: bool = False,
    gold_scope: str = "core",
) -> dict[str, object]:
    normalized_gold_scope = _normalize_gold_scope(gold_scope)
    selected_required_provider_modes = _required_provider_modes_for_scope(
        normalized_gold_scope
    )
    root, root_source, runtime_snapshot_handle = _resolve_tour_root(tour_root=tour_root, live_probe=live_probe)
    try:
        manifests = sorted(root.glob("*/tour.json")) if root.is_dir() else []
        tours: list[dict[str, object]] = []
        provider_counts = {provider: 0 for provider in PROVIDER_MODES}
        action_counts = {provider: 0 for provider in PROVIDER_MODES}
        provider_blocker_reason_counts: dict[str, dict[str, dict[str, object]]] = {provider: {} for provider in PROVIDER_MODES}
        provider_ready_controls: dict[str, list[dict[str, object]]] = {provider: [] for provider in PROVIDER_MODES}
        provider_hidden_counts = {provider: 0 for provider in PROVIDER_MODES}
        three_d_vista_white_label_evidence: list[dict[str, object]] = []
        magicfit_playback_evidence_count = 0
        magicfit_playback_evidence: list[dict[str, object]] = []
        invalid_manifest_failures = 0
        provider_probe_failure_counts = {
            provider: 0 for provider in PUBLIC_REQUIRED_PROVIDER_MODES
        }
        for manifest_path in manifests:
            bundle_dir = manifest_path.parent.resolve()
            try:
                payload = json.loads(
                    manifest_path.read_text(encoding="utf-8"),
                    object_pairs_hook=_strict_json_object,
                    parse_constant=_reject_nonfinite_json,
                )
            except Exception as exc:
                tours.append({"slug": manifest_path.parent.name, "status": "invalid_manifest", "error": f"{type(exc).__name__}: {exc}"})
                invalid_manifest_failures += 1
                continue
            if not isinstance(payload, dict):
                tours.append({"slug": manifest_path.parent.name, "status": "invalid_manifest"})
                invalid_manifest_failures += 1
                continue
            payload = _payload_with_private_provider_receipt(bundle_dir, payload)
            slug = str(payload.get("slug") or manifest_path.parent.name).strip()
            three_d_vista_white_label_evidence_row = _three_d_vista_white_label_evidence(payload)
            controls = _control_candidates(slug=slug, bundle_dir=bundle_dir, payload=payload)
            for control in controls:
                provider = str(control.get("provider") or "").strip().lower()
                internal_probe_url = str(control.pop("_probe_url", "") or "").strip()
                if live_probe and ((base_url and control.get("control_path")) or internal_probe_url):
                    probe_url = internal_probe_url or urllib.parse.urljoin(base_url.rstrip("/") + "/", str(control["control_path"]).lstrip("/"))
                    probe = _probe_url(
                        probe_url,
                        timeout_seconds=timeout_seconds,
                        provider=str(control.get("provider") or ""),
                        host_header=host_header,
                    )
                    control["probe"] = probe
                    playback_markers = dict(probe.get("playback_markers") or {})
                    playback_failed = bool(playback_markers) and not all(bool(value) for value in playback_markers.values())
                    body_markers = dict(probe.get("body_markers") or {})
                    marker_failed = bool(body_markers) and provider in body_markers and not bool(body_markers.get(provider))
                    hidden_optional_ready = _optional_hidden_probe_is_acceptable(control, probe)
                    if hidden_optional_ready:
                        control["route_visibility"] = "hidden_by_product_boundary"
                        control["customer_visible"] = False
                    elif int(probe.get("http_status") or 0) != 200 or playback_failed or marker_failed:
                        if provider in PUBLIC_REQUIRED_PROVIDER_MODES:
                            control["status"] = "probe_failed"
                            provider_probe_failure_counts[provider] += 1
                        else:
                            control["status"] = "optional_probe_failed"
                    elif str(control.get("status") or "").strip().lower() == "probe_required":
                        control["status"] = "ready"
                        if provider == "magicfit":
                            control["evidence"] = "live_probed_magicfit_video_url"
                if provider in provider_counts and str(control.get("status") or "").strip().lower() == "ready":
                    provider_counts[provider] += 1
                    if str(control.get("route_visibility") or "").strip() == "hidden_by_product_boundary":
                        provider_hidden_counts[provider] += 1
                    provider_ready_controls[provider].append(
                        {
                            "slug": slug,
                            "title": str(payload.get("display_title") or payload.get("title") or slug).strip()[:160],
                            "control_path": str(control.get("control_path") or "").strip(),
                            "evidence": str(control.get("evidence") or "").strip(),
                            "route_visibility": str(control.get("route_visibility") or "").strip() or "public",
                        }
                    )
                    if provider == "3dvista":
                        three_d_vista_white_label_evidence.append(three_d_vista_white_label_evidence_row)
                    if provider == "magicfit" and str(control.get("evidence") or "").strip() in {
                        "local_magicfit_playable_video",
                        "live_probed_magicfit_video_url",
                    }:
                        magicfit_playback_evidence_count += 1
                        magicfit_playback_evidence.append(
                            {
                                "slug": slug,
                                "provider": "magicfit",
                                "evidence": str(control.get("evidence") or "").strip(),
                                "control_path": str(control.get("control_path") or "").strip(),
                                "media_identity": str(control.get("control_path") or "").strip(),
                            }
                        )
            ready_control_providers = {
                str(control.get("provider") or "").strip().lower()
                for control in controls
                if str(control.get("status") or "").strip().lower() == "ready"
            }
            missing_evidence = [
                row
                for row in _provider_missing_evidence(bundle_dir, payload)
                if str(row.get("provider") or "").strip().lower() not in ready_control_providers
            ]
            for row in missing_evidence:
                provider = str(row.get("provider") or "").strip().lower()
                if provider in action_counts:
                    action_counts[provider] += 1
                    reason = str(row.get("reason") or "unknown").strip() or "unknown"
                    existing = provider_blocker_reason_counts[provider].setdefault(
                        reason,
                        {"count": 0, "action": str(row.get("action") or "").strip()},
                    )
                    existing["count"] = int(existing.get("count") or 0) + 1
            ready_controls = [
                control
                for control in controls
                if str(control.get("status") or "").strip().lower() == "ready"
            ]
            required_missing_evidence = [
                row
                for row in missing_evidence
                if _missing_evidence_blocks_public_tour(row)
            ]
            optional_missing_evidence = [
                row
                for row in missing_evidence
                if not _missing_evidence_blocks_public_tour(row) and _surface_optional_missing_evidence(row, payload)
            ]
            missing_public_evidence = required_missing_evidence
            tour_missing_provider_modes = sorted(
                {
                    str(row.get("provider") or "").strip().lower()
                    for row in required_missing_evidence
                }
            )
            tours.append(
                {
                    "slug": slug,
                    "title": str(payload.get("display_title") or payload.get("title") or slug).strip()[:160],
                    "status": "ready" if ready_controls else "blocked_missing_verified_controls",
                    "blocked_reason": "" if ready_controls else _blocked_control_reason(payload),
                    "controls": [
                        {
                            "provider": str(control.get("provider") or "").strip(),
                            "status": str(control.get("status") or "").strip(),
                            "control_path": str(control.get("control_path") or "").strip(),
                            "evidence": str(control.get("evidence") or "").strip(),
                            "route_visibility": str(control.get("route_visibility") or "").strip(),
                        }
                        for control in controls
                    ],
                    "missing_evidence": missing_public_evidence,
                    "optional_missing_evidence": optional_missing_evidence,
                    "missing_provider_modes": tour_missing_provider_modes,
                }
            )
        ready_provider_modes = sorted(provider for provider, count in provider_counts.items() if count > 0)
        hidden_ready_provider_modes = sorted(provider for provider, count in provider_hidden_counts.items() if count > 0)
        missing_provider_modes = [provider for provider in PUBLIC_REQUIRED_PROVIDER_MODES if provider not in ready_provider_modes]
        core_missing_provider_modes = [
            provider
            for provider in CORE_REQUIRED_PROVIDER_MODES
            if provider not in ready_provider_modes
        ]
        advanced_visual_missing_provider_modes = [
            provider
            for provider in ADVANCED_VISUAL_REQUIRED_PROVIDER_MODES
            if provider not in ready_provider_modes
        ]
        selected_missing_provider_modes = [
            provider
            for provider in selected_required_provider_modes
            if provider not in ready_provider_modes
        ]
        global_failed_probes = sum(provider_probe_failure_counts.values())
        selected_failed_probes = sum(
            provider_probe_failure_counts[provider]
            for provider in selected_required_provider_modes
        )
        provider_blockers = _summarize_provider_blockers(provider_blocker_reason_counts)
        status = (
            "blocked_no_tour_manifests"
            if not manifests
            else "fail"
            if invalid_manifest_failures or selected_failed_probes
            else "blocked_missing_provider_modes"
            if require_all_provider_modes and selected_missing_provider_modes
            else "pass"
            if ready_provider_modes
            else "blocked_missing_verified_controls"
        )
        return {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "tour_root": str(root),
            "tour_root_source": root_source,
            "tour_count": len(manifests),
            "ready_tour_count": sum(1 for tour in tours if tour.get("status") == "ready"),
            "provider_counts": provider_counts,
            "provider_probe_failures": {
                "global_count": global_failed_probes,
                "selected_fatal_count": selected_failed_probes,
                "by_provider": provider_probe_failure_counts,
            },
            "provider_blockers": provider_blockers,
            "delivery_contracts": _provider_delivery_contracts(
                tours=tours,
                provider_counts=provider_counts,
                provider_blockers=provider_blockers,
                missing_provider_modes=missing_provider_modes,
                provider_ready_controls=provider_ready_controls,
                three_d_vista_white_label_evidence=three_d_vista_white_label_evidence,
            ),
            "magicfit_playback": {
                "playback_ok": provider_counts.get("magicfit", 0) == 0 or magicfit_playback_evidence_count == provider_counts.get("magicfit", 0),
                "playable_count": magicfit_playback_evidence_count,
                "ready_count": provider_counts.get("magicfit", 0),
                "evidence": magicfit_playback_evidence[:12],
            },
            "ready_provider_modes": ready_provider_modes,
            "hidden_ready_provider_modes": hidden_ready_provider_modes,
            "core_required_provider_modes": list(CORE_REQUIRED_PROVIDER_MODES),
            "advanced_visual_required_provider_modes": list(ADVANCED_VISUAL_REQUIRED_PROVIDER_MODES),
            "core_missing_provider_modes": core_missing_provider_modes,
            "advanced_visual_missing_provider_modes": advanced_visual_missing_provider_modes,
            "gold_scope": normalized_gold_scope,
            "selected_required_provider_modes": list(
                selected_required_provider_modes
            ),
            "selected_missing_provider_modes": selected_missing_provider_modes,
            "operator_missing_provider_modes": missing_provider_modes,
            "required_provider_modes": list(PUBLIC_REQUIRED_PROVIDER_MODES),
            "optional_provider_modes": [provider for provider in PROVIDER_MODES if provider not in PUBLIC_REQUIRED_PROVIDER_MODES],
            "missing_provider_modes": missing_provider_modes,
            "next_required_actions": [
                {
                    "provider": provider,
                    "blocked_tour_count": action_counts[provider],
                    "action": _next_provider_action(provider, provider_blockers),
                }
                for provider in PUBLIC_REQUIRED_PROVIDER_MODES
                if provider in missing_provider_modes
            ],
            "live_probe": bool(live_probe),
            "base_url": base_url if live_probe else "",
            "host_header": host_header if live_probe else "",
            "require_all_provider_modes": bool(require_all_provider_modes),
            "tours": tours,
            "notes": [
                "Core Gold requires verified first-party 3DVista for interactive customer tour delivery.",
                "Matterport is retained for historical/internal audit only and does not satisfy or block the public delivery gate.",
                "Pano2VR is tracked as an optional/internal export lane and does not block the public tour-control gold gate.",
                "Optional panorama export lanes can count as ready from verified local evidence even when the panorama control shell stays intentionally hidden on the public route.",
                "MagicFit is an Advanced Visual Gold walkthrough lane and is ready only when the manifest points to a receipt-backed local playable video asset or a live-probed allowlisted hosted video URL with provider=magicfit.",
                "The receipt intentionally omits raw external provider URLs and private listing/source fields.",
            ],
        }
    finally:
        if runtime_snapshot_handle is not None:
            runtime_snapshot_handle.cleanup()


def _receipt_summary(receipt: dict[str, object]) -> dict[str, object]:
    return {
        "generated_at": receipt.get("generated_at"),
        "status": receipt.get("status"),
        "tour_root": receipt.get("tour_root"),
        "tour_root_source": receipt.get("tour_root_source"),
        "tour_count": receipt.get("tour_count"),
        "ready_tour_count": receipt.get("ready_tour_count"),
        "provider_counts": receipt.get("provider_counts"),
        "provider_probe_failures": receipt.get("provider_probe_failures"),
        "provider_blockers": receipt.get("provider_blockers"),
        "ready_provider_modes": receipt.get("ready_provider_modes"),
        "core_required_provider_modes": receipt.get("core_required_provider_modes"),
        "advanced_visual_required_provider_modes": receipt.get("advanced_visual_required_provider_modes"),
        "core_missing_provider_modes": receipt.get("core_missing_provider_modes"),
        "advanced_visual_missing_provider_modes": receipt.get("advanced_visual_missing_provider_modes"),
        "gold_scope": receipt.get("gold_scope"),
        "selected_required_provider_modes": receipt.get(
            "selected_required_provider_modes"
        ),
        "selected_missing_provider_modes": receipt.get(
            "selected_missing_provider_modes"
        ),
        "operator_missing_provider_modes": receipt.get("operator_missing_provider_modes"),
        "required_provider_modes": receipt.get("required_provider_modes"),
        "missing_provider_modes": receipt.get("missing_provider_modes"),
        "next_required_actions": receipt.get("next_required_actions"),
        "live_probe": receipt.get("live_probe"),
        "base_url": receipt.get("base_url"),
        "require_all_provider_modes": receipt.get("require_all_provider_modes"),
    }


def _runtime_container_live_probe_receipt(
    *,
    base_url: str,
    host_header: str,
    timeout_seconds: float,
    require_all_provider_modes: bool,
    gold_scope: str = "core",
) -> tuple[dict[str, object] | None, int | None]:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return None, None
    container = _runtime_container_name()
    requested_base_url = str(base_url or "").strip()
    container_base_url = requested_base_url
    if requested_base_url:
        parsed = urllib.parse.urlparse(requested_base_url)
        host = str(parsed.hostname or "").strip().lower()
        if host in {"127.0.0.1", "localhost", "0.0.0.0"}:
            container_base_url = urllib.parse.urlunparse(
                (
                    parsed.scheme or "http",
                    "127.0.0.1:8090",
                    parsed.path or "",
                    "",
                    parsed.query or "",
                    "",
                )
            )
    else:
        container_base_url = "http://127.0.0.1:8090"
    command = [
        docker_bin,
        "exec",
        "-e",
        "EA_PUBLIC_TOUR_DIR=/data/public_property_tours",
        container,
        "python",
        "/app/scripts/verify_property_tour_controls.py",
        "--tour-root",
        "/data/public_property_tours",
        "--base-url",
        container_base_url,
        "--host-header",
        host_header,
        "--live-probe",
        "--timeout-seconds",
        str(timeout_seconds),
        "--gold-scope",
        gold_scope,
    ]
    if require_all_provider_modes:
        command.append("--require-all-provider-modes")
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except Exception:
        return None, None
    stdout = str(completed.stdout or "").strip()
    if not stdout:
        return None, completed.returncode
    try:
        receipt = json.loads(stdout)
    except Exception:
        return None, completed.returncode
    if not isinstance(receipt, dict):
        return None, completed.returncode
    receipt["host_runtime_probe_via"] = "docker_exec_runtime_container"
    receipt["host_runtime_probe_command"] = " ".join(shlex.quote(part) for part in command)
    receipt["host_requested_base_url"] = requested_base_url
    receipt["container_probe_base_url"] = container_base_url
    return receipt, completed.returncode


def main() -> int:
    _load_cli_env_defaults()
    parser = argparse.ArgumentParser(description="Verify PropertyQuarry hosted 3D tour and walkthrough control readiness.")
    parser.add_argument("--tour-root", default="", help="Tour root. Defaults to EA_PUBLIC_TOUR_DIR or state/public_property_tours.")
    parser.add_argument("--base-url", default=os.getenv("PROPERTYQUARRY_TOUR_CONTROL_BASE_URL") or "http://localhost:8097")
    parser.add_argument("--host-header", default=os.getenv("PROPERTYQUARRY_LIVE_HOST_HEADER") or "propertyquarry.com")
    parser.add_argument("--live-probe", action="store_true", help="Probe ready control paths over HTTP.")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--write", default="", help="Optional JSON receipt path.")
    parser.add_argument("--summary-only", action="store_true", help="Print only top-level counts/actions; --write still stores the full receipt.")
    parser.add_argument("--require-all-provider-modes", action="store_true", help="Return blocked status until every required provider mode has at least one verified live-ready control.")
    parser.add_argument(
        "--gold-scope",
        default=os.getenv("PROPERTYQUARRY_GOLD_SCOPE") or "core",
        choices=("core", "advanced_visual"),
        help="Select Core Gold providers or the additive Advanced Visual Gold provider set.",
    )
    parser.add_argument(
        "--fail-on-blocked",
        action="store_true",
        help="Return a non-zero exit code for blocked_* receipts. Use this for gold/release gates.",
    )
    args = parser.parse_args()
    explicit_tour_root = Path(args.tour_root) if str(args.tour_root or "").strip() else None
    needs_runtime_fallback = bool(args.live_probe) and explicit_tour_root is None and _running_container_public_tour_dir(_runtime_container_name()) is None
    receipt = build_property_tour_control_receipt(
        tour_root=explicit_tour_root,
        base_url=str(args.base_url or "").strip(),
        host_header=str(args.host_header or "").strip(),
        live_probe=bool(args.live_probe),
        timeout_seconds=float(args.timeout_seconds),
        require_all_provider_modes=bool(args.require_all_provider_modes),
        gold_scope=str(args.gold_scope),
    )
    if needs_runtime_fallback and str(receipt.get("tour_root_source") or "").strip() == "preferred":
        delegated_receipt, _ = _runtime_container_live_probe_receipt(
            base_url=str(args.base_url or "").strip(),
            host_header=str(args.host_header or "").strip(),
            timeout_seconds=float(args.timeout_seconds),
            require_all_provider_modes=bool(args.require_all_provider_modes),
            gold_scope=str(args.gold_scope),
        )
        if delegated_receipt is not None:
            receipt = delegated_receipt
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        Path(args.write).parent.mkdir(parents=True, exist_ok=True)
        Path(args.write).write_text(output + "\n", encoding="utf-8")
    printed_receipt = _receipt_summary(receipt) if args.summary_only else receipt
    print(json.dumps(printed_receipt, indent=2, sort_keys=True))
    status = str(receipt.get("status") or "")
    if status == "pass":
        return 0
    if status.startswith("blocked"):
        return 2 if args.fail_on_blocked else 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
