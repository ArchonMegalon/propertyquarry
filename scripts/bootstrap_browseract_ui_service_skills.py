#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

EA_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = EA_ROOT / ".env"
HOST = os.environ.get("EA_SKILL_HOST", "http://127.0.0.1:8090")
sys.path.insert(0, str(EA_ROOT / "ea"))

from app.services.browseract_ui_service_catalog import (  # noqa: E402
    BrowserActUiServiceDefinition,
    browseract_ui_service_by_capability,
    browseract_ui_service_definitions,
)


def env_value(name: str) -> str:
    direct = str(os.environ.get(name) or "").strip()
    if direct:
        return direct
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip()
    return ""


def upsert_skill(body: dict[str, object]) -> dict[str, object]:
    token = env_value("EA_API_TOKEN")
    request = urllib.request.Request(
        f"{HOST}/v1/skills",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        data=json.dumps(body).encode("utf-8"),
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def skill_payload(service: BrowserActUiServiceDefinition) -> dict[str, object]:
    output_names = [name for name in service.browseract_service_names if str(name).strip().lower() != "browseract"]
    notes = [f"Steerable BrowserAct workflow for {service.name.lower()}."]
    return {
        "skill_key": service.skill_key,
        "task_key": service.task_key,
        "name": service.name,
        "description": service.description,
        "deliverable_type": service.deliverable_type,
        "default_risk_class": "medium",
        "default_approval_class": "none",
        "workflow_template": "tool_then_artifact",
        "allowed_tools": [service.tool_name, "artifact_repository"],
        "evidence_requirements": ["service_prompt", "ui_render_request", "browseract_template"],
        "memory_write_policy": "none",
        "memory_reads": ["entities", "relationships"],
        "memory_writes": [],
        "tags": list(service.tags),
        "input_schema_json": service.input_schema_json(),
        "output_schema_json": {
            "type": "object",
            "required": ["deliverable_type"],
            "properties": {
                "deliverable_type": {"const": service.deliverable_type},
                **service.output_schema_json().get("properties", {}),
            },
        },
        "authority_profile_json": {"authority_class": "draft", "review_class": "operator"},
        "provider_hints_json": {
            "primary": ["BrowserAct"],
            "output": output_names,
            "notes": notes,
        },
        "tool_policy_json": {"allowed_tools": [service.tool_name, "artifact_repository"]},
        "human_policy_json": {"review_roles": ["automation_architect"]},
        "evaluation_cases_json": [{"case_key": f"{service.skill_key}_golden", "priority": "medium"}],
        "budget_policy_json": {
            "class": "medium",
            "workflow_template": "tool_then_artifact",
            "pre_artifact_capability_key": service.capability_key,
            "browseract_failure_strategy": "retry",
            "browseract_max_attempts": 2,
            "browseract_retry_backoff_seconds": 1,
            "skill_catalog_json": {
                "mode": "ui_service_operator",
                "service_key": service.service_key,
                "output_label": service.output_label,
            },
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upsert BrowserAct UI-service skills into EA.")
    parser.add_argument(
        "--service",
        action="append",
        default=[],
        help="Capability/service key to upsert. Repeat for multiple services. Defaults to all catalog services.",
    )
    return parser.parse_args()


def selected_services(names: list[str]) -> list[BrowserActUiServiceDefinition]:
    requested = [str(value or "").strip() for value in names if str(value or "").strip()]
    if not requested:
        return list(browseract_ui_service_definitions())
    resolved: list[BrowserActUiServiceDefinition] = []
    seen: set[str] = set()
    for name in requested:
        service = browseract_ui_service_by_capability(name)
        if service is None:
            raise SystemExit(f"unknown_service:{name}")
        if service.service_key in seen:
            continue
        seen.add(service.service_key)
        resolved.append(service)
    return resolved


def main() -> int:
    args = parse_args()
    results: list[dict[str, object]] = []
    for service in selected_services(args.service):
        body = skill_payload(service)
        try:
            result = upsert_skill(body)
        except urllib.error.URLError as exc:
            print(json.dumps({"status": "skipped", "reason": f"api_unavailable:{exc.reason}", "service_key": service.service_key}))
            return 0
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            print(
                json.dumps(
                    {
                        "status": "skipped",
                        "reason": f"http_{exc.code}",
                        "body": detail[:240],
                        "service_key": service.service_key,
                    }
                )
            )
            return 0
        results.append(
            {
                "status": "ok",
                "service_key": service.service_key,
                "skill_key": result.get("skill_key", ""),
            }
        )
    print(json.dumps({"status": "ok", "skills": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
