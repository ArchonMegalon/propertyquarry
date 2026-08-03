"""Source-geometry contract shared by floorplan reconstruction lanes.

The renderer used to infer a handful of semantic camera stops from a bitmap.
That is useful for a visual preview, but it is not sufficient for a tour that
claims to represent a measured apartment.  This module keeps the reviewed
floorplan graph (rooms, measured components, door edges and the exit gate) as
the single source of truth and provides small, dependency-free validators for
the generator and its publication bridge.
"""

from __future__ import annotations

import hashlib
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
    entry_room_id = str(payload.get("entry_room_id") or "").strip()
    if entry_room_id and entry_room_id not in room_ids:
        raise LayoutContractError("floorplan_analysis_entry_room_unknown")
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
    if entry_room_id and not any(
        str(portal.get("kind") or "").strip() == "exit_gate"
        and entry_room_id in {str(value or "").strip() for value in list(portal.get("room_ids") or [])}
        and "outside" in {str(value or "").strip() for value in list(portal.get("room_ids") or [])}
        for portal in portals
        if isinstance(portal, dict)
    ):
        raise LayoutContractError("floorplan_analysis_entry_exit_gate_missing")
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
    declared_entry = str(payload.get("entry_room_id") or "").strip()
    start = (
        declared_entry
        if declared_entry in adjacency
        else "entrance-vestibule"
        if "entrance-vestibule" in adjacency
        else room_ids[0]
        if room_ids
        else ""
    )
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


def source_portal_projection(portal: object) -> dict[str, Any]:
    """Return the canonical, comparable representation of a measured portal.

    Portal ids and room references identify topology, but they do not prove
    that a doorway is in the right wall.  The analyzer supplies the optional
    geometric fields below (and older synthetic contracts may omit them); the
    projection preserves every field that is present so a generated scene
    cannot silently move or relabel a measured door or exit gate.
    """

    if not isinstance(portal, dict):
        return {}
    result: dict[str, Any] = {
        "id": str(portal.get("id") or "").strip(),
        "room_ids": [str(value or "").strip() for value in list(portal.get("room_ids") or [])],
    }
    for key in ("kind", "target_room_id", "label", "target_label"):
        if key in portal:
            result[key] = str(portal.get(key) or "").strip()
    if "room_sides" in portal:
        raw_sides = portal.get("room_sides")
        result["room_sides"] = (
            {str(room_id).strip(): str(side).strip().lower() for room_id, side in raw_sides.items()}
            if isinstance(raw_sides, dict)
            else {}
        )
    if "center_px" in portal:
        raw_center = portal.get("center_px")
        if isinstance(raw_center, dict):
            try:
                result["center_px"] = {
                    "x": int(round(float(raw_center.get("x") or 0))),
                    "y": int(round(float(raw_center.get("y") or 0))),
                }
            except (TypeError, ValueError, OverflowError):
                result["center_px"] = {"x": None, "y": None}
        else:
            result["center_px"] = {}
    if "width_px" in portal:
        try:
            result["width_px"] = int(round(float(portal.get("width_px") or 0)))
        except (TypeError, ValueError, OverflowError):
            result["width_px"] = None
    return result


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


