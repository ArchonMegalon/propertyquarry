from __future__ import annotations

import hashlib
import math
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
    candidate_ref: str = ""
    listing_url: str = ""
    property_url: str = ""
    source_ref: str = ""
    external_id: str = ""
    recipient_email: str = ""
    crezlo_public_url: str = ""
    source_virtual_tour_url: str = ""
    source_virtual_tour_origin: str = ""
    panorama_source: str = ""
    pano2vr_spatial_provenance: dict[str, object] = field(default_factory=dict)
    three_d_vista_import: dict[str, object] = field(default_factory=dict)
    three_d_vista_white_label_proof: dict[str, object] = field(default_factory=dict)
    three_d_vista_browser_render_proof: dict[str, object] = field(default_factory=dict)
    three_d_vista_entry_relpath: str = ""
    three_d_vista_url: str = ""
    matterport_url: str = ""
    video_provider: str = ""
    video_provider_key: str = ""
    video_render_provider: str = ""
    video_coverage_proof: str = ""
    private_exact_location: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "PrivateTourReceipt":
        source = dict(payload or {})
        return cls(
            principal_id=str(source.get("principal_id") or "").strip(),
            search_run_id=str(source.get("search_run_id") or "").strip(),
            candidate_ref=str(
                source.get("candidate_ref")
                or source.get("research_candidate_ref")
                or ""
            ).strip(),
            listing_url=str(source.get("listing_url") or "").strip(),
            property_url=str(source.get("property_url") or "").strip(),
            source_ref=str(source.get("source_ref") or "").strip(),
            external_id=str(source.get("external_id") or "").strip(),
            recipient_email=str(source.get("recipient_email") or "").strip().lower(),
            crezlo_public_url=str(source.get("crezlo_public_url") or "").strip(),
            source_virtual_tour_url=str(source.get("source_virtual_tour_url") or "").strip(),
            source_virtual_tour_origin=str(source.get("source_virtual_tour_origin") or "").strip(),
            panorama_source=str(source.get("panorama_source") or "").strip(),
            pano2vr_spatial_provenance=dict(source.get("pano2vr_spatial_provenance") or {})
            if isinstance(source.get("pano2vr_spatial_provenance"), dict)
            else {},
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
            video_provider=str(source.get("video_provider") or "").strip(),
            video_provider_key=str(source.get("video_provider_key") or "").strip(),
            video_render_provider=str(
                source.get("video_render_provider") or ""
            ).strip(),
            video_coverage_proof=str(
                source.get("video_coverage_proof") or ""
            ).strip(),
            private_exact_location=private_tour_exact_location_snapshot(source),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "principal_id": self.principal_id,
            "search_run_id": self.search_run_id,
            "candidate_ref": self.candidate_ref,
            "listing_url": self.listing_url,
            "property_url": self.property_url,
            "source_ref": self.source_ref,
            "external_id": self.external_id,
            "recipient_email": self.recipient_email,
            "crezlo_public_url": self.crezlo_public_url,
            "source_virtual_tour_url": self.source_virtual_tour_url,
            "source_virtual_tour_origin": self.source_virtual_tour_origin,
            "panorama_source": self.panorama_source,
            "pano2vr_spatial_provenance": self.pano2vr_spatial_provenance,
            "three_d_vista_import": self.three_d_vista_import,
            "three_d_vista_white_label_proof": self.three_d_vista_white_label_proof,
            "three_d_vista_browser_render_proof": self.three_d_vista_browser_render_proof,
            "three_d_vista_entry_relpath": self.three_d_vista_entry_relpath,
            "three_d_vista_url": self.three_d_vista_url,
            "matterport_url": self.matterport_url,
            "video_provider": self.video_provider,
            "video_provider_key": self.video_provider_key,
            "video_render_provider": self.video_render_provider,
            "video_coverage_proof": self.video_coverage_proof,
            "private_exact_location": self.private_exact_location,
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
        "personal_fit_assessment",
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
        "video_coverage_proof",
        "video_provider",
        "video_provider_key",
        "video_render_provider",
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
_PUBLIC_TOUR_READY_PUBLICATION_STATUSES = frozenset(
    {"active", "published", "ready"}
)
_PUBLIC_TOUR_SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PUBLIC_TOUR_RESERVED_STAGE_PREFIX = ".propertyquarry-stage-"
_PUBLIC_TOUR_URI_DELIMITERS = frozenset(":?#[]@!$&'()*+,;=%")
_PUBLIC_TOUR_QUARANTINED_ASSET_NAMES = frozenset(
    {
        "tour.json",
        "tour.private.json",
        "tour.magicfit.json",
        "tour.magicfit.pending.json",
    }
)
_PUBLIC_TOUR_QUARANTINED_ASSET_ROOTS = frozenset(
    {
        ".magicfit-deliveries",
        ".magicfit-staging",
    }
)
_PUBLIC_TOUR_PENDING_MAGICFIT_PROOF_STATUSES = frozenset(
    {
        "provider_render_pending_delivery_acceptance",
        "render_verified_pending_delivery_acceptance",
        "rendered_pending_delivery_acceptance",
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
_PUBLIC_TOUR_EXACT_LOCATION_COMPACT_KEYS = frozenset(
    re.sub(r"[^a-z0-9]+", "", key.lower())
    for key in _PUBLIC_TOUR_EXACT_LOCATION_FACT_KEYS
)
_PUBLIC_TOUR_NEAREST_COORDINATE_SUFFIXES = frozenset(
    {"lat", "latitude", "lng", "lon", "longitude"}
)
_PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_KEYS = frozenset(
    re.sub(r"[^a-z0-9]+", "", key.lower())
    for key in {
        "address",
        "address_line",
        "address_lines",
        "exact_address",
        "formatted_address",
        "geocoded_address",
        "reverse_geocode",
        "street",
        "street_address",
        "street_name",
    }
)
_PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_LIMIT = 64
_PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_MAX_CHARS = 512
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
_PUBLIC_TOUR_COARSE_LOCATION_FACT_KEYS = frozenset(
    {
        "city",
        "district",
        "district_name",
        "municipality",
        "postal_name",
    }
)
_PUBLIC_TOUR_UNIQUENESS_SENSITIVE_FACT_KEYS = frozenset(
    {
        "area_sqm",
        "district",
        "district_name",
        "floor",
        "municipality",
        "postal_name",
        "price_eur",
        "purchase_price_eur",
        "total_rent_eur",
    }
)
_PUBLIC_TOUR_UNIQUENESS_RISK_LABELS = frozenset(
    {
        "high",
        "rare",
        "rare_listing",
        "small_market",
        "small_municipality",
        "unique",
    }
)
_PUBLIC_TOUR_SMALL_MUNICIPALITY_POPULATION = 20_000
_PUBLIC_TOUR_HIGH_PURCHASE_PRICE_EUR = 2_000_000
_PUBLIC_TOUR_HIGH_MONTHLY_RENT_EUR = 5_000
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
        "covered_route_labels",
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
        "property_url_sha256",
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
        "video_sidecar_relpath",
        "video_source",
        "room_visit_plan",
        "generated_reconstruction",
        "walkable_scene",
        "pano2vr_entry_relpath",
        "pano2vr_export_entry_relpath",
        "pano2vr_export_root_relpath",
        "tour_privacy_mode",
        "privacy_mode",
    }
)
_PUBLIC_TOUR_GENERATED_RECONSTRUCTION_KEYS = frozenset(
    {
        "provider",
        "generated_at",
        "viewer_version",
        "viewer_relpath",
        "model_relpath",
        "material_relpath",
        "manifest_relpath",
        "glb_export_status",
        "verified_provider_capture",
        "satisfies_verified_tour_gate",
        "disclosure",
        "style_contract_version",
        "style_id",
        "style_label",
        "style_signature",
        "style_scene_signature",
        "style_evidence_status",
        "styled_scene_instance_count",
        "style_cue_kinds",
        "floorplan_display_mode",
        "route_labels",
        "room_stop_count",
        "walkthrough_route_labels",
        "walkthrough_stop_count",
        "photo_reference_panel_count",
        "walkable_scene",
        "walkable_scene_kind",
        "floorplan_relpath",
        "photo_relpaths",
        "glb_model_relpath",
        "diorama_preview_bundle_relpath",
        "telegram_preview_bundle_relpath",
        "walkthrough_video_relpath",
        "walkthrough_style_label",
        "walkthrough_composition",
        "walkthrough_motion_style",
        "walkthrough_sidecar_relpath",
        "walkthrough_coverage_proof",
    }
)
_PUBLIC_TOUR_GENERATED_RECONSTRUCTION_PUBLIC_ASSET_KEYS = frozenset(
    {
        "viewer_relpath",
        "model_relpath",
        "material_relpath",
        "floorplan_relpath",
        "glb_model_relpath",
        "diorama_preview_bundle_relpath",
        "telegram_preview_bundle_relpath",
        "walkthrough_video_relpath",
    }
)
_PUBLIC_TOUR_GENERATED_RECONSTRUCTION_INTERNAL_ASSET_KEYS = frozenset(
    {
        "manifest_relpath",
        "walkthrough_sidecar_relpath",
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
        "street_view_url",
    }
)

_PUBLIC_TOUR_STREET_CONTEXT_HOSTS = frozenset(
    {"google.com", "www.google.com", "maps.google.com"}
)


def public_tour_key_is_private(key: object) -> bool:
    normalized = str(key or "").strip().lower()
    if not normalized:
        return True
    if normalized in _PUBLIC_TOUR_PRIVATE_KEYS:
        return True
    return any(marker in normalized for marker in _PUBLIC_TOUR_PRIVATE_KEY_MARKERS)


def public_tour_key_is_exact_location(key: object) -> bool:
    raw_key = str(key or "").strip()
    compact = re.sub(r"[^a-z0-9]+", "", raw_key.lower())
    if bool(compact) and compact in _PUBLIC_TOUR_EXACT_LOCATION_COMPACT_KEYS:
        return True
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw_key)
    segments = tuple(
        segment
        for segment in re.sub(r"[^a-z0-9]+", "_", separated.lower()).split("_")
        if segment
    )
    return (
        len(segments) >= 2
        and segments[0] == "nearest"
        and segments[-1] in _PUBLIC_TOUR_NEAREST_COORDINATE_SUFFIXES
    )


