#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
for candidate in (ROOT, EA_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app.services.property_outreach_policy import materialize_propertyquarry_sendr_campaign_receipt  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize a local PropertyQuarry Sendr campaign receipt.")
    parser.add_argument("--packet", required=True)
    parser.add_argument("--write", default="")
    parser.add_argument("--reviewer", default="operator")
    parser.add_argument("--reviewed-at", default="")
    parser.add_argument("--max-contacts", type=int, default=50)
    args = parser.parse_args()

    packet_path = Path(args.packet)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    if not isinstance(packet, dict):
        raise SystemExit("packet file must contain a JSON object")

    receipt = materialize_propertyquarry_sendr_campaign_receipt(
        packet,
        reviewer=args.reviewer,
        reviewed_at=args.reviewed_at,
        max_contacts=args.max_contacts,
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    write_path = args.write
    if not write_path:
        completion_dir = Path(os.getenv("PROPERTYQUARRY_SENDR_COMPLETION_DIR") or "_completion/sendr")
        packet_id = str(packet.get("packet_id") or "campaign").replace("/", "_")
        write_path = str(completion_dir / f"propertyquarry_campaign_{packet_id}.generated.json")
    output_path = Path(write_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output + "\n", encoding="utf-8")
    print(str(output_path))
    return 0 if receipt["status"] == "pilot_approved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
