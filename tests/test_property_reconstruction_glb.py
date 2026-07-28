from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path

import pytest

from scripts import generate_property_reconstruction as reconstruction


def _write_sample_model(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    reconstruction._write_obj(
        target_dir,
        width_m=12.0,
        depth_m=8.0,
        height_m=2.8,
        wall_rectangles=[
            {
                "center_x": 1.0,
                "center_z": -1.0,
                "width": 2.0,
                "depth": 0.4,
                "rotation_y": 0.0,
            }
        ],
    )


def _decode_glb(path: Path) -> tuple[dict[str, object], bytes]:
    payload = path.read_bytes()
    magic, version, declared_length = struct.unpack_from("<III", payload, 0)
    assert magic == 0x46546C67
    assert version == 2
    assert declared_length == len(payload)

    json_length, json_type = struct.unpack_from("<II", payload, 12)
    assert json_type == 0x4E4F534A
    assert json_length % 4 == 0
    json_start = 20
    json_end = json_start + json_length
    document = json.loads(payload[json_start:json_end].rstrip(b" ").decode("utf-8"))

    binary_length, binary_type = struct.unpack_from("<II", payload, json_end)
    assert binary_type == 0x004E4942
    assert binary_length % 4 == 0
    binary_start = json_end + 8
    binary = payload[binary_start : binary_start + binary_length]
    assert binary_start + binary_length == len(payload)
    return document, binary


def _accessor_values(document: dict[str, object], binary: bytes, accessor_index: int) -> list[tuple[float | int, ...]]:
    accessors = document["accessors"]
    buffer_views = document["bufferViews"]
    assert isinstance(accessors, list)
    assert isinstance(buffer_views, list)
    accessor = accessors[accessor_index]
    assert isinstance(accessor, dict)
    buffer_view = buffer_views[int(accessor["bufferView"])]
    assert isinstance(buffer_view, dict)

    component_formats = {5123: "H", 5125: "I", 5126: "f"}
    component_counts = {"SCALAR": 1, "VEC3": 3}
    component_format = component_formats[int(accessor["componentType"])]
    component_count = component_counts[str(accessor["type"])]
    row_format = "<" + (component_format * component_count)
    row_size = struct.calcsize(row_format)
    start = int(buffer_view.get("byteOffset") or 0) + int(accessor.get("byteOffset") or 0)
    count = int(accessor["count"])
    return [struct.unpack_from(row_format, binary, start + (row_index * row_size)) for row_index in range(count)]


def test_direct_glb_writer_is_deterministic_and_has_no_blender_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    _write_sample_model(first_dir)
    _write_sample_model(second_dir)
    obj_before = (first_dir / "model.obj").read_bytes()
    mtl_before = (first_dir / "model.mtl").read_bytes()

    def unexpected_dependency(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("direct GLB generation must not resolve or invoke Blender")

    monkeypatch.setattr(reconstruction.shutil, "which", unexpected_dependency)
    monkeypatch.setattr(reconstruction.subprocess, "run", unexpected_dependency)

    first_result = reconstruction._write_glb(first_dir)
    second_result = reconstruction._write_glb(second_dir)
    first_payload = (first_dir / "model.glb").read_bytes()
    second_payload = (second_dir / "model.glb").read_bytes()

    assert first_payload == second_payload
    assert first_result == second_result
    assert set(first_result) == {
        "status",
        "exporter",
        "glb_relpath",
        "glb_sha256",
        "glb_size_bytes",
        "material_count",
        "source_obj_sha256",
        "triangle_count",
        "vertex_count",
    }
    assert first_result["status"] == "generated"
    assert first_result["exporter"] == reconstruction.GLB_EXPORTER_VERSION
    assert first_result["glb_relpath"] == "model.glb"
    assert first_result["glb_sha256"] == hashlib.sha256(first_payload).hexdigest()
    assert first_result["glb_size_bytes"] == len(first_payload)
    assert first_result["material_count"] == 2
    assert first_result["source_obj_sha256"] == hashlib.sha256(obj_before).hexdigest()
    assert int(first_result["triangle_count"]) > 0
    assert int(first_result["vertex_count"]) > 0
    assert (first_dir / "model.obj").read_bytes() == obj_before
    assert (first_dir / "model.mtl").read_bytes() == mtl_before


def test_direct_glb_writer_emits_aligned_indexed_y_up_material_primitives(tmp_path: Path) -> None:
    _write_sample_model(tmp_path)
    assert reconstruction._write_glb(tmp_path)["status"] == "generated"
    document, binary = _decode_glb(tmp_path / "model.glb")

    assert document["asset"] == {
        "generator": "PropertyQuarry deterministic GLB writer",
        "version": "2.0",
    }
    assert document["scene"] == 0
    assert document["scenes"] == [{"nodes": [0]}]
    assert document["nodes"] == [{"mesh": 0, "name": "propertyquarry_generated_layout"}]
    assert document["buffers"] == [{"byteLength": len(binary)}]

    buffer_views = document["bufferViews"]
    assert isinstance(buffer_views, list)
    for buffer_view in buffer_views:
        assert isinstance(buffer_view, dict)
        assert int(buffer_view["byteOffset"]) % 4 == 0
        assert int(buffer_view["byteOffset"]) + int(buffer_view["byteLength"]) <= len(binary)
        assert buffer_view["target"] in {34962, 34963}

    materials = document["materials"]
    assert isinstance(materials, list)
    assert [material["name"] for material in materials] == ["warm_floor", "warm_plaster"]
    assert all(material["doubleSided"] is True for material in materials)

    meshes = document["meshes"]
    assert isinstance(meshes, list)
    primitives = meshes[0]["primitives"]
    assert isinstance(primitives, list)
    assert [primitive["material"] for primitive in primitives] == [0, 1]

    all_positions: list[tuple[float | int, ...]] = []
    for primitive in primitives:
        assert primitive["mode"] == 4
        position_accessor_index = int(primitive["attributes"]["POSITION"])
        normal_accessor_index = int(primitive["attributes"]["NORMAL"])
        index_accessor_index = int(primitive["indices"])
        positions = _accessor_values(document, binary, position_accessor_index)
        normals = _accessor_values(document, binary, normal_accessor_index)
        indices = _accessor_values(document, binary, index_accessor_index)
        flat_indices = [int(row[0]) for row in indices]

        assert len(flat_indices) % 3 == 0
        assert min(flat_indices) == 0
        assert max(flat_indices) < len(positions)
        assert len(normals) == len(positions)
        assert all(
            math.isclose(math.sqrt(sum(float(value) ** 2 for value in normal)), 1.0, abs_tol=1e-6)
            for normal in normals
        )

        accessors = document["accessors"]
        assert isinstance(accessors, list)
        position_accessor = accessors[position_accessor_index]
        assert position_accessor["min"] == [min(row[axis] for row in positions) for axis in range(3)]
        assert position_accessor["max"] == [max(row[axis] for row in positions) for axis in range(3)]
        all_positions.extend(positions)

    assert [min(row[axis] for row in all_positions) for axis in range(3)] == [-6.0, 0.0, -4.0]
    assert [max(row[axis] for row in all_positions) for axis in range(3)] == pytest.approx([6.0, 2.8, 4.0])
    assert len(_accessor_values(document, binary, int(primitives[0]["indices"]))) == 6
    assert len(_accessor_values(document, binary, int(primitives[1]["indices"]))) == 36


def test_direct_glb_writer_removes_stale_output_on_invalid_source(tmp_path: Path) -> None:
    (tmp_path / "model.obj").write_text(
        "usemtl warm_floor\nf 1 2 3\n",
        encoding="utf-8",
    )
    stale = tmp_path / "model.glb"
    stale.write_bytes(b"stale-glb")

    result = reconstruction._write_glb(tmp_path)

    assert result["status"] == "failed"
    assert result["reason"] == "glb_export_failed"
    assert not stale.exists()
    assert not list(tmp_path.glob(".model.glb.*.tmp"))


def test_direct_glb_writer_removes_stale_output_on_float32_overflow(
    tmp_path: Path,
) -> None:
    (tmp_path / "model.obj").write_text(
        "\n".join(
            (
                "v 1e300 0 0",
                "v 0 1 0",
                "v 0 0 1",
                "usemtl warm_floor",
                "f 1 2 3",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    stale = tmp_path / "model.glb"
    stale.write_bytes(b"stale-glb")

    result = reconstruction._write_glb(tmp_path)

    assert result["status"] == "failed"
    assert result["reason"] == "glb_export_failed"
    assert not stale.exists()
    assert not list(tmp_path.glob(".model.glb.*.tmp"))


def test_direct_glb_writer_failure_receipt_never_reflects_local_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_sample_model(tmp_path)

    def denied(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"cannot publish {tmp_path}/operator-private/model.glb")

    monkeypatch.setattr(reconstruction.os, "replace", denied)

    result = reconstruction._write_glb(tmp_path)

    assert result == {
        "status": "failed",
        "reason": "glb_export_failed",
        "error_class": "OSError",
    }
    assert str(tmp_path) not in json.dumps(result, sort_keys=True)
