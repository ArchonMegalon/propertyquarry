#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class ScenePreset:
    name: str
    walls: tuple[tuple[tuple[float, float], tuple[float, float]], ...]
    openings: tuple[tuple[tuple[float, float], tuple[float, float]], ...]
    windows: tuple[tuple[tuple[float, float], tuple[float, float]], ...]
    sprites: tuple[tuple[float, float, str], ...]
    route: tuple[tuple[float, float], ...]
    scan_stops: tuple[tuple[float, float, float, float, str], ...] = ()


SACHSENPLATZ_PRESET = ScenePreset(
    name="sachsenplatz",
    walls=(
        ((1.0, 1.0), (5.0, 1.0)),
        ((6.0, 1.0), (9.0, 1.0)),
        ((9.0, 1.0), (9.0, 8.8)),
        ((9.0, 8.8), (1.0, 8.8)),
        ((1.0, 8.8), (1.0, 1.0)),
        ((1.0, 3.6), (4.2, 3.6)),
        ((4.2, 3.6), (4.2, 5.65)),
        ((4.2, 5.65), (1.0, 5.65)),
        ((1.0, 7.0), (3.35, 7.0)),
        ((3.35, 7.0), (3.35, 8.8)),
        ((4.9, 1.0), (4.9, 4.35)),
        ((4.9, 4.35), (4.2, 4.35)),
        ((7.9, 6.25), (9.0, 6.25)),
        ((8.55, 6.25), (8.55, 8.8)),
    ),
    openings=(
        ((5.0, 1.0), (6.0, 1.0)),
        ((4.2, 4.5), (4.2, 5.25)),
        ((4.9, 2.0), (4.9, 3.1)),
        ((8.55, 7.15), (8.55, 7.95)),
    ),
    windows=(
        ((5.0, 1.0), (6.0, 1.0)),
        ((8.55, 7.15), (8.55, 7.95)),
    ),
    sprites=(
        (6.15, 4.2, "island"),
        (7.25, 3.55, "sofa"),
        (7.55, 4.75, "table"),
        (8.05, 3.7, "tv"),
        (2.9, 2.35, "bed"),
        (2.2, 4.55, "bath"),
        (8.15, 7.55, "plants"),
        (7.1, 5.1, "chair"),
    ),
    route=(
        (4.05, 8.15),
        (4.08, 7.35),
        (4.10, 6.55),
        (4.18, 5.78),
        (4.55, 5.20),
        (5.25, 4.78),
        (6.05, 4.40),
        (6.85, 4.00),
        (7.35, 3.45),
        (7.35, 2.88),
        (6.85, 2.48),
        (6.05, 2.32),
        (5.15, 2.26),
        (4.15, 2.24),
        (3.20, 2.23),
    ),
)


PRESETS: dict[str, ScenePreset] = {
    SACHSENPLATZ_PRESET.name: SACHSENPLATZ_PRESET,
}


def _float_pair(value: object, *, fallback: tuple[float, float] = (0.0, 0.0)) -> tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return fallback
    return fallback


def _rect_edges(x: float, y: float, w: float, h: float) -> tuple[tuple[tuple[float, float], tuple[float, float]], ...]:
    return (
        ((x, y), (x + w, y)),
        ((x + w, y), (x + w, y + h)),
        ((x + w, y + h), (x, y + h)),
        ((x, y + h), (x, y)),
    )


def _room_center(room: dict[str, object]) -> tuple[float, float]:
    return (
        float(room.get("x") or 0.0) + float(room.get("w") or 1.0) / 2.0,
        float(room.get("y") or 0.0) + float(room.get("h") or 1.0) / 2.0,
    )


def _default_sprite_for_room(room: dict[str, object]) -> tuple[float, float, str]:
    cx, cy = _room_center(room)
    name = str(room.get("name") or room.get("type") or "").lower()
    kind = "chair"
    if any(token in name for token in ("bad", "bath", "wc", "toilet")):
        kind = "bath"
    elif any(token in name for token in ("kueche", "küche", "kitchen", "wohn", "living")):
        kind = "cooking" if "k" in name or "kitchen" in name else "sofa"
    elif any(token in name for token in ("zimmer", "bed", "schlaf")):
        kind = "bed"
    elif any(token in name for token in ("balkon", "balcony", "terrace", "loggia")):
        kind = "plants"
    elif any(token in name for token in ("vorraum", "hall", "entry", "flur")):
        kind = "coat"
    return (cx, cy, kind)


