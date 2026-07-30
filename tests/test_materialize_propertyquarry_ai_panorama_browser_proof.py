from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from app.product import property_tour_hosting
from scripts import materialize_propertyquarry_ai_panorama_browser_proof as materializer


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_source_bundle(
    bundle_dir: Path,
    *,
    slug: str = "proof-fixture",
    disclosure: str = materializer.CANONICAL_DISCLOSURE,
) -> dict[str, object]:
    bundle_dir.mkdir(parents=True)
    panorama_dir = bundle_dir / "panoramas"
    proof_dir = bundle_dir / "proof"
    panorama_dir.mkdir()
    proof_dir.mkdir()
    scene_specs = (
        ("living", (184, 172, 158), ("hall",), 30, 60, 0),
        ("hall", (199, 204, 211), ("living", "kitchen"), 50, 50, 20),
        ("kitchen", (166, 184, 174), ("hall",), 70, 30, -15),
    )
    scenes: list[dict[str, object]] = []
    panorama_hashes: dict[str, str] = {}
    for index, (scene_id, color, targets, floorplan_x, floorplan_y, start_yaw) in enumerate(
        scene_specs
    ):
        path = panorama_dir / f"{scene_id}.jpg"
        Image.new("RGB", (4096, 2048), color=color).save(
            path,
            format="JPEG",
            quality=82,
        )
        panorama_hashes[scene_id] = _sha256(path)
        scenes.append(
            {
                "id": scene_id,
                "label": scene_id.title(),
                "projection": "equirectangular",
                "asset_relpath": f"panoramas/{scene_id}.jpg",
                "start_yaw": start_yaw,
                "start_pitch": 0,
                "start_fov": 72,
                "floorplan_x_pct": floorplan_x,
                "floorplan_y_pct": floorplan_y,
                "hotspots": [
                    {
                        "target_scene_id": target,
                        "label": f"Go to {target}",
                        "yaw": -70 + hotspot_index * 90,
                        "pitch": -8,
                    }
                    for hotspot_index, target in enumerate(targets)
                ],
            }
        )
    floorplan_path = bundle_dir / "floorplan.webp"
    Image.new("RGB", (1200, 1200), color=(244, 241, 234)).save(
        floorplan_path,
        format="WEBP",
        quality=88,
    )
    scene_ids = [row[0] for row in scene_specs]
    property_url_sha256 = "8" * 64
    provenance = {
        "contract_name": "propertyquarry.ai_panorama_provenance.v1",
        "generation_method": "ai_image_reconstruction",
        "captured_360": False,
        "measured_survey": False,
        "representation_disclosure": disclosure,
        "property_url_sha256": property_url_sha256,
        "source_image_sha256": [
            hashlib.sha256(f"source:{scene_id}".encode()).hexdigest()
            for scene_id in scene_ids
        ],
        "floorplan_sha256": _sha256(floorplan_path),
        "panorama_asset_sha256": panorama_hashes,
        "spatial_model_basis": "floorplan_scaled_approximation",
        "spatial_model_measured": False,
        "spatial_scene_ids": scene_ids,
    }
    provenance_path = proof_dir / "provenance.json"
    provenance_path.write_bytes(materializer._canonical_json_bytes(provenance))
    payload: dict[str, object] = {
        "slug": slug,
        "title": "AI panorama proof fixture",
        "display_title": "AI panorama proof fixture",
        "publication_status": "ready",
        "tour_privacy_mode": "anonymous_public",
        "control_mode": "ai_panorama_360",
        "scene_count": len(scenes),
        "property_url_sha256": property_url_sha256,
        "walkable_scene": {
            "representation_kind": "ai_reconstruction",
            "representation_disclosure": disclosure,
            "expected_scene_count": len(scenes),
            "initial_scene_id": "living",
            "floorplan_relpath": "floorplan.webp",
            "scenes": scenes,
            "spatial_model": {
                "source_basis": "floorplan_scaled_approximation",
                "measured": False,
                "rooms": [
                    {
                        "id": "living-volume",
                        "label": "Living",
                        "scene_id": "living",
                        "kind": "interior",
                        "x": 0,
                        "z": 0,
                        "width": 4.8,
                        "depth": 4.2,
                        "height": 3.1,
                    },
                    {
                        "id": "hall-volume",
                        "label": "Hall",
                        "scene_id": "hall",
                        "kind": "interior",
                        "x": 4.8,
                        "z": 0,
                        "width": 2.2,
                        "depth": 4.2,
                        "height": 2.6,
                    },
                    {
                        "id": "kitchen-volume",
                        "label": "Kitchen",
                        "scene_id": "kitchen",
                        "kind": "interior",
                        "x": 7,
                        "z": 0,
                        "width": 3.5,
                        "depth": 4.2,
                        "height": 2.6,
                    },
                ],
            },
            "acceptance": {
                "contract_name": "propertyquarry.ai_panorama_acceptance.v1",
                "proof_status": "pending",
                "property_url_sha256": property_url_sha256,
                "panorama_asset_sha256": panorama_hashes,
                "provenance_relpath": "proof/provenance.json",
                "provenance_sha256": _sha256(provenance_path),
            },
        },
    }
    (bundle_dir / "tour.json").write_bytes(materializer._canonical_json_bytes(payload))
    return payload


