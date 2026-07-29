from __future__ import annotations

import json
import subprocess
import sys

from app.api.routes import public_tour_payloads


def test_public_tour_manifest_allowlist_excludes_private_source_fields() -> None:
    forbidden = {
        "brief",
        "listing_url",
        "property_url",
        "source_url",
        "source_ref",
        "external_id",
        "principal_id",
        "recipient_email",
        "source_virtual_tour_url",
        "source_virtual_tour_origin",
        "panorama_source",
        "three_d_vista_url",
        "matterport_url",
        "exact_address",
        "map_lat",
        "map_lng",
        "video_provider",
        "video_provider_key",
        "video_render_provider",
        "video_coverage_proof",
    }

    assert forbidden.isdisjoint(public_tour_payloads._PUBLIC_TOUR_TOP_LEVEL_KEYS)


def test_public_tour_projection_drops_private_video_provider_fields() -> None:
    payload = {
        "slug": "private-video-provider-contract",
        "title": "Private provider contract",
        "video_provider": "internal_renderer",
        "video_provider_key": "internal_renderer_key",
        "video_render_provider": "internal_render_lane",
        "video_coverage_proof": "internal_acceptance_receipt",
    }

    projection = public_tour_payloads.build_public_tour_manifest(
        payload,
        url_allowed=lambda _value: False,
        bundle_dir_resolver=lambda _slug: None,
    ).as_dict()

    assert "video_provider" not in projection
    assert "video_provider_key" not in projection
    assert "video_render_provider" not in projection
    assert "video_coverage_proof" not in projection
    assert "internal_renderer" not in json.dumps(projection)

    private_receipt = public_tour_payloads.PrivateTourReceipt.from_payload(
        payload
    ).as_dict()
    assert private_receipt["video_provider"] == "internal_renderer"
    assert private_receipt["video_provider_key"] == "internal_renderer_key"
    assert private_receipt["video_render_provider"] == "internal_render_lane"
    assert private_receipt["video_coverage_proof"] == (
        "internal_acceptance_receipt"
    )


def test_governed_public_projection_drops_external_media_and_embedded_authority_marker() -> None:
    payload = {
        "slug": "governed-tour",
        "title": "Governed tour",
        "governed_spatial": {
            "artifact_verified": True,
            "private_url": "https://private.invalid/task",
        },
        "scenes": [
            {
                "scene_id": "scene-1",
                "role": "live_360",
                "image_url": "https://provider.invalid/private-asset.jpg",
            }
        ],
    }

    projection = public_tour_payloads.build_public_tour_manifest(
        payload,
        url_allowed=lambda _value: True,
        bundle_dir_resolver=lambda _slug: None,
    ).as_dict()

    assert projection["scenes"] == []
    assert "governed_spatial" not in projection
    assert "private.invalid" not in str(projection)
    assert "provider.invalid" not in str(projection)


def test_public_projection_preserves_coarse_location_while_scrubbing_copied_exact_address() -> None:
    payload = {
        "slug": "coarse-location-contract",
        "display_title": "Private Generated Street 9, 1190 Wien",
        "facts": {
            "postal_name": "1190 Wien",
            "city": "Wien",
            "district": "Döbling",
            "municipality": "Vienna",
            "exact_address": "Private Generated Street 9, 1190 Wien",
            "address_lines": ["Private Generated Street 9", "1190 Wien"],
        },
    }

    projection = public_tour_payloads.build_public_tour_manifest(
        payload,
        url_allowed=lambda _value: False,
        bundle_dir_resolver=lambda _slug: None,
    ).as_dict()

    assert projection["facts"] == {
        "postal_name": "1190 Wien",
        "city": "Wien",
        "district": "Döbling",
        "municipality": "Vienna",
    }
    assert projection["display_title"] == "Property tour"
    assert "Private Generated Street" not in json.dumps(projection, sort_keys=True)


def test_public_tour_manifest_contract_script_passes() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_property_public_tour_manifest_contract.py"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "ok: property public tour manifest contract" in result.stdout
