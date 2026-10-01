#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / ".codex-design" / "repo" / "DESIGN_MIRROR_MANIFEST.yaml"


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inspect_binding(root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    local_path = root / str(binding.get("local_path") or "").strip()
    source_path = Path(str(binding.get("source_path") or "").strip())
    required = bool(binding.get("required", True))
    row: dict[str, Any] = {
        "key": str(binding.get("key") or "").strip(),
        "local_path": local_path.as_posix(),
        "source_path": source_path.as_posix(),
        "required": required,
        "status": "ok",
    }
    if not source_path.exists():
        if local_path.exists() and os.environ.get("EA_DESIGN_MIRROR_REQUIRE_SOURCE") != "1":
            row["source_unavailable"] = True
            row["local_sha256"] = _sha256(local_path)
        else:
            row["status"] = "missing_source"
        return row
    if not local_path.exists():
        row["status"] = "missing_local"
        return row
    row["local_sha256"] = _sha256(local_path)
    row["source_sha256"] = _sha256(source_path)
    if row["local_sha256"] != row["source_sha256"]:
        row["status"] = "drift"
    return row


def inspect_manifest(root: Path, manifest_path: Path) -> list[dict[str, Any]]:
    manifest = _load_manifest(manifest_path)
    bindings = list(manifest.get("bindings") or [])
    rows: list[dict[str, Any]] = []
    for binding in bindings:
        if isinstance(binding, dict):
            rows.append(_inspect_binding(root, binding))
    return rows


def repair_manifest(root: Path, manifest_path: Path) -> list[dict[str, Any]]:
    manifest = _load_manifest(manifest_path)
    bindings = list(manifest.get("bindings") or [])
    rows: list[dict[str, Any]] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        row = _inspect_binding(root, binding)
        status = str(row.get("status") or "")
        if status in {"ok", "missing_source"}:
            row["action"] = "unchanged" if status == "ok" else "blocked_missing_source"
            rows.append(row)
            continue
        local_path = root / str(binding.get("local_path") or "").strip()
        source_path = Path(str(binding.get("source_path") or "").strip())
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, local_path)
        repaired = _inspect_binding(root, binding)
        repaired["action"] = "copied"
        rows.append(repaired)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify or repair full design mirror parity from an explicit manifest.")
    parser.add_argument("--root", type=Path, default=ROOT, help="EA repository root.")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH, help="Mirror manifest path.")
    parser.add_argument("--repair", action="store_true", help="Repair drifted mirror files from their canonical sources.")
    parser.add_argument("--json", action="store_true", help="Print JSON output instead of a human summary.")
    args = parser.parse_args()

    root = args.root.resolve()
    manifest_path = args.manifest.resolve()
    rows = repair_manifest(root, manifest_path) if args.repair else inspect_manifest(root, manifest_path)
    bad = [row for row in rows if str(row.get("status") or "") != "ok"]
    if args.json:
        print(json.dumps({"status": "ok" if not bad else "failed", "items": rows}, indent=2))
    else:
        for row in rows:
            print(f"{row['status']}: {row['key']} -> {row['local_path']}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