def source_geometry_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical measured geometry projection used by provider receipts.

    A floorplan file hash proves which analysis was reviewed, but it does not
    make it obvious what geometry a hosted provider was checked against.  This
    compact projection makes room identities, measured components, doorway
    edges, portals, and the source bounds explicit and gives that projection a
    deterministic digest.  Importers must derive it from the reviewed contract;
    publishers only accept the resulting receipt.
    """

    validate_layout_contract(payload)
    rooms = [
        {
            "id": str(room.get("id") or "").strip(),
            "components": [
                {
                    key: round(float(component.get(key) or 0), 4)
                    for key in ("x", "z", "width", "depth")
                }
                for component in list(room.get("components") or [])
                if isinstance(component, dict)
            ],
        }
        for room in room_rows(payload)
    ]
    doorway_edges = [
        [str(edge[0]).strip(), str(edge[1]).strip()]
        for edge in list(payload.get("doorway_edges") or [])
        if isinstance(edge, (list, tuple)) and len(edge) == 2
    ]
    portals = [
        source_portal_projection(portal)
        for portal in list(dict(payload.get("source_geometry") or {}).get("portals") or [])
        if isinstance(portal, dict)
    ]
    projection = {
        "contract_name": CONTRACT_NAME,
        "entry_room_id": str(payload.get("entry_room_id") or "").strip(),
        "room_count": int(payload.get("room_count") or 0),
        "room_ids": [row["id"] for row in rooms],
        "rooms": rooms,
        "doorway_edges": doorway_edges,
        "portal_ids": [row["id"] for row in portals],
        "portals": portals,
        "source_bounds_m": list(source_bounds_m(payload)),
    }
    digest = hashlib.sha256(
        json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**projection, "sha256": digest}


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
    expected_portals = [
        source_portal_projection(row)
        for row in list(dict(payload.get("source_geometry") or {}).get("portals") or [])
        if isinstance(row, dict)
    ]
    actual_portals = [
        source_portal_projection(row)
        for row in list(scene.get("portals") or [])
        if isinstance(row, dict)
    ]
    if actual_portals != expected_portals:
        failures.append("source_portal_geometry_mismatch")
    if scene.get("source_geometry_locked") is True:
        width_m, depth_m = source_bounds_m(payload)
        expected_rooms = {
            str(room.get("id") or "").strip(): room
            for room in room_rows(payload)
            if isinstance(room, dict)
        }

        def _components(row: object) -> list[dict[str, float]]:
            if not isinstance(row, dict):
                return []
            return [
                {
                    key: round(float(component.get(key) or 0.0), 4)
                    for key in ("x", "z", "width", "depth")
                }
                for component in list(row.get("components") or row.get("source_components_m") or [])
                if isinstance(component, dict)
            ]

        for collection_key in ("route", "rooms"):
            for row in list(scene.get(collection_key) or []):
                if not isinstance(row, dict):
                    failures.append(f"{collection_key}_source_geometry_row_invalid")
                    continue
                room_id = str(row.get("source_room_id") or "").strip()
                expected_room = expected_rooms.get(room_id)
                if expected_room is None:
                    failures.append(f"{collection_key}_source_room_unknown")
                    continue
                expected_components = _components(expected_room)
                actual_components = _components(row)
                if actual_components != expected_components:
                    failures.append(f"{collection_key}_source_components_mismatch:{room_id}")
                bounds = row.get("source_component_bounds_m")
                if not isinstance(bounds, dict) or not actual_components:
                    failures.append(f"{collection_key}_source_component_bounds_missing:{room_id}")
                    continue
                largest = max(
                    actual_components,
                    key=lambda component: component["width"] * component["depth"],
                )
                expected_bounds = {
                    "x": round(largest["x"] - (width_m * 0.5), 4),
                    "z": round(largest["z"] - (depth_m * 0.5), 4),
                    "width": largest["width"],
                    "depth": largest["depth"],
                }
                observed_bounds = {
                    key: round(float(bounds.get(key) or 0.0), 4)
                    for key in ("x", "z", "width", "depth")
                }
                if observed_bounds != expected_bounds:
                    failures.append(f"{collection_key}_source_component_bounds_mismatch:{room_id}")
        scene_bounds = scene.get("bounds")
        if isinstance(scene_bounds, dict):
            if round(float(scene_bounds.get("width_m") or 0.0), 4) != round(width_m, 4):
                failures.append("source_bounds_width_mismatch")
            if round(float(scene_bounds.get("depth_m") or 0.0), 4) != round(depth_m, 4):
                failures.append("source_bounds_depth_mismatch")
        else:
            failures.append("source_bounds_missing")
    return failures
