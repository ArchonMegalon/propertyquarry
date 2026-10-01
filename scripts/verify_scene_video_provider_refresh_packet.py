#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_PACKET = Path("/data/artifacts/property-scene-video-provider-refresh-packet.generated.json")
FALLBACK_PACKET = ROOT / "_completion" / "scene_video_readiness" / "provider-refresh-packet.json"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*(?:_\*)?$|^ONEMIN_\*$")
SENSITIVE_PATH_RE = re.compile(r"(api[_-]?key|cookie|password|secret|session|token)", re.IGNORECASE)
SAFE_ACCOUNT_MERGE_SCRIPT_NAME = "merge_scene_video_provider_accounts_env.py"
ACCOUNT_JSON_MODE_GUIDANCE = "0o600"
REQUIRED_EXPECTED_ACCOUNT_COUNTS = {"magicfit": 3, "omagic": 8}
FILE_ENV_FLAG = "--write-file-env"
FILE_ENV_HOST_TARGET = "state/incoming_property_tours/_operator-import-lane/scene_video_provider_accounts"
FILE_ENV_RUNTIME_TARGET = "/data/incoming_property_tours/_operator-import-lane/scene_video_provider_accounts"
VERIFIER_CONTRACT_NAME = (
    "propertyquarry.scene_video_provider_refresh_packet_verifier.v1"
)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_packet_path() -> Path:
    return DEFAULT_RUNTIME_PACKET if DEFAULT_RUNTIME_PACKET.exists() else FALLBACK_PACKET


def _load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _providers_by_name(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    providers: dict[str, dict[str, Any]] = {}
    for row in list(packet.get("providers") or []):
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or "").strip().lower()
        if provider:
            providers[provider] = row
    return providers