def private_tour_exact_location_snapshot(payload: dict[str, object]) -> dict[str, object]:
    """Retain exact location only in the private, owner-controlled receipt."""

    def _select(value: object) -> tuple[bool, object]:
        if isinstance(value, dict):
            selected: dict[str, object] = {}
            for key, child in value.items():
                normalized_key = str(key)
                if public_tour_key_is_exact_location(key):
                    selected[normalized_key] = child
                    continue
                found, nested = _select(child)
                if found:
                    selected[normalized_key] = nested
            return bool(selected), selected
        if isinstance(value, (list, tuple)):
            selected_items: list[object] = []
            found_any = False
            for child in value:
                found, nested = _select(child)
                selected_items.append(nested if found else None)
                found_any = found_any or found
            return found_any, selected_items
        return False, None

    found, selected = _select(dict(payload or {}))
    return dict(selected) if found and isinstance(selected, dict) else {}


def public_tour_exact_location_string_fingerprints(
    payload: dict[str, object],
) -> tuple[str, ...]:
    """Return bounded exact-location strings that must never enter public JSON.

    Exact-address fields often get copied into otherwise public presentation
    strings (titles, scene labels, summaries).  Key filtering cannot catch that
    second-order leak.  Only address/street-sized values become fingerprints;
    coordinates, postcodes, and lone house numbers are deliberately excluded so
    a small scalar cannot erase unrelated public copy globally.
    """

    fingerprints: set[str] = set()
    facts = payload.get("facts")
    coarse_location_values = {
        re.sub(r"\s+", " ", value).strip().casefold()
        for key, value in (facts.items() if isinstance(facts, dict) else ())
        if str(key or "").strip().lower() in _PUBLIC_TOUR_COARSE_LOCATION_FACT_KEYS
        and isinstance(value, str)
        and re.sub(r"\s+", " ", value).strip()
    }

    def _add(value: object) -> None:
        if len(fingerprints) >= _PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_LIMIT:
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                _add(child)
                if len(fingerprints) >= _PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_LIMIT:
                    break
            return
        if not isinstance(value, str):
            return
        candidate = re.sub(r"\s+", " ", value).strip()
        candidate = candidate[
            :_PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_MAX_CHARS
        ].rstrip()
        # Coarse location labels are an explicit part of the anonymous public
        # facts contract. Address-line arrays commonly repeat them, but that
        # must not turn an allowlisted district or postal label into a global
        # exact-address fingerprint.
        if candidate.casefold() in coarse_location_values:
            return
        if len(candidate) < 8 or not any(character.isalpha() for character in candidate):
            return
        words = re.findall(r"[^\W_]+", candidate, flags=re.UNICODE)
        if len(words) < 2 and len(candidate) < 12:
            return
        fingerprints.add(candidate)

    def _collect(value: object) -> None:
        if len(fingerprints) >= _PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_LIMIT:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                compact_key = re.sub(
                    r"[^a-z0-9]+",
                    "",
                    str(key or "").strip().lower(),
                )
                if compact_key in _PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_KEYS:
                    _add(child)
                _collect(child)
                if len(fingerprints) >= _PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_LIMIT:
                    break
        elif isinstance(value, (list, tuple)):
            for child in value:
                _collect(child)
                if len(fingerprints) >= _PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_LIMIT:
                    break

    _collect(private_tour_exact_location_snapshot(payload))
    return tuple(
        sorted(fingerprints, key=lambda item: (-len(item), item.casefold()))[
            :_PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_LIMIT
        ]
    )


