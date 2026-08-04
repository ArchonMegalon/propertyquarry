import hashlib
import json
from pathlib import Path

import pytest

from app.api.routes.public_tours import (
    _public_tour_canonical_object_sha256,
    _public_tour_walkthrough_acceptance,
)
from app.api.routes.public_tour_payloads import (
    build_public_tour_manifest,
    canonical_public_tour_payload,
)


def _payload(sidecar_key: str, sidecar_relpath: str) -> dict[str, object]:
    return {
        "slug": "walkthrough-sidecar-fixture",
        "title": "Walkthrough sidecar fixture",
        "publication_status": "published",
        "tour_privacy_mode": "anonymous_public",
        sidecar_key: sidecar_relpath,
    }


def test_canonical_manifest_retains_safe_walkthrough_sidecar_reference(
    tmp_path: Path,
) -> None:
    payload = _payload("video_sidecar_relpath", "tour.walkthrough.json")

    canonical = canonical_public_tour_payload(payload, bundle_dir=tmp_path)

    assert canonical["video_sidecar_relpath"] == "tour.walkthrough.json"
    assert canonical_public_tour_payload(canonical, bundle_dir=tmp_path) == canonical


def test_public_payload_hides_walkthrough_sidecar_reference(tmp_path: Path) -> None:
    payload = _payload("walkthrough_sidecar_relpath", "review/walkthrough.json")

    public = build_public_tour_manifest(
        payload,
        expose_asset_relpaths=False,
        url_allowed=lambda _url: False,
        bundle_dir_resolver=lambda _slug: tmp_path,
    ).as_dict()

    assert "walkthrough_sidecar_relpath" not in public


def test_canonical_manifest_drops_unsafe_walkthrough_sidecar_reference(
    tmp_path: Path,
) -> None:
    canonical = canonical_public_tour_payload(
        _payload("video_sidecar_relpath", "../tour.private.json"),
        bundle_dir=tmp_path,
    )

    assert "video_sidecar_relpath" not in canonical


def _core_gold_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    desktop_relpath = "walkthrough-desktop-1080p60.mp4"
    mobile_relpath = "walkthrough-mobile-720p60.mp4"
    desktop_bytes = b"privacy-safe-desktop-walkthrough"
    mobile_bytes = b"privacy-safe-mobile-walkthrough"
    (tmp_path / desktop_relpath).write_bytes(desktop_bytes)
    (tmp_path / mobile_relpath).write_bytes(mobile_bytes)
    walkable_scene = {
        "initial_scene_id": "entry",
        "scenes": [
            {
                "id": "entry",
                "label": "Entrance",
                "hotspots": [{"target_scene_id": "living"}],
            },
            {"id": "living", "label": "Living room", "hotspots": []},
        ],
    }
    payload: dict[str, object] = {
        "slug": "walkthrough-sidecar-fixture",
        "video_relpath": desktop_relpath,
        "video_mobile_relpath": mobile_relpath,
        "video_sidecar_relpath": "tour.walkthrough.json",
        "walkable_scene": walkable_scene,
    }
    sidecar: dict[str, object] = {
        "contract_name": "propertyquarry.core_gold_walkthrough.v1",
        "property_slug": payload["slug"],
        "provider_key": "propertyquarry_core_gold",
        "provider_backend_key": "propertyquarry_core_gold",
        "status": "installed",
        "delivery_status": "installed",
        "launch_eligible": True,
        "full_decode_verified": True,
        "motion_interpolation_verified": True,
        "frame_duplication_only": False,
        "continuity_repair_status": "pass",
        "composition": "manifest_graph_bound_panorama_camera_walkthrough",
        "representation_kind": "normal_camera_mono",
        "default_walkthrough": True,
        "optional_spatial_tour_unchanged": True,
        "walkable_scene_sha256": _public_tour_canonical_object_sha256(walkable_scene),
        "initial_scene_id": "entry",
        "route_scene_ids": ["entry", "living"],
        "route_labels": ["Entrance", "Living room"],
        "covered_route_labels": ["Entrance", "Living room"],
        "segment_count": 2,
        "transition_offsets_seconds": [1.0],
        "boundary_checks": [
            {"source": "entry", "target": "living", "status": "pass"}
        ],
        "video_relpath": desktop_relpath,
        "video_mobile_relpath": mobile_relpath,
        "video_sha256": hashlib.sha256(desktop_bytes).hexdigest(),
        "video_mobile_sha256": hashlib.sha256(mobile_bytes).hexdigest(),
        "video_metadata": {
            "width": 1920,
            "height": 1080,
            "codec_name": "h264",
            "avg_frame_rate": "60/1",
            "duration_seconds": 2.0,
            "nb_frames": 120,
            "size_bytes": len(desktop_bytes),
        },
        "video_mobile_metadata": {
            "width": 1280,
            "height": 720,
            "codec_name": "h264",
            "avg_frame_rate": "60/1",
            "duration_seconds": 2.0,
            "nb_frames": 120,
            "size_bytes": len(mobile_bytes),
        },
    }
    (tmp_path / "tour.walkthrough.json").write_text(
        json.dumps(sidecar),
        encoding="utf-8",
    )
    return payload, sidecar


def test_core_gold_acceptance_uses_private_sidecar_not_stripped_public_attestations(
    tmp_path: Path,
) -> None:
    payload, _sidecar = _core_gold_fixture(tmp_path)

    acceptance = _public_tour_walkthrough_acceptance(payload, bundle_dir=tmp_path)

    assert acceptance["allowed"] is True
    assert acceptance["status"] == "core_gold_accepted"
    assert acceptance["verified_video_relpaths"] == [
        "walkthrough-desktop-1080p60.mp4",
        "walkthrough-mobile-720p60.mp4",
    ]


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        ({"provider_key": "untrusted"}, "core_gold_provider_invalid"),
        (
            {
                "boundary_checks": [
                    {"source": "entry", "target": "living", "status": "fail"}
                ]
            },
            "core_gold_route_boundary_invalid",
        ),
    ],
)
def test_core_gold_private_sidecar_remains_fail_closed(
    tmp_path: Path,
    mutation: dict[str, object],
    expected_status: str,
) -> None:
    payload, sidecar = _core_gold_fixture(tmp_path)
    sidecar.update(mutation)
    (tmp_path / "tour.walkthrough.json").write_text(
        json.dumps(sidecar),
        encoding="utf-8",
    )

    acceptance = _public_tour_walkthrough_acceptance(payload, bundle_dir=tmp_path)

    assert acceptance["allowed"] is False
    assert acceptance["status"] == expected_status
