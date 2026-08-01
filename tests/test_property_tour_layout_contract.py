from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.property_tour_layout_contract import (
    LayoutContractError,
    load_layout_contract,
    room_ids_in_walk_order,
    source_bounds_m,
    validate_walkable_scene,
)


def _contract() -> dict[str, object]:
    return {
        "contract_name": "propertyquarry.floorplan_analysis.v2",
        "review_status": "approved",
        "room_count": 3,
        "rooms": [
            {"id": "living-kitchen", "components": [{"x": 2, "z": 2, "width": 4, "depth": 3}]},
            {"id": "entrance-vestibule", "components": [{"x": 0, "z": 2, "width": 2, "depth": 3}]},
            {"id": "balcony-loggia", "components": [{"x": 6, "z": 2, "width": 2, "depth": 2}]},
        ],
        "doorway_edges": [["entrance-vestibule", "living-kitchen"], ["living-kitchen", "balcony-loggia"]],
        "source_geometry": {
            "portals": [
                {"id": "entrance-to-living", "room_ids": ["entrance-vestibule", "living-kitchen"]},
                {"id": "entrance-exit-gate", "room_ids": ["entrance-vestibule", "outside"]},
            ]
        },
        "round_trip": {"status": "pass"},
    }


def test_layout_contract_walk_order_starts_at_entrance_and_preserves_exit_gate() -> None:
    payload = _contract()
    assert room_ids_in_walk_order(payload) == [
        "entrance-vestibule",
        "living-kitchen",
        "balcony-loggia",
    ]
    assert source_bounds_m(payload) == (8.0, 5.0)
    scene = {
        "route": [
            {"source_room_id": "entrance-vestibule"},
            {"source_room_id": "living-kitchen"},
            {"source_room_id": "balcony-loggia"},
        ],
        "portals": payload["source_geometry"]["portals"],  # type: ignore[index]
    }
    assert validate_walkable_scene(scene, payload) == []


def test_layout_contract_rejects_route_drift() -> None:
    payload = _contract()
    scene = {
        "route": [
            {"source_room_id": "living-kitchen"},
            {"source_room_id": "entrance-vestibule"},
            {"source_room_id": "balcony-loggia"},
        ],
        "portals": payload["source_geometry"]["portals"],  # type: ignore[index]
    }
    assert "route_room_order_mismatch" in validate_walkable_scene(scene, payload)


def test_layout_contract_loader_requires_reviewed_v2(tmp_path: Path) -> None:
    path = tmp_path / "layout.json"
    invalid = _contract()
    invalid["review_status"] = "draft"
    path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(LayoutContractError, match="floorplan_analysis_not_reviewed"):
        load_layout_contract(path)
