#!/usr/bin/env python3
"""Import a reviewed Crezlo-hosted tour without inventing a local reconstruction.

The provider URL is kept in ``tour.private.json`` and is exposed only through the
receipt-backed first-party ``/control/crezlo`` route.  A URL alone is never enough:
the receipt must bind the exact property, measured floorplan contract, scene graph,
and real-browser interaction proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .property_tour_governed_reservation import require_dynamic_tour_slug
    from .property_tour_layout_contract import load_layout_contract
else:
    from property_tour_governed_reservation import require_dynamic_tour_slug
    from property_tour_layout_contract import load_layout_contract


SCHEMA = "propertyquarry.crezlo_source_provenance.v1"
CREZLO_HOST_SUFFIX = ".crezlotours.com"
PRIVATE_MODE = 0o600


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_slug(value: object) -> str:
    raw = str(value or "").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        return ""
    return raw


def _safe_crezlo_url(value: object) -> str:
    from urllib.parse import urlparse

    normalized = str(value or "").strip()
    if len(normalized) > 2048 or not normalized.startswith("https://"):
        return ""
    parsed = urlparse(normalized)
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if host != CREZLO_HOST_SUFFIX.lstrip(".") and not host.endswith(CREZLO_HOST_SUFFIX):
        return ""
    return normalized if parsed.path and parsed.path != "/" else ""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"crezlo_receipt_invalid_json:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("crezlo_receipt_not_object")
    return dict(payload)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_pass(receipt: dict[str, Any], *, slug: str, floorplan_path: Path) -> dict[str, Any]:
    if str(receipt.get("schema") or "").strip() != SCHEMA:
        raise SystemExit("crezlo_provenance_schema_invalid")
    if str(receipt.get("status") or "").strip().lower() != "pass":
        raise SystemExit("crezlo_provenance_status_invalid")
    if str(receipt.get("provider") or "").strip().lower() != "crezlo":
        raise SystemExit("crezlo_provenance_provider_invalid")
    if str(receipt.get("target_slug") or "").strip() != slug:
        raise SystemExit("crezlo_provenance_target_mismatch")
    hosted_url = _safe_crezlo_url(receipt.get("hosted_url"))
    if not hosted_url:
        raise SystemExit("crezlo_hosted_url_invalid")
    if str(receipt.get("authorization_status") or "").strip().lower() != "approved":
        raise SystemExit("crezlo_authorization_missing")

    review = receipt.get("review")
    if not isinstance(review, dict) or any(
        str(review.get(key) or "").strip().lower() != "pass"
        for key in ("property_match", "visual_match", "spatial_capture_match")
    ):
        raise SystemExit("crezlo_property_review_missing")

    capture = receipt.get("capture")
    if not isinstance(capture, dict):
        raise SystemExit("crezlo_capture_receipt_missing")
    if str(capture.get("representation_kind") or "").strip().lower() not in {"captured_360", "provider_render"}:
        raise SystemExit("crezlo_capture_provenance_unverified")
    try:
        scene_count = int(capture.get("scene_count") or 0)
        hotspot_count = int(capture.get("navigation_hotspot_count") or 0)
        covered_space_count = int(capture.get("covered_space_count") or 0)
    except (TypeError, ValueError) as exc:
        raise SystemExit("crezlo_capture_counts_invalid") from exc
    if scene_count < 3:
        raise SystemExit("crezlo_scene_count_insufficient")
    if covered_space_count < 3:
        raise SystemExit("crezlo_space_coverage_insufficient")
    if hotspot_count < scene_count - 1:
        raise SystemExit("crezlo_hotspot_graph_insufficient")
    if capture.get("scene_graph_connected") is not True or capture.get("all_scenes_reachable") is not True:
        raise SystemExit("crezlo_scene_graph_unverified")

    floorplan = receipt.get("floorplan")
    if not isinstance(floorplan, dict):
        raise SystemExit("crezlo_floorplan_receipt_missing")
    layout = load_layout_contract(floorplan_path)
    expected_hash = _sha256_file(floorplan_path)
    declared_hash = str(floorplan.get("analysis_sha256") or "").strip().lower().removeprefix("sha256:")
    if declared_hash != expected_hash:
        raise SystemExit("crezlo_floorplan_hash_mismatch")
    if str(floorplan.get("contract_name") or "").strip() != str(layout.get("contract_name") or ""):
        raise SystemExit("crezlo_floorplan_contract_mismatch")
    if str(floorplan.get("review_status") or "").strip().lower() not in {"approved", "reviewed"}:
        raise SystemExit("crezlo_floorplan_review_missing")
    if floorplan.get("alignment_verified") is not True or floorplan.get("geometry_receipt_verified") is not True:
        raise SystemExit("crezlo_floorplan_alignment_unverified")
    try:
        declared_room_count = int(floorplan.get("source_room_count") or 0)
    except (TypeError, ValueError) as exc:
        raise SystemExit("crezlo_floorplan_room_count_invalid") from exc
    if declared_room_count != int(layout.get("room_count") or 0):
        raise SystemExit("crezlo_floorplan_room_count_mismatch")
    portal_ids = {
        str(value or "").strip()
        for value in list(floorplan.get("source_portal_ids") or [])
        if str(value or "").strip()
    }
    expected_portal_ids = {
        str(row.get("id") or "").strip()
        for row in list(dict(layout.get("source_geometry") or {}).get("portals") or [])
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    }
    if not expected_portal_ids.issubset(portal_ids):
        raise SystemExit("crezlo_floorplan_portal_coverage_mismatch")

    browser = receipt.get("browser")
    if not isinstance(browser, dict):
        raise SystemExit("crezlo_browser_receipt_missing")
    if str(browser.get("status") or "").strip().lower() != "pass" or browser.get("rendered_viewer") is not True:
        raise SystemExit("crezlo_browser_render_unverified")
    try:
        anonymous_http_status = int(browser.get("anonymous_http_status") or 0)
    except (TypeError, ValueError) as exc:
        raise SystemExit("crezlo_browser_http_status_invalid") from exc
    if anonymous_http_status != 200:
        raise SystemExit("crezlo_browser_http_status_invalid")
    if not all(
        browser.get(key) is True
        for key in (
            "drag_look_verified",
            "scene_navigation_verified",
            "desktop_viewer_verified",
            "mobile_viewer_verified",
            "touch_look_verified",
        )
    ):
        raise SystemExit("crezlo_browser_interaction_unverified")
    browser_hash = str(browser.get("browser_receipt_sha256") or "").strip().lower().removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", browser_hash):
        raise SystemExit("crezlo_browser_receipt_hash_invalid")

    return {
        "hosted_url": hosted_url,
        "floorplan": {
            **floorplan,
            "analysis_sha256": expected_hash,
            "source_room_count": int(layout.get("room_count") or 0),
            "source_portal_ids": sorted(expected_portal_ids),
        },
        "capture": {
            **capture,
            "scene_count": scene_count,
            "navigation_hotspot_count": hotspot_count,
            "covered_space_count": covered_space_count,
        },
        "browser": {**browser, "anonymous_http_status": anonymous_http_status},
        "review": dict(review),
    }


def import_crezlo_hosted_tour(
    *,
    slug: str,
    receipt_path: Path,
    floorplan_path: Path,
    public_tour_dir: Path,
) -> dict[str, Any]:
    safe_slug = _safe_slug(slug)
    if not safe_slug:
        raise SystemExit("invalid_tour_slug")
    try:
        require_dynamic_tour_slug(safe_slug)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    receipt = _load_object(receipt_path)
    normalized = _require_pass(receipt, slug=safe_slug, floorplan_path=floorplan_path)
    bundle_dir = (public_tour_dir.expanduser().resolve() / safe_slug).resolve()
    public_root = public_tour_dir.expanduser().resolve()
    if public_root not in bundle_dir.parents or not bundle_dir.is_dir():
        raise SystemExit("tour_bundle_missing")
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.is_file():
        raise SystemExit("tour_manifest_missing")
    manifest = _load_object(manifest_path)
    if str(manifest.get("slug") or "").strip() != safe_slug:
        raise SystemExit("tour_manifest_slug_mismatch")
    imported_at = _now_iso()
    manifest.update(
        {
            "control_mode": "crezlo",
            "viewer_provider": "crezlo",
            "crezlo_receipt_schema": SCHEMA,
            "crezlo_imported_at": imported_at,
        }
    )
    private_path = bundle_dir / "tour.private.json"
    private = _load_object(private_path) if private_path.is_file() else {}
    private.update(
        {
            "crezlo_public_url": normalized["hosted_url"],
            "crezlo_source_provenance": {
                "schema": SCHEMA,
                "status": "pass",
                "provider": "crezlo",
                "target_slug": safe_slug,
                "hosted_url": normalized["hosted_url"],
                "authorization_status": str(receipt.get("authorization_status") or "approved").strip().lower(),
                "capture": normalized["capture"],
                "floorplan": normalized["floorplan"],
                "review": normalized["review"],
                "source_receipt_sha256": _sha256_file(receipt_path),
                "imported_at": imported_at,
            },
            "crezlo_browser_render_proof": normalized["browser"],
            "crezlo_floorplan_geometry_receipt": normalized["floorplan"],
            "crezlo_import": {
                "source": "crezlo_hosted_provider_export",
                "receipt_path": str(receipt_path.resolve()),
                "floorplan_path": str(floorplan_path.resolve()),
                "imported_at": imported_at,
            },
        }
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    private_path.write_text(json.dumps(private, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(private_path, PRIVATE_MODE)
    return {
        "status": "imported",
        "provider": "crezlo",
        "slug": safe_slug,
        "control_url": f"/tours/{safe_slug}/control/crezlo",
        "hosted_url": normalized["hosted_url"],
        "floorplan_analysis_sha256": normalized["floorplan"]["analysis_sha256"],
        "source_receipt_sha256": _sha256_file(receipt_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a receipt-backed Crezlo hosted tour.")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--floorplan-analysis", required=True)
    parser.add_argument("--public-tour-dir", default=os.getenv("EA_PUBLIC_TOUR_DIR") or "/data/public_property_tours")
    args = parser.parse_args()
    result = import_crezlo_hosted_tour(
        slug=str(args.slug),
        receipt_path=Path(args.receipt).expanduser().resolve(),
        floorplan_path=Path(args.floorplan_analysis).expanduser().resolve(),
        public_tour_dir=Path(args.public_tour_dir),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
