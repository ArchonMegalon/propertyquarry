from __future__ import annotations

import urllib.parse

from app.services.property_market_catalog import (
    currency_code_for_country,
    currency_symbol_for_country,
    default_timezone_for_country,
    default_language_for_country,
    default_platforms_for_country,
    default_platforms_for_country_listing_mode,
    evidence_source_options,
    filter_selectable_property_platform_details,
    filter_selectable_property_platforms,
    generated_source_specs,
    country_options,
    is_customer_search_country_code,
    is_supported_country_code,
    language_label,
    listing_mode_label,
    normalize_property_search_preferences,
    normalize_country_code,
    property_type_label,
    property_type_options,
    provider_options,
    provider_governance,
    property_provider_search_ready,
    property_provider_for_platform,
    provider_quality_labels,
    provider_listing_markers_for_host,
    normalize_listing_mode,
    supported_currency_codes,
)


def test_provider_options_are_filtered_by_country() -> None:
    germany = provider_options(country_code="DE")
    austria = provider_options(country_code="AT")
    sweden = provider_options(country_code="SE")
    costa_rica = provider_options(country_code="CR")

    assert any(row["value"] == "immoscout_de" for row in germany)
    assert any(row["value"] == "core_portals_de" and row["family"] == "core_portal" for row in germany)
    assert any(row["value"] == "shared_housing_de" and row["family"] == "shared_housing" for row in germany)
    assert any(row["value"] == "corporate_landlords_de" and row["family"] == "corporate_landlord" for row in germany)
    assert any(row["value"] == "municipal_housing_de" and row["family"] == "municipal_housing" for row in germany)
    assert any(row["value"] == "cooperatives_de" and row["family"] == "cooperative" for row in germany)
    assert any(row["value"] == "new_build_de" and row["family"] == "developer_projects" for row in germany)
    assert any(row["value"] == "auctions_de" and row["family"] == "distressed_sales" for row in germany)
    assert any(row["value"] == "broker_direct_de" and row["family"] == "broker_direct" for row in germany)
    assert any(row["value"] == "furnished_relocation_de" and row["family"] == "furnished_relocation" for row in germany)
    assert any(row["value"] == "immowelt" for row in germany)
    assert any(row["value"] == "zvg_de" for row in germany)
    assert any(row["value"] == "wg_gesucht_de" and row["family"] == "shared_housing" for row in germany)
    assert any(row["value"] == "vonovia_de" and row["family"] == "corporate_landlord" for row in germany)
    assert any(row["value"] == "leg_wohnen_de" and row["family"] == "corporate_landlord" for row in germany)
    assert any(row["value"] == "degewo_berlin" and row["family"] == "municipal_housing" for row in germany)
    assert any(row["value"] == "portal_zvg_de" and row["family"] == "distressed_sales" for row in germany)
    assert any(row["value"] == "neubaukompass_de" and row["family"] == "developer_projects" for row in germany)
    assert any(row["value"] == "ohne_makler_de" and row["family"] == "broker_direct" for row in germany)
    assert any(row["value"] == "von_poll_de" and row["family"] == "broker_direct" for row in germany)
    assert any(row["value"] == "wohnungsboerse_de" and row["family"] == "core_portal" for row in germany)
    assert any(row["value"] == "genossenschaften_at" for row in austria)
    assert any(row["value"] == "wag_at" and row["family"] == "cooperative" for row in austria)
    assert any(row["value"] == "immobilien_net_at" and row["family"] == "marketplace" for row in austria)
    assert any(row["value"] == "ohne_makler_at" and row["family"] == "broker_direct" for row in austria)
    assert any(row["value"] == "sreal_at" and row["family"] == "broker_direct" for row in austria)
    assert any(row["value"] == "raiffeisen_immobilien_at" and row["family"] == "broker_direct" for row in austria)
    assert any(row["value"] == "wohnnet_at" and row["family"] == "marketplace" for row in austria)
    assert any(row["value"] == "keinmakler_at" and row["family"] == "broker_direct" for row in austria)
    assert any(row["value"] == "heimat_oesterreich_at" and row["family"] == "cooperative" for row in austria)
    assert any(row["value"] == "bwsg_at" and row["family"] == "cooperative" for row in austria)
    assert any(row["value"] == "arwag_at" and row["family"] == "developer_projects" for row in austria)
    assert any(row["value"] == "raiffeisen_wohnbau_at" and row["family"] == "developer_projects" for row in austria)
    assert any(row["value"] == "justiz_edikte_at" for row in austria)
    assert any(row["value"] == "kronofogden_auktionstorget_se" for row in sweden)
    assert any(row["value"] == "encuentra24_cr" for row in costa_rica)
    assert any(row["value"] == "re_cr_mls" and "MLS" in row["label"] for row in costa_rica)
    assert any(row["value"] == "desarrollos_cr" and row["family"] == "developer_projects" for row in costa_rica)
    assert any(row["value"] == "tierraverde_cr" and row["family"] == "developer_projects" for row in costa_rica)
    assert any(row["value"] == "properstar_cr" and row["family"] == "marketplace" for row in costa_rica)
    assert any(row["value"] == "century21_cr" and row["family"] == "broker_direct" for row in costa_rica)
    assert any(row["value"] == "remax_cr" and row["family"] == "broker_direct" for row in costa_rica)
    assert any(row["value"] == "theagency_cr" and row["family"] == "broker_direct" for row in costa_rica)
    assert any(row["value"] == "krain_cr" and row["family"] == "broker_direct" for row in costa_rica)
    assert all("Germany" in str(row.get("description") or "") for row in germany)
    assert all(str(row.get("floorplan_reliability") or "") for row in austria)
    assert all(str(row.get("filter_pushdown_strength") or "") for row in costa_rica)


def test_provider_options_expose_market_readiness_and_rights_governance() -> None:
    austria = provider_options(country_code="AT")
    willhaben = next(row for row in austria if row["value"] == "willhaben")
    community = next(row for row in austria if row["value"] == "community_signals_at")

    assert willhaben["market_readiness"] in {"experimental", "private_beta", "verified", "public"}
    assert willhaben["rights_review_status"] == "needs_review"
    rights = dict(willhaben["provider_rights"])
    assert rights["access_mode"] == "public_web"
    assert rights["public_packet_allowed"] is False
    assert rights["photo_republication_allowed"] is False
    assert rights["floorplan_republication_allowed"] is False
    assert rights["customer_packet_allowed"] is True
    assert rights["maximum_concurrency"] >= 1
    assert rights["requests_per_hour"] >= 1

    assert community["search_ready"] is False
    assert community["coming_soon"] is True
    assert community["market_readiness"] == "catalog_only"
    assert dict(community["provider_rights"])["public_packet_allowed"] is False


def test_provider_governance_fails_closed_for_unknown_provider() -> None:
    governance = provider_governance("not-a-provider")

    assert governance["market_readiness"] == "catalog_only"
    assert governance["access_mode"] == "unknown"
    assert governance["customer_packet_allowed"] is False
    assert governance["public_packet_allowed"] is False
    assert governance["maximum_concurrency"] == 0


def test_immobilien_net_is_integrated_as_browser_backed_austria_provider() -> None:
    provider = property_provider_for_platform("immobilien_net_at")
    assert provider is not None
    assert provider.country_code == "AT"
    assert property_provider_search_ready("immobilien_net_at") is True

    governance = provider_governance("immobilien_net_at")
    assert governance["access_mode"] == "browser_public_web"
    assert governance["browser_access_allowed"] is True
    assert governance["maximum_concurrency"] == 1

    rent_defaults = default_platforms_for_country_listing_mode("AT", "rent")
    buy_defaults = default_platforms_for_country_listing_mode("AT", "buy")
    assert "immobilien_net_at" in rent_defaults
    assert "immobilien_net_at" in buy_defaults

    markers = provider_listing_markers_for_host("www.immobilien.net")
    assert "/immobiliensuche/" in markers