def _fake_browser_capture(
    *,
    candidate: materializer.PreparedCandidate,
    tested_origin: str,
    transport_origin: str,
    output_dir: Path,
    observed_at: str,
    timeout_ms: int,
) -> dict[str, object]:
    del transport_origin, timeout_ms
    screenshot_specs = {
        "desktop": ((1440, 960), (40, 60, 75)),
        "mobile": ((780, 1688), (75, 55, 40)),
        "dollhouse": ((1440, 960), (55, 40, 75)),
    }
    screenshot_paths: dict[str, Path] = {}
    screenshot_hashes: dict[str, str] = {}
    for surface, (dimensions, color) in screenshot_specs.items():
        path = output_dir / f"browser-{surface}.png"
        Image.new("RGB", dimensions, color=color).save(path, format="PNG")
        screenshot_paths[surface] = path
        screenshot_hashes[surface] = _sha256(path)
    tested_path = f"/tours/{candidate.slug}/control"
    common_surface = {
        "page_errors": [],
        "failed_requests": [],
        "console_errors": [],
    }
    browser_spec_sha256 = hashlib.sha256(
        materializer._canonical_json_bytes(
            materializer._expected_browser_spec(candidate)
        )
    ).hexdigest()
    viewer_implementation_sha256 = (
        materializer._expected_viewer_implementation_sha256(candidate)
    )
    return {
        "contract_name": materializer.CONTRACT_NAME,
        "proof_status": "pass",
        "observed_at": observed_at,
        "core_manifest_sha256": candidate.core_manifest_sha256,
        "bundle_material_sha256": candidate.bundle_material_sha256,
        "bundle_material_file_count": len(candidate.bundle_material_files),
        "browser_spec_sha256": browser_spec_sha256,
        "tested_url": f"{tested_origin}{tested_path}",
        "tested_origin": tested_origin,
        "tour_path": tested_path,
        "test_transport": "canonical_hostname_replay_over_loopback",
        "route_stack": "fastapi_public_route",
        "viewer_implementation": "app.api.routes.public_tours._tour_control_panorama_html",
        "viewer_implementation_sha256": viewer_implementation_sha256,
        "representation_disclosure": materializer._candidate_representation_disclosure(
            candidate
        ),
        "scene_ids": list(candidate.scene_ids),
        "anonymous_http_200": True,
        "drag_navigation_verified": True,
        "scene_navigation_verified": True,
        "all_hotspots_verified": True,
        "dollhouse_verified": True,
        "desktop_verified": True,
        "mobile_verified": True,
        "touch_verified": True,
        "pinch_zoom_verified": True,
        "zoom_controls_verified": True,
        "dollhouse_raycast_verified": True,
        "first_party_viewer_verified": True,
        "first_party_renderer_verified": True,
        "slow_network_verified": True,
        "performance_budget_verified": True,
        "immutable_asset_cache_verified": True,
        "self_only_csp_verified": True,
        "canonical_host_replay_verified": True,
        "disclosure_verified": True,
        "renderer_module_path": materializer.RENDERER_MODULE_PATH,
        "renderer_module_sha256": materializer.RENDERER_MODULE_SHA256,
        "renderer_http_status": 200,
        "external_script_requests": [],
        "verified_hotspot_edges": list(candidate.hotspot_edges),
        "dollhouse_room_count": candidate.spatial_room_count,
        "performance": {
            "initial_scene_loaded_ms": 1200.0,
            "mobile_initial_scene_loaded_ms": 1350.0,
            "slow_network_initial_scene_loaded_ms": 5000.0,
            "total_panorama_bytes": candidate.total_panorama_bytes,
            "largest_panorama_bytes": candidate.largest_panorama_bytes,
            "slow_network_profile": "150ms-latency-4mbps",
            "slow_network_all_scenes_loaded": True,
        },
        "desktop": {
            "viewport": "1440x960",
            "canvas": "1440x960",
            "screenshot_relpath": materializer.SCREENSHOT_RELPATHS["desktop"],
            "screenshot_sha256": screenshot_hashes["desktop"],
            **common_surface,
        },
        "mobile": {
            "viewport": "390x844",
            "canvas": "780x1688",
            "screenshot_relpath": materializer.SCREENSHOT_RELPATHS["mobile"],
            "screenshot_sha256": screenshot_hashes["mobile"],
            **common_surface,
        },
        "dollhouse": {
            "viewport": "1440x960",
            "canvas": "1440x960",
            "screenshot_relpath": materializer.SCREENSHOT_RELPATHS["dollhouse"],
            "screenshot_sha256": screenshot_hashes["dollhouse"],
            **common_surface,
        },
        "_temporary_screenshot_paths": {
            surface: str(path) for surface, path in screenshot_paths.items()
        },
    }


