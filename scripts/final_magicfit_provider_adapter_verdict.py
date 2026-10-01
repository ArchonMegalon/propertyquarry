#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path("/docker/chummercomplete/_completion/magicfit_provider")
VERDICT_PATH = ROOT / "FINAL_MAGICFIT_PROVIDER_ADAPTER_VERDICT.md"


def load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def main() -> int:
    provider = load("MAGICFIT_PROVIDER_VERIFICATION.generated.json")
    render = load("MAGICFIT_SAMPLE_RENDER_RECEIPT.generated.json")
    motion = load("MAGICFIT_MOTION_SCORE.generated.json")
    people = load("MAGICFIT_PEOPLE_ACTION_SCORE.generated.json")
    safety = load("MAGICFIT_PUBLIC_SAFETY.generated.json")
    boundary = load("MAGICFIT_DESIGN_BOUNDARY.generated.json")
    review = ROOT / "MAGICFIT_HUMAN_CREATIVE_REVIEW.md"

    ready = (
        boundary.get("status") == "pass"
        and provider.get("status") == "verified"
        and render.get("status") == "pass"
        and render.get("rendered_clip_count", 0) >= 4
        and render.get("downloadable_mp4_verified") is True
        and render.get("watermark_present") is False
        and render.get("commercial_use_reviewed") is True
        and render.get("source_receipt_association") == "verified"
        and motion.get("status") == "pass"
        and people.get("status") == "pass"
        and safety.get("status") == "pass"
        and review.is_file()
        and "PASS" in review.read_text(encoding="utf-8")
    )
    VERDICT_PATH.write_text(
        "MAGICFIT_PROVIDER_ADAPTER_READY\n" if ready else "NOT_READY\n",
        encoding="utf-8",
    )
    print(VERDICT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
