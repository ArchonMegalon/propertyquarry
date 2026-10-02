#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "ea") not in sys.path:
    sys.path.insert(0, str(ROOT / "ea"))

from app.yaml_inputs import load_yaml_dict

DOCS_ROOT = ROOT / "docs" / "chummer5a_parity_lab"
EA_ROOT = Path("/docker/EA") if Path("/docker/EA").exists() else ROOT

OUTPUT_PATH = DOCS_ROOT / "NEXT90_M143_ROUTE_SPECIFIC_COMPARE_PACKS.generated.yaml"
MARKDOWN_PATH = DOCS_ROOT / "NEXT90_M143_ROUTE_SPECIFIC_COMPARE_PACKS.generated.md"
FEEDBACK_PATH = ROOT / "feedback" / "2026-05-05-next90-m143-ea-route-specific-compare-packs.md"
COMPARE_PACKS_PATH = EA_ROOT / "docs" / "chummer5a_parity_lab" / "compare_packs.yaml"
VETERAN_WORKFLOW_PACK_PATH = Path("/docker/fleet/docs/chummer5a-oracle/veteran_workflow_packs.yaml")
SUCCESSOR_REGISTRY_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml")
DESIGN_QUEUE_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
FLEET_QUEUE_PATH = Path("/docker/fleet/.codex-studio/published/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
LOCAL_MIRROR_QUEUE_PATH = EA_ROOT / ".codex-design" / "product" / "NEXT_90_DAY_QUEUE_STAGING.generated.yaml"
NEXT90_GUIDE_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_PRODUCT_ADVANCE_GUIDE.md")
SCREENSHOT_GATE_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/CHUMMER5A_SCREENSHOT_REVIEW_GATE.generated.json")
SECTION_HOST_PARITY_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/SECTION_HOST_RULESET_PARITY.generated.json")
GENERATED_DIALOG_PARITY_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/GENERATED_DIALOG_ELEMENT_PARITY.generated.json")
M114_RULE_STUDIO_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/NEXT90_M114_UI_RULE_STUDIO.generated.json")
UI_DIRECT_OUTPUT_PROOF_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/NEXT90_M143_UI_DIRECT_OUTPUT_PROOF.generated.json")
CORE_RECEIPTS_DOC_PATH = Path("/docker/chummercomplete/chummer-core-engine/docs/NEXT90_M143_EXPORT_PRINT_SUPPLEMENT_RULE_ENVIRONMENT_RECEIPTS.md")
FLEET_M143_GATE_PATH = Path("/docker/fleet/.codex-studio/published/NEXT90_M143_FLEET_ROUTE_LOCAL_OUTPUT_CLOSEOUT_GATES.generated.json")
FLAGSHIP_READINESS_PATH = Path("/docker/fleet/.codex-studio/published/FLAGSHIP_PRODUCT_READINESS.generated.json")
LOCAL_MIRROR_REGISTRY_PATH = EA_ROOT / ".codex-design" / "product" / "NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml"

PACKAGE_ID = "next90-m143-ea-compile-route-specific-compare-packs-and-artifact-proofs-for-print-export"
TITLE = "Compile route-specific compare packs and artifact proofs for print, export, exchange, SR6 supplement, and house-rule workflows."
WORK_TASK_ID = "143.5"
MILESTONE_ID = 143
FRONTIER_ID = 5326878760
WAVE = "W22P"
OWNED_SURFACES = ["compile_route_specific_compare_packs_and_artifact_proofs:ea"]
ALLOWED_PATHS = ["scripts", "feedback", "docs"]

GUIDE_MARKERS = {
    "wave": "## Wave 22P - close human-tested parity proof and desktop executable trust before successor breadth",
    "milestone": "### 143. Direct parity proof for print/export/exchange and SR6 supplements or house-rule workflows",
    "exit": "Exit: print/export/exchange plus SR6 supplement/house-rule families all flip to direct `yes/yes` parity with current screenshot/runtime proof and receipt-backed outputs.",
}

TARGET_FAMILIES: dict[str, dict[str, Any]] = {
    "sheet_export_print_viewer_and_exchange": {
        "label": "Sheet export, print viewer, and exchange",
        "required_compare_artifacts": ["menu:open_for_printing", "menu:open_for_export", "menu:file_print_multiple"],
        "required_route_receipts": [
            {
                "route_id": "menu:open_for_printing",
                "source_key": "section_host_ruleset_parity",
                "required_tokens": ["open_for_printing"],
            },
            {
                "route_id": "menu:open_for_export",
                "source_key": "section_host_ruleset_parity",
                "required_tokens": ["open_for_export"],
            },
            {
                "route_id": "menu:file_print_multiple",
                "source_key": "generated_dialog_parity",
                "required_tokens": ["print_multiple"],
            },
            {
                "route_id": "receipt:workspace_exchange",
                "source_key": "core_receipts_doc",
                "required_tokens": [
                    "WorkspaceExchangeDeterministicReceipt",
                    "family:sheet_export_print_viewer_and_exchange",
                ],
            },
            {
                "route_id": "screenshot:print_export_exchange",
                "source_key": "ui_direct_output_proof",
                "required_tokens": [
                    "print_export_exchange",
                    "open_for_printing_menu_route",
                    "open_for_export_menu_route",
                    "print_multiple_menu_route",
                ],
            },
        ],
        "evidence_paths": [
            str(VETERAN_WORKFLOW_PACK_PATH),
            str(SECTION_HOST_PARITY_PATH),
            str(GENERATED_DIALOG_PARITY_PATH),
            str(SCREENSHOT_GATE_PATH),
            str(CORE_RECEIPTS_DOC_PATH),
        ],
    },
    "sr6_supplements_designers_and_house_rules": {
        "label": "SR6 supplements, designers, and house rules",
        "required_compare_artifacts": ["workflow:sr6_supplements", "workflow:house_rules"],
        "required_route_receipts": [
            {
                "route_id": "workflow:sr6_supplements",
                "source_key": "core_receipts_doc",
                "required_tokens": [
                    "Sr6SuccessorLaneDeterministicReceipt",
                    "family:sr6_supplements_designers_and_house_rules",
                    "supplement",
                ],
            },
            {
                "route_id": "workflow:house_rules",
                "source_key": "core_receipts_doc",
                "required_tokens": [
                    "Sr6SuccessorLaneDeterministicReceipt",
                    "family:sr6_supplements_designers_and_house_rules",
                    "house-rule",
                ],
            },
            {
                "route_id": "surface:rule_environment_studio",
                "source_key": "m114_rule_studio",
                "required_tokens": ["rule_environment_studio"],
            },
            {
                "route_id": "screenshot:sr6_supplements_and_house_rules",
                "source_key": "ui_direct_output_proof",
                "required_tokens": ["sr6_supplements_and_house_rules", "sr6_supplements", "house_rules"],
            },
        ],
        "evidence_paths": [
            str(VETERAN_WORKFLOW_PACK_PATH),
            str(UI_DIRECT_OUTPUT_PROOF_PATH),
            str(M114_RULE_STUDIO_PATH),
            str(CORE_RECEIPTS_DOC_PATH),
        ],
    },
}

DESKTOP_REASON_MARKERS: dict[str, tuple[str, ...]] = {
    "sheet_export_print_viewer_and_exchange": (
        "desktop workflow execution gate",
        "chummer5a desktop workflow parity proof",
        "release channel publishes linux installer media",
        "release channel publishes windows installer media",
    ),
    "sr6_supplements_designers_and_house_rules": (
        "sr6 desktop workflow parity proof",
        "desktop workflow execution gate",
    ),
}


def _yaml(path: Path) -> dict[str, Any]:
    return load_yaml_dict(path)


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8")) or {}
    return dict(payload) if isinstance(payload, dict) else {}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _row_fingerprint(payload: dict[str, Any], fields: list[str]) -> str:
    normalized = {field: payload.get(field) for field in fields}
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _queue_rows(path: Path) -> list[dict[str, Any]]:
    payload = _yaml(path)
    return [dict(row) for row in (payload.get("items") or []) if isinstance(row, dict) and str(row.get("package_id") or "") == PACKAGE_ID]


def _queue_input(path: Path) -> dict[str, Any]:
    rows = _queue_rows(path)
    row = rows[0] if rows else {}
    fields = ["package_id", "status", "frontier_id", "repo", "wave", "work_task_id", "milestone_id", "allowed_paths", "owned_surfaces"]
    return {
        "path": str(path),
        "match_count": len(rows),
        "unique_match": len(rows) == 1,
        "status": str(row.get("status") or ""),
        "frontier_id": int(row.get("frontier_id") or 0),
        "owner": str(row.get("repo") or ""),
        "row_fingerprint": __import__("hashlib").sha256(_row_fingerprint(row, fields).encode("utf-8")).hexdigest() if row else "",
    }


def _registry_tasks(path: Path) -> list[dict[str, Any]]:
    payload = _yaml(path)
    rows: list[dict[str, Any]] = []
    for milestone in payload.get("milestones") or []:
        if isinstance(milestone, dict) and int(milestone.get("id") or 0) == MILESTONE_ID:
            for task in milestone.get("work_tasks") or []:
                if isinstance(task, dict) and str(task.get("id") or "").strip() == WORK_TASK_ID:
                    rows.append(dict(task))
    return rows


def _registry_input(path: Path) -> dict[str, Any]:
    rows = _registry_tasks(path)
    row = rows[0] if rows else {}
    fields = ["id", "title", "owner", "status"]
    return {
        "path": str(path),
        "match_count": len(rows),
        "unique_match": len(rows) == 1,
        "status": str(row.get("status") or ""),
        "owner": str(row.get("owner") or ""),
        "title": str(row.get("title") or ""),
        "row_fingerprint": __import__("hashlib").sha256(_row_fingerprint(row, fields).encode("utf-8")).hexdigest() if row else "",
    }


def _family_row(compare_packs: dict[str, Any], family_id: str) -> dict[str, Any]:
    for row in compare_packs.get("families") or []:
        if isinstance(row, dict) and str(row.get("id") or "").strip() == family_id:
            return dict(row)
    return {}


def _workflow_family_row(workflow_pack: dict[str, Any], family_id: str) -> dict[str, Any]:
    for row in workflow_pack.get("families") or []:
        if isinstance(row, dict) and str(row.get("id") or "").strip() == family_id:
            return dict(row)
    return {}


def _generated_at(path: Path, payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    direct = str(payload.get("generated_at") or payload.get("generatedAt") or "").strip()
    if direct:
        return direct
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    if _strip_generated_fields(existing) == _strip_generated_fields(payload):
        stable_payload = dict(payload)
        stable_payload["generated_at"] = existing_generated_at
        return stable_payload
    return payload


def build_payload() -> dict[str, Any]:
    compare_packs = _yaml(COMPARE_PACKS_PATH)
    workflow_pack = _yaml(VETERAN_WORKFLOW_PACK_PATH)
    design_queue_input = _queue_input(DESIGN_QUEUE_PATH)
    fleet_queue_input = _queue_input(FLEET_QUEUE_PATH)
    local_mirror_queue_input = _queue_input(LOCAL_MIRROR_QUEUE_PATH)
    design_queue_row = _queue_rows(DESIGN_QUEUE_PATH)[0] if design_queue_input["match_count"] else {}
    fleet_queue_row = _queue_rows(FLEET_QUEUE_PATH)[0] if fleet_queue_input["match_count"] else {}
    local_mirror_queue_row = _queue_rows(LOCAL_MIRROR_QUEUE_PATH)[0] if local_mirror_queue_input["match_count"] else {}
    registry_input = _registry_input(SUCCESSOR_REGISTRY_PATH)
    local_mirror_registry_input = _registry_input(LOCAL_MIRROR_REGISTRY_PATH)
    registry_task = _registry_tasks(SUCCESSOR_REGISTRY_PATH)[0] if registry_input["match_count"] else {}
    guide_text = _text(NEXT90_GUIDE_PATH)
    screenshot_gate = _json(SCREENSHOT_GATE_PATH)
    section_host_parity = _json(SECTION_HOST_PARITY_PATH)
    generated_dialog_parity = _json(GENERATED_DIALOG_PARITY_PATH)
    m114_rule_studio = _json(M114_RULE_STUDIO_PATH)
    ui_direct_output_proof = _json(UI_DIRECT_OUTPUT_PROOF_PATH)
    fleet_gate = _json(FLEET_M143_GATE_PATH)
    readiness = _json(FLAGSHIP_READINESS_PATH)
    coverage = dict(readiness.get("coverage") or {})
    coverage_details = dict(readiness.get("coverage_details") or {})
    desktop_coverage = dict(coverage_details.get("desktop_client") or {})
    raw_desktop_status = str(coverage.get("desktop_client") or desktop_coverage.get("status") or "")
    raw_desktop_summary = str(desktop_coverage.get("summary") or "")
    raw_desktop_reasons = [str(item) for item in (desktop_coverage.get("reasons") or [])]
    readiness_row_fingerprint_basis = ["status", "summary", "reasons", "evidence"]
    readiness_row_fingerprint = __import__("hashlib").sha256(
        json.dumps({field: desktop_coverage.get(field) for field in readiness_row_fingerprint_basis}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    proof_texts = {
        "screenshot_gate": json.dumps(screenshot_gate, sort_keys=True),
        "section_host_ruleset_parity": json.dumps(section_host_parity, sort_keys=True),
        "generated_dialog_parity": json.dumps(generated_dialog_parity, sort_keys=True),
        "m114_rule_studio": json.dumps(m114_rule_studio, sort_keys=True),
        "ui_direct_output_proof": json.dumps(ui_direct_output_proof, sort_keys=True),
        "core_receipts_doc": _text(CORE_RECEIPTS_DOC_PATH),
    }

    compare_rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for family_id, spec in TARGET_FAMILIES.items():
        compare_row = _family_row(compare_packs, family_id)
        workflow_row = _workflow_family_row(workflow_pack, family_id)
        compare_artifacts = [str(item) for item in (compare_row.get("compare_artifacts") or [])]
        missing_compare_artifacts = [item for item in spec["required_compare_artifacts"] if item not in compare_artifacts]
        route_receipts: list[dict[str, Any]] = []
        missing_route_receipts: list[str] = []
        for receipt in spec["required_route_receipts"]:
            text = proof_texts[receipt["source_key"]]
            satisfied = all(token in text for token in receipt["required_tokens"])
            route_receipts.append(
                {
                    "route_id": receipt["route_id"],
                    "source_key": receipt["source_key"],
                    "required_tokens": list(receipt["required_tokens"]),
                    "satisfied": satisfied,
                }
            )
            if not satisfied:
                missing_route_receipts.append(receipt["route_id"])
        issues: list[str] = []
        if not compare_row:
            issues.append("EA compare_packs family row is missing.")
        if not workflow_row:
            issues.append("Fleet veteran workflow family row is missing.")
        if missing_compare_artifacts:
            issues.append("missing compare_artifacts: " + ", ".join(missing_compare_artifacts))
        if missing_route_receipts:
            issues.append("missing route-local receipts: " + ", ".join(missing_route_receipts))
        if issues:
            unresolved.append(f"{family_id}: {'; '.join(issues)}")
        relevant_desktop_reasons = [
            reason
            for reason in raw_desktop_reasons
            if any(marker in reason.lower() for marker in DESKTOP_REASON_MARKERS.get(family_id, ()))
        ]
        compare_rows.append(
            {
                "family_id": family_id,
                "label": spec["label"],
                "compare_artifacts": compare_artifacts,
                "required_compare_artifacts": list(spec["required_compare_artifacts"]),
                "workflow_readiness_target": str(workflow_row.get("readiness_target") or ""),
                "expected_readiness_floor": str(compare_row.get("expected_readiness_floor") or ""),
                "evidence_paths": list(spec["evidence_paths"]),
                "desktop_client_dependency": {
                    "coverage_key": "desktop_client",
                    "coverage_status": raw_desktop_status,
                    "coverage_summary": raw_desktop_summary,
                    "relevant_reasons": relevant_desktop_reasons,
                },
                "route_receipts": route_receipts,
                "issues": issues,
            }
        )

    if not unresolved:
        desktop_status = "ready"
        desktop_summary = "EA-scoped route-specific compare proof for milestone 143 is ready."
        desktop_reasons: list[str] = []
    else:
        desktop_status = raw_desktop_status
        desktop_summary = raw_desktop_summary
        desktop_reasons = list(raw_desktop_reasons)

    for row in compare_rows:
        dependency = dict(row.get("desktop_client_dependency") or {})
        dependency["coverage_status"] = desktop_status
        dependency["coverage_summary"] = desktop_summary
        if desktop_status == "ready":
            dependency["relevant_reasons"] = []
        row["desktop_client_dependency"] = dependency

    guide_checks = {name: marker in guide_text for name, marker in GUIDE_MARKERS.items()}
    guide_issues = [name for name, present in guide_checks.items() if not present]
    queue_checks = {
        "design_queue_present": bool(design_queue_row),
        "design_queue_unique": bool(design_queue_input["unique_match"]),
        "fleet_queue_present": bool(fleet_queue_row),
        "fleet_queue_unique": bool(fleet_queue_input["unique_match"]),
        "local_mirror_queue_present": bool(local_mirror_queue_row),
        "local_mirror_queue_unique": bool(local_mirror_queue_input["unique_match"]),
        "registry_task_present": bool(registry_task),
        "registry_task_unique": bool(registry_input["unique_match"]),
        "local_mirror_registry_task_present": bool(local_mirror_registry_input["match_count"]),
        "local_mirror_registry_task_unique": bool(local_mirror_registry_input["unique_match"]),
        "package_id_matches": str(design_queue_row.get("package_id") or "") == PACKAGE_ID
        and str(fleet_queue_row.get("package_id") or "") == PACKAGE_ID
        and str(local_mirror_queue_row.get("package_id") or "") == PACKAGE_ID,
        "title_matches": str(design_queue_row.get("title") or "") == TITLE
        and str(fleet_queue_row.get("title") or "") == TITLE
        and str(local_mirror_queue_row.get("title") or "") == TITLE
        and str(registry_task.get("title") or "") == TITLE
        and local_mirror_registry_input["title"] == TITLE,
        "task_matches": str(design_queue_row.get("task") or "") == TITLE
        and str(fleet_queue_row.get("task") or "") == TITLE
        and str(local_mirror_queue_row.get("task") or "") == TITLE,
        "work_task_matches": str(design_queue_row.get("work_task_id") or "") == WORK_TASK_ID
        and str(fleet_queue_row.get("work_task_id") or "") == WORK_TASK_ID
        and str(local_mirror_queue_row.get("work_task_id") or "") == WORK_TASK_ID
        and str(registry_task.get("id") or "") == WORK_TASK_ID,
        "frontier_matches": int(design_queue_row.get("frontier_id") or 0) == FRONTIER_ID
        and int(fleet_queue_row.get("frontier_id") or 0) == FRONTIER_ID
        and int(local_mirror_queue_row.get("frontier_id") or 0) == FRONTIER_ID,
        "milestone_matches": int(design_queue_row.get("milestone_id") or 0) == MILESTONE_ID
        and int(fleet_queue_row.get("milestone_id") or 0) == MILESTONE_ID
        and int(local_mirror_queue_row.get("milestone_id") or 0) == MILESTONE_ID,
        "wave_matches": str(design_queue_row.get("wave") or "") == WAVE
        and str(fleet_queue_row.get("wave") or "") == WAVE
        and str(local_mirror_queue_row.get("wave") or "") == WAVE,
        "repo_matches": str(design_queue_row.get("repo") or "") == "executive-assistant"
        and str(fleet_queue_row.get("repo") or "") == "executive-assistant"
        and str(local_mirror_queue_row.get("repo") or "") == "executive-assistant"
        and str(registry_task.get("owner") or "") == "executive-assistant"
        and local_mirror_registry_input["owner"] == "executive-assistant",
        "allowed_paths_match": list(design_queue_row.get("allowed_paths") or []) == ALLOWED_PATHS
        and list(fleet_queue_row.get("allowed_paths") or []) == ALLOWED_PATHS
        and list(local_mirror_queue_row.get("allowed_paths") or []) == ALLOWED_PATHS,
        "owned_surfaces_match": list(design_queue_row.get("owned_surfaces") or []) == OWNED_SURFACES
        and list(fleet_queue_row.get("owned_surfaces") or []) == OWNED_SURFACES
        and list(local_mirror_queue_row.get("owned_surfaces") or []) == OWNED_SURFACES,
        "design_fleet_queue_fingerprint_matches": design_queue_input["row_fingerprint"] != "" and design_queue_input["row_fingerprint"] == fleet_queue_input["row_fingerprint"],
        "design_local_mirror_queue_fingerprint_matches": design_queue_input["row_fingerprint"] != "" and design_queue_input["row_fingerprint"] == local_mirror_queue_input["row_fingerprint"],
        "registry_task_owner_matches": registry_input["owner"] == "executive-assistant",
        "registry_task_title_matches": registry_input["title"] == TITLE,
        "local_mirror_queue_owner_matches": local_mirror_queue_input["owner"] == "executive-assistant",
        "local_mirror_queue_frontier_matches": local_mirror_queue_input["frontier_id"] == FRONTIER_ID,
        "local_mirror_queue_allowed_paths_match": list(local_mirror_queue_row.get("allowed_paths") or []) == ALLOWED_PATHS,
        "local_mirror_queue_owned_surfaces_match": list(local_mirror_queue_row.get("owned_surfaces") or []) == OWNED_SURFACES,
        "local_mirror_registry_task_owner_matches": local_mirror_registry_input["owner"] == "executive-assistant",
        "local_mirror_registry_task_title_matches": local_mirror_registry_input["title"] == TITLE,
        "registry_local_mirror_task_fingerprint_matches": registry_input["row_fingerprint"] != "" and registry_input["row_fingerprint"] == local_mirror_registry_input["row_fingerprint"],
    }
    fleet_gate_status = str(fleet_gate.get("status") or "")
    fleet_closeout_status = str(dict(fleet_gate.get("monitor_summary") or {}).get("route_local_output_closeout_status") or "")
    fleet_gate_checks = {
        "gate_status_pass": fleet_gate_status == "pass",
        "route_local_output_closeout_status_pass": fleet_closeout_status == "pass",
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
    if not all(fleet_gate_checks.values()):
        closeout_blockers.append("fleet route-local output closeout gate is not passing")
    if unresolved:
        closeout_blockers.extend(unresolved)
    if desktop_status != "ready":
        blocker = f"published readiness still reports desktop_client as {desktop_status or 'unknown'}"
        if desktop_summary:
            blocker += f": {desktop_summary}"
        closeout_blockers.append(blocker)
    if not queue_closeout["ready_to_mark_complete"]:
        closeout_blockers.append("canonical design/queue rows are not marked complete yet")

    packet_status = (
        "pass"
        if not unresolved and not guide_issues and all(queue_checks.values()) and all(fleet_gate_checks.values())
        else "fail"
    )

    payload = {
        "contract_name": "ea.next90_m143_route_specific_compare_packs",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "package_id": PACKAGE_ID,
        "title": TITLE,
        "milestone_id": MILESTONE_ID,
        "work_task_id": WORK_TASK_ID,
        "frontier_id": FRONTIER_ID,
        "wave": WAVE,
        "owned_surfaces": list(OWNED_SURFACES),
        "allowed_paths": list(ALLOWED_PATHS),
        "status": packet_status,
        "summary": {
            "route_family_count": len(compare_rows),
            "route_family_pass_count": sum(1 for row in compare_rows if not row["issues"]),
            "fleet_m143_gate_status": fleet_gate_status,
            "fleet_m143_closeout_status": fleet_closeout_status,
            "desktop_client_status": desktop_status,
            "desktop_client_reason_count": len(desktop_reasons),
        },
        "source_inputs": {
            "ea_compare_packs": {"path": str(COMPARE_PACKS_PATH), "generated_at": _generated_at(COMPARE_PACKS_PATH)},
            "fleet_veteran_workflow_pack": {"path": str(VETERAN_WORKFLOW_PACK_PATH), "generated_at": _generated_at(VETERAN_WORKFLOW_PACK_PATH)},
            "next90_guide": {"path": str(NEXT90_GUIDE_PATH), "generated_at": _generated_at(NEXT90_GUIDE_PATH)},
            "design_queue": design_queue_input,
            "fleet_queue": fleet_queue_input,
            "local_mirror_queue": local_mirror_queue_input,
            "registry": registry_input,
            "local_mirror_registry": local_mirror_registry_input,
            "screenshot_gate": {"path": str(SCREENSHOT_GATE_PATH), "generated_at": _generated_at(SCREENSHOT_GATE_PATH, screenshot_gate)},
            "section_host_ruleset_parity": {"path": str(SECTION_HOST_PARITY_PATH), "generated_at": _generated_at(SECTION_HOST_PARITY_PATH, section_host_parity)},
            "generated_dialog_parity": {"path": str(GENERATED_DIALOG_PARITY_PATH), "generated_at": _generated_at(GENERATED_DIALOG_PARITY_PATH, generated_dialog_parity)},
            "m114_rule_studio": {"path": str(M114_RULE_STUDIO_PATH), "generated_at": _generated_at(M114_RULE_STUDIO_PATH, m114_rule_studio)},
            "ui_direct_output_proof": {"path": str(UI_DIRECT_OUTPUT_PROOF_PATH), "generated_at": _generated_at(UI_DIRECT_OUTPUT_PROOF_PATH, ui_direct_output_proof)},
            "core_receipts_doc": {"path": str(CORE_RECEIPTS_DOC_PATH), "generated_at": _generated_at(CORE_RECEIPTS_DOC_PATH)},
            "fleet_m143_gate": {"path": str(FLEET_M143_GATE_PATH), "generated_at": _generated_at(FLEET_M143_GATE_PATH, fleet_gate)},
            "flagship_readiness": {
                "path": str(FLAGSHIP_READINESS_PATH),
                "coverage_key": "desktop_client",
                "status": desktop_status,
                "summary": desktop_summary,
                "reason_count": len(desktop_reasons),
                "source_status": raw_desktop_status,
                "source_summary": raw_desktop_summary,
                "source_reason_count": len(raw_desktop_reasons),
                "row_fingerprint_basis": readiness_row_fingerprint_basis,
                "row_fingerprint": readiness_row_fingerprint,
            },
        },
        "canonical_monitors": {
            "guide_markers": guide_checks,
            "queue_alignment": queue_checks,
            "fleet_gate": fleet_gate_checks,
            "queue_closeout": queue_closeout,
        },
        "desktop_client_readiness": {
            "coverage_key": "desktop_client",
            "status": desktop_status,
            "summary": desktop_summary,
            "reason_count": len(desktop_reasons),
            "reasons": desktop_reasons,
        },
        "family_route_compare_packs": compare_rows,
        "closeout": {
            "ready": not closeout_blockers,
            "blockers": closeout_blockers,
            "notes": [
                "This EA packet compiles route-local compare proof for milestone 143 using current Fleet and owner-repo receipts.",
                "It does not overwrite owner-repo executable proof or pretend the canonical queue closeout already happened.",
            ],
        },
    }
    return _preserve_generated_at(OUTPUT_PATH, payload)


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Next90 M143 EA Route-Specific Compare Packs",
        "",
        f"- status: `{payload.get('status', '')}`",
        f"- ready: `{dict(payload.get('closeout') or {}).get('ready', False)}`",
        "",
        "## Desktop readiness",
        f"- `desktop_client`: `{dict(payload.get('desktop_client_readiness') or {}).get('status', '')}`",
        f"- summary: {dict(payload.get('desktop_client_readiness') or {}).get('summary', '')}",
        f"- canonical queue frontier: `{payload.get('frontier_id', '')}`",
        "",
        "## Family summary",
    ]
    for row in payload.get("family_route_compare_packs") or []:
        current = dict(row)
        lines.append(f"- `{current.get('family_id', '')}`: {'pass' if not current.get('issues') else 'fail'}")
        lines.append(f"  - compare artifacts: `{', '.join(current.get('compare_artifacts') or [])}`")
        dependency = dict(current.get("desktop_client_dependency") or {})
        if dependency.get("relevant_reasons"):
            lines.append(
                f"  - desktop dependency: `{dependency.get('coverage_status', '')}` ({len(list(dependency.get('relevant_reasons') or []))} route-relevant blocker(s))"
            )
        lines.append("  - route receipts:")
        for receipt in current.get("route_receipts") or []:
            route = dict(receipt)
            lines.append(f"    - `{route.get('route_id', '')}` -> `{'ok' if route.get('satisfied') else 'missing'}`")
            lines.append(
                f"      - receipt proof: `{route.get('source_key', '')}` requires `{', '.join(route.get('required_tokens') or [])}`"
            )
    lines.extend(
        [
            "",
            "## Queue guardrails",
            "- design queue, Fleet queue, and the approved `.codex-design` local mirror must each contain exactly one matching package row.",
            "- duplicate queue or registry rows fail closed.",
            "",
            "## Closeout blockers",
        ]
    )
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
        "# Next90 M143 EA route-specific compare packs",
        "",
        "Refreshed the EA-owned M143 receipt so it stays aligned with the current route-local compare proof instead of stale queue prose.",
        "",
        f"The packet is pinned to canonical queue frontier `{payload.get('frontier_id', '')}`, the live readiness posture is `desktop_client = {readiness.get('status', 'unknown')}`, and duplicate queue or registry rows fail closed across the design queue, Fleet queue, the approved `.codex-design local mirror`, and the mirrored registry task.",
        "",
        "Current families:",
    ]
    for row in payload.get("family_route_compare_packs") or []:
        lines.append(f"- `{dict(row).get('family_id', '')}`")
    lines.extend(
        [
            "",
            "Guardrails:",
            "- canonical queue frontier alignment is required before any closeout claim",
            "- duplicate queue or registry rows fail closed",
            "- the approved `.codex-design local mirror` must stay aligned with canonical queue and registry metadata",
            "",
            "Intentional boundary:",
            "- this package compiles route-local compare and artifact proof only",
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
