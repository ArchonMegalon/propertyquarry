from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.product.property_location_research as property_location_research
import app.product.property_search_storage as property_search_storage
import app.product.service as product_service
import app.settings as app_settings
import app.api.routes.landing_view_models as landing_view_models
from app.api.dependencies import RequestContext
from app.api.routes.product_api_contracts import (
    PropertyFactEnrichmentOut,
    PropertyFactProvenanceOut,
)
from app.api.routes.product_api_delivery import _require_property_fact_same_origin
from app.product.property_fact_enrichment import (
    PROPERTY_FACT_DISTANCE_SPECS,
    PROPERTY_FACT_ENRICHMENT_SCHEMA_VERSION,
    PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION,
    property_fact_coordinate_digest,
    property_fact_distance_evidence_is_valid,
    property_fact_distance_specs,
    property_fact_issue_provider_attestation,
    property_fact_requirement_plan,
    property_fact_score_projection,
    property_fact_source_fingerprint,
)
from app.product.property_location_research import (
    PROPERTY_FACT_OSM_QUERY_ENDPOINT,
    PROPERTY_FACT_OSM_QUERY_SCHEMA,
    property_fact_osm_nearby_query,
)
from app.product.service import (
    ProductService,
    _property_fact_fresh_geo_snapshot,
    _property_fact_location_query_is_exact,
    _property_fact_retry_remaining_seconds,
    _property_fact_safe_job_payload,
    _property_search_ranked_candidates_from_sources,
)
from tests.product_test_helpers import build_property_operator_client, start_workspace


_OSM_CLASSIFICATION_BY_FACT: dict[str, dict[str, str]] = {
    "nearest_supermarket_m": {"shop": "supermarket"},
    "nearest_playground_m": {"leisure": "playground"},
    "nearest_pharmacy_m": {"amenity": "pharmacy"},
    "nearest_medical_care_m": {"amenity": "clinic"},
    "nearest_subway_m": {"railway": "subway_entrance"},
    "nearest_university_m": {"amenity": "university"},
    "nearest_kindergarten_m": {"amenity": "kindergarten"},
    "nearest_library_m": {"amenity": "library"},
    "nearest_zoo_m": {"tourism": "zoo"},
    "nearest_market_m": {"amenity": "marketplace"},
    "nearest_hardware_store_m": {"shop": "hardware"},
    "nearest_shopping_center_m": {"shop": "mall"},
    "nearest_shopping_street_m": {"highway": "pedestrian"},
    "nearest_theatre_m": {"amenity": "theatre"},
    "nearest_public_pool_m": {"leisure": "swimming_pool"},
    "nearest_starbucks_m": {"brand": "starbucks"},
    "nearest_fitness_center_m": {"leisure": "fitness_centre"},
    "nearest_cinema_m": {"amenity": "cinema"},
    "nearest_bouldering_m": {"sport": "bouldering"},
    "nearest_dog_park_m": {"leisure": "dog_park"},
    "nearest_full_day_primary_school_m": {
        "schoolatlas": "full_day_primary_school"
    },
    "nearest_half_day_primary_school_m": {
        "schoolatlas": "half_day_primary_school"
    },
    "nearest_good_cafe_m": {
        "amenity": "cafe+independent_quality_evidence"
    },
}


def _valid_osm_evidence(
    *,
    key: str,
    distance_m: int,
    property_url: str,
    latitude: float = 48.2082,
    longitude: float = 16.3738,
    source_key: str | None = None,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    observed = observed_at or datetime.now(timezone.utc)
    expires = observed + timedelta(hours=24)
    query = property_fact_osm_nearby_query(latitude, longitude)
    object_id = str(10_000_000 + sum(ord(value) for value in key))
    poi_latitude = latitude + math.degrees(float(distance_m) / 6_371_000.0)
    evidence: dict[str, object] = {
        "provider": "openstreetmap_overpass",
        "method": "straight_line_osm",
        "observed_at": observed.isoformat(),
        "expires_at": expires.isoformat(),
        "freshness": "fresh",
        "confidence": 0.95,
        "source_key": source_key or key,
        "observed_key": source_key or key,
        "listing_url": urllib.parse.urldefrag(property_url)[0],
        "source_fingerprint": property_fact_source_fingerprint(property_url),
        "coordinate_basis": "candidate_listing_coordinates",
        "coordinate_observed_at": observed.isoformat(),
        "coordinate_precision": "address",
        "coordinate_source": "listing",
        "coordinate_exact": True,
        "listing_latitude": latitude,
        "listing_longitude": longitude,
        "coordinate_digest": property_fact_coordinate_digest(latitude, longitude),
        "query_endpoint_url": PROPERTY_FACT_OSM_QUERY_ENDPOINT,
        "query_digest": "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "query_schema": PROPERTY_FACT_OSM_QUERY_SCHEMA,
        "receipt_url": f"https://api.openstreetmap.org/api/0.6/node/{object_id}/1",
        "provider_object_id": object_id,
        "provider_object_type": "node",
        "provider_object_version": 1,
        "provider_object_timestamp": observed.isoformat(),
        "provider_object_changeset": "123456",
        "provider_observed_at": observed.isoformat(),
        "provider_expires_at": expires.isoformat(),
        "poi_latitude": poi_latitude,
        "poi_longitude": longitude,
        "poi_classification_tags": dict(_OSM_CLASSIFICATION_BY_FACT[key]),
    }
    evidence["attestation_version"] = (
        PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION
    )
    evidence["provider_attestation"] = property_fact_issue_provider_attestation(
        evidence,
        observed_value=distance_m,
    )
    return evidence


def _valid_osm_research(
    distances: dict[str, int],
    *,
    property_url: str,
    latitude: float = 48.2082,
    longitude: float = 16.3738,
) -> tuple[dict[str, object], dict[str, object]]:
    evidence = {
        key: _valid_osm_evidence(
            key=key,
            distance_m=distance,
            property_url=property_url,
            latitude=latitude,
            longitude=longitude,
        )
        for key, distance in distances.items()
    }
    research: dict[str, object] = {
        **distances,
        "property_fact_geo_evidence": {
            key: dict(row)
            for key, row in evidence.items()
        },
    }
    meta = {
        "coordinate_basis": "candidate_listing_coordinates",
        "coordinate_observed_at": datetime.now(timezone.utc).isoformat(),
        "coordinate_precision": "address",
        "coordinate_source": "listing",
        "coordinate_exact": True,
        "listing_latitude": latitude,
        "listing_longitude": longitude,
        "coordinate_digest": property_fact_coordinate_digest(latitude, longitude),
        "coordinate_updates": {},
    }
    return research, meta


def _valid_strict_evidence(
    *,
    provider: str,
    key: str,
    distance_m: int,
    property_url: str,
) -> dict[str, object]:
    evidence = _valid_osm_evidence(
        key=key,
        distance_m=distance_m,
        property_url=property_url,
    )
    evidence.pop("query_endpoint_url", None)
    evidence.pop("provider_attestation", None)
    evidence["provider"] = provider
    evidence["provider_object_type"] = "record"
    query_url = (
        f"https://{provider.replace('_', '-')}.example/query"
        f"?lat={evidence['listing_latitude']}&lon={evidence['listing_longitude']}&key={key}"
    )
    evidence["query_url"] = query_url
    evidence["query_schema"] = f"propertyquarry.{provider}.v1"
    evidence["query_digest"] = "sha256:" + hashlib.sha256(
        query_url.encode("utf-8")
    ).hexdigest()
    evidence["receipt_url"] = (
        f"https://{provider.replace('_', '-')}.example/records/"
        f"{evidence['provider_object_id']}/versions/1"
    )
    evidence["provider_attestation"] = property_fact_issue_provider_attestation(
        evidence,
        observed_value=distance_m,
    )
    return evidence


def _candidate(*, candidate_ref: str = "candidate-facts") -> dict[str, object]:
    return {
        "candidate_ref": candidate_ref,
        "title": "Family home",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/family-home-1234567890/",
        "source_ref": "property-scout:1234567890",
        "source_label": "Willhaben",
        "fit_score": 60.0,
        "ranking_score": 60.0,
        "assessment": {"domain": "willhaben", "fit_score": 60.0, "recommendation": "mention"},
        "property_facts": {},
    }


def _run_record(*, principal_id: str, run_id: str, candidate: dict[str, object]) -> dict[str, object]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "processed",
        "created_at": now,
        "updated_at": now,
        "property_search_preferences": {"prefer_supermarket_nearby": True},
        "summary": {
            "status": "processed",
            "ranked_candidates": [dict(candidate)],
            "sources": [
                {
                    "source_label": "Willhaben",
                    "source_url": "https://www.willhaben.at/iad/immobilien/",
                    "top_candidates": [dict(candidate)],
                    "research_candidates": [dict(candidate)],
                }
            ],
        },
        "events": [],
    }


def _seed_run(record: dict[str, object]) -> None:
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[str(record["run_id"])] = dict(record)


def _clear_run(run_id: str) -> None:
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY.pop(run_id, None)


def _required_fact_run_record(
    *,
    principal_id: str,
    run_id: str,
    candidate_ref: str = "candidate-required-durable",
    distance_preference_key: str = "max_distance_to_supermarket_m",
    distance_importance_key: str = "max_distance_to_supermarket_importance",
    fact_key: str = "nearest_supermarket_m",
) -> dict[str, object]:
    now = datetime.now(timezone.utc).isoformat()
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/"
        "required-durable-home-1234567890/"
    )
    preferences = {
        distance_preference_key: 500,
        distance_importance_key: "must_have",
        "use_stored_feedback_preferences": False,
    }
    facts: dict[str, object] = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "map_location_precision": "address",
        "map_location_source": "listing",
        "house_number": "12",
        "required_fact_research": {
            "status": "partial",
            "attempt": 1,
            "requested_keys": [fact_key],
            "resolved_keys": [],
            "unresolved_keys": [fact_key],
            "provider_receipts": [],
        },
    }
    plan = property_fact_requirement_plan(
        facts=facts,
        preferences=preferences,
        include_resolved=True,
        property_url=property_url,
    )
    projection = property_fact_score_projection(
        candidate={"fit_score": 60.0},
        plan=plan,
        preferences=preferences,
    )
    candidate: dict[str, object] = {
        "candidate_ref": candidate_ref,
        "title": "Durable required-fact home",
        "listing_id": "required-durable-home-1234567890",
        "property_url": property_url,
        "source_ref": "property-scout:required-durable-home-1234567890",
        "source_label": "Willhaben",
        "fit_score": 60.0,
        "ranking_score": 60.0,
        "assessment": {
            "domain": "willhaben",
            "fit_score": 60.0,
            "recommendation": "review",
        },
        "property_facts": facts,
        "fact_requirement_plan": plan,
        "score_projection": projection,
        "score_state": "evaluating",
        "ranking_eligible": False,
        "evaluation_state": "evaluating_missing_required_facts",
    }
    source = {
        "source_label": "Willhaben",
        "source_url": "https://www.willhaben.at/iad/immobilien/",
        "status": "completed_partial",
        "top_candidates": [],
        "research_candidates": [copy.deepcopy(candidate)],
        "evaluating_candidates": [copy.deepcopy(candidate)],
        "evaluating_candidate_total": 1,
    }
    return {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "completed_partial",
        "created_at": now,
        "updated_at": now,
        "property_search_preferences": preferences,
        "summary": {
            "status": "completed_partial",
            "status_without_required_fact_hold": "processed",
            "required_fact_hold_applied": True,
            "required_fact_resolution_pending": True,
            "required_fact_resolution_exhausted": False,
            "required_fact_resolution_attempts": 1,
            "required_fact_research_pending_total": 1,
            "evaluating_candidate_total": 1,
            "completion_reason": "required_fact_resolution_pending",
            "results_delivery_blocked_reason": (
                "required_property_facts_unresolved"
            ),
            "results_delivery_semantically_blocked": False,
            "ranked_candidates": [],
            "evaluating_candidates": [copy.deepcopy(candidate)],
            "sources": [source],
        },
        "events": [],
    }


def _install_durable_required_fact_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    record: dict[str, object],
) -> dict[str, object]:
    store: dict[str, object] = {
        "record": copy.deepcopy(record),
        "compact_writes": [],
    }

    def _load(*, run_id: str, principal_id: str) -> dict[str, object] | None:
        persisted = dict(store["record"])
        if (
            str(persisted.get("run_id") or "") != run_id
            or str(persisted.get("principal_id") or "") != principal_id
        ):
            return None
        return copy.deepcopy(persisted)

    def _compare_and_swap(**kwargs: object) -> dict[str, object]:
        updated = copy.deepcopy(dict(kwargs["updated_record"]))
        store["record"] = updated
        return {
            "status": "applied",
            "record": copy.deepcopy(updated),
            "record_sha256": "durable-test-applied",
        }

    def _list_records(**_kwargs: object) -> tuple[dict[str, object], ...]:
        return (
            property_search_storage._compact_property_search_run_record(
                copy.deepcopy(dict(store["record"]))
            ),
        )

    def _store_compact(record_to_store: dict[str, object]) -> bool:
        compact_writes = list(store["compact_writes"])
        compact_writes.append(
            property_search_storage._compact_property_search_run_record(
                copy.deepcopy(record_to_store)
            )
        )
        store["compact_writes"] = compact_writes
        return True

    monkeypatch.setattr(
        product_service,
        "_property_search_run_database_url",
        lambda: "postgresql://durable.test/property",
    )
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record_storage",
        _load,
    )
    monkeypatch.setattr(
        product_service,
        "_compare_and_swap_property_search_run_record",
        _compare_and_swap,
    )
    monkeypatch.setattr(
        product_service,
        "_list_property_search_run_records",
        _list_records,
    )
    monkeypatch.setattr(
        product_service,
        "_store_property_search_run_compact_record",
        _store_compact,
    )
    monkeypatch.setattr(
        ProductService,
        "_property_search_tour_events_by_source",
        lambda self, **kwargs: {},
    )
    monkeypatch.setattr(
        ProductService,
        "_refresh_property_search_results_delivery_state",
        lambda self, **kwargs: dict(kwargs.get("result") or {}),
    )
    return store


def _active_required_fact_job(
    store: dict[str, object],
    *,
    candidate_ref: str,
) -> tuple[str, dict[str, object]]:
    record = dict(store["record"])
    summary = dict(record.get("summary") or {})
    candidate_jobs = dict(
        summary.get("required_fact_enrichment_candidate_jobs") or {}
    )
    job_id = str(candidate_jobs[candidate_ref])
    job = dict(dict(summary.get("fact_enrichment_jobs") or {})[job_id])
    return job_id, job


def _age_required_fact_job_retry(
    store: dict[str, object],
    *,
    candidate_ref: str,
) -> None:
    record = copy.deepcopy(dict(store["record"]))
    summary = dict(record.get("summary") or {})
    candidate_jobs = dict(
        summary.get("required_fact_enrichment_candidate_jobs") or {}
    )
    job_id = str(candidate_jobs[candidate_ref])
    jobs = dict(summary.get("fact_enrichment_jobs") or {})
    job = dict(jobs[job_id])
    job["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()
    jobs[job_id] = job
    summary["fact_enrichment_jobs"] = jobs
    record["summary"] = summary
    store["record"] = record


def _resolved_geo_facts(
    *,
    include_playground: bool = True,
    property_url: str = (
        "https://www.willhaben.at/iad/immobilien/d/"
        "family-home-1234567890/"
    ),
) -> dict[str, object]:
    observed_at = datetime.now(timezone.utc)
    source_fingerprint = property_fact_source_fingerprint(property_url)
    coordinate_digest = property_fact_coordinate_digest(48.2082, 16.3738)
    distances = {
        "nearest_supermarket_m": 280,
        "nearest_pharmacy_m": 530,
        "nearest_medical_care_m": 570,
        "nearest_subway_m": 640,
    }
    facts: dict[str, object] = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "map_location_precision": "address",
        "map_location_source": "listing",
        "house_number": "12",
        "map_coordinate_evidence": {
            "exact": True,
            "trusted": True,
            "provider": "listing_structured_coordinates",
            "source_fingerprint": source_fingerprint,
            "latitude": 48.2082,
            "longitude": 16.3738,
            "coordinate_digest": coordinate_digest,
            "observed_at": observed_at.isoformat(),
            "expires_at": (observed_at + timedelta(hours=6)).isoformat(),
        },
        **distances,
        "property_fact_evidence": {
            key: _valid_osm_evidence(
                key=key,
                distance_m=value,
                property_url=property_url,
                observed_at=observed_at,
            )
            for key, value in distances.items()
        },
    }
    if include_playground:
        facts["nearest_playground_m"] = 410
        facts["property_fact_evidence"]["nearest_playground_m"] = _valid_osm_evidence(
            key="nearest_playground_m",
            distance_m=410,
            property_url=property_url,
            observed_at=observed_at,
        )
    return facts


