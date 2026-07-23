from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response, StreamingResponse

from app.api.routes import public_tours
from app.services.public_tour_release_policy import (
    PUBLIC_TOUR_GENERATED_VIEWER_RELEASE_CONTRACT,
    evaluate_public_tour_generated_viewer_release,
)
from scripts import property_reconstruction_styles as reconstruction_styles


SLUG = "reviewed-layout-only-tour"
VIEWER = "generated-reconstruction/viewer.html"
PROOF = "generated-reconstruction/reconstruction.json"
FLOORPLAN = "generated-reconstruction/source-floorplan.png"
THREE = "generated-reconstruction/vendor/three.module.js"
ORBIT = "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js"
MODEL_OBJ = "generated-reconstruction/model.obj"
MODEL_MTL = "generated-reconstruction/model.mtl"
MODEL_GLB = "generated-reconstruction/model.glb"
SIGNED_WEBP = "generated-reconstruction/source-floorplan-signed.webp"
LEGACY_WEBP = "generated-reconstruction/photo-legacy.webp"


def _style_contract_fields() -> dict[str, object]:
    selected = reconstruction_styles.reconstruction_style(
        "urban_jungle",
        style_id="urban_jungle",
    )
    scene = reconstruction_styles.build_style_scene(selected, route_stop_count=1)
    return {
        "viewer_version": reconstruction_styles.GENERATED_RECONSTRUCTION_VIEWER_VERSION,
        "style_contract_version": reconstruction_styles.STYLE_SCENE_CONTRACT_VERSION,
        "style_id": selected["id"],
        "style_label": selected["label"],
        "style_signature": selected["signature"],
        "style_scene_signature": scene["scene_signature"],
        "style_evidence_status": "ready",
        "styled_scene_instance_count": len(scene["instances"]),
        "style_cue_kinds": list(scene["required_cues"]),
        "floorplan_display_mode": reconstruction_styles.FLOORPLAN_DISPLAY_MODE,
    }


