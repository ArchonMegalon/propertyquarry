#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "ea") not in sys.path:
    sys.path.insert(0, str(ROOT / "ea"))

from app.product.property_score_methodology import (  # noqa: E402
    build_property_score_methodology,
    supported_property_score_methodology_languages,
)


REQUIRED_SOURCE_TOKENS = (
    "data.gv.at",
    "climate",
    "air-quality",
    "noise",
    "flood",
    "broadband",
    "school",
    "childcare",
)
REQUIRED_DISTRICT_POLICY_TOKENS = (
    "selected district is only a pass/fail gate",
    "not a soft preference or district reward",
    "Being central, at the edge, or on a border inside an allowed district is not rewarded",
    "contradicted postal code",
)
REQUIRED_GERMAN_DISTRICT_TOKENS = (
    "Randlage",
    "Bezirksgrenze",
    "nicht belohnt",
    "widersprechende PLZ",
)


def _blob(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_blob(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_blob(item) for item in value)
    return str(value or "")


def _payload_for(language_code: str) -> dict[str, Any]:
    return build_property_score_methodology(
        language_code=language_code,
        country_code="AT",
        candidate={
            "fit_score": 62,
            "match_reasons": ["Verified costs, floorplan, and 360 evidence raise confidence."],
            "mismatch_reasons": ["Heating detail still needs confirmation before a final decision."],
        },
    )


def build_bts_methodology_contract_receipt() -> dict[str, object]:
    failures: list[str] = []
    languages = tuple(supported_property_score_methodology_languages())
    if "en" not in languages:
        failures.append("score methodology must include English BTS/source copy")
    if "de" not in languages:
        failures.append("score methodology must include German BTS/source copy")

    english = _payload_for("en")
    german = _payload_for("de")
    english_blob = _blob(english)
    german_blob = _blob(german)

    source_sections = list(english.get("source_sections") or [])
    if len(source_sections) < 5:
        failures.append("BTS methodology must expose at least five source sections")
    for token in REQUIRED_SOURCE_TOKENS:
        if token not in english_blob:
            failures.append(f"BTS methodology missing official source token: {token}")

    detail_rows = [row for row in list(english.get("calculation_detail_rows") or []) if isinstance(row, dict)]
    detail_blob = _blob(detail_rows)
    for token in REQUIRED_DISTRICT_POLICY_TOKENS[:2]:
        if token not in detail_blob:
            failures.append(f"selected-district policy copy missing: {token}")
    location_rows = [row for row in detail_rows if str(row.get("label") or "") == "Location checked"]
    if not location_rows:
        failures.append("BTS methodology must include a Location checked calculation row")
    else:
        location_row = location_rows[0]
        if str(location_row.get("delta") or "") != "+0":
            failures.append("selected-district location row must stay +0")
        location_blob = _blob(location_row)
        for token in REQUIRED_DISTRICT_POLICY_TOKENS[2:]:
            if token not in location_blob:
                failures.append(f"selected-district policy copy missing: {token}")

    german_location_rows = [
        row
        for row in list(german.get("calculation_detail_rows") or [])
        if isinstance(row, dict) and str(row.get("label") or "") == "Lage gepruft"
    ]
    german_location_blob = _blob(german_location_rows) or german_blob
    for token in REQUIRED_GERMAN_DISTRICT_TOKENS:
        if token not in german_location_blob:
            failures.append(f"German district policy copy missing: {token}")

    return {
        "schema": "propertyquarry.bts_methodology_contract_receipt.v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "status": "pass" if not failures else "fail",
        "language_count": len(languages),
        "languages": list(languages),
        "source_section_count": len(source_sections),
        "required_source_tokens": list(REQUIRED_SOURCE_TOKENS),
        "required_district_policy_tokens": list(REQUIRED_DISTRICT_POLICY_TOKENS),
        "failure_count": len(failures),
        "failures": failures,
        "note": "Verifies BTS score-PDF provenance, official data-source copy, and selected-district no-reward scoring policy.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check PropertyQuarry BTS/source methodology contract.")
    parser.add_argument("--write", default="", help="Optional path for a JSON receipt.")
    args = parser.parse_args()

    receipt = build_bts_methodology_contract_receipt()
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failures = list(receipt.get("failures") or [])
    if failures:
        print("property BTS methodology contract check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("ok: property BTS methodology contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