def test_fact_priority_preserves_unknown_and_holds_required_facts_out_of_ranking() -> None:
    required_plan = property_fact_requirement_plan(
        facts={},
        preferences={},
        preference_nodes=(
            {
                "category": "soft_preference",
                "key": "prefer_supermarket_nearby",
                "strength": "high",
                "value": True,
            },
        ),
    )
    supermarket = next(row for row in required_plan if row["key"] == "nearest_supermarket_m")
    projection = property_fact_score_projection(
        candidate={"fit_score": 77},
        plan=required_plan,
        preferences={},
    )

    assert supermarket["state"] == "unknown"
    assert supermarket["priority"] == "required"
    assert supermarket["value"] is None
    assert projection["state"] == "evaluating"
    assert projection["current"] is None
    assert projection["ranking_eligible"] is False


def test_nice_to_have_unknown_is_provisional_and_distance_alias_resolves() -> None:
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/alias-home-1234567890/"
    )
    plan = property_fact_requirement_plan(
        facts={
            "map_lat": 48.2082,
            "map_lng": 16.3738,
            "distance_supermarket_m": 325,
            "property_fact_evidence": {
                "distance_supermarket_m": _valid_osm_evidence(
                    key="nearest_supermarket_m",
                    source_key="distance_supermarket_m",
                    distance_m=325,
                    property_url=property_url,
                )
            },
        },
        preferences={"prefer_supermarket_nearby": True},
        property_url=property_url,
    )
    supermarket = next(row for row in plan if row["key"] == "nearest_supermarket_m")
    projection = property_fact_score_projection(
        candidate={"fit_score": 64},
        plan=plan,
        preferences={"prefer_supermarket_nearby": True},
    )

    assert supermarket["state"] == "resolved"
    assert supermarket["source_key"] == "distance_supermarket_m"
    assert supermarket["display_value"] == "325 m"
    assert projection["state"] == "provisional"
    assert projection["current"] == 64.0
    assert projection["ranking_eligible"] is True


def test_distance_fact_registry_exhaustively_covers_search_preferences() -> None:
    expected_search_preferences = {
        "max_distance_to_supermarket_m",
        "max_distance_to_pharmacy_m",
        "max_distance_to_subway_m",
        "max_distance_to_underground_m",
        "max_distance_to_university_m",
        "max_distance_to_kindergarten_m",
        "max_distance_to_ganztags_volksschule_m",
        "max_distance_to_halbtags_volksschule_m",
        "max_distance_to_playground_m",
        "max_distance_to_library_m",
        "max_distance_to_zoo_m",
        "max_distance_to_market_m",
        "max_distance_to_hardware_store_m",
        "max_distance_to_shopping_center_m",
        "max_distance_to_shopping_street_m",
        "max_distance_to_theatre_m",
        "max_distance_to_public_pool_m",
        "max_distance_to_medical_care_m",
        "max_distance_to_starbucks_m",
        "max_distance_to_fitness_center_m",
        "max_distance_to_cinema_m",
        "max_distance_to_bouldering_m",
        "max_distance_to_dog_park_m",
        "max_distance_to_good_cafe_m",
    }
    registry = property_fact_distance_specs(search_supported_only=True)
    actual_preferences = {
        str(preference_key)
        for spec in registry
        for preference_key in list(spec["preference_keys"])
        if str(preference_key).startswith("max_distance_to_")
    }

    assert actual_preferences == expected_search_preferences
    assert len(actual_preferences) == len(registry) + 1
    assert len({str(spec["key"]) for spec in registry}) == len(registry)
    assert all(spec["aliases"] for spec in registry)
    assert all(str(spec["label"]).strip() for spec in registry)
    assert all(str(spec["search_label"]).strip() for spec in registry)
    assert all(spec["poi_keys"] for spec in registry)
    assert all(str(spec["provider"]).strip() for spec in registry)
    assert all(spec["evidence_providers"] for spec in registry)
    assert all(spec["evidence_source_keys_by_provider"] for spec in registry)


def test_every_search_distance_preference_defaults_lazy_and_explicit_strong_is_required() -> None:
    for spec in property_fact_distance_specs(search_supported_only=True):
        preference_key = next(
            str(value)
            for value in list(spec["preference_keys"])
            if str(value).startswith("max_distance_to_")
        )
        default_plan = property_fact_requirement_plan(
            facts={},
            preferences={preference_key: 750},
        )
        default_row = next(row for row in default_plan if row["key"] == spec["key"])
        assert default_row["priority"] == "lazy", preference_key
        assert default_row["state"] == "unknown", preference_key

        required_plan = property_fact_requirement_plan(
            facts={},
            preferences={
                preference_key: 750,
                f"{preference_key[:-2]}_importance": "strong_wish",
            },
        )
        required = next(row for row in required_plan if row["key"] == spec["key"])
        assert required["priority"] == "required", preference_key

        lazy_plan = property_fact_requirement_plan(
            facts={},
            preferences={
                preference_key: 750,
                f"{preference_key[:-2]}_importance": "nice_to_have",
            },
        )
        lazy = next(row for row in lazy_plan if row["key"] == spec["key"])
        assert lazy["priority"] == "lazy", preference_key
        assert lazy["state"] == "unknown", preference_key


def test_distance_fact_registry_accessor_returns_defensive_copies() -> None:
    first = property_fact_distance_specs()
    first[0]["aliases"].append("mutated_alias")
    second = property_fact_distance_specs()

    assert "mutated_alias" not in second[0]["aliases"]
    assert isinstance(PROPERTY_FACT_DISTANCE_SPECS, tuple)


def test_quality_cafe_and_school_classification_require_honest_provenance() -> None:
    observed_at = datetime.now(timezone.utc)
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/provider-home-1234567890/"
    )

    def _evidence(provider: str, *, source_key: str = "") -> dict[str, object]:
        return {
            "provider": provider,
            "observed_at": observed_at.isoformat(),
            "expires_at": (observed_at + timedelta(hours=24)).isoformat(),
            "source_fingerprint": "sha256:" + "c" * 64,
            "source_key": source_key,
            "coordinate_exact": True,
        }

    kindergarten_preferences = {"max_distance_to_kindergarten_m": 700}
    exact_osm_kindergarten = property_fact_requirement_plan(
        facts={
            "map_lat": 48.2082,
            "map_lng": 16.3738,
            "nearest_kindergarten_m": 260,
            "property_fact_evidence": {
                "nearest_kindergarten_m": _valid_osm_evidence(
                    key="nearest_kindergarten_m",
                    distance_m=260,
                    property_url=property_url,
                ),
            },
        },
        preferences=kindergarten_preferences,
        property_url=property_url,
    )
    assert next(
        row
        for row in exact_osm_kindergarten
        if row["key"] == "nearest_kindergarten_m"
    )["state"] == "resolved"

    generic_osm_school = property_fact_requirement_plan(
        facts={
            "nearest_school_m": 260,
            "property_fact_evidence": {
                "nearest_school_m": _evidence(
                    "openstreetmap_overpass",
                    source_key="nearest_school_m",
                ),
            },
        },
        preferences=kindergarten_preferences,
    )
    assert next(
        row
        for row in generic_osm_school
        if row["key"] == "nearest_kindergarten_m"
    )["state"] == "stale"

    school_preferences = {"max_distance_to_ganztags_volksschule_m": 900}
    school_from_osm = property_fact_requirement_plan(
        facts={
            "nearest_full_day_primary_school_m": 420,
            "property_fact_evidence": {
                "nearest_full_day_primary_school_m": _evidence("openstreetmap_overpass"),
            },
        },
        preferences=school_preferences,
    )
    school_row = next(
        row for row in school_from_osm if row["key"] == "nearest_full_day_primary_school_m"
    )
    assert school_row["state"] == "stale"

    schoolatlas = property_fact_requirement_plan(
        facts={
            "map_lat": 48.2082,
            "map_lng": 16.3738,
            "nearest_full_day_primary_school_m": 420,
            "property_fact_evidence": {
                "nearest_full_day_primary_school_m": _valid_strict_evidence(
                    provider="schoolatlas",
                    key="nearest_full_day_primary_school_m",
                    distance_m=420,
                    property_url=property_url,
                ),
            },
        },
        preferences=school_preferences,
        property_url=property_url,
    )
    assert next(
        row for row in schoolatlas if row["key"] == "nearest_full_day_primary_school_m"
    )["state"] == "resolved"

    cafe_preferences = {"max_distance_to_good_cafe_m": 700}
    generic_cafe = property_fact_requirement_plan(
        facts={
            "nearest_good_cafe_m": 180,
            "property_fact_evidence": {
                "nearest_good_cafe_m": _evidence("openstreetmap_overpass"),
            },
        },
        preferences=cafe_preferences,
    )
    cafe_row = next(row for row in generic_cafe if row["key"] == "nearest_good_cafe_m")
    assert cafe_row["label"] == "Quality-verified café distance"
    assert cafe_row["state"] == "stale"

    verified_cafe = property_fact_requirement_plan(
        facts={
            "map_lat": 48.2082,
            "map_lng": 16.3738,
            "nearest_good_cafe_m": 180,
            "property_fact_evidence": {
                "nearest_good_cafe_m": _valid_strict_evidence(
                    provider="quality_verified_cafe_source",
                    key="nearest_good_cafe_m",
                    distance_m=180,
                    property_url=property_url,
                ),
            },
        },
        preferences=cafe_preferences,
        property_url=property_url,
    )
    assert next(
        row for row in verified_cafe if row["key"] == "nearest_good_cafe_m"
    )["state"] == "resolved"


def test_distance_evidence_is_bound_to_the_exact_listing_url() -> None:
    original_url = (
        "https://www.willhaben.at/iad/immobilien/d/"
        "exact-source-home-1234567890/"
    )
    different_url = (
        "https://www.willhaben.at/iad/immobilien/d/"
        "different-source-home-1234567891/"
    )
    facts = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "nearest_supermarket_m": 240,
        "property_fact_evidence": {
            "nearest_supermarket_m": _valid_osm_evidence(
                key="nearest_supermarket_m",
                distance_m=240,
                property_url=original_url,
            )
        },
    }
    preferences = {"max_distance_to_supermarket_m": 500}

    matching = property_fact_requirement_plan(
        facts=facts,
        preferences=preferences,
        property_url=original_url,
    )
    mismatched = property_fact_requirement_plan(
        facts=facts,
        preferences=preferences,
        property_url=different_url,
    )

    assert next(
        row for row in matching if row["key"] == "nearest_supermarket_m"
    )["state"] == "resolved"
    assert next(
        row for row in mismatched if row["key"] == "nearest_supermarket_m"
    )["state"] == "stale"


def test_lazy_distance_cannot_borrow_another_listing_or_alias_evidence() -> None:
    original_url = (
        "https://www.willhaben.at/iad/immobilien/d/"
        "lazy-source-home-1234567890/"
    )
    different_url = (
        "https://www.willhaben.at/iad/immobilien/d/"
        "lazy-source-home-1234567891/"
    )
    facts = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "distance_supermarket_m": 240,
        "property_fact_evidence": {
            # Evidence for the canonical value must not authenticate the alias
            # value that was actually observed.
            "nearest_supermarket_m": _valid_osm_evidence(
                key="nearest_supermarket_m",
                distance_m=240,
                property_url=original_url,
            ),
            "distance_supermarket_m": _valid_osm_evidence(
                key="nearest_supermarket_m",
                source_key="distance_supermarket_m",
                distance_m=240,
                property_url=original_url,
            ),
        },
    }
    preferences = {
        "max_distance_to_supermarket_m": 500,
        "max_distance_to_supermarket_importance": "nice_to_have",
    }

    original_plan = property_fact_requirement_plan(
        facts=facts,
        preferences=preferences,
        property_url=original_url,
    )
    different_plan = property_fact_requirement_plan(
        facts=facts,
        preferences=preferences,
        property_url=different_url,
    )
    different_adjustment, different_notes = (
        product_service._property_distance_preference_score_adjustment(
            preferences=preferences,
            property_facts=facts,
            property_url=different_url,
        )
    )

    assert next(
        row for row in original_plan if row["key"] == "nearest_supermarket_m"
    )["state"] == "resolved"
    assert next(
        row for row in different_plan if row["key"] == "nearest_supermarket_m"
    )["state"] == "stale"
    assert different_adjustment == 0
    assert different_notes == ()

    alias_evidence = dict(facts["property_fact_evidence"])
    alias_evidence.pop("distance_supermarket_m")
    alias_only_plan = property_fact_requirement_plan(
        facts={**facts, "property_fact_evidence": alias_evidence},
        preferences=preferences,
        property_url=original_url,
    )
    alias_row = next(
        row for row in alias_only_plan if row["key"] == "nearest_supermarket_m"
    )
    assert alias_row["state"] == "stale"
    assert alias_row["provenance"] == {}


@pytest.mark.parametrize(
    ("provider", "source_key"),
    (
        ("wrong_provider", "nearest_supermarket_m"),
        ("openstreetmap_overpass", "nearest_playground_m"),
    ),
)
def test_ordinary_osm_fact_rejects_wrong_provider_or_wrong_classification(
    provider: str,
    source_key: str,
) -> None:
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/adversarial-home-1234567890/"
    )
    invalid_evidence = _valid_osm_evidence(
        key="nearest_supermarket_m",
        distance_m=240,
        property_url=property_url,
    )
    invalid_evidence["provider"] = provider
    invalid_evidence["source_key"] = source_key
    facts = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "nearest_supermarket_m": 240,
        "property_fact_evidence": {
            "nearest_supermarket_m": invalid_evidence
        },
    }
    preferences = {
        "max_distance_to_supermarket_m": 500,
        "max_distance_to_supermarket_importance": "must_have",
    }
    plan = property_fact_requirement_plan(
        facts=facts,
        preferences=preferences,
        property_url=property_url,
    )
    supermarket = next(
        row for row in plan if row["key"] == "nearest_supermarket_m"
    )

    assert supermarket["state"] == "stale"
    assert property_fact_score_projection(
        candidate={"fit_score": 80.0},
        plan=plan,
        preferences=preferences,
    )["ranking_eligible"] is False


def test_required_resolution_receipt_names_unavailable_strict_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/"
        "strict-school-home-1234567890/"
    )
    facts = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "map_location_precision": "address",
        "map_location_source": "listing",
        "house_number": "12",
    }
    preferences = {
        "max_distance_to_ganztags_volksschule_m": 900,
        "max_distance_to_ganztags_volksschule_importance": "must_have",
    }
    plan = property_fact_requirement_plan(
        facts=facts,
        preferences=preferences,
        property_url=property_url,
    )
    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        lambda **kwargs: (
            {"nearest_school_m": 420},
            {
                "coordinate_basis": "candidate_listing_coordinates",
                "coordinate_observed_at": "",
                "coordinate_precision": "address",
                "coordinate_source": "listing",
                "coordinate_exact": True,
            },
        ),
    )

    merged, receipt = product_service._property_search_resolve_required_facts(
        property_url=property_url,
        facts=facts,
        plan=plan,
        preferences=preferences,
    )
    refreshed = property_fact_requirement_plan(
        facts=merged,
        preferences=preferences,
        property_url=property_url,
    )
    strict_field = next(
        row
        for row in refreshed
        if row["key"] == "nearest_full_day_primary_school_m"
    )
    provider_receipt = next(
        row
        for row in receipt["provider_receipts"]
        if row["field_key"] == "nearest_full_day_primary_school_m"
    )

    assert strict_field["state"] == "unknown"
    assert receipt["status"] == "terminal_error"
    assert provider_receipt["provider"] == "schoolatlas"
    assert provider_receipt["status"] == "unavailable"


