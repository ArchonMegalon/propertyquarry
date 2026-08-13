from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import pytest
from PIL import Image

from scripts import discover_property_tour_exports as tour_export_discovery
from scripts.discover_property_tour_exports import REJECTION_ACTIONS, build_discovery_receipt
from scripts.import_3dvista_export import _normalize_web_readable_export_tree
from scripts.intake_3dvista_gold_artifact import build_3dvista_intake_receipt
from scripts.property_tour_3dvista_provenance import (
    THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
    export_tree_sha256,
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
from scripts.verify_property_tour_controls import build_property_tour_control_receipt
from scripts.check_property_tour_delivery_contract import build_tour_delivery_contract_receipt
from scripts.import_magicfit_walkthrough import (
    _activation_lock as _magicfit_import_activation_lock,
    _confirm_named_bundle_identity as _confirm_magicfit_import_bundle_identity,
)
from scripts.property_tour_publication_lock import property_tour_publication_lock


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _bounded_import_test_disk_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    # Importer disk guards are tested independently with synthetic disk usage;
    # fixture imports must not depend on the CI host's current free capacity.
    monkeypatch.setenv("PROPERTYQUARRY_TOUR_MIN_FREE_BYTES", "0")


def _run_importer(script_name: str, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["EA_PUBLIC_TOUR_DIR"] = str(tmp_path / "public_tours")
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _write_base_tour(tmp_path: Path, slug: str) -> Path:
    bundle_dir = tmp_path / "public_tours" / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps({"slug": slug, "display_title": "Import target"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return bundle_dir


def test_tour_export_discovery_default_public_root_prefers_runtime_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime-public-tours"
    runtime_root.mkdir()
    captured: dict[str, object] = {}

    def fake_preferred_public_tour_root(**kwargs: object) -> Path:
        captured.update(kwargs)
        return runtime_root.resolve()

    monkeypatch.setattr(
        tour_export_discovery,
        "preferred_public_tour_root",
        fake_preferred_public_tour_root,
    )

    assert tour_export_discovery._default_public_tour_dir() == runtime_root.resolve()
    assert captured["fallback_root"] == "/docker/property/state/public_property_tours"


def test_tour_export_discovery_default_drop_root_prefers_runtime_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime-incoming-tours"
    runtime_root.mkdir()
    monkeypatch.delenv("PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR", raising=False)
    monkeypatch.setattr(
        tour_export_discovery,
        "running_container_incoming_tour_dir",
        lambda _container="": runtime_root,
    )

    assert tour_export_discovery._default_drop_dir() == runtime_root.resolve()


def test_tour_export_discovery_cli_snapshots_unmounted_runtime_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime-snapshot"
    runtime_root.mkdir()

    class SnapshotHandle:
        cleaned = False

        def cleanup(self) -> None:
            self.cleaned = True

    handle = SnapshotHandle()
    monkeypatch.setattr(
        tour_export_discovery,
        "running_container_public_tour_dir",
        lambda _container="": None,
    )
    monkeypatch.setattr(
        tour_export_discovery,
        "snapshot_running_container_public_tours",
        lambda _container="": (runtime_root.resolve(), handle),
    )

    with tour_export_discovery._cli_public_tour_dir() as selected:
        assert selected == runtime_root.resolve()
        assert handle.cleaned is False

    assert handle.cleaned is True


def test_tour_export_discovery_does_not_reimport_exact_live_3dvista_artifact(
    tmp_path: Path,
) -> None:
    slug = "already-live-3dvista"
    bundle_dir = _write_base_tour(tmp_path, slug)
    drop_export = tmp_path / "drop" / slug / "3dvista"
    drop_export.mkdir(parents=True)
    (drop_export / "index.htm").write_text(
        "<!doctype html><script src='lib/tdvplayer.js'></script>",
        encoding="utf-8",
    )
    provenance_path = _write_3dvista_provenance(
        drop_export,
        slug,
        entry_relpath="index.htm",
    )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    live_export = bundle_dir / "3dvista"
    shutil.copytree(drop_export, live_export)
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "three_d_vista_entry_relpath": "3dvista/index.htm",
                "three_d_vista_target_provenance": provenance,
            }
        ),
        encoding="utf-8",
    )

    receipt = build_discovery_receipt(
        drop_dir=tmp_path / "drop",
        public_tour_dir=tmp_path / "public_tours",
    )

    assert receipt["status"] == "ready"
    assert receipt["import_count"] == 0
    assert receipt["resolved_existing_import_count"] == 1
    resolved = receipt["resolved_existing_imports"][0]
    assert resolved["status"] == "already_imported_live_bundle"
    assert resolved["artifact_sha256"] == provenance["artifact"]["sha256"]
    assert resolved["live_control_path"] == f"/tours/{slug}/control/3dvista"


def test_tour_export_discovery_does_not_replace_newer_live_3dvista_artifact(
    tmp_path: Path,
) -> None:
    slug = "newer-live-3dvista"
    bundle_dir = _write_base_tour(tmp_path, slug)
    drop_export = tmp_path / "drop" / slug / "3dvista"
    drop_export.mkdir(parents=True)
    (drop_export / "index.htm").write_text(
        "<!doctype html><script src='lib/tdvplayer.js'></script><p>older</p>",
        encoding="utf-8",
    )
    _write_3dvista_provenance(drop_export, slug, entry_relpath="index.htm")

    live_export = bundle_dir / "3dvista"
    live_export.mkdir()
    (live_export / "index.htm").write_text(
        "<!doctype html><script src='lib/tdvplayer.js'></script><p>newer</p>",
        encoding="utf-8",
    )
    live_provenance_path = _write_3dvista_provenance(
        live_export,
        slug,
        entry_relpath="index.htm",
    )
    live_provenance = json.loads(live_provenance_path.read_text(encoding="utf-8"))
    live_provenance["review"]["reviewed_at"] = "2026-07-15T00:00:00+00:00"
    live_provenance_path.write_text(json.dumps(live_provenance), encoding="utf-8")
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "three_d_vista_entry_relpath": "3dvista/index.htm",
                "three_d_vista_target_provenance": live_provenance,
            }
        ),
        encoding="utf-8",
    )

    receipt = build_discovery_receipt(
        drop_dir=tmp_path / "drop",
        public_tour_dir=tmp_path / "public_tours",
    )

    assert receipt["status"] == "ready"
    assert receipt["import_count"] == 0
    assert receipt["resolved_existing_import_count"] == 1
    resolved = receipt["resolved_existing_imports"][0]
    assert resolved["status"] == "superseded_by_newer_live_bundle"
    assert "newer target review" in resolved["resolution"]


