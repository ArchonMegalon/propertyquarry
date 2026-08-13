from __future__ import annotations

from app.repositories.preference_profiles import InMemoryPreferenceProfileRepository
from app.services.preference_profile_service import PreferenceProfileService


def _service() -> PreferenceProfileService:
    return PreferenceProfileService(repo=InMemoryPreferenceProfileRepository())


def test_preference_profile_service_can_upsert_profile_and_node_bundle() -> None:
    service = _service()

    profile = service.ensure_profile(
        principal_id="pref-principal",
        person_id="self",
        display_name="Tibor",
        consent_mode="behavioral_learning",
        learning_enabled=True,
    )
    node = service.upsert_preference_node(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        category="constraint",
        key="max_total_rent_eur",
        value_json=2500,
        confidence=1.0,
    )
    bundle = service.get_profile_bundle(principal_id="pref-principal", person_id="self")

    assert profile["display_name"] == "Tibor"
    assert profile["learning_enabled"] is True
    assert node["key"] == "max_total_rent_eur"
    assert bundle["profile"]["person_id"] == "self"
    assert bundle["preference_nodes"][0]["key"] == "max_total_rent_eur"


def test_preference_profile_service_applies_correction_and_records_receipt() -> None:
    service = _service()

    applied = service.apply_correction(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        category="aversion",
        key="avoid_heating_types",
        value_json=["Gasheizung"],
        reason="Strong no for future screening",
        corrected_by="operator-1",
    )
    bundle = service.get_profile_bundle(principal_id="pref-principal", person_id="self")

    assert applied["node"]["source_mode"] == "explicit_correction"
    assert applied["node"]["confidence"] == 1.0
    assert applied["correction"]["reason"] == "Strong no for future screening"
    assert bundle["recent_corrections"][0]["corrected_by"] == "operator-1"


def test_preference_profile_service_archives_node_and_records_receipt() -> None:
    service = _service()
    node = service.upsert_preference_node(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        category="soft_preference",
        key="prefer_balcony",
        value_json=True,
        strength="medium",
        confidence=0.8,
    )

    archived = service.archive_preference_node(
        principal_id="pref-principal",
        person_id="self",
        node_id=str(node["node_id"]),
        reason="Outdoor space was over-weighted.",
        corrected_by="operator-1",
    )
    bundle = service.get_profile_bundle(principal_id="pref-principal", person_id="self")

    assert archived["node"]["status"] == "inactive"
    assert archived["node"]["source_mode"] == "explicit_correction"
    assert archived["correction"]["old_value_json"]["status"] == "active"
    assert archived["correction"]["new_value_json"]["status"] == "inactive"
    assert bundle["preference_nodes"][0]["status"] == "inactive"


def test_preference_profile_service_records_evidence_and_applies_preference_hints() -> None:
    service = _service()
    service.ensure_profile(
        principal_id="pref-principal",
        person_id="self",
        consent_mode="behavioral_learning",
        learning_enabled=True,
    )

    result = service.record_evidence_event(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        event_type="listing_shortlisted",
        object_type="listing",
        object_id="listing-1",
        interpreted_signal_json={
            "preference_hints": [
                {
                    "domain": "willhaben",
                    "category": "soft_preference",
                    "key": "preferred_areas",
                    "value_json": ["Waehring"],
                    "strength": "medium",
                    "merge_mode": "append_unique",
                }
            ]
        },
    )

    assert result["event"]["event_type"] == "listing_shortlisted"
    assert result["applied_nodes"][0]["key"] == "preferred_areas"
    assert result["applied_nodes"][0]["value_json"] == ["Waehring"]


def test_preference_profile_service_scores_willhaben_candidate_from_profile() -> None:
    service = _service()
    service.ensure_profile(
        principal_id="pref-principal",
        person_id="self",
        consent_mode="behavioral_learning",
        learning_enabled=True,
    )
    service.upsert_preference_node(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        category="constraint",
        key="require_floorplan",
        value_json=True,
        confidence=1.0,
    )
    service.upsert_preference_node(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        category="aversion",
        key="avoid_heating_types",
        value_json=["Gasheizung"],
        confidence=1.0,
    )
    assessment = service.assess_candidate(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        object_type="listing",
        object_id="listing-1",
        object_payload={
            "postal_name": "Waehring",
            "total_rent_eur": 2200.0,
            "rooms": 4.0,
            "area_sqm": 106.0,
            "heating": "Gasheizung",
            "floorplan_count": 1,
            "tour_media_mode": "panorama_360",
        },
        persist=False,
        require_existing_profile=True,
    )

    assert assessment is not None
    assert assessment["recommendation"] == "reject"
    assert any("Gasheizung" in entry for entry in assessment["mismatch_reasons_json"])
    assert assessment["blocking_constraints_json"] == []


