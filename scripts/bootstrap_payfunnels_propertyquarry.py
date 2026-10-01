#!/usr/bin/env python3
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_env_file() -> Path:
    return _root() / ".env"


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _upsert_env(path: Path, updates: dict[str, str]) -> None:
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    output: list[str] = []
    for raw_line in existing_lines:
        if "=" not in raw_line or raw_line.lstrip().startswith("#"):
            output.append(raw_line)
            continue
        key, _value = raw_line.split("=", 1)
        normalized = key.strip()
        if normalized in pending:
            output.append(f"{normalized}={pending.pop(normalized)}")
        else:
            output.append(raw_line)
    for key, value in pending.items():
        output.append(f"{key}={value}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _public_base(env_values: dict[str, str]) -> str:
    return (
        env_values.get("PROPERTYQUARRY_PUBLIC_BASE_URL")
        or env_values.get("EA_PUBLIC_APP_BASE_URL")
        or "https://propertyquarry.com"
    ).strip().rstrip("/")


def main() -> int:
    if any(arg in {"--help", "-h"} for arg in sys.argv[1:]):
        print("Usage: python3 scripts/bootstrap_payfunnels_propertyquarry.py [--env-file PATH] [--write-env]")
        print()
    parser = argparse.ArgumentParser(
        description="Prepare PropertyQuarry PayFunnels runtime configuration.",
    )
    parser.add_argument("--env-file", default=str(_default_env_file()), help="Env file to inspect and update.")
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="Write generated PAYFUNNELS_WEBHOOK_SECRET into the env file if missing.",
    )
    args = parser.parse_args()

    env_path = Path(args.env_file).expanduser().resolve()
    env_values = _parse_env(env_path)
    api_key = str(env_values.get("PAYFUNNELS_API_KEY") or "").strip()
    webhook_secret = str(env_values.get("PAYFUNNELS_WEBHOOK_SECRET") or "").strip()
    if not api_key:
        raise SystemExit("PAYFUNNELS_API_KEY is missing from the env file.")
    generated_secret = ""
    if not webhook_secret:
        generated_secret = secrets.token_urlsafe(32)
        webhook_secret = generated_secret
        if args.write_env:
            _upsert_env(env_path, {"PAYFUNNELS_WEBHOOK_SECRET": webhook_secret})
    base_url = _public_base(env_values)
    webhook_url = f"{base_url}/app/api/signals/property/billing/payfunnels/webhook"
    print(f"env_file={env_path}")
    print("payfunnels_api_key=present")
    print(f"payfunnels_webhook_secret={'generated' if generated_secret else 'present'}")
    print(f"payfunnels_webhook_url={webhook_url}")
    if generated_secret and not args.write_env:
        print(f"export PAYFUNNELS_WEBHOOK_SECRET={webhook_secret}")
    print("next_step=register this webhook URL in the PayFunnels dashboard integration screen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
