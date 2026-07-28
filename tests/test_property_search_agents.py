from __future__ import annotations

from app.api.routes.landing_property_saved_searches import (
    build_agent_management_rows,
    build_property_search_agents,
    format_property_search_agent,
)
import app.services.onboarding as onboarding_service
from app.services.onboarding import OnboardingService
from tests.product_test_helpers import build_property_client, start_workspace


def _seed_trusted_commercial(
    client,
    *,
    principal_id: str,
    preferences: dict[str, object],
    plan_key: str,
    plan_source: str = "",
) -> None:
    trusted_preferences = dict(preferences)
    trusted_preferences["property_commercial"] = {
        "active_plan_key": plan_key,
        "status": "active",
        "active_until": "2999-01-01T00:00:00+00:00",
        **({"plan_source": plan_source} if plan_source else {}),
    }
    client.app.state.container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json=trusted_preferences,
        trusted_commercial_update=True,
    )


def test_property_search_agents_can_be_managed_independently() -> None:
    principal_id = "exec-property-search-agents"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property office")

    created = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "wien",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "Wien",
            "selected_platforms": ["willhaben", "immobilienscout24_at"],
            "search_agent_enabled": True,
            "search_agent_duration_days": 90,
            "search_agent_notification_limit": 3,
            "search_agent_notification_period": "day",
        },
    )
    assert created.status_code == 200, created.text
    preferences = created.json()["property_search_preferences"]
    agents = preferences["search_agents"]
    assert len(agents) == 1
    agent_id = agents[0]["agent_id"]
    assert agents[0]["enabled"] is True
    assert agents[0]["notification_limit"] == 3

    _seed_trusted_commercial(
        client,
        principal_id=principal_id,
        preferences=preferences,
        plan_key="plus",
    )

    duplicated = client.post(
        f"/v1/onboarding/property-search/agents/{agent_id}",
        json={"action": "duplicate"},
    )
    assert duplicated.status_code == 200, duplicated.text
    agents = duplicated.json()["property_search_preferences"]["search_agents"]
    assert len(agents) == 2
    duplicate_id = next(agent["agent_id"] for agent in agents if agent["agent_id"] != agent_id)
    duplicate = next(agent for agent in agents if agent["agent_id"] == duplicate_id)
    assert duplicate["enabled"] is False

    saved = client.post(
        f"/v1/onboarding/property-search/agents/{duplicate_id}",
        json={
            "action": "save",
            "patch": {
                "name": "Vienna weekly shortlist",
                "notification_limit": 9,
                "notification_period": "week",
                "duration_days": 365,
                "last_run_at": "2026-06-12T08:00:00+02:00",
                "next_run_at": "2026-06-13T08:00:00+02:00",
                "sent_in_current_window": 4,
            },
        },
    )
    assert saved.status_code == 200, saved.text
    duplicate = next(
        agent
        for agent in saved.json()["property_search_preferences"]["search_agents"]
        if agent["agent_id"] == duplicate_id
    )
    assert duplicate["name"] == "Vienna weekly shortlist"
    assert duplicate["notification_limit"] == 9
    assert duplicate["notification_period"] == "week"
    assert duplicate["duration_days"] == 365
    assert duplicate["last_run_at"] == "2026-06-12T08:00:00+02:00"
    assert duplicate["next_run_at"] == "2026-06-13T08:00:00+02:00"
    assert duplicate["sent_in_current_window"] == 4

    paused = client.post(
        f"/v1/onboarding/property-search/agents/{agent_id}",
        json={"action": "pause"},
    )
    assert paused.status_code == 200, paused.text
    original = next(
        agent
        for agent in paused.json()["property_search_preferences"]["search_agents"]
        if agent["agent_id"] == agent_id
    )
    assert original["enabled"] is False

    resumed = client.post(
        f"/v1/onboarding/property-search/agents/{duplicate_id}",
        json={"action": "resume"},
    )
    assert resumed.status_code == 200, resumed.text
    preferences = resumed.json()["property_search_preferences"]
    assert preferences["active_search_agent_id"] == duplicate_id
    resumed_agent = next(agent for agent in preferences["search_agents"] if agent["agent_id"] == duplicate_id)
    assert resumed_agent["enabled"] is True

    deleted = client.post(
        f"/v1/onboarding/property-search/agents/{agent_id}",
        json={"action": "delete"},
    )
    assert deleted.status_code == 200, deleted.text
    agents = deleted.json()["property_search_preferences"]["search_agents"]
    assert [agent["agent_id"] for agent in agents] == [duplicate_id]