def test_preference_profile_service_scores_air_conditioning_and_attic_preferences() -> None:
    service = _service()
    service.ensure_profile(
        principal_id="pref-principal",
        person_id="self",
        consent_mode="behavioral_learning",
        learning_enabled=True,
    )
    service.upsert_preference_node(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        category="soft_preference",
        key="prefer_air_conditioning",
        value_json=True,
        confidence=1.0,
    )
    service.upsert_preference_node(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        category="aversion",
        key="avoid_attic_apartment",
        value_json=True,
        confidence=1.0,
    )

    weak = service.assess_candidate(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        object_type="listing",
        object_id="listing-attic",
        object_payload={
            "title": "Dachgeschosswohnung ohne Klimaanlage",
            "dachgeschoss": True,
        },
        persist=False,
        require_existing_profile=True,
    )
    strong = service.assess_candidate(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        object_type="listing",
        object_id="listing-cool",
        object_payload={
            "title": "Ruhige Wohnung mit Klimaanlage",
        },
        persist=False,
        require_existing_profile=True,
    )

    assert weak is not None
    assert strong is not None
    assert weak["fit_score"] < strong["fit_score"]
    assert "Air conditioning is not confirmed." in weak["mismatch_reasons_json"]
    assert any("top-floor or attic" in reason for reason in weak["mismatch_reasons_json"])
    assert "Air conditioning or active cooling is mentioned." in strong["match_reasons_json"]


def test_preference_profile_service_uses_candidate_currency_in_rent_reasons() -> None:
    service = _service()
    service.ensure_profile(
        principal_id="pref-principal",
        person_id="self",
        consent_mode="behavioral_learning",
        learning_enabled=True,
    )
    service.upsert_preference_node(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        category="constraint",
        key="max_total_rent_eur",
        value_json=1200,
        confidence=1.0,
    )
    service.upsert_preference_node(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        category="soft_preference",
        key="prefer_lower_total_rent_eur",
        value_json=900,
        confidence=1.0,
    )

    assessment = service.assess_candidate(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        object_type="listing",
        object_id="uk-listing-1",
        object_payload={
            "country_code": "GB",
            "currency_code": "GBP",
            "postal_name": "London",
            "total_rent_eur": 1350.0,
            "rooms": 2.0,
            "area_sqm": 55.0,
        },
        persist=False,
        require_existing_profile=True,
    )

    assert assessment is not None
    copy = " ".join(
        list(assessment["blocking_constraints_json"])
        + list(assessment["match_reasons_json"])
        + list(assessment["mismatch_reasons_json"])
    )
    assert "GBP 1200" in copy
    assert "GBP 900" in copy
    assert "EUR 1200" not in copy
    assert "EUR 900" not in copy


def test_search_brief_produces_actionable_opportunity_without_learned_nodes() -> None:
    service = _service()

    assessment = service.assess_candidate(
        principal_id="search-brief-principal",
        person_id="self",
        domain="willhaben",
        object_type="listing",
        object_id="listing-search-brief",
        object_payload={
            "title": "Serviced Apartment with terrace",
            "postal_name": "1020 Wien",
            "source_postal_code": "1020",
            "total_rent_eur": 2680.0,
            "area_m2": 89.0,
            "rooms": 3,
            "has_floorplan": True,
            "has_360": False,
            "fact_requirement_plan": [
                {"key": "nearest_supermarket_m", "label": "Supermarket distance", "state": "unknown"},
            ],
            "search_preferences": {
                "max_price_eur": 3500,
                "min_area_m2": 45,
                "location_query": "1010 Vienna, 1020 Vienna",
                "keyword_preferences": {
                    "balcony": "nice_to_have",
                    "lift": "nice_to_have",
                },
            },
        },
        persist=False,
    )

    assert assessment is not None
    assert assessment["fit_score"] >= 68
    assert assessment["recommendation"] == "shortlist"
    assert assessment["confidence"] >= 0.6
    reasons = list(assessment["match_reasons_json"])
    assert any("within the EUR 3500 search ceiling" in reason for reason in reasons)
    assert any("89 m² living area" in reason for reason in reasons)
    assert any("1020 Wien is inside" in reason for reason in reasons)
    assert any("Balcony, terrace, or loggia" in reason for reason in reasons)
    assert any("floor plan" in reason for reason in reasons)
    assert "Lift access is not confirmed." in assessment["unknowns_json"]
    assert "Supermarket distance is not verified yet." in assessment["unknowns_json"]
    assert not any("360" in reason for reason in assessment["mismatch_reasons_json"])