def test_legacy_underground_preference_canonicalizes_to_subway_hard_gate() -> None:
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/subway-home-1234567890/"
    )
    preferences = {
        "max_distance_to_underground_m": 500,
        "max_distance_to_underground_importance": "must_have",
    }
    normalized = product_service._property_search_canonical_distance_preferences(
        preferences
    )
    facts = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "nearest_subway_m": 720,
        "property_fact_evidence": {
            "nearest_subway_m": _valid_osm_evidence(
                key="nearest_subway_m",
                distance_m=720,
                property_url=property_url,
            )
        },
    }
    plan = property_fact_requirement_plan(
        facts=facts,
        preferences=normalized,
        include_resolved=True,
        property_url=property_url,
    )

    assert normalized["max_distance_to_subway_m"] == 500
    assert normalized["max_distance_to_subway_importance"] == "must_have"
    assert next(row for row in plan if row["key"] == "nearest_subway_m")[
        "state"
    ] == "resolved"
    assert product_service._property_candidate_passes_resolved_distance_gates(
        facts=facts,
        preferences=normalized,
        plan=plan,
    ) is False


@pytest.mark.parametrize(
    ("preferences", "facts", "canonical_key", "preference_key"),
    (
        (
            {
                "max_distance_to_ganztags_volksschule_m": 900,
                "max_distance_to_ganztags_volksschule_importance": "strong_wish",
            },
            {"nearest_school_m": 1_800},
            "nearest_full_day_primary_school_m",
            "max_distance_to_ganztags_volksschule_m",
        ),
        (
            {
                "max_distance_to_good_cafe_m": 700,
                "max_distance_to_good_cafe_importance": "nice_to_have",
            },
            {"nearest_cafe_m": 120},
            "nearest_good_cafe_m",
            "max_distance_to_good_cafe_m",
        ),
    ),
)
def test_strict_evidence_raw_aliases_cannot_gate_or_score(
    preferences: dict[str, object],
    facts: dict[str, object],
    canonical_key: str,
    preference_key: str,
) -> None:
    observed_at = datetime.now(timezone.utc)
    source_key = next(iter(facts))
    facts["property_fact_evidence"] = {
        source_key: {
            "provider": "openstreetmap_overpass",
            "observed_at": observed_at.isoformat(),
            "expires_at": (observed_at + timedelta(hours=24)).isoformat(),
            "source_fingerprint": "sha256:" + "e" * 64,
            "source_key": source_key,
            "coordinate_exact": True,
        }
    }
    plan = property_fact_requirement_plan(facts=facts, preferences=preferences)
    row = next(item for item in plan if item["key"] == canonical_key)

    adjustment, notes = product_service._property_distance_preference_score_adjustment(
        preferences=preferences,
        property_facts=facts,
    )
    gate_result = product_service._property_apply_distance_gate(
        facts,
        request_preferences=preferences,
        preference_key=preference_key,
        fact_key=source_key,
        label=str(row["label"]),
        fact_is_resolved=row["state"] == "resolved",
    )
    projection = property_fact_score_projection(
        candidate={"fit_score": 75},
        plan=plan,
        preferences=preferences,
    )

    assert row["state"] == "stale"
    assert gate_result is True
    assert adjustment == 0
    assert notes == ()
    assert projection["state"] == (
        "evaluating" if row["priority"] == "required" else "provisional"
    )


def test_nearby_provider_queries_supported_specialty_pois_but_not_unverified_good_cafes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_request: dict[str, str] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "elements": [
                    {"lat": 48.2090, "lon": 16.3738, "tags": {"brand": "Starbucks"}},
                    {"lat": 48.2100, "lon": 16.3738, "tags": {"leisure": "fitness_centre"}},
                    {"lat": 48.2110, "lon": 16.3738, "tags": {"amenity": "cinema"}},
                    {"lat": 48.2120, "lon": 16.3738, "tags": {"sport": "bouldering"}},
                    {"lat": 48.2130, "lon": 16.3738, "tags": {"leisure": "dog_park"}},
                ]
            }

    def _post(url: str, *, data: str, headers: dict[str, str], timeout: float):
        observed_request.update({"url": url, "data": data})
        return _Response()

    property_location_research._property_research_nearby_pois.cache_clear()
    monkeypatch.setattr(property_location_research.requests, "post", _post)
    result = property_location_research._property_research_nearby_pois(48.2082, 16.3738)
    property_location_research._property_research_nearby_pois.cache_clear()

    assert {
        "nearest_starbucks_m",
        "nearest_fitness_center_m",
        "nearest_cinema_m",
        "nearest_bouldering_m",
        "nearest_dog_park_m",
    }.issubset(result)
    decoded_query = urllib.parse.unquote(observed_request["data"])
    assert '["brand"~"^starbucks$",i]' in decoded_query
    assert '["leisure"="fitness_centre"]' in decoded_query
    assert '["amenity"="cinema"]' in decoded_query
    assert '["sport"~"^(climbing|bouldering)$"]' in decoded_query
    assert '["leisure"="dog_park"]' in decoded_query
    assert '["amenity"="cafe"]' not in decoded_query


def test_live_parser_merge_and_compact_restart_preserve_resolved_attested_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/compact-home-1234567890/"
    )
    latitude = 48.2082
    longitude = 16.3738

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "elements": [
                    {
                        "type": "node",
                        "id": 424242,
                        "version": 7,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "changeset": 99,
                        "lat": latitude + 0.002,
                        "lon": longitude,
                        "tags": {"shop": "supermarket", "name": "Signed Market"},
                    }
                ]
            }

    monkeypatch.setattr(
        property_location_research.requests,
        "post",
        lambda *args, **kwargs: _Response(),
    )
    property_location_research._property_research_nearby_pois.cache_clear()
    research = property_location_research._property_research_nearby_pois(
        latitude,
        longitude,
        property_url,
    )
    property_location_research._property_research_nearby_pois.cache_clear()
    preferences = {
        "max_distance_to_supermarket_m": 700,
        "max_distance_to_supermarket_importance": "must_have",
    }
    base_facts = {"map_lat": latitude, "map_lng": longitude}
    requested_plan = property_fact_requirement_plan(
        facts=base_facts,
        preferences=preferences,
        property_url=property_url,
    )
    merged, _labels, observed_keys = product_service._property_fact_merge_geo_research(
        property_url=property_url,
        facts=base_facts,
        requested_plan=requested_plan,
        research=research,
        geo_source_meta={
            "coordinate_basis": "candidate_listing_coordinates",
            "coordinate_observed_at": datetime.now(timezone.utc).isoformat(),
            "coordinate_precision": "address",
            "coordinate_source": "listing_structured_coordinates",
            "coordinate_exact": True,
            "listing_latitude": latitude,
            "listing_longitude": longitude,
            "coordinate_digest": property_fact_coordinate_digest(
                latitude,
                longitude,
            ),
        },
    )
    assert observed_keys == {"nearest_supermarket_m"}
    plan_before = property_fact_requirement_plan(
        facts=merged,
        preferences=preferences,
        property_url=property_url,
    )
    resolved_before = next(
        row for row in plan_before if row["key"] == "nearest_supermarket_m"
    )
    assert resolved_before["state"] == "resolved"

    facts_with_fillers = {
        **merged,
        **{f"filler_{index}": f"value-{index}" for index in range(45)},
    }
    raw_job = {
        "job_id": "pfe_" + "a" * 24,
        "status": "succeeded",
        "attempt": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_ref": "candidate-compact",
        "required_only": True,
        "fields": plan_before,
        "score": property_fact_score_projection(
            candidate={"fit_score": 70.0},
            plan=plan_before,
            preferences=preferences,
        ),
        "provider_receipts": product_service._property_fact_provider_receipts(
            plan_before
        ),
        "retryable": False,
    }
    compact = property_search_storage._compact_property_search_run_record(
        {
            "run_id": "run-compact",
            "principal_id": "principal-compact",
            "property_search_preferences": preferences,
            "summary": {
                "ranked_candidates": [
                    {
                        "candidate_ref": "candidate-compact",
                        "property_url": property_url,
                        "property_facts": facts_with_fillers,
                    }
                ],
                "fact_enrichment_jobs": {raw_job["job_id"]: raw_job},
                "required_fact_enrichment_candidate_jobs": {
                    "candidate-compact": raw_job["job_id"]
                },
            },
        }
    )
    restarted_candidate = compact["summary"]["ranked_candidates"][0]
    restarted_facts = restarted_candidate["property_facts"]
    restarted_plan = property_fact_requirement_plan(
        facts=restarted_facts,
        preferences=preferences,
        property_url=property_url,
    )
    restarted_row = next(
        row for row in restarted_plan if row["key"] == "nearest_supermarket_m"
    )
    assert restarted_facts["nearest_supermarket_m"] == merged[
        "nearest_supermarket_m"
    ]
    assert restarted_row["state"] == "resolved"
    restarted_job = compact["summary"]["fact_enrichment_jobs"][raw_job["job_id"]]
    assert _property_fact_safe_job_payload(restarted_job)["fields"][0][
        "provenance"
    ] == _property_fact_safe_job_payload(raw_job)["fields"][0]["provenance"]
    public_payload = _property_fact_safe_job_payload(restarted_job)
    public_payload.update(
        {
            "run_id": "run-compact",
            "candidate_ref": "candidate-compact",
            "status_url": "/app/api/signals/property/search/run/run-compact/candidates/candidate-compact/fact-enrichment",
        }
    )
    PropertyFactEnrichmentOut.model_validate(public_payload)


def test_provider_attestation_rejects_every_bound_field_mutation_and_replay() -> None:
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/sealed-home-1234567890/"
    )
    evidence = _valid_osm_evidence(
        key="nearest_supermarket_m",
        distance_m=280,
        property_url=property_url,
    )
    facts = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "nearest_supermarket_m": 280,
    }
    spec = next(
        row
        for row in property_fact_distance_specs()
        if row["key"] == "nearest_supermarket_m"
    )

    def is_valid(candidate: dict[str, object], value: int = 280) -> bool:
        return property_fact_distance_evidence_is_valid(
            facts={**facts, "nearest_supermarket_m": value},
            evidence=candidate,
            spec=spec,
            observed_source_key="nearest_supermarket_m",
            observed_value=value,
            property_url=property_url,
        )

    assert is_valid(evidence) is True
    mutations: dict[str, object] = {
        "provider": "wrong_provider",
        "listing_url": property_url + "other",
        "source_fingerprint": "sha256:" + "0" * 64,
        "source_key": "nearest_playground_m",
        "observed_key": "nearest_playground_m",
        "listing_latitude": 48.3,
        "listing_longitude": 16.4,
        "coordinate_digest": "sha256:" + "1" * 64,
        "query_endpoint_url": "https://overpass-api.de/api/other",
        "query_digest": "sha256:" + "2" * 64,
        "query_schema": "propertyquarry.osm-nearby.v999",
        "receipt_url": "https://api.openstreetmap.org/api/0.6/node/1/1",
        "provider_object_id": "1",
        "provider_object_type": "way",
        "provider_object_version": 2,
        "provider_object_timestamp": "2020-01-01T00:00:00+00:00",
        "provider_object_changeset": "654321",
        "provider_observed_at": "2020-01-01T00:00:00+00:00",
        "provider_expires_at": "2030-01-01T00:00:00+00:00",
        "observed_at": "2020-01-01T00:00:00+00:00",
        "expires_at": "2030-01-01T00:00:00+00:00",
        "poi_latitude": 48.4,
        "poi_longitude": 16.5,
        "attestation_version": "propertyquarry.provider-fact-attestation.v0",
        "provider_attestation": "hmac-sha256:" + "f" * 64,
    }
    for field, replacement in mutations.items():
        tampered = copy.deepcopy(evidence)
        tampered[field] = replacement
        assert is_valid(tampered) is False, field
    tampered_tags = copy.deepcopy(evidence)
    tampered_tags["poi_classification_tags"] = {"shop": "convenience"}
    assert is_valid(tampered_tags) is False
    assert is_valid(copy.deepcopy(evidence), value=281) is False
    unsigned = copy.deepcopy(evidence)
    unsigned.pop("provider_attestation")
    assert is_valid(unsigned) is False

    future = _valid_osm_evidence(
        key="nearest_supermarket_m",
        distance_m=280,
        property_url=property_url,
        observed_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert is_valid(future) is False
    oversized = copy.deepcopy(evidence)
    oversized_expiry = (
        datetime.fromisoformat(str(oversized["observed_at"]))
        + timedelta(hours=25)
    ).isoformat()
    oversized["expires_at"] = oversized_expiry
    oversized["provider_expires_at"] = oversized_expiry
    oversized["provider_attestation"] = property_fact_issue_provider_attestation(
        oversized,
        observed_value=280,
    )
    assert is_valid(oversized) is False


def test_provider_attestation_prod_secret_is_durable_and_rotation_invalidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = app_settings.get_settings()
    prod_runtime = replace(original.runtime, mode="prod")
    missing = replace(
        original,
        runtime=prod_runtime,
        auth=replace(original.auth, signing_secret=""),
    )
    monkeypatch.setattr(app_settings, "get_settings", lambda: missing)
    unsigned = _valid_osm_evidence(
        key="nearest_supermarket_m",
        distance_m=280,
        property_url=(
            "https://www.willhaben.at/iad/immobilien/d/secret-home-1234567890/"
        ),
    )
    assert unsigned["provider_attestation"] == ""

    first_process = replace(
        missing,
        auth=replace(original.auth, signing_secret="a" * 48),
    )
    monkeypatch.setattr(app_settings, "get_settings", lambda: first_process)
    sealed = _valid_osm_evidence(
        key="nearest_supermarket_m",
        distance_m=280,
        property_url=(
            "https://www.willhaben.at/iad/immobilien/d/secret-home-1234567890/"
        ),
    )
    restarted_process = replace(
        original,
        runtime=prod_runtime,
        auth=replace(original.auth, signing_secret="a" * 48),
    )
    monkeypatch.setattr(app_settings, "get_settings", lambda: restarted_process)
    assert product_service.property_fact_distance_evidence_is_valid(
        facts={
            "map_lat": 48.2082,
            "map_lng": 16.3738,
            "nearest_supermarket_m": 280,
        },
        evidence=sealed,
        spec=next(
            row
            for row in property_fact_distance_specs()
            if row["key"] == "nearest_supermarket_m"
        ),
        observed_source_key="nearest_supermarket_m",
        observed_value=280,
        property_url=sealed["listing_url"],
    ) is True
    rotated = replace(
        restarted_process,
        auth=replace(original.auth, signing_secret="b" * 48),
    )
    monkeypatch.setattr(app_settings, "get_settings", lambda: rotated)
    assert property_fact_distance_evidence_is_valid(
        facts={
            "map_lat": 48.2082,
            "map_lng": 16.3738,
            "nearest_supermarket_m": 280,
        },
        evidence=sealed,
        spec=next(
            row
            for row in property_fact_distance_specs()
            if row["key"] == "nearest_supermarket_m"
        ),
        observed_source_key="nearest_supermarket_m",
        observed_value=280,
        property_url=sealed["listing_url"],
    ) is False


def test_safe_public_fact_projection_bounds_untrusted_classification_tags() -> None:
    provenance = _valid_osm_evidence(
        key="nearest_supermarket_m",
        distance_m=280,
        property_url=(
            "https://www.willhaben.at/iad/immobilien/d/tag-home-1234567890/"
        ),
    )
    provenance["poi_classification_tags"] = {
        f"tag_{index}": f"value_{index}" for index in range(20)
    }
    safe = _property_fact_safe_job_payload(
        {
            "job_id": "pfe_" + "c" * 24,
            "status": "succeeded",
            "attempt": 1,
            "fields": [
                {
                    "key": "nearest_supermarket_m",
                    "label": "Supermarket distance",
                    "provider": "openstreetmap_overpass",
                    "state": "stale",
                    "priority": "lazy",
                    "affects_score": True,
                    "provenance": provenance,
                }
            ],
            "score": {},
        }
    )
    projected = safe["fields"][0]["provenance"]
    assert len(projected["poi_classification_tags"]) == 8
    PropertyFactProvenanceOut.model_validate(projected)


def test_legacy_underground_and_pharmacy_controls_roundtrip_exact_fields() -> None:
    form: dict[str, object] = {"fields": []}
    landing_view_models._complete_property_distance_form_controls(
        form,
        preferences={
            "max_distance_to_underground_m": 333,
            "max_distance_to_underground_importance": "important",
            "max_distance_to_pharmacy_m": 444,
            "max_distance_to_pharmacy_importance": "avoid",
        },
    )
    contract = {
        str(row[0]): list(row)
        for row in list(form["distance_preference_contract"])
        if isinstance(row, list) and len(row) == 3
    }
    assert contract["max_distance_to_subway_m"] == [
        "max_distance_to_subway_m",
        333,
        "strong_wish",
    ]
    assert contract["max_distance_to_pharmacy_m"] == [
        "max_distance_to_pharmacy_m",
        444,
        "avoid",
    ]
    script = Path(
        "ea/app/templates/app/_property_workbench_script.html"
    ).read_text(encoding="utf-8")
    assert "'pharmacy nearby': 'max_distance_to_pharmacy_m'" in script
    assert "'pharmacy nearby': 'max_distance_to_pharmacy_importance'" in script


@pytest.mark.parametrize("importance", ("must_have", "strong_wish", "avoid"))
def test_explicit_blocking_distance_importance_holds_ranking(
    importance: str,
) -> None:
    preferences = {
        "max_distance_to_supermarket_m": 300,
        "max_distance_to_supermarket_importance": importance,
    }
    plan = property_fact_requirement_plan(facts={}, preferences=preferences)
    supermarket = next(row for row in plan if row["key"] == "nearest_supermarket_m")
    projection = property_fact_score_projection(
        candidate={"fit_score": 75},
        plan=plan,
        preferences=preferences,
    )

    assert supermarket["priority"] == "required"
    assert projection["state"] == "evaluating"
    assert projection["ranking_eligible"] is False


def test_explicit_nice_to_have_distance_stays_lazy_over_stored_profile_default() -> None:
    preferences = {
        "max_distance_to_supermarket_m": 300,
        "max_distance_to_supermarket_importance": "nice_to_have",
    }
    plan = property_fact_requirement_plan(
        facts={},
        preferences=preferences,
        preference_nodes=(
            {
                "category": "constraint",
                "key": "prefer_supermarket_nearby",
                "strength": "high",
                "value": True,
            },
        ),
    )
    supermarket = next(row for row in plan if row["key"] == "nearest_supermarket_m")
    projection = property_fact_score_projection(
        candidate={"fit_score": 75},
        plan=plan,
        preferences=preferences,
    )

    assert supermarket["priority"] == "lazy"
    assert projection["state"] == "provisional"
    assert projection["ranking_eligible"] is True


def test_rank_projection_excludes_evaluating_candidate() -> None:
    ready = {**_candidate(candidate_ref="ready"), "score_state": "provisional", "ranking_eligible": True}
    evaluating = {
        **_candidate(candidate_ref="evaluating"),
        "fit_score": 99,
        "score_state": "evaluating",
        "ranking_eligible": False,
    }

    ranked = _property_search_ranked_candidates_from_sources(
        [
            {
                "source_label": "Willhaben",
                "top_candidates": [ready],
                "research_candidates": [ready, evaluating],
            }
        ]
    )

    assert [row["candidate_ref"] for row in ranked] == ["ready"]
    assert ranked[0]["rank"] == 1


def test_query_inferred_address_coordinates_cannot_finalize_required_distance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime.now(timezone.utc)
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/example-1234567890/"
    )
    facts: dict[str, object] = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "map_location_precision": "address",
        "map_location_source": "nominatim_query_inferred",
        "house_number": "12",
    }
    monkeypatch.setattr(
        product_service,
        "_property_fact_coordinate_snapshot",
        lambda _property_url: dict(facts),
    )
    monkeypatch.setattr(
        product_service,
        "_property_research_nearby_pois",
        lambda _lat, _lng, _property_url: {"nearest_supermarket_m": 240},
    )

    nearby, coordinate_meta = _property_fact_fresh_geo_snapshot(
        property_url=property_url,
        facts=facts,
    )
    facts.update(nearby)
    facts["property_fact_evidence"] = {
        "nearest_supermarket_m": {
            "provider": "openstreetmap_overpass",
            "observed_at": observed_at.isoformat(),
            "expires_at": (observed_at + timedelta(hours=24)).isoformat(),
            "source_key": "nearest_supermarket_m",
            "source_fingerprint": property_fact_source_fingerprint(property_url),
            **coordinate_meta,
        }
    }
    plan = property_fact_requirement_plan(
        facts=facts,
        preferences={
            "max_distance_to_supermarket_m": 800,
            "max_distance_to_supermarket_importance": "must_have",
        },
        property_url=property_url,
    )
    supermarket = next(row for row in plan if row["key"] == "nearest_supermarket_m")
    projection = property_fact_score_projection(
        candidate={"fit_score": 91.0},
        plan=plan,
        preferences={
            "max_distance_to_supermarket_m": 800,
            "max_distance_to_supermarket_importance": "must_have",
        },
    )

    assert coordinate_meta["coordinate_exact"] is False
    assert supermarket["state"] == "stale"
    assert supermarket["priority"] == "required"
    assert projection["state"] == "evaluating"
    assert projection["current"] is None
    assert projection["ranking_eligible"] is False