def test_materialize_seals_only_isolated_candidate_copy(tmp_path: Path) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    source_payload = _write_source_bundle(source_bundle)
    source_snapshot = {
        path.relative_to(source_bundle).as_posix(): path.read_bytes()
        for path in source_bundle.rglob("*")
        if path.is_file()
    }
    expected_core = (
        property_tour_hosting._hosted_property_tour_ai_panorama_core_manifest_sha256(
            source_payload
        )
    )
    candidate_root = tmp_path / "candidate-public"
    receipt_path = tmp_path / "materialization-receipt.json"

    receipt = materializer.materialize(
        source_bundle=source_bundle.resolve(),
        candidate_public_root=candidate_root.resolve(),
        base_url="https://propertyquarry.com",
        transport_base_url="http://127.0.0.1:18080",
        expected_slug="proof-fixture",
        expected_core_manifest_sha256=expected_core,
        receipt_out=receipt_path.resolve(),
        capture=_fake_browser_capture,
    )

    assert receipt["status"] == "pass"
    assert receipt["core_manifest_sha256"] == expected_core
    assert receipt["source_bundle_unchanged"] is True
    assert receipt["source_unchanged_after_candidate_seal"] is True
    assert receipt["external_receipt"] == {
        "written": True,
        "source_unchanged_post_write": True,
        "candidate_unchanged_post_write": True,
    }
    assert receipt["production_mutation_performed"] is False
    source_file_count, source_size_bytes, source_tree_sha256 = (
        materializer._regular_tree_snapshot(source_bundle)
    )
    assert receipt["source_tree_sha256"] == source_tree_sha256
    assert receipt["source_file_count"] == source_file_count
    assert receipt["source_size_bytes"] == source_size_bytes
    assert receipt["source_identity"]["identifier"] == (
        f"filesystem-tree-sha256:{source_tree_sha256}"
    )
    assert receipt_path.read_bytes() == materializer._canonical_json_bytes(receipt)
    assert {
        path.relative_to(source_bundle).as_posix(): path.read_bytes()
        for path in source_bundle.rglob("*")
        if path.is_file()
    } == source_snapshot
    candidate_bundle = candidate_root / "proof-fixture"
    candidate_snapshot = materializer._regular_tree_snapshot(candidate_bundle)
    assert receipt["candidate_file_count"] == candidate_snapshot[0]
    assert receipt["candidate_size_bytes"] == candidate_snapshot[1]
    assert receipt["candidate_tree_sha256"] == candidate_snapshot[2]
    installer_identity = (
        materializer.installer_intake.snapshot_ai_panorama_installer_source_bundle(
            candidate_bundle
        )
    )
    assert receipt["installer_source_identity_contract"] == (
        installer_identity.contract_name
    )
    assert receipt["installer_source_tree_algorithm"] == (
        installer_identity.tree_algorithm
    )
    assert receipt["installer_source_relative_root"] == "."
    assert receipt["installer_source_relative_path_semantics"] == (
        installer_identity.relative_path_semantics
    )
    assert receipt["installer_source_tree_sha256"] == installer_identity.tree_sha256
    assert receipt["installer_source_tour_sha256"] == installer_identity.tour_sha256
    assert receipt["installer_source_file_count"] == installer_identity.file_count
    assert receipt["installer_source_total_bytes"] == installer_identity.total_bytes
    assert receipt["candidate_tree_sha256"] != receipt["installer_source_tree_sha256"]
    assert receipt["tour_manifest_sha256"] == receipt["installer_source_tour_sha256"]
    marker = json.loads(
        (candidate_root / materializer.CANDIDATE_MARKER_RELPATH).read_text(
            encoding="utf-8"
        )
    )
    assert marker["source_tree_sha256"] == receipt["source_tree_sha256"]
    assert "installer_source_tree_sha256" not in marker
    assert "installer_source_tour_sha256" not in marker
    assert receipt["candidate_identity_rechecked_after_receipt_write"] is True
    assert receipt["external_receipt"]["candidate_unchanged_post_write"] is True
    sealed = json.loads((candidate_bundle / "tour.json").read_text(encoding="utf-8"))
    acceptance = sealed["walkable_scene"]["acceptance"]
    assert acceptance["proof_status"] == "pass"
    assert acceptance["core_manifest_sha256"] == expected_core
    proof_path = candidate_bundle / acceptance["browser_receipt_relpath"]
    assert _sha256(proof_path) == acceptance["browser_receipt_sha256"]
    assert proof_path.read_bytes() == materializer._canonical_json_bytes(
        json.loads(proof_path.read_text(encoding="utf-8"))
    )
    monkeypatch_origin = "PROPERTYQUARRY_PUBLIC_TOUR_BASE_URL"
    previous = materializer.os.environ.get(monkeypatch_origin)
    materializer.os.environ[monkeypatch_origin] = "https://propertyquarry.com/tours"
    try:
        contract = property_tour_hosting._hosted_property_tour_ai_panorama_contract(
            bundle_dir=candidate_bundle,
            payload=sealed,
        )
    finally:
        if previous is None:
            materializer.os.environ.pop(monkeypatch_origin, None)
        else:
            materializer.os.environ[monkeypatch_origin] = previous
    assert contract["ready"] is True


