from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import base64
import fcntl
import hashlib
import html
import ipaddress
import json
import logging
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import threading
import time
import urllib.parse
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from app.api.dependencies import get_container
from app.container import AppContainer
from app.api.routes.landing import _anonymous_onboarding_status, _public_context, templates as public_templates
from app.api.routes.public_tour_payloads import (
    _PUBLIC_TOUR_ALLOWED_ASSET_EXTENSIONS,
    _PUBLIC_TOUR_ADDRESS_ALLOWED_MODES,
    _PUBLIC_TOUR_ANONYMOUS_FACT_KEYS,
    _PUBLIC_TOUR_DENIED_ASSET_EXTENSIONS,
    _PUBLIC_TOUR_EXACT_LOCATION_FACT_KEYS,
    _PUBLIC_TOUR_PRIVATE_KEY_MARKERS,
    _PUBLIC_TOUR_PRIVATE_KEYS,
    _PUBLIC_TOUR_PRIVACY_MODES,
    _PUBLIC_TOUR_PUBLIC_ASSESSMENT_KEYS,
    _PUBLIC_TOUR_PUBLIC_PDF_PRIVACY_CLASSES,
    _PUBLIC_TOUR_SCENE_KEYS,
    _PUBLIC_TOUR_TOP_LEVEL_KEYS,
    public_tour_allowed_asset_paths as _payload_public_tour_allowed_asset_paths,
    public_tour_asset_metadata as _payload_public_tour_asset_metadata,
    public_tour_canonical_path as _payload_public_tour_canonical_path,
    public_tour_env_truthy as _payload_public_tour_env_truthy,
    public_tour_exact_address_allowed as _payload_public_tour_exact_address_allowed,
    public_tour_external_media_url_allowed as _payload_public_tour_external_media_url_allowed,
    public_tour_file_url as _payload_public_tour_file_url,
    public_tour_key_is_private as _payload_public_tour_key_is_private,
    public_tour_manifest as _payload_public_tour_manifest,
    public_tour_privacy_mode as _payload_public_tour_privacy_mode,
    public_tour_safe_asset_relpath as _payload_public_tour_safe_asset_relpath,
    public_tour_safe_http_url as _payload_public_tour_safe_http_url,
    redact_public_tour_value as _payload_redact_public_tour_value,
    redacted_public_tour_facts as _payload_redacted_public_tour_facts,
    redacted_public_tour_payload as _payload_redacted_public_tour_payload,
    redacted_public_tour_scenes as _payload_redacted_public_tour_scenes,
    require_public_tour_viewable as _payload_require_public_tour_viewable,
)
from app.product.property_tour_hosting import (
    _PROPERTY_GENERATED_RECONSTRUCTION_VIEWER_VERSION,
    _hosted_property_tour_preview_image_url,
    hosted_property_tour_revocation_receipt,
)
from app.product.service import _property_feedback_reason_map, build_product_service
from app.services.public_clickrank import clickrank_head_snippet, request_hostname, request_path
from app.services.property_market_catalog import currency_code_for_country, supported_currency_codes
from app.services.public_tour_release_policy import (
    evaluate_public_tour_generated_viewer_release,
)
try:
    from scripts.property_tour_3dvista_provenance import (
        THREE_D_VISTA_PROVENANCE_FILENAMES,
        safe_relpath as _safe_3dvista_provenance_relpath,
        validate_3dvista_target_provenance,
    )
    from scripts.property_tour_panorama_provenance import (
        PANO2VR_SPATIAL_PROVENANCE_KEY,
        export_tree_sha256 as _panorama_export_tree_sha256,
        panorama_walkable_required,
        pano2vr_export_topology,
        safe_relpath as _safe_panorama_provenance_relpath,
        validate_panorama_spatial_provenance,
    )
    from scripts.property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        require_bounded_file,
        require_bounded_tree,
        tour_manifest_max_bytes,
    )
except ModuleNotFoundError:
    from property_tour_3dvista_provenance import (  # type: ignore[no-redef]
        THREE_D_VISTA_PROVENANCE_FILENAMES,
        safe_relpath as _safe_3dvista_provenance_relpath,
        validate_3dvista_target_provenance,
    )
    from property_tour_panorama_provenance import (  # type: ignore[no-redef]
        PANO2VR_SPATIAL_PROVENANCE_KEY,
        export_tree_sha256 as _panorama_export_tree_sha256,
        panorama_walkable_required,
        pano2vr_export_topology,
        safe_relpath as _safe_panorama_provenance_relpath,
        validate_panorama_spatial_provenance,
    )
    from property_tour_host_safety import (  # type: ignore[no-redef]
        TourHostSafetyError,
        bounded_env_int,
        require_bounded_file,
        require_bounded_tree,
        tour_manifest_max_bytes,
    )

router = APIRouter(tags=["public-tours"])

_PUBLIC_TOUR_ACTIONS = frozenset({"request-details", "feedback", "filters"})
_PUBLIC_TOUR_FEEDBACK_RATE_LIMIT: OrderedDict[str, tuple[float, int]] = OrderedDict()
_PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_WINDOW_SECONDS = 60.0
_PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_MAX = 12
_PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_MAX_KEYS = 2048
_PUBLIC_TOUR_DEFAULT_EXTERNAL_MEDIA_HOSTS = (
    "propertyquarry.com",
    "*.propertyquarry.com",
    "3dvista.com",
    "*.3dvista.com",
    "360.kalandra.at",
)
_PUBLIC_TOUR_PROVIDER_CSP_ORIGINS = (
    "https://3dvista.com",
    "https://*.3dvista.com",
)
# This bootstrap is retained solely for the private, receipt-backed SDK
# walkthrough proof helper below.  Public tour routing deliberately rejects the
# retired Matterport control and its CSP never grants this origin.
_MATTERPORT_SDK_BOOTSTRAP_URL = "https://static.matterport.com/showcase-sdk/latest.js"
_PUBLIC_TOUR_CSP_REPORT_PATH = "/tours/security/csp-report"
_PUBLIC_TOUR_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_GENERATED_RECONSTRUCTION_PREVIEW_PREFIX = "generated-reconstruction/"
_GENERATED_RECONSTRUCTION_PREVIEW_PRIVACY_CLASS = "generated_reconstruction_public"
_GENERATED_RECONSTRUCTION_PREVIEW_ROLES = frozenset(
    {
        "floorplan",
        "generated_reconstruction_viewer",
        "generated_reconstruction_viewer_asset",
        "photo",
    }
)
_PANO2VR_EXPORT_ALLOWED_EXTENSIONS = frozenset(
    {
        ".css",
        ".gif",
        ".ggpkg",
        ".ggskin",
        ".htm",
        ".html",
        ".jpeg",
        ".jpg",
        ".js",
        ".m4v",
        ".mjs",
        ".mov",
        ".mp4",
        ".png",
        ".svg",
        ".txt",
        ".wasm",
        ".webm",
        ".webp",
        ".xml",
    }
)
_3DVISTA_EXPORT_MARKERS = ("tdvplayer", "tdvplayerapi", "tourviewer")
_3DVISTA_FORBIDDEN_PUBLIC_MARKERS = (
    "created with the trial of 3dvista",
    "created with 3dvista",
    "3dvista virtual tour suite",
    "immocontract",
)
_3DVISTA_PROVENANCE_CACHE_SECONDS = 10.0
_3DVISTA_PROVENANCE_VALIDATION_LOCK = threading.Lock()
_PANORAMA_PROVENANCE_CACHE_SECONDS = 10.0
_PANORAMA_PROVENANCE_VALIDATION_LOCK = threading.Lock()
_PRIVATE_TOUR_RECEIPT_ALLOWED_KEYS = frozenset(
    {
        "3dvista_entry_relpath",
        "3dvista_export_root_relpath",
        "3dvista_url",
        "crezlo_public_url",
        "matterport_url",
        "krpano_spatial_provenance",
        "pano2vr_entry_relpath",
        "pano2vr_export_entry_relpath",
        "pano2vr_export_root_relpath",
        "pano2vr_root_relpath",
        PANO2VR_SPATIAL_PROVENANCE_KEY,
        "source_virtual_tour_origin",
        "source_virtual_tour_url",
        "three_d_vista_browser_render_proof",
        "three_d_vista_entry_relpath",
        "three_d_vista_export_root_relpath",
        "three_d_vista_import",
        "three_d_vista_target_provenance",
        "three_d_vista_url",
        "three_d_vista_white_label_proof",
        "threedvista_entry_relpath",
        "threedvista_export_root_relpath",
        "threedvista_url",
    }
)
_3DVISTA_EXPORT_ALLOWED_EXTENSIONS = frozenset(
    {
        ".css",
        ".gif",
        ".htm",
        ".html",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".m4v",
        ".mjs",
        ".mov",
        ".mp4",
        ".png",
        ".svg",
        ".cur",
        ".glb",
        ".txt",
        ".wasm",
        ".webm",
        ".webp",
        ".xml",
    }
)
def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "verified", "ready", "pass"}
_PANO2VR_EXPORT_MARKERS = ("ggpkg", "ggskin", "pano.xml", "tour.js")
_KRPANO_FORBIDDEN_SCENE_STRATEGIES = {"generated_listing_summary", "photo_gallery_hosted", "floorplan_hosted", "pure_360_cube"}
_KRPANO_FORBIDDEN_CREATION_MODES = {"hosted_listing_fallback", "hosted_photo_gallery_tour"}
_PANORAMA_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
log = logging.getLogger(__name__)


def _fact_value_is_weak(value: object) -> bool:
    if value is None:
        return True
    if value is False:
        return True
    if isinstance(value, (int, float)):
        return float(value) <= 0.0
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        if not value:
            return True
        return all(_fact_value_is_weak(item) for item in value)
    if isinstance(value, dict):
        return not value
    return False


def _tour_dir() -> Path:
    raw_value = str(os.getenv("EA_PUBLIC_TOUR_DIR") or "").strip()
    if raw_value:
        return Path(raw_value).expanduser()
    return Path("/docker/property/state/public_property_tours").expanduser()


def _resolved_tour_root() -> Path:
    return _tour_dir().resolve()


def _resolved_tour_bundle(slug: str) -> Path:
    safe = str(slug or "").strip()
    if not safe or "/" in safe or ".." in safe:
        raise HTTPException(status_code=404, detail="tour_not_found")
    if hosted_property_tour_revocation_receipt(safe):
        raise HTTPException(status_code=410, detail="tour_revoked")
    root = _resolved_tour_root()
    bundle_entry = root / safe
    if bundle_entry.is_symlink():
        raise HTTPException(status_code=404, detail="tour_not_found")
    bundle_dir = bundle_entry.resolve()
    if bundle_dir != root and root not in bundle_dir.parents:
        raise HTTPException(status_code=404, detail="tour_not_found")
    if not bundle_dir.exists() or not bundle_dir.is_dir():
        raise HTTPException(status_code=404, detail="tour_not_found")
    return bundle_dir


def _tour_path(slug: str) -> Path:
    safe = str(slug or "").strip()
    if not safe or "/" in safe or ".." in safe:
        raise HTTPException(status_code=404, detail="tour_not_found")
    try:
        bundle_dir = _resolved_tour_bundle(slug)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        bundle_dir = None
    if bundle_dir is not None:
        bundle_manifest = bundle_dir / "tour.json"
        if bundle_manifest.exists():
            return bundle_manifest
    root = _resolved_tour_root()
    candidate = (root / f"{safe}.json").resolve()
    if root not in candidate.parents:
        raise HTTPException(status_code=404, detail="tour_not_found")
    return candidate


def _tour_bundle_dir(slug: str) -> Path | None:
    try:
        return _resolved_tour_bundle(slug)
    except HTTPException:
        return None


def _load_tour(slug: str) -> dict[str, object]:
    path = _tour_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="tour_not_found")
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        raise HTTPException(status_code=500, detail="tour_payload_invalid") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="tour_payload_invalid")
    return payload


def _load_private_tour_receipt(slug: str) -> dict[str, object]:
    bundle_dir = _tour_bundle_dir(slug)
    if bundle_dir is None:
        return {}
    private_manifest_path = bundle_dir / "tour.private.json"
    if not private_manifest_path.exists():
        return {}
    try:
        require_bounded_file(
            private_manifest_path,
            reason_prefix="private_tour_receipt",
            maximum_bytes=tour_manifest_max_bytes(),
        )
        private_payload = json.loads(private_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TourHostSafetyError) as exc:
        raise HTTPException(status_code=500, detail="tour_payload_invalid") from exc
    return dict(private_payload) if isinstance(private_payload, dict) else {}


def _load_tour_with_private_receipt(slug: str) -> dict[str, object]:
    payload = _load_tour(slug)
    private_payload = _load_private_tour_receipt(slug)
    if not private_payload:
        return payload
    safe_private_payload = {
        key: value
        for key, value in private_payload.items()
        if key in _PRIVATE_TOUR_RECEIPT_ALLOWED_KEYS
    }
    return {**payload, **safe_private_payload}



def _public_tour_key_is_private(key: object) -> bool:
    return _payload_public_tour_key_is_private(key)


def _public_tour_safe_asset_relpath(value: object) -> str:
    return _payload_public_tour_safe_asset_relpath(value)


def _public_tour_env_truthy(raw: object) -> bool:
    return _payload_public_tour_env_truthy(raw)


def _public_tour_prod_mode_enabled() -> bool:
    return str(os.getenv("EA_RUNTIME_MODE") or "").strip().lower() == "prod"


def _public_tour_privacy_mode(payload: dict[str, object]) -> str:
    return _payload_public_tour_privacy_mode(payload)


def _require_public_tour_viewable(payload: dict[str, object]) -> None:
    _payload_require_public_tour_viewable(payload)


def _public_tour_exact_address_allowed(payload: dict[str, object], *, privacy_mode: str) -> bool:
    return _payload_public_tour_exact_address_allowed(payload, privacy_mode=privacy_mode)


def _public_tour_asset_path_is_public(
    relpath: str,
    *,
    privacy_class: str = "",
    role: str = "",
    mime_type: str = "",
) -> bool:
    safe_relpath = _public_tour_safe_asset_relpath(relpath)
    if not safe_relpath:
        return False
    suffix = PurePosixPath(safe_relpath).suffix.lower()
    if suffix in _PUBLIC_TOUR_DENIED_ASSET_EXTENSIONS:
        return False
    if suffix not in _PUBLIC_TOUR_ALLOWED_ASSET_EXTENSIONS:
        return False
    if suffix == ".pdf" or "pdf" in str(mime_type or "").strip().lower():
        normalized_privacy = str(privacy_class or "").strip().lower()
        normalized_role = str(role or "").strip().lower().replace("-", "_")
        return normalized_privacy in _PUBLIC_TOUR_PUBLIC_PDF_PRIVACY_CLASSES and normalized_role in {
            "floorplan",
            "floor_plan",
            "layout",
            "valuation_floorplan",
        }
    return True


def _public_tour_collect_asset_refs(payload: dict[str, object]) -> set[str]:
    return set(_payload_public_tour_allowed_asset_paths(payload))


def _public_tour_allowed_asset_paths(payload: dict[str, object]) -> set[str]:
    return _payload_public_tour_allowed_asset_paths(payload)


def _public_tour_asset_metadata(payload: dict[str, object]) -> dict[str, dict[str, str]]:
    return _payload_public_tour_asset_metadata(payload)


def _public_tour_manifest(payload: dict[str, object], *, only_relpath: str = "") -> dict[str, dict[str, object]]:
    return _payload_public_tour_manifest(
        payload,
        only_relpath=only_relpath,
        bundle_dir_resolver=_tour_bundle_dir,
    )


def _public_tour_file_url(slug: str, relpath: str) -> str:
    return _payload_public_tour_file_url(slug, relpath)


def _public_tour_safe_http_url(value: object) -> str:
    return _payload_public_tour_safe_http_url(value)


def _public_tour_safe_navigation_url(value: object, *, allow_fragment: bool = False) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 4096:
        return ""
    if allow_fragment and re.fullmatch(r"#[A-Za-z][A-Za-z0-9_-]{0,127}", normalized):
        return normalized
    if normalized.startswith("/") and not normalized.startswith("//"):
        if "\\" in normalized or any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            return ""
        parsed = urllib.parse.urlsplit(normalized)
        if parsed.scheme or parsed.netloc:
            return ""
        return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))
    return _public_tour_safe_http_url(normalized)


def _public_tour_script_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _public_tour_normalize_host_pattern(value: object) -> str:
    normalized = str(value or "").strip().lower().rstrip(".")
    wildcard = normalized.startswith("*.")
    hostname = normalized[2:] if wildcard else normalized
    if (
        not hostname
        or len(hostname) > 253
        or ".." in hostname
        or not re.fullmatch(r"[a-z0-9.-]+", hostname)
        or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in hostname.split("."))
    ):
        return ""
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return "" if wildcard else hostname
    return f"*.{hostname}" if wildcard else hostname


def _public_tour_static_media_allowed_hosts() -> tuple[str, ...]:
    raw = str(os.getenv("PROPERTYQUARRY_PUBLIC_MEDIA_ALLOWED_HOSTS") or "").strip()
    candidates = raw.split(",") if raw else _PUBLIC_TOUR_DEFAULT_EXTERNAL_MEDIA_HOSTS
    hosts = tuple(
        normalized
        for item in candidates
        if (normalized := _public_tour_normalize_host_pattern(item))
    )
    if hosts:
        return tuple(dict.fromkeys(hosts))
    return tuple(
        normalized
        for item in _PUBLIC_TOUR_DEFAULT_EXTERNAL_MEDIA_HOSTS
        if (normalized := _public_tour_normalize_host_pattern(item))
    )


def _public_tour_live_360_allowed_hosts() -> tuple[str, ...]:
    raw = str(
        os.getenv(
            "PROPERTYQUARRY_PUBLIC_360_ALLOWED_HOSTS",
            "propertyquarry.com,*.propertyquarry.com,3dvista.com,*.3dvista.com,360.kalandra.at",
        )
        or ""
    ).strip()
    return tuple(
        dict.fromkeys(
            normalized
            for item in raw.split(",")
            if (normalized := _public_tour_normalize_host_pattern(item))
        )
    )


def _public_tour_external_csp_origins() -> tuple[str, ...]:
    hosts = tuple(dict.fromkeys((*_public_tour_static_media_allowed_hosts(), *_public_tour_live_360_allowed_hosts())))
    return tuple(f"https://{hostname}" for hostname in hosts)


def _public_tour_hostname_matches_allowed_pattern(hostname: str, pattern: str) -> bool:
    normalized_host = str(hostname or "").strip().lower().rstrip(".")
    normalized_pattern = str(pattern or "").strip().lower().rstrip(".")
    if not normalized_host or not normalized_pattern:
        return False
    if normalized_pattern.startswith("*."):
        suffix = normalized_pattern[1:]
        return normalized_host.endswith(suffix) and normalized_host != suffix.lstrip(".")
    return normalized_host == normalized_pattern


def _public_tour_static_media_url_allowed(value: object) -> bool:
    normalized = _public_tour_safe_http_url(value)
    if not normalized:
        return False
    parsed = urllib.parse.urlparse(normalized)
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    return any(
        _public_tour_hostname_matches_allowed_pattern(host, pattern)
        for pattern in _public_tour_static_media_allowed_hosts()
    )


def _public_tour_external_media_url_allowed(value: object) -> bool:
    return _payload_public_tour_external_media_url_allowed(
        value,
        url_allowed=_public_tour_static_media_url_allowed,
    )


def _redact_public_tour_value(value: object) -> object:
    return _payload_redact_public_tour_value(value)


def _redacted_public_tour_facts(
    payload: dict[str, object],
    facts: dict[str, object],
    *,
    privacy_mode: str,
) -> dict[str, object]:
    return _payload_redacted_public_tour_facts(payload, facts, privacy_mode=privacy_mode)


def _redacted_public_tour_scenes(
    payload: dict[str, object],
    *,
    expose_asset_relpaths: bool,
) -> list[dict[str, object]]:
    return _payload_redacted_public_tour_scenes(
        payload,
        expose_asset_relpaths=expose_asset_relpaths,
        url_allowed=_public_tour_static_media_url_allowed,
    )


def _redacted_public_tour_payload(
    payload: dict[str, object],
    *,
    expose_asset_relpaths: bool = False,
    include_external_tour_urls: bool = True,
) -> dict[str, object]:
    rendered = _payload_redacted_public_tour_payload(
        payload,
        expose_asset_relpaths=expose_asset_relpaths,
        url_allowed=_public_tour_static_media_url_allowed,
        bundle_dir_resolver=_tour_bundle_dir,
    )
    slug = str(rendered.get("slug") or payload.get("slug") or "").strip()
    if include_external_tour_urls:
        for key in ("source_virtual_tour_url", "source_virtual_tour_origin"):
            safe_url = _safe_live_360_url(payload.get(key))
            if safe_url:
                rendered[key] = safe_url
        for key in ("matterport_url", "three_d_vista_url", "threedvista_url", "3dvista_url", "crezlo_public_url"):
            raw_url = payload.get(key)
            safe_url = _safe_matterport_external_url(raw_url) or _safe_3dvista_external_url(raw_url) or _safe_live_360_url(raw_url)
            if safe_url:
                rendered[key] = safe_url
        if (rendered.get("source_virtual_tour_url") or rendered.get("source_virtual_tour_origin")) and payload.get("panorama_source"):
            rendered["panorama_source"] = str(payload.get("panorama_source") or "").strip()[:120]
        if _3dvista_browser_render_proof_ready(payload):
            # Carry only the public readiness result into the HTML projection;
            # private browser evidence remains outside the public manifest.
            rendered["three_d_vista_browser_render_proof"] = {
                "provider": "3dvista",
                "status": "pass",
                "rendered_viewer": True,
            }
    if slug and _3dvista_private_viewer_proof_ready(payload, slug=slug):
        for key in ("three_d_vista_entry_relpath", "threedvista_entry_relpath", "3dvista_entry_relpath"):
            relpath = _public_tour_safe_asset_relpath(str(payload.get(key) or "").strip())
            if relpath and _local_tour_asset_path(slug, relpath) is not None:
                rendered["three_d_vista_entry_relpath"] = relpath
                rendered["three_d_vista_import"] = {"source_project": "propertyquarry"}
                rendered["three_d_vista_white_label_proof"] = {
                    "source_project": "propertyquarry",
                    "private_viewer_verified": True,
                    "non_trial_export_verified": True,
                    "propertyquarry_tour_metadata": True,
                    "trial_branding_checked": True,
                    "trial_branding_present": False,
                }
                break
    return rendered


def _asset_file(slug: str, asset_path: str) -> Path:
    payload = _load_tour(slug)
    _require_public_tour_viewable(payload)
    safe_relpath = _public_tour_safe_asset_relpath(asset_path)
    # Keep an explicit full-manifest pass in the route so release gates can prove
    # file serving is anchored to the manifest-backed allowlist rather than only
    # ad hoc path checks at the call site.
    _public_tour_manifest(payload)
    manifest = _public_tour_manifest(payload, only_relpath=safe_relpath)
    bundle_dir = _tour_bundle_dir(slug)
    if not safe_relpath or bundle_dir is None:
        raise HTTPException(status_code=404, detail="tour_file_not_found")
    if safe_relpath not in manifest:
        raise HTTPException(status_code=404, detail="tour_file_not_found")
    unresolved_candidate = bundle_dir
    for part in PurePosixPath(safe_relpath).parts:
        unresolved_candidate = unresolved_candidate / part
        if unresolved_candidate.is_symlink():
            raise HTTPException(status_code=404, detail="tour_file_not_found")
    candidate = unresolved_candidate.resolve()
    if bundle_dir.resolve() not in candidate.parents:
        raise HTTPException(status_code=404, detail="tour_file_not_found")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="tour_file_not_found")
    if candidate.suffix.lower() == ".pdf":
        max_bytes = max(int(os.getenv("PROPERTYQUARRY_PUBLIC_PDF_MAX_BYTES") or "15728640"), 1)
        try:
            if candidate.stat().st_size > max_bytes:
                raise HTTPException(status_code=404, detail="tour_file_not_found")
        except OSError as exc:
            raise HTTPException(status_code=404, detail="tour_file_not_found") from exc
    return candidate


def _pano2vr_export_file(slug: str, asset_path: str) -> Path:
    payload = _load_tour_with_private_receipt(slug)
    _require_public_tour_viewable(payload)
    if _tour_payload_is_disabled_fallback(payload):
        raise HTTPException(status_code=404, detail="tour_disabled_fallback")
    entry_relpath = _pano2vr_entry_relpath(payload)
    safe_relpath = _public_tour_safe_asset_relpath(asset_path)
    bundle_dir = _tour_bundle_dir(slug)
    if not entry_relpath or not safe_relpath or bundle_dir is None:
        raise HTTPException(status_code=404, detail="tour_pano2vr_file_not_found")
    if not _pano2vr_spatial_provenance_ready(payload, slug=slug):
        raise HTTPException(status_code=404, detail="tour_pano2vr_file_not_found")
    if not _local_tour_html_asset_has_marker(slug, entry_relpath, markers=_PANO2VR_EXPORT_MARKERS):
        raise HTTPException(status_code=404, detail="tour_pano2vr_file_not_found")
    suffix = PurePosixPath(safe_relpath).suffix.lower()
    if suffix not in _PANO2VR_EXPORT_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="tour_pano2vr_file_not_found")
    export_root = _pano2vr_export_root_relpath(payload)
    if export_root:
        allowed = safe_relpath == entry_relpath or safe_relpath.startswith(f"{export_root}/")
    else:
        allowed = safe_relpath == entry_relpath
    if not allowed:
        raise HTTPException(status_code=404, detail="tour_pano2vr_file_not_found")
    candidate = (bundle_dir / safe_relpath).resolve()
    resolved_bundle = bundle_dir.resolve()
    if candidate == resolved_bundle or resolved_bundle not in candidate.parents:
        raise HTTPException(status_code=404, detail="tour_pano2vr_file_not_found")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="tour_pano2vr_file_not_found")
    return candidate


def _3dvista_export_file(slug: str, asset_path: str) -> Path:
    payload = _load_tour_with_private_receipt(slug)
    _require_public_tour_viewable(payload)
    if _tour_payload_is_disabled_fallback(payload):
        raise HTTPException(status_code=404, detail="tour_disabled_fallback")
    safe_relpath = _public_tour_safe_asset_relpath(asset_path)
    bundle_dir = _tour_bundle_dir(slug)
    entries, _roots = _3dvista_export_allowed_relpaths(payload)
    if not entries or not safe_relpath or bundle_dir is None:
        raise HTTPException(status_code=404, detail="tour_3dvista_file_not_found")
    if PurePosixPath(safe_relpath).name in THREE_D_VISTA_PROVENANCE_FILENAMES:
        raise HTTPException(status_code=404, detail="tour_3dvista_file_not_found")
    verified_entries = {
        entry_relpath
        for entry_relpath in entries
        if _3dvista_entry_export_ready(slug, payload, entry_relpath)
    }
    if not verified_entries:
        raise HTTPException(status_code=404, detail="tour_3dvista_file_not_found")
    suffix = PurePosixPath(safe_relpath).suffix.lower()
    if suffix not in _3DVISTA_EXPORT_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="tour_3dvista_file_not_found")
    verified_roots: set[str] = set()
    for entry_relpath in verified_entries:
        parent = str(PurePosixPath(entry_relpath).parent)
        if parent and parent != ".":
            verified_roots.add(parent.rstrip("/"))
    allowed = safe_relpath in verified_entries or any(safe_relpath.startswith(f"{root}/") for root in verified_roots)
    if not allowed:
        raise HTTPException(status_code=404, detail="tour_3dvista_file_not_found")
    candidate = (bundle_dir / safe_relpath).resolve()
    resolved_bundle = bundle_dir.resolve()
    if candidate == resolved_bundle or resolved_bundle not in candidate.parents:
        raise HTTPException(status_code=404, detail="tour_3dvista_file_not_found")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="tour_3dvista_file_not_found")
    return candidate


def _public_tour_currency_code(facts: dict[str, object] | None = None) -> str:
    normalized_facts = dict(facts or {})
    supported = set(supported_currency_codes())
    for key in ("price_currency", "currency_code", "currency"):
        currency = str(normalized_facts.get(key) or "").strip().upper()
        if currency in supported:
            return currency
    country_code = str(normalized_facts.get("country_code") or normalized_facts.get("market_country_code") or "").strip()
    if country_code:
        return currency_code_for_country(country_code)
    return "EUR"


def _money(value: object, *, currency_code: object = "EUR") -> str:
    currency = str(currency_code or "").strip().upper()
    if currency not in set(supported_currency_codes()):
        currency = "EUR"
    if isinstance(value, (int, float)):
        return f"{currency} {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{currency} ?"


def _safe_live_360_url(value: object) -> str:
    normalized = _public_tour_safe_http_url(value)
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if host == "matterport.com" or host.endswith(".matterport.com"):
        return ""
    allowed = _public_tour_live_360_allowed_hosts()
    if not allowed:
        return ""
    if not any(_public_tour_hostname_matches_allowed_pattern(host, item) for item in allowed):
        return ""
    return normalized


def _embedded_live_360_url(payload: dict[str, object]) -> str:
    normalized = dict(payload or {})
    if str(normalized.get("scene_strategy") or "").strip() == "pure_360_cube":
        return ""
    live_url = _safe_live_360_url(
        normalized.get("source_virtual_tour_url")
        or normalized.get("source_virtual_tour_origin")
    )
    # Matterport is no longer an active PropertyQuarry delivery lane. Keep a
    # static preview scene when one is present, but never embed its retired
    # viewer as the public tour experience.
    if _safe_matterport_external_url(live_url):
        return ""
    if _safe_3dvista_external_url(live_url) and not _3dvista_browser_render_proof_ready(normalized):
        return ""
    return live_url


def _merged_facts_with_listing_research(payload: dict[str, object], facts: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    merged = dict(facts)
    stored_research = dict(facts.get("listing_research_snapshot") or {}) if isinstance(facts.get("listing_research_snapshot"), dict) else {}
    research = stored_research
    if not research:
        return merged, {}
    for key, value in research.items():
        existing = merged.get(key)
        if _fact_value_is_weak(existing):
            merged[key] = value
    return merged, research


def _tour_payload_is_disabled_fallback(payload: dict[str, object]) -> bool:
    normalized = dict(payload or {})
    scene_strategy = str(normalized.get("scene_strategy") or "").strip().lower()
    creation_mode = str(normalized.get("creation_mode") or "").strip().lower()
    control_mode = str(normalized.get("control_mode") or "").strip().lower()
    if scene_strategy in {"generated_listing_summary", "photo_gallery_hosted", "floorplan_hosted", "pure_360_cube"}:
        return True
    if creation_mode == "hosted_listing_fallback":
        return True
    if control_mode in {"walkable_3d", "internal_walkable_3d"}:
        return True
    scenes = [dict(row) for row in (normalized.get("scenes") or []) if isinstance(row, dict)]
    if any(str(scene.get("role") or "").strip() == "generated_overview" for scene in scenes):
        return True
    return False


def _public_tour_rate_limit_dir() -> Path | None:
    raw = (
        os.environ.get("PROPERTYQUARRY_PUBLIC_RATE_LIMIT_DIR")
        or os.environ.get("EA_PUBLIC_RATE_LIMIT_DIR")
        or ""
    ).strip()
    if raw:
        return Path(raw).expanduser()
    ledger_dir = str(os.environ.get("EA_RESPONSES_PROVIDER_LEDGER_DIR") or "").strip()
    if ledger_dir:
        return Path(ledger_dir).expanduser() / "public-tour-rate-limits"
    return None


def _safe_public_tour_ip(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("[") and "]" in raw:
        raw = raw[1 : raw.find("]")]
    elif raw.count(":") == 1 and "." in raw:
        raw = raw.rsplit(":", 1)[0]
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return ""


def _public_tour_trust_x_forwarded_for() -> bool:
    raw = str(os.environ.get("PROPERTYQUARRY_TRUST_X_FORWARDED_FOR") or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "y"}


def _public_tour_client_identity(request: Request) -> str:
    cf_ip = _safe_public_tour_ip(request.headers.get("cf-connecting-ip"))
    if cf_ip and str(request.headers.get("cf-ray") or "").strip():
        return f"cf:{cf_ip}"
    if _public_tour_trust_x_forwarded_for():
        for part in str(request.headers.get("x-forwarded-for") or "").split(","):
            forwarded_ip = _safe_public_tour_ip(part)
            if forwarded_ip:
                return f"xff:{forwarded_ip}"
    client_ip = _safe_public_tour_ip(getattr(getattr(request, "client", None), "host", "") or "")
    return f"client:{client_ip or 'unknown'}"


def _public_tour_feedback_rate_limit_key(*, request: Request, slug: str, principal_id: str) -> str:
    identity = _public_tour_client_identity(request)
    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    principal_hash = hashlib.sha256(str(principal_id or "").encode("utf-8")).hexdigest()[:16]
    slug_hash = hashlib.sha256(str(slug or "").encode("utf-8")).hexdigest()[:16]
    return f"{slug_hash}:{principal_hash}:{identity_hash}"


def _prune_public_tour_feedback_memory_rate_limit(now: float) -> None:
    expired = [
        key
        for key, (window_started, _count) in _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT.items()
        if now - float(window_started) > _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_WINDOW_SECONDS
    ]
    for key in expired:
        _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT.pop(key, None)
    while len(_PUBLIC_TOUR_FEEDBACK_RATE_LIMIT) > _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_MAX_KEYS:
        _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT.popitem(last=False)


def _enforce_public_tour_feedback_memory_rate_limit(*, key: str, now: float) -> None:
    _prune_public_tour_feedback_memory_rate_limit(now)
    window_started, count = _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT.get(key, (now, 0))
    if now - float(window_started) > _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_WINDOW_SECONDS:
        _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT[key] = (now, 1)
        return
    if int(count) >= _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="public_tour_feedback_rate_limited")
    _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT[key] = (float(window_started), int(count) + 1)
    _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT.move_to_end(key)


def _prune_public_tour_feedback_file_rate_limit(rate_dir: Path, *, now: float) -> None:
    try:
        files = sorted(rate_dir.glob("*.json"), key=lambda item: item.stat().st_mtime)
    except OSError:
        return
    stale_before = now - (_PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_WINDOW_SECONDS * 4)
    for path in files:
        try:
            if path.stat().st_mtime < stale_before:
                path.unlink(missing_ok=True)
        except OSError:
            continue
    if len(files) <= _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_MAX_KEYS:
        return
    for path in files[: max(0, len(files) - _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_MAX_KEYS)]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


def _enforce_public_tour_feedback_file_rate_limit(*, key: str, now: float) -> bool:
    rate_dir = _public_tour_rate_limit_dir()
    if rate_dir is None:
        return False
    try:
        rate_dir.mkdir(parents=True, exist_ok=True)
        lock_path = rate_dir / ".feedback-rate-limit.lock"
        state_path = rate_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            except Exception:
                state = {}
            try:
                window_started = float(state.get("window_started") or now)
            except Exception:
                window_started = now
            try:
                count = int(state.get("count") or 0)
            except Exception:
                count = 0
            if now - window_started > _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_WINDOW_SECONDS:
                window_started = now
                count = 0
            if count >= _PUBLIC_TOUR_FEEDBACK_RATE_LIMIT_MAX:
                raise HTTPException(status_code=429, detail="public_tour_feedback_rate_limited")
            tmp_path = state_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps({"window_started": window_started, "count": count + 1}, sort_keys=True),
                encoding="utf-8",
            )
            tmp_path.replace(state_path)
            _prune_public_tour_feedback_file_rate_limit(rate_dir, now=now)
        return True
    except HTTPException:
        raise
    except Exception:
        log.exception("public tour feedback durable rate limit failed")
        if _public_tour_env_truthy(os.getenv("PROPERTYQUARRY_PUBLIC_RATE_LIMIT_FAIL_CLOSED")) or _public_tour_prod_mode_enabled():
            raise HTTPException(status_code=503, detail="public_tour_feedback_rate_limit_unavailable")
        return False


def _enforce_public_tour_feedback_rate_limit(*, request: Request, slug: str, principal_id: str) -> None:
    now = time.time()
    key = _public_tour_feedback_rate_limit_key(request=request, slug=slug, principal_id=principal_id)
    if _enforce_public_tour_feedback_file_rate_limit(key=key, now=now):
        return
    _enforce_public_tour_feedback_memory_rate_limit(key=key, now=now)


def _public_tour_authenticated_action_required(action: str) -> HTTPException:
    normalized_action = str(action or "").strip()
    if normalized_action not in _PUBLIC_TOUR_ACTIONS:
        normalized_action = "public-tour-action"
    return HTTPException(status_code=403, detail=f"{normalized_action}_requires_authenticated_account")


def _feedback_reason_label(reason_key: object) -> str:
    reason_map = _property_feedback_reason_map()
    row = dict(reason_map.get(str(reason_key or "").strip(), {}))
    return str(row.get("label") or reason_key or "").strip()


def _preference_snapshot_nodes(facts: dict[str, object]) -> list[dict[str, object]]:
    snapshot = dict(facts.get("public_preference_snapshot") or {}) if isinstance(facts.get("public_preference_snapshot"), dict) else {}
    return [dict(row) for row in list(snapshot.get("preference_nodes") or []) if isinstance(row, dict)]


def _filter_node_active(nodes: list[dict[str, object]], *, key: str, category: str) -> bool:
    normalized_key = str(key or "").strip().lower()
    for row in nodes:
        row_key = str(row.get("key") or "").strip().lower()
        if row_key != normalized_key:
            continue
        if str(row.get("category") or "").strip().lower() != category.lower():
            continue
        if str(row.get("status") or "active").strip().lower() == "inactive":
            continue
        value = row.get("value_json")
        if isinstance(value, bool):
            return bool(value)
        if isinstance(value, list):
            return any(str(item or "").strip() for item in value)
        return value not in (None, "", 0, 0.0)
    return False


def _public_filter_specs(*, facts: dict[str, object]) -> list[dict[str, object]]:
    district_value = str(facts.get("postal_name") or facts.get("district") or "").strip()
    filters: list[dict[str, object]] = [
        {
            "key": "avoid_gas_heating",
            "label": "Avoid gas heating",
            "summary": "Suppress listings with gas-based heating.",
            "domain": "willhaben",
            "category": "aversion",
            "node_key": "avoid_heating_types",
            "value_json": ["Gasheizung", "Hauszentralheizung (Gas)"],
            "strength": "high",
            "confidence": 0.95,
        },
        {
            "key": "require_lift",
            "label": "Require lift",
            "summary": "Rank lift-access properties higher.",
            "domain": "willhaben",
            "category": "soft_preference",
            "node_key": "prefer_lift",
            "value_json": True,
            "strength": "high",
            "confidence": 0.92,
        },
        {
            "key": "require_floorplan",
            "label": "Require floor plan",
            "summary": "Prefer listings with a usable layout plan.",
            "domain": "willhaben",
            "category": "soft_preference",
            "node_key": "requires_floorplan_for_remote_review",
            "value_json": True,
            "strength": "high",
            "confidence": 0.9,
        },
        {
            "key": "prefer_subway_nearby",
            "label": "Prefer underground nearby",
            "summary": "Bias ranking toward strong underground access.",
            "domain": "willhaben",
            "category": "soft_preference",
            "node_key": "prefer_subway_nearby",
            "value_json": True,
            "strength": "medium",
            "confidence": 0.84,
        },
        {
            "key": "prefer_supermarket_nearby",
            "label": "Prefer supermarket nearby",
            "summary": "Bias ranking toward easier daily shopping.",
            "domain": "willhaben",
            "category": "soft_preference",
            "node_key": "prefer_supermarket_nearby",
            "value_json": True,
            "strength": "medium",
            "confidence": 0.82,
        },
        {
            "key": "prefer_playgrounds_nearby",
            "label": "Prefer playground nearby",
            "summary": "Bias ranking toward family-oriented micro-locations.",
            "domain": "willhaben",
            "category": "soft_preference",
            "node_key": "prefer_playgrounds_nearby",
            "value_json": True,
            "strength": "medium",
            "confidence": 0.82,
        },
        {
            "key": "prefer_unlimited_lease",
            "label": "Prefer unlimited lease",
            "summary": "Penalize limited-term leases.",
            "domain": "willhaben",
            "category": "soft_preference",
            "node_key": "prefer_unlimited_lease",
            "value_json": True,
            "strength": "high",
            "confidence": 0.9,
        },
        {
            "key": "prefer_balcony",
            "label": "Prefer balcony or terrace",
            "summary": "Bias ranking toward private outdoor space.",
            "domain": "willhaben",
            "category": "soft_preference",
            "node_key": "prefer_balcony",
            "value_json": True,
            "strength": "medium",
            "confidence": 0.8,
        },
    ]
    if district_value:
        filters.append(
            {
                "key": "prefer_this_area",
                "label": f"Prefer {district_value}",
                "summary": "Bias future ranking toward this area.",
                "domain": "willhaben",
                "category": "soft_preference",
                "node_key": "preferred_areas",
                "value_json": [district_value],
                "strength": "medium",
                "confidence": 0.88,
            }
        )
    return filters


def _filter_panel_context(*, facts: dict[str, object]) -> dict[str, object]:
    nodes = _preference_snapshot_nodes(facts)
    filters: list[dict[str, object]] = []
    active_labels: list[str] = []
    hard_filters: list[dict[str, object]] = []
    soft_filters: list[dict[str, object]] = []
    for spec in _public_filter_specs(facts=facts):
        active = _filter_node_active(nodes, key=str(spec.get("node_key") or ""), category=str(spec.get("category") or ""))
        enriched = {**spec, "active": active}
        filters.append(enriched)
        if str(spec.get("category") or "").strip().lower() == "aversion" or str(spec.get("strength") or "").strip().lower() == "high":
            hard_filters.append(enriched)
        else:
            soft_filters.append(enriched)
        if active:
            active_labels.append(str(spec.get("label") or "").strip())
    return {
        "filters": filters,
        "hard_filters": hard_filters,
        "soft_filters": soft_filters,
        "active_labels": active_labels[:8],
    }


def _shortlist_normalized_ref_tokens(value: object) -> tuple[str, ...]:
    raw = str(value or "").strip()
    if not raw:
        return ()
    normalized = raw.lower().strip()
    tokens: set[str] = {normalized}
    if "://" in normalized:
        normalized_url = str(urllib.parse.urldefrag(normalized)[0]).strip()
        if normalized_url:
            tokens.add(normalized_url)
            tokens.add(normalized_url.rstrip("/"))
            parsed = urllib.parse.urlparse(normalized_url)
            path = (parsed.path or "").rstrip("/")
            if path:
                tokens.add(path.rsplit("/", 1)[-1].lower())
    if ":" in normalized and not normalized.startswith("http"):
        tokens.add(normalized.split(":", 1)[-1].strip())
    maybe_id_match = re.search(r"(\d{4,})", normalized)
    if maybe_id_match:
        tokens.add(maybe_id_match.group(1))
    tail = normalized.rsplit("/", 1)[-1]
    if tail:
        tokens.add(tail)
    return tuple(sorted(token for token in tokens if token and token.lower() not in {"http", "https", "www"}))


def _shortlist_as_float(value: object) -> float | None:
    try:
        if isinstance(value, (int, float)):
            return float(value)
        normalized = (
            str(value)
            .replace("€", "")
            .replace(" ", "")
            .replace("m²", "")
            .replace("sqm", "")
            .replace("m", "")
        )
        for currency_code in supported_currency_codes():
            normalized = re.sub(rf"\b{re.escape(currency_code)}\b", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"[^0-9.,\-]", "", normalized)
        if not normalized or normalized in {"-", "+", ".", ","}:
            return None
        if "," in normalized and "." in normalized:
            if normalized.rfind(",") > normalized.rfind("."):
                normalized = normalized.replace(".", "").replace(",", ".")
            else:
                normalized = normalized.replace(",", "")
            return float(normalized)
        if "," in normalized:
            before, after = normalized.rsplit(",", 1)
            if before and after and len(after) in {1, 2}:
                normalized = f"{before}.{after}"
            else:
                normalized = normalized.replace(",", "")
            return float(normalized)
        if "." in normalized:
            before, after = normalized.rsplit(".", 1)
            if before and after and len(after) == 3:
                normalized = normalized.replace(".", "")
            else:
                normalized = normalized
            return float(normalized)
        return float(normalized)
    except Exception:
        return None


def _shortlist_safe_http_url(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return normalized


def _shortlist_as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "y", "ja", "on", "enabled", "present"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "disabled", "missing", "none", "n/a"}:
        return False
    return None


def _public_tour_normalize_reason_keys(value: object, *, allowed: set[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="invalid_tour_feedback_reason_keys")
    keys: list[str] = []
    seen: set[str] = set()
    for row in value:
        key = str(row or "").strip().lower()
        if not key:
            continue
        if key not in allowed:
            raise HTTPException(status_code=422, detail="invalid_tour_feedback_reason_key")
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return tuple(keys)


@lru_cache(maxsize=8)
def _shortlist_tour_manifest_index(root: str) -> tuple[dict[str, object], ...]:
    root_dir = Path(root).expanduser()
    if not root_dir.exists() or not root_dir.is_dir():
        return ()
    rows: list[dict[str, object]] = []
    for candidate in sorted((entry for entry in root_dir.iterdir() if entry.is_dir()), key=lambda entry: entry.name):
        payload_path = candidate / "tour.json"
        if not payload_path.exists() or not payload_path.is_file():
            continue
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            rows.append(dict(payload))
    return tuple(rows)


def _shortlist_tour_match_tokens(payload: dict[str, object]) -> tuple[str, ...]:
    tokens: set[str] = set()
    facts = dict(payload.get("facts") or {})
    runtime_inputs = dict(payload.get("runtime_inputs_json") or {})
    for candidate in (
        payload.get("listing_url"),
        payload.get("property_url"),
        payload.get("source_ref"),
        payload.get("external_id"),
        payload.get("tour_slug"),
        payload.get("slug"),
        runtime_inputs.get("listing_id"),
        facts.get("listing_id"),
    ):
        tokens.update(_shortlist_normalized_ref_tokens(candidate))
    return tuple(sorted(tokens))


def _shortlist_find_tour_payload_for_refs(*, refs: tuple[str, ...]) -> dict[str, object] | None:
    if not refs:
        return None
    ref_set = set(refs)
    for candidate in _shortlist_tour_manifest_index(str(_tour_dir())):
        candidate_tokens = set(_shortlist_tour_match_tokens(candidate))
        if ref_set & candidate_tokens:
            return dict(candidate)
    return None


def _shortlist_tour_row_metrics(payload: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {
            "total_rent_eur": None,
            "area_sqm": None,
            "rooms": None,
            "heating_type": "",
            "lift": None,
            "has_floorplan": None,
            "has_balcony": None,
            "nearest_subway_m": None,
            "nearest_supermarket_m": None,
            "nearest_playground_m": None,
        }
    facts = dict(payload.get("facts") or {})
    rent_keys = ("total_rent_eur", "rent_eur", "price_eur", "price", "base_rent_eur")
    area_keys = ("area_sqm", "area", "living_area", "living_area_sqm", "floor_area")
    room_keys = ("rooms", "room_count", "zimmer")
    lift_keys = ("lift", "has_lift", "elevator")
    floorplan_keys = ("has_floorplan", "requires_floorplan_for_remote_review", "floorplan_available", "floor_plan")
    balcony_keys = ("has_balcony", "has_terrace", "balcony", "terrace", "outdoor_space")
    distance_specs = (
        ("nearest_subway_m", ("nearest_subway_m", "distance_to_subway_m", "subway_distance_m")),
        ("nearest_supermarket_m", ("nearest_supermarket_m", "distance_to_supermarket_m", "supermarket_distance_m")),
        (
            "nearest_playground_m",
            ("nearest_playground_m", "distance_to_playground_m", "playground_distance_m"),
        ),
    )

    def _first_numeric(candidate_keys: tuple[str, ...]) -> float | None:
        for key in candidate_keys:
            value = _shortlist_as_float(facts.get(key))
            if value is not None and value > 0:
                return value
        return None

    def _first_bool(candidate_keys: tuple[str, ...]) -> bool | None:
        for key in candidate_keys:
            value = _shortlist_as_bool(facts.get(key))
            if value is not None:
                return value
        return None

    rent_value = _first_numeric(rent_keys)
    area_value = _first_numeric(area_keys)
    room_value = _first_numeric(room_keys)
    heating_value = str(facts.get("heating_type") or facts.get("heating") or "").strip()
    metrics = {
        "total_rent_eur": rent_value,
        "area_sqm": area_value,
        "rooms": int(room_value) if room_value is not None and room_value > 0 else None,
        "heating_type": heating_value,
        "lift": _first_bool(lift_keys),
        "has_floorplan": _first_bool(floorplan_keys),
        "has_balcony": _first_bool(balcony_keys),
    }
    for metric_key, options in distance_specs:
        metrics[metric_key] = _first_numeric(options)
    return metrics


def _shortlist_metric_labels() -> tuple[tuple[str, str, str], ...]:
    return (
        ("total_rent_eur", "Rent", "higher_is_worse"),
        ("area_sqm", "Area", "higher_is_better"),
        ("rooms", "Rooms", "higher_is_better"),
        ("heating_type", "Heating", "compare_text"),
        ("lift", "Lift", "higher_is_better"),
        ("has_floorplan", "Floor plan", "higher_is_better"),
        ("has_balcony", "Balcony/Terrace", "higher_is_better"),
        ("nearest_subway_m", "Underground", "higher_is_worse"),
        ("nearest_supermarket_m", "Supermarket", "higher_is_worse"),
        ("nearest_playground_m", "Playground", "higher_is_worse"),
    )


def _shortlist_metric_display(metric_key: str, value: object, *, currency_code: object = "EUR") -> str:
    if value is None:
        return "Not available"
    if metric_key == "total_rent_eur":
        if isinstance(value, (int, float)):
            return _money(value, currency_code=currency_code)
        return str(value)
    if metric_key == "area_sqm":
        if isinstance(value, (int, float)):
            return f"{int(round(float(value)))} m²"
    if metric_key == "rooms":
        if isinstance(value, (int, float)):
            return f"{int(round(float(value)))}"
    if metric_key.endswith("_m"):
        if isinstance(value, (int, float)) and value >= 0:
            return f"about {int(round(value))} m"
    if metric_key in {"lift", "has_floorplan", "has_balcony"}:
        return "Yes" if bool(value) else "No"
    return str(value or "Not available")


def _shortlist_metric_delta(metric_key: str, *, baseline: object, candidate: object, currency_code: object = "EUR") -> tuple[str, str]:
    if candidate is None or baseline is None:
        return "No ranking delta", "neutral"
    if metric_key.endswith("_m") or metric_key in {"total_rent_eur", "area_sqm", "rooms"}:
        if not isinstance(baseline, (int, float)) or not isinstance(candidate, (int, float)):
            return "No ranking delta", "neutral"
        base_value = float(baseline)
        cand_value = float(candidate)
        if base_value == 0:
            return "No ranking delta", "neutral"
        difference = cand_value - base_value
        if abs(difference) < 0.0001:
            return "No change", "neutral"
        ratio = int(round((difference / base_value) * 100.0)) if base_value else 0
        prefix = "+" if difference > 0 else "-"
        delta = abs(difference)
        if metric_key == "total_rent_eur":
            delta_text = f"{prefix}{_money(delta, currency_code=currency_code)}"
        elif metric_key in {"area_sqm", "rooms"}:
            delta_text = f"{prefix}{abs(difference):.0f}"
        else:
            delta_text = f"{prefix}{int(round(abs(difference)))} m"
        metric_is_lower_better = metric_key in {"total_rent_eur", "nearest_subway_m", "nearest_supermarket_m", "nearest_playground_m"}
        if (metric_is_lower_better and difference < 0) or (not metric_is_lower_better and difference > 0):
            return f"{delta_text} ({abs(ratio)}%)", "better"
        return f"{delta_text} ({abs(ratio)}%)", "worse"
    if metric_key in {"lift", "has_floorplan", "has_balcony"}:
        if bool(baseline) == bool(candidate):
            return "Same", "neutral"
        if bool(candidate) and not bool(baseline):
            return "Better", "better"
        return "Worse", "worse"
    baseline_text = str(baseline or "").strip().lower()
    candidate_text = str(candidate or "").strip().lower()
    if baseline_text == candidate_text:
        return "Same", "neutral"
    if candidate_text:
        return "Different", "neutral"
    return "No ranking delta", "neutral"


def _public_shortlist_action_label(value: object) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered == "compare against shortlist":
        return "Review ranked alternative"
    if lowered == "review property alert":
        return "Open ranked property"
    if lowered in {"review", "open", "open property"}:
        return "Open ranked property"
    return text or "Review ranking"


def _live_property_feedback_context(
    *,
    container: AppContainer,
    payload: dict[str, object],
    slug: str,
) -> dict[str, object]:
    def _merge_snapshot_nodes(
        stored_snapshot: dict[str, object],
        profile_bundle: dict[str, object],
    ) -> list[dict[str, object]]:
        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        for source in (
            list(dict(stored_snapshot or {}).get("preference_nodes") or []),
            list(dict(profile_bundle or {}).get("preference_nodes") or []),
        ):
            for row in source:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("key") or "").strip().lower()
                category = str(row.get("category") or "").strip().lower()
                value_marker = json.dumps(row.get("value_json"), sort_keys=True, ensure_ascii=False, default=str)
                marker = (key, category, value_marker)
                if not key or marker in seen:
                    continue
                seen.add(marker)
                merged.append(dict(row))
        return merged

    principal_id = str(payload.get("principal_id") or "").strip()
    facts = dict(payload.get("facts") or {})
    facts, _ = _merged_facts_with_listing_research(payload, facts)
    stored_snapshot = dict(facts.get("public_preference_snapshot") or {}) if isinstance(facts.get("public_preference_snapshot"), dict) else {}
    if not principal_id:
        return {"facts": facts, "feedback_suggestions": {"negative": [], "positive": []}, "learning_summary": {}}
    service = build_product_service(container)
    profile_bundle = service.get_preference_profile(principal_id=principal_id, person_id="self")
    listing_object_id = str(payload.get("listing_id") or payload.get("property_url") or payload.get("listing_url") or slug).strip() or slug
    existing_assessment = dict(facts.get("personal_fit_assessment") or {}) if isinstance(facts.get("personal_fit_assessment"), dict) else {}
    live_assessment = service.preview_preference_candidate(
        principal_id=principal_id,
        person_id="self",
        domain="willhaben",
        object_type="listing",
        object_id=listing_object_id,
        object_payload=facts,
        require_existing_profile=False,
    )
    if isinstance(live_assessment, dict):
        merged_assessment = dict(existing_assessment)
        merged_assessment.update(dict(live_assessment))
        existing_livability = dict(existing_assessment.get("livability_snapshot") or {}) if isinstance(existing_assessment.get("livability_snapshot"), dict) else {}
        live_livability = dict(live_assessment.get("livability_snapshot") or {}) if isinstance(live_assessment.get("livability_snapshot"), dict) else {}
        if existing_livability or live_livability:
            merged_livability = dict(existing_livability)
            merged_livability.update(live_livability)
            merged_assessment["livability_snapshot"] = merged_livability
        facts["personal_fit_assessment"] = merged_assessment
    facts["public_preference_snapshot"] = {
        "profile": dict(profile_bundle.get("profile") or stored_snapshot.get("profile") or {}),
        "preference_nodes": _merge_snapshot_nodes(stored_snapshot, profile_bundle),
    }
    return {
        "facts": facts,
        "feedback_suggestions": service.property_feedback_suggestions(
            property_facts=facts,
            assessment=dict(live_assessment or {}) if isinstance(live_assessment, dict) else None,
        ),
        "learning_summary": service.property_feedback_learning_summary(
            principal_id=principal_id,
            person_id="self",
            domain="willhaben",
        ),
        "live_assessment": dict(live_assessment or {}) if isinstance(live_assessment, dict) else {},
        "profile_bundle": profile_bundle,
    }


def _public_shortlist_comparison_context(
    *,
    container: AppContainer,
    payload: dict[str, object],
    slug: str,
    facts: dict[str, object],
) -> dict[str, object]:
    principal_id = str(payload.get("principal_id") or "").strip()
    if not principal_id:
        return {
            "current": {},
            "items": [],
            "metric_specs": list(_shortlist_metric_labels()),
        }
    metric_specs = list(_shortlist_metric_labels())
    current_refs = {
        str(payload.get("listing_id") or "").strip(),
        str(payload.get("property_url") or "").strip(),
        str(payload.get("listing_url") or "").strip(),
        str(slug or "").strip(),
    }
    current_payload = {
        "facts": dict(facts),
        "listing_url": payload.get("listing_url"),
        "property_url": payload.get("property_url"),
        "source_ref": payload.get("source_ref"),
        "external_id": payload.get("external_id"),
    }
    current_metrics = _shortlist_tour_row_metrics(current_payload)
    current_score_value = dict(facts.get("personal_fit_assessment") or {}).get("fit_score")
    current_score = float(current_score_value or 0.0) if isinstance(current_score_value, (int, float)) else 0.0
    current_title = str(payload.get("display_title") or payload.get("title") or slug).strip() or slug
    try:
        service = build_product_service(container)
        brief_items = list(service.list_brief_items(principal_id=principal_id, limit=8))
    except Exception:
        return {
            "current": {
                "title": current_title,
                "score": current_score,
                "object_ref": str(payload.get("listing_url") or payload.get("property_url") or slug).strip(),
                "listing_url": str(payload.get("listing_url") or payload.get("property_url") or "").strip(),
                "score_label": "Fit",
                "why_now": "",
                "recommended_action": "review current property",
                "metrics": current_metrics,
            },
            "items": [],
            "metric_specs": metric_specs,
        }
    items: list[dict[str, object]] = []
    if tuple(current_refs):
        normalized_current_refs = tuple(
            token
            for ref in current_refs
            for token in _shortlist_normalized_ref_tokens(ref)
            if token
        )
    else:
        normalized_current_refs = ()
    for row in brief_items:
        object_ref = str(getattr(row, "object_ref", "") or "").strip()
        object_ref_tokens = tuple(_shortlist_normalized_ref_tokens(object_ref))
        if any(token in object_ref_tokens for token in normalized_current_refs):
            continue
        if not object_ref_tokens:
            continue
        candidate_payload = _shortlist_find_tour_payload_for_refs(refs=object_ref_tokens)
        candidate_metrics = _shortlist_tour_row_metrics(candidate_payload)
        candidate_listing_url = str(
            (candidate_payload.get("listing_url") if isinstance(candidate_payload, dict) else "")
            or (candidate_payload.get("property_url") if isinstance(candidate_payload, dict) else "")
        ) if isinstance(candidate_payload, dict) else ""
        if not candidate_listing_url and object_ref.startswith("http"):
            candidate_listing_url = object_ref
        candidate_listing_url = _shortlist_safe_http_url(candidate_listing_url)
        items.append(
            {
                "title": str(getattr(row, "title", "") or "").strip() or "Shortlist property",
                "score": float(getattr(row, "score", 0.0) or 0.0),
                "why_now": str(getattr(row, "why_now", "") or "").strip(),
                "recommended_action": str(getattr(row, "recommended_action", "") or "").strip(),
                "object_ref": object_ref,
                "listing_url": candidate_listing_url,
                "metrics": candidate_metrics,
            }
        )
    current_reason = ""
    assessment = dict(facts.get("personal_fit_assessment") or {})
    for source in (list(assessment.get("good_fit_reasons") or []), list(assessment.get("match_reasons_json") or [])):
        for value in source:
            text = str(value or "").strip()
            if text:
                current_reason = text
                break
        if current_reason:
            break
    return {
        "current": {
            "title": current_title,
            "score": current_score,
            "why_now": current_reason or "Current hosted property under review.",
            "score_label": "Fit",
            "metrics": current_metrics,
            "recommended_action": "review current property",
            "object_ref": str(payload.get("listing_url") or payload.get("property_url") or slug).strip(),
            "listing_url": str(payload.get("listing_url") or payload.get("property_url") or "").strip(),
        },
        "items": items[:2],
        "metric_specs": metric_specs,
    }


def _public_tour_host_brand_label(hostname: str, *, fallback: str = "this domain") -> str:
    host = str(hostname or "").strip().lower().rstrip(".")
    if host.endswith("propertyquarry.com"):
        return "PropertyQuarry"
    if host.endswith("myexternalbrain.com"):
        return "My External Brain"
    return str(fallback or "this domain").strip() or "this domain"


def _public_tour_payload_needs_defensive_redaction(payload: dict[str, object]) -> bool:
    if any(
        key in payload
        for key in (
            "brief",
            "listing_url",
            "property_url",
            "principal_id",
            "recipient_email",
            "source_ref",
            "external_id",
        )
    ):
        return True
    facts = payload.get("facts")
    if isinstance(facts, dict):
        if any(str(key or "").strip() in _PUBLIC_TOUR_EXACT_LOCATION_FACT_KEYS for key in facts):
            return True
        if any(_public_tour_key_is_private(str(key or "")) for key in facts):
            return True
    for scene in list(payload.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        if any(_public_tour_key_is_private(str(key or "")) for key in scene):
            return True
        if any(str(key or "").strip() in {"source_url", "property_url", "listing_url"} for key in scene):
            return True
    return False


def _pano2vr_entry_relpath(payload: dict[str, object]) -> str:
    for key in ("pano2vr_entry_relpath", "pano2vr_export_entry_relpath"):
        relpath = _public_tour_safe_asset_relpath(str(payload.get(key) or "").strip())
        if relpath:
            return relpath
    return ""


def _3dvista_entry_relpath(payload: dict[str, object]) -> str:
    for key in ("three_d_vista_entry_relpath", "threedvista_entry_relpath", "3dvista_entry_relpath"):
        relpath = _public_tour_safe_asset_relpath(str(payload.get(key) or "").strip())
        if relpath:
            return relpath
    return ""


def _3dvista_export_root_relpath(payload: dict[str, object]) -> str:
    for key in ("three_d_vista_export_root_relpath", "threedvista_export_root_relpath", "3dvista_export_root_relpath"):
        relpath = _public_tour_safe_asset_relpath(str(payload.get(key) or "").strip())
        if relpath:
            return relpath.rstrip("/")
    entry_relpath = _3dvista_entry_relpath(payload)
    if not entry_relpath:
        return ""
    parent = str(PurePosixPath(entry_relpath).parent)
    return "" if parent == "." else parent.rstrip("/")


def _3dvista_export_layer_entry_relpaths(payload: dict[str, object]) -> list[str]:
    relpaths: list[str] = []
    slug = str(payload.get("slug") or "").strip()
    raw_layers = payload.get("tour_layers") or payload.get("provider_layers") or payload.get("interactive_layers")
    if not isinstance(raw_layers, list):
        return relpaths
    for row in raw_layers:
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or row.get("viewer_provider") or "").strip().lower()
        if provider not in {"3dvista", "3d_vista", "three_d_vista"}:
            continue
        provider_browser_ready = _3dvista_browser_render_proof_ready(row) or _3dvista_browser_render_proof_ready(payload)
        if not provider_browser_ready:
            continue
        relpath = _public_tour_safe_asset_relpath(
            str(
                row.get("three_d_vista_entry_relpath")
                or row.get("threedvista_entry_relpath")
                or row.get("3dvista_entry_relpath")
                or row.get("entry_relpath")
                or ""
            ).strip()
        )
        if slug and relpath and not _3dvista_entry_ready(slug, payload, relpath):
            continue
        if relpath and relpath not in relpaths:
            relpaths.append(relpath)
    return relpaths


def _3dvista_export_allowed_relpaths(payload: dict[str, object]) -> tuple[set[str], set[str]]:
    entries = {_3dvista_entry_relpath(payload)}
    roots = {_3dvista_export_root_relpath(payload)}
    for entry_relpath in _3dvista_export_layer_entry_relpaths(payload):
        entries.add(entry_relpath)
        parent = str(PurePosixPath(entry_relpath).parent)
        if parent and parent != ".":
            roots.add(parent.rstrip("/"))
    return {entry for entry in entries if entry}, {root.rstrip("/") for root in roots if root}


@lru_cache(maxsize=256)
def _3dvista_target_provenance_errors_cached(
    target_slug: str,
    bundle_dir_value: str,
    payload_json: str,
    maximum_files: int,
    maximum_total_bytes: int,
    maximum_file_bytes: int,
    _cache_bucket: int,
) -> tuple[str, ...]:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return ("receipt_invalid",)
    if not isinstance(payload, dict):
        return ("receipt_invalid",)
    raw_provenance = payload.get("three_d_vista_target_provenance")
    if not isinstance(raw_provenance, dict):
        return ("receipt_missing",)

    artifact = (
        dict(raw_provenance.get("artifact") or {})
        if isinstance(raw_provenance.get("artifact"), dict)
        else {}
    )
    evidence_kind = str(artifact.get("kind") or "").strip().lower()
    entry_relpath = _3dvista_entry_relpath(payload)
    provider_url = ""
    for key in (
        "three_d_vista_url",
        "threedvista_url",
        "3dvista_url",
        "source_virtual_tour_url",
        "crezlo_public_url",
    ):
        provider_url = _safe_3dvista_external_url(payload.get(key))
        if provider_url:
            break

    export_dir: Path | None = None
    expected_export_entry = ""
    if evidence_kind == "local_export":
        if not bundle_dir_value:
            return ("local_export_missing",)
        bundle_dir = Path(bundle_dir_value)
        imported = (
            dict(payload.get("three_d_vista_import") or {})
            if isinstance(payload.get("three_d_vista_import"), dict)
            else {}
        )
        entry_parts = PurePosixPath(entry_relpath).parts if entry_relpath else ()
        target_subdir = _safe_3dvista_provenance_relpath(
            raw_provenance.get("target_subdir")
            or imported.get("target_subdir")
            or (entry_parts[0] if len(entry_parts) > 1 else "")
        )
        if not target_subdir:
            return ("target_subdir_missing",)
        try:
            unresolved_export_dir = bundle_dir
            for part in PurePosixPath(target_subdir).parts:
                unresolved_export_dir = unresolved_export_dir / part
                if unresolved_export_dir.is_symlink():
                    return ("target_subdir_symlink_not_allowed",)
            bundle_root = bundle_dir.resolve()
            candidate = unresolved_export_dir.resolve()
        except (OSError, RuntimeError, ValueError):
            return ("target_subdir_invalid",)
        if bundle_root not in candidate.parents or not candidate.is_dir():
            return ("target_subdir_invalid",)
        export_dir = candidate
        prefix = f"{target_subdir}/"
        if not entry_relpath.startswith(prefix):
            return ("entry_outside_target_subdir",)
        expected_export_entry = entry_relpath[len(prefix) :]
        try:
            require_bounded_tree(
                export_dir,
                reason_prefix="public_3dvista_export",
                maximum_files=maximum_files,
                maximum_total_bytes=maximum_total_bytes,
                maximum_file_bytes=maximum_file_bytes,
                maximum_depth=24,
            )
        except TourHostSafetyError as exc:
            return (str(exc),)

    _normalized, errors = validate_3dvista_target_provenance(
        dict(raw_provenance),
        target_slug=target_slug,
        export_dir=export_dir,
        entry_relpath=expected_export_entry,
        provider_url=provider_url,
    )
    return tuple(errors)


def _3dvista_target_provenance_errors(
    payload: dict[str, object],
    *,
    slug: object = "",
    entry_relpath: object = "",
) -> list[str]:
    target_slug = str(slug or payload.get("slug") or "").strip()
    if not target_slug:
        return ["target_slug_missing"]
    raw_provenance = payload.get("three_d_vista_target_provenance")
    if not isinstance(raw_provenance, dict):
        return ["receipt_missing"]
    artifact = (
        dict(raw_provenance.get("artifact") or {})
        if isinstance(raw_provenance.get("artifact"), dict)
        else {}
    )
    bundle_dir_value = ""
    if str(artifact.get("kind") or "").strip().lower() == "local_export":
        bundle_dir = _tour_bundle_dir(target_slug)
        if bundle_dir is None:
            return ["local_export_missing"]
        try:
            bundle_dir_value = str(bundle_dir.resolve())
        except (OSError, RuntimeError, ValueError):
            return ["local_export_missing"]
    relevant_keys = (
        "slug",
        "three_d_vista_target_provenance",
        "three_d_vista_import",
        "three_d_vista_entry_relpath",
        "threedvista_entry_relpath",
        "3dvista_entry_relpath",
        "three_d_vista_url",
        "threedvista_url",
        "3dvista_url",
        "source_virtual_tour_url",
        "crezlo_public_url",
    )
    cache_payload = {
        key: payload.get(key)
        for key in relevant_keys
        if key in payload
    }
    cache_payload["slug"] = target_slug
    requested_entry_relpath = _public_tour_safe_asset_relpath(entry_relpath)
    if requested_entry_relpath:
        cache_payload["three_d_vista_entry_relpath"] = requested_entry_relpath
        cache_payload.pop("threedvista_entry_relpath", None)
        cache_payload.pop("3dvista_entry_relpath", None)
    try:
        payload_json = json.dumps(
            cache_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return ["receipt_invalid"]
    cache_bucket = int(time.monotonic() // _3DVISTA_PROVENANCE_CACHE_SECONDS)
    maximum_files = bounded_env_int(
        "PROPERTYQUARRY_PUBLIC_3DVISTA_MAX_HASH_FILES",
        default=5_000,
        minimum=1,
        maximum=20_000,
    )
    maximum_total_bytes = bounded_env_int(
        "PROPERTYQUARRY_PUBLIC_3DVISTA_MAX_HASH_BYTES",
        default=512 * 1024 * 1024,
        minimum=1_024,
        maximum=2 * 1024 * 1024 * 1024,
    )
    maximum_file_bytes = bounded_env_int(
        "PROPERTYQUARRY_PUBLIC_3DVISTA_MAX_FILE_BYTES",
        default=256 * 1024 * 1024,
        minimum=1_024,
        maximum=512 * 1024 * 1024,
    )
    with _3DVISTA_PROVENANCE_VALIDATION_LOCK:
        return list(
            _3dvista_target_provenance_errors_cached(
                target_slug,
                bundle_dir_value,
                payload_json,
                maximum_files,
                maximum_total_bytes,
                maximum_file_bytes,
                cache_bucket,
            )
        )


def _3dvista_private_viewer_proof_ready(
    payload: dict[str, object],
    *,
    slug: object = "",
    entry_relpath: object = "",
) -> bool:
    proof = payload.get("three_d_vista_white_label_proof")
    proof_payload = dict(proof) if isinstance(proof, dict) else {}
    import_payload = payload.get("three_d_vista_import")
    import_payload = dict(import_payload) if isinstance(import_payload, dict) else {}
    source_project = str(
        proof_payload.get("source_project")
        or import_payload.get("source_project")
        or proof_payload.get("project")
        or import_payload.get("project")
        or ""
    ).strip().lower()
    source_project = re.sub(r"[^a-z0-9]+", "", source_project)
    if source_project not in {"propertyquarry", "propertyquarrycom"}:
        return False
    if _truthy(proof_payload.get("trial_branding_present")):
        return False
    legacy_proof_ready = (
        _truthy(proof_payload.get("private_viewer_verified") or proof_payload.get("private_viewer_delivered"))
        and _truthy(proof_payload.get("non_trial_export_verified") or proof_payload.get("licensed_export_verified"))
        and _truthy(proof_payload.get("propertyquarry_tour_metadata") or proof_payload.get("property_tour_metadata_verified"))
        and _truthy(proof_payload.get("trial_branding_checked"))
    )
    return legacy_proof_ready and not _3dvista_target_provenance_errors(
        payload,
        slug=slug,
        entry_relpath=entry_relpath,
    )


def _3dvista_browser_render_proof_ready(payload: dict[str, object]) -> bool:
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


def _3dvista_entry_ready(slug: object, payload: dict[str, object], entry_relpath: object) -> bool:
    if not _3dvista_browser_render_proof_ready(payload):
        return False
    return _3dvista_entry_export_ready(slug, payload, entry_relpath)


def _3dvista_entry_export_ready(slug: object, payload: dict[str, object], entry_relpath: object) -> bool:
    relpath = _public_tour_safe_asset_relpath(str(entry_relpath or "").strip())
    if not relpath:
        return False
    if not _3dvista_private_viewer_proof_ready(
        payload,
        slug=slug,
        entry_relpath=relpath,
    ):
        return False
    if _local_tour_html_asset_has_marker(slug, relpath, markers=_3DVISTA_FORBIDDEN_PUBLIC_MARKERS):
        return False
    return _local_tour_html_asset_has_marker(slug, relpath, markers=_3DVISTA_EXPORT_MARKERS)


def _pano2vr_export_root_relpath(payload: dict[str, object]) -> str:
    for key in ("pano2vr_export_root_relpath", "pano2vr_root_relpath"):
        relpath = _public_tour_safe_asset_relpath(str(payload.get(key) or "").strip())
        if relpath:
            return relpath.rstrip("/")
    entry_relpath = _pano2vr_entry_relpath(payload)
    if not entry_relpath:
        return ""
    parent = str(PurePosixPath(entry_relpath).parent)
    return "" if parent == "." else parent.rstrip("/")


@lru_cache(maxsize=256)
def _pano2vr_spatial_provenance_errors_cached(
    target_slug: str,
    bundle_dir_value: str,
    payload_json: str,
    maximum_files: int,
    maximum_total_bytes: int,
    maximum_file_bytes: int,
    _cache_bucket: int,
) -> tuple[str, ...]:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return ("receipt_invalid",)
    if not isinstance(payload, dict):
        return ("receipt_invalid",)
    raw_receipt = payload.get(PANO2VR_SPATIAL_PROVENANCE_KEY)
    if not isinstance(raw_receipt, dict):
        return ("receipt_missing",)
    entry_relpath = _pano2vr_entry_relpath(payload)
    export_root_relpath = _pano2vr_export_root_relpath(payload)
    if not entry_relpath or not export_root_relpath or not bundle_dir_value:
        return ("pano2vr_export_root_missing",)
    expected_prefix = f"{export_root_relpath}/"
    if not entry_relpath.startswith(expected_prefix):
        return ("pano2vr_entry_outside_export_root",)
    expected_entry = entry_relpath[len(expected_prefix) :]
    if not _safe_panorama_provenance_relpath(expected_entry):
        return ("pano2vr_entry_invalid",)

    bundle_dir = Path(bundle_dir_value)
    try:
        unresolved_export_dir = bundle_dir
        for part in PurePosixPath(export_root_relpath).parts:
            unresolved_export_dir = unresolved_export_dir / part
            if unresolved_export_dir.is_symlink():
                return ("pano2vr_export_symlink_not_allowed",)
        bundle_root = bundle_dir.resolve()
        export_dir = unresolved_export_dir.resolve()
    except (OSError, RuntimeError, ValueError):
        return ("pano2vr_export_root_invalid",)
    if bundle_root not in export_dir.parents or not export_dir.is_dir():
        return ("pano2vr_export_root_invalid",)
    try:
        require_bounded_tree(
            export_dir,
            reason_prefix="public_pano2vr_export",
            maximum_files=maximum_files,
            maximum_total_bytes=maximum_total_bytes,
            maximum_file_bytes=maximum_file_bytes,
            maximum_depth=24,
        )
        artifact_sha256 = _panorama_export_tree_sha256(export_dir)
        topology = pano2vr_export_topology(export_dir)
    except (OSError, RuntimeError, ValueError, TourHostSafetyError) as exc:
        return (str(exc),)
    _normalized, errors = validate_panorama_spatial_provenance(
        dict(raw_receipt),
        provider="pano2vr",
        target_slug=target_slug,
        artifact_kind="local_export",
        artifact_sha256=artifact_sha256,
        entry_relpath=expected_entry,
        observed_topology=topology,
        walkable_required=panorama_walkable_required(payload),
    )
    return tuple(errors)


def _pano2vr_spatial_provenance_errors(
    payload: dict[str, object],
    *,
    slug: object = "",
) -> list[str]:
    target_slug = str(slug or payload.get("slug") or "").strip()
    if not target_slug:
        return ["target_slug_missing"]
    bundle_dir = _tour_bundle_dir(target_slug)
    if bundle_dir is None:
        return ["pano2vr_export_missing"]
    relevant_keys = (
        "slug",
        "scene_strategy",
        "creation_mode",
        "pano2vr_entry_relpath",
        "pano2vr_export_entry_relpath",
        "pano2vr_export_root_relpath",
        "pano2vr_root_relpath",
        "pano2vr_import",
        PANO2VR_SPATIAL_PROVENANCE_KEY,
    )
    cache_payload = {
        key: payload.get(key)
        for key in relevant_keys
        if key in payload
    }
    cache_payload["slug"] = target_slug
    try:
        payload_json = json.dumps(
            cache_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        bundle_dir_value = str(bundle_dir.resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        return ["receipt_invalid"]
    maximum_files = bounded_env_int(
        "PROPERTYQUARRY_PUBLIC_PANO2VR_MAX_HASH_FILES",
        default=5_000,
        minimum=1,
        maximum=20_000,
    )
    maximum_total_bytes = bounded_env_int(
        "PROPERTYQUARRY_PUBLIC_PANO2VR_MAX_HASH_BYTES",
        default=512 * 1024 * 1024,
        minimum=1_024,
        maximum=2 * 1024 * 1024 * 1024,
    )
    maximum_file_bytes = bounded_env_int(
        "PROPERTYQUARRY_PUBLIC_PANO2VR_MAX_FILE_BYTES",
        default=256 * 1024 * 1024,
        minimum=1_024,
        maximum=512 * 1024 * 1024,
    )
    cache_bucket = int(time.monotonic() // _PANORAMA_PROVENANCE_CACHE_SECONDS)
    with _PANORAMA_PROVENANCE_VALIDATION_LOCK:
        return list(
            _pano2vr_spatial_provenance_errors_cached(
                target_slug,
                bundle_dir_value,
                payload_json,
                maximum_files,
                maximum_total_bytes,
                maximum_file_bytes,
                cache_bucket,
            )
        )


def _pano2vr_spatial_provenance_ready(
    payload: dict[str, object],
    *,
    slug: object = "",
) -> bool:
    return not _pano2vr_spatial_provenance_errors(payload, slug=slug)


def _pano2vr_control_url(slug: str, payload: dict[str, object]) -> str:
    return ""


def _pano2vr_public_enabled() -> bool:
    return str(os.getenv("PROPERTYQUARRY_SHOW_PANO2VR") or "").strip().lower() in {"1", "true", "yes", "on"}


def _local_tour_html_asset_has_marker(slug: object, relpath: object, *, markers: tuple[str, ...]) -> bool:
    safe_slug = str(slug or "").strip()
    safe_relpath = _public_tour_safe_asset_relpath(str(relpath or "").strip())
    if not safe_slug or not safe_relpath:
        return False
    if PurePosixPath(safe_relpath).suffix.lower() not in {".html", ".htm"}:
        return False
    bundle_dir = _tour_bundle_dir(safe_slug)
    if bundle_dir is None:
        return False
    candidate = (bundle_dir / safe_relpath).resolve()
    resolved_bundle = bundle_dir.resolve()
    if candidate == resolved_bundle or resolved_bundle not in candidate.parents or not candidate.is_file():
        return False
    try:
        body = candidate.read_text(encoding="utf-8", errors="replace")[:200_000].lower()
    except OSError:
        return False
    return any(marker in body for marker in markers)


def _local_tour_asset_path(slug: object, relpath: object) -> Path | None:
    safe_slug = str(slug or "").strip()
    safe_relpath = _public_tour_safe_asset_relpath(str(relpath or "").strip())
    if not safe_slug or not safe_relpath:
        return None
    bundle_dir = _tour_bundle_dir(safe_slug)
    if bundle_dir is None:
        return None
    candidate = (bundle_dir / safe_relpath).resolve()
    resolved_bundle = bundle_dir.resolve()
    if candidate == resolved_bundle or resolved_bundle not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def _local_tour_image_dimensions(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return (0, 0)


def _local_tour_equirectangular_image_ready(slug: object, relpath: object) -> bool:
    safe_relpath = _public_tour_safe_asset_relpath(str(relpath or "").strip())
    if not safe_relpath or PurePosixPath(safe_relpath).suffix.lower() not in _PANORAMA_IMAGE_EXTENSIONS:
        return False
    candidate = _local_tour_asset_path(slug, safe_relpath)
    if candidate is None:
        return False
    width, height = _local_tour_image_dimensions(candidate)
    if width < 1024 or height < 512:
        return False
    ratio = width / height if height else 0
    return 1.75 <= ratio <= 2.25


def _local_tour_cube_face_ready(slug: object, relpath: object) -> bool:
    safe_relpath = _public_tour_safe_asset_relpath(str(relpath or "").strip())
    if not safe_relpath or PurePosixPath(safe_relpath).suffix.lower() not in _PANORAMA_IMAGE_EXTENSIONS:
        return False
    candidate = _local_tour_asset_path(slug, safe_relpath)
    if candidate is None:
        return False
    width, height = _local_tour_image_dimensions(candidate)
    if width < 512 or height < 512:
        return False
    ratio = width / height if height else 0
    return 0.9 <= ratio <= 1.1


def _krpano_scene_has_real_360_asset(
    slug: str,
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
        if _local_tour_equirectangular_image_ready(slug, scene.get(key)):
            return True
    cube_faces = scene.get("cube_faces")
    values = list(cube_faces.values()) if isinstance(cube_faces, dict) else list(cube_faces or []) if isinstance(cube_faces, list) else []
    valid_faces = [value for value in values if _local_tour_cube_face_ready(slug, value)]
    return len(valid_faces) >= 6


def _walkable_scene_has_real_360_asset(payload: dict[str, object]) -> bool:
    slug = str(payload.get("slug") or "").strip()
    if not slug:
        return False
    scene_strategy = str(payload.get("scene_strategy") or "").strip().lower()
    creation_mode = str(payload.get("creation_mode") or "").strip().lower()
    if scene_strategy in _KRPANO_FORBIDDEN_SCENE_STRATEGIES or creation_mode in _KRPANO_FORBIDDEN_CREATION_MODES:
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
                slug,
                scene,
                default_projection=default_projection,
            )
            for scene in scene_rows
        )
    return _krpano_scene_has_real_360_asset(
        slug,
        walkable_scene,
        default_projection=default_projection,
    )


def _tour_spatial_review_experience(
    payload: dict[str, object],
    *,
    slug: str,
    matterport_url: str = "",
    three_d_vista_url: str = "",
    pano2vr_url: str = "",
    video_url: str = "",
) -> dict[str, str]:
    video_provider = str(
        payload.get("video_provider")
        or payload.get("video_provider_key")
        or payload.get("video_render_provider")
        or ""
    ).strip().lower()
    if matterport_url:
        return {
            "mode": "spatial",
            "provider": "matterport",
            "provenance": "Live 3D tour",
            "summary": "The 3D tour is ready inside PropertyQuarry.",
            "primary_label": "Open 3D tour",
            "primary_href": matterport_url,
        }
    if three_d_vista_url:
        return {
            "mode": "panorama",
            "provider": "3dvista",
            "provenance": "Interactive 3D tour",
            "summary": "The 3D tour is ready inside PropertyQuarry.",
            "primary_label": "Open 3D tour",
            "primary_href": three_d_vista_url,
        }
    if video_url and video_provider:
        return {
            "mode": "walkthrough",
            "provider": video_provider,
            "provenance": "Walkthrough",
            "summary": "The walkthrough is ready to open.",
            "primary_label": "Open walkthrough",
            "primary_href": video_url,
        }
    return {
        "mode": "pending",
        "provider": "propertyquarry",
        "provenance": "Tour pending",
        "summary": "No tour is ready yet.",
        "primary_label": "Request 3D tour",
        "primary_href": "#",
    }


def _public_tour_primary_control_path(payload: dict[str, object]) -> str:
    slug = str(payload.get("slug") or "").strip()
    if not slug:
        return ""
    quoted_slug = urllib.parse.quote(slug, safe="")
    local_3dvista_entry = _public_tour_safe_asset_relpath(
        str(
            payload.get("three_d_vista_entry_relpath")
            or payload.get("threedvista_entry_relpath")
            or payload.get("3dvista_entry_relpath")
            or ""
        ).strip()
    )
    three_d_vista_browser_ready = _3dvista_browser_render_proof_ready(payload)
    three_d_vista_private_ready = _3dvista_private_viewer_proof_ready(
        payload,
        slug=slug,
    )
    if three_d_vista_browser_ready and three_d_vista_private_ready:
        for key in ("three_d_vista_url", "threedvista_url", "3dvista_url", "source_virtual_tour_url", "crezlo_public_url"):
            if _safe_3dvista_external_url(payload.get(key)):
                return f"/tours/{quoted_slug}/control/3dvista"
        if local_3dvista_entry and _3dvista_entry_ready(slug, payload, local_3dvista_entry):
            return f"/tours/{quoted_slug}/control/3dvista"

    return ""


def _generated_reconstruction_non_tour_asset(payload: dict[str, object], relpath: str) -> str:
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return ""
    safe_relpath = _public_tour_safe_asset_relpath(relpath)
    if not safe_relpath:
        return ""
    if safe_relpath == _public_tour_safe_asset_relpath(str(generated_reconstruction.get("viewer_relpath") or "").strip()):
        return "viewer"
    for key in ("model_relpath", "material_relpath", "glb_model_relpath"):
        if safe_relpath == _public_tour_safe_asset_relpath(str(generated_reconstruction.get(key) or "").strip()):
            return "model"
    return ""


def _generated_reconstruction_preview_relpath(value: object) -> str:
    safe_relpath = _public_tour_safe_asset_relpath(value)
    if not safe_relpath or not safe_relpath.startswith(_GENERATED_RECONSTRUCTION_PREVIEW_PREFIX):
        return ""
    return safe_relpath


def _generated_reconstruction_viewer_module_relpath(viewer_relpath: str, value: object) -> str:
    raw_ref = str(value or "").strip()
    if (
        not raw_ref
        or raw_ref.startswith(("/", "#"))
        or "://" in raw_ref
        or "?" in raw_ref
        or "#" in raw_ref
        or "\\" in raw_ref
    ):
        return ""
    ref_path = PurePosixPath(raw_ref)
    if ref_path.is_absolute() or any(part == ".." for part in ref_path.parts):
        return ""
    parts = [*PurePosixPath(viewer_relpath).parent.parts]
    parts.extend(part for part in ref_path.parts if part not in {"", "."})
    return _generated_reconstruction_preview_relpath("/".join(parts))


def _generated_reconstruction_preview_contract(payload: dict[str, object]) -> dict[str, object]:
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return {}
    if str(generated_reconstruction.get("provider") or "").strip().lower() != "propertyquarry_generated_reconstruction":
        return {}
    # These are deliberately identity checks: publication requires the
    # generator to state the unverified boundary, not merely omit a proof.
    if generated_reconstruction.get("verified_provider_capture") is not False:
        return {}
    if generated_reconstruction.get("satisfies_verified_tour_gate") is not False:
        return {}
    if str(generated_reconstruction.get("viewer_version") or "").strip() != _PROPERTY_GENERATED_RECONSTRUCTION_VIEWER_VERSION:
        return {}
    slug = str(payload.get("slug") or "").strip()
    viewer_relpath = _generated_reconstruction_preview_relpath(generated_reconstruction.get("viewer_relpath"))
    if not slug or not viewer_relpath or PurePosixPath(viewer_relpath).suffix.lower() not in {".htm", ".html"}:
        return {}
    manifest = _public_tour_manifest(payload)
    viewer_row = manifest.get(viewer_relpath)
    if not isinstance(viewer_row, dict):
        return {}
    if str(viewer_row.get("privacy_class") or "").strip().lower() != _GENERATED_RECONSTRUCTION_PREVIEW_PRIVACY_CLASS:
        return {}
    if str(viewer_row.get("role") or "").strip().lower() != "generated_reconstruction_viewer":
        return {}
    try:
        viewer_path = _asset_file(slug, viewer_relpath)
        if viewer_path.stat().st_size > 4 * 1024 * 1024:
            return {}
        viewer_html = viewer_path.read_text(encoding="utf-8")
    except (HTTPException, OSError, UnicodeError):
        return {}
    if not re.search(
        r"\bdata-pq-preview-kind\s*=\s*(['\"])approximate-layout\1",
        viewer_html,
        flags=re.IGNORECASE,
    ):
        return {}
    if not re.search(
        r"\bdata-pq-verified-provider-capture\s*=\s*(['\"])false\1",
        viewer_html,
        flags=re.IGNORECASE,
    ):
        return {}
    if re.search(r"(?i)(?:https?:)?//[a-z0-9]", viewer_html):
        return {}

    module_refs: list[str] = []
    for pattern in (
        r"\bfrom\s*(['\"])(?P<ref>[^'\"]+)\1",
        r"\bimport\s*(['\"])(?P<ref>[^'\"]+)\1",
        r"<script\b[^>]*\bsrc\s*=\s*(['\"])(?P<ref>[^'\"]+)\1",
    ):
        for match in re.finditer(pattern, viewer_html, flags=re.IGNORECASE):
            module_relpath = _generated_reconstruction_viewer_module_relpath(
                viewer_relpath,
                match.group("ref"),
            )
            if not module_relpath:
                return {}
            module_refs.append(module_relpath)
    required_module_suffixes = (
        "/vendor/three.module.js",
        "/vendor/examples/jsm/controls/OrbitControls.js",
    )
    if not all(any(relpath.endswith(suffix) for relpath in module_refs) for suffix in required_module_suffixes):
        return {}
    for module_relpath in module_refs:
        module_row = manifest.get(module_relpath)
        if not isinstance(module_row, dict):
            return {}
        if str(module_row.get("privacy_class") or "").strip().lower() != _GENERATED_RECONSTRUCTION_PREVIEW_PRIVACY_CLASS:
            return {}
        if str(module_row.get("role") or "").strip().lower() != "generated_reconstruction_viewer_asset":
            return {}
        try:
            _asset_file(slug, module_relpath)
        except HTTPException:
            return {}
    return {
        "manifest": manifest,
        "viewer_html": viewer_html,
        "viewer_path": viewer_path,
        "viewer_relpath": viewer_relpath,
    }


def _generated_reconstruction_preview_asset_manifest_row(
    payload: dict[str, object],
    relpath: object,
) -> dict[str, object]:
    safe_relpath = _generated_reconstruction_preview_relpath(relpath)
    if not safe_relpath:
        return {}
    contract = _generated_reconstruction_preview_contract(payload)
    manifest = contract.get("manifest")
    if not contract or not safe_relpath or not isinstance(manifest, dict):
        return {}
    row = manifest.get(safe_relpath)
    if not isinstance(row, dict):
        return {}
    privacy_class = str(row.get("privacy_class") or "").strip().lower()
    role = str(row.get("role") or "").strip().lower().replace("-", "_")
    if privacy_class != _GENERATED_RECONSTRUCTION_PREVIEW_PRIVACY_CLASS or role not in _GENERATED_RECONSTRUCTION_PREVIEW_ROLES:
        return {}
    if role == "generated_reconstruction_viewer" and safe_relpath != contract.get("viewer_relpath"):
        return {}
    if role == "generated_reconstruction_viewer_asset" and PurePosixPath(safe_relpath).suffix.lower() not in {".js", ".mjs"}:
        return {}
    slug = str(payload.get("slug") or "").strip()
    try:
        _asset_file(slug, safe_relpath)
    except HTTPException:
        return {}
    return dict(row)


def _public_tour_is_generated_reconstruction_only(payload: dict[str, object]) -> bool:
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return False
    if _public_tour_primary_control_path(payload):
        return False
    provider = str(generated_reconstruction.get("provider") or "").strip().lower()
    return provider == "propertyquarry_generated_reconstruction"


def _generated_reconstruction_public_shell_ready(payload: dict[str, object]) -> bool:
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return False
    if str(generated_reconstruction.get("provider") or "").strip().lower() != "propertyquarry_generated_reconstruction":
        return False
    if _truthy(generated_reconstruction.get("verified_provider_capture")):
        return False
    slug = str(payload.get("slug") or "").strip()
    if not slug:
        return False
    if str(generated_reconstruction.get("viewer_version") or "").strip() != _PROPERTY_GENERATED_RECONSTRUCTION_VIEWER_VERSION:
        return False
    bundle_dir = _tour_bundle_dir(slug)
    if bundle_dir is None:
        return False
    bundle_root = bundle_dir.resolve()

    def _bundle_file(relpath: object) -> Path | None:
        safe_relpath = _public_tour_safe_asset_relpath(relpath)
        if not safe_relpath:
            return None
        candidate = (bundle_root / safe_relpath).resolve()
        if bundle_root not in candidate.parents or not candidate.is_file():
            return None
        return candidate

    viewer_path = _bundle_file(generated_reconstruction.get("viewer_relpath"))
    walkthrough_path = _bundle_file(generated_reconstruction.get("walkthrough_video_relpath"))
    walkthrough_sidecar_path = _bundle_file(generated_reconstruction.get("walkthrough_sidecar_relpath"))
    if viewer_path is None or walkthrough_path is None or walkthrough_sidecar_path is None:
        return False

    photo_paths = [
        path
        for path in (
            _bundle_file(raw_relpath)
            for raw_relpath in list(generated_reconstruction.get("photo_relpaths") or [])
        )
        if path is not None
    ]
    if len(photo_paths) < 2:
        return False
    floorplan_path = _bundle_file(generated_reconstruction.get("floorplan_relpath"))
    if floorplan_path is None and len(photo_paths) < 3:
        return False

    route_labels = [
        str(label or "").strip()
        for label in list(generated_reconstruction.get("route_labels") or [])
        if str(label or "").strip()
    ]
    if not route_labels:
        return False
    try:
        room_stop_count = int(generated_reconstruction.get("room_stop_count") or len(route_labels))
    except Exception:
        return False
    if room_stop_count <= 0 or room_stop_count != len(route_labels):
        return False

    walkthrough_route_labels = [
        str(label or "").strip()
        for label in list(generated_reconstruction.get("walkthrough_route_labels") or [])
        if str(label or "").strip()
    ]
    try:
        walkthrough_stop_count = int(generated_reconstruction.get("walkthrough_stop_count") or len(walkthrough_route_labels))
    except Exception:
        return False
    if not walkthrough_route_labels or walkthrough_stop_count <= 0 or walkthrough_stop_count != len(walkthrough_route_labels):
        return False
    if walkthrough_stop_count < room_stop_count:
        return False

    receipt: dict[str, object] = {}
    receipt_path = _bundle_file(generated_reconstruction.get("manifest_relpath"))
    if receipt_path is not None:
        try:
            parsed_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(parsed_receipt, dict):
            return False
        receipt = parsed_receipt

    walkable_scene = (
        dict(generated_reconstruction.get("walkable_scene") or {})
        if isinstance(generated_reconstruction.get("walkable_scene"), dict)
        else {}
    )
    if not walkable_scene and isinstance(receipt.get("walkable_scene"), dict):
        walkable_scene = dict(receipt.get("walkable_scene") or {})
    if str(walkable_scene.get("kind") or "").strip() != "generated_reconstruction_layout":
        return False
    route_stops = list(walkable_scene.get("route") or []) if isinstance(walkable_scene.get("route"), list) else []
    room_stops = list(walkable_scene.get("rooms") or []) if isinstance(walkable_scene.get("rooms"), list) else []
    if len(route_stops) != room_stop_count or len(room_stops) != room_stop_count:
        return False
    route_stop_labels: list[str] = []
    room_stop_labels: list[str] = []
    for stop in route_stops:
        if not isinstance(stop, dict):
            return False
        label = str(stop.get("label") or stop.get("room") or stop.get("name") or "").strip()
        focus = dict(stop.get("focus") or {}) if isinstance(stop.get("focus"), dict) else {}
        camera = dict(stop.get("camera") or {}) if isinstance(stop.get("camera"), dict) else {}
        if not label or not focus or not camera:
            return False
        route_stop_labels.append(label)
    for room in room_stops:
        if not isinstance(room, dict):
            return False
        label = str(room.get("label") or room.get("room") or room.get("name") or "").strip()
        position = dict(room.get("position") or {}) if isinstance(room.get("position"), dict) else {}
        focus = dict(room.get("focus") or {}) if isinstance(room.get("focus"), dict) else {}
        if not label or not position or not focus:
            return False
        room_stop_labels.append(label)
    if [label.lower() for label in route_stop_labels] != [label.lower() for label in route_labels]:
        return False
    if [label.lower() for label in room_stop_labels] != [label.lower() for label in route_labels]:
        return False

    coverage = (
        dict(generated_reconstruction.get("walkthrough_coverage_proof") or {})
        if isinstance(generated_reconstruction.get("walkthrough_coverage_proof"), dict)
        else {}
    )
    if not coverage and isinstance(receipt.get("walkthrough"), dict):
        coverage = (
            dict(dict(receipt.get("walkthrough") or {}).get("coverage_proof") or {})
            if isinstance(dict(receipt.get("walkthrough") or {}).get("coverage_proof"), dict)
            else {}
        )
    if not coverage:
        try:
            parsed_sidecar = json.loads(walkthrough_sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if isinstance(parsed_sidecar, dict):
            coverage = (
                dict(parsed_sidecar.get("walkthrough_coverage_proof") or {})
                if isinstance(parsed_sidecar.get("walkthrough_coverage_proof"), dict)
                else {}
            )
    if str(coverage.get("status") or "").strip().lower() != "pass":
        return False

    if receipt:
        geometry = dict(receipt.get("geometry") or {}) if isinstance(receipt.get("geometry"), dict) else {}
        try:
            wall_rect_count = int(geometry.get("wall_rect_count") or 0)
        except Exception:
            wall_rect_count = 0
        if wall_rect_count < 4:
            return False
        room_dimensions = dict(receipt.get("room_dimensions_m") or {}) if isinstance(receipt.get("room_dimensions_m"), dict) else {}
        try:
            width_m = float(room_dimensions.get("width") or 0.0)
            depth_m = float(room_dimensions.get("depth") or 0.0)
            height_m = float(room_dimensions.get("height") or 0.0)
        except Exception:
            return False
        if width_m <= 0.0 or depth_m <= 0.0 or height_m <= 0.0:
            return False
    return True


def _generated_reconstruction_public_viewer_enabled(payload: dict[str, object]) -> bool:
    return _public_tour_is_generated_reconstruction_only(payload) and bool(
        _generated_reconstruction_preview_contract(payload)
    )


def _public_tour_request_prefers_embedded_media(request: Request) -> bool:
    pane = str(request.query_params.get("pane") or "").strip().lower()
    if pane in {"overview-pane", "floorplan-pane", "flythrough-pane"}:
        return True
    if str(request.query_params.get("scene") or "").strip():
        return True
    return _truthy(request.query_params.get("autoplay"))


def _public_tour_request_embeds_walkthrough(request: Request) -> bool:
    pane = str(request.query_params.get("pane") or "").strip().lower()
    if pane:
        return pane == "flythrough-pane"
    return _truthy(request.query_params.get("autoplay"))


def _tour_html(
    payload: dict[str, object],
    *,
    hostname: str = "",
    path: str = "",
    nonce: str = "",
    validated_3dvista_control_path: str = "",
) -> str:
    nonce_attr = html.escape(_public_tour_normalized_nonce(nonce) or _public_tour_csp_nonce(), quote=True)
    if _public_tour_payload_needs_defensive_redaction(payload):
        rendered_payload = _redacted_public_tour_payload(payload, expose_asset_relpaths=True)
        for runtime_key in (
            "_feedback_enabled",
            "_feedback_suggestions",
            "_learning_summary",
            "_shortlist_compare",
            "_public_research_completed",
        ):
            if runtime_key in payload:
                rendered_payload[runtime_key] = payload[runtime_key]
        payload = rendered_payload
    slug = str(payload.get("slug") or "").strip()
    # Retired Matterport receipts may remain in historical bundles, but they
    # are not a public control or CTA authority.
    matterport_url = ""
    three_d_vista_url = ""
    if slug:
        expected_3dvista_control_path = f"/tours/{urllib.parse.quote(slug, safe='')}/control/3dvista"
        if validated_3dvista_control_path == expected_3dvista_control_path:
            # The raw private receipt was validated before the HTML payload
            # was redacted. Carry only the same-origin control path, never the
            # private provenance or provider URL, into the rendered page.
            three_d_vista_url = expected_3dvista_control_path
        three_d_vista_browser_ready = _3dvista_browser_render_proof_ready(payload)
        if not three_d_vista_url and three_d_vista_browser_ready:
            for key in ("three_d_vista_url", "threedvista_url", "3dvista_url", "source_virtual_tour_url", "crezlo_public_url"):
                if _safe_3dvista_external_url(payload.get(key)):
                    three_d_vista_url = f"/tours/{html.escape(slug)}/control/3dvista"
                    break
        local_3dvista_entry = _public_tour_safe_asset_relpath(
            str(
                payload.get("three_d_vista_entry_relpath")
                or payload.get("threedvista_entry_relpath")
                or payload.get("3dvista_entry_relpath")
                or ""
            ).strip()
        )
        if (
            not three_d_vista_url
            and three_d_vista_browser_ready
            and local_3dvista_entry
            and _3dvista_entry_ready(slug, payload, local_3dvista_entry)
        ):
            three_d_vista_url = f"/tours/{html.escape(slug)}/control/3dvista"
    pano2vr_url = _pano2vr_control_url(slug, payload)
    early_scene_strategy = str(payload.get("scene_strategy") or "").strip()
    if early_scene_strategy == "pure_360_cube":
        raise HTTPException(status_code=404, detail="tour_disabled_fallback")
    scenes = [dict(row) for row in (payload.get("scenes") or []) if isinstance(row, dict)]
    control_mode = str(payload.get("control_mode") or "").strip().lower()
    if control_mode == "walkable_3d" or isinstance(payload.get("walkable_scene"), dict):
        safe_title = html.escape(str(payload.get("display_title") or payload.get("title") or payload.get("slug") or "Property tour").strip())
        video_provider = str(
            payload.get("video_provider")
            or payload.get("video_provider_key")
            or payload.get("video_render_provider")
            or ""
        ).strip().lower()
        video_coverage_proof = str(payload.get("video_coverage_proof") or "").strip()
        generated_video_providers = {"magicfit", "onemin_i2v", "ea_one_manager_onemin_i2v", "poppy_ai"}
        video_allowed = bool(video_provider) and (
            video_provider not in generated_video_providers
            or video_coverage_proof == "boundary_verified_frame_continuation"
        )
        walkthrough_url, _walkthrough_mime_type = _public_tour_walkthrough_media_context(payload)
        video_url = walkthrough_url if video_allowed else ""
        spatial_review = _tour_spatial_review_experience(
            payload,
            slug=slug,
            matterport_url=matterport_url,
            three_d_vista_url=three_d_vista_url,
            pano2vr_url=pano2vr_url,
            video_url=video_url,
        )
        return f"""<!doctype html>
<html lang="de">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{safe_title}</title>
    {clickrank_head_snippet(hostname, path)}
    <style nonce="{nonce_attr}">
      html, body {{ margin: 0; min-height: 100%; background: #111; color: #f7f1e6; font-family: Inter, system-ui, sans-serif; }}
      body {{ display: grid; place-items: center; padding: 24px; }}
      main {{ width: min(760px, 100%); border: 1px solid rgba(255,255,255,.18); border-radius: 8px; padding: 22px; background: rgba(255,255,255,.06); }}
      h1 {{ margin: 0 0 10px; font-size: 24px; letter-spacing: 0; }}
      p {{ margin: 0 0 16px; color: rgba(247,241,230,.78); line-height: 1.45; }}
      .eyebrow {{ margin-bottom: 10px; font-size: 12px; letter-spacing: .12em; text-transform: uppercase; color: rgba(247,241,230,.68); }}
      .actions {{ display: flex; flex-wrap: wrap; gap: 10px; }}
      a {{ color: #111; background: #f7f1e6; border-radius: 8px; padding: 11px 13px; text-decoration: none; font-weight: 700; }}
      a.secondary {{ color: #f7f1e6; background: transparent; border: 1px solid rgba(255,255,255,.28); }}
    </style>
  </head>
  <body>
    <main data-spatial-review-mode="{html.escape(spatial_review["mode"])}" data-spatial-review-provider="{html.escape(spatial_review["provider"])}">
      <div class="eyebrow">PropertyQuarry Tour Access · {html.escape(spatial_review["provenance"])}</div>
      <h1>{safe_title}</h1>
      <p>{html.escape(spatial_review["summary"])}</p>
      <p>Only playable tour controls are shown here.</p>
      <div class="actions">
        {f'<a href="{matterport_url}">Open 3D tour</a>' if matterport_url else ''}
        {f'<a href="{three_d_vista_url}">Open 3D tour</a>' if three_d_vista_url else ''}
        {f'<a class="secondary" href="{video_url}">Open walkthrough</a>' if video_url else ''}
      </div>
    </main>
  </body>
</html>"""
    if not scenes:
        raise HTTPException(status_code=500, detail="tour_scenes_missing")
    facts, researched_facts = _merged_facts_with_listing_research(payload, dict(payload.get("facts") or {}))
    facts.pop("public_preference_snapshot", None)
    display_currency_code = _public_tour_currency_code(facts)
    feedback_suggestions = dict(payload.get("_feedback_suggestions") or {}) if isinstance(payload.get("_feedback_suggestions"), dict) else {}
    learning_summary = dict(payload.get("_learning_summary") or {}) if isinstance(payload.get("_learning_summary"), dict) else {}
    filter_context = _filter_panel_context(facts=facts)
    shortlist_compare = dict(payload.get("_shortlist_compare") or {}) if isinstance(payload.get("_shortlist_compare"), dict) else {}
    brief = dict(payload.get("brief") or {})
    title = str(payload.get("title") or payload.get("tour_title") or payload.get("slug") or "Property Tour").strip()
    display_title = str(payload.get("display_title") or title).strip() or title
    listing_url = _public_tour_safe_navigation_url(payload.get("listing_url"))
    hosted_url = _payload_public_tour_canonical_path(slug)
    source_virtual_tour_url = _embedded_live_360_url(payload)
    is_pure_360_cube = str(payload.get("scene_strategy") or "").strip() == "pure_360_cube"
    brand_name = str(payload.get("brand_name") or "Pioche Lecombe").strip() or "Pioche Lecombe"
    hosted_brand_name = _public_tour_host_brand_label(hostname, fallback=brand_name)
    hosted_brand_html = html.escape(hosted_brand_name)
    video_url, video_mime_type = _public_tour_walkthrough_media_context(payload)
    video_source_markup = _public_tour_walkthrough_source_markup(
        payload,
        video_url=video_url,
        video_mime_type=video_mime_type,
    )

    def _trim_text(value: object) -> str:
        return str(value or "").strip()

    def _collect_scene_refs(value: object) -> list[str]:
        if value is None:
            return []
        refs: list[str] = []
        if isinstance(value, (str, int)):
            trimmed = _trim_text(value)
            if trimmed:
                refs.append(trimmed)
            return refs
        if isinstance(value, float):
            if value.is_integer():
                refs.append(_trim_text(int(value)))
            else:
                refs.append(_trim_text(value))
            return refs
        if isinstance(value, (list, tuple)):
            for item in value:
                refs.extend(_collect_scene_refs(item))
            return refs
        if isinstance(value, dict):
            for candidate_key in ("id", "location_id", "scene_id", "next", "to", "target"):
                refs.extend(_collect_scene_refs(value.get(candidate_key)))
            return refs
        return []

    def _text_list(value: object) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item or "").strip() for item in value if str(item or "").strip()]

    def _json_attr(value: object) -> str:
        return html.escape(json.dumps(value, ensure_ascii=False), quote=True)

    def _distance_rows(snapshot: dict[str, object]) -> list[tuple[str, str]]:
        labels = (
            ("nearest_transit_m", "Transit"),
            ("nearest_subway_m", "Underground"),
            ("nearest_supermarket_m", "Supermarket"),
            ("nearest_pharmacy_m", "Pharmacy"),
            ("nearest_library_m", "Library"),
            ("nearest_medical_care_m", "Medical care"),
            ("nearest_market_m", "Market"),
            ("nearest_hardware_store_m", "Baumarkt"),
            ("nearest_shopping_street_m", "Flaniermeile"),
            ("nearest_shopping_center_m", "Shopping center"),
            ("nearest_theatre_m", "Theatre"),
            ("nearest_public_pool_m", "Public pool"),
            ("nearest_bakery_m", "Bakery"),
            ("nearest_bicycle_parking_m", "Bicycle parking"),
            ("nearest_cycleway_m", "Cycleway"),
            ("nearest_playground_m", "Playground"),
            ("nearest_school_m", "School"),
            ("nearest_running_m", "Run or green space"),
        )
        rows: list[tuple[str, str]] = []
        for key, label in labels:
            value = snapshot.get(key)
            if isinstance(value, (int, float)) and value > 0:
                rows.append((label, f"about {int(value):d} m"))
        return rows[:6]

    def _fact_text(*keys: str) -> str:
        for key in keys:
            value = facts.get(key)
            text = str(value or "").strip()
            if text and text not in {"?", "None"}:
                return text
        return ""

    def _fact_bool(*keys: str) -> bool:
        for key in keys:
            value = facts.get(key)
            if isinstance(value, bool):
                return value
        return False

    def _missing_fact_items() -> list[dict[str, object]]:
        research = facts.get("missing_fact_research")
        if not isinstance(research, dict):
            return []
        items = research.get("items")
        if not isinstance(items, list):
            return []
        return [dict(item) for item in items if isinstance(item, dict)]

    def _missing_fact_item(field: str) -> dict[str, object]:
        normalized = str(field or "").strip()
        for item in _missing_fact_items():
            if str(item.get("field") or "").strip() == normalized:
                return item
        return {}

    def _rooms_display() -> str:
        label = _fact_text("rooms_label")
        if "under research" in label.lower():
            label = ""
        if label:
            return label
        raw_rooms = facts.get("rooms") or facts.get("room_count")
        if isinstance(raw_rooms, (int, float)) and float(raw_rooms) > 0:
            return f"{int(raw_rooms) if float(raw_rooms).is_integer() else raw_rooms} rooms"
        item = _missing_fact_item("rooms")
        if item:
            display_value = str(item.get("display_value") or "").strip()
            if "under research" in display_value.lower():
                return ""
            return display_value
        return ""

    def _normalized_token(value: object) -> str:
        text = str(value or "").strip().lower()
        return (
            text.replace("ä", "ae")
            .replace("ö", "oe")
            .replace("ü", "ue")
            .replace("ß", "ss")
        )

    def _feature_highlights() -> list[str]:
        rows: list[str] = []
        terrace_area_value = facts.get("terrace_area_sqm")
        if _fact_bool("lift") and _fact_bool("has_floorplan"):
            rows.append("Lift and floor plan materially reduce remote-viewing uncertainty.")
        elif _fact_bool("has_floorplan"):
            rows.append("A floor plan is available for layout validation.")
        elif _fact_bool("lift"):
            rows.append("The building has a passenger lift.")
        if isinstance(terrace_area_value, (int, float)) and terrace_area_value > 0:
            rows.append(f"{terrace_area_value:g} m² of terrace area adds meaningful private outdoor space.")
        elif _fact_bool("terrace"):
            rows.append("Multiple terraces materially improve usable outdoor space.")
        building_units = facts.get("building_units")
        if isinstance(building_units, (int, float)) and building_units > 0:
            rows.append(f"The building has only {int(building_units)} residential units, which should keep internal traffic lower.")
        state = _fact_text("state")
        renovation_year = facts.get("last_renovation_year")
        if state and isinstance(renovation_year, (int, float)) and renovation_year > 0:
            rows.append(f"The listing describes the condition as {state} and notes renovation in {int(renovation_year)}.")
        availability = _fact_text("availability")
        if availability:
            rows.append(f"Availability is listed as {availability}.")
        if _fact_bool("furnished", "is_furnished"):
            rows.append("The furnished setup lowers move-in friction.")
        elif _fact_bool("balcony"):
            rows.append("Includes a balcony.")
        if _fact_bool("garden"):
            rows.append("Includes outdoor garden space.")
        heating = _fact_text("heating", "heating_type")
        if heating and "gas" not in heating.lower():
            rows.append(f"Heating: {heating}.")
        deduped: list[str] = []
        seen: set[str] = set()
        for row in rows:
            normalized = row.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(row)
        return deduped[:5]

    def _feature_concerns() -> list[str]:
        rows: list[str] = []
        heating = _fact_text("heating", "heating_type")
        if heating and "gas" in heating.lower():
            rows.append("Gas heating may increase running-cost risk.")
        lease_term = facts.get("lease_term_years_max")
        if isinstance(lease_term, (int, float)) and lease_term > 0:
            rows.append(f"The lease is limited to about {int(lease_term)} years, which matters if long-term stability is important.")
        parking_monthly = facts.get("parking_monthly_eur")
        if isinstance(parking_monthly, (int, float)) and parking_monthly > 0:
            rows.append(f"The garage space is optional but adds about {_money(parking_monthly, currency_code=display_currency_code)} per month.")
        if _fact_bool("air_quality_risk"):
            rows.append("Air quality needs a closer look for pollution burden and respiratory comfort.")
        if _fact_bool("crime_risk"):
            rows.append("Safety patterns need a closer look for this micro-location.")
        if _fact_bool("parking_pressure_risk"):
            rows.append("Parking pressure needs a closer look because no garage fallback is listed.")
        if _fact_bool("drinking_water_risk"):
            rows.append("Drinking-water source and groundwater burden need a closer look.")
        if _fact_bool("cesspit_risk"):
            rows.append("Senkgrube or septic dependence needs a closer look for cost and smell burden.")
        if _fact_bool("winter_access_risk"):
            rows.append("Winter snow or slope access needs a closer look.")
        if _fact_bool("flood_risk"):
            rows.append("Flood and runoff exposure need a closer look.")
        if not _fact_bool("has_floorplan") and not rows:
            rows.append("No floor plan is stored yet.")
        if not _fact_bool("lift") and not rows:
            rows.append("Lift access is not listed.")
        return rows[:4]

    def _personalized_priority_rows() -> tuple[list[str], list[str], list[str]]:
        snapshot = dict(facts.get("public_preference_snapshot") or {}) if isinstance(facts.get("public_preference_snapshot"), dict) else {}
        nodes = [dict(row) for row in list(snapshot.get("preference_nodes") or []) if isinstance(row, dict)]
        positive: list[str] = []
        caution: list[str] = []
        open_questions: list[str] = []
        area_value = _normalized_token(_fact_text("postal_name", "district", "location"))
        heating_value = _fact_text("heating", "heating_type")
        heating_lower = heating_value.lower()
        has_floorplan = _fact_bool("has_floorplan")
        has_360 = _fact_bool("has_360")
        has_lift = _fact_bool("lift")
        has_balcony = _fact_bool("balcony") or _fact_bool("terrace")
        nearest_playground = livability_snapshot.get("nearest_playground_m")
        nearest_library = livability_snapshot.get("nearest_library_m")
        nearest_medical_care = livability_snapshot.get("nearest_medical_care_m")
        nearest_cycleway = livability_snapshot.get("nearest_cycleway_m")
        nearest_bicycle_parking = livability_snapshot.get("nearest_bicycle_parking_m")
        nearest_running = livability_snapshot.get("nearest_running_m")
        for row in nodes:
            key = str(row.get("key") or "").strip().lower()
            value = row.get("value_json")
            if key == "preferred_areas" and isinstance(value, list):
                preferred = [_normalized_token(item) for item in value if str(item or "").strip()]
                if area_value and any(item in area_value for item in preferred):
                    positive.append(f"The area matches your preferred places ({_fact_text('postal_name', 'district', 'location')}).")
                elif preferred:
                    caution.append(f"The area is outside your stated preferred places ({', '.join(str(item or '') for item in value if str(item or '').strip())}).")
            elif key == "avoid_heating_types" and isinstance(value, list):
                avoided = [str(item or "").strip().lower() for item in value if str(item or "").strip()]
                if heating_lower and any(item in heating_lower for item in avoided):
                    caution.append(f"{heating_value} conflicts with your heating preferences.")
                elif heating_value and avoided:
                    positive.append(f"{heating_value} avoids your excluded heating types.")
                elif avoided:
                    open_questions.append("The heating type should be checked against your exclusions.")
            elif key in {"require_floorplan", "requires_floorplan_for_remote_review"}:
                if has_floorplan:
                    positive.append("A floor plan is available, which supports your remote review workflow.")
                else:
                    caution.append("No floor plan is stored, although you prefer one for review.")
            elif key == "prefer_360_for_remote_review":
                if not has_360:
                    caution.append("A 360 tour is missing, even though you prefer one for remote review.")
            elif key == "prefer_lift":
                if has_lift:
                    positive.append("Lift access matches your stated preference.")
                else:
                    caution.append("Lift access is not listed, although you prefer it.")
            elif key == "prefer_balcony":
                if has_balcony:
                    positive.append("Outdoor space is available, which matches your balcony or terrace preference.")
                else:
                    caution.append("Outdoor space is not listed.")
            elif "playground" in key:
                if isinstance(nearest_playground, (int, float)) and nearest_playground > 0:
                    positive.append(f"The nearest playground is about {int(nearest_playground):d} m away.")
                else:
                    open_questions.append("Playground distance is not stored yet.")
            elif "library" in key:
                if isinstance(nearest_library, (int, float)) and nearest_library > 0:
                    positive.append(f"The nearest library is about {int(nearest_library):d} m away.")
                else:
                    open_questions.append("Library distance is not stored yet.")
            elif "medical" in key or "doctor" in key or "hospital" in key:
                if isinstance(nearest_medical_care, (int, float)) and nearest_medical_care > 0:
                    positive.append(f"Medical care is about {int(nearest_medical_care):d} m away.")
                else:
                    open_questions.append("Medical-care distance is not stored yet.")
            elif "bike" in key:
                if isinstance(nearest_cycleway, (int, float)) and nearest_cycleway > 0:
                    positive.append(f"Cycleway access is about {int(nearest_cycleway):d} m away.")
                elif isinstance(nearest_bicycle_parking, (int, float)) and nearest_bicycle_parking > 0:
                    positive.append(f"Bicycle parking is about {int(nearest_bicycle_parking):d} m away.")
                else:
                    open_questions.append("Bike infrastructure distance is not stored yet.")
            elif "green" in key or "park" in key or "running" in key:
                if isinstance(nearest_running, (int, float)) and nearest_running > 0:
                    positive.append(f"Green-space or running access is about {int(nearest_running):d} m away.")
                else:
                    open_questions.append("Green-space distance is not stored yet.")
        return positive[:4], caution[:4], open_questions[:4]

    def _decision_rows() -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        exact_address_value = _fact_text("street_address", "exact_address")
        if exact_address_value:
            rows.append(("Address", exact_address_value))
        district_value = _fact_text("postal_name", "district", "location")
        if district_value:
            rows.append(("District", district_value))
        price_value = facts.get("total_rent_eur")
        if isinstance(price_value, (int, float)):
            rows.append(("Price", _money(price_value, currency_code=display_currency_code)))
        elif rent != f"{display_currency_code} ?":
            rows.append(("Price", rent))
        area_value = _fact_text("area_label")
        if not area_value:
            area_sqm_value = facts.get("area_sqm")
            if isinstance(area_sqm_value, (int, float)):
                area_value = f"{int(area_sqm_value) if float(area_sqm_value).is_integer() else area_sqm_value} m²"
        if area_value:
            rows.append(("Area", area_value))
        rooms_value = _rooms_display()
        if not rooms_value:
            rooms_raw = facts.get("rooms")
            if isinstance(rooms_raw, (int, float)):
                rooms_value = f"{int(rooms_raw) if float(rooms_raw).is_integer() else rooms_raw} rooms"
        if rooms_value:
            rows.append(("Rooms", rooms_value))
        availability_value = _fact_text("availability")
        if availability_value:
            rows.append(("Availability", availability_value))
        heating_value = _fact_text("heating", "heating_type")
        if heating_value:
            rows.append(("Heating", heating_value))
        if _fact_bool("lift"):
            rows.append(("Access", "Lift available"))
        elif "lift" in facts:
            rows.append(("Access", "Lift not listed"))
        return rows[:6]

    scene_data = []
    for index, scene in enumerate(scenes):
        scene_id = _trim_text(
            scene.get("scene_id") or scene.get("location_id") or scene.get("id") or scene.get("scene")
        )
        if not scene_id:
            scene_id = str(index + 1)
        next_scene_refs = (
            _collect_scene_refs(scene.get("next_scene_id"))
            + _collect_scene_refs(scene.get("next_scene"))
            + _collect_scene_refs(scene.get("next_location_id"))
            + _collect_scene_refs(scene.get("next"))
        )
        prev_scene_refs = (
            _collect_scene_refs(scene.get("prev_scene_id"))
            + _collect_scene_refs(scene.get("prev_scene"))
            + _collect_scene_refs(scene.get("prev_location_id"))
            + _collect_scene_refs(scene.get("prev"))
        )
        scene_asset_relpath = _public_tour_safe_asset_relpath(scene.get("asset_relpath"))
        scene_image_url = ""
        if slug and scene_asset_relpath:
            scene_image_url = (
                f"/tours/files/{urllib.parse.quote(slug, safe='')}/"
                f"{urllib.parse.quote(scene_asset_relpath, safe='/')}"
            )
        else:
            external_image_url = _public_tour_safe_http_url(scene.get("image_url"))
            if external_image_url and _public_tour_static_media_url_allowed(external_image_url):
                scene_image_url = external_image_url
        scene_row = {
            "name": str(scene.get("name") or "").strip(),
            "scene_id": scene_id,
            "next_scene_id": _trim_text(next_scene_refs[0]) if next_scene_refs else "",
            "prev_scene_id": _trim_text(prev_scene_refs[0]) if prev_scene_refs else "",
            "next_scene_index": scene.get("next_scene_index"),
            "prev_scene_index": scene.get("prev_scene_index"),
            "image_url": scene_image_url,
            "role": str(scene.get("role") or "photo").strip(),
            "mime_type": str(scene.get("mime_type") or "").strip(),
            "source_url": "",
        }
        cube_faces = {
            key: f"/tours/files/{urllib.parse.quote(slug, safe='')}/{urllib.parse.quote(relpath, safe='/')}"
            for key, value in dict(scene.get("cube_faces") or {}).items()
            if slug and (relpath := _public_tour_safe_asset_relpath(value))
        }
        if cube_faces:
            scene_row["cube_faces"] = cube_faces
        scene_data.append(scene_row)

    if is_pure_360_cube and len(scene_data) > 1:
        scene_id_to_index = {
            scene_entry["scene_id"]: index for index, scene_entry in enumerate(scene_data) if scene_entry.get("scene_id")
        }

        def _resolve_scene_index(raw_ref: object, fallback: int) -> int:
            for ref in _collect_scene_refs(raw_ref):
                if ref in scene_id_to_index:
                    return scene_id_to_index[ref]
            return fallback

        for index, entry in enumerate(scene_data):
            next_index_raw = _collect_scene_refs(entry.get("next_scene_id") or entry.get("next_scene_index") or entry.get("next"))
            prev_index_raw = _collect_scene_refs(entry.get("prev_scene_id") or entry.get("prev_scene_index") or entry.get("prev"))
            next_index = _resolve_scene_index(next_index_raw, (index + 1) % len(scene_data))
            prev_index = _resolve_scene_index(prev_index_raw, (index - 1) % len(scene_data))
            entry["next_scene_index"] = next_index
            entry["prev_scene_index"] = prev_index
    data_json = _public_tour_script_json(scene_data)
    title_html = html.escape(title)
    display_html = html.escape(display_title)
    raw_variant_label = str(payload.get("variant_label") or payload.get("variant_key") or "").strip()
    if re.search(r"\b(matterport|3d\s*vista|3dvista|pano2vr|krpano|magicfit|live\s*360|3d\s*tour)\b", raw_variant_label, flags=re.IGNORECASE):
        raw_variant_label = ""
    variant_label = html.escape(raw_variant_label)
    rooms = html.escape(_rooms_display())
    area_display = _fact_text("area_sqm", "area_m2", "living_area_m2")
    area = html.escape(area_display or "Area under research")
    rent_value = _money(
        facts.get("total_rent_eur") or facts.get("price_eur") or facts.get("purchase_price_eur"),
        currency_code=display_currency_code,
    )
    rent = html.escape("" if rent_value == f"{display_currency_code} ?" else rent_value)
    availability = html.escape(_fact_text("availability", "availability_text") or "Availability under research")
    teaser = " · ".join(html.escape(str(value)) for value in (facts.get("teaser_attributes") or []))
    tour_brief_panel = ""
    brand_html = html.escape(brand_name)
    listing_link = f'<a class="ghost" href="{html.escape(listing_url)}" target="_blank" rel="noreferrer">Open listing</a>' if listing_url else ""
    hosted_link = ""
    provider_action_links = [
        ("Open 3D tour", matterport_url, "ghost"),
        ("Open 3D tour", three_d_vista_url, "ghost"),
        ("Open walkthrough", video_url, "ghost"),
    ]
    provider_actions_html = "".join(
        f'<a class="{css_class}" href="{html.escape(href)}">{html.escape(label)}</a>'
        for label, href, css_class in provider_action_links
        if href
    )
    provider_actions_block = (
        f"""
        <div class="actions" aria-label="Tour links">
          {provider_actions_html}
        </div>
        """
        if provider_actions_html
        else ""
    )
    if three_d_vista_url:
        primary_cta = "Open 3D tour"
        primary_cta_href = three_d_vista_url
    elif source_virtual_tour_url:
        primary_cta = "Open 3D tour"
        primary_cta_href = "#live-360"
    else:
        primary_cta = "Review photos"
        primary_cta_href = "#viewer"
    assessment = dict(facts.get("personal_fit_assessment") or {}) if isinstance(facts.get("personal_fit_assessment"), dict) else {}
    if not assessment and isinstance(facts.get("decision_summary"), dict):
        assessment = dict(facts.get("decision_summary") or {})
    recommendation = html.escape(str(assessment.get("recommendation") or "").strip().replace("_", " "))
    fit_score_value = assessment.get("fit_score")
    fit_score = int(round(float(fit_score_value))) if isinstance(fit_score_value, (int, float)) else None
    good_fit_reasons = _text_list(assessment.get("good_fit_reasons") or assessment.get("match_reasons_json"))
    bad_fit_reasons = _text_list(assessment.get("bad_fit_reasons") or assessment.get("mismatch_reasons_json"))
    unknowns = _text_list(assessment.get("unknowns") or assessment.get("unknowns_json"))
    livability_snapshot = dict(assessment.get("livability_snapshot") or {}) if isinstance(assessment.get("livability_snapshot"), dict) else {}
    for livability_key in (
        "nearest_transit_m",
        "nearest_subway_m",
        "nearest_supermarket_m",
        "nearest_pharmacy_m",
        "nearest_library_m",
        "nearest_medical_care_m",
        "nearest_market_m",
        "nearest_hardware_store_m",
        "nearest_shopping_street_m",
        "nearest_shopping_center_m",
        "nearest_theatre_m",
        "nearest_public_pool_m",
        "nearest_bakery_m",
        "nearest_bicycle_parking_m",
        "nearest_cycleway_m",
        "nearest_playground_m",
        "nearest_school_m",
        "nearest_running_m",
    ):
        if livability_key not in livability_snapshot and isinstance(facts.get(livability_key), (int, float)):
            livability_snapshot[livability_key] = facts.get(livability_key)
    location_fit_value = assessment.get("location_fit_score")
    location_fit = int(round(float(location_fit_value))) if isinstance(location_fit_value, (int, float)) else None
    distance_rows = _distance_rows(livability_snapshot)
    has_floorplan = _fact_bool("has_floorplan")
    has_lift = _fact_bool("lift")
    has_balcony = _fact_bool("balcony") or _fact_bool("terrace")
    nearest_playground = livability_snapshot.get("nearest_playground_m")
    district = html.escape(str(facts.get("postal_name") or facts.get("district") or "").strip())
    source_tour_link = ""
    rooms_chip = rooms
    area_chip = area
    rent_chip = rent
    availability_chip = availability
    rooms_legacy_chip_html = f'<div class="chip">{rooms}</div>' if rooms else ""
    area_legacy_chip_html = f'<div class="chip">{area} m²</div>' if area_display else f'<div class="chip">{area}</div>'
    rent_legacy_chip_html = f'<div class="chip">{rent}</div>' if rent else ""
    availability_legacy_chip_html = f'<div class="chip">{availability}</div>' if availability else ""
    decision_rows = _decision_rows()
    personalized_positive, personalized_caution, personalized_unknowns = _personalized_priority_rows()
    highlight_lines = personalized_positive or good_fit_reasons[:4] or _feature_highlights()
    concern_lines = personalized_caution or bad_fit_reasons[:4] or _feature_concerns()
    missing_fact_lines = []
    for item in _missing_fact_items():
        if str(item.get("status") or "").strip().lower() == "filled":
            continue
        label = str(item.get("label") or item.get("field") or "Missing fact").strip()
        ooda = dict(item.get("ooda") or {}) if isinstance(item.get("ooda"), dict) else {}
        action = str(ooda.get("act") or item.get("evidence") or "Research queued.").strip()
        line = f"{label}: {action}".strip()
        missing_fact_lines.append(line[:180])
    unknown_lines = missing_fact_lines[:3] or personalized_unknowns or unknowns[:4]
    completed_research_line = ""
    if researched_facts or _public_tour_env_truthy(payload.get("_public_research_completed")):
        research_fragments: list[str] = []
        if _fact_text("street_address", "exact_address"):
            research_fragments.append("address")
        if _fact_bool("lift"):
            research_fragments.append("lift")
        if _fact_bool("has_floorplan"):
            research_fragments.append("floor plan")
        availability_value = _fact_text("availability")
        if availability_value:
            research_fragments.append(f"availability ({availability_value})")
        if isinstance(facts.get("nearest_supermarket_m"), (int, float)):
            research_fragments.append("supermarket distance")
        if isinstance(facts.get("nearest_pharmacy_m"), (int, float)):
            research_fragments.append("pharmacy distance")
        if isinstance(facts.get("nearest_playground_m"), (int, float)):
            research_fragments.append("playground distance")
        if isinstance(facts.get("nearest_subway_m"), (int, float)):
            research_fragments.append("underground distance")
        if research_fragments:
            completed_research_line = f"Saved details: {', '.join(research_fragments)}."
    if is_pure_360_cube and source_virtual_tour_url:
        fit_score_chip = f'<div class="chip">Fit {fit_score}/100</div>' if fit_score is not None else ""
        recommendation_chip = f'<div class="chip">{recommendation}</div>' if recommendation else ""
        location_chip = f'<div class="chip">Area fit {location_fit}/10</div>' if location_fit is not None else ""
        district_chip = f'<div class="chip">{district}</div>' if district else ""
        rooms_chip_html = f'<div class="chip">{html.escape(rooms_chip)}</div>' if rooms_chip else ""
        area_chip_html = f'<div class="chip">{html.escape(area_chip)}</div>' if area_chip else ""
        rent_chip_html = f'<div class="chip">{html.escape(rent_chip)}</div>' if rent_chip else ""
        availability_chip_html = f'<div class="chip">{html.escape(availability_chip)}</div>' if availability_chip else ""
        reasons_html = "".join(f"<li>{html.escape(item)}</li>" for item in highlight_lines)
        risks_html = "".join(f"<li>{html.escape(item)}</li>" for item in concern_lines)
        unknowns_html = "".join(f"<li>{html.escape(item)}</li>" for item in unknown_lines)
        decision_html = "".join(
            f'<div class="stat"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
            for label, value in decision_rows
        )
        distance_html = "".join(
            f'<div class="stat"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
            for label, value in distance_rows
        )
        recommendation_label = "Conditional match"
        if fit_score is not None and fit_score >= 78:
            recommendation_label = "Strong match"
        elif fit_score is not None and fit_score <= 49:
            recommendation_label = "Low match"
        recommendation_note = (
            highlight_lines[0]
            if highlight_lines
            else "The decision should be driven by constraints, neighborhood fit, and cost risk rather than the tour itself."
        )
        requirement_rows: list[tuple[str, str, str, str]] = []
        snapshot = dict(facts.get("public_preference_snapshot") or {}) if isinstance(facts.get("public_preference_snapshot"), dict) else {}
        nodes = [dict(row) for row in list(snapshot.get("preference_nodes") or []) if isinstance(row, dict)]
        for row in nodes:
            key = str(row.get("key") or "").strip().lower()
            value = row.get("value_json")
            if key == "avoid_heating_types" and isinstance(value, list):
                heating_value = _fact_text("heating", "heating_type") or "Unknown"
                avoided = ", ".join(str(item or "").strip() for item in value if str(item or "").strip())
                status = "Conflict" if "gas" in heating_value.lower() and any("gas" in str(item).lower() for item in value) else "Match"
                note = f"Preference excludes {avoided}." if avoided else "Heating preference stored."
                requirement_rows.append(("Heating", heating_value, status, note))
            elif key in {"require_floorplan", "requires_floorplan_for_remote_review"}:
                requirement_rows.append(("Floor plan", "Included" if has_floorplan else "Missing", "Match" if has_floorplan else "Unknown", "Layout review needs this."))
            elif key == "prefer_lift":
                requirement_rows.append(("Lift", "Present" if has_lift else "Not listed", "Match" if has_lift else "Unknown", "Building access preference."))
            elif key == "prefer_balcony":
                requirement_rows.append(("Outdoor space", "Present" if has_balcony else "Not listed", "Match" if has_balcony else "Unknown", "Balcony or terrace preference."))
            elif "playground" in key:
                playground_value = f"{int(nearest_playground):d} m" if isinstance(nearest_playground, (int, float)) and nearest_playground > 0 else "Unknown"
                requirement_rows.append(("Playground access", playground_value, "Match" if playground_value != "Unknown" else "Unknown", "Family-fit proximity check."))
        if not requirement_rows:
            requirement_rows.extend(
                [
                    ("Heating", _fact_text("heating", "heating_type") or "Unknown", "Conflict" if "gas" in _fact_text("heating", "heating_type").lower() else "Check", "Operating-cost and preference fit."),
                    ("Floor plan", "Included" if has_floorplan else "Missing", "Match" if has_floorplan else "Unknown", "Layout check."),
                    ("Lift", "Present" if has_lift else "Not listed", "Match" if has_lift else "Unknown", "Access convenience."),
                ]
            )
        requirement_table = "".join(
            f'<tr><td data-label="Requirement">{html.escape(label)}</td><td data-label="Property answer">{html.escape(answer)}</td><td data-label="Status"><span class="status status-{status.lower().replace(" ", "-")}">{html.escape(status)}</span></td><td data-label="Why it matters">{html.escape(note)}</td></tr>'
            for label, answer, status, note in requirement_rows[:8]
        )
        cost_rows: list[tuple[str, str]] = []
        if rent_chip:
            cost_rows.append(("Base rent", rent_chip))
        if isinstance(facts.get("parking_monthly_eur"), (int, float)) and float(facts.get("parking_monthly_eur") or 0.0) > 0:
            cost_rows.append(
                (
                    "Parking option",
                    f"{_money(float(facts.get('parking_monthly_eur') or 0.0), currency_code=display_currency_code)}/month",
                )
            )
        heating_value = _fact_text("heating", "heating_type")
        if heating_value:
            cost_rows.append(("Heating system", heating_value))
        lease_term_value = facts.get("lease_term_years_max")
        if isinstance(lease_term_value, (int, float)) and lease_term_value > 0:
            cost_rows.append(("Lease term", f"About {int(lease_term_value)} years"))
        cost_html = "".join(
            f'<div class="stat"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
            for label, value in cost_rows
        )
        evidence_rows: list[tuple[str, str, str]] = []
        evidence_specs = (
            ("street_address", "Address"),
            ("lift", "Lift"),
            ("has_floorplan", "Floor plan"),
            ("availability", "Availability"),
            ("nearest_supermarket_m", "Supermarket"),
            ("nearest_pharmacy_m", "Pharmacy"),
            ("nearest_playground_m", "Playground"),
            ("nearest_library_m", "Library"),
            ("nearest_medical_care_m", "Medical care"),
            ("nearest_market_m", "Market"),
            ("nearest_hardware_store_m", "Baumarkt"),
            ("nearest_shopping_street_m", "Flaniermeile"),
            ("nearest_shopping_center_m", "Shopping center"),
            ("nearest_theatre_m", "Theatre"),
            ("nearest_public_pool_m", "Public pool"),
            ("nearest_subway_m", "Underground"),
        )
        for key, label in evidence_specs:
            raw_value = facts.get(key)
            if _fact_value_is_weak(raw_value):
                continue
            if isinstance(raw_value, bool):
                value = "Yes" if raw_value else "No"
            elif isinstance(raw_value, (int, float)) and key.endswith("_m"):
                value = f"about {int(raw_value)} m"
            else:
                value = str(raw_value)
            provenance = "Map" if key in researched_facts else "Listing"
            if key in {"street_address", "exact_address"} and "map_lat" in researched_facts:
                provenance = "Approx."
            evidence_rows.append((label, value, provenance))
        evidence_html = "".join(
            f'<div class="evidence-row"><div><b>{html.escape(label)}</b><span>{html.escape(value)}</span></div><em class="provenance provenance-{provenance.lower()}">{html.escape(provenance)}</em></div>'
            for label, value, provenance in evidence_rows
        )
        feedback_negative = [dict(row) for row in list(feedback_suggestions.get("negative") or []) if isinstance(row, dict)]
        feedback_positive = [dict(row) for row in list(feedback_suggestions.get("positive") or []) if isinstance(row, dict)]
        feedback_negative_html = "".join(
            f'<button class="reason-chip reason-chip-negative" type="button" data-reason-key="{html.escape(str(row.get("key") or ""))}" data-sentiment="negative">{html.escape(str(row.get("label") or ""))}</button>'
            for row in feedback_negative
        )
        feedback_positive_html = "".join(
            f'<button class="reason-chip reason-chip-positive" type="button" data-reason-key="{html.escape(str(row.get("key") or ""))}" data-sentiment="positive">{html.escape(str(row.get("label") or ""))}</button>'
            for row in feedback_positive
        )
        learned_likes = _text_list(learning_summary.get("likes"))
        learned_dislikes = _text_list(learning_summary.get("dislikes"))
        learned_hard_rules = _text_list(learning_summary.get("hard_rules"))
        recent_feedback_rows = [dict(row) for row in list(learning_summary.get("recent_feedback") or []) if isinstance(row, dict)]
        learned_likes_html = "".join(f"<li>{html.escape(item)}</li>" for item in learned_likes[:6])
        learned_dislikes_html = "".join(f"<li>{html.escape(item)}</li>" for item in learned_dislikes[:6])
        learned_hard_rules_html = "".join(f"<li>{html.escape(item)}</li>" for item in learned_hard_rules[:4])
        recent_feedback_html = "".join(
            (
                '<div class="feedback-log-row">'
                f'<b>{html.escape(str(row.get("reaction") or "").title() or "Feedback")}</b>'
                f'<span>{html.escape(", ".join(_feedback_reason_label(item) for item in list(row.get("reasons") or []) if str(item or "").strip()) or "No structured reasons yet")}</span>'
                f'<em>{html.escape(str(row.get("recorded_at") or "")[:16].replace("T", " "))}</em>'
                '</div>'
            )
            for row in recent_feedback_rows[:6]
        )
        comparison_positive = (personalized_positive or good_fit_reasons or highlight_lines)[:3]
        comparison_conflicts = (personalized_caution or bad_fit_reasons or concern_lines)[:3]
        ranking_read_panel = (
            '<section class="panel">'
            '<div class="eyebrow">Quick read</div>'
            '<h2>Quick take</h2>'
            '<div class="summary-grid flush-top">'
            '<div class="summary-card"><h3>Best points</h3><ul>'
            f'{"".join(f"<li>{html.escape(item)}</li>" for item in comparison_positive) or "<li>No best point saved yet.</li>"}'
            '</ul></div>'
            '<div class="summary-card"><h3>Main caution</h3><ul>'
            f'{"".join(f"<li>{html.escape(item)}</li>" for item in comparison_conflicts) or "<li>No main caution saved yet.</li>"}'
            '</ul></div>'
            '<div class="summary-card"><h3>Open checks</h3><ul>'
            f'{"".join(f"<li>{html.escape(item)}</li>" for item in (unknown_lines[:3] or ["No open check saved yet."]))}'
            '</ul></div>'
            '</div>'
            '</section>'
        )
        shortlist_items = [dict(row) for row in list(shortlist_compare.get("items") or []) if isinstance(row, dict)]
        shortlist_current = dict(shortlist_compare.get("current") or {}) if isinstance(shortlist_compare.get("current"), dict) else {}
        shortlist_rows: list[dict[str, object]] = []
        if shortlist_current:
            shortlist_rows.append(shortlist_current)
        shortlist_rows.extend(shortlist_items)

        shortlist_cards = ""
        if shortlist_rows:
            shortlist_cards = "".join(
                (
                    '<div class="summary-card">'
                    f'<h3>{html.escape(str(card.get("title") or "Property").strip())}</h3>'
                    f'<div class="subtle">{html.escape(str(card.get("score_label") or "Fit").strip())} '
                    f'{int(round(float(card.get("score") or 0.0))):d}/100</div>'
                    f'<p class="sub">{html.escape(str(card.get("why_now") or "No ranking note stored.").strip())}</p>'
                    f'<a class="chip rank-chip" href="{html.escape(_public_tour_safe_navigation_url(card.get("listing_url")) or "#")}"'
                    f'{"" if _public_tour_safe_navigation_url(card.get("listing_url")) else " aria-disabled=\"true\""}>{html.escape(_public_shortlist_action_label(card.get("recommended_action")))}</a>'
                    '</div>'
                )
                for card in shortlist_rows[:3]
            )
        shortlist_panel = (
            '<section class="panel">'
            '<div class="eyebrow">Shortlist</div>'
            '<h2>Current property in the active shortlist</h2>'
            '<div class="summary-grid flush-top">'
            f'{shortlist_cards or "<div class=\"summary-card\"><h3>No shortlist loaded</h3><p class=\"sub\">No other active shortlist property is currently available.</p></div>"}'
            '</div>'
            '</section>'
        )
        detail_request_button = (
            '<div class="request-row">'
            '<span id="request-details-status" class="request-status">'
            'Open the authenticated PropertyQuarry property page to request deeper research.'
            '</span>'
            '</div>'
        )
        active_filter_labels = [str(item or "").strip() for item in list(filter_context.get("active_labels") or []) if str(item or "").strip()]
        hard_filter_button_html = "".join(
            (
                f'<button class="reason-chip filter-chip{" active" if bool(spec.get("active")) else ""}" '
                f'type="button" data-filter-key="{html.escape(str(spec.get("key") or ""))}" '
                f'data-enabled="{html.escape("false" if bool(spec.get("active")) else "true")}" '
                'disabled title="Open the authenticated PropertyQuarry account to change profile filters.">'
                f'{html.escape(str(spec.get("label") or ""))}'
                '</button>'
            )
            for spec in list(filter_context.get("hard_filters") or [])
            if isinstance(spec, dict)
        )
        soft_filter_button_html = "".join(
            (
                f'<button class="reason-chip filter-chip{" active" if bool(spec.get("active")) else ""}" '
                f'type="button" data-filter-key="{html.escape(str(spec.get("key") or ""))}" '
                f'data-enabled="{html.escape("false" if bool(spec.get("active")) else "true")}" '
                'disabled title="Open the authenticated PropertyQuarry account to change profile filters.">'
                f'{html.escape(str(spec.get("label") or ""))}'
                '</button>'
            )
            for spec in list(filter_context.get("soft_filters") or [])
            if isinstance(spec, dict)
        )
        active_filter_html = "".join(f"<li>{html.escape(label)}</li>" for label in active_filter_labels[:8])
        filters_panel = ""
        feedback_panel = ""
        if bool(payload.get("_feedback_enabled")) or str(payload.get("principal_id") or "").strip():
            feedback_panel = (
                '<section id="feedback" class="panel">'
                '<div class="eyebrow">Preference Feedback</div>'
                '<h2>Teach the system what to rank higher or lower</h2>'
                '<p class="sub">Give a quick reaction and mark concrete reasons. Public-link feedback is captured as an external signal; sign in to apply it to a ranking profile.</p>'
                '<div class="feedback-reaction-row">'
                '<button class="reaction-btn" type="button" data-reaction="like">Like</button>'
                '<button class="reaction-btn" type="button" data-reaction="maybe">Maybe</button>'
                '<button class="reaction-btn" type="button" data-reaction="dislike">Dislike</button>'
                '<button class="reaction-btn" type="button" data-reaction="hide">Hide</button>'
                '</div>'
                '<div class="feedback-groups">'
                '<div><h3>What hurts this property</h3><div class="reason-chip-row">'
                f'{feedback_negative_html or "<span class=\"subtle\">No structured negatives suggested yet.</span>"}'
                '</div></div>'
                '<div><h3>What works well</h3><div class="reason-chip-row">'
                f'{feedback_positive_html or "<span class=\"subtle\">No structured positives suggested yet.</span>"}'
                '</div></div>'
                '</div>'
                '<label class="feedback-note-label" for="feedback-note">Optional note</label>'
                '<textarea id="feedback-note" class="feedback-note" rows="3" placeholder="Anything subtle that the chips missed."></textarea>'
                '<div class="request-row">'
                '<button id="feedback-submit-btn" class="request-btn" type="button">Save feedback</button>'
                '<span id="feedback-status" class="request-status"></span>'
                '</div>'
                '</section>'
            )
        learned_panel = ""
        return f"""<!doctype html>
<html lang="de">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title_html}</title>
    {clickrank_head_snippet(hostname, path)}
    <style nonce="{nonce_attr}">
      :root {{
        --bg: #f5f2ec;
        --panel: #ffffff;
        --panel-soft: #f7f6f3;
        --ink: #171717;
        --muted: #646464;
        --accent: #8d3f1f;
        --edge: #e6e0d6;
        --good: #166534;
        --warn: #9a6700;
        --risk: #991b1b;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        color: var(--ink);
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: var(--bg);
      }}
      .shell {{ max-width: 1340px; margin: 0 auto; padding: 22px; }}
      .topbar, .panel, .live-shell, .hero, .section-band {{
        background: var(--panel);
        border: 1px solid var(--edge);
        border-radius: 18px;
        box-shadow: 0 8px 28px rgba(17, 17, 17, 0.05);
      }}
      .topbar, .panel, .live-shell, .hero, .section-band {{ padding: 20px; }}
      .topbar {{ position: sticky; top: 0; z-index: 20; display: flex; gap: 10px; align-items: center; justify-content: space-between; margin-bottom: 16px; flex-wrap: wrap; backdrop-filter: blur(12px); }}
      .section-nav {{ display: flex; gap: 8px; flex-wrap: wrap; }}
      .eyebrow {{ display: inline-flex; gap: 8px; align-items: center; font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); }}
      h1 {{ margin: 10px 0 10px; font-size: clamp(2rem, 3vw, 3.2rem); line-height: 1.02; }}
      h2 {{ margin: 0 0 14px; font-size: 1.05rem; }}
      h3 {{ margin: 0 0 10px; font-size: 0.95rem; }}
      .sub {{ margin: 0; color: var(--muted); font-size: 0.98rem; line-height: 1.55; max-width: 72ch; }}
      .hero {{ display: grid; grid-template-columns: 1.25fr 0.75fr; gap: 18px; align-items: start; margin-bottom: 18px; }}
      .summary-grid, .section-grid {{ display: grid; gap: 18px; }}
      .summary-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top: 18px; }}
      .flush-top {{ margin-top: 0; }}
      .spaced-top {{ margin-top: 12px; }}
      .spaced-top-lg {{ margin-top: 16px; }}
      .section-grid {{ grid-template-columns: 1.05fr 0.95fr; margin-top: 18px; }}
      .facts, .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; }}
      .chip, .ghost, .cta {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 38px;
        padding: 0 14px;
        border-radius: 999px;
        background: var(--panel-soft);
        border: 1px solid var(--edge);
        color: inherit;
        text-decoration: none;
        font-size: 0.92rem;
      }}
      .cta {{ background: var(--ink); color: #fff; border-color: var(--ink); }}
      .kicker {{
        display: inline-flex;
        align-items: center;
        gap: 10px;
        min-height: 34px;
        padding: 0 12px;
        border-radius: 999px;
        background: #f0ebe4;
        color: var(--accent);
        font-size: 0.86rem;
      }}
      .summary-card, .panel, .live-shell {{
        background: var(--panel);
      }}
      .summary-card {{
        padding: 16px;
        border-radius: 16px;
        border: 1px solid var(--edge);
        background: var(--panel-soft);
      }}
      .filter-summary-grid {{ grid-template-columns: minmax(0, 0.75fr) minmax(0, 1.25fr); }}
      .summary-card ul, .panel ul {{ margin: 0; padding-left: 18px; }}
      .summary-card li + li, .panel li + li {{ margin-top: 8px; }}
      .rank-chip {{ margin-top: 12px; }}
      .stat-grid {{
        display: grid;
        gap: 10px;
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .stat {{
        padding: 12px 14px;
        border-radius: 14px;
        background: var(--panel-soft);
        border: 1px solid var(--edge);
        display: grid;
        gap: 4px;
      }}
      .stat span {{
        font-size: 0.76rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: var(--muted);
      }}
      .stat strong {{ font-size: 1rem; font-weight: 600; }}
      .tour-detail-grid {{
        display: grid;
        grid-template-columns: 1.05fr 0.95fr;
        gap: 18px;
        align-items: start;
      }}
      .tour-detail-stack {{ display: grid; gap: 18px; }}
      .requirement-table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 0.94rem;
      }}
      .requirement-table th, .requirement-table td {{
        padding: 12px 10px;
        border-bottom: 1px solid var(--edge);
        vertical-align: top;
        text-align: left;
      }}
      .requirement-table th {{
        font-size: 0.74rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: var(--muted);
      }}
      .status {{
        display: inline-flex;
        align-items: center;
        min-height: 28px;
        padding: 0 10px;
        border-radius: 999px;
        font-size: 0.82rem;
        border: 1px solid transparent;
      }}
      .status-match {{ background: rgba(22,101,52,0.10); color: var(--good); border-color: rgba(22,101,52,0.18); }}
      .status-conflict {{ background: rgba(153,27,27,0.10); color: var(--risk); border-color: rgba(153,27,27,0.18); }}
      .status-unknown, .status-check {{ background: rgba(154,103,0,0.10); color: var(--warn); border-color: rgba(154,103,0,0.18); }}
      details.research-card {{
        padding: 12px 14px;
        border-radius: 14px;
        background: var(--panel-soft);
        border: 1px solid var(--edge);
      }}
      details.research-card + details.research-card {{ margin-top: 12px; }}
      details.research-card > summary {{ cursor: pointer; font-weight: 600; }}
      .evidence-stack {{ display: grid; gap: 10px; }}
      .evidence-row {{
        display: flex;
        justify-content: space-between;
        gap: 16px;
        align-items: center;
        padding: 12px 14px;
        border-radius: 14px;
        background: var(--panel-soft);
        border: 1px solid var(--edge);
      }}
      .evidence-row b, .evidence-row span {{
        display: block;
      }}
      .evidence-row b {{ margin-bottom: 4px; font-size: 0.86rem; }}
      .evidence-row span {{ color: var(--muted); font-size: 0.92rem; }}
      .shortlist-matrix-wrap {{
        margin-top: 14px;
        overflow-x: auto;
        border: 1px solid var(--edge);
        border-radius: 14px;
        background: var(--panel-soft);
      }}
      .shortlist-table {{
        width: 100%;
        border-collapse: collapse;
        min-width: 780px;
      }}
      .shortlist-table th,
      .shortlist-table td {{
        padding: 12px 10px;
        border-bottom: 1px solid var(--edge);
        border-right: 1px solid var(--edge);
        text-align: left;
        vertical-align: top;
      }}
      .shortlist-table th:last-child,
      .shortlist-table td:last-child {{ border-right: 0; }}
      .shortlist-table th {{
        background: #f0ebe4;
        color: var(--muted);
        font-size: 0.74rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }}
      .shortlist-metric-label {{
        width: 170px;
        white-space: nowrap;
      }}
      .shortlist-table tbody tr:last-child th,
      .shortlist-table tbody tr:last-child td {{
        border-bottom: 0;
      }}
      .shortlist-header-link {{
        color: inherit;
        font-weight: 600;
        text-decoration: none;
        display: inline-flex;
      }}
      .shortlist-header-link:hover {{
        text-decoration: underline;
      }}
      .shortlist-value {{
        display: block;
        font-weight: 600;
      }}
      .shortlist-delta {{
        display: inline-flex;
        margin-top: 6px;
        font-size: 0.76rem;
        font-weight: 600;
      }}
      .shortlist-delta-better {{ color: var(--good); }}
      .shortlist-delta-worse {{ color: var(--risk); }}
      .shortlist-delta-neutral {{ color: var(--muted); }}
      .shortlist-empty {{
        margin-top: 12px;
      }}
      .provenance {{
        display: inline-flex;
        align-items: center;
        min-height: 28px;
        padding: 0 10px;
        border-radius: 999px;
        font-size: 0.8rem;
        font-style: normal;
        border: 1px solid var(--edge);
        background: #fff;
      }}
      .request-row {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-top: 14px; }}
      .request-btn {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 40px;
        padding: 0 16px;
        border-radius: 999px;
        border: 1px solid var(--edge);
        background: var(--panel-soft);
        color: inherit;
        cursor: pointer;
      }}
      .request-status {{ color: var(--muted); font-size: 0.95rem; }}
      .feedback-reaction-row, .reason-chip-row, .feedback-groups {{
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
      }}
      .filter-chip-row {{ align-items: stretch; }}
      .filter-group {{ display: grid; gap: 10px; margin-top: 12px; }}
      .filter-group b {{ font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }}
      .feedback-groups {{ flex-direction: column; margin-top: 14px; }}
      .reaction-btn, .reason-chip {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 38px;
        padding: 0 14px;
        border-radius: 999px;
        border: 1px solid var(--edge);
        background: var(--panel-soft);
        color: inherit;
        cursor: pointer;
      }}
      .filter-chip {{ min-height: 44px; font-weight: 600; text-align: center; }}
      .reaction-btn.active {{ background: var(--ink); color: #fff; border-color: var(--ink); }}
      .reason-chip.active {{ border-color: var(--accent); color: var(--accent); background: #fff8f3; }}
      .reason-chip-negative {{ background: #fbf5f3; }}
      .reason-chip-positive {{ background: #f3f8f3; }}
      .feedback-note-label {{ display: block; margin: 16px 0 8px; font-size: 0.85rem; color: var(--muted); }}
      .feedback-note {{
        width: 100%;
        border-radius: 14px;
        border: 1px solid var(--edge);
        background: var(--panel-soft);
        padding: 12px 14px;
        resize: vertical;
        font: inherit;
        color: inherit;
      }}
      .feedback-log {{ display: grid; gap: 10px; margin-top: 16px; }}
      .feedback-log-row {{
        display: grid;
        gap: 4px;
        padding: 12px 14px;
        border-radius: 14px;
        border: 1px solid var(--edge);
        background: var(--panel-soft);
      }}
      .feedback-log-row span, .subtle {{ color: var(--muted); font-size: 0.92rem; }}
      .ooda-grid {{
        display: grid;
        gap: 10px;
      }}
      .ooda-cell {{
        padding: 12px 14px;
        border-radius: 14px;
        background: var(--panel-soft);
        border: 1px solid var(--edge);
      }}
      .ooda-cell b {{
        display: block;
        margin-bottom: 4px;
        font-size: 0.76rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: var(--muted);
      }}
      .live-frame-wrap {{
        overflow: hidden;
        border-radius: 16px;
        background: rgba(18,17,16,0.94);
        border: 1px solid var(--edge);
        min-height: 540px;
      }}
      .live-frame {{
        display: block;
        width: 100%;
        height: 78vh;
        min-height: 540px;
        border: 0;
        background: #111;
      }}
      a {{ color: inherit; text-decoration: none; }}
      @media (max-width: 1000px) {{
        .hero, .section-grid, .tour-detail-grid, .summary-grid {{ grid-template-columns: 1fr; }}
        .stat-grid {{ grid-template-columns: 1fr; }}
        .filter-summary-grid {{ grid-template-columns: 1fr; }}
      }}
      @media (max-width: 720px) {{
        .section-nav {{
          flex-wrap: nowrap;
          overflow-x: auto;
          padding-bottom: 4px;
          width: 100%;
          scrollbar-width: none;
        }}
        .section-nav::-webkit-scrollbar {{ display: none; }}
        .section-nav .ghost {{ flex: 0 0 auto; min-height: 40px; padding: 0 12px; }}
        .facts {{
          flex-wrap: nowrap;
          overflow-x: auto;
          padding-bottom: 4px;
          scrollbar-width: none;
        }}
        .facts::-webkit-scrollbar {{ display: none; }}
        .facts .chip {{ flex: 0 0 auto; }}
        .requirement-table, .requirement-table thead, .requirement-table tbody, .requirement-table tr, .requirement-table td {{
          display: block;
          width: 100%;
        }}
        .requirement-table thead {{
          position: absolute;
          width: 1px;
          height: 1px;
          padding: 0;
          margin: -1px;
          overflow: hidden;
          clip: rect(0, 0, 0, 0);
          white-space: nowrap;
          border: 0;
        }}
        .requirement-table tbody {{ display: grid; gap: 12px; }}
        .requirement-table tr {{
          border: 1px solid var(--edge);
          border-radius: 14px;
          background: var(--panel-soft);
          padding: 12px;
        }}
        .requirement-table td {{ border: 0; padding: 8px 0 0; }}
        .requirement-table td:first-child {{ padding-top: 0; }}
        .requirement-table td::before {{
          content: attr(data-label);
          display: block;
          margin-bottom: 4px;
          font-size: 0.72rem;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          color: var(--muted);
        }}
        .evidence-row {{ display: grid; justify-content: start; }}
        .request-row {{ align-items: stretch; }}
        .request-btn, .reaction-btn {{ width: 100%; }}
      }}
      @media (max-width: 640px) {{
        .shell {{ padding: 14px; }}
        .topbar, .panel, .live-shell, .hero, .section-band {{ padding: 16px; border-radius: 16px; }}
        h1 {{ font-size: clamp(1.85rem, 10vw, 2.5rem); line-height: 1; }}
        .sub {{ max-width: none; font-size: 0.95rem; }}
        .actions {{ display: grid; grid-template-columns: 1fr; }}
        .actions .cta, .actions .ghost {{ width: 100%; }}
        .feedback-reaction-row {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
        .reason-chip-row {{ display: grid; grid-template-columns: 1fr; }}
        .live-frame-wrap {{ min-height: 380px; }}
        .live-frame {{ min-height: 380px; height: 60vh; }}
      }}
    </style>
  </head>
  <body>
    <div class="shell">
      <div class="topbar">
        <div class="eyebrow">Property review <span>•</span> personal shortlist</div>
        <nav class="section-nav">
          <a class="ghost" href="#decision">Decision</a>
          <a class="ghost" href="#match">Match</a>
          <a class="ghost" href="#location">Location</a>
          <a class="ghost" href="#costs">Costs</a>
          <a class="ghost" href="#filters">Filters</a>
          <a class="ghost" href="#feedback">Feedback</a>
          <a class="ghost" href="#risks">Risks</a>
          <a class="ghost" href="#research">Research</a>
          <a class="ghost" href="#tour">3D Tour</a>
        </nav>
      </div>
      <section id="decision" class="hero">
        <div>
          <div class="kicker">{html.escape(recommendation_label)}</div>
          <h1>{title_html}</h1>
          <p class="sub">{display_html}</p>
          <div class="facts">
            {rooms_chip_html}
            {area_chip_html}
            {rent_chip_html}
            {availability_chip_html}
            {fit_score_chip}
            {recommendation_chip}
            {location_chip}
            {district_chip}
            <div class="chip">{html.escape(str(payload.get("scene_count") or len(scenes)))} views</div>
          </div>
          <p class="sub">{html.escape(recommendation_note)}</p>
          <div class="actions">
            <a class="cta" href="{html.escape(primary_cta_href)}">{html.escape(primary_cta)}</a>
            {listing_link}
            {hosted_link}
          </div>
          <div class="summary-grid">
            <div class="summary-card">
              <h3>Highlights</h3>
              <ul>{reasons_html}</ul>
            </div>
            <div class="summary-card">
              <h3>Decision pressure</h3>
              <ul>{risks_html}</ul>
            </div>
            <div class="summary-card">
              <h3>Still missing</h3>
              <ul>{unknowns_html}</ul>
            </div>
          </div>
        </div>
        <aside class="panel">
          <h2>At a glance</h2>
          <div class="stat-grid">{decision_html}</div>
          <div class="ooda-grid spaced-top-lg">
            <div class="ooda-cell"><b>Observe</b>{html.escape(highlight_lines[0]) if highlight_lines else 'Current facts are still incomplete.'}</div>
            <div class="ooda-cell"><b>Orient</b>{html.escape((personalized_positive or good_fit_reasons or ['The current fit is driven by the stored constraints and research pass.'])[0])}</div>
            <div class="ooda-cell"><b>Decide</b>{html.escape((personalized_caution or bad_fit_reasons or ['Shortlist only if the open questions are acceptable.'])[0])}</div>
            <div class="ooda-cell"><b>Act</b>{html.escape((personalized_unknowns or unknowns or ['Trigger deeper research before deciding.'])[0])}</div>
          </div>
        </aside>
      </section>
      <div class="tour-detail-grid">
        <div class="tour-detail-stack">
          <section id="match" class="panel">
            <div class="eyebrow">Requirement Match</div>
            <h2>How it matches your brief</h2>
            <table class="requirement-table">
              <thead>
                <tr><th>Requirement</th><th>Property answer</th><th>Status</th><th>Why it matters</th></tr>
              </thead>
              <tbody>{requirement_table}</tbody>
            </table>
          </section>
          <section id="location" class="panel">
            <div class="eyebrow">Location Fit</div>
            <h2>Daily-life access</h2>
            <div class="stat-grid">{distance_html}</div>
          </section>
          <section id="costs" class="panel">
            <div class="eyebrow">Cost Picture</div>
            <h2>Monthly and structural costs</h2>
            <div class="stat-grid">{cost_html}</div>
          </section>
          {filters_panel}
          {feedback_panel}
          <section id="tour" class="live-shell">
            <div class="eyebrow">{brand_html} <span>•</span> 3D tour</div>
            <h2>Inspect layout, light, and finish quality</h2>
            <p class="sub">Use the original interactive 360 experience after the quick read, not instead of it.</p>
            {provider_actions_block}
            <div class="live-frame-wrap">
              <iframe
                class="live-frame"
                src="{html.escape(source_virtual_tour_url)}"
                title="{title_html}"
                allowfullscreen
                loading="lazy"
                referrerpolicy="no-referrer"
              ></iframe>
            </div>
          </section>
        </div>
        <div class="tour-detail-stack">
          <section id="risks" class="panel">
            <div class="eyebrow">Check first</div>
            <h2>Before you book a viewing</h2>
            <ul>{risks_html}</ul>
          </section>
          {shortlist_panel}
          {ranking_read_panel}
          <section id="research" class="panel">
            <div class="eyebrow">Area</div>
            <h2>Local notes</h2>
            <details class="research-card" open>
              <summary>Saved details</summary>
              <p class="sub">{html.escape(completed_research_line) if completed_research_line else 'No completed enrichment checks are stored yet.'}</p>
            </details>
            <details class="research-card">
              <summary>Details</summary>
              <div class="evidence-stack spaced-top">{evidence_html}</div>
            </details>
            <details class="research-card">
              <summary>Still to check</summary>
              <ul>{unknowns_html}</ul>
            </details>
            {detail_request_button}
          </section>
          {learned_panel}
          <section class="panel">
            <div class="eyebrow">Executive Brief</div>
            <h2>What this means</h2>
            <div class="ooda-grid">
              <div class="ooda-cell"><b>Strongest practical upside</b>{html.escape((highlight_lines or ['No clear upside has been stored yet.'])[0])}</div>
              <div class="ooda-cell"><b>Hardest practical downside</b>{html.escape((concern_lines or ['No concrete downside has been stored yet.'])[0])}</div>
              <div class="ooda-cell"><b>Most important next check</b>{html.escape((unknown_lines or ['No explicit follow-up question is stored yet.'])[0])}</div>
            </div>
          </section>
        </div>
      </div>
    </div>
    <script nonce="{nonce_attr}">
      const requestButton = document.getElementById("request-details-btn");
      const requestStatus = document.getElementById("request-details-status");
      let selectedReaction = "";
      const selectedReasons = new Set();
      const reactionButtons = [...document.querySelectorAll(".reaction-btn")];
      const reasonButtons = [...document.querySelectorAll(".reason-chip[data-reason-key]")];
      const filterButtons = [...document.querySelectorAll(".filter-chip[data-filter-key]")];
      const feedbackSubmitButton = document.getElementById("feedback-submit-btn");
      const feedbackStatus = document.getElementById("feedback-status");
      const feedbackNote = document.getElementById("feedback-note");
      const filterStatus = document.getElementById("filter-status");
      reactionButtons.forEach((button) => {{
        button.addEventListener("click", () => {{
          selectedReaction = String(button.dataset.reaction || "");
          reactionButtons.forEach((candidate) => candidate.classList.toggle("active", candidate === button));
        }});
      }});
      reasonButtons.forEach((button) => {{
        button.addEventListener("click", () => {{
          const reasonKey = String(button.dataset.reasonKey || "");
          if (!reasonKey) return;
          if (selectedReasons.has(reasonKey)) {{
            selectedReasons.delete(reasonKey);
            button.classList.remove("active");
          }} else {{
            selectedReasons.add(reasonKey);
            button.classList.add("active");
          }}
        }});
      }});
      if (feedbackSubmitButton && feedbackStatus) {{
        feedbackSubmitButton.addEventListener("click", async () => {{
          if (!selectedReaction) {{
            feedbackStatus.textContent = "Choose a reaction first.";
            return;
          }}
          feedbackSubmitButton.disabled = true;
          feedbackStatus.textContent = "Saving feedback...";
          try {{
            const response = await fetch(window.location.pathname + "/feedback", {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{
                reaction: selectedReaction,
                reason_keys: [...selectedReasons],
                note: feedbackNote ? feedbackNote.value : "",
              }}),
            }});
            const payload = await response.json();
            if (!response.ok) {{
              feedbackStatus.textContent = payload.status === "not_captured"
                ? "Feedback could not be saved. Please retry from your signed-in account."
                : "Could not save feedback right now.";
              return;
            }}
            feedbackStatus.textContent = payload.status === "captured_external"
              ? "Feedback captured as an external review signal. Sign in to make it part of your ranking profile."
              : "Feedback captured.";
          }} catch (error) {{
            feedbackSubmitButton.disabled = false;
            feedbackStatus.textContent = "Could not save feedback right now.";
          }}
        }});
      }}
      filterButtons.forEach((button) => {{
        button.addEventListener("click", async () => {{
          const filterKey = String(button.dataset.filterKey || "");
          if (!filterKey || !filterStatus) return;
          filterStatus.textContent = "Open the authenticated PropertyQuarry account to change profile filters.";
        }});
      }});
      if (requestButton && requestStatus) {{
        requestButton.addEventListener("click", async () => {{
          requestStatus.textContent = "Open the authenticated PropertyQuarry property page to request deeper research.";
        }});
      }}
    </script>
  </body>
</html>"""
    pure_decision_rows_html = "".join(
        f'<div class="stat"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
        for label, value in decision_rows
    )
    pure_reasons_html = "".join(f"<li>{html.escape(item)}</li>" for item in (highlight_lines or ["No best signal saved yet."]))
    pure_risks_html = "".join(f"<li>{html.escape(item)}</li>" for item in (concern_lines or ["No concrete downside has been stored yet."]))
    pure_unknowns_html = "".join(f"<li>{html.escape(item)}</li>" for item in (unknown_lines or ["No explicit follow-up question is stored yet."]))
    pure_feedback_panel = ""
    if bool(payload.get("_feedback_enabled")) or str(payload.get("principal_id") or "").strip():
        pure_feedback_panel = (
            '<section class="card decision-card">'
            '<div class="eyebrow">Preference Feedback</div>'
            '<h2>Teach the system what to rank higher or lower</h2>'
            '<p class="sub">Give a quick reaction and mark concrete reasons. Public-link feedback is captured as an external signal; sign in to apply it to a ranking profile.</p>'
            '<button class="btn" type="button">Save feedback</button>'
            '</section>'
        )
    pure_shortlist_items = [dict(row) for row in list(shortlist_compare.get("items") or []) if isinstance(row, dict)]
    pure_shortlist_current = dict(shortlist_compare.get("current") or {}) if isinstance(shortlist_compare.get("current"), dict) else {}
    pure_shortlist_rows = ([pure_shortlist_current] if pure_shortlist_current else []) + pure_shortlist_items
    pure_shortlist_cards = "".join(
        (
            '<div class="stat">'
            f'<span>Fit {int(round(float(row.get("score") or 0.0))):d}/100</span>'
            f'<strong>{html.escape(str(row.get("title") or "Property").strip())}</strong>'
            f'<p>{html.escape(str(row.get("why_now") or "No ranking note stored.").strip())}</p>'
            f'<p><b>{html.escape(_public_shortlist_action_label(row.get("recommended_action")))}</b></p>'
            f'<p><span class="shortlist-delta-better">shortlist upside</span> <span class="shortlist-delta-worse">shortlist trade-off</span></p>'
            + ''.join(
                f'<p><b>{html.escape(label)}:</b> {html.escape(_shortlist_metric_display(key, dict(row.get("metrics") or {}).get(key)))}</p>'
                for key, label, _direction in _shortlist_metric_labels()
                if key in dict(row.get("metrics") or {})
            )
            + '</div>'
        )
        for row in pure_shortlist_rows[:3]
    )
    pure_shortlist_panel = (
        '<section class="card decision-card">'
        '<div class="eyebrow">Shortlist</div>'
        '<h2>Current property in the active shortlist</h2>'
        f'<div class="stat-grid">{pure_shortlist_cards or "<div class=\"stat\"><span>Shortlist</span><strong>No shortlist loaded</strong></div>"}</div>'
        '</section>'
    )
    pure_distance_html = "".join(
        f'<div class="stat"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
        for label, value in distance_rows
    )
    pure_decision_panel = (
        '<section class="card decision-card">'
        '<div class="eyebrow">Summary</div>'
        '<h2>Summary</h2>'
        f'<div class="stat-grid">{pure_decision_rows_html}</div>'
        '<div class="decision-grid">'
        '<div><h3>Highlights</h3><ul>' + pure_reasons_html + '</ul></div>'
        '<div><h3>Watch for</h3><ul>' + pure_risks_html + '</ul></div>'
        '<div><h3>To confirm</h3><ul>' + pure_unknowns_html + '</ul></div>'
        '</div>'
        + (f'<h2>Daily-life access</h2><div class="stat-grid">{pure_distance_html}</div>' if pure_distance_html else '')
        + (f'<p class="sub">{html.escape(completed_research_line)}</p>' if completed_research_line else '')
        + '</section>'
    )
    if is_pure_360_cube:
        return f"""<!doctype html>
<html lang="de">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title_html}</title>
    {clickrank_head_snippet(hostname, path)}
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@photo-sphere-viewer/core/index.min.css">
    <style nonce="{nonce_attr}">
      :root {{
        --bg: #f5efe3;
        --panel: rgba(255,255,255,0.82);
        --ink: #1f1c18;
        --muted: #6f665a;
        --line: rgba(31,28,24,0.12);
        --accent: #1f5f51;
        --accent-soft: rgba(31,95,81,0.10);
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        color: var(--ink);
        font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
        background:
          radial-gradient(circle at top left, rgba(31,95,81,0.14), transparent 32%),
          radial-gradient(circle at bottom right, rgba(183,132,40,0.16), transparent 28%),
          linear-gradient(160deg, #f8f4ec 0%, #efe8db 100%);
      }}
      .shell {{
        max-width: 1380px;
        margin: 0 auto;
        padding: 24px;
      }}
      .hero {{
        display: grid;
        grid-template-columns: 1.25fr 0.75fr;
        gap: 18px;
        align-items: start;
      }}
      .card {{
        border-radius: 28px;
        border: 1px solid var(--line);
        background: var(--panel);
        backdrop-filter: blur(14px);
        box-shadow: 0 18px 48px rgba(31,28,24,0.08);
      }}
      .hero-main {{ padding: 28px; }}
      .hero-side {{ padding: 22px; }}
      .eyebrow {{
        display: inline-flex;
        gap: 10px;
        align-items: center;
        font-size: 12px;
        letter-spacing: 0.16em;
        text-transform: uppercase;
        color: var(--muted);
      }}
      h1 {{
        margin: 14px 0 10px;
        font-size: clamp(2rem, 4vw, 4rem);
        line-height: 0.95;
      }}
      .sub {{
        margin: 0;
        color: var(--muted);
        line-height: 1.55;
        max-width: 66ch;
      }}
      .actions {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-top: 20px;
      }}
      .btn {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 44px;
        padding: 0 18px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: rgba(255,255,255,0.72);
        color: var(--ink);
        text-decoration: none;
        cursor: pointer;
      }}
      .btn.primary {{
        background: var(--ink);
        color: #fff9f0;
        border-color: transparent;
      }}
      .stack {{
        display: grid;
        gap: 12px;
      }}
      .kv {{
        padding: 12px 14px;
        border-radius: 18px;
        background: rgba(255,255,255,0.68);
        border: 1px solid rgba(31,28,24,0.08);
      }}
      .kv b {{
        display: block;
        margin-bottom: 4px;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.14em;
        color: var(--muted);
      }}
      .stage {{
        margin-top: 18px;
        display: grid;
        gap: 18px;
      }}
      .decision-card {{
        padding: 22px;
      }}
      .decision-card h2 {{
        margin: 8px 0 14px;
      }}
      .decision-grid, .stat-grid {{
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 12px;
      }}
      .decision-grid {{
        margin-top: 16px;
      }}
      .decision-grid h3 {{
        margin: 0 0 8px;
      }}
      .decision-grid ul {{
        margin: 0;
        padding-left: 18px;
        color: var(--muted);
        line-height: 1.5;
      }}
      .stat {{
        padding: 12px 14px;
        border-radius: 18px;
        background: rgba(255,255,255,0.68);
        border: 1px solid rgba(31,28,24,0.08);
      }}
      .stat span {{
        display: block;
        margin-bottom: 4px;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.14em;
        color: var(--muted);
      }}
      .stat strong {{
        display: block;
      }}
      .stat p {{
        margin: 6px 0 0;
        color: var(--muted);
        line-height: 1.45;
      }}
      .toolbar {{
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
      }}
      .toggle {{
        display: inline-flex;
        gap: 8px;
        flex-wrap: wrap;
      }}
      .toggle button {{
        min-height: 42px;
        padding: 0 14px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: rgba(255,255,255,0.74);
        color: var(--ink);
        cursor: pointer;
      }}
      .toggle button.active {{
        background: var(--ink);
        color: #fff8ef;
        border-color: var(--ink);
      }}
      .toggle button:disabled {{
        opacity: .4;
        cursor: not-allowed;
      }}
      .stage-grid {{
        display: grid;
        grid-template-columns: minmax(0, 1.22fr) minmax(320px, 0.78fr);
        gap: 18px;
      }}
      .viewer-shell {{
        padding: 16px;
      }}
      .pane {{ display: none; }}
      .pane.active {{ display: block; }}
      #cube {{
        min-height: 72vh;
        height: 72vh;
        border-radius: 24px;
        overflow: hidden;
        background: #111;
        border: 1px solid rgba(31,28,24,0.14);
      }}
      .viewer-empty {{
        min-height: 72vh;
        height: 72vh;
        display: grid;
        place-items: center;
        padding: 28px;
        text-align: center;
        color: #fff8ef;
        background: radial-gradient(circle at top, rgba(31,95,81,0.42), rgba(12,12,12,0.94));
      }}
      .viewer-empty strong {{
        display: block;
        margin-bottom: 10px;
        font-size: 1.1rem;
      }}
      .viewer-empty p {{
        max-width: 32rem;
        margin: 0 auto 16px;
        line-height: 1.55;
        color: rgba(255,248,239,0.82);
      }}
      .viewer-empty button {{
        min-height: 40px;
        padding: 0 14px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.18);
        background: rgba(255,255,255,0.10);
        color: #fff8ef;
        cursor: pointer;
      }}
      .overview-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 12px;
      }}
      .overview-card {{
        padding: 16px;
        border-radius: 22px;
        border: 1px solid rgba(31,28,24,0.09);
        background: rgba(255,255,255,0.74);
      }}
      .overview-card strong {{
        display: block;
        margin-bottom: 8px;
      }}
      .overview-card p {{
        margin: 0 0 14px;
        color: var(--muted);
        line-height: 1.5;
      }}
      .overview-card button {{
        min-height: 38px;
        padding: 0 14px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: var(--accent-soft);
        color: var(--accent);
        cursor: pointer;
      }}
      .doc-stage {{
        border-radius: 24px;
        overflow: hidden;
        border: 1px solid rgba(31,28,24,0.12);
        background: rgba(255,255,255,0.88);
        min-height: 72vh;
      }}
      .video-stage {{
        border-radius: 24px;
        overflow: hidden;
        border: 1px solid rgba(31,28,24,0.12);
        background: #0f1012;
        min-height: 72vh;
      }}
      .video-stage video {{
        width: 100%;
        height: 72vh;
        min-height: 72vh;
        display: block;
        object-fit: cover;
        background: #0f1012;
      }}
      .doc-stage iframe,
      .doc-stage img {{
        width: 100%;
        height: 72vh;
        min-height: 72vh;
        display: block;
        border: 0;
        background: #fff;
      }}
      .doc-stage img {{
        object-fit: contain;
      }}
      .sidebar {{
        padding: 18px;
        display: grid;
        gap: 14px;
        align-content: start;
      }}
      .section-title {{
        margin: 0;
        font-size: 1rem;
      }}
      .note {{
        margin: 0;
        color: var(--muted);
        line-height: 1.5;
      }}
      .thumbs {{
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(124px, 1fr));
        gap: 10px;
      }}
      .thumb {{
        position: relative;
        overflow: hidden;
        border-radius: 18px;
        border: 2px solid transparent;
        background: rgba(255,255,255,0.62);
        cursor: pointer;
      }}
      .thumb.active {{ border-color: var(--accent); }}
      .thumb img {{
        width: 100%;
        height: 108px;
        object-fit: cover;
        display: block;
      }}
      .thumb-doc {{
        min-height: 108px;
        display: grid;
        place-items: center;
        background: linear-gradient(135deg, rgba(255,255,255,0.95), rgba(240,233,218,0.92));
        color: var(--ink);
        font-weight: 800;
        letter-spacing: 0.08em;
      }}
      .thumb-badge {{
        position: absolute;
        left: 8px;
        top: 8px;
        padding: 4px 8px;
        border-radius: 999px;
        background: rgba(14,14,13,0.72);
        color: #fffaf2;
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }}
      .scene-list {{
        display: grid;
        gap: 8px;
      }}
      .brief-list {{
        margin: 0;
        padding-left: 18px;
        color: var(--muted);
        line-height: 1.5;
      }}
      .brief-list li + li {{
        margin-top: 6px;
      }}
      .scene-row {{
        display: flex;
        gap: 10px;
        align-items: center;
        justify-content: space-between;
        padding: 10px 12px;
        border-radius: 16px;
        border: 1px solid rgba(31,28,24,0.08);
        background: rgba(255,255,255,0.68);
      }}
      .scene-row.active {{
        background: rgba(31,95,81,0.10);
        border-color: rgba(31,95,81,0.28);
      }}
      .scene-row button {{
        min-height: 34px;
        padding: 0 12px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: #fff;
        cursor: pointer;
      }}
      .status-pill {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 8px 12px;
        border-radius: 999px;
        background: rgba(255,255,255,0.74);
        border: 1px solid var(--line);
        color: var(--muted);
      }}
      .plan-preview {{
        border-radius: 20px;
        overflow: hidden;
        border: 1px solid rgba(31,28,24,0.1);
        background: rgba(255,255,255,0.84);
      }}
      .plan-preview img {{
        width: 100%;
        height: 148px;
        object-fit: cover;
        display: block;
        background: #fff;
      }}
      .plan-preview-doc {{
        min-height: 148px;
        display: grid;
        place-items: center;
        background: linear-gradient(135deg, rgba(255,255,255,0.97), rgba(240,233,218,0.92));
        color: var(--ink);
        font-weight: 800;
        letter-spacing: 0.08em;
      }}
      .plan-preview-copy {{
        padding: 12px 14px 14px;
        display: grid;
        gap: 8px;
      }}
      .plan-preview-copy strong {{
        display: block;
      }}
      .plan-preview-copy button {{
        min-height: 38px;
        padding: 0 14px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: var(--accent-soft);
        color: var(--accent);
        cursor: pointer;
      }}
      @media (max-width: 1040px) {{
        .hero, .stage-grid {{ grid-template-columns: 1fr; }}
      }}
      @media (max-width: 640px) {{
        .shell {{ padding: 14px; }}
        .card {{ border-radius: 22px; }}
        #cube, .doc-stage, .doc-stage iframe, .doc-stage img, .video-stage, .video-stage video {{
          min-height: 56vh;
          height: 56vh;
        }}
      }}
    </style>
  </head>
  <body>
    <div class="shell">
      <section class="hero">
        <div class="card hero-main">
          <div class="eyebrow">PropertyQuarry <span>•</span> 3D tour</div>
          <h1>{title_html}</h1>
          <p class="sub">Move through the tour, photos, and floor plans from one clean page.</p>
          <div class="actions">
            <a class="btn primary" href="#panorama-pane">Open panorama</a>
            {listing_link}
          </div>
        </div>
        <aside class="card hero-side">
          <div class="stack">
            <div class="kv"><b>Hosted by</b>{hosted_brand_html}</div>
            <div class="kv"><b>Location</b>{address or district or 'Location under review'}</div>
            <div class="kv"><b>Scenes</b>{html.escape(str(len([scene for scene in scene_data if scene.get('cube_faces')])))} panorama positions</div>
            <div class="kv"><b>Floor plans</b>{html.escape(str(len([scene for scene in scene_data if str(scene.get('role') or '') == 'floorplan'])))} attached documents</div>
            <div class="kv"><b>Tour</b>Panorama, layout, and photos.</div>
          </div>
        </aside>
      </section>
      <section class="stage">
        {pure_decision_panel}
        {pure_feedback_panel}
        {pure_shortlist_panel}
      </section>
      <section class="stage">
        <div class="toolbar">
          <div class="toggle" id="mode-toggle">
            <button type="button" class="active" data-pane="panorama-pane">Panorama</button>
            <button type="button" data-pane="overview-pane">Overview</button>
            <button type="button" data-pane="floorplan-pane">Floor plans</button>
            {"<button type=\"button\" data-pane=\"flythrough-pane\">Flythrough</button>" if video_url else ""}
          </div>
          <div class="status-pill" id="tour-status">Interactive 3D tour</div>
        </div>
        <div class="stage-grid">
          <div class="card viewer-shell">
            <section id="panorama-pane" class="pane active">
              <div id="cube"></div>
            </section>
            <section id="overview-pane" class="pane">
              <div class="overview-grid" id="overview-grid"></div>
            </section>
            <section id="floorplan-pane" class="pane">
              <div class="doc-stage" id="floorplan-stage">
                <img id="floorplan-image" alt="Floorplan preview" hidden referrerpolicy="no-referrer">
                <iframe id="floorplan-frame" title="Floorplan document" hidden referrerpolicy="no-referrer"></iframe>
              </div>
            </section>
            {"<section id=\"flythrough-pane\" class=\"pane\"><div class=\"video-stage\"><video id=\"flythrough-video\" controls playsinline webkit-playsinline=\"true\" preload=\"auto\">" + video_source_markup + "</video></div></section>" if video_url else ""}
          </div>
          <aside class="card sidebar">
            <h2 class="section-title">Scene navigation</h2>
            <p class="note">Move through the panorama, check the floorplan, then return to the property page with a cleaner room-by-room read.</p>
            <h2 class="section-title">Review route</h2>
            <ol class="brief-list">
              <li>Open the main panorama and get the room proportions.</li>
              <li>Switch to the floorplan to check doors, walls, and usable edges.</li>
              <li>Return to the property page for open questions and next steps.</li>
            </ol>
            <div class="actions">
              <button class="btn" id="prev-link" type="button">Previous</button>
              <button class="btn" id="next-link" type="button">Next</button>
              {"<button class=\"btn\" id=\"open-flythrough\" type=\"button\">Play flythrough</button>" if video_url else ""}
            </div>
            <div class="scene-list" id="scene-list"></div>
            <h2 class="section-title">Layout preview</h2>
            <div id="layout-preview" class="plan-preview">
              <div class="plan-preview-doc">No plan</div>
              <div class="plan-preview-copy">
                <strong>Layout preview unavailable</strong>
                <p class="note">This tour currently has no stored floorplan document.</p>
              </div>
            </div>
            <h2 class="section-title">Media deck</h2>
            <div class="thumbs" id="thumbs"></div>
          </aside>
        </div>
      </section>
    </div>
    <script nonce="{nonce_attr}" id="scene-data" type="application/json">{data_json}</script>
    <script nonce="{nonce_attr}" type="importmap">
      {{
        "imports": {{
          "three": "https://cdn.jsdelivr.net/npm/three/build/three.module.js",
          "@photo-sphere-viewer/core": "https://cdn.jsdelivr.net/npm/@photo-sphere-viewer/core/index.module.js",
          "@photo-sphere-viewer/cubemap-adapter": "https://cdn.jsdelivr.net/npm/@photo-sphere-viewer/cubemap-adapter/index.module.js"
        }}
      }}
    </script>
    <script nonce="{nonce_attr}" type="module">
      import {{ Viewer }} from '@photo-sphere-viewer/core';
      import {{ CubemapAdapter }} from '@photo-sphere-viewer/cubemap-adapter';

      const sceneData = JSON.parse(document.getElementById("scene-data").textContent);
      const panoramaScenes = sceneData.filter((scene) => scene.cube_faces && scene.cube_faces.f);
      const floorplanScenes = sceneData.filter((scene) => String(scene.role || "").trim() === "floorplan");
      const thumbs = document.getElementById("thumbs");
      const sceneList = document.getElementById("scene-list");
      const overviewGrid = document.getElementById("overview-grid");
      const floorplanImage = document.getElementById("floorplan-image");
      const floorplanFrame = document.getElementById("floorplan-frame");
      const layoutPreview = document.getElementById("layout-preview");
      const flythroughVideo = document.getElementById("flythrough-video");
      const modeButtons = [...document.querySelectorAll('#mode-toggle button[data-pane]')];
      const panes = [...document.querySelectorAll('.pane')];
      const floorplanButton = modeButtons.find((button) => button.dataset.pane === 'floorplan-pane');
      if (floorplanButton && floorplanScenes.length === 0) {{
        floorplanButton.disabled = true;
      }}
      let activePanorama = 0;
      let activeFloorplan = 0;
      const viewerContainer = document.querySelector('#cube');
      let viewer = null;
      let viewerInitAttempted = false;

      function showPanoramaFallback(message) {{
        if (!viewerContainer) return;
        const fallback = document.createElement('div');
        fallback.className = 'viewer-empty';
        const content = document.createElement('div');
        const heading = document.createElement('strong');
        heading.textContent = 'Panorama preview unavailable on this device right now';
        const detail = document.createElement('p');
        detail.textContent = String(message || 'Use the overview and floorplan tabs to keep the layout review moving, then reopen the panorama after the connection or browser stabilizes.');
        const fallbackButton = document.createElement('button');
        fallbackButton.type = 'button';
        fallbackButton.id = 'panorama-fallback-overview';
        fallbackButton.textContent = 'Open overview instead';
        content.append(heading, detail, fallbackButton);
        fallback.appendChild(content);
        viewerContainer.replaceChildren(fallback);
        if (fallbackButton) {{
          fallbackButton.addEventListener('click', () => switchPane(floorplanScenes.length ? 'floorplan-pane' : 'overview-pane'));
        }}
        if (panoramaScenes.length) {{
          document.getElementById('tour-status').textContent = 'Panorama unavailable · showing the overview';
        }}
      }}

      function ensurePanoramaViewer() {{
        if (!panoramaScenes.length || !viewerContainer) return viewer;
        if (viewer) return viewer;
        if (viewerInitAttempted) return null;
        viewerInitAttempted = true;
        try {{
          viewer = new Viewer({{
            container: viewerContainer,
            adapter: CubemapAdapter,
            navbar: ['zoom', 'move', 'fullscreen'],
            mousewheel: true,
            touchmoveTwoFingers: false,
            defaultZoomLvl: 42,
            panorama: {{
              left: panoramaScenes[0].cube_faces.l,
              front: panoramaScenes[0].cube_faces.f,
              right: panoramaScenes[0].cube_faces.r,
              back: panoramaScenes[0].cube_faces.b,
              top: panoramaScenes[0].cube_faces.u,
              bottom: panoramaScenes[0].cube_faces.d,
            }},
          }});
          return viewer;
        }} catch (error) {{
          console.error('PropertyQuarry panorama init failed', error);
          showPanoramaFallback('The panorama could not initialize here. The overview and floor plans stay available so the property page remains useful on mobile.');
          return null;
        }}
      }}

      function switchPane(name) {{
        panes.forEach((pane) => pane.classList.toggle('active', pane.id === name));
        modeButtons.forEach((button) => button.classList.toggle('active', button.dataset.pane === name));
        if (name === 'panorama-pane') {{
          ensurePanoramaViewer();
          document.getElementById('tour-status').textContent = `Panorama · ${{panoramaScenes[activePanorama]?.name || `Scene ${{activePanorama + 1}}`}}`;
        }} else if (name === 'flythrough-pane') {{
          document.getElementById('tour-status').textContent = 'Flythrough · interior route';
        }} else if (name === 'floorplan-pane' && floorplanScenes.length) {{
          document.getElementById('tour-status').textContent = `Floorplan · ${{floorplanScenes[activeFloorplan]?.name || `Plan ${{activeFloorplan + 1}}`}}`;
        }}
      }}

      async function autoplayFlythrough() {{
        if (!flythroughVideo || typeof flythroughVideo.play !== 'function') return;
        flythroughVideo.defaultMuted = true;
        flythroughVideo.muted = true;
        flythroughVideo.autoplay = true;
        flythroughVideo.playsInline = true;
        flythroughVideo.setAttribute("muted", "");
        flythroughVideo.setAttribute("autoplay", "");
        flythroughVideo.setAttribute("playsinline", "");
        const attemptPlay = async () => {{
          try {{
            await flythroughVideo.play();
          }} catch (_error) {{
            flythroughVideo.controls = true;
          }}
        }};
        if (flythroughVideo.readyState >= 2) {{
          await attemptPlay();
          return;
        }}
        const once = () => {{
          flythroughVideo.removeEventListener("loadedmetadata", once);
          flythroughVideo.removeEventListener("canplay", once);
          void attemptPlay();
        }};
        flythroughVideo.addEventListener("loadedmetadata", once, {{ once: true }});
        flythroughVideo.addEventListener("canplay", once, {{ once: true }});
        try {{
          flythroughVideo.load();
        }} catch (_error) {{
          void attemptPlay();
        }}
      }}

      function setPanoramaScene(index) {{
        activePanorama = ((index % panoramaScenes.length) + panoramaScenes.length) % panoramaScenes.length;
        if (!panoramaScenes.length) return;
        const activeViewer = ensurePanoramaViewer();
        if (!activeViewer) return;
        const scene = panoramaScenes[activePanorama];
        try {{
          activeViewer.setPanorama({{
            left: scene.cube_faces.l,
            front: scene.cube_faces.f,
            right: scene.cube_faces.r,
            back: scene.cube_faces.b,
            top: scene.cube_faces.u,
            bottom: scene.cube_faces.d,
          }});
        }} catch (error) {{
          console.error('PropertyQuarry panorama scene switch failed', error);
          showPanoramaFallback('This panorama scene could not be rendered cleanly on the current device. Use the scene overview or floorplan lane for the layout-first review.');
          switchPane(floorplanScenes.length ? 'floorplan-pane' : 'overview-pane');
          return;
        }}
        [...sceneList.children].forEach((node, sceneIndex) => node.classList.toggle('active', sceneIndex === activePanorama));
        [...thumbs.children].forEach((node) => {{
          const role = String(node.dataset.role || '');
          const sceneIndex = Number.parseInt(String(node.dataset.index || '-1'), 10);
          node.classList.toggle('active', role === 'pure_360' && sceneIndex === activePanorama);
        }});
        const target = new URL(window.location.href);
        const sceneId = scene.scene_id || String(activePanorama + 1);
        if (sceneId && sceneId !== '1') target.searchParams.set('scene', sceneId);
        else target.searchParams.delete('scene');
        target.hash = '';
        history.replaceState({{}}, '', target.pathname + (target.search || ''));
        document.getElementById('tour-status').textContent = `Panorama · ${{scene.name || `Scene ${{activePanorama + 1}}`}}`;
      }}

      function setFloorplan(index) {{
        if (!floorplanScenes.length) return;
        activeFloorplan = ((index % floorplanScenes.length) + floorplanScenes.length) % floorplanScenes.length;
        const scene = floorplanScenes[activeFloorplan];
        const url = String(scene.image_url || '');
        const isPdf = String(scene.mime_type || '').includes('pdf') || /\\.pdf(?:$|[?#])/i.test(url);
        if (isPdf) {{
          floorplanImage.hidden = true;
          floorplanFrame.hidden = false;
          floorplanFrame.src = url;
        }} else {{
          floorplanFrame.hidden = true;
          floorplanFrame.src = '';
          floorplanImage.hidden = false;
          floorplanImage.src = url;
        }}
        [...thumbs.children].forEach((node) => {{
          const role = String(node.dataset.role || '');
          const sceneIndex = Number.parseInt(String(node.dataset.index || '-1'), 10);
          node.classList.toggle('active', role === 'floorplan' && sceneIndex === activeFloorplan);
        }});
      }}

      function renderLayoutPreview() {{
        if (!layoutPreview) return;
        layoutPreview.replaceChildren();
        if (!floorplanScenes.length) {{
          const documentLabel = document.createElement('div');
          documentLabel.className = 'plan-preview-doc';
          documentLabel.textContent = 'No plan';
          const copy = document.createElement('div');
          copy.className = 'plan-preview-copy';
          const heading = document.createElement('strong');
          heading.textContent = 'Layout preview unavailable';
          const note = document.createElement('p');
          note.className = 'note';
          note.textContent = 'This tour currently has no stored floorplan document.';
          copy.append(heading, note);
          layoutPreview.append(documentLabel, copy);
          return;
        }}
        const scene = floorplanScenes[0];
        const url = String(scene.image_url || '');
        const isPdf = String(scene.mime_type || '').includes('pdf') || /\\.pdf(?:$|[?#])/i.test(url);
        if (isPdf) {{
          const documentLabel = document.createElement('div');
          documentLabel.className = 'plan-preview-doc';
          documentLabel.textContent = 'PDF';
          layoutPreview.appendChild(documentLabel);
        }} else {{
          const image = document.createElement('img');
          image.src = url;
          image.alt = String(scene.name || 'Floorplan preview');
          image.referrerPolicy = 'no-referrer';
          layoutPreview.appendChild(image);
        }}
        const copy = document.createElement('div');
        copy.className = 'plan-preview-copy';
        const heading = document.createElement('strong');
        heading.textContent = String(scene.name || 'Attached floorplan');
        const note = document.createElement('p');
        note.className = 'note';
        note.textContent = isPdf
          ? 'Open the plan sheet to validate room flow, circulation, and usable edges.'
          : 'Use the layout image as a quick map while reading the panorama.';
        const openButton = document.createElement('button');
        openButton.type = 'button';
        openButton.id = 'layout-preview-open';
        openButton.textContent = 'Open floorplan';
        copy.append(heading, note, openButton);
        layoutPreview.appendChild(copy);
        if (openButton) {{
          openButton.addEventListener('click', () => {{
            switchPane('floorplan-pane');
            setFloorplan(0);
          }});
        }}
      }}

      modeButtons.forEach((button) => {{
        button.addEventListener('click', () => {{
          if (button.disabled) return;
          switchPane(String(button.dataset.pane || 'panorama-pane'));
        }});
      }});

      document.getElementById('prev-link').addEventListener('click', () => setPanoramaScene(activePanorama - 1));
      document.getElementById('next-link').addEventListener('click', () => setPanoramaScene(activePanorama + 1));
      const openFlythrough = document.getElementById('open-flythrough');
      if (openFlythrough) {{
        openFlythrough.addEventListener('click', () => {{
          switchPane('flythrough-pane');
          void autoplayFlythrough();
        }});
      }}
      window.addEventListener('keydown', (event) => {{
        if (event.key === 'ArrowLeft') setPanoramaScene(activePanorama - 1);
        if (event.key === 'ArrowRight') setPanoramaScene(activePanorama + 1);
      }});

      panoramaScenes.forEach((scene, index) => {{
        const row = document.createElement('div');
        row.className = 'scene-row';
        const rowCopy = document.createElement('div');
        const rowHeading = document.createElement('strong');
        rowHeading.textContent = String(scene.name || `Scene ${{index + 1}}`);
        const rowNote = document.createElement('div');
        rowNote.className = 'note';
        rowNote.textContent = `Panorama position ${{index + 1}}`;
        const rowButton = document.createElement('button');
        rowButton.type = 'button';
        rowButton.textContent = 'Open';
        rowCopy.append(rowHeading, rowNote);
        row.append(rowCopy, rowButton);
        rowButton.addEventListener('click', () => {{
          switchPane('panorama-pane');
          setPanoramaScene(index);
        }});
        sceneList.appendChild(row);

        const card = document.createElement('article');
        card.className = 'overview-card';
        const cardHeading = document.createElement('strong');
        cardHeading.textContent = String(scene.name || `Scene ${{index + 1}}`);
        const cardCopy = document.createElement('p');
        cardCopy.textContent = 'Use this viewpoint for the spatial read before switching into the packet and floorplan review.';
        const cardButton = document.createElement('button');
        cardButton.type = 'button';
        cardButton.textContent = 'View panorama';
        card.append(cardHeading, cardCopy, cardButton);
        cardButton.addEventListener('click', () => {{
          switchPane('panorama-pane');
          setPanoramaScene(index);
        }});
        overviewGrid.appendChild(card);

        const thumb = document.createElement('button');
        thumb.type = 'button';
        thumb.className = 'thumb';
        thumb.dataset.role = 'pure_360';
        thumb.dataset.index = String(index);
        const thumbBadge = document.createElement('span');
        thumbBadge.className = 'thumb-badge';
        thumbBadge.textContent = '360';
        const thumbImage = document.createElement('img');
        thumbImage.src = String(scene.image_url || scene.cube_faces.f || '');
        thumbImage.alt = String(scene.name || `Scene ${{index + 1}}`);
        thumbImage.referrerPolicy = 'no-referrer';
        thumb.append(thumbBadge, thumbImage);
        thumb.addEventListener('click', () => {{
          switchPane('panorama-pane');
          setPanoramaScene(index);
        }});
        thumbs.appendChild(thumb);
      }});

      floorplanScenes.forEach((scene, index) => {{
        const thumb = document.createElement('button');
        thumb.type = 'button';
        thumb.className = 'thumb';
        thumb.dataset.role = 'floorplan';
        thumb.dataset.index = String(index);
        const isPdf = String(scene.mime_type || '').includes('pdf') || /\\.pdf(?:$|[?#])/i.test(String(scene.image_url || ''));
        const thumbBadge = document.createElement('span');
        thumbBadge.className = 'thumb-badge';
        thumbBadge.textContent = 'Plan';
        thumb.appendChild(thumbBadge);
        if (isPdf) {{
          const documentLabel = document.createElement('div');
          documentLabel.className = 'thumb-doc';
          documentLabel.textContent = 'PDF';
          thumb.appendChild(documentLabel);
        }} else {{
          const thumbImage = document.createElement('img');
          thumbImage.src = String(scene.image_url || '');
          thumbImage.alt = String(scene.name || `Floorplan ${{index + 1}}`);
          thumbImage.referrerPolicy = 'no-referrer';
          thumb.appendChild(thumbImage);
        }}
        thumb.addEventListener('click', () => {{
          switchPane('floorplan-pane');
          setFloorplan(index);
        }});
        thumbs.appendChild(thumb);

        const card = document.createElement('article');
        card.className = 'overview-card';
        const cardHeading = document.createElement('strong');
        cardHeading.textContent = String(scene.name || `Floorplan ${{index + 1}}`);
        const cardCopy = document.createElement('p');
        cardCopy.textContent = 'Use the layout sheet to validate room flow, circulation, and usable edges before a viewing.';
        const cardButton = document.createElement('button');
        cardButton.type = 'button';
        cardButton.textContent = 'Open floorplan';
        card.append(cardHeading, cardCopy, cardButton);
        cardButton.addEventListener('click', () => {{
          switchPane('floorplan-pane');
          setFloorplan(index);
        }});
        overviewGrid.appendChild(card);
      }});

      const initialParams = new URLSearchParams(window.location.search);
      const initialScene = initialParams.get('scene');
      const initialPane = initialParams.get('pane');
      const initialAutoplay = initialParams.get('autoplay');
      const initialSceneIndex = panoramaScenes.findIndex((scene) => String(scene.scene_id || '').trim() === String(initialScene || '').trim());
      const initialPanoramaIndex = initialSceneIndex >= 0 ? initialSceneIndex : 0;
      activePanorama = initialPanoramaIndex;
      if (!panoramaScenes.length) {{
        document.getElementById('tour-status').textContent = 'No panorama scenes stored';
      }}
      if (floorplanScenes.length) {{
        setFloorplan(0);
      }}
      renderLayoutPreview();
      const autoplayOnlyFlythrough = !initialPane && initialAutoplay === '1' && flythroughVideo;
      if (flythroughVideo && (initialPane === 'flythrough-pane' || autoplayOnlyFlythrough)) {{
        switchPane('flythrough-pane');
        if (initialAutoplay === '1') {{
          autoplayFlythrough();
        }}
      }} else if (initialPane === 'floorplan-pane' && floorplanScenes.length) {{
        switchPane('floorplan-pane');
      }} else if (initialPane === 'overview-pane') {{
        switchPane('overview-pane');
      }} else if (panoramaScenes.length) {{
        ensurePanoramaViewer();
        if (viewer) {{
          setPanoramaScene(initialPanoramaIndex);
        }} else {{
          showPanoramaFallback('The panorama is currently unavailable here. Use the overview and floor plans while the 3D scene is unavailable.');
        }}
      }}
    </script>
  </body>
</html>"""
    live_shell = (
        f'''
        <section id="live-360" class="live-shell">
          <div class="live-head">
            <div>
              <div class="eyebrow">{brand_html} <span>•</span> 3D tour</div>
              <h2>Interactive tour</h2>
              <p class="sub">Open the interactive tour below without leaving this page.</p>
            </div>
            <div class="stack">
              <div class="kv"><b>Brand</b>{brand_html}</div>
              <div class="kv"><b>Link</b>{html.escape(hostname or 'this domain')}</div>
            </div>
          </div>
          <div class="live-frame-wrap">
            <iframe
              class="live-frame"
              src="{html.escape(source_virtual_tour_url)}"
              title="{title_html} live 360 viewer"
              loading="lazy"
              allowfullscreen
              referrerpolicy="no-referrer"
            ></iframe>
          </div>
        </section>'''
        if source_virtual_tour_url
        else ""
    )
    provider_access_shell = (
        f'''
        <section id="provider-views" class="live-shell">
          <div class="live-head">
            <div>
              <div class="eyebrow">{brand_html} <span>•</span> 3D tour</div>
              <h2>Open the prepared tour view</h2>
              <p class="sub">Open the tour or walkthrough that is ready for this property.</p>
            </div>
            <div class="actions">
              {provider_actions_html}
            </div>
          </div>
        </section>'''
        if provider_actions_html
        else ""
    )
    legacy_decision_rows_html = "".join(
        f'<div class="kv"><b>{html.escape(label)}</b>{html.escape(value)}</div>'
        for label, value in decision_rows
    )
    legacy_reasons_html = "".join(f"<li>{html.escape(item)}</li>" for item in (highlight_lines or ["No best signal saved yet."]))
    legacy_risks_html = "".join(f"<li>{html.escape(item)}</li>" for item in (concern_lines or ["No concrete downside has been stored yet."]))
    legacy_unknowns_html = "".join(f"<li>{html.escape(item)}</li>" for item in (unknown_lines or ["No explicit follow-up question is stored yet."]))
    feedback_negative = [dict(row) for row in list(feedback_suggestions.get("negative") or []) if isinstance(row, dict)]
    feedback_positive = [dict(row) for row in list(feedback_suggestions.get("positive") or []) if isinstance(row, dict)]
    legacy_feedback_chips = "".join(
        f'<span class="chip">{html.escape(str(row.get("label") or row.get("key") or "").strip())}</span>'
        for row in [*feedback_negative[:4], *feedback_positive[:4]]
        if str(row.get("label") or row.get("key") or "").strip()
    )
    legacy_feedback_panel = ""
    if bool(payload.get("_feedback_enabled")) or str(payload.get("principal_id") or "").strip():
        legacy_feedback_panel = (
            '<section class="panel">'
            '<div class="eyebrow">Preference Feedback</div>'
            '<h2>Teach the system what to rank higher or lower</h2>'
            '<p class="sub">Give a quick reaction and mark concrete reasons. Public-link feedback is captured as an external signal; sign in to apply it to a ranking profile.</p>'
            f'<div class="facts">{legacy_feedback_chips or "<span class=\"chip\">No structured feedback chips yet</span>"}</div>'
            '</section>'
        )
    legacy_shortlist_items = [dict(row) for row in list(shortlist_compare.get("items") or []) if isinstance(row, dict)]
    legacy_shortlist_current = dict(shortlist_compare.get("current") or {}) if isinstance(shortlist_compare.get("current"), dict) else {}
    legacy_shortlist_rows = ([legacy_shortlist_current] if legacy_shortlist_current else []) + legacy_shortlist_items
    legacy_shortlist_cards = "".join(
        (
            '<div class="kv">'
            f'<b>{html.escape(str(row.get("title") or "Property").strip())}</b>'
            f'{html.escape(str(row.get("why_now") or row.get("score_label") or "No ranking note stored.").strip())}'
            f' · {html.escape(_public_shortlist_action_label(row.get("recommended_action")))}'
            '</div>'
        )
        for row in legacy_shortlist_rows[:3]
    )
    legacy_shortlist_panel = (
        '<section class="panel">'
        '<div class="eyebrow">Shortlist</div>'
        '<h2>Current property in the active shortlist</h2>'
        f'<div class="stack">{legacy_shortlist_cards or "<div class=\"kv\"><b>No shortlist loaded</b>No other active shortlist property is currently available.</div>"}</div>'
        '</section>'
    )
    legacy_decision_panel = (
        '<section class="panel">'
        '<div class="eyebrow">Summary</div>'
        '<h2>Summary</h2>'
        f'<div class="stack">{legacy_decision_rows_html}</div>'
        '<div class="stage">'
        '<div class="panel"><h2>Highlights</h2><ul>' + legacy_reasons_html + '</ul></div>'
        '<div class="panel"><h2>Watch for</h2><ul>' + legacy_risks_html + '</ul></div>'
        '<div class="panel"><h2>To confirm</h2><ul>' + legacy_unknowns_html + '</ul></div>'
        '</div>'
        '</section>'
    )
    clickrank_html = clickrank_head_snippet(hostname, path)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title_html}</title>
    {clickrank_html}
    <style nonce="{nonce_attr}">
      :root {{
        --bg: #f3eee3;
        --panel: rgba(255,255,255,0.76);
        --ink: #1d1c1a;
        --muted: #6e6658;
        --accent: #9f2f22;
        --edge: rgba(29,28,26,0.12);
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        color: var(--ink);
        font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
        background:
          radial-gradient(circle at top left, rgba(159,47,34,0.18), transparent 34%),
          radial-gradient(circle at bottom right, rgba(29,28,26,0.10), transparent 30%),
          linear-gradient(160deg, #f8f4eb 0%, #ece5d8 100%);
      }}
      .shell {{
        max-width: 1220px;
        margin: 0 auto;
        padding: 24px;
      }}
      .hero {{
        display: grid;
        grid-template-columns: 1.2fr 0.8fr;
        gap: 22px;
        align-items: start;
      }}
      .mast, .panel {{
        background: var(--panel);
        backdrop-filter: blur(14px);
        border: 1px solid var(--edge);
        border-radius: 28px;
        box-shadow: 0 18px 50px rgba(29,28,26,0.08);
      }}
      .mast {{
        padding: 28px;
      }}
      .eyebrow {{
        display: inline-flex;
        gap: 10px;
        align-items: center;
        font-size: 12px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--muted);
      }}
      h1 {{
        margin: 16px 0 10px;
        font-size: clamp(2rem, 4vw, 4.2rem);
        line-height: 0.95;
      }}
      .sub {{
        margin: 0;
        color: var(--muted);
        font-size: 1rem;
        line-height: 1.55;
        max-width: 65ch;
      }}
      .facts {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin: 20px 0 22px;
      }}
      .chip {{
        padding: 10px 14px;
        border-radius: 999px;
        background: rgba(255,255,255,0.72);
        border: 1px solid rgba(29,28,26,0.09);
        font-size: 14px;
      }}
      .actions {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin-top: 18px;
      }}
      a {{
        color: inherit;
        text-decoration: none;
      }}
      .ghost, .cta {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 44px;
        padding: 0 18px;
        border-radius: 999px;
        border: 1px solid var(--edge);
      }}
      .cta {{
        background: var(--ink);
        color: #fff9f1;
        border-color: transparent;
      }}
      .panel {{
        padding: 22px;
      }}
      .panel h2 {{
        margin: 0 0 10px;
        font-size: 1.1rem;
      }}
      .stack {{
        display: grid;
        gap: 12px;
      }}
      .kv {{
        padding: 12px 14px;
        border-radius: 18px;
        background: rgba(255,255,255,0.7);
        border: 1px solid rgba(29,28,26,0.07);
      }}
      .kv b {{
        display: block;
        margin-bottom: 4px;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.14em;
        color: var(--muted);
      }}
      .stage {{
        margin-top: 22px;
        display: grid;
        gap: 18px;
      }}
      .live-shell {{
        display: grid;
        gap: 16px;
        padding: 22px;
        border-radius: 30px;
        background: rgba(255,255,255,0.76);
        border: 1px solid rgba(29,28,26,0.12);
        box-shadow: 0 18px 50px rgba(29,28,26,0.08);
      }}
      .live-head {{
        display: grid;
        grid-template-columns: 1.2fr 0.8fr;
        gap: 18px;
        align-items: start;
      }}
      .live-head h2 {{
        margin: 8px 0 10px;
        font-size: 1.6rem;
      }}
      .live-frame-wrap {{
        overflow: hidden;
        border-radius: 30px;
        background: rgba(18,17,16,0.94);
        border: 1px solid rgba(29,28,26,0.15);
        min-height: 540px;
      }}
      .live-frame {{
        display: block;
        width: 100%;
        height: 78vh;
        min-height: 540px;
        border: 0;
        background: #111;
      }}
      .hero-video {{
        overflow: hidden;
        border-radius: 30px;
        background: rgba(18,17,16,0.94);
        border: 1px solid rgba(29,28,26,0.15);
        box-shadow: 0 18px 50px rgba(29,28,26,0.08);
      }}
      .hero-video video {{
        display: block;
        width: 100%;
        min-height: 360px;
        max-height: 78vh;
        background: #111;
      }}
      .tour-toolbar {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
      }}
      .toggle {{
        display: inline-flex;
        gap: 8px;
        flex-wrap: wrap;
      }}
      .toggle button {{
        min-height: 42px;
        padding: 0 14px;
        border-radius: 999px;
        border: 1px solid rgba(29,28,26,0.10);
        background: rgba(255,255,255,0.72);
        color: var(--ink);
        cursor: pointer;
      }}
      .toggle button.active {{
        background: var(--ink);
        color: #fff8ef;
        border-color: var(--ink);
      }}
      .viewer {{
        position: relative;
        overflow: hidden;
        border-radius: 30px;
        background: rgba(18,17,16,0.94);
        min-height: 420px;
        border: 1px solid rgba(29,28,26,0.15);
      }}
      .viewer img,
      .viewer iframe {{
        width: 100%;
        height: 72vh;
        max-height: 760px;
        min-height: 420px;
        display: block;
        border: 0;
      }}
      .viewer img {{
        object-fit: contain;
      }}
      .viewer iframe {{
        background: #fff;
      }}
      .caption {{
        position: absolute;
        left: 18px;
        bottom: 18px;
        padding: 12px 16px;
        max-width: min(90%, 520px);
        border-radius: 18px;
        background: rgba(11,11,10,0.64);
        color: #fffaf2;
      }}
      .caption small {{
        display: block;
        opacity: 0.72;
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }}
      .nav {{
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0 14px;
        pointer-events: none;
      }}
      .nav button {{
        pointer-events: auto;
        width: 52px;
        height: 52px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.18);
        background: rgba(255,255,255,0.08);
        color: #fffaf2;
        font-size: 20px;
        cursor: pointer;
      }}
      .thumbs {{
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
        gap: 10px;
      }}
      .thumb {{
        position: relative;
        overflow: hidden;
        border-radius: 18px;
        border: 2px solid transparent;
        background: rgba(255,255,255,0.6);
        cursor: pointer;
      }}
      .thumb.active {{
        border-color: var(--accent);
      }}
      .thumb.hidden {{
        display: none;
      }}
      .thumb img {{
        width: 100%;
        height: 104px;
        object-fit: cover;
        display: block;
      }}
      .thumb-doc {{
        min-height: 104px;
        display: grid;
        place-items: center;
        color: var(--ink);
        font-weight: 800;
        letter-spacing: 0.08em;
        background: linear-gradient(135deg, rgba(255,255,255,0.94), rgba(241,231,214,0.86));
      }}
      .badge {{
        position: absolute;
        left: 8px;
        top: 8px;
        padding: 4px 8px;
        border-radius: 999px;
        background: rgba(11,11,10,0.72);
        color: #fffaf2;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }}
      @media (max-width: 900px) {{
        .hero {{ grid-template-columns: 1fr; }}
        .live-head {{ grid-template-columns: 1fr; }}
      }}
      @media (max-width: 640px) {{
        .shell {{ padding: 14px; }}
        .mast, .panel {{ border-radius: 22px; }}
        .viewer img {{ min-height: 320px; height: 52vh; }}
        .live-frame-wrap, .live-shell {{ border-radius: 22px; }}
        .live-frame {{ min-height: 380px; height: 60vh; }}
      }}
    </style>
  </head>
  <body>
    <div class="shell">
      <section class="hero">
        <div class="mast">
          <div class="eyebrow">3D tour{f' <span>•</span> {variant_label}' if variant_label else ''}</div>
          <h1>{title_html}</h1>
          <p class="sub">{display_html}</p>
          <div class="facts">
            {rooms_legacy_chip_html}
            {area_legacy_chip_html}
            {rent_legacy_chip_html}
            {availability_legacy_chip_html}
            <div class="chip">{html.escape(str(payload.get("scene_count") or len(scenes)))} views</div>
          </div>
          <p class="sub">{teaser}</p>
          <div class="actions">
            <a class="cta" href="{primary_cta_href}">{primary_cta}</a>
            {listing_link}
            {hosted_link}
            {provider_actions_html}
          </div>
        </div>
        {tour_brief_panel}
      </section>
      <section class="stage">
        {legacy_decision_panel}
        {legacy_feedback_panel}
        {legacy_shortlist_panel}
      </section>
      <section class="stage">
        {live_shell}
        {provider_access_shell}
        {(
            f'''<div class="hero-video">
              <video id="tour-video" controls playsinline preload="metadata" poster="{html.escape(scene_data[0]["image_url"])}">
                {video_source_markup}
              </video>
            </div>'''
        ) if video_url else ''}
        <div class="tour-toolbar">
          <div class="toggle" id="role-filter">
            <button type="button" class="active" data-role="all">All views</button>
            <button type="button" data-role="photo">Photos</button>
            <button type="button" data-role="floorplan">Floor plans</button>
          </div>
          <div class="toggle">
            <button type="button" id="autoplay-btn">Autoplay Scenes</button>
          </div>
        </div>
        <div id="viewer" class="viewer">
          <img id="stage-image" src="{html.escape(scene_data[0]['image_url'])}" alt="{html.escape(scene_data[0]['name'])}" referrerpolicy="no-referrer">
          <iframe id="stage-frame" src="" title="{html.escape(scene_data[0]['name'])}" referrerpolicy="no-referrer" hidden></iframe>
          <div class="caption">
            <small id="stage-role">{html.escape(scene_data[0]['role'])}</small>
            <div id="stage-name">{html.escape(scene_data[0]['name'])}</div>
          </div>
          <div class="nav">
            <button id="prev-btn" aria-label="Previous scene">‹</button>
            <button id="next-btn" aria-label="Next scene">›</button>
          </div>
        </div>
        <div id="thumbs" class="thumbs"></div>
      </section>
    </div>
    <script nonce="{nonce_attr}" id="scene-data" type="application/json">{data_json}</script>
    <script nonce="{nonce_attr}">
      const scenes = JSON.parse(document.getElementById("scene-data").textContent);
      let activeIndex = 0;
      const stageImage = document.getElementById("stage-image");
      const stageFrame = document.getElementById("stage-frame");
      const stageName = document.getElementById("stage-name");
      const stageRole = document.getElementById("stage-role");
      const thumbs = document.getElementById("thumbs");
      const autoplayButton = document.getElementById("autoplay-btn");
      const tourVideo = document.getElementById("tour-video");
      let autoplayHandle = null;
      let activeRoleFilter = "all";
      function visibleSceneIndexes() {{
        return scenes
          .map((scene, index) => (activeRoleFilter === "all" || scene.role === activeRoleFilter ? index : -1))
          .filter((index) => index >= 0);
      }}
      function renderThumbs() {{
        thumbs.replaceChildren();
        scenes.forEach((scene, index) => {{
          const button = document.createElement("button");
          button.className = "thumb" + (index === activeIndex ? " active" : "");
          button.type = "button";
          if (activeRoleFilter !== "all" && scene.role !== activeRoleFilter) button.classList.add("hidden");
          const isPdf = String(scene.mime_type || "").includes("pdf") || /\\.pdf(?:$|[?#])/i.test(String(scene.image_url || ""));
          const badge = document.createElement("span");
          badge.className = "badge";
          badge.textContent = String(scene.role || "view");
          button.appendChild(badge);
          if (isPdf) {{
            const documentLabel = document.createElement("span");
            documentLabel.className = "thumb-doc";
            documentLabel.textContent = "PDF";
            button.appendChild(documentLabel);
          }} else {{
            const image = document.createElement("img");
            image.src = String(scene.image_url || "");
            image.alt = String(scene.name || "Scene");
            image.referrerPolicy = "no-referrer";
            button.appendChild(image);
          }}
          button.addEventListener("click", () => setActive(index));
          thumbs.appendChild(button);
        }});
      }}
      function setActive(index) {{
        activeIndex = (index + scenes.length) % scenes.length;
        const scene = scenes[activeIndex];
        const isPdf = String(scene.mime_type || "").includes("pdf") || /\\.pdf(?:$|[?#])/i.test(String(scene.image_url || ""));
        if (isPdf) {{
          stageFrame.src = scene.image_url;
          stageFrame.title = scene.name;
          stageFrame.hidden = false;
          stageImage.hidden = true;
        }} else {{
          stageImage.src = scene.image_url;
          stageImage.alt = scene.name;
          stageImage.hidden = false;
          stageFrame.hidden = true;
        }}
        stageName.textContent = scene.name;
        stageRole.textContent = scene.role;
        renderThumbs();
      }}
      function shiftVisible(delta) {{
        const visible = visibleSceneIndexes();
        if (!visible.length) return;
        const currentSlot = Math.max(0, visible.indexOf(activeIndex));
        const nextSlot = (currentSlot + delta + visible.length) % visible.length;
        setActive(visible[nextSlot]);
      }}
      document.getElementById("prev-btn").addEventListener("click", () => shiftVisible(-1));
      document.getElementById("next-btn").addEventListener("click", () => shiftVisible(1));
      window.addEventListener("keydown", (event) => {{
        if (event.key === "ArrowLeft") shiftVisible(-1);
        if (event.key === "ArrowRight") shiftVisible(1);
      }});
      document.querySelectorAll("#role-filter button").forEach((button) => {{
        button.addEventListener("click", () => {{
          activeRoleFilter = button.dataset.role || "all";
          document.querySelectorAll("#role-filter button").forEach((candidate) => candidate.classList.toggle("active", candidate === button));
          const visible = visibleSceneIndexes();
          if (visible.length && !visible.includes(activeIndex)) activeIndex = visible[0];
          renderThumbs();
          setActive(activeIndex);
        }});
      }});
      autoplayButton.addEventListener("click", () => {{
        if (autoplayHandle) {{
          clearInterval(autoplayHandle);
          autoplayHandle = null;
          autoplayButton.textContent = "Autoplay Scenes";
          return;
        }}
        autoplayButton.textContent = "Stop Autoplay";
        autoplayHandle = setInterval(() => shiftVisible(1), 2600);
      }});
      async function primeTourVideoPlayback() {{
        if (!tourVideo || typeof tourVideo.play !== "function") return;
        tourVideo.defaultMuted = true;
        tourVideo.muted = true;
        tourVideo.autoplay = true;
        tourVideo.playsInline = true;
        tourVideo.setAttribute("muted", "");
        tourVideo.setAttribute("autoplay", "");
        tourVideo.setAttribute("playsinline", "");
        const attemptPlay = async () => {{
          try {{
            await tourVideo.play();
          }} catch (_error) {{
            tourVideo.controls = true;
          }}
        }};
        if (tourVideo.readyState >= 2) {{
          await attemptPlay();
          return;
        }}
        const once = () => {{
          tourVideo.removeEventListener("loadedmetadata", once);
          tourVideo.removeEventListener("canplay", once);
          void attemptPlay();
        }};
        tourVideo.addEventListener("loadedmetadata", once, {{ once: true }});
        tourVideo.addEventListener("canplay", once, {{ once: true }});
        try {{
          tourVideo.load();
        }} catch (_error) {{
          void attemptPlay();
        }}
      }}
      setActive(0);
      const params = new URLSearchParams(window.location.search);
      if (tourVideo && params.get("autoplay") === "1") {{
        void primeTourVideoPlayback();
      }}
    </script>
  </body>
</html>"""


def _render_tour_unavailable_page(
    request: Request,
    *,
    status_code: int,
    title: str,
    summary: str,
    status_label: str,
    rows: list[dict[str, str]],
) -> HTMLResponse:
    response = public_templates.TemplateResponse(
        request,
        "workspace_link.html",
        _public_context(
            request=request,
            current_nav="product",
            page_title=title,
            principal_id="",
            status=_anonymous_onboarding_status(),
            access_identity=None,
            extra={
                "link_kicker": "Tour link unavailable",
                "link_title": title,
                "link_summary": summary,
                "link_detail_kicker": "Link status",
                "link_detail_title": "What happened",
                "link_status_label": status_label,
                "link_rows": rows,
                "primary_action_href": "/",
                "primary_action_label": "Go to PropertyQuarry",
                "secondary_action_href": "/app/search",
                "secondary_action_label": "Start a search",
            },
        ),
    )
    response.status_code = status_code
    rendered_html = bytes(response.body).decode("utf-8", errors="replace")
    for key, value in _public_tour_security_headers(
        cache_control="no-store",
        script_hashes=_public_tour_inline_csp_hashes(rendered_html, tag_name="script"),
        style_hashes=_public_tour_inline_csp_hashes(rendered_html, tag_name="style"),
    ).items():
        response.headers[key] = value
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response


def _public_tour_csp_nonce() -> str:
    return secrets.token_urlsafe(24)


def _public_tour_normalized_nonce(value: object) -> str:
    normalized = str(value or "").strip()
    return normalized if _PUBLIC_TOUR_NONCE_PATTERN.fullmatch(normalized) else ""


def _public_tour_inline_csp_hashes(html_body: str, *, tag_name: str) -> tuple[str, ...]:
    tag = re.escape(str(tag_name or "").strip().lower())
    if tag not in {"script", "style"}:
        return ()
    pattern = re.compile(rf"<{tag}\b(?P<attrs>[^>]*)>(?P<body>.*?)</{tag}\s*>", flags=re.IGNORECASE | re.DOTALL)
    hashes: list[str] = []
    for match in pattern.finditer(str(html_body or "")):
        attrs = str(match.group("attrs") or "")
        if tag == "script" and re.search(r"\bsrc\s*=", attrs, flags=re.IGNORECASE):
            continue
        digest = hashlib.sha256(str(match.group("body") or "").encode("utf-8")).digest()
        hashes.append("'sha256-" + base64.b64encode(digest).decode("ascii") + "'")
    return tuple(dict.fromkeys(hashes))


def _public_tour_security_headers(
    *,
    cache_control: str = "no-store",
    allow_base_uri_self: bool = False,
    nonce: str = "",
    allow_jsdelivr: bool = False,
    runtime_profile: str = "document",
    script_hashes: tuple[str, ...] = (),
    style_hashes: tuple[str, ...] = (),
) -> dict[str, str]:
    base_uri_policy = "'self'" if allow_base_uri_self else "'none'"
    normalized_nonce = _public_tour_normalized_nonce(nonce)
    normalized_profile = str(runtime_profile or "document").strip().lower()
    external_origins = () if normalized_profile == "generated_viewer" else _public_tour_external_csp_origins()
    media_sources = " ".join(("'self'", "data:", "blob:", *external_origins))
    # Public control routes expose only local assets and verified 3DVista
    # exports. Media host configuration must never silently expand iframe
    # authority (or revive a retired provider).
    frame_origins = () if normalized_profile == "generated_viewer" else _PUBLIC_TOUR_PROVIDER_CSP_ORIGINS
    frame_sources = " ".join(("'self'", *frame_origins))

    script_sources: list[str] = ["'self'"]
    style_sources: list[str] = ["'self'"]
    if normalized_nonce:
        script_sources.append(f"'nonce-{normalized_nonce}'")
        style_sources.append(f"'nonce-{normalized_nonce}'")
    script_sources.extend(value for value in script_hashes if re.fullmatch(r"'sha256-[A-Za-z0-9+/=]+'", value))
    style_sources.extend(value for value in style_hashes if re.fullmatch(r"'sha256-[A-Za-z0-9+/=]+'", value))
    if allow_jsdelivr and normalized_profile != "generated_viewer":
        script_sources.append("https://cdn.jsdelivr.net")
    if normalized_profile == "vendor_export":
        # Vendor-authored 3DVista/Pano2VR exports can require dynamic code and
        # inline runtime styles. Keep those exceptions confined to the export
        # asset route rather than granting them to every public tour document.
        script_sources.extend(("'unsafe-inline'", "'unsafe-eval'", "'wasm-unsafe-eval'", *_PUBLIC_TOUR_PROVIDER_CSP_ORIGINS))
        style_sources.append("'unsafe-inline'")

    script_attr_policy = "'unsafe-inline'" if normalized_profile == "vendor_export" else "'none'"
    style_attr_policy = "'unsafe-inline'" if normalized_profile in {"vendor_export", "generated_viewer"} else "'none'"
    connect_sources = ["'self'"]
    if normalized_profile == "vendor_export":
        connect_sources.extend(_PUBLIC_TOUR_PROVIDER_CSP_ORIGINS)
    directives = (
        "default-src 'none'; "
        f"base-uri {base_uri_policy}; "
        "object-src 'none'; "
        "frame-ancestors 'self'; "
        "form-action 'self'; "
        f"img-src {media_sources}; "
        f"media-src {media_sources}; "
        f"frame-src {frame_sources}; "
        f"script-src {' '.join(dict.fromkeys(script_sources))}; "
        f"script-src-attr {script_attr_policy}; "
        f"style-src {' '.join(dict.fromkeys(style_sources))}; "
        f"style-src-attr {style_attr_policy}; "
        "font-src 'self' data:; "
        f"connect-src {' '.join(dict.fromkeys(connect_sources))}; "
        "worker-src 'self' blob:; "
        "manifest-src 'self'"
    )
    report_only_directives = directives
    if normalized_profile == "vendor_export":
        strict_script_sources = [source for source in script_sources if source not in {"'unsafe-inline'", "'unsafe-eval'", "'wasm-unsafe-eval'"}]
        strict_style_sources = [source for source in style_sources if source != "'unsafe-inline'"]
        report_only_directives = directives.replace(
            f"script-src {' '.join(dict.fromkeys(script_sources))}",
            f"script-src {' '.join(dict.fromkeys(strict_script_sources))}",
        ).replace(
            f"style-src {' '.join(dict.fromkeys(style_sources))}",
            f"style-src {' '.join(dict.fromkeys(strict_style_sources))}",
        ).replace("script-src-attr 'unsafe-inline'", "script-src-attr 'none'").replace(
            "style-src-attr 'unsafe-inline'", "style-src-attr 'none'"
        )
    report_only_directives += f"; report-uri {_PUBLIC_TOUR_CSP_REPORT_PATH}; report-to propertyquarry-csp"
    return {
        "Cache-Control": cache_control,
        "Content-Security-Policy": directives,
        "Content-Security-Policy-Report-Only": report_only_directives,
        "Reporting-Endpoints": f'propertyquarry-csp="{_PUBLIC_TOUR_CSP_REPORT_PATH}"',
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
        "Surrogate-Control": "no-store" if cache_control == "no-store" else cache_control,
    }


def _public_tour_csp_report_location(value: object) -> str:
    normalized = str(value or "").strip()[:2048]
    if normalized in {"inline", "eval", "self", "data", "blob"}:
        return normalized
    safe_url = _public_tour_safe_http_url(normalized)
    if safe_url:
        parsed = urllib.parse.urlsplit(safe_url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path[:1024], "", ""))
    if normalized.startswith("/") and not normalized.startswith("//"):
        parsed = urllib.parse.urlsplit(normalized)
        return parsed.path[:1024]
    return "redacted"


@router.post(_PUBLIC_TOUR_CSP_REPORT_PATH, include_in_schema=False)
async def public_tour_csp_report(request: Request) -> Response:
    report_identity = hashlib.sha256(_public_tour_client_identity(request).encode("utf-8")).hexdigest()[:24]
    try:
        _enforce_public_tour_feedback_memory_rate_limit(key=f"csp-report:{report_identity}", now=time.time())
    except HTTPException:
        # CSP reporters retry aggressively. A quiet 204 avoids amplifying a
        # noisy client while keeping the public report endpoint non-reflective.
        return Response(status_code=204, headers=_public_tour_security_headers())
    content_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/csp-report", "application/reports+json", "application/json"}:
        return Response(status_code=415, headers=_public_tour_security_headers())
    raw_body = await request.body()
    if not raw_body or len(raw_body) > 16_384:
        return Response(status_code=413 if raw_body else 204, headers=_public_tour_security_headers())
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Response(status_code=400, headers=_public_tour_security_headers())
    reports = payload if isinstance(payload, list) else [payload]
    for raw_report in reports[:10]:
        if not isinstance(raw_report, dict):
            continue
        report = raw_report.get("csp-report") or raw_report.get("body") or raw_report
        if not isinstance(report, dict):
            continue
        directive = re.sub(
            r"[^a-z0-9-]",
            "",
            str(report.get("effective-directive") or report.get("effectiveDirective") or report.get("violated-directive") or "unknown").lower(),
        )[:80]
        disposition = re.sub(r"[^a-z]", "", str(report.get("disposition") or "enforce").lower())[:20]
        log.warning(
            "public tour csp violation directive=%s disposition=%s document=%s blocked=%s",
            directive or "unknown",
            disposition or "unknown",
            _public_tour_csp_report_location(report.get("document-uri") or report.get("documentURL") or report.get("url")),
            _public_tour_csp_report_location(report.get("blocked-uri") or report.get("blockedURL")),
        )
    return Response(status_code=204, headers=_public_tour_security_headers())


def _public_tour_generated_viewer_url(slug: str, relpath: object) -> str:
    safe_slug = str(slug or "").strip()
    safe_relpath = _public_tour_safe_asset_relpath(relpath)
    if not safe_slug or "/" in safe_slug or ".." in safe_slug or not safe_relpath:
        return ""
    return (
        f"/tours/viewer/{urllib.parse.quote(safe_slug, safe='')}/"
        f"{urllib.parse.quote(safe_relpath, safe='/')}"
    )


def _public_tour_generated_manifest_source_paths(value: object) -> list[object]:
    paths: list[object] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key or "").strip().lower().replace("-", "_")
            if normalized_key in {
                "source_path",
                "source_uri",
                "source_asset_ref",
                "source_asset_id",
            }:
                paths.append(child)
            else:
                paths.extend(_public_tour_generated_manifest_source_paths(child))
    elif isinstance(value, list):
        for child in value:
            paths.extend(_public_tour_generated_manifest_source_paths(child))
    return paths


def _public_tour_generated_source_path_is_unsafe(value: object) -> bool:
    if not isinstance(value, str):
        return True
    normalized = value.strip().replace("\\", "/")

    def is_local_path(path: str) -> bool:
        lowered_path = path.lower()
        return bool(
            not path
            or "\x00" in path
            or path.startswith(("/", "~"))
            or re.match(r"^[a-zA-Z]:", path)
            or lowered_path.startswith(
                (
                    "file:",
                    "home/",
                    "root/",
                    "tmp/",
                    "var/tmp/",
                    "users/",
                )
            )
        )

    if is_local_path(normalized):
        return True
    lowered = normalized.lower()
    source_path = normalized
    if "://" in lowered:
        scheme, source_path = normalized.split("://", 1)
        if scheme.lower() not in {"pcloud", "property"}:
            return True
        if is_local_path(source_path):
            return True
    if any(part in {"", ".", ".."} for part in source_path.split("/")):
        return True
    return bool(
        re.search(
            r"(?:^|[/._-])(?:pytest(?:-of)?|debug|probe)(?:[/._-]|$)",
            lowered,
        )
    )


def _public_tour_read_bound_file(
    bundle_dir: Path,
    binding: dict[str, object],
    *,
    maximum_size_bytes: int = 8 * 1024 * 1024,
) -> bytes:
    relpath = _public_tour_safe_asset_relpath(binding.get("path"))
    expected_sha256 = str(binding.get("sha256") or "").strip().lower()
    expected_size = binding.get("size_bytes")
    if (
        not relpath
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or type(expected_size) is not int
        or expected_size <= 0
        or expected_size > maximum_size_bytes
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
    ):
        raise HTTPException(status_code=404, detail="tour_viewer_not_found")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC
    descriptors: list[int] = []
    try:
        current_fd = os.open(bundle_dir, directory_flags)
        descriptors.append(current_fd)
        parts = PurePosixPath(relpath).parts
        for part in parts[:-1]:
            current_fd = os.open(part, directory_flags, dir_fd=current_fd)
            descriptors.append(current_fd)
        file_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        descriptors.append(file_fd)
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != expected_size
        ):
            raise HTTPException(status_code=410, detail="tour_viewer_integrity_failed")
        chunks: list[bytes] = []
        remaining = expected_size
        while remaining:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(file_fd)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(content) != expected_size
            or any(getattr(before, field) != getattr(after, field) for field in stable_fields)
            or hashlib.sha256(content).hexdigest() != expected_sha256
        ):
            raise HTTPException(status_code=410, detail="tour_viewer_integrity_failed")
        return content
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="tour_viewer_not_found") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _public_tour_verified_generated_viewer_asset(
    slug: str,
    asset_path: str,
    *,
    payload: dict[str, object] | None = None,
) -> tuple[bytes, dict[str, object], dict[str, object]]:
    loaded_payload = payload or _load_tour_with_private_receipt(slug)
    _require_public_tour_viewable(loaded_payload)
    release = evaluate_public_tour_generated_viewer_release(loaded_payload)
    if not release.get("released"):
        status_code = 410 if release.get("terminal") else 404
        detail = (
            "tour_viewer_no_longer_available"
            if status_code == 410
            else "tour_viewer_not_found"
        )
        raise HTTPException(status_code=status_code, detail=detail)

    safe_relpath = _public_tour_safe_asset_relpath(asset_path)
    bindings = release.get("bindings")
    binding = (
        dict(bindings.get(safe_relpath) or {})
        if isinstance(bindings, dict) and safe_relpath
        else {}
    )
    role = str(binding.get("role") or "").strip().lower()
    mime_type = str(binding.get("mime_type") or "").strip().lower()
    allowed_mime_types = {
        "viewer_document": {"text/html"},
        "viewer_module": {"application/javascript", "text/javascript"},
        "floorplan_texture": {"image/jpeg", "image/png", "image/webp"},
        "photo_texture": {"image/jpeg", "image/png", "image/webp"},
    }
    if (
        not safe_relpath
        or role not in allowed_mime_types
        or mime_type not in allowed_mime_types[role]
        or (
            role == "viewer_document"
            and safe_relpath != release.get("viewer_relpath")
        )
    ):
        raise HTTPException(status_code=404, detail="tour_viewer_not_found")

    bundle_dir = _tour_bundle_dir(slug)
    if bundle_dir is None:
        raise HTTPException(status_code=404, detail="tour_viewer_not_found")
    proof_bindings = [
        dict(row)
        for row in dict(release.get("bindings") or {}).values()
        if isinstance(row, dict)
        and str(row.get("role") or "").strip().lower()
        == "reconstruction_manifest"
    ]
    if len(proof_bindings) != 1:
        raise HTTPException(status_code=410, detail="tour_viewer_integrity_failed")
    proof_bytes = _public_tour_read_bound_file(bundle_dir, proof_bindings[0])
    try:
        proof = json.loads(proof_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=410,
            detail="tour_viewer_integrity_failed",
        ) from exc
    source_paths = _public_tour_generated_manifest_source_paths(proof)
    if not isinstance(proof, dict) or not source_paths or any(
        _public_tour_generated_source_path_is_unsafe(path) for path in source_paths
    ):
        raise HTTPException(status_code=410, detail="tour_viewer_integrity_failed")
    content = _public_tour_read_bound_file(bundle_dir, binding)
    return content, binding, release


def _public_tour_verified_generated_viewer_response(
    slug: str,
    asset_path: str,
    *,
    payload: dict[str, object] | None = None,
) -> Response:
    content, binding, release = _public_tour_verified_generated_viewer_asset(
        slug,
        asset_path,
        payload=payload,
    )
    role = str(binding.get("role") or "").strip().lower()
    mime_type = str(binding.get("mime_type") or "application/octet-stream").strip()
    is_document = role == "viewer_document"
    viewer_html = ""
    if is_document:
        try:
            viewer_html = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=410,
                detail="tour_viewer_integrity_failed",
            ) from exc
    headers = _public_tour_security_headers(
        cache_control=(
            "no-cache, max-age=0, must-revalidate"
            if is_document
            else "public, max-age=86400, immutable"
        ),
        runtime_profile="generated_viewer" if is_document else "document",
        script_hashes=(
            _public_tour_inline_csp_hashes(viewer_html, tag_name="script")
            if is_document
            else ()
        ),
        style_hashes=(
            _public_tour_inline_csp_hashes(viewer_html, tag_name="style")
            if is_document
            else ()
        ),
    )
    headers.update(
        {
            "Cross-Origin-Resource-Policy": "same-origin",
            "X-Frame-Options": "SAMEORIGIN",
            "X-PropertyQuarry-Asset-SHA256": str(binding.get("sha256") or ""),
            "X-PropertyQuarry-Preview-Kind": "approximate-layout",
            "X-PropertyQuarry-Verified-Provider-Capture": "false",
            "X-PropertyQuarry-Verified-Tour-Gate": "false",
            "X-PropertyQuarry-Viewer-Revision": str(
                release.get("release_revision") or ""
            ),
        }
    )
    return Response(content=content, media_type=mime_type, headers=headers)


@router.get("/tours/{slug}.json", response_class=JSONResponse)
def public_tour_payload(slug: str) -> JSONResponse:
    payload = _load_tour(slug)
    _require_public_tour_viewable(payload)
    if _tour_payload_is_disabled_fallback(payload):
        raise HTTPException(status_code=404, detail="tour_disabled_fallback")
    payload = _without_disqualified_walkthrough(payload)
    return JSONResponse(
        _redacted_public_tour_payload(
            payload,
            expose_asset_relpaths=False,
            include_external_tour_urls=False,
        ),
        headers=_public_tour_security_headers(),
    )


@router.get("/tours/files/{slug}/{asset_path:path}")
@router.head("/tours/files/{slug}/{asset_path:path}")
def public_tour_file(slug: str, asset_path: str, request: Request):
    payload = _load_tour_with_private_receipt(slug)
    _require_public_tour_viewable(payload)
    safe_relpath = _public_tour_safe_asset_relpath(asset_path)
    generated_viewer_release = payload.get("generated_viewer_release")
    manifest_row = _public_tour_manifest(payload, only_relpath=safe_relpath).get(safe_relpath, {}) if safe_relpath else {}
    generated_privacy_class = (
        str(manifest_row.get("privacy_class") or "").strip().lower()
        == _GENERATED_RECONSTRUCTION_PREVIEW_PRIVACY_CLASS
    )
    if isinstance(generated_viewer_release, dict) and (
        safe_relpath.startswith(_GENERATED_RECONSTRUCTION_PREVIEW_PREFIX)
        or generated_privacy_class
    ):
        return _public_tour_verified_generated_viewer_response(
            slug,
            safe_relpath,
            payload=payload,
        )
    preview_manifest_row = _generated_reconstruction_preview_asset_manifest_row(payload, safe_relpath)
    walkthrough_acceptance = _public_tour_walkthrough_acceptance(payload)
    if walkthrough_acceptance.get("allowed") is False and safe_relpath in set(
        walkthrough_acceptance.get("asset_relpaths") or []
    ):
        return Response(
            "This walkthrough is no longer available.\n",
            status_code=410,
            media_type="text/plain; charset=utf-8",
            headers=_public_tour_security_headers(cache_control="no-store"),
        )
    safe_name = PurePosixPath(safe_relpath).name
    generated_asset_kind = _generated_reconstruction_non_tour_asset(payload, safe_relpath)
    if generated_asset_kind == "viewer":
        if not preview_manifest_row or not _generated_reconstruction_public_viewer_enabled(payload):
            primary_control_path = _public_tour_primary_control_path(payload)
            if primary_control_path:
                return RedirectResponse(
                    primary_control_path,
                    status_code=302,
                    headers=_public_tour_security_headers(cache_control="no-store"),
                )
            return RedirectResponse(
                f"/tours/{urllib.parse.quote(str(payload.get('slug') or slug).strip(), safe='')}",
                status_code=302,
                headers=_public_tour_security_headers(cache_control="no-store"),
            )
    elif (
        str(manifest_row.get("privacy_class") or "").strip().lower()
        == _GENERATED_RECONSTRUCTION_PREVIEW_PRIVACY_CLASS
        and str(manifest_row.get("role") or "").strip().lower().replace("-", "_")
        in {"generated_reconstruction_viewer", "generated_reconstruction_viewer_asset"}
        and not preview_manifest_row
    ):
        raise HTTPException(status_code=404, detail="tour_file_not_found")
    if generated_asset_kind == "model":
        return Response(
            "This generated model is not a public 3D tour.\n",
            status_code=410,
            media_type="text/plain; charset=utf-8",
            headers=_public_tour_security_headers(cache_control="no-store"),
        )
    removed_cube_assets = {str(item or "").strip() for item in list(payload.get("removed_cube_assets") or [])}
    if bool(payload.get("cube_fallback_removed")) and (
        safe_name in removed_cube_assets or safe_name.lower().startswith("pq-3d-top22")
    ):
        return Response(
            "This tour asset is no longer available.\n",
            status_code=410,
            media_type="text/plain; charset=utf-8",
            headers=_public_tour_security_headers(cache_control="no-store"),
        )
    # Public PDFs must stay on explicit public privacy classes such as
    # `floorplan_pdf_public` from `_PUBLIC_TOUR_PUBLIC_PDF_PRIVACY_CLASSES`.
    file_path = _asset_file(slug, asset_path)
    media_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    headers = _public_tour_security_headers(cache_control="public, max-age=86400, immutable")
    if generated_asset_kind == "viewer":
        viewer_html = ""
        if media_type in {"text/html", "application/xhtml+xml"}:
            try:
                viewer_html = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                viewer_html = ""
        headers = _public_tour_security_headers(
            cache_control="no-cache, max-age=0, must-revalidate",
            allow_base_uri_self=True,
            runtime_profile="generated_viewer",
            script_hashes=_public_tour_inline_csp_hashes(viewer_html, tag_name="script"),
            style_hashes=_public_tour_inline_csp_hashes(viewer_html, tag_name="style"),
        )
        headers["Cross-Origin-Resource-Policy"] = "same-origin"
        headers["X-Frame-Options"] = "SAMEORIGIN"
        headers["X-PropertyQuarry-Tour-Asset-Kind"] = "generated-reconstruction-viewer"
    if preview_manifest_row:
        headers["Cross-Origin-Resource-Policy"] = "same-origin"
        headers["X-PropertyQuarry-Preview-Kind"] = "approximate-layout"
        headers["X-PropertyQuarry-Verified-Provider-Capture"] = "false"
        headers["X-PropertyQuarry-Verified-Tour-Gate"] = "false"
    if manifest_row.get("sha256"):
        headers["X-PropertyQuarry-Asset-SHA256"] = str(manifest_row["sha256"])
    if manifest_row.get("privacy_class"):
        headers["X-PropertyQuarry-Asset-Privacy"] = str(manifest_row["privacy_class"])
    return FileResponse(
        file_path,
        media_type=media_type,
        headers=headers,
    )


@router.get("/tours/viewer/{slug}/{asset_path:path}")
@router.head("/tours/viewer/{slug}/{asset_path:path}")
def public_tour_generated_reconstruction_preview_asset(slug: str, asset_path: str, request: Request):
    payload = _load_tour_with_private_receipt(slug)
    _require_public_tour_viewable(payload)
    safe_relpath = _generated_reconstruction_preview_relpath(asset_path)
    if isinstance(payload.get("generated_viewer_release"), dict):
        return _public_tour_verified_generated_viewer_response(
            slug,
            safe_relpath,
            payload=payload,
        )
    if not safe_relpath or not _generated_reconstruction_preview_asset_manifest_row(payload, safe_relpath):
        raise HTTPException(status_code=404, detail="tour_file_not_found")
    return public_tour_file(slug, safe_relpath, request)


@router.get("/tours/pano2vr/{slug}/{asset_path:path}")
@router.head("/tours/pano2vr/{slug}/{asset_path:path}")
def public_tour_pano2vr_file(slug: str, asset_path: str):
    file_path = _pano2vr_export_file(slug, asset_path)
    media_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    runtime_profile = "vendor_export" if media_type in {"text/html", "application/xhtml+xml"} else "document"
    return FileResponse(
        file_path,
        media_type=media_type,
        headers=_public_tour_security_headers(
            cache_control="public, max-age=86400",
            allow_base_uri_self=runtime_profile == "vendor_export",
            runtime_profile=runtime_profile,
        ),
    )


@router.get("/tours/3dvista/{slug}/{asset_path:path}")
@router.head("/tours/3dvista/{slug}/{asset_path:path}")
def public_tour_3dvista_file(slug: str, asset_path: str):
    file_path = _3dvista_export_file(slug, asset_path)
    media_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    runtime_profile = "vendor_export" if media_type in {"text/html", "application/xhtml+xml"} else "document"
    return FileResponse(
        file_path,
        media_type=media_type,
        headers=_public_tour_security_headers(
            cache_control="public, max-age=86400",
            allow_base_uri_self=runtime_profile == "vendor_export",
            runtime_profile=runtime_profile,
        ),
    )


@router.get("/tours/{slug}/walkthrough")
@router.head("/tours/{slug}/walkthrough")
def public_tour_walkthrough(slug: str):
    payload = _load_tour(slug)
    _require_public_tour_viewable(payload)
    if _tour_payload_is_disabled_fallback(payload):
        raise HTTPException(status_code=404, detail="tour_disabled_fallback")
    if _public_tour_walkthrough_acceptance(payload).get("allowed") is False:
        raise HTTPException(status_code=404, detail="tour_walkthrough_unavailable")
    video_relpath = _public_tour_safe_asset_relpath(str(payload.get("video_relpath") or "").strip())
    if not video_relpath:
        video_relpath = _public_tour_safe_asset_relpath(
            str(dict(payload.get("generated_reconstruction") or {}).get("walkthrough_video_relpath") or "").strip()
        )
    if not video_relpath:
        raise HTTPException(status_code=404, detail="tour_walkthrough_unavailable")
    file_path = _asset_file(slug, video_relpath)
    media_type = mimetypes.guess_type(str(file_path))[0] or "video/mp4"
    return FileResponse(
        file_path,
        media_type=media_type,
        headers=_public_tour_security_headers(cache_control="public, max-age=86400, immutable"),
    )


@router.get("/tours/{slug}/layout-preview", response_class=HTMLResponse)
@router.head("/tours/{slug}/layout-preview", response_class=HTMLResponse)
def public_tour_generated_layout_preview(slug: str, request: Request) -> HTMLResponse:
    try:
        payload = _load_tour_with_private_receipt(slug)
        _require_public_tour_viewable(payload)
        generated_reconstruction_only = _public_tour_is_generated_reconstruction_only(payload)
        if _tour_payload_is_disabled_fallback(payload) and not generated_reconstruction_only:
            raise HTTPException(status_code=404, detail="tour_disabled_fallback")
        primary_control_path = _public_tour_primary_control_path(payload)
        if primary_control_path:
            return RedirectResponse(
                primary_control_path,
                status_code=302,
                headers=_public_tour_security_headers(),
            )
        viewer_release = evaluate_public_tour_generated_viewer_release(payload)
        if (
            not primary_control_path
            and isinstance(payload.get("generated_viewer_release"), dict)
        ):
            if not viewer_release.get("released"):
                raise HTTPException(
                    status_code=410 if viewer_release.get("terminal") else 404,
                    detail="tour_generated_layout_preview_unavailable",
                )
            viewer_url = _public_tour_generated_viewer_url(
                slug,
                viewer_release.get("viewer_relpath"),
            )
            if not viewer_url:
                raise HTTPException(
                    status_code=404,
                    detail="tour_generated_layout_preview_unavailable",
                )
            return RedirectResponse(
                viewer_url,
                status_code=302,
                headers=_public_tour_security_headers(),
            )
        if _generated_reconstruction_layout_preview_relpath(payload):
            return _generated_reconstruction_public_launch_response(payload, layout_focus=True, request=request)
        if generated_reconstruction_only:
            return _generated_reconstruction_public_launch_response(payload, layout_focus=True, request=request)
        html_body = _generated_reconstruction_layout_preview_html(slug=slug, payload=payload)
        return HTMLResponse(
            html_body,
            headers=_public_tour_security_headers(
                allow_base_uri_self=True,
                allow_jsdelivr="https://cdn.jsdelivr.net/" in html_body,
                runtime_profile="generated_viewer",
                script_hashes=_public_tour_inline_csp_hashes(html_body, tag_name="script"),
                style_hashes=_public_tour_inline_csp_hashes(html_body, tag_name="style"),
            ),
        )
    except HTTPException as exc:
        detail = str(exc.detail or "").strip().lower()
        if (
            exc.status_code == 410
            and detail == "tour_generated_layout_preview_unavailable"
        ):
            return _render_generated_viewer_terminal_page(request)
        if exc.status_code == 410 and detail == "tour_revoked":
            return _render_tour_unavailable_page(
                request,
                status_code=410,
                title="This tour was removed by its owner.",
                summary="The public copy and its assets are no longer available. Cached copies are queued for removal too.",
                status_label="Tour revoked",
                rows=[
                    {
                        "label": "Tour state",
                        "value": "Removed",
                        "detail": "PropertyQuarry blocks this link even while edge caches finish purging.",
                    },
                    {
                        "label": "Next step",
                        "value": "Return to PropertyQuarry",
                        "detail": "Ask the owner for a new share only if they choose to publish again.",
                    },
                ],
            )
        if exc.status_code == 404 and detail in {"tour_disabled_fallback", "tour_generated_layout_preview_unavailable"}:
            return _render_generated_reconstruction_not_tour_page(request)
        raise


def _tour_control_html(
    payload: dict[str, object],
    *,
    viewer_mode: str = "",
    fullscreen: bool = False,
    nonce: str = "",
) -> str:
    control_nonce = _public_tour_normalized_nonce(nonce) or _public_tour_csp_nonce()
    if fullscreen:
        payload = {**payload, "_tour_control_fullscreen": True}
    forced_mode = str(viewer_mode or "").strip().lower()
    if forced_mode == "marzipano":
        raise HTTPException(status_code=410, detail="tour_control_legacy_viewer_removed")
    if forced_mode in {"matterport", "metaport"}:
        raise HTTPException(status_code=404, detail="tour_control_provider_retired")
    if forced_mode in {"3dvista", "3d_vista", "three_d_vista"}:
        return _tour_control_3dvista_html(payload, nonce=control_nonce)
    if forced_mode in {"pano2vr", "pano_2_vr", "krpano"}:
        raise HTTPException(status_code=404, detail="tour_control_panorama_export_hidden")
    control_mode = str(payload.get("control_mode") or "").strip().lower()
    if control_mode == "marzipano":
        raise HTTPException(status_code=410, detail="tour_control_legacy_viewer_removed")
    if control_mode in {"3dvista", "3d_vista", "three_d_vista"}:
        return _tour_control_3dvista_html(payload, nonce=control_nonce)
    if control_mode in {"pano2vr", "pano_2_vr"}:
        raise HTTPException(status_code=404, detail="tour_control_panorama_export_hidden")
    if control_mode == "internal_walkable_3d":
        raise HTTPException(status_code=410, detail="tour_control_legacy_viewer_removed")
    if control_mode == "walkable_3d" or isinstance(payload.get("walkable_scene"), dict):
        raise HTTPException(status_code=404, detail="tour_control_provider_export_missing")
    if str(payload.get("scene_strategy") or "").strip() == "pure_360_cube":
        raise HTTPException(status_code=404, detail="tour_control_cube_viewer_removed")
    raise HTTPException(status_code=404, detail="tour_control_provider_export_missing")


def _krpano_license_runtime_config() -> dict[str, str]:
    domain = str(os.getenv("KRPANO_LICENSE_DOMAIN") or "").strip()
    key = str(os.getenv("KRPANO_LICENSE_KEY") or "").strip()
    if not domain or not key:
        return {}
    return {
        "domain": domain,
        "key": key,
    }


def _safe_3dvista_external_url(value: object) -> str:
    normalized = _public_tour_safe_http_url(value)
    if not normalized:
        return ""
    parsed = urllib.parse.urlparse(normalized)
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if host != "3dvista.com" and not host.endswith(".3dvista.com"):
        return ""
    return normalized


def _safe_matterport_external_url(value: object) -> str:
    normalized = _public_tour_safe_http_url(value)
    if not normalized:
        return ""
    parsed = urllib.parse.urlparse(normalized)
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if host != "matterport.com" and not host.endswith(".matterport.com"):
        return ""
    if host == "discover.matterport.com" and parsed.path.startswith("/space/"):
        model_id = parsed.path.rsplit("/", 1)[-1].strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{6,32}", model_id):
            return f"https://my.matterport.com/show/?m={urllib.parse.quote(model_id)}"
    if host == "my.matterport.com" and parsed.path.startswith("/models/"):
        model_id = parsed.path.rsplit("/", 1)[-1].strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{6,32}", model_id):
            return f"https://my.matterport.com/show/?m={urllib.parse.quote(model_id)}"
    if host != "my.matterport.com" or parsed.path.rstrip("/") != "/show":
        return ""
    allowed_query_keys = {"m", "mls", "play", "qs", "brand", "help", "vr", "dh", "gt"}
    query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    model_id = next((item for key, item in query_items if key == "m"), "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", model_id):
        return ""
    safe_query = urllib.parse.urlencode(
        [(key, item[:80]) for key, item in query_items if key in allowed_query_keys],
        doseq=True,
    )
    return urllib.parse.urlunparse(("https", "my.matterport.com", "/show/", "", safe_query, ""))


def _matterport_sdk_walkthrough_contract(payload: dict[str, object]) -> dict[str, object]:
    """Return a normalized, edit-free private Matterport FLY route or fail closed."""

    raw_contract = payload.get("matterport_walkthrough")
    if not isinstance(raw_contract, dict):
        return {}
    contract = dict(raw_contract)
    if str(contract.get("status") or "").strip().lower() != "pass":
        return {}
    model_sid = str(contract.get("model_sid") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", model_sid):
        return {}
    try:
        edit_counts = {
            key: int(contract.get(key) or 0)
            for key in ("cut_count", "dissolve_count", "teleport_count")
        }
    except (TypeError, ValueError):
        return {}
    if any(value != 0 for value in edit_counts.values()):
        return {}
    declared_transition = str(contract.get("transition") or "fly").strip().lower()
    if declared_transition != "fly":
        return {}

    raw_route = contract.get("route")
    raw_walkable_rooms = contract.get("walkable_room_ids")
    if not isinstance(raw_route, list) or len(raw_route) < 2 or not isinstance(raw_walkable_rooms, list):
        return {}
    walkable_room_ids = {
        str(room_id or "").strip()
        for room_id in raw_walkable_rooms
        if str(room_id or "").strip()
    }
    if not walkable_room_ids:
        return {}

    route: list[dict[str, object]] = []
    seen_sweep_ids: set[str] = set()
    covered_room_ids: set[str] = set()
    for raw_node in raw_route:
        if not isinstance(raw_node, dict):
            return {}
        sweep_id = str(raw_node.get("sweep_id") or "").strip()
        room_id = str(raw_node.get("room_id") or "").strip()
        try:
            transition_time_ms = int(raw_node.get("transition_time_ms") or 0)
        except (TypeError, ValueError):
            return {}
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sweep_id)
            or sweep_id in seen_sweep_ids
            or not room_id
            or transition_time_ms < 600
            or transition_time_ms > 30_000
        ):
            return {}
        rotation = raw_node.get("rotation")
        normalized_rotation: dict[str, float] = {}
        if rotation is not None:
            if not isinstance(rotation, dict):
                return {}
            try:
                normalized_rotation = {
                    axis: float(rotation[axis])
                    for axis in ("x", "y")
                    if axis in rotation
                }
            except (TypeError, ValueError):
                return {}
        node: dict[str, object] = {
            "sweep_id": sweep_id,
            "room_id": room_id,
            "transition_time_ms": transition_time_ms,
        }
        if normalized_rotation:
            node["rotation"] = normalized_rotation
        route.append(node)
        seen_sweep_ids.add(sweep_id)
        covered_room_ids.add(room_id)

    missing_room_ids = sorted(walkable_room_ids - covered_room_ids)
    if missing_room_ids or list(contract.get("missing_room_ids") or []):
        return {}
    try:
        start_ss = int(contract.get("start_ss") or 1)
    except (TypeError, ValueError):
        return {}
    if start_ss < 1:
        return {}
    return {
        **contract,
        **edit_counts,
        "model_sid": model_sid,
        "transition": "fly",
        "start_ss": start_ss,
        "route_node_count": len(route),
        "walkable_room_count": len(walkable_room_ids),
        "walkable_room_ids": sorted(walkable_room_ids),
        "covered_room_ids": sorted(walkable_room_ids),
        "missing_room_ids": [],
        "route": route,
    }


def _matterport_sdk_timestamp(value: object) -> datetime | None:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _matterport_model_publication_contract(
    payload: dict[str, object],
    *,
    model_sid: str,
) -> dict[str, object]:
    raw_publication = payload.get("matterport_model_publication")
    if not isinstance(raw_publication, dict):
        return {}
    publication = dict(raw_publication)
    if (
        str(publication.get("status") or "").strip().lower() != "pass"
        or publication.get("model_available") is not True
        or str(publication.get("model_sid") or "").strip() != model_sid
    ):
        return {}
    checked_at = _matterport_sdk_timestamp(publication.get("checked_at"))
    asset_valid_until = _matterport_sdk_timestamp(publication.get("asset_valid_until"))
    now = datetime.now(timezone.utc)
    if (
        checked_at is None
        or checked_at < now - timedelta(hours=24)
        or checked_at > now + timedelta(minutes=5)
        or asset_valid_until is None
        or asset_valid_until <= now
    ):
        return {}
    try:
        enabled_sweep_count = int(publication.get("enabled_sweep_count") or 0)
        connected_component_count = int(publication.get("connected_component_count") or 0)
    except (TypeError, ValueError):
        return {}
    if enabled_sweep_count < 2 or connected_component_count != 1:
        return {}
    source_sha256 = str(publication.get("source_sha256") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
        return {}
    return publication


def _matterport_sdk_walkthrough_context(
    payload: dict[str, object],
    *,
    external_url: str,
) -> dict[str, object]:
    """Build private SDK state only when every key, route, and publication proof agrees."""

    if payload.get("_tour_control_matterport_walkthrough") is not True:
        return {}
    sdk_key = str(
        os.getenv("MATTERPORT_SDK_KEY")
        or os.getenv("MATTERPORT_APPLICATION_KEY")
        or ""
    ).strip()
    if not sdk_key or len(sdk_key) > 512 or any(ord(character) < 33 for character in sdk_key):
        return {}
    safe_external_url = _safe_matterport_external_url(external_url)
    if not safe_external_url:
        return {}
    parsed_url = urllib.parse.urlparse(safe_external_url)
    model_sid = next(
        (
            value
            for key, value in urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=False)
            if key == "m"
        ),
        "",
    )
    contract = _matterport_sdk_walkthrough_contract(payload)
    if not contract or str(contract.get("model_sid") or "") != model_sid:
        return {}
    publication = _matterport_model_publication_contract(payload, model_sid=model_sid)
    if not publication:
        return {}

    query_items = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=False)
        if key not in {"applicationKey", "play", "qs", "ss"}
    ]
    query_items.extend(
        (
            ("applicationKey", sdk_key),
            ("play", "0"),
            ("qs", "1"),
            ("ss", str(contract["start_ss"])),
        )
    )
    iframe_url = urllib.parse.urlunparse(
        (
            "https",
            "my.matterport.com",
            "/show/",
            "",
            urllib.parse.urlencode(query_items),
            "",
        )
    )
    return {
        "sdk_key": sdk_key,
        "iframe_url": iframe_url,
        "contract": contract,
        "publication": publication,
    }


def _tour_control_matterport_html(payload: dict[str, object]) -> str:
    """Render the isolated private SDK proof harness; never selected by public routing."""

    external_url = _safe_matterport_external_url(payload.get("matterport_url"))
    context = _matterport_sdk_walkthrough_context(payload, external_url=external_url)
    title = html.escape(str(payload.get("title") or "Matterport walkthrough").strip())
    if not context:
        iframe = (
            f'<iframe title="{title}" src="{html.escape(external_url, quote=True)}" '
            'allow="fullscreen; xr-spatial-tracking" allowfullscreen></iframe>'
            if external_url
            else '<p role="status">Matterport walkthrough unavailable.</p>'
        )
        return f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{title}</title></head><body>{iframe}</body></html>"

    script_config = _public_tour_script_json(
        {
            "sdkKey": context["sdk_key"],
            "contract": context["contract"],
        }
    )
    iframe_url = html.escape(str(context["iframe_url"]), quote=True)
    bootstrap_url = html.escape(_MATTERPORT_SDK_BOOTSTRAP_URL, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
</head>
<body>
  <main>
    <iframe id="matterport-showcase" title="{title}" src="{iframe_url}" allow="fullscreen; xr-spatial-tracking" allowfullscreen></iframe>
    <button type="button" data-matterport-walkthrough-toggle aria-label="Pause walkthrough">Pause</button>
    <p data-matterport-walkthrough-status aria-live="polite">Preparing walkthrough…</p>
  </main>
  <script src="{bootstrap_url}"></script>
  <script>
  (() => {{
    'use strict';
    const config = {script_config};
    const frame = document.getElementById('matterport-showcase');
    const toggle = document.querySelector('[data-matterport-walkthrough-toggle]');
    const status = document.querySelector('[data-matterport-walkthrough-status]');
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let paused = reducedMotion;
    let running = false;
    let initialized = false;
    let resumeWaiters = [];

    const publish = (state, detail = {{}}) => {{
      const proof = {{
        status: state,
        transition: 'fly',
        route_node_count: config.contract.route.length,
        walkable_room_count: config.contract.walkable_room_ids.length,
        missing_room_count: Number(detail.missing_room_count || 0),
        ...detail,
      }};
      window.__PROPERTYQUARRY_MATTERPORT_WALKTHROUGH__ = proof;
      document.documentElement.dataset.matterportWalkthroughState = state;
      window.dispatchEvent(new CustomEvent('propertyquarry:matterport-walkthrough', {{ detail: proof }}));
    }};

    const renderToggle = () => {{
      const manual = !running && reducedMotion;
      const label = manual ? 'Play walkthrough' : paused ? 'Resume walkthrough' : 'Pause walkthrough';
      toggle.setAttribute('aria-label', label);
      toggle.textContent = label.replace(' walkthrough', '');
      status.textContent = manual ? 'Walkthrough ready to play.' : paused ? 'Walkthrough paused.' : 'Walkthrough playing.';
    }};

    const waitForMatterportWalkthroughResume = async () => {{
      if (!paused) return;
      await new Promise((resolve) => resumeWaiters.push(resolve));
    }};

    const play = async () => {{
      if (running) return;
      running = true;
      paused = false;
      renderToggle();
      publish('running');
      try {{
        if (!window.MP_SDK || typeof window.MP_SDK.connect !== 'function') {{
          throw new Error('matterport_sdk_bootstrap_unavailable');
        }}
        const mpSdk = await window.MP_SDK.connect(frame, config.sdkKey);
        const coveredRoomIds = new Set();
        if (mpSdk.Sweep.current && typeof mpSdk.Sweep.current.subscribe === 'function') {{
          mpSdk.Sweep.current.subscribe(() => {{}});
        }}
        for (const node of config.contract.route) {{
          await waitForMatterportWalkthroughResume();
          await mpSdk.Sweep.moveTo(node.sweep_id, {{
            rotation: node.rotation || undefined,
            transition: mpSdk.Camera.TransitionType.FLY,
            transitionTime: node.transition_time_ms,
          }});
          coveredRoomIds.add(node.room_id);
        }}
        const missingRoomIds = config.contract.walkable_room_ids.filter((roomId) => !coveredRoomIds.has(roomId));
        if (missingRoomIds.length) throw new Error('walkable_room_coverage_missing');
        paused = false;
        renderToggle();
        status.textContent = 'Walkthrough complete.';
        publish('pass', {{ missing_room_count: 0 }});
      }} catch (error) {{
        status.textContent = 'Walkthrough could not be completed.';
        publish('fail', {{ reason: String(error && error.message || error) }});
      }} finally {{
        running = false;
      }}
    }};

    toggle.addEventListener('click', () => {{
      if (!running) {{
        void play();
        return;
      }}
      paused = !paused;
      if (!paused) {{
        const waiters = resumeWaiters;
        resumeWaiters = [];
        waiters.forEach((resolve) => resolve());
      }}
      renderToggle();
      publish(paused ? 'paused' : 'running');
    }});

    const initialize = () => {{
      if (initialized) return;
      initialized = true;
      if (reducedMotion) {{
        renderToggle();
        publish('manual');
      }} else {{
        void play();
      }}
    }};
    frame.addEventListener('load', initialize, {{ once: true }});
    window.setTimeout(initialize, 0);
  }})();
  </script>
</body>
</html>"""


def _public_tour_walkthrough_acceptance(payload: dict[str, object]) -> dict[str, object]:
    slug = str(payload.get("slug") or "").strip()
    generated_reconstruction = (
        dict(payload.get("generated_reconstruction") or {})
        if isinstance(payload.get("generated_reconstruction"), dict)
        else {}
    )
    top_level_asset_relpaths = {
        _public_tour_safe_asset_relpath(str(payload.get(key) or "").strip())
        for key in ("video_relpath", "video_mobile_relpath", "flythrough_video_relpath")
    }
    top_level_asset_relpaths.discard("")
    if top_level_asset_relpaths:
        raw_sidecar_relpath = str(
            payload.get("video_sidecar_relpath")
            or payload.get("walkthrough_sidecar_relpath")
            or ""
        ).strip()
        scope = "top_level"
        asset_relpaths = top_level_asset_relpaths
    else:
        raw_sidecar_relpath = str(
            generated_reconstruction.get("walkthrough_sidecar_relpath") or ""
        ).strip()
        scope = "generated_reconstruction"
        generated_video_relpath = _public_tour_safe_asset_relpath(
            str(generated_reconstruction.get("walkthrough_video_relpath") or "").strip()
        )
        asset_relpaths = {generated_video_relpath} if generated_video_relpath else set()
    if not raw_sidecar_relpath:
        return {
            "allowed": True,
            "declared": False,
            "scope": scope,
            "asset_relpaths": sorted(asset_relpaths),
            "status": "legacy_unreviewed",
        }
    sidecar_relpath = _public_tour_safe_asset_relpath(raw_sidecar_relpath)
    if not slug or not sidecar_relpath or PurePosixPath(sidecar_relpath).suffix.lower() != ".json":
        return {
            "allowed": False,
            "declared": True,
            "scope": scope,
            "asset_relpaths": sorted(asset_relpaths),
            "status": "sidecar_invalid",
        }
    try:
        bundle_dir = _resolved_tour_bundle(slug)
        sidecar_path = (bundle_dir / sidecar_relpath).resolve()
        if bundle_dir != sidecar_path and bundle_dir not in sidecar_path.parents:
            raise ValueError("sidecar_outside_bundle")
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "allowed": False,
            "declared": True,
            "scope": scope,
            "asset_relpaths": sorted(asset_relpaths),
            "status": "sidecar_unavailable",
        }
    if not isinstance(sidecar, dict):
        return {
            "allowed": False,
            "declared": True,
            "scope": scope,
            "asset_relpaths": sorted(asset_relpaths),
            "status": "sidecar_invalid",
        }
    acceptance_status = str(sidecar.get("acceptance_status") or "unreviewed").strip().lower()
    disqualified = (
        acceptance_status in {"disqualified", "rejected", "failed"}
        or sidecar.get("launch_eligible") is False
    )
    return {
        "allowed": not disqualified,
        "declared": True,
        "scope": scope,
        "asset_relpaths": sorted(asset_relpaths),
        "status": "disqualified" if disqualified else acceptance_status,
    }


def _without_disqualified_walkthrough(payload: dict[str, object]) -> dict[str, object]:
    acceptance = _public_tour_walkthrough_acceptance(payload)
    if acceptance.get("allowed") is not False:
        return payload
    sanitized = dict(payload)
    if acceptance.get("scope") == "top_level":
        for key in (
            "video_relpath",
            "video_mobile_relpath",
            "flythrough_video_relpath",
            "video_url",
            "flythrough_url",
            "video_sidecar_relpath",
            "walkthrough_sidecar_relpath",
            "video_provider",
            "video_provider_key",
            "video_render_provider",
            "video_coverage_proof",
        ):
            sanitized.pop(key, None)
    else:
        generated_reconstruction = dict(sanitized.get("generated_reconstruction") or {})
        generated_reconstruction.pop("walkthrough_video_relpath", None)
        generated_reconstruction.pop("walkthrough_sidecar_relpath", None)
        sanitized["generated_reconstruction"] = generated_reconstruction
    sanitized["_walkthrough_media_suppressed"] = True
    return sanitized


def _public_tour_walkthrough_media_context(payload: dict[str, object]) -> tuple[str, str]:
    payload = _without_disqualified_walkthrough(payload)
    if payload.get("_walkthrough_media_suppressed") is True:
        return "", "video/mp4"
    slug = str(payload.get("slug") or "").strip()
    video_relpath = _public_tour_safe_asset_relpath(str(payload.get("video_relpath") or "").strip())
    if not video_relpath:
        generated_reconstruction = payload.get("generated_reconstruction")
        if isinstance(generated_reconstruction, dict):
            video_relpath = _public_tour_safe_asset_relpath(
                str(generated_reconstruction.get("walkthrough_video_relpath") or "").strip()
            )
    raw_video_url = str(payload.get("video_url") or "").strip()
    video_url = ""
    mime_source_path = ""
    if slug and video_relpath:
        video_url = f"/tours/{slug}/walkthrough"
        mime_source_path = video_relpath
    elif raw_video_url and _public_tour_external_media_url_allowed(raw_video_url):
        video_url = raw_video_url
        mime_source_path = urllib.parse.urlparse(raw_video_url).path
    video_mime_type = mimetypes.guess_type(mime_source_path)[0] or "video/mp4"
    return video_url, video_mime_type


def _public_tour_walkthrough_source_markup(
    payload: dict[str, object],
    *,
    video_url: str,
    video_mime_type: str,
) -> str:
    payload = _without_disqualified_walkthrough(payload)
    if payload.get("_walkthrough_media_suppressed") is True:
        return ""
    sources: list[str] = []
    slug = str(payload.get("slug") or "").strip()
    mobile_relpath = _public_tour_safe_asset_relpath(str(payload.get("video_mobile_relpath") or "").strip())
    if slug and mobile_relpath and mobile_relpath in _public_tour_allowed_asset_paths(payload):
        mobile_url = _public_tour_file_url(slug, mobile_relpath)
        mobile_mime_type = mimetypes.guess_type(mobile_relpath)[0] or "video/mp4"
        sources.append(
            f'<source src="{html.escape(mobile_url)}" type="{html.escape(mobile_mime_type)}" '
            'media="(max-width: 760px)">'
        )
    if video_url:
        sources.append(
            f'<source src="{html.escape(video_url)}" type="{html.escape(video_mime_type)}">'
        )
    return "".join(sources)


def _generated_reconstruction_walkthrough_scenes(payload: dict[str, object]) -> list[dict[str, str]]:
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return []
    walkthrough_labels: list[str] = []
    for raw_label in list(
        generated_reconstruction.get("walkthrough_route_labels")
        or dict(generated_reconstruction.get("walkthrough_coverage_proof") or {}).get("segments_expected")
        or generated_reconstruction.get("route_labels")
        or []
    ):
        label = str(raw_label or "").strip()
        if label and label.lower() not in {item.lower() for item in walkthrough_labels}:
            walkthrough_labels.append(label)
    scenes: list[dict[str, str]] = []
    floorplan_relpath = _public_tour_safe_asset_relpath(str(generated_reconstruction.get("floorplan_relpath") or "").strip())
    if floorplan_relpath:
        scenes.append(
            {
                "name": "Route floorplan",
                "role": "floorplan",
                "asset_relpath": floorplan_relpath,
                "mime_type": mimetypes.guess_type(floorplan_relpath)[0] or "image/jpeg",
            }
        )
    for index, raw_relpath in enumerate(list(generated_reconstruction.get("photo_relpaths") or []), start=1):
        relpath = _public_tour_safe_asset_relpath(str(raw_relpath or "").strip())
        if not relpath:
            continue
        label = (
            walkthrough_labels[min(index - 1, len(walkthrough_labels) - 1)]
            if walkthrough_labels
            else f"Room photo {index:02d}"
        )
        scenes.append(
            {
                "name": label,
                "role": "photo",
                "asset_relpath": relpath,
                "mime_type": mimetypes.guess_type(relpath)[0] or "image/jpeg",
            }
        )
    return scenes


def _payload_with_generated_reconstruction_walkthrough(payload: dict[str, object]) -> dict[str, object]:
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return {}
    walkthrough_relpath = _public_tour_safe_asset_relpath(
        str(generated_reconstruction.get("walkthrough_video_relpath") or "").strip()
    )
    if not walkthrough_relpath:
        return {}
    augmented = dict(payload)
    augmented["video_relpath"] = walkthrough_relpath
    augmented.setdefault("video_provider", "propertyquarry_generated_reconstruction")
    existing_scenes = list(augmented.get("scenes") or []) if isinstance(augmented.get("scenes"), list) else []
    if not existing_scenes:
        generated_scenes = _generated_reconstruction_walkthrough_scenes(payload)
        if generated_scenes:
            augmented["scenes"] = generated_scenes
    return augmented


def _generated_reconstruction_route_labels(payload: dict[str, object]) -> list[str]:
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return []
    labels: list[str] = []
    for raw_label in list(generated_reconstruction.get("route_labels") or []):
        label = str(raw_label or "").strip()
        if label and label.lower() not in {item.lower() for item in labels}:
            labels.append(label)
    return labels


def _normalized_generated_reconstruction_label(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _generated_reconstruction_launch_route_actions(
    payload: dict[str, object],
    *,
    scene_entries: list[dict[str, str]],
) -> list[dict[str, object]]:
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return []
    route_labels = _generated_reconstruction_route_labels(payload)
    coverage = (
        dict(generated_reconstruction.get("walkthrough_coverage_proof") or {})
        if isinstance(generated_reconstruction.get("walkthrough_coverage_proof"), dict)
        else {}
    )
    coverage_segments = [dict(row) for row in list(coverage.get("coverage_segments") or []) if isinstance(row, dict)]
    photo_scene_indices: list[int] = []
    photo_scene_index_by_label: dict[str, int] = {}
    floorplan_scene_index = -1
    for index, row in enumerate(scene_entries):
        role = str(row.get("role") or "").strip().lower()
        label_key = _normalized_generated_reconstruction_label(row.get("preview_label") or row.get("name"))
        if role == "floorplan" and floorplan_scene_index < 0:
            floorplan_scene_index = index
        if role != "photo":
            continue
        photo_scene_indices.append(index)
        if label_key and label_key not in photo_scene_index_by_label:
            photo_scene_index_by_label[label_key] = index
    fallback_scene_index = floorplan_scene_index if floorplan_scene_index >= 0 else (photo_scene_indices[0] if photo_scene_indices else (0 if scene_entries else -1))
    actions: list[dict[str, object]] = []
    for index, label in enumerate(route_labels):
        start_seconds = 0.0
        end_seconds = 0.0
        matched = False
        for segment in coverage_segments:
            segment_label = str(segment.get("segment") or "").strip()
            if segment_label.lower() != label.lower():
                continue
            try:
                start_seconds = max(0.0, float(segment.get("start") or 0.0))
            except Exception:
                start_seconds = 0.0
            try:
                end_seconds = max(start_seconds, float(segment.get("end") or 0.0))
            except Exception:
                end_seconds = start_seconds
            matched = True
            break
        if not matched and index < len(coverage_segments):
            try:
                start_seconds = max(0.0, float(coverage_segments[index].get("start") or 0.0))
            except Exception:
                start_seconds = 0.0
            try:
                end_seconds = max(start_seconds, float(coverage_segments[index].get("end") or 0.0))
            except Exception:
                end_seconds = start_seconds
        normalized_label = _normalized_generated_reconstruction_label(label)
        scene_index = photo_scene_index_by_label.get(normalized_label, -1)
        if scene_index < 0 and photo_scene_indices and index < len(photo_scene_indices):
            scene_index = photo_scene_indices[index]
        if scene_index < 0:
            scene_index = fallback_scene_index
        resolved_label = ""
        focus_mode = ""
        if 0 <= scene_index < len(scene_entries):
            resolved_label = _normalized_generated_reconstruction_label(
                scene_entries[scene_index].get("preview_label") or scene_entries[scene_index].get("name")
            )
            focus_mode = str(scene_entries[scene_index].get("role") or scene_entries[scene_index].get("kind") or "").strip().lower()
        actions.append(
            {
                "index": index,
                "label": label,
                "start_seconds": round(start_seconds, 3),
                "end_seconds": round(end_seconds, 3),
                "scene_index": scene_index,
                "focus_label": label if normalized_label and normalized_label != resolved_label else "",
                "focus_mode": focus_mode,
                "cue_label": _generated_reconstruction_focus_mode_label(focus_mode),
            }
        )
    for index, action in enumerate(actions):
        start_seconds = max(0.0, float(action.get("start_seconds") or 0.0))
        end_seconds = max(start_seconds, float(action.get("end_seconds") or 0.0))
        if index + 1 < len(actions):
            next_start = max(0.0, float(actions[index + 1].get("start_seconds") or 0.0))
            if next_start > end_seconds:
                end_seconds = next_start
        duration_seconds = max(0.0, end_seconds - start_seconds)
        action["end_seconds"] = round(end_seconds, 3)
        action["duration_seconds"] = round(duration_seconds, 3)
        action["duration_label"] = _generated_reconstruction_duration_label(duration_seconds)
    return actions


def _generated_reconstruction_focus_mode_label(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "floorplan":
        return "Floorplan cue"
    if normalized == "photo":
        return "Photo cue"
    if normalized == "document":
        return "Document cue"
    return "Reference cue"


def _generated_reconstruction_duration_label(value: object) -> str:
    try:
        numeric_seconds = float(value or 0.0)
    except Exception:
        return ""
    total_seconds = max(1, int(round(numeric_seconds))) if numeric_seconds > 0 else 0
    if total_seconds <= 0:
        return ""
    minutes, seconds = divmod(total_seconds, 60)
    if minutes and seconds:
        return f"{minutes}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _render_generated_reconstruction_not_tour_page(request: Request) -> HTMLResponse:
    return _render_tour_unavailable_page(
        request,
        status_code=404,
        title="This tour link is no longer available.",
        summary="This link points to a generated layout reconstruction, not a published 3D tour.",
        status_label="Tour unavailable",
        rows=[
            {
                "label": "Surface",
                "value": "Generated reconstruction",
                "detail": "PropertyQuarry no longer presents generated layout reconstructions as public 3D tours.",
            },
            {
                "label": "Next step",
                "value": "Open PropertyQuarry",
                "detail": "Use the property page for the diorama and walkthrough, or request a fresh 3D tour once provider media is ready.",
            },
        ],
    )


def _render_generated_viewer_terminal_page(request: Request) -> HTMLResponse:
    return _render_tour_unavailable_page(
        request,
        status_code=410,
        title="This 3D preview is no longer available.",
        summary=(
            "The reviewed layout preview was revoked or disqualified and its "
            "public assets are no longer served."
        ),
        status_label="Preview removed",
        rows=[
            {
                "label": "Preview state",
                "value": "Removed",
                "detail": "PropertyQuarry fails closed when a release authority withdraws a viewer.",
            },
            {
                "label": "Next step",
                "value": "Request a fresh link",
                "detail": "A new review and publication authority are required before this preview can return.",
            },
        ],
    )


def _generated_reconstruction_public_launch_response(
    payload: dict[str, object],
    *,
    layout_focus: bool = False,
    request: Request | None = None,
) -> HTMLResponse:
    launch_payload = _generated_reconstruction_launch_payload(payload, layout_focus=layout_focus)
    if not launch_payload:
        raise HTTPException(status_code=404, detail="tour_generated_layout_preview_unavailable")
    if request is not None and _truthy(request.query_params.get("browser_shell_probe")):
        launch_payload = {**launch_payload, "_generated_reconstruction_browser_shell_probe": True}
    nonce = _public_tour_csp_nonce()
    html_body = _generated_reconstruction_public_launch_html(launch_payload, nonce=nonce)
    return HTMLResponse(
        html_body,
        headers=_public_tour_security_headers(
            allow_base_uri_self=True,
            nonce=nonce,
            allow_jsdelivr="https://cdn.jsdelivr.net/" in html_body,
        ),
    )


def _generated_reconstruction_launch_payload(
    payload: dict[str, object],
    *,
    layout_focus: bool = False,
) -> dict[str, object]:
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return {}
    if not _generated_reconstruction_public_shell_ready(payload):
        return {}
    augmented = dict(payload)
    generated_scenes = _generated_reconstruction_walkthrough_scenes(payload)
    if generated_scenes:
        augmented["scenes"] = generated_scenes
    walkthrough_relpath = _public_tour_safe_asset_relpath(
        str(generated_reconstruction.get("walkthrough_video_relpath") or "").strip()
    )
    if walkthrough_relpath:
        augmented["video_relpath"] = walkthrough_relpath
        augmented.setdefault("video_provider", "propertyquarry_generated_reconstruction")
        augmented.setdefault("video_coverage_proof", "boundary_verified_frame_continuation")
    lead_preview_url = ""
    for key in ("diorama_preview_relpath", "preview_relpath"):
        relpath = _public_tour_safe_asset_relpath(
            str(payload.get(key) or generated_reconstruction.get(key) or "").strip()
        )
        if relpath:
            lead_preview_url = _public_tour_file_url(str(payload.get("slug") or "").strip(), relpath)
            break
    if not lead_preview_url:
        lead_preview_url = _hosted_property_tour_preview_image_url(f"/tours/{str(payload.get('slug') or '').strip()}")
    if lead_preview_url:
        augmented["_lead_preview_url"] = lead_preview_url
    augmented["_generated_reconstruction_public_shell"] = True
    if layout_focus:
        augmented["_generated_reconstruction_layout_focus"] = True
    return augmented


def _generated_reconstruction_layout_preview_relpath(payload: dict[str, object]) -> str:
    contract = _generated_reconstruction_preview_contract(payload)
    return str(contract.get("viewer_relpath") or "").strip()


def _generated_reconstruction_layout_preview_html(*, slug: str, payload: dict[str, object]) -> str:
    if not _generated_reconstruction_public_shell_ready(payload):
        raise HTTPException(status_code=404, detail="tour_generated_layout_preview_unavailable")
    viewer_relpath = _generated_reconstruction_layout_preview_relpath(payload)
    if not viewer_relpath:
        raise HTTPException(status_code=404, detail="tour_generated_layout_preview_unavailable")
    viewer_path = _asset_file(slug, viewer_relpath)
    html_body = viewer_path.read_text(encoding="utf-8")
    base_href = f"/tours/files/{urllib.parse.quote(slug, safe='')}/generated-reconstruction/"
    if "<base " not in html_body:
        if "<head>" in html_body:
            html_body = html_body.replace("<head>", f'<head>\n  <base href="{html.escape(base_href)}">', 1)
        elif "<html>" in html_body:
            html_body = html_body.replace(
                "<html>",
                f'<html><head><base href="{html.escape(base_href)}"></head>',
                1,
            )
        else:
            html_body = f'<!doctype html><html><head><base href="{html.escape(base_href)}"></head><body>{html_body}</body></html>'
    return html_body


def _generated_reconstruction_public_launch_html(payload: dict[str, object], *, nonce: str = "") -> str:
    nonce_attr = html.escape(_public_tour_normalized_nonce(nonce) or _public_tour_csp_nonce(), quote=True)
    slug = str(payload.get("slug") or "").strip()
    title_text = str(payload.get("display_title") or payload.get("title") or slug or "Layout walkthrough").strip()
    title = html.escape(title_text)
    layout_focus = bool(payload.get("_generated_reconstruction_layout_focus"))
    launch_mode = "layout_preview" if layout_focus else "tour_public_launch"
    video_url, video_mime_type = _public_tour_walkthrough_media_context(payload)
    video_source_markup = _public_tour_walkthrough_source_markup(
        payload,
        video_url=video_url,
        video_mime_type=video_mime_type,
    )
    route_labels = _generated_reconstruction_route_labels(payload)
    scenes, _, _ = _tour_control_media_context(payload)
    route_stop_count = len(route_labels)
    photo_evidence_count = sum(1 for scene in scenes if str(scene.get("role") or "").strip().lower() == "photo")
    floorplan_evidence_count = sum(1 for scene in scenes if str(scene.get("role") or "").strip().lower() == "floorplan")
    scene_entries: list[dict[str, str]] = []
    for scene in scenes:
        image_url = str(scene.get("image_url") or "").strip()
        scene_name = str(scene.get("name") or "Scene").strip() or "Scene"
        mime_type = str(scene.get("mime_type") or "").strip().lower()
        role = str(scene.get("role") or "").strip().lower()
        is_pdf = mime_type.startswith("application/pdf")
        scene_entries.append(
            {
                "url": image_url,
                "name": scene_name,
                "mime_type": mime_type,
                "role": role,
                "kind": "document" if is_pdf else "image",
                "preview_label": "Plan reference" if role == "floorplan" else scene_name,
            }
        )
    media_cards = "".join(
        (
            f"""
            <button class="media-card" type="button" data-target="{html.escape(scene['url'])}" data-kind="{html.escape(scene['kind'])}" data-role="{html.escape(scene['role'])}" data-name="{html.escape(scene['name'])}" data-preview-label="{html.escape(scene['preview_label'])}">
              <img src="{html.escape(scene['url'])}" alt="{html.escape(scene['name'])}" referrerpolicy="no-referrer">
              <strong>{html.escape(scene['name'])}</strong>
            </button>"""
            if scene["kind"] != "document"
            else f"""
            <button class="media-card media-card-doc" type="button" data-target="{html.escape(scene['url'])}" data-kind="document" data-role="{html.escape(scene['role'])}" data-name="{html.escape(scene['name'])}" data-preview-label="{html.escape(scene['preview_label'])}">
              <span class="doc-mark">PDF</span>
              <strong>{html.escape(scene['name'])}</strong>
            </button>"""
            )
        for scene in scene_entries
    )
    first_scene_url_raw = str(scene_entries[0].get("url") or "").strip() if scene_entries else ""
    first_scene_url = html.escape(first_scene_url_raw)
    first_scene_name = html.escape(str(scene_entries[0].get("name") or "Reference scene")) if scene_entries else "Reference scene"
    lead_preview_url_raw = str(payload.get("_lead_preview_url") or first_scene_url_raw or "").strip()
    lead_preview_url = html.escape(lead_preview_url_raw)
    route_actions = _generated_reconstruction_launch_route_actions(payload, scene_entries=scene_entries)
    initial_route_action = dict(route_actions[0]) if route_actions else {}
    initial_route_label = html.escape(
        str(initial_route_action.get("focus_label") or initial_route_action.get("label") or "Route stop").strip() or "Route stop"
    )
    initial_route_position = (
        f"Stop {int(initial_route_action.get('index') or 0) + 1} / {len(route_actions)}"
        if route_actions
        else "Route stop"
    )
    initial_route_mode = _generated_reconstruction_focus_mode_label(initial_route_action.get("focus_mode"))
    initial_route_summary_parts = [
        part
        for part in (
            initial_route_mode,
            str(initial_route_action.get("duration_label") or "").strip(),
        )
        if part
    ]
    initial_route_summary = html.escape(" · ".join(initial_route_summary_parts) or initial_route_mode or "Reference cue")
    has_floorplan_reference = any(str(scene.get("role") or "").strip().lower() == "floorplan" for scene in scene_entries)
    layout_viewer_relpath = _generated_reconstruction_layout_preview_relpath(payload)
    layout_viewer_open_url_raw = (
        f"/tours/files/{urllib.parse.quote(slug, safe='')}/{urllib.parse.quote(layout_viewer_relpath, safe='/')}"
        if slug and layout_viewer_relpath
        else ""
    )
    layout_viewer_embed_url_raw = (
        f"/tours/files/{urllib.parse.quote(slug, safe='')}/{urllib.parse.quote(layout_viewer_relpath, safe='/')}?embed=1"
        if slug and layout_viewer_relpath
        else ""
    )
    if layout_viewer_embed_url_raw and bool(payload.get("_generated_reconstruction_browser_shell_probe")):
        layout_viewer_embed_url_raw = f"{layout_viewer_embed_url_raw}&shell_probe=1"
    layout_viewer_open_url = html.escape(layout_viewer_open_url_raw)
    layout_viewer_embed_url = html.escape(layout_viewer_embed_url_raw)
    layout_viewer_poster_url_raw = lead_preview_url_raw or first_scene_url_raw
    layout_viewer_poster_url = html.escape(layout_viewer_poster_url_raw)
    route_stat_label = f"{route_stop_count} stop{'s' if route_stop_count != 1 else ''}"
    photo_stat_label = f"{photo_evidence_count} photo{'s' if photo_evidence_count != 1 else ''}"
    plan_stat_label = f"{floorplan_evidence_count} plan cue{'s' if floorplan_evidence_count != 1 else ''}"
    hero_eyebrow = "PropertyQuarry layout preview" if layout_focus else "PropertyQuarry layout tour"
    hero_sub = (
        "Start in the interactive layout viewer, then compare the room route and source images from the same generated reconstruction."
        if layout_focus
        else "Walk the likely room order, inspect the floorplan, and compare the source images from one guided surface. This is built from the floorplan and listing photos, not from a captured provider tour."
    )
    lead_preview_badge = "Generated diorama"
    lead_preview_title = "Start with room adjacency" if layout_focus else "Likely spatial layout"
    lead_preview_copy = (
        "Open the layout viewer first, then use the walkthrough and source deck to double-check room order, connections, and likely light."
        if layout_focus
        else "Built from the floorplan and listing photos so you can screen room order, adjacency, and likely light before opening the walkthrough."
    )
    primary_cta_href = (
        "#layout-viewer"
        if layout_focus and layout_viewer_embed_url_raw
        else "#walkthrough"
    )
    primary_cta_label = (
        "Open layout viewer"
        if layout_focus and layout_viewer_embed_url_raw
        else ("Play walkthrough" if video_url else "Start room route")
    )
    secondary_cta_href = "#walkthrough" if layout_focus else "#reference-focus"
    secondary_cta_label = (
        "Play room route"
        if layout_focus and video_url
        else ("Start room route" if layout_focus else ("Review floorplan cue" if has_floorplan_reference else "Review reference deck"))
    )
    route_markup = "".join(
        f"""
        <li>
          <button class="route-action" type="button" data-route-index="{int(action['index'])}" data-route-label="{html.escape(str(action['label'] or 'Route stop'))}" data-seek-start="{float(action['start_seconds']):.3f}" data-seek-end="{float(action['end_seconds']):.3f}" data-duration-seconds="{float(action['duration_seconds']):.3f}" data-duration-label="{html.escape(str(action.get('duration_label') or ''))}" data-cue-label="{html.escape(str(action.get('cue_label') or 'Reference cue'))}" data-scene-index="{int(action['scene_index'])}" data-focus-label="{html.escape(str(action['focus_label'] or ''))}" data-focus-mode="{html.escape(str(action['focus_mode'] or ''))}">
            <span class="route-step">{int(action['index']) + 1}</span>
            <span class="route-copy">
              <strong class="route-name">{html.escape(str(action['label'] or 'Route stop'))}</strong>
              <span class="route-meta">
                <span class="route-pill">{html.escape(str(action.get('cue_label') or 'Reference cue'))}</span>
                {f'<span class="route-pill muted">{html.escape(str(action.get("duration_label") or ""))}</span>' if str(action.get("duration_label") or "").strip() else ''}
              </span>
            </span>
          </button>
        </li>"""
        for action in route_actions
    )
    layout_viewer_section = (
        f'''
      <section class="card layout-viewer-card" id="layout-viewer">
        <div class="layout-viewer-head">
          <div class="stack">
            <div class="eyebrow">{"Layout-first viewer" if layout_focus else "Spatial layout viewer"}</div>
            <h2>{"Start in the layout viewer" if layout_focus else "Walk the generated layout"}</h2>
            <p class="layout-viewer-note">{"The layout viewer is the main entrypoint on this page. Use the walkthrough and source deck below to cross-check what the generated route is suggesting." if layout_focus else "The same disclosed reconstruction, but with an interactive room-to-room spatial pass."}</p>
          </div>
          <a class="mini-btn" id="layout-viewer-open" href="{layout_viewer_open_url}" target="_blank" rel="noopener noreferrer">Open full viewer</a>
        </div>
        <div class="layout-viewer-shell">
          <div class="layout-viewer-poster" id="layout-viewer-poster">
            {f'<img class="layout-viewer-poster-media" src="{layout_viewer_poster_url}" alt="" aria-hidden="true" referrerpolicy="no-referrer">' if layout_viewer_poster_url_raw else ''}
            <div class="layout-viewer-poster-copy">
              <div class="eyebrow">{"Layout-first entry" if layout_focus else "Interactive layout viewer"}</div>
              <strong>{"Loading the room layout" if layout_focus else "Loading spatial layout"}</strong>
              <p>{"The generated layout opens first here so you can judge adjacency and route order before committing to the walkthrough." if layout_focus else "Room-to-room pass from the same generated reconstruction, with the disclosed layout ready inside the viewer."}</p>
            </div>
          </div>
          <iframe id="layout-viewer-frame" src="{layout_viewer_embed_url}" title="{html.escape(title_text)} spatial layout viewer" loading="lazy" referrerpolicy="no-referrer"></iframe>
        </div>
        <p class="layout-viewer-note">Generated from the floorplan and listing photos. Use it to judge adjacency, route order, and approximate spatial proportion.</p>
      </section>'''
        if layout_viewer_embed_url_raw
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} | PropertyQuarry</title>
    <style nonce="{nonce_attr}">
      :root {{ color-scheme: light; --ink:#17130c; --muted:#766d5e; --paper:#f6f0e5; --card:rgba(255,252,245,.84); --line:rgba(54,42,27,.14); --gold:#a77c2b; --gold-soft:rgba(167,124,43,.14); --shadow:0 24px 70px rgba(68,47,24,.12); }}
      * {{ box-sizing:border-box; }}
      html, body {{ margin:0; min-height:100%; background:radial-gradient(circle at 18% 10%, rgba(255,255,255,.92), transparent 28%), linear-gradient(135deg,#faf6ed 0%,#efe4d1 48%,#d9c4a5 100%); color:var(--ink); font-family:Aptos, ui-sans-serif, system-ui, sans-serif; }}
      body {{ padding:18px; }}
      .shell {{ width:min(1320px, 100%); margin:0 auto; display:grid; gap:18px; }}
      .hero {{ display:grid; grid-template-columns:minmax(0, 1.3fr) minmax(320px, .7fr); gap:18px; }}
      .card {{ border:1px solid var(--line); border-radius:28px; background:var(--card); box-shadow:var(--shadow); }}
      .hero-main {{ padding:24px; display:grid; gap:14px; }}
      .eyebrow {{ color:var(--muted); font-size:12px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; }}
      h1 {{ margin:0; font-family:Georgia, ui-serif, serif; font-size:clamp(34px, 5vw, 62px); line-height:.92; letter-spacing:-.055em; }}
      .sub {{ margin:0; color:var(--muted); font-size:15px; line-height:1.5; max-width:52ch; }}
      .actions {{ display:flex; flex-wrap:wrap; gap:10px; }}
      .btn {{ min-height:46px; display:inline-flex; align-items:center; justify-content:center; border-radius:999px; padding:0 16px; font:inherit; font-weight:700; text-decoration:none; border:1px solid var(--line); }}
      .btn.primary {{ background:#17130c; color:#fff7eb; border-color:#17130c; }}
      .btn.secondary {{ background:var(--gold-soft); color:#6c4c16; }}
      .hero-side {{ padding:0; display:grid; gap:0; align-content:start; overflow:hidden; }}
      .lead-preview-shell {{ position:relative; min-height:420px; background:linear-gradient(160deg, #b69f7b 0%, #7f694c 100%); }}
      .lead-preview-shell img {{ display:block; width:100%; min-height:420px; height:100%; object-fit:cover; background:#111; }}
      .lead-preview-shell::after {{ content:""; position:absolute; inset:0; background:linear-gradient(180deg, rgba(17,13,9,.04) 0%, rgba(17,13,9,.14) 42%, rgba(17,13,9,.58) 100%); pointer-events:none; }}
      .lead-preview-overlay {{ position:absolute; inset:auto 18px 18px 18px; z-index:1; display:grid; gap:10px; align-content:end; color:#fff7eb; text-shadow:0 2px 12px rgba(17,13,9,.24); }}
      .lead-preview-badge {{ min-height:30px; width:fit-content; max-width:100%; display:inline-flex; align-items:center; padding:0 11px; border-radius:999px; border:1px solid rgba(255,247,235,.18); background:rgba(255,247,235,.12); color:inherit; font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }}
      .lead-preview-title {{ max-width:11ch; font-family:Georgia, ui-serif, serif; font-size:clamp(26px, 3vw, 38px); line-height:.94; letter-spacing:-.04em; text-wrap:balance; }}
      .lead-preview-caption {{ padding:16px 18px 18px; display:grid; gap:12px; border-top:1px solid rgba(54,42,27,.08); background:linear-gradient(180deg, rgba(255,252,245,.86), rgba(255,252,245,.72)); }}
      .lead-preview-copy {{ margin:0; max-width:38ch; color:#5d5141; font-size:13px; line-height:1.45; }}
      .lead-preview-stats {{ display:flex; flex-wrap:wrap; gap:8px; }}
      .lead-preview-stat {{ min-height:30px; display:inline-flex; align-items:center; padding:0 11px; border-radius:999px; border:1px solid rgba(54,42,27,.1); background:rgba(255,255,255,.58); color:#6c4c16; font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }}
      .stack {{ display:grid; gap:10px; }}
      .kv {{ display:grid; gap:4px; border:1px solid var(--line); border-radius:18px; padding:12px 13px; background:rgba(255,255,255,.44); }}
      .kv b {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; }}
      .stage {{ display:grid; grid-template-columns:minmax(0, 1.15fr) minmax(320px, .85fr); gap:18px; }}
      .video-card {{ padding:16px; display:grid; gap:12px; }}
      .video-stage {{ position:relative; overflow:hidden; border-radius:22px; border:1px solid var(--line); background:#111; }}
      .video-stage video, .video-stage img {{ display:block; width:100%; min-height:360px; max-height:62vh; object-fit:cover; background:#111; }}
      .walkthrough-hud {{ position:absolute; top:16px; left:16px; z-index:2; display:grid; gap:8px; max-width:min(72%, 460px); padding:14px 16px; border-radius:18px; background:linear-gradient(180deg, rgba(23,19,12,.78), rgba(23,19,12,.46)); color:#fff7eb; box-shadow:0 18px 40px rgba(16,12,7,.24); backdrop-filter:blur(10px); pointer-events:none; }}
      .walkthrough-chip-row {{ display:flex; flex-wrap:wrap; gap:8px; }}
      .walkthrough-chip {{ min-height:30px; display:inline-flex; align-items:center; padding:0 10px; border-radius:999px; border:1px solid rgba(255,247,235,.16); background:rgba(255,247,235,.12); color:inherit; font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }}
      .walkthrough-chip.muted {{ background:rgba(255,247,235,.08); color:rgba(255,247,235,.88); }}
      .walkthrough-stop-label {{ font-family:Georgia, ui-serif, serif; font-size:clamp(24px, 2.9vw, 34px); line-height:.98; letter-spacing:-.04em; text-wrap:balance; text-shadow:0 1px 0 rgba(0,0,0,.18); }}
      .walkthrough-toolbar {{ display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:12px; }}
      .walkthrough-nav {{ display:flex; flex-wrap:wrap; gap:8px; }}
      .mini-btn {{ min-height:40px; display:inline-flex; align-items:center; justify-content:center; border-radius:999px; padding:0 14px; border:1px solid var(--line); background:rgba(255,255,255,.58); color:#6c4c16; font:inherit; font-weight:700; cursor:pointer; }}
      .mini-btn[disabled] {{ opacity:.42; cursor:default; }}
      .walkthrough-route-summary {{ color:var(--muted); font-size:13px; font-weight:700; letter-spacing:.02em; }}
      .walkthrough-progress {{ display:grid; gap:8px; }}
      .walkthrough-progress-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; color:var(--muted); font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }}
      .walkthrough-progress-track {{ position:relative; height:12px; border-radius:999px; background:rgba(108,76,22,.12); overflow:visible; }}
      .walkthrough-progress-fill {{ position:absolute; inset:0 auto 0 0; width:0%; border-radius:999px; background:linear-gradient(90deg, #8c6620 0%, #d7b36c 100%); box-shadow:0 10px 18px rgba(140,102,32,.18); }}
      .walkthrough-progress-marker {{ position:absolute; top:50%; width:14px; height:14px; margin:0; border:2px solid #fff7eb; border-radius:999px; background:rgba(167,124,43,.34); box-shadow:0 0 0 1px rgba(23,19,12,.06); transform:translate(-50%, -50%); cursor:pointer; }}
      .walkthrough-progress-marker[data-focus-mode="floorplan"] {{ background:rgba(99,126,172,.5); }}
      .walkthrough-progress-marker.is-active {{ background:#17130c; box-shadow:0 0 0 3px rgba(167,124,43,.22); }}
      .video-note {{ margin:0; color:var(--muted); font-size:13px; line-height:1.45; }}
      .sidebar {{ padding:18px; display:grid; gap:16px; align-content:start; }}
      .sidebar-block {{ display:grid; gap:10px; align-content:start; }}
      .sidebar h2 {{ margin:0; font-size:16px; letter-spacing:-.02em; }}
      .reference-focus {{ display:grid; gap:10px; }}
      .reference-shell {{ overflow:hidden; border-radius:20px; border:1px solid var(--line); background:rgba(255,255,255,.58); min-height:220px; display:grid; }}
      .reference-shell img {{ display:block; width:100%; min-height:220px; height:100%; object-fit:cover; background:#fff; }}
      .reference-shell-doc {{ min-height:220px; padding:18px; display:grid; align-content:center; gap:10px; background:linear-gradient(160deg, rgba(255,255,255,.72), rgba(247,239,225,.88)); }}
      .reference-shell-doc strong {{ font-size:14px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }}
      .reference-shell-doc b {{ font-family:Georgia, ui-serif, serif; font-size:28px; line-height:1; }}
      .reference-meta {{ display:grid; gap:6px; }}
      .reference-badge-row {{ display:flex; flex-wrap:wrap; gap:8px; }}
      .reference-badge {{ min-height:28px; display:inline-flex; align-items:center; padding:0 10px; border-radius:999px; border:1px solid var(--line); background:rgba(255,255,255,.64); color:#6c4c16; font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }}
      .reference-meta b {{ font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.1em; }}
      .reference-meta strong {{ font-size:17px; line-height:1.2; }}
      .reference-meta a {{ min-height:44px; display:inline-flex; align-items:center; justify-content:center; width:fit-content; max-width:100%; padding:0 16px; border-radius:999px; border:1px solid var(--line); background:rgba(255,255,255,.58); color:#6c4c16; font-weight:700; text-decoration:none; }}
      .route-list {{ margin:0; padding-left:20px; display:grid; gap:8px; color:var(--ink); }}
      .route-list li {{ line-height:1.35; }}
      .route-action {{ width:100%; display:grid; grid-template-columns:auto 1fr; gap:10px; align-items:center; text-align:left; border:1px solid var(--line); border-radius:16px; background:rgba(255,255,255,.46); padding:10px 12px; color:inherit; font:inherit; cursor:pointer; }}
      .route-action.is-active {{ border-color:rgba(108,76,22,.46); box-shadow:0 0 0 2px rgba(167,124,43,.16); background:rgba(255,255,255,.78); }}
      .route-step {{ min-width:28px; height:28px; display:inline-grid; place-items:center; border-radius:999px; background:var(--gold-soft); color:#6c4c16; font-size:12px; font-weight:700; }}
      .route-copy {{ display:grid; gap:5px; min-width:0; }}
      .route-name {{ display:block; font-size:14px; line-height:1.2; }}
      .route-meta {{ display:flex; flex-wrap:wrap; gap:6px; }}
      .route-pill {{ min-height:24px; display:inline-flex; align-items:center; padding:0 8px; border-radius:999px; background:rgba(167,124,43,.14); color:#6c4c16; font-size:10px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }}
      .route-pill.muted {{ background:rgba(23,19,12,.06); color:var(--muted); }}
      .media-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }}
      .media-card {{ width:100%; text-align:left; border:1px solid var(--line); border-radius:18px; background:rgba(255,255,255,.42); padding:0; color:inherit; cursor:pointer; overflow:hidden; }}
      .media-card img {{ display:block; width:100%; aspect-ratio:1.25; object-fit:cover; background:#fff; }}
      .media-card strong, .media-card .doc-mark {{ display:block; padding:10px 12px 12px; }}
      .media-card-doc {{ min-height:170px; display:grid; align-content:center; justify-items:start; padding:16px; }}
      .media-card.is-active {{ border-color:rgba(108,76,22,.46); box-shadow:0 0 0 2px rgba(167,124,43,.16); background:rgba(255,255,255,.76); }}
      .doc-mark {{ font-size:28px; font-family:Georgia, ui-serif, serif; padding-bottom:2px; }}
      .disclosure {{ margin:0; color:var(--muted); font-size:13px; line-height:1.45; }}
      .layout-viewer-card {{ padding:18px; display:grid; gap:14px; }}
      .layout-viewer-head {{ display:flex; flex-wrap:wrap; align-items:flex-start; justify-content:space-between; gap:12px; }}
      .layout-viewer-head h2 {{ margin:0; font-size:clamp(24px, 3.2vw, 34px); line-height:.98; letter-spacing:-.04em; font-family:Georgia, ui-serif, serif; }}
      .layout-viewer-shell {{ position:relative; overflow:hidden; border-radius:24px; border:1px solid var(--line); background:linear-gradient(180deg, #f1e6d6, #e4d3bb); min-height:560px; }}
      .layout-viewer-shell iframe {{ position:relative; z-index:2; display:block; width:100%; height:min(78vh, 860px); min-height:560px; border:0; background:#111; opacity:0; transition:opacity .24s ease; }}
      .layout-viewer-shell.is-ready iframe {{ opacity:1; }}
      .layout-viewer-poster {{ position:absolute; inset:0; z-index:1; display:grid; align-content:end; padding:22px; background-position:center; background-size:cover; background-repeat:no-repeat; transition:opacity .24s ease, visibility .24s ease; }}
      .layout-viewer-poster-media {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
      .layout-viewer-poster::after {{ content:""; position:absolute; inset:0; background:linear-gradient(180deg, rgba(17,13,9,.1) 0%, rgba(17,13,9,.2) 40%, rgba(17,13,9,.72) 100%); }}
      .layout-viewer-shell.is-ready .layout-viewer-poster {{ opacity:0; visibility:hidden; }}
      .layout-viewer-poster-copy {{ position:relative; z-index:1; display:grid; gap:8px; width:min(420px, 100%); padding:18px; border-radius:22px; background:linear-gradient(180deg, rgba(23,19,12,.78), rgba(23,19,12,.52)); color:#fff7eb; box-shadow:0 18px 42px rgba(16,12,7,.18); backdrop-filter:blur(10px); }}
      .layout-viewer-poster-copy strong {{ font-family:Georgia, ui-serif, serif; font-size:clamp(26px, 3vw, 34px); line-height:.96; letter-spacing:-.04em; }}
      .layout-viewer-poster-copy p {{ margin:0; color:rgba(255,247,235,.88); font-size:13px; line-height:1.45; }}
      .layout-viewer-note {{ margin:0; color:var(--muted); font-size:13px; line-height:1.5; max-width:62ch; }}
      @media (max-width: 980px) {{
        body {{ padding:10px; }}
        .hero, .stage {{ grid-template-columns:1fr; }}
        .sidebar-route {{ order:1; }}
        .sidebar-reference {{ order:2; }}
        .sidebar-deck {{ order:3; }}
        .media-grid {{ grid-template-columns:repeat(2, minmax(0,1fr)); }}
        .video-stage video, .video-stage img {{ min-height:280px; max-height:42vh; }}
        .walkthrough-hud {{ max-width:calc(100% - 32px); }}
        .layout-viewer-shell {{ min-height:460px; }}
        .layout-viewer-shell iframe {{ min-height:460px; height:66vh; }}
      }}
      @media (max-width: 620px) {{
        .hero-main, .hero-side, .video-card, .sidebar {{ border-radius:22px; }}
        .lead-preview-shell, .lead-preview-shell img {{ min-height:320px; }}
        .media-grid {{ grid-template-columns:1fr 1fr; }}
        .walkthrough-hud {{ top:12px; left:12px; right:12px; padding:12px 13px; }}
        .walkthrough-stop-label {{ font-size:22px; }}
        .layout-viewer-shell {{ min-height:380px; }}
        .layout-viewer-shell iframe {{ min-height:380px; height:58vh; }}
      }}
    </style>
  </head>
  <body>
    <div class="shell" data-launch-mode="{html.escape(launch_mode)}">
      <section class="hero">
        <div class="card hero-main">
          <div class="eyebrow">{html.escape(hero_eyebrow)}</div>
          <h1>{title}</h1>
          <p class="sub">{html.escape(hero_sub)}</p>
          <div class="actions">
            <a class="btn primary" href="{html.escape(primary_cta_href)}">{html.escape(primary_cta_label)}</a>
            <a class="btn secondary" href="{html.escape(secondary_cta_href)}">{html.escape(secondary_cta_label)}</a>
          </div>
        </div>
        <aside class="card hero-side" id="lead-preview-panel">
          <div class="lead-preview-shell">
            {f'<img id="lead-preview-image" src="{lead_preview_url}" alt="{html.escape(title_text)} generated diorama" referrerpolicy="no-referrer">' if lead_preview_url_raw else ''}
            <div class="lead-preview-overlay">
              <span class="lead-preview-badge" id="lead-preview-badge">{html.escape(lead_preview_badge)}</span>
              <strong class="lead-preview-title">{html.escape(lead_preview_title)}</strong>
            </div>
          </div>
          <div class="lead-preview-caption">
            <p class="lead-preview-copy" id="lead-preview-copy">{html.escape(lead_preview_copy)}</p>
            <div class="lead-preview-stats" id="lead-preview-stats">
              <span class="lead-preview-stat">{html.escape(route_stat_label)}</span>
              <span class="lead-preview-stat">{html.escape(photo_stat_label)}</span>
              <span class="lead-preview-stat">{html.escape(plan_stat_label)}</span>
            </div>
          </div>
        </aside>
      </section>
      {layout_viewer_section if layout_focus else ''}
      <section class="stage">
        <div class="card video-card" id="walkthrough">
          <h2>Walkthrough</h2>
          {f'''<div class="video-stage">
            <div class="walkthrough-hud" id="walkthrough-hud" aria-live="polite" aria-atomic="true">
              <div class="walkthrough-chip-row">
                <span class="walkthrough-chip" id="walkthrough-stop-position">{html.escape(initial_route_position)}</span>
                <span class="walkthrough-chip muted" id="walkthrough-stop-mode">{html.escape(initial_route_mode)}</span>
              </div>
              <strong class="walkthrough-stop-label" id="walkthrough-stop-name">{initial_route_label}</strong>
            </div>
            <video id="tour-video" controls playsinline webkit-playsinline="true" preload="metadata" poster="{first_scene_url}">{video_source_markup}</video>
          </div>''' if video_url else f'''<div class="video-stage">
            <div class="walkthrough-hud" id="walkthrough-hud" aria-live="polite" aria-atomic="true">
              <div class="walkthrough-chip-row">
                <span class="walkthrough-chip" id="walkthrough-stop-position">{html.escape(initial_route_position)}</span>
                <span class="walkthrough-chip muted" id="walkthrough-stop-mode">{html.escape(initial_route_mode)}</span>
              </div>
              <strong class="walkthrough-stop-label" id="walkthrough-stop-name">{initial_route_label}</strong>
            </div>
            <img src="{first_scene_url}" alt="{first_scene_name}">
          </div>'''}
          <div class="walkthrough-toolbar">
            <div class="walkthrough-nav">
              <button type="button" class="mini-btn" id="route-prev" aria-label="Go to previous route stop">Previous stop</button>
              <button type="button" class="mini-btn" id="route-next" aria-label="Go to next route stop">Next stop</button>
            </div>
            <div class="walkthrough-route-summary" id="walkthrough-route-summary">{initial_route_summary}</div>
          </div>
          <div class="walkthrough-progress" aria-label="Walkthrough route progress">
            <div class="walkthrough-progress-head">
              <span id="walkthrough-progress-status">Route progress</span>
              <span id="walkthrough-progress-time">0:00 / 0:00</span>
            </div>
            <div class="walkthrough-progress-track" id="walkthrough-progress-track">
              <span class="walkthrough-progress-fill" id="walkthrough-progress-fill"></span>
            </div>
          </div>
          <p class="video-note">The walkthrough follows the room route and keeps the floorplan visible as a secondary cue instead of pretending to be a captured 360 tour.</p>
        </div>
        <aside class="card sidebar">
          <section class="reference-focus sidebar-block sidebar-reference" id="reference-focus">
            <h2>Reference focus</h2>
            <div class="reference-shell" id="reference-shell"></div>
            <div class="reference-meta">
              <b>Selected</b>
              <div class="reference-badge-row">
                <span class="reference-badge" id="reference-focus-kind">Reference cue</span>
              </div>
              <strong id="reference-focus-name">Reference scene</strong>
              <a id="reference-focus-open" href="#" target="_blank" rel="noopener noreferrer">Open source image</a>
            </div>
          </section>
          <section class="sidebar-block sidebar-route">
            <h2>Room route</h2>
            <ol class="route-list">{route_markup or '<li>Route labels unavailable</li>'}</ol>
          </section>
          <section class="sidebar-block sidebar-deck" id="reference-deck">
            <h2>Reference deck</h2>
            <div class="media-grid" id="media-grid">{media_cards or '<p class="disclosure">Reference media unavailable.</p>'}</div>
          </section>
        </aside>
      </section>
      {'' if layout_focus else layout_viewer_section}
    </div>
    <script nonce="{nonce_attr}">
      const mediaCards = Array.from(document.querySelectorAll('[data-target]'));
      const routeActions = Array.from(document.querySelectorAll('.route-action'));
      const routeMetadata = routeActions
        .map((action) => ({{
          action,
          routeIndex: Number(action.getAttribute('data-route-index')),
          label: String(action.getAttribute('data-route-label') || '').trim(),
          startSeconds: Number(action.getAttribute('data-seek-start')),
          endSeconds: Number(action.getAttribute('data-seek-end')),
          durationSeconds: Number(action.getAttribute('data-duration-seconds')),
          durationLabel: String(action.getAttribute('data-duration-label') || '').trim(),
          cueLabel: String(action.getAttribute('data-cue-label') || '').trim(),
          sceneIndex: Number(action.getAttribute('data-scene-index')),
          focusLabel: String(action.getAttribute('data-focus-label') || '').trim(),
          focusMode: String(action.getAttribute('data-focus-mode') || '').trim(),
        }}))
      const routeMetadataByIndex = new Map(
        routeMetadata
          .filter((item) => Number.isFinite(item.routeIndex))
          .map((item) => [item.routeIndex, item])
      );
      const routeTimeline = routeMetadata
        .filter((item) => Number.isFinite(item.startSeconds))
        .sort((left, right) => left.startSeconds - right.startSeconds);
      const walkthroughVideo = document.getElementById('tour-video');
      const referenceShell = document.getElementById('reference-shell');
      const referenceFocusName = document.getElementById('reference-focus-name');
      const referenceFocusKind = document.getElementById('reference-focus-kind');
      const referenceFocusOpen = document.getElementById('reference-focus-open');
      const walkthroughStopName = document.getElementById('walkthrough-stop-name');
      const walkthroughStopPosition = document.getElementById('walkthrough-stop-position');
      const walkthroughStopMode = document.getElementById('walkthrough-stop-mode');
      const walkthroughRouteSummary = document.getElementById('walkthrough-route-summary');
      const walkthroughProgressTrack = document.getElementById('walkthrough-progress-track');
      const walkthroughProgressFill = document.getElementById('walkthrough-progress-fill');
      const walkthroughProgressTime = document.getElementById('walkthrough-progress-time');
      const routePrev = document.getElementById('route-prev');
      const routeNext = document.getElementById('route-next');
      const layoutViewerShell = document.querySelector('.layout-viewer-shell');
      const layoutViewerFrame = document.getElementById('layout-viewer-frame');
      const progressMarkers = new Map();
      let pendingLayoutViewerRouteIndex = Number.NaN;
      let layoutViewerSyncedRouteIndex = Number.NaN;
      let layoutViewerLastState = null;
      let layoutViewerRouteButtonCount = 0;
      let layoutViewerFloorplanStopCount = 0;
      let layoutViewerRouteSyncAttempts = 0;
      let layoutViewerRouteSyncTimerId = 0;
      let layoutViewerReadyAttempts = 0;
      let manualRouteHoldUntil = 0;
      function holdManualRoute(milliseconds = 1400) {{
        manualRouteHoldUntil = Date.now() + Math.max(0, Number(milliseconds) || 0);
      }}
      function scheduleLayoutViewerRouteSync(delayMs = 0) {{
        if (layoutViewerRouteSyncTimerId) {{
          window.clearTimeout(layoutViewerRouteSyncTimerId);
        }}
        layoutViewerRouteSyncTimerId = window.setTimeout(() => {{
          layoutViewerRouteSyncTimerId = 0;
          applyLayoutViewerRouteSync();
        }}, Math.max(0, Number(delayMs) || 0));
      }}
      function mediaCardByIndex(rawIndex) {{
        const index = Number(rawIndex);
        if (!Number.isFinite(index) || index < 0 || !mediaCards[index]) return null;
        return mediaCards[index];
      }}
      function formatClock(rawSeconds) {{
        const totalSeconds = Math.max(0, Math.floor(Number(rawSeconds) || 0));
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        return String(minutes) + ':' + String(seconds).padStart(2, '0');
      }}
      function routeCueLabel(rawMode) {{
        const normalized = String(rawMode || '').trim().toLowerCase();
        if (normalized === 'floorplan') return 'Floorplan cue';
        if (normalized === 'photo') return 'Photo cue';
        if (normalized === 'document') return 'Document cue';
        return 'Reference cue';
      }}
      function effectiveTimelineTotal() {{
        if (walkthroughVideo && Number.isFinite(walkthroughVideo.duration) && walkthroughVideo.duration > 0.05) {{
          return Number(walkthroughVideo.duration);
        }}
        return routeTimeline.reduce((maxValue, item) => {{
          const endValue = Number.isFinite(item.endSeconds) ? item.endSeconds : item.startSeconds;
          return endValue > maxValue ? endValue : maxValue;
        }}, 0);
      }}
      function routeDurationLabel(routeItem) {{
        const label = String(routeItem?.durationLabel || '').trim();
        if (label) return label;
        const seconds = Number(routeItem?.durationSeconds);
        if (!Number.isFinite(seconds) || seconds <= 0.05) return '';
        if (seconds >= 60) {{
          const rounded = Math.round(seconds);
          const minutes = Math.floor(rounded / 60);
          const remainder = rounded % 60;
          return remainder ? String(minutes) + 'm ' + String(remainder).padStart(2, '0') + 's' : String(minutes) + 'm';
        }}
        return String(Math.max(1, Math.round(seconds))) + 's';
      }}
      function setActiveRoute(action) {{
        routeActions.forEach((node) => node.classList.toggle('is-active', node === action));
      }}
      function setActiveProgressMarker(routeItem) {{
        progressMarkers.forEach((marker, routeIndex) => {{
          marker.classList.toggle('is-active', !!routeItem && routeIndex === routeItem.routeIndex);
        }});
      }}
      function updateRouteTransport(routeItem) {{
        const routeIndex = Number(routeItem?.routeIndex);
        const maxIndex = routeMetadata.length - 1;
        if (routePrev) {{
          routePrev.disabled = !Number.isFinite(routeIndex) || routeIndex <= 0;
        }}
        if (routeNext) {{
          routeNext.disabled = !Number.isFinite(routeIndex) || routeIndex >= maxIndex;
        }}
      }}
      function updateWalkthroughProgress(routeItem, options = {{}}) {{
        const currentTime = Number.isFinite(options.currentTime) ? Number(options.currentTime) : (
          walkthroughVideo && Number.isFinite(walkthroughVideo.currentTime)
            ? Number(walkthroughVideo.currentTime)
            : Number(routeItem?.startSeconds || 0)
        );
        const totalDuration = effectiveTimelineTotal();
        const clampedTime = totalDuration > 0 ? Math.max(0, Math.min(currentTime, totalDuration)) : Math.max(0, currentTime);
        if (walkthroughProgressFill) {{
          const pct = totalDuration > 0 ? (clampedTime / totalDuration) * 100 : 0;
          walkthroughProgressFill.style.width = Math.max(0, Math.min(100, pct)).toFixed(2) + '%';
        }}
        if (walkthroughProgressTime) {{
          walkthroughProgressTime.textContent = formatClock(clampedTime) + ' / ' + formatClock(totalDuration);
        }}
        if (walkthroughRouteSummary) {{
          const parts = [];
          const cueLabel = String(routeItem?.cueLabel || routeCueLabel(routeItem?.focusMode || '')).trim();
          const durationLabel = routeDurationLabel(routeItem);
          if (cueLabel) parts.push(cueLabel);
          if (durationLabel) parts.push(durationLabel);
          walkthroughRouteSummary.textContent = parts.join(' · ') || 'Reference cue';
        }}
        setActiveProgressMarker(routeItem || null);
        updateRouteTransport(routeItem || null);
      }}
      function renderProgressMarkers() {{
        if (!walkthroughProgressTrack || progressMarkers.size || !routeTimeline.length) return;
        const totalDuration = effectiveTimelineTotal();
        routeTimeline.forEach((item) => {{
          const marker = document.createElement('button');
          marker.type = 'button';
          marker.className = 'walkthrough-progress-marker';
          marker.dataset.routeIndex = String(item.routeIndex);
          marker.dataset.focusMode = String(item.focusMode || '').trim().toLowerCase();
          marker.setAttribute('aria-label', 'Jump to ' + String(item.label || 'route stop'));
          const leftPct = totalDuration > 0 ? (Math.max(0, item.startSeconds) / totalDuration) * 100 : 0;
          marker.style.left = Math.max(0, Math.min(100, leftPct)).toFixed(2) + '%';
          marker.addEventListener('click', () => syncRouteSelection(item, {{ seek: true, forceFocus: true }}));
          walkthroughProgressTrack.append(marker);
          progressMarkers.set(item.routeIndex, marker);
        }});
      }}
      function selectAdjacentRoute(offset) {{
        if (!routeMetadata.length) return;
        const activeIndex = routeMetadata.findIndex((item) => item.action.classList.contains('is-active'));
        const startIndex = activeIndex >= 0 ? activeIndex : 0;
        const nextIndex = Math.max(0, Math.min(routeMetadata.length - 1, startIndex + offset));
        const routeItem = routeMetadata[nextIndex];
        if (!routeItem) return;
        syncRouteSelection(routeItem, {{ seek: true, forceFocus: true, manual: true }});
      }}
      function applyLayoutViewerRouteSync() {{
        if (!layoutViewerFrame || !Number.isFinite(pendingLayoutViewerRouteIndex)) return;
        try {{
          const viewerWindow = layoutViewerFrame.contentWindow;
          const debug = viewerWindow && viewerWindow.__pqReconstructionDebug;
          if (debug && typeof debug.setRouteView === 'function') {{
            const nextRouteIndex = Number(pendingLayoutViewerRouteIndex);
            const applyRoute = () => {{
              if (Number(pendingLayoutViewerRouteIndex) !== nextRouteIndex) return;
              try {{
	                const liveDebug = layoutViewerFrame?.contentWindow?.__pqReconstructionDebug;
	                if (liveDebug && typeof liveDebug.setRouteView === 'function') {{
	                  liveDebug.setRouteView(nextRouteIndex, {{ immediate: true }});
	                  layoutViewerSyncedRouteIndex = nextRouteIndex;
	                  layoutViewerRouteSyncAttempts = 0;
	                  return;
	                }}
              }} catch (_error) {{
                // Retry below.
              }}
              if (layoutViewerRouteSyncAttempts >= 100) return;
              layoutViewerRouteSyncAttempts += 1;
              scheduleLayoutViewerRouteSync(160);
            }};
            applyRoute();
            return;
          }}
        }} catch (_error) {{
          return;
        }}
        if (layoutViewerRouteSyncAttempts >= 100) return;
        layoutViewerRouteSyncAttempts += 1;
        scheduleLayoutViewerRouteSync(160);
      }}
      function syncLayoutViewerRoute(routeItem) {{
        if (!layoutViewerFrame || !routeItem) return;
        const routeIndex = Number(routeItem.routeIndex);
        if (!Number.isFinite(routeIndex)) return;
        pendingLayoutViewerRouteIndex = routeIndex;
        layoutViewerRouteSyncAttempts = 0;
        scheduleLayoutViewerRouteSync();
      }}
      function layoutViewerRenderedReady() {{
        if (!layoutViewerFrame) return false;
        try {{
          const viewerWindow = layoutViewerFrame.contentWindow;
          const debug = viewerWindow && viewerWindow.__pqReconstructionDebug;
          if (!debug) return false;
          const renderMetrics = typeof debug.getRenderMetrics === 'function'
            ? debug.getRenderMetrics()
            : null;
          const liveState = typeof debug.getLiveState === 'function'
            ? debug.getLiveState()
            : null;
	          const metrics = renderMetrics && typeof renderMetrics === 'object'
	            ? {{ ...(liveState && typeof liveState === 'object' ? liveState : {{}}), ...renderMetrics }}
	            : (liveState && typeof liveState === 'object' ? liveState : null);
	          if (metrics && metrics.ready) {{
	            layoutViewerLastState = metrics;
	            const doc = layoutViewerFrame.contentDocument;
	            layoutViewerRouteButtonCount = Number(doc?.querySelectorAll('.route-button').length || layoutViewerRouteButtonCount || 0);
	            layoutViewerFloorplanStopCount = Number(doc?.querySelectorAll('.floorplan-stop').length || layoutViewerFloorplanStopCount || 0);
	          }}
	          return Boolean(
	            metrics &&
            metrics.ready &&
            Number(metrics.frameCount || 0) >= 2 &&
            Number(metrics.renderCalls || 0) > 0 &&
            Number(metrics.renderTriangles || 0) > 0
          );
        }} catch (_error) {{
          return false;
        }}
      }}
      function revealLayoutViewerWhenReady() {{
        if (!layoutViewerShell || !layoutViewerFrame) return;
        if (layoutViewerRenderedReady()) {{
	          layoutViewerShell.classList.add('is-ready');
	          layoutViewerReadyAttempts = 0;
	          return;
        }}
        if (layoutViewerReadyAttempts >= 48) return;
        layoutViewerReadyAttempts += 1;
        window.setTimeout(revealLayoutViewerWhenReady, 180);
      }}
      function renderReferenceFocus(card, options = {{}}) {{
        if (!referenceShell || !referenceFocusName || !referenceFocusOpen || !card) return;
        const target = String(card.getAttribute('data-target') || '').trim();
        const kind = String(card.getAttribute('data-kind') || 'image').trim();
        const role = String(card.getAttribute('data-role') || '').trim();
        const name = String(card.getAttribute('data-name') || 'Reference scene').trim() || 'Reference scene';
        const previewLabel = String(card.getAttribute('data-preview-label') || name).trim() || name;
        const focusLabel = String(options?.focusLabel || '').trim();
        const selectedLabel = focusLabel || previewLabel || name;
        referenceShell.replaceChildren();
        if (kind === 'document') {{
          const doc = document.createElement('div');
          doc.className = 'reference-shell-doc';
          const eyebrow = document.createElement('strong');
          eyebrow.textContent = 'Reference file';
          const title = document.createElement('b');
          title.textContent = previewLabel;
          const note = document.createElement('span');
          note.className = 'disclosure';
          note.textContent = 'Use the original document for dimension and doorway checks.';
          doc.append(eyebrow, title, note);
          referenceShell.append(doc);
          referenceFocusOpen.textContent = 'Open document';
        }} else {{
          const image = document.createElement('img');
          image.id = 'reference-focus-image';
          image.src = target;
          image.alt = name;
          image.referrerPolicy = 'no-referrer';
          referenceShell.append(image);
          referenceFocusOpen.textContent = role === 'floorplan' ? 'Open floorplan' : 'Open source image';
        }}
        referenceFocusName.textContent = selectedLabel;
        if (referenceFocusKind) {{
          referenceFocusKind.textContent = routeCueLabel(role || kind);
        }}
        referenceFocusOpen.href = target || '#';
        referenceShell.dataset.focusTarget = target;
        referenceShell.dataset.focusRole = role;
        referenceShell.dataset.focusLabel = selectedLabel;
        mediaCards.forEach((node) => node.classList.toggle('is-active', node === card));
      }}
      function updateWalkthroughHud(routeItem, options = {{}}) {{
        if (!routeItem) return;
        const card = options.card || mediaCardByIndex(routeItem.sceneIndex);
        const label = String(options.label || routeItem.focusLabel || routeItem.label || card?.getAttribute('data-preview-label') || card?.getAttribute('data-name') || 'Route stop').trim() || 'Route stop';
        const step = Number(routeItem.routeIndex);
        if (walkthroughStopPosition) {{
          walkthroughStopPosition.textContent = Number.isFinite(step) && step >= 0 && routeMetadata.length
            ? 'Stop ' + String(step + 1) + ' / ' + String(routeMetadata.length)
            : 'Route stop';
        }}
        if (walkthroughStopMode) {{
          walkthroughStopMode.textContent = routeCueLabel(routeItem.focusMode || card?.getAttribute('data-role') || '');
        }}
        if (walkthroughStopName) {{
          walkthroughStopName.textContent = label;
        }}
        updateWalkthroughProgress(routeItem, options);
      }}
      function syncRouteSelection(routeItem, options = {{}}) {{
        if (!routeItem || !routeItem.action) return;
        if (options.manual === true) {{
          const selectedDurationMs = Number(routeItem.durationSeconds || 0) * 1000;
          holdManualRoute(Math.min(30000, Math.max(8000, selectedDurationMs + 1000)));
        }}
        const card = mediaCardByIndex(routeItem.sceneIndex);
        const selectedLabel = String(routeItem.focusLabel || routeItem.label || card?.getAttribute('data-preview-label') || card?.getAttribute('data-name') || 'Route stop').trim() || 'Route stop';
        const expectedTarget = String(card?.getAttribute('data-target') || '').trim();
        const currentTarget = String(referenceShell?.dataset.focusTarget || '').trim();
        const currentLabel = String(referenceFocusName?.textContent || '').trim();
        if (!routeItem.action.classList.contains('is-active')) {{
          setActiveRoute(routeItem.action);
        }}
        if (card && (options.forceFocus === true || currentTarget !== expectedTarget || currentLabel !== selectedLabel)) {{
          renderReferenceFocus(card, {{ focusLabel: String(routeItem.focusLabel || '').trim() }});
        }}
        updateWalkthroughHud(routeItem, {{ card, label: selectedLabel }});
        syncLayoutViewerRoute(routeItem);
        if (options.seek === true) {{
          seekWalkthrough(routeItem.startSeconds, {{ play: options.playAfterSeek === true }});
        }}
      }}
      function syncRouteFromPlayback() {{
        if (!walkthroughVideo || !routeTimeline.length) return;
        if (Date.now() < manualRouteHoldUntil) return;
        const currentTime = Number(walkthroughVideo.currentTime || 0);
        let selected = routeTimeline[0];
        routeTimeline.forEach((item) => {{
          if (currentTime + 0.05 >= item.startSeconds) {{
            selected = item;
          }}
        }});
        syncRouteSelection(selected);
      }}
      function seekWalkthrough(rawStartSeconds, options = {{}}) {{
        if (!walkthroughVideo) return;
        const startSeconds = Number(rawStartSeconds);
        if (!Number.isFinite(startSeconds)) return;
        const duration = Number.isFinite(walkthroughVideo.duration) ? Number(walkthroughVideo.duration) : 0;
        const clamped = duration > 0 ? Math.max(0, Math.min(startSeconds, Math.max(0, duration - 0.15))) : Math.max(0, startSeconds);
        try {{
          walkthroughVideo.currentTime = clamped;
          if (options.play === true) {{
            walkthroughVideo.play().catch(() => null);
          }}
        }} catch (_error) {{
          return;
        }}
      }}
      mediaCards.forEach((card) => {{
        card.addEventListener('click', () => {{
          walkthroughVideo?.pause?.();
          renderReferenceFocus(card);
        }});
      }});
      routeActions.forEach((action) => {{
        action.addEventListener('click', () => {{
          const routeItem = routeMetadataByIndex.get(Number(action.getAttribute('data-route-index')));
          if (!routeItem) return;
          syncRouteSelection(routeItem, {{ seek: true, forceFocus: true, manual: true }});
        }});
      }});
      if (routePrev) {{
        routePrev.addEventListener('click', () => selectAdjacentRoute(-1));
      }}
      if (routeNext) {{
        routeNext.addEventListener('click', () => selectAdjacentRoute(1));
      }}
      if (layoutViewerFrame) {{
        layoutViewerFrame.addEventListener('load', () => {{
          layoutViewerShell?.classList.remove('is-ready');
          layoutViewerReadyAttempts = 0;
	          layoutViewerRouteSyncAttempts = 0;
	          scheduleLayoutViewerRouteSync();
	          revealLayoutViewerWhenReady();
	        }});
	      }}
	      window.__pqLayoutViewerShellDebug = {{
	        getState: () => ({{
	          ready: Boolean(layoutViewerShell?.classList.contains('is-ready')),
	          pendingRouteIndex: Number.isFinite(pendingLayoutViewerRouteIndex) ? Number(pendingLayoutViewerRouteIndex) : -1,
	          syncedRouteIndex: Number.isFinite(layoutViewerSyncedRouteIndex) ? Number(layoutViewerSyncedRouteIndex) : -1,
	          routeActionCount: Number(routeActions.length || 0),
	          mediaCardCount: Number(mediaCards.length || 0),
	          layoutViewerRouteButtonCount: Number(layoutViewerRouteButtonCount || 0),
	          layoutViewerFloorplanStopCount: Number(layoutViewerFloorplanStopCount || 0),
	          layoutViewerState: layoutViewerLastState && typeof layoutViewerLastState === 'object' ? {{ ...layoutViewerLastState }} : null,
	        }}),
	      }};
	      if (mediaCards.length) {{
        renderReferenceFocus(mediaCards[0]);
      }} else if (referenceShell && referenceFocusOpen) {{
        const note = document.createElement('p');
        note.className = 'disclosure';
        note.textContent = 'Reference media unavailable.';
        referenceShell.append(note);
        referenceFocusOpen.hidden = true;
      }}
      if (routeActions.length) {{
        const firstRoute = routeMetadataByIndex.get(0) || routeMetadata[0];
        if (firstRoute) {{
          renderProgressMarkers();
          syncRouteSelection(firstRoute, {{ forceFocus: true }});
        }} else {{
          setActiveRoute(routeActions[0]);
        }}
      }}
      if (walkthroughVideo) {{
        walkthroughVideo.addEventListener('loadedmetadata', () => {{
          renderProgressMarkers();
          syncRouteFromPlayback();
          updateWalkthroughProgress(routeMetadata.find((item) => item.action.classList.contains('is-active')) || routeTimeline[0] || null);
        }});
        walkthroughVideo.addEventListener('seeking', () => {{
          syncRouteFromPlayback();
          updateWalkthroughProgress(routeMetadata.find((item) => item.action.classList.contains('is-active')) || routeTimeline[0] || null);
        }});
        walkthroughVideo.addEventListener('timeupdate', () => {{
          syncRouteFromPlayback();
          updateWalkthroughProgress(routeMetadata.find((item) => item.action.classList.contains('is-active')) || routeTimeline[0] || null);
        }});
        const params = new URLSearchParams(window.location.search);
        if (params.get('autoplay') === '1' || params.get('pane') === 'flythrough-pane') {{
          walkthroughVideo.defaultMuted = true;
          walkthroughVideo.muted = true;
          walkthroughVideo.autoplay = true;
          walkthroughVideo.setAttribute('muted', '');
          walkthroughVideo.setAttribute('autoplay', '');
          walkthroughVideo.play().catch(() => null);
        }}
      }} else {{
        renderProgressMarkers();
        updateWalkthroughProgress(routeMetadata.find((item) => item.action.classList.contains('is-active')) || routeTimeline[0] || null);
      }}
    </script>
  </body>
</html>"""


def _tour_control_media_context(payload: dict[str, object]) -> tuple[list[dict[str, str]], str, str]:
    slug = str(payload.get("slug") or "").strip()
    scene_data: list[dict[str, str]] = []
    for index, scene in enumerate(payload.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        asset_relpath = _public_tour_safe_asset_relpath(str(scene.get("asset_relpath") or "").strip())
        image_url = f"/tours/files/{urllib.parse.quote(slug, safe='')}/{urllib.parse.quote(asset_relpath, safe='/')}" if slug and asset_relpath else ""
        if not image_url:
            external_image_url = _public_tour_safe_http_url(scene.get("image_url"))
            if external_image_url and _public_tour_static_media_url_allowed(external_image_url):
                image_url = external_image_url
        if not image_url:
            continue
        name = str(scene.get("name") or f"Scene {index + 1}").strip() or f"Scene {index + 1}"
        scene_data.append(
            {
                "name": name,
                "role": str(scene.get("role") or "photo").strip() or "photo",
                "image_url": image_url,
                "mime_type": str(scene.get("mime_type") or "").strip(),
            }
        )

    video_url, video_mime_type = _public_tour_walkthrough_media_context(payload)
    return scene_data, video_url, video_mime_type


def _tour_control_video_provider(payload: dict[str, object]) -> str:
    return str(
        payload.get("video_provider")
        or payload.get("video_provider_key")
        or payload.get("video_render_provider")
        or ""
    ).strip().lower()


def _public_tour_layer_disclosure(value: object, *, fallback: str = "Styled view.") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if re.search(r"\b(matterport|3d\s*vista|3dvista|pano2vr|krpano|magicfit|1min)\b", text, flags=re.IGNORECASE):
        return fallback
    return text


def _public_tour_safe_frame_url(value: object) -> str:
    normalized = str(value or "").strip()
    if normalized == "about:blank":
        return normalized
    local_url = _public_tour_safe_navigation_url(normalized)
    if local_url.startswith("/tours/"):
        parsed = urllib.parse.urlsplit(local_url)
        if re.match(r"^/tours/(?:3dvista|pano2vr|files)/", parsed.path):
            return local_url
        return ""
    return (
        _safe_3dvista_external_url(normalized)
        or _safe_matterport_external_url(normalized)
        or _safe_live_360_url(normalized)
    )


def _tour_control_provider_layers(
    *,
    payload: dict[str, object],
    default_src: str,
    default_label: str,
) -> list[dict[str, str]]:
    slug = str(payload.get("slug") or "").strip()
    safe_slug = urllib.parse.quote(slug, safe="")
    safe_default_src = _public_tour_safe_frame_url(default_src)
    if not safe_default_src or safe_default_src == "about:blank":
        return []
    layers: list[dict[str, str]] = [
        {
            "id": "as_listed",
            "label": "As listed",
            "src": safe_default_src,
            "provider": "3D tour",
            "disclosure": "Current view.",
        }
    ]
    if _safe_matterport_external_url(safe_default_src):
        return []
    seen = {layers[0]["src"]}
    raw_layers = payload.get("tour_layers") or payload.get("provider_layers") or payload.get("interactive_layers")
    if not isinstance(raw_layers, list):
        return layers
    for index, row in enumerate(raw_layers, start=1):
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or row.get("viewer_provider") or "").strip().lower()
        layer_id = re.sub(r"[^a-z0-9_-]+", "-", str(row.get("id") or row.get("mode") or f"layer-{index}").strip().lower()).strip("-")
        label = str(row.get("label") or row.get("title") or layer_id.replace("-", " ").title()).strip()
        disclosure = str(row.get("disclosure") or "").strip()
        src = ""
        if provider in {"3dvista", "3d_vista", "three_d_vista"}:
            provider_browser_ready = _3dvista_browser_render_proof_ready(row) or _3dvista_browser_render_proof_ready(payload)
            if not provider_browser_ready:
                continue
            for key in ("three_d_vista_url", "threedvista_url", "3dvista_url", "url", "iframe_src"):
                src = _safe_3dvista_external_url(row.get(key))
                if src:
                    break
            if not src and bool(row.get("same_tour_layer")):
                query = str(row.get("query") or row.get("layer_query") or "").strip().lstrip("?")
                fragment = str(row.get("fragment") or row.get("hash") or row.get("layer_hash") or "").strip().lstrip("#")
                if safe_default_src and (query or fragment):
                    safe_query = urllib.parse.urlencode(urllib.parse.parse_qsl(query, keep_blank_values=False), doseq=True)
                    safe_fragment = urllib.parse.quote(fragment, safe="/=&:;,+_-")
                    parsed_default = urllib.parse.urlparse(safe_default_src)
                    src = urllib.parse.urlunparse(
                        (
                            parsed_default.scheme,
                            parsed_default.netloc,
                            parsed_default.path,
                            parsed_default.params,
                            safe_query,
                            safe_fragment,
                        )
                    )
            if not src and slug:
                entry_relpath = _public_tour_safe_asset_relpath(
                    str(
                        row.get("three_d_vista_entry_relpath")
                        or row.get("threedvista_entry_relpath")
                        or row.get("3dvista_entry_relpath")
                        or row.get("entry_relpath")
                        or ""
                    ).strip()
                )
                if entry_relpath and _3dvista_entry_ready(slug, payload, entry_relpath):
                    src = f"/tours/3dvista/{safe_slug}/{urllib.parse.quote(entry_relpath, safe='/')}"
            disclosure = _public_tour_layer_disclosure(disclosure)
        else:
            continue
        src = _public_tour_safe_frame_url(src)
        if not src or src in seen:
            continue
        seen.add(src)
        layers.append(
            {
                "id": layer_id or f"layer-{index}",
                "label": label or f"Layer {index}",
                "src": src,
                "provider": "3D tour",
                "disclosure": disclosure,
            }
        )
    return layers


def _tour_control_provider_recovery_html(*, direct_href: str) -> str:
    safe_direct_href = _public_tour_safe_frame_url(direct_href) or "#"
    return f"""<div class="provider-load-state" data-provider-status role="status" aria-live="polite" aria-atomic="true">
              <div class="provider-loading" data-provider-loading>Loading 3D tour...</div>
              <div class="provider-recovery" data-provider-recovery hidden>
                <strong>3D tour unavailable</strong>
                <span>Try again or open the provider directly.</span>
                <div class="provider-recovery-actions">
                  <button type="button" data-provider-retry>Retry</button>
                  <a href="{html.escape(safe_direct_href)}" data-provider-direct target="_blank" rel="noopener noreferrer">Open directly</a>
                </div>
              </div>
            </div>"""


def _tour_control_provider_recovery_script() -> str:
    return """
      const providerFrameWrap = document.querySelector(".provider-frame-wrap");
      const providerStatus = document.querySelector("[data-provider-status]");
      const providerLoading = document.querySelector("[data-provider-loading]");
      const providerRecovery = document.querySelector("[data-provider-recovery]");
      const providerRetry = document.querySelector("[data-provider-retry]");
      const providerDirect = document.querySelector("[data-provider-direct]");
      let providerLoadTimer = 0;
      function setProviderFrameStatus(state) {
        if (providerFrameWrap) {
          providerFrameWrap.dataset.providerState = state;
          providerFrameWrap.setAttribute("aria-busy", String(state === "loading"));
        }
        if (providerStatus) providerStatus.hidden = state === "ready";
        if (providerLoading) providerLoading.hidden = state !== "loading";
        if (providerRecovery) providerRecovery.hidden = state !== "error";
      }
      function armProviderLoadWatchdog() {
        window.clearTimeout(providerLoadTimer);
        setProviderFrameStatus("loading");
        providerLoadTimer = window.setTimeout(() => setProviderFrameStatus("error"), 12000);
      }
      function setProviderFrameSource(targetSrc, forceReload = false) {
        if (!providerFrame) return;
        const nextSrc = String(targetSrc || "about:blank");
        providerFrame.dataset.src = nextSrc;
        if (providerDirect) providerDirect.setAttribute("href", nextSrc);
        const loadTarget = () => {
          armProviderLoadWatchdog();
          providerFrame.setAttribute("src", nextSrc);
        };
        if (forceReload || providerFrame.getAttribute("src") === nextSrc) {
          providerFrame.setAttribute("src", "about:blank");
          window.requestAnimationFrame(loadTarget);
          return;
        }
        loadTarget();
      }
      if (providerFrame) {
        providerFrame.addEventListener("load", () => {
          if ((providerFrame.getAttribute("src") || "") === "about:blank") return;
          window.clearTimeout(providerLoadTimer);
          setProviderFrameStatus("ready");
        });
        providerFrame.addEventListener("error", () => {
          window.clearTimeout(providerLoadTimer);
          setProviderFrameStatus("error");
        });
        setProviderFrameSource(providerFrame.dataset.src || "about:blank", true);
      }
      if (providerRetry) {
        providerRetry.addEventListener("click", () => {
          setProviderFrameSource(providerFrame?.dataset.src || providerLayers[0]?.src || "about:blank", true);
        });
      }
      window.addEventListener("offline", () => {
        window.clearTimeout(providerLoadTimer);
        setProviderFrameStatus("error");
      });
    """


def _tour_control_external_iframe_html(
    *,
    title: str,
    iframe_src: str,
    badge: str,
    payload: dict[str, object] | None = None,
    fullscreen_href: str = "",
    fullscreen: bool = False,
    nonce: str = "",
) -> str:
    nonce_attr = html.escape(_public_tour_normalized_nonce(nonce) or _public_tour_csp_nonce(), quote=True)
    payload = payload or {}
    scene_data, video_url, video_mime_type = _tour_control_media_context(payload)
    video_source_markup = _public_tour_walkthrough_source_markup(
        payload,
        video_url=video_url,
        video_mime_type=video_mime_type,
    )
    embed_walkthrough = bool(payload.get("_tour_control_embed_walkthrough"))
    provider_layers = _tour_control_provider_layers(payload=payload, default_src=iframe_src, default_label=badge)
    provider_layers_json = _public_tour_script_json(provider_layers)
    has_provider_layers = len(provider_layers) > 1
    provider_badge = html.escape(str(badge or "3D Tour").strip() or "3D Tour")
    provider_layer_buttons = "".join(
        f'<button type="button" data-provider-layer="{html.escape(row["id"])}" '
        f'data-provider-src="{html.escape(row["src"])}" '
        f'aria-pressed="{"true" if index == 0 else "false"}">{html.escape(row["label"])}</button>'
        for index, row in enumerate(provider_layers)
    )
    provider_layer_switch_html = (
        f'<div class="provider-layer-switch" aria-label="3D tour layer">{provider_layer_buttons}</div>'
        if has_provider_layers
        else ""
    )
    initial_provider_layer = provider_layers[0] if provider_layers else {}
    initial_provider_src_raw = str(initial_provider_layer.get("src") or "about:blank").strip() or "about:blank"
    initial_provider_disclosure = str(initial_provider_layer.get("disclosure") or "3D tour unavailable.").strip()
    raw_fullscreen_href = str((fullscreen_href or initial_provider_src_raw) if provider_layers else "#").strip() or "#"
    safe_fullscreen_href = (
        _public_tour_safe_navigation_url(raw_fullscreen_href, allow_fragment=True)
        or _public_tour_safe_frame_url(raw_fullscreen_href)
        or "#"
    )
    clean_fullscreen_href = html.escape(safe_fullscreen_href)
    payload_slug = str(payload.get("slug") or "").strip()
    return_href = f"/tours/{urllib.parse.quote(payload_slug, safe='')}" if payload_slug else "#"
    clean_return_href = html.escape(_public_tour_safe_navigation_url(return_href, allow_fragment=True) or "#")
    provider_recovery_script = _tour_control_provider_recovery_script()
    if (scene_data or video_url) and not fullscreen:
        data_json = _public_tour_script_json(scene_data)
        first_scene = scene_data[0] if scene_data else {"name": title, "image_url": "", "role": "photo", "mime_type": ""}
        initial_provider_src = html.escape(initial_provider_src_raw)
        provider_recovery_html = _tour_control_provider_recovery_html(direct_href=initial_provider_src_raw)
        _ = video_mime_type
        walkthrough_html = (
            (
                f"""<div class="media-actions">
              <a href="{html.escape(video_url)}" target="_blank" rel="noopener noreferrer">Open walkthrough</a>
            </div>
            <div class="video-stage">
              <video id="tour-video" controls playsinline webkit-playsinline="true" preload="metadata" poster="{html.escape(first_scene.get("image_url", ""))}">
                {video_source_markup}
              </video>
            </div>"""
                if embed_walkthrough
                else f"""<div class="media-actions">
              <a href="{html.escape(video_url)}" target="_blank" rel="noopener noreferrer">Open walkthrough</a>
            </div>"""
            )
            if video_url
            else ""
        )
        scene_viewer_html = (
            f"""<div class="tour-toolbar">
            <div class="toggle" id="role-filter">
              <button type="button" class="active" data-role="all">All</button>
              <button type="button" data-role="photo">Photos</button>
              <button type="button" data-role="floorplan">Floor plans</button>
            </div>
          </div>
          <div id="viewer" class="viewer">
            <img id="stage-image" src="{html.escape(first_scene.get("image_url", ""))}" alt="{html.escape(first_scene.get("name", title))}" referrerpolicy="no-referrer">
            <iframe src="" id="stage-frame" title="{html.escape(first_scene.get("name", title))}" referrerpolicy="no-referrer" hidden></iframe>
            <div class="caption">
              <small id="stage-role">{html.escape(first_scene.get("role", "photo"))}</small>
              <div id="stage-name">{html.escape(first_scene.get("name", title))}</div>
            </div>
          </div>
          <div id="thumbs" class="thumbs"></div>"""
            if scene_data
            else """<p class="empty">Photos and floorplans are not attached yet.</p>"""
        )
        return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} - {provider_badge}</title>
    <style nonce="{nonce_attr}">
      :root {{ color-scheme: dark; --bg: #111412; --panel: #1a1e1b; --line: #46514b; --text: #f6f8f4; --muted: #b7c2bc; --accent: #7bd8c3; --warm: #f2b66d; --focus: #9de7d5; }}
      html, body {{ margin: 0; min-height: 100%; background: var(--bg); color: var(--text); font-family: Inter, system-ui, sans-serif; }}
      body {{ overflow-x: hidden; }}
      .skip-link {{ position: fixed; left: 12px; top: 8px; z-index: 20; min-height: 44px; display: inline-flex; align-items: center; padding: 0 12px; border-radius: 6px; background: var(--text); color: #111; font-weight: 800; text-decoration: none; transform: translateY(-160%); }}
      .skip-link:focus {{ transform: translateY(0); }}
      :focus-visible {{ outline: 3px solid var(--focus); outline-offset: 3px; }}
      .shell {{ width: min(1520px, 100%); margin: 0 auto; padding: 14px; box-sizing: border-box; display: grid; gap: 14px; }}
      .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: #171b18; }}
      .badge {{ width: fit-content; padding: 7px 10px; border-radius: 999px; background: rgba(123,216,195,.12); border: 1px solid rgba(123,216,195,.42); color: var(--accent); font-size: 11px; font-weight: 800; letter-spacing: 0; text-transform: uppercase; }}
      .summary {{ min-width: 0; }}
      .summary p {{ margin: 0 0 3px; font-size: 11px; font-weight: 800; letter-spacing: 0; text-transform: uppercase; color: var(--muted); }}
      .summary h1 {{ margin: 0; max-width: 72ch; overflow-wrap: anywhere; font-size: 1.35rem; line-height: 1.12; letter-spacing: 0; }}
      .grid {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(340px, 460px); gap: 14px; align-items: start; }}
      .panel {{ border: 1px solid var(--line); border-radius: 8px; background: var(--panel); overflow: hidden; }}
      .provider-panel {{ min-height: min(74vh, 820px); display: grid; grid-template-rows: auto 1fr; }}
      .provider-launch {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px; border-bottom: 1px solid var(--line); }}
      .provider-launch strong {{ display: block; margin-bottom: 3px; }}
      .provider-actions {{ display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }}
      .provider-actions a, .provider-actions button {{ min-height: 44px; display: inline-flex; align-items: center; justify-content: center; border-radius: 999px; padding: 0 13px; border: 1px solid var(--line); background: transparent; color: var(--text); font: inherit; font-weight: 800; text-decoration: none; cursor: pointer; }}
      .provider-actions button {{ appearance: none; }}
      .provider-layer-switch {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }}
      .provider-layer-switch button {{ min-height: 44px; border: 1px solid var(--line); border-radius: 999px; padding: 0 13px; background: #232925; color: var(--text); font: inherit; font-weight: 800; cursor: pointer; }}
      .provider-layer-switch button[aria-pressed="true"] {{ background: var(--text); color: #111; }}
      .provider-layer-note {{ margin-top: 8px; color: var(--muted); font-size: .86rem; line-height: 1.35; }}
      .provider-frame-wrap {{ position: relative; min-height: 520px; background: #111; }}
      .provider-frame {{ display: block; width: 100%; height: 100%; min-height: 520px; border: 0; background: #111; }}
      .provider-load-state {{ position: absolute; inset: 0; z-index: 3; display: grid; place-items: center; padding: 20px; box-sizing: border-box; background: rgba(17,20,18,.92); text-align: center; }}
      .provider-load-state[hidden], .provider-loading[hidden], .provider-recovery[hidden] {{ display: none; }}
      .provider-loading {{ color: var(--muted); font-weight: 800; }}
      .provider-recovery {{ max-width: 420px; display: grid; gap: 8px; }}
      .provider-recovery strong {{ font-size: 1.05rem; }}
      .provider-recovery span {{ color: var(--muted); line-height: 1.45; }}
      .provider-recovery-actions {{ display: flex; justify-content: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }}
      .provider-recovery-actions button, .provider-recovery-actions a {{ min-width: 120px; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--line); border-radius: 6px; padding: 0 12px; background: #242b27; color: var(--text); font: inherit; font-weight: 800; text-decoration: none; cursor: pointer; }}
      .evidence {{ padding: 14px; display: grid; gap: 12px; }}
      .evidence h2 {{ margin: 0; font-size: 1rem; letter-spacing: 0; }}
      .hint, .empty {{ margin: 0; color: var(--muted); line-height: 1.45; font-size: .92rem; }}
      .card-label {{ margin-bottom: 8px; color: var(--warm); font-size: 11px; font-weight: 800; letter-spacing: 0; text-transform: uppercase; }}
      .media-actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
      .media-actions a {{ min-height: 44px; display: inline-flex; align-items: center; justify-content: center; border-radius: 6px; padding: 0 14px; border: 1px solid var(--line); background: #232925; color: var(--text); font-weight: 800; text-decoration: none; }}
      .video-stage {{ overflow: hidden; border-radius: 8px; border: 1px solid var(--line); background: rgba(0,0,0,.42); }}
      .video-stage video {{ display: block; width: 100%; min-height: 240px; max-height: 42vh; background: #080808; }}
      .tour-toolbar {{ display: flex; gap: 8px; flex-wrap: wrap; }}
      .toggle {{ display: inline-flex; gap: 6px; padding: 4px; border-radius: 8px; background: #232925; border: 1px solid var(--line); }}
      .toggle button {{ min-height: 44px; border: 0; border-radius: 6px; padding: 0 13px; background: transparent; color: var(--muted); font: inherit; font-weight: 750; cursor: pointer; }}
      .toggle button.active {{ background: var(--text); color: #111; }}
      .viewer {{ position: relative; overflow: hidden; border-radius: 8px; border: 1px solid var(--line); background: rgba(0,0,0,.26); }}
      #stage-image, #stage-frame {{ display: block; width: 100%; min-height: 310px; max-height: 45vh; object-fit: contain; border: 0; background: #0b0b0b; }}
      #stage-frame {{ height: 45vh; }}
      #stage-image[hidden], #stage-frame[hidden] {{ display: none; }}
      .caption {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 12px; border-top: 1px solid var(--line); }}
      .caption small {{ color: #f8df9b; font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
      .thumbs {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }}
      .thumb {{ position: relative; min-height: 84px; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; padding: 0; background: rgba(255,255,255,.06); cursor: pointer; }}
      .thumb.active {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
      .thumb.hidden {{ display: none; }}
      .thumb img {{ display: block; width: 100%; height: 100%; min-height: 84px; object-fit: cover; }}
      .thumb-doc {{ min-height: 84px; display: grid; place-items: center; color: var(--muted); font-weight: 800; }}
      .thumb .mini-badge {{ position: absolute; left: 7px; top: 7px; padding: 3px 7px; border-radius: 999px; background: rgba(0,0,0,.62); color: #fff; font-size: 10px; font-weight: 800; text-transform: uppercase; }}
      @media (max-width: 940px) {{
        .shell {{ padding: 10px; }}
        .topbar {{ align-items: flex-start; flex-direction: column; border-radius: 8px; }}
        .grid {{ grid-template-columns: 1fr; }}
        .provider-panel {{ min-height: 58vh; }}
        .provider-launch {{ align-items: stretch; flex-direction: column; }}
        .provider-actions {{ justify-content: stretch; }}
        .provider-actions button, .provider-actions a {{ width: 100%; }}
        .provider-frame {{ height: 58vh; min-height: 380px; }}
        .evidence {{ padding: 12px; }}
        .video-stage video {{ min-height: 220px; max-height: 36vh; }}
        .toggle {{ width: 100%; display: grid; grid-template-columns: repeat(3, 1fr); border-radius: 8px; }}
        .toggle button {{ min-height: 48px; padding: 0 8px; border-radius: 6px; }}
        #stage-image, #stage-frame {{ min-height: 280px; max-height: 52vh; }}
        .thumbs {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      }}
      @media (prefers-reduced-motion: reduce) {{
        *, *::before, *::after {{ animation-duration: .001ms !important; animation-iteration-count: 1 !important; scroll-behavior: auto !important; transition-duration: .001ms !important; }}
      }}
    </style>
  </head>
  <body>
    <a class="skip-link" href="#provider-frame">Skip to 3D tour</a>
    <div class="shell">
      <header class="topbar">
        <div class="summary" aria-label="Property tour summary">
          <p>3D Tour</p>
          <h1>{title}</h1>
        </div>
        <div class="badge">{provider_badge}</div>
      </header>
      <main class="grid" id="tour-content">
        <section class="panel provider-panel" aria-label="{provider_badge}">
          <div class="provider-launch">
            <div>
              <strong>{provider_badge}</strong>
              <p class="hint">Explore the space.</p>
              {provider_layer_switch_html}
              <p class="provider-layer-note" id="provider-layer-note">{html.escape(initial_provider_disclosure)}</p>
            </div>
            <div class="provider-actions">
              <a href="{clean_fullscreen_href}">Full screen</a>
            </div>
          </div>
          <div class="provider-frame-wrap" aria-busy="true" data-provider-state="loading">
            <iframe src="about:blank" data-src="{initial_provider_src}" class="provider-frame" id="provider-frame" title="{title}" aria-label="{provider_badge}: {title}" aria-describedby="provider-layer-note" allowfullscreen loading="eager" referrerpolicy="no-referrer"></iframe>
            {provider_recovery_html}
          </div>
        </section>
        <aside class="panel evidence" aria-label="Inside the space">
          <div>
            <h2>Inside the space</h2>
            <p class="hint">Photos and floorplan.</p>
          </div>
          {walkthrough_html}
          {scene_viewer_html}
        </aside>
      </main>
    </div>
    <script nonce="{nonce_attr}" id="provider-layers" type="application/json">{provider_layers_json}</script>
    <script nonce="{nonce_attr}" id="scene-data" type="application/json">{data_json}</script>
    <script nonce="{nonce_attr}">
      const providerLayers = JSON.parse(document.getElementById("provider-layers").textContent || "[]");
      const scenes = JSON.parse(document.getElementById("scene-data").textContent || "[]");
      const stageImage = document.getElementById("stage-image");
      const stageFrame = document.getElementById("stage-frame");
      const stageName = document.getElementById("stage-name");
      const stageRole = document.getElementById("stage-role");
      const thumbs = document.getElementById("thumbs");
      const tourVideo = document.getElementById("tour-video");
      const providerFrame = document.querySelector(".provider-frame");
      const providerLayerNote = document.getElementById("provider-layer-note");
      let selectedProviderLayer = providerLayers[0] || {{}};
      let activeIndex = 0;
      let activeRoleFilter = "all";
      {provider_recovery_script}
      document.querySelectorAll("[data-provider-layer]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const layer = providerLayers.find((candidate) => candidate.id === button.dataset.providerLayer);
          if (!layer) return;
          selectedProviderLayer = layer;
          document.querySelectorAll("[data-provider-layer]").forEach((candidate) => candidate.setAttribute("aria-pressed", String(candidate === button)));
          if (providerLayerNote) providerLayerNote.textContent = layer.disclosure || "";
          setProviderFrameSource(layer.src || "about:blank", true);
        }});
      }});
      function visibleSceneIndexes() {{
        return scenes
          .map((scene, index) => (activeRoleFilter === "all" || scene.role === activeRoleFilter ? index : -1))
          .filter((index) => index >= 0);
      }}
      function renderThumbs() {{
        if (!thumbs) return;
        thumbs.replaceChildren();
        scenes.forEach((scene, index) => {{
          const button = document.createElement("button");
          button.type = "button";
          button.className = "thumb" + (index === activeIndex ? " active" : "");
          if (activeRoleFilter !== "all" && scene.role !== activeRoleFilter) button.classList.add("hidden");
          const isPdf = String(scene.mime_type || "").includes("pdf") || /\\.pdf(?:$|[?#])/i.test(String(scene.image_url || ""));
          const badge = document.createElement("span");
          badge.className = "mini-badge";
          badge.textContent = String(scene.role || (isPdf ? "doc" : "photo"));
          button.appendChild(badge);
          if (isPdf) {{
            const documentLabel = document.createElement("span");
            documentLabel.className = "thumb-doc";
            documentLabel.textContent = "PDF";
            button.appendChild(documentLabel);
          }} else {{
            const image = document.createElement("img");
            image.src = String(scene.image_url || "");
            image.alt = String(scene.name || "Scene");
            image.referrerPolicy = "no-referrer";
            button.appendChild(image);
          }}
          button.addEventListener("click", () => setActive(index));
          thumbs.appendChild(button);
        }});
      }}
      function setActive(index) {{
        if (!scenes.length || !stageImage || !stageFrame) return;
        activeIndex = (index + scenes.length) % scenes.length;
        const scene = scenes[activeIndex] || {{}};
        const isPdf = String(scene.mime_type || "").includes("pdf") || /\\.pdf(?:$|[?#])/i.test(String(scene.image_url || ""));
        if (isPdf) {{
          stageFrame.src = scene.image_url || "";
          stageFrame.title = scene.name || "Floorplan";
          stageFrame.hidden = false;
          stageImage.hidden = true;
        }} else {{
          stageImage.src = scene.image_url || "";
          stageImage.alt = scene.name || "Scene";
          stageImage.hidden = false;
          stageFrame.hidden = true;
        }}
        if (stageName) stageName.textContent = scene.name || "Scene";
        if (stageRole) stageRole.textContent = scene.role || "photo";
        renderThumbs();
      }}
      document.querySelectorAll("#role-filter button").forEach((button) => {{
        button.addEventListener("click", () => {{
          activeRoleFilter = button.dataset.role || "all";
          document.querySelectorAll("#role-filter button").forEach((candidate) => candidate.classList.toggle("active", candidate === button));
          const visible = visibleSceneIndexes();
          setActive(visible.length ? visible[0] : activeIndex);
        }});
      }});
      const params = new URLSearchParams(window.location.search);
      if (params.get("pane") === "floorplan-pane") {{
        const floorplanIndex = scenes.findIndex((scene) => scene.role === "floorplan");
        setActive(floorplanIndex >= 0 ? floorplanIndex : 0);
      }} else {{
        setActive(0);
      }}
      async function primeTourVideoPlayback() {{
        if (!tourVideo || typeof tourVideo.play !== "function") return;
        tourVideo.defaultMuted = true;
        tourVideo.muted = true;
        tourVideo.autoplay = true;
        tourVideo.playsInline = true;
        tourVideo.setAttribute("muted", "");
        tourVideo.setAttribute("autoplay", "");
        tourVideo.setAttribute("playsinline", "");
        const attemptPlay = async () => {{
          try {{
            await tourVideo.play();
          }} catch (_error) {{
            tourVideo.controls = true;
          }}
        }};
        if (tourVideo.readyState >= 2) {{
          await attemptPlay();
          return;
        }}
        const once = () => {{
          tourVideo.removeEventListener("loadedmetadata", once);
          tourVideo.removeEventListener("canplay", once);
          void attemptPlay();
        }};
        tourVideo.addEventListener("loadedmetadata", once, {{ once: true }});
        tourVideo.addEventListener("canplay", once, {{ once: true }});
        try {{
          tourVideo.load();
        }} catch (_error) {{
          void attemptPlay();
        }}
      }}
      if (tourVideo && params.get("autoplay") === "1") {{
        void primeTourVideoPlayback();
      }}
    </script>
  </body>
</html>"""
    fullscreen_recovery_html = _tour_control_provider_recovery_html(direct_href=initial_provider_src_raw)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} - {provider_badge}</title>
    <style nonce="{nonce_attr}">
      :root {{ color-scheme: dark; --bg: #111412; --panel: #1a1e1b; --line: #46514b; --text: #f6f8f4; --muted: #b7c2bc; --accent: #7bd8c3; --focus: #9de7d5; }}
      html, body {{ margin: 0; width: 100%; height: 100%; overflow: hidden; background: var(--bg); color: var(--text); font-family: Inter, system-ui, sans-serif; }}
      :focus-visible {{ outline: 3px solid var(--focus); outline-offset: 3px; }}
      .provider-frame-wrap, iframe {{ position: fixed; inset: 0; width: 100vw; height: 100vh; border: 0; background: var(--bg); }}
      .shell {{ position: fixed; left: max(10px, env(safe-area-inset-left)); top: max(10px, env(safe-area-inset-top)); z-index: 2; display: flex; align-items: center; gap: 8px; max-width: min(680px, calc(100vw - 20px)); pointer-events: none; }}
      .badge {{ width: fit-content; min-height: 42px; box-sizing: border-box; display: inline-flex; align-items: center; padding: 0 11px; border-radius: 6px; background: rgba(26,30,27,.94); border: 1px solid var(--line); color: var(--accent); font-size: 11px; font-weight: 800; letter-spacing: 0; text-transform: uppercase; pointer-events: auto; }}
      .summary {{ min-width: 0; max-width: min(460px, calc(100vw - 190px)); padding: 10px 12px; border-radius: 6px; background: rgba(26,30,27,.94); border: 1px solid var(--line); pointer-events: auto; }}
      .summary p {{ display: none; }}
      .summary h1 {{ margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: .9rem; line-height: 1.25; letter-spacing: 0; }}
      .layer-switch {{ display: flex; gap: 8px; flex-wrap: wrap; }}
      .layer-switch button {{ min-height: 44px; border: 1px solid var(--line); border-radius: 999px; padding: 0 13px; background: rgba(26,30,27,.94); color: var(--text); font: inherit; font-weight: 800; cursor: pointer; }}
      .layer-switch button[aria-pressed="true"] {{ background: var(--text); color: #111; }}
      .layer-note {{ margin: 0; color: var(--muted); font-size: 12px; line-height: 1.35; }}
      .viewer-actions {{ display: flex; pointer-events: auto; }}
      .viewer-actions a {{ width: 44px; height: 44px; box-sizing: border-box; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--line); border-radius: 6px; background: rgba(26,30,27,.94); color: var(--text); font-size: 24px; font-weight: 800; line-height: 1; text-decoration: none; }}
      .provider-load-state {{ position: fixed; inset: 0; z-index: 1; display: grid; place-items: center; padding: 20px; box-sizing: border-box; background: rgba(17,20,18,.92); text-align: center; }}
      .provider-load-state[hidden], .provider-loading[hidden], .provider-recovery[hidden] {{ display: none; }}
      .provider-loading {{ color: var(--muted); font-weight: 800; }}
      .provider-recovery {{ max-width: 420px; display: grid; gap: 8px; }}
      .provider-recovery span {{ color: var(--muted); line-height: 1.45; }}
      .provider-recovery-actions {{ display: flex; justify-content: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }}
      .provider-recovery-actions button, .provider-recovery-actions a {{ min-width: 120px; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--line); border-radius: 6px; padding: 0 12px; background: #242b27; color: var(--text); font: inherit; font-weight: 800; text-decoration: none; cursor: pointer; }}
      @media (max-width: 720px) {{
        .shell {{ max-width: calc(100vw - 20px); }}
        .badge {{ display: none; }}
        .summary {{ display: none; }}
      }}
      @media (prefers-reduced-motion: reduce) {{
        *, *::before, *::after {{ animation-duration: .001ms !important; animation-iteration-count: 1 !important; scroll-behavior: auto !important; transition-duration: .001ms !important; }}
      }}
    </style>
  </head>
  <body>
    <div class="provider-frame-wrap" aria-busy="true" data-provider-state="loading">
      <iframe id="provider-frame" src="about:blank" data-src="{html.escape(initial_provider_src_raw)}" title="{title}" aria-label="{provider_badge}: {title}" allowfullscreen loading="eager" referrerpolicy="no-referrer"></iframe>
      {fullscreen_recovery_html}
    </div>
    <div class="shell">
      <div class="viewer-actions"><a href="{clean_return_href}" aria-label="Back to tour" title="Back to tour"><span aria-hidden="true">&#8592;</span></a></div>
      <div class="badge">{provider_badge}</div>
      {f'<div class="layer-switch" aria-label="3D tour layer">{provider_layer_buttons}</div><p class="layer-note" id="provider-layer-note">{html.escape(provider_layers[0]["disclosure"])}</p>' if has_provider_layers else ""}
      <section class="summary" aria-label="Tour summary">
        <p>3D Tour</p>
        <h1>{title}</h1>
      </section>
    </div>
    <script nonce="{nonce_attr}" id="provider-layers" type="application/json">{provider_layers_json}</script>
    <script nonce="{nonce_attr}">
      const providerLayers = JSON.parse(document.getElementById("provider-layers").textContent || "[]");
      const providerFrame = document.getElementById("provider-frame");
      const providerLayerNote = document.getElementById("provider-layer-note");
      {provider_recovery_script}
      document.querySelectorAll("[data-provider-layer]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const layer = providerLayers.find((candidate) => candidate.id === button.dataset.providerLayer);
          if (!layer || !providerFrame) return;
          setProviderFrameSource(layer.src || "about:blank", true);
          document.querySelectorAll("[data-provider-layer]").forEach((candidate) => candidate.setAttribute("aria-pressed", String(candidate === button)));
          if (providerLayerNote) providerLayerNote.textContent = layer.disclosure || "";
        }});
      }});
    </script>
  </body>
</html>"""


def _tour_control_3dvista_html(payload: dict[str, object], *, nonce: str = "") -> str:
    title = html.escape(str(payload.get("display_title") or payload.get("title") or "3D tour control").strip())
    raw_slug = str(payload.get("slug") or "").strip()
    slug = html.escape(raw_slug)
    if not _3dvista_private_viewer_proof_ready(payload, slug=raw_slug):
        raise HTTPException(status_code=404, detail="tour_control_3d_export_hidden")
    external_url = ""
    for key in ("three_d_vista_url", "threedvista_url", "3dvista_url", "source_virtual_tour_url", "crezlo_public_url"):
        external_url = _safe_3dvista_external_url(payload.get(key))
        if external_url:
            break
    entry_relpath = _3dvista_entry_relpath(payload)
    iframe_src = external_url
    if not iframe_src and entry_relpath and slug:
        if not _3dvista_entry_export_ready(raw_slug, payload, entry_relpath):
            raise HTTPException(status_code=404, detail="tour_control_3dvista_export_missing")
        iframe_src = f"/tours/3dvista/{slug}/{urllib.parse.quote(entry_relpath, safe='/')}"
    if iframe_src:
        return _tour_control_external_iframe_html(
            title=title,
            iframe_src=iframe_src,
            badge="3DVista Control",
            payload=payload,
            fullscreen_href=f"/tours/{urllib.parse.quote(raw_slug, safe='')}/control/3dvista?fullscreen=1" if raw_slug else iframe_src,
            fullscreen=bool(payload.get("_tour_control_fullscreen")),
            nonce=nonce,
        )
    raise HTTPException(status_code=404, detail="tour_control_3dvista_export_missing")


def _tour_control_pano2vr_html(payload: dict[str, object], *, nonce: str = "") -> str:
    title = html.escape(str(payload.get("display_title") or payload.get("title") or "3D tour control").strip())
    slug = str(payload.get("slug") or "").strip()
    entry_relpath = _pano2vr_entry_relpath(payload)
    if not slug or not entry_relpath:
        raise HTTPException(status_code=404, detail="tour_control_pano2vr_export_missing")
    if not _local_tour_html_asset_has_marker(slug, entry_relpath, markers=_PANO2VR_EXPORT_MARKERS):
        raise HTTPException(status_code=404, detail="tour_control_pano2vr_export_missing")
    iframe_src = f"/tours/pano2vr/{urllib.parse.quote(slug, safe='')}/{urllib.parse.quote(entry_relpath, safe='/')}"
    return _tour_control_external_iframe_html(
        title=title,
        iframe_src=iframe_src,
        badge="3D Tour",
        payload=payload,
        fullscreen_href=f"/tours/{urllib.parse.quote(slug, safe='')}/control/pano2vr?fullscreen=1" if slug else iframe_src,
        fullscreen=bool(payload.get("_tour_control_fullscreen")),
        nonce=nonce,
    )


def _tour_control_walkable_html(
    payload: dict[str, object],
    *,
    provider_label: str = "Interactive Viewing",
    license_config: dict[str, str] | None = None,
    viewer_name: str = "",
    nonce: str = "",
) -> str:
    nonce_attr = html.escape(_public_tour_normalized_nonce(nonce) or _public_tour_csp_nonce(), quote=True)
    title = html.escape(str(payload.get("display_title") or payload.get("title") or "3D walk control").strip())
    safe_provider_label = html.escape(str(provider_label or "Interactive Viewing").strip())
    walkable_scene = payload.get("walkable_scene") if isinstance(payload.get("walkable_scene"), dict) else {}
    if not walkable_scene:
        raise HTTPException(status_code=404, detail="tour_control_walkable_scene_missing")
    public_walkable_scene = dict(walkable_scene)
    if "walkthrough_scene_images" not in public_walkable_scene and isinstance(public_walkable_scene.get("magicfit_scene_images"), list):
        public_walkable_scene["walkthrough_scene_images"] = list(public_walkable_scene.get("magicfit_scene_images") or [])
    public_walkable_scene.pop("magicfit_scene_images", None)
    normalized_license = license_config or {}
    license_enabled = bool(str(normalized_license.get("domain") or "").strip() and str(normalized_license.get("key") or "").strip())
    if license_enabled and ((viewer_name or "").strip().lower() == "krpano" or "krpano" in safe_provider_label.lower()) and not _walkable_scene_has_real_360_asset(payload):
        raise HTTPException(status_code=404, detail="tour_control_krpano_asset_missing")
    data_json = _public_tour_script_json(public_walkable_scene)
    license_domain = html.escape(str(normalized_license.get("domain") or "").strip())
    license_json = _public_tour_script_json({"domain": str(normalized_license.get("domain") or "").strip()}) if license_enabled else ""
    license_badge = f'<div class="panel license"><strong>Viewer license</strong><span>Registered for {license_domain}</span></div>' if license_enabled else ""
    resolved_viewer_name = str(viewer_name or "").strip().lower() or (
        "krpano" if license_enabled and "krpano" in safe_provider_label.lower() else "walkable"
    )
    return f"""<!doctype html>
<html lang="de">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} - {safe_provider_label}</title>
    <style nonce="{nonce_attr}">
      html, body {{ margin: 0; width: 100%; height: 100%; overflow: hidden; background: #15130f; color: #f7f1e6; font-family: Inter, system-ui, sans-serif; }}
      #viewer {{ position: fixed; inset: 0; }}
      #walkthrough-still-view {{ position: fixed; inset: 0; width: 100vw; height: 100vh; object-fit: cover; display: none; z-index: 1; background: #15130f; }}
      .hud {{ position: fixed; left: 14px; top: 14px; z-index: 5; display: flex; gap: 8px; flex-wrap: wrap; max-width: min(620px, calc(100vw - 28px)); }}
      .panel {{ background: rgba(18,16,13,.72); border: 1px solid rgba(255,255,255,.18); border-radius: 10px; padding: 10px 12px; backdrop-filter: blur(12px); }}
      .panel strong {{ display: block; font-size: 13px; margin-bottom: 3px; }}
      .panel span {{ display: block; font-size: 12px; color: rgba(247,241,230,.78); }}
      .rooms {{ position: fixed; right: 14px; top: 14px; z-index: 5; display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; max-width: min(520px, calc(100vw - 28px)); }}
      .move-pad {{ position: fixed; left: 14px; bottom: 14px; z-index: 5; display: grid; grid-template-columns: repeat(3, 46px); gap: 7px; }}
      .turn-pad {{ position: fixed; right: 14px; bottom: 14px; z-index: 5; display: flex; gap: 8px; }}
      button {{ min-height: 38px; padding: 0 13px; border: 1px solid rgba(255,255,255,.24); border-radius: 9px; background: rgba(255,255,255,.14); color: #f7f1e6; cursor: pointer; font: inherit; }}
      button.active {{ background: #f7f1e6; color: #15130f; }}
      .move-pad button {{ width: 46px; padding: 0; font-weight: 700; }}
      .move-pad .blank {{ visibility: hidden; }}
      @media (max-width: 720px) {{
        .panel span {{ display: none; }}
        .rooms {{ top: 72px; left: 14px; right: 14px; justify-content: flex-start; }}
        .rooms button {{ min-height: 34px; padding: 0 9px; font-size: 12px; }}
      }}
    </style>
  </head>
  <body data-viewer="{resolved_viewer_name}">
    <div id="viewer"></div>
    <img id="walkthrough-still-view" alt="">
    <div class="hud"><div class="panel"><strong>{safe_provider_label}</strong><span>Walk with WASD or arrows. Drag to look around. Room buttons jump inside rooms.</span></div>{license_badge}</div>
    <div class="rooms" id="rooms"></div>
    <div class="move-pad">
      <button class="blank" tabindex="-1"></button><button data-hold="forward">W</button><button class="blank" tabindex="-1"></button>
      <button data-hold="left">A</button><button data-hold="back">S</button><button data-hold="right">D</button>
    </div>
    <div class="turn-pad"><button data-hold="turn-left">Turn left</button><button data-hold="turn-right">Turn right</button></div>
    <script nonce="{nonce_attr}" id="walkable-data" type="application/json">{data_json}</script>
    {f'<script id="krpano-license" type="application/json">{license_json}</script>' if license_enabled else ''}
    <script nonce="{nonce_attr}" type="importmap">{{"imports":{{"three":"https://cdn.jsdelivr.net/npm/three@0.165.0/build/three.module.js"}}}}</script>
    <script nonce="{nonce_attr}" type="module">
      import * as THREE from 'three';
      const spec = JSON.parse(document.getElementById('walkable-data').textContent || '{{}}');
      const krpanoLicense = document.getElementById('krpano-license');
      if (krpanoLicense) window.__PROPERTYQUARRY_KRPANO_LICENSE__ = JSON.parse(krpanoLicense.textContent || '{{}}');
      const rooms = Array.isArray(spec.rooms) ? spec.rooms : [];
      const stops = Array.isArray(spec.route) ? spec.route : [];
      const walkthroughStillImages = Array.isArray(spec.walkthrough_scene_images) ? spec.walkthrough_scene_images : [];
      const walkthroughStillView = document.getElementById('walkthrough-still-view');
      const useWalkthroughStillImages = walkthroughStillImages.length > 0;
      const scene = new THREE.Scene();
      scene.background = new THREE.Color(0xf2eee7);
      scene.fog = new THREE.Fog(0xf2eee7, 8, 22);
      const camera = new THREE.PerspectiveCamera(68, innerWidth / innerHeight, 0.05, 80);
      const renderer = new THREE.WebGLRenderer({{ antialias: true }});
      renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.5));
      renderer.setSize(innerWidth, innerHeight);
      renderer.shadowMap.enabled = true;
      document.getElementById('viewer').appendChild(renderer.domElement);
      if (useWalkthroughStillImages) {{
        document.getElementById('viewer').style.display = 'none';
        walkthroughStillView.style.display = 'block';
      }}
      const mats = {{
        floor: new THREE.MeshStandardMaterial({{ color: 0xb78959, roughness: .82 }}),
        balcony: new THREE.MeshStandardMaterial({{ color: 0x99978e, roughness: .9 }}),
        wall: new THREE.MeshStandardMaterial({{ color: 0xf1eee8, roughness: .76 }}),
        wood: new THREE.MeshStandardMaterial({{ color: 0x8d633d, roughness: .78 }}),
        fabric: new THREE.MeshStandardMaterial({{ color: 0x6d7f94, roughness: .9 }}),
        dark: new THREE.MeshStandardMaterial({{ color: 0x222326, roughness: .72 }}),
        white: new THREE.MeshStandardMaterial({{ color: 0xf8f5ef, roughness: .58 }}),
        green: new THREE.MeshStandardMaterial({{ color: 0x5e8055, roughness: .88 }}),
        red: new THREE.MeshStandardMaterial({{ color: 0xa45550, roughness: .84 }}),
        blue: new THREE.MeshStandardMaterial({{ color: 0x397eb9, roughness: .7 }}),
        metal: new THREE.MeshStandardMaterial({{ color: 0xb9b5ad, roughness: .35, metalness: .25 }}),
        screen: new THREE.MeshStandardMaterial({{ color: 0x101114, roughness: .25, emissive: 0x25385f, emissiveIntensity: .35 }}),
        skin: new THREE.MeshStandardMaterial({{ color: 0xc99d78, roughness: .65 }}),
        shirt: new THREE.MeshStandardMaterial({{ color: 0x7d5148, roughness: .82 }}),
        glass: new THREE.MeshStandardMaterial({{ color: 0xaed2e5, transparent: true, opacity: .34, roughness: .25 }}),
      }};
      scene.add(new THREE.HemisphereLight(0xffffff, 0x8a735a, 1.7));
      const sun = new THREE.DirectionalLight(0xfff3d6, 2.1); sun.position.set(2,8,-4); sun.castShadow = true; scene.add(sun);
      const lamp = new THREE.PointLight(0xffddb0, 1.25, 13); lamp.position.set(7,2.4,2.3); scene.add(lamp);
      function box(n,x,y,z,sx,sy,sz,m,cast=true) {{ const mesh = new THREE.Mesh(new THREE.BoxGeometry(sx,sy,sz), m); mesh.name=n; mesh.position.set(x,y,z); mesh.castShadow=cast; mesh.receiveShadow=true; scene.add(mesh); return mesh; }}
      function cyl(n,x,y,z,r,h,m) {{ const mesh = new THREE.Mesh(new THREE.CylinderGeometry(r,r,h,28),m); mesh.name=n; mesh.position.set(x,y,z); mesh.castShadow=true; mesh.receiveShadow=true; scene.add(mesh); return mesh; }}
      function sphere(n,x,y,z,r,m) {{ const mesh = new THREE.Mesh(new THREE.SphereGeometry(r,28,16),m); mesh.name=n; mesh.position.set(x,y,z); mesh.castShadow=true; scene.add(mesh); return mesh; }}
      function wall(x1,z1,x2,z2) {{ const dx=x2-x1,dz=z2-z1,len=Math.hypot(dx,dz); const w=box('wall',(x1+x2)/2,1.25,(z1+z2)/2,len,2.5,.08,mats.wall,false); w.rotation.y=-Math.atan2(dz,dx); }}
      function rug(x,z,sx,sz,color) {{ const mesh = new THREE.Mesh(new THREE.PlaneGeometry(sx,sz), new THREE.MeshStandardMaterial({{color, roughness:.95}})); mesh.rotation.x=-Math.PI/2; mesh.position.set(x,.012,z); mesh.receiveShadow=true; scene.add(mesh); }}
      for (const room of rooms) {{
        const cx=room.x+room.w/2, cz=room.z+room.d/2;
        box(room.name+'_floor',cx,-.025,cz,room.w,.05,room.d,room.kind==='balcony'?mats.balcony:mats.floor,false);
        box(room.name+'_ceiling',cx,2.52,cz,room.w,.04,room.d,mats.wall,false);
      }}
      wall(1.0,1.0,10.8,1.0); wall(10.8,1.0,10.8,9.2); wall(10.8,9.2,1.0,9.2); wall(1.0,9.2,1.0,1.0);
      wall(3.0,1.0,3.0,3.55); wall(3.0,4.75,3.0,5.3); wall(4.6,1.0,4.6,1.95); wall(4.6,3.05,4.6,5.2); wall(4.6,6.25,4.6,9.2);
      wall(4.6,3.8,6.45,3.8); wall(7.3,3.8,10.8,3.8); wall(7.7,3.8,7.7,5.25); wall(7.7,6.4,7.7,9.2); wall(7.7,7.0,9.35,7.0); wall(10.25,7.0,10.8,7.0);
      box('entry_runner',2.3,.02,7.1,1.45,.04,2.2,mats.red); box('shoe_cabinet',1.34,.45,7,.45,.9,1.6,mats.wood);
      for(let i=0;i<5;i++) box('coat',1.25,1.35-i*.02,6.1+i*.25,.08,.85,.26,i%2?mats.dark:mats.fabric);
      for(let i=0;i<4;i++) box('shoes',2+i*.28,.08,8.65,.22,.12,.38,i%2?mats.dark:mats.wood);
      box('vanity',1.35,.48,2,.48,.85,.9,mats.white); box('mirror',1.08,1.55,2,.04,.7,.95,mats.metal,false); cyl('toilet',2.25,.26,2.8,.32,.52,mats.white); box('shower_glass',2,1.05,4.45,1.25,2,.05,mats.glass,false);
      box('kitchen_wall',5.8,1,1.18,2.25,2,.42,mats.white); box('counter',5.9,.48,2.1,2.5,.9,.72,mats.white); box('island',6.45,.48,2.85,1.55,.9,.75,mats.wood);
      cyl('person_body',6.75,1.05,2.1,.18,.95,mats.shirt); sphere('person_head',6.75,1.65,2.1,.18,mats.skin); box('person_arm',6.52,1.25,2.22,.5,.08,.08,mats.skin);
      box('sofa',9.25,.38,2.62,1.85,.75,.82,mats.fabric); box('sofa_back',9.25,.82,3.02,1.9,.7,.18,mats.fabric); box('coffee_table',8.55,.22,2.2,.82,.28,.45,mats.wood); box('tv_screen',10.66,1.15,2.35,.08,.72,1.12,mats.screen,false); rug(8.85,2.55,2.55,1.3,0xddd1c2);
      for(let i=0;i<9;i++) box('toys',8.25+(i%3)*.22,.08,3.2+Math.floor(i/3)*.18,.13,.13,.13,i%3===0?mats.blue:(i%3===1?mats.red:mats.green));
      box('bed1',5.55,.35,6.6,1.35,.7,2.05,mats.green); box('bed1_pillow',5.55,.78,5.72,1.08,.18,.36,mats.white); box('desk1',6.95,.42,8.45,1.05,.85,.48,mats.wood); box('chair1',6.6,.35,8.02,.45,.7,.45,mats.fabric);
      box('bed2',8.95,.34,5,1.26,.68,1.72,mats.red); box('bed2_pillow',8.95,.77,4.35,.96,.16,.34,mats.white); box('toy_shelf',10.38,.72,5.85,.45,1.35,.95,mats.wood); for(let i=0;i<7;i++) sphere('child_toys',8.35+(i%4)*.25,.12,6.45+(i%2)*.23,.09,i%2?mats.blue:mats.green);
      box('balcony_rail',9.25,1,9.12,2.7,1.5,.08,mats.metal,false); box('balcony_chair',8.65,.35,8.15,.55,.7,.55,mats.wood); box('balcony_table',9.35,.35,8.25,.62,.7,.62,mats.wood); for(let i=0;i<5;i++) {{ cyl('plant_pot',10.15,.18,7.35+i*.35,.16,.35,mats.wood); sphere('plant',10.15,.55,7.35+i*.35,.22,mats.green); }}
      const roomButtons = document.getElementById('rooms');
      let yaw = -2.2, pitch = -0.02, pos = new THREE.Vector3(2.35,1.52,7.55);
      function jump(stop, index) {{
        pos.set(stop.at[0], 1.52, stop.at[1]);
        yaw = THREE.MathUtils.degToRad(stop.start_deg || 0);
        if (useWalkthroughStillImages && walkthroughStillImages[index]) walkthroughStillView.src = walkthroughStillImages[index].url || walkthroughStillImages[index].image_url || walkthroughStillImages[index];
        [...roomButtons.children].forEach((b,i)=>b.classList.toggle('active', i===index));
      }}
      stops.forEach((stop,index)=>{{ const b=document.createElement('button'); b.type='button'; b.textContent=stop.label || stop.room || `Room ${{index+1}}`; b.addEventListener('click',()=>jump(stop,index)); b.classList.toggle('active', index===0); roomButtons.appendChild(b); }});
      if (stops[0]) jump(stops[0],0);
      const keys = new Set(); const held = new Set();
      addEventListener('keydown', e => keys.add(e.key.toLowerCase()));
      addEventListener('keyup', e => keys.delete(e.key.toLowerCase()));
      document.querySelectorAll('[data-hold]').forEach(btn => {{ const v=btn.dataset.hold; ['pointerdown','touchstart'].forEach(ev=>btn.addEventListener(ev,e=>{{e.preventDefault(); held.add(v);}})); ['pointerup','pointerleave','touchend','touchcancel'].forEach(ev=>btn.addEventListener(ev,()=>held.delete(v))); }});
      let dragging=false,lastX=0,lastY=0; renderer.domElement.addEventListener('pointerdown', e=>{{ dragging=true; lastX=e.clientX; lastY=e.clientY; renderer.domElement.setPointerCapture(e.pointerId); }});
      renderer.domElement.addEventListener('pointermove', e=>{{ if(!dragging) return; yaw -= (e.clientX-lastX)*.004; pitch = Math.max(-.45, Math.min(.35, pitch - (e.clientY-lastY)*.003)); lastX=e.clientX; lastY=e.clientY; }});
      renderer.domElement.addEventListener('pointerup', ()=>dragging=false);
      let last = performance.now();
      function tick(now) {{
        const dt = Math.min(.05, (now-last)/1000); last=now;
        const forward = new THREE.Vector3(Math.sin(yaw),0,Math.cos(yaw));
        const right = new THREE.Vector3(Math.cos(yaw),0,-Math.sin(yaw));
        let move = new THREE.Vector3();
        if(keys.has('w')||keys.has('arrowup')||held.has('forward')) move.add(forward);
        if(keys.has('s')||keys.has('arrowdown')||held.has('back')) move.sub(forward);
        if(keys.has('a')||held.has('left')) move.sub(right);
        if(keys.has('d')||held.has('right')) move.add(right);
        if(keys.has('arrowleft')||held.has('turn-left')) yaw += dt*1.5;
        if(keys.has('arrowright')||held.has('turn-right')) yaw -= dt*1.5;
        if(move.lengthSq()>0) {{ move.normalize().multiplyScalar(dt*1.35); pos.add(move); }}
        pos.x = Math.max(.75, Math.min(11.1, pos.x)); pos.z = Math.max(.75, Math.min(9.55, pos.z));
        camera.position.copy(pos); camera.position.y = 1.52 + Math.sin(now*.004)*.006;
        camera.rotation.order='YXZ'; camera.rotation.y = yaw; camera.rotation.x = pitch;
        renderer.render(scene,camera); requestAnimationFrame(tick);
      }}
      addEventListener('resize',()=>{{ camera.aspect=innerWidth/innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth,innerHeight); }});
      requestAnimationFrame(tick);
    </script>
  </body>
</html>"""


@router.api_route("/tours/{slug}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def public_tour_page(
    slug: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> Response:
    hostname = request_hostname(request)
    try:
        payload = _load_tour_with_private_receipt(slug)
        _require_public_tour_viewable(payload)
        generated_reconstruction_only = _public_tour_is_generated_reconstruction_only(payload)
        if _tour_payload_is_disabled_fallback(payload) and not generated_reconstruction_only:
            raise HTTPException(status_code=404, detail="tour_disabled_fallback")
        primary_control_path = _public_tour_primary_control_path(payload)
        if primary_control_path and not _public_tour_request_prefers_embedded_media(request):
            return RedirectResponse(
                primary_control_path,
                status_code=302,
                headers=_public_tour_security_headers(),
            )
        viewer_release = evaluate_public_tour_generated_viewer_release(payload)
        if (
            not primary_control_path
            and isinstance(payload.get("generated_viewer_release"), dict)
        ):
            if not viewer_release.get("released"):
                raise HTTPException(
                    status_code=410 if viewer_release.get("terminal") else 404,
                    detail="tour_generated_layout_preview_unavailable",
                )
            viewer_url = _public_tour_generated_viewer_url(
                slug,
                viewer_release.get("viewer_relpath"),
            )
            if not viewer_url:
                raise HTTPException(
                    status_code=404,
                    detail="tour_generated_layout_preview_unavailable",
                )
            return RedirectResponse(
                viewer_url,
                status_code=302,
                headers=_public_tour_security_headers(),
            )
        if generated_reconstruction_only:
            return _generated_reconstruction_public_launch_response(payload, request=request)
        rendered_payload = _redacted_public_tour_payload(payload, expose_asset_relpaths=True)
        rendered_facts, research_snapshot = _merged_facts_with_listing_research(payload, dict(payload.get("facts") or {}))
        rendered_facts.pop("public_preference_snapshot", None)
        feedback_context = _live_property_feedback_context(
            container=container,
            payload=payload,
            slug=slug,
        )
        live_feedback_facts = dict(feedback_context.get("facts") or {}) if isinstance(feedback_context.get("facts"), dict) else {}
        if live_feedback_facts:
            rendered_facts.update(live_feedback_facts)
        shortlist_compare = _public_shortlist_comparison_context(
            container=container,
            payload=payload,
            slug=slug,
            facts=rendered_facts,
        )
        rendered_payload["facts"] = _redacted_public_tour_facts(
            payload,
            rendered_facts,
            privacy_mode=str(rendered_payload.get("tour_privacy_mode") or "anonymous_public"),
        )
        rendered_payload["_public_research_completed"] = bool(research_snapshot)
        rendered_payload["_feedback_enabled"] = bool(str(payload.get("principal_id") or "").strip())
        rendered_payload["_feedback_suggestions"] = dict(feedback_context.get("feedback_suggestions") or {})
        rendered_payload["_learning_summary"] = dict(feedback_context.get("learning_summary") or {})
        rendered_payload["_shortlist_compare"] = dict(shortlist_compare or {})
        nonce = _public_tour_csp_nonce()
        html_body = _tour_html(
            rendered_payload,
            hostname=hostname,
            path=request_path(request),
            nonce=nonce,
            validated_3dvista_control_path=(
                primary_control_path
                if primary_control_path.endswith("/control/3dvista")
                else ""
            ),
        )
        return HTMLResponse(
            html_body,
            headers=_public_tour_security_headers(
                nonce=nonce,
                allow_jsdelivr="https://cdn.jsdelivr.net/" in html_body,
            ),
        )
    except HTTPException as exc:
        detail = str(exc.detail or "").strip().lower()
        if (
            exc.status_code == 410
            and detail == "tour_generated_layout_preview_unavailable"
        ):
            return _render_generated_viewer_terminal_page(request)
        if exc.status_code == 410 and detail == "tour_revoked":
            return _render_tour_unavailable_page(
                request,
                status_code=410,
                title="This tour was removed by its owner.",
                summary="The public copy and its assets are no longer available. Cached copies are queued for removal too.",
                status_label="Tour revoked",
                rows=[
                    {
                        "label": "Tour state",
                        "value": "Removed",
                        "detail": "PropertyQuarry blocks this link even while edge caches finish purging.",
                    },
                    {
                        "label": "Next step",
                        "value": "Return to PropertyQuarry",
                        "detail": "Ask the owner for a new share only if they choose to publish again.",
                    },
                ],
            )
        if exc.status_code == 404 and detail == "tour_generated_layout_preview_unavailable":
            return _render_generated_reconstruction_not_tour_page(request)
        if exc.status_code == 404 and detail == "tour_disabled_fallback":
            return _render_tour_unavailable_page(
                request,
                status_code=404,
                title="This tour link is no longer available.",
                summary="This old link no longer opens as a 3D tour. Ask the sender for a fresh tour link.",
                status_label="Tour unavailable",
                rows=[
                    {
                        "label": "Tour",
                        "value": "Unavailable",
                        "detail": "This link points to an old tour format that is no longer shown.",
                    },
                    {
                        "label": "Next step",
                        "value": "Request a fresh 3D tour",
                        "detail": "Only live tours and licensed panorama tours remain available on this surface.",
                    },
                ],
            )
        if exc.status_code == 404 and detail == "tour_not_found":
            return _render_tour_unavailable_page(
                request,
                status_code=404,
                title="This tour link is no longer available.",
                summary="Ask the sender to share a fresh apartment-tour link or open PropertyQuarry for the latest property page.",
                status_label="Tour unavailable",
                rows=[
                    {
                        "label": "Tour state",
                        "value": "Unavailable",
                        "detail": "The share bundle may have been replaced, removed, or never finished publishing.",
                    },
                    {
                        "label": "Next step",
                        "value": "Request a fresh tour",
                        "detail": f"A current link will open the branded {_public_tour_host_brand_label(hostname, fallback='this domain')} view when the bundle is ready.",
                    },
                ],
            )
        return _render_tour_unavailable_page(
            request,
            status_code=max(int(exc.status_code), 500) if int(exc.status_code) >= 500 else 500,
            title="This tour is temporarily unavailable.",
            summary="The tour link exists, but the published bundle is not ready to render right now. Open PropertyQuarry or ask the sender to republish it.",
            status_label="Tour unavailable",
            rows=[
                {
                    "label": "Tour state",
                    "value": "Publish problem",
                    "detail": "The hosted tour bundle is missing required scenes or metadata.",
                },
                {
                    "label": "Recovery",
                    "value": "Open PropertyQuarry",
                    "detail": "The sender can regenerate or resend the latest branded property-tour link.",
                },
            ],
        )


@router.get("/tours/{slug}/control", response_class=HTMLResponse)
@router.head("/tours/{slug}/control", response_class=HTMLResponse)
def public_tour_control(slug: str, request: Request) -> HTMLResponse:
    payload = _load_tour_with_private_receipt(slug)
    _require_public_tour_viewable(payload)
    if _tour_payload_is_disabled_fallback(payload):
        raise HTTPException(status_code=404, detail="tour_disabled_fallback")
    control_mode = str(payload.get("control_mode") or "").strip().lower()
    primary_control_path = _public_tour_primary_control_path(payload)
    if primary_control_path.endswith("/control/3dvista"):
        # Match the explicit 3DVista route: retain only the allowlisted private
        # provider receipt fields long enough to revalidate target provenance.
        rendered_payload = payload
    else:
        rendered_payload = _redacted_public_tour_payload(
            payload,
            expose_asset_relpaths=control_mode in {"pano2vr", "pano_2_vr"} or bool(_pano2vr_entry_relpath(payload)),
        )
    if _public_tour_request_embeds_walkthrough(request):
        rendered_payload["_tour_control_embed_walkthrough"] = True
    fullscreen = str(request.query_params.get("fullscreen") or "").strip().lower() in {"1", "true", "yes", "on"}
    nonce = _public_tour_csp_nonce()
    html_body = _tour_control_html(
        rendered_payload,
        viewer_mode=(
            "3dvista"
            if primary_control_path.endswith("/control/3dvista")
            else ""
        ),
        fullscreen=fullscreen,
        nonce=nonce,
    )
    return HTMLResponse(
        html_body,
        headers=_public_tour_security_headers(
            nonce=nonce,
            allow_jsdelivr="https://cdn.jsdelivr.net/" in html_body,
        ),
    )


@router.get("/tours/{slug}/control/{viewer_mode}", response_class=HTMLResponse)
@router.head("/tours/{slug}/control/{viewer_mode}", response_class=HTMLResponse)
def public_tour_control_viewer(slug: str, viewer_mode: str, request: Request) -> HTMLResponse:
    payload = _load_tour_with_private_receipt(slug)
    _require_public_tour_viewable(payload)
    if _tour_payload_is_disabled_fallback(payload):
        raise HTTPException(status_code=404, detail="tour_disabled_fallback")
    normalized_viewer_mode = str(viewer_mode or "").strip().lower()
    fullscreen = str(request.query_params.get("fullscreen") or "").strip().lower() in {"1", "true", "yes", "on"}
    if normalized_viewer_mode in {"matterport", "metaport"}:
        raise HTTPException(status_code=404, detail="tour_control_provider_retired")
    nonce = _public_tour_csp_nonce()
    if normalized_viewer_mode in {"3dvista", "3d_vista", "three_d_vista"}:
        # Provider controls need the verified private receipt URL server-side, but
        # the public JSON manifest must continue to omit source/provider URLs.
        rendered_payload = payload
        if _public_tour_request_embeds_walkthrough(request):
            rendered_payload = {**rendered_payload, "_tour_control_embed_walkthrough": True}
        html_body = _tour_control_html(
            rendered_payload,
            viewer_mode=viewer_mode,
            fullscreen=fullscreen,
            nonce=nonce,
        )
        return HTMLResponse(
            html_body,
            headers=_public_tour_security_headers(
                nonce=nonce,
                allow_jsdelivr="https://cdn.jsdelivr.net/" in html_body,
            ),
        )
    rendered_payload = _redacted_public_tour_payload(
        payload,
        expose_asset_relpaths=normalized_viewer_mode in {"pano2vr", "pano_2_vr"},
    )
    if _public_tour_request_embeds_walkthrough(request):
        rendered_payload["_tour_control_embed_walkthrough"] = True
    html_body = _tour_control_html(
        rendered_payload,
        viewer_mode=viewer_mode,
        fullscreen=fullscreen,
        nonce=nonce,
    )
    return HTMLResponse(
        html_body,
        headers=_public_tour_security_headers(
            nonce=nonce,
            allow_jsdelivr="https://cdn.jsdelivr.net/" in html_body,
        ),
    )


@router.post("/tours/{slug}/request-details", response_class=JSONResponse)
async def public_tour_request_details(
    slug: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> JSONResponse:
    payload = _load_tour_with_private_receipt(slug)
    if _tour_payload_is_disabled_fallback(payload):
        raise HTTPException(status_code=404, detail="tour_disabled_fallback")
    principal_id = str(payload.get("principal_id") or "").strip()
    property_url = str(payload.get("property_url") or payload.get("listing_url") or "").strip()
    if not principal_id or not property_url:
        raise HTTPException(status_code=409, detail="tour_detail_request_unavailable")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raise _public_tour_authenticated_action_required("request-details")


@router.post("/tours/{slug}/filters", response_class=JSONResponse)
async def public_tour_filters(
    slug: str,
    request: Request,
) -> JSONResponse:
    payload = _load_tour_with_private_receipt(slug)
    _require_public_tour_viewable(payload)
    if _tour_payload_is_disabled_fallback(payload):
        raise HTTPException(status_code=404, detail="tour_disabled_fallback")
    # Public tour pages intentionally expose no account mutation capability.
    # Filter changes must cross the authenticated application boundary; old
    # browser action tokens are not accepted as a substitute.
    raise _public_tour_authenticated_action_required("filters")


@router.post("/tours/{slug}/feedback", response_class=JSONResponse)
async def public_tour_feedback(
    slug: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> JSONResponse:
    payload = _load_tour_with_private_receipt(slug)
    if _tour_payload_is_disabled_fallback(payload):
        raise HTTPException(status_code=404, detail="tour_disabled_fallback")
    principal_id = str(payload.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=409, detail="tour_feedback_unavailable")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid_tour_feedback_payload")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="invalid_tour_feedback_payload")
    _enforce_public_tour_feedback_rate_limit(request=request, slug=slug, principal_id=principal_id)
    reaction = str(body.get("reaction") or "").strip().lower()
    if reaction not in {"like", "dislike", "maybe", "hide"}:
        raise HTTPException(status_code=422, detail="invalid_tour_feedback_reaction")
    reason_map = _property_feedback_reason_map()
    reason_keys = _public_tour_normalize_reason_keys(
        body.get("reason_keys"),
        allowed=set(reason_map.keys()),
    )
    note = str(body.get("note") or "").strip()
    facts, _ = _merged_facts_with_listing_research(payload, dict(payload.get("facts") or {}))
    facts.pop("public_preference_snapshot", None)
    observation_payload = {
        "slug": slug,
        "property_url": str(payload.get("property_url") or payload.get("listing_url") or "").strip(),
        "property_title": str(payload.get("display_title") or payload.get("title") or "").strip(),
        "reaction": reaction,
        "reason_keys": reason_keys,
        "reason_labels": [_feedback_reason_label(reason_key) for reason_key in reason_keys],
        "note": note[:500],
        "host": request_hostname(request),
        "source": "public_tour_external_feedback",
        "trust": "untrusted_external",
        "facts": dict(facts) if isinstance(facts, dict) else {},
    }
    try:
        container.channel_runtime.ingest_observation(
            principal_id=principal_id,
            channel="propertyquarry",
            event_type="public_tour_external_feedback",
            payload=observation_payload,
            source_id=f"public-tour:{slug}",
            dedupe_key="",
        )
    except Exception:
        log.exception(
            "public tour feedback persistence failed slug=%s principal_hash=%s reaction=%s",
            slug,
            hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:16],
            reaction,
        )
        return JSONResponse(
            {
                "status": "not_captured",
                "trust": "untrusted_external",
                "message": "Feedback could not be saved right now. Please retry from your signed-in account.",
                "retryable": True,
                "error": "public_tour_feedback_persistence_failed",
            },
            status_code=503,
        )
    return JSONResponse(
        {
            "status": "captured_external",
            "trust": "untrusted_external",
            "message": "Feedback captured as an external signal.",
            "reaction": reaction,
            "reason_keys": list(reason_keys),
            "reason_labels": [_feedback_reason_label(reason_key) for reason_key in reason_keys],
            "note": note[:500],
        }
    )