def _write_3dvista_provenance(
    export_dir: Path,
    slug: str,
    *,
    entry_relpath: str = "index.html",
    authorization_status: str = "approved",
    property_match: str = "pass",
    visual_match: str = "pass",
) -> Path:
    receipt_path = export_dir / "3dvista-target-provenance.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema": THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
                "status": "pass",
                "provider": "3dvista",
                "target_slug": slug,
                "artifact": {
                    "kind": "local_export",
                    "sha256": export_tree_sha256(export_dir),
                    "entry_relpath": entry_relpath,
                },
                "authorization": {
                    "status": authorization_status,
                    "reference": f"fixture-authorization:{slug}",
                },
                "review": {
                    "property_match": property_match,
                    "visual_match": visual_match,
                    "reviewed_by": "propertyquarry-test-reviewer",
                    "reviewed_at": "2026-07-14T00:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    return receipt_path


def _attach_panorama_spatial_provenance(
    bundle_dir: Path,
    *,
    provider: str,
) -> None:
    manifest_path = bundle_dir / "tour.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    private_path = bundle_dir / "tour.private.json"
    private_payload = (
        json.loads(private_path.read_text(encoding="utf-8"))
        if private_path.is_file()
        else {}
    )
    merged = {**payload, **private_payload}
    slug = str(merged["slug"])
    if provider == "pano2vr":
        entry_relpath = str(
            merged.get("pano2vr_entry_relpath")
            or merged.get("pano2vr_export_entry_relpath")
            or ""
        )
        export_root = str(
            merged.get("pano2vr_export_root_relpath")
            or merged.get("pano2vr_root_relpath")
            or Path(entry_relpath).parent.as_posix()
        )
        topology = pano2vr_export_topology(bundle_dir / export_root)
        key = PANO2VR_SPATIAL_PROVENANCE_KEY
        artifact = {
            "kind": "local_export",
            "sha256": panorama_export_tree_sha256(bundle_dir / export_root),
            "entry_relpath": Path(entry_relpath).relative_to(export_root).as_posix(),
        }
        projection = "equirectangular"
        source_kind = "camera_equirectangular"
    else:
        topology = walkable_scene_topology(merged)
        key = KRPANO_SPATIAL_PROVENANCE_KEY
        artifact = {
            "kind": "panorama_assets",
            "sha256": panorama_asset_set_sha256(
                bundle_dir,
                panorama_asset_relpaths(merged),
            ),
            "entry_relpath": "",
        }
        walkable_scene = merged.get("walkable_scene")
        projection = str(
            walkable_scene.get("projection")
            if isinstance(walkable_scene, dict)
            else "equirectangular"
        )
        source_kind = (
            "camera_cubemap" if projection == "cubemap" else "camera_equirectangular"
        )
    private_payload[key] = {
        "schema": PANORAMA_SPATIAL_PROVENANCE_SCHEMA,
        "status": "pass",
        "provider": provider,
        "target_slug": slug,
        "artifact": artifact,
        "capture": {
            "source_kind": source_kind,
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
            "reviewed_by": "propertyquarry-test-reviewer",
            "reviewed_at": "2026-07-18T12:00:00+00:00",
        },
    }
    private_path.write_text(json.dumps(private_payload), encoding="utf-8")


def _write_playable_mp4(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AssertionError("ffmpeg is required for playable MagicFit importer fixtures")
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


def _magicfit_source_receipt(*, slug: str, video: Path) -> dict[str, object]:
    hosted_url = f"https://media.powlcdn.com/magicfit/{slug}.mp4"
    return {
        "provider": "magicfit",
        "provider_key": "magicfit",
        "provider_backend_key": "magicfit",
        "render_status": "completed",
        "target_slug": slug,
        "hosted_walkthrough_video_url": hosted_url,
        "video_output_url": hosted_url,
        "output_file": str(video),
    }


def _write_equirectangular_image(path: Path) -> None:
    image = Image.new("RGB", (2048, 1024), color=(28, 42, 36))
    image.save(path, format="JPEG")


def _write_sixteen_by_nine_image(path: Path) -> None:
    image = Image.new("RGB", (1280, 720), color=(28, 36, 42))
    image.save(path, format="JPEG")


def _write_square_image(path: Path) -> None:
    image = Image.new("RGB", (1024, 1024), color=(42, 36, 28))
    image.save(path, format="JPEG")


def _successful_provider_probe(*_args, provider: str = "", **_kwargs) -> dict[str, object]:
    if provider == "magicfit":
        return {
            "http_status": 200,
            "playback_markers": {
                "video_content_type": True,
                "video_signature": True,
                "video_stream": True,
                "duration_positive": True,
            },
        }
    return {"http_status": 200, "body_markers": {provider: True}}


def test_3dvista_importer_requires_verified_export_markers(tmp_path: Path) -> None:
    slug = "verified-3dvista-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    placeholder_export = tmp_path / "placeholder_3dvista"
    placeholder_export.mkdir()
    (placeholder_export / "index.html").write_text("<!doctype html><title>Coming soon</title>", encoding="utf-8")

    rejected = _run_importer(
        "import_3dvista_export.py",
        tmp_path,
        "--slug",
        slug,
        "--export-dir",
        str(placeholder_export),
    )

    assert rejected.returncode != 0
    assert "3dvista_export_entry_unverified" in rejected.stderr
    assert not (bundle_dir / "3dvista" / "index.html").exists()
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert "three_d_vista_entry_relpath" not in manifest

    verified_export = tmp_path / "verified_3dvista"
    verified_export.mkdir()
    (verified_export / "index.html").write_text(
        "<!doctype html><script src='runtime/app.js'></script><div>3DVista export shell</div>",
        encoding="utf-8",
    )
    (verified_export / "runtime").mkdir()
    (verified_export / "runtime" / "app.js").write_text("window.TDVPlayer = true;", encoding="utf-8")

    missing_provenance = _run_importer(
        "import_3dvista_export.py",
        tmp_path,
        "--slug",
        slug,
        "--export-dir",
        str(verified_export),
    )

    assert missing_provenance.returncode != 0
    assert "3dvista_target_provenance_missing" in missing_provenance.stderr
    assert not (bundle_dir / "3dvista" / "index.html").exists()
    provenance_receipt = _write_3dvista_provenance(verified_export, slug)
    verified_export.chmod(0o700)
    (verified_export / "runtime").chmod(0o700)
    (verified_export / "index.html").chmod(0o600)
    (verified_export / "runtime" / "app.js").chmod(0o600)
    provenance_receipt.chmod(0o600)

    imported = _run_importer(
        "import_3dvista_export.py",
        tmp_path,
        "--slug",
        slug,
        "--export-dir",
        str(verified_export),
    )

    assert imported.returncode == 0, imported.stderr
    body = json.loads(imported.stdout)
    assert body["control_url"] == f"/tours/{slug}/control/3dvista"
    assert body["web_permissions"]["status"] == "pass"
    assert body["web_permissions"]["directories_mode"] == "0755"
    assert body["web_permissions"]["files_mode"] == "0644"
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert manifest["control_mode"] == "3dvista"
    assert manifest["viewer_provider"] == "3dvista_vt_pro"
    assert manifest["three_d_vista_entry_relpath"] == "3dvista/index.html"
    assert manifest["three_d_vista_export_root_relpath"] == "3dvista"
    assert "three_d_vista_white_label_proof" not in manifest
    assert "three_d_vista_target_provenance" not in manifest
    private_manifest = json.loads((bundle_dir / "tour.private.json").read_text(encoding="utf-8"))
    assert private_manifest["three_d_vista_white_label_proof"]["non_trial_export_verified"] is True
    assert private_manifest["three_d_vista_white_label_proof"]["trial_branding_present"] is False
    provenance = private_manifest["three_d_vista_target_provenance"]
    assert provenance["target_slug"] == slug
    assert provenance["artifact"]["sha256"] == export_tree_sha256(verified_export)
    permission_receipt = private_manifest["three_d_vista_export_permissions"]
    assert permission_receipt["status"] == "pass"
    assert permission_receipt["scope"] == "3dvista"
    assert permission_receipt["normalized_at"]
    assert (bundle_dir / "tour.private.json").stat().st_mode & 0o777 == 0o600
    assert not (bundle_dir / "3dvista" / "3dvista-target-provenance.json").exists()
    assert (bundle_dir / "3dvista" / "runtime" / "app.js").exists()
    assert (bundle_dir / "3dvista").stat().st_mode & 0o777 == 0o755
    assert (bundle_dir / "3dvista" / "runtime").stat().st_mode & 0o777 == 0o755
    assert (bundle_dir / "3dvista" / "index.html").stat().st_mode & 0o777 == 0o644
    assert (bundle_dir / "3dvista" / "runtime" / "app.js").stat().st_mode & 0o777 == 0o644
    # The importer normalizes only its owned copy, never the operator artifact
    # or private provenance receipt outside the public bundle.
    assert verified_export.stat().st_mode & 0o777 == 0o700
    assert (verified_export / "runtime").stat().st_mode & 0o777 == 0o700
    assert (verified_export / "index.html").stat().st_mode & 0o777 == 0o600
    assert provenance_receipt.stat().st_mode & 0o777 == 0o600


def test_3dvista_importer_rejects_wrong_target_hash_or_review(tmp_path: Path) -> None:
    slug = "target-bound-3dvista-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    export_dir = tmp_path / "target_bound_3dvista"
    export_dir.mkdir()
    (export_dir / "index.html").write_text(
        "<!doctype html><script src='runtime/app.js'></script><div>3DVista export shell</div>",
        encoding="utf-8",
    )
    (export_dir / "runtime").mkdir()
    (export_dir / "runtime" / "app.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    receipt_path = _write_3dvista_provenance(export_dir, slug)
    valid_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    cases = (
        ("target_slug_mismatch", {"target_slug": "different-property"}),
        ("artifact_sha256_mismatch", {"artifact": {**valid_receipt["artifact"], "sha256": "0" * 64}}),
        ("authorization_not_approved", {"authorization": {**valid_receipt["authorization"], "status": "pending"}}),
        ("visual_match_not_pass", {"review": {**valid_receipt["review"], "visual_match": "fail"}}),
    )
    for expected_error, override in cases:
        receipt_path.write_text(json.dumps({**valid_receipt, **override}), encoding="utf-8")
        rejected = _run_importer(
            "import_3dvista_export.py",
            tmp_path,
            "--slug",
            slug,
            "--export-dir",
            str(export_dir),
        )
        assert rejected.returncode != 0
        assert expected_error in rejected.stderr
        assert not (bundle_dir / "3dvista").exists()
        assert "three_d_vista_entry_relpath" not in json.loads(
            (bundle_dir / "tour.json").read_text(encoding="utf-8")
        )


def test_3dvista_importer_can_attach_reviewed_provenance_to_existing_export(
    tmp_path: Path,
) -> None:
    slug = "existing-reviewed-3dvista"
    bundle_dir = _write_base_tour(tmp_path, slug)
    export_dir = bundle_dir / "3dvista"
    export_dir.mkdir()
    (export_dir / "index.htm").write_text(
        "<!doctype html><script src='runtime/app.js'></script><div>3DVista export shell</div>",
        encoding="utf-8",
    )
    (export_dir / "runtime").mkdir()
    (export_dir / "runtime" / "app.js").write_text(
        "window.TDVPlayer = true;", encoding="utf-8"
    )
    manifest_path = bundle_dir / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "control_mode": "3dvista",
            "viewer_provider": "3dvista_vt_pro",
            "three_d_vista_entry_relpath": "3dvista/index.htm",
            "three_d_vista_export_root_relpath": "3dvista",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    private_path = bundle_dir / "tour.private.json"
    private_path.write_text(
        json.dumps(
            {
                "private_marker": "preserved",
                "three_d_vista_white_label_proof": {
                    "source_project": "propertyquarry",
                    "non_trial_export_verified": True,
                    "propertyquarry_tour_metadata": True,
                    "trial_branding_checked": True,
                    "trial_branding_present": False,
                },
            }
        ),
        encoding="utf-8",
    )
    receipt_dir = tmp_path / "review"
    receipt_dir.mkdir()
    receipt_path = _write_3dvista_provenance(
        receipt_dir,
        slug,
        entry_relpath="index.htm",
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["artifact"]["sha256"] = export_tree_sha256(export_dir)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    export_tree_before = export_tree_sha256(export_dir)
    export_dir.chmod(0o700)
    (export_dir / "runtime").chmod(0o700)
    (export_dir / "index.htm").chmod(0o600)
    (export_dir / "runtime" / "app.js").chmod(0o600)
    receipt_path.chmod(0o600)

    attached = _run_importer(
        "import_3dvista_export.py",
        tmp_path,
        "--slug",
        slug,
        "--attach-provenance-only",
        "--provenance-receipt",
        str(receipt_path),
    )

    assert attached.returncode == 0, attached.stderr
    result = json.loads(attached.stdout)
    assert result["status"] == "provenance_attached"
    assert result["export_tree_sha256"] == export_tree_before
    assert result["web_permissions"]["status"] == "pass"
    assert export_tree_sha256(export_dir) == export_tree_before
    private_payload = json.loads(private_path.read_text(encoding="utf-8"))
    assert private_payload["private_marker"] == "preserved"
    provenance = private_payload["three_d_vista_target_provenance"]
    assert provenance["target_slug"] == slug
    assert provenance["artifact"]["sha256"] == export_tree_before
    assert provenance["target_subdir"] == "3dvista"
    permission_receipt = private_payload["three_d_vista_export_permissions"]
    assert permission_receipt["status"] == "pass"
    assert permission_receipt["scope"] == "3dvista"
    assert private_path.stat().st_mode & 0o777 == 0o600
    assert export_dir.stat().st_mode & 0o777 == 0o755
    assert (export_dir / "runtime").stat().st_mode & 0o777 == 0o755
    assert (export_dir / "index.htm").stat().st_mode & 0o777 == 0o644
    assert (export_dir / "runtime" / "app.js").stat().st_mode & 0o777 == 0o644
    assert receipt_path.stat().st_mode & 0o777 == 0o600


def test_3dvista_attach_provenance_rejects_export_drift_or_source_mix(
    tmp_path: Path,
) -> None:
    slug = "existing-drifted-3dvista"
    bundle_dir = _write_base_tour(tmp_path, slug)
    export_dir = bundle_dir / "3dvista"
    export_dir.mkdir()
    (export_dir / "index.htm").write_text(
        "<!doctype html><script>window.TDVPlayer = true;</script>",
        encoding="utf-8",
    )
    manifest_path = bundle_dir / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["three_d_vista_entry_relpath"] = "3dvista/index.htm"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    review_dir = tmp_path / "review-drift"
    review_dir.mkdir()
    receipt_path = _write_3dvista_provenance(
        review_dir,
        slug,
        entry_relpath="index.htm",
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["artifact"]["sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    rejected = _run_importer(
        "import_3dvista_export.py",
        tmp_path,
        "--slug",
        slug,
        "--attach-provenance-only",
        "--provenance-receipt",
        str(receipt_path),
    )
    mixed = _run_importer(
        "import_3dvista_export.py",
        tmp_path,
        "--slug",
        slug,
        "--attach-provenance-only",
        "--export-dir",
        str(export_dir),
        "--provenance-receipt",
        str(receipt_path),
    )

    assert rejected.returncode != 0
    assert "artifact_sha256_mismatch" in rejected.stderr
    assert not (bundle_dir / "tour.private.json").exists()
    assert mixed.returncode != 0
    assert "3dvista_attach_provenance_rejects_export_source" in mixed.stderr


def test_3dvista_permission_normalization_denial_cannot_create_provider_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = "permission-denied-3dvista"
    bundle_dir = _write_base_tour(tmp_path, slug)
    export_dir = bundle_dir / "3dvista"
    export_dir.mkdir()
    entry = export_dir / "index.htm"
    entry.write_text(
        "<!doctype html><script>window.TDVPlayer = true;</script>",
        encoding="utf-8",
    )
    manifest_path = bundle_dir / "tour.json"
    manifest_before = manifest_path.read_bytes()
    original_chmod = Path.chmod

    def deny_entry_chmod(path: Path, mode: int, *args: object, **kwargs: object) -> None:
        if path == entry:
            raise PermissionError("fixture denies web-readable mode")
        original_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", deny_entry_chmod)
    with pytest.raises(SystemExit, match="3dvista_export_permissions_invalid"):
        _normalize_web_readable_export_tree(
            bundle_dir=bundle_dir,
            export_dir=export_dir,
        )

    assert manifest_path.read_bytes() == manifest_before
    assert not (bundle_dir / "tour.private.json").exists()
    assert "three_d_vista_entry_relpath" not in json.loads(manifest_before)


def test_3dvista_provenance_template_is_private_and_fails_closed_until_reviewed(tmp_path: Path) -> None:
    slug = "review-template-3dvista-import"
    _write_base_tour(tmp_path, slug)
    export_dir = tmp_path / "review_template_3dvista"
    export_dir.mkdir()
    (export_dir / "index.html").write_text(
        "<!doctype html><script src='tdvplayer.js'></script><div>3DVista export shell</div>",
        encoding="utf-8",
    )
    (export_dir / "tdvplayer.js").write_text("window.TDVPlayer = true;", encoding="utf-8")

    generated = _run_importer(
        "create_3dvista_provenance_template.py",
        tmp_path,
        "--slug",
        slug,
        "--export-dir",
        str(export_dir),
    )

    assert generated.returncode == 0, generated.stderr
    receipt_path = export_dir / "3dvista-target-provenance.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "pending_review"
    assert receipt["artifact"]["sha256"] == export_tree_sha256(export_dir)
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    pending = _run_importer(
        "import_3dvista_export.py",
        tmp_path,
        "--slug",
        slug,
        "--export-dir",
        str(export_dir),
    )
    assert pending.returncode != 0
    assert "status_not_pass" in pending.stderr
    assert "authorization_not_approved" in pending.stderr

    receipt["status"] = "pass"
    receipt["authorization"] = {"status": "approved", "reference": "fixture-approved-reuse"}
    receipt["review"] = {
        "property_match": "pass",
        "visual_match": "pass",
        "reviewed_by": "propertyquarry-test-reviewer",
        "reviewed_at": "2026-07-14T00:00:00+00:00",
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    imported = _run_importer(
        "import_3dvista_export.py",
        tmp_path,
        "--slug",
        slug,
        "--export-dir",
        str(export_dir),
    )
    assert imported.returncode == 0, imported.stderr


def test_3dvista_intake_manifest_is_scoped_to_exact_requested_slug(tmp_path: Path) -> None:
    target_slug = "exact-intake-target"
    other_slug = "other-importable-tour"
    for slug in (target_slug, other_slug):
        _write_base_tour(tmp_path, slug)
        export_dir = tmp_path / "drop" / slug / "3dvista"
        export_dir.mkdir(parents=True)
        (export_dir / "index.html").write_text(
            "<!doctype html><script src='tdvplayer.js'></script><div>3DVista export shell</div>",
            encoding="utf-8",
        )
        (export_dir / "tdvplayer.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
        _write_3dvista_provenance(export_dir, slug)

    completion_dir = tmp_path / "completion"
    receipt = build_3dvista_intake_receipt(
        drop_dir=tmp_path / "drop",
        public_tour_dir=tmp_path / "public_tours",
        slug=target_slug,
        completion_dir=completion_dir,
        dry_run=True,
    )

    assert receipt["status"] == "ready_to_import"
    assert receipt["3dvista_import_count"] == 1
    assert receipt["ignored_non_target_import_count"] == 1
    scoped_manifest = json.loads(
        (completion_dir / "3dvista-gold-import-manifest.json").read_text(encoding="utf-8")
    )
    assert [row["slug"] for row in scoped_manifest["imports"]] == [target_slug]
    assert (completion_dir / "3dvista-gold-import-manifest.json").stat().st_mode & 0o777 == 0o600

    missing_completion = tmp_path / "missing-completion"
    missing = build_3dvista_intake_receipt(
        drop_dir=tmp_path / "drop",
        public_tour_dir=tmp_path / "public_tours",
        slug="missing-exact-target",
        completion_dir=missing_completion,
        dry_run=True,
    )
    assert missing["status"] == "blocked_waiting_for_artifact"
    assert missing["3dvista_import_count"] == 0
    empty_manifest = json.loads(
        (missing_completion / "3dvista-gold-import-manifest.json").read_text(encoding="utf-8")
    )
    assert empty_manifest == {"imports": []}


def test_3dvista_trial_branded_export_is_not_premium_ready(tmp_path: Path) -> None:
    slug = "trial-branded-3dvista-import"
    _write_base_tour(tmp_path, slug)
    trial_export = tmp_path / "trial_3dvista"
    trial_export.mkdir()
    (trial_export / "index.html").write_text(
        "\n".join(
            [
                "<!doctype html><script src='runtime/app.js'></script>",
                "<div>3DVista export shell</div>",
                "<p>created with the trial of 3DVista VT Pro</p>",
            ]
        ),
        encoding="utf-8",
    )
    (trial_export / "runtime").mkdir()
    (trial_export / "runtime" / "app.js").write_text("window.TDVPlayer = true;", encoding="utf-8")

    rejected = _run_importer(
        "import_3dvista_export.py",
        tmp_path,
        "--slug",
        slug,
        "--export-dir",
        str(trial_export),
    )

    assert rejected.returncode != 0
    assert "3dvista_trial_branding_present" in rejected.stderr
    manifest = json.loads((tmp_path / "public_tours" / slug / "tour.json").read_text(encoding="utf-8"))
    assert "three_d_vista_entry_relpath" not in manifest
    assert not (tmp_path / "public_tours" / slug / "3dvista").exists()
    verifier = build_property_tour_control_receipt(
        tour_root=tmp_path / "public_tours",
        require_all_provider_modes=True,
    )
    assert verifier["provider_counts"]["3dvista"] == 0
    assert "3dvista" not in verifier["optional_provider_modes"]
    assert "3dvista" in verifier["core_missing_provider_modes"]
    blockers = verifier["provider_blockers"]["3dvista"]["reasons"]
    assert blockers[0]["reason"] == "missing_3dvista_export"
    vista_contract = verifier["delivery_contracts"]["3dvista"]
    assert vista_contract["schema"] == "propertyquarry.tour_delivery_contract.v1"
    assert vista_contract["status"] == "blocked"
    assert vista_contract["blocked_reason"] == "missing_3dvista_export"
    assert any("verified non-trial 3DVista" in item for item in vista_contract["required_to_send"])
    assert "private target-bound receipt" in " ".join(vista_contract["required_to_send"])
    assert vista_contract["white_label_contract"]["schema"] == "propertyquarry.tour_white_label_contract.v1"
    assert vista_contract["white_label_contract"]["status"] == "blocked"
    assert any("Private Viewer" in item for item in vista_contract["white_label_contract"]["required_to_white_label"])
    assert "Chummer RunSite/Horizon" in vista_contract["white_label_contract"]["cross_project_warning"]
    assert "created with the trial" not in json.dumps(vista_contract).lower()


def test_3dvista_white_label_contract_becomes_ready_for_propertyquarry_source_project(tmp_path: Path, monkeypatch) -> None:
    slug = "propertyquarry-ready-3dvista-white-label"
    _write_base_tour(tmp_path, slug)
    verified_export = tmp_path / "verified_3dvista_ready"
    verified_export.mkdir()
    (verified_export / "index.html").write_text(
        "<!doctype html><script src='runtime/app.js'></script><div>3DVista export shell</div>",
        encoding="utf-8",
    )
    (verified_export / "runtime").mkdir()
    (verified_export / "runtime" / "app.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    _write_3dvista_provenance(verified_export, slug)

    imported = _run_importer(
        "import_3dvista_export.py",
        tmp_path,
        "--slug",
        slug,
        "--export-dir",
        str(verified_export),
        "--source-project",
        "propertyquarry",
    )

    assert imported.returncode == 0, imported.stderr
    monkeypatch.setattr("scripts.verify_property_tour_controls._probe_url", _successful_provider_probe)
    verifier = build_property_tour_control_receipt(
        tour_root=tmp_path / "public_tours",
        base_url="https://propertyquarry.example",
        live_probe=True,
        require_all_provider_modes=True,
    )
    vista_contract = verifier["delivery_contracts"]["3dvista"]
    assert verifier["provider_counts"]["3dvista"] == 1
    assert vista_contract["white_label_contract"]["status"] == "ready"
    assert vista_contract["white_label_contract"]["cross_project_warning"] == ""
    proof_basis = vista_contract["white_label_contract"]["proof_basis"]
    assert proof_basis["source_projects"] == ["propertyquarry"]
    assert proof_basis["ready_basis"] == ["propertyquarry_non_trial_vt_pro_export"]
    assert proof_basis["non_trial_export_verified"] is True
    assert proof_basis["propertyquarry_tour_metadata"] is True

    imported_runtime = tmp_path / "public_tours" / slug / "3dvista" / "runtime" / "app.js"
    imported_runtime.write_text("window.TDVPlayer = false; // tampered", encoding="utf-8")
    tampered = build_property_tour_control_receipt(
        tour_root=tmp_path / "public_tours",
        base_url="https://propertyquarry.example",
        live_probe=True,
        require_all_provider_modes=True,
    )
    assert tampered["provider_counts"]["3dvista"] == 0
    assert tampered["provider_blockers"]["3dvista"]["reasons"][0]["reason"] == (
        "3dvista_target_provenance_missing_or_invalid"
    )


def test_3dvista_white_label_contract_requires_review_for_non_propertyquarry_source_project(tmp_path: Path) -> None:
    slug = "chummer-runsite-3dvista-white-label"
    bundle_dir = _write_base_tour(tmp_path, slug)
    manifest_path = bundle_dir / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "display_title": "Chummer runsite 3DVista",
            "three_d_vista_entry_relpath": "3dvista/index.html",
            "three_d_vista_import": {
                "source": "3dvista_horizon_runsite_export",
                "source_project": "chummer-runsite-horizon",
            },
            "three_d_vista_white_label_proof": {
                "source_project": "chummer-runsite-horizon",
                "source": "runsite_export",
            },
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (bundle_dir / "3dvista").mkdir(exist_ok=True)
    (bundle_dir / "3dvista" / "index.html").write_text(
        "<!doctype html><script src='tdvplayer.js'></script><div>3DVista export shell</div>",
        encoding="utf-8",
    )

    verifier = build_property_tour_control_receipt(tour_root=tmp_path / "public_tours", require_all_provider_modes=True)
    vista_contract = verifier["delivery_contracts"]["3dvista"]
    assert vista_contract["white_label_contract"]["status"] == "blocked"
    assert "Chummer RunSite/Horizon" in vista_contract["white_label_contract"]["cross_project_warning"]
    proof_basis = vista_contract["white_label_contract"]["proof_basis"]
    assert proof_basis["source_projects"] == []
    assert proof_basis["ready_basis"] == []


def test_tour_delivery_contract_requires_topology_proof_for_matterport_payload(
    tmp_path: Path,
) -> None:
    slug = "matterport-contract"
    bundle_dir = _write_base_tour(tmp_path, slug)
    manifest_path = bundle_dir / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "display_title": "Matterport Contract",
            "matterport_url": "https://my.matterport.com/show/?m=READY123",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verifier = build_property_tour_control_receipt(tour_root=tmp_path / "public_tours")
    matterport_contract = verifier["delivery_contracts"]["matterport"]
    serialized_contract = json.dumps(matterport_contract)

    assert matterport_contract["status"] == "blocked"
    assert (
        matterport_contract["blocked_reason"]
        == "matterport_model_publication_missing_or_invalid"
    )
    assert matterport_contract["required_to_send"]
    requirements = " ".join(matterport_contract["required_to_send"]).lower()
    assert "connected" in requirements
    assert "two rooms" in requirements
    assert matterport_contract["white_label_contract"]["status"] == "blocked"
    assert matterport_contract["white_label_contract"]["required_to_white_label"]
    assert matterport_contract["ready_payload"]["ready_count"] == 0
    assert matterport_contract["ready_payload"]["sample_controls"] == []
    assert "READY123" not in serialized_contract
    assert "my.matterport.com" not in serialized_contract


def test_tour_delivery_contract_checker_accepts_unproven_matterport_and_3dvista_blocked(
    tmp_path: Path,
) -> None:
    slug = "delivery-contract-checker"
    bundle_dir = _write_base_tour(tmp_path, slug)
    manifest_path = bundle_dir / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "display_title": "Matterport Delivery Contract",
            "matterport_url": "https://my.matterport.com/show/?m=READY123",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    tour_control_receipt = tmp_path / "tour-control.json"
    tour_control_receipt.write_text(
        json.dumps(build_property_tour_control_receipt(tour_root=tmp_path / "public_tours")),
        encoding="utf-8",
    )

    receipt = build_tour_delivery_contract_receipt(tour_control_receipt)

    assert receipt["status"] == "pass"
    assert receipt["matterport_ready_count"] == 0
    assert "matterport" not in receipt["ready_provider_modes"]
    assert receipt["retired_provider_modes"] == []
    assert receipt["required_provider_modes"] == [
        "matterport",
        "3dvista",
        "magicfit",
    ]
    assert receipt["optional_provider_modes"] == ["pano2vr", "krpano"]
    assert receipt["core_required_provider_mode_groups"] == [
        ["matterport", "3dvista"]
    ]
    assert receipt["core_requirement_satisfied"] is False
    assert set(receipt["missing_provider_modes"]) == {
        "matterport",
        "3dvista",
        "magicfit",
    }
    assert receipt["failures"] == []


def test_tour_delivery_contract_checker_accepts_topology_verified_matterport(
    tmp_path: Path,
) -> None:
    slug = "topology-verified-matterport"
    bundle_dir = _write_base_tour(tmp_path, slug)
    observed_at = datetime.now(timezone.utc)
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "matterport_url": "https://my.matterport.com/show/?m=READY123",
                "matterport_model_publication": {
                    "status": "pass",
                    "model_available": True,
                    "model_sid": "READY123",
                    "checked_at": observed_at.isoformat().replace("+00:00", "Z"),
                    "proof_valid_until": (
                        observed_at + timedelta(hours=24)
                    ).isoformat().replace("+00:00", "Z"),
                    "enabled_sweep_count": 23,
                    "available_sweep_count": 23,
                    "connected_component_count": 1,
                    "room_count": 6,
                    "navigation_edge_count": 49,
                    "source_sha256": "a" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    tour_control_receipt = tmp_path / "tour-control.json"
    tour_control_receipt.write_text(
        json.dumps(
            build_property_tour_control_receipt(
                tour_root=tmp_path / "public_tours"
            )
        ),
        encoding="utf-8",
    )

    receipt = build_tour_delivery_contract_receipt(tour_control_receipt)

    assert receipt["status"] == "pass"
    assert receipt["matterport_ready_count"] == 1
    assert receipt["three_d_vista_ready_count"] == 0
    assert receipt["walkable_ready_count"] == 1
    assert receipt["core_requirement_satisfied"] is True
    assert "matterport" in receipt["ready_provider_modes"]
    assert "matterport" not in receipt["missing_provider_modes"]
    assert receipt["retired_provider_modes"] == []
    assert "my.matterport.com" not in json.dumps(receipt)


def test_tour_delivery_contract_checker_rejects_matterport_url_leak(tmp_path: Path) -> None:
    receipt_path = tmp_path / "tour-control.json"
    receipt_path.write_text(
        json.dumps(
            {
                "ready_provider_modes": ["matterport"],
                "missing_provider_modes": ["3dvista", "pano2vr", "krpano", "magicfit"],
                "delivery_contracts": {
                    provider: {
                        "schema": "propertyquarry.tour_delivery_contract.v1",
                        "provider": provider,
                        "status": "blocked",
                        "ready_payload": {"provider": provider, "ready_count": 0, "sample_controls": []},
                        "blocked_reason": f"missing_{provider}",
                        "required_to_send": ["attach verified evidence"],
                        "white_label_contract": {
                            "schema": "propertyquarry.tour_white_label_contract.v1",
                            "provider": provider,
                            "status": "blocked",
                            "required_to_white_label": ["attach verified evidence"],
                            "source_project": "propertyquarry",
                            "cross_project_warning": "Chummer RunSite/Horizon white-label readiness is reusable process evidence only; it is not PropertyQuarry tour proof."
                            if provider == "3dvista"
                            else "",
                        },
                        "notes": [
                            "The viewer presents tour media only. PropertyQuarry remains source of truth for listing facts, ranking, evidence, pricing, entitlement, and customer decisions."
                        ],
                    }
                    for provider in ("matterport", "3dvista", "pano2vr", "krpano", "magicfit")
                },
            }
        ),
        encoding="utf-8",
    )
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["delivery_contracts"]["matterport"]["status"] = "ready"
    payload["delivery_contracts"]["matterport"]["blocked_reason"] = ""
    payload["delivery_contracts"]["matterport"]["required_to_send"] = []
    payload["delivery_contracts"]["matterport"]["ready_payload"] = {
        "provider": "matterport",
        "ready_count": 1,
        "sample_controls": [
            {
                "slug": "leaky",
                "title": "Leaky Matterport",
                "control_path": "/tours/leaky/control/matterport",
                "evidence": "https://my.matterport.com/show/?m=LEAKED123",
            }
        ],
    }
    payload["delivery_contracts"]["matterport"]["white_label_contract"]["status"] = "ready"
    payload["delivery_contracts"]["matterport"]["white_label_contract"]["required_to_white_label"] = []
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    receipt = build_tour_delivery_contract_receipt(receipt_path)

    assert receipt["status"] == "fail"
    assert any("my.matterport.com/show" in failure for failure in receipt["failures"])


def test_tour_delivery_contract_checker_rejects_3dvista_ready_without_propertyquarry_proof_basis(tmp_path: Path) -> None:
    receipt_path = tmp_path / "tour-control.json"
    receipt_path.write_text(
        json.dumps(
            {
                "ready_provider_modes": ["matterport", "3dvista"],
                "missing_provider_modes": ["pano2vr", "krpano", "magicfit"],
                "delivery_contracts": {
                    provider: {
                        "schema": "propertyquarry.tour_delivery_contract.v1",
                        "provider": provider,
                        "status": "ready" if provider in {"matterport", "3dvista"} else "blocked",
                        "ready_payload": {
                            "provider": provider,
                            "ready_count": 1 if provider in {"matterport", "3dvista"} else 0,
                            "sample_controls": [
                                {
                                    "slug": f"{provider}-tour",
                                    "title": f"{provider} tour",
                                    "control_path": f"/tours/{provider}-tour/control/{provider}",
                                    "evidence": f"local_{provider}_control",
                                }
                            ]
                            if provider in {"matterport", "3dvista"}
                            else [],
                        },
                        "blocked_reason": "" if provider in {"matterport", "3dvista"} else f"missing_{provider}",
                        "required_to_send": [] if provider in {"matterport", "3dvista"} else ["attach verified evidence"],
                        "white_label_contract": {
                            "schema": "propertyquarry.tour_white_label_contract.v1",
                            "provider": provider,
                            "status": "ready" if provider in {"matterport", "3dvista"} else "blocked",
                            "required_to_white_label": [] if provider in {"matterport", "3dvista"} else ["attach verified evidence"],
                            "source_project": "propertyquarry",
                            "cross_project_warning": "",
                            "proof_basis": {} if provider == "3dvista" else {},
                        },
                        "notes": [
                            "The viewer presents tour media only. PropertyQuarry remains source of truth for listing facts, ranking, evidence, pricing, entitlement, and customer decisions."
                        ],
                    }
                    for provider in ("matterport", "3dvista", "pano2vr", "krpano", "magicfit")
                },
            }
        ),
        encoding="utf-8",
    )

    receipt = build_tour_delivery_contract_receipt(receipt_path)

    assert receipt["status"] == "fail"
    assert any("3dvista ready white_label_contract must prove PropertyQuarry" in failure for failure in receipt["failures"])


def test_discovery_rejects_trial_branded_3dvista_export(tmp_path: Path) -> None:
    slug = "discover-trial-3dvista"
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, slug)
    drop_dir = tmp_path / "drop"
    trial_export = drop_dir / slug / "3dvista"
    trial_export.mkdir(parents=True)
    (trial_export / "index.htm").write_text(
        "<!doctype html><script src='tdvplayer.js'></script><p>created with the trial of 3DVista VT Pro</p>",
        encoding="utf-8",
    )
    (trial_export / "tdvplayer.js").write_text("window.TDVPlayer = true;", encoding="utf-8")

    receipt = build_discovery_receipt(drop_dir=drop_dir, public_tour_dir=public_root)

    assert receipt["status"] == "blocked_no_verified_exports"
    assert receipt["import_count"] == 0
    assert receipt["rejected"][0]["reason"] == "3dvista_trial_branding_present"


def test_discovery_rejects_trial_branded_3dvista_zip(tmp_path: Path) -> None:
    slug = "discover-trial-zip-3dvista"
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, slug)
    zip_src = tmp_path / "trial_zip_src" / "export"
    zip_src.mkdir(parents=True)
    (zip_src / "index.htm").write_text(
        "<!doctype html><script src='tdvplayer.js'></script><p>created with the trial of 3DVista VT Pro</p>",
        encoding="utf-8",
    )
    (zip_src / "tdvplayer.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    drop_dir = tmp_path / "drop"
    zip_drop = drop_dir / slug / "3dvista"
    zip_drop.mkdir(parents=True)
    with zipfile.ZipFile(zip_drop / "export.zip", "w") as archive:
        for path in zip_src.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(zip_src))

    receipt = build_discovery_receipt(drop_dir=drop_dir, public_tour_dir=public_root)

    assert receipt["status"] == "blocked_no_verified_exports"
    assert receipt["import_count"] == 0
    assert receipt["rejected"][0]["reason"] == "3dvista_trial_branding_present"


def test_attach_provider_tour_layer_adds_second_matterport_model_and_rejects_lookalike(
    tmp_path: Path,
) -> None:
    slug = "layered-matterport-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    manifest_path = bundle_dir / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["matterport_url"] = "https://my.matterport.com/show/?m=SOURCE123"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rejected = _run_importer(
        "attach_provider_tour_layer.py",
        tmp_path,
        "--slug",
        slug,
        "--provider",
        "matterport",
        "--layer-id",
        "lived-in",
        "--matterport-url",
        "https://matterport.com.evil.example/show/?m=STAGED123",
    )

    assert rejected.returncode != 0
    assert "matterport.com_url_not_allowlisted" in rejected.stderr

    attached = _run_importer(
        "attach_provider_tour_layer.py",
        tmp_path,
        "--slug",
        slug,
        "--provider",
        "matterport",
        "--layer-id",
        "lived-in",
        "--label",
        "Lived-in",
        "--matterport-url",
        "https://my.matterport.com/show/?m=STAGED123",
    )

    assert attached.returncode == 0, attached.stderr
    body = json.loads(attached.stdout)
    assert body["control_url"] == f"/tours/{slug}/control/matterport"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tour_layers"] == [
        {
            "id": "lived-in",
            "label": "Lived-in",
            "provider": "matterport",
            "matterport_url": "https://my.matterport.com/show/?m=STAGED123",
            "disclosure": "Separate staged Matterport model. The original source tour remains unchanged.",
        }
    ]


def test_attach_provider_tour_layer_adds_3dvista_same_tour_and_second_export_layers(
    tmp_path: Path,
) -> None:
    slug = "layered-3dvista-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    base_export = bundle_dir / "3dvista"
    staged_export = bundle_dir / "3dvista-staged"
    placeholder_export = bundle_dir / "3dvista-placeholder"
    for path in (base_export, staged_export, placeholder_export):
        path.mkdir()
    (base_export / "index.htm").write_text("<script src='tdvplayer.js'></script>", encoding="utf-8")
    (base_export / "tdvplayer.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    (staged_export / "index.htm").write_text("<script src='tdvplayer.js'></script><div>lived in</div>", encoding="utf-8")
    (staged_export / "tdvplayer.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    (placeholder_export / "index.htm").write_text("<title>Coming soon</title>", encoding="utf-8")
    manifest_path = bundle_dir / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "control_mode": "3dvista",
            "three_d_vista_entry_relpath": "3dvista/index.htm",
            "three_d_vista_export_root_relpath": "3dvista",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rejected = _run_importer(
        "attach_provider_tour_layer.py",
        tmp_path,
        "--slug",
        slug,
        "--provider",
        "3dvista",
        "--layer-id",
        "placeholder",
        "--three-d-vista-entry-relpath",
        "3dvista-placeholder/index.htm",
    )

    assert rejected.returncode != 0
    assert "3dvista_layer_entry_unverified" in rejected.stderr

    same_tour = _run_importer(
        "attach_provider_tour_layer.py",
        tmp_path,
        "--slug",
        slug,
        "--provider",
        "3dvista",
        "--layer-id",
        "same-tour-lived-in",
        "--label",
        "Lived-in",
        "--same-tour-layer",
        "--query",
        "startmedia=lived_in&skin=staged",
        "--fragment",
        "scene=living-room",
    )
    second_export = _run_importer(
        "attach_provider_tour_layer.py",
        tmp_path,
        "--slug",
        slug,
        "--provider",
        "3dvista",
        "--layer-id",
        "second-export-lived-in",
        "--label",
        "Staged export",
        "--three-d-vista-entry-relpath",
        "3dvista-staged/index.htm",
    )

    assert same_tour.returncode == 0, same_tour.stderr
    assert second_export.returncode == 0, second_export.stderr
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    layers = {row["id"]: row for row in manifest["tour_layers"]}
    assert layers["same-tour-lived-in"]["same_tour_layer"] is True
    assert layers["same-tour-lived-in"]["query"] == "startmedia=lived_in&skin=staged"
    assert layers["same-tour-lived-in"]["fragment"] == "scene=living-room"
    assert layers["second-export-lived-in"]["three_d_vista_entry_relpath"] == "3dvista-staged/index.htm"


def test_pano2vr_importer_materializes_verified_export_and_rejects_placeholders(tmp_path: Path) -> None:
    slug = "verified-pano2vr-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    placeholder_export = tmp_path / "placeholder_pano2vr"
    placeholder_export.mkdir()
    (placeholder_export / "index.html").write_text("<!doctype html><title>Static placeholder</title>", encoding="utf-8")

    rejected = _run_importer(
        "import_pano2vr_export.py",
        tmp_path,
        "--slug",
        slug,
        "--export-dir",
        str(placeholder_export),
    )

    assert rejected.returncode != 0
    assert "pano2vr_export_entry_unverified" in rejected.stderr
    assert not (bundle_dir / "pano2vr" / "index.html").exists()
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert "pano2vr_entry_relpath" not in manifest

    verified_export = tmp_path / "verified_pano2vr"
    verified_export.mkdir()
    (verified_export / "index.html").write_text(
        "<!doctype html><script src='assets/viewer.js'></script><div>Pano2VR export shell</div>",
        encoding="utf-8",
    )
    (verified_export / "assets").mkdir()
    (verified_export / "assets" / "viewer.js").write_text("window.GGSKIN = true;", encoding="utf-8")

    imported = _run_importer(
        "import_pano2vr_export.py",
        tmp_path,
        "--slug",
        slug,
        "--export-dir",
        str(verified_export),
    )

    assert imported.returncode == 0, imported.stderr
    body = json.loads(imported.stdout)
    assert body["control_url"] == f"/tours/{slug}/control/pano2vr"
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert manifest["control_mode"] == "pano2vr"
    assert manifest["viewer_provider"] == "pano2vr"
    assert manifest["pano2vr_entry_relpath"] == "pano2vr/index.html"
    assert (bundle_dir / "pano2vr" / "assets" / "viewer.js").exists()


def test_tour_export_discovery_accepts_vendor_named_export_folders(tmp_path: Path) -> None:
    slug = "vendor-named-tour-export"
    _write_base_tour(tmp_path, slug)
    drop_root = tmp_path / "incoming"
    tour_drop = drop_root / slug
    three_dvista_export = tour_drop / "3DVista VT Pro Export"
    pano2vr_export = tour_drop / "Pano2VR 8 Pro Output"
    three_dvista_export.mkdir(parents=True)
    pano2vr_export.mkdir(parents=True)
    (three_dvista_export / "index.htm").write_text(
        "<!doctype html><script src='tdvplayer.js'></script>",
        encoding="utf-8",
    )
    (three_dvista_export / "tdvplayer.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    _write_3dvista_provenance(three_dvista_export, slug, entry_relpath="index.htm")
    (pano2vr_export / "index.html").write_text(
        "<!doctype html><script src='tour.js'></script>",
        encoding="utf-8",
    )
    (pano2vr_export / "tour.js").write_text("window.GGSKIN = true;", encoding="utf-8")

    receipt = build_discovery_receipt(drop_dir=drop_root, public_tour_dir=tmp_path / "public_tours")

    imports = {(row["provider"], row["slug"]): row for row in receipt["imports"]}
    assert receipt["status"] == "ready"
    assert imports[("3dvista", slug)]["export_dir"] == str(three_dvista_export.resolve())
    assert imports[("3dvista", slug)]["entry"] == "index.htm"
    assert imports[("pano2vr", slug)]["export_dir"] == str(pano2vr_export.resolve())
    assert imports[("pano2vr", slug)]["entry"] == "index.html"


def test_krpano_importer_requires_real_equirectangular_panorama(tmp_path: Path, monkeypatch) -> None:
    slug = "verified-krpano-panorama-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "license-key")
    flat_image = tmp_path / "flat.jpg"
    Image.new("RGB", (1024, 768), color=(21, 31, 26)).save(flat_image, format="JPEG")

    rejected = _run_importer(
        "import_krpano_walkable_scene.py",
        tmp_path,
        "--slug",
        slug,
        "--panorama",
        str(flat_image),
    )

    assert rejected.returncode != 0
    assert "krpano_panorama_not_equirectangular" in rejected.stderr
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert "walkable_scene" not in manifest

    panorama = tmp_path / "panorama.jpg"
    _write_equirectangular_image(panorama)
    imported = _run_importer(
        "import_krpano_walkable_scene.py",
        tmp_path,
        "--slug",
        slug,
        "--panorama",
        str(panorama),
    )

    assert imported.returncode == 0, imported.stderr
    body = json.loads(imported.stdout)
    assert body["control_url"] == f"/tours/{slug}/control/krpano"
    assert body["asset_count"] == 1
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert manifest["control_mode"] == "krpano"
    assert manifest["viewer_provider"] == "krpano"
    assert manifest["scene_strategy"] == "single_panorama"
    assert manifest["creation_mode"] == "hosted_panorama_360"
    assert manifest["walkable_scene"]["projection"] == "equirectangular"
    assert manifest["walkable_scene"]["panorama_relpath"] == "krpano/panorama.jpg"
    assert manifest["krpano_import"]["license_domain"] == "propertyquarry.com"
    assert "license-key" not in json.dumps(manifest)
    _attach_panorama_spatial_provenance(bundle_dir, provider="krpano")
    verifier = build_property_tour_control_receipt(tour_root=tmp_path / "public_tours")
    assert verifier["provider_counts"]["krpano"] == 1
    assert verifier["ready_provider_modes"] == ["krpano"]


def test_krpano_importer_rejects_16_9_still_named_panorama(tmp_path: Path, monkeypatch) -> None:
    slug = "reject-flat-panorama-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "license-key")
    still = tmp_path / "panorama.jpg"
    _write_sixteen_by_nine_image(still)

    rejected = _run_importer(
        "import_krpano_walkable_scene.py",
        tmp_path,
        "--slug",
        slug,
        "--panorama",
        str(still),
    )

    assert rejected.returncode != 0
    assert "krpano_panorama_not_equirectangular" in rejected.stderr
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert "walkable_scene" not in manifest


def test_krpano_importer_accepts_six_real_cube_faces(tmp_path: Path, monkeypatch) -> None:
    slug = "verified-krpano-cube-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "license-key")
    faces = []
    for index in range(6):
        face = tmp_path / f"face-{index}.jpg"
        _write_square_image(face)
        faces.extend(["--cube-face", str(face)])

    imported = _run_importer("import_krpano_walkable_scene.py", tmp_path, "--slug", slug, *faces)

    assert imported.returncode == 0, imported.stderr
    body = json.loads(imported.stdout)
    assert body["scene_strategy"] == "single_cubemap"
    assert body["asset_count"] == 6
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert manifest["walkable_scene"]["projection"] == "cubemap"
    assert len(manifest["walkable_scene"]["cube_faces"]) == 6
    assert all((bundle_dir / relpath).is_file() for relpath in manifest["walkable_scene"]["cube_faces"].values())
    _attach_panorama_spatial_provenance(bundle_dir, provider="krpano")
    verifier = build_property_tour_control_receipt(tour_root=tmp_path / "public_tours")
    assert verifier["provider_counts"]["krpano"] == 1


def test_krpano_importer_can_materialize_existing_cube_face_scene(tmp_path: Path, monkeypatch) -> None:
    slug = "verified-krpano-existing-scene-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    manifest_path = bundle_dir / "tour.json"
    face_relpaths: dict[str, str] = {}
    for face_key in ("f", "b", "l", "r", "u", "d"):
        relpath = f"panorama/source/tablet_{face_key}.jpg"
        face_path = bundle_dir / relpath
        face_path.parent.mkdir(parents=True, exist_ok=True)
        _write_square_image(face_path)
        face_relpaths[face_key] = relpath
    manifest_path.write_text(
        json.dumps(
            {
                "slug": slug,
                "display_title": "Existing cube scene",
                "scenes": [{"name": "Living room", "cube_faces": face_relpaths}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "license-key")

    imported = _run_importer(
        "import_krpano_walkable_scene.py",
        tmp_path,
        "--slug",
        slug,
        "--from-existing-scene",
        "0",
    )

    assert imported.returncode == 0, imported.stderr
    body = json.loads(imported.stdout)
    assert body["scene_strategy"] == "single_cubemap"
    assert body["asset_count"] == 6
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["control_mode"] == "krpano"
    assert manifest["viewer_provider"] == "krpano"
    assert manifest["walkable_scene"]["projection"] == "cubemap"
    assert set(manifest["walkable_scene"]["cube_faces"]) == {"f", "b", "l", "r", "u", "d"}
    assert all((bundle_dir / relpath).is_file() for relpath in manifest["walkable_scene"]["cube_faces"].values())
    _attach_panorama_spatial_provenance(bundle_dir, provider="krpano")
    verifier = build_property_tour_control_receipt(tour_root=tmp_path / "public_tours")
    assert verifier["provider_counts"]["krpano"] == 1
    assert verifier["ready_provider_modes"] == ["krpano"]


def test_batch_tour_export_importer_materializes_verified_3dvista_and_pano2vr_exports(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, "batch-3dvista")
    _write_base_tour(tmp_path, "batch-pano2vr")
    vista_export = tmp_path / "batch_vista_export"
    vista_export.mkdir()
    (vista_export / "index.html").write_text(
        "<!doctype html><script src='runtime/app.js'></script><div>3DVista export shell</div>",
        encoding="utf-8",
    )
    (vista_export / "runtime").mkdir()
    (vista_export / "runtime" / "app.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    _write_3dvista_provenance(vista_export, "batch-3dvista")
    pano_export = tmp_path / "batch_pano_export"
    pano_export.mkdir()
    (pano_export / "index.html").write_text(
        "<!doctype html><script src='assets/viewer.js'></script><div>Pano2VR export shell</div>",
        encoding="utf-8",
    )
    (pano_export / "assets").mkdir()
    (pano_export / "assets" / "viewer.js").write_text("window.GGSKIN = true;", encoding="utf-8")
    (pano_export / "pano.xml").write_text(
        "<panorama id='node1'><hotspots /></panorama>",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "tour-imports.json"
    receipt_path = tmp_path / "tour-import-receipt.json"
    manifest_path.write_text(
        json.dumps(
            {
                "imports": [
                    {"slug": "batch-3dvista", "provider": "3dvista", "export_dir": str(vista_export)},
                    {"slug": "batch-pano2vr", "provider": "pano2vr", "export_dir": str(pano_export)},
                ]
            }
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["EA_PUBLIC_TOUR_DIR"] = str(public_root)
    imported = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "import_property_tour_exports.py"),
            "--manifest",
            str(manifest_path),
            "--write",
            str(receipt_path),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert imported.returncode == 0, imported.stderr
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "pass"
    assert receipt["imported_count"] == 2
    assert {row["provider"] for row in receipt["imports"]} == {"3dvista", "pano2vr"}
    assert all(row["status"] == "imported" for row in receipt["imports"])
    assert "batch_vista_export" not in json.dumps(receipt)
    vista_manifest = json.loads((public_root / "batch-3dvista" / "tour.json").read_text(encoding="utf-8"))
    pano_manifest = json.loads((public_root / "batch-pano2vr" / "tour.json").read_text(encoding="utf-8"))
    assert vista_manifest["control_mode"] == "3dvista"
    assert pano_manifest["control_mode"] == "pano2vr"
    _attach_panorama_spatial_provenance(
        public_root / "batch-pano2vr",
        provider="pano2vr",
    )
    monkeypatch.setattr("scripts.verify_property_tour_controls._probe_url", _successful_provider_probe)
    verifier = build_property_tour_control_receipt(
        tour_root=public_root,
        base_url="https://propertyquarry.example",
        live_probe=True,
    )
    assert verifier["provider_counts"]["3dvista"] == 1
    assert verifier["provider_counts"]["pano2vr"] == 1


def test_batch_tour_export_importer_accepts_verified_3dvista_and_pano2vr_zips(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, "zip-3dvista")
    _write_base_tour(tmp_path, "zip-pano2vr")
    vista_export = tmp_path / "vista_zip_src" / "vista-export"
    vista_export.mkdir(parents=True)
    (vista_export / "index.html").write_text(
        "<!doctype html><script src='runtime/app.js'></script><div>3DVista export shell</div>",
        encoding="utf-8",
    )
    (vista_export / "runtime").mkdir()
    (vista_export / "runtime" / "app.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    pano_export = tmp_path / "pano_zip_src" / "pano-export"
    pano_export.mkdir(parents=True)
    (pano_export / "index.html").write_text(
        "<!doctype html><script src='assets/viewer.js'></script><div>Pano2VR export shell</div>",
        encoding="utf-8",
    )
    (pano_export / "assets").mkdir()
    (pano_export / "assets" / "viewer.js").write_text("window.GGSKIN = true;", encoding="utf-8")
    (pano_export / "pano.xml").write_text(
        "<panorama id='node1'><hotspots /></panorama>",
        encoding="utf-8",
    )
    vista_provenance = _write_3dvista_provenance(vista_export, "zip-3dvista")
    vista_zip = tmp_path / "vista-export.zip"
    pano_zip = tmp_path / "pano-export.zip"
    for source_dir, target_zip in ((vista_export, vista_zip), (pano_export, pano_zip)):
        with zipfile.ZipFile(target_zip, "w") as archive:
            for path in sorted(source_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source_dir.parent).as_posix())
    manifest_path = tmp_path / "tour-imports.json"
    receipt_path = tmp_path / "tour-import-receipt.json"
    manifest_path.write_text(
        json.dumps(
            {
                "imports": [
                    {
                        "slug": "zip-3dvista",
                        "provider": "3dvista",
                        "export_zip": str(vista_zip),
                        "provenance_receipt": str(vista_provenance),
                    },
                    {"slug": "zip-pano2vr", "provider": "pano2vr", "export_zip": str(pano_zip)},
                ]
            }
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["EA_PUBLIC_TOUR_DIR"] = str(public_root)
    imported = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "import_property_tour_exports.py"),
            "--manifest",
            str(manifest_path),
            "--write",
            str(receipt_path),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert imported.returncode == 0, imported.stderr
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "pass"
    assert receipt["imported_count"] == 2
    _attach_panorama_spatial_provenance(
        public_root / "zip-pano2vr",
        provider="pano2vr",
    )
    monkeypatch.setattr("scripts.verify_property_tour_controls._probe_url", _successful_provider_probe)
    verifier = build_property_tour_control_receipt(
        tour_root=public_root,
        base_url="https://propertyquarry.example",
        live_probe=True,
    )
    assert verifier["provider_counts"]["3dvista"] == 1
    assert verifier["provider_counts"]["pano2vr"] == 1


def test_3dvista_zip_importer_rejects_placeholder_zip(tmp_path: Path) -> None:
    slug = "zip-placeholder-3dvista"
    _write_base_tour(tmp_path, slug)
    placeholder = tmp_path / "placeholder-vista"
    placeholder.mkdir()
    (placeholder / "index.html").write_text("<!doctype html><title>Coming soon</title>", encoding="utf-8")
    placeholder_zip = tmp_path / "placeholder-vista.zip"
    with zipfile.ZipFile(placeholder_zip, "w") as archive:
        archive.write(placeholder / "index.html", "index.html")

    rejected = _run_importer(
        "import_3dvista_export.py",
        tmp_path,
        "--slug",
        slug,
        "--export-zip",
        str(placeholder_zip),
    )

    assert rejected.returncode != 0
    assert "3dvista_export_entry_unverified" in rejected.stderr


def test_batch_tour_export_importer_materializes_krpano_and_magicfit_assets(tmp_path: Path, monkeypatch) -> None:
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, "batch-krpano")
    _write_base_tour(tmp_path, "batch-magicfit")
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "license-key")

    krpano_assets = tmp_path / "incoming" / "batch-krpano" / "krpano"
    krpano_assets.mkdir(parents=True)
    _write_equirectangular_image(krpano_assets / "panorama.jpg")

    magicfit_assets = tmp_path / "incoming" / "batch-magicfit" / "magicfit"
    magicfit_assets.mkdir(parents=True)
    video_path = magicfit_assets / "magicfit-walkthrough.mp4"
    _write_playable_mp4(video_path)
    receipt_path = magicfit_assets / "magicfit-receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "provider": "magicfit",
                "provider_key": "magicfit",
                "provider_backend_key": "magicfit",
                "render_status": "completed",
                "video_output_url": "https://media.powlcdn.com/magicfit/batch-magicfit.mp4",
                "hosted_walkthrough_video_url": "https://media.powlcdn.com/magicfit/batch-magicfit.mp4",
                "target_slug": "batch-magicfit",
                "output_file": str(video_path.resolve()),
            }
        ),
        encoding="utf-8",
    )

    manifest_path = tmp_path / "tour-imports.json"
    receipt_out = tmp_path / "tour-import-receipt.json"
    manifest_path.write_text(
        json.dumps(
            {
                "imports": [
                    {"slug": "batch-krpano", "provider": "krpano", "asset_dir": str(krpano_assets)},
                    {"slug": "batch-magicfit", "provider": "magicfit", "asset_dir": str(magicfit_assets)},
                ]
            }
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["EA_PUBLIC_TOUR_DIR"] = str(public_root)
    imported = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "import_property_tour_exports.py"),
            "--manifest",
            str(manifest_path),
            "--write",
            str(receipt_out),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert imported.returncode == 0, imported.stderr
    receipt = json.loads(receipt_out.read_text(encoding="utf-8"))
    assert receipt["status"] == "staged_pending_delivery_acceptance"
    assert receipt["imported_count"] == 2
    assert receipt["accepted_count"] == 1
    assert receipt["staged_count"] == 1
    assert receipt["successful_count"] == 2
    assert receipt["failed_count"] == 0
    rows = {row["provider"]: row for row in receipt["imports"]}
    assert rows["krpano"]["status"] == "imported"
    assert rows["magicfit"]["status"] == "staged_pending_delivery_acceptance"
    assert rows["magicfit"]["acceptance_status"] == "pending"
    assert rows["magicfit"]["launch_eligible"] is False
    assert "control_url" not in rows["magicfit"]
    assert not any("url" in key.lower() for key in rows["magicfit"])
    krpano_manifest = json.loads((public_root / "batch-krpano" / "tour.json").read_text(encoding="utf-8"))
    magicfit_manifest = json.loads((public_root / "batch-magicfit" / "tour.json").read_text(encoding="utf-8"))
    assert krpano_manifest["control_mode"] == "krpano"
    assert krpano_manifest["walkable_scene"]["panorama_relpath"] == "krpano/panorama.jpg"
    assert "video_provider" not in magicfit_manifest
    assert "video_relpath" not in magicfit_manifest
    _attach_panorama_spatial_provenance(
        public_root / "batch-krpano",
        provider="krpano",
    )
    verifier = build_property_tour_control_receipt(tour_root=public_root)
    assert verifier["provider_counts"]["krpano"] == 1
    assert verifier["provider_counts"]["magicfit"] == 0
    magicfit_pending = json.loads(
        (public_root / "batch-magicfit" / "tour.magicfit.pending.json").read_text(
            encoding="utf-8"
        )
    )
    assert magicfit_pending["acceptance_status"] == "pending"
    assert magicfit_pending["launch_eligible"] is False
    staged_video = public_root / "batch-magicfit" / magicfit_pending["staged_video_relpath"]
    assert staged_video.is_file()
    assert staged_video.stat().st_mode & 0o777 == 0o600
    assert not (
        public_root / "batch-magicfit" / magicfit_pending["video_relpath"]
    ).exists()


def test_batch_tour_export_importer_fails_placeholder_rows_without_false_ready(tmp_path: Path) -> None:
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, "batch-placeholder")
    placeholder_export = tmp_path / "placeholder_export"
    placeholder_export.mkdir()
    (placeholder_export / "index.html").write_text("<!doctype html><title>Coming soon</title>", encoding="utf-8")
    manifest_path = tmp_path / "tour-imports.json"
    receipt_path = tmp_path / "tour-import-receipt.json"
    manifest_path.write_text(
        json.dumps({"imports": [{"slug": "batch-placeholder", "provider": "pano2vr", "export_dir": str(placeholder_export)}]}),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["EA_PUBLIC_TOUR_DIR"] = str(public_root)
    rejected = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "import_property_tour_exports.py"),
            "--manifest",
            str(manifest_path),
            "--write",
            str(receipt_path),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert rejected.returncode == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "fail"
    assert receipt["failed_count"] == 1
    assert receipt["imports"][0]["error"] == "pano2vr_export_entry_unverified"
    verifier = build_property_tour_control_receipt(tour_root=public_root)
    assert verifier["provider_counts"]["pano2vr"] == 0


def test_magicfit_importer_materializes_playable_walkthrough_and_rejects_placeholders(tmp_path: Path) -> None:
    slug = "verified-magicfit-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    placeholder_video = tmp_path / "placeholder.mp4"
    placeholder_video.write_bytes(b"not a playable video")

    rejected = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        slug,
        "--video-path",
        str(placeholder_video),
    )

    assert rejected.returncode != 0
    assert "magicfit_video_unverified" in rejected.stderr
    assert not (bundle_dir / "magicfit-walkthrough.mp4").exists()
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert "video_relpath" not in manifest
    assert "magicfit_import" not in manifest

    stub_video = tmp_path / "signature-only.mp4"
    stub_video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
    stub_rejected = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        slug,
        "--video-path",
        str(stub_video),
        "--allow-unreceipted-test-asset",
    )

    assert stub_rejected.returncode != 0
    assert "magicfit_video_unverified" in stub_rejected.stderr
    assert not (bundle_dir / "magicfit-walkthrough.mp4").exists()
    staging_root = bundle_dir / ".magicfit-staging"
    assert staging_root.is_dir()
    assert list(staging_root.iterdir()) == []

    playable_video = tmp_path / "walkthrough.mp4"
    _write_playable_mp4(playable_video)
    playable_video.chmod(0o600)
    unreceipted = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        slug,
        "--video-path",
        str(playable_video),
    )

    assert unreceipted.returncode != 0
    assert "magicfit_receipt_missing" in unreceipted.stderr
    assert not (bundle_dir / "magicfit-walkthrough.mp4").exists()

    receipt_path = tmp_path / "walkthrough.magicfit.json"
    receipt_path.write_text(
        json.dumps(
            {
                "provider": "MagicFit",
                "video_output_url": "https://media.powlcdn.com/magicfit/example.mp4",
                "output_file": str(playable_video),
                "target_slug": "different-tour",
                "generated_at": "2026-06-25T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    mismatched = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        slug,
        "--video-path",
        str(playable_video),
        "--source-receipt",
        str(receipt_path),
    )

    assert mismatched.returncode != 0
    assert "magicfit_source_receipt_provider_invalid" in mismatched.stderr
    assert not (bundle_dir / "magicfit-walkthrough.mp4").exists()

    receipt_path.write_text(
        json.dumps(
            {
                "provider": "MagicFit",
                "video_output_url": "https://media.powlcdn.com/magicfit/example.mp4",
                "output_file": str(playable_video),
                "target_slug": slug,
                "property_slug": slug,
                "property_title": "Import target",
                "generated_at": "2026-06-25T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    weak_receipt = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        slug,
        "--video-path",
        str(playable_video),
        "--source-receipt",
        str(receipt_path),
    )

    assert weak_receipt.returncode != 0
    assert "magicfit_source_receipt_provider_invalid" in weak_receipt.stderr
    assert not (bundle_dir / "magicfit-walkthrough.mp4").exists()

    receipt_path.write_text(
        json.dumps(
            {
                "provider": "magicfit",
                "provider_key": "magicfit",
                "provider_backend_key": "magicfit",
                "render_status": "completed",
                "video_output_url": "https://media.powlcdn.com/magicfit/example.mp4",
                "hosted_walkthrough_video_url": "https://media.powlcdn.com/magicfit/example.mp4",
                "output_file": str(playable_video),
                "target_slug": slug,
                "property_slug": slug,
                "property_title": "Import target",
                "generated_at": "2026-06-25T00:00:00Z",
                "walkthrough_coverage_proof": {
                    "status": "pass",
                    "segments_expected": ["entry", "living", "kitchen"],
                    "segments_visited": ["entry", "living", "kitchen"],
                    "coverage_segments": [
                        {"segment": "entry", "start": 0, "end": 10},
                        {"segment": "living", "start": 10, "end": 22},
                        {"segment": "kitchen", "start": 22, "end": 35},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    imported = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        slug,
        "--video-path",
        str(playable_video),
        "--target-relpath",
        "walkthrough/final.mp4",
        "--source-receipt",
        str(receipt_path),
    )

    assert imported.returncode == 0, imported.stderr
    body = json.loads(imported.stdout)
    assert body["status"] == "staged_pending_delivery_acceptance"
    assert "video_url" not in body
    assert body["provider"] == "magicfit"
    assert body["provider_backend_key"] == "magicfit"
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert manifest == {"slug": slug, "display_title": "Import target"}
    assert "video_provider" not in manifest
    assert "video_relpath" not in manifest

    pending_path = bundle_dir / "tour.magicfit.pending.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending_path.stat().st_mode & 0o777 == 0o600
    assert pending["contract_name"] == (
        "propertyquarry.magicfit_delivery_pending.v2"
    )
    assert pending["manifest_transform_contract"] == (
        "propertyquarry.magicfit_manifest_transform.v1"
    )
    assert pending["tour_slug"] == slug
    assert pending["requested_target_relpath"] == "walkthrough/final.mp4"
    assert pending["video_size_bytes"] == playable_video.stat().st_size
    assert pending["coverage_proof"]["status"] == "pass"
    assert pending["render_status"] == "completed"
    assert pending["generated_at"].endswith("Z")
    assert pending["acceptance_status"] == "pending"
    assert pending["launch_eligible"] is False
    assert pending["video_relpath"].startswith("magicfit-media/final.")
    assert pending["video_relpath"].endswith(".mp4")
    assert body["video_relpath_after_acceptance"] == pending["video_relpath"]
    assert body["public_video_url_after_acceptance"] == (
        f"/tours/files/{slug}/{pending['video_relpath']}"
    )
    assert pending["source_receipt_sha256"] == hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()

    staged_video = bundle_dir / pending["staged_video_relpath"]
    assert staged_video.read_bytes() == playable_video.read_bytes()
    assert staged_video.stat().st_mode & 0o777 == 0o600
    assert not (bundle_dir / pending["video_relpath"]).exists()
    assert not (bundle_dir / pending["accepted_sidecar_relpath"]).exists()

    staged_manifest_path = bundle_dir / pending["staged_manifest_relpath"]
    assert staged_manifest_path.stat().st_mode & 0o777 == 0o600
    staged_manifest = json.loads(staged_manifest_path.read_text(encoding="utf-8"))
    assert staged_manifest["video_provider"] == "magicfit"
    assert staged_manifest["video_provider_backend_key"] == "magicfit"
    assert staged_manifest["video_relpath"] == pending["video_relpath"]
    assert staged_manifest["video_sidecar_relpath"] == pending["accepted_sidecar_relpath"]
    assert (
        staged_manifest["video_coverage_proof"] == "route_coverage_verified"
    )
    assert staged_manifest["walkthrough_coverage_proof"]["status"] == "pass"
    assert staged_manifest["walkthrough_coverage_proof"]["segments_expected"] == ["entry", "living", "kitchen"]
    assert staged_manifest["magicfit_import"]["source"] == "magicfit_rendered_walkthrough"
    assert staged_manifest["magicfit_import"]["provider_backend_key"] == "magicfit"
    assert staged_manifest["magicfit_import"]["proof_status"] == "delivery_accepted"
    assert "source_receipt_path" not in staged_manifest["magicfit_import"]
    assert len(staged_manifest["magicfit_import"]["source_receipt_sha256"]) == 64
    assert staged_manifest["magicfit_import"]["coverage_proof"]["coverage_segments"][0]["segment"] == "entry"
    assert staged_manifest["magicfit_import"]["size_bytes"] == playable_video.stat().st_size
    assert len(staged_manifest["magicfit_import"]["sha256"]) == 64

    pending_receipt = build_property_tour_control_receipt(
        tour_root=tmp_path / "public_tours"
    )
    assert pending_receipt["provider_counts"]["magicfit"] == 0
    assert pending_receipt["magicfit_playback"]["playback_ok"] is True
    assert pending_receipt["magicfit_playback"]["playable_count"] == 0
    assert pending_receipt["magicfit_playback"]["ready_count"] == 0
    assert pending_receipt["ready_provider_modes"] == []
    pending_missing = {
        row["provider"]: row
        for row in pending_receipt["tours"][0]["missing_evidence"]
    }
    assert pending_missing["magicfit"]["reason"] == "magicfit_walkthrough_disqualified"
    pending_actions = {
        row["provider"]: row["action"]
        for row in pending_receipt["next_required_actions"]
    }
    assert "complete delivery acceptance" in pending_actions["magicfit"]

    # Status flips on the private pending pointer cannot activate either the
    # staged manifest or staged media.
    pending["acceptance_status"] = "accepted"
    pending["launch_eligible"] = True
    pending_path.write_text(json.dumps(pending), encoding="utf-8")
    status_flip_receipt = build_property_tour_control_receipt(
        tour_root=tmp_path / "public_tours"
    )
    assert status_flip_receipt["provider_counts"]["magicfit"] == 0
    status_flip_missing = {
        row["provider"]: row
        for row in status_flip_receipt["tours"][0]["missing_evidence"]
    }
    assert status_flip_missing["magicfit"]["reason"] == (
        "magicfit_walkthrough_disqualified"
    )
    assert json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8")) == manifest
    assert not (bundle_dir / pending["video_relpath"]).exists()


def test_magicfit_importer_retires_previous_and_bounded_closed_orphans(
    tmp_path: Path,
) -> None:
    slug = "magicfit-stage-retirement"
    bundle = _write_base_tour(tmp_path, slug)
    stage_root = bundle / ".magicfit-staging"
    previous_digest = "2" * 64
    orphan_digest = "3" * 64
    unknown_digest = "4" * 64
    for digest in (previous_digest, orphan_digest):
        stage = stage_root / digest
        stage.mkdir(parents=True)
        (stage / "tour.json").write_text("{}\n", encoding="utf-8")
        (stage / "video.mp4").write_bytes(b"closed-orphan")
    unknown_stage = stage_root / unknown_digest
    unknown_stage.mkdir()
    (unknown_stage / "operator-authority.bin").write_bytes(b"preserve")
    (bundle / "tour.magicfit.pending.json").write_text(
        json.dumps(
            {
                "staged_manifest_relpath": (
                    f".magicfit-staging/{previous_digest}/tour.json"
                ),
                "staged_video_relpath": (
                    f".magicfit-staging/{previous_digest}/video.mp4"
                ),
            }
        ),
        encoding="utf-8",
    )

    video = tmp_path / "retirement.mp4"
    _write_playable_mp4(video)
    receipt = tmp_path / "retirement-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "provider": "magicfit",
                "provider_backend_key": "magicfit",
                "render_status": "completed",
                "target_slug": slug,
                "hosted_walkthrough_video_url": (
                    "https://media.powlcdn.com/magicfit/retirement.mp4"
                ),
                "output_file": str(video),
            }
        ),
        encoding="utf-8",
    )

    imported = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        slug,
        "--video-path",
        str(video),
        "--source-receipt",
        str(receipt),
    )

    assert imported.returncode == 0, imported.stderr
    pending = json.loads(
        (bundle / "tour.magicfit.pending.json").read_text(encoding="utf-8")
    )
    selected_digest = PurePosixPath(pending["staged_manifest_relpath"]).parts[1]
    assert (stage_root / selected_digest).is_dir()
    assert not (stage_root / previous_digest).exists()
    assert not (stage_root / orphan_digest).exists()
    assert (unknown_stage / "operator-authority.bin").read_bytes() == b"preserve"


@pytest.mark.parametrize("symlinked_subject", ("video", "source_receipt"))
def test_magicfit_importer_rejects_symlinked_integrity_subjects(
    tmp_path: Path,
    symlinked_subject: str,
) -> None:
    slug = f"symlinked-magicfit-{symlinked_subject.replace('_', '-')}"
    bundle = _write_base_tour(tmp_path, slug)
    real_video = tmp_path / "real-walkthrough.mp4"
    _write_playable_mp4(real_video)
    real_receipt = tmp_path / "real-magicfit-receipt.json"
    real_receipt.write_text(
        json.dumps(
            {
                "provider": "magicfit",
                "provider_backend_key": "magicfit",
                "render_status": "completed",
                "hosted_walkthrough_video_url": (
                    "https://media.powlcdn.com/magicfit/symlinked.mp4"
                ),
                "output_file": str(real_video),
                "target_slug": slug,
            }
        ),
        encoding="utf-8",
    )
    video_argument = real_video
    receipt_argument = real_receipt
    if symlinked_subject == "video":
        video_argument = tmp_path / "linked-walkthrough.mp4"
        video_argument.symlink_to(real_video.name)
    else:
        receipt_argument = tmp_path / "linked-magicfit-receipt.json"
        receipt_argument.symlink_to(real_receipt.name)

    rejected = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        slug,
        "--video-path",
        str(video_argument),
        "--source-receipt",
        str(receipt_argument),
    )

    assert rejected.returncode != 0
    assert "magicfit_video" in rejected.stderr or "magicfit_receipt" in rejected.stderr
    assert not (bundle / "tour.magicfit.pending.json").exists()


def test_magicfit_importer_rejects_ambiguous_receipts_and_noncanonical_paths(
    tmp_path: Path,
) -> None:
    playable_video = tmp_path / "walkthrough.mp4"
    _write_playable_mp4(playable_video)
    hosted_url = "https://media.powlcdn.com/magicfit/example.mp4"

    def valid_receipt(target_slug: object) -> dict[str, object]:
        return {
            "provider": "magicfit",
            "provider_backend_key": "magicfit",
            "render_status": "completed",
            "hosted_walkthrough_video_url": hosted_url,
            "output_file": str(playable_video),
            "target_slug": target_slug,
        }

    duplicate_slug = "duplicate-receipt"
    duplicate_bundle = _write_base_tour(tmp_path, duplicate_slug)
    duplicate_receipt = tmp_path / "duplicate-receipt.json"
    duplicate_body = json.dumps(valid_receipt(duplicate_slug))
    duplicate_receipt.write_text(
        '{"provider":"wrong-provider",' + duplicate_body[1:],
        encoding="utf-8",
    )
    duplicate_result = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        duplicate_slug,
        "--video-path",
        str(playable_video),
        "--source-receipt",
        str(duplicate_receipt),
    )
    assert duplicate_result.returncode != 0
    assert "magicfit_receipt_invalid:_DuplicateJsonKey" in duplicate_result.stderr
    assert not (duplicate_bundle / "magicfit-walkthrough.mp4").exists()

    nonfinite_slug = "nonfinite-receipt"
    nonfinite_bundle = _write_base_tour(tmp_path, nonfinite_slug)
    nonfinite_receipt = tmp_path / "nonfinite-receipt.json"
    nonfinite_payload = valid_receipt(nonfinite_slug)
    nonfinite_payload["unused_metric"] = float("nan")
    nonfinite_receipt.write_text(json.dumps(nonfinite_payload), encoding="utf-8")
    nonfinite_result = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        nonfinite_slug,
        "--video-path",
        str(playable_video),
        "--source-receipt",
        str(nonfinite_receipt),
    )
    assert nonfinite_result.returncode != 0
    assert "magicfit_receipt_invalid:ValueError" in nonfinite_result.stderr
    assert not (nonfinite_bundle / "magicfit-walkthrough.mp4").exists()

    numeric_slug = "123"
    numeric_bundle = _write_base_tour(tmp_path, numeric_slug)
    numeric_receipt = tmp_path / "numeric-receipt.json"
    numeric_receipt.write_text(
        json.dumps(valid_receipt(123)),
        encoding="utf-8",
    )
    numeric_result = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        numeric_slug,
        "--video-path",
        str(playable_video),
        "--source-receipt",
        str(numeric_receipt),
    )
    assert numeric_result.returncode != 0
    assert "magicfit_source_receipt_slug_invalid" in numeric_result.stderr
    assert not (numeric_bundle / "magicfit-walkthrough.mp4").exists()

    output_slug = "output-mismatch-receipt"
    output_bundle = _write_base_tour(tmp_path, output_slug)
    output_receipt = tmp_path / "output-mismatch-receipt.json"
    output_payload = valid_receipt(output_slug)
    output_payload["output_file"] = str(tmp_path / "different.mp4")
    output_receipt.write_text(json.dumps(output_payload), encoding="utf-8")
    output_result = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        output_slug,
        "--video-path",
        str(playable_video),
        "--source-receipt",
        str(output_receipt),
    )
    assert output_result.returncode != 0
    assert "magicfit_receipt_output_mismatch" in output_result.stderr
    assert not (output_bundle / "tour.magicfit.pending.json").exists()

    target_slug = "noncanonical-target"
    target_bundle = _write_base_tour(tmp_path, target_slug)
    target_receipt = tmp_path / "target-receipt.json"
    target_receipt.write_text(
        json.dumps(valid_receipt(target_slug)),
        encoding="utf-8",
    )
    target_result = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        target_slug,
        "--video-path",
        str(playable_video),
        "--target-relpath",
        "nested/../final.mp4",
        "--source-receipt",
        str(target_receipt),
    )
    assert target_result.returncode != 0
    assert "invalid_magicfit_target" in target_result.stderr
    assert not (target_bundle / "nested" / "final.mp4").exists()

    slug_alias = "canonical-target"
    alias_bundle = _write_base_tour(tmp_path, slug_alias)
    alias_receipt = tmp_path / "alias-receipt.json"
    alias_receipt.write_text(
        json.dumps(valid_receipt(slug_alias)),
        encoding="utf-8",
    )
    alias_result = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        f"../{slug_alias}",
        "--video-path",
        str(playable_video),
        "--source-receipt",
        str(alias_receipt),
    )
    assert alias_result.returncode != 0
    assert "invalid_tour_slug" in alias_result.stderr
    assert not (alias_bundle / "magicfit-walkthrough.mp4").exists()


def test_krpano_control_requires_real_walkable_360_asset(tmp_path: Path, monkeypatch) -> None:
    slug = "verified-krpano-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "license-key")

    manifest_path = bundle_dir / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "scene_strategy": "photo_gallery_hosted",
            "creation_mode": "hosted_photo_gallery_tour",
            "walkable_scene": {"projection": "equirectangular", "panorama_relpath": "flat-photo.jpg"},
        }
    )
    (bundle_dir / "flat-photo.jpg").write_bytes(b"not actually inspected as panorama, but forbidden strategy blocks it")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rejected = build_property_tour_control_receipt(tour_root=tmp_path / "public_tours")
    assert rejected["provider_counts"]["krpano"] == 0
    assert rejected["tours"][0]["status"] == "blocked_missing_verified_controls"
    assert rejected["tours"][0]["blocked_reason"] == "gallery_only_not_3d"
    assert rejected["tours"][0]["missing_evidence"] == []

    manifest.update(
        {
            "scene_strategy": "single_panorama",
            "creation_mode": "hosted_panorama_360",
            "walkable_scene": {"projection": "equirectangular", "panorama_relpath": "panorama.jpg"},
        }
    )
    _write_equirectangular_image(bundle_dir / "panorama.jpg")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _attach_panorama_spatial_provenance(bundle_dir, provider="krpano")

    accepted = build_property_tour_control_receipt(tour_root=tmp_path / "public_tours")
    assert accepted["provider_counts"]["krpano"] == 1
    assert accepted["ready_provider_modes"] == ["krpano"]
    assert accepted["tours"][0]["controls"][0]["evidence"] == (
        "provenance_bound_licensed_krpano_spatial_scene"
    )


def test_krpano_control_rejects_16_9_stills_as_fake_panorama(tmp_path: Path, monkeypatch) -> None:
    slug = "reject-16-9-krpano"
    bundle_dir = _write_base_tour(tmp_path, slug)
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "license-key")
    manifest_path = bundle_dir / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "scene_strategy": "walkable_panorama",
            "creation_mode": "hosted_walkable_360",
            "walkable_scene": {"projection": "equirectangular", "panorama_relpath": "still-16-9.jpg"},
        }
    )
    _write_sixteen_by_nine_image(bundle_dir / "still-16-9.jpg")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    receipt = build_property_tour_control_receipt(tour_root=tmp_path / "public_tours")

    assert receipt["provider_counts"]["krpano"] == 0
    assert all(row["provider"] != "krpano" for row in receipt["tours"][0]["missing_evidence"])
    optional_missing = receipt["tours"][0]["optional_missing_evidence"]
    assert any(
        row["provider"] == "krpano" and row["reason"] == "walkable_scene_asset_missing_or_not_360"
        for row in optional_missing
    )
    assert receipt["provider_blockers"]["krpano"]["reasons"][0]["reason"] == "walkable_scene_asset_missing_or_not_360"