def test_new_provider_research_batch_is_cataloged_with_explicit_governance() -> None:
    expected = {
        "ohne_makler_at": ("AT", "public_web", False),
        "sreal_at": ("AT", "public_web", False),
        "raiffeisen_immobilien_at": ("AT", "public_web", False),
        "wohnnet_at": ("AT", "public_web", False),
        "keinmakler_at": ("AT", "public_web", False),
        "wohnungsboerse_de": ("DE", "public_web", False),
        "properstar_cr": ("CR", "browser_public_web", True),
        "century21_cr": ("CR", "public_web", False),
        "remax_cr": ("CR", "public_web", False),
    }

    for key, (country_code, access_mode, browser_required) in expected.items():
        provider = property_provider_for_platform(key)
        assert provider is not None
        assert provider.country_code == country_code
        assert provider.search_urls
        assert property_provider_search_ready(key) is True

        governance = provider_governance(key)
        assert governance["access_mode"] == access_mode
        assert governance["browser_access_allowed"] is browser_required
        assert governance["maximum_concurrency"] >= 1
        assert governance["requests_per_hour"] >= 1


def test_normalize_property_search_preferences_defaults_country_and_language() -> None:
    payload = normalize_property_search_preferences({"location_query": "Berlin"})

    assert payload["country_code"] == "AT"
    assert payload["region_code"] == ""
    assert payload["language_code"] == "de"
    assert payload["search_goal"] == "home"
    assert payload["listing_mode"] == "rent"
    assert payload["property_type"] == ["any"]
    assert payload["alert_frequency"] == "daily"
    assert payload["alert_channels"] == ["telegram"]
    assert payload["search_agent_enabled"] is False
    assert payload["search_agent_duration_days"] == 30
    assert payload["search_agent_notification_limit"] == 5
    assert payload["search_agent_notification_period"] == "day"


def test_normalize_property_search_preferences_keeps_whatsapp_alert_channel() -> None:
    payload = normalize_property_search_preferences(
        {
            "alert_channels": ["whatsapp", "telegram", "unknown"],
        }
    )

    assert payload["alert_channels"] == ["whatsapp", "telegram"]


def test_normalize_property_search_preferences_filters_unselectable_providers() -> None:
    payload = normalize_property_search_preferences(
        {
            "country_code": "AT",
            "listing_mode": "rent",
            "selected_platforms": ["willhaben", "community_signals_at", "rightmove", "all"],
        }
    )

    assert payload["selected_platforms"] == ["willhaben"]
    assert payload["provider_selection_filter_applied"] is True
    assert payload["provider_selection_filter_removed"] == ["community_signals_at", "rightmove"]
    removed_details = {row["platform"]: row for row in payload["provider_selection_filter_removed_details"]}
    assert removed_details["community_signals_at"]["reason"] == "not_search_ready"
    assert removed_details["rightmove"]["reason"] == "wrong_country"


def test_normalize_property_search_preferences_defaults_to_at_and_drops_foreign_providers() -> None:
    payload = normalize_property_search_preferences(
        {
            "listing_mode": "rent",
            "location_query": "Vienna",
            "selected_platforms": ["realestate_au", "willhaben", "otodom"],
        }
    )

    assert payload["country_code"] == "AT"
    assert payload["selected_platforms"] == ["willhaben"]
    assert payload["provider_selection_filter_applied"] is True
    assert payload["provider_selection_filter_removed"] == ["realestate_au", "otodom"]
    removed_details = {row["platform"]: row for row in payload["provider_selection_filter_removed_details"]}
    assert removed_details["realestate_au"]["reason"] == "wrong_country"
    assert removed_details["realestate_au"]["provider_country_code"] == "AU"
    assert removed_details["otodom"]["reason"] == "wrong_country"
    assert removed_details["otodom"]["provider_country_code"] == "PL"


def test_filter_selectable_property_platforms_honors_mode_country_and_readiness() -> None:
    kept, removed = filter_selectable_property_platforms(
        ("willhaben", "community_signals_at", "rightmove", "corporate_landlords_de"),
        country_code="DE",
        listing_mode="buy",
    )

    assert kept == ()
    assert removed == ("willhaben", "community_signals_at", "rightmove", "corporate_landlords_de")
    kept_details, removed_details = filter_selectable_property_platform_details(
        ("willhaben", "community_signals_at", "rightmove", "corporate_landlords_de"),
        country_code="DE",
        listing_mode="buy",
    )
    assert kept_details == ()
    removal_reasons = {row["platform"]: row["reason"] for row in removed_details}
    assert removal_reasons == {
        "willhaben": "wrong_country",
        "community_signals_at": "wrong_country",
        "rightmove": "wrong_country",
        "corporate_landlords_de": "listing_mode_unsupported",
    }


def test_normalize_property_search_preferences_investment_goal_forces_buy_without_forcing_underwriting_on() -> None:
    payload = normalize_property_search_preferences(
        {
            "search_goal": "investment",
            "listing_mode": "rent",
            "investment_research_mode": "off",
            "investment_strategy": "cash_flow",
            "min_gross_yield_pct": "5",
            "investment_require_floorplan": "true",
            "enable_family_mode": "true",
            "enable_commute_research": "true",
        }
    )

    assert payload["search_goal"] == "investment"
    assert payload["listing_mode"] == "buy"
    assert payload["investment_research_mode"] == "off"
    assert payload["investment_strategy"] == "cash_flow"
    assert payload["min_gross_yield_pct"] == 5
    assert payload["investment_require_floorplan"] is True
    assert payload["enable_family_mode"] is False
    assert payload["enable_commute_research"] is False
    assert payload["require_floorplan"] is True
    assert payload["school_stage_preferences"] == []
    assert payload["require_school_evidence"] is False
    assert payload["preferred_reachability_modes"] == []
    assert "commute_destination" not in payload


def test_normalize_property_search_preferences_land_only_clears_dwelling_only_gates() -> None:
    payload = normalize_property_search_preferences(
        {
            "search_goal": "home",
            "listing_mode": "buy",
            "property_type": ["land"],
            "require_floorplan": True,
            "require_energy_certificate": True,
            "require_operating_cost_statement": True,
            "investment_require_floorplan": True,
            "require_barrier_free": True,
            "min_rooms": 4,
            "keywords": "lift, balcony, playground nearby",
            "avoid_keywords": "barrier-free, bright",
            "keyword_preferences": {
                "lift": "must_have",
                "balcony": "important",
                "playground nearby": "nice_to_have_1km",
            },
        }
    )

    assert payload["property_type"] == ["land"]
    assert payload["require_floorplan"] is False
    assert payload["require_energy_certificate"] is False
    assert payload["require_operating_cost_statement"] is False
    assert payload["investment_require_floorplan"] is False
    assert payload["require_barrier_free"] is False
    assert "min_rooms" not in payload
    assert payload["keywords"] == ""
    assert payload["avoid_keywords"] == ""
    assert payload["keyword_preferences"] == {"playground nearby": "nice_to_have_1km"}


def test_normalize_property_search_preferences_strips_soft_keyword_filters_from_discovery_keywords() -> None:
    payload = normalize_property_search_preferences(
        {
            "keywords": "balcony, terrace, bright, custom term",
            "avoid_keywords": "quiet, noisy street",
            "keyword_preferences": {
                "balcony": "nice_to_have",
                "terrace": "strong_wish",
                "bright": "must_have",
                "quiet": "avoid",
            },
        }
    )

    assert payload["keywords"] == "bright, custom term"
    assert payload["avoid_keywords"] == "quiet, noisy street"
    assert payload["keyword_preferences"] == {
        "balcony": "nice_to_have",
        "terrace": "strong_wish",
        "bright": "must_have",
        "quiet": "avoid",
    }