def test_coordinate_snapshot_improves_valid_but_inexact_preview_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/"
        "coordinate-refresh-home-1234567890/"
    )
    monkeypatch.setattr(
        product_service,
        "_property_scout_page_preview",
        lambda *_args, **_kwargs: {
            "title": "Apartment at Example Street 12",
            "summary": "1020 Vienna",
            "property_facts_json": {
                "map_lat": 48.20,
                "map_lng": 16.37,
                "map_location_precision": "postal_area",
                "map_location_source": "postal_area_centroid",
                "house_number": "12",
            },
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_research_location_hint_queries",
        lambda **_kwargs: ["Example Street 12, 1020 Vienna"],
    )
    monkeypatch.setattr(
        product_service,
        "_property_research_forward_geocode",
        lambda _query, country_code="": {
            "lat": "48.211",
            "lon": "16.381",
            "addresstype": "house",
            "address": {
                "house_number": "12",
                "road": "Example Street",
                "postcode": "1020",
                "city": "Vienna",
                "country_code": "at",
            },
            "display_name": "Example Street 12, 1020 Vienna",
        },
    )

    snapshot = product_service._property_fact_coordinate_snapshot(property_url)

    assert snapshot["map_lat"] == 48.211
    assert snapshot["map_lng"] == 16.381
    assert snapshot["map_location_precision"] == "address"
    coordinate_evidence = dict(snapshot["map_coordinate_evidence"])
    assert coordinate_evidence["exact"] is True
    assert coordinate_evidence["trusted"] is True
    assert coordinate_evidence["provider"] == "nominatim_structured_result"
    assert coordinate_evidence["source_fingerprint"] == (
        property_fact_source_fingerprint(property_url)
    )
    assert coordinate_evidence["result_type"] == "house"
    assert coordinate_evidence["country_code"] == "at"
    assert snapshot["map_house_number"] == "12"
    assert coordinate_evidence["coordinate_digest"] == (
        property_fact_coordinate_digest(48.211, 16.381)
    )


def test_exact_geocoder_rejects_same_house_and_street_in_wrong_city(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "Hauptstraße 12, 1010 Wien"
    wrong = {
        "lat": "47.0707",
        "lon": "15.4395",
        "addresstype": "house",
        "address": {
            "house_number": "12",
            "road": "Hauptstraße",
            "postcode": "8010",
            "city": "Graz",
            "country_code": "at",
        },
    }
    correct = {
        "lat": "48.2082",
        "lon": "16.3738",
        "addresstype": "house",
        "address": {
            "house_number": "12",
            "road": "Hauptstraße",
            "postcode": "1010",
            "city": "Wien",
            "country_code": "at",
        },
    }
    wrong_locality = copy.deepcopy(correct)
    wrong_locality["address"]["city"] = "Graz"
    missing_locality = copy.deepcopy(correct)
    missing_locality["address"].pop("city")
    assert product_service._property_fact_geocode_is_exact(
        query,
        wrong,
        expected_country_code="at",
    ) is False
    assert product_service._property_fact_geocode_is_exact(
        query,
        wrong_locality,
        expected_country_code="at",
    ) is False
    assert product_service._property_fact_geocode_is_exact(
        query,
        missing_locality,
        expected_country_code="at",
    ) is False
    assert product_service._property_fact_geocode_is_exact(
        query,
        correct,
        expected_country_code="at",
    ) is True

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps([wrong, correct]).encode("utf-8")

    property_location_research._property_research_forward_geocode.cache_clear()
    monkeypatch.setattr(
        property_location_research.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(),
    )
    selected = property_location_research._property_research_forward_geocode(
        query,
        "at",
    )
    property_location_research._property_research_forward_geocode.cache_clear()
    assert selected["address"]["postcode"] == "1010"
    assert selected["address"]["city"] == "Wien"


def test_fresh_geo_snapshot_never_relabels_coordinates_from_another_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_url = (
        "https://www.willhaben.at/iad/immobilien/d/"
        "coordinate-source-home-1234567890/"
    )
    current_url = (
        "https://www.willhaben.at/iad/immobilien/d/"
        "coordinate-source-home-1234567891/"
    )
    facts = {
        "map_lat": 48.20,
        "map_lng": 16.37,
        "map_location_precision": "address",
        "map_location_source": "listing_preview",
        "map_coordinate_evidence": {
            "exact": True,
            "trusted": True,
            "provider": "listing_preview",
            "source_fingerprint": property_fact_source_fingerprint(original_url),
        },
    }
    refresh_calls: list[str] = []
    refreshed = {
        "map_lat": 48.22,
        "map_lng": 16.39,
        "map_location_precision": "address",
        "map_location_source": "listing_structured_coordinates",
        "map_coordinate_evidence": {
            "exact": True,
            "trusted": True,
            "provider": "listing_structured_coordinates",
            "source_fingerprint": property_fact_source_fingerprint(current_url),
            "latitude": 48.22,
            "longitude": 16.39,
            "coordinate_digest": property_fact_coordinate_digest(48.22, 16.39),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=6)
            ).isoformat(),
        },
    }
    observed_coordinates: list[tuple[float, float]] = []
    monkeypatch.setattr(
        product_service,
        "_property_fact_coordinate_snapshot",
        lambda property_url: refresh_calls.append(property_url) or dict(refreshed),
    )
    monkeypatch.setattr(
        product_service,
        "_property_research_nearby_pois",
        lambda latitude, longitude, _property_url: (
            observed_coordinates.append((latitude, longitude))
            or {"nearest_supermarket_m": 220}
        ),
    )

    nearby, meta = _property_fact_fresh_geo_snapshot(
        property_url=current_url,
        facts=facts,
    )

    assert nearby == {"nearest_supermarket_m": 220}
    assert refresh_calls == [current_url]
    assert observed_coordinates == [(48.22, 16.39)]
    assert meta["coordinate_exact"] is True
    assert dict(meta["coordinate_updates"])["map_coordinate_evidence"] == (
        refreshed["map_coordinate_evidence"]
    )


def test_fresh_geo_snapshot_rederives_even_current_url_bound_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/"
        "coordinate-reuse-home-1234567890/"
    )
    facts = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "map_location_precision": "address",
        "map_location_source": "listing_preview",
        "map_coordinate_evidence": {
            "exact": True,
            "trusted": True,
            "provider": "listing_preview",
            "source_fingerprint": property_fact_source_fingerprint(property_url),
        },
    }
    observed_coordinates: list[tuple[float, float]] = []
    refreshed_at = datetime.now(timezone.utc)
    refreshed = {
        **facts,
        "map_location_source": "listing_structured_coordinates",
        "map_coordinate_evidence": {
            "exact": True,
            "trusted": True,
            "provider": "listing_structured_coordinates",
            "source_fingerprint": property_fact_source_fingerprint(property_url),
            "latitude": 48.2082,
            "longitude": 16.3738,
            "coordinate_digest": property_fact_coordinate_digest(48.2082, 16.3738),
            "observed_at": refreshed_at.isoformat(),
            "expires_at": (refreshed_at + timedelta(hours=6)).isoformat(),
        },
    }
    refresh_calls: list[str] = []
    monkeypatch.setattr(
        product_service,
        "_property_fact_coordinate_snapshot",
        lambda current_url: refresh_calls.append(current_url) or dict(refreshed),
    )
    monkeypatch.setattr(
        product_service,
        "_property_research_nearby_pois",
        lambda latitude, longitude, _property_url: (
            observed_coordinates.append((latitude, longitude)) or {}
        ),
    )

    _nearby, meta = _property_fact_fresh_geo_snapshot(
        property_url=property_url,
        facts=facts,
    )

    assert observed_coordinates == [(48.2082, 16.3738)]
    assert refresh_calls == [property_url]
    assert meta["coordinate_exact"] is True
    assert dict(meta["coordinate_updates"])["map_coordinate_evidence"] == (
        refreshed["map_coordinate_evidence"]
    )


@pytest.mark.parametrize("importance", ("must_have", "strong_wish", "avoid"))
def test_unknown_required_distance_never_hard_fails_before_research(
    importance: str,
) -> None:
    facts: dict[str, object] = {}

    accepted = product_service._property_apply_distance_gate(
        facts,
        request_preferences={
            "max_distance_to_supermarket_m": 500,
            "max_distance_to_supermarket_importance": importance,
        },
        preference_key="max_distance_to_supermarket_m",
        fact_key="nearest_supermarket_m",
        label="supermarket",
    )

    assert accepted is True
    assert facts["distance_unknowns_json"] == [
        {"label": "supermarket", "requested_m": 500}
    ]


def test_shared_search_score_is_two_call_idempotent_and_reproducible() -> None:
    candidate = {
        "title": "Quiet family apartment",
        "summary": "82 m2 with floorplan in Vienna",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/example-1234567890/",
    }
    assessment = {
        "fit_score": 61.0,
        "recommendation": "review",
        "unknowns_json": ["Energy certificate pending"],
        "upstream_personalization": {"adjusted_fit_score": 66.0},
    }
    preferences = {
        "max_distance_to_supermarket_m": 500,
        "max_distance_to_supermarket_importance": "strong_wish",
        "apply_unknowns_penalty": True,
    }
    facts = {
        "area_sqm": 82,
        "has_floorplan": True,
        "postal_name": "1020 Wien",
        "nearest_supermarket_m": 220,
        "discovery_soft_penalty_points": 1.5,
    }

    first = product_service._property_search_score_candidate(
        candidate=candidate,
        assessment=assessment,
        property_facts=facts,
        preferences=preferences,
        preview={
            "title": candidate["title"],
            "summary": candidate["summary"],
            "property_facts_json": dict(facts),
        },
        ordinal=3,
        location_penalty_points=0.0,
        apply_unknowns_penalty=True,
    )
    first_facts = copy.deepcopy(facts)
    second = product_service._property_search_score_candidate(
        candidate={**candidate, **first},
        assessment=assessment,
        property_facts=facts,
        preferences=preferences,
    )

    assert second == first
    assert facts == first_facts
    assert first["score_provenance"]["algorithm_version"] == (
        product_service._PROPERTY_SEARCH_SCORE_ALGORITHM_VERSION
    )
    assert first["score_provenance"]["facts_digest"] == second["score_provenance"][
        "facts_digest"
    ]


