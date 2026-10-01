#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from app.yaml_inputs import load_yaml_dict


PACKAGE_ID = "next90-m118-ea-organizer-followthrough"
TITLE = "Compile organizer packets, event prep, and followthrough from governed operations truth."
TASK = "Compile organizer packets, event prep, and followthrough from governed operations truth."
MILESTONE_ID = 118
WORK_TASK_ID = "118.3"
ALLOWED_PATHS = ["scripts", "feedback", "docs"]
OWNED_SURFACES = ["organizer_followthrough:ea", "event_prep_packets"]
WAVE = "W13"
REPO = "executive-assistant"
SOURCE_ANCHOR_IDS = [
    "community_scale_audit_schema",
    "organizer_boundary_policy",
    "hub_organizer_ops_verifier",
    "hub_creator_publication_verifier",
    "fleet_weekly_governor_packet",
    "fleet_support_case_packets",
    "ea_operator_safe_baseline",
]
PACKET_FAMILY_DETAILS = {
    "organizer_followthrough:ea": {
        "required_sections": [
            "packet_identity",
            "source_packet_links",
            "event_or_roster_summary",
            "publication_posture",
            "support_followthrough_gate",
            "next_safe_action",
        ],
        "required_fields": [
            "packet_id",
            "truth_bundle_id",
            "source_packet_ids",
            "operation_family",
            "event_or_roster_summary",
            "publication_posture",
            "support_followthrough_gate",
            "next_safe_action",
        ],
        "source_packet_fields": [
            "CommunityScaleAuditPacket.packet_id",
            "CommunityScaleAuditPacket.operation_family",
            "CommunityScaleAuditPacket.support_case_ref",
            "SUPPORT_CASE_PACKETS.generated.json.followthrough_receipt_gates.required_gates",
            "SUPPORT_CASE_PACKETS.generated.json.packets[].packet_id",
        ],
    },
    "event_prep_packets": {
        "required_sections": [
            "packet_identity",
            "prep_scope",
            "dependency_window",
            "publication_readiness",
            "blocker_holds",
            "handoff_receipts",
        ],
        "required_fields": [
            "packet_id",
            "truth_bundle_id",
            "source_packet_ids",
            "prep_scope",
            "dependency_window",
            "publication_readiness",
            "blocker_holds",
            "handoff_receipts",
        ],
        "source_packet_fields": [
            "CommunityScaleAuditPacket.packet_id",
            "WEEKLY_GOVERNOR_PACKET.generated.json.decision_alignment.actual_action",
            "WEEKLY_GOVERNOR_PACKET.generated.json.public_status_copy.state",
            "SUPPORT_CASE_PACKETS.generated.json.summary.needs_human_response",
            "CHUMMER_OPERATOR_SAFE_PACKET_PACK.yaml.packet_families.gm_prep_packets.state",
        ],
    },
}
PROOF_ARTIFACTS = [
    "docs/chummer_organizer_packets/CHUMMER_ORGANIZER_PACKET_PACK.yaml",
    "docs/chummer_organizer_packets/ORGANIZER_PACKET_SPECIMENS.yaml",
    "docs/chummer_organizer_packets/README.md",
    "docs/chummer_organizer_packets/SUCCESSOR_HANDOFF_CLOSEOUT.yaml",
    "scripts/materialize_next90_m118_ea_organizer_packets.py",
    "scripts/verify_next90_m118_ea_organizer_packets.py",
    "tests/test_next90_m118_ea_organizer_packets.py",
    ".codex-studio/published/NEXT90_M118_EA_ORGANIZER_PACKETS.generated.json",
    "feedback/2026-05-05-next90-m118-ea-organizer-followthrough-progress.md",
]
FORBIDDEN_PROOF_MARKERS = [
    "TASK_LOCAL_TELEMETRY",
    "ACTIVE_RUN_HANDOFF",
    "/var/lib/codex-fleet",
    "supervisor status",
    "supervisor eta",
    "operator telemetry",
    "codexea status",
    "codexea eta",
]

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("EA_NEXT90_M118_ORGANIZER_PACKETS_ROOT", DEFAULT_ROOT))
PACK_PATH = ROOT / "docs" / "chummer_organizer_packets" / "CHUMMER_ORGANIZER_PACKET_PACK.yaml"
SPECIMENS_PATH = ROOT / "docs" / "chummer_organizer_packets" / "ORGANIZER_PACKET_SPECIMENS.yaml"
PROOF_PATH = ROOT / ".codex-studio" / "published" / "NEXT90_M118_EA_ORGANIZER_PACKETS.generated.json"
MATERIALIZER_PATH = ROOT / "scripts" / "materialize_next90_m118_ea_organizer_packets.py"
HANDOFF_CLOSEOUT_PATH = ROOT / "docs" / "chummer_organizer_packets" / "SUCCESSOR_HANDOFF_CLOSEOUT.yaml"
FEEDBACK_PROGRESS_PATH = ROOT / "feedback" / "2026-05-05-next90-m118-ea-organizer-followthrough-progress.md"
QUEUE_STAGING_PATH = Path("/docker/fleet/.codex-studio/published/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
DESIGN_QUEUE_STAGING_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
SUCCESSOR_REGISTRY_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml")


def load_yaml(path: Path) -> dict[str, Any]:
    return load_yaml_dict(path)


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8")) or {}
    return dict(payload) if isinstance(payload, dict) else {}


def materializer_module():
    spec = importlib.util.spec_from_file_location("ea_next90_m118_materializer", MATERIALIZER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load materializer from {MATERIALIZER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _find_queue_row(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    marker = f"package_id: {PACKAGE_ID}"
    start = text.find(marker)
    if start == -1:
        return None
    block_start = text.rfind("- title:", 0, start)
    if block_start == -1:
        return None
    next_start = text.find("\n- title:", start)
    block = text[block_start:] if next_start == -1 else text[block_start:next_start]
    payload = yaml.safe_load(block) or []
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return dict(payload[0])
    return None


def _find_registry_task(path: Path) -> dict[str, Any] | None:
    payload = load_yaml(path)
    for milestone in payload.get("milestones") or []:
        if not isinstance(milestone, dict) or int(milestone.get("id") or 0) != MILESTONE_ID:
            continue
        for task in milestone.get("work_tasks") or []:
            if isinstance(task, dict) and str(task.get("id")) == WORK_TASK_ID:
                return dict(task)
    return None


def main() -> int:
    missing: list[str] = []
    for path, label in (
        (PACK_PATH, "pack"),
        (SPECIMENS_PATH, "specimens"),
        (PROOF_PATH, "generated proof"),
        (HANDOFF_CLOSEOUT_PATH, "handoff"),
        (FEEDBACK_PROGRESS_PATH, "feedback progress"),
    ):
        if not path.is_file():
            missing.append(f"missing {label}: {path}")
    if missing:
        print("\n".join(missing), file=sys.stderr)
        return 1

    pack = load_yaml(PACK_PATH)
    specimens = load_yaml(SPECIMENS_PATH)
    proof = load_json(PROOF_PATH)
    handoff = load_yaml(HANDOFF_CLOSEOUT_PATH)

    if pack.get("package_id") != PACKAGE_ID:
        missing.append("pack package_id drifted")
    if specimens.get("package_id") != PACKAGE_ID:
        missing.append("specimens package_id drifted")
    if int(pack.get("milestone_id") or 0) != MILESTONE_ID:
        missing.append("pack milestone drifted")
    if list(pack.get("owned_surfaces") or []) != OWNED_SURFACES:
        missing.append("pack owned_surfaces drifted")
    if proof.get("package_proof") != {
        "package_id": PACKAGE_ID,
        "title": TITLE,
        "task": TASK,
        "milestone_id": MILESTONE_ID,
        "allowed_paths": ALLOWED_PATHS,
        "owned_surfaces": OWNED_SURFACES,
    }:
        missing.append("generated package_proof drifted")
    if handoff.get("package_id") != PACKAGE_ID or handoff.get("status") != "active_package_proven":
        missing.append("handoff identity drifted")
    if list(handoff.get("active_surfaces") or []) != OWNED_SURFACES:
        missing.append("handoff active_surfaces drifted")
    if list(handoff.get("completed_outputs") or []) != PROOF_ARTIFACTS:
        missing.append("handoff completed_outputs drifted")
    if [str(item) for item in handoff.get("proof_artifacts") or []] != PROOF_ARTIFACTS:
        missing.append("handoff proof_artifacts drifted")

    governed_bundle = dict(pack.get("governed_truth_bundle") or {})
    if str(governed_bundle.get("bundle_id") or "").strip() != "ea-m118-organizer-followthrough-v1":
        missing.append("governed truth bundle drifted")
    if list(governed_bundle.get("source_anchor_ids") or []) != SOURCE_ANCHOR_IDS:
        missing.append("governed truth source_anchor_ids drifted")
    for section in ("source_truth", "proof_guardrails", "packet_families"):
        if not isinstance(pack.get(section), dict):
            missing.append(f"{section} section missing")

    source_truth = dict(pack.get("source_truth") or {})
    if list(source_truth.keys()) != SOURCE_ANCHOR_IDS:
        missing.append("source_truth anchors drifted")
    for anchor_id in SOURCE_ANCHOR_IDS:
        anchor = dict(source_truth.get(anchor_id) or {})
        path_value = str(anchor.get("path") or "").strip()
        if not path_value:
            missing.append(f"source truth missing path for {anchor_id}")
            continue
        if not Path(path_value).is_file():
            missing.append(f"source truth path missing for {anchor_id}: {path_value}")

    try:
        module = materializer_module()
        rebuilt = module.build_payload()
        existing = module.without_generated_at(proof)
        if rebuilt != existing:
            missing.append("generated proof drifted from materializer output")
    except Exception as exc:
        missing.append(f"materializer rebuild failed: {exc}")

    packets = dict(proof.get("packets") or {})
    if dict(packets.get("organizer_followthrough:ea") or {}).get("state") != "ready":
        missing.append("organizer followthrough packet is not ready")
    if dict(packets.get("event_prep_packets") or {}).get("state") != "ready":
        missing.append("event prep packet is not ready")
    for packet_name in OWNED_SURFACES:
        packet = dict(packets.get(packet_name) or {})
        expected = PACKET_FAMILY_DETAILS[packet_name]
        if [str(item) for item in packet.get("proof_artifacts") or []] != PROOF_ARTIFACTS:
            missing.append(f"{packet_name} proof_artifacts drifted")
        if packet.get("packet_kind") != packet_name:
            missing.append(f"{packet_name} packet_kind drifted")
        if list(packet.get("required_source_anchors") or []) != list(dict(pack.get("packet_families") or {}).get(packet_name, {}).get("required_source_anchors") or []):
            missing.append(f"{packet_name} required_source_anchors drifted")
        if list(packet.get("required_sections") or []) != expected["required_sections"]:
            missing.append(f"{packet_name} required_sections drifted")
        if list(packet.get("required_fields") or []) != expected["required_fields"]:
            missing.append(f"{packet_name} required_fields drifted")
        if list(packet.get("source_packet_fields") or []) != expected["source_packet_fields"]:
            missing.append(f"{packet_name} source_packet_fields drifted")

    truth_bundle = dict(proof.get("truth_bundle") or {})
    if list(truth_bundle.get("source_anchor_ids") or []) != SOURCE_ANCHOR_IDS:
        missing.append("generated truth_bundle source_anchor_ids drifted")
    guardrails = dict(proof.get("guardrails") or {})
    if list(guardrails.get("claim_guard_rules") or []) != list(dict(pack.get("proof_guardrails") or {}).get("claim_guard_rules") or []):
        missing.append("generated claim_guard_rules drifted")
    if dict(guardrails.get("fail_closed_posture") or {}) != dict(pack.get("fail_closed_posture") or {}):
        missing.append("generated fail_closed_posture drifted")
    if list(guardrails.get("prohibited_behaviors") or []) != list(pack.get("prohibited_behaviors") or []):
        missing.append("generated prohibited_behaviors drifted")
    if guardrails.get("active_package_status") != "active_package_proven":
        missing.append("generated active_package_status drifted")

    shared_truth_sources = handoff.get("shared_truth_runtime_sources") or []
    if [str(dict(item).get("source_anchor_id") or "") for item in shared_truth_sources] != SOURCE_ANCHOR_IDS:
        missing.append("handoff shared_truth_runtime_sources drifted")

    for queue_path in (QUEUE_STAGING_PATH, DESIGN_QUEUE_STAGING_PATH):
        row = _find_queue_row(queue_path)
        if row is None:
            missing.append(f"queue row missing in {queue_path}")
            continue
        if row.get("title") != TITLE:
            missing.append(f"{queue_path}: title drifted")
        if row.get("task") != TASK:
            missing.append(f"{queue_path}: task drifted")
        if str(row.get("work_task_id") or "") != WORK_TASK_ID:
            missing.append(f"{queue_path}: work_task_id drifted")
        if int(row.get("milestone_id") or 0) != MILESTONE_ID:
            missing.append(f"{queue_path}: milestone_id drifted")
        if row.get("status") != "complete":
            missing.append(f"{queue_path}: queue row status drifted")
        if row.get("wave") != WAVE:
            missing.append(f"{queue_path}: wave drifted")
        if row.get("repo") != REPO:
            missing.append(f"{queue_path}: repo drifted")
        if list(row.get("allowed_paths") or []) != ALLOWED_PATHS:
            missing.append(f"{queue_path}: allowed_paths drifted")
        if list(row.get("owned_surfaces") or []) != OWNED_SURFACES:
            missing.append(f"{queue_path}: owned_surfaces drifted")

    registry_task = _find_registry_task(SUCCESSOR_REGISTRY_PATH)
    if registry_task is None:
        missing.append("registry work task 118.3 missing")
    else:
        if registry_task.get("owner") != "executive-assistant":
            missing.append("registry work task owner drifted")
        if registry_task.get("title") != "Compile organizer packets, event prep, and followthrough from governed operations truth.":
            missing.append("registry work task title drifted")

    authority = dict(handoff.get("canonical_authority") or {})
    if authority.get("queue_package") != "next90-m118-ea-organizer-followthrough status=complete":
        missing.append("handoff canonical queue package drifted")
    if authority.get("registry_work_task") != "118.3 status=complete owner=executive-assistant":
        missing.append("handoff canonical registry work task drifted")
    if [str(item) for item in authority.get("queue_proof_required_entries") or []] != [
        f"/docker/EA/{entry}" if not entry.startswith(".codex-studio") else f"/docker/EA/{entry}"
        for entry in PROOF_ARTIFACTS
    ] + [
        "python3 scripts/materialize_next90_m118_ea_organizer_packets.py exits 0",
        "python3 scripts/verify_next90_m118_ea_organizer_packets.py exits 0",
        "python3 tests/test_next90_m118_ea_organizer_packets.py exits 0 with ran=6 failed=0",
    ]:
        missing.append("handoff queue_proof_required_entries drifted")

    feedback_text = FEEDBACK_PROGRESS_PATH.read_text(encoding="utf-8").lower()
    encoded = json.dumps({key: value for key, value in proof.items() if key != "guardrails"}, sort_keys=True).lower()
    for marker in FORBIDDEN_PROOF_MARKERS:
        if marker.lower() in encoded:
            missing.append(f"forbidden proof marker present: {marker}")
        if marker.lower() in feedback_text:
            missing.append(f"forbidden feedback marker present: {marker}")

    if missing:
        print("\n".join(missing), file=sys.stderr)
        return 1

    print("ok: next90 m118 ea organizer packet contract")
    print(f"ok: {PROOF_PATH}")
    print(f"ok: {HANDOFF_CLOSEOUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
