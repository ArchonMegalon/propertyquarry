#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / "ea", ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app.services.scene_video_contract import scene_video_provider_runtime_readiness  # noqa: E402
from app.mootion_remote_asset_policy import mootion_remote_asset_host_policy_readiness  # noqa: E402


DEFAULT_PROVIDERS = ("mootion", "magicfit", "magic", "omagic", "onemin_i2v")
SAFE_CHECK_KEYS = {
    "account_config_env_names",
    "account_config_scope",
    "backend_adapter_key",
    "credit_marker_path",
    "credit_probe_source",
    "credit_state",
    "quota_capability_state",
    "balance_probe_status",
    "credentials_configured",
    "docker_cli_configured",
    "docker_cli_path",
    "docker_daemon_detail",
    "docker_daemon_ready",
    "docker_socket_configured",
    "docker_socket_detail",
    "docker_socket_path",
    "last_failure_at",
    "last_failure_reason",
    "minimum_required_credits",
    "model_upload_adapter_enabled",
    "model_upload_adapter_target_configured",
    "model_upload_command_env_names",
    "model_upload_endpoint_env_names",
    "model_upload_supported",
    "mootion_browseract_remote",
    "mootion_execution_lane",
    "mootion_local_worker_blockers",
    "public_provider_key",
    "runtime_account_count",
    "runtime_account_email_env_names",
    "runtime_api_key_env_names",
    "script_exists",
    "script_path",
}
MOOTION_LOCAL_WORKER_BLOCKERS = {
    "mootion_docker_socket_missing",
    "mootion_docker_cli_missing",
    "mootion_docker_daemon_unavailable",
}
MOOTION_BROWSERACT_WORKFLOW_KEYS = (
    "mootion_movie_workflow_id",
    "browseract_mootion_movie_workflow_id",
    "workflow_id",
)
MOOTION_BROWSERACT_RUN_URL_KEYS = (
    "mootion_movie_run_url",
    "browseract_mootion_movie_run_url",
    "run_url",
)
MOOTION_BROWSERACT_PRINCIPAL_ENV_NAMES = (
    "PROPERTYQUARRY_SCENE_VIDEO_PRINCIPAL_ID",
    "EA_TELEGRAM_DEFAULT_PRINCIPAL_ID",
    "EA_DEFAULT_PRINCIPAL_ID",
)
TELEGRAM_TOKEN_ENV_NAMES = (
    "PROPERTYQUARRY_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "EA_TELEGRAM_BOT_TOKEN",
)
TELEGRAM_CHAT_ENV_NAMES = (
    "PROPERTYQUARRY_TELEGRAM_CHAT_ID",
    "TELEGRAM_CHAT_ID",
    "EA_TELEGRAM_CHAT_ID",
    "EA_TELEGRAM_DEFAULT_CHAT_ID",
    "EA_PROACTIVE_OODA_TELEGRAM_CHAT_ID",
)
TELEGRAM_ROUTE_ENV_NAMES = (
    "EA_TELEGRAM_DEFAULT_PRINCIPAL_ID",
    "EA_DEFAULT_PRINCIPAL_ID",
    "EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT",
)
EXPECTED_ACCOUNT_COUNT_JSON_ENV = "PROPERTYQUARRY_SCENE_VIDEO_EXPECTED_ACCOUNT_COUNTS_JSON"
EXPECTED_ACCOUNT_COUNT_FILE_ENV = "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_INVENTORY_FILE"
ACCOUNT_INVENTORY_METADATA_KEYS = (
    "tracked_account_count",
    "unavailable_account_count",
    "availability_reason",
)
EXPECTED_ACCOUNT_COUNT_ENV_NAMES = {
    "magicfit": (
        "PROPERTYQUARRY_MAGICFIT_EXPECTED_ACCOUNT_COUNT",
        "MAGICFIT_EXPECTED_ACCOUNT_COUNT",
    ),
    "omagic": (
        "PROPERTYQUARRY_OMAGIC_EXPECTED_ACCOUNT_COUNT",
        "OMAGIC_EXPECTED_ACCOUNT_COUNT",
        "PROPERTYQUARRY_MAGIC_EXPECTED_ACCOUNT_COUNT",
        "MAGIC_EXPECTED_ACCOUNT_COUNT",
    ),
    "magic": (
        "PROPERTYQUARRY_MAGIC_EXPECTED_ACCOUNT_COUNT",
        "MAGIC_EXPECTED_ACCOUNT_COUNT",
        "PROPERTYQUARRY_OMAGIC_EXPECTED_ACCOUNT_COUNT",
        "OMAGIC_EXPECTED_ACCOUNT_COUNT",
    ),
}


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return dict(loaded) if isinstance(loaded, dict) else {}


