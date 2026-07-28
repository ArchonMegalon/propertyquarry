from __future__ import annotations

import math
from pathlib import Path
from typing import Any

try:
    from PIL import (
        Image,
        ImageChops,
        ImageDraw,
        ImageEnhance,
        ImageFilter,
        ImageFont,
        ImageOps,
        ImageStat,
    )
except Exception:  # pragma: no cover - Pillow is optional in the web fallback image
    Image = None
    ImageChops = None
    ImageDraw = None
    ImageEnhance = None
    ImageFilter = None
    ImageFont = None
    ImageOps = None
    ImageStat = None


_FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
_BOLD_FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
DIORAMA_PREVIEW_RENDERER_VERSION = "propertyquarry_bright_playful_cutaway_v1"


def _mix_color(
    first: tuple[int, int, int],
    second: tuple[int, int, int],
    second_weight: float,
) -> tuple[int, int, int]:
    weight = max(0.0, min(1.0, float(second_weight)))
    return tuple(
        max(0, min(255, int(round((first[index] * (1.0 - weight)) + (second[index] * weight)))))
        for index in range(3)
    )


def _font(size: int, *, bold: bool = False):
    if ImageFont is None:
        return None
    path = _BOLD_FONT_PATH if bold else _FONT_PATH
    try:
        return ImageFont.truetype(str(path), max(10, int(size)))
    except Exception:
        return ImageFont.load_default()


def _room_kind(stop: dict[str, object]) -> str:
    explicit = str(stop.get("kind") or "").strip().lower()
    if explicit:
        return explicit
    label = str(stop.get("label") or stop.get("room") or stop.get("name") or "").strip().lower()
    if any(token in label for token in ("entry", "hall", "foyer", "vorraum", "flur")):
        return "entry"
    if any(token in label for token in ("stair", "treppe", "stiege")):
        return "stairs"
    if any(token in label for token in ("bath", "bad", "badezimmer")):
        return "bath"
    if any(token in label for token in ("toilet", "wc")):
        return "toilet"
    if any(token in label for token in ("storage", "abstell")):
        return "storage"
    if any(token in label for token in ("balcony", "terrace", "balkon", "terrasse", "loggia")):
        return "outdoor"
    if any(token in label for token in ("kitchen", "kuche", "küche", "wohnkuche", "wohnküche")):
        return "kitchen"
    if any(token in label for token in ("dining", "esszimmer")):
        return "dining"
    if any(token in label for token in ("bedroom", "sleep", "schlaf")):
        return "bedroom"
    if "living" in label or "wohn" in label:
        return "living"
    return "generic"


def _route_position(
    stop: dict[str, object],
    *,
    index: int,
    count: int,
    width_m: float,
    depth_m: float,
) -> tuple[float, float]:
    focus = dict(stop.get("focus") or {}) if isinstance(stop.get("focus"), dict) else {}
    if focus:
        x_ratio = (float(focus.get("x") or 0.0) / max(0.001, width_m)) + 0.5
        y_ratio = (float(focus.get("z") or 0.0) / max(0.001, depth_m)) + 0.5
        return (
            max(0.1, min(0.9, x_ratio)),
            max(0.12, min(0.88, y_ratio)),
        )
    columns = max(2, int(math.ceil(math.sqrt(max(1, count) * 1.35))))
    rows = max(1, int(math.ceil(max(1, count) / columns)))
    column = index % columns
    row = index // columns
    return (
        0.16 + (0.68 * (column + 0.5) / columns),
        0.17 + (0.66 * (row + 0.5) / rows),
    )


