#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "ea") not in sys.path:
    sys.path.insert(0, str(ROOT / "ea"))

from app.yaml_inputs import load_yaml_dict

DOCS_ROOT = ROOT / "docs" / "chummer5a_parity_lab"
PACK_PATH = DOCS_ROOT / "NEXT90_M143_ROUTE_SPECIFIC_COMPARE_PACKS.generated.yaml"
MARKDOWN_PATH = DOCS_ROOT / "NEXT90_M143_ROUTE_SPECIFIC_COMPARE_PACKS.generated.md"
FEEDBACK_PATH = ROOT / "feedback" / "2026-05-05-next90-m143-ea-route-specific-compare-packs.md"

PACKAGE_ID = "next90-m143-ea-compile-route-specific-compare-packs-and-artifact-proofs-for-print-export"
TITLE = "Compile route-specific compare packs and artifact proofs for print, export, exchange, SR6 supplement, and house-rule workflows."
WORK_TASK_ID = "143.5"
FRONTIER_ID = 5326878760
WAVE = "W22P"
EXPECTED_FAMILIES = {
    "sheet_export_print_viewer_and_exchange",
    "sr6_supplements_designers_and_house_rules",
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


def main() -> int:
    issues: list[str] = []
    for path in (PACK_PATH, MARKDOWN_PATH, FEEDBACK_PATH):
        if not path.is_file():
            issues.append(f"missing required file: {path}")
    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1

    payload = _yaml(PACK_PATH)
    if payload.get("package_id") != PACKAGE_ID:
        issues.append("package_id drifted")
    if payload.get("title") != TITLE:
        issues.append("title drifted")
    if payload.get("work_task_id") != WORK_TASK_ID:
        issues.append("work_task_id drifted")
    if int(payload.get("frontier_id") or 0) != FRONTIER_ID:
        issues.append("frontier_id drifted")
    if payload.get("wave") != WAVE:
        issues.append("wave drifted")
    if list(payload.get("allowed_paths") or []) != ["scripts", "feedback", "docs"]:
        issues.append("allowed_paths drifted")
    if list(payload.get("owned_surfaces") or []) != ["compile_route_specific_compare_packs_and_artifact_proofs:ea"]:
        issues.append("owned_surfaces drifted")
    source_inputs = dict(payload.get("source_inputs") or {})
    for source_key, expected_path in (
        ("design_queue", "/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_QUEUE_STAGING.generated.yaml"),
        ("fleet_queue", "/docker/fleet/.codex-studio/published/NEXT_90_DAY_QUEUE_STAGING.generated.yaml"),
        ("local_mirror_queue", "/docker/EA/.codex-design/product/NEXT_90_DAY_QUEUE_STAGING.generated.yaml"),
        ("registry", "/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml"),
        ("local_mirror_registry", "/docker/EA/.codex-design/product/NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml"),
        ("fleet_m143_gate", "/docker/fleet/.codex-studio/published/NEXT90_M143_FLEET_ROUTE_LOCAL_OUTPUT_CLOSEOUT_GATES.generated.json"),
    ):
        if dict(source_inputs.get(source_key) or {}).get("path") != expected_path:
            issues.append(f"{source_key} source path drifted")
    for source_key in ("design_queue", "fleet_queue", "local_mirror_queue"):
        source_row = dict(source_inputs.get(source_key) or {})
        if int(source_row.get("match_count") or 0) != 1:
            issues.append(f"{source_key} match_count drifted")
        if source_row.get("unique_match") is not True:
            issues.append(f"{source_key} should have exactly one canonical row")
        if source_row.get("status") != "not_started":
            issues.append(f"{source_key} status drifted")
        if int(source_row.get("frontier_id") or 0) != FRONTIER_ID:
            issues.append(f"{source_key} frontier drifted")
        if not str(source_row.get("row_fingerprint") or "").strip():
            issues.append(f"{source_key} row_fingerprint missing")
    for source_key in ("registry", "local_mirror_registry"):
        source_row = dict(source_inputs.get(source_key) or {})
        if int(source_row.get("match_count") or 0) != 1:
            issues.append(f"{source_key} match_count drifted")
        if source_row.get("unique_match") is not True:
            issues.append(f"{source_key} should have exactly one registry task row")
        if source_row.get("owner") != "executive-assistant":
            issues.append(f"{source_key} owner drifted")
        if not str(source_row.get("row_fingerprint") or "").strip():
            issues.append(f"{source_key} row_fingerprint missing")
    readiness_input = dict(source_inputs.get("flagship_readiness") or {})
    if readiness_input.get("path") != "/docker/fleet/.codex-studio/published/FLAGSHIP_PRODUCT_READINESS.generated.json":
        issues.append("flagship_readiness source path drifted")
    if readiness_input.get("coverage_key") != "desktop_client":
        issues.append("flagship_readiness coverage key drifted")
    if readiness_input.get("status") != dict(payload.get("desktop_client_readiness") or {}).get("status"):
        issues.append("flagship_readiness status drifted")
    if int(readiness_input.get("reason_count") or 0) != len(list(dict(payload.get("desktop_client_readiness") or {}).get("reasons") or [])):
        issues.append("flagship_readiness reason_count drifted")
    if list(readiness_input.get("row_fingerprint_basis") or []) != ["status", "summary", "reasons", "evidence"]:
        issues.append("flagship_readiness row_fingerprint_basis drifted")
    if not str(readiness_input.get("row_fingerprint") or "").strip():
        issues.append("flagship_readiness row_fingerprint missing")

    summary = dict(payload.get("summary") or {})
    if summary.get("fleet_m143_gate_status") != "pass":
        issues.append("fleet_m143_gate_status must stay pass")
    if summary.get("fleet_m143_closeout_status") != "pass":
        issues.append("fleet_m143_closeout_status must stay pass")

    canonical_monitors = dict(payload.get("canonical_monitors") or {})
    queue_alignment = dict(canonical_monitors.get("queue_alignment") or {})
    if not queue_alignment:
        issues.append("queue_alignment monitor missing")
    else:
        expected_true_checks = (
            "design_queue_unique",
            "fleet_queue_unique",
            "local_mirror_queue_unique",
            "registry_task_unique",
            "local_mirror_registry_task_unique",
            "design_fleet_queue_fingerprint_matches",
            "design_local_mirror_queue_fingerprint_matches",
            "registry_task_owner_matches",
            "registry_task_title_matches",
            "local_mirror_queue_owner_matches",
            "local_mirror_queue_frontier_matches",
            "local_mirror_queue_allowed_paths_match",
            "local_mirror_queue_owned_surfaces_match",
            "local_mirror_registry_task_owner_matches",
            "local_mirror_registry_task_title_matches",
            "registry_local_mirror_task_fingerprint_matches",
        )
        for key in expected_true_checks:
            if queue_alignment.get(key) is not True:
                issues.append(f"queue_alignment {key} drifted")
    fleet_gate = dict(canonical_monitors.get("fleet_gate") or {})
    if not fleet_gate:
        issues.append("fleet_gate monitor missing")
    elif not all(bool(value) for value in fleet_gate.values()):
        issues.append("fleet_gate monitor drifted")

    desktop_readiness = dict(payload.get("desktop_client_readiness") or {})
    if desktop_readiness.get("coverage_key") != "desktop_client":
        issues.append("desktop_client_readiness coverage key drifted")
    if not str(desktop_readiness.get("status") or "").strip():
        issues.append("desktop_client_readiness status missing")
    if not str(desktop_readiness.get("summary") or "").strip():
        issues.append("desktop_client_readiness summary missing")
    if int(desktop_readiness.get("reason_count") or 0) != len(list(desktop_readiness.get("reasons") or [])):
        issues.append("desktop_client_readiness reason_count drifted")

    rows = [dict(row) for row in (payload.get("family_route_compare_packs") or [])]
    family_ids = {str(row.get("family_id") or "") for row in rows}
    if family_ids != EXPECTED_FAMILIES:
        issues.append(f"family ids drifted: {sorted(family_ids)}")
    unresolved_families: list[str] = []
    for row in rows:
        if not list(row.get("evidence_paths") or []):
            issues.append(f"{row.get('family_id')}: evidence_paths missing")
        route_receipts = [dict(receipt) for receipt in (row.get("route_receipts") or [])]
        if not route_receipts:
            issues.append(f"{row.get('family_id')}: route_receipts missing")
        dependency = dict(row.get("desktop_client_dependency") or {})
        if dependency.get("coverage_key") != "desktop_client":
            issues.append(f"{row.get('family_id')}: desktop_client_dependency coverage key drifted")
        if dependency.get("coverage_status") != desktop_readiness.get("status"):
            issues.append(f"{row.get('family_id')}: desktop_client_dependency status drifted")
        if row.get("issues"):
            unresolved_families.append(str(row.get("family_id") or ""))
        for receipt in route_receipts:
            if not receipt.get("required_tokens"):
                issues.append(f"{row.get('family_id')}::{receipt.get('route_id')}: required_tokens missing")

    closeout = dict(payload.get("closeout") or {})
    blockers = [str(item) for item in (closeout.get("blockers") or [])]
    if not blockers:
        issues.append("closeout blockers should stay explicit until canonical queue rows are complete")
    if desktop_readiness.get("status") != "ready" and not any("desktop_client" in blocker for blocker in blockers):
        issues.append("closeout blockers must mention the live desktop_client gap while readiness is not ready")
    for family_id in unresolved_families:
        if not any(family_id in blocker for blocker in blockers):
            issues.append(f"closeout blockers must mention the live route-proof gap for {family_id}")

    feedback_text = FEEDBACK_PATH.read_text(encoding="utf-8")
    if "desktop_client" not in feedback_text:
        issues.append("feedback note must mention desktop_client readiness posture")
    if "canonical queue frontier" not in feedback_text:
        issues.append("feedback note must mention canonical queue frontier alignment")
    if ".codex-design local mirror" not in feedback_text:
        issues.append("feedback note must mention local mirror alignment")
    if "duplicate queue or registry rows fail closed" not in feedback_text:
        issues.append("feedback note must mention duplicate canonical row fail-closed posture")
    for forbidden in FORBIDDEN_PROOF_MARKERS:
        if forbidden.lower() in feedback_text.lower():
            issues.append(f"feedback note cites forbidden helper evidence: {forbidden}")

    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1

    print("ok: next90 m143 ea route-specific compare packs")
    print(f"ok: {PACK_PATH}")
    print(f"ok: {MARKDOWN_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
