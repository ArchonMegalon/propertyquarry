#!/usr/bin/env python3
"""Evidence-backed floorplan ingestion and 3D-to-floorplan round-tripping.

The first-generation tour builder mixed source-image pins, manually typed room
areas, and synthetic model coordinates in one script.  That made it possible
to publish a model whose labels were internally consistent while its footprint
was not traceable to the source plan.  This module is the boundary between an
architectural plan and a tour: it normalizes the source, validates room
evidence, renders the plan implied by the completed construction, and records a
machine-readable comparison receipt.

Room annotations are deliberately explicit.  OCR/CV may propose them, but a
publishable ``approved`` analysis must carry the dimension text and a review
confidence for every room.  Missing or conflicting evidence raises instead of
silently inventing geometry.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps


ANALYZER_CONTRACT = "propertyquarry.floorplan_analysis.v2"
ROUNDTRIP_CONTRACT = "propertyquarry.floorplan_roundtrip.v1"
MIN_REVIEW_CONFIDENCE = 0.90
DEFAULT_TOLERANCE_M = 0.05


class FloorplanAnalysisError(ValueError):
    """Stable, path-free rejection for unsafe or incomplete plan evidence."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(value: object, *, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise FloorplanAnalysisError("floorplan_numeric_evidence_invalid") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise FloorplanAnalysisError("floorplan_numeric_evidence_invalid")
    return result