@pytest.mark.parametrize("provider_resolves", (True, False))
def test_required_fact_search_pass_blocks_terminal_ranking_until_resolved(
    monkeypatch: pytest.MonkeyPatch,
    provider_resolves: bool,
) -> None:
    principal_id = f"exec-required-fact-search-{provider_resolves}"
    client = build_property_operator_client(principal_id=principal_id)
    start_workspace(
        client,
        mode="personal",
        workspace_name="Required Fact Search Office",
    )
    service = ProductService(client.app.state.container)
    source_url = (
        "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/"
        "wien-1020-leopoldstadt"
    )
    listing_url = (
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/"
        "wien-1020-leopoldstadt/required-fact-home-1234567890/"
    )
    monkeypatch.setattr(
        product_service,
        "_merged_property_scout_source_specs",
        lambda **kwargs: [
            {
                "url": source_url,
                "label": "Willhaben | Austria | Rent | 1020 Vienna",
                "platform": "willhaben",
                "provider_family": "marketplace",
                "country_code": "AT",
                "max_results": 1,
            }
        ],
    )
    monkeypatch.setattr(
        product_service,
        "_property_search_interleave_by_provider_group",
        lambda specs: list(specs),
    )
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": [listing_url],
                "provider_cache_state": {
                    "status": "miss",
                    "cache_key": "willhaben:required-fact",
                },
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )
    preview = {
        "listing_id": "required-fact-home-1234567890",
        "title": "Mietwohnung in 1020 Wien mit Balkon",
        "summary": "82 m2, 3 Zimmer, Gesamtmiete EUR 1.650, Balkon.",
        "property_facts_json": {
            "postal_name": "1020 Wien",
            "area_sqm": 82,
            "rooms": 3,
            "total_rent_eur": 1650,
            "map_lat": 48.2082,
            "map_lng": 16.3738,
            "map_location_precision": "address",
            "map_location_source": "listing",
            "house_number": "12",
        },
    }
    monkeypatch.setattr(
        product_service,
        "_property_scout_page_preview_with_timeout",
        lambda property_url, *, prefer_fast=False: copy.deepcopy(preview),
    )
    monkeypatch.setattr(
        ProductService,
        "_warm_property_public_preview_cache_for_sources",
        lambda self, **kwargs: {},
    )
    monkeypatch.setattr(
        product_service,
        "_property_fact_validated_source_url",
        lambda url: str(url),
    )
    provider_calls: list[str] = []

    def _fresh_required_facts(*, property_url: str, facts: dict[str, object]):
        provider_calls.append(property_url)
        return (
            _valid_osm_research(
                {"nearest_supermarket_m": 280},
                property_url=property_url,
            )
            if provider_resolves
            else ({}, {})
        )

    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        _fresh_required_facts,
    )
    score_calls: list[str] = []

    def _fit(**kwargs) -> dict[str, object]:
        score_calls.append(str(kwargs.get("property_url") or ""))
        return {
            "fit_score": 62.0,
            "recommendation": "review",
            "match_reasons_json": ["Core brief matches."],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
        }

    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        _fit,
    )
    monkeypatch.setattr(
        ProductService,
        "_open_property_alert_review_with_timeout",
        lambda self, **kwargs: {
            "status": "deferred",
            "reason": "test",
            "review_reused": False,
        },
    )

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "property_type": "apartment",
            "max_distance_to_supermarket_m": 500,
            "max_distance_to_supermarket_importance": "must_have",
            "use_stored_feedback_preferences": False,
        },
        max_results_per_source=1,
        force_refresh=True,
    )

    assert provider_calls == [listing_url]
    source = dict(result["sources"][0])
    if provider_resolves:
        assert result["status"] == "processed"
        assert result["required_fact_research_resolved_total"] == 1
        assert result["required_fact_research_pending_total"] == 0
        assert result["evaluating_candidate_total"] == 0
        assert score_calls == [listing_url]
        candidate = dict(source["top_candidates"][0])
        assert candidate["ranking_eligible"] is True
        assert dict(candidate["property_facts"])["nearest_supermarket_m"] == 280
        assert dict(candidate["score_projection"])["current"] is not None
    else:
        assert result["status"] == "completed_partial"
        assert result["required_fact_research_pending_total"] == 1
        assert result["required_fact_resolution_pending"] is True
        assert result["results_delivery_blocked_reason"] == (
            "required_property_facts_unresolved"
        )
        assert source["top_candidates"] == []
        assert source["evaluating_candidate_total"] == 1
        assert score_calls == []
        evaluating = dict(source["evaluating_candidates"][0])
        assert evaluating["ranking_eligible"] is False
        assert evaluating["score_state"] == "evaluating"
        assert service._property_search_results_delivery_pending(result=result) is True


def test_required_fact_scheduler_survives_restart_and_unblocks_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-required-fact-durable-success"
    run_id = "run-required-fact-durable-success"
    candidate_ref = "candidate-required-durable"
    record = _required_fact_run_record(
        principal_id=principal_id,
        run_id=run_id,
        candidate_ref=candidate_ref,
    )
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=record,
    )
    client = build_property_operator_client(principal_id=principal_id)
    provider_calls: list[str] = []
    provider_results = [{}, {"nearest_supermarket_m": 280}]

    def _provider(*, property_url: str, facts: dict[str, object]):
        provider_calls.append(property_url)
        result = provider_results.pop(0)
        return (
            _valid_osm_research(result, property_url=property_url)
            if result
            else ({}, {})
        )

    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        _provider,
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {
            "domain": "willhaben",
            "fit_score": 68.0,
            "recommendation": "shortlist",
            "match_reasons_json": ["Required distance verified."],
        },
    )
    notification_calls: list[str] = []
    monkeypatch.setattr(
        ProductService,
        "_notify_property_search_results_ready",
        lambda self, **kwargs: notification_calls.append(str(kwargs.get("run_id") or "")),
    )
    try:
        first_service = ProductService(client.app.state.container)
        first_result = first_service.reconcile_property_search_results_delivery(
            principal_id=principal_id,
            allow_notifications=False,
        )
        _, first_job = _active_required_fact_job(
            store,
            candidate_ref=candidate_ref,
        )
        first_summary = dict(dict(store["record"]).get("summary") or {})
        first_compact = property_search_storage._compact_property_search_run_record(
            copy.deepcopy(dict(store["record"]))
        )

        assert first_result["pending"] == 1
        assert first_job["attempt"] == 2
        assert first_job["status"] == "retryable_error"
        assert first_job["retryable"] is True
        assert first_summary["required_fact_resolution_pending"] is True
        assert first_compact["summary"]["results_delivery_blocked_reason"] == (
            "required_property_facts_unresolved"
        )
        assert property_search_storage._property_search_run_compact_supports_delivery(
            first_compact
        ) is True

        _age_required_fact_job_retry(store, candidate_ref=candidate_ref)
        _clear_run(run_id)
        restarted_service = ProductService(client.app.state.container)
        second_result = restarted_service.reconcile_property_search_results_delivery(
            principal_id=principal_id,
            allow_notifications=False,
        )
        _, final_job = _active_required_fact_job(
            store,
            candidate_ref=candidate_ref,
        )
        persisted = dict(store["record"])
        summary = dict(persisted.get("summary") or {})
        ranked = [
            dict(row)
            for row in list(summary.get("ranked_candidates") or [])
            if isinstance(row, dict)
        ]

        assert second_result["pending"] == 0
        assert provider_calls == [
            str(record["summary"]["sources"][0]["research_candidates"][0]["property_url"]),
            str(record["summary"]["sources"][0]["research_candidates"][0]["property_url"]),
        ]
        assert final_job["attempt"] == 3
        assert final_job["status"] == "succeeded"
        assert persisted["status"] == "processed"
        assert summary["required_fact_resolution_pending"] is False
        assert summary["required_fact_resolution_exhausted"] is False
        assert summary["evaluating_candidate_total"] == 0
        assert [row["candidate_ref"] for row in ranked] == [candidate_ref]
        assert ranked[0]["ranking_eligible"] is True
        assert notification_calls == []
    finally:
        _clear_run(run_id)


def test_required_fact_exhaustion_is_terminal_partial_and_never_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-required-fact-durable-exhausted"
    run_id = "run-required-fact-durable-exhausted"
    candidate_ref = "candidate-required-durable"
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=_required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        ),
    )
    client = build_property_operator_client(principal_id=principal_id)
    provider_calls: list[str] = []
    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        lambda **kwargs: (
            provider_calls.append(str(kwargs.get("property_url") or "")) or {},
            {
                "coordinate_basis": "candidate_listing_coordinates",
                "coordinate_observed_at": "",
                "coordinate_precision": "address",
                "coordinate_source": "listing",
                "coordinate_exact": True,
            },
        ),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {"fit_score": 64.0, "recommendation": "review"},
    )
    notification_calls: list[str] = []
    monkeypatch.setattr(
        ProductService,
        "_notify_property_search_results_ready",
        lambda self, **kwargs: notification_calls.append(str(kwargs.get("run_id") or "")),
    )
    monkeypatch.setattr(
        ProductService,
        "_recent_product_event_exists",
        lambda self, **kwargs: False,
    )
    try:
        service = ProductService(client.app.state.container)
        service.reconcile_property_search_results_delivery(
            principal_id=principal_id,
            allow_notifications=False,
        )
        _age_required_fact_job_retry(store, candidate_ref=candidate_ref)
        _clear_run(run_id)
        restarted_service = ProductService(client.app.state.container)
        restarted_service.reconcile_property_search_results_delivery(
            principal_id=principal_id,
            allow_notifications=False,
        )
        _, job = _active_required_fact_job(store, candidate_ref=candidate_ref)
        persisted = dict(store["record"])
        summary = dict(persisted.get("summary") or {})
        provider_receipt = next(
            row
            for row in job["provider_receipts"]
            if row["field_key"] == "nearest_supermarket_m"
        )

        assert len(provider_calls) == 2
        assert job["attempt"] == 3
        assert job["status"] == "terminal_error"
        assert job["retryable"] is False
        assert provider_receipt["provider"] == "openstreetmap_overpass"
        assert provider_receipt["status"] == "unavailable"
        assert provider_receipt["reason_code"] == (
            "fact_enrichment_attempts_exhausted"
        )
        assert persisted["status"] == "completed_partial"
        assert summary["required_fact_resolution_pending"] is False
        assert summary["required_fact_resolution_exhausted"] is True
        assert summary["results_delivery_semantically_blocked"] is True
        assert summary["results_delivery_blocked_reason"] == (
            "required_property_facts_unresolved"
        )
        assert summary["completion_reason"] == (
            "required_fact_resolution_exhausted"
        )
        assert restarted_service._property_search_results_delivery_pending(
            result=summary
        ) is False
        assert restarted_service._property_search_results_delivery_semantically_blocked(
            result=summary
        ) is True

        restarted_service.reconcile_property_search_results_delivery(
            principal_id=principal_id,
            allow_notifications=True,
        )
        assert notification_calls == []
    finally:
        _clear_run(run_id)


def test_resolved_required_fact_outside_must_have_limit_is_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-required-fact-hard-gate"
    run_id = "run-required-fact-hard-gate"
    candidate_ref = "candidate-required-durable"
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=_required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        ),
    )
    client = build_property_operator_client(principal_id=principal_id)
    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        lambda **kwargs: _valid_osm_research(
            {"nearest_supermarket_m": 900},
            property_url=str(kwargs.get("property_url") or ""),
        ),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {"fit_score": 72.0, "recommendation": "shortlist"},
    )
    try:
        service = ProductService(client.app.state.container)
        service.reconcile_property_search_results_delivery(
            principal_id=principal_id,
            allow_notifications=False,
        )
        persisted = dict(store["record"])
        summary = dict(persisted.get("summary") or {})
        source = dict(summary["sources"][0])
        research_candidate = dict(source["research_candidates"][0])

        assert persisted["status"] == "processed"
        assert summary["required_fact_resolution_pending"] is False
        assert summary["evaluating_candidate_total"] == 0
        assert summary["ranked_candidates"] == []
        assert source["top_candidates"] == []
        assert research_candidate["ranking_eligible"] is False
        assert research_candidate["evaluation_state"] == (
            "excluded_required_fact_mismatch"
        )
    finally:
        _clear_run(run_id)


def test_score_recompute_failure_keeps_required_hold_across_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-required-fact-score-retry"
    run_id = "run-required-fact-score-retry"
    candidate_ref = "candidate-required-durable"
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=_required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        ),
    )
    client = build_property_operator_client(principal_id=principal_id)
    provider_calls: list[str] = []
    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        lambda **kwargs: (
            provider_calls.append(str(kwargs.get("property_url") or ""))
            or _valid_osm_research(
                {"nearest_supermarket_m": 280},
                property_url=str(kwargs.get("property_url") or ""),
            )
        ),
    )
    assessment_results = [
        {},
        {"fit_score": 70.0, "recommendation": "shortlist"},
    ]
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: assessment_results.pop(0),
    )
    try:
        service = ProductService(client.app.state.container)
        service.reconcile_property_search_results_delivery(
            principal_id=principal_id,
            allow_notifications=False,
        )
        _, first_job = _active_required_fact_job(
            store,
            candidate_ref=candidate_ref,
        )
        first_summary = dict(dict(store["record"]).get("summary") or {})

        assert first_job["status"] == "retryable_error"
        assert first_job["score_recompute_required"] is True
        assert first_summary["required_fact_resolution_pending"] is True
        assert first_summary["evaluating_candidate_total"] == 1
        assert first_summary["blocked_required_facts"][0]["field_keys"] == [
            "required_score_recompute"
        ]

        _age_required_fact_job_retry(store, candidate_ref=candidate_ref)
        _clear_run(run_id)
        restarted_service = ProductService(client.app.state.container)
        restarted_service.reconcile_property_search_results_delivery(
            principal_id=principal_id,
            allow_notifications=False,
        )
        _, final_job = _active_required_fact_job(
            store,
            candidate_ref=candidate_ref,
        )
        final_summary = dict(dict(store["record"]).get("summary") or {})

        assert len(provider_calls) == 1
        assert final_job["attempt"] == 3
        assert final_job["status"] == "succeeded"
        assert final_job["score_recompute_required"] is False
        assert final_summary["required_fact_resolution_pending"] is False
        assert final_summary["evaluating_candidate_total"] == 0
        assert len(final_summary["ranked_candidates"]) == 1
    finally:
        _clear_run(run_id)


def test_stale_succeeded_required_job_requeues_with_bounded_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-required-fact-stale-success"
    run_id = "run-required-fact-stale-success"
    candidate_ref = "candidate-required-durable"
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=_required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        ),
    )
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    started = service.start_property_candidate_fact_enrichment(
        principal_id=principal_id,
        run_id=run_id,
        candidate_ref=candidate_ref,
        required_only=True,
        launch_worker=False,
        attempt_floor=1,
    )
    record = copy.deepcopy(dict(store["record"]))
    summary = dict(record.get("summary") or {})
    jobs = dict(summary.get("fact_enrichment_jobs") or {})
    stale_job = dict(jobs[started["job_id"]])
    stale_job.update(
        {
            "status": "succeeded",
            "retryable": False,
            "result_facts_digest": stale_job["facts_digest"],
        }
    )
    jobs[started["job_id"]] = stale_job
    summary["fact_enrichment_jobs"] = jobs
    record["summary"] = summary
    store["record"] = record
    provider_calls: list[str] = []
    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        lambda **kwargs: (
            provider_calls.append(str(kwargs.get("property_url") or ""))
            or _valid_osm_research(
                {"nearest_supermarket_m": 260},
                property_url=str(kwargs.get("property_url") or ""),
            )
        ),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {"fit_score": 69.0, "recommendation": "shortlist"},
    )
    try:
        _clear_run(run_id)
        restarted_service = ProductService(client.app.state.container)
        restarted_service.reconcile_property_search_results_delivery(
            principal_id=principal_id,
            allow_notifications=False,
        )
        _, final_job = _active_required_fact_job(
            store,
            candidate_ref=candidate_ref,
        )
        final_summary = dict(dict(store["record"]).get("summary") or {})

        assert len(provider_calls) == 1
        assert final_job["attempt"] == 3
        assert final_job["status"] == "succeeded"
        assert final_summary["required_fact_resolution_pending"] is False
        assert final_summary["evaluating_candidate_total"] == 0
    finally:
        _clear_run(run_id)


@pytest.mark.parametrize(
    ("expired_attempt", "expected_status", "expected_provider_calls"),
    (
        (2, "succeeded", 1),
        (3, "terminal_error", 0),
    ),
)
def test_expired_running_lease_consumes_attempt_and_never_runs_a_fourth_time(
    monkeypatch: pytest.MonkeyPatch,
    expired_attempt: int,
    expected_status: str,
    expected_provider_calls: int,
) -> None:
    principal_id = f"exec-required-fact-expired-{expired_attempt}"
    run_id = f"run-required-fact-expired-{expired_attempt}"
    candidate_ref = "candidate-required-durable"
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=_required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        ),
    )
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    started = service.start_property_candidate_fact_enrichment(
        principal_id=principal_id,
        run_id=run_id,
        candidate_ref=candidate_ref,
        required_only=True,
        launch_worker=False,
        attempt_floor=1,
    )
    record = copy.deepcopy(dict(store["record"]))
    summary = dict(record.get("summary") or {})
    jobs = dict(summary.get("fact_enrichment_jobs") or {})
    running_job = dict(jobs[started["job_id"]])
    running_job.update(
        {
            "status": "running",
            "attempt": expired_attempt,
            "lease_token": "abandoned-worker-lease",
            "lease_expires_at": (
                datetime.now(timezone.utc) - timedelta(minutes=5)
            ).isoformat(),
            "retryable": False,
            "fields": [
                {**dict(row), "state": "running"}
                for row in list(running_job.get("fields") or [])
                if isinstance(row, dict)
            ],
        }
    )
    jobs[started["job_id"]] = running_job
    summary["fact_enrichment_jobs"] = jobs
    record["summary"] = summary
    store["record"] = record
    provider_calls: list[str] = []
    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        lambda **kwargs: (
            provider_calls.append(str(kwargs.get("property_url") or ""))
            or _valid_osm_research(
                {"nearest_supermarket_m": 250},
                property_url=str(kwargs.get("property_url") or ""),
            )
        ),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {"fit_score": 67.0, "recommendation": "shortlist"},
    )
    try:
        _clear_run(run_id)
        restarted_service = ProductService(client.app.state.container)
        restarted_service.reconcile_property_search_results_delivery(
            principal_id=principal_id,
            allow_notifications=False,
        )
        _, final_job = _active_required_fact_job(
            store,
            candidate_ref=candidate_ref,
        )

        assert final_job["attempt"] == 3
        assert final_job["status"] == expected_status
        assert len(provider_calls) == expected_provider_calls
        if expired_attempt == 3:
            final_summary = dict(dict(store["record"]).get("summary") or {})
            assert final_summary["required_fact_resolution_exhausted"] is True
    finally:
        _clear_run(run_id)