def test_materialize_without_external_receipt_reports_no_post_write_recheck(
    tmp_path: Path,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"

    receipt = materializer.materialize(
        source_bundle=source_bundle.resolve(),
        candidate_public_root=candidate_root.resolve(),
        base_url="https://propertyquarry.com",
        transport_base_url="http://127.0.0.1:18080",
        capture=_fake_browser_capture,
    )

    assert receipt["status"] == "pass"
    assert receipt["external_receipt"] == {
        "written": False,
        "source_unchanged_post_write": None,
        "candidate_unchanged_post_write": None,
    }
    assert receipt["candidate_identity_rechecked_after_receipt_write"] is False


def test_materialize_accepts_honest_floorplan_reconstruction_disclosure(
    tmp_path: Path,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(
        source_bundle,
        disclosure=materializer.FLOORPLAN_CANONICAL_DISCLOSURE,
    )
    candidate_root = tmp_path / "candidate-public"

    receipt = materializer.materialize(
        source_bundle=source_bundle.resolve(),
        candidate_public_root=candidate_root.resolve(),
        base_url="https://propertyquarry.com",
        transport_base_url="http://127.0.0.1:18080",
        capture=_fake_browser_capture,
    )

    assert receipt["status"] == "pass"
    sealed = json.loads(
        (candidate_root / "proof-fixture" / "tour.json").read_text(
            encoding="utf-8"
        )
    )
    contract = property_tour_hosting._hosted_property_tour_ai_panorama_contract(
        bundle_dir=candidate_root / "proof-fixture",
        payload=sealed,
    )
    assert contract["ready"] is True
    assert contract["representation_disclosure"] == (
        materializer.FLOORPLAN_CANONICAL_DISCLOSURE
    )


def test_capture_failure_leaves_candidate_pending_and_source_unchanged(
    tmp_path: Path,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    source_manifest = (source_bundle / "tour.json").read_bytes()
    candidate_root = tmp_path / "candidate-public"

    def fail_capture(**_kwargs: object) -> dict[str, object]:
        raise materializer.MaterializationError("injected_browser_failure")

    with pytest.raises(
        materializer.MaterializationError,
        match="injected_browser_failure",
    ):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            capture=fail_capture,
        )

    assert (source_bundle / "tour.json").read_bytes() == source_manifest
    pending = json.loads(
        (candidate_root / "proof-fixture" / "tour.json").read_text(encoding="utf-8")
    )
    acceptance = pending["walkable_scene"]["acceptance"]
    assert acceptance["proof_status"] == "pending"
    assert "browser_receipt_sha256" not in acceptance
    assert not (candidate_root / "proof-fixture" / "proof" / "browser-proof.json").exists()
    assert not (candidate_root / "proof-fixture" / "proof" / "browser-proof.json").exists()


def test_source_mutation_during_capture_cannot_leave_candidate_accepted(
    tmp_path: Path,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"

    def mutate_source_then_capture(**kwargs: object) -> dict[str, object]:
        (source_bundle / "unexpected.txt").write_text("changed", encoding="utf-8")
        return _fake_browser_capture(**kwargs)

    with pytest.raises(
        materializer.MaterializationError,
        match="source_bundle_mutated",
    ):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            capture=mutate_source_then_capture,
        )

    pending = json.loads(
        (candidate_root / "proof-fixture" / "tour.json").read_text(encoding="utf-8")
    )
    acceptance = pending["walkable_scene"]["acceptance"]
    assert acceptance["proof_status"] == "pending"
    assert "browser_receipt_sha256" not in acceptance


def test_browser_spec_binding_covers_functional_scene_values(tmp_path: Path) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate = materializer._prepare_candidate_copy(
        source_bundle=source_bundle.resolve(),
        candidate_public_root=(tmp_path / "candidate-public").resolve(),
    )
    expected = materializer._expected_browser_spec(candidate)

    digest = materializer._assert_browser_spec_binding(candidate, expected)

    assert digest == hashlib.sha256(
        materializer._canonical_json_bytes(expected)
    ).hexdigest()
    changed = copy.deepcopy(expected)
    changed["scenes"][1]["start_yaw"] = 65.0
    with pytest.raises(
        materializer.MaterializationError,
        match="desktop_panorama_spec_binding_mismatch",
    ):
        materializer._assert_browser_spec_binding(candidate, changed)


@pytest.mark.parametrize(
    ("tested_base_url", "transport_base_url", "reason"),
    (
        (
            "https://propertyquarry.com/path",
            "http://127.0.0.1:18080",
            "base_url_invalid",
        ),
        (
            "https://propertyquarry.com",
            "https://example.net",
            "transport_origin_not_loopback",
        ),
    ),
)
def test_materialize_rejects_unsafe_browser_origins_before_copy(
    tmp_path: Path,
    tested_base_url: str,
    transport_base_url: str,
    reason: str,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"

    with pytest.raises(materializer.MaterializationError, match=reason):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url=tested_base_url,
            transport_base_url=transport_base_url,
            capture=_fake_browser_capture,
        )

    assert not candidate_root.exists()


def test_materialize_rejects_nonempty_candidate_root(tmp_path: Path) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"
    candidate_root.mkdir()
    (candidate_root / "unrelated.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(
        materializer.MaterializationError,
        match="candidate_root_not_empty",
    ):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            capture=_fake_browser_capture,
        )

    assert (candidate_root / "unrelated.txt").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize(
    ("expected_argument", "reason"),
    (
        ("expected_core_manifest_sha256", "core_manifest_sha256_mismatch"),
        ("expected_source_tree_sha256", "source_tree_sha256_mismatch"),
        ("expected_bundle_material_sha256", "bundle_material_sha256_mismatch"),
    ),
)
def test_expected_identity_mismatch_fails_before_candidate_write(
    tmp_path: Path,
    expected_argument: str,
    reason: str,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"

    with pytest.raises(materializer.MaterializationError, match=reason):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            capture=_fake_browser_capture,
            **{expected_argument: "f" * 64},
        )

    assert not candidate_root.exists()


def test_source_identity_change_before_copy_is_detected_without_candidate_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"
    original_core = (
        materializer.property_tour_hosting._hosted_property_tour_ai_panorama_core_manifest_sha256
    )
    mutation_performed = False

    def core_then_mutate(payload: dict[str, object]) -> str:
        nonlocal mutation_performed
        result = original_core(payload)
        if not mutation_performed:
            mutation_performed = True
            (source_bundle / "late-empty-directory").mkdir()
        return result

    monkeypatch.setattr(
        materializer.property_tour_hosting,
        "_hosted_property_tour_ai_panorama_core_manifest_sha256",
        core_then_mutate,
    )
    with pytest.raises(
        materializer.MaterializationError,
        match="source_bundle_mutated_before_copy",
    ):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            capture=_fake_browser_capture,
        )

    assert not candidate_root.exists()


