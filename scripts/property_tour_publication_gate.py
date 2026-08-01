#!/usr/bin/env python3
"""Shared fail-closed gate for every public Crezlo bundle publisher."""

from __future__ import annotations

import json
import re
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
        "provider_control_route_verified",
    )
    for key in required_true:
        if acceptance.get(key) is not True:
            return False, f"crezlo_publication_receipt_{key}_missing"
    provenance = _coerce_dict(payload.get("crezlo_source_provenance"))
    if (
        provenance.get("schema") != "propertyquarry.crezlo_source_provenance.v1"
        or str(provenance.get("status") or "").strip().lower() != "pass"
        or str(provenance.get("provider") or "").strip().lower() != "crezlo"
    ):
        return False, "crezlo_source_provenance_missing"
    hosted_url = str(provenance.get("hosted_url") or "").strip()
    if not re.match(r"^https://(?:[a-z0-9-]+\.)*crezlotours\.com/[^/].*$", hosted_url, re.I):
        return False, "crezlo_source_provenance_url_invalid"
    capture = _coerce_dict(provenance.get("capture"))
    try:
        capture_scene_count = int(capture.get("scene_count") or 0)
        capture_space_count = int(capture.get("covered_space_count") or 0)
        capture_hotspot_count = int(capture.get("navigation_hotspot_count") or 0)
    except (TypeError, ValueError):
        return False, "crezlo_source_provenance_capture_invalid"
    if (
        str(capture.get("representation_kind") or "").strip().lower()
        not in {"captured_360", "provider_render"}
        or capture_scene_count < 3
        or capture_space_count < 3
        or capture_hotspot_count < capture_scene_count - 1
        or capture.get("scene_graph_connected") is not True
        or capture.get("all_scenes_reachable") is not True
    ):
        return False, "crezlo_source_provenance_capture_invalid"
    if acceptance.get("floorplan_required") is True:
        for key in (
            "floorplan_alignment_verified",
            "floorplan_layout_receipt_verified",
            "floorplan_geometry_projection_verified",
        ):
            if acceptance.get(key) is not True:
                return False, f"crezlo_publication_receipt_{key}_missing"
        floorplan = _coerce_dict(provenance.get("floorplan"))
        projection = _coerce_dict(floorplan.get("source_geometry_projection"))
        projection_hash = str(projection.get("sha256") or "").strip().lower().removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", projection_hash):
            return False, "crezlo_source_provenance_geometry_projection_missing"
    return True, ""


def require_verified_crezlo_publication(structured: Mapping[str, object] | dict[str, object]) -> None:
    accepted, reason = publication_gate_result(structured)
    if not accepted:
        raise SystemExit(f"crezlo_publication_blocked:{reason}")