def test_required_and_optional_fact_jobs_keep_independent_mode_pointers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-required-optional-concurrent"
    run_id = "run-required-optional-concurrent"
    candidate_ref = "candidate-required-durable"
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=_required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        ),
    )
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        lambda **kwargs: _valid_osm_research(
            {"nearest_supermarket_m": 240},
            property_url=str(kwargs.get("property_url") or ""),
        ),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {"fit_score": 71.0, "recommendation": "shortlist"},
    )
    monkeypatch.setattr(
        ProductService,
        "_launch_property_candidate_fact_enrichment",
        lambda self, **kwargs: False,
    )
    try:
        required = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            required_only=True,
            launch_worker=False,
            attempt_floor=1,
        )
        optional = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            required_only=False,
            launch_worker=False,
        )
        before = dict(dict(store["record"]).get("summary") or {})
        polled = service.get_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        )

        assert required["job_id"] != optional["job_id"]
        assert before["required_fact_enrichment_candidate_jobs"][candidate_ref] == (
            required["job_id"]
        )
        assert before["optional_fact_enrichment_candidate_jobs"][candidate_ref] == (
            optional["job_id"]
        )
        assert polled is not None
        assert polled["job_id"] == required["job_id"]
        assert polled["status"] == "queued"

        service._run_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            job_id=required["job_id"],
        )
        after = dict(dict(store["record"]).get("summary") or {})
        assert after["required_fact_enrichment_candidate_jobs"][candidate_ref] == (
            required["job_id"]
        )
        assert after["optional_fact_enrichment_candidate_jobs"][candidate_ref] == (
            optional["job_id"]
        )
        assert after["fact_enrichment_jobs"][required["job_id"]]["status"] == (
            "succeeded"
        )
        assert after["fact_enrichment_jobs"][optional["job_id"]]["status"] == (
            "queued"
        )
    finally:
        _clear_run(run_id)


def test_attestation_readiness_recovers_supported_field_once_in_mixed_terminal_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-attestation-recovery"
    run_id = "run-attestation-recovery"
    candidate_ref = "candidate-required-durable"
    record = _required_fact_run_record(
        principal_id=principal_id,
        run_id=run_id,
        candidate_ref=candidate_ref,
    )
    preferences = dict(record["property_search_preferences"])
    preferences.update(
        {
            "max_distance_to_ganztags_volksschule_m": 900,
            "max_distance_to_ganztags_volksschule_importance": "must_have",
        }
    )
    record["property_search_preferences"] = preferences
    store = _install_durable_required_fact_store(monkeypatch, record=record)
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    readiness = {"ready": False}
    provider_calls: list[str] = []
    monkeypatch.setattr(
        product_service,
        "property_fact_provider_attestation_is_ready",
        lambda: readiness["ready"],
    )

    def _provider(**kwargs: object):
        property_url = str(kwargs.get("property_url") or "")
        provider_calls.append(property_url)
        return _valid_osm_research(
            {"nearest_supermarket_m": 240},
            property_url=property_url,
        )

    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        _provider,
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {"fit_score": 72.0, "recommendation": "shortlist"},
    )
    try:
        held = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            required_only=True,
            launch_worker=False,
        )
        assert held["status"] == "terminal_error"
        assert held["attempt"] == 1
        assert provider_calls == []
        assert {
            dict(row.get("error") or {}).get("code")
            for row in held["fields"]
            if row["state"] == "unavailable"
        } == {
            "fact_attestation_secret_unavailable",
            "fact_provider_adapter_unavailable",
        }

        readiness["ready"] = True
        queued = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            required_only=True,
            retry_failed=True,
            launch_worker=False,
        )
        assert queued["status"] == "queued"
        service._run_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            job_id=queued["job_id"],
        )
        _job_id, final_job = _active_required_fact_job(
            store,
            candidate_ref=candidate_ref,
        )
        assert final_job["status"] == "terminal_error"
        assert len(provider_calls) == 1
        assert next(
            row
            for row in final_job["fields"]
            if row["key"] == "nearest_supermarket_m"
        )["state"] == "resolved"
        assert next(
            row
            for row in final_job["fields"]
            if row["key"] == "nearest_full_day_primary_school_m"
        )["state"] == "unavailable"

        retried_terminal = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            required_only=True,
            retry_failed=True,
            launch_worker=False,
        )
        assert retried_terminal["status"] == "terminal_error"
        assert len(provider_calls) == 1
    finally:
        _clear_run(run_id)


def test_optional_succeeded_attestation_hold_recovers_on_explicit_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-optional-attestation-recovery"
    run_id = "run-optional-attestation-recovery"
    candidate = _candidate(candidate_ref="candidate-optional-recovery")
    candidate["property_facts"] = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "map_location_precision": "address",
        "map_location_source": "listing_structured_coordinates",
    }
    record = _run_record(
        principal_id=principal_id,
        run_id=run_id,
        candidate=candidate,
    )
    record["property_search_preferences"] = {
        "max_distance_to_supermarket_m": 700,
        "max_distance_to_supermarket_importance": "nice_to_have",
    }
    store = _install_durable_required_fact_store(monkeypatch, record=record)
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    readiness = {"ready": False}
    provider_calls: list[str] = []
    monkeypatch.setattr(
        product_service,
        "property_fact_provider_attestation_is_ready",
        lambda: readiness["ready"],
    )
    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        lambda **kwargs: (
            provider_calls.append(str(kwargs.get("property_url") or ""))
            or _valid_osm_research(
                {"nearest_supermarket_m": 240},
                property_url=str(kwargs.get("property_url") or ""),
            )
        ),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {"fit_score": 72.0, "recommendation": "shortlist"},
    )
    try:
        held = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref="candidate-optional-recovery",
            required_only=False,
            launch_worker=False,
        )
        assert held["status"] == "succeeded"
        assert provider_calls == []
        assert next(
            row for row in held["fields"] if row["key"] == "nearest_supermarket_m"
        )["error"]["code"] == "fact_attestation_secret_unavailable"

        readiness["ready"] = True
        queued = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref="candidate-optional-recovery",
            required_only=False,
            launch_worker=False,
        )
        assert queued["status"] == "queued"
        service._run_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref="candidate-optional-recovery",
            job_id=queued["job_id"],
        )
        summary = dict(dict(store["record"])["summary"])
        job = dict(summary["fact_enrichment_jobs"][queued["job_id"]])
        assert job["status"] == "succeeded"
        assert len(provider_calls) == 1
        assert next(
            row for row in job["fields"] if row["key"] == "nearest_supermarket_m"
        )["state"] == "resolved"
    finally:
        _clear_run(run_id)


def test_required_success_projects_optional_idle_for_lazy_detail_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-required-optional-handoff"
    run_id = "run-required-optional-handoff"
    candidate_ref = "candidate-required-durable"
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=_required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        ),
    )
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        lambda **kwargs: _valid_osm_research(
            {"nearest_supermarket_m": 240},
            property_url=str(kwargs.get("property_url") or ""),
        ),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {"fit_score": 71.0, "recommendation": "shortlist"},
    )
    monkeypatch.setattr(
        ProductService,
        "_launch_property_candidate_fact_enrichment",
        lambda self, **kwargs: False,
    )
    try:
        required = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            required_only=True,
            launch_worker=False,
            attempt_floor=1,
        )
        service._run_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            job_id=required["job_id"],
        )

        persisted_summary = dict(dict(store["record"]).get("summary") or {})
        assert persisted_summary["fact_enrichment_jobs"][required["job_id"]][
            "status"
        ] == "succeeded"

        polled = service.get_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        )

        assert polled is not None
        assert polled["status"] == "idle"
        assert polled["bundle_kind"] == "optional-geo-v1"
        assert next(
            row
            for row in polled["fields"]
            if row["key"] == "nearest_supermarket_m"
        )["state"] == "resolved"
        assert any(
            row["priority"] == "lazy" and row["state"] in {"unknown", "stale"}
            for row in polled["fields"]
        )
    finally:
        _clear_run(run_id)


def test_optional_queued_job_get_is_read_only_after_capacity_miss_and_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-optional-dispatch-watchdog"
    run_id = "run-optional-dispatch-watchdog"
    candidate_ref = "candidate-required-durable"
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=_required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        ),
    )
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    dispatch_attempts: list[str] = []
    monkeypatch.setattr(
        ProductService,
        "_launch_property_candidate_fact_enrichment",
        lambda self, **kwargs: (
            dispatch_attempts.append(str(kwargs.get("job_id") or "")) or False
        ),
    )
    try:
        started = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            required_only=False,
            launch_worker=True,
        )
        assert started["status"] == "queued"
        assert dispatch_attempts == [started["job_id"]]

        persisted_before_get = copy.deepcopy(dict(store["record"]))
        _clear_run(run_id)
        restarted_service = ProductService(client.app.state.container)
        polled = restarted_service.get_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        )

        assert polled is not None
        assert polled["status"] == "queued"
        assert polled["job_id"] == started["job_id"]
        assert dispatch_attempts == [started["job_id"]]
        assert dict(store["record"]) == persisted_before_get
        assert dict(store["record"])["summary"][
            "optional_fact_enrichment_candidate_jobs"
        ][candidate_ref] == started["job_id"]
    finally:
        _clear_run(run_id)


def test_legacy_candidate_job_pointer_is_read_and_migrated_as_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-fact-legacy-pointer"
    run_id = "run-property-fact-legacy-pointer"
    candidate_ref = "candidate-required-durable"
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=_required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        ),
    )
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    try:
        started = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            required_only=False,
            launch_worker=False,
        )
        record = copy.deepcopy(dict(store["record"]))
        summary = dict(record.get("summary") or {})
        summary["fact_enrichment_candidate_jobs"] = {
            candidate_ref: started["job_id"]
        }
        summary.pop("optional_fact_enrichment_candidate_jobs", None)
        record["summary"] = summary
        store["record"] = record
        _clear_run(run_id)

        restarted_service = ProductService(client.app.state.container)
        joined = restarted_service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            required_only=False,
            launch_worker=False,
        )
        migrated_summary = dict(dict(store["record"]).get("summary") or {})

        assert joined["job_id"] == started["job_id"]
        assert "fact_enrichment_candidate_jobs" not in migrated_summary
        assert migrated_summary["optional_fact_enrichment_candidate_jobs"] == {
            candidate_ref: started["job_id"]
        }
    finally:
        _clear_run(run_id)


def test_legacy_candidate_job_pointer_uses_persisted_required_job_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-fact-legacy-required-pointer"
    run_id = "run-property-fact-legacy-required-pointer"
    candidate_ref = "candidate-required-durable"
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=_required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        ),
    )
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    try:
        started = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            required_only=True,
            launch_worker=False,
            attempt_floor=1,
        )
        record = copy.deepcopy(dict(store["record"]))
        summary = dict(record.get("summary") or {})
        summary["fact_enrichment_candidate_jobs"] = {
            candidate_ref: started["job_id"]
        }
        summary.pop("required_fact_enrichment_candidate_jobs", None)
        record["summary"] = summary
        store["record"] = record
        _clear_run(run_id)

        restarted_service = ProductService(client.app.state.container)
        joined = restarted_service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
            required_only=True,
            launch_worker=False,
            attempt_floor=1,
        )
        migrated_summary = dict(dict(store["record"]).get("summary") or {})

        assert joined["job_id"] == started["job_id"]
        assert "fact_enrichment_candidate_jobs" not in migrated_summary
        assert migrated_summary["required_fact_enrichment_candidate_jobs"] == {
            candidate_ref: started["job_id"]
        }
        assert "optional_fact_enrichment_candidate_jobs" not in migrated_summary
    finally:
        _clear_run(run_id)


def test_required_scheduler_work_budget_skips_terminal_and_backoff_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-required-fact-fairness"
    run_id = "run-required-fact-fairness"
    base = _required_fact_run_record(
        principal_id=principal_id,
        run_id=run_id,
    )
    candidates: list[dict[str, object]] = []
    for index in range(12):
        candidate_ref = f"candidate-fair-{index:02d}"
        candidate_record = _required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        )
        candidate = copy.deepcopy(
            candidate_record["summary"]["sources"][0]["research_candidates"][0]
        )
        candidate["property_url"] = (
            "https://www.willhaben.at/iad/immobilien/d/"
            f"fairness-home-{1234567800 + index}/"
        )
        facts = dict(candidate["property_facts"])
        preferences = dict(base["property_search_preferences"])
        plan = property_fact_requirement_plan(
            facts=facts,
            preferences=preferences,
            include_resolved=True,
            property_url=candidate["property_url"],
        )
        candidate["fact_requirement_plan"] = plan
        candidate["score_projection"] = property_fact_score_projection(
            candidate=candidate,
            plan=plan,
            preferences=preferences,
        )
        candidates.append(candidate)
    source = dict(base["summary"]["sources"][0])
    source.update(
        {
            "research_candidates": copy.deepcopy(candidates),
            "evaluating_candidates": copy.deepcopy(candidates),
            "top_candidates": [],
            "evaluating_candidate_total": len(candidates),
        }
    )
    base_summary = dict(base["summary"])
    base_summary.update(
        {
            "sources": [source],
            "evaluating_candidates": copy.deepcopy(candidates),
            "evaluating_candidate_total": len(candidates),
        }
    )
    jobs: dict[str, dict[str, object]] = {}
    candidate_jobs: dict[str, str] = {}
    now = datetime.now(timezone.utc).isoformat()
    for index, candidate in enumerate(candidates[:8]):
        candidate_ref = str(candidate["candidate_ref"])
        job_id = "pfe_" + f"{index + 1:024x}"
        plan_field = next(
            dict(row)
            for row in list(candidate["fact_requirement_plan"])
            if row["key"] == "nearest_supermarket_m"
        )
        terminal = index < 4
        jobs[job_id] = {
            "job_id": job_id,
            "candidate_ref": candidate_ref,
            "required_only": True,
            "status": "terminal_error" if terminal else "retryable_error",
            "attempt": 3 if terminal else 2,
            "updated_at": now,
            "retryable": not terminal,
            "fields": [
                {
                    **plan_field,
                    "state": "unavailable" if terminal else "retryable_error",
                    "error": {
                        "code": (
                            "fact_enrichment_attempts_exhausted"
                            if terminal
                            else "fact_provider_temporarily_unavailable"
                        ),
                        "message": "Provider not ready.",
                        "retry_after_seconds": 0 if terminal else 3600,
                    },
                }
            ],
            "score": dict(candidate["score_projection"]),
        }
        candidate_jobs[candidate_ref] = job_id
    base_summary["fact_enrichment_jobs"] = jobs
    base_summary["required_fact_enrichment_candidate_jobs"] = candidate_jobs
    base["summary"] = base_summary
    store = _install_durable_required_fact_store(monkeypatch, record=base)
    client = build_property_operator_client(principal_id=principal_id)
    provider_calls: list[str] = []
    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        lambda **kwargs: (
            provider_calls.append(str(kwargs.get("property_url") or ""))
            or {"nearest_supermarket_m": 220},
            {
                "coordinate_basis": "candidate_listing_coordinates",
                "coordinate_observed_at": "",
                "coordinate_precision": "address",
                "coordinate_source": "listing",
                "coordinate_exact": True,
            },
        ),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {"fit_score": 66.0, "recommendation": "shortlist"},
    )
    try:
        service = ProductService(client.app.state.container)
        service._advance_property_search_required_fact_jobs(
            principal_id=principal_id,
            run_id=run_id,
            max_candidates=2,
        )
        first_summary = dict(dict(store["record"]).get("summary") or {})
        first_pointers = dict(
            first_summary["required_fact_enrichment_candidate_jobs"]
        )

        assert len(provider_calls) == 2
        assert "candidate-fair-08" in first_pointers
        assert "candidate-fair-09" in first_pointers
        assert "candidate-fair-10" not in first_pointers

        service._advance_property_search_required_fact_jobs(
            principal_id=principal_id,
            run_id=run_id,
            max_candidates=2,
        )
        second_summary = dict(dict(store["record"]).get("summary") or {})
        second_pointers = dict(
            second_summary["required_fact_enrichment_candidate_jobs"]
        )

        assert len(provider_calls) == 4
        assert "candidate-fair-10" in second_pointers
        assert "candidate-fair-11" in second_pointers
    finally:
        _clear_run(run_id)