@pytest.mark.parametrize("overlap", ("source", "candidate"))
def test_receipt_output_overlap_is_rejected_before_candidate_write(
    tmp_path: Path,
    overlap: str,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    source_manifest = (source_bundle / "tour.json").read_bytes()
    candidate_root = tmp_path / "candidate-public"
    receipt_path = (
        source_bundle / "tour.json"
        if overlap == "source"
        else candidate_root / "receipt.json"
    )

    with pytest.raises(
        materializer.MaterializationError,
        match=f"receipt_out_{overlap}_overlap",
    ):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            receipt_out=receipt_path.absolute(),
            capture=_fake_browser_capture,
        )

    assert (source_bundle / "tour.json").read_bytes() == source_manifest
    assert not candidate_root.exists()


@pytest.mark.parametrize("symlink_kind", ("source", "candidate", "receipt"))
def test_supplied_symlink_path_is_rejected_before_copy(
    tmp_path: Path,
    symlink_kind: str,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"
    receipt_path = tmp_path / "receipt.json"
    supplied_source = source_bundle.resolve()
    supplied_candidate = candidate_root.resolve()
    supplied_receipt = receipt_path.resolve()
    if symlink_kind == "source":
        supplied_source = tmp_path / "source-link"
        supplied_source.symlink_to(source_bundle, target_is_directory=True)
    elif symlink_kind == "candidate":
        target = tmp_path / "candidate-target"
        target.mkdir()
        supplied_candidate = tmp_path / "candidate-link"
        supplied_candidate.symlink_to(target, target_is_directory=True)
    else:
        supplied_receipt = tmp_path / "receipt-link"
        supplied_receipt.symlink_to(source_bundle / "tour.json")

    with pytest.raises(
        materializer.MaterializationError,
        match=f"{symlink_kind if symlink_kind != 'candidate' else 'candidate_root'}.*symlink_component",
    ):
        materializer.materialize(
            source_bundle=supplied_source,
            candidate_public_root=supplied_candidate,
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            receipt_out=supplied_receipt,
            capture=_fake_browser_capture,
        )

    assert not candidate_root.exists()


def test_partial_candidate_copy_is_cleaned_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"

    def fail_copytree(_source: Path, destination: Path, **_kwargs: object) -> None:
        destination.mkdir()
        (destination / "partial.txt").write_text("partial", encoding="utf-8")
        raise materializer.MaterializationError("injected_copy_failure")

    monkeypatch.setattr(materializer.shutil, "copytree", fail_copytree)
    with pytest.raises(
        materializer.MaterializationError,
        match="injected_copy_failure",
    ):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            capture=_fake_browser_capture,
        )

    assert not candidate_root.exists()


