from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

import numpy
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("blender") is None, reason="blender is not installed")
def test_omagic_showcase_model_is_materially_rich_and_truthfully_labeled(tmp_path: Path) -> None:
    model_path = tmp_path / "showcase.glb"
    receipt_path = tmp_path / "showcase.json"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(
        Path(numpy.__file__).resolve().parents[1]
    )
    completed = subprocess.run(
        [
            "blender",
            "--background",
            "--factory-startup",
            "--python",
            str(ROOT / "scripts" / "build_propertyquarry_omagic_showcase_model.py"),
            "--",
            "--out",
            str(model_path),
            "--receipt",
            str(receipt_path),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    diagnostics = f"{completed.stderr}\n{completed.stdout}"[-4000:]
    assert completed.returncode == 0, diagnostics
    assert receipt_path.is_file(), diagnostics
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "generated"
    assert receipt["claim"] == "property-specific generated reconstruction; not a measured scan"
    assert receipt["object_count"] >= 50
    assert receipt["material_count"] >= 10
    assert receipt["size_bytes"] >= 20_000

    raw = model_path.read_bytes()
    magic, version, total_length = struct.unpack_from("<4sII", raw, 0)
    json_length, json_chunk_type = struct.unpack_from("<I4s", raw, 12)
    document = json.loads(raw[20 : 20 + json_length].decode("utf-8").rstrip(" \x00"))
    assert magic == b"glTF"
    assert version == 2
    assert total_length == len(raw)
    assert json_chunk_type == b"JSON"
    assert len(document.get("meshes") or []) >= 50
    assert len(document.get("materials") or []) >= 10
