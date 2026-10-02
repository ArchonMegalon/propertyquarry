#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from scripts.propertyquarry_live_run_status_canary import (
    TERMINAL_STATUSES,
    _delete_run,
    _env_file_values,
    _fetch_run_page,
    _fetch_run_status,
    _now_iso,
    _start_run,
    _start_workspace,
)


DEFAULT_EMPTY_RECOVERY_PAYLOAD = {
    "selected_platforms": ["willhaben"],
    "property_preferences": {
        "country_code": "AT",
        "region_code": "vienna",
        "listing_mode": "rent",
        "property_type": ["apartment"],
        "location_query": "1020 Vienna",
        "selected_location_values": ["1020 Vienna"],
        "language_code": "en",
        "search_goal": "home",
        "search_mode": "strict",
        "max_price_eur": 500,
        "min_area_m2": 150,
    },
    "max_results_per_source": 1,
    "force_refresh": False,
}


def _strip_html(value: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))).strip()


def _extract_region(
    html_text: object,
    *,
    marker: str,
    end_markers: tuple[str, ...],
    fallback_chars: int = 20_000,
    marker_is_regex: bool = False,
) -> str:
    text = str(html_text or "")
    if marker_is_regex:
        match = re.search(marker, text, flags=re.IGNORECASE)
        if not match:
            return ""
        marker_index = match.start()
        start_index = match.start()
    else:
        marker_index = text.find(marker)
        if marker_index < 0:
            return ""
        start_index = text.rfind("<", 0, marker_index)
        if start_index < 0:
            start_index = marker_index
    end_index = len(text)
    for candidate in end_markers:
        candidate_index = text.find(candidate, marker_index + len(marker))
        if candidate_index >= 0:
            end_index = min(end_index, candidate_index)
    if end_index == len(text):
        end_index = min(len(text), marker_index + max(2_000, int(fallback_chars or 20_000)))
    return text[start_index:end_index]


def _extract_counterfactual_buttons(html_text: object) -> list[dict[str, Any]]:
    region = _extract_region(
        html_text,
        marker="<div data-pqx-counterfactuals>",
        end_markers=("data-pqx-ranked-candidates", "data-pqx-running-details", "</section>"),
        fallback_chars=24_000,
    )
    rows: list[dict[str, Any]] = []
    patterns = (
        r"<button[^>]+class=\"pqx-suppression-item\"[^>]+data-pqx-counterfactual=(?P<quote>['\"])(?P<payload>.*?)(?P=quote)[^>]*>"
        r".*?<strong>(?P<title>.*?)</strong>.*?<span class=\"pqx-suppression-action\">(?P<action>.*?)</span>",
        r"<div[^>]+class=\"pqx-suppression-item[^\"]*\"[^>]*>.*?<strong>(?P<title>.*?)</strong>.*?<button[^>]+data-pqx-counterfactual=(?P<quote>['\"])(?P<payload>.*?)(?P=quote)[^>]*>(?P<action>.*?)</button>",
    )
    seen_payloads: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, region, flags=re.IGNORECASE | re.DOTALL):
            payload_text = html.unescape(str(match.group("payload") or "")).strip()
            if payload_text in seen_payloads:
                continue
            seen_payloads.add(payload_text)
            try:
                adjustments = json.loads(payload_text) if payload_text else {}
            except Exception:
                adjustments = {}
            rows.append(
                {
                    "title": _strip_html(match.group("title")),
                    "action": _strip_html(match.group("action")),
                    "adjustments": adjustments if isinstance(adjustments, dict) else {},
                    "payload_text": payload_text,
                }
            )
    return rows


def _extract_filtered_dialog_slider_fields(html_text: object) -> list[dict[str, str]]:
    dialog = _extract_region(
        html_text,
        marker="<dialog class=\"pqx-filtered-dialog\" data-pqx-filtered-dialog",
        end_markers=("</dialog>",),
        fallback_chars=24_000,
    )
    rows: list[dict[str, str]] = []
    for tag in re.findall(r"<input\b[^>]*data-pqx-filter-slider[^>]*>", dialog, flags=re.IGNORECASE | re.DOTALL):
        attrs = {
            key.strip(): html.unescape(value).strip()
            for key, value in re.findall(r"([a-zA-Z0-9:_-]+)=\"([^\"]*)\"", tag)
        }
        field = str(attrs.get("data-pqx-filter-field") or "").strip()
        if not field:
            continue
        rows.append(
            {
                "field": field,
                "kind": str(attrs.get("data-pqx-filter-kind") or "").strip(),
                "unit": str(attrs.get("data-pqx-filter-unit") or "").strip(),
                "min": str(attrs.get("min") or "").strip(),
                "max": str(attrs.get("max") or "").strip(),
                "value": str(attrs.get("value") or "").strip(),
            }
        )
    return rows


