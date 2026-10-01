#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse


FORBIDDEN_TOUR_MARKERS = ("cubeviewer", "marzipano", "#cube", "fallback", "dummy")
ALLOWED_TOUR_HINTS = ("matterport", "3dvista")


def _load_items(path: Path) -> list[dict[str, object]]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, list):
        raise RuntimeError("sent_links_manifest_must_be_list")
    return [dict(item) for item in parsed if isinstance(item, dict)]


def verify_sent_links_manifest(path: Path) -> dict[str, object]:
    items = _load_items(path)
    failures: list[str] = []
    for index, item in enumerate(items):
        label = str(item.get("title") or item.get("key") or f"item_{index}").strip()
        for key in ("tour_url", "direct_tour_url"):
            url = str(item.get(key) or "").strip()
            if not url:
                if key == "tour_url":
                    failures.append(f"{label}:{key}:missing")
                continue
            parsed = urlparse(url)
            normalized = url.lower()
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                failures.append(f"{label}:{key}:invalid_url")
            if any(marker in normalized for marker in FORBIDDEN_TOUR_MARKERS):
                failures.append(f"{label}:{key}:forbidden_fallback_marker")
            if key == "direct_tour_url" and not any(hint in normalized for hint in ALLOWED_TOUR_HINTS):
                failures.append(f"{label}:{key}:missing_matterport_or_3dvista_hint")
        flythrough = str(item.get("flythrough_url") or item.get("direct_flythrough_url") or "").strip()
        if not flythrough:
            failures.append(f"{label}:flythrough_url:missing")
    return {
        "status": "passed" if not failures else "failed",
        "items_total": len(items),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify PropertyQuarry sent-link manifest artifact contracts.")
    parser.add_argument("manifest", help="Path to a sent-links manifest JSON file.")
    args = parser.parse_args()
    receipt = verify_sent_links_manifest(Path(args.manifest))
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
