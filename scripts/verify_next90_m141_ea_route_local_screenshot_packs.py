#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "ea") not in sys.path:
    sys.path.insert(0, str(ROOT / "ea"))

from app.yaml_inputs import load_yaml_dict

DOCS_ROOT = ROOT / "docs" / "chummer5a_parity_lab"
PACK_PATH = DOCS_ROOT / "NEXT90_M141_ROUTE_LOCAL_SCREENSHOT_PACKS.generated.yaml"
MARKDOWN_PATH = DOCS_ROOT / "NEXT90_M141_ROUTE_LOCAL_SCREENSHOT_PACKS.generated.md"
MATERIALIZER_PATH = ROOT / "scripts" / "materialize_next90_m141_ea_route_local_screenshot_packs.py"
FEEDBACK_PATH = ROOT / "feedback" / "2026-05-06-next90-m141-ea-route-local-screenshot-packs.md"

PACKAGE_ID = "next90-m141-ea-compile-route-local-screenshot-packs-and-compare-packets-for-translator-x"
EXPECTED_ROUTE_IDS = {
    "menu:translator",
    "menu:xml_editor",
    "menu:hero_lab_importer",
    "workflow:import_oracle",
}
EXPECTED_FAMILY_IDS = {
    "custom_data_xml_and_translator_bridge",
    "legacy_and_adjacent_import_oracles",
}
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


def _find_forbidden_markers(label: str, text: str, issues: list[str]) -> None:
    lowered = text.lower()
    for forbidden in FORBIDDEN_PROOF_MARKERS:
        if forbidden.lower() in lowered:
            issues.append(f"{label} cites forbidden helper evidence: {forbidden}")