def test_source_mutation_during_external_receipt_write_rolls_back_and_cleans_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"
    receipt_path = tmp_path / "receipt.json"
    original_atomic_create = materializer._atomic_create

    def create_then_mutate(
        path: Path,
        content: bytes,
        **kwargs: object,
    ) -> tuple[int, int]:
        identity = original_atomic_create(path, content, **kwargs)
        if path == receipt_path:
            (source_bundle / "late-empty-directory").mkdir()
        return identity

    monkeypatch.setattr(materializer, "_atomic_create", create_then_mutate)
    with pytest.raises(
        materializer.MaterializationError,
        match="source_bundle_mutated_during_receipt_write",
    ):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            receipt_out=receipt_path.resolve(),
            capture=_fake_browser_capture,
        )

    assert not receipt_path.exists()
    pending = json.loads(
        (candidate_root / "proof-fixture" / "tour.json").read_text(encoding="utf-8")
    )
    assert pending["walkable_scene"]["acceptance"]["proof_status"] == "pending"
    assert "browser_receipt_sha256" not in pending["walkable_scene"]["acceptance"]
    assert not (candidate_root / "proof-fixture" / "proof" / "browser-proof.json").exists()


def test_candidate_mutation_after_seal_rejects_receipt_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"
    receipt_path = tmp_path / "receipt.json"
    original_atomic_create = materializer._atomic_create

    def create_then_mutate(
        path: Path,
        content: bytes,
        **kwargs: object,
    ) -> tuple[int, int]:
        identity = original_atomic_create(path, content, **kwargs)
        if path == receipt_path:
            (candidate_root / "proof-fixture" / "proof" / "browser-desktop.png").write_bytes(
                b"post-seal mutation"
            )
        return identity

    monkeypatch.setattr(materializer, "_atomic_create", create_then_mutate)
    with pytest.raises(
        materializer.MaterializationError,
        match="candidate_bundle_mutated_during_receipt_write",
    ):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            receipt_out=receipt_path.resolve(),
            capture=_fake_browser_capture,
        )

    assert not receipt_path.exists()
    pending = json.loads(
        (candidate_root / "proof-fixture" / "tour.json").read_text(encoding="utf-8")
    )
    assert pending["walkable_scene"]["acceptance"]["proof_status"] == "pending"
    assert not (candidate_root / "proof-fixture" / "proof" / "browser-proof.json").exists()


def test_candidate_root_replacement_after_seal_rejects_receipt_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"
    receipt_path = tmp_path / "receipt.json"
    original_atomic_create = materializer._atomic_create

    def create_then_replace_root(
        path: Path,
        content: bytes,
        **kwargs: object,
    ) -> tuple[int, int]:
        identity = original_atomic_create(path, content, **kwargs)
        if path == receipt_path:
            moved_root = tmp_path / "candidate-public-before-swap"
            candidate_root.rename(moved_root)
            candidate_root.mkdir()
            (moved_root / "proof-fixture").rename(
                candidate_root / "proof-fixture"
            )
            (moved_root / materializer.CANDIDATE_MARKER_RELPATH).rename(
                candidate_root / materializer.CANDIDATE_MARKER_RELPATH
            )
            moved_root.rmdir()
        return identity

    monkeypatch.setattr(materializer, "_atomic_create", create_then_replace_root)
    with pytest.raises(
        materializer.MaterializationError,
        match="candidate_bundle_mutated_during_receipt_write",
    ):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            receipt_out=receipt_path.resolve(),
            capture=_fake_browser_capture,
        )

    assert not receipt_path.exists()
    pending = json.loads(
        (candidate_root / "proof-fixture" / "tour.json").read_text(encoding="utf-8")
    )
    assert pending["walkable_scene"]["acceptance"]["proof_status"] == "pending"
    assert not (candidate_root / "proof-fixture" / "proof" / "browser-proof.json").exists()


