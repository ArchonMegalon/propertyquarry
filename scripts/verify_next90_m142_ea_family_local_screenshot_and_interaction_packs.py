#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "ea") not in sys.path:
    sys.path.insert(0, str(ROOT / "ea"))

from app.yaml_inputs import load_yaml_dict

DOCS_ROOT = ROOT / "docs" / "chummer5a_parity_lab"
PACK_PATH = DOCS_ROOT / "NEXT90_M142_FAMILY_LOCAL_SCREENSHOT_AND_INTERACTION_PACKS.generated.yaml"
MARKDOWN_PATH = DOCS_ROOT / "NEXT90_M142_FAMILY_LOCAL_SCREENSHOT_AND_INTERACTION_PACKS.generated.md"
FEEDBACK_PATH = ROOT / "feedback" / "2026-05-06-next90-m142-ea-family-local-screenshot-and-interaction-packs.md"

PACKAGE_ID = "next90-m142-ea-compile-family-local-screenshot-and-interaction-packs-for-these-workflows"
EXPECTED_FAMILIES = {
    "dense_builder_and_career_workflows",
    "dice_initiative_and_table_utilities",
    "identity_contacts_lifestyles_history",
}
QUEUE_STAGING_PATH = Path("/docker/fleet/.codex-studio/published/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
EXPECTED_MARKDOWN_RECEIPTS = {
    "dense_builder_and_career_workflows": {
        "compare artifacts: `oracle:tabs, oracle:workspace_actions, workflow:build_explain_publish`",
        "workflow task ids: `reach_real_workbench, recover_section_rhythm`",
        "required screenshots: `05-dense-section-light.png, 06-dense-section-dark.png, 07-loaded-runner-tabs-light.png`",
        "- screenshot receipts:",
        "screenshot `screenshot:dense_workbench_light` -> `ok`",
        "receipt proof: `screenshot_gate` requires `05-dense-section-light.png, dense_builder, legacy_dense_builder_rhythm`",
        "- interaction receipts:",
        "interaction `workflow:build_explain_publish` -> `ok`",
        "receipt proof: `workflow_gate` requires `create-open-import-save-save-as-print-export, dense-workbench-affordances-search-add-edit-remove-preview-drill-in-compare, Loaded_runner_workbench_preserves_legacy_frmcareer_landmarks, Character_creation_preserves_familiar_dense_builder_rhythm, Advancement_and_karma_journal_workflows_preserve_familiar_progression_rhythm`",
    },
    "dice_initiative_and_table_utilities": {
        "compare artifacts: `menu:dice_roller, workflow:initiative`",
        "workflow task ids: `locate_save_import_settings`",
        "required screenshots: `02-menu-open-light.png, 04-loaded-runner-light.png`",
        "- screenshot receipts:",
        "screenshot `screenshot:menu_open` -> `ok`",
        "receipt proof: `visual_gate` requires `02-menu-open-light.png, Runtime_backed_menu_bar_preserves_classic_labels_and_clickable_primary_menus`",
        "- interaction receipts:",
        "interaction `workflow:initiative_runtime_marker` -> `ok`",
        "receipt proof: `workflow_gate` requires `initiative_utility, menu:dice_roller_or_workflow:initiative_screenshot, 11 + 2d6`",
    },
    "identity_contacts_lifestyles_history": {
        "compare artifacts: `workflow:contacts, workflow:lifestyles, workflow:notes`",
        "workflow task ids: `recover_section_rhythm`",
        "required screenshots: `10-contacts-section-light.png, 11-diary-dialog-light.png`",
        "- screenshot receipts:",
        "screenshot `screenshot:contacts_section` -> `ok`",
        "receipt proof: `visual_gate` requires `10-contacts-section-light.png, legacyContactsWorkflowRhythm`",
        "- interaction receipts:",
        "interaction `workflow:contacts_notes_runtime_marker` -> `ok`",
        "receipt proof: `workflow_gate` requires `Contacts_diary_and_support_routes_execute_with_public_path_visibility, tab-lifestyle.lifestyles, tab-notes.metadata`",
    },
}
EXPECTED_WORKFLOW_TASK_IDS = {
    "dense_builder_and_career_workflows": ["reach_real_workbench", "recover_section_rhythm"],
    "dice_initiative_and_table_utilities": ["locate_save_import_settings"],
    "identity_contacts_lifestyles_history": ["recover_section_rhythm"],
}
EXPECTED_FLAGSHIP_READINESS_FINGERPRINT_BASIS = [
    "status",
    "summary",
    "reasons",
    "evidence",
]
FORBIDDEN_PROOF_MARKERS = [
    "TASK_LOCAL_TELEMETRY",
    "ACTIVE_RUN_HANDOFF",
    "/var/lib/codex-fleet",
    "supervisor status",
    "supervisor eta",
    "operator telemetry",
]


def _yaml(path: Path) -> dict[str, Any]:
    return load_yaml_dict(path)


def _expected_frontier_id() -> int:
    queue = _yaml(QUEUE_STAGING_PATH)
    for item in queue.get("items") or []:
        row = dict(item)
        if str(row.get("package_id") or "") == PACKAGE_ID:
            return int(row.get("frontier_id") or 0)
    return 0


def _check_forbidden_markers(label: str, text: str, issues: list[str]) -> None:
    lowered = text.lower()
    for forbidden in FORBIDDEN_PROOF_MARKERS:
        if forbidden.lower() in lowered:
            issues.append(f"{label} cites forbidden helper evidence: {forbidden}")


def main() -> int:
    issues: list[str] = []
    family_markdown_status: dict[str, str] = {}
    for path in (PACK_PATH, MARKDOWN_PATH, FEEDBACK_PATH):
        if not path.is_file():
            issues.append(f"missing required file: {path}")
    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1

    payload = _yaml(PACK_PATH)
    expected_frontier_id = _expected_frontier_id()
    if expected_frontier_id <= 0:
        issues.append("canonical fleet queue row missing for M142 package")
    elif int(payload.get("frontier_id") or 0) != expected_frontier_id:
        issues.append("frontier_id drifted from canonical queue row")

    if payload.get("package_id") != PACKAGE_ID:
        issues.append("package_id drifted")
    if list(payload.get("allowed_paths") or []) != ["scripts", "feedback", "docs"]:
        issues.append("allowed_paths drifted")
    if list(payload.get("owned_surfaces") or []) != ["compile_family_local_screenshot_and_interaction_packs_fo:ea"]:
        issues.append("owned_surfaces drifted")

    source_inputs = dict(payload.get("source_inputs") or {})
    design_queue = dict(source_inputs.get("design_queue") or {})
    if int(design_queue.get("match_count") or 0) != 1:
        issues.append("design_queue match_count drifted")
    if design_queue.get("unique_match") is not True:
        issues.append("design_queue should have exactly one canonical row")
    if design_queue.get("status") != "not_started":
        issues.append("design_queue status drifted")
    if int(design_queue.get("frontier_id") or 0) != expected_frontier_id:
        issues.append("design_queue frontier drifted")
    if not str(design_queue.get("row_fingerprint") or "").strip():
        issues.append("design_queue row_fingerprint missing")
    fleet_queue = dict(source_inputs.get("fleet_queue") or {})
    if int(fleet_queue.get("match_count") or 0) != 1:
        issues.append("fleet_queue match_count drifted")
    if fleet_queue.get("unique_match") is not True:
        issues.append("fleet_queue should have exactly one canonical row")
    if fleet_queue.get("status") != "not_started":
        issues.append("fleet_queue status drifted")
    if int(fleet_queue.get("frontier_id") or 0) != expected_frontier_id:
        issues.append("fleet_queue frontier drifted")
    if not str(fleet_queue.get("row_fingerprint") or "").strip():
        issues.append("fleet_queue row_fingerprint missing")
    local_mirror_queue = dict(source_inputs.get("local_mirror_queue") or {})
    if local_mirror_queue.get("path") != "/docker/EA/.codex-design/product/NEXT_90_DAY_QUEUE_STAGING.generated.yaml":
        issues.append("local_mirror_queue source path drifted")
    if int(local_mirror_queue.get("match_count") or 0) != 1:
        issues.append("local_mirror_queue match_count drifted")
    if local_mirror_queue.get("unique_match") is not True:
        issues.append("local_mirror_queue should have exactly one mirrored row")
    if local_mirror_queue.get("status") != "not_started":
        issues.append("local_mirror_queue status drifted")
    if int(local_mirror_queue.get("frontier_id") or 0) != expected_frontier_id:
        issues.append("local_mirror_queue frontier drifted")
    if not str(local_mirror_queue.get("row_fingerprint") or "").strip():
        issues.append("local_mirror_queue row_fingerprint missing")
    registry_input = dict(source_inputs.get("registry") or {})
    if int(registry_input.get("match_count") or 0) != 1:
        issues.append("registry match_count drifted")
    if registry_input.get("unique_match") is not True:
        issues.append("registry should have exactly one canonical work-task row")
    if registry_input.get("owner") != "executive-assistant":
        issues.append("registry owner drifted")
    if not str(registry_input.get("row_fingerprint") or "").strip():
        issues.append("registry row_fingerprint missing")
    local_mirror_registry = dict(source_inputs.get("local_mirror_registry") or {})
    if local_mirror_registry.get("path") != "/docker/EA/.codex-design/product/NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml":
        issues.append("local_mirror_registry source path drifted")
    if int(local_mirror_registry.get("match_count") or 0) != 1:
        issues.append("local_mirror_registry match_count drifted")
    if local_mirror_registry.get("unique_match") is not True:
        issues.append("local_mirror_registry should have exactly one mirrored task row")
    if local_mirror_registry.get("owner") != "executive-assistant":
        issues.append("local_mirror_registry owner drifted")
    if not str(local_mirror_registry.get("row_fingerprint") or "").strip():
        issues.append("local_mirror_registry row_fingerprint missing")
    readiness_input = dict(source_inputs.get("flagship_readiness") or {})
    if readiness_input.get("path") != "/docker/fleet/.codex-studio/published/FLAGSHIP_PRODUCT_READINESS.generated.json":
        issues.append("flagship_readiness source path drifted")
    if readiness_input.get("coverage_key") != "desktop_client":
        issues.append("flagship_readiness coverage key drifted")
    if readiness_input.get("status") != payload.get("desktop_client_readiness", {}).get("status"):
        issues.append("flagship_readiness status drifted")
    if int(readiness_input.get("reason_count") or 0) != len(list(payload.get("desktop_client_readiness", {}).get("reasons") or [])):
        issues.append("flagship_readiness reason_count drifted")
    if list(readiness_input.get("row_fingerprint_basis") or []) != EXPECTED_FLAGSHIP_READINESS_FINGERPRINT_BASIS:
        issues.append("flagship_readiness row_fingerprint_basis drifted")
    if not str(readiness_input.get("row_fingerprint") or "").strip():
        issues.append("flagship_readiness row_fingerprint missing")

    desktop_readiness = dict(payload.get("desktop_client_readiness") or {})
    if desktop_readiness.get("coverage_key") != "desktop_client":
        issues.append("desktop_client_readiness coverage key drifted")
    if not str(desktop_readiness.get("status") or "").strip():
        issues.append("desktop_client_readiness status missing")
    if not str(desktop_readiness.get("summary") or "").strip():
        issues.append("desktop_client_readiness summary missing")
    if int(desktop_readiness.get("reason_count") or 0) != len(list(desktop_readiness.get("reasons") or [])):
        issues.append("desktop_client_readiness reason_count drifted")
    queue_alignment = dict(dict(payload.get("canonical_monitors") or {}).get("queue_alignment") or {})
    if queue_alignment.get("design_queue_unique") is not True:
        issues.append("queue_alignment design_queue_unique drifted")
    if queue_alignment.get("fleet_queue_unique") is not True:
        issues.append("queue_alignment fleet_queue_unique drifted")
    if queue_alignment.get("local_mirror_queue_unique") is not True:
        issues.append("queue_alignment local_mirror_queue_unique drifted")
    if queue_alignment.get("registry_task_unique") is not True:
        issues.append("queue_alignment registry_task_unique drifted")
    if queue_alignment.get("local_mirror_registry_task_unique") is not True:
        issues.append("queue_alignment local_mirror_registry_task_unique drifted")
    if queue_alignment.get("design_fleet_queue_fingerprint_matches") is not True:
        issues.append("queue_alignment design_fleet_queue_fingerprint_matches drifted")
    if queue_alignment.get("design_local_mirror_queue_fingerprint_matches") is not True:
        issues.append("queue_alignment design_local_mirror_queue_fingerprint_matches drifted")
    if queue_alignment.get("registry_task_owner_matches") is not True:
        issues.append("queue_alignment registry_task_owner_matches drifted")
    if queue_alignment.get("registry_task_title_matches") is not True:
        issues.append("queue_alignment registry_task_title_matches drifted")
    if queue_alignment.get("local_mirror_queue_owner_matches") is not True:
        issues.append("queue_alignment local_mirror_queue_owner_matches drifted")
    if queue_alignment.get("local_mirror_queue_frontier_matches") is not True:
        issues.append("queue_alignment local_mirror_queue_frontier_matches drifted")
    if queue_alignment.get("local_mirror_queue_allowed_paths_match") is not True:
        issues.append("queue_alignment local_mirror_queue_allowed_paths_match drifted")
    if queue_alignment.get("local_mirror_queue_owned_surfaces_match") is not True:
        issues.append("queue_alignment local_mirror_queue_owned_surfaces_match drifted")
    if queue_alignment.get("local_mirror_registry_task_owner_matches") is not True:
        issues.append("queue_alignment local_mirror_registry_task_owner_matches drifted")
    if queue_alignment.get("local_mirror_registry_task_title_matches") is not True:
        issues.append("queue_alignment local_mirror_registry_task_title_matches drifted")
    if queue_alignment.get("registry_local_mirror_task_fingerprint_matches") is not True:
        issues.append("queue_alignment registry_local_mirror_task_fingerprint_matches drifted")

    rows = [dict(row) for row in (payload.get("family_local_packs") or [])]
    family_ids = {str(row.get("family_id") or "") for row in rows}
    if family_ids != EXPECTED_FAMILIES:
        issues.append(f"family ids drifted: {sorted(family_ids)}")
    direct_gap_families: list[str] = []
    for row in rows:
        if not list(row.get("evidence_paths") or []):
            issues.append(f"{row.get('family_id')}: evidence_paths missing")
        if not list(row.get("required_screenshots") or []):
            issues.append(f"{row.get('family_id')}: required_screenshots missing")
        if list(row.get("workflow_task_ids") or []) != EXPECTED_WORKFLOW_TASK_IDS.get(str(row.get("family_id") or "")):
            issues.append(f"{row.get('family_id')}: workflow_task_ids drifted")
        screenshot_receipts = [dict(receipt) for receipt in (row.get("screenshot_receipts") or [])]
        interaction_receipts = [dict(receipt) for receipt in (row.get("interaction_receipts") or [])]
        if not screenshot_receipts:
            issues.append(f"{row.get('family_id')}: screenshot_receipts missing")
        if not interaction_receipts:
            issues.append(f"{row.get('family_id')}: interaction_receipts missing")
        dependency = dict(row.get("desktop_client_dependency") or {})
        if dependency.get("coverage_key") != "desktop_client":
            issues.append(f"{row.get('family_id')}: desktop_client_dependency coverage key drifted")
        if dependency.get("coverage_status") != desktop_readiness.get("status"):
            issues.append(f"{row.get('family_id')}: desktop_client_dependency status drifted")
        parity = dict(row.get("parity_audit") or {})
        visual_parity = str(parity.get("visual_parity") or "").strip().lower()
        behavioral_parity = str(parity.get("behavioral_parity") or "").strip().lower()
        if visual_parity not in {"yes", "no"}:
            issues.append(f"{row.get('family_id')}: parity_audit visual_parity missing or invalid")
        if behavioral_parity not in {"yes", "no"}:
            issues.append(f"{row.get('family_id')}: parity_audit behavioral_parity missing or invalid")
        if visual_parity != "yes" or behavioral_parity != "yes":
            direct_gap_families.append(str(row.get("family_id") or ""))
        row_has_missing_receipt = False
        for receipt in screenshot_receipts + interaction_receipts:
            if not receipt.get("required_tokens"):
                issues.append(f"{row.get('family_id')}::{receipt.get('route_id')}: required_tokens missing")
            if receipt.get("satisfied") is not True:
                row_has_missing_receipt = True
        family_markdown_status[str(row.get("family_id") or "")] = "fail" if row_has_missing_receipt or list(row.get("issues") or []) else "pass"

    closeout = dict(payload.get("closeout") or {})
    blockers = [str(item) for item in (closeout.get("blockers") or [])]
    if not blockers:
        issues.append("closeout blockers should stay explicit until canonical queue rows are complete")
    if desktop_readiness.get("status") != "ready" and not any("desktop_client" in blocker for blocker in blockers):
        issues.append("closeout blockers must mention the live desktop_client gap while readiness is not ready")
    for family_id in direct_gap_families:
        if not any(family_id in blocker for blocker in blockers):
            issues.append(f"closeout blockers must mention the live direct-parity gap for {family_id}")

    markdown_text = MARKDOWN_PATH.read_text(encoding="utf-8")
    if "## Family summary" not in markdown_text:
        issues.append("markdown summary missing family summary section")
    if "## Queue guardrails" not in markdown_text:
        issues.append("markdown summary missing queue guardrails section")
    if "approved `.codex-design` local mirror" not in markdown_text:
        issues.append("markdown summary missing local mirror guardrail")
    for family_id, markers in EXPECTED_MARKDOWN_RECEIPTS.items():
        expected_status = family_markdown_status.get(family_id, "pass")
        if f"- `{family_id}`: {expected_status}" not in markdown_text:
            issues.append(f"markdown summary missing explicit family status line for {family_id}")
        for marker in markers:
            if marker not in markdown_text:
                issues.append(f"markdown summary collapsed or drifted for {family_id}: missing `{marker}`")

    feedback_text = FEEDBACK_PATH.read_text(encoding="utf-8")
    if "desktop_client" not in feedback_text:
        issues.append("feedback note must mention desktop_client readiness posture")
    expected_desktop_feedback = f"desktop_client = {desktop_readiness.get('status') or 'unknown'}"
    if expected_desktop_feedback not in feedback_text:
        issues.append("feedback note must pin the current desktop_client posture")
    if "canonical queue frontier" not in feedback_text:
        issues.append("feedback note must mention canonical queue frontier alignment")
    if ".codex-design local mirror" not in feedback_text:
        issues.append("feedback note must mention local mirror alignment")
    if "duplicate queue or registry rows fail closed" not in feedback_text:
        issues.append("feedback note must mention duplicate canonical row fail-closed posture")
    if "screenshot receipts" not in feedback_text or "interaction receipts" not in feedback_text:
        issues.append("feedback note must distinguish screenshot receipts from interaction receipts")
    for family_id in EXPECTED_FAMILIES:
        if family_id not in feedback_text:
            issues.append(f"feedback note must mention {family_id}")
    _check_forbidden_markers("generated packet", repr(payload), issues)
    _check_forbidden_markers("markdown summary", markdown_text, issues)
    _check_forbidden_markers("feedback note", feedback_text, issues)

    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1

    print("ok: next90 m142 ea family-local screenshot and interaction packs")
    print(f"ok: {PACK_PATH}")
    print(f"ok: {MARKDOWN_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
