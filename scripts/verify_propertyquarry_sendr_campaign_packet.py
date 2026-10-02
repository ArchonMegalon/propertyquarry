#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
for candidate in (ROOT, EA_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app.services.property_outreach_policy import validate_propertyquarry_sendr_campaign_packet  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a PropertyQuarry Sendr campaign packet.")
    parser.add_argument("--packet", required=True)
    parser.add_argument("--write", default="")
    args = parser.parse_args()

    packet_path = Path(args.packet)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    if not isinstance(packet, dict):
        raise SystemExit("packet file must contain a JSON object")

    validation = validate_propertyquarry_sendr_campaign_packet(packet)
    output = json.dumps(validation, indent=2, sort_keys=True)
    if args.write:
        Path(args.write).parent.mkdir(parents=True, exist_ok=True)
        Path(args.write).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0 if validation["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
