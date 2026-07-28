from __future__ import annotations

import copy
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time

import pytest
from PIL import Image, ImageDraw

from app.product import service as product_service
from scripts import generate_property_reconstruction as generator
from scripts import property_reconstruction_styles as styles


EXPECTED_CUES = {
    "warm_scandi": {"light_oak", "linen", "neutral_textile", "clean_storage"},
    "ikea_practical": {"modular_storage", "bright_storage", "simple_lines", "rental_friendly"},
    "urban_jungle": {"healthy_plants", "rattan", "warm_wood", "linen"},
    "landhaus": {"warm_timber", "linen", "ceramics", "traditional_details"},
    "gilded_penthouse": {"polished_marble", "brass", "gold_accents", "classical_details"},
}


@contextmanager
def _serve_directory(root: Path):
    class QuietHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _viewer_manifest(style_id: str) -> dict[str, object]:
    selected = styles.reconstruction_style(style_id, style_id=style_id)
    style_scene = styles.build_style_scene(selected, route_stop_count=1)
    return {
        "room_dimensions_m": {"width": 8.0, "depth": 6.0, "height": 2.7},
        "geometry": {
            "wall_rectangles": [
                {"center_x": 0.0, "center_z": -3.0, "width": 8.0, "depth": 0.12, "rotation_y": 0.0},
                {"center_x": 0.0, "center_z": 3.0, "width": 8.0, "depth": 0.12, "rotation_y": 0.0},
                {"center_x": -4.0, "center_z": 0.0, "width": 6.0, "depth": 0.12, "rotation_y": -1.570796},
                {"center_x": 4.0, "center_z": 0.0, "width": 6.0, "depth": 0.12, "rotation_y": -1.570796},
            ],
            "floor_texture_crop": {"offset_x": 0.0, "offset_y": 0.0, "repeat_x": 1.0, "repeat_y": 1.0},
        },
        "floorplan": {"relpath": "source-floorplan.png"},
        "photos": [],
        "photo_reference_panels": [],
        "style_label": selected["label"],
        "requested_style": selected,
        "style_scene": style_scene,
        "walkable_scene": {
            "kind": "generated_reconstruction_layout",
            "route": [
                {
                    "label": "Living room",
                    "kind": "living",
                    "focus": {"x": 0.0, "y": 1.2, "z": 0.0},
                    "camera": {"x": 2.0, "y": 1.6, "z": 2.0},
                }
            ],
            "rooms": [
                {
                    "label": "Living room",
                    "position": {"x": 0.0, "z": 0.0},
                    "focus": {"x": 0.0, "y": 1.2, "z": 0.0},
                }
            ],
        },
    }