def test_normalize_property_search_preferences_promotes_attic_and_air_conditioning_soft_filters() -> None:
    payload = normalize_property_search_preferences(
        {
            "keywords": "Klimaanlage, custom term, Dachgeschoss",
            "avoid_keywords": "Dachgeschoßwohnung, noisy street",
            "keyword_preferences": {
                "Air conditioning": "important",
                "Dachgeschoßwohnung": "avoid",
            },
        }
    )

    assert payload["keyword_preferences"] == {
        "klimaanlage": "important",
        "dachgeschosswohnung": "avoid",
    }
    assert payload["prefer_air_conditioning"] is True
    assert payload["avoid_attic_apartment"] is True
    assert payload["prefer_attic_apartment"] is False
    assert payload["keywords"] == "custom term"
    assert payload["avoid_keywords"] == "Dachgeschoßwohnung, noisy street"


def test_normalize_property_search_preferences_land_only_drops_dwelling_soft_filters() -> None:
    payload = normalize_property_search_preferences(
        {
            "property_type": ["land"],
            "keywords": "Klimaanlage, Dachgeschoßwohnung, baugrund",
            "avoid_keywords": "Dachgeschoss, noisy street",
            "keyword_preferences": {
                "AC": "important",
                "attic apartment": "avoid",
                "baugrund": "must_have",
            },
        }
    )

    assert payload["keyword_preferences"] == {"baugrund": "must_have"}
    assert payload["keywords"] == "baugrund"
    assert payload["avoid_keywords"] == "noisy street"


def test_generated_source_specs_pushes_only_hard_keywords_to_provider_search() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "custom_keywords": "altbau",
            "keywords": "balcony, bright, quiet, altbau",
            "avoid_keywords": "quiet",
            "keyword_preferences": {
                "balcony": "nice_to_have",
                "bright": "must_have",
                "quiet": "avoid",
            },
        },
        selected_platforms=("willhaben",),
        principal_id="cf-email:person@example.test",
    )

    assert specs
    pushdown = dict(specs[0].get("provider_filter_pushdown") or {})
    requested = dict(pushdown.get("requested") or {})
    assert requested.get("keywords") == "bright, altbau"


def test_generated_source_specs_ignores_legacy_managed_soft_keywords_without_priority_map() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "keywords": "balcony, terrace, quiet, bright",
        },
        selected_platforms=("willhaben",),
        principal_id="cf-email:person@example.test",
    )

    assert specs
    pushdown = dict(specs[0].get("provider_filter_pushdown") or {})
    requested = dict(pushdown.get("requested") or {})
    assert requested.get("keywords") is None


def test_normalize_property_search_preferences_clamps_search_agent_controls() -> None:
    payload = normalize_property_search_preferences(
        {
            "search_agent_enabled": "on",
            "search_agent_duration_days": 999,
            "search_agent_notification_limit": 999,
            "search_agent_notification_period": "week",
        }
    )

    assert payload["search_agent_enabled"] is True
    assert payload["search_agent_duration_days"] == 365
    assert payload["search_agent_notification_limit"] == 50
    assert payload["search_agent_notification_period"] == "week"


def test_normalize_property_search_preferences_drops_removed_match_bar() -> None:
    payload = normalize_property_search_preferences(
        {
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "Vienna",
            "min_match_score": 0,
        }
    )

    assert "min_match_score" not in payload


def test_normalize_property_search_preferences_scopes_full_region_backend_runs() -> None:
    payload = normalize_property_search_preferences(
        {
            "country_code": "AT",
            "region_code": "vienna",
            "full_region_scope": "true",
            "location_query": "",
            "selected_location_values": ["1010 Vienna", "1020 Vienna"],
        }
    )

    assert payload["full_region_scope"] is True
    assert payload["location_query"] == "Vienna"
    assert payload["selected_location_values"] == []


def test_generated_source_specs_do_not_expand_selected_districts_without_radius() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "search_mode": "discovery",
            "location_query": "1010 Vienna, 1020 Vienna",
            "selected_location_values": ["1010 Vienna", "1020 Vienna"],
        },
        selected_platforms=("ohne_makler_at",),
        principal_id="cf-email:person@example.test",
    )

    assert [row["location_query"] for row in specs] == ["1010 Vienna", "1020 Vienna"]


def test_generated_source_specs_expand_selected_districts_only_with_explicit_radius() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "search_mode": "discovery",
            "location_query": "1010 Vienna",
            "selected_location_values": ["1010 Vienna"],
            "adjacent_area_radius_m": 850,
        },
        selected_platforms=("ohne_makler_at",),
        principal_id="cf-email:person@example.test",
    )

    locations = [str(row["location_query"]) for row in specs]
    assert locations[0] == "1010 Vienna"
    assert len(locations) > 1


def test_generated_source_specs_marks_weak_austrian_location_queries_as_attempted() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "1010 Vienna",
            "selected_location_values": ["1010 Vienna"],
        },
        selected_platforms=("ohne_makler_at",),
        principal_id="cf-email:person@example.test",
    )

    assert specs
    pushdown = dict(specs[0].get("provider_filter_pushdown") or {})
    assert dict(pushdown.get("attempted") or {}).get("location_query") == "1010 Vienna"
    assert dict(pushdown.get("applied") or {}).get("location_query") is None
    assert "location_query" in list(pushdown.get("post_filter_only") or [])


def test_normalize_property_search_preferences_drops_stale_austrian_postal_codes_for_foreign_searches() -> None:
    payload = normalize_property_search_preferences(
        {
            "country_code": "CR",
            "region_code": "puntarenas",
            "location_query": "Monteverde, 1116",
            "custom_location_query": "1116",
        }
    )

    assert payload["country_code"] == "CR"
    assert payload["location_query"] == "Monteverde"
    assert payload["custom_location_query"] == ""


def test_normalize_property_search_preferences_drops_invalid_vienna_postal_stub() -> None:
    payload = normalize_property_search_preferences(
        {
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "1020 Vienna, 1116, 1180 Vienna",
            "custom_location_query": "1116",
        }
    )

    assert payload["location_query"] == "1020 Vienna, 1180 Vienna"
    assert payload["custom_location_query"] == ""


def test_normalize_listing_mode_accepts_sale_aliases() -> None:
    assert normalize_listing_mode("sale") == "buy"
    assert normalize_listing_mode("for-sale") == "buy"


def test_property_type_options_include_office_as_separate_category() -> None:
    values = {row["value"]: row["label"] for row in property_type_options()}

    assert values["apartment"] == "Apartment"
    assert values["office"] == "Office"
    assert values["office"] != values["apartment"]
    assert property_type_label("büro") == "Office"


def test_country_normalization_understands_common_country_names() -> None:
    assert normalize_country_code("Germany") == "DE"
    assert normalize_country_code("GB") == "UK"
    assert normalize_country_code("Great Britain") == "UK"
    assert normalize_country_code("United States") == "US"
    assert normalize_country_code("Costa Rica") == "CR"
    assert is_supported_country_code("Spain") is True
    assert is_supported_country_code("NO") is False


def test_customer_country_options_are_limited_to_presentation_markets() -> None:
    options = country_options()
    values = [row["value"] for row in options]

    assert values == ["AT"]
    assert is_customer_search_country_code("Austria") is True
    assert is_customer_search_country_code("Germany") is False
    assert is_customer_search_country_code("Costa Rica") is False
    assert is_customer_search_country_code("UK") is False
    assert is_customer_search_country_code("AU") is False
    assert is_customer_search_country_code("PL") is False


def test_generated_source_specs_use_country_platform_defaults() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "DE",
            "language_code": "de",
            "listing_mode": "buy",
            "location_query": "Berlin",
            "keywords": "lift balcony",
        },
        selected_platforms=(),
        principal_id="exec-property-de",
        default_person_id="self",
        max_results=4,
    )

    assert tuple(row["platform"] for row in specs[:2])[:1]
    assert specs[0]["country_code"] == "DE"
    assert specs[0]["listing_mode"] == "buy"
    assert "Berlin" in str(specs[0]["label"])
    assert specs[0]["provider_quality"]["floorplan_reliability"]
    assert specs[0]["provider_quality"]["filter_pushdown_strength"]