def _source_receipt_provider_rows(receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in list(receipt.get("providers") or []):
        if not isinstance(row, dict):
            continue
        requested = str(row.get("requested_provider") or "").strip().lower()
        if requested:
            rows[requested] = row
    return rows


def _int_value(row: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(row.get(key) or 0))
    except Exception:
        return 0


def _effective_tracked_account_count(row: dict[str, Any]) -> int:
    tracked = _int_value(row, "tracked_account_count")
    if tracked > 0:
        return tracked
    return _int_value(row, "expected_account_count")


def _has_onemin_boundary(row: dict[str, Any]) -> bool:
    return "ONEMIN_*" in {str(value or "").strip() for value in list(row.get("do_not_touch") or [])}


def _looks_like_safe_reference(value: str) -> bool:
    stripped = value.strip()
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    if ENV_NAME_RE.fullmatch(stripped):
        return True
    return False


def _secret_value_blockers(value: Any, *, path: str = "$") -> list[str]:
    blockers: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            blockers.extend(_secret_value_blockers(child, path=f"{path}.{key}"))
        return blockers
    if isinstance(value, list):
        for index, child in enumerate(value):
            blockers.extend(_secret_value_blockers(child, path=f"{path}[{index}]"))
        return blockers
    if not isinstance(value, str):
        return blockers

    if EMAIL_RE.search(value):
        blockers.append(f"packet_contains_real_email:{path}")
    if re.search(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}", value, re.IGNORECASE):
        blockers.append(f"packet_contains_auth_header:{path}")
    if path.endswith(".secret_boundary"):
        return blockers
    if SENSITIVE_PATH_RE.search(path) and value and not _looks_like_safe_reference(value):
        blockers.append(f"packet_contains_secret_value:{path}")
    return blockers


def _validate_gap(provider: str, row: dict[str, Any]) -> list[str]:
    expected = _int_value(row, "expected_account_count")
    runtime = _int_value(row, "runtime_account_count")
    visible_gap = _int_value(row, "visible_account_gap")
    calculated_gap = max(0, expected - runtime)
    if visible_gap != calculated_gap:
        return [f"{provider}_visible_account_gap_mismatch"]
    return []


def _validate_tracked_inventory(provider: str, row: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    expected = _int_value(row, "expected_account_count")
    tracked = _effective_tracked_account_count(row)
    unavailable = _int_value(row, "unavailable_account_count")
    availability_reason = str(row.get("availability_reason") or "").strip()
    if tracked < REQUIRED_EXPECTED_ACCOUNT_COUNTS[provider]:
        blockers.append(f"{provider}_expected_account_count_below_required")
    if tracked and expected and expected > tracked:
        blockers.append(f"{provider}_expected_account_count_exceeds_tracked_inventory")
    if tracked > expected:
        if unavailable != tracked - expected:
            blockers.append(f"{provider}_unavailable_account_count_mismatch")
        if not availability_reason:
            blockers.append(f"{provider}_availability_reason_missing")
    return blockers


def _post_refresh_guidance(row: dict[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in list(row.get("post_refresh_checks") or [])
    )


def _has_safe_account_merge_guidance(row: dict[str, Any]) -> bool:
    guidance = _post_refresh_guidance(row)
    return SAFE_ACCOUNT_MERGE_SCRIPT_NAME in guidance and "--write" in guidance and FILE_ENV_FLAG in guidance


def _has_secure_account_json_guidance(row: dict[str, Any]) -> bool:
    guidance = _post_refresh_guidance(row)
    return ACCOUNT_JSON_MODE_GUIDANCE in guidance and "before merge" in guidance


def _merge_guidance_blockers(provider: str, row: dict[str, Any]) -> list[str]:
    guidance = _post_refresh_guidance(row)
    blockers: list[str] = []
    if FILE_ENV_FLAG not in guidance:
        blockers.append(f"{provider}_write_file_env_flag_missing")
    if FILE_ENV_HOST_TARGET not in guidance:
        blockers.append(f"{provider}_host_file_env_target_guidance_missing")
    if FILE_ENV_RUNTIME_TARGET not in guidance:
        blockers.append(f"{provider}_runtime_file_env_target_guidance_missing")
    if provider == "magicfit":
        if "--magicfit-accounts-json-file" not in guidance:
            blockers.append("magicfit_account_json_file_flag_missing")
        expected_flag = f"--expected-magicfit-count {_int_value(row, 'expected_account_count')}"
        if expected_flag not in guidance:
            blockers.append("magicfit_expected_account_count_guard_missing")
    if provider == "omagic":
        if "--omagic-accounts-json-file" not in guidance:
            blockers.append("omagic_account_json_file_flag_missing")
        expected_flag = f"--expected-omagic-count {_int_value(row, 'expected_account_count')}"
        if expected_flag not in guidance:
            blockers.append("omagic_expected_account_count_guard_missing")
    return blockers


def _source_receipt_truth_blockers(packet: dict[str, Any]) -> list[str]:
    receipt_ref = Path(str(packet.get("source_receipt") or "").strip()).expanduser()
    if not str(receipt_ref):
        return ["source_receipt_missing"]
    if not receipt_ref.is_file():
        return [f"source_receipt_unavailable:{receipt_ref}"]
    try:
        source_receipt = _load_json(receipt_ref)
    except Exception as exc:
        return [f"source_receipt_unreadable:{type(exc).__name__}"]

    blockers: list[str] = []
    packet_contract_name = str(packet.get("source_receipt_contract_name") or "").strip()
    source_contract_name = str(source_receipt.get("contract_name") or "").strip()
    if packet_contract_name != source_contract_name:
        blockers.append("source_receipt_contract_name_mismatch")

    packet_generated_at = str(packet.get("source_receipt_generated_at") or "").strip()
    source_generated_at = str(source_receipt.get("generated_at") or "").strip()
    if packet_generated_at != source_generated_at:
        blockers.append("source_receipt_generated_at_mismatch")
    if str(packet.get("source_receipt_sha256") or "").strip().lower() != _sha256(
        receipt_ref
    ):
        blockers.append("source_receipt_sha256_mismatch")
    if (
        str(packet.get("release_commit_sha") or "").strip()
        != str(source_receipt.get("release_commit_sha") or "").strip()
        or str(packet.get("image_digest") or "").strip()
        != str(source_receipt.get("image_digest") or "").strip()
    ):
        blockers.append("source_receipt_release_identity_mismatch")

    source_rows = _source_receipt_provider_rows(source_receipt)
    providers = _providers_by_name(packet)
    source_row_specs = {
        "magicfit": source_rows.get("magicfit") or {},
        "omagic": source_rows.get("omagic") or source_rows.get("magic") or {},
    }
    for provider, packet_row in providers.items():
        if provider not in source_row_specs:
            continue
        source_row = source_row_specs.get(provider) or {}
        if not source_row:
            blockers.append(f"{provider}_source_receipt_provider_missing")
            continue
        packet_status = str(packet_row.get("runtime_status") or "").strip()
        source_status = str(source_row.get("status") or "").strip()
        if packet_status != source_status:
            blockers.append(f"{provider}_runtime_status_mismatch_with_source_receipt")

        packet_runtime_count = _int_value(packet_row, "runtime_account_count")
        source_runtime_count = _int_value(source_row, "runtime_account_count")
        if packet_runtime_count != source_runtime_count:
            blockers.append(f"{provider}_runtime_account_count_mismatch_with_source_receipt")

        packet_blockers = [
            str(value or "").strip()
            for value in list(packet_row.get("runtime_blockers") or [])
            if str(value or "").strip()
        ]
        source_blockers = [
            str(value or "").strip()
            for value in list(source_row.get("blockers") or [])
            if str(value or "").strip()
        ]
        if packet_blockers != source_blockers:
            blockers.append(f"{provider}_runtime_blockers_mismatch_with_source_receipt")
    return blockers


def _safe_account_merge_script_path() -> Path:
    return ROOT / "scripts" / SAFE_ACCOUNT_MERGE_SCRIPT_NAME


def verify_packet(packet: dict[str, Any], *, packet_path: str | None = None) -> dict[str, Any]:
    blockers: list[str] = []
    if packet.get("contract_name") != "propertyquarry.scene_video_provider_refresh_packet.v1":
        blockers.append("invalid_contract_name")

    safe_merge_script_path = _safe_account_merge_script_path()
    if not safe_merge_script_path.is_file():
        blockers.append("safe_env_merge_script_missing")

    blockers.extend(_secret_value_blockers(packet))

    rendered = json.dumps(packet, sort_keys=True)
    if "ONEMIN_AI_API_KEY" not in rendered or "ONEMIN_DIRECT_API_KEYS_JSON" not in rendered:
        blockers.append("global_onemin_no_touch_keys_missing")

    blockers.extend(_source_receipt_truth_blockers(packet))

    providers = _providers_by_name(packet)
    for provider in ("magicfit", "omagic"):
        row = providers.get(provider)
        if not row:
            blockers.append(f"{provider}_provider_missing")
            continue
        if not _has_onemin_boundary(row):
            blockers.append(f"{provider}_onemin_boundary_missing")
        if not _has_safe_account_merge_guidance(row):
            blockers.append(f"{provider}_safe_env_merge_guidance_missing")
        if not _has_secure_account_json_guidance(row):
            blockers.append(f"{provider}_secure_account_json_mode_guidance_missing")
        blockers.extend(_merge_guidance_blockers(provider, row))
        blockers.extend(_validate_gap(provider, row))
        blockers.extend(_validate_tracked_inventory(provider, row))

    magicfit = providers.get("magicfit") or {}
    magicfit_contract = magicfit.get("credential_contract") if isinstance(magicfit.get("credential_contract"), dict) else {}
    if magicfit_contract.get("preferred_accounts_json_env") != "PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON":
        blockers.append("magicfit_preferred_accounts_env_missing")
    if magicfit_contract.get("fallback_accounts_json_env") != "MAGICFIT_ACCOUNTS_JSON":
        blockers.append("magicfit_fallback_accounts_env_missing")
    if magicfit_contract.get("account_selector_env") != "PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX":
        blockers.append("magicfit_account_selector_env_missing")
    magicfit_proof = magicfit.get("proof_contract") if isinstance(magicfit.get("proof_contract"), dict) else {}
    if magicfit_proof.get("proof_render_required") is not True:
        blockers.append("magicfit_proof_render_required_missing")
    if magicfit_proof.get("credit_marker") != "magicfit_insufficient_credits":
        blockers.append("magicfit_credit_marker_contract_missing")
    if magicfit_proof.get("account_selector_env") != "PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX":
        blockers.append("magicfit_proof_account_selector_env_missing")
    credit_policy = str(magicfit_proof.get("credit_marker_policy") or "")
    for required_token, blocker in (
        ("magicfit_insufficient_credits", "magicfit_credit_marker_policy_marker_missing"),
        ("after", "magicfit_credit_marker_policy_order_missing"),
        ("proof render", "magicfit_credit_marker_policy_proof_missing"),
        ("hosted walkthrough video", "magicfit_credit_marker_policy_hosted_video_missing"),
    ):
        if required_token not in credit_policy:
            blockers.append(blocker)
    magicfit_proof_checks = " ".join(str(value or "") for value in list(magicfit_proof.get("proof_render_checks") or []))
    for required_token, blocker in (
        ("PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX", "magicfit_selected_account_proof_check_missing"),
        ("provider_backend_key=magicfit", "magicfit_backend_proof_check_missing"),
        ("hosted walkthrough video", "magicfit_hosted_video_proof_check_missing"),
        ("magicfit_insufficient_credits", "magicfit_credit_marker_proof_check_missing"),
    ):
        if required_token not in magicfit_proof_checks:
            blockers.append(blocker)
    magicfit_guidance = _post_refresh_guidance(magicfit)
    for required_token, blocker in (
        ("PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX", "magicfit_account_selection_guidance_missing"),
        ("proof render", "magicfit_proof_render_guidance_missing"),
        ("provider_backend_key=magicfit", "magicfit_backend_proof_guidance_missing"),
        ("playable hosted walkthrough video", "magicfit_hosted_video_guidance_missing"),
        ("clear MagicFit credit marker only after", "magicfit_credit_marker_after_proof_guidance_missing"),
    ):
        if required_token not in magicfit_guidance:
            blockers.append(blocker)

    omagic = providers.get("omagic") or {}
    aliases = {str(value or "").strip().lower() for value in list(omagic.get("aliases") or [])}
    omagic_contract = omagic.get("credential_contract") if isinstance(omagic.get("credential_contract"), dict) else {}
    adapter_contract = omagic.get("adapter_contract") if isinstance(omagic.get("adapter_contract"), dict) else {}
    if "magic" not in aliases:
        blockers.append("omagic_magic_alias_missing")
    if omagic_contract.get("preferred_accounts_json_env") != "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON":
        blockers.append("omagic_preferred_accounts_env_missing")
    if omagic_contract.get("alias_accounts_json_env") != "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON":
        blockers.append("omagic_magic_alias_accounts_env_missing")
    if adapter_contract.get("enable_flag") != "PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED":
        blockers.append("omagic_enable_flag_missing")
    if adapter_contract.get("runtime_script") != "/app/scripts/render_omagic_property_model_walkthrough.py":
        blockers.append("omagic_runtime_adapter_script_missing")
    endpoint_envs = {str(value or "").strip() for value in list(adapter_contract.get("render_endpoint_envs") or [])}
    command_envs = {str(value or "").strip() for value in list(adapter_contract.get("render_command_envs") or [])}
    if "PROPERTYQUARRY_OMAGIC_RENDER_ENDPOINT" not in endpoint_envs:
        blockers.append("omagic_primary_render_endpoint_env_missing")
    if "PROPERTYQUARRY_OMAGIC_RENDER_COMMAND" not in command_envs:
        blockers.append("omagic_primary_render_command_env_missing")
    if adapter_contract.get("proof_render_required") is not True:
        blockers.append("omagic_proof_render_required_missing")
    proof_checks = " ".join(str(value or "") for value in list(adapter_contract.get("proof_render_checks") or []))
    for required_token, blocker in (
        ("model_input_consumed=true", "omagic_model_input_consumption_check_missing"),
        ("provider_backend_key=omagic", "omagic_backend_proof_check_missing"),
        ("walkthrough video", "omagic_hosted_video_proof_check_missing"),
    ):
        if required_token not in proof_checks:
            blockers.append(blocker)
    omagic_guidance = _post_refresh_guidance(omagic)
    for required_token, blocker in (
        ("PROPERTYQUARRY_OMAGIC_RENDER_ENDPOINT", "omagic_endpoint_config_guidance_missing"),
        ("PROPERTYQUARRY_OMAGIC_RENDER_COMMAND", "omagic_command_config_guidance_missing"),
        ("PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED=1 only after", "omagic_enable_after_proof_guidance_missing"),
        ("model_input_consumed=true", "omagic_model_input_proof_guidance_missing"),
        ("provider_backend_key=omagic", "omagic_backend_proof_guidance_missing"),
    ):
        if required_token not in omagic_guidance:
            blockers.append(blocker)

    status = "fail" if blockers else "pass"
    receipt: dict[str, Any] = {
        "contract_name": VERIFIER_CONTRACT_NAME,
        "generated_at": _utc_now(),
        "status": status,
        "release_commit_sha": str(packet.get("release_commit_sha") or "").strip(),
        "image_digest": str(packet.get("image_digest") or "").strip(),
        "blockers": blockers,
        "checked_providers": sorted(providers),
        "provider_count": len(providers),
        "safe_env_merge_script": str(safe_merge_script_path),
    }
    if packet_path:
        receipt["packet"] = packet_path
        packet_file = Path(packet_path).expanduser()
        if packet_file.is_file():
            receipt["source_packet_sha256"] = _sha256(packet_file)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a secret-safe scene-video provider refresh packet.")
    parser.add_argument("--packet", default=str(_default_packet_path()))
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    packet_path = Path(args.packet).expanduser()
    try:
        packet = _load_json(packet_path)
        receipt = verify_packet(packet, packet_path=str(packet_path))
    except Exception as exc:
        receipt = {
            "contract_name": VERIFIER_CONTRACT_NAME,
            "generated_at": _utc_now(),
            "status": "fail",
            "blockers": [f"packet_load_failed:{exc.__class__.__name__}"],
            "checked_providers": [],
            "provider_count": 0,
            "packet": str(packet_path),
        }

    rendered = json.dumps(receipt, sort_keys=True)
    print(rendered)
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