def test_tour_export_discovery_emits_manifest_for_verified_drop_folders(tmp_path: Path) -> None:
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, "discover-3dvista")
    _write_base_tour(tmp_path, "discover-pano2vr")
    _write_base_tour(tmp_path, "discover-krpano")
    _write_base_tour(tmp_path, "discover-magicfit")
    drop_dir = tmp_path / "drop"
    vista_export = drop_dir / "discover-3dvista" / "3dvista"
    vista_export.mkdir(parents=True)
    (vista_export / "index.html").write_text(
        "<!doctype html><script src='runtime/app.js'></script><div>3DVista export shell</div>",
        encoding="utf-8",
    )
    (vista_export / "runtime").mkdir()
    (vista_export / "runtime" / "app.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    _write_3dvista_provenance(vista_export, "discover-3dvista")
    pano_export = drop_dir / "pano2vr" / "discover-pano2vr"
    pano_export.mkdir(parents=True)
    (pano_export / "index.html").write_text(
        "<!doctype html><script src='assets/viewer.js'></script><div>Pano2VR export shell</div>",
        encoding="utf-8",
    )
    (pano_export / "assets").mkdir()
    (pano_export / "assets" / "viewer.js").write_text("window.GGSKIN = true;", encoding="utf-8")
    krpano_assets = drop_dir / "discover-krpano" / "krpano"
    krpano_assets.mkdir(parents=True)
    _write_equirectangular_image(krpano_assets / "panorama.jpg")
    magicfit_assets = drop_dir / "magicfit" / "discover-magicfit"
    magicfit_assets.mkdir(parents=True)
    magicfit_video = magicfit_assets / "magicfit-walkthrough.mp4"
    _write_playable_mp4(magicfit_video)
    (magicfit_assets / "magicfit-receipt.json").write_text(
        json.dumps(
            _magicfit_source_receipt(
                slug="discover-magicfit",
                video=magicfit_video,
            )
        ),
        encoding="utf-8",
    )
    receipt_path = tmp_path / "discovery.json"
    manifest_path = tmp_path / "imports.json"

    discovered = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "discover_property_tour_exports.py"),
            "--drop-dir",
            str(drop_dir),
            "--public-tour-dir",
            str(public_root),
            "--write",
            str(receipt_path),
            "--manifest-write",
            str(manifest_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert discovered.returncode == 0, discovered.stderr
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "ready"
    assert receipt["import_count"] == 4
    assert receipt["rejected_count"] == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert {row["provider"] for row in manifest["imports"]} == {"3dvista", "pano2vr", "krpano", "magicfit"}
    assert {row["slug"] for row in manifest["imports"]} == {
        "discover-3dvista",
        "discover-pano2vr",
        "discover-krpano",
        "discover-magicfit",
    }
    assert {
        row["entry"]
        for row in manifest["imports"]
        if row["provider"] in {"3dvista", "pano2vr"}
    } == {"index.html"}
    assert any(row["provider"] == "krpano" and row["panorama"].endswith("panorama.jpg") for row in manifest["imports"])
    assert any(row["provider"] == "magicfit" and row["video"].endswith("magicfit-walkthrough.mp4") for row in manifest["imports"])


def test_tour_export_discovery_emits_explicit_krpano_cube_faces(tmp_path: Path) -> None:
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, "discover-krpano-cube")
    drop_dir = tmp_path / "drop"
    krpano_assets = drop_dir / "discover-krpano-cube" / "krpano"
    krpano_assets.mkdir(parents=True)
    for index in range(1, 7):
        _write_square_image(krpano_assets / f"cube-face-{index}.jpg")
    receipt_path = tmp_path / "discovery.json"
    manifest_path = tmp_path / "imports.json"

    discovered = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "discover_property_tour_exports.py"),
            "--drop-dir",
            str(drop_dir),
            "--public-tour-dir",
            str(public_root),
            "--write",
            str(receipt_path),
            "--manifest-write",
            str(manifest_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert discovered.returncode == 0, discovered.stderr
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    row = manifest["imports"][0]
    assert row["provider"] == "krpano"
    assert "panorama" not in row
    assert {row[f"cube_face_{index}"].rsplit("/", 1)[-1] for index in range(1, 7)} == {
        f"cube-face-{index}.jpg" for index in range(1, 7)
    }


def test_tour_export_discovery_accepts_verified_provider_zips(tmp_path: Path) -> None:
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, "discover-zip-3dvista")
    _write_base_tour(tmp_path, "discover-zip-pano2vr")
    drop_dir = tmp_path / "drop"
    vista_src = tmp_path / "vista-src" / "export"
    vista_src.mkdir(parents=True)
    (vista_src / "index.html").write_text("<script src='runtime/app.js'></script>", encoding="utf-8")
    (vista_src / "runtime").mkdir()
    (vista_src / "runtime" / "app.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    pano_src = tmp_path / "pano-src" / "export"
    pano_src.mkdir(parents=True)
    (pano_src / "index.html").write_text("<script src='assets/viewer.js'></script>", encoding="utf-8")
    (pano_src / "assets").mkdir()
    (pano_src / "assets" / "viewer.js").write_text("window.GGSKIN = true;", encoding="utf-8")
    vista_drop = drop_dir / "discover-zip-3dvista" / "3dvista"
    pano_drop = drop_dir / "discover-zip-pano2vr" / "pano2vr"
    vista_drop.mkdir(parents=True)
    pano_drop.mkdir(parents=True)
    for source_dir, target_zip in ((vista_src, vista_drop / "export.zip"), (pano_src, pano_drop / "export.zip")):
        with zipfile.ZipFile(target_zip, "w") as archive:
            for path in sorted(source_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source_dir.parent).as_posix())
    vista_provenance = _write_3dvista_provenance(vista_src, "discover-zip-3dvista")
    shutil.copy2(vista_provenance, vista_drop / "3dvista-target-provenance.json")
    receipt_path = tmp_path / "discovery.json"
    manifest_path = tmp_path / "imports.json"

    discovered = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "discover_property_tour_exports.py"),
            "--drop-dir",
            str(drop_dir),
            "--public-tour-dir",
            str(public_root),
            "--write",
            str(receipt_path),
            "--manifest-write",
            str(manifest_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert discovered.returncode == 0, discovered.stderr
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "ready"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = {(row["slug"], row["provider"]): row for row in manifest["imports"]}
    assert rows[("discover-zip-3dvista", "3dvista")]["export_zip"].endswith("export.zip")
    assert rows[("discover-zip-pano2vr", "pano2vr")]["export_zip"].endswith("export.zip")


def test_tour_export_discovery_resolves_stale_drop_when_provider_already_imported_live(tmp_path: Path) -> None:
    public_root = tmp_path / "public_tours"
    bundle_dir = _write_base_tour(tmp_path, "existing-3dvista")
    manifest_path = bundle_dir / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["three_d_vista_entry_relpath"] = "3dvista/index.htm"
    manifest["three_d_vista_import"] = {
        "source": "3dvista_private_viewer_runtime_refresh",
        "entry_relpath": "3dvista/index.htm",
        "target_subdir": "3dvista",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    live_export = bundle_dir / "3dvista"
    live_export.mkdir()
    (live_export / "index.htm").write_text(
        "<!doctype html><script src='tdvplayer.js'></script><div>3DVista export shell</div>",
        encoding="utf-8",
    )
    (live_export / "tdvplayer.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    provenance_path = _write_3dvista_provenance(
        live_export,
        "existing-3dvista",
        entry_relpath="index.htm",
    )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["target_subdir"] = "3dvista"
    provenance_path.unlink()
    (bundle_dir / "tour.private.json").write_text(
        json.dumps({"three_d_vista_target_provenance": provenance}),
        encoding="utf-8",
    )

    drop_dir = tmp_path / "drop"
    stale_export = drop_dir / "existing-3dvista" / "3dvista"
    stale_export.mkdir(parents=True)
    (stale_export / "index.html").write_text("<!doctype html><title>Coming soon</title>", encoding="utf-8")

    receipt = build_discovery_receipt(drop_dir=drop_dir, public_tour_dir=public_root)

    assert receipt["status"] == "ready"
    assert receipt["import_count"] == 0
    assert receipt["rejected_count"] == 0
    assert receipt["repair_count"] == 0
    assert receipt["resolved_existing_import_count"] == 1
    resolved = receipt["resolved_existing_imports"][0]
    assert resolved["provider"] == "3dvista"
    assert resolved["reason"] == "3dvista_export_entry_unverified"
    assert resolved["status"] == "already_imported_live_bundle"
    assert resolved["live_evidence"] == "public_bundle_3dvista_import"
    assert resolved["live_control_path"] == "/tours/existing-3dvista/control/3dvista"


def test_tour_export_discovery_does_not_resolve_unbound_live_3dvista_manifest(tmp_path: Path) -> None:
    public_root = tmp_path / "public_tours"
    bundle_dir = _write_base_tour(tmp_path, "unbound-live-3dvista")
    manifest_path = bundle_dir / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["three_d_vista_entry_relpath"] = "3dvista/index.htm"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    drop_dir = tmp_path / "drop"
    stale_export = drop_dir / "unbound-live-3dvista" / "3dvista"
    stale_export.mkdir(parents=True)
    (stale_export / "index.html").write_text("<!doctype html><title>Coming soon</title>", encoding="utf-8")

    receipt = build_discovery_receipt(drop_dir=drop_dir, public_tour_dir=public_root)

    assert receipt["status"] == "blocked_no_verified_exports"
    assert receipt["resolved_existing_import_count"] == 0
    assert receipt["rejected_count"] == 1
    assert receipt["rejected"][0]["reason"] == "3dvista_export_entry_unverified"


def test_tour_export_discovery_ignores_duplicate_rejected_drop_when_same_provider_is_importable(
    tmp_path: Path,
) -> None:
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, "duplicate-pano2vr")

    drop_dir = tmp_path / "drop"
    valid_export = drop_dir / "duplicate-pano2vr" / "pano2vr"
    valid_export.mkdir(parents=True)
    (valid_export / "index.html").write_text("<script src='assets/viewer.js'></script>", encoding="utf-8")
    (valid_export / "assets").mkdir()
    (valid_export / "assets" / "viewer.js").write_text("window.GGSKIN = true;", encoding="utf-8")

    duplicate_placeholder = drop_dir / "pano2vr" / "duplicate-pano2vr"
    duplicate_placeholder.mkdir(parents=True)
    (duplicate_placeholder / "index.html").write_text("<!doctype html><title>Coming soon</title>", encoding="utf-8")

    receipt = build_discovery_receipt(drop_dir=drop_dir, public_tour_dir=public_root)

    assert receipt["status"] == "ready"
    assert receipt["import_count"] == 1
    assert receipt["rejected_count"] == 0
    assert receipt["ignored_duplicate_drop_count"] == 1
    ignored = receipt["ignored_duplicate_drop_rows"][0]
    assert ignored["provider"] == "pano2vr"
    assert ignored["reason"] == "pano2vr_export_entry_unverified"
    assert ignored["status"] == "ignored_duplicate_drop"
    assert "already importable" in ignored["resolution"]


def test_tour_export_discovery_rejects_16_9_krpano_panorama_candidates(tmp_path: Path) -> None:
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, "discover-flat-krpano")
    drop_dir = tmp_path / "drop"
    krpano_assets = drop_dir / "discover-flat-krpano" / "krpano"
    krpano_assets.mkdir(parents=True)
    _write_sixteen_by_nine_image(krpano_assets / "panorama.jpg")
    receipt_path = tmp_path / "discovery.json"

    discovered = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "discover_property_tour_exports.py"),
            "--drop-dir",
            str(drop_dir),
            "--public-tour-dir",
            str(public_root),
            "--write",
            str(receipt_path),
            "--fail-on-blocked",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert discovered.returncode == 2
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    handoff_path = receipt_path.with_name("discovery.handoff.md")
    assert handoff_path.is_file()
    handoff = handoff_path.read_text(encoding="utf-8")
    assert "PropertyQuarry Tour Export Handoff" in handoff
    assert "Gold remains blocked until real provider assets are copied into the drop folders" in handoff
    assert receipt["status"] == "blocked_no_verified_exports"
    assert receipt["rejected"][0]["reason"] == "krpano_assets_missing"
    assert receipt["repair_manifest"][0]["reason"] == "krpano_assets_missing"


