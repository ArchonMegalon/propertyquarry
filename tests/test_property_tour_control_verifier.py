from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

from app.api.routes.public_tour_payloads import require_public_tour_viewable
from app.api.routes.public_tours import (
    _PUBLIC_TOUR_RUNTIME_ACCEPTANCE_TOKEN,
    _tour_control_external_iframe_html,
)
from scripts.property_tour_3dvista_provenance import (
    THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
    export_tree_sha256,
    sha256_text,
)
from scripts.property_tour_panorama_provenance import (
    KRPANO_SPATIAL_PROVENANCE_KEY,
    PANORAMA_SPATIAL_PROVENANCE_SCHEMA,
    PANO2VR_SPATIAL_PROVENANCE_KEY,
    asset_set_sha256 as panorama_asset_set_sha256,
    export_tree_sha256 as panorama_export_tree_sha256,
    panorama_asset_relpaths,
    pano2vr_export_topology,
    walkable_scene_topology,
)
from scripts.property_magicfit_delivery_contract import (
    ACCEPTED_DELIVERY_CONTRACT,
    AUDIT_CONTRACT,
    BROWSER_RECEIPT_CONTRACT,
    DELIVERY_REVIEW_CONTRACT,
    EVIDENCE_CONTRACT,
    MANIFEST_TRANSFORM_CONTRACT,
    VISUAL_REVIEW_CONTRACT,
    accepted_sidecar_relpath,
    audit_relpaths,
    build_audit_entry,
    build_candidate_manifest_bytes,
    canonical_json_bytes,
    delivery_digest as magicfit_delivery_digest,
    digest_bound_video_relpath,
)
from scripts.property_magicfit_reviewer_authority import (
    REVIEWER_TEST_OWNER_UID_ENV,
    REVIEWER_TRUST_STORE_ENV,
    verify_magicfit_reviewer_authorization,
)
from scripts.verify_property_tour_controls import (
    _best_tour_root,
    _load_cli_env_defaults,
    _receipt_summary,
    _running_container_public_tour_dir,
    _runtime_container_live_probe_receipt,
    build_property_tour_control_receipt,
    main,
)
from tests.magicfit_test_support import (
    magicfit_reviewer_subject,
    provision_magicfit_reviewer_test_authority,
)


def _write_tour(root: Path, slug: str, payload: dict[str, object], files: dict[str, str | bytes] | None = None) -> None:
    bundle = root / slug
    bundle.mkdir(parents=True)
    body = {"slug": slug, "title": slug, **payload}
    (bundle / "tour.json").write_text(json.dumps(body), encoding="utf-8")
    for relpath, content in (files or {}).items():
        target = bundle / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    if "three_d_vista_white_label_proof" in body and "three_d_vista_target_provenance" not in body:
        entry_relpath = str(body.get("three_d_vista_entry_relpath") or "").strip()
        entry_parts = Path(entry_relpath).parts if entry_relpath else ()
        if len(entry_parts) > 1 and (bundle / entry_relpath).is_file():
            target_subdir = entry_parts[0]
            body["three_d_vista_target_provenance"] = _clean_3dvista_target_provenance(
                slug,
                sha256=export_tree_sha256(bundle / target_subdir),
                entry_relpath=Path(*entry_parts[1:]).as_posix(),
                target_subdir=target_subdir,
            )
            (bundle / "tour.json").write_text(json.dumps(body), encoding="utf-8")


def _matterport_publication(model_sid: str) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "status": "pass",
        "model_sid": model_sid,
        "model_available": True,
        "checked_at": (now - timedelta(minutes=1)).isoformat(),
        "asset_valid_until": (now + timedelta(hours=6)).isoformat(),
        "proof_valid_until": (now + timedelta(hours=6)).isoformat(),
        "enabled_sweep_count": 23,
        "available_sweep_count": 23,
        "connected_component_count": 1,
        "room_count": 6,
        "navigation_edge_count": 49,
        "source_sha256": "a" * 64,
    }

@pytest.mark.parametrize("publication_status", ["ready", "published", "active", " READY "])
def test_public_tour_explicit_terminal_publication_status_is_viewable(
    publication_status: str,
) -> None:
    require_public_tour_viewable({"publication_status": publication_status})


def test_public_tour_absent_publication_status_remains_legacy_compatible() -> None:
    require_public_tour_viewable({})


