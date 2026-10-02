#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from app.product import service as product_service  # noqa: E402
from app.product.property_fact_enrichment import (  # noqa: E402
    PROPERTY_FACT_PROVIDER_ATTESTATION_VERSION,
    property_fact_coordinate_digest,
    property_fact_issue_provider_attestation,
    property_fact_source_fingerprint,
)
from app.product.property_location_research import (  # noqa: E402
    PROPERTY_FACT_OSM_QUERY_ENDPOINT,
    PROPERTY_FACT_OSM_QUERY_SCHEMA,
    property_fact_osm_nearby_query,
)


@dataclass(frozen=True)
class BenchmarkCandidate:
    candidate_ref: str
    property_url: str
    title: str
    summary: str
    facts: dict[str, object]
    base_score: float
    relevance: int
    expected_hard_filtered: bool = False
    expected_notified: bool = False


HARD_PREFERENCES: dict[str, object] = {
    "country_code": "AT",
    "region_code": "vienna",
    "listing_mode": "rent",
    "location_query": "Wien",
    "selected_districts": ["1010 Vienna"],
    "property_type": "apartment",
}

SOFT_PREFERENCES: dict[str, object] = {
    **HARD_PREFERENCES,
    "max_distance_to_playground_m": 100,
    "max_distance_to_playground_importance": "nice_to_have",
    "max_distance_to_library_m": 500,
    "max_distance_to_library_importance": "strong_wish",
    "max_distance_to_shopping_center_m": 500,
    "max_distance_to_shopping_center_importance": "avoid",
    "max_distance_to_supermarket_m": 300,
    "max_distance_to_supermarket_importance": "nice_to_have",
}

_OSM_CLASSIFICATION_BY_FACT: dict[str, dict[str, str]] = {
    "nearest_library_m": {"amenity": "library"},
    "nearest_playground_m": {"leisure": "playground"},
    "nearest_shopping_center_m": {"shop": "mall"},
    "nearest_supermarket_m": {"shop": "supermarket"},
}

BENCHMARK_CANDIDATES: tuple[BenchmarkCandidate, ...] = (
    BenchmarkCandidate(
        candidate_ref="target-1010",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/target-1010/",
        title="Mietwohnung in 1010 Wien | 77 m2 | 3 Zimmer | EUR 1.590",
        summary="Ruhige Wohnung in der Inneren Stadt mit guter U-Bahn-Anbindung.",
        facts={
            "postal_name": "1010 Wien",
            "location": "1010 Wien, Innere Stadt",
            "street_address": "Karnter Strasse 12, 1010 Wien",
            "area_sqm": 77,
            "rooms": 3,
            "total_rent_eur": 1590,
            "nearest_playground_m": 260,
            "nearest_library_m": 420,
            "nearest_shopping_center_m": 1300,
            "nearest_supermarket_m": 180,
        },
        base_score=82.0,
        relevance=3,
        expected_notified=True,
    ),
    BenchmarkCandidate(
        candidate_ref="soft-mismatch-1010",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/soft-mismatch/",
        title="Wohnung mieten in 1010 Wien | 80 m2 | 3 Zimmer | EUR 1.720",
        summary="Central apartment that misses several optional daily-life preferences.",
        facts={
            "postal_name": "1010 Wien",
            "location": "1010 Wien, Innere Stadt",
            "area_sqm": 80,
            "rooms": 3,
            "total_rent_eur": 1720,
            "nearest_playground_m": 1200,
            "nearest_library_m": 1800,
            "nearest_shopping_center_m": 160,
            "nearest_supermarket_m": 760,
        },
        base_score=69.0,
        relevance=2,
        expected_notified=False,
    ),
    BenchmarkCandidate(
        candidate_ref="low-score-1010",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1010-innere-stadt/low-score/",
        title="Kompakte Wohnung in 1010 Wien | 42 m2 | 1 Zimmer | EUR 1.450",
        summary="Location fits, but the personal fit score is low.",
        facts={
            "postal_name": "1010 Wien",
            "location": "1010 Wien, Innere Stadt",
            "area_sqm": 42,
            "rooms": 1,
            "total_rent_eur": 1450,
            "nearest_playground_m": 1500,
            "nearest_library_m": 1900,
            "nearest_shopping_center_m": 180,
            "nearest_supermarket_m": 900,
        },
        base_score=50.0,
        relevance=1,
        expected_notified=False,
    ),
    BenchmarkCandidate(
        candidate_ref="wrong-1220",
        property_url="https://www.derstandard.at/immobilien/wohnung-mieten-in-1220-wien-benchmark",
        title="Wohnung mieten in 1220 Wien | 60 m2 | 2 Zimmer | EUR 1.090",
        summary="2-Zimmer Wohnung mit Traumblick / UNO und U-Bahn ums Eck in 1220 Wien.",
        facts={
            "postal_name": "1010 Vienna",
            "source_scope_location": "1010 Vienna",
            "source_postal_code": "1010",
            "source_city": "Vienna",
            "area_sqm": 60,
            "rooms": 2,
            "total_rent_eur": 1090,
        },
        base_score=74.0,
        relevance=0,
        expected_hard_filtered=True,
    ),
    BenchmarkCandidate(
        candidate_ref="wrong-1090",
        property_url="https://www.raiffeisen-wohnbau.at/de/projects/id/1090-vienna/augasse-17/70?quot%3B%2Fn=",
        title="Augasse 17 | Raiffeisen WohnBau",
        summary="Provider card was returned from a selected 1010 source scope.",
        facts={
            "postal_name": "1010 Vienna",
            "source_scope_location": "1010 Vienna",
            "source_postal_code": "1010",
            "source_city": "Vienna",
            "area_sqm": 70,
            "rooms": 2,
            "purchase_price_eur": 520000,
        },
        base_score=76.0,
        relevance=0,
        expected_hard_filtered=True,
    ),
    BenchmarkCandidate(
        candidate_ref="wrong-salzburg",
        property_url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/salzburg/salzburg-stadt/benchmark-salzburg/",
        title="Moderne Zwei-Zimmer Wohnung mit Terrasse",
        summary="Moderne Wohnung mit Penthouse-Charakter in Salzburg.",
        facts={
            "postal_name": "1010 Vienna",
            "source_scope_location": "1010 Vienna",
            "source_postal_code": "1010",
            "source_city": "Vienna",
            "area_sqm": 72,
            "rooms": 2,
            "total_rent_eur": 1320,
        },
        base_score=78.0,
        relevance=0,
        expected_hard_filtered=True,
    ),
)