def test_tour_export_discovery_rejects_magicfit_receipt_mismatch_before_import(tmp_path: Path) -> None:
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, "discover-magicfit")
    drop_dir = tmp_path / "drop"
    magicfit_assets = drop_dir / "discover-magicfit" / "magicfit"
    magicfit_assets.mkdir(parents=True)
    magicfit_video = magicfit_assets / "magicfit-walkthrough.mp4"
    _write_playable_mp4(magicfit_video)
    source_receipt = _magicfit_source_receipt(
        slug="different-tour",
        video=magicfit_video,
    )
    (magicfit_assets / "magicfit-receipt.json").write_text(
        json.dumps(source_receipt),
        encoding="utf-8",
    )
    receipt_path = tmp_path / "discovery.json"

    discovered = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "discover_property_tour_exports.py"),
            "--drop-dir",
            str(drop_dir),
            "--public-tour-dir",
            str(public_root),
            "--write",
            str(receipt_path),
            "--fail-on-blocked",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert discovered.returncode == 2
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "blocked_no_verified_exports"
    assert receipt["import_count"] == 0
    assert len(receipt["rejected"]) == 1
    assert receipt["repair_count"] == 1
    rejection = receipt["rejected"][0]
    assert rejection["slug"] == "discover-magicfit"
    assert rejection["provider"] == "magicfit"
    assert rejection["reason"] == "magicfit_receipt_target_mismatch"
    assert "target_slug" in rejection["action"]
    assert "magicfit-walkthrough" in rejection["drop_layout"]
    repair = receipt["repair_manifest"][0]
    assert repair["status"] == "waiting_for_verified_assets"
    assert repair["reason"] == "magicfit_receipt_target_mismatch"
    assert "import_magicfit_walkthrough.py" in repair["import_command_after_assets_arrive"]
    assert "magicfit-receipt.json" in repair["import_command_after_assets_arrive"]