def _cb82_scale_viewer_manifest() -> dict[str, object]:
    manifest = _viewer_manifest("urban_jungle")
    selected = styles.reconstruction_style("urban_jungle", style_id="urban_jungle")
    manifest["room_dimensions_m"] = {"width": 10.0, "depth": 15.741, "height": 2.75}
    manifest["geometry"] = {
        "wall_rectangles": [
            {"center_x": 0.0, "center_z": -7.87, "width": 10.0, "depth": 0.12, "rotation_y": 0.0},
            {"center_x": 0.0, "center_z": 7.87, "width": 10.0, "depth": 0.12, "rotation_y": 0.0},
            {"center_x": -5.0, "center_z": 0.0, "width": 15.741, "depth": 0.12, "rotation_y": -1.570796},
            {"center_x": 5.0, "center_z": 0.0, "width": 15.741, "depth": 0.12, "rotation_y": -1.570796},
            {"center_x": 0.4, "center_z": 0.8, "width": 5.4, "depth": 0.14, "rotation_y": -1.570796},
            {"center_x": -1.2, "center_z": -1.9, "width": 3.2, "depth": 0.14, "rotation_y": 0.0},
        ],
        "floor_texture_crop": {"offset_x": 0.0, "offset_y": 0.0, "repeat_x": 1.0, "repeat_y": 1.0},
    }
    manifest["style_scene"] = styles.build_style_scene(selected, route_stop_count=2)
    manifest["walkable_scene"] = {
        "kind": "generated_reconstruction_layout",
        "route": [
            {
                "label": "living kitchen",
                "kind": "kitchen",
                "focus": {"x": 1.65, "y": 1.25, "z": 3.45},
                "camera": {"x": 4.0, "y": 1.6, "z": 5.4},
            },
            {
                "label": "living room",
                "kind": "living",
                "focus": {"x": -1.25, "y": 1.25, "z": -3.55},
                "camera": {"x": -4.1, "y": 1.6, "z": -5.1},
            },
        ],
        "rooms": [
            {
                "label": "living kitchen",
                "position": {"x": 1.65, "z": 3.45},
                "focus": {"x": 1.65, "y": 1.25, "z": 3.45},
            },
            {
                "label": "living room",
                "position": {"x": -1.25, "z": -3.55},
                "focus": {"x": -1.25, "y": 1.25, "z": -3.55},
            },
        ],
    }
    manifest["photos"] = [{"relpath": "source-photo.png", "label": "Listing photo"}]
    manifest["photo_reference_panels"] = [
        {
            "route_index": 0,
            "photo_relpath": "source-photo.png",
            "label": "living kitchen source",
            "position": {"x": -3.4, "y": 1.75, "z": -5.8},
            "rotation_y": 0.2,
            "frame_width": 3.0,
            "frame_height": 1.65,
            "photo_width": 2.72,
            "photo_height": 1.35,
        },
        {
            "route_index": 1,
            "photo_relpath": "source-photo.png",
            "label": "living room source",
            "position": {"x": 3.2, "y": 1.75, "z": 5.9},
            "rotation_y": -0.2,
            "frame_width": 3.0,
            "frame_height": 1.65,
            "photo_width": 2.72,
            "photo_height": 1.35,
        },
    ]
    return manifest


def _runtime_walkthrough_ikea_viewer_manifest() -> dict[str, object]:
    manifest = _viewer_manifest("ikea_practical")
    selected = styles.reconstruction_style("ikea_practical", style_id="ikea_practical")
    manifest["room_dimensions_m"] = {"width": 10.0, "depth": 6.269, "height": 2.75}
    manifest["geometry"] = {
        "wall_rectangles": [
            {"center_x": 0.0, "center_z": -2.9673, "width": 9.8336, "depth": 0.1669, "rotation_y": 0.0},
            {"center_x": -2.5833, "center_z": -1.3792, "width": 3.3435, "depth": 0.1667, "rotation_y": -1.570796},
            {"center_x": 2.5417, "center_z": -1.3792, "width": 4.7503, "depth": 0.1669, "rotation_y": 0.0},
            {"center_x": -4.8333, "center_z": 0.0, "width": 6.1018, "depth": 0.1667, "rotation_y": 1.570796},
            {"center_x": 0.2083, "center_z": 0.0, "width": 6.1018, "depth": 0.1125, "rotation_y": -1.570796},
            {"center_x": 4.8333, "center_z": 0.0, "width": 6.1018, "depth": 0.1667, "rotation_y": -1.570796},
            {"center_x": -2.3333, "center_z": 0.209, "width": 5.1669, "depth": 0.1669, "rotation_y": 0.0},
            {"center_x": 2.5417, "center_z": 0.7941, "width": 4.5137, "depth": 0.1125, "rotation_y": -1.570796},
            {"center_x": 0.0, "center_z": 2.9673, "width": 9.8336, "depth": 0.1669, "rotation_y": 0.0},
        ],
        "floor_texture_crop": {
            "offset_x": 0.0,
            "offset_y": 0.0,
            "repeat_x": 1.0,
            "repeat_y": 1.0,
        },
    }
    route_specs = (
        ("staircase", "stairs", (-0.697, 1.375, 0.515), (0.023, 1.595, 1.095)),
        ("living kitchen", "kitchen", (4.143, 1.375, -2.501), (3.423, 1.595, -1.581)),
        ("living room", "living", (-4.143, 1.375, -2.501), (-3.423, 1.595, -1.581)),
        ("bedroom", "bedroom", (4.143, 1.375, 2.501), (3.423, 1.595, 2.633)),
        ("bedroom 2", "bedroom", (-4.143, 1.375, 2.501), (-3.423, 1.595, 2.633)),
        ("bedroom 3", "bedroom", (-0.037, 1.375, -2.060), (0.683, 1.595, -1.140)),
        ("balcony/terrace", "outdoor", (3.337, 1.375, 0.0), (2.617, 1.595, 0.920)),
    )
    route = [
        {
            "label": label,
            "kind": kind,
            "focus": {"x": focus[0], "y": focus[1], "z": focus[2]},
            "camera": {"x": camera[0], "y": camera[1], "z": camera[2]},
        }
        for label, kind, focus, camera in route_specs
    ]
    manifest["style_scene"] = styles.build_style_scene(
        selected,
        route_stop_count=len(route),
    )
    manifest["walkable_scene"] = {
        "kind": "generated_reconstruction_layout",
        "route": route,
        "rooms": [
            {
                "label": row["label"],
                "position": {
                    "x": row["focus"]["x"],
                    "z": row["focus"]["z"],
                },
                "focus": dict(row["focus"]),
            }
            for row in route
        ],
    }
    return manifest


