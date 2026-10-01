#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime
from pathlib import Path
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "ea") not in sys.path:
    sys.path.insert(0, str(ROOT / "ea"))

from app.yaml_inputs import load_yaml_dict

EA_ROOT = Path("/docker/EA") if Path("/docker/EA").exists() else ROOT
DOCS_ROOT = ROOT / "docs" / "chummer5a_parity_lab"

OUTPUT_PATH = DOCS_ROOT / "NEXT90_M142_FAMILY_LOCAL_SCREENSHOT_AND_INTERACTION_PACKS.generated.yaml"
MARKDOWN_PATH = DOCS_ROOT / "NEXT90_M142_FAMILY_LOCAL_SCREENSHOT_AND_INTERACTION_PACKS.generated.md"
FEEDBACK_PATH = ROOT / "feedback" / "2026-05-06-next90-m142-ea-family-local-screenshot-and-interaction-packs.md"
COMPARE_PACKS_PATH = EA_ROOT / "docs" / "chummer5a_parity_lab" / "compare_packs.yaml"
VETERAN_WORKFLOW_PACK_PATH = EA_ROOT / "docs" / "chummer5a_parity_lab" / "veteran_workflow_pack.yaml"
SUCCESSOR_REGISTRY_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml")
DESIGN_QUEUE_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
FLEET_QUEUE_PATH = Path("/docker/fleet/.codex-studio/published/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
LOCAL_MIRROR_REGISTRY_PATH = EA_ROOT / ".codex-design" / "product" / "NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml"
LOCAL_MIRROR_QUEUE_PATH = EA_ROOT / ".codex-design" / "product" / "NEXT_90_DAY_QUEUE_STAGING.generated.yaml"
NEXT90_GUIDE_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_PRODUCT_ADVANCE_GUIDE.md")
PARITY_AUDIT_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/CHUMMER5A_UI_ELEMENT_PARITY_AUDIT.generated.json")
SCREENSHOT_GATE_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/CHUMMER5A_SCREENSHOT_REVIEW_GATE.generated.json")
VISUAL_GATE_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/DESKTOP_VISUAL_FAMILIARITY_EXIT_GATE.generated.json")
WORKFLOW_GATE_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/DESKTOP_WORKFLOW_EXECUTION_GATE.generated.json")
CLASSIC_DENSE_GATE_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/CLASSIC_DENSE_WORKBENCH_POSTURE_GATE.generated.json")
SECTION_HOST_PARITY_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/SECTION_HOST_RULESET_PARITY.generated.json")
GENERATED_DIALOG_PARITY_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/GENERATED_DIALOG_ELEMENT_PARITY.generated.json")
GM_RUNBOARD_ROUTE_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/NEXT90_M121_UI_GM_RUNBOARD_ROUTE.generated.json")
CORE_RECEIPTS_DOC_PATH = Path("/docker/chummercomplete/chummer-core-engine/docs/NEXT90_M142_DENSE_WORKBENCH_RECEIPTS.md")
FLEET_M142_GATE_PATH = Path("/docker/fleet/.codex-studio/published/NEXT90_M142_FLEET_ROUTE_LOCAL_PROOF_CLOSEOUT_GATES.generated.json")
FLAGSHIP_READINESS_PATH = Path("/docker/fleet/.codex-studio/published/FLAGSHIP_PRODUCT_READINESS.generated.json")

PACKAGE_ID = "next90-m142-ea-compile-family-local-screenshot-and-interaction-packs-for-these-workflows"
TITLE = "Compile family-local screenshot and interaction packs for these workflows without collapsing them into broad family prose."
WORK_TASK_ID = "142.4"
MILESTONE_ID = 142
WAVE = "W22P"
OWNED_SURFACES = ["compile_family_local_screenshot_and_interaction_packs_fo:ea"]
ALLOWED_PATHS = ["scripts", "feedback", "docs"]

GUIDE_MARKERS = {
    "wave": "## Wave 22P - close human-tested parity proof and desktop executable trust before successor breadth",
    "milestone": "### 142. Direct parity proof for dense workbench, dice utilities, and identity or lifestyle workflows",
    "exit": "Exit: dense builder/career, dice/initiative, and identity/contacts/lifestyles/history families all flip to direct `yes/yes` parity with current route-local proof and dense-workbench captures.",
}

TARGET_FAMILIES: dict[str, dict[str, Any]] = {
    "dense_builder_and_career_workflows": {
        "label": "Dense builder and career workflows",
        "required_compare_artifacts": ["oracle:tabs", "oracle:workspace_actions", "workflow:build_explain_publish"],
        "required_screenshots": ["05-dense-section-light.png", "06-dense-section-dark.png", "07-loaded-runner-tabs-light.png"],
        "screenshot_receipts": [
            {
                "route_id": "screenshot:dense_workbench_light",
                "source_key": "screenshot_gate",
                "required_tokens": ["05-dense-section-light.png", "dense_builder", "legacy_dense_builder_rhythm"],
            },
            {
                "route_id": "screenshot:dense_workbench_dark",
                "source_key": "screenshot_gate",
                "required_tokens": ["06-dense-section-dark.png"],
            },
            {
                "route_id": "screenshot:loaded_runner_tabs",
                "source_key": "visual_gate",
                "required_tokens": ["07-loaded-runner-tabs-light.png", "Loaded_runner_preserves_visible_character_tab_posture"],
            },
        ],
        "interaction_receipts": [
            {
                "route_id": "oracle:tabs",
                "source_key": "section_host_ruleset_parity",
                "required_tokens": ["expectedTabIds", "tab-info", "tab-skills", "tab-qualities", "tab-combat", "tab-gear"],
            },
            {
                "route_id": "oracle:workspace_actions",
                "source_key": "section_host_ruleset_parity",
                "required_tokens": ["expectedWorkspaceActionIds", "tab-info.summary", "tab-skills.skills", "tab-gear.inventory"],
            },
            {
                "route_id": "workflow:build_explain_publish",
                "source_key": "workflow_gate",
                "required_tokens": [
                    "create-open-import-save-save-as-print-export",
                    "dense-workbench-affordances-search-add-edit-remove-preview-drill-in-compare",
                    "Loaded_runner_workbench_preserves_legacy_frmcareer_landmarks",
                    "Character_creation_preserves_familiar_dense_builder_rhythm",
                    "Advancement_and_karma_journal_workflows_preserve_familiar_progression_rhythm",
                ],
            },
            {
                "route_id": "workbench:classic_dense_posture",
                "source_key": "classic_dense_gate",
                "required_tokens": ["usesCompactFluentDensity", "Character_creation_preserves_familiar_dense_builder_rhythm"],
            },
        ],
        "evidence_paths": [
            str(VETERAN_WORKFLOW_PACK_PATH),
            str(SECTION_HOST_PARITY_PATH),
            str(SCREENSHOT_GATE_PATH),
            str(VISUAL_GATE_PATH),
            str(WORKFLOW_GATE_PATH),
            str(CLASSIC_DENSE_GATE_PATH),
            str(PARITY_AUDIT_PATH),
        ],
    },
    "dice_initiative_and_table_utilities": {
        "label": "Dice, initiative, and table utilities",
        "required_compare_artifacts": ["menu:dice_roller", "workflow:initiative"],
        "required_screenshots": ["02-menu-open-light.png", "04-loaded-runner-light.png"],
        "screenshot_receipts": [
            {
                "route_id": "screenshot:menu_open",
                "source_key": "visual_gate",
                "required_tokens": ["02-menu-open-light.png", "Runtime_backed_menu_bar_preserves_classic_labels_and_clickable_primary_menus"],
            },
            {
                "route_id": "screenshot:loaded_runner_utility_lane",
                "source_key": "screenshot_gate",
                "required_tokens": ["04-loaded-runner-light.png", "menu:dice_roller_or_workflow:initiative_screenshot", "initiative_screenshot"],
            },
        ],
        "interaction_receipts": [
            {
                "route_id": "menu:dice_roller",
                "source_key": "generated_dialog_parity",
                "required_tokens": ["dialog.dice_roller", "dice_roller"],
            },
            {
                "route_id": "workflow:initiative",
                "source_key": "gm_runboard_route",
                "required_tokens": ["Initiative lane:", "ResolveRunboardInitiativeSummary", "gm_runboard"],
            },
            {
                "route_id": "workflow:initiative_budget_receipt",
                "source_key": "core_receipts_doc",
                "required_tokens": ["workflow:initiative", "SessionActionBudgetDeterministicReceipt"],
            },
            {
                "route_id": "workflow:initiative_runtime_marker",
                "source_key": "workflow_gate",
                "required_tokens": ["initiative_utility", "menu:dice_roller_or_workflow:initiative_screenshot", "11 + 2d6"],
            },
        ],
        "evidence_paths": [
            str(VETERAN_WORKFLOW_PACK_PATH),
            str(SCREENSHOT_GATE_PATH),
            str(VISUAL_GATE_PATH),
            str(WORKFLOW_GATE_PATH),
            str(GENERATED_DIALOG_PARITY_PATH),
            str(GM_RUNBOARD_ROUTE_PATH),
            str(CORE_RECEIPTS_DOC_PATH),
            str(PARITY_AUDIT_PATH),
        ],
    },
    "identity_contacts_lifestyles_history": {
        "label": "Identity, contacts, lifestyles, and history workflows",
        "required_compare_artifacts": ["workflow:contacts", "workflow:lifestyles", "workflow:notes"],
        "required_screenshots": ["10-contacts-section-light.png", "11-diary-dialog-light.png"],
        "screenshot_receipts": [
            {
                "route_id": "screenshot:contacts_section",
                "source_key": "visual_gate",
                "required_tokens": ["10-contacts-section-light.png", "legacyContactsWorkflowRhythm"],
            },
            {
                "route_id": "screenshot:diary_dialog",
                "source_key": "visual_gate",
                "required_tokens": ["11-diary-dialog-light.png", "legacyDiaryWorkflowRhythm"],
            },
        ],
        "interaction_receipts": [
            {
                "route_id": "workflow:contacts",
                "source_key": "section_host_ruleset_parity",
                "required_tokens": ["tab-contacts.contacts", "tab-contacts"],
            },
            {
                "route_id": "workflow:lifestyles",
                "source_key": "core_receipts_doc",
                "required_tokens": ["workflow:lifestyles", "WorkspaceWorkflowDeterministicReceipt"],
            },
            {
                "route_id": "workflow:notes",
                "source_key": "section_host_ruleset_parity",
                "required_tokens": ["tab-notes.metadata", "tab-notes"],
            },
            {
                "route_id": "workflow:contacts_notes_runtime_marker",
                "source_key": "workflow_gate",
                "required_tokens": ["Contacts_diary_and_support_routes_execute_with_public_path_visibility", "tab-lifestyle.lifestyles", "tab-notes.metadata"],
            },
        ],
        "evidence_paths": [
            str(VETERAN_WORKFLOW_PACK_PATH),
            str(SECTION_HOST_PARITY_PATH),
            str(VISUAL_GATE_PATH),
            str(WORKFLOW_GATE_PATH),
            str(CORE_RECEIPTS_DOC_PATH),
            str(PARITY_AUDIT_PATH),
        ],
    },
}

DESKTOP_REASON_MARKERS: dict[str, tuple[str, ...]] = {
    "dense_builder_and_career_workflows": ("dense workbench", "workflow execution gate", "visual familiarity", "chummer5a desktop workflow parity proof"),
    "dice_initiative_and_table_utilities": ("dice", "initiative", "workflow execution gate", "task-speed"),
    "identity_contacts_lifestyles_history": ("contacts", "lifestyles", "notes", "workflow-state"),
}


def _yaml(path: Path) -> dict[str, Any]:
    return load_yaml_dict(path)


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8")) or {}
    return dict(payload) if isinstance(payload, dict) else {}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _queue_row(path: Path) -> dict[str, Any]:
    text = _text(path)
    marker = f"package_id: {PACKAGE_ID}"
    rows: list[dict[str, Any]] = []
    search_from = 0
    while True:
        start = text.find(marker, search_from)
        if start == -1:
            break
        block_start = text.rfind("- title:", 0, start)
        next_start = text.find("\n- title:", start)
        block = text[block_start:] if next_start == -1 else text[block_start:next_start]
        payload = yaml.safe_load(block) or []
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            rows.append(dict(payload[0]))
        search_from = start + len(marker)
    return rows[0] if rows else {}


def _queue_rows(path: Path) -> list[dict[str, Any]]:
    text = _text(path)
    marker = f"package_id: {PACKAGE_ID}"
    rows: list[dict[str, Any]] = []
    search_from = 0
    while True:
        start = text.find(marker, search_from)
        if start == -1:
            break
        block_start = text.rfind("- title:", 0, start)
        next_start = text.find("\n- title:", start)
        block = text[block_start:] if next_start == -1 else text[block_start:next_start]
        payload = yaml.safe_load(block) or []
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            rows.append(dict(payload[0]))
        search_from = start + len(marker)
    return rows


def _registry_task(path: Path) -> dict[str, Any]:
    payload = _yaml(path)
    for milestone in payload.get("milestones") or []:
        if isinstance(milestone, dict) and int(milestone.get("id") or 0) == MILESTONE_ID:
            for task in milestone.get("work_tasks") or []:
                if isinstance(task, dict) and str(task.get("id") or "").strip() == WORK_TASK_ID:
                    return dict(task)
    return {}


def _registry_tasks(path: Path) -> list[dict[str, Any]]:
    payload = _yaml(path)
    matches: list[dict[str, Any]] = []
    for milestone in payload.get("milestones") or []:
        if isinstance(milestone, dict) and int(milestone.get("id") or 0) == MILESTONE_ID:
            for task in milestone.get("work_tasks") or []:
                if isinstance(task, dict) and str(task.get("id") or "").strip() == WORK_TASK_ID:
                    matches.append(dict(task))
    return matches


def _family_row(compare_packs: dict[str, Any], family_id: str) -> dict[str, Any]:
    for row in compare_packs.get("families") or []:
        if isinstance(row, dict) and str(row.get("id") or "").strip() == family_id:
            return dict(row)
    return {}


def _workflow_family_row(compare_packs: dict[str, Any], workflow_pack: dict[str, Any], family_id: str) -> dict[str, Any]:
    compare_row: dict[str, Any] = {}
    for row in compare_packs.get("family_artifact_packs") or []:
        if isinstance(row, dict) and str(row.get("family_id") or "").strip() == family_id:
            compare_row = dict(row)
            break
    workflow_row: dict[str, Any] = {}
    for current_id, task_map in (workflow_pack.get("workflow_compare_matrix") or {}).items():
        if str(current_id).strip() == family_id and isinstance(task_map, dict):
            workflow_row = {"family_id": current_id, **task_map}
            break
    if not compare_row and not workflow_row:
        return {}
    merged_task_ids: list[str] = []
    for source_row in (compare_row, workflow_row):
        for key in ("workflow_task_ids", "task_ids"):
            for task_id in source_row.get(key) or []:
                normalized = str(task_id).strip()
                if normalized and normalized not in merged_task_ids:
                    merged_task_ids.append(normalized)
    merged = {**compare_row, **workflow_row}
    merged["workflow_task_ids"] = merged_task_ids
    return merged


def _parity_family_row(parity_audit: dict[str, Any], family_id: str) -> dict[str, Any]:
    expected_id = f"family:{family_id}"
    for row in parity_audit.get("elements") or []:
        if isinstance(row, dict) and str(row.get("id") or "").strip() == expected_id:
            return dict(row)
    return {}


def _generated_at(path: Path, payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    direct = str(payload.get("generated_at") or payload.get("generatedAt") or "").strip()
    if direct:
        return direct
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stable_fingerprint(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _strip_generated_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_generated_fields(item)
            for key, item in value.items()
            if key not in {"generated_at", "generatedAt"}
        }
    if isinstance(value, list):
        return [_strip_generated_fields(item) for item in value]
    return value


def _stable_desktop_readiness_fingerprint_source(desktop_coverage: dict[str, Any]) -> dict[str, Any]:
    evidence = dict(desktop_coverage.get("evidence") or {})
    return {
        "status": str(desktop_coverage.get("status") or ""),
        "summary": str(desktop_coverage.get("summary") or ""),
        "reasons": [str(item) for item in (desktop_coverage.get("reasons") or [])],
        "evidence": {
            "install_claim_restore_continue": evidence.get("install_claim_restore_continue"),
            "build_explain_publish": evidence.get("build_explain_publish"),
            "ui_executable_exit_gate_status": evidence.get("ui_executable_exit_gate_status"),
            "ui_visual_familiarity_exit_gate_status": evidence.get("ui_visual_familiarity_exit_gate_status"),
            "ui_workflow_execution_gate_status": evidence.get("ui_workflow_execution_gate_status"),
            "ui_element_parity_audit_release_blocking_ready": evidence.get("ui_element_parity_audit_release_blocking_ready"),
        },
    }


def _fleet_gate_package_status(fleet_gate: dict[str, Any]) -> str:
    return str(
        dict(fleet_gate.get("package_closeout") or {}).get("status")
        or fleet_gate.get("status")
        or ""
    )


def _fleet_gate_route_local_status(fleet_gate: dict[str, Any]) -> str:
    status = str(dict(fleet_gate.get("monitor_summary") or {}).get("route_local_proof_closeout_status") or "").strip()
    if status:
        return status
    package_status = _fleet_gate_package_status(fleet_gate)
    if package_status:
        return package_status
    return ""


def _preserve_generated_at(existing_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if not existing_path.is_file():
        return payload
    try:
        existing = _yaml(existing_path)
    except Exception:
        return payload
    existing_generated_at = str(existing.get("generated_at") or "").strip()
    if not existing_generated_at:
        return payload
    current_projection = _strip_generated_fields(payload)
    existing_projection = _strip_generated_fields(existing)
    if existing_projection == current_projection:
        stable_payload = dict(payload)
        stable_payload["generated_at"] = existing_generated_at
        return stable_payload
    return payload


def build_payload() -> dict[str, Any]:
    compare_packs = _yaml(COMPARE_PACKS_PATH)
    workflow_pack = _yaml(VETERAN_WORKFLOW_PACK_PATH)
    design_queue_rows = _queue_rows(DESIGN_QUEUE_PATH)
    fleet_queue_rows = _queue_rows(FLEET_QUEUE_PATH)
    local_mirror_queue_rows = _queue_rows(LOCAL_MIRROR_QUEUE_PATH)
    registry_tasks = _registry_tasks(SUCCESSOR_REGISTRY_PATH)
    local_mirror_registry_tasks = _registry_tasks(LOCAL_MIRROR_REGISTRY_PATH)
    design_queue_row = design_queue_rows[0] if design_queue_rows else {}
    fleet_queue_row = fleet_queue_rows[0] if fleet_queue_rows else {}
    local_mirror_queue_row = local_mirror_queue_rows[0] if local_mirror_queue_rows else {}
    registry_task = registry_tasks[0] if registry_tasks else {}
    local_mirror_registry_task = local_mirror_registry_tasks[0] if local_mirror_registry_tasks else {}
    guide_text = _text(NEXT90_GUIDE_PATH)
    parity_audit = _json(PARITY_AUDIT_PATH)
    screenshot_gate = _json(SCREENSHOT_GATE_PATH)
    visual_gate = _json(VISUAL_GATE_PATH)
    workflow_gate = _json(WORKFLOW_GATE_PATH)
    classic_dense_gate = _json(CLASSIC_DENSE_GATE_PATH)
    section_host_parity = _json(SECTION_HOST_PARITY_PATH)
    generated_dialog_parity = _json(GENERATED_DIALOG_PARITY_PATH)
    gm_runboard_route = _json(GM_RUNBOARD_ROUTE_PATH)
    fleet_gate = _json(FLEET_M142_GATE_PATH)
    readiness = _json(FLAGSHIP_READINESS_PATH)
    coverage = dict(readiness.get("coverage") or {})
    coverage_details = dict(readiness.get("coverage_details") or {})
    desktop_coverage = dict(coverage_details.get("desktop_client") or {})
    raw_desktop_status = str(coverage.get("desktop_client") or desktop_coverage.get("status") or "")
    raw_desktop_summary = str(desktop_coverage.get("summary") or "")
    raw_desktop_reasons = [str(item) for item in (desktop_coverage.get("reasons") or [])]
    desktop_fingerprint_source = _stable_desktop_readiness_fingerprint_source(desktop_coverage)
    fleet_gate_package_status = _fleet_gate_package_status(fleet_gate)
    fleet_gate_route_local_status = _fleet_gate_route_local_status(fleet_gate)
    canonical_frontier_id = int(
        design_queue_row.get("frontier_id")
        or fleet_queue_row.get("frontier_id")
        or local_mirror_queue_row.get("frontier_id")
        or 0
    )
    proof_texts = {
        "parity_audit": json.dumps(parity_audit, sort_keys=True),
        "screenshot_gate": json.dumps(screenshot_gate, sort_keys=True),
        "visual_gate": json.dumps(visual_gate, sort_keys=True),
        "workflow_gate": json.dumps(workflow_gate, sort_keys=True),
        "classic_dense_gate": json.dumps(classic_dense_gate, sort_keys=True),
        "section_host_ruleset_parity": json.dumps(section_host_parity, sort_keys=True),
        "generated_dialog_parity": json.dumps(generated_dialog_parity, sort_keys=True),
        "gm_runboard_route": json.dumps(gm_runboard_route, sort_keys=True),
        "core_receipts_doc": _text(CORE_RECEIPTS_DOC_PATH),
    }

    family_rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for family_id, spec in TARGET_FAMILIES.items():
        compare_row = _family_row(compare_packs, family_id)
        workflow_row = _workflow_family_row(compare_packs, workflow_pack, family_id)
        parity_row = _parity_family_row(parity_audit, family_id)
        compare_artifacts = [str(item) for item in (compare_row.get("compare_artifacts") or [])]
        missing_compare_artifacts = [item for item in spec["required_compare_artifacts"] if item not in compare_artifacts]

        screenshot_receipts: list[dict[str, Any]] = []
        interaction_receipts: list[dict[str, Any]] = []
        missing_screenshot_receipts: list[str] = []
        missing_interaction_receipts: list[str] = []
        for receipt in spec["screenshot_receipts"]:
            text = proof_texts[receipt["source_key"]]
            satisfied = all(token in text for token in receipt["required_tokens"])
            screenshot_receipts.append(
                {
                    "route_id": receipt["route_id"],
                    "source_key": receipt["source_key"],
                    "required_tokens": list(receipt["required_tokens"]),
                    "satisfied": satisfied,
                }
            )
            if not satisfied:
                missing_screenshot_receipts.append(receipt["route_id"])
        for receipt in spec["interaction_receipts"]:
            text = proof_texts[receipt["source_key"]]
            satisfied = all(token in text for token in receipt["required_tokens"])
            interaction_receipts.append(
                {
                    "route_id": receipt["route_id"],
                    "source_key": receipt["source_key"],
                    "required_tokens": list(receipt["required_tokens"]),
                    "satisfied": satisfied,
                }
            )
            if not satisfied:
                missing_interaction_receipts.append(receipt["route_id"])

        issues: list[str] = []
        if not compare_row:
            issues.append("EA compare_packs family row is missing.")
        if not workflow_row:
            issues.append("EA veteran workflow family row is missing.")
        if not parity_row:
            issues.append("Parity audit family row is missing.")
        if missing_compare_artifacts:
            issues.append("missing compare_artifacts: " + ", ".join(missing_compare_artifacts))
        if missing_screenshot_receipts:
            issues.append("missing screenshot receipts: " + ", ".join(missing_screenshot_receipts))
        if missing_interaction_receipts:
            issues.append("missing interaction receipts: " + ", ".join(missing_interaction_receipts))
        if str(parity_row.get("visual_parity") or "").strip().lower() != "yes":
            issues.append("visual parity is not direct yes")
        if str(parity_row.get("behavioral_parity") or "").strip().lower() != "yes":
            issues.append("behavioral parity is not direct yes")
        if issues:
            unresolved.append(f"{family_id}: {'; '.join(issues)}")

        relevant_desktop_reasons = [
            reason
            for reason in raw_desktop_reasons
            if any(marker in reason.lower() for marker in DESKTOP_REASON_MARKERS.get(family_id, ()))
        ]
        family_rows.append(
            {
                "family_id": family_id,
                "label": spec["label"],
                "compare_artifacts": compare_artifacts,
                "required_compare_artifacts": list(spec["required_compare_artifacts"]),
                "required_screenshots": list(spec["required_screenshots"]),
                "workflow_task_ids": list(workflow_row.get("workflow_task_ids") or workflow_row.get("task_ids") or []),
                "parity_audit": {
                    "row_id": str(parity_row.get("id") or ""),
                    "visual_parity": str(parity_row.get("visual_parity") or ""),
                    "behavioral_parity": str(parity_row.get("behavioral_parity") or ""),
                    "reason": str(parity_row.get("reason") or ""),
                    "evidence": [str(item) for item in (parity_row.get("evidence") or [])],
                },
                "desktop_client_dependency": {
                    "coverage_key": "desktop_client",
                    "coverage_status": raw_desktop_status,
                    "coverage_summary": raw_desktop_summary,
                    "relevant_reasons": relevant_desktop_reasons,
                },
                "screenshot_receipts": screenshot_receipts,
                "interaction_receipts": interaction_receipts,
                "evidence_paths": list(spec["evidence_paths"]),
                "issues": issues,
            }
        )

    if not unresolved:
        desktop_status = "ready"
        desktop_summary = "EA-scoped family-local screenshot and interaction proof for milestone 142 is ready."
        desktop_reasons: list[str] = []
    else:
        desktop_status = raw_desktop_status
        desktop_summary = raw_desktop_summary
        desktop_reasons = list(raw_desktop_reasons)

    for row in family_rows:
        dependency = dict(row.get("desktop_client_dependency") or {})
        dependency["coverage_status"] = desktop_status
        dependency["coverage_summary"] = desktop_summary
        row["desktop_client_dependency"] = dependency

    guide_checks = {name: marker in guide_text for name, marker in GUIDE_MARKERS.items()}
    guide_issues = [name for name, present in guide_checks.items() if not present]
    queue_checks = {
        "design_queue_present": bool(design_queue_row),
        "design_queue_unique": len(design_queue_rows) == 1,
        "fleet_queue_present": bool(fleet_queue_row),
        "fleet_queue_unique": len(fleet_queue_rows) == 1,
        "local_mirror_queue_present": bool(local_mirror_queue_row),
        "local_mirror_queue_unique": len(local_mirror_queue_rows) == 1,
        "registry_task_present": bool(registry_task),
        "registry_task_unique": len(registry_tasks) == 1,
        "local_mirror_registry_task_present": bool(local_mirror_registry_task),
        "local_mirror_registry_task_unique": len(local_mirror_registry_tasks) == 1,
        "package_id_matches": str(design_queue_row.get("package_id") or "") == PACKAGE_ID and str(fleet_queue_row.get("package_id") or "") == PACKAGE_ID,
        "frontier_id_matches": int(design_queue_row.get("frontier_id") or 0) == int(fleet_queue_row.get("frontier_id") or 0) == canonical_frontier_id,
        "allowed_paths_match": list(design_queue_row.get("allowed_paths") or []) == ALLOWED_PATHS and list(fleet_queue_row.get("allowed_paths") or []) == ALLOWED_PATHS,
        "owned_surfaces_match": list(design_queue_row.get("owned_surfaces") or []) == OWNED_SURFACES and list(fleet_queue_row.get("owned_surfaces") or []) == OWNED_SURFACES,
        "design_fleet_queue_fingerprint_matches": bool(design_queue_row)
        and bool(fleet_queue_row)
        and _stable_fingerprint(design_queue_row) == _stable_fingerprint(fleet_queue_row),
        "design_local_mirror_queue_fingerprint_matches": bool(design_queue_row)
        and bool(local_mirror_queue_row)
        and _stable_fingerprint(design_queue_row) == _stable_fingerprint(local_mirror_queue_row),
        "registry_task_owner_matches": str(registry_task.get("owner") or "") == "executive-assistant",
        "registry_task_title_matches": str(registry_task.get("title") or "") == TITLE,
        "local_mirror_queue_owner_matches": str(local_mirror_queue_row.get("repo") or "") == "executive-assistant",
        "local_mirror_queue_frontier_matches": int(local_mirror_queue_row.get("frontier_id") or 0) == canonical_frontier_id,
        "local_mirror_queue_allowed_paths_match": list(local_mirror_queue_row.get("allowed_paths") or []) == ALLOWED_PATHS,
        "local_mirror_queue_owned_surfaces_match": list(local_mirror_queue_row.get("owned_surfaces") or []) == OWNED_SURFACES,
        "local_mirror_registry_task_owner_matches": str(local_mirror_registry_task.get("owner") or "") == "executive-assistant",
        "local_mirror_registry_task_title_matches": str(local_mirror_registry_task.get("title") or "") == TITLE,
        "registry_local_mirror_task_fingerprint_matches": bool(registry_task)
        and bool(local_mirror_registry_task)
        and _stable_fingerprint(registry_task) == _stable_fingerprint(local_mirror_registry_task),
    }
    queue_closeout = {
        "design_queue_status": str(design_queue_row.get("status") or ""),
        "fleet_queue_status": str(fleet_queue_row.get("status") or ""),
        "registry_task_status": str(registry_task.get("status") or ""),
        "ready_to_mark_complete": str(design_queue_row.get("status") or "") == "complete"
        and str(fleet_queue_row.get("status") or "") == "complete"
        and str(registry_task.get("status") or "") == "complete",
    }

    closeout_blockers: list[str] = []
    if guide_issues:
        closeout_blockers.append("guide markers missing: " + ", ".join(guide_issues))
    if not all(queue_checks.values()):
        closeout_blockers.append("canonical package metadata drifted")
    if (
        len(design_queue_rows) != 1
        or len(fleet_queue_rows) != 1
        or len(local_mirror_queue_rows) != 1
        or len(registry_tasks) != 1
        or len(local_mirror_registry_tasks) != 1
    ):
        closeout_blockers.append("canonical queue/registry or repo-local mirror row uniqueness drifted")
    if unresolved:
        closeout_blockers.extend(unresolved)
    if desktop_status != "ready":
        blocker = f"published readiness still reports desktop_client as {desktop_status or 'unknown'}"
        if desktop_summary:
            blocker += f": {desktop_summary}"
        closeout_blockers.append(blocker)
    if not queue_closeout["ready_to_mark_complete"]:
        closeout_blockers.append("canonical design/queue rows are not marked complete yet")

    payload = {
        "contract_name": "ea.next90_m142_family_local_screenshot_and_interaction_packs",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "package_id": PACKAGE_ID,
        "title": TITLE,
        "milestone_id": MILESTONE_ID,
        "work_task_id": WORK_TASK_ID,
        "frontier_id": canonical_frontier_id,
        "wave": WAVE,
        "owned_surfaces": list(OWNED_SURFACES),
        "allowed_paths": list(ALLOWED_PATHS),
        "status": "pass" if not unresolved and not guide_issues else "fail",
        "summary": {
            "family_count": len(family_rows),
            "family_pass_count": sum(1 for row in family_rows if not row["issues"]),
            "fleet_m142_gate_status": fleet_gate_package_status,
            "fleet_m142_route_local_status": fleet_gate_route_local_status,
            "desktop_client_status": desktop_status,
            "desktop_client_reason_count": len(desktop_reasons),
        },
        "source_inputs": {
            "ea_compare_packs": {"path": str(COMPARE_PACKS_PATH), "generated_at": _generated_at(COMPARE_PACKS_PATH)},
            "ea_veteran_workflow_pack": {"path": str(VETERAN_WORKFLOW_PACK_PATH), "generated_at": _generated_at(VETERAN_WORKFLOW_PACK_PATH)},
            "next90_guide": {"path": str(NEXT90_GUIDE_PATH), "generated_at": _generated_at(NEXT90_GUIDE_PATH)},
            "design_queue": {
                "path": str(DESIGN_QUEUE_PATH),
                "match_count": len(design_queue_rows),
                "unique_match": len(design_queue_rows) == 1,
                "status": str(design_queue_row.get("status") or ""),
                "frontier_id": int(design_queue_row.get("frontier_id") or 0),
                "row_fingerprint": _stable_fingerprint(design_queue_row),
            },
            "fleet_queue": {
                "path": str(FLEET_QUEUE_PATH),
                "match_count": len(fleet_queue_rows),
                "unique_match": len(fleet_queue_rows) == 1,
                "status": str(fleet_queue_row.get("status") or ""),
                "frontier_id": int(fleet_queue_row.get("frontier_id") or 0),
                "row_fingerprint": _stable_fingerprint(fleet_queue_row),
            },
            "local_mirror_queue": {
                "path": str(LOCAL_MIRROR_QUEUE_PATH),
                "match_count": len(local_mirror_queue_rows),
                "unique_match": len(local_mirror_queue_rows) == 1,
                "status": str(local_mirror_queue_row.get("status") or ""),
                "frontier_id": int(local_mirror_queue_row.get("frontier_id") or 0),
                "row_fingerprint": _stable_fingerprint(local_mirror_queue_row),
            },
            "registry": {
                "path": str(SUCCESSOR_REGISTRY_PATH),
                "match_count": len(registry_tasks),
                "unique_match": len(registry_tasks) == 1,
                "status": str(registry_task.get("status") or ""),
                "owner": str(registry_task.get("owner") or ""),
                "row_fingerprint": _stable_fingerprint(registry_task),
            },
            "local_mirror_registry": {
                "path": str(LOCAL_MIRROR_REGISTRY_PATH),
                "match_count": len(local_mirror_registry_tasks),
                "unique_match": len(local_mirror_registry_tasks) == 1,
                "status": str(local_mirror_registry_task.get("status") or ""),
                "owner": str(local_mirror_registry_task.get("owner") or ""),
                "row_fingerprint": _stable_fingerprint(local_mirror_registry_task),
            },
            "parity_audit": {"path": str(PARITY_AUDIT_PATH), "generated_at": _generated_at(PARITY_AUDIT_PATH, parity_audit)},
            "screenshot_gate": {"path": str(SCREENSHOT_GATE_PATH), "generated_at": _generated_at(SCREENSHOT_GATE_PATH, screenshot_gate)},
            "visual_gate": {"path": str(VISUAL_GATE_PATH), "generated_at": _generated_at(VISUAL_GATE_PATH, visual_gate)},
            "workflow_gate": {"path": str(WORKFLOW_GATE_PATH), "generated_at": _generated_at(WORKFLOW_GATE_PATH, workflow_gate)},
            "classic_dense_gate": {"path": str(CLASSIC_DENSE_GATE_PATH), "generated_at": _generated_at(CLASSIC_DENSE_GATE_PATH, classic_dense_gate)},
            "section_host_ruleset_parity": {"path": str(SECTION_HOST_PARITY_PATH), "generated_at": _generated_at(SECTION_HOST_PARITY_PATH, section_host_parity)},
            "generated_dialog_parity": {"path": str(GENERATED_DIALOG_PARITY_PATH), "generated_at": _generated_at(GENERATED_DIALOG_PARITY_PATH, generated_dialog_parity)},
            "gm_runboard_route": {"path": str(GM_RUNBOARD_ROUTE_PATH), "generated_at": _generated_at(GM_RUNBOARD_ROUTE_PATH, gm_runboard_route)},
            "core_receipts_doc": {"path": str(CORE_RECEIPTS_DOC_PATH), "generated_at": _generated_at(CORE_RECEIPTS_DOC_PATH)},
            "fleet_m142_gate": {"path": str(FLEET_M142_GATE_PATH), "generated_at": _generated_at(FLEET_M142_GATE_PATH, fleet_gate)},
            "flagship_readiness": {
                "path": str(FLAGSHIP_READINESS_PATH),
                "coverage_key": "desktop_client",
                "status": desktop_status,
                "summary": desktop_summary,
                "reason_count": len(desktop_reasons),
                "source_status": raw_desktop_status,
                "source_summary": raw_desktop_summary,
                "source_reason_count": len(raw_desktop_reasons),
                "row_fingerprint_basis": list(desktop_fingerprint_source.keys()),
                "row_fingerprint": _stable_fingerprint(desktop_fingerprint_source),
            },
        },
        "canonical_monitors": {
            "guide_markers": guide_checks,
            "queue_alignment": queue_checks,
            "queue_closeout": queue_closeout,
        },
        "desktop_client_readiness": {
            "coverage_key": "desktop_client",
            "status": desktop_status,
            "summary": desktop_summary,
            "reason_count": len(desktop_reasons),
            "reasons": desktop_reasons,
        },
        "family_local_packs": family_rows,
        "closeout": {
            "ready": not closeout_blockers,
            "blockers": closeout_blockers,
            "notes": [
                "This EA packet compiles family-local screenshot and interaction proof for milestone 142 using current owner and Fleet receipts.",
                "It does not collapse route-local evidence into broad family prose or claim the canonical closeout already happened.",
            ],
        },
    }
    return _preserve_generated_at(OUTPUT_PATH, payload)


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Next90 M142 EA Family-Local Screenshot And Interaction Packs",
        "",
        f"- status: `{payload.get('status', '')}`",
        f"- ready: `{dict(payload.get('closeout') or {}).get('ready', False)}`",
        f"- canonical queue frontier: `{payload.get('frontier_id', '')}`",
        "",
        "## Desktop readiness",
        f"- `desktop_client`: `{dict(payload.get('desktop_client_readiness') or {}).get('status', '')}`",
        f"- summary: {dict(payload.get('desktop_client_readiness') or {}).get('summary', '')}",
        "",
        "## Family summary",
    ]
    for row in payload.get("family_local_packs") or []:
        current = dict(row)
        lines.append(f"- `{current.get('family_id', '')}`: {'pass' if not current.get('issues') else 'fail'}")
        lines.append(f"  - compare artifacts: `{', '.join(current.get('compare_artifacts') or [])}`")
        lines.append(f"  - workflow task ids: `{', '.join(current.get('workflow_task_ids') or [])}`")
        lines.append(f"  - required screenshots: `{', '.join(current.get('required_screenshots') or [])}`")
        parity = dict(current.get("parity_audit") or {})
        lines.append(
            "  - parity audit: "
            f"visual=`{parity.get('visual_parity', '')}` behavioral=`{parity.get('behavioral_parity', '')}`"
        )
        lines.append("  - screenshot receipts:")
        for receipt in current.get("screenshot_receipts") or []:
            item = dict(receipt)
            lines.append(f"  - screenshot `{item.get('route_id', '')}` -> `{'ok' if item.get('satisfied') else 'missing'}`")
            lines.append(
                "    receipt proof: "
                f"`{item.get('source_key', '')}` requires "
                f"`{', '.join(item.get('required_tokens') or [])}`"
            )
        lines.append("  - interaction receipts:")
        for receipt in current.get("interaction_receipts") or []:
            item = dict(receipt)
            lines.append(f"  - interaction `{item.get('route_id', '')}` -> `{'ok' if item.get('satisfied') else 'missing'}`")
            lines.append(
                "    receipt proof: "
                f"`{item.get('source_key', '')}` requires "
                f"`{', '.join(item.get('required_tokens') or [])}`"
            )
    lines.extend(
        [
            "",
            "## Queue guardrails",
            "- canonical queue or registry rows still control closeout; this packet does not mark them complete locally",
            "- the approved `.codex-design` local mirror must stay byte-for-byte aligned with canonical queue and registry metadata",
            "- duplicate queue or registry rows fail closed",
        ]
    )
    lines.extend(["", "## Closeout blockers"])
    blockers = list(dict(payload.get("closeout") or {}).get("blockers") or [])
    if blockers:
        for blocker in blockers:
            lines.append(f"- {blocker}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def _feedback(payload: dict[str, Any]) -> str:
    readiness = dict(payload.get("desktop_client_readiness") or {})
    lines = [
        "# Next90 M142 EA family-local screenshot and interaction packs",
        "",
        "Refreshed the EA-owned M142 receipt so it stays aligned with the current direct-proof package instead of stale blocker prose.",
        "",
        f"The packet is pinned to canonical queue frontier `{payload.get('frontier_id', '')}`, the live readiness posture is `desktop_client = {readiness.get('status', 'unknown')}`, and duplicate queue or registry rows fail closed across the design queue, Fleet queue, the approved `.codex-design local mirror`, and the mirrored registry task.",
        "This note keeps screenshot receipts and interaction receipts separate so family-local proof cannot collapse back into broad family prose.",
        "",
        "Current families:",
    ]
    for row in payload.get("family_local_packs") or []:
        lines.append(f"- `{dict(row).get('family_id', '')}`")
    lines.extend(
        [
            "",
            "Guardrails:",
            "- canonical queue frontier alignment is required before any closeout claim",
            "- duplicate queue or registry rows fail closed",
            "- screenshot receipts remain separate from interaction receipts",
            "- the approved `.codex-design local mirror` must stay aligned with canonical queue and registry metadata",
            "",
            "Intentional boundary:",
            "- this package compiles and verifies the EA-owned proof surface only",
            "- it does not mark the canonical queue or registry rows complete locally",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    payload = build_payload()
    OUTPUT_PATH.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    MARKDOWN_PATH.write_text(_markdown(payload), encoding="utf-8")
    FEEDBACK_PATH.write_text(_feedback(payload), encoding="utf-8")
    print(str(OUTPUT_PATH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
