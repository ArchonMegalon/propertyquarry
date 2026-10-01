#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


OUT_DIR = Path("/docker/chummercomplete/_completion/magicfit_provider")
OUT_PATH = OUT_DIR / "MAGICFIT_SAMPLE_RENDER_RECEIPT.generated.json"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    payload = {
        "generated_at": utc_now(),
        "status": "fail",
        "provider": "MagicFit",
        "render_mode": "bakeoff_plan_only",
        "candidate_asset_only": True,
        "publish_authority": False,
        "downloadable_mp4_verified": False,
        "watermark_present": None,
        "commercial_use_reviewed": False,
        "credits_per_month_captured": 6000,
        "credit_costs_captured": False,
        "source_receipt_association": "missing",
        "rendered_clip_count": 0,
        "required_clip_count": 4,
        "samples": [
            {
                "sample_id": "photoreal_elf_anchor",
                "job_type": "photoreal_anchor_test",
                "prompt": "Photorealistic cyberpunk newsroom broadcast with believable elf anchor, restrained urgency, subtle gestures, no watermark.",
                "status": "not_rendered",
                "expected_output_files": ["mp4", "poster"],
            },
            {
                "sample_id": "photoreal_orc_field_reporter",
                "job_type": "photoreal_anchor_test",
                "prompt": "Photorealistic cyberpunk field report with believable orc reporter at night under rain-slick neon, no watermark.",
                "status": "not_rendered",
                "expected_output_files": ["mp4", "poster"],
            },
            {
                "sample_id": "research_facility_breach_broll",
                "job_type": "broll_scene",
                "prompt": "Public-safe photoreal reconstruction of rainy research facility breach exterior with drones and security lights.",
                "status": "not_rendered",
                "expected_output_files": ["mp4", "poster"],
            },
            {
                "sample_id": "rust_market_faction_scene",
                "job_type": "faction_promo_scene",
                "prompt": "Photoreal Rust Market faction scene at night with fixer and courier package exchange, crowd motion, public-safe.",
                "status": "not_rendered",
                "expected_output_files": ["mp4", "poster"],
            },
        ],
        "blocking_reasons": [
            "No authenticated provider execution lane or AppSumo-included API execution path is proven.",
            "No rendered MP4 sample clips were produced for Chummer review in this pass.",
        ],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
