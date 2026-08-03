from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Mapping, Sequence

from app.property_distance_preferences import (
    normalize_property_distance_importance as _normalize_property_distance_importance,
    property_distance_importance_key as _property_distance_importance_key,
    property_distance_preference_keys as _property_distance_preference_keys,
)


PROPERTY_FACT_ENRICHMENT_SCHEMA_VERSION = "propertyquarry.fact-enrichment.v1"
PROPERTY_FACT_SCORE_ALGORITHM_VERSION = "propertyquarry.fact-score-state.v1"
PROPERTY_FACT_GEO_BUNDLE_KIND = "optional-geo-v1"
PROPERTY_FACT_REQUIRED_GEO_BUNDLE_KIND = "required-geo-v1"
PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION = "propertyquarry.provider-fact-attestation.v1"

_PROPERTY_FACT_PROVIDER_LABELS = MappingProxyType(
    {
        "openstreetmap_overpass": "OpenStreetMap nearby places",
        "schoolatlas": "official school atlas",
        "quality_verified_cafe_source": "quality-verified café source",
    }
)


def _distance_fact_spec(
    *,
    key: str,
    aliases: Sequence[str],
    label: str,
    search_label: str,
    preference_keys: Sequence[str] = (),
    poi_keys: Sequence[str] = (),
    provider: str = "openstreetmap_overpass",
    evidence_providers: Sequence[str] = (),
    evidence_source_keys_by_provider: Mapping[str, Sequence[str]] | None = None,
    strict_evidence_provider: bool = False,
    default_lazy: bool = False,
    search_supported: bool = True,
) -> Mapping[str, object]:
    """Build one immutable row in the shared distance-fact registry."""
    normalized_provider = str(provider).strip()
    normalized_aliases = tuple(
        dict.fromkeys((key, *(str(value) for value in aliases)))
    )
    normalized_preferences = tuple(str(value) for value in preference_keys)
    required_preferences = tuple(
        value for value in normalized_preferences if value.startswith("max_distance_to_")
    )
    default_evidence_providers = (
        (normalized_provider, "google_maps_browseract")
        if normalized_provider == "openstreetmap_overpass"
        and not strict_evidence_provider
        else (normalized_provider,)
    )
    normalized_evidence_providers = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in (evidence_providers or default_evidence_providers)
            if str(value).strip()
        )
    )
    default_source_keys_by_provider: dict[str, Sequence[str]] = {
        normalized_provider: normalized_aliases,
    }
    if "google_maps_browseract" in normalized_evidence_providers:
        default_source_keys_by_provider["google_maps_browseract"] = normalized_aliases
    normalized_source_keys_by_provider = {
        str(provider_key): tuple(str(value) for value in source_keys)
        for provider_key, source_keys in dict(
            evidence_source_keys_by_provider
            or default_source_keys_by_provider
        ).items()
    }
    return MappingProxyType(
        {
            "key": key,
            "aliases": normalized_aliases,
            "label": label,
            "search_label": search_label,
            "preference_keys": normalized_preferences,
            "required_preference_keys": required_preferences,
            "poi_keys": tuple(str(value) for value in poi_keys),
            "provider": normalized_provider,
            "evidence_providers": normalized_evidence_providers,
            "evidence_source_keys_by_provider": MappingProxyType(
                normalized_source_keys_by_provider
            ),
            "strict_evidence_provider": bool(strict_evidence_provider),
            "default_lazy": bool(default_lazy),
            "search_supported": bool(search_supported),
        }
    )


