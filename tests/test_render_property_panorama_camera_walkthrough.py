from pathlib import Path

from PIL import Image
import pytest

from scripts.render_property_panorama_camera_walkthrough import (
    default_route,
    scene_graph,
    validate_route,
    yaw_commands,
)


def _fixture(tmp_path: Path) -> tuple[dict[str, object], Path]:
    bundle = tmp_path / "tour"
    panoramas = bundle / "panoramas"
    panoramas.mkdir(parents=True)
    for scene_id in ("vorraum", "living", "bedroom"):
        Image.new("RGB", (1024, 512), color=(220, 214, 198)).save(
            panoramas / f"{scene_id}.jpg"
        )
    manifest = {
        "slug": "karl",
        "walkable_scene": {
            "initial_scene_id": "vorraum",
            "scenes": [
                {
                    "id": "vorraum",
                    "label": "VR / Vorraum",
                    "asset_relpath": "panoramas/vorraum.jpg",
                    "hotspots": [{"target_scene_id": "living"}],
                },
                {
                    "id": "living",
                    "label": "Wohnküche",
                    "asset_relpath": "panoramas/living.jpg",
                    "hotspots": [
                        {"target_scene_id": "vorraum"},
                        {"target_scene_id": "bedroom"},
                    ],
                },
                {
                    "id": "bedroom",
                    "label": "Bedroom",
                    "asset_relpath": "panoramas/bedroom.jpg",
                    "hotspots": [{"target_scene_id": "living"}],
                },
            ],
        },
    }
    return manifest, bundle


def test_default_route_starts_in_vorraum_and_uses_only_hotspot_edges(
    tmp_path: Path,
) -> None:
    manifest, bundle = _fixture(tmp_path)
    initial, scenes, edges = scene_graph(manifest, bundle)
    route = default_route(initial, scenes, edges)

    assert route == ["vorraum", "living", "bedroom"]
    assert validate_route(
        route, initial=initial, scenes=scenes, edges=edges
    ) == route


def test_route_rejects_floorplan_shortcut_not_present_in_hotspots(
    tmp_path: Path,
) -> None:
    manifest, bundle = _fixture(tmp_path)
    initial, scenes, edges = scene_graph(manifest, bundle)

    with pytest.raises(
        RuntimeError,
        match="camera_walkthrough_route_edge_invalid:vorraum:bedroom",
    ):
        validate_route(
            ["vorraum", "bedroom", "living"],
            initial=initial,
            scenes=scenes,
            edges=edges,
        )


def test_yaw_commands_target_named_v360_filter_and_stay_bounded() -> None:
    commands = yaw_commands(start_yaw=170.0, duration_seconds=3.0, direction=1)

    assert "v360@view yaw" in commands
    values = [float(command.rsplit(" ", 1)[1]) for command in commands.split(";")]
    assert min(values) >= -180.0
    assert max(values) <= 180.0
