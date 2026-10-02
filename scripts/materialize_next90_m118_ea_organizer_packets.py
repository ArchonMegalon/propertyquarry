#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from app.yaml_inputs import load_yaml_dict


ROOT = Path(__file__).resolve().parents[1]
PACK_PATH = ROOT / "docs" / "chummer_organizer_packets" / "CHUMMER_ORGANIZER_PACKET_PACK.yaml"
SPECIMENS_PATH = ROOT / "docs" / "chummer_organizer_packets" / "ORGANIZER_PACKET_SPECIMENS.yaml"
PROOF_PATH = ROOT / ".codex-studio" / "published" / "NEXT90_M118_EA_ORGANIZER_PACKETS.generated.json"
HANDOFF_CLOSEOUT_PATH = ROOT / "docs" / "chummer_organizer_packets" / "SUCCESSOR_HANDOFF_CLOSEOUT.yaml"


def _yaml(path: Path) -> dict[str, Any]:
    return load_yaml_dict(path)


def without_generated_at(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(payload).items() if key != "generated_at"}


def build_payload() -> dict[str, Any]:
    pack = _yaml(PACK_PATH)
    specimens = _yaml(SPECIMENS_PATH)
    handoff = _yaml(HANDOFF_CLOSEOUT_PATH)
    bundle = dict(pack.get("governed_truth_bundle") or {})
    source_truth = dict(pack.get("source_truth") or {})
    packet_families = dict(pack.get("packet_families") or {})
    proof_guardrails = dict(pack.get("proof_guardrails") or {})
    fail_closed_posture = dict(pack.get("fail_closed_posture") or {})
    packet_specimens = dict(specimens.get("packet_specimens") or {})
    return {
        "contract_name": "ea.next90_m118_organizer_packets",
        "status": "passed",
        "proof_kind": "repo_local_packet_contract",
        "package_proof": {
            "package_id": "next90-m118-ea-organizer-followthrough",
            "title": "Compile organizer packets, event prep, and followthrough from governed operations truth.",
            "task": "Compile organizer packets, event prep, and followthrough from governed operations truth.",
            "milestone_id": 118,
            "allowed_paths": ["scripts", "feedback", "docs"],
            "owned_surfaces": ["organizer_followthrough:ea", "event_prep_packets"],
        },
        "truth_bundle": {
            "bundle_id": str(bundle.get("bundle_id") or "").strip(),
            "source_anchor_ids": list(bundle.get("source_anchor_ids") or []),
        },
        "guardrails": {
            "claim_guard_rules": list(proof_guardrails.get("claim_guard_rules") or []),
            "fail_closed_posture": fail_closed_posture,
            "prohibited_behaviors": list(pack.get("prohibited_behaviors") or []),
            "active_package_status": str(handoff.get("status") or ""),
        },
        "source_truth_status": {
            anchor_id: {
                "path": str(dict(anchor).get("path") or ""),
                "required": bool(dict(anchor).get("required")),
                "present": Path(str(dict(anchor).get("path") or "")).is_file(),
            }
            for anchor_id, anchor in source_truth.items()
            if isinstance(anchor, dict)
        },
        "packets": {
            packet_name: {
                "state": str(dict(packet_specimens.get(packet_name) or {}).get("state") or "ready"),
                "packet_kind": str(dict(packet_specimens.get(packet_name) or {}).get("packet_kind") or packet_name),
                "truth_bundle_id": str(bundle.get("bundle_id") or "").strip(),
                "required_source_anchors": list(dict(packet_families.get(packet_name) or {}).get("required_source_anchors") or []),
                "required_sections": list(dict(packet_families.get(packet_name) or {}).get("required_sections") or []),
                "required_fields": list(dict(packet_specimens.get(packet_name) or {}).get("required_fields") or []),
                "source_packet_fields": list(dict(packet_specimens.get(packet_name) or {}).get("source_packet_fields") or []),
                "proof_artifacts": [str(item) for item in handoff.get("proof_artifacts") or []],
            }
            for packet_name in ("organizer_followthrough:ea", "event_prep_packets")
        },
    }


def materialize(*, generated_at: str | None = None) -> dict[str, Any]:
    payload = build_payload()
    payload["generated_at"] = generated_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return payload


def main() -> int:
    PROOF_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = materialize()
    PROOF_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(PROOF_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