def _apply_balance_probe(
    rows: list[dict[str, Any]],
    balance_probe: dict[str, Any],
) -> None:
    if balance_probe.get("status") != "pass":
        return
    results = {
        str(row.get("provider") or "").strip().lower(): dict(row)
        for row in list(balance_probe.get("provider_results") or [])
        if isinstance(row, dict) and row.get("status") == "pass"
    }
    for row in rows:
        requested = str(row.get("requested_provider") or "").strip().lower()
        provider = "omagic" if requested in {"magic", "omagic"} else requested
        balance = results.get(provider)
        if balance is None:
            continue
        credit_state = str(balance.get("credit_state") or "").strip().lower()
        capability_state = str(balance.get("capability_state") or "").strip()
        if credit_state:
            row["credit_state"] = credit_state
        if capability_state:
            row["quota_capability_state"] = capability_state
        checks = dict(row.get("checks") or {})
        checks["balance_probe_status"] = "pass"
        if credit_state:
            checks["credit_state"] = credit_state
        if capability_state:
            checks["quota_capability_state"] = capability_state
        row["checks"] = checks


def _default_output_path() -> Path:
    configured = str(os.getenv("PROPERTYQUARRY_SCENE_VIDEO_READINESS_RECEIPT") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if Path("/data/artifacts").exists():
        return Path("/data/artifacts/property-scene-video-readiness.generated.json")
    return ROOT / "_completion" / "scene_video_readiness" / "PROPERTY_SCENE_VIDEO_READINESS.generated.json"


def _csv_values(raw: str) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for item in str(raw or "").split(","):
        value = item.strip()
        if value and value not in seen:
            values.append(value)
            seen.add(value)
    return tuple(values)


def _env_names_configured(names: tuple[str, ...]) -> list[str]:
    return [name for name in names if str(os.getenv(name) or "").strip()]


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value or "").strip())
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _expected_account_count_files() -> tuple[Path, ...]:
    configured = str(os.getenv(EXPECTED_ACCOUNT_COUNT_FILE_ENV) or "").strip()
    if configured:
        return (Path(configured).expanduser(),)
    return (
        Path("/config/scene_video_provider_inventory.json"),
        ROOT / "config" / "scene_video_provider_inventory.json",
    )


def _extract_account_counts(payload: object, *, source: str) -> dict[str, tuple[int, str]]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, tuple[int, str]] = {}

    def add_count(key: object, value: object) -> None:
        normalized_key = str(key or "").strip().lower()
        count = _positive_int(value)
        if normalized_key and count is not None:
            result[normalized_key] = (count, source)

    providers = payload.get("providers")
    if isinstance(providers, dict):
        for key, row in providers.items():
            if isinstance(row, dict):
                count = row.get("expected_account_count") or row.get("account_count")
                add_count(key, count)
                for alias in list(row.get("aliases") or []):
                    add_count(alias, count)
            else:
                add_count(key, row)
    elif isinstance(providers, list):
        for row in providers:
            if not isinstance(row, dict):
                continue
            count = row.get("expected_account_count") or row.get("account_count")
            keys = [
                row.get("provider_key"),
                row.get("provider"),
                row.get("name"),
                *list(row.get("aliases") or []),
            ]
            for key in keys:
                add_count(key, count)

    for key, value in payload.items():
        if str(key or "").strip().lower() in {"contract_name", "generated_at", "providers", "notes"}:
            continue
        add_count(key, value)
    return result