def _extract_empty_state_copy(html_text: object) -> dict[str, str]:
    region = _extract_region(
        html_text,
        marker=r"<section\b[^>]*class=\"[^\"]*pqx-empty-results[^\"]*\"[^>]*>",
        end_markers=("<div data-pqx-counterfactuals>",),
        fallback_chars=18_000,
        marker_is_regex=True,
    )
    copy = _strip_html(region)
    heading_match = re.search(r"<h1>(?P<text>.*?)</h1>", region, flags=re.IGNORECASE | re.DOTALL)
    summary_match = re.search(
        r"<p class=\"pqx-note pqx-empty-outcome-line\"[^>]*>(?P<text>.*?)</p>",
        region,
        flags=re.IGNORECASE | re.DOTALL,
    )
    next_move_match = re.search(
        r"<div class=\"pqx-note\" data-pqx-run-summary>(?P<text>.*?)</div>",
        region,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return {
        "region_text": copy,
        "heading": _strip_html(heading_match.group("text")) if heading_match else "",
        "summary": _strip_html(summary_match.group("text")) if summary_match else "",
        "next_move": _strip_html(next_move_match.group("text")) if next_move_match else "",
    }


def build_live_empty_recovery_canary_receipt(
    *,
    base_url: str,
    token: str,
    principal_id: str,
    timeout_seconds: float,
    poll_seconds: float,
    run_body: dict[str, Any] | None = None,
    workspace_starter: Callable[..., dict[str, Any]] | None = None,
    run_starter: Callable[..., dict[str, Any]] | None = None,
    status_fetcher: Callable[..., dict[str, Any]] | None = None,
    page_fetcher: Callable[..., dict[str, Any]] | None = None,
    run_deleter: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    workspace_runner = workspace_starter or _start_workspace
    start_runner = run_starter or _start_run
    status_reader = status_fetcher or _fetch_run_status
    page_reader = page_fetcher or _fetch_run_page
    delete_runner = run_deleter or _delete_run
    payload = dict(run_body or DEFAULT_EMPTY_RECOVERY_PAYLOAD)
    receipt: dict[str, Any] = {
        "generated_at": _now_iso(),
        "base_url": base_url,
        "principal_id": principal_id,
        "timeout_seconds": float(timeout_seconds),
        "poll_seconds": float(poll_seconds),
        "run_payload": payload,
        "status": "failed",
    }

    workspace_result = workspace_runner(
        base_url=base_url,
        token=token,
        principal_id=principal_id,
        timeout_seconds=min(timeout_seconds, 30.0),
    )
    receipt["workspace_start"] = workspace_result

    start_result = start_runner(
        base_url=base_url,
        token=token,
        principal_id=principal_id,
        body=payload,
        timeout_seconds=min(timeout_seconds, 30.0),
    )
    receipt["run_start"] = start_result
    run_id = str(start_result.get("run_id") or "").strip()
    receipt["run_id"] = run_id

    observed_statuses: list[dict[str, Any]] = []
    latest_status_payload: dict[str, Any] = {}
    terminal_status_observed = False
    deadline = time.monotonic() + max(float(timeout_seconds or 0.0), 1.0)
    if run_id:
        while time.monotonic() < deadline:
            latest_status_payload = status_reader(
                base_url=base_url,
                token=token,
                principal_id=principal_id,
                run_id=run_id,
                timeout_seconds=min(25.0, timeout_seconds),
            )
            summary = dict(latest_status_payload.get("summary") or {})
            status_value = str(latest_status_payload.get("status") or "").strip().lower()
            observed_statuses.append(
                {
                    "status": status_value,
                    "listing_total": int(summary.get("listing_total") or 0),
                    "filtered_total": int(summary.get("filtered_total") or summary.get("held_back_total") or 0),
                    "raw_listing_total": int(summary.get("raw_listing_total") or 0),
                    "message": str(latest_status_payload.get("message") or "").strip()[:220],
                    "eta_label": str(summary.get("eta_label") or "").strip(),
                }
            )
            if status_value in TERMINAL_STATUSES:
                terminal_status_observed = True
                break
            time.sleep(max(1.0, float(poll_seconds or 1.0)))
    receipt["observed_statuses"] = observed_statuses
    receipt["status_payload"] = latest_status_payload

    page_result = (
        page_reader(
            base_url=base_url,
            token=token,
            principal_id=principal_id,
            run_id=run_id,
            timeout_seconds=min(timeout_seconds, 30.0),
        )
        if run_id
        else {"status_code": 0, "text": "", "error": "run_id_missing", "final_url": ""}
    )
    page_text = str(page_result.get("text") or "")
    counterfactual_buttons = _extract_counterfactual_buttons(page_text)
    slider_fields = _extract_filtered_dialog_slider_fields(page_text)
    empty_copy = _extract_empty_state_copy(page_text)
    receipt["run_page"] = {
        "status_code": int(page_result.get("status_code") or 0),
        "duration_ms": int(page_result.get("duration_ms") or 0),
        "error": str(page_result.get("error") or "").strip(),
        "final_url": str(page_result.get("final_url") or "").strip(),
    }
    receipt["counterfactual_buttons"] = counterfactual_buttons
    receipt["slider_fields"] = slider_fields
    receipt["empty_state"] = empty_copy

    if run_id and not counterfactual_buttons:
        page_deadline = time.monotonic() + min(20.0, max(4.0, float(timeout_seconds or 0.0) / 3.0))
        while time.monotonic() < page_deadline and not counterfactual_buttons:
            time.sleep(max(1.0, float(poll_seconds or 1.0)))
            refreshed_page_result = page_reader(
                base_url=base_url,
                token=token,
                principal_id=principal_id,
                run_id=run_id,
                timeout_seconds=min(timeout_seconds, 30.0),
            )
            page_result = refreshed_page_result
            page_text = str(page_result.get("text") or "")
            counterfactual_buttons = _extract_counterfactual_buttons(page_text)
            slider_fields = _extract_filtered_dialog_slider_fields(page_text)
            empty_copy = _extract_empty_state_copy(page_text)
            receipt["run_page"] = {
                "status_code": int(page_result.get("status_code") or 0),
                "duration_ms": int(page_result.get("duration_ms") or 0),
                "error": str(page_result.get("error") or "").strip(),
                "final_url": str(page_result.get("final_url") or "").strip(),
            }
            receipt["counterfactual_buttons"] = counterfactual_buttons
            receipt["slider_fields"] = slider_fields
            receipt["empty_state"] = empty_copy
            if counterfactual_buttons:
                break

    cleanup_result = (
        delete_runner(
            base_url=base_url,
            token=token,
            principal_id=principal_id,
            run_id=run_id,
            timeout_seconds=min(timeout_seconds, 20.0),
        )
        if run_id
        else {"deleted": False, "reason": "run_id_missing"}
    )
    receipt["cleanup"] = cleanup_result

    removed_ranking_buttons = [
        row
        for row in counterfactual_buttons
        if "min_match_score" in dict(row.get("adjustments") or {})
        or "ranking" in str(row.get("title") or "").strip().lower()
        or "ranking" in str(row.get("action") or "").strip().lower()
    ]
    removed_ranking_sliders = [
        row
        for row in slider_fields
        if str(row.get("field") or "").strip() == "min_match_score"
        or str(row.get("kind") or "").strip() == "ranking_bar"
    ]
    heading_text = str(empty_copy.get("heading") or "")
    region_text = str(empty_copy.get("region_text") or "")
    no_shortlist_visible = (
        "No homes in scope yet." in heading_text
        or "Nothing landed in the selected area yet." in heading_text
        or "No ranked homes" in region_text
    )
    final_url = str(page_result.get("final_url") or "").strip()
    checks = [
        {
            "name": "workspace_start_ok",
            "ok": int(dict(workspace_result.get("_http") or {}).get("status_code") or 0) == 200,
        },
        {
            "name": "run_start_ok",
            "ok": bool(run_id) and int(dict(start_result.get("_http") or {}).get("status_code") or 0) == 200,
        },
        {
            "name": "terminal_status_observed",
            "ok": terminal_status_observed,
        },
        {
            "name": "run_page_loaded",
            "ok": int(page_result.get("status_code") or 0) == 200,
        },
        {
            "name": "stays_on_properties_surface",
            "ok": "/app/properties" in final_url and str(run_id) in final_url,
        },
        {
            "name": "no_shortlist_state_visible",
            "ok": no_shortlist_visible,
        },
        {
            "name": "removed_ranking_recovery_button_absent",
            "ok": not removed_ranking_buttons,
        },
        {
            "name": "removed_ranking_slider_absent",
            "ok": not removed_ranking_sliders,
        },
        {
            "name": "no_removed_ranking_copy",
            "ok": "ranking bar" not in page_text.lower()
            and "turn bar off" not in page_text.lower()
            and "use 15/100" not in page_text.lower(),
        },
        {
            "name": "no_old_fit_threshold_filter_copy",
            "ok": "Below fit threshold" not in page_text and "Below the matching threshold." not in page_text,
        },
        {
            "name": "cleanup_deleted",
            "ok": bool(cleanup_result.get("deleted")),
        },
    ]
    receipt["checks"] = checks
    receipt["failed_checks"] = [row["name"] for row in checks if not bool(row.get("ok"))]
    receipt["status"] = "pass" if not receipt["failed_checks"] else "fail"
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the live PropertyQuarry no-shortlist recovery surface on a real empty-result run.")
    parser.add_argument("--api-base", default="http://127.0.0.1:8097")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--principal-id", default="")
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--write", default="")
    args = parser.parse_args()

    env_values = {**_env_file_values(Path(args.env_file)), **os.environ}
    token = str(env_values.get("EA_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("EA_API_TOKEN missing")
    principal_id = str(args.principal_id or "").strip() or f"pq-live-empty-recovery-canary-{int(time.time())}"
    receipt = build_live_empty_recovery_canary_receipt(
        base_url=str(args.api_base),
        token=token,
        principal_id=principal_id,
        timeout_seconds=max(20.0, float(args.timeout_seconds)),
        poll_seconds=max(1.0, float(args.poll_seconds)),
    )
    if args.write:
        output_path = Path(args.write)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
