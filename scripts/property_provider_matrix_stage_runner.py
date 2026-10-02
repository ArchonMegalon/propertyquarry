#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from scripts.property_live_provider_smoke import build_live_provider_smoke_receipt, _targeted_search_matrix_summary
from app.services.property_market_catalog import CUSTOMER_SEARCH_COUNTRY_ORDER, provider_options

_EXPECTED_MODES = {"targeted_no_soft_filters", "targeted_soft_filters"}


def _normalize_countries(countries: Iterable[str]) -> tuple[str, ...]:
    normalized = [
        str(country or "").strip().upper()
        for country in countries
        if str(country or "").strip()
    ]
    return tuple(dict.fromkeys(normalized))


def _search_ready_provider_keys_by_country(countries: Iterable[str]) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for country in _normalize_countries(countries):
        rows[country] = [
            str(option.get("value") or "").strip()
            for option in provider_options(country_code=country)
            if bool(option.get("search_ready")) and not bool(option.get("coming_soon")) and str(option.get("value") or "").strip()
        ]
    return rows


def _load_receipt(path: str | Path = "") -> dict[str, Any]:
    candidate = Path(str(path or "").strip()) if str(path or "").strip() else None
    if candidate is None or not candidate.exists():
        return {}
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    return dict(payload) if isinstance(payload, dict) else {}


def _targeted_row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("country_code") or "").strip().upper(),
        str(row.get("provider") or "").strip(),
        str(row.get("mode") or "").strip(),
    )


def _country_key(row: dict[str, Any], field: str = "country_code") -> str:
    return str(row.get(field) or "").strip().upper()


def _passed_modes_by_provider(receipt: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    passed: dict[tuple[str, str], set[str]] = {}
    for row in list(receipt.get("targeted_search_matrix") or []):
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip().lower() != "pass":
            continue
        country = str(row.get("country_code") or "").strip().upper()
        provider = str(row.get("provider") or "").strip()
        mode = str(row.get("mode") or "").strip()
        if not country or not provider or not mode:
            continue
        passed.setdefault((country, provider), set()).add(mode)
    return passed


def next_provider_batch(
    *,
    receipt: dict[str, Any],
    countries: Iterable[str],
    batch_size: int,
    allowed_provider_keys: Iterable[str] = (),
) -> list[str]:
    allowed = {
        str(provider_key or "").strip()
        for provider_key in allowed_provider_keys
        if str(provider_key or "").strip()
    }
    passed = _passed_modes_by_provider(receipt)
    remaining_by_country: dict[str, list[str]] = {}
    normalized_countries = _normalize_countries(countries)
    for country, provider_keys in _search_ready_provider_keys_by_country(normalized_countries).items():
        remaining = [
            provider_key
            for provider_key in provider_keys
            if (not allowed or provider_key in allowed)
            and passed.get((country, provider_key), set()) != _EXPECTED_MODES
        ]
        if remaining:
            remaining_by_country[country] = remaining
    selected: list[str] = []
    target_size = max(1, int(batch_size or 1))
    ordered_countries = [
        country
        for country in normalized_countries
        if country in remaining_by_country
    ]
    while len(selected) < target_size:
        progress_made = False
        for country in ordered_countries:
            remaining = remaining_by_country.get(country) or []
            if not remaining:
                continue
            selected.append(remaining.pop(0))
            progress_made = True
            if len(selected) >= target_size:
                break
        if not progress_made:
            break
    return selected


def _merge_rows_by_key(
    previous_rows: Iterable[dict[str, Any]],
    current_rows: Iterable[dict[str, Any]],
    *,
    key_builder,
) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, ...] | str, dict[str, Any]] = {}
    for row in previous_rows:
        if not isinstance(row, dict):
            continue
        merged[key_builder(row)] = dict(row)
    for row in current_rows:
        if not isinstance(row, dict):
            continue
        merged[key_builder(row)] = dict(row)
    return [
        merged[key]
        for key in sorted(merged.keys())
    ]