def test_tour_export_discovery_enforces_closed_magicfit_source_contract(
    tmp_path: Path,
) -> None:
    public_root = tmp_path / "public_tours"
    drop_dir = tmp_path / "drop"
    template_video = tmp_path / "magicfit-template.mp4"
    _write_playable_mp4(template_video)
    expected_reasons = {
        "magicfit-receipt-missing": "magicfit_receipt_missing",
        "magicfit-status-invalid": "magicfit_receipt_status_invalid",
        "magicfit-url-missing": "magicfit_receipt_url_invalid",
        "magicfit-url-alias-conflict": "magicfit_receipt_url_invalid",
        "magicfit-provider-alias-conflict": "magicfit_receipt_provider_mismatch",
        "magicfit-slug-alias-conflict": "magicfit_receipt_target_mismatch",
    }

    for slug in expected_reasons:
        _write_base_tour(tmp_path, slug)
        assets = drop_dir / slug / "magicfit"
        assets.mkdir(parents=True)
        video = assets / "magicfit-walkthrough.mp4"
        shutil.copy2(template_video, video)
        if slug == "magicfit-receipt-missing":
            continue
        source_receipt = _magicfit_source_receipt(slug=slug, video=video)
        if slug == "magicfit-status-invalid":
            source_receipt["render_status"] = "processing"
        elif slug == "magicfit-url-missing":
            source_receipt.pop("hosted_walkthrough_video_url")
            source_receipt.pop("video_output_url")
        elif slug == "magicfit-url-alias-conflict":
            source_receipt["video_output_url"] = (
                "https://cdn.pushowl.com/magicfit/different.mp4"
            )
        elif slug == "magicfit-provider-alias-conflict":
            source_receipt["provider_key"] = "MagicFit"
        elif slug == "magicfit-slug-alias-conflict":
            source_receipt["tour_slug"] = "different-tour"
        (assets / "magicfit-receipt.json").write_text(
            json.dumps(source_receipt),
            encoding="utf-8",
        )

    receipt = build_discovery_receipt(
        drop_dir=drop_dir,
        public_tour_dir=public_root,
    )

    assert receipt["status"] == "blocked_no_verified_exports"
    assert receipt["import_count"] == 0
    assert receipt["rejected_count"] == len(expected_reasons)
    rejected_by_slug = {row["slug"]: row for row in receipt["rejected"]}
    assert set(rejected_by_slug) == set(expected_reasons)
    for slug, reason in expected_reasons.items():
        assert rejected_by_slug[slug]["reason"] == reason
        assert rejected_by_slug[slug]["action"] == REJECTION_ACTIONS[reason]