def _location_hints(preferences: dict[str, object]) -> tuple[str, ...]:
    return product_service._property_search_location_hints(preferences)


def _facts_with_valid_osm_evidence(
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
    if not evidence_by_key:
        return facts
    return {
        **facts,
        "map_lat": latitude,
        "map_lng": longitude,
        "map_location_precision": "address",
        "property_fact_evidence": evidence_by_key,
    }


def _enriched_facts(candidate: BenchmarkCandidate, *, preferences: dict[str, object]) -> dict[str, object]:
    return product_service._property_enrich_facts_from_listing_text(
        facts=_facts_with_valid_osm_evidence(
            property_url=candidate.property_url,
            facts=dict(candidate.facts),
        ),
        title=candidate.title,
        summary=candidate.summary,
        listing_mode=str(preferences.get("listing_mode") or ""),
    )


def _matches_location(candidate: BenchmarkCandidate, *, preferences: dict[str, object]) -> bool:
    return product_service._property_candidate_matches_requested_location(
        location_hints=_location_hints(preferences),
        property_url=candidate.property_url,
        title=candidate.title,
        summary=candidate.summary,
        property_facts=_enriched_facts(candidate, preferences=preferences),
        country_code=str(preferences.get("country_code") or ""),
        region_code=str(preferences.get("region_code") or ""),
    )


def _ranked_rows(
    candidates: Iterable[BenchmarkCandidate],
    *,
    preferences: dict[str, object],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for candidate in candidates:
        if not _matches_location(candidate, preferences=preferences):
            continue
        adjustment, notes = product_service._property_distance_preference_score_adjustment(
            preferences=preferences,
            property_facts=_enriched_facts(candidate, preferences=preferences),
            property_url=candidate.property_url,
        )
        score = max(0.0, min(100.0, candidate.base_score + adjustment))
        rows.append(
            {
                "candidate_ref": candidate.candidate_ref,
                "score": round(score, 2),
                "base_score": candidate.base_score,
                "score_delta": round(adjustment, 2),
                "relevance": candidate.relevance,
                "notes": list(notes),
                "notified": score >= product_service._property_scout_outbound_notification_min_score(),
            }
        )
    rows.sort(key=lambda row: (float(row["score"]), str(row["candidate_ref"])), reverse=True)
    return rows


def _dcg(relevances: Iterable[int]) -> float:
    score = 0.0
    for index, relevance in enumerate(relevances, start=1):
        score += (2.0**float(relevance) - 1.0) / math.log2(index + 1.0)
    return score


def _precision_at(rows: list[dict[str, object]], cutoff: int) -> float:
    if cutoff <= 0:
        return 0.0
    top = rows[:cutoff]
    return sum(1 for row in top if int(row["relevance"]) > 0) / float(cutoff)


def _recall_at(rows: list[dict[str, object]], all_candidates: Iterable[BenchmarkCandidate], cutoff: int) -> float:
    relevant_total = sum(1 for candidate in all_candidates if candidate.relevance > 0 and not candidate.expected_hard_filtered)
    if relevant_total <= 0:
        return 1.0
    found = sum(1 for row in rows[:cutoff] if int(row["relevance"]) > 0)
    return found / float(relevant_total)


def _ndcg_at(rows: list[dict[str, object]], cutoff: int) -> float:
    actual = _dcg(int(row["relevance"]) for row in rows[:cutoff])
    ideal = _dcg(sorted((candidate.relevance for candidate in BENCHMARK_CANDIDATES if not candidate.expected_hard_filtered), reverse=True)[:cutoff])
    if ideal <= 0.0:
        return 1.0
    return actual / ideal


def _distance_gate_contract() -> dict[str, object]:
    facts = {"nearest_playground_m": 1200}
    soft_results = {
        mode: product_service._property_apply_distance_gate(
            dict(facts),
            request_preferences={
                "max_distance_to_playground_m": 100,
                "max_distance_to_playground_importance": mode,
            },
            preference_key="max_distance_to_playground_m",
            fact_key="nearest_playground_m",
            label="playground",
        )
        for mode in ("nice_to_have", "strong_wish", "avoid")
    }
    hard_result = product_service._property_apply_distance_gate(
        dict(facts),
        request_preferences={
            "max_distance_to_playground_m": 100,
            "max_distance_to_playground_importance": "must_have",
        },
        preference_key="max_distance_to_playground_m",
        fact_key="nearest_playground_m",
        label="playground",
    )
    return {"soft_modes_pass": soft_results, "hard_mode_pass": hard_result}


def build_benchmark_receipt() -> dict[str, object]:
    hard_rows = _ranked_rows(BENCHMARK_CANDIDATES, preferences=HARD_PREFERENCES)
    soft_rows = _ranked_rows(BENCHMARK_CANDIDATES, preferences=SOFT_PREFERENCES)
    hard_hitset = {str(row["candidate_ref"]) for row in hard_rows}
    soft_hitset = {str(row["candidate_ref"]) for row in soft_rows}
    filtered_refs = {
        candidate.candidate_ref
        for candidate in BENCHMARK_CANDIDATES
        if not _matches_location(candidate, preferences=HARD_PREFERENCES)
    }
    expected_filtered_refs = {
        candidate.candidate_ref
        for candidate in BENCHMARK_CANDIDATES
        if candidate.expected_hard_filtered
    }
    notifications = {
        str(row["candidate_ref"]): bool(row["notified"])
        for row in soft_rows
    }
    expected_notifications = {
        candidate.candidate_ref: candidate.expected_notified
        for candidate in BENCHMARK_CANDIDATES
        if not candidate.expected_hard_filtered
    }
    distance_gate = _distance_gate_contract()
    metrics = {
        "recall_at_20": round(_recall_at(soft_rows, BENCHMARK_CANDIDATES, 20), 4),
        "precision_at_5": round(_precision_at(soft_rows, 5), 4),
        "ndcg_at_10": round(_ndcg_at(soft_rows, 10), 4),
        "hard_filter_violation_count": len(filtered_refs.symmetric_difference(expected_filtered_refs)),
        "soft_filter_hitset_preserved": hard_hitset == soft_hitset,
        "top_candidate_ok": bool(soft_rows and soft_rows[0]["candidate_ref"] == "target-1010"),
        "low_score_notifications_suppressed": notifications == expected_notifications,
        "soft_distance_gates_score_only": all(bool(value) for value in dict(distance_gate["soft_modes_pass"]).values()),
        "hard_distance_gate_blocks": not bool(distance_gate["hard_mode_pass"]),
    }
    return {
        "status": "ok" if all(
            (
                metrics["recall_at_20"] >= 1.0,
                metrics["ndcg_at_10"] >= 0.95,
                metrics["hard_filter_violation_count"] == 0,
                metrics["soft_filter_hitset_preserved"],
                metrics["top_candidate_ok"],
                metrics["low_score_notifications_suppressed"],
                metrics["soft_distance_gates_score_only"],
                metrics["hard_distance_gate_blocks"],
            )
        ) else "failed",
        "contract": "propertyquarry.offline_ranking_benchmark.v1",
        "case_count": 1,
        "candidate_count": len(BENCHMARK_CANDIDATES),
        "metrics": metrics,
        "hard_hitset": sorted(hard_hitset),
        "soft_hitset": sorted(soft_hitset),
        "expected_hard_filtered": sorted(expected_filtered_refs),
        "actual_hard_filtered": sorted(filtered_refs),
        "ranked": soft_rows,
        "distance_gate": distance_gate,
    }


def main() -> int:
    receipt = build_benchmark_receipt()
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
