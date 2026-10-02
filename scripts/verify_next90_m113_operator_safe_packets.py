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


PACKAGE_ID = "next90-m113-executive-assistant-operator-safe-packets"
TITLE = "Produce operator-safe GM prep and roster movement packets"
TASK = "Produce operator-safe GM prep and roster movement packets from governed campaign truth."
MILESTONE_ID = 113
FRONTIER_ID = 4554903920
ALLOWED_PATHS = ["scripts", "feedback", "docs"]
OWNED_SURFACES = ["gm_prep_packets", "roster_movement_followthrough"]
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
ROOT = Path(os.environ.get("EA_NEXT90_M113_OPERATOR_SAFE_PACKETS_ROOT", DEFAULT_ROOT))
PACK_PATH = ROOT / "docs" / "chummer_operator_safe_packets" / "CHUMMER_OPERATOR_SAFE_PACKET_PACK.yaml"
SPECIMENS_PATH = ROOT / "docs" / "chummer_operator_safe_packets" / "OPERATOR_SAFE_PACKET_SPECIMENS.yaml"
PROOF_PATH = ROOT / ".codex-studio" / "published" / "NEXT90_M113_OPERATOR_SAFE_PACKETS.generated.json"
MATERIALIZER_PATH = ROOT / "scripts" / "materialize_next90_m113_operator_safe_packets.py"
HANDOFF_CLOSEOUT_PATH = ROOT / "docs" / "chummer_operator_safe_packets" / "SUCCESSOR_HANDOFF_CLOSEOUT.yaml"
FEEDBACK_CLOSEOUT_PATH = ROOT / "feedback" / "2026-04-24-ea-operator-safe-packets-package-closeout.md"
QUEUE_STAGING_PATH = Path("/docker/fleet/.codex-studio/published/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
DESIGN_QUEUE_STAGING_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_QUEUE_STAGING.generated.yaml")
SUCCESSOR_REGISTRY_PATH = Path("/docker/chummercomplete/chummer-design/products/chummer/NEXT_90_DAY_PRODUCT_ADVANCE_REGISTRY.yaml")
LANDED_COMMIT = "38fdba5"
COMPLETION_ACTION = "verify_closed_package_only"
DO_NOT_REOPEN_REASON = (
    "M113 executive-assistant operator-safe packets is complete; future shards must verify the EA packet pack, "
    "generated proof, focused verifier and test, canonical registry row, and queue rows instead of reopening "
    "the GM prep and roster followthrough slice."
)
QUEUE_PROOF = [
    "/docker/EA/docs/chummer_operator_safe_packets/CHUMMER_OPERATOR_SAFE_PACKET_PACK.yaml",
    "/docker/EA/docs/chummer_operator_safe_packets/OPERATOR_SAFE_PACKET_SPECIMENS.yaml",
    "/docker/EA/docs/chummer_operator_safe_packets/README.md",
    "/docker/EA/docs/chummer_operator_safe_packets/SUCCESSOR_HANDOFF_CLOSEOUT.yaml",
    "/docker/EA/scripts/materialize_next90_m113_operator_safe_packets.py",
    "/docker/EA/scripts/verify_next90_m113_operator_safe_packets.py",
    "/docker/EA/tests/test_next90_m113_operator_safe_packets.py",
    "/docker/EA/.codex-studio/published/NEXT90_M113_OPERATOR_SAFE_PACKETS.generated.json",
    "/docker/EA/feedback/2026-04-24-ea-operator-safe-packets-package-closeout.md",
]


def load_yaml(path: Path) -> dict[str, Any]:
    return load_yaml_dict(path)


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8")) or {}
    return dict(payload) if isinstance(payload, dict) else {}


def materializer_module():
    spec = importlib.util.spec_from_file_location("ea_next90_m113_materializer", MATERIALIZER_PATH)
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
            if isinstance(task, dict) and str(task.get("id")) == "113.5":
                return dict(task)
    return None


def main() -> int:
    missing: list[str] = []
    if not PACK_PATH.is_file():
        missing.append(f"missing pack: {PACK_PATH}")
    if not SPECIMENS_PATH.is_file():
        missing.append(f"missing specimens: {SPECIMENS_PATH}")
    if not PROOF_PATH.is_file():
        missing.append(f"missing generated proof: {PROOF_PATH}")
    if not HANDOFF_CLOSEOUT_PATH.is_file():
        missing.append(f"missing handoff closeout: {HANDOFF_CLOSEOUT_PATH}")
    if not FEEDBACK_CLOSEOUT_PATH.is_file():
        missing.append(f"missing feedback closeout: {FEEDBACK_CLOSEOUT_PATH}")
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
        "frontier_id": FRONTIER_ID,
        "milestone_id": MILESTONE_ID,
        "allowed_paths": ALLOWED_PATHS,
        "owned_surfaces": OWNED_SURFACES,
    }:
        missing.append("generated package_proof drifted")
    if handoff.get("package_id") != PACKAGE_ID or handoff.get("status") != "ea_scope_complete":
        missing.append("handoff closeout identity drifted")
    if list(handoff.get("closed_surfaces") or []) != OWNED_SURFACES:
        missing.append("handoff closeout surfaces drifted")
    if list(handoff.get("completed_outputs") or []) != [
        "docs/chummer_operator_safe_packets/CHUMMER_OPERATOR_SAFE_PACKET_PACK.yaml",
        "docs/chummer_operator_safe_packets/OPERATOR_SAFE_PACKET_SPECIMENS.yaml",
        "docs/chummer_operator_safe_packets/README.md",
        "docs/chummer_operator_safe_packets/SUCCESSOR_HANDOFF_CLOSEOUT.yaml",
        "scripts/materialize_next90_m113_operator_safe_packets.py",
        "scripts/verify_next90_m113_operator_safe_packets.py",
        ".codex-studio/published/NEXT90_M113_OPERATOR_SAFE_PACKETS.generated.json",
        "feedback/2026-04-24-ea-operator-safe-packets-package-closeout.md",
    ]:
        missing.append("handoff completed_outputs drifted")

    try:
        module = materializer_module()
        rebuilt = module.build_payload()
        existing = module.without_generated_at(proof)
        if rebuilt != existing:
            missing.append("generated proof drifted from materializer output")
    except Exception as exc:
        missing.append(f"materializer rebuild failed: {exc}")

    packets = proof.get("packets")
    if not isinstance(packets, dict):
        missing.append("generated packets block missing")
    else:
        gm_packet = dict(packets.get("gm_prep_packets") or {})
        roster_packet = dict(packets.get("roster_movement_followthrough") or {})
        if gm_packet.get("state") != "ready":
            missing.append("gm prep packet is not ready")
        if roster_packet.get("state") != "ready":
            missing.append("roster followthrough packet is not ready")

    for queue_path in (QUEUE_STAGING_PATH, DESIGN_QUEUE_STAGING_PATH):
        row = _find_queue_row(queue_path)
        if row is None:
            missing.append(f"queue row missing in {queue_path}")
            continue
        if row.get("status") != "complete":
            missing.append(f"{queue_path}: queue row status drifted")
        if int(row.get("frontier_id") or 0) != FRONTIER_ID:
            missing.append(f"{queue_path}: frontier_id drifted")
        if row.get("landed_commit") != LANDED_COMMIT:
            missing.append(f"{queue_path}: landed_commit drifted")
        if row.get("completion_action") != COMPLETION_ACTION:
            missing.append(f"{queue_path}: completion_action drifted")
        if row.get("do_not_reopen_reason") != DO_NOT_REOPEN_REASON:
            missing.append(f"{queue_path}: do_not_reopen_reason drifted")
        if list(row.get("allowed_paths") or []) != ALLOWED_PATHS:
            missing.append(f"{queue_path}: allowed_paths drifted")
        if list(row.get("owned_surfaces") or []) != OWNED_SURFACES:
            missing.append(f"{queue_path}: owned_surfaces drifted")
        if [str(item) for item in row.get("proof") or []] != QUEUE_PROOF:
            missing.append(f"{queue_path}: proof drifted")

    registry_task = _find_registry_task(SUCCESSOR_REGISTRY_PATH)
    if registry_task is None:
        missing.append("registry work task 113.5 missing")
    else:
        if registry_task.get("status") != "complete":
            missing.append("registry work task status drifted")
        evidence = [str(item) for item in registry_task.get("evidence") or []]
        if not evidence:
            missing.append("registry work task evidence missing")
        if "/docker/EA/docs/chummer_operator_safe_packets/SUCCESSOR_HANDOFF_CLOSEOUT.yaml records the closed EA proof boundary, canonical queue and registry authority, and repeat-prevention rule for future shards." not in evidence:
            missing.append("registry work task handoff evidence missing")

    encoded = json.dumps(
        {
            "pack": {key: value for key, value in pack.items() if key != "proof_guardrails"},
            "specimens": specimens,
            "proof": {key: value for key, value in proof.items() if key != "guardrails"},
            "handoff": {key: value for key, value in handoff.items() if key != "runtime_safety_posture"},
        },
        sort_keys=True,
    ).lower()
    for marker in FORBIDDEN_PROOF_MARKERS:
        if marker.lower() in encoded:
            missing.append(f"forbidden proof marker present: {marker}")

    if missing:
        print("\n".join(missing), file=sys.stderr)
        return 1

    print("ok: next90 m113 operator-safe packet contract")
    print(f"ok: {PROOF_PATH}")
    print(f"ok: {HANDOFF_CLOSEOUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