def _viewer_style_attributes() -> str:
    style = _style_contract_fields()
    return (
        'data-pq-preview-kind="styled-3d-reconstruction" '
        'data-pq-verified-provider-capture="false" '
        f'data-pq-style-id="{style["style_id"]}" '
        f'data-pq-style-signature="{style["style_signature"]}" '
        f'data-pq-style-scene-signature="{style["style_scene_signature"]}" '
        f'data-pq-floorplan-display-mode="{style["floorplan_display_mode"]}"'
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _request(
    path: str,
    *,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": headers or [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


def _asset_bytes() -> dict[str, bytes]:
    viewer = (
        f"""<!doctype html><html lang="en" {_viewer_style_attributes()}><head><style>canvas{{display:block}}</style></head><body><canvas aria-label="Interactive 3D layout preview"></canvas><script type="module">import './vendor/three.module.js'; import './vendor/examples/jsm/controls/OrbitControls.js';</script></body></html>"""
    ).encode("utf-8")
    selected_style = reconstruction_styles.reconstruction_style(
        "urban_jungle",
        style_id="urban_jungle",
    )
    style_scene = reconstruction_styles.build_style_scene(
        selected_style,
        route_stop_count=1,
    )
    return {
        VIEWER: viewer,
        PROOF: json.dumps(
            {
                "schema": "propertyquarry.generated-reconstruction.v1",
                "provider": "propertyquarry_generated_reconstruction",
                "requested_style": selected_style,
                "style_scene": style_scene,
                "viewer": {
                    "version": reconstruction_styles.GENERATED_RECONSTRUCTION_VIEWER_VERSION,
                    "style_id": selected_style["id"],
                    "style_signature": selected_style["signature"],
                    "style_scene_signature": style_scene["scene_signature"],
                    "floorplan_display_mode": reconstruction_styles.FLOORPLAN_DISPLAY_MODE,
                    "sha256": _sha256(viewer),
                },
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
            **_style_contract_fields(),
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


def _write_governed_layout_model_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_release: bool = False,
) -> tuple[Path, dict[str, object], dict[str, bytes]]:
    root = tmp_path / "public-tours"
    bundle = root / SLUG
    viewer = (
        f"""<!doctype html><html lang="en" {_viewer_style_attributes()}><body><script type="module">import './vendor/three.module.js'; import './vendor/examples/jsm/controls/OrbitControls.js'; const modelUrl = './model.glb'; void modelUrl;</script></body></html>"""
    ).encode("utf-8")
    selected_style = reconstruction_styles.reconstruction_style(
        "urban_jungle",
        style_id="urban_jungle",
    )
    style_scene = reconstruction_styles.build_style_scene(
        selected_style,
        route_stop_count=1,
    )
    proof = json.dumps(
        {
            "provider": "propertyquarry_generated_reconstruction",
            "requested_style": selected_style,
            "style_scene": style_scene,
            "viewer": {
                "version": reconstruction_styles.GENERATED_RECONSTRUCTION_VIEWER_VERSION,
                "style_id": selected_style["id"],
                "style_signature": selected_style["signature"],
                "style_scene_signature": style_scene["scene_signature"],
                "floorplan_display_mode": reconstruction_styles.FLOORPLAN_DISPLAY_MODE,
                "sha256": _sha256(viewer),
            },
            "floorplan": {
                "source_path": "property://ArchonMegalon/propertyquarry/reviewed/floorplan.png"
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assets = {
        VIEWER: viewer,
        PROOF: proof,
        FLOORPLAN: b"reviewed-layout-floorplan",
        THREE: b"export const Scene = class Scene {};",
        ORBIT: b"export class OrbitControls {};",
        MODEL_OBJ: b"o governed-layout\nv 0 0 0\n",
        MODEL_MTL: b"newmtl governed-layout\nKd 1 1 1\n",
        MODEL_GLB: b"glTF-governed-layout",
    }
    for relpath, content in assets.items():
        target = bundle / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    model_roles = {
        MODEL_OBJ: ("generated_reconstruction_model", "model/obj"),
        MODEL_MTL: ("generated_reconstruction_material", "model/mtl"),
        MODEL_GLB: ("generated_reconstruction_model", "model/gltf-binary"),
    }
    disclosure = "Planning preview from source material, not a captured tour."
    payload: dict[str, object] = {
        "slug": SLUG,
        "publication_status": "ready",
        "tour_privacy_mode": "anonymous_public",
        "creation_mode": "generated_reconstruction_tour",
        "scene_strategy": "generated_reconstruction",
        "generated_reconstruction": {
            "provider": "propertyquarry_generated_reconstruction",
            **_style_contract_fields(),
            "viewer_relpath": VIEWER,
            "manifest_relpath": PROOF,
            "floorplan_relpath": FLOORPLAN,
            "model_relpath": MODEL_OBJ,
            "material_relpath": MODEL_MTL,
            "glb_model_relpath": MODEL_GLB,
            "photo_relpaths": [],
            "photo_reference_panel_count": 0,
            "walkable_scene_kind": "generated_reconstruction_layout",
            "walkable_scene": {"kind": "generated_reconstruction_layout"},
            "capture_mode": False,
            "synthetic": True,
            "verified_provider_capture": False,
            "satisfies_verified_tour_gate": False,
            "disclosure": disclosure,
        },
        "public_assets": [
            {
                "path": THREE,
                "privacy_class": "generated_reconstruction_public",
                "role": "generated_reconstruction_viewer_asset",
                "mime_type": "text/javascript",
            },
            {
                "path": ORBIT,
                "privacy_class": "generated_reconstruction_public",
                "role": "generated_reconstruction_viewer_asset",
                "mime_type": "text/javascript",
            },
            *[
                {
                    "path": relpath,
                    "privacy_class": "generated_reconstruction_public",
                    "role": role,
                    "mime_type": mime_type,
                    "sha256": _sha256(assets[relpath]),
                    "size_bytes": len(assets[relpath]),
                }
                for relpath, (role, mime_type) in model_roles.items()
            ],
        ],
    }
    if with_release:
        release_roles = {
            VIEWER: ("viewer_document", "text/html"),
            PROOF: ("reconstruction_manifest", "application/json"),
            FLOORPLAN: ("floorplan_texture", "image/png"),
            THREE: ("viewer_module", "text/javascript"),
            ORBIT: ("viewer_module", "text/javascript"),
            MODEL_OBJ: ("generated_reconstruction_model", "model/obj"),
            MODEL_MTL: ("generated_reconstruction_material", "model/mtl"),
            MODEL_GLB: (
                "generated_reconstruction_model",
                "model/gltf-binary",
            ),
        }
        payload["generated_viewer_release"] = {
            "contract": PUBLIC_TOUR_GENERATED_VIEWER_RELEASE_CONTRACT,
            "status": "ready",
            "provider": "propertyquarry_generated_reconstruction",
            "viewer_relpath": VIEWER,
            "asset_bindings": [
                {
                    "path": relpath,
                    "sha256": _sha256(assets[relpath]),
                    "size_bytes": len(assets[relpath]),
                    "mime_type": mime_type,
                    "role": role,
                }
                for relpath, (role, mime_type) in release_roles.items()
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
            "release_revision": "governed-layout-model-release-test",
            "disclosure": disclosure,
            "revoked": False,
            "disqualified": False,
        }
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(public_tours, "_tour_dir", lambda: root)
    return bundle, payload, assets


def _streaming_body(response: StreamingResponse) -> bytes:
    async def _consume() -> bytes:
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
        return b"".join(chunks)

    return asyncio.run(_consume())


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
    assert viewer.headers["x-propertyquarry-preview-kind"] == "styled-3d-reconstruction"
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


def test_layout_only_routes_cross_bind_style_shell_proof_and_viewer_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, payload = _write_bundle(tmp_path, monkeypatch)
    proof = json.loads((bundle / PROOF).read_text(encoding="utf-8"))
    replacement_style = reconstruction_styles.reconstruction_style(
        "warm_scandi",
        style_id="warm_scandi",
    )
    replacement_scene = reconstruction_styles.build_style_scene(
        replacement_style,
        route_stop_count=1,
    )
    proof["requested_style"] = replacement_style
    proof["style_scene"] = replacement_scene
    proof["viewer"].update(
        {
            "style_id": replacement_style["id"],
            "style_signature": replacement_style["signature"],
            "style_scene_signature": replacement_scene["scene_signature"],
        }
    )
    proof_bytes = json.dumps(
        proof,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    (bundle / PROOF).write_bytes(proof_bytes)
    proof_binding = _binding(payload, PROOF)
    proof_binding["sha256"] = _sha256(proof_bytes)
    proof_binding["size_bytes"] = len(proof_bytes)
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )

    assert evaluate_public_tour_generated_viewer_release(payload)["released"] is True
    with pytest.raises(HTTPException) as style_binding_error:
        public_tours.public_tour_generated_reconstruction_preview_asset(
            SLUG,
            VIEWER,
            _request(f"/tours/viewer/{SLUG}/{VIEWER}"),
        )
    assert style_binding_error.value.status_code == 410
    assert style_binding_error.value.detail == "tour_viewer_integrity_failed"


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


def test_governed_layout_model_assets_and_viewer_dependency_are_served(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bundle, _payload, assets = _write_governed_layout_model_bundle(
        tmp_path,
        monkeypatch,
    )

    viewer = public_tours.public_tour_file(
        SLUG,
        VIEWER,
        _request(f"/tours/files/{SLUG}/{VIEWER}"),
    )
    assert isinstance(viewer, StreamingResponse)
    assert b"./model.glb" in _streaming_body(viewer)

    expected_mime_types = {
        MODEL_OBJ: "model/obj",
        MODEL_MTL: "model/mtl",
        MODEL_GLB: "model/gltf-binary",
    }
    for relpath in (MODEL_OBJ, MODEL_MTL, MODEL_GLB):
        response = public_tours.public_tour_file(
            SLUG,
            relpath,
            _request(f"/tours/files/{SLUG}/{relpath}"),
        )
        assert isinstance(response, StreamingResponse)
        assert response.status_code == 200
        assert _streaming_body(response) == assets[relpath]
        assert response.headers["content-type"].split(";", 1)[0] == (
            expected_mime_types[relpath]
        )
        assert response.headers["x-propertyquarry-asset-sha256"] == _sha256(
            assets[relpath]
        )
        assert response.headers["x-propertyquarry-preview-kind"] == "styled-3d-reconstruction"
        assert response.headers["x-propertyquarry-verified-provider-capture"] == "false"
        viewer_dependency = (
            public_tours.public_tour_generated_reconstruction_preview_asset(
                SLUG,
                relpath,
                _request(f"/tours/viewer/{SLUG}/{relpath}"),
            )
        )
        assert isinstance(viewer_dependency, StreamingResponse)
        assert viewer_dependency.status_code == 200
        assert _streaming_body(viewer_dependency) == assets[relpath]
        assert viewer_dependency.headers["content-type"].split(";", 1)[0] == (
            expected_mime_types[relpath]
        )

    range_response = public_tours.public_tour_file(
        SLUG,
        MODEL_GLB,
        _request(
            f"/tours/files/{SLUG}/{MODEL_GLB}",
            headers=[(b"range", b"bytes=0-3")],
        ),
    )
    assert isinstance(range_response, StreamingResponse)
    assert range_response.status_code == 206
    assert _streaming_body(range_response) == assets[MODEL_GLB][:4]
    assert range_response.headers["content-range"] == (
        f"bytes 0-3/{len(assets[MODEL_GLB])}"
    )

    head_response = public_tours.public_tour_file(
        SLUG,
        MODEL_GLB,
        _request(f"/tours/files/{SLUG}/{MODEL_GLB}", method="HEAD"),
    )
    assert isinstance(head_response, Response)
    assert head_response.status_code == 200
    assert head_response.body == b""
    assert head_response.headers["content-length"] == str(len(assets[MODEL_GLB]))


def test_governed_layout_signed_mime_is_independent_of_host_mime_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, payload, assets = _write_governed_layout_model_bundle(
        tmp_path,
        monkeypatch,
    )
    webp_bytes = b"RIFF\x10\x00\x00\x00WEBPVP8 signed-layout-preview"
    assets[SIGNED_WEBP] = webp_bytes
    webp_path = bundle / SIGNED_WEBP
    webp_path.parent.mkdir(parents=True, exist_ok=True)
    webp_path.write_bytes(webp_bytes)
    payload["generated_reconstruction"]["floorplan_relpath"] = SIGNED_WEBP
    payload["public_assets"].append(
        {
            "path": SIGNED_WEBP,
            "privacy_class": "generated_reconstruction_public",
            "role": "floorplan",
            "mime_type": "image/webp",
            "sha256": _sha256(webp_bytes),
            "size_bytes": len(webp_bytes),
        }
    )
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        public_tours.mimetypes,
        "guess_type",
        lambda *_args, **_kwargs: (None, None),
    )

    expected_mime_types = {
        MODEL_OBJ: "model/obj",
        MODEL_MTL: "model/mtl",
        MODEL_GLB: "model/gltf-binary",
        SIGNED_WEBP: "image/webp",
    }
    for relpath, expected_mime_type in expected_mime_types.items():
        for response in (
            public_tours.public_tour_file(
                SLUG,
                relpath,
                _request(f"/tours/files/{SLUG}/{relpath}"),
            ),
            public_tours.public_tour_generated_reconstruction_preview_asset(
                SLUG,
                relpath,
                _request(f"/tours/viewer/{SLUG}/{relpath}"),
            ),
        ):
            assert isinstance(response, StreamingResponse)
            assert response.status_code == 200
            assert _streaming_body(response) == assets[relpath]
            assert response.headers["content-type"].split(";", 1)[0] == (
                expected_mime_type
            )

    range_response = public_tours.public_tour_file(
        SLUG,
        MODEL_GLB,
        _request(
            f"/tours/files/{SLUG}/{MODEL_GLB}",
            headers=[(b"range", b"bytes=0-3")],
        ),
    )
    assert isinstance(range_response, StreamingResponse)
    assert range_response.status_code == 206
    assert _streaming_body(range_response) == assets[MODEL_GLB][:4]
    assert range_response.headers["content-type"].split(";", 1)[0] == (
        "model/gltf-binary"
    )

    head_response = public_tours.public_tour_file(
        SLUG,
        MODEL_GLB,
        _request(f"/tours/files/{SLUG}/{MODEL_GLB}", method="HEAD"),
    )
    assert isinstance(head_response, Response)
    assert head_response.status_code == 200
    assert head_response.body == b""
    assert head_response.headers["content-type"].split(";", 1)[0] == (
        "model/gltf-binary"
    )
    assert head_response.headers["content-length"] == str(len(assets[MODEL_GLB]))


def test_legacy_generated_webp_mime_is_independent_of_host_mime_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, payload, _assets = _write_governed_layout_model_bundle(
        tmp_path,
        monkeypatch,
    )
    webp_bytes = b"RIFF\x10\x00\x00\x00WEBPVP8 legacy-layout-preview"
    webp_path = bundle / LEGACY_WEBP
    webp_path.parent.mkdir(parents=True, exist_ok=True)
    webp_path.write_bytes(webp_bytes)
    payload["generated_reconstruction"]["photo_relpaths"] = [LEGACY_WEBP]
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        public_tours.mimetypes,
        "guess_type",
        lambda *_args, **_kwargs: (None, None),
    )

    response = public_tours.public_tour_file(
        SLUG,
        LEGACY_WEBP,
        _request(f"/tours/files/{SLUG}/{LEGACY_WEBP}"),
    )
    assert isinstance(response, StreamingResponse)
    assert response.status_code == 200
    assert _streaming_body(response) == webp_bytes
    assert response.headers["content-type"].split(";", 1)[0] == "image/webp"

    head_response = public_tours.public_tour_file(
        SLUG,
        LEGACY_WEBP,
        _request(f"/tours/files/{SLUG}/{LEGACY_WEBP}", method="HEAD"),
    )
    assert isinstance(head_response, Response)
    assert head_response.status_code == 200
    assert head_response.body == b""
    assert head_response.headers["content-type"].split(";", 1)[0] == "image/webp"

    range_response = public_tours.public_tour_file(
        SLUG,
        LEGACY_WEBP,
        _request(
            f"/tours/files/{SLUG}/{LEGACY_WEBP}",
            headers=[(b"range", b"bytes=0-0")],
        ),
    )
    assert isinstance(range_response, StreamingResponse)
    assert range_response.status_code == 206
    assert _streaming_body(range_response) == webp_bytes[:1]
    assert range_response.headers["content-type"].split(";", 1)[0] == "image/webp"


def test_governed_layout_model_assets_honor_verified_viewer_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bundle, payload, assets = _write_governed_layout_model_bundle(
        tmp_path,
        monkeypatch,
        with_release=True,
    )
    decision = evaluate_public_tour_generated_viewer_release(payload)
    assert decision["released"] is True

    expected_mime_types = {
        MODEL_OBJ: "model/obj",
        MODEL_MTL: "model/mtl",
        MODEL_GLB: "model/gltf-binary",
    }
    for relpath in (MODEL_OBJ, MODEL_MTL, MODEL_GLB):
        for response in (
            public_tours.public_tour_file(
                SLUG,
                relpath,
                _request(f"/tours/files/{SLUG}/{relpath}"),
            ),
            public_tours.public_tour_generated_reconstruction_preview_asset(
                SLUG,
                relpath,
                _request(f"/tours/viewer/{SLUG}/{relpath}"),
            ),
        ):
            assert isinstance(response, Response)
            assert response.status_code == 200
            assert response.body == assets[relpath]
            assert response.headers["content-type"].split(";", 1)[0] == (
                expected_mime_types[relpath]
            )
            assert response.headers["x-propertyquarry-asset-sha256"] == _sha256(
                assets[relpath]
            )


@pytest.mark.parametrize(
    "invalid_binding",
    ("path_only", "wrong_digest", "wrong_size", "conflicting_aliases"),
)
def test_governed_layout_model_assets_require_exact_explicit_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_binding: str,
) -> None:
    bundle, payload, _assets = _write_governed_layout_model_bundle(
        tmp_path,
        monkeypatch,
    )
    public_assets = list(payload["public_assets"])
    model_row = next(
        row
        for row in public_assets
        if isinstance(row, dict) and row.get("path") == MODEL_GLB
    )
    if invalid_binding == "path_only":
        public_assets.remove(model_row)
    elif invalid_binding == "wrong_digest":
        model_row["sha256"] = "f" * 64
    elif invalid_binding == "wrong_size":
        model_row["size_bytes"] = int(model_row["size_bytes"]) + 1
    else:
        model_row["privacy"] = "private"
        model_row["asset_role"] = "debug"
        model_row["content_type"] = "text/html"
    payload["public_assets"] = public_assets
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )

    response = public_tours.public_tour_file(
        SLUG,
        MODEL_GLB,
        _request(f"/tours/files/{SLUG}/{MODEL_GLB}"),
    )
    assert isinstance(response, Response)
    assert response.status_code == 410


def test_generated_viewer_release_keeps_path_only_legacy_models_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, payload, _assets = _write_governed_layout_model_bundle(
        tmp_path,
        monkeypatch,
        with_release=True,
    )
    model_paths = {MODEL_OBJ, MODEL_MTL, MODEL_GLB}
    payload["public_assets"] = [
        row
        for row in payload["public_assets"]
        if not isinstance(row, dict) or row.get("path") not in model_paths
    ]
    release = payload["generated_viewer_release"]
    release["asset_bindings"] = [
        row
        for row in release["asset_bindings"]
        if row.get("path") not in model_paths
    ]
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )

    decision = evaluate_public_tour_generated_viewer_release(payload)
    assert decision["released"] is True
    with pytest.raises(HTTPException) as model_error:
        public_tours.public_tour_file(
            SLUG,
            MODEL_GLB,
            _request(f"/tours/files/{SLUG}/{MODEL_GLB}"),
        )
    assert model_error.value.status_code == 404


@pytest.mark.parametrize(
    "release_mutation",
    (
        "missing_public_row",
        "wrong_public_digest",
        "wrong_public_size",
        "wrong_public_mime",
        "wrong_public_role",
        "duplicate_public_row",
        "conflicting_aliases",
        "extra_release_binding",
    ),
)
def test_generated_viewer_release_model_bindings_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_mutation: str,
) -> None:
    bundle, payload, _assets = _write_governed_layout_model_bundle(
        tmp_path,
        monkeypatch,
        with_release=True,
    )
    public_assets = payload["public_assets"]
    model_row = next(
        row
        for row in public_assets
        if isinstance(row, dict) and row.get("path") == MODEL_GLB
    )
    release = payload["generated_viewer_release"]
    if release_mutation == "missing_public_row":
        public_assets.remove(model_row)
    elif release_mutation == "wrong_public_digest":
        model_row["sha256"] = "f" * 64
    elif release_mutation == "wrong_public_size":
        model_row["size_bytes"] = int(model_row["size_bytes"]) + 1
    elif release_mutation == "wrong_public_mime":
        model_row["mime_type"] = "application/octet-stream"
    elif release_mutation == "wrong_public_role":
        model_row["role"] = "generated_reconstruction_material"
    elif release_mutation == "duplicate_public_row":
        public_assets.append(dict(model_row))
    elif release_mutation == "conflicting_aliases":
        model_row["relpath"] = MODEL_GLB
        model_row["privacy"] = "private"
        model_row["asset_role"] = "debug"
        model_row["content_type"] = "text/html"
    else:
        release["asset_bindings"].append(
            {
                "path": "generated-reconstruction/extra.glb",
                "sha256": "a" * 64,
                "size_bytes": 1,
                "mime_type": "model/gltf-binary",
                "role": "generated_reconstruction_model",
            }
        )
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )

    assert evaluate_public_tour_generated_viewer_release(payload)["released"] is False
    for route in ("files", "viewer"):
        with pytest.raises(HTTPException) as model_error:
            if route == "files":
                public_tours.public_tour_file(
                    SLUG,
                    MODEL_GLB,
                    _request(f"/tours/files/{SLUG}/{MODEL_GLB}"),
                )
            else:
                public_tours.public_tour_generated_reconstruction_preview_asset(
                    SLUG,
                    MODEL_GLB,
                    _request(f"/tours/viewer/{SLUG}/{MODEL_GLB}"),
                )
        assert model_error.value.status_code == 404


@pytest.mark.parametrize("unsafe_topology", ("symlink", "hardlink"))
def test_governed_layout_model_assets_reject_unsafe_file_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_topology: str,
) -> None:
    bundle, _payload, assets = _write_governed_layout_model_bundle(
        tmp_path,
        monkeypatch,
    )
    model_path = bundle / MODEL_GLB
    model_path.unlink()
    if unsafe_topology == "symlink":
        target = bundle / "generated-reconstruction" / "model-target.glb"
        target.write_bytes(assets[MODEL_GLB])
        model_path.symlink_to(target.name)
    else:
        target = bundle / "generated-reconstruction" / "model-hardlink.glb"
        target.write_bytes(assets[MODEL_GLB])
        model_path.hardlink_to(target)

    with pytest.raises(HTTPException) as error:
        public_tours.public_tour_file(
            SLUG,
            MODEL_GLB,
            _request(f"/tours/files/{SLUG}/{MODEL_GLB}"),
        )
    assert error.value.status_code == 404


def test_governed_layout_model_route_rejects_traversal_and_arbitrary_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, payload, _assets = _write_governed_layout_model_bundle(
        tmp_path,
        monkeypatch,
    )
    with pytest.raises(HTTPException) as traversal_error:
        public_tours.public_tour_file(
            SLUG,
            "../tour.private.json",
            _request(f"/tours/files/{SLUG}/../tour.private.json"),
        )
    assert traversal_error.value.status_code == 404

    arbitrary_relpath = "generated-reconstruction/private-debug.glb"
    arbitrary_bytes = b"private-debug-model"
    (bundle / arbitrary_relpath).write_bytes(arbitrary_bytes)
    payload["public_assets"].append(
        {
            "path": arbitrary_relpath,
            "privacy_class": "generated_reconstruction_public",
            "role": "generated_reconstruction_model",
            "mime_type": "model/gltf-binary",
            "sha256": _sha256(arbitrary_bytes),
            "size_bytes": len(arbitrary_bytes),
        }
    )
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    response = public_tours.public_tour_file(
        SLUG,
        arbitrary_relpath,
        _request(f"/tours/files/{SLUG}/{arbitrary_relpath}"),
    )
    assert isinstance(response, Response)
    assert response.status_code == 410


@pytest.mark.parametrize(
    "source_value",
    [
        {"private": "/home/operator/floorplan.png"},
        r"C:\Users\operator\private-floorplan.png",
        r"C:Users\operator\private-floorplan.png",
        r"C:private-floorplan.png",
        "property://C:/Users/operator/private-floorplan.png",
        "pcloud://C:Users/operator/private-floorplan.png",
        "run/secrets/propertyquarry-token",
        "proc/self/environ",
        "etc/propertyquarry/authority.json",
        "docker/property/state/runtime/private.env",
    ],
)
def test_layout_only_provenance_rejects_non_string_and_local_paths(
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


@pytest.mark.parametrize(
    "source_value",
    [
        "<provided-image>",
        "property://ArchonMegalon/propertyquarry/reviewed/floorplan.png",
        "pcloud://propertyquarry/reviewed/floorplan.png",
    ],
)
def test_layout_only_provenance_accepts_only_governed_source_references(
    source_value: str,
) -> None:
    assert public_tours._public_tour_generated_source_path_is_unsafe(source_value) is False


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
