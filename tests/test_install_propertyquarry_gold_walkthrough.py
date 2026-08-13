import json
from pathlib import Path
import stat

import pytest

from scripts.install_propertyquarry_gold_walkthrough import (
    CORE_GOLD_PROVIDER_KEY,
    DESKTOP_RELPATH,
    KARL_FLOORPLAN_REVIEW_RELPATH,
    KARL_PROPERTY_SLUG,
    MOBILE_RELPATH,
    SIDECAR_RELPATH,
    _atomic_copy,
    _atomic_write_json,
    _manifest_walkthrough_path,
    _canonical_object_sha256,
    _verify_rollback_subject,
    build_sidecar,
    updated_manifest,
    updated_private_manifest,
    updated_public_assets,
)


def _karl_floorplan_review() -> dict[str, object]:
    return {
        "schema": "propertyquarry.floorplan_interpretation_review.v2",
        "status": "pass",
        "target_slug": KARL_PROPERTY_SLUG,
        "corrections": {
            "entrance": {
                "entry_room_id": "vorraum",
                "initial_panorama_scene_id": "vorraum",
                "portal_id": "entrance-exit-gate",
            },
            "vertragsgrundlage": {
                "classification": "document_stamp_overlay",
                "architectural_space": False,
                "excluded_from_room_count": True,
                "excluded_from_scene_graph": True,
                "excluded_from_doorway_graph": True,
            },
        },
        "boundary_assertions": {
            "balcony_loggia_access": "living-kitchen-only",
            "stairwell_is_architectural_space": False,
            "stairwell_role": "outside-apartment-entrance-exit-only",
            "terrace_access": "primary-bedroom-only",
        },
        "required_adjacency": [["balcony-loggia", "living-kitchen"]],
        "forbidden_adjacency": [
            ["balcony-loggia", "vorraum"],
            ["balcony-loggia", "terrace"],
            ["living-kitchen", "terrace"],
        ],
        "verified_room_count": 9,
        "verified_architectural_spaces": [
            "terrace",
            "primary-bedroom",
            "separate-wc",
            "bathroom",
            "circulation-hall",
            "guest-bedroom",
            "living-kitchen",
            "vorraum",
            "balcony-loggia",
        ],
        "verified_panorama_scene_count": 7,
        "verified_panorama_scene_ids": [
            "vorraum",
            "bedroom-primary",
            "terrace",
            "bedroom-guest",
            "wc",
            "bath",
            "living-kitchen",
        ],
    }


def test_karl_private_manifest_pins_corrected_floorplan_review(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(_karl_floorplan_review(), sort_keys=True),
        encoding="utf-8",
    )
    updated, proof = updated_private_manifest(
        {
            "three_d_vista_white_label_proof": {
                "source": "3dvista_import_script",
                "non_trial_export_verified": True,
                "private_viewer_verified": True,
                "trial_branding_present": False,
                "floorplan_interpretation_review": "obsolete-review.json",
            }
        },
        property_slug=KARL_PROPERTY_SLUG,
        floorplan_review_path=review_path,
    )

    white_label = updated["three_d_vista_white_label_proof"]
    assert white_label["floorplan_interpretation_review"] == (
        KARL_FLOORPLAN_REVIEW_RELPATH
    )
    assert white_label["floorplan_interpretation_review_sha256"] == proof["sha256"]
    assert white_label["floorplan_interpretation_review_schema"].endswith(".v2")
    assert white_label["floorplan_interpretation_review_status"] == "pass"


def test_karl_private_manifest_rejects_wrong_balcony_topology(
    tmp_path: Path,
) -> None:
    review = _karl_floorplan_review()
    review["boundary_assertions"]["balcony_loggia_access"] = "vorraum"
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")

    with pytest.raises(RuntimeError, match="karl_floorplan_review_invariants_invalid"):
        updated_private_manifest(
            {
                "three_d_vista_white_label_proof": {
                    "source": "3dvista_import_script",
                    "non_trial_export_verified": True,
                    "private_viewer_verified": True,
                    "trial_branding_present": False,
                }
            },
            property_slug=KARL_PROPERTY_SLUG,
            floorplan_review_path=review_path,
        )