def test_property_search_agent_principal_listing_only_returns_enabled_saved_searches() -> None:
    client = build_property_client(principal_id="exec-property-search-agent-principal-listing")
    onboarding = client.app.state.container.onboarding

    start_workspace(client, mode="personal", workspace_name="Property office")
    onboarding.start_workspace(
        principal_id="principal-enabled",
        workspace_name="Enabled workspace",
        workspace_mode="personal",
        region="AT",
        language="de",
        timezone="Europe/Vienna",
        selected_channels=(),
    )
    onboarding.upsert_property_search_preferences(
        principal_id="principal-enabled",
        property_search_preferences_json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "search_agents": [
                {
                    "agent_id": "agent-enabled",
                    "name": "Enabled",
                    "enabled": True,
                    "country_code": "AT",
                    "listing_mode": "rent",
                    "location_query": "Wien",
                    "selected_platforms": ["willhaben"],
                }
            ],
        },
    )
    onboarding.start_workspace(
        principal_id="principal-paused",
        workspace_name="Paused workspace",
        workspace_mode="personal",
        region="AT",
        language="de",
        timezone="Europe/Vienna",
        selected_channels=(),
    )
    onboarding.upsert_property_search_preferences(
        principal_id="principal-paused",
        property_search_preferences_json={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "search_agents": [
                {
                    "agent_id": "agent-paused",
                    "name": "Paused",
                    "enabled": False,
                    "country_code": "AT",
                    "listing_mode": "rent",
                    "location_query": "Wien",
                    "selected_platforms": ["willhaben"],
                }
            ],
        },
    )

    principals = onboarding.list_property_search_agent_principals(limit=20)

    assert principals == ("principal-enabled",)


def test_property_search_preferences_drop_cross_country_providers() -> None:
    client = build_property_client(principal_id="exec-property-cross-country-providers")
    start_workspace(client, mode="personal", workspace_name="Property office")

    created = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "listing_mode": "rent",
            "location_query": "Vienna",
            "selected_platforms": ["willhaben", "otodom", "olx_pl_nieruchomosci"],
        },
    )

    assert created.status_code == 200, created.text
    preferences = created.json()["property_search_preferences"]
    assert preferences["country_code"] == "AT"
    assert preferences["selected_platforms"] == ["willhaben"]


def test_property_search_agents_can_delete_the_last_saved_search() -> None:
    client = build_property_client(principal_id="exec-property-search-agent-delete-last")
    start_workspace(client, mode="personal", workspace_name="Property office")

    created = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "wien",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "Wien",
            "selected_platforms": ["willhaben"],
            "search_agent_enabled": True,
            "search_agent_duration_days": 30,
            "search_agent_notification_limit": 3,
            "search_agent_notification_period": "day",
            "property_commercial": {
                "active_plan_key": "plus",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )
    assert created.status_code == 200, created.text
    agent_id = created.json()["property_search_preferences"]["active_search_agent_id"]

    deleted = client.post(
        f"/v1/onboarding/property-search/agents/{agent_id}",
        json={"action": "delete"},
    )
    assert deleted.status_code == 200, deleted.text
    preferences = deleted.json()["property_search_preferences"]
    assert preferences["search_agents"] == []
    assert preferences["active_search_agent_id"] == ""