def test_provider_quality_labels_are_available_for_ui_and_ranking() -> None:
    quality = provider_quality_labels("willhaben")

    assert quality["coverage"]
    assert quality["floorplan_reliability"]
    assert quality["duplicate_rate"]
    assert quality["tour_availability"]
    assert quality["scan_reliability"]
    assert quality["filter_pushdown_strength"]
    assert quality["official_source_quality"]
    assert quality["last_verified"]


def test_new_german_provider_quality_labels_are_specific_enough_for_ranking() -> None:
    developer = provider_quality_labels("neubaukompass_de")
    private_direct = provider_quality_labels("ohne_makler_de")
    broker = provider_quality_labels("von_poll_de")
    landlord = provider_quality_labels("vonovia_de")
    municipal = provider_quality_labels("degewo_berlin")

    assert developer["coverage"] == "national"
    assert developer["official_source_quality"] == "developer_primary"
    assert private_direct["coverage"] == "national"
    assert private_direct["official_source_quality"] == "owner_direct"
    assert broker["coverage"] == "multi_region"
    assert broker["official_source_quality"] == "broker_primary"
    assert landlord["official_source_quality"] == "landlord_primary"
    assert municipal["official_source_quality"] == "municipal_primary"


def test_germany_provider_catalog_groups_by_family() -> None:
    germany = provider_options(country_code="DE")
    families = {(row["value"], row["family"]) for row in germany}

    assert ("core_portals_de", "core_portal") in families
    assert ("wohnungsboerse_de", "core_portal") in families
    assert ("shared_housing_de", "shared_housing") in families
    assert ("corporate_landlords_de", "corporate_landlord") in families
    assert ("municipal_housing_de", "municipal_housing") in families
    assert ("cooperatives_de", "cooperative") in families
    assert ("new_build_de", "developer_projects") in families
    assert ("auctions_de", "distressed_sales") in families
    assert ("broker_direct_de", "broker_direct") in families
    assert ("furnished_relocation_de", "furnished_relocation") in families


def test_germany_official_sources_are_evidence_not_listing_providers() -> None:
    sources = evidence_source_options(country_code="DE")

    assert any(row["value"] == "uba_noise_de" and row["evidence_family"] == "noise" for row in sources)
    assert any(row["value"] == "uba_air_de" and row["evidence_family"] == "air_quality" for row in sources)
    assert any(row["value"] == "school_directories_de" and row["evidence_family"] == "school_evidence" for row in sources)
    assert all("Germany" in str(row.get("description") or "") for row in sources)
    assert all("official_public_data" != row["evidence_family"] for row in sources)
    assert all(str(row.get("license_label") or "").strip() for row in sources)
    assert all(str(row.get("refresh_cadence") or "").strip() for row in sources)
    assert all(str(row.get("downstream_use") or "").strip() for row in sources)
    assert all(str(row.get("geographic_granularity") or "").strip() for row in sources)

    osm = next(row for row in sources if row["value"] == "osm_overpass_de")
    assert osm["attribution_required"] is True
    assert "OpenStreetMap" in str(osm["license_label"])
    assert osm["downstream_use"] == "derived_proximity_evidence_with_attribution"


def test_school_quality_is_called_school_evidence_not_quality_score() -> None:
    sources = evidence_source_options(country_code="DE")
    school = next(row for row in sources if row["value"] == "school_directories_de")

    assert "school evidence" in str(school["description"]).lower()
    assert "quality score" not in str(school["description"]).lower()


def test_new_austrian_provider_quality_labels_are_specific_enough_for_ranking() -> None:
    coop = provider_quality_labels("wag_at")
    developer = provider_quality_labels("arwag_at")

    assert coop["coverage"] in {"regional", "multi_region", "national"}
    assert coop["floorplan_reliability"] == "medium"
    assert coop["official_source_quality"] == "cooperative_primary"
    assert developer["coverage"] in {"regional", "multi_region", "national"}
    assert developer["floorplan_reliability"] == "medium"
    assert developer["official_source_quality"] == "developer_primary"


def test_austria_provider_catalog_groups_by_family() -> None:
    austria = provider_options(country_code="AT")
    families = {(row["value"], row["family"]) for row in austria}

    assert ("public_housing_at", "public_housing") in families
    assert ("genossenschaften_at", "cooperative") in families
    assert ("developer_projects_at", "developer_projects") in families
    assert ("distressed_sales_at", "distressed_sales") in families
    assert ("ohne_makler_at", "broker_direct") in families
    assert ("sreal_at", "broker_direct") in families
    assert ("raiffeisen_immobilien_at", "broker_direct") in families
    assert ("wohnnet_at", "marketplace") in families
    assert ("keinmakler_at", "broker_direct") in families
    assert ("wohnberatung_wien", "public_housing") in families
    assert ("wiener_wohnen", "public_housing") in families
    assert ("gesiba_at", "cooperative") in families
    assert ("oesw_at", "cooperative") in families
    assert ("egw_at", "cooperative") in families
    assert ("zvginfo_at", "distressed_sales") in families


def test_austria_official_sources_are_evidence_not_listing_providers() -> None:
    sources = evidence_source_options(country_code="AT")

    assert any(row["value"] == "hora_at" and row["evidence_family"] == "natural_hazards" for row in sources)
    assert any(row["value"] == "laerminfo_at" and row["evidence_family"] == "noise" for row in sources)
    assert any(row["value"] == "uba_luft_at" and row["evidence_family"] == "air_quality" for row in sources)
    assert any(row["value"] == "statatlas_schulen_at" and row["evidence_family"] == "school_evidence" for row in sources)
    assert any(row["value"] == "breitbandatlas_at" and row["evidence_family"] == "broadband" for row in sources)
    assert any(row["value"] == "wien_klimaanalyse_ogd_at" and row["evidence_family"] == "climate" for row in sources)
    assert all("Austria" in str(row.get("description") or "") for row in sources)


def test_austria_school_filters_use_school_evidence_not_quality_score() -> None:
    sources = evidence_source_options(country_code="AT")
    school = next(row for row in sources if row["value"] == "statatlas_schulen_at")

    assert "school evidence" in str(school["description"]).lower()
    assert "quality score" not in str(school["description"]).lower()


def test_austria_default_platforms_follow_listing_mode() -> None:
    rent_defaults = default_platforms_for_country_listing_mode("AT", "rent")
    buy_defaults = default_platforms_for_country_listing_mode("AT", "buy")
    land_defaults = default_platforms_for_country_listing_mode("AT", "buy", property_type="land")

    assert "public_housing_at" in rent_defaults
    assert "genossenschaften_at" in rent_defaults
    assert "developer_projects_at" in buy_defaults
    assert "broker_direct_at" in buy_defaults
    assert "broker_direct_at" in land_defaults


def test_germany_buy_defaults_drop_dead_corporate_landlord_lane() -> None:
    buy_defaults = default_platforms_for_country_listing_mode("DE", "buy")

    assert buy_defaults == ("core_portals_de", "wohnungsboerse_de", "new_build_de", "broker_direct_de")
    assert "corporate_landlords_de" not in buy_defaults
    assert all(property_provider_search_ready(platform) for platform in buy_defaults)


def test_germany_buy_provider_markers_only_accept_real_listing_routes() -> None:
    ohne_makler = property_provider_for_platform("ohne_makler_de")
    neubau = property_provider_for_platform("neubaukompass_de")
    broker_direct = property_provider_for_platform("broker_direct_de")

    assert ohne_makler is not None
    assert neubau is not None
    assert broker_direct is not None
    assert ohne_makler.listing_path_markers == ("/immobilie/",)
    assert neubau.listing_path_markers == ("/property/",)
    assert "/immobilie/" in broker_direct.listing_path_markers


