from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable


_MAGICFIT_SUFFIX_ALIASES = {
    "MAGICFIT_EMAIL": ("PROPERTYQUARRY_MAGICFIT_EMAIL", "MAGICFIT_EMAIL"),
    "MAGICFIT_PASSWORD": ("PROPERTYQUARRY_MAGICFIT_PASSWORD", "MAGICFIT_PASSWORD"),
    "MAGICFIT_TIER": ("PROPERTYQUARRY_MAGICFIT_TIER", "MAGICFIT_TIER"),
    "MAGICFIT_ACCOUNTS_JSON": ("PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON", "MAGICFIT_ACCOUNTS_JSON"),
    "MAGICFIT_ACCOUNTS_JSON_FILE": ("PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE", "MAGICFIT_ACCOUNTS_JSON_FILE"),
}
_RUNTIME_INCOMING_ROOT = Path("/data/incoming_property_tours")
_BLOCKED_ENV_PREFIXES = ("CHUMMER_EA_",)


def default_magicfit_env_files() -> tuple[Path, ...]:
    property_root = Path(os.environ.get("PROPERTYQUARRY_ROOT") or "/docker/property").expanduser()
    ea_root = Path(os.environ.get("PROPERTYQUARRY_EA_ROOT") or "/docker/EA").expanduser()
    return (
        property_root / ".env",
        Path("/app/.env"),
        Path("/app/config/.env"),
        ea_root / ".env.local",
        ea_root / ".env",
    )


def _normalize_env_value(raw: str) -> str:
    return raw.strip().strip("'").strip('"')


def _is_blocked_env_name(name: str) -> bool:
    return any(str(name or "").startswith(prefix) for prefix in _BLOCKED_ENV_PREFIXES)


def _selected_account_index(values: dict[str, str]) -> int:
    for key in ("PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX", "MAGICFIT_ACCOUNT_INDEX"):
        raw = str(values.get(key) or "").strip()
        if not raw:
            continue
        try:
            parsed = int(raw)
        except Exception:
            continue
        if parsed > 0:
            return parsed - 1
    return 0


def _load_accounts_from_json_text(raw_accounts: str) -> list[dict[str, object]]:
    try:
        loaded = json.loads(raw_accounts)
    except Exception:
        return []
    if isinstance(loaded, dict):
        nested_accounts = loaded.get("accounts")
        if not isinstance(nested_accounts, list):
            return []
        loaded = nested_accounts
    if not isinstance(loaded, list):
        return []
    return [row for row in loaded if isinstance(row, dict)]


def _is_runtime_incoming_path(path: Path) -> bool:
    normalized = str(path).strip()
    runtime_root = str(_RUNTIME_INCOMING_ROOT)
    return normalized == runtime_root or normalized.startswith(f"{runtime_root}/")


def _host_incoming_root() -> Path:
    configured = str(
        os.getenv("PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR")
        or os.getenv("PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR")
        or ""
    ).strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if not _is_runtime_incoming_path(configured_path):
            return configured_path
    property_root = Path(os.environ.get("PROPERTYQUARRY_ROOT") or "/docker/property").expanduser()
    return property_root / "state" / "incoming_property_tours"


def _resolve_accounts_json_file_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_file() or not _is_runtime_incoming_path(path):
        return path
    try:
        relative = path.relative_to(_RUNTIME_INCOMING_ROOT)
    except ValueError:
        return path
    return _host_incoming_root() / relative


def _apply_accounts_json_defaults(values: dict[str, str], sources: dict[str, str]) -> None:
    if values.get("PROPERTYQUARRY_MAGICFIT_EMAIL") and values.get("PROPERTYQUARRY_MAGICFIT_PASSWORD"):
        return
    accounts_path = values.get("PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE") or values.get("MAGICFIT_ACCOUNTS_JSON_FILE") or ""
    file_source_key = (
        "PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE"
        if values.get("PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE")
        else "MAGICFIT_ACCOUNTS_JSON_FILE"
    )
    loaded: list[dict[str, object]] = []
    source_key = ""
    if accounts_path:
        path = _resolve_accounts_json_file_path(accounts_path)
        if path.is_file():
            try:
                loaded = _load_accounts_from_json_text(path.read_text(encoding="utf-8"))
            except Exception:
                loaded = []
            if loaded:
                source_key = file_source_key
    if not loaded:
        raw_accounts = values.get("PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON") or values.get("MAGICFIT_ACCOUNTS_JSON") or ""
        source_key = (
            "PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON"
            if values.get("PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON")
            else "MAGICFIT_ACCOUNTS_JSON"
        )
        if not raw_accounts:
            return
        loaded = _load_accounts_from_json_text(raw_accounts)
        if not loaded:
            return
    credentialed_accounts: list[dict[str, object]] = []
    for row in loaded:
        email = str(row.get("email") or row.get("username") or row.get("login") or "").strip()
        password = str(row.get("password") or row.get("pass") or "").strip()
        if email and password:
            credentialed_accounts.append(row)
    if not credentialed_accounts:
        return
    selected_index = min(_selected_account_index(values), len(credentialed_accounts) - 1)
    account = credentialed_accounts[selected_index]
    email = str(account.get("email") or account.get("username") or account.get("login") or "").strip()
    password = str(account.get("password") or account.get("pass") or "").strip()
    source_label = f"{sources.get(source_key, 'process_env')}#{source_key}[{selected_index + 1}]"
    for key in ("PROPERTYQUARRY_MAGICFIT_EMAIL", "MAGICFIT_EMAIL"):
        values.setdefault(key, email)
        sources.setdefault(key, source_label)
    for key in ("PROPERTYQUARRY_MAGICFIT_PASSWORD", "MAGICFIT_PASSWORD"):
        values.setdefault(key, password)
        sources.setdefault(key, source_label)


def discover_magicfit_env(
    env_files: Iterable[Path] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    files = tuple(env_files or default_magicfit_env_files())
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for normalized_key, raw_value in os.environ.items():
        if _is_blocked_env_name(normalized_key):
            continue
        normalized_value = _normalize_env_value(str(raw_value or ""))
        if not normalized_key or not normalized_value:
            continue
        values.setdefault(normalized_key, normalized_value)
        sources.setdefault(normalized_key, "process_env")
        for suffix, aliases in _MAGICFIT_SUFFIX_ALIASES.items():
            if normalized_key == suffix or normalized_key.endswith(f"_{suffix}"):
                for alias in aliases:
                    values.setdefault(alias, normalized_value)
                    sources.setdefault(alias, "process_env")
    for path in files:
        if not path.exists():
            continue
        resolved = str(path.resolve())
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            normalized_key = key.strip()
            if _is_blocked_env_name(normalized_key):
                continue
            normalized_value = _normalize_env_value(value)
            if not normalized_key or not normalized_value:
                continue
            values.setdefault(normalized_key, normalized_value)
            sources.setdefault(normalized_key, resolved)
            for suffix, aliases in _MAGICFIT_SUFFIX_ALIASES.items():
                if normalized_key == suffix or normalized_key.endswith(f"_{suffix}"):
                    for alias in aliases:
                        values.setdefault(alias, normalized_value)
                        sources.setdefault(alias, resolved)
    _apply_accounts_json_defaults(values, sources)
    return values, sources


def load_magicfit_env(env_files: Iterable[Path] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    values, sources = discover_magicfit_env(env_files)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values, sources
