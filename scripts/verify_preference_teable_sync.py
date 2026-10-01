#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ea"))

from app.container import build_container  # type: ignore
from app.product.service import build_product_service  # type: ignore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or execute preference-profile Teable sync.")
    parser.add_argument("--principal-id", required=True)
    parser.add_argument("--person-id", default="self")
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    container = build_container()
    service = build_product_service(container)
    if args.execute:
        receipt = service.request_preference_teable_sync(
            principal_id=str(args.principal_id or "").strip(),
            person_id=str(args.person_id or "").strip() or "self",
        )
    else:
        receipt = service.preference_teable_sync_preview(
            principal_id=str(args.principal_id or "").strip(),
            person_id=str(args.person_id or "").strip() or "self",
        )
    print(json.dumps(receipt, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