def test_late_candidate_root_replacement_rejects_receipt_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bundle = tmp_path / "source" / "bundle"
    _write_source_bundle(source_bundle)
    candidate_root = tmp_path / "candidate-public"
    receipt_path = tmp_path / "receipt.json"
    original_marker_check = materializer._assert_candidate_marker_identity
    marker_check_count = 0

    def check_then_replace_root(
        candidate: materializer.PreparedCandidate,
        *,
        reason: str,
    ) -> None:
        nonlocal marker_check_count
        original_marker_check(candidate, reason=reason)
        marker_check_count += 1
        if marker_check_count == 5:
            moved_root = tmp_path / "candidate-public-before-late-swap"
            candidate_root.rename(moved_root)
            candidate_root.mkdir()
            (moved_root / "proof-fixture").rename(
                candidate_root / "proof-fixture"
            )
            (moved_root / materializer.CANDIDATE_MARKER_RELPATH).rename(
                candidate_root / materializer.CANDIDATE_MARKER_RELPATH
            )
            moved_root.rmdir()

    monkeypatch.setattr(
        materializer,
        "_assert_candidate_marker_identity",
        check_then_replace_root,
    )
    with pytest.raises(
        materializer.MaterializationError,
        match="candidate_bundle_mutated_during_receipt_write",
    ):
        materializer.materialize(
            source_bundle=source_bundle.resolve(),
            candidate_public_root=candidate_root.resolve(),
            base_url="https://propertyquarry.com",
            transport_base_url="http://127.0.0.1:18080",
            receipt_out=receipt_path.resolve(),
            capture=_fake_browser_capture,
        )

    assert not receipt_path.exists()
    pending = json.loads(
        (candidate_root / "proof-fixture" / "tour.json").read_text(encoding="utf-8")
    )
    assert pending["walkable_scene"]["acceptance"]["proof_status"] == "pending"
    assert not (candidate_root / "proof-fixture" / "proof" / "browser-proof.json").exists()


def test_tree_snapshot_is_order_independent_and_binds_empty_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tree"
    (root / "b").mkdir(parents=True)
    (root / "a").mkdir()
    (root / "a" / "one.txt").write_text("one", encoding="utf-8")
    (root / "b" / "two.txt").write_text("two", encoding="utf-8")
    baseline = materializer._regular_tree_snapshot(root)
    real_walk = materializer.os.walk

    def reversed_walk(*args: object, **kwargs: object):
        rows = list(real_walk(*args, **kwargs))
        for current, directories, files in reversed(rows):
            yield current, list(reversed(directories)), list(reversed(files))

    monkeypatch.setattr(materializer.os, "walk", reversed_walk)
    assert materializer._regular_tree_snapshot(root) == baseline
    (root / "empty").mkdir()
    with_empty_directory = materializer._regular_tree_snapshot(root)
    assert with_empty_directory[:2] == baseline[:2]
    assert with_empty_directory[2] != baseline[2]


class _FakeRequest:
    def __init__(self, url: str, *, resource_type: str = "document") -> None:
        self.url = url
        self.resource_type = resource_type
        self.method = "GET"
        self.failure = None


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        self.status = status
        self.headers = headers
        self._body = body
        self.url = ""

    def body(self) -> bytes:
        return self._body


class _FakeRoute:
    def __init__(self, request: _FakeRequest, response: _FakeResponse) -> None:
        self.request = request
        self.response = response
        self.fetch_calls: list[dict[str, object]] = []
        self.aborted = ""
        self.fulfilled = False
        self.continued = False

    def fetch(self, **kwargs: object) -> _FakeResponse:
        self.fetch_calls.append(kwargs)
        self.response.url = str(kwargs["url"])
        return self.response

    def abort(self, reason: str) -> None:
        self.aborted = reason

    def fulfill(self, **_kwargs: object) -> None:
        self.fulfilled = True

    def continue_(self) -> None:
        self.continued = True