def test_tour_export_discovery_rejects_magicfit_symlinks_and_sparse_subjects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_TOUR_ASSET_MAX_BYTES", str(1024 * 1024))
    monkeypatch.setenv("PROPERTYQUARRY_MAGICFIT_RECEIPT_MAX_BYTES", "1024")
    public_root = tmp_path / "public_tours"
    drop_dir = tmp_path / "drop"
    template_video = tmp_path / "magicfit-template.mp4"
    _write_playable_mp4(template_video)
    expected_reasons = {
        "magicfit-video-symlink": "magicfit_video_unverified",
        "magicfit-receipt-symlink": "magicfit_receipt_invalid",
        "magicfit-component-symlink": "magicfit_video_unverified",
        "magicfit-sparse-video": "magicfit_video_unverified",
        "magicfit-sparse-receipt": "magicfit_receipt_invalid",
    }

    video_symlink_slug = "magicfit-video-symlink"
    _write_base_tour(tmp_path, video_symlink_slug)
    video_symlink_assets = drop_dir / video_symlink_slug / "magicfit"
    video_symlink_assets.mkdir(parents=True)
    video_symlink = video_symlink_assets / "magicfit-walkthrough.mp4"
    video_symlink.symlink_to(template_video)
    (video_symlink_assets / "magicfit-receipt.json").write_text(
        json.dumps(
            _magicfit_source_receipt(
                slug=video_symlink_slug,
                video=video_symlink,
            )
        ),
        encoding="utf-8",
    )

    receipt_symlink_slug = "magicfit-receipt-symlink"
    _write_base_tour(tmp_path, receipt_symlink_slug)
    receipt_symlink_assets = drop_dir / receipt_symlink_slug / "magicfit"
    receipt_symlink_assets.mkdir(parents=True)
    receipt_symlink_video = receipt_symlink_assets / "magicfit-walkthrough.mp4"
    shutil.copy2(template_video, receipt_symlink_video)
    external_receipt = tmp_path / "external-magicfit-receipt.json"
    external_receipt.write_text(
        json.dumps(
            _magicfit_source_receipt(
                slug=receipt_symlink_slug,
                video=receipt_symlink_video,
            )
        ),
        encoding="utf-8",
    )
    (receipt_symlink_assets / "magicfit-receipt.json").symlink_to(
        external_receipt
    )

    component_symlink_slug = "magicfit-component-symlink"
    _write_base_tour(tmp_path, component_symlink_slug)
    external_assets = tmp_path / "external-magicfit-assets"
    external_assets.mkdir()
    external_video = external_assets / "magicfit-walkthrough.mp4"
    shutil.copy2(template_video, external_video)
    (external_assets / "magicfit-receipt.json").write_text(
        json.dumps(
            _magicfit_source_receipt(
                slug=component_symlink_slug,
                video=external_video,
            )
        ),
        encoding="utf-8",
    )
    component_parent = drop_dir / component_symlink_slug
    component_parent.mkdir(parents=True)
    (component_parent / "magicfit").symlink_to(
        external_assets,
        target_is_directory=True,
    )

    sparse_video_slug = "magicfit-sparse-video"
    _write_base_tour(tmp_path, sparse_video_slug)
    sparse_video_assets = drop_dir / sparse_video_slug / "magicfit"
    sparse_video_assets.mkdir(parents=True)
    sparse_video = sparse_video_assets / "magicfit-walkthrough.mp4"
    sparse_video.write_bytes(b"\x00\x00\x00\x18ftypisom")
    with sparse_video.open("r+b") as stream:
        stream.truncate(2 * 1024 * 1024)
    (sparse_video_assets / "magicfit-receipt.json").write_text(
        json.dumps(
            _magicfit_source_receipt(
                slug=sparse_video_slug,
                video=sparse_video,
            )
        ),
        encoding="utf-8",
    )

    sparse_receipt_slug = "magicfit-sparse-receipt"
    _write_base_tour(tmp_path, sparse_receipt_slug)
    sparse_receipt_assets = drop_dir / sparse_receipt_slug / "magicfit"
    sparse_receipt_assets.mkdir(parents=True)
    sparse_receipt_video = sparse_receipt_assets / "magicfit-walkthrough.mp4"
    shutil.copy2(template_video, sparse_receipt_video)
    sparse_receipt = sparse_receipt_assets / "magicfit-receipt.json"
    sparse_receipt.write_text(
        json.dumps(
            _magicfit_source_receipt(
                slug=sparse_receipt_slug,
                video=sparse_receipt_video,
            )
        ),
        encoding="utf-8",
    )
    with sparse_receipt.open("r+b") as stream:
        stream.truncate(2 * 1024)

    receipt = build_discovery_receipt(
        drop_dir=drop_dir,
        public_tour_dir=public_root,
    )

    assert receipt["status"] == "blocked_no_verified_exports"
    assert receipt["import_count"] == 0
    assert receipt["rejected_count"] == len(expected_reasons)
    rejected_by_slug = {row["slug"]: row for row in receipt["rejected"]}
    assert set(rejected_by_slug) == set(expected_reasons)
    for slug, reason in expected_reasons.items():
        assert rejected_by_slug[slug]["reason"] == reason
        assert rejected_by_slug[slug]["action"] == REJECTION_ACTIONS[reason]


