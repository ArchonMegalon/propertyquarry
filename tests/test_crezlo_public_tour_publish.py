from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE_PATH = SCRIPTS / "publish_crezlo_public_tours.py"


def _load_module():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("publish_crezlo_public_tours", MODULE_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _walk_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys.update(_walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_walk_keys(item))
    return keys


def test_crezlo_public_tour_payload_uses_public_manifest_schema(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / "scene-01.jpg").write_bytes(b"image")

    payload = {
        "slug": "crezlo-safe-tour",
        "hosted_url": "https://propertyquarry.com/tours/crezlo-safe-tour",
        "title": "Public title",
        "listing_url": "https://portal.example/private-listing",
        "property_url": "https://broker.example/private-property",
        "source_ref": "private-source-ref",
        "external_id": "private-run-key",
        "editor_url": "https://crezlo.example/editor/private",
        "crezlo_public_url": "https://crezlo.example/public/private",
        "brief": {
            "creative_brief": "Operator-only production prompt",
            "call_to_action": "Private CTA",
        },
        "facts": {
            "rooms": 3,
            "address_lines": ["Exact private street 12"],
            "teaser_attributes": ["Lift"],
        },
        "scenes": [
            {
                "name": "Scene",
                "role": "photo",
                "asset_relpath": "scene-01.jpg",
                "source_url": "https://cdn.example/private-original.jpg",
                "property_url": "https://broker.example/private-property",
                "mime_type": "image/jpeg",
            }
        ],
    }

    public_payload = module.public_tour_bundle_payload(payload=payload, bundle_dir=tmp_path)
    keys = _walk_keys(public_payload)
    serialized = str(public_payload)

    assert "slug" in public_payload
    assert public_payload["hosted_url"] == "/tours/crezlo-safe-tour"
    assert "listing_url" not in keys
    assert "property_url" not in keys
    assert "source_url" not in keys
    assert "editor_url" not in keys
    assert "crezlo_public_url" not in keys
    assert "brief" not in keys
    assert "Exact private street" not in serialized
    assert "private-listing" not in serialized
    assert "private-original" not in serialized


def test_crezlo_public_tour_private_receipt_keeps_source_urls_off_manifest_path() -> None:
    module = _load_module()

    receipt = module.private_tour_receipt(
        {
            "principal_id": "owner",
            "listing_url": "https://portal.example/private-listing",
            "property_url": "https://broker.example/private-property",
            "source_ref": "source",
            "external_id": "run",
            "crezlo_public_url": "https://crezlo.example/public/private",
        }
    )

    assert receipt["listing_url"] == "https://portal.example/private-listing"
    assert receipt["property_url"] == "https://broker.example/private-property"
    assert receipt["crezlo_public_url"] == "https://crezlo.example/public/private"


def test_crezlo_publish_gate_rejects_missing_immersive_source_geometry_receipt() -> None:
    gate_path = SCRIPTS / "property_tour_publication_gate.py"
    spec = importlib.util.spec_from_file_location("property_tour_publication_gate", gate_path)
    assert spec is not None
    gate = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(gate)

    with pytest.raises(SystemExit, match="crezlo_publication_blocked:spatial_scenes_missing"):
        gate.require_verified_crezlo_publication(
            {"immersive_acceptance_json": {"accepted": False, "reason": "spatial_scenes_missing"}}
        )


def test_crezlo_publish_gate_accepts_complete_source_geometry_receipt() -> None:
    gate_path = SCRIPTS / "property_tour_publication_gate.py"
    spec = importlib.util.spec_from_file_location("property_tour_publication_gate_ok", gate_path)
    assert spec is not None
    gate = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(gate)

    gate.require_verified_crezlo_publication(
        {
            "immersive_acceptance_json": {
                "accepted": True,
                "spatial_provenance_verified": True,
                "exact_property_provenance_verified": True,
                "browser_receipt_verified": True,
                "scene_graph_connected": True,
                "all_required_scenes_navigable": True,
                "first_party_viewer_verified": True,
                "provider_control_route_verified": True,
                "floorplan_required": True,
                "floorplan_alignment_verified": True,
                "floorplan_layout_receipt_verified": True,
                "floorplan_geometry_projection_verified": True,
            },
            "crezlo_source_provenance": {
                "schema": "propertyquarry.crezlo_source_provenance.v1",
                "status": "pass",
                "provider": "crezlo",
                "hosted_url": "https://ea-property-tours-20260320.crezlotours.com/tours/verified",
                "capture": {
                    "representation_kind": "captured_360",
                    "scene_count": 3,
                    "covered_space_count": 3,
                    "navigation_hotspot_count": 2,
                    "scene_graph_connected": True,
                    "all_scenes_reachable": True,
                },
                "floorplan": {
                    "source_geometry_projection": {"sha256": "a" * 64},
                },
            },
        }
    )


def test_crezlo_publish_gate_rejects_accepted_payload_without_provider_receipt() -> None:
    gate_path = SCRIPTS / "property_tour_publication_gate.py"
    spec = importlib.util.spec_from_file_location("property_tour_publication_gate_missing_provider", gate_path)
    assert spec is not None
    gate = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(gate)

    with pytest.raises(SystemExit, match="crezlo_publication_blocked:crezlo_source_provenance_missing"):
        gate.require_verified_crezlo_publication(
            {
                "immersive_acceptance_json": {
                    "accepted": True,
                    "spatial_provenance_verified": True,
                    "exact_property_provenance_verified": True,
                    "browser_receipt_verified": True,
                    "scene_graph_connected": True,
                    "all_required_scenes_navigable": True,
                    "first_party_viewer_verified": True,
                    "provider_control_route_verified": True,
                }
            }
        )