def _spread_route_positions(
    route_stops: list[dict[str, object]],
    *,
    width_m: float,
    depth_m: float,
) -> list[tuple[float, float]]:
    raw_positions = [
        _route_position(
            stop,
            index=index,
            count=len(route_stops),
            width_m=width_m,
            depth_m=depth_m,
        )
        for index, stop in enumerate(route_stops)
    ]
    if len(raw_positions) < 2:
        return raw_positions
    x_span = max(position[0] for position in raw_positions) - min(position[0] for position in raw_positions)
    y_span = max(position[1] for position in raw_positions) - min(position[1] for position in raw_positions)
    largest_span = max(x_span, y_span)
    spread = min(2.6, max(1.0, 0.58 / max(0.01, largest_span)))
    expanded = [
        (
            max(0.1, min(0.9, 0.5 + ((position[0] - 0.5) * spread))),
            max(0.12, min(0.88, 0.5 + ((position[1] - 0.5) * spread))),
        )
        for position in raw_positions
    ]
    semantic_fallbacks: dict[str, tuple[float, float]] = {
        "entry": (0.18, 0.7),
        "stairs": (0.42, 0.46),
        "bath": (0.2, 0.24),
        "toilet": (0.34, 0.22),
        "storage": (0.42, 0.2),
        "kitchen": (0.7, 0.25),
        "living": (0.62, 0.48),
        "dining": (0.82, 0.48),
        "bedroom": (0.68, 0.75),
        "outdoor": (0.84, 0.78),
        "generic": (0.42, 0.72),
    }
    grid_fallbacks = [
        (0.18, 0.22),
        (0.4, 0.22),
        (0.63, 0.22),
        (0.84, 0.25),
        (0.18, 0.49),
        (0.41, 0.49),
        (0.64, 0.49),
        (0.84, 0.51),
        (0.2, 0.76),
        (0.43, 0.76),
        (0.66, 0.76),
        (0.84, 0.78),
    ]
    resolved: list[tuple[float, float]] = []
    for stop, desired in zip(route_stops, expanded):
        candidates = [desired, semantic_fallbacks.get(_room_kind(stop), desired)]
        candidates.extend(
            sorted(
                grid_fallbacks,
                key=lambda candidate: math.dist(candidate, desired),
            )
        )
        chosen = desired
        for candidate in candidates:
            if all(math.dist(candidate, existing) >= 0.135 for existing in resolved):
                chosen = candidate
                break
        resolved.append(chosen)
    return resolved