def test_generated_source_specs_support_distressed_sale_platforms() -> None:
    sweden_specs = generated_source_specs(
        preferences={
            "country_code": "SE",
            "language_code": "sv",
            "listing_mode": "buy",
            "location_query": "Stockholm",
            "keywords": "auction repossessed",
        },
        selected_platforms=("kronofogden_auktionstorget_se", "treasury_real_property_us"),
        principal_id="exec-property-auctions",
        default_person_id="self",
        max_results=3,
    )
    us_specs = generated_source_specs(
        preferences={
            "country_code": "US",
            "language_code": "en",
            "listing_mode": "buy",
            "location_query": "Florida",
        },
        selected_platforms=("treasury_real_property_us",),
        principal_id="exec-property-auctions-us",
        default_person_id="self",
        max_results=3,
    )

    assert any(str(row["platform"]) == "kronofogden_auktionstorget_se" for row in sweden_specs)
    assert any("kronofogden" in str(row["url"]).lower() for row in sweden_specs)
    assert any(str(row["platform"]) == "treasury_real_property_us" for row in us_specs)
    assert any("treasury.gov" in str(row["url"]).lower() for row in us_specs)


def test_market_labels_are_human_readable() -> None:
    assert language_label("fr", country_code="FR") == "Français"
    assert listing_mode_label("buy") == "Buy"
    assert property_type_label("house") == "House"
    assert property_type_label("office") == "Office"
    assert property_type_label("land") == "Building land"


def test_generated_source_specs_support_austrian_land_searches() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "buy",
            "property_type": "land",
            "location_query": "Niederösterreich",
            "keywords": "baugrund seezugang",
        },
        selected_platforms=("willhaben",),
        principal_id="exec-property-land-at",
        default_person_id="self",
        max_results=4,
    )

    assert specs
    assert specs[0]["provider_filter_pushdown"]["requested"]["property_type"] == "land"
    assert "grundstuecke" in str(specs[0]["url"])
    assert "Nieder" in urllib.parse.unquote(str(specs[0]["url"]))
    assert "seezugang" in urllib.parse.unquote(str(specs[0]["url"])).lower()


def test_generated_source_specs_support_austrian_office_searches() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "office",
            "location_query": "Wien",
            "keywords": "büro praxis",
        },
        selected_platforms=("willhaben",),
        principal_id="exec-property-office-at",
        default_person_id="self",
        max_results=4,
    )

    assert specs
    assert specs[0]["provider_filter_pushdown"]["requested"]["property_type"] == "office"
    decoded_url = urllib.parse.unquote(str(specs[0]["url"])).lower()
    assert "gewerbeimmobilien" in decoded_url
    assert "büro" in decoded_url


def test_generated_source_specs_include_new_austrian_cooperative_providers() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "keywords": "gefördert",
        },
        selected_platforms=("wag_at", "heimat_oesterreich_at", "bwsg_at", "wiensued_at"),
        principal_id="exec-property-at-coops",
        default_person_id="self",
        max_results=4,
    )

    by_platform = {row["platform"]: row for row in specs}
    assert set(by_platform) == {"wag_at", "heimat_oesterreich_at", "bwsg_at", "wiensued_at"}
    assert "wag.at" in by_platform["wag_at"]["url"]
    assert "heimat-oesterreich.at" in by_platform["heimat_oesterreich_at"]["url"]
    assert "bwsg.at" in by_platform["bwsg_at"]["url"]
    assert "wiensued.at" in by_platform["wiensued_at"]["url"]
    assert all(row["provider_family"] == "cooperative" for row in by_platform.values())
    assert all("q=Wien+gef" in str(row["url"]) for row in by_platform.values())


def test_generated_source_specs_include_new_austrian_developer_providers() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "buy",
            "location_query": "Wien",
            "keywords": "neubau projekt",
        },
        selected_platforms=("arwag_at", "raiffeisen_wohnbau_at", "leitgoeb_wohnbau_at", "viktoria_wohnbau_at"),
        principal_id="exec-property-at-developers",
        default_person_id="self",
        max_results=4,
    )

    by_platform = {row["platform"]: row for row in specs}
    assert set(by_platform) == {"raiffeisen_wohnbau_at", "leitgoeb_wohnbau_at", "viktoria_wohnbau_at"}
    assert "raiffeisen-wohnbau.at" in by_platform["raiffeisen_wohnbau_at"]["url"]
    assert "leitgoeb-wohnbau.at" in by_platform["leitgoeb_wohnbau_at"]["url"]
    assert "viktoria-wohnbau.at" in by_platform["viktoria_wohnbau_at"]["url"]
    assert all(row["provider_family"] == "developer_projects" for row in by_platform.values())
    assert all("q=Wien+neubau+projekt" in str(row["url"]) for row in by_platform.values())


def test_generated_source_specs_skip_buy_only_sources_for_rent_without_distress_signal() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
        },
        selected_platforms=("justiz_edikte_at",),
        principal_id="exec-property-rent-no-justiz",
        default_person_id="self",
        max_results=4,
    )
    distress_specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "include_distressed_sale_signals": True,
        },
        selected_platforms=("justiz_edikte_at",),
        principal_id="exec-property-rent-with-justiz",
        default_person_id="self",
        max_results=4,
    )

    assert specs == ()
    assert distress_specs
    assert distress_specs[0]["listing_mode"] == "buy"


def test_generated_source_specs_build_provider_specific_market_urls() -> None:
    uk_specs = generated_source_specs(
        preferences={
            "country_code": "UK",
            "language_code": "en",
            "listing_mode": "buy",
            "location_query": "London",
            "min_rooms": 3,
            "max_price_eur": 950000,
        },
        selected_platforms=("rightmove", "zoopla"),
        principal_id="exec-property-uk",
        default_person_id="self",
        max_results=3,
    )
    france_specs = generated_source_specs(
        preferences={
            "country_code": "FR",
            "language_code": "fr",
            "listing_mode": "rent",
            "location_query": "Paris",
            "min_rooms": 2,
        },
        selected_platforms=("bienici",),
        principal_id="exec-property-fr",
        default_person_id="self",
        max_results=3,
    )
    netherlands_specs = generated_source_specs(
        preferences={
            "country_code": "NL",
            "language_code": "nl",
            "listing_mode": "buy",
            "location_query": "Amsterdam",
            "property_type": "house",
        },
        selected_platforms=("funda",),
        principal_id="exec-property-nl",
        default_person_id="self",
        max_results=3,
    )

    assert "searchLocation=London" in str(uk_specs[0]["url"])
    assert "q=London" in str(uk_specs[1]["url"])
    assert "price_max=950000" in str(uk_specs[1]["url"])
    assert "bienici.com/recherche/location/paris" in str(france_specs[0]["url"]).lower()
    assert "minRooms=2" in str(france_specs[0]["url"])
    assert "funda.nl/zoeken/koop/amsterdam/" in str(netherlands_specs[0]["url"]).lower()
    assert "object_type=house" in str(netherlands_specs[0]["url"])


def test_default_platforms_for_country_are_stable() -> None:
    assert default_platforms_for_country("UK") == ("rightmove", "zoopla", "onthemarket")
    assert default_platforms_for_country("PT") == ("idealista_pt", "imovirtual", "casa_sapo")
    assert default_platforms_for_country("CR") == (
        "encuentra24_cr",
        "re_cr_mls",
        "properstar_cr",
    )
    assert default_platforms_for_country("AT") == (
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
        "public_housing_at",
        "genossenschaften_at",
    )
    assert default_platforms_for_country("DE") == (
        "core_portals_de",
        "wohnungsboerse_de",
        "corporate_landlords_de",
        "municipal_housing_de",
        "broker_direct_de",
    )
    assert default_language_for_country("SE") == "sv"
    assert default_language_for_country("CR") == "es"
    assert currency_code_for_country("UK") == "GBP"
    assert currency_symbol_for_country("US") == "USD"
    assert default_timezone_for_country("UK") == "Europe/London"
    assert default_timezone_for_country("US") == "America/New_York"
    assert currency_code_for_country("not-a-country") == "EUR"
    assert default_timezone_for_country("not-a-country") == "Europe/Vienna"
    assert {"EUR", "GBP", "USD", "CAD", "AUD", "CRC", "SEK", "PLN", "CHF"}.issubset(set(supported_currency_codes()))


