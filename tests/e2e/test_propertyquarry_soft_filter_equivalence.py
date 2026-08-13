from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
import urllib.parse

import app.product.service as product_service
from app.product.property_fact_enrichment import (
    PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION,
    property_fact_coordinate_digest,
    property_fact_issue_provider_attestation,
    property_fact_source_fingerprint,
)
from app.product.property_location_research import (
    PROPERTY_FACT_OSM_QUERY_ENDPOINT,
    PROPERTY_FACT_OSM_QUERY_SCHEMA,
    property_fact_osm_nearby_query,
)
from app.product.service import ProductService
from tests.product_test_helpers import build_property_client, start_workspace


_OSM_CLASSIFICATION_BY_FACT: dict[str, dict[str, str]] = {
    "nearest_library_m": {"amenity": "library"},
    "nearest_playground_m": {"leisure": "playground"},
    "nearest_shopping_center_m": {"shop": "mall"},
    "nearest_supermarket_m": {"shop": "supermarket"},
    "nearest_theatre_m": {"amenity": "theatre"},
}


def _poll_search_run(client, run_id: str) -> dict[str, object]:
    latest: dict[str, object] = {}
    for _ in range(600):
        response = client.get(f"/app/api/property/search-runs/{run_id}")
        assert response.status_code == 200, response.text
        latest = response.json()
        if str(latest.get("status") or "") in {"processed", "completed_partial", "failed", "cancelled"}:
            return latest
        time.sleep(0.05)
    return latest


def _candidate_urls(status: dict[str, object]) -> set[str]:
    summary = dict(status.get("summary") or {})
    rows: list[dict[str, object]] = []
    for source in list(summary.get("sources") or []):
        if not isinstance(source, dict):
            continue
        rows.extend(row for row in list(source.get("research_candidates") or []) if isinstance(row, dict))
    if not rows:
        for row in list(summary.get("ranked_candidates") or []):
            if isinstance(row, dict):
                rows.append(row)
    return {str(row.get("property_url") or "").strip() for row in rows if str(row.get("property_url") or "").strip()}


def _candidate_fact_rows(status: dict[str, object]) -> list[dict[str, object]]:
    summary = dict(status.get("summary") or {})
    rows: list[dict[str, object]] = []
    for source in list(summary.get("sources") or []):
        if not isinstance(source, dict):
            continue
        for row in list(source.get("research_candidates") or []):
            if isinstance(row, dict):
                rows.append(row)
    for row in list(summary.get("ranked_candidates") or []):
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _with_fresh_exact_distance_evidence(
    *,
    property_url: str,
    facts: dict[str, object],
) -> dict[str, object]:
    observed_at = datetime.now(timezone.utc)
    expires_at = observed_at + timedelta(hours=24)
    latitude = 48.2082
    longitude = 16.3738
    query = property_fact_osm_nearby_query(latitude, longitude)
    evidence_by_key: dict[str, dict[str, object]] = {}
    for key, raw_distance in facts.items():
        if key not in _OSM_CLASSIFICATION_BY_FACT:
            continue
        distance_m = int(raw_distance)
        object_id = str(10_000_000 + sum(ord(value) for value in key))
        evidence: dict[str, object] = {
            "provider": "openstreetmap_overpass",
            "method": "straight_line_osm",
            "observed_at": observed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "freshness": "fresh",
            "confidence": 0.95,
            "source_key": key,
            "observed_key": key,
            "listing_url": urllib.parse.urldefrag(property_url)[0],
            "source_fingerprint": property_fact_source_fingerprint(property_url),
            "coordinate_basis": "candidate_listing_coordinates",
            "coordinate_observed_at": observed_at.isoformat(),
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
            "provider_object_timestamp": observed_at.isoformat(),
            "provider_object_changeset": "123456",
            "provider_observed_at": observed_at.isoformat(),
            "provider_expires_at": expires_at.isoformat(),
            "poi_latitude": latitude + math.degrees(float(distance_m) / 6_371_000.0),
            "poi_longitude": longitude,
            "poi_classification_tags": dict(_OSM_CLASSIFICATION_BY_FACT[key]),
            "attestation_version": PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION,
        }
        evidence["provider_attestation"] = property_fact_issue_provider_attestation(
            evidence,
            observed_value=distance_m,
        )
        evidence_by_key[key] = evidence
    return {
        **facts,
        "map_lat": latitude,
        "map_lng": longitude,
        "map_location_precision": "address",
        "property_fact_evidence": evidence_by_key,
    }


