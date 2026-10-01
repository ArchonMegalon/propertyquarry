#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"processed", "completed_partial", "failed", "cancelled", "noop"}
SOFT_BOOLEAN_KEYS = {
    "avoid_noise_risk_area",
    "prefer_good_air_quality",
    "prefer_low_crime_area",
    "require_drinking_water_quality_research",
    "require_parking_pressure_check",
    "avoid_flood_risk_area",
    "avoid_cesspit_or_septic_risk",
    "require_winter_access_research",
    "require_high_speed_internet",
    "require_high_speed_internet_evidence",
    "check_parking_situation",
}
COUNTER_KEYS = (
    "raw_listing_total",
    "scanned_listing_total",
    "reviewed_listing_total",
    "listing_total",
    "ranked_total",
    "held_back_total",
    "filtered_total",
    "filtered_area_total",
    "filtered_generic_page_total",
    "filtered_floorplan_total",
    "filtered_listing_mode_total",
    "filtered_property_type_total",
    "filtered_availability_total",
    "filtered_low_fit_total",
    "duplicate_listing_total",
    "sources_completed",
    "sources_total",
    "high_fit_total",
    "top_fit_score",
    "max_match_score",
    "high_match_min_score",
    "notified_total",
    "notification_score_suppressed_total",
)


