from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from scripts.propertyquarry_floorplan_analyzer import (
    FloorplanAnalysisError,
    analyze_floorplan,
    compare_constructed_floorplan,
    render_derived_floorplan,
)


def _spec() -> dict[str, object]:
    return {
        "rooms": [
            {
                "id": "living",
                "label": "Living · 12 m²",
                "kind": "interior",
                "scene_id": "living",
                "floorplan_bounds_pct": {"x": 10, "y": 10, "width": 40, "height": 40},
                "area_m2": 12,
                "dimension_label": "3 × 4 m",
                "components": [{"x": 0, "z": 0, "width": 3, "depth": 4}],
                "dimension_evidence": [
                    {"text": "300", "method": "operator_dimension_line", "confidence": 0.99},
                    {"text": "400", "method": "operator_dimension_line", "confidence": 0.99},
                ],
            },
            {
                "id": "terrace",
                "label": "Terrace · 4 m²",
                "kind": "exterior",
                "scene_id": "terrace",
                "floorplan_bounds_pct": {"x": 50, "y": 10, "width": 20, "height": 40},
                "area_m2": 4,
                "dimension_label": "2 × 2 m",
                "components": [{"x": 3, "z": 0, "width": 2, "depth": 2}],
                "dimension_evidence": [
                    {"text": "200", "method": "operator_dimension_line", "confidence": 0.99},
                ],
            },
        ],
        "doorway_edges": [["living", "terrace"]],
    }


def test_analysis_requires_source_linked_high_confidence_dimensions(tmp_path: Path) -> None:
    source = tmp_path / "plan.png"
    Image.new("RGB", (900, 700), "white").save(source)
    result = analyze_floorplan(source, specification=_spec(), output_dir=tmp_path / "proof")
    assert result["contract_name"] == "propertyquarry.floorplan_analysis.v2"
    assert result["room_count"] == 2
    assert result["minimum_dimension_confidence"] == 0.99
    assert (tmp_path / "proof" / "floorplan-analysis.json").is_file()

    low_confidence = _spec()
    low_confidence["rooms"][0]["dimension_evidence"][0]["confidence"] = 0.42  # type: ignore[index]
    with pytest.raises(FloorplanAnalysisError, match="confidence_low"):
        analyze_floorplan(source, specification=low_confidence)


def test_derived_floorplan_round_trip_is_a_publishable_gate(tmp_path: Path) -> None:
    source = tmp_path / "plan.png"
    Image.new("RGB", (900, 700), "white").save(source)
    analysis = analyze_floorplan(source, specification=_spec())
    derived = tmp_path / "derived.png"
    render_derived_floorplan(analysis, derived, source_size=(900, 700))
    receipt = compare_constructed_floorplan(
        analysis,
        source=source,
        derived=derived,
        tolerance_m=0.05,
    )
    assert receipt["status"] == "pass"
    assert receipt["contract_name"] == "propertyquarry.floorplan_roundtrip.v1"
    assert receipt["room_count"] == 2

    changed = json.loads(json.dumps(analysis))
    changed["rooms"][0]["area_m2"] = 13  # type: ignore[index]
    with pytest.raises(FloorplanAnalysisError, match="roundtrip_mismatch"):
        compare_constructed_floorplan(changed, source=source, derived=derived, tolerance_m=0.05)
