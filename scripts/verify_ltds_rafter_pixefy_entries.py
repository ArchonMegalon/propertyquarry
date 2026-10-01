#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPLETION_ROOT = Path("/docker/chummercomplete/_completion/ltd_inventory")
LTD_PATH = ROOT / "LTDs.md"

RAFTER_ARTIFACT = COMPLETION_ROOT / "RAFTER_TIER3_LTDS_ENTRY.generated.json"
PIXEFY_ARTIFACT = COMPLETION_ROOT / "PIXEFY_TIER3_LTDS_ENTRY.generated.json"


def _read_lines(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _find_row(lines: str, service: str) -> str | None:
    for line in lines.splitlines():
        if line.startswith(f"| `{service}` "):
            return line
    return None


def _row_contains(row: str, *needles: str) -> bool:
    return all(needle in row for needle in needles)


def _build_payload(service: str, tier: str, account_user: str, status: str) -> dict:
    return {
        "status": status,
        "service": service,
        "plan": tier,
        "account_user": account_user,
        "workspace_integration_tier": "Tier 2",
        "verification_status": "fleet_verified",
        "missing_tokens": [],
        "source_path": str(LTD_PATH),
    }


def _write_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _extract_discovery_row(lines: str, service: str) -> str | None:
    for line in lines.splitlines():
        if line.startswith(f"| `{service}` ") and "manual_seeded" in line:
            return line
    return None


def _summary_total_at_least(lines: str, minimum: int) -> bool:
    match = re.search(r"`(\d+)`\s+total LTD products tracked", lines)
    if not match:
        return False
    try:
        return int(match.group(1)) >= minimum
    except Exception:
        return False


def main() -> int:
    text = _read_lines(LTD_PATH)
    summary_has_minimum_total = _summary_total_at_least(text, 45)
    rafter_attention = "`Rafter` highest tier is now reported and Fleet security/proof provider verification now passes. It remains an auxiliary QA gate, not release truth." in text
    pixefy_attention = "`Pixefy` highest tier is tracked and Fleet responsive-visual-QA provider verification now passes. It remains an auxiliary QA gate, not product truth." in text

    rafter_row = _find_row(text, "Rafter")
    pixefy_row = _find_row(text, "Pixefy")
    rafter_discovery = _extract_discovery_row(text, "Rafter")
    pixefy_discovery = _extract_discovery_row(text, "Pixefy")

    rafter_missing: list[str] = []
    pixefy_missing: list[str] = []

    if rafter_row is None:
        rafter_missing.append("rafter_inventory_row")
    elif not _row_contains(
        rafter_row,
        "`Rafter`",
        "License Tier 3",
        "`1 account`",
        "`Owned`",
        "`Tier 2`",
        "Fleet security/proof gate verified",
        "false-complete prevention",
        "must not own product truth",
    ):
        rafter_missing.append("rafter_inventory_row_malformed")

    if pixefy_row is None:
        pixefy_missing.append("pixefy_inventory_row")
    elif not _row_contains(
        pixefy_row,
        "`Pixefy`",
        "License Tier 3",
        "`1 account`",
        "`Owned`",
        "`Tier 2`",
        "Fleet responsive visual QA gate verified",
        "responsive",
        "must not be product truth",
    ):
        pixefy_missing.append("pixefy_inventory_row_malformed")

    if not rafter_discovery:
        rafter_missing.append("rafter_discovery_tracking_row")
    elif "the.girscheles@gmail.com" not in rafter_discovery or "fleet_verified" not in rafter_discovery:
        rafter_missing.append("rafter_discovery_tracking_account")

    if not pixefy_discovery:
        pixefy_missing.append("pixefy_discovery_tracking_row")
    elif "the.girscheles@gmail.com" not in pixefy_discovery or "fleet_verified" not in pixefy_discovery:
        pixefy_missing.append("pixefy_discovery_tracking_account")

    if not summary_has_minimum_total:
        rafter_missing.append("summary_total")
        pixefy_missing.append("summary_total")

    if not rafter_attention:
        rafter_missing.append("rafter_attention_item")
    if not pixefy_attention:
        pixefy_missing.append("pixefy_attention_item")

    rafter_payload = _build_payload(
        service="Rafter",
        tier="License Tier 3 / highest AppSumo tier",
        account_user="the.girscheles@gmail.com",
        status="pass" if not rafter_missing else "fail",
    )
    pixefy_payload = _build_payload(
        service="Pixefy",
        tier="License Tier 3 / highest AppSumo tier",
        account_user="the.girscheles@gmail.com",
        status="pass" if not pixefy_missing else "fail",
    )
    rafter_payload["missing_tokens"] = rafter_missing
    pixefy_payload["missing_tokens"] = pixefy_missing

    _write_payload(RAFTER_ARTIFACT, rafter_payload)
    _write_payload(PIXEFY_ARTIFACT, pixefy_payload)

    if rafter_payload["status"] != "pass" or pixefy_payload["status"] != "pass":
        raise SystemExit("Rafter and/or Pixefy LTD inventory checks failed.")

    print(RAFTER_ARTIFACT)
    print(PIXEFY_ARTIFACT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
