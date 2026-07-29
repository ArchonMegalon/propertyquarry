#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECEIPT = Path("/data/artifacts/property-scene-video-readiness.generated.json")
FALLBACK_RECEIPT = ROOT / "_completion" / "scene_video_readiness" / "release-gate.json"
DEFAULT_OUTPUT = ROOT / "_completion" / "scene_video_readiness" / "provider-refresh-packet.json"
FILE_ENV_HOST_TARGET = "state/incoming_property_tours/_operator-import-lane/scene_video_provider_accounts"
FILE_ENV_RUNTIME_TARGET = "/data/incoming_property_tours/_operator-import-lane/scene_video_provider_accounts"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_receipt_path() -> Path:
    return DEFAULT_RECEIPT if DEFAULT_RECEIPT.exists() else FALLBACK_RECEIPT


def _load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _provider_rows(receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in list(receipt.get("providers") or []):
        if not isinstance(row, dict):
            continue
        requested = str(row.get("requested_provider") or "").strip().lower()
        if requested:
            rows[requested] = row
    return rows


def _account_inventory(row: dict[str, Any]) -> dict[str, Any]:
    inventory = row.get("account_inventory")
    return inventory if isinstance(inventory, dict) else {}


def _visible_gap(row: dict[str, Any]) -> int:
    try:
        return max(0, int(_account_inventory(row).get("visible_account_gap") or 0))
    except Exception:
        return 0


def _expected_count(row: dict[str, Any]) -> int:
    try:
        return max(0, int(_account_inventory(row).get("expected_account_count") or 0))
    except Exception:
        return 0


def _runtime_count(row: dict[str, Any]) -> int:
    try:
        return max(0, int(_account_inventory(row).get("runtime_account_count") or row.get("runtime_account_count") or 0))
    except Exception:
        return 0


def _blockers(row: dict[str, Any]) -> list[str]:
    return [
        str(value or "").strip()
        for value in list(row.get("blockers") or [])
        if str(value or "").strip()
    ]


def _credit_state(row: dict[str, Any]) -> str:
    return str(row.get("credit_state") or dict(row.get("checks") or {}).get("credit_state") or "").strip()


def _inventory_value(row: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(_account_inventory(row).get(key) or 0))
    except Exception:
        return 0


def _inventory_text(row: dict[str, Any], key: str) -> str:
    return str(_account_inventory(row).get(key) or "").strip()


def _magicfit_packet(row: dict[str, Any]) -> dict[str, Any]:
    blockers = _blockers(row)
    expected_count = _expected_count(row)
    credit_state = _credit_state(row)
    return {
        "provider": "magicfit",
        "expected_account_count": expected_count,
        "tracked_account_count": _inventory_value(row, "tracked_account_count"),
        "unavailable_account_count": _inventory_value(row, "unavailable_account_count"),
        "availability_reason": _inventory_text(row, "availability_reason"),
        "runtime_account_count": _runtime_count(row),
        "visible_account_gap": _visible_gap(row),
        "runtime_status": str(row.get("status") or ""),
        "runtime_blockers": blockers,
        "credit_state": credit_state,
        "credential_contract": {
            "preferred_accounts_json_env": "PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON",
            "fallback_accounts_json_env": "MAGICFIT_ACCOUNTS_JSON",
            "preferred_accounts_json_file_env": "PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE",
            "fallback_accounts_json_file_env": "MAGICFIT_ACCOUNTS_JSON_FILE",
            "account_selector_env": "PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX",
            "json_shape": [{"email": "<magicfit-account-email>", "password": "<magicfit-account-password>"}],
            "single_account_env_pairs": [
                ["PROPERTYQUARRY_MAGICFIT_EMAIL", "PROPERTYQUARRY_MAGICFIT_PASSWORD"],
                ["MAGICFIT_EMAIL", "MAGICFIT_PASSWORD"],
            ],
        },
        "credit_refresh_required": credit_state in {"constrained", "insufficient"} or "magicfit_insufficient_credits" in blockers,
        "proof_contract": {
            "proof_render_required": True,
            "credit_marker": "magicfit_insufficient_credits",
            "account_selector_env": "PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX",
            "credit_marker_policy": "clear magicfit_insufficient_credits only after a successful MagicFit proof render from the selected funded account returns a hosted walkthrough video",
            "proof_render_checks": [
                "selected funded account resolves through PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX or account JSON order",
                "proof receipt reports provider_backend_key=magicfit",
                "successful MagicFit proof render returns a playable hosted walkthrough video",
                "magicfit_insufficient_credits marker is cleared only after the proof render succeeds",
            ],
        },
        "post_refresh_checks": [
            "set provider account JSON file mode to 0o600 before merge",
            f"or set PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE or MAGICFIT_ACCOUNTS_JSON_FILE to the same 0o600 account JSON file with expected count {expected_count}",
            f"prefer file-env mode so merge_scene_video_provider_accounts_env.py writes the provider-only account file under {FILE_ENV_HOST_TARGET} and points runtime env to {FILE_ENV_RUNTIME_TARGET}",
            f"merge provider-only MagicFit account JSON with merge_scene_video_provider_accounts_env.py --magicfit-accounts-json-file <magicfit-accounts.json> --expected-magicfit-count {expected_count} --write-file-env --write",
            "select a funded MagicFit account with PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX before proof render",
            "run a MagicFit proof render and verify provider_backend_key=magicfit plus a playable hosted walkthrough video",
            "regenerate property_scene_video_readiness_report.py",
            "run verify_property_scene_video_readiness.py",
            "clear MagicFit credit marker only after the successful MagicFit proof render and playable hosted walkthrough video are confirmed",
        ],
        "do_not_touch": ["ONEMIN_*"],
    }


def _omagic_packet(row: dict[str, Any]) -> dict[str, Any]:
    blockers = _blockers(row)
    expected_count = _expected_count(row)
    return {
        "provider": "omagic",
        "aliases": ["magic"],
        "expected_account_count": expected_count,
        "tracked_account_count": _inventory_value(row, "tracked_account_count"),
        "unavailable_account_count": _inventory_value(row, "unavailable_account_count"),
        "availability_reason": _inventory_text(row, "availability_reason"),
        "runtime_account_count": _runtime_count(row),
        "visible_account_gap": _visible_gap(row),
        "runtime_status": str(row.get("status") or ""),
        "runtime_blockers": blockers,
        "credential_contract": {
            "preferred_accounts_json_env": "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON",
            "alias_accounts_json_env": "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON",
            "fallback_accounts_json_envs": ["OMAGIC_ACCOUNTS_JSON", "MAGIC_ACCOUNTS_JSON"],
            "preferred_accounts_json_file_env": "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON_FILE",
            "alias_accounts_json_file_env": "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON_FILE",
            "fallback_accounts_json_file_envs": ["OMAGIC_ACCOUNTS_JSON_FILE", "MAGIC_ACCOUNTS_JSON_FILE"],
            "json_shape": [{"email": "<omagic-account-email>", "password": "<omagic-account-password>"}],
            "api_key_envs": ["PROPERTYQUARRY_OMAGIC_API_KEY", "PROPERTYQUARRY_MAGIC_API_KEY"],
        },
        "adapter_contract": {
            "script": "scripts/render_omagic_property_model_walkthrough.py",
            "runtime_script": "/app/scripts/render_omagic_property_model_walkthrough.py",
            "enable_flag": "PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED",
            "render_endpoint_envs": [
                "PROPERTYQUARRY_OMAGIC_RENDER_ENDPOINT",
                "OMAGIC_RENDER_ENDPOINT",
                "PROPERTYQUARRY_MAGIC_RENDER_ENDPOINT",
                "MAGIC_RENDER_ENDPOINT",
            ],
            "render_command_envs": [
                "PROPERTYQUARRY_OMAGIC_RENDER_COMMAND",
                "OMAGIC_RENDER_COMMAND",
                "PROPERTYQUARRY_MAGIC_RENDER_COMMAND",
                "MAGIC_RENDER_COMMAND",
            ],
            "enable_after": "successful OMagic model-upload proof render",
            "proof_render_required": True,
            "proof_render_checks": [
                "real model input supplied through --model-path or --model-url",
                "adapter state reports model_input_consumed=true",
                "adapter state reports provider_backend_key=omagic",
                "hosted tour bundle contains the returned walkthrough video",
            ],
        },
        "post_refresh_checks": [
            "set provider account JSON file mode to 0o600 before merge",
            f"or set PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON_FILE and PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON_FILE to the same 0o600 account JSON file with expected count {expected_count}",
            f"prefer file-env mode so merge_scene_video_provider_accounts_env.py writes the provider-only account file under {FILE_ENV_HOST_TARGET} and points runtime env to {FILE_ENV_RUNTIME_TARGET}",
            f"merge provider-only OMagic/Magic account JSON with merge_scene_video_provider_accounts_env.py --omagic-accounts-json-file <omagic-accounts.json> --expected-omagic-count {expected_count} --write-file-env --write",
            "configure PROPERTYQUARRY_OMAGIC_RENDER_ENDPOINT or PROPERTYQUARRY_OMAGIC_RENDER_COMMAND before enabling PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED",
            "run an OMagic model-upload proof render with real model input and verify model_input_consumed=true plus provider_backend_key=omagic in the adapter state",
            "enable PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED=1 only after the OMagic model-upload proof render succeeds",
            "regenerate property_scene_video_readiness_report.py",
            "run verify_property_scene_video_readiness.py",
            "confirm magic and omagic still report provider_backend_key=omagic",
        ],
        "do_not_touch": ["ONEMIN_*"],
    }


def build_packet(receipt: dict[str, Any], *, receipt_path: Path) -> dict[str, Any]:
    rows = _provider_rows(receipt)
    magicfit = _magicfit_packet(rows.get("magicfit") or {})
    omagic_source = rows.get("omagic") or rows.get("magic") or {}
    omagic = _omagic_packet(omagic_source)
    return {
        "contract_name": "propertyquarry.scene_video_provider_refresh_packet.v1",
        "generated_at": _utc_now(),
        "release_commit_sha": str(receipt.get("release_commit_sha") or "").strip(),
        "image_digest": str(receipt.get("image_digest") or "").strip(),
        "source_receipt": str(receipt_path),
        "source_receipt_sha256": (
            _sha256(receipt_path) if receipt_path.is_file() else ""
        ),
        "source_receipt_contract_name": str(receipt.get("contract_name") or ""),
        "source_receipt_generated_at": str(receipt.get("generated_at") or ""),
        "secret_boundary": "This packet names env keys and JSON shapes only; it never contains account emails, passwords, API keys, session cookies, or 1min credentials.",
        "providers": [magicfit, omagic],
        "global_checks": [
            "do not modify ONEMIN_AI_API_KEY, ONEMIN_AI_API_KEY_FALLBACK_*, ONEMIN_DIRECT_API_KEYS_JSON, or ONEMIN_DIRECT_API_KEYS_JSON_FILE",
            "after provider refresh, regenerate the scene-video readiness receipt and verifier before running gold status",
        ],
    }


def write_packet(packet: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize a secret-safe scene-video provider refresh packet from readiness gaps.")
    parser.add_argument("--receipt", default=str(_default_receipt_path()))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    receipt_path = Path(args.receipt).expanduser()
    output_path = Path(args.output).expanduser()
    packet = build_packet(_load_json(receipt_path), receipt_path=receipt_path)
    write_packet(packet, output_path)
    print(
        json.dumps(
            {
                "status": "pass",
                "output": str(output_path),
                "provider_count": len(packet.get("providers") or []),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
