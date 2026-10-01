#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACTIVE_INTERACTIVE_PROVIDER = "3dvista"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _check(name: str, ok: bool, **details: object) -> dict[str, object]:
    return {"name": name, "ok": bool(ok), **details}


def _provider_result(receipt: dict[str, Any]) -> dict[str, Any]:
    rows = [
        dict(row)
        for row in list(receipt.get("provider_results") or [])
        if isinstance(row, dict)
        and str(row.get("provider") or "").strip().lower() == ACTIVE_INTERACTIVE_PROVIDER
    ]
    return rows[0] if len(rows) == 1 else {}


def _browser_checks(
    *,
    viewport: str,
    receipt: dict[str, Any],
    min_frame_samples: int,
    min_median_fps: float,
    max_p95_frame_interval_ms: float,
) -> list[dict[str, object]]:
    providers = [
        str(provider or "").strip().lower()
        for provider in list(receipt.get("providers") or [])
        if str(provider or "").strip()
    ]
    provider = _provider_result(receipt)
    state = dict(provider.get("state") or {})
    ux_state = dict(provider.get("ux_state") or {})
    recovery_state = dict(provider.get("recovery_state") or {})
    frame_metrics = dict(provider.get("frame_metrics") or receipt.get("frame_metrics") or {})
    errors = [
        *list(receipt.get("console_errors") or []),
        *list(receipt.get("page_errors") or []),
        *list(receipt.get("failures") or []),
    ]
    sample_count = int(frame_metrics.get("sample_count") or 0)
    median_fps = float(frame_metrics.get("median_fps") or 0.0)
    p95_interval_ms = float(frame_metrics.get("p95_frame_interval_ms") or 0.0)
    viewport_width = int(ux_state.get("viewport_width") or 0)
    scroll_width = int(ux_state.get("body_scroll_width") or 0)
    return [
        _check(f"{viewport}_browser_gate_pass", receipt.get("status") == "pass"),
        _check(
            f"{viewport}_3dvista_only_scope",
            providers == [ACTIVE_INTERACTIVE_PROVIDER],
            providers=providers,
        ),
        _check(f"{viewport}_3dvista_result_pass", provider.get("status") == "pass"),
        _check(
            f"{viewport}_same_origin_canvas_rendered",
            state.get("same_origin_frame_inspected") is True
            and int(state.get("visible_canvas_count") or 0) > 0
            and int(state.get("loading_indicator_count") or 0) == 0,
            state=state,
        ),
        _check(
            f"{viewport}_frame_samples",
            sample_count >= min_frame_samples,
            sample_count=sample_count,
            required=min_frame_samples,
        ),
        _check(
            f"{viewport}_median_fps",
            median_fps >= min_median_fps,
            median_fps=median_fps,
            required=min_median_fps,
        ),
        _check(
            f"{viewport}_p95_frame_interval",
            0.0 < p95_interval_ms <= max_p95_frame_interval_ms,
            p95_frame_interval_ms=p95_interval_ms,
            maximum=max_p95_frame_interval_ms,
        ),
        _check(f"{viewport}_runtime_errors_absent", not errors, errors=errors[:12]),
        _check(
            f"{viewport}_responsive_accessible_shell",
            viewport_width > 0
            and scroll_width <= viewport_width + 1
            and not list(ux_state.get("undersized_controls") or [])
            and ux_state.get("reduced_motion") is True,
            ux_state=ux_state,
        ),
        _check(
            f"{viewport}_recovery_controls",
            recovery_state.get("recovery_controls_ok") is True
            and recovery_state.get("retry_ready") is True
            and recovery_state.get("rendered_after_retry") is True,
            recovery_state=recovery_state,
        ),
    ]