@pytest.mark.parametrize("style_id", sorted(EXPECTED_CUES))
def test_all_catalog_styles_bind_exact_palette_instances_and_viewer_scene(style_id: str) -> None:
    selected = styles.reconstruction_style(style_id, style_id=style_id)
    scene = styles.build_style_scene(selected, route_stop_count=2)
    assert styles.validate_style_scene(scene, expected_style=selected) == (True, "ready")
    assert set(scene["required_cues"]) == EXPECTED_CUES[style_id]
    assert scene["route_stop_count"] == 2
    assert scene["minimum_instance_count"] == 8
    assert {row["route_index"] for row in scene["instances"]} == {0, 1}
    assert all(EXPECTED_CUES[style_id] <= {
        row["cue"] for row in scene["instances"] if row["route_index"] == route_index
    } for route_index in (0, 1))

    viewer_html = generator._viewer_html(
        manifest=_viewer_manifest(style_id),
        three_relpath="vendor/three.module.js",
        orbit_controls_relpath="vendor/examples/jsm/controls/OrbitControls.js",
    )
    assert f'data-pq-style-id="{style_id}"' in viewer_html
    assert f'data-pq-style-signature="{selected["signature"]}"' in viewer_html
    assert "styleInstances.forEach((instance) => addStyledSceneInstance(instance));" in viewer_html
    assert "const maxStagedRouteStops = 12;" in viewer_html
    assert "const stagedRouteStops = routeStops.slice(0, maxStagedRouteStops);" in viewer_html
    assert (
        "stagedRouteStops.forEach((stop, index) => addGeneratedStagingForStop(stop, index));"
        in viewer_html
    )
    assert (
        "routeStops.forEach((stop, index) => addGeneratedStagingForStop(stop, index));"
        not in viewer_html
    )
    assert "map: null" in viewer_html
    assert "minimumStyledCoveragePct = activeViewMode === \"room\" ? 5.0 : 3.0" in viewer_html
    for cue in EXPECTED_CUES[style_id]:
        assert f'"cue": "{cue}"' in viewer_html


def test_urban_jungle_has_visible_material_cues_not_a_label_only_scene() -> None:
    scene = styles.build_style_scene(
        styles.reconstruction_style("urban_jungle", style_id="urban_jungle"),
        route_stop_count=1,
    )
    by_cue = {row["cue"]: row for row in scene["instances"]}
    assert by_cue["healthy_plants"]["shape"] == "plant"
    assert by_cue["rattan"]["shape"] == "rattan_chair"
    assert by_cue["warm_wood"]["material"] == "timber"
    assert by_cue["linen"]["material"] == "textile"


