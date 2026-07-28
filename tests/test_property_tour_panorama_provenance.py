from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from app.api.routes.public_tours import (
    _pano2vr_export_file,
    _pano2vr_spatial_provenance_errors_cached,
)
from scripts.property_tour_panorama_provenance import (
    KRPANO_SPATIAL_PROVENANCE_KEY,
    PANORAMA_SPATIAL_PROVENANCE_SCHEMA,
    PANO2VR_SPATIAL_PROVENANCE_KEY,
    asset_set_sha256,
    export_tree_sha256,
    panorama_asset_relpaths,
    pano2vr_export_topology,
    walkable_scene_topology,
)
from scripts.verify_property_tour_controls import build_property_tour_control_receipt


def _spatial_receipt(
    *,
    slug: str,
    provider: str,
    artifact: dict[str, object],
    topology: dict[str, object],
    projection: str = "equirectangular",
) -> dict[str, object]:
    return {
        "schema": PANORAMA_SPATIAL_PROVENANCE_SCHEMA,
        "status": "pass",
        "provider": provider,
        "target_slug": slug,
        "artifact": artifact,
        "capture": {
            "source_kind": "camera_equirectangular",
            "projection": projection,
            **topology,
        },
        "authorization": {
            "status": "approved",
            "reference": f"camera-release:{slug}",
        },
        "review": {
            "property_match": "pass",
            "visual_match": "pass",
            "spatial_capture_match": "pass",
            "flat_composite_absent": True,
            "reviewed_by": "property-tour-reviewer",
            "reviewed_at": "2026-07-18T12:00:00+00:00",
        },
    }


def _write_pano2vr_bundle(
    root: Path,
    slug: str,
    *,
    walkable: bool = False,
    connected_scenes: bool = False,
    include_receipt: bool = True,
) -> Path:
    bundle = root / slug
    export_dir = bundle / "pano"
    export_dir.mkdir(parents=True)
    payload: dict[str, object] = {
        "slug": slug,
        "title": slug,
        "pano2vr_entry_relpath": "pano/index.html",
    }
    if walkable:
        payload.update(
            {
                "scene_strategy": "walkable_panorama",
                "creation_mode": "hosted_walkable_360",
            }
        )
    (bundle / "tour.json").write_text(json.dumps(payload), encoding="utf-8")
    (export_dir / "index.html").write_text(
        "<!doctype html><script src='tour.js'></script><span>pano.xml</span>",
        encoding="utf-8",
    )
    (export_dir / "tour.js").write_text("window.ggskin = true;", encoding="utf-8")
    xml = (
        "<tour>"
        "<panorama id='node1'><hotspots><hotspot url='{node2}' /></hotspots></panorama>"
        "<panorama id='node2'><hotspots><hotspot url='{node1}' /></hotspots></panorama>"
        "</tour>"
        if connected_scenes
        else "<panorama id='node1'><hotspots /></panorama>"
    )
    (export_dir / "pano.xml").write_text(xml, encoding="utf-8")
    if include_receipt:
        topology = pano2vr_export_topology(export_dir)
        private_payload = {
            PANO2VR_SPATIAL_PROVENANCE_KEY: _spatial_receipt(
                slug=slug,
                provider="pano2vr",
                artifact={
                    "kind": "local_export",
                    "sha256": export_tree_sha256(export_dir),
                    "entry_relpath": "index.html",
                },
                topology=topology,
            )
        }
        (bundle / "tour.private.json").write_text(
            json.dumps(private_payload),
            encoding="utf-8",
        )
    return bundle


def _write_krpano_bundle(
    root: Path,
    slug: str,
    *,
    walkable: bool = False,
    connected_scenes: bool = False,
    include_receipt: bool = True,
) -> Path:
    bundle = root / slug
    panorama_path = bundle / "krpano" / "panorama.jpg"
    panorama_path.parent.mkdir(parents=True)
    Image.new("RGB", (2048, 1024), color=(12, 31, 45)).save(
        panorama_path,
        format="JPEG",
    )
    second_panorama_path = bundle / "krpano" / "living.jpg"
    if connected_scenes:
        Image.new("RGB", (2048, 1024), color=(22, 41, 55)).save(
            second_panorama_path,
            format="JPEG",
        )
    walkable_scene: dict[str, object] = {
        "projection": "equirectangular",
        "panorama_relpath": "krpano/panorama.jpg",
    }
    if connected_scenes:
        walkable_scene = {
            "projection": "equirectangular",
            "scenes": [
                {
                    "id": "entry",
                    "panorama_relpath": "krpano/panorama.jpg",
                    "hotspots": [{"target_scene": "living"}],
                },
                {
                    "id": "living",
                    "panorama_relpath": "krpano/living.jpg",
                    "hotspots": [{"target_scene": "entry"}],
                },
            ],
        }
    payload: dict[str, object] = {
        "slug": slug,
        "title": slug,
        "scene_strategy": "walkable_panorama" if walkable else "single_panorama",
        "creation_mode": "hosted_walkable_360" if walkable else "hosted_panorama_360",
        "walkable_scene": walkable_scene,
    }
    (bundle / "tour.json").write_text(json.dumps(payload), encoding="utf-8")
    if include_receipt:
        private_payload = {
            KRPANO_SPATIAL_PROVENANCE_KEY: _spatial_receipt(
                slug=slug,
                provider="krpano",
                artifact={
                    "kind": "panorama_assets",
                    "sha256": asset_set_sha256(
                        bundle,
                        panorama_asset_relpaths(payload),
                    ),
                    "entry_relpath": "",
                },
                topology=walkable_scene_topology(payload),
            )
        }
        (bundle / "tour.private.json").write_text(
            json.dumps(private_payload),
            encoding="utf-8",
        )
    return bundle


