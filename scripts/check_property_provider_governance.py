#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ea"))

from app.services.property_market_catalog import PROVIDERS, provider_governance


ALLOWED_ACCESS_MODES = {
    "official_api",
    "public_web",
    "browser_public_web",
    "manual_review",
    "partner_feed",
    "unknown",
}

ALLOWED_REVIEW_STATUSES = {
    "needs_review",
    "reviewed",
    "restricted",
    "blocked",
    "unknown",
}

ALLOWED_MARKET_READINESS = {
    "catalog_only",
    "experimental",
    "private_beta",
    "verified",
    "public",
}


def _has_value(value: object) -> bool:
    return bool(str(value or "").strip())


def _provider_failures() -> list[str]:
    failures: list[str] = []
    seen: set[str] = set()
    for provider in PROVIDERS:
        if provider.key in seen:
            failures.append(f"{provider.key}: duplicate provider key")
            continue
        seen.add(provider.key)

        governance = provider_governance(provider.key)
        market_readiness = str(governance.get("market_readiness") or "").strip().lower()
        access_mode = str(governance.get("access_mode") or "").strip().lower()
        terms_status = str(governance.get("terms_review_status") or "").strip().lower()
        robots_status = str(governance.get("robots_review_status") or "").strip().lower()
        owner = str(governance.get("operator_owner") or "").strip()
        reviewed_at = str(governance.get("last_rights_reviewed_at") or "").strip()

        if market_readiness not in ALLOWED_MARKET_READINESS:
            failures.append(f"{provider.key}: invalid market_readiness {market_readiness!r}")
        if access_mode not in ALLOWED_ACCESS_MODES:
            failures.append(f"{provider.key}: invalid access_mode {access_mode!r}")
        if terms_status not in ALLOWED_REVIEW_STATUSES:
            failures.append(f"{provider.key}: invalid terms_review_status {terms_status!r}")
        if robots_status not in ALLOWED_REVIEW_STATUSES:
            failures.append(f"{provider.key}: invalid robots_review_status {robots_status!r}")
        if not owner:
            failures.append(f"{provider.key}: missing operator_owner")

        if provider.search_ready:
            if market_readiness == "catalog_only":
                failures.append(f"{provider.key}: search_ready provider cannot be catalog_only")
            if int(governance.get("maximum_concurrency") or 0) <= 0:
                failures.append(f"{provider.key}: search_ready provider needs maximum_concurrency > 0")
            if int(governance.get("requests_per_hour") or 0) <= 0:
                failures.append(f"{provider.key}: search_ready provider needs requests_per_hour > 0")
            if terms_status == "unknown" or robots_status == "unknown":
                failures.append(f"{provider.key}: search_ready provider cannot have unknown rights review status")
        elif market_readiness != "catalog_only":
            failures.append(f"{provider.key}: unimplemented provider must remain catalog_only")

        if bool(governance.get("listing_cache_allowed")) and int(governance.get("cache_ttl_seconds") or 0) <= 0:
            failures.append(f"{provider.key}: listing_cache_allowed requires cache_ttl_seconds > 0")

        public_or_media_rights = any(
            bool(governance.get(key))
            for key in (
                "public_packet_allowed",
                "photo_republication_allowed",
                "floorplan_republication_allowed",
            )
        )
        if public_or_media_rights:
            if terms_status != "reviewed":
                failures.append(f"{provider.key}: public/media rights require reviewed terms")
            if not _has_value(reviewed_at):
                failures.append(f"{provider.key}: public/media rights require last_rights_reviewed_at")

        if market_readiness == "public":
            if terms_status != "reviewed" or robots_status != "reviewed":
                failures.append(f"{provider.key}: public market readiness requires reviewed terms and robots")
            if not _has_value(reviewed_at):
                failures.append(f"{provider.key}: public market readiness requires last_rights_reviewed_at")

    return failures


def main() -> int:
    failures = _provider_failures()
    payload = {
        "status": "ok" if not failures else "failed",
        "provider_count": len(PROVIDERS),
        "failure_count": len(failures),
        "failures": failures,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
