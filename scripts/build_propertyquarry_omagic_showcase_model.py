#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def _args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description="Build a furnished PropertyQuarry GLB for governed OMagic proof.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--receipt", default="")
    parser.add_argument("--preview", default="")
    return parser.parse_args(argv)


def _material(name: str, color: tuple[float, float, float, float], *, metallic: float = 0.0, roughness: float = 0.55):
    material = bpy.data.materials.new(name)
    material.diffuse_color = color
    material.use_nodes = True
    shader = material.node_tree.nodes.get("Principled BSDF")
    if shader is not None:
        shader.inputs["Base Color"].default_value = color
        shader.inputs["Metallic"].default_value = metallic
        shader.inputs["Roughness"].default_value = roughness
    return material


def _box(name: str, location: tuple[float, float, float], size: tuple[float, float, float], material, *, bevel: float = 0.04):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = tuple(value / 2.0 for value in size)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(material)
    if bevel > 0:
        modifier = obj.modifiers.new(name="soft_edges", type="BEVEL")
        modifier.width = bevel
        modifier.segments = 2
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    return obj


def _cylinder(name: str, location: tuple[float, float, float], radius: float, depth: float, material, *, vertices: int = 24):
    bpy.ops.mesh.primitive_cylinder_add(vertices=vertices, radius=radius, depth=depth, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(material)
    return obj


def _chair(prefix: str, x: float, y: float, rotation: float, materials: dict[str, object]) -> None:
    parts = [
        _box(f"{prefix}_seat", (x, y, 0.62), (0.58, 0.58, 0.12), materials["oak"], bevel=0.06),
        _box(f"{prefix}_back", (x, y + 0.25, 1.02), (0.58, 0.10, 0.72), materials["linen"], bevel=0.05),
    ]
    for index, (dx, dy) in enumerate(((-0.22, -0.22), (0.22, -0.22), (-0.22, 0.22), (0.22, 0.22)), start=1):
        parts.append(_box(f"{prefix}_leg_{index}", (x + dx, y + dy, 0.30), (0.07, 0.07, 0.60), materials["charcoal"], bevel=0.02))
    for obj in parts:
        obj.rotation_euler[2] = rotation


def _plant(prefix: str, x: float, y: float, scale: float, materials: dict[str, object]) -> None:
    _cylinder(f"{prefix}_pot", (x, y, 0.28 * scale), 0.28 * scale, 0.56 * scale, materials["terracotta"])
    _cylinder(f"{prefix}_stem", (x, y, 0.92 * scale), 0.045 * scale, 1.10 * scale, materials["plant_dark"], vertices=12)
    for index, (dx, dy, dz, sx, sy) in enumerate(
        ((0.18, 0.00, 1.15, 0.40, 0.16), (-0.17, 0.05, 1.35, 0.36, 0.15), (0.05, -0.13, 1.55, 0.43, 0.17), (0.14, 0.08, 1.75, 0.32, 0.14)),
        start=1,
    ):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=20, ring_count=12, location=(x + dx * scale, y + dy * scale, dz * scale))
        leaf = bpy.context.object
        leaf.name = f"{prefix}_leaf_{index}"
        leaf.scale = (sx * scale, sy * scale, 0.07 * scale)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        leaf.data.materials.append(materials["plant"])