@pytest.mark.parametrize(
    "publication_status",
    [
        "draft",
        "failed",
        "blocked",
        "rejected",
        "garbage",
        "generating",
        "pending",
        "staging",
        "",
        None,
        False,
    ],
)
def test_public_tour_any_explicit_nonterminal_publication_status_fails_closed(
    publication_status: object,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_public_tour_viewable({"publication_status": publication_status})
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "tour_not_found"


def test_public_tour_pending_magicfit_import_fails_closed_during_upgrade() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_public_tour_viewable(
            {
                "magicfit_import": {
                    "proof_status": "render_verified_pending_delivery_acceptance"
                }
            }
        )
    assert exc_info.value.status_code == 404


def _write_playable_mp4(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AssertionError("ffmpeg is required for playable MagicFit verifier fixtures")
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:d=1",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def _tiny_decodable_contact_sheet_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), color=(28, 36, 42)).save(buffer, format="PNG")
    body = buffer.getvalue()
    with Image.open(io.BytesIO(body)) as decoded:
        decoded.load()
        assert decoded.format == "PNG"
        assert decoded.size == (1, 1)
    return body


def _write_reproducible_magicfit_tour(
    root: Path,
    slug: str,
    video_bytes: bytes,
    *,
    generated_at: str = "2024-01-01T00:00:00Z",
    include_scene: bool = False,
) -> dict[str, object]:
    """Materialize a complete synthetic v4 audit bundle for verifier tests."""

    bundle = root / slug
    bundle.mkdir(parents=True, exist_ok=True)
    base_manifest: dict[str, object] = {"slug": slug, "title": slug}
    if include_scene:
        base_manifest["scenes"] = [
            {
                "name": "Property overview",
                "role": "photo",
                "asset_relpath": "overview.jpg",
            }
        ]
    base_manifest_bytes = canonical_json_bytes(base_manifest)
    video_sha256 = hashlib.sha256(video_bytes).hexdigest()
    source_receipt = {
        "provider": "magicfit",
        "provider_key": "magicfit",
        "provider_backend_key": "magicfit",
        "render_status": "completed",
        "target_slug": slug,
        "hosted_walkthrough_video_url": (
            f"https://media.powlcdn.com/magicfit/{slug}.mp4"
        ),
    }
    source_receipt_bytes = canonical_json_bytes(source_receipt)
    source_receipt_sha256 = hashlib.sha256(source_receipt_bytes).hexdigest()
    base_manifest_sha256 = hashlib.sha256(base_manifest_bytes).hexdigest()
    requested_target_relpath = "walkthrough.mp4"
    video_relpath = digest_bound_video_relpath(
        requested_target_relpath, video_sha256
    )
    coverage_proof: dict[str, object] = {}
    delivery_digest = magicfit_delivery_digest(
        slug=slug,
        requested_target_relpath=requested_target_relpath,
        video_relpath=video_relpath,
        video_sha256=video_sha256,
        video_size_bytes=len(video_bytes),
        source_receipt_sha256=source_receipt_sha256,
        base_manifest_sha256=base_manifest_sha256,
        generated_at=generated_at,
        coverage_proof=coverage_proof,
    )
    sidecar_relpath = accepted_sidecar_relpath(delivery_digest)
    active_manifest_bytes = build_candidate_manifest_bytes(
        base_manifest_bytes=base_manifest_bytes,
        slug=slug,
        requested_target_relpath=requested_target_relpath,
        video_relpath=video_relpath,
        video_sha256=video_sha256,
        video_size_bytes=len(video_bytes),
        source_receipt_sha256=source_receipt_sha256,
        generated_at=generated_at,
        coverage_proof=coverage_proof,
    )
    staged_manifest_sha256 = hashlib.sha256(active_manifest_bytes).hexdigest()
    checklist = {
        "playback_to_end": True,
        "continuous_walkthrough": True,
        "no_visible_rotation_jump": True,
        "intended_property_and_scope": True,
        "no_sensitive_or_trial_branding": True,
    }
    review_route = (
        f"operator-review://propertyquarry/magicfit/{slug}/{video_sha256}"
    )
    browser_bytes = canonical_json_bytes(
        {
            "schema": BROWSER_RECEIPT_CONTRACT,
            "status": "pass",
            "provider": "magicfit",
            "target_slug": slug,
            "observed_at": generated_at.replace("+00:00", "Z"),
            "route": review_route,
            "http_status": 200,
            "video_sha256": video_sha256,
            "base_manifest_sha256": base_manifest_sha256,
            "staged_manifest_sha256": staged_manifest_sha256,
            "delivery_digest": delivery_digest,
            "duration_seconds": 1.0,
            "final_current_time": 1.0,
            "playback_to_end": True,
            "video_error": None,
            "console_errors": [],
            "request_failures": [],
            "benign_request_aborts": [],
            "bad_responses": [],
        }
    )
    visual_bytes = canonical_json_bytes(
        {
            "schema": VISUAL_REVIEW_CONTRACT,
            "status": "pass",
            "provider": "magicfit",
            "target_slug": slug,
            "observed_at": generated_at.replace("+00:00", "Z"),
            "video_sha256": video_sha256,
            "base_manifest_sha256": base_manifest_sha256,
            "staged_manifest_sha256": staged_manifest_sha256,
            "delivery_digest": delivery_digest,
            "checklist": checklist,
        }
    )
    contact_sheet_bytes = _tiny_decodable_contact_sheet_png()
    evidence_bytes = canonical_json_bytes(
        {
            "schema": EVIDENCE_CONTRACT,
            "status": "pass",
            "provider": "magicfit",
            "target_slug": slug,
            "observed_at": generated_at.replace("+00:00", "Z"),
            "source_receipt_sha256": source_receipt_sha256,
            "base_manifest_sha256": base_manifest_sha256,
            "staged_manifest_sha256": staged_manifest_sha256,
            "delivery_digest": delivery_digest,
            "video": {
                "sha256": video_sha256,
                "size_bytes": len(video_bytes),
                "duration_seconds": 1.0,
            },
            "checklist": checklist,
            "artifacts": {
                "contact_sheet_sha256": hashlib.sha256(
                    contact_sheet_bytes
                ).hexdigest(),
                "browser_receipt_sha256": hashlib.sha256(
                    browser_bytes
                ).hexdigest(),
                "visual_review_sha256": hashlib.sha256(
                    visual_bytes
                ).hexdigest(),
            },
        }
    )
    reviewed_at = "2024-01-01T00:01:00Z"
    issued_at = "2024-01-01T00:01:00Z"
    reviewer_test_authority = provision_magicfit_reviewer_test_authority(
        root.parent / f".{root.name}-reviewer-trust",
        public_tour_root=root,
    )
    signed_authorization = reviewer_test_authority.sign_authorization(
        subject=magicfit_reviewer_subject(
            delivery_digest=delivery_digest,
            video_sha256=video_sha256,
            staged_manifest_sha256=staged_manifest_sha256,
            browser_receipt_sha256=hashlib.sha256(browser_bytes).hexdigest(),
            evidence_receipt_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
            visual_review_sha256=hashlib.sha256(visual_bytes).hexdigest(),
            contact_sheet_sha256=hashlib.sha256(contact_sheet_bytes).hexdigest(),
            reviewed_at=reviewed_at,
        ),
        issued_at=issued_at,
        expires_at="2024-01-01T01:01:00Z",
    )
    authority_bytes = signed_authorization.body
    reviewer_authorization_projection = verify_magicfit_reviewer_authorization(
        signed_authorization.path,
        expected_subject=signed_authorization.subject,
        trust_store_path=reviewer_test_authority.trust_store_path,
        public_tour_root=root,
        observed_at=datetime.fromisoformat(
            issued_at.replace("Z", "+00:00")
        ).astimezone(timezone.utc),
        allowed_owner_uids=[os.geteuid()],
    ).as_dict()
    os.environ[REVIEWER_TRUST_STORE_ENV] = str(
        reviewer_test_authority.trust_store_path
    )
    os.environ[REVIEWER_TEST_OWNER_UID_ENV] = str(os.geteuid())
    audit_bodies = {
        "base_manifest": base_manifest_bytes,
        "source_receipt": source_receipt_bytes,
        "browser_receipt": browser_bytes,
        "evidence_receipt": evidence_bytes,
        "visual_review": visual_bytes,
        "reviewer_authority": authority_bytes,
        "contact_sheet": contact_sheet_bytes,
    }
    paths = audit_relpaths(delivery_digest)
    artifacts = {
        name: build_audit_entry(relpath=paths[name], body=body)
        for name, body in audit_bodies.items()
    }
    accepted: dict[str, object] = {
        "contract_name": ACCEPTED_DELIVERY_CONTRACT,
        "provider": "magicfit",
        "provider_key": "magicfit",
        "provider_backend_key": "magicfit",
        "render_status": "completed",
        "status": "delivery_accepted",
        "acceptance_status": "accepted",
        "launch_eligible": True,
        "manifest_transform_contract": MANIFEST_TRANSFORM_CONTRACT,
        "requested_target_relpath": requested_target_relpath,
        "video_relpath": video_relpath,
        "video_sha256": video_sha256,
        "video_size_bytes": len(video_bytes),
        "source_receipt_sha256": source_receipt_sha256,
        "coverage_proof": coverage_proof,
        "base_manifest_sha256": base_manifest_sha256,
        "staged_manifest_sha256": staged_manifest_sha256,
        "delivery_digest": delivery_digest,
        "generated_at": generated_at,
        "review": {
            "contract_name": DELIVERY_REVIEW_CONTRACT,
            "reviewed_at": reviewed_at,
            "reviewer_authority_sha256": hashlib.sha256(
                authority_bytes
            ).hexdigest(),
            "reviewer_authorization": reviewer_authorization_projection,
            "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
            "visual_review_sha256": hashlib.sha256(visual_bytes).hexdigest(),
            "subject": {
                "tour_slug": slug,
                "provider": "magicfit",
                "delivery_contract_name": ACCEPTED_DELIVERY_CONTRACT,
                "manifest_transform_contract": MANIFEST_TRANSFORM_CONTRACT,
                "requested_target_relpath": requested_target_relpath,
                "source_receipt_sha256": source_receipt_sha256,
                "video_relpath": video_relpath,
                "video_sha256": video_sha256,
                "video_size_bytes": len(video_bytes),
                "coverage_proof": coverage_proof,
                "base_manifest_sha256": base_manifest_sha256,
                "staged_manifest_sha256": staged_manifest_sha256,
                "delivery_digest": delivery_digest,
            },
            "checklist": checklist,
        },
        "audit": {"contract_name": AUDIT_CONTRACT, "artifacts": artifacts},
    }
    (bundle / "tour.json").write_bytes(active_manifest_bytes)
    if include_scene:
        (bundle / "overview.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    video_path = bundle / video_relpath
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(video_bytes)
    video_path.chmod(0o444)
    for name, body in audit_bodies.items():
        artifact_path = bundle / paths[name]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(body)
    sidecar_path = bundle / sidecar_relpath
    sidecar_path.write_bytes(canonical_json_bytes(accepted))
    return accepted


def _synthetic_accepted_sidecar_path(
    root: Path,
    slug: str,
    accepted: dict[str, object],
) -> Path:
    delivery_digest = str(accepted.get("delivery_digest") or "")
    assert delivery_digest
    return root / slug / accepted_sidecar_relpath(delivery_digest)


def _persist_synthetic_accepted_sidecar(
    root: Path,
    slug: str,
    accepted: dict[str, object],
) -> None:
    _synthetic_accepted_sidecar_path(root, slug, accepted).write_bytes(
        canonical_json_bytes(accepted)
    )


def _rewrite_magicfit_review_receipts(
    bundle: Path,
    accepted: dict[str, object],
    *,
    browser: dict[str, object],
    evidence: dict[str, object],
    visual: dict[str, object],
) -> None:
    """Rebind a synthetic review chain so one structural mutation is isolated."""

    browser_bytes = canonical_json_bytes(browser)
    visual_bytes = canonical_json_bytes(visual)
    evidence_artifacts = evidence.get("artifacts")
    assert isinstance(evidence_artifacts, dict)
    evidence_artifacts["browser_receipt_sha256"] = hashlib.sha256(
        browser_bytes
    ).hexdigest()
    evidence_artifacts["visual_review_sha256"] = hashlib.sha256(
        visual_bytes
    ).hexdigest()
    evidence_bytes = canonical_json_bytes(evidence)

    review = accepted.get("review")
    assert isinstance(review, dict)
    review["evidence_sha256"] = hashlib.sha256(evidence_bytes).hexdigest()
    review["visual_review_sha256"] = hashlib.sha256(visual_bytes).hexdigest()
    audit = accepted.get("audit")
    assert isinstance(audit, dict)
    audit_artifacts = audit.get("artifacts")
    assert isinstance(audit_artifacts, dict)
    for name, body in (
        ("browser_receipt", browser_bytes),
        ("evidence_receipt", evidence_bytes),
        ("visual_review", visual_bytes),
    ):
        entry = audit_artifacts.get(name)
        assert isinstance(entry, dict)
        relpath = str(entry["relpath"])
        (bundle / relpath).write_bytes(body)
        audit_artifacts[name] = build_audit_entry(relpath=relpath, body=body)

    manifest = json.loads((bundle / "tour.json").read_text(encoding="utf-8"))
    sidecar_relpath = str(manifest["video_sidecar_relpath"])
    sidecar_path = bundle / sidecar_relpath
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_bytes(canonical_json_bytes(accepted))


def _write_equirectangular_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (2048, 1024), color=(28, 42, 36))
    image.save(path, format="JPEG")


def _attach_panorama_spatial_provenance(
    root: Path,
    slug: str,
    *,
    provider: str,
) -> None:
    bundle = root / slug
    payload = json.loads((bundle / "tour.json").read_text(encoding="utf-8"))
    private_path = bundle / "tour.private.json"
    private_payload = (
        json.loads(private_path.read_text(encoding="utf-8"))
        if private_path.is_file()
        else {}
    )
    merged = {**payload, **private_payload}
    if provider == "pano2vr":
        entry_relpath = str(
            merged.get("pano2vr_entry_relpath")
            or merged.get("pano2vr_export_entry_relpath")
            or ""
        )
        export_root = str(Path(entry_relpath).parent.as_posix())
        topology = pano2vr_export_topology(bundle / export_root)
        artifact = {
            "kind": "local_export",
            "sha256": panorama_export_tree_sha256(bundle / export_root),
            "entry_relpath": Path(entry_relpath).name,
        }
        key = PANO2VR_SPATIAL_PROVENANCE_KEY
        projection = "equirectangular"
    else:
        topology = walkable_scene_topology(merged)
        artifact = {
            "kind": "panorama_assets",
            "sha256": panorama_asset_set_sha256(
                bundle,
                panorama_asset_relpaths(merged),
            ),
            "entry_relpath": "",
        }
        key = KRPANO_SPATIAL_PROVENANCE_KEY
        walkable_scene = merged.get("walkable_scene")
        projection = str(
            walkable_scene.get("projection")
            if isinstance(walkable_scene, dict)
            else "equirectangular"
        )
        projection = {"panorama": "equirectangular", "cube": "cubemap"}.get(
            projection,
            projection,
        )
    private_payload[key] = {
        "schema": PANORAMA_SPATIAL_PROVENANCE_SCHEMA,
        "status": "pass",
        "provider": provider,
        "target_slug": slug,
        "artifact": artifact,
        "capture": {
            "source_kind": "camera_equirectangular",
            "projection": projection,
            **topology,
        },
        "authorization": {
            "status": "approved",
            "reference": f"fixture-authorization:{slug}",
        },
        "review": {
            "property_match": "pass",
            "visual_match": "pass",
            "spatial_capture_match": "pass",
            "flat_composite_absent": True,
            "reviewed_by": "fixture-reviewer",
            "reviewed_at": "2026-07-18T12:00:00+00:00",
        },
    }
    private_path.write_text(json.dumps(private_payload), encoding="utf-8")


def _clean_3dvista_proof() -> dict[str, object]:
    return {
        "three_d_vista_white_label_proof": {
            "source_project": "propertyquarry",
            "private_viewer_verified": True,
            "non_trial_export_verified": True,
            "propertyquarry_tour_metadata": True,
            "trial_branding_checked": True,
            "trial_branding_present": False,
        },
        "three_d_vista_browser_render_proof": {
            "provider": "3dvista",
            "status": "pass",
            "rendered_viewer": True,
        },
    }


def _clean_3dvista_target_provenance(
    slug: str,
    *,
    sha256: str,
    entry_relpath: str = "",
    target_subdir: str = "",
    kind: str = "local_export",
) -> dict[str, object]:
    return {
        "schema": THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
        "status": "pass",
        "provider": "3dvista",
        "target_slug": slug,
        "artifact": {
            "kind": kind,
            "sha256": sha256,
            "entry_relpath": entry_relpath,
        },
        "authorization": {
            "status": "approved",
            "reference": f"fixture-authorization:{slug}",
        },
        "review": {
            "property_match": "pass",
            "visual_match": "pass",
            "reviewed_by": "propertyquarry-test-reviewer",
            "reviewed_at": "2026-07-14T00:00:00+00:00",
        },
        "target_subdir": target_subdir,
    }


def _clean_3dvista_private_viewer_proof() -> dict[str, object]:
    proof = _clean_3dvista_proof()
    proof.pop("three_d_vista_browser_render_proof", None)
    return proof


def test_best_tour_root_prefers_fresher_runtime_snapshot(tmp_path: Path) -> None:
    sparse = tmp_path / "sparse"
    rich = tmp_path / "rich"
    (sparse / "only-one").mkdir(parents=True)
    (rich / "one").mkdir(parents=True)
    (rich / "two").mkdir(parents=True)
    (sparse / "only-one" / "tour.json").write_text("{}", encoding="utf-8")
    (rich / "one" / "tour.json").write_text("{}", encoding="utf-8")
    (rich / "two" / "tour.json").write_text("{}", encoding="utf-8")
    sparse_mtime = (sparse / "only-one" / "tour.json").stat().st_mtime
    os.utime(rich / "two" / "tour.json", (sparse_mtime + 5, sparse_mtime + 5))

    assert _best_tour_root([sparse, rich]) == rich


def test_best_tour_root_prefers_earlier_candidate_when_freshness_matches(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    for root, slug in ((repo_root, "current"), (runtime_root, "archive")):
        bundle = root / slug
        bundle.mkdir(parents=True)
        (bundle / "tour.json").write_text("{}", encoding="utf-8")
    shared_mtime = (repo_root / "current" / "tour.json").stat().st_mtime
    os.utime(runtime_root / "archive" / "tour.json", (shared_mtime, shared_mtime))

    assert _best_tour_root([repo_root, runtime_root]) == repo_root


def test_running_container_public_tour_dir_reads_docker_mount(monkeypatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime-public-tours"
    runtime_root.mkdir()
    monkeypatch.setenv("PROPERTYQUARRY_RUNTIME_CONTAINER", "propertyquarry-api")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=f"{runtime_root}\n", stderr=""),
    )

    assert _running_container_public_tour_dir() == runtime_root


def test_property_tour_control_verifier_live_probe_prefers_runtime_root_when_no_explicit_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "host"
    runtime_root = tmp_path / "runtime"
    _write_tour(
        host_root,
        "host-only-3dvista",
        {"three_d_vista_entry_relpath": "3dvista/index.html", **_clean_3dvista_private_viewer_proof()},
        {"3dvista/index.html": "<html><script src='tdvplayer.js'></script><div>tourviewer</div></html>"},
    )
    _write_tour(
        runtime_root,
        "runtime-matterport",
        {"matterport_url": "https://my.matterport.com/show/?m=READY123"},
    )
    monkeypatch.setattr("scripts.verify_property_tour_controls._tour_root", lambda: host_root)
    monkeypatch.setattr("scripts.verify_property_tour_controls._running_container_public_tour_dir", lambda *_args, **_kwargs: runtime_root)
    monkeypatch.setattr(
        "scripts.verify_property_tour_controls._probe_url",
        lambda *_args, **_kwargs: {"http_status": 200, "body_markers": {"matterport": True}},
    )

    receipt = build_property_tour_control_receipt(
        tour_root=None,
        base_url="https://propertyquarry.example",
        live_probe=True,
    )

    assert receipt["tour_root"] == str(runtime_root.resolve())
    assert receipt["tour_root_source"] == "runtime_container"
    assert receipt["tour_count"] == 1
    assert receipt["tours"][0]["slug"] == "runtime-matterport"
    assert receipt["provider_counts"]["matterport"] == 0
    assert receipt["provider_counts"]["3dvista"] == 0
    assert receipt["provider_blockers"]["matterport"]["reasons"][0]["reason"] == (
        "matterport_model_publication_missing_or_invalid"
    )


def test_property_tour_control_verifier_live_probe_uses_runtime_snapshot_when_mount_is_inaccessible(
    monkeypatch,
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "host"
    runtime_root = tmp_path / "runtime-snapshot"
    _write_tour(
        host_root,
        "host-only-3dvista",
        {"three_d_vista_entry_relpath": "3dvista/index.html", **_clean_3dvista_private_viewer_proof()},
        {"3dvista/index.html": "<html><script src='tdvplayer.js'></script><div>tourviewer</div></html>"},
    )
    _write_tour(
        runtime_root,
        "runtime-matterport",
        {"matterport_url": "https://my.matterport.com/show/?m=READY123"},
    )

    class _SnapshotHandle:
        def cleanup(self) -> None:
            return None

    monkeypatch.setattr("scripts.verify_property_tour_controls._tour_root", lambda: host_root)
    monkeypatch.setattr("scripts.verify_property_tour_controls._running_container_public_tour_dir", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "scripts.verify_property_tour_controls._snapshot_runtime_container_public_tours",
        lambda *_args, **_kwargs: (runtime_root, _SnapshotHandle()),
    )
    monkeypatch.setattr(
        "scripts.verify_property_tour_controls._probe_url",
        lambda *_args, **_kwargs: {"http_status": 200, "body_markers": {"matterport": True}},
    )

    receipt = build_property_tour_control_receipt(
        tour_root=None,
        base_url="https://propertyquarry.example",
        live_probe=True,
    )

    assert receipt["tour_root"] == str(runtime_root.resolve())
    assert receipt["tour_root_source"] == "runtime_container_snapshot"
    assert receipt["tour_count"] == 1
    assert receipt["tours"][0]["slug"] == "runtime-matterport"
    assert receipt["provider_counts"]["matterport"] == 0


def test_public_tour_control_labels_manual_video_as_video_evidence_not_walkthrough() -> None:
    html_body = _tour_control_external_iframe_html(
        title="Manual media loft",
        iframe_src="https://my.matterport.com/show/?m=abc123",
        badge="3D Tour",
        payload={
            "slug": "manual-media-loft",
            "video_provider": "manual_upload",
            "video_relpath": "tour.mp4",
            "_walkthrough_runtime_acceptance": {
                "allowed": True,
                "declared": True,
                "status": "accepted",
            },
            "_walkthrough_runtime_acceptance_token": (
                _PUBLIC_TOUR_RUNTIME_ACCEPTANCE_TOKEN
            ),
            "scenes": [{"name": "Living room", "asset_relpath": "living.jpg", "role": "photo"}],
        },
    )

    assert 'data-video-provider="manual_upload"' not in html_body
    assert 'data-walkthrough-ready="false"' not in html_body
    assert '<div class="card-label">Video</div>' not in html_body
    assert "Open walkthrough" in html_body
    assert "/tours/manual-media-loft/walkthrough" in html_body
    assert "MagicFit walkthrough" not in html_body
    assert '<div class="card-label">Walkthrough</div>' not in html_body
    assert "my.matterport.com" not in html_body
    assert 'data-src="about:blank"' in html_body


def test_public_tour_control_hides_unaccepted_magicfit_walkthrough() -> None:
    html_body = _tour_control_external_iframe_html(
        title="Walkthrough loft",
        iframe_src="https://propertyquarry.com/tours/files/walkthrough-loft/matterport.html",
        badge="Matterport Control",
        payload={
            "slug": "walkthrough-loft",
            "video_provider": "magicfit",
            "video_relpath": "walkthrough.mp4",
            "scenes": [{"name": "Living room", "asset_relpath": "living.jpg", "role": "photo"}],
        },
    )

    assert 'data-video-provider="magicfit"' not in html_body
    assert 'data-walkthrough-ready="true"' not in html_body
    assert '<div class="card-label">Walkthrough</div>' not in html_body
    assert "Open walkthrough" not in html_body
    assert "/tours/walkthrough-loft/walkthrough" not in html_body
    assert "magicfit" not in html_body
    assert "MagicFit walkthrough" not in html_body
    assert "Video evidence" not in html_body


def test_property_tour_control_verifier_rejects_unproven_private_matterport_control_without_url_leak(tmp_path: Path) -> None:
    _write_tour(tmp_path, "private-matterport", {})
    private_receipt = tmp_path / "private-matterport" / "tour.private.json"
    private_receipt.write_text(
        json.dumps({"matterport_url": "https://my.matterport.com/show/?m=PRIVATE123"}),
        encoding="utf-8",
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "blocked_missing_verified_controls"
    assert receipt["provider_counts"]["matterport"] == 0
    assert receipt["ready_provider_modes"] == []
    blocker = receipt["provider_blockers"]["matterport"]["reasons"][0]
    assert blocker["reason"] == "matterport_model_publication_missing_or_invalid"
    assert "model-publication" in blocker["action"]
    assert "PRIVATE123" not in json.dumps(receipt)


def test_property_tour_control_verifier_accepts_topology_verified_matterport(
    tmp_path: Path,
) -> None:
    model_sid = "CAPTURED123"
    _write_tour(tmp_path, "captured-matterport", {})
    private_receipt = tmp_path / "captured-matterport" / "tour.private.json"
    private_receipt.write_text(
        json.dumps(
            {
                "matterport_url": f"https://my.matterport.com/show/?m={model_sid}",
                "matterport_model_publication": _matterport_publication(model_sid),
            }
        ),
        encoding="utf-8",
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["matterport"] == 1
    assert "matterport" in receipt["ready_provider_modes"]
    control = receipt["delivery_contracts"]["matterport"]["ready_payload"][
        "sample_controls"
    ][0]
    assert control["control_path"] == "/tours/captured-matterport/control/matterport"
    assert model_sid not in json.dumps(receipt)


def test_property_tour_control_verifier_cli_loads_krpano_license_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("KRPANO_LICENSE_DOMAIN", raising=False)
    monkeypatch.delenv("KRPANO_LICENSE_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "KRPANO_LICENSE_DOMAIN=propertyquarry.com\nKRPANO_LICENSE_KEY=licensed-from-env-file\n",
        encoding="utf-8",
    )

    _load_cli_env_defaults()

    assert os.environ["KRPANO_LICENSE_DOMAIN"] == "propertyquarry.com"
    assert os.environ["KRPANO_LICENSE_KEY"] == "licensed-from-env-file"


def test_property_tour_control_verifier_accepts_private_receipt_3dvista_without_url_leak(tmp_path: Path) -> None:
    slug = "private-3dvista"
    provider_url = "https://example.3dvista.com/tours/PRIVATE3D/index.html"
    _write_tour(tmp_path, slug, _clean_3dvista_proof())
    private_receipt = tmp_path / "private-3dvista" / "tour.private.json"
    private_receipt.write_text(
        json.dumps(
            {
                "three_d_vista_url": provider_url,
                "three_d_vista_target_provenance": _clean_3dvista_target_provenance(
                    slug,
                    sha256=sha256_text(provider_url),
                    kind="hosted_url",
                ),
            }
        ),
        encoding="utf-8",
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "pass"
    assert receipt["provider_counts"]["3dvista"] == 1
    assert receipt["ready_provider_modes"] == ["3dvista"]
    assert receipt["core_required_provider_modes"] == ["matterport", "3dvista"]
    assert receipt["core_required_provider_mode_groups"] == [
        ["matterport", "3dvista"]
    ]
    assert receipt["core_requirement_satisfied"] is True
    assert receipt["advanced_visual_required_provider_modes"] == ["magicfit"]
    assert receipt["core_missing_provider_modes"] == []
    assert receipt["advanced_visual_missing_provider_modes"] == ["magicfit"]
    assert receipt["operator_missing_provider_modes"] == ["magicfit"]
    assert "PRIVATE3D" not in json.dumps(receipt)


def test_core_gold_accepts_verified_3dvista_when_matterport_probe_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    slug = "licensed-3dvista-core"
    model_sid = "ALTERNATIVE1"
    provider_url = "https://example.3dvista.com/tours/COREALT/index.html"
    _write_tour(tmp_path, slug, _clean_3dvista_proof())
    (tmp_path / slug / "tour.private.json").write_text(
        json.dumps(
            {
                "matterport_url": (
                    f"https://my.matterport.com/show/?m={model_sid}"
                ),
                "matterport_model_publication": _matterport_publication(
                    model_sid
                ),
                "three_d_vista_url": provider_url,
                "three_d_vista_target_provenance": (
                    _clean_3dvista_target_provenance(
                        slug,
                        sha256=sha256_text(provider_url),
                        kind="hosted_url",
                    )
                ),
            }
        ),
        encoding="utf-8",
    )

    def _probe(*_args, provider: str = "", **_kwargs) -> dict[str, object]:
        if provider == "matterport":
            return {"http_status": 503, "error": "matterport unavailable"}
        return {
            "http_status": 200,
            "body_markers": {"matterport": False, "3dvista": True},
        }

    monkeypatch.setattr(
        "scripts.verify_property_tour_controls._probe_url",
        _probe,
    )

    receipt = build_property_tour_control_receipt(
        tour_root=tmp_path,
        base_url="https://propertyquarry.example",
        live_probe=True,
        require_all_provider_modes=True,
        gold_scope="core",
    )

    assert receipt["status"] == "pass"
    assert receipt["ready_provider_modes"] == ["3dvista"]
    assert receipt["core_missing_provider_modes"] == []
    assert receipt["selected_missing_provider_modes"] == []
    assert receipt["provider_probe_failures"] == {
        "global_count": 1,
        "selected_fatal_count": 0,
        "by_provider": {"matterport": 1, "3dvista": 0, "magicfit": 0},
    }


def test_property_tour_control_verifier_selects_required_modes_by_gold_scope(
    tmp_path: Path,
) -> None:
    slug = "scope-matterport"
    model_sid = "SCOPEMATTER"
    _write_tour(tmp_path, slug, {})
    (tmp_path / slug / "tour.private.json").write_text(
        json.dumps(
            {
                "matterport_url": (
                    f"https://my.matterport.com/show/?m={model_sid}"
                ),
                "matterport_model_publication": _matterport_publication(
                    model_sid
                ),
            }
        ),
        encoding="utf-8",
    )

    core = build_property_tour_control_receipt(
        tour_root=tmp_path,
        require_all_provider_modes=True,
        gold_scope="core",
    )
    advanced = build_property_tour_control_receipt(
        tour_root=tmp_path,
        require_all_provider_modes=True,
        gold_scope="advanced_visual",
    )

    assert core["status"] == "pass"
    assert core["gold_scope"] == "core"
    assert core["selected_required_provider_modes"] == ["matterport", "3dvista"]
    assert core["selected_missing_provider_modes"] == []
    assert core["operator_missing_provider_modes"] == ["magicfit"]
    assert advanced["status"] == "blocked_missing_provider_modes"
    assert advanced["gold_scope"] == "advanced_visual"
    assert advanced["selected_required_provider_modes"] == [
        "matterport",
        "3dvista",
        "magicfit",
    ]
    assert advanced["selected_missing_provider_modes"] == ["magicfit"]


@pytest.mark.parametrize(
    ("gold_scope", "expected_status", "expected_selected_failures"),
    (
        ("core", "pass", 0),
        ("advanced_visual", "blocked_missing_provider_modes", 0),
    ),
)
def test_property_tour_control_verifier_scopes_broken_magicfit_probe_failure(
    tmp_path: Path,
    monkeypatch,
    gold_scope: str,
    expected_status: str,
    expected_selected_failures: int,
) -> None:
    slug = "scope-probe-tour"
    model_sid = "SCOPEPROBE1"
    provider_url = "https://example.3dvista.com/tours/SCOPEPROBE/index.html"
    _write_tour(
        tmp_path,
        slug,
        {
            **_clean_3dvista_proof(),
            "video_provider": "magicfit",
            "video_url": (
                "https://propertyquarry.com/tours/files/"
                f"{slug}/walkthrough.mp4"
            ),
        },
    )
    (tmp_path / slug / "tour.private.json").write_text(
        json.dumps(
            {
                "matterport_url": (
                    f"https://my.matterport.com/show/?m={model_sid}"
                ),
                "matterport_model_publication": _matterport_publication(
                    model_sid
                ),
                "three_d_vista_url": provider_url,
                "three_d_vista_target_provenance": (
                    _clean_3dvista_target_provenance(
                        slug,
                        sha256=sha256_text(provider_url),
                        kind="hosted_url",
                    )
                ),
            }
        ),
        encoding="utf-8",
    )

    def _scope_probe(*_args, provider: str = "", **_kwargs) -> dict[str, object]:
        if provider == "magicfit":
            return {"http_status": 503, "error": "magicfit unavailable"}
        return {
            "http_status": 200,
            "body_markers": {"matterport": True, "3dvista": True},
        }

    monkeypatch.setattr(
        "scripts.verify_property_tour_controls._probe_url",
        _scope_probe,
    )

    receipt = build_property_tour_control_receipt(
        tour_root=tmp_path,
        base_url="https://propertyquarry.example",
        live_probe=True,
        require_all_provider_modes=True,
        gold_scope=gold_scope,
    )

    controls = {row["provider"]: row for row in receipt["tours"][0]["controls"]}
    assert receipt["status"] == expected_status
    assert receipt["provider_counts"]["matterport"] == 1
    assert receipt["provider_counts"]["3dvista"] == 1
    assert receipt["provider_counts"]["magicfit"] == 0
    assert receipt["operator_missing_provider_modes"] == ["magicfit"]
    assert receipt["provider_probe_failures"] == {
        "global_count": 0,
        "selected_fatal_count": expected_selected_failures,
        "by_provider": {"matterport": 0, "3dvista": 0, "magicfit": 0},
    }
    assert controls["matterport"]["status"] == "ready"
    assert controls["3dvista"]["status"] == "ready"
    assert "magicfit" not in controls


def test_property_tour_control_verifier_accepts_private_receipt_pano2vr_without_path_leak(tmp_path: Path) -> None:
    _write_tour(
        tmp_path,
        "private-pano2vr",
        {},
        {
            "pano2vr/private-entry.html": "<!doctype html><script src='tour.js'></script><div>Pano2VR</div>",
            "pano2vr/pano.xml": "<panorama id='node1'><hotspots /></panorama>",
        },
    )
    private_receipt = tmp_path / "private-pano2vr" / "tour.private.json"
    private_receipt.write_text(
        json.dumps(
            {
                "pano2vr_entry_relpath": "pano2vr/private-entry.html",
                "listing_url": "https://private.example.test/pano2vr-source",
                "source_ref": "PRIVATEPANO2VR",
            }
        ),
        encoding="utf-8",
    )
    _attach_panorama_spatial_provenance(
        tmp_path,
        "private-pano2vr",
        provider="pano2vr",
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "pass"
    assert receipt["provider_counts"]["pano2vr"] == 1
    assert receipt["ready_provider_modes"] == ["pano2vr"]
    serialized = json.dumps(receipt)
    assert "PRIVATEPANO2VR" not in serialized
    assert "private.example.test" not in serialized
    assert "private-entry" not in serialized


def test_property_tour_control_verifier_summary_omits_tour_rows(tmp_path: Path) -> None:
    _write_tour(tmp_path, "matterport-tour", {"matterport_url": "https://my.matterport.com/show/?m=SUMMARY123"})

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)
    summary = _receipt_summary(receipt)

    assert summary["status"] == "blocked_missing_verified_controls"
    assert summary["provider_counts"]["matterport"] == 0
    assert "tours" not in summary
    assert "SUMMARY123" not in json.dumps(summary)


def test_property_tour_control_verifier_next_actions_only_include_globally_missing_modes(tmp_path: Path) -> None:
    _write_tour(tmp_path, "matterport-tour", {"matterport_url": "https://my.matterport.com/show/?m=READY123"})
    _write_tour(tmp_path, "blocked-gallery", {"scene_strategy": "photo_gallery_hosted"})

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["ready_provider_modes"] == []
    assert set(receipt["missing_provider_modes"]) == {
        "matterport",
        "3dvista",
        "magicfit",
    }
    assert receipt["core_missing_provider_modes"] == ["matterport", "3dvista"]
    assert receipt["advanced_visual_missing_provider_modes"] == ["magicfit"]
    assert {row["provider"] for row in receipt["next_required_actions"]} == {
        "matterport",
        "3dvista",
        "magicfit",
    }


def test_property_tour_control_verifier_can_require_all_provider_modes_for_gold_gate(tmp_path: Path) -> None:
    _write_tour(tmp_path, "matterport-tour", {"matterport_url": "https://my.matterport.com/show/?m=READY123"})

    receipt = build_property_tour_control_receipt(tour_root=tmp_path, require_all_provider_modes=True)
    summary = _receipt_summary(receipt)

    assert receipt["status"] == "blocked_missing_provider_modes"
    assert receipt["require_all_provider_modes"] is True
    assert summary["require_all_provider_modes"] is True
    assert receipt["ready_provider_modes"] == []
    assert set(receipt["missing_provider_modes"]) == {
        "matterport",
        "3dvista",
        "magicfit",
    }
    assert receipt["core_missing_provider_modes"] == ["matterport", "3dvista"]
    assert receipt["advanced_visual_missing_provider_modes"] == ["magicfit"]
    assert summary["core_missing_provider_modes"] == ["matterport", "3dvista"]
    assert summary["advanced_visual_missing_provider_modes"] == ["magicfit"]
    assert {row["provider"] for row in receipt["next_required_actions"]} == {
        "matterport",
        "3dvista",
        "magicfit",
    }
    assert summary["provider_blockers"]["3dvista"]["blocked_count"] == 1
    assert summary["provider_blockers"]["3dvista"]["reasons"][0]["reason"] == "missing_3dvista_export"
    assert summary["provider_blockers"]["pano2vr"]["reasons"][0]["reason"] == "missing_pano2vr_export"
    assert summary["provider_blockers"]["magicfit"]["reasons"][0]["reason"] == "missing_magicfit_walkthrough"


def test_property_tour_control_verifier_cli_fails_closed_for_blocked_gold_gate(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_tour(tmp_path, "matterport-tour", {"matterport_url": "https://my.matterport.com/show/?m=READY123"})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_property_tour_controls.py",
            "--tour-root",
            str(tmp_path),
            "--require-all-provider-modes",
            "--fail-on-blocked",
            "--summary-only",
        ],
    )

    exit_code = main()

    assert exit_code == 2
    output = capsys.readouterr().out
    assert '"status": "blocked_missing_provider_modes"' in output
    assert '"missing_provider_modes"' in output


def test_property_release_gate_fails_before_live_work_when_tour_modes_are_blocked() -> None:
    release_gate = (Path(__file__).resolve().parents[1] / "scripts" / "property_release_gates.sh").read_text(
        encoding="utf-8"
    )

    assert (
        'scripts/verify_property_tour_controls.py \\\n'
        '  --require-all-provider-modes \\\n'
        '  --gold-scope "${gold_scope}" \\\n'
        '  --fail-on-blocked \\\n'
        in release_gate
    )


def test_property_tour_control_verifier_cli_delegates_live_probe_to_runtime_container_when_mount_is_inaccessible(
    monkeypatch,
    capsys,
) -> None:
    delegated_receipt = {
        "generated_at": "2026-07-04T21:20:00+00:00",
        "status": "pass",
        "tour_root": "/data/public_property_tours",
        "tour_root_source": "explicit",
        "tour_count": 1,
        "ready_tour_count": 1,
        "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 0, "krpano": 0, "magicfit": 1},
        "provider_blockers": {provider: {"blocked_count": 0, "reasons": []} for provider in ("matterport", "3dvista", "pano2vr", "krpano", "magicfit")},
        "ready_provider_modes": ["3dvista", "magicfit", "matterport"],
        "required_provider_modes": ["matterport", "3dvista", "magicfit"],
        "missing_provider_modes": [],
        "next_required_actions": [],
        "live_probe": True,
        "base_url": "https://propertyquarry.example",
        "require_all_provider_modes": False,
        "tours": [],
    }
    monkeypatch.setattr("scripts.verify_property_tour_controls._running_container_public_tour_dir", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "scripts.verify_property_tour_controls.build_property_tour_control_receipt",
        lambda **_kwargs: {
            "generated_at": "2026-07-04T21:19:00+00:00",
            "status": "blocked_no_tour_manifests",
            "tour_root": "/docker/property/state/public_property_tours",
            "tour_root_source": "preferred",
            "tour_count": 0,
            "ready_tour_count": 0,
            "provider_counts": {"matterport": 0, "3dvista": 0, "pano2vr": 0, "krpano": 0, "magicfit": 0},
            "provider_blockers": {
                provider: {"blocked_count": 0, "reasons": []}
                for provider in ("matterport", "3dvista", "pano2vr", "krpano", "magicfit")
            },
            "ready_provider_modes": [],
            "required_provider_modes": ["matterport", "3dvista", "magicfit"],
            "missing_provider_modes": ["matterport", "3dvista", "magicfit"],
            "next_required_actions": [],
            "live_probe": True,
            "base_url": "https://propertyquarry.example",
            "require_all_provider_modes": False,
            "tours": [],
        },
    )
    monkeypatch.setattr(
        "scripts.verify_property_tour_controls._runtime_container_live_probe_receipt",
        lambda **_kwargs: (dict(delegated_receipt), 0),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_property_tour_controls.py",
            "--base-url",
            "https://propertyquarry.example",
            "--live-probe",
            "--summary-only",
        ],
    )

    exit_code = main()

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"status": "pass"' in output
    assert '"tour_root": "/data/public_property_tours"' in output


def test_runtime_container_live_probe_receipt_rewrites_loopback_base_url(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr("scripts.verify_property_tour_controls.shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr("scripts.verify_property_tour_controls._runtime_container_name", lambda: "propertyquarry-api")

    def _run(command, **_kwargs):
        commands.append(list(command))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "generated_at": "2026-07-05T08:50:00+00:00",
                    "status": "pass",
                    "tour_root": "/data/public_property_tours",
                    "tour_root_source": "preferred",
                    "tour_count": 1,
                    "ready_tour_count": 1,
                    "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 0, "krpano": 0, "magicfit": 1},
                    "provider_blockers": {
                        provider: {"blocked_count": 0, "reasons": []}
                        for provider in ("matterport", "3dvista", "pano2vr", "krpano", "magicfit")
                    },
                    "ready_provider_modes": ["3dvista", "magicfit", "matterport"],
                    "required_provider_modes": ["matterport", "3dvista", "magicfit"],
                    "missing_provider_modes": [],
                    "next_required_actions": [],
                    "live_probe": True,
                    "base_url": "http://127.0.0.1:8090",
                    "require_all_provider_modes": False,
                    "tours": [],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("scripts.verify_property_tour_controls.subprocess.run", _run)

    receipt, exit_code = _runtime_container_live_probe_receipt(
        base_url="http://127.0.0.1:8097",
        host_header="propertyquarry.com",
        timeout_seconds=5.0,
        require_all_provider_modes=False,
    )

    assert exit_code == 0
    assert receipt is not None
    assert "--base-url" in commands[0]
    assert "http://127.0.0.1:8090" in commands[0]
    assert receipt["host_requested_base_url"] == "http://127.0.0.1:8097"
    assert receipt["container_probe_base_url"] == "http://127.0.0.1:8090"
    assert receipt["base_url"] == "http://127.0.0.1:8090"


def test_property_tour_control_verifier_counts_provider_gaps_on_unproven_matterport_tours(tmp_path: Path) -> None:
    _write_tour(tmp_path, "matterport-only", {"matterport_url": "https://my.matterport.com/show/?m=READY123"})

    receipt = build_property_tour_control_receipt(tour_root=tmp_path, require_all_provider_modes=True)

    actions = {row["provider"]: row for row in receipt["next_required_actions"]}
    missing = {row["provider"]: row for row in receipt["tours"][0]["missing_evidence"]}
    assert receipt["status"] == "blocked_missing_provider_modes"
    assert receipt["tours"][0]["status"] == "blocked_missing_verified_controls"
    assert set(receipt["required_provider_modes"]) == {
        "matterport",
        "3dvista",
        "magicfit",
    }
    assert set(missing) == {"matterport", "3dvista", "magicfit"}
    assert (
        missing["matterport"]["reason"]
        == "matterport_model_publication_missing_or_invalid"
    )
    assert missing["magicfit"]["reason"] == "missing_magicfit_walkthrough"
    assert set(receipt["tours"][0]["missing_provider_modes"]) == {
        "matterport",
        "3dvista",
        "magicfit",
    }
    assert actions["matterport"]["blocked_tour_count"] == 1
    assert actions["3dvista"]["blocked_tour_count"] == 1
    assert actions["magicfit"]["blocked_tour_count"] == 1


def test_property_tour_control_verifier_distinguishes_empty_provider_placeholder_fields(tmp_path: Path) -> None:
    _write_tour(
        tmp_path,
        "placeholder-fields",
        {
            "matterport_url": "https://my.matterport.com/show/?m=READY123",
            "three_d_vista_url": "",
            "pano2vr_entry_relpath": "",
        },
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path, require_all_provider_modes=True)

    missing = {row["provider"]: row for row in receipt["tours"][0]["missing_evidence"]}
    optional = {
        row["provider"]: row
        for row in receipt["tours"][0]["optional_missing_evidence"]
    }
    assert (
        receipt["provider_blockers"]["3dvista"]["reasons"][0]["reason"]
        == "3dvista_placeholder_field_empty_or_unusable"
    )
    assert "pano2vr" not in missing
    assert set(optional) == {"pano2vr"}
    assert receipt["provider_blockers"]["pano2vr"]["reasons"][0]["reason"] == "pano2vr_placeholder_field_empty_or_unusable"


def test_property_tour_control_verifier_reports_all_verified_provider_modes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "licensed")
    model_sid = "ALLMODES123"
    _write_tour(
        tmp_path,
        "matterport-tour",
        {
            "matterport_url": (
                f"https://my.matterport.com/show/?m={model_sid}"
            ),
            "matterport_model_publication": _matterport_publication(model_sid),
        },
    )
    _write_tour(
        tmp_path,
        "3dvista-tour",
        {"three_d_vista_entry_relpath": "3dvista/index.html", **_clean_3dvista_proof()},
        {
            "3dvista/index.html": "<html><script src='runtime/app.js'></script><div>3DVista shell</div></html>",
            "3dvista/runtime/app.js": "window.TDVPlayer = true;",
        },
    )
    _write_tour(
        tmp_path,
        "pano2vr-tour",
        {"pano2vr_entry_relpath": "pano/index.html"},
        {
            "pano/index.html": "<html><script src='assets/viewer.js'></script><span>pano.xml</span></html>",
            "pano/assets/viewer.js": "window.GGSKIN = true;",
            "pano/pano.xml": "<panorama id='node1'><hotspots /></panorama>",
        },
    )
    _attach_panorama_spatial_provenance(
        tmp_path,
        "pano2vr-tour",
        provider="pano2vr",
    )
    panorama = tmp_path / "verified-panorama.jpg"
    _write_equirectangular_image(panorama)
    _write_tour(
        tmp_path,
        "krpano-tour",
        {
            "scene_strategy": "single_panorama",
            "creation_mode": "hosted_panorama_360",
            "walkable_scene": {"projection": "equirectangular", "panorama_relpath": "krpano/panorama.jpg"},
        },
        {"krpano/panorama.jpg": panorama.read_bytes()},
    )
    _attach_panorama_spatial_provenance(
        tmp_path,
        "krpano-tour",
        provider="krpano",
    )
    playable_magicfit = tmp_path / "walkthrough.mp4"
    _write_playable_mp4(playable_magicfit)
    playable_magicfit_bytes = playable_magicfit.read_bytes()
    accepted_magicfit = _write_reproducible_magicfit_tour(
        tmp_path, "magicfit-tour", playable_magicfit_bytes
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "pass"
    assert receipt["provider_counts"] == {
        "matterport": 1,
        "3dvista": 1,
        "pano2vr": 1,
        "krpano": 1,
        "magicfit": 1,
    }
    assert receipt["missing_provider_modes"] == []
    assert receipt["magicfit_playback"]["playback_ok"] is True
    magicfit_control_path = (
        "/tours/files/magicfit-tour/" + str(accepted_magicfit["video_relpath"])
    )
    assert receipt["magicfit_playback"]["evidence"] == [
        {
            "slug": "magicfit-tour",
            "provider": "magicfit",
            "evidence": "local_magicfit_playable_video",
            "control_path": magicfit_control_path,
            "media_identity": magicfit_control_path,
        }
    ]
    assert all("matterport.com/show" not in json.dumps(tour) for tour in receipt["tours"])


@pytest.mark.parametrize("gold_scope", ("core", "advanced_visual"))
def test_property_tour_control_verifier_does_not_count_failed_live_probe_as_ready(
    tmp_path: Path,
    monkeypatch,
    gold_scope: str,
) -> None:
    _write_tour(
        tmp_path,
        "matterport-tour",
        {
            "matterport_url": "https://my.matterport.com/show/?m=PROBEFAIL1",
            "matterport_model_publication": _matterport_publication(
                "PROBEFAIL1"
            ),
        },
    )

    def _failed_probe(*_args, **_kwargs) -> dict[str, object]:
        return {"http_status": 503, "error": "unavailable"}

    monkeypatch.setattr("scripts.verify_property_tour_controls._probe_url", _failed_probe)

    receipt = build_property_tour_control_receipt(
        tour_root=tmp_path,
        base_url="https://propertyquarry.example",
        live_probe=True,
        gold_scope=gold_scope,
    )

    assert receipt["status"] == "fail"
    assert receipt["provider_counts"]["matterport"] == 0
    assert "matterport" not in receipt["ready_provider_modes"]
    assert "matterport" in receipt["missing_provider_modes"]
    assert receipt["provider_probe_failures"] == {
        "global_count": 1,
        "selected_fatal_count": 1,
        "by_provider": {"matterport": 1, "3dvista": 0, "magicfit": 0},
    }
    assert receipt["tours"][0]["controls"][0]["status"] == "probe_failed"


def test_property_tour_control_verifier_keeps_hidden_optional_pano2vr_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_tour(
        tmp_path,
        "matterport-with-hidden-pano2vr",
        {
            "matterport_url": "https://my.matterport.com/show/?m=READY123",
            "matterport_model_publication": _matterport_publication("READY123"),
            "pano2vr_entry_relpath": "pano/index.html",
        },
        {
            "pano/index.html": "<!doctype html><script src='tour.js'></script><div>Pano2VR</div>",
            "pano/pano.xml": "<panorama id='node1'><hotspots /></panorama>",
        },
    )
    _attach_panorama_spatial_provenance(
        tmp_path,
        "matterport-with-hidden-pano2vr",
        provider="pano2vr",
    )

    def _probe(url: str, *, provider: str = "", **_kwargs) -> dict[str, object]:
        if provider == "pano2vr":
            return {"http_status": 404, "error": "hidden", "error_code": "tour_control_panorama_export_hidden"}
        return {"http_status": 200, "body_markers": {"matterport": True}}

    monkeypatch.setattr("scripts.verify_property_tour_controls._probe_url", _probe)

    receipt = build_property_tour_control_receipt(
        tour_root=tmp_path,
        base_url="https://propertyquarry.example",
        live_probe=True,
        require_all_provider_modes=True,
    )

    controls = {row["provider"]: row for row in receipt["tours"][0]["controls"]}
    assert receipt["status"] == "pass"
    assert receipt["provider_counts"]["matterport"] == 1
    assert receipt["provider_counts"]["pano2vr"] == 1
    assert receipt["ready_provider_modes"] == ["matterport", "pano2vr"]
    assert receipt["hidden_ready_provider_modes"] == ["pano2vr"]
    assert receipt["missing_provider_modes"] == ["magicfit"]
    assert controls["matterport"]["status"] == "ready"
    assert controls["pano2vr"]["status"] == "ready"
    assert controls["pano2vr"]["route_visibility"] == "hidden_by_product_boundary"


def test_property_tour_control_verifier_keeps_hidden_optional_krpano_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_tour(
        tmp_path,
        "matterport-with-hidden-krpano",
        {
            "matterport_url": "https://my.matterport.com/show/?m=READY123",
            "matterport_model_publication": _matterport_publication("READY123"),
            "walkable_scene": {"projection": "equirectangular", "panorama_relpath": "krpano/panorama.jpg"},
        },
    )
    _write_equirectangular_image(tmp_path / "matterport-with-hidden-krpano" / "krpano" / "panorama.jpg")
    _attach_panorama_spatial_provenance(
        tmp_path,
        "matterport-with-hidden-krpano",
        provider="krpano",
    )
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "demo-license")

    def _probe(url: str, *, provider: str = "", **_kwargs) -> dict[str, object]:
        if provider == "krpano":
            return {"http_status": 404, "error": "hidden", "error_code": "tour_control_panorama_export_hidden"}
        return {"http_status": 200, "body_markers": {"matterport": True}}

    monkeypatch.setattr("scripts.verify_property_tour_controls._probe_url", _probe)

    receipt = build_property_tour_control_receipt(
        tour_root=tmp_path,
        base_url="https://propertyquarry.example",
        live_probe=True,
        require_all_provider_modes=True,
    )

    controls = {row["provider"]: row for row in receipt["tours"][0]["controls"]}
    assert receipt["status"] == "pass"
    assert receipt["provider_counts"]["matterport"] == 1
    assert receipt["provider_counts"]["krpano"] == 1
    assert receipt["ready_provider_modes"] == ["krpano", "matterport"]
    assert receipt["hidden_ready_provider_modes"] == ["krpano"]
    assert receipt["missing_provider_modes"] == ["magicfit"]
    assert controls["matterport"]["status"] == "ready"
    assert controls["krpano"]["status"] == "ready"
    assert controls["krpano"]["route_visibility"] == "hidden_by_product_boundary"


def test_property_tour_control_verifier_marks_optional_pano2vr_probe_failed_when_hidden_code_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_tour(
        tmp_path,
        "matterport-with-broken-pano2vr",
        {
            "matterport_url": "https://my.matterport.com/show/?m=READY123",
            "pano2vr_entry_relpath": "pano/index.html",
        },
        {
            "pano/index.html": "<!doctype html><script src='tour.js'></script><div>Pano2VR</div>",
            "pano/pano.xml": "<panorama id='node1'><hotspots /></panorama>",
        },
    )
    _attach_panorama_spatial_provenance(
        tmp_path,
        "matterport-with-broken-pano2vr",
        provider="pano2vr",
    )

    def _probe(url: str, *, provider: str = "", **_kwargs) -> dict[str, object]:
        if provider == "pano2vr":
            return {"http_status": 404, "error": "missing"}
        return {"http_status": 200, "body_markers": {"matterport": True}}

    monkeypatch.setattr("scripts.verify_property_tour_controls._probe_url", _probe)

    receipt = build_property_tour_control_receipt(
        tour_root=tmp_path,
        base_url="https://propertyquarry.example",
        live_probe=True,
        require_all_provider_modes=True,
    )

    controls = {row["provider"]: row for row in receipt["tours"][0]["controls"]}
    assert receipt["provider_counts"]["pano2vr"] == 0
    assert receipt["ready_provider_modes"] == []
    assert "hidden_ready_provider_modes" in receipt
    assert receipt["hidden_ready_provider_modes"] == []
    assert controls["pano2vr"]["status"] == "optional_probe_failed"


def test_property_tour_control_verifier_rejects_wrong_provider_live_probe_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_tour(
        tmp_path,
        "matterport-tour",
        {
            "matterport_url": "https://my.matterport.com/show/?m=WRONGPROBE1",
            "matterport_model_publication": _matterport_publication(
                "WRONGPROBE1"
            ),
        },
    )

    def _wrong_provider_probe(*_args, **_kwargs) -> dict[str, object]:
        return {"http_status": 200, "body_markers": {"matterport": False}}

    monkeypatch.setattr("scripts.verify_property_tour_controls._probe_url", _wrong_provider_probe)

    receipt = build_property_tour_control_receipt(
        tour_root=tmp_path,
        base_url="https://propertyquarry.example",
        live_probe=True,
    )

    assert receipt["status"] == "fail"
    assert receipt["provider_counts"]["matterport"] == 0
    assert "matterport" not in receipt["ready_provider_modes"]
    assert receipt["tours"][0]["controls"][0]["status"] == "probe_failed"


def test_property_tour_control_verifier_counts_successful_3dvista_live_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_tour(
        tmp_path,
        "3dvista-tour",
        {"three_d_vista_entry_relpath": "3dvista/index.html", **_clean_3dvista_private_viewer_proof()},
        {"3dvista/index.html": "<html><script src='tdvplayer.js'></script><div>tourviewer</div></html>"},
    )

    def _successful_3dvista_probe(*_args, **_kwargs) -> dict[str, object]:
        return {"http_status": 200, "body_markers": {"3dvista": True}}

    monkeypatch.setattr("scripts.verify_property_tour_controls._probe_url", _successful_3dvista_probe)

    receipt = build_property_tour_control_receipt(
        tour_root=tmp_path,
        base_url="https://propertyquarry.example",
        live_probe=True,
    )

    assert receipt["status"] == "pass"
    assert receipt["provider_counts"]["3dvista"] == 1
    assert receipt["ready_provider_modes"] == ["3dvista"]
    assert receipt["tours"][0]["controls"][0]["evidence"] == "local_3dvista_export_entry"


def test_property_tour_control_verifier_rejects_magicfit_placeholder_video(tmp_path: Path) -> None:
    _write_tour(
        tmp_path,
        "magicfit-placeholder",
        {"video_provider": "magicfit", "video_relpath": "walkthrough.mp4"},
        {"walkthrough.mp4": "video"},
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "blocked_missing_verified_controls"
    assert receipt["provider_counts"]["magicfit"] == 0
    assert receipt["tours"][0]["blocked_reason"] == "missing_verified_provider_control"


def test_property_tour_control_verifier_rejects_magicfit_signature_only_stub(tmp_path: Path) -> None:
    signature_only = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    _write_reproducible_magicfit_tour(
        tmp_path, "magicfit-stub", signature_only
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "blocked_missing_verified_controls"
    assert receipt["provider_counts"]["magicfit"] == 0
    missing = {row["provider"]: row for row in receipt["tours"][0]["missing_evidence"]}
    assert missing["magicfit"]["reason"] == "magicfit_video_missing_or_unplayable"


def test_property_tour_control_verifier_rejects_disqualified_magicfit_delivery_receipts(tmp_path: Path) -> None:
    playable_magicfit = tmp_path / "walkthrough.mp4"
    _write_playable_mp4(playable_magicfit)
    disqualifications = {
        "acceptance-status": {"acceptance_status": "disqualified"},
        "launch-eligibility": {"launch_eligible": False},
        "explicit-disqualification": {"disqualification": {"reason_codes": ["visible_rotation_jump"]}},
    }
    for slug, delivery_receipt in disqualifications.items():
        _write_tour(
            tmp_path,
            slug,
            {"video_provider": "magicfit", "video_relpath": "walkthrough.mp4"},
            {
                "walkthrough.mp4": playable_magicfit.read_bytes(),
                "tour.magicfit.json": json.dumps(delivery_receipt),
            },
        )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "blocked_missing_verified_controls"
    assert receipt["provider_counts"]["magicfit"] == 0
    assert receipt["ready_provider_modes"] == []
    for tour in receipt["tours"]:
        missing = {row["provider"]: row for row in tour["missing_evidence"]}
        assert missing["magicfit"]["reason"] == "magicfit_walkthrough_disqualified"
        assert "replacement MagicFit walkthrough" in missing["magicfit"]["action"]


def test_property_tour_control_verifier_binds_magicfit_acceptance_to_active_video(
    tmp_path: Path,
) -> None:
    playable_magicfit = tmp_path / "walkthrough.mp4"
    _write_playable_mp4(playable_magicfit)
    video_bytes = playable_magicfit.read_bytes()
    _write_reproducible_magicfit_tour(tmp_path, "accepted-binding", video_bytes)
    _write_reproducible_magicfit_tour(
        tmp_path,
        "accepted-utc-offset-generated-at",
        video_bytes,
        generated_at="2024-01-01T00:00:00+00:00",
    )

    stale_relpath = _write_reproducible_magicfit_tour(
        tmp_path, "stale-relpath-binding", video_bytes
    )
    stale_relpath_bundle = tmp_path / "stale-relpath-binding"
    stale_relpath_manifest_path = stale_relpath_bundle / "tour.json"
    stale_relpath_manifest = json.loads(
        stale_relpath_manifest_path.read_text(encoding="utf-8")
    )
    stale_relpath_manifest["video_relpath"] = "replacement.mp4"
    stale_relpath_manifest_path.write_bytes(
        canonical_json_bytes(stale_relpath_manifest)
    )
    (stale_relpath_bundle / "replacement.mp4").write_bytes(video_bytes)
    assert stale_relpath["video_relpath"] != "replacement.mp4"

    stale_hash = _write_reproducible_magicfit_tour(
        tmp_path, "stale-hash-binding", video_bytes
    )
    stale_hash_path = (
        tmp_path / "stale-hash-binding" / str(stale_hash["video_relpath"])
    )
    stale_hash_path.chmod(0o644)
    stale_hash_path.write_bytes(video_bytes + b"stale-active-video")

    _write_tour(
        tmp_path,
        "unbound-acceptance",
        {
            "video_provider": "magicfit",
            "video_relpath": "walkthrough.mp4",
            "video_sidecar_relpath": "tour.magicfit.json",
        },
        {
            "walkthrough.mp4": video_bytes,
            "tour.magicfit.json": json.dumps(
                {"acceptance_status": "accepted", "launch_eligible": True}
            ),
        },
    )
    _write_tour(
        tmp_path,
        "missing-acceptance",
        {
            "video_provider": "magicfit",
            "video_relpath": "walkthrough.mp4",
            "video_sidecar_relpath": "tour.magicfit.json",
        },
        {"walkthrough.mp4": video_bytes},
    )
    invalid_sidecars: list[tuple[str, dict[str, object]]] = []
    for slug, field in (
        ("missing-acceptance-status", "acceptance_status"),
        ("missing-launch-eligibility", "launch_eligible"),
    ):
        sidecar = _write_reproducible_magicfit_tour(
            tmp_path, slug, video_bytes
        )
        sidecar.pop(field)
        invalid_sidecars.append((slug, sidecar))
    pending = _write_reproducible_magicfit_tour(
        tmp_path, "pending-acceptance-status", video_bytes
    )
    pending["acceptance_status"] = "pending"
    invalid_sidecars.append(("pending-acceptance-status", pending))
    for slug, sidecar in invalid_sidecars:
        _persist_synthetic_accepted_sidecar(tmp_path, slug, sidecar)
    _write_tour(
        tmp_path,
        "implicit-missing-acceptance",
        {
            "video_provider": "magicfit",
            "video_relpath": "walkthrough.mp4",
        },
        {"walkthrough.mp4": video_bytes},
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    tours = {row["slug"]: row for row in receipt["tours"]}
    assert tours["accepted-binding"]["controls"][0]["provider"] == "magicfit"
    assert tours["accepted-utc-offset-generated-at"]["controls"][0]["provider"] == (
        "magicfit"
    )
    for slug in (
        "stale-relpath-binding",
        "stale-hash-binding",
        "unbound-acceptance",
        "missing-acceptance",
        "missing-acceptance-status",
        "missing-launch-eligibility",
        "pending-acceptance-status",
        "implicit-missing-acceptance",
    ):
        missing = {
            row["provider"]: row for row in tours[slug]["missing_evidence"]
        }
        assert missing["magicfit"]["reason"] == "magicfit_walkthrough_disqualified"


def test_magicfit_verifier_rejects_mistyped_numeric_review_receipts(
    tmp_path: Path,
) -> None:
    for case in (
        "boolean_browser_timing",
        "string_browser_timing",
        "boolean_evidence_duration",
        "string_evidence_size",
    ):
        case_root = tmp_path / case
        case_root.mkdir()
        video_path = case_root / "walkthrough.mp4"
        _write_playable_mp4(video_path)
        slug = f"mistyped-{case.replace('_', '-')}"
        accepted = _write_reproducible_magicfit_tour(
            case_root, slug, video_path.read_bytes()
        )
        bundle = case_root / slug
        assert build_property_tour_control_receipt(
            tour_root=case_root
        )["provider_counts"]["magicfit"] == 1

        audit = accepted.get("audit")
        assert isinstance(audit, dict)
        artifacts = audit.get("artifacts")
        assert isinstance(artifacts, dict)

        def load_artifact(name: str) -> dict[str, object]:
            entry = artifacts.get(name)
            assert isinstance(entry, dict)
            return json.loads(
                (bundle / str(entry["relpath"])).read_text(encoding="utf-8")
            )

        browser = load_artifact("browser_receipt")
        evidence = load_artifact("evidence_receipt")
        visual = load_artifact("visual_review")
        if case == "boolean_browser_timing":
            browser["duration_seconds"] = True
            browser["final_current_time"] = True
        elif case == "string_browser_timing":
            browser["duration_seconds"] = str(browser["duration_seconds"])
            browser["final_current_time"] = str(browser["final_current_time"])
        else:
            evidence_video = evidence.get("video")
            assert isinstance(evidence_video, dict)
            if case == "boolean_evidence_duration":
                evidence_video["duration_seconds"] = True
            else:
                evidence_video["size_bytes"] = str(evidence_video["size_bytes"])
        _rewrite_magicfit_review_receipts(
            bundle,
            accepted,
            browser=browser,
            evidence=evidence,
            visual=visual,
        )

        receipt = build_property_tour_control_receipt(tour_root=case_root)

        assert receipt["provider_counts"]["magicfit"] == 0, case
        missing = {
            row["provider"]: row for row in receipt["tours"][0]["missing_evidence"]
        }
        assert missing["magicfit"]["reason"] == "magicfit_walkthrough_disqualified"


def test_magicfit_verifier_requires_digest_derived_accepted_sidecar_path(
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "walkthrough.mp4"
    _write_playable_mp4(video_path)
    slug = "nondigest-sidecar-path"
    accepted = _write_reproducible_magicfit_tour(
        tmp_path, slug, video_path.read_bytes()
    )
    bundle = tmp_path / slug
    assert build_property_tour_control_receipt(
        tour_root=tmp_path
    )["provider_counts"]["magicfit"] == 1

    delivery_digest = str(accepted["delivery_digest"])
    expected_sidecar_relpath = accepted_sidecar_relpath(delivery_digest)
    wrong_sidecar_relpath = ".magicfit-deliveries/not-the-delivery-digest.json"
    manifest = json.loads((bundle / "tour.json").read_text(encoding="utf-8"))
    manifest["video_sidecar_relpath"] = wrong_sidecar_relpath
    magicfit_import = manifest.get("magicfit_import")
    assert isinstance(magicfit_import, dict)
    magicfit_import["delivery_sidecar_relpath"] = wrong_sidecar_relpath
    active_manifest_bytes = canonical_json_bytes(manifest)
    staged_manifest_sha256 = hashlib.sha256(active_manifest_bytes).hexdigest()
    (bundle / "tour.json").write_bytes(active_manifest_bytes)

    accepted["staged_manifest_sha256"] = staged_manifest_sha256
    review = accepted.get("review")
    assert isinstance(review, dict)
    subject = review.get("subject")
    assert isinstance(subject, dict)
    subject["staged_manifest_sha256"] = staged_manifest_sha256
    audit = accepted.get("audit")
    assert isinstance(audit, dict)
    artifacts = audit.get("artifacts")
    assert isinstance(artifacts, dict)

    def load_artifact(name: str) -> dict[str, object]:
        entry = artifacts.get(name)
        assert isinstance(entry, dict)
        return json.loads(
            (bundle / str(entry["relpath"])).read_text(encoding="utf-8")
        )

    browser = load_artifact("browser_receipt")
    evidence = load_artifact("evidence_receipt")
    visual = load_artifact("visual_review")
    for payload in (browser, evidence, visual):
        payload["staged_manifest_sha256"] = staged_manifest_sha256
    _rewrite_magicfit_review_receipts(
        bundle,
        accepted,
        browser=browser,
        evidence=evidence,
        visual=visual,
    )
    (bundle / expected_sidecar_relpath).unlink()
    assert (bundle / wrong_sidecar_relpath).is_file()

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["magicfit"] == 0
    missing = {
        row["provider"]: row for row in receipt["tours"][0]["missing_evidence"]
    }
    assert missing["magicfit"]["reason"] == "magicfit_walkthrough_disqualified"


@pytest.mark.parametrize("legacy_version", ("v1", "v2"))
def test_magicfit_verifier_explicitly_rejects_named_legacy_accepted_profiles(
    tmp_path: Path,
    legacy_version: str,
) -> None:
    video_path = tmp_path / f"legacy-{legacy_version}.mp4"
    _write_playable_mp4(video_path)
    slug = f"legacy-{legacy_version}-accepted-profile"
    accepted = _write_reproducible_magicfit_tour(
        tmp_path, slug, video_path.read_bytes()
    )
    accepted["contract_name"] = (
        f"propertyquarry.magicfit_delivery_acceptance.{legacy_version}"
    )
    review = accepted.get("review")
    assert isinstance(review, dict)
    review["contract_name"] = (
        f"propertyquarry.magicfit_delivery_review.{legacy_version}"
    )
    subject = review.get("subject")
    assert isinstance(subject, dict)
    subject["delivery_contract_name"] = accepted["contract_name"]
    _persist_synthetic_accepted_sidecar(tmp_path, slug, accepted)

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["magicfit"] == 0
    missing = {
        row["provider"]: row for row in receipt["tours"][0]["missing_evidence"]
    }
    assert missing["magicfit"]["reason"] == "magicfit_walkthrough_disqualified"


def test_magicfit_accepted_profile_is_closed_typed_and_review_bound(
    tmp_path: Path,
) -> None:
    playable_magicfit = tmp_path / "closed-profile.mp4"
    _write_playable_mp4(playable_magicfit)
    video_bytes = playable_magicfit.read_bytes()

    def payload(slug: str) -> dict[str, object]:
        return _write_reproducible_magicfit_tour(
            tmp_path, slug, video_bytes
        )

    expected_tour_slugs: dict[str, str] = {}

    def persist(slug: str, candidate: dict[str, object]) -> None:
        _persist_synthetic_accepted_sidecar(tmp_path, slug, candidate)
        expected_tour_slugs[slug] = slug

    status_flip = payload("status-flip-only")
    status_flip["status"] = "rendered_pending_delivery_acceptance"
    persist("status-flip-only", status_flip)

    missing_review = payload("missing-review")
    missing_review.pop("review")
    persist("missing-review", missing_review)

    for slug, field, value in (
        ("acceptance-alias", "acceptance_status", "pass"),
        ("render-alias", "render_status", "succeeded"),
        ("truthy-launch", "launch_eligible", "true"),
        ("wrong-contract", "contract_name", "other.contract.v1"),
        ("extra-outer-field", "disqualification", {}),
    ):
        candidate = payload(slug)
        candidate[field] = value
        persist(slug, candidate)

    uppercase_source = payload("uppercase-source-hash")
    uppercase_source["source_receipt_sha256"] = "A" * 64
    uppercase_review = uppercase_source["review"]
    assert isinstance(uppercase_review, dict)
    uppercase_subject = uppercase_review["subject"]
    assert isinstance(uppercase_subject, dict)
    uppercase_subject["source_receipt_sha256"] = "A" * 64
    persist("uppercase-source-hash", uppercase_source)

    unreceipted = payload("empty-source-hash")
    unreceipted["source_receipt_sha256"] = ""
    empty_review = unreceipted["review"]
    assert isinstance(empty_review, dict)
    empty_subject = empty_review["subject"]
    assert isinstance(empty_subject, dict)
    empty_subject["source_receipt_sha256"] = ""
    persist("empty-source-hash", unreceipted)

    wrong_subject = payload("wrong-review-subject")
    wrong_review = wrong_subject["review"]
    assert isinstance(wrong_review, dict)
    wrong_subject_body = wrong_review["subject"]
    assert isinstance(wrong_subject_body, dict)
    wrong_subject_body["tour_slug"] = "another-tour"
    persist("wrong-review-subject", wrong_subject)

    mismatched_subject_hash = payload("mismatched-subject-hash")
    mismatched_review = mismatched_subject_hash["review"]
    assert isinstance(mismatched_review, dict)
    mismatched_subject = mismatched_review["subject"]
    assert isinstance(mismatched_subject, dict)
    mismatched_subject["video_sha256"] = "0" * 64
    persist("mismatched-subject-hash", mismatched_subject_hash)

    for slug, review_field, value in (
        ("short-reviewer-authority", "reviewer_authority_sha256", "b" * 63),
        ("uppercase-evidence-hash", "evidence_sha256", "C" * 64),
        ("short-visual-review-hash", "visual_review_sha256", "f" * 63),
    ):
        candidate = payload(slug)
        review = candidate["review"]
        assert isinstance(review, dict)
        review[review_field] = value
        persist(slug, candidate)

    for slug, check_value in (
        ("false-review-check", False),
        ("string-review-check", "true"),
    ):
        candidate = payload(slug)
        review = candidate["review"]
        assert isinstance(review, dict)
        checklist = review["checklist"]
        assert isinstance(checklist, dict)
        checklist["playback_to_end"] = check_value
        persist(slug, candidate)

    extra_check = payload("extra-review-check")
    extra_review = extra_check["review"]
    assert isinstance(extra_review, dict)
    extra_checklist = extra_review["checklist"]
    assert isinstance(extra_checklist, dict)
    extra_checklist["operator_said_ok"] = True
    persist("extra-review-check", extra_check)

    for slug, reviewed_at in (
        ("naive-review-time", "2024-01-01T00:01:00"),
        ("offset-review-time", "2024-01-01T01:01:00+01:00"),
        ("utc-offset-review-time", "2024-01-01T00:01:00+00:00"),
        ("future-review-time", "2999-01-01T00:01:00Z"),
        ("pre-import-review-time", "2023-12-31T23:59:59Z"),
    ):
        candidate = payload(slug)
        review = candidate["review"]
        assert isinstance(review, dict)
        review["reviewed_at"] = reviewed_at
        persist(slug, candidate)

    noncanonical_path = payload("noncanonical-review-path")
    noncanonical_path["video_relpath"] = "./walkthrough.mp4"
    noncanonical_review = noncanonical_path["review"]
    assert isinstance(noncanonical_review, dict)
    noncanonical_subject = noncanonical_review["subject"]
    assert isinstance(noncanonical_subject, dict)
    noncanonical_subject["video_relpath"] = "./walkthrough.mp4"
    persist("noncanonical-review-path", noncanonical_path)

    for slug, relpath in (
        ("parent-review-path", "nested/../walkthrough.mp4"),
        ("absolute-review-path", "/walkthrough.mp4"),
        ("backslash-review-path", "nested\\walkthrough.mp4"),
        ("double-slash-review-path", "nested//walkthrough.mp4"),
        ("control-review-path", "walk\x00through.mp4"),
        ("surrogate-review-path", "walk\ud800through.mp4"),
    ):
        candidate = payload(slug)
        candidate["video_relpath"] = relpath
        candidate_review = candidate["review"]
        assert isinstance(candidate_review, dict)
        candidate_subject = candidate_review["subject"]
        assert isinstance(candidate_subject, dict)
        candidate_subject["video_relpath"] = relpath
        if slug == "surrogate-review-path":
            _synthetic_accepted_sidecar_path(tmp_path, slug, candidate).write_text(
                json.dumps(candidate, sort_keys=True),
                encoding="utf-8",
            )
            expected_tour_slugs[slug] = slug
        else:
            persist(slug, candidate)

    manifest_slug = "manifest-slug-mismatch"
    payload(manifest_slug)
    manifest_slug_path = tmp_path / manifest_slug / "tour.json"
    manifest_slug_payload = json.loads(
        manifest_slug_path.read_text(encoding="utf-8")
    )
    manifest_slug_payload["slug"] = "other-slug"
    manifest_slug_path.write_bytes(canonical_json_bytes(manifest_slug_payload))
    expected_tour_slugs[manifest_slug] = "other-slug"

    noncanonical_sidecar_slug = "noncanonical-sidecar-path"
    noncanonical_sidecar = payload(noncanonical_sidecar_slug)
    noncanonical_manifest_path = (
        tmp_path / noncanonical_sidecar_slug / "tour.json"
    )
    noncanonical_manifest = json.loads(
        noncanonical_manifest_path.read_text(encoding="utf-8")
    )
    noncanonical_manifest["video_sidecar_relpath"] = (
        "./" + accepted_sidecar_relpath(str(noncanonical_sidecar["delivery_digest"]))
    )
    noncanonical_manifest_path.write_bytes(
        canonical_json_bytes(noncanonical_manifest)
    )
    expected_tour_slugs[noncanonical_sidecar_slug] = noncanonical_sidecar_slug

    duplicate_slug = "duplicate-review-key"
    duplicate = payload(duplicate_slug)
    duplicate_path = _synthetic_accepted_sidecar_path(
        tmp_path, duplicate_slug, duplicate
    )
    duplicate_body = duplicate_path.read_bytes()
    assert duplicate_body.startswith(b"{")
    duplicate_path.write_bytes(
        b'{"acceptance_status":"accepted",' + duplicate_body[1:]
    )
    expected_tour_slugs[duplicate_slug] = duplicate_slug

    duplicate_nested_slug = "duplicate-nested-review-key"
    duplicate_nested = payload(duplicate_nested_slug)
    duplicate_nested_path = _synthetic_accepted_sidecar_path(
        tmp_path, duplicate_nested_slug, duplicate_nested
    )
    duplicate_nested_body = duplicate_nested_path.read_bytes()
    needle = b'"playback_to_end": true'
    assert duplicate_nested_body.count(needle) == 1
    duplicate_nested_path.write_bytes(
        duplicate_nested_body.replace(
            needle,
            b'"playback_to_end": false, "playback_to_end": true',
            1,
        )
    )
    expected_tour_slugs[duplicate_nested_slug] = duplicate_nested_slug

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["provider_counts"]["magicfit"] == 0
    tours = {row["slug"]: row for row in receipt["tours"]}
    for fixture_slug, receipt_slug in expected_tour_slugs.items():
        missing = {
            row["provider"]: row
            for row in tours[receipt_slug]["missing_evidence"]
        }
        assert missing["magicfit"]["reason"] == (
            "magicfit_walkthrough_disqualified"
        ), fixture_slug


def test_magicfit_manifest_identity_json_and_paths_fail_closed(
    tmp_path: Path,
) -> None:
    playable_magicfit = tmp_path / "manifest-boundary.mp4"
    _write_playable_mp4(playable_magicfit)
    video_bytes = playable_magicfit.read_bytes()

    numeric_slug = "123"
    _write_reproducible_magicfit_tour(tmp_path, numeric_slug, video_bytes)
    numeric_manifest = tmp_path / numeric_slug / "tour.json"
    numeric_payload = json.loads(numeric_manifest.read_text(encoding="utf-8"))
    numeric_payload["slug"] = 123
    numeric_manifest.write_text(json.dumps(numeric_payload), encoding="utf-8")

    duplicate_slug = "duplicate-manifest-key"
    _write_reproducible_magicfit_tour(tmp_path, duplicate_slug, video_bytes)
    duplicate_manifest = tmp_path / duplicate_slug / "tour.json"
    duplicate_body = duplicate_manifest.read_text(encoding="utf-8")
    duplicate_manifest.write_text(
        '{"video_provider":"not-magicfit",' + duplicate_body[1:],
        encoding="utf-8",
    )

    nonfinite_slug = "nonfinite-manifest"
    _write_reproducible_magicfit_tour(tmp_path, nonfinite_slug, video_bytes)
    nonfinite_manifest = tmp_path / nonfinite_slug / "tour.json"
    nonfinite_payload = json.loads(nonfinite_manifest.read_text(encoding="utf-8"))
    nonfinite_payload["unrelated_metric"] = float("nan")
    nonfinite_manifest.write_text(json.dumps(nonfinite_payload), encoding="utf-8")

    invalid_path_slug = "invalid-manifest-path"
    _write_tour(
        tmp_path,
        invalid_path_slug,
        {
            "video_provider": "magicfit",
            "video_relpath": "walk\ud800through.mp4",
            "video_sidecar_relpath": "tour.magicfit.json",
        },
        {"tour.magicfit.json": "{}"},
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "fail"
    assert receipt["provider_counts"]["magicfit"] == 0
    tours = {row["slug"]: row for row in receipt["tours"]}
    numeric_missing = {
        row["provider"]: row for row in tours[numeric_slug]["missing_evidence"]
    }
    assert numeric_missing["magicfit"]["reason"] == (
        "magicfit_walkthrough_disqualified"
    )
    assert tours[duplicate_slug]["status"] == "invalid_manifest"
    assert tours[nonfinite_slug]["status"] == "invalid_manifest"
    assert tours[invalid_path_slug]["status"] == (
        "blocked_missing_verified_controls"
    )


def test_property_tour_control_verifier_rejects_remote_only_magicfit_video(tmp_path: Path) -> None:
    _write_tour(
        tmp_path,
        "remote-magicfit",
        {
            "video_provider": "magicfit",
            "video_url": "https://propertyquarry.com/tours/files/remote-magicfit/walkthrough.mp4",
        },
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "blocked_missing_verified_controls"
    assert receipt["provider_counts"]["magicfit"] == 0
    assert receipt["ready_provider_modes"] == []
    assert receipt["tours"][0]["controls"] == []
    missing = {row["provider"]: row for row in receipt["tours"][0]["missing_evidence"]}
    assert missing["magicfit"]["reason"] == "magicfit_walkthrough_disqualified"
    assert "remote-magicfit/walkthrough.mp4" not in json.dumps(receipt)


def test_property_tour_control_verifier_does_not_promote_remote_magicfit_live_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_tour(
        tmp_path,
        "remote-magicfit-ready",
        {
            "video_provider": "magicfit",
            "video_url": "https://propertyquarry.com/tours/files/remote-magicfit-ready/walkthrough.mp4",
        },
    )
    seen_urls: list[str] = []

    def _successful_probe(url: str, *_args, **_kwargs) -> dict[str, object]:
        seen_urls.append(url)
        return {
            "http_status": 200,
            "content_type": "video/mp4",
            "playback_markers": {
                "video_content_type": True,
                "video_signature": True,
                "video_stream": True,
                "duration_positive": True,
            },
        }

    monkeypatch.setattr("scripts.verify_property_tour_controls._probe_url", _successful_probe)

    receipt = build_property_tour_control_receipt(
        tour_root=tmp_path,
        base_url="https://propertyquarry.example",
        live_probe=True,
    )

    assert receipt["status"] == "blocked_missing_verified_controls"
    assert receipt["provider_counts"]["magicfit"] == 0
    assert receipt["magicfit_playback"]["playback_ok"] is True
    assert receipt["magicfit_playback"]["playable_count"] == 0
    assert receipt["magicfit_playback"]["ready_count"] == 0
    assert receipt["ready_provider_modes"] == []
    assert seen_urls == []
    assert receipt["tours"][0]["controls"] == []
    assert "remote-magicfit-ready/walkthrough.mp4" not in json.dumps(receipt)


def test_property_tour_control_verifier_rejects_remote_magicfit_failed_live_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_tour(
        tmp_path,
        "remote-magicfit-failed",
        {
            "video_provider": "magicfit",
            "video_url": "https://propertyquarry.com/tours/files/remote-magicfit-failed/walkthrough.mp4",
        },
    )

    def _failed_probe(*_args, **_kwargs) -> dict[str, object]:
        return {
            "http_status": 200,
            "content_type": "text/html",
            "playback_markers": {
                "video_content_type": False,
                "video_signature": False,
            },
        }

    monkeypatch.setattr("scripts.verify_property_tour_controls._probe_url", _failed_probe)

    receipt = build_property_tour_control_receipt(
        tour_root=tmp_path,
        base_url="https://propertyquarry.example",
        live_probe=True,
        gold_scope="advanced_visual",
    )

    assert receipt["status"] == "blocked_missing_verified_controls"
    assert receipt["provider_probe_failures"]["selected_fatal_count"] == 0
    assert receipt["provider_counts"]["magicfit"] == 0
    assert "magicfit" not in receipt["ready_provider_modes"]
    assert receipt["tours"][0]["status"] == "blocked_missing_verified_controls"
    assert receipt["tours"][0]["controls"] == []


def test_property_tour_control_verifier_rejects_placeholder_local_3d_exports(tmp_path: Path) -> None:
    _write_tour(
        tmp_path,
        "placeholder-3dvista",
        {"three_d_vista_entry_relpath": "3dvista/index.html"},
        {"3dvista/index.html": "<html><body>Coming soon</body></html>"},
    )
    _write_tour(
        tmp_path,
        "placeholder-pano2vr",
        {"pano2vr_entry_relpath": "pano/index.html"},
        {"pano/index.html": "<html><body>Static placeholder</body></html>"},
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "blocked_missing_verified_controls"
    assert receipt["provider_counts"]["3dvista"] == 0
    assert receipt["provider_counts"]["pano2vr"] == 0
    assert {tour["blocked_reason"] for tour in receipt["tours"]} == {"missing_verified_provider_control"}


def test_property_tour_control_verifier_blocks_when_no_verified_controls(tmp_path: Path) -> None:
    _write_tour(tmp_path, "fallback-tour", {"scene_strategy": "pure_360_cube"})

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "blocked_missing_verified_controls"
    assert receipt["ready_provider_modes"] == []
    assert receipt["tours"][0]["status"] == "blocked_missing_verified_controls"
    assert receipt["tours"][0]["blocked_reason"] == "generated_cube_not_verified_3d"
    assert set(receipt["missing_provider_modes"]) == {
        "matterport",
        "3dvista",
        "magicfit",
    }


def test_matterport_delivery_contract_reports_dominant_missing_evidence_for_mixed_tour_set(
    tmp_path: Path,
) -> None:
    _write_tour(tmp_path, "without-matterport", {})
    _write_tour(
        tmp_path,
        "also-without-matterport",
        {},
    )
    _write_tour(
        tmp_path,
        "retired-matterport",
        {"matterport_url": "https://my.matterport.com/show/?m=RETIRED123"},
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    blocker_reasons = receipt["provider_blockers"]["matterport"]["reasons"]
    assert blocker_reasons[0]["reason"] == "missing_matterport_url"
    contract = receipt["delivery_contracts"]["matterport"]
    assert contract["status"] == "blocked"
    assert contract["blocked_reason"] == "missing_matterport_url"
    assert contract["ready_payload"]["ready_count"] == 0
    assert contract["ready_payload"]["sample_controls"] == []


def test_property_tour_control_verifier_marks_photo_gallery_as_not_3d(tmp_path: Path) -> None:
    _write_tour(
        tmp_path,
        "gallery-tour",
        {
            "creation_mode": "hosted_photo_gallery_tour",
            "scene_strategy": "photo_gallery_hosted",
            "scenes": [{"asset_relpath": "photo-01.jpg", "role": "photo"}],
        },
        {"photo-01.jpg": "image"},
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "blocked_missing_verified_controls"
    assert receipt["provider_counts"] == {
        "matterport": 0,
        "3dvista": 0,
        "pano2vr": 0,
        "krpano": 0,
        "magicfit": 0,
    }
    assert receipt["tours"][0]["blocked_reason"] == "gallery_only_not_3d"
    assert receipt["tours"][0]["controls"] == []
    assert receipt["tours"][0]["missing_evidence"] == []
    assert {row["provider"] for row in receipt["next_required_actions"]} == {
        "matterport",
        "3dvista",
        "magicfit",
    }


def test_property_tour_control_verifier_reports_actionable_missing_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KRPANO_LICENSE_DOMAIN", raising=False)
    monkeypatch.delenv("KRPANO_LICENSE_KEY", raising=False)
    _write_tour(
        tmp_path,
        "partial-provider-tour",
        {
            "matterport_url": "https://tracker.example/show/?m=abc",
            "three_d_vista_entry_relpath": "3dvista/index.html",
            "pano2vr_entry_relpath": "pano/index.html",
            "video_provider": "stock",
            "video_relpath": "walkthrough.mp4",
            "walkable_scene": {"rooms": []},
        },
        {
            "3dvista/index.html": "<html><body>placeholder</body></html>",
            "pano/index.html": "<html><body>placeholder</body></html>",
            "walkthrough.mp4": b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom",
        },
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    missing = {row["provider"]: row for row in receipt["tours"][0]["missing_evidence"]}
    assert missing["matterport"]["reason"] == "matterport_url_not_allowlisted_or_invalid"
    assert receipt["provider_blockers"]["matterport"]["reasons"][0]["reason"] == "matterport_url_not_allowlisted_or_invalid"
    assert missing["3dvista"]["reason"] == "3dvista_entry_missing_or_not_verified"
    assert (
        receipt["provider_blockers"]["3dvista"]["reasons"][0]["reason"]
        == "3dvista_entry_missing_or_not_verified"
    )
    assert "pano2vr" not in missing
    assert receipt["provider_blockers"]["pano2vr"]["reasons"][0]["reason"] == "pano2vr_entry_missing_or_not_verified"
    assert "krpano" not in missing
    assert {
        row["provider"]
        for row in receipt["tours"][0]["optional_missing_evidence"]
    } == {"pano2vr", "krpano"}
    assert missing["magicfit"]["reason"] == "walkthrough_provider_not_magicfit"
    assert "tracker.example" not in json.dumps(receipt)


def test_property_tour_control_verifier_does_not_treat_private_or_missing_assets_as_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("KRPANO_LICENSE_DOMAIN", raising=False)
    monkeypatch.delenv("KRPANO_LICENSE_KEY", raising=False)
    _write_tour(
        tmp_path,
        "unsafe-tour",
        {
            "matterport_url": "https://tracker.example/show/?m=abc",
            "three_d_vista_entry_relpath": "../private/index.html",
            "pano2vr_entry_relpath": "missing/index.html",
            "video_provider": "magicfit",
            "video_relpath": "private.txt",
            "walkable_scene": {"rooms": []},
        },
    )

    receipt = build_property_tour_control_receipt(tour_root=tmp_path)

    assert receipt["status"] == "blocked_missing_verified_controls"
    assert receipt["provider_counts"] == {
        "matterport": 0,
        "3dvista": 0,
        "pano2vr": 0,
        "krpano": 0,
        "magicfit": 0,
    }