# Canonical distance-fact registry shared by detail enrichment and search-time
# gating. Keep provider keys here so a new distance preference cannot silently
# become rankable without a corresponding fact source.
PROPERTY_FACT_DISTANCE_SPECS: tuple[Mapping[str, object], ...] = (
    _distance_fact_spec(
        key="nearest_supermarket_m",
        aliases=("distance_supermarket_m",),
        label="Supermarket distance",
        search_label="supermarket",
        preference_keys=("max_distance_to_supermarket_m", "prefer_supermarket_nearby"),
        poi_keys=("shop=supermarket", "shop=convenience", "shop=greengrocer"),
        default_lazy=True,
    ),
    _distance_fact_spec(
        key="nearest_playground_m",
        aliases=("distance_playground_m",),
        label="Playground distance",
        search_label="playground",
        preference_keys=("max_distance_to_playground_m", "prefer_playgrounds_nearby"),
        poi_keys=("leisure=playground",),
        default_lazy=True,
    ),
    _distance_fact_spec(
        key="nearest_pharmacy_m",
        aliases=("distance_pharmacy_m",),
        label="Pharmacy distance",
        search_label="pharmacy",
        preference_keys=("max_distance_to_pharmacy_m", "prefer_pharmacy_nearby"),
        poi_keys=("amenity=pharmacy",),
        default_lazy=True,
    ),
    _distance_fact_spec(
        key="nearest_medical_care_m",
        aliases=("distance_medical_care_m",),
        label="Medical-care distance",
        search_label="medical care",
        preference_keys=("max_distance_to_medical_care_m", "prefer_medical_care_nearby"),
        poi_keys=("amenity=doctors", "amenity=clinic", "amenity=hospital"),
        default_lazy=True,
    ),
    _distance_fact_spec(
        key="nearest_subway_m",
        aliases=("nearest_transit_m", "distance_underground_m", "distance_subway_m"),
        label="Underground distance",
        search_label="underground",
        preference_keys=(
            "max_distance_to_subway_m",
            "max_distance_to_underground_m",
            "prefer_subway_nearby",
        ),
        poi_keys=("railway=subway_entrance",),
        default_lazy=True,
    ),
    _distance_fact_spec(
        key="nearest_university_m",
        aliases=("distance_university_m",),
        label="University distance",
        search_label="university",
        preference_keys=("max_distance_to_university_m",),
        poi_keys=("amenity=university",),
    ),
    _distance_fact_spec(
        key="nearest_kindergarten_m",
        aliases=("nearest_school_m", "distance_kindergarten_m"),
        label="Kindergarten distance",
        search_label="kindergarten",
        preference_keys=("max_distance_to_kindergarten_m",),
        poi_keys=("amenity=kindergarten", "schoolatlas=kindergarten"),
        provider="schoolatlas",
        evidence_providers=("schoolatlas", "openstreetmap_overpass"),
        evidence_source_keys_by_provider={
            "schoolatlas": (
                "nearest_kindergarten_m",
                "distance_kindergarten_m",
            ),
            "openstreetmap_overpass": ("nearest_kindergarten_m",),
        },
        strict_evidence_provider=True,
    ),
    _distance_fact_spec(
        key="nearest_full_day_primary_school_m",
        aliases=("nearest_school_m", "distance_full_day_primary_school_m"),
        label="Full-day primary-school distance",
        search_label="full-day primary school",
        preference_keys=("max_distance_to_ganztags_volksschule_m",),
        poi_keys=("schoolatlas=full_day_primary_school",),
        provider="schoolatlas",
        evidence_providers=("schoolatlas",),
        evidence_source_keys_by_provider={
            "schoolatlas": (
                "nearest_full_day_primary_school_m",
                "distance_full_day_primary_school_m",
            ),
        },
        strict_evidence_provider=True,
    ),
    _distance_fact_spec(
        key="nearest_half_day_primary_school_m",
        aliases=("nearest_school_m", "distance_half_day_primary_school_m"),
        label="Half-day primary-school distance",
        search_label="half-day primary school",
        preference_keys=("max_distance_to_halbtags_volksschule_m",),
        poi_keys=("schoolatlas=half_day_primary_school",),
        provider="schoolatlas",
        evidence_providers=("schoolatlas",),
        evidence_source_keys_by_provider={
            "schoolatlas": (
                "nearest_half_day_primary_school_m",
                "distance_half_day_primary_school_m",
            ),
        },
        strict_evidence_provider=True,
    ),
    _distance_fact_spec(
        key="nearest_library_m",
        aliases=("distance_library_m",),
        label="Library distance",
        search_label="library",
        preference_keys=("max_distance_to_library_m", "prefer_libraries_nearby"),
        poi_keys=("amenity=library",),
    ),
    _distance_fact_spec(
        key="nearest_zoo_m",
        aliases=("distance_zoo_m",),
        label="Zoo distance",
        search_label="zoo",
        preference_keys=("max_distance_to_zoo_m", "prefer_zoo_nearby"),
        poi_keys=("tourism=zoo",),
    ),
    _distance_fact_spec(
        key="nearest_market_m",
        aliases=("distance_market_m",),
        label="Market distance",
        search_label="market",
        preference_keys=("max_distance_to_market_m", "prefer_markets_nearby"),
        poi_keys=("amenity=marketplace",),
    ),
    _distance_fact_spec(
        key="nearest_hardware_store_m",
        aliases=("nearest_baumarkt_m", "distance_hardware_store_m"),
        label="Hardware-store distance",
        search_label="hardware store",
        preference_keys=("max_distance_to_hardware_store_m", "prefer_hardware_store_nearby"),
        poi_keys=("shop=doityourself", "shop=hardware"),
    ),
    _distance_fact_spec(
        key="nearest_shopping_center_m",
        aliases=("nearest_shopping_centre_m", "distance_shopping_center_m"),
        label="Shopping-center distance",
        search_label="shopping center",
        preference_keys=("max_distance_to_shopping_center_m", "prefer_shopping_center_nearby"),
        poi_keys=("shop=mall",),
    ),
    _distance_fact_spec(
        key="nearest_shopping_street_m",
        aliases=("distance_shopping_street_m",),
        label="Shopping-street distance",
        search_label="shopping street",
        preference_keys=("max_distance_to_shopping_street_m", "prefer_shopping_street_nearby"),
        poi_keys=("highway=pedestrian",),
    ),
    _distance_fact_spec(
        key="nearest_theatre_m",
        aliases=("nearest_theater_m", "distance_theatre_m", "distance_theater_m"),
        label="Theatre distance",
        search_label="theatre",
        preference_keys=("max_distance_to_theatre_m", "prefer_theatre_nearby"),
        poi_keys=("amenity=theatre",),
    ),
    _distance_fact_spec(
        key="nearest_public_pool_m",
        aliases=("nearest_swimming_pool_m", "distance_public_pool_m"),
        label="Public-pool distance",
        search_label="public pool",
        preference_keys=("max_distance_to_public_pool_m", "prefer_public_pool_nearby"),
        poi_keys=("leisure=swimming_pool",),
    ),
    _distance_fact_spec(
        key="nearest_starbucks_m",
        aliases=("distance_starbucks_m",),
        label="Starbucks distance",
        search_label="Starbucks",
        preference_keys=("max_distance_to_starbucks_m", "prefer_starbucks_nearby"),
        poi_keys=("brand=starbucks", "name~=starbucks"),
    ),
    _distance_fact_spec(
        key="nearest_fitness_center_m",
        aliases=("nearest_fitness_centre_m", "nearest_gym_m", "distance_fitness_center_m"),
        label="Fitness-center distance",
        search_label="fitness center",
        preference_keys=("max_distance_to_fitness_center_m", "prefer_fitness_center_nearby"),
        poi_keys=("leisure=fitness_centre", "amenity=gym", "sport=fitness"),
    ),
    _distance_fact_spec(
        key="nearest_cinema_m",
        aliases=("distance_cinema_m",),
        label="Cinema distance",
        search_label="cinema",
        preference_keys=("max_distance_to_cinema_m", "prefer_cinema_nearby"),
        poi_keys=("amenity=cinema",),
    ),
    _distance_fact_spec(
        key="nearest_bouldering_m",
        aliases=("nearest_climbing_m", "distance_bouldering_m"),
        label="Bouldering distance",
        search_label="bouldering",
        preference_keys=("max_distance_to_bouldering_m", "prefer_bouldering_nearby"),
        poi_keys=("sport=climbing", "sport=bouldering", "name~=boulder"),
    ),
    _distance_fact_spec(
        key="nearest_dog_park_m",
        aliases=("distance_dog_park_m",),
        label="Dog-park distance",
        search_label="dog park",
        preference_keys=("max_distance_to_dog_park_m", "prefer_dog_park_nearby"),
        poi_keys=("leisure=dog_park", "amenity=dog_park"),
    ),
    _distance_fact_spec(
        key="nearest_good_cafe_m",
        aliases=("nearest_cafe_m", "distance_good_cafe_m"),
        label="Quality-verified café distance",
        search_label="quality-verified café",
        preference_keys=("max_distance_to_good_cafe_m", "prefer_good_cafe_nearby"),
        poi_keys=("amenity=cafe+independent_quality_evidence",),
        provider="quality_verified_cafe_source",
        evidence_providers=("quality_verified_cafe_source",),
        evidence_source_keys_by_provider={
            "quality_verified_cafe_source": (
                "nearest_good_cafe_m",
                "distance_good_cafe_m",
            ),
        },
        strict_evidence_provider=True,
    ),
    _distance_fact_spec(
        key="nearest_tram_bus_m",
        aliases=("nearest_surface_transit_m", "distance_tram_bus_m"),
        label="Tram/bus distance",
        search_label="tram or bus",
        poi_keys=("railway=tram_stop", "highway=bus_stop"),
        search_supported=False,
    ),
    _distance_fact_spec(
        key="nearest_flowing_water_m",
        aliases=("distance_flowing_water_m",),
        label="Flowing-water distance",
        search_label="flowing water",
        poi_keys=("waterway~=river|riverbank|canal|stream|brook", "natural=water"),
        search_supported=False,
    ),
)


