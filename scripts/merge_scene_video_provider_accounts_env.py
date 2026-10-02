#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


ALLOWED_UPDATE_KEYS = {
    "PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON",
    "PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE",
    "PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX",
    "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON",
    "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON_FILE",
    "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON",
    "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON_FILE",
}
PROTECTED_KEY_PREFIXES = ("ONEMIN_", "PROPERTYQUARRY_ONEMIN_")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SECURE_FILE_MODE = 0o600
RUNTIME_INCOMING_ROOT = Path("/data/incoming_property_tours")
DEFAULT_FILE_ENV_DIR = "state/incoming_property_tours/_operator-import-lane/scene_video_provider_accounts"
DEFAULT_FILE_ENV_SUBDIR = Path("_operator-import-lane") / "scene_video_provider_accounts"


def _load_accounts(path: str) -> list[dict[str, str]]:
    if not path:
        return []
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ValueError(f"{path} account JSON file not found")
    try:
        loaded = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} account JSON file is not valid JSON") from exc
    if not isinstance(loaded, list):
        raise ValueError(f"{path} must contain a JSON array")
    accounts: list[dict[str, str]] = []
    seen_emails: set[str] = set()
    for index, row in enumerate(loaded):
        if not isinstance(row, dict):
            raise ValueError(f"{path}[{index}] must be an object")
        allowed = {"email", "password", "label", "tier"}
        extra = sorted(str(key) for key in row if str(key) not in allowed)
        if extra:
            raise ValueError(f"{path}[{index}] has unsupported keys: {', '.join(extra)}")
        email = str(row.get("email") or "").strip()
        password = str(row.get("password") or "")
        if not EMAIL_RE.match(email):
            raise ValueError(f"{path}[{index}] has invalid email")
        email_key = email.lower()
        if email_key in seen_emails:
            raise ValueError(f"{path}[{index}] duplicates an earlier account email")
        seen_emails.add(email_key)
        if not password:
            raise ValueError(f"{path}[{index}] has empty password")
        account: dict[str, str] = {"email": email, "password": password}
        for optional in ("label", "tier"):
            value = str(row.get(optional) or "").strip()
            if value:
                account[optional] = value
        accounts.append(account)
    return accounts


def _require_secure_account_json_file(path: str) -> None:
    if not path:
        return
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ValueError(f"{path} account JSON file not found")
    mode = file_path.stat().st_mode & 0o777
    if mode != SECURE_FILE_MODE:
        raise ValueError(f"{path} must have mode 0o600 before merge; current mode is {oct(mode)}")


def _compact_accounts_json(accounts: list[dict[str, str]]) -> str:
    return json.dumps(accounts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _line_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    key = stripped.split("=", 1)[0].strip()
    if key.startswith("export "):
        key = key[len("export ") :].strip()
    return key


def _line_assignment_prefix(line: str) -> str:
    stripped = line.strip()
    return "export " if stripped.startswith("export ") else ""


def _validate_updates(updates: dict[str, str]) -> None:
    forbidden = sorted(key for key in updates if key not in ALLOWED_UPDATE_KEYS)
    if forbidden:
        raise ValueError(f"refusing unsupported env keys: {', '.join(forbidden)}")
    protected = sorted(
        key
        for key in updates
        if any(key.startswith(prefix) for prefix in PROTECTED_KEY_PREFIXES)
    )
    if protected:
        raise ValueError(f"refusing protected env keys: {', '.join(protected)}")


def _protected_env_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if any(_line_key(line).startswith(prefix) for prefix in PROTECTED_KEY_PREFIXES)
    ]


def _ensure_protected_env_lines_preserved(existing: str, merged: str) -> None:
    if _protected_env_lines(existing) != _protected_env_lines(merged):
        raise ValueError("protected ONEMIN env lines changed during merge")


def _ensure_no_duplicate_update_keys(existing: str, update_keys: set[str]) -> None:
    counts: dict[str, int] = {}
    for line in existing.splitlines():
        key = _line_key(line)
        if key in update_keys:
            counts[key] = counts.get(key, 0) + 1
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate provider account env keys in target env: {', '.join(duplicates)}")


