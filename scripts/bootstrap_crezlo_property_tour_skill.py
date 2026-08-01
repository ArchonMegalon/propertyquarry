#!/usr/bin/env python3
from __future__ import annotations

import json
import os
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
        "skill_key": "create_property_tour",
        "task_key": "create_property_tour",
        "name": "Create Property Tour",
        "description": "Planner-executed Crezlo property tour builder for steerable real-estate walkthrough variants.",
        "deliverable_type": "property_tour_packet",
        "default_risk_class": "medium",
        "default_approval_class": "none",
        "workflow_template": "tool_then_artifact",
        "allowed_tools": ["browseract.crezlo_property_tour", "artifact_repository"],
        "evidence_requirements": ["property_listing", "tour_brief", "property_media", "floorplan_analysis_receipt"],
        "memory_write_policy": "none",
        "memory_reads": ["entities", "relationships"],
        "memory_writes": [],
        "tags": ["browseract", "crezlo", "property", "tour"],
        "input_schema_json": {
            "type": "object",
            "required": ["binding_id", "tour_title", "property_url"],
            "properties": {
                "binding_id": {"type": "string"},
                "workspace_id": {"type": "string"},
                "workspace_domain": {"type": "string"},
                "workspace_base_url": {"type": "string"},
                "workspace_tours_url": {"type": "string"},
                "workflow_id": {"type": "string"},
                "run_url": {"type": "string"},
                "tour_title": {"type": "string"},
                "property_url": {"type": "string"},
                "media_urls_json": {"type": "array", "items": {"type": "string"}},
                "floorplan_urls_json": {"type": "array", "items": {"type": "string"}},
                "floorplan_analysis_json": {"type": "object"},
                "scene_strategy": {"type": "string"},
                "scene_selection_json": {"type": "object"},
                "property_facts_json": {"type": "object"},
                "creative_brief": {"type": "string"},
                "variant_key": {"type": "string"},
                "language": {"type": "string"},
                "theme_name": {"type": "string"},
                "tour_style": {"type": "string"},
                "audience": {"type": "string"},
                "call_to_action": {"type": "string"},
                "display_title": {"type": "string"},
                "tour_visibility": {"type": "string"},
                "tour_settings_json": {"type": "object"},
                "tour_patch_json": {"type": "object"},
                "tour_payload_json": {"type": ["object", "array", "string", "number", "boolean"]},
                "is_private": {"type": "boolean"},
                "runtime_inputs_json": {"type": "object"},
                "timeout_seconds": {"type": "integer"},
                "login_email": {"type": "string"},
                "login_password": {"type": "string"},
            },
        },
        "output_schema_json": {
            "type": "object",
            "required": ["deliverable_type"],
            "properties": {
                "deliverable_type": {"const": "property_tour_packet"},
                "tour_title": {"type": "string"},
                "tour_status": {"type": "string"},
                "tour_id": {"type": ["string", "null"]},
                "slug": {"type": ["string", "null"]},
                "share_url": {"type": ["string", "null"]},
                "editor_url": {"type": ["string", "null"]},
                "public_url": {"type": ["string", "null"]},
                "hosted_url": {"type": ["string", "null"]},
                "crezlo_public_url": {"type": ["string", "null"]},
                "workspace_id": {"type": ["string", "null"]},
                "workspace_domain": {"type": ["string", "null"]},
                "creation_mode": {"type": ["string", "null"]},
                "scene_count": {"type": "integer"},
                "workflow_id": {"type": ["string", "null"]},
                "task_id": {"type": ["string", "null"]},
                "structured_output_json": {"type": "object"},
            },
        },
        "authority_profile_json": {"authority_class": "draft", "review_class": "operator"},
        "provider_hints_json": {
            "primary": ["BrowserAct"],
            "output": ["Crezlo"],
            "notes": ["Steerable property-tour workflow backed by a BrowserAct Crezlo template."],
        },
        "tool_policy_json": {"allowed_tools": ["browseract.crezlo_property_tour", "artifact_repository"]},
        "human_policy_json": {"review_roles": ["automation_architect"]},
        "evaluation_cases_json": [{"case_key": "create_property_tour_golden", "priority": "medium"}],
        "budget_policy_json": {
            "class": "medium",
            "workflow_template": "tool_then_artifact",
            "pre_artifact_capability_key": "crezlo_property_tour",
            "browseract_failure_strategy": "retry",
            "browseract_max_attempts": 2,
            "browseract_retry_backoff_seconds": 1,
            "skill_catalog_json": {
                "mode": "tour_operator",
                "capabilities": ["property_media_ingest", "creative_variation", "tour_publish"],
            },
        },
    }


def main() -> int:
    skill = build_skill_payload()
    try:
        result = upsert_skill(skill)
    except urllib.error.URLError as exc:
        print(json.dumps({"status": "skipped", "reason": f"api_unavailable:{exc.reason}"}))
        return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        print(json.dumps({"status": "skipped", "reason": f"http_{exc.code}", "body": body[:240]}))
        return 0
    print(json.dumps({"status": "ok", "skill_key": result.get("skill_key", "")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