def property_fact_distance_specs(
    *,
    search_supported_only: bool = False,
) -> tuple[dict[str, object], ...]:
    """Return defensive copies of the shared bounded distance-fact registry."""
    return tuple(
        {
            key: (
                list(value)
                if isinstance(value, tuple)
                else {
                    nested_key: list(nested_value)
                    if isinstance(nested_value, tuple)
                    else nested_value
                    for nested_key, nested_value in value.items()
                }
                if isinstance(value, Mapping)
                else value
            )
            for key, value in spec.items()
        }
        for spec in PROPERTY_FACT_DISTANCE_SPECS
        if not search_supported_only or bool(spec.get("search_supported"))
    )


def property_fact_distance_preference_keys(
    *,
    search_supported_only: bool = False,
    canonical_only: bool = False,
) -> tuple[str, ...]:
    """Return the registry-backed radius keys accepted by search preferences.

    The first required preference key on each row is the canonical form shown
    by the search UI.  Remaining keys are bounded compatibility aliases (for
    example, ``max_distance_to_underground_m``).
    """
    keys: list[str] = []
    for spec in PROPERTY_FACT_DISTANCE_SPECS:
        if search_supported_only and not bool(spec.get("search_supported")):
            continue
        preference_keys = tuple(
            str(value or "").strip()
            for value in tuple(spec.get("required_preference_keys") or ())
            if str(value or "").strip().startswith("max_distance_to_")
            and str(value or "").strip().endswith("_m")
        )
        if canonical_only:
            preference_keys = preference_keys[:1]
        for key in preference_keys:
            if key not in keys:
                keys.append(key)
    registry_keys = tuple(keys)
    if search_supported_only:
        contract_keys = _property_distance_preference_keys(
            canonical_only=canonical_only,
        )
        if registry_keys != contract_keys:
            raise RuntimeError("property_distance_preference_registry_contract_mismatch")
    return registry_keys


def property_fact_distance_importance_key(preference_key: object) -> str:
    """Return the importance companion for one registry radius key."""
    return _property_distance_importance_key(preference_key)


def normalize_property_fact_distance_importance(
    value: object,
    *,
    default: str = "nice_to_have",
) -> str:
    """Canonicalize the four search/detail distance-importance states."""
    return _normalize_property_distance_importance(value, default=default)