def _env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _container_payload(run_id: str, *, principal_id: str, container: str) -> dict[str, Any]:
    script = (
        "import json, os, psycopg\n"
        f"run_id={run_id!r}\n"
        f"principal={principal_id!r}\n"
        "with psycopg.connect(os.environ['DATABASE_URL']) as conn:\n"
        "    with conn.cursor() as cur:\n"
        "        cur.execute('SELECT payload_json FROM property_search_runs WHERE principal_id=%s AND run_id=%s', (principal, run_id))\n"
        "        row=cur.fetchone()\n"
        "        print(json.dumps(dict(row[0] or {}) if row else {}, sort_keys=True))\n"
    )
    completed = subprocess.run(
        ["docker", "exec", container, "python", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout or "{}")
    if not isinstance(payload, dict) or not payload:
        raise RuntimeError(f"run_not_found:{run_id}")
    return payload


def _api_request(
    method: str,
    url: str,
    *,
    token: str,
    principal_id: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-EA-Principal-ID": principal_id,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"http_{exc.code}:{detail}") from exc


def _neutralize_soft_preferences(preferences: dict[str, Any]) -> dict[str, Any]:
    neutral = dict(preferences)
    raw_preferences = neutral.get("raw_preferences")
    if isinstance(raw_preferences, dict):
        neutral["raw_preferences"] = _neutralize_soft_preferences(dict(raw_preferences))
    for key in list(neutral):
        if key.startswith("max_distance_to_") and key.endswith("_importance"):
            value = str(neutral.get(key) or "").strip().lower()
            if value not in {"hard", "must_have", "must-have", "strict"}:
                neutral[key] = "neutral"
        elif key in SOFT_BOOLEAN_KEYS:
            neutral[key] = False
    return neutral


def _clear_location_scope(preferences: dict[str, Any]) -> dict[str, Any]:
    patched = dict(preferences)
    patched["location_query"] = "Austria"
    patched["custom_location_query"] = ""
    patched["region_code"] = ""
    patched["full_region_scope"] = True
    patched["selected_districts"] = []
    patched["selected_location_values"] = []
    raw_preferences = patched.get("raw_preferences")
    if isinstance(raw_preferences, dict):
        raw = dict(raw_preferences)
        raw["location_query"] = "Austria"
        raw["custom_location_query"] = ""
        raw["region_code"] = ""
        raw["full_region_scope"] = True
        raw["selected_districts"] = []
        raw["selected_location_values"] = []
        patched["raw_preferences"] = raw
    return patched


def _variant_preferences(base: dict[str, Any]) -> dict[str, dict[str, Any]]:
    neutral = _neutralize_soft_preferences(base)
    variants: dict[str, dict[str, Any]] = {"soft_neutral": neutral}
    no_location = _clear_location_scope(neutral)
    variants["no_location_hard_scope"] = no_location
    no_price = dict(neutral)
    no_price["max_price_eur"] = None
    variants["no_max_price"] = no_price
    no_area = dict(neutral)
    no_area["min_area_m2"] = 0
    variants["no_min_area"] = no_area
    no_rooms = dict(neutral)
    no_rooms["min_rooms"] = 0
    variants["no_min_rooms"] = no_rooms
    any_type = dict(neutral)
    any_type["property_type"] = "any"
    variants["any_property_type"] = any_type
    no_floorplan = dict(neutral)
    no_floorplan["require_floorplan"] = False
    no_floorplan["investment_require_floorplan"] = False
    variants["no_floorplan_requirement"] = no_floorplan
    no_availability = dict(neutral)
    no_availability["available_within_years"] = 0
    variants["no_availability_window"] = no_availability
    return variants


def _summary_counters(payload: dict[str, Any]) -> dict[str, Any]:
    summary = dict(payload.get("summary") or {})
    ranked = summary.get("ranked_candidates")
    counters = {key: summary.get(key) for key in COUNTER_KEYS if key in summary}
    counters["ranked_count"] = len(ranked) if isinstance(ranked, list) else 0
    counters["status"] = payload.get("status") or summary.get("status") or ""
    counters["message"] = payload.get("message") or ""
    return counters


def _start_variant(
    *,
    api_base: str,
    token: str,
    principal_id: str,
    selected_platforms: list[Any],
    preferences: dict[str, Any],
    max_results_per_source: int,
) -> str:
    body = {
        "selected_platforms": selected_platforms,
        "property_preferences": preferences,
        "force_refresh": False,
        "max_results_per_source": max_results_per_source,
    }
    response = _api_request(
        "POST",
        f"{api_base.rstrip('/')}/app/api/property/search-runs",
        token=token,
        principal_id=principal_id,
        body=body,
    )
    run_id = str(response.get("run_id") or "").strip()
    if not run_id:
        raise RuntimeError(f"run_start_missing_id:{response}")
    return run_id


def _poll_run(
    *,
    api_base: str,
    token: str,
    principal_id: str,
    run_id: str,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _api_request(
            "GET",
            f"{api_base.rstrip('/')}/app/api/property/search-runs/{run_id}",
            token=token,
            principal_id=principal_id,
        )
        status = str(latest.get("status") or "").strip().lower()
        counters = _summary_counters(latest)
        print(
            f"{run_id[:8]} {status or 'unknown'} "
            f"ranked={counters.get('ranked_count', 0)} "
            f"filtered={counters.get('filtered_total') or counters.get('held_back_total') or 0} "
            f"sources={counters.get('sources_completed') or 0}/{counters.get('sources_total') or 0}",
            flush=True,
        )
        if status in TERMINAL_STATUSES:
            return latest
        time.sleep(max(1.0, poll_seconds))
    latest["status"] = latest.get("status") or "timeout"
    latest["ablation_timeout"] = True
    return latest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PropertyQuarry hard-filter ablations for a saved live search.")
    parser.add_argument("--baseline-run-id", required=True)
    parser.add_argument("--principal-id", default="cf-email:tibor.girschele@gmail.com")
    parser.add_argument("--api-base", default="http://127.0.0.1:8097")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--container", default="propertyquarry-api")
    parser.add_argument("--output-dir", default="artifacts/propertyquarry_hard_filter_ablation")
    parser.add_argument("--variants", nargs="*", default=[])
    parser.add_argument("--skip-soft-neutral", action="store_true")
    parser.add_argument("--reuse-run", action="append", default=[], help="Map variant=run_id for already-started runs.")
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    args = parser.parse_args()

    env_values = {**_env_file_values(Path(args.env_file)), **os.environ}
    token = str(env_values.get("EA_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("EA_API_TOKEN missing")

    baseline = _container_payload(args.baseline_run_id, principal_id=args.principal_id, container=args.container)
    preferences = dict(baseline.get("property_search_preferences") or {})
    selected_platforms = list(baseline.get("selected_platforms") or [])
    if not preferences or not selected_platforms:
        raise RuntimeError("baseline_missing_preferences_or_platforms")
    try:
        max_results = int(preferences.get("max_results_per_source") or 8)
    except Exception:
        max_results = 8
    max_results = max(1, min(10, max_results))

    variants = _variant_preferences(preferences)
    if args.skip_soft_neutral:
        variants.pop("soft_neutral", None)
    if args.variants:
        selected = set(args.variants)
        variants = {key: value for key, value in variants.items() if key in selected}
    reuse_map: dict[str, str] = {}
    for raw in args.reuse_run:
        if "=" not in raw:
            raise RuntimeError(f"bad_reuse_run:{raw}")
        key, value = raw.split("=", 1)
        reuse_map[key.strip()] = value.strip()

    output: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline_run_id": args.baseline_run_id,
        "principal_id": args.principal_id,
        "api_base": args.api_base,
        "baseline": _summary_counters(baseline),
        "variant_order": list(variants),
        "variants": {},
    }

    for variant_key, variant_preferences in variants.items():
        print(f"== {variant_key} ==", flush=True)
        run_id = reuse_map.get(variant_key)
        if not run_id:
            variant_preferences = {
                **variant_preferences,
                "__ablation_baseline_run_id__": args.baseline_run_id,
                "__ablation_variant__": variant_key,
                "force_refresh": False,
            }
            run_id = _start_variant(
                api_base=args.api_base,
                token=token,
                principal_id=args.principal_id,
                selected_platforms=selected_platforms,
                preferences=variant_preferences,
                max_results_per_source=max_results,
            )
        payload = _poll_run(
            api_base=args.api_base,
            token=token,
            principal_id=args.principal_id,
            run_id=run_id,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        output["variants"][variant_key] = {
            "run_id": run_id,
            "counters": _summary_counters(payload),
        }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"hard_filter_ablation_{args.baseline_run_id[:8]}_{stamp}.json"
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