def test_required_scheduler_isolates_start_failure_and_advances_next_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-required-fact-start-isolation"
    run_id = "run-required-fact-start-isolation"
    first_ref = "candidate-start-failure"
    second_ref = "candidate-start-success"
    record = _required_fact_run_record(
        principal_id=principal_id,
        run_id=run_id,
        candidate_ref=first_ref,
    )
    first_candidate = copy.deepcopy(
        record["summary"]["sources"][0]["research_candidates"][0]
    )
    second_candidate = copy.deepcopy(first_candidate)
    second_candidate.update(
        {
            "candidate_ref": second_ref,
            "listing_id": "required-start-success-1234567891",
            "property_url": (
                "https://www.willhaben.at/iad/immobilien/d/"
                "required-start-success-1234567891/"
            ),
            "source_ref": "property-scout:required-start-success-1234567891",
        }
    )
    preferences = dict(record["property_search_preferences"])
    second_plan = property_fact_requirement_plan(
        facts=dict(second_candidate["property_facts"]),
        preferences=preferences,
        include_resolved=True,
        property_url=second_candidate["property_url"],
    )
    second_candidate["fact_requirement_plan"] = second_plan
    second_candidate["score_projection"] = property_fact_score_projection(
        candidate=second_candidate,
        plan=second_plan,
        preferences=preferences,
    )
    source = dict(record["summary"]["sources"][0])
    source["research_candidates"] = [first_candidate, second_candidate]
    source["evaluating_candidates"] = [first_candidate, second_candidate]
    source["evaluating_candidate_total"] = 2
    record["summary"]["sources"] = [source]
    record["summary"]["evaluating_candidates"] = [
        copy.deepcopy(first_candidate),
        copy.deepcopy(second_candidate),
    ]
    record["summary"]["evaluating_candidate_total"] = 2
    record["summary"]["required_fact_research_pending_total"] = 2
    store = _install_durable_required_fact_store(monkeypatch, record=record)
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    original_start = service.start_property_candidate_fact_enrichment

    def _start_with_one_failure(**kwargs: object) -> dict[str, object]:
        if str(kwargs.get("candidate_ref") or "") == first_ref:
            raise ValueError("unsupported listing URL must stay sanitized")
        return original_start(**kwargs)

    monkeypatch.setattr(
        service,
        "start_property_candidate_fact_enrichment",
        _start_with_one_failure,
    )
    monkeypatch.setattr(
        product_service,
        "_property_fact_fresh_geo_snapshot",
        lambda **kwargs: _valid_osm_research(
            {"nearest_supermarket_m": 240},
            property_url=str(kwargs.get("property_url") or ""),
        ),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {"fit_score": 71.0, "recommendation": "shortlist"},
    )
    try:
        service._advance_property_search_required_fact_jobs(
            principal_id=principal_id,
            run_id=run_id,
            max_candidates=2,
        )
        summary = dict(dict(store["record"]).get("summary") or {})
        pointers = dict(summary["required_fact_enrichment_candidate_jobs"])
        jobs = dict(summary["fact_enrichment_jobs"])
        failed_job = dict(jobs[pointers[first_ref]])
        succeeded_job = dict(jobs[pointers[second_ref]])

        assert failed_job["status"] == "retryable_error"
        assert failed_job["attempt"] == 2
        assert failed_job["error"] == {
            "code": "fact_enrichment_start_failed",
            "message": (
                "Required property facts could not be queued from the current "
                "listing source."
            ),
        }
        assert "unsupported listing URL" not in json.dumps(failed_job)
        assert succeeded_job["status"] == "succeeded"
        assert succeeded_job["attempt"] == 2
    finally:
        _clear_run(run_id)


def test_event_cas_retry_preserves_concurrently_acquired_fact_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-required-fact-event-race"
    run_id = "run-required-fact-event-race"
    candidate_ref = "candidate-required-durable"
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=_required_fact_run_record(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        ),
    )
    client = build_property_operator_client(principal_id=principal_id)
    cas_calls = 0
    job_id = "pfe_" + "f" * 24
    stale_event_state = copy.deepcopy(dict(store["record"]))
    stale_event_candidate = product_service._property_fact_find_candidate(
        stale_event_state,
        candidate_ref=candidate_ref,
    )
    assert stale_event_candidate is not None
    stale_event_candidate = {
        **stale_event_candidate,
        "tour_url": "/tours/concurrent-visual-refresh",
        "tour_status": "ready",
    }
    assert product_service._property_fact_update_candidate_copies(
        stale_event_state,
        candidate_ref=candidate_ref,
        updated_candidate=stale_event_candidate,
    )
    stale_summary_updates = copy.deepcopy(stale_event_state["summary"])

    def _racing_cas(**kwargs: object) -> dict[str, object]:
        nonlocal cas_calls
        cas_calls += 1
        if cas_calls == 1:
            live = copy.deepcopy(dict(store["record"]))
            summary = dict(live.get("summary") or {})
            jobs = dict(summary.get("fact_enrichment_jobs") or {})
            jobs[job_id] = {
                "job_id": job_id,
                "candidate_ref": candidate_ref,
                "required_only": True,
                "status": "running",
                "attempt": 2,
                "lease_token": "concurrent-lease",
                "lease_expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=2)
                ).isoformat(),
            }
            summary["fact_enrichment_jobs"] = jobs
            summary["required_fact_enrichment_candidate_jobs"] = {
                candidate_ref: job_id
            }
            summary["blocked_required_facts"] = [
                {
                    "candidate_ref": candidate_ref,
                    "key": "nearest_supermarket_m",
                }
            ]
            live["summary"] = summary
            live_candidate = product_service._property_fact_find_candidate(
                live,
                candidate_ref=candidate_ref,
            )
            assert live_candidate is not None
            live_candidate = {
                **live_candidate,
                "property_facts": {
                    **dict(live_candidate.get("property_facts") or {}),
                    "nearest_supermarket_m": 333,
                },
                "fit_score": 91.0,
                "assessment_fit_score": 90.0,
                "adjusted_fit_score": 91.0,
                "ranking_score": 91.0,
                "search_score_context": {"source": "live-fact-completion"},
                "score_provenance": {"facts_digest": "sha256:live"},
                "score_state": "final",
                "ranking_eligible": True,
            }
            assert product_service._property_fact_update_candidate_copies(
                live,
                candidate_ref=candidate_ref,
                updated_candidate=live_candidate,
            )
            store["record"] = live
            return {"status": "record_changed", "record_sha256": "raced"}
        updated = copy.deepcopy(dict(kwargs["updated_record"]))
        store["record"] = updated
        return {
            "status": "applied",
            "record": copy.deepcopy(updated),
            "record_sha256": "event-applied",
        }

    monkeypatch.setattr(
        product_service,
        "_compare_and_swap_property_search_run_record",
        _racing_cas,
    )
    try:
        service = ProductService(client.app.state.container)
        stored = service._record_property_search_run_event(
            run_id=run_id,
            principal_id=principal_id,
            step="results_finalizing",
            message="Result state refreshed.",
            status="completed_partial",
            steps_delta=0,
            summary_updates=stale_summary_updates,
        )
        persisted = dict(store["record"])
        summary = dict(persisted.get("summary") or {})
        job = dict(summary["fact_enrichment_jobs"][job_id])

        assert stored is True
        assert cas_calls == 2
        assert job["status"] == "running"
        assert job["lease_token"] == "concurrent-lease"
        assert summary["blocked_required_facts"] == [
            {
                "candidate_ref": candidate_ref,
                "key": "nearest_supermarket_m",
            }
        ]
        persisted_candidate = product_service._property_fact_find_candidate(
            persisted,
            candidate_ref=candidate_ref,
        )
        assert persisted_candidate is not None
        assert dict(persisted_candidate["property_facts"])[
            "nearest_supermarket_m"
        ] == 333
        assert persisted_candidate["fit_score"] == 91.0
        assert persisted_candidate["assessment_fit_score"] == 90.0
        assert persisted_candidate["adjusted_fit_score"] == 91.0
        assert persisted_candidate["search_score_context"] == {
            "source": "live-fact-completion"
        }
        assert persisted_candidate["score_provenance"] == {
            "facts_digest": "sha256:live"
        }
        assert persisted_candidate["tour_url"] == (
            "/tours/concurrent-visual-refresh"
        )
        assert persisted_candidate["tour_status"] == "ready"
        assert any(
            str(event.get("step") or "") == "results_finalizing"
            for event in list(persisted.get("events") or [])
            if isinstance(event, dict)
        )
    finally:
        _clear_run(run_id)


def test_required_fact_research_counter_does_not_block_first_result_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-required-fact-counter-event"
    run_id = "run-required-fact-counter-event"
    candidate = _candidate(candidate_ref="candidate-first-result")
    record = _run_record(
        principal_id=principal_id,
        run_id=run_id,
        candidate=candidate,
    )
    record["summary"] = {"required_fact_research_attempted_total": 1}
    store = _install_durable_required_fact_store(
        monkeypatch,
        record=record,
    )
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    incoming_source = {
        "source_label": "Willhaben",
        "source_url": "https://www.willhaben.at/iad/immobilien/",
        "top_candidates": [copy.deepcopy(candidate)],
        "research_candidates": [copy.deepcopy(candidate)],
    }
    try:
        assert service._record_property_search_run_event(
            run_id=run_id,
            principal_id=principal_id,
            step="results_ready",
            message="First ranked result ready.",
            status="processed",
            steps_delta=0,
            summary_updates={
                "sources": [incoming_source],
                "ranked_candidates": [copy.deepcopy(candidate)],
                "ranked_candidate_total": 1,
            },
            force_status="processed",
        )
        summary = dict(dict(store["record"]).get("summary") or {})

        assert summary["sources"][0]["source_url"] == incoming_source["source_url"]
        assert summary["ranked_candidates"][0]["candidate_ref"] == (
            "candidate-first-result"
        )
        assert summary["ranked_candidate_total"] == 1
    finally:
        _clear_run(run_id)


def test_compact_v3_smoke_preserves_required_blocker_without_promoting_evaluating() -> None:
    record = _required_fact_run_record(
        principal_id="exec-required-fact-compact-smoke",
        run_id="run-required-fact-compact-smoke",
    )

    compact = property_search_storage._compact_property_search_run_record(record)
    summary = dict(compact["summary"])

    assert compact["compact_schema_version"] == 3
    assert compact["delivery_pending"] is True
    assert summary["results_delivery_blocked_reason"] == (
        "required_property_facts_unresolved"
    )
    assert summary["required_fact_resolution_pending"] is True
    assert summary["required_fact_resolution_exhausted"] is False
    assert summary["ranked_candidates"] == []
    assert summary["evaluating_candidate_total"] == 1


def test_fact_enrichment_job_is_idempotent_persistent_and_recomputes_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-facts-job"
    run_id = "run-property-facts-job"
    candidate = _candidate()
    candidate["property_facts"] = {
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "map_location_precision": "address",
        "map_location_source": "listing",
        "house_number": "12",
    }
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    _seed_run(_run_record(principal_id=principal_id, run_id=run_id, candidate=candidate))
    calls = {"research": 0}
    observed_coordinates: list[tuple[float, float]] = []

    def _research(
        _latitude: float,
        _longitude: float,
        property_url: str,
    ) -> dict[str, object]:
        calls["research"] += 1
        observed_coordinates.append((_latitude, _longitude))
        research, _meta = _valid_osm_research(
            {
                "nearest_supermarket_m": 280,
                "nearest_playground_m": 410,
                "nearest_pharmacy_m": 530,
                "nearest_medical_care_m": 570,
                "nearest_subway_m": 640,
            },
            property_url=property_url,
            latitude=_latitude,
            longitude=_longitude,
        )
        research["nearest_supermarket_name"] = "Daily Market"
        return research

    refreshed_latitude = 48.215
    refreshed_longitude = 16.385
    source_fingerprint = property_fact_source_fingerprint(
        str(candidate["property_url"])
    )
    coordinate_observed_at = datetime.now(timezone.utc)
    monkeypatch.setattr(
        product_service,
        "_property_fact_coordinate_snapshot",
        lambda _property_url: {
            "map_lat": refreshed_latitude,
            "map_lng": refreshed_longitude,
            "map_location_precision": "address",
            "map_location_source": "listing_structured_coordinates",
            "house_number": "22",
            "map_coordinate_evidence": {
                "exact": True,
                "trusted": True,
                "provider": "listing_structured_coordinates",
                "source_fingerprint": source_fingerprint,
                "latitude": refreshed_latitude,
                "longitude": refreshed_longitude,
                "coordinate_digest": property_fact_coordinate_digest(
                    refreshed_latitude,
                    refreshed_longitude,
                ),
                "observed_at": coordinate_observed_at.isoformat(),
                "expires_at": (
                    coordinate_observed_at + timedelta(hours=6)
                ).isoformat(),
            },
        },
    )
    monkeypatch.setattr(product_service, "_property_research_nearby_pois", _research)
    monkeypatch.setattr(
        product_service,
        "_property_fact_validated_source_url",
        lambda url: str(url),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {
            "domain": "willhaben",
            "fit_score": 68.0,
            "recommendation": "shortlist",
            "match_reasons_json": ["Nearby facts verified."],
        },
    )
    try:
        first = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref="candidate-facts",
        )
        second = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref="candidate-facts",
        )
        assert first["job_id"] == second["job_id"]

        deadline = time.monotonic() + 4.0
        status: dict[str, object] | None = None
        while time.monotonic() < deadline:
            status = service.get_property_candidate_fact_enrichment(
                principal_id=principal_id,
                run_id=run_id,
                candidate_ref="candidate-facts",
            )
            if status and status["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)

        assert status is not None
        assert status["schema_version"] == PROPERTY_FACT_ENRICHMENT_SCHEMA_VERSION
        assert status["status"] == "succeeded"
        assert calls["research"] == 1
        assert observed_coordinates == [(refreshed_latitude, refreshed_longitude)]
        assert status["score"]["state"] == "final"
        assert status["score"]["previous"] == 60.0
        assert status["score"]["current"] == 68.0
        assert status["score"]["delta"] == 8.0
        supermarket = next(row for row in status["fields"] if row["key"] == "nearest_supermarket_m")
        assert supermarket["display_value"] == "280 m"
        assert supermarket["provenance"]["provider"] == "openstreetmap_overpass"
        assert supermarket["provenance"]["method"] == "straight_line_osm"
        assert supermarket["provenance"]["coordinate_exact"] is True
        assert supermarket["provenance"]["observed_at"]
        assert supermarket["provenance"]["expires_at"]
        assert PropertyFactEnrichmentOut(**status).status == "succeeded"

        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            persisted = copy.deepcopy(
                product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id]
            )
        persisted_candidate = product_service._property_fact_find_candidate(
            persisted,
            candidate_ref="candidate-facts",
        )
        assert persisted_candidate is not None
        persisted_facts = dict(persisted_candidate["property_facts"])
        assert persisted_facts["map_lat"] == refreshed_latitude
        assert persisted_facts["map_lng"] == refreshed_longitude
        assert dict(persisted_facts["map_coordinate_evidence"])[
            "source_fingerprint"
        ] == source_fingerprint

        joined = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref="candidate-facts",
        )
        assert joined["job_id"] == first["job_id"]
        assert joined["status"] == "succeeded"
        assert calls["research"] == 1
    finally:
        _clear_run(run_id)