def test_unsaved_property_brief_is_not_exposed_as_saved_search() -> None:
    agents, active_agent = build_property_search_agents(
        {
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "1020 Vienna",
            "listing_mode": "buy",
            "property_type": "apartment",
            "selected_platforms": ["willhaben"],
            "search_agent_enabled": True,
            "search_agent_duration_days": 30,
            "search_agent_notification_limit": 3,
            "search_agent_notification_period": "week",
        },
        selected_platforms=["willhaben"],
        selected_listing_mode="buy",
        search_mode_requested="strict",
        default_duration_days=30,
        default_notification_limit=3,
        default_notification_period="week",
        normalize_property_type_values=lambda value: [str(value or "apartment")],
        scope_preview_builder=lambda country, region, location: {"summary": f"{country}:{region}:{location}"},
    )

    assert agents == []
    assert active_agent == {}


def test_property_search_agent_management_rows_edit_in_search_editor() -> None:
    rows = build_agent_management_rows(
        [
            {
                "agent_id": "agent-vienna",
                "name": "Vienna apartments",
                "scope_label": "1020 Vienna",
                "notification_label": "3 per week",
                "run_label": "Waiting for first run",
                "enabled": True,
            }
        ],
        run_id="run-live-42",
    )

    assert rows[0]["action_href"] == "/app/agents?agent_id=agent-vienna&run_id=run-live-42"
    assert rows[0]["secondary_action_href"] == "/app/search?load_agent=agent-vienna&run_id=run-live-42"


def test_property_search_agent_load_returns_saved_filters_without_overwriting_current_preferences() -> None:
    principal_id = "exec-property-search-agent-load"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property office")

    created = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "language_code": "de",
            "listing_mode": "buy",
            "property_type": "apartment",
            "location_query": "1020 Wien",
            "selected_platforms": ["willhaben"],
            "min_area_m2": 70,
            "max_price_eur": 650000,
            "search_agent_enabled": True,
            "search_agent_duration_days": 90,
            "search_agent_notification_limit": 3,
            "search_agent_notification_period": "day",
            },
        )
    assert created.status_code == 200, created.text
    preferences = created.json()["property_search_preferences"]
    first_agent_id = preferences["search_agents"][0]["agent_id"]
    _seed_trusted_commercial(
        client,
        principal_id=principal_id,
        preferences=preferences,
        plan_key="plus",
    )
    duplicated = client.post(f"/v1/onboarding/property-search/agents/{first_agent_id}", json={"action": "duplicate"})
    assert duplicated.status_code == 200, duplicated.text
    second_agent_id = next(
        agent["agent_id"]
        for agent in duplicated.json()["property_search_preferences"]["search_agents"]
        if agent["agent_id"] != first_agent_id
    )

    saved = client.post(
        f"/v1/onboarding/property-search/agents/{second_agent_id}",
        json={
            "action": "save",
            "patch": {
                "name": "Costa Rica land search",
                "country_code": "CR",
                "region_code": "puntarenas",
                "location_query": "Monteverde",
                "listing_mode": "buy",
                "property_type": "land",
                "selected_platforms": ["re_cr_mls"],
                "duration_days": 365,
                "notification_limit": 6,
                "notification_period": "week",
                "preferences_json": {
                    "country_code": "CR",
                    "region_code": "puntarenas",
                    "location_query": "Monteverde",
                    "listing_mode": "buy",
                    "property_type": "land",
                    "selected_platforms": ["re_cr_mls"],
                    "min_area_m2": 1200,
                    "max_price_eur": 350000,
                    "keywords": "seezugang, jungle",
                    "search_agent_enabled": False,
                    "search_agent_duration_days": 365,
                    "search_agent_notification_limit": 6,
                    "search_agent_notification_period": "week",
                },
            },
        },
    )
    assert saved.status_code == 200, saved.text

    loaded = client.post(f"/v1/onboarding/property-search/agents/{second_agent_id}", json={"action": "load"})
    assert loaded.status_code == 200, loaded.text
    preferences = loaded.json()["property_search_preferences"]
    loaded_preferences = loaded.json()["loaded_property_search_preferences"]
    assert preferences["country_code"] == "AT"
    assert preferences["region_code"] == "vienna"
    assert preferences["location_query"] == "1020 Wien"
    assert preferences["property_type"] == "apartment"
    assert preferences["selected_platforms"] == ["willhaben"]
    assert preferences["min_area_m2"] == 70
    assert preferences["max_price_eur"] == 650000
    assert loaded.json()["loaded_search_agent_id"] == second_agent_id
    assert loaded_preferences["country_code"] == "CR"
    assert loaded_preferences["region_code"] == "puntarenas"
    assert loaded_preferences["location_query"] == "Monteverde"
    assert loaded_preferences["property_type"] == ["land"]
    assert loaded_preferences["selected_platforms"] == ["re_cr_mls"]
    assert loaded_preferences["min_area_m2"] == 1200
    assert loaded_preferences["max_price_eur"] == 350000
    assert loaded_preferences["keywords"] == "seezugang, jungle"
    assert loaded_preferences["search_agent_duration_days"] == 365
    assert loaded_preferences["search_agent_notification_limit"] == 6
    assert loaded_preferences["search_agent_notification_period"] == "week"


