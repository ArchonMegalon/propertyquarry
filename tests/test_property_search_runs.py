from __future__ import annotations

import json
import importlib
import inspect
import hashlib
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.product.service as product_service
import app.product.property_search_storage as property_search_storage
import app.product.property_investment_external_data as property_investment_external_data
import app.api.routes.product_api_delivery as product_api_delivery_routes
import app.api.routes.landing_property_workspace_payload as property_workspace_payload
from app.api.routes.product_api_contracts import PropertySearchRunStatusOut
from app.api.routes.product_api_delivery import (
    _property_search_apply_response_display_totals,
    _property_search_lightweight_candidate_payload,
    _property_search_payload_with_status_url,
)
from app.api.routes.landing_property_workspace_payload import _property_distance_evidence_rows
from app.product.service import ProductService
from app.product.service import _property_alert_personal_fit_snapshot, _property_candidate_google_maps_url, _property_candidate_is_generic_listing_page, _property_candidate_matches_requested_location, _property_candidate_url_has_exact_location_probe, _property_candidate_url_has_location_probe, _property_search_location_hints
from app.product.service import _property_investment_underwriting_payload
from app.product.property_fact_enrichment import (
    PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION,
    property_fact_distance_importance_key,
    property_fact_distance_preference_keys,
    property_fact_coordinate_digest,
    property_fact_issue_provider_attestation,
    property_fact_source_fingerprint,
)
from app.product.property_location_research import (
    PROPERTY_FACT_OSM_QUERY_ENDPOINT,
    PROPERTY_FACT_OSM_QUERY_SCHEMA,
    property_fact_osm_nearby_query,
)
from app.services.fliplink import build_fliplink_packet_service
from app.services.onboarding import OnboardingService, flatten_property_search_preferences_snapshot
from app.services.property_billing import property_billing_event_updates, property_billing_invoice_handoffs, property_commercial_snapshot, property_worker_cap
from app.services import property_market_catalog
from app.services.heyy_whatsapp_service import redact_phone_number
from tests.product_test_helpers import build_product_client, build_property_client, seed_product_state, start_workspace


_TEST_DISTANCE_CLASSIFICATION_TAGS = {
    "nearest_library_m": {"amenity": "library"},
    "nearest_playground_m": {"leisure": "playground"},
    "nearest_shopping_center_m": {"shop": "mall"},
    "nearest_theatre_m": {"amenity": "theatre"},
    "nearest_supermarket_m": {"shop": "supermarket"},
}

_CANONICAL_CATALOG_DISTANCE_RADIUS_KEYS = (
    property_fact_distance_preference_keys(
        search_supported_only=True,
        canonical_only=True,
    )
)
_CANONICAL_DISTANCE_IMPORTANCE_STATES = (
    "must_have",
    "strong_wish",
    "nice_to_have",
    "avoid",
)


def _property_tour_manifest_nested_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested_value in value.items():
            keys.add(str(key))
            keys.update(_property_tour_manifest_nested_keys(nested_value))
    elif isinstance(value, list):
        for nested_value in value:
            keys.update(_property_tour_manifest_nested_keys(nested_value))
    return keys


def _attested_search_distance_facts(
    *,
    property_url: str,
    distances: dict[str, int],
    latitude: float = 48.2082,
    longitude: float = 16.3738,
) -> dict[str, object]:
    observed_at = datetime.now(timezone.utc)
    expires_at = observed_at + timedelta(hours=24)
    coordinate_expires_at = observed_at + timedelta(hours=6)
    query = property_fact_osm_nearby_query(latitude, longitude)
    source_fingerprint = property_fact_source_fingerprint(property_url)
    coordinate_digest = property_fact_coordinate_digest(latitude, longitude)
    evidence: dict[str, dict[str, object]] = {}
    for key, distance_m in distances.items():
        object_id = str(20_000_000 + sum(ord(value) for value in key))
        row: dict[str, object] = {
            "provider": "openstreetmap_overpass",
            "method": "straight_line_osm",
            "observed_at": observed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "freshness": "fresh",
            "confidence": 0.95,
            "source_key": key,
            "observed_key": key,
            "listing_url": urllib.parse.urldefrag(property_url)[0],
            "source_fingerprint": source_fingerprint,
            "coordinate_basis": "candidate_listing_coordinates",
            "coordinate_observed_at": observed_at.isoformat(),
            "coordinate_precision": "address",
            "coordinate_source": "listing_structured_coordinates",
            "coordinate_exact": True,
            "listing_latitude": latitude,
            "listing_longitude": longitude,
            "coordinate_digest": coordinate_digest,
            "query_endpoint_url": PROPERTY_FACT_OSM_QUERY_ENDPOINT,
            "query_digest": "sha256:"
            + hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "query_schema": PROPERTY_FACT_OSM_QUERY_SCHEMA,
            "receipt_url": (
                f"https://api.openstreetmap.org/api/0.6/node/{object_id}/1"
            ),
            "provider_object_id": object_id,
            "provider_object_type": "node",
            "provider_object_version": 1,
            "provider_object_timestamp": observed_at.isoformat(),
            "provider_object_changeset": "123456",
            "provider_observed_at": observed_at.isoformat(),
            "provider_expires_at": expires_at.isoformat(),
            "poi_latitude": latitude
            + math.degrees(float(distance_m) / 6_371_000.0),
            "poi_longitude": longitude,
            "poi_classification_tags": dict(
                _TEST_DISTANCE_CLASSIFICATION_TAGS[key]
            ),
            "attestation_version": PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION,
        }
        row["provider_attestation"] = property_fact_issue_provider_attestation(
            row,
            observed_value=distance_m,
        )
        evidence[key] = row
    return {
        "map_lat": latitude,
        "map_lng": longitude,
        "map_location_precision": "address",
        "map_location_source": "listing_structured_coordinates",
        "map_coordinate_evidence": {
            "exact": True,
            "trusted": True,
            "provider": "listing_structured_coordinates",
            "source_fingerprint": source_fingerprint,
            "latitude": latitude,
            "longitude": longitude,
            "coordinate_digest": coordinate_digest,
            "observed_at": observed_at.isoformat(),
            "expires_at": coordinate_expires_at.isoformat(),
        },
        **distances,
        "property_fact_evidence": evidence,
    }


def test_property_search_preferences_normalization_is_idempotent_and_preserves_raw_only_values() -> None:
    normalized = OnboardingService._normalize_property_search_preferences(
        {
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "future_raw_only_preference": {"mode": "keep"},
        }
    )

    serialized_sizes: list[int] = []
    for _ in range(10):
        normalized = OnboardingService._normalize_property_search_preferences(normalized)
        raw_preferences = dict(normalized.get("raw_preferences") or {})
        assert raw_preferences["future_raw_only_preference"] == {"mode": "keep"}
        assert "raw_preferences" not in raw_preferences
        assert "saved_shortlist_candidates" not in raw_preferences
        assert "search_agents" not in raw_preferences
        serialized_sizes.append(len(json.dumps(normalized, ensure_ascii=True, sort_keys=True)))

    assert len(set(serialized_sizes)) == 1


def test_property_search_preferences_normalization_preserves_explicit_agents_across_round_trips() -> None:
    normalized = OnboardingService._normalize_property_search_preferences(
        {
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "active_search_agent_id": "agent-vienna-family",
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
            "search_agents": [
                {
                    "agent_id": "agent-vienna-family",
                    "name": "Vienna family homes",
                    "country_code": "AT",
                    "listing_mode": "rent",
                    "location_query": "Wien",
                    "selected_platforms": ["willhaben"],
                    "enabled": True,
                    "duration_days": 45,
                    "notification_limit": 7,
                    "notification_period": "day",
                },
                {
                    "agent_id": "agent-graz-investment",
                    "name": "Graz investment homes",
                    "country_code": "AT",
                    "listing_mode": "buy",
                    "location_query": "Graz",
                    "selected_platforms": ["willhaben"],
                    "enabled": False,
                    "duration_days": 90,
                    "notification_limit": 3,
                    "notification_period": "week",
                }
            ],
        }
    )
    expected_agents = [dict(agent) for agent in list(normalized.get("search_agents") or [])]
    assert [agent["agent_id"] for agent in expected_agents] == [
        "agent-vienna-family",
        "agent-graz-investment",
    ]

    for _ in range(10):
        agents = list(normalized.get("search_agents") or [])
        assert agents == expected_agents
        assert normalized["active_search_agent_id"] == "agent-vienna-family"
        assert "search_agents" not in dict(normalized.get("raw_preferences") or {})
        normalized = OnboardingService._normalize_property_search_preferences(normalized)


def test_property_search_preferences_normalization_flattens_full_legacy_chain_with_outer_precedence() -> None:
    normalized = OnboardingService._normalize_property_search_preferences(
        {
            "country_code": "AT",
            "listing_mode": "buy",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "investment_strategy": "appreciation",
            "raw_preferences": {
                "alert_frequency": "weekday",
                "raw_preferences": {
                    "investment_strategy": "cash_flow",
                    "include_shared_housing_sources": True,
                    "raw_preferences": {"legacy_raw_only_leaf": "preserve-deep-value"},
                },
            },
        }
    )

    raw_preferences = dict(normalized.get("raw_preferences") or {})
    assert raw_preferences["investment_strategy"] == "appreciation"
    assert raw_preferences["alert_frequency"] == "weekday"
    assert raw_preferences["include_shared_housing_sources"] is True
    assert raw_preferences["legacy_raw_only_leaf"] == "preserve-deep-value"
    assert "raw_preferences" not in raw_preferences


def test_property_search_preferences_snapshot_flattening_fails_closed_at_depth_bound() -> None:
    nested: dict[str, object] = {"deep_value": "must-not-be-silently-lost"}
    for _ in range(3):
        nested = {"raw_preferences": nested}

    with pytest.raises(ValueError, match="property_search_raw_preferences_depth_limit_exceeded"):
        flatten_property_search_preferences_snapshot(nested, max_depth=2)


def test_property_search_preferences_upsert_flattens_legacy_raw_snapshot_and_stays_bounded() -> None:
    principal_id = "exec-property-search-raw-preferences-idempotent"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Raw Preferences Office")
    onboarding = client.app.state.container.onboarding

    state = onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Wien",
            "raw_preferences": {
                "location_query": "Graz",
                "future_raw_only_preference": "preserve-me",
                "raw_preferences": {
                    "location_query": "legacy-nested-value",
                    "legacy_nested_raw_only_value": "preserve-me-too",
                },
            },
        },
    )

    serialized_sizes: list[int] = []
    for _ in range(8):
        preferences = dict(state.get("property_search_preferences") or {})
        raw_preferences = dict(preferences.get("raw_preferences") or {})
        assert preferences["location_query"] == "Wien"
        assert raw_preferences["location_query"] == "Wien"
        assert raw_preferences["future_raw_only_preference"] == "preserve-me"
        assert "raw_preferences" not in raw_preferences
        assert "saved_shortlist_candidates" not in raw_preferences
        assert "search_agents" not in raw_preferences
        assert raw_preferences["legacy_nested_raw_only_value"] == "preserve-me-too"
        serialized_sizes.append(len(json.dumps(preferences, ensure_ascii=True, sort_keys=True)))
        state = onboarding.upsert_property_search_preferences(
            principal_id=principal_id,
            property_search_preferences_json=preferences,
        )

    # The first persisted round trip may add derived agent/commercial defaults;
    # subsequent full-state saves must remain stable instead of nesting again.
    assert len(set(serialized_sizes[1:])) == 1
    assert max(serialized_sizes) < min(serialized_sizes) * 2


def test_property_search_run_merge_flattens_storage_and_projects_only_run_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-search-run-legacy-raw"
    client = build_property_client(principal_id=principal_id)
    service = ProductService(client.app.state.container)
    monkeypatch.setattr(
        client.app.state.container.onboarding,
        "status",
        lambda *, principal_id: {
            "property_search_preferences": {
                "country_code": "AT",
                "listing_mode": "rent",
                "location_query": "Wien",
                "selected_platforms": ["willhaben"],
                "raw_preferences": {
                    "future_raw_only_preference": "preserve-me",
                    "raw_preferences": {
                        "investment_strategy": "cash_flow",
                        "alert_frequency": "weekday",
                        "include_shared_housing_sources": True,
                        "raw_preferences": {"legacy_raw_only_leaf": "preserve-deep-value"},
                    },
                },
            }
        },
    )

    merged = service._merged_raw_property_search_preferences(
        principal_id=principal_id,
        property_preferences=None,
    )
    assert merged["future_raw_only_preference"] == "preserve-me"
    assert merged["investment_strategy"] == "cash_flow"
    assert merged["alert_frequency"] == "weekday"
    assert merged["include_shared_housing_sources"] is True
    assert merged["legacy_raw_only_leaf"] == "preserve-deep-value"
    assert "raw_preferences" not in merged

    _platforms, resolved, _max_results = service._resolve_property_search_run_preferences(
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_preferences=None,
        max_results_per_source=1,
        force_refresh=False,
    )
    # Durable preferences retain unknown legacy/future fields for lossless
    # account round trips, but run snapshots admit only recognized execution
    # inputs so arbitrary account state cannot leak into each search record.
    assert "future_raw_only_preference" not in resolved
    assert resolved["investment_strategy"] == "cash_flow"
    assert resolved["alert_frequency"] == "weekday"
    assert resolved["include_shared_housing_sources"] is True
    assert "legacy_raw_only_leaf" not in resolved
    assert "raw_preferences" not in resolved

    record = product_service._new_property_search_run_record(
        run_id="run-legacy-raw-preferences",
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences=resolved,
        force_refresh=False,
    )
    assert "raw_preferences" not in dict(record["property_search_preferences"])


def test_property_search_compact_record_hydrates_preview_from_provider_media_diagnostics() -> None:
    compact = property_search_storage._compact_property_search_run_record(
        {
            "run_id": "run-preview-media",
            "principal_id": "user-1",
            "status": "processed",
            "summary": {
                "status": "processed",
                "ranked_candidates": [
                    {
                        "candidate_ref": "cand-preview-media",
                        "title": "Provider home",
                        "property_url": "https://www.willhaben.at/iad/immobilien/d/demo",
                        "fit_score": 72,
                        "property_facts": {
                            "floorplan_urls_json": ["https://cache.example.test/mmo/1/floorplan.jpg"],
                            "floorplan_recovery_diagnostics": {
                                "candidate_document_or_media_urls": [
                                    "https://www.willhaben.at/iad/myprofile/login",
                                    "https://cache.example.test/mmo/logo/provider.png",
                                    "https://cache.example.test/mmo/1/photo_thumb.jpg",
                                    "https://cache.example.test/mmo/1/photo.jpg",
                                ],
                            },
                        },
                    }
                ],
            },
        }
    )

    candidate = compact["summary"]["ranked_candidates"][0]
    assert candidate["preview_image_url"] == "https://cache.example.test/mmo/1/photo.jpg"


def test_property_search_compact_record_keeps_bounded_delivery_projection() -> None:
    compact = property_search_storage._compact_property_search_run_record(
        {
            "run_id": "run-delivery-projection",
            "principal_id": "principal-delivery-projection",
            "status": "processed",
            "summary": {
                "status": "processed",
                "sources": [
                    {
                        "source_label": "Willhaben",
                        "top_candidates": [
                            {
                                "candidate_ref": "candidate-delivery-projection",
                                "title": "Pending tour",
                                "property_url": "https://example.test/pending-tour",
                                "source_ref": "source-delivery-projection",
                                "tour_status": "pending",
                                "property_facts": {
                                    "has_floorplan": True,
                                    "provider_diagnostics": "drop-me" * 10_000,
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )

    summary = dict(compact["summary"])
    projection = [dict(row) for row in list(summary["_delivery_candidates"])]
    assert compact["compact_schema_version"] == (
        property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION
    )
    assert summary["eligible_tour_total"] == 1
    assert summary["pending_tour_total"] == 1
    assert summary["ready_tour_total"] == 0
    assert "top_candidates" not in summary["sources"][0]
    assert projection == [
        {
            "candidate_ref": "candidate-delivery-projection",
            "title": "Pending tour",
            "property_url": "https://example.test/pending-tour",
            "source_ref": "source-delivery-projection",
            "tour_status": "pending",
            "property_facts": {"has_floorplan": True},
        }
    ]
    assert property_search_storage._property_search_run_compact_supports_delivery(compact) is True
    legacy = dict(compact)
    legacy.pop("compact_schema_version")
    assert property_search_storage._property_search_run_compact_supports_delivery(legacy) is False


def test_property_search_truncated_delivery_projection_stays_scheduled_for_hydration() -> None:
    candidate_limit = property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_DELIVERY_CANDIDATE_LIMIT
    compact = property_search_storage._compact_property_search_run_record(
        {
            "run_id": "run-truncated-delivery-projection",
            "principal_id": "principal-truncated-delivery-projection",
            "status": "processed",
            "summary": {
                "ranked_candidates": [
                    {
                        "candidate_ref": f"candidate-{index}",
                        "tour_status": "ready" if index < candidate_limit else "pending",
                    }
                    for index in range(candidate_limit + 1)
                ]
            },
        }
    )

    summary = dict(compact["summary"])
    assert len(summary["_delivery_candidates"]) == candidate_limit
    assert summary["_delivery_projection_truncated"] is True
    assert summary["_delivery_projection_total"] == candidate_limit + 1
    assert summary["eligible_tour_total"] == candidate_limit + 1
    assert summary["pending_tour_total"] == 1
    assert compact["delivery_pending"] is True
    assert property_search_storage._property_search_run_compact_supports_delivery(compact) is False
    recompacted = property_search_storage._compact_property_search_run_record(compact)
    assert recompacted["summary"]["_delivery_projection_truncated"] is True
    assert recompacted["delivery_pending"] is True


def test_property_search_compact_ui_arrays_and_sources_are_strictly_bounded() -> None:
    candidate_total = property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_UI_CANDIDATE_LIMIT + 5
    source_total = property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SOURCE_LIMIT + 5
    oversized_text = "x" * (property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_TEXT_LIMIT + 500)

    compact = property_search_storage._compact_property_search_run_record(
        {
            "run_id": "run-bounded-ui",
            "principal_id": "principal-bounded-ui",
            "status": "processed",
            "summary": {
                "ranked_candidates": [
                    {
                        "candidate_ref": f"candidate-{index}",
                        "title": oversized_text,
                        "fit_score": 80,
                    }
                    for index in range(candidate_total)
                ],
                "results": [{"candidate_ref": f"result-{index}"} for index in range(candidate_total)],
                "top_candidates": [{"candidate_ref": f"top-{index}"} for index in range(candidate_total)],
                "sources": [{"source_label": f"Source {index}"} for index in range(source_total)],
            },
        }
    )

    summary = dict(compact["summary"])
    for key in ("ranked_candidates", "results", "top_candidates"):
        assert len(summary[key]) == property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_UI_CANDIDATE_LIMIT
    assert len(summary["sources"]) == property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SOURCE_LIMIT
    assert len(summary["ranked_candidates"][0]["title"]) == property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_TEXT_LIMIT


def test_property_search_compact_record_never_infers_unverified_tour_url_as_ready() -> None:
    compact = property_search_storage._compact_property_search_run_record(
        {
            "run_id": "run-unverified-tour",
            "principal_id": "principal-unverified-tour",
            "status": "processed",
            "summary": {
                "ranked_candidates": [
                    {
                        "candidate_ref": "candidate-unverified-tour",
                        "property_url": "https://example.test/unverified-tour",
                        "vendor_tour_url": "https://vendor.example.test/unverified-tour",
                    }
                ]
            },
        }
    )

    summary = dict(compact["summary"])
    assert summary["eligible_tour_total"] == 1
    assert summary["ready_tour_total"] == 0
    assert summary["pending_tour_total"] == 1


def test_property_search_compact_record_never_counts_generated_layout_as_verified_ready() -> None:
    generated_tour_url = "https://propertyquarry.com/tours/generated-layout-only"
    compact = property_search_storage._compact_property_search_run_record(
        {
            "run_id": "run-generated-layout-only",
            "principal_id": "principal-generated-layout-only",
            "status": "processed",
            "summary": {
                "ranked_candidates": [
                    {
                        "candidate_ref": "candidate-generated-layout-only",
                        "property_url": "https://example.test/generated-layout-only",
                        "tour_status": "ready",
                        "tour_url": "",
                        "generated_reconstruction_url": generated_tour_url,
                    }
                ]
            },
        }
    )

    summary = dict(compact["summary"])
    projection = dict(summary["_delivery_candidates"][0])
    assert summary["eligible_tour_total"] == 1
    assert summary["ready_tour_total"] == 0
    assert summary["blocked_tour_total"] == 1
    assert summary["pending_tour_total"] == 0
    assert projection["generated_reconstruction_url"] == generated_tour_url
    assert projection["tour_status"] == "blocked"
    assert projection["blocked_reason"] == "property_tour_rebuild_required"


def test_property_search_compact_record_counts_verified_open_only_tour_as_ready() -> None:
    verified_open_tour_url = (
        "https://propertyquarry.com/tours/verified-open-only/control/matterport"
    )
    compact = property_search_storage._compact_property_search_run_record(
        {
            "run_id": "run-verified-open-only",
            "principal_id": "principal-verified-open-only",
            "status": "processed",
            "summary": {
                "ranked_candidates": [
                    {
                        "candidate_ref": "candidate-verified-open-only",
                        "property_url": "https://example.test/verified-open-only",
                        "tour_status": "ready",
                        "tour_url": "",
                        "open_tour_url": verified_open_tour_url,
                        "verified_tour_url": verified_open_tour_url,
                    }
                ]
            },
        }
    )

    summary = dict(compact["summary"])
    projection = dict(summary["_delivery_candidates"][0])
    assert summary["eligible_tour_total"] == 1
    assert summary["ready_tour_total"] == 1
    assert summary["blocked_tour_total"] == 0
    assert summary["pending_tour_total"] == 0
    assert projection["tour_status"] == "ready"
    assert projection["verified_tour_url"] == verified_open_tour_url


def test_property_workbench_ai_tour_payload_preserves_disclosure_and_safe_label() -> None:
    generated_tour_url = "/tours/generated-layout-only"
    payload = property_workspace_payload._property_workbench_client_tour_payload(
        {
            "status": "ready",
            "label": "Open AI-generated 3D tour",
            "provider_label": "AI-generated 3D tour",
            "provider_key": "generated_reconstruction",
            "tour_media_mode": "generated_reconstruction",
        },
        validated_url=generated_tour_url,
    )

    assert payload["status"] == "ready"
    assert payload["url"] == generated_tour_url
    assert payload["label"] == "Open AI-generated 3D tour"
    assert payload["provider_label"] == "AI-generated 3D tour"
    assert payload["tour_media_mode"] == "generated_reconstruction"
    assert "AI-generated from the floor plan and listing photos" in str(payload["status_detail"])
    assert "not a captured 360° tour" in str(payload["status_detail"])


def test_property_workbench_generated_layout_never_populates_verified_tour_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated_tour_url = "/tours/generated-layout-only"
    monkeypatch.setattr(
        property_workspace_payload,
        "_property_visual_layout_preview_url",
        lambda value, **_kwargs: (
            generated_tour_url
            if str(value or "").strip() == generated_tour_url
            else ""
        ),
    )
    monkeypatch.setattr(
        property_workspace_payload.property_tour_hosting,
        "_hosted_property_tour_verified_open_url",
        lambda *_args, **_kwargs: "",
    )

    payload = property_workspace_payload._property_workbench_client_candidate_payload(
        {
            "candidate_ref": "candidate-generated-layout-only",
            "title": "Generated layout only",
            "property_url": "https://example.test/generated-layout-only",
            "tour_status": "blocked",
            "generated_reconstruction_url": generated_tour_url,
            "layout_preview_url": generated_tour_url,
            "layout_preview_status": "ready",
            "tour_media_mode": "generated_reconstruction",
            "tour": {
                "status": "blocked",
                "label": "Open AI-generated 3D tour",
                "provider_key": "generated_reconstruction",
                "provider_label": "AI-generated 3D tour",
                "tour_media_mode": "generated_reconstruction",
            },
        }
    )

    assert payload["tour_status"] == "blocked"
    assert payload["tour_url"] == ""
    assert payload["verified_tour_url"] == ""
    assert payload["open_tour_url"] == generated_tour_url
    assert payload["layout_preview_status"] == "ready"
    assert payload["tour"]["status"] == "ready"
    assert payload["tour"]["label"] == "Open AI-generated 3D tour"
    assert "not a captured 360° tour" in str(payload["tour"]["status_detail"])


def test_property_search_delivery_work_filter_runs_before_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    registry: dict[str, dict[str, object]] = {}
    for index in range(40):
        registry[f"ready-{index}"] = {
            "run_id": f"ready-{index}",
            "principal_id": "principal-delivery-work",
            "status": "processed",
            "updated_at": f"2026-07-15T10:{59 - index:02d}:00+00:00",
            "summary": {
                "eligible_tour_total": 1,
                "pending_tour_total": 0,
                "ready_tour_total": 1,
                "blocked_tour_total": 0,
            },
        }
    registry["older-pending"] = {
        "run_id": "older-pending",
        "principal_id": "principal-delivery-work",
        "status": "processed",
        "updated_at": "2026-07-14T10:00:00+00:00",
        "summary": {
            "eligible_tour_total": 1,
            "pending_tour_total": 1,
            "ready_tour_total": 0,
            "blocked_tour_total": 0,
        },
    }
    registry["newer-pending"] = {
        "run_id": "newer-pending",
        "principal_id": "principal-delivery-work",
        "status": "processed",
        "updated_at": "2026-07-15T11:00:00+00:00",
        "summary": {
            "eligible_tour_total": 1,
            "pending_tour_total": 1,
            "ready_tour_total": 0,
            "blocked_tour_total": 0,
        },
    }

    rows = property_search_storage._list_property_search_run_records(
        limit=1,
        statuses=("processed",),
        principal_id="principal-delivery-work",
        lightweight=True,
        delivery_work_only=True,
        registry=registry,
    )

    assert [row["run_id"] for row in rows] == ["older-pending"]


def test_property_search_delivery_work_query_prioritizes_schema_then_durable_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            observed["query"] = str(query)
            observed["params"] = params

        def fetchall(self):
            return []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(property_search_storage, "_property_search_run_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(property_search_storage, "_require_property_search_run_schema", lambda: None)
    monkeypatch.setattr(property_search_storage, "_property_search_run_connect", lambda: _Connection())

    rows = property_search_storage._list_property_search_run_records(
        limit=40,
        statuses=("processed",),
        principal_id="",
        admin=True,
        lightweight=True,
        delivery_work_only=True,
    )

    assert rows == ()
    normalized_query = " ".join(str(observed["query"]).split())
    assert "delivery_checked_at" in normalized_query
    assert (
        "ORDER BY compact_schema_version ASC, delivery_checked_at ASC NULLS FIRST, updated_at ASC"
        in normalized_query
    )
    assert observed["params"] == [
        ["processed"],
        property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
        40,
    ]


def test_property_search_compact_backfill_uses_updated_at_compare_and_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            observed["query"] = str(query)
            observed["params"] = params

        def fetchone(self):
            return None

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(property_search_storage, "_property_search_run_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(property_search_storage, "_require_property_search_run_schema", lambda: None)
    monkeypatch.setattr(property_search_storage, "_property_search_run_connect", lambda: _Connection())

    updated = property_search_storage._store_property_search_run_compact_record(
        {
            "run_id": "run-cas-loser",
            "principal_id": "principal-cas-loser",
            "status": "processed",
            "updated_at": "2026-07-15T10:00:00+00:00",
            "summary": {
                "eligible_tour_total": 0,
                "pending_tour_total": 0,
                "ready_tour_total": 0,
                "blocked_tour_total": 0,
            },
        }
    )

    assert updated is False
    assert "updated_at = %s::timestamptz" in str(observed["query"])
    assert "delivery_checked_at IS NOT DISTINCT FROM %s::timestamptz" in str(observed["query"])
    assert "RETURNING 1" in str(observed["query"])
    assert observed["params"][5] == "2026-07-15T10:00:00+00:00"
    assert observed["params"][6] is None


def test_property_search_delivery_checked_touch_uses_both_cas_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            observed["query"] = str(query)
            observed["params"] = params

        def fetchone(self):
            return (1,)

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(property_search_storage, "_property_search_run_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(property_search_storage, "_require_property_search_run_schema", lambda: None)
    monkeypatch.setattr(property_search_storage, "_property_search_run_connect", lambda: _Connection())

    updated = property_search_storage._mark_property_search_run_delivery_checked(
        {
            "run_id": "run-delivery-touch",
            "principal_id": "principal-delivery-touch",
            "updated_at": "2026-07-15T10:00:00+00:00",
            "delivery_checked_at": "2026-07-15T10:05:00+00:00",
        }
    )

    assert updated is True
    assert "updated_at = %s::timestamptz" in str(observed["query"])
    assert "delivery_checked_at IS NOT DISTINCT FROM %s::timestamptz" in str(observed["query"])
    assert observed["params"] == (
        "run-delivery-touch",
        "principal-delivery-touch",
        "2026-07-15T10:00:00+00:00",
        "2026-07-15T10:05:00+00:00",
    )


def test_property_search_compact_delivery_projection_refreshes_without_full_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "principal-delivery-refresh"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    compact = property_search_storage._compact_property_search_run_record(
        {
            "run_id": "run-delivery-refresh",
            "principal_id": principal_id,
            "status": "processed",
            "summary": {
                "sources": [
                    {
                        "source_label": "Willhaben",
                        "top_candidates": [
                            {
                                "candidate_ref": "candidate-delivery-refresh",
                                "property_url": "https://example.test/delivery-refresh",
                                "source_ref": "source-delivery-refresh",
                                "tour_status": "pending",
                                "property_facts": {"has_floorplan": True},
                            }
                        ],
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_verified_open_url",
        lambda value, *, principal_id="": str(value or ""),
    )

    refreshed = service._refresh_property_search_results_delivery_state(
        principal_id=principal_id,
        result=dict(compact["summary"]),
        tour_events_by_source={
            "source-delivery-refresh": [
                {
                    "event_type": "property_tour_ready",
                    "payload": {
                        "property_url": "https://example.test/delivery-refresh",
                        "tour_url": "https://propertyquarry.com/tours/delivery-refresh",
                    },
                }
            ]
        },
    )

    assert refreshed["pending_tour_total"] == 0
    assert refreshed["ready_tour_total"] == 1
    assert refreshed["_delivery_candidates"][0]["tour_status"] == "ready"
    assert refreshed["_delivery_candidates"][0]["tour_url"] == "https://propertyquarry.com/tours/delivery-refresh"


def test_property_search_delivery_refresh_does_not_receipt_unresolved_candidate() -> None:
    principal_id = "principal-unresolved-delivery"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)

    refreshed = service._refresh_property_search_results_delivery_state(
        principal_id=principal_id,
        result={
            "eligible_tour_total": 1,
            "pending_tour_total": 1,
            "ready_tour_total": 0,
            "blocked_tour_total": 0,
            "_delivery_candidates": [
                {
                    "candidate_ref": "candidate-unresolved-delivery",
                    "source_ref": "source-unresolved-delivery",
                    "tour_status": "created",
                }
            ],
            "timing_receipts": {"run_started_at": "2026-07-15T10:00:00+00:00"},
        },
        tour_events_by_source={},
    )

    assert refreshed["eligible_tour_total"] == 1
    assert refreshed["pending_tour_total"] == 0
    assert refreshed["ready_tour_total"] == 0
    assert refreshed["blocked_tour_total"] == 0
    assert service._property_search_results_delivery_pending(result=refreshed) is True
    assert "results_delivery_ready_at" not in dict(refreshed.get("timing_receipts") or {})


def test_property_search_lightweight_status_hydrates_preview_from_provider_media_diagnostics() -> None:
    candidate = _property_search_lightweight_candidate_payload(
        {
            "candidate_ref": "cand-preview-media",
            "title": "Provider home",
            "property_url": "https://www.willhaben.at/iad/immobilien/d/demo",
            "fit_score": 72,
            "property_facts": {
                "floorplan_recovery_diagnostics": {
                    "candidate_document_or_media_urls": [
                        "https://cache.example.test/mmo/logo/provider.png",
                        "https://cache.example.test/mmo/1/photo_thumb.jpg",
                        "https://cache.example.test/mmo/1/photo.jpg",
                    ],
                },
            },
        },
        run_id="run-preview-media",
        index=1,
    )

    assert candidate["preview_image_url"] == "https://cache.example.test/mmo/1/photo.jpg"
    assert "floorplan_recovery_diagnostics" not in candidate.get("property_facts", {})


def test_property_search_lightweight_candidate_payload_summarizes_provider_marketing_copy() -> None:
    marketing_copy = (
        "UNBEFRISTETE MIETDAUER | MITTEN IN DER STADT | 3 ZIMMER. "
        "Wählen Sie aus 113.283 Angeboten. Immobilien suchen und finden auf willhaben."
    )
    candidate = _property_search_lightweight_candidate_payload(
        {
            "candidate_ref": "cand-marketing-copy",
            "title": "Charmante Altbauwohnung - willhaben",
            "property_url": "https://www.willhaben.at/iad/immobilien/d/demo",
            "summary": marketing_copy,
            "fit_summary": marketing_copy,
        },
        run_id="run-marketing-copy",
        index=1,
    )

    assert candidate["title"] == "Charmante Altbauwohnung"
    assert candidate["summary"] == "Unbefristete Mietdauer, mitten in der Stadt, 3 Zimmer."
    assert candidate["fit_summary"] == "Unbefristete Mietdauer, mitten in der Stadt, 3 Zimmer."
    assert "Wählen Sie aus" not in candidate["summary"]
    assert "Immobilien suchen und finden" not in candidate["summary"]


def test_property_search_lightweight_candidate_payload_derives_diorama_preview_from_ready_generated_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diorama_url = "https://cdn.example.test/lightweight-derived-diorama.png"
    generated_reconstruction_url = "https://propertyquarry.com/tours/lightweight-derived-layout"
    listing_photo_url = "https://cdn.example.test/lightweight-derived-photo.jpg"

    monkeypatch.setattr(
        product_api_delivery_routes,
        "_property_visual_ready_tour_url",
        lambda *, tour_url="", open_tour_url="": (
            generated_reconstruction_url
            if str(tour_url or open_tour_url).strip() == generated_reconstruction_url
            else ""
        ),
    )
    monkeypatch.setattr(
        product_api_delivery_routes,
        "_hosted_property_tour_telegram_preview_image_url_for_style",
        lambda tour_url, *, diorama_style_hint="": diorama_url if tour_url == generated_reconstruction_url else "",
    )

    candidate = _property_search_lightweight_candidate_payload(
        {
            "candidate_ref": "cand-lightweight-derived-diorama",
            "title": "Lightweight generated layout flat",
            "property_url": "https://example.test/source/lightweight-derived-diorama",
            "tour_url": generated_reconstruction_url,
            "preview_image_url": listing_photo_url,
            "property_facts": {
                "price_display": "EUR 1,980",
                "monthly_rent_eur": 1980,
                "area_m2": 81,
                "rooms": 3,
                "postal_name": "1020 Wien",
                "preview_image_url": listing_photo_url,
                "image_url": listing_photo_url,
                "media_urls_json": [listing_photo_url],
            },
        },
        run_id="run-lightweight-derived-diorama",
        index=1,
    )

    assert candidate["preview_image_url"] == listing_photo_url
    assert candidate["diorama_preview_url"] == diorama_url


def test_property_distance_evidence_rows_include_named_nearest_amenity_and_source() -> None:
    rows = _property_distance_evidence_rows(
        {
            "nearest_supermarket_m": 280,
            "nearest_supermarket_name": "BILLA Praterstern",
            "nearest_supermarket_source": "OpenStreetMap",
            "nearest_pharmacy_m": 640,
            "nearest_pharmacy_name": "Apotheke Nordbahn",
        },
        include_family_only=False,
    )

    assert rows[0]["label"] == "Pharmacy"
    supermarket = next(row for row in rows if row["label"] == "Supermarket")
    assert supermarket["title"] == "Supermarket: BILLA Praterstern"
    assert supermarket["value"] == "280 m"
    assert supermarket["inline"] == "Supermarket BILLA Praterstern 280 m | 1 min bike"
    assert supermarket["detail"] == "about 1 min by bike | source: OpenStreetMap"


def test_property_distance_evidence_rows_suppress_empty_or_invalid_distance_noise() -> None:
    rows = _property_distance_evidence_rows(
        {
            "nearest_supermarket_m": 0,
            "nearest_supermarket_name": "BILLA without confirmed distance",
            "nearest_pharmacy_m": "",
            "nearest_subway_m": "unknown",
        },
        include_family_only=False,
    )

    assert rows == []


def test_property_distance_evidence_rows_keep_family_only_amenities_scoped_to_family_filters() -> None:
    facts = {
        "nearest_playground_m": 310,
        "nearest_playground_name": "Rudolfspark Spielplatz",
        "nearest_supermarket_m": 280,
        "nearest_supermarket_name": "BILLA Praterstern",
    }

    non_family_rows = _property_distance_evidence_rows(facts, include_family_only=False)
    family_rows = _property_distance_evidence_rows(facts, include_family_only=True)

    assert [row["label"] for row in non_family_rows] == ["Supermarket"]
    assert [row["label"] for row in family_rows[:2]] == ["Playground", "Supermarket"]


def _poll_property_search_run_status(client, run_id: str) -> dict[str, object]:
    latest_status: dict[str, object] = {}
    for _ in range(120):
        response = client.get(f"/app/api/signals/property/search/run/{run_id}")
        assert response.status_code == 200, response.text
        latest_status = response.json()
        if str(latest_status.get("status") or "").strip() in {"processed", "completed_partial", "failed", "noop", "cancelled"}:
            return latest_status
        time.sleep(0.02)
    return latest_status


def test_free_property_plan_keeps_visible_results_uncapped() -> None:
    snapshot = property_commercial_snapshot({})

    assert snapshot["current_plan_key"] == "free"
    assert snapshot["research_depth"] == "standard"
    assert snapshot["investment_research_level"] == "none"
    assert snapshot["max_platforms"] == 3
    assert snapshot["max_results_per_source"] == 0
    assert snapshot["max_match_score"] == 35


def test_agent_property_plan_exposes_unlimited_results_per_provider() -> None:
    snapshot = property_commercial_snapshot(
        {"property_commercial": {"active_plan_key": "agent", "active_until": "2999-01-01T00:00:00+00:00"}}
    )

    assert snapshot["current_plan_key"] == "agent"
    assert snapshot["max_results_per_source"] == 0


def test_agency_lifetime_alias_maps_to_agent_unlimited_results() -> None:
    snapshot = property_commercial_snapshot(
        {
            "property_commercial": {
                "active_plan_key": "agency_lifetime",
                "status": "active",
            }
        }
    )

    assert snapshot["current_plan_key"] == "agent"
    assert snapshot["current_plan_label"] == "Agent"
    assert snapshot["max_results_per_source"] == 0
    assert snapshot["search_agent_limit"] == 0
    assert snapshot["active_until"].startswith("2999-01-01")
    assert property_worker_cap("agency") == property_worker_cap("agent")


def test_property_search_defined_max_results_value_preserves_explicit_zero() -> None:
    assert product_service._property_search_defined_max_results_value(  # type: ignore[attr-defined]
        {"max_results_per_source": 0},
        {"max_results_per_source": 2},
    ) == 0


def test_property_search_run_default_summary_carries_provider_filter_details() -> None:
    summary = product_service._property_search_run_default_summary(  # type: ignore[attr-defined]
        {
            "country_code": "AT",
            "provider_country_filter_applied": True,
            "provider_country_filter_removed": ["realestate_au"],
            "provider_country_filter_removed_details": [
                {
                    "platform": "realestate_au",
                    "provider_label": "realestate.com.au",
                    "reason": "wrong_country",
                    "requested_country_code": "AT",
                    "requested_country_label": "Austria",
                    "provider_country_code": "AU",
                    "provider_country_label": "Australia",
                    "requested_listing_mode": "rent",
                    "supported_listing_modes": ["rent", "buy"],
                    "search_ready": True,
                    "market_readiness": "private_beta",
                }
            ],
        }
    )

    assert summary["provider_country_filter_applied"] is True
    assert summary["provider_country_filter_removed"] == ["realestate_au"]
    assert summary["provider_country_filter_removed_details"] == [
        {
            "platform": "realestate_au",
            "provider_label": "realestate.com.au",
            "reason": "wrong_country",
            "requested_country_code": "AT",
            "requested_country_label": "Austria",
            "provider_country_code": "AU",
            "provider_country_label": "Australia",
            "requested_listing_mode": "rent",
            "supported_listing_modes": ["rent", "buy"],
            "search_ready": True,
            "market_readiness": "private_beta",
        }
    ]


def test_property_notification_price_signal_uses_catalog_currencies() -> None:
    assert product_service._property_candidate_notification_price_signal(  # type: ignore[attr-defined]
        {},
        listing_mode="buy",
        title="Sydney apartment | AUD 1,250,000 | 96 m2",
    ) == "AUD 1,250,000"


def test_property_search_compact_run_preserves_repair_lifecycle_fields() -> None:
    compact = property_search_storage._compact_property_search_run_record(  # type: ignore[attr-defined]
        {
            "run_id": "repair-run",
            "principal_id": "repair-principal",
            "status": "failed",
            "summary": {
                "status": "failed",
                "repair_status": "repairing",
                "repair_status_label": "Repairing",
                "repair_step_label": "Started a replacement search run from the saved brief.",
                "repair_outcome_summary": "Repair is retrying the interrupted search.",
                "repair_attempt_count": 1,
                "repair_replacement_run_id": "repair-run-retry",
                "repair_replacement_status_url": "/app/api/signals/property/search/run/repair-run-retry",
                "repair_resolved_total": 1,
                "repair_receipts": [
                    {
                        "run_id": "repair-run",
                        "filter_key": "run_worker_exception",
                        "resolution": "worker_exception_restart_required",
                    }
                ],
                "provider_repair_task_opened_total": 1,
                "provider_repair_task_existing_total": 0,
                "provider_repair_tasks": [
                    {
                        "status": "returned",
                        "filter_key": "run_worker_exception",
                        "resolution": "worker_exception_restart_required",
                    }
                ],
                "can_auto_repair": True,
            },
        }
    )

    summary = dict(compact["summary"])
    assert summary["repair_status"] == "repairing"
    assert summary["repair_step_label"] == "Started a replacement search run from the saved brief."
    assert summary["repair_replacement_run_id"] == "repair-run-retry"
    assert summary["repair_replacement_status_url"] == "/app/api/signals/property/search/run/repair-run-retry"
    assert summary["repair_resolved_total"] == 1
    assert summary["repair_receipts"][0]["resolution"] == "worker_exception_restart_required"
    assert summary["provider_repair_task_opened_total"] == 1
    assert summary["provider_repair_tasks"][0]["filter_key"] == "run_worker_exception"
    assert summary["can_auto_repair"] is True


def test_property_search_compact_run_preserves_run_entitlements() -> None:
    compact = property_search_storage._compact_property_search_run_record(  # type: ignore[attr-defined]
        {
            "run_id": "agent-run",
            "principal_id": "agent-principal",
            "property_search_preferences": {
                "country_code": "AT",
                "location_query": "1010 Vienna",
                "property_commercial": {
                    "active_plan_key": "agent",
                    "status": "active",
                    "active_until": "2999-01-01T00:00:00+00:00",
                },
            },
            "status": "in_progress",
            "summary": {
                "status": "in_progress",
                "current_plan_key": "agent",
                "current_plan_label": "Agent",
                "research_depth": "deep",
                "max_results_per_source": 0,
                "provider_workers": {"worker_concurrency": 4, "warm_limit": 3},
            },
        }
    )

    summary = dict(compact["summary"])
    preferences = dict(compact["property_search_preferences"])
    assert summary["current_plan_key"] == "agent"
    assert summary["current_plan_label"] == "Agent"
    assert summary["research_depth"] == "deep"
    assert summary["max_results_per_source"] == 0
    assert summary["provider_workers"] == {"worker_concurrency": 4, "warm_limit": 3}
    assert preferences["property_commercial"]["active_plan_key"] == "agent"


def test_property_search_compact_run_preserves_display_totals() -> None:
    compact = property_search_storage._compact_property_search_run_record(  # type: ignore[attr-defined]
        {
            "run_id": "display-total-run",
            "principal_id": "display-total-principal",
            "status": "in_progress",
            "provider_display_total": 29,
            "source_variant_display_total": 231,
            "summary": {
                "status": "in_progress",
                "provider_total": 2,
                "provider_display_total": 29,
                "sources_total": 2,
                "source_variant_total": 2,
                "source_variant_display_total": 231,
            },
        }
    )

    summary = dict(compact["summary"])
    assert compact["provider_display_total"] == 29
    assert compact["source_variant_display_total"] == 231
    assert summary["provider_display_total"] == 29
    assert summary["source_variant_display_total"] == 231


def test_property_search_compact_run_backfills_ranked_summary_counts() -> None:
    compact = property_search_storage._compact_property_search_run_record(  # type: ignore[attr-defined]
        {
            "run_id": "ranked-run",
            "principal_id": "ranked-principal",
            "status": "completed_partial",
            "summary": {
                "status": "completed_partial",
                "listing_total": 0,
                "reviewed_listing_total": 0,
                "scanned_listing_total": 0,
                "ranked_total": 0,
                "ranked_candidate_total": 0,
                "ranked_candidates": [
                    {"candidate_ref": "cand-1", "title": "Ranked One", "property_url": "https://example.test/one"},
                    {"candidate_ref": "cand-2", "title": "Ranked Two", "property_url": "https://example.test/two"},
                    {"candidate_ref": "cand-3", "title": "Ranked Three", "property_url": "https://example.test/three"},
                ],
            },
        }
    )

    summary = dict(compact["summary"])
    assert summary["listing_total"] == 3
    assert summary["ranked_total"] == 3
    assert summary["ranked_candidate_total"] == 3
    assert summary["reviewed_listing_total"] == 3
    assert summary["scanned_listing_total"] == 3


def test_property_search_compact_run_synthesizes_ranked_candidates_from_source_rows() -> None:
    compact = property_search_storage._compact_property_search_run_record(  # type: ignore[attr-defined]
        {
            "run_id": "source-ranked-run",
            "principal_id": "source-ranked-principal",
            "status": "completed_partial",
            "summary": {
                "status": "completed_partial",
                "listing_total": 0,
                "reviewed_listing_total": 0,
                "sources": [
                    {
                        "source_label": "Willhaben",
                        "listing_total": 0,
                        "reviewed_listing_total": 0,
                        "top_candidates": [
                            {
                                "candidate_ref": "cand-1",
                                "title": "Ranked One",
                                "property_url": "https://example.test/one",
                                "fit_score": 61,
                            },
                            {
                                "candidate_ref": "cand-2",
                                "title": "Ranked Two",
                                "property_url": "https://example.test/two",
                                "fit_score": 57,
                            },
                        ],
                    }
                ],
            },
        }
    )

    summary = dict(compact["summary"])
    assert len(summary["ranked_candidates"]) == 2
    assert summary["listing_total"] == 2
    assert summary["ranked_total"] == 2
    assert summary["ranked_candidate_total"] == 2
    assert summary["reviewed_listing_total"] == 2
    assert summary["sources"][0]["listing_total"] == 2
    assert summary["sources"][0]["reviewed_listing_total"] == 2


def test_property_search_compact_run_zeroes_stale_visible_counts_when_review_gate_kept_no_candidates() -> None:
    compact = property_search_storage._compact_property_search_run_record(  # type: ignore[attr-defined]
        {
            "run_id": "suppressed-run",
            "principal_id": "suppressed-principal",
            "status": "processed",
            "summary": {
                "status": "processed",
                "listing_total": 1,
                "ranked_total": 1,
                "reviewed_listing_total": 30,
                "sources": [
                    {
                        "source_label": "Willhaben",
                        "listing_total": 1,
                        "reviewed_listing_total": 30,
                        "review_created_total": 0,
                        "review_existing_total": 0,
                        "top_fit_score": 54.0,
                        "top_candidates": [],
                        "research_candidates": [],
                    }
                ],
            },
        }
    )

    summary = dict(compact["summary"])
    source = dict(summary["sources"][0])
    assert summary["listing_total"] == 0
    assert summary["ranked_total"] == 0
    assert summary["ranked_candidate_total"] == 0
    assert source["listing_total"] == 0
    assert source["top_fit_score"] == 0.0


def test_property_search_compact_run_backfills_visible_counts_from_review_packets_when_candidate_rows_are_pruned() -> None:
    compact = property_search_storage._compact_property_search_run_record(  # type: ignore[attr-defined]
        {
            "run_id": "review-only-run",
            "principal_id": "review-only-principal",
            "status": "processed",
            "summary": {
                "status": "processed",
                "listing_total": 7,
                "ranked_total": 7,
                "reviewed_listing_total": 42,
                "sources": [
                    {
                        "source_label": "Willhaben",
                        "listing_total": 7,
                        "reviewed_listing_total": 42,
                        "review_created_total": 1,
                        "review_existing_total": 1,
                        "top_candidates": [],
                        "research_candidates": [],
                    }
                ],
            },
        }
    )

    summary = dict(compact["summary"])
    source = dict(summary["sources"][0])
    assert summary["listing_total"] == 2
    assert summary["ranked_total"] == 2
    assert summary["ranked_candidate_total"] == 2
    assert source["listing_total"] == 2


def test_property_search_compact_run_backfills_missing_row_timestamps() -> None:
    compact = property_search_storage._compact_property_search_run_record_with_row_timestamps(  # type: ignore[attr-defined]
        {
            "run_id": "compact-run",
            "principal_id": "compact-principal",
            "status": "in_progress",
            "updated_at": None,
            "summary": {
                "status": "in_progress",
                "updated_at": None,
            },
        },
        created_at="2026-06-25T14:55:00+00:00",
        updated_at="2026-06-25T15:03:00+00:00",
    )

    assert compact is not None
    assert compact["created_at"] == "2026-06-25T14:55:00+00:00"
    assert compact["updated_at"] == "2026-06-25T15:03:00+00:00"
    assert compact["summary"]["updated_at"] == "2026-06-25T15:03:00+00:00"


def test_property_search_ranked_candidates_replace_search_url_title() -> None:
    ranked = product_service._property_search_ranked_candidates_from_sources(  # type: ignore[attr-defined]
        [
            {
                "source_label": "RE/MAX Austria",
                "top_candidates": [
                    {
                        "title": "https://www.remax.at/properties/propertysearch?q=1010+Vienna&maxPrice=1200&minArea=45",
                        "property_url": "https://www.remax.at/properties/propertysearch?q=1010+Vienna&maxPrice=1200&minArea=45",
                        "fit_score": 72,
                    }
                ],
            }
        ]
    )

    assert ranked[0]["title"] == "RE/MAX Austria · 1010 Vienna · search candidate"
    assert ranked[0]["display_title_was_url"] is True
    assert "PropertyQuarry is still extracting a concrete listing" in ranked[0]["summary"]


def test_property_search_repair_receipts_normalize_historical_top_level_tasks() -> None:
    client = build_property_client(principal_id="cf-email:historical.repair@example.com")
    service = ProductService(client.app.state.container)

    summary = service._apply_property_search_run_repair_receipts(  # type: ignore[attr-defined]
        summary={
            "repair_status": "repairing",
            "provider_repair_tasks": [
                {
                    "status": "opened",
                    "filter_key": "run_worker_exception",
                    "human_task_id": "human_task:old-repair",
                    "queue_item_ref": "human_task:old-repair",
                }
            ],
            "repair_receipts": [
                {
                    "human_task_id": "human_task:old-repair",
                    "filter_key": "run_worker_exception",
                    "resolution": "worker_exception_restart_required",
                    "reason": "started a fresh bounded run from the saved brief",
                    "replacement_run_id": "replacement-run",
                }
            ],
        }
    )

    assert summary["provider_repair_tasks"][0]["status"] == "returned"
    assert summary["provider_repair_tasks"][0]["resolution"] == "worker_exception_restart_required"
    assert summary["provider_repair_tasks"][0]["replacement_run_id"] == "replacement-run"
    assert summary["repair_replacement_run_id"] == "replacement-run"
    assert summary["repair_replacement_status_url"] == "/app/api/signals/property/search/run/replacement-run"


def test_property_plan_investment_research_levels_follow_tier() -> None:
    plus = property_commercial_snapshot(
        {"property_commercial": {"active_plan_key": "plus", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"}}
    )
    agent = property_commercial_snapshot(
        {"property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"}}
    )

    assert plus["investment_research_level"] == "preview"
    assert plus["research_depth"] == "deep"
    assert plus["max_platforms"] == 8
    assert plus["max_match_score"] == 45
    assert plus["magic_fit_scene_period"] == "day"
    assert plus["magic_fit_video_period"] == "day"
    assert agent["investment_research_level"] == "full"
    assert agent["research_depth"] == "deep"
    assert agent["max_platforms"] == 0
    assert agent["max_match_score"] == 60
    assert agent["magic_fit_scene_period"] == "none"
    assert agent["magic_fit_video_period"] == "none"


def test_property_billing_invoice_handoff_preserves_vat_and_document_state() -> None:
    updates = property_billing_event_updates(
        {},
        provider="payfunnels",
        event_type="payment.completed",
        event_id="evt-invoice-vat",
        plan_key="plus",
        order_id="pf-plus-vat",
        invoice_id="inv-vat-123",
        invoice_url="https://billing.example.test/invoices/inv-vat-123.pdf",
        invoice_status="issued",
        accounting_status="invoice_pending",
        payment_status="completed",
        currency="EUR",
        amount_eur="3.00",
        net_amount_eur="2.52",
        vat_amount_eur="0.48",
        vat_rate="20%",
    )

    handoffs = property_billing_invoice_handoffs({"billing_events_json": updates["billing_events_json"]})

    assert handoffs == [
        {
            "event_id": "evt-invoice-vat",
            "provider": "payfunnels",
            "plan_key": "plus",
            "order_id": "pf-plus-vat",
            "invoice_id": "inv-vat-123",
            "invoice_url": "https://billing.example.test/invoices/inv-vat-123.pdf",
            "state": "issued",
            "accounting_status": "invoice_pending",
            "invoice_status": "issued",
            "payment_status": "completed",
            "currency": "EUR",
            "amount_eur": "3.00",
            "net_amount_eur": "2.52",
            "vat_amount_eur": "0.48",
            "vat_rate": "20%",
            "recorded_at": handoffs[0]["recorded_at"],
        }
    ]


def test_property_search_preferences_use_school_evidence_priority_with_legacy_alias() -> None:
    normalized = property_market_catalog.normalize_property_search_preferences(
        {
            "school_evidence_priority": "very_important",
            "school_quality_priority": "important",
            "school_stage_preferences": ["volksschule"],
        }
    )
    legacy = property_market_catalog.normalize_property_search_preferences(
        {
            "school_quality_priority": "important",
            "school_stage_preferences": ["volksschule"],
        }
    )

    assert normalized["school_evidence_priority"] == "very_important"
    assert legacy["school_evidence_priority"] == "important"
    assert "school_quality_priority" not in normalized
    assert "school_quality_priority" not in legacy


def test_property_search_preferences_normalizer_drops_stale_agent_result_cap() -> None:
    normalized = property_market_catalog.normalize_property_search_preferences(
        {
            "max_results_per_source": 50,
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        }
    )

    assert "max_results_per_source" not in normalized


def test_property_search_preferences_normalizer_removes_paid_result_cap() -> None:
    normalized = property_market_catalog.normalize_property_search_preferences(
        {
            "max_results_per_source": 50,
            "property_commercial": {
                "active_plan_key": "plus",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        }
    )

    assert "max_results_per_source" not in normalized


def test_property_search_preferences_normalizer_keeps_what_matters_school_and_parking_controls() -> None:
    normalized = property_market_catalog.normalize_property_search_preferences(
        {
            "search_goal": "home",
            "enable_family_mode": True,
            "school_stage_preferences": ["kindergarten", "ganztags_volksschule"],
            "school_evidence_priority": "important",
            "max_distance_to_kindergarten_m": 400,
            "max_distance_to_kindergarten_importance": "must_have",
            "max_distance_to_ganztags_volksschule_m": 650,
            "max_distance_to_ganztags_volksschule_importance": "important",
            "max_distance_to_market_m": 1000,
            "max_distance_to_market_importance": "nice_to_have",
            "max_distance_to_supermarket_m": 500,
            "max_distance_to_supermarket_importance": "strong_wish",
            "max_distance_to_playground_m": 450,
            "max_distance_to_playground_importance": "avoid",
            "parking_pressure_preference": "low",
            "require_parking_pressure_check": True,
        }
    )

    assert normalized["school_stage_preferences"] == ["kindergarten", "ganztags_volksschule"]
    assert normalized["school_evidence_priority"] == "important"
    assert normalized["max_distance_to_kindergarten_m"] == 400
    assert normalized["max_distance_to_kindergarten_importance"] == "must_have"
    assert normalized["max_distance_to_ganztags_volksschule_m"] == 650
    assert normalized["max_distance_to_ganztags_volksschule_importance"] == "strong_wish"
    assert normalized["max_distance_to_market_m"] == 1000
    assert normalized["max_distance_to_market_importance"] == "nice_to_have"
    assert normalized["max_distance_to_supermarket_m"] == 500
    assert normalized["max_distance_to_supermarket_importance"] == "strong_wish"
    assert normalized["max_distance_to_playground_m"] == 450
    assert normalized["max_distance_to_playground_importance"] == "avoid"
    assert normalized["parking_pressure_preference"] == "low"
    assert normalized["require_parking_pressure_check"] is True


def test_property_candidate_google_maps_url_prefers_listing_snapshot_locality_over_source_scope_placeholder() -> None:
    candidate = {
        "title": "expat flat",
        "property_facts": {
            "postal_name": "1010 Vienna",
            "source_scope_location": "1010 Vienna",
            "source_postal_code": "1010",
            "listing_research_snapshot": {
                "address": "Brunnthalgasse 1B, 1020 Wien",
                "postal_name": "1020 Wien",
            },
        },
    }

    url = _property_candidate_google_maps_url(candidate)

    assert "Brunnthalgasse%201B%2C%201020%20Wien" in url


def test_property_candidate_google_maps_url_uses_listing_text_postal_over_dirty_source_scope() -> None:
    candidate = {
        "title": "Wohnung mieten in 1220 Wien | 60 m² | 2 Zimmer | € 1.090 | DER STANDARD",
        "summary": "2-Zimmer Wohnung mit Traumblick / UNO und U-Bahn ums Eck in 1220 Wien.",
        "property_facts": {
            "postal_name": "1010 Vienna",
            "district": "1010 Vienna",
            "address": "1010 Vienna",
            "source_scope_location": "1010 Vienna",
            "source_postal_code": "1010",
            "source_city": "Vienna",
        },
    }

    url = _property_candidate_google_maps_url(candidate)

    assert "1220%20Wien" in url
    assert "1010%20Vienna" not in url


def test_property_worker_caps_follow_plan() -> None:
    assert property_worker_cap("free") == 1
    assert property_worker_cap("plus") == 2
    assert property_worker_cap("agent") == 4


def test_ranked_candidates_prefer_explicit_ranking_score_when_present() -> None:
    ranked = product_service._property_search_ranked_candidates_from_sources(
        [
            {
                "source_label": "Source A",
                "top_candidates": [
                    {"source_ref": "a", "fit_score": 92, "ranking_score": 48, "title": "Home-biased high fit"},
                    {"source_ref": "b", "fit_score": 70, "ranking_score": 83, "title": "Better investment score"},
                ],
            }
        ]
    )

    assert [row["source_ref"] for row in ranked[:2]] == ["b", "a"]


def test_ranked_candidates_return_all_rows_without_global_slice_by_default() -> None:
    candidates = [
        {
            "source_ref": f"home-{index:02d}",
            "fit_score": 100 - index,
            "ranking_score": 100 - index,
            "title": f"Ranked home {index:02d}",
        }
        for index in range(75)
    ]

    default_ranked = product_service._property_search_ranked_candidates_from_sources(
        [{"source_label": "Source A", "research_candidates": candidates}]
    )
    unlimited_ranked = product_service._property_search_ranked_candidates_from_sources(
        [{"source_label": "Source A", "research_candidates": candidates}],
        limit=None,
    )

    assert len(default_ranked) == 75
    assert len(unlimited_ranked) == 75
    assert default_ranked[-1]["rank"] == 75
    assert unlimited_ranked[-1]["rank"] == 75


def test_private_brigittenau_showcase_does_not_inject_when_bundle_is_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PRIVATE_SHOWCASE_ALLOWED_EMAILS", "property-showcase-owner@example.test")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("PROPERTYQUARRY_PRIVATE_SHOWCASE_FLOOR", "6")
    monkeypatch.setenv("PROPERTYQUARRY_PRIVATE_SHOWCASE_HAS_LIFT", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PRIVATE_SHOWCASE_HAS_BALCONY", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PRIVATE_SHOWCASE_HAS_TERRACE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PRIVATE_SHOWCASE_TITLE", "Private zero-cost flat")
    monkeypatch.setenv("PROPERTYQUARRY_PRIVATE_SHOWCASE_SUMMARY", "Private showcase flat.")
    monkeypatch.setenv("PROPERTYQUARRY_PRIVATE_SHOWCASE_FIT_SUMMARY", "Private match")
    snapshot = product_service._property_search_snapshot_with_private_showcase(  # type: ignore[attr-defined]
        {
            "run_id": "run-private-20",
            "property_search_preferences": {"country_code": "AT", "location_query": "1200 Vienna"},
            "summary": {
                "sources": [
                    {
                        "source_label": "Willhaben",
                        "top_candidates": [
                            {
                                "candidate_ref": "public-hit",
                                "source_ref": "provider:public-hit",
                                "title": "Normal Brigittenau hit",
                                "property_url": "https://example.test/hit",
                                "fit_score": 95,
                                "ranking_score": 95,
                                "property_facts": {"postal_name": "1200 Wien"},
                            }
                        ],
                    }
                ],
            },
        },
        principal_id="cf-email:property-showcase-owner@example.test",
    )

    summary = dict(snapshot["summary"])
    assert summary.get("private_showcase_candidate_ref") is None
    assert summary.get("private_showcase_status") is None
    assert [row["candidate_ref"] for row in summary["sources"][0]["top_candidates"]] == ["public-hit"]
    serialized = json.dumps(snapshot, sort_keys=True)
    assert "private-showcase-flat" not in serialized
    assert "generated-walkthrough.mp4" not in serialized
    assert "source-floorplan.jpg" not in serialized


def test_private_brigittenau_showcase_injects_verified_first_party_tour(monkeypatch, tmp_path: Path) -> None:
    from scripts.property_tour_3dvista_provenance import (
        THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
        export_tree_sha256,
    )

    principal_id = "cf-email:property-showcase-owner@example.test"
    slug = product_service._PROPERTY_PRIVATE_SHOWCASE_TOUR_SLUG  # type: ignore[attr-defined]
    bundle_dir = tmp_path / slug
    export_dir = bundle_dir / "3dvista"
    export_dir.mkdir(parents=True)
    (export_dir / "index.htm").write_text(
        "<!doctype html><html><body><div id='tour-viewer'>3D tour ready</div>"
        "<script>window.TDVPlayer = { ready: true };</script></body></html>",
        encoding="utf-8",
    )
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "three_d_vista_target_provenance": {
                    "schema": THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
                    "status": "pass",
                    "provider": "3dvista",
                    "target_slug": slug,
                    "artifact": {
                        "kind": "local_export",
                        "sha256": export_tree_sha256(export_dir),
                        "entry_relpath": "index.htm",
                    },
                    "authorization": {
                        "status": "approved",
                        "reference": f"fixture-authorization:{slug}",
                    },
                    "review": {
                        "property_match": "pass",
                        "visual_match": "pass",
                        "reviewed_by": "propertyquarry-test-reviewer",
                        "reviewed_at": "2026-07-18T00:00:00+00:00",
                    },
                    "target_subdir": "3dvista",
                },
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "principal_id": principal_id,
                "three_d_vista_entry_relpath": "3dvista/index.htm",
                "three_d_vista_import": {"source_project": "propertyquarry"},
                "three_d_vista_white_label_proof": {
                    "source_project": "propertyquarry",
                    "private_viewer_verified": True,
                    "non_trial_export_verified": True,
                    "propertyquarry_tour_metadata": True,
                    "trial_branding_checked": True,
                    "trial_branding_present": False,
                },
                "three_d_vista_browser_render_proof": {
                    "provider": "3dvista",
                    "status": "pass",
                    "rendered_viewer": True,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROPERTYQUARRY_PRIVATE_SHOWCASE_ALLOWED_EMAILS", "property-showcase-owner@example.test")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    snapshot = product_service._property_search_snapshot_with_private_showcase(  # type: ignore[attr-defined]
        {
            "run_id": "run-private-20",
            "property_search_preferences": {"country_code": "AT", "location_query": "1200 Vienna"},
            "summary": {
                "sources": [
                    {
                        "source_label": "Willhaben",
                        "top_candidates": [
                            {
                                "candidate_ref": "public-hit",
                                "source_ref": "provider:public-hit",
                                "title": "Normal Brigittenau hit",
                                "property_url": "https://example.test/hit",
                                "fit_score": 95,
                                "ranking_score": 95,
                                "property_facts": {"postal_name": "1200 Wien"},
                            }
                        ],
                    }
                ],
            },
        },
        principal_id=principal_id,
    )

    summary = dict(snapshot["summary"])
    ranked = [dict(row) for row in list(summary["ranked_candidates"])]
    showcase = ranked[0]
    facts = dict(showcase["property_facts"])

    assert showcase["candidate_ref"] == product_service._PROPERTY_PRIVATE_SHOWCASE_CANDIDATE_REF  # type: ignore[attr-defined]
    assert showcase["tour_url"] == f"/tours/{slug}/control/3dvista"
    assert "://" not in showcase["tour_url"]
    assert showcase["tour_status"] == "ready"
    assert showcase["flythrough_url"] == ""
    assert showcase["flythrough_status"] == "unavailable"
    assert facts["has_floorplan"] is False
    assert facts["floorplan_urls_json"] == []
    assert ranked[1]["candidate_ref"] == "public-hit"


def test_private_brigittenau_showcase_does_not_inject_for_other_users() -> None:
    snapshot = product_service._property_search_snapshot_with_private_showcase(  # type: ignore[attr-defined]
        {
            "run_id": "run-private-20",
            "property_search_preferences": {"country_code": "AT", "location_query": "Brigittenau"},
            "summary": {"sources": []},
        },
        principal_id="cf-email:viewer@example.test",
    )

    summary = dict(snapshot["summary"])

    assert summary.get("private_showcase_candidate_ref") is None
    assert list(summary.get("sources") or []) == []


def test_private_brigittenau_showcase_does_not_treat_budget_1200_as_district(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PRIVATE_SHOWCASE_ALLOWED_EMAILS", "property-showcase-owner@example.test")
    snapshot = product_service._property_search_snapshot_with_private_showcase(  # type: ignore[attr-defined]
        {
            "run_id": "run-private-budget",
            "property_search_preferences": {
                "country_code": "AT",
                "location_query": "1020 Vienna",
                "max_price_eur": 1200,
            },
            "summary": {"sources": []},
        },
        principal_id="cf-email:property-showcase-owner@example.test",
    )

    summary = dict(snapshot["summary"])

    assert summary.get("private_showcase_candidate_ref") is None
    assert list(summary.get("sources") or []) == []


def test_ranked_candidates_exclude_false_positive_and_repair_only_rows() -> None:
    ranked = product_service._property_search_ranked_candidates_from_sources(
        [
            {
                "source_label": "Source A",
                "top_candidates": [
                    {"source_ref": "good", "fit_score": 92, "title": "Real ranked home"},
                    {"source_ref": "maybe", "fit_score": 99, "title": "Maybe false", "maybe_false": True},
                    {"source_ref": "repair", "fit_score": 98, "title": "Repair only", "flagged_for_repair": True},
                    {"source_ref": "filtered", "fit_score": 97, "title": "Hard filtered", "hard_filter_reason": "area_mismatch"},
                    {"source_ref": "status", "fit_score": 96, "title": "Status false positive", "candidate_status": "false_positive"},
                ],
            }
        ]
    )

    assert [row["source_ref"] for row in ranked] == ["good"]


def test_ranked_candidates_keep_soft_filter_reasons_but_exclude_hard_reasons() -> None:
    ranked = product_service._property_search_ranked_candidates_from_sources(
        [
            {
                "source_label": "Source A",
                "top_candidates": [
                    {
                        "source_ref": "soft",
                        "fit_score": 66,
                        "ranking_score": 66,
                        "title": "Soft preference miss",
                        "filter_reason": "playground_too_far_score_only",
                    },
                    {
                        "source_ref": "hard",
                        "fit_score": 98,
                        "ranking_score": 98,
                        "title": "Outside area",
                        "filter_reason": "outside_selected_area",
                    },
                    {
                        "source_ref": "hard-explicit",
                        "fit_score": 97,
                        "ranking_score": 97,
                        "title": "Hard filtered",
                        "hard_filter_reason": "area_mismatch",
                    },
                ],
            }
        ]
    )

    assert [row["source_ref"] for row in ranked] == ["soft"]
    assert ranked[0]["rank"] == 1


def test_ranked_candidates_exclude_unmarked_postal_conflicts_from_exact_source_scope() -> None:
    ranked = product_service._property_search_ranked_candidates_from_sources(
        [
            {
                "source_label": "Willhaben | Austria | Rent | 1010 Vienna",
                "source_scope_label": "Willhaben | Austria | Rent | 1010 Vienna",
                "source_url": "https://www.willhaben.at/iad/immobilien/mietwohnungen?q=1010+Vienna",
                "top_candidates": [
                    {
                        "source_ref": "outside-scope",
                        "fit_score": 98,
                        "ranking_score": 98,
                        "title": "Terrassenwohnung auf der Hohen Warte, 66 m2, EUR 1.599, (1190 Wien)",
                        "property_facts": {
                            "postal_name": "1190 Wien",
                            "source_scope_location": "1010 Vienna",
                            "listing_postal_evidence": [
                                {"postal_code": "1190", "postal_name": "1190 Wien"},
                            ],
                        },
                    },
                    {
                        "source_ref": "inside-scope",
                        "fit_score": 77,
                        "ranking_score": 77,
                        "title": "Wohnung mieten in 1010 Wien | 70 m2 | 2 Zimmer",
                        "property_facts": {
                            "postal_name": "1010 Wien",
                            "source_scope_location": "1010 Vienna",
                            "listing_postal_evidence": [
                                {"postal_code": "1010", "postal_name": "1010 Wien"},
                            ],
                        },
                    },
                ],
            }
        ]
    )

    assert [row["source_ref"] for row in ranked] == ["inside-scope"]
    assert ranked[0]["rank"] == 1


def test_ranked_candidates_merge_top_and_research_rows_and_keep_stable_candidate_ref() -> None:
    ranked = product_service._property_search_ranked_candidates_from_sources(
        [
            {
                "source_label": "Willhaben",
                "top_candidates": [
                    {
                        "source_ref": "property-scout:1900851485",
                        "property_url": "https://example.test/listing/1900851485",
                        "review_url": "https://propertyquarry.test/app/research/packet-1",
                        "title": "Luxury Residence",
                        "fit_score": 53,
                        "tour_url": "https://propertyquarry.test/tours/luxury-residence",
                        "tour_status": "created",
                    }
                ],
                "research_candidates": [
                    {
                        "source_ref": "property-scout:1900851485",
                        "property_url": "https://example.test/listing/1900851485",
                        "review_url": "https://propertyquarry.test/app/research/packet-1",
                        "title": "Luxury Residence",
                        "fit_score": 53,
                        "flythrough_url": "https://propertyquarry.test/tours/files/luxury-residence/video.mp4",
                        "flythrough_status": "rendered",
                    }
                ],
            }
        ]
    )

    assert len(ranked) == 1
    assert ranked[0]["source_ref"] == "property-scout:1900851485"
    assert ranked[0]["tour_url"] == "https://propertyquarry.test/tours/luxury-residence"
    assert ranked[0]["flythrough_url"] == "https://propertyquarry.test/tours/files/luxury-residence/video.mp4"
    assert ranked[0]["candidate_ref"]


def test_property_scout_notification_source_hides_search_scope_metadata() -> None:
    text = product_service._property_alert_review_telegram_text(
        title="Wohnung mieten in 1220 Wien | 60 m² | 2 Zimmer | EUR 1.090",
        summary="2-Zimmer Wohnung mit Traumblick in 1220 Wien.",
        counterparty="DER STANDARD Immobilien | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://immobilien.derstandard.at/detail/wohnung-mieten-in-1220-wien",
        personal_fit_assessment={"fit_score": 54.0, "recommendation": "ask_for_clarification"},
    )

    assert "Source: DER STANDARD Immobilien" in text
    assert "Source: DER STANDARD Immobilien | Austria | Rent | 1010 Vienna" not in text

    willhaben = product_service._property_alert_review_telegram_text(
        title="#W2 Moderne Schöne Zwei-Zimmer Wohnung mit großem Ess- & Wohnbereich",
        summary="Wählen Sie aus 113.217 Angeboten. Immobilien suchen und finden auf willhaben.",
        counterparty="Willhaben | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/demo-1631373932/",
        personal_fit_assessment={"fit_score": 54.0, "recommendation": "ask_for_clarification"},
    )

    assert "Source: Willhaben" in willhaben
    assert "1010 Vienna" not in willhaben
    assert "Austria | Rent" not in willhaben

    willhaben_german_scope = product_service._property_alert_review_telegram_text(
        title="#W2 Moderne Schöne Zwei-Zimmer Wohnung mit Terrasse",
        summary="Penthouse-Charakter in Stadt Salzburg.",
        counterparty="Willhaben | AT | Miete | 1010 Wien, Innere Stadt",
        account_email="",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/demo-1631373932/",
        personal_fit_assessment={"fit_score": 54.0, "recommendation": "ask_for_clarification"},
    )

    assert "Source: Willhaben" in willhaben_german_scope
    assert "1010" not in willhaben_german_scope
    assert "Innere Stadt" not in willhaben_german_scope
    assert "Miete" not in willhaben_german_scope

    cooperative = product_service._property_alert_review_telegram_text(
        title="Geförderte Wohnung",
        summary="Review candidate.",
        counterparty="Genossenschaften | Austria | Rent | 1010 Vienna | GESIBA Wohnungen",
        account_email="",
        property_url="https://example.test/listing",
        personal_fit_assessment={"fit_score": 64.0, "recommendation": "mention"},
    )

    assert "Source: Genossenschaften · GESIBA Wohnungen" in cooperative
    assert "1010 Vienna" not in cooperative


def test_property_requested_location_match_keeps_title_postal_match_even_when_scope_shares_same_postal() -> None:
    assert _property_candidate_matches_requested_location(
        location_hints=("8055 Graz",),
        property_url="https://www.willhaben.at/example",
        title="Erstbezugswohnung, 47,57 m², € 613,49, (8055 Graz) - willhaben",
        summary="Modern rental apartment in Graz.",
        property_facts={
            "postal_name": "8055 Graz",
            "source_scope_location": "8055 Graz",
            "source_postal_code": "8055",
            "country_code": "AT",
        },
        country_code="AT",
        region_code="steiermark",
    )


def test_property_search_analysis_cap_expands_for_exact_scope() -> None:
    assert product_service._property_search_analysis_cap_per_source(
        max_results=5,
        candidate_total=40,
        exact_scope=False,
    ) == 12
    assert product_service._property_search_analysis_cap_per_source(
        max_results=5,
        candidate_total=40,
        exact_scope=True,
    ) == 30
    assert product_service._property_search_analysis_cap_per_source(
        max_results=5,
        candidate_total=40,
        exact_scope=True,
        focused_scope=True,
    ) == 40
    assert product_service._property_search_has_exact_scope(
        request_preferences={"selected_districts": []},
        location_hints=("1210 Wien",),
    )
    assert not product_service._property_search_has_exact_scope(
        request_preferences={"selected_districts": []},
        location_hints=("Wien",),
    )


def test_property_search_location_matching_treats_wien_as_broad_vienna_scope() -> None:
    assert _property_candidate_matches_requested_location(
        location_hints=("Vienna",),
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/demo",
        title="Wohnung mieten in 1020 Wien | 74 m² | 3 Zimmer",
        summary="Wohnung im 2. Bezirk.",
        property_facts={"postal_name": "1020 Wien"},
        country_code="AT",
        region_code="vienna",
    )
    assert not _property_candidate_matches_requested_location(
        location_hints=("1010 Vienna",),
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/demo",
        title="Wohnung mieten in 1020 Wien | 74 m² | 3 Zimmer",
        summary="Wohnung im 2. Bezirk.",
        property_facts={"postal_name": "1020 Wien"},
        country_code="AT",
        region_code="vienna",
    )


def test_investment_underwriting_payload_exposes_dimensions_and_confidence() -> None:
    payload = _property_investment_underwriting_payload(
        title="Apartment near U-Bahn",
        summary="Clean apartment with floorplan and stable tenant demand.",
        facts={
            "has_floorplan": True,
            "tenant_status": "vacant",
            "energy_certificate_present": True,
            "map_lat": 48.21,
            "map_lng": 16.38,
            "nearest_subway_m": 240,
            "nearest_supermarket_m": 180,
            "nearest_medical_care_m": 700,
            "source_trust_tier": "high",
            "source_access_level": "direct",
            "future_change_research": {
                "planning_confidence": "high",
                "investment_impact": "positive tailwind",
                "future_value_drivers": ["subway upgrade", "office employment growth"],
            },
            "official_risk_evidence": {
                "sources": [{"risk_key": "flood_risk", "verification_state": "needs_review"}],
            },
        },
        preferences={
            "search_goal": "investment",
            "investment_strategy": "best_overall",
            "min_gross_yield_pct": 4,
        },
        snapshot={
            "gross_yield_pct": 4.8,
            "market_buy_delta_pct": -7.5,
            "expected_monthly_rent_eur": 1450.0,
            "payback_years": 20.3,
            "current_price_eur": 362000.0,
            "current_area_sqm": 67.0,
            "current_price_per_sqm_eur": 5400.0,
            "market_buy_per_sqm_eur": 5838.0,
            "market_rent_per_sqm_eur": 18.1,
            "buy_sample_count": 5,
            "rent_sample_count": 4,
        },
    )

    assert payload["score"] > 0
    assert payload["confidence_label"] in {"High confidence", "Partial evidence"}
    assert payload["gross_yield_display"] == "4.8% gross yield"
    assert payload["market_delta_display"] == "7.5% below local buy median"
    assert payload["score_display"].endswith("institutional score")
    assert len(payload["dimensions"]) == 7
    assert {row["key"] for row in payload["dimensions"]} == {"return", "value", "demand", "liquidity", "risk", "execution", "evidence"}
    assert payload["net_yield_display"]
    assert payload["cap_rate_display"]


def test_investment_external_snapshot_falls_back_honestly_without_live_feeds(monkeypatch) -> None:
    monkeypatch.setattr(property_investment_external_data, "_fetch_external_feed", lambda prefix, request_payload: {})
    snapshot = property_investment_external_data.property_investment_external_snapshot(
        country_code="AT",
        property_url="https://example.test/listing/1",
        title="Fallback investment case",
        facts={"area_m2": 80, "operating_costs_monthly": 260, "map_lat": 48.2, "map_lng": 16.38},
        preferences={"equity_available_eur": 140000, "loan_term_years": 25, "vacancy_reserve_pct": 5, "capex_reserve_pct": 6},
        snapshot={
            "current_price_eur": 420000,
            "current_area_sqm": 80,
            "expected_monthly_rent_eur": 1450,
            "expected_annual_rent_eur": 17400,
        },
    )

    assert snapshot["feed_status_label"] == "Fallback underwriting model"
    assert snapshot["confidence_label"] == "Fallback assumptions"
    assert snapshot["rent_roll"]["source_mode"] == "comp_fallback"
    assert snapshot["operating_costs"]["source_mode"] in {"listing_fact", "assumption"}
    assert snapshot["taxes"]["source_mode"] == "country_default"
    assert snapshot["financing"]["source_mode"] == "assumption"
    assert snapshot["net_yield_pct"] is not None
    assert snapshot["cap_rate_pct"] is not None


def test_investment_external_feed_rejects_insecure_http_without_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_PROPERTY_RENT_ROLL_FEED_URL", "http://example.test/feed")

    def _unexpected_urlopen(*args, **kwargs):
        raise AssertionError("urlopen should not run for insecure feed URLs")

    monkeypatch.setattr(property_investment_external_data.urllib.request, "urlopen", _unexpected_urlopen)
    payload = property_investment_external_data._fetch_external_feed(
        "EA_PROPERTY_RENT_ROLL_FEED",
        {"country_code": "AT", "purchase_price_eur": 300000},
    )

    assert payload == {}


def test_investment_external_feed_rejects_https_host_without_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_PROPERTY_RENT_ROLL_FEED_URL", "https://example.test/feed")
    monkeypatch.delenv("EA_PROPERTY_INVESTMENT_EXTERNAL_ALLOWED_HOSTS", raising=False)

    def _unexpected_urlopen(*args, **kwargs):
        raise AssertionError("urlopen should not run for non-allowlisted https feed URLs")

    monkeypatch.setattr(property_investment_external_data.urllib.request, "urlopen", _unexpected_urlopen)
    payload = property_investment_external_data._fetch_external_feed(
        "EA_PROPERTY_RENT_ROLL_FEED",
        {"country_code": "AT", "purchase_price_eur": 300000},
    )

    assert payload == {}


def test_investment_external_feed_allows_only_exact_or_wildcard_subdomains(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_PROPERTY_RENT_ROLL_FEED_URL", "https://rent.data.example.test/feed")
    monkeypatch.setenv("EA_PROPERTY_INVESTMENT_EXTERNAL_ALLOWED_HOSTS", "example.test, *.data.example.test")
    monkeypatch.setenv("EA_PROPERTY_INVESTMENT_EXTERNAL_CACHE_PATH", str(tmp_path / "investment_cache.json"))
    monkeypatch.setenv("EA_PROPERTY_INVESTMENT_EXTERNAL_CACHE_TTL_SECONDS", "60")
    calls: list[str] = []

    class _Response:
        def __init__(self) -> None:
            self.headers = {"Content-Type": "application/json"}
            self._sent = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size: int = -1) -> bytes:
            if self._sent:
                return b""
            self._sent = True
            return b'{"annual_rent_eur": 18000, "source_label": "Approved data feed"}'

    def _urlopen(request, *args, **kwargs):
        calls.append(str(request.full_url))
        return _Response()

    monkeypatch.setattr(property_investment_external_data.urllib.request, "urlopen", _urlopen)

    payload = property_investment_external_data._fetch_external_feed(
        "EA_PROPERTY_RENT_ROLL_FEED",
        {"country_code": "AT", "purchase_price_eur": 300000},
    )

    assert calls
    assert payload["annual_rent_eur"] == 18000
    assert payload["source_mode"] == "live_feed"


def test_investment_external_feed_wildcard_does_not_match_apex_or_lookalike_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_PROPERTY_INVESTMENT_EXTERNAL_ALLOWED_HOSTS", "*.data.example.test")

    def _unexpected_urlopen(*args, **kwargs):
        raise AssertionError("urlopen should not run for non-matching wildcard feed hosts")

    monkeypatch.setattr(property_investment_external_data.urllib.request, "urlopen", _unexpected_urlopen)

    for url in (
        "https://data.example.test/feed",
        "https://evil-data.example.test/feed",
        "https://data.example.test.evil.test/feed",
    ):
        monkeypatch.setenv("EA_PROPERTY_RENT_ROLL_FEED_URL", url)
        payload = property_investment_external_data._fetch_external_feed(
            "EA_PROPERTY_RENT_ROLL_FEED",
            {"country_code": "AT", "purchase_price_eur": 300000, "url": url},
        )
        assert payload == {}


def test_investment_external_feed_rejects_oversized_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_PROPERTY_RENT_ROLL_FEED_URL", "https://example.test/feed")
    monkeypatch.setenv("EA_PROPERTY_INVESTMENT_EXTERNAL_ALLOWED_HOSTS", "example.test")
    monkeypatch.setenv("EA_PROPERTY_INVESTMENT_EXTERNAL_MAX_RESPONSE_BYTES", "64")
    monkeypatch.setenv("EA_PROPERTY_INVESTMENT_EXTERNAL_CACHE_PATH", str(tmp_path / "investment_cache.json"))

    class _Response:
        def __init__(self) -> None:
            self.headers = {"Content-Type": "application/json", "Content-Length": "512"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size: int = -1) -> bytes:
            return b'{"annual_rent_eur": 18000}'

    monkeypatch.setattr(property_investment_external_data.urllib.request, "urlopen", lambda *args, **kwargs: _Response())
    payload = property_investment_external_data._fetch_external_feed(
        "EA_PROPERTY_RENT_ROLL_FEED",
        {"country_code": "AT", "purchase_price_eur": 300000},
    )

    assert payload == {}


def test_investment_external_feed_rejects_streamed_oversized_response_without_content_length(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_PROPERTY_RENT_ROLL_FEED_URL", "https://example.test/feed")
    monkeypatch.setenv("EA_PROPERTY_INVESTMENT_EXTERNAL_ALLOWED_HOSTS", "example.test")
    monkeypatch.setenv("EA_PROPERTY_INVESTMENT_EXTERNAL_MAX_RESPONSE_BYTES", "64")
    monkeypatch.setenv("EA_PROPERTY_INVESTMENT_EXTERNAL_CACHE_PATH", str(tmp_path / "investment_cache.json"))

    class _Response:
        def __init__(self) -> None:
            self.headers = {"Content-Type": "application/json"}
            self._body = b'{"annual_rent_eur": 18000, "source_label": "' + (b"a" * 5000) + b'"}'
            self._offset = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size: int = -1) -> bytes:
            if self._offset >= len(self._body):
                return b""
            if size is None or size < 0:
                size = len(self._body) - self._offset
            chunk = self._body[self._offset : self._offset + size]
            self._offset += len(chunk)
            return chunk

    monkeypatch.setattr(property_investment_external_data.urllib.request, "urlopen", lambda *args, **kwargs: _Response())
    payload = property_investment_external_data._fetch_external_feed(
        "EA_PROPERTY_RENT_ROLL_FEED",
        {"country_code": "AT", "purchase_price_eur": 300000},
    )

    assert payload == {}


def test_investment_external_cache_path_defaults_to_durable_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EA_PROPERTY_INVESTMENT_EXTERNAL_CACHE_PATH", raising=False)

    assert property_investment_external_data._cache_path() == Path("/docker/property/state/property_investment_external_cache.json")


def test_investment_external_cache_file_is_private(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache_path = tmp_path / "investment_cache.json"
    monkeypatch.setenv("EA_PROPERTY_INVESTMENT_EXTERNAL_CACHE_PATH", str(cache_path))

    property_investment_external_data._put_cached_feed_payload(
        lane="EA_PROPERTY_RENT_ROLL_FEED",
        request_payload={"country_code": "AT"},
        data={"annual_rent_eur": 18000},
    )

    assert cache_path.exists()
    assert oct(cache_path.stat().st_mode & 0o777) == "0o600"


def test_findmyhome_entry_links_are_not_treated_as_supported_property_listings() -> None:
    assert not product_service._property_scout_is_supported_listing_url(
        "https://www.findmyhome.at/immo/wohnung-kaufen/wien?id=13&entry=20&sort=&dir=ASC&pp=20&vars=id%3A13%3Bw_e%3A1%3Bland%3AAT%3Bbl%3A9%3B&lang=de&module=select&list="
    )


def test_findmyhome_search_page_is_not_treated_as_property_listing() -> None:
    assert not product_service._property_scout_is_supported_listing_url(
        "https://www.findmyhome.at/immo/wohnung-kaufen/wien"
    )


def test_findmyhome_search_state_urls_stay_unsupported_after_sanitization() -> None:
    assert not product_service._property_scout_is_supported_listing_url(
        "https://findmyhome.at/immo/wohnung-kaufen/wien?id=14&entry=10&sort=sort_fl&dir=ASC&pp=10&vars=&lang=&module=&list='/'"
    )


def test_findmyhome_short_detail_url_is_treated_as_supported_listing() -> None:
    assert product_service._property_scout_is_supported_listing_url(
        "https://www.findmyhome.at/5620769?tl=1"
    )


def test_findmyhome_result_cards_extract_short_detail_urls() -> None:
    html = '''
    <div class="row margin-top-20">
      <div class="col-xs-12 col-sm-9 col-md-9 col-lg-9">
        <h3 class="obj_list">
          <strong><span style="color:#c30a32">TOP: </span></strong>
          <a href='/5620769?tl=1' class='btnHeadlineErgebnisliste'>Helle 2-Zimmer Wohnung, Nähe Meiselmarkt</a>
        </h3>
      </div>
    </div>
    '''

    urls = product_service._property_scout_extract_listing_urls(
        source_url="https://www.findmyhome.at/immo/wohnung-kaufen/wien",
        html=html,
        source_spec={"provider_filter_pushdown": {"requested": {}, "applied": {}}},
    )

    assert urls == ("https://www.findmyhome.at/5620769?tl=1",)


def test_free_property_plan_uses_declared_visual_generation_caps() -> None:
    snapshot = property_commercial_snapshot({})

    assert snapshot["magic_fit_scene_limit"] == 1
    assert snapshot["magic_fit_video_limit"] == 1
    assert snapshot["magic_fit_scene_period"] == "week"
    assert snapshot["magic_fit_video_period"] == "day"
    free_plan = next(plan for plan in snapshot["plan_catalog"] if plan["plan_key"] == "free")
    assert "one 3D reconstruction floor plan per week and one interior flythrough per day" in free_plan["features"]


class _QuotaRow:
    def __init__(self, *, event_type: str, created_at: str, channel: str = "product") -> None:
        self.channel = channel
        self.event_type = event_type
        self.created_at = created_at


class _QuotaRuntime:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def list_recent_observations(self, limit: int = 4000, principal_id: str = "") -> list[object]:
        return list(self._rows)[:limit]


class _QuotaOnboarding:
    def __init__(self, preferences: dict[str, object]) -> None:
        self._preferences = preferences

    def status(self, principal_id: str = "") -> dict[str, object]:
        return {"property_search_preferences": dict(self._preferences)}


class _QuotaContainer:
    def __init__(self, preferences: dict[str, object], rows: list[object]) -> None:
        self.onboarding = _QuotaOnboarding(preferences)
        self.channel_runtime = _QuotaRuntime(rows)


class _PreviewCacheRuntime:
    def __init__(self) -> None:
        self.rows: list[object] = []

    def ingest_observation(
        self,
        principal_id: str,
        channel: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        source_id: str = "",
        dedupe_key: str = "",
        **_kwargs,
    ) -> object:
        row = SimpleNamespace(
            principal_id=principal_id,
            channel=channel,
            event_type=event_type,
            payload=dict(payload or {}),
            source_id=source_id,
            dedupe_key=dedupe_key,
            created_at=datetime.now(timezone.utc).isoformat(),
            observation_id=str(uuid.uuid4()),
        )
        self.rows.insert(0, row)
        return row

    def list_recent_observations(self, limit: int = 4000, principal_id: str = "") -> list[object]:
        rows = [
            row
            for row in self.rows
            if not principal_id or str(getattr(row, "principal_id", "") or "").strip() == str(principal_id or "").strip()
        ]
        return rows[:limit]


class _PreviewCacheContainer:
    def __init__(self) -> None:
        self.channel_runtime = _PreviewCacheRuntime()


def test_property_visual_quota_enforces_free_daily_magic_fit_limit() -> None:
    service = ProductService.__new__(ProductService)
    service._container = _QuotaContainer(
        {},
        [
            _QuotaRow(
                event_type="property_magic_fit_scene_created",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        ],
    )

    with pytest.raises(ValueError, match="property_magic_fit_upgrade_required:plus"):
        service._enforce_property_visual_quota(
            principal_id="cf-email:quota-free@example.test",
            property_preferences={},
            quota_kind="scene",
        )


def test_property_visual_quota_enforces_plus_daily_video_limit() -> None:
    service = ProductService.__new__(ProductService)
    service._container = _QuotaContainer(
        {"property_commercial": {"active_plan_key": "plus", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"}},
        [
            _QuotaRow(event_type="generic_property_tour_created", created_at=datetime.now(timezone.utc).isoformat()),
            _QuotaRow(event_type="willhaben_property_tour_created", created_at=datetime.now(timezone.utc).isoformat()),
            _QuotaRow(event_type="generic_property_tour_created", created_at=datetime.now(timezone.utc).isoformat()),
        ],
    )

    with pytest.raises(ValueError, match="property_tour_upgrade_required:agent"):
        service._enforce_property_visual_quota(
            principal_id="cf-email:quota-plus@example.test",
            property_preferences={"property_commercial": {"active_plan_key": "plus", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"}},
            quota_kind="video",
        )


def test_property_preview_timeout_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    started = {"value": False}

    def _slow_preview(property_url: str, prefer_fast: bool = False) -> dict[str, object]:
        started["value"] = True
        time.sleep(0.2)
        return {"property_url": property_url, "property_facts_json": {}}

    monkeypatch.setattr(product_service, "_property_scout_page_preview", _slow_preview)
    monkeypatch.setattr(product_service, "_property_search_preview_timeout_seconds", lambda *, prefer_fast: 0.05)

    with pytest.raises(TimeoutError, match="property_preview_timeout:fast"):
        product_service._property_scout_page_preview_with_timeout("https://example.com/listing", prefer_fast=True)

    assert started["value"] is True


def test_floorplan_recovery_workers_store_recovered_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    service = ProductService.__new__(ProductService)
    stored: dict[str, dict[str, object]] = {}

    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_FLOORPLAN_RECOVERY_LIMIT", "4")
    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_PREVIEW_TIMEOUT_SECONDS", "1")

    def _fake_preview(property_url: str, prefer_fast: bool = False) -> dict[str, object]:
        return {
            "property_url": property_url,
            "title": "Recovered floorplan listing",
            "summary": "Floorplan PDF found",
            "property_facts_json": {"floorplan_urls_json": [f"{property_url}/floorplan.pdf"]},
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)
    monkeypatch.setattr(
        service,
        "_property_public_preview_cache_store",
        lambda *, cache_index, property_url, preview: stored.setdefault(property_url, dict(preview)),
    )

    recovered = service._recover_floorplans_for_candidates(
        candidates=[
            {"property_url": "https://example.com/listing-1"},
            {"property_url": "https://example.com/listing-2"},
        ],
        cache_index={},
        plan_key="plus",
    )

    assert set(recovered) == {"https://example.com/listing-1", "https://example.com/listing-2"}
    assert set(stored) == {"https://example.com/listing-1", "https://example.com/listing-2"}


def test_propertyquarry_public_urls_do_not_inherit_external_brain_defaults(monkeypatch) -> None:
    monkeypatch.delenv("PROPERTYQUARRY_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_PUBLIC_APP_BASE_URL", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_PUBLIC_TOUR_BASE_URL", raising=False)
    monkeypatch.delenv("EA_PUBLIC_APP_BASE_URL", raising=False)
    monkeypatch.delenv("EA_PUBLIC_TOUR_BASE_URL", raising=False)

    assert product_service._public_app_base_url() == "https://propertyquarry.com"

    monkeypatch.setenv("EA_PUBLIC_APP_BASE_URL", "https://myexternalbrain.com")
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://myexternalbrain.com/tours")

    assert product_service._property_public_app_base_url() == "https://propertyquarry.com"
    assert product_service._property_public_tour_base_url() == "https://propertyquarry.com/tours"

    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_APP_BASE_URL", "https://app.propertyquarry.test")

    assert product_service._property_public_app_base_url() == "https://app.propertyquarry.test"


def test_property_public_preview_cache_reuses_sanitized_public_facts() -> None:
    service = ProductService.__new__(ProductService)
    service._container = _PreviewCacheContainer()
    cache_index: dict[str, dict[str, object]] = {}
    stored = service._property_public_preview_cache_store(
        cache_index=cache_index,
        property_url="https://example.test/listing/1",
        preview={
            "property_url": "https://example.test/listing/1",
            "listing_id": "listing-1",
            "title": "Quiet courtyard flat",
            "summary": "Useful public preview facts.",
            "property_facts_json": {
                "provider_channel": "findmyhome_at",
                "postal_name": "1200 Wien",
                "rooms": 3,
                "has_floorplan": True,
                "exact_address": "Hidden 1",
                "lat": 48.2,
                "map_lat": 48.2082,
                "map_lng": 16.3738,
                "map_coordinate_evidence": {
                    "latitude": 48.2082,
                    "longitude": 16.3738,
                    "coordinate_digest": "sha256:private",
                },
                "nearest_supermarket_m": 220,
                "nearest_supermarket_name": "Private nearby shop",
                "property_fact_evidence": {
                    "nearest_supermarket_m": {
                        "listing_latitude": 48.2082,
                        "listing_longitude": 16.3738,
                        "provider_attestation": "hmac-sha256:private",
                    }
                },
                "listing_research_snapshot": {
                    "map_lat": 48.2082,
                    "map_lng": 16.3738,
                    "nearest_supermarket_m": 220,
                    "property_fact_evidence": {
                        "nearest_supermarket_m": {
                            "listing_latitude": 48.2082,
                            "listing_longitude": 16.3738,
                            "provider_attestation": "hmac-sha256:nested-private",
                        }
                    },
                },
                "address_lines": ["Hidden Street 1", "1200 Wien"],
                "serialized_fragment": (
                    '{"map_lat":48.2082,"map_lng":16.3738,'
                    '"provider_attestation":"hmac-sha256:serialized-private"}'
                ),
                "official_risk_evidence": {
                    "source": {"lat": 48.2082, "lon": 16.3738}
                },
                "public_nested_summary": {
                    "rooms": 3,
                    "map_lat": 48.2082,
                    "listing_longitude": 16.3738,
                    "cookie_debug": "nested-nope",
                },
                "cookie_debug": "nope",
            },
            "floorplan_urls_json": ["https://cdn.example.test/floorplan.png"],
        },
    )

    assert stored["property_facts_json"]["provider_channel"] == "findmyhome_at"
    assert "exact_address" not in stored["property_facts_json"]
    assert "lat" not in stored["property_facts_json"]
    assert "map_lat" not in stored["property_facts_json"]
    assert "map_lng" not in stored["property_facts_json"]
    assert "map_coordinate_evidence" not in stored["property_facts_json"]
    assert "nearest_supermarket_m" not in stored["property_facts_json"]
    assert "nearest_supermarket_name" not in stored["property_facts_json"]
    assert "property_fact_evidence" not in stored["property_facts_json"]
    assert "listing_research_snapshot" not in stored["property_facts_json"]
    assert "public_nested_summary" not in stored["property_facts_json"]
    assert "address_lines" not in stored["property_facts_json"]
    assert "serialized_fragment" not in stored["property_facts_json"]
    assert "official_risk_evidence" not in stored["property_facts_json"]
    assert "cookie_debug" not in stored["property_facts_json"]
    serialized_public_cache = json.dumps(
        stored["property_facts_json"],
        sort_keys=True,
    )
    for forbidden_fragment in (
        "map_lat",
        "map_lng",
        "nearest_supermarket_m",
        "listing_latitude",
        "listing_longitude",
        "provider_attestation",
        "hmac-sha256:",
        "Hidden Street 1",
        "listing_longitude",
    ):
        assert forbidden_fragment not in serialized_public_cache

    indexed = service._property_public_preview_cache_index()
    loaded = service._property_public_preview_cache_lookup(
        cache_index=indexed,
        property_url="https://example.test/listing/1",
    )

    assert loaded is not None
    assert loaded["title"] == "Quiet courtyard flat"
    assert loaded["property_facts_json"]["has_floorplan"] is True


def test_property_public_preview_refresh_policy_keeps_nice_distance_facts_lazy() -> None:
    nice_preferences = {
        "max_distance_to_supermarket_m": 300,
        "max_distance_to_supermarket_importance": "nice_to_have",
    }
    assert (
        product_service._property_search_requires_fresh_distance_facts(
            nice_preferences
        )
        is False
    )

    for importance in ("strong_wish", "must_have", "avoid"):
        assert product_service._property_search_requires_fresh_distance_facts(
            {
                "max_distance_to_supermarket_m": 300,
                "max_distance_to_supermarket_importance": importance,
            }
        ) is True


def test_property_public_preview_cache_preserves_flat_hard_gate_aliases() -> None:
    property_url = "https://example.test/listing/cache-parity"
    original_facts = {
        "provider_channel": "findmyhome_at",
        "postal_name": "1020 Wien",
        "object_type": "apartment",
        "living_area_sqm": 81.5,
        "available_from_iso": "2026-08-15",
        "transaction_type": "rent",
        "warm_rent_eur": 1650,
        "has_floorplan": "yes",
        "balcony_available": 1,
        "has_garden": "false",
        "source_platform": "willhaben",
        "source_family": "marketplace",
        "city": "Secretstraße 9, 1010 Wien",
        "district_name": "Coordinates 48.2, 16.3",
        "property_subtype": '{"latitude":48.2,"longitude":16.3}',
    }
    cached_facts = dict(
        product_service._property_public_preview_cache_payload(
            {
                "property_url": property_url,
                "property_facts_json": original_facts,
            }
        ).get("property_facts_json")
        or {}
    )

    assert cached_facts == {
        "provider_channel": "findmyhome_at",
        "postal_name": "1020 Wien",
        "object_type": "apartment",
        "living_area_sqm": 81.5,
        "available_from_iso": "2026-08-15",
        "transaction_type": "rent",
        "warm_rent_eur": 1650,
        "has_floorplan": True,
        "balcony_available": True,
        "has_garden": False,
        "source_platform": "willhaben",
        "source_family": "marketplace",
    }
    for facts in (original_facts, cached_facts):
        assert product_service._property_candidate_matches_requested_property_type(
            property_type="apartment",
            property_url=property_url,
            property_facts=facts,
        ) is True
        assert product_service._property_candidate_min_area_status(
            min_area_m2=80,
            property_url=property_url,
            property_facts=facts,
        ) == "pass"
        assert product_service._property_candidate_matches_availability_horizon(
            available_within_years=1,
            property_facts=facts,
        ) is True
        assert product_service._property_candidate_listing_mode_mismatch(
            listing_mode="rent",
            property_url=property_url,
            property_facts=facts,
        ) is False
        assert product_service._property_candidate_listing_mode_mismatch(
            listing_mode="buy",
            property_url=property_url,
            property_facts=facts,
        ) is True

    poisoned_facts = dict(
        product_service._property_public_preview_cache_payload(
            {
                "property_url": property_url,
                "property_facts_json": {
                    "provider_channel": "findmyhome_at oauth_token=VERYSECRET",
                    "source_platform": "willhaben session=private",
                    "postal_name": "Secretstraße 9, 1010 Wien",
                    "condition": "48.2082, 16.3738",
                    "rooms_label": "lat=48.2082; lon=16.3738",
                    "area_sqm": float("nan"),
                    "price_eur": float("inf"),
                },
            }
        ).get("property_facts_json")
        or {}
    )
    assert poisoned_facts == {}


def test_property_cached_floorplan_augments_current_derstandard_preview_without_replacing_fresher_data() -> None:
    property_url = "https://immobilien.derstandard.at/detail/15201500"
    merged = product_service._property_merge_public_preview(
        property_url=property_url,
        current={
            "property_url": property_url,
            "listing_id": "derstandard-15201500",
            "title": "Current DerStandard title",
            "summary": "Current availability and pricing summary.",
            "media_urls_json": ["https://cdn.example.test/current-hero.jpg"],
            "property_facts_json": {
                "price_eur": 249000,
                "area_m2": 78,
                "is_available": False,
                "has_floorplan": False,
                "floorplan_count": 0,
            },
        },
        cached={
            "property_url": property_url,
            "listing_id": "stale-cache-id",
            "title": "Stale cached title",
            "summary": "Stale cached summary.",
            "media_urls_json": ["https://cdn.example.test/cached-room.jpg"],
            "floorplan_urls_json": ["https://cdn.example.test/derstandard-15201500-floorplan.png"],
            "source_virtual_tour_url": "https://tour.example.test/derstandard-15201500",
            "property_facts_json": {
                "price_eur": 239000,
                "area_m2": 74,
                "is_available": True,
                "rooms": 3,
                "has_floorplan": True,
                "floorplan_count": 1,
                "floorplan_urls_json": [
                    "https://cdn.example.test/derstandard-15201500-floorplan.png"
                ],
            },
        },
    )

    assert merged["listing_id"] == "derstandard-15201500"
    assert merged["title"] == "Current DerStandard title"
    assert merged["summary"] == "Current availability and pricing summary."
    assert merged["media_urls_json"] == [
        "https://cdn.example.test/current-hero.jpg",
        "https://cdn.example.test/cached-room.jpg",
    ]
    assert merged["floorplan_urls_json"] == [
        "https://cdn.example.test/derstandard-15201500-floorplan.png"
    ]
    assert merged["source_virtual_tour_url"] == "https://tour.example.test/derstandard-15201500"
    assert merged["property_facts_json"] == {
        "price_eur": 249000,
        "area_m2": 78,
        "is_available": False,
        "rooms": 3,
        "has_floorplan": True,
        "floorplan_count": 1,
        "floorplan_urls_json": [
            "https://cdn.example.test/derstandard-15201500-floorplan.png"
        ],
    }


def test_property_source_research_only_promotes_labelled_floorplan_media() -> None:
    assert product_service._property_scout_floorplan_media_urls(
        (
            "https://cdn.example.test/living-room.jpg",
            "https://cdn.example.test/Grundriss-Wohnung-12.png",
            "https://cdn.example.test/balcony.webp",
            "https://cdn.example.test/Grundriss-Wohnung-12.png",
        )
    ) == ("https://cdn.example.test/Grundriss-Wohnung-12.png",)


def test_property_floorplan_unknown_status_is_not_positive_layout_evidence() -> None:
    assert product_service._property_candidate_has_floorplan(
        property_url="https://listings.example.test/home-12",
        title="Home 12",
        summary="Two-room rental with balcony.",
        property_facts={
            "has_floorplan": False,
            "floorplan_count": 0,
            "floorplan_research_status": "missing_or_unverified_soft_requirement",
            "floorplan_requirement_mode": "soft",
            "unknowns_json": [
                "Floorplan is required by preference but not exposed by this provider before review."
            ],
            "missing_facts_json": ["floorplan"],
        },
    ) is False


def test_austria_noise_preference_uses_layout_quiet_signal_only_as_weak_hint() -> None:
    adjustment, notes = product_service._property_austria_preference_score_adjustment(
        preferences={"country_code": "AT", "avoid_noise_risk_area": True},
        property_facts={"quiet_layout_signal": "weak_positive"},
        title="Wohnung",
        summary="Ruhige Lage",
    )

    assert adjustment == -2.0
    assert "noise evidence missing" in notes
    assert "layout-derived quiet signal" in notes


def test_property_public_preview_workers_warm_multiple_provider_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    service = ProductService.__new__(ProductService)
    service._container = _PreviewCacheContainer()
    cache_index: dict[str, dict[str, object]] = {}
    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_PROVIDER_WORKER_CONCURRENCY", "2")
    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_PROVIDER_WORKER_WARM_LIMIT", "2")

    preview_calls: list[str] = []

    def _fake_preview(property_url: str, prefer_fast: bool = False) -> dict[str, object]:
        preview_calls.append(property_url)
        return {
            "property_url": property_url,
            "listing_id": property_url.rsplit("/", 1)[-1],
            "title": f"Preview for {property_url.rsplit('/', 1)[-1]}",
            "summary": "Reusable public facts.",
            "property_facts_json": {
                "provider_channel": "provider",
                "has_floorplan": property_url.endswith("1"),
            },
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_compat", _fake_preview)

    result = service._warm_property_public_preview_cache_for_sources(
        specs=[
            {"platform": "derstandard_at", "label": "DER STANDARD", "url": "https://example.test/derstandard", "source_access_level": "browser"},
            {"platform": "immmo_at", "label": "immmo", "url": "https://example.test/immmo", "source_access_level": "public"},
        ],
        prefetched_source_results={
            ("derstandard_at", "https://example.test/derstandard"): {
                "listing_urls": [
                    "https://example.test/listing/1",
                    "https://example.test/listing/2",
                ]
            },
            ("immmo_at", "https://example.test/immmo"): {
                "listing_urls": [
                    "https://example.test/listing/3",
                    "https://example.test/listing/4",
                ]
            },
        },
        cache_index=cache_index,
        plan_key="plus",
    )

    assert result["enabled"] is True
    assert result["worker_concurrency"] == 2
    assert result["warm_limit"] == 2
    assert result["warmed_total"] == 4
    assert result["sources_touched"] == 2
    assert set(preview_calls) == {
        "https://example.test/listing/1",
        "https://example.test/listing/2",
        "https://example.test/listing/3",
        "https://example.test/listing/4",
    }
    assert service._property_public_preview_cache_lookup(
        cache_index=cache_index,
        property_url="https://example.test/listing/3",
    ) is not None


def test_property_adjacent_area_radius_uses_boundary_distance_before_centroid(monkeypatch: pytest.MonkeyPatch) -> None:
    boundary_geojson = {
        "type": "Polygon",
        "coordinates": [[
            [16.3600, 48.2000],
            [16.3700, 48.2000],
            [16.3700, 48.2100],
            [16.3600, 48.2100],
            [16.3600, 48.2000],
        ]],
    }

    monkeypatch.setattr(
        product_service,
        "_property_research_boundary_record",
        lambda query: {
            "display_name": query,
            "geojson": boundary_geojson,
            "bounds": (16.3600, 48.2000, 16.3700, 48.2100),
            "lat": 48.2050,
            "lon": 16.3650,
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_research_forward_geocode",
        lambda query: {"lat": 48.2050, "lon": 16.3650},
    )

    assert product_service._property_candidate_within_adjacent_area_radius(
        location_hints=("1020 Vienna",),
        property_facts={"map_lat": 48.2050, "map_lng": 16.3714},
        country_code="AT",
        region_code="vienna",
        adjacent_area_radius_m=200,
    ) is True


def test_property_adjacent_area_radius_falls_back_to_reference_point_when_boundary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(product_service, "_property_research_boundary_record", lambda query: {})
    monkeypatch.setattr(
        product_service,
        "_property_research_forward_geocode",
        lambda query: {"lat": 48.2050, "lon": 16.3650},
    )

    assert product_service._property_candidate_within_adjacent_area_radius(
        location_hints=("1020 Vienna",),
        property_facts={"map_lat": 48.2050, "map_lng": 16.3655},
        country_code="AT",
        region_code="vienna",
        adjacent_area_radius_m=100,
    ) is True


def test_property_search_area_interior_ratio_is_relative_to_district_size(monkeypatch: pytest.MonkeyPatch) -> None:
    compact_district = {
        "type": "Polygon",
        "coordinates": [[
            [16.0000, 48.0000],
            [16.0100, 48.0000],
            [16.0100, 48.0100],
            [16.0000, 48.0100],
            [16.0000, 48.0000],
        ]],
    }
    large_district = {
        "type": "Polygon",
        "coordinates": [[
            [16.0000, 48.0000],
            [16.1000, 48.0000],
            [16.1000, 48.1000],
            [16.0000, 48.1000],
            [16.0000, 48.0000],
        ]],
    }
    facts = {"map_lat": 48.0050, "map_lng": 16.0020}

    monkeypatch.setattr(product_service, "_property_search_area_boundary_geojsons", lambda **_kwargs: (compact_district,))
    compact_ratio = product_service._property_candidate_search_area_interior_ratio(
        location_hints=("1010 Vienna",),
        property_facts=facts,
        country_code="AT",
        region_code="vienna",
    )

    monkeypatch.setattr(product_service, "_property_search_area_boundary_geojsons", lambda **_kwargs: (large_district,))
    large_ratio = product_service._property_candidate_search_area_interior_ratio(
        location_hints=("1220 Vienna",),
        property_facts=facts,
        country_code="AT",
        region_code="vienna",
    )

    assert compact_ratio is not None
    assert large_ratio is not None
    assert compact_ratio > 0.35
    assert large_ratio < 0.05
    assert compact_ratio > large_ratio * 8


def test_property_search_interleave_by_provider_group_spreads_same_provider_shards() -> None:
    ordered = product_service._property_search_interleave_by_provider_group(
        [
            {"platform": "derstandard_at", "label": "DER STANDARD | 1010 Vienna"},
            {"platform": "derstandard_at", "label": "DER STANDARD | 1020 Vienna"},
            {"platform": "immmo_at", "label": "immmo | 1010 Vienna"},
            {"platform": "findmyhome_at", "label": "FindMyHome | 1010 Vienna"},
            {"platform": "derstandard_at", "label": "DER STANDARD | 1080 Vienna"},
        ]
    )

    assert [row["platform"] for row in ordered[:4]] == [
        "derstandard_at",
        "immmo_at",
        "findmyhome_at",
        "derstandard_at",
    ]


def test_property_search_provider_total_collapses_source_variants() -> None:
    provider_specs = [
        {"provider_source_key": "willhaben:vienna", "platform": "kalandra"},
        {"provider_source_key": "willhaben:salzburg", "provider_key": "WILLHABEN"},
        {"provider_key": "kalandra", "label": "Kalandra | Vienna"},
        {"source_provider_key": "derstandard:vienna", "platform": "derstandard_at"},
        {"source_provider_key": "derstandard:1220", "provider_family": "cooperative"},
    ]

    assert product_service._property_search_provider_total(provider_specs) == 3


def test_property_preview_prefetch_uses_full_worker_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_property_client(principal_id="exec-property-preview-prefetch")
    service = product_service.build_product_service(client.app.state.container)
    lock = threading.Lock()
    current_workers = 0
    max_workers = 0

    monkeypatch.setattr(
        ProductService,
        "_property_public_preview_cache_lookup",
        lambda self, *, cache_index, property_url: None,
    )
    monkeypatch.setattr(
        ProductService,
        "_property_public_preview_cache_store",
        lambda self, *, cache_index, property_url, preview: dict(preview),
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        nonlocal current_workers
        nonlocal max_workers
        with lock:
            current_workers += 1
            max_workers = max(max_workers, current_workers)
        try:
            time.sleep(0.05)
            return {
                "listing_id": property_url,
                "title": property_url,
                "summary": "",
                "property_facts_json": {},
            }
        finally:
            with lock:
                current_workers -= 1

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)

    result = service._prefetch_property_public_previews_for_listing_urls(
        listing_urls=[
            "https://example.test/listing/1",
            "https://example.test/listing/2",
            "https://example.test/listing/3",
            "https://example.test/listing/4",
            "https://example.test/listing/5",
            "https://example.test/listing/6",
        ],
        cache_index={},
        worker_cap=4,
    )

    assert result["worker_concurrency"] == 4
    assert result["cache_refresh_total"] == 6
    assert result["cache_hit_total"] == 0
    assert len(result["previews"]) == 6
    assert max_workers == 4


def test_property_source_preview_prefetch_uses_four_parallel_source_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_property_client(principal_id="exec-property-source-preview-prefetch")
    service = product_service.build_product_service(client.app.state.container)
    lock = threading.Lock()
    current_workers = 0
    max_workers = 0
    started_sources: list[str] = []
    finished_sources: list[str] = []
    source_urls = [f"https://example.test/source/{index}" for index in range(6)]
    listing_urls_by_source = {
        source_url: [f"https://example.test/listing/{index}/{ordinal}" for ordinal in range(2)]
        for index, source_url in enumerate(source_urls)
    }
    initial_listing_urls = {
        listing_urls_by_source[source_url][0]
        for source_url in source_urls[:4]
    }
    initial_worker_barrier = threading.Barrier(4)
    preview_start_order: list[str] = []

    monkeypatch.setattr(
        ProductService,
        "_property_public_preview_cache_lookup",
        lambda self, *, cache_index, property_url: None,
    )
    monkeypatch.setattr(
        ProductService,
        "_property_public_preview_cache_store",
        lambda self, *, cache_index, property_url, preview: dict(preview),
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        nonlocal current_workers
        nonlocal max_workers
        with lock:
            current_workers += 1
            max_workers = max(max_workers, current_workers)
            preview_start_order.append(property_url)
        try:
            if property_url in initial_listing_urls:
                initial_worker_barrier.wait(timeout=2.0)
            return {
                "listing_id": property_url,
                "title": property_url,
                "summary": "",
                "property_facts_json": {},
            }
        finally:
            with lock:
                current_workers -= 1

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)

    result = service._prefetch_property_public_previews_for_sources(
        source_jobs=[
            {
                "platform": f"provider_{index}",
                "url": source_url,
                "__listing_urls__": listing_urls_by_source[source_url],
            }
            for index, source_url in enumerate(source_urls)
        ],
        cache_index={},
        worker_cap=4,
        on_source_started=lambda job: started_sources.append(str(job.get("url") or "")),
        on_source_finished=lambda job, payload: finished_sources.append(str(job.get("url") or "")),
    )

    assert result["worker_concurrency"] == 4
    assert result["cache_refresh_total"] == 12
    assert result["cache_hit_total"] == 0
    assert len(result["source_results"]) == 6
    assert max_workers == 4
    assert started_sources == source_urls
    assert set(preview_start_order[:4]) == initial_listing_urls
    assert sorted(finished_sources) == source_urls
    assert list(result["source_results"]) == [
        (f"provider_{index}", source_url)
        for index, source_url in enumerate(source_urls)
    ]
    for index, source_url in enumerate(source_urls):
        source_result = result["source_results"][(f"provider_{index}", source_url)]
        assert list(source_result["previews"]) == listing_urls_by_source[source_url]
        assert source_result["errors"] == {}


def test_property_source_preview_prefetch_uses_full_worker_cap_for_one_source(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_property_client(principal_id="exec-property-source-preview-single-source")
    service = product_service.build_product_service(client.app.state.container)
    lock = threading.Lock()
    current_workers = 0
    max_workers = 0
    started_sources: list[str] = []
    finished_sources: list[str] = []
    progress_totals: list[int] = []
    listing_urls = [f"https://example.test/listing/{index}" for index in range(8)]
    worker_barrier = threading.Barrier(4)

    monkeypatch.setattr(
        ProductService,
        "_property_public_preview_cache_lookup",
        lambda self, *, cache_index, property_url: None,
    )
    monkeypatch.setattr(
        ProductService,
        "_property_public_preview_cache_store",
        lambda self, *, cache_index, property_url, preview: dict(preview),
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        nonlocal current_workers
        nonlocal max_workers
        with lock:
            current_workers += 1
            max_workers = max(max_workers, current_workers)
        try:
            worker_barrier.wait(timeout=2.0)
            return {
                "listing_id": property_url,
                "title": property_url,
                "summary": "",
                "property_facts_json": {},
            }
        finally:
            with lock:
                current_workers -= 1

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)

    result = service._prefetch_property_public_previews_for_sources(
        source_jobs=[
            {
                "platform": "willhaben",
                "url": "https://example.test/source/willhaben",
                "__listing_urls__": listing_urls,
            }
        ],
        cache_index={},
        worker_cap=4,
        on_source_started=lambda job: started_sources.append(str(job.get("url") or "")),
        on_source_progress=lambda job, payload: progress_totals.append(int(payload.get("completed_total") or 0)),
        on_source_finished=lambda job, payload: finished_sources.append(str(job.get("url") or "")),
    )

    source_result = result["source_results"][("willhaben", "https://example.test/source/willhaben")]
    assert result["worker_concurrency"] == 4
    assert result["cache_refresh_total"] == len(listing_urls)
    assert result["cache_hit_total"] == 0
    assert max_workers == 4
    assert list(source_result["previews"]) == listing_urls
    assert source_result["errors"] == {}
    assert started_sources == ["https://example.test/source/willhaben"]
    assert finished_sources == ["https://example.test/source/willhaben"]
    assert progress_totals == list(range(1, len(listing_urls) + 1))


def test_property_search_run_status_fixes_provider_total_from_source_variants() -> None:
    principal_id = "exec-property-run-provider-count-fix"
    client = build_product_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"provider-total-fix-{uuid.uuid4().hex}"
    now_iso = datetime.now(timezone.utc).isoformat()

    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "created_at": now_iso,
            "updated_at": now_iso,
            "selected_platforms": ["willhaben", "kalandra", "wiener_wohnen"],
            "summary": {
                "provider_total": 156,
                "source_variant_total": 156,
                "sources": [
                    {
                        "provider_source_key": "willhaben:vienna",
                        "source_label": "Willhaben | Vienna",
                    },
                    {
                        "provider_source_key": "willhaben:salzburg",
                        "source_label": "Willhaben | Salzburg",
                    },
                    {
                        "source_provider_key": "wiener_wohnen:wien",
                        "source_label": "Wiener Wohnen",
                    },
                    {
                        "source_provider_key": "kalandra:wien",
                        "source_label": "Kalandra Wien",
                    },
                ],
            },
        }

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert int(dict(status.get("summary") or {}).get("provider_total") or 0) == 3
    assert int(dict(status.get("summary") or {}).get("source_variant_total") or 0) == 156


def test_property_search_run_status_repairs_active_preseeded_source_overcount() -> None:
    principal_id = "exec-property-run-source-count-fix"
    client = build_product_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"source-total-fix-{uuid.uuid4().hex}"
    now_iso = datetime.now(timezone.utc).isoformat()
    source_rows = [
        *[
            {
                "provider_source_key": f"provider-{index}:area",
                "source_label": f"Provider {index}",
                "status": "completed",
            }
            for index in range(6)
        ],
        {"provider_source_key": "provider-6:area", "source_label": "Provider 6", "status": "warming"},
        {"provider_source_key": "provider-7:area", "source_label": "Provider 7", "status": "starting"},
        {"provider_source_key": "provider-8:area", "source_label": "Provider 8", "status": "queued"},
        {"provider_source_key": "provider-9:area", "source_label": "Provider 9", "status": "queued"},
    ]

    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "progress": 96,
            "created_at": now_iso,
            "updated_at": now_iso,
            "selected_platforms": [f"provider-{index}" for index in range(10)],
            "summary": {
                "status": "in_progress",
                "sources_total": 10,
                "source_variant_total": 10,
                "sources_completed": 10,
                "sources": source_rows,
            },
        }

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert int(dict(status.get("summary") or {}).get("sources_completed") or 0) == 6


def test_property_visual_state_does_not_cross_update_same_source_ref_different_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-visual-state-provider-collision"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"visual-state-collision-{uuid.uuid4().hex}"
    shared_source_ref = "provider:shared-listing-id"
    first_url = "https://provider-a.example/listings/shared"
    second_url = "https://provider-b.example/listings/shared"
    now_iso = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(product_service, "_store_property_search_run_record", lambda payload: None)

    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        previous_registry = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY)
        product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "processed",
            "created_at": now_iso,
            "updated_at": now_iso,
            "summary": {
                "sources": [
                    {
                        "source_label": "Provider A",
                        "top_candidates": [
                            {
                                "title": "Shared listing from provider A",
                                "source_ref": shared_source_ref,
                                "property_url": first_url,
                            }
                        ],
                    },
                    {
                        "source_label": "Provider B",
                        "top_candidates": [
                            {
                                "title": "Shared listing from provider B",
                                "source_ref": shared_source_ref,
                                "property_url": second_url,
                                "flythrough_eta_minutes": "10",
                            }
                        ],
                    },
                ],
                "ranked_candidates": [],
            },
        }
    try:
        service._persist_property_search_visual_state(  # noqa: SLF001
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref="",
            source_ref=shared_source_ref,
            property_url=second_url,
            visual_state={"tour_status": "pending", "flythrough_status": "queued", "flythrough_eta_minutes": ""},
        )
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            sources = list(dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id]["summary"]).get("sources") or [])
        first_candidate = dict(dict(sources[0]).get("top_candidates")[0])
        second_candidate = dict(dict(sources[1]).get("top_candidates")[0])
        assert "tour_status" not in first_candidate
        assert first_candidate["property_url"] == first_url
        assert second_candidate["property_url"] == second_url
        assert second_candidate["tour_status"] == "pending"
        assert second_candidate["flythrough_status"] == "queued"
        assert second_candidate["flythrough_eta_minutes"] == ""
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.update(previous_registry)


def test_property_tour_event_lookup_does_not_reuse_same_source_ref_for_other_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-tour-event-url-collision"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    shared_source_ref = "provider:shared-listing-id"
    first_url = "https://provider-a.example/listings/shared"
    second_url = "https://provider-b.example/listings/shared"
    rows = [
        SimpleNamespace(
            principal_id=principal_id,
            channel="product",
            event_type="generic_property_tour_created",
            source_id=shared_source_ref,
            payload={
                "property_url": first_url,
                "tour_url": "https://propertyquarry.com/tours/provider-a-shared",
            },
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    ]

    monkeypatch.setattr(
        client.app.state.container.channel_runtime,
        "list_recent_observations",
        lambda limit=4000, principal_id="": rows[:limit],
    )

    assert (
        service._latest_property_tour_event(  # noqa: SLF001
            principal_id=principal_id,
            source_ref=shared_source_ref,
            property_url=second_url,
        )
        is None
    )
    matched = service._latest_property_tour_event(  # noqa: SLF001
        principal_id=principal_id,
        source_ref=shared_source_ref,
        property_url=first_url,
    )
    assert matched is not None
    assert dict(matched["payload"])["tour_url"].endswith("/provider-a-shared")


def test_property_scout_listing_ref_host_qualifies_weak_provider_ids() -> None:
    willhaben_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/test-123456789/"
    provider_a_url = "https://provider-a.example/listings/123456789/"
    provider_b_url = "https://provider-b.example/listings/123456789/"

    assert product_service._property_scout_listing_ref("123456789", willhaben_url) == "123456789"  # type: ignore[attr-defined]
    assert (  # type: ignore[attr-defined]
        product_service._property_scout_listing_ref("123456789", provider_a_url) == "provider-a.example:123456789"
    )
    assert (  # type: ignore[attr-defined]
        product_service._property_scout_listing_ref("123456789", provider_b_url) == "provider-b.example:123456789"
    )
    assert product_service._property_scout_listing_ref("", provider_a_url) == "provider-a.example:123456789"  # type: ignore[attr-defined]
    assert product_service._property_scout_listing_ref("provider:123", provider_a_url) == "provider:123"  # type: ignore[attr-defined]


def test_property_search_run_status_fixes_inflated_provider_total_when_source_rows_missing() -> None:
    principal_id = "exec-property-run-provider-count-no-sources"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"provider-total-no-sources-{uuid.uuid4().hex}"

    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "selected_platforms": ["willhaben", "kalandra", "wiener_wohnen"],
            "summary": {
                "provider_total": 156,
                "source_variant_total": 156,
                "sources": [],
            },
        }

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert int(dict(status.get("summary") or {}).get("provider_total") or 0) == 3


def test_property_search_run_status_derives_display_total_for_old_default_all_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-run-provider-display-old-default"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"provider-display-old-{uuid.uuid4().hex}"
    compact_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "processed",
        "selected_platforms": [],
        "summary": {
            "status": "processed",
            "provider_total": 1,
            "sources_total": 2,
            "source_variant_total": 2,
            "sources_completed": 2,
            "brief_snapshot_status": "old_run",
            "sources": [
                {
                    "platform": "willhaben",
                    "source_label": "Willhaben",
                    "source_scope_label": "Willhaben | Austria | Rent | 1020 Vienna",
                    "status": "completed",
                },
                {
                    "platform": "willhaben",
                    "source_label": "Willhaben",
                    "source_scope_label": "Willhaben | Austria | Rent | 1010 Vienna",
                    "status": "completed",
                },
            ],
        },
    }

    def _fake_load_compact_record(*, run_id: str, principal_id: str) -> dict[str, object] | None:
        if str(run_id or "").strip() == compact_run["run_id"] and str(principal_id or "").strip() == compact_run["principal_id"]:
            return dict(compact_run)
        return None

    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", _fake_load_compact_record)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=True,
    )

    assert status is not None
    summary = dict(status.get("summary") or {})
    expected_total = len(property_market_catalog.selectable_property_platform_keys(country_code="AT", listing_mode="rent"))
    assert int(summary.get("provider_total") or 0) == 1
    assert int(summary.get("source_variant_total") or 0) == 2
    assert int(summary.get("sources_completed") or 0) == 2
    assert int(summary.get("provider_display_total") or 0) == expected_total
    assert int(status.get("provider_display_total") or 0) == expected_total
    assert int(summary.get("source_variant_display_total") or 0) == expected_total


def test_property_search_status_response_guard_derives_default_all_display_total() -> None:
    expected_total = len(property_market_catalog.selectable_property_platform_keys(country_code="AT", listing_mode="rent"))

    payload = _property_search_apply_response_display_totals(
        {
            "run_id": "old-default-all-run",
            "status": "processed",
            "selected_platforms": [],
            "summary": {
                "provider_total": 1,
                "source_variant_total": 2,
                "sources_total": 2,
                "sources_completed": 2,
                "sources": [
                    {
                        "platform": "willhaben",
                        "source_scope_label": "Willhaben | Austria | Rent | 1020 Vienna",
                        "status": "completed",
                    },
                    {
                        "platform": "willhaben",
                        "source_scope_label": "Willhaben | Austria | Rent | 1010 Vienna",
                        "status": "completed",
                    },
                ],
            },
        }
    )

    summary = dict(payload.get("summary") or {})
    assert payload["provider_display_total"] == expected_total
    assert payload["source_variant_display_total"] == expected_total
    assert summary["provider_display_total"] == expected_total
    assert summary["source_variant_display_total"] == expected_total


def test_property_search_status_response_guard_preserves_explicit_display_total() -> None:
    payload = _property_search_apply_response_display_totals(
        {
            "run_id": "explicit-display-run",
            "status": "processed",
            "provider_display_total": 29,
            "source_variant_display_total": 231,
            "selected_platforms": [],
            "summary": {
                "provider_total": 2,
                "provider_display_total": 29,
                "source_variant_total": 2,
                "source_variant_display_total": 231,
                "sources_total": 2,
                "sources_completed": 2,
                "sources": [
                    {
                        "platform": "willhaben",
                        "source_scope_label": "Willhaben | Austria | Rent | 1020 Vienna",
                        "status": "completed",
                    },
                    {
                        "platform": "remax_at",
                        "source_scope_label": "RE/MAX Austria | Austria | Rent | 1020 Vienna",
                        "status": "completed",
                    },
                ],
            },
        }
    )

    summary = dict(payload.get("summary") or {})
    assert payload["provider_display_total"] == 29
    assert payload["source_variant_display_total"] == 231
    assert summary["provider_display_total"] == 29
    assert summary["source_variant_display_total"] == 231


def test_property_search_run_lightweight_status_preserves_compact_display_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-run-lightweight-display-totals"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"lightweight-display-totals-{uuid.uuid4().hex}"

    compact_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "in_progress",
        "progress": 12,
        "provider_display_total": 29,
        "source_variant_display_total": 231,
        "selected_platforms": [],
        "summary": {
            "status": "in_progress",
            "provider_total": 2,
            "provider_display_total": 29,
            "sources_total": 2,
            "source_variant_total": 2,
            "source_variant_display_total": 231,
            "sources_completed": 2,
            "sources": [
                {"platform": "willhaben", "source_label": "Willhaben", "status": "completed"},
                {"platform": "remax_at", "source_label": "RE/MAX Austria", "status": "completed"},
            ],
        },
    }

    def _fake_load_compact_record(*, run_id: str, principal_id: str) -> dict[str, object] | None:
        if str(run_id or "").strip() == compact_run["run_id"] and str(principal_id or "").strip() == compact_run["principal_id"]:
            return property_search_storage._compact_property_search_run_record(compact_run)  # type: ignore[attr-defined]
        return None

    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", _fake_load_compact_record)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=True,
    )

    assert status is not None
    summary = dict(status.get("summary") or {})
    assert int(status.get("provider_display_total") or 0) == 29
    assert int(status.get("source_variant_display_total") or 0) == 231
    assert int(summary.get("provider_display_total") or 0) == 29
    assert int(summary.get("source_variant_display_total") or 0) == 231


def test_property_search_run_status_lightweight_fixes_inflated_provider_total(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-run-provider-count-lightweight"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"provider-total-lightweight-{uuid.uuid4().hex}"

    compact_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "in_progress",
        "selected_platforms": ["willhaben", "kalandra", "wiener_wohnen"],
        "summary": {
            "provider_total": 156,
            "source_variant_total": 156,
            "sources_total": 104,
        },
    }

    def _fake_load_compact_record(*, run_id: str, principal_id: str) -> dict[str, object] | None:
        if str(run_id or "").strip() == compact_run["run_id"] and str(principal_id or "").strip() == compact_run["principal_id"]:
            return dict(compact_run)
        return None

    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", _fake_load_compact_record)
    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=True,
    )

    assert status is not None
    assert int(dict(status.get("summary") or {}).get("provider_total") or 0) == 3
    assert int(dict(status.get("summary") or {}).get("source_variant_total") or 0) == 156


def test_property_search_location_matching_prefers_requested_districts() -> None:
    hints = _property_search_location_hints({"location_query": "1200 Vienna, 1020 Vienna, 1090"})

    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.willhaben.at/iad/object?adId=1",
        title="Wohnung in 1200 Wien mit Lift",
        summary="Nahe U6 und familienfreundlich.",
        property_facts={"postal_name": "1200 Wien"},
    ) is True
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.willhaben.at/iad/object?adId=2",
        title="Wohnung in 1130 Wien",
        summary="Altbau",
        property_facts={"postal_name": "1130 Wien"},
    ) is False


def test_property_search_location_matching_rejects_unselected_vienna_districts() -> None:
    hints = _property_search_location_hints(
        {
            "location_query": (
                "1020 Vienna, 1070 Vienna, 1090 Vienna, 1100 Vienna, 1110 Vienna, "
                "1180 Vienna, 1200 Vienna, 1220 Vienna, Aspern"
            )
        }
    )

    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1150-rudolfsheim-fuenfhaus/top-lage-naehe-westbahnhof",
        title="Top Lage Nähe Westbahnhof, 69 m², € 838,13, (1150 Wien) - willhaben",
        summary="Provider result page was queried from a selected Vienna source scope.",
        property_facts={"source_scope_location": "1020 Vienna", "source_city": "Vienna"},
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/familienwohnung",
        title="Helle Familienwohnung, 69 m², € 938,13, (1020 Wien) - willhaben",
        summary="Provider result page was queried from a selected Vienna source scope.",
        property_facts={"source_scope_location": "1020 Vienna", "source_city": "Vienna"},
    ) is True


def test_property_search_area_accepts_adjacent_districts_for_fuzzy_scope_only() -> None:
    strict_preferences = {
        "country_code": "AT",
        "region_code": "vienna",
        "location_query": "Wien",
        "selected_districts": ["1010 Vienna"],
    }
    fuzzy_preferences = {
        **strict_preferences,
        "adjacent_area_radius_m": 750,
    }
    hints = _property_search_location_hints(strict_preferences)

    assert product_service._property_candidate_matches_search_area(
        location_hints=hints,
        request_preferences=strict_preferences,
        source_spec={"country_code": "AT"},
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/fuzzy-adjacent/",
        title="Wohnung mieten in 1020 Wien | 70 m² | 3 Zimmer",
        summary="Concrete listing evidence says Leopoldstadt.",
        property_facts={"postal_name": "1020 Wien"},
    ) is False
    assert product_service._property_candidate_matches_search_area(
        location_hints=hints,
        request_preferences=fuzzy_preferences,
        source_spec={"country_code": "AT"},
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/fuzzy-adjacent/",
        title="Wohnung mieten in 1020 Wien | 70 m² | 3 Zimmer",
        summary="Concrete listing evidence says Leopoldstadt.",
        property_facts={"postal_name": "1020 Wien"},
    ) is True
    assert product_service._property_candidate_matches_search_area(
        location_hints=hints,
        request_preferences=fuzzy_preferences,
        source_spec={"country_code": "AT"},
        property_url="https://immobilien.derstandard.at/detail/wohnung-mieten-in-1220-wien",
        title="Wohnung mieten in 1220 Wien | 60 m² | 2 Zimmer",
        summary="Concrete listing evidence says Donaustadt.",
        property_facts={"postal_name": "1220 Wien"},
    ) is False
    assert product_service._property_candidate_matches_search_area(
        location_hints=hints,
        request_preferences=fuzzy_preferences,
        source_spec={"country_code": "AT"},
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/fuzzy-far/",
        title="Moderne Zwei-Zimmer Wohnung mit Terrasse in Salzburg",
        summary="Concrete listing evidence says Salzburg.",
        property_facts={"postal_name": "5020 Salzburg"},
    ) is False


def test_property_search_area_keeps_selected_district_scope_hard_without_radius() -> None:
    strict_preferences = {
        "country_code": "AT",
        "region_code": "vienna",
        "location_query": "Wien",
        "selected_districts": ["1010 Vienna"],
        "search_mode": "discovery",
    }
    hints = _property_search_location_hints(strict_preferences)

    assert product_service._property_candidate_matches_search_area(
        location_hints=hints,
        request_preferences=strict_preferences,
        source_spec={"country_code": "AT"},
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/adjacent/",
        title="Wohnung mieten in 1020 Wien | 70 m² | 3 Zimmer",
        summary="Concrete listing evidence says Leopoldstadt.",
        property_facts={"postal_name": "1020 Wien"},
    ) is False
    assert product_service._property_candidate_matches_search_area(
        location_hints=hints,
        request_preferences=strict_preferences,
        source_spec={"country_code": "AT"},
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1220-donaustadt/neighbor/",
        title="Wohnung mieten in 1220 Wien | 62 m² | 2 Zimmer",
        summary="Concrete listing evidence says Donaustadt.",
        property_facts={"postal_name": "1220 Wien"},
    ) is False


def test_property_search_location_hints_prefer_selected_districts_over_broad_location_query() -> None:
    assert _property_search_location_hints(
        {
            "location_query": "Wien",
            "selected_districts": ["1010 Vienna"],
        }
    ) == ("1010 Vienna",)

    assert _property_search_location_hints(
        {
            "location_query": "Wien",
            "raw_preferences": {"selected_districts": ["1010 Vienna"]},
        }
    ) == ("1010 Vienna",)


def test_property_exact_source_scope_location_hints_only_use_postal_scopes() -> None:
    assert product_service._property_exact_source_scope_location_hints(
        source_label="Willhaben | Austria | Rent | 1010 Vienna",
    ) == ("1010 Vienna",)
    assert product_service._property_exact_source_scope_location_hints(
        source_label="Provider | Austria | Rent | 8055 Graz",
    ) == ("8055 Graz",)
    assert product_service._property_exact_source_scope_location_hints(
        source_label="Willhaben Vienna",
    ) == ()


def test_property_source_scope_location_extracts_all_postal_url_scopes_cleanly() -> None:
    assert product_service._property_search_source_scope_location(
        source_url="https://www.raiffeisen-wohnbau.at/de/projects/id/1090-vienna/augasse-17/70",
        source_label="Raiffeisen WohnBau | Austria | Rent",
    ) == "1090 Vienna"
    assert product_service._property_search_source_scope_location(
        source_url="https://example.at/search/8055-graz/results",
        source_label="Provider | Austria | Rent",
    ) == "8055 Graz"
    assert product_service._property_search_source_scope_location(
        source_url="https://example.at/search/5020-salzburg/results",
        source_label="Provider | Austria | Rent",
    ) == "5020 Salzburg"
    assert product_service._property_search_source_scope_location(
        source_url="https://example.at/search/4780-schaerding/results",
        source_label="Provider | Austria | Rent",
    ) == "4780 Schaerding"
    assert product_service._property_search_source_scope_location(
        source_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/oberoesterreich/linz/demo-dirty-scope/",
        source_label="Willhaben | Austria | Rent | 8055 Graz",
    ) == "8055 Graz"


def test_property_exact_source_scope_hints_strip_url_path_tail_for_all_postals() -> None:
    assert product_service._property_exact_source_scope_location_hints(
        source_url="https://www.raiffeisen-wohnbau.at/de/projects/id/1090-vienna/augasse-17/70",
        source_label="Raiffeisen WohnBau | Austria | Rent",
    ) == ("1090 Vienna",)
    assert product_service._property_exact_source_scope_location_hints(
        source_url="https://example.at/search/8055-graz/results",
        source_label="Provider | Austria | Rent",
    ) == ("8055 Graz",)


def test_property_search_location_matching_rejects_explicit_non_vienna_marker() -> None:
    hints = _property_search_location_hints({"location_query": "Vienna"})

    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://example.test/listing/waidhofen",
        title="Altbau apartment in Waidhofen an der Ybbs",
        summary="Austria buy opportunity.",
        property_facts={"postal_name": "Waidhofen an der Ybbs"},
    ) is False


def test_property_investment_price_eur_parses_localized_thousand_separators() -> None:
    assert product_service._property_investment_price_eur({"price_display": "EUR 669.000,-"}) == 669000.0
    assert product_service._property_investment_price_eur({"price_display": "€ 1.250.000"}) == 1250000.0


def test_property_investment_underwriting_display_uses_listing_currency() -> None:
    payload = product_service._property_investment_underwriting_payload(
        title="Two bedroom flat",
        summary="Investment candidate.",
        facts={"country_code": "GB", "currency_code": "GBP", "area_sqm": 80, "price_display": "GBP 420000"},
        preferences={},
        snapshot={
            "gross_yield_pct": 4.8,
            "expected_monthly_rent_eur": 1650,
            "current_price_per_sqm_eur": 5250,
            "market_buy_per_sqm_eur": 5400,
            "market_rent_per_sqm_eur": 22.25,
        },
    )

    assert payload["expected_rent_display"] == "Rent model about GBP 1 650/mo"
    assert payload["price_per_sqm"] == "Buy side about GBP 5 250/m2"
    assert payload["market_buy_per_sqm_display"] == "Local buy median about GBP 5 400/m2"
    assert payload["market_rent_per_sqm_display"] == "Local rent median about GBP 22.25/m2"
    assert "EUR" not in " ".join(
        str(payload.get(key) or "")
        for key in (
            "expected_rent_display",
            "price_per_sqm",
            "market_buy_per_sqm_display",
            "market_rent_per_sqm_display",
        )
    )


def test_property_listing_mode_mismatch_uses_transaction_text_not_parser_price_field() -> None:
    enriched_rent_mode = product_service._property_enrich_facts_from_listing_text(
        facts={"property_type": "apartment", "postal_name": "1010 Wien"},
        title="Eigentumswohnung in 1010 Wien | 77 m² | € 669.000",
        summary="Kaufpreis laut Expose.",
        listing_mode="rent",
    )

    assert enriched_rent_mode.get("total_rent_eur") == 669000.0
    assert product_service._property_candidate_listing_mode_mismatch(
        listing_mode="rent",
        property_url="https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/wien-1010-innere-stadt/example/",
        title="Eigentumswohnung in 1010 Wien | 77 m² | € 669.000",
        summary="Kaufpreis laut Expose.",
        property_facts=enriched_rent_mode,
    ) is True
    assert product_service._property_candidate_listing_mode_mismatch(
        listing_mode="buy",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/example/",
        title="Mietwohnung in 1010 Wien | 77 m² | € 1.598",
        summary="Gesamtmiete laut Expose.",
        property_facts={"property_type": "apartment", "price_eur": 1598.0, "postal_name": "1010 Wien"},
    ) is True


def test_property_listing_mode_mismatch_treats_generic_price_as_neutral_for_rent_preview() -> None:
    assert product_service._property_candidate_listing_mode_mismatch(
        listing_mode="rent",
        property_url="https://www.willhaben.at/iad/object?adId=1775972917",
        title="All-inclusive living, Balkon, U-Bahn, Lift vorhanden",
        summary="",
        property_facts={"property_type": "apartment", "price_display": "€ 1.017,09", "postal_name": "1010 Vienna"},
    ) is False

    assert product_service._property_candidate_listing_mode_mismatch(
        listing_mode="rent",
        property_url="https://www.willhaben.at/iad/object?adId=1775972917",
        title="Eigentumswohnung mit Balkon",
        summary="",
        property_facts={"property_type": "apartment", "price_display": "€ 669.000", "postal_name": "1010 Vienna"},
    ) is True


def test_property_search_location_matching_rejects_source_scope_only_for_exact_postal_scope() -> None:
    hints = _property_search_location_hints({"location_query": "1200 Vienna, 1020 Vienna, 1090"})
    facts = product_service._property_facts_with_source_scope(
        facts={"street_address": "Rotensterngasse 21", "provider_channel": "justiz_edikte_at"},
        source_url=(
            "https://edikte2.justiz.gv.at/edikte/ex/exedi3.nsf/suchedi?"
            "retfields=%5BVPLZ%5D=1020;%5BVOrt%5D=Wien"
        ),
        source_label="Justiz Edikte Auctions | Austria | Buy | 1020 Vienna",
    )

    assert facts["source_scope_location"] == "1020 Vienna"
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://edikte2.justiz.gv.at/edikte/ex/exedi3.nsf/alldoc/example!OpenDocument",
        title="BG Leopoldstadt, 082 25 E 89/25g",
        summary="Sparse judicial auction detail page.",
        property_facts=facts,
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://edikte2.justiz.gv.at/edikte/ex/exedi3.nsf/alldoc/example-1020-wien!OpenDocument",
        title="BG Leopoldstadt, 1020 Wien, 082 25 E 89/25g",
        summary="Sparse judicial auction detail page.",
        property_facts=facts,
    ) is True


def test_property_provider_greenfield_api_returns_country_scoped_catalog_with_austria_and_cr_regression_coverage() -> None:
    client = build_property_client(principal_id="exec-provider-catalog-germany")
    de_body = client.get("/app/api/property/providers?country=DE").json()

    assert any(row["value"] == "core_portals_de" and row["family"] == "core_portal" for row in de_body["providers"])
    assert any(row["value"] == "shared_housing_de" and row["family"] == "shared_housing" for row in de_body["providers"])
    assert any(row["value"] == "corporate_landlords_de" and row["family"] == "corporate_landlord" for row in de_body["providers"])
    assert any(row["value"] == "municipal_housing_de" and row["family"] == "municipal_housing" for row in de_body["providers"])
    assert any(row["value"] == "immoscout_de" for row in de_body["providers"])
    assert any(row["value"] == "wg_gesucht_de" and row["family"] == "shared_housing" for row in de_body["providers"])
    assert any(row["value"] == "vonovia_de" and row["family"] == "corporate_landlord" for row in de_body["providers"])
    assert any(row["value"] == "neubaukompass_de" and row["family"] == "developer_projects" for row in de_body["providers"])
    assert any(row["value"] == "auctions_de" and row["family"] == "distressed_sales" for row in de_body["providers"])
    assert any(row["value"] == "broker_direct_de" and row["family"] == "broker_direct" for row in de_body["providers"])
    assert any(row["value"] == "furnished_relocation_de" and row["family"] == "furnished_relocation" for row in de_body["providers"])
    assert any(row["value"] == "ohne_makler_de" and row["family"] == "broker_direct" for row in de_body["providers"])
    assert any(row["value"] == "von_poll_de" and row["family"] == "broker_direct" for row in de_body["providers"])
    assert any(row["value"] == "wohnungsboerse_de" and row["family"] == "core_portal" for row in de_body["providers"])

    at_body = client.get("/app/api/property/providers?country=AT").json()

    assert any(row["value"] == "public_housing_at" and row["family"] == "public_housing" for row in at_body["providers"])
    assert any(row["value"] == "genossenschaften_at" and row["family"] == "cooperative" for row in at_body["providers"])
    assert any(row["value"] == "wohnberatung_wien" and row["family"] == "public_housing" for row in at_body["providers"])
    assert any(row["value"] == "wiener_wohnen" and row["family"] == "public_housing" for row in at_body["providers"])
    assert any(row["value"] == "gesiba_at" and row["family"] == "cooperative" for row in at_body["providers"])
    assert any(row["value"] == "oesw_at" and row["family"] == "cooperative" for row in at_body["providers"])
    assert any(row["value"] == "egw_at" and row["family"] == "cooperative" for row in at_body["providers"])
    assert any(row["value"] == "ohne_makler_at" and row["family"] == "broker_direct" for row in at_body["providers"])
    assert any(row["value"] == "sreal_at" and row["family"] == "broker_direct" for row in at_body["providers"])
    assert any(row["value"] == "raiffeisen_immobilien_at" and row["family"] == "broker_direct" for row in at_body["providers"])
    assert any(row["value"] == "wohnnet_at" and row["family"] == "marketplace" for row in at_body["providers"])
    assert any(row["value"] == "keinmakler_at" and row["family"] == "broker_direct" for row in at_body["providers"])
    assert any(row["value"] == "zvginfo_at" and row["family"] == "distressed_sales" for row in at_body["providers"])
    assert any(row["value"] == "school_directories_de" for row in de_body["evidence_sources"])
    assert any(row["value"] == "statatlas_schulen_at" for row in at_body["evidence_sources"])

    cr_body = client.get("/app/api/property/providers?country=CR").json()

    assert any(row["value"] == "properstar_cr" and row["family"] == "marketplace" for row in cr_body["providers"])
    assert any(row["value"] == "century21_cr" and row["family"] == "broker_direct" for row in cr_body["providers"])
    assert any(row["value"] == "remax_cr" and row["family"] == "broker_direct" for row in cr_body["providers"])


def test_property_provider_greenfield_api_returns_mode_aware_default_platforms() -> None:
    client = build_property_client(principal_id="exec-provider-catalog-mode-aware")

    at_buy_body = client.get(
        "/app/api/property/providers",
        params={"country": "AT", "listing_mode": "buy", "property_type": "apartment"},
    ).json()
    at_land_body = client.get(
        "/app/api/property/providers",
        params={"country": "AT", "listing_mode": "buy", "property_type": "land"},
    ).json()
    de_buy_body = client.get(
        "/app/api/property/providers",
        params={"country": "DE", "listing_mode": "buy", "property_type": "apartment"},
    ).json()
    cr_rent_body = client.get(
        "/app/api/property/providers",
        params={"country": "CR", "listing_mode": "rent", "property_type": "apartment"},
    ).json()
    cr_buy_body = client.get(
        "/app/api/property/providers",
        params={"country": "CR", "listing_mode": "buy", "property_type": "apartment"},
    ).json()

    assert at_buy_body["listing_mode"] == "buy"
    assert at_buy_body["property_type"] == "apartment"
    assert at_buy_body["default_platforms"] == [
        "willhaben",
        "immmo",
        "immoscout_at",
        "immobilien_net_at",
        "ohne_makler_at",
        "sreal_at",
        "raiffeisen_immobilien_at",
        "wohnnet_at",
        "keinmakler_at",
        "derstandard_at",
        "broker_direct_at",
        "developer_projects_at",
    ]
    assert at_land_body["default_platforms"] == [
        "willhaben",
        "immmo",
        "immoscout_at",
        "broker_direct_at",
    ]
    assert de_buy_body["default_platforms"] == [
        "core_portals_de",
        "wohnungsboerse_de",
        "new_build_de",
        "broker_direct_de",
    ]
    assert cr_rent_body["default_platforms"] == [
        "encuentra24_cr",
        "re_cr_mls",
        "properstar_cr",
    ]
    assert "century21_cr" in cr_buy_body["default_platforms"]
    assert "remax_cr" in cr_buy_body["default_platforms"]


def test_austria_generated_source_defaults_use_public_and_cooperative_lanes_for_rent() -> None:
    specs = property_market_catalog.generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Vienna",
        },
        selected_platforms=(),
        principal_id="exec-property-at-rent-defaults",
        default_person_id="self",
        max_results=4,
    )

    platforms = {str(row["platform"]) for row in specs}

    assert "public_housing_at" in platforms
    assert "genossenschaften_at" in platforms
    assert "immowelt_at" in platforms
    assert "flatbee" in platforms


def test_empty_property_provider_selection_expands_to_all_selectable_market_providers() -> None:
    preferences = {
        "country_code": "AT",
        "language_code": "de",
        "listing_mode": "rent",
        "location_query": "Vienna",
    }

    platforms, removed_platforms, removed_details = product_service._property_search_execution_platforms((), preferences)
    selectable = property_market_catalog.selectable_property_platform_keys(country_code="AT", listing_mode="rent")
    featured_defaults = property_market_catalog.default_platforms_for_country_listing_mode("AT", "rent")

    assert set(platforms) == set(selectable)
    assert len(platforms) > len(featured_defaults)
    assert "immowelt_at" in platforms
    assert "flatbee" in platforms
    assert removed_platforms == ()
    assert removed_details == ()


def test_generated_source_specs_use_selected_districts_over_broad_location_query() -> None:
    specs = property_market_catalog.generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "region_code": "vienna",
            "location_query": "1010 Vienna, 1020 Vienna, 1090 Vienna, 1190 Vienna, 1200 Vienna, 1220 Vienna",
            "selected_districts": ["1010 Vienna"],
            "property_type": ["apartment"],
            "max_price_eur": 1000,
            "min_rooms": 2,
            "min_area_m2": 60,
        },
        selected_platforms=("willhaben",),
        principal_id="exec-property-selected-district-source-scope",
        default_person_id="self",
        max_results=4,
    )

    labels = [str(row.get("label") or "") for row in specs]
    urls = [str(row.get("url") or "") for row in specs]
    assert labels == ["Willhaben | Austria | Rent | 1010 Vienna"]
    assert len(urls) == 1
    assert "/mietwohnungen/wien/wien-1010-innere-stadt" in urls[0]
    assert "q=1010+Vienna" not in urls[0]
    assert "1020" not in urls[0]
    assert specs[0]["provider_filter_pushdown"]["applied"]["location_query"] == "1010 Vienna"


def test_generated_source_specs_skip_region_incompatible_austria_grouped_sources() -> None:
    specs = property_market_catalog.generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "region_code": "vienna",
            "location_query": "1010 Vienna",
            "selected_districts": ["1010 Vienna"],
            "property_type": ["apartment"],
            "min_area_m2": 60,
        },
        selected_platforms=("genossenschaften_at", "salzburg_wohnbau_at", "ooe_wohnbau_at", "wag_at"),
        principal_id="exec-property-region-compatible-source-scope",
        default_person_id="self",
        max_results=4,
    )

    labels = [str(row.get("label") or "") for row in specs]
    joined = "\n".join(labels)
    assert labels
    assert all("1010 Vienna" in label for label in labels)
    assert "Salzburg Wohnbau" not in joined
    assert "OÖ Wohnbau" not in joined
    assert "WAG Wohngebiete" not in joined


def test_austria_generated_source_defaults_use_broker_and_project_lanes_for_buy() -> None:
    specs = property_market_catalog.generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "buy",
            "location_query": "Vienna",
        },
        selected_platforms=(),
        principal_id="exec-property-at-buy-defaults",
        default_person_id="self",
        max_results=4,
    )

    platforms = {str(row["platform"]) for row in specs}

    assert "broker_direct_at" in platforms
    assert "developer_projects_at" in platforms


def test_germany_generated_source_defaults_use_live_buy_lanes_only() -> None:
    specs = property_market_catalog.generated_source_specs(
        preferences={
            "country_code": "DE",
            "language_code": "de",
            "listing_mode": "buy",
            "location_query": "Berlin",
        },
        selected_platforms=(),
        principal_id="exec-property-de-buy-defaults",
        default_person_id="self",
        max_results=4,
    )

    platforms = {str(row["platform"]) for row in specs}
    urls = [str(row["url"]) for row in specs]

    assert "core_portals_de" in platforms
    assert "wohnungsboerse_de" in platforms
    assert "new_build_de" in platforms
    assert "broker_direct_de" in platforms
    assert "corporate_landlords_de" not in platforms
    assert any("ohne-makler.net/immobilien/berlin/berlin/" in url for url in urls)
    assert any("neubaukompass.com/new-build-real-estate/berlin/" in url for url in urls)


def test_germany_auction_sources_require_buy_or_explicit_distressed_signal_mode() -> None:
    rent_specs = property_market_catalog.generated_source_specs(
        preferences={
            "country_code": "DE",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Berlin",
        },
        selected_platforms=("auctions_de", "zvg_de"),
        principal_id="exec-property-de-auctions-rent",
        default_person_id="self",
        max_results=3,
    )
    distressed_specs = property_market_catalog.generated_source_specs(
        preferences={
            "country_code": "DE",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Berlin",
            "include_distressed_sale_signals": True,
        },
        selected_platforms=("auctions_de",),
        principal_id="exec-property-de-auctions-distressed",
        default_person_id="self",
        max_results=3,
    )

    assert rent_specs == ()
    assert distressed_specs
    assert all(str(row["listing_mode"]) == "buy" for row in distressed_specs)


def test_property_search_location_matching_rejects_source_scope_only_location() -> None:
    hints = _property_search_location_hints({"country_code": "CR", "region_code": "puntarenas", "location_query": "Monteverde"})
    facts = product_service._property_facts_with_source_scope(
        facts={"provider_channel": "re_cr_mls"},
        source_url="https://re.cr/en/search?country=CR&q=Monteverde",
        source_label="RE.cr Costa Rica MLS | Costa Rica | Buy | Monteverde",
    )

    assert facts["source_scope_location"] == "Monteverde"
    assert facts["source_city"] == "Monteverde"
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://re.cr/en/listing/sparse-card",
        title="Mountain view home",
        summary="Sparse provider card.",
        property_facts=facts,
        country_code="CR",
        region_code="puntarenas",
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://re.cr/en/listing/monteverde-home",
        title="Mountain view home in Monteverde",
        summary="Sparse provider card with concrete listing locality in Monteverde.",
        property_facts=facts,
        country_code="CR",
        region_code="puntarenas",
    ) is True


def test_property_search_location_matching_rejects_concrete_cr_location_conflict() -> None:
    hints = _property_search_location_hints({"country_code": "CR", "region_code": "puntarenas", "location_query": "Monteverde"})

    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.re.cr/en/real-estate/heredia-costa-rica",
        title="Properties for sale and for rent in Heredia, Costa Rica",
        summary="Provider result page was queried from a Monteverde source scope.",
        property_facts={"source_scope_location": "Monteverde", "source_city": "Monteverde", "country_code": "CR", "region_code": "puntarenas"},
        country_code="CR",
        region_code="puntarenas",
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.realtor.com/international/cr/limon-talamanca-puerto-viejo-limon-310108049873/",
        title="Limón Talamanca Puerto Viejo, Limon 70403 Apartment for Sale",
        summary="Provider result page was queried from a Monteverde source scope.",
        property_facts={"source_scope_location": "Monteverde", "source_city": "Monteverde", "country_code": "CR", "region_code": "puntarenas"},
        country_code="CR",
        region_code="puntarenas",
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.re.cr/en/real-estate/lake-arenal",
        title="Lake Arenal Real Estate",
        summary="Provider result page was queried from a Monteverde source scope.",
        property_facts={"source_scope_location": "Monteverde", "source_city": "Monteverde", "country_code": "CR", "region_code": "puntarenas"},
        country_code="CR",
        region_code="puntarenas",
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.realtor.com/international/cr/bella-vista-nuevo-arenal-lake-arenal-guanacaste-310101836907/",
        title="Bella Vista Nuevo Arenal Lake Arenal Guanacaste House for Sale",
        summary="Provider result page was queried from a Monteverde source scope.",
        property_facts={"source_scope_location": "Monteverde", "source_city": "Monteverde", "country_code": "CR", "region_code": "puntarenas"},
        country_code="CR",
        region_code="puntarenas",
    ) is False


def test_property_search_location_matching_rejects_source_scope_postal_conflict() -> None:
    hints = _property_search_location_hints({"location_query": "1020 Vienna, 1030 Vienna, Wien"})

    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.willhaben.at/iad/object?adId=2098041582",
        title="Neubau 2 Zimmer Traum mit Balkon, 51,81 m², € 1.099,-, (3400 Klosterneuburg)",
        summary="Provider result page was queried from a Vienna source scope.",
        property_facts={"source_scope_location": "Wien", "source_city": "Wien"},
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://propertyquarry.com/tours/gefrderte-2-zimmer-mietwohnung-mit-balkon-und-carport-in-jagerberg-layout-first-828b943ae4",
        title="Geförderte 2 Zimmer Mietwohnung mit Balkon und Carport in Jagerberg",
        summary="Provider result page was queried from a Vienna source scope.",
        property_facts={"postal_name": "8091 Jagerberg", "source_scope_location": "Wien", "source_city": "Wien"},
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/oberoesterreich/gmunden/wohnung-mit-seeblick",
        title="Wohnung mit Seeblick in Gmunden",
        summary="Provider result page was queried from a Vienna source scope.",
        property_facts={"postal_name": "4810 Gmunden", "source_scope_location": "Wien", "source_city": "Wien"},
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.willhaben.at/iad/immobilien/d/einfamilienhaus/niederoesterreich/hollabrunn/familienhaus",
        title="Familienhaus in Hollabrunn",
        summary="Provider result page was queried from a Vienna source scope.",
        property_facts={"postal_name": "2020 Hollabrunn", "source_scope_location": "Wien", "source_city": "Wien"},
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://immobilien.derstandard.at/detail/wohnung-mieten-in-4020-linz",
        title="Wohnung mieten in 4020 Linz | 48.38 m² | 2 Zimmer",
        summary="Provider result page was queried from a Vienna source scope.",
        property_facts={"postal_name": "4020 Linz", "source_scope_location": "1020 Vienna", "source_city": "Vienna"},
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.immobilienscout24.at/expose/natters-top-05",
        title="Wohnhausanlage Osteräcker 01 - Natters | TOP 05",
        summary="Provider result page was queried from a Vienna source scope.",
        property_facts={"postal_name": "6161 Natters", "source_scope_location": "1020 Vienna", "source_city": "Vienna"},
    ) is False


def test_property_search_location_matching_rejects_non_vienna_title_even_with_vienna_source_scope() -> None:
    hints = _property_search_location_hints({"location_query": "Wien"})

    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/oberoesterreich/gmunden/seeblick",
        title="Moderne Wohnung mit Seeblick in Gmunden",
        summary="Provider result page was queried from a Vienna source scope.",
        property_facts={"source_scope_location": "Wien", "source_city": "Wien"},
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://www.willhaben.at/iad/immobilien/d/haus/niederoesterreich/hollabrunn/familienhaus",
        title="Familienhaus in Hollabrunn mit Garten",
        summary="Provider result page was queried from a Vienna source scope.",
        property_facts={"source_scope_location": "Wien", "source_city": "Wien"},
    ) is False


def test_property_search_location_matching_rejects_catalog_alias_conflict_before_source_scope() -> None:
    hints = _property_search_location_hints(
        {
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "1010 Vienna",
        }
    )

    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://familienwohnbau.at/wohnen-naehe-u4-huetteldorf",
        title="WOHNEN NÄHE U4 HÜTTELDORF | Familienwohnbau",
        summary="Provider result page was queried from a selected 1010 source scope.",
        property_facts={
            "source_scope_location": "1010 Vienna",
            "source_city": "Vienna",
            "country_code": "AT",
            "region_code": "vienna",
        },
        country_code="AT",
        region_code="vienna",
    ) is False
    assert _property_candidate_matches_requested_location(
        location_hints=hints,
        property_url="https://example.test/1010-inner-city",
        title="Innere Stadt apartment, 1010 Wien",
        summary="Sparse provider card.",
        property_facts={
            "source_scope_location": "1010 Vienna",
            "source_city": "Vienna",
            "country_code": "AT",
            "region_code": "vienna",
        },
        country_code="AT",
        region_code="vienna",
    ) is True


def test_property_search_location_hints_ignore_broad_austria_scope() -> None:
    assert _property_search_location_hints({"location_query": "Österreich"}) == ()
    assert _property_search_location_hints({"location_query": "All Austria"}) == ()
    assert _property_search_location_hints({"location_query": "Niederösterreich"}) == ("Niederösterreich",)


def test_property_distance_gate_records_relaxed_and_unknown_distances() -> None:
    relaxed_facts = {"nearest_supermarket_m": 420}

    assert product_service._property_apply_distance_gate(
        relaxed_facts,
        request_preferences={
            "max_distance_to_supermarket_m": 200,
            "max_distance_to_supermarket_importance": "important",
        },
        preference_key="max_distance_to_supermarket_m",
        fact_key="nearest_supermarket_m",
        label="supermarket",
    ) is True
    assert relaxed_facts["distance_relaxations_json"] == [
        {"label": "supermarket", "requested_m": 200, "actual_m": 420}
    ]

    unknown_facts: dict[str, object] = {}
    assert product_service._property_apply_distance_gate(
        unknown_facts,
        request_preferences={
            "max_distance_to_playground_m": 300,
            "max_distance_to_playground_importance": "must_have",
        },
        preference_key="max_distance_to_playground_m",
        fact_key="nearest_playground_m",
        label="playground",
    ) is True
    assert unknown_facts["distance_unknowns_json"] == [
        {"label": "playground", "requested_m": 300}
    ]

    outside_facts = {"nearest_library_m": 1200}
    assert product_service._property_apply_distance_gate(
        outside_facts,
        request_preferences={
            "max_distance_to_library_m": 300,
            "max_distance_to_library_importance": "must_have",
        },
        preference_key="max_distance_to_library_m",
        fact_key="nearest_library_m",
        label="Library",
    ) is False
    assert "distance_relaxations_json" not in outside_facts


def test_property_distance_gate_treats_non_hard_preferences_as_score_only() -> None:
    outside_facts = {"nearest_library_m": 1200}
    assert product_service._property_apply_distance_gate(
        outside_facts,
        request_preferences={
            "max_distance_to_library_m": 300,
            "max_distance_to_library_importance": "important",
        },
        preference_key="max_distance_to_library_m",
        fact_key="nearest_library_m",
        label="Library",
    ) is True
    assert "distance_relaxations_json" not in outside_facts

    soft_unknown_facts: dict[str, object] = {}
    assert product_service._property_apply_distance_gate(
        soft_unknown_facts,
        request_preferences={
            "max_distance_to_playground_m": 500,
            "max_distance_to_playground_importance": "nice_to_have",
        },
        preference_key="max_distance_to_playground_m",
        fact_key="nearest_playground_m",
        label="playground",
    ) is True
    assert soft_unknown_facts["distance_unknowns_json"] == [
        {"label": "playground", "requested_m": 500}
    ]


def test_property_distance_preference_score_adjustment_rewards_and_penalizes_soft_matches() -> None:
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/score-home-1234567890/"
    )

    def _with_exact_distance_evidence(
        facts: dict[str, object],
    ) -> dict[str, object]:
        return _attested_search_distance_facts(
            property_url=property_url,
            distances={
                str(key): int(value)
                for key, value in facts.items()
                if str(key).startswith("nearest_")
                and str(key).endswith("_m")
            },
        )

    positive_adjustment, positive_notes = product_service._property_distance_preference_score_adjustment(
        preferences={
            "max_distance_to_library_m": 500,
            "max_distance_to_library_importance": "important",
            "max_distance_to_playground_m": 800,
            "max_distance_to_playground_importance": "nice_to_have",
        },
        property_facts=_with_exact_distance_evidence(
            {
                "nearest_library_m": 240,
                "nearest_playground_m": 620,
            }
        ),
        property_url=property_url,
    )

    assert positive_adjustment > 0
    assert "library close by" in positive_notes
    assert "playground nearby" in positive_notes

    negative_adjustment, negative_notes = product_service._property_distance_preference_score_adjustment(
        preferences={
            "max_distance_to_library_m": 400,
            "max_distance_to_library_importance": "important",
            "max_distance_to_playground_m": 500,
            "max_distance_to_playground_importance": "nice_to_have",
        },
        property_facts=_with_exact_distance_evidence(
            {
                "nearest_library_m": 1800,
            }
        ),
        property_url=property_url,
    )

    assert negative_adjustment < 0
    assert "Nearest library is 1800 m away; your limit was 400 m." in negative_notes
    assert "playground distance missing" not in negative_notes

    unknown_facts: dict[str, object] = {}
    unknown_adjustment, unknown_notes = product_service._property_distance_preference_score_adjustment(
        preferences={
            "max_distance_to_playground_m": 500,
            "max_distance_to_playground_importance": "nice_to_have",
        },
        property_facts=unknown_facts,
    )

    assert unknown_adjustment == 0
    assert unknown_notes == ()
    assert unknown_facts["distance_unknowns_json"] == [{"label": "playground", "requested_m": 500}]

    avoid_adjustment, avoid_notes = product_service._property_distance_preference_score_adjustment(
        preferences={
            "max_distance_to_shopping_center_m": 500,
            "max_distance_to_shopping_center_importance": "avoid",
            "max_distance_to_theatre_m": 700,
            "max_distance_to_theatre_importance": "strong_wish",
        },
        property_facts=_with_exact_distance_evidence(
            {
                "nearest_shopping_center_m": 220,
                "nearest_theatre_m": 360,
            }
        ),
        property_url=property_url,
    )

    assert avoid_adjustment < 0
    assert "Nearest shopping center is 220 m away; you asked to keep it farther than 500 m." in avoid_notes
    assert "theatre within requested radius" in avoid_notes


def test_property_distance_preference_score_adjustment_tapers_to_zero_then_ramps_malus() -> None:
    property_url = (
        "https://www.willhaben.at/iad/immobilien/d/taper-home-1234567890/"
    )
    boost_adjustment, boost_notes = product_service._property_distance_preference_score_adjustment(
        preferences={
            "max_distance_to_playground_m": 500,
            "max_distance_to_playground_importance": "nice_to_have",
        },
        property_facts=_attested_search_distance_facts(
            property_url=property_url,
            distances={"nearest_playground_m": 180},
        ),
        property_url=property_url,
    )
    neutral_edge_adjustment, neutral_edge_notes = product_service._property_distance_preference_score_adjustment(
        preferences={
            "max_distance_to_playground_m": 500,
            "max_distance_to_playground_importance": "nice_to_have",
        },
        property_facts=_attested_search_distance_facts(
            property_url=property_url,
            distances={"nearest_playground_m": 500},
        ),
        property_url=property_url,
    )
    soft_malus_adjustment, soft_malus_notes = product_service._property_distance_preference_score_adjustment(
        preferences={
            "max_distance_to_playground_m": 500,
            "max_distance_to_playground_importance": "nice_to_have",
        },
        property_facts=_attested_search_distance_facts(
            property_url=property_url,
            distances={"nearest_playground_m": 720},
        ),
        property_url=property_url,
    )
    full_malus_adjustment, full_malus_notes = product_service._property_distance_preference_score_adjustment(
        preferences={
            "max_distance_to_playground_m": 500,
            "max_distance_to_playground_importance": "nice_to_have",
        },
        property_facts=_attested_search_distance_facts(
            property_url=property_url,
            distances={"nearest_playground_m": 1200},
        ),
        property_url=property_url,
    )

    assert boost_adjustment > 0
    assert boost_notes == ("playground nearby",)
    assert neutral_edge_adjustment == 0
    assert neutral_edge_notes == ("playground nearby",)
    assert soft_malus_adjustment < 0
    assert "Nearest playground is 720 m away; your limit was 500 m." in soft_malus_notes
    assert full_malus_adjustment == -3.0
    assert "Nearest playground is 1200 m away; your limit was 500 m." in full_malus_notes


def test_property_candidate_effective_fit_score_prefers_adjusted_rank_score() -> None:
    assert (
        product_service._property_candidate_effective_fit_score(
            assessment_fit_score=38,
            ranked_fit_score=51,
        )
        == 51.0
    )
    assert (
        product_service._property_candidate_effective_fit_score(
            assessment_fit_score=62,
            ranked_fit_score=51,
        )
        == 62.0
    )


def test_property_distance_gate_can_avoid_nearby_locations() -> None:
    too_close_facts = {"nearest_shopping_center_m": 220}
    assert product_service._property_apply_distance_gate(
        too_close_facts,
        request_preferences={
            "max_distance_to_shopping_center_m": 500,
            "max_distance_to_shopping_center_importance": "avoid_nearby",
        },
        preference_key="max_distance_to_shopping_center_m",
        fact_key="nearest_shopping_center_m",
        label="shopping center",
    ) is True
    assert too_close_facts["distance_avoidances_json"] == [
        {"label": "shopping center", "requested_m": 500, "actual_m": 220}
    ]

    far_enough_facts = {"nearest_shopping_center_m": 1400}
    assert product_service._property_apply_distance_gate(
        far_enough_facts,
        request_preferences={
            "max_distance_to_shopping_center_m": 500,
            "max_distance_to_shopping_center_importance": "avoid_nearby",
        },
        preference_key="max_distance_to_shopping_center_m",
        fact_key="nearest_shopping_center_m",
        label="shopping center",
    ) is True


def test_property_search_prefetch_listing_urls_records_timings_and_errors(monkeypatch) -> None:
    def _fake_listing_urls_for_source(*, source_url: str, source_spec: dict[str, object], force_refresh: bool):
        if source_spec.get("platform") == "bad":
            raise RuntimeError("fetch_failed")
        return (("https://example.com/listing-1",), {"status": "miss"})

    monkeypatch.setattr(product_service, "_property_scout_listing_urls_for_source", _fake_listing_urls_for_source)

    prefetched = product_service._property_search_prefetch_listing_urls(
        specs=[
            {"url": "https://example.com/good", "platform": "good", "provider_family": "core_portal"},
            {"url": "https://example.com/bad", "platform": "bad", "provider_family": "core_portal"},
        ],
        force_refresh=False,
    )

    good = prefetched[("good", "https://example.com/good")]
    bad = prefetched[("bad", "https://example.com/bad")]
    assert good["listing_urls"] == ("https://example.com/listing-1",)
    assert good["provider_cache_state"]["status"] == "miss"
    assert float(good["timing_ms"]["provider_fetch"]) >= 0.0
    assert bad["error"] == "fetch_failed"
    assert float(bad["timing_ms"]["provider_fetch"]) >= 0.0


def test_property_search_prefetch_listing_urls_emits_source_callbacks(monkeypatch) -> None:
    started: list[str] = []
    finished: list[tuple[str, bool]] = []

    def _fake_listing_urls_for_source(*, source_url: str, source_spec: dict[str, object], force_refresh: bool):
        if source_spec.get("platform") == "bad":
            raise RuntimeError("fetch_failed")
        return (("https://example.com/listing-1",), {"status": "miss"})

    monkeypatch.setattr(product_service, "_property_scout_listing_urls_for_source", _fake_listing_urls_for_source)

    product_service._property_search_prefetch_listing_urls(
        specs=[
            {"url": "https://example.com/good", "platform": "good", "provider_family": "core_portal"},
            {"url": "https://example.com/bad", "platform": "bad", "provider_family": "core_portal"},
        ],
        force_refresh=False,
        on_source_started=lambda source_spec: started.append(str(source_spec.get("platform") or "")),
        on_source_finished=lambda source_spec, payload: finished.append((str(source_spec.get("platform") or ""), bool(payload.get("error")))),
    )

    assert sorted(started) == ["bad", "good"]
    assert sorted(finished) == [("bad", True), ("good", False)]


def test_property_filter_feedback_patch_disables_filter_and_reruns_search(monkeypatch) -> None:
    principal_id = "exec-property-filter-feedback-patch"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Filter Feedback Office")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "max_distance_to_supermarket_m": 200,
            "max_distance_to_supermarket_importance": "important",
            "property_search_enabled": True,
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)
    prompt = service._prepare_notification_feedback_prompt(
        principal_id=principal_id,
        notification_kind="property_scout_filter_near_miss",
        person_id="self",
        domain="property_search",
        object_type="property_listing",
        object_id="https://www.willhaben.at/iad/object?adId=near-miss",
        source_ref="property-scout:near-miss",
        raw_signal_json={"failed_filter_key": "max_distance_to_supermarket_m"},
        interpreted_signal_json={},
        suggestion_options=[
            {
                "key": "disable_max_distance_to_supermarket_m",
                "label": "Disable supermarket radius",
                "event_type": "property_filter_disable_requested",
                "reply_text": "Noted. I disabled that one search filter and started a fresh search.",
                "property_search_preference_patch": {"max_distance_to_supermarket_m": None},
                "property_search_rerun": True,
            }
        ],
    )
    service._record_notification_feedback_prompt(
        principal_id=principal_id,
        prompt=prompt,
        delivery_channel="telegram",
        telegram_chat_ref="42",
        telegram_message_ids=["77"],
    )
    observed: dict[str, object] = {}

    def _fake_sync_direct_property_scout(
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["principal_id"] = principal_id
        observed["actor"] = actor
        observed["force_refresh"] = force_refresh
        observed["property_search_preferences"] = dict(property_search_preferences or {})
        return {"status": "processed", "listing_total": 2}

    monkeypatch.setattr(service, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    result = service.record_notification_feedback(
        principal_id=principal_id,
        notification_key=str(prompt["notification_key"]),
        feedback_key="disable_max_distance_to_supermarket_m",
        actor="telegram_test",
        chat_id="42",
    )

    assert result["status"] == "recorded"
    assert result["property_search_preference_patch_status"] == "patched"
    assert result["property_search_rerun_status"] == "processed"
    assert observed["principal_id"] == principal_id
    assert observed["actor"] == "telegram_filter_feedback"
    assert observed["force_refresh"] is True
    updated = client.app.state.container.onboarding.status(principal_id=principal_id)["property_search_preferences"]
    raw = updated["raw_preferences"]
    assert raw["max_distance_to_supermarket_m"] is None
    assert "max_distance_to_supermarket_importance" not in raw


def test_property_filter_feedback_patch_ignores_unsupported_keys() -> None:
    principal_id = "exec-property-filter-feedback-unsupported"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Filter Unsupported Office")
    service = ProductService(client.app.state.container)

    result = service._apply_property_search_feedback_patch(
        principal_id=principal_id,
        patch={"selected_platforms": [], "max_distance_to_playground_m": None},
    )

    assert result["status"] == "patched"
    assert result["patched_keys"] == ["max_distance_to_playground_m"]
    updated = client.app.state.container.onboarding.status(principal_id=principal_id)["property_search_preferences"]
    assert updated["raw_preferences"]["max_distance_to_playground_m"] is None
    assert "selected_platforms" in updated["raw_preferences"]


def test_property_filter_near_miss_feedback_buttons_fit_telegram_callback_limit(monkeypatch) -> None:
    principal_id = "exec-property-filter-near-miss-buttons"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Filter Button Office")
    monkeypatch.setenv(
        "EA_TELEGRAM_BOT_REGISTRY_JSON",
        json.dumps({"default": {"token": "telegram-token", "handle": "tibor_concierge_bot"}}),
    )
    client.app.state.container.tool_runtime.upsert_connector_binding(
        principal_id=principal_id,
        connector_name="telegram_identity",
        external_account_ref="1354554303",
        auth_metadata_json={"default_chat_ref": "1354554303", "bot_key": "default", "bot_handle": "tibor_concierge_bot"},
        scope_json={"assistant_surfaces": ["dm"]},
        status="enabled",
    )
    service = ProductService(client.app.state.container)
    sent: dict[str, object] = {}

    def _fake_send_telegram_message_for_principal(*args, **kwargs):
        sent.update(kwargs)
        return SimpleNamespace(chat_id="1354554303", message_ids=("7",))

    monkeypatch.setattr(product_service, "send_telegram_message_for_principal", _fake_send_telegram_message_for_principal)

    result = service._send_property_scout_filter_near_miss_telegram(
        principal_id=principal_id,
        actor="test",
        title="Near miss apartment",
        summary="Strong candidate",
        counterparty="Willhaben",
        property_url="https://www.willhaben.at/iad/object?adId=near-miss",
        source_ref="property-scout:near-miss",
        preference_person_id="self",
        failed_filter_key="max_distance_to_supermarket_m",
        failed_filter_label="supermarket radius",
        prefilter_score=86.0,
        requested_distance_m=500,
        observed_distance_m=951,
        observed_place_name="BILLA Praterstern",
    )

    assert result["status"] == "sent"
    assert result["requested_distance_m"] == 500.0
    assert result["observed_distance_m"] == 951.0
    assert result["observed_place_name"] == "BILLA Praterstern"
    assert "Nearest supermarket: BILLA Praterstern is 951 m away; your limit was 500 m." in str(sent["text"])
    inline_buttons = list(sent["inline_buttons"])
    callback_values = [
        str(callback_data)
        for row in inline_buttons
        for _label, callback_data in row
    ]
    assert callback_values
    assert all(len(value.encode("utf-8")) <= 64 for value in callback_values)
    assert any("|df_super|" in value for value in callback_values)
    assert any("|kf_super|" in value for value in callback_values)


def test_property_filter_near_miss_message_names_observed_distance() -> None:
    message = product_service._property_near_miss_filter_message(
        title="Near miss apartment",
        source_label="Willhaben",
        filter_label="supermarket radius",
        score=86.0,
        requested_distance_m=500,
        observed_distance_m=951,
        observed_place_name="BILLA Praterstern",
    )

    assert "Nearest supermarket: BILLA Praterstern is 951 m away; your limit was 500 m." in message


def test_property_filter_near_miss_message_omits_unverified_distance_value() -> None:
    message = product_service._property_near_miss_filter_message(
        title="Near miss apartment",
        source_label="Willhaben",
        filter_label="supermarket radius",
        score=86.0,
        requested_distance_m=500,
        observed_distance_m=None,
        observed_place_name="BILLA without confirmed distance",
    )

    assert "BILLA without confirmed distance" not in message
    assert "is 0 m away" not in message
    assert "your limit was 500 m" not in message


def test_property_filter_near_miss_sender_suppresses_location_conflicts(monkeypatch) -> None:
    principal_id = "exec-property-filter-near-miss-location-sender"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Filter Location Gate Office")
    monkeypatch.setenv(
        "EA_TELEGRAM_BOT_REGISTRY_JSON",
        json.dumps({"default": {"token": "telegram-token", "handle": "tibor_concierge_bot"}}),
    )
    client.app.state.container.tool_runtime.upsert_connector_binding(
        principal_id=principal_id,
        connector_name="telegram_identity",
        external_account_ref="1354554303",
        auth_metadata_json={"default_chat_ref": "1354554303", "bot_key": "default", "bot_handle": "tibor_concierge_bot"},
        scope_json={"assistant_surfaces": ["dm"]},
        status="enabled",
    )
    service = ProductService(client.app.state.container)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: sent.append(dict(kwargs)) or SimpleNamespace(chat_id="1354554303", message_ids=("7",)),
    )

    result = service._send_property_scout_filter_near_miss_telegram(
        principal_id=principal_id,
        actor="test",
        title="Wohnung mieten in 4020 Linz | 48.38 m2 | 2 Zimmer",
        summary="Outside Vienna.",
        counterparty="DER STANDARD Immobilien | Austria | Buy | 1020 Vienna",
        property_url="https://immobilien.derstandard.at/detail/wohnung-mieten-in-4020-linz",
        source_ref="property-scout:linz-near-miss",
        preference_person_id="self",
        failed_filter_key="min_area_m2",
        failed_filter_label="minimum area",
        prefilter_score=86.0,
        requested_location_hints=("1020 Vienna",),
        requested_country_code="AT",
        requested_region_code="vienna",
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    assert sent == []


def test_property_scout_hit_sender_suppresses_location_conflicts_and_opens_repair(monkeypatch) -> None:
    principal_id = "exec-property-hit-location-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Scout Hit Location Gate Office")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: sent.append(dict(kwargs)) or SimpleNamespace(chat_id="1354554303", message_ids=("7",)),
    )

    result = service._send_property_scout_hit_telegram(
        principal_id=principal_id,
        actor="test",
        title="WOHNEN NÄHE U4 HÜTTELDORF | Familienwohnbau",
        summary="Generic project page surfaced from a 1010 scope.",
        counterparty="Genossenschaften | Austria | Rent | 1010 Vienna | Familienwohnbau Angebote",
        account_email="",
        property_url="https://example.invalid/angebote/huetteldorf",
        source_ref="property-scout:huetteldorf-mismatch",
        assessment={"fit_score": 50.0, "recommendation": "review"},
        fit_score=50.0,
        preference_person_id="self",
        candidate_properties=(
            {
                "property_url": "https://example.invalid/angebote/huetteldorf",
                "listing_title": "WOHNEN NÄHE U4 HÜTTELDORF | Familienwohnbau",
                "summary": "Scout update candidate",
                "source_platform": "genossenschaften",
                "source_family": "housing_coop",
                "property_facts_json": {
                    "postal_name": "1140 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "district": "Hütteldorf",
                },
            },
        ),
        render_dossier=False,
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    assert sent == []
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks
    assert repair_tasks[0].priority == "urgent"
    assert dict(repair_tasks[0].input_json or {}).get("filter_key") == "location_scope"
    assert repair_tasks[0].assigned_operator_id == "ea_one_manager"
    assert repair_tasks[0].status in {"pending", "returned"}


def test_property_scout_hit_sender_suppresses_low_score_direct_calls(monkeypatch) -> None:
    principal_id = "exec-property-hit-low-score-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Scout Low Score Gate Office")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)
    monkeypatch.setenv("PROPERTYQUARRY_SCOUT_OUTBOUND_MIN_SCORE", "60")
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("low-score scout hit must not notify")),
    )

    result = service._send_property_scout_hit_telegram(
        principal_id=principal_id,
        actor="test",
        title="Wohnung mieten in 1010 Wien | 60 m² | 2 Zimmer | EUR 1.090",
        summary="2-Zimmer Wohnung im 1. Bezirk, 60 m2, Gesamtmiete EUR 1.090.",
        counterparty="Willhaben | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/low-score/",
        source_ref="property-scout:low-score-1010",
        assessment={"fit_score": 50.0, "recommendation": "review"},
        fit_score=50.0,
        preference_person_id="self",
        candidate_properties=(
            {
                "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/low-score/",
                "listing_title": "Wohnung mieten in 1010 Wien | 60 m² | 2 Zimmer | EUR 1.090",
                "summary": "2-Zimmer Wohnung im 1. Bezirk, 60 m2, Gesamtmiete EUR 1.090.",
                "source_platform": "willhaben",
                "source_family": "marketplace",
                "property_facts_json": {
                    "postal_name": "1010 Wien",
                    "location": "1010 Wien, Innere Stadt",
                    "street_address": "Kärntner Straße 12, 1010 Wien",
                    "exact_address": "Kärntner Straße 12, 1010 Wien",
                    "area_sqm": 60,
                    "rooms": 2,
                    "total_rent_eur": 1090,
                },
            },
        ),
        render_dossier=False,
        requested_location_hints=("1010 Vienna",),
        requested_country_code="AT",
        requested_region_code="vienna",
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "fit_below_outbound_threshold"
    assert result["fit_score"] == 50.0
    assert result["min_score"] == 60.0
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks == []


def test_property_scout_outbound_min_score_env_cannot_lower_sixty_floor(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_SCOUT_OUTBOUND_MIN_SCORE", "50")

    assert product_service._property_scout_outbound_notification_min_score() == 60.0

    monkeypatch.setenv("PROPERTYQUARRY_SCOUT_OUTBOUND_MIN_SCORE", "72")

    assert product_service._property_scout_outbound_notification_min_score() == 72.0


def test_property_source_scope_placeholder_detection_keeps_street_addresses_concrete() -> None:
    facts = {
        "postal_name": "1010 Wien",
        "location": "1010 Wien, Innere Stadt",
        "street_address": "Kärntner Straße 12, 1010 Wien",
        "exact_address": "Kärntner Straße 12, 1010 Wien",
        "source_scope_location": "1010 Vienna",
        "source_postal_code": "1010",
        "source_city": "Vienna",
    }

    assert product_service._property_candidate_has_concrete_location(facts)


def test_property_location_hint_queries_keep_postal_scope_placeholders_for_poi_backfill() -> None:
    facts = {
        "postal_name": "1010 Wien",
        "address": "1010 Wien",
        "source_scope_location": "1010 Vienna",
        "source_postal_code": "1010",
        "source_city": "Vienna",
    }

    assert product_service._property_research_location_hint_queries(facts=facts) == ("1010 Wien",)


def test_property_location_match_uses_listing_postal_evidence_over_source_scope() -> None:
    dirty_scope_facts = {
        "postal_name": "1010 Vienna",
        "source_scope_location": "1010 Vienna",
        "source_postal_code": "1010",
        "source_city": "Vienna",
    }
    title = "Wohnung mieten in 1220 Wien | 60 m² | 2 Zimmer | € 1.090 | DER STANDARD"
    summary = "2-Zimmer Wohnung mit Traumblick / UNO und U-Bahn ums Eck in 1220 Wien."

    enriched = product_service._property_enrich_facts_from_listing_text(
        facts=dirty_scope_facts,
        title=title,
        summary=summary,
        listing_mode="rent",
    )

    assert enriched["postal_name"] == "1220 Wien"
    assert [row["postal_code"] for row in enriched["listing_postal_evidence"]] == ["1220"]
    assert not _property_candidate_matches_requested_location(
        location_hints=("1010 Vienna",),
        property_url="https://www.derstandard.at/immobilien/wohnung-1220-wien",
        title=title,
        summary=summary,
        property_facts=dirty_scope_facts,
        country_code="AT",
        region_code="vienna",
    )
    assert _property_candidate_matches_requested_location(
        location_hints=("1220 Vienna",),
        property_url="https://www.derstandard.at/immobilien/wohnung-1220-wien",
        title=title,
        summary=summary,
        property_facts=dirty_scope_facts,
        country_code="AT",
        region_code="vienna",
    )


def test_property_location_match_uses_url_slug_postal_evidence_over_source_scope() -> None:
    dirty_scope_facts = {
        "postal_name": "1010 Vienna",
        "source_scope_location": "1010 Vienna",
        "source_postal_code": "1010",
        "source_city": "Vienna",
    }

    assert not _property_candidate_matches_requested_location(
        location_hints=("1010 Vienna",),
        property_url="https://www.raiffeisen-wohnbau.at/de/projects/id/1090-vienna/augasse-17/70?quot%3B%2Fn=",
        title="Augasse 17",
        summary="Provider card was returned from a selected 1010 source scope.",
        property_facts=dirty_scope_facts,
        country_code="AT",
        region_code="vienna",
    )
    assert _property_candidate_matches_requested_location(
        location_hints=("1090 Vienna",),
        property_url="https://www.raiffeisen-wohnbau.at/de/projects/id/1090-vienna/augasse-17/70?quot%3B%2Fn=",
        title="Augasse 17",
        summary="Provider card was returned from a selected 1090 source scope.",
        property_facts=dirty_scope_facts,
        country_code="AT",
        region_code="vienna",
    )

    assert not _property_candidate_matches_requested_location(
        location_hints=("90210 Beverly Hills",),
        property_url="https://example.test/apartment-10001-new-york",
        title="Apartment in 10001 New York | 70 m2 | USD 3,200",
        summary="Sunny apartment in 10001 New York.",
        property_facts={
            "postal_name": "90210 Beverly Hills",
            "source_scope_location": "90210 Beverly Hills",
            "source_postal_code": "90210",
            "source_city": "Beverly Hills",
        },
        country_code="US",
        region_code="ny",
    )
    assert _property_candidate_matches_requested_location(
        location_hints=("10001 New York",),
        property_url="https://example.test/apartment-10001-new-york",
        title="Apartment in 10001 New York | 70 m2 | USD 3,200",
        summary="Sunny apartment in 10001 New York.",
        property_facts={},
        country_code="US",
        region_code="ny",
    )


def test_property_postal_extraction_ignores_source_scope_locality_noise() -> None:
    assert product_service._property_postal_location_evidence("selected 1010 source scope") == ()
    assert product_service._property_postal_location_evidence("requested 1010 search area") == ()


def test_property_notification_location_evidence_kind_never_treats_source_scope_as_listing_truth() -> None:
    source_scope_facts = {
        "postal_name": "1010 Vienna",
        "source_scope_location": "1010 Vienna",
        "source_postal_code": "1010",
        "source_city": "Vienna",
    }

    source_scope_only = product_service._property_candidate_notification_location_evidence_kind(
        property_url="https://www.willhaben.at/iad/object?adId=1631373932",
        title="#W2 Moderne Schöne Zwei-Zimmer Wohnung",
        summary="Provider card from selected search scope.",
        property_facts=source_scope_facts,
    )
    assert source_scope_only == "source_scope_only"
    assert not product_service._property_candidate_notification_location_evidence_is_concrete(source_scope_only)

    listing_postal = product_service._property_candidate_notification_location_evidence_kind(
        property_url="https://www.derstandard.at/immobilien/wohnung-1220-wien",
        title="Wohnung mieten in 1220 Wien | 60 m² | 2 Zimmer",
        summary="2-Zimmer Wohnung mit Traumblick in 1220 Wien.",
        property_facts=source_scope_facts,
    )
    assert listing_postal == "listing_postal"
    assert product_service._property_candidate_notification_location_evidence_is_concrete(listing_postal)

    url_postal = product_service._property_candidate_notification_location_evidence_kind(
        property_url="https://www.raiffeisen-wohnbau.at/de/projects/id/1090-vienna/augasse-17/70",
        title="Augasse 17",
        summary="Provider card from selected search scope.",
        property_facts=source_scope_facts,
    )
    assert url_postal == "url_postal"
    assert product_service._property_candidate_notification_location_evidence_is_concrete(url_postal)

    street_address = product_service._property_candidate_notification_location_evidence_kind(
        property_url="https://example.test/listing",
        title="Wohnung beim Stephansplatz",
        summary="2-Zimmer Wohnung.",
        property_facts={**source_scope_facts, "street_address": "Kärntner Straße 12, 1010 Wien"},
    )
    assert street_address == "listing_concrete"
    assert product_service._property_candidate_notification_location_evidence_is_concrete(street_address)

    url_region = product_service._property_candidate_notification_location_evidence_kind(
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/demo-1631373932/",
        title="#W2 Moderne Schöne Zwei-Zimmer Wohnung",
        summary="Provider card from selected search scope.",
        property_facts=source_scope_facts,
    )
    assert url_region == "url_region"
    assert not product_service._property_candidate_notification_location_evidence_is_concrete(url_region)


def test_property_objective_review_boost_ignores_source_scope_location_without_listing_location() -> None:
    source_scope_only = product_service._property_scout_objective_review_boost(
        property_url="https://example.invalid/listing-with-source-scope-only",
        preview={"property_facts_json": {"source_scope_location": "1010 Vienna", "source_city": "Vienna"}},
    )
    listing_location = product_service._property_scout_objective_review_boost(
        property_url="https://example.invalid/listing-with-postal-name",
        preview={"property_facts_json": {"postal_name": "1010 Wien"}},
    )

    assert source_scope_only == 0.0
    assert listing_location == 2.0


@pytest.mark.parametrize(
    ("title", "summary", "property_url", "expected_postal"),
    [
        (
            "Wohnung mieten in 1200 Wien, Brigittenau | 81.98 m² | 3 Zimmer | € 1.649",
            "Stilvolle 3-Zimmer-Wohnung mit Garten & Terrasse im 20. Bezirk, Miete €1.649,- in 1200 Wien.",
            "https://immobilien.derstandard.at/detail/wohnung-mieten-in-1200-wien-brigittenau",
            "1200",
        ),
        (
            "Wohnung mieten in 1220 Wien | 60 m² | 2 Zimmer | € 1.090 | DER STANDARD",
            "2-Zimmer Wohnung mit Traumblick / UNO und U-Bahn ums Eck in 1220 Wien.",
            "https://immobilien.derstandard.at/detail/wohnung-mieten-in-1220-wien",
            "1220",
        ),
        (
            "#W2 Moderne Schöne Zwei-Zimmer Wohnung mit großem Ess- & Wohnbereich",
            "Moderne Zwei-Zimmer Wohnung mit Terrasse in Salzburg.",
            "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/demo",
            "",
        ),
        (
            "Einziehen sorgenfrei starten - Ihre Traumwohnung mit Balkon",
            "Unbefristeter Vertrag ab sofort verfügbar in Schärding.",
            "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/oberoesterreich/schaerding/demo",
            "",
        ),
        (
            "Traumwohnung mit Balkon in Schärding | 84 m² | 3 Zimmer",
            "Sofort verfügbare Mietwohnung in 4780 Schärding.",
            "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/oberoesterreich/schaerding/demo",
            "4780",
        ),
    ],
)
def test_property_location_match_rejects_reported_off_scope_austrian_hits(
    title: str,
    summary: str,
    property_url: str,
    expected_postal: str,
) -> None:
    dirty_scope_facts = {
        "postal_name": "1010 Vienna",
        "source_scope_location": "1010 Vienna",
        "source_postal_code": "1010",
        "source_city": "Vienna",
        "country_code": "AT",
        "region_code": "vienna",
    }

    enriched = product_service._property_enrich_facts_from_listing_text(
        facts=dirty_scope_facts,
        title=title,
        summary=summary,
        listing_mode="rent",
        property_url=property_url,
    )

    if expected_postal:
        assert any(
            str(row.get("postal_code") or "") == expected_postal
            for row in list(enriched.get("listing_postal_evidence") or [])
            if isinstance(row, dict)
        )
    else:
        assert enriched.get("postal_name") != "1010 Vienna"
        assert enriched.get("source_scope_location_placeholder_cleared") is True
    assert not _property_candidate_matches_requested_location(
        location_hints=("1010 Vienna",),
        property_url=property_url,
        title=title,
        summary=summary,
        property_facts=dirty_scope_facts,
        country_code="AT",
        region_code="vienna",
    )


def test_property_text_enrichment_does_not_infer_postal_from_price_only_sparse_card() -> None:
    enriched = product_service._property_enrich_facts_from_listing_text(
        facts={
            "source_scope_location": "1010 Vienna",
            "source_postal_code": "1010",
            "source_city": "Vienna",
        },
        title="#W2 Moderne Schone Zwei-Zimmer Wohnung",
        summary="78 m2, 2 Zimmer, Gesamtmiete EUR 1.450.",
        listing_mode="rent",
        property_url="https://example.invalid/propertyquarry/source-scope-only-sparse-city-card",
    )

    assert str(enriched.get("postal_name") or "").strip() == ""
    assert list(enriched.get("listing_postal_evidence") or []) == []


def test_property_url_location_probe_rejects_off_scope_willhaben_detail_paths() -> None:
    salzburg_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/demo-1631373932/"
    vienna_1220_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1220-donaustadt/demo-1631373932/"
    vienna_1010_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/demo-1631373932/"
    opaque_url = "https://www.willhaben.at/iad/object?adId=1631373932"

    assert _property_candidate_url_has_location_probe(salzburg_url)
    assert _property_candidate_url_has_location_probe(vienna_1220_url)
    assert _property_candidate_url_has_location_probe(vienna_1010_url)
    assert not _property_candidate_url_has_location_probe(opaque_url)
    assert not _property_candidate_url_has_exact_location_probe(salzburg_url)
    assert _property_candidate_url_has_exact_location_probe(vienna_1220_url)
    assert _property_candidate_url_has_exact_location_probe(vienna_1010_url)
    assert not _property_candidate_url_has_exact_location_probe(opaque_url)
    assert not _property_candidate_matches_requested_location(
        location_hints=("1010 Vienna",),
        property_url=salzburg_url,
        country_code="AT",
        region_code="vienna",
    )
    assert not _property_candidate_matches_requested_location(
        location_hints=("1010 Vienna",),
        property_url=vienna_1220_url,
        country_code="AT",
        region_code="vienna",
    )
    assert _property_candidate_matches_requested_location(
        location_hints=("1010 Vienna",),
        property_url=vienna_1010_url,
        country_code="AT",
        region_code="vienna",
    )


def test_property_scout_hit_sender_suppresses_dirty_source_scope_when_listing_postal_conflicts(monkeypatch) -> None:
    principal_id = "exec-property-hit-dirty-postal-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Dirty Postal Gate Office")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_platforms": ["derstandard_at"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: sent.append(dict(kwargs)) or SimpleNamespace(chat_id="1354554303", message_ids=("8",)),
    )

    title = "Wohnung mieten in 1220 Wien | 60 m² | 2 Zimmer | € 1.090 | DER STANDARD"
    summary = "2-Zimmer Wohnung mit Traumblick / UNO und U-Bahn ums Eck in 1220 Wien."
    result = service._send_property_scout_hit_telegram(
        principal_id=principal_id,
        actor="test",
        title=title,
        summary=summary,
        counterparty="DER STANDARD Immobilien | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://www.derstandard.at/immobilien/wohnung-1220-wien",
        source_ref="property-scout:derstandard-1220-dirty-scope",
        assessment={"fit_score": 50.0, "recommendation": "review"},
        fit_score=50.0,
        preference_person_id="self",
        candidate_properties=(
            {
                "property_url": "https://www.derstandard.at/immobilien/wohnung-1220-wien",
                "listing_title": title,
                "summary": summary,
                "source_platform": "derstandard_at",
                "source_family": "broker_portal",
                "property_facts_json": {
                    "postal_name": "1010 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "source_postal_code": "1010",
                    "source_city": "Vienna",
                    "price_display": "€ 1.090",
                },
            },
        ),
        requested_location_hints=("1010 Vienna",),
        requested_country_code="AT",
        requested_region_code="vienna",
        render_dossier=False,
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    assert sent == []
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks
    assert dict(repair_tasks[0].input_json or {}).get("filter_key") == "location_scope"


def test_property_scout_hit_sender_validates_matching_candidate_not_first_shortlist_item(monkeypatch) -> None:
    principal_id = "exec-property-hit-matching-candidate-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Matching Candidate Gate")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_districts": ["1010 Vienna"],
            "selected_platforms": ["derstandard_at"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: sent.append(dict(kwargs)) or SimpleNamespace(chat_id="1354554303", message_ids=("12",)),
    )

    current_title = "Wohnung mieten in 1220 Wien | 60 m² | 2 Zimmer | € 1.090 | DER STANDARD"
    current_summary = "2-Zimmer Wohnung mit Traumblick / UNO und U-Bahn ums Eck in 1220 Wien."
    result = service._send_property_scout_hit_telegram(
        principal_id=principal_id,
        actor="test",
        title=current_title,
        summary=current_summary,
        counterparty="DER STANDARD Immobilien | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://www.derstandard.at/immobilien/wohnung-1220-wien",
        source_ref="property-scout:derstandard-1220",
        assessment={"fit_score": 54.0, "recommendation": "review"},
        fit_score=54.0,
        preference_person_id="self",
        candidate_properties=(
            {
                "property_url": "https://www.derstandard.at/immobilien/wohnung-1010-wien",
                "listing_title": "Wohnung mieten in 1010 Wien | 55 m² | 2 Zimmer | € 1.250",
                "summary": "Zentrale Mietwohnung in 1010 Wien.",
                "property_facts": {
                    "postal_name": "1010 Wien",
                    "price_display": "€ 1.250",
                },
            },
            {
                "property_url": "https://www.derstandard.at/immobilien/wohnung-1220-wien",
                "listing_title": current_title,
                "summary": current_summary,
                "source_ref": "property-scout:derstandard-1220",
                "property_facts": {
                    "postal_name": "1010 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "source_postal_code": "1010",
                    "source_city": "Vienna",
                    "price_display": "€ 1.090",
                },
            },
        ),
        requested_location_hints=("1010 Vienna",),
        requested_country_code="AT",
        requested_region_code="vienna",
        render_dossier=False,
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    assert sent == []


def test_property_scout_hit_sender_suppresses_source_scope_only_exact_area_match(monkeypatch) -> None:
    principal_id = "exec-property-hit-source-scope-only-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Source Scope Only Gate")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_districts": ["1010 Vienna"],
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: sent.append(dict(kwargs)) or SimpleNamespace(chat_id="1354554303", message_ids=("9",)),
    )

    title = "#W2 Moderne Schöne Zwei-Zimmer Wohnung mit Terrasse"
    summary = "Wählen Sie aus 113.217 Angeboten. Immobilien suchen und finden auf willhaben."
    result = service._send_property_scout_hit_telegram(
        principal_id=principal_id,
        actor="test",
        title=title,
        summary=summary,
        counterparty="Willhaben | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/demo-1631373932/",
        source_ref="property-scout:willhaben-salzburg-dirty-scope",
        assessment={"fit_score": 54.0, "recommendation": "review"},
        fit_score=54.0,
        preference_person_id="self",
        candidate_properties=(
            {
                "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/demo-1631373932/",
                "listing_title": title,
                "summary": summary,
                "source_platform": "willhaben",
                "source_family": "core_portal",
                "property_facts_json": {
                    "postal_name": "1010 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "source_postal_code": "1010",
                    "source_city": "Vienna",
                    "price_display": "€ 1.190",
                },
            },
        ),
        requested_location_hints=("1010 Vienna",),
        requested_country_code="AT",
        requested_region_code="vienna",
        render_dossier=False,
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    assert sent == []
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks
    diagnostics = dict(repair_tasks[0].input_json or {}).get("diagnostics") or {}
    assert dict(repair_tasks[0].input_json or {}).get("filter_key") == "location_scope"
    assert diagnostics["location_hints"] == ["1010 Vienna"]


def test_property_scout_hit_sender_suppresses_source_scope_only_sparse_card(monkeypatch) -> None:
    principal_id = "exec-property-hit-source-scope-only-sparse"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Source Scope Sparse Gate")
    service = ProductService(client.app.state.container)
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: pytest.fail("source-scope-only cards must not notify Telegram"),
    )

    result = service._send_property_scout_hit_telegram(
        principal_id=principal_id,
        actor="test",
        title="Moderne 2-Zimmer Wohnung mit Terrasse",
        summary="Sparse provider card without a concrete listing location.",
        counterparty="Willhaben | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://www.willhaben.at/iad/object?adId=1631373932",
        source_ref="property-scout:source-scope-only-sparse",
        assessment={"fit_score": 82.0, "recommendation": "review"},
        fit_score=82.0,
        preference_person_id="self",
        candidate_properties=(
            {
                "property_url": "https://www.willhaben.at/iad/object?adId=1631373932",
                "listing_title": "Moderne 2-Zimmer Wohnung mit Terrasse",
                "summary": "Sparse provider card without a concrete listing location.",
                "source_platform": "willhaben",
                "source_family": "core_portal",
                "property_facts_json": {
                    "postal_name": "1010 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "source_postal_code": "1010",
                    "source_city": "Vienna",
                    "price_display": "€ 1.190",
                },
            },
        ),
        requested_location_hints=(),
        requested_country_code="AT",
        requested_region_code="vienna",
        render_dossier=False,
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks
    assert dict(repair_tasks[0].input_json or {}).get("filter_key") == "location_scope"


def test_property_generic_listing_page_detector_overrides_detail_shaped_url_for_search_count_snippets() -> None:
    generic_footer = "Wählen Sie aus 113.302 Angeboten. Immobilien suchen und finden auf willhaben."

    assert _property_candidate_is_generic_listing_page(
        property_url="https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1220-donaustadt?isNavigation=true",
        title="Mietwohnungen in 1220 Wien",
        summary=generic_footer,
        property_facts={
            "postal_name": "1220 Wien",
            "source_scope_location": "1220 Wien",
        },
    )

    assert _property_candidate_is_generic_listing_page(
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1220-donaustadt/category",
        title="Mietwohnungen in 1220 Wien",
        summary=generic_footer,
        property_facts={"postal_name": "1220 Wien"},
    )

    assert _property_candidate_is_generic_listing_page(
        property_url="https://www.willhaben.at.example.test/iad/immobilien/d/mietwohnungen/wien/demo-1545890000/",
        title="Haus im Grünen, (1220 Wien) - willhaben",
        summary=generic_footer,
        property_facts={"media_count": 30},
    )

    assert _property_candidate_is_generic_listing_page(
        property_url=(
            "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/../../"
            "mietwohnungen/wien/fake-1545890000"
        ),
        title="Haus im Grünen, (1220 Wien) - willhaben",
        summary=generic_footer,
        property_facts={"media_count": 30},
    )

    assert not _property_candidate_is_generic_listing_page(
        property_url=(
            "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1220-donaustadt/"
            "-im-gruenen-niedrigenergiehaus-top-ruhelage-grosse-freiflaechen-neuwertig-1545890000/"
        ),
        title=(
            "| IM GRÜNEN | NIEDRIGENERGIEHAUS| TOP-RUHELAGE | GROSSE FREIFLÄCHEN | NEUWERTIG |, "
            "(1220 Wien) - willhaben"
        ),
        summary=generic_footer,
        property_facts={"has_360": False, "has_floorplan": False, "media_count": 30},
    )

    assert not _property_candidate_is_generic_listing_page(
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/demo-1631373932/",
        title="Moderne Zwei-Zimmer Wohnung mit Terrasse",
        summary=generic_footer,
        property_facts={
            "postal_name": "1010 Vienna",
            "source_scope_location": "1010 Vienna",
            "source_postal_code": "1010",
            "source_city": "Vienna",
            "price_display": "€ 1.190",
            "listing_id": "1631373932",
        },
    )

    assert not _property_candidate_is_generic_listing_page(
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/demo-848017019/",
        title=(
            "PROVISIONSFREI - MIETE RUHELAGE SALZBURG PARSCH: "
            "Geräumige 79 m² 3-Zimmer-Wohnung, € 1.398,64, (5020 Salzburg) - willhaben"
        ),
        summary=generic_footer,
        property_facts={"rooms": 3, "has_floorplan": True},
    )


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/iad/immobilien/d/mietwohnungen/%252e%252e/%252e%252e/wien/fake-1545890000",
        "/iad/immobilien/d/mietwohnungen/%252f..%252f../wien/fake-1545890000",
        "/iad/immobilien/d/mietwohnungen/%255c..%255c../wien/fake-1545890000",
        "/iad/immobilien/d/mietwohnungen/..;/..;/wien/fake-1545890000",
        "/iad/immobilien/d/mietwohnungen/%2e%2e/%2e%2e/wien/fake-1545890000",
    ],
)
def test_property_generic_listing_page_detector_rejects_encoded_detail_path_bypasses(
    unsafe_path: str,
) -> None:
    assert _property_candidate_is_generic_listing_page(
        property_url=f"https://www.willhaben.at{unsafe_path}",
        title="Haus im Grünen, (1220 Wien) - willhaben",
        summary="Wählen Sie aus 113.302 Angeboten. Immobilien suchen und finden auf willhaben.",
        property_facts={"media_count": 30},
    )


@pytest.mark.parametrize(
    "malformed_url",
    [
        "https://[invalid/iad/immobilien/d/mietwohnungen/wien/fake-1545890000",
        "https://[::1/iad/immobilien/d/mietwohnungen/wien/fake-1545890000",
    ],
)
def test_property_generic_listing_page_detector_fails_closed_for_malformed_urls(
    malformed_url: str,
) -> None:
    assert _property_candidate_is_generic_listing_page(
        property_url=malformed_url,
        title="Haus im Grünen, (1220 Wien) - willhaben",
        summary="Wählen Sie aus 113.302 Angeboten. Immobilien suchen und finden auf willhaben.",
        property_facts={"media_count": 30},
    )


def test_property_source_scope_metadata_is_current_run_context_not_cached_listing_truth() -> None:
    stale_facts = {
        "source_scope_location": "1010 Vienna",
        "source_postal_code": "1010",
        "source_city": "Vienna",
        "rooms": 2,
    }

    refreshed = product_service._property_facts_with_source_scope(
        facts=stale_facts,
        source_url="https://www.willhaben.at/iad/immobilien/mietwohnungen?isNavigation=true&q=8010+Waltendorf",
        source_label="Willhaben | Austria | Rent | 8010 Waltendorf",
    )

    assert refreshed["source_scope_location"] == "8010 Waltendorf"
    assert refreshed["source_postal_code"] == "8010"
    assert refreshed["source_city"] == "Waltendorf"


def test_property_scout_hit_sender_uses_source_scope_as_notification_location_fallback(monkeypatch) -> None:
    principal_id = "exec-property-hit-source-scope-fallback"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Source Scope Fallback Gate")
    service = ProductService(client.app.state.container)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: sent.append(dict(kwargs)) or SimpleNamespace(chat_id="1354554303", message_ids=("9",)),
    )

    title = "#W2 Moderne Schöne Zwei-Zimmer Wohnung mit großem Ess- & Wohnbereich"
    summary = "Moderne Zwei-Zimmer Wohnung mit Terrasse in Salzburg."
    result = service._send_property_scout_hit_telegram(
        principal_id=principal_id,
        actor="test",
        title=title,
        summary=summary,
        counterparty="Willhaben | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/demo-1631373932/",
        source_ref="property-scout:willhaben-salzburg-dirty-scope",
        assessment={"fit_score": 54.0, "recommendation": "review"},
        fit_score=54.0,
        preference_person_id="self",
        candidate_properties=(
            {
                "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/demo-1631373932/",
                "listing_title": title,
                "summary": summary,
                "source_platform": "willhaben",
                "source_family": "core_portal",
                "property_facts_json": {
                    "postal_name": "1010 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "source_postal_code": "1010",
                    "source_city": "Vienna",
                    "price_display": "€ 1.190",
                },
            },
        ),
        requested_location_hints=(),
        requested_country_code="AT",
        requested_region_code="vienna",
        render_dossier=False,
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    assert sent == []
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks
    diagnostics = dict(repair_tasks[0].input_json or {}).get("diagnostics") or {}
    assert dict(repair_tasks[0].input_json or {}).get("filter_key") == "location_scope"
    assert diagnostics["location_hints"] == ["1010 Vienna"]


def test_property_scout_hit_email_suppresses_source_scope_only_exact_area_match(monkeypatch) -> None:
    principal_id = "cf-email:source-scope-email-gate@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Source Scope Email Gate")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_districts": ["1010 Vienna"],
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)
    monkeypatch.setattr(
        product_service,
        "send_property_match_email",
        lambda **kwargs: pytest.fail("source-scope-only mismatches must not send email"),
    )

    title = "#W2 Moderne Schöne Zwei-Zimmer Wohnung mit Terrasse"
    summary = "Wählen Sie aus 113.217 Angeboten. Immobilien suchen und finden auf willhaben."
    result = service._send_property_scout_hit_email(
        principal_id=principal_id,
        actor="test",
        title=title,
        summary=summary,
        counterparty="Willhaben | Austria | Rent | 1010 Vienna",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/demo-1631373932/",
        source_ref="property-scout:willhaben-salzburg-dirty-scope-email",
        assessment={"fit_score": 54.0, "recommendation": "review"},
        candidate_properties=(
            {
                "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/demo-1631373932/",
                "listing_title": title,
                "summary": summary,
                "source_platform": "willhaben",
                "source_family": "core_portal",
                "property_facts_json": {
                    "postal_name": "1010 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "source_postal_code": "1010",
                    "source_city": "Vienna",
                    "price_display": "€ 1.190",
                },
            },
        ),
        requested_location_hints=("1010 Vienna",),
        requested_country_code="AT",
        requested_region_code="vienna",
        render_dossier=False,
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks
    diagnostics = dict(repair_tasks[0].input_json or {}).get("diagnostics") or {}
    assert dict(repair_tasks[0].input_json or {}).get("filter_key") == "location_scope"
    assert diagnostics["location_hints"] == ["1010 Vienna"]


def test_property_scout_hit_email_uses_source_scope_as_notification_location_fallback(monkeypatch) -> None:
    principal_id = "exec-property-hit-email-source-scope-fallback"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Email Source Scope Fallback Gate")
    service = ProductService(client.app.state.container)

    title = "Wohnung mieten in 1220 Wien | 60 m² | 2 Zimmer | € 1.090 | DER STANDARD"
    summary = "2-Zimmer Wohnung mit Traumblick / UNO und U-Bahn ums Eck in 1220 Wien."
    result = service._send_property_scout_hit_email(
        principal_id=principal_id,
        actor="test",
        title=title,
        summary=summary,
        counterparty="DER STANDARD Immobilien | Austria | Rent | 1010 Vienna",
        property_url="https://immobilien.derstandard.at/detail/wohnung-mieten-in-1220-wien",
        source_ref="property-scout:derstandard-1220-dirty-scope",
        assessment={"fit_score": 50.0, "recommendation": "review"},
        candidate_properties=(
            {
                "property_url": "https://immobilien.derstandard.at/detail/wohnung-mieten-in-1220-wien",
                "listing_title": title,
                "summary": summary,
                "source_platform": "derstandard_at",
                "source_family": "broker_portal",
                "property_facts_json": {
                    "postal_name": "1010 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "source_postal_code": "1010",
                    "source_city": "Vienna",
                    "price_display": "€ 1.090",
                },
            },
        ),
        requested_location_hints=(),
        requested_country_code="AT",
        requested_region_code="vienna",
        render_dossier=False,
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"


def test_property_scout_hit_sender_suppresses_generic_pages_missing_concrete_facts(monkeypatch) -> None:
    principal_id = "exec-property-hit-generic-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Scout Hit Generic Gate Office")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: sent.append(dict(kwargs)) or SimpleNamespace(chat_id="1354554303", message_ids=("7",)),
    )

    result = service._send_property_scout_hit_telegram(
        principal_id=principal_id,
        actor="test",
        title="Familienwohnbau Angebote",
        summary="Ihr Immobilienmakler für Wien. Verkauf, Vermietung, Preis-Check.",
        counterparty="Genossenschaften | Austria | Rent | 1010 Vienna | Familienwohnbau Angebote",
        account_email="",
        property_url="https://example.invalid/immobilien/angebote",
        source_ref="property-scout:generic-offer-page",
        assessment={"fit_score": 50.0, "recommendation": "review"},
        fit_score=50.0,
        preference_person_id="self",
        candidate_properties=(
            {
                "property_url": "https://example.invalid/immobilien/angebote",
                "listing_title": "Familienwohnbau Angebote",
                "summary": "Ihr Immobilienmakler für Wien. Verkauf, Vermietung, Preis-Check.",
                "source_platform": "genossenschaften",
                "source_family": "housing_coop",
                "property_facts_json": {
                    "source_scope_location": "1010 Vienna",
                },
            },
        ),
        render_dossier=False,
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_generic_listing_page"
    assert sent == []
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks
    assert repair_tasks[0].priority == "urgent"
    assert repair_tasks[0].assigned_operator_id == "ea_one_manager"
    assert repair_tasks[0].status in {"pending", "returned"}


def test_property_scout_hit_sender_suppresses_architecture_competition_pages(monkeypatch) -> None:
    principal_id = "exec-property-hit-architecture-competition-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Scout Hit Architecture Competition Gate Office")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_platforms": ["genossenschaften"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: sent.append(dict(kwargs)) or SimpleNamespace(chat_id="1354554303", message_ids=("7",)),
    )

    result = service._send_property_scout_hit_telegram(
        principal_id=principal_id,
        actor="test",
        title="Ausschreibungen Architekturwettbewerbe",
        summary="Erhalten Sie alle Informationen zu den neuesten Architekturwettbewerben der Heimat Österreich!",
        counterparty="Genossenschaften | Austria | Rent | 1010 Vienna | Heimat Österreich",
        account_email="",
        property_url="https://example.invalid/ausschreibungen/architekturwettbewerbe",
        source_ref="property-scout:heimat-oesterreich-architecture-competition",
        assessment={"fit_score": 50.0, "recommendation": "review"},
        fit_score=50.0,
        preference_person_id="self",
        candidate_properties=(
            {
                "property_url": "https://example.invalid/ausschreibungen/architekturwettbewerbe",
                "listing_title": "Ausschreibungen Architekturwettbewerbe",
                "summary": "Erhalten Sie alle Informationen zu den neuesten Architekturwettbewerben der Heimat Österreich!",
                "source_platform": "genossenschaften",
                "source_family": "housing_coop",
                "property_facts_json": {
                    "source_scope_location": "1010 Vienna",
                },
            },
        ),
        render_dossier=False,
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_generic_listing_page"
    assert sent == []
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks
    assert repair_tasks[0].priority == "urgent"
    assert repair_tasks[0].assigned_operator_id == "ea_one_manager"
    assert repair_tasks[0].status in {"pending", "returned"}


def test_property_provider_repair_task_dedupes_across_transient_source_refs() -> None:
    principal_id = "exec-property-provider-repair-dedupe"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Repair Dedupe Office")
    service = ProductService(client.app.state.container)

    first = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://familienwohnbau.at/de/objekt/wohnen-naehe-u4-huetteldorf-e8fd5b2b",
        title="WOHNEN NÄHE U4 HÜTTELDORF | Familienwohnbau",
        source_url="https://familienwohnbau.at/de/objekt/wohnen-naehe-u4-huetteldorf-e8fd5b2b",
        source_label="Familienwohnbau",
        source_platform="familienwohnbau",
        source_family="housing_coop",
        filter_key="require_floorplan",
        diagnostics={"provider_host": "familienwohnbau.at"},
        source_ref="property-scout:run-a-candidate-1",
    )
    second = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://familienwohnbau.at/de/objekt/wohnen-naehe-u4-huetteldorf-e8fd5b2b",
        title="WOHNEN NÄHE U4 HÜTTELDORF | Familienwohnbau",
        source_url="https://familienwohnbau.at/de/objekt/wohnen-naehe-u4-huetteldorf-e8fd5b2b",
        source_label="Familienwohnbau",
        source_platform="familienwohnbau",
        source_family="housing_coop",
        filter_key="require_floorplan",
        diagnostics={"provider_host": "familienwohnbau.at"},
        source_ref="property-scout:run-b-candidate-9",
    )

    assert first["status"] == "opened"
    assert second["status"] == "existing"
    assert second["human_task_id"] == first["human_task_id"]
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert len(repair_tasks) == 1
    assert repair_tasks[0].assigned_operator_id == "ea_one_manager"


def test_property_provider_repair_subject_key_keeps_full_provider_path() -> None:
    service = ProductService.__new__(ProductService)

    willhaben_key = service._property_provider_repair_subject_key(
        property_url="propertyquarry://provider/willhaben/generic-listing-page",
    )
    costa_rica_key = service._property_provider_repair_subject_key(
        property_url="propertyquarry://provider/re_cr_mls/generic-listing-page",
    )

    assert "willhaben/generic-listing-page" in willhaben_key
    assert "re_cr_mls/generic-listing-page" in costa_rica_key
    assert willhaben_key != costa_rica_key


def test_property_provider_repair_copy_uses_propertyquarry_language() -> None:
    principal_id = "exec-property-provider-repair-copy"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Repair Copy Office")
    service = ProductService(client.app.state.container)

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://www.gesiba.at/wohnen/demo",
        title="GESIBA demo listing",
        source_url="https://www.gesiba.at/wohnen/demo",
        source_label="GESIBA",
        source_platform="gesiba",
        source_family="housing_coop",
        filter_key="require_floorplan",
        diagnostics={"provider_host": "www.gesiba.at"},
        source_ref="property-scout:copy-guard",
    )

    assert opened["status"] == "opened"
    assert opened["queue_lane"] == "repair"
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert len(repair_tasks) == 1
    task = repair_tasks[0]
    assert dict(task.input_json or {})["queue_lane"] == "repair"
    assert dict(task.input_json or {})["queue_max_attempts"] >= 1
    assert dict(task.input_json or {})["queue_timeout_seconds"] >= 30
    task_text = json.dumps(
        {
            "brief": task.brief,
            "why_human": task.why_human,
            "input_json": task.input_json,
        },
        ensure_ascii=False,
    )
    assert "PropertyQuarry provider repair" in task_text
    assert "EA Provider OODA" not in task_text

    events = [
        row
        for row in client.app.state.container.channel_runtime.list_recent_observations(
            limit=20,
            principal_id=principal_id,
        )
        if row.event_type == "property_provider_repair_task_created"
    ]
    assert len(events) == 1
    assert dict(events[0].payload or {})["queue_lane"] == "repair"
    assert "EA Provider OODA" not in json.dumps(events[0].payload or {}, ensure_ascii=False)


def test_existing_returned_provider_repair_records_receipt_for_new_run() -> None:
    principal_id = "exec-property-provider-repair-existing-receipt"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Repair Existing Receipt Office")
    service = ProductService(client.app.state.container)
    property_url = "https://wohnberatung.example.invalid/search"

    first = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url=property_url,
        title="Wohnberatung Wien",
        source_url=property_url,
        source_label="Wohnberatung Wien | Austria | Rent | Vienna",
        source_platform="wohnberatung_wien",
        source_family="public_housing",
        filter_key="source_fetch",
        diagnostics={"provider_host": "wohnberatung.example.invalid", "error": "HTTP Error 403: Forbidden"},
        run_id="first-repair-run",
    )
    assert first["repair_status"] == "returned"

    run_id = f"existing-repair-{uuid.uuid4().hex}"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "summary": {
                "sources": [
                    {
                        "source_url": property_url,
                        "source_label": "Wohnberatung Wien | Austria | Rent | Vienna",
                        "provider_repair_task_opened_total": 1,
                    }
                ]
            },
        }

    second = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url=property_url,
        title="Wohnberatung Wien",
        source_url=property_url,
        source_label="Wohnberatung Wien | Austria | Rent | Vienna",
        source_platform="wohnberatung_wien",
        source_family="public_housing",
        filter_key="source_fetch",
        diagnostics={"provider_host": "wohnberatung.example.invalid", "error": "HTTP Error 403: Forbidden"},
        run_id=run_id,
    )

    assert second["status"] == "existing"
    assert second["repair_status"] == "returned"
    snapshot = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    summary = dict(dict(snapshot or {}).get("summary") or {})
    assert summary["repair_resolved_total"] == 1
    assert summary["repair_receipts"][0]["run_id"] == run_id
    assert summary["sources"][0]["repair_status"] == "returned"


def test_repair_receipt_loads_registry_fallback_without_holding_run_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-repair-unlocked-load"
    run_id = f"repair-unlocked-{uuid.uuid4().hex}"
    persisted = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "in_progress",
        "summary": {"sources": []},
    }
    calls = {"load": 0}

    def _load_unlocked(**kwargs: object) -> dict[str, object]:
        calls["load"] += 1
        assert kwargs == {"run_id": run_id, "principal_id": principal_id}
        acquired = product_service._PROPERTY_SEARCH_RUN_LOCK.acquire(blocking=False)
        assert acquired, "durable run loading must happen outside the registry lock"
        product_service._PROPERTY_SEARCH_RUN_LOCK.release()
        return dict(persisted)

    monkeypatch.setattr(product_service, "_load_property_search_run_record", _load_unlocked)
    monkeypatch.setattr(product_service, "_store_property_search_run_record", lambda _record: True)
    service = ProductService.__new__(ProductService)
    task = SimpleNamespace(
        human_task_id="repair-unlocked-task",
        input_json={
            "run_id": run_id,
            "source_label": "Property source",
            "filter_key": "source_fetch",
        },
        returned_payload_json={},
    )

    try:
        service._record_property_search_run_repair_receipt(
            principal_id=principal_id,
            run_id=run_id,
            task=task,
            resolution="retry",
            reason="Provider recovered.",
            actor="test",
        )

        assert calls["load"] == 1
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            state = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id])
        receipts = list(dict(state.get("summary") or {}).get("repair_receipts") or [])
        assert receipts[0]["human_task_id"] == "human_task:repair-unlocked-task"
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.pop(run_id, None)


def test_old_willhaben_repair_receipt_does_not_contaminate_costa_rica_run() -> None:
    principal_id = "exec-property-provider-repair-country-isolation"
    run_id = f"costa-rica-repair-{uuid.uuid4().hex}"
    service = ProductService.__new__(ProductService)
    costa_rica_source = {
        "source_url": "https://www.encuentra24.com/costa-rica-es/bienes-raices-venta-de-propiedades",
        "source_label": "Encuentra24 | Costa Rica | Buy",
        "source_platform": "encuentra24_cr",
        "status": "failed",
        "error": "source fetch failed",
    }
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "summary": {"sources": [dict(costa_rica_source)]},
        }

    old_willhaben_task = SimpleNamespace(
        human_task_id="old-willhaben-repair",
        input_json={
            "run_id": "old-austria-run",
            "property_url": "propertyquarry://provider/willhaben/generic-listing-page",
            "source_url": "https://www.willhaben.at/iad/immobilien/eigentumswohnung",
            "source_label": "Willhaben | Austria | Buy",
            "source_platform": "willhaben",
            "filter_key": "generic_listing_page",
            "repair_workflow": "ea_provider_ooda",
        },
        returned_payload_json={},
    )

    service._record_property_search_run_repair_receipt(
        principal_id=principal_id,
        run_id=run_id,
        task=old_willhaben_task,
        resolution="suppressed_generic_listing_page",
        reason="old Austrian provider result",
        actor="ea_one_manager",
    )

    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        summary = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id]["summary"])
    assert summary == {"sources": [costa_rica_source]}


def test_historical_willhaben_receipt_is_removed_from_costa_rica_run_view() -> None:
    run_id = "historical-costa-rica-repair-run"
    task_ref = "human_task:old-willhaben-repair"
    service = ProductService.__new__(ProductService)

    projected = service._apply_property_search_run_repair_receipts(
        run_id=run_id,
        summary={
            "sources": [
                {
                    "source_url": "https://www.encuentra24.com/costa-rica-es/bienes-raices-venta-de-propiedades",
                    "source_label": "Encuentra24 | Costa Rica | Buy",
                    "source_platform": "encuentra24_cr",
                    "status": "repaired",
                    "original_error": "source fetch failed",
                    "provider_repair_task_opened_total": 1,
                    "provider_repair_tasks": [
                        {
                            "status": "returned",
                            "human_task_id": task_ref,
                            "queue_item_ref": task_ref,
                        }
                    ],
                    "repair_status": "returned",
                    "repair_resolution": "suppressed_generic_listing_page",
                }
            ],
            "provider_repair_tasks": [
                {
                    "status": "returned",
                    "human_task_id": task_ref,
                    "queue_item_ref": task_ref,
                }
            ],
            "repair_receipts": [
                {
                    "run_id": run_id,
                    "source_url": "propertyquarry://provider/willhaben/generic-listing-page",
                    "source_label": "Willhaben | Austria | Buy",
                    "source_platform": "willhaben",
                    "filter_key": "generic_listing_page",
                    "human_task_id": task_ref,
                    "resolution": "suppressed_generic_listing_page",
                }
            ],
            "repair_resolved_total": 1,
        },
    )

    assert projected["repair_receipts"] == []
    assert projected["repair_resolved_total"] == 0
    assert "provider_repair_tasks" not in projected
    source = dict(projected["sources"][0])
    assert source["status"] == "failed"
    assert source["error"] == "source fetch failed"
    assert "provider_repair_tasks" not in source
    assert "repair_status" not in source
    assert "repair_resolution" not in source


def test_process_property_provider_repair_tasks_scopes_to_requested_run(monkeypatch) -> None:
    target_task = SimpleNamespace(
        task_type="property_provider_repair_ooda",
        input_json={"run_id": "target-run"},
    )
    other_task = SimpleNamespace(
        task_type="property_provider_repair_ooda",
        input_json={"run_id": "other-run"},
    )
    service = ProductService.__new__(ProductService)
    service._container = SimpleNamespace(
        orchestrator=SimpleNamespace(
            list_human_tasks=lambda **_kwargs: [other_task, target_task],
        )
    )
    processed_run_ids: list[str] = []

    def _fake_auto_resolve(
        self,
        *,
        principal_id: str,
        task,
        actor: str,
    ) -> dict[str, object]:
        task_run_id = str(dict(task.input_json or {}).get("run_id") or "")
        processed_run_ids.append(task_run_id)
        return {"status": "resolved", "run_id": task_run_id}

    monkeypatch.setattr(
        ProductService,
        "_auto_resolve_property_provider_repair_task",
        _fake_auto_resolve,
    )

    result = service.process_property_provider_repair_tasks(
        principal_id="exec-property-provider-repair-run-scope",
        actor="test",
        run_id="target-run",
    )

    assert processed_run_ids == ["target-run"]
    assert result["resolved_total"] == 1
    assert result["deferred_total"] == 0
    assert result["resolved"] == [{"status": "resolved", "run_id": "target-run"}]


def test_process_property_provider_repair_tasks_scoped_limit_skips_unrelated_tasks(monkeypatch) -> None:
    unrelated_task = SimpleNamespace(
        task_type="property_provider_repair_ooda",
        input_json={"run_id": "other-run", "task_key": "unrelated"},
    )
    target_task = SimpleNamespace(
        task_type="property_provider_repair_ooda",
        input_json={"run_id": "target-run", "task_key": "target-first"},
    )
    second_target_task = SimpleNamespace(
        task_type="property_provider_repair_ooda",
        input_json={"run_id": "target-run", "task_key": "target-second"},
    )
    query_limits: list[int] = []

    def _list_human_tasks(*, limit: int, **_kwargs):
        query_limits.append(limit)
        return [unrelated_task, target_task, second_target_task][:limit]

    service = ProductService.__new__(ProductService)
    service._container = SimpleNamespace(
        orchestrator=SimpleNamespace(list_human_tasks=_list_human_tasks)
    )
    processed_task_keys: list[str] = []

    def _fake_auto_resolve(
        self,
        *,
        principal_id: str,
        task,
        actor: str,
    ) -> dict[str, object]:
        input_json = dict(task.input_json or {})
        task_key = str(input_json.get("task_key") or "")
        processed_task_keys.append(task_key)
        return {
            "status": "resolved",
            "run_id": str(input_json.get("run_id") or ""),
            "task_key": task_key,
        }

    monkeypatch.setattr(
        ProductService,
        "_auto_resolve_property_provider_repair_task",
        _fake_auto_resolve,
    )

    result = service.process_property_provider_repair_tasks(
        principal_id="exec-property-provider-repair-scoped-limit",
        actor="test",
        run_id="target-run",
        limit=1,
    )

    assert query_limits and 1 < query_limits[0] <= 2000
    assert processed_task_keys == ["target-first"]
    assert result["resolved_total"] == 1
    assert result["deferred_total"] == 0
    assert result["resolved"] == [
        {
            "status": "resolved",
            "run_id": "target-run",
            "task_key": "target-first",
        }
    ]


def test_property_provider_repair_auto_resolves_generic_listing_pages(monkeypatch) -> None:
    principal_id = "exec-property-provider-auto-resolve"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Repair Auto Resolve Office")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)

    class _Resp:
        ok = True
        text = """
        <html><head><title>Familienwohnbau Angebote</title></head>
        <body>
        Familienwohnbau Angebote
        Ihr Immobilienmakler für Wien. Verkauf, Vermietung, Preis-Check.
        </body></html>
        """

    monkeypatch.setattr(product_service.requests, "get", lambda *args, **kwargs: _Resp())

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://example.invalid/immobilien/angebote",
        title="Familienwohnbau Angebote",
        source_url="https://example.invalid/immobilien/angebote",
        source_label="Familienwohnbau",
        source_platform="familienwohnbau",
        source_family="housing_coop",
        filter_key="missing_price",
        diagnostics={"provider_host": "example.invalid"},
        source_ref="property-scout:generic-offer-page",
    )

    assert opened["status"] == "opened"
    tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert len(tasks) == 1
    assert tasks[0].status == "returned"
    assert tasks[0].resolution == "suppressed_generic_listing_page"


def test_property_provider_repair_auto_resolves_generic_listing_page_key_and_records_receipt(monkeypatch) -> None:
    principal_id = "exec-property-provider-generic-key-receipt"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Generic Repair Receipt Office")
    service = ProductService(client.app.state.container)
    property_url = "https://example.invalid/immobilien/angebote"
    run_id = f"generic-repair-{uuid.uuid4().hex}"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "summary": {
                "sources": [
                    {
                        "source_url": property_url,
                        "source_label": "Familienwohnbau | Austria | Rent | 1010 Vienna",
                        "provider_repair_task_opened_total": 1,
                    }
                ]
            },
        }

    class _Resp:
        ok = True
        text = """
        <html><head><title>Familienwohnbau Angebote</title></head>
        <body>
        Familienwohnbau Angebote
        Ihr Immobilienmakler für Wien. Verkauf, Vermietung, Preis-Check.
        </body></html>
        """

    monkeypatch.setattr(product_service.requests, "get", lambda *args, **kwargs: _Resp())

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url=property_url,
        title="Familienwohnbau Angebote",
        source_url=property_url,
        source_label="Familienwohnbau | Austria | Rent | 1010 Vienna",
        source_platform="familienwohnbau",
        source_family="housing_coop",
        filter_key="generic_listing_page",
        diagnostics={"provider_host": "example.invalid"},
        source_ref="property-scout:generic-offer-page",
        run_id=run_id,
    )

    assert opened["status"] == "opened"
    assert opened["repair_status"] == "returned"
    assert opened["resolution"] == "suppressed_generic_listing_page"
    snapshot = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    summary = dict(dict(snapshot or {}).get("summary") or {})
    assert summary["repair_resolved_total"] == 1
    assert summary["repair_receipts"][0]["filter_key"] == "generic_listing_page"
    assert summary["repair_receipts"][0]["resolution"] == "suppressed_generic_listing_page"
    assert summary["sources"][0]["repair_status"] == "returned"
    assert summary["sources"][0]["repair_resolution"] == "suppressed_generic_listing_page"


def test_property_provider_repair_auto_resolves_provider_scoped_generic_listing_page() -> None:
    principal_id = "exec-property-provider-scoped-generic-repair"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Scoped Generic Repair Office")
    service = ProductService(client.app.state.container)
    run_id = f"provider-scoped-generic-{uuid.uuid4().hex}"
    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen?q=1010+Vienna"
    example_url = "https://www.willhaben.at/iad/object?adId=755995091"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "summary": {
                "sources": [
                    {
                        "source_url": source_url,
                        "source_label": "Willhaben | Austria | Rent | 1010 Vienna",
                        "provider_repair_task_opened_total": 1,
                    }
                ]
            },
        }

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="propertyquarry://provider/willhaben/generic-listing-page",
        title="Willhaben generic listing-page extraction drift",
        source_url=source_url,
        source_label="Willhaben | Austria | Rent | 1010 Vienna",
        source_platform="willhaben",
        source_family="core_portal",
        filter_key="generic_listing_page",
        diagnostics={
            "provider_host": "www.willhaben.at",
            "source_url": source_url,
            "example_property_url": example_url,
            "title": "Wohnen im Zentrum von Graz - Uhrturmblick - willhaben",
            "postal_name": "8020 Graz",
            "source_scope_location": "1010 Vienna",
        },
        source_ref="property-provider:willhaben:generic-listing-page",
        run_id=run_id,
    )

    assert opened["status"] == "opened"
    assert opened["repair_status"] == "returned"
    assert opened["resolution"] == "suppressed_generic_listing_page"
    snapshot = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    summary = dict(dict(snapshot or {}).get("summary") or {})
    assert summary["repair_receipts"][0]["filter_key"] == "generic_listing_page"
    assert summary["repair_receipts"][0]["resolution"] == "suppressed_generic_listing_page"
    assert summary["repair_receipts"][0]["source_url"] == source_url


def test_property_provider_repair_auto_resolves_generic_listing_scope_from_diagnostics() -> None:
    principal_id = "exec-property-provider-generic-scope-diagnostics"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Generic Scope Repair Office")
    service = ProductService(client.app.state.container)

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://www.egw.at/suche/3789-2-zimmer-wohnung-mit-balkon-top-19",
        title="2-Zimmer-Wohnung mit Balkon in 2630 Ternitz",
        source_url="https://www.egw.at/suche/3789-2-zimmer-wohnung-mit-balkon-top-19",
        source_label="Genossenschaften | Austria | Rent | 1010 Vienna | EGW Immobiliensuche",
        source_platform="egw",
        source_family="housing_coop",
        filter_key="generic_listing_page",
        diagnostics={
            "title": "2-Zimmer-Wohnung mit Balkon in 2630 Ternitz",
            "postal_name": "2630 Ternitz",
            "source_scope_location": "1010 Vienna",
            "provider_host": "www.egw.at",
        },
        source_ref="property-scout:egw:generic-listing-page",
    )

    assert opened["status"] == "opened"
    assert opened["repair_status"] == "returned"
    assert opened["resolution"] == "suppressed_location_scope"


def test_property_provider_repair_uses_task_scope_over_current_preferences() -> None:
    principal_id = "exec-property-provider-task-scope-over-current"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Task Scope Repair Office")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1220 Vienna",
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://www.oesw.at/immobilienangebot/projektdetail/mhimmo/anzeigen/Wohnhaus/1220-wien-berresgasse.html",
        title="ProjektDetail",
        source_url="https://www.oesw.at/immobilienangebot/projektdetail/mhimmo/anzeigen/Wohnhaus/1220-wien-berresgasse.html",
        source_label="Genossenschaften | Austria | Rent | 1010 Vienna | ÖSW Sofort verfügbar",
        source_platform="oesw",
        source_family="housing_coop",
        filter_key="generic_listing_page",
        diagnostics={
            "title": "ProjektDetail",
            "source_scope_location": "1010 Vienna",
            "provider_host": "www.oesw.at",
        },
        source_ref="property-scout:oesw:generic-listing-page",
    )

    assert opened["status"] == "opened"
    assert opened["repair_status"] == "returned"
    assert opened["resolution"] == "suppressed_location_scope"


def test_property_provider_repair_auto_resolves_stale_run_without_claiming_patch() -> None:
    principal_id = "exec-property-provider-stale-run-fallback"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Stale Run Repair Office")
    service = ProductService(client.app.state.container)
    run_id = f"stale-run-fallback-{uuid.uuid4().hex}"

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url=f"propertyquarry://search-run/{run_id}",
        title="Search interrupted",
        source_url="https://www.willhaben.at/iad/immobilien/mietwohnungen?q=1010+Vienna",
        source_label="Willhaben | Austria | Rent | 1010 Vienna",
        source_platform="willhaben",
        source_family="core_portal",
        filter_key="run_interrupted_stale",
        diagnostics={"run_id": run_id, "failure_class": "run_interrupted_stale"},
        source_ref=f"property-search-run:{run_id}:stale",
        run_id=run_id,
    )

    assert opened["status"] == "opened"
    assert opened["repair_status"] == "returned"
    assert opened["resolution"] == "stale_run_restart_required"


def test_property_provider_repair_rebuilds_missing_packet_from_exact_saved_brief(
    monkeypatch,
) -> None:
    principal_id = "exec-property-provider-missing-packet-rebuild"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Missing Packet Repair Office")
    service = ProductService(client.app.state.container)
    run_id = f"missing-packet-parent-{uuid.uuid4().hex}"
    parent_state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "selected_platforms": ["willhaben"],
        },
        force_refresh=False,
    )
    observed: dict[str, object] = {}

    def _start_replacement(
        self,
        *,
        principal_id: str,
        selected_platforms: tuple[str, ...],
        property_search_preferences: dict[str, object],
        max_results_per_source: int | None,
        parent_run_id: str = "",
    ) -> dict[str, object]:
        observed.update(
            {
                "principal_id": principal_id,
                "selected_platforms": selected_platforms,
                "property_search_preferences": dict(property_search_preferences),
                "max_results_per_source": max_results_per_source,
                "parent_run_id": parent_run_id,
            }
        )
        return {"run_id": "replacement-run-1"}

    monkeypatch.setattr(
        ProductService,
        "_start_property_search_repair_replacement_run",
        _start_replacement,
    )

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url=f"propertyquarry://research-packet/{run_id}/missing-candidate",
        title="Missing property page",
        source_url="/app/shortlist",
        source_label="Property page",
        source_platform="propertyquarry",
        source_family="research_packet",
        filter_key="research_packet_missing",
        diagnostics={
            "run_id": run_id,
            "candidate_ref": "missing-candidate",
            "failure_class": "research_packet_missing",
        },
        source_ref=f"property-research-packet:{run_id}:missing-candidate",
        run_id=run_id,
    )

    assert opened["status"] == "opened"
    assert opened["repair_status"] == "deferred"
    assert opened["reason"] == "research_packet_recovery_context_missing"

    monkeypatch.setitem(product_service._PROPERTY_SEARCH_RUN_REGISTRY, run_id, parent_state)
    repeated = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url=f"propertyquarry://research-packet/{run_id}/missing-candidate",
        title="Missing property page",
        source_url="/app/shortlist",
        source_label="Property page",
        source_platform="propertyquarry",
        source_family="research_packet",
        filter_key="research_packet_missing",
        diagnostics={
            "run_id": run_id,
            "candidate_ref": "missing-candidate",
            "failure_class": "research_packet_missing",
        },
        source_ref=f"property-research-packet:{run_id}:missing-candidate",
        run_id=run_id,
    )
    assert repeated["status"] == "existing"
    assert repeated["repair_status"] == "returned"
    assert repeated["resolution"] == "research_packet_rebuild_started"
    assert repeated["replacement_run_id"] == "replacement-run-1"
    assert observed["principal_id"] == principal_id
    assert observed["parent_run_id"] == run_id
    assert observed["selected_platforms"] == ("willhaben",)
    assert dict(observed["property_search_preferences"])["location_query"] == "1020 Vienna"


def test_property_provider_repair_missing_packet_fails_closed_without_saved_brief(
    monkeypatch,
) -> None:
    principal_id = "exec-property-provider-missing-packet-no-context"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Missing Packet No Context Office")
    service = ProductService(client.app.state.container)
    monkeypatch.setattr(
        ProductService,
        "list_property_search_runs",
        lambda self, *, principal_id, limit=8: [],
    )

    def _unexpected_start(*args, **kwargs):
        raise AssertionError("replacement search must not start without a persisted brief")

    monkeypatch.setattr(
        ProductService,
        "_start_property_search_repair_replacement_run",
        _unexpected_start,
    )

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="propertyquarry://research-packet/unknown/no-context-candidate",
        title="Missing property page",
        source_url="/app/shortlist",
        source_label="Property page",
        source_platform="propertyquarry",
        source_family="research_packet",
        filter_key="research_packet_missing",
        diagnostics={
            "candidate_ref": "no-context-candidate",
            "failure_class": "research_packet_missing",
        },
        source_ref="property-research-packet:unknown:no-context-candidate",
    )

    assert opened["status"] == "opened"
    assert opened["repair_status"] == "deferred"
    assert opened["reason"] == "research_packet_recovery_context_missing"


def test_property_provider_repair_auto_resolves_walkthrough_without_retrying_render() -> None:
    principal_id = "exec-property-provider-walkthrough-fallback"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Walkthrough Repair Office")
    service = ProductService(client.app.state.container)

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://www.willhaben.at/iad/object?adId=1530201253",
        title="Attraktive Wohnung im 2. Bezirk",
        source_url="https://www.willhaben.at/iad/object?adId=1530201253",
        source_label="Willhaben | Austria | Rent | 1010 Vienna",
        source_platform="willhaben",
        source_family="core_portal",
        filter_key="walkthrough_video",
        diagnostics={
            "raw_status": "failed",
            "failure_reason": "onemin_segment_subprocess_timeout",
            "video_url_present": False,
        },
        source_ref="property-tour:walkthrough-video",
    )

    assert opened["status"] == "opened"
    assert opened["repair_status"] == "returned"
    assert opened["resolution"] == "walkthrough_video_auto_generation_disabled"


def test_property_provider_repair_does_not_cross_resolve_floorplan_into_location_scope(monkeypatch) -> None:
    principal_id = "exec-property-provider-floorplan-semantic-fence"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Repair Semantic Fence Office")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_platforms": ["kalandra"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)

    class _Resp:
        ok = True
        text = """
        <html><head><title>Wohnung in 1160 Wien</title></head>
        <body>
        Lage: 1160 Wien
        2 Zimmer
        58 m2
        EUR 1.250
        </body></html>
        """

    monkeypatch.setattr(product_service.requests, "get", lambda *args, **kwargs: _Resp())

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://example.invalid/listing/1160-no-floorplan",
        title="Wohnung in 1160 Wien",
        source_url="https://example.invalid/search",
        source_label="Kalandra | Austria | Rent | 1010 Vienna",
        source_platform="kalandra",
        source_family="core_portal",
        filter_key="require_floorplan",
        diagnostics={"provider_host": "example.invalid"},
        source_ref="property-scout:kalandra-floorplan",
    )

    assert opened["status"] == "opened"
    tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert len(tasks) == 1
    task = service._assign_property_provider_repair_task(
        principal_id=principal_id,
        task=tasks[0],
        actor="ea_one_manager",
        operator_id="ea_one_manager",
    )
    result = service._auto_resolve_property_provider_repair_task(
        principal_id=principal_id,
        task=task,
        actor="ea_one_manager",
    )

    refreshed = [
        row
        for row in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if row.task_type == "property_provider_repair_ooda"
    ][0]
    assert result["status"] == "deferred"
    assert refreshed.status == "pending"
    assert refreshed.resolution == ""


def test_property_provider_repair_snapshot_uses_generic_postal_evidence(monkeypatch) -> None:
    principal_id = "exec-property-provider-repair-generic-postal"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Repair Generic Postal Office")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_districts": ["1010 Vienna"],
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = ProductService(client.app.state.container)

    class _Resp:
        ok = True
        text = """
        <html><head><title>Moderne Wohnung mit Terrasse</title></head>
        <body>
        Lage: 5020 Salzburg
        2 Zimmer
        58 m2
        EUR 1.250
        </body></html>
        """

    monkeypatch.setattr(product_service.requests, "get", lambda *args, **kwargs: _Resp())

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://example.invalid/listing/opaque-123",
        title="Moderne Wohnung mit Terrasse",
        source_url="https://example.invalid/search",
        source_label="Willhaben | Austria | Rent | 1010 Vienna",
        source_platform="willhaben",
        source_family="core_portal",
        filter_key="location_scope",
        diagnostics={
            "provider_host": "example.invalid",
            "source_scope_location": "1010 Vienna",
            "source_postal_code": "1010",
            "source_city": "Vienna",
        },
        source_ref="property-scout:generic-postal-repair",
    )

    assert opened["status"] == "opened"
    assert opened["repair_status"] == "returned"
    assert opened["resolution"] == "suppressed_location_scope"
    refreshed = [
        row
        for row in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if row.task_type == "property_provider_repair_ooda"
    ][0]
    assert refreshed.status == "returned"
    assert refreshed.resolution == "suppressed_location_scope"


def test_property_search_source_fetch_failure_opens_provider_repair_task(monkeypatch) -> None:
    principal_id = "exec-property-source-fetch-repair"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Source Fetch Repair Office")
    service = ProductService(client.app.state.container)

    monkeypatch.setattr(
        product_service,
        "_merged_property_scout_source_specs",
        lambda **kwargs: [
            {
                "url": "https://wohnberatung.example.invalid/search",
                "label": "Wohnberatung Wien | Austria | Rent | Vienna",
                "platform": "wohnberatung_wien",
                "provider_family": "public_housing",
                "country_code": "AT",
                "max_results": 4,
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("wohnberatung_wien", "https://wohnberatung.example.invalid/search"): {
                "error": "HTTP Error 403: Forbidden",
                "listing_urls": [],
                "provider_cache_state": {"status": "failed", "cache_key": "wohnberatung:vienna"},
            }
        },
    )
    monkeypatch.setattr(
        ProductService,
        "_warm_property_public_preview_cache_for_sources",
        lambda self, **kwargs: {},
    )

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("wohnberatung_wien",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Vienna",
        },
        max_results_per_source=2,
        force_refresh=True,
    )

    assert result["failed_total"] == 1
    assert result["provider_repair_task_opened_total"] == 1
    assert result["sources"][0]["provider_repair_task_opened_total"] == 1
    assert result["sources"][0]["error"] == "HTTP Error 403: Forbidden"
    tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert len(tasks) == 1
    assert tasks[0].priority == "urgent"
    assert tasks[0].assigned_operator_id == "ea_one_manager"
    repair_input = dict(tasks[0].input_json or {})
    assert repair_input["filter_key"] == "source_fetch"
    assert repair_input["diagnostics"]["error"] == "HTTP Error 403: Forbidden"


def test_property_search_sources_resolved_preseeds_queued_source_rows(monkeypatch) -> None:
    principal_id = "exec-property-source-progress-queue"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Source Progress Queue Office")
    service = ProductService(client.app.state.container)

    specs = [
        {
            "url": "https://willhaben.example.invalid/search",
            "label": "Willhaben | Austria | Rent | Vienna",
            "platform": "willhaben",
            "provider_family": "classifieds",
            "country_code": "AT",
            "max_results": 4,
        },
        {
            "url": "https://immmo.example.invalid/search",
            "label": "immmo | Austria | Rent | Vienna",
            "platform": "immmo",
            "provider_family": "classifieds",
            "country_code": "AT",
            "max_results": 4,
        },
        {
            "url": "https://derstandard.example.invalid/search",
            "label": "DER STANDARD Immobilien | Austria | Rent | Vienna",
            "platform": "derstandard",
            "provider_family": "portal",
            "country_code": "AT",
            "max_results": 4,
        },
        {
            "url": "https://immoscout.example.invalid/search",
            "label": "ImmoScout24 | Austria | Rent | Vienna",
            "platform": "immoscout_at",
            "provider_family": "portal",
            "country_code": "AT",
            "max_results": 4,
        },
    ]

    monkeypatch.setattr(product_service, "_merged_property_scout_source_specs", lambda **kwargs: list(specs))
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda rows: list(rows))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            (str(spec["platform"]).strip().lower(), str(spec["url"]).strip()): {
                "listing_urls": [],
                "provider_cache_state": {
                    "status": "hit",
                    "cache_key": f"{spec['platform']}:vienna",
                },
            }
            for spec in specs
        },
    )
    monkeypatch.setattr(
        ProductService,
        "_warm_property_public_preview_cache_for_sources",
        lambda self, **kwargs: {"worker_concurrency": 4},
    )

    events: list[dict[str, object]] = []
    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben", "immmo", "derstandard", "immoscout_at"),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Vienna",
        },
        max_results_per_source=2,
        force_refresh=True,
        progress_callback=lambda **payload: events.append(json.loads(json.dumps(payload, default=str))),
    )

    sources_resolved = next(item for item in events if item["step"] == "sources_resolved")
    source_rows = [dict(row) for row in list(dict(sources_resolved.get("summary_updates") or {}).get("sources") or []) if isinstance(row, dict)]

    assert len(source_rows) == int(result["source_variant_total"])
    assert len(source_rows) >= 3
    assert all(str(row.get("status") or "").strip() == "queued" for row in source_rows)
    assert {
        str(row.get("source_label") or "").strip()
        for row in source_rows
    }.issubset(
        {
        "Willhaben",
        "immmo",
        "DER STANDARD Immobilien",
        "ImmoScout24",
        }
    )
    assert int(result["sources_total"]) == int(result["source_variant_total"]) == len(source_rows)
    source_completed_events = [item for item in events if item.get("step") == "source_completed"]
    assert source_completed_events
    assert all(
        "listing_total" not in dict(item.get("summary_updates") or {})
        for item in source_completed_events
    )


def test_property_search_source_completed_progress_does_not_overwrite_listing_total(monkeypatch) -> None:
    principal_id = "exec-property-source-progress-listing-total"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Source Progress Listing Total Office")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"
    listing_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/listing-total-guard/"
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
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": [listing_url],
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:listing-total-guard"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_scout_page_preview_with_timeout",
        lambda property_url, *, prefer_fast=False: {
            "listing_id": "listing-total-guard",
            "title": "Mietwohnung in 1020 Wien mit Balkon",
            "summary": "78 m2, 3 Zimmer, Gesamtmiete EUR 1.650, Balkon.",
            "property_facts_json": {
                "postal_name": "1020 Wien",
                "area_sqm": 78,
                "rooms": 3,
                "total_rent_eur": 1650,
                "has_balcony": True,
            },
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {
            "fit_score": 72.0,
            "recommendation": "review",
            "match_reasons_json": [],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
        },
    )
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})

    events: list[dict[str, object]] = []
    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "min_match_score": 0,
            "require_floorplan": False,
        },
        max_results_per_source=1,
        force_refresh=True,
        progress_callback=lambda **payload: events.append(json.loads(json.dumps(payload, default=str))),
    )

    assert result["listing_total"] == 1
    source_completed_events = [item for item in events if item.get("step") == "source_completed"]
    assert source_completed_events
    assert all(
        "listing_total" not in dict(item.get("summary_updates") or {})
        for item in source_completed_events
    )


def test_scheduler_property_results_finalize_processes_provider_repair_tasks(monkeypatch) -> None:
    principal_id = "exec-property-provider-repair-scheduler"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Repair Scheduler Office")
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *args, **kwargs: None))
    app_runner = importlib.import_module("app.runner")
    monkeypatch.setattr(app_runner, "_scheduler_property_scout_principal_ids", lambda container: (principal_id,))
    reconcile_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        ProductService,
        "reconcile_property_search_results_delivery",
        lambda self, limit=40, allow_notifications=True: reconcile_calls.append(
            {"limit": limit, "allow_notifications": allow_notifications}
        )
        or {"attempted": 0, "finalized": 0, "emailed": 0, "pending": 0},
    )
    monkeypatch.setattr(
        ProductService,
        "process_property_provider_repair_tasks",
        lambda self, *, principal_id, actor, limit=40: {
            "generated_at": product_service._now_iso(),
            "resolved_total": 1 if principal_id == "exec-property-provider-repair-scheduler" else 0,
            "deferred_total": 2 if principal_id == "exec-property-provider-repair-scheduler" else 0,
            "resolved": [],
        },
    )

    summary = app_runner._run_scheduler_property_results_finalize(client.app.state.container, SimpleNamespace(exception=lambda *a, **k: None))

    assert reconcile_calls == [{"limit": 40, "allow_notifications": False}]
    assert summary["repair_resolved_total"] == 1
    assert summary["repair_deferred_total"] == 2


def test_scheduler_property_search_recovery_adopts_stale_in_progress_runs(monkeypatch) -> None:
    principal_id = "exec-property-search-recovery-scheduler"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Search Recovery Scheduler Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_STALE_SECONDS", "60")
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *args, **kwargs: None))
    app_runner = importlib.import_module("app.runner")
    monkeypatch.setattr(app_runner, "_scheduler_property_scout_principal_ids", lambda container: (principal_id,))
    service = product_service.build_product_service(client.app.state.container)
    replacement_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        ProductService,
        "_start_property_search_repair_replacement_run",
        lambda self, **kwargs: replacement_calls.append(dict(kwargs)) or {"run_id": "scheduler-stale-repair"},
    )
    run_id = "scheduler-stale-run"
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "location_query": "1010 Vienna",
            "max_results_per_source": 1,
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
        force_refresh=False,
    )
    state["status"] = "in_progress"
    state["current_step"] = "source_previewing"
    state["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    state["events"] = [
        {
            "at": state["updated_at"],
            "step": "source_previewing",
            "status": "in_progress",
            "message": "Reviewing candidate 12 of 19 for Willhaben.",
        }
    ]
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)
    product_service._store_property_search_run_record(dict(state))

    summary = app_runner._run_scheduler_property_search_recovery(
        client.app.state.container,
        SimpleNamespace(exception=lambda *a, **k: None),
    )

    assert summary["stale_total"] == 1
    assert summary["repaired"] == 1
    assert summary["replacement_started"] == 1
    assert replacement_calls
    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    assert status["status"] == "failed"
    assert status["summary"]["repair_replacement_run_id"] == "scheduler-stale-repair"
    assert any(event["step"] == "run_repair_queued" for event in status["events"])
    assert replacement_calls[0]["max_results_per_source"] is None


def test_property_search_recovery_picks_up_stale_replacement_run(monkeypatch) -> None:
    principal_id = "exec-property-search-recovery-replacement"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Search Recovery Replacement Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_STALE_SECONDS", "3600")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_REPLACEMENT_RUN_STALE_SECONDS", "60")
    service = product_service.build_product_service(client.app.state.container)
    monkeypatch.setattr(ProductService, "_best_effort_propertyquarry_teable_sync", lambda *args, **kwargs: None)
    replacement_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        ProductService,
        "_start_property_search_repair_replacement_run",
        lambda self, **kwargs: replacement_calls.append(dict(kwargs)) or {"run_id": "unexpected-nested-repair"},
    )
    scout_calls: list[dict[str, object]] = []

    def _fake_sync_direct_property_scout(self, **kwargs):
        scout_calls.append(dict(kwargs))
        progress_callback = kwargs.get("progress_callback")
        if progress_callback:
            progress_callback(
                step="source_started",
                message="Checking recovered replacement source.",
                status="in_progress",
                steps_delta=1,
                summary_updates={"sources_total": 1},
            )
        return {
            "status": "processed",
            "sources_total": 1,
            "sources": [{"source_label": "Willhaben", "status": "processed"}],
            "listing_total": 0,
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)
    parent_run_id = "scheduler-parent-stale-run"
    replacement_run_id = "scheduler-replacement-stale-run"
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    parent_state = product_service._new_property_search_run_record(
        run_id=parent_run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "location_query": "1010 Vienna",
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
        force_refresh=False,
    )
    parent_state["status"] = "failed"
    parent_state["summary"] = {
        **dict(parent_state.get("summary") or {}),
        "repair_replacement_run_id": replacement_run_id,
        "repair_replacement_status_url": f"/app/api/signals/property/search/run/{replacement_run_id}",
    }
    replacement_state = product_service._new_property_search_run_record(
        run_id=replacement_run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "location_query": "1010 Vienna",
            "max_results_per_source": 1,
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
        force_refresh=True,
    )
    replacement_state["status"] = "starting"
    replacement_state["current_step"] = "starting"
    replacement_state["message"] = "Starting property search run."
    replacement_state["updated_at"] = stale_timestamp
    replacement_state["events"] = [
        {
            "at": stale_timestamp,
            "step": "starting",
            "status": "starting",
            "message": "Starting property search run.",
        }
    ]
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[parent_run_id] = dict(parent_state)
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[replacement_run_id] = dict(replacement_state)
    product_service._store_property_search_run_record(dict(parent_state))
    product_service._store_property_search_run_record(dict(replacement_state))

    summary = service.reconcile_stale_property_search_runs(principal_id=principal_id, limit=20)

    assert summary["stale_total"] == 1
    assert summary["repaired"] == 1
    assert summary["replacement_started"] == 0
    assert summary["recovered"][0]["execution_pickup_status"] == "started"
    for _ in range(60):
        status = service.get_property_search_run_status(principal_id=principal_id, run_id=replacement_run_id)
        if status and str(status.get("status") or "") == "processed":
            break
        time.sleep(0.02)
    assert status is not None
    assert status["status"] == "processed"
    assert status["summary"]["execution_pickup_status"] == "completed"
    assert status["summary"]["execution_pickup_reason"] == "replacement_run_stale"
    assert status["summary"]["repair_parent_run_ids"] == [parent_run_id]
    assert any(event["step"] == "recovery_pickup_started" for event in status["events"])
    assert scout_calls
    assert scout_calls[0]["max_results_per_source"] == 1
    assert scout_calls[0]["property_search_preferences"]["__property_search_run_id__"] == replacement_run_id
    assert replacement_calls == []


def test_property_search_status_picks_up_stale_replacement_run_from_lightweight_poll(monkeypatch) -> None:
    principal_id = "exec-property-search-status-recovery-replacement"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Search Status Replacement Recovery Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_STALE_SECONDS", "3600")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_REPLACEMENT_RUN_STALE_SECONDS", "60")
    service = product_service.build_product_service(client.app.state.container)
    parent_run_id = "status-parent-stale-run"
    replacement_run_id = "status-replacement-stale-run"
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    replacement_state = product_service._new_property_search_run_record(
        run_id=replacement_run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna", "max_results_per_source": 1},
        force_refresh=True,
    )
    replacement_state["status"] = "in_progress"
    replacement_state["current_step"] = "source_previewing"
    replacement_state["message"] = "Reviewing candidate 7 of 30 for Kalandra."
    replacement_state["updated_at"] = stale_timestamp
    replacement_state["summary"] = {
        **dict(replacement_state.get("summary") or {}),
        "repair_parent_run_id": parent_run_id,
        "sources_total": 117,
        "sources_completed": 1,
    }
    replacement_state["events"] = [
        {
            "at": stale_timestamp,
            "step": "source_previewing",
            "status": "in_progress",
            "message": "Reviewing candidate 7 of 30 for Kalandra.",
        }
    ]
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[replacement_run_id] = dict(replacement_state)
    product_service._store_property_search_run_record(dict(replacement_state))

    pickup_calls: list[dict[str, object]] = []

    def _fake_pickup(self, **kwargs):
        pickup_calls.append(dict(kwargs))
        return {
            "status": "started",
            "run_id": replacement_run_id,
            "principal_id": principal_id,
            "reason": "replacement_run_stale",
            "parent_run_ids": [parent_run_id],
        }

    monkeypatch.setattr(ProductService, "_pick_up_property_search_run_execution", _fake_pickup)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=replacement_run_id,
        lightweight=True,
    )

    assert status is not None
    assert pickup_calls
    assert pickup_calls[0]["reason"] == "replacement_run_stale"
    assert pickup_calls[0]["parent_run_ids"] == (parent_run_id,)
    assert pickup_calls[0]["record"]["run_id"] == replacement_run_id


def test_property_search_status_does_not_restart_replacement_at_sources_resolved(monkeypatch) -> None:
    principal_id = "exec-property-search-status-recovery-sources-resolved"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Search Status Sources Resolved Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_STALE_SECONDS", "3600")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_REPLACEMENT_RUN_STALE_SECONDS", "60")
    service = product_service.build_product_service(client.app.state.container)
    parent_run_id = "status-parent-sources-resolved"
    replacement_run_id = "status-replacement-sources-resolved"
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    replacement_state = product_service._new_property_search_run_record(
        run_id=replacement_run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna", "max_results_per_source": 1},
        force_refresh=True,
    )
    replacement_state["status"] = "in_progress"
    replacement_state["current_step"] = "sources_resolved"
    replacement_state["message"] = "Selected 21 provider(s) with expanded coverage."
    replacement_state["updated_at"] = stale_timestamp
    replacement_state["summary"] = {
        **dict(replacement_state.get("summary") or {}),
        "repair_parent_run_id": parent_run_id,
        "sources_total": 117,
        "sources_completed": 9,
    }
    replacement_state["events"] = [
        {
            "at": stale_timestamp,
            "step": "sources_resolved",
            "status": "in_progress",
            "message": "Selected 21 provider(s) with expanded coverage.",
        }
    ]
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[replacement_run_id] = dict(replacement_state)
    product_service._store_property_search_run_record(dict(replacement_state))

    pickup_calls: list[dict[str, object]] = []

    def _fake_pickup(self, **kwargs):
        pickup_calls.append(dict(kwargs))
        return {"status": "started"}

    monkeypatch.setattr(ProductService, "_pick_up_property_search_run_execution", _fake_pickup)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=replacement_run_id,
        lightweight=True,
    )

    assert status is not None
    assert status["current_step"] == "sources_resolved"
    assert pickup_calls == []
    should_pick_up, parent_refs, reason = service._property_search_run_should_pick_up_execution(dict(replacement_state))
    assert should_pick_up is False
    assert parent_refs == ()
    assert reason == ""


def test_property_search_status_resumes_interrupted_pickup_at_sources_resolved(monkeypatch) -> None:
    principal_id = "exec-property-search-status-recovery-sources-resolved-pickup"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Search Status Sources Resolved Pickup Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_STALE_SECONDS", "3600")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_REPLACEMENT_RUN_STALE_SECONDS", "60")
    service = product_service.build_product_service(client.app.state.container)
    parent_run_id = "status-parent-sources-resolved-pickup"
    replacement_run_id = "status-replacement-sources-resolved-pickup"
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    replacement_state = product_service._new_property_search_run_record(
        run_id=replacement_run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna", "max_results_per_source": 1},
        force_refresh=True,
    )
    replacement_state["status"] = "in_progress"
    replacement_state["current_step"] = "sources_resolved"
    replacement_state["message"] = "Selected 21 provider(s) with expanded coverage."
    replacement_state["updated_at"] = stale_timestamp
    replacement_state["summary"] = {
        **dict(replacement_state.get("summary") or {}),
        "repair_parent_run_id": parent_run_id,
        "sources_total": 117,
        "sources_completed": 9,
        "execution_pickup_status": "started",
        "execution_pickup_attempt": 1,
    }
    replacement_state["events"] = [
        {
            "at": stale_timestamp,
            "step": "recovery_pickup_started",
            "status": "in_progress",
            "message": "Scheduler picked up the stale active search run for live execution.",
        },
        {
            "at": stale_timestamp,
            "step": "sources_resolved",
            "status": "in_progress",
            "message": "Selected 21 provider(s) with expanded coverage.",
        },
    ]
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[replacement_run_id] = dict(replacement_state)
    product_service._store_property_search_run_record(dict(replacement_state))

    pickup_calls: list[dict[str, object]] = []

    def _fake_pickup(self, **kwargs):
        pickup_calls.append(dict(kwargs))
        return {"status": "started"}

    monkeypatch.setattr(ProductService, "_pick_up_property_search_run_execution", _fake_pickup)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=replacement_run_id,
        lightweight=True,
    )

    assert status is not None
    assert pickup_calls
    assert pickup_calls[0]["reason"] == "replacement_run_stale"
    assert pickup_calls[0]["parent_run_ids"] == (parent_run_id,)
    should_pick_up, parent_refs, reason = service._property_search_run_should_pick_up_execution(dict(replacement_state))
    assert should_pick_up is True
    assert parent_refs == (parent_run_id,)
    assert reason == "replacement_run_stale"


def test_property_search_status_recovers_stale_review_packet_failure(monkeypatch) -> None:
    principal_id = "exec-property-search-status-recovery-review-packet-failed"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Search Status Review Packet Recovery Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_STALE_SECONDS", "3600")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_REPLACEMENT_RUN_STALE_SECONDS", "60")
    service = product_service.build_product_service(client.app.state.container)
    parent_run_id = "status-parent-review-packet-failed"
    replacement_run_id = "status-replacement-review-packet-failed"
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    replacement_state = product_service._new_property_search_run_record(
        run_id=replacement_run_id,
        principal_id=principal_id,
        selected_platforms=("remax_at",),
        property_search_preferences={"country_code": "AT", "location_query": "1090 Vienna", "max_results_per_source": 1},
        force_refresh=True,
    )
    replacement_state["status"] = "in_progress"
    replacement_state["current_step"] = "source_review_packet_failed"
    replacement_state["message"] = "Review page preparation timed out after 20s for https://www.remax.at/properties/propertysearch."
    replacement_state["updated_at"] = stale_timestamp
    replacement_state["summary"] = {
        **dict(replacement_state.get("summary") or {}),
        "repair_parent_run_id": parent_run_id,
        "sources_total": 117,
        "sources_completed": 17,
        "execution_pickup_status": "started",
        "execution_pickup_attempt": 1,
    }
    replacement_state["events"] = [
        {
            "at": stale_timestamp,
            "step": "source_review_packet_failed",
            "status": "in_progress",
            "message": "Review page preparation timed out after 20s for https://www.remax.at/properties/propertysearch.",
        }
    ]
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[replacement_run_id] = dict(replacement_state)
    product_service._store_property_search_run_record(dict(replacement_state))

    pickup_calls: list[dict[str, object]] = []

    def _fake_pickup(self, **kwargs):
        pickup_calls.append(dict(kwargs))
        return {"status": "started"}

    monkeypatch.setattr(ProductService, "_pick_up_property_search_run_execution", _fake_pickup)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=replacement_run_id,
        lightweight=True,
    )

    assert status is not None
    assert pickup_calls
    assert pickup_calls[0]["reason"] == "replacement_run_stale"
    assert pickup_calls[0]["parent_run_ids"] == (parent_run_id,)
    should_pick_up, parent_refs, reason = service._property_search_run_should_pick_up_execution(dict(replacement_state))
    assert should_pick_up is True
    assert parent_refs == (parent_run_id,)
    assert reason == "replacement_run_stale"


def test_property_search_status_picks_up_stale_active_checkpoint_from_lightweight_poll(monkeypatch) -> None:
    principal_id = "exec-property-search-status-recovery-active"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Search Status Active Recovery Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_STALE_SECONDS", "3600")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_ACTIVE_RUN_STALE_SECONDS", "60")
    service = product_service.build_product_service(client.app.state.container)
    run_id = "status-active-stale-run"
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna", "max_results_per_source": 1},
        force_refresh=True,
    )
    state["status"] = "in_progress"
    state["current_step"] = "source_previewing"
    state["message"] = "Reviewing candidate 12 of 34 for Willhaben."
    state["updated_at"] = stale_timestamp
    state["events"] = [
        {
            "at": stale_timestamp,
            "step": "source_previewing",
            "status": "in_progress",
            "message": "Reviewing candidate 12 of 34 for Willhaben.",
        }
    ]
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)
    product_service._store_property_search_run_record(dict(state))

    pickup_calls: list[dict[str, object]] = []

    def _fake_pickup(self, **kwargs):
        pickup_calls.append(dict(kwargs))
        return {
            "status": "started",
            "run_id": run_id,
            "principal_id": principal_id,
            "reason": "active_run_checkpoint_stale",
            "parent_run_ids": [],
        }

    monkeypatch.setattr(ProductService, "_pick_up_property_search_run_execution", _fake_pickup)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=True,
    )

    assert status is not None
    assert pickup_calls
    assert pickup_calls[0]["reason"] == "active_run_checkpoint_stale"
    assert pickup_calls[0]["parent_run_ids"] == ()
    assert pickup_calls[0]["record"]["run_id"] == run_id


def test_property_search_status_does_not_pick_up_active_checkpoint_with_fresh_progress_event(monkeypatch) -> None:
    principal_id = "exec-property-search-status-recovery-active-fresh-event"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Search Status Active Fresh Event Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_ACTIVE_RUN_STALE_SECONDS", "60")
    service = product_service.build_product_service(client.app.state.container)
    run_id = "status-active-fresh-event-run"
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    fresh_timestamp = datetime.now(timezone.utc).isoformat()
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna", "max_results_per_source": 1},
        force_refresh=True,
    )
    state["status"] = "in_progress"
    state["current_step"] = "source_previewing"
    state["message"] = "Reviewing candidate 12 of 34 for Willhaben."
    state["updated_at"] = stale_timestamp
    state["events"] = [
        {
            "at": fresh_timestamp,
            "step": "source_previewing",
            "status": "in_progress",
            "message": "Reviewing candidate 12 of 34 for Willhaben.",
        }
    ]
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)

    pickup_calls: list[dict[str, object]] = []

    def _fake_pickup(self, **kwargs):
        pickup_calls.append(dict(kwargs))
        return {"status": "started"}

    monkeypatch.setattr(ProductService, "_pick_up_property_search_run_execution", _fake_pickup)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=True,
    )

    assert status is not None
    assert status["current_step"] == "source_previewing"
    assert pickup_calls == []


def test_property_search_status_backfills_top_level_timestamp_from_summary(monkeypatch) -> None:
    principal_id = "exec-property-search-status-summary-timestamp"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Search Status Summary Timestamp Office")
    service = product_service.build_product_service(client.app.state.container)
    run_id = "status-summary-timestamp-run"
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna", "max_results_per_source": 1},
        force_refresh=True,
    )
    state["status"] = "in_progress"
    state["current_step"] = "source_previewing"
    state["updated_at"] = None
    state["summary"] = {
        **dict(state.get("summary") or {}),
        "updated_at": "2026-06-25T15:07:35.790880+00:00",
    }
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=False,
    )

    assert status is not None
    assert status["updated_at"] == "2026-06-25T15:07:35.790880+00:00"


def test_property_search_recovery_allows_pickup_after_real_progress(monkeypatch) -> None:
    principal_id = "exec-property-search-recovery-after-progress"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Search Recovery After Progress Office")
    service = product_service.build_product_service(client.app.state.container)
    monkeypatch.setattr(ProductService, "_best_effort_propertyquarry_teable_sync", lambda *args, **kwargs: None)

    scout_calls: list[dict[str, object]] = []

    def _fake_scout(self, **kwargs):
        scout_calls.append(dict(kwargs))
        return {
            "status": "processed",
            "sources_total": 1,
            "sources_completed": 1,
            "ranked_candidates": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_scout)

    run_id = "recovery-after-progress-run"
    older = (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()
    recent_progress = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna", "max_results_per_source": 1},
        force_refresh=True,
    )
    state["status"] = "in_progress"
    state["current_step"] = "source_previewing"
    state["updated_at"] = recent_progress
    state["events"] = [
        {
            "at": older,
            "step": "recovery_pickup_started",
            "status": "in_progress",
            "message": "Scheduler picked up the stale active search run for live execution.",
        },
        {
            "at": older,
            "step": "recovery_pickup_started",
            "status": "in_progress",
            "message": "Scheduler picked up the stale active search run for live execution.",
        },
        {
            "at": recent_progress,
            "step": "source_previewing",
            "status": "in_progress",
            "message": "Reviewing candidate 12 of 34 for Willhaben.",
        },
    ]
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)

    pickup = service._pick_up_property_search_run_execution(
        record=dict(state),
        actor="property_search_status_recovery",
        reason="active_run_checkpoint_stale",
    )

    assert pickup["status"] == "started"
    for _ in range(60):
        status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
        summary = dict(status.get("summary") or {}) if isinstance(status, dict) else {}
        if summary.get("execution_pickup_status") == "completed":
            break
        time.sleep(0.02)
    assert scout_calls
    assert status["summary"]["execution_pickup_attempt"] == 3
    assert status["summary"]["execution_pickup_consecutive_attempt"] == 1
    assert status["summary"]["execution_pickup_reason"] == "active_run_checkpoint_stale"


def test_property_search_recovery_pickup_failure_opens_repair_task(monkeypatch) -> None:
    principal_id = "exec-property-search-recovery-pickup-failure"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Search Recovery Pickup Failure Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_STALE_SECONDS", "60")
    service = product_service.build_product_service(client.app.state.container)
    monkeypatch.setattr(ProductService, "_best_effort_propertyquarry_teable_sync", lambda *args, **kwargs: None)

    def _raise_pickup_failure(self, **kwargs):
        raise RuntimeError("pickup worker crashed before source rows existed")

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _raise_pickup_failure)
    parent_run_id = "scheduler-parent-pickup-failure"
    replacement_run_id = "scheduler-replacement-pickup-failure"
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    parent_state = product_service._new_property_search_run_record(
        run_id=parent_run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna"},
        force_refresh=False,
    )
    parent_state["status"] = "failed"
    parent_state["summary"] = {
        **dict(parent_state.get("summary") or {}),
        "repair_replacement_run_id": replacement_run_id,
    }
    replacement_state = product_service._new_property_search_run_record(
        run_id=replacement_run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna", "max_results_per_source": 1},
        force_refresh=True,
    )
    replacement_state["status"] = "starting"
    replacement_state["current_step"] = "starting"
    replacement_state["updated_at"] = stale_timestamp
    replacement_state["events"] = [
        {
            "at": stale_timestamp,
            "step": "starting",
            "status": "starting",
            "message": "Starting property search run.",
        }
    ]
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[parent_run_id] = dict(parent_state)
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[replacement_run_id] = dict(replacement_state)
    product_service._store_property_search_run_record(dict(parent_state))
    product_service._store_property_search_run_record(dict(replacement_state))

    summary = service.reconcile_stale_property_search_runs(principal_id=principal_id, limit=20)

    assert summary["stale_total"] == 1
    assert summary["repaired"] == 1
    for _ in range(60):
        status = service.get_property_search_run_status(principal_id=principal_id, run_id=replacement_run_id)
        if status and str(status.get("status") or "") == "failed":
            break
        time.sleep(0.02)
    assert status is not None
    assert status["status"] == "failed"
    assert status["summary"]["execution_pickup_status"] == "failed"
    assert status["summary"]["repair_status"] == "repairing"
    assert status["summary"]["provider_repair_task_opened_total"] == 1
    tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    matching_tasks = [
        task
        for task in tasks
        if dict(task.input_json or {}).get("filter_key") == "run_worker_exception"
        and dict(task.input_json or {}).get("run_id") == replacement_run_id
    ]
    assert matching_tasks
    repair_input = dict(matching_tasks[0].input_json or {})
    assert repair_input["filter_key"] == "run_worker_exception"
    assert repair_input["run_id"] == replacement_run_id
    assert repair_input["diagnostics"]["recovery_reason"] == "replacement_run_stale"
    assert repair_input["diagnostics"]["repair_parent_run_ids"] == [parent_run_id]


def test_property_search_run_status_enriches_source_fetch_repairs() -> None:
    principal_id = "exec-property-run-status-repair-enrichment"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Run Status Repair Enrichment Office")
    service = ProductService(client.app.state.container)
    run_id = f"repair-status-{uuid.uuid4().hex}"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(product_service.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fetch blocked")))
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": product_service._now_iso(),
            "updated_at": product_service._now_iso(),
            "status": "in_progress",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["wohnberatung_wien"],
            "progress": 36,
            "message": "Fetching source page for Wohnberatung Wien.",
            "summary": {
                "status": "in_progress",
                "sources_total": 1,
                "reviewed_listing_total": 0,
                "sources": [
                    {
                        "source_url": "https://wohnberatung.example.invalid/search",
                        "source_label": "Wohnberatung Wien | Austria | Rent | Vienna",
                        "error": "HTTP Error 403: Forbidden",
                    }
                ],
            },
        }
    service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://wohnberatung.example.invalid/search",
        title="Wohnberatung Wien | Austria | Rent | Vienna",
        source_url="https://wohnberatung.example.invalid/search",
        source_label="Wohnberatung Wien | Austria | Rent | Vienna",
        source_platform="wohnberatung_wien",
        source_family="public_housing",
        filter_key="source_fetch",
        diagnostics={"provider_host": "wohnberatung.example.invalid", "error": "HTTP Error 403: Forbidden"},
        source_ref="property-source:test",
        run_id=run_id,
    )
    tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert len(tasks) == 1
    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    source = status["summary"]["sources"][0]
    assert source["status"] == "repaired"
    assert source["repair_status"] == "returned"
    assert source["provider_repair_tasks"][0]["resolution"] == "suppressed_source_fetch_forbidden"
    monkeypatch.undo()


def test_property_search_run_terminal_receives_repair_receipt_without_task_scan(monkeypatch) -> None:
    principal_id = "exec-property-run-terminal-repair-receipt"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Terminal Repair Receipt Office")
    service = ProductService(client.app.state.container)
    run_id = f"repair-receipt-{uuid.uuid4().hex}"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": product_service._now_iso(),
            "updated_at": product_service._now_iso(),
            "status": "completed_partial",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["wohnberatung_wien"],
            "progress": 100,
            "message": "Current shortlist is still available.",
            "summary": {
                "status": "completed_partial",
                "sources_total": 2,
                "ranked_candidates": [{"candidate_ref": "cand-1", "title": "Recovered hit"}],
                "sources": [
                    {
                        "source_url": "https://wohnberatung.example.invalid/search",
                        "source_label": "Wohnberatung Wien | Austria | Rent | Vienna",
                        "status": "failed",
                        "error": "HTTP Error 403: Forbidden",
                    }
                ],
            },
        }
    monkeypatch.setattr(product_service.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fetch blocked")))
    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://wohnberatung.example.invalid/search",
        title="Wohnberatung Wien | Austria | Rent | Vienna",
        source_url="https://wohnberatung.example.invalid/search",
        source_label="Wohnberatung Wien | Austria | Rent | Vienna",
        source_platform="wohnberatung_wien",
        source_family="public_housing",
        filter_key="source_fetch",
        diagnostics={"provider_host": "wohnberatung.example.invalid", "error": "HTTP Error 403: Forbidden"},
        source_ref="property-source:test",
        run_id=run_id,
    )
    assert opened["status"] in {"opened", "existing"}

    monkeypatch.setattr(
        client.app.state.container.orchestrator,
        "list_human_tasks",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("terminal status should not scan repair tasks")),
    )
    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    summary = dict(status.get("summary") or {})
    source = dict((summary.get("sources") or [])[0])
    assert source["status"] == "repaired"
    assert source["repair_status"] == "returned"
    assert source["repair_resolution"] == "suppressed_source_fetch_forbidden"
    receipts = [dict(row) for row in list(summary.get("repair_receipts") or []) if isinstance(row, dict)]
    assert len(receipts) == 1
    assert receipts[0]["run_id"] == run_id
    assert receipts[0]["resolution"] == "suppressed_source_fetch_forbidden"


def test_property_search_run_final_event_applies_early_source_fetch_repair_receipt(monkeypatch) -> None:
    principal_id = "exec-property-run-early-repair-receipt"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Early Repair Receipt Office")
    service = ProductService(client.app.state.container)
    run_id = f"early-repair-receipt-{uuid.uuid4().hex}"
    now = product_service._now_iso()
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": now,
            "updated_at": now,
            "status": "in_progress",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["wohnberatung_wien", "willhaben"],
            "progress": 18,
            "message": "Fetching source page for Wohnberatung Wien.",
            "summary": {
                "status": "in_progress",
                "sources_total": 2,
                "sources": [],
            },
        }
    monkeypatch.setattr(product_service.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fetch blocked")))

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url="https://wohnberatung.example.invalid/search",
        title="Wohnberatung Wien | Austria | Rent | Vienna",
        source_url="https://wohnberatung.example.invalid/search",
        source_label="Wohnberatung Wien | Austria | Rent | Vienna",
        source_platform="wohnberatung_wien",
        source_family="public_housing",
        filter_key="source_fetch",
        diagnostics={"provider_host": "wohnberatung.example.invalid", "error": "HTTP Error 403: Forbidden"},
        source_ref="property-source:test",
        run_id=run_id,
    )
    assert opened["status"] == "opened"
    assert opened["repair_status"] == "returned"

    service._record_property_search_run_event(
        run_id=run_id,
        principal_id=principal_id,
        step="completed",
        message="Search run completed with partial coverage.",
        status="completed_partial",
        steps_delta=0,
        force_status="completed_partial",
        summary_updates={
            "status": "completed_partial",
            "sources_total": 2,
            "failed_total": 1,
            "ranked_candidates": [{"candidate_ref": "cand-1", "title": "Usable hit"}],
            "sources": [
                {
                    "source_url": "https://wohnberatung.example.invalid/search",
                    "source_label": "Wohnberatung Wien | Austria | Rent | Vienna",
                    "status": "failed",
                    "error": "HTTP Error 403: Forbidden",
                    "provider_repair_task_opened_total": 1,
                    "provider_repair_tasks": [
                        {
                            "status": "opened",
                            "filter_key": "source_fetch",
                            "human_task_id": opened["human_task_id"],
                            "queue_item_ref": opened["queue_item_ref"],
                        }
                    ],
                },
                {
                    "source_url": "https://www.willhaben.at/iad/immobilien/",
                    "source_label": "Willhaben | Austria | Rent | Vienna",
                    "status": "completed",
                    "listing_total": 1,
                },
            ],
        },
    )

    monkeypatch.setattr(
        client.app.state.container.orchestrator,
        "list_human_tasks",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("terminal status should use repair receipts")),
    )
    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    summary = dict(status.get("summary") or {})
    source = dict((summary.get("sources") or [])[0])
    assert source["status"] == "repaired"
    assert source["repair_status"] == "returned"
    assert source["repair_resolution"] == "suppressed_source_fetch_forbidden"
    assert source["error"] == ""
    assert source["original_error"] == "HTTP Error 403: Forbidden"
    assert source["provider_repair_tasks"][0]["status"] == "returned"
    receipts = [dict(row) for row in list(summary.get("repair_receipts") or []) if isinstance(row, dict)]
    assert len(receipts) == 1
    assert receipts[0]["run_id"] == run_id


def test_recent_property_source_fetch_repair_memory_returns_latest_returned_source_fetch_resolution(monkeypatch) -> None:
    principal_id = "exec-property-repair-memory"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Repair Memory Office")
    service = ProductService(client.app.state.container)
    older = SimpleNamespace(
        task_type="property_provider_repair_ooda",
        status="returned",
        resolution="suppressed_source_fetch_missing",
        updated_at="2026-06-16T09:00:00Z",
        created_at="2026-06-16T08:00:00Z",
        human_task_id="older-task",
        input_json={
            "filter_key": "source_fetch",
            "source_url": "https://www.kalandra.at/search",
            "source_label": "Kalandra | Austria | Rent | 1010 Vienna",
        },
        returned_payload_json={"reason": "older"},
    )
    latest = SimpleNamespace(
        task_type="property_provider_repair_ooda",
        status="returned",
        resolution="suppressed_source_fetch_forbidden",
        updated_at=product_service._now_iso(),
        created_at=product_service._now_iso(),
        human_task_id="latest-task",
        input_json={
            "filter_key": "source_fetch",
            "source_url": "https://www.kalandra.at/search",
            "source_label": "Kalandra | Austria | Rent | 1010 Vienna",
        },
        returned_payload_json={"reason": "provider blocked"},
    )
    monkeypatch.setattr(
        client.app.state.container.orchestrator,
        "list_human_tasks",
        lambda **kwargs: [older, latest],
    )

    memory = service._recent_property_source_fetch_repair_memory(principal_id=principal_id)
    key = service._property_source_repair_memory_key(
        source_url="https://www.kalandra.at/search",
        source_label="Kalandra | Austria | Rent | 1010 Vienna",
    )

    assert memory[key]["resolution"] == "suppressed_source_fetch_forbidden"
    assert memory[key]["reason"] == "provider blocked"
    assert memory[key]["human_task_id"] == "latest-task"


def test_property_search_sparse_auction_floorplan_area_scores_above_review_threshold() -> None:
    preview = {
        "title": "BG Leopoldstadt, 082 25 E 89/25g",
        "summary": "Sparse judicial auction detail page.",
        "property_facts_json": {
            "area_sqm": 126.59,
            "floorplan_count": 1,
            "floorplan_urls_json": ["https://edikte2.justiz.gv.at/edikte/ex/exedi3.nsf/0/example/$file/Gutachten.pdf"],
            "provider_channel": "justiz_edikte_at",
            "sale_channel": "judicial_auction",
            "source_scope_location": "1020 Vienna",
        },
    }
    assessment = {
        "fit_score": 47.96,
        "upstream_personalization": {"adjusted_fit_score": 45.46},
    }

    score = product_service._property_scout_rank_score(
        property_url="https://edikte2.justiz.gv.at/edikte/ex/exedi3.nsf/alldoc/example!OpenDocument",
        assessment=assessment,
        preview=preview,
        ordinal=6,
    )

    assert score >= 54.0


def test_property_search_keeps_review_candidate_when_only_score_threshold_fails(monkeypatch) -> None:
    principal_id = "exec-property-soft-score-fallback"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Soft Score Fallback Office")
    service = ProductService(client.app.state.container)

    monkeypatch.setattr(
        product_service,
        "_merged_property_scout_source_specs",
        lambda **kwargs: [
            {
                "url": "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt",
                "label": "Willhaben | Austria | Rent | 1020 Vienna",
                "platform": "willhaben",
                "provider_family": "marketplace",
                "country_code": "AT",
                "max_results": 1,
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    listing_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/review-but-low-score-1/"
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"): {
                "listing_urls": [listing_url],
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:soft-score"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_scout_page_preview_with_timeout",
        lambda property_url, *, prefer_fast=False: {
            "listing_id": "soft-score-1",
            "title": "Mietwohnung in 1020 Wien mit Balkon",
            "summary": "78 m2, 3 Zimmer, Gesamtmiete EUR 1.650, Balkon.",
            "property_facts_json": {
                "postal_name": "1020 Wien",
                "area_sqm": 78,
                "rooms": 3,
                "total_rent_eur": 1650,
                "has_balcony": True,
            },
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {
            "fit_score": 12.0,
            "recommendation": "review",
            "match_reasons_json": [],
            "mismatch_reasons_json": ["Soft preference mismatch"],
            "unknowns_json": [],
        },
    )
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "min_match_score": 95,
            "require_floorplan": False,
        },
        max_results_per_source=1,
        force_refresh=True,
    )

    assert result["listing_total"] == 1
    assert result["high_fit_total"] == 0
    assert result["filtered_low_fit_total"] == 0
    assert result["score_demoted_total"] == 0
    assert result["sources"][0]["score_demoted_total"] == 0
    candidate = dict(result["sources"][0]["top_candidates"][0])
    assert candidate["property_url"] == listing_url
    assert 0 < float(candidate["fit_score"]) < 95
    assert candidate["recommendation"] == "review"
    assert candidate.get("score_demoted") in (None, False)
    assert candidate.get("below_match_threshold") in (None, False)
    assert not str(candidate.get("score_demotion_reason") or "").strip()
    assert dict(candidate["property_facts"]).get("score_demoted_by_match_threshold") in (None, False)


def test_property_search_finds_expected_listing_when_hard_filters_match(monkeypatch) -> None:
    principal_id = "exec-property-pinned-listing"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Pinned Listing Office")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"
    listing_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/pinned-listing-1/"
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
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": [listing_url],
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:pinned-listing"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_scout_page_preview_with_timeout",
        lambda property_url, *, prefer_fast=False: {
            "listing_id": "pinned-listing-1",
            "title": "Mietwohnung in 1020 Wien mit Balkon",
            "summary": "78 m2, 3 Zimmer, Gesamtmiete EUR 1.650, Balkon.",
            "property_facts_json": {
                "postal_name": "1020 Wien",
                "property_type": "apartment",
                "area_sqm": 78,
                "rooms": 3,
                "total_rent_eur": 1650,
                "has_balcony": True,
            },
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {
            "fit_score": 84.0,
            "recommendation": "strong_fit",
            "match_reasons_json": ["Matches the target district, size, and rent cap."],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
        },
    )
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "max_price_eur": 1700,
            "min_area_m2": 70,
            "require_floorplan": False,
            "min_match_score": 0,
        },
        max_results_per_source=1,
        force_refresh=True,
    )

    assert result["listing_total"] == 1
    assert result["sources"][0]["listing_total"] == 1
    candidate = dict(result["sources"][0]["top_candidates"][0])
    assert candidate["property_url"] == listing_url
    assert candidate["title"] == "Mietwohnung in 1020 Wien mit Balkon"


def test_property_search_alert_scoring_respects_stored_feedback_toggle(monkeypatch) -> None:
    principal_id = "exec-property-alert-neutral-personalization"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Alert Neutral Personalization Office")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"
    listing_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/neutral-alert-score/"
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
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": [listing_url],
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:neutral-alert"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_scout_page_preview_with_timeout",
        lambda property_url, *, prefer_fast=False: {
            "listing_id": "neutral-alert-score",
            "title": "Mietwohnung in 1020 Wien mit Balkon",
            "summary": "78 m2, 3 Zimmer, Gesamtmiete EUR 1.650, Balkon.",
            "property_facts_json": {
                "postal_name": "1020 Wien",
                "area_sqm": 78,
                "rooms": 3,
                "total_rent_eur": 1650,
                "has_balcony": True,
            },
        },
    )
    observed_profile_flags: list[bool] = []

    def _fake_fit(**kwargs) -> dict[str, object]:
        use_profile_preferences = bool(kwargs.get("use_profile_preferences", True))
        observed_profile_flags.append(use_profile_preferences)
        return {
            "fit_score": 91.0 if use_profile_preferences else 52.0,
            "recommendation": "strong_fit" if use_profile_preferences else "review",
            "match_reasons_json": [],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
            "stored_feedback_preferences_used": use_profile_preferences,
        }

    monkeypatch.setattr(product_service, "_property_alert_personal_fit_from_facts", _fake_fit)
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})
    monkeypatch.setattr(
        ProductService,
        "_open_property_alert_review_with_timeout",
        lambda self, **kwargs: {"status": "opened", "editor_url": "/app/research/neutral-alert-score"},
    )

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "min_match_score": 40,
            "require_floorplan": False,
            "use_stored_feedback_preferences": False,
        },
        max_results_per_source=1,
        force_refresh=True,
    )

    assert observed_profile_flags
    assert observed_profile_flags == [False]
    assert result["listing_total"] == 1
    candidate = dict(result["sources"][0]["top_candidates"][0])
    assert candidate["property_url"] == listing_url
    assert 52.0 <= float(candidate["fit_score"]) < 91.0
    assert dict(candidate["assessment"])["stored_feedback_preferences_used"] is False


def test_property_search_keeps_soft_mismatch_candidates_visible_without_score_gate(monkeypatch) -> None:
    principal_id = "exec-property-soft-mismatch-shortlist-slots"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Soft Mismatch Shortlist Office")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"
    strong_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/high-fit/"
    demoted_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/soft-mismatch/"
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
                "max_results": 3,
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": [strong_url, demoted_url],
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:soft-mismatch-shortlist"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        if property_url == strong_url:
            return {
                "listing_id": "strong-shortlist",
                "title": "Familienwohnung nahe Park",
                "summary": "78 m2, 3 Zimmer, Gesamtmiete EUR 1.650, Balkon.",
                "property_facts_json": {
                    "postal_name": "1020 Wien",
                    "area_sqm": 78,
                    "rooms": 3,
                    "total_rent_eur": 1650,
                    "has_balcony": True,
                },
            }
        return {
            "listing_id": "demoted-shortlist",
            "title": "Helle Wohnung mit Lift und Balkon",
            "summary": "71 m2, 2 Zimmer, Gesamtmiete EUR 1.540, Lift, Balkon.",
            "property_facts_json": {
                "postal_name": "1020 Wien",
                "area_sqm": 71,
                "rooms": 2,
                "total_rent_eur": 1540,
                "has_lift": True,
                "has_balcony": True,
            },
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)
    scored_fact_inputs: list[dict[str, object]] = []
    original_score_candidate = product_service._property_search_score_candidate

    def _capture_score_candidate(**kwargs: object) -> dict[str, object]:
        scored_fact_inputs.append(dict(kwargs.get("property_facts") or {}))
        return original_score_candidate(**kwargs)

    monkeypatch.setattr(
        product_service,
        "_property_search_score_candidate",
        _capture_score_candidate,
    )

    def _fake_fit(**kwargs) -> dict[str, object]:
        listing_id = str(kwargs.get("listing_id") or kwargs.get("object_id") or "")
        score = 81.0 if listing_id == "strong-shortlist" else 12.0
        return {
            "fit_score": score,
            "confidence": 0.76,
            "predicted_reaction": "consider",
            "recommendation": "shortlist" if score >= 65 else "review",
            "match_reasons_json": ["Above the matching threshold."] if score >= 65 else ["Worth a look despite softer mismatches."],
            "mismatch_reasons_json": [] if score >= 65 else ["Below the matching threshold."],
            "unknowns_json": [],
            "blocking_constraints_json": [],
        }

    monkeypatch.setattr(product_service, "_property_alert_personal_fit_from_facts", _fake_fit)
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})
    monkeypatch.setattr(
        ProductService,
        "_open_property_alert_review_with_timeout",
        lambda self, **kwargs: {"status": "opened", "editor_url": f"/app/research/{kwargs.get('source_ref') or 'candidate'}"},
    )

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "min_match_score": 65,
            "require_floorplan": False,
        },
        max_results_per_source=3,
        force_refresh=True,
    )

    titles = [row["title"] for row in result["sources"][0]["top_candidates"]]
    assert result["listing_total"] == 2
    assert result["sources"][0]["filtered_low_fit_total"] == 0
    assert result["sources"][0]["score_demoted_total"] == 0
    assert titles == ["Familienwohnung nahe Park", "Helle Wohnung mit Lift und Balkon"]
    assert result["sources"][0]["top_candidates"][1].get("below_match_threshold") in (None, False)


def test_property_search_ranking_rules_do_not_reorder_provider_results(monkeypatch) -> None:
    principal_id = "exec-property-ranking-bar-ordering-only"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Ranking Bar Ordering Office")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"
    strong_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/assessment-strong/"
    ranked_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/ranked-first/"
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
                "max_results": 2,
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": [strong_url, ranked_url],
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:ranking-bar-ordering"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        if property_url == strong_url:
            return {
                "listing_id": "assessment-strong",
                "title": "Assessment strong listing",
                "summary": "72 m2, 2 rooms, EUR 1,450.",
                "property_facts_json": {"postal_name": "1020 Wien", "area_sqm": 72, "rooms": 2, "total_rent_eur": 1450},
            }
        return {
            "listing_id": "ranked-first",
            "title": "Ranked first listing",
            "summary": "74 m2, 3 rooms, EUR 1,520.",
            "property_facts_json": {"postal_name": "1020 Wien", "area_sqm": 74, "rooms": 3, "total_rent_eur": 1520},
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)

    def _fake_fit(**kwargs) -> dict[str, object]:
        listing_id = str(kwargs.get("listing_id") or kwargs.get("object_id") or "")
        if listing_id == "assessment-strong":
            return {
                "fit_score": 70.0,
                "confidence": 0.76,
                "predicted_reaction": "consider",
                "recommendation": "shortlist",
                "match_reasons_json": ["Assessment stayed strong."],
                "mismatch_reasons_json": [],
                "unknowns_json": [],
                "blocking_constraints_json": [],
            }
        return {
            "fit_score": 30.0,
            "confidence": 0.71,
            "predicted_reaction": "review",
            "recommendation": "review",
            "match_reasons_json": ["Provider ranking still keeps this high in the order."],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
            "blocking_constraints_json": [],
        }

    monkeypatch.setattr(product_service, "_property_alert_personal_fit_from_facts", _fake_fit)
    monkeypatch.setattr(
        product_service,
        "_property_scout_rank_score",
        lambda **kwargs: 40.0 if str(kwargs.get("property_url") or "") == strong_url else 59.0,
    )
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})
    monkeypatch.setattr(
        ProductService,
        "_open_property_alert_review_with_timeout",
        lambda self, **kwargs: {"status": "opened", "editor_url": f"/app/research/{kwargs.get('source_ref') or 'candidate'}"},
    )

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "min_match_score": 60,
            "require_floorplan": False,
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
        max_results_per_source=2,
        force_refresh=True,
    )

    titles = [row["title"] for row in result["sources"][0]["top_candidates"]]
    assert titles == ["Ranked first listing", "Assessment strong listing"]
    assert result["sources"][0]["top_candidates"][0]["property_url"] == ranked_url
    assert result["sources"][0]["top_candidates"][1]["property_url"] == strong_url


def test_property_search_resolve_max_results_honors_explicit_cap_even_on_unlimited_plan() -> None:
    assert product_service._property_search_resolve_max_results_per_source({}, 1) == 1
    assert product_service._property_search_resolve_max_results_per_source({}, "3") == 3
    assert product_service._property_search_resolve_max_results_per_source({}, 0) is None


def test_property_search_defaults_to_all_ranked_results_when_no_cap_requested(monkeypatch) -> None:
    principal_id = "exec-property-default-all-ranked"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Default All Ranked Office")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"
    listing_urls = [
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/default-all-ranked-1/",
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/default-all-ranked-2/",
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/default-all-ranked-3/",
    ]
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
                "max_results": 2,
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": list(listing_urls),
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:default-all-ranked"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        ordinal = listing_urls.index(property_url) + 1
        return {
            "listing_id": f"default-all-ranked-{ordinal}",
            "title": f"Ranked listing {ordinal}",
            "summary": f"2 rooms | {60 + ordinal} m2 | 1020 Wien",
            "property_facts_json": {
                "postal_name": "1020 Wien",
                "area_sqm": 60 + ordinal,
                "rooms": 2,
                "total_rent_eur": 1500 + (ordinal * 10),
            },
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)

    def _fake_fit(**kwargs) -> dict[str, object]:
        property_url = str(kwargs.get("property_url") or "")
        ordinal = listing_urls.index(property_url) + 1
        score = 70.0 - ordinal
        return {
            "fit_score": score,
            "recommendation": "review",
            "match_reasons_json": [f"Ranked candidate {ordinal} stays admissible."],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
        }

    monkeypatch.setattr(product_service, "_property_alert_personal_fit_from_facts", _fake_fit)
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "require_floorplan": False,
        },
        max_results_per_source=None,
        force_refresh=True,
    )

    assert result["max_results_per_source"] == 0
    assert result["listing_total"] == 3
    assert len(result["sources"][0]["top_candidates"]) == 3
    assert [row["title"] for row in result["sources"][0]["top_candidates"]] == [
        "Ranked listing 1",
        "Ranked listing 2",
        "Ranked listing 3",
    ]
    assert len(list(result.get("ranked_candidates") or [])) == 3


def test_property_search_keeps_all_provider_results_after_final_ranking(monkeypatch) -> None:
    principal_id = "exec-property-provider-cap-after-ranking"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Cap After Ranking Office")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"
    early_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/early-preview-leader/"
    late_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/late-final-winner/"
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
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": [early_url, late_url],
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:provider-cap-after-ranking"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        if property_url == early_url:
            return {
                "listing_id": "early-preview-leader",
                "title": "Early preview leader",
                "summary": "72 m2, 2 rooms, EUR 1,450.",
                "property_facts_json": {"postal_name": "1020 Wien", "area_sqm": 72, "rooms": 2, "total_rent_eur": 1450},
            }
        return {
            "listing_id": "late-final-winner",
            "title": "Late final winner",
            "summary": "74 m2, 3 rooms, EUR 1,520.",
            "property_facts_json": {"postal_name": "1020 Wien", "area_sqm": 74, "rooms": 3, "total_rent_eur": 1520},
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)

    def _fake_fit(**kwargs) -> dict[str, object]:
        listing_id = str(kwargs.get("listing_id") or kwargs.get("object_id") or "")
        if listing_id == "early-preview-leader":
            return {
                "fit_score": 66.0,
                "confidence": 0.76,
                "predicted_reaction": "consider",
                "recommendation": "shortlist",
                "match_reasons_json": ["Looks acceptable after detail scoring."],
                "mismatch_reasons_json": [],
                "unknowns_json": [],
                "blocking_constraints_json": [],
            }
        return {
            "fit_score": 88.0,
            "confidence": 0.8,
            "predicted_reaction": "consider",
            "recommendation": "strong_fit",
            "match_reasons_json": ["Wins after the final detail pass."],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
            "blocking_constraints_json": [],
        }

    monkeypatch.setattr(product_service, "_property_alert_personal_fit_from_facts", _fake_fit)
    monkeypatch.setattr(
        product_service,
        "_property_scout_rank_score",
        lambda **kwargs: 62.0 if str(kwargs.get("property_url") or "") == early_url else 91.0,
    )
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})
    monkeypatch.setattr(
        ProductService,
        "_open_property_alert_review_with_timeout",
        lambda self, **kwargs: {"status": "opened", "editor_url": f"/app/research/{kwargs.get('source_ref') or 'candidate'}"},
    )

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "min_match_score": 65,
            "require_floorplan": False,
        },
        max_results_per_source=1,
        force_refresh=True,
    )

    source = dict(result["sources"][0])
    assert result["listing_total"] == 2
    assert result["reviewed_listing_total"] == 2
    assert source["listing_total"] == 2
    assert source["reviewed_listing_total"] == 2
    assert [row["title"] for row in source["top_candidates"]] == ["Late final winner", "Early preview leader"]


def test_agent_property_search_keeps_all_ranked_results_per_provider(monkeypatch) -> None:
    principal_id = "exec-property-agent-unlimited-provider-results"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Agent Unlimited Provider Results")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"
    listing_urls = [
        f"https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/unlimited-{index}/"
        for index in range(12)
    ]
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
                "max_results": 3,
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": list(listing_urls),
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:agent-unlimited"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        slug = property_url.rstrip("/").rsplit("/", 1)[-1]
        return {
            "listing_id": slug,
            "title": f"Mietwohnung {slug}",
            "summary": "78 m2, 3 Zimmer, Gesamtmiete EUR 1.650, Balkon.",
            "property_facts_json": {
                "postal_name": "1020 Wien",
                "area_sqm": 78,
                "rooms": 3,
                "total_rent_eur": 1650,
                "has_balcony": True,
            },
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {
            "fit_score": 84.0,
            "confidence": 0.82,
            "predicted_reaction": "consider",
            "recommendation": "strong_fit",
            "match_reasons_json": ["Strong fit"],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
            "blocking_constraints_json": [],
        },
    )
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})
    monkeypatch.setattr(
        ProductService,
        "_open_property_alert_review_with_timeout",
        lambda self, **kwargs: {"status": "opened", "editor_url": f"/app/research/{kwargs.get('source_ref') or 'candidate'}"},
    )

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "min_match_score": 40,
            "require_floorplan": False,
            "max_results_per_source": 1,
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
        force_refresh=True,
    )

    source = dict(result["sources"][0])
    assert result["listing_total"] == len(listing_urls)
    assert source["listing_total"] == len(listing_urls)
    assert len(source["top_candidates"]) == len(listing_urls)
    assert len(source["research_candidates"]) == len(listing_urls)
    assert {row["property_url"] for row in source["top_candidates"]} == set(listing_urls)


def test_property_search_soft_filters_do_not_change_discovered_hit_set(monkeypatch) -> None:
    principal_id = "exec-property-soft-filter-equivalence"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Soft Filter Equivalence Office")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"
    listing_urls = [
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/soft-filter-a-1234567890/",
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/soft-filter-b-1234567891/",
    ]
    facts_by_url = {
        listing_urls[0]: {
            "postal_name": "1020 Wien",
            "area_sqm": 78,
            "rooms": 3,
            "total_rent_eur": 1650,
            "has_floorplan": True,
            "nearest_library_m": 1800,
            "nearest_playground_m": 1200,
            "nearest_shopping_center_m": 160,
            "nearest_theatre_m": 900,
            "nearest_supermarket_m": 700,
        },
        listing_urls[1]: {
            "postal_name": "1020 Wien",
            "area_sqm": 82,
            "rooms": 3,
            "total_rent_eur": 1720,
            "has_floorplan": True,
            "nearest_library_m": 220,
            "nearest_playground_m": 420,
            "nearest_shopping_center_m": 900,
            "nearest_theatre_m": 350,
            "nearest_supermarket_m": 180,
        },
    }

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
                "max_results": 5,
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": list(listing_urls),
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:soft-filter-equivalence"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )

    preview_calls: list[str] = []

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        preview_calls.append(property_url)
        raw_facts = dict(facts_by_url[property_url])
        facts = _attested_search_distance_facts(
            property_url=property_url,
            distances={
                str(key): int(value)
                for key, value in raw_facts.items()
                if str(key).startswith("nearest_")
                and str(key).endswith("_m")
            },
        )
        facts.update(
            {
                key: value
                for key, value in raw_facts.items()
                if not (
                    str(key).startswith("nearest_")
                    and str(key).endswith("_m")
                )
            }
        )
        return {
            "listing_id": property_url.rsplit("/", 2)[-2],
            "title": f"Mietwohnung in 1020 Wien {property_url.rsplit('/', 2)[-2]}",
            "summary": "78 m2, 3 Zimmer, Gesamtmiete EUR 1.650, Balkon.",
            "property_facts_json": facts,
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)

    def _fake_fit(**kwargs) -> dict[str, object]:
        property_url = str(kwargs.get("property_url") or "")
        return {
            "fit_score": 54.0 if "soft-filter-a-" in property_url else 58.0,
            "recommendation": "review",
            "match_reasons_json": ["Hard search basics match"],
            "mismatch_reasons_json": ["Some soft preferences differ"],
            "unknowns_json": [],
        }

    monkeypatch.setattr(product_service, "_property_alert_personal_fit_from_facts", _fake_fit)
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})
    monkeypatch.setattr(
        ProductService,
        "_open_property_alert_review_with_timeout",
        lambda self, **kwargs: {
            "status": "opened",
            "editor_url": f"/app/research/{str(kwargs.get('source_ref') or 'candidate').split(':')[-1]}",
        },
    )

    hard_preferences = {
        "country_code": "AT",
        "listing_mode": "rent",
        "location_query": "1020 Vienna",
        "property_type": "apartment",
        "min_match_score": 95,
        "require_floorplan": False,
    }
    soft_preferences = {
        **hard_preferences,
        "max_distance_to_library_m": 500,
        "max_distance_to_library_importance": "strong_wish",
        "max_distance_to_playground_m": 500,
        "max_distance_to_playground_importance": "nice_to_have",
        "max_distance_to_shopping_center_m": 500,
        "max_distance_to_shopping_center_importance": "avoid",
        "max_distance_to_theatre_m": 700,
        "max_distance_to_theatre_importance": "strong_wish",
        "max_distance_to_supermarket_m": 300,
        "max_distance_to_supermarket_importance": "nice_to_have",
        "avoid_noise_risk_area": True,
        "require_high_speed_internet_evidence": True,
        "check_parking_situation": True,
        "avoid_flood_risk_area": True,
    }

    def _urls(result: dict[str, object]) -> set[str]:
        rows = list(
            dict(list(result.get("sources") or [])[0]).get(
                "research_candidates"
            )
            or []
        )
        return {str(row.get("property_url") or "") for row in rows}

    plain_result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences=hard_preferences,
        max_results_per_source=5,
        force_refresh=True,
    )
    cached_after_plain = service._property_public_preview_cache_index()
    assert set(cached_after_plain) == set(listing_urls)
    for cached_preview in cached_after_plain.values():
        cached_facts = dict(cached_preview.get("property_facts_json") or {})
        assert "map_lat" not in cached_facts
        assert "map_lng" not in cached_facts
        assert "map_coordinate_evidence" not in cached_facts
        assert "property_fact_evidence" not in cached_facts
        assert not any(
            str(key).startswith(("nearest_", "distance_", "map_"))
            for key in cached_facts
        )
    first_run_preview_calls = len(preview_calls)
    plain_cached_result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences=hard_preferences,
        max_results_per_source=5,
        force_refresh=False,
    )
    assert len(preview_calls) == first_run_preview_calls
    assert _urls(plain_cached_result) == set(listing_urls)
    for cached_row in list(
        dict(list(plain_cached_result.get("sources") or [])[0]).get(
            "research_candidates"
        )
        or []
    ):
        cached_candidate_facts = dict(
            dict(cached_row).get("property_facts") or {}
        )
        assert str(cached_candidate_facts.get("postal_name") or "").startswith(
            "1020 Wien"
        )
        assert cached_candidate_facts.get("rooms") == 3
        assert float(cached_candidate_facts.get("area_sqm") or 0) > 0
        assert float(cached_candidate_facts.get("total_rent_eur") or 0) > 0
        assert cached_candidate_facts.get("has_floorplan") is True
    soft_result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences=soft_preferences,
        max_results_per_source=5,
        force_refresh=False,
    )
    assert len(preview_calls) >= first_run_preview_calls + len(listing_urls)

    assert _urls(plain_result) == set(listing_urls)
    assert _urls(soft_result) == set(listing_urls)
    assert _urls(soft_result) == _urls(plain_result)
    soft_rows = list(dict(list(soft_result.get("sources") or [])[0]).get("research_candidates") or [])
    assert any(
        dict(dict(row).get("property_facts") or {}).get("distance_preference_notes")
        for row in soft_rows
    )


def test_property_search_neutral_filter_importance_keeps_distance_candidates(monkeypatch) -> None:
    principal_id = "exec-property-neutral-filter-preserve-candidates"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Neutral Filter Preserves Hits")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1220-brigittenau"
    listing_urls = [
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1220-brigittenau/neutral-filter-a/",
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1220-brigittenau/neutral-filter-b/",
    ]
    facts_by_url = {
        listing_urls[0]: {
            "postal_name": "1220 Wien",
            "area_sqm": 74,
            "rooms": 2,
            "total_rent_eur": 1300,
            "nearest_library_m": 2200,
            "nearest_supermarket_m": 2800,
            "nearest_playground_m": 1900,
        },
        listing_urls[1]: {
            "postal_name": "1220 Wien",
            "area_sqm": 68,
            "rooms": 2,
            "total_rent_eur": 1280,
            "nearest_library_m": 2600,
            "nearest_supermarket_m": 2500,
            "nearest_playground_m": 1700,
        },
    }

    monkeypatch.setattr(
        product_service,
        "_merged_property_scout_source_specs",
        lambda **kwargs: [
            {
                "url": source_url,
                "label": "Willhaben | Austria | Rent | 1220 Vienna",
                "platform": "willhaben",
                "provider_family": "marketplace",
                "country_code": "AT",
                "max_results": 6,
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": list(listing_urls),
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:neutral-filter-preserve"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        facts = dict(facts_by_url[property_url])
        return {
            "listing_id": property_url.rsplit("/", 2)[-2],
            "title": f"Mietwohnung in 1220 Wien {property_url.rsplit('/', 2)[-2]}",
            "summary": "68 m², 2 Zimmer, Gesamtmiete EUR 1.300, Balkon.",
            "property_facts_json": facts,
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)

    def _fake_fit(**kwargs) -> dict[str, object]:
        property_url = str(kwargs.get("property_url") or "")
        return {
            "fit_score": 52.0 if property_url.endswith("neutral-filter-a/") else 57.0,
            "recommendation": "review",
            "match_reasons_json": ["Hard search basics match"],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
        }

    monkeypatch.setattr(product_service, "_property_alert_personal_fit_from_facts", _fake_fit)
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})
    monkeypatch.setattr(
        ProductService,
        "_open_property_alert_review_with_timeout",
        lambda self, **kwargs: {
            "status": "opened",
            "editor_url": f"/app/research/{str(kwargs.get('source_ref') or 'candidate').split(':')[-1]}",
        },
    )

    base_preferences = {
        "country_code": "AT",
        "listing_mode": "rent",
        "location_query": "1220 Vienna",
        "property_type": "apartment",
        "require_floorplan": False,
    }
    neutral_preferences = {
        **base_preferences,
        "max_distance_to_library_m": 500,
        "max_distance_to_library_importance": "neutral",
        "max_distance_to_supermarket_m": 500,
        "max_distance_to_supermarket_importance": "neutral",
        "max_distance_to_playground_m": 500,
        "max_distance_to_playground_importance": "neutral",
    }

    neutral_result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences=neutral_preferences,
        max_results_per_source=6,
        force_refresh=True,
    )

    def _captured_urls(result: dict[str, object]) -> set[str]:
        rows = list(dict(list(result.get("sources") or [])[0]).get("research_candidates") or [])
        return {str(row.get("property_url") or "") for row in rows}

    assert _captured_urls(neutral_result) == set(listing_urls)
def test_property_search_filters_dirty_source_scope_postal_conflict_before_shortlist(monkeypatch) -> None:
    principal_id = "exec-property-run-dirty-postal-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Run Dirty Postal Gate Office")
    service = ProductService(client.app.state.container)

    source_url = "https://www.derstandard.at/immobilien/mieten/wien/1010"
    listing_url = "https://www.derstandard.at/immobilien/wohnung-1220-wien"
    monkeypatch.setattr(
        product_service,
        "_merged_property_scout_source_specs",
        lambda **kwargs: [
            {
                "url": source_url,
                "label": "DER STANDARD Immobilien | Austria | Rent | 1010 Vienna",
                "platform": "derstandard_at",
                "provider_family": "broker_portal",
                "country_code": "AT",
                "max_results": 1,
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("derstandard_at", source_url): {
                "listing_urls": [listing_url],
                "provider_cache_state": {"status": "miss", "cache_key": "derstandard:dirty-postal"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_scout_page_preview_with_timeout",
        lambda property_url, *, prefer_fast=False: {
            "listing_id": "derstandard-1220",
            "title": "Wohnung mieten in 1220 Wien | 60 m² | 2 Zimmer | € 1.090 | DER STANDARD",
            "summary": "2-Zimmer Wohnung mit Traumblick / UNO und U-Bahn ums Eck in 1220 Wien.",
            "property_facts_json": {
                "postal_name": "1010 Vienna",
                "source_scope_location": "1010 Vienna",
                "source_postal_code": "1010",
                "source_city": "Vienna",
                "area_sqm": 60,
                "rooms": 2,
                "total_rent_eur": 1090,
            },
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {
            "fit_score": 94.0,
            "recommendation": "strong_fit",
            "match_reasons_json": ["Would otherwise rank highly."],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
        },
    )
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("outside-area candidate must not notify")),
    )

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("derstandard_at",),
        property_search_preferences={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "property_type": "apartment",
            "min_match_score": 50,
            "require_floorplan": False,
        },
        max_results_per_source=1,
        force_refresh=True,
    )

    assert result["listing_total"] == 0
    assert result["high_fit_total"] == 0
    assert result["review_created_total"] == 0
    assert result["location_mismatch_candidate_total"] >= 1
    source = dict(result["sources"][0])
    assert source["raw_listing_total"] == 1
    assert source["scanned_listing_total"] == 1
    assert source["top_candidates"] == []
    assert source["location_mismatch_candidate_total"] >= 1
    assert source["location_mismatch_reason"] == "provider_returned_candidates_outside_selected_location"


def test_property_search_full_region_does_not_treat_generated_district_source_as_hard_scope(monkeypatch) -> None:
    principal_id = "exec-property-run-full-region-source-scope"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Run Full Region Source Scope Office")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen?isNavigation=true&q=1010+Vienna"
    listing_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/full-region-1020/"
    monkeypatch.setattr(
        product_service,
        "_merged_property_scout_source_specs",
        lambda **kwargs: [
            {
                "url": source_url,
                "label": "Willhaben | Austria | Rent | 1010 Vienna",
                "platform": "willhaben",
                "provider_family": "marketplace",
                "country_code": "AT",
                "max_results": 1,
                "notify_telegram": False,
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": [listing_url],
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:full-region-source-scope"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_scout_page_preview_with_timeout",
        lambda property_url, *, prefer_fast=False: {
            "listing_id": "full-region-1020",
            "title": "Wohnung mieten in 1020 Wien | 74 m² | 3 Zimmer",
            "summary": "Wohnung im 2. Bezirk, aber innerhalb der gewählten Stadt Wien.",
            "property_facts_json": {
                "postal_name": "1020 Wien",
                "area_sqm": 74,
                "rooms": 3,
                "total_rent_eur": 990,
            },
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {
            "fit_score": 72.0,
            "recommendation": "review",
            "match_reasons_json": ["The listing is inside Vienna."],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
        },
    )
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})
    monkeypatch.setattr(
        ProductService,
        "_open_property_alert_review_with_timeout",
        lambda self, **kwargs: {
            "status": "opened",
            "editor_url": "/app/research/full-region-1020",
        },
    )

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "Vienna",
            "full_region_scope": True,
            "property_type": "apartment",
            "min_match_score": 50,
            "require_floorplan": False,
        },
        max_results_per_source=1,
        force_refresh=True,
    )

    assert result["listing_total"] == 1
    source = dict(result["sources"][0])
    assert source["filtered_area_total"] == 0
    assert source["location_mismatch_candidate_total"] == 0
    assert [row["property_url"] for row in source["research_candidates"]] == [listing_url]


def test_property_search_area_keeps_source_scope_only_candidate_for_broad_city_scope() -> None:
    hints = _property_search_location_hints(
        {
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "Vienna",
            "full_region_scope": True,
        }
    )

    assert product_service._property_candidate_matches_search_area(
        location_hints=hints,
        request_preferences={
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "Vienna",
            "full_region_scope": True,
        },
        source_spec={"country_code": "AT"},
        property_url="https://www.willhaben.at/iad/object?adId=1775972917",
        title="#W2 Moderne Schone Zwei-Zimmer Wohnung",
        summary="Provider card from a Vienna source scope.",
        property_facts={
            "source_scope_location": "1020 Vienna",
            "source_postal_code": "1020",
            "source_city": "Vienna",
        },
    ) is True
    assert product_service._property_candidate_matches_search_area(
        location_hints=hints,
        request_preferences={
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "Vienna",
            "full_region_scope": True,
        },
        source_spec={"country_code": "AT"},
        property_url="https://www.willhaben.at/iad/object?adId=1775972918",
        title="Moderne Wohnung mit Seeblick in Gmunden",
        summary="Provider card from a Vienna source scope.",
        property_facts={
            "source_scope_location": "1020 Vienna",
            "source_postal_code": "1020",
            "source_city": "Vienna",
        },
    ) is False


def test_property_search_full_region_keeps_source_scope_only_sparse_city_card(monkeypatch) -> None:
    principal_id = "exec-property-run-full-region-source-scope-sparse-card"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Run Full Region Sparse Scope Office")
    service = ProductService(client.app.state.container)

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen?isNavigation=true&q=1010+Vienna"
    listing_url = "https://example.invalid/propertyquarry/source-scope-only-sparse-city-card"
    monkeypatch.setattr(
        product_service,
        "_merged_property_scout_source_specs",
        lambda **kwargs: [
            {
                "url": source_url,
                "label": "Willhaben | Austria | Rent | 1010 Vienna",
                "platform": "willhaben",
                "provider_family": "marketplace",
                "country_code": "AT",
                "max_results": 1,
                "notify_telegram": False,
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_interleave_by_provider_group", lambda specs: list(specs))
    monkeypatch.setattr(
        product_service,
        "_property_search_prefetch_listing_urls",
        lambda **kwargs: {
            ("willhaben", source_url): {
                "listing_urls": [listing_url],
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:full-region-source-scope-sparse-card"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_scout_page_preview_with_timeout",
        lambda property_url, *, prefer_fast=False: {
            "listing_id": "full-region-sparse-1020",
            "title": "#W2 Moderne Schone Zwei-Zimmer Wohnung",
            "summary": "78 m2, 2 Zimmer, Gesamtmiete EUR 1.450.",
            "property_facts_json": {
                "area_sqm": 78,
                "rooms": 2,
                "total_rent_eur": 1450,
            },
        },
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {
            "fit_score": 68.0,
            "recommendation": "review",
            "match_reasons_json": ["The source scope stays inside Vienna."],
            "mismatch_reasons_json": [],
            "unknowns_json": [],
        },
    )
    monkeypatch.setattr(ProductService, "_warm_property_public_preview_cache_for_sources", lambda self, **kwargs: {})
    monkeypatch.setattr(
        ProductService,
        "_open_property_alert_review_with_timeout",
        lambda self, **kwargs: {
            "status": "opened",
            "editor_url": "/app/research/full-region-sparse-1020",
        },
    )

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "Vienna",
            "full_region_scope": True,
            "property_type": "apartment",
            "min_match_score": 50,
            "require_floorplan": False,
        },
        max_results_per_source=1,
        force_refresh=True,
    )

    assert result["listing_total"] == 1
    source = dict(result["sources"][0])
    assert source["location_mismatch_candidate_total"] == 0
    assert [row["property_url"] for row in source["research_candidates"]] == [listing_url]


def test_property_search_type_filter_blocks_garage_for_residential_searches() -> None:
    garage_title = "Garagenplatz zu vermieten, 10 m2, EUR 190,-, (1030 Wien) - willhaben"

    assert (
        product_service._property_candidate_matches_requested_property_type(
            property_type="apartment",
            property_url="https://www.willhaben.at/iad/object?adId=1835567057",
            title=garage_title,
            summary="Garagenplatz zu vermieten.",
            property_facts={},
        )
        is False
    )
    assert (
        product_service._property_candidate_matches_requested_property_type(
            property_type="house",
            property_url="https://www.willhaben.at/iad/object?adId=1835567057",
            title=garage_title,
            summary="Garagenplatz zu vermieten.",
            property_facts={},
        )
        is False
    )
    assert (
        product_service._property_candidate_matches_requested_property_type(
            property_type="apartment",
            property_url="https://www.willhaben.at/iad/object?adId=1",
            title="Wohnung mit Balkon und optionalem Garagenplatz",
            summary="Helle Wohnung, Lift, Terrasse, Garagenplatz optional anmietbar.",
            property_facts={"property_type": "apartment"},
        )
        is True
    )
    assert (
        product_service._property_candidate_matches_requested_property_type(
            property_type="apartment",
            property_url="https://www.immmo.at/expose/praxis",
            title="Großzügige Praxisfläche in gepflegtem Zustand",
            summary="Ideal für medizinische Nutzung.",
            property_facts={},
        )
        is False
    )
    assert (
        product_service._property_candidate_matches_requested_property_type(
            property_type="office",
            property_url="https://www.immmo.at/expose/praxis",
            title="Großzügige Praxisfläche in gepflegtem Zustand",
            summary="Ideal für medizinische Nutzung.",
            property_facts={},
        )
        is True
    )
    assert (
        product_service._property_candidate_matches_requested_property_type(
            property_type="apartment",
            property_url="https://www.willhaben.at/iad/immobilien/d/gewerbeimmobilien/buero",
            title="Bürofläche mit Balkon nahe U-Bahn",
            summary="Gewerbefläche mit Teeküche, Besprechungszimmern und Lift.",
            property_facts={"property_type": "office"},
        )
        is False
    )
    assert (
        product_service._property_candidate_matches_requested_property_type(
            property_type="any",
            property_url="https://www.immmo.at/expose/praxis",
            title="Großzügige Praxisfläche in gepflegtem Zustand",
            summary="Ideal für medizinische Nutzung.",
            property_facts={},
        )
        is False
    )


def test_property_search_type_filter_supports_building_land() -> None:
    assert (
        product_service._property_candidate_matches_requested_property_type(
            property_type="land",
            property_url="https://www.willhaben.at/iad/object?adId=land-one",
            title="Baugrundstück mit Seezugang in Niederösterreich",
            summary="Bauland, aufgeschlossen, ruhige Lage.",
            property_facts={},
        )
        is True
    )
    assert (
        product_service._property_candidate_matches_requested_property_type(
            property_type="land",
            property_url="https://www.willhaben.at/iad/object?adId=flat-one",
            title="Wohnung mit Garten und Balkon",
            summary="Helle Wohnung, kein Baugrund.",
            property_facts={"property_type": "apartment"},
        )
        is False
    )


def test_property_scout_listing_url_cache_reuses_provider_result_lists(monkeypatch) -> None:
    with product_service._PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        product_service._PROPERTY_SOURCE_LISTING_CACHE.clear()
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = ""
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = 0.0
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_BACKEND", "memory")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_PATH", "")
    fetch_calls: list[str] = []

    def _fake_fetch_html(url: str, *, timeout_seconds: float = 60.0) -> str:
        fetch_calls.append(url)
        return "<html>provider source</html>"

    def _fake_extract_listing_urls(
        *,
        source_url: str,
        html: str,
        source_spec: dict[str, object] | None = None,
    ) -> tuple[str, ...]:
        return (
            "https://www.willhaben.at/iad/object?adId=cache-1",
            "https://www.willhaben.at/iad/object?adId=cache-2",
        )

    monkeypatch.setattr(product_service, "_property_scout_fetch_html", _fake_fetch_html)
    monkeypatch.setattr(product_service, "_property_scout_extract_listing_urls", _fake_extract_listing_urls)
    source_spec = {
        "platform": "willhaben",
        "provider_cache_key": "willhaben:test-cache-key",
        "provider_filter_pushdown": {"cache_key": "willhaben:test-cache-key"},
    }

    first_urls, first_cache = product_service._property_scout_listing_urls_for_source(
        source_url="https://www.willhaben.at/iad/immobilien/mietwohnungen?ESTATE_SIZE%2FLIVING_AREA_FROM=80",
        source_spec=source_spec,
    )
    second_urls, second_cache = product_service._property_scout_listing_urls_for_source(
        source_url="https://www.willhaben.at/iad/immobilien/mietwohnungen?ESTATE_SIZE%2FLIVING_AREA_FROM=80",
        source_spec=source_spec,
    )
    refreshed_urls, refreshed_cache = product_service._property_scout_listing_urls_for_source(
        source_url="https://www.willhaben.at/iad/immobilien/mietwohnungen?ESTATE_SIZE%2FLIVING_AREA_FROM=80",
        source_spec=source_spec,
        force_refresh=True,
    )

    assert first_cache["status"] == "miss"
    assert second_cache["status"] == "hit"
    assert refreshed_cache["status"] == "refresh"
    assert first_urls == second_urls == refreshed_urls
    assert len(fetch_calls) == 2


def test_property_scout_listing_url_cache_persists_provider_result_lists(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "provider-listings.json"
    with product_service._PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        product_service._PROPERTY_SOURCE_LISTING_CACHE.clear()
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = ""
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = 0.0
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_BACKEND", "file")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_PATH", str(cache_path))
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_STALE_MAX_SECONDS", "3600")
    fetch_calls: list[str] = []

    def _fake_fetch_html(url: str, *, timeout_seconds: float = 60.0) -> str:
        fetch_calls.append(url)
        return "<html>provider source</html>"

    def _fake_extract_listing_urls(
        *,
        source_url: str,
        html: str,
        source_spec: dict[str, object] | None = None,
    ) -> tuple[str, ...]:
        return (
            "https://www.willhaben.at/iad/object?adId=persist-1",
            "https://www.willhaben.at/iad/object?adId=persist-2",
        )

    monkeypatch.setattr(product_service, "_property_scout_fetch_html", _fake_fetch_html)
    monkeypatch.setattr(product_service, "_property_scout_extract_listing_urls", _fake_extract_listing_urls)
    source_spec = {
        "platform": "willhaben",
        "provider_cache_key": "willhaben:persistent-cache-key",
        "provider_filter_pushdown": {"cache_key": "willhaben:persistent-cache-key"},
    }

    first_urls, first_cache = product_service._property_scout_listing_urls_for_source(
        source_url="https://www.willhaben.at/iad/immobilien/mietwohnungen?ESTATE_SIZE%2FLIVING_AREA_FROM=90",
        source_spec=source_spec,
    )
    assert first_cache["status"] == "miss"
    assert first_cache["persistence"] == "file"
    assert cache_path.exists()

    with product_service._PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        product_service._PROPERTY_SOURCE_LISTING_CACHE.clear()
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = ""
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = 0.0

    def _blocked_fetch_html(url: str, *, timeout_seconds: float = 60.0) -> str:
        raise AssertionError("persistent provider-list cache should satisfy this request")

    monkeypatch.setattr(product_service, "_property_scout_fetch_html", _blocked_fetch_html)
    second_urls, second_cache = product_service._property_scout_listing_urls_for_source(
        source_url="https://www.willhaben.at/iad/immobilien/mietwohnungen?ESTATE_SIZE%2FLIVING_AREA_FROM=90",
        source_spec=source_spec,
    )

    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    assert persisted["version"] == "property_source_listing_cache_v1"
    assert persisted["schema_version"] == 1
    assert persisted["entry_count"] == 1
    assert persisted["lock_strategy"] == "fcntl"
    assert "willhaben:persistent-cache-key" in persisted["entries"]
    assert cache_path.with_name(f"{cache_path.name}.lock").exists()
    assert second_cache["status"] == "hit"
    assert second_cache["persistence"] == "file"
    assert second_urls == first_urls
    assert len(fetch_calls) == 1


def test_property_scout_listing_url_cache_uses_source_fallback_when_provider_fetch_fails(monkeypatch) -> None:
    with product_service._PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        product_service._PROPERTY_SOURCE_LISTING_CACHE.clear()
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = ""
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = 0.0
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_BACKEND", "memory")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_PATH", "")
    observed: dict[str, object] = {}

    def _blocked_fetch_html(url: str, *, timeout_seconds: float = 60.0) -> str:
        observed["timeout_seconds"] = timeout_seconds
        raise TimeoutError("remax upstream timeout")

    monkeypatch.setattr(product_service, "_property_scout_fetch_html", _blocked_fetch_html)
    source_spec = {
        "platform": "remax_at",
        "provider_cache_key": "remax_at:fallback-cache-key",
        "provider_filter_pushdown": {"cache_key": "remax_at:fallback-cache-key"},
        "fetch_timeout_seconds": 8,
        "fallback_listing_urls": ["https://www.remax.at/de/ib/remax-first-wien/immobilien"],
    }

    urls, cache_state = product_service._property_scout_listing_urls_for_source(
        source_url="https://www.remax.at/en/properties/propertysearch?q=Wien&minArea=35",
        source_spec=source_spec,
        force_refresh=True,
    )

    assert observed["timeout_seconds"] == 8
    assert urls == ("https://www.remax.at/de/ib/remax-first-wien/immobilien",)
    assert cache_state["status"] == "fallback"
    assert cache_state["fallback_reason"] == "source_fetch_failed"


def test_property_scout_listing_url_cache_merges_existing_persistent_entries(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "provider-listings.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": "property_source_listing_cache_v1",
                "entries": {
                    "willhaben:other-worker-key": {
                        "cache_key": "willhaben:other-worker-key",
                        "source_url": "https://www.willhaben.at/iad/immobilien/mietwohnungen?q=other",
                        "listing_urls": ["https://www.willhaben.at/iad/object?adId=other-1"],
                        "stored_at_epoch": time.time(),
                        "provider_filter_pushdown": {"cache_key": "willhaben:other-worker-key"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with product_service._PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        product_service._PROPERTY_SOURCE_LISTING_CACHE.clear()
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = ""
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = 0.0
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_BACKEND", "file")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_PATH", str(cache_path))
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_STALE_MAX_SECONDS", "3600")

    product_service._property_source_listing_cache_put(
        "willhaben:this-worker-key",
        source_url="https://www.willhaben.at/iad/immobilien/mietwohnungen?q=this",
        listing_urls=("https://www.willhaben.at/iad/object?adId=this-1",),
        source_spec={"provider_filter_pushdown": {"cache_key": "willhaben:this-worker-key"}},
    )

    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "willhaben:other-worker-key" in persisted["entries"]
    assert "willhaben:this-worker-key" in persisted["entries"]
    assert persisted["entry_count"] == 2


def test_property_scout_listing_url_cache_quarantines_corrupt_persistent_snapshot(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "provider-listings.json"
    cache_path.write_text("{not valid json", encoding="utf-8")
    with product_service._PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        product_service._PROPERTY_SOURCE_LISTING_CACHE.clear()
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = ""
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = 0.0
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_BACKEND", "file")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_PATH", str(cache_path))
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_STALE_MAX_SECONDS", "3600")

    product_service._property_source_listing_cache_put(
        "willhaben:recovered-cache-key",
        source_url="https://www.willhaben.at/iad/immobilien/mietwohnungen?q=recovered",
        listing_urls=("https://www.willhaben.at/iad/object?adId=recovered-1",),
        source_spec={"provider_filter_pushdown": {"cache_key": "willhaben:recovered-cache-key"}},
    )

    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    corrupt_files = sorted(tmp_path.glob("provider-listings.json.corrupt-*.json"))
    assert corrupt_files
    assert persisted["version"] == "property_source_listing_cache_v1"
    assert persisted["schema_version"] == 1
    assert persisted["lock_strategy"] == "fcntl"
    assert "willhaben:recovered-cache-key" in persisted["entries"]


def test_property_scout_listing_url_cache_rejects_overstale_persistent_fallback(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "provider-listings.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": "property_source_listing_cache_v1",
                "entries": {
                    "willhaben:old-cache-key": {
                        "cache_key": "willhaben:old-cache-key",
                        "source_url": "https://www.willhaben.at/iad/immobilien/mietwohnungen",
                        "listing_urls": ["https://www.willhaben.at/iad/object?adId=old-1"],
                        "stored_at_epoch": time.time() - 3600,
                        "provider_filter_pushdown": {"cache_key": "willhaben:old-cache-key"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with product_service._PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        product_service._PROPERTY_SOURCE_LISTING_CACHE.clear()
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = ""
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = 0.0
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_BACKEND", "file")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_PATH", str(cache_path))
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_TTL_SECONDS", "1")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_STALE_MAX_SECONDS", "60")

    cached_urls, cached_state = product_service._property_source_listing_cache_get(
        "willhaben:old-cache-key",
        allow_stale=True,
    )

    assert cached_urls == ()
    assert cached_state == {}


def test_hosted_property_tour_bundle_reuses_existing_manifest(monkeypatch, tmp_path) -> None:
    from app.product import property_tour_hosting

    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    title = "Reusable listing"
    listing_id = "reuse-1"
    property_url = "https://www.willhaben.at/iad/object?adId=reuse-1"
    variant_key = "layout_first"
    principal_id = "exec-reuse"
    slug = product_service._hosted_property_tour_slug(
        title=title,
        listing_id=listing_id,
        property_url=property_url,
        variant_key=variant_key,
        principal_id=principal_id,
    )
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "floorplan-01.pdf").write_bytes(b"%PDF-1.4\n")
    property_tour_hosting._write_hosted_property_tour_payload(
        bundle_dir,
        {
            "slug": slug,
            "principal_id": principal_id,
            "property_url": property_url,
            "hosted_url": f"https://propertyquarry.com/tours/{slug}",
            "public_url": f"https://propertyquarry.com/tours/{slug}",
            "creation_mode": "hosted_floorplan_tour",
            "scenes": [
                {
                    "asset_relpath": "floorplan-01.pdf",
                    "role": "floorplan",
                    "privacy_class": "floorplan_pdf_public",
                    "mime_type": "application/pdf",
                }
            ],
        },
    )

    def _blocked_download(*args, **kwargs) -> str:
        raise AssertionError("existing hosted tour should not download assets again")

    monkeypatch.setattr(product_service, "_download_public_tour_asset_with_type", _blocked_download)

    payload = product_service._write_hosted_floorplan_property_tour_bundle(
        principal_id=principal_id,
        title=title,
        listing_id=listing_id,
        property_url=property_url,
        variant_key=variant_key,
        floorplan_urls=("https://cdn.example.com/floorplan.pdf",),
        property_facts_json={},
        source_host="willhaben.at",
    )


def test_hosted_property_tour_bundle_splits_public_manifest_from_private_receipt(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://propertyquarry.com/tours")

    def _fake_download(url: str, target) -> str:
        target.write_bytes(b"%PDF-1.4\n")
        return "application/pdf"

    monkeypatch.setattr("app.product.property_tour_hosting._download_public_tour_asset_with_type", _fake_download)

    payload = product_service._write_hosted_floorplan_property_tour_bundle(
        principal_id="exec-private-tour",
        title="Private floorplan tour",
        listing_id="private-floorplan-1",
        property_url="https://www.willhaben.at/iad/object?adId=private-floorplan-1",
        variant_key="layout_first",
        floorplan_urls=("https://cdn.example.com/floorplan.pdf",),
        property_facts_json={
            "address_lines": ["1200 Wien"],
            "exact_address": "Private Street 1, 1200 Wien",
            "map_lat": 48.2,
            "map_lng": 16.3,
            "personal_fit_assessment": {
                "fit_score": 81,
                "good_fit_reasons": ["Strong layout signal"],
                "preference_nodes": [{"key": "private-node"}],
            },
            "public_preference_snapshot": {
                "profile": {"principal_id": "exec-private-tour"},
                "preference_nodes": [{"key": "prefer_balcony", "value_json": True}],
            },
        },
        source_host="willhaben.at",
        source_ref="property-scout:private-floorplan-1",
        external_id="ext-private-floorplan-1",
        recipient_email="anna@example.com",
    )

    bundle_dir = tmp_path / str(payload["slug"])
    public_manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    private_manifest = json.loads((bundle_dir / "tour.private.json").read_text(encoding="utf-8"))

    assert public_manifest["hosted_url"] == f"/tours/{payload['slug']}"
    assert "principal_id" not in public_manifest
    assert "recipient_email" not in public_manifest
    assert "source_ref" not in public_manifest
    assert "external_id" not in public_manifest
    assert "listing_url" not in public_manifest
    assert "property_url" not in public_manifest
    public_manifest_keys = _property_tour_manifest_nested_keys(public_manifest)
    for private_key in (
        "map_lat",
        "map_lng",
        "listing_url",
        "property_url",
        "source_url",
        "exact_address",
        "principal_id",
        "source_ref",
        "external_id",
        "private_recipient_email",
        "recipient_email",
        "public_preference_snapshot",
        "preference_nodes",
    ):
        assert private_key not in public_manifest_keys
    assert {
        key for key in public_manifest_keys if "property_url" in key
    } == {"property_url_sha256"}
    assert public_manifest["property_url_sha256"] == hashlib.sha256(
        b"https://www.willhaben.at/iad/object?adId=private-floorplan-1"
    ).hexdigest()
    serialized_public_manifest = json.dumps(public_manifest, sort_keys=True)
    for private_marker in (
        "exec-private-tour",
        "anna@example.com",
        "property-scout:private-floorplan-1",
        "ext-private-floorplan-1",
        "Private Street 1",
        "https://www.willhaben.at/iad/object?adId=private-floorplan-1",
    ):
        assert private_marker not in serialized_public_manifest
    assert private_manifest["principal_id"] == "exec-private-tour"
    assert private_manifest["recipient_email"] == "anna@example.com"
    assert private_manifest["source_ref"] == "property-scout:private-floorplan-1"
    assert private_manifest["external_id"] == "ext-private-floorplan-1"


def test_hosted_property_tour_public_manifest_has_no_private_fields(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://propertyquarry.com/tours")

    def _fake_download(url: str, target) -> str:
        target.write_bytes(b"%PDF-1.4\n")
        return "application/pdf"

    monkeypatch.setattr("app.product.property_tour_hosting._download_public_tour_asset_with_type", _fake_download)

    payload = product_service._write_hosted_floorplan_property_tour_bundle(
        principal_id="exec-manifest-safety",
        title="Manifest-safe floorplan tour",
        listing_id="safety-floorplan-1",
        property_url="https://www.willhaben.at/iad/object?adId=private-floorplan-2",
        variant_key="layout_first",
        floorplan_urls=("https://cdn.example.com/floorplan.pdf",),
        property_facts_json={
            "address_lines": ["1200 Wien"],
            "map_lat": 48.2,
            "map_lng": 16.3,
            "exact_address": "Private Street 12, 1200 Wien",
            "source_url": "https://www.willhaben.at/iad/object?adId=private-floorplan-2",
        },
        source_host="willhaben.at",
        source_ref="property-scout:private-floorplan-2",
        external_id="ext-private-floorplan-2",
        recipient_email="private@example.com",
    )

    bundle_dir = tmp_path / str(payload["slug"])
    public_manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    serialized_public_manifest = json.dumps(public_manifest, sort_keys=True)
    assert "brief" not in public_manifest
    public_manifest_keys = _property_tour_manifest_nested_keys(public_manifest)
    for private_key in (
        "map_lat",
        "map_lng",
        "listing_url",
        "property_url",
        "source_url",
        "exact_address",
        "principal_id",
        "source_ref",
        "external_id",
        "private_recipient_email",
        "recipient_email",
        "public_preference_snapshot",
        "preference_nodes",
    ):
        assert private_key not in public_manifest_keys
    assert {
        key for key in public_manifest_keys if "property_url" in key
    } == {"property_url_sha256"}
    assert public_manifest["property_url_sha256"] == hashlib.sha256(
        b"https://www.willhaben.at/iad/object?adId=private-floorplan-2"
    ).hexdigest()
    for private_value in (
        "exec-manifest-safety",
        "private@example.com",
        "property-scout:private-floorplan-2",
        "ext-private-floorplan-2",
        "Private Street 12",
        "https://www.willhaben.at/iad/object?adId=private-floorplan-2",
    ):
        assert private_value not in serialized_public_manifest


def test_hosted_live_provider_tour_writer_rejects_url_without_provenance_proof(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://propertyquarry.com/tours")

    live_url = "https://example.3dvista.com/tours/top22/index.html"
    with pytest.raises(RuntimeError, match="property_tour_output_unverified"):
        product_service._write_hosted_feelestate_pure_360_property_tour_bundle(
            principal_id="exec-live-provider-private",
            title="3DVista writer coverage",
            listing_id="3dvista-writer-1",
            property_url="https://www.willhaben.at/iad/object?adId=3dvista-writer-1",
            variant_key="layout_first",
            source_virtual_tour_url=live_url,
            property_facts_json={
                "has_360": True,
                "exact_address": "Private 3DVista Street 1, 1200 Wien",
                "map_lat": 48.2,
                "map_lng": 16.3,
            },
            source_host="willhaben.at",
            source_ref="property-scout:3dvista-writer-1",
            external_id="ext-3dvista-writer-1",
            recipient_email="owner@example.com",
        )

    assert list(tmp_path.iterdir()) == []


def test_hosted_property_tour_bundle_rejects_post_download_invalid_asset_suffix(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://propertyquarry.com/tours")

    def _fake_download(url: str, target) -> str:
        target.write_text("<html>not a floorplan</html>", encoding="utf-8")
        return "application/octet-stream"

    monkeypatch.setattr("app.product.property_tour_hosting._download_public_tour_asset_with_type", _fake_download)

    with pytest.raises(RuntimeError, match="floorplan_assets_unavailable"):
        product_service._write_hosted_floorplan_property_tour_bundle(
            principal_id="exec-invalid-floorplan-suffix",
            title="Bad floorplan asset",
            listing_id="bad-floorplan-1",
            property_url="https://www.willhaben.at/iad/object?adId=bad-floorplan-1",
            variant_key="layout_first",
            floorplan_urls=("https://cdn.example.com/floorplan.html",),
            property_facts_json={},
            source_host="willhaben.at",
        )


@pytest.mark.parametrize(
    ("asset_url", "content_type"),
    (
        ("https://cdn.example.com/floorplan.html", "text/html"),
        ("https://cdn.example.com/floorplan.zip", "application/octet-stream"),
        ("https://cdn.example.com/floorplan.json", "application/octet-stream"),
    ),
)
def test_hosted_property_tour_bundle_rejects_hostile_asset_suffix_after_content_type_detection(
    monkeypatch, tmp_path, asset_url, content_type
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://propertyquarry.com/tours")

    def _fake_download(url: str, target) -> str:
        target.write_text("<html>not a floorplan</html>", encoding="utf-8")
        return content_type

    monkeypatch.setattr("app.product.property_tour_hosting._download_public_tour_asset_with_type", _fake_download)

    with pytest.raises(RuntimeError, match="floorplan_assets_unavailable"):
        product_service._write_hosted_floorplan_property_tour_bundle(
            principal_id="exec-invalid-floorplan-suffix",
            title="Bad floorplan asset",
            listing_id="bad-floorplan-2",
            property_url="https://www.willhaben.at/iad/object?adId=bad-floorplan-2",
            variant_key="layout_first",
            floorplan_urls=(asset_url,),
            property_facts_json={},
            source_host="willhaben.at",
        )


def test_public_tour_asset_download_enforces_max_bytes(monkeypatch, tmp_path) -> None:
    class _FakeResponse:
        def __init__(self) -> None:
            self.headers = {"Content-Type": "application/pdf", "Content-Length": "8"}
            self._chunks = [b"1234", b"5678"]

        def read(self, size: int = -1) -> bytes:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setenv("PROPERTYQUARRY_TOUR_ASSET_MAX_BYTES", "4")
    monkeypatch.setattr("app.product.outbound_url_security.open_guarded_url", lambda *args, **kwargs: _FakeResponse())

    with pytest.raises(RuntimeError, match="tour_asset_too_large"):
        product_service._download_public_tour_asset_with_type(
            "https://cdn.example.com/floorplan.pdf",
            tmp_path / "floorplan.pdf",
        )


def test_property_alert_review_reuses_returned_review_packet() -> None:
    principal_id = "exec-property-review-packet-reuse"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Review Reuse Office")
    seed_product_state(client, principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    property_url = "https://www.willhaben.at/iad/object?adId=reuse-returned-1"

    first = service._open_property_alert_review(
        principal_id=principal_id,
        title="Reusable returned review flat",
        summary="A completed review packet should remain reusable.",
        source_ref="property-scout:reuse-returned-1",
        external_id=property_url,
        counterparty="Willhaben",
        account_email="",
        property_url=property_url,
        actor="test",
        notify_telegram=False,
        personal_fit_assessment={"fit_score": 76.0, "recommendation": "shortlist"},
        preference_person_id="self",
        tour_url="https://propertyquarry.com/tours/reuse-returned-1",
    )
    task_id = str(first["human_task_id"]).split(":", 1)[1]
    returned = client.app.state.container.orchestrator.return_human_task(
        task_id,
        principal_id=principal_id,
        operator_id="operator-office",
        resolution="reviewed",
        returned_payload_json={"resolution": "reviewed"},
        provenance_json={"source": "test"},
    )
    assert returned is not None
    assert returned.status == "returned"

    second = service._open_property_alert_review(
        principal_id=principal_id,
        title="Reusable returned review flat",
        summary="Same listing in a later search.",
        source_ref="property-scout:reuse-returned-1",
        external_id=property_url,
        counterparty="Willhaben",
        account_email="",
        property_url=property_url,
        actor="test",
        notify_telegram=False,
        personal_fit_assessment={"fit_score": 78.0, "recommendation": "shortlist"},
        preference_person_id="self",
        tour_url="https://propertyquarry.com/tours/reuse-returned-1-refresh",
    )

    all_reviews = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_alert_review"
    ]
    assert second["status"] == "existing"
    assert second["human_task_id"] == first["human_task_id"]
    assert second["review_task_status"] == "returned"
    assert second["review_reused"] is True
    assert second["tour_url"] == "https://propertyquarry.com/tours/reuse-returned-1-refresh"
    assert len(all_reviews) == 1
    events = client.get("/app/api/events", params={"channel": "product", "event_type": "property_alert_review_reused"})
    assert events.status_code == 200
    reused_events = [
        item
        for item in events.json()["items"]
        if item["payload"]["human_task_id"] == first["human_task_id"]
    ]
    assert reused_events
    assert reused_events[0]["payload"]["review_task_status"] == "returned"


def test_property_alert_review_suppresses_candidate_outside_active_location() -> None:
    principal_id = "exec-property-alert-location-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Review Location Gate Office")
    seed_product_state(client, principal_id=principal_id)
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben", "flatbee"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    service = product_service.build_product_service(client.app.state.container)

    result = service._open_property_alert_review(
        principal_id=principal_id,
        title="Familienfreundliche 3-Zimmer-Wohnung im Zentrum von Gmunden",
        summary="Provider result was queried from a Vienna source scope.",
        source_ref="property-scout:https://www.flatbee.at/properties/searchengine_property_detail/d05ee215-Gmunden",
        external_id="flatbee-gmunden",
        counterparty="Flatbee",
        account_email="",
        property_url="https://www.flatbee.at/properties/searchengine_property_detail/d05ee215-Gmunden",
        actor="test",
        notify_telegram=True,
        candidate_properties=(
            {
                "property_url": "https://www.flatbee.at/properties/searchengine_property_detail/d05ee215-Gmunden",
                "listing_title": "Familienfreundliche 3-Zimmer-Wohnung im Zentrum von Gmunden - Oberösterreich - 4810",
                "property_facts_json": {"postal_name": "4810 Gmunden", "source_scope_location": "Wien", "source_city": "Wien"},
            },
        ),
        personal_fit_assessment={"fit_score": 92.0, "recommendation": "shortlist"},
        preference_person_id="self",
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    assert not [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_alert_review"
    ]
    events = client.get("/app/api/events", params={"channel": "product", "event_type": "property_alert_review_suppressed_location_mismatch"})
    assert events.status_code == 200
    assert any("Gmunden" in str(item["payload"]) for item in events.json()["items"])


def test_property_alert_review_suppresses_candidate_outside_selected_district_even_when_location_query_is_broad(monkeypatch) -> None:
    principal_id = "exec-property-alert-selected-district-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Review District Gate Office")
    seed_product_state(client, principal_id=principal_id)
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_districts": ["1010 Vienna"],
            "selected_platforms": ["derstandard_at"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    monkeypatch.setattr(
        product_service.ProductService,
        "_fetch_property_provider_repair_snapshot",
        lambda self, *, property_url: {},
    )
    service = product_service.build_product_service(client.app.state.container)

    result = service._open_property_alert_review(
        principal_id=principal_id,
        title="Wohnung mieten in 1200 Wien, Brigittenau | 81.98 m² | 3 Zimmer | EUR 1.649",
        summary="Provider result was queried from a selected 1010 source scope.",
        source_ref="property-scout:https://immobilien.derstandard.at/detail/1200-brigittenau",
        external_id="derstandard-1200",
        counterparty="DER STANDARD Immobilien | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://immobilien.derstandard.at/detail/1200-brigittenau",
        actor="test",
        notify_telegram=True,
        candidate_properties=(
            {
                "property_url": "https://immobilien.derstandard.at/detail/1200-brigittenau",
                "listing_title": "Wohnung mieten in 1200 Wien, Brigittenau | 81.98 m² | 3 Zimmer | EUR 1.649",
                "summary": "Stilvolle 3-Zimmer-Wohnung mit Garten & Terrasse im 20. Bezirk.",
                "property_facts_json": {
                    "postal_name": "1200 Wien",
                    "source_scope_location": "1010 Vienna",
                    "source_city": "Vienna",
                },
            },
        ),
        personal_fit_assessment={"fit_score": 92.0, "recommendation": "shortlist"},
        preference_person_id="self",
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    assert dict(result["repair_task"])["task_type"] == "property_provider_repair_ooda"
    assert dict(result["repair_task"])["filter_key"] == "location_scope"
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks
    repair_input = dict(repair_tasks[0].input_json or {})
    assert repair_input["filter_key"] == "location_scope"
    assert dict(repair_input["diagnostics"])["postal_name"] == "1200 Wien"
    assert dict(repair_input["diagnostics"])["location_hints"] == ["1010 Vienna"]


def test_property_alert_review_accepts_search_run_candidate_inside_adjacent_area_radius(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-alert-adjacent-radius-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Review Adjacent Radius Gate Office")
    seed_product_state(client, principal_id=principal_id)
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_districts": ["1010 Vienna"],
            "adjacent_area_radius_m": 200,
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    boundary_geojson = {
        "type": "Polygon",
        "coordinates": [[
            [16.3600, 48.2000],
            [16.3700, 48.2000],
            [16.3700, 48.2100],
            [16.3600, 48.2100],
            [16.3600, 48.2000],
        ]],
    }
    monkeypatch.setattr(
        product_service,
        "_property_research_boundary_record",
        lambda query: {
            "display_name": query,
            "geojson": boundary_geojson,
            "bounds": (16.3600, 48.2000, 16.3700, 48.2100),
            "lat": 48.2050,
            "lon": 16.3650,
        },
    )
    monkeypatch.setattr(
        product_service.ProductService,
        "_fetch_property_provider_repair_snapshot",
        lambda self, *, property_url: pytest.fail("adjacent-radius match must not open provider repair"),
    )
    service = product_service.build_product_service(client.app.state.container)

    result = service._open_property_alert_review(
        principal_id=principal_id,
        title="Wohnung mieten in 1200 Wien | 70 m² | 3 Zimmer | EUR 1.450",
        summary="Listing text is outside 1010 but the map point sits just over the selected-area boundary.",
        source_ref="property-scout:adjacent-radius-1200",
        external_id="willhaben-adjacent-radius-1200",
        counterparty="Willhaben | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1200-brigittenau/adjacent-radius/",
        actor="test",
        notify_telegram=False,
        candidate_properties=(
            {
                "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1200-brigittenau/adjacent-radius/",
                "listing_title": "Wohnung mieten in 1200 Wien | 70 m² | 3 Zimmer | EUR 1.450",
                "summary": "Helle Wohnung in 1200 Wien, knapp neben dem Suchgebiet.",
                "property_facts_json": {
                    "postal_name": "1200 Wien",
                    "map_lat": 48.2050,
                    "map_lng": 16.3714,
                    "source_scope_location": "1010 Vienna",
                    "source_city": "Vienna",
                },
            },
        ),
        personal_fit_assessment={"fit_score": 92.0, "recommendation": "shortlist"},
        preference_person_id="self",
        requested_location_hints=("1010 Vienna",),
        requested_country_code="AT",
        requested_region_code="vienna",
    )

    assert result["status"] == "opened"
    assert result.get("reason") != "property_location_conflicts_with_active_search"
    assert [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_alert_review"
    ]


def test_property_alert_review_suppresses_salzburg_listing_under_vienna_source_scope(monkeypatch) -> None:
    principal_id = "exec-property-alert-salzburg-source-scope"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Salzburg Source Scope Guard")
    seed_product_state(client, principal_id=principal_id)
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_districts": ["1010 Vienna"],
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: pytest.fail("Salzburg listing under 1010 source scope must not notify"),
    )
    monkeypatch.setattr(
        product_service.ProductService,
        "_fetch_property_provider_repair_snapshot",
        lambda self, *, property_url: {},
    )
    service = product_service.build_product_service(client.app.state.container)

    result = service._open_property_alert_review(
        principal_id=principal_id,
        title="#W2 Moderne Schöne Zwei-Zimmer Wohnung mit Terrasse in Salzburg",
        summary="Moderne schöne Zwei-Zimmer Wohnung mit großem Ess- & Wohnbereich in Salzburg Stadt.",
        source_ref="property-scout:https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/demo-1631373932/",
        external_id="willhaben-salzburg-source-scope",
        counterparty="Willhaben | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/demo-1631373932/",
        actor="test",
        notify_telegram=True,
        candidate_properties=(
            {
                "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/demo-1631373932/",
                "listing_title": "#W2 Moderne Schöne Zwei-Zimmer Wohnung mit Terrasse in Salzburg",
                "summary": "Moderne Zwei-Zimmer Wohnung mit Terrasse in Salzburg Stadt.",
                "source_platform": "willhaben",
                "source_family": "core_portal",
                "property_facts_json": {
                    "postal_name": "1010 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "source_postal_code": "1010",
                    "source_city": "Vienna",
                    "price_display": "EUR 1.190",
                },
            },
        ),
        personal_fit_assessment={"fit_score": 88.0, "recommendation": "shortlist"},
        preference_person_id="self",
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks
    repair_input = dict(repair_tasks[0].input_json or {})
    assert repair_input["filter_key"] == "location_scope"
    diagnostics = dict(repair_input["diagnostics"])
    assert diagnostics["location_evidence_kind"] in {"url_region", "listing_concrete", "listing_postal"}
    assert diagnostics["postal_name"] != "1010 Vienna"
    assert "Salzburg" in diagnostics["summary"] or "Salzburg" in diagnostics["title"]


def test_property_alert_review_suppresses_low_score_telegram_even_when_review_opens(monkeypatch) -> None:
    principal_id = "exec-property-alert-low-score-telegram-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Review Low Score Telegram Gate")
    seed_product_state(client, principal_id=principal_id)
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    monkeypatch.setenv("PROPERTYQUARRY_SCOUT_OUTBOUND_MIN_SCORE", "60")
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("low-score review must not notify")),
    )
    service = product_service.build_product_service(client.app.state.container)

    result = service._open_property_alert_review(
        principal_id=principal_id,
        title="Wohnung mieten in 1010 Wien | 60 m² | 2 Zimmer | EUR 1.090",
        summary="2-Zimmer Wohnung im 1. Bezirk, 60 m2, Gesamtmiete EUR 1.090.",
        source_ref="property-scout:review-low-score-1010",
        external_id="review-low-score-1010",
        counterparty="Willhaben | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/review-low-score/",
        actor="test",
        notify_telegram=True,
        candidate_properties=(
            {
                "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/review-low-score/",
                "listing_title": "Wohnung mieten in 1010 Wien | 60 m² | 2 Zimmer | EUR 1.090",
                "summary": "2-Zimmer Wohnung im 1. Bezirk, 60 m2, Gesamtmiete EUR 1.090.",
                "property_facts_json": {
                    "postal_name": "1010 Wien",
                    "street_address": "Kärntner Straße 12, 1010 Wien",
                    "area_sqm": 60,
                    "rooms": 2,
                    "total_rent_eur": 1090,
                },
            },
        ),
        personal_fit_assessment={"fit_score": 50.0, "recommendation": "review"},
        preference_person_id="self",
    )

    assert result["status"] == "opened"
    assert result["telegram_delivery_status"] == "suppressed"
    assert result["telegram_delivery_error"] == "fit_below_outbound_threshold"
    assert result["telegram_min_score"] == 60.0
    events = client.get(
        "/app/api/events",
        params={"channel": "product", "event_type": "property_alert_review_telegram_suppressed"},
    )
    assert events.status_code == 200
    assert any(dict(item["payload"]).get("reason") == "fit_below_outbound_threshold" for item in events.json()["items"])


def test_property_alert_review_uses_exact_source_scope_when_saved_location_is_missing(monkeypatch) -> None:
    principal_id = "exec-property-alert-source-scope-fallback"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Review Source Scope Fallback Office")
    seed_product_state(client, principal_id=principal_id)
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("outside-source-scope alert must not notify")),
    )
    service = product_service.build_product_service(client.app.state.container)

    result = service._open_property_alert_review(
        principal_id=principal_id,
        title="#W2 Moderne Schöne Zwei-Zimmer Wohnung mit großem Ess- & Wohnbereich",
        summary="Wählen Sie aus 113.217 Angeboten. Immobilien suchen und finden auf willhaben.",
        source_ref="gmail-thread:willhaben:salzburg-returned-from-1010",
        external_id="gmail-message:willhaben:salzburg-returned-from-1010",
        counterparty="Willhaben | Austria | Rent | 1010 Vienna",
        account_email="",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/demo-1631373932/",
        actor="test",
        notify_telegram=True,
        candidate_properties=(
            {
                "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/demo-1631373932/",
                "listing_title": "#W2 Moderne Schöne Zwei-Zimmer Wohnung mit großem Ess- & Wohnbereich",
                "summary": "Wählen Sie aus 113.217 Angeboten. Immobilien suchen und finden auf willhaben.",
                "property_facts_json": {
                    "postal_name": "1010 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "source_postal_code": "1010",
                    "source_city": "Vienna",
                    "price_display": "EUR 1.190",
                },
            },
        ),
        personal_fit_assessment={"fit_score": 92.0, "recommendation": "shortlist"},
        preference_person_id="self",
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    assert result["location_hints"] == ["1010 Vienna"]
    assert result["source_scope_location_hints"] == ["1010 Vienna"]
    assert not [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_alert_review"
    ]


def test_property_alert_review_exact_source_scope_fallback_applies_to_non_vienna_postcodes() -> None:
    principal_id = "exec-property-alert-source-scope-all-postals"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Review Source Scope All Postals")
    seed_product_state(client, principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)

    result = service._open_property_alert_review(
        principal_id=principal_id,
        title="Moderne Wohnung mit Loggia",
        summary="Provider result was queried from a selected Graz source scope.",
        source_ref="gmail-thread:willhaben:linz-returned-from-8055",
        external_id="gmail-message:willhaben:linz-returned-from-8055",
        counterparty="Willhaben | Austria | Rent | 8055 Graz",
        account_email="",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/oberoesterreich/linz/demo-dirty-scope/",
        actor="test",
        notify_telegram=False,
        candidate_properties=(
            {
                "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/oberoesterreich/linz/demo-dirty-scope/",
                "listing_title": "Moderne Wohnung mit Loggia",
                "summary": "Moderne Wohnung mit Loggia und heller Küche.",
                "property_facts_json": {
                    "postal_name": "8055 Graz",
                    "source_scope_location": "8055 Graz",
                    "source_postal_code": "8055",
                    "source_city": "Graz",
                },
            },
        ),
        personal_fit_assessment={"fit_score": 88.0, "recommendation": "shortlist"},
        preference_person_id="self",
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    assert result["location_hints"] == ["8055 Graz"]


def test_property_search_run_status_reconstructs_missing_status_url() -> None:
    principal_id = "exec-property-search-missing-status-url"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"legacy-{uuid.uuid4().hex}"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": product_service._now_iso(),
            "updated_at": product_service._now_iso(),
            "status": "in_progress",
            "status_url": "",
            "selected_platforms": ["willhaben"],
            "progress": 25,
            "current_step": "source_started",
            "message": "Scanning source.",
            "stages_total": 4,
            "steps_completed": 1,
            "summary": {"sources_total": 1},
            "events": [],
            "property_search_preferences": {},
        }

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert status["status_url"] == f"/app/api/signals/property/search/run/{run_id}"


def test_property_search_run_status_prefers_newer_persisted_state_over_stale_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-search-persisted-wins"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"persisted-wins-{uuid.uuid4().hex}"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": "2026-06-28T20:06:45+00:00",
            "updated_at": "2026-06-28T20:06:45+00:00",
            "status": "queued",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["core_portals_de"],
            "progress": 0,
            "current_step": "queued",
            "message": "Queued for execution.",
            "stages_total": 4,
            "steps_completed": 0,
            "summary": {"status": "queued", "provider_total": 1, "listing_total": 0},
            "events": [{"at": "2026-06-28T20:06:45+00:00", "step": "queued", "message": "Search run queued", "status": "queued"}],
            "property_search_preferences": {},
        }

    persisted = {
        "run_id": run_id,
        "principal_id": principal_id,
        "created_at": "2026-06-28T20:06:45+00:00",
        "updated_at": "2026-06-28T20:09:38+00:00",
        "status": "completed_partial",
        "status_url": f"/app/api/signals/property/search/run/{run_id}",
        "selected_platforms": ["core_portals_de"],
        "progress": 100,
        "current_step": "completed",
        "message": "Search run completed with status completed_partial.",
        "stages_total": 4,
        "steps_completed": 4,
        "summary": {
            "status": "completed_partial",
            "listing_total": 12,
            "reviewed_listing_total": 12,
            "provider_total": 1,
            "sources_total": 1,
            "sources_completed": 1,
            "updated_at": "2026-06-28T20:09:38+00:00",
            "ranked_candidates": [{"candidate_ref": "cand-1", "title": "Recovered match", "fit_score": 58}],
        },
        "events": [
            {"at": "2026-06-28T20:06:45+00:00", "step": "queued", "message": "Search run queued", "status": "queued"},
            {"at": "2026-06-28T20:09:38+00:00", "step": "completed", "message": "Search run completed with status completed_partial.", "status": "completed_partial"},
        ],
        "property_search_preferences": {},
    }
    monkeypatch.setattr(product_service, "_load_property_search_run_record", lambda **kwargs: dict(persisted))

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert status["status"] == "completed_partial"
    assert status["progress"] == 100
    assert status["summary"]["listing_total"] == 12
    assert len(status["events"]) == 2
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        refreshed = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY.get(run_id) or {})
    assert refreshed["status"] == "completed_partial"
    assert refreshed["updated_at"] == "2026-06-28T20:09:38+00:00"


def test_find_active_property_search_run_prefers_in_progress_over_newer_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-search-active-priority"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    queued_run_id = f"queued-{uuid.uuid4().hex}"
    running_run_id = f"running-{uuid.uuid4().hex}"
    queued_updated_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    running_updated_at = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()

    monkeypatch.setattr(
        product_service,
        "_list_property_search_run_records",
        lambda **kwargs: (
            {
                "run_id": queued_run_id,
                "principal_id": principal_id,
                "status": "queued",
                "created_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
                "updated_at": queued_updated_at,
                "summary": {"status": "queued"},
            },
            {
                "run_id": running_run_id,
                "principal_id": principal_id,
                "status": "in_progress",
                "created_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                "updated_at": running_updated_at,
                "summary": {"status": "in_progress"},
            },
        ),
    )

    def _fake_status(
        self,
        *,
        principal_id: str,
        run_id: str,
        lightweight: bool = False,
        account_email: str = "",
    ):
        if run_id == queued_run_id:
            return {
                "run_id": run_id,
                "principal_id": principal_id,
                "status": "queued",
                "updated_at": queued_updated_at,
                "progress": 0,
                "summary": {"status": "queued", "reviewed_listing_total": 0, "sources_completed": 0},
            }
        return {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "updated_at": running_updated_at,
            "progress": 58,
            "summary": {"status": "in_progress", "reviewed_listing_total": 7, "sources_completed": 3},
        }

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_status)

    active = service.find_active_property_search_run(principal_id=principal_id, limit=8)

    assert active is not None
    assert active["run_id"] == running_run_id
    assert active["status"] == "in_progress"


def test_find_active_property_search_run_hydrates_only_until_first_fresh_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-search-active-fast"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    first_run_id = f"first-{uuid.uuid4().hex}"
    second_run_id = f"second-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)

    monkeypatch.setattr(
        product_service,
        "_list_property_search_run_records",
        lambda **kwargs: (
            {
                "run_id": first_run_id,
                "principal_id": principal_id,
                "status": "in_progress",
                "created_at": (now - timedelta(minutes=1)).isoformat(),
                "updated_at": (now - timedelta(seconds=3)).isoformat(),
                "summary": {"status": "in_progress", "reviewed_listing_total": 8, "sources_completed": 2},
            },
            {
                "run_id": second_run_id,
                "principal_id": principal_id,
                "status": "in_progress",
                "created_at": (now - timedelta(minutes=2)).isoformat(),
                "updated_at": (now - timedelta(seconds=7)).isoformat(),
                "summary": {"status": "in_progress", "reviewed_listing_total": 4, "sources_completed": 1},
            },
        ),
    )
    hydrated_run_ids: list[str] = []

    def _fake_status(
        self,
        *,
        principal_id: str,
        run_id: str,
        lightweight: bool = False,
        account_email: str = "",
    ):
        hydrated_run_ids.append(run_id)
        return {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "created_at": (now - timedelta(minutes=1)).isoformat(),
            "updated_at": (now - timedelta(seconds=3)).isoformat(),
            "progress": 48,
            "summary": {"status": "in_progress", "reviewed_listing_total": 8, "sources_completed": 2},
        }

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_status)

    active = service.find_active_property_search_run(principal_id=principal_id, limit=8)

    assert active is not None
    assert active["run_id"] == first_run_id
    assert hydrated_run_ids == [first_run_id]


def test_find_active_property_search_run_prefers_fresh_active_run_over_stale_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-search-fresh-active-priority"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    stale_run_id = f"stale-{uuid.uuid4().hex}"
    fresh_run_id = f"fresh-{uuid.uuid4().hex}"
    stale_updated_at = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    fresh_updated_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()

    monkeypatch.setattr(
        product_service,
        "_list_property_search_run_records",
        lambda **kwargs: (
            {
                "run_id": stale_run_id,
                "principal_id": principal_id,
                "status": "in_progress",
                "created_at": (datetime.now(timezone.utc) - timedelta(minutes=9)).isoformat(),
                "updated_at": stale_updated_at,
                "summary": {"status": "in_progress"},
            },
            {
                "run_id": fresh_run_id,
                "principal_id": principal_id,
                "status": "in_progress",
                "created_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                "updated_at": fresh_updated_at,
                "summary": {"status": "in_progress"},
            },
        ),
    )

    def _fake_status(self, *, principal_id: str, run_id: str, lightweight: bool = False):
        if run_id == stale_run_id:
            return {
                "run_id": run_id,
                "principal_id": principal_id,
                "status": "in_progress",
                "updated_at": stale_updated_at,
                "progress": 76,
                "summary": {"status": "in_progress", "reviewed_listing_total": 18, "sources_completed": 4},
            }
        return {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "updated_at": fresh_updated_at,
            "progress": 9,
            "summary": {"status": "in_progress", "reviewed_listing_total": 0, "sources_completed": 0},
        }

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_status)

    active = service.find_active_property_search_run(principal_id=principal_id, limit=8)

    assert active is not None
    assert active["run_id"] == fresh_run_id
    assert active["status"] == "in_progress"


def test_find_active_property_search_run_returns_none_when_only_stale_active_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-search-stale-active-none"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    stale_run_id = f"stale-{uuid.uuid4().hex}"
    stale_updated_at = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()

    monkeypatch.setattr(
        product_service,
        "_list_property_search_run_records",
        lambda **kwargs: (
            {
                "run_id": stale_run_id,
                "principal_id": principal_id,
                "status": "in_progress",
                "created_at": (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat(),
                "updated_at": stale_updated_at,
                "summary": {"status": "in_progress"},
            },
        ),
    )

    monkeypatch.setattr(
        ProductService,
        "get_property_search_run_status",
        lambda self, *, principal_id, run_id, lightweight=False: {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "updated_at": stale_updated_at,
            "progress": 61,
            "summary": {"status": "in_progress", "reviewed_listing_total": 14, "sources_completed": 2},
        },
    )

    assert service.find_active_property_search_run(principal_id=principal_id, limit=8) is None


def test_property_search_run_progress_stays_monotonic_when_stage_totals_expand() -> None:
    principal_id = "exec-property-search-progress-monotonic"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"progress-{uuid.uuid4().hex}"
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": created_at,
            "updated_at": created_at,
            "status": "in_progress",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["willhaben"],
            "progress": 41,
            "current_step": "source_previewing",
            "message": "Reviewing candidate 4 of 31.",
            "stages_total": 120,
            "steps_completed": 49,
            "summary": {
                "sources_total": 10,
                "sources": [{"source_label": f"Source {index}"} for index in range(4)],
            },
            "events": [],
            "property_search_preferences": {},
            "eta_seconds": 0,
            "eta_label": "",
            "eta_seconds_smoothed": 0,
        }

    service._record_property_search_run_event(
        run_id=run_id,
        principal_id=principal_id,
        step="source_extracting",
        message="Extracting listing candidates from the next source.",
        status="in_progress",
        steps_delta=1,
        summary_updates={"sources_total": 10},
        stages_total_override=220,
    )

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    assert status is not None
    assert int(status["progress"]) >= 41
    assert str(status.get("eta_label") or "").startswith("about") or str(status.get("eta_label") or "").startswith("under")


def test_property_search_run_visible_counters_stay_monotonic_during_recovery() -> None:
    principal_id = "exec-property-search-counter-monotonic"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"counter-monotonic-{uuid.uuid4().hex}"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": product_service._now_iso(),
            "updated_at": product_service._now_iso(),
            "status": "in_progress",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["willhaben"],
            "progress": 41,
            "current_step": "source_previewing",
            "message": "Reviewing candidate 1 of 2 for Wohnberatung Wien.",
            "stages_total": 120,
            "steps_completed": 49,
            "summary": {
                "sources_total": 117,
                "sources_completed": 12,
                "reviewed_listing_total": 138,
                "scanned_listing_total": 138,
                "raw_listing_total": 138,
                "sources": [
                    {"source_label": "Willhaben", "reviewed_listing_total": 34, "scanned_listing_total": 34},
                    {"source_label": "Wohnberatung Wien", "reviewed_listing_total": 2, "scanned_listing_total": 2},
                    {"source_label": "RE/MAX Austria", "reviewed_listing_total": 1, "scanned_listing_total": 1},
                ],
            },
            "events": [],
            "property_search_preferences": {},
            "eta_seconds": 0,
            "eta_label": "",
            "eta_seconds_smoothed": 0,
        }

    service._record_property_search_run_event(
        run_id=run_id,
        principal_id=principal_id,
        step="source_previewing",
        message="Reviewing candidate 1 of 2 for Wohnberatung Wien.",
        status="in_progress",
        steps_delta=1,
        summary_updates={
            "sources_total": 117,
            "sources_completed": 3,
            "reviewed_listing_total": 70,
            "scanned_listing_total": 70,
            "raw_listing_total": 70,
        },
    )

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    assert status is not None
    summary = dict(status.get("summary") or {})
    assert int(summary.get("sources_completed") or 0) == 12
    assert int(summary.get("reviewed_listing_total") or 0) == 138
    assert int(summary.get("scanned_listing_total") or 0) == 138
    assert int(summary.get("raw_listing_total") or 0) == 138


def test_property_search_run_status_synthesizes_ranked_candidates_and_filtered_totals_from_sources() -> None:
    principal_id = "exec-property-search-synthesized-shortlist"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"synth-{uuid.uuid4().hex}"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": product_service._now_iso(),
            "updated_at": product_service._now_iso(),
            "status": "processed",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["immoscout_de"],
            "progress": 100,
            "current_step": "completed",
            "message": "The final results email was sent. Refreshing this page will continue to show the completed result desk.",
            "stages_total": 4,
            "steps_completed": 4,
            "summary": {
                "sources_total": 1,
                "raw_listing_total": 1,
                "filtered_low_fit_total": 5,
                "sources": [
                    {
                        "source_label": "ImmoScout24 Germany",
                        "status": "processed",
                        "raw_listing_total": 12,
                        "reviewed_listing_total": 11,
                        "location_mismatch_candidate_total": 3,
                        "top_candidates": [
                            {
                                "title": "Altbau near U6",
                                "property_url": "https://www.immobilienscout24.de/expose/altbau-u6",
                                "fit_score": 92,
                                "fit_summary": "Personal fit 92/100",
                                "property_facts": {
                                    "price_display": "EUR 420,000",
                                    "area_m2": 78,
                                },
                            }
                        ],
                    }
                ],
            },
            "events": [
                {
                    "step": "results_email_sent",
                    "message": "The final results email was sent. Refreshing this page will continue to show the completed result desk.",
                    "status": "processed",
                }
            ],
            "property_search_preferences": {},
        }

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert status["message"] == "Results are fully ready."
    assert dict(list(status.get("events") or [])[0])["message"] == "The final results email was sent. The completed result desk is ready."
    summary = dict(status.get("summary") or {})
    assert int(summary.get("held_back_total") or 0) == 0
    assert int(summary.get("filtered_total") or 0) == 0
    assert int(summary.get("filtered_low_fit_total") or 0) == 5
    assert int(summary.get("score_demoted_total") or 0) == 5
    assert int(summary.get("raw_listing_total") or 0) == 12
    assert int(summary.get("scanned_listing_total") or 0) == 11
    assert int(summary.get("location_mismatch_candidate_total") or 0) == 3
    ranked = [dict(row) for row in list(summary.get("ranked_candidates") or []) if isinstance(row, dict)]
    assert len(ranked) == 1
    assert ranked[0]["title"] == "Altbau near U6"


def test_property_search_run_status_backfills_live_counts_from_ranked_candidates() -> None:
    principal_id = "exec-property-search-live-count-backfill"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"live-count-backfill-{uuid.uuid4().hex}"
    candidate = {
        "title": "Broad Vienna shortlist candidate",
        "property_url": "https://example.com/vienna-shortlist-1",
        "fit_score": 53,
        "ranking_score": 53,
    }
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": product_service._now_iso(),
            "updated_at": product_service._now_iso(),
            "status": "in_progress",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["willhaben", "immoscout_at"],
            "progress": 72,
            "current_step": "source_shortlist",
            "message": "Built shortlist of 1 listing(s) for Willhaben.",
            "summary": {
                "sources_total": 2,
                "provider_total": 2,
                "listing_total": 0,
                "reviewed_listing_total": 0,
                "scanned_listing_total": 0,
                "ranked_total": 0,
                "sources": [
                    {
                        "source_label": "Willhaben",
                        "platform": "willhaben",
                        "status": "repaired",
                        "listing_total": 0,
                        "reviewed_listing_total": 0,
                        "scanned_listing_total": 0,
                        "top_candidates": [dict(candidate)],
                    }
                ],
            },
            "events": [],
            "property_search_preferences": {},
        }

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    summary = dict(status.get("summary") or {})
    assert int(summary.get("listing_total") or 0) == 1
    assert int(summary.get("reviewed_listing_total") or 0) == 1
    assert int(summary.get("scanned_listing_total") or 0) == 1
    assert int(summary.get("ranked_total") or 0) == 1
    assert int(summary.get("ranked_candidate_total") or 0) == 1
    ranked = [dict(row) for row in list(summary.get("ranked_candidates") or []) if isinstance(row, dict)]
    assert len(ranked) == 1
    assert ranked[0]["title"] == "Broad Vienna shortlist candidate"
    sources = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    assert len(sources) == 1
    assert int(sources[0].get("listing_total") or 0) == 1
    assert int(sources[0].get("reviewed_listing_total") or 0) == 1
    assert int(sources[0].get("scanned_listing_total") or 0) == 1
    assert int(sources[0].get("ranked_total") or 0) == 1


def test_property_search_run_status_backfills_live_reviewed_total_from_progress_message() -> None:
    principal_id = "exec-property-search-live-progress-count"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"live-progress-count-{uuid.uuid4().hex}"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": product_service._now_iso(),
            "updated_at": product_service._now_iso(),
            "status": "in_progress",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["willhaben"],
            "progress": 32,
            "current_step": "source_previewing",
            "message": "Reviewing candidate 20 of 30 for Willhaben.",
            "summary": {
                "sources_total": 1,
                "listing_total": 0,
                "reviewed_listing_total": 0,
                "scanned_listing_total": 0,
                "sources": [
                    {
                        "source_label": "Willhaben",
                        "platform": "willhaben",
                        "status": "warming",
                        "listing_total": 0,
                        "reviewed_listing_total": 0,
                        "scanned_listing_total": 0,
                    }
                ],
            },
            "events": [],
            "property_search_preferences": {},
        }

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    summary = dict(status.get("summary") or {})
    assert int(summary.get("reviewed_listing_total") or 0) == 19
    assert int(summary.get("scanned_listing_total") or 0) == 19


def test_property_search_run_status_rederives_ranked_candidates_from_source_scope_rows(monkeypatch) -> None:
    principal_id = "exec-property-search-rerank-source-scope"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"rerank-source-scope-{uuid.uuid4().hex}"
    source_row = {
        "source_label": "Willhaben | Austria | Rent | 1010 Vienna",
        "source_scope_label": "Willhaben | Austria | Rent | 1010 Vienna",
        "source_url": "https://www.willhaben.at/iad/immobilien/mietwohnungen?q=1010+Vienna",
        "status": "processed",
        "top_candidates": [
            {
                "source_ref": "outside-scope",
                "title": "Terrassenwohnung auf der Hohen Warte, 66 m2, EUR 1.599, (1190 Wien)",
                "property_url": "https://example.test/1190",
                "fit_score": 98,
                "property_facts": {
                    "postal_name": "1190 Wien",
                    "source_scope_location": "1010 Vienna",
                    "listing_postal_evidence": [{"postal_code": "1190", "postal_name": "1190 Wien"}],
                },
            },
            {
                "source_ref": "inside-scope",
                "title": "Wohnung mieten in 1010 Wien | 70 m2 | 2 Zimmer",
                "property_url": "https://example.test/1010",
                "fit_score": 77,
                "property_facts": {
                    "postal_name": "1010 Wien",
                    "source_scope_location": "1010 Vienna",
                    "listing_postal_evidence": [{"postal_code": "1010", "postal_name": "1010 Wien"}],
                },
            },
        ],
    }
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": product_service._now_iso(),
            "updated_at": product_service._now_iso(),
            "status": "completed_partial",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["willhaben"],
            "progress": 100,
            "current_step": "processed",
            "message": "Current shortlist is still available.",
            "summary": {
                "sources_total": 1,
                "ranked_candidates": [dict(source_row["top_candidates"][0])],
                "sources": [source_row],
            },
            "events": [],
            "property_search_preferences": {},
        }
    monkeypatch.setattr(service, "persist_property_saved_shortlist_candidates", lambda **kwargs: None)

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    ranked = [
        dict(row)
        for row in list(dict(status.get("summary") or {}).get("ranked_candidates") or [])
        if isinstance(row, dict)
    ]
    assert [row["source_ref"] for row in ranked] == ["inside-scope"]
    assert ranked[0]["rank"] == 1


def test_property_search_run_status_skips_provider_repair_task_scan_for_terminal_runs(monkeypatch) -> None:
    principal_id = "exec-property-search-terminal-skip-repairs"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"terminal-skip-{uuid.uuid4().hex}"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": product_service._now_iso(),
            "updated_at": product_service._now_iso(),
            "status": "completed_partial",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["kalandra"],
            "progress": 100,
            "current_step": "run_interrupted",
            "message": "Current shortlist is still available.",
            "stages_total": 10,
            "steps_completed": 10,
            "summary": {
                "sources_total": 2,
                "ranked_candidates": [
                    {
                        "candidate_ref": "cand-1",
                        "title": "Ranked One",
                        "property_url": "https://example.test/listing/1",
                        "fit_score": 61,
                    }
                ],
                "sources": [
                    {
                        "source_label": "Source One",
                        "status": "failed",
                        "source_url": "https://example.test/source/1",
                        "top_candidates": [],
                    }
                ],
            },
            "events": [],
            "property_search_preferences": {},
        }

    monkeypatch.setattr(
        client.app.state.container.orchestrator,
        "list_human_tasks",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("terminal runs should not scan provider repair tasks")),
    )
    monkeypatch.setattr(service, "persist_property_saved_shortlist_candidates", lambda **kwargs: None)

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert status["status"] == "completed_partial"
    assert len(list((status.get("summary") or {}).get("ranked_candidates") or [])) == 1


def test_property_search_run_status_api_synthesizes_ranked_candidates_from_source_rows(monkeypatch) -> None:
    principal_id = "exec-property-search-status-api-synth"
    os.environ["EA_API_TOKEN"] = ""
    client = build_property_client(principal_id=principal_id)
    top_candidates = [
        {
            "title": f"Altbau near U6 #{index}",
            "property_url": f"https://www.immobilienscout24.de/expose/altbau-u6-{index}",
            "fit_score": 200 - index,
        }
        for index in range(60)
    ]

    def _fake_status(self, *, principal_id: str, run_id: str):
        assert principal_id == "exec-property-search-status-api-synth"
        assert run_id == "run-42"
        return {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "processed",
            "progress": 100,
            "summary": {
                "sources_total": 1,
                "filtered_low_fit_total": 7,
                "sources": [
                    {
                        "source_label": "ImmoScout24 Germany",
                        "status": "processed",
                        "top_candidates": top_candidates,
                    }
                ],
            },
            "events": [],
        }

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_status)

    response = client.get("/app/api/signals/property/search/run/run-42")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert int(payload["summary"].get("held_back_total") or 0) == 0
    assert int(payload["summary"].get("filtered_total") or 0) == 0
    assert int(payload["summary"].get("filtered_low_fit_total") or 0) == 7
    assert int(payload["summary"].get("score_demoted_total") or 0) == 7
    ranked = [dict(row) for row in list(payload["summary"].get("ranked_candidates") or []) if isinstance(row, dict)]
    assert len(ranked) == 60
    assert ranked[0]["title"] == "Altbau near U6 #0"
    assert ranked[-1]["title"] == "Altbau near U6 #59"
    assert ranked[-1]["rank"] == 60


def test_property_search_run_status_api_accepts_lightweight_query(monkeypatch) -> None:
    principal_id = "exec-property-search-status-api-lightweight"
    os.environ["EA_API_TOKEN"] = ""
    client = build_property_client(principal_id=principal_id)
    calls: list[bool] = []

    def _fake_status(self, *, principal_id: str, run_id: str, lightweight: bool = False):
        assert principal_id == "exec-property-search-status-api-lightweight"
        assert run_id == "run-light"
        calls.append(lightweight)
        return {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "in_progress",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "progress": 42,
            "summary": {"sources_total": 2},
            "events": [],
            "selected_platforms": ["willhaben"],
            "updated_at": "2026-06-23T12:00:00+00:00",
        }

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _fake_status)

    response = client.get("/app/api/signals/property/search/run/run-light?lightweight=1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert calls == [True]
    assert body["generated_at"] == "2026-06-23T12:00:00+00:00"
    assert body["summary"]["sources_total"] == 2


def test_property_search_run_progress_records_only_terminal_sources_and_eta_summary() -> None:
    principal_id = "exec-property-search-progress-eta"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"progress-{uuid.uuid4().hex}"
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=18)).isoformat()
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": created_at,
            "updated_at": created_at,
            "status": "in_progress",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["immowelt_at"],
            "progress": 0,
            "current_step": "sources_resolved",
            "message": "Resolved 6 provider(s) for scanning.",
            "stages_total": 120,
            "steps_completed": 2,
            "summary": {
                "sources_total": 6,
                "sources": [
                    {"source_label": "Source A", "status": "completed"},
                    {"source_label": "Source B", "status": "in_progress"},
                ],
            },
            "events": [],
            "property_search_preferences": {},
            "eta_seconds": 0,
            "eta_label": "",
            "eta_seconds_smoothed": 0,
        }

    service._record_property_search_run_event(
        run_id=run_id,
        principal_id=principal_id,
        step="source_assessing",
        message="Enriching top 6 candidate(s) out of 31 for immowelt Austria.",
        status="in_progress",
        steps_delta=1,
        summary_updates={"sources_total": 6},
    )

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    assert status is not None
    assert int(status["summary"]["sources_completed"]) == 1
    assert int(status["summary"]["eta_seconds"]) > 0
    assert str(status["summary"]["eta_label"])


def test_property_search_run_surfaces_and_updates_missing_fact_research_tasks() -> None:
    principal_id = "exec-property-search-research-queue"
    client = build_property_client(principal_id=principal_id)
    run_id = f"research-{uuid.uuid4().hex}"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": product_service._now_iso(),
            "updated_at": product_service._now_iso(),
            "status": "in_progress",
            "status_url": "",
            "selected_platforms": ["justiz_edikte_at"],
            "progress": 65,
            "current_step": "source_review_packet",
            "message": "Preparing review packets.",
            "stages_total": 8,
            "steps_completed": 5,
            "summary": {
                "sources_total": 1,
                "sources": [
                    {
                        "source_label": "Justiz Edikte Auctions",
                        "top_candidates": [
                            {
                                "source_ref": "property-scout:auction-1",
                                "property_url": "https://edikte2.justiz.gv.at/example",
                                "title": "Auction apartment with floorplan",
                                "fit_score": 72.0,
                                "review_url": "/app/handoffs/human_task:auction-review",
                                "property_facts": {
                                    "has_floorplan": True,
                                    "missing_fact_research": {
                                        "status": "queued",
                                        "updated_at": "2026-06-06T01:00:00+00:00",
                                        "items": [
                                            {
                                                "field": "rooms",
                                                "label": "Rooms",
                                                "status": "research_needed",
                                                "display_value": "Rooms under research",
                                                "evidence": "Floorplan exists but no structured room count.",
                                                "ooda": {
                                                    "observe": "Room count is missing.",
                                                    "act": "Parse the downloadable floorplan bundle.",
                                                },
                                                "next_actions": ["Parse ZIP/PDF bundle.", "Run floorplan OCR."],
                                            }
                                        ],
                                    },
                                },
                            }
                        ],
                    }
                ],
            },
            "events": [],
            "property_search_preferences": {},
        }

    status = client.get(f"/app/api/signals/property/search/run/{run_id}")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["research_task_total"] == 1
    assert body["open_research_task_total"] == 1
    task = body["research_tasks"][0]
    assert task["field"] == "rooms"
    assert task["priority"] == "high"
    assert task["status"] == "queued"
    assert task["review_url"] == "/app/handoffs/human_task:auction-review"

    filled = client.post(
        f"/app/api/signals/property/search/run/{run_id}/research-tasks/{task['task_id']}",
        json={"action": "fill", "value": "4 rooms", "note": "Read from the valuation PDF."},
    )
    assert filled.status_code == 200, filled.text
    updated = filled.json()
    assert updated["filled_research_task_total"] == 1
    assert updated["open_research_task_total"] == 0
    updated_task = updated["research_tasks"][0]
    assert updated_task["status"] == "filled"
    assert updated_task["display_value"] == "4 rooms"
    assert updated_task["owner_note"] == "Read from the valuation PDF."
    assert any(event["step"] == "research_task_updated" for event in updated["events"])


def test_research_task_update_loads_registry_fallback_without_holding_run_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-research-task-unlocked-load"
    run_id = f"research-task-unlocked-{uuid.uuid4().hex}"
    task_id = "mf_rooms_unlocked"
    snapshot = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "processed",
        "research_tasks": [{"task_id": task_id, "status": "queued"}],
    }
    persisted = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "processed",
        "summary": {},
        "events": [],
    }
    calls = {"load": 0}

    def _load_unlocked(**kwargs: object) -> dict[str, object]:
        calls["load"] += 1
        assert kwargs == {"run_id": run_id, "principal_id": principal_id}
        acquired = product_service._PROPERTY_SEARCH_RUN_LOCK.acquire(blocking=False)
        assert acquired, "durable run loading must happen outside the registry lock"
        product_service._PROPERTY_SEARCH_RUN_LOCK.release()
        return dict(persisted)

    service = ProductService.__new__(ProductService)
    monkeypatch.setattr(service, "_snapshot_property_search_run", lambda **_kwargs: dict(snapshot))
    monkeypatch.setattr(service, "_record_property_search_run_event", lambda **_kwargs: True)
    monkeypatch.setattr(service, "_best_effort_propertyquarry_teable_sync", lambda **_kwargs: None)
    monkeypatch.setattr(product_service, "_load_property_search_run_record", _load_unlocked)
    monkeypatch.setattr(product_service, "_store_property_search_run_record", lambda _record: True)

    try:
        result = service.update_property_search_research_task(
            principal_id=principal_id,
            run_id=run_id,
            task_id=task_id,
            action="fill",
            value="4 rooms",
        )

        assert result == snapshot
        assert calls["load"] == 1
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            state = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id])
        override = dict(dict(state.get("research_task_overrides") or {})[task_id])
        assert override["status"] == "filled"
        assert override["value"] == "4 rooms"
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.pop(run_id, None)


def test_property_alert_personal_fit_snapshot_times_out_fast(monkeypatch) -> None:
    class _Profiles:
        def assess_candidate(self, **kwargs):  # type: ignore[no-untyped-def]
            time.sleep(0.2)
            return {"fit_score": 50}

    monkeypatch.setenv("EA_PROPERTY_ALERT_ASSESSMENT_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setattr(
        product_service,
        "_property_alert_facts_for_url",
        lambda url: ({"postal_name": "1200 Wien"}, "listing-1"),
    )

    assessment, facts, listing_id = _property_alert_personal_fit_snapshot(
        preference_profiles=_Profiles(),
        principal_id="exec-timeout",
        person_id="self",
        property_url="https://www.willhaben.at/iad/object?adId=1",
    )

    assert assessment is None
    assert facts == {}
    assert listing_id == ""


def test_property_alert_personal_fit_snapshot_pins_scorer_across_timeout(monkeypatch) -> None:
    release_facts = threading.Event()
    scoring_finished = threading.Event()
    scoring_calls: list[str] = []

    def _blocked_facts(url: str) -> tuple[dict[str, object], str]:
        assert release_facts.wait(timeout=2.0)
        return {"postal_name": "1200 Wien"}, "listing-pinned-scorer"

    def _record_scorer(label: str):
        def _scorer(**kwargs) -> dict[str, object]:
            scoring_calls.append(label)
            scoring_finished.set()
            return {"fit_score": 50}

        return _scorer

    monkeypatch.setenv("EA_PROPERTY_ALERT_ASSESSMENT_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setattr(product_service, "_property_alert_facts_for_url", _blocked_facts)
    monkeypatch.setattr(product_service, "_property_alert_personal_fit_from_facts", _record_scorer("initial"))

    assessment, facts, listing_id = _property_alert_personal_fit_snapshot(
        preference_profiles=object(),
        principal_id="exec-pinned-scorer",
        person_id="self",
        property_url="https://www.willhaben.at/iad/object?adId=2",
    )

    assert assessment is None
    assert facts == {}
    assert listing_id == ""

    monkeypatch.setattr(product_service, "_property_alert_personal_fit_from_facts", _record_scorer("replacement"))
    release_facts.set()

    assert scoring_finished.wait(timeout=2.0)
    assert scoring_calls == ["initial"]


def test_property_candidate_supports_live_tour_detects_360() -> None:
    assert product_service._property_candidate_supports_live_tour(
        {"property_facts": {"has_360": True}}
    ) is True
    assert product_service._property_candidate_supports_live_tour(
        {"property_facts": {"source_virtual_tour_url": "https://example.com/tour"}}
    ) is True
    assert product_service._property_candidate_supports_live_tour(
        {"property_facts": {"has_floorplan": True}}
    ) is True
    assert product_service._property_candidate_supports_live_tour(
        {"property_facts": {"has_360": False}}
    ) is False


def test_property_candidate_supports_live_tour_rejects_willhaben_tracking_endpoint() -> None:
    assert product_service._property_candidate_supports_live_tour(
        {
            "property_facts": {
                "has_360": True,
                "source_virtual_tour_url": "https://api.willhaben.at/restapi/v2/logevent/atz/1134225012/virtual-tour-link-clicked",
            }
        }
    ) is False
    assert product_service._safe_provider_live_360_url(
        "https://api.willhaben.at/restapi/v2/logevent/atz/1134225012/virtual-tour-link-clicked"
    ) == ""


def test_property_scout_extract_source_virtual_tour_url_rejects_willhaben_tracking_link() -> None:
    html = """
    <html>
      <body>
        <a href="https://api.willhaben.at/restapi/v2/logevent/atz/1134225012/virtual-tour-link-clicked">Tour</a>
      </body>
    </html>
    """

    assert (
        product_service._property_scout_extract_source_virtual_tour_url(
            source_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/demo-1134225012/",
            html=html,
        )
        == ""
    )


def test_willhaben_packet_source_virtual_tour_url_falls_back_to_attribute_map_links() -> None:
    packet = {
        "property_facts_json": {
            "attribute_map": {
                "INFOLINK/NAME": ["3D Rundgang"],
                "INFOLINK/URL": ["https://my.matterport.com/show/?m=BmVWxvZQZLq"],
                "VIRTUAL_VIEW_LINK/URL": ["https://my.matterport.com/show/?m=BmVWxvZQZLq"],
            }
        }
    }

    assert (
        product_service._willhaben_packet_source_virtual_tour_url(packet)
        == "https://my.matterport.com/show/?m=BmVWxvZQZLq"
    )


def test_willhaben_packet_source_virtual_tour_url_rejects_tracking_link() -> None:
    packet = {
        "source_virtual_tour_url": "https://api.willhaben.at/restapi/v2/logevent/atz/1134225012/virtual-tour-link-clicked",
        "property_facts_json": {
            "source_virtual_tour_url": "https://api.willhaben.at/restapi/v2/logevent/atz/1134225012/virtual-tour-link-clicked",
        },
    }

    assert product_service._willhaben_packet_source_virtual_tour_url(packet) == ""


def test_property_search_run_starts_with_explicit_platform_and_tracks_progress(monkeypatch) -> None:
    principal_id = "exec-property-search-run-explicit"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Run Office")
    seed_product_state(client, principal_id=principal_id)

    observed: dict[str, object] = {}

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["principal_id"] = principal_id
        observed["actor"] = actor
        observed["selected_platforms"] = tuple(selected_platforms)
        observed["property_search_preferences"] = dict(property_search_preferences or {})
        observed["force_refresh"] = bool(force_refresh)
        observed["max_results_per_source"] = max_results_per_source
        if callable(progress_callback):
            progress_callback(
                step="mock-progress",
                message="mock scout step",
                status="in_progress",
                steps_delta=2,
                summary_updates={"sources_total": 1},
            )
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 1,
            "review_created_total": 1,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [
                {
                    "source_url": "https://www.willhaben.at/iad/immobilien/mietwohnungen",
                    "source_label": "Willhaben Rentals",
                    "preference_person_id": "self",
                    "listing_total": 1,
                    "review_created_total": 1,
                    "review_existing_total": 0,
                    "notified_total": 0,
                    "tour_created_total": 0,
                    "tour_existing_total": 0,
                    "high_fit_total": 0,
                    "watch_notified_total": 0,
                    "top_fit_score": 0.0,
                }
            ],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post(
        "/app/api/signals/property/search/run",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": {"preference_person_id": "elisabeth", "min_match_score": 80, "require_floorplan": True},
            "force_refresh": True,
            "max_results_per_source": 2,
        },
    )
    assert started.status_code == 202, started.text

    started_body = started.json()
    run_id = started_body["run_id"]
    assert run_id
    assert started_body["selected_platforms"] == ["willhaben"]
    assert started_body["status_url"] == f"/app/api/signals/property/search/run/{run_id}"

    status = _poll_property_search_run_status(client, run_id)
    assert status["status"] == "processed"
    assert status["summary"]["sources_total"] == 1
    assert status["steps_completed"] > 0
    assert status["progress"] >= 0
    assert status["principal_id"] == principal_id
    assert observed["selected_platforms"] == ("willhaben",)
    assert observed["force_refresh"] is True
    assert observed["max_results_per_source"] is None
    assert observed["property_search_preferences"]["preference_person_id"] == "elisabeth"
    assert observed["property_search_preferences"]["min_match_score"] == 0.0
    assert observed["property_search_preferences"]["require_floorplan"] is True


def test_property_search_run_api_passes_normalized_merged_preferences_to_worker(monkeypatch) -> None:
    principal_id = "exec-property-search-run-normalized-merged"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Run Normalization Office")
    seed_product_state(client, principal_id=principal_id)

    saved = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "property_type": ["apartment"],
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
        },
    )
    assert saved.status_code == 200, saved.text

    observed: dict[str, object] = {}

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["selected_platforms"] = tuple(selected_platforms)
        observed["property_search_preferences"] = dict(property_search_preferences or {})
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 0,
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": {
                "property_type": ["land"],
                "require_floorplan": True,
                "require_energy_certificate": True,
                "require_operating_cost_statement": True,
                "investment_require_floorplan": True,
                "require_barrier_free": True,
                "min_rooms": 4,
                "keywords": "lift, balcony, playground nearby",
                "avoid_keywords": "barrier-free",
                "keyword_preferences": {
                    "lift": "must_have",
                    "barrier-free": "avoid",
                    "playground nearby": "nice_to_have_1km",
                },
            },
            "max_results_per_source": 2,
        },
    )
    assert started.status_code == 202, started.text

    status = _poll_property_search_run_status(client, started.json()["run_id"])
    assert status["status"] == "processed"

    payload = dict(observed["property_search_preferences"])
    assert observed["selected_platforms"] == ("willhaben",)
    assert payload["property_type"] == ["land"]
    assert payload["min_match_score"] == 0.0
    assert payload["require_floorplan"] is False
    assert payload["require_energy_certificate"] is False
    assert payload["require_operating_cost_statement"] is False
    assert payload["investment_require_floorplan"] is False
    assert payload["require_barrier_free"] is False
    assert "min_rooms" not in payload
    assert payload["keywords"] == ""
    assert payload["avoid_keywords"] == ""
    assert payload["keyword_preferences"] == {"playground nearby": "nice_to_have_1km"}


def test_property_search_run_greenfield_api_wraps_legacy_signal_contract(monkeypatch) -> None:
    principal_id = "exec-property-search-run-greenfield-api"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Run Greenfield API")

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        if callable(progress_callback):
            progress_callback(
                step="sources_resolved",
                message="Resolved sources for greenfield API.",
                status="in_progress",
                steps_delta=1,
                summary_updates={"sources_total": 1},
            )
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "email_notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": {"country_code": "AT", "min_area_m2": 80},
            "max_results_per_source": 2,
        },
    )
    assert started.status_code == 202, started.text
    body = started.json()
    run_id = body["run_id"]
    assert body["status_url"] == f"/app/api/property/search-runs/{run_id}"

    latest: dict[str, object] = {}
    for _ in range(120):
        status = client.get(f"/app/api/property/search-runs/{run_id}")
        assert status.status_code == 200, status.text
        latest = status.json()
        if latest["status"] == "processed":
            break
        time.sleep(0.02)
    assert latest["status"] == "processed"
    assert latest["status_url"] == f"/app/api/property/search-runs/{run_id}"

    events = client.get(f"/app/api/property/search-runs/{run_id}/events")
    assert events.status_code == 200, events.text
    events_body = events.json()
    assert events_body["run_id"] == run_id
    assert events_body["status_url"] == f"/app/api/property/search-runs/{run_id}"
    assert any(item["step"] == "sources_resolved" for item in events_body["events"])

    legacy_status = client.get(f"/app/api/signals/property/search/run/{run_id}")
    assert legacy_status.status_code == 200, legacy_status.text
    assert legacy_status.json()["status_url"] == f"/app/api/signals/property/search/run/{run_id}"


def test_property_search_run_dispatch_only_returns_queued_without_snapshot(monkeypatch) -> None:
    principal_id = "exec-property-search-run-dispatch-only"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Dispatch Office")
    observed: dict[str, object] = {}
    monkeypatch.delenv("PROPERTYQUARRY_SEARCH_RUN_WORKER_CONCURRENCY", raising=False)

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["selected_platforms"] = tuple(selected_platforms)
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 0,
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    def _fail_snapshot(*args, **kwargs):
        raise AssertionError("dispatch_only must not wait on search-run snapshot before responding")

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)
    monkeypatch.setattr(ProductService, "_snapshot_property_search_run", _fail_snapshot)

    started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": {"country_code": "AT", "location_query": "1010 Vienna"},
            "dispatch_only": True,
        },
    )

    assert started.status_code == 202, started.text
    body = started.json()
    assert body["run_id"]
    assert body["status"] == "queued"
    assert body["status_url"] == f"/app/api/property/search-runs/{body['run_id']}"
    assert body["summary"]["dispatch_only"] is True
    assert body["summary"]["worker_started"] is True
    assert body["summary"]["worker_deferred"] is True
    assert body["summary"]["worker_concurrency_limit"] == 4
    for _ in range(50):
        if observed.get("selected_platforms") == ("willhaben",):
            break
        time.sleep(0.01)
    assert observed["selected_platforms"] == ("willhaben",)


def test_property_search_run_preserves_restored_agent_commercial_when_raw_preferences_exist(monkeypatch) -> None:
    principal_id = "exec-property-search-run-restored-agent-commercial"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Restored Commercial Office")
    client.app.state.container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json={
            "country_code": "AT",
            "location_query": "1010 Vienna",
            "raw_preferences": {
                "country_code": "AT",
                "location_query": "1010 Vienna",
            },
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )
    observed: dict[str, object] = {}

    def _fake_start_property_search_run(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...],
        property_search_preferences: dict[str, object],
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        dispatch_only: bool = False,
        dispatch_probe_ack_only: bool = False,
        trace_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        observed["property_search_preferences"] = dict(property_search_preferences)
        return {
            "run_id": "run-restored-agent-commercial",
            "status": "queued",
            "summary": product_service._property_search_run_default_summary(dict(property_search_preferences)),
        }

    monkeypatch.setattr(ProductService, "start_property_search_run", _fake_start_property_search_run)

    started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": {"country_code": "AT", "location_query": "1010 Vienna"},
            "dispatch_only": True,
        },
    )

    assert started.status_code == 202, started.text
    body = started.json()
    assert dict(observed["property_search_preferences"])["property_commercial"]["active_plan_key"] == "agent"
    assert body["summary"]["current_plan_key"] == "agent"
    assert body["summary"]["provider_workers"]["worker_concurrency"] == 4


def test_property_search_run_preferences_strip_workspace_state_and_keep_active_agent() -> None:
    persisted_preferences = {
        "country_code": "AT",
        "listing_mode": "rent",
        "selected_platforms": ["willhaben"],
        "active_search_agent_id": "persisted-agent",
        "search_agents": [{"agent_id": "persisted-agent", "location_query": "Wien"}],
        "raw_preferences": {
            "country_code": "AT",
            "contact_email": "persisted-secret@example.test",
        },
        "property_commercial": {
            "active_plan_key": "agent",
            "status": "active",
            "stripe_customer_id": "cus_persisted_secret",
        },
        "saved_shortlist_candidates": [{"property_url": "https://persisted.example.test/listing"}],
        "saved_shortlist_share_slug": "persisted-private-slug",
    }
    service = ProductService.__new__(ProductService)
    service._container = SimpleNamespace(
        onboarding=SimpleNamespace(
            status=lambda **_kwargs: {
                "property_search_preferences": persisted_preferences,
            }
        )
    )
    requested_preferences = {
        "country_code": "AT",
        "listing_mode": "rent",
        "active_search_agent_id": "requested-agent",
        "search_agents": [{"agent_id": "requested-agent", "location_query": "Graz"}],
        "raw_preferences": {"contact_email": "requested-secret@example.test"},
        "property_commercial": {
            "active_plan_key": "agent",
            "status": "active",
            "active_until": "2999-01-01T00:00:00+00:00",
            "checkout_session_id": "cs_requested_secret",
        },
        "saved_shortlist_candidates": [{"property_url": "https://requested.example.test/listing"}],
        "saved_shortlist_share_slug": "requested-private-slug",
        "provider_selection_filter_applied": True,
        "provider_selection_filter_removed": ["stale-selection-provider"],
        "provider_selection_filter_removed_details": [{"platform": "stale-selection-provider"}],
        "provider_country_filter_applied": True,
        "provider_country_filter_removed": ["stale-country-provider"],
        "provider_country_filter_removed_details": [{"platform": "stale-country-provider"}],
    }

    selected_platforms, run_preferences, resolved_max_results = service._resolve_property_search_run_preferences(
        principal_id="exec-property-search-run-preference-isolation",
        selected_platforms=("willhaben",),
        property_preferences=requested_preferences,
        max_results_per_source=37,
        force_refresh=False,
    )

    assert selected_platforms == ("willhaben",)
    assert run_preferences["active_search_agent_id"] == "requested-agent"
    assert run_preferences["property_commercial"] == {
        "active_plan_key": "agent",
        "status": "active",
        "active_until": "2999-01-01T00:00:00+00:00",
    }
    assert run_preferences["min_match_score"] == 0.0
    assert run_preferences["max_results_per_source"] == 37
    assert resolved_max_results == 37
    for workspace_only_key in (
        "search_agents",
        "raw_preferences",
        "saved_shortlist_candidates",
        "saved_shortlist_share_slug",
        "contact_email",
    ):
        assert workspace_only_key not in run_preferences
    for stale_filter_key in (
        "provider_selection_filter_applied",
        "provider_selection_filter_removed",
        "provider_selection_filter_removed_details",
        "provider_country_filter_applied",
        "provider_country_filter_removed",
        "provider_country_filter_removed_details",
    ):
        assert stale_filter_key not in run_preferences


def test_property_search_run_snapshot_projection_strips_historical_workspace_state() -> None:
    snapshot = {
        "run_id": "historical-costa-rica-run",
        "property_search_preferences": {
            "country_code": "CR",
            "region_code": "G",
            "listing_mode": "buy",
            "location_query": "Tamarindo, Costa Rica",
            "selected_platforms": ["re_cr_mls", "encuentra24_cr"],
            "active_search_agent_id": "costa-rica-agent",
            "raw_preferences": {
                "country_code": "CR",
                "contact_email": "historical-secret@example.test",
            },
            "contact_email": "historical-secret@example.test",
            "search_agents": [
                {
                    "agent_id": "stale-vienna-agent",
                    "location_query": "1010 Vienna",
                    "selected_platforms": ["willhaben"],
                }
            ],
            "saved_shortlist_candidates": [
                {
                    "title": "Stale Vienna shortlist listing",
                    "property_url": "https://stale-vienna.example.test/listing",
                }
            ],
            "saved_shortlist_share_slug": "stale-private-share-slug",
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
                "stripe_customer_id": "cus_historical_secret",
                "payment_history": [{"payment_intent_id": "pi_historical_secret"}],
            },
        },
    }

    projected = product_service._property_search_run_snapshot_projection(snapshot)

    preferences = dict(projected["property_search_preferences"])
    assert preferences["country_code"] == "CR"
    assert preferences["region_code"] == "G"
    assert preferences["listing_mode"] == "buy"
    assert preferences["location_query"] == "Tamarindo, Costa Rica"
    assert preferences["selected_platforms"] == ["re_cr_mls", "encuentra24_cr"]
    assert preferences["active_search_agent_id"] == "costa-rica-agent"
    assert preferences["property_commercial"] == {
        "active_plan_key": "agent",
        "status": "active",
        "active_until": "2999-01-01T00:00:00+00:00",
    }
    serialized = json.dumps(projected, ensure_ascii=False)
    for excluded_value in (
        "historical-secret@example.test",
        "1010 Vienna",
        "stale-vienna-agent",
        "https://stale-vienna.example.test/listing",
        "stale-private-share-slug",
        "cus_historical_secret",
        "pi_historical_secret",
    ):
        assert excluded_value not in serialized


def test_property_search_run_projection_preserves_provider_filter_audit_only() -> None:
    projected = product_service._property_search_run_preferences_projection(
        {
            "country_code": "AT",
            "provider_selection_filter_applied": True,
            "provider_selection_filter_removed": ["unsupported-provider"],
            "provider_selection_filter_removed_details": [
                {"platform": "unsupported-provider", "reason": "not_searchable"}
            ],
            "provider_country_filter_applied": True,
            "provider_country_filter_removed": ["wrong-country-provider"],
            "provider_country_filter_removed_details": [
                {"platform": "wrong-country-provider", "reason": "country_mismatch"}
            ],
            "future_workspace_secret": "must-not-enter-run-snapshot",
        }
    )

    assert projected == {
        "country_code": "AT",
        "provider_selection_filter_applied": True,
        "provider_selection_filter_removed": ["unsupported-provider"],
        "provider_selection_filter_removed_details": [
            {"platform": "unsupported-provider", "reason": "not_searchable"}
        ],
        "provider_country_filter_applied": True,
        "provider_country_filter_removed": ["wrong-country-provider"],
        "provider_country_filter_removed_details": [
            {"platform": "wrong-country-provider", "reason": "country_mismatch"}
        ],
    }


def test_property_search_run_worker_concurrency_defaults_to_four(monkeypatch) -> None:
    monkeypatch.delenv("PROPERTYQUARRY_SEARCH_RUN_WORKER_CONCURRENCY", raising=False)

    assert product_service._property_search_run_worker_concurrency() == 4


def test_property_search_run_worker_semaphore_allows_four_live_runs(monkeypatch) -> None:
    principal_id = "exec-property-search-run-four-live-workers"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Four Live Workers")
    monkeypatch.setattr(product_service, "_PROPERTY_SEARCH_RUN_WORKER_SEMAPHORE", threading.BoundedSemaphore(4))

    release_event = threading.Event()
    state_lock = threading.Lock()
    entered_run_ids: list[str] = []
    current_workers = 0
    max_workers = 0

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        nonlocal current_workers
        nonlocal max_workers
        run_id = str((property_search_preferences or {}).get("__property_search_run_id__") or "").strip()
        with state_lock:
            current_workers += 1
            max_workers = max(max_workers, current_workers)
            entered_run_ids.append(run_id)
        try:
            assert release_event.wait(timeout=5.0)
        finally:
            with state_lock:
                current_workers = max(0, current_workers - 1)
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 0,
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    run_ids: list[str] = []
    for index in range(5):
        started = client.post(
            "/app/api/property/search-runs",
            json={
                "selected_platforms": ["willhaben"],
                "property_preferences": {
                    "country_code": "AT",
                    "location_query": f"10{index}0 Vienna",
                },
            },
        )
        assert started.status_code == 202, started.text
        run_ids.append(str(started.json()["run_id"]))

    deadline = time.time() + 2.0
    while time.time() < deadline:
        with state_lock:
            if len(entered_run_ids) >= 4 and current_workers == 4:
                break
        time.sleep(0.01)

    with state_lock:
        assert len(entered_run_ids) == 4
        assert current_workers == 4
        assert max_workers == 4

    time.sleep(0.1)
    with state_lock:
        assert len(entered_run_ids) == 4
        assert current_workers == 4

    release_event.set()
    final_statuses = [_poll_property_search_run_status(client, run_id) for run_id in run_ids]
    assert all(str(row.get("status") or "").strip() == "processed" for row in final_statuses)
    with state_lock:
        assert max_workers == 4
        assert len(entered_run_ids) == 5


def test_property_search_agent_active_run_guard_matches_agent_id(monkeypatch) -> None:
    principal_id = "exec-property-search-agent-active-run-guard"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Agent Active Run Guard")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "active_search_agent_id": "agent-vienna",
            "search_agents": [
                {
                    "agent_id": "agent-vienna",
                    "name": "Vienna rent watch",
                    "enabled": True,
                    "selected_platforms": ["willhaben"],
                    "preferences_json": {
                        "country_code": "AT",
                        "listing_mode": "rent",
                        "location_query": "Wien",
                        "selected_platforms": ["willhaben"],
                    },
                }
            ],
        },
    )
    assert stored.status_code == 200, stored.text

    started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": {
                "country_code": "AT",
                "listing_mode": "rent",
                "location_query": "Wien",
                "selected_platforms": ["willhaben"],
                "active_search_agent_id": "agent-vienna",
                "search_agents": [
                    {
                        "agent_id": "agent-vienna",
                        "name": "Vienna rent watch",
                        "enabled": True,
                        "selected_platforms": ["willhaben"],
                    }
                ],
            },
            "dispatch_only": True,
        },
        headers={"X-PropertyQuarry-Dispatch-Probe": "1"},
    )
    assert started.status_code == 202, started.text

    service = product_service.build_product_service(client.app.state.container)
    assert service._property_search_agent_has_active_run(principal_id=principal_id, agent_id="agent-vienna") is True
    assert service._property_search_agent_has_active_run(principal_id=principal_id, agent_id="agent-other") is False


def test_launch_due_property_search_agents_queues_each_due_enabled_agent(monkeypatch) -> None:
    principal_id = "exec-property-search-agent-launcher"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Agent Launcher")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
            "search_agents": [
                {
                    "agent_id": "agent-a",
                    "name": "Vienna A",
                    "enabled": True,
                    "country_code": "AT",
                    "listing_mode": "rent",
                    "location_query": "1010 Vienna",
                    "next_run_at": "2026-01-01T08:00:00+00:00",
                    "selected_platforms": ["willhaben"],
                    "preferences_json": {
                        "country_code": "AT",
                        "listing_mode": "rent",
                        "location_query": "1010 Vienna",
                        "selected_platforms": ["willhaben"],
                    },
                },
                {
                    "agent_id": "agent-b",
                    "name": "Vienna B",
                    "enabled": True,
                    "country_code": "AT",
                    "listing_mode": "buy",
                    "location_query": "1020 Vienna",
                    "next_run_at": "2026-01-02T08:00:00+00:00",
                    "selected_platforms": ["remax_at"],
                    "preferences_json": {
                        "country_code": "AT",
                        "listing_mode": "buy",
                        "location_query": "1020 Vienna",
                        "selected_platforms": ["remax_at"],
                    },
                },
                {
                    "agent_id": "agent-c",
                    "name": "Vienna Future",
                    "enabled": True,
                    "country_code": "AT",
                    "listing_mode": "rent",
                    "location_query": "1030 Vienna",
                    "next_run_at": "2999-01-01T08:00:00+00:00",
                    "selected_platforms": ["remax_at"],
                },
                {
                    "agent_id": "agent-d",
                    "name": "Vienna Paused",
                    "enabled": False,
                    "country_code": "AT",
                    "listing_mode": "rent",
                    "location_query": "1040 Vienna",
                    "next_run_at": "2026-01-03T08:00:00+00:00",
                    "selected_platforms": ["findmyhome_at"],
                },
                {
                    "agent_id": "agent-e",
                    "name": "Vienna Active",
                    "enabled": True,
                    "country_code": "AT",
                    "listing_mode": "rent",
                    "location_query": "1050 Vienna",
                    "next_run_at": "2026-01-04T08:00:00+00:00",
                    "selected_platforms": ["derstandard_at"],
                },
            ],
        },
    )
    assert stored.status_code == 200, stored.text
    launches: list[dict[str, object]] = []

    def _fake_has_active_run(self, *, principal_id: str, agent_id: str) -> bool:
        return agent_id == "agent-e"

    def _fake_start_property_search_run(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...],
        property_search_preferences: dict[str, object],
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        dispatch_only: bool = False,
        dispatch_probe_ack_only: bool = False,
    ) -> dict[str, object]:
        launches.append(
            {
                "principal_id": principal_id,
                "actor": actor,
                "selected_platforms": selected_platforms,
                "active_search_agent_id": property_search_preferences.get("active_search_agent_id"),
                "location_query": property_search_preferences.get("location_query"),
                "listing_mode": property_search_preferences.get("listing_mode"),
                "force_refresh": force_refresh,
                "dispatch_only": dispatch_only,
            }
        )
        agent_id = str(property_search_preferences.get("active_search_agent_id") or "unknown")
        return {"run_id": f"run-{agent_id}", "status": "queued"}

    monkeypatch.setattr(ProductService, "_property_search_agent_has_active_run", _fake_has_active_run)
    monkeypatch.setattr(ProductService, "start_property_search_run", _fake_start_property_search_run)
    service = product_service.build_product_service(client.app.state.container)

    summary = service.launch_due_property_search_agents(principal_id=principal_id, actor="scheduler", limit=8)

    assert summary["mode"] == "agents"
    assert summary["configured_total"] == 5
    assert summary["enabled_total"] == 4
    assert summary["due_total"] == 3
    assert summary["launched_total"] == 2
    assert summary["skipped_disabled_total"] == 1
    assert summary["skipped_not_due_total"] == 1
    assert summary["skipped_active_total"] == 1
    assert summary["skipped_invalid_total"] == 0
    assert [row["agent_id"] for row in summary["launched_runs"]] == ["agent-a", "agent-b"]
    assert launches == [
        {
            "principal_id": principal_id,
            "actor": "scheduler",
            "selected_platforms": ("willhaben",),
            "active_search_agent_id": "agent-a",
            "location_query": "1010 Vienna",
            "listing_mode": "rent",
            "force_refresh": True,
            "dispatch_only": True,
        },
        {
            "principal_id": principal_id,
            "actor": "scheduler",
            "selected_platforms": ("remax_at",),
            "active_search_agent_id": "agent-b",
            "location_query": "1020 Vienna",
            "listing_mode": "buy",
            "force_refresh": True,
            "dispatch_only": True,
        },
    ]


def test_property_search_run_dispatch_probe_ack_only_does_not_start_worker(monkeypatch) -> None:
    principal_id = "exec-property-search-run-dispatch-probe"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Dispatch Probe Office")
    observed: dict[str, object] = {}

    def _fake_sync_direct_property_scout(self, **kwargs):  # type: ignore[no-untyped-def]
        observed["called"] = True
        return {"generated_at": product_service._now_iso(), "status": "processed", "sources": []}

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": {"country_code": "AT", "location_query": "1010 Vienna"},
            "dispatch_only": True,
        },
        headers={"X-PropertyQuarry-Dispatch-Probe": "1"},
    )

    assert started.status_code == 202, started.text
    body = started.json()
    assert body["run_id"]
    assert body["status"] == "queued"
    assert body["summary"]["dispatch_only"] is True
    assert body["summary"]["dispatch_probe_ack_only"] is True
    assert body["summary"]["worker_started"] is False
    assert body["summary"]["worker_deferred"] is False
    assert body["summary"]["worker_start_mode"] == "probe_ack_only"
    time.sleep(0.1)
    assert observed == {}


def test_property_search_run_worker_preserves_provider_repair_receipts_before_terminal(monkeypatch) -> None:
    principal_id = "exec-property-search-run-repair-before-terminal"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Repair Worker Office")

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        if callable(progress_callback):
            progress_callback(
                step="source_repair_needed",
                message="Provider returned a repairable extraction issue.",
                status="in_progress",
                steps_delta=1,
                summary_updates={
                    "sources_total": 1,
                    "sources": [
                        {
                            "source_url": "https://provider.example/search",
                            "source_label": "Provider Example",
                            "provider_repair_task_opened_total": 1,
                            "provider_repair_tasks": [{"status": "pending", "filter_key": "missing_price"}],
                        }
                    ],
                },
            )
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 1,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "email_notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [
                {
                    "source_url": "https://provider.example/search",
                    "source_label": "Provider Example",
                    "provider_repair_task_opened_total": 1,
                    "provider_repair_tasks": [{"status": "pending", "filter_key": "missing_price"}],
                }
            ],
        }

    def _fake_process_property_provider_repair_tasks(
        self,
        *,
        principal_id: str,
        actor: str,
        limit: int = 40,
        run_id: str = "",
    ) -> dict[str, object]:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            active = [
                state
                for state in product_service._PROPERTY_SEARCH_RUN_REGISTRY.values()
                if isinstance(state, dict)
                and str(state.get("principal_id") or "").strip() == principal_id
                and str(state.get("status") or "").strip().lower() == "in_progress"
            ]
            assert active
            state = active[-1]
            assert str(state.get("run_id") or "") == run_id
            summary = dict(state.get("summary") or {})
            summary["repair_receipts"] = [
                {
                    "at": product_service._now_iso(),
                    "run_id": state["run_id"],
                    "source_url": "https://provider.example/search",
                    "source_label": "Provider Example",
                    "filter_key": "missing_price",
                    "resolution": "suppressed_missing_price",
                    "reason": "provider page still lacks a concrete price",
                    "actor": actor,
                    "human_task_id": "human_task:repair-before-terminal",
                    "repair_workflow": "ea_provider_ooda",
                }
            ]
            summary["repair_resolved_total"] = 1
            state["summary"] = summary
        return {"generated_at": product_service._now_iso(), "resolved_total": 1, "deferred_total": 0, "resolved": []}

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)
    monkeypatch.setattr(ProductService, "process_property_provider_repair_tasks", _fake_process_property_provider_repair_tasks)

    started = client.post(
        "/app/api/property/search-runs",
        json={"selected_platforms": ["willhaben"], "property_preferences": {"country_code": "AT"}},
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    status = _poll_property_search_run_status(client, run_id)

    assert status["status"] == "processed"
    summary = dict(status["summary"])
    assert summary["repair_resolved_total"] == 1
    source = dict(summary["sources"][0])
    assert source["repair_status"] == "returned"
    assert source["provider_repair_tasks"][0]["status"] == "returned"
    assert source["provider_repair_tasks"][0]["resolution"] == "suppressed_missing_price"


def test_property_provider_greenfield_api_returns_country_scoped_catalog() -> None:
    client = build_property_client(principal_id="exec-property-provider-greenfield-api")

    at_response = client.get("/app/api/property/providers", params={"country": "AT"})
    uk_response = client.get("/app/api/property/providers", params={"country": "UK"})
    cr_response = client.get("/app/api/property/providers", params={"country": "CR"})

    assert at_response.status_code == 200, at_response.text
    assert uk_response.status_code == 400, uk_response.text
    assert cr_response.status_code == 200, cr_response.text
    at_body = at_response.json()
    cr_body = cr_response.json()
    assert at_body["country_code"] == "AT"
    assert cr_body["country_code"] == "CR"
    assert any(row["value"] == "willhaben" for row in at_body["providers"])
    assert any(row["value"] == "immowelt_at" and "immowelt" in row["label"].lower() for row in at_body["providers"])
    assert any(row["value"] == "findmyhome_at" and "FindMyHome" in row["label"] for row in at_body["providers"])
    assert any(row["value"] == "derstandard_at" and "STANDARD" in row["label"] for row in at_body["providers"])
    assert any(row["value"] == "remax_at" and "RE/MAX Austria" in row["label"] for row in at_body["providers"])
    assert any(row["value"] == "glorit_at" and row["family"] == "developer_projects" for row in at_body["providers"])
    assert any(row["value"] == "wag_at" and row["family"] == "cooperative" for row in at_body["providers"])
    assert any(row["value"] == "heimat_oesterreich_at" and row["family"] == "cooperative" for row in at_body["providers"])
    assert any(row["value"] == "bwsg_at" and row["family"] == "cooperative" for row in at_body["providers"])
    assert any(row["value"] == "arwag_at" and row["family"] == "developer_projects" for row in at_body["providers"])
    assert any(row["value"] == "raiffeisen_wohnbau_at" and row["family"] == "developer_projects" for row in at_body["providers"])
    assert any(row["value"] == "ohne_makler_at" and row["family"] == "broker_direct" for row in at_body["providers"])
    assert any(row["value"] == "sreal_at" and row["family"] == "broker_direct" for row in at_body["providers"])
    assert any(row["value"] == "raiffeisen_immobilien_at" and row["family"] == "broker_direct" for row in at_body["providers"])
    assert any(row["value"] == "wohnnet_at" and row["family"] == "marketplace" for row in at_body["providers"])
    assert any(row["value"] == "keinmakler_at" and row["family"] == "broker_direct" for row in at_body["providers"])
    assert uk_response.json()["error"]["code"] == "unsupported_property_market"
    assert any(row["value"] == "encuentra24_cr" for row in cr_body["providers"])
    assert any(row["value"] == "re_cr_mls" for row in cr_body["providers"])
    assert any(row["value"] == "properstar_cr" and row["family"] == "marketplace" for row in cr_body["providers"])
    assert any(row["value"] == "century21_cr" and row["family"] == "broker_direct" for row in cr_body["providers"])
    assert any(row["value"] == "remax_cr" and row["family"] == "broker_direct" for row in cr_body["providers"])
    assert any(row["value"] == "theagency_cr" and row["family"] == "broker_direct" for row in cr_body["providers"])
    assert any(row["value"] == "krain_cr" and row["family"] == "broker_direct" for row in cr_body["providers"])
    assert any(row["value"] == "desarrollos_cr" and row["family"] == "developer_projects" for row in cr_body["providers"])
    assert any(row["value"] == "tierraverde_cr" and row["family"] == "developer_projects" for row in cr_body["providers"])
    assert any(row["value"] == "propertiesincostarica_cr" and row["family"] == "broker_direct" for row in cr_body["providers"])
    assert any(row["value"] == "costaricarealestateservice_cr" and row["family"] == "broker_direct" for row in cr_body["providers"])
    assert any(row["value"] == "twocostaricarealestate_cr" and row["family"] == "broker_direct" for row in cr_body["providers"])


def test_property_search_run_can_be_deleted_from_api(monkeypatch) -> None:
    principal_id = "exec-property-search-run-delete"
    client = build_property_client(principal_id=principal_id)

    def _fake_sync_direct_property_scout(self, *, principal_id: str, selected_platforms, property_search_preferences, force_refresh: bool = False):
        return {
            "summary": {
                "ranked_candidates": [],
                "sources": [],
                "sources_total": 0,
                "listing_total": 0,
            }
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": {"country_code": "AT"},
            "max_results_per_source": 1,
        },
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]

    deleted = client.delete(f"/app/api/property/search-runs/{run_id}")
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["run_id"] == run_id
    assert body["deleted"] is True

    missing = client.get(f"/app/api/property/search-runs/{run_id}")
    assert missing.status_code == 404, missing.text


def test_property_search_runs_can_be_cleared_for_current_principal_only() -> None:
    principal_id = "exec-property-search-run-clear"
    other_principal_id = "exec-property-search-run-clear-other"
    client = build_property_client(principal_id=principal_id)
    current_run_id = "clear-current-run"
    other_run_id = "clear-other-run"
    current_form_run_id = "clear-current-form-run"

    current_record = product_service._new_property_search_run_record(
        run_id=current_run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT"},
        force_refresh=False,
    )
    other_record = product_service._new_property_search_run_record(
        run_id=other_run_id,
        principal_id=other_principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT"},
        force_refresh=False,
    )
    current_form_record = product_service._new_property_search_run_record(
        run_id=current_form_run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT"},
        force_refresh=False,
    )
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        previous_registry = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY)
        product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[current_run_id] = current_record
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[other_run_id] = other_record
    try:
        cleared = client.delete("/app/api/property/search-runs")
        assert cleared.status_code == 200, cleared.text
        body = cleared.json()
        assert body["deleted_count"] == 1
        assert body["run_ids"] == [current_run_id]
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            assert current_run_id not in product_service._PROPERTY_SEARCH_RUN_REGISTRY
            assert other_run_id in product_service._PROPERTY_SEARCH_RUN_REGISTRY

        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY[current_form_run_id] = current_form_record
        form_cleared = client.post("/app/api/property/search-runs/clear", follow_redirects=False)
        assert form_cleared.status_code == 303, form_cleared.text
        assert form_cleared.headers["location"] == "/app/account?history_cleared=1#data-export"
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            assert current_form_run_id not in product_service._PROPERTY_SEARCH_RUN_REGISTRY
            assert other_run_id in product_service._PROPERTY_SEARCH_RUN_REGISTRY
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.update(previous_registry)


def test_property_search_runs_keep_recent_history_but_prune_stale_payloads_by_default(monkeypatch) -> None:
    monkeypatch.delenv("EA_PROPERTY_SEARCH_RUN_RETENTION_SECONDS", raising=False)
    assert product_service._property_search_run_retention_seconds() == 90 * 24 * 60 * 60
    assert property_search_storage.property_search_run_retention_policy() == {
        "property_search_run_retention_status": "enabled",
        "property_search_run_retention_seconds": "7776000",
        "property_search_run_retention_days": "90.0",
        "property_search_run_retention_env": "EA_PROPERTY_SEARCH_RUN_RETENTION_SECONDS",
        "property_search_run_retention_default_seconds": "7776000",
    }
    run_id = "retained-recent-run"
    stale_run_id = "pruned-stale-run"
    principal_id = "exec-property-search-run-retained"
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    old_record = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT"},
        force_refresh=False,
    )
    old_record["created_at"] = old_timestamp
    old_record["updated_at"] = old_timestamp
    stale_record = dict(old_record)
    stale_record["run_id"] = stale_run_id
    stale_record["created_at"] = stale_timestamp
    stale_record["updated_at"] = stale_timestamp

    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        previous_registry = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY)
        product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = old_record
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[stale_run_id] = stale_record
    try:
        product_service._prune_property_search_runs()
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            assert run_id in product_service._PROPERTY_SEARCH_RUN_REGISTRY
            assert stale_run_id in product_service._PROPERTY_SEARCH_RUN_REGISTRY
            stale_saved_result = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY[stale_run_id])
            assert stale_saved_result["payload_retention_status"] == "compact_only"
            assert stale_saved_result["run_id"] == stale_run_id
            assert stale_saved_result["principal_id"] == principal_id
            assert "summary" in stale_saved_result
            assert "selected_platforms" in stale_saved_result
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.update(previous_registry)


def test_property_search_run_retention_env_allows_explicit_admin_pruning(monkeypatch) -> None:
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_RETENTION_SECONDS", "60")
    run_id = "explicit-retention-old-run"
    principal_id = "exec-property-search-run-explicit-retention"
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    old_record = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT"},
        force_refresh=False,
    )
    old_record["created_at"] = old_timestamp
    old_record["updated_at"] = old_timestamp

    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        previous_registry = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY)
        product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = old_record
    try:
        product_service._prune_property_search_runs()
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            assert run_id in product_service._PROPERTY_SEARCH_RUN_REGISTRY
            saved_result = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id])
            assert saved_result["payload_retention_status"] == "compact_only"
            assert saved_result["run_id"] == run_id
            assert saved_result["principal_id"] == principal_id
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.update(previous_registry)


def test_property_provider_catalog_generates_remax_austria_sources() -> None:
    rows = property_market_catalog.generated_source_specs(
        preferences={
            "country_code": "AT",
            "listing_mode": "buy",
            "location_query": "Wien",
            "min_area_m2": 70,
        },
        selected_platforms=("remax",),
        principal_id="exec-property-remax-source",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["platform"] == "remax_at"
    assert row["provider_family"] == "broker_direct"
    assert row["url"].startswith("https://www.remax.at/en/properties/propertysearch")
    assert "q=Wien" in row["url"]
    assert row["fetch_timeout_seconds"] == 8
    assert "https://www.remax.at/de/ib/remax-first-wien/immobilien" in row["fallback_listing_urls"]
    assert row["provider_filter_pushdown"]["applied"]["min_area_m2"] == 70


def test_property_provider_catalog_generates_glorit_austria_buy_sources() -> None:
    rows = property_market_catalog.generated_source_specs(
        preferences={
            "country_code": "AT",
            "region_code": "wien",
            "listing_mode": "buy",
            "location_query": "1220 Wien Alte Donau",
            "min_area_m2": 55,
            "max_price_eur": 650000,
        },
        selected_platforms=("glorit",),
        principal_id="exec-property-glorit-source",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["platform"] == "glorit_at"
    assert row["provider_family"] == "developer_projects"
    assert row["source_access_level"] == "public"
    assert row["provider_governance"]["access_mode"] == "browser_public_web"
    assert row["provider_governance"]["browser_access_allowed"] is True
    assert row["url"].startswith("https://glorit.at/wohnung-kaufen/")
    assert "q=1220+Wien+Alte+Donau" in row["url"]
    assert "maxPrice=650000" in row["url"]
    assert "minArea=55" in row["url"]
    assert row["fetch_timeout_seconds"] == 10
    assert "https://glorit.at/wohnung-kaufen/1220-wien-arminenstrasse-4a-8?t=top201" in row["fallback_listing_urls"]
    assert row["provider_filter_pushdown"]["applied"]["location_query"] == "1220 Wien Alte Donau"
    assert property_market_catalog.normalize_property_platform("glorit.at") == "glorit_at"


def test_property_search_run_rejects_invalid_platform_and_enforces_run_principal_scope(monkeypatch) -> None:
    principal_id = "exec-property-search-run-scope"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Run Scope Office")

    response = client.post(
        "/app/api/signals/property/search/run",
        json={"selected_platforms": ["not-a-real-platform"]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_property_provider"

    observed_sync: dict[str, object] = {}

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed_sync["called"] = True
        observed_sync["selected_platforms"] = tuple(selected_platforms)
        observed_sync["force_refresh"] = bool(force_refresh)
        observed_sync["max_results_per_source"] = max_results_per_source
        if callable(progress_callback):
            progress_callback(
                step="mock-progress",
                message="mocked from onboarding prefs",
                status="in_progress",
                steps_delta=3,
                summary_updates={"sources_total": 1},
            )
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 1,
            "review_created_total": 1,
            "review_existing_total": 0,
            "notified_total": 1,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 1,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    owner = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "selected_platforms": ["willhaben", "kalandra"],
            "preference_person_id": "elisabeth",
            "max_results_per_source": 2,
            "property_commercial": {"active_plan_key": "plus", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert owner.status_code == 200, owner.text

    started = client.post("/app/api/signals/property/search/run", json={"property_preferences": {}})
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    assert observed_sync.get("called") is True
    assert set(observed_sync.get("selected_platforms") or ()) == {"willhaben", "kalandra"}

    status = _poll_property_search_run_status(client, run_id)
    assert status["status"] == "processed"
    assert status["summary"]["sources_total"] == 1

    intruder = build_property_client(principal_id="intruder-property-search-run-scope")
    intruder_status = intruder.get(f"/app/api/signals/property/search/run/{run_id}")
    assert intruder_status.status_code == 404


def test_property_search_run_rejects_market_outside_current_customer_scope() -> None:
    principal_id = "cf-email:bootstrap.market@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Bootstrap Request Office")

    started = client.post(
        "/app/api/signals/property/search/run",
        json={
            "property_preferences": {
                "country_code": "NO",
                "language_code": "en",
                "listing_mode": "buy",
                "location_query": "Oslo",
            }
        },
    )
    assert started.status_code == 400, started.text
    assert started.json()["error"]["code"] == "unsupported_property_market"


def test_property_search_run_sends_results_ready_email_when_processed(monkeypatch) -> None:
    principal_id = "cf-email:results.ready@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Results Ready Office")

    sent: list[dict[str, object]] = []

    class _Receipt:
        provider = "emailit"
        message_id = "results-ready-1"
        accepted_at = "2026-06-04T12:00:00+00:00"

    monkeypatch.setattr(
        product_service,
        "send_property_search_results_ready_email",
        lambda **kwargs: sent.append(dict(kwargs)) or _Receipt(),
    )
    monkeypatch.setattr(product_service.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_verified_open_url",
        lambda value, *, principal_id="": str(value) if "/control/3dvista" in str(value) else "",
    )

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 3,
            "review_created_total": 2,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 1,
            "tour_existing_total": 1,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [
                {
                    "source_label": "Willhaben",
                    "top_candidates": [
                        {
                            "title": "Best floorplan flat",
                            "fit_score": 88.0,
                            "fit_summary": "Personal fit 88/100",
                            "review_url": "https://propertyquarry.com/workspace-access/review-token?return_to=%2Fapp%2Fhandoffs%2Fhuman_task%3Areview-1",
                            "tour_url": "https://propertyquarry.com/tours/best-floorplan-flat/control/3dvista",
                            "tour_status": "created",
                        }
                    ],
                }
            ],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post(
        "/app/api/signals/property/search/run",
        json={"selected_platforms": ["willhaben"], "property_preferences": {"country_code": "AT", "location_query": "Wien"}},
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    status = _poll_property_search_run_status(client, run_id)
    assert status["status"] == "processed"
    assert sent
    assert sent[0]["recipient_email"] == "results.ready@example.com"
    assert sent[0]["result_total"] == 3
    assert sent[0]["hosted_tour_total"] == 2
    assert urllib.parse.quote(f"/app/properties?run_id={run_id}", safe="/") in str(sent[0]["results_url"])
    assert sent[0]["top_properties"][0]["title"] == "Best floorplan flat"
    assert sent[0]["top_properties"][0]["review_url"].startswith("https://propertyquarry.com/workspace-access/")
    assert str(sent[0]["top_properties"][0]["review_url"]).endswith("return_to=%2Fapp%2Fhandoffs%2Fhuman_task%3Areview-1")
    assert "return_to=%2Ftours%2Fbest-floorplan-flat%2Fcontrol%2F3dvista" in str(sent[0]["top_properties"][0]["tour_url"])


def test_property_search_results_ready_email_waits_for_tour_completion(monkeypatch) -> None:
    principal_id = "cf-email:tour.wait@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Results Finalization Office")

    sent: list[dict[str, object]] = []

    class _Receipt:
        provider = "emailit"
        message_id = "results-ready-2"
        accepted_at = "2026-06-04T12:05:00+00:00"

    monkeypatch.setattr(
        product_service,
        "send_property_search_results_ready_email",
        lambda **kwargs: sent.append(dict(kwargs)) or _Receipt(),
    )
    monkeypatch.setattr(product_service.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_verified_open_url",
        lambda value, *, principal_id="": str(value) if "/control/3dvista" in str(value) else "",
    )

    poll_state = {"calls": 0}

    def _fake_recent_observations(limit: int = 1000, principal_id: str = "", **_kwargs) -> list[object]:
        poll_state["calls"] += 1
        if poll_state["calls"] < 2:
            return []
        return [
            SimpleNamespace(
                channel="product",
                event_type="generic_property_tour_created",
                source_id="property-scout:test-1",
                payload={
                    "tour_url": "https://propertyquarry.com/tours/final-tour/control/3dvista",
                    "vendor_tour_url": "",
                },
                created_at=product_service._now_iso(),
            )
        ]

    monkeypatch.setattr(
        client.app.state.container.channel_runtime,
        "list_recent_observations_matching",
        _fake_recent_observations,
    )

    service = product_service.build_product_service(client.app.state.container)
    result = {
        "status": "processed",
        "listing_total": 1,
        "sources": [
            {
                "source_label": "Willhaben",
                "top_candidates": [
                    {
                        "source_ref": "property-scout:test-1",
                        "tour_status": "queued",
                        "tour_url": "",
                        "blocked_reason": "",
                        "property_facts": {"has_360": True},
                    }
                ],
            }
        ],
    }
    run_id = "run-final-1"
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "Vienna"},
        force_refresh=False,
    )
    state.update({"status": "processed", "summary": dict(result)})
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = state
    try:
        service._await_property_search_results_delivery_ready(
            principal_id=principal_id,
            run_id=run_id,
            result=result,
            timeout_seconds=1,
            poll_interval_seconds=0.01,
        )
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.pop(run_id, None)

    assert sent
    assert sent[0]["hosted_tour_total"] == 1


def test_property_search_results_delivery_preserves_completed_partial_status(monkeypatch) -> None:
    principal_id = "cf-email:partial.delivery@example.com"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"run-partial-delivery-{uuid.uuid4().hex}"
    result = {
        "status": "completed_partial",
        "failed_total": 6,
        "listing_total": 64,
        "reviewed_listing_total": 447,
        "repair_status": "degraded",
        "repair_status_label": "Partial coverage",
        "sources": [],
    }
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": product_service._now_iso(),
            "updated_at": product_service._now_iso(),
            "status": "completed_partial",
            "progress": 100,
            "current_step": "completed",
            "message": "Search run completed with status completed_partial.",
            "stages_total": 6,
            "steps_completed": 6,
            "summary": dict(result),
            "events": [],
            "selected_platforms": ["century21_cr"],
        }

    monkeypatch.setattr(ProductService, "_recent_product_event_exists", lambda self, **kwargs: False)
    monkeypatch.setattr(ProductService, "_notify_property_search_results_ready", lambda self, **kwargs: None)

    service._await_property_search_results_delivery_ready(
        principal_id=principal_id,
        run_id=run_id,
        result=result,
        timeout_seconds=1,
        poll_interval_seconds=0.01,
    )

    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        final_state = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id])
    assert final_state["status"] == "completed_partial"
    assert dict(final_state["summary"])["status"] == "completed_partial"
    assert [event["status"] for event in final_state["events"]] == [
        "completed_partial",
        "completed_partial",
    ]
    assert [event["step"] for event in final_state["events"]] == [
        "results_finalizing",
        "results_email_sent",
    ]


def test_property_search_results_delivery_refresh_batches_tour_observation_lookup(monkeypatch) -> None:
    verified_open_url_calls: list[dict[str, str]] = []

    def _verified_open_url(value: object, *, principal_id: str = "") -> str:
        verified_open_url_calls.append(
            {
                "tour_url": str(value or ""),
                "principal_id": str(principal_id or ""),
            }
        )
        return str(value) if "/control/3dvista" in str(value) else ""

    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_verified_open_url",
        _verified_open_url,
    )
    service = ProductService.__new__(ProductService)

    class _Runtime:
        calls = 0

        def list_recent_observations(self, limit: int = 1000, principal_id: str = "") -> list[object]:
            self.calls += 1
            return [
                SimpleNamespace(
                    channel="product",
                    event_type="generic_property_tour_created",
                    source_id="source-1",
                    payload={
                        "property_url": "https://example.test/flat-1",
                        "tour_url": "https://propertyquarry.com/tours/flat-1/control/3dvista",
                        "vendor_tour_url": "",
                    },
                    created_at=product_service._now_iso(),
                )
            ]

    runtime = _Runtime()
    service._container = SimpleNamespace(channel_runtime=runtime)

    refreshed = service._refresh_property_search_results_delivery_state(
        principal_id="cf-email:batched-tour-refresh@example.com",
        result={
            "sources": [
                {
                    "source_label": "Source",
                    "top_candidates": [
                        {
                            "source_ref": "source-1",
                            "property_url": "https://example.test/flat-1",
                            "tour_status": "queued",
                            "property_facts": {"has_360": True},
                        },
                        {
                            "source_ref": "source-2",
                            "property_url": "https://example.test/flat-2",
                            "tour_status": "queued",
                            "property_facts": {"has_360": True},
                        },
                    ],
                }
            ],
        },
    )

    assert runtime.calls == 1
    assert verified_open_url_calls == [
        {
            "tour_url": "https://propertyquarry.com/tours/flat-1/control/3dvista",
            "principal_id": "cf-email:batched-tour-refresh@example.com",
        }
    ]
    assert refreshed["ready_tour_total"] == 1
    assert refreshed["pending_tour_total"] == 1
    candidates = refreshed["sources"][0]["top_candidates"]
    assert candidates[0]["tour_url"] == "https://propertyquarry.com/tours/flat-1/control/3dvista"
    assert candidates[1].get("tour_url") in {"", None}


def test_property_search_results_delivery_refresh_does_not_full_scan_when_targeted_query_is_empty() -> None:
    service = ProductService.__new__(ProductService)

    class _Runtime:
        matching_calls = 0
        legacy_calls = 0

        def list_recent_observations_matching(self, **_kwargs) -> list[object]:
            self.matching_calls += 1
            return []

        def list_recent_observations(self, **_kwargs) -> list[object]:
            self.legacy_calls += 1
            return []

    runtime = _Runtime()
    service._container = SimpleNamespace(channel_runtime=runtime)

    refreshed = service._refresh_property_search_results_delivery_state(
        principal_id="cf-email:empty-targeted-tour-query@example.com",
        result={
            "sources": [
                {
                    "source_label": "Source",
                    "top_candidates": [
                        {
                            "source_ref": "source-1",
                            "property_url": "https://example.test/flat-1",
                            "tour_status": "queued",
                            "property_facts": {"has_360": True},
                        }
                    ],
                }
            ],
        },
    )

    assert runtime.matching_calls == 1
    assert runtime.legacy_calls == 0
    assert refreshed["ready_tour_total"] == 0
    assert refreshed["pending_tour_total"] == 1


def test_property_search_results_delivery_refresh_does_not_promote_generated_reconstruction_to_ready(monkeypatch) -> None:
    service = ProductService.__new__(ProductService)

    class _Runtime:
        def list_recent_observations(self, limit: int = 1000, principal_id: str = "") -> list[object]:
            return [
                SimpleNamespace(
                    channel="product",
                    event_type="generic_property_tour_created",
                    source_id="source-1",
                    payload={
                        "property_url": "https://example.test/flat-1",
                        "tour_url": "https://propertyquarry.com/tours/generated-flat-1",
                    },
                    created_at=product_service._now_iso(),
                )
            ]

    service._container = SimpleNamespace(channel_runtime=_Runtime())
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_first_party_open_url",
        lambda _url: "",
    )

    refreshed = service._refresh_property_search_results_delivery_state(
        principal_id="cf-email:generated-reconstruction-ready@example.com",
        result={
            "sources": [
                {
                    "source_label": "Source",
                    "top_candidates": [
                        {
                            "source_ref": "source-1",
                            "property_url": "https://example.test/flat-1",
                            "tour_status": "queued",
                            "property_facts": {"has_360": True},
                        }
                    ],
                }
            ],
        },
    )

    candidate = refreshed["sources"][0]["top_candidates"][0]
    assert candidate.get("tour_url") == "https://propertyquarry.com/tours/generated-flat-1"
    assert candidate["tour_status"] == "created"
    assert refreshed["ready_tour_total"] == 0
    assert refreshed["pending_tour_total"] == 0


def test_property_search_run_status_snapshot_recovers_tour_readiness_without_email_side_effect(monkeypatch) -> None:
    principal_id = "cf-email:tour.restart@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Results Restart Office")

    sent: list[dict[str, object]] = []

    class _Receipt:
        provider = "emailit"
        message_id = "results-ready-3"
        accepted_at = "2026-06-04T12:10:00+00:00"

    monkeypatch.setattr(
        product_service,
        "send_property_search_results_ready_email",
        lambda **kwargs: sent.append(dict(kwargs)) or _Receipt(),
    )
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_verified_open_url",
        lambda value, **_kwargs: str(value) if "/control/3dvista" in str(value) else "",
    )

    container = client.app.state.container
    service = product_service.build_product_service(container)
    run_id = "run-final-2"
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "Vienna"},
        force_refresh=False,
    )
    state["status"] = "processed"
    state["summary"] = {
        "status": "processed",
        "listing_total": 1,
        "sources": [
            {
                "source_label": "Willhaben",
                "top_candidates": [
                    {
                        "source_ref": "property-scout:test-2",
                        "tour_status": "queued",
                        "tour_url": "",
                        "blocked_reason": "",
                        "property_facts": {"has_360": True},
                    }
                ],
            }
        ],
    }
    product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)

    monkeypatch.setattr(
        container.channel_runtime,
        "list_recent_observations_matching",
        lambda **_kwargs: [
            SimpleNamespace(
                channel="product",
                event_type="generic_property_tour_created",
                source_id="property-scout:test-2",
                payload={"tour_url": "https://propertyquarry.com/tours/recovered-tour/control/3dvista"},
                created_at=product_service._now_iso(),
            )
        ],
    )

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert sent == []
    assert status["summary"]["ready_tour_total"] == 1


def test_property_search_run_status_marks_stale_active_run_failed(monkeypatch) -> None:
    principal_id = "cf-email:stale.run@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Stale Run Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_STALE_SECONDS", "60")

    container = client.app.state.container
    service = product_service.build_product_service(container)
    replacement_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        ProductService,
        "_start_property_search_repair_replacement_run",
        lambda self, **kwargs: replacement_calls.append(dict(kwargs)) or {"run_id": "run-stale-1-repair"},
    )
    run_id = "run-stale-1"
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "Vienna"},
        force_refresh=False,
    )
    state["status"] = "in_progress"
    state["progress"] = 1
    state["events"] = [
        {
            "step": "source_previewing",
            "status": "in_progress",
            "message": "Reviewing candidate 25 of 60 for Willhaben | Austria | Rent | 1010 Vienna.",
        }
    ]
    state["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert status["status"] == "failed"
    assert status["progress"] == 100
    assert status["summary"]["interrupted"] is True
    assert status["summary"]["repair_status"] == "repairing"
    assert status["summary"]["repair_status_label"] == "Checking again"
    assert status["summary"]["repair_replacement_run_id"] == "run-stale-1-repair"
    assert status["summary"]["repair_replacement_status_url"] == "/app/api/signals/property/search/run/run-stale-1-repair"
    assert status["summary"]["provider_repair_task_opened_total"] == 1
    assert any(event["step"] == "run_interrupted" for event in status["events"])
    assert any(event["step"] == "run_repair_queued" for event in status["events"])

    tasks = [
        task
        for task in container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert len(tasks) == 1
    assert tasks[0].priority == "urgent"
    repair_input = dict(tasks[0].input_json or {})
    assert repair_input["filter_key"] == "run_interrupted_stale"
    assert repair_input["run_id"] == run_id
    assert repair_input["source_label"] == "Willhaben | Austria | Rent | 1010 Vienna"
    assert repair_input["diagnostics"]["failure_class"] == "run_interrupted_stale"
    assert replacement_calls
    assert replacement_calls[0]["selected_platforms"] == ("willhaben",)
    assert replacement_calls[0]["property_search_preferences"]["location_query"] == "Vienna"

    status_again = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    assert status_again is not None
    assert len(replacement_calls) == 1
    tasks_again = [
        task
        for task in container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert len(tasks_again) == 1


def test_property_search_run_status_marks_source_previewing_stale_by_last_progress_event_even_if_repair_updates_row(monkeypatch) -> None:
    principal_id = "cf-email:stale.source.previewing@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Stale Source Previewing Repair Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_STALE_SECONDS", "60")

    container = client.app.state.container
    service = product_service.build_product_service(container)
    replacement_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        ProductService,
        "_start_property_search_repair_replacement_run",
        lambda self, **kwargs: replacement_calls.append(dict(kwargs)) or {"run_id": "run-source-previewing-stale-repair"},
    )
    run_id = "run-source-previewing-stale"
    stale_event_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna"},
        force_refresh=False,
    )
    state["status"] = "in_progress"
    state["progress"] = 23
    state["current_step"] = "source_previewing"
    state["events"] = [
        {
            "at": stale_event_at,
            "step": "source_previewing",
            "status": "in_progress",
            "message": "Reviewing candidate 20 of 24 for Willhaben | Austria | Rent | 1010 Vienna.",
        }
    ]
    state["summary"] = {
        **dict(state.get("summary") or {}),
        "repair_receipts": [
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "filter_key": "require_floorplan",
                "resolution": "provider_quarantined_retry_budget_exhausted",
            }
        ],
    }
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert status["status"] == "failed"
    assert status["summary"]["interrupted"] is True
    assert status["summary"]["repair_status"] == "repairing"
    assert status["summary"]["repair_replacement_run_id"] == "run-source-previewing-stale-repair"
    assert any(event["step"] == "run_interrupted" for event in status["events"])
    assert any(event["step"] == "run_repair_queued" for event in status["events"])
    assert replacement_calls


def test_property_search_run_worker_exception_opens_generic_repair_task(monkeypatch) -> None:
    principal_id = "cf-email:worker.exception@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Worker Exception Repair Office")
    service = product_service.build_product_service(client.app.state.container)

    def _raise_worker_failure(self, **kwargs):
        raise RuntimeError("provider merge crashed before source rows existed")

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _raise_worker_failure)
    replacement_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        ProductService,
        "_start_property_search_repair_replacement_run",
        lambda self, **kwargs: replacement_calls.append(dict(kwargs)) or {"run_id": "worker-exception-repair-run"},
    )
    run = service.start_property_search_run(
        principal_id=principal_id,
        actor="test",
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
        },
        force_refresh=True,
        max_results_per_source=1,
    )

    status = _poll_property_search_run_status(client, str(run["run_id"]))

    assert status["status"] == "failed"
    assert status["summary"]["repair_status"] == "repairing"
    assert status["summary"]["repair_step_label"] == "Started a fresh search from the saved brief."
    assert status["summary"]["repair_replacement_run_id"] == "worker-exception-repair-run"
    assert status["summary"]["repair_replacement_status_url"] == "/app/api/signals/property/search/run/worker-exception-repair-run"
    assert status["summary"]["provider_repair_task_opened_total"] == 1
    assert status["summary"]["repair_receipts"][0]["resolution"] == "worker_exception_restart_required"
    assert status["summary"]["provider_repair_tasks"][0]["status"] == "returned"
    assert status["summary"]["provider_repair_tasks"][0]["replacement_run_id"] == "worker-exception-repair-run"
    assert any(event["step"] == "run_repair_queued" for event in status["events"])
    tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert len(tasks) == 1
    assert tasks[0].priority == "urgent"
    assert tasks[0].assigned_operator_id == "ea_one_manager"
    assert tasks[0].status == "returned"
    repair_input = dict(tasks[0].input_json or {})
    assert repair_input["filter_key"] == "run_worker_exception"
    assert repair_input["run_id"] == run["run_id"]
    assert repair_input["diagnostics"]["failure_class"] == "run_worker_exception"
    assert repair_input["diagnostics"]["error"] == "provider merge crashed before source rows existed"
    assert replacement_calls
    assert replacement_calls[0]["selected_platforms"] == ("willhaben",)
    assert replacement_calls[0]["property_search_preferences"]["location_query"] == "1010 Vienna"

    repaired_status = service.get_property_search_run_status(principal_id=principal_id, run_id=str(run["run_id"]))
    assert repaired_status["summary"]["repair_replacement_run_id"] == "worker-exception-repair-run"
    assert repaired_status["summary"]["repair_replacement_status_url"] == "/app/api/signals/property/search/run/worker-exception-repair-run"
    assert repaired_status["summary"]["repair_receipts"][0]["resolution"] == "worker_exception_restart_required"

    lightweight_status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=str(run["run_id"]),
        lightweight=True,
    )
    assert lightweight_status["summary"]["repair_replacement_run_id"] == "worker-exception-repair-run"


def test_property_provider_repair_quarantines_stale_deferred_source_fetch(monkeypatch) -> None:
    principal_id = "cf-email:provider.quarantine@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Provider Quarantine Repair Office")
    service = ProductService(client.app.state.container)
    run_id = f"provider-quarantine-{uuid.uuid4().hex}"
    source_url = "https://kalandra.example.invalid/search"
    now = product_service._now_iso()
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": now,
            "updated_at": now,
            "status": "failed",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["kalandra", "willhaben"],
            "progress": 100,
            "message": "Search stopped before all provider checks finished.",
            "summary": {
                "status": "failed",
                "ranked_candidates": [{"candidate_ref": "cand-1", "title": "Recovered Willhaben hit"}],
                "sources": [
                    {
                        "source_url": source_url,
                        "source_label": "Kalandra | Austria | Rent | 1010 Vienna",
                        "status": "failed",
                        "error": "temporary fetch failed",
                    }
                ],
            },
        }
    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url=source_url,
        title="Kalandra source fetch failed",
        source_url=source_url,
        source_label="Kalandra | Austria | Rent | 1010 Vienna",
        source_platform="kalandra",
        source_family="private_portal",
        filter_key="source_fetch",
        diagnostics={"provider_host": "kalandra.example.invalid", "error": "timeout", "repair_attempts": 3},
        source_ref="property-source:kalandra",
        run_id=run_id,
    )
    assert opened["status"] == "opened"
    tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status="pending",
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert len(tasks) == 1
    monkeypatch.setenv("EA_PROPERTY_PROVIDER_REPAIR_RETRY_BUDGET_SECONDS", "60")
    monkeypatch.setattr(
        ProductService,
        "_auto_resolve_property_provider_repair_task",
        lambda self, *, principal_id, task, actor: {"status": "deferred", "reason": "manual_provider_patch_required"},
    )

    repair_summary = service.process_property_provider_repair_tasks(
        principal_id=principal_id,
        actor="test",
        limit=5,
    )

    assert repair_summary["resolved_total"] == 1
    assert repair_summary["deferred_total"] == 0
    assert repair_summary["resolved"][0]["resolution"] == "provider_quarantined_retry_budget_exhausted"
    task_after = client.app.state.container.orchestrator.list_human_tasks(
        principal_id=principal_id,
        status="returned",
        limit=20,
    )[0]
    assert task_after.resolution == "provider_quarantined_retry_budget_exhausted"

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    assert status["status"] == "completed_partial"
    summary = dict(status["summary"])
    source = dict(summary["sources"][0])
    assert source["status"] == "repaired"
    assert source["repair_status"] == "returned"
    assert source["repair_resolution"] == "provider_quarantined_retry_budget_exhausted"
    assert source["original_error"] == "temporary fetch failed"
    assert summary["repair_receipts"][0]["resolution"] == "provider_quarantined_retry_budget_exhausted"
    assert summary["repair_resolved_total"] == 1


def test_run_scene_video_skill_routes_propertyquarry_through_shared_ea_task(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_property_client(principal_id="exec-property-scene-video-shared")
    service = ProductService(client.app.state.container)
    observed: dict[str, object] = {}

    def _fake_execute_task_artifact(request):
        observed["skill_key"] = request.skill_key
        observed["principal_id"] = request.principal_id
        observed["goal"] = request.goal
        observed["text"] = request.text
        observed["input_json"] = dict(request.input_json or {})
        return SimpleNamespace(
            structured_output_json={
                "deliverable_type": "scene_video_packet",
                "provider_key": "omagic",
                "provider_backend_key": "onemin_i2v",
                "video_url": "https://cdn.example/propertyquarry/flythrough.mp4",
            },
            content="",
        )

    monkeypatch.setattr(service._container.orchestrator, "execute_task_artifact", _fake_execute_task_artifact)

    packet = service._run_scene_video_skill(
        title="PropertyQuarry flythrough",
        actor="test-worker",
        provider_key="magic",
        input_json={
            "context_kind": "property_walkthrough",
            "tour_url": "https://property.example/tours/alpha",
        },
    )

    assert observed["skill_key"] == "scene_video_generate"
    assert observed["principal_id"] == "ea-scene-video-test-worker"
    assert observed["text"] == "https://property.example/tours/alpha"
    assert observed["input_json"] == {
        "title": "PropertyQuarry flythrough",
        "context_kind": "property_walkthrough",
        "tour_url": "https://property.example/tours/alpha",
        "provider_key": "omagic",
    }
    assert packet["deliverable_type"] == "scene_video_packet"
    assert packet["provider_key"] == "omagic"
    assert packet["provider_backend_key"] == "onemin_i2v"


def test_property_scout_flythrough_uses_governed_render_lane_with_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_property_client(principal_id="exec-property-scout-flythrough")
    service = ProductService(client.app.state.container)
    observed: dict[str, object] = {"delivery_calls": 0}

    def _fake_resolve_property_walkthrough_runtime_provider(provider_key: str):
        observed["requested_provider_key"] = provider_key
        return {
            "provider_key": "magic",
            "provider_backend_key": "magic",
            "runtime_readiness_json": {"ready": True, "blockers": []},
        }

    def _fake_issue_owned_governed_property_flythrough_consent(
        *,
        tour_url: str,
        principal_id: str,
        preferred_provider_key: str,
        external_processing_consent_granted: bool,
    ):
        observed["consent"] = {
            "tour_url": tour_url,
            "principal_id": principal_id,
            "preferred_provider_key": preferred_provider_key,
            "external_processing_consent_granted": external_processing_consent_granted,
        }
        return "governed-consent-receipt", ""

    def _fake_render_property_flythrough_into_hosted_tour(**kwargs):
        observed["render"] = dict(kwargs)
        return {
            "status": "pending",
            "reason": "governed_render_request_accepted",
            "provider_key": "magic",
            "execution_lane": "ea_governed_render",
            "public_ready": False,
            "launch_eligible": False,
            "video_url": "",
            "flythrough_url": "",
            "governed_render_request_id": "governed-request-north-tower",
            "governed_render_receipt": {
                "request_id": "governed-request-north-tower",
                "status": "accepted",
                "receipt_sha256": "a" * 64,
            },
        }

    def _fake_hosted_property_tour_video_delivery(tour_url: str) -> dict[str, object]:
        assert tour_url == "https://property.example/tours/north-tower"
        observed["delivery_calls"] = int(observed["delivery_calls"]) + 1
        return {}

    from app.services import scene_video_contract

    monkeypatch.setattr(
        scene_video_contract,
        "resolve_property_walkthrough_runtime_provider",
        _fake_resolve_property_walkthrough_runtime_provider,
    )
    monkeypatch.setattr(
        product_service,
        "_issue_owned_governed_property_flythrough_consent",
        _fake_issue_owned_governed_property_flythrough_consent,
    )
    monkeypatch.setattr(
        product_service,
        "_render_property_flythrough_into_hosted_tour",
        _fake_render_property_flythrough_into_hosted_tour,
    )
    monkeypatch.setattr(product_service, "_hosted_property_tour_video_delivery", _fake_hosted_property_tour_video_delivery)
    monkeypatch.setattr(service, "_record_product_event", lambda **kwargs: observed.setdefault("event_payload", kwargs["payload"]))

    result = service._maybe_render_property_scout_flythrough(
        principal_id="exec-property-scout-flythrough",
        actor="property-scout",
        title="North Tower",
        property_url="https://property.example/listings/north-tower",
        source_ref="property-source:north-tower",
        tour_result={"tour_url": "https://property.example/tours/north-tower"},
        property_facts={"bedrooms": 3},
        fit_score=0.0,
        allow_below_threshold=True,
        diorama_style_hint="miniature realism",
        walkthrough_provider_key="magic",
        external_processing_consent_granted=True,
    )

    assert observed["requested_provider_key"] == "magic"
    assert observed["consent"] == {
        "tour_url": "https://property.example/tours/north-tower",
        "principal_id": "exec-property-scout-flythrough",
        "preferred_provider_key": "magic",
        "external_processing_consent_granted": True,
    }
    render = dict(observed["render"])
    assert render["title"] == "North Tower"
    assert render["actor"] == "property-scout"
    assert render["principal_id"] == "exec-property-scout-flythrough"
    assert render["tour_url"] == "https://property.example/tours/north-tower"
    assert render["property_facts"] == {"bedrooms": 3}
    assert render["diorama_style_hint"] == "miniature realism"
    assert render["preferred_provider_key"] == "magic"
    assert render["external_processing_consent_receipt"] == "governed-consent-receipt"
    assert isinstance(render.get("tour_context_json"), dict)
    assert observed["delivery_calls"] == 2
    assert result["status"] == "pending"
    assert result["reason"] == "governed_render_request_accepted"
    assert result["provider_key"] == "magic"
    assert result["execution_lane"] == "ea_governed_render"
    assert result["public_ready"] is False
    assert result["launch_eligible"] is False
    assert result["governed_render_request_id"] == "governed-request-north-tower"
    assert result["governed_render_receipt"] == {
        "request_id": "governed-request-north-tower",
        "status": "accepted",
        "receipt_sha256": "a" * 64,
    }
    assert result["tour_url"] == "https://property.example/tours/north-tower"
    assert result["video_url"] == ""
    assert result["flythrough_url"] == ""
    assert result["delivery_provider_key"] == ""
    assert result["delivery_duration_seconds"] == 0.0


def test_property_search_run_state_builds_stale_failure_event() -> None:
    event = product_service._state_property_search_run_stale_failure_event(
        {"status": "in_progress"},
        stale_seconds=20 * 60,
    )

    assert event["step"] == "run_interrupted"
    assert event["status"] == "failed"
    assert "more than 20 minutes" in str(event["message"])
    assert dict(event["summary_updates"]) == {
        "interrupted": True,
        "stale_after_seconds": 1200,
        "last_known_status": "in_progress",
    }
    assert event["force_status"] == "failed"


def test_property_search_run_state_builds_stale_partial_event_when_shortlist_exists() -> None:
    event = product_service._state_property_search_run_stale_failure_event(
        {
            "status": "in_progress",
            "summary": {
                "listing_total": 26,
                "ranked_candidates": [{"title": "Recovered hit"}],
            },
        },
        stale_seconds=20 * 60,
    )

    assert event["step"] == "run_interrupted"
    assert event["status"] == "completed_partial"
    assert "current shortlist is still available" in str(event["message"])
    assert dict(event["summary_updates"])["repair_status"] == "degraded"
    assert dict(event["summary_updates"])["repair_status_label"] == "Partial coverage"
    assert event["force_status"] == "completed_partial"


def test_property_search_run_status_marks_stale_partial_run_completed_partial(monkeypatch) -> None:
    principal_id = "cf-email:stale.partial@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Stale Partial Run Office")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_STALE_SECONDS", "60")

    container = client.app.state.container
    service = product_service.build_product_service(container)
    run_id = "run-stale-partial-1"
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "Vienna"},
        force_refresh=False,
    )
    state["status"] = "in_progress"
    state["progress"] = 86
    state["summary"] = {
        "listing_total": 26,
        "ranked_candidates": [{"title": "Recovered hit"}],
    }
    state["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert status["status"] == "completed_partial"
    assert status["progress"] == 100
    assert status["summary"]["interrupted"] is True
    assert status["summary"]["repair_status"] == "degraded"
    assert any(event["step"] == "run_interrupted" for event in status["events"])


def test_property_search_run_terminal_outcome_prefers_partial_success_for_mixed_sources() -> None:
    assert product_service._state_property_search_run_terminal_outcome(
        sources_total=5,
        failed_total=2,
        successful_source_total=3,
    ) == "completed_partial"
    assert product_service._state_property_search_run_terminal_outcome(
        sources_total=5,
        failed_total=0,
        successful_source_total=5,
    ) == "processed"
    assert product_service._state_property_search_run_terminal_outcome(
        sources_total=5,
        failed_total=5,
        successful_source_total=0,
    ) == "failed"


def test_property_search_provider_totals_distinguish_integrations_from_groups() -> None:
    specs = [
        {
            "platform": "willhaben",
            "provider_family": "core_portal",
            "label": "Willhaben | Austria | Rent | 1010 Vienna",
            "url": "https://www.willhaben.at/iad/immobilien/mietwohnungen?q=1010+Vienna",
        },
        {
            "platform": "willhaben",
            "provider_family": "core_portal",
            "label": "Willhaben | Austria | Rent | 1020 Vienna",
            "url": "https://www.willhaben.at/iad/immobilien/mietwohnungen?q=1020+Vienna",
        },
        {
            "platform": "derstandard_at",
            "provider_family": "core_portal",
            "label": "DER STANDARD Immobilien | Austria | Rent | 1020 Vienna",
            "url": "https://immobilien.derstandard.at/search/1020",
        },
        {
            "platform": "gesiba_at",
            "provider_family": "housing_coop",
            "label": "GESIBA | Austria | Rent | 1020 Vienna",
            "url": "https://www.gesiba.at/search/1020",
        },
        {
            "platform": "oesw_at",
            "provider_family": "housing_coop",
            "label": "ÖSW | Austria | Rent | 1020 Vienna",
            "url": "https://www.oesw.at/search/1020",
        },
    ]

    assert product_service._property_search_provider_total(specs) == 4
    assert product_service._property_search_provider_group_total(specs) == 2


def test_property_search_run_state_syncs_summary_projection() -> None:
    summary = product_service._state_property_search_run_sync_summary(
        state={"status": "in_progress", "progress": 42},
        summary={"listing_total": 3},
        terminal_statuses={"processed", "completed", "failed", "cancelled", "noop"},
        eta_seconds=360,
        eta_label="about 6 min",
    )

    assert summary["status"] == "in_progress"
    assert summary["progress"] == 42
    assert summary["progress_percent"] == 42
    assert summary["eta_seconds"] == 360
    assert summary["eta_label"] == "about 6 min"


def test_property_search_run_progress_stays_zero_during_early_bootstrap_without_real_source_output() -> None:
    progress, eta_seconds, eta_label = product_service._property_search_run_progress_projection(
        state={
            "created_at": "2026-01-01T00:00:00Z",
            "progress": 0,
        },
        step="source_fetching",
        status="in_progress",
        summary={
            "sources_total": 8,
            "sources": [],
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
        },
        stages_total=120,
        steps_completed=14,
    )

    assert progress == 0
    assert eta_seconds == 0
    assert eta_label == ""


def test_property_search_run_progress_stays_zero_during_bootstrap_before_sources_are_materialized() -> None:
    progress, eta_seconds, eta_label = product_service._property_search_run_progress_projection(
        state={
            "created_at": "2026-01-01T00:00:00Z",
            "progress": 0,
        },
        step="starting",
        status="in_progress",
        summary={
            "sources_total": 0,
            "sources": [],
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
        },
        stages_total=12,
        steps_completed=1,
    )

    assert progress == 0
    assert eta_seconds == 0
    assert eta_label == ""


def test_property_search_run_progress_advances_once_real_source_output_exists() -> None:
    progress, eta_seconds, eta_label = product_service._property_search_run_progress_projection(
        state={
            "created_at": "2026-01-01T00:00:00Z",
            "progress": 0,
        },
        step="source_extracting",
        status="in_progress",
        summary={
            "sources_total": 8,
            "sources": [{"source_label": "Willhaben"}],
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
        },
        stages_total=120,
        steps_completed=15,
    )

    assert progress > 0
    assert isinstance(eta_seconds, int)
    assert isinstance(eta_label, str)


def test_property_search_run_progress_repairs_preseeded_source_overcount() -> None:
    summary = {
        "sources_total": 10,
        "source_variant_total": 10,
        "sources_completed": 10,
        "sources": [
            *[
                {"source_label": f"Finished {index}", "status": "completed"}
                for index in range(6)
            ],
            {"source_label": "Running", "status": "in_progress"},
            {"source_label": "Starting", "status": "starting"},
            {"source_label": "Queued A", "status": "queued"},
            {"source_label": "Queued B", "status": "queued"},
        ],
        "listing_total": 32,
        "reviewed_listing_total": 342,
    }

    progress, eta_seconds, eta_label = product_service._property_search_run_progress_projection(
        state={
            "created_at": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
            "progress": 96,
        },
        step="source_ranking",
        status="in_progress",
        summary=summary,
        stages_total=100,
        steps_completed=60,
    )

    assert summary["sources_completed"] == 6
    assert 0 < progress < 96
    assert eta_seconds > 0
    assert eta_label


def test_property_search_run_state_applies_event_and_caps_history() -> None:
    state = {
        "status": "queued",
        "progress": 0,
        "stages_total": 4,
        "steps_completed": 0,
        "summary": {"sources_total": 2, "sources": [{"source": "a"}]},
        "events": [{"at": f"2026-01-01T00:00:{index:02d}Z", "step": "queued", "message": "queued", "status": "queued"} for index in range(240)],
    }

    updated = product_service._state_property_search_run_apply_event(
        state=state,
        step="source_started",
        message="Scanning source.",
        status="in_progress",
        steps_delta=1,
        summary_updates={"listing_total": 3},
        force_status="",
        stages_total_override=None,
        terminal_statuses={"processed", "completed", "failed", "cancelled", "noop"},
        default_stages_total=4,
        now_iso=lambda: "2026-01-01T01:00:00Z",
        compact_text=product_service.compact_text,
        progress_projection=product_service._property_search_run_progress_projection,
        sync_summary=product_service._state_property_search_run_sync_summary,
    )

    assert updated["status"] == "in_progress"
    assert updated["current_step"] == "source_started"
    assert updated["message"] == "Scanning source."
    assert updated["steps_completed"] == 1
    assert updated["summary"]["listing_total"] == 3
    assert updated["summary"]["status"] == "in_progress"
    assert isinstance(updated["summary"]["progress_percent"], int)
    assert len(updated["events"]) == 240
    assert updated["events"][-1]["step"] == "source_started"
    assert updated["updated_at"] == "2026-01-01T01:00:00Z"


def test_property_search_run_event_syncs_summary_status_and_progress() -> None:
    principal_id = "cf-email:summary.sync@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Summary Sync Office")
    service = product_service.build_product_service(client.app.state.container)
    run_id = "run-summary-sync-1"
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "Vienna"},
        force_refresh=False,
    )
    product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)

    service._record_property_search_run_event(
        run_id=run_id,
        principal_id=principal_id,
        step="source_started",
        message="Scanning source.",
        status="in_progress",
        steps_delta=1,
    )

    updated = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id])
    assert updated["summary"]["status"] == "in_progress"
    assert isinstance(updated["summary"]["progress_percent"], int)


def test_property_alert_review_open_timeout_returns_failed_payload(monkeypatch) -> None:
    principal_id = "cf-email:timeout@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Review Timeout Office")
    service = product_service.build_product_service(client.app.state.container)
    monkeypatch.setenv("EA_PROPERTY_SEARCH_REVIEW_OPEN_TIMEOUT_SECONDS", "1")

    recorded: list[dict[str, object]] = []

    def _fake_record_product_event(self, *, principal_id: str, event_type: str, payload: dict[str, object], source_id: str = "", dedupe_key: str = "") -> None:  # type: ignore[no-untyped-def]
        recorded.append(
            {
                "principal_id": principal_id,
                "event_type": event_type,
                "payload": dict(payload),
                "source_id": source_id,
                "dedupe_key": dedupe_key,
            }
        )

    def _fake_open(*args, **kwargs):  # type: ignore[no-untyped-def]
        time.sleep(1.2)
        return {"status": "opened"}

    monkeypatch.setattr(ProductService, "_record_product_event", _fake_record_product_event)
    monkeypatch.setattr(ProductService, "_open_property_alert_review", _fake_open)

    result = service._open_property_alert_review_with_timeout(
        principal_id=principal_id,
        title="Delayed packet",
        summary="This review creation hangs.",
        source_ref="property-scout:timeout",
        external_id="https://example.com/listing",
        counterparty="Willhaben",
        account_email="timeout@example.com",
        property_url="https://example.com/listing",
        actor="property_scout",
        notify_telegram=False,
    )

    assert result["status"] == "failed"
    assert result["reason"] == "property_alert_review_open_timeout"
    assert recorded
    assert any(row["event_type"] == "property_alert_review_open_timeout" for row in recorded)


def test_property_search_inline_review_packet_limit_is_bounded(monkeypatch) -> None:
    monkeypatch.delenv("PROPERTYQUARRY_SEARCH_INLINE_REVIEW_PACKET_LIMIT", raising=False)
    monkeypatch.delenv("EA_PROPERTY_SEARCH_INLINE_REVIEW_PACKET_LIMIT", raising=False)

    assert product_service._property_search_inline_review_packet_limit() == 3
    assert product_service._property_search_should_prepare_inline_review_packet(row_index=1, inline_limit=3) is True
    assert product_service._property_search_should_prepare_inline_review_packet(row_index=3, inline_limit=3) is True
    assert product_service._property_search_should_prepare_inline_review_packet(row_index=4, inline_limit=3) is False

    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_INLINE_REVIEW_PACKET_LIMIT", "1")
    assert product_service._property_search_inline_review_packet_limit() == 1
    assert product_service._property_search_should_prepare_inline_review_packet(row_index=2, inline_limit=1) is False

    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_INLINE_REVIEW_PACKET_LIMIT", "0")
    assert product_service._property_search_inline_review_packet_limit() == 0
    assert product_service._property_search_should_prepare_inline_review_packet(row_index=1, inline_limit=0) is False

    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_INLINE_REVIEW_PACKET_LIMIT", "500")
    assert product_service._property_search_inline_review_packet_limit() == 50


def test_property_search_run_status_survives_registry_loss_via_persisted_record(monkeypatch) -> None:
    principal_id = "exec-property-search-run-persisted"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Persisted Office")

    persisted: dict[str, dict[str, object]] = {}

    def _fake_store(record: dict[str, object]) -> None:
        persisted[str(record.get("run_id") or "")] = dict(record)

    def _fake_load(*, run_id: str, principal_id: str = "") -> dict[str, object] | None:
        row = persisted.get(run_id)
        if principal_id and str(dict(row or {}).get("principal_id") or "").strip() != str(principal_id or "").strip():
            return None
        return dict(row) if isinstance(row, dict) else None

    monkeypatch.setattr(product_service, "_store_property_search_run_record", _fake_store)
    monkeypatch.setattr(product_service, "_load_property_search_run_record", _fake_load)

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        if callable(progress_callback):
            progress_callback(
                step="mock-progress",
                message="persisted status event",
                status="in_progress",
                steps_delta=2,
                summary_updates={"sources_total": 1},
            )
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post("/app/api/signals/property/search/run", json={"selected_platforms": ["willhaben"]})
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    status = _poll_property_search_run_status(client, run_id)
    assert status["status"] == "processed"

    product_service._PROPERTY_SEARCH_RUN_REGISTRY.pop(run_id, None)

    reloaded = client.get(f"/app/api/signals/property/search/run/{run_id}")
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["status"] == "processed"


def test_property_search_run_can_finish_completed_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-run-completed-partial"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Partial Run Office")

    persisted: dict[str, dict[str, object]] = {}

    def _fake_store(record: dict[str, object]) -> None:
        persisted[str(record.get("run_id") or "")] = dict(record)

    def _fake_load(*, run_id: str, principal_id: str = "") -> dict[str, object] | None:
        row = persisted.get(run_id)
        if principal_id and str(dict(row or {}).get("principal_id") or "").strip() != str(principal_id or "").strip():
            return None
        return dict(row) if isinstance(row, dict) else None

    monkeypatch.setattr(product_service, "_store_property_search_run_record", _fake_store)
    monkeypatch.setattr(product_service, "_load_property_search_run_record", _fake_load)

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        if callable(progress_callback):
            progress_callback(
                step="source_failed",
                message="One provider degraded but others finished.",
                status="in_progress",
                steps_delta=1,
                summary_updates={"sources_total": 3, "failed_total": 1},
            )
        return {
            "generated_at": product_service._now_iso(),
            "status": "completed_partial",
            "sources_total": 3,
            "listing_total": 12,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 2,
            "watch_notified_total": 0,
            "failed_total": 1,
            "repair_status": "degraded",
            "sources": [{"source_label": "Willhaben"}, {"source_label": "Gesiba", "error": "provider degraded"}],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post("/app/api/signals/property/search/run", json={"selected_platforms": ["willhaben"]})
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    status = _poll_property_search_run_status(client, run_id)
    assert status["status"] == "completed_partial"
    assert status["summary"]["failed_total"] == 1


def test_property_search_preferences_persist_and_merge_into_run(monkeypatch) -> None:
    principal_id = "exec-property-search-run-merge"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Merge Office")
    seed_product_state(client, principal_id=principal_id)

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "selected_platforms": ["willhaben", "kalandra"],
            "preference_person_id": "elisabeth",
            "max_results_per_source": 50,
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["property_search_preferences"]["max_results_per_source"] is None

    status_snapshot = client.get("/v1/onboarding/property-search/preferences")
    assert status_snapshot.status_code == 200
    assert set(status_snapshot.json()["property_search_preferences"]["selected_platforms"]) == {"willhaben", "kalandra"}


def test_property_search_preferences_persist_what_matters_round_trip_fields() -> None:
    principal_id = "exec-property-search-what-matters-round-trip"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search What Matters")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "search_goal": "home",
            "listing_mode": "rent",
            "property_type": ["apartment"],
            "location_query": "1200 Vienna",
            "selected_location_values": ["1200 Vienna"],
            "selected_platforms": ["willhaben"],
            "enable_family_mode": True,
            "school_stage_preferences": ["kindergarten", "ganztags_volksschule"],
            "school_evidence_priority": "important",
            "max_distance_to_kindergarten_m": 400,
            "max_distance_to_kindergarten_importance": "must_have",
            "max_distance_to_ganztags_volksschule_m": 650,
            "max_distance_to_ganztags_volksschule_importance": "important",
            "max_distance_to_market_m": 1000,
            "max_distance_to_market_importance": "nice_to_have",
            "max_distance_to_supermarket_m": 500,
            "max_distance_to_supermarket_importance": "strong_wish",
            "max_distance_to_playground_m": 450,
            "max_distance_to_playground_importance": "avoid",
            "parking_pressure_preference": "low",
            "require_parking_pressure_check": True,
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )

    assert stored.status_code == 200, stored.text
    preferences = stored.json()["property_search_preferences"]
    assert preferences["search_goal"] == "home"
    assert preferences["school_stage_preferences"] == ["kindergarten", "ganztags_volksschule"]
    assert preferences["school_evidence_priority"] == "important"
    assert preferences["max_distance_to_kindergarten_m"] == 400
    assert preferences["max_distance_to_kindergarten_importance"] == "must_have"
    assert preferences["max_distance_to_ganztags_volksschule_m"] == 650
    assert preferences["max_distance_to_ganztags_volksschule_importance"] == "strong_wish"
    assert preferences["max_distance_to_market_m"] == 1000
    assert preferences["max_distance_to_market_importance"] == "nice_to_have"
    assert preferences["max_distance_to_supermarket_m"] == 500
    assert preferences["max_distance_to_supermarket_importance"] == "strong_wish"
    assert preferences["max_distance_to_playground_m"] == 450
    assert preferences["max_distance_to_playground_importance"] == "avoid"
    assert preferences["parking_pressure_preference"] == "low"
    assert preferences["require_parking_pressure_check"] is True

    status_snapshot = client.get("/v1/onboarding/property-search/preferences")
    assert status_snapshot.status_code == 200, status_snapshot.text
    persisted = status_snapshot.json()["property_search_preferences"]
    assert persisted["search_goal"] == "home"
    assert persisted["school_stage_preferences"] == ["kindergarten", "ganztags_volksschule"]
    assert persisted["school_evidence_priority"] == "important"
    assert persisted["max_distance_to_kindergarten_m"] == 400
    assert persisted["max_distance_to_kindergarten_importance"] == "must_have"
    assert persisted["max_distance_to_ganztags_volksschule_m"] == 650
    assert persisted["max_distance_to_ganztags_volksschule_importance"] == "strong_wish"
    assert persisted["max_distance_to_market_m"] == 1000
    assert persisted["max_distance_to_market_importance"] == "nice_to_have"
    assert persisted["max_distance_to_supermarket_m"] == 500
    assert persisted["max_distance_to_supermarket_importance"] == "strong_wish"
    assert persisted["max_distance_to_playground_m"] == 450
    assert persisted["max_distance_to_playground_importance"] == "avoid"
    assert persisted["parking_pressure_preference"] == "low"
    assert persisted["require_parking_pressure_check"] is True


@pytest.fixture(scope="module")
def catalog_distance_preferences_client():
    principal_id = "exec-property-search-catalog-distance-round-trip"
    client = build_property_client(principal_id=principal_id)
    start_workspace(
        client,
        mode="personal",
        workspace_name="Property Search Catalog Distance Contract",
    )
    return client


@pytest.mark.parametrize(
    "distance_key",
    _CANONICAL_CATALOG_DISTANCE_RADIUS_KEYS,
    ids=lambda value: str(value).removeprefix("max_distance_to_").removesuffix("_m"),
)
@pytest.mark.parametrize(
    "importance",
    _CANONICAL_DISTANCE_IMPORTANCE_STATES,
)
def test_property_search_preferences_api_round_trips_every_catalog_distance_and_importance_state(
    catalog_distance_preferences_client,
    distance_key: str,
    importance: str,
) -> None:
    importance_key = property_fact_distance_importance_key(distance_key)
    stored = catalog_distance_preferences_client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "search_goal": "home",
            "listing_mode": "rent",
            "property_type": ["apartment"],
            "location_query": "Vienna",
            distance_key: 650,
            importance_key: importance,
        },
    )

    assert stored.status_code == 200, stored.text
    stored_preferences = stored.json()["property_search_preferences"]
    assert stored_preferences[distance_key] == 650
    assert stored_preferences[importance_key] == importance

    reloaded = catalog_distance_preferences_client.get(
        "/v1/onboarding/property-search/preferences"
    )
    assert reloaded.status_code == 200, reloaded.text
    reloaded_preferences = reloaded.json()["property_search_preferences"]
    assert reloaded_preferences[distance_key] == 650
    assert reloaded_preferences[importance_key] == importance


def test_property_search_preferences_api_canonicalizes_legacy_distance_importance_alias() -> None:
    principal_id = "exec-property-search-distance-important-alias"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Distance importance alias")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "max_distance_to_supermarket_m": 500,
            "max_distance_to_supermarket_importance": "important",
        },
    )

    assert stored.status_code == 200, stored.text
    assert (
        stored.json()["property_search_preferences"]
        ["max_distance_to_supermarket_importance"]
        == "strong_wish"
    )
    persisted = client.get("/v1/onboarding/property-search/preferences")
    assert persisted.status_code == 200, persisted.text
    assert (
        persisted.json()["property_search_preferences"]
        ["max_distance_to_supermarket_importance"]
        == "strong_wish"
    )


@pytest.mark.parametrize(
    "invalid_patch",
    (
        {"max_distance_to_unicorn_m": 500},
        {"max_distance_to_supermarket_importance": "critical"},
        {"max_distance_to_supermarket_m": 7001},
        {"max_distance_to_supermarket_m ": 500},
        {"max_distance_to_supermarket_importance ": "strong_wish"},
        {"MAX_DISTANCE_TO_SUPERMARKET_M": 500},
        {"MAX_DISTANCE_TO_UNKNOWN_M": 500},
        {"raw_preferences": {"max_distance_to_supermarket_m ": 500}},
        {"raw_preferences": {"max_distance_to_supermarket_m": 7001}},
    ),
    ids=(
        "unknown-field",
        "unknown-importance",
        "radius-out-of-range",
        "radius-trailing-space",
        "importance-trailing-space",
        "uppercase-known",
        "uppercase-unknown",
        "nested-trailing-space",
        "nested-radius-out-of-range",
    ),
)
def test_property_search_preferences_api_rejects_invalid_distance_contract_fields(
    invalid_patch: dict[str, object],
) -> None:
    principal_id = "exec-property-search-invalid-distance-contract"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Invalid distance contract")

    response = client.post(
        "/v1/onboarding/property-search/preferences",
        json={"country_code": "AT", **invalid_patch},
    )

    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "malformed_key",
    (
        "max_distance_to_supermarket_m ",
        "max_distance_to_supermarket_importance ",
        "MAX_DISTANCE_TO_SUPERMARKET_M",
        "MAX_DISTANCE_TO_UNKNOWN_M",
    ),
)
def test_property_search_preferences_service_drops_malformed_distance_keys(
    malformed_key: str,
) -> None:
    normalized = OnboardingService._normalize_property_search_preferences(
        {
            "country_code": "AT",
            malformed_key: 500,
            "raw_preferences": {malformed_key: 500},
        }
    )

    assert malformed_key not in normalized
    assert malformed_key not in json.dumps(normalized, sort_keys=True)


@pytest.mark.parametrize("importance", _CANONICAL_DISTANCE_IMPORTANCE_STATES)
def test_property_search_preferences_api_drops_orphan_distance_importance(
    importance: str,
) -> None:
    principal_id = f"exec-property-search-orphan-distance-{importance}"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Orphan distance importance")
    importance_key = "max_distance_to_pharmacy_importance"

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "max_distance_to_pharmacy_m": None,
            importance_key: importance,
        },
    )

    assert stored.status_code == 200, stored.text
    assert importance_key not in json.dumps(
        stored.json()["property_search_preferences"],
        sort_keys=True,
    )
    reloaded = client.get("/v1/onboarding/property-search/preferences")
    assert reloaded.status_code == 200, reloaded.text
    assert importance_key not in json.dumps(
        reloaded.json()["property_search_preferences"],
        sort_keys=True,
    )


def test_property_search_preferences_active_distance_defaults_missing_importance_and_migrates_alias() -> None:
    normalized = OnboardingService._normalize_property_search_preferences(
        {
            "max_distance_to_supermarket_m": 500,
            "max_distance_to_subway_m": 333,
            "max_distance_to_underground_importance": "important",
            "max_distance_to_pharmacy_importance": "avoid",
        }
    )

    assert normalized["max_distance_to_supermarket_importance"] == "nice_to_have"
    assert normalized["max_distance_to_subway_importance"] == "strong_wish"
    assert "max_distance_to_underground_importance" not in normalized
    assert "max_distance_to_pharmacy_importance" not in json.dumps(
        normalized,
        sort_keys=True,
    )


def test_property_search_preferences_api_active_distance_defaults_missing_importance() -> None:
    principal_id = "exec-property-search-default-distance-importance"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Default distance importance")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={"max_distance_to_supermarket_m": 500},
    )

    assert stored.status_code == 200, stored.text
    preferences = stored.json()["property_search_preferences"]
    assert preferences["max_distance_to_supermarket_m"] == 500
    assert preferences["max_distance_to_supermarket_importance"] == "nice_to_have"


def test_property_search_preferences_round_trip_all_hidden_what_matters_distance_backing_fields() -> None:
    principal_id = "exec-property-search-what-matters-hidden-distance-fields"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Hidden What Matters")

    search_response = client.get("/app/search")
    assert search_response.status_code == 200, search_response.text
    contract_match = re.search(
        r'<script type="application/json" data-property-distance-preference-contract>(.*?)</script>',
        search_response.text,
        re.S,
    )
    assert contract_match is not None
    compact_contract = json.loads(contract_match.group(1))
    numeric_fields = [str(row[0]) for row in compact_contract]
    importance_fields = [
        property_fact_distance_importance_key(name) for name in numeric_fields
    ]
    assert tuple(numeric_fields) == _CANONICAL_CATALOG_DISTANCE_RADIUS_KEYS
    assert all(
        len(row) == 3
        and row[2] in _CANONICAL_DISTANCE_IMPORTANCE_STATES
        for row in compact_contract
    )
    assert "max_distance_to_pharmacy_m" in numeric_fields
    assert "max_distance_to_pharmacy_importance" in importance_fields
    assert "max_distance_to_subway_m" in numeric_fields
    assert "max_distance_to_subway_importance" in importance_fields
    assert "max_distance_to_university_m" in numeric_fields
    assert "max_distance_to_university_importance" in importance_fields

    importance_cycle = _CANONICAL_DISTANCE_IMPORTANCE_STATES
    payload = {
        "country_code": "AT",
        "region_code": "vienna",
        "search_goal": "home",
        "listing_mode": "rent",
        "property_type": ["apartment"],
        "location_query": "1200 Vienna",
        "selected_location_values": ["1200 Vienna"],
        "selected_platforms": ["willhaben"],
        "enable_family_mode": True,
        "school_stage_preferences": [
            "kindergarten",
            "ganztags_volksschule",
            "halbtags_volksschule",
        ],
        "school_evidence_priority": "important",
        "parking_pressure_preference": "medium",
        "require_parking_pressure_check": True,
        "property_commercial": {
            "active_plan_key": "agent",
            "status": "active",
            "active_until": "2999-01-01T00:00:00+00:00",
        },
    }
    for index, name in enumerate(numeric_fields):
        payload[name] = 300 + (index * 50)
    for index, name in enumerate(importance_fields):
        payload[name] = importance_cycle[index % len(importance_cycle)]

    stored = client.post("/v1/onboarding/property-search/preferences", json=payload)
    assert stored.status_code == 200, stored.text
    preferences = stored.json()["property_search_preferences"]

    assert preferences["search_goal"] == "home"
    assert preferences["school_evidence_priority"] == "important"
    assert preferences["parking_pressure_preference"] == "medium"
    assert preferences["require_parking_pressure_check"] is True
    for name in numeric_fields:
        assert preferences[name] == payload[name], name
    for name in importance_fields:
        assert preferences[name] == payload[name], name
    assert preferences["max_distance_to_pharmacy_m"] == payload[
        "max_distance_to_pharmacy_m"
    ]
    assert preferences["max_distance_to_pharmacy_importance"] == payload[
        "max_distance_to_pharmacy_importance"
    ]
    assert preferences["max_distance_to_subway_m"] == payload[
        "max_distance_to_subway_m"
    ]
    assert preferences["max_distance_to_subway_importance"] == payload[
        "max_distance_to_subway_importance"
    ]
    assert preferences["max_distance_to_university_m"] == payload[
        "max_distance_to_university_m"
    ]
    assert preferences["max_distance_to_university_importance"] == payload[
        "max_distance_to_university_importance"
    ]

    status_snapshot = client.get("/v1/onboarding/property-search/preferences")
    assert status_snapshot.status_code == 200, status_snapshot.text
    persisted = status_snapshot.json()["property_search_preferences"]
    assert persisted["search_goal"] == "home"
    assert persisted["school_evidence_priority"] == "important"
    assert persisted["parking_pressure_preference"] == "medium"
    assert persisted["require_parking_pressure_check"] is True
    for name in numeric_fields:
        assert persisted[name] == payload[name], name
    for name in importance_fields:
        assert persisted[name] == payload[name], name
    assert persisted["max_distance_to_pharmacy_m"] == payload[
        "max_distance_to_pharmacy_m"
    ]
    assert persisted["max_distance_to_pharmacy_importance"] == payload[
        "max_distance_to_pharmacy_importance"
    ]
    assert persisted["max_distance_to_subway_m"] == payload[
        "max_distance_to_subway_m"
    ]
    assert persisted["max_distance_to_subway_importance"] == payload[
        "max_distance_to_subway_importance"
    ]
    assert persisted["max_distance_to_university_m"] == payload[
        "max_distance_to_university_m"
    ]
    assert persisted["max_distance_to_university_importance"] == payload[
        "max_distance_to_university_importance"
    ]


def test_agent_property_search_preferences_drop_stale_result_cap_when_saved() -> None:
    principal_id = "exec-property-agent-preferences-unlimited"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Agent Unlimited")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "selected_platforms": ["willhaben"],
            "max_results_per_source": 7,
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )

    assert stored.status_code == 200, stored.text
    assert stored.json()["property_search_preferences"]["max_results_per_source"] is None


def test_plus_property_search_preferences_drop_stale_result_cap_when_saved() -> None:
    principal_id = "exec-property-plus-preferences-clamped"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Plus Clamp")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "selected_platforms": ["willhaben"],
            "max_results_per_source": 50,
            "property_commercial": {
                "active_plan_key": "plus",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )

    assert stored.status_code == 200, stored.text
    assert stored.json()["property_search_preferences"]["max_results_per_source"] is None


def test_property_search_preferences_persist_full_region_scope_as_hard_location_scope() -> None:
    principal_id = "exec-property-search-all-vienna-scope"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Vienna Scope")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "full_region_scope": True,
            "location_query": "",
            "selected_platforms": ["willhaben"],
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )

    assert stored.status_code == 200, stored.text
    preferences = stored.json()["property_search_preferences"]
    assert preferences["country_code"] == "AT"
    assert preferences["region_code"] == "vienna"
    assert preferences["full_region_scope"] is True
    assert preferences["location_query"] == "Vienna"


def test_property_search_preferences_normalize_country_names_before_saving() -> None:
    principal_id = "exec-property-search-country-name-scope"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Country Name Scope")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "Costa Rica",
            "listing_mode": "sale",
            "property_type": "land",
            "location_query": "Tamarindo",
            "selected_platforms": ["encuentra24_cr"],
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )

    assert stored.status_code == 200, stored.text
    preferences = stored.json()["property_search_preferences"]
    assert preferences["country_code"] == "CR"
    assert preferences["language_code"] == "es"
    assert preferences["listing_mode"] == "buy"
    assert preferences["property_type"] == "land"
    assert preferences["location_query"] == "Tamarindo"


def test_direct_property_scout_uses_saved_preferences_and_respects_disabled_flag(monkeypatch) -> None:
    principal_id = "exec-property-direct-saved-preferences"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Direct Saved Preferences")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "property_search_enabled": False,
            "alert_frequency": "disabled",
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert stored.status_code == 200, stored.text

    service = product_service.build_product_service(client.app.state.container)
    disabled = service.sync_direct_property_scout(principal_id=principal_id, actor="scheduler")

    assert disabled["status"] == "noop"
    assert disabled["noop_reason"] == "property_search_disabled"

    enabled = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
            "alert_frequency": "daily",
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert enabled.status_code == 200, enabled.text
    observed: dict[str, object] = {}

    def _fake_generated_specs(**kwargs):
        observed["preferences"] = dict(kwargs.get("preferences") or {})
        observed["selected_platforms"] = tuple(kwargs.get("selected_platforms") or ())
        return ()

    monkeypatch.setattr(product_service, "generated_property_source_specs", _fake_generated_specs)

    result = service.sync_direct_property_scout(principal_id=principal_id, actor="scheduler")

    assert result["status"] == "noop"
    assert observed["preferences"]["location_query"] == "Wien"
    assert observed["preferences"]["listing_mode"] == "rent"
    assert observed["selected_platforms"] == ("willhaben",)
    assert observed["preferences"]["selected_platforms"] == ["willhaben"]
    assert result["timing_receipts"]["sources_resolved_at"]
    assert result["timing_receipts"]["results_delivery_ready_at"]
    assert result["timing_receipts"]["completed_at"]


def test_direct_property_scout_filters_cross_country_platforms_from_runtime_preferences(monkeypatch) -> None:
    principal_id = "exec-property-direct-country-gate"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Direct Country Gate")
    service = product_service.build_product_service(client.app.state.container)
    observed: dict[str, object] = {}

    def _fake_generated_specs(**kwargs):
        observed["preferences"] = dict(kwargs.get("preferences") or {})
        observed["selected_platforms"] = tuple(kwargs.get("selected_platforms") or ())
        return ()

    monkeypatch.setattr(product_service, "generated_property_source_specs", _fake_generated_specs)

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="scheduler",
        property_search_preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["realestate_au", "willhaben"],
            "property_search_enabled": True,
            "alert_frequency": "daily",
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )

    assert result["status"] == "noop"
    assert observed["selected_platforms"] == ("willhaben",)
    assert observed["preferences"]["selected_platforms"] == ["willhaben"]
    assert observed["preferences"]["provider_country_filter_applied"] is True
    assert observed["preferences"]["provider_country_filter_removed"] == ["realestate_au"]
    assert observed["preferences"]["provider_country_filter_removed_details"] == [
        {
            "platform": "realestate_au",
            "provider_label": "realestate.com.au",
            "reason": "wrong_country",
            "requested_country_code": "AT",
            "requested_country_label": "Austria",
            "provider_country_code": "AU",
            "provider_country_label": "Australia",
            "requested_listing_mode": "rent",
            "supported_listing_modes": ["rent", "buy"],
            "search_ready": True,
            "market_readiness": "private_beta",
        }
    ]


def test_property_search_run_uses_saved_platforms_before_family_toggles(monkeypatch) -> None:
    principal_id = "exec-property-search-saved-platforms"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Saved Platforms")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben", "immmo", "immoscout_at", "remax_at", "kalandra", "broker_direct_at"],
            "include_broker_direct_sources": True,
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert stored.status_code == 200, stored.text

    observed: dict[str, object] = {}

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["selected_platforms"] = tuple(selected_platforms)
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 1,
            "review_created_total": 1,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post("/app/api/signals/property/search/run", json={"selected_platforms": []})
    assert started.status_code == 202, started.text
    _poll_property_search_run_status(client, started.json()["run_id"])

    assert set(observed.get("selected_platforms") or ()) >= {
        "willhaben",
        "immmo",
        "immoscout_at",
        "derstandard_at",
        "remax_at",
        "kalandra",
        "broker_direct_at",
    }


def test_property_search_run_updates_active_search_agent_lifecycle(monkeypatch) -> None:
    principal_id = "exec-property-search-agent-lifecycle"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Agent Lifecycle")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
            "alert_frequency": "daily",
            "search_agent_enabled": True,
            "search_agent_notification_limit": 3,
            "search_agent_notification_period": "day",
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert stored.status_code == 200, stored.text
    monkeypatch.setattr(
        product_service,
        "generated_property_source_specs",
        lambda *, preferences, selected_platforms, principal_id, default_person_id, max_results: (
            {
                "url": "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien",
                "label": "Willhaben Vienna",
                "platform": "willhaben",
                "principal_id": principal_id,
                "preference_person_id": default_person_id,
                "notify_telegram": False,
                "max_results": 1,
            },
        ),
    )
    monkeypatch.setattr(product_service, "_property_scout_listing_urls_for_source", lambda **kwargs: ((), {"status": "miss"}))
    service = product_service.build_product_service(client.app.state.container)

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="scheduler",
        selected_platforms=("willhaben",),
        max_results_per_source=1,
        force_refresh=True,
    )

    lifecycle = dict(result.get("search_agent_lifecycle") or {})
    assert lifecycle["notification_period"] == "day"
    assert lifecycle["notification_limit"] == 3
    assert lifecycle["last_run_at"]
    assert lifecycle["next_run_at"]
    state = client.app.state.container.onboarding.status(principal_id=principal_id)
    agents = list(dict(state.get("property_search_preferences") or {}).get("search_agents") or [])
    assert agents[0]["last_run_at"] == lifecycle["last_run_at"]
    assert agents[0]["next_run_at"] == lifecycle["next_run_at"]
    assert agents[0]["sent_in_current_window"] == 0


def test_direct_property_scout_emits_timing_receipts_even_when_sources_are_empty(monkeypatch) -> None:
    principal_id = "exec-property-scout-timing"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Scout Timing")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
            "alert_frequency": "daily",
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert stored.status_code == 200, stored.text
    monkeypatch.setattr(
        product_service,
        "_merged_property_scout_source_specs",
        lambda **kwargs: [
            {
                "url": "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien",
                "label": "Willhaben Vienna",
                "platform": "willhaben",
                "provider_family": "core_portal",
                "principal_id": principal_id,
                "preference_person_id": "self",
                "notify_telegram": False,
                "max_results": 1,
            }
        ],
    )
    monkeypatch.setattr(
        product_service,
        "_property_scout_listing_urls_for_source",
        lambda **kwargs: ((), {"status": "miss"}),
    )
    service = product_service.build_product_service(client.app.state.container)

    result = service.sync_direct_property_scout(
        principal_id=principal_id,
        actor="scheduler",
        selected_platforms=("willhaben",),
        max_results_per_source=1,
        force_refresh=True,
    )

    assert result["status"] == "processed"
    assert float(dict(result.get("timing_ms") or {}).get("run_total") or 0.0) >= 0.0
    assert float(dict(result.get("timing_ms") or {}).get("provider_fetch_total") or 0.0) >= 0.0
    assert len(result["sources"]) == 1
    assert float(dict(result["sources"][0].get("timing_ms") or {}).get("provider_fetch") or 0.0) >= 0.0


def test_property_search_run_explicit_empty_keywords_clear_saved_keywords(monkeypatch) -> None:
    principal_id = "exec-property-search-clear-keywords"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Clear Keywords")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "keywords": "supermarket nearby, underground nearby, no gas",
            "custom_keywords": "quiet, bright",
            "selected_platforms": ["willhaben"],
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert stored.status_code == 200, stored.text

    observed: dict[str, object] = {}

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["property_search_preferences"] = dict(property_search_preferences or {})
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 1,
            "review_created_total": 1,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post(
        "/app/api/signals/property/search/run",
        json={"property_preferences": {"keywords": "", "custom_keywords": ""}},
    )
    assert started.status_code == 202, started.text
    _poll_property_search_run_status(client, started.json()["run_id"])

    assert observed["property_search_preferences"]["keywords"] == ""
    assert observed["property_search_preferences"]["custom_keywords"] == ""


def test_property_search_preferences_update_preserves_existing_commercial_state(monkeypatch) -> None:
    principal_id = "pq-commercial-preserve"
    client = build_property_client(principal_id=principal_id)
    started = client.post("/v1/onboarding/start", json={"workspace_name": "Commercial Preserve", "workspace_mode": "personal"})
    assert started.status_code == 200

    seeded = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "buy",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "property_commercial": {
                "status": "active",
                "active_plan_key": "agent",
                "active_until": "2099-12-31T23:59:59+00:00",
            },
        },
    )
    assert seeded.status_code == 200

    updated = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "buy",
            "location_query": "Wien",
            "selected_platforms": ["willhaben", "genossenschaften_at"],
            "investment_research_mode": "auto",
            "use_stored_feedback_preferences": False,
        },
    )
    assert updated.status_code == 200
    commercial = updated.json()["property_search_preferences"]["property_commercial"]
    assert commercial["active_plan_key"] == "agent"
    assert commercial["status"] == "active"

    observed: dict[str, object] = {}

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["selected_platforms"] = tuple(selected_platforms)
        observed["preference_person_id"] = str((property_search_preferences or {}).get("preference_person_id") or "").strip()
        observed["use_stored_feedback_preferences"] = bool((property_search_preferences or {}).get("use_stored_feedback_preferences"))
        observed["max_results_per_source"] = max_results_per_source
        observed["force_refresh"] = bool(force_refresh)
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 1,
            "review_created_total": 1,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post(
        "/app/api/signals/property/search/run",
        json={"property_preferences": {"preference_person_id": "override"}},
    )
    assert started.status_code == 202
    assert set(observed.get("selected_platforms") or ()) == {"willhaben", "genossenschaften_at"}
    assert observed.get("preference_person_id") == "override"
    assert observed.get("use_stored_feedback_preferences") is False
    assert observed.get("max_results_per_source") is None


def test_property_search_run_does_not_reapply_stale_saved_agent_area_filter(monkeypatch) -> None:
    principal_id = "exec-property-search-stale-agent-merge"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Stale Agent Merge")
    seed_product_state(client, principal_id=principal_id)

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "CR",
            "listing_mode": "buy",
            "property_type": "house",
            "location_query": "Monteverde",
            "min_area_m2": 80,
            "require_floorplan": True,
            "min_match_score": 40,
            "selected_platforms": ["re_cr_mls", "realtor_com_cr"],
            "search_agents": [
                {
                    "agent_id": "agent-monteverde-buy",
                    "name": "Monteverde buy",
                    "country_code": "CR",
                    "listing_mode": "buy",
                    "property_type": "house",
                    "location_query": "Monteverde",
                    "min_area_m2": 80,
                    "require_floorplan": True,
                    "min_match_score": 40,
                    "selected_platforms": ["re_cr_mls", "realtor_com_cr"],
                }
            ],
            "active_search_agent_id": "agent-monteverde-buy",
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )
    assert stored.status_code == 200, stored.text

    observed: dict[str, object] = {}

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["selected_platforms"] = tuple(selected_platforms)
        observed["property_search_preferences"] = dict(property_search_preferences or {})
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post(
        "/app/api/signals/property/search/run",
        json={
            "selected_platforms": ["re_cr_mls", "realtor_com_cr"],
            "property_preferences": {
                "country_code": "CR",
                "listing_mode": "buy",
                "property_type": "house",
                "location_query": "Monteverde",
                "min_area_m2": 0,
                "require_floorplan": False,
                "min_match_score": 25,
                "search_agents": stored.json()["property_search_preferences"]["search_agents"],
                "active_search_agent_id": "agent-monteverde-buy",
                "raw_preferences": {"min_area_m2": 80},
            },
        },
    )
    assert started.status_code == 202, started.text
    _poll_property_search_run_status(client, started.json()["run_id"])

    preferences = dict(observed["property_search_preferences"])
    assert "re_cr_mls" in tuple(observed["selected_platforms"] or ())
    assert preferences.get("min_area_m2") not in {80, "80"}
    assert preferences["require_floorplan"] is False
    assert preferences["min_match_score"] == 0.0


def test_property_search_execution_preferences_relax_only_floorplan_for_discovery_mode() -> None:
    request_preferences, execution_policy = product_service._property_search_execution_preferences(
        {
            "search_mode": "discovery",
            "max_price_eur": 500000,
            "min_area_m2": 80,
            "require_floorplan": True,
            "floorplan_requirement_mode": "hard",
        }
    )

    assert request_preferences["search_mode"] == "discovery"
    assert request_preferences["require_floorplan"] is True
    assert request_preferences["floorplan_requirement_mode"] == "soft"
    assert request_preferences["max_price_eur"] == 500000
    assert request_preferences["min_area_m2"] == 80
    assert execution_policy["search_mode"] == "discovery"
    assert execution_policy["require_floorplan"] is True
    assert execution_policy["enforce_floorplan_filter"] is False
    assert execution_policy["discovery_relaxed_filters"] == ["require_floorplan", "min_area_m2"]


def test_property_search_effective_min_match_score_always_disables_the_removed_bar() -> None:
    assert product_service._property_search_effective_min_match_score({}) == 0.0
    assert product_service._property_search_effective_min_match_score({"min_match_score": 0}) == 0.0
    assert product_service._property_search_effective_min_match_score({"search_mode": "discovery", "min_match_score": 20}) == 0.0


def test_property_search_run_status_marks_old_snapshot_after_budget_change(monkeypatch) -> None:
    principal_id = "exec-property-stale-budget-run"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Stale Budget Office")
    service = ProductService(client.app.state.container)
    client.app.state.container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "selected_platforms": ["willhaben"],
            "max_price_eur": 26000,
        },
    )

    run_id = "run-stale-budget-regression"
    now = datetime.now(timezone.utc).isoformat()
    run_record = {
        "run_id": run_id,
        "principal_id": principal_id,
        "created_at": now,
        "updated_at": now,
        "status": "processed",
        "status_url": f"/app/api/signals/property/search/run/{run_id}",
        "selected_platforms": ["willhaben"],
        "progress": 100,
        "current_step": "completed",
        "message": "Property scouting run completed.",
        "stages_total": 1,
        "steps_completed": 1,
        "summary": {
            "status": "processed",
            "sources_total": 1,
            "provider_total": 1,
            "listing_total": 0,
            "ranked_total": 0,
            "filtered_total": 22,
            "held_back_total": 22,
            "sources": [],
        },
        "events": [],
        "property_search_preferences": {
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "selected_platforms": ["willhaben"],
            "max_price_eur": 1200,
        },
        "generated_at": now,
    }
    monkeypatch.setattr(product_service, "_load_property_search_run_record", lambda **kwargs: None)
    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", lambda **kwargs: None)
    previous_registry = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY)
    try:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(run_record)

        snapshot = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.update(previous_registry)

    assert snapshot is not None
    summary = dict(snapshot["summary"])
    assert snapshot["brief_preferences_stale"] is True
    assert snapshot["stale_run_snapshot"] is True
    assert summary["brief_snapshot_status"] == "old_run"
    assert summary["brief_stale_reason"] == "saved_brief_changed"
    assert "max_price_eur" in summary["brief_stale_changed_keys"]
    assert summary["previous_filtered_total"] == 22
    assert summary["filtered_total"] == 0
    assert summary["held_back_total"] == 0
    assert summary["ranked_total"] == 0
    assert summary["can_refresh_with_current_brief"] is True
    assert snapshot["message"].startswith("This search used an earlier brief.")


def test_property_search_run_status_reopens_budget_filtered_saved_candidate_after_budget_increase(monkeypatch) -> None:
    principal_id = "exec-property-budget-revalidated-run"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Budget Revalidation Office")
    service = ProductService(client.app.state.container)
    client.app.state.container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "selected_platforms": ["willhaben"],
            "max_price_eur": 1700,
        },
    )

    run_id = "run-budget-revalidated-regression"
    now = datetime.now(timezone.utc).isoformat()
    budget_filtered_candidate = {
        "title": "Helle Wohnung mit Balkon",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/budget-reopen/",
        "fit_score": 64,
        "status": "filtered",
        "filter_status": "hard_filtered",
        "hard_filter_reason": "price_above_budget",
        "property_facts": {
            "postal_name": "1020 Wien",
            "area_sqm": 72,
            "rooms": 2,
            "total_rent_eur": 1500,
        },
    }
    run_record = {
        "run_id": run_id,
        "principal_id": principal_id,
        "created_at": now,
        "updated_at": now,
        "status": "processed",
        "status_url": f"/app/api/signals/property/search/run/{run_id}",
        "selected_platforms": ["willhaben"],
        "progress": 100,
        "current_step": "completed",
        "message": "Property scouting run completed.",
        "summary": {
            "status": "processed",
            "sources_total": 1,
            "provider_total": 1,
            "listing_total": 1,
            "ranked_total": 0,
            "filtered_total": 1,
            "held_back_total": 1,
            "sources": [
                {
                    "source_label": "Willhaben",
                    "status": "completed",
                    "listing_total": 1,
                    "research_candidates": [dict(budget_filtered_candidate)],
                    "filtered_area_total": 0,
                }
            ],
        },
        "events": [],
        "property_search_preferences": {
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "property_type": "apartment",
            "selected_platforms": ["willhaben"],
            "max_price_eur": 1200,
        },
        "generated_at": now,
    }
    monkeypatch.setattr(product_service, "_load_property_search_run_record", lambda **kwargs: None)
    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", lambda **kwargs: None)
    previous_registry = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY)
    try:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(run_record)

        snapshot = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.update(previous_registry)

    assert snapshot is not None
    summary = dict(snapshot["summary"])
    ranked = [dict(row) for row in list(summary.get("ranked_candidates") or [])]

    assert snapshot["brief_preferences_revalidated"] is True
    assert not snapshot["brief_preferences_stale"]
    assert summary["brief_snapshot_status"] == "revalidated_saved_candidates"
    assert summary["brief_revalidated_reason"] == "budget_expanded"
    assert summary["previous_filtered_total"] == 1
    assert summary["filtered_total"] == 0
    assert summary["held_back_total"] == 0
    assert summary["ranked_total"] == 1
    assert len(ranked) == 1
    assert ranked[0]["property_url"] == budget_filtered_candidate["property_url"]
    assert ranked[0]["budget_revalidated"] is True
    assert ranked[0]["revalidated_from_old_brief"] is True
    assert ranked[0].get("hard_filter_reason") in (None, "")
    assert "current budget" in snapshot["message"]


def test_property_search_run_status_reopens_area_filtered_saved_candidate_after_district_expansion(monkeypatch) -> None:
    principal_id = "exec-property-area-revalidated-run"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Area Revalidation Office")
    service = ProductService(client.app.state.container)
    client.app.state.container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna, 1020 Vienna",
            "selected_locations": ["1010 Vienna", "1020 Vienna"],
            "property_type": "apartment",
            "selected_platforms": ["willhaben"],
        },
    )

    run_id = "run-area-revalidated-regression"
    now = datetime.now(timezone.utc).isoformat()
    area_filtered_candidate = {
        "title": "Helle Wohnung nahe Prater",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/area-reopen/",
        "fit_score": 64,
        "status": "filtered",
        "filter_status": "hard_filtered",
        "hard_filter_reason": "outside_selected_area",
        "property_facts": {
            "postal_name": "1020 Wien",
            "area_sqm": 72,
            "rooms": 2,
            "total_rent_eur": 1500,
        },
    }
    run_record = {
        "run_id": run_id,
        "principal_id": principal_id,
        "created_at": now,
        "updated_at": now,
        "status": "processed",
        "status_url": f"/app/api/signals/property/search/run/{run_id}",
        "selected_platforms": ["willhaben"],
        "progress": 100,
        "current_step": "completed",
        "message": "Property scouting run completed.",
        "summary": {
            "status": "processed",
            "sources_total": 1,
            "provider_total": 1,
            "listing_total": 1,
            "ranked_total": 0,
            "filtered_total": 1,
            "held_back_total": 1,
            "sources": [
                {
                    "source_label": "Willhaben",
                    "status": "completed",
                    "listing_total": 1,
                    "research_candidates": [dict(area_filtered_candidate)],
                }
            ],
        },
        "events": [],
        "property_search_preferences": {
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_locations": ["1010 Vienna"],
            "property_type": "apartment",
            "selected_platforms": ["willhaben"],
        },
        "generated_at": now,
    }
    monkeypatch.setattr(product_service, "_load_property_search_run_record", lambda **kwargs: None)
    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", lambda **kwargs: None)
    previous_registry = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY)
    try:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(run_record)

        snapshot = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.update(previous_registry)

    assert snapshot is not None
    summary = dict(snapshot["summary"])
    ranked = [dict(row) for row in list(summary.get("ranked_candidates") or [])]

    assert snapshot["brief_preferences_revalidated"] is True
    assert not snapshot["brief_preferences_stale"]
    assert summary["brief_snapshot_status"] == "revalidated_saved_candidates"
    assert summary["brief_revalidated_reason"] == "area_expanded"
    assert summary["previous_filtered_total"] == 1
    assert summary["filtered_total"] == 0
    assert summary["held_back_total"] == 0
    assert summary["ranked_total"] == 1
    assert ranked[0]["property_url"] == area_filtered_candidate["property_url"]
    assert ranked[0]["area_revalidated"] is True
    assert ranked[0]["revalidated_from_old_brief"] is True
    assert ranked[0].get("hard_filter_reason") in (None, "")
    assert "current area" in snapshot["message"]


def test_property_search_run_status_keeps_source_scope_only_area_candidate_as_old_snapshot(monkeypatch) -> None:
    principal_id = "exec-property-area-source-scope-stays-old"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Area Source Scope Office")
    service = ProductService(client.app.state.container)
    client.app.state.container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna, 1020 Vienna",
            "selected_locations": ["1010 Vienna", "1020 Vienna"],
            "property_type": "apartment",
            "selected_platforms": ["willhaben"],
        },
    )

    run_id = "run-area-source-scope-old-regression"
    now = datetime.now(timezone.utc).isoformat()
    source_scope_candidate = {
        "title": "Provider result page",
        "property_url": "https://www.willhaben.at/iad/immobilien/mietwohnungen/mietwohnung-angebote?areaId=1020",
        "fit_score": 64,
        "status": "filtered",
        "filter_status": "hard_filtered",
        "hard_filter_reason": "outside_selected_area",
        "property_facts": {
            "source_scope_location": "1020 Vienna",
            "source_postal_code": "1020",
            "source_city": "Vienna",
        },
    }
    run_record = {
        "run_id": run_id,
        "principal_id": principal_id,
        "created_at": now,
        "updated_at": now,
        "status": "processed",
        "status_url": f"/app/api/signals/property/search/run/{run_id}",
        "selected_platforms": ["willhaben"],
        "progress": 100,
        "current_step": "completed",
        "message": "Property scouting run completed.",
        "summary": {
            "status": "processed",
            "sources_total": 1,
            "provider_total": 1,
            "listing_total": 1,
            "ranked_total": 0,
            "filtered_total": 1,
            "held_back_total": 1,
            "sources": [
                {
                    "source_label": "Willhaben | Austria | Rent | 1020 Vienna",
                    "status": "completed",
                    "listing_total": 1,
                    "research_candidates": [dict(source_scope_candidate)],
                }
            ],
        },
        "events": [],
        "property_search_preferences": {
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_locations": ["1010 Vienna"],
            "property_type": "apartment",
            "selected_platforms": ["willhaben"],
        },
        "generated_at": now,
    }
    monkeypatch.setattr(product_service, "_load_property_search_run_record", lambda **kwargs: None)
    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", lambda **kwargs: None)
    previous_registry = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY)
    try:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(run_record)

        snapshot = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.update(previous_registry)

    assert snapshot is not None
    summary = dict(snapshot["summary"])
    assert snapshot["brief_preferences_stale"] is True
    assert snapshot["stale_run_snapshot"] is True
    assert summary["brief_snapshot_status"] == "old_run"
    assert summary["previous_filtered_total"] == 1
    assert summary["filtered_total"] == 0
    assert summary["ranked_total"] == 0
    assert "selected_locations" in summary["brief_stale_changed_keys"]


def test_property_search_run_status_reopens_area_filtered_candidate_after_radius_expansion(monkeypatch) -> None:
    principal_id = "exec-property-radius-revalidated-run"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Radius Revalidation Office")
    service = ProductService(client.app.state.container)
    client.app.state.container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_locations": ["1010 Vienna"],
            "adjacent_area_radius_m": 900,
            "property_type": "apartment",
            "selected_platforms": ["willhaben"],
        },
    )
    monkeypatch.setattr(product_service, "_property_search_area_boundary_geojsons", lambda **kwargs: ())
    monkeypatch.setattr(product_service, "_property_search_area_reference_points", lambda **kwargs: ((48.2082, 16.3738),))
    monkeypatch.setattr(product_service, "_property_research_distance_m", lambda *args, **kwargs: 700.0)

    run_id = "run-radius-revalidated-regression"
    now = datetime.now(timezone.utc).isoformat()
    radius_filtered_candidate = {
        "title": "Wohnung knapp außerhalb vom ersten Bezirk",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/radius-reopen/",
        "fit_score": 61,
        "status": "filtered",
        "filter_status": "hard_filtered",
        "hard_filter_reason": "outside_selected_area",
        "property_facts": {
            "postal_name": "1020 Wien",
            "map_lat": 48.2144,
            "map_lng": 16.3740,
            "area_sqm": 65,
            "total_rent_eur": 1400,
        },
    }
    run_record = {
        "run_id": run_id,
        "principal_id": principal_id,
        "created_at": now,
        "updated_at": now,
        "status": "processed",
        "status_url": f"/app/api/signals/property/search/run/{run_id}",
        "selected_platforms": ["willhaben"],
        "progress": 100,
        "current_step": "completed",
        "message": "Property scouting run completed.",
        "summary": {
            "status": "processed",
            "sources_total": 1,
            "provider_total": 1,
            "listing_total": 1,
            "ranked_total": 0,
            "filtered_total": 1,
            "held_back_total": 1,
            "sources": [
                {
                    "source_label": "Willhaben",
                    "status": "completed",
                    "listing_total": 1,
                    "research_candidates": [dict(radius_filtered_candidate)],
                }
            ],
        },
        "events": [],
        "property_search_preferences": {
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_locations": ["1010 Vienna"],
            "adjacent_area_radius_m": 500,
            "property_type": "apartment",
            "selected_platforms": ["willhaben"],
        },
        "generated_at": now,
    }
    monkeypatch.setattr(product_service, "_load_property_search_run_record", lambda **kwargs: None)
    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", lambda **kwargs: None)
    previous_registry = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY)
    try:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(run_record)

        snapshot = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.update(previous_registry)

    assert snapshot is not None
    summary = dict(snapshot["summary"])
    ranked = [dict(row) for row in list(summary.get("ranked_candidates") or [])]

    assert snapshot["brief_preferences_revalidated"] is True
    assert summary["brief_revalidated_reason"] == "area_expanded"
    assert summary["ranked_total"] == 1
    assert summary["filtered_total"] == 0
    assert ranked[0]["property_url"] == radius_filtered_candidate["property_url"]
    assert ranked[0]["area_revalidated"] is True


def _property_search_revalidation_status_for_candidate(
    monkeypatch,
    *,
    principal_id: str,
    workspace_name: str,
    run_id: str,
    current_preferences: dict[str, object],
    run_preferences: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object] | None:
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name=workspace_name)
    service = ProductService(client.app.state.container)
    client.app.state.container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json=dict(current_preferences),
    )

    now = datetime.now(timezone.utc).isoformat()
    run_record = {
        "run_id": run_id,
        "principal_id": principal_id,
        "created_at": now,
        "updated_at": now,
        "status": "processed",
        "status_url": f"/app/api/signals/property/search/run/{run_id}",
        "selected_platforms": list(run_preferences.get("selected_platforms") or ["willhaben"]),
        "progress": 100,
        "current_step": "completed",
        "message": "Property scouting run completed.",
        "summary": {
            "status": "processed",
            "sources_total": 1,
            "provider_total": 1,
            "listing_total": 1,
            "ranked_total": 0,
            "filtered_total": 1,
            "held_back_total": 1,
            "sources": [
                {
                    "source_label": "Willhaben",
                    "status": "completed",
                    "listing_total": 1,
                    "research_candidates": [dict(candidate)],
                }
            ],
        },
        "events": [],
        "property_search_preferences": dict(run_preferences),
        "generated_at": now,
    }
    monkeypatch.setattr(product_service, "_load_property_search_run_record", lambda **kwargs: None)
    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", lambda **kwargs: None)
    previous_registry = dict(product_service._PROPERTY_SEARCH_RUN_REGISTRY)
    try:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(run_record)

        return service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.clear()
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.update(previous_registry)


def test_property_search_run_status_reopens_min_area_filtered_candidate_after_size_relaxation(monkeypatch) -> None:
    current_preferences = {
        "country_code": "AT",
        "region_code": "vienna",
        "listing_mode": "rent",
        "location_query": "1020 Vienna",
        "selected_locations": ["1020 Vienna"],
        "property_type": "apartment",
        "selected_platforms": ["willhaben"],
        "min_area_m2": 60,
    }
    candidate = {
        "title": "Kompakte Wohnung im zweiten Bezirk",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/area-hard-reopen/",
        "fit_score": 71,
        "status": "filtered",
        "filter_status": "hard_filtered",
        "hard_filter_reason": "min_area_m2",
        "property_facts": {
            "postal_name": "1020 Wien",
            "property_type": "apartment",
            "area_sqm": 65,
            "total_rent_eur": 1300,
            "country_code": "AT",
        },
    }

    snapshot = _property_search_revalidation_status_for_candidate(
        monkeypatch,
        principal_id="exec-property-min-area-relaxed-run",
        workspace_name="Min Area Revalidation Office",
        run_id="run-min-area-revalidated-regression",
        current_preferences=current_preferences,
        run_preferences={**current_preferences, "min_area_m2": 80},
        candidate=candidate,
    )

    assert snapshot is not None
    summary = dict(snapshot["summary"])
    ranked = [dict(row) for row in list(summary.get("ranked_candidates") or [])]
    assert snapshot["brief_preferences_revalidated"] is True
    assert summary["brief_revalidated_reason"] == "hard_rules_relaxed"
    assert summary["previous_filtered_total"] == 1
    assert summary["filtered_total"] == 0
    assert summary["ranked_total"] == 1
    assert ranked[0]["property_url"] == candidate["property_url"]
    assert ranked[0]["hard_rules_revalidated"] is True
    assert ranked[0]["hard_rules_revalidated_filters"] == ["min_area_m2"]
    assert "current rules" in snapshot["message"]


def test_property_search_run_status_reopens_floorplan_filtered_candidate_after_requirement_removed(monkeypatch) -> None:
    current_preferences = {
        "country_code": "AT",
        "region_code": "vienna",
        "listing_mode": "rent",
        "location_query": "1020 Vienna",
        "selected_locations": ["1020 Vienna"],
        "property_type": "apartment",
        "selected_platforms": ["willhaben"],
        "require_floorplan": False,
    }
    candidate = {
        "title": "Altbauwohnung ohne Grundriss",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/floorplan-hard-reopen/",
        "fit_score": 68,
        "status": "filtered",
        "filter_status": "hard_filtered",
        "hard_filter_reason": "require_floorplan",
        "property_facts": {
            "postal_name": "1020 Wien",
            "property_type": "apartment",
            "area_sqm": 72,
            "total_rent_eur": 1450,
            "country_code": "AT",
        },
    }

    snapshot = _property_search_revalidation_status_for_candidate(
        monkeypatch,
        principal_id="exec-property-floorplan-relaxed-run",
        workspace_name="Floorplan Revalidation Office",
        run_id="run-floorplan-revalidated-regression",
        current_preferences=current_preferences,
        run_preferences={**current_preferences, "require_floorplan": True, "floorplan_requirement_mode": "hard"},
        candidate=candidate,
    )

    assert snapshot is not None
    summary = dict(snapshot["summary"])
    ranked = [dict(row) for row in list(summary.get("ranked_candidates") or [])]
    assert snapshot["brief_preferences_revalidated"] is True
    assert summary["brief_revalidated_reason"] == "hard_rules_relaxed"
    assert summary["ranked_total"] == 1
    assert ranked[0]["hard_rules_revalidated_filters"] == ["require_floorplan"]
    assert ranked[0].get("hard_filter_reason") in (None, "")


def test_property_search_run_status_reopens_property_type_filtered_candidate_after_type_widening(monkeypatch) -> None:
    current_preferences = {
        "country_code": "AT",
        "region_code": "vienna",
        "listing_mode": "rent",
        "location_query": "1020 Vienna",
        "selected_locations": ["1020 Vienna"],
        "property_type": ["apartment", "house"],
        "selected_platforms": ["willhaben"],
    }
    candidate = {
        "title": "Haus mit Garten in Leopoldstadt",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/haus-mieten/wien/type-hard-reopen/",
        "fit_score": 73,
        "status": "filtered",
        "filter_status": "hard_filtered",
        "hard_filter_reason": "property_type_mismatch",
        "property_facts": {
            "postal_name": "1020 Wien",
            "property_type": "house",
            "area_sqm": 95,
            "total_rent_eur": 2200,
            "country_code": "AT",
        },
    }

    snapshot = _property_search_revalidation_status_for_candidate(
        monkeypatch,
        principal_id="exec-property-type-relaxed-run",
        workspace_name="Property Type Revalidation Office",
        run_id="run-property-type-revalidated-regression",
        current_preferences=current_preferences,
        run_preferences={**current_preferences, "property_type": "apartment"},
        candidate=candidate,
    )

    assert snapshot is not None
    summary = dict(snapshot["summary"])
    ranked = [dict(row) for row in list(summary.get("ranked_candidates") or [])]
    assert snapshot["brief_preferences_revalidated"] is True
    assert summary["brief_revalidated_reason"] == "hard_rules_relaxed"
    assert summary["ranked_total"] == 1
    assert ranked[0]["hard_rules_revalidated_filters"] == ["property_type"]


def test_property_search_run_status_keeps_wrong_country_candidate_old_after_size_relaxation(monkeypatch) -> None:
    current_preferences = {
        "country_code": "AT",
        "region_code": "vienna",
        "listing_mode": "rent",
        "location_query": "1020 Vienna",
        "selected_locations": ["1020 Vienna"],
        "property_type": "apartment",
        "selected_platforms": ["willhaben"],
        "min_area_m2": 60,
    }
    candidate = {
        "title": "Kompakte Wohnung im zweiten Bezirk",
        "property_url": "https://example.pl/listing/wrong-country-area-relaxed",
        "fit_score": 71,
        "status": "filtered",
        "filter_status": "hard_filtered",
        "hard_filter_reason": "min_area_m2",
        "property_facts": {
            "postal_name": "1020 Wien",
            "property_type": "apartment",
            "area_sqm": 65,
            "total_rent_eur": 1300,
            "country_code": "PL",
        },
    }

    snapshot = _property_search_revalidation_status_for_candidate(
        monkeypatch,
        principal_id="exec-property-min-area-wrong-country-old-run",
        workspace_name="Wrong Country Revalidation Office",
        run_id="run-min-area-wrong-country-old-regression",
        current_preferences=current_preferences,
        run_preferences={**current_preferences, "min_area_m2": 80},
        candidate=candidate,
    )

    assert snapshot is not None
    summary = dict(snapshot["summary"])
    assert snapshot["brief_preferences_stale"] is True
    assert snapshot["stale_run_snapshot"] is True
    assert summary["brief_snapshot_status"] == "old_run"
    assert summary["previous_filtered_total"] == 1
    assert summary["filtered_total"] == 0
    assert summary["ranked_total"] == 0


def test_property_search_run_status_keeps_country_scope_change_old_even_with_relaxed_size(monkeypatch) -> None:
    current_preferences = {
        "country_code": "AT",
        "region_code": "vienna",
        "listing_mode": "rent",
        "location_query": "1020 Vienna",
        "selected_locations": ["1020 Vienna"],
        "property_type": "apartment",
        "selected_platforms": ["willhaben"],
        "min_area_m2": 60,
    }
    candidate = {
        "title": "Costa Rica apartment",
        "property_url": "https://example.cr/listing/country-scope-stale",
        "fit_score": 72,
        "status": "filtered",
        "filter_status": "hard_filtered",
        "hard_filter_reason": "min_area_m2",
        "property_facts": {
            "postal_name": "Monteverde",
            "property_type": "apartment",
            "area_sqm": 65,
            "monthly_rent_eur": 1200,
            "country_code": "CR",
        },
    }

    snapshot = _property_search_revalidation_status_for_candidate(
        monkeypatch,
        principal_id="exec-property-country-scope-old-run",
        workspace_name="Country Scope Old Run Office",
        run_id="run-country-scope-old-regression",
        current_preferences=current_preferences,
        run_preferences={
            **current_preferences,
            "country_code": "CR",
            "region_code": "puntarenas",
            "location_query": "Monteverde",
            "selected_locations": ["Monteverde"],
            "selected_platforms": ["encuentra24_cr"],
            "min_area_m2": 80,
        },
        candidate=candidate,
    )

    assert snapshot is not None
    summary = dict(snapshot["summary"])
    assert snapshot["brief_preferences_stale"] is True
    assert snapshot["stale_run_snapshot"] is True
    assert summary["brief_snapshot_status"] == "old_run"
    assert "country_code" in summary["brief_stale_changed_keys"]
    assert summary["previous_filtered_total"] == 1
    assert summary["filtered_total"] == 0
    assert summary["ranked_total"] == 0


def test_property_search_run_status_keeps_provider_scope_change_old_even_with_relaxed_budget(monkeypatch) -> None:
    current_preferences = {
        "country_code": "AT",
        "region_code": "vienna",
        "listing_mode": "rent",
        "location_query": "1020 Vienna",
        "selected_locations": ["1020 Vienna"],
        "property_type": "apartment",
        "selected_platforms": ["willhaben"],
        "max_price_eur": 1800,
    }
    candidate = {
        "title": "Leopoldstadt apartment from old provider scope",
        "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/provider-scope-stale/",
        "fit_score": 72,
        "status": "filtered",
        "filter_status": "hard_filtered",
        "hard_filter_reason": "price_above_budget",
        "property_facts": {
            "postal_name": "1020 Wien",
            "property_type": "apartment",
            "area_sqm": 65,
            "total_rent_eur": 1500,
            "country_code": "AT",
        },
    }

    snapshot = _property_search_revalidation_status_for_candidate(
        monkeypatch,
        principal_id="exec-property-provider-scope-old-run",
        workspace_name="Provider Scope Old Run Office",
        run_id="run-provider-scope-old-regression",
        current_preferences=current_preferences,
        run_preferences={
            **current_preferences,
            "selected_platforms": ["remax_at"],
            "max_price_eur": 1200,
        },
        candidate=candidate,
    )

    assert snapshot is not None
    summary = dict(snapshot["summary"])
    assert snapshot["brief_preferences_stale"] is True
    assert snapshot["stale_run_snapshot"] is True
    assert summary["brief_snapshot_status"] == "old_run"
    assert "selected_platforms" in summary["brief_stale_changed_keys"]
    assert summary["previous_filtered_total"] == 1
    assert summary["filtered_total"] == 0
    assert summary["ranked_total"] == 0


def test_property_search_run_rejects_saved_out_of_scope_country_preferences(monkeypatch) -> None:
    principal_id = "exec-property-search-country-defaults"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Country Defaults")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "UK",
            "language_code": "en",
            "listing_mode": "rent",
            "location_query": "London",
            "selected_platforms": [],
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert stored.status_code == 200, stored.text

    started = client.post("/app/api/signals/property/search/run", json={"property_preferences": {}})
    assert started.status_code == 400, started.text
    assert started.json()["error"]["code"] == "unsupported_property_market"


def test_property_search_run_drops_saved_providers_from_wrong_country(monkeypatch) -> None:
    principal_id = "exec-property-search-country-provider-guard"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Country Provider Guard")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "1020 Vienna",
            "selected_platforms": ["re_cr_mls", "encuentra24_cr"],
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert stored.status_code == 200, stored.text

    observed: dict[str, object] = {}

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["selected_platforms"] = tuple(selected_platforms)
        observed["property_search_preferences"] = dict(property_search_preferences or {})
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post("/app/api/signals/property/search/run", json={"property_preferences": {}})
    assert started.status_code == 202, started.text
    assert "re_cr_mls" not in observed["selected_platforms"]
    assert "encuentra24_cr" not in observed["selected_platforms"]
    assert set(observed["selected_platforms"]) >= {"willhaben", "immmo", "immoscout_at"}
    preferences = observed["property_search_preferences"]
    assert preferences["provider_selection_filter_applied"] is True
    assert set(preferences["provider_selection_filter_removed"]) == {"re_cr_mls", "encuentra24_cr"}


def test_property_search_run_drops_saved_unready_and_mode_mismatched_providers(monkeypatch) -> None:
    principal_id = "exec-property-search-provider-readiness-guard"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Provider Readiness Guard")

    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "DE",
            "language_code": "de",
            "listing_mode": "buy",
            "location_query": "Berlin",
            "selected_platforms": ["core_portals_de", "corporate_landlords_de", "community_signals_at"],
            "property_commercial": {"active_plan_key": "agent", "status": "active", "active_until": "2999-01-01T00:00:00+00:00"},
        },
    )
    assert stored.status_code == 200, stored.text

    observed: dict[str, object] = {}

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["selected_platforms"] = tuple(selected_platforms)
        observed["property_search_preferences"] = dict(property_search_preferences or {})
        return {
            "generated_at": product_service._now_iso(),
            "status": "processed",
            "sources_total": 1,
            "listing_total": 0,
            "review_created_total": 0,
            "review_existing_total": 0,
            "notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 0,
            "high_fit_total": 0,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)

    started = client.post("/app/api/signals/property/search/run", json={"property_preferences": {}})
    assert started.status_code == 202, started.text
    assert observed["selected_platforms"] == ("core_portals_de",)
    preferences = observed["property_search_preferences"]
    assert preferences["provider_selection_filter_applied"] is True
    assert set(preferences["provider_selection_filter_removed"]) == {"corporate_landlords_de", "community_signals_at"}
    removed_details = {row["platform"]: row for row in preferences["provider_selection_filter_removed_details"]}
    assert removed_details["corporate_landlords_de"]["reason"] == "listing_mode_unsupported"
    assert removed_details["corporate_landlords_de"]["requested_listing_mode"] == "buy"
    assert removed_details["community_signals_at"]["reason"] == "wrong_country"


def test_property_search_run_status_does_not_send_results_ready_email(monkeypatch) -> None:
    principal_id = "exec-property-search-status-read-only"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"run-status-read-only-{uuid.uuid4().hex}"
    state = {
        "run_id": run_id,
        "principal_id": principal_id,
        "created_at": product_service._now_iso(),
        "updated_at": product_service._now_iso(),
        "status": "processed",
        "summary": {
            "sources_total": 1,
            "listing_total": 1,
            "eligible_tour_total": 1,
            "pending_tour_total": 0,
            "ready_tour_total": 1,
            "blocked_tour_total": 0,
            "sources": [],
        },
        "events": [],
        "selected_platforms": ["willhaben"],
    }
    product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)
    product_service._store_property_search_run_record(state)

    def _unexpected_notify(self, *, principal_id: str, run_id: str, result: dict[str, object]) -> None:
        raise AssertionError("status reads must not send result email")

    monkeypatch.setattr(ProductService, "_notify_property_search_results_ready", _unexpected_notify)

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id)

    assert status is not None
    assert dict(status.get("summary") or {})["listing_total"] == 1


def test_reconcile_property_search_results_delivery_completes_unsent_ready_run(monkeypatch) -> None:
    client = build_property_client(principal_id="exec-property-search-reconcile")
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"run-reconcile-ready-{uuid.uuid4().hex}"
    state = {
        "run_id": run_id,
        "principal_id": "exec-property-search-reconcile",
        "created_at": product_service._now_iso(),
        "updated_at": product_service._now_iso(),
        "status": "processed",
        "summary": {
            "sources_total": 1,
            "listing_total": 1,
            "eligible_tour_total": 1,
            "pending_tour_total": 0,
            "ready_tour_total": 1,
            "blocked_tour_total": 0,
            "top_candidates": [
                {
                    "title": "Ready candidate",
                    "source_ref": "source-1",
                    "listing_id": "listing-1",
                    "tour_status": "ready",
                    "tour_url": "https://propertyquarry.com/tours/ready-candidate",
                }
            ],
        },
        "events": [],
        "selected_platforms": ["willhaben"],
    }
    product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)
    product_service._store_property_search_run_record(state)
    observed: dict[str, object] = {}

    def _fake_notify(self, *, principal_id: str, run_id: str, result: dict[str, object]) -> None:
        observed["principal_id"] = principal_id
        observed["run_id"] = run_id
        observed["result"] = dict(result)
        self._record_product_event(
            principal_id=principal_id,
            event_type="property_search_results_ready_email_sent",
            payload={"run_id": run_id},
            source_id=run_id,
            dedupe_key=f"{principal_id}|{run_id}|property-search-results-ready-email",
        )

    monkeypatch.setattr(ProductService, "_notify_property_search_results_ready", _fake_notify)

    summary = service.reconcile_property_search_results_delivery(
        principal_id="exec-property-search-reconcile",
        limit=10,
    )

    assert summary["attempted"] >= 1
    assert summary["finalized"] >= 1
    assert summary["emailed"] >= 1
    assert observed["principal_id"] == "exec-property-search-reconcile"
    assert observed["run_id"] == run_id


def test_reconcile_property_search_results_delivery_includes_partial_runs(monkeypatch) -> None:
    client = build_property_client(principal_id="exec-property-search-reconcile-partial")
    service = product_service.build_product_service(client.app.state.container)
    captured: dict[str, object] = {}

    def _fake_list_records(**kwargs):
        captured.update(kwargs)
        return ()

    monkeypatch.setattr(product_service, "_list_property_search_run_records", _fake_list_records)

    summary = service.reconcile_property_search_results_delivery(
        principal_id="exec-property-search-reconcile-partial",
        limit=10,
    )

    assert "completed_partial" in tuple(captured["statuses"])
    assert captured["lightweight"] is True
    assert summary == {
        "attempted": 0,
        "finalized": 0,
        "emailed": 0,
        "pending": 0,
    }


def test_scheduler_reconcile_persists_compact_delivery_refresh_without_full_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "principal-compact-finalization"
    run_id = "run-compact-finalization"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    compact_record = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "processed",
        "updated_at": "2026-07-15T10:00:00+00:00",
        "compact_schema_version": property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
        "delivery_pending": True,
        "summary": {
            "eligible_tour_total": 1,
            "pending_tour_total": 1,
            "ready_tour_total": 0,
            "blocked_tour_total": 0,
            "required_fact_resolution_pending": False,
            "required_fact_resolution_exhausted": False,
            "evaluating_candidate_total": 0,
            "_delivery_candidates": [
                {
                    "candidate_ref": "candidate-compact-finalization",
                    "source_ref": "source-compact-finalization",
                    "property_url": "https://example.test/compact-finalization",
                    "tour_status": "pending",
                }
            ],
        },
    }
    stored: list[dict[str, object]] = []
    monkeypatch.setattr(product_service, "_list_property_search_run_records", lambda **_kwargs: (compact_record,))
    monkeypatch.setattr(product_service, "_property_search_run_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full payload must not be loaded")),
    )
    monkeypatch.setattr(
        ProductService,
        "_property_search_tour_events_by_source",
        lambda *_args, **_kwargs: {
            "source-compact-finalization": [
                {
                    "event_type": "property_tour_ready",
                    "payload": {
                        "property_url": "https://example.test/compact-finalization",
                        "tour_url": "https://propertyquarry.com/tours/compact-finalization",
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_verified_open_url",
        lambda value, *, principal_id="": str(value or ""),
    )
    monkeypatch.setattr(
        ProductService,
        "_maybe_advance_property_search_run_finalization",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full finalization path must not run")),
    )
    monkeypatch.setattr(
        ProductService,
        "_recent_product_event_exists",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("notification lookup must not run")),
    )

    def _capture_compact_refresh(state: dict[str, object]) -> bool:
        stored.append(property_search_storage._compact_property_search_run_record(state))
        return True

    monkeypatch.setattr(product_service, "_store_property_search_run_compact_record", _capture_compact_refresh)

    summary = service.reconcile_property_search_results_delivery(
        principal_id=principal_id,
        limit=1,
        allow_notifications=False,
    )

    assert summary == {"attempted": 1, "finalized": 1, "emailed": 0, "pending": 0}
    assert len(stored) == 1
    assert stored[0]["updated_at"] == "2026-07-15T10:00:00+00:00"
    assert stored[0]["delivery_pending"] is False
    stored_summary = dict(stored[0]["summary"])
    assert stored_summary["pending_tour_total"] == 0
    assert stored_summary["ready_tour_total"] == 1, stored_summary
    assert stored_summary["_delivery_candidates"][0]["tour_status"] == "ready"


def test_scheduler_reconcile_skips_ready_runs_and_reuses_principal_tour_events(monkeypatch) -> None:
    client = build_property_client(principal_id="exec-property-search-reconcile-bounded")
    service = product_service.build_product_service(client.app.state.container)
    principal_id = "cf-email:person@example.test"
    pending_summary = {
        "eligible_tour_total": 1,
        "pending_tour_total": 1,
        "ready_tour_total": 0,
        "blocked_tour_total": 0,
        "required_fact_resolution_pending": False,
        "required_fact_resolution_exhausted": False,
        "evaluating_candidate_total": 0,
        "_delivery_candidates": [{"source_ref": "source-1", "tour_status": "pending"}],
    }
    ready_summary = {
        "eligible_tour_total": 1,
        "pending_tour_total": 0,
        "ready_tour_total": 1,
        "blocked_tour_total": 0,
        "required_fact_resolution_pending": False,
        "required_fact_resolution_exhausted": False,
        "evaluating_candidate_total": 0,
        "sources": [{"source_ref": "source-ready", "top_candidates": []}],
    }
    records = tuple(
        {
            "run_id": f"run-ready-{index}",
            "principal_id": principal_id,
            "status": "processed",
            "compact_schema_version": property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
            "summary": ready_summary,
        }
        for index in range(18)
    ) + tuple(
        {
            "run_id": f"run-pending-{index}",
            "principal_id": principal_id,
            "status": "processed",
            "compact_schema_version": property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
            "summary": pending_summary,
        }
        for index in range(22)
    )
    monkeypatch.setattr(product_service, "_list_property_search_run_records", lambda **_kwargs: records)
    event_loads: list[str] = []
    event_indexes: list[dict[str, list[dict[str, object]]]] = []

    def _fake_tour_events(self, *, principal_id: str):
        event_loads.append(principal_id)
        return {"source-1": []}

    def _fake_advance(
        self,
        *,
        principal_id: str,
        run_id: str,
        state: dict[str, object],
        allow_notifications: bool = True,
        tour_events_by_source: dict[str, list[dict[str, object]]] | None = None,
    ):
        assert allow_notifications is False
        event_indexes.append(tour_events_by_source or {})
        return dict(state)

    monkeypatch.setattr(ProductService, "_property_search_tour_events_by_source", _fake_tour_events)
    monkeypatch.setattr(ProductService, "_maybe_advance_property_search_run_finalization", _fake_advance)
    monkeypatch.setattr(
        ProductService,
        "_recent_product_event_exists",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("notification lookup must be skipped")),
    )

    summary = service.reconcile_property_search_results_delivery(limit=40, allow_notifications=False)

    assert summary == {"attempted": 22, "finalized": 0, "emailed": 0, "pending": 22}
    assert event_loads == [principal_id]
    assert len(event_indexes) == 22
    assert all(event_index is event_indexes[0] for event_index in event_indexes)


def test_scheduler_reconcile_backfills_only_bounded_legacy_compact_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = build_property_client(principal_id="principal-legacy-finalization")
    service = product_service.build_product_service(client.app.state.container)
    principal_id = "principal-legacy-finalization"
    records = tuple(
        {
            "run_id": f"run-legacy-{index}",
            "principal_id": principal_id,
            "status": "processed",
            "summary": {},
        }
        for index in range(3)
    )
    loaded: list[str] = []
    compact_updates: list[str] = []
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_RESULTS_LEGACY_HYDRATION_LIMIT", "2")
    monkeypatch.setattr(product_service, "_list_property_search_run_records", lambda **_kwargs: records)
    monkeypatch.setattr(product_service, "_property_search_run_database_url", lambda: "postgresql://test")

    def _fake_load(*, run_id: str, principal_id: str):
        loaded.append(run_id)
        return {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "processed",
            "summary": {
                "eligible_tour_total": 0,
                "pending_tour_total": 0,
                "ready_tour_total": 0,
                "blocked_tour_total": 0,
            },
        }

    monkeypatch.setattr(product_service, "_load_property_search_run_record", _fake_load)
    monkeypatch.setattr(
        product_service,
        "_store_property_search_run_compact_record",
        lambda state: (compact_updates.append(str(state.get("run_id") or "")) or True),
    )

    summary = service.reconcile_property_search_results_delivery(
        principal_id=principal_id,
        limit=3,
        allow_notifications=False,
    )

    assert summary == {"attempted": 0, "finalized": 0, "emailed": 0, "pending": 0}
    assert loaded == ["run-legacy-0", "run-legacy-1"]
    assert compact_updates == loaded


def test_scheduler_reconcile_separates_legacy_and_truncated_hydration_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "principal-separated-hydration"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    records = (
        {
            "run_id": "run-truncated-0",
            "principal_id": principal_id,
            "status": "processed",
            "updated_at": "2026-07-15T10:00:00+00:00",
            "compact_schema_version": property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
            "summary": {
                "_delivery_projection_truncated": True,
                "required_fact_resolution_pending": False,
                "required_fact_resolution_exhausted": False,
                "evaluating_candidate_total": 0,
            },
        },
        {
            "run_id": "run-truncated-1",
            "principal_id": principal_id,
            "status": "processed",
            "updated_at": "2026-07-15T10:01:00+00:00",
            "compact_schema_version": property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
            "summary": {
                "_delivery_projection_truncated": True,
                "required_fact_resolution_pending": False,
                "required_fact_resolution_exhausted": False,
                "evaluating_candidate_total": 0,
            },
        },
        {
            "run_id": "run-legacy-0",
            "principal_id": principal_id,
            "status": "processed",
            "updated_at": "2026-07-15T10:02:00+00:00",
            "summary": {},
        },
    )
    loaded: list[str] = []
    stored: list[dict[str, object]] = []
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_RESULTS_LEGACY_HYDRATION_LIMIT", "1")
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_RESULTS_TRUNCATED_HYDRATION_LIMIT", "1")
    monkeypatch.setattr(product_service, "_list_property_search_run_records", lambda **_kwargs: records)
    monkeypatch.setattr(product_service, "_property_search_run_database_url", lambda: "postgresql://test")

    def _fake_load(*, run_id: str, principal_id: str):
        loaded.append(run_id)
        return {
            "run_id": run_id,
            "principal_id": principal_id,
            "status": "processed",
            "summary": {
                "eligible_tour_total": 0,
                "pending_tour_total": 0,
                "ready_tour_total": 0,
                "blocked_tour_total": 0,
            },
        }

    monkeypatch.setattr(product_service, "_load_property_search_run_record", _fake_load)
    monkeypatch.setattr(
        product_service,
        "_store_property_search_run_compact_record",
        lambda state: (stored.append(dict(state)) or True),
    )

    summary = service.reconcile_property_search_results_delivery(
        principal_id=principal_id,
        limit=3,
        allow_notifications=False,
    )

    assert summary == {"attempted": 0, "finalized": 0, "emailed": 0, "pending": 0}
    assert loaded == ["run-truncated-0", "run-legacy-0"]
    assert [row["run_id"] for row in stored] == loaded
    assert [row["updated_at"] for row in stored] == [
        "2026-07-15T10:00:00+00:00",
        "2026-07-15T10:02:00+00:00",
    ]


def test_scheduler_reconcile_refreshes_truncated_state_before_failed_compact_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "principal-truncated-cas"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    lightweight = {
        "run_id": "run-truncated-cas",
        "principal_id": principal_id,
        "status": "processed",
        "updated_at": "2026-07-15T10:00:00+00:00",
        "compact_schema_version": property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
        "delivery_pending": True,
        "summary": {
            "_delivery_projection_truncated": True,
            "required_fact_resolution_pending": False,
            "required_fact_resolution_exhausted": False,
            "evaluating_candidate_total": 0,
        },
    }
    stored: list[dict[str, object]] = []
    event_loads: list[str] = []
    monkeypatch.setattr(product_service, "_list_property_search_run_records", lambda **_kwargs: (lightweight,))
    monkeypatch.setattr(product_service, "_property_search_run_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda **_kwargs: {
            "run_id": "run-truncated-cas",
            "principal_id": principal_id,
            "status": "processed",
            "summary": {
                "eligible_tour_total": 1,
                "pending_tour_total": 1,
                "ready_tour_total": 0,
                "blocked_tour_total": 0,
                "_delivery_candidates": [
                    {
                        "candidate_ref": "candidate-truncated-cas",
                        "source_ref": "source-truncated-cas",
                        "property_url": "https://example.test/truncated-cas",
                        "tour_status": "pending",
                    }
                ],
            },
        },
    )

    def _events(self, *, principal_id: str):
        event_loads.append(principal_id)
        return {
            "source-truncated-cas": [
                {
                    "event_type": "property_tour_ready",
                    "payload": {
                        "property_url": "https://example.test/truncated-cas",
                        "tour_url": "https://propertyquarry.com/tours/truncated-cas",
                    },
                }
            ]
        }

    monkeypatch.setattr(ProductService, "_property_search_tour_events_by_source", _events)
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_verified_open_url",
        lambda value, *, principal_id="": str(value or ""),
    )
    monkeypatch.setattr(
        product_service,
        "_store_property_search_run_compact_record",
        lambda state: (stored.append(dict(state)) or False),
    )

    summary = service.reconcile_property_search_results_delivery(
        principal_id=principal_id,
        limit=1,
        allow_notifications=False,
    )

    assert summary == {"attempted": 1, "finalized": 0, "emailed": 0, "pending": 1}
    assert event_loads == [principal_id]
    assert len(stored) == 1
    assert stored[0]["updated_at"] == lightweight["updated_at"]
    assert stored[0]["summary"]["ready_tour_total"] == 1
    assert stored[0]["summary"]["pending_tour_total"] == 0


def test_scheduler_reconcile_touches_fairness_cursor_when_projection_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "principal-delivery-cursor"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    state = {
        "run_id": "run-delivery-cursor",
        "principal_id": principal_id,
        "status": "processed",
        "updated_at": "2026-07-15T10:00:00+00:00",
        "delivery_checked_at": "2026-07-15T10:05:00+00:00",
        "compact_schema_version": property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
        "delivery_pending": True,
        "summary": {
            "eligible_tour_total": 1,
            "pending_tour_total": 1,
            "ready_tour_total": 0,
            "blocked_tour_total": 0,
            "required_fact_resolution_pending": False,
            "required_fact_resolution_exhausted": False,
            "evaluating_candidate_total": 0,
            "_delivery_candidates": [{"source_ref": "source-delivery-cursor", "tour_status": "pending"}],
        },
    }
    touched: list[dict[str, object]] = []
    monkeypatch.setattr(product_service, "_list_property_search_run_records", lambda **_kwargs: (state,))
    monkeypatch.setattr(product_service, "_property_search_run_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(ProductService, "_property_search_tour_events_by_source", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        ProductService,
        "_refresh_property_search_results_delivery_state",
        lambda _self, *, principal_id, result, tour_events_by_source=None: dict(result),
    )
    monkeypatch.setattr(
        product_service,
        "_store_property_search_run_compact_record",
        lambda _state: pytest.fail("unchanged projection must use the scalar cursor touch"),
    )
    monkeypatch.setattr(
        product_service,
        "_mark_property_search_run_delivery_checked",
        lambda record: (touched.append(dict(record)) or True),
    )

    summary = service.reconcile_property_search_results_delivery(
        principal_id=principal_id,
        limit=1,
        allow_notifications=False,
    )

    assert summary == {"attempted": 1, "finalized": 0, "emailed": 0, "pending": 1}
    assert len(touched) == 1
    assert touched[0]["delivery_checked_at"] == "2026-07-15T10:05:00+00:00"


def test_property_search_results_ready_can_send_heyy_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-search-heyy"
    client = build_product_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Heyy Office", selected_channels=["whatsapp"])
    onboarding = client.app.state.container.onboarding
    state = onboarding._ensure_state(principal_id)  # noqa: SLF001
    onboarding._replace_channel_pref(  # noqa: SLF001
        state,
        "whatsapp",
        {"mode": "business", "phone_number": "+43 660 0000000"},
        status="in_progress",
    )
    monkeypatch.setenv("PROPERTYQUARRY_HEYY_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_HEYY_TEMPLATE_SEARCH_AGENT_DIGEST", "tmpl-search-digest")
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        "app.product.service.HeyyWhatsAppBridgeService.send_template",
        lambda self, **kwargs: observed.update(kwargs) or {
            "status": "sent",
            "provider": "heyy",
            "channel_id": kwargs.get("channel_id") or "",
            "message_id": "msg-search-digest-1",
            "delivery_status": "queued",
        },
    )

    service = product_service.build_product_service(client.app.state.container)
    result = service._notify_property_search_results_ready_heyy(
        principal_id=principal_id,
        run_id="run-heyy-1",
        result={
            "listing_total": 12,
            "high_fit_total": 4,
            "notification_budget_suppressed_total": 8,
            "ranked_candidates": [{"fit_score": 91.0}],
            "search_agent_lifecycle": {"agent_name": "Vienna rent watch"},
        },
    )
    assert result["status"] == "sent"
    assert observed["phone_number"] == "+436600000000"
    assert observed["template_id"] == "tmpl-search-digest"
    assert any(item.get("name") == "agent_name" and item.get("value") == "Vienna rent watch" for item in list(observed.get("variables") or []))
    assert any(item.get("name") == "top_fit_score" and item.get("value") == "91" for item in list(observed.get("variables") or []))
    packet_service = build_fliplink_packet_service(client.app.state.container)
    events = packet_service.list_events(principal_id=principal_id, event_type="heyy_whatsapp_template_sent", limit=10)
    payload = next(dict(row.get("payload_json") or {}) for row in events if dict(row.get("payload_json") or {}).get("template_kind") == "search_agent_digest")
    assert payload["phone_last4"] == "0000"
    assert payload["phone_e164_hash"] == redact_phone_number("+43 660 0000000")["phone_e164_hash"]
    assert "phone_number" not in payload


def test_property_search_results_ready_heyy_digest_honors_stop_command(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-search-heyy-stopped"
    client = build_product_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Search Heyy Stopped Office", selected_channels=["whatsapp"])
    onboarding = client.app.state.container.onboarding
    state = onboarding._ensure_state(principal_id)  # noqa: SLF001
    onboarding._replace_channel_pref(  # noqa: SLF001
        state,
        "whatsapp",
        {"mode": "business", "phone_number": "+43 660 0000000"},
        status="in_progress",
    )
    monkeypatch.setenv("PROPERTYQUARRY_HEYY_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_HEYY_TEMPLATE_SEARCH_AGENT_DIGEST", "tmpl-search-digest")
    packet_service = build_fliplink_packet_service(client.app.state.container)
    packet_service._repo.record_event(  # noqa: SLF001
        {
            "publication_id": "",
            "principal_id": principal_id,
            "event_type": "heyy_whatsapp_message_received",
            "actor": "heyy",
            "payload_json": {
                "opt_command": "STOP",
                **redact_phone_number("+43 660 0000000"),
            },
        }
    )
    monkeypatch.setattr(
        "app.product.service.HeyyWhatsAppBridgeService.send_template",
        lambda self, **kwargs: pytest.fail("service-level Heyy digest ignored STOP"),
    )

    service = product_service.build_product_service(client.app.state.container)
    result = service._notify_property_search_results_ready_heyy(
        principal_id=principal_id,
        run_id="run-heyy-stopped",
        result={
            "listing_total": 12,
            "high_fit_total": 4,
            "notification_budget_suppressed_total": 8,
            "ranked_candidates": [{"fit_score": 91.0}],
            "search_agent_lifecycle": {"agent_name": "Vienna rent watch"},
        },
    )

    assert result == {"status": "suppressed", "reason": "heyy_whatsapp_stopped"}


def test_property_scout_queued_alert_routes_to_selected_whatsapp_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-scout-whatsapp-preference"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Scout WhatsApp Preference")
    headers = {"host": "propertyquarry.com"}
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_districts": ["1010 Vienna"],
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    updated = client.post(
        "/app/api/property/account/notifications",
        data={"preferred_channel": "whatsapp", "whatsapp_ai_support_phone": "+43 660 0000000"},
        headers=headers,
        follow_redirects=False,
    )
    assert updated.status_code == 303, updated.text
    monkeypatch.setenv("PROPERTYQUARRY_HEYY_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_HEYY_TEMPLATE_PROPERTY_MATCH", "tmpl-property-match")
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: pytest.fail("WhatsApp-selected scout alert must not call Telegram"),
    )
    monkeypatch.setattr(
        product_service,
        "send_property_match_email",
        lambda **kwargs: pytest.fail("WhatsApp-selected scout alert must not call email"),
    )
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        "app.product.service.HeyyWhatsAppBridgeService.send_template",
        lambda self, **kwargs: observed.update(kwargs)
        or {
            "status": "sent",
            "provider": "heyy",
            "channel_id": kwargs.get("channel_id") or "",
            "message_id": "msg-whatsapp-alert-1",
            "delivery_status": "queued",
        },
    )
    service = product_service.build_product_service(client.app.state.container)

    result = service._send_property_scout_queued_notification(
        kind="hit",
        kwargs={
            "principal_id": principal_id,
            "actor": "test",
            "title": "Wohnung mieten in 1010 Wien | 85 m² | 3 Zimmer | EUR 1.900",
            "summary": "3-Zimmer Wohnung im 1. Bezirk, 85 m2, Gesamtmiete EUR 1.900.",
            "counterparty": "Willhaben | Austria | Rent | 1010 Vienna",
            "account_email": "",
            "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/high-fit/",
            "source_ref": "property-scout:whatsapp-alert-1010",
            "assessment": {"fit_score": 91.0, "recommendation": "shortlist"},
            "fit_score": 91.0,
            "preference_person_id": "self",
            "review_url": "/app/research/high-fit",
            "tour_result": {"status": "skipped", "tour_url": ""},
            "candidate_properties": (
                {
                    "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/high-fit/",
                    "listing_title": "Wohnung mieten in 1010 Wien | 85 m² | 3 Zimmer | EUR 1.900",
                    "summary": "3-Zimmer Wohnung im 1. Bezirk, 85 m2, Gesamtmiete EUR 1.900.",
                    "property_facts": {
                        "postal_name": "1010 Wien",
                        "street_address": "Kärntner Straße 12, 1010 Wien",
                        "area_sqm": 85,
                        "rooms": 3,
                        "total_rent_eur": 1900,
                    },
                },
            ),
            "requested_location_hints": ("1010 Vienna",),
            "requested_country_code": "AT",
            "requested_region_code": "vienna",
        },
    )

    assert result["status"] == "sent"
    assert result["message_id"] == "msg-whatsapp-alert-1"
    assert observed["phone_number"] == "+436600000000"
    assert observed["template_id"] == "tmpl-property-match"
    assert any(item.get("name") == "fit_score" and item.get("value") == "91/100" for item in list(observed.get("variables") or []))
    packet_service = build_fliplink_packet_service(client.app.state.container)
    sent_events = packet_service.list_events(principal_id=principal_id, event_type="heyy_whatsapp_template_sent", limit=10)
    payload = dict(sent_events[0].get("payload_json") or {})
    assert payload["template_kind"] == "property_match"
    assert payload["phone_last4"] == "0000"
    assert "phone_number" not in payload


def test_property_scout_queued_whatsapp_suppresses_wrong_area_and_opens_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-scout-whatsapp-wrong-area"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Scout WhatsApp Wrong Area")
    stored = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_districts": ["1010 Vienna"],
            "selected_platforms": ["willhaben"],
            "property_search_enabled": True,
        },
    )
    assert stored.status_code == 200, stored.text
    updated = client.post(
        "/app/api/property/account/notifications",
        data={"preferred_channel": "whatsapp", "whatsapp_ai_support_phone": "+43 660 0000000"},
        headers={"host": "propertyquarry.com"},
        follow_redirects=False,
    )
    assert updated.status_code == 303, updated.text
    monkeypatch.setenv("PROPERTYQUARRY_HEYY_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_HEYY_TEMPLATE_PROPERTY_MATCH", "tmpl-property-match")
    monkeypatch.setattr(
        "app.product.service.HeyyWhatsAppBridgeService.send_template",
        lambda self, **kwargs: pytest.fail("wrong-area WhatsApp scout alert must not be sent"),
    )
    service = product_service.build_product_service(client.app.state.container)

    title = "#W2 Moderne Schöne Zwei-Zimmer Wohnung mit großem Ess- & Wohnbereich"
    summary = "Moderne Zwei-Zimmer Wohnung mit Terrasse in Salzburg."
    result = service._send_property_scout_queued_notification(
        kind="hit",
        kwargs={
            "principal_id": principal_id,
            "actor": "test",
            "title": title,
            "summary": summary,
            "counterparty": "Willhaben | Austria | Rent | 1010 Vienna",
            "account_email": "",
            "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/demo-1631373932/",
            "source_ref": "property-scout:whatsapp-salzburg-dirty-scope",
            "assessment": {"fit_score": 82.0, "recommendation": "review"},
            "fit_score": 82.0,
            "preference_person_id": "self",
            "review_url": "/app/research/wrong-area",
            "tour_result": {"status": "skipped", "tour_url": ""},
            "candidate_properties": (
                {
                    "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/demo-1631373932/",
                    "listing_title": title,
                    "summary": summary,
                    "source_platform": "willhaben",
                    "source_family": "core_portal",
                    "property_facts_json": {
                        "postal_name": "1010 Vienna",
                        "source_scope_location": "1010 Vienna",
                        "source_postal_code": "1010",
                        "source_city": "Vienna",
                        "price_display": "€ 1.190",
                    },
                },
            ),
            "requested_location_hints": (),
            "requested_country_code": "AT",
            "requested_region_code": "vienna",
        },
    )

    assert result["status"] == "suppressed"
    assert result["reason"] == "property_location_conflicts_with_active_search"
    repair_tasks = [
        task
        for task in client.app.state.container.orchestrator.list_human_tasks(
            principal_id=principal_id,
            status=None,
            limit=20,
        )
        if task.task_type == "property_provider_repair_ooda"
    ]
    assert repair_tasks
    assert repair_tasks[0].priority == "urgent"
    assert repair_tasks[0].assigned_operator_id == "ea_one_manager"
    assert dict(repair_tasks[0].input_json or {}).get("filter_key") == "location_scope"
    diagnostics = dict(repair_tasks[0].input_json or {}).get("diagnostics") or {}
    assert diagnostics["location_hints"] == ["1010 Vienna"]
    assert diagnostics["location_evidence_kind"] == "url_region"
    packet_service = build_fliplink_packet_service(client.app.state.container)
    sent_events = packet_service.list_events(principal_id=principal_id, event_type="heyy_whatsapp_template_sent", limit=10)
    assert sent_events == []


def test_property_scout_queued_near_miss_respects_nontelegram_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-scout-near-miss-whatsapp-preference"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Scout Near Miss Preference")
    updated = client.post(
        "/app/api/property/account/notifications",
        data={"preferred_channel": "whatsapp", "whatsapp_ai_support_phone": "+43 660 0000000"},
        headers={"host": "propertyquarry.com"},
        follow_redirects=False,
    )
    assert updated.status_code == 303, updated.text
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: pytest.fail("near-miss prompts must not leak to Telegram when WhatsApp is selected"),
    )
    service = product_service.build_product_service(client.app.state.container)

    result = service._send_property_scout_queued_notification(
        kind="near_miss",
        kwargs={
            "principal_id": principal_id,
            "actor": "test",
            "title": "Near miss",
            "summary": "Strong candidate held by a soft filter.",
            "counterparty": "Willhaben",
            "property_url": "https://example.test/property",
            "source_ref": "property-scout:near-miss",
            "preference_person_id": "self",
            "failed_filter_key": "max_distance_to_supermarket_m",
            "failed_filter_label": "supermarket distance",
            "prefilter_score": 84.0,
        },
    )

    assert result == {"status": "suppressed", "reason": "preferred_channel_whatsapp"}


def test_property_scout_queued_hit_delivers_to_all_selected_channels_with_explicit_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "cf-email:multichannel-primary@example.com"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Scout Multichannel Primary")
    updated = client.post(
        "/app/api/property/account/notifications",
        data={
            "notification_channels": ["email", "whatsapp"],
            "preferred_channel": "whatsapp",
            "whatsapp_ai_support_phone": "+43 660 0000000",
        },
        headers={"host": "propertyquarry.com"},
        follow_redirects=False,
    )
    assert updated.status_code == 303, updated.text
    monkeypatch.setenv("PROPERTYQUARRY_HEYY_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_HEYY_TEMPLATE_PROPERTY_MATCH", "tmpl-property-match")
    observed_email: dict[str, object] = {}
    observed_whatsapp: dict[str, object] = {}
    monkeypatch.setattr(product_service, "email_delivery_enabled", lambda: True)
    monkeypatch.setattr(
        product_service,
        "send_property_match_email",
        lambda **kwargs: observed_email.update(kwargs) or SimpleNamespace(provider="test-email", message_id="email-1"),
    )
    monkeypatch.setattr(
        "app.product.service.HeyyWhatsAppBridgeService.send_template",
        lambda self, **kwargs: observed_whatsapp.update(kwargs)
        or {
            "status": "sent",
            "provider": "heyy",
            "channel_id": kwargs.get("channel_id") or "",
            "message_id": "msg-whatsapp-multi-1",
            "delivery_status": "queued",
        },
    )
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: pytest.fail("telegram must not receive a hit when it is not selected"),
    )
    service = product_service.build_product_service(client.app.state.container)

    result = service._send_property_scout_queued_notification(
        kind="hit",
        kwargs={
            "principal_id": principal_id,
            "actor": "test",
            "title": "Wohnung mieten in 1010 Wien | 85 m² | 3 Zimmer | EUR 1.900",
            "summary": "3-Zimmer Wohnung im 1. Bezirk, 85 m2, Gesamtmiete EUR 1.900.",
            "counterparty": "Willhaben | Austria | Rent | 1010 Vienna",
            "account_email": "",
            "property_url": "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/high-fit/",
            "source_ref": "property-scout:multichannel-hit",
            "assessment": {"fit_score": 91.0, "recommendation": "shortlist"},
            "fit_score": 91.0,
            "preference_person_id": "self",
            "review_url": "/app/research/high-fit",
            "tour_result": {"status": "skipped", "tour_url": ""},
            "candidate_properties": (),
            "requested_location_hints": ("1010 Vienna",),
            "requested_country_code": "AT",
            "requested_region_code": "vienna",
        },
    )

    assert result["status"] == "sent"
    assert result["channel"] in {"whatsapp", "email"}
    assert set(result["delivery_results"]) == {"email", "whatsapp"}
    assert result["delivery_results"]["email"]["status"] == "sent"
    assert result["delivery_results"]["whatsapp"]["status"] == "sent"
    assert observed_whatsapp["phone_number"] == "+436600000000"


def test_property_scout_queued_near_miss_uses_explicit_primary_not_checkbox_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-scout-near-miss-explicit-primary"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property Scout Near Miss Explicit Primary")
    updated = client.post(
        "/app/api/property/account/notifications",
        data={
            "notification_channels": ["telegram", "whatsapp"],
            "preferred_channel": "whatsapp",
            "whatsapp_ai_support_phone": "+43 660 0000000",
        },
        headers={"host": "propertyquarry.com"},
        follow_redirects=False,
    )
    assert updated.status_code == 303, updated.text
    monkeypatch.setattr(
        product_service,
        "send_telegram_message_for_principal",
        lambda *args, **kwargs: pytest.fail("near-miss prompts must respect explicit primary, not checkbox order"),
    )
    service = product_service.build_product_service(client.app.state.container)

    result = service._send_property_scout_queued_notification(
        kind="near_miss",
        kwargs={
            "principal_id": principal_id,
            "actor": "test",
            "title": "Near miss",
            "summary": "Strong candidate held by a soft filter.",
            "counterparty": "Willhaben",
            "property_url": "https://example.test/property",
            "source_ref": "property-scout:near-miss-explicit-primary",
            "preference_person_id": "self",
            "failed_filter_key": "max_distance_to_supermarket_m",
            "failed_filter_label": "supermarket distance",
            "prefilter_score": 84.0,
        },
    )

    assert result == {"status": "suppressed", "reason": "preferred_channel_whatsapp"}


def test_property_search_run_postgres_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    db_url = str(os.environ.get("EA_TEST_PROPERTY_DATABASE_URL") or "").strip()
    if not db_url:
        pytest.skip("EA_TEST_PROPERTY_DATABASE_URL is not set")
    from app.product.property_search_schema import migrate_property_search_schema

    migrate_property_search_schema(db_url, applied_by="property-search-run-contract")
    monkeypatch.setenv("DATABASE_URL", db_url)
    run_id = f"run-postgres-round-trip-{uuid.uuid4().hex}"
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id="exec-property-postgres-round-trip",
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "Vienna"},
        force_refresh=False,
    )
    state["status"] = "processed"
    state["progress"] = 100

    product_service._store_property_search_run_record(state)
    loaded = product_service._load_property_search_run_record(
        run_id=run_id,
        principal_id="exec-property-postgres-round-trip",
    )
    listed = product_service._list_property_search_run_records(
        limit=5,
        statuses=("processed",),
        principal_id="exec-property-postgres-round-trip",
    )

    assert loaded is not None
    assert loaded["run_id"] == run_id
    assert loaded["principal_id"] == "exec-property-postgres-round-trip"
    assert loaded["property_search_preferences"]["country_code"] == "AT"
    assert any(row.get("run_id") == run_id for row in listed)


def test_property_search_run_postgres_retention_compacts_without_deleting_saved_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_url = str(os.environ.get("EA_TEST_PROPERTY_DATABASE_URL") or "").strip()
    if not db_url:
        pytest.skip("EA_TEST_PROPERTY_DATABASE_URL is not set")
    from app.product.property_search_schema import migrate_property_search_schema

    migrate_property_search_schema(db_url, applied_by="property-search-retention-contract")
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_RETENTION_SECONDS", "60")
    run_id = f"run-postgres-retention-{uuid.uuid4().hex}"
    principal_id = "exec-property-postgres-retention"
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna"},
        force_refresh=False,
    )
    state["status"] = "completed"
    state["created_at"] = old_timestamp
    state["updated_at"] = old_timestamp
    state["summary"] = {
        "status": "completed",
        "ranked_total": 1,
        "ranked_candidates": [{"candidate_ref": "saved-result", "title": "Saved result"}],
        "sources": [{"source_label": "Willhaben", "source_html": "<html>discard me</html>"}],
    }

    try:
        product_service._store_property_search_run_record(state)
        property_search_storage._prune_property_search_run_records()

        loaded = product_service._load_property_search_run_record(run_id=run_id, principal_id=principal_id)
        listed = product_service._list_property_search_run_records(
            limit=5,
            principal_id=principal_id,
            lightweight=True,
        )
    finally:
        product_service._delete_property_search_run_record(run_id=run_id, principal_id=principal_id)

    assert loaded is not None
    assert loaded["run_id"] == run_id
    assert loaded["principal_id"] == principal_id
    assert loaded["payload_retention_status"] == "compact_only"
    assert loaded["summary"]["ranked_candidates"] == [{"candidate_ref": "saved-result", "title": "Saved result"}]
    assert "sources" not in loaded["summary"]
    assert any(row.get("run_id") == run_id for row in listed)


def test_property_search_run_listing_requires_principal_unless_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    registry = {
        "run-1": {"run_id": "run-1", "principal_id": "principal-a", "status": "processed", "updated_at": "2026-06-18T00:00:00+00:00"},
        "run-2": {"run_id": "run-2", "principal_id": "principal-b", "status": "processed", "updated_at": "2026-06-18T00:01:00+00:00"},
    }

    assert property_search_storage._list_property_search_run_records(limit=10, registry=registry) == ()
    principal_rows = property_search_storage._list_property_search_run_records(
        limit=10,
        principal_id="principal-a",
        registry=registry,
    )
    admin_rows = property_search_storage._list_property_search_run_records(
        limit=10,
        admin=True,
        registry=registry,
    )

    assert [row["run_id"] for row in principal_rows] == ["run-1"]
    assert [row["run_id"] for row in admin_rows] == ["run-2", "run-1"]


def test_property_search_run_lightweight_listing_strips_source_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    registry = {
        "run-compact": {
            "run_id": "run-compact",
            "principal_id": "principal-a",
            "status": "completed_partial",
            "updated_at": "2026-06-18T00:00:00+00:00",
            "property_search_preferences": {
                "country_code": "AT",
                "location_query": "1010 Vienna",
                "raw_preferences": {"huge": "x" * 1000},
                "saved_shortlist_candidates": [{"candidate_ref": "saved"}],
                "search_agents": [{"id": "agent"}],
            },
            "summary": {
                "status": "completed_partial",
                "sources_total": 104,
                "listing_total": 304,
                "ranked_total": 2,
                "filtered_total": 270,
                "ranked_candidates": [{"candidate_ref": "ranked-1", "title": "Kept"}],
                "sources": [
                    {
                        "source_label": "Willhaben",
                        "source_html": "<html>" + ("x" * 1000) + "</html>",
                        "top_candidates": [{"candidate_ref": "source-cand"}],
                    }
                ],
                "events": [{"message": "large diagnostic"}],
            },
        }
    }

    rows = property_search_storage._list_property_search_run_records(
        limit=10,
        principal_id="principal-a",
        lightweight=True,
        registry=registry,
    )

    assert len(rows) == 1
    summary = rows[0]["summary"]
    assert rows[0]["run_id"] == "run-compact"
    assert rows[0]["property_search_preferences"]["location_query"] == "1010 Vienna"
    assert "raw_preferences" not in rows[0]["property_search_preferences"]
    assert "saved_shortlist_candidates" not in rows[0]["property_search_preferences"]
    assert "search_agents" not in rows[0]["property_search_preferences"]
    assert summary["sources_total"] == 104
    assert summary["ranked_candidates"] == [{"candidate_ref": "ranked-1", "title": "Kept"}]
    assert summary["sources"] == [
        {
            "listing_total": 1,
            "reviewed_listing_total": 1,
            "scanned_listing_total": 1,
            "source_label": "Willhaben",
        }
    ]
    assert "events" not in summary


def test_property_search_run_lightweight_status_keeps_slim_sources_for_worker_strip(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-run-lightweight-sources"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"lightweight-sources-{uuid.uuid4().hex}"

    compact_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "in_progress",
        "selected_platforms": ["willhaben", "immmo", "derstandard_at", "findmyhome_at"],
        "summary": {
            "provider_total": 4,
            "source_variant_total": 4,
            "sources_total": 4,
            "provider_workers": {"worker_concurrency": 4, "warm_limit": 3},
            "sources": [
                {
                    "source_label": "Willhaben",
                    "status": "warming",
                    "provider_cache": {"status": "warming", "cache_key": "willhaben:1010", "ignored": "drop"},
                    "provider_repair_tasks": [{"status": "opened", "filter_key": "source_fetch", "queue_item_ref": "human_task:1", "ignored": "drop"}],
                    "top_candidates": [{"title": "drop me"}],
                },
                {
                    "source_label": "immmo",
                    "status": "warming",
                },
                {
                    "source_label": "DER STANDARD Immobilien",
                    "status": "warming",
                },
                {
                    "source_label": "FindMyHome.at",
                    "status": "warming",
                },
            ],
        },
    }

    def _fake_load_compact_record(*, run_id: str, principal_id: str) -> dict[str, object] | None:
        if str(run_id or "").strip() == compact_run["run_id"] and str(principal_id or "").strip() == compact_run["principal_id"]:
            return property_search_storage._compact_property_search_run_record(compact_run)
        return None

    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", _fake_load_compact_record)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=True,
    )

    assert status is not None
    summary = dict(status.get("summary") or {})
    assert summary["provider_total"] == 4
    assert summary["provider_workers"] == {"worker_concurrency": 4, "warm_limit": 3}
    assert [row["source_label"] for row in summary["sources"]] == [
        "Willhaben",
        "immmo",
        "DER STANDARD Immobilien",
        "FindMyHome.at",
    ]
    assert summary["sources"][0]["status"] == "warming"
    assert summary["sources"][0]["provider_cache"] == {"status": "warming", "cache_key": "willhaben:1010"}
    assert summary["sources"][0]["provider_repair_tasks"] == [
        {"status": "opened", "filter_key": "source_fetch", "queue_item_ref": "human_task:1"}
    ]
    assert "top_candidates" not in summary["sources"][0]
    assert "source_html" not in summary["sources"][0]


def test_property_search_run_lightweight_status_backfills_live_sources_from_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-run-lightweight-registry"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"lightweight-registry-{uuid.uuid4().hex}"

    compact_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "in_progress",
        "current_step": "source_catalog_loading",
        "selected_platforms": ["willhaben", "immmo", "derstandard_at", "findmyhome_at"],
        "summary": {
            "provider_total": 4,
            "source_variant_total": 4,
            "sources_total": 4,
        },
    }

    live_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "in_progress",
        "current_step": "source_catalog_loading",
        "summary": {
            "current_plan_key": "agent",
            "current_plan_label": "Agent",
            "research_depth": "deep",
            "max_results_per_source": 0,
            "provider_total": 4,
            "source_variant_total": 4,
            "sources_total": 4,
            "provider_workers": {"worker_concurrency": 4},
            "sources": [
                {"source_label": "Willhaben", "status": "warming"},
                {"source_label": "immmo", "status": "warming"},
                {"source_label": "DER STANDARD Immobilien", "status": "warming"},
                {"source_label": "FindMyHome.at", "status": "warming"},
            ],
        },
    }

    def _fake_load_compact_record(*, run_id: str, principal_id: str) -> dict[str, object] | None:
        if str(run_id or "").strip() == compact_run["run_id"] and str(principal_id or "").strip() == compact_run["principal_id"]:
            return property_search_storage._compact_property_search_run_record(compact_run)
        return None

    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", _fake_load_compact_record)
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(live_run)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=True,
    )

    assert status is not None
    summary = dict(status.get("summary") or {})
    assert summary["current_plan_key"] == "agent"
    assert summary["current_plan_label"] == "Agent"
    assert summary["research_depth"] == "deep"
    assert summary["max_results_per_source"] == 0
    assert summary["provider_workers"] == {"worker_concurrency": 4}
    assert [row["source_label"] for row in summary["sources"]] == [
        "Willhaben",
        "immmo",
        "DER STANDARD Immobilien",
        "FindMyHome.at",
    ]


def test_property_search_run_lightweight_status_prefers_richer_live_registry_sources_over_partial_compact_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-run-lightweight-richer-live-registry"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"lightweight-richer-live-registry-{uuid.uuid4().hex}"

    compact_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "in_progress",
        "progress": 18,
        "current_step": "source_previewing",
        "selected_platforms": ["willhaben", "immmo", "derstandard_at", "findmyhome_at"],
        "summary": {
            "provider_total": 4,
            "source_variant_total": 4,
            "sources_total": 4,
            "reviewed_listing_total": 2,
            "provider_workers": {"worker_concurrency": 4},
            "sources": [
                {"source_label": "Willhaben", "status": "running"},
            ],
        },
    }

    live_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "in_progress",
        "progress": 31,
        "current_step": "source_previewing",
        "message": "29 providers · 102 homes reviewed · RE/MAX Austria · 1 / 1",
        "summary": {
            "current_plan_key": "agent",
            "current_plan_label": "Agent",
            "research_depth": "deep",
            "max_results_per_source": 0,
            "provider_total": 4,
            "source_variant_total": 4,
            "sources_total": 4,
            "reviewed_listing_total": 19,
            "provider_workers": {"worker_concurrency": 4},
            "sources": [
                {"source_label": "Willhaben", "status": "running"},
                {"source_label": "immmo", "status": "warming"},
                {"source_label": "DER STANDARD Immobilien", "status": "warming"},
                {"source_label": "FindMyHome.at", "status": "warming"},
            ],
        },
    }

    def _fake_load_compact_record(*, run_id: str, principal_id: str) -> dict[str, object] | None:
        if str(run_id or "").strip() == compact_run["run_id"] and str(principal_id or "").strip() == compact_run["principal_id"]:
            return property_search_storage._compact_property_search_run_record(compact_run)
        return None

    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", _fake_load_compact_record)
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(live_run)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=True,
    )

    assert status is not None
    assert int(status.get("progress") or 0) == 31
    summary = dict(status.get("summary") or {})
    assert int(summary.get("reviewed_listing_total") or 0) == 19
    assert [row["source_label"] for row in summary["sources"]] == [
        "Willhaben",
        "immmo",
        "DER STANDARD Immobilien",
        "FindMyHome.at",
    ]


def test_property_search_run_lightweight_status_prefers_live_ranked_preview_when_compact_snapshot_has_no_current_best(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-run-lightweight-live-current-best"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"lightweight-live-current-best-{uuid.uuid4().hex}"

    compact_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "in_progress",
        "progress": 31,
        "current_step": "source_previewing",
        "selected_platforms": ["willhaben"],
        "summary": {
            "provider_total": 1,
            "sources_total": 1,
            "reviewed_listing_total": 19,
            "sources": [
                {"source_label": "Willhaben", "status": "running"},
            ],
        },
    }
    live_candidate = {
        "candidate_ref": "cand-live-1",
        "title": "Live current best",
        "property_url": "https://example.test/live-current-best",
        "fit_score": 81,
    }
    live_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "in_progress",
        "progress": 31,
        "current_step": "source_previewing",
        "message": "1 provider · 19 homes reviewed · Willhaben · 1 / 1",
        "summary": {
            "provider_total": 1,
            "sources_total": 1,
            "reviewed_listing_total": 19,
            "ranked_candidates": [dict(live_candidate)],
            "sources": [
                {"source_label": "Willhaben", "status": "running"},
            ],
        },
    }

    def _fake_load_compact_record(*, run_id: str, principal_id: str) -> dict[str, object] | None:
        if str(run_id or "").strip() == compact_run["run_id"] and str(principal_id or "").strip() == compact_run["principal_id"]:
            return property_search_storage._compact_property_search_run_record(compact_run)
        return None

    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", _fake_load_compact_record)
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(live_run)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=True,
    )

    assert status is not None
    summary = dict(status.get("summary") or {})
    ranked = [dict(row) for row in list(summary.get("ranked_candidates") or []) if isinstance(row, dict)]
    assert len(ranked) == 1
    assert ranked[0]["candidate_ref"] == "cand-live-1"
    assert ranked[0]["title"] == "Live current best"


def test_property_search_run_lightweight_status_preserves_agent_unlimited_cap_from_compact_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "exec-property-run-lightweight-agent-cap"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"lightweight-agent-cap-{uuid.uuid4().hex}"

    compact_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "queued",
        "selected_platforms": ["willhaben", "immmo", "derstandard_at", "findmyhome_at"],
        "property_search_preferences": {
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna, 1020 Vienna",
        },
        "summary": {
            "status": "queued",
            "current_plan_key": "agent",
            "current_plan_label": "Agent",
            "research_depth": "deep",
            "max_results_per_source": 0,
            "provider_total": 4,
            "source_variant_total": 0,
            "sources_total": 0,
            "provider_workers": {"worker_concurrency": 4},
            "sources": [],
        },
    }

    def _fake_load_compact_record(*, run_id: str, principal_id: str) -> dict[str, object] | None:
        if str(run_id or "").strip() == compact_run["run_id"] and str(principal_id or "").strip() == compact_run["principal_id"]:
            return property_search_storage._compact_property_search_run_record(compact_run)
        return None

    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", _fake_load_compact_record)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=True,
    )

    assert status is not None
    summary = dict(status.get("summary") or {})
    assert summary["current_plan_key"] == "agent"
    assert summary["max_results_per_source"] == 0
    assert summary["provider_workers"] == {"worker_concurrency": 4}


def test_property_search_run_status_defaults_missing_result_cap_to_all_ranked(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-run-default-all-ranked-cap"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    run_id = f"default-all-ranked-cap-{uuid.uuid4().hex}"

    compact_run = {
        "run_id": run_id,
        "principal_id": principal_id,
        "status": "queued",
        "selected_platforms": ["willhaben"],
        "property_search_preferences": {
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna, 1020 Vienna",
        },
        "summary": {
            "status": "queued",
            "current_plan_key": "free",
            "current_plan_label": "Free",
            "research_depth": "standard",
            "provider_total": 1,
            "source_variant_total": 0,
            "sources_total": 0,
            "provider_workers": {"worker_concurrency": 1},
            "sources": [],
        },
    }

    def _fake_load_compact_record(*, run_id: str, principal_id: str) -> dict[str, object] | None:
        if str(run_id or "").strip() == compact_run["run_id"] and str(principal_id or "").strip() == compact_run["principal_id"]:
            return property_search_storage._compact_property_search_run_record(compact_run)
        return None

    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record", _fake_load_compact_record)

    status = service.get_property_search_run_status(
        principal_id=principal_id,
        run_id=run_id,
        lightweight=True,
    )

    assert status is not None
    summary = dict(status.get("summary") or {})
    assert summary["current_plan_key"] == "free"
    assert summary["max_results_per_source"] == 0


def test_property_search_run_upsert_does_not_change_existing_owner() -> None:
    source = Path(property_search_storage.__file__).read_text(encoding="utf-8")
    migration_source = Path("ea/app/product/property_search_schema.py").read_text(
        encoding="utf-8"
    )

    assert "PRIMARY KEY (principal_id, run_id)" in migration_source
    assert "property_search_runs_pkey PRIMARY KEY (principal_id, run_id)" in migration_source
    assert "SET principal_id = EXCLUDED.principal_id" not in source
    assert "ON CONFLICT (run_id)" not in source
    assert "ON CONFLICT (principal_id, run_id) DO UPDATE" in source
    assert "payload_retention_status" in source
    assert "compact_only" in source
    assert "UPDATE property_search_runs AS runs" in source
    assert "DELETE FROM property_search_runs WHERE updated_at < %s" not in source


def test_property_search_status_polling_retries_refresh_failures() -> None:
    source = Path("ea/app/templates/console_shell.html").read_text(encoding="utf-8")
    workbench_source = Path("ea/app/templates/app/_property_workbench_script.html").read_text(encoding="utf-8")

    assert "let failedRefreshCount = 0;" in source
    assert "Status: retrying quietly" in source
    assert "Retrying quietly" in source
    assert "Still checking the run." in source
    assert "Status refresh" not in source
    assert "Could not load property search status." not in source
    assert "Could not load property search status." not in workbench_source
    assert "Search status is still updating." in workbench_source
    assert "throw new Error(String(body.detail || 'Could not load property search status.'));" not in source


def test_property_search_emits_detail_check_heartbeats_before_slow_preview_paths() -> None:
    source = Path(product_service.__file__).read_text(encoding="utf-8")

    assert 'step="source_detail_check"' in source
    assert "Confirming selected-area match" in source
    assert "Confirming area facts" in source
    assert "Checking floorplan evidence" in source
    assert "Recovering listing details" in source


def test_property_search_status_api_preserves_backfilled_updated_at() -> None:
    payload = _property_search_payload_with_status_url(
        {
            "generated_at": "2026-06-25T15:00:00+00:00",
            "updated_at": None,
            "run_id": "status-api-timestamp-run",
            "provider_display_total": 35,
            "source_variant_display_total": 35,
            "selected_platform_count": 0,
            "summary": {"updated_at": "2026-06-25T15:09:37.641749+00:00"},
        },
        canonical=False,
    )
    response = PropertySearchRunStatusOut(**payload)

    assert payload["updated_at"] == "2026-06-25T15:09:37.641749+00:00"
    assert response.updated_at == "2026-06-25T15:09:37.641749+00:00"
    assert response.provider_display_total == 35
    assert response.source_variant_display_total == 35
    assert response.selected_platform_count == 0


def test_property_search_runtime_schema_boundary_is_check_only() -> None:
    source = Path(property_search_storage.__file__).read_text(encoding="utf-8").upper()

    assert "DEF _REQUIRE_PROPERTY_SEARCH_RUN_SCHEMA" in source
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source
    assert "CREATE INDEX" not in source


def test_property_search_run_upsert_skips_noop_conflict_updates() -> None:
    source = inspect.getsource(property_search_storage._store_property_search_run_record)  # type: ignore[attr-defined]

    assert "WHERE property_search_runs.payload_json IS DISTINCT FROM EXCLUDED.payload_json" in source
    assert "property_search_runs.status IS DISTINCT FROM EXCLUDED.status" in source
    assert "property_search_runs.compact_json IS DISTINCT FROM EXCLUDED.compact_json" in source
    assert "property_search_runs.updated_at IS DISTINCT FROM EXCLUDED.updated_at" not in source


def test_property_search_run_storage_defaults_limit_connection_pressure() -> None:
    source = Path(property_search_storage.__file__).read_text(encoding="utf-8")
    env_example = Path(".env.example").read_text(encoding="utf-8")

    assert "_PROPERTY_SEARCH_RUN_DB_CONNECT_RETRY_SECONDS = 45.0" in source
    assert "return 4" in inspect.getsource(property_search_storage._property_search_run_db_max_connections)  # type: ignore[attr-defined]
    assert "min(parsed, 16)" in inspect.getsource(property_search_storage._property_search_run_db_max_connections)  # type: ignore[attr-defined]
    assert "PROPERTYQUARRY_SEARCH_DB_MAX_CONNECTIONS=4" in env_example
    assert "PROPERTYQUARRY_SEARCH_DB_CONNECT_RETRY_SECONDS=45" in env_example


def test_property_search_run_load_falls_back_to_memory_during_db_pressure(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = f"run-pressure-fallback-{uuid.uuid4().hex}"
    principal_id = "exec-property-pressure-fallback"
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1010 Vienna"},
        force_refresh=False,
    )
    state["status"] = "in_progress"
    state["updated_at"] = "2026-07-03T12:00:00+00:00"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)

    def _raise_pressure(**kwargs):  # noqa: ANN003
        raise RuntimeError('connection failed: FATAL: sorry, too many clients already')

    monkeypatch.setattr(product_service, "_load_property_search_run_record_storage", _raise_pressure)
    monkeypatch.setattr(product_service, "_load_property_search_run_compact_record_storage", _raise_pressure)

    try:
        loaded = product_service._load_property_search_run_record(run_id=run_id, principal_id=principal_id)
        compact = product_service._load_property_search_run_compact_record(run_id=run_id, principal_id=principal_id)
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.pop(run_id, None)

    assert loaded is not None
    assert loaded["run_id"] == run_id
    assert loaded["status"] == "in_progress"
    assert compact is not None
    assert compact["run_id"] == run_id
    assert compact["status"] == "in_progress"


def test_property_search_run_list_falls_back_to_memory_during_db_pressure(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "exec-property-pressure-list"
    run_id = f"run-pressure-list-{uuid.uuid4().hex}"
    state = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT", "location_query": "1020 Vienna"},
        force_refresh=False,
    )
    state["status"] = "processed"
    state["progress"] = 100
    state["updated_at"] = "2026-07-03T12:01:00+00:00"
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(state)

    def _raise_pressure(**kwargs):  # noqa: ANN003
        raise RuntimeError("database_busy: property search storage connection queue is full")

    monkeypatch.setattr(product_service, "_list_property_search_run_records_storage", _raise_pressure)

    try:
        rows = product_service._list_property_search_run_records(
            limit=5,
            statuses=("processed",),
            principal_id=principal_id,
            lightweight=True,
        )
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.pop(run_id, None)

    assert [row.get("run_id") for row in rows] == [run_id]
    assert rows[0]["status"] == "processed"


def test_property_source_listing_cache_postgres_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    db_url = str(os.environ.get("EA_TEST_PROPERTY_DATABASE_URL") or "").strip()
    if not db_url:
        pytest.skip("EA_TEST_PROPERTY_DATABASE_URL is not set")
    from app.product.property_search_schema import migrate_property_search_schema

    migrate_property_search_schema(db_url, applied_by="property-search-cache-contract")
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_BACKEND", "postgres")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("EA_PROPERTY_SOURCE_LISTING_CACHE_STALE_MAX_SECONDS", "3600")
    monkeypatch.setattr(property_search_storage, "_PROPERTY_SOURCE_LISTING_CACHE_SCHEMA_READY", False)
    cache_key = f"willhaben:postgres-round-trip:{uuid.uuid4().hex}"
    listing_urls = (
        "https://www.willhaben.at/iad/object?adId=postgres-cache-1",
        "https://www.willhaben.at/iad/object?adId=postgres-cache-2",
    )
    with product_service._PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        product_service._PROPERTY_SOURCE_LISTING_CACHE.clear()
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_PATH = ""
        product_service._PROPERTY_SOURCE_LISTING_CACHE_LOADED_MTIME = 0.0

    stored = product_service._property_source_listing_cache_put(
        cache_key,
        source_url="https://www.willhaben.at/iad/immobilien/mietwohnungen?ESTATE_SIZE%2FLIVING_AREA_FROM=85",
        listing_urls=listing_urls,
        source_spec={"provider_filter_pushdown": {"cache_key": cache_key, "min_area_m2": 85}},
    )
    with product_service._PROPERTY_SOURCE_LISTING_CACHE_LOCK:
        product_service._PROPERTY_SOURCE_LISTING_CACHE.clear()

    cached_urls, cached_state = product_service._property_source_listing_cache_get(cache_key)

    assert stored["persistence"] == "postgres"
    assert cached_urls == listing_urls
    assert cached_state["status"] == "hit"
    assert cached_state["persistence"] == "postgres"
    assert cached_state["listing_total"] == 2


def test_property_search_storage_schema_scripts() -> None:
    db_url = str(os.environ.get("EA_TEST_PROPERTY_DATABASE_URL") or "").strip()
    if not db_url:
        pytest.skip("EA_TEST_PROPERTY_DATABASE_URL is not set")

    env = dict(os.environ)
    env["DATABASE_URL"] = db_url
    env["PYTHONPATH"] = "ea"

    migrate = subprocess.run(
        ["python3", "scripts/migrate_property_search_storage.py"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert migrate.returncode == 0, migrate.stderr or migrate.stdout

    check = subprocess.run(
        ["python3", "scripts/check_property_search_storage_schema.py"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr or check.stdout


def test_property_search_storage_schema_check_enforces_tenant_primary_key() -> None:
    source = Path("scripts/check_property_search_storage_schema.py").read_text(encoding="utf-8")
    migration_source = Path("ea/app/product/property_search_schema.py").read_text(
        encoding="utf-8"
    )

    assert "idx_property_search_runs_principal_updated" in migration_source
    assert "idx_property_search_runs_status_updated" in migration_source
    assert "idx_property_search_runs_principal_status_updated" in migration_source
    assert "_check_source_contracts()" in source
    assert "ON CONFLICT (principal_id, run_id) DO UPDATE" in source
    assert "payload_retention_status" in source
    assert "compact_only" in source
    assert "UPDATE property_search_runs AS runs" in source
    assert "SET principal_id = EXCLUDED.principal_id" in source
    assert "forbidden_storage_contract" in source
    assert "if not normalized_principal_id and not admin:" in source
    assert "runtime_schema_ddl_forbidden" in source
    assert "property_search_runs_pkey" in migration_source
    assert "pg_advisory_xact_lock" in migration_source
    assert "checksum_sha256" in migration_source
    assert "(payload_json->>'status') = ANY(%s)" in source


def test_property_search_storage_schema_check_runs_source_contracts_without_database() -> None:
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env["PYTHONPATH"] = "ea"

    result = subprocess.run(
        ["python3", "scripts/check_property_search_storage_schema.py"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "source contracts look ready" in result.stdout