def merge_env_text(existing: str, updates: dict[str, str]) -> tuple[str, list[str]]:
    _validate_updates(updates)
    _ensure_no_duplicate_update_keys(existing, set(updates))
    updated_keys: list[str] = []
    seen: set[str] = set()
    output: list[str] = []
    for line in existing.splitlines():
        key = _line_key(line)
        if key in updates:
            output.append(f"{_line_assignment_prefix(line)}{key}={_shell_quote(updates[key])}")
            updated_keys.append(key)
            seen.add(key)
        else:
            output.append(line)
    for key in sorted(updates):
        if key not in seen:
            output.append(f"{key}={_shell_quote(updates[key])}")
            updated_keys.append(key)
    rendered = "\n".join(output).rstrip() + "\n"
    return rendered, updated_keys


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(text)
        tmp_path = Path(handle.name)
    tmp_path.chmod(SECURE_FILE_MODE)
    tmp_path.replace(path)
    path.chmod(SECURE_FILE_MODE)


def _repo_root(env_file: Path) -> Path:
    env_parent = env_file.expanduser().resolve().parent
    candidates = [env_parent, *env_parent.parents, Path.cwd().resolve(), Path("/docker/property")]
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "docker-compose.property.yml").is_file():
            return candidate.resolve()
    return env_parent


def _is_runtime_incoming_path(path: Path) -> bool:
    normalized = str(path).strip()
    runtime_root = str(RUNTIME_INCOMING_ROOT)
    return normalized == runtime_root or normalized.startswith(f"{runtime_root}/")


def _host_incoming_root(env_file: Path) -> Path:
    configured = str(
        os.getenv("PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR")
        or os.getenv("PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR")
        or ""
    ).strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if not _is_runtime_incoming_path(configured_path):
            return configured_path.resolve()
    repo_root = _repo_root(env_file)
    if (repo_root / "docker-compose.property.yml").is_file():
        return (repo_root / "state" / "incoming_property_tours").resolve()
    if configured:
        return Path(configured).expanduser().resolve()
    return RUNTIME_INCOMING_ROOT


def _stable_account_file_dir(env_file: Path, configured_dir: str) -> Path:
    if str(configured_dir or "").strip():
        configured_path = Path(configured_dir).expanduser()
    else:
        configured_path = _host_incoming_root(env_file) / DEFAULT_FILE_ENV_SUBDIR
    if _is_runtime_incoming_path(configured_path):
        try:
            relative = configured_path.relative_to(RUNTIME_INCOMING_ROOT)
        except ValueError:
            return configured_path.resolve()
        return (_host_incoming_root(env_file) / relative).resolve()
    return configured_path.resolve()


def _stable_account_file_path(account_file_dir: Path, provider: str) -> Path:
    safe_provider = str(provider or "").strip().lower().replace("_", "-")
    return account_file_dir / f"{safe_provider}-accounts.json"


def _write_accounts_file(path: Path, accounts: list[dict[str, str]]) -> None:
    rendered = json.dumps(accounts, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    _atomic_write(path, rendered)


def _next_backup_path(path: Path) -> Path:
    base = path.with_name(f"{path.name}.scene-video-provider-accounts.bak")
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}.scene-video-provider-accounts.bak.{index}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"too many existing backup files for {path}")


def _write_backup(path: Path, existing: str) -> str:
    if not path.exists():
        return ""
    backup_path = _next_backup_path(path)
    backup_path.write_text(existing, encoding="utf-8")
    backup_path.chmod(SECURE_FILE_MODE)
    return str(backup_path)


def _env_account_file_value(env_file: Path, path: Path) -> str:
    resolved_path = path.expanduser().resolve()
    if _is_runtime_incoming_path(resolved_path):
        return str(resolved_path)
    try:
        relative = resolved_path.relative_to(_host_incoming_root(env_file))
    except ValueError:
        return str(resolved_path)
    return str((RUNTIME_INCOMING_ROOT / relative).resolve())


