"""Source-geometry contract shared by floorplan reconstruction lanes.

The renderer used to infer a handful of semantic camera stops from a bitmap.
That is useful for a visual preview, but it is not sufficient for a tour that
claims to represent a measured apartment.  This module keeps the reviewed
floorplan graph (rooms, measured components, door edges and the exit gate) as
the single source of truth and provides small, dependency-free validators for
the generator and its publication bridge.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class LayoutContractError(ValueError):
    """Raised when a source floorplan contract is missing or inconsistent."""


CONTRACT_NAME = "propertyquarry.floorplan_analysis.v2"


def load_layout_contract(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser()
    if not source.is_file():
        raise LayoutContractError("floorplan_analysis_missing")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LayoutContractError("floorplan_analysis_invalid_json") from exc
    if not isinstance(payload, dict):
        raise LayoutContractError("floorplan_analysis_not_object")
    validate_layout_contract(payload)
    return payload


def validate_layout_contract(payload: dict[str, Any]) -> None:
    if str(payload.get("contract_name") or "").strip() != CONTRACT_NAME:
        raise LayoutContractError("floorplan_analysis_contract_mismatch")
    if str(payload.get("review_status") or "").strip().lower() not in {"approved", "reviewed"}:
        raise LayoutContractError("floorplan_analysis_not_reviewed")
    rooms = payload.get("rooms")
    if not isinstance(rooms, list) or not rooms:
        raise LayoutContractError("floorplan_analysis_rooms_missing")
    declared_count = int(payload.get("room_count") or 0)
    if declared_count != len(rooms):
        raise LayoutContractError("floorplan_analysis_room_count_mismatch")
    room_ids: set[str] = set()
    for room in rooms:
        if not isinstance(room, dict):
            raise LayoutContractError("floorplan_analysis_room_invalid")
        room_id = str(room.get("id") or "").strip()
        if not room_id or room_id in room_ids:
            raise LayoutContractError("floorplan_analysis_room_id_invalid")
        room_ids.add(room_id)
        components = room.get("components")
        if not isinstance(components, list) or not components:
            raise LayoutContractError("floorplan_analysis_room_components_missing")
        for component in components:
            if not isinstance(component, dict):
                raise LayoutContractError("floorplan_analysis_component_invalid")
            for key in ("x", "z", "width", "depth"):
                try:
                    if float(component.get(key)) < 0 and key in {"width", "depth"}:
                        raise ValueError
                except (TypeError, ValueError) as exc:
                    raise LayoutContractError("floorplan_analysis_component_dimension_invalid") from exc
            if float(component.get("width") or 0) <= 0 or float(component.get("depth") or 0) <= 0:
                raise LayoutContractError("floorplan_analysis_component_dimension_invalid")
    for edge in payload.get("doorway_edges") or []:
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            raise LayoutContractError("floorplan_analysis_doorway_edge_invalid")
        if str(edge[0]) not in room_ids or str(edge[1]) not in room_ids:
            raise LayoutContractError("floorplan_analysis_doorway_edge_unknown_room")
    geometry = payload.get("source_geometry")
    if not isinstance(geometry, dict):
        raise LayoutContractError("floorplan_source_geometry_missing")
    portals = geometry.get("portals")
    if not isinstance(portals, list) or not portals:
        raise LayoutContractError("floorplan_source_portals_missing")
    portal_ids: set[str] = set()
    for portal in portals:
        if not isinstance(portal, dict):
            raise LayoutContractError("floorplan_source_portal_invalid")
        portal_id = str(portal.get("id") or "").strip()
        if not portal_id or portal_id in portal_ids:
            raise LayoutContractError("floorplan_source_portal_id_invalid")
        portal_ids.add(portal_id)
        room_refs = portal.get("room_ids")
        if not isinstance(room_refs, list) or not room_refs:
            raise LayoutContractError("floorplan_source_portal_rooms_missing")
        if any(str(room_id) != "outside" and str(room_id) not in room_ids for room_id in room_refs):
            raise LayoutContractError("floorplan_source_portal_unknown_room")
    round_trip = payload.get("round_trip")
    if isinstance(round_trip, dict) and str(round_trip.get("status") or "").strip().lower() not in {"pass", "approved"}:
        raise LayoutContractError("floorplan_analysis_round_trip_failed")


def room_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in list(payload.get("rooms") or []) if isinstance(row, dict)]


def room_ids_in_walk_order(payload: dict[str, Any]) -> list[str]:
    """Traverse the measured doorway graph from the entrance, then append islands."""
    rooms = room_rows(payload)
    room_ids = [str(row.get("id") or "").strip() for row in rooms]
    adjacency: dict[str, list[str]] = {room_id: [] for room_id in room_ids}
    for edge in payload.get("doorway_edges") or []:
        left, right = str(edge[0]), str(edge[1])
        adjacency.setdefault(left, []).append(right)
        adjacency.setdefault(right, []).append(left)
    start = "entrance-vestibule" if "entrance-vestibule" in adjacency else (room_ids[0] if room_ids else "")
    order: list[str] = []
    pending = [start] if start else []
    while pending:
        current = pending.pop(0)
        if current in order:
            continue
        order.append(current)
        pending.extend(neighbor for neighbor in adjacency.get(current, []) if neighbor not in order)
    order.extend(room_id for room_id in room_ids if room_id not in order)
    return order


def source_bounds_m(payload: dict[str, Any]) -> tuple[float, float]:
    max_x = 0.0
    max_z = 0.0
    for room in room_rows(payload):
        for component in room.get("components") or []:
            if not isinstance(component, dict):
                continue
            max_x = max(max_x, float(component.get("x") or 0) + float(component.get("width") or 0))
            max_z = max(max_z, float(component.get("z") or 0) + float(component.get("depth") or 0))
    if max_x <= 0 or max_z <= 0:
        raise LayoutContractError("floorplan_source_bounds_missing")
    return round(max_x, 4), round(max_z, 4)


def validate_walkable_scene(scene: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    """Return stable failures for a generated scene that drifted from source geometry."""
    failures: list[str] = []
    expected = room_ids_in_walk_order(payload)
    actual = [
        str(row.get("source_room_id") or "").strip()
        for row in list(scene.get("route") or [])
        if isinstance(row, dict)
    ]
    if actual != expected:
        failures.append("route_room_order_mismatch")
    if len(actual) != int(payload.get("room_count") or 0):
        failures.append("route_room_count_mismatch")
    portal_ids = {
        str(row.get("id") or "").strip()
        for row in list(scene.get("portals") or [])
        if isinstance(row, dict)
    }
    expected_portal_ids = {
        str(row.get("id") or "").strip()
        for row in list(dict(payload.get("source_geometry") or {}).get("portals") or [])
        if isinstance(row, dict)
    }
    if not expected_portal_ids.issubset(portal_ids):
        failures.append("source_portals_missing")
    if "entrance-exit-gate" in expected_portal_ids and "entrance-exit-gate" not in portal_ids:
        failures.append("entrance_exit_gate_missing")
    return failures