def test_fact_enrichment_no_work_returns_complete_resolved_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-facts-no-work"
    run_id = "run-property-facts-no-work"
    candidate = _candidate(candidate_ref="candidate-no-work")
    candidate["property_facts"] = _resolved_geo_facts()
    client = build_property_operator_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    _seed_run(_run_record(principal_id=principal_id, run_id=run_id, candidate=candidate))
    monkeypatch.setattr(product_service, "_property_fact_validated_source_url", lambda url: str(url))
    launched: list[str] = []
    monkeypatch.setattr(
        ProductService,
        "_launch_property_candidate_fact_enrichment",
        lambda self, **kwargs: launched.append(str(kwargs.get("job_id") or "")),
    )
    try:
        started = service.start_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref="candidate-no-work",
        )
        polled = service.get_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref="candidate-no-work",
        )

        assert launched == []
        assert started["status"] == "succeeded"
        assert polled is not None
        assert polled["status"] == "succeeded"
        expected_keys = {
            "nearest_supermarket_m",
            "nearest_playground_m",
            "nearest_pharmacy_m",
            "nearest_medical_care_m",
            "nearest_subway_m",
        }
        for payload in (started, polled):
            assert {row["key"] for row in payload["fields"]} == expected_keys
            assert len(payload["fields"]) == 5
            assert all(row["state"] == "resolved" for row in payload["fields"])
            assert PropertyFactEnrichmentOut(**payload).status == "succeeded"

        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            record = product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id]
            stale_candidate = dict(candidate)
            stale_facts = copy.deepcopy(candidate["property_facts"])
            supermarket_evidence = dict(
                dict(stale_facts["property_fact_evidence"])["nearest_supermarket_m"]
            )
            supermarket_evidence["expires_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ).isoformat()
            stale_facts["property_fact_evidence"] = {
                "nearest_supermarket_m": supermarket_evidence,
            }
            stale_candidate["property_facts"] = stale_facts
            assert product_service._property_fact_update_candidate_copies(
                record,
                candidate_ref="candidate-no-work",
                updated_candidate=stale_candidate,
            )

        stale = service.get_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref="candidate-no-work",
        )
        assert stale is not None
        assert stale["status"] == "retryable_error"
        assert stale["retryable"] is True
        assert len(stale["fields"]) == 5
        stale_supermarket = next(
            row for row in stale["fields"] if row["key"] == "nearest_supermarket_m"
        )
        assert stale_supermarket["state"] == "retryable_error"
        assert stale_supermarket["error"]["code"] == "fact_enrichment_requires_retry"
        assert not any(
            row["state"] in {"unknown", "stale", "queued", "running"}
            for row in stale["fields"]
        )
        assert PropertyFactEnrichmentOut(**stale).status == "retryable_error"
    finally:
        _clear_run(run_id)


def test_fact_enrichment_partial_retry_api_preserves_complete_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-facts-partial-retry"
    run_id = "run-property-facts-partial-retry"
    candidate = _candidate(candidate_ref="candidate-partial-retry")
    candidate["property_facts"] = _resolved_geo_facts(include_playground=False)
    client = build_property_operator_client(principal_id=principal_id)
    _seed_run(_run_record(principal_id=principal_id, run_id=run_id, candidate=candidate))
    monkeypatch.setattr(product_service, "_property_fact_validated_source_url", lambda url: str(url))
    monkeypatch.setattr(ProductService, "_launch_property_candidate_fact_enrichment", lambda self, **kwargs: None)
    endpoint = (
        f"/app/api/signals/property/search/run/{run_id}/candidates/"
        "candidate-partial-retry/fact-enrichment"
    )
    same_origin_headers = {
        "origin": "https://propertyquarry.com",
        "sec-fetch-site": "same-origin",
    }
    try:
        started = client.post(
            endpoint,
            json={"retry_failed": False},
            headers=same_origin_headers,
        )
        assert started.status_code == 202, started.text
        started_payload = started.json()
        assert len(started_payload["fields"]) == 5
        assert next(
            row for row in started_payload["fields"] if row["key"] == "nearest_playground_m"
        )["state"] == "queued"

        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            record = product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id]
            summary = dict(record.get("summary") or {})
            jobs = dict(summary.get("fact_enrichment_jobs") or {})
            job = dict(jobs[started_payload["job_id"]])
            playground = next(
                dict(row)
                for row in list(job.get("fields") or [])
                if isinstance(row, dict) and row.get("key") == "nearest_playground_m"
            )
            playground.update(
                {
                    "state": "retryable_error",
                    "error": {
                        "code": "provider_timeout",
                        "message": "Nearby provider timed out.",
                        "retry_after_seconds": 0,
                    },
                }
            )
            job.update(
                {
                    "status": "retryable_error",
                    "retryable": True,
                    "fields": [playground],
                    "result_facts_digest": job["facts_digest"],
                }
            )
            jobs[started_payload["job_id"]] = job
            summary["fact_enrichment_jobs"] = jobs
            record["summary"] = summary

        failed_payload = client.get(endpoint).json()
        assert len(failed_payload["fields"]) == 5
        failed_supermarket = next(
            row for row in failed_payload["fields"] if row["key"] == "nearest_supermarket_m"
        )
        failed_playground = next(
            row for row in failed_payload["fields"] if row["key"] == "nearest_playground_m"
        )
        assert failed_supermarket["state"] == "resolved"
        assert failed_supermarket["value"] == 280
        assert failed_supermarket["provenance"]["provider"] == "openstreetmap_overpass"
        assert failed_playground["state"] == "retryable_error"
        assert failed_playground["error"]["code"] == "provider_timeout"

        retried = client.post(
            endpoint,
            json={"retry_failed": True},
            headers=same_origin_headers,
        )
        assert retried.status_code == 202, retried.text
        retried_payload = retried.json()
        assert len(retried_payload["fields"]) == 5
        retried_supermarket = next(
            row for row in retried_payload["fields"] if row["key"] == "nearest_supermarket_m"
        )
        retried_playground = next(
            row for row in retried_payload["fields"] if row["key"] == "nearest_playground_m"
        )
        assert retried_supermarket["state"] == "resolved"
        assert retried_supermarket["value"] == 280
        assert retried_supermarket["provenance"]["provider"] == "openstreetmap_overpass"
        assert retried_playground["state"] == "queued"
        assert retried_playground["error"]["code"] == ""
    finally:
        _clear_run(run_id)


def test_fact_enrichment_retry_backoff_and_exhaustion_are_bounded() -> None:
    now = datetime.now(timezone.utc)
    retryable_job = {
        "job_id": "pfe_" + "a" * 24,
        "status": "retryable_error",
        "attempt": 2,
        "retryable": True,
        "updated_at": (now - timedelta(seconds=17)).isoformat(),
        "fields": [
            {
                "key": "nearest_supermarket_m",
                "label": "Supermarket distance",
                "state": "retryable_error",
                "priority": "lazy",
                "affects_score": True,
                "value": None,
                "display_value": "",
                "provenance": {},
                "error": {
                    "code": "fact_provider_temporarily_unavailable",
                    "message": "Map provider unavailable.",
                    "retry_after_seconds": 60,
                },
            }
        ],
        "score": {},
    }

    assert _property_fact_retry_remaining_seconds(retryable_job, now=now) == 43
    safe_retryable = _property_fact_safe_job_payload(retryable_job)
    assert safe_retryable["status"] == "retryable_error"
    assert safe_retryable["retryable"] is True
    assert safe_retryable["fields"][0]["error"]["retry_after_seconds"] in {42, 43}

    exhausted_job = {**retryable_job, "attempt": 3}
    assert _property_fact_retry_remaining_seconds(exhausted_job, now=now) is None
    safe_exhausted = _property_fact_safe_job_payload(exhausted_job)
    assert safe_exhausted["status"] == "terminal_error"
    assert safe_exhausted["retryable"] is False
    assert safe_exhausted["fields"][0]["state"] == "unavailable"
    assert safe_exhausted["fields"][0]["error"]["code"] == "fact_enrichment_attempts_exhausted"


def test_fact_enrichment_run_mutation_retries_durable_compare_and_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-facts-cas"
    run_id = "run-property-facts-cas"
    stored = _run_record(principal_id=principal_id, run_id=run_id, candidate=_candidate())
    calls = {"cas": 0, "preserve_memberships": []}

    monkeypatch.setattr(product_service, "_property_search_run_database_url", lambda: "postgresql://durable.test/property")
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record_storage",
        lambda **kwargs: copy.deepcopy(stored),
    )

    def _cas(**kwargs: object) -> dict[str, object]:
        calls["cas"] += 1
        calls["preserve_memberships"].append(
            kwargs.get("preserve_unprojected_packet_memberships") is True
        )
        if calls["cas"] == 1:
            return {"status": "record_changed", "record_sha256": "changed"}
        updated = copy.deepcopy(kwargs["updated_record"])
        return {"status": "applied", "record": updated, "record_sha256": "applied"}

    monkeypatch.setattr(product_service, "_compare_and_swap_property_search_run_record", _cas)
    try:
        result = product_service._property_fact_mutate_run_record(
            principal_id=principal_id,
            run_id=run_id,
            mutate=lambda record: record.setdefault("fact_test_marker", "stored"),
        )

        assert result == "stored"
        assert calls["cas"] == 2
        assert calls["preserve_memberships"] == [True, True]
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            assert product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id]["fact_test_marker"] == "stored"
    finally:
        _clear_run(run_id)


def test_fact_candidate_hydrates_only_exact_indexed_packet_after_run_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-facts-indexed"
    run_id = "run-property-facts-indexed"
    candidate_ref = "candidate-indexed"
    candidate = _candidate(candidate_ref=candidate_ref)
    record: dict[str, object] = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "completed_partial",
        "summary": {"fact_enrichment_jobs": {}},
    }
    observed: list[dict[str, str]] = []

    monkeypatch.setattr(
        product_service,
        "_property_search_run_database_url",
        lambda: "postgresql://durable.test/property",
    )

    def _load_indexed(**kwargs: str) -> dict[str, object]:
        observed.append(dict(kwargs))
        return {"candidate": copy.deepcopy(candidate)}

    monkeypatch.setattr(
        product_service._property_search_storage,
        "_load_property_research_packet_link_for_run",
        _load_indexed,
    )

    hydrated = product_service._property_fact_find_candidate(
        record,
        candidate_ref=candidate_ref,
    )

    assert hydrated == candidate
    assert observed == [
        {
            "principal_id": principal_id,
            "run_id": run_id,
            "candidate_ref": candidate_ref,
        }
    ]
    assert record["summary"]["research_candidates"] == [candidate]
    bounded = property_search_storage._bounded_property_search_run_payload(record)
    bounded_candidates = list(
        dict(bounded.get("summary") or {}).get("research_candidates") or []
    )
    assert len(bounded_candidates) == 1
    assert bounded_candidates[0]["candidate_ref"] == candidate_ref


def test_fact_candidate_prefers_exact_packet_over_lossy_bounded_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-facts-bounded-preview"
    run_id = "run-property-facts-bounded-preview"
    candidate_ref = "candidate-bounded-preview"
    indexed_candidate = _candidate(candidate_ref=candidate_ref)
    indexed_candidate["property_facts"] = {
        "address": "Karl-Czerny-Gasse 2, 1020 Wien",
        **{f"durable_fact_{index}": index for index in range(40)},
    }
    compact_candidate = copy.deepcopy(indexed_candidate)
    compact_candidate["property_facts"] = {
        "address": "Karl-Czerny-Gasse 2, 1020 Wien"
    }
    record: dict[str, object] = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "completed_partial",
        "payload_retention_status": "compact_only",
        "summary": {
            "fact_enrichment_jobs": {},
            "research_candidates": [compact_candidate],
        },
    }
    observed: list[dict[str, str]] = []

    monkeypatch.setattr(
        product_service,
        "_property_search_run_database_url",
        lambda: "postgresql://durable.test/property",
    )

    def _load_indexed(**kwargs: str) -> dict[str, object]:
        observed.append(dict(kwargs))
        return {"candidate": copy.deepcopy(indexed_candidate)}

    monkeypatch.setattr(
        product_service._property_search_storage,
        "_load_property_research_packet_link_for_run",
        _load_indexed,
    )

    hydrated = product_service._property_fact_find_candidate(
        record,
        candidate_ref=candidate_ref,
    )

    assert hydrated == indexed_candidate
    assert hydrated != compact_candidate
    assert observed == [
        {
            "principal_id": principal_id,
            "run_id": run_id,
            "candidate_ref": candidate_ref,
        }
    ]
    assert record["summary"]["research_candidates"] == [indexed_candidate]


def test_fact_candidate_recovers_membership_removed_by_legacy_partial_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-facts-recovery"
    run_id = "run-property-facts-recovery"
    candidate_ref = "candidate-recovery"
    candidate = _candidate(candidate_ref=candidate_ref)
    record: dict[str, object] = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "completed_partial",
        "summary": {
            "fact_enrichment_jobs": {
                "pfe_recovery": {
                    "job_id": "pfe_recovery",
                    "candidate_ref": candidate_ref,
                    "status": "queued",
                }
            }
        },
    }
    active_lookups: list[dict[str, str]] = []

    monkeypatch.setattr(
        product_service,
        "_property_search_run_database_url",
        lambda: "postgresql://durable.test/property",
    )
    monkeypatch.setattr(
        product_service._property_search_storage,
        "_load_property_research_packet_link_for_run",
        lambda **_kwargs: None,
    )

    def _load_active(**kwargs: str) -> dict[str, object]:
        active_lookups.append(dict(kwargs))
        return {"candidate": copy.deepcopy(candidate)}

    monkeypatch.setattr(
        product_service,
        "_load_property_research_packet_link_storage",
        _load_active,
    )

    hydrated = product_service._property_fact_find_candidate(
        record,
        candidate_ref=candidate_ref,
    )

    assert hydrated == candidate
    assert active_lookups == [
        {"principal_id": principal_id, "candidate_ref": candidate_ref}
    ]
    assert record["summary"]["research_candidates"] == [candidate]


def test_fact_enrichment_api_is_authenticated_same_origin_and_never_accepts_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-facts-api"
    run_id = "run-property-facts-api"
    client = build_property_operator_client(principal_id=principal_id)
    _seed_run(_run_record(principal_id=principal_id, run_id=run_id, candidate=_candidate()))
    monkeypatch.setattr(product_service, "_property_fact_validated_source_url", lambda url: str(url))
    monkeypatch.setattr(ProductService, "_launch_property_candidate_fact_enrichment", lambda self, **kwargs: None)
    endpoint = f"/app/api/signals/property/search/run/{run_id}/candidates/candidate-facts/fact-enrichment"
    try:
        authorization = client.headers.pop("authorization")
        assert client.get(endpoint).status_code == 401
        assert client.post(endpoint, json={"retry_failed": False}).status_code == 401
        client.headers["authorization"] = authorization

        cross_origin = client.post(
            endpoint,
            json={"retry_failed": False},
            headers={"origin": "https://attacker.example", "sec-fetch-site": "cross-site"},
        )
        assert cross_origin.status_code == 403

        arbitrary_url = client.post(
            endpoint,
            json={"retry_failed": False, "property_url": "http://127.0.0.1/private"},
            headers={"origin": "https://propertyquarry.com"},
        )
        assert arbitrary_url.status_code == 422

        token_without_origin = client.post(endpoint, json={"retry_failed": False})
        assert token_without_origin.status_code == 202, token_without_origin.text

        started = client.post(
            endpoint,
            json={"retry_failed": False},
            headers={"origin": "https://propertyquarry.com", "sec-fetch-site": "same-origin"},
        )
        assert started.status_code == 202, started.text
        payload = started.json()
        assert payload["status"] == "queued"
        assert payload["candidate_ref"] == "candidate-facts"
        assert "property_url" not in payload

        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            record = product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id]
            summary = dict(record.get("summary") or {})
            jobs = dict(summary.get("fact_enrichment_jobs") or {})
            job = dict(jobs[payload["job_id"]])
            job["status"] = "retryable_error"
            job["retryable"] = True
            jobs[payload["job_id"]] = job
            summary["fact_enrichment_jobs"] = jobs
            record["summary"] = summary

        retried = client.post(
            endpoint,
            json={"retry_failed": True},
            headers={"origin": "https://propertyquarry.com", "sec-fetch-site": "same-origin"},
        )
        assert retried.status_code == 202, retried.text
        assert retried.json()["job_id"] == payload["job_id"]
        assert retried.json()["attempt"] == 2
        assert retried.json()["status"] == "queued"

        polled = client.get(endpoint)
        assert polled.status_code == 200
        assert polled.json()["job_id"] == payload["job_id"]
    finally:
        _clear_run(run_id)