def _build_scene() -> dict[str, object]:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    materials = {
        "oak": _material("light_oak", (0.58, 0.35, 0.17, 1.0), roughness=0.72),
        "oak_light": _material("oak_floor", (0.72, 0.53, 0.31, 1.0), roughness=0.78),
        "plaster": _material("warm_plaster", (0.91, 0.88, 0.80, 1.0), roughness=0.90),
        "linen": _material("warm_linen", (0.72, 0.68, 0.59, 1.0), roughness=0.95),
        "linen_light": _material("light_linen", (0.90, 0.87, 0.79, 1.0), roughness=0.96),
        "charcoal": _material("charcoal_metal", (0.055, 0.065, 0.064, 1.0), metallic=0.35, roughness=0.32),
        "stone": _material("soft_stone", (0.72, 0.70, 0.65, 1.0), roughness=0.62),
        "glass": _material("sky_glass", (0.20, 0.46, 0.61, 0.42), metallic=0.05, roughness=0.18),
        "plant": _material("leaf_green", (0.08, 0.30, 0.13, 1.0), roughness=0.80),
        "plant_dark": _material("stem_green", (0.04, 0.16, 0.06, 1.0), roughness=0.86),
        "terracotta": _material("terracotta", (0.52, 0.18, 0.08, 1.0), roughness=0.82),
        "brass": _material("brushed_brass", (0.62, 0.40, 0.08, 1.0), metallic=0.78, roughness=0.26),
        "blue": _material("danube_blue", (0.08, 0.22, 0.34, 1.0), roughness=0.68),
    }

    _box("floor_slab", (0.0, 0.0, 0.05), (12.0, 8.0, 0.10), materials["oak_light"], bevel=0.08)
    _box("left_wall", (-5.92, 0.0, 1.55), (0.16, 8.0, 3.10), materials["plaster"], bevel=0.02)
    _box("right_wall", (5.92, 1.65, 1.55), (0.16, 4.70, 3.10), materials["plaster"], bevel=0.02)
    _box("window_sill", (0.0, 3.92, 0.35), (12.0, 0.16, 0.70), materials["plaster"], bevel=0.02)
    _box("window_lintel", (0.0, 3.92, 2.85), (12.0, 0.16, 0.50), materials["plaster"], bevel=0.02)
    for index, x in enumerate((-5.8, -3.0, 0.0, 3.0, 5.8), start=1):
        _box(f"window_mullion_{index}", (x, 3.84, 1.62), (0.12, 0.12, 2.20), materials["charcoal"], bevel=0.015)
    for index, x in enumerate((-4.4, -1.5, 1.5, 4.4), start=1):
        _box(f"window_glass_{index}", (x, 3.90, 1.62), (2.72, 0.045, 2.18), materials["glass"], bevel=0.0)

    _box("bedroom_partition", (2.25, 1.65, 1.55), (0.14, 4.50, 3.10), materials["plaster"], bevel=0.02)
    _box("hall_partition", (4.15, -0.55, 1.55), (3.80, 0.14, 3.10), materials["plaster"], bevel=0.02)

    _box("living_rug", (-1.8, -0.9, 0.12), (4.15, 2.65, 0.07), materials["blue"], bevel=0.05)
    _box("sofa_base", (-2.45, -0.35, 0.52), (3.15, 0.95, 0.55), materials["linen"], bevel=0.16)
    _box("sofa_back", (-2.45, 0.03, 1.10), (3.15, 0.24, 1.05), materials["linen"], bevel=0.14)
    _box("sofa_arm_left", (-4.02, -0.35, 0.82), (0.22, 0.96, 0.85), materials["linen"], bevel=0.10)
    _box("sofa_arm_right", (-0.88, -0.35, 0.82), (0.22, 0.96, 0.85), materials["linen"], bevel=0.10)
    for index, x in enumerate((-3.35, -2.45, -1.55), start=1):
        _box(f"sofa_cushion_{index}", (x, -0.42, 0.84), (0.80, 0.72, 0.20), materials["linen_light"], bevel=0.10)
    _box("coffee_table_top", (-2.1, -1.75, 0.58), (2.15, 0.92, 0.13), materials["oak"], bevel=0.08)
    for index, (x, y) in enumerate(((-2.95, -2.04), (-1.25, -2.04), (-2.95, -1.46), (-1.25, -1.46)), start=1):
        _box(f"coffee_table_leg_{index}", (x, y, 0.34), (0.09, 0.09, 0.48), materials["charcoal"], bevel=0.02)

    _box("kitchen_run", (-3.05, 3.15, 1.05), (5.25, 0.65, 2.05), materials["charcoal"], bevel=0.06)
    _box("kitchen_counter", (-3.05, 2.77, 1.05), (5.30, 0.78, 0.12), materials["stone"], bevel=0.03)
    _box("kitchen_island", (-1.25, 1.85, 0.68), (2.70, 1.05, 1.25), materials["oak"], bevel=0.08)
    _box("kitchen_island_top", (-1.25, 1.85, 1.34), (2.88, 1.17, 0.10), materials["stone"], bevel=0.04)

    _box("dining_table", (-4.20, -2.70, 0.78), (2.25, 1.30, 0.14), materials["oak"], bevel=0.08)
    for index, (x, y) in enumerate(((-5.05, -3.15), (-3.35, -3.15), (-5.05, -2.25), (-3.35, -2.25)), start=1):
        _box(f"dining_table_leg_{index}", (x, y, 0.41), (0.09, 0.09, 0.74), materials["charcoal"], bevel=0.02)
    _chair("dining_chair_1", -5.35, -2.70, math.radians(-90), materials)
    _chair("dining_chair_2", -3.05, -2.70, math.radians(90), materials)

    _box("bed_frame", (4.00, 1.65, 0.38), (2.70, 3.65, 0.45), materials["oak"], bevel=0.10)
    _box("mattress", (4.00, 1.65, 0.72), (2.55, 3.45, 0.36), materials["linen_light"], bevel=0.15)
    _box("headboard", (4.00, 3.25, 1.25), (2.70, 0.20, 1.55), materials["linen"], bevel=0.10)
    _box("pillow_left", (3.43, 2.60, 1.03), (0.85, 0.65, 0.20), materials["linen_light"], bevel=0.10)
    _box("pillow_right", (4.57, 2.60, 1.03), (0.85, 0.65, 0.20), materials["linen_light"], bevel=0.10)
    _box("bed_throw", (4.00, 0.55, 0.95), (2.45, 0.95, 0.10), materials["blue"], bevel=0.04)
    _box("wardrobe", (5.45, 1.70, 1.40), (0.72, 2.45, 2.70), materials["oak"], bevel=0.06)

    _plant("living_plant", 0.35, -2.75, 1.05, materials)
    _plant("window_plant", 1.25, 3.10, 0.78, materials)
    _plant("bedroom_plant", 5.15, 3.10, 0.68, materials)
    _cylinder("pendant_1", (-4.20, -2.70, 2.35), 0.26, 0.26, materials["brass"])
    _cylinder("pendant_2", (-1.25, 1.85, 2.38), 0.22, 0.30, materials["brass"])

    mesh_objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    return {
        "mesh_objects": mesh_objects,
        "object_count": len(mesh_objects),
        "material_count": len(materials),
        "dimensions_m": {"width": 12.0, "depth": 8.0, "height": 3.1},
    }