def _extract_account_inventory_metadata(payload: object, *, source: str) -> dict[str, tuple[dict[str, Any], str]]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, tuple[dict[str, Any], str]] = {}

    def add_metadata(key: object, row: object) -> None:
        normalized_key = str(key or "").strip().lower()
        if not normalized_key or not isinstance(row, dict):
            return
        metadata: dict[str, Any] = {}
        tracked_count = _positive_int(row.get("tracked_account_count"))
        if tracked_count is not None:
            metadata["tracked_account_count"] = tracked_count
        unavailable_count = _positive_int(row.get("unavailable_account_count"))
        if unavailable_count is not None:
            metadata["unavailable_account_count"] = unavailable_count
        availability_reason = str(row.get("availability_reason") or "").strip()
        if availability_reason:
            metadata["availability_reason"] = availability_reason
        if metadata:
            result[normalized_key] = (metadata, source)

    providers = payload.get("providers")
    if isinstance(providers, dict):
        for key, row in providers.items():
            add_metadata(key, row)
            if isinstance(row, dict):
                for alias in list(row.get("aliases") or []):
                    add_metadata(alias, row)
    elif isinstance(providers, list):
        for row in providers:
            if not isinstance(row, dict):
                continue
            keys = [
                row.get("provider_key"),
                row.get("provider"),
                row.get("name"),
                *list(row.get("aliases") or []),
            ]
            for key in keys:
                add_metadata(key, row)
    return result


def _expected_account_counts_from_files() -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for path in _expected_account_count_files():
        if not path.is_file():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        result.update(_extract_account_counts(loaded, source=str(path)))
    return result


def _account_inventory_metadata_from_files() -> dict[str, tuple[dict[str, Any], str]]:
    result: dict[str, tuple[dict[str, Any], str]] = {}
    for path in _expected_account_count_files():
        if not path.is_file():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        result.update(_extract_account_inventory_metadata(loaded, source=str(path)))
    return result


def _expected_account_counts_json() -> dict[str, tuple[int, str]]:
    raw = str(os.getenv(EXPECTED_ACCOUNT_COUNT_JSON_ENV) or "").strip()
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except Exception:
        return {}
    return _extract_account_counts(loaded, source=EXPECTED_ACCOUNT_COUNT_JSON_ENV)


def _account_inventory_metadata_json() -> dict[str, tuple[dict[str, Any], str]]:
    raw = str(os.getenv(EXPECTED_ACCOUNT_COUNT_JSON_ENV) or "").strip()
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except Exception:
        return {}
    return _extract_account_inventory_metadata(loaded, source=EXPECTED_ACCOUNT_COUNT_JSON_ENV)


def expected_account_count_for_provider(*, requested_provider: object, provider_key: object) -> tuple[int | None, str]:
    requested = str(requested_provider or "").strip().lower()
    canonical = str(provider_key or "").strip().lower()
    env_counts = _expected_account_counts_json()
    for key in (requested, canonical):
        if key and key in env_counts:
            return env_counts[key]
    loaded_counts = _expected_account_counts_from_files()
    for key in (requested, canonical):
        if key and key in loaded_counts:
            return loaded_counts[key]
    for key in (requested, canonical):
        for env_name in EXPECTED_ACCOUNT_COUNT_ENV_NAMES.get(key, ()):
            count = _positive_int(os.getenv(env_name))
            if count is not None:
                return count, env_name
    return None, ""


def account_inventory_metadata_for_provider(*, requested_provider: object, provider_key: object) -> tuple[dict[str, Any], str]:
    requested = str(requested_provider or "").strip().lower()
    canonical = str(provider_key or "").strip().lower()
    env_metadata = _account_inventory_metadata_json()
    for key in (requested, canonical):
        if key and key in env_metadata:
            return env_metadata[key]
    loaded_metadata = _account_inventory_metadata_from_files()
    for key in (requested, canonical):
        if key and key in loaded_metadata:
            return loaded_metadata[key]
    return {}, ""


def account_inventory_gap(*, requested_provider: object, provider_key: object, runtime_account_count: object) -> dict[str, Any]:
    expected_count, source = expected_account_count_for_provider(
        requested_provider=requested_provider,
        provider_key=provider_key,
    )
    if expected_count is None:
        return {}
    runtime_count = _positive_int(runtime_account_count) or 0
    gap = max(0, expected_count - runtime_count)
    inventory = {
        "expected_account_count": expected_count,
        "runtime_account_count": runtime_count,
        "visible_account_gap": gap,
        "status": "ready" if gap == 0 else "gap",
        "source_ref": source,
        "source_kind": "env" if source.startswith("PROPERTYQUARRY_") or source.endswith("_COUNT") else "file",
    }
    metadata: dict[str, Any] = {}
    if source == EXPECTED_ACCOUNT_COUNT_JSON_ENV:
        requested = str(requested_provider or "").strip().lower()
        canonical = str(provider_key or "").strip().lower()
        env_metadata = _account_inventory_metadata_json()
        for key in (requested, canonical):
            if key and key in env_metadata:
                metadata = dict(env_metadata[key][0])
                break
    elif source and not source.startswith("PROPERTYQUARRY_") and not source.endswith("_COUNT"):
        metadata, _metadata_source = account_inventory_metadata_for_provider(
            requested_provider=requested_provider,
            provider_key=provider_key,
        )
    inventory.update(metadata)
    return inventory