def test_propertyquarry_live_soft_filter_ablation_fixture_preserves_diagnostic_truth() -> None:
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "propertyquarry_live_soft_filter_ablation_20260619.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    baseline = dict(payload["baseline"])
    neutral_soft = dict(payload["neutral_soft"])
    no_location = dict(payload["no_location_hard_scope"])

    assert baseline["status"] == "completed_partial"
    assert int(baseline["sources_completed"]) < int(baseline["sources_total"])
    assert int(baseline["filtered_low_fit_total"]) == 0
    assert int(neutral_soft["filtered_low_fit_total"]) == 0
    assert int(no_location["filtered_low_fit_total"]) == 0
    assert int(neutral_soft["ranked_count"]) == int(no_location["ranked_count"]) == 10
    assert int(neutral_soft["sources_completed"]) == int(neutral_soft["sources_total"])
    assert int(no_location["sources_completed"]) == int(no_location["sources_total"])
    assert int(neutral_soft["filtered_area_total"]) > int(neutral_soft["filtered_low_fit_total"])
    assert int(neutral_soft["filtered_generic_page_total"]) > int(neutral_soft["filtered_low_fit_total"])


def test_propertyquarry_e2e_soft_preferences_preserve_search_hits(monkeypatch) -> None:
    principal_id = "exec-property-e2e-soft-filter-equivalence"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Soft Filter E2E Equivalence")

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"
    listing_urls = [
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/e2e-soft-filter-a/",
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/e2e-soft-filter-b/",
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/e2e-soft-filter-c/",
    ]
    facts_by_url = {
        listing_urls[0]: {
            "postal_name": "1020 Wien",
            "area_sqm": 76,
            "rooms": 3,
            "total_rent_eur": 1640,
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
            "nearest_library_m": 220,
            "nearest_playground_m": 420,
            "nearest_shopping_center_m": 900,
            "nearest_theatre_m": 350,
            "nearest_supermarket_m": 180,
        },
        listing_urls[2]: {
            "postal_name": "1020 Wien",
            "area_sqm": 88,
            "rooms": 4,
            "total_rent_eur": 1850,
            "nearest_library_m": 760,
            "nearest_playground_m": 80,
            "nearest_shopping_center_m": 1400,
            "nearest_theatre_m": 1400,
            "nearest_supermarket_m": 110,
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
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:e2e-soft-filter-equivalence"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        facts = _with_fresh_exact_distance_evidence(
            property_url=property_url,
            facts=dict(facts_by_url[property_url]),
        )
        return {
            "listing_id": property_url.rsplit("/", 2)[-2],
            "title": f"Mietwohnung in 1020 Wien {property_url.rsplit('/', 2)[-2]}",
            "summary": "Mietwohnung in 1020 Wien mit Balkon, Lift und guter Anbindung.",
            "property_facts_json": facts,
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)
    monkeypatch.setattr(
        product_service,
        "_merge_property_facts_with_source_research",
        lambda *, property_url, property_facts, image_urls=(): dict(
            property_facts or {}
        ),
    )

    def _fake_fit(**kwargs) -> dict[str, object]:
        property_url = str(kwargs.get("property_url") or "")
        score = 44.0 if property_url.endswith("e2e-soft-filter-a/") else 58.0
        if property_url.endswith("e2e-soft-filter-c/"):
            score = 52.0
        return {
            "fit_score": score,
            "recommendation": "review",
            "match_reasons_json": ["Hard search basics match"],
            "mismatch_reasons_json": ["Optional daily-life preferences differ"],
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
        "selected_districts": ["1020"],
        "property_type": "apartment",
        "search_mode": "discovery",
        "min_match_score": 90,
        "require_floorplan": False,
        "property_commercial": {
            "active_plan_key": "agent",
            "status": "active",
            "active_until": "2999-01-01T00:00:00+00:00",
        },
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

    plain_started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": hard_preferences,
            "force_refresh": True,
            "max_results_per_source": 5,
        },
    )
    assert plain_started.status_code == 202, plain_started.text
    soft_started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": soft_preferences,
            "force_refresh": True,
            "max_results_per_source": 5,
        },
    )
    assert soft_started.status_code == 202, soft_started.text

    plain_status = _poll_search_run(client, plain_started.json()["run_id"])
    soft_status = _poll_search_run(client, soft_started.json()["run_id"])

    assert plain_status["status"] == "processed", json.dumps(
        plain_status, ensure_ascii=False, sort_keys=True, default=str
    )
    assert soft_status["status"] == "processed", json.dumps(
        soft_status, ensure_ascii=False, sort_keys=True, default=str
    )
    assert _candidate_urls(plain_status) == set(listing_urls)
    assert _candidate_urls(soft_status) == set(listing_urls)
    assert _candidate_urls(soft_status) == _candidate_urls(plain_status)
    assert any(
        dict(row.get("property_facts") or {}).get("distance_preference_notes")
        or dict(row.get("property_facts") or {}).get("score_demoted_by_match_threshold")
        for row in _candidate_fact_rows(soft_status)
    )


