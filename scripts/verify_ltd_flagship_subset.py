#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


FLAGSHIP_REQUIRED = {
    "1min.AI": {"live_provider_call_verified"},
    "Prompt Architects": {"manual_seeded", "complete"},
    "PayFunnels": {"unconfigured_external_authority"},
    "BrowserAct": {"complete"},
    "Teable": {"complete"},
    "ClickRank.ai": {"complete"},
    "Emailit": {"manual_seeded", "complete"},
    "Pixefy": {"manual_seeded", "complete"},
    "Rafter": {"manual_seeded", "complete"},
}

ACCEPTED_SOURCES = {
    "1min.AI": {"worker_health_probe + principal_bound_provider_receipt"},
    "Prompt Architects": {"local_env + prompt_foundry_receipts"},
    "PayFunnels": {"deployed_runtime_probe + contract_tests"},
    "BrowserAct": {"browseract_live"},
    "Teable": {"browseract_live"},
    "ClickRank.ai": {"clickrank_live"},
    "Emailit": {"emailit_api_live"},
    "Pixefy": {"fleet_verified"},
    "Rafter": {"fleet_verified"},
}

EVIDENCE_CLASSES = {
    "1min.AI": "principal_bound_live_provider_call",
    "Prompt Architects": "credential_and_contract_evidence",
    "PayFunnels": "unconfigured_external_authority",
    "BrowserAct": "live_account_evidence",
    "Teable": "live_account_evidence",
    "ClickRank.ai": "live_service_evidence",
    "Emailit": "live_service_evidence",
    "Pixefy": "auxiliary_verification_evidence",
    "Rafter": "auxiliary_verification_evidence",
}

LIVE_INTEGRATION_SERVICES = {
    "1min.AI",
    "BrowserAct",
    "Teable",
    "ClickRank.ai",
    "Emailit",
}

MIN_ACCEPTED_COUNT = 9


def _extract_discovery_rows(markdown_text: str) -> dict[str, dict[str, str]]:
    lines = markdown_text.splitlines()
    rows: dict[str, dict[str, str]] = {}
    in_section = False
    for raw_line in lines:
        line = raw_line.rstrip()
        if line.startswith("## Discovery Tracking"):
            in_section = True
            continue
        if in_section and line.startswith("## ") and not line.startswith("## Discovery Tracking"):
            break
        if not in_section or not line.startswith("|"):
            continue
        if line.startswith("|---"):
            continue
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(parts) != 6 or parts[0] == "Service":
            continue
        service = parts[0].strip().strip("`")
        rows[service] = {
            "account": parts[1],
            "discovery_status": parts[2].strip("`"),
            "verification_source": parts[3].strip("`"),
            "last_verified": parts[4],
            "notes": parts[5],
        }
    return rows


def build_receipt(*, markdown_text: str) -> dict[str, object]:
    rows = _extract_discovery_rows(markdown_text)
    failures: list[str] = []
    accepted_total = 0
    live_verified_total = 0
    service_checks: dict[str, dict[str, object]] = {}
    for service, accepted_statuses in FLAGSHIP_REQUIRED.items():
        row = rows.get(service)
        status = str((row or {}).get("discovery_status") or "").strip()
        source = str((row or {}).get("verification_source") or "").strip()
        accepted = bool(row) and status in accepted_statuses and source in ACCEPTED_SOURCES[service]
        if accepted:
            accepted_total += 1
        else:
            failures.append(f"flagship_subset_mismatch:{service}:{status or 'missing'}:{source or 'missing'}")
        live_integration_verified = (
            accepted and service in LIVE_INTEGRATION_SERVICES
        )
        if live_integration_verified:
            live_verified_total += 1
        service_checks[service] = {
            "present": bool(row),
            "status": status,
            "source": source,
            "accepted": accepted,
            "evidence_class": EVIDENCE_CLASSES[service],
            "live_integration_verified": live_integration_verified,
        }
    if accepted_total < MIN_ACCEPTED_COUNT:
        failures.append(f"flagship_subset_coverage_below_floor:{accepted_total}<{MIN_ACCEPTED_COUNT}")
    return {
        "contract_name": "ea.verify_ltd_flagship_subset",
        "status": "pass" if not failures else "fail",
        "accepted_total": accepted_total,
        "live_verified_total": live_verified_total,
        "contract_or_unconfigured_total": accepted_total - live_verified_total,
        "minimum_required": MIN_ACCEPTED_COUNT,
        "not_live_integration_gate": True,
        "acceptance_semantics": (
            "Passing means each named service has its exact recorded posture; "
            "contract-only, auxiliary, and unconfigured rows do not count as live integration."
        ),
        "services": service_checks,
        "failures": failures,
    }


def main() -> int:
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        print(
            "Usage:\n"
            "  python3 scripts/verify_ltd_flagship_subset.py\n\n"
            "Fail closed when the named flagship LTD subset drifts away from its\n"
            "accepted verification sources or minimum coverage floor."
        )
        return 0
    root = Path(__file__).resolve().parents[1]
    markdown_text = (root / "LTDs.md").read_text(encoding="utf-8")
    receipt = build_receipt(markdown_text=markdown_text)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