def scene_preset_from_floorplan_spec(spec: dict[str, object]) -> ScenePreset:
    rooms = [dict(room) for room in list(spec.get("rooms") or []) if isinstance(room, dict)]
    if not rooms:
        raise ValueError("floorplan_spec_rooms_missing")
    walls: list[tuple[tuple[float, float], tuple[float, float]]] = []
    openings: list[tuple[tuple[float, float], tuple[float, float]]] = []
    windows: list[tuple[tuple[float, float], tuple[float, float]]] = []
    sprites: list[tuple[float, float, str]] = []
    for room in rooms:
        x = float(room.get("x") or 0.0)
        y = float(room.get("y") or 0.0)
        w = max(0.4, float(room.get("w") or 1.0))
        h = max(0.4, float(room.get("h") or 1.0))
        walls.extend(_rect_edges(x, y, w, h))
        for opening in list(room.get("openings") or []):
            if isinstance(opening, dict):
                openings.append((_float_pair(opening.get("from")), _float_pair(opening.get("to"))))
        for window in list(room.get("windows") or []):
            if isinstance(window, dict):
                windows.append((_float_pair(window.get("from")), _float_pair(window.get("to"))))
        room_sprites = [row for row in list(room.get("sprites") or []) if isinstance(row, dict)]
        if room_sprites:
            for sprite in room_sprites:
                sx, sy = _float_pair(sprite.get("at"), fallback=_room_center(room))
                sprites.append((sx, sy, str(sprite.get("kind") or "chair").strip() or "chair"))
        else:
            sprites.append(_default_sprite_for_room(room))
    route = tuple(_float_pair(row) for row in list(spec.get("route") or []) if isinstance(row, (list, tuple)) and len(row) >= 2)
    if not route:
        route = tuple(_room_center(room) for room in rooms)
    scan_stops: list[tuple[float, float, float, float, str]] = []
    for index, stop in enumerate(list(spec.get("scan_stops") or [])):
        if not isinstance(stop, dict):
            continue
        x, y = _float_pair(stop.get("at"), fallback=route[min(index, len(route) - 1)])
        scan_stops.append(
            (
                x,
                y,
                float(stop.get("start_deg") or 0.0),
                max(180.0, float(stop.get("sweep_deg") or 360.0)),
                str(stop.get("label") or f"Room {index + 1}"),
            )
        )
    if not scan_stops:
        scan_stops = [
            (x, y, 0.0, 360.0 if index not in {0, len(route) - 1} else 270.0, f"Room {index + 1}")
            for index, (x, y) in enumerate(route)
        ]
    return ScenePreset(
        name=str(spec.get("name") or "floorplan").strip() or "floorplan",
        walls=tuple(walls),
        openings=tuple(openings),
        windows=tuple(windows),
        sprites=tuple(sprites),
        route=tuple(route),
        scan_stops=tuple(scan_stops),
    )


