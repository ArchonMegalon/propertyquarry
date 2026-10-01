#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
for candidate in (ROOT, EA_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app.services.workllm_client import (  # noqa: E402
    WorkllmApiError,
    WorkllmClient,
    redacted_workllm_error,
    workllm_enabled,
    workllm_runtime_enabled,
)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_env_file(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = str(key or "").strip()
        if not normalized_key.startswith("WORKLLM_"):
            continue
        normalized_value = value.strip()
        if (
            len(normalized_value) >= 2
            and normalized_value[0] == normalized_value[-1]
            and normalized_value[0] in {'"', "'"}
        ):
            normalized_value = normalized_value[1:-1]
        os.environ.setdefault(normalized_key, normalized_value)


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_template(template: dict[str, Any]) -> dict[str, object]:
    safe_keys = (
        "id",
        "slug",
        "name",
        "title",
        "function_key",
        "industry_key",
        "category",
        "status",
        "version",
    )
    return {
        key: template[key]
        for key in safe_keys
        if key in template and isinstance(template[key], (str, int, float, bool))
    }


def _tool_count(payload: dict[str, object]) -> int:
    candidates: list[object] = [payload.get("tools")]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("tools"))
    for candidate in candidates:
        if isinstance(candidate, list):
            return len(candidate)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the governed WorkLLM real-estate advisory provider.")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--live", action="store_true", help="Authenticate and perform read-only catalog checks.")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "_completion" / "workllm" / "WORKLLM_PROVIDER_VERIFICATION.generated.json",
    )
    args = parser.parse_args()
    if args.env_file is not None:
        _load_env_file(args.env_file)

    client = WorkllmClient()
    tier = str(os.getenv("WORKLLM_LICENSE_TIER") or "").strip()
    payload: dict[str, object] = {
        "contract_name": "propertyquarry.workllm_provider_verification.v1",
        "generated_at": _now_utc_iso(),
        "provider": "workllm",
        "workspace_host": client.workspace_host,
        "account_hash": client.account_hash,
        "configured": client.configured,
        "declared_license_tier": tier,
        "declared_tier_4": tier.lower() in {"4", "tier4", "tier_4", "tier 4"},
        "provider_verified_flag": str(os.getenv("WORKLLM_PROVIDER_VERIFIED") or "").strip().lower()
        in {"1", "true", "yes", "on"},
        "runtime_enabled_flag": str(os.getenv("WORKLLM_RUNTIME_ENABLED") or "").strip().lower()
        in {"1", "true", "yes", "on"},
        "runtime_switch_open": workllm_runtime_enabled(),
        "execution_ready": workllm_enabled(),
        "real_estate_agent_id_configured": bool(str(os.getenv("WORKLLM_REAL_ESTATE_AGENT_ID") or "").strip()),
        "live_check_requested": bool(args.live),
        "read_only_verification": True,
        "agent_run_performed": False,
        "agent_provision_performed": False,
        "organization_memory_written": False,
        "status": "configured_not_live_verified",
        "activation_allowed": False,
        "sources": [
            "https://workllm.io/product/ai-agents/",
            "https://workllm.mintlify.app/ai-agents",
            "https://workllm.mintlify.app/ready-made-ai-agents",
        ],
    }
    if not client.configured:
        payload["status"] = "blocked_missing_credentials"
    elif args.live:
        try:
            client.authenticate()
            tools = client.list_tools()
            payload["login_verified"] = True
            payload["prompt_tool_count"] = _tool_count(tools)
            payload["prompt_tool_catalog_sha256"] = _sha256_json(tools)
            templates = client.list_real_estate_templates()
            payload["real_estate_template_count"] = len(templates)
            payload["real_estate_templates"] = [_safe_template(item) for item in templates]
            payload["real_estate_template_catalog_sha256"] = _sha256_json(templates)
            if not templates:
                payload["status"] = "blocked_real_estate_category_empty"
            elif not str(os.getenv("WORKLLM_REAL_ESTATE_AGENT_ID") or "").strip():
                payload["status"] = "blocked_real_estate_agent_not_provisioned"
            elif not workllm_runtime_enabled():
                payload["status"] = "verified_live_activation_switch_closed"
            else:
                payload["status"] = "verified_live"
                payload["activation_allowed"] = True
        except WorkllmApiError as exc:
            payload["login_verified"] = exc.detail != "workllm_authentication_failed"
            payload["error"] = redacted_workllm_error(exc)
            payload["status"] = (
                "blocked_plan_entitlement"
                if exc.detail == "workllm_feature_unavailable_current_plan"
                else "provider_failed"
            )
        except Exception as exc:
            payload["status"] = "provider_failed"
            payload["error"] = redacted_workllm_error(exc)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0 if payload["status"] in {"configured_not_live_verified", "verified_live"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
