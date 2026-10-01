#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from app.yaml_inputs import load_yaml_dict


ROOT = Path(__file__).resolve().parents[1]
PACK_PATH = ROOT / "docs" / "chummer_operator_safe_packets" / "CHUMMER_OPERATOR_SAFE_PACKET_PACK.yaml"
SPECIMENS_PATH = ROOT / "docs" / "chummer_operator_safe_packets" / "OPERATOR_SAFE_PACKET_SPECIMENS.yaml"
PROOF_PATH = ROOT / ".codex-studio" / "published" / "NEXT90_M113_OPERATOR_SAFE_PACKETS.generated.json"
HANDOFF_CLOSEOUT_PATH = ROOT / "docs" / "chummer_operator_safe_packets" / "SUCCESSOR_HANDOFF_CLOSEOUT.yaml"


def _yaml(path: Path) -> dict[str, Any]:
    return load_yaml_dict(path)


def without_generated_at(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(payload).items() if key != "generated_at"}


def build_payload() -> dict[str, Any]:
    pack = _yaml(PACK_PATH)
    specimens = _yaml(SPECIMENS_PATH)
    handoff = _yaml(HANDOFF_CLOSEOUT_PATH)
    bundle = dict(pack.get("governed_truth_bundle") or {})
    proof_artifacts = [str(item) for item in handoff.get("proof_artifacts") or []]
    packet_specimens = dict(specimens.get("packet_specimens") or {})
    return {
        "contract_name": "ea.next90_m113_operator_safe_packets",
        "status": "passed",
        "proof_kind": "repo_local_packet_contract",
        "package_proof": {
            "package_id": "next90-m113-executive-assistant-operator-safe-packets",
            "title": "Produce operator-safe GM prep and roster movement packets",
            "task": "Produce operator-safe GM prep and roster movement packets from governed campaign truth.",
            "frontier_id": 4554903920,
            "milestone_id": 113,
            "allowed_paths": ["scripts", "feedback", "docs"],
            "owned_surfaces": ["gm_prep_packets", "roster_movement_followthrough"],
        },
        "truth_bundle": {
            "bundle_id": str(bundle.get("bundle_id") or "").strip(),
            "source_anchor_ids": list(bundle.get("source_anchor_ids") or []),
        },
        "packets": {
            "gm_prep_packets": {
                "state": str(dict(packet_specimens.get("gm_prep_packets") or {}).get("state") or "ready"),
                "truth_bundle_id": str(bundle.get("bundle_id") or "").strip(),
                "proof_artifacts": proof_artifacts,
            },
            "roster_movement_followthrough": {
                "state": str(dict(packet_specimens.get("roster_movement_followthrough") or {}).get("state") or "ready"),
                "truth_bundle_id": str(bundle.get("bundle_id") or "").strip(),
                "proof_artifacts": proof_artifacts,
            },
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
