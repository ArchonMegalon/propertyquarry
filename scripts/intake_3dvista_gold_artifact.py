#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SLUG = "luxury-residence-with-breathtaking-skyline-views-danubeflats-vienna-layout-first-742df65557"
DEFAULT_DROP_DIR = ROOT / "state" / "incoming_property_tours"
DEFAULT_PUBLIC_TOUR_DIR = ROOT / "state" / "public_property_tours"
DEFAULT_COMPLETION_DIR = ROOT / "_completion" / "artifact_intake"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "unreadable", "error": f"{type(exc).__name__}: {exc}"}
    return dict(payload) if isinstance(payload, dict) else {"status": "invalid_payload"}


def _run_command(cmd: list[str], *, timeout_seconds: int) -> dict[str, Any]:
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "cmd": _redacted_cmd(cmd),
        "returncode": completed.returncode,
        "stdout_tail": "\n".join((completed.stdout or "").splitlines()[-20:]),
        "stderr_tail": "\n".join((completed.stderr or "").splitlines()[-20:]),
    }


def _redacted_cmd(cmd: list[str]) -> list[str]:
    # Paths and slugs are operational evidence; no credentials are passed here.
    return [str(item) for item in cmd]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def build_3dvista_intake_receipt(
    *,
    drop_dir: Path = DEFAULT_DROP_DIR,
    public_tour_dir: Path = DEFAULT_PUBLIC_TOUR_DIR,
    slug: str = DEFAULT_SLUG,
    completion_dir: Path = DEFAULT_COMPLETION_DIR,
    timeout_seconds: int = 180,
    dry_run: bool = False,
) -> dict[str, Any]:
    completion_dir = completion_dir.expanduser().resolve()
    discovery_path = completion_dir / "3dvista-gold-discovery.json"
    import_manifest_path = completion_dir / "3dvista-gold-import-manifest.json"
    import_receipt_path = completion_dir / "3dvista-gold-import.json"
    tour_controls_path = completion_dir / "3dvista-gold-tour-controls.json"
    gold_status_path = completion_dir / "3dvista-gold-status.json"

    receipt: dict[str, Any] = {
        "status": "blocked_waiting_for_artifact",
        "generated_at": _now_iso(),
        "slug": str(slug),
        "drop_dir": str(drop_dir.expanduser()),
        "expected_export_dir": str((drop_dir / slug / "3dvista").expanduser()),
        "public_tour_dir": str(public_tour_dir.expanduser()),
        "dry_run": bool(dry_run),
        "steps": [],
        "artifact_requirements": [
            "licensed non-trial 3DVista VT Pro export",
            "PropertyQuarry-owned tour metadata",
            "tdvplayer/tdvplayerapi/tourviewer runtime evidence",
            "no 3DVista trial branding",
            "private propertyquarry.3dvista_target_provenance.v1 receipt binding the exact slug and export bytes to approved reuse and dated human property/visual match",
        ],
    }

    discovery_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "discover_property_tour_exports.py"),
        "--drop-dir",
        str(drop_dir),
        "--public-tour-dir",
        str(public_tour_dir),
        "--write",
        str(discovery_path),
        "--manifest-write",
        str(import_manifest_path),
    ]
    discovery_result = _run_command(discovery_cmd, timeout_seconds=timeout_seconds)
    receipt["steps"].append({"name": "discover", **discovery_result, "receipt_path": str(discovery_path)})
    discovery = _load_json(discovery_path)
    receipt["discovery_status"] = discovery.get("status")
    receipt["total_import_count"] = int(discovery.get("import_count") or 0)
    receipt["rejected_count"] = int(discovery.get("rejected_count") or 0)
    receipt["rejected_3dvista_reasons"] = [
        {
            "slug": row.get("slug"),
            "reason": row.get("reason"),
            "action": row.get("action"),
            "drop_path": row.get("drop_path"),
        }
        for row in list(discovery.get("rejected") or [])
        if (
            isinstance(row, dict)
            and str(row.get("provider") or "").strip().lower() == "3dvista"
            and str(row.get("slug") or "").strip() == str(slug).strip()
        )
    ]

    import_rows = [
        row
        for row in list((discovery.get("import_manifest") or {}).get("imports") or [])
        if (
            isinstance(row, dict)
            and str(row.get("provider") or "").strip().lower() == "3dvista"
            and str(row.get("slug") or "").strip() == str(slug).strip()
        )
    ]
    receipt["3dvista_import_count"] = len(import_rows)
    receipt["ignored_non_target_import_count"] = max(int(discovery.get("import_count") or 0) - len(import_rows), 0)
    _write_json(import_manifest_path, {"imports": import_rows})
    if not import_rows:
        receipt["next_action"] = (
            "Copy a licensed non-trial 3DVista export plus 3dvista-target-provenance.json "
            "into the expected export dir. Use create_3dvista_provenance_template.py to compute the "
            "exact export hash, complete its authorization/property/visual review, then rerun this script."
        )
        return receipt

    if dry_run:
        receipt["status"] = "ready_to_import"
        receipt["next_action"] = "Rerun without --dry-run to import verified 3DVista rows."
        return receipt

    import_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "import_property_tour_exports.py"),
        "--manifest",
        str(import_manifest_path),
        "--public-tour-dir",
        str(public_tour_dir),
        "--write",
        str(import_receipt_path),
    ]
    import_result = _run_command(import_cmd, timeout_seconds=timeout_seconds)
    receipt["steps"].append({"name": "import", **import_result, "receipt_path": str(import_receipt_path)})
    import_receipt = _load_json(import_receipt_path)
    receipt["import_status"] = import_receipt.get("status")
    receipt["imported_count"] = int(import_receipt.get("imported_count") or 0)

    controls_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "verify_property_tour_controls.py"),
        "--tour-root",
        str(public_tour_dir),
        "--require-all-provider-modes",
        "--write",
        str(tour_controls_path),
    ]
    controls_result = _run_command(controls_cmd, timeout_seconds=timeout_seconds)
    receipt["steps"].append({"name": "tour_controls", **controls_result, "receipt_path": str(tour_controls_path)})
    controls = _load_json(tour_controls_path)
    receipt["tour_controls_status"] = controls.get("status")
    receipt["ready_provider_modes"] = controls.get("ready_provider_modes") if isinstance(controls.get("ready_provider_modes"), list) else []
    receipt["missing_provider_modes"] = controls.get("missing_provider_modes") if isinstance(controls.get("missing_provider_modes"), list) else []

    gold_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "propertyquarry_gold_status.py"),
        "--write",
        str(gold_status_path),
    ]
    gold_result = _run_command(gold_cmd, timeout_seconds=timeout_seconds)
    receipt["steps"].append({"name": "gold_status", **gold_result, "receipt_path": str(gold_status_path)})
    gold = _load_json(gold_status_path)
    receipt["gold_status"] = gold.get("status")
    receipt["gold_blockers"] = gold.get("blockers") if isinstance(gold.get("blockers"), list) else []

    if "3dvista" in set(str(item) for item in receipt.get("ready_provider_modes") or []):
        receipt["status"] = "imported_verified_3dvista"
        receipt["next_action"] = "Review refreshed gold status and deploy if runtime state changed."
    else:
        receipt["status"] = "import_attempted_but_3dvista_not_verified"
        receipt["next_action"] = "Inspect import/tour-control receipts; replace the export if trial branding or runtime markers are still rejected."
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover, import, and verify the missing PropertyQuarry 3DVista gold artifact.")
    parser.add_argument("--drop-dir", default=str(DEFAULT_DROP_DIR))
    parser.add_argument("--public-tour-dir", default=str(DEFAULT_PUBLIC_TOUR_DIR))
    parser.add_argument("--slug", default=DEFAULT_SLUG)
    parser.add_argument("--completion-dir", default=str(DEFAULT_COMPLETION_DIR))
    parser.add_argument("--write", default="")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    receipt = build_3dvista_intake_receipt(
        drop_dir=Path(args.drop_dir),
        public_tour_dir=Path(args.public_tour_dir),
        slug=str(args.slug),
        completion_dir=Path(args.completion_dir),
        timeout_seconds=int(args.timeout_seconds),
        dry_run=bool(args.dry_run),
    )
    write_path = Path(args.write).expanduser() if str(args.write or "").strip() else Path(args.completion_dir) / "3dvista-gold-intake-current.json"
    _write_json(write_path, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if str(receipt.get("status")) in {"ready_to_import", "imported_verified_3dvista"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
