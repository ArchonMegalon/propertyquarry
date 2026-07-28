from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_design_mirror_bundle.py"
REPAIR_SCRIPT = ROOT / "scripts" / "repair_design_mirror_bundle.sh"


def _isolated_design_mirror_verifier(tmp_path: Path) -> tuple[ModuleType, Path, Path, Path]:
    workspace_root = tmp_path / "workspace"
    local_queue = workspace_root / ".codex-design" / "product" / "NEXT_90_DAY_QUEUE_STAGING.generated.yaml"
    queue_overlay = workspace_root / ".codex-studio" / "published" / "QUEUE.generated.yaml"
    source_root = tmp_path / "source"
    source_queue = source_root / "NEXT_90_DAY_QUEUE_STAGING.generated.yaml"

    local_queue.parent.mkdir(parents=True, exist_ok=True)
    queue_overlay.parent.mkdir(parents=True, exist_ok=True)
    source_queue.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        ROOT / ".codex-design" / "product" / "NEXT_90_DAY_QUEUE_STAGING.generated.yaml",
        local_queue,
    )
    shutil.copy2(ROOT / ".codex-studio" / "published" / "QUEUE.generated.yaml", queue_overlay)
    shutil.copy2(
        Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_QUEUE_STAGING.generated.yaml"),
        source_queue,
    )

    module_name = "propertyquarry_design_mirror_test_verifier"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.ROOT = workspace_root
    module.LOCAL_PRODUCT_ROOT = local_queue.parent
    module.DEFAULT_DESIGN_ROOT = source_root
    module.QUEUE_OVERLAY_PATH = queue_overlay
    return module, local_queue, source_queue, queue_overlay


def test_design_mirror_bundle_bindings_cover_the_audited_queue_slice() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert {row["status"] for row in payload} == {"ok"}
    keys = {row["key"] for row in payload}
    assert keys == {
        "next_90_day_queue_staging",
        "published_queue_overlay",
    }
    queue_row = next(row for row in payload if row["key"] == "next_90_day_queue_staging")
    assert queue_row["local_path"].endswith(".codex-design/product/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
    assert queue_row["source_path"] == "/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_QUEUE_STAGING.generated.yaml"
    assert int(queue_row["local_item_count"]) > 0
    assert int(queue_row["source_item_count"]) > 0
    overlay_row = next(row for row in payload if row["key"] == "published_queue_overlay")
    assert overlay_row["local_path"].endswith(".codex-studio/published/QUEUE.generated.yaml")
    assert overlay_row["source_items"] == [
        "/docker/EA/.codex-design/product/NEXT_90_DAY_QUEUE_STAGING.generated.yaml",
    ]


def test_repair_design_mirror_bundle_help_mentions_bounded_bundle() -> None:
    completed = subprocess.run(
        ["bash", str(REPAIR_SCRIPT), "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "bounded EA design-mirror bundle" in completed.stdout


def test_release_assets_guard_wires_design_mirror_bundle_verifier() -> None:
    script = (ROOT / "scripts" / "verify_release_assets.sh").read_text(encoding="utf-8")
    assert "scripts/verify_design_mirror_bundle.py" in script
    assert "scripts/repair_design_mirror_bundle.sh" in script
    assert "ok: bounded design mirror bundle parity" in script


def test_makefile_exposes_design_mirror_bundle_targets() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "verify-design-mirror-bundle:" in makefile
    assert "repair-design-mirror-bundle:" in makefile


def test_verify_design_mirror_bundle_normalizes_dynamic_repeated_audit_count() -> None:
    payload = yaml.safe_load((ROOT / ".codex-studio" / "published" / "QUEUE.generated.yaml").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    items = payload.get("items") or []
    assert isinstance(items, list) and items
    item = items[0]
    assert "repeated audit observations" not in str(item.get("title") or "")
    assert "repeated audit observations" not in str(item.get("task") or "")


def test_repair_design_mirror_bundle_restores_drifted_queue_staging(tmp_path) -> None:
    verifier, local_queue, source_queue, _queue_overlay = _isolated_design_mirror_verifier(tmp_path)
    local_queue.write_text("mode: append\nitems: []\n", encoding="utf-8")

    failed_row = next(row for row in verifier.inspect_bundle() if row["key"] == "next_90_day_queue_staging")
    assert failed_row["status"] == "invalid_local_payload"

    verifier.repair_bundle()
    repaired_row = next(row for row in verifier.inspect_bundle() if row["key"] == "next_90_day_queue_staging")
    assert repaired_row["status"] == "ok"
    assert local_queue.read_text(encoding="utf-8") == source_queue.read_text(encoding="utf-8")


def test_repair_design_mirror_bundle_restores_drifted_queue_overlay_source_items(tmp_path) -> None:
    verifier, _local_queue, _source_queue, queue_overlay = _isolated_design_mirror_verifier(tmp_path)
    payload = yaml.safe_load(queue_overlay.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    items = payload.get("items") or []
    assert isinstance(items, list) and items
    items[0]["source_items"] = ["/docker/EA/.codex-design/product/NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml"]
    queue_overlay.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    failed_row = verifier.inspect_queue_overlay()
    assert failed_row["status"] == "queue_drift"
    assert "source_items" in failed_row["mismatches"]

    verifier.repair_bundle()
    assert verifier.inspect_queue_overlay()["status"] == "ok"
    repaired_payload = yaml.safe_load(queue_overlay.read_text(encoding="utf-8"))
    repaired_items = repaired_payload.get("items") or []
    assert repaired_items[0]["source_items"] == [
        "/docker/EA/.codex-design/product/NEXT_90_DAY_QUEUE_STAGING.generated.yaml",
    ]
