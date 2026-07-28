#!/usr/bin/env python3
"""Canonical, deterministic furnishing styles for generated reconstructions.

This module intentionally has no dependency on the web route layer so the API,
render bridge, standalone generator, and publication/readiness checks all bind
the same style identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping


STYLE_SCENE_CONTRACT_VERSION = "propertyquarry_generated_style_scene_v1"
GENERATED_RECONSTRUCTION_VIEWER_VERSION = "propertyquarry_3d_tour_viewer_v5"
FLOORPLAN_DISPLAY_MODE = "reference_toggle_default_off"

_STYLE_ROWS: tuple[dict[str, object], ...] = (
    {
        "id": "warm_scandi",
        "label": "Warm Scandinavian",
        "prompt": "warm Scandinavian staging, bright neutral textiles, light oak, clean storage, realistic family-home warmth",
        "aliases": ("warm scandi", "warm scandinavian", "scandi", "scandinavian"),
        "palette": {
            "background": "#e8eeeb",
            "floor": "#e7dbc5",
            "wall": "#f4efe4",
            "edge": "#c2ab83",
            "textile": "#d9d0c3",
            "paleTextile": "#eee9df",
            "timber": "#c5a274",
            "stone": "#d8d4ca",
            "accent": "#78938a",
            "foliage": "#718467",
            "rattan": "#c39b65",
            "metal": "#8d9692",
        },
        "required_cues": ("light_oak", "linen", "neutral_textile", "clean_storage"),
        "decor": (
            ("light-oak-console", "table", "light_oak", "timber", (-1.06, 0.30, -0.68), (0.88, 0.60, 0.34)),
            ("linen-lounge", "box", "linen", "textile", (0.92, 0.23, -0.62), (0.82, 0.46, 0.52)),
            ("neutral-rug", "rug", "neutral_textile", "paleTextile", (-0.48, 0.03, 0.82), (1.55, 0.03, 1.05)),
            ("clean-storage", "shelf", "clean_storage", "stone", (1.08, 0.56, 0.70), (0.66, 1.12, 0.30)),
        ),
    },
    {
        "id": "ikea_practical",
        "label": "IKEA practical",
        "prompt": "IKEA-inspired practical modular furniture, bright storage, simple rental-friendly pieces, realistic affordable staging",
        "aliases": ("ikea", "ikea practical", "practical"),
        "palette": {
            "background": "#eef2f4",
            "floor": "#e9e2d3",
            "wall": "#f7f6f0",
            "edge": "#8796a6",
            "textile": "#d6dce2",
            "paleTextile": "#f2f0e7",
            "timber": "#c5a06c",
            "stone": "#f4f3ed",
            "accent": "#2f6fb3",
            "foliage": "#6f8965",
            "rattan": "#c69d66",
            "metal": "#f4c542",
        },
        "required_cues": ("modular_storage", "bright_storage", "simple_lines", "rental_friendly"),
        "decor": (
            ("modular-storage", "shelf", "modular_storage", "stone", (-1.08, 0.62, -0.20), (0.72, 1.24, 0.32)),
            ("blue-cabinet", "box", "bright_storage", "accent", (0.98, 0.36, -0.66), (0.72, 0.72, 0.36)),
            ("yellow-stool", "cylinder", "simple_lines", "metal", (-1.28, 0.24, 0.80), (0.34, 0.48, 0.34)),
            ("rental-table", "table", "rental_friendly", "timber", (0.88, 0.31, 0.80), (0.82, 0.62, 0.52)),
        ),
    },
    {
        "id": "urban_jungle",
        "label": "Urban jungle",
        "prompt": "urban jungle interior with healthy plants, rattan, warm wood, linen, soft daylight, lived-in but uncluttered",
        "aliases": ("urban jungle", "jungle", "lush"),
        "palette": {
            "background": "#dfe8df",
            "floor": "#b58b5f",
            "wall": "#eee7da",
            "edge": "#76624a",
            "textile": "#dfd2bd",
            "paleTextile": "#eee4d4",
            "timber": "#8f603d",
            "stone": "#b8ae95",
            "accent": "#365f43",
            "foliage": "#3f7049",
            "rattan": "#b78249",
            "metal": "#8c7659",
        },
        "required_cues": ("healthy_plants", "rattan", "warm_wood", "linen"),
        "decor": (
            ("healthy-plant", "plant", "healthy_plants", "foliage", (-1.20, 0.52, -0.72), (0.62, 1.24, 0.62)),
            ("rattan-chair", "rattan_chair", "rattan", "rattan", (1.04, 0.42, -0.66), (0.64, 0.84, 0.64)),
            ("warm-wood-table", "table", "warm_wood", "timber", (-0.96, 0.27, 0.82), (0.86, 0.54, 0.56)),
            ("linen-rug", "rug", "linen", "textile", (0.18, 0.03, 0.72), (1.62, 0.03, 1.10)),
        ),
    },
    {
        "id": "landhaus",
        "label": "Landhaus",
        "prompt": "Austrian Landhaus country-home staging, warm timber, linen, ceramics, classic comfortable furniture, premium realistic finish",
        "aliases": ("landhaus", "country", "country home", "austrian landhaus"),
        "palette": {
            "background": "#eee7dc",
            "floor": "#aa754c",
            "wall": "#f1e4d0",
            "edge": "#806243",
            "textile": "#d7c4a7",
            "paleTextile": "#eee1cc",
            "timber": "#855936",
            "stone": "#c8b79e",
            "accent": "#6d7f52",
            "foliage": "#667b50",
            "rattan": "#b78b58",
            "metal": "#826f5e",
        },
        "required_cues": ("warm_timber", "linen", "ceramics", "traditional_details"),
        "decor": (
            ("timber-bench", "bench", "warm_timber", "timber", (-1.04, 0.25, -0.68), (1.02, 0.50, 0.42)),
            ("linen-settee", "box", "linen", "textile", (0.94, 0.28, -0.62), (0.86, 0.56, 0.54)),
            ("ceramic-vessels", "vessels", "ceramics", "stone", (-0.72, 0.34, 0.82), (0.58, 0.68, 0.36)),
            ("traditional-cabinet", "shelf", "traditional_details", "accent", (1.04, 0.62, 0.76), (0.68, 1.24, 0.34)),
        ),
    },
    {
        "id": "gilded_penthouse",
        "label": "Trump gold",
        "prompt": "playful Trump-style gold maximalist penthouse staging with polished marble, brass, gold accents, oversized classical details, photorealistic but tasteful",
        "aliases": ("trump gold", "gilded penthouse", "gold", "penthouse", "playful luxe"),
        "palette": {
            "background": "#eee7d7",
            "floor": "#eee7db",
            "wall": "#f6eddd",
            "edge": "#a47b28",
            "textile": "#622f3c",
            "paleTextile": "#efe0b0",
            "timber": "#5d4029",
            "stone": "#ddd7ce",
            "accent": "#c79a31",
            "foliage": "#59694a",
            "rattan": "#c29955",
            "metal": "#d6ad3c",
        },
        "required_cues": ("polished_marble", "brass", "gold_accents", "classical_details"),
        "decor": (
            ("marble-plinth", "box", "polished_marble", "stone", (-1.04, 0.34, -0.68), (0.86, 0.68, 0.52)),
            ("brass-console", "table", "brass", "metal", (0.96, 0.32, -0.64), (0.92, 0.64, 0.30)),
            ("gold-column", "cylinder", "gold_accents", "accent", (-0.76, 0.64, 0.84), (0.34, 1.28, 0.34)),
            ("classical-vessels", "vessels", "classical_details", "paleTextile", (0.88, 0.38, 0.82), (0.64, 0.76, 0.38)),
        ),
    },
)

STYLE_CATALOG: tuple[dict[str, object], ...] = tuple(
    {
        key: value
        for key, value in row.items()
        if key not in {"aliases", "decor"}
    }
    for row in _STYLE_ROWS
)
STYLE_IDS = frozenset(str(row["id"]) for row in _STYLE_ROWS)
_STYLE_BY_ID = {str(row["id"]): row for row in _STYLE_ROWS}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalized_style_input(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def reconstruction_style(value: object = "", *, style_id: object = "") -> dict[str, object]:
    """Return a canonical style identity or raise for an unsupported request."""

    explicit_id = _normalized_style_input(style_id).replace("-", "_").replace(" ", "_")
    raw = _normalized_style_input(value)
    selected: Mapping[str, object] | None = _STYLE_BY_ID.get(explicit_id)
    if selected is None and raw in _STYLE_BY_ID:
        selected = _STYLE_BY_ID[raw]
    if selected is None:
        for row in _STYLE_ROWS:
            candidates = {
                _normalized_style_input(row["id"]),
                _normalized_style_input(row["label"]),
                _normalized_style_input(row["prompt"]),
                *(_normalized_style_input(alias) for alias in tuple(row.get("aliases") or ())),
            }
            if raw and raw in candidates:
                selected = row
                break
    if selected is None and raw:
        # Accept the canonical catalog prompts even when transport has added
        # harmless surrounding copy; require distinctive cue combinations so
        # an arbitrary display label never silently becomes a supported style.
        cue_markers = (
            ("urban_jungle", ("urban jungle", "rattan", "linen")),
            ("ikea_practical", ("ikea", "modular", "storage")),
            ("gilded_penthouse", ("gold", "marble", "brass")),
            ("landhaus", ("landhaus", "timber", "ceramic")),
            ("warm_scandi", ("scandinav", "light oak", "neutral")),
        )
        for candidate_id, markers in cue_markers:
            if all(marker in raw for marker in markers):
                selected = _STYLE_BY_ID[candidate_id]
                break
    if selected is None and not raw and not explicit_id:
        selected = _STYLE_BY_ID["warm_scandi"]
    if selected is None:
        raise ValueError("property_reconstruction_style_unsupported")

    identity_core = {
        "contract_version": STYLE_SCENE_CONTRACT_VERSION,
        "id": str(selected["id"]),
        "label": str(selected["label"]),
        "prompt": str(selected["prompt"]),
        "palette": dict(selected["palette"]),
        "required_cues": list(selected["required_cues"]),
    }
    return {
        **identity_core,
        "signature": _digest(identity_core),
        "request_input_sha256": "sha256:"
        + hashlib.sha256(str(value or "").strip().encode("utf-8")).hexdigest(),
    }


def build_style_scene(
    style: Mapping[str, object],
    *,
    route_stop_count: int,
) -> dict[str, object]:
    """Build the exact deterministic decor instance plan consumed by the viewer."""

    canonical = reconstruction_style(style.get("id"), style_id=style.get("id"))
    row = _STYLE_BY_ID[str(canonical["id"])]
    stop_count = max(1, int(route_stop_count or 0))
    instances: list[dict[str, object]] = []
    for route_index in range(stop_count):
        for decor_index, decor in enumerate(tuple(row["decor"])):
            name, shape, cue, material, position, dimensions = decor
            instances.append(
                {
                    "id": f"{canonical['id']}-r{route_index + 1:02d}-{decor_index + 1:02d}-{name}",
                    "route_index": route_index,
                    "kind": str(name),
                    "shape": str(shape),
                    "cue": str(cue),
                    "material": str(material),
                    "position": {
                        "x": float(position[0]),
                        "y": float(position[1]),
                        "z": float(position[2]),
                    },
                    "dimensions": {
                        "x": float(dimensions[0]),
                        "y": float(dimensions[1]),
                        "z": float(dimensions[2]),
                    },
                }
            )
    core = {
        "contract_version": STYLE_SCENE_CONTRACT_VERSION,
        "style_id": canonical["id"],
        "style_label": canonical["label"],
        "style_signature": canonical["signature"],
        "material_palette": dict(canonical["palette"]),
        "required_cues": list(canonical["required_cues"]),
        "instances": instances,
        "route_stop_count": stop_count,
        "instances_per_route": len(tuple(row["decor"])),
        "minimum_instance_count": len(instances),
        "floorplan_display_mode": FLOORPLAN_DISPLAY_MODE,
        "materially_applied": True,
    }
    return {
        **core,
        "scene_signature": _digest(core),
        "evidence_status": "ready",
    }


def validate_style_scene(
    scene: object,
    *,
    expected_style: Mapping[str, object] | None = None,
) -> tuple[bool, str]:
    if not isinstance(scene, Mapping):
        return False, "style_scene_missing"
    if str(scene.get("contract_version") or "") != STYLE_SCENE_CONTRACT_VERSION:
        return False, "style_scene_contract_invalid"
    try:
        canonical = reconstruction_style(
            (expected_style or scene).get("id") or scene.get("style_id"),
            style_id=(expected_style or scene).get("id") or scene.get("style_id"),
        )
    except (AttributeError, ValueError):
        return False, "style_scene_identity_invalid"
    if str(scene.get("style_id") or "") != str(canonical["id"]):
        return False, "style_scene_id_mismatch"
    if str(scene.get("style_label") or "") != str(canonical["label"]):
        return False, "style_scene_label_mismatch"
    if str(scene.get("style_signature") or "") != str(canonical["signature"]):
        return False, "style_scene_signature_mismatch"
    if expected_style and str(expected_style.get("signature") or "") != str(canonical["signature"]):
        return False, "requested_style_signature_mismatch"
    if scene.get("materially_applied") is not True:
        return False, "style_scene_not_applied"
    if str(scene.get("evidence_status") or "") != "ready":
        return False, "style_scene_evidence_not_ready"
    if str(scene.get("floorplan_display_mode") or "") != FLOORPLAN_DISPLAY_MODE:
        return False, "style_scene_floorplan_mode_invalid"
    if dict(scene.get("material_palette") or {}) != dict(canonical["palette"]):
        return False, "style_scene_palette_mismatch"
    required_cues = list(canonical["required_cues"])
    if list(scene.get("required_cues") or []) != required_cues:
        return False, "style_scene_required_cues_mismatch"
    try:
        route_stop_count = int(scene.get("route_stop_count") or 0)
        instances_per_route = int(scene.get("instances_per_route") or 0)
        minimum_instance_count = int(scene.get("minimum_instance_count") or 0)
    except (TypeError, ValueError):
        return False, "style_scene_instance_count_invalid"
    if route_stop_count <= 0 or instances_per_route != len(tuple(_STYLE_BY_ID[str(canonical["id"])]["decor"])):
        return False, "style_scene_route_contract_invalid"
    instances = list(scene.get("instances") or [])
    if minimum_instance_count != route_stop_count * instances_per_route or len(instances) != minimum_instance_count:
        return False, "style_scene_instances_missing"
    instance_ids: set[str] = set()
    observed_cues: set[str] = set()
    supported_shapes = {"bench", "box", "cylinder", "plant", "rattan_chair", "rug", "shelf", "table", "vessels"}
    palette = dict(canonical["palette"])
    cues_by_route: dict[int, set[str]] = {index: set() for index in range(route_stop_count)}
    for instance in instances:
        if not isinstance(instance, Mapping):
            return False, "style_scene_instance_invalid"
        if set(instance) != {
            "id",
            "route_index",
            "kind",
            "shape",
            "cue",
            "material",
            "position",
            "dimensions",
        }:
            return False, "style_scene_instance_schema_invalid"
        instance_id = str(instance.get("id") or "")
        cue = str(instance.get("cue") or "")
        material = str(instance.get("material") or "")
        shape = str(instance.get("shape") or "")
        if not instance_id or instance_id in instance_ids:
            return False, "style_scene_instance_id_invalid"
        if not cue or material not in palette or shape not in supported_shapes:
            return False, "style_scene_instance_binding_invalid"
        position = instance.get("position")
        dimensions = instance.get("dimensions")
        if not isinstance(position, Mapping) or not isinstance(dimensions, Mapping):
            return False, "style_scene_instance_geometry_invalid"
        if set(position) != {"x", "y", "z"} or set(dimensions) != {"x", "y", "z"}:
            return False, "style_scene_instance_geometry_schema_invalid"
        try:
            position_values = tuple(float(position[axis]) for axis in ("x", "y", "z"))
            dimension_values = tuple(float(dimensions[axis]) for axis in ("x", "y", "z"))
            route_index = int(instance.get("route_index"))
        except (KeyError, TypeError, ValueError):
            return False, "style_scene_instance_geometry_invalid"
        if not all(value == value and abs(value) <= 4.0 for value in position_values):
            return False, "style_scene_instance_position_invalid"
        if not all(value == value and 0.02 <= value <= 4.0 for value in dimension_values):
            return False, "style_scene_instance_dimensions_invalid"
        if route_index < 0 or route_index >= route_stop_count:
            return False, "style_scene_instance_route_invalid"
        instance_ids.add(instance_id)
        observed_cues.add(cue)
        cues_by_route[route_index].add(cue)
    if not set(required_cues).issubset(observed_cues):
        return False, "style_scene_required_cue_evidence_missing"
    if any(not set(required_cues).issubset(route_cues) for route_cues in cues_by_route.values()):
        return False, "style_scene_route_cue_coverage_missing"
    expected_scene = build_style_scene(canonical, route_stop_count=route_stop_count)
    if list(scene.get("instances") or []) != list(expected_scene.get("instances") or []):
        return False, "style_scene_instances_not_canonical"
    core = {
        "contract_version": scene.get("contract_version"),
        "style_id": scene.get("style_id"),
        "style_label": scene.get("style_label"),
        "style_signature": scene.get("style_signature"),
        "material_palette": scene.get("material_palette"),
        "required_cues": scene.get("required_cues"),
        "instances": scene.get("instances"),
        "route_stop_count": scene.get("route_stop_count"),
        "instances_per_route": scene.get("instances_per_route"),
        "minimum_instance_count": scene.get("minimum_instance_count"),
        "floorplan_display_mode": scene.get("floorplan_display_mode"),
        "materially_applied": scene.get("materially_applied"),
    }
    if str(scene.get("scene_signature") or "") != _digest(core):
        return False, "style_scene_evidence_signature_mismatch"
    return True, "ready"
