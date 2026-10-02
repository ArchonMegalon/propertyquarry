#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
for candidate in (SCRIPTS_DIR, ROOT / "ea", ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from property_scene_video_readiness_report import DEFAULT_PROVIDERS, build_report  # noqa: E402


def _csv_values(raw: str) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for item in str(raw or "").split(","):
        value = item.strip()
        if value and value not in seen:
            values.append(value)
            seen.add(value)
    return tuple(values)


def _load_receipt(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return dict(loaded) if isinstance(loaded, dict) else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_live_report(*, providers: tuple[str, ...], load_shared_env: bool) -> dict[str, Any]:
    if load_shared_env:
        from property_scene_video_shared_env import load_shared_env as load_scene_video_shared_env

        load_scene_video_shared_env()
    return build_report(providers=providers)


def _severity_rank(value: object) -> int:
    normalized = str(value or "").strip().lower()
    if normalized == "high":
        return 3
    if normalized == "medium":
        return 2
    if normalized == "low":
        return 1
    return 0


def _next_action_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    best_by_provider: dict[str, dict[str, Any]] = {}
    for raw_action in list(report.get("next_actions") or []):
        if not isinstance(raw_action, dict):
            continue
        action = dict(raw_action)
        provider = str(action.get("provider") or "").strip()
        if not provider:
            continue
        current = best_by_provider.get(provider)
        if current is None or _severity_rank(action.get("severity")) > _severity_rank(current.get("severity")):
            best_by_provider[provider] = action
    return best_by_provider


def build_runtime_status(
    report: dict[str, Any],
    *,
    source_kind: str,
    source_ref: str,
    source_receipt_sha256: str = "",
) -> dict[str, Any]:
    generated_at = str(report.get("generated_at") or "").strip()
    next_action_by_provider = _next_action_map(report)
    rows: list[dict[str, Any]] = []
    action_required_providers: list[str] = []
    blocked_providers: list[str] = []
    for raw_row in list(report.get("providers") or []):
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        provider = str(row.get("requested_provider") or row.get("provider_key") or "").strip() or "unknown"
        provider_key = str(row.get("provider_key") or provider).strip() or provider
        action = next_action_by_provider.get(provider) or next_action_by_provider.get(provider_key)
        blockers = [str(value or "").strip() for value in list(row.get("blockers") or []) if str(value or "").strip()]
        account_inventory = dict(row.get("account_inventory") or {})
        normalized = {
            "provider": provider,
            "provider_key": provider_key,
            "provider_backend_key": str(row.get("provider_backend_key") or "").strip(),
            "status": str(row.get("status") or ("ready" if row.get("ready") else "blocked")).strip() or "blocked",
            "ready": bool(row.get("ready")),
            "attention_required": bool(action),
            "updated_at": generated_at,
            "source": source_kind,
            "source_ref": source_ref,
            "execution_lane": str(row.get("execution_lane") or row.get("provider_backend_key") or provider_key).strip(),
            "runtime_account_count": row.get("runtime_account_count"),
            "credit_state": row.get("credit_state"),
            "quota_capability_state": row.get("quota_capability_state"),
            "blocking_reason": blockers[0] if blockers else "",
            "blockers": blockers,
            "progress_pct": 100 if bool(row.get("ready")) else 0,
        }
        if account_inventory:
            normalized["expected_account_count"] = account_inventory.get("expected_account_count")
            normalized["tracked_account_count"] = account_inventory.get("tracked_account_count")
            normalized["unavailable_account_count"] = account_inventory.get("unavailable_account_count")
            normalized["availability_reason"] = account_inventory.get("availability_reason")
            normalized["visible_account_gap"] = account_inventory.get("visible_account_gap")
            normalized["account_inventory_status"] = account_inventory.get("status")
        if action:
            normalized["next_action"] = str(action.get("action") or "").strip()
            normalized["next_action_reason"] = str(action.get("reason") or "").strip()
            normalized["next_action_severity"] = str(action.get("severity") or "").strip()
            action_required_providers.append(provider)
        if normalized["ready"] is not True:
            blocked_providers.append(provider)
        rows.append(normalized)
    delivery_row: dict[str, Any] = {}
    telegram_readiness = dict(report.get("telegram_delivery_readiness") or {})
    telegram_action = next_action_by_provider.get("telegram")
    if telegram_readiness:
        blockers = [str(value or "").strip() for value in list(telegram_readiness.get("blockers") or []) if str(value or "").strip()]
        delivery_row = {
            "transport": "telegram",
            "status": str(telegram_readiness.get("status") or "blocked").strip() or "blocked",
            "configured": bool(telegram_readiness.get("configured")),
            "updated_at": generated_at,
            "blocking_reason": blockers[0] if blockers else "",
            "blockers": blockers,
        }
        if telegram_action:
            delivery_row["next_action"] = str(telegram_action.get("action") or "").strip()
            delivery_row["next_action_reason"] = str(telegram_action.get("reason") or "").strip()
            delivery_row["next_action_severity"] = str(telegram_action.get("severity") or "").strip()
    summary = {
        "provider_count": len(rows),
        "ready_count": sum(1 for row in rows if row.get("ready") is True),
        "blocked_count": len(blocked_providers),
        "blocked_providers": blocked_providers,
        "action_required_count": len(action_required_providers),
        "action_required_providers": action_required_providers,
        "delivery_ready": not delivery_row or delivery_row.get("status") == "ready",
    }
    return {
        "contract_name": "propertyquarry.scene_video_runtime_status.v1",
        "generated_at": generated_at,
        "release_commit_sha": str(report.get("release_commit_sha") or "").strip(),
        "image_digest": str(report.get("image_digest") or "").strip(),
        "source_receipt_sha256": str(source_receipt_sha256 or "").strip().lower(),
        "source_contract_name": str(report.get("contract_name") or "").strip(),
        "source_kind": source_kind,
        "source_ref": source_ref,
        "summary": summary,
        "providers": rows,
        "delivery": delivery_row,
        "secret_boundary": "This status view is derived from the secret-safe readiness receipt and exposes factual runtime states only.",
    }


def render_operator_status(status: dict[str, Any]) -> str:
    summary = dict(status.get("summary") or {})
    lines = [
        f"Scene-video runtime status @ {str(status.get('generated_at') or '').strip()}",
        f"Source: {str(status.get('source_kind') or '').strip()} {str(status.get('source_ref') or '').strip()}".strip(),
        (
            "Summary: "
            f"ready {int(summary.get('ready_count') or 0)}/{int(summary.get('provider_count') or 0)}"
            f" | blocked {int(summary.get('blocked_count') or 0)}"
            f" | action_required {int(summary.get('action_required_count') or 0)}"
        ),
    ]
    delivery = dict(status.get("delivery") or {})
    if delivery:
        delivery_line = f"telegram | {str(delivery.get('status') or 'unknown').strip()}"
        if str(delivery.get("blocking_reason") or "").strip():
            delivery_line = f"{delivery_line} | blocker={str(delivery.get('blocking_reason') or '').strip()}"
        if str(delivery.get("next_action_reason") or "").strip():
            delivery_line = f"{delivery_line} | next={str(delivery.get('next_action_reason') or '').strip()}"
        lines.append(delivery_line)
    for raw_row in list(status.get("providers") or []):
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        parts = [
            str(row.get("provider") or row.get("provider_key") or "unknown").strip(),
            str(row.get("status") or "unknown").strip(),
        ]
        execution_lane = str(row.get("execution_lane") or "").strip()
        if execution_lane:
            parts.append(f"lane={execution_lane}")
        runtime_account_count = row.get("runtime_account_count")
        expected_account_count = row.get("expected_account_count")
        visible_account_gap = row.get("visible_account_gap")
        tracked_account_count = row.get("tracked_account_count")
        unavailable_account_count = row.get("unavailable_account_count")
        if expected_account_count not in (None, ""):
            parts.append(f"accounts={runtime_account_count}/{expected_account_count}")
        elif runtime_account_count not in (None, ""):
            parts.append(f"accounts={runtime_account_count}")
        if tracked_account_count not in (None, "", 0) and tracked_account_count != expected_account_count:
            parts.append(f"tracked={tracked_account_count}")
        if unavailable_account_count not in (None, "", 0):
            parts.append(f"unavailable={unavailable_account_count}")
        if visible_account_gap not in (None, "", 0):
            parts.append(f"gap={visible_account_gap}")
        credit_state = str(row.get("credit_state") or "").strip()
        if credit_state:
            parts.append(f"credit={credit_state}")
        blocking_reason = str(row.get("blocking_reason") or "").strip()
        if blocking_reason:
            parts.append(f"blocker={blocking_reason}")
        next_action_reason = str(row.get("next_action_reason") or "").strip()
        if next_action_reason:
            parts.append(f"next={next_action_reason}")
        lines.append(" | ".join(parts))
    return "\n".join(lines) + "\n"


def _emit_output(payload: str, output_path: Path | None) -> None:
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
    print(payload, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a compact, operator-safe PropertyQuarry scene-video runtime status view.")
    parser.add_argument("--providers", default=",".join(DEFAULT_PROVIDERS))
    parser.add_argument("--receipt", default="")
    parser.add_argument(
        "--load-shared-env",
        dest="load_shared_env",
        action="store_true",
        default=None,
        help="Load the generated shared scene-video env bridge before probing live runtime readiness.",
    )
    parser.add_argument(
        "--no-load-shared-env",
        dest="load_shared_env",
        action="store_false",
        help="Skip loading the generated shared scene-video env bridge before probing live runtime readiness.",
    )
    parser.add_argument("--format", choices=("json", "operator"), default="json")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    receipt_path = Path(str(args.receipt or "").strip()).expanduser() if str(args.receipt or "").strip() else None
    if receipt_path is not None:
        report = _load_receipt(receipt_path)
        source_kind = "receipt_file"
        source_ref = str(receipt_path)
        source_receipt_sha256 = _sha256(receipt_path)
    else:
        providers = _csv_values(args.providers) or DEFAULT_PROVIDERS
        load_shared_env = True if args.load_shared_env is None else bool(args.load_shared_env)
        report = _build_live_report(providers=providers, load_shared_env=load_shared_env)
        source_kind = "live_runtime"
        source_ref = "property_scene_video_readiness_report.build_report"
        source_receipt_sha256 = ""
    status = build_runtime_status(
        report,
        source_kind=source_kind,
        source_ref=source_ref,
        source_receipt_sha256=source_receipt_sha256,
    )
    output_path = Path(str(args.output or "").strip()).expanduser() if str(args.output or "").strip() else None
    if args.format == "operator":
        _emit_output(render_operator_status(status), output_path)
    else:
        _emit_output(json.dumps(status, indent=2, sort_keys=True) + "\n", output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