def test_workspace_location_options_follow_supported_country_codes() -> None:
    from app.api.routes.landing_view_models import _property_location_options, _property_region_options

    assert any(row["value"] == "London" for row in _property_location_options("UK"))
    assert any(row["value"] == "London" for row in _property_location_options("GB"))
    assert any(row["value"] == "costa_rica" for row in _property_region_options("CR"))
    assert any(row["value"] == "Costa Rica" for row in _property_location_options("CR", "costa_rica"))
    assert any(row["value"] == "Tamarindo" for row in _property_location_options("CR", "guanacaste"))
    assert any(row["value"] == "Monteverde" for row in _property_location_options("CR", "costa_rica"))
    assert any(row["value"] == "Monteverde" for row in _property_location_options("CR", "puntarenas"))
    assert any(row["value"] == "Santa Elena" for row in _property_location_options("CR", "puntarenas"))
    assert any(row["value"] == "Puerto Viejo" for row in _property_location_options("CR", "caribbean"))
    assert any(row["value"] == "canada" for row in _property_region_options("CA"))
    assert any(row["value"] == "Toronto" for row in _property_location_options("CA", "canada"))
    assert any(row["value"] == "Switzerland" for row in _property_location_options("CH", "switzerland"))


def test_workspace_preference_schema_matches_backend_categories() -> None:
    from app.api.routes.landing_view_models import _property_preference_schema

    schema = _property_preference_schema()
    categories = schema["categories"]
    constraint_keys = {row["key"] for row in categories["constraint"]["keys"]}
    soft_keys = {row["key"] for row in categories["soft_preference"]["keys"]}
    aversion_keys = {row["key"] for row in categories["aversion"]["keys"]}

    assert "require_lift" in constraint_keys
    assert "require_lift" not in soft_keys
    assert "prefer_lift" in soft_keys
    assert "avoided_areas" in aversion_keys


def test_provider_listing_markers_follow_provider_host() -> None:
    markers = provider_listing_markers_for_host("www.realtor.com")

    assert "/realestateandhomes-detail/" in markers


def test_generated_source_specs_cover_new_country_bundles() -> None:
    portugal_specs = generated_source_specs(
        preferences={
            "country_code": "PT",
            "language_code": "pt",
            "listing_mode": "buy",
            "location_query": "Lisbon",
        },
        selected_platforms=("idealista_pt", "imovirtual"),
        principal_id="exec-property-pt",
        default_person_id="self",
        max_results=3,
    )
    ireland_specs = generated_source_specs(
        preferences={
            "country_code": "IE",
            "language_code": "en",
            "listing_mode": "rent",
            "location_query": "Dublin",
        },
        selected_platforms=("daft_ie",),
        principal_id="exec-property-ie",
        default_person_id="self",
        max_results=3,
    )
    australia_specs = generated_source_specs(
        preferences={
            "country_code": "AU",
            "language_code": "en",
            "listing_mode": "buy",
            "location_query": "Sydney",
            "min_rooms": 2,
        },
        selected_platforms=("domain_au",),
        principal_id="exec-property-au",
        default_person_id="self",
        max_results=3,
    )

    assert "idealista.pt/en/comprar-casas/lisbon/" in str(portugal_specs[0]["url"]).lower()
    assert "imovirtual.com" in str(portugal_specs[1]["url"]).lower()
    assert "daft.ie/property-for-rent/dublin" in str(ireland_specs[0]["url"]).lower()
    assert "domain.com.au" in str(australia_specs[0]["url"]).lower()
    assert "suburb=Sydney" in str(australia_specs[0]["url"])


def test_generated_source_specs_cover_costa_rica_providers() -> None:
    rent_specs = generated_source_specs(
        preferences={
            "country_code": "CR",
            "language_code": "es",
            "listing_mode": "rent",
            "location_query": "Escazú",
            "keywords": "condo seguridad",
            "min_area_m2": 80,
        },
        selected_platforms=(),
        principal_id="exec-property-cr",
        default_person_id="self",
        max_results=4,
    )
    buy_specs = generated_source_specs(
        preferences={
            "country_code": "CR",
            "language_code": "es",
            "listing_mode": "buy",
            "location_query": "Tamarindo",
            "keywords": "beach house",
        },
        selected_platforms=("encuentra24", "recr", "realtorcr", "coldwellbankercr"),
        principal_id="exec-property-cr-buy",
        default_person_id="self",
        max_results=4,
    )

    assert {row["platform"] for row in rent_specs} == {"encuentra24_cr", "re_cr_mls", "properstar_cr"}
    assert any("encuentra24.com/costa-rica-en/real-estate-for-rent" in str(row["url"]) for row in rent_specs)
    assert any("properstar.com/costa-rica/rent" in str(row["url"]) for row in rent_specs)
    assert any("Escaz" in str(row["label"]) for row in rent_specs)
    assert {row["platform"] for row in buy_specs} == {"encuentra24_cr", "re_cr_mls", "realtor_cr", "coldwellbanker_cr"}
    assert any("realtor.com/international/cr" in str(row["url"]) for row in buy_specs)
    assert all(row["country_code"] == "CR" for row in [*rent_specs, *buy_specs])


def test_generated_source_specs_allow_countrywide_costa_rica_without_target_area() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "CR",
            "language_code": "es",
            "listing_mode": "buy",
            "property_type": "land",
            "location_query": "",
            "keywords": "beach access",
        },
        selected_platforms=(),
        principal_id="exec-property-cr-countrywide",
        default_person_id="self",
        max_results=4,
    )

    assert {row["platform"] for row in specs} == {
        "encuentra24_cr",
        "re_cr_mls",
        "realtor_cr",
        "properstar_cr",
        "coldwellbanker_cr",
        "century21_cr",
        "remax_cr",
        "theagency_cr",
        "krain_cr",
        "desarrollos_cr",
        "propertiesincostarica_cr",
        "twocostaricarealestate_cr",
        "tierraverde_cr",
        "costaricarealestateservice_cr",
    }
    assert all(row["location_query"] == "" for row in specs)
    assert all(row["country_code"] == "CR" for row in specs)
    assert any("beach+access" in str(row["url"]) or "q=beach" in str(row["url"]) for row in specs)


def test_generated_source_specs_drop_stale_austrian_postal_code_from_costa_rica_urls() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "CR",
            "region_code": "puntarenas",
            "language_code": "es",
            "listing_mode": "buy",
            "location_query": "Monteverde, 1116",
            "custom_location_query": "1116",
        },
        selected_platforms=("encuentra24_cr",),
        principal_id="exec-property-cr-clean-location",
        default_person_id="self",
        max_results=3,
    )

    assert len(specs) == 1
    assert specs[0]["location_query"] == "Monteverde"
    assert "q=Monteverde" in str(specs[0]["url"])
    assert "1116" not in str(specs[0]["url"])


def test_generated_source_specs_include_new_costa_rica_broker_direct_providers() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "CR",
            "region_code": "puntarenas",
            "language_code": "es",
            "listing_mode": "buy",
            "location_query": "Monteverde",
            "keywords": "cloud forest",
        },
        selected_platforms=(
            "propertiesincostarica_cr",
            "costaricarealestateservice_cr",
            "twocostaricarealestate_cr",
            "theagency_cr",
            "krain_cr",
            "century21_cr",
            "remax_cr",
        ),
        principal_id="exec-property-cr-broker-direct",
        default_person_id="self",
        max_results=3,
    )

    by_platform = {row["platform"]: row for row in specs}
    assert set(by_platform) == {
        "propertiesincostarica_cr",
        "costaricarealestateservice_cr",
        "twocostaricarealestate_cr",
        "theagency_cr",
        "krain_cr",
        "century21_cr",
        "remax_cr",
    }
    assert "propertiesincostarica.com" in by_platform["propertiesincostarica_cr"]["url"]
    assert "costaricarealestateservice.com" in by_platform["costaricarealestateservice_cr"]["url"]
    assert "2costaricarealestate.com" in by_platform["twocostaricarealestate_cr"]["url"]
    assert "ta.cr" in by_platform["theagency_cr"]["url"]
    assert "kraincostarica.com" in by_platform["krain_cr"]["url"]
    assert "century21costarica.com" in by_platform["century21_cr"]["url"]
    assert "remax-costa-rica.com" in by_platform["remax_cr"]["url"]
    assert by_platform["twocostaricarealestate_cr"]["url"].startswith("https://www.2costaricarealestate.com/?")
    assert all(row["provider_family"] == "broker_direct" for row in by_platform.values())
    assert all("q=Monteverde+cloud+forest" in str(row["url"]) for row in by_platform.values())


