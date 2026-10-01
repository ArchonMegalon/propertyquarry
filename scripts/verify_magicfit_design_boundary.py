#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path("/docker/fleet/repos/chummer-media-factory")
DOC_PATH = ROOT / "docs" / "MAGICFIT_PROVIDER_BOUNDARY.md"
ADAPTER_PATH = ROOT / "src" / "Chummer.Media.Factory.Runtime" / "Providers" / "MagicFit" / "MagicFitProviderAdapter.cs"
OUT_DIR = Path("/docker/chummercomplete/_completion/magicfit_provider")
OUT_PATH = OUT_DIR / "MAGICFIT_DESIGN_BOUNDARY.generated.json"


def main() -> int:
    doc = DOC_PATH.read_text(encoding="utf-8")
    adapter = ADAPTER_PATH.read_text(encoding="utf-8")
    required_doc_tokens = [
        "Black Ledger Newsroom B-roll",
        "Faction promo scenes",
        "Short photoreal anchor tests",
        "Social video derivatives",
        "Text-to-video/image-to-video provider bake-off",
        "direct publish",
        "editorial truth",
        "product behavior proof",
        "private campaign data",
        "sourcebook text",
        "unreviewed public videos",
    ]
    required_adapter_tokens = [
        "DownloadAsync",
        "CandidateAssetOnly: true",
        "PublishAuthority: false",
        "may_publish_to_chummer_run: false",
        "may_send_email: false",
        "may_set_editorial_truth: false",
    ]
    missing = [token for token in required_doc_tokens if token not in doc]
    missing.extend(token for token in required_adapter_tokens if token not in adapter)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "status": "pass" if not missing else "fail",
        "missing_tokens": missing,
        "doc_path": str(DOC_PATH),
        "adapter_path": str(ADAPTER_PATH),
    }, indent=2) + "\n", encoding="utf-8")
    if missing:
        raise SystemExit("MagicFit design boundary is incomplete.")
    print(OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