def _collect_tokens(value: object) -> set[str]:
    tokens: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            tokens.update(_collect_tokens(key))
            tokens.update(_collect_tokens(item))
        return tokens
    if isinstance(value, (list, tuple, set)):
        for item in value:
            tokens.update(_collect_tokens(item))
        return tokens
    text = str(value or "").strip().lower()
    if text:
        tokens.add(text)
    return tokens


def _binding_workflow_configured(metadata: dict[str, object]) -> tuple[bool, bool]:
    workflow_configured = any(str(metadata.get(key) or "").strip() for key in MOOTION_BROWSERACT_WORKFLOW_KEYS)
    run_url_configured = any(str(metadata.get(key) or "").strip() for key in MOOTION_BROWSERACT_RUN_URL_KEYS)
    return workflow_configured, run_url_configured


def _mootion_browseract_target_from_binding(binding: object) -> dict[str, Any]:
    if str(getattr(binding, "connector_name", "") or "").strip().lower() != "browseract":
        return {}
    status = str(getattr(binding, "status", "") or "").strip().lower()
    if status not in {"enabled", "ready", "active"}:
        return {}
    metadata = dict(getattr(binding, "auth_metadata_json", {}) or {})
    scope = dict(getattr(binding, "scope_json", {}) or {})
    workflow_configured, run_url_configured = _binding_workflow_configured(metadata)
    if not workflow_configured and not run_url_configured:
        return {}
    tokens: set[str] = set()
    for value in (
        metadata.get("service_key"),
        metadata.get("browseract_service_key"),
        metadata.get("capability_key"),
        metadata.get("tool_name"),
        metadata.get("mootion_browseract_bridge"),
        getattr(binding, "external_account_ref", ""),
        scope.get("services"),
        scope.get("scopes"),
        scope.get("assistant_surfaces"),
        scope.get("tags"),
        metadata.get("services"),
        metadata.get("scopes"),
        metadata.get("assistant_surfaces"),
        metadata.get("tags"),
    ):
        tokens.update(_collect_tokens(value))
    accounts = metadata.get("service_accounts_json")
    if isinstance(accounts, dict):
        tokens.update(_collect_tokens(list(accounts.keys())))
    if not bool(metadata.get("mootion_browseract_bridge")) and not any("mootion" in token for token in tokens):
        return {}
    return {
        "binding_id": str(getattr(binding, "binding_id", "") or "").strip(),
        "external_account_ref": str(getattr(binding, "external_account_ref", "") or "").strip(),
        "status": status,
        "workflow_configured": workflow_configured,
        "run_url_configured": run_url_configured,
    }


def mootion_browseract_bridge_readiness() -> dict[str, Any]:
    configured_principal_env_names = _env_names_configured(MOOTION_BROWSERACT_PRINCIPAL_ENV_NAMES)
    configured_principal_values = {
        str(os.getenv(env_name) or "").strip()
        for env_name in configured_principal_env_names
        if str(os.getenv(env_name) or "").strip()
    }
    if not configured_principal_values:
        return {
            "ready": False,
            "status": "blocked",
            "reason": "mootion_browseract_principal_scope_missing",
            "principal_scope_configured": False,
            "principal_env_names": [],
            "target_count": 0,
            "targets": [],
        }
    selected_principal_env_name = next(
        env_name
        for env_name in MOOTION_BROWSERACT_PRINCIPAL_ENV_NAMES
        if str(os.getenv(env_name) or "").strip()
    )
    principal_id = str(os.getenv(selected_principal_env_name) or "").strip()
    try:
        from app.services.tool_runtime import build_tool_runtime

        tool_runtime = build_tool_runtime()
        bindings = tool_runtime.list_connector_bindings(principal_id, limit=500)
    except Exception as exc:  # noqa: BLE001
        return {
            "ready": False,
            "status": "unavailable",
            "reason": "mootion_browseract_principal_binding_probe_failed",
            "principal_scope_configured": True,
            "principal_env_names": configured_principal_env_names,
            "selected_principal_env_name": selected_principal_env_name,
            "target_count": 0,
            "targets": [],
            "error": exc.__class__.__name__[:120],
        }
    targets = [
        target
        for target in (_mootion_browseract_target_from_binding(binding) for binding in bindings)
        if target
    ]
    asset_host_policy = mootion_remote_asset_host_policy_readiness()
    blockers: list[str] = []
    if not targets:
        blockers.append("mootion_browseract_principal_binding_missing")
    if not bool(asset_host_policy.get("configured")):
        blockers.append("mootion_remote_asset_host_allowlist_missing")
    elif not bool(asset_host_policy.get("valid")):
        blockers.append("mootion_remote_asset_host_allowlist_invalid")
    ready = not blockers
    reason = str(blockers[0] if blockers else "")
    return {
        "ready": ready,
        "status": "ready" if ready else "blocked",
        "reason": reason,
        "blockers": blockers,
        "principal_scope_configured": True,
        "principal_env_names": configured_principal_env_names,
        "selected_principal_env_name": selected_principal_env_name,
        "asset_host_allowlist_configured": bool(asset_host_policy.get("configured")),
        "asset_host_allowlist_valid": bool(asset_host_policy.get("valid")),
        "asset_host_count": int(asset_host_policy.get("host_count") or 0),
        "target_count": len(targets),
        "targets": targets,
    }


