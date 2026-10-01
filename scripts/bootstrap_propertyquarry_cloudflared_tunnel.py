#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
EA_ENV_PATH = Path("/docker/EA/.env")
PROPERTY_ENV_PATH = ROOT / ".env"
API_BASE = "https://api.cloudflare.com/client/v4"


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _effective_env() -> dict[str, str]:
    env = _load_env_file(EA_ENV_PATH)
    env.update(_load_env_file(PROPERTY_ENV_PATH))
    for key, value in os.environ.items():
        if value:
            env[key] = value
    return env


def _cf_headers(env: dict[str, str]) -> dict[str, str]:
    email = str(env.get("CLOUDFLARE_EMAIL") or "").strip()
    api_key = str(env.get("CLOUDFLARE_GLOBAL_API_KEY") or "").strip()
    api_token = str(env.get("CLOUDFLARE_API_TOKEN") or env.get("CF_API_TOKEN") or "").strip()
    if api_token:
        return {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    if email and api_key:
        return {
            "X-Auth-Email": email,
            "X-Auth-Key": api_key,
            "Content-Type": "application/json",
        }
    raise SystemExit("Cloudflare credentials missing. Set CLOUDFLARE_API_TOKEN or CLOUDFLARE_EMAIL + CLOUDFLARE_GLOBAL_API_KEY.")


def _cf_request(method: str, path: str, *, headers: dict[str, str], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.request(method, f"{API_BASE}{path}", headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    body = response.json()
    if not body.get("success"):
        raise SystemExit(f"Cloudflare API error for {path}: {json.dumps(body.get('errors') or body, ensure_ascii=True)}")
    return body


def _discover_account_id(*, headers: dict[str, str], env: dict[str, str]) -> str:
    configured = str(env.get("PROPERTYQUARRY_CF_ACCOUNT_ID") or env.get("EA_CF_ACCOUNT_ID") or "").strip()
    if configured:
        return configured
    body = _cf_request("GET", "/accounts", headers=headers)
    accounts = list(body.get("result") or [])
    if len(accounts) != 1:
        summary = ", ".join(f"{item.get('name')}:{item.get('id')}" for item in accounts[:10])
        raise SystemExit(f"Could not uniquely determine Cloudflare account. Set PROPERTYQUARRY_CF_ACCOUNT_ID. Visible accounts: {summary}")
    return str(accounts[0].get("id") or "").strip()


def _list_tunnels(*, account_id: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    body = _cf_request("GET", f"/accounts/{account_id}/cfd_tunnel", headers=headers)
    return list(body.get("result") or [])


def _ensure_tunnel(*, account_id: str, headers: dict[str, str], tunnel_name: str) -> dict[str, Any]:
    for item in _list_tunnels(account_id=account_id, headers=headers):
        if str(item.get("name") or "").strip() == tunnel_name:
            return item
    tunnel_secret = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    body = _cf_request(
        "POST",
        f"/accounts/{account_id}/cfd_tunnel",
        headers=headers,
        payload={"name": tunnel_name, "config_src": "cloudflare", "tunnel_secret": tunnel_secret},
    )
    return dict(body.get("result") or {})


def _get_tunnel_token(*, account_id: str, headers: dict[str, str], tunnel_id: str) -> str:
    body = _cf_request("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token", headers=headers)
    result = body.get("result")
    if isinstance(result, dict):
        token = str(result.get("token") or "")
    else:
        token = str(result or "")
    if not token:
        raise SystemExit(f"Tunnel token missing for tunnel {tunnel_id}")
    return token


def _write_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    rendered = f"{key}={value}"
    updated = False
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = rendered
            updated = True
            break
    if not updated:
        lines.append(rendered)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or reuse a dedicated PropertyQuarry Cloudflare tunnel and store its run token locally."
    )
    parser.add_argument("--tunnel-name", default="propertyquarry", help="Cloudflare tunnel name to ensure.")
    parser.add_argument("--property-env", default=str(PROPERTY_ENV_PATH), help="PropertyQuarry env file to update.")
    parser.add_argument("--token-key", default="PROPERTYQUARRY_CF_TUNNEL_TOKEN", help="Env key to write.")
    parser.add_argument("--enable-cloudflared", action="store_true", help="Also set PROPERTYQUARRY_ENABLE_CLOUDFLARED=1.")
    parser.add_argument("--dry-run", action="store_true", help="Discover/create the tunnel but do not write local env files.")
    args = parser.parse_args()

    env = _effective_env()
    headers = _cf_headers(env)
    account_id = _discover_account_id(headers=headers, env=env)
    tunnel = _ensure_tunnel(account_id=account_id, headers=headers, tunnel_name=args.tunnel_name)
    tunnel_id = str(tunnel.get("id") or "").strip()
    if not tunnel_id:
        raise SystemExit("Cloudflare tunnel creation/list response did not include a tunnel id.")
    token = _get_tunnel_token(account_id=account_id, headers=headers, tunnel_id=tunnel_id)

    property_env_path = Path(args.property_env)
    if not args.dry_run:
        _write_env_value(property_env_path, args.token_key, token)
        if args.enable_cloudflared:
            _write_env_value(property_env_path, "PROPERTYQUARRY_ENABLE_CLOUDFLARED", "1")

    print(json.dumps(
        {
            "account_id": account_id,
            "tunnel_id": tunnel_id,
            "tunnel_name": args.tunnel_name,
            "wrote_env": not args.dry_run,
            "property_env": str(property_env_path),
            "token_key": args.token_key,
            "cloudflared_enabled": bool(args.enable_cloudflared and not args.dry_run),
        },
        ensure_ascii=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