TOP22_FLOORPLAN_SPEC: dict[str, object] = {
    "name": "top22",
    "rooms": [
        {
            "name": "Vorraum / entry with jackets",
            "x": 1.0,
            "y": 5.3,
            "w": 3.6,
            "h": 3.9,
            "openings": [{"from": [2.18, 5.3], "to": [2.82, 5.3]}],
            "sprites": [{"at": [2.15, 6.55], "kind": "coat"}],
        },
        {
            "name": "Bad/WC",
            "x": 1.0,
            "y": 1.0,
            "w": 2.0,
            "h": 4.3,
            "openings": [{"from": [3.0, 3.9], "to": [3.0, 4.65]}],
            "sprites": [{"at": [2.35, 2.9], "kind": "bath"}],
        },
        {
            "name": "Wohnkueche with cooking and TV",
            "x": 4.6,
            "y": 1.0,
            "w": 6.2,
            "h": 2.8,
            "openings": [{"from": [4.6, 2.05], "to": [4.6, 2.9]}],
            "windows": [{"from": [5.4, 1.0], "to": [7.0, 1.0]}, {"from": [8.2, 1.0], "to": [10.0, 1.0]}],
            "sprites": [
                {"at": [5.35, 2.45], "kind": "island"},
                {"at": [6.55, 2.55], "kind": "cups"},
                {"at": [6.95, 3.15], "kind": "cooking"},
                {"at": [8.95, 2.45], "kind": "sofa"},
                {"at": [10.10, 2.55], "kind": "tv"},
                {"at": [9.2, 3.3], "kind": "toys"},
            ],
        },
        {
            "name": "Zimmer 1",
            "x": 4.6,
            "y": 3.8,
            "w": 3.1,
            "h": 5.4,
            "openings": [{"from": [6.55, 3.8], "to": [7.25, 3.8]}],
            "sprites": [{"at": [5.75, 6.85], "kind": "bed"}, {"at": [6.9, 8.4], "kind": "desk"}],
        },
        {
            "name": "Zimmer 2",
            "x": 7.7,
            "y": 3.8,
            "w": 3.1,
            "h": 3.2,
            "openings": [{"from": [7.7, 5.45], "to": [7.7, 6.25]}, {"from": [9.52, 7.0], "to": [10.28, 7.0]}],
            "sprites": [{"at": [8.9, 5.85], "kind": "bed"}, {"at": [9.9, 6.45], "kind": "laundry"}],
        },
        {
            "name": "Balkon",
            "x": 7.7,
            "y": 7.0,
            "w": 3.1,
            "h": 2.2,
            "windows": [{"from": [8.6, 9.2], "to": [10.4, 9.2]}],
            "sprites": [{"at": [10.0, 8.25], "kind": "plants"}, {"at": [9.25, 8.55], "kind": "chair"}],
        },
    ],
    "route": [[2.25, 8.45], [2.25, 6.35], [2.28, 2.78], [6.95, 2.78], [6.12, 7.35], [9.15, 5.95], [9.92, 8.25]],
    "scan_stops": [
        {"at": [2.25, 7.55], "start_deg": -90.0, "sweep_deg": 360.0, "label": "Vorraum / entry with jackets"},
        {"at": [2.30, 2.82], "start_deg": -20.0, "sweep_deg": 270.0, "label": "Bad/WC"},
        {"at": [6.95, 2.78], "start_deg": 0.0, "sweep_deg": 360.0, "label": "Wohnkueche with cooking and TV"},
        {"at": [6.12, 7.35], "start_deg": 88.0, "sweep_deg": 360.0, "label": "Zimmer 1"},
        {"at": [9.15, 5.95], "start_deg": 45.0, "sweep_deg": 360.0, "label": "Zimmer 2"},
        {"at": [9.92, 8.25], "start_deg": 130.0, "sweep_deg": 270.0, "label": "Balkon"},
    ],
}

PRESETS[TOP22_FLOORPLAN_SPEC["name"]] = scene_preset_from_floorplan_spec(TOP22_FLOORPLAN_SPEC)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def point_segment_distance(px: float, py: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    abx = bx - ax
    aby = by - ay
    apx = px - ax
    apy = py - ay
    ab2 = abx * abx + aby * aby
    if ab2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, (apx * abx + apy * aby) / ab2))
    cx = ax + t * abx
    cy = ay + t * aby
    return math.hypot(px - cx, py - cy)