def _tour_row(receipt: dict[str, object]) -> dict[str, object]:
    rows = receipt.get("tours")
    assert isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
    return rows[0]


def test_marker_only_pano2vr_export_fails_closed(tmp_path: Path) -> None:
    _write_pano2vr_bundle(tmp_path, "marker-only", include_receipt=False)

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["pano2vr"] == 0
    blocker = receipt["provider_blockers"]["pano2vr"]["reasons"][0]
    assert blocker["reason"] == "pano2vr_spatial_provenance_missing_or_invalid"


def test_one_node_pano2vr_receipt_cannot_satisfy_walkable_claim(tmp_path: Path) -> None:
    _write_pano2vr_bundle(tmp_path, "one-node-walkable", walkable=True)

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["pano2vr"] == 0
    blocker = receipt["provider_blockers"]["pano2vr"]["reasons"][0]
    assert blocker["reason"] == "pano2vr_spatial_provenance_missing_or_invalid"


def test_connected_pano2vr_receipt_satisfies_walkable_claim(tmp_path: Path) -> None:
    _write_pano2vr_bundle(
        tmp_path,
        "connected-walkable",
        walkable=True,
        connected_scenes=True,
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["pano2vr"] == 1
    control = _tour_row(receipt)["controls"][0]
    assert control["evidence"] == "provenance_bound_pano2vr_spatial_export"


def test_ratio_only_krpano_asset_without_receipt_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "licensed")
    _write_krpano_bundle(tmp_path, "ratio-only", include_receipt=False)

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["krpano"] == 0
    blocker = receipt["provider_blockers"]["krpano"]["reasons"][0]
    assert blocker["reason"] == "krpano_spatial_provenance_missing_or_invalid"


def test_one_scene_krpano_receipt_cannot_satisfy_walkable_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "licensed")
    _write_krpano_bundle(tmp_path, "one-scene-walkable", walkable=True)

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["krpano"] == 0


def test_reviewed_single_krpano_panorama_remains_honestly_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "licensed")
    _write_krpano_bundle(tmp_path, "single-panorama")

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["krpano"] == 1
    control = _tour_row(receipt)["controls"][0]
    assert control["evidence"] == "provenance_bound_licensed_krpano_spatial_scene"


def test_connected_reviewed_krpano_scenes_satisfy_walkable_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "licensed")
    _write_krpano_bundle(
        tmp_path,
        "connected-krpano",
        walkable=True,
        connected_scenes=True,
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["krpano"] == 1
    control = _tour_row(receipt)["controls"][0]
    assert control["evidence"] == "provenance_bound_licensed_krpano_spatial_scene"


def test_panorama_byte_tamper_invalidates_receipt(tmp_path: Path) -> None:
    bundle = _write_pano2vr_bundle(tmp_path, "tampered-pano")
    (bundle / "pano" / "tour.js").write_text(
        "window.ggskin = false; // changed after review",
        encoding="utf-8",
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["pano2vr"] == 0


def test_panorama_asset_hash_rejects_internal_symlink(tmp_path: Path) -> None:
    asset_dir = tmp_path / "krpano"
    asset_dir.mkdir()
    real_asset = asset_dir / "real-panorama.jpg"
    real_asset.write_bytes(b"real panorama bytes")
    (asset_dir / "panorama.jpg").symlink_to(real_asset.name)

    with pytest.raises(ValueError, match="asset_symlink_forbidden"):
        asset_set_sha256(tmp_path, ("krpano/panorama.jpg",))


def test_offline_verifier_fails_closed_above_panorama_hash_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pano2vr_bundle(tmp_path, "offline-budget")
    monkeypatch.setenv("PROPERTYQUARRY_PANORAMA_MAX_HASH_FILES", "1")

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["pano2vr"] == 0


def test_raw_pano2vr_files_require_private_byte_bound_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pano2vr_bundle(tmp_path, "raw-marker-only", include_receipt=False)
    _write_pano2vr_bundle(tmp_path, "raw-reviewed")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    _pano2vr_spatial_provenance_errors_cached.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        _pano2vr_export_file("raw-marker-only", "pano/index.html")

    assert exc_info.value.status_code == 404
    assert _pano2vr_export_file("raw-reviewed", "pano/index.html").is_file()

    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_PANO2VR_MAX_HASH_FILES", "1")
    _pano2vr_spatial_provenance_errors_cached.cache_clear()
    with pytest.raises(HTTPException) as budget_exc_info:
        _pano2vr_export_file("raw-reviewed", "pano/index.html")
    assert budget_exc_info.value.status_code == 404