def build_flagship_3d_launch_receipt(
    *,
    desktop_browser: dict[str, Any],
    mobile_browser: dict[str, Any],
    style_matrix: dict[str, Any],
    gold_status: dict[str, Any],
    canary: dict[str, Any],
    min_frame_samples: int = 120,
    min_desktop_median_fps: float = 55.0,
    min_mobile_median_fps: float = 45.0,
    max_p95_frame_interval_ms: float = 34.0,
    required_canary_hours: float = 48.0,
) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    checks.extend(
        _browser_checks(
            viewport="desktop",
            receipt=desktop_browser,
            min_frame_samples=min_frame_samples,
            min_median_fps=min_desktop_median_fps,
            max_p95_frame_interval_ms=max_p95_frame_interval_ms,
        )
    )
    checks.extend(
        _browser_checks(
            viewport="mobile",
            receipt=mobile_browser,
            min_frame_samples=min_frame_samples,
            min_median_fps=min_mobile_median_fps,
            max_p95_frame_interval_ms=max_p95_frame_interval_ms,
        )
    )

    style_checks = dict(style_matrix.get("checks") or {})
    checks.extend(
        [
            _check(
                "all_style_videos_verified",
                style_matrix.get("status") == "pass"
                and int(style_matrix.get("style_count") or 0) >= 5
                and int(style_matrix.get("accepted_count") or 0)
                == int(style_matrix.get("style_count") or 0)
                and style_checks.get("all_requested_styles_rendered") is True
                and style_checks.get("all_full_decodes_passed") is True
                and style_checks.get("all_visual_reviews_passed") is True,
            ),
            _check(
                "all_style_videos_delivered",
                style_checks.get("all_accepted_videos_delivered_to_telegram") is True,
            ),
            _check(
                "gold_status_pass",
                gold_status.get("status") == "pass"
                and gold_status.get("ready_for_notification") is True,
                status=str(gold_status.get("status") or "missing"),
            ),
        ]
    )
    canary_hours = float(
        canary.get("soak_hours")
        or canary.get("duration_hours")
        or canary.get("canary_hours")
        or 0.0
    )
    checks.extend(
        [
            _check("canary_receipt_pass", canary.get("status") == "pass"),
            _check(
                "canary_duration",
                canary_hours >= required_canary_hours,
                canary_hours=canary_hours,
                required_canary_hours=required_canary_hours,
            ),
            _check(
                "canary_blockers_absent",
                bool(canary)
                and not list(canary.get("blockers") or [])
                and int(canary.get("failed_count") or 0) == 0,
            ),
        ]
    )
    failed_checks = [str(row["name"]) for row in checks if not row.get("ok")]
    return {
        "contract_name": "propertyquarry.flagship_3d_launch_gate.v2",
        "generated_at": _utc_now(),
        "status": "pass" if not failed_checks else "blocked",
        "launch_ready": not failed_checks,
        "active_interactive_provider": ACTIVE_INTERACTIVE_PROVIDER,
        "historical_providers_launch_critical": [],
        "thresholds": {
            "min_frame_samples": min_frame_samples,
            "min_desktop_median_fps": min_desktop_median_fps,
            "min_mobile_median_fps": min_mobile_median_fps,
            "max_p95_frame_interval_ms": max_p95_frame_interval_ms,
            "required_canary_hours": required_canary_hours,
        },
        "failed_count": len(failed_checks),
        "blockers": failed_checks,
        "checks": checks,
        "truth_boundary": (
            "PropertyQuarry launch-critical interactive proof is 3DVista-only. Historical "
            "Matterport receipts remain audit history and are not active primary, fallback, or "
            "launch evidence. Walkthrough media is evaluated by its separate continuity and "
            "provider-proof gates."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Hard PropertyQuarry 3DVista flagship launch gate.")
    parser.add_argument("--desktop-browser", required=True)
    parser.add_argument("--mobile-browser", required=True)
    parser.add_argument("--style-matrix", required=True)
    parser.add_argument("--gold-status", required=True)
    parser.add_argument("--canary", required=True)
    parser.add_argument("--min-frame-samples", type=int, default=120)
    parser.add_argument("--min-desktop-median-fps", type=float, default=55.0)
    parser.add_argument("--min-mobile-median-fps", type=float, default=45.0)
    parser.add_argument("--max-p95-frame-interval-ms", type=float, default=34.0)
    parser.add_argument("--required-canary-hours", type=float, default=48.0)
    parser.add_argument("--write", required=True)
    args = parser.parse_args()

    receipt = build_flagship_3d_launch_receipt(
        desktop_browser=_load_json(Path(args.desktop_browser)),
        mobile_browser=_load_json(Path(args.mobile_browser)),
        style_matrix=_load_json(Path(args.style_matrix)),
        gold_status=_load_json(Path(args.gold_status)),
        canary=_load_json(Path(args.canary)),
        min_frame_samples=max(1, int(args.min_frame_samples)),
        min_desktop_median_fps=max(1.0, float(args.min_desktop_median_fps)),
        min_mobile_median_fps=max(1.0, float(args.min_mobile_median_fps)),
        max_p95_frame_interval_ms=max(1.0, float(args.max_p95_frame_interval_ms)),
        required_canary_hours=max(1.0, float(args.required_canary_hours)),
    )
    output_path = Path(args.write)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt.get("launch_ready") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
