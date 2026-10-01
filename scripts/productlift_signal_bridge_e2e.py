#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ea"))
from app.services.productlift_signal_adapter import build_product_signal_bridge_dry_run


COMPLETION_DIR = Path("/docker/chummercomplete/_completion/ltd_capability_mesh_v2")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run the Chummer public signal mirror bridge.")
    parser.add_argument("--dry-run", action="store_true", help="Required flag for the MVP dry-run contract.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dry_run:
        raise SystemExit("use --dry-run for this MVP contract")
    payload = build_product_signal_bridge_dry_run()
    receipt = {
        "contract_name": "ea.productlift_signal_bridge_e2e",
        "status": "pass",
        "dry_run": True,
        "payload": payload,
    }
    COMPLETION_DIR.mkdir(parents=True, exist_ok=True)
    (COMPLETION_DIR / "PRODUCTLIFT_SIGNAL_BRIDGE_DRY_RUN.generated.json").write_text(
        json.dumps(receipt, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
