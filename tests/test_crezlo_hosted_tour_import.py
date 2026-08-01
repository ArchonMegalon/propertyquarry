from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.import_crezlo_hosted_tour import import_crezlo_hosted_tour


def _layout() -> dict[str, object]:
    return {
        "contract_name": "propertyquarry.floorplan_analysis.v2",
        "review_status": "approved",
        "room_count": 3,
        "rooms": [
            {"id": "entrance-vestibule", "components": [{"x": 0, "z": 0, "width": 2, "depth": 2}]},
            {"id": "living-kitchen", "components": [{"x": 2, "z": 0, "width": 4, "depth": 3}]},
            {"id": "balcony-loggia", "components": [{"x": 6, "z": 0, "width": 2, "depth": 2}]},
        ],
        "doorway_edges": [["entrance-vestibule", "living-kitchen"], ["living-kitchen", "balcony-loggia"]],
        "source_geometry": {
            "portals": [
                {"id": "entrance-to-living", "room_ids": ["entrance-vestibule", "living-kitchen"]},
                {"id": "balcony-door", "room_ids": ["living-kitchen", "balcony-loggia"]},
            ]
        },
        "round_trip": {"status": "pass"},
    }


def _receipt(slug: str, floorplan: Path) -> dict[str, object]:
    floorplan_hash = hashlib.sha256(floorplan.read_bytes()).hexdigest()
    return {
        "schema": "propertyquarry.crezlo_source_provenance.v1",
        "status": "pass",
        "provider": "crezlo",
        "target_slug": slug,
        "hosted_url": "https://ea-property-tours-1781067093.crezlotours.com/tours/real-tour",
        "authorization_status": "approved",
        "capture": {
            "representation_kind": "captured_360",
            "scene_count": 3,
            "navigation_hotspot_count": 2,
            "covered_space_count": 3,
            "scene_graph_connected": True,
            "all_scenes_reachable": True,
        },
        "floorplan": {
            "contract_name": "propertyquarry.floorplan_analysis.v2",
            "review_status": "approved",
            "analysis_sha256": floorplan_hash,
            "source_room_count": 3,
            "source_portal_ids": ["entrance-to-living", "balcony-door"],
            "alignment_verified": True,
            "geometry_receipt_verified": True,
        },
        "browser": {
            "status": "pass",
            "rendered_viewer": True,
            "anonymous_http_status": 200,
            "drag_look_verified": True,
            "scene_navigation_verified": True,
            "desktop_viewer_verified": True,
            "mobile_viewer_verified": True,
            "touch_look_verified": True,
            "browser_receipt_sha256": "a" * 64,
        },
        "review": {"property_match": "pass", "visual_match": "pass", "spatial_capture_match": "pass"},
    }


def _bundle(tmp_path: Path, slug: str) -> Path:
    bundle = tmp_path / "public" / slug
    bundle.mkdir(parents=True)
    (bundle / "tour.json").write_text(json.dumps({"slug": slug, "title": "Measured tour"}), encoding="utf-8")
    return bundle


def test_crezlo_import_requires_source_geometry_and_writes_private_first_party_receipt(tmp_path: Path) -> None:
    slug = "crezlo-measured-tour"
    bundle = _bundle(tmp_path, slug)
    floorplan = tmp_path / "floorplan-analysis.json"
    floorplan.write_text(json.dumps(_layout(), separators=(",", ":")), encoding="utf-8")
    receipt = tmp_path / "crezlo-provenance.json"
    receipt.write_text(json.dumps(_receipt(slug, floorplan)), encoding="utf-8")

    result = import_crezlo_hosted_tour(
        slug=slug,
        receipt_path=receipt,
        floorplan_path=floorplan,
        public_tour_dir=tmp_path / "public",
    )

    assert result["control_url"] == f"/tours/{slug}/control/crezlo"
    manifest = json.loads((bundle / "tour.json").read_text(encoding="utf-8"))
    private = json.loads((bundle / "tour.private.json").read_text(encoding="utf-8"))
    assert manifest["control_mode"] == "crezlo"
    assert private["crezlo_source_provenance"]["floorplan"]["analysis_sha256"] == hashlib.sha256(floorplan.read_bytes()).hexdigest()
    assert private["crezlo_public_url"].startswith("https://ea-property-tours-")


def test_crezlo_import_rejects_hallucinated_provider_render(tmp_path: Path) -> None:
    slug = "crezlo-rejects-hallucination"
    _bundle(tmp_path, slug)
    floorplan = tmp_path / "floorplan-analysis.json"
    floorplan.write_text(json.dumps(_layout()), encoding="utf-8")
    receipt = _receipt(slug, floorplan)
    receipt["capture"] = {**dict(receipt["capture"]), "representation_kind": "ai_reconstruction"}
    receipt_path = tmp_path / "crezlo-provenance.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SystemExit, match="crezlo_capture_provenance_unverified"):
        import_crezlo_hosted_tour(
            slug=slug,
            receipt_path=receipt_path,
            floorplan_path=floorplan,
            public_tour_dir=tmp_path / "public",
        )