def build_updates(
    *,
    magicfit_accounts: list[dict[str, str]],
    omagic_accounts: list[dict[str, str]],
    magicfit_account_index: int | None,
    write_magic_alias: bool,
    write_file_env: bool = False,
    magicfit_accounts_env_file: str = "",
    omagic_accounts_env_file: str = "",
) -> dict[str, str]:
    updates: dict[str, str] = {}
    if magicfit_accounts:
        if write_file_env:
            if not magicfit_accounts_env_file:
                raise ValueError("magicfit file-env mode requires a stable target file path")
            updates["PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE"] = str(magicfit_accounts_env_file)
        else:
            updates["PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON"] = _compact_accounts_json(magicfit_accounts)
        if magicfit_account_index is not None:
            if magicfit_account_index < 0 or magicfit_account_index >= len(magicfit_accounts):
                raise ValueError(
                    f"magicfit account index {magicfit_account_index} is outside available account range 0..{len(magicfit_accounts) - 1}"
                )
            updates["PROPERTYQUARRY_MAGICFIT_ACCOUNT_INDEX"] = str(magicfit_account_index)
    elif magicfit_account_index is not None:
        raise ValueError("magicfit account index requires MagicFit accounts")
    if omagic_accounts:
        if not write_magic_alias:
            raise ValueError("magic alias account env is required when OMagic accounts are supplied")
        if write_file_env:
            if not omagic_accounts_env_file:
                raise ValueError("omagic file-env mode requires a stable target file path")
            updates["PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON_FILE"] = str(omagic_accounts_env_file)
            updates["PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON_FILE"] = str(omagic_accounts_env_file)
        else:
            omagic_json = _compact_accounts_json(omagic_accounts)
            updates["PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON"] = omagic_json
            updates["PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON"] = omagic_json
    _validate_updates(updates)
    return updates


