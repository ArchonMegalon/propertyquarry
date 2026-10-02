#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ea"))
from app.services.teable_projection_adapter import build_teable_projection_summary


COMPLETION_DIR = Path("/docker/chummercomplete/_completion/ltd_capability_mesh_v2")


def main() -> int:
    summary = build_teable_projection_summary()
    failures: list[str] = []
    table_names = {table["table_name"] for table in summary["tables"]}
    expected = {
        "product_signals",
        "black_ledger_dispatches",
        "tick_news_delivery",
        "package_pressure",
        "ltd_adapter_readiness",
    }
    missing = sorted(expected - table_names)
    if missing:
        failures.append(f"missing_tables:{','.join(missing)}")
    receipt = {
        "contract_name": "ea.verify_teable_projection_adapter",
        "status": "pass" if not failures else "fail",
        "summary": summary,
        "failures": failures,
    }
    COMPLETION_DIR.mkdir(parents=True, exist_ok=True)
    (COMPLETION_DIR / "VERIFY_TEABLE_PROJECTION_ADAPTER.generated.json").write_text(
        json.dumps(receipt, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
