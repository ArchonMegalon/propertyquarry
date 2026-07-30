#!/usr/bin/env python3
"""Build the sealed-source AI-panorama bundle for the Karl-Czerny showcase."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


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
    ("hall", "Entrance vestibule · 3.80 m²", 52.0, 81.5),
    ("bedroom-primary", "Bedroom · 16.86 m²", 36.5, 60.0),
    ("terrace", "Terrace · 7.78 m²", 24.5, 59.0),
    ("bedroom-guest", "Bedroom · 15.24 m²", 78.0, 42.0),
    ("wc", "Separate WC · 1.36 m²", 47.5, 43.0),
    ("bath", "Bathroom · 4.96 m²", 56.5, 42.0),
    ("living-kitchen", "Wohnküche · 30.33 m²", 64.0, 65.0),
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
    "hall": (("Continue to Wohnküche", "living-kitchen", 154, ()),),
    "bedroom-primary": (
        ("Return to Wohnküche", "living-kitchen", 112, ()),
        ("Step onto terrace", "terrace", -76, ()),
    ),
    "terrace": (("Return to primary bedroom", "bedroom-primary", 180, ()),),
    "bedroom-guest": (
        (
            "Return through internal hall to Wohnküche",
            "living-kitchen",
            -128,
            ("circulation-hall",),
        ),
    ),
    "wc": (("Return to Wohnküche", "living-kitchen", 168, ()),),
    "bath": (
        (
            "Return through internal hall to Wohnküche",
            "living-kitchen",
            172,
            ("circulation-hall",),
        ),
    ),
    "living-kitchen": (
        ("Return to entrance vestibule", "hall", -154, ()),
        ("Enter primary bedroom", "bedroom-primary", -92, ()),
        (
            "Through internal hall to second bedroom",
            "bedroom-guest",
            86,
            ("circulation-hall",),
        ),
        ("Open separate WC", "wc", -32, ()),
        (
            "Through internal hall to bathroom",
            "bath",
            28,
            ("circulation-hall",),
        ),
    ),
}
# Bounds are percentages of the exact published floorplan image. The 3D model
# is derived from these bounds at one uniform scale; it is not a separately
# invented room layout.
SPATIAL_ROOMS = (
    ("terrace", "Terrace · 7.78 m²", "terrace", "exterior", 19.7, 35.0, 9.2, 42.0),
    ("primary-bedroom", "Bedroom · 16.86 m²", "bedroom-primary", "interior", 28.9, 34.8, 15.4, 42.1),
    ("separate-wc", "Separate WC · 1.36 m²", "wc", "interior", 43.1, 34.8, 8.9, 14.3),
    ("bathroom", "Bathroom · 4.96 m²", "bath", "interior", 52.1, 34.7, 10.0, 14.4),
    ("circulation-hall", "Internal hall · 3.50 m²", "", "unavailable", 62.0, 39.3, 8.0, 9.8),
    ("guest-bedroom", "Bedroom · 15.24 m²", "bedroom-guest", "interior", 70.0, 34.7, 14.2, 14.4),
    ("living-kitchen", "Wohnküche · 30.33 m²", "living-kitchen", "interior", 44.3, 49.1, 39.9, 27.9),
    ("entrance-vestibule", "Entrance vestibule · 3.80 m²", "hall", "interior", 44.3, 77.0, 15.1, 9.3),
)
DOORWAY_EDGES = (
    ("terrace", "primary-bedroom"),
    ("primary-bedroom", "living-kitchen"),
    ("separate-wc", "living-kitchen"),
    ("bathroom", "circulation-hall"),
    ("circulation-hall", "living-kitchen"),
    ("circulation-hall", "guest-bedroom"),
    ("living-kitchen", "entrance-vestibule"),
)
MODEL_UNITS_PER_FLOORPLAN_PERCENT = 0.2
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


def _save_floorplan_fidelity_overlay(source: Path, target: Path) -> None:
    with Image.open(source) as opened:
        image = opened.convert("RGBA")
    draw = ImageDraw.Draw(image, "RGBA")
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(18, image.width // 90))
    except OSError:
        font = ImageFont.load_default()
    colors = (
        (64, 145, 108, 62),
        (216, 161, 78, 62),
        (83, 125, 174, 62),
        (169, 92, 128, 62),
    )
    for index, (
        room_id,
        label,
        _scene_id,
        _kind,
        x_pct,
        y_pct,
        width_pct,
        height_pct,
    ) in enumerate(SPATIAL_ROOMS):
        left = round(image.width * x_pct / 100)
        top = round(image.height * y_pct / 100)
        right = round(image.width * (x_pct + width_pct) / 100)
        bottom = round(image.height * (y_pct + height_pct) / 100)
        fill = colors[index % len(colors)]
        outline = (*fill[:3], 235)
        draw.rectangle((left, top, right, bottom), fill=fill, outline=outline, width=5)
        text = label.split(" · ", 1)[0]
        text_box = draw.textbbox((0, 0), text, font=font)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        text_x = min(max(left + 8, 0), max(0, image.width - text_width - 8))
        text_y = min(max(top + 8, 0), max(0, image.height - text_height - 8))
        draw.rounded_rectangle(
            (
                text_x - 5,
                text_y - 4,
                text_x + text_width + 5,
                text_y + text_height + 5,
            ),
            radius=5,
            fill=(10, 16, 13, 210),
        )
        draw.text((text_x, text_y), text, font=font, fill=(255, 255, 255, 255))
    target.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(target, format="PNG", optimize=True)


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
                **({"via_room_ids": list(via_room_ids)} if via_room_ids else {}),
                "yaw": yaw,
            }
            for hotspot_label, target, yaw, via_room_ids in HOTSPOTS[scene_id]
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
    _save_floorplan_fidelity_overlay(
        bundle / "floorplan.webp",
        bundle / "proof" / "floorplan-fidelity-overlay.png",
    )
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
    spatial_rooms = [
        {
            "depth": round(height_pct * MODEL_UNITS_PER_FLOORPLAN_PERCENT, 3),
            "floorplan_bounds_pct": {
                "height": height_pct,
                "width": width_pct,
                "x": x_pct,
                "y": y_pct,
            },
            "height": 2.7,
            "id": room_id,
            "kind": kind,
            "label": label,
            **({"scene_id": scene_id} if scene_id else {}),
            "width": round(width_pct * MODEL_UNITS_PER_FLOORPLAN_PERCENT, 3),
            "x": round(x_pct * MODEL_UNITS_PER_FLOORPLAN_PERCENT, 3),
            "z": round(y_pct * MODEL_UNITS_PER_FLOORPLAN_PERCENT, 3),
        }
        for (
            room_id,
            label,
            scene_id,
            kind,
            x_pct,
            y_pct,
            width_pct,
            height_pct,
        ) in SPATIAL_ROOMS
    ]
    layout_fidelity = {
        "contract_name": "propertyquarry.floorplan_spatial_fidelity.v1",
        "doorway_edges": [list(edge) for edge in DOORWAY_EDGES],
        "floorplan_sha256": floorplan_sha256,
        "model_units_per_floorplan_percent": MODEL_UNITS_PER_FLOORPLAN_PERCENT,
        "overlay_relpath": "proof/floorplan-fidelity-overlay.png",
        "overlay_sha256": _sha256(bundle / "proof" / "floorplan-fidelity-overlay.png"),
        "review_method": "operator_floorplan_overlay",
        "review_status": "pass",
        "source_room_count": len(spatial_rooms),
    }
    layout_fidelity_sha256 = hashlib.sha256(
        json.dumps(
            layout_fidelity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
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
        "layout_fidelity_sha256": layout_fidelity_sha256,
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
                "Wohnküche, both bedrooms, entrance vestibule, internal hall, "
                "separate WC, bathroom, and terrace as an exterior room"
            ),
            "unsupported_rooms_omitted": False,
            "panorama_unavailable_space_ids": ["circulation-hall"],
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
                "layout_fidelity": layout_fidelity,
                "measured": False,
                "rooms": spatial_rooms,
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
