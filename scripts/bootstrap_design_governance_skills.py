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


SKILLS: list[dict[str, object]] = [
    {
        "skill_key": "design_petition",
        "task_key": "design_petition",
        "name": "Design Petition",
        "description": "Normalize a blocked-by-design issue into a concise petition packet for chummer6-design instead of letting a worker invent local truth.",
        "deliverable_type": "design_petition_packet",
        "default_risk_class": "medium",
        "default_approval_class": "none",
        "workflow_template": "artifact_then_memory_candidate",
        "allowed_tools": ["artifact_repository"],
        "evidence_requirements": ["blocked_repo_context", "boundary_context", "contract_context"],
        "memory_write_policy": "reviewed_only",
        "memory_reads": ["design_scope", "contract_sets", "feedback_findings"],
        "memory_writes": ["design_petition_fact"],
        "tags": ["design", "petition", "governance"],
        "authority_profile_json": {"authority_class": "draft", "review_class": "design"},
        "model_policy_json": {"brain_profile": "review_light"},
        "provider_hints_json": {
            "primary": ["ChatPlayground AI"],
            "secondary": ["Gemini Vortex"],
            "notes": ["Escalation packets should be cheap, crisp, and grounded in the existing canon."],
        },
        "tool_policy_json": {"allowed_tools": ["artifact_repository"]},
        "human_policy_json": {"review_roles": ["lead_designer"]},
        "evaluation_cases_json": [{"case_key": "design_petition_packet_golden", "priority": "medium"}],
        "budget_policy_json": {
            "class": "low",
            "workflow_template": "artifact_then_memory_candidate",
            "memory_candidate_category": "design_petition_fact",
            "memory_candidate_confidence": 0.72,
            "memory_candidate_sensitivity": "internal",
        },
    },
    {
        "skill_key": "design_synthesis",
        "task_key": "design_synthesis",
        "name": "Design Synthesis",
        "description": "Collapse repeated feedback or uncovered-scope findings into one clearer blocker, task, or no-change decision for design canon.",
        "deliverable_type": "design_synthesis_packet",
        "default_risk_class": "medium",
        "default_approval_class": "none",
        "workflow_template": "artifact_then_memory_candidate",
        "allowed_tools": ["artifact_repository"],
        "evidence_requirements": ["feedback_inputs", "canon_context"],
        "memory_write_policy": "reviewed_only",
        "memory_reads": ["design_scope", "feedback_findings", "public_status"],
        "memory_writes": ["design_synthesis_fact"],
        "tags": ["design", "synthesis", "governance"],
        "authority_profile_json": {"authority_class": "draft", "review_class": "design"},
        "model_policy_json": {"brain_profile": "groundwork"},
        "provider_hints_json": {
            "primary": ["Gemini Vortex"],
            "secondary": ["ChatPlayground AI"],
            "notes": ["Use the reasoning lane for clustering and root-cause reduction, not for bookkeeping."],
        },
        "tool_policy_json": {"allowed_tools": ["artifact_repository"]},
        "human_policy_json": {"review_roles": ["lead_designer"]},
        "evaluation_cases_json": [{"case_key": "design_synthesis_packet_golden", "priority": "high"}],
        "budget_policy_json": {
            "class": "low",
            "workflow_template": "artifact_then_memory_candidate",
            "memory_candidate_category": "design_synthesis_fact",
            "memory_candidate_confidence": 0.75,
            "memory_candidate_sensitivity": "internal",
        },
    },
    {
        "skill_key": "mirror_status_brief",
        "task_key": "mirror_status_brief",
        "name": "Mirror Status Brief",
        "description": "Turn raw mirror parity state into a short human brief so designers do not have to parse checksum walls.",
        "deliverable_type": "mirror_status_brief",
        "default_risk_class": "low",
        "default_approval_class": "none",
        "workflow_template": "rewrite",
        "allowed_tools": ["artifact_repository"],
        "evidence_requirements": ["mirror_status_payload"],
        "memory_write_policy": "none",
        "memory_reads": ["design_scope"],
        "memory_writes": [],
        "tags": ["design", "mirrors", "status"],
        "authority_profile_json": {"authority_class": "draft", "review_class": "operator"},
        "model_policy_json": {"brain_profile": "review_light"},
        "provider_hints_json": {
            "primary": ["ChatPlayground AI"],
            "notes": ["Summaries should stay compact and evidence-backed."],
        },
        "tool_policy_json": {"allowed_tools": ["artifact_repository"]},
        "human_policy_json": {"review_roles": ["lead_designer", "operator"]},
        "evaluation_cases_json": [{"case_key": "mirror_status_brief_golden", "priority": "medium"}],
        "budget_policy_json": {"class": "low", "workflow_template": "rewrite"},
    },
]


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


def main() -> int:
    results: list[dict[str, object]] = []
    for skill in SKILLS:
        try:
            result = upsert_skill(skill)
        except urllib.error.URLError as exc:
            print(json.dumps({"status": "skipped", "reason": f"api_unavailable:{exc.reason}"}))
            return 0
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            print(json.dumps({"status": "skipped", "reason": f"http_{exc.code}", "body": body[:240]}))
            return 0
        results.append({"skill_key": result.get("skill_key", ""), "task_key": result.get("task_key", "")})
    print(json.dumps({"status": "ok", "skills": results}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
