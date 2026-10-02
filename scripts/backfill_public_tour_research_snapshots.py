#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EA_DIR = REPO_ROOT / "ea"
if str(EA_DIR) not in sys.path:
    sys.path.insert(0, str(EA_DIR))

from app.product.service import _now_iso


LEGACY_RESEARCH_KEYS = (
    "map_lat",
    "map_lng",
    "street_address",
    "exact_address",
    "address_lines",
    "availability",
    "heating_type",
    "lift",
    "has_floorplan",
    "terrace_area_sqm",
    "building_units",
    "parking_monthly_eur",
    "lease_term_years_max",
    "nearest_transit_m",
    "nearest_tram_bus_m",
    "nearest_subway_m",
    "nearest_subway_name",
    "nearest_supermarket_m",
    "nearest_supermarket_name",
    "nearest_pharmacy_m",
    "nearest_pharmacy_name",
    "nearest_playground_m",
    "nearest_playground_name",
    "nearest_clinic_m",
    "nearest_hospital_m",
    "postal_name",
    "district",
    "rooms",
    "area_sqm",
    "total_rent_eur",
    "state",
)


def _tour_dir() -> Path:
    return Path(str(os.getenv("EA_PUBLIC_TOUR_DIR") or "/docker/property/state/public_property_tours")).expanduser()


def _is_weak(value: object) -> bool:
    if value is None or value is False:
        return True
    if isinstance(value, (int, float)):
        return float(value) <= 0.0
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return not any(not _is_weak(item) for item in value)
    if isinstance(value, dict):
        return not value
    return False


def main() -> int:
    root = _tour_dir()
    updated = 0
    scanned = 0
    for manifest_path in sorted(root.glob("*/tour.json")):
        scanned += 1
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        facts = dict(payload.get("facts") or {})
        if not facts:
            continue
        existing_snapshot = dict(facts.get("listing_research_snapshot") or {}) if isinstance(facts.get("listing_research_snapshot"), dict) else {}
        snapshot = dict(existing_snapshot)
        for key in LEGACY_RESEARCH_KEYS:
            value = facts.get(key)
            if _is_weak(snapshot.get(key)) and not _is_weak(value):
                snapshot[key] = value
        if not snapshot:
            continue
        meta = dict(facts.get("listing_research_meta") or {}) if isinstance(facts.get("listing_research_meta"), dict) else {}
        changed = False
        if snapshot != existing_snapshot:
            facts["listing_research_snapshot"] = snapshot
            changed = True
        if not meta:
            facts["listing_research_meta"] = {
                "captured_at": str(payload.get("generated_at") or _now_iso()).strip(),
                "source_url": str(payload.get("listing_url") or payload.get("property_url") or "").strip(),
                "field_count": len(snapshot),
                "strategy": "legacy_backfill",
            }
            changed = True
        elif int(meta.get("field_count") or 0) != len(snapshot):
            meta["field_count"] = len(snapshot)
            facts["listing_research_meta"] = meta
            changed = True
        if not changed:
            continue
        payload["facts"] = facts
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        updated += 1
    print(json.dumps({"scanned": scanned, "updated": updated}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
