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
        frozenset(("living-kitchen", "vorraum")),
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
    assert room_by_scene_id["vorraum"] == "vorraum"
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

    assert builder.HOTSPOTS["vorraum"] == (
        ("Continue from VR / Vorraum to Wohnküche", "living-kitchen", 154, ()),
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
        "width": 40.0,
        "height": 18.5,
    }
    assert source_rooms["living-kitchen"]["source_bbox_px"] == {
        "x": 795,
        "y": 780,
        "width": 720,
        "height": 245,
    }
    assert source_rooms["balcony-loggia"]["shape"] == "rectangle"
    living_bbox = source_rooms["living-kitchen"]["source_bbox_px"]
    balcony_bbox = source_rooms["balcony-loggia"]["source_bbox_px"]
    assert living_bbox["x"] + living_bbox["width"] == 1515
    assert balcony_bbox["x"] == 1360
    assert balcony_bbox["x"] < living_bbox["x"] + living_bbox["width"]
    assert balcony_bbox["y"] < living_bbox["y"] + living_bbox["height"]
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
        "forbidden": [
            ["living-kitchen", "terrace"],
            ["balcony-loggia", "terrace"],
            ["balcony-loggia", "vorraum"],
        ],
    }
    source_geometry = builder._ANALYSIS_SPEC["source_geometry"]
    assert source_geometry["contract_name"] == "propertyquarry.floorplan_source_geometry.v1"
    source_geometry_rooms = {
        str(room["id"]): room for room in source_geometry["rooms"]
    }
    assert builder._ANALYSIS_SPEC["entry_room_id"] == "vorraum"
    assert "entrance-vestibule" not in source_rooms
    assert len(source_geometry_rooms["vorraum"]["components_px"]) == 2
    assert source_geometry_rooms["living-kitchen"]["components_px"] == [
        {"x": 795, "y": 780, "width": 720, "height": 245}
    ]
    source_portals = {
        str(portal["id"]): portal for portal in source_geometry["portals"]
    }
    assert source_portals["vorraum-to-living"]["room_ids"] == [
        "vorraum",
        "living-kitchen",
    ]
    assert source_portals["vorraum-to-living"]["room_sides"] == {
        "vorraum": "north",
        "living-kitchen": "south",
    }
    assert source_portals["entrance-exit-gate"]["kind"] == "exit_gate"
    assert source_portals["entrance-exit-gate"]["room_ids"] == ["vorraum", "outside"]
    assert source_portals["entrance-exit-gate"]["room_sides"] == {"vorraum": "west"}
    assert source_portals["entrance-exit-gate"]["target_room_id"] == "outside"
    assert source_portals["entrance-exit-gate"]["center_px"] == {"x": 795, "y": 1100}
    assert source_portals["entrance-exit-gate"]["label"] == (
        "Apartment entrance · VR / Vorraum ↔ Stairwell 3"
    )
    assert source_portals["entrance-exit-gate"]["target_label"] == "Stairwell 3"
    assert source_portals["vorraum-to-living"]["label"] == "Door · Wohnküche"
    assert source_portals["living-to-balcony-loggia"] == {
        "id": "living-to-balcony-loggia",
        "label": "Door · Balkon / Loggia",
        "kind": "door",
        "room_ids": ["living-kitchen", "balcony-loggia"],
        "room_sides": {"living-kitchen": "south", "balcony-loggia": "west"},
        "center_px": {"x": 1360, "y": 1055},
        "width_px": 80,
        "target_room_id": "balcony-loggia",
    }
    portal_topology = builder._showcase_portal_topology()
    assert portal_topology == {
        "balcony_portal_id": "living-to-balcony-loggia",
        "exit_portal_id": "entrance-exit-gate",
        "source_pixel_separation": 565,
        "status": "pass",
    }

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
        "vorraum": 18.80,
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


def test_karl_czerny_viewer_uses_component_local_portal_mapping() -> None:
    viewer = (Path(__file__).resolve().parents[1] / "ea/app/api/routes/public_tours.py").read_text(
        encoding="utf-8"
    )
    assert "const sourcePointToWorld = (roomId, point, side)" in viewer
    assert "const canonicalPortalWorld = portal" in viewer
    assert "const components = measuredComponents.length ? measuredComponents" in viewer
    assert "Entrance / exit · Stairwell 3" in viewer
    assert "const isLoggiaDoor" in viewer
    assert "? 'Loggia door'" in viewer
    assert "Doorway: ${connectedLabels.join(' ↔ ')}" in viewer


def test_source_pixel_envelopes_drive_reviewed_plan_bounds(tmp_path: Path) -> None:
    source = tmp_path / "floorplan.webp"
    Image.new("RGB", (1800, 1310), "white").save(source, format="WEBP")
    analysis = analyze_floorplan(source, specification=builder._ANALYSIS_SPEC)
    living = next(room for room in analysis["rooms"] if room["id"] == "living-kitchen")
    assert living["floorplan_bounds_pct"] == {
        "x": 44.166667,
        "y": 59.541985,
        "width": 40.0,
        "height": 18.70229,
    }

    invalid_spec = copy.deepcopy(builder._ANALYSIS_SPEC)
    invalid_spec["rooms"][6]["source_bbox_px"]["x"] = 1000
    with pytest.raises(FloorplanAnalysisError, match="floorplan_source_bbox_drift:living-kitchen"):
        analyze_floorplan(source, specification=invalid_spec)


def test_diorama_is_rendered_from_reviewed_source_geometry(tmp_path: Path) -> None:
    source = tmp_path / "floorplan.webp"
    source_projection = tmp_path / "diorama-source-floorplan.png"
    preview = tmp_path / "diorama-preview.png"
    Image.new("RGB", (1800, 1310), "white").save(source, format="WEBP")
    analysis = analyze_floorplan(source, specification=builder._ANALYSIS_SPEC)

    receipt = builder._save_source_locked_diorama(
        floorplan_source=source,
        floorplan_analysis=analysis,
        source_crop_target=source_projection,
        target=preview,
    )

    assert receipt["status"] == "pass"
    assert receipt["renderer_version"] == "propertyquarry_bright_playful_cutaway_v1"
    assert receipt["source_projection_kind"] == (
        "source_pixel_geometry_with_verified_portals"
    )
    assert receipt["source_geometry_contract_name"] == (
        "propertyquarry.floorplan_source_geometry.v1"
    )
    assert receipt["displayed_route_stop_count"] == len(builder.SPATIAL_ROOMS)
    assert receipt["furnished_room_count"] == len(builder.SPATIAL_ROOMS)
    assert all(receipt["checks"].values())
    assert len(receipt["source_projection_sha256"]) == 64
    assert len(receipt["preview_sha256"]) == 64
    assert source_projection.is_file()
    assert preview.is_file()
    with Image.open(preview) as rendered:
        assert rendered.size == (1600, 1100)