def test_loopback_redirect_to_external_origin_is_never_followed() -> None:
    control_url = "https://propertyquarry.com/tours/example/control"
    audit = materializer.BrowserAudit(
        "https://propertyquarry.com",
        "http://127.0.0.1:18080",
        frozenset({control_url}),
        {},
        control_url,
        "a" * 64,
    )
    route = _FakeRoute(
        _FakeRequest(control_url),
        _FakeResponse(
            status=302,
            headers={"location": "https://attacker.example/collect"},
            body=b"redirect",
        ),
    )

    audit._handle_route(route)

    assert route.fetch_calls == [
        {
            "url": "http://127.0.0.1:18080/tours/example/control",
            "timeout": 120_000,
            "max_redirects": 0,
        }
    ]
    assert all("attacker.example" not in str(call) for call in route.fetch_calls)
    assert route.aborted == "blockedbyclient"
    assert route.fulfilled is False
    assert audit.bad_responses[0]["reason"] == "redirect_blocked"


@pytest.mark.parametrize(
    "url",
    (
        "https://attacker.example/script.js",
        "https://propertyquarry.com/api/private",
        "file:///etc/passwd",
    ),
)
def test_browser_replay_rejects_external_scheme_host_and_unapproved_path(
    url: str,
) -> None:
    control_url = "https://propertyquarry.com/tours/example/control"
    audit = materializer.BrowserAudit(
        "https://propertyquarry.com",
        "http://127.0.0.1:18080",
        frozenset({control_url}),
        {},
        control_url,
        "a" * 64,
    )
    route = _FakeRoute(
        _FakeRequest(url, resource_type="script"),
        _FakeResponse(status=200, headers={}, body=b"unused"),
    )

    audit._handle_route(route)

    assert route.fetch_calls == []
    assert route.aborted == "blockedbyclient"
    assert route.fulfilled is False


def test_asset_body_hash_is_verified_even_when_digest_header_matches() -> None:
    asset_url = "https://propertyquarry.com/tours/files/example/panorama.jpg?v=abc"
    expected_body = b"good"
    expected_digest = hashlib.sha256(expected_body).hexdigest()
    audit = materializer.BrowserAudit(
        "https://propertyquarry.com",
        "http://127.0.0.1:18080",
        frozenset({asset_url}),
        {asset_url: (expected_digest, len(expected_body))},
        "https://propertyquarry.com/tours/example/control",
        "a" * 64,
    )
    route = _FakeRoute(
        _FakeRequest(asset_url, resource_type="image"),
        _FakeResponse(
            status=200,
            headers={
                "cache-control": "public, max-age=31536000, immutable",
                "x-propertyquarry-asset-sha256": expected_digest,
            },
            body=b"evil",
        ),
    )

    audit._handle_route(route)

    assert route.aborted == "blockedbyclient"
    assert route.fulfilled is False
    assert audit.immutable_asset_digests == set()
    assert audit.bad_responses[0]["reason"] == "asset_body_binding_invalid"


def test_viewer_implementation_projection_binds_module_and_style() -> None:
    first = b"<html><style>.viewer{display:block}</style><script type='module'>const version=1;</script></html>"
    equivalent = b"<html><style nonce='different'>.viewer{display:block}</style><script nonce='other' type='module'>const version=1;</script></html>"
    changed = b"<html><style>.viewer{display:block}</style><script type='module'>const version=2;</script></html>"

    first_hash = materializer._viewer_implementation_projection_sha256(first)

    assert first_hash == materializer._viewer_implementation_projection_sha256(
        equivalent
    )
    assert first_hash != materializer._viewer_implementation_projection_sha256(
        changed
    )


def _valid_ai_panorama_csp() -> str:
    return (
        "default-src 'none'; object-src 'none'; frame-src 'none'; "
        "script-src 'self' 'nonce-proof123'; script-src-attr 'none'; "
        "connect-src 'self'; img-src 'self' data: blob:"
    )


def test_csp_parser_accepts_only_the_self_nonce_profile() -> None:
    materializer._assert_csp({"content-security-policy": _valid_ai_panorama_csp()})


@pytest.mark.parametrize(
    "malicious_csp",
    (
        _valid_ai_panorama_csp().replace(
            "script-src 'self' 'nonce-proof123'",
            "script-src 'self' 'nonce-proof123' *",
        ),
        _valid_ai_panorama_csp().replace(
            "connect-src 'self'",
            "connect-src 'self' *",
        ),
        _valid_ai_panorama_csp()
        + "; script-src https://attacker.example 'unsafe-inline'",
    ),
)
def test_csp_parser_rejects_wildcard_and_duplicate_bypass(
    malicious_csp: str,
) -> None:
    with pytest.raises(materializer.MaterializationError, match="csp"):
        materializer._assert_csp({"content-security-policy": malicious_csp})
