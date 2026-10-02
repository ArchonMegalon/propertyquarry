#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _distance(left: dict[str, object], right: dict[str, object]) -> float:
    left_position = dict(left.get("position") or {})
    right_position = dict(right.get("position") or {})
    return math.sqrt(
        sum(
            (
                float(right_position.get(axis) or 0.0)
                - float(left_position.get(axis) or 0.0)
            )
            ** 2
            for axis in ("x", "y", "z")
        )
    )


def build_sdk_walkthrough_contract(
    route_payload: dict[str, object],
    *,
    minimum_transition_ms: int = 1200,
    maximum_transition_ms: int = 4200,
    transition_ms_per_meter: float = 450.0,
) -> dict[str, object]:
    if str(route_payload.get("status") or "").strip().lower() != "pass":
        raise RuntimeError("matterport_route_not_passing")
    try:
        edit_counts = {
            key: int(route_payload.get(key) or 0)
            for key in ("cut_count", "dissolve_count", "teleport_count")
        }
    except (TypeError, ValueError) as error:
        raise RuntimeError("matterport_route_edit_counts_invalid") from error
    if any(edit_counts.values()):
        raise RuntimeError("matterport_route_contains_internal_edits")

    model_sid = str(route_payload.get("model_sid") or "").strip()
    raw_nodes = route_payload.get("route")
    if not model_sid or not isinstance(raw_nodes, list) or len(raw_nodes) < 2:
        raise RuntimeError("matterport_route_shape_invalid")
    nodes = [dict(node) for node in raw_nodes if isinstance(node, dict)]
    if len(nodes) != len(raw_nodes):
        raise RuntimeError("matterport_route_node_invalid")
    walkable_room_ids = {
        str(room_id or "").strip()
        for room_id in list(route_payload.get("walkable_room_ids") or [])
        if str(room_id or "").strip()
    }
    if not walkable_room_ids:
        raise RuntimeError("matterport_walkable_rooms_missing")

    route: list[dict[str, object]] = []
    covered_room_ids: set[str] = set()
    total_distance_m = 0.0
    for index, node in enumerate(nodes):
        sweep_id = str(node.get("id") or "").strip()
        room_id = str(node.get("room_id") or "").strip()
        try:
            sweep_index = int(node.get("index"))
        except (TypeError, ValueError) as error:
            raise RuntimeError("matterport_route_sweep_index_invalid") from error
        if not sweep_id or not room_id:
            raise RuntimeError("matterport_route_sweep_identity_missing")
        transition_distance_m = 0.0
        if index:
            source = nodes[index - 1]
            source_neighbors = {
                str(neighbor or "").strip()
                for neighbor in list(source.get("neighbors") or [])
            }
            if sweep_id not in source_neighbors:
                raise RuntimeError("matterport_route_edge_not_declared")
            transition_distance_m = _distance(source, node)
            total_distance_m += transition_distance_m
        transition_time_ms = min(
            max(
                round(max(transition_distance_m, 1.0) * transition_ms_per_meter),
                minimum_transition_ms,
            ),
            maximum_transition_ms,
        )
        route.append(
            {
                "sweep_id": sweep_id,
                "sweep_index": sweep_index,
                "room_id": room_id,
                "transition_distance_m": round(transition_distance_m, 3),
                "transition_time_ms": transition_time_ms,
            }
        )
        covered_room_ids.add(room_id)

    missing_room_ids = sorted(walkable_room_ids - covered_room_ids)
    if missing_room_ids:
        raise RuntimeError("matterport_route_walkable_room_coverage_missing")
    first_index = int(route[0]["sweep_index"])
    return {
        "contract_name": "propertyquarry.matterport_sdk_walkthrough.v1",
        "status": "pass",
        "generated_at": _utc_now(),
        "source_contract_name": str(route_payload.get("contract_name") or ""),
        "model_sid": model_sid,
        **edit_counts,
        "transition": "fly",
        "start_sweep_id": route[0]["sweep_id"],
        "start_sweep_index": first_index,
        "start_ss": first_index + 1,
        "route_node_count": len(route),
        "route_edge_count": len(route) - 1,
        "route_distance_m": round(total_distance_m, 3),
        "walkable_room_count": len(walkable_room_ids),
        "walkable_room_ids": sorted(walkable_room_ids),
        "covered_room_ids": sorted(covered_room_ids & walkable_room_ids),
        "missing_room_ids": [],
        "route": route,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Materialize a private Matterport SDK FLY walkthrough contract."
    )
    parser.add_argument("--route", required=True)
    parser.add_argument("--write", required=True)
    parser.add_argument("--minimum-transition-ms", type=int, default=1200)
    parser.add_argument("--maximum-transition-ms", type=int, default=4200)
    parser.add_argument("--transition-ms-per-meter", type=float, default=450.0)
    args = parser.parse_args()
    source_path = Path(args.route).expanduser().resolve()
    output_path = Path(args.write).expanduser().resolve()
    route_payload = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(route_payload, dict):
        raise SystemExit("matterport_route_payload_invalid")
    contract = build_sdk_walkthrough_contract(
        route_payload,
        minimum_transition_ms=max(600, int(args.minimum_transition_ms)),
        maximum_transition_ms=max(int(args.minimum_transition_ms), int(args.maximum_transition_ms)),
        transition_ms_per_meter=max(1.0, float(args.transition_ms_per_meter)),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(contract, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