def test_agent_saved_search_payload_drops_stale_result_cap_on_save_and_load() -> None:
    client = build_property_client(principal_id="exec-property-agent-search-agent-max-results")
    start_workspace(client, mode="personal", workspace_name="Property office")

    created = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "selected_platforms": ["willhaben"],
            "search_agent_enabled": True,
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )
    assert created.status_code == 200, created.text
    agent_id = created.json()["property_search_preferences"]["search_agents"][0]["agent_id"]

    saved = client.post(
        f"/v1/onboarding/property-search/agents/{agent_id}",
        json={
            "action": "save",
            "patch": {
                "preferences_json": {
                    "country_code": "AT",
                    "listing_mode": "rent",
                    "selected_platforms": ["willhaben"],
                    "max_results_per_source": 8,
                },
            },
        },
    )
    assert saved.status_code == 200, saved.text
    saved_agent = next(
        agent
        for agent in saved.json()["property_search_preferences"]["search_agents"]
        if agent["agent_id"] == agent_id
    )
    assert "max_results_per_source" not in saved_agent["preferences_json"]

    loaded = client.post(f"/v1/onboarding/property-search/agents/{agent_id}", json={"action": "load"})
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["property_search_preferences"].get("max_results_per_source") is None


def test_plus_saved_search_payload_drops_stale_result_cap_on_save_and_load() -> None:
    client = build_property_client(principal_id="exec-property-plus-search-agent-max-results")
    start_workspace(client, mode="personal", workspace_name="Property office")

    created = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "listing_mode": "rent",
            "selected_platforms": ["willhaben"],
            "search_agent_enabled": True,
            "property_commercial": {
                "active_plan_key": "plus",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )
    assert created.status_code == 200, created.text
    agent_id = created.json()["property_search_preferences"]["search_agents"][0]["agent_id"]

    saved = client.post(
        f"/v1/onboarding/property-search/agents/{agent_id}",
        json={
            "action": "save",
            "patch": {
                "preferences_json": {
                    "country_code": "AT",
                    "listing_mode": "rent",
                    "selected_platforms": ["willhaben"],
                    "max_results_per_source": 50,
                },
            },
        },
    )
    assert saved.status_code == 200, saved.text
    saved_agent = next(
        agent
        for agent in saved.json()["property_search_preferences"]["search_agents"]
        if agent["agent_id"] == agent_id
    )
    assert "max_results_per_source" not in saved_agent["preferences_json"]

    loaded = client.post(f"/v1/onboarding/property-search/agents/{agent_id}", json={"action": "load"})
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["property_search_preferences"].get("max_results_per_source") is None