def _public_tour_exact_location_pattern(fingerprint: str) -> re.Pattern[str]:
    normalized = str(fingerprint or "").strip()[
        :_PUBLIC_TOUR_EXACT_LOCATION_FINGERPRINT_MAX_CHARS
    ]
    tokens = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    if not tokens:
        return re.compile(re.escape(normalized), flags=re.IGNORECASE)
    token_pattern = r"[^\w]+".join(re.escape(token) for token in tokens)
    return re.compile(
        rf"(?<!\w){token_pattern}(?!\w)",
        flags=re.IGNORECASE,
    )


def _public_tour_exact_location_key_is_pathlike(key: object) -> bool:
    normalized_key = str(key or "").strip().lower().replace("-", "_")
    return any(marker in normalized_key for marker in ("path", "relpath", "url"))


def _public_tour_exact_location_string_fallback(*, key: object, value: str) -> str:
    normalized_key = str(key or "").strip().lower().replace("-", "_")
    if normalized_key in {"title", "tour_title", "display_title"}:
        return "Property tour"
    if normalized_key.endswith("_id") or normalized_key in {
        "id",
        "location_id",
        "scene_id",
    }:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"public-location-{digest}"
    if _public_tour_exact_location_key_is_pathlike(normalized_key):
        return ""
    if normalized_key in {"name", "label", "scene"}:
        return "Property view"
    if normalized_key == "role":
        return "detail"
    return "Property detail"