def test_atomic_public_files_are_world_readable_and_durable(tmp_path: Path) -> None:
    source = tmp_path / "private-source.mp4"
    source.write_bytes(b"walkthrough")
    source.chmod(0o600)
    copied = tmp_path / "public.mp4"
    written = tmp_path / "public.json"

    _atomic_copy(source, copied)
    _atomic_write_json(written, {"status": "pass"})

    assert stat.S_IMODE(copied.stat().st_mode) == 0o644
    assert stat.S_IMODE(written.stat().st_mode) == 0o644
    assert copied.read_bytes() == b"walkthrough"
    assert json.loads(written.read_text()) == {"status": "pass"}


def test_first_install_rollback_accepts_explicit_absent_walkthrough(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "fresh-bundle"
    bundle_dir.mkdir()
    manifest_path = bundle_dir / "tour.json"
    manifest_path.write_text('{"slug":"fresh-bundle"}\n', encoding="utf-8")
    import hashlib

    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    _verify_rollback_subject(
        bundle_dir=bundle_dir,
        manifest={"slug": "fresh-bundle"},
        manifest_path=manifest_path,
        rollback={
            "manifest": {"sha256": manifest_sha},
            "walkthrough": {"present": False},
        },
    )


def test_first_install_rollback_rejects_absent_claim_when_manifest_has_video(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "fresh-bundle"
    bundle_dir.mkdir()
    manifest_path = bundle_dir / "tour.json"
    manifest = {"slug": "fresh-bundle", "video_relpath": "existing.mp4"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    import hashlib

    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="gold_walkthrough_rollback_video_mismatch"):
        _verify_rollback_subject(
            bundle_dir=bundle_dir,
            manifest=manifest,
            manifest_path=manifest_path,
            rollback={
                "manifest": {"sha256": manifest_sha},
                "walkthrough": {"present": False},
            },
        )


def test_walkable_scene_subject_hash_is_format_independent() -> None:
    left = {"initial_scene_id": "vorraum", "scenes": [{"id": "vorraum"}]}
    right = {"scenes": [{"id": "vorraum"}], "initial_scene_id": "vorraum"}

    assert _canonical_object_sha256(left) == _canonical_object_sha256(right)


def test_rollback_verification_uses_current_manifest_video_not_a_provider_name(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "generic-bundle"
    bundle_dir.mkdir()
    video = bundle_dir / "current-provider-neutral-video.mp4"
    video.write_bytes(b"existing-video")

    assert _manifest_walkthrough_path(
        bundle_dir,
        {"video_relpath": "current-provider-neutral-video.mp4"},
    ) == video.resolve()


def _variant(*, key: str, width: int, height: int, size_bytes: int) -> dict[str, object]:
    return {
        "key": key,
        "sha256": f"{key}-sha",
        "metadata": {
            "width": width,
            "height": height,
            "size_bytes": size_bytes,
            "avg_frame_rate": "60/1",
        },
    }


def _source_receipt() -> dict[str, object]:
    return {
        "property_slug": "danube-flats",
        "walkable_scene_sha256": "a" * 64,
        "initial_scene_id": "vorraum",
        "route_scene_ids": ["vorraum", "living-kitchen"],
        "representation_kind": "normal_camera_mono",
        "default_walkthrough": True,
        "optional_spatial_tour_unchanged": True,
    }


def test_updated_public_assets_preserves_existing_assets_and_replaces_video_rows() -> None:
    assets = updated_public_assets(
        {
            "public_assets": [
                {"path": "preview.png", "role": "preview", "privacy_class": "public"},
                {"path": DESKTOP_RELPATH, "role": "stale"},
                {"path": MOBILE_RELPATH, "role": "stale"},
            ]
        }
    )

    assert assets[0]["path"] == "preview.png"
    assert assets[1] == {
        "path": DESKTOP_RELPATH,
        "privacy_class": "public",
        "role": "video",
        "mime_type": "video/mp4",
    }
    assert assets[2] == {
        "path": MOBILE_RELPATH,
        "privacy_class": "public",
        "role": "video_mobile",
        "mime_type": "video/mp4",
    }


def test_core_gold_sidecar_is_provider_independent(
    tmp_path: Path,
) -> None:
    source_receipt = tmp_path / "source.json"
    desktop_receipt = tmp_path / "desktop.json"
    mobile_receipt = tmp_path / "mobile.json"
    for path in (source_receipt, desktop_receipt, mobile_receipt):
        path.write_text("{}\n", encoding="utf-8")

    sidecar = build_sidecar(
        source_receipt={
            "composition": "boundary_verified_frame_continuation",
            "continuity_repair_status": "pass",
        },
        source_receipt_path=source_receipt,
        desktop_receipt_path=desktop_receipt,
        desktop_variant=_variant(
            key="desktop", width=1920, height=1080, size_bytes=50_000_000
        ),
        mobile_receipt_path=mobile_receipt,
        mobile_variant=_variant(
            key="mobile", width=1280, height=720, size_bytes=30_000_000
        ),
        generated_at="2026-07-10T15:00:00Z",
    )

    assert SIDECAR_RELPATH == "tour.walkthrough.json"
    assert sidecar["contract_name"] == "propertyquarry.core_gold_walkthrough.v1"
    assert sidecar["provider_key"] == CORE_GOLD_PROVIDER_KEY
    assert sidecar["provider_backend_key"] == CORE_GOLD_PROVIDER_KEY
    assert "magicfit_import" not in sidecar
    assert not any("magicfit" in key.lower() for key in sidecar)


def test_updated_manifest_selects_60fps_desktop_and_mobile_variants() -> None:
    manifest = updated_manifest(
        {"slug": "danube-flats", "public_assets": [{"path": "preview.png"}]},
        source_receipt=_source_receipt(),
        desktop_variant=_variant(key="desktop", width=1920, height=1080, size_bytes=50_000_000),
        mobile_variant=_variant(key="mobile", width=1280, height=720, size_bytes=30_000_000),
        generated_at="2026-07-10T15:00:00Z",
    )

    assert manifest["video_relpath"] == DESKTOP_RELPATH
    assert manifest["video_mobile_relpath"] == MOBILE_RELPATH
    assert manifest["video_provider"] == CORE_GOLD_PROVIDER_KEY
    assert manifest["video_provider_key"] == CORE_GOLD_PROVIDER_KEY
    assert manifest["video_sidecar_relpath"] == "tour.walkthrough.json"
    assert manifest["video_coverage_proof"] == "boundary_verified_frame_continuation"
    assert manifest["core_gold_walkthrough"]["desktop_frame_rate"] == "60/1"
    assert manifest["core_gold_walkthrough"]["mobile_frame_rate"] == "60/1"
    assert manifest["core_gold_walkthrough"]["frame_duplication_only"] is False
    assert "magicfit_import" not in manifest
    assert "magicfit" not in json.dumps(manifest, sort_keys=True).lower()


def test_updated_manifest_removes_legacy_magicfit_claims_and_asset_rows() -> None:
    manifest = updated_manifest(
        {
            "slug": "danube-flats",
            "video_provider": "magicfit",
            "video_sidecar_relpath": "tour.magicfit.json",
            "magicfit_import": {"launch_eligible": True},
            "magicfit_stale_claim": "remove-me",
            "public_assets": [
                {"path": "preview.png"},
                {"path": "magicfit-walkthrough.mp4"},
                {"path": "magicfit-walkthrough-desktop-1080p60.mp4"},
                {"path": "magicfit-walkthrough-mobile-720p60.mp4"},
                {"path": "tour.magicfit.json"},
            ],
        },
        source_receipt=_source_receipt(),
        desktop_variant=_variant(
            key="desktop", width=1920, height=1080, size_bytes=50_000_000
        ),
        mobile_variant=_variant(
            key="mobile", width=1280, height=720, size_bytes=30_000_000
        ),
        generated_at="2026-07-10T15:00:00Z",
    )

    serialized = json.dumps(manifest, sort_keys=True).lower()
    assert "magicfit" not in serialized
    assert [row["path"] for row in manifest["public_assets"]] == [
        "preview.png",
        DESKTOP_RELPATH,
        MOBILE_RELPATH,
    ]


def test_updated_manifest_fails_closed_on_unknown_nested_provider_claim() -> None:
    with pytest.raises(
        RuntimeError,
        match="core_gold_manifest_provider_claims_remain",
    ):
        updated_manifest(
            {
                "slug": "danube-flats",
                "historical_delivery": {"provider": "magicfit"},
            },
            source_receipt=_source_receipt(),
            desktop_variant=_variant(
                key="desktop",
                width=1920,
                height=1080,
                size_bytes=50_000_000,
            ),
            mobile_variant=_variant(
                key="mobile",
                width=1280,
                height=720,
                size_bytes=30_000_000,
            ),
            generated_at="2026-07-10T15:00:00Z",
        )