def test_tour_export_discovery_rejects_placeholders_and_missing_tour_manifests(tmp_path: Path) -> None:
    public_root = tmp_path / "public_tours"
    _write_base_tour(tmp_path, "placeholder-tour")
    drop_dir = tmp_path / "drop"
    placeholder = drop_dir / "placeholder-tour" / "pano2vr"
    placeholder.mkdir(parents=True)
    (placeholder / "index.html").write_text("<!doctype html><title>Coming soon</title>", encoding="utf-8")
    krpano_placeholder = drop_dir / "placeholder-tour" / "krpano"
    krpano_placeholder.mkdir(parents=True)
    magicfit_placeholder = drop_dir / "placeholder-tour" / "magicfit"
    magicfit_placeholder.mkdir(parents=True)
    orphan = drop_dir / "orphan-tour" / "3dvista"
    orphan.mkdir(parents=True)
    (orphan / "index.html").write_text("<!doctype html><script src='tdvplayer.js'></script>", encoding="utf-8")
    receipt_path = tmp_path / "discovery.json"

    discovered = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "discover_property_tour_exports.py"),
            "--drop-dir",
            str(drop_dir),
            "--public-tour-dir",
            str(public_root),
            "--write",
            str(receipt_path),
            "--fail-on-blocked",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert discovered.returncode == 2
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "blocked_no_verified_exports"
    assert receipt["import_count"] == 0
    assert {row["reason"] for row in receipt["rejected"]} == {
        "krpano_assets_missing",
        "magicfit_video_missing",
        "pano2vr_export_entry_unverified",
        "tour_manifest_missing",
    }
    assert receipt["repair_count"] == 4
    assert {row["reason"] for row in receipt["repair_manifest"]} == {
        "krpano_assets_missing",
        "magicfit_video_missing",
        "pano2vr_export_entry_unverified",
        "tour_manifest_missing",
    }
    for row in receipt["rejected"]:
        assert row["action"]
        assert row["drop_layout"]
        assert row["drop_path"]
    pano_rejection = next(row for row in receipt["rejected"] if row["provider"] == "pano2vr")
    assert pano_rejection["file_count"] == 1
    assert pano_rejection["present_sample"] == ["index.html"]
    assert pano_rejection["entry_candidates"] == ["index.html"]
    assert pano_rejection["runtime_marker_status"] == "missing"
    assert pano_rejection["missing"] == ["pano2vr_runtime_marker"]
    assert pano_rejection["missing_markers"] == ["ggpkg", "ggskin", "pano.xml", "tour.js"]
    orphan_rejection = next(row for row in receipt["rejected"] if row["provider"] == "3dvista")
    assert orphan_rejection["reason"] == "tour_manifest_missing"
    assert orphan_rejection["runtime_marker_status"] == "verified"
    assert orphan_rejection["verified_entry"] == "index.html"
    assert orphan_rejection["missing"] == []
    assert "missing_markers" not in orphan_rejection
    for row in receipt["repair_manifest"]:
        assert row["status"] == "waiting_for_verified_assets"
        assert row["required_action"]
        assert row["drop_layout"]
        assert row["drop_path"]
    pano_repair = next(row for row in receipt["repair_manifest"] if row["provider"] == "pano2vr")
    assert pano_repair["file_count"] == 1
    assert pano_repair["present_sample"] == ["index.html"]
    assert pano_repair["runtime_marker_status"] == "missing"
    assert pano_repair["missing_markers"] == ["ggpkg", "ggskin", "pano.xml", "tour.js"]
    orphan_repair = next(row for row in receipt["repair_manifest"] if row["provider"] == "3dvista")
    assert orphan_repair["runtime_marker_status"] == "verified"
    assert orphan_repair["verified_entry"] == "index.html"
    assert orphan_repair["missing"] == []
    assert "missing_markers" not in orphan_repair
    assert any("import_pano2vr_export.py" in row["import_command_after_assets_arrive"] for row in receipt["repair_manifest"])
    assert any("import_krpano_walkable_scene.py" in row["import_command_after_assets_arrive"] for row in receipt["repair_manifest"])
    assert any("import_magicfit_walkthrough.py" in row["import_command_after_assets_arrive"] for row in receipt["repair_manifest"])
    assert any("ggpkg" in row["action"] for row in receipt["rejected"] if row["provider"] == "pano2vr")
    assert any("panorama" in row["action"] for row in receipt["rejected"] if row["provider"] == "krpano")
    handoff_path = receipt_path.with_name("discovery.handoff.md")
    assert handoff_path.is_file()
    handoff = handoff_path.read_text(encoding="utf-8")
    assert "pano2vr · placeholder-tour" in handoff
    assert "Files found: `1`" in handoff
    assert "Present sample: `index.html`" in handoff
    assert "Required markers/evidence: `ggpkg, ggskin, pano.xml, tour.js`" in handoff
    assert "import_pano2vr_export.py" in handoff


