#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


OUT_DIR = Path("/docker/chummercomplete/_completion/magicfit_provider")
RENDER_PATH = OUT_DIR / "MAGICFIT_SAMPLE_RENDER_RECEIPT.generated.json"
OUT_PATH = OUT_DIR / "MAGICFIT_PUBLIC_SAFETY.generated.json"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    receipt = json.loads(RENDER_PATH.read_text(encoding="utf-8"))
    rendered = int(receipt.get("rendered_clip_count", 0))
    payload = {
        "generated_at": utc_now(),
        "status": "fail" if rendered < 4 else "pass",
        "provider": "MagicFit",
        "rendered_clip_count": rendered,
        "policy": {
            "candidate_asset_only": True,
            "direct_publish_forbidden": True,
            "editorial_truth_forbidden": True,
            "product_behavior_proof_forbidden": True,
            "private_campaign_data_forbidden": True,
            "sourcebook_text_forbidden": True,
            "unreviewed_public_videos_forbidden": True,
        },
        "reasons": [
            "No private campaign data or sourcebook text was sent during this pass.",
            "No rendered clips exist yet, so public-safe output is not proven.",
        ],
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