def _normalized_text(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _active_preference(value: object) -> bool:
    if value in (None, "", False, (), [], {}):
        return False
    if isinstance(value, str):
        return _normalized_text(value) not in {
            "0",
            "0.0",
            "false",
            "off",
            "none",
            "null",
            "neutral",
            "any",
        }
    if isinstance(value, (int, float)):
        return float(value) > 0.0
    return True


def _bounded_positive_distance(value: object) -> int | None:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return parsed if 0 < parsed <= 5_000_000 else None


def property_fact_value(
    facts: Mapping[str, object],
    aliases: Sequence[str],
) -> tuple[int | None, str]:
    for alias in aliases:
        value = _bounded_positive_distance(facts.get(alias))
        if value is not None:
            return value, alias
    return None, ""


def property_fact_candidate_ref(candidate: Mapping[str, object]) -> str:
    explicit = str(candidate.get("candidate_ref") or candidate.get("research_candidate_ref") or "").strip()
    if explicit:
        return explicit
    raw = "|".join(
        str(candidate.get(key) or "").strip()
        for key in ("title", "property_url", "review_url", "source_ref", "source_label")
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def property_fact_source_fingerprint(property_url: object) -> str:
    """Bind evidence to the exact defragmented listing URL it describes."""
    normalized = urllib.parse.urldefrag(str(property_url or "").strip())[0]
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _finite_coordinate(value: object, *, latitude: bool) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    limit = 90.0 if latitude else 180.0
    return parsed if math.isfinite(parsed) and -limit <= parsed <= limit else None


def property_fact_coordinate_digest(latitude: object, longitude: object) -> str:
    """Canonical digest for the exact coordinate pair used by a fact query."""
    lat = _finite_coordinate(latitude, latitude=True)
    lon = _finite_coordinate(longitude, latitude=False)
    if lat is None or lon is None:
        return ""
    encoded = f"{lat:.8f},{lon:.8f}".encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _property_fact_provider_attestation_payload(
    evidence: Mapping[str, object],
    *,
    observed_value: object,
) -> dict[str, object]:
    """Canonical response-derived fields covered by the internal trust seal."""
    try:
        normalized_value: object = int(round(float(observed_value)))
    except (TypeError, ValueError):
        normalized_value = ""
    return {
        "version": PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION,
        "provider": _normalized_text(evidence.get("provider")),
        "listing_url": urllib.parse.urldefrag(
            str(evidence.get("listing_url") or "").strip()
        )[0],
        "source_fingerprint": str(
            evidence.get("source_fingerprint") or ""
        ).strip(),
        "source_key": str(evidence.get("source_key") or "").strip(),
        "observed_key": str(evidence.get("observed_key") or "").strip(),
        "observed_value_m": normalized_value,
        "listing_latitude": _finite_coordinate(
            evidence.get("listing_latitude"), latitude=True
        ),
        "listing_longitude": _finite_coordinate(
            evidence.get("listing_longitude"), latitude=False
        ),
        "coordinate_digest": str(
            evidence.get("coordinate_digest") or ""
        ).strip(),
        "query_endpoint_url": str(
            evidence.get("query_endpoint_url") or ""
        ).strip(),
        "query_url": str(evidence.get("query_url") or "").strip(),
        "query_digest": str(evidence.get("query_digest") or "").strip(),
        "query_schema": str(evidence.get("query_schema") or "").strip(),
        "receipt_url": str(evidence.get("receipt_url") or "").strip(),
        "provider_object_id": str(
            evidence.get("provider_object_id") or ""
        ).strip(),
        "provider_object_type": _normalized_text(
            evidence.get("provider_object_type")
        ),
        "provider_object_version": str(
            evidence.get("provider_object_version") or ""
        ).strip(),
        "provider_object_timestamp": str(
            evidence.get("provider_object_timestamp") or ""
        ).strip(),
        "provider_object_changeset": str(
            evidence.get("provider_object_changeset") or ""
        ).strip(),
        "provider_observed_at": str(
            evidence.get("provider_observed_at") or ""
        ).strip(),
        "provider_expires_at": str(
            evidence.get("provider_expires_at") or ""
        ).strip(),
        "observed_at": str(evidence.get("observed_at") or "").strip(),
        "expires_at": str(evidence.get("expires_at") or "").strip(),
        "poi_latitude": _finite_coordinate(
            evidence.get("poi_latitude"), latitude=True
        ),
        "poi_longitude": _finite_coordinate(
            evidence.get("poi_longitude"), latitude=False
        ),
        "poi_classification_tags": {
            str(key).strip(): str(value).strip()
            for key, value in sorted(
                dict(evidence.get("poi_classification_tags") or {}).items()
            )
            if str(key).strip() and str(value).strip()
        },
    }


def _property_fact_provider_attestation_secret() -> bytes:
    # A process-local key is acceptable only in dev/test. Production evidence
    # must remain valid across API/worker processes and restarts, so fail closed
    # unless the operator configured the shared signing secret explicitly.
    from app.settings import get_settings, is_prod_mode, resolve_signing_secret

    secret = resolve_signing_secret(
        settings := get_settings(),
        purpose=PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION,
    )
    if is_prod_mode(getattr(getattr(settings, "runtime", None), "mode", "")):
        configured = str(
            getattr(getattr(settings, "auth", None), "signing_secret", "")
            or ""
        ).strip()
        if not configured:
            return b""
    return str(secret or "").encode("utf-8")


def property_fact_provider_attestation_is_ready() -> bool:
    """Whether this process can mint and verify durable provider evidence."""
    return bool(_property_fact_provider_attestation_secret())


def property_fact_issue_provider_attestation(
    evidence: Mapping[str, object],
    *,
    observed_value: object,
) -> str:
    """Seal evidence only after a trusted provider adapter parsed its response."""
    secret = _property_fact_provider_attestation_secret()
    if not secret:
        return ""
    payload = _property_fact_provider_attestation_payload(
        evidence,
        observed_value=observed_value,
    )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "hmac-sha256:" + hmac.new(secret, encoded, hashlib.sha256).hexdigest()


def property_fact_provider_attestation_is_valid(
    evidence: Mapping[str, object],
    *,
    observed_value: object,
) -> bool:
    version = str(evidence.get("attestation_version") or "").strip()
    supplied = str(evidence.get("provider_attestation") or "").strip()
    if (
        version != PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION
        or not re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", supplied)
    ):
        return False
    expected = property_fact_issue_provider_attestation(
        evidence,
        observed_value=observed_value,
    )
    return bool(expected) and hmac.compare_digest(supplied, expected)


def _explicit_priority_keys(preferences: Mapping[str, object], priority: str) -> set[str]:
    keys: set[str] = set()
    list_keys = {
        "required": (
            "required_fact_keys",
            "must_have_fact_keys",
            "avoid_fact_keys",
            "strong_wish_fact_keys",
        ),
        "lazy": ("nice_to_have_fact_keys", "lazy_fact_keys"),
    }
    for list_key in list_keys.get(priority, ()):
        raw_values = preferences.get(list_key)
        values = raw_values if isinstance(raw_values, (list, tuple, set)) else str(raw_values or "").split(",")
        keys.update(_normalized_text(value) for value in values if _normalized_text(value))
    raw_priorities = preferences.get("fact_priorities")
    if isinstance(raw_priorities, Mapping):
        keys.update(
            _normalized_text(key)
            for key, value in raw_priorities.items()
            if _normalized_text(value) == priority
        )
    return keys


def _node_priority(
    *,
    preference_keys: Sequence[str],
    nodes: Sequence[Mapping[str, object]],
) -> str:
    normalized_keys = {_normalized_text(key) for key in preference_keys}
    for node in nodes:
        key = _normalized_text(node.get("key") or node.get("preference_key") or node.get("name"))
        node_value = node.get("value") if "value" in node else node.get("value_json", True)
        if key not in normalized_keys or not _active_preference(node_value):
            continue
        category = _normalized_text(node.get("category") or node.get("kind") or node.get("type"))
        strength = _normalized_text(node.get("strength") or node.get("priority") or node.get("weight"))
        if category in {"constraint", "must_have", "must", "aversion", "avoid"}:
            return "required"
        if category in {"soft_preference", "preference", "wish"} and strength in {
            "high",
            "strong",
            "critical",
            "must",
            "3",
        }:
            return "required"
    return "lazy"


def _explicit_importance_priority(
    *,
    preference_keys: Sequence[str],
    preferences: Mapping[str, object],
) -> str | None:
    """Map the current request's importance selector before stored-profile defaults."""
    required_importance = {
        "avoid",
        "critical",
        "high",
        "important",
        "must",
        "must_have",
        "required",
        "strong",
        "strong_wish",
        "3",
    }
    lazy_importance = {
        "lazy",
        "low",
        "nice",
        "nice_to_have",
        "optional",
        "1",
    }
    for preference_key in preference_keys:
        if not _active_preference(preferences.get(preference_key)):
            continue
        importance_keys = [f"{preference_key}_importance"]
        if preference_key.endswith("_m"):
            importance_keys.insert(0, f"{preference_key[:-2]}_importance")
        for importance_key in importance_keys:
            if importance_key not in preferences:
                continue
            importance = _normalized_text(preferences.get(importance_key))
            if importance in required_importance:
                return "required"
            if importance in lazy_importance:
                return "lazy"
    return None


def _fact_priority(
    *,
    spec: Mapping[str, object],
    preferences: Mapping[str, object],
    nodes: Sequence[Mapping[str, object]],
) -> str:
    canonical_key = _normalized_text(spec.get("key"))
    aliases = tuple(str(value) for value in tuple(spec.get("aliases") or ()))
    preference_keys = tuple(str(value) for value in tuple(spec.get("preference_keys") or ()))
    all_keys = {canonical_key, *(_normalized_text(value) for value in aliases), *(_normalized_text(value) for value in preference_keys)}
    required_keys = _explicit_priority_keys(preferences, "required")
    lazy_keys = _explicit_priority_keys(preferences, "lazy")
    if required_keys.intersection(all_keys):
        return "required"
    if lazy_keys.intersection(all_keys):
        return "lazy"
    importance_priority = _explicit_importance_priority(
        preference_keys=preference_keys,
        preferences=preferences,
    )
    if importance_priority is not None:
        return importance_priority
    strength_map = preferences.get("preference_strengths")
    if isinstance(strength_map, Mapping):
        for preference_key in preference_keys:
            if _active_preference(preferences.get(preference_key)) and _normalized_text(strength_map.get(preference_key)) in {
                "high",
                "strong",
                "critical",
                "must",
                "3",
            }:
                return "required"
    return _node_priority(preference_keys=preference_keys, nodes=nodes)


def _fact_spec_is_relevant(
    *,
    spec: Mapping[str, object],
    facts: Mapping[str, object],
    preferences: Mapping[str, object],
    nodes: Sequence[Mapping[str, object]],
) -> bool:
    """Limit detail work to baseline facts plus facts selected by the user."""
    if bool(spec.get("default_lazy")):
        return True
    canonical_key = _normalized_text(spec.get("key"))
    aliases = tuple(str(value) for value in tuple(spec.get("aliases") or ()))
    preference_keys = tuple(str(value) for value in tuple(spec.get("preference_keys") or ()))
    if property_fact_value(facts, aliases)[0] is not None:
        return True
    all_keys = {
        canonical_key,
        *(_normalized_text(value) for value in aliases),
        *(_normalized_text(value) for value in preference_keys),
    }
    if _explicit_priority_keys(preferences, "required").intersection(all_keys):
        return True
    if _explicit_priority_keys(preferences, "lazy").intersection(all_keys):
        return True
    if any(_active_preference(preferences.get(key)) for key in preference_keys):
        return True
    for node in nodes:
        node_key = _normalized_text(
            node.get("key") or node.get("preference_key") or node.get("name")
        )
        node_value = node.get("value") if "value" in node else node.get("value_json", True)
        if node_key in all_keys and _active_preference(node_value):
            return True
    return False


def _evidence_for_fact(
    facts: Mapping[str, object],
    key: str,
    aliases: Sequence[str] = (),
    *,
    observed_source_key: str = "",
) -> dict[str, object]:
    evidence_map = facts.get("property_fact_evidence")
    if not isinstance(evidence_map, Mapping):
        return {}
    if str(observed_source_key or "").strip():
        raw = evidence_map.get(str(observed_source_key).strip())
        return dict(raw) if isinstance(raw, Mapping) else {}
    for evidence_key in tuple(dict.fromkeys((key, *(str(value) for value in aliases)))):
        raw = evidence_map.get(evidence_key)
        if isinstance(raw, Mapping):
            return dict(raw)
    return {}


def _evidence_is_stale(evidence: Mapping[str, object]) -> bool:
    expires_at = str(evidence.get("expires_at") or "").strip()
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return expires <= datetime.now(timezone.utc)


def _evidence_provider_is_allowed(
    evidence: Mapping[str, object],
    spec: Mapping[str, object],
    *,
    observed_source_key: str = "",
) -> bool:
    allowed = {
        _normalized_text(value)
        for value in tuple(spec.get("evidence_providers") or ())
        if _normalized_text(value)
    }
    provider = _normalized_text(evidence.get("provider"))
    if allowed and provider not in allowed:
        return False
    source_keys_by_provider = spec.get("evidence_source_keys_by_provider")
    if not isinstance(source_keys_by_provider, Mapping):
        return True
    allowed_source_keys = {
        _normalized_text(value)
        for value in tuple(source_keys_by_provider.get(provider) or ())
        if _normalized_text(value)
    }
    if not allowed_source_keys:
        return True
    return bool(
        _normalized_text(evidence.get("source_key")) in allowed_source_keys
        and _normalized_text(observed_source_key) in allowed_source_keys
    )


def _distance_between_coordinates_m(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    radius_m = 6_371_000.0
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = math.radians(latitude_b - latitude_a)
    delta_lon = math.radians(longitude_b - longitude_a)
    arc = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat_a)
        * math.cos(lat_b)
        * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * radius_m * math.atan2(
        math.sqrt(arc),
        math.sqrt(max(0.0, 1.0 - arc)),
    )


def _classification_tags_match_spec(
    tags: Mapping[str, object],
    spec: Mapping[str, object],
) -> bool:
    normalized_tags = {
        str(key).strip().lower(): str(value).strip().lower()
        for key, value in tags.items()
        if str(key).strip() and str(value).strip()
    }
    if not normalized_tags:
        return False
    for raw_criterion in tuple(spec.get("poi_keys") or ()):
        criterion = str(raw_criterion or "").strip().lower()
        if "~=" in criterion:
            tag_key, pattern = criterion.split("~=", 1)
            observed = normalized_tags.get(tag_key)
            if observed and re.search(pattern, observed, flags=re.IGNORECASE):
                return True
            continue
        if "=" not in criterion:
            continue
        tag_key, expected = criterion.split("=", 1)
        if normalized_tags.get(tag_key) == expected:
            return True
    return False


def _https_url(value: object) -> urllib.parse.SplitResult | None:
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    return parsed


def _provider_receipt_is_structurally_valid(
    evidence: Mapping[str, object],
    *,
    provider: str,
) -> bool:
    object_id = str(evidence.get("provider_object_id") or "").strip()
    object_type = _normalized_text(evidence.get("provider_object_type"))
    object_version = str(evidence.get("provider_object_version") or "").strip()
    receipt = _https_url(evidence.get("receipt_url"))
    if not object_id or not object_type or not object_version or receipt is None:
        return False
    if provider == "openstreetmap_overpass":
        if (
            object_type not in {"node", "way", "relation"}
            or not object_id.isdigit()
            or not object_version.isdigit()
            or int(object_version) <= 0
            or str(receipt.hostname or "").lower() != "api.openstreetmap.org"
        ):
            return False
        expected_path = f"/api/0.6/{object_type}/{object_id}/{int(object_version)}"
        return receipt.path == expected_path and not receipt.query
    if provider == "google_maps_browseract":
        hostname = str(receipt.hostname or "").lower()
        return bool(
            object_type == "place"
            and object_version == "1"
            and 5 <= len(object_id) <= 120
            and (hostname == "google.com" or hostname.endswith(".google.com"))
            and receipt.path.startswith("/maps")
            and object_id in urllib.parse.unquote(receipt.geturl())
        )
    receipt_text = urllib.parse.unquote(receipt.geturl()).casefold()
    return object_id.casefold() in receipt_text and object_version.casefold() in receipt_text


def _provider_query_binding_is_valid(
    evidence: Mapping[str, object],
    *,
    provider: str,
    listing_latitude: float,
    listing_longitude: float,
) -> bool:
    query_digest = str(evidence.get("query_digest") or "").strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", query_digest):
        return False
    if provider == "openstreetmap_overpass":
        endpoint = _https_url(evidence.get("query_endpoint_url"))
        if (
            endpoint is None
            or str(endpoint.hostname or "").lower() != "overpass-api.de"
            or endpoint.path != "/api/interpreter"
            or endpoint.query
            or str(evidence.get("query_schema") or "").strip()
            != "propertyquarry.osm-nearby.v2"
        ):
            return False
        # The producer and validator share the deterministic query builder, so
        # a plausible-looking digest cannot be detached from the exact point.
        from app.product.property_location_research import property_fact_osm_nearby_query

        query = property_fact_osm_nearby_query(
            listing_latitude,
            listing_longitude,
        )
        expected = "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest()
        return query_digest == expected
    query_url = _https_url(evidence.get("query_url"))
    query_schema = str(evidence.get("query_schema") or "").strip()
    if query_url is None or not query_schema:
        return False
    if provider == "google_maps_browseract":
        hostname = str(query_url.hostname or "").lower()
        query = urllib.parse.parse_qs(query_url.query)
        origin = next(iter(query.get("origin") or []), "").strip()
        destination = next(iter(query.get("destination") or []), "").strip()
        travel_mode = next(iter(query.get("travelmode") or []), "").strip()
        if (
            query_schema != "propertyquarry.google-maps-browseract-distance.v1"
            or not (hostname == "google.com" or hostname.endswith(".google.com"))
            or query_url.path != "/maps/dir/"
            or next(iter(query.get("api") or []), "") != "1"
            or origin != f"{listing_latitude:.8f},{listing_longitude:.8f}"
            or not destination
            or travel_mode not in {"walking", "driving", "bicycling", "transit"}
        ):
            return False
    expected = "sha256:" + hashlib.sha256(
        query_url.geturl().encode("utf-8")
    ).hexdigest()
    return query_digest == expected


def property_fact_distance_evidence_is_valid(
    *,
    facts: Mapping[str, object],
    evidence: Mapping[str, object],
    spec: Mapping[str, object],
    observed_source_key: str,
    observed_value: object,
    property_url: object,
) -> bool:
    """Verify evidence strongly enough for a distance to affect score or rank."""
    normalized_url = urllib.parse.urldefrag(str(property_url or "").strip())[0]
    expected_source_fingerprint = (
        property_fact_source_fingerprint(normalized_url) if normalized_url else ""
    )
    provider = _normalized_text(evidence.get("provider"))
    listing_latitude = _finite_coordinate(
        evidence.get("listing_latitude"), latitude=True
    )
    listing_longitude = _finite_coordinate(
        evidence.get("listing_longitude"), latitude=False
    )
    current_latitude = _finite_coordinate(facts.get("map_lat"), latitude=True)
    current_longitude = _finite_coordinate(facts.get("map_lng"), latitude=False)
    poi_latitude = _finite_coordinate(evidence.get("poi_latitude"), latitude=True)
    poi_longitude = _finite_coordinate(
        evidence.get("poi_longitude"), latitude=False
    )
    if (
        not normalized_url
        or not expected_source_fingerprint
        or str(evidence.get("listing_url") or "").strip() != normalized_url
        or str(evidence.get("source_fingerprint") or "").strip()
        != expected_source_fingerprint
        or evidence.get("coordinate_exact") is not True
        or listing_latitude is None
        or listing_longitude is None
        or current_latitude is None
        or current_longitude is None
        or poi_latitude is None
        or poi_longitude is None
        or abs(listing_latitude - current_latitude) > 0.00000001
        or abs(listing_longitude - current_longitude) > 0.00000001
        or str(evidence.get("coordinate_digest") or "").strip()
        != property_fact_coordinate_digest(current_latitude, current_longitude)
        or _normalized_text(evidence.get("observed_key"))
        != _normalized_text(observed_source_key)
        or _normalized_text(evidence.get("source_key"))
        != _normalized_text(observed_source_key)
        or not _evidence_provider_is_allowed(
            evidence,
            spec,
            observed_source_key=observed_source_key,
        )
        or not _provider_receipt_is_structurally_valid(
            evidence,
            provider=provider,
        )
        or not _provider_query_binding_is_valid(
            evidence,
            provider=provider,
            listing_latitude=listing_latitude,
            listing_longitude=listing_longitude,
        )
    ):
        return False
    if not property_fact_provider_attestation_is_valid(
        evidence,
        observed_value=observed_value,
    ):
        return False
    observed_at = str(evidence.get("observed_at") or "").strip()
    expires_at = str(evidence.get("expires_at") or "").strip()
    provider_observed_at = str(
        evidence.get("provider_observed_at") or ""
    ).strip()
    provider_expires_at = str(
        evidence.get("provider_expires_at") or ""
    ).strip()
    if (
        not observed_at
        or not expires_at
        or observed_at != provider_observed_at
        or expires_at != provider_expires_at
    ):
        return False
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if observed.tzinfo is None or expires.tzinfo is None:
            return False
        observed = observed.astimezone(timezone.utc)
        expires = expires.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False
    now = datetime.now(timezone.utc)
    if (
        observed > now
        or now >= expires
        or expires <= observed
        or expires - observed > timedelta(hours=24)
    ):
        return False
    classification_tags = evidence.get("poi_classification_tags")
    if not isinstance(classification_tags, Mapping) or not _classification_tags_match_spec(
        classification_tags,
        spec,
    ):
        return False
    try:
        observed_distance = float(observed_value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(observed_distance) or observed_distance <= 0.0:
        return False
    recomputed_distance = _distance_between_coordinates_m(
        listing_latitude,
        listing_longitude,
        poi_latitude,
        poi_longitude,
    )
    # Provider distances are normalized to a coordinate-derived straight-line
    # metre value. Nothing wider than rounding tolerance is accepted into
    # score or gate calculations, including browser-backed Maps evidence.
    return abs(recomputed_distance - observed_distance) <= 1.0


def _score_evidence_is_fresh(
    evidence: Mapping[str, object],
    spec: Mapping[str, object],
    *,
    facts: Mapping[str, object],
    observed_source_key: str,
    observed_value: object,
    property_url: object,
) -> bool:
    if (
        not property_fact_distance_evidence_is_valid(
            facts=facts,
            evidence=evidence,
            spec=spec,
            observed_source_key=observed_source_key,
            observed_value=observed_value,
            property_url=property_url,
        )
    ):
        return False
    observed_at = str(evidence.get("observed_at") or "").strip()
    expires_at = str(evidence.get("expires_at") or "").strip()
    if not observed_at or not expires_at:
        return False
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if observed.tzinfo is None or expires.tzinfo is None:
            return False
    except (TypeError, ValueError):
        return False
    now = datetime.now(timezone.utc)
    return observed <= now < expires


def _distance_is_coordinate_estimate(
    facts: Mapping[str, object],
    evidence: Mapping[str, object],
) -> bool:
    if "coordinate_exact" in evidence:
        return evidence.get("coordinate_exact") is not True
    precision = _normalized_text(facts.get("map_location_precision"))
    source = str(facts.get("map_location_source") or "").strip().casefold()
    if precision in {"address", "building", "exact", "parcel", "property", "rooftop"}:
        return False
    return bool(
        precision in {
            "area",
            "centroid",
            "city",
            "district",
            "locality",
            "postal",
            "postal_area",
            "postcode",
            "region",
        }
        or "postal area" in source
        or "centroid" in source
        or "estimate" in source
    )


def property_fact_requirement_plan(
    *,
    facts: Mapping[str, object],
    preferences: Mapping[str, object] | None = None,
    preference_nodes: Sequence[Mapping[str, object]] = (),
    include_resolved: bool = True,
    property_url: object = "",
) -> list[dict[str, object]]:
    normalized_preferences = dict(preferences or {})
    plan: list[dict[str, object]] = []
    for spec in PROPERTY_FACT_DISTANCE_SPECS:
        if not _fact_spec_is_relevant(
            spec=spec,
            facts=facts,
            preferences=normalized_preferences,
            nodes=preference_nodes,
        ):
            continue
        aliases = tuple(str(value) for value in tuple(spec.get("aliases") or ()))
        value, source_key = property_fact_value(facts, aliases)
        key = str(spec.get("key") or "").strip()
        evidence = _evidence_for_fact(
            facts,
            key,
            aliases,
            observed_source_key=source_key,
        )
        priority = _fact_priority(
            spec=spec,
            preferences=normalized_preferences,
            nodes=preference_nodes,
        )
        state = "resolved" if value is not None else "unknown"
        if value is not None and not _score_evidence_is_fresh(
            evidence,
            spec,
            facts=facts,
            observed_source_key=source_key,
            observed_value=value,
            property_url=property_url,
        ):
            state = "stale"
        elif value is not None and not _evidence_provider_is_allowed(
                evidence,
                spec,
                observed_source_key=source_key,
        ):
            state = "stale"
        elif value is not None and _distance_is_coordinate_estimate(facts, evidence):
            state = "stale"
        elif value is not None and evidence and _evidence_is_stale(evidence):
            state = "stale"
        if not include_resolved and state == "resolved":
            continue
        plan.append(
            {
                "key": key,
                "aliases": list(aliases),
                "label": str(spec.get("label") or key).strip(),
                "state": state,
                "priority": priority,
                "affects_score": True,
                "value": value,
                "display_value": f"{value:,} m".replace(",", " ") if value is not None else "",
                "source_key": source_key,
                "provider": str(spec.get("provider") or "").strip(),
                "poi_keys": [
                    str(value)
                    for value in tuple(spec.get("poi_keys") or ())
                    if str(value).strip()
                ],
                "evidence_providers": [
                    str(value)
                    for value in tuple(spec.get("evidence_providers") or ())
                    if str(value).strip()
                ],
                "evidence_source_keys_by_provider": {
                    str(provider_key): [
                        str(value)
                        for value in tuple(source_keys or ())
                        if str(value).strip()
                    ]
                    for provider_key, source_keys in dict(
                        spec.get("evidence_source_keys_by_provider") or {}
                    ).items()
                },
                "provider_label": str(
                    _PROPERTY_FACT_PROVIDER_LABELS.get(
                        str(spec.get("provider") or "").strip(),
                        str(spec.get("provider") or "fact provider").strip(),
                    )
                ),
                "strict_evidence_provider": bool(
                    spec.get("strict_evidence_provider")
                ),
                "provenance": evidence,
                "error": {},
            }
        )
    return plan


def property_fact_score_state(plan: Sequence[Mapping[str, object]]) -> str:
    unresolved = [row for row in plan if str(row.get("state") or "unknown") != "resolved"]
    if any(str(row.get("priority") or "lazy") == "required" for row in unresolved):
        return "evaluating"
    if any(bool(row.get("affects_score")) for row in unresolved):
        return "provisional"
    return "final"


def _score_value(candidate: Mapping[str, object]) -> float | None:
    assessment = candidate.get("assessment")
    values = (
        candidate.get("fit_score"),
        candidate.get("score"),
        assessment.get("fit_score") if isinstance(assessment, Mapping) else None,
    )
    for raw in values:
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            continue
        if 0.0 <= parsed <= 100.0:
            return round(parsed, 2)
    return None


def property_fact_input_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def property_fact_score_projection(
    *,
    candidate: Mapping[str, object],
    plan: Sequence[Mapping[str, object]],
    preferences: Mapping[str, object] | None = None,
    previous_score: float | None = None,
    changed_reasons: Sequence[str] = (),
) -> dict[str, object]:
    state = property_fact_score_state(plan)
    raw_score = _score_value(candidate)
    current = None if state == "evaluating" else raw_score
    previous = previous_score
    if previous is None:
        prior_projection = candidate.get("score_projection")
        if isinstance(prior_projection, Mapping):
            try:
                previous = float(prior_projection.get("current"))
            except (TypeError, ValueError):
                previous = None
    if previous is None and current is not None:
        previous = current
    delta = round(current - previous, 2) if current is not None and previous is not None else None
    fact_digest_payload = [
        {
            "key": row.get("key"),
            "state": row.get("state"),
            "value": row.get("value"),
            "priority": row.get("priority"),
            "observed_at": dict(row.get("provenance") or {}).get("observed_at")
            if isinstance(row.get("provenance"), Mapping)
            else "",
        }
        for row in plan
    ]
    return {
        "state": state,
        "previous": previous,
        "current": current,
        "delta": delta,
        "changed_reasons": [str(value).strip() for value in changed_reasons if str(value).strip()][:8],
        "ranking_eligible": state != "evaluating" and current is not None,
        "algorithm_version": PROPERTY_FACT_SCORE_ALGORITHM_VERSION,
        "facts_digest": property_fact_input_digest(fact_digest_payload),
        "preference_digest": property_fact_input_digest(dict(preferences or {})),
    }


def property_fact_job_id(
    *,
    principal_id: str,
    run_id: str,
    candidate_ref: str,
    source_fingerprint: str,
    request_digest: str = "",
) -> str:
    raw = "|".join(
        (
            str(principal_id or "").strip(),
            str(run_id or "").strip(),
            str(candidate_ref or "").strip(),
            PROPERTY_FACT_GEO_BUNDLE_KIND,
            str(source_fingerprint or "").strip(),
            str(request_digest or "").strip(),
            PROPERTY_FACT_ENRICHMENT_SCHEMA_VERSION,
        )
    )
    return "pfe_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def property_fact_expiry(*, observed_at: datetime | None = None, hours: int = 24) -> str:
    observed = observed_at or datetime.now(timezone.utc)
    return (observed + timedelta(hours=max(1, min(int(hours), 168)))).isoformat()
