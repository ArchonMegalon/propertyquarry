from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.api.routes import public_tours
from app.services.public_tour_release_policy import (
    PUBLIC_TOUR_GENERATED_VIEWER_RELEASE_CONTRACT,
    evaluate_public_tour_generated_viewer_release,
)


SLUG = "reviewed-layout-only-tour"
VIEWER = "generated-reconstruction/viewer.html"
PROOF = "generated-reconstruction/reconstruction.json"
FLOORPLAN = "generated-reconstruction/source-floorplan.png"
THREE = "generated-reconstruction/vendor/three.module.js"
ORBIT = "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


def _asset_bytes() -> dict[str, bytes]:
    return {
        VIEWER: b"""<!doctype html><html lang=\"en\" data-pq-preview-kind=\"approximate-layout\" data-pq-verified-provider-capture=\"false\"><head><style>canvas{display:block}</style></head><body><canvas aria-label=\"Interactive 3D layout preview\"></canvas><script type=\"module\">import './vendor/three.module.js'; import './vendor/examples/jsm/controls/OrbitControls.js';</script></body></html>""",
        PROOF: json.dumps(
            {
                "schema": "propertyquarry.generated-reconstruction.v1",
                "floorplan": {
                    "source_path": "property://ArchonMegalon/propertyquarry/reviewed/floorplan.png"
                },
                "photo_reference_panel_count": 0,
                "photo_reference_panels": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        FLOORPLAN: b"reviewed-png-bytes",
        THREE: b"export const Scene = class Scene {};",
        ORBIT: b"export class OrbitControls {};",
    }


def test_layout_only_fixture_uses_standalone_repository_identity() -> None:
    proof = json.loads(_asset_bytes()[PROOF])
    source_path = proof["floorplan"]["source_path"]

    assert source_path.startswith("property://ArchonMegalon/propertyquarry/")
    assert "property://ArchonMegalon/property/" not in source_path


def _payload(assets: dict[str, bytes]) -> dict[str, object]:
    disclosure = (
        "Generated interactive reconstruction from the supplied floor plan. "
        "It is a layout aid, not a captured or provider-verified 3D scan."
    )
    roles = {
        VIEWER: ("text/html", "viewer_document"),
        PROOF: ("application/json", "reconstruction_manifest"),
        FLOORPLAN: ("image/png", "floorplan_texture"),
        THREE: ("text/javascript", "viewer_module"),
        ORBIT: ("text/javascript", "viewer_module"),
    }
    return {
        "slug": SLUG,
        "generated_reconstruction": {
            "provider": "propertyquarry_generated_reconstruction",
            "viewer_version": "propertyquarry_3d_tour_viewer_v3",
            "viewer_relpath": VIEWER,
            "manifest_relpath": PROOF,
            "floorplan_relpath": FLOORPLAN,
            "photo_relpaths": [],
            "photo_reference_panel_count": 0,
            "capture_mode": False,
            "synthetic": True,
            "verified_provider_capture": False,
            "satisfies_verified_tour_gate": False,
            "disclosure": disclosure,
        },
        "generated_viewer_release": {
            "contract": PUBLIC_TOUR_GENERATED_VIEWER_RELEASE_CONTRACT,
            "status": "ready",
            "provider": "propertyquarry_generated_reconstruction",
            "viewer_relpath": VIEWER,
            "asset_bindings": [
                {
                    "path": path,
                    "sha256": _sha256(content),
                    "size_bytes": len(content),
                    "mime_type": roles[path][0],
                    "role": roles[path][1],
                }
                for path, content in assets.items()
            ],
            "browser_receipt_sha256": "1" * 64,
            "source_provenance_receipt_sha256": "2" * 64,
            "publication_authority_receipt_sha256": "3" * 64,
            "security_review_receipt_sha256": "4" * 64,
            "accessibility_review_receipt_sha256": "5" * 64,
            "browser_interaction_verified": True,
            "visual_quality_review_passed": True,
            "security_review_passed": True,
            "accessibility_review_passed": True,
            "source_provenance_verified": True,
            "publication_authority_verified": True,
            "public_activation_authority": True,
            "capture_mode": False,
            "synthetic": True,
            "verified_provider_capture": False,
            "satisfies_verified_tour_gate": False,
            "release_revision": "property-layout-release-test",
            "disclosure": disclosure,
            "revoked": False,
            "disqualified": False,
        },
    }


def _write_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "public-tours"
    bundle = root / SLUG
    assets = _asset_bytes()
    for relpath, content in assets.items():
        target = bundle / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    payload = _payload(assets)
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(public_tours, "_tour_dir", lambda: root)
    return bundle, payload


def _binding(payload: dict[str, object], relpath: str) -> dict[str, object]:
    release = payload["generated_viewer_release"]
    return next(row for row in release["asset_bindings"] if row["path"] == relpath)


def test_layout_only_release_requires_explicit_exact_zero_and_exact_bindings() -> None:
    payload = _payload(_asset_bytes())
    decision = evaluate_public_tour_generated_viewer_release(payload)

    assert decision["released"] is True
    assert decision["photo_reference_panel_count"] == 0
    assert set(decision["bindings"]) == {VIEWER, PROOF, FLOORPLAN, THREE, ORBIT}

    for invalid_count in (False, None, "0", -1, 1):
        changed = json.loads(json.dumps(payload))
        changed["generated_reconstruction"]["photo_reference_panel_count"] = invalid_count
        assert evaluate_public_tour_generated_viewer_release(changed)["released"] is False

    missing_paths = json.loads(json.dumps(payload))
    missing_paths["generated_reconstruction"].pop("photo_relpaths")
    assert evaluate_public_tour_generated_viewer_release(missing_paths)["released"] is False

    boolean_size = json.loads(json.dumps(payload))
    boolean_size["generated_viewer_release"]["asset_bindings"][0]["size_bytes"] = True
    assert evaluate_public_tour_generated_viewer_release(boolean_size)["released"] is False

    extra_binding = json.loads(json.dumps(payload))
    extra_binding["generated_viewer_release"]["asset_bindings"].append(
        {
            "path": "generated-reconstruction/unreviewed.js",
            "sha256": "9" * 64,
            "size_bytes": 1,
            "mime_type": "text/javascript",
            "role": "viewer_module",
        }
    )
    assert evaluate_public_tour_generated_viewer_release(extra_binding)["released"] is False

    photo_payload = json.loads(json.dumps(payload))
    photo_relpath = "generated-reconstruction/photos/living-room.jpg"
    photo_payload["generated_reconstruction"]["photo_relpaths"] = [photo_relpath]
    photo_payload["generated_reconstruction"]["photo_reference_panel_count"] = 1
    photo_payload["generated_viewer_release"]["asset_bindings"].append(
        {
            "path": photo_relpath,
            "sha256": "a" * 64,
            "size_bytes": 42,
            "mime_type": "image/jpeg",
            "role": "photo_texture",
        }
    )
    assert evaluate_public_tour_generated_viewer_release(photo_payload)["released"] is True

    photo_payload["generated_reconstruction"]["photo_reference_panel_count"] = 0
    assert evaluate_public_tour_generated_viewer_release(photo_payload)["released"] is False

    outside_namespace = json.loads(json.dumps(payload))
    outside_namespace["generated_reconstruction"]["viewer_relpath"] = "viewer.html"
    outside_namespace["generated_viewer_release"]["viewer_relpath"] = "viewer.html"
    viewer_binding = next(
        row
        for row in outside_namespace["generated_viewer_release"]["asset_bindings"]
        if row["path"] == VIEWER
    )
    viewer_binding["path"] = "viewer.html"
    assert (
        evaluate_public_tour_generated_viewer_release(outside_namespace)["released"]
        is False
    )

    oversized = json.loads(json.dumps(payload))
    oversized["generated_viewer_release"]["asset_bindings"][0]["size_bytes"] = (
        8 * 1024 * 1024 + 1
    )
    assert evaluate_public_tour_generated_viewer_release(oversized)["released"] is False

    numeric_receipt_hash = json.loads(json.dumps(payload))
    numeric_receipt_hash["generated_viewer_release"]["browser_receipt_sha256"] = int(
        "1" * 64
    )
    assert (
        evaluate_public_tour_generated_viewer_release(numeric_receipt_hash)["released"]
        is False
    )

    boolean_revision = json.loads(json.dumps(payload))
    boolean_revision["generated_viewer_release"]["release_revision"] = True
    assert evaluate_public_tour_generated_viewer_release(boolean_revision)["released"] is False

    numeric_binding_hash = json.loads(json.dumps(payload))
    numeric_binding_hash["generated_viewer_release"]["asset_bindings"][0]["sha256"] = int(
        "1" * 64
    )
    assert (
        evaluate_public_tour_generated_viewer_release(numeric_binding_hash)["released"]
        is False
    )


def test_layout_only_routes_serve_only_verified_public_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bundle, payload = _write_bundle(tmp_path, monkeypatch)
    viewer_url = f"/tours/viewer/{SLUG}/{VIEWER}"

    root = public_tours.public_tour_page(
        SLUG,
        _request(f"/tours/{SLUG}"),
        None,
    )
    layout = public_tours.public_tour_generated_layout_preview(
        SLUG,
        _request(f"/tours/{SLUG}/layout-preview"),
    )
    assert isinstance(root, RedirectResponse)
    assert isinstance(layout, RedirectResponse)
    assert root.status_code == layout.status_code == 302
    assert root.headers["location"] == layout.headers["location"] == viewer_url

    viewer = public_tours.public_tour_generated_reconstruction_preview_asset(
        SLUG,
        VIEWER,
        _request(viewer_url),
    )
    module = public_tours.public_tour_generated_reconstruction_preview_asset(
        SLUG,
        THREE,
        _request(f"/tours/viewer/{SLUG}/{THREE}"),
    )
    generic_viewer = public_tours.public_tour_file(
        SLUG,
        VIEWER,
        _request(f"/tours/files/{SLUG}/{VIEWER}"),
    )
    assert isinstance(viewer, Response)
    assert viewer.body == _asset_bytes()[VIEWER]
    assert module.body == _asset_bytes()[THREE]
    assert generic_viewer.body == viewer.body
    assert viewer.headers["x-propertyquarry-asset-sha256"] == _binding(
        payload,
        VIEWER,
    )["sha256"]
    assert viewer.headers["x-propertyquarry-preview-kind"] == "approximate-layout"
    assert viewer.headers["x-propertyquarry-verified-provider-capture"] == "false"
    assert "script-src 'self' 'sha256-" in viewer.headers["content-security-policy"]

    with pytest.raises(HTTPException) as proof_error:
        public_tours.public_tour_generated_reconstruction_preview_asset(
            SLUG,
            PROOF,
            _request(f"/tours/viewer/{SLUG}/{PROOF}"),
        )
    assert proof_error.value.status_code == 404


def test_layout_only_routes_fail_closed_on_byte_drift_symlinks_and_private_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, payload = _write_bundle(tmp_path, monkeypatch)

    (bundle / THREE).write_bytes(b"tampered-module")
    with pytest.raises(HTTPException) as drift_error:
        public_tours.public_tour_generated_reconstruction_preview_asset(
            SLUG,
            THREE,
            _request(f"/tours/viewer/{SLUG}/{THREE}"),
        )
    assert drift_error.value.status_code == 410

    (bundle / THREE).write_bytes(_asset_bytes()[THREE])
    orbit_path = bundle / ORBIT
    orbit_path.unlink()
    orbit_path.symlink_to(bundle / THREE)
    with pytest.raises(HTTPException) as symlink_error:
        public_tours.public_tour_generated_reconstruction_preview_asset(
            SLUG,
            ORBIT,
            _request(f"/tours/viewer/{SLUG}/{ORBIT}"),
        )
    assert symlink_error.value.status_code == 404

    orbit_path.unlink()
    orbit_path.write_bytes(_asset_bytes()[ORBIT])
    private_proof = b'{"floorplan":{"source_path":"/home/operator/private.png"}}'
    (bundle / PROOF).write_bytes(private_proof)
    proof_binding = _binding(payload, PROOF)
    proof_binding["sha256"] = _sha256(private_proof)
    proof_binding["size_bytes"] = len(private_proof)
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(HTTPException) as provenance_error:
        public_tours.public_tour_generated_reconstruction_preview_asset(
            SLUG,
            VIEWER,
            _request(f"/tours/viewer/{SLUG}/{VIEWER}"),
        )
    assert provenance_error.value.status_code == 410


def test_layout_only_generic_route_cannot_bypass_release_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, payload = _write_bundle(tmp_path, monkeypatch)
    extra_relpath = "generated-reconstruction/unbound-preview.png"
    extra_path = bundle / extra_relpath
    extra_path.write_bytes(b"unbound-public-looking-image")
    payload["public_assets"] = [
        {
            "path": extra_relpath,
            "privacy_class": "generated_reconstruction_public",
            "role": "floorplan",
            "mime_type": "image/png",
        }
    ]
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(HTTPException) as unbound_error:
        public_tours.public_tour_file(
            SLUG,
            extra_relpath,
            _request(f"/tours/files/{SLUG}/{extra_relpath}"),
        )
    assert unbound_error.value.status_code == 404

    payload["generated_viewer_release"]["asset_bindings"] = [
        row
        for row in payload["generated_viewer_release"]["asset_bindings"]
        if row["path"] != THREE
    ]
    payload["public_assets"].append(
        {
            "path": THREE,
            "privacy_class": "generated_reconstruction_public",
            "role": "generated_reconstruction_viewer_asset",
            "mime_type": "text/javascript",
        }
    )
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(HTTPException) as invalid_release_error:
        public_tours.public_tour_file(
            SLUG,
            THREE,
            _request(f"/tours/files/{SLUG}/{THREE}"),
        )
    assert invalid_release_error.value.status_code == 404


@pytest.mark.parametrize(
    "source_value",
    [
        {"private": "/home/operator/floorplan.png"},
        r"C:\Users\operator\private-floorplan.png",
        r"C:Users\operator\private-floorplan.png",
        r"C:private-floorplan.png",
        "property://C:/Users/operator/private-floorplan.png",
        "pcloud://C:Users/operator/private-floorplan.png",
    ],
)
def test_layout_only_provenance_rejects_non_string_and_local_windows_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_value: object,
) -> None:
    bundle, payload = _write_bundle(tmp_path, monkeypatch)
    proof = json.dumps(
        {"floorplan": {"source_path": source_value}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    (bundle / PROOF).write_bytes(proof)
    proof_binding = _binding(payload, PROOF)
    proof_binding["sha256"] = _sha256(proof)
    proof_binding["size_bytes"] = len(proof)
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(HTTPException) as provenance_error:
        public_tours.public_tour_generated_reconstruction_preview_asset(
            SLUG,
            VIEWER,
            _request(f"/tours/viewer/{SLUG}/{VIEWER}"),
        )
    assert provenance_error.value.status_code == 410


def test_layout_only_terminal_release_renders_gone_and_provider_control_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, payload = _write_bundle(tmp_path, monkeypatch)
    payload["generated_viewer_release"]["revoked"] = True
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )

    root = public_tours.public_tour_page(
        SLUG,
        _request(f"/tours/{SLUG}"),
        None,
    )
    layout = public_tours.public_tour_generated_layout_preview(
        SLUG,
        _request(f"/tours/{SLUG}/layout-preview"),
    )
    assert root.status_code == layout.status_code == 410
    assert b"Preview removed" in root.body

    payload["generated_viewer_release"]["revoked"] = False
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    control_path = f"/tours/{SLUG}/control/3dvista"
    monkeypatch.setattr(
        public_tours,
        "_public_tour_primary_control_path",
        lambda _payload: control_path,
    )
    root_with_control = public_tours.public_tour_page(
        SLUG,
        _request(f"/tours/{SLUG}"),
        None,
    )
    layout_with_control = public_tours.public_tour_generated_layout_preview(
        SLUG,
        _request(f"/tours/{SLUG}/layout-preview"),
    )
    assert root_with_control.headers["location"] == control_path
    assert layout_with_control.headers["location"] == control_path