def telegram_delivery_readiness() -> dict[str, Any]:
    token_env_names = _env_names_configured(TELEGRAM_TOKEN_ENV_NAMES)
    registry_env_names = _env_names_configured(("EA_TELEGRAM_BOT_REGISTRY_JSON",))
    chat_env_names = _env_names_configured(TELEGRAM_CHAT_ENV_NAMES)
    route_env_names = _env_names_configured(TELEGRAM_ROUTE_ENV_NAMES)
    blockers: list[str] = []
    if not token_env_names and not registry_env_names:
        blockers.append("telegram_bot_token_missing")
    if not chat_env_names and not route_env_names:
        blockers.append("telegram_route_missing")
    return {
        "configured": not blockers,
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "token_env_names": token_env_names,
        "registry_env_names": registry_env_names,
        "chat_env_names": chat_env_names,
        "route_env_names": route_env_names,
    }


def _safe_checks(checks: object) -> dict[str, Any]:
    if not isinstance(checks, dict):
        return {}
    return {str(key): value for key, value in checks.items() if str(key) in SAFE_CHECK_KEYS}


def _provider_row(provider: str) -> dict[str, Any]:
    readiness = scene_video_provider_runtime_readiness(provider)
    checks = _safe_checks(readiness.get("checks"))
    blockers = list(readiness.get("blockers") or [])
    ready = bool(readiness.get("ready"))
    status = str(readiness.get("status") or "blocked")
    execution_lane = str(readiness.get("execution_lane") or "").strip()
    requested_provider_key = str(provider or "").strip().lower()
    runtime_provider_key = str(readiness.get("provider_key") or "").strip().lower()
    if "mootion" in {requested_provider_key, runtime_provider_key}:
        checks["mootion_browseract_remote"] = mootion_browseract_bridge_readiness()
        remote_value = checks.get("mootion_browseract_remote")
        remote = remote_value if isinstance(remote_value, dict) else {}
        if execution_lane != "browseract_remote":
            blockers.append("mootion_browseract_remote_lane_missing")
        if remote.get("ready") is not True:
            blockers.append("mootion_browseract_bridge_not_ready")
            remote_blockers = [
                str(value or "").strip()
                for value in list(remote.get("blockers") or [])
                if str(value or "").strip()
            ]
            if remote_blockers:
                blockers.extend(remote_blockers)
            else:
                remote_reason = str(remote.get("reason") or "").strip()
                if remote_reason:
                    blockers.append(remote_reason)
        blockers = list(dict.fromkeys(blockers))
        if blockers:
            ready = False
            status = "blocked"
    return {
        "requested_provider": provider,
        "provider_key": readiness.get("provider_key"),
        "provider_backend_key": readiness.get("provider_backend_key"),
        "ready": ready,
        "status": status,
        "blockers": blockers,
        "runtime_account_count": readiness.get("runtime_account_count"),
        "credit_state": readiness.get("credit_state"),
        "checks": checks,
        **(
            {"account_inventory": inventory}
            if (
                inventory := account_inventory_gap(
                    requested_provider=provider,
                    provider_key=readiness.get("provider_key"),
                    runtime_account_count=readiness.get("runtime_account_count"),
                )
            )
            else {}
        ),
        **({"execution_lane": execution_lane} if execution_lane else {}),
    }