def test_generated_source_specs_include_new_costa_rica_developer_providers() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "CR",
            "region_code": "puntarenas",
            "language_code": "es",
            "listing_mode": "buy",
            "location_query": "Santa Teresa",
            "keywords": "project condo",
        },
        selected_platforms=("desarrollos_cr", "tierraverde_cr"),
        principal_id="exec-property-cr-developers",
        default_person_id="self",
        max_results=3,
    )

    by_platform = {row["platform"]: row for row in specs}
    assert set(by_platform) == {"desarrollos_cr", "tierraverde_cr"}
    assert "desarrollos.cr" in by_platform["desarrollos_cr"]["url"]
    assert "tierraverde.cr" in by_platform["tierraverde_cr"]["url"]
    assert all(row["provider_family"] == "developer_projects" for row in by_platform.values())
    assert all("q=Santa+Teresa+project+condo" in str(row["url"]) for row in by_platform.values())


def test_generated_source_specs_split_multi_area_queries_into_dedicated_sources() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "1200 Vienna, 1020 Vienna, 1090",
            "keywords": "lift family",
        },
        selected_platforms=("willhaben",),
        principal_id="exec-property-at",
        default_person_id="self",
        max_results=2,
    )

    assert len(specs) == 3
    assert all(row["platform"] == "willhaben" for row in specs)
    assert [row["location_query"] for row in specs] == ["1200 Vienna", "1020 Vienna", "1090"]
    assert "/mietwohnungen/wien/wien-1200-brigittenau" in str(specs[0]["url"])
    assert "q=lift+family" in str(specs[0]["url"])
    assert "/mietwohnungen/wien/wien-1020-leopoldstadt" in str(specs[1]["url"])
    assert "q=lift+family" in str(specs[1]["url"])
    assert "/mietwohnungen/wien/wien-1090-alsergrund" in str(specs[2]["url"])


def test_generated_source_specs_expand_selected_districts_to_adjacent_sources_when_radius_is_enabled() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "region_code": "vienna",
            "location_query": "1010 Vienna",
            "selected_districts": ["1010 Vienna"],
            "adjacent_area_radius_m": 750,
            "keywords": "lift",
        },
        selected_platforms=("willhaben",),
        principal_id="exec-property-fuzzy-adjacent-districts",
        default_person_id="self",
        max_results=2,
    )

    location_queries = [str(row["location_query"]) for row in specs]
    assert location_queries[0] == "1010 Vienna"
    assert {"1020 Vienna", "1030 Vienna", "1040 Vienna", "1090 Vienna"}.issubset(set(location_queries))
    assert "Salzburg" not in set(location_queries)
    assert all(
        dict(row["provider_filter_pushdown"])["applied"]["location_query"] == row["location_query"]
        for row in specs
    )


def test_generated_source_specs_keep_selected_districts_exact_without_radius() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "region_code": "vienna",
            "location_query": "1010 Vienna",
            "selected_districts": ["1010 Vienna"],
            "search_mode": "discovery",
            "keywords": "lift",
        },
        selected_platforms=("willhaben",),
        principal_id="exec-property-fuzzy-adjacent-districts-default",
        default_person_id="self",
        max_results=2,
    )

    location_queries = [str(row["location_query"]) for row in specs]
    assert location_queries[0] == "1010 Vienna"
    assert location_queries == ["1010 Vienna"]


def test_generated_source_specs_ignore_stale_districts_for_full_region_scope() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "region_code": "vienna",
            "location_query": "Vienna",
            "full_region_scope": True,
            "selected_location_values": ["1010 Vienna", "1020 Vienna"],
            "adjacent_area_radius_m": 750,
        },
        selected_platforms=("willhaben",),
        principal_id="exec-property-full-region-no-adjacent-expansion",
        default_person_id="self",
        max_results=2,
    )

    assert [str(row["location_query"]) for row in specs] == ["Vienna"]
    pushdown = dict(specs[0]["provider_filter_pushdown"])
    assert dict(pushdown["applied"])["location_query"] == "Vienna"


def test_generated_source_specs_pushes_coarse_filters_to_willhaben() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "1200 Vienna",
            "keywords": "lift family",
            "min_area_m2": 80,
            "min_rooms": 3,
            "max_price_eur": 2200,
            "require_floorplan": True,
        },
        selected_platforms=("willhaben",),
        principal_id="exec-property-at-pushdown",
        default_person_id="self",
        max_results=2,
    )

    assert len(specs) == 1
    url = str(specs[0]["url"])
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert "/mietwohnungen/wien/wien-1200-brigittenau" in url
    assert params["q"] == ["lift family"]
    assert params["ESTATE_SIZE/LIVING_AREA_FROM"] == ["80"]
    assert params["PRICE_TO"] == ["2200"]
    assert params["NO_OF_ROOMS_BUCKET"] == ["3X3"]
    pushdown = dict(specs[0]["provider_filter_pushdown"])
    assert pushdown["applied"]["min_area_m2"] == 80
    assert pushdown["applied"]["max_price_eur"] == 2200
    assert pushdown["applied"]["min_rooms"] == 3
    assert "require_floorplan" in pushdown["post_filter_only"]
    assert pushdown["post_filter_reasons"]["require_floorplan"] == "provider_has_no_reliable_dedicated_filter_or_parameter"
    assert str(pushdown["cache_key"]).startswith("willhaben:")
    assert specs[0]["provider_cache_key"] == pushdown["cache_key"]


def test_generated_source_specs_do_not_push_hidden_any_constraints_to_willhaben() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": ["any"],
            "location_query": "1010 Vienna",
            "selected_districts": ["1010 Vienna"],
            "min_area_m2": None,
            "min_rooms": None,
            "max_price_eur": None,
        },
        selected_platforms=("willhaben",),
        principal_id="exec-property-at-any-pushdown",
        default_person_id="self",
        max_results=2,
    )

    assert len(specs) == 1
    url = str(specs[0]["url"])
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert "/mietwohnungen/wien/wien-1010-innere-stadt" in url
    assert "q" not in params
    assert "ESTATE_SIZE/LIVING_AREA_FROM" not in params
    assert "NO_OF_ROOMS_BUCKET" not in params
    assert "PRICE_TO" not in params
    pushdown = dict(specs[0]["provider_filter_pushdown"])
    assert "property_type" not in dict(pushdown["applied"])
    assert "min_area_m2" not in dict(pushdown["applied"])
    assert "min_rooms" not in dict(pushdown["applied"])
    assert "max_price_eur" not in dict(pushdown["applied"])


def test_generated_source_specs_marks_weak_costa_rica_provider_filters_as_attempted() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "CR",
            "language_code": "es",
            "listing_mode": "buy",
            "location_query": "Monteverde",
            "keywords": "cloud forest",
            "min_area_m2": 60,
            "require_floorplan": True,
        },
        selected_platforms=("twocostaricarealestate_cr",),
        principal_id="exec-property-cr-pushdown",
        default_person_id="self",
        max_results=2,
    )

    assert len(specs) == 1
    pushdown = dict(specs[0]["provider_filter_pushdown"])
    assert pushdown["filter_strength"] == "weak_search_then_post_filter"
    assert pushdown["attempted"]["location_query"] == "Monteverde"
    assert pushdown["attempted"]["keywords"] == "cloud forest"
    assert pushdown["attempted"]["min_area_m2"] == 60
    assert "location_query" in pushdown["post_filter_only"]
    assert "keywords" in pushdown["post_filter_only"]
    assert "min_area_m2" in pushdown["post_filter_only"]
    assert "require_floorplan" in pushdown["post_filter_only"]
    assert pushdown["post_filter_reasons"]["location_query"] == "attempted_as_provider_search_query_then_verified_after_fetch"
    assert pushdown["post_filter_reasons"]["require_floorplan"] == "provider_has_no_reliable_dedicated_filter_or_parameter"


