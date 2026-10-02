#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "newsroom_editorial_packet.py"
OUTPUT_PATH = ROOT / ".codex-studio" / "published" / "NEWSROOM_EDITORIAL_PACKET.generated.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("newsroom_editorial_packet", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable_to_load:{SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if not OUTPUT_PATH.is_file():
        print(f"missing_output:{OUTPUT_PATH}", file=sys.stderr)
        return 1
    output = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    module = _load_module()
    rebuilt = module.build_payload()
    output_without_time = {key: value for key, value in dict(output).items() if key != "generated_at"}
    rebuilt_without_time = {key: value for key, value in dict(rebuilt).items() if key != "generated_at"}
    errors: list[str] = []
    if output_without_time != rebuilt_without_time:
        errors.append("packet_drifted_from_builder")
    if output.get("episode", {}).get("status") != "editorial_ready":
        errors.append("episode_not_editorial_ready")
    if len(output.get("segments") or []) < 3:
        errors.append("segment_count_too_small")
    if not output.get("anchor", {}).get("host_performance_prompt"):
        errors.append("anchor_prompt_missing")
    if "source_receipts" not in (output.get("watch_page_contract") or {}).get("required_sections", []):
        errors.append("watch_page_contract_missing_source_receipts")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("ok: newsroom editorial packet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
