from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.routes.product_api_contracts import PropertySearchRunStartIn
from app.product import service as product_service


def _strict_district_preferences() -> dict[str, object]:
    return {
        "country_code": "AT",
        "listing_mode": "rent",
        "location_query": "Wien",
        "selected_districts": ["1010 Vienna"],
        "search_mode": "discovery",
    }


def test_search_run_projection_preserves_adjacent_radius_inputs_without_workspace_state() -> None:
    projected = product_service._property_search_run_preferences_projection(
        {
            **_strict_district_preferences(),
            "adjacent_area_radius_m": 1000,
            "adjacent_area_radius_value": 1.0,
            "adjacent_area_radius_unit": "km",
            "future_workspace_secret": "must-not-enter-run-snapshot",
        }
    )

    assert projected["adjacent_area_radius_m"] == 1000
    assert projected["adjacent_area_radius_value"] == 1.0
    assert projected["adjacent_area_radius_unit"] == "km"
    assert "future_workspace_secret" not in projected


def test_search_brief_fingerprint_canonicalizes_and_detects_adjacent_radius() -> None:
    strict = _strict_district_preferences()
    radius_in_units = {
        **strict,
        "adjacent_area_radius_value": 1.0,
        "adjacent_area_radius_unit": "km",
    }
    radius_in_meters = {
        **strict,
        "adjacent_area_radius_m": 1000,
    }

    strict_source = product_service._property_search_brief_fingerprint_source(strict)
    unit_source = product_service._property_search_brief_fingerprint_source(radius_in_units)
    meter_source = product_service._property_search_brief_fingerprint_source(radius_in_meters)

    assert strict_source["adjacent_area_radius_m"] == 0
    assert unit_source["adjacent_area_radius_m"] == 1000
    assert unit_source == meter_source
    assert product_service._property_search_brief_fingerprint(strict) != product_service._property_search_brief_fingerprint(radius_in_units)
    assert product_service._property_search_brief_changed_keys(
        run_preferences=strict,
        current_preferences=radius_in_units,
    ) == ["adjacent_area_radius_m"]


@pytest.mark.parametrize(
    ("radius_preferences", "expected"),
    [
        (
            {"adjacent_area_radius_value": 1.0, "adjacent_area_radius_unit": " KM "},
            {"adjacent_area_radius_value": 1.0, "adjacent_area_radius_unit": "km"},
        ),
        (
            {"adjacent_area_radius_m": 1000},
            {"adjacent_area_radius_m": 1000},
        ),
        (
            {"adjacent_area_radius_value": 1000, "adjacent_area_radius_unit": "km"},
            {"adjacent_area_radius_value": 1000, "adjacent_area_radius_unit": "km"},
        ),
        (
            {"adjacent_area_radius_m": 1_000_000},
            {"adjacent_area_radius_m": 1_000_000},
        ),
    ],
)
def test_search_run_request_accepts_bounded_adjacent_radius(
    radius_preferences: dict[str, object],
    expected: dict[str, object],
) -> None:
    payload = {**_strict_district_preferences(), **radius_preferences}

    request = PropertySearchRunStartIn(property_preferences=payload)

    assert request.property_preferences == {**_strict_district_preferences(), **expected}


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("adjacent_area_radius_value", True),
        ("adjacent_area_radius_value", "not-a-number"),
        ("adjacent_area_radius_value", float("nan")),
        ("adjacent_area_radius_value", float("inf")),
        ("adjacent_area_radius_value", -1),
        ("adjacent_area_radius_m", False),
        ("adjacent_area_radius_m", "not-a-number"),
        ("adjacent_area_radius_m", float("nan")),
        ("adjacent_area_radius_m", float("inf")),
        ("adjacent_area_radius_m", -1),
    ],
)
def test_search_run_request_rejects_invalid_adjacent_radius_numbers(
    field: str,
    invalid_value: object,
) -> None:
    with pytest.raises(ValidationError):
        PropertySearchRunStartIn(property_preferences={field: invalid_value})


@pytest.mark.parametrize(
    "radius_preferences",
    [
        {"adjacent_area_radius_m": 1_000_001},
        {"adjacent_area_radius_value": 1_000_001, "adjacent_area_radius_unit": "m"},
        {"adjacent_area_radius_value": 1000.001, "adjacent_area_radius_unit": "km"},
    ],
)
def test_search_run_request_enforces_one_adjacent_radius_meter_cap(
    radius_preferences: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="adjacent_area_radius_(m|value)_out_of_range"):
        PropertySearchRunStartIn(property_preferences=radius_preferences)


@pytest.mark.parametrize("unit", ["mi", "meters", 1, True])
def test_search_run_request_rejects_unsupported_adjacent_radius_unit(unit: object) -> None:
    with pytest.raises(ValidationError, match="adjacent_area_radius_unit_invalid"):
        PropertySearchRunStartIn(
            property_preferences={
                "adjacent_area_radius_value": 1,
                "adjacent_area_radius_unit": unit,
            }
        )


@pytest.mark.parametrize(
    "radius_preferences",
    [
        {},
        {"adjacent_area_radius_m": ""},
        {"adjacent_area_radius_value": "", "adjacent_area_radius_unit": ""},
        {"adjacent_area_radius_value": None, "adjacent_area_radius_unit": None},
    ],
)
def test_search_run_request_preserves_absent_and_empty_adjacent_radius(
    radius_preferences: dict[str, object],
) -> None:
    original = dict(radius_preferences)

    request = PropertySearchRunStartIn(property_preferences=radius_preferences)

    assert request.property_preferences == original
    assert radius_preferences == original