def _draw_furniture(
    draw,
    *,
    center: tuple[int, int],
    kind: str,
    scale: float,
    accent: tuple[int, int, int],
    marker_number: int,
) -> None:
    x, y = center
    unit = max(8, int(round(18 * scale)))
    playful_pastels = (
        (232, 190, 181),
        (180, 211, 200),
        (190, 205, 230),
        (236, 211, 164),
        (211, 193, 224),
    )
    playful = playful_pastels[(max(1, marker_number) - 1) % len(playful_pastels)]
    wood = _mix_color(accent, (145, 99, 60), 0.58)
    upholstery = _mix_color(playful, accent, 0.08)
    stone = (218, 222, 218)
    outline = (91, 78, 63)
    green = (107, 139, 102)
    linen = _mix_color((249, 245, 235), playful, 0.13)
    shadow = (64, 53, 42, 42)

    rug_fill = (*_mix_color(playful, (255, 250, 239), 0.48), 148)
    draw.rounded_rectangle(
        (
            x - int(unit * 3.25),
            y - int(unit * 2.5),
            x + int(unit * 3.25),
            y + int(unit * 3.15),
        ),
        radius=max(8, int(unit * 0.8)),
        fill=rug_fill,
        outline=(*_mix_color(playful, (150, 127, 100), 0.32), 90),
        width=max(1, unit // 8),
    )
    draw.ellipse(
        (x - (unit * 3), y + unit, x + (unit * 3), y + (unit * 3)),
        fill=shadow,
    )
    if kind == "bedroom":
        draw.rounded_rectangle(
            (x - (unit * 2), y - (unit * 2), x + (unit * 2), y + (unit * 3)),
            radius=max(3, unit // 3),
            fill=linen,
            outline=outline,
            width=max(2, unit // 5),
        )
        draw.rectangle(
            (x - (unit * 2), y - (unit * 2), x + (unit * 2), y - unit),
            fill=wood,
            outline=outline,
            width=max(1, unit // 6),
        )
        draw.rounded_rectangle(
            (x - int(unit * 1.55), y - int(unit * 0.85), x - 2, y),
            radius=max(2, unit // 4),
            fill=(255, 253, 246),
        )
        draw.rounded_rectangle(
            (x + 2, y - int(unit * 0.85), x + int(unit * 1.55), y),
            radius=max(2, unit // 4),
            fill=(255, 253, 246),
        )
    elif kind == "living":
        draw.rounded_rectangle(
            (x - (unit * 3), y - unit, x + (unit * 3), y + unit),
            radius=max(4, unit // 2),
            fill=upholstery,
            outline=outline,
            width=max(2, unit // 5),
        )
        for offset in (-2, 0, 2):
            cushion_x = x + int(offset * unit * 0.72)
            draw.rounded_rectangle(
                (cushion_x - int(unit * 0.62), y - int(unit * 0.72), cushion_x + int(unit * 0.62), y + int(unit * 0.55)),
                radius=max(2, unit // 4),
                outline=(171, 158, 137),
                width=max(1, unit // 7),
            )
        draw.ellipse(
            (x - int(unit * 1.3), y + int(unit * 1.5), x + int(unit * 1.3), y + int(unit * 2.7)),
            fill=wood,
            outline=outline,
            width=max(1, unit // 6),
        )
    elif kind == "kitchen":
        draw.rounded_rectangle(
            (x - (unit * 3), y - int(unit * 1.3), x + (unit * 3), y + int(unit * 0.15)),
            radius=max(3, unit // 3),
            fill=wood,
            outline=outline,
            width=max(2, unit // 5),
        )
        draw.rounded_rectangle(
            (x - int(unit * 1.75), y + int(unit * 0.7), x + int(unit * 1.75), y + int(unit * 1.8)),
            radius=max(3, unit // 3),
            fill=stone,
            outline=outline,
            width=max(2, unit // 5),
        )
        draw.ellipse(
            (x - int(unit * 0.55), y - unit, x + int(unit * 0.55), y - int(unit * 0.25)),
            outline=(128, 145, 147),
            width=max(2, unit // 5),
        )
    elif kind == "dining":
        draw.ellipse(
            (x - (unit * 2), y - int(unit * 1.35), x + (unit * 2), y + int(unit * 1.35)),
            fill=wood,
            outline=outline,
            width=max(2, unit // 5),
        )
        for chair_x, chair_y in (
            (x - int(unit * 2.7), y),
            (x + int(unit * 2.7), y),
            (x, y - int(unit * 1.9)),
            (x, y + int(unit * 1.9)),
        ):
            draw.ellipse(
                (chair_x - int(unit * 0.55), chair_y - int(unit * 0.55), chair_x + int(unit * 0.55), chair_y + int(unit * 0.55)),
                fill=upholstery,
                outline=outline,
                width=max(1, unit // 6),
            )
    elif kind in {"bath", "toilet"}:
        draw.rounded_rectangle(
            (x - (unit * 2), y - unit, x + (unit * 2), y + unit),
            radius=unit,
            fill=(240, 244, 241),
            outline=(111, 133, 137),
            width=max(2, unit // 5),
        )
        draw.ellipse(
            (x + int(unit * 1.05), y + int(unit * 1.15), x + int(unit * 2.15), y + int(unit * 2.25)),
            fill=stone,
            outline=(111, 133, 137),
            width=max(1, unit // 6),
        )
    elif kind == "outdoor":
        draw.rounded_rectangle(
            (x - (unit * 2), y - int(unit * 0.7), x + (unit * 2), y + int(unit * 0.7)),
            radius=max(3, unit // 3),
            fill=wood,
            outline=outline,
            width=max(2, unit // 5),
        )
        for plant_x, plant_y in ((x - int(unit * 2.5), y + int(unit * 1.4)), (x + int(unit * 2.45), y - int(unit * 1.35))):
            draw.ellipse(
                (plant_x - unit, plant_y - unit, plant_x + unit, plant_y + unit),
                fill=green,
                outline=(69, 101, 69),
                width=max(1, unit // 6),
            )
    elif kind == "stairs":
        for step in range(6):
            top = y - (unit * 2) + (step * max(3, unit // 2))
            draw.line(
                (x - (unit * 2), top, x + (unit * 2), top),
                fill=wood,
                width=max(2, unit // 4),
            )
        draw.line(
            (x - (unit * 2), y - (unit * 2), x - (unit * 2), y + (unit * 2)),
            fill=outline,
            width=max(2, unit // 4),
        )
    elif kind in {"entry", "storage"}:
        draw.rounded_rectangle(
            (x - (unit * 2), y - unit, x + (unit * 2), y + unit),
            radius=max(2, unit // 4),
            fill=wood,
            outline=outline,
            width=max(2, unit // 5),
        )
        draw.ellipse(
            (x - int(unit * 0.2), y - int(unit * 0.2), x + int(unit * 0.2), y + int(unit * 0.2)),
            fill=(238, 211, 145),
        )
    else:
        draw.rounded_rectangle(
            (x - int(unit * 1.45), y - int(unit * 1.2), x + int(unit * 1.45), y + int(unit * 1.2)),
            radius=max(4, unit // 2),
            fill=upholstery,
            outline=outline,
            width=max(2, unit // 5),
        )
        draw.ellipse(
            (x + int(unit * 1.15), y + int(unit * 0.9), x + int(unit * 2.25), y + int(unit * 2.0)),
            fill=wood,
            outline=outline,
            width=max(1, unit // 6),
        )

    if kind in {"entry", "living", "kitchen", "bedroom", "generic"}:
        plant_x = x - int(unit * 2.55)
        plant_y = y + int(unit * 1.8)
        draw.rounded_rectangle(
            (
                plant_x - int(unit * 0.52),
                plant_y,
                plant_x + int(unit * 0.52),
                plant_y + int(unit * 0.8),
            ),
            radius=max(2, unit // 5),
            fill=(190, 120, 83),
            outline=outline,
            width=max(1, unit // 7),
        )
        for leaf_x, leaf_y in (
            (plant_x - int(unit * 0.42), plant_y - int(unit * 0.45)),
            (plant_x + int(unit * 0.38), plant_y - int(unit * 0.55)),
            (plant_x, plant_y - int(unit * 0.95)),
        ):
            draw.ellipse(
                (
                    leaf_x - int(unit * 0.55),
                    leaf_y - int(unit * 0.72),
                    leaf_x + int(unit * 0.55),
                    leaf_y + int(unit * 0.72),
                ),
                fill=green,
                outline=(69, 101, 69),
                width=max(1, unit // 7),
            )

    badge_radius = max(7, int(round(unit * 0.55)))
    badge_x = x + (unit * 2)
    badge_y = y - (unit * 2)
    draw.ellipse(
        (
            badge_x - badge_radius,
            badge_y - badge_radius,
            badge_x + badge_radius,
            badge_y + badge_radius,
        ),
        fill=accent,
        outline=(255, 250, 239),
        width=max(2, badge_radius // 4),
    )
    badge_font = _font(max(10, int(round(badge_radius * 1.25))), bold=True)
    if badge_font is not None:
        text = str(marker_number)
        box = draw.textbbox((0, 0), text, font=badge_font)
        draw.text(
            (
                badge_x - ((box[2] - box[0]) / 2),
                badge_y - ((box[3] - box[1]) / 2) - box[1],
            ),
            text,
            font=badge_font,
            fill=(255, 252, 244),
        )


def _synthetic_wall_mask(size: tuple[int, int], room_count: int):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    width, height = size
    margin_x = max(28, int(width * 0.055))
    margin_y = max(24, int(height * 0.07))
    wall_width = max(8, int(round(min(size) * 0.018)))
    draw.rounded_rectangle(
        (margin_x, margin_y, width - margin_x, height - margin_y),
        radius=max(4, wall_width),
        outline=255,
        width=wall_width,
    )
    count = max(2, min(9, int(room_count or 0)))
    columns = max(2, int(math.ceil(math.sqrt(count * 1.4))))
    rows = max(1, int(math.ceil(count / columns)))
    for column in range(1, columns):
        x = margin_x + int(((width - (margin_x * 2)) * column) / columns)
        door_center = margin_y + int(((height - (margin_y * 2)) * (0.32 + (0.18 * (column % 2)))))
        draw.line((x, margin_y, x, door_center - (wall_width * 2)), fill=255, width=wall_width)
        draw.line((x, door_center + (wall_width * 2), x, height - margin_y), fill=255, width=wall_width)
    for row in range(1, rows):
        y = margin_y + int(((height - (margin_y * 2)) * row) / rows)
        door_center = margin_x + int(((width - (margin_x * 2)) * (0.38 + (0.17 * (row % 2)))))
        draw.line((margin_x, y, door_center - (wall_width * 2), y), fill=255, width=wall_width)
        draw.line((door_center + (wall_width * 2), y, width - margin_x, y), fill=255, width=wall_width)
    return mask


def _floorplan_stage(
    floorplan_path: Path,
    *,
    route_stops: list[dict[str, object]],
    bounds: dict[str, object],
    palette: dict[str, tuple[int, int, int]],
    size: tuple[int, int],
):
    with Image.open(floorplan_path) as floorplan_image:
        source = ImageOps.exif_transpose(floorplan_image).convert("RGB")
        source.thumbnail(size, Image.Resampling.LANCZOS)
        lifted_wash = _mix_color(
            tuple(palette.get("floorplan_wash") or (239, 232, 220)),
            (250, 247, 240),
            0.62,
        )
        plan = Image.new("RGB", size, lifted_wash)
        offset = ((size[0] - source.width) // 2, (size[1] - source.height) // 2)
        plan.paste(source, offset)
    plan = ImageEnhance.Brightness(plan).enhance(1.13)
    plan = ImageEnhance.Contrast(plan).enhance(1.08)
    plan = ImageEnhance.Color(plan).enhance(0.88)
    plan = Image.blend(plan, Image.new("RGB", plan.size, lifted_wash), 0.12)

    grayscale = ImageOps.grayscale(plan)
    wall_mask = grayscale.point(lambda value: 255 if value <= 132 else 0)
    wall_mask = wall_mask.filter(ImageFilter.MaxFilter(5))
    mask_coverage = sum(wall_mask.histogram()[128:]) / max(1, wall_mask.width * wall_mask.height)
    if mask_coverage < 0.004 or mask_coverage > 0.34:
        wall_mask = _synthetic_wall_mask(size, len(route_stops))
        mask_coverage = sum(wall_mask.histogram()[128:]) / max(1, wall_mask.width * wall_mask.height)

    draw = ImageDraw.Draw(plan, "RGBA")
    width_m = max(0.001, float(bounds.get("width_m") or 0.0))
    depth_m = max(0.001, float(bounds.get("depth_m") or 0.0))
    furniture_scale = max(0.68, min(1.0, 1.08 - (len(route_stops) * 0.035)))
    route_positions = _spread_route_positions(
        route_stops[:12],
        width_m=width_m,
        depth_m=depth_m,
    )
    for index, (stop, (x_ratio, y_ratio)) in enumerate(zip(route_stops[:12], route_positions)):
        center = (
            int(round(x_ratio * plan.width)),
            int(round(y_ratio * plan.height)),
        )
        _draw_furniture(
            draw,
            center=center,
            kind=_room_kind(stop),
            scale=furniture_scale,
            accent=tuple(palette.get("accent") or (177, 128, 67)),
            marker_number=index + 1,
        )
    return plan.convert("RGBA"), wall_mask, mask_coverage


def _project_affine(
    source,
    *,
    canvas_size: tuple[int, int],
    origin: tuple[float, float],
    x_vector: tuple[float, float],
    y_vector: tuple[float, float],
    resample,
):
    source_width, source_height = source.size
    forward_a = x_vector[0] / max(1, source_width)
    forward_b = y_vector[0] / max(1, source_height)
    forward_d = x_vector[1] / max(1, source_width)
    forward_e = y_vector[1] / max(1, source_height)
    determinant = (forward_a * forward_e) - (forward_b * forward_d)
    if abs(determinant) < 1e-9:
        raise ValueError("diorama_projection_is_singular")
    inverse_a = forward_e / determinant
    inverse_b = -forward_b / determinant
    inverse_d = -forward_d / determinant
    inverse_e = forward_a / determinant
    inverse_c = -((inverse_a * origin[0]) + (inverse_b * origin[1]))
    inverse_f = -((inverse_d * origin[0]) + (inverse_e * origin[1]))
    fillcolor: Any = (0, 0, 0, 0) if source.mode == "RGBA" else 0
    return source.transform(
        canvas_size,
        Image.Transform.AFFINE,
        (inverse_a, inverse_b, inverse_c, inverse_d, inverse_e, inverse_f),
        resample=resample,
        fillcolor=fillcolor,
    )


def _offset_mask(mask, *, dy: int):
    shifted = Image.new("L", mask.size, 0)
    width, height = mask.size
    if dy < 0:
        amount = min(height, -dy)
        shifted.paste(mask.crop((0, amount, width, height)), (0, 0))
    elif dy > 0:
        amount = min(height, dy)
        shifted.paste(mask.crop((0, 0, width, height - amount)), (0, amount))
    else:
        shifted.paste(mask)
    return shifted


def _brightness_metrics(image) -> dict[str, float]:
    sample = image.convert("RGB")
    sample.thumbnail((240, 180), Image.Resampling.BILINEAR)
    flattened = getattr(sample, "get_flattened_data", None)
    pixels = list(flattened() if callable(flattened) else sample.getdata())
    if not pixels:
        return {"mean_luma": 0.0, "dark_pixel_ratio": 1.0, "light_pixel_ratio": 0.0}
    luminances = [
        (0.2126 * red) + (0.7152 * green) + (0.0722 * blue)
        for red, green, blue in pixels
    ]
    return {
        "mean_luma": round(sum(luminances) / len(luminances), 2),
        "dark_pixel_ratio": round(sum(1 for value in luminances if value < 70.0) / len(luminances), 4),
        "light_pixel_ratio": round(sum(1 for value in luminances if value >= 185.0) / len(luminances), 4),
    }


def render_bright_apartment_diorama(
    *,
    floorplan_path: Path,
    walkable_scene: dict[str, object],
    palette: dict[str, tuple[int, int, int]],
    hero_path: Path | None = None,
    source_photo_count: int = 0,
    canvas_size: tuple[int, int] = (1600, 1100),
) -> tuple[Any, dict[str, object]] | None:
    """Render a bright elevated cutaway that remains legible at card-thumbnail size."""

    if any(
        dependency is None
        for dependency in (
            Image,
            ImageChops,
            ImageDraw,
            ImageEnhance,
            ImageFilter,
            ImageFont,
            ImageOps,
            ImageStat,
        )
    ):
        return None
    if not floorplan_path.exists() or not floorplan_path.is_file():
        return None

    width, height = canvas_size
    route_stops = [
        dict(stop)
        for stop in list(walkable_scene.get("route") or [])
        if isinstance(stop, dict)
    ]
    bounds = dict(walkable_scene.get("bounds") or {}) if isinstance(walkable_scene, dict) else {}
    base_wash = _mix_color(
        tuple(palette.get("wash") or (231, 222, 207)),
        (248, 246, 240),
        0.82,
    )
    top_wash = _mix_color(base_wash, (255, 255, 252), 0.45)
    bottom_wash = _mix_color(base_wash, (232, 225, 214), 0.2)
    background = Image.new("RGB", canvas_size, top_wash)
    background_draw = ImageDraw.Draw(background)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = _mix_color(top_wash, bottom_wash, ratio)
        background_draw.line((0, y, width, y), fill=color)
    if hero_path is not None and hero_path.exists() and hero_path.is_file():
        try:
            with Image.open(hero_path) as hero_image:
                hero = ImageOps.fit(
                    ImageOps.exif_transpose(hero_image).convert("RGB"),
                    canvas_size,
                    Image.Resampling.LANCZOS,
                )
            hero = ImageEnhance.Brightness(hero).enhance(1.18)
            hero = hero.filter(ImageFilter.GaussianBlur(max(22, int(round(width * 0.025)))))
            background = Image.blend(hero, background, 0.9)
        except Exception:
            pass

    canvas = background.convert("RGBA")
    atmosphere = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    atmosphere_draw = ImageDraw.Draw(atmosphere, "RGBA")
    atmosphere_draw.ellipse(
        (-int(width * 0.12), -int(height * 0.28), int(width * 0.58), int(height * 0.5)),
        fill=(255, 255, 250, 92),
    )
    atmosphere_draw.ellipse(
        (int(width * 0.58), -int(height * 0.05), int(width * 1.12), int(height * 0.58)),
        fill=(255, 247, 228, 60),
    )
    atmosphere_draw.ellipse(
        (int(width * 0.03), int(height * 0.56), int(width * 0.25), int(height * 0.88)),
        fill=(211, 228, 221, 54),
    )
    atmosphere_draw.ellipse(
        (int(width * 0.78), int(height * 0.62), int(width * 1.03), int(height * 0.96)),
        fill=(235, 207, 199, 50),
    )
    atmosphere = atmosphere.filter(ImageFilter.GaussianBlur(max(24, int(round(width * 0.035)))))
    canvas.alpha_composite(atmosphere)

    plan_size = (1040, 650)
    plan, wall_mask, mask_coverage = _floorplan_stage(
        floorplan_path,
        route_stops=route_stops,
        bounds=bounds,
        palette=palette,
        size=plan_size,
    )
    origin = (width * 0.235, height * 0.155)
    x_vector = (width * 0.68, height * 0.145)
    y_vector = (-width * 0.135, height * 0.58)
    p0 = origin
    p1 = (origin[0] + x_vector[0], origin[1] + x_vector[1])
    p2 = (origin[0] + y_vector[0], origin[1] + y_vector[1])
    p3 = (p1[0] + y_vector[0], p1[1] + y_vector[1])
    slab_depth = max(34, int(round(height * 0.047)))
    wall_height = max(28, int(round(height * 0.038)))

    stage_mask = Image.new("L", canvas_size, 0)
    stage_mask_draw = ImageDraw.Draw(stage_mask)
    stage_mask_draw.polygon((p0, p1, p3, p2), fill=210)
    stage_shadow = _offset_mask(stage_mask, dy=slab_depth + 22)
    stage_shadow = stage_shadow.filter(ImageFilter.GaussianBlur(max(18, int(round(width * 0.018)))))
    shadow_layer = Image.new("RGBA", canvas_size, (39, 31, 24, 0))
    shadow_layer.putalpha(stage_shadow.point(lambda value: int(value * 0.34)))
    canvas.alpha_composite(shadow_layer)

    draw = ImageDraw.Draw(canvas, "RGBA")
    slab_front = (
        p2,
        p3,
        (p3[0], p3[1] + slab_depth),
        (p2[0], p2[1] + slab_depth),
    )
    slab_right = (
        p1,
        p3,
        (p3[0], p3[1] + slab_depth),
        (p1[0], p1[1] + slab_depth),
    )
    accent = tuple(palette.get("accent") or (177, 128, 67))
    draw.polygon(slab_front, fill=(*_mix_color((190, 153, 109), accent, 0.18), 255))
    draw.polygon(slab_right, fill=(*_mix_color((154, 122, 88), accent, 0.14), 255))
    draw.line((*p2, *p3), fill=(255, 244, 222, 190), width=max(2, int(width * 0.002)))

    projected_plan = _project_affine(
        plan,
        canvas_size=canvas_size,
        origin=origin,
        x_vector=x_vector,
        y_vector=y_vector,
        resample=Image.Resampling.BICUBIC,
    )
    canvas.alpha_composite(projected_plan)

    projected_wall_mask = _project_affine(
        wall_mask,
        canvas_size=canvas_size,
        origin=origin,
        x_vector=x_vector,
        y_vector=y_vector,
        resample=Image.Resampling.BICUBIC,
    )
    relief_mask = Image.new("L", canvas_size, 0)
    for offset in range(0, wall_height + 1, 3):
        relief_mask = ImageChops.lighter(relief_mask, _offset_mask(projected_wall_mask, dy=-offset))
    wall_face = Image.new("RGBA", canvas_size, (169, 151, 126, 0))
    wall_face.putalpha(relief_mask.point(lambda value: int(value * 0.92)))
    canvas.alpha_composite(wall_face)
    wall_top_mask = _offset_mask(projected_wall_mask, dy=-wall_height)
    wall_top_outline = wall_top_mask.filter(ImageFilter.MaxFilter(5))
    wall_outline = Image.new("RGBA", canvas_size, (117, 101, 82, 0))
    wall_outline.putalpha(wall_top_outline.point(lambda value: int(value * 0.78)))
    canvas.alpha_composite(wall_outline)
    wall_top = Image.new("RGBA", canvas_size, (248, 245, 236, 0))
    wall_top.putalpha(wall_top_mask)
    canvas.alpha_composite(wall_top)

    overlay = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay, "RGBA")
    badge_box = (
        int(width * 0.045),
        int(height * 0.055),
        int(width * 0.33),
        int(height * 0.135),
    )
    overlay_draw.rounded_rectangle(
        badge_box,
        radius=max(18, int(height * 0.022)),
        fill=(255, 253, 247, 230),
        outline=(*_mix_color(accent, (170, 150, 121), 0.5), 220),
        width=max(2, int(width * 0.0015)),
    )
    eyebrow_font = _font(max(15, int(round(height * 0.019))), bold=True)
    title_font = _font(max(20, int(round(height * 0.027))), bold=True)
    if eyebrow_font is not None and title_font is not None:
        overlay_draw.text(
            (badge_box[0] + 24, badge_box[1] + 12),
            "GENERATED DIORAMA",
            font=eyebrow_font,
            fill=(*_mix_color(accent, (80, 61, 42), 0.42), 255),
        )
        overlay_draw.text(
            (badge_box[0] + 24, badge_box[1] + 36),
            "Whole-home cutaway",
            font=title_font,
            fill=(48, 42, 35, 255),
        )
    sparkle_x = badge_box[2] + int(width * 0.018)
    sparkle_y = badge_box[1] + int(height * 0.025)
    sparkle_size = max(6, int(round(height * 0.009)))
    overlay_draw.polygon(
        (
            (sparkle_x, sparkle_y - sparkle_size),
            (sparkle_x + (sparkle_size // 3), sparkle_y - (sparkle_size // 3)),
            (sparkle_x + sparkle_size, sparkle_y),
            (sparkle_x + (sparkle_size // 3), sparkle_y + (sparkle_size // 3)),
            (sparkle_x, sparkle_y + sparkle_size),
            (sparkle_x - (sparkle_size // 3), sparkle_y + (sparkle_size // 3)),
            (sparkle_x - sparkle_size, sparkle_y),
            (sparkle_x - (sparkle_size // 3), sparkle_y - (sparkle_size // 3)),
        ),
        fill=(232, 178, 109, 220),
    )
    overlay_draw.ellipse(
        (
            sparkle_x + (sparkle_size * 2),
            sparkle_y + (sparkle_size * 2),
            sparkle_x + (sparkle_size * 3),
            sparkle_y + (sparkle_size * 3),
        ),
        fill=(171, 207, 197, 210),
    )

    source_box = (
        int(width * 0.77),
        int(height * 0.06),
        int(width * 0.95),
        int(height * 0.115),
    )
    overlay_draw.rounded_rectangle(
        source_box,
        radius=max(14, int(height * 0.018)),
        fill=(255, 253, 247, 218),
        outline=(197, 184, 164, 210),
        width=max(2, int(width * 0.0015)),
    )
    source_font = _font(max(14, int(round(height * 0.018))), bold=True)
    if source_font is not None:
        source_text = (
            f"Floor plan + {max(0, int(source_photo_count))} photo refs"
            if source_photo_count
            else "Floor-plan layout preview"
        )
        source_text_box = overlay_draw.textbbox((0, 0), source_text, font=source_font)
        overlay_draw.text(
            (
                (source_box[0] + source_box[2] - (source_text_box[2] - source_text_box[0])) / 2,
                (source_box[1] + source_box[3] - (source_text_box[3] - source_text_box[1])) / 2 - source_text_box[1],
            ),
            source_text,
            font=source_font,
            fill=(79, 68, 55, 255),
        )

    disclosure_box = (
        int(width * 0.055),
        int(height * 0.915),
        int(width * 0.59),
        int(height * 0.968),
    )
    overlay_draw.rounded_rectangle(
        disclosure_box,
        radius=max(13, int(height * 0.016)),
        fill=(255, 253, 248, 218),
        outline=(200, 188, 169, 190),
        width=max(2, int(width * 0.0012)),
    )
    disclosure_font = _font(max(13, int(round(height * 0.017))))
    disclosure_text = "Planning preview · layout aid, not a captured tour"
    if disclosure_font is not None:
        disclosure_text_box = overlay_draw.textbbox((0, 0), disclosure_text, font=disclosure_font)
        overlay_draw.text(
            (
                disclosure_box[0] + 20,
                (disclosure_box[1] + disclosure_box[3] - (disclosure_text_box[3] - disclosure_text_box[1])) / 2 - disclosure_text_box[1],
            ),
            disclosure_text,
            font=disclosure_font,
            fill=(85, 73, 59, 255),
        )
    canvas.alpha_composite(overlay)

    stage_box = (
        int(min(p0[0], p1[0], p2[0], p3[0])),
        int(min(p0[1], p1[1], p2[1], p3[1]) - wall_height),
        int(max(p0[0], p1[0], p2[0], p3[0])),
        int(max(p0[1], p1[1], p2[1], p3[1]) + slab_depth),
    )
    brightness = _brightness_metrics(canvas)
    metadata: dict[str, object] = {
        "renderer_version": DIORAMA_PREVIEW_RENDERER_VERSION,
        "composition": "elevated_three_quarter_cutaway",
        "camera": "elevated_three_quarter",
        "background": "bright_neutral",
        "mood": "warm_playful_miniature",
        "canvas_size_px": {"width": width, "height": height},
        "displayed_route_stop_count": len(route_stops),
        "furnished_room_count": min(12, len(route_stops)),
        "wall_relief": True,
        "wall_mask_coverage": round(float(mask_coverage), 4),
        "brightness": brightness,
        "boxes": {
            "stage": list(stage_box),
            "title": list(badge_box),
            "source": list(source_box),
            "disclosure": list(disclosure_box),
        },
        "checks": {
            "stage_fits_canvas": (
                0 <= stage_box[0] < stage_box[2] <= width
                and 0 <= stage_box[1] < stage_box[3] <= height
            ),
            "labels_fit_canvas": all(
                0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
                for box in (badge_box, source_box, disclosure_box)
            ),
            "thumbnail_brightness": (
                float(brightness["mean_luma"]) >= 145.0
                and float(brightness["dark_pixel_ratio"]) <= 0.18
            ),
            "whole_home_stage_dominates": (
                (stage_box[2] - stage_box[0]) >= int(width * 0.72)
                and (stage_box[3] - stage_box[1]) >= int(height * 0.58)
            ),
        },
    }
    return canvas.convert("RGB"), metadata
