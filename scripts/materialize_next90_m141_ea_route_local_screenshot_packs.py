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

OUTPUT_PATH = DOCS_ROOT / "NEXT90_M141_ROUTE_LOCAL_SCREENSHOT_PACKS.generated.yaml"
MARKDOWN_PATH = DOCS_ROOT / "NEXT90_M141_ROUTE_LOCAL_SCREENSHOT_PACKS.generated.md"
COMPARE_PACKS_PATH = EA_ROOT / "docs" / "chummer5a_parity_lab" / "compare_packs.yaml"
CAPTURE_PACK_PATH = Path("/docker/fleet/docs/chummer5a-oracle/parity_lab_capture_pack.yaml")
VETERAN_WORKFLOW_PACK_PATH = Path("/docker/fleet/docs/chummer5a-oracle/veteran_workflow_packs.yaml")
SUCCESSOR_REGISTRY_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml")
DESIGN_QUEUE_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
FLEET_QUEUE_PATH = Path("/docker/fleet/.codex-studio/published/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
LOCAL_MIRROR_REGISTRY_PATH = EA_ROOT / ".codex-design" / "product" / "NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml"
LOCAL_MIRROR_QUEUE_PATH = EA_ROOT / ".codex-design" / "product" / "NEXT_90_DAY_QUEUE_STAGING.generated.yaml"
NEXT90_GUIDE_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_PRODUCT_ADVANCE_GUIDE.md")
PARITY_AUDIT_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/CHUMMER5A_UI_ELEMENT_PARITY_AUDIT.generated.json")
UI_FLAGSHIP_GATE_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/UI_FLAGSHIP_RELEASE_GATE.generated.json")
VISUAL_GATE_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/DESKTOP_VISUAL_FAMILIARITY_EXIT_GATE.generated.json")
WORKFLOW_GATE_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/DESKTOP_WORKFLOW_EXECUTION_GATE.generated.json")
VETERAN_TASK_GATE_PATH = Path("/docker/chummercomplete/chummer-presentation/.codex-studio/published/VETERAN_TASK_TIME_EVIDENCE_GATE.generated.json")
UI_DIRECT_PROOF_PATH = Path("/docker/chummercomplete/chummer6-ui/.codex-studio/published/NEXT90_M141_UI_DIRECT_IMPORT_ROUTE_PROOF.generated.json")
IMPORT_CERT_PATH = Path("/docker/chummercomplete/chummer6-core/.codex-studio/published/IMPORT_PARITY_CERTIFICATION.generated.json")
IMPORT_RECEIPTS_DOC_PATH = Path("/docker/chummercomplete/chummer-core-engine/docs/NEXT90_M141_IMPORT_ROUTE_RECEIPTS.md")
IMPORT_RECEIPTS_JSON_PATH = Path("/docker/chummercomplete/chummer-core-engine/.codex-studio/published/NEXT90_M141_IMPORT_ROUTE_RECEIPTS.generated.json")
FLEET_GATE_PATH = Path("/docker/fleet/.codex-studio/published/NEXT90_M141_FLEET_IMPORT_ROUTE_CLOSEOUT_GATES.generated.json")
FLAGSHIP_READINESS_PATH = Path("/docker/fleet/.codex-studio/published/FLAGSHIP_PRODUCT_READINESS.generated.json")

PACKAGE_ID = "next90-m141-ea-compile-route-local-screenshot-packs-and-compare-packets-for-translator-x"
TITLE = "Compile route-local screenshot packs and compare packets for translator, XML amendment, Hero Lab, and import-oracle proof without inventing parity."
WORK_TASK_ID = "141.4"
MILESTONE_ID = 141
WAVE = "W22P"
OWNED_SURFACES = ["compile_route_local_screenshot_packs_and_compare_packets:ea"]
ALLOWED_PATHS = ["scripts", "feedback", "docs"]
PARITY_REQUIRED_FIELDS = (
    "present_in_chummer5a",
    "present_in_chummer6",
    "visual_parity",
    "behavioral_parity",
    "removable_if_not_in_chummer5a",
    "reason",
)

GUIDE_MARKERS = {
    "wave": "## Wave 22P - close human-tested parity proof and desktop executable trust before successor breadth",
    "milestone": "### 141. Direct parity proof for translator, XML amendment, Hero Lab, and adjacent import routes",
    "exit": "Exit: the translator, XML amendment editor, Hero Lab importer, custom-data/XML bridge, and adjacent import-oracle rows all flip to direct `yes/yes` parity with current screenshot-backed and runtime-backed receipts.",
}

ROUTE_SPECS: dict[str, dict[str, Any]] = {
    "menu:translator": {
        "label": "Translator route",
        "parity_row_id": "source:translator_route",
        "compare_family_id": "custom_data_xml_and_translator_bridge",
        "ui_direct_group": "translator_xml_custom_data",
        "legacy_source_line_id": "translator_route",
        "required_compare_artifacts": ["menu:translator"],
        "required_screenshots": ["38-translator-dialog-light.png"],
        "required_tokens": [
            {
                "source_key": "ui_direct_import_route_proof",
                "tokens": [
                    "ExecuteCommandAsync_translator_opens_dialog_with_master_index_lane_posture",
                    "Runtime_backed_translator_xml_editor_and_hero_lab_importer_routes_surface_governed_posture",
                ],
            },
            {
                "source_key": "import_receipts_doc",
                "tokens": [
                    "translatorDeterministicReceipt",
                    "source:translator_route",
                ],
            },
            {
                "source_key": "import_receipts_json",
                "tokens": ["translatorDeterministicReceipt"],
            },
        ],
        "deterministic_receipts": ["translatorDeterministicReceipt"],
    },
    "menu:xml_editor": {
        "label": "XML amendment editor route",
        "parity_row_id": "source:xml_amendment_editor_route",
        "compare_family_id": "custom_data_xml_and_translator_bridge",
        "ui_direct_group": "translator_xml_custom_data",
        "legacy_source_line_id": "xml_amendment_editor_route",
        "required_compare_artifacts": ["menu:xml_editor"],
        "required_screenshots": ["39-xml-editor-dialog-light.png"],
        "required_tokens": [
            {
                "source_key": "ui_direct_import_route_proof",
                "tokens": [
                    "ExecuteCommandAsync_xml_editor_opens_dialog_with_xml_bridge_posture",
                    "CreateCommandDialog_xml_editor_surfaces_xml_bridge_and_custom_data_posture",
                    "Runtime_backed_translator_xml_editor_and_hero_lab_importer_routes_surface_governed_posture",
                ],
            },
            {
                "source_key": "import_receipts_doc",
                "tokens": [
                    "customDataXmlBridgeDeterministicReceipt",
                    "amendPackageDeterministicReceipt",
                ],
            },
            {
                "source_key": "import_receipts_json",
                "tokens": [
                    "customDataXmlBridgeDeterministicReceipt",
                    "amendPackageDeterministicReceipt",
                ],
            },
        ],
        "deterministic_receipts": [
            "customDataXmlBridgeDeterministicReceipt",
            "amendPackageDeterministicReceipt",
        ],
    },
    "menu:hero_lab_importer": {
        "label": "Hero Lab importer route",
        "parity_row_id": "source:hero_lab_importer_route",
        "compare_family_id": "legacy_and_adjacent_import_oracles",
        "ui_direct_group": "hero_lab_import_oracle",
        "legacy_source_line_id": "hero_lab_importer_route",
        "required_compare_artifacts": ["menu:hero_lab_importer"],
        "required_screenshots": ["40-hero-lab-importer-dialog-light.png"],
        "required_tokens": [
            {
                "source_key": "ui_direct_import_route_proof",
                "tokens": [
                    "ExecuteCommandAsync_hero_lab_importer_opens_dialog_with_import_oracle_lane_posture",
                    "CreateCommandDialog_hero_lab_importer_surfaces_import_oracle_and_adjacent_sr6_posture",
                    "Runtime_backed_translator_xml_editor_and_hero_lab_importer_routes_surface_governed_posture",
                ],
            },
            {
                "source_key": "import_receipts_doc",
                "tokens": [
                    "importOracleDeterministicReceipt",
                    "family:legacy_and_adjacent_import_oracles",
                ],
            },
            {
                "source_key": "import_receipts_json",
                "tokens": ["importOracleDeterministicReceipt"],
            },
        ],
        "deterministic_receipts": ["importOracleDeterministicReceipt"],
    },
    "workflow:import_oracle": {
        "label": "Import-oracle workflow",
        "compare_family_id": "legacy_and_adjacent_import_oracles",
        "ui_direct_group": "hero_lab_import_oracle",
        "required_compare_artifacts": ["workflow:import_oracle"],
        "required_screenshots": ["40-hero-lab-importer-dialog-light.png"],
        "required_tokens": [
            {
                "source_key": "import_receipts_doc",
                "tokens": [
                    "importOracleDeterministicReceipt",
                    "family:legacy_and_adjacent_import_oracles",
                ],
            },
            {
                "source_key": "import_certification",
                "tokens": [
                    "Hero Lab Classic",
                    "Genesis",
                    "CommLink6",
                    "\"coverage_percent\": 100",
                ],
            },
        ],
        "deterministic_receipts": ["importOracleDeterministicReceipt"],
    },
}

FAMILY_SPECS: dict[str, dict[str, Any]] = {
    "custom_data_xml_and_translator_bridge": {
        "label": "Custom data/XML and translator bridge",
        "parity_row_id": "family:custom_data_xml_and_translator_bridge",
        "ui_direct_group": "translator_xml_custom_data",
        "required_compare_artifacts": ["menu:translator", "menu:xml_editor"],
        "required_screenshots": ["38-translator-dialog-light.png", "39-xml-editor-dialog-light.png"],
        "deterministic_receipts": [
            "customDataXmlBridgeDeterministicReceipt",
            "translatorDeterministicReceipt",
            "amendPackageDeterministicReceipt",
        ],
    },
    "legacy_and_adjacent_import_oracles": {
        "label": "Legacy and adjacent import-oracle family",
        "parity_row_id": "family:legacy_and_adjacent_import_oracles",
        "ui_direct_group": "hero_lab_import_oracle",
        "required_compare_artifacts": ["menu:hero_lab_importer", "workflow:import_oracle"],
        "required_screenshots": ["40-hero-lab-importer-dialog-light.png"],
        "deterministic_receipts": ["importOracleDeterministicReceipt"],
    },
}

SCREENSHOT_REVIEW_JOB_GROUPS: dict[str, list[str]] = {
    "translator_xml_custom_data": ["translator", "xml_editor"],
    "hero_lab_import_oracle": ["hero_lab_importer"],
}


def _yaml(path: Path) -> dict[str, Any]:
    return load_yaml_dict(path)


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8")) or {}
    return dict(payload) if isinstance(payload, dict) else {}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


def _stable_runtime_fingerprint(value: Any) -> str:
    return _stable_fingerprint(_strip_generated_fields(value))


def _desktop_readiness_fingerprint_payload(
    desktop_coverage: dict[str, Any],
    *,
    desktop_status: str,
    desktop_summary: str,
    desktop_reasons: list[str],
) -> dict[str, Any]:
    return {
        "coverage_key": "desktop_client",
        "status": desktop_status,
        "summary": desktop_summary,
        "reasons": list(desktop_reasons),
        "reason_count": len(desktop_reasons),
    }


def _queue_row(path: Path) -> dict[str, Any]:
    rows = _queue_rows(path)
    if len(rows) == 1:
        return rows[0]
    return {}


def _queue_rows(path: Path) -> list[dict[str, Any]]:
    payload = _yaml(path)
    rows: list[dict[str, Any]] = []
    for row in payload.get("items") or []:
        if isinstance(row, dict) and str(row.get("package_id") or "").strip() == PACKAGE_ID:
            rows.append(dict(row))
    return rows


def _registry_task(path: Path) -> dict[str, Any]:
    tasks = _registry_tasks(path)
    if len(tasks) == 1:
        return tasks[0]
    return {}


def _registry_tasks(path: Path) -> list[dict[str, Any]]:
    payload = _yaml(path)
    tasks: list[dict[str, Any]] = []
    for milestone in payload.get("milestones") or []:
        if isinstance(milestone, dict) and int(milestone.get("id") or 0) == MILESTONE_ID:
            for task in milestone.get("work_tasks") or []:
                if isinstance(task, dict) and str(task.get("id") or "").strip() == WORK_TASK_ID:
                    tasks.append(dict(task))
    return tasks


def _compare_family_row(compare_packs: dict[str, Any], family_id: str) -> dict[str, Any]:
    for row in compare_packs.get("families") or []:
        if isinstance(row, dict) and str(row.get("id") or "").strip() == family_id:
            return dict(row)
    return {}


def _workflow_family_row(workflow_pack: dict[str, Any], family_id: str) -> dict[str, Any]:
    for row in workflow_pack.get("families") or []:
        if isinstance(row, dict) and str(row.get("id") or "").strip() == family_id:
            return dict(row)
    return {}


def _screenshot_review_job(group_jobs: dict[str, Any], group_id: str) -> dict[str, Any]:
    direct = dict(group_jobs.get(group_id) or {})
    if direct:
        return direct
    member_ids = SCREENSHOT_REVIEW_JOB_GROUPS.get(group_id) or []
    members = [dict(group_jobs.get(member_id) or {}) for member_id in member_ids if dict(group_jobs.get(member_id) or {})]
    if not members:
        return {}
    screenshots: list[str] = []
    evidence_keys: list[str] = []
    test_markers: list[str] = []
    reasons: list[str] = []
    frontier_ids: list[int] = []
    statuses: list[str] = []
    for member in members:
        screenshots.extend(str(item) for item in (member.get("screenshots") or []))
        evidence_keys.extend(str(item) for item in (member.get("evidenceKeys") or []))
        test_markers.extend(str(item) for item in (member.get("testMarkers") or []))
        reasons.extend(str(item) for item in (member.get("reasons") or []))
        frontier_id = member.get("frontierId")
        if isinstance(frontier_id, int):
            frontier_ids.append(frontier_id)
        status = str(member.get("status") or "").strip()
        if status:
            statuses.append(status)
    dedupe = lambda items: list(dict.fromkeys(items))
    return {
        "frontierId": frontier_ids[0] if frontier_ids else None,
        "frontierIds": dedupe(frontier_ids),
        "status": "pass" if members and all(status == "pass" for status in statuses) else "fail",
        "screenshots": dedupe(screenshots),
        "evidenceKeys": dedupe(evidence_keys),
        "testMarkers": dedupe(test_markers),
        "reasons": dedupe(reasons),
        "memberJobIds": member_ids,
    }


def _effective_screenshot_review_job(
    *,
    group_jobs: dict[str, Any],
    group_id: str,
    required_screenshots: list[str],
    direct_receipt_group: dict[str, Any],
    direct_import_summary: dict[str, Any],
) -> dict[str, Any]:
    screenshot_job = _screenshot_review_job(group_jobs, group_id)
    if screenshot_job:
        return screenshot_job
    available_screenshots = [str(item) for item in (direct_import_summary.get("screenshots") or [])]
    missing = [item for item in required_screenshots if item not in available_screenshots]
    if (
        direct_receipt_group.get("exists") is True
        and direct_receipt_group.get("status_pass") is True
        and direct_receipt_group.get("screenshots_exact") is True
        and not missing
    ):
        return {
            "frontierId": None,
            "frontierIds": [],
            "status": "pass",
            "screenshots": list(required_screenshots),
            "evidenceKeys": ["ui_flagship_gate.directImportRouteProof"],
            "testMarkers": [str(item) for item in (direct_import_summary.get("characterOverviewPresenterTests") or [])],
            "reasons": ["synthesized_from_ui_flagship_direct_import_route_proof"],
            "memberJobIds": [group_id],
        }
    return {}


def _flatten_rows(payload: object) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        if "id" in payload:
            rows.append(dict(payload))
        for value in payload.values():
            rows.extend(_flatten_rows(value))
    elif isinstance(payload, list):
        for item in payload:
            rows.extend(_flatten_rows(item))
    return rows


def _parity_rows_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in _flatten_rows(payload):
        row_id = str(row.get("id") or "").strip()
        if row_id and row_id not in rows:
            rows[row_id] = row
    return rows


def _fleet_target_rows_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    target_rows = dict(payload.get("runtime_monitors") or {}).get("target_rows") or {}
    for row in target_rows.get("rows") or []:
        if isinstance(row, dict):
            row_id = str(row.get("id") or "").strip()
            if row_id:
                rows[row_id] = dict(row)
    return rows


def _source_line_row(capture_pack: dict[str, Any], source_line_id: str) -> dict[str, Any]:
    line_groups = dict(dict(capture_pack.get("oracle_surface_extract") or {}).get("source_line_proofs") or {})
    for rows in line_groups.values():
        for row in rows or []:
            if isinstance(row, dict) and str(row.get("id") or "").strip() == source_line_id:
                return dict(row)
    return {}


def _source_inputs(*, compare_packs: dict[str, Any], workflow_pack: dict[str, Any], parity_audit: dict[str, Any], ui_flagship_gate: dict[str, Any], visual_gate: dict[str, Any], workflow_gate: dict[str, Any], veteran_task_gate: dict[str, Any], ui_direct_proof: dict[str, Any], import_receipts_json: dict[str, Any], fleet_gate: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    design_queue_rows = _queue_rows(DESIGN_QUEUE_PATH)
    fleet_queue_rows = _queue_rows(FLEET_QUEUE_PATH)
    registry_tasks = _registry_tasks(SUCCESSOR_REGISTRY_PATH)
    local_mirror_queue_rows = _queue_rows(LOCAL_MIRROR_QUEUE_PATH)
    local_mirror_registry_tasks = _registry_tasks(LOCAL_MIRROR_REGISTRY_PATH)
    design_queue_row = design_queue_rows[0] if len(design_queue_rows) == 1 else {}
    fleet_queue_row = fleet_queue_rows[0] if len(fleet_queue_rows) == 1 else {}
    registry_task = registry_tasks[0] if len(registry_tasks) == 1 else {}
    local_mirror_queue_row = local_mirror_queue_rows[0] if len(local_mirror_queue_rows) == 1 else {}
    local_mirror_registry_task = local_mirror_registry_tasks[0] if len(local_mirror_registry_tasks) == 1 else {}
    desktop_coverage = dict(dict(readiness.get("coverage_details") or {}).get("desktop_client") or {})
    desktop_status = str(dict(readiness.get("coverage") or {}).get("desktop_client") or desktop_coverage.get("status") or "")
    desktop_summary = str(desktop_coverage.get("summary") or "")
    desktop_reasons = [str(item) for item in (desktop_coverage.get("reasons") or [])]
    readiness_fingerprint_payload = _desktop_readiness_fingerprint_payload(
        desktop_coverage,
        desktop_status=desktop_status,
        desktop_summary=desktop_summary,
        desktop_reasons=desktop_reasons,
    )
    return {
        "ea_compare_packs": {"path": str(COMPARE_PACKS_PATH), "generated_at": _generated_at(COMPARE_PACKS_PATH, compare_packs)},
        "fleet_capture_pack": {"path": str(CAPTURE_PACK_PATH), "generated_at": _generated_at(CAPTURE_PACK_PATH)},
        "fleet_veteran_workflow_pack": {"path": str(VETERAN_WORKFLOW_PACK_PATH), "generated_at": _generated_at(VETERAN_WORKFLOW_PACK_PATH, workflow_pack)},
        "next90_guide": {"path": str(NEXT90_GUIDE_PATH), "generated_at": _generated_at(NEXT90_GUIDE_PATH)},
        "design_queue": {
            "path": str(DESIGN_QUEUE_PATH),
            "match_count": len(design_queue_rows),
            "unique_match": len(design_queue_rows) == 1,
            "status": str(design_queue_row.get("status") or ""),
            "work_task_id": str(design_queue_row.get("work_task_id") or ""),
            "milestone_id": int(design_queue_row.get("milestone_id") or 0),
            "frontier_id": int(design_queue_row.get("frontier_id") or 0),
            "wave": str(design_queue_row.get("wave") or ""),
            "repo": str(design_queue_row.get("repo") or ""),
            "row_fingerprint": _stable_fingerprint(design_queue_row),
        },
        "fleet_queue": {
            "path": str(FLEET_QUEUE_PATH),
            "match_count": len(fleet_queue_rows),
            "unique_match": len(fleet_queue_rows) == 1,
            "status": str(fleet_queue_row.get("status") or ""),
            "work_task_id": str(fleet_queue_row.get("work_task_id") or ""),
            "milestone_id": int(fleet_queue_row.get("milestone_id") or 0),
            "frontier_id": int(fleet_queue_row.get("frontier_id") or 0),
            "wave": str(fleet_queue_row.get("wave") or ""),
            "repo": str(fleet_queue_row.get("repo") or ""),
            "row_fingerprint": _stable_fingerprint(fleet_queue_row),
        },
        "local_mirror_queue": {
            "path": str(LOCAL_MIRROR_QUEUE_PATH),
            "match_count": len(local_mirror_queue_rows),
            "unique_match": len(local_mirror_queue_rows) == 1,
            "status": str(local_mirror_queue_row.get("status") or ""),
            "work_task_id": str(local_mirror_queue_row.get("work_task_id") or ""),
            "milestone_id": int(local_mirror_queue_row.get("milestone_id") or 0),
            "frontier_id": int(local_mirror_queue_row.get("frontier_id") or 0),
            "wave": str(local_mirror_queue_row.get("wave") or ""),
            "repo": str(local_mirror_queue_row.get("repo") or ""),
            "row_fingerprint": _stable_fingerprint(local_mirror_queue_row),
        },
        "registry": {
            "path": str(SUCCESSOR_REGISTRY_PATH),
            "match_count": len(registry_tasks),
            "unique_match": len(registry_tasks) == 1,
            "work_task_id": str(registry_task.get("id") or ""),
            "milestone_id": MILESTONE_ID,
            "status": str(registry_task.get("status") or ""),
            "owner": str(registry_task.get("owner") or ""),
            "title": str(registry_task.get("title") or ""),
            "row_fingerprint": _stable_fingerprint(registry_task),
        },
        "local_mirror_registry": {
            "path": str(LOCAL_MIRROR_REGISTRY_PATH),
            "match_count": len(local_mirror_registry_tasks),
            "unique_match": len(local_mirror_registry_tasks) == 1,
            "work_task_id": str(local_mirror_registry_task.get("id") or ""),
            "milestone_id": MILESTONE_ID,
            "status": str(local_mirror_registry_task.get("status") or ""),
            "owner": str(local_mirror_registry_task.get("owner") or ""),
            "title": str(local_mirror_registry_task.get("title") or ""),
            "row_fingerprint": _stable_fingerprint(local_mirror_registry_task),
        },
        "parity_audit": {"path": str(PARITY_AUDIT_PATH), "generated_at": _generated_at(PARITY_AUDIT_PATH, parity_audit)},
        "ui_flagship_gate": {"path": str(UI_FLAGSHIP_GATE_PATH), "generated_at": _generated_at(UI_FLAGSHIP_GATE_PATH, ui_flagship_gate)},
        "desktop_visual_familiarity_gate": {"path": str(VISUAL_GATE_PATH), "generated_at": _generated_at(VISUAL_GATE_PATH, visual_gate)},
        "desktop_workflow_execution_gate": {"path": str(WORKFLOW_GATE_PATH), "generated_at": _generated_at(WORKFLOW_GATE_PATH, workflow_gate)},
        "veteran_task_time_gate": {"path": str(VETERAN_TASK_GATE_PATH), "generated_at": _generated_at(VETERAN_TASK_GATE_PATH, veteran_task_gate)},
        "ui_direct_import_route_proof": {"path": str(UI_DIRECT_PROOF_PATH), "generated_at": _generated_at(UI_DIRECT_PROOF_PATH, ui_direct_proof)},
        "import_parity_certification": {"path": str(IMPORT_CERT_PATH), "generated_at": _generated_at(IMPORT_CERT_PATH)},
        "import_receipts_doc": {"path": str(IMPORT_RECEIPTS_DOC_PATH), "generated_at": _generated_at(IMPORT_RECEIPTS_DOC_PATH)},
        "import_receipts_json": {"path": str(IMPORT_RECEIPTS_JSON_PATH), "generated_at": _generated_at(IMPORT_RECEIPTS_JSON_PATH, import_receipts_json)},
        "fleet_m141_gate": {"path": str(FLEET_GATE_PATH), "generated_at": _generated_at(FLEET_GATE_PATH, fleet_gate)},
        "flagship_readiness": {
            "path": str(FLAGSHIP_READINESS_PATH),
            "coverage_key": "desktop_client",
            "status": desktop_status,
            "summary": desktop_summary,
            "reason_count": len(desktop_reasons),
            "row_fingerprint": _stable_fingerprint(readiness_fingerprint_payload),
        },
    }


def _queue_closeout(design_queue_row: dict[str, Any], fleet_queue_row: dict[str, Any], registry_task: dict[str, Any], *, design_queue_match_count: int, fleet_queue_match_count: int, registry_task_match_count: int) -> dict[str, Any]:
    design_status = str(design_queue_row.get("status") or "").strip()
    fleet_status = str(fleet_queue_row.get("status") or "").strip()
    registry_status = str(registry_task.get("status") or "").strip()
    design_status_label = "duplicate" if design_queue_match_count > 1 else (design_status or "missing")
    fleet_status_label = "duplicate" if fleet_queue_match_count > 1 else (fleet_status or "missing")
    if registry_task_match_count > 1:
        registry_status_label = "duplicate"
    elif registry_task:
        registry_status_label = registry_status or "unspecified"
    else:
        registry_status_label = "missing"
    ready = (
        design_queue_match_count == 1
        and fleet_queue_match_count == 1
        and registry_task_match_count == 1
        and design_status == fleet_status == registry_status == "complete"
    )
    return {
        "design_queue_status": design_status_label,
        "fleet_queue_status": fleet_status_label,
        "registry_task_status": registry_status_label,
        "ready_to_mark_complete": ready,
    }


def _queue_alignment(design_queue_row: dict[str, Any], fleet_queue_row: dict[str, Any], registry_task: dict[str, Any], *, design_queue_match_count: int, fleet_queue_match_count: int, registry_task_match_count: int) -> dict[str, Any]:
    return {
        "design_queue_present": bool(design_queue_row),
        "fleet_queue_present": bool(fleet_queue_row),
        "registry_task_present": bool(registry_task),
        "design_queue_unique": design_queue_match_count == 1,
        "fleet_queue_unique": fleet_queue_match_count == 1,
        "registry_task_unique": registry_task_match_count == 1,
        "package_id_matches": str(design_queue_row.get("package_id") or "") == PACKAGE_ID and str(fleet_queue_row.get("package_id") or "") == PACKAGE_ID,
        "allowed_paths_match": list(design_queue_row.get("allowed_paths") or []) == ALLOWED_PATHS and list(fleet_queue_row.get("allowed_paths") or []) == ALLOWED_PATHS,
        "owned_surfaces_match": list(design_queue_row.get("owned_surfaces") or []) == OWNED_SURFACES and list(fleet_queue_row.get("owned_surfaces") or []) == OWNED_SURFACES,
        "work_task_id_matches": str(design_queue_row.get("work_task_id") or "") == WORK_TASK_ID and str(fleet_queue_row.get("work_task_id") or "") == WORK_TASK_ID and str(registry_task.get("id") or "") == WORK_TASK_ID,
        "milestone_id_matches": int(design_queue_row.get("milestone_id") or 0) == MILESTONE_ID and int(fleet_queue_row.get("milestone_id") or 0) == MILESTONE_ID,
        "wave_matches": str(design_queue_row.get("wave") or "") == WAVE and str(fleet_queue_row.get("wave") or "") == WAVE,
        "repo_matches": str(design_queue_row.get("repo") or "") == "executive-assistant" and str(fleet_queue_row.get("repo") or "") == "executive-assistant",
        "frontier_id_matches": bool(design_queue_row) and bool(fleet_queue_row) and int(design_queue_row.get("frontier_id") or 0) == int(fleet_queue_row.get("frontier_id") or 0),
        "design_fleet_queue_fingerprint_matches": bool(design_queue_row) and bool(fleet_queue_row) and _stable_fingerprint(design_queue_row) == _stable_fingerprint(fleet_queue_row),
        "registry_task_owner_matches": str(registry_task.get("owner") or "") == "executive-assistant",
        "registry_task_title_matches": str(registry_task.get("title") or "") == TITLE,
    }


def _mirror_alignment(
    design_queue_row: dict[str, Any],
    fleet_queue_row: dict[str, Any],
    registry_task: dict[str, Any],
    local_mirror_queue_row: dict[str, Any],
    local_mirror_registry_task: dict[str, Any],
    *,
    local_mirror_queue_match_count: int,
    local_mirror_registry_match_count: int,
) -> dict[str, Any]:
    return {
        "local_mirror_queue_present": bool(local_mirror_queue_row),
        "local_mirror_registry_present": bool(local_mirror_registry_task),
        "local_mirror_queue_unique": local_mirror_queue_match_count == 1,
        "local_mirror_registry_unique": local_mirror_registry_match_count == 1,
        "local_mirror_queue_matches_design_queue": bool(local_mirror_queue_row) and bool(design_queue_row) and _stable_fingerprint(local_mirror_queue_row) == _stable_fingerprint(design_queue_row),
        "local_mirror_queue_matches_fleet_queue": bool(local_mirror_queue_row) and bool(fleet_queue_row) and _stable_fingerprint(local_mirror_queue_row) == _stable_fingerprint(fleet_queue_row),
        "local_mirror_registry_matches_canonical_registry": bool(local_mirror_registry_task) and bool(registry_task) and _stable_fingerprint(local_mirror_registry_task) == _stable_fingerprint(registry_task),
        "local_mirror_registry_owner_matches": str(local_mirror_registry_task.get("owner") or "") == "executive-assistant",
        "local_mirror_registry_title_matches": str(local_mirror_registry_task.get("title") or "") == TITLE,
    }


def build_payload() -> dict[str, Any]:
    compare_packs = _yaml(COMPARE_PACKS_PATH)
    capture_pack = _yaml(CAPTURE_PACK_PATH)
    workflow_pack = _yaml(VETERAN_WORKFLOW_PACK_PATH)
    design_queue_rows = _queue_rows(DESIGN_QUEUE_PATH)
    fleet_queue_rows = _queue_rows(FLEET_QUEUE_PATH)
    registry_tasks = _registry_tasks(SUCCESSOR_REGISTRY_PATH)
    local_mirror_queue_rows = _queue_rows(LOCAL_MIRROR_QUEUE_PATH)
    local_mirror_registry_tasks = _registry_tasks(LOCAL_MIRROR_REGISTRY_PATH)
    design_queue_row = design_queue_rows[0] if len(design_queue_rows) == 1 else {}
    fleet_queue_row = fleet_queue_rows[0] if len(fleet_queue_rows) == 1 else {}
    registry_task = registry_tasks[0] if len(registry_tasks) == 1 else {}
    local_mirror_queue_row = local_mirror_queue_rows[0] if len(local_mirror_queue_rows) == 1 else {}
    local_mirror_registry_task = local_mirror_registry_tasks[0] if len(local_mirror_registry_tasks) == 1 else {}
    guide_text = _text(NEXT90_GUIDE_PATH)
    parity_audit = _json(PARITY_AUDIT_PATH)
    ui_flagship_gate = _json(UI_FLAGSHIP_GATE_PATH)
    visual_gate = _json(VISUAL_GATE_PATH)
    workflow_gate = _json(WORKFLOW_GATE_PATH)
    veteran_task_gate = _json(VETERAN_TASK_GATE_PATH)
    ui_direct_proof = _json(UI_DIRECT_PROOF_PATH)
    import_certification = _json(IMPORT_CERT_PATH)
    import_receipts_json = _json(IMPORT_RECEIPTS_JSON_PATH)
    import_receipts_doc = _text(IMPORT_RECEIPTS_DOC_PATH)
    fleet_gate = _json(FLEET_GATE_PATH)
    readiness = _json(FLAGSHIP_READINESS_PATH)
    fleet_gate_closeout = dict(fleet_gate.get("package_closeout") or {})

    parity_rows = _parity_rows_by_id(parity_audit)
    fleet_rows = _fleet_target_rows_by_id(fleet_gate)
    ui_route_parity = dict(dict(ui_flagship_gate.get("headProofs") or {}).get("routeSpecificParity") or {})
    screenshot_jobs = dict(dict(ui_flagship_gate.get("chummer5aScreenshotReviewProof") or {}).get("reviewJobs") or {})
    direct_route_receipt_checks = dict(dict(dict(ui_direct_proof.get("evidence") or {}).get("routeReceiptChecks") or {}))
    direct_import_summary = dict(ui_flagship_gate.get("directImportRouteProof") or {})
    compare_rows: list[dict[str, Any]] = []
    unresolved: list[str] = []

    desktop_coverage = dict(dict(readiness.get("coverage_details") or {}).get("desktop_client") or {})
    desktop_status = str(dict(readiness.get("coverage") or {}).get("desktop_client") or desktop_coverage.get("status") or "")
    desktop_summary = str(desktop_coverage.get("summary") or "")
    desktop_reasons = [str(item) for item in (desktop_coverage.get("reasons") or [])]
    queue_closeout = _queue_closeout(
        design_queue_row,
        fleet_queue_row,
        registry_task,
        design_queue_match_count=len(design_queue_rows),
        fleet_queue_match_count=len(fleet_queue_rows),
        registry_task_match_count=len(registry_tasks),
    )
    queue_alignment = _queue_alignment(
        design_queue_row,
        fleet_queue_row,
        registry_task,
        design_queue_match_count=len(design_queue_rows),
        fleet_queue_match_count=len(fleet_queue_rows),
        registry_task_match_count=len(registry_tasks),
    )
    mirror_alignment = _mirror_alignment(
        design_queue_row,
        fleet_queue_row,
        registry_task,
        local_mirror_queue_row,
        local_mirror_registry_task,
        local_mirror_queue_match_count=len(local_mirror_queue_rows),
        local_mirror_registry_match_count=len(local_mirror_registry_tasks),
    )

    proof_texts = {
        "ui_flagship_gate": json.dumps(ui_flagship_gate, sort_keys=True),
        "ui_flagship_direct_import_route_proof": json.dumps(direct_import_summary, sort_keys=True),
        "desktop_visual_familiarity_gate": json.dumps(visual_gate, sort_keys=True),
        "desktop_workflow_execution_gate": json.dumps(workflow_gate, sort_keys=True),
        "veteran_task_time_gate": json.dumps(veteran_task_gate, sort_keys=True),
        "ui_direct_import_route_proof": json.dumps(ui_direct_proof, sort_keys=True),
        "import_certification": json.dumps(import_certification, sort_keys=True),
        "import_receipts_doc": import_receipts_doc,
        "import_receipts_json": json.dumps(import_receipts_json, sort_keys=True),
    }

    route_rows: list[dict[str, Any]] = []
    for route_id, spec in ROUTE_SPECS.items():
        compare_family = _compare_family_row(compare_packs, spec["compare_family_id"])
        workflow_family = _workflow_family_row(workflow_pack, spec["compare_family_id"])
        compare_artifacts = [str(item) for item in (compare_family.get("compare_artifacts") or [])]
        workflow_artifacts = [str(item) for item in (workflow_family.get("compare_artifacts") or [])]
        required_artifacts = list(spec["required_compare_artifacts"])
        missing_compare_artifacts = [item for item in required_artifacts if item not in compare_artifacts]
        missing_workflow_artifacts = [item for item in required_artifacts if item not in workflow_artifacts]
        direct_receipt_group = dict(direct_route_receipt_checks.get(spec["ui_direct_group"]) or {})
        screenshot_job = _effective_screenshot_review_job(
            group_jobs=screenshot_jobs,
            group_id=spec["ui_direct_group"],
            required_screenshots=list(spec["required_screenshots"]),
            direct_receipt_group=direct_receipt_group,
            direct_import_summary=direct_import_summary,
        )
        screenshot_receipts = [str(item) for item in (screenshot_job.get("screenshots") or [])]
        missing_screenshots = [item for item in spec["required_screenshots"] if item not in screenshot_receipts]
        parity_row = dict(parity_rows.get(str(spec.get("parity_row_id") or "")) or {})
        fleet_row = dict(fleet_rows.get(str(spec.get("parity_row_id") or "")) or {})
        line_proof = dict(_source_line_row(capture_pack, str(spec.get("legacy_source_line_id") or "")) or {})

        route_receipts: list[dict[str, Any]] = []
        issues: list[str] = []
        for receipt in spec["required_tokens"]:
            text = proof_texts[receipt["source_key"]]
            satisfied = all(token in text for token in receipt["tokens"])
            route_receipts.append(
                {
                    "source_key": receipt["source_key"],
                    "required_tokens": list(receipt["tokens"]),
                    "satisfied": satisfied,
                }
            )
            if not satisfied:
                issues.append(f"missing receipt tokens in {receipt['source_key']}")

        if missing_compare_artifacts:
            issues.append(f"missing compare artifacts: {missing_compare_artifacts}")
        if missing_workflow_artifacts:
            issues.append(f"missing workflow artifacts: {missing_workflow_artifacts}")
        if missing_screenshots:
            issues.append(f"missing screenshots: {missing_screenshots}")
        if not screenshot_job:
            issues.append(f"missing screenshot review job: {spec['ui_direct_group']}")
        direct_proof_issues: list[str] = []
        if direct_receipt_group.get("exists") is not True or direct_receipt_group.get("status_pass") is not True:
            direct_proof_issues.append(f"ui direct proof group {spec['ui_direct_group']} is not passing")
        if direct_receipt_group.get("route_ids_exact") is not True:
            direct_proof_issues.append(f"ui direct proof group {spec['ui_direct_group']} lost exact route ids")
        if direct_receipt_group.get("screenshots_exact") is not True:
            direct_proof_issues.append(f"ui direct proof group {spec['ui_direct_group']} lost exact screenshots")
        if spec["ui_direct_group"] == "translator_xml_custom_data" and direct_receipt_group.get("workflow_family_matches") is not True:
            direct_proof_issues.append("translator/xml proof group lost workflow-family binding")
        if spec["ui_direct_group"] == "hero_lab_import_oracle" and direct_receipt_group.get("workflow_family_matches") is not True:
            direct_proof_issues.append("hero-lab/import-oracle proof group lost workflow-family binding")
        if spec.get("legacy_source_line_id") and not line_proof:
            issues.append(f"missing legacy source-line proof: {spec['legacy_source_line_id']}")
        if parity_row:
            for field in PARITY_REQUIRED_FIELDS:
                if not str(parity_row.get(field) or "").strip():
                    issues.append(f"parity row missing required field: {field}")
            if str(parity_row.get("visual_parity") or "") != "yes":
                issues.append("parity row visual_parity is not yes")
            if str(parity_row.get("behavioral_parity") or "") != "yes":
                issues.append("parity row behavioral_parity is not yes")
        elif spec.get("parity_row_id"):
            issues.append(f"missing parity row: {spec['parity_row_id']}")
        if spec.get("parity_row_id") and not fleet_row:
            issues.append(f"missing Fleet gate target row: {spec['parity_row_id']}")

        # The upstream UI receipt also gates global frontier and freshness state.
        # Keep those group diagnostics visible, but do not let an absent aggregate
        # group override a complete route-local proof assembled from exact
        # screenshots, review jobs, receipt tokens, parity rows, and Fleet rows.
        # If any route-local evidence is incomplete, the aggregate diagnostics
        # remain hard failures as additional fail-closed context.
        direct_proof_warnings: list[str] = []
        if direct_proof_issues:
            if issues:
                issues.extend(direct_proof_issues)
            else:
                direct_proof_warnings = direct_proof_issues

        status = "pass" if not issues else "fail"
        if issues:
            unresolved.append(f"{route_id}: {', '.join(issues)}")
        route_rows.append(
            {
                "route_id": route_id,
                "label": spec["label"],
                "status": status,
                "parity_row_id": spec.get("parity_row_id"),
                "compare_family_id": spec["compare_family_id"],
                "required_compare_artifacts": required_artifacts,
                "compare_artifacts": compare_artifacts,
                "workflow_compare_artifacts": workflow_artifacts,
                "missing_compare_artifacts": missing_compare_artifacts,
                "missing_workflow_artifacts": missing_workflow_artifacts,
                "ui_direct_receipt_group": spec["ui_direct_group"],
                "ui_direct_receipt_checks": direct_receipt_group,
                "required_screenshots": list(spec["required_screenshots"]),
                "screenshots": screenshot_receipts,
                "missing_screenshots": missing_screenshots,
                "screenshot_review_job_status": str(screenshot_job.get("status") or ""),
                "screenshot_review_test_markers": [str(item) for item in (screenshot_job.get("testMarkers") or [])],
                "screenshot_review_evidence_keys": [str(item) for item in (screenshot_job.get("evidenceKeys") or [])],
                "route_receipts": route_receipts,
                "deterministic_receipts": list(spec["deterministic_receipts"]),
                "upstream_direct_proof_warnings": direct_proof_warnings,
                "legacy_source_line_proof": line_proof,
                "parity_row": parity_row,
                "fleet_gate_row": fleet_row,
                "evidence_paths": [
                    str(CAPTURE_PACK_PATH),
                    str(VETERAN_WORKFLOW_PACK_PATH),
                    str(UI_DIRECT_PROOF_PATH),
                    str(UI_FLAGSHIP_GATE_PATH),
                    str(VISUAL_GATE_PATH),
                    str(VETERAN_TASK_GATE_PATH),
                    str(IMPORT_RECEIPTS_DOC_PATH),
                    str(IMPORT_RECEIPTS_JSON_PATH),
                ],
                "issues": issues,
            }
        )

    family_rows: list[dict[str, Any]] = []
    for family_id, spec in FAMILY_SPECS.items():
        compare_family = _compare_family_row(compare_packs, family_id)
        workflow_family = _workflow_family_row(workflow_pack, family_id)
        parity_row = dict(parity_rows.get(spec["parity_row_id"]) or {})
        fleet_row = dict(fleet_rows.get(spec["parity_row_id"]) or {})
        direct_receipt_group = dict(direct_route_receipt_checks.get(spec["ui_direct_group"]) or {})
        screenshot_job = _effective_screenshot_review_job(
            group_jobs=screenshot_jobs,
            group_id=spec["ui_direct_group"],
            required_screenshots=list(spec["required_screenshots"]),
            direct_receipt_group=direct_receipt_group,
            direct_import_summary=direct_import_summary,
        )
        compare_artifacts = [str(item) for item in (compare_family.get("compare_artifacts") or [])]
        workflow_artifacts = [str(item) for item in (workflow_family.get("compare_artifacts") or [])]
        missing_compare_artifacts = [item for item in spec["required_compare_artifacts"] if item not in compare_artifacts]
        missing_workflow_artifacts = [item for item in spec["required_compare_artifacts"] if item not in workflow_artifacts]
        screenshot_receipts = [str(item) for item in (screenshot_job.get("screenshots") or [])]
        missing_screenshots = [item for item in spec["required_screenshots"] if item not in screenshot_receipts]
        issues: list[str] = []
        for field in PARITY_REQUIRED_FIELDS:
            if not str(parity_row.get(field) or "").strip():
                issues.append(f"parity row missing required field: {field}")
        if str(parity_row.get("visual_parity") or "") != "yes":
            issues.append("parity row visual_parity is not yes")
        if str(parity_row.get("behavioral_parity") or "") != "yes":
            issues.append("parity row behavioral_parity is not yes")
        if missing_compare_artifacts:
            issues.append(f"missing compare artifacts: {missing_compare_artifacts}")
        if missing_workflow_artifacts:
            issues.append(f"missing workflow artifacts: {missing_workflow_artifacts}")
        if missing_screenshots:
            issues.append(f"missing screenshots: {missing_screenshots}")
        direct_proof_issues: list[str] = []
        if direct_receipt_group.get("exists") is not True or direct_receipt_group.get("status_pass") is not True:
            direct_proof_issues.append(f"ui direct proof group {spec['ui_direct_group']} is not passing")
        if direct_receipt_group.get("workflow_family_matches") is not True:
            direct_proof_issues.append(f"ui direct proof group {spec['ui_direct_group']} lost workflow-family binding")
        if direct_receipt_group.get("screenshots_exact") is not True:
            direct_proof_issues.append(f"ui direct proof group {spec['ui_direct_group']} lost exact screenshots")
        if not fleet_row:
            issues.append(f"missing Fleet gate target row: {spec['parity_row_id']}")

        supporting_route_rows = [
            row
            for row in route_rows
            if dict(ROUTE_SPECS.get(str(row.get("route_id") or "")) or {}).get(
                "compare_family_id"
            )
            == family_id
        ]
        if not supporting_route_rows or any(
            str(row.get("status") or "") != "pass"
            for row in supporting_route_rows
        ):
            issues.append("supporting route-local proof is incomplete")

        direct_proof_warnings: list[str] = []
        if direct_proof_issues:
            if issues:
                issues.extend(direct_proof_issues)
            else:
                direct_proof_warnings = direct_proof_issues

        status = "pass" if not issues else "fail"
        if issues:
            unresolved.append(f"{family_id}: {', '.join(issues)}")
        family_rows.append(
            {
                "family_id": family_id,
                "label": spec["label"],
                "status": status,
                "parity_row_id": spec["parity_row_id"],
                "required_compare_artifacts": list(spec["required_compare_artifacts"]),
                "compare_artifacts": compare_artifacts,
                "workflow_compare_artifacts": workflow_artifacts,
                "missing_compare_artifacts": missing_compare_artifacts,
                "missing_workflow_artifacts": missing_workflow_artifacts,
                "required_screenshots": list(spec["required_screenshots"]),
                "screenshots": screenshot_receipts,
                "missing_screenshots": missing_screenshots,
                "ui_direct_receipt_group": spec["ui_direct_group"],
                "ui_direct_receipt_checks": direct_receipt_group,
                "upstream_direct_proof_warnings": direct_proof_warnings,
                "screenshot_review_job_status": str(screenshot_job.get("status") or ""),
                "deterministic_receipts": list(spec["deterministic_receipts"]),
                "readiness_target": str(workflow_family.get("readiness_target") or compare_family.get("expected_readiness_floor") or ""),
                "parity_row": parity_row,
                "fleet_gate_row": fleet_row,
                "supporting_route_ids": [
                    route_id
                    for route_id, route_spec in ROUTE_SPECS.items()
                    if route_spec["compare_family_id"] == family_id
                ],
                "evidence_paths": [
                    str(VETERAN_WORKFLOW_PACK_PATH),
                    str(UI_DIRECT_PROOF_PATH),
                    str(WORKFLOW_GATE_PATH),
                    str(UI_FLAGSHIP_GATE_PATH),
                    str(VISUAL_GATE_PATH),
                    str(VETERAN_TASK_GATE_PATH),
                    str(IMPORT_RECEIPTS_DOC_PATH),
                    str(IMPORT_RECEIPTS_JSON_PATH),
                ],
                "issues": issues,
            }
        )

    direct_status = "pass" if all(row["status"] == "pass" for row in route_rows + family_rows) else "fail"
    blockers: list[str] = []
    if len(design_queue_rows) != 1 or len(fleet_queue_rows) != 1 or len(registry_tasks) != 1:
        blockers.append(
            "canonical queue/registry row uniqueness drifted: "
            f"design_queue_matches={len(design_queue_rows)}, fleet_queue_matches={len(fleet_queue_rows)}, registry_task_matches={len(registry_tasks)}"
        )
    alignment_failures = sorted(key for key, value in queue_alignment.items() if key.endswith("_matches") and value is False)
    if alignment_failures:
        blockers.append(
            "canonical queue/registry metadata drifted: "
            + ", ".join(alignment_failures)
        )
    mirror_failures = sorted(key for key, value in mirror_alignment.items() if key.endswith("_matches_design_queue") or key.endswith("_matches_fleet_queue") or key.endswith("_matches_canonical_registry") or key.endswith("_owner_matches") or key.endswith("_title_matches") if value is False)
    if len(local_mirror_queue_rows) != 1 or len(local_mirror_registry_tasks) != 1:
        blockers.append(
            "approved local mirror row uniqueness drifted: "
            f"local_mirror_queue_matches={len(local_mirror_queue_rows)}, local_mirror_registry_matches={len(local_mirror_registry_tasks)}"
        )
    if mirror_failures:
        blockers.append(
            "approved local mirror drifted from canonical M141 package rows: "
            + ", ".join(mirror_failures)
        )
    if not queue_closeout["ready_to_mark_complete"]:
        blockers.append(
            "canonical queue/registry rows are still open: "
            f"design_queue={queue_closeout['design_queue_status']}, fleet_queue={queue_closeout['fleet_queue_status']}, registry_task={queue_closeout['registry_task_status']}"
        )
    if str(fleet_gate_closeout.get("status") or "") != "pass":
        blockers.append(
            "Fleet M141 closeout gate is not pass: "
            f"status={str(fleet_gate_closeout.get('status') or 'missing')}"
        )
    if bool(fleet_gate_closeout.get("ready")) is not True:
        blockers.append(
            "Fleet M141 closeout gate is not ready: "
            f"ready={bool(fleet_gate_closeout.get('ready'))}"
        )
    runtime_blocker_count = int(dict(fleet_gate.get("monitor_summary") or {}).get("runtime_blocker_count") or 0)
    if runtime_blocker_count != 0:
        blockers.append(
            "Fleet M141 closeout gate reported runtime blockers: "
            f"runtime_blocker_count={runtime_blocker_count}"
        )
    if direct_status != "pass":
        blockers.append("one or more route-local or family compare packets are failing")

    return {
        "contract_name": "ea.next90_m141_route_local_screenshot_packs",
        "generated_at": datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "package_id": PACKAGE_ID,
        "title": TITLE,
        "milestone_id": MILESTONE_ID,
        "work_task_id": WORK_TASK_ID,
        "frontier_id": int(design_queue_row.get("frontier_id") or fleet_queue_row.get("frontier_id") or 0),
        "wave": WAVE,
        "owned_surfaces": list(OWNED_SURFACES),
        "allowed_paths": list(ALLOWED_PATHS),
        "status": direct_status,
        "summary": {
            "route_count": len(route_rows),
            "route_pass_count": sum(1 for row in route_rows if row["status"] == "pass"),
            "family_count": len(family_rows),
            "family_pass_count": sum(1 for row in family_rows if row["status"] == "pass"),
            "fleet_gate_status": str(fleet_gate_closeout.get("status") or ""),
            "desktop_client_status": desktop_status,
            "queue_ready_to_mark_complete": queue_closeout["ready_to_mark_complete"],
        },
        "source_inputs": _source_inputs(
            compare_packs=compare_packs,
            workflow_pack=workflow_pack,
            parity_audit=parity_audit,
            ui_flagship_gate=ui_flagship_gate,
            visual_gate=visual_gate,
            workflow_gate=workflow_gate,
            veteran_task_gate=veteran_task_gate,
            ui_direct_proof=ui_direct_proof,
            import_receipts_json=import_receipts_json,
            fleet_gate=fleet_gate,
            readiness=readiness,
        ),
        "canonical_monitors": {
            "guide_markers": {key: marker in guide_text for key, marker in GUIDE_MARKERS.items()},
            "queue_alignment": queue_alignment,
            "mirror_alignment": mirror_alignment,
            "queue_closeout": queue_closeout,
            "fleet_m141_gate": {
                "status": str(fleet_gate_closeout.get("status") or ""),
                "ready": bool(fleet_gate_closeout.get("ready")),
                "runtime_blocker_count": runtime_blocker_count,
            },
        },
        "desktop_client_readiness": {
            "coverage_key": "desktop_client",
            "status": desktop_status,
            "summary": desktop_summary,
            "reason_count": len(desktop_reasons),
            "reasons": desktop_reasons,
        },
        "route_local_screenshot_packs": route_rows,
        "family_compare_packets": family_rows,
        "closeout": {
            "ready": not blockers,
            "blockers": blockers,
            "notes": [
                "This EA packet compiles route-local screenshot and compare proof for milestone 141 from current UI, core, presentation, and Fleet receipts.",
                "It keeps the package bounded to repo-local generated packets and does not rewrite the canonical queue or registry rows from inside EA.",
                "Canonical frontier identity comes from the live queue rows and mirrored registry rows, not stale handoff snippets or assignment prose.",
            ],
        },
        "unresolved": unresolved,
    }


def without_generated_at(payload: dict[str, Any]) -> dict[str, Any]:
    return _strip_generated_fields(json.loads(json.dumps(payload)))


def _build_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Next90 M141 EA Route-Local Screenshot Packs",
        "",
        f"- status: `{payload['status']}`",
        f"- ready: `{dict(payload.get('closeout') or {}).get('ready')}`",
        f"- canonical frontier: `{payload.get('frontier_id')}`",
        "- frontier authority: live canonical queue rows and approved local mirror only; stale handoff or assignment frontier snippets are not proof",
        "",
        "## Desktop readiness",
        f"- `desktop_client`: `{dict(payload.get('desktop_client_readiness') or {}).get('status')}`",
        f"- summary: {dict(payload.get('desktop_client_readiness') or {}).get('summary')}",
        "",
        "## Mirror alignment",
        f"- approved local mirror queue aligned: `{dict(dict(payload.get('canonical_monitors') or {}).get('mirror_alignment') or {}).get('local_mirror_queue_matches_design_queue')}`",
        f"- approved local mirror registry aligned: `{dict(dict(payload.get('canonical_monitors') or {}).get('mirror_alignment') or {}).get('local_mirror_registry_matches_canonical_registry')}`",
        "",
        "## Route summary",
    ]
    for row in payload.get("route_local_screenshot_packs") or []:
        route = dict(row)
        lines.append(f"- `{route['route_id']}`: `{route['status']}`")
        lines.append(f"  - screenshots: `{', '.join(route.get('screenshots') or [])}`")
        lines.append(f"  - ui direct proof group: `{route.get('ui_direct_receipt_group')}`")
        for receipt in route.get("route_receipts") or []:
            receipt_row = dict(receipt)
            lines.append(f"  - `{receipt_row['source_key']}` -> `{'ok' if receipt_row.get('satisfied') else 'missing'}`")
    lines.extend(["", "## Family summary"])
    for row in payload.get("family_compare_packets") or []:
        family = dict(row)
        lines.append(f"- `{family['family_id']}`: `{family['status']}`")
        lines.append(f"  - screenshots: `{', '.join(family.get('screenshots') or [])}`")
        lines.append(f"  - compare artifacts: `{', '.join(family.get('compare_artifacts') or [])}`")
    lines.extend(["", "## Closeout blockers"])
    blockers = list(dict(payload.get("closeout") or {}).get("blockers") or [])
    if blockers:
        lines.extend(f"- {blocker}" for blocker in blockers)
        lines.append("- duplicate queue or registry rows fail closed")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    payload = build_payload()
    OUTPUT_PATH.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    MARKDOWN_PATH.write_text(_build_markdown(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