def merge_accounts_env(
    *,
    env_file: Path,
    magicfit_accounts: list[dict[str, str]],
    omagic_accounts: list[dict[str, str]],
    expected_magicfit_count: int | None,
    expected_omagic_count: int | None,
    magicfit_account_index: int | None,
    write_magic_alias: bool,
    write_file_env: bool = False,
    account_file_dir: Path | None = None,
    write: bool,
) -> dict[str, Any]:
    if expected_magicfit_count is not None and len(magicfit_accounts) != expected_magicfit_count:
        raise ValueError(f"magicfit account count {len(magicfit_accounts)} does not match expected {expected_magicfit_count}")
    if expected_omagic_count is not None and len(omagic_accounts) != expected_omagic_count:
        raise ValueError(f"omagic account count {len(omagic_accounts)} does not match expected {expected_omagic_count}")
    resolved_account_file_dir = (
        _stable_account_file_dir(env_file, str(account_file_dir or ""))
        if write_file_env
        else None
    )
    magicfit_accounts_file_path = (
        _stable_account_file_path(resolved_account_file_dir, "magicfit")
        if resolved_account_file_dir is not None and magicfit_accounts
        else None
    )
    omagic_accounts_file_path = (
        _stable_account_file_path(resolved_account_file_dir, "omagic")
        if resolved_account_file_dir is not None and omagic_accounts
        else None
    )
    magicfit_accounts_env_file = (
        _env_account_file_value(env_file, magicfit_accounts_file_path)
        if magicfit_accounts_file_path is not None
        else ""
    )
    omagic_accounts_env_file = (
        _env_account_file_value(env_file, omagic_accounts_file_path)
        if omagic_accounts_file_path is not None
        else ""
    )
    updates = build_updates(
        magicfit_accounts=magicfit_accounts,
        omagic_accounts=omagic_accounts,
        magicfit_account_index=magicfit_account_index,
        write_magic_alias=write_magic_alias,
        write_file_env=write_file_env,
        magicfit_accounts_env_file=magicfit_accounts_env_file,
        omagic_accounts_env_file=omagic_accounts_env_file,
    )
    if write and not updates:
        raise ValueError("no provider account updates supplied for --write")
    existing = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    merged, updated_keys = merge_env_text(existing, updates)
    _ensure_protected_env_lines_preserved(existing, merged)
    backup_path = ""
    written_account_files: list[str] = []
    if write and updates:
        if write_file_env and resolved_account_file_dir is not None:
            if magicfit_accounts:
                magicfit_path = Path(magicfit_accounts_file_path or "")
                _write_accounts_file(magicfit_path, magicfit_accounts)
                written_account_files.append(str(magicfit_path))
            if omagic_accounts:
                omagic_path = Path(omagic_accounts_file_path or "")
                _write_accounts_file(omagic_path, omagic_accounts)
                written_account_files.append(str(omagic_path))
        backup_path = _write_backup(env_file, existing)
        _atomic_write(env_file, merged)
    env_account_file_values = {
        key: value
        for key, value in (
            ("PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE", magicfit_accounts_env_file),
            ("PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON_FILE", omagic_accounts_env_file),
            ("PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON_FILE", omagic_accounts_env_file),
        )
        if value
    }
    return {
        "status": "pass",
        "dry_run": not write,
        "write_mode": "file_env" if write_file_env else "inline_json_env",
        "env_file": str(env_file),
        "backup_path": backup_path,
        "secure_file_mode": oct(SECURE_FILE_MODE),
        "updated_keys": updated_keys,
        "account_file_dir": str(resolved_account_file_dir) if resolved_account_file_dir is not None else "",
        "planned_account_files": [
            str(path)
            for path in (magicfit_accounts_file_path, omagic_accounts_file_path)
            if path is not None
        ],
        "written_account_files": written_account_files,
        "env_account_file_values": env_account_file_values,
        "provider_account_counts": {
            "magicfit": len(magicfit_accounts),
            "omagic": len(omagic_accounts),
            "magic_alias_written": bool(omagic_accounts and write_magic_alias),
        },
        "protected_key_prefixes": list(PROTECTED_KEY_PREFIXES),
        "protected_keys_touched": [],
        "protected_line_count": len(_protected_env_lines(existing)),
        "secret_values_in_receipt": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely merge MagicFit and OMagic scene-video provider accounts into an env file.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--magicfit-accounts-json-file", default="")
    parser.add_argument("--omagic-accounts-json-file", default="")
    parser.add_argument("--expected-magicfit-count", type=int, default=None)
    parser.add_argument("--expected-omagic-count", type=int, default=None)
    parser.add_argument("--magicfit-account-index", type=int, default=None)
    parser.add_argument("--no-magic-alias", action="store_true")
    parser.add_argument("--write-file-env", action="store_true")
    parser.add_argument("--account-file-dir", default="")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    try:
        _require_secure_account_json_file(args.magicfit_accounts_json_file)
        _require_secure_account_json_file(args.omagic_accounts_json_file)
        receipt = merge_accounts_env(
            env_file=Path(args.env_file).expanduser(),
            magicfit_accounts=_load_accounts(args.magicfit_accounts_json_file),
            omagic_accounts=_load_accounts(args.omagic_accounts_json_file),
            expected_magicfit_count=args.expected_magicfit_count,
            expected_omagic_count=args.expected_omagic_count,
            magicfit_account_index=args.magicfit_account_index,
            write_magic_alias=not args.no_magic_alias,
            write_file_env=args.write_file_env,
            account_file_dir=Path(args.account_file_dir).expanduser() if args.account_file_dir else None,
            write=args.write,
        )
    except Exception as exc:  # noqa: BLE001
        receipt = {
            "status": "fail",
            "blockers": [str(exc)],
            "protected_key_prefixes": list(PROTECTED_KEY_PREFIXES),
            "protected_keys_touched": [],
            "secret_values_in_receipt": False,
        }
        print(json.dumps(receipt, sort_keys=True))
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