def test_propertyquarry_e2e_targeted_listing_survives_strict_and_soft_runs(monkeypatch) -> None:
    principal_id = "exec-property-e2e-targeted-recovery"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Targeted Recovery E2E")

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1020-leopoldstadt"
    target_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/targeted-recovery-match/"
    same_scope_alt_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/targeted-recovery-alt/"
    off_scope_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1220-donaustadt/targeted-recovery-off-scope/"
    listing_urls = [target_url, same_scope_alt_url, off_scope_url]

    facts_by_url = {
        target_url: {
            "postal_name": "1020 Wien",
            "property_type": "apartment",
            "area_sqm": 78,
            "rooms": 3,
            "total_rent_eur": 1650,
            "nearest_library_m": 1400,
            "nearest_playground_m": 850,
            "nearest_shopping_center_m": 180,
            "nearest_theatre_m": 920,
            "nearest_supermarket_m": 520,
        },
        same_scope_alt_url: {
            "postal_name": "1020 Wien",
            "property_type": "apartment",
            "area_sqm": 82,
            "rooms": 3,
            "total_rent_eur": 1680,
            "nearest_library_m": 260,
            "nearest_playground_m": 180,
            "nearest_shopping_center_m": 420,
            "nearest_theatre_m": 240,
            "nearest_supermarket_m": 140,
        },
        off_scope_url: {
            "postal_name": "1220 Wien",
            "property_type": "apartment",
            "area_sqm": 81,
            "rooms": 3,
            "total_rent_eur": 1620,
            "nearest_library_m": 180,
            "nearest_playground_m": 140,
            "nearest_shopping_center_m": 230,
            "nearest_theatre_m": 220,
            "nearest_supermarket_m": 90,
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
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:e2e-targeted-recovery"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        facts = _with_fresh_exact_distance_evidence(
            property_url=property_url,
            facts=dict(facts_by_url[property_url]),
        )
        listing_id = property_url.rstrip("/").rsplit("/", 1)[-1]
        return {
            "listing_id": listing_id,
            "title": f"Mietwohnung in {facts['postal_name']} | {facts['area_sqm']} m2 | {facts['rooms']} Zimmer",
            "summary": "Balkonwohnung mit Lift und guter Anbindung.",
            "property_facts_json": facts,
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)
    monkeypatch.setattr(
        product_service,
        "_merge_property_facts_with_source_research",
        lambda *, property_url, property_facts, image_urls=(): dict(
            property_facts or {}
        ),
    )

    def _fake_fit(**kwargs) -> dict[str, object]:
        property_url = str(kwargs.get("property_url") or "")
        score = 43.0 if property_url == target_url else 57.0
        if property_url == off_scope_url:
            score = 94.0
        return {
            "fit_score": score,
            "recommendation": "review",
            "match_reasons_json": ["Hard basics match the requested listing brief."],
            "mismatch_reasons_json": ["Optional preferences differ."],
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

    strict_preferences = {
        "country_code": "AT",
        "listing_mode": "rent",
        "location_query": "1020 Vienna",
        "selected_districts": ["1020 Vienna"],
        "property_type": "apartment",
        "search_mode": "discovery",
        "max_price_eur": 1700,
        "min_area_m2": 70,
        "min_rooms": 3,
        "min_match_score": 95,
        "require_floorplan": False,
        "property_commercial": {
            "active_plan_key": "agent",
            "status": "active",
            "active_until": "2999-01-01T00:00:00+00:00",
        },
    }
    soft_preferences = {
        **strict_preferences,
        "max_distance_to_library_m": 400,
        "max_distance_to_library_importance": "strong_wish",
        "max_distance_to_playground_m": 250,
        "max_distance_to_playground_importance": "nice_to_have",
        "max_distance_to_theatre_m": 350,
        "max_distance_to_theatre_importance": "strong_wish",
        "max_distance_to_supermarket_m": 200,
        "max_distance_to_supermarket_importance": "nice_to_have",
        "avoid_noise_risk_area": True,
        "require_high_speed_internet_evidence": True,
    }

    run_statuses: dict[str, dict[str, object]] = {}
    for label, preferences in (("strict", strict_preferences), ("soft", soft_preferences)):
        started = client.post(
            "/app/api/property/search-runs",
            json={
                "selected_platforms": ["willhaben"],
                "property_preferences": preferences,
                "force_refresh": True,
                "max_results_per_source": 5,
            },
        )
        assert started.status_code == 202, started.text
        run_statuses[label] = _poll_search_run(client, started.json()["run_id"])

    strict_status = run_statuses["strict"]
    soft_status = run_statuses["soft"]
    assert strict_status["status"] == "processed"
    assert soft_status["status"] == "processed"

    strict_urls = _candidate_urls(strict_status)
    soft_urls = _candidate_urls(soft_status)
    assert target_url in strict_urls
    assert target_url in soft_urls
    assert same_scope_alt_url in strict_urls
    assert same_scope_alt_url in soft_urls
    assert off_scope_url not in strict_urls
    assert off_scope_url not in soft_urls
    assert any(
        str(row.get("property_url") or "") == target_url
        and float(row.get("assessment_fit_score") or 0.0) < float(strict_preferences["min_match_score"])
        and str(row.get("flythrough_reason") or "") == "fit_below_threshold"
        for row in _candidate_fact_rows(strict_status)
    )
    assert any(
        str(row.get("property_url") or "") == target_url
        and dict(row.get("property_facts") or {}).get("distance_preference_notes")
        for row in _candidate_fact_rows(soft_status)
    )


def test_propertyquarry_e2e_exact_district_selection_remains_a_hard_filter(monkeypatch) -> None:
    principal_id = "exec-property-e2e-location-hard-filter"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Location Hard Filter E2E")

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1010-innere-stadt"
    in_scope_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/e2e-location-1010/"
    wrong_vienna_url = "https://www.derstandard.at/immobilien/wohnung-mieten-in-1220-wien-e2e-location"
    wrong_region_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/e2e-location-salzburg/"
    wrong_project_url = "https://www.raiffeisen-wohnbau.at/de/projects/id/1090-vienna/augasse-17/70?quot%3B%2Fn="
    listing_urls = [in_scope_url, wrong_vienna_url, wrong_region_url, wrong_project_url]

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
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:e2e-location-hard-filter"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        if property_url == in_scope_url:
            return {
                "listing_id": "e2e-location-1010",
                "title": "Mietwohnung in 1010 Wien | 77 m2 | 3 Zimmer",
                "summary": "Ruhige Wohnung in der Inneren Stadt.",
                "property_facts_json": {"postal_name": "1010 Wien", "area_sqm": 77, "rooms": 3, "total_rent_eur": 1590},
            }
        if property_url == wrong_vienna_url:
            return {
                "listing_id": "e2e-location-1220",
                "title": "Wohnung mieten in 1220 Wien | 60 m2 | 2 Zimmer | EUR 1.090",
                "summary": "2-Zimmer Wohnung mit Traumblick / UNO und U-Bahn ums Eck in 1220 Wien.",
                "property_facts_json": {
                    "postal_name": "1010 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "source_postal_code": "1010",
                    "area_sqm": 60,
                    "rooms": 2,
                    "total_rent_eur": 1090,
                },
            }
        if property_url == wrong_project_url:
            return {
                "listing_id": "e2e-location-raiffeisen-1090",
                "title": "Augasse 17 | Raiffeisen WohnBau",
                "summary": "Projektadresse Augasse 17 in 1090 Wien.",
                "property_facts_json": {
                    "postal_name": "1010 Vienna",
                    "source_scope_location": "1010 Vienna",
                    "source_postal_code": "1010",
                    "area_sqm": 70,
                    "rooms": 2,
                    "purchase_price_eur": 520000,
                },
            }
        return {
            "listing_id": "e2e-location-salzburg",
            "title": "Moderne Zwei-Zimmer Wohnung mit Terrasse",
            "summary": "Moderne Wohnung mit Penthouse-Charakter in Salzburg.",
            "property_facts_json": {
                "postal_name": "1010 Vienna",
                "source_scope_location": "1010 Vienna",
                "source_postal_code": "1010",
                "area_sqm": 70,
                "rooms": 2,
                "total_rent_eur": 1320,
            },
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)
    monkeypatch.setattr(
        product_service,
        "_merge_property_facts_with_source_research",
        lambda *, property_url, property_facts, image_urls=(): dict(
            property_facts or {}
        ),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {
            "fit_score": 72.0,
            "recommendation": "review",
            "match_reasons_json": ["Listing basics match"],
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
            "editor_url": f"/app/research/{str(kwargs.get('source_ref') or 'candidate').split(':')[-1]}",
        },
    )

    started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": {
                "country_code": "AT",
                "listing_mode": "rent",
                "location_query": "Wien",
                "selected_districts": ["1010 Vienna"],
                "property_type": "apartment",
                "search_mode": "discovery",
                "min_match_score": 50,
                "require_floorplan": False,
                "max_distance_to_playground_m": 100,
                "max_distance_to_playground_importance": "nice_to_have",
                "property_commercial": {
                    "active_plan_key": "agent",
                    "status": "active",
                    "active_until": "2999-01-01T00:00:00+00:00",
                },
            },
            "force_refresh": True,
            "max_results_per_source": 5,
        },
    )
    assert started.status_code == 202, started.text

    status = _poll_search_run(client, started.json()["run_id"])

    assert status["status"] == "processed"
    assert _candidate_urls(status) == {in_scope_url}
    summary = dict(status.get("summary") or {})
    assert int(summary.get("filtered_area_total") or 0) >= 1