def _module():
    spec = importlib.util.spec_from_file_location("ea_next90_m141_materializer", MATERIALIZER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load materializer from {MATERIALIZER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    issues: list[str] = []
    module = _module()
    for path in (PACK_PATH, MARKDOWN_PATH, FEEDBACK_PATH):
        if not path.is_file():
            issues.append(f"missing required file: {path}")
    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1

    payload = _yaml(PACK_PATH)
    materialized = module.build_payload()
    materialized["generated_at"] = str(payload.get("generated_at") or "")
    if module.without_generated_at(payload) != module.without_generated_at(materialized):
        issues.append("generated route-local screenshot packet drifted from materializer output")

    if payload.get("package_id") != PACKAGE_ID:
        issues.append("package_id drifted")
    if list(payload.get("allowed_paths") or []) != ["scripts", "feedback", "docs"]:
        issues.append("allowed_paths drifted")
    if list(payload.get("owned_surfaces") or []) != ["compile_route_local_screenshot_packs_and_compare_packets:ea"]:
        issues.append("owned_surfaces drifted")
    source_inputs = dict(payload.get("source_inputs") or {})
    design_queue = dict(source_inputs.get("design_queue") or {})
    if design_queue.get("path") != "/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_QUEUE_STAGING.generated.yaml":
        issues.append("design_queue source path drifted")
    if int(design_queue.get("match_count") or 0) != 1:
        issues.append("design_queue match_count drifted")
    if design_queue.get("unique_match") is not True:
        issues.append("design_queue should have exactly one canonical row")
    if design_queue.get("status") != "not_started":
        issues.append("design_queue status drifted")
    if design_queue.get("work_task_id") != "141.4":
        issues.append("design_queue work_task_id drifted")
    if int(design_queue.get("milestone_id") or 0) != 141:
        issues.append("design_queue milestone_id drifted")
    if int(design_queue.get("frontier_id") or 0) != int(payload.get("frontier_id") or 0):
        issues.append("design_queue frontier drifted")
    if design_queue.get("wave") != "W22P":
        issues.append("design_queue wave drifted")
    if design_queue.get("repo") != "executive-assistant":
        issues.append("design_queue repo drifted")
    if not str(design_queue.get("row_fingerprint") or "").strip():
        issues.append("design_queue row_fingerprint missing")
    fleet_queue = dict(source_inputs.get("fleet_queue") or {})
    if fleet_queue.get("path") != "/docker/fleet/.codex-studio/published/NEXT_90_DAY_QUEUE_STAGING.generated.yaml":
        issues.append("fleet_queue source path drifted")
    if int(fleet_queue.get("match_count") or 0) != 1:
        issues.append("fleet_queue match_count drifted")
    if fleet_queue.get("unique_match") is not True:
        issues.append("fleet_queue should have exactly one canonical row")
    if fleet_queue.get("status") != "not_started":
        issues.append("fleet_queue status drifted")
    if fleet_queue.get("work_task_id") != "141.4":
        issues.append("fleet_queue work_task_id drifted")
    if int(fleet_queue.get("milestone_id") or 0) != 141:
        issues.append("fleet_queue milestone_id drifted")
    if int(fleet_queue.get("frontier_id") or 0) != int(payload.get("frontier_id") or 0):
        issues.append("fleet_queue frontier drifted")
    if fleet_queue.get("wave") != "W22P":
        issues.append("fleet_queue wave drifted")
    if fleet_queue.get("repo") != "executive-assistant":
        issues.append("fleet_queue repo drifted")
    if not str(fleet_queue.get("row_fingerprint") or "").strip():
        issues.append("fleet_queue row_fingerprint missing")
    local_mirror_queue = dict(source_inputs.get("local_mirror_queue") or {})
    if local_mirror_queue.get("path") != "/docker/EA/.codex-design/product/NEXT_90_DAY_QUEUE_STAGING.generated.yaml":
        issues.append("local_mirror_queue source path drifted")
    if int(local_mirror_queue.get("match_count") or 0) != 1:
        issues.append("local_mirror_queue match_count drifted")
    if local_mirror_queue.get("unique_match") is not True:
        issues.append("local_mirror_queue should have exactly one canonical row")
    if local_mirror_queue.get("status") != "not_started":
        issues.append("local_mirror_queue status drifted")
    if local_mirror_queue.get("work_task_id") != "141.4":
        issues.append("local_mirror_queue work_task_id drifted")
    if int(local_mirror_queue.get("milestone_id") or 0) != 141:
        issues.append("local_mirror_queue milestone_id drifted")
    if int(local_mirror_queue.get("frontier_id") or 0) != int(payload.get("frontier_id") or 0):
        issues.append("local_mirror_queue frontier drifted")
    if local_mirror_queue.get("wave") != "W22P":
        issues.append("local_mirror_queue wave drifted")
    if local_mirror_queue.get("repo") != "executive-assistant":
        issues.append("local_mirror_queue repo drifted")
    if not str(local_mirror_queue.get("row_fingerprint") or "").strip():
        issues.append("local_mirror_queue row_fingerprint missing")
    registry_input = dict(source_inputs.get("registry") or {})
    if registry_input.get("path") != "/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml":
        issues.append("registry source path drifted")
    if int(registry_input.get("match_count") or 0) != 1:
        issues.append("registry match_count drifted")
    if registry_input.get("unique_match") is not True:
        issues.append("registry should have exactly one canonical work-task row")
    if registry_input.get("work_task_id") != "141.4":
        issues.append("registry work_task_id drifted")
    if int(registry_input.get("milestone_id") or 0) != 141:
        issues.append("registry milestone_id drifted")
    if registry_input.get("owner") != "executive-assistant":
        issues.append("registry owner drifted")
    if registry_input.get("title") != payload.get("title"):
        issues.append("registry title drifted")
    if not str(registry_input.get("row_fingerprint") or "").strip():
        issues.append("registry row_fingerprint missing")
    local_mirror_registry = dict(source_inputs.get("local_mirror_registry") or {})
    if local_mirror_registry.get("path") != "/docker/EA/.codex-design/product/NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml":
        issues.append("local_mirror_registry source path drifted")
    if int(local_mirror_registry.get("match_count") or 0) != 1:
        issues.append("local_mirror_registry match_count drifted")
    if local_mirror_registry.get("unique_match") is not True:
        issues.append("local_mirror_registry should resolve to exactly one mirrored work-task row")
    if local_mirror_registry.get("work_task_id") != "141.4":
        issues.append("local_mirror_registry work_task_id drifted")
    if int(local_mirror_registry.get("milestone_id") or 0) != 141:
        issues.append("local_mirror_registry milestone_id drifted")
    if local_mirror_registry.get("status") != "":
        issues.append("local_mirror_registry status drifted")
    if local_mirror_registry.get("owner") != "executive-assistant":
        issues.append("local_mirror_registry owner drifted")
    if local_mirror_registry.get("title") != payload.get("title"):
        issues.append("local_mirror_registry title drifted")
    if not str(local_mirror_registry.get("row_fingerprint") or "").strip():
        issues.append("local_mirror_registry row_fingerprint missing")
    readiness_input = dict(source_inputs.get("flagship_readiness") or {})
    if readiness_input.get("path") != "/docker/fleet/.codex-studio/published/FLAGSHIP_PRODUCT_READINESS.generated.json":
        issues.append("flagship_readiness source path drifted")
    if readiness_input.get("coverage_key") != "desktop_client":
        issues.append("flagship_readiness coverage key drifted")
    if readiness_input.get("status") != payload.get("desktop_client_readiness", {}).get("status"):
        issues.append("flagship_readiness status drifted")
    if readiness_input.get("summary") != payload.get("desktop_client_readiness", {}).get("summary"):
        issues.append("flagship_readiness summary drifted")
    if int(readiness_input.get("reason_count") or 0) != len(list(payload.get("desktop_client_readiness", {}).get("reasons") or [])):
        issues.append("flagship_readiness reason_count drifted")
    expected_readiness_fingerprint = module._stable_fingerprint(
        module._desktop_readiness_fingerprint_payload(
            dict(payload.get("desktop_client_readiness") or {}),
            desktop_status=str(dict(payload.get("desktop_client_readiness") or {}).get("status") or ""),
            desktop_summary=str(dict(payload.get("desktop_client_readiness") or {}).get("summary") or ""),
            desktop_reasons=[str(item) for item in (dict(payload.get("desktop_client_readiness") or {}).get("reasons") or [])],
        )
    )
    if readiness_input.get("row_fingerprint") != expected_readiness_fingerprint:
        issues.append("flagship_readiness row_fingerprint drifted")
    if not str(readiness_input.get("row_fingerprint") or "").strip():
        issues.append("flagship_readiness row_fingerprint missing")
    if int(payload.get("frontier_id") or 0) <= 0:
        issues.append("frontier_id must stay bound to the canonical queue row")
    guide_markers = dict(dict(payload.get("canonical_monitors") or {}).get("guide_markers") or {})
    queue_alignment = dict(dict(payload.get("canonical_monitors") or {}).get("queue_alignment") or {})
    mirror_alignment = dict(dict(payload.get("canonical_monitors") or {}).get("mirror_alignment") or {})
    fleet_gate_monitor = dict(dict(payload.get("canonical_monitors") or {}).get("fleet_m141_gate") or {})
    for marker in ("wave", "milestone", "exit"):
        if guide_markers.get(marker) is not True:
            issues.append(f"guide marker missing: {marker}")
    if queue_alignment.get("registry_task_present") is not True:
        issues.append("registry task should be present")
    if queue_alignment.get("design_queue_unique") is not True:
        issues.append("design queue row uniqueness drifted")
    if queue_alignment.get("fleet_queue_unique") is not True:
        issues.append("fleet queue row uniqueness drifted")
    if queue_alignment.get("registry_task_unique") is not True:
        issues.append("registry task uniqueness drifted")
    if queue_alignment.get("design_fleet_queue_fingerprint_matches") is not True:
        issues.append("design/fleet queue row fingerprints drifted")
    if queue_alignment.get("work_task_id_matches") is not True:
        issues.append("work_task_id alignment drifted")
    if queue_alignment.get("milestone_id_matches") is not True:
        issues.append("milestone_id alignment drifted")
    if queue_alignment.get("wave_matches") is not True:
        issues.append("wave alignment drifted")
    if queue_alignment.get("repo_matches") is not True:
        issues.append("repo alignment drifted")
    if queue_alignment.get("frontier_id_matches") is not True:
        issues.append("frontier_id alignment drifted")
    if queue_alignment.get("registry_task_owner_matches") is not True:
        issues.append("registry task owner drifted")
    if queue_alignment.get("registry_task_title_matches") is not True:
        issues.append("registry task title drifted")
    if mirror_alignment.get("local_mirror_queue_present") is not True:
        issues.append("local_mirror_queue should be present")
    if mirror_alignment.get("local_mirror_queue_unique") is not True:
        issues.append("local_mirror_queue uniqueness drifted")
    if mirror_alignment.get("local_mirror_queue_matches_design_queue") is not True:
        issues.append("local_mirror_queue should match the design queue row")
    if mirror_alignment.get("local_mirror_queue_matches_fleet_queue") is not True:
        issues.append("local_mirror_queue should match the Fleet queue row")
    if mirror_alignment.get("local_mirror_registry_present") is not True:
        issues.append("local_mirror_registry should now be present in the approved local mirror")
    if mirror_alignment.get("local_mirror_registry_unique") is not True:
        issues.append("local_mirror_registry uniqueness drifted")
    if mirror_alignment.get("local_mirror_registry_matches_canonical_registry") is not True:
        issues.append("local_mirror_registry should match the canonical registry row")
    if mirror_alignment.get("local_mirror_registry_owner_matches") is not True:
        issues.append("local_mirror_registry owner match drifted")
    if mirror_alignment.get("local_mirror_registry_title_matches") is not True:
        issues.append("local_mirror_registry title match drifted")
    if fleet_gate_monitor.get("status") != "pass":
        issues.append("fleet_m141_gate status drifted")
    if fleet_gate_monitor.get("ready") is not True:
        issues.append("fleet_m141_gate ready drifted")
    if int(fleet_gate_monitor.get("runtime_blocker_count") or 0) != 0:
        issues.append("fleet_m141_gate runtime_blocker_count drifted")

    route_rows = [dict(row) for row in (payload.get("route_local_screenshot_packs") or [])]
    family_rows = [dict(row) for row in (payload.get("family_compare_packets") or [])]
    route_specs = {str(key): dict(value) for key, value in dict(module.ROUTE_SPECS).items()}
    family_specs = {str(key): dict(value) for key, value in dict(module.FAMILY_SPECS).items()}
    route_ids = {str(row.get("route_id") or "") for row in route_rows}
    family_ids = {str(row.get("family_id") or "") for row in family_rows}
    if route_ids != EXPECTED_ROUTE_IDS:
        issues.append(f"route ids drifted: {sorted(route_ids)}")
    if family_ids != EXPECTED_FAMILY_IDS:
        issues.append(f"family ids drifted: {sorted(family_ids)}")

    for row in route_rows:
        route_id = str(row.get("route_id") or "")
        spec = route_specs.get(route_id)
        if spec is None:
            issues.append(f"{route_id}: unexpected route row")
            continue
        if not row.get("screenshots"):
            issues.append(f"{route_id}: screenshots missing")
        if not row.get("route_receipts"):
            issues.append(f"{route_id}: route_receipts missing")
        if row.get("status") != "pass":
            issues.append(f"{route_id}: status is not pass")
        if list(row.get("required_compare_artifacts") or []) != list(spec["required_compare_artifacts"]):
            issues.append(f"{route_id}: required_compare_artifacts drifted")
        if list(row.get("required_screenshots") or []) != list(spec["required_screenshots"]):
            issues.append(f"{route_id}: required_screenshots drifted from route-local proof pack")
        if list(row.get("missing_compare_artifacts") or []):
            issues.append(f"{route_id}: missing_compare_artifacts must stay empty")
        if list(row.get("missing_workflow_artifacts") or []):
            issues.append(f"{route_id}: missing_workflow_artifacts must stay empty")
        if list(row.get("missing_screenshots") or []):
            issues.append(f"{route_id}: missing_screenshots must stay empty")
        if list(row.get("deterministic_receipts") or []) != list(spec["deterministic_receipts"]):
            issues.append(f"{route_id}: deterministic_receipts drifted")
        if str(row.get("ui_direct_receipt_group") or "") != str(spec["ui_direct_group"]):
            issues.append(f"{route_id}: ui_direct_receipt_group drifted")
        for receipt in row.get("route_receipts") or []:
            if not dict(receipt).get("required_tokens"):
                issues.append(f"{route_id}: receipt missing required_tokens")
        parity_row = dict(row.get("parity_row") or {})
        if row.get("parity_row_id"):
            for field in ("present_in_chummer5a", "present_in_chummer6", "visual_parity", "behavioral_parity", "removable_if_not_in_chummer5a", "reason"):
                if not str(parity_row.get(field) or "").strip():
                    issues.append(f"{route_id}: parity row field missing: {field}")
            for field in ("present_in_chummer5a", "present_in_chummer6", "visual_parity", "behavioral_parity"):
                if parity_row.get(field) != "yes":
                    issues.append(f"{route_id}: parity row {field} must stay yes")

    for row in family_rows:
        family_id = str(row.get("family_id") or "")
        spec = family_specs.get(family_id)
        if spec is None:
            issues.append(f"{family_id}: unexpected family row")
            continue
        if not row.get("screenshots"):
            issues.append(f"{family_id}: screenshots missing")
        if row.get("status") != "pass":
            issues.append(f"{family_id}: status is not pass")
        if list(row.get("required_compare_artifacts") or []) != list(spec["required_compare_artifacts"]):
            issues.append(f"{family_id}: required_compare_artifacts drifted")
        if list(row.get("required_screenshots") or []) != list(spec["required_screenshots"]):
            issues.append(f"{family_id}: required_screenshots drifted from family-local proof pack")
        if list(row.get("missing_compare_artifacts") or []):
            issues.append(f"{family_id}: missing_compare_artifacts must stay empty")
        if list(row.get("missing_workflow_artifacts") or []):
            issues.append(f"{family_id}: missing_workflow_artifacts must stay empty")
        if list(row.get("missing_screenshots") or []):
            issues.append(f"{family_id}: missing_screenshots must stay empty")
        if list(row.get("deterministic_receipts") or []) != list(spec["deterministic_receipts"]):
            issues.append(f"{family_id}: deterministic_receipts drifted")
        if str(row.get("ui_direct_receipt_group") or "") != str(spec["ui_direct_group"]):
            issues.append(f"{family_id}: ui_direct_receipt_group drifted")
        parity_row = dict(row.get("parity_row") or {})
        for field in ("present_in_chummer5a", "present_in_chummer6", "visual_parity", "behavioral_parity", "removable_if_not_in_chummer5a", "reason"):
            if not str(parity_row.get(field) or "").strip():
                issues.append(f"{family_id}: parity row field missing: {field}")
        for field in ("present_in_chummer5a", "present_in_chummer6", "visual_parity", "behavioral_parity"):
            if parity_row.get(field) != "yes":
                issues.append(f"{family_id}: parity row {field} must stay yes")

    closeout = dict(payload.get("closeout") or {})
    blockers = [str(item) for item in (closeout.get("blockers") or [])]
    if closeout.get("ready") is not False:
        issues.append("closeout should remain not-ready while canonical queue rows are still open")
    uniqueness_is_healthy = (
        int(design_queue.get("match_count") or 0) == 1
        and design_queue.get("unique_match") is True
        and int(fleet_queue.get("match_count") or 0) == 1
        and fleet_queue.get("unique_match") is True
        and int(registry_input.get("match_count") or 0) == 1
        and registry_input.get("unique_match") is True
    )
    if not uniqueness_is_healthy and not any("canonical queue/registry row uniqueness drifted" in blocker for blocker in blockers):
        issues.append("closeout blockers must mention canonical row uniqueness posture")
    mirror_is_healthy = (
        int(local_mirror_queue.get("match_count") or 0) == 1
        and local_mirror_queue.get("unique_match") is True
        and int(local_mirror_registry.get("match_count") or 0) == 1
        and local_mirror_registry.get("unique_match") is True
        and mirror_alignment.get("local_mirror_queue_matches_design_queue") is True
        and mirror_alignment.get("local_mirror_queue_matches_fleet_queue") is True
        and mirror_alignment.get("local_mirror_registry_matches_canonical_registry") is True
        and mirror_alignment.get("local_mirror_registry_owner_matches") is True
        and mirror_alignment.get("local_mirror_registry_title_matches") is True
    )
    if not mirror_is_healthy and not any("approved local mirror row uniqueness drifted" in blocker for blocker in blockers):
        issues.append("closeout blockers must mention approved local mirror row uniqueness posture")
    if not mirror_is_healthy and not any("approved local mirror drifted from canonical M141 package rows" in blocker for blocker in blockers):
        issues.append("closeout blockers must mention approved local mirror drift posture")
    if not any("canonical queue/registry rows are still open" in blocker for blocker in blockers):
        issues.append("closeout blockers must mention the open canonical queue/registry rows")
    if not any("registry_task=unspecified" in blocker for blocker in blockers):
        issues.append("closeout blockers must distinguish an unspecified registry task status from a missing row")
    if any("canonical queue/registry metadata drifted" in blocker for blocker in blockers):
        issues.append("closeout blockers should not report metadata drift when the canonical rows align")
    if mirror_is_healthy and any("approved local mirror" in blocker for blocker in blockers):
        issues.append("closeout blockers should not report approved local mirror drift while the mirror is aligned")
    if any("Fleet M141 closeout gate" in blocker for blocker in blockers):
        issues.append("closeout blockers should not mention Fleet M141 gate regressions while the gate is green")

    markdown_text = MARKDOWN_PATH.read_text(encoding="utf-8")
    feedback_text = FEEDBACK_PATH.read_text(encoding="utf-8")
    payload_text = repr(payload)
    if "duplicate queue or registry rows fail closed" not in markdown_text:
        issues.append("markdown summary must mention duplicate canonical row fail-closed posture")
    if "approved local mirror" not in markdown_text:
        issues.append("markdown summary must mention approved local mirror posture")
    if f"canonical frontier: `{payload.get('frontier_id')}`" not in markdown_text:
        issues.append("markdown summary must pin the canonical frontier id")
    if "stale handoff or assignment frontier snippets are not proof" not in markdown_text:
        issues.append("markdown summary must reject stale handoff frontier snippets as proof")
    if "route-local screenshot packs" not in feedback_text:
        issues.append("feedback note must mention route-local screenshot packs")
    if "does not mark the canonical queue rows complete" not in feedback_text:
        issues.append("feedback note must keep the closeout boundary explicit")
    if "duplicate queue or registry rows fail closed" not in feedback_text:
        issues.append("feedback note must mention duplicate canonical row fail-closed posture")
    if "approved local mirror" not in feedback_text:
        issues.append("feedback note must mention approved local mirror posture")
    if f"current canonical frontier is `{payload.get('frontier_id')}`" not in feedback_text:
        issues.append("feedback note must pin the current canonical frontier id")
    if "stale handoff or assignment frontier snippets are not authority" not in feedback_text:
        issues.append("feedback note must reject stale handoff frontier snippets as authority")
    if "Fleet M141 closeout gate" not in feedback_text:
        issues.append("feedback note must mention the downstream Fleet M141 closeout gate posture")
    _find_forbidden_markers("generated packet", payload_text, issues)
    _find_forbidden_markers("markdown summary", markdown_text, issues)
    _find_forbidden_markers("feedback note", feedback_text, issues)

    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1

    print("ok: next90 m141 ea route-local screenshot packs")
    print(f"ok: {PACK_PATH}")
    print(f"ok: {MARKDOWN_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
