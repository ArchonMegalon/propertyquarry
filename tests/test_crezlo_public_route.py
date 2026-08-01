from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.routes import public_tours


def _payload() -> dict[str, object]:
    return {
        "slug": "crezlo-route-proof",
        "crezlo_public_url": "https://ea-property-tours-1781067093.crezlotours.com/tours/real-tour",
        "crezlo_source_provenance": {
            "schema": "propertyquarry.crezlo_source_provenance.v1",
            "status": "pass",
            "provider": "crezlo",
            "target_slug": "crezlo-route-proof",
            "hosted_url": "https://ea-property-tours-1781067093.crezlotours.com/tours/real-tour",
            "authorization_status": "approved",
            "capture": {
                "representation_kind": "captured_360",
                "scene_count": 3,
                "navigation_hotspot_count": 2,
                "covered_space_count": 3,
                "scene_graph_connected": True,
                "all_scenes_reachable": True,
            },
            "floorplan": {
                "contract_name": "propertyquarry.floorplan_analysis.v2",
                "review_status": "approved",
                "analysis_sha256": "a" * 64,
                "source_room_count": 3,
                "source_portal_ids": ["entrance-to-living"],
                "alignment_verified": True,
                "geometry_receipt_verified": True,
            },
            "review": {"property_match": "pass", "visual_match": "pass", "spatial_capture_match": "pass"},
        },
        "crezlo_browser_render_proof": {
            "status": "pass",
            "rendered_viewer": True,
            "anonymous_http_status": 200,
            "drag_look_verified": True,
            "scene_navigation_verified": True,
            "desktop_viewer_verified": True,
            "mobile_viewer_verified": True,
            "touch_look_verified": True,
            "browser_receipt_sha256": "b" * 64,
        },
    }


def test_crezlo_route_is_allowlisted_and_source_geometry_locked() -> None:
    payload = _payload()
    assert public_tours._safe_crezlo_external_url(payload["crezlo_public_url"])
    assert public_tours._safe_crezlo_external_url("https://evil.example/tour") == ""
    assert public_tours._crezlo_hosted_tour_ready(payload, slug="crezlo-route-proof") is True
    assert public_tours._public_tour_primary_control_path(payload) == "/tours/crezlo-route-proof/control/crezlo"


def test_crezlo_route_rejects_missing_geometry_receipt() -> None:
    payload = _payload()
    del payload["crezlo_source_provenance"]["floorplan"]
    assert public_tours._crezlo_hosted_tour_ready(payload, slug="crezlo-route-proof") is False


def test_crezlo_origin_is_scoped_to_receipt_backed_control_documents() -> None:
    default_csp = public_tours._public_tour_security_headers()["Content-Security-Policy"]
    control_csp = public_tours._public_tour_security_headers(allow_crezlo=True)["Content-Security-Policy"]
    assert "frame-src 'self' https://3dvista.com https://*.3dvista.com https://crezlotours.com" not in default_csp
    assert "frame-src 'self' https://3dvista.com https://*.3dvista.com https://crezlotours.com https://*.crezlotours.com;" in control_csp


def test_crezlo_first_party_control_route_embeds_only_verified_provider(monkeypatch, tmp_path: Path) -> None:
    slug = "crezlo-browser-route"
    bundle = tmp_path / slug
    bundle.mkdir()
    payload = _payload()
    payload["slug"] = slug
    payload["crezlo_source_provenance"]["target_slug"] = slug
    (bundle / "tour.json").write_text(json.dumps({"slug": slug, "title": "Crezlo route", "control_mode": "crezlo", "scenes": [{"name": "Living", "role": "photo"}]}), encoding="utf-8")
    (bundle / "tour.private.json").write_text(
        json.dumps({
            "crezlo_public_url": payload["crezlo_public_url"],
            "crezlo_source_provenance": payload["crezlo_source_provenance"],
            "crezlo_browser_render_proof": payload["crezlo_browser_render_proof"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_API_TOKEN", "")
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "1")
    from app.api.app import create_app

    response = TestClient(create_app()).get(f"/tours/{slug}/control/crezlo")
    assert response.status_code == 200
    assert "https://ea-property-tours-1781067093.crezlotours.com/tours/real-tour" in response.text
    assert "frame-src 'self' https://3dvista.com https://*.3dvista.com https://crezlotours.com https://*.crezlotours.com;" in response.headers["content-security-policy"]
