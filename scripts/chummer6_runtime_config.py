#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path


EA_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = EA_ROOT / ".env"
POLICY_PATH = Path(os.environ.get("CHUMMER6_POLICY_PATH", "/docker/fleet/.chummer6_local_policy.json"))


@lru_cache(maxsize=1)
def load_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for raw in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


@lru_cache(maxsize=1)
def load_runtime_overrides() -> dict[str, str]:
    if not POLICY_PATH.exists():
        return {}
    try:
        loaded = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    overrides = loaded.get("runtime_overrides")
    if not isinstance(overrides, dict):
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in overrides.items()
        if str(key).strip() and str(value).strip()
    }


def resolve_env_value(name: str, local_env: dict[str, str] | None = None, policy_env: dict[str, str] | None = None) -> str:
    if name in os.environ:
        return str(os.environ.get(name) or "").strip()
    if local_env is not None and name in local_env:
        return str(local_env.get(name) or "").strip()
    if policy_env is not None and name in policy_env:
        return str(policy_env.get(name) or "").strip()
    return ""