def test_saved_search_load_payload_prefers_saved_preferences_over_current_brief_defaults() -> None:
    formatted = format_property_search_agent(
        {
            "name": "Monteverde buy watch",
            "enabled": True,
            "preferences_json": {
                "country_code": "CR",
                "region_code": "puntarenas",
                "location_query": "Monteverde",
                "listing_mode": "buy",
                "property_type": "house",
                "selected_platforms": ["re_cr_mls"],
            },
        },
        property_preferences={
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "1090 Vienna",
            "property_type": "apartment",
        },
        selected_platforms=["willhaben"],
        selected_listing_mode="rent",
        search_mode_requested="strict",
        default_duration_days=30,
        default_notification_limit=5,
        default_notification_period="day",
        normalize_property_type_values=lambda value: [str(value).strip().lower()] if str(value).strip() else ["any"],
        scope_preview_builder=lambda country_code, region_code, location_query: {
            "country_code": country_code,
            "region_code": region_code,
            "location_query": location_query,
        },
    )

    assert formatted["country_code"] == "CR"
    assert formatted["region_code"] == "puntarenas"
    assert formatted["location_query"] == "Monteverde"
    assert formatted["listing_mode"] == "buy"
    assert formatted["scope_preview"]["country_code"] == "CR"


def test_agent_saved_search_format_payload_drops_stale_result_cap() -> None:
    formatted = format_property_search_agent(
        {
            "name": "Vienna rent watch",
            "enabled": True,
            "preferences_json": {
                "country_code": "AT",
                "region_code": "vienna",
                "location_query": "1020 Wien",
                "listing_mode": "rent",
                "property_type": "apartment",
                "selected_platforms": ["willhaben"],
                "max_results_per_source": 6,
            },
        },
        property_preferences={
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "1020 Wien",
            "property_type": "apartment",
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
        selected_platforms=["willhaben"],
        selected_listing_mode="rent",
        search_mode_requested="strict",
        default_duration_days=30,
        default_notification_limit=5,
        default_notification_period="day",
        normalize_property_type_values=lambda value: [str(value).strip().lower()] if str(value).strip() else ["any"],
        scope_preview_builder=lambda country_code, region_code, location_query: {
            "country_code": country_code,
            "region_code": region_code,
            "location_query": location_query,
        },
    )

    assert "max_results_per_source" not in formatted["load_payload"]


def test_plus_saved_search_format_payload_drops_stale_result_cap() -> None:
    formatted = format_property_search_agent(
        {
            "name": "Vienna rent watch",
            "enabled": True,
            "preferences_json": {
                "country_code": "AT",
                "region_code": "vienna",
                "location_query": "1020 Wien",
                "listing_mode": "rent",
                "property_type": "apartment",
                "selected_platforms": ["willhaben"],
                "max_results_per_source": 50,
            },
        },
        property_preferences={
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "1020 Wien",
            "property_type": "apartment",
            "property_commercial": {
                "active_plan_key": "plus",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
        selected_platforms=["willhaben"],
        selected_listing_mode="rent",
        search_mode_requested="strict",
        default_duration_days=30,
        default_notification_limit=5,
        default_notification_period="day",
        normalize_property_type_values=lambda value: [str(value).strip().lower()] if str(value).strip() else ["any"],
        scope_preview_builder=lambda country_code, region_code, location_query: {
            "country_code": country_code,
            "region_code": region_code,
            "location_query": location_query,
        },
    )

    assert "max_results_per_source" not in formatted["load_payload"]


def test_investment_saved_search_snapshot_forces_buy_and_investment_labels() -> None:
    formatted = format_property_search_agent(
        {
            "name": "",
            "enabled": True,
            "preferences_json": {
                "country_code": "AT",
                "region_code": "vienna",
                "location_query": "Vienna",
                "search_goal": "investment",
                "listing_mode": "rent",
                "selected_platforms": ["willhaben"],
            },
        },
        property_preferences={
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "Vienna",
            "search_goal": "home",
        },
        selected_platforms=["willhaben"],
        selected_listing_mode="rent",
        search_mode_requested="strict",
        default_duration_days=30,
        default_notification_limit=5,
        default_notification_period="day",
        normalize_property_type_values=lambda value: [str(value).strip().lower()] if str(value).strip() else ["any"],
        scope_preview_builder=lambda country_code, region_code, location_query: {
            "country_code": country_code,
            "region_code": region_code,
            "location_query": location_query,
        },
    )

    assert formatted["listing_mode"] == "buy"
    assert formatted["scope_label"].startswith("Investment · ")
    assert formatted["load_payload"]["search_goal"] == "investment"
    assert formatted["load_payload"]["listing_mode"] == "buy"


def test_property_search_preference_save_preserves_other_agents_and_sanitizes_provider_country() -> None:
    principal_id = "exec-property-search-agent-preserve"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property office")

    created = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "1020 Vienna",
            "selected_platforms": ["willhaben", "encuentra24_cr"],
            "search_agent_enabled": True,
            "search_agent_duration_days": 56,
            "search_agent_notification_limit": 5,
            "search_agent_notification_period": "day",
            },
        )
    assert created.status_code == 200, created.text
    created_preferences = created.json()["property_search_preferences"]
    first_agent_id = created_preferences["active_search_agent_id"]
    assert len(created_preferences["search_agents"]) == 1
    assert created_preferences["search_agents"][0]["selected_platforms"] == ["willhaben"]

    _seed_trusted_commercial(
        client,
        principal_id=principal_id,
        preferences=created_preferences,
        plan_key="agent",
    )
    duplicated = client.post(f"/v1/onboarding/property-search/agents/{first_agent_id}", json={"action": "duplicate"})
    assert duplicated.status_code == 200, duplicated.text
    second_agent_id = next(
        agent["agent_id"]
        for agent in duplicated.json()["property_search_preferences"]["search_agents"]
        if agent["agent_id"] != first_agent_id
    )
    saved_second = client.post(
        f"/v1/onboarding/property-search/agents/{second_agent_id}",
        json={
            "action": "save",
            "patch": {
                "name": "Monteverde buy watch",
                "country_code": "CR",
                "region_code": "puntarenas",
                "location_query": "Monteverde",
                "listing_mode": "buy",
                "property_type": "house",
                "selected_platforms": ["re_cr_mls", "willhaben"],
                "preferences_json": {
                    "country_code": "CR",
                    "region_code": "puntarenas",
                    "location_query": "Monteverde",
                    "listing_mode": "buy",
                    "property_type": "house",
                    "selected_platforms": ["re_cr_mls", "willhaben"],
                    "search_agent_enabled": True,
                    "search_agent_duration_days": 365,
                    "search_agent_notification_limit": 7,
                    "search_agent_notification_period": "week",
                },
            },
        },
    )
    assert saved_second.status_code == 200, saved_second.text

    saved_current = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "language_code": "de",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "1040 Vienna",
            "selected_platforms": ["willhaben", "encuentra24_cr"],
            "active_search_agent_id": first_agent_id,
            "search_agent_enabled": True,
            "search_agent_duration_days": 90,
            "search_agent_notification_limit": 4,
            "search_agent_notification_period": "day",
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        },
    )
    assert saved_current.status_code == 200, saved_current.text
    preferences = saved_current.json()["property_search_preferences"]
    assert preferences["active_search_agent_id"] == first_agent_id
    assert len(preferences["search_agents"]) == 2
    vienna_agent = next(agent for agent in preferences["search_agents"] if agent["agent_id"] == first_agent_id)
    monteverde_agent = next(agent for agent in preferences["search_agents"] if agent["agent_id"] == second_agent_id)
    assert vienna_agent["location_query"] == "1040 Vienna"
    assert vienna_agent["selected_platforms"] == ["willhaben"]
    assert vienna_agent["preferences_json"]["selected_platforms"] == ["willhaben"]
    assert monteverde_agent["location_query"] == "Monteverde"
    assert monteverde_agent["selected_platforms"] == ["re_cr_mls"]
    assert monteverde_agent["preferences_json"]["selected_platforms"] == ["re_cr_mls"]


