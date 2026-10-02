#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ea"))
from app.services.teable_projection_adapter import build_teable_projection_records, build_teable_projection_summary


COMPLETION_DIR = Path("/docker/chummercomplete/_completion/ltd_capability_mesh_v2")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run projection of Chummer operator surfaces into Teable.")
    parser.add_argument("--dry-run", action="store_true", help="Required MVP mode.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dry_run:
        raise SystemExit("use --dry-run for this MVP contract")
    receipt = {
        "contract_name": "ea.teable_projection_dry_run",
        "status": "pass",
        "dry_run": True,
        "summary": build_teable_projection_summary(),
        "records": build_teable_projection_records(),
    }
    COMPLETION_DIR.mkdir(parents=True, exist_ok=True)
    (COMPLETION_DIR / "TEABLE_PROJECTION_DRY_RUN.generated.json").write_text(
        json.dumps(receipt, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