@pytest.mark.parametrize(
    "tamper",
    ("label", "signature", "route", "nan_dimensions", "missing_cue", "offscreen_position"),
)
def test_style_scene_evidence_fails_closed_on_tamper(tamper: str) -> None:
    selected = styles.reconstruction_style("urban_jungle", style_id="urban_jungle")
    scene = styles.build_style_scene(selected, route_stop_count=2)
    changed = copy.deepcopy(scene)
    if tamper == "label":
        changed["style_label"] = "Urban jungle in name only"
    elif tamper == "signature":
        changed["style_signature"] = "sha256:" + ("0" * 64)
    elif tamper == "route":
        changed["instances"][0]["route_index"] = 99
    elif tamper == "nan_dimensions":
        changed["instances"][0]["dimensions"]["x"] = float("nan")
    elif tamper == "missing_cue":
        changed["instances"] = [
            row for row in changed["instances"] if row["cue"] != "healthy_plants"
        ]
        changed["minimum_instance_count"] = len(changed["instances"])
    elif tamper == "offscreen_position":
        changed["instances"][0]["position"]["x"] = 10_000
    assert styles.validate_style_scene(changed, expected_style=selected)[0] is False


def test_style_aware_cache_rejects_v6_and_a_different_requested_style() -> None:
    urban = styles.reconstruction_style("urban_jungle", style_id="urban_jungle")
    warm = styles.reconstruction_style("warm_scandi", style_id="warm_scandi")
    generated = {
        "viewer_version": styles.GENERATED_RECONSTRUCTION_VIEWER_VERSION,
        "style_id": urban["id"],
        "style_signature": urban["signature"],
        "style_scene_signature": "sha256:" + ("1" * 64),
        "style_evidence_status": "ready",
    }
    assert product_service._property_reconstruction_style_cache_matches(
        generated,
        requested_style=urban,
    )
    assert not product_service._property_reconstruction_style_cache_matches(
        generated,
        requested_style=warm,
    )
    generated["viewer_version"] = "propertyquarry_3d_tour_viewer_v6"
    assert not product_service._property_reconstruction_style_cache_matches(
        generated,
        requested_style=urban,
    )


@pytest.mark.parametrize("stop_count", [1, 2, 4, 6])
def test_walkthrough_duration_floor_is_enforced_for_viewer_and_crossfade_paths(
    stop_count: int,
) -> None:
    viewer_seconds = generator._quality_safe_walkthrough_seconds_per_stop(
        5.0,
        stop_count=stop_count,
        crossfade=False,
    )
    assert viewer_seconds * stop_count >= generator.MIN_WALKTHROUGH_DURATION_SECONDS

    crossfade_seconds = generator._quality_safe_walkthrough_seconds_per_stop(
        5.0,
        stop_count=stop_count,
        crossfade=True,
    )
    encoded_frames = generator._stop_card_walkthrough_encoded_frame_count(
        stop_count=stop_count,
        seconds_per_stop=crossfade_seconds,
    )
    assert (
        encoded_frames / generator.WALKTHROUGH_OUTPUT_FPS
        >= generator.MIN_WALKTHROUGH_DURATION_SECONDS
    )


def test_ikea_walkthrough_browser_keeps_every_style_cue_visible_across_routes(
    tmp_path: Path,
) -> None:
    if not generator._playwright_chromium_capture_available():
        pytest.skip("playwright_missing")
    manifest = _runtime_walkthrough_ikea_viewer_manifest()
    vendor = generator._copy_viewer_vendor_assets(tmp_path)
    Image.new("RGB", (800, 600), (246, 244, 238)).save(
        tmp_path / "source-floorplan.png"
    )
    (tmp_path / "viewer.html").write_text(
        generator._viewer_html(
            manifest=manifest,
            three_relpath=str(vendor["three_relpath"]),
            orbit_controls_relpath=str(vendor["orbit_controls_relpath"]),
        ),
        encoding="utf-8",
    )

    with _serve_directory(tmp_path) as base_url:
        with generator.sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                **generator._playwright_chromium_launch_kwargs(playwright)
            )
            try:
                page = browser.new_page(
                    viewport={"width": 1280, "height": 780},
                    device_scale_factor=1,
                )
                page.goto(f"{base_url}/viewer.html", wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => window.__pqReconstructionDebug?.getRenderMetrics?.().ready === true",
                    timeout=20_000,
                )
                for route_index in range(6):
                    page.evaluate(
                        """(index) => {
                            window.__pqReconstructionDebug.setRouteView(
                                index,
                                { immediate: true },
                            );
                        }""",
                        route_index,
                    )
                    metrics = page.evaluate(
                        "() => window.__pqReconstructionDebug.getRenderMetrics()"
                    )
                    assert metrics["ready"] is True, metrics
                    assert metrics["activeRouteIndex"] == route_index
                    assert metrics["missingVisibleStyleCues"] == []
                    assert metrics["styleCueVisibilityReady"] is True
                    for cue in EXPECTED_CUES["ikea_practical"]:
                        assert metrics["visibleStyleCueInstanceIds"][cue], metrics
                        assert (
                            float(metrics["projectedStyleCueCoveragePct"][cue])
                            >= float(metrics["minimumStyleCueCoveragePct"])
                        )
                        assert float(metrics["visibleStyleCueRayPct"][cue]) > 0
            finally:
                browser.close()