def test_property_search_agent_update_rejects_unknown_agent() -> None:
    client = build_property_client(principal_id="exec-property-search-agent-missing")
    start_workspace(client, mode="personal", workspace_name="Property office")

    missing = client.post(
        "/v1/onboarding/property-search/agents/does-not-exist",
        json={"action": "pause"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "property_search_agent_not_found"


def test_property_search_agent_plan_limits_are_enforced() -> None:
    raw_agents = [
        {"agent_id": f"agent-{index}", "name": f"Search {index}", "country_code": "AT", "location_query": "Wien"}
        for index in range(30)
    ]

    free_agents = OnboardingService._normalize_property_search_agents(
        {
            "country_code": "AT",
            "location_query": "Wien",
            "search_agents": raw_agents,
            "property_commercial": {"active_plan_key": "free"},
        }
    )
    plus_agents = OnboardingService._normalize_property_search_agents(
        {
            "country_code": "AT",
            "location_query": "Wien",
            "search_agents": raw_agents,
            "property_commercial": {
                "active_plan_key": "plus",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        }
    )
    agent_agents = OnboardingService._normalize_property_search_agents(
        {
            "country_code": "AT",
            "location_query": "Wien",
            "search_agents": raw_agents,
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
        }
    )

    assert len(free_agents) == 1
    assert len(plus_agents) == 3
    assert len(agent_agents) == 30


def test_preexisting_agents_survive_free_plan_save_load_and_landing() -> None:
    principal_id = "exec-property-search-agent-preserve-over-limit"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Preserved searches")
    raw_agents = [
        {
            "agent_id": f"agent-{index}",
            "name": f"Preserved search {index}",
            "enabled": True,
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": f"10{index + 10} Vienna",
            "listing_mode": "rent",
            "property_type": "apartment",
        }
        for index in range(3)
    ]

    saved = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "1010 Vienna",
            "listing_mode": "rent",
            "property_type": "apartment",
            "active_search_agent_id": "agent-0",
            "property_commercial": {"active_plan_key": "free"},
            "search_agents": raw_agents,
        },
    )

    assert saved.status_code == 200, saved.text
    saved_preferences = dict(saved.json()["property_search_preferences"])
    assert OnboardingService._property_search_agent_limit(saved_preferences) == 1
    assert [row["agent_id"] for row in saved_preferences["search_agents"]] == [
        "agent-0",
        "agent-1",
        "agent-2",
    ]

    loaded = client.get("/v1/onboarding/property-search/preferences")
    assert loaded.status_code == 200, loaded.text
    assert [
        row["agent_id"]
        for row in loaded.json()["property_search_preferences"]["search_agents"]
    ] == ["agent-0", "agent-1", "agent-2"]

    page = client.get("/app/agents", headers={"host": "propertyquarry.com"})
    assert page.status_code == 200
    assert all(agent["name"] in page.text for agent in raw_agents)

    duplicate = client.post(
        "/v1/onboarding/property-search/agents/agent-0",
        json={"action": "duplicate"},
    )
    assert duplicate.status_code == 400
    assert "property_search_agent_limit_reached:1" in duplicate.text


def test_property_search_agent_payloads_do_not_embed_other_agents() -> None:
    agents = OnboardingService._normalize_property_search_agents(
        {
            "country_code": "AT",
            "location_query": "Vienna",
            "active_search_agent_id": "agent-vienna",
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            },
            "search_agents": [
                {
                    "agent_id": "agent-vienna",
                    "country_code": "AT",
                    "location_query": "Vienna",
                    "selected_platforms": ["willhaben"],
                    "preferences_json": {
                        "country_code": "AT",
                        "location_query": "Vienna",
                        "search_agents": [{"agent_id": "stale-nested"}],
                        "active_search_agent_id": "stale-nested",
                        "raw_preferences": {"private": True},
                        "property_commercial": {"active_plan_key": "agent"},
                    },
                },
                {
                    "agent_id": "agent-monteverde",
                    "country_code": "CR",
                    "location_query": "Monteverde",
                    "selected_platforms": ["re_cr_mls"],
                },
            ],
        }
    )

    assert len(agents) == 2
    for agent in agents:
        payload = agent["preferences_json"]
        assert "search_agents" not in payload
        assert "active_search_agent_id" not in payload
        assert "raw_preferences" not in payload
        assert "property_commercial" not in payload


