#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CONTRACT_NAME = "propertyquarry.scene_video_readiness.v1"
VERIFIER_CONTRACT_NAME = "propertyquarry.scene_video_readiness_verifier.v1"
DEFAULT_RECEIPT = Path("/data/artifacts/property-scene-video-readiness.generated.json")
FALLBACK_RECEIPT = Path(__file__).resolve().parents[1] / "_completion" / "scene_video_readiness" / "PROPERTY_SCENE_VIDEO_READINESS.generated.json"
REQUIRED_PROVIDERS = ("mootion", "magicfit", "magic", "omagic", "onemin_i2v")
ONEMIN_PROTECTED_ACTION_REASONS = {
    "provider_account_visibility_gap",
    "magicfit_credit_constrained",
    "magicfit_insufficient_credits",
    "omagic_credentials_missing",
}


def _default_receipt_path() -> Path:
    return DEFAULT_RECEIPT if DEFAULT_RECEIPT.exists() else FALLBACK_RECEIPT


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row_by_provider(receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in list(receipt.get("providers") or []):
        if not isinstance(row, dict):
            continue
        key = str(row.get("requested_provider") or "").strip()
        if key:
            rows[key] = row
    return rows


def _actions_by_provider_reason(receipt: dict[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for action in list(receipt.get("next_actions") or []):
        if not isinstance(action, dict):
            continue
        provider = str(action.get("provider") or "").strip()
        reason = str(action.get("reason") or "").strip()
        if provider and reason:
            pairs.add((provider, reason))
    return pairs


def _actions_for_reason(receipt: dict[str, Any], reason: str) -> list[dict[str, Any]]:
    return [
        action
        for action in list(receipt.get("next_actions") or [])
        if isinstance(action, dict) and str(action.get("reason") or "").strip() == reason
    ]


def _inventory_gap(row: dict[str, Any]) -> int:
    inventory = dict(row.get("account_inventory") or {})
    try:
        return max(0, int(inventory.get("visible_account_gap") or 0))
    except Exception:
        return 0


def _require_action(blockers: list[str], receipt: dict[str, Any], provider: str, reason: str) -> None:
    if (provider, reason) not in _actions_by_provider_reason(receipt):
        blockers.append(f"next_action_missing:{provider}:{reason}")


def validate_receipt(
    receipt: dict[str, Any],
    *,
    required_providers: tuple[str, ...] = REQUIRED_PROVIDERS,
) -> dict[str, Any]:
    blockers: list[str] = []
    if receipt.get("contract_name") != CONTRACT_NAME:
        blockers.append("contract_name_mismatch")
    rows = _row_by_provider(receipt)
    for provider in required_providers:
        if provider not in rows:
            blockers.append(f"provider_row_missing:{provider}")

    telegram = dict(receipt.get("telegram_delivery_readiness") or {})
    if telegram.get("status") != "ready":
        blockers.append("telegram_not_ready")

    if "mootion" in required_providers:
        mootion = rows.get("mootion") or {}
        if mootion.get("ready") is not True:
            blockers.append("mootion_not_ready")
        if str(mootion.get("execution_lane") or "").strip() != "browseract_remote":
            blockers.append("mootion_browseract_remote_lane_missing")
            _require_action(blockers, receipt, "mootion", "mootion_browseract_remote_lane_missing")
        mootion_remote = dict(dict(mootion.get("checks") or {}).get("mootion_browseract_remote") or {})
        if mootion_remote.get("ready") is not True:
            blockers.append("mootion_browseract_bridge_not_ready")
            _require_action(blockers, receipt, "mootion", "mootion_browseract_bridge_not_ready")

    if "onemin_i2v" in required_providers:
        onemin = rows.get("onemin_i2v") or {}
        if onemin.get("ready") is not True:
            blockers.append("onemin_i2v_not_ready")
        if str(onemin.get("provider_backend_key") or "").strip() != "onemin_i2v":
            blockers.append("onemin_i2v_backend_mismatch")

    if "magicfit" in required_providers:
        magicfit = rows.get("magicfit") or {}
        if str(magicfit.get("provider_backend_key") or "").strip() != "magicfit":
            blockers.append("magicfit_backend_mismatch")
        if _inventory_gap(magicfit) > 0:
            _require_action(blockers, receipt, "magicfit", "provider_account_visibility_gap")
        credit_state = str(magicfit.get("credit_state") or dict(magicfit.get("checks") or {}).get("credit_state") or "").strip()
        if credit_state == "constrained":
            _require_action(blockers, receipt, "magicfit", "magicfit_credit_constrained")
        if "magicfit_insufficient_credits" in list(magicfit.get("blockers") or []):
            _require_action(blockers, receipt, "magicfit", "magicfit_insufficient_credits")

    for requested in tuple(
        provider for provider in ("magic", "omagic") if provider in required_providers
    ):
        row = rows.get(requested) or {}
        if str(row.get("provider_key") or "").strip() != "omagic":
            blockers.append(f"{requested}_provider_key_mismatch")
        if str(row.get("provider_backend_key") or "").strip() != "omagic":
            blockers.append(f"{requested}_backend_mismatch")
        if _inventory_gap(row) > 0:
            _require_action(blockers, receipt, requested, "provider_account_visibility_gap")
        row_blockers = list(row.get("blockers") or [])
        if "omagic_credentials_missing" in row_blockers:
            _require_action(blockers, receipt, "omagic", "omagic_credentials_missing")
        if "omagic_model_upload_adapter_missing" in row_blockers:
            _require_action(blockers, receipt, "omagic", "omagic_model_upload_adapter_missing")
        if "omagic_model_upload_adapter_disabled" in row_blockers:
            _require_action(blockers, receipt, "omagic", "omagic_model_upload_adapter_disabled")
        if "omagic_model_upload_endpoint_missing" in row_blockers:
            _require_action(blockers, receipt, "omagic", "omagic_model_upload_endpoint_missing")

    for reason in ONEMIN_PROTECTED_ACTION_REASONS:
        for action in _actions_for_reason(receipt, reason):
            protected = [str(value or "").strip() for value in list(action.get("do_not_touch") or [])]
            if "ONEMIN_*" not in protected:
                blockers.append(f"onemin_boundary_missing:{action.get('provider')}:{reason}")

    balance_ref = str(receipt.get("balance_probe_ref") or "").strip()
    balance_sha = str(receipt.get("balance_probe_sha256") or "").strip().lower()
    if balance_ref or balance_sha:
        balance_path = Path(balance_ref).expanduser()
        if not balance_path.is_file():
            blockers.append("balance_probe_receipt_missing")
        else:
            try:
                balance = json.loads(balance_path.read_text(encoding="utf-8"))
            except Exception:
                balance = {}
                blockers.append("balance_probe_receipt_unreadable")
            if _sha256(balance_path) != balance_sha:
                blockers.append("balance_probe_sha256_mismatch")
            if not isinstance(balance, dict) or balance.get("status") != "pass":
                blockers.append("balance_probe_status_not_passed")
            elif (
                balance.get("release_commit_sha") != receipt.get("release_commit_sha")
                or balance.get("image_digest") != receipt.get("image_digest")
            ):
                blockers.append("balance_probe_release_identity_mismatch")

    return {
        "contract_name": VERIFIER_CONTRACT_NAME,
        "status": "pass" if not blockers else "fail",
        "blockers": blockers,
        "provider_count": len(rows),
        "checked_providers": list(required_providers),
    }


def _emit_result(result: dict[str, Any], output_path: str = "") -> None:
    result.setdefault("generated_at", _utc_now())
    rendered = json.dumps(result, sort_keys=True)
    print(rendered)
    if output_path:
        path = Path(output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the PropertyQuarry scene-video readiness receipt invariants.")
    parser.add_argument("--receipt", default=str(_default_receipt_path()))
    parser.add_argument("--required-providers", default=",".join(REQUIRED_PROVIDERS))
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    receipt_path = Path(args.receipt).expanduser()
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        result = {"status": "fail", "blockers": [f"receipt_unreadable:{exc}"], "receipt": str(receipt_path)}
        _emit_result(result, args.output)
        return 1
    if not isinstance(receipt, dict):
        result = {"status": "fail", "blockers": ["receipt_not_object"], "receipt": str(receipt_path)}
        _emit_result(result, args.output)
        return 1
    required_providers = tuple(
        dict.fromkeys(
            provider.strip().lower()
            for provider in str(args.required_providers or "").split(",")
            if provider.strip().lower() in REQUIRED_PROVIDERS
        )
    ) or REQUIRED_PROVIDERS
    result = {
        **validate_receipt(receipt, required_providers=required_providers),
        "receipt": str(receipt_path),
        "release_commit_sha": str(receipt.get("release_commit_sha") or ""),
        "image_digest": str(receipt.get("image_digest") or ""),
        "source_receipt_sha256": _sha256(receipt_path),
    }
    _emit_result(result, args.output)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