def test_urban_jungle_browser_starts_styled_floor_with_visible_coverage(tmp_path: Path) -> None:
    if not generator._playwright_chromium_capture_available():
        pytest.skip("playwright_missing")
    manifest = _cb82_scale_viewer_manifest()
    vendor = generator._copy_viewer_vendor_assets(tmp_path)
    Image.new("RGB", (800, 600), (246, 244, 238)).save(tmp_path / "source-floorplan.png")
    Image.new("RGB", (960, 640), (218, 205, 184)).save(tmp_path / "source-photo.png")
    (tmp_path / "viewer.html").write_text(
        generator._viewer_html(
            manifest=manifest,
            three_relpath=str(vendor["three_relpath"]),
            orbit_controls_relpath=str(vendor["orbit_controls_relpath"]),
        ),
        encoding="utf-8",
    )

    with _serve_directory(tmp_path) as base_url:
        with generator.sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                **generator._playwright_chromium_launch_kwargs(playwright)
            )
            try:
                page = browser.new_page(
                    viewport={"width": 1280, "height": 720},
                    device_scale_factor=1,
                )
                page.goto(f"{base_url}/viewer.html", wait_until="domcontentloaded")
                deadline = time.monotonic() + 20
                metrics: dict[str, object] = {}
                while time.monotonic() < deadline:
                    metrics = page.evaluate(
                        "() => window.__pqReconstructionDebug?.getRenderMetrics?.() || {}"
                    )
                    if metrics.get("ready"):
                        break
                    page.wait_for_timeout(200)
                assert metrics["ready"] is True, metrics
                assert metrics["styleKey"] == "urban_jungle"
                assert metrics["viewMode"] == "room"
                assert metrics["activeRouteIndex"] == 0
                assert metrics["floorplanLayerState"] == "off"
                assert metrics["floorColorHex"] == manifest["requested_style"]["palette"]["floor"].lower()
                assert metrics["styledObjectCount"] == 8
                assert int(metrics["visibleStyledObjectCount"]) == 4
                assert float(metrics["projectedStyledCoveragePct"]) >= 5.0
                assert set(metrics["styleCueKinds"]) == EXPECTED_CUES["urban_jungle"]
                assert set(metrics["styledInstanceIds"]) == {
                    row["id"] for row in manifest["style_scene"]["instances"]
                }
                assert metrics["missingVisibleStyleCues"] == []
                assert metrics["styleCueVisibilityReady"] is True
                for cue in EXPECTED_CUES["urban_jungle"]:
                    assert metrics["visibleStyleCueInstanceIds"][cue], metrics
                    assert all(
                        "-r01-" in instance_id
                        for instance_id in metrics["visibleStyleCueInstanceIds"][cue]
                    )
                    assert (
                        float(metrics["projectedStyleCueCoveragePct"][cue])
                        >= float(metrics["minimumStyleCueCoveragePct"])
                    )
                    assert float(metrics["visibleStyleCueRayPct"][cue]) > 0
                assert metrics["photoPanelGroupVisible"] is False
                assert metrics["semanticStagingGroupVisible"] is False
                assert metrics["visibleSemanticStagingObjectCount"] == 0
                assert metrics["routeMarkerGroupVisible"] is False
                assert metrics["visibleHotspotCount"] == 0
                assert metrics["cameraInsideGeneratedDecor"] is False
                page.screenshot(path=str(tmp_path / "urban-jungle-initial-room.png"))
                page.click("#view-floorplan-reference")
                assert page.evaluate(
                    "() => window.__pqReconstructionDebug.getRenderMetrics().floorplanLayerState"
                ) == "on"
                page.click("#view-floorplan-reference")
                assert page.evaluate(
                    "() => window.__pqReconstructionDebug.getRenderMetrics().floorplanLayerState"
                ) == "off"
                page.click("#view-overview")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    metrics = page.evaluate(
                        "() => window.__pqReconstructionDebug.getRenderMetrics()"
                    )
                    if metrics.get("viewMode") == "overview" and not metrics.get("isTransitioning"):
                        break
                    page.wait_for_timeout(150)
                assert metrics["viewMode"] == "overview"
                assert metrics["floorColorHex"] == manifest["requested_style"]["palette"]["floor"].lower()
                assert metrics["photoPanelGroupVisible"] is True
                assert metrics["semanticStagingGroupVisible"] is True
                assert metrics["routeMarkerGroupVisible"] is True
                page.screenshot(path=str(tmp_path / "urban-jungle-overview.png"))
                page.click("#view-inside")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    metrics = page.evaluate(
                        "() => window.__pqReconstructionDebug.getRenderMetrics()"
                    )
                    if (
                        metrics.get("viewMode") == "room"
                        and not metrics.get("isTransitioning")
                        and metrics.get("ready")
                    ):
                        break
                    page.wait_for_timeout(150)
                assert metrics["viewMode"] == "room"
                assert metrics["ready"] is True, metrics
                assert metrics["floorColorHex"] == manifest["requested_style"]["palette"]["floor"].lower()
                assert float(metrics["projectedStyledCoveragePct"]) >= 5.0
                assert metrics["missingVisibleStyleCues"] == []
                assert metrics["styleCueVisibilityReady"] is True
                assert metrics["photoPanelGroupVisible"] is False
                assert metrics["visibleSemanticStagingObjectCount"] == 0
                assert metrics["visibleHotspotCount"] == 0
                assert metrics["cameraInsideGeneratedDecor"] is False
                page.screenshot(path=str(tmp_path / "urban-jungle-room.png"))
                page.evaluate(
                    "() => window.__pqReconstructionDebug.setRouteView(1, { immediate: true })"
                )
                page.wait_for_timeout(250)
                metrics = page.evaluate(
                    "() => window.__pqReconstructionDebug.getRenderMetrics()"
                )
                assert metrics["ready"] is True, metrics
                assert metrics["activeRouteIndex"] == 1
                assert metrics["floorColorHex"] == manifest["requested_style"]["palette"]["floor"].lower()
                assert metrics["missingVisibleStyleCues"] == []
                assert metrics["styleCueVisibilityReady"] is True
                for cue in EXPECTED_CUES["urban_jungle"]:
                    assert metrics["visibleStyleCueInstanceIds"][cue], metrics
                    assert all(
                        "-r02-" in instance_id
                        for instance_id in metrics["visibleStyleCueInstanceIds"][cue]
                    )
                focus_receipt = page.evaluate(
                    """() => {
                        const stage = document.querySelector('.stage')?.getBoundingClientRect();
                        const actions = document.querySelector('.viewer-actions')?.getBoundingClientRect();
                        return {
                            windowScrollY: Number(window.scrollY || 0),
                            stageTop: Number(stage?.top || 0),
                            actionsBottom: Number(actions?.bottom || 0),
                            viewportHeight: Number(window.innerHeight || 0),
                        };
                    }"""
                )
                assert focus_receipt["windowScrollY"] == 0
                assert focus_receipt["stageTop"] == pytest.approx(0.0)
                assert focus_receipt["actionsBottom"] <= focus_receipt["viewportHeight"]
                page.screenshot(path=str(tmp_path / "urban-jungle-route-two-room.png"))
            finally:
                browser.close()