def test_generated_source_specs_do_not_push_keyword_query_into_wohnberatung_wien() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "1010 Vienna",
            "keywords": "balcony terrace family quiet bright",
            "min_area_m2": 60,
            "min_rooms": 2,
            "max_price_eur": 1500,
        },
        selected_platforms=("wohnberatung_wien",),
        principal_id="exec-property-wohnberatung-at",
        default_person_id="self",
        max_results=2,
    )

    assert len(specs) == 1
    url = str(specs[0]["url"])
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert "q" not in params
    assert params["maxPrice"] == ["1500"]
    assert params["minRooms"] == ["2"]
    assert params["minArea"] == ["60"]
    pushdown = dict(specs[0]["provider_filter_pushdown"])
    assert pushdown["applied"]["max_price_eur"] == 1500
    assert pushdown["applied"]["min_rooms"] == 2
    assert pushdown["applied"]["min_area_m2"] == 60
    assert "location_query" not in dict(pushdown["applied"])
    assert "keywords" not in dict(pushdown["applied"])
    assert "location_query" not in dict(pushdown["attempted"])
    assert "keywords" not in dict(pushdown["attempted"])
    assert "location_query" in set(pushdown["post_filter_only"])
    assert "keywords" in set(pushdown["post_filter_only"])
    assert "max_price_eur" not in set(pushdown["post_filter_only"])
    assert "min_rooms" not in set(pushdown["post_filter_only"])
    assert "min_area_m2" not in set(pushdown["post_filter_only"])
    assert pushdown["post_filter_reasons"]["location_query"] == "provider_has_no_reliable_dedicated_filter_or_parameter"
    assert pushdown["post_filter_reasons"]["keywords"] == "provider_has_no_reliable_dedicated_filter_or_parameter"


def test_generated_source_specs_every_requested_filter_has_pushdown_receipt() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "1020 Vienna",
            "keywords": "lift family",
            "min_area_m2": 70,
            "min_rooms": 3,
            "max_price_eur": 1800,
            "require_floorplan": True,
        },
        selected_platforms=("willhaben", "immmo", "immoscout_at", "derstandard_at", "remax_at"),
        principal_id="exec-property-filter-receipt",
        default_person_id="self",
        max_results=2,
    )

    assert specs
    for spec in specs:
        pushdown = dict(spec["provider_filter_pushdown"])
        requested = set(dict(pushdown["requested"]).keys())
        applied = set(dict(pushdown["applied"]).keys())
        attempted = set(dict(pushdown["attempted"]).keys())
        post_filter_only = set(pushdown["post_filter_only"])
        reasons = dict(pushdown["post_filter_reasons"])
        assert requested <= applied | attempted | post_filter_only
        assert post_filter_only <= set(reasons)
        assert all(str(reasons[key]).strip() for key in post_filter_only)


def test_generated_source_specs_expand_austria_cooperative_provider_group() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Vienna",
        },
        selected_platforms=("genossenschaften_at",),
        principal_id="exec-property-coops-at",
        default_person_id="self",
        max_results=3,
    )

    assert len(specs) == 18
    assert all(row["platform"] == "genossenschaften_at" for row in specs)
    assert any("gesiba.at" in str(row["url"]).lower() for row in specs)
    assert any("siedlungsunion.at" in str(row["url"]).lower() for row in specs)
    assert any("angebote.sozialbau.at" in str(row["url"]).lower() for row in specs)
    assert any("wbv-gpa.at" in str(row["url"]).lower() for row in specs)
    assert any("frieden.at" in str(row["url"]).lower() for row in specs)
    assert any("egw.at" in str(row["url"]).lower() for row in specs)
    assert any("oesw.at" in str(row["url"]).lower() for row in specs)
    assert any("familienwohnbau.at" in str(row["url"]).lower() for row in specs)
    assert any("wag.at" in str(row["url"]).lower() for row in specs)
    assert any("heimat-oesterreich.at" in str(row["url"]).lower() for row in specs)
    assert any("bwsg.at" in str(row["url"]).lower() for row in specs)
    assert any("wiensued.at" in str(row["url"]).lower() for row in specs)
    assert any("ebg-wohnen.at" in str(row["url"]).lower() for row in specs)
    assert any("ooewohnbau.at" in str(row["url"]).lower() for row in specs)
    assert any("salzburg-wohnbau.at" in str(row["url"]).lower() for row in specs)
    assert any("oevw.at" in str(row["url"]).lower() for row in specs)
    assert all("Vienna" in str(row["label"]) for row in specs)


def test_generated_source_specs_skip_unimplemented_provider_lanes() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Vienna",
        },
        selected_platforms=("community_signals_at",),
        principal_id="exec-property-community-coming-soon",
        default_person_id="self",
        max_results=3,
    )

    assert specs == ()


def test_generated_source_specs_carry_provider_governance() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Vienna",
        },
        selected_platforms=("willhaben",),
        principal_id="exec-property-provider-governance",
        default_person_id="self",
        max_results=3,
    )

    assert specs
    spec = dict(specs[0])
    governance = dict(spec["provider_governance"])
    assert spec["provider_market_readiness"] == governance["market_readiness"]
    assert spec["provider_rights_review_status"] == governance["terms_review_status"]
    assert governance["public_packet_allowed"] is False
    assert governance["listing_cache_allowed"] is False
    assert governance["attribution_required"] is True


def test_generated_source_specs_build_justiz_edikte_result_search_url() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "buy",
            "location_query": "1090",
            "keywords": "lift family",
            "property_type": "apartment",
        },
        selected_platforms=("justiz_edikte_at",),
        principal_id="exec-property-justiz-at",
        default_person_id="self",
        max_results=3,
    )

    assert len(specs) == 1
    assert specs[0]["platform"] == "justiz_edikte_at"
    assert "suchedi?SearchView&subf=eex" in str(specs[0]["url"])
    assert "%5BVPLZ%5D=1090" in str(specs[0]["url"])
    assert "%5BBL%5D=0" in str(specs[0]["url"])


def test_generated_source_specs_use_public_fallback_for_immoscout_at_and_kalandra() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "buy",
            "location_query": "Wien",
        },
        selected_platforms=("immoscout_at", "kalandra"),
        principal_id="exec-property-at-upstreams",
        default_person_id="self",
        max_results=3,
    )

    assert len(specs) == 2
    immoscout_spec = next(row for row in specs if row["platform"] == "immoscout_at")
    kalandra_spec = next(row for row in specs if row["platform"] == "kalandra")
    assert str(immoscout_spec["url"]).startswith("https://www.immmo.at/suche/kauf")
    assert "pq_upstream=immoscout_at" in str(immoscout_spec["url"])
    assert kalandra_spec["url"] == "https://www.kalandra.at/immobiliensuche"


def test_generated_source_specs_use_live_austrian_paths_for_immowelt_and_findmyhome() -> None:
    specs = generated_source_specs(
        preferences={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "buy",
            "location_query": "Vienna",
            "property_type": "apartment",
        },
        selected_platforms=("immowelt_at", "findmyhome_at"),
        principal_id="exec-property-at-live-paths",
        default_person_id="self",
        max_results=3,
    )

    assert len(specs) == 2
    immowelt_spec = next(row for row in specs if row["platform"] == "immowelt_at")
    findmyhome_spec = next(row for row in specs if row["platform"] == "findmyhome_at")
    assert str(immowelt_spec["url"]).startswith("https://www.immowelt.at/suche/wohnungen/kaufen")
    assert findmyhome_spec["url"] == "https://www.findmyhome.at/immo/wohnung-kaufen/wien"
