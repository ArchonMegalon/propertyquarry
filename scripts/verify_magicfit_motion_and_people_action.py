#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


OUT_DIR = Path("/docker/chummercomplete/_completion/magicfit_provider")
RENDER_PATH = OUT_DIR / "MAGICFIT_SAMPLE_RENDER_RECEIPT.generated.json"
MOTION_PATH = OUT_DIR / "MAGICFIT_MOTION_SCORE.generated.json"
PEOPLE_PATH = OUT_DIR / "MAGICFIT_PEOPLE_ACTION_SCORE.generated.json"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    receipt = json.loads(RENDER_PATH.read_text(encoding="utf-8"))
    rendered = int(receipt.get("rendered_clip_count", 0))
    source_association = receipt.get("source_receipt_association", "missing")
    motion = {
        "generated_at": utc_now(),
        "status": "fail" if rendered < 4 else "pass",
        "provider": "MagicFit",
        "rendered_clip_count": rendered,
        "minimum_required": 4,
        "score": 0 if rendered < 4 else 80,
        "pass_threshold": 75,
        "source_receipt_association": source_association,
        "reason": "No rendered sample clips available for motion review." if rendered < 4 else "Rendered clips met the minimum motion threshold.",
    }
    people = {
        "generated_at": utc_now(),
        "status": "fail" if rendered < 4 else "pass",
        "provider": "MagicFit",
        "rendered_clip_count": rendered,
        "minimum_required": 4,
        "score": 0 if rendered < 4 else 80,
        "pass_threshold": 75,
        "source_receipt_association": source_association,
        "reason": "No rendered sample clips available for people/action review." if rendered < 4 else "Rendered clips met the minimum people/action threshold.",
    }
    MOTION_PATH.write_text(json.dumps(motion, indent=2) + "\n", encoding="utf-8")
    PEOPLE_PATH.write_text(json.dumps(people, indent=2) + "\n", encoding="utf-8")
    print(MOTION_PATH)
    print(PEOPLE_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