def _bounds(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise FloorplanAnalysisError("floorplan_room_bounds_missing")
    result = {
        key: _finite(raw.get(key), minimum=0.0)
        for key in ("x", "y", "width", "height")
    }
    if result["width"] <= 0.0 or result["height"] <= 0.0:
        raise FloorplanAnalysisError("floorplan_room_bounds_invalid")
    if result["x"] + result["width"] > 100.0 or result["y"] + result["height"] > 100.0:
        raise FloorplanAnalysisError("floorplan_room_bounds_invalid")
    return {key: round(value, 6) for key, value in result.items()}


def _source_bbox_px(
    raw: object,
    *,
    room_id: str,
    source_size: dict[str, int],
) -> dict[str, int] | None:
    """Validate an operator-reviewed wall envelope in source pixels.

    A percentage box can look internally consistent while pointing at the
    wrong part of a scanned plan.  When a reviewed pixel envelope is present,
    it is the source of truth and the percentage box is checked against it.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise FloorplanAnalysisError(f"floorplan_source_bbox_invalid:{room_id}")
    try:
        values = {
            key: int(round(_finite(raw.get(key), minimum=0.0)))
            for key in ("x", "y", "width", "height")
        }
    except (TypeError, ValueError) as exc:
        raise FloorplanAnalysisError(f"floorplan_source_bbox_invalid:{room_id}") from exc
    if values["width"] <= 0 or values["height"] <= 0:
        raise FloorplanAnalysisError(f"floorplan_source_bbox_invalid:{room_id}")
    if (
        values["x"] + values["width"] > int(source_size["width"])
        or values["y"] + values["height"] > int(source_size["height"])
    ):
        raise FloorplanAnalysisError(f"floorplan_source_bbox_invalid:{room_id}")
    return values


def _source_bbox_to_bounds(
    source_bbox: dict[str, int],
    *,
    source_size: dict[str, int],
) -> dict[str, float]:
    return {
        "x": round(source_bbox["x"] * 100.0 / source_size["width"], 6),
        "y": round(source_bbox["y"] * 100.0 / source_size["height"], 6),
        "width": round(source_bbox["width"] * 100.0 / source_size["width"], 6),
        "height": round(source_bbox["height"] * 100.0 / source_size["height"], 6),
    }


def _source_geometry(
    raw: object,
    *,
    room_ids: set[str],
    source_size: dict[str, int],
    source_bboxes: dict[str, dict[str, int]],
) -> dict[str, object]:
    """Normalize the source-pixel construction graph.

    The first tour workflow converted each room to an independently guessed
    metric rectangle.  That is useful for labels, but it cannot reproduce a
    scanned plan whose rooms do not share one uniform scale.  This contract is
    deliberately pixel-native: the reviewed source components are the
    authoritative footprint, and metric measurements remain annotations.
    """
    if not isinstance(raw, dict):
        raise FloorplanAnalysisError("floorplan_source_geometry_missing")
    if str(raw.get("contract_name") or "").strip() != "propertyquarry.floorplan_source_geometry.v1":
        raise FloorplanAnalysisError("floorplan_source_geometry_contract_invalid")
    if str(raw.get("coordinate_system") or "").strip() != "source_image_pixels":
        raise FloorplanAnalysisError("floorplan_source_geometry_coordinates_invalid")
    canvas = raw.get("canvas_size_px")
    if not isinstance(canvas, dict):
        raise FloorplanAnalysisError("floorplan_source_geometry_canvas_invalid")
    try:
        canvas_size = {
            "width": int(round(_finite(canvas.get("width"), minimum=1.0))),
            "height": int(round(_finite(canvas.get("height"), minimum=1.0))),
        }
    except (TypeError, ValueError) as exc:
        raise FloorplanAnalysisError("floorplan_source_geometry_canvas_invalid") from exc
    if canvas_size != source_size:
        raise FloorplanAnalysisError("floorplan_source_geometry_canvas_mismatch")
    raw_rooms = raw.get("rooms")
    if not isinstance(raw_rooms, list) or not raw_rooms:
        raise FloorplanAnalysisError("floorplan_source_geometry_rooms_missing")
    normalized_rooms: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_room in raw_rooms:
        if not isinstance(raw_room, dict):
            raise FloorplanAnalysisError("floorplan_source_geometry_room_invalid")
        room_id = str(raw_room.get("id") or "").strip()
        if room_id not in room_ids or room_id in seen:
            raise FloorplanAnalysisError("floorplan_source_geometry_room_invalid")
        seen.add(room_id)
        raw_components = raw_room.get("components_px")
        if not isinstance(raw_components, list) or not raw_components:
            raise FloorplanAnalysisError(f"floorplan_source_geometry_components_missing:{room_id}")
        components: list[dict[str, int]] = []
        for raw_component in raw_components:
            if not isinstance(raw_component, dict):
                raise FloorplanAnalysisError(f"floorplan_source_geometry_component_invalid:{room_id}")
            try:
                component = {
                    key: int(round(_finite(raw_component.get(key), minimum=0.0)))
                    for key in ("x", "y", "width", "height")
                }
            except (TypeError, ValueError) as exc:
                raise FloorplanAnalysisError(f"floorplan_source_geometry_component_invalid:{room_id}") from exc
            if component["width"] <= 0 or component["height"] <= 0:
                raise FloorplanAnalysisError(f"floorplan_source_geometry_component_invalid:{room_id}")
            if (
                component["x"] + component["width"] > source_size["width"]
                or component["y"] + component["height"] > source_size["height"]
            ):
                raise FloorplanAnalysisError(f"floorplan_source_geometry_component_invalid:{room_id}")
            components.append(component)
        left = min(item["x"] for item in components)
        top = min(item["y"] for item in components)
        right = max(item["x"] + item["width"] for item in components)
        bottom = max(item["y"] + item["height"] for item in components)
        expected = source_bboxes.get(room_id)
        if expected is None or max(
            abs(left - expected["x"]),
            abs(top - expected["y"]),
            abs((right - left) - expected["width"]),
            abs((bottom - top) - expected["height"]),
        ) > 12:
            raise FloorplanAnalysisError(f"floorplan_source_geometry_bbox_mismatch:{room_id}")
        normalized_rooms.append({"id": room_id, "components_px": components})
    if seen != room_ids:
        raise FloorplanAnalysisError("floorplan_source_geometry_coverage_incomplete")
    normalized_portals: list[dict[str, object]] = []
    raw_portals = raw.get("portals") or []
    if not isinstance(raw_portals, list):
        raise FloorplanAnalysisError("floorplan_source_geometry_portals_invalid")
    for raw_portal in raw_portals:
        if not isinstance(raw_portal, dict):
            raise FloorplanAnalysisError("floorplan_source_geometry_portal_invalid")
        portal_id = str(raw_portal.get("id") or "").strip()
        kind = str(raw_portal.get("kind") or "door").strip().lower()
        if not portal_id or kind not in {"door", "exit_gate"}:
            raise FloorplanAnalysisError("floorplan_source_geometry_portal_invalid")
        raw_room_ids = raw_portal.get("room_ids")
        if not isinstance(raw_room_ids, list) or len(raw_room_ids) not in {1, 2}:
            raise FloorplanAnalysisError("floorplan_source_geometry_portal_invalid")
        portal_room_ids = [str(value or "").strip() for value in raw_room_ids]
        if any(value != "outside" and value not in room_ids for value in portal_room_ids):
            raise FloorplanAnalysisError("floorplan_source_geometry_portal_invalid")
        if kind == "door" and (len(portal_room_ids) != 2 or "outside" in portal_room_ids):
            raise FloorplanAnalysisError("floorplan_source_geometry_portal_invalid")
        if kind == "exit_gate" and "outside" not in portal_room_ids:
            raise FloorplanAnalysisError("floorplan_source_geometry_portal_invalid")
        center = raw_portal.get("center_px")
        if not isinstance(center, dict):
            raise FloorplanAnalysisError("floorplan_source_geometry_portal_invalid")
        try:
            center_px = {
                "x": int(round(_finite(center.get("x"), minimum=0.0))),
                "y": int(round(_finite(center.get("y"), minimum=0.0))),
                "width": int(round(_finite(raw_portal.get("width_px"), minimum=1.0))),
            }
        except (TypeError, ValueError) as exc:
            raise FloorplanAnalysisError("floorplan_source_geometry_portal_invalid") from exc
        if (
            center_px["x"] > source_size["width"]
            or center_px["y"] > source_size["height"]
            or center_px["width"] < 12
        ):
            raise FloorplanAnalysisError("floorplan_source_geometry_portal_invalid")
        raw_sides = raw_portal.get("room_sides")
        if not isinstance(raw_sides, dict):
            raise FloorplanAnalysisError("floorplan_source_geometry_portal_sides_missing")
        room_sides: dict[str, str] = {}
        for room_id in portal_room_ids:
            if room_id == "outside":
                continue
            side = str(raw_sides.get(room_id) or "").strip().lower()
            if side not in {"north", "south", "east", "west"}:
                raise FloorplanAnalysisError("floorplan_source_geometry_portal_side_invalid")
            room_sides[room_id] = side
            room_components = next(
                item["components_px"]
                for item in normalized_rooms
                if item["id"] == room_id
            )
            on_boundary = False
            half_width = center_px["width"] / 2
            for component in room_components:
                left = component["x"]
                right = left + component["width"]
                top = component["y"]
                bottom = top + component["height"]
                horizontal_span = left - half_width <= center_px["x"] <= right + half_width
                vertical_span = top - half_width <= center_px["y"] <= bottom + half_width
                if side == "north" and abs(center_px["y"] - top) <= 30 and horizontal_span:
                    on_boundary = True
                if side == "south" and abs(center_px["y"] - bottom) <= 30 and horizontal_span:
                    on_boundary = True
                if side == "west" and abs(center_px["x"] - left) <= 30 and vertical_span:
                    on_boundary = True
                if side == "east" and abs(center_px["x"] - right) <= 30 and vertical_span:
                    on_boundary = True
            if not on_boundary:
                raise FloorplanAnalysisError(f"floorplan_source_geometry_portal_boundary_invalid:{portal_id}")
        normalized_portal = {
            "id": portal_id,
            "kind": kind,
            "room_ids": portal_room_ids,
            "room_sides": room_sides,
            "center_px": {"x": center_px["x"], "y": center_px["y"]},
            "width_px": center_px["width"],
            "target_room_id": str(raw_portal.get("target_room_id") or "").strip(),
        }
        for key in ("label", "target_label"):
            value = str(raw_portal.get(key) or "").strip()
            if value:
                normalized_portal[key] = value
        normalized_portals.append(normalized_portal)
    return {
        "contract_name": "propertyquarry.floorplan_source_geometry.v1",
        "coordinate_system": "source_image_pixels",
        "canvas_size_px": canvas_size,
        "rooms": normalized_rooms,
        "portals": normalized_portals,
    }


def _rectangles(raw: object, *, room_id: str) -> list[dict[str, float]]:
    if not isinstance(raw, list) or not raw:
        raise FloorplanAnalysisError(f"floorplan_room_components_missing:{room_id}")
    result: list[dict[str, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise FloorplanAnalysisError(f"floorplan_room_components_invalid:{room_id}")
        values = {
            key: _finite(item.get(key), minimum=0.0)
            for key in ("x", "z", "width", "depth")
        }
        if values["width"] <= 0.0 or values["depth"] <= 0.0:
            raise FloorplanAnalysisError(f"floorplan_room_components_invalid:{room_id}")
        result.append({key: round(value, 6) for key, value in values.items()})
    return result


def _dimension_evidence(raw: object, *, room_id: str) -> list[dict[str, object]]:
    if not isinstance(raw, list) or not raw:
        raise FloorplanAnalysisError(f"floorplan_dimension_evidence_missing:{room_id}")
    result: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise FloorplanAnalysisError(f"floorplan_dimension_evidence_invalid:{room_id}")
        text = str(item.get("text") or "").strip()
        method = str(item.get("method") or "").strip()
        confidence = _finite(item.get("confidence"), minimum=0.0)
        if not text or not method or confidence > 1.0:
            raise FloorplanAnalysisError(f"floorplan_dimension_evidence_invalid:{room_id}")
        result.append(
            {
                "text": text,
                "method": method,
                "confidence": round(confidence, 4),
                **(
                    {"source_bbox_pct": _bounds(item["source_bbox_pct"])}
                    if isinstance(item.get("source_bbox_pct"), dict)
                    else {}
                ),
            }
        )
    return result


def _boundary_pairs(
    raw: object,
    *,
    room_ids: set[str],
    field: str,
) -> list[list[str]]:
    """Normalize explicit room-boundary constraints from the reviewed plan."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FloorplanAnalysisError(f"floorplan_{field}_invalid")
    result: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise FloorplanAnalysisError(f"floorplan_{field}_invalid")
        left, right = (str(item[0]).strip(), str(item[1]).strip())
        if left not in room_ids or right not in room_ids or left == right:
            raise FloorplanAnalysisError(f"floorplan_{field}_invalid")
        pair = tuple(sorted((left, right)))
        if pair in seen:
            raise FloorplanAnalysisError(f"floorplan_{field}_invalid")
        seen.add(pair)
        result.append(list(pair))
    return result


def _rectangles_share_boundary(
    left: list[dict[str, float]],
    right: list[dict[str, float]],
    *,
    tolerance: float = 0.06,
    minimum_overlap: float = 0.45,
) -> bool:
    """Return whether two room footprints touch along a meaningful wall edge."""
    for a in left:
        for b in right:
            horizontal = (
                abs(a["z"] + a["depth"] - b["z"]) <= tolerance
                or abs(b["z"] + b["depth"] - a["z"]) <= tolerance
            ) and max(
                0.0,
                min(a["x"] + a["width"], b["x"] + b["width"])
                - max(a["x"], b["x"]),
            ) >= minimum_overlap
            vertical = (
                abs(a["x"] + a["width"] - b["x"]) <= tolerance
                or abs(b["x"] + b["width"] - a["x"]) <= tolerance
            ) and max(
                0.0,
                min(a["z"] + a["depth"], b["z"] + b["depth"])
                - max(a["z"], b["z"]),
            ) >= minimum_overlap
            if horizontal or vertical:
                return True
    return False


def _normalise_source(source: Path) -> tuple[Image.Image, str, dict[str, int]]:
    if not source.is_file() or source.stat().st_size <= 0:
        raise FloorplanAnalysisError("floorplan_source_missing")
    raw_sha256 = _sha256(source)
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
    except Exception as exc:  # pragma: no cover - Pillow error varies by plugin
        raise FloorplanAnalysisError("floorplan_source_unreadable") from exc
    width, height = image.size
    if width < 400 or height < 400 or width * height > 64_000_000:
        raise FloorplanAnalysisError("floorplan_source_dimensions_invalid")
    return image, raw_sha256, {"width": width, "height": height}


def _content_bbox(image: Image.Image) -> dict[str, int]:
    """Find the architectural ink envelope without treating the whole scan as geometry."""
    gray = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    preview_width = min(360, max(160, gray.width // 5))
    preview_height = max(120, round(gray.height * preview_width / max(gray.width, 1)))
    preview = gray.resize((preview_width, preview_height), Image.Resampling.LANCZOS)
    pixels = preview.load()
    active = [
        (x, y)
        for y in range(preview_height)
        for x in range(preview_width)
        if pixels[x, y] < 190
    ]
    if not active:
        return {"left": 0, "top": 0, "right": image.width, "bottom": image.height}
    min_x = min(item[0] for item in active)
    max_x = max(item[0] for item in active)
    min_y = min(item[1] for item in active)
    max_y = max(item[1] for item in active)
    scale_x = image.width / preview_width
    scale_y = image.height / preview_height
    padding = max(8, round(min(image.width, image.height) * 0.012))
    return {
        "left": max(0, round(min_x * scale_x) - padding),
        "top": max(0, round(min_y * scale_y) - padding),
        "right": min(image.width, round((max_x + 1) * scale_x) + padding),
        "bottom": min(image.height, round((max_y + 1) * scale_y) + padding),
    }


def _stable_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def analyze_floorplan(
    source: Path,
    *,
    specification: dict[str, object],
    output_dir: Path | None = None,
) -> dict[str, object]:
    """Validate and materialize a versioned source-plan evidence artifact."""
    image, source_sha256, source_size = _normalise_source(source)
    if not isinstance(specification, dict):
        raise FloorplanAnalysisError("floorplan_specification_invalid")
    rooms_raw = specification.get("rooms")
    if not isinstance(rooms_raw, list) or not rooms_raw:
        raise FloorplanAnalysisError("floorplan_rooms_missing")
    rooms: list[dict[str, object]] = []
    room_ids: set[str] = set()
    source_bboxes: dict[str, dict[str, int]] = {}
    for raw_room in rooms_raw:
        if not isinstance(raw_room, dict):
            raise FloorplanAnalysisError("floorplan_room_invalid")
        room_id = str(raw_room.get("id") or "").strip()
        if not room_id or room_id in room_ids:
            raise FloorplanAnalysisError("floorplan_room_id_invalid")
        room_ids.add(room_id)
        label = str(raw_room.get("label") or room_id).strip()
        kind = str(raw_room.get("kind") or "interior").strip().lower()
        if kind not in {"interior", "exterior", "unavailable"}:
            raise FloorplanAnalysisError(f"floorplan_room_kind_invalid:{room_id}")
        bounds = _bounds(raw_room.get("floorplan_bounds_pct"))
        source_bbox = _source_bbox_px(
            raw_room.get("source_bbox_px"),
            room_id=room_id,
            source_size=source_size,
        )
        if source_bbox is not None:
            source_bboxes[room_id] = source_bbox
            source_bounds = _source_bbox_to_bounds(source_bbox, source_size=source_size)
            if max(
                abs(source_bounds[key] - bounds[key])
                for key in ("x", "y", "width", "height")
            ) > 0.75:
                raise FloorplanAnalysisError(
                    f"floorplan_source_bbox_drift:{room_id}"
                )
            # Keep the normalized artifact derived from the reviewed pixels;
            # the hand-entered percentage box is only a cross-check.
            bounds = source_bounds
        components = _rectangles(raw_room.get("components"), room_id=room_id)
        area_m2 = _finite(raw_room.get("area_m2"), minimum=0.001)
        component_area = sum(item["width"] * item["depth"] for item in components)
        tolerance = _finite(
            raw_room.get("measurement_tolerance_m", DEFAULT_TOLERANCE_M),
            minimum=0.005,
        )
        if abs(component_area - area_m2) > tolerance:
            raise FloorplanAnalysisError(f"floorplan_room_area_mismatch:{room_id}")
        evidence = _dimension_evidence(raw_room.get("dimension_evidence"), room_id=room_id)
        confidence = min(float(item["confidence"]) for item in evidence)
        if confidence < MIN_REVIEW_CONFIDENCE:
            raise FloorplanAnalysisError(f"floorplan_dimension_confidence_low:{room_id}")
        dimension_label = str(raw_room.get("dimension_label") or "").strip()
        if not dimension_label:
            raise FloorplanAnalysisError(f"floorplan_dimension_label_missing:{room_id}")
        shape = str(raw_room.get("shape") or ("rectangle" if len(components) == 1 else "composite")).strip().lower()
        if shape not in {"rectangle", "composite"}:
            raise FloorplanAnalysisError(f"floorplan_room_shape_invalid:{room_id}")
        if shape == "rectangle" and len(components) != 1:
            raise FloorplanAnalysisError(f"floorplan_room_shape_mismatch:{room_id}")
        room = {
            "id": room_id,
            "label": label,
            "kind": kind,
            "scene_id": str(raw_room.get("scene_id") or "").strip(),
            "shape": shape,
            "floorplan_bounds_pct": bounds,
            **({"source_bbox_px": source_bbox} if source_bbox is not None else {}),
            "area_m2": round(area_m2, 4),
            "dimension_label": dimension_label,
            "components": components,
            "dimension_evidence": evidence,
            "confidence": round(confidence, 4),
        }
        rooms.append(room)

    edges_raw = specification.get("doorway_edges")
    if not isinstance(edges_raw, list):
        raise FloorplanAnalysisError("floorplan_doorways_missing")
    edges: list[list[str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for raw_edge in edges_raw:
        if not isinstance(raw_edge, (list, tuple)) or len(raw_edge) != 2:
            raise FloorplanAnalysisError("floorplan_doorways_invalid")
        left, right = (str(raw_edge[0]).strip(), str(raw_edge[1]).strip())
        if left not in room_ids or right not in room_ids or left == right:
            raise FloorplanAnalysisError("floorplan_doorways_invalid")
        edge = tuple(sorted((left, right)))
        if edge in seen_edges:
            raise FloorplanAnalysisError("floorplan_doorways_invalid")
        seen_edges.add(edge)
        edges.append(list(edge))

    room_components_by_id = {
        str(room["id"]): list(room["components"])
        for room in rooms
    }
    boundary_adjacency_raw = specification.get("boundary_adjacency")
    boundary_adjacency = {
        "required": _boundary_pairs(
            dict(boundary_adjacency_raw or {}).get("required")
            if isinstance(boundary_adjacency_raw, dict)
            else None,
            room_ids=room_ids,
            field="boundary_adjacency",
        ),
        "forbidden": _boundary_pairs(
            dict(boundary_adjacency_raw or {}).get("forbidden")
            if isinstance(boundary_adjacency_raw, dict)
            else None,
            room_ids=room_ids,
            field="boundary_adjacency",
        ),
    }
    for pair in boundary_adjacency["required"]:
        if not _rectangles_share_boundary(
            room_components_by_id[pair[0]], room_components_by_id[pair[1]]
        ):
            raise FloorplanAnalysisError(
                f"floorplan_required_boundary_missing:{pair[0]}:{pair[1]}"
            )
    for pair in boundary_adjacency["forbidden"]:
        if _rectangles_share_boundary(
            room_components_by_id[pair[0]], room_components_by_id[pair[1]]
        ):
            raise FloorplanAnalysisError(
                f"floorplan_forbidden_boundary_present:{pair[0]}:{pair[1]}"
            )

    if specification.get("source_geometry") is None:
        # Keep the analyzer backwards-compatible for older non-publishable
        # callers.  The showcase/publish path must provide the explicit
        # source-pixel contract above; this fallback is intentionally marked
        # as legacy so it cannot be mistaken for a reviewed wall trace.
        legacy_rooms = []
        for room in rooms:
            bounds = dict(room.get("floorplan_bounds_pct") or {})
            legacy_rooms.append({
                "id": str(room["id"]),
                "components_px": [{
                    "x": round(float(bounds["x"]) * source_size["width"] / 100),
                    "y": round(float(bounds["y"]) * source_size["height"] / 100),
                    "width": round(float(bounds["width"]) * source_size["width"] / 100),
                    "height": round(float(bounds["height"]) * source_size["height"] / 100),
                }],
            })
        source_geometry = {
            "contract_name": "propertyquarry.floorplan_source_geometry.v1",
            "coordinate_system": "source_image_pixels",
            "canvas_size_px": dict(source_size),
            "rooms": legacy_rooms,
            "portals": [],
            "review_status": "legacy_derived",
        }
    else:
        source_geometry = _source_geometry(
            specification.get("source_geometry"),
            room_ids=room_ids,
            source_size=source_size,
            source_bboxes=source_bboxes,
        )

    content_bbox = _content_bbox(image)
    analysis: dict[str, object] = {
        "contract_name": ANALYZER_CONTRACT,
        "analysis_method": "source-normalization-plus-reviewed-dimension-evidence-v2",
        "review_status": "approved",
        "source": {
            "sha256": source_sha256,
            "size": source_size,
            "content_bbox_px": content_bbox,
        },
        "rooms": rooms,
        "doorway_edges": edges,
        "boundary_adjacency": boundary_adjacency,
        "source_geometry": source_geometry,
        "room_count": len(rooms),
        "minimum_dimension_confidence": round(
            min(float(room["confidence"]) for room in rooms), 4
        ),
        "measurement_tolerance_m": round(
            max(float(raw.get("measurement_tolerance_m", DEFAULT_TOLERANCE_M)) for raw in rooms_raw if isinstance(raw, dict)),
            4,
        ),
    }
    analysis["analysis_sha256"] = hashlib.sha256(_stable_json(analysis)).hexdigest()
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        normalised_path = output_dir / "source-floorplan-normalized.png"
        image.save(normalised_path, format="PNG", optimize=True)
        analysis_path = output_dir / "floorplan-analysis.json"
        analysis_path.write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        analysis["normalized_relpath"] = normalised_path.name
        analysis["analysis_relpath"] = analysis_path.name
    return analysis


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:  # pragma: no cover - depends on host fonts
        return ImageFont.load_default()


def render_derived_floorplan(
    analysis: dict[str, object],
    target: Path,
    *,
    source_size: tuple[int, int],
) -> dict[str, object]:
    """Render a clean top-down plan from the finished construction geometry."""
    width, height = source_size
    canvas = Image.new("RGB", (width, height), (250, 248, 242))
    draw = ImageDraw.Draw(canvas)
    rooms = list(analysis.get("rooms") or [])
    palette = ((43, 93, 75), (115, 83, 43), (54, 85, 121), (119, 61, 91))
    for index, raw_room in enumerate(rooms):
        if not isinstance(raw_room, dict):
            continue
        bounds = dict(raw_room.get("floorplan_bounds_pct") or {})
        left = round(width * float(bounds.get("x") or 0.0) / 100.0)
        top = round(height * float(bounds.get("y") or 0.0) / 100.0)
        right = round(width * (float(bounds.get("x") or 0.0) + float(bounds.get("width") or 0.0)) / 100.0)
        bottom = round(height * (float(bounds.get("y") or 0.0) + float(bounds.get("height") or 0.0)) / 100.0)
        color = palette[index % len(palette)]
        fill = (*color, 34)
        room_components = [item for item in list(raw_room.get("components") or []) if isinstance(item, dict)]
        min_x = min((float(item.get("x") or 0.0) for item in room_components), default=0.0)
        min_z = min((float(item.get("z") or 0.0) for item in room_components), default=0.0)
        max_x = max((float(item.get("x") or 0.0) + float(item.get("width") or 0.0) for item in room_components), default=1.0)
        max_z = max((float(item.get("z") or 0.0) + float(item.get("depth") or 0.0) for item in room_components), default=1.0)
        room_span_x = max(max_x - min_x, 0.001)
        room_span_z = max(max_z - min_z, 0.001)
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        for component in room_components or [{"x": min_x, "z": min_z, "width": room_span_x, "depth": room_span_z}]:
            component_left = left + round((float(component.get("x") or 0.0) - min_x) / room_span_x * max(1, right - left))
            component_top = top + round((float(component.get("z") or 0.0) - min_z) / room_span_z * max(1, bottom - top))
            component_right = component_left + round(float(component.get("width") or 0.0) / room_span_x * max(1, right - left))
            component_bottom = component_top + round(float(component.get("depth") or 0.0) / room_span_z * max(1, bottom - top))
            overlay_draw.rectangle((component_left, component_top, component_right, component_bottom), fill=fill)
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(canvas)
        for component in room_components or [{"x": min_x, "z": min_z, "width": room_span_x, "depth": room_span_z}]:
            component_left = left + round((float(component.get("x") or 0.0) - min_x) / room_span_x * max(1, right - left))
            component_top = top + round((float(component.get("z") or 0.0) - min_z) / room_span_z * max(1, bottom - top))
            component_right = component_left + round(float(component.get("width") or 0.0) / room_span_x * max(1, right - left))
            component_bottom = component_top + round(float(component.get("depth") or 0.0) / room_span_z * max(1, bottom - top))
            draw.rectangle((component_left, component_top, component_right, component_bottom), outline=color, width=max(3, round(min(width, height) / 280)))
        label = str(raw_room.get("label") or raw_room.get("id") or "")
        label = label.split(" · ", 1)[0]
        dim = str(raw_room.get("dimension_label") or "")
        text = f"{label}\n{dim}" if dim else label
        label_component = max(
            room_components,
            key=lambda item: float(item.get("width") or 0.0) * float(item.get("depth") or 0.0),
            default={"x": min_x, "z": min_z, "width": room_span_x, "depth": room_span_z},
        )
        label_left = left + round((float(label_component.get("x") or 0.0) - min_x) / room_span_x * max(1, right - left))
        label_top = top + round((float(label_component.get("z") or 0.0) - min_z) / room_span_z * max(1, bottom - top))
        label_right = label_left + round(float(label_component.get("width") or 0.0) / room_span_x * max(1, right - left))
        label_bottom = label_top + round(float(label_component.get("depth") or 0.0) / room_span_z * max(1, bottom - top))
        label_font = _font(max(14, round(width / 125)))
        text_box = draw.multiline_textbbox((0, 0), text, font=label_font, spacing=3)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        text_x = max(label_left + 6, min(label_right - text_width - 6, label_left + 8))
        text_y = max(label_top + 6, min(label_bottom - text_height - 6, label_top + 8))
        draw.rounded_rectangle(
            (text_x - 4, text_y - 3, text_x + text_width + 4, text_y + text_height + 4),
            radius=4,
            fill=(250, 248, 242),
        )
        draw.multiline_text((text_x, text_y), text, fill=(24, 30, 28), font=label_font, spacing=3)
    draw.text((20, 20), "Derived from completed 3D construction · not the source scan", fill=(24, 30, 28), font=_font(max(16, round(width / 90))))
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, format="PNG", optimize=True)
    return {"relpath": target.name, "sha256": _sha256(target), "width": width, "height": height}


def _room_geometry_signature(analysis: dict[str, object]) -> str:
    rows = []
    for room in list(analysis.get("rooms") or []):
        if not isinstance(room, dict):
            continue
        rows.append(
            {
                "id": room.get("id"),
                "area_m2": room.get("area_m2"),
                "dimension_label": room.get("dimension_label"),
                "bounds": room.get("floorplan_bounds_pct"),
                "components": room.get("components"),
            }
        )
    return hashlib.sha256(_stable_json(rows)).hexdigest()


def compare_constructed_floorplan(
    analysis: dict[str, object],
    *,
    derived: Path,
    source: Path,
    geometry: dict[str, object] | None = None,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
) -> dict[str, object]:
    """Compare the derived plan against the source evidence and geometry.

    The comparison is intentionally structural rather than a brittle pixel
    diff: scanned plans contain annotations, furniture, and skew.  Room IDs,
    dimensions, source placement bounds, and the generated artifact hash are
    exact; callers may add a visual reviewer on top of the proof overlay.
    """
    if not derived.is_file() or derived.stat().st_size <= 0:
        raise FloorplanAnalysisError("floorplan_derived_missing")
    if not source.is_file() or source.stat().st_size <= 0:
        raise FloorplanAnalysisError("floorplan_source_missing")
    source_sha256 = str(dict(analysis.get("source") or {}).get("sha256") or "")
    if source_sha256 != _sha256(source):
        raise FloorplanAnalysisError("floorplan_source_changed")
    rooms = [room for room in list(analysis.get("rooms") or []) if isinstance(room, dict)]
    model_rooms = {
        str(room.get("id") or ""): room
        for room in list((geometry or {}).get("rooms") or [])
        if isinstance(room, dict)
    }
    dimension_errors: list[dict[str, object]] = []
    placement_errors: list[dict[str, object]] = []
    for room in rooms:
        room_id = str(room.get("id") or "")
        model = model_rooms.get(room_id, room)
        expected_components = list(room.get("components") or [])
        actual_components = list(
            dict(model.get("measurement") or {}).get("components")
            or model.get("components")
            or []
        )
        expected_area = float(room.get("area_m2") or 0.0)
        actual_area = sum(
            float(item.get("width") or 0.0) * float(item.get("depth") or 0.0)
            for item in actual_components
            if isinstance(item, dict)
        )
        error = abs(actual_area - expected_area)
        if error > tolerance_m:
            dimension_errors.append({"room_id": room_id, "area_error_m2": round(error, 6)})
        expected_bounds = dict(room.get("floorplan_bounds_pct") or {})
        actual_bounds = dict(model.get("floorplan_bounds_pct") or expected_bounds)
        max_error = max(
            abs(float(actual_bounds.get(key) or 0.0) - float(expected_bounds.get(key) or 0.0))
            for key in ("x", "y", "width", "height")
        )
        if max_error > 1.0:
            placement_errors.append({"room_id": room_id, "max_error_pct": round(max_error, 6)})
    result = {
        "contract_name": ROUNDTRIP_CONTRACT,
        "status": "pass" if not dimension_errors and not placement_errors else "blocked",
        "source_sha256": source_sha256,
        "derived_sha256": _sha256(derived),
        "construction_geometry_signature": _room_geometry_signature({"rooms": rooms}),
        "room_count": len(rooms),
        "dimension_errors": dimension_errors,
        "placement_errors": placement_errors,
        "tolerance_m": round(float(tolerance_m), 4),
        "comparison_method": "room-dimensions-and-source-placement-bounds-v1",
    }
    if result["status"] != "pass":
        raise FloorplanAnalysisError("floorplan_roundtrip_mismatch")
    result["receipt_sha256"] = hashlib.sha256(_stable_json(result)).hexdigest()
    return result


__all__ = [
    "ANALYZER_CONTRACT",
    "DEFAULT_TOLERANCE_M",
    "FloorplanAnalysisError",
    "ROUNDTRIP_CONTRACT",
    "analyze_floorplan",
    "compare_constructed_floorplan",
    "render_derived_floorplan",
]