def test_tour_export_discovery_ignores_operator_drop_lane_readmes(tmp_path: Path) -> None:
    public_root = tmp_path / "public_tours"
    drop_dir = tmp_path / "drop"
    for provider in ("3dvista", "pano2vr", "krpano", "magicfit"):
        export_dir = drop_dir / "_operator-import-lane" / provider
        export_dir.mkdir(parents=True)
        (export_dir / "README.propertyquarry-export.txt").write_text(
            f"Operator drop instructions for {provider}",
            encoding="utf-8",
        )
    receipt_path = tmp_path / "discovery.json"

    discovered = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "discover_property_tour_exports.py"),
            "--drop-dir",
            str(drop_dir),
            "--public-tour-dir",
            str(public_root),
            "--write",
            str(receipt_path),
            "--fail-on-blocked",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert discovered.returncode == 2
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "blocked_no_verified_exports"
    assert receipt["import_count"] == 0
    assert receipt["rejected_count"] == 0
    assert receipt["rejected"] == []
    assert receipt["repair_count"] == 0
    assert receipt["repair_manifest"] == []
    assert any("readme.propertyquarry-export.txt" in note.lower() for note in receipt["notes"])


def test_tour_export_discovery_ignores_documentation_only_provider_folders_for_normal_slugs(tmp_path: Path) -> None:
    public_root = tmp_path / "public_tours"
    drop_dir = tmp_path / "drop"
    for provider in ("3dvista", "pano2vr", "krpano"):
        export_dir = drop_dir / "normal-slug" / provider
        export_dir.mkdir(parents=True)
        (export_dir / "README.propertyquarry-export.txt").write_text(
            f"Instructions for {provider}",
            encoding="utf-8",
        )
    receipt = build_discovery_receipt(drop_dir=drop_dir, public_tour_dir=public_root)

    assert receipt["status"] == "blocked_no_verified_exports"
    assert receipt["import_count"] == 0
    assert receipt["rejected_count"] == 0
    assert receipt["rejected"] == []
    assert receipt["repair_count"] == 0
    assert receipt["repair_manifest"] == []
    assert any("readme.propertyquarry-export.txt" in note.lower() for note in receipt["notes"])


def test_magicfit_importer_fails_closed_on_low_disk_before_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "low-disk-magicfit-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    source = tmp_path / "magicfit-low-disk.mp4"
    _write_playable_mp4(source)
    monkeypatch.setenv(
        "PROPERTYQUARRY_TOUR_MIN_FREE_BYTES",
        str(10 * 1024 * 1024 * 1024 * 1024),
    )

    rejected = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        slug,
        "--video-path",
        str(source),
        "--allow-unreceipted-test-asset",
    )

    assert rejected.returncode != 0
    assert "magicfit_import_low_disk" in rejected.stderr
    assert not (bundle_dir / "magicfit-walkthrough.mp4").exists()
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert "video_relpath" not in manifest


def test_magicfit_importer_serializes_on_canonical_publication_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "publication-locked-magicfit-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    source = tmp_path / "publication-locked-magicfit.mp4"
    _write_playable_mp4(source)
    manifest_before = (bundle_dir / "tour.json").read_bytes()
    monkeypatch.setenv(
        "PROPERTYQUARRY_RECONSTRUCTION_PUBLICATION_LOCK_TIMEOUT_SECONDS",
        "0.05",
    )

    with property_tour_publication_lock(
        public_dir=tmp_path / "public_tours",
        slug=slug,
        timeout_seconds=1.0,
    ):
        blocked = _run_importer(
            "import_magicfit_walkthrough.py",
            tmp_path,
            "--slug",
            slug,
            "--video-path",
            str(source),
            "--allow-unreceipted-test-asset",
        )

    assert blocked.returncode != 0
    assert "property_reconstruction_publication_lock_timeout" in blocked.stderr
    assert (bundle_dir / "tour.json").read_bytes() == manifest_before
    assert not (bundle_dir / "tour.magicfit.pending.json").exists()

    imported = _run_importer(
        "import_magicfit_walkthrough.py",
        tmp_path,
        "--slug",
        slug,
        "--video-path",
        str(source),
        "--allow-unreceipted-test-asset",
    )
    assert imported.returncode == 0, imported.stderr


def test_magicfit_importer_rejects_named_bundle_swap_before_pointer_commit(
    tmp_path: Path,
) -> None:
    slug = "bundle-swapped-magicfit-import"
    bundle_dir = _write_base_tour(tmp_path, slug)
    public_root = tmp_path / "public_tours"
    moved_bundle = bundle_dir.with_name(f"{slug}.moved")

    with _magicfit_import_activation_lock(
        public_root, slug
    ) as (public_root_fd, bundle_fd):
        bundle_dir.rename(moved_bundle)
        bundle_dir.symlink_to(moved_bundle.name, target_is_directory=True)

        with pytest.raises(SystemExit, match="magicfit_import_bundle_changed"):
            _confirm_magicfit_import_bundle_identity(
                public_root_fd,
                slug,
                bundle_fd,
            )