def _aim(obj, target: tuple[float, float, float]) -> None:
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def _render_preview(path: Path) -> None:
    bpy.ops.object.camera_add(location=(13.5, -15.5, 12.0))
    camera = bpy.context.object
    _aim(camera, (0.0, 0.2, 0.7))
    camera.data.lens = 52
    bpy.context.scene.camera = camera
    for name, location, energy, size in (
        ("key", (-7.0, -8.0, 12.0), 1700.0, 7.0),
        ("fill", (8.0, -1.0, 9.0), 1100.0, 6.0),
        ("window", (0.0, 8.0, 10.0), 1500.0, 8.0),
    ):
        bpy.ops.object.light_add(type="AREA", location=location)
        light = bpy.context.object
        light.name = f"preview_{name}"
        light.data.energy = energy
        light.data.shape = "DISK"
        light.data.size = size
        _aim(light, (0.0, 0.0, 0.5))
    scene = bpy.context.scene
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except TypeError:
        scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 960
    scene.render.resolution_y = 720
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(path)
    scene.world.color = (0.035, 0.035, 0.035)
    bpy.ops.render.render(write_still=True)


def main() -> int:
    args = _args()
    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    built = _build_scene()
    if args.preview:
        preview_path = Path(args.preview).expanduser().resolve()
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        _render_preview(preview_path)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in built["mesh_objects"]:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = built["mesh_objects"][0]
    bpy.ops.export_scene.gltf(
        filepath=str(out_path),
        export_format="GLB",
        use_selection=True,
        export_yup=True,
        export_apply=True,
    )
    payload = {
        "schema": "propertyquarry.omagic_showcase_model.v1",
        "status": "generated",
        "claim": "property-specific generated reconstruction; not a measured scan",
        "source_context": "Danube Flats photo/floorplan-derived PropertyQuarry demo geometry",
        "output_path": str(out_path),
        "size_bytes": out_path.stat().st_size,
        "sha256": hashlib.sha256(out_path.read_bytes()).hexdigest(),
        "object_count": built["object_count"],
        "material_count": built["material_count"],
        "dimensions_m": built["dimensions_m"],
    }
    if args.receipt:
        receipt_path = Path(args.receipt).expanduser().resolve()
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