def ray_segment_intersection(
    origin: tuple[float, float],
    direction: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple[float, tuple[float, float], float] | None:
    ox, oy = origin
    dx, dy = direction
    ax, ay = a
    bx, by = b
    sx = bx - ax
    sy = by - ay
    denom = dx * sy - dy * sx
    if abs(denom) < 1e-9:
        return None
    qx = ax - ox
    qy = ay - oy
    t = (qx * sy - qy * sx) / denom
    u = (qx * dy - qy * dx) / denom
    if t >= 0 and 0 <= u <= 1:
        return t, (ox + dx * t, oy + dy * t), u
    return None


def make_floor_texture(size: int = 512) -> np.ndarray:
    tex = np.zeros((size, size, 3), dtype=np.uint8)
    for y in range(size):
        for x in range(size):
            plank = ((x // 42) % 2) * 8
            grain = int(18 * math.sin(x * 0.12 + y * 0.02) + 10 * math.sin(x * 0.43))
            base = np.array([94, 137, 187], dtype=np.int32)
            tex[y, x] = np.clip(base + plank + grain, 0, 255).astype(np.uint8)
    for seam in range(0, size, 42):
        cv2.line(tex, (seam, 0), (seam, size - 1), (78, 117, 161), 1)
    return tex


def make_wall_texture(size: int = 512) -> np.ndarray:
    tex = np.zeros((size, size, 3), dtype=np.uint8)
    base = np.array([232, 236, 240], dtype=np.int32)
    for y in range(size):
        for x in range(size):
            noise = int(4 * math.sin(x * 0.08) + 3 * math.sin(y * 0.14))
            tex[y, x] = np.clip(base + noise, 0, 255).astype(np.uint8)
    for stripe in range(28, size, 96):
        cv2.line(tex, (stripe, 0), (stripe, size - 1), (220, 224, 228), 3)
    return tex


def make_window_texture(size: int = 512) -> np.ndarray:
    tex = np.zeros((size, size, 3), dtype=np.uint8)
    sky = np.array([240, 238, 220], dtype=np.int32)
    for y in range(size):
        for x in range(size):
            grad = int(14 * (1.0 - y / max(1, size - 1)))
            tex[y, x] = np.clip(sky + np.array([grad, grad, grad // 2]), 0, 255).astype(np.uint8)
    cv2.rectangle(tex, (0, 0), (size - 1, size - 1), (205, 210, 220), 10)
    cv2.line(tex, (size // 2, 0), (size // 2, size - 1), (205, 210, 220), 8)
    return tex


FLOOR_TEXTURE = make_floor_texture()
WALL_TEXTURE = make_wall_texture()
WINDOW_TEXTURE = make_window_texture()


def sample_texture(texture: np.ndarray, u: float, v: float) -> np.ndarray:
    h, w = texture.shape[:2]
    tx = int(abs(u % 1.0) * (w - 1))
    ty = int(abs(v % 1.0) * (h - 1))
    return texture[ty, tx].astype(np.float32)


def sprite_color(kind: str) -> tuple[int, int, int]:
    return {
        "island": (226, 230, 234),
        "sofa": (205, 209, 212),
        "table": (92, 127, 177),
        "tv": (44, 44, 46),
        "bed": (114, 140, 106),
        "bath": (220, 220, 224),
        "plants": (72, 122, 78),
        "chair": (98, 122, 158),
        "coat": (88, 86, 96),
        "cups": (230, 232, 222),
        "cooking": (190, 210, 198),
        "toys": (90, 150, 204),
        "desk": (128, 116, 98),
        "laundry": (205, 188, 180),
    }.get(kind, (170, 170, 170))


class OneTakeRenderer:
    def __init__(
        self,
        *,
        preset: ScenePreset,
        width: int = 1280,
        height: int = 720,
        fps: int = 24,
        duration: float = 14.0,
        fov_deg: float = 74.0,
        cam_height: float = 0.54,
        eye_level: float = 0.47,
        wall_height: float = 1.0,
    ) -> None:
        self.preset = preset
        self.width = width
        self.height = height
        self.fps = fps
        self.duration = duration
        self.frame_total = int(self.fps * self.duration)
        self.fov = math.radians(fov_deg)
        self.half_fov = self.fov / 2.0
        self.cam_height = cam_height
        self.eye_level = eye_level
        self.wall_height = wall_height

    def is_opening(self, point: tuple[float, float]) -> bool:
        return any(point_segment_distance(point[0], point[1], a, b) < 0.03 for a, b in self.preset.openings)

    def classify_hit(self, point: tuple[float, float]) -> str:
        return "window" if any(point_segment_distance(point[0], point[1], a, b) < 0.04 for a, b in self.preset.windows) else "wall"

    def cast_ray(self, origin: tuple[float, float], angle: float) -> tuple[float, tuple[float, float], float, str] | None:
        direction = (math.cos(angle), math.sin(angle))
        best = None
        for seg in self.preset.walls:
            hit = ray_segment_intersection(origin, direction, seg[0], seg[1])
            if hit is None:
                continue
            distance, point, u = hit
            if distance <= 0 or self.is_opening(point):
                continue
            kind = self.classify_hit(point)
            if best is None or distance < best[0]:
                best = (distance, point, u, kind)
        return best

    def draw_sprite(self, frame: np.ndarray, depth_buffer: np.ndarray, cam_x: float, cam_y: float, cam_a: float, sprite: tuple[float, float, str]) -> None:
        sx, sy, kind = sprite
        rel_x = sx - cam_x
        rel_y = sy - cam_y
        sin_a = math.sin(-cam_a)
        cos_a = math.cos(-cam_a)
        view_x = rel_x * cos_a - rel_y * sin_a
        view_y = rel_x * sin_a + rel_y * cos_a
        if view_y <= 0.25:
            return
        focal = self.width / (2.0 * math.tan(self.half_fov))
        center_x = int(self.width / 2 + (view_x / view_y) * focal)
        scale = int((self.height * 0.72) / view_y)
        if scale < 8:
            return
        x0 = center_x - scale // 2
        x1 = center_x + scale // 2
        y0 = int(self.height * 0.58 - scale * 0.62)
        y1 = y0 + scale
        if x1 < 0 or x0 >= self.width or y1 < 0 or y0 >= self.height:
            return
        color = sprite_color(kind)
        alpha = max(0.10, min(0.32, 0.38 / view_y))
        for x in range(max(0, x0), min(self.width, x1)):
            if view_y >= depth_buffer[x]:
                continue
            band = (x - x0) / max(1, x1 - x0)
            shaded = np.array(color, dtype=np.float32) * (0.88 + 0.12 * math.sin(band * math.pi))
            for y in range(max(0, y0), min(self.height, y1)):
                frame[y, x] = np.clip(frame[y, x].astype(np.float32) * (1.0 - alpha) + shaded * alpha, 0, 255).astype(np.uint8)

    def render_frame(self, cam_x: float, cam_y: float, cam_a: float, roll_deg: float) -> np.ndarray:
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        horizon = int(self.height * self.eye_level)
        depth_buffer = np.full(self.width, 9999.0, dtype=np.float32)
        ceiling_top = np.array([250, 248, 244], dtype=np.float32)
        ceiling_bottom = np.array([228, 224, 218], dtype=np.float32)
        focal = self.width / (2.0 * math.tan(self.half_fov))

        for y in range(horizon):
            t = y / max(1, horizon - 1)
            frame[y, :, :] = np.clip(ceiling_top * (1.0 - t) + ceiling_bottom * t, 0, 255).astype(np.uint8)

        for y in range(horizon, self.height):
            row_distance = (self.cam_height * focal) / max(1.0, (y - horizon))
            for x in range(0, self.width, 2):
                angle = cam_a - self.half_fov + (x / self.width) * self.fov
                wx = cam_x + math.cos(angle) * row_distance
                wy = cam_y + math.sin(angle) * row_distance
                tex_color = sample_texture(FLOOR_TEXTURE, wx * 0.7, wy * 0.7)
                shade = max(0.45, 1.2 - row_distance * 0.08)
                color = np.clip(tex_color * shade, 0, 255).astype(np.uint8)
                frame[y, x : x + 2, :] = color

        for x in range(self.width):
            angle = cam_a - self.half_fov + (x / max(1, self.width - 1)) * self.fov
            hit = self.cast_ray((cam_x, cam_y), angle)
            if hit is None:
                continue
            distance, _point, segment_u, kind = hit
            distance *= math.cos(angle - cam_a)
            depth_buffer[x] = distance
            if distance <= 0.01:
                continue
            wall_height = min(self.height * 0.94, (self.height * self.wall_height * 0.92) / distance)
            y0 = int(horizon - wall_height * (1.0 - self.cam_height))
            y1 = int(horizon + wall_height * self.cam_height)
            texture = WINDOW_TEXTURE if kind == "window" else WALL_TEXTURE
            for y in range(max(0, y0), min(self.height, y1)):
                v = (y - y0) / max(1, y1 - y0)
                tex_color = sample_texture(texture, segment_u, v)
                shade = max(0.40, 1.24 - distance * 0.11)
                frame[y, x] = np.clip(tex_color * shade, 0, 255).astype(np.uint8)

        for sprite in self.preset.sprites:
            self.draw_sprite(frame, depth_buffer, cam_x, cam_y, cam_a, sprite)

        rotation = cv2.getRotationMatrix2D((self.width / 2, self.height / 2), roll_deg, 1.0)
        frame = cv2.warpAffine(frame, rotation, (self.width, self.height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        yy, xx = np.mgrid[0 : self.height, 0 : self.width]
        dx = (xx - self.width / 2) / (self.width / 2)
        dy = (yy - self.height / 2) / (self.height / 2)
        vignette = np.clip(1.0 - 0.18 * (dx * dx + dy * dy), 0.78, 1.0)
        return np.clip(frame.astype(np.float32) * vignette[..., None], 0, 255).astype(np.uint8)

    def route_pose(self, t: float) -> tuple[float, float, float]:
        if self.preset.scan_stops:
            stops = self.preset.scan_stops
            move_share = 0.28
            scan_share = 0.72
            segment_count = len(stops)
            seg = min(segment_count - 1, int(max(0.0, min(0.999999, t)) * segment_count))
            local = (t * segment_count) - seg
            x, y, start_deg, sweep_deg, _label = stops[seg]
            prev_x, prev_y = self.preset.route[0] if seg == 0 else (stops[seg - 1][0], stops[seg - 1][1])
            approach_angle = math.atan2(y - prev_y, x - prev_x) if (x, y) != (prev_x, prev_y) else math.radians(start_deg)
            if local < move_share:
                u = smoothstep(local / move_share)
                return lerp(prev_x, x, u), lerp(prev_y, y, u), lerp(approach_angle, math.radians(start_deg), u)
            u = smoothstep((local - move_share) / scan_share)
            return x, y, math.radians(start_deg + sweep_deg * u)
        route = self.preset.route
        t = max(0.0, min(0.999999, t))
        n = len(route) - 1
        u = t * n
        i = int(u)
        frac = smoothstep(u - i)
        x0, y0 = route[i]
        x1, y1 = route[i + 1]
        x = lerp(x0, x1, frac)
        y = lerp(y0, y1, frac)
        dx = x1 - x0
        dy = y1 - y0
        angle = math.atan2(dy, dx)
        return x, y, angle

    def render_preview_frames(self, out_dir: Path, *, frames: tuple[int, ...] = (0, 60, 120, 180, 240, 300)) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        frame_cap = max(frames) if frames else 0
        for idx in frames:
            t = idx / max(1, frame_cap)
            x, y, angle = self.route_pose(t)
            roll = 1.8 * math.sin(t * math.pi * 2.0) + 0.7 * math.sin(t * math.pi * 6.0)
            frame = self.render_frame(x, y, angle, roll)
            cv2.imwrite(str(out_dir / f"frame_{idx:03d}.png"), frame)

    def render_video(self, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (self.width, self.height))
        for n in range(self.frame_total):
            t = n / max(1, self.frame_total - 1)
            x, y, angle = self.route_pose(t)
            speed = 0.7 + 0.3 * math.sin(t * math.pi)
            roll = 2.0 * math.sin(t * math.pi * 2.2) * speed + 0.8 * math.sin(t * math.pi * 7.0)
            writer.write(self.render_frame(x, y, angle, roll))
        writer.release()
        return out_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a true one-take shell-style flythrough previs from a hard-coded room shell preset."
    )
    parser.add_argument("--preset", choices=sorted(PRESETS), default="sachsenplatz")
    parser.add_argument(
        "--floorplan-json",
        default="",
        help="Optional generic floorplan spec JSON with rooms, openings, windows, route, and scan_stops.",
    )
    parser.add_argument("--out-dir", default="/tmp/sachsenplatz_true_take_v2")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--duration", type=float, default=14.0)
    parser.add_argument("--preview-only", action="store_true", help="Render preview frames only, skip the MP4.")
    parser.add_argument("--no-preview", action="store_true", help="Render the MP4 only, skip preview stills.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.floorplan_json:
        spec_path = Path(args.floorplan_json).expanduser()
        preset = scene_preset_from_floorplan_spec(json.loads(spec_path.read_text(encoding="utf-8")))
    else:
        preset = PRESETS[args.preset]
    out_dir = Path(args.out_dir).expanduser()
    renderer = OneTakeRenderer(
        preset=preset,
        width=args.width,
        height=args.height,
        fps=args.fps,
        duration=args.duration,
    )
    if not args.no_preview:
        renderer.render_preview_frames(out_dir)
    if not args.preview_only:
        renderer.render_video(out_dir / f"{preset.name}_true_one_take.mp4")
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
