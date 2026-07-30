#!/usr/bin/env python3
"""Build the sealed-source AI-panorama bundle for the Karl-Czerny showcase."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


SLUG = "karl-czerny-gasse-2-urban-jungle"
PROPERTY_URL = (
    "https://propertyquarry.com/app/research/"
    "karl-czerny-gasse-2-private-showcase"
)
DISCLOSURE = (
    "AI-reconstructed from an operator-provided architectural floorplan; "
    "not a captured 360 or measured survey."
)
SCENES = (
    ("hall", "Hall · entrance", 66.0, 57.0),
    ("bedroom-primary", "Bedroom · 16.86 m²", 34.0, 54.0),
    ("terrace", "Terrace · 7.78 m²", 22.0, 55.0),
    ("bedroom-guest", "Bedroom · 15.24 m²", 80.0, 47.0),
    ("wc", "Separate WC · 1.36 m²", 48.0, 42.0),
    ("bath", "Bathroom · 4.96 m²", 58.0, 41.0),
    ("living-kitchen", "Wohnküche · 30.33 m²", 61.0, 67.0),
)
SCENE_INPUT_NAMES = {
    "hall": "karl-czerny-hall.png",
    "bedroom-primary": "karl-czerny-bedroom-16-86.png",
    "terrace": "karl-czerny-terrace.png",
    "bedroom-guest": "karl-czerny-bedroom-15-24.png",
    "wc": "karl-czerny-wc.png",
    "bath": "karl-czerny-bath.png",
    "living-kitchen": "karl-czerny-living-kitchen.png",
}
HOTSPOTS = {
    "hall": (
        ("Enter primary bedroom", "bedroom-primary", -92),
        ("Enter second bedroom", "bedroom-guest", 86),
        ("Open separate WC", "wc", -32),
        ("Enter bathroom", "bath", 28),
        ("Continue to Wohnküche", "living-kitchen", 154),
    ),
    "bedroom-primary": (
        ("Return to hall", "hall", 112),
        ("Step onto terrace", "terrace", -76),
    ),
    "terrace": (("Return to primary bedroom", "bedroom-primary", 180),),
    "bedroom-guest": (("Return to hall", "hall", -128),),
    "wc": (("Return to hall", "hall", 168),),
    "bath": (("Return to hall", "hall", 172),),
    "living-kitchen": (("Return to hall", "hall", -154),),
}
SPATIAL_ROOMS = (
    ("terrace", "Terrace · 7.78 m²", "terrace", "exterior", 0.0, 0.0, 1.5, 4.9),
    ("primary-bedroom", "Bedroom · 16.86 m²", "bedroom-primary", "interior", 1.6, 0.0, 2.9, 4.9),
    ("separate-wc", "Separate WC · 1.36 m²", "wc", "interior", 4.6, 0.0, 1.0, 2.0),
    ("bathroom", "Bathroom · 4.96 m²", "bath", "interior", 5.7, 0.0, 2.4, 2.0),
    ("hall", "Hall · 3.50 m²", "hall", "interior", 8.2, 0.0, 2.0, 2.0),
    ("guest-bedroom", "Bedroom · 15.24 m²", "bedroom-guest", "interior", 10.3, 0.0, 4.6, 3.3),
    ("living-kitchen", "Wohnküche · 30.33 m²", "living-kitchen", "interior", 4.6, 2.2, 6.2, 4.9),
)
WALKTHROUGH_CHAPTERS = (
    ("Entrance hall", 0.0),
    ("Primary bedroom", 5.0),
    ("Terrace", 16.0),
    ("Return via primary bedroom", 23.0),
    ("Central hall return", 27.0),
    ("Second bedroom", 32.0),
    ("Separate WC", 42.0),
    ("Bathroom", 50.0),
    ("Return to central hall", 59.0),
    ("Wohnküche", 74.0),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _save_panorama(source: Path, target: Path) -> None:
    with Image.open(source) as opened:
        image = opened.convert("RGB").resize((4096, 2048), Image.Resampling.LANCZOS)
        image.save(
            target,
            format="JPEG",
            quality=92,
            subsampling=0,
            optimize=True,
            progressive=True,
        )


def _save_floorplan(source: Path, target: Path) -> None:
    with Image.open(source) as opened:
        image = opened.convert("RGB")
        image.thumbnail((2400, 1800), Image.Resampling.LANCZOS)
        image.save(target, format="WEBP", quality=92, method=6)


def _scene_payload(
    scene_id: str,
    label: str,
    floorplan_x: float,
    floorplan_y: float,
) -> dict[str, object]:
    return {
        "asset_relpath": f"panoramas/{scene_id}.jpg",
        "floorplan_x_pct": floorplan_x,
        "floorplan_y_pct": floorplan_y,
        "hotspots": [
            {
                "label": hotspot_label,
                "pitch": -13,
                "target_scene_id": target,
                "yaw": yaw,
            }
            for hotspot_label, target, yaw in HOTSPOTS[scene_id]
        ],
        "id": scene_id,
        "label": label,
        "mime_type": "image/jpeg",
        "privacy_class": "public",
        "projection": "equirectangular",
        "role": "panorama",
        "start_fov": 72,
        "start_pitch": 0,
        "start_yaw": 0,
    }


def build(args: argparse.Namespace) -> Path:
    input_dir = args.input_dir.expanduser().resolve()
    floorplan_source = args.floorplan.expanduser().resolve()
    diorama_source = args.diorama.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    bundle = output_root / SLUG
    if bundle.exists():
        raise RuntimeError("target_bundle_exists")
    for path in (
        floorplan_source,
        diorama_source,
        *(input_dir / name for name in SCENE_INPUT_NAMES.values()),
    ):
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"required_input_missing:{path.name}")

    (bundle / "panoramas").mkdir(parents=True)
    (bundle / "proof").mkdir()
    _save_floorplan(floorplan_source, bundle / "floorplan.webp")
    shutil.copyfile(diorama_source, bundle / "diorama-preview.png")

    raw_asset_hashes: dict[str, str] = {}
    panorama_hashes: dict[str, str] = {}
    for scene_id, _label, _x, _y in SCENES:
        source = input_dir / SCENE_INPUT_NAMES[scene_id]
        target = bundle / "panoramas" / f"{scene_id}.jpg"
        raw_asset_hashes[scene_id] = _sha256(source)
        _save_panorama(source, target)
        panorama_hashes[scene_id] = _sha256(target)

    property_url_sha256 = hashlib.sha256(PROPERTY_URL.encode("utf-8")).hexdigest()
    floorplan_sha256 = _sha256(bundle / "floorplan.webp")
    provenance = {
        "contract_name": "propertyquarry.ai_panorama_provenance.v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "property_binding_kind": "propertyquarry_showcase_url_sha256",
        "property_url_sha256": property_url_sha256,
        "source_listing_id": "karl-czerny-gasse-2-private-showcase",
        "source_artifact_id": floorplan_sha256,
        "generation_method": "ai_image_reconstruction",
        "captured_360": False,
        "measured_survey": False,
        "representation_disclosure": DISCLOSURE,
        "source_image_sha256": [raw_asset_hashes[scene_id] for scene_id, *_rest in SCENES],
        "floorplan_sha256": floorplan_sha256,
        "spatial_model_basis": "floorplan_scaled_approximation",
        "spatial_model_measured": False,
        "spatial_scene_ids": [scene_id for scene_id, *_rest in SCENES],
        "raw_generated_asset_sha256": raw_asset_hashes,
        "panorama_asset_sha256": panorama_hashes,
        "release_transform": {
            "source_dimensions": "1774x887",
            "output_dimensions": "4096x2048",
            "filter": "Pillow LANCZOS",
            "jpeg_quality": 92,
            "sampling_factor": "4:4:4",
            "content_generation_after_source": False,
        },
        "visual_style": {
            "id": "urban_jungle",
            "label": "Urban Jungle",
            "reference_interaction_model": "Matterport-like spatial navigation",
            "reference_presentation_model": "Crezlo-style photographic panorama tour",
        },
        "source_scope": {
            "supported_space": (
                "Wohnküche, both bedrooms, hall, separate WC, bathroom, "
                "and terrace as an exterior room"
            ),
            "unsupported_rooms_omitted": False,
            "source_photo_count": 0,
            "source_floorplan_count": 1,
            "generated_panorama_count": len(SCENES),
        },
    }
    provenance_path = bundle / "proof" / "provenance.json"
    _write_json(provenance_path, provenance)

    tour = {
        "brand_name": "PropertyQuarry",
        "control_mode": "ai_panorama_360",
        "creation_mode": "ai_image_reconstruction",
        "diorama_preview_relpath": "diorama-preview.png",
        "display_title": "Karl-Czerny-Gasse 2 · Urban Jungle 360",
        "facts": {
            "photographic_panorama_nodes": len(SCENES),
            "source_floorplan_count": 1,
            "source_scope": provenance["source_scope"]["supported_space"],
            "style_id": "urban_jungle",
            "walkthrough_provider_required": True,
        },
        "property_url_sha256": property_url_sha256,
        "publication_status": "ready",
        "scene_count": len(SCENES),
        "scene_strategy": "photographic_multi_node_full_apartment_with_terrace",
        "slug": SLUG,
        "title": "Karl-Czerny-Gasse 2 · Urban Jungle floorplan concept tour",
        "tour_privacy_mode": "public_first_party",
        "walkthrough_chapters": [
            {
                "label": label,
                "start_seconds": start_seconds,
            }
            for label, start_seconds in WALKTHROUGH_CHAPTERS
        ],
        "walkable_scene": {
            "acceptance": {
                "contract_name": "propertyquarry.ai_panorama_acceptance.v1",
                "panorama_asset_sha256": panorama_hashes,
                "proof_status": "pending",
                "property_url_sha256": property_url_sha256,
                "provenance_relpath": "proof/provenance.json",
                "provenance_sha256": _sha256(provenance_path),
            },
            "expected_scene_count": len(SCENES),
            "floorplan_relpath": "floorplan.webp",
            "initial_scene_id": "hall",
            "representation_disclosure": DISCLOSURE,
            "representation_kind": "ai_reconstruction",
            "scenes": [_scene_payload(*scene) for scene in SCENES],
            "spatial_model": {
                "measured": False,
                "rooms": [
                    {
                        "depth": depth,
                        "height": 2.7,
                        "id": room_id,
                        "kind": kind,
                        "label": label,
                        "scene_id": scene_id,
                        "width": width,
                        "x": x,
                        "z": z,
                    }
                    for room_id, label, scene_id, kind, x, z, width, depth in SPATIAL_ROOMS
                ],
                "source_basis": "floorplan_scaled_approximation",
            },
        },
    }
    _write_json(bundle / "tour.json", tour)
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--floorplan", type=Path, required=True)
    parser.add_argument("--diorama", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    bundle = build(args)
    print(
        json.dumps(
            {
                "status": "built",
                "slug": SLUG,
                "bundle": str(bundle),
                "scene_count": len(SCENES),
                "property_url": PROPERTY_URL,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