def _provider_next_actions(rows: list[dict[str, Any]], telegram_readiness: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add_action(provider: str, reason: str, action: str, *, severity: str = "medium", **extra: Any) -> None:
        key = (provider, reason)
        if key in seen:
            return
        seen.add(key)
        actions.append(
            {
                "provider": provider,
                "reason": reason,
                "severity": severity,
                "action": action,
                **extra,
            }
        )

    if str(telegram_readiness.get("status") or "").strip() != "ready":
        add_action(
            "telegram",
            "telegram_delivery_not_ready",
            "Restore Telegram token and route configuration before claiming delivery receipts healthy.",
            severity="high",
            blockers=list(telegram_readiness.get("blockers") or []),
        )

    for row in rows:
        requested_provider = str(row.get("requested_provider") or row.get("provider_key") or "").strip()
        provider_key = str(row.get("provider_key") or "").strip()
        provider_label = requested_provider or provider_key or "unknown"
        is_mootion = "mootion" in {requested_provider.lower(), provider_key.lower()}
        blockers = [str(value or "").strip() for value in list(row.get("blockers") or []) if str(value or "").strip()]
        checks = dict(row.get("checks") or {})
        account_inventory = dict(row.get("account_inventory") or {})
        gap = int(account_inventory.get("visible_account_gap") or 0)
        if is_mootion:
            remote = dict(checks.get("mootion_browseract_remote") or {})
            execution_lane = str(row.get("execution_lane") or "").strip()
            if execution_lane != "browseract_remote":
                add_action(
                    "mootion",
                    "mootion_browseract_remote_lane_missing",
                    "Restore the Mootion BrowserAct bridge binding so release-grade scene-video generation uses the remote lane instead of a local-only fallback.",
                    severity="high",
                    current_execution_lane=execution_lane or "local_worker_or_unset",
                    remote_status=str(remote.get("status") or "unknown"),
                    remote_target_count=int(remote.get("target_count") or 0),
                )
            if remote.get("ready") is not True:
                add_action(
                    "mootion",
                    "mootion_browseract_bridge_not_ready",
                    "Configure an enabled BrowserAct connector binding with Mootion workflow or run-url metadata, then regenerate the scene-video readiness receipt.",
                    severity="high",
                    remote_status=str(remote.get("status") or "unknown"),
                    remote_target_count=int(remote.get("target_count") or 0),
                )
        if gap > 0:
            add_action(
                provider_label,
                "provider_account_visibility_gap",
                "Expose the expected runtime-visible provider accounts to the runtime secret/config layer, then regenerate the scene-video readiness receipt.",
                severity="high",
                expected_account_count=account_inventory.get("expected_account_count"),
                runtime_account_count=account_inventory.get("runtime_account_count"),
                visible_account_gap=gap,
                tracked_account_count=account_inventory.get("tracked_account_count"),
                unavailable_account_count=account_inventory.get("unavailable_account_count"),
                availability_reason=account_inventory.get("availability_reason"),
                source_ref=account_inventory.get("source_ref"),
                do_not_touch=["ONEMIN_*"],
            )
        credit_state = str(row.get("credit_state") or checks.get("credit_state") or "").strip()
        if provider_key == "magicfit" and credit_state == "constrained":
            add_action(
                "magicfit",
                "magicfit_credit_constrained",
                "Select a funded MagicFit account or refresh credits, then clear the failure marker only after a successful provider render proof.",
                severity="high",
                credit_state=credit_state,
                runtime_account_count=row.get("runtime_account_count"),
                tracked_account_count=account_inventory.get("tracked_account_count"),
                unavailable_account_count=account_inventory.get("unavailable_account_count"),
                availability_reason=account_inventory.get("availability_reason"),
                do_not_touch=["ONEMIN_*"],
            )
        if "magicfit_insufficient_credits" in blockers:
            add_action(
                "magicfit",
                "magicfit_insufficient_credits",
                "Refresh MagicFit credits or select a funded MagicFit account, then remove the failure marker only after a successful provider render proof.",
                severity="high",
                do_not_touch=["ONEMIN_*"],
            )
        if "omagic_credentials_missing" in blockers:
            add_action(
                "omagic",
                "omagic_credentials_missing",
                "Configure OMagic/Magic credentials in the OMagic/Magic runtime secret layer; do not satisfy this provider from 1min credentials.",
                severity="high",
                do_not_touch=["ONEMIN_*"],
            )
        if "omagic_model_upload_adapter_missing" in blockers:
            add_action(
                "omagic",
                "omagic_model_upload_adapter_missing",
                "Implement and deploy the OMagic model-upload adapter before enabling PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED.",
                severity="high",
            )
        if "omagic_model_upload_adapter_disabled" in blockers:
            add_action(
                "omagic",
                "omagic_model_upload_adapter_disabled",
                "Enable PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED only after the deployed OMagic adapter has a successful proof render.",
                severity="medium",
            )
        if "omagic_model_upload_endpoint_missing" in blockers:
            add_action(
                "omagic",
                "omagic_model_upload_endpoint_missing",
                "Configure PROPERTYQUARRY_OMAGIC_RENDER_ENDPOINT or PROPERTYQUARRY_OMAGIC_RENDER_COMMAND for the deployed model-upload adapter before claiming OMagic runtime readiness.",
                severity="high",
            )
        if is_mootion and row.get("ready") is not True:
            add_action(
                "mootion",
                "mootion_not_ready",
                "Restore the Mootion BrowserAct bridge binding or local worker runtime, then regenerate the readiness receipt.",
                severity="high",
            )
    return actions


def build_report(
    *,
    providers: tuple[str, ...] = DEFAULT_PROVIDERS,
    release_commit_sha: str = "",
    image_digest: str = "",
    balance_probe: dict[str, Any] | None = None,
    balance_probe_ref: str = "",
    balance_probe_sha256: str = "",
) -> dict[str, Any]:
    rows = [_provider_row(provider) for provider in providers]
    if balance_probe is not None:
        _apply_balance_probe(rows, balance_probe)
    ready_count = sum(1 for row in rows if row.get("ready") is True)
    blocked = [row for row in rows if row.get("ready") is not True]
    telegram_readiness = telegram_delivery_readiness()
    return {
        "contract_name": "propertyquarry.scene_video_readiness.v1",
        "generated_at": _utc_now(),
        "release_commit_sha": str(release_commit_sha or "").strip().lower(),
        "image_digest": str(image_digest or "").strip().lower(),
        "balance_probe_ref": str(balance_probe_ref or "").strip(),
        "balance_probe_sha256": str(balance_probe_sha256 or "").strip().lower(),
        "providers": rows,
        "summary": {
            "provider_count": len(rows),
            "ready_count": ready_count,
            "blocked_count": len(blocked),
            "blocked_providers": [row["requested_provider"] for row in blocked],
        },
        "telegram_delivery_readiness": telegram_readiness,
        "next_actions": _provider_next_actions(rows, telegram_readiness),
        "secret_boundary": "This receipt records env variable names and readiness states only; credential values are never included.",
    }


def write_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a secret-safe PropertyQuarry scene-video readiness receipt.")
    parser.add_argument("--providers", default=",".join(DEFAULT_PROVIDERS))
    parser.add_argument("--output", default=str(_default_output_path()))
    parser.add_argument("--release-commit-sha", default="")
    parser.add_argument("--image-digest", default="")
    parser.add_argument("--balance-probe-receipt", default="")
    parser.add_argument(
        "--load-shared-env",
        action="store_true",
        help="Load the generated shared scene-video env bridge before probing runtime readiness.",
    )
    args = parser.parse_args()
    if args.load_shared_env:
        from property_scene_video_shared_env import load_shared_env

        load_shared_env()
    providers = _csv_values(args.providers) or DEFAULT_PROVIDERS
    balance_path = (
        Path(args.balance_probe_receipt).expanduser()
        if str(args.balance_probe_receipt or "").strip()
        else None
    )
    balance_probe = _load_json(balance_path) if balance_path is not None else None
    output_path = write_report(
        build_report(
            providers=providers,
            release_commit_sha=args.release_commit_sha,
            image_digest=args.image_digest,
            balance_probe=balance_probe,
            balance_probe_ref=str(balance_path) if balance_path is not None else "",
            balance_probe_sha256=(
                _sha256(balance_path)
                if balance_path is not None and balance_path.is_file()
                else ""
            ),
        ),
        Path(args.output).expanduser(),
    )
    print(json.dumps({"status": "pass", "output": str(output_path), "providers": list(providers)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