def test_property_search_preferences_drop_removed_match_bar_from_saved_preferences_and_agent_payloads() -> None:
    normalized = OnboardingService._normalize_property_search_preferences(
        {
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "Vienna",
            "min_match_score": 35,
            "search_agent_enabled": True,
            "search_agents": [
                {
                    "agent_id": "agent-vienna",
                    "country_code": "AT",
                    "location_query": "Vienna",
                    "preferences_json": {
                        "country_code": "AT",
                        "location_query": "Vienna",
                        "min_match_score": 20,
                    },
                }
            ],
        }
    )

    assert "min_match_score" not in normalized
    assert "min_match_score" not in normalized["raw_preferences"]
    assert "min_match_score" not in normalized["search_agents"][0]["preferences_json"]


def test_property_search_agent_full_region_scope_drops_stale_selected_locations() -> None:
    normalized = OnboardingService._normalize_property_search_preferences(
        {
            "country_code": "AT",
            "region_code": "vienna",
            "location_query": "Vienna",
            "full_region_scope": True,
            "selected_location_values": ["1010 Vienna", "1020 Vienna"],
            "selected_platforms": ["willhaben"],
            "search_agent_enabled": True,
            "search_agents": [
                {
                    "agent_id": "agent-vienna",
                    "country_code": "AT",
                    "region_code": "vienna",
                    "location_query": "Vienna",
                    "full_region_scope": True,
                    "selected_location_values": ["1010 Vienna", "1020 Vienna"],
                    "selected_platforms": ["willhaben"],
                    "preferences_json": {
                        "country_code": "AT",
                        "region_code": "vienna",
                        "location_query": "Vienna",
                        "full_region_scope": True,
                        "selected_location_values": ["1010 Vienna", "1020 Vienna"],
                        "selected_platforms": ["willhaben"],
                    },
                }
            ],
        }
    )

    assert normalized["selected_location_values"] == []
    agent = normalized["search_agents"][0]
    assert agent["full_region_scope"] is True
    assert agent["selected_location_values"] == []
    assert agent["preferences_json"]["full_region_scope"] is True
    assert agent["preferences_json"]["selected_location_values"] == []


