#!/usr/bin/env python3
"""Shared fail-closed gate for every public Crezlo bundle publisher."""

from __future__ import annotations

import json
from typing import Mapping


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def publication_gate_result(structured: Mapping[str, object] | dict[str, object]) -> tuple[bool, str]:
    """Return whether a Crezlo output has the complete publishable receipt.

    The adapter's immersive acceptance is the authoritative proof.  These
    additional checks deliberately repeat the high-value invariants so a
    legacy bulk publisher cannot accidentally promote a payload that merely
    contains a vendor URL or a forged ``accepted`` flag.
    """

    payload = dict(structured or {})
    acceptance = _coerce_dict(payload.get("immersive_acceptance_json"))
    if acceptance.get("accepted") is not True:
        return False, str(acceptance.get("reason") or "crezlo_immersive_evidence_missing").strip()
    required_true = (
        "spatial_provenance_verified",
        "exact_property_provenance_verified",
        "browser_receipt_verified",
        "scene_graph_connected",
        "all_required_scenes_navigable",
        "first_party_viewer_verified",
    )
    for key in required_true:
        if acceptance.get(key) is not True:
            return False, f"crezlo_publication_receipt_{key}_missing"
    if acceptance.get("floorplan_required") is True:
        for key in ("floorplan_alignment_verified", "floorplan_layout_receipt_verified"):
            if acceptance.get(key) is not True:
                return False, f"crezlo_publication_receipt_{key}_missing"
    return True, ""


def require_verified_crezlo_publication(structured: Mapping[str, object] | dict[str, object]) -> None:
    accepted, reason = publication_gate_result(structured)
    if not accepted:
        raise SystemExit(f"crezlo_publication_blocked:{reason}")

