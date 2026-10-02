#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


EA_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = EA_ROOT / ".env"
HOST = os.environ.get("EA_SKILL_HOST", "http://127.0.0.1:8090")


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


def build_skill_payload() -> dict[str, object]:
    return {
        "skill_key": "browseract_workflow_repair_manager",
        "task_key": "browseract_workflow_repair_manager",
        "name": "BrowserAct Workflow Repair Manager",
        "description": "Planner-executed BrowserAct workflow repair skill that uses Gemini Vortex judgment to patch broken BrowserAct workflow specs after runtime failures.",
        "deliverable_type": "browseract_workflow_repair_packet",
        "default_risk_class": "medium",
        "default_approval_class": "none",
        "workflow_template": "tool_then_artifact",
        "allowed_tools": ["browseract.repair_workflow_spec", "artifact_repository"],
        "evidence_requirements": ["workflow_runtime_failure", "workflow_spec"],
        "memory_write_policy": "none",
        "memory_reads": ["entities", "relationships"],
        "memory_writes": [],
        "tags": ["browseract", "workflow", "repair", "gemini", "self-heal"],
        "input_schema_json": {
            "type": "object",
            "required": ["workflow_name", "purpose", "tool_url", "failure_summary"],
            "properties": {
                "workflow_name": {"type": "string"},
                "purpose": {"type": "string"},
                "login_url": {"type": "string"},
                "tool_url": {"type": "string"},
                "failure_summary": {"type": "string"},
                "failing_step_goals": {"type": "array", "items": {"type": "string"}},
                "current_workflow_spec_json": {"type": "object"},
                "prompt_selector": {"type": "string"},
                "submit_selector": {"type": "string"},
                "result_selector": {"type": "string"},
                "output_dir": {"type": "string"},
            },
        },
        "output_schema_json": {
            "type": "object",
            "properties": {
                "deliverable_type": {"const": "browseract_workflow_repair_packet"},
                "artifact_kind": {"type": "string"},
                "structured_output_json": {"type": "object"},
            },
            "required": ["deliverable_type"],
        },
        "authority_profile_json": {"authority_class": "draft", "review_class": "operator"},
        "model_policy_json": {
            "provider": "gemini_vortex",
            "default_model": env_value("EA_GEMINI_VORTEX_MODEL") or "gemini-2.5-flash",
            "output_mode": "json",
        },
        "provider_hints_json": {
            "primary": ["BrowserAct", "Gemini Vortex"],
            "notes": ["Repair failing BrowserAct workflow specs while preserving runtime input bindings and compact operator checks."],
        },
        "tool_policy_json": {"allowed_tools": ["browseract.repair_workflow_spec", "artifact_repository"]},
        "human_policy_json": {"review_roles": ["automation_architect"]},
        "evaluation_cases_json": [{"case_key": "browseract_workflow_repair_manager_golden", "priority": "medium"}],
        "budget_policy_json": {
            "class": "medium",
            "workflow_template": "tool_then_artifact",
            "pre_artifact_capability_key": "workflow_spec_repair",
            "browseract_failure_strategy": "retry",
            "browseract_max_attempts": 2,
            "browseract_retry_backoff_seconds": 1,
            "skill_catalog_json": {
                "mode": "repair_compiler",
                "capabilities": ["workflow_spec_repair", "runtime_failure_diagnosis"],
            },
        },
    }


def apply_skill_payload(skills, body: dict[str, object]) -> dict[str, object]:
    row = skills.upsert_skill(
        skill_key=str(body.get("skill_key") or ""),
        task_key=str(body.get("task_key") or ""),
        name=str(body.get("name") or ""),
        description=str(body.get("description") or ""),
        deliverable_type=str(body.get("deliverable_type") or ""),
        default_risk_class=str(body.get("default_risk_class") or "low"),
        default_approval_class=str(body.get("default_approval_class") or "none"),
        workflow_template=str(body.get("workflow_template") or "rewrite"),
        allowed_tools=tuple(str(value) for value in (body.get("allowed_tools") or []) if str(value or "").strip()),
        evidence_requirements=tuple(str(value) for value in (body.get("evidence_requirements") or []) if str(value or "").strip()),
        memory_write_policy=str(body.get("memory_write_policy") or "none"),
        memory_reads=tuple(str(value) for value in (body.get("memory_reads") or []) if str(value or "").strip()),
        memory_writes=tuple(str(value) for value in (body.get("memory_writes") or []) if str(value or "").strip()),
        tags=tuple(str(value) for value in (body.get("tags") or []) if str(value or "").strip()),
        input_schema_json=dict(body.get("input_schema_json") or {}),
        output_schema_json=dict(body.get("output_schema_json") or {}),
        authority_profile_json=dict(body.get("authority_profile_json") or {}),
        model_policy_json=dict(body.get("model_policy_json") or {}),
        provider_hints_json=dict(body.get("provider_hints_json") or {}),
        tool_policy_json=dict(body.get("tool_policy_json") or {}),
        human_policy_json=dict(body.get("human_policy_json") or {}),
        evaluation_cases_json=tuple(dict(value) for value in (body.get("evaluation_cases_json") or [])),
        budget_policy_json=dict(body.get("budget_policy_json") or {}),
    )
    return {
        "skill_key": row.skill_key,
        "task_key": row.task_key,
        "workflow_template": row.workflow_template,
        "provider_hints_json": dict(row.provider_hints_json or {}),
    }


def upsert_skill_local(body: dict[str, object]) -> dict[str, object]:
    app_root = str(EA_ROOT / "ea")
    if app_root not in sys.path:
        sys.path.insert(0, app_root)
    from app.services.skills import SkillCatalogService
    from app.services.task_contracts import build_task_contract_service

    skills = SkillCatalogService(build_task_contract_service())
    return apply_skill_payload(skills, body)


def main() -> int:
    skill = build_skill_payload()
    try:
        result = upsert_skill(skill)
    except urllib.error.URLError as exc:
        try:
            result = upsert_skill_local(skill)
            print(json.dumps({"status": "ok", "skill_key": result.get("skill_key", ""), "path": "local", "reason": f"api_unavailable:{exc.reason}"}))
            return 0
        except Exception as local_exc:
            print(json.dumps({"status": "skipped", "reason": f"api_unavailable:{exc.reason}", "local_error": str(local_exc)[:240]}))
            return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        try:
            result = upsert_skill_local(skill)
            print(json.dumps({"status": "ok", "skill_key": result.get("skill_key", ""), "path": "local", "reason": f"http_{exc.code}", "body": body[:240]}))
            return 0
        except Exception as local_exc:
            print(json.dumps({"status": "skipped", "reason": f"http_{exc.code}", "body": body[:240], "local_error": str(local_exc)[:240]}))
            return 0
    print(json.dumps({"status": "ok", "skill_key": result.get("skill_key", ""), "path": "api"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