def scrub_public_tour_exact_location_strings(
    value: object,
    *,
    fingerprints: tuple[str, ...],
    key: object = "",
) -> object:
    """Recursively remove exact-address fingerprints from public string values."""

    if isinstance(value, dict):
        return {
            str(child_key): scrub_public_tour_exact_location_strings(
                child,
                fingerprints=fingerprints,
                key=child_key,
            )
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [
            scrub_public_tour_exact_location_strings(
                child,
                fingerprints=fingerprints,
                key=key,
            )
            for child in value
        ]
    if isinstance(value, tuple):
        return [
            scrub_public_tour_exact_location_strings(
                child,
                fingerprints=fingerprints,
                key=key,
            )
            for child in value
        ]
    if not isinstance(value, str) or not fingerprints:
        return value

    cleaned = value
    matched = False
    for fingerprint in fingerprints:
        pattern = _public_tour_exact_location_pattern(fingerprint)
        if pattern.search(cleaned):
            matched = True
            if _public_tour_exact_location_key_is_pathlike(key):
                return ""
            cleaned = pattern.sub(" ", cleaned)
    if not matched:
        return value
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\r\n-\u2013\u2014,:;|/()[]")
    if (
        len(cleaned) >= 3
        and any(character.isalpha() for character in cleaned)
        and not any(
            _public_tour_exact_location_pattern(fingerprint).search(cleaned)
            for fingerprint in fingerprints
        )
    ):
        return cleaned
    return _public_tour_exact_location_string_fallback(
        key=key,
        value=value,
    )


def public_tour_safe_asset_relpath(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if (
        not raw
        or "\x00" in raw
        or "://" in raw
        or raw.startswith("/")
        or any(character in _PUBLIC_TOUR_URI_DELIMITERS for character in raw)
    ):
        return ""
    path = PurePosixPath(raw)
    if path.is_absolute() or any(
        part in {"", ".", ".."} or part.startswith(".")
        for part in path.parts
    ):
        return ""
    lowered_parts = tuple(part.lower() for part in path.parts)
    lowered_name = lowered_parts[-1]
    if (
        lowered_parts[0] in _PUBLIC_TOUR_QUARANTINED_ASSET_ROOTS
        or lowered_name in _PUBLIC_TOUR_QUARANTINED_ASSET_NAMES
        or lowered_name.startswith("tour.magicfit.")
        or lowered_name.endswith(".magicfit.json")
        or ".magicfit.pending." in lowered_name
    ):
        return ""
    return "/".join(path.parts)


def public_tour_safe_slug(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not _PUBLIC_TOUR_SLUG_PATTERN.fullmatch(normalized)
        or ".." in normalized
        or normalized.startswith(_PUBLIC_TOUR_RESERVED_STAGE_PREFIX)
    ):
        return ""
    return normalized


def public_tour_env_truthy(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def public_tour_privacy_mode(payload: dict[str, object]) -> str:
    raw = str(payload.get("tour_privacy_mode") or payload.get("privacy_mode") or "").strip().lower()
    return raw if raw in _PUBLIC_TOUR_PRIVACY_MODES else "anonymous_public"


def require_public_tour_viewable(payload: dict[str, object]) -> None:
    # Legacy manifests predate publication_status and remain viewable.  Once a
    # producer opts into the field, however, only an explicit terminal public
    # state is safe; unknown values must never become public by omission from a
    # denylist.
    if "publication_status" in payload:
        publication_status = payload.get("publication_status")
        normalized_publication_status = (
            publication_status.strip().lower()
            if isinstance(publication_status, str)
            else ""
        )
        if normalized_publication_status not in _PUBLIC_TOUR_READY_PUBLICATION_STATUSES:
            raise HTTPException(status_code=404, detail="tour_not_found")

    # Suppress manifests written by the pre-activation MagicFit importer.  New
    # imports leave the active manifest untouched until delivery acceptance,
    # but this keeps already-staged/pending manifests fail-closed during an
    # upgrade and does not reinterpret legacy manifests without an import
    # contract.
    magicfit_import = payload.get("magicfit_import")
    if isinstance(magicfit_import, Mapping):
        proof_status = magicfit_import.get("proof_status")
        normalized_proof_status = (
            proof_status.strip().lower() if isinstance(proof_status, str) else ""
        )
        if normalized_proof_status in _PUBLIC_TOUR_PENDING_MAGICFIT_PROOF_STATUSES:
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
        expected_role = (
            "generated_reconstruction_material"
            if suffix == ".mtl"
            else "generated_reconstruction_model"
        )
        expected_mime_type = {
            ".obj": "model/obj",
            ".mtl": "model/mtl",
            ".glb": "model/gltf-binary",
        }[suffix]
        return (
            is_generated_reconstruction_asset
            and normalized_role == expected_role
            and normalized_role in _PUBLIC_TOUR_GENERATED_RECONSTRUCTION_MODEL_ROLES
            and str(mime_type or "").strip().lower() == expected_mime_type
        )
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


def _public_tour_explicit_model_binding_is_valid(row: dict[str, object]) -> bool:
    declared_paths: set[str] = set()
    for key in ("path", "relpath", "asset_relpath"):
        raw_value = str(row.get(key) or "").strip()
        if not raw_value:
            continue
        candidate = public_tour_safe_asset_relpath(raw_value)
        if not candidate:
            return False
        declared_paths.add(candidate)
    if len(declared_paths) != 1:
        return False
    relpath = next(iter(declared_paths))
    suffix = PurePosixPath(relpath).suffix.lower()
    if suffix not in {".obj", ".mtl", ".glb"}:
        return True
    privacy_class = str(
        row.get("privacy_class") or row.get("privacy") or ""
    ).strip().lower()
    role = str(row.get("role") or row.get("asset_role") or "").strip().lower()
    role = role.replace("-", "_")
    expected_role = (
        "generated_reconstruction_material"
        if suffix == ".mtl"
        else "generated_reconstruction_model"
    )
    expected_mime_type = {
        ".obj": "model/obj",
        ".mtl": "model/mtl",
        ".glb": "model/gltf-binary",
    }[suffix]
    privacy_aliases = {
        str(row.get(key) or "").strip().lower()
        for key in ("privacy_class", "privacy")
        if str(row.get(key) or "").strip()
    }
    role_aliases = {
        str(row.get(key) or "").strip().lower().replace("-", "_")
        for key in ("role", "asset_role")
        if str(row.get(key) or "").strip()
    }
    mime_aliases = {
        str(row.get(key) or "").strip().lower()
        for key in ("mime_type", "content_type")
        if str(row.get(key) or "").strip()
    }
    digest = str(row.get("sha256") or "").strip()
    size_bytes = row.get("size_bytes")
    return (
        privacy_class == "generated_reconstruction_public"
        and privacy_aliases == {"generated_reconstruction_public"}
        and role == expected_role
        and role_aliases == {expected_role}
        and str(row.get("mime_type") or row.get("content_type") or "")
        .strip()
        .lower()
        == expected_mime_type
        and mime_aliases == {expected_mime_type}
        and bool(re.fullmatch(r"[0-9a-f]{64}", digest))
        and not isinstance(size_bytes, bool)
        and isinstance(size_bytes, int)
        and 0 < size_bytes <= 8 * 1024 * 1024
    )


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
    scene_rows = [scene for scene in list(payload.get("scenes") or []) if isinstance(scene, dict)]
    walkable_scene = payload.get("walkable_scene")
    if isinstance(walkable_scene, dict):
        _add(walkable_scene.get("floorplan_relpath"), privacy_class="public", role="floorplan")
        _add(walkable_scene.get("derived_floorplan_relpath"), privacy_class="public", role="derived_floorplan")
        walkable_scenes = walkable_scene.get("scenes")
        if isinstance(walkable_scenes, dict):
            scene_rows.extend(scene for scene in walkable_scenes.values() if isinstance(scene, dict))
        elif isinstance(walkable_scenes, list):
            scene_rows.extend(scene for scene in walkable_scenes if isinstance(scene, dict))
    for scene in scene_rows:
        if not isinstance(scene, dict):
            continue
        scene_privacy = str(scene.get("privacy_class") or scene.get("privacy") or "").strip()
        scene_role = str(scene.get("role") or "").strip()
        scene_mime = str(scene.get("mime_type") or "").strip()
        for key in (
            "asset_relpath",
            "panorama_relpath",
            "equirect_relpath",
            "image_relpath",
            "thumbnail_relpath",
            "preview_relpath",
            "floorplan_relpath",
        ):
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
            if not _public_tour_explicit_model_binding_is_valid(row):
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
    scene_rows = [scene for scene in list(payload.get("scenes") or []) if isinstance(scene, dict)]
    walkable_scene = payload.get("walkable_scene")
    if isinstance(walkable_scene, dict):
        _record(walkable_scene.get("floorplan_relpath"), privacy_class="public", role="floorplan")
        _record(walkable_scene.get("derived_floorplan_relpath"), privacy_class="public", role="derived_floorplan")
        walkable_scenes = walkable_scene.get("scenes")
        if isinstance(walkable_scenes, dict):
            scene_rows.extend(scene for scene in walkable_scenes.values() if isinstance(scene, dict))
        elif isinstance(walkable_scenes, list):
            scene_rows.extend(scene for scene in walkable_scenes if isinstance(scene, dict))
    for scene in scene_rows:
        if not isinstance(scene, dict):
            continue
        scene_privacy = str(scene.get("privacy_class") or scene.get("privacy") or "").strip()
        scene_role = str(scene.get("role") or "").strip()
        scene_mime = str(scene.get("mime_type") or "").strip()
        for key in (
            "asset_relpath",
            "panorama_relpath",
            "equirect_relpath",
            "image_relpath",
            "thumbnail_relpath",
            "preview_relpath",
            "floorplan_relpath",
        ):
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
            if not _public_tour_explicit_model_binding_is_valid(row):
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
    normalized_slug = public_tour_safe_slug(slug)
    safe_relpath = public_tour_safe_asset_relpath(relpath)
    if not normalized_slug or not safe_relpath:
        return ""
    from urllib.parse import quote

    encoded_slug = quote(normalized_slug, safe="-._~")
    encoded_relpath = "/".join(
        quote(part, safe="-._~") for part in PurePosixPath(safe_relpath).parts
    )
    return f"/tours/files/{encoded_slug}/{encoded_relpath}"


def public_tour_canonical_path(slug: str) -> str:
    normalized_slug = public_tour_safe_slug(slug)
    if not normalized_slug:
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


def public_tour_google_maps_context_url(value: object) -> str:
    """Allow only a Google-hosted Maps/Street View context navigation URL."""

    normalized = public_tour_safe_http_url(value)
    if not normalized:
        return ""
    from urllib.parse import urlparse

    parsed = urlparse(normalized)
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if hostname not in _PUBLIC_TOUR_STREET_CONTEXT_HOSTS:
        return ""
    if not parsed.path.startswith(("/maps", "/@")):
        return ""
    return normalized


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
            if public_tour_key_is_private(key) or public_tour_key_is_exact_location(key):
                continue
            redacted[str(key)] = redact_public_tour_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_public_tour_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_public_tour_value(item) for item in value]
    return value


def redacted_public_tour_generated_reconstruction(
    payload: dict[str, object],
    *,
    expose_asset_relpaths: bool,
) -> dict[str, object]:
    source = payload.get("generated_reconstruction")
    if not isinstance(source, dict):
        return {}
    if (
        str(source.get("provider") or "").strip().lower()
        != "propertyquarry_generated_reconstruction"
        or source.get("verified_provider_capture") is not False
        or source.get("satisfies_verified_tour_gate") is not False
    ):
        return {}

    allowed_assets = public_tour_allowed_asset_paths(payload)
    rendered: dict[str, object] = {}
    for key in _PUBLIC_TOUR_GENERATED_RECONSTRUCTION_KEYS:
        if key not in source or public_tour_key_is_private(key):
            continue
        if key in _PUBLIC_TOUR_GENERATED_RECONSTRUCTION_PUBLIC_ASSET_KEYS:
            relpath = public_tour_safe_asset_relpath(source.get(key))
            if not relpath or relpath not in allowed_assets:
                continue
            if expose_asset_relpaths:
                rendered[key] = relpath
            else:
                rendered[key.replace("_relpath", "_url")] = public_tour_file_url(
                    str(payload.get("slug") or "").strip(),
                    relpath,
                )
            continue
        if key in _PUBLIC_TOUR_GENERATED_RECONSTRUCTION_INTERNAL_ASSET_KEYS:
            if not expose_asset_relpaths:
                continue
            relpath = public_tour_safe_asset_relpath(source.get(key))
            if (
                relpath
                and relpath.startswith(_PUBLIC_TOUR_GENERATED_RECONSTRUCTION_PREFIX)
                and PurePosixPath(relpath).suffix.lower() == ".json"
            ):
                rendered[key] = relpath
            continue
        if key == "photo_relpaths":
            photo_relpaths = [
                relpath
                for relpath in (
                    public_tour_safe_asset_relpath(value)
                    for value in list(source.get(key) or [])
                )
                if relpath and relpath in allowed_assets
            ][:64]
            if expose_asset_relpaths:
                rendered[key] = photo_relpaths
            else:
                rendered["photo_urls"] = [
                    public_tour_file_url(
                        str(payload.get("slug") or "").strip(),
                        relpath,
                    )
                    for relpath in photo_relpaths
                ]
            continue
        rendered[key] = redact_public_tour_value(source.get(key))
    return rendered


def public_tour_anonymous_uniqueness_risk(
    payload: dict[str, object],
    facts: dict[str, object],
) -> bool:
    """Return whether an anonymous fact combination can identify one listing.

    The filter deliberately requires both a precision pair (area plus floor)
    and a rarity signal. Producers can provide an explicit risk label/flag;
    small-municipality population and high purchase/rent values are conservative
    built-in signals when a producer has not classified the listing.
    """

    source = dict(facts or {})

    def _truthy(value: object) -> bool:
        return value is True or str(value or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _number(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            return None
        return candidate if math.isfinite(candidate) else None

    area = _number(source.get("area_sqm"))
    floor = source.get("floor")
    precise_pair = area is not None and area > 0 and bool(str(floor or "").strip())
    if not precise_pair:
        return False

    risk_label = str(
        payload.get("public_uniqueness_risk")
        or source.get("uniqueness_risk")
        or ""
    ).strip().lower()
    explicit_risk = (
        risk_label in _PUBLIC_TOUR_UNIQUENESS_RISK_LABELS
        or _truthy(payload.get("rare_listing"))
        or _truthy(source.get("rare_listing"))
        or _truthy(payload.get("small_municipality"))
        or _truthy(source.get("small_municipality"))
    )
    municipality_population = _number(
        payload.get("municipality_population")
        or source.get("municipality_population")
    )
    small_municipality = (
        municipality_population is not None
        and 0 < municipality_population <= _PUBLIC_TOUR_SMALL_MUNICIPALITY_POPULATION
    )
    purchase_price = _number(
        source.get("purchase_price_eur") or source.get("price_eur")
    )
    monthly_rent = _number(source.get("total_rent_eur"))
    high_price = (
        purchase_price is not None
        and purchase_price >= _PUBLIC_TOUR_HIGH_PURCHASE_PRICE_EUR
    ) or (
        monthly_rent is not None
        and monthly_rent >= _PUBLIC_TOUR_HIGH_MONTHLY_RENT_EUR
    )
    return explicit_risk or small_municipality or high_price


def redacted_public_tour_facts(
    payload: dict[str, object],
    facts: dict[str, object],
    *,
    privacy_mode: str,
) -> dict[str, object]:
    redacted_value = redact_public_tour_value(facts if isinstance(facts, dict) else {})
    redacted = dict(redacted_value) if isinstance(redacted_value, dict) else {}
    for key in list(redacted.keys()):
        if public_tour_key_is_exact_location(key):
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
    suppress_uniqueness_facts = public_tour_anonymous_uniqueness_risk(
        payload,
        redacted,
    )
    for key, value in redacted.items():
        normalized_key = str(key or "").strip().lower()
        if normalized_key in _PUBLIC_TOUR_EXACT_LOCATION_FACT_KEYS:
            continue
        if (
            suppress_uniqueness_facts
            and normalized_key in _PUBLIC_TOUR_UNIQUENESS_SENSITIVE_FACT_KEYS
        ):
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
    if suppress_uniqueness_facts:
        public_facts["public_fact_precision"] = "reduced_for_uniqueness"
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
            if key == "street_view_url":
                safe_url = public_tour_google_maps_context_url(value)
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


def redacted_public_tour_walkable_scene(
    payload: dict[str, object],
    *,
    expose_asset_relpaths: bool,
) -> dict[str, object]:
    """Project only viewer-safe panorama fields from a walkable scene.

    The publication manifest also carries immutable provenance and browser-proof
    receipts.  Those fields are required server-side, but they must never be
    serialized into public tour JSON or page source.
    """

    walkable_scene = payload.get("walkable_scene")
    if not isinstance(walkable_scene, dict):
        return {}
    slug = str(payload.get("slug") or "").strip()
    allowed_assets = public_tour_allowed_asset_paths(payload)
    asset_digests = (
        payload.get("_ai_panorama_asset_sha256")
        if isinstance(payload.get("_ai_panorama_asset_sha256"), dict)
        else {}
    )

    def _versioned_file_url(relpath: str) -> str:
        url = public_tour_file_url(slug, relpath)
        digest = str(asset_digests.get(relpath) or "").strip().lower()
        return f"{url}?v={digest}" if re.fullmatch(r"[0-9a-f]{64}", digest) else url

    def _bounded_number(
        value: object,
        *,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        if not math.isfinite(parsed):
            parsed = default
        return max(minimum, min(maximum, parsed))

    raw_scenes = walkable_scene.get("scenes")
    if isinstance(raw_scenes, dict):
        scene_rows = [
            (str(key or "").strip(), dict(value))
            for key, value in raw_scenes.items()
            if isinstance(value, dict)
        ]
    elif isinstance(raw_scenes, list):
        scene_rows = [
            (str(index + 1), dict(value))
            for index, value in enumerate(raw_scenes)
            if isinstance(value, dict)
        ]
    else:
        scene_rows = []

    normalized_rows: list[tuple[dict[str, object], dict[str, object]]] = []
    seen_ids: set[str] = set()
    for index, (fallback_id, scene) in enumerate(scene_rows[:64]):
        scene_id = str(
            scene.get("id")
            or scene.get("node_id")
            or scene.get("scene_id")
            or fallback_id
            or f"scene-{index + 1}"
        ).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", scene_id) or scene_id in seen_ids:
            continue
        projection = str(
            scene.get("projection") or scene.get("type") or "equirectangular"
        ).strip().lower()
        if projection not in {
            "equirectangular",
            "equirect",
            "panorama",
            "spherical",
            "360",
        }:
            continue
        relpath = ""
        for key in (
            "asset_relpath",
            "panorama_relpath",
            "equirect_relpath",
            "image_relpath",
        ):
            candidate = public_tour_safe_asset_relpath(scene.get(key))
            if candidate and candidate in allowed_assets:
                relpath = candidate
                break
        if not relpath:
            continue
        rendered: dict[str, object] = {
            "id": scene_id,
            "label": str(
                scene.get("label")
                or scene.get("name")
                or scene.get("room")
                or f"Space {index + 1}"
            ).strip()[:80],
            "projection": "equirectangular",
            "start_yaw": _bounded_number(
                scene.get("start_yaw") or scene.get("start_deg"),
                default=0.0,
                minimum=-360.0,
                maximum=360.0,
            ),
            "start_pitch": _bounded_number(
                scene.get("start_pitch"),
                default=0.0,
                minimum=-80.0,
                maximum=80.0,
            ),
            "start_fov": _bounded_number(
                scene.get("start_fov"),
                default=72.0,
                minimum=40.0,
                maximum=100.0,
            ),
            "floorplan_x_pct": _bounded_number(
                scene.get("floorplan_x_pct"),
                default=-1.0,
                minimum=-1.0,
                maximum=100.0,
            ),
            "floorplan_y_pct": _bounded_number(
                scene.get("floorplan_y_pct"),
                default=-1.0,
                minimum=-1.0,
                maximum=100.0,
            ),
        }
        if expose_asset_relpaths:
            rendered["asset_relpath"] = relpath
        else:
            rendered["image_url"] = _versioned_file_url(relpath)
        street_view_url = public_tour_google_maps_context_url(scene.get("street_view_url"))
        if street_view_url:
            rendered["street_view_url"] = street_view_url
        seen_ids.add(scene_id)
        normalized_rows.append((rendered, scene))

    if not normalized_rows:
        return {}
    scene_ids = {str(row[0]["id"]) for row in normalized_rows}
    for rendered, source in normalized_rows:
        hotspots: list[dict[str, object]] = []
        for key in ("hotspots", "transitions", "links"):
            raw_hotspots = source.get(key)
            if not isinstance(raw_hotspots, list):
                continue
            for raw_hotspot in raw_hotspots[:64]:
                if not isinstance(raw_hotspot, dict):
                    continue
                target = str(
                    raw_hotspot.get("target")
                    or raw_hotspot.get("target_scene_id")
                    or raw_hotspot.get("target_node_id")
                    or raw_hotspot.get("target_scene")
                    or raw_hotspot.get("scene")
                    or ""
                ).strip()
                if target not in scene_ids or target == rendered["id"]:
                    continue
                hotspots.append(
                    {
                        "target": target,
                        "label": str(
                            raw_hotspot.get("label")
                            or raw_hotspot.get("title")
                            or "Continue"
                        ).strip()[:80],
                        "yaw": _bounded_number(
                            raw_hotspot.get("yaw") or raw_hotspot.get("yaw_deg"),
                            default=0.0,
                            minimum=-360.0,
                            maximum=360.0,
                        ),
                        "pitch": _bounded_number(
                            raw_hotspot.get("pitch") or raw_hotspot.get("pitch_deg"),
                            default=-12.0,
                            minimum=-80.0,
                            maximum=80.0,
                        ),
                    }
                )
        rendered["hotspots"] = hotspots

    rendered_walkable: dict[str, object] = {
        "representation_kind": str(
            walkable_scene.get("representation_kind") or ""
        ).strip().lower()[:80],
        "representation_disclosure": str(
            walkable_scene.get("representation_disclosure") or ""
        ).strip()[:500],
        "initial_scene_id": str(
            walkable_scene.get("initial_scene_id") or normalized_rows[0][0]["id"]
        ).strip()[:80],
        "scenes": [row[0] for row in normalized_rows],
    }
    raw_spatial_model = walkable_scene.get("spatial_model")
    if isinstance(raw_spatial_model, dict):
        raw_rooms = raw_spatial_model.get("rooms")
        rendered_rooms: list[dict[str, object]] = []
        seen_room_ids: set[str] = set()
        if isinstance(raw_rooms, list):
            for index, raw_room in enumerate(raw_rooms[:64]):
                if not isinstance(raw_room, dict):
                    continue
                room_id = str(raw_room.get("id") or f"room-{index + 1}").strip()
                if (
                    not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", room_id)
                    or room_id in seen_room_ids
                ):
                    continue
                width = _bounded_number(
                    raw_room.get("width"),
                    default=0.0,
                    minimum=0.0,
                    maximum=40.0,
                )
                depth = _bounded_number(
                    raw_room.get("depth"),
                    default=0.0,
                    minimum=0.0,
                    maximum=40.0,
                )
                if width < 0.2 or depth < 0.2:
                    continue
                scene_id = str(raw_room.get("scene_id") or "").strip()
                if scene_id not in scene_ids:
                    scene_id = ""
                kind = str(raw_room.get("kind") or "interior").strip().lower()
                if kind not in {"interior", "exterior", "unavailable"}:
                    kind = "interior"
                seen_room_ids.add(room_id)
                rendered_room = {
                        "id": room_id,
                        "label": str(
                            raw_room.get("label")
                            or raw_room.get("name")
                            or f"Space {index + 1}"
                        ).strip()[:80],
                        "scene_id": scene_id,
                        "kind": kind,
                        "x": _bounded_number(
                            raw_room.get("x"),
                            default=0.0,
                            minimum=-40.0,
                            maximum=40.0,
                        ),
                        "z": _bounded_number(
                            raw_room.get("z"),
                            default=0.0,
                            minimum=-40.0,
                            maximum=40.0,
                        ),
                        "width": width,
                        "depth": depth,
                        "height": _bounded_number(
                            raw_room.get("height"),
                            default=2.55,
                            minimum=1.8,
                            maximum=6.0,
                        ),
                    }
                dimension_label = str(raw_room.get("dimension_label") or "").strip()[:80]
                if dimension_label:
                    rendered_room["dimension_label"] = dimension_label
                raw_measurement = raw_room.get("measurement")
                if isinstance(raw_measurement, dict):
                    raw_components = raw_measurement.get("components")
                    rendered_components: list[dict[str, float]] = []
                    if isinstance(raw_components, list):
                        for raw_component in raw_components[:16]:
                            if not isinstance(raw_component, dict):
                                continue
                            component_width = _bounded_number(
                                raw_component.get("width"),
                                default=0.0,
                                minimum=0.0,
                                maximum=40.0,
                            )
                            component_depth = _bounded_number(
                                raw_component.get("depth"),
                                default=0.0,
                                minimum=0.0,
                                maximum=40.0,
                            )
                            if component_width < 0.2 or component_depth < 0.2:
                                continue
                            rendered_components.append(
                                {
                                    "x": _bounded_number(raw_component.get("x"), default=0.0, minimum=-40.0, maximum=40.0),
                                    "z": _bounded_number(raw_component.get("z"), default=0.0, minimum=-40.0, maximum=40.0),
                                    "width": component_width,
                                    "depth": component_depth,
                                }
                            )
                    if rendered_components:
                        rendered_room["measurement"] = {
                            "contract_name": str(raw_measurement.get("contract_name") or "").strip()[:120],
                            "source": str(raw_measurement.get("source") or "").strip()[:120],
                            "area_m2": _bounded_number(raw_measurement.get("area_m2"), default=0.0, minimum=0.0, maximum=1000.0),
                            "components": rendered_components,
                        }
                rendered_rooms.append(rendered_room)
        if (
            str(raw_spatial_model.get("source_basis") or "").strip().lower()
            in {
                "floorplan_scaled_approximation",
                "floorplan_measured_dimensions",
                "floorplan_analyzer_reviewed_dimensions",
            }
            and raw_spatial_model.get("measured") in {False, True}
            and rendered_rooms
        ):
            rendered_walkable["spatial_model"] = {
                "source_basis": str(raw_spatial_model.get("source_basis") or "").strip().lower(),
                "measured": raw_spatial_model.get("measured") is True,
                "rooms": rendered_rooms,
            }
            analyzer_contract_name = str(raw_spatial_model.get("analyzer_contract_name") or "").strip()
            if analyzer_contract_name:
                rendered_walkable["spatial_model"]["analyzer_contract_name"] = analyzer_contract_name
    floorplan_relpath = public_tour_safe_asset_relpath(
        walkable_scene.get("floorplan_relpath")
    )
    if floorplan_relpath and floorplan_relpath in allowed_assets:
        if expose_asset_relpaths:
            rendered_walkable["floorplan_relpath"] = floorplan_relpath
        else:
            rendered_walkable["floorplan_url"] = _versioned_file_url(floorplan_relpath)
    derived_floorplan_relpath = public_tour_safe_asset_relpath(
        walkable_scene.get("derived_floorplan_relpath")
    )
    if derived_floorplan_relpath and derived_floorplan_relpath in allowed_assets:
        if expose_asset_relpaths:
            rendered_walkable["derived_floorplan_relpath"] = derived_floorplan_relpath
        else:
            rendered_walkable["derived_floorplan_url"] = _versioned_file_url(derived_floorplan_relpath)
    return rendered_walkable


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
        if key == "walkable_scene":
            rendered_walkable_scene = redacted_public_tour_walkable_scene(
                payload,
                expose_asset_relpaths=expose_asset_relpaths,
            )
            if rendered_walkable_scene:
                rendered[key] = rendered_walkable_scene
            continue
        if key == "generated_reconstruction":
            generated_reconstruction = redacted_public_tour_generated_reconstruction(
                payload,
                expose_asset_relpaths=expose_asset_relpaths,
            )
            if generated_reconstruction:
                rendered[key] = generated_reconstruction
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
        if key == "video_sidecar_relpath":
            if not expose_asset_relpaths:
                continue
            relpath = public_tour_safe_asset_relpath(payload.get(key))
            if (
                relpath
                and relpath.startswith(_PUBLIC_TOUR_GENERATED_RECONSTRUCTION_PREFIX)
                and PurePosixPath(relpath).suffix.lower() == ".json"
            ):
                rendered[key] = relpath
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
        if key == "property_url_sha256":
            digest = str(payload.get(key) or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                rendered[key] = digest
            continue
        rendered[key] = redact_public_tour_value(payload.get(key))
    rendered["slug"] = slug
    rendered["tour_privacy_mode"] = privacy_mode
    rendered.setdefault("facts", {})
    rendered.setdefault("scenes", [])
    asset_manifest = list(
        public_tour_manifest(
            payload,
            bundle_dir_resolver=bundle_dir_resolver,
        ).values()
    )
    if expose_asset_relpaths:
        if (
            payload.get("public_assets") is not None
            or payload.get("generated_reconstruction") is not None
        ):
            rendered["public_assets"] = [
                {key: value for key, value in row.items() if key != "url"}
                for row in asset_manifest
            ]
    else:
        rendered["public_assets"] = asset_manifest
    fingerprints = public_tour_exact_location_string_fingerprints(payload)
    scrubbed = scrub_public_tour_exact_location_strings(
        rendered,
        fingerprints=fingerprints,
    )
    return dict(scrubbed) if isinstance(scrubbed, dict) else {}


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


def canonical_public_tour_payload(
    payload: dict[str, object],
    *,
    bundle_dir: Path,
) -> dict[str, object]:
    current = dict(payload or {})
    for _attempt in range(4):
        slug = str(current.get("slug") or "").strip()
        canonical = build_public_tour_manifest(
            current,
            expose_asset_relpaths=True,
            url_allowed=lambda _url: False,
            bundle_dir_resolver=lambda requested_slug: (
                bundle_dir
                if str(requested_slug or "").strip() == slug
                else None
            ),
        ).as_dict()
        if canonical == current:
            return canonical
        current = canonical
    raise ValueError("public_manifest_canonicalization_did_not_converge")
