#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


EA_ROOT = Path("/docker/EA")
DESIGN_ROOT = Path("/docker/chummercomplete/chummer-design/products/chummer")
COMPLETION_DIR = Path("/docker/chummercomplete/_completion/ltd_capability_mesh_v2")


def main() -> int:
    required_design = [
        "LTD_CAPABILITY_MESH_OPERATING_MODEL.md",
        "LTD_ADAPTER_PUBLIC_COPY_POLICY.md",
        "PRODUCTLIFT_PUBLIC_SIGNAL_MIRROR_SPEC.md",
        "PUBLIC_FEEDBACK_AND_SIGNAL_BOARD_SPEC.md",
        "PRODUCT_SIGNAL_RECEIPT_MODEL.yaml",
        "TEABLE_PROJECTION_SURFACE_SPEC.md",
        "TEABLE_PROJECTION_TABLES.yaml",
        "OPERATOR_VOICE_CAPTURE_SPEC.md",
    ]
    required_ea = [
        "ea/app/templates/blip_operator_capture_to_chummer_packet.md",
        "ea/app/templates/black_ledger_dispatch_from_tick.md",
        "ea/app/services/dispatch_draft_adapters/syllabbles_adapter.py",
        "ea/app/services/teable_projection_adapter.py",
        "ea/app/services/productlift_signal_adapter.py",
        "ea/app/services/operator_voice_capture_adapter.py",
        "scripts/productlift_signal_bridge_e2e.py",
        "scripts/sync_chummer_projection_to_teable.py",
        "scripts/verify_teable_projection_adapter.py",
        "tests/test_syllabbles_dispatch_draft_lane.py",
        "tests/test_blip_operator_capture_packet.py",
    ]
    failures: list[str] = []
    for rel in required_design:
        if not (DESIGN_ROOT / rel).is_file():
            failures.append(f"missing_design:{rel}")
    for rel in required_ea:
        if not (EA_ROOT / rel).is_file():
            failures.append(f"missing_ea:{rel}")
    receipt = {
        "contract_name": "ea.verify_ltd_capability_mesh",
        "status": "pass" if not failures else "fail",
        "failure_count": len(failures),
        "failures": failures,
    }
    COMPLETION_DIR.mkdir(parents=True, exist_ok=True)
    (COMPLETION_DIR / "LTD_CAPABILITY_MESH_REPORT.md").write_text(
        "# LTD Capability Mesh Report\n\n"
        f"- Status: `{receipt['status']}`\n"
        f"- Failure count: `{receipt['failure_count']}`\n",
        encoding="utf-8",
    )
    (COMPLETION_DIR / "FINAL_LTD_MESH_VERDICT.md").write_text(
        "pass\n" if not failures else ("fail\n" + "\n".join(failures) + "\n"),
        encoding="utf-8",
    )
    print(json.dumps(receipt, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