def merge_provider_matrix_receipts(
    *,
    previous_receipt: dict[str, Any],
    current_receipt: dict[str, Any],
    countries: Iterable[str],
) -> dict[str, Any]:
    normalized_countries = _normalize_countries(countries)
    merged_checks = _merge_rows_by_key(
        list(previous_receipt.get("checks") or []),
        list(current_receipt.get("checks") or []),
        key_builder=lambda row: _country_key(row),
    )
    merged_sanitization = _merge_rows_by_key(
        list(previous_receipt.get("cross_country_sanitization_checks") or []),
        list(current_receipt.get("cross_country_sanitization_checks") or []),
        key_builder=lambda row: _country_key(row),
    )
    merged_targeted = _merge_rows_by_key(
        list(previous_receipt.get("targeted_search_matrix") or []),
        list(current_receipt.get("targeted_search_matrix") or []),
        key_builder=_targeted_row_key,
    )
    summary = _targeted_search_matrix_summary(
        merged_targeted,
        countries=normalized_countries,
        execute_requested=True,
        enabled=True,
        dry_run=False,
        provider_keys=(),
        max_providers=0,
    )
    check_statuses = {str(row.get("status") or "").strip().lower() for row in merged_checks if isinstance(row, dict)}
    targeted_statuses = {str(row.get("status") or "").strip().lower() for row in merged_targeted if isinstance(row, dict)}
    sanitization_statuses = {str(row.get("status") or "").strip().lower() for row in merged_sanitization if isinstance(row, dict)}
    sanitization_ok = all(bool(row.get("sanitization_ok", True)) for row in merged_sanitization if isinstance(row, dict))
    if "fail" in check_statuses or "fail" in targeted_statuses or "fail" in sanitization_statuses or not sanitization_ok:
        status = "fail"
        targeted_status = "fail"
    elif (
        check_statuses == {"pass"}
        and summary.get("all_search_ready_providers_covered") is True
        and summary.get("all_search_ready_provider_modes_passed") is True
        and summary.get("dispatch_acceptance_complete") is True
        and summary.get("status_readback_complete") is True
        and sanitization_statuses == {"pass"}
    ):
        status = "pass"
        targeted_status = "pass"
    else:
        status = "staged_provider_coverage_incomplete"
        targeted_status = "partial" if merged_targeted else "planned"

    passed = _passed_modes_by_provider({"targeted_search_matrix": merged_targeted})
    remaining_by_country: dict[str, int] = {}
    for country, provider_keys in _search_ready_provider_keys_by_country(normalized_countries).items():
        remaining_by_country[country] = sum(
            1
            for provider_key in provider_keys
            if passed.get((country, provider_key), set()) != _EXPECTED_MODES
        )
    next_batch = next_provider_batch(
        receipt={"targeted_search_matrix": merged_targeted},
        countries=normalized_countries,
        batch_size=3,
    )
    latest_base = str(current_receipt.get("base_url") or previous_receipt.get("base_url") or "").strip()
    latest_timeout = float(current_receipt.get("search_run_timeout_seconds") or previous_receipt.get("search_run_timeout_seconds") or 25.0)
    latest_catalog_timeout = float(current_receipt.get("provider_catalog_timeout_seconds") or previous_receipt.get("provider_catalog_timeout_seconds") or 8.0)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "base_url": latest_base,
        "enabled": True,
        "dry_run": False,
        "country_scope": "explicit",
        "provider_catalog_timeout_seconds": latest_catalog_timeout,
        "search_run_timeout_seconds": latest_timeout,
        "checks": merged_checks,
        "targeted_search_matrix": merged_targeted,
        "targeted_search_matrix_count": len(merged_targeted),
        "targeted_search_matrix_executed": bool(merged_targeted),
        "targeted_search_matrix_status": targeted_status,
        "targeted_search_matrix_summary": summary,
        "cross_country_sanitization_checks": merged_sanitization,
        "cross_country_sanitization_summary": {
            "case_count": len(merged_sanitization),
            "status_counts": {
                status_key: sum(
                    1
                    for row in merged_sanitization
                    if str(row.get("status") or "").strip().lower() == status_key
                )
                for status_key in sorted(sanitization_statuses)
            },
            "sanitization_ok": sanitization_ok,
        },
        "remaining_search_ready_provider_count_by_country": remaining_by_country,
        "next_provider_batch_suggestion": next_batch,
        "checkpoint": False,
        "complete": True,
        "notes": [
            "This receipt aggregates staged live provider E2E slices.",
            "A partial status means the executed slices passed, but full search-ready provider coverage is still incomplete.",
        ],
    }


def build_staged_provider_matrix_receipt(
    *,
    countries: Iterable[str],
    base_url: str,
    batch_size: int,
    resume_receipt_path: str | Path = "",
    allowed_provider_keys: Iterable[str] = (),
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    normalized_countries = _normalize_countries(countries)
    previous_receipt = _load_receipt(resume_receipt_path)
    batch_provider_keys = next_provider_batch(
        receipt=previous_receipt,
        countries=normalized_countries,
        batch_size=batch_size,
        allowed_provider_keys=allowed_provider_keys,
    )
    if not batch_provider_keys:
        if previous_receipt:
            return merge_provider_matrix_receipts(
                previous_receipt=previous_receipt,
                current_receipt={},
                countries=normalized_countries,
            )
        raise ValueError("no_provider_batch_available")
    current_receipt = build_live_provider_smoke_receipt(
        countries=normalized_countries,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        provider_keys=tuple(batch_provider_keys),
        max_providers=max(1, int(batch_size or 1)),
        execute_search_matrix=True,
        all_search_ready_countries=False,
        resume_checkpoint_path=resume_receipt_path,
    )
    return merge_provider_matrix_receipts(
        previous_receipt=previous_receipt,
        current_receipt=current_receipt,
        countries=normalized_countries,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage and merge live PropertyQuarry provider E2E receipts.")
    parser.add_argument("--country", action="append", default=[], help="Country code to include. Defaults to AT, DE, CR.")
    parser.add_argument("--provider", action="append", default=[], help="Optional provider key allow-list. Repeatable.")
    parser.add_argument("--batch-size", type=int, default=3, help="How many uncovered providers to execute in this batch.")
    parser.add_argument("--base-url", default="http://localhost:8097")
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--resume-from", default="", help="Optional aggregate or smoke receipt to continue from.")
    parser.add_argument("--write", default="", help="Optional JSON output path.")
    args = parser.parse_args()

    countries = tuple(args.country or CUSTOMER_SEARCH_COUNTRY_ORDER)
    receipt = build_staged_provider_matrix_receipt(
        countries=countries,
        base_url=str(args.base_url),
        batch_size=max(1, int(args.batch_size or 1)),
        resume_receipt_path=args.resume_from,
        allowed_provider_keys=tuple(args.provider or []),
        timeout_seconds=float(args.timeout_seconds),
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        write_path = Path(args.write)
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if str(receipt.get("status") or "").strip().lower() in {"pass", "staged_provider_coverage_incomplete"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