def test_property_search_preferences_recover_paid_commercial_state_from_teable(monkeypatch) -> None:
    principal_id = "pq-teable-restore"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property restore")

    created = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "property_commercial": {"active_plan_key": "free", "status": "free"},
        },
    )
    assert created.status_code == 200, created.text

    monkeypatch.setattr(
        onboarding_service,
        "fetch_propertyquarry_subscription_fields",
        lambda **kwargs: {
            "principal_id": principal_id,
            "current_plan_key": "agent",
            "status": "active",
            "active_until": "2999-01-01T00:00:00+00:00",
            "plan_source": "teable_projection",
            "commercial_json": "{\"active_plan_key\":\"agent\",\"status\":\"active\",\"active_until\":\"2999-01-01T00:00:00+00:00\",\"plan_source\":\"teable_projection\"}",
        },
    )

    restored = client.get("/v1/onboarding/property-search/preferences")
    assert restored.status_code == 200, restored.text
    commercial = restored.json()["property_search_preferences"]["property_commercial"]
    assert commercial["active_plan_key"] == "agent"
    assert commercial["status"] == "active"
    assert commercial["plan_source"] == "teable_projection"


def test_property_search_preferences_ignore_empty_free_overwrite_when_paid_exists() -> None:
    principal_id = "pq-commercial-overwrite-guard"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Property preserve")

    seeded = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            },
    )
    assert seeded.status_code == 200, seeded.text
    _seed_trusted_commercial(
        client,
        principal_id=principal_id,
        preferences=seeded.json()["property_search_preferences"],
        plan_key="agent",
        plan_source="billing",
    )

    downgraded = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "country_code": "AT",
            "language_code": "de",
            "listing_mode": "rent",
            "location_query": "Wien",
            "property_commercial": {"active_plan_key": "free", "status": "free"},
        },
    )
    assert downgraded.status_code == 200, downgraded.text
    commercial = downgraded.json()["property_search_preferences"]["property_commercial"]
    assert commercial["active_plan_key"] == "agent"
    assert commercial["status"] == "active"