def test_preference_profile_service_builds_teable_projection_rows() -> None:
    service = _service()
    service.ensure_profile(principal_id="pref-principal", person_id="self", display_name="Tibor")
    service.upsert_preference_node(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        category="soft_preference",
        key="preferred_areas",
        value_json=["Waehring"],
        confidence=0.8,
    )

    projection = service.build_teable_projection_records(principal_id="pref-principal", person_id="self")

    assert "preference_review_queue" in projection
    assert projection["preference_review_queue"][0]["display_name"] == "Tibor"
    assert projection["preference_review_queue"][0]["domain"] == "willhaben"


def test_preference_profile_service_uses_factual_neutral_copy_for_midrange_distance_and_lease_signals() -> None:
    service = _service()
    service.ensure_profile(
        principal_id="pref-principal",
        person_id="self",
        consent_mode="behavioral_learning",
        learning_enabled=True,
    )
    for key in (
        "prefer_subway_nearby",
        "prefer_supermarket_nearby",
        "prefer_pharmacy_nearby",
        "prefer_unlimited_lease",
    ):
        service.upsert_preference_node(
            principal_id="pref-principal",
            person_id="self",
            domain="willhaben",
            category="soft_preference",
            key=key,
            value_json=True,
            confidence=1.0,
        )

    assessment = service.assess_candidate(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        object_type="listing",
        object_id="listing-neutral-facts",
        object_payload={
            "nearest_subway_m": 900.0,
            "nearest_supermarket_m": 850.0,
            "nearest_pharmacy_m": 900.0,
            "lease_term_years_max": 7.0,
        },
        persist=False,
        require_existing_profile=True,
    )

    assert assessment is not None
    assert "Underground access is about 900 m away." in assessment["unknowns_json"]
    assert "Supermarket access is about 850 m away." in assessment["unknowns_json"]
    assert "Pharmacy access is about 900 m away." in assessment["unknowns_json"]
    assert "The lease runs about 7 years." in assessment["unknowns_json"]
    assert not any("needs verification" in item for item in assessment["unknowns_json"])


def test_preference_profile_service_names_confirmed_midrange_and_weaker_distance_signals() -> None:
    service = _service()
    service.ensure_profile(
        principal_id="pref-principal",
        person_id="self",
        consent_mode="behavioral_learning",
        learning_enabled=True,
    )
    for key in (
        "prefer_subway_nearby",
        "prefer_supermarket_nearby",
        "prefer_pharmacy_nearby",
    ):
        service.upsert_preference_node(
            principal_id="pref-principal",
            person_id="self",
            domain="willhaben",
            category="soft_preference",
            key=key,
            value_json=True,
            confidence=1.0,
        )

    assessment = service.assess_candidate(
        principal_id="pref-principal",
        person_id="self",
        domain="willhaben",
        object_type="listing",
        object_id="listing-named-distance-facts",
        object_payload={
            "nearest_subway_m": 900.0,
            "nearest_subway_name": "U2 Messe-Prater",
            "nearest_supermarket_m": 1300.0,
            "nearest_supermarket_name": "BILLA Praterstern",
            "nearest_pharmacy_m": 1400.0,
            "nearest_pharmacy_name": "Marien Apotheke",
        },
        persist=False,
        require_existing_profile=True,
    )

    assert assessment is not None
    assert "Underground access via U2 Messe-Prater is about 900 m away." in assessment["unknowns_json"]
    assert "Supermarket access via BILLA Praterstern is about 1300 m away, which is weaker than preferred." in assessment["mismatch_reasons_json"]
    assert "Pharmacy access via Marien Apotheke is about 1400 m away, which is weaker than preferred." in assessment["mismatch_reasons_json"]


def test_preference_profile_service_partial_profile_update_keeps_existing_flags() -> None:
    service = _service()

    first = service.ensure_profile(
        principal_id="pref-principal",
        person_id="self",
        display_name="Tibor",
        consent_mode="behavioral_learning",
        learning_enabled=True,
        high_stakes_domains_enabled=True,
    )
    second = service.ensure_profile(
        principal_id="pref-principal",
        person_id="self",
        display_name="Updated Tibor",
    )

    assert first["learning_enabled"] is True
    assert second["display_name"] == "Updated Tibor"
    assert second["learning_enabled"] is True
    assert second["high_stakes_domains_enabled"] is True
