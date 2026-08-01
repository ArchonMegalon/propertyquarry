from __future__ import annotations

import copy
from pathlib import Path

import pytest
from PIL import Image

from scripts import build_propertyquarry_karl_czerny_showcase_bundle as builder
from scripts.propertyquarry_floorplan_analyzer import (
    FloorplanAnalysisError,
    analyze_floorplan,
)


def test_karl_czerny_showcase_matches_source_floorplan_topology() -> None:
    doorway_edges = {
        frozenset((left_room_id, right_room_id))
        for left_room_id, right_room_id in builder.DOORWAY_EDGES
    }

    assert doorway_edges == {
        frozenset(("terrace", "primary-bedroom")),
        frozenset(("primary-bedroom", "living-kitchen")),
        frozenset(("separate-wc", "living-kitchen")),
        frozenset(("bathroom", "circulation-hall")),
        frozenset(("circulation-hall", "living-kitchen")),
        frozenset(("circulation-hall", "guest-bedroom")),
        frozenset(("living-kitchen", "entrance-vestibule")),
        frozenset(("balcony-loggia", "living-kitchen")),
    }
    assert frozenset(("bathroom", "living-kitchen")) not in doorway_edges
    assert frozenset(("terrace", "living-kitchen")) not in doorway_edges

    room_by_scene_id = {
        scene_id: room_id
        for (
            room_id,
            _label,
            scene_id,
            _kind,
            _x,
            _y,
            _width,
            _height,
        ) in builder.SPATIAL_ROOMS
        if scene_id
    }
    assert room_by_scene_id["hall"] == "entrance-vestibule"
    assert any(
        room_id == "circulation-hall"
        and scene_id == ""
        and kind == "unavailable"
        for (
            room_id,
            _label,
            scene_id,
            kind,
            _x,
            _y,
            _width,
            _height,
        ) in builder.SPATIAL_ROOMS
    )

    for source_scene_id, hotspots in builder.HOTSPOTS.items():
        for _label, target_scene_id, _yaw, via_room_ids in hotspots:
            room_path = (
                room_by_scene_id[source_scene_id],
                *via_room_ids,
                room_by_scene_id[target_scene_id],
            )
            assert all(
                frozenset((left_room_id, right_room_id)) in doorway_edges
                for left_room_id, right_room_id in zip(
                    room_path,
                    room_path[1:],
                )
            )

    assert builder.HOTSPOTS["hall"] == (
        ("Continue to Wohnküche", "living-kitchen", 154, ()),
    )
    assert builder.HOTSPOTS["bath"][0][3] == ("circulation-hall",)
    assert next(
        route
        for route in builder.HOTSPOTS["living-kitchen"]
        if route[1] == "bath"
    )[3] == ("circulation-hall",)

    source_rooms = {str(room["id"]): room for room in builder._ANALYSIS_ROOMS}
    assert source_rooms["terrace"]["shape"] == "rectangle"
    assert len(source_rooms["terrace"]["components"]) == 1
    assert source_rooms["living-kitchen"]["floorplan_bounds_pct"] == {
        "x": 44.2,
        "y": 59.5,
        "width": 17.8,
        "height": 18.5,
    }
    assert source_rooms["living-kitchen"]["source_bbox_px"] == {
        "x": 795,
        "y": 780,
        "width": 320,
        "height": 245,
    }
    assert source_rooms["balcony-loggia"]["shape"] == "rectangle"
    assert source_rooms["bathroom"]["components"] == [
        {"x": 5.335, "z": 0.584, "width": 2.4, "depth": 2.066667}
    ]
    assert source_rooms["circulation-hall"]["components"] == [
        {"x": 7.735, "z": 0.0, "width": 1.8, "depth": 1.944444}
    ]
    assert source_rooms["guest-bedroom"]["components"] == [
        {"x": 9.535, "z": 0.584, "width": 4.6, "depth": 3.313043}
    ]
    assert builder._ANALYSIS_SPEC["boundary_adjacency"] == {
        "required": [["balcony-loggia", "living-kitchen"]],
        "forbidden": [["living-kitchen", "terrace"], ["balcony-loggia", "terrace"]],
    }
    source_geometry = builder._ANALYSIS_SPEC["source_geometry"]
    assert source_geometry["contract_name"] == "propertyquarry.floorplan_source_geometry.v1"
    source_geometry_rooms = {
        str(room["id"]): room for room in source_geometry["rooms"]
    }
    assert len(source_geometry_rooms["entrance-vestibule"]["components_px"]) == 2
    assert source_geometry_rooms["living-kitchen"]["components_px"] == [
        {"x": 795, "y": 780, "width": 320, "height": 245}
    ]
    source_portals = {
        str(portal["id"]): portal for portal in source_geometry["portals"]
    }
    assert source_portals["entrance-to-living"]["room_ids"] == [
        "entrance-vestibule",
        "living-kitchen",
    ]
    assert source_portals["entrance-to-living"]["room_sides"] == {
        "entrance-vestibule": "north",
        "living-kitchen": "south",
    }
    assert source_portals["entrance-exit-gate"]["kind"] == "exit_gate"
    assert source_portals["entrance-exit-gate"]["target_room_id"] == "outside"

    measured_areas = {
        room_id: float(geometry["area_m2"])
        for room_id, geometry in builder.MEASURED_ROOM_GEOMETRY.items()
    }
    assert measured_areas == {
        "terrace": 7.78,
        "primary-bedroom": 16.86,
        "separate-wc": 1.36,
        "bathroom": 4.96,
        "circulation-hall": 3.50,
        "guest-bedroom": 15.24,
        "living-kitchen": 30.33,
        "entrance-vestibule": 18.80,
        "balcony-loggia": 4.38,
    }
    for room_id, geometry in builder.MEASURED_ROOM_GEOMETRY.items():
        components = geometry.get("components", (geometry,))
        assert abs(
            sum(
                float(component["width"]) * float(component["depth"])
                for component in components
            )
            - float(geometry["area_m2"])
        ) < 0.01


def test_source_pixel_envelopes_drive_reviewed_plan_bounds(tmp_path: Path) -> None:
    source = tmp_path / "floorplan.webp"
    Image.new("RGB", (1800, 1310), "white").save(source, format="WEBP")
    analysis = analyze_floorplan(source, specification=builder._ANALYSIS_SPEC)
    living = next(room for room in analysis["rooms"] if room["id"] == "living-kitchen")
    assert living["floorplan_bounds_pct"] == {
        "x": 44.166667,
        "y": 59.541985,
        "width": 17.777778,
        "height": 18.70229,
    }

    invalid_spec = copy.deepcopy(builder._ANALYSIS_SPEC)
    invalid_spec["rooms"][6]["source_bbox_px"]["x"] = 1200
    with pytest.raises(FloorplanAnalysisError, match="floorplan_source_bbox_drift:living-kitchen"):
        analyze_floorplan(source, specification=invalid_spec)