def test_propertyquarry_e2e_adjacent_districts_enabled_for_fuzzy_search(monkeypatch) -> None:
    principal_id = "exec-property-e2e-location-adjacent-fuzzy"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Location Adjacent Fuzzy E2E")

    source_url = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/wien-1010-innere-stadt"
    in_scope_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/e2e-location-1010/"
    adjacent_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1020-leopoldstadt/e2e-location-1020/"
    wrong_vienna_url = "https://www.derstandard.at/immobilien/wohnung-mieten-in-1220-wien-e2e-location"
    wrong_region_url = "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/e2e-location-salzburg/"
    listing_urls = [in_scope_url, adjacent_url, wrong_vienna_url, wrong_region_url]

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
                "provider_cache_state": {"status": "miss", "cache_key": "willhaben:e2e-location-adjacent"},
                "timing_ms": {"provider_fetch": 1.0},
            }
        },
    )

    def _fake_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        if property_url == in_scope_url:
            return {
                "listing_id": "e2e-location-adjacent-1010",
                "title": "Mietwohnung in 1010 Wien | 77 m2 | 3 Zimmer",
                "summary": "Ruhige Wohnung in der Inneren Stadt.",
                "property_facts_json": {"postal_name": "1010 Wien", "area_sqm": 77, "rooms": 3, "total_rent_eur": 1590},
            }
        if property_url == adjacent_url:
            return {
                "listing_id": "e2e-location-adjacent-1020",
                "title": "Wohnung in 1020 Wien | 70 m² | 3 Zimmer",
                "summary": "Wohnung in Leopoldstadt.",
                "property_facts_json": {"postal_name": "1020 Wien", "area_sqm": 70, "rooms": 3, "total_rent_eur": 1690},
            }
        if property_url == wrong_vienna_url:
            return {
                "listing_id": "e2e-location-1220",
                "title": "Wohnung mieten in 1220 Wien | 60 m2 | 2 Zimmer | EUR 1.090",
                "summary": "2-Zimmer Wohnung in Donaustadt.",
                "property_facts_json": {"postal_name": "1220 Wien", "area_sqm": 60, "rooms": 2, "total_rent_eur": 1090},
            }
        return {
            "listing_id": "e2e-location-salzburg",
            "title": "Moderne Zwei-Zimmer Wohnung mit Terrasse",
            "summary": "Moderne Wohnung mit Penthouse-Charakter in Salzburg.",
            "property_facts_json": {"postal_name": "5020 Salzburg", "area_sqm": 72, "rooms": 2, "total_rent_eur": 1320},
        }

    monkeypatch.setattr(product_service, "_property_scout_page_preview_with_timeout", _fake_preview)
    monkeypatch.setattr(
        product_service,
        "_merge_property_facts_with_source_research",
        lambda *, property_url, property_facts, image_urls=(): dict(
            property_facts or {}
        ),
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **kwargs: {
            "fit_score": 72.0,
            "recommendation": "review",
            "match_reasons_json": ["Listing basics match"],
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
            "editor_url": f"/app/research/{str(kwargs.get('source_ref') or 'candidate').split(':')[-1]}",
        },
    )

    strict_started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": {
                "country_code": "AT",
                "listing_mode": "rent",
                "location_query": "Wien",
                "selected_districts": ["1010 Vienna"],
                "property_type": "apartment",
                "search_mode": "discovery",
                "min_match_score": 50,
                "require_floorplan": False,
                "max_distance_to_playground_m": 100,
                "max_distance_to_playground_importance": "nice_to_have",
                "property_commercial": {
                    "active_plan_key": "agent",
                    "status": "active",
                    "active_until": "2999-01-01T00:00:00+00:00",
                },
            },
            "force_refresh": True,
            "max_results_per_source": 5,
        },
    )
    assert strict_started.status_code == 202, strict_started.text
    strict_status = _poll_search_run(client, strict_started.json()["run_id"])

    assert strict_status["status"] == "processed", json.dumps(
        strict_status, ensure_ascii=False, sort_keys=True, default=str
    )
    assert _candidate_urls(strict_status) == {in_scope_url}

    fuzzy_started = client.post(
        "/app/api/property/search-runs",
        json={
            "selected_platforms": ["willhaben"],
            "property_preferences": {
                "country_code": "AT",
                "listing_mode": "rent",
                "location_query": "Wien",
                "selected_districts": ["1010 Vienna"],
                "adjacent_area_radius_value": 1.0,
                "adjacent_area_radius_unit": "km",
                "property_type": "apartment",
                "search_mode": "discovery",
                "min_match_score": 50,
                "require_floorplan": False,
                "max_distance_to_playground_m": 100,
                "max_distance_to_playground_importance": "nice_to_have",
                "property_commercial": {
                    "active_plan_key": "agent",
                    "status": "active",
                    "active_until": "2999-01-01T00:00:00+00:00",
                },
            },
            "force_refresh": True,
            "max_results_per_source": 5,
        },
    )
    assert fuzzy_started.status_code == 202, fuzzy_started.text
    fuzzy_status = _poll_search_run(client, fuzzy_started.json()["run_id"])

    assert fuzzy_status["status"] == "processed"
    candidates = _candidate_urls(fuzzy_status)
    assert in_scope_url in candidates
    assert adjacent_url in candidates
    assert wrong_vienna_url not in candidates
    assert wrong_region_url not in candidates
