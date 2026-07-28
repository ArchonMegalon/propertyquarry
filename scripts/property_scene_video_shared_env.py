#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import quote, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = ROOT / "state" / "runtime" / "property_scene_video_shared.env"
DEFAULT_ACCOUNT_HOST_DIR = ROOT / "state" / "incoming_property_tours" / "_operator-import-lane" / "scene_video_provider_accounts"
DEFAULT_ACCOUNT_RUNTIME_DIR = PurePosixPath("/data/incoming_property_tours/_operator-import-lane/scene_video_provider_accounts")
DEFAULT_MAGICAI_RENDER_COMMAND = "python /app/scripts/render_magicai_model_upload_adapter.py"
PASSTHROUGH_ENV_NAMES = (
    "EA_STORAGE_BACKEND",
    "EA_TELEGRAM_BOT_TOKEN",
    "EA_TELEGRAM_BOT_REGISTRY_JSON",
    "EA_TELEGRAM_DEFAULT_PRINCIPAL_ID",
    "EA_DEFAULT_PRINCIPAL_ID",
    "EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT",
    "PROPERTYQUARRY_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "PROPERTYQUARRY_TELEGRAM_CHAT_ID",
    "TELEGRAM_CHAT_ID",
    "EA_TELEGRAM_CHAT_ID",
    "EA_TELEGRAM_DEFAULT_CHAT_ID",
    "EA_PROACTIVE_OODA_TELEGRAM_CHAT_ID",
    "ONEMIN_AI_API_KEY",
    "ONEMIN_DIRECT_API_KEYS_JSON",
    "ONEMIN_DIRECT_API_KEYS_JSON_FILE",
    "BROWSERACT_API_KEY",
    "BROWSERACT_API_KEY_FALLBACK_1",
    "BROWSERACT_API_KEY_FALLBACK_2",
    "BROWSERACT_API_KEY_FALLBACK_3",
    "WORKLLM_BASE_URL",
    "WORKLLM_EMAIL",
    "WORKLLM_PASSWORD",
    "WORKLLM_LICENSE_TIER",
    "WORKLLM_PROVIDER_VERIFIED",
    "WORKLLM_RUNTIME_ENABLED",
    "WORKLLM_REAL_ESTATE_AGENT_ID",
    "WORKLLM_ALLOWED_HOSTS",
    "PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS",
    "PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_MAX_BYTES",
    "PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_TIMEOUT_SECONDS",
    "PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED",
    "PROPERTYQUARRY_OMAGIC_TEMPLATE_VARIANT_ID",
    "PROPERTYQUARRY_OMAGIC_TEMPLATE_ARGUMENT_NAME",
    "PROPERTYQUARRY_OMAGIC_TEMPLATE_TEXT_ARGUMENT_NAME",
    "PROPERTYQUARRY_OMAGIC_TEMPLATE_ASPECT_RATIO_ARGUMENT_NAME",
    "PROPERTYQUARRY_OMAGIC_MODEL_ROTATION_DEGREES",
)
PASSTHROUGH_ENV_PREFIXES = (
    "ONEMIN_AI_API_KEY_FALLBACK_",
)
SECURE_FILE_MODE = 0o600
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _default_output_path() -> Path:
    configured = str(os.environ.get("PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_OUTPUT_PATH


def _default_source_env_files() -> tuple[Path, ...]:
    property_root = Path(os.environ.get("PROPERTYQUARRY_ROOT") or ROOT).expanduser()
    ea_root = Path(os.environ.get("PROPERTYQUARRY_EA_ROOT") or "/docker/EA").expanduser()
    chummer_root = Path(
        os.environ.get("PROPERTYQUARRY_CHUMMER_RUN_SERVICES_ROOT") or "/docker/chummercomplete/chummer.run-services"
    ).expanduser()
    return (
        property_root / ".env",
        property_root / ".env.local",
        ea_root / ".env",
        ea_root / ".env.local",
        chummer_root / ".env",
        chummer_root / ".env.local",
    )


def _normalize_env_value(raw: str) -> str:
    value = str(raw or "").strip()
    if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_env_assignments(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    assignments: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key.startswith("export "):
            normalized_key = normalized_key[len("export ") :].strip()
        if normalized_key:
            assignments[normalized_key] = _normalize_env_value(value)
    return assignments


def _merged_assignments(source_env_files: Iterable[Path]) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for path in source_env_files:
        assignments.update(_load_env_assignments(Path(path).expanduser()))
    for key, value in os.environ.items():
        normalized = _normalize_env_value(value)
        if normalized:
            assignments[key] = normalized
    return assignments


def _shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _write_secret_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(SECURE_FILE_MODE)


def _write_secret_json(path: Path, payload: object) -> None:
    _write_secret_text(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")


def _compact_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _account_row(email: str, password: str, *, label: str = "", tier: str = "") -> dict[str, str]:
    row = {"email": email, "password": password}
    if label:
        row["label"] = label
    if tier:
        row["tier"] = tier
    return row


def _valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(str(value or "").strip()))


def _copy_if_present(assignments: dict[str, str], env_updates: dict[str, str], env_name: str) -> None:
    passthrough_value = str(assignments.get(env_name) or "").strip()
    if passthrough_value:
        env_updates[env_name] = passthrough_value


def _copy_by_prefix(assignments: dict[str, str], env_updates: dict[str, str], prefix: str) -> None:
    for env_name in sorted(key for key in assignments if str(key).startswith(prefix)):
        _copy_if_present(assignments, env_updates, env_name)


def _property_compose_database_url(assignments: dict[str, str], *, source_assignments: Iterable[dict[str, str]] = ()) -> str:
    for env_name in ("PROPERTYQUARRY_SCENE_VIDEO_DATABASE_URL", "PROPERTYQUARRY_DATABASE_URL"):
        configured = str(assignments.get(env_name) or "").strip()
        if configured:
            return configured
    password = ""
    for candidate in list(source_assignments)[:2]:
        source_password = str(candidate.get("POSTGRES_PASSWORD") or "").strip()
        if source_password:
            password = source_password
            break
    if not password:
        password = str(assignments.get("POSTGRES_PASSWORD") or "").strip()
    if not password:
        return ""
    return f"postgresql://postgres:{password}@propertyquarry-db:5432/postgres"


def _host_resolves(hostname: str) -> bool:
    normalized = str(hostname or "").strip()
    if not normalized:
        return False
    try:
        socket.getaddrinfo(normalized, None)
    except OSError:
        return False
    return True


def _docker_container_ip_for_host_alias(hostname: str) -> str:
    normalized = str(hostname or "").strip()
    if not normalized:
        return ""

    def inspect_targets(targets: list[str]) -> str:
        if not targets:
            return ""
        try:
            completed = subprocess.run(
                ["docker", "inspect", *targets],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            return ""
        if completed.returncode != 0 or not str(completed.stdout or "").strip():
            return ""
        try:
            payload = json.loads(completed.stdout)
        except Exception:
            return ""
        for row in payload:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name") or "").strip().lstrip("/")
            networks = dict(((row.get("NetworkSettings") or {}).get("Networks") or {}))
            aliases: set[str] = {name}
            for network_row in networks.values():
                if not isinstance(network_row, dict):
                    continue
                for alias in list(network_row.get("Aliases") or []):
                    alias_value = str(alias or "").strip()
                    if alias_value:
                        aliases.add(alias_value)
            if normalized not in aliases:
                continue
            for network_row in networks.values():
                if not isinstance(network_row, dict):
                    continue
                ip_address = str(network_row.get("IPAddress") or "").strip()
                if ip_address:
                    return ip_address
        return ""

    direct = inspect_targets([normalized])
    if direct:
        return direct
    try:
        ps = subprocess.run(
            ["docker", "ps", "-q"],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    targets = [line.strip() for line in str(ps.stdout or "").splitlines() if line.strip()]
    return inspect_targets(targets)


def _database_url_with_host(url: str, host: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if not parsed.scheme or not parsed.hostname:
        return str(url or "").strip()
    userinfo = ""
    if parsed.username is not None:
        userinfo = quote(parsed.username, safe="")
        if parsed.password is not None:
            userinfo += ":" + quote(parsed.password, safe="")
        userinfo += "@"
    netloc = f"{userinfo}{host}"
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _normalize_database_url_for_host_runtime(url: str) -> str:
    normalized = str(url or "").strip()
    parsed = urlsplit(normalized)
    if not normalized or not parsed.scheme.startswith("postgres") or not parsed.hostname:
        return normalized
    hostname = str(parsed.hostname or "").strip()
    if _host_resolves(hostname):
        return normalized
    replacement_host = _docker_container_ip_for_host_alias(hostname)
    if not replacement_host:
        return normalized
    return _database_url_with_host(normalized, replacement_host)


def _load_source_env_assignments(source_env_files: Iterable[Path]) -> list[dict[str, str]]:
    return [_load_env_assignments(Path(path).expanduser()) for path in source_env_files]


def _build_magicfit_accounts(assignments: dict[str, str], *, source_assignments: Iterable[dict[str, str]] = ()) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_row(candidate: dict[str, str], *, primary: bool = False) -> None:
        email = str(candidate.get("CHUMMER_EA_MAGICFIT_EMAIL") or "").strip()
        password = str(candidate.get("CHUMMER_EA_MAGICFIT_PASSWORD") or "").strip()
        tier = str(candidate.get("CHUMMER_EA_MAGICFIT_TIER") or "").strip()
        if not _valid_email(email) or not password:
            return
        email_key = email.lower()
        if email_key in seen:
            return
        seen.add(email_key)
        label = "shared_magicfit_primary" if primary else f"shared_magicfit_{len(rows) + 1:02d}"
        rows.append(_account_row(email, password, label=label, tier=tier))

    add_row(assignments, primary=True)
    for candidate in source_assignments:
        add_row(candidate)
    return rows


def _magicai_alias_order(assignments: dict[str, str]) -> list[str]:
    aliases: set[str] = set()
    for key, value in assignments.items():
        if not str(value or "").strip():
            continue
        match = re.fullmatch(r"MAGICAI_ACCOUNT_(.+)_(EMAIL|PASSWORD|API_KEY)", key)
        if match:
            aliases.add(match.group(1))
    return sorted(aliases, key=lambda value: (not str(value).isdigit(), str(value).zfill(6)))


def _magicai_account_value(assignments: dict[str, str], alias: str, field: str) -> str:
    candidates = [f"MAGICAI_ACCOUNT_{alias}_{field}"]
    if alias.isdigit():
        candidates.append(f"MAGICAI_ACCOUNT_{int(alias)}_{field}")
        candidates.append(f"MAGICAI_ACCOUNT_{alias.zfill(2)}_{field}")
    for key in candidates:
        value = str(assignments.get(key) or "").strip()
        if value:
            return value
    return ""


def _build_magicai_accounts(assignments: dict[str, str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_row(email: str, password: str, *, label: str = "", tier: str = "") -> None:
        normalized_email = str(email or "").strip()
        normalized_password = str(password or "").strip()
        if not _valid_email(normalized_email) or not normalized_password:
            return
        email_key = normalized_email.lower()
        if email_key in seen:
            return
        seen.add(email_key)
        rows.append(_account_row(normalized_email, normalized_password, label=label, tier=tier))

    add_row(
        str(assignments.get("CHUMMER_EA_MAGICAI_EMAIL") or "").strip(),
        str(assignments.get("CHUMMER_EA_MAGICAI_PASSWORD") or "").strip(),
        label="shared_magicai_primary",
        tier=str(assignments.get("CHUMMER_EA_MAGICAI_TIER") or "").strip(),
    )
    for alias in _magicai_alias_order(assignments):
        add_row(
            _magicai_account_value(assignments, alias, "EMAIL"),
            _magicai_account_value(assignments, alias, "PASSWORD"),
            label=f"magicai_account_{alias}",
        )
    return rows


def _preferred_magicai_api_key(assignments: dict[str, str]) -> str:
    primary = str(assignments.get("CHUMMER_EA_MAGICAI_API_KEY") or "").strip()
    if primary:
        return primary
    for alias in _magicai_alias_order(assignments):
        candidate = _magicai_account_value(assignments, alias, "API_KEY")
        if candidate:
            return candidate
    return ""


def build_shared_env_assignments(
    *,
    source_env_files: Iterable[Path] | None = None,
    account_host_dir: Path | None = None,
    account_runtime_dir: PurePosixPath | None = None,
) -> tuple[dict[str, str], dict[str, object]]:
    files = tuple(Path(path).expanduser() for path in (source_env_files or _default_source_env_files()))
    source_assignments = _load_source_env_assignments(files)
    assignments = _merged_assignments(files)
    host_dir = Path(account_host_dir or DEFAULT_ACCOUNT_HOST_DIR).expanduser()
    runtime_dir = account_runtime_dir or DEFAULT_ACCOUNT_RUNTIME_DIR

    env_updates: dict[str, str] = {}
    details: dict[str, object] = {
        "source_env_files": [str(path) for path in files],
        "account_host_dir": str(host_dir),
        "account_runtime_dir": str(runtime_dir),
        "providers": {},
    }

    magicfit_accounts = _build_magicfit_accounts(assignments, source_assignments=reversed(source_assignments))
    if magicfit_accounts:
        magicfit_host_path = host_dir / "propertyquarry-shared-magicfit-accounts.json"
        _write_secret_json(magicfit_host_path, magicfit_accounts)
        runtime_path = str(runtime_dir / magicfit_host_path.name)
        env_updates["PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON"] = _compact_json(magicfit_accounts)
        env_updates["PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE"] = runtime_path
        tier = str(assignments.get("CHUMMER_EA_MAGICFIT_TIER") or "").strip()
        if tier:
            env_updates["PROPERTYQUARRY_MAGICFIT_TIER"] = tier
        details["providers"]["magicfit"] = {
            "account_count": len(magicfit_accounts),
            "account_file": str(magicfit_host_path),
            "runtime_env_keys": sorted(env_updates_key for env_updates_key in env_updates if "MAGICFIT" in env_updates_key),
        }

    magicai_accounts = _build_magicai_accounts(assignments)
    magicai_api_key = _preferred_magicai_api_key(assignments)
    if magicai_accounts:
        magicai_host_path = host_dir / "propertyquarry-shared-magicai-accounts.json"
        _write_secret_json(magicai_host_path, magicai_accounts)
        runtime_path = str(runtime_dir / magicai_host_path.name)
        env_updates["PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON"] = _compact_json(magicai_accounts)
        env_updates["PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON_FILE"] = runtime_path
        env_updates["PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON"] = _compact_json(magicai_accounts)
        env_updates["PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON_FILE"] = runtime_path
    primary_magicai_email = str(assignments.get("CHUMMER_EA_MAGICAI_EMAIL") or "").strip()
    primary_magicai_password = str(assignments.get("CHUMMER_EA_MAGICAI_PASSWORD") or "").strip()
    if _valid_email(primary_magicai_email) and primary_magicai_password:
        env_updates["PROPERTYQUARRY_MAGIC_EMAIL"] = primary_magicai_email
        env_updates["PROPERTYQUARRY_MAGIC_PASSWORD"] = primary_magicai_password
        env_updates["PROPERTYQUARRY_OMAGIC_EMAIL"] = primary_magicai_email
        env_updates["PROPERTYQUARRY_OMAGIC_PASSWORD"] = primary_magicai_password
    if magicai_api_key:
        env_updates["PROPERTYQUARRY_MAGIC_API_KEY"] = magicai_api_key
        env_updates["PROPERTYQUARRY_OMAGIC_API_KEY"] = magicai_api_key
        env_updates["PROPERTYQUARRY_MAGIC_RENDER_COMMAND"] = DEFAULT_MAGICAI_RENDER_COMMAND
        env_updates["PROPERTYQUARRY_OMAGIC_RENDER_COMMAND"] = DEFAULT_MAGICAI_RENDER_COMMAND
    if magicai_accounts or magicai_api_key:
        details["providers"]["omagic"] = {
            "account_count": len(magicai_accounts),
            "account_file": str(host_dir / "propertyquarry-shared-magicai-accounts.json") if magicai_accounts else "",
            "api_key_present": bool(magicai_api_key),
            "runtime_env_keys": sorted(
                key
                for key in env_updates
                if "OMAGIC" in key
                or key
                in {
                    "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON_FILE",
                    "PROPERTYQUARRY_MAGIC_API_KEY",
                    "PROPERTYQUARRY_MAGIC_EMAIL",
                    "PROPERTYQUARRY_MAGIC_PASSWORD",
                    "PROPERTYQUARRY_MAGIC_RENDER_COMMAND",
                }
            ),
        }
    property_database_url = _property_compose_database_url(assignments, source_assignments=source_assignments)
    if property_database_url:
        env_updates["DATABASE_URL"] = property_database_url
    for env_name in PASSTHROUGH_ENV_NAMES:
        _copy_if_present(assignments, env_updates, env_name)
    for prefix in PASSTHROUGH_ENV_PREFIXES:
        _copy_by_prefix(assignments, env_updates, prefix)
    workllm_runtime_keys = sorted(key for key in env_updates if key.startswith("WORKLLM_"))
    if workllm_runtime_keys:
        details["providers"]["workllm"] = {
            "credentials_present": all(
                str(env_updates.get(name) or "").strip()
                for name in ("WORKLLM_BASE_URL", "WORKLLM_EMAIL", "WORKLLM_PASSWORD")
            ),
            "agent_id_present": bool(str(env_updates.get("WORKLLM_REAL_ESTATE_AGENT_ID") or "").strip()),
            "runtime_env_keys": workllm_runtime_keys,
        }
    if property_database_url:
        env_updates["EA_STORAGE_BACKEND"] = "postgres"
    return env_updates, details


def render_shared_env_text(assignments: dict[str, str]) -> str:
    if not assignments:
        return "# generated by property_scene_video_shared_env.py; no shared scene-video aliases resolved\n"
    lines = ["# generated by property_scene_video_shared_env.py; secrets are intentionally untracked"]
    for key in sorted(assignments):
        lines.append(f"{key}={_shell_quote(assignments[key])}")
    return "\n".join(lines) + "\n"


def write_shared_env_file(
    *,
    output_path: Path | None = None,
    source_env_files: Iterable[Path] | None = None,
    account_host_dir: Path | None = None,
    account_runtime_dir: PurePosixPath | None = None,
) -> dict[str, object]:
    resolved_output = Path(output_path or _default_output_path()).expanduser()
    env_updates, details = build_shared_env_assignments(
        source_env_files=source_env_files,
        account_host_dir=account_host_dir,
        account_runtime_dir=account_runtime_dir,
    )
    _write_secret_text(resolved_output, render_shared_env_text(env_updates))
    return {
        "status": "pass",
        "output_path": str(resolved_output),
        "env_key_count": len(env_updates),
        "providers": details.get("providers") or {},
        "source_env_files": details.get("source_env_files") or [],
    }


def load_shared_env(
    path: Path | None = None,
    *,
    override: bool = False,
) -> dict[str, str]:
    resolved = Path(path or _default_output_path()).expanduser()
    loaded = _load_env_assignments(resolved)
    applied: dict[str, str] = {}
    for key, value in loaded.items():
        normalized_value = _normalize_database_url_for_host_runtime(value) if key == "DATABASE_URL" else value
        if override or not str(os.environ.get(key) or "").strip():
            os.environ[key] = normalized_value
            applied[key] = normalized_value
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize a shared PropertyQuarry scene-video env bridge from EA/Chummer credentials.")
    parser.add_argument("--output", default=str(_default_output_path()))
    args = parser.parse_args()
    result = write_shared_env_file(output_path=Path(args.output).expanduser())
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
