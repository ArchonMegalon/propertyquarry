#!/usr/bin/env python3
"""Build the sealed-source AI-panorama bundle for the Karl-Czerny showcase."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ea.app.product.property_diorama_preview import render_bright_apartment_diorama

try:
    from scripts.propertyquarry_floorplan_analyzer import (
        ANALYZER_CONTRACT,
        FloorplanAnalysisError,
        analyze_floorplan,
        compare_constructed_floorplan,
        render_derived_floorplan,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from propertyquarry_floorplan_analyzer import (  # type: ignore[no-redef]
        ANALYZER_CONTRACT,
        FloorplanAnalysisError,
        analyze_floorplan,
        compare_constructed_floorplan,
        render_derived_floorplan,
    )


SLUG = "karl-czerny-gasse-2-urban-jungle"
ANALYSIS_SPEC_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "propertyquarry"
    / "karl-czerny-gasse-2-floorplan-analysis.v2.json"
)
PROPERTY_URL = (
    "https://propertyquarry.com/app/research/"
    "karl-czerny-gasse-2-private-showcase"
)
# Google-hosted street context for the terrace.  This is deliberately a
# navigation link rather than copied Street View imagery: the public tour does
# not claim to have ingested or licensed Google's pixels.
TERRACE_STREET_VIEW_URL = (
    "https://www.google.com/maps/@?api=1&map_action=pano"
    "&viewpoint=48.2337189,16.3637295&pitch=0&fov=80"
)
DISCLOSURE = (
    "AI-reconstructed from a reviewed architectural floorplan analysis with "
    "source-linked room dimensions and a derived-plan round-trip check; not a "
    "captured 360 or measured survey."
)
DIORAMA_PALETTE = {
    "accent": (79, 126, 103),
    "floorplan_wash": (235, 229, 216),
    "wash": (239, 234, 224),
}
SCENES = (
    ("hall", "Entrance vestibule · 18.80 m²", 52.0, 81.5),
    ("bedroom-primary", "Bedroom · 16.86 m²", 36.5, 60.0),
    ("terrace", "Terrace · 7.78 m²", 24.5, 59.0),
    ("bedroom-guest", "Bedroom · 15.24 m²", 78.0, 42.0),
    ("wc", "Separate WC · 1.36 m²", 47.5, 43.0),
    ("bath", "Bathroom · 4.96 m²", 56.5, 42.0),
    ("living-kitchen", "Wohnküche · 30.33 m²", 53.0, 68.0),
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
# The reviewed JSON is the only geometry source.  These compatibility views
# are derived from it for tests and the floorplan overlay; the builder never
# maintains a second set of coordinates or silently reshapes an exterior room.
_ANALYSIS_SPEC = json.loads(ANALYSIS_SPEC_PATH.read_text(encoding="utf-8"))
_ANALYSIS_ROOMS = tuple(
    room for room in list(_ANALYSIS_SPEC.get("rooms") or []) if isinstance(room, dict)
)
SPATIAL_ROOMS = tuple(
    (
        str(room["id"]),
        str(room["label"]),
        str(room.get("scene_id") or ""),
        str(room["kind"]),
        float(dict(room["floorplan_bounds_pct"])["x"]),
        float(dict(room["floorplan_bounds_pct"])["y"]),
        float(dict(room["floorplan_bounds_pct"])["width"]),
        float(dict(room["floorplan_bounds_pct"])["height"]),
    )
    for room in _ANALYSIS_ROOMS
)
DOORWAY_EDGES = tuple(
    tuple(edge)
    for edge in list(_ANALYSIS_SPEC.get("doorway_edges") or [])
    if isinstance(edge, (list, tuple)) and len(edge) == 2
)
MEASURED_ROOM_GEOMETRY = {}
for _room in _ANALYSIS_ROOMS:
    _components = tuple(dict(component) for component in list(_room["components"]))
    _min_x = min(float(component["x"]) for component in _components)
    _min_z = min(float(component["z"]) for component in _components)
    _max_x = max(float(component["x"]) + float(component["width"]) for component in _components)
    _max_z = max(float(component["z"]) + float(component["depth"]) for component in _components)
    MEASURED_ROOM_GEOMETRY[str(_room["id"])] = {
        "x": _min_x,
        "z": _min_z,
        "width": _max_x - _min_x,
        "depth": _max_z - _min_z,
        "area_m2": float(_room["area_m2"]),
        "dimension_label": str(_room["dimension_label"]),
        "shape": str(_room.get("shape") or "rectangle"),
        "components": _components,
    }
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


def _showcase_portal_topology() -> dict[str, object]:
    source_geometry = dict(_ANALYSIS_SPEC.get("source_geometry") or {})
    portals = {
        str(portal.get("id") or ""): dict(portal)
        for portal in list(source_geometry.get("portals") or [])
        if isinstance(portal, dict)
    }
    exit_gate = portals.get("entrance-exit-gate")
    balcony_door = portals.get("living-to-balcony-loggia")
    if exit_gate is None or balcony_door is None:
        raise RuntimeError("showcase_required_portal_missing")
    if (
        str(exit_gate.get("kind") or "") != "exit_gate"
        or list(exit_gate.get("room_ids") or [])
        != ["entrance-vestibule", "outside"]
        or str(exit_gate.get("target_room_id") or "") != "outside"
    ):
        raise RuntimeError("showcase_stairwell_exit_topology_invalid")
    if (
        str(balcony_door.get("kind") or "") != "door"
        or set(balcony_door.get("room_ids") or ())
        != {"living-kitchen", "balcony-loggia"}
        or str(balcony_door.get("target_room_id") or "") != "balcony-loggia"
    ):
        raise RuntimeError("showcase_balcony_door_topology_invalid")
    exit_center = dict(exit_gate.get("center_px") or {})
    balcony_center = dict(balcony_door.get("center_px") or {})
    separation_px = abs(int(balcony_center.get("x") or 0) - int(exit_center.get("x") or 0))
    canvas_width = int(dict(source_geometry.get("canvas_size_px") or {}).get("width") or 0)
    if canvas_width <= 0 or separation_px < round(canvas_width * 0.25):
        raise RuntimeError("showcase_stairwell_balcony_separation_invalid")
    forbidden_pairs = {
        frozenset(str(room_id) for room_id in pair)
        for pair in list(dict(_ANALYSIS_SPEC.get("boundary_adjacency") or {}).get("forbidden") or [])
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    }
    if frozenset(("entrance-vestibule", "balcony-loggia")) not in forbidden_pairs:
        raise RuntimeError("showcase_stairwell_balcony_adjacency_not_forbidden")
    return {
        "balcony_portal_id": "living-to-balcony-loggia",
        "exit_portal_id": "entrance-exit-gate",
        "source_pixel_separation": separation_px,
        "status": "pass",
    }


def _save_source_locked_diorama(
    *,
    floorplan_source: Path,
    floorplan_analysis: dict[str, object],
    source_crop_target: Path,
    target: Path,
) -> dict[str, object]:
    source_geometry = dict(floorplan_analysis.get("source_geometry") or {})
    source_rooms = [
        dict(room)
        for room in list(source_geometry.get("rooms") or [])
        if isinstance(room, dict)
    ]
    if not source_rooms:
        raise RuntimeError("showcase_diorama_source_geometry_missing")
    component_rows = [
        dict(component)
        for room in source_rooms
        for component in list(room.get("components_px") or [])
        if isinstance(component, dict)
    ]
    if not component_rows:
        raise RuntimeError("showcase_diorama_source_components_missing")
    with Image.open(floorplan_source) as opened:
        source_width, source_height = opened.size
    padding = max(56, round(min(source_width, source_height) * 0.055))
    crop_left = max(0, min(int(row["x"]) for row in component_rows) - padding)
    crop_top = max(0, min(int(row["y"]) for row in component_rows) - padding)
    crop_right = min(
        source_width,
        max(int(row["x"]) + int(row["width"]) for row in component_rows) + padding,
    )
    crop_bottom = min(
        source_height,
        max(int(row["y"]) + int(row["height"]) for row in component_rows) + padding,
    )
    crop_width = max(1, crop_right - crop_left)
    crop_height = max(1, crop_bottom - crop_top)
    analysis_rooms = {
        str(room.get("id") or ""): dict(room)
        for room in list(floorplan_analysis.get("rooms") or [])
        if isinstance(room, dict)
    }
    clean_plan = Image.new("RGB", (crop_width, crop_height), (248, 246, 240))
    plan_draw = ImageDraw.Draw(clean_plan)
    wall_color = (69, 74, 70)
    room_fills = (
        (231, 223, 207),
        (219, 229, 221),
        (226, 222, 232),
        (232, 224, 218),
    )
    exterior_fill = (196, 216, 199)
    labels: list[tuple[str, tuple[int, int]]] = []
    for room_index, source_room in enumerate(source_rooms):
        room_id = str(source_room.get("id") or "")
        room = analysis_rooms.get(room_id, {})
        fill = (
            exterior_fill
            if str(room.get("kind") or "") == "exterior" or room_id == "balcony-loggia"
            else room_fills[room_index % len(room_fills)]
        )
        components = [
            dict(component)
            for component in list(source_room.get("components_px") or [])
            if isinstance(component, dict)
        ]
        for component in components:
            left = int(component["x"]) - crop_left
            top = int(component["y"]) - crop_top
            right = left + int(component["width"])
            bottom = top + int(component["height"])
            plan_draw.rectangle(
                (left, top, right, bottom),
                fill=fill,
                outline=wall_color,
                width=9,
            )
        left = min(int(component["x"]) for component in components) - crop_left
        top = min(int(component["y"]) for component in components) - crop_top
        right = max(int(component["x"]) + int(component["width"]) for component in components) - crop_left
        bottom = max(int(component["y"]) + int(component["height"]) for component in components) - crop_top
        labels.append(
            (
                str(room.get("label") or room_id).split(" · ", 1)[0],
                (round((left + right) / 2), round((top + bottom) / 2)),
            )
        )

    source_portals = [
        dict(portal)
        for portal in list(source_geometry.get("portals") or [])
        if isinstance(portal, dict)
    ]
    exit_gate = next(
        portal
        for portal in source_portals
        if str(portal.get("id") or "") == "entrance-exit-gate"
    )
    exit_center = dict(exit_gate.get("center_px") or {})
    exit_x = int(exit_center["x"]) - crop_left
    exit_y = int(exit_center["y"]) - crop_top
    stair_box = (
        max(6, exit_x - 190),
        max(6, exit_y - 104),
        max(24, exit_x - 16),
        min(crop_height - 6, exit_y + 104),
    )
    plan_draw.rectangle(stair_box, fill=(213, 214, 210), outline=wall_color, width=9)
    for step_index in range(1, 8):
        step_y = round(
            stair_box[1]
            + ((stair_box[3] - stair_box[1]) * step_index / 8)
        )
        plan_draw.line(
            (stair_box[0] + 10, step_y, stair_box[2] - 10, step_y),
            fill=(128, 132, 128),
            width=4,
        )
    labels.append(("Stiegenhaus 3", (round((stair_box[0] + stair_box[2]) / 2), stair_box[1] + 20)))

    for portal in source_portals:
        center = dict(portal.get("center_px") or {})
        center_x = int(center["x"]) - crop_left
        center_y = int(center["y"]) - crop_top
        width_px = max(24, int(portal.get("width_px") or 0))
        sides = dict(portal.get("room_sides") or {})
        side = next(iter(sides.values()), "north")
        if side in {"north", "south"}:
            segment = (
                center_x - (width_px // 2),
                center_y,
                center_x + (width_px // 2),
                center_y,
            )
        else:
            segment = (
                center_x,
                center_y - (width_px // 2),
                center_x,
                center_y + (width_px // 2),
            )
        plan_draw.line(segment, fill=(248, 246, 240), width=15)
        plan_draw.line(segment, fill=(167, 112, 61), width=5)

    try:
        label_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 19)
    except OSError:
        label_font = ImageFont.load_default()
    for label, center in labels:
        text_box = plan_draw.textbbox((0, 0), label, font=label_font)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        plan_draw.rounded_rectangle(
            (
                center[0] - (text_width // 2) - 5,
                center[1] - (text_height // 2) - 4,
                center[0] + (text_width // 2) + 5,
                center[1] + (text_height // 2) + 4,
            ),
            radius=5,
            fill=(255, 253, 247),
        )
        plan_draw.text(
            (center[0] - (text_width / 2), center[1] - (text_height / 2) - text_box[1]),
            label,
            font=label_font,
            fill=(49, 55, 51),
        )
    source_crop_target.parent.mkdir(parents=True, exist_ok=True)
    clean_plan.save(source_crop_target, format="PNG", optimize=True)
    route = []
    for source_room in source_rooms:
        room_id = str(source_room.get("id") or "")
        components = [
            dict(component)
            for component in list(source_room.get("components_px") or [])
            if isinstance(component, dict)
        ]
        left = min(int(component["x"]) for component in components)
        top = min(int(component["y"]) for component in components)
        right = max(int(component["x"]) + int(component["width"]) for component in components)
        bottom = max(int(component["y"]) + int(component["height"]) for component in components)
        room = analysis_rooms.get(room_id, {})
        route.append(
            {
                "focus": {
                    "x": round(((left + right) / 2) - crop_left - (crop_width / 2), 4),
                    "z": round(((top + bottom) / 2) - crop_top - (crop_height / 2), 4),
                },
                "kind": "outdoor" if str(room.get("kind") or "") == "exterior" else "",
                "label": str(room.get("label") or room_id),
                "source_room_id": room_id,
            }
        )
    rendered = render_bright_apartment_diorama(
        floorplan_path=source_crop_target,
        walkable_scene={
            "bounds": {"depth_m": float(crop_height), "width_m": float(crop_width)},
            "route": route,
        },
        palette=DIORAMA_PALETTE,
        source_photo_count=0,
    )
    if rendered is None:
        raise RuntimeError("showcase_diorama_renderer_unavailable")
    image, metadata = rendered
    checks = dict(metadata.get("checks") or {})
    if not checks or not all(value is True for value in checks.values()):
        raise RuntimeError("showcase_diorama_layout_gate_failed")
    if int(metadata.get("displayed_route_stop_count") or 0) != len(source_rooms):
        raise RuntimeError("showcase_diorama_room_coverage_failed")
    image.save(target, format="PNG", optimize=True)
    return {
        **metadata,
        "preview_sha256": _sha256(target),
        "source_projection_relpath": "proof/diorama-source-floorplan.png",
        "source_projection_sha256": _sha256(source_crop_target),
        "source_projection_kind": "source_pixel_geometry_with_verified_portals",
        "source_geometry_contract_name": str(source_geometry.get("contract_name") or ""),
        "status": "pass",
    }


def _scene_payload(
    scene_id: str,
    label: str,
    floorplan_x: float,
    floorplan_y: float,
) -> dict[str, object]:
    payload = {
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
    if scene_id == "terrace":
        payload["street_view_url"] = TERRACE_STREET_VIEW_URL
    return payload


def build(args: argparse.Namespace) -> Path:
    input_dir = args.input_dir.expanduser().resolve()
    floorplan_source = args.floorplan.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    bundle = output_root / SLUG
    if bundle.exists():
        raise RuntimeError("target_bundle_exists")
    for path in (
        floorplan_source,
        *(input_dir / name for name in SCENE_INPUT_NAMES.values()),
    ):
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"required_input_missing:{path.name}")

    (bundle / "panoramas").mkdir(parents=True)
    (bundle / "proof").mkdir()
    try:
        analysis_spec = json.loads(ANALYSIS_SPEC_PATH.read_text(encoding="utf-8"))
        floorplan_analysis = analyze_floorplan(
            floorplan_source,
            specification=analysis_spec,
            output_dir=bundle / "proof" / "floorplan-analysis",
        )
    except (OSError, json.JSONDecodeError, FloorplanAnalysisError) as exc:
        raise RuntimeError(f"floorplan_analysis_failed:{exc}") from exc
    _save_floorplan(floorplan_source, bundle / "floorplan.webp")
    _save_floorplan_fidelity_overlay(
        bundle / "floorplan.webp",
        bundle / "proof" / "floorplan-fidelity-overlay.png",
    )
    portal_topology = _showcase_portal_topology()
    diorama_layout = _save_source_locked_diorama(
        floorplan_source=floorplan_source,
        floorplan_analysis=floorplan_analysis,
        source_crop_target=bundle / "proof" / "diorama-source-floorplan.png",
        target=bundle / "diorama-preview.png",
    )
    derived_floorplan = render_derived_floorplan(
        floorplan_analysis,
        bundle / "derived-floorplan.png",
        source_size=Image.open(bundle / "floorplan.webp").size,
    )
    with Image.open(bundle / "floorplan.webp") as source_plan, Image.open(bundle / "derived-floorplan.png") as derived_plan:
        roundtrip_overlay_path = bundle / "proof" / "floorplan-roundtrip-overlay.png"
        Image.blend(source_plan.convert("RGB"), derived_plan.convert("RGB"), 0.5).save(
            roundtrip_overlay_path,
            format="PNG",
            optimize=True,
        )
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
    spatial_rooms = []
    for room in list(floorplan_analysis.get("rooms") or []):
        components = [dict(component) for component in list(room.get("components") or [])]
        min_x = min(float(component["x"]) for component in components)
        min_z = min(float(component["z"]) for component in components)
        max_x = max(float(component["x"]) + float(component["width"]) for component in components)
        max_z = max(float(component["z"]) + float(component["depth"]) for component in components)
        bounds = dict(room.get("floorplan_bounds_pct") or {})
        spatial_rooms.append(
            {
                "depth": round(max_z - min_z, 6),
                "floorplan_bounds_pct": bounds,
                **(
                    {"source_bbox_px": dict(room["source_bbox_px"])}
                    if isinstance(room.get("source_bbox_px"), dict)
                    else {}
                ),
                "height": 2.7,
                "id": str(room["id"]),
                "kind": str(room["kind"]),
                "label": str(room["label"]),
                "shape": str(room.get("shape") or "rectangle"),
                "measurement": {
                    "contract_name": "propertyquarry.floorplan_measurement.v1",
                    "source": "floorplan_analyzer_reviewed_dimension_evidence",
                    "area_m2": float(room["area_m2"]),
                    "components": components,
                    "dimension_evidence": list(room.get("dimension_evidence") or []),
                    "confidence": float(room.get("confidence") or 0.0),
                },
                **({"scene_id": str(room["scene_id"])} if str(room.get("scene_id") or "") else {}),
                "dimension_label": str(room["dimension_label"]),
                "width": round(max_x - min_x, 6),
                "x": round(min_x, 6),
                "z": round(min_z, 6),
            }
        )
    round_trip = compare_constructed_floorplan(
        floorplan_analysis,
        derived=bundle / "derived-floorplan.png",
        source=floorplan_source,
        geometry={"rooms": spatial_rooms},
        tolerance_m=float(floorplan_analysis.get("measurement_tolerance_m") or 0.05),
    )
    analysis_artifact = dict(floorplan_analysis)
    analysis_artifact["derived_floorplan"] = derived_floorplan
    analysis_artifact["round_trip_overlay"] = {
        "relpath": "proof/floorplan-roundtrip-overlay.png",
        "sha256": _sha256(bundle / "proof" / "floorplan-roundtrip-overlay.png"),
    }
    analysis_artifact["round_trip"] = round_trip
    (bundle / "proof" / "floorplan-analysis.json").write_text(
        json.dumps(analysis_artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    layout_fidelity = {
        "contract_name": "propertyquarry.floorplan_spatial_fidelity.v1",
        "boundary_adjacency": dict(floorplan_analysis.get("boundary_adjacency") or {}),
        "doorway_edges": [list(edge) for edge in list(floorplan_analysis.get("doorway_edges") or [])],
        "diorama_layout": diorama_layout,
        "floorplan_sha256": floorplan_sha256,
        "source_floorplan_sha256": str(dict(floorplan_analysis.get("source") or {}).get("sha256") or ""),
        "analyzer_contract_name": ANALYZER_CONTRACT,
        "analyzer_sha256": str(floorplan_analysis.get("analysis_sha256") or ""),
        "measurement_contract_name": "propertyquarry.floorplan_measurement.v1",
        "measurement_source": "floorplan_analyzer_reviewed_dimension_evidence",
        "measurement_tolerance_m": float(floorplan_analysis.get("measurement_tolerance_m") or 0.05),
        "round_trip_contract_name": str(round_trip.get("contract_name") or ""),
        "round_trip_receipt_sha256": str(round_trip.get("receipt_sha256") or ""),
        "derived_floorplan_relpath": "derived-floorplan.png",
        "derived_floorplan_sha256": str(derived_floorplan.get("sha256") or ""),
        "round_trip_overlay_relpath": "proof/floorplan-roundtrip-overlay.png",
        "round_trip_overlay_sha256": _sha256(bundle / "proof" / "floorplan-roundtrip-overlay.png"),
        "overlay_relpath": "proof/floorplan-fidelity-overlay.png",
        "overlay_sha256": _sha256(bundle / "proof" / "floorplan-fidelity-overlay.png"),
        "review_method": "floorplan_analyzer_round_trip",
        "review_status": "pass",
        "portal_topology": portal_topology,
        "source_room_count": len(spatial_rooms),
        "source_geometry_contract_name": str(
            dict(floorplan_analysis.get("source_geometry") or {}).get("contract_name") or ""
        ),
        "source_geometry": dict(floorplan_analysis.get("source_geometry") or {}),
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
        "floorplan_analysis_sha256": str(floorplan_analysis.get("analysis_sha256") or ""),
        "floorplan_round_trip_sha256": str(round_trip.get("receipt_sha256") or ""),
        "derived_floorplan_sha256": str(derived_floorplan.get("sha256") or ""),
        "diorama_preview_sha256": _sha256(bundle / "diorama-preview.png"),
        "diorama_renderer_version": str(diorama_layout.get("renderer_version") or ""),
        "spatial_model_basis": "floorplan_analyzer_reviewed_dimensions",
        "spatial_model_measured": True,
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
                "separate WC, bathroom, terrace, and the balcony/loggia as exterior rooms"
            ),
            "unsupported_rooms_omitted": False,
            "panorama_unavailable_space_ids": ["circulation-hall", "balcony-loggia"],
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
        "derived_floorplan_relpath": "derived-floorplan.png",
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
            "derived_floorplan_relpath": "derived-floorplan.png",
            "initial_scene_id": "hall",
            "representation_disclosure": DISCLOSURE,
            "representation_kind": "ai_reconstruction",
            "scenes": [_scene_payload(*scene) for scene in SCENES],
            "spatial_model": {
                "layout_fidelity": layout_fidelity,
                "analyzer_contract_name": ANALYZER_CONTRACT,
                "measured": True,
                "rooms": spatial_rooms,
                "source_geometry": dict(floorplan_analysis.get("source_geometry") or {}),
                "source_basis": "floorplan_analyzer_reviewed_dimensions",
            },
        },
    }
    _write_json(bundle / "tour.json", tour)
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--floorplan", type=Path, required=True)
    parser.add_argument(
        "--diorama",
        type=Path,
        help="Deprecated compatibility input; the preview is rendered from the reviewed floorplan.",
    )
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
