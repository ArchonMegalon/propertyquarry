#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connected_component_count(location_rows: list[dict[str, Any]]) -> int:
    ids = {str(row.get("id") or "").strip() for row in location_rows}
    ids.discard("")
    adjacency: dict[str, set[str]] = {location_id: set() for location_id in ids}
    for row in location_rows:
        location_id = str(row.get("id") or "").strip()
        if location_id not in adjacency:
            continue
        for neighbor in list(row.get("neighbors") or []):
            neighbor_id = str(neighbor or "").strip()
            if neighbor_id not in adjacency:
                continue
            adjacency[location_id].add(neighbor_id)
            adjacency[neighbor_id].add(location_id)
    remaining = set(ids)
    components = 0
    while remaining:
        components += 1
        stack = [remaining.pop()]
        while stack:
            node = stack.pop()
            for neighbor in adjacency[node] & remaining:
                remaining.remove(neighbor)
                stack.append(neighbor)
    return components


def build_publication_contract(
    topology: dict[str, Any],
    *,
    source_path: Path,
    checked_at: str = "",
) -> dict[str, object]:
    model_sid = str(topology.get("model_sid") or "").strip()
    locations = [dict(row) for row in list(topology.get("locations") or []) if isinstance(row, dict)]
    if not model_sid or len(locations) < 2:
        raise RuntimeError("matterport_publication_topology_invalid")
    if any(str(dict(row.get("model") or {}).get("id") or "").strip() != model_sid for row in locations):
        raise RuntimeError("matterport_publication_model_mismatch")
    component_count = _connected_component_count(locations)
    if component_count != 1:
        raise RuntimeError("matterport_publication_topology_disconnected")
    room_ids = {
        str(dict(row.get("room") or {}).get("id") or "").strip()
        for row in locations
    }
    room_ids.discard("")
    navigation_edges = {
        tuple(sorted((str(row.get("id") or "").strip(), str(neighbor or "").strip())))
        for row in locations
        for neighbor in list(row.get("neighbors") or [])
        if str(row.get("id") or "").strip() and str(neighbor or "").strip()
    }
    if len(room_ids) < 2 or len(navigation_edges) < len(locations) - 1:
        raise RuntimeError("matterport_publication_spatial_coverage_insufficient")

    available_until: list[datetime] = []
    for row in locations:
        skyboxes = [
            dict(item)
            for item in list(dict(row.get("pano") or {}).get("skyboxes") or [])
            if isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "available"
        ]
        location_expirations = [
            parsed
            for parsed in (_parse_timestamp(item.get("validUntil")) for item in skyboxes)
            if parsed is not None
        ]
        if not location_expirations:
            raise RuntimeError("matterport_publication_available_skybox_missing")
        available_until.append(max(location_expirations))
    asset_valid_until = min(available_until)
    source_checked_at = _parse_timestamp(checked_at)
    if source_checked_at is None:
        source_checked_at = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc)
    if asset_valid_until <= source_checked_at:
        raise RuntimeError("matterport_publication_assets_expired_at_capture")
    return {
        "contract_name": "propertyquarry.matterport_model_publication.v1",
        "status": "pass",
        "model_sid": model_sid,
        "model_available": True,
        "checked_at": source_checked_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "asset_valid_until": asset_valid_until.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "capture_asset_valid_until": asset_valid_until.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "proof_valid_until": (source_checked_at + timedelta(hours=24)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "enabled_sweep_count": len(locations),
        "connected_component_count": component_count,
        "available_sweep_count": len(available_until),
        "room_count": len(room_ids),
        "navigation_edge_count": len(navigation_edges),
        "source_kind": "matterport_topology_capture",
        "source_sha256": _sha256(source_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize a private Matterport publication proof.")
    parser.add_argument("--topology", required=True)
    parser.add_argument("--checked-at", default="")
    parser.add_argument("--write", required=True)
    args = parser.parse_args()

    source_path = Path(args.topology).expanduser().resolve()
    topology = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(topology, dict):
        raise RuntimeError("matterport_publication_json_object_required")
    contract = build_publication_contract(
        dict(topology),
        source_path=source_path,
        checked_at=str(args.checked_at or "").strip(),
    )
    output_path = Path(args.write).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(contract, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
