from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from fastapi import HTTPException


@dataclass(frozen=True)
class PublicTourManifest:
    payload: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return dict(self.payload)


@dataclass(frozen=True)
class PrivateTourReceipt:
    principal_id: str = ""
    search_run_id: str = ""
    listing_url: str = ""
    property_url: str = ""
    source_ref: str = ""
    external_id: str = ""
    recipient_email: str = ""
    crezlo_public_url: str = ""
    source_virtual_tour_url: str = ""
    source_virtual_tour_origin: str = ""
    panorama_source: str = ""
    three_d_vista_import: dict[str, object] = field(default_factory=dict)
    three_d_vista_white_label_proof: dict[str, object] = field(default_factory=dict)
    three_d_vista_browser_render_proof: dict[str, object] = field(default_factory=dict)
    three_d_vista_entry_relpath: str = ""
    three_d_vista_url: str = ""
    matterport_url: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "PrivateTourReceipt":
        source = dict(payload or {})
        return cls(
            principal_id=str(source.get("principal_id") or "").strip(),
            search_run_id=str(source.get("search_run_id") or "").strip(),
            listing_url=str(source.get("listing_url") or "").strip(),
            property_url=str(source.get("property_url") or "").strip(),
            source_ref=str(source.get("source_ref") or "").strip(),
            external_id=str(source.get("external_id") or "").strip(),
            recipient_email=str(source.get("recipient_email") or "").strip().lower(),
            crezlo_public_url=str(source.get("crezlo_public_url") or "").strip(),
            source_virtual_tour_url=str(source.get("source_virtual_tour_url") or "").strip(),
            source_virtual_tour_origin=str(source.get("source_virtual_tour_origin") or "").strip(),
            panorama_source=str(source.get("panorama_source") or "").strip(),
            three_d_vista_import=dict(source.get("three_d_vista_import") or {})
            if isinstance(source.get("three_d_vista_import"), dict)
            else {},
            three_d_vista_white_label_proof=dict(source.get("three_d_vista_white_label_proof") or {})
            if isinstance(source.get("three_d_vista_white_label_proof"), dict)
            else {},
            three_d_vista_browser_render_proof=dict(source.get("three_d_vista_browser_render_proof") or {})
            if isinstance(source.get("three_d_vista_browser_render_proof"), dict)
            else {},
            three_d_vista_entry_relpath=str(
                source.get("three_d_vista_entry_relpath")
                or source.get("threedvista_entry_relpath")
                or source.get("3dvista_entry_relpath")
                or ""
            ).strip(),
            three_d_vista_url=str(source.get("three_d_vista_url") or "").strip(),
            matterport_url=str(source.get("matterport_url") or "").strip(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "principal_id": self.principal_id,
            "search_run_id": self.search_run_id,
            "listing_url": self.listing_url,
            "property_url": self.property_url,
            "source_ref": self.source_ref,
            "external_id": self.external_id,
            "recipient_email": self.recipient_email,
            "crezlo_public_url": self.crezlo_public_url,
            "source_virtual_tour_url": self.source_virtual_tour_url,
            "source_virtual_tour_origin": self.source_virtual_tour_origin,
            "panorama_source": self.panorama_source,
            "three_d_vista_import": self.three_d_vista_import,
            "three_d_vista_white_label_proof": self.three_d_vista_white_label_proof,
            "three_d_vista_browser_render_proof": self.three_d_vista_browser_render_proof,
            "three_d_vista_entry_relpath": self.three_d_vista_entry_relpath,
            "three_d_vista_url": self.three_d_vista_url,
            "matterport_url": self.matterport_url,
        }

_PUBLIC_TOUR_PRIVATE_KEYS = frozenset(
    {
        "_feedback_suggestions",
        "_learning_summary",
        "_shortlist_compare",
        "actor",
        "api_key",
        "audit_rows",
        "auth_header",
        "authorization",
        "cookie",
        "cookies",
        "debug",
        "external_id",
        "headers",
        "internal_ref",
        "learning_summary",
        "owner_id",
        "person_id",
        "preference_nodes",
        "preference_profile",
        "principal_id",
        "private_recipient_email",
        "public_preference_snapshot",
        "raw_signal_json",
        "recipient",
        "recipient_email",
        "recipient_name",
        "recipient_phone",
        "refresh_token",
        "runtime_inputs_json",
        "search_run_id",
        "secret",
        "session",
        "shortlist",
        "shortlist_context",
        "source_ref",
        "token",
    }
)
_PUBLIC_TOUR_PRIVATE_KEY_MARKERS = (
    "access_token",
    "api_key",
    "auth",
    "cookie",
    "credential",
    "debug",
    "internal",
    "learning",
    "oauth",
    "owner",
    "preference",
    "principal",
    "private",
    "recipient",
    "secret",
    "session",
    "shortlist",
    "source_ref",
    "token",
)
_PUBLIC_TOUR_ALLOWED_ASSET_EXTENSIONS = frozenset(
    {
        ".avif",
        ".gif",
        ".jpeg",
        ".jpg",
        ".m4v",
        ".mov",
        ".mp4",
        ".pdf",
        ".png",
        ".webm",
        ".webp",
    }
)
_PUBLIC_TOUR_PUBLIC_PDF_PRIVACY_CLASSES = frozenset(
    {
        "floorplan_pdf_public",
        "floorplan_public",
        "public_floorplan_pdf",
    }
)
_PUBLIC_TOUR_PANO2VR_PUBLIC_PRIVACY_CLASSES = frozenset(
    {
        "pano2vr_export_public",
        "public_pano2vr_export",
    }
)
_PUBLIC_TOUR_PANO2VR_ENTRY_ROLES = frozenset(
    {
        "pano2vr_entry",
        "virtual_tour_entry",
    }
)
_PUBLIC_TOUR_GENERATED_RECONSTRUCTION_PRIVACY_CLASSES = frozenset(
    {
        "generated_reconstruction_public",
        "public_generated_reconstruction",
    }
)
_PUBLIC_TOUR_GENERATED_RECONSTRUCTION_HTML_ROLES = frozenset(
    {
        "generated_reconstruction_viewer",
    }
)
_PUBLIC_TOUR_GENERATED_RECONSTRUCTION_VIEWER_ASSET_ROLES = frozenset(
    {
        "generated_reconstruction_viewer_asset",
    }
)
_PUBLIC_TOUR_GENERATED_RECONSTRUCTION_MODEL_ROLES = frozenset(
    {
        "generated_reconstruction_model",
        "generated_reconstruction_material",
    }
)
_PUBLIC_TOUR_GENERATED_RECONSTRUCTION_PREFIX = "generated-reconstruction/"
_PUBLIC_TOUR_DENIED_ASSET_EXTENSIONS = frozenset(
    {
        ".conf",
        ".csv",
        ".db",
        ".env",
        ".gz",
        ".htm",
        ".html",
        ".ini",
        ".json",
        ".key",
        ".log",
        ".pem",
        ".sqlite",
        ".tar",
        ".txt",
        ".yaml",
        ".yml",
        ".zip",
    }
)
_PUBLIC_TOUR_PRIVACY_MODES = frozenset(
    {
        "anonymous_public",
        "viewer_only",
        "agent_share",
        "family_review",
        "owner_private",
    }
)
_PUBLIC_TOUR_ADDRESS_ALLOWED_MODES = frozenset({"viewer_only", "agent_share", "family_review"})
_PUBLIC_TOUR_EXACT_LOCATION_FACT_KEYS = frozenset(
    {
        "address",
        "address_line",
        "address_lines",
        "exact_address",
        "formatted_address",
        "geojson",
        "geocode",
        "geocoded_address",
        "house_number",
        "lat",
        "latitude",
        "lng",
        "lon",
        "longitude",
        "map_lat",
        "map_lng",
        "postcode",
        "postal_code",
        "reverse_geocode",
        "street",
        "street_address",
        "street_name",
    }
)
_PUBLIC_TOUR_ANONYMOUS_FACT_KEYS = frozenset(
    {
        "area_sqm",
        "availability",
        "balcony_sqm",
        "bathrooms",
        "bedrooms",
        "city",
        "country_code",
        "district",
        "district_name",
        "energy_class",
        "floor",
        "floor_plan",
        "floorplan_available",
        "floorplan_count",
        "garden_sqm",
        "has_360",
        "has_balcony",
        "has_floorplan",
        "has_garden",
        "has_lift",
        "has_loggia",
        "has_terrace",
        "heating_type",
        "lift",
        "elevator",
        "livability_snapshot",
        "municipality",
        "parking_monthly_eur",
        "personal_fit_assessment",
        "postal_name",
        "price_eur",
        "property_type",
        "purchase_price_eur",
        "rooms",
        "teaser_attributes",
        "terrace_sqm",
        "terrace_area_sqm",
        "total_rent_eur",
        "building_units",
        "year_built",
    }
)
_PUBLIC_TOUR_PUBLIC_ASSESSMENT_KEYS = frozenset(
    {
        "adjusted_fit_score",
        "decision_summary",
        "fit_score",
        "good_fit_reasons",
        "livability_snapshot",
        "location_fit_score",
        "match_reasons_json",
        "mismatch_reasons_json",
        "pros",
        "cons",
        "risk_flags",
        "summary",
        "unknowns_json",
    }
)
_PUBLIC_TOUR_TOP_LEVEL_KEYS = frozenset(
    {
        "slug",
        "title",
        "tour_title",
        "display_title",
        "variant_key",
        "variant_label",
        "scene_count",
        "scene_strategy",
        "creation_mode",
        "publication_status",
        "brand_name",
        "hosted_url",
        "public_url",
        "diorama_preview_relpath",
        "preview_relpath",
        "telegram_preview_relpath",
        "facts",
        "control_mode",
        "scenes",
        "video_relpath",
        "video_mobile_relpath",
        "walkable_scene",
        "pano2vr_entry_relpath",
        "pano2vr_export_entry_relpath",
        "pano2vr_export_root_relpath",
        "tour_privacy_mode",
        "privacy_mode",
    }
)
_PUBLIC_TOUR_SCENE_KEYS = frozenset(
    {
        "name",
        "role",
        "mime_type",
        "scene_id",
        "location_id",
        "id",
        "scene",
        "next_scene_id",
        "prev_scene_id",
        "next_scene",
        "prev_scene",
        "next_location_id",
        "prev_location_id",
        "next",
        "prev",
        "next_scene_index",
        "prev_scene_index",
        "image_url",
        "asset_relpath",
        "cube_faces",
    }
)


def public_tour_key_is_private(key: object) -> bool:
    normalized = str(key or "").strip().lower()
    if not normalized:
        return True
    if normalized in _PUBLIC_TOUR_PRIVATE_KEYS:
        return True
    return any(marker in normalized for marker in _PUBLIC_TOUR_PRIVATE_KEY_MARKERS)


def public_tour_safe_asset_relpath(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw or "://" in raw or raw.startswith("/"):
        return ""
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return "/".join(path.parts)


def public_tour_env_truthy(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def public_tour_privacy_mode(payload: dict[str, object]) -> str:
    raw = str(payload.get("tour_privacy_mode") or payload.get("privacy_mode") or "").strip().lower()
    return raw if raw in _PUBLIC_TOUR_PRIVACY_MODES else "anonymous_public"


def require_public_tour_viewable(payload: dict[str, object]) -> None:
    if "publication_status" in payload and payload.get("publication_status") != "ready":
        raise HTTPException(status_code=404, detail="tour_not_found")
    if public_tour_privacy_mode(payload) == "owner_private":
        raise HTTPException(status_code=404, detail="tour_not_found")
    require_governed_spatial_public_tour_viewable(payload)


def require_governed_spatial_public_tour_viewable(
    payload: dict[str, object],
    *,
    observed_at: datetime | None = None,
    lifecycle_resolver: Callable[[str, datetime], Mapping[str, object]] | None = None,
) -> None:
    marker = payload.get("governed_spatial")
    marker_present = marker is not None
    if marker_present and not isinstance(marker, dict):
        raise HTTPException(status_code=404, detail="tour_not_found")
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise HTTPException(status_code=404, detail="tour_not_found")
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    slug = str(payload.get("slug") or "").strip()
    if not slug:
        if not marker_present:
            return
        raise HTTPException(status_code=404, detail="tour_not_found")
    if lifecycle_resolver is None:
        from app.product.property_tour_hosting import (
            GovernedPropertyTourContractError,
            GovernedPropertyTourIntegrityError,
            GovernedPropertyTourLifecycleStore,
            _public_tour_dir,
        )

        public_root = _public_tour_dir()
        private_root = public_root / GovernedPropertyTourLifecycleStore._PRIVATE_DIR
        if not private_root.exists() or private_root.is_symlink():
            if not marker_present and not private_root.is_symlink():
                return
            raise HTTPException(status_code=404, detail="tour_not_found")
        try:
            lifecycle = GovernedPropertyTourLifecycleStore(public_root).public_state(
                slug=slug,
                observed_at=now,
            )
        except (GovernedPropertyTourContractError, GovernedPropertyTourIntegrityError, OSError) as exc:
            raise HTTPException(status_code=404, detail="tour_not_found") from exc
    else:
        lifecycle = lifecycle_resolver(slug, now)
    if not isinstance(lifecycle, Mapping):
        raise HTTPException(status_code=404, detail="tour_not_found")
    lifecycle_missing = (
        lifecycle.get("status") == "blocked"
        and lifecycle.get("reason") == "privacy_lifecycle_missing"
    )
    if lifecycle_missing and not marker_present:
        return
    if lifecycle_missing or not marker_present:
        raise HTTPException(status_code=404, detail="tour_not_found")
    expected_marker_fields = {
        "contract_name",
        "composition_digest",
        "artifact_digest",
        "publication_decision_digest",
    }
    if not isinstance(marker, Mapping) or set(marker) != expected_marker_fields:
        raise HTTPException(status_code=404, detail="tour_not_found")
    if marker.get("contract_name") != "propertyquarry.governed_spatial_public_binding.v1":
        raise HTTPException(status_code=404, detail="tour_not_found")
    for field in ("composition_digest", "artifact_digest", "publication_decision_digest"):
        value = marker.get(field)
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
            or value != lifecycle.get(field)
        ):
            raise HTTPException(status_code=404, detail="tour_not_found")
    try:
        retention_expires_at_epoch = int(lifecycle.get("retention_expires_at_epoch") or 0)
    except (TypeError, ValueError):
        retention_expires_at_epoch = 0
    eligible = (
        lifecycle.get("status") == "active"
        and lifecycle.get("serving_allowed") is True
        and lifecycle.get("deleted") is False
        and lifecycle.get("revoked") is False
    )
    if not eligible or retention_expires_at_epoch <= int(now.timestamp()):
        raise HTTPException(status_code=404, detail="tour_not_found")


def public_tour_exact_address_allowed(payload: dict[str, object], *, privacy_mode: str) -> bool:
    if privacy_mode not in _PUBLIC_TOUR_ADDRESS_ALLOWED_MODES:
        return False
    return public_tour_env_truthy(
        payload.get("public_address_allowed")
        or payload.get("public_exact_location_allowed")
        or payload.get("share_exact_location")
    )


def public_tour_asset_path_is_public(
    relpath: str,
    *,
    privacy_class: str = "",
    role: str = "",
    mime_type: str = "",
) -> bool:
    safe_relpath = public_tour_safe_asset_relpath(relpath)
    if not safe_relpath:
        return False
    suffix = PurePosixPath(safe_relpath).suffix.lower()
    normalized_privacy = str(privacy_class or "").strip().lower()
    normalized_role = str(role or "").strip().lower().replace("-", "_")
    is_generated_reconstruction_asset = normalized_privacy in _PUBLIC_TOUR_GENERATED_RECONSTRUCTION_PRIVACY_CLASSES
    if is_generated_reconstruction_asset and not safe_relpath.startswith(_PUBLIC_TOUR_GENERATED_RECONSTRUCTION_PREFIX):
        return False
    if suffix in {".htm", ".html"}:
        return (
            (
                normalized_privacy in _PUBLIC_TOUR_PANO2VR_PUBLIC_PRIVACY_CLASSES
                and normalized_role in _PUBLIC_TOUR_PANO2VR_ENTRY_ROLES
            )
            or (
                is_generated_reconstruction_asset
                and normalized_role in _PUBLIC_TOUR_GENERATED_RECONSTRUCTION_HTML_ROLES
            )
        )
    if suffix in {".js", ".mjs"}:
        return (
            is_generated_reconstruction_asset
            and normalized_role in _PUBLIC_TOUR_GENERATED_RECONSTRUCTION_VIEWER_ASSET_ROLES
        )
    if suffix in {".obj", ".mtl", ".glb"}:
        return False
    if suffix in _PUBLIC_TOUR_DENIED_ASSET_EXTENSIONS:
        return False
    if suffix not in _PUBLIC_TOUR_ALLOWED_ASSET_EXTENSIONS:
        return False
    if suffix == ".pdf" or "pdf" in str(mime_type or "").strip().lower():
        return normalized_privacy in _PUBLIC_TOUR_PUBLIC_PDF_PRIVACY_CLASSES and normalized_role in {
            "floorplan",
            "floor_plan",
            "layout",
            "valuation_floorplan",
        }
    return True


def public_tour_collect_asset_refs(payload: dict[str, object]) -> set[str]:
    refs: set[str] = set()

    def _add(
        value: object,
        *,
        privacy_class: str = "",
        role: str = "",
        mime_type: str = "",
    ) -> None:
        relpath = public_tour_safe_asset_relpath(value)
        if relpath and public_tour_asset_path_is_public(
            relpath,
            privacy_class=privacy_class,
            role=role,
            mime_type=mime_type,
        ):
            refs.add(relpath)

    for key, role in (
        ("diorama_preview_relpath", "diorama"),
        ("preview_relpath", "preview"),
        ("telegram_preview_relpath", "preview"),
    ):
        _add(payload.get(key), privacy_class="public", role=role)
    _add(payload.get("video_relpath"))
    _add(payload.get("video_mobile_relpath"), role="video_mobile")
    for key in ("pano2vr_entry_relpath", "pano2vr_export_entry_relpath"):
        _add(
            payload.get(key),
            privacy_class="pano2vr_export_public",
            role="pano2vr_entry",
            mime_type="text/html",
        )
    generated_reconstruction = payload.get("generated_reconstruction")
    if isinstance(generated_reconstruction, dict):
        _add(
            generated_reconstruction.get("viewer_relpath"),
            privacy_class="generated_reconstruction_public",
            role="generated_reconstruction_viewer",
            mime_type="text/html",
        )
        _add(
            generated_reconstruction.get("walkthrough_video_relpath"),
            privacy_class="generated_reconstruction_public",
            role="video",
        )
        _add(
            generated_reconstruction.get("floorplan_relpath"),
            privacy_class="generated_reconstruction_public",
            role="floorplan",
        )
        for value in list(generated_reconstruction.get("photo_relpaths") or []):
            _add(
                value,
                privacy_class="generated_reconstruction_public",
                role="photo",
            )
    for scene in list(payload.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        scene_privacy = str(scene.get("privacy_class") or scene.get("privacy") or "").strip()
        scene_role = str(scene.get("role") or "").strip()
        scene_mime = str(scene.get("mime_type") or "").strip()
        for key in ("asset_relpath", "thumbnail_relpath", "preview_relpath", "floorplan_relpath"):
            _add(scene.get(key), privacy_class=scene_privacy, role=scene_role, mime_type=scene_mime)
        cube_faces = scene.get("cube_faces")
        if isinstance(cube_faces, dict):
            for value in cube_faces.values():
                _add(value)
    public_assets = payload.get("public_assets")
    if isinstance(public_assets, list):
        for row in public_assets:
            if isinstance(row, str):
                _add(row)
                continue
            if not isinstance(row, dict):
                continue
            privacy_class = str(row.get("privacy_class") or row.get("privacy") or "public").strip().lower()
            if privacy_class in {"private", "internal", "debug", "restricted"}:
                continue
            role = str(row.get("role") or row.get("asset_role") or "").strip()
            mime_type = str(row.get("mime_type") or row.get("content_type") or "").strip()
            for key in ("path", "relpath", "asset_relpath"):
                _add(row.get(key), privacy_class=privacy_class, role=role, mime_type=mime_type)
    return refs


def public_tour_allowed_asset_paths(payload: dict[str, object]) -> set[str]:
    return set(public_tour_collect_asset_refs(payload))


def public_tour_asset_metadata(payload: dict[str, object]) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}

    def _record(
        value: object,
        *,
        privacy_class: str = "",
        role: str = "",
        mime_type: str = "",
    ) -> None:
        relpath = public_tour_safe_asset_relpath(value)
        if not relpath or not public_tour_asset_path_is_public(
            relpath,
            privacy_class=privacy_class,
            role=role,
            mime_type=mime_type,
        ):
            return
        row = metadata.setdefault(relpath, {})
        normalized_privacy = str(privacy_class or "").strip().lower()
        normalized_role = str(role or "").strip().lower().replace("-", "_")
        if normalized_privacy:
            row["privacy_class"] = normalized_privacy
        if normalized_role:
            row["role"] = normalized_role
        if mime_type:
            row["mime_type"] = str(mime_type).strip()

    for key, role in (
        ("diorama_preview_relpath", "diorama"),
        ("preview_relpath", "preview"),
        ("telegram_preview_relpath", "preview"),
    ):
        _record(payload.get(key), privacy_class="public", role=role)
    _record(payload.get("video_relpath"), role="video")
    _record(payload.get("video_mobile_relpath"), role="video_mobile")
    for key in ("pano2vr_entry_relpath", "pano2vr_export_entry_relpath"):
        _record(
            payload.get(key),
            privacy_class="pano2vr_export_public",
            role="pano2vr_entry",
            mime_type="text/html",
        )
    generated_reconstruction = payload.get("generated_reconstruction")
    if isinstance(generated_reconstruction, dict):
        _record(
            generated_reconstruction.get("viewer_relpath"),
            privacy_class="generated_reconstruction_public",
            role="generated_reconstruction_viewer",
            mime_type="text/html",
        )
        _record(
            generated_reconstruction.get("walkthrough_video_relpath"),
            privacy_class="generated_reconstruction_public",
            role="video",
        )
        _record(
            generated_reconstruction.get("floorplan_relpath"),
            privacy_class="generated_reconstruction_public",
            role="floorplan",
        )
        for value in list(generated_reconstruction.get("photo_relpaths") or []):
            _record(
                value,
                privacy_class="generated_reconstruction_public",
                role="photo",
            )
    for scene in list(payload.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        scene_privacy = str(scene.get("privacy_class") or scene.get("privacy") or "").strip()
        scene_role = str(scene.get("role") or "").strip()
        scene_mime = str(scene.get("mime_type") or "").strip()
        for key in ("asset_relpath", "thumbnail_relpath", "preview_relpath", "floorplan_relpath"):
            _record(scene.get(key), privacy_class=scene_privacy, role=scene_role, mime_type=scene_mime)
        cube_faces = scene.get("cube_faces")
        if isinstance(cube_faces, dict):
            for value in cube_faces.values():
                _record(value, role="cube_face")
    public_assets = payload.get("public_assets")
    if isinstance(public_assets, list):
        for row in public_assets:
            if isinstance(row, str):
                _record(row)
                continue
            if not isinstance(row, dict):
                continue
            privacy_class = str(row.get("privacy_class") or row.get("privacy") or "public").strip().lower()
            if privacy_class in {"private", "internal", "debug", "restricted"}:
                continue
            role = str(row.get("role") or row.get("asset_role") or "").strip()
            mime_type = str(row.get("mime_type") or row.get("content_type") or "").strip()
            for key in ("path", "relpath", "asset_relpath"):
                _record(row.get(key), privacy_class=privacy_class, role=role, mime_type=mime_type)
    return metadata


def public_tour_file_url(slug: str, relpath: str) -> str:
    safe_relpath = public_tour_safe_asset_relpath(relpath)
    if not slug or not safe_relpath:
        return ""
    return f"/tours/files/{slug}/{safe_relpath}"


def public_tour_canonical_path(slug: str) -> str:
    normalized_slug = str(slug or "").strip()
    if not normalized_slug:
        return ""
    if "/" in normalized_slug or "\\" in normalized_slug or ".." in normalized_slug:
        return ""
    return f"/tours/{normalized_slug}"


def public_tour_manifest(
    payload: dict[str, object],
    *,
    only_relpath: str = "",
    bundle_dir_resolver: Callable[[str], Path | None],
) -> dict[str, dict[str, object]]:
    slug = str(payload.get("slug") or "").strip()
    bundle_dir = bundle_dir_resolver(slug)
    only_safe_relpath = public_tour_safe_asset_relpath(only_relpath)
    manifest: dict[str, dict[str, object]] = {}
    for relpath, metadata in sorted(public_tour_asset_metadata(payload).items()):
        if only_safe_relpath and relpath != only_safe_relpath:
            continue
        row: dict[str, object] = {
            "path": relpath,
            "url": public_tour_file_url(slug, relpath),
            "mime_type": metadata.get("mime_type") or mimetypes.guess_type(relpath)[0] or "application/octet-stream",
            "privacy_class": metadata.get("privacy_class") or "public",
        }
        if metadata.get("role"):
            row["role"] = metadata["role"]
        if bundle_dir is not None:
            candidate = (bundle_dir / relpath).resolve()
            try:
                if bundle_dir.resolve() in candidate.parents and candidate.exists() and candidate.is_file():
                    size_bytes = candidate.stat().st_size
                    row["size_bytes"] = size_bytes
                    mime_type = str(row.get("mime_type") or "").strip().lower()
                    should_hash = size_bytes <= (8 * 1024 * 1024) and not mime_type.startswith("video/")
                    if should_hash:
                        digest = hashlib.sha256()
                        with candidate.open("rb") as handle:
                            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                                digest.update(chunk)
                        row["sha256"] = digest.hexdigest()
            except OSError:
                pass
        manifest[relpath] = row
    return manifest


def public_tour_safe_http_url(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 4096:
        return ""
    from urllib.parse import urlparse

    if "\\" in normalized or any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        return ""
    parsed = urlparse(normalized)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    try:
        if parsed.port not in {None, 443}:
            return ""
    except ValueError:
        return ""
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname or ".." in hostname or not re.fullmatch(r"[a-z0-9.-]+", hostname):
        return ""
    netloc = hostname
    return parsed._replace(scheme="https", netloc=netloc).geturl()


def public_tour_external_media_url_allowed(
    value: object,
    *,
    url_allowed: Callable[[str], bool],
) -> bool:
    normalized = public_tour_safe_http_url(value)
    if not normalized:
        return False
    return url_allowed(normalized)


def redact_public_tour_value(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            if public_tour_key_is_private(key):
                continue
            redacted[str(key)] = redact_public_tour_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_public_tour_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_public_tour_value(item) for item in value]
    return value


def redacted_public_tour_facts(
    payload: dict[str, object],
    facts: dict[str, object],
    *,
    privacy_mode: str,
) -> dict[str, object]:
    redacted_value = redact_public_tour_value(facts if isinstance(facts, dict) else {})
    redacted = dict(redacted_value) if isinstance(redacted_value, dict) else {}
    exact_address_allowed = public_tour_exact_address_allowed(payload, privacy_mode=privacy_mode)
    for key in list(redacted.keys()):
        normalized_key = str(key or "").strip().lower()
        if normalized_key in _PUBLIC_TOUR_EXACT_LOCATION_FACT_KEYS and not exact_address_allowed:
            redacted.pop(key, None)
    if privacy_mode != "anonymous_public":
        return redacted

    def _redacted_public_livability(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {}
        return {
            str(livability_key): redact_public_tour_value(livability_value)
            for livability_key, livability_value in value.items()
            if str(livability_key or "").strip().lower().startswith("nearest_")
        }

    public_facts: dict[str, object] = {}
    for key, value in redacted.items():
        normalized_key = str(key or "").strip().lower()
        if normalized_key in _PUBLIC_TOUR_EXACT_LOCATION_FACT_KEYS:
            continue
        if normalized_key.startswith("nearest_") or normalized_key in _PUBLIC_TOUR_ANONYMOUS_FACT_KEYS:
            if normalized_key == "personal_fit_assessment" and isinstance(value, dict):
                assessment: dict[str, object] = {}
                for assessment_key, assessment_value in value.items():
                    normalized_assessment_key = str(assessment_key or "").strip().lower()
                    if normalized_assessment_key not in _PUBLIC_TOUR_PUBLIC_ASSESSMENT_KEYS:
                        continue
                    if normalized_assessment_key == "livability_snapshot":
                        assessment[str(assessment_key)] = _redacted_public_livability(assessment_value)
                    else:
                        assessment[str(assessment_key)] = redact_public_tour_value(assessment_value)
                public_facts[str(key)] = assessment
            elif normalized_key == "livability_snapshot":
                public_facts[str(key)] = _redacted_public_livability(value)
            else:
                public_facts[str(key)] = value
    return public_facts


def redacted_public_tour_scenes(
    payload: dict[str, object],
    *,
    expose_asset_relpaths: bool,
    url_allowed: Callable[[str], bool],
) -> list[dict[str, object]]:
    slug = str(payload.get("slug") or "").strip()
    allowed_assets = public_tour_allowed_asset_paths(payload)
    rows: list[dict[str, object]] = []
    for scene in list(payload.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        rendered: dict[str, object] = {}
        for key, value in scene.items():
            if key not in _PUBLIC_TOUR_SCENE_KEYS or public_tour_key_is_private(key):
                continue
            if key == "asset_relpath":
                relpath = public_tour_safe_asset_relpath(value)
                if relpath not in allowed_assets:
                    continue
                if expose_asset_relpaths:
                    rendered[key] = relpath
                else:
                    rendered["image_url"] = public_tour_file_url(slug, relpath)
                continue
            if key == "cube_faces":
                cube_faces: dict[str, object] = {}
                for face_key, face_value in dict(value or {}).items():
                    relpath = public_tour_safe_asset_relpath(face_value)
                    if relpath not in allowed_assets:
                        continue
                    cube_faces[str(face_key)] = relpath if expose_asset_relpaths else public_tour_file_url(slug, relpath)
                if cube_faces:
                    rendered[key] = cube_faces
                continue
            if key == "image_url":
                safe_url = public_tour_external_media_url_allowed(value, url_allowed=url_allowed) and public_tour_safe_http_url(value)
                if safe_url:
                    rendered[key] = safe_url
                continue
            rendered[str(key)] = redact_public_tour_value(value)
        scene_role = str(rendered.get("role") or "").strip().lower().replace("-", "_")
        if rendered and (
            "image_url" in rendered
            or "asset_relpath" in rendered
            or "cube_faces" in rendered
            or scene_role == "live_360"
        ):
            rows.append(rendered)
    return rows


def redacted_public_tour_payload(
    payload: dict[str, object],
    *,
    expose_asset_relpaths: bool = False,
    url_allowed: Callable[[str], bool],
    bundle_dir_resolver: Callable[[str], Path | None],
) -> dict[str, object]:
    rendered: dict[str, object] = {}
    slug = str(payload.get("slug") or "").strip()
    privacy_mode = public_tour_privacy_mode(payload)
    governed = payload.get("governed_spatial") is not None
    for key in _PUBLIC_TOUR_TOP_LEVEL_KEYS:
        if key not in payload or public_tour_key_is_private(key):
            continue
        if key == "facts":
            rendered[key] = redacted_public_tour_facts(
                payload,
                payload.get(key) if isinstance(payload.get(key), dict) else {},
                privacy_mode=privacy_mode,
            )
            continue
        if key == "scenes":
            rendered_scenes = redacted_public_tour_scenes(
                payload,
                expose_asset_relpaths=expose_asset_relpaths,
                url_allowed=(lambda _value: False) if governed else url_allowed,
            )
            if governed:
                rendered_scenes = [
                    scene
                    for scene in rendered_scenes
                    if "image_url" in scene or "asset_relpath" in scene or "cube_faces" in scene
                ]
            rendered[key] = rendered_scenes
            continue
        if key in {"video_relpath", "video_mobile_relpath"}:
            relpath = public_tour_safe_asset_relpath(payload.get(key))
            if not relpath or relpath not in public_tour_allowed_asset_paths(payload):
                continue
            if expose_asset_relpaths:
                rendered[key] = relpath
            else:
                rendered[key.replace("_relpath", "_url")] = public_tour_file_url(slug, relpath)
            continue
        if key in {"diorama_preview_relpath", "preview_relpath", "telegram_preview_relpath"}:
            relpath = public_tour_safe_asset_relpath(payload.get(key))
            if not relpath or relpath not in public_tour_allowed_asset_paths(payload):
                continue
            if expose_asset_relpaths:
                rendered[key] = relpath
            else:
                rendered[key.replace("_relpath", "_url")] = public_tour_file_url(slug, relpath)
            continue
        if key in {"pano2vr_entry_relpath", "pano2vr_export_entry_relpath"}:
            relpath = public_tour_safe_asset_relpath(payload.get(key))
            if not relpath or relpath not in public_tour_allowed_asset_paths(payload):
                continue
            if expose_asset_relpaths:
                rendered[key] = relpath
            else:
                rendered[key.replace("_relpath", "_url")] = public_tour_file_url(slug, relpath)
            continue
        if key in {"hosted_url", "public_url"}:
            canonical_path = public_tour_canonical_path(slug)
            if canonical_path:
                rendered[key] = canonical_path
            continue
        rendered[key] = redact_public_tour_value(payload.get(key))
    rendered["slug"] = slug
    rendered["tour_privacy_mode"] = privacy_mode
    rendered.setdefault("facts", {})
    rendered.setdefault("scenes", [])
    if not expose_asset_relpaths:
        rendered["public_assets"] = list(
            public_tour_manifest(
                payload,
                bundle_dir_resolver=bundle_dir_resolver,
            ).values()
        )
    return rendered


def build_public_tour_manifest(
    payload: dict[str, object],
    *,
    expose_asset_relpaths: bool = False,
    url_allowed: Callable[[str], bool],
    bundle_dir_resolver: Callable[[str], Path | None],
) -> PublicTourManifest:
    return PublicTourManifest(
        redacted_public_tour_payload(
            dict(payload or {}),
            expose_asset_relpaths=expose_asset_relpaths,
            url_allowed=url_allowed,
            bundle_dir_resolver=bundle_dir_resolver,
        )
    )
