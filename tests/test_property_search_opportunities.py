from __future__ import annotations

from app.product.property_opportunities import (
    find_property_search_candidate,
    materialize_property_search_opportunities,
    property_opportunity_public_projection,
)
from app.product.property_search_storage import _compact_property_search_run_record
from app.repositories.preference_profiles import InMemoryPreferenceProfileRepository
from app.services.preference_profile_service import PreferenceProfileService


def _assessment(*, object_id: str) -> dict[str, object]:
    return {
        "assessment_id": f"assessment:{object_id}",
        "object_type": "listing",
        "object_id": object_id,
        "fit_score": 88.0,
        "confidence": 0.84,
        "predicted_reaction": "Likely shortlist if the heating system is confirmed.",
        "recommendation": "shortlist",
        "match_reasons_json": ["Layout matches the explicit room preference."],
        "mismatch_reasons_json": ["Heating type is not confirmed."],
        "unknowns_json": ["heating_type"],
        "blocking_constraints_json": [],
        "generated_at": "2026-08-12T12:00:00Z",
    }


def test_search_candidates_materialize_one_durable_opportunity_across_card_copies() -> None:
    top = {
        "candidate_ref": "listing-1",
        "title": "Quiet Vienna flat",
        "source_platform": "willhaben",
        "property_facts": {"rooms": 3, "has_floorplan": True},
    }
    research = dict(top)
    sources = [{"source_label": "Willhaben", "top_candidates": [top], "research_candidates": [research]}]
    calls: list[dict[str, object]] = []

    def assess(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return _assessment(object_id=str(kwargs["object_id"]))

    summary = materialize_property_search_opportunities(
        sources,
        principal_id="principal-1",
        person_id="self",
        run_id="run-1",
        assess=assess,
        search_preferences={"max_price_eur": 3500, "min_area_m2": 45},
    )

    assert len(calls) == 1
    assert calls[0]["domain"] == "willhaben"
    assert calls[0]["object_payload"]["has_floorplan"] is True
    assert calls[0]["object_payload"]["search_preferences"] == {
        "max_price_eur": 3500,
        "min_area_m2": 45,
    }
    assert summary == {
        "opportunity_total": 1,
        "opportunity_persistence_failed_total": 0,
        "opportunity_person_id": "self",
        "opportunity_generation_status": "ready",
    }
    for candidate in (top, research):
        assert candidate["opportunity_id"] == "assessment:listing-1"
        assert candidate["opportunity_status"] == "ready"
        assert candidate["preference_fit_score"] == 88.0
        assert candidate["opportunity_recommendation"] == "shortlist"
        assert candidate["opportunity"]["unknowns"] == ["heating_type"]


def test_opportunity_persistence_failure_is_visible_without_breaking_search() -> None:
    candidate = {
        "title": "A listing without a provider id",
        "property_url": "https://example.test/listing/secret-address",
        "source_platform": "immobilienscout24",
    }
    sources = [{"top_candidates": [candidate]}]

    def unavailable(**_kwargs: object) -> None:
        raise RuntimeError("database unavailable")

    summary = materialize_property_search_opportunities(
        sources,
        principal_id="principal-2",
        person_id="buyer",
        run_id="run-2",
        assess=unavailable,
    )

    assert summary["opportunity_total"] == 0
    assert summary["opportunity_persistence_failed_total"] == 1
    assert summary["opportunity_generation_status"] == "unavailable"
    opportunity = candidate["opportunity"]
    assert opportunity["status"] == "unavailable"
    assert opportunity["domain"] == "property"
    assert opportunity["object_id"].startswith("property-opportunity-")
    assert "secret-address" not in opportunity["object_id"]
    assert candidate["candidate_ref"] == opportunity["object_id"]


def test_public_opportunity_projection_allowlists_customer_safe_fields() -> None:
    projection = property_opportunity_public_projection(
        {
            "status": "ready",
            "opportunity_id": "assessment:1",
            "person_id": "elisabeth",
            "fit_score": "88",
            "unknowns": ["heating_type"],
            "private_prompt": "must not reach the browser",
        }
    )

    assert projection["person_id"] == "elisabeth"
    assert projection["fit_score"] == 88.0
    assert projection["unknowns"] == ["heating_type"]
    assert "private_prompt" not in projection


def test_candidate_lookup_accepts_one_legacy_url_encoded_layer() -> None:
    candidate = {
        "candidate_ref": "property-scout:1355793819",
        "title": "Vienna opportunity",
    }
    run = {"summary": {"sources": [{"top_candidates": [candidate]}]}}

    resolved = find_property_search_candidate(
        run,
        candidate_ref="property-scout%3A1355793819",
    )

    assert resolved == candidate


def test_candidate_lookup_prefers_exact_ref_before_decoded_fallback() -> None:
    encoded_candidate = {
        "candidate_ref": "property-scout%3Aencoded",
        "title": "Literal encoded reference",
    }
    decoded_candidate = {
        "candidate_ref": "property-scout:encoded",
        "title": "Decoded reference",
    }
    run = {
        "summary": {
            "sources": [
                {"top_candidates": [decoded_candidate]},
                {"top_candidates": [encoded_candidate]},
            ]
        }
    }

    resolved = find_property_search_candidate(
        run,
        candidate_ref="property-scout%3Aencoded",
    )

    assert resolved == encoded_candidate


def test_repeated_search_poll_upserts_one_run_scoped_opportunity() -> None:
    repo = InMemoryPreferenceProfileRepository()
    profiles = PreferenceProfileService(repo=repo)

    def assess(**kwargs: object) -> dict[str, object] | None:
        return profiles.assess_candidate(**kwargs)

    def sources() -> list[dict[str, object]]:
        return [
            {
                "top_candidates": [
                    {
                        "candidate_ref": "listing-poll-safe",
                        "title": "Stable opportunity",
                        "source_platform": "willhaben",
                    }
                ]
            }
        ]

    first_sources = sources()
    second_sources = sources()
    materialize_property_search_opportunities(
        first_sources,
        principal_id="principal-poll-safe",
        person_id="self",
        run_id="run-one",
        assess=assess,
    )
    materialize_property_search_opportunities(
        second_sources,
        principal_id="principal-poll-safe",
        person_id="self",
        run_id="run-one",
        assess=assess,
    )

    assessments = repo.list_decision_assessments(
        principal_id="principal-poll-safe",
        person_id="self",
        limit=20,
    )
    assert len(assessments) == 1
    first_opportunity = first_sources[0]["top_candidates"][0]["opportunity"]
    second_opportunity = second_sources[0]["top_candidates"][0]["opportunity"]
    assert first_opportunity["opportunity_id"] == second_opportunity["opportunity_id"]
    assert str(first_opportunity["opportunity_id"]).startswith("property_opportunity:")


def test_new_search_run_gets_a_distinct_opportunity_assessment() -> None:
    captured_ids: list[str] = []

    def assess(**kwargs: object) -> dict[str, object]:
        assessment_id = str(kwargs["assessment_id"])
        captured_ids.append(assessment_id)
        return _assessment(object_id=str(kwargs["object_id"])) | {"assessment_id": assessment_id}

    for run_id in ("run-one", "run-two"):
        materialize_property_search_opportunities(
            [{"top_candidates": [{"candidate_ref": "listing-1"}]}],
            principal_id="principal-1",
            person_id="self",
            run_id=run_id,
            assess=assess,
        )

    assert len(set(captured_ids)) == 2


def test_compact_search_storage_preserves_customer_opportunity_projection() -> None:
    opportunity = {
        "opportunity_id": "property_opportunity:stored",
        "status": "ready",
        "person_id": "self",
        "run_id": "run-compact-opportunity",
        "fit_score": 74.0,
        "confidence": 0.68,
        "recommendation": "shortlist",
        "match_reasons": ["The rent is within the active search ceiling."],
        "mismatch_reasons": [],
        "unknowns": ["Lift access is not confirmed."],
        "blocking_constraints": [],
    }
    compact = _compact_property_search_run_record(
        {
            "run_id": "run-compact-opportunity",
            "principal_id": "principal-compact-opportunity",
            "status": "processed",
            "summary": {
                "status": "processed",
                "fact_enrichment_jobs": {"listing-1": {"status": "queued"}},
                "opportunity_total": 1,
                "opportunity_persistence_failed_total": 0,
                "opportunity_person_id": "self",
                "opportunity_generation_status": "ready",
                "sources": [
                    {
                        "source_label": "Willhaben",
                        "top_candidates": [
                            {
                                "candidate_ref": "listing-1",
                                "title": "Compact opportunity",
                                "opportunity": opportunity,
                                "opportunity_id": opportunity["opportunity_id"],
                                "opportunity_status": "ready",
                                "preference_fit_score": 74.0,
                                "preference_confidence": 0.68,
                                "opportunity_recommendation": "shortlist",
                            }
                        ],
                    }
                ],
            },
        }
    )

    summary = compact["summary"]
    assert summary["opportunity_total"] == 1
    assert summary["opportunity_generation_status"] == "ready"
    candidate = summary["sources"][0]["top_candidates"][0]
    assert candidate["opportunity"] == opportunity
    assert candidate["opportunity_id"] == "property_opportunity:stored"
    assert candidate["opportunity_recommendation"] == "shortlist"
