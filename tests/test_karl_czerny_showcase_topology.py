from __future__ import annotations

from scripts import build_propertyquarry_karl_czerny_showcase_bundle as builder


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
    }
    assert frozenset(("bathroom", "living-kitchen")) not in doorway_edges

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
