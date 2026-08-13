from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.api.routes import public_tours


def _write_bundle(bundle_dir: Path) -> dict[str, object]:
    desktop = b"desktop-core-gold-walkthrough"
    mobile = b"mobile-core-gold-walkthrough"
    (bundle_dir / "walkthrough-desktop-1080p60.mp4").write_bytes(desktop)
    (bundle_dir / "walkthrough-mobile-720p60.mp4").write_bytes(mobile)
    walkable_scene = {
        "initial_scene_id": "vorraum",
        "scenes": [
            {
                "id": "vorraum",
                "label": "VR / Vorraum",
                "hotspots": [{"target_scene_id": "living-kitchen"}],
            },
            {
                "id": "living-kitchen",
                "label": "Living / Kitchen",
                "hotspots": [{"target_scene_id": "vorraum"}],
            },
        ],
    }
    scene_hash = public_tours._public_tour_canonical_object_sha256(walkable_scene)
    route_ids = ["vorraum", "living-kitchen"]
    subject = {
        "contract_name": "propertyquarry.core_gold_walkthrough.v1",
        "property_slug": "core-gold-home",
        "walkable_scene_sha256": scene_hash,
        "initial_scene_id": "vorraum",
        "route_scene_ids": route_ids,
        "representation_kind": "normal_camera_mono",
        "default_walkthrough": True,
        "optional_spatial_tour_unchanged": True,
    }
    desktop_sha = hashlib.sha256(desktop).hexdigest()
    mobile_sha = hashlib.sha256(mobile).hexdigest()
    core_gold = {
        **subject,
        "source": "continuity_repaired_motion_interpolated_walkthrough",
        "desktop_target_relpath": "walkthrough-desktop-1080p60.mp4",
        "desktop_sha256": desktop_sha,
        "desktop_size_bytes": len(desktop),
        "desktop_frame_rate": "60/1",
        "mobile_target_relpath": "walkthrough-mobile-720p60.mp4",
        "mobile_sha256": mobile_sha,
        "mobile_size_bytes": len(mobile),
        "mobile_frame_rate": "60/1",
        "continuity_repair_verified": True,
        "motion_interpolation_verified": True,
        "frame_duplication_only": False,
    }
    payload: dict[str, object] = {
        "slug": "core-gold-home",
        "walkable_scene": walkable_scene,
        "video_relpath": "walkthrough-desktop-1080p60.mp4",
        "video_mobile_relpath": "walkthrough-mobile-720p60.mp4",
        "flythrough_video_relpath": "walkthrough-desktop-1080p60.mp4",
        "video_sidecar_relpath": "tour.walkthrough.json",
        "video_provider": "propertyquarry_core_gold",
        "video_provider_key": "propertyquarry_core_gold",
        "video_coverage_proof": "boundary_verified_frame_continuation",
        "core_gold_walkthrough": core_gold,
    }
    sidecar = {
        **subject,
        "provider": "PropertyQuarry Core Gold",
        "provider_key": "propertyquarry_core_gold",
        "provider_backend_key": "propertyquarry_core_gold",
        "status": "installed",
        "delivery_status": "installed",
        "launch_eligible": True,
        "composition": "manifest_graph_bound_panorama_camera_walkthrough",
        "continuity_repair_status": "pass",
        "continuity_repair_method": "hotspot_graph_bound_crossfade",
        "segment_count": 2,
        "route_labels": ["VR / Vorraum", "Living / Kitchen"],
        "covered_route_labels": ["VR / Vorraum", "Living / Kitchen"],
        "boundary_checks": [
            {"source": "vorraum", "target": "living-kitchen", "status": "pass"}
        ],
        "transition_offsets_seconds": [2.8],
        "video_relpath": "walkthrough-desktop-1080p60.mp4",
        "video_sha256": desktop_sha,
        "video_metadata": {
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "60/1",
            "duration_seconds": 6.0,
            "nb_frames": 360,
            "size_bytes": len(desktop),
        },
        "video_mobile_relpath": "walkthrough-mobile-720p60.mp4",
        "video_mobile_sha256": mobile_sha,
        "video_mobile_metadata": {
            "codec_name": "h264",
            "width": 1280,
            "height": 720,
            "avg_frame_rate": "60/1",
            "duration_seconds": 6.0,
            "nb_frames": 360,
            "size_bytes": len(mobile),
        },
        "motion_interpolation_verified": True,
        "frame_duplication_only": False,
        "full_decode_verified": True,
    }
    (bundle_dir / "tour.walkthrough.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )
    return payload


def test_core_gold_walkthrough_accepts_exact_graph_and_video_hashes(tmp_path: Path) -> None:
    payload = _write_bundle(tmp_path)

    acceptance = public_tours._public_tour_walkthrough_acceptance(
        payload, bundle_dir=tmp_path
    )

    assert acceptance["allowed"] is True
    assert acceptance["status"] == "core_gold_accepted"
    assert acceptance["verified_video_relpaths"] == [
        "walkthrough-desktop-1080p60.mp4",
        "walkthrough-mobile-720p60.mp4",
    ]


def test_core_gold_walkthrough_rejects_video_tampering(tmp_path: Path) -> None:
    payload = _write_bundle(tmp_path)
    (tmp_path / "walkthrough-desktop-1080p60.mp4").write_bytes(b"tampered-video")

    acceptance = public_tours._public_tour_walkthrough_acceptance(
        payload, bundle_dir=tmp_path
    )

    assert acceptance["allowed"] is False
    assert acceptance["status"] == "core_gold_video_unavailable"


def test_core_gold_walkthrough_rejects_provider_alias_tampering(tmp_path: Path) -> None:
    payload = _write_bundle(tmp_path)
    payload["video_provider"] = "unreviewed-provider"

    acceptance = public_tours._public_tour_walkthrough_acceptance(
        payload, bundle_dir=tmp_path
    )

    assert acceptance["allowed"] is False
    assert acceptance["status"] == "core_gold_provider_invalid"


def test_core_gold_walkthrough_rejects_route_shortcuts(tmp_path: Path) -> None:
    payload = _write_bundle(tmp_path)
    walkable_scene = dict(payload["walkable_scene"])
    scenes = [dict(row) for row in walkable_scene["scenes"]]
    scenes[0]["hotspots"] = []
    walkable_scene["scenes"] = scenes
    payload["walkable_scene"] = walkable_scene
    scene_hash = public_tours._public_tour_canonical_object_sha256(walkable_scene)
    payload["core_gold_walkthrough"]["walkable_scene_sha256"] = scene_hash
    sidecar_path = tmp_path / "tour.walkthrough.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["walkable_scene_sha256"] = scene_hash
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    acceptance = public_tours._public_tour_walkthrough_acceptance(
        payload, bundle_dir=tmp_path
    )

    assert acceptance["allowed"] is False
    assert acceptance["status"] == "core_gold_route_shortcut_detected"


def test_core_gold_malformed_metadata_fails_closed(tmp_path: Path) -> None:
    payload = _write_bundle(tmp_path)
    sidecar_path = tmp_path / "tour.walkthrough.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["video_metadata"]["size_bytes"] = "not-an-integer"
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    acceptance = public_tours._public_tour_walkthrough_acceptance(
        payload, bundle_dir=tmp_path
    )

    assert acceptance["allowed"] is False
    assert acceptance["status"] == "core_gold_acceptance_invalid"
