from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.materialize_property_tour_export_manifest import (
    _artifact_dir,
    _incoming_root,
    _tour_root,
    build_drop_status_rows,
    build_export_manifest,
    prepare_export_drop_dirs,
)
from scripts.property_tour_3dvista_provenance import (
    THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
    sha256_text,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_equirectangular_image(path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2048, 1024), color=(28, 42, 36)).save(path, format="JPEG")


def _write_base_tour(root: Path, slug: str) -> None:
    bundle = root / slug
    bundle.mkdir(parents=True)
    (bundle / "tour.json").write_text(json.dumps({"slug": slug, "display_title": slug}), encoding="utf-8")


def test_materialize_property_tour_export_manifest_writes_operator_drop_paths(tmp_path: Path) -> None:
    tour_root = tmp_path / "public_tours"
    incoming_root = tmp_path / "incoming"
    _write_base_tour(tour_root, "needs-exports")

    manifest = build_export_manifest(tour_root=tour_root, incoming_root=incoming_root, limit_per_provider=1)

    assert manifest["status"] == "waiting_for_verified_assets"
    assert manifest["tour_root"] == str(tour_root.resolve())
    assert manifest["incoming_root"] == str(incoming_root.resolve())
    assert set(manifest["providers"]) == {"3dvista", "pano2vr", "krpano", "magicfit"}
    assert manifest["import_count"] == 4
    imports = {(row["provider"], row["slug"]): row for row in manifest["imports"]}
    assert imports[("3dvista", "needs-exports")]["export_dir"] == str(incoming_root.resolve() / "needs-exports" / "3dvista")
    assert imports[("pano2vr", "needs-exports")]["export_dir"] == str(incoming_root.resolve() / "needs-exports" / "pano2vr")
    assert imports[("krpano", "needs-exports")]["asset_dir"] == str(incoming_root.resolve() / "needs-exports" / "krpano")
    assert imports[("magicfit", "needs-exports")]["asset_dir"] == str(incoming_root.resolve() / "needs-exports" / "magicfit")
    assert len(manifest["drop_status"]) == 4
    assert {row["status"] for row in manifest["drop_status"]} == {"waiting_for_assets"}
    assert {row["missing"][0] for row in manifest["drop_status"]} == {"drop_folder"}
    assert manifest["drop_status_summary"] == {"ready_for_import": 0, "waiting_for_assets": 4, "other": 0}
    assert "import_property_tour_exports.py" in manifest["next_command"]


def test_materialize_property_tour_export_manifest_defaults_to_repo_state_incoming_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR", raising=False)
    repo = tmp_path / "property"
    repo.mkdir()
    (repo / "docker-compose.property.yml").write_text("services: {}\n", encoding="utf-8")
    (repo / "state").mkdir()
    monkeypatch.chdir(repo)

    assert _incoming_root() == (repo / "state" / "incoming_property_tours").resolve()


def test_materialize_property_tour_export_manifest_reads_plural_artifacts_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    monkeypatch.delenv("EA_ARTIFACT_DIR", raising=False)
    monkeypatch.setenv("EA_ARTIFACTS_DIR", str(artifact_root))

    assert _artifact_dir() == artifact_root.resolve()


def test_materialize_property_tour_export_manifest_defaults_artifacts_to_repo_completion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("EA_ARTIFACT_DIR", raising=False)
    monkeypatch.delenv("EA_ARTIFACTS_DIR", raising=False)
    repo = tmp_path / "property"
    repo.mkdir()
    (repo / "docker-compose.property.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    assert _artifact_dir() == (repo / "_completion" / "property_tour_exports").resolve()


def test_materialize_property_tour_export_manifest_tour_root_prefers_runtime_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime-public-tours"
    runtime_root.mkdir()
    repo = tmp_path / "property"
    repo.mkdir()
    (repo / "docker-compose.property.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "scripts.materialize_property_tour_export_manifest.preferred_public_tour_root",
        lambda **kwargs: runtime_root.resolve(),
    )

    assert _tour_root() == runtime_root.resolve()


def test_materialize_property_tour_export_manifest_loads_krpano_env_defaults_before_verifying(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "property"
    tour_root = repo / "public_tours"
    incoming_root = repo / "incoming"
    repo.mkdir()
    (repo / "docker-compose.property.yml").write_text("services: {}\n", encoding="utf-8")
    (repo / ".env").write_text(
        "KRPANO_LICENSE_DOMAIN=propertyquarry.com\n"
        "KRPANO_LICENSE_KEY=redacted-demo-value\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    monkeypatch.delenv("KRPANO_LICENSE_DOMAIN", raising=False)
    monkeypatch.delenv("KRPANO_LICENSE_KEY", raising=False)
    _write_base_tour(tour_root, "needs-env-aware-verifier")

    expected_tour_root = tour_root.resolve()

    def fake_build_property_tour_control_receipt(*, tour_root: Path, require_all_provider_modes: bool) -> dict[str, object]:
        assert require_all_provider_modes is True
        assert tour_root == expected_tour_root
        assert os.getenv("KRPANO_LICENSE_DOMAIN") == "propertyquarry.com"
        assert os.getenv("KRPANO_LICENSE_KEY") == "redacted-demo-value"
        return {
            "missing_provider_modes": ["magicfit"],
            "tours": [
                {
                    "slug": "needs-env-aware-verifier",
                    "title": "needs-env-aware-verifier",
                    "status": "ready",
                    "controls": [
                        {"provider": "matterport", "status": "ready"},
                        {"provider": "3dvista", "status": "ready"},
                        {"provider": "pano2vr", "status": "ready"},
                        {"provider": "krpano", "status": "ready"},
                    ],
                    "missing_provider_modes": ["magicfit"],
                    "missing_evidence": [
                        {
                            "provider": "magicfit",
                            "reason": "missing_magicfit_walkthrough",
                            "action": "render and import a receipt-backed playable MagicFit walkthrough",
                        }
                    ],
                }
            ],
        }

    monkeypatch.setattr(
        "scripts.materialize_property_tour_export_manifest.build_property_tour_control_receipt",
        fake_build_property_tour_control_receipt,
    )

    manifest = build_export_manifest(
        tour_root=tour_root,
        incoming_root=incoming_root,
        limit_per_provider=1,
    )

    assert manifest["missing_provider_modes"] == ["magicfit"]
    assert manifest["providers"] == ["magicfit"]
    assert manifest["import_count"] == 1
    assert manifest["imports"][0]["provider"] == "magicfit"


def test_materialize_property_tour_export_manifest_prioritizes_ready_tour_gaps(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tour_root = tmp_path / "public_tours"
    incoming_root = tmp_path / "incoming"
    _write_base_tour(tour_root, "blocked-needs-exports")
    slug = "3dvista-ready"
    provider_url = "https://example.3dvista.com/tours/READY3D/index.html"
    ready_bundle = tour_root / slug
    ready_bundle.mkdir(parents=True)
    (ready_bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "display_title": "3DVista Ready",
            }
        ),
        encoding="utf-8",
    )
    (ready_bundle / "tour.private.json").write_text(
        json.dumps(
            {
                "three_d_vista_url": provider_url,
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
                "three_d_vista_target_provenance": {
                    "schema": THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
                    "status": "pass",
                    "provider": "3dvista",
                    "target_slug": slug,
                    "artifact": {
                        "kind": "hosted_url",
                        "sha256": sha256_text(provider_url),
                        "entry_relpath": "",
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
                    "target_subdir": "",
                },
            }
        ),
        encoding="utf-8",
    )

    manifest = build_export_manifest(tour_root=tour_root, incoming_root=incoming_root, limit_per_provider=1)

    assert manifest["status"] == "waiting_for_verified_assets"
    assert manifest["import_count"] == 4
    imports = {row["provider"]: row for row in manifest["imports"]}
    assert imports["3dvista"]["slug"] == "blocked-needs-exports"
    ready_gap_providers = {"pano2vr", "krpano", "magicfit"}
    assert {imports[provider]["slug"] for provider in ready_gap_providers} == {slug}
    assert {imports[provider]["current_control_providers"] for provider in ready_gap_providers} == {"3dvista"}
    assert {imports[provider]["title"] for provider in ready_gap_providers} == {"3DVista Ready"}


def test_materialize_property_tour_export_manifest_prepares_drop_dir_readmes(tmp_path: Path) -> None:
    tour_root = tmp_path / "public_tours"
    incoming_root = tmp_path / "incoming"
    _write_base_tour(tour_root, "needs-exports")

    manifest = build_export_manifest(tour_root=tour_root, incoming_root=incoming_root, limit_per_provider=1)
    prepared = prepare_export_drop_dirs(manifest)

    assert len(prepared) == 4
    for row in prepared:
        readme = Path(row["readme"])
        assert readme.is_file()
        body = readme.read_text(encoding="utf-8")
        assert "PropertyQuarry provider export drop folder" in body
        assert f"Slug: {row['slug']}" in body
        assert f"Provider: {row['provider']}" in body
        assert "Do not copy placeholder HTML" in body
        assert "Current drop status: waiting_for_assets" in body
        assert "Missing now:" in body
        assert "import_property_tour_exports.py" in body
        assert "Single-provider dry import example:" in body
        assert "Core Gold requires the verified first-party 3DVista customer tour" in body
        assert "MagicFit is prepared here for the separate Advanced Visual Gold lane" in body
        assert "exact receipt, playback, quota, privacy, and isolation evidence" in body
        assert "Pano2VR is an optional/internal export lane" in body
        assert "matterport, 3dvista, krpano, and magicfit" not in body
        assert "matterport, 3dvista, pano2vr, krpano, and magicfit" not in body.lower()
        assert "Pano2VR is an optional/internal export lane" in body
        assert "generated cube fallbacks" in body
        if row["provider"] == "3dvista":
            assert "tdvplayer" in body
            assert "3DVista .zip export" in body
            assert "import_3dvista_export.py" in body
            assert "--export-zip" in body
        if row["provider"] == "pano2vr":
            assert "tour.js" in body
            assert "Pano2VR .zip export" in body
            assert "import_pano2vr_export.py" in body
            assert "--export-zip" in body
        if row["provider"] == "krpano":
            assert "equirectangular" in body
            assert "real captured/exported" in body
            assert "cube-face-1" in body
            assert "KRPANO_LICENSE_DOMAIN=propertyquarry.com" in body
            assert "import_krpano_walkable_scene.py" in body
        if row["provider"] == "magicfit":
            assert "MagicFit render receipt" in body
            assert "magicfit-walkthrough.mp4" in body
            assert "magicfit-receipt.json" in body
            assert "import_magicfit_walkthrough.py" in body
        assert row["drop_status"]["status"] == "waiting_for_assets"
        assert row["drop_status"]["missing"]


def test_materialize_property_tour_export_manifest_falls_back_when_drop_readme_is_unwritable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tour_root = tmp_path / "public_tours"
    incoming_root = tmp_path / "incoming"
    artifact_root = tmp_path / "artifacts"
    _write_base_tour(tour_root, "needs-exports")
    monkeypatch.setenv("EA_ARTIFACT_DIR", str(artifact_root))
    original_write_text = Path.write_text

    def write_text_with_drop_permission_error(self: Path, *args, **kwargs):
        if self.name == "README.propertyquarry-export.txt" and incoming_root in self.parents:
            raise PermissionError("drop readme is not writable")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text_with_drop_permission_error)

    manifest = build_export_manifest(
        tour_root=tour_root,
        incoming_root=incoming_root,
        providers={"3dvista"},
        limit_per_provider=1,
    )
    prepared = prepare_export_drop_dirs(manifest)

    assert len(prepared) == 4
    row = next(row for row in prepared if row["provider"] == "3dvista")
    assert row["provider"] == "3dvista"
    assert "PermissionError" in row["readme_write_error"]
    assert row["artifact_readme_write_error"] == ""
    assert Path(row["readme"]) == Path(row["artifact_readme"])
    assert Path(row["artifact_readme"]).is_file()
    body = Path(row["artifact_readme"]).read_text(encoding="utf-8")
    assert "PropertyQuarry provider export drop folder" in body
    assert "Copy the complete 3DVista export folder" in body
    assert "import_3dvista_export.py" in body


def test_materialize_property_tour_export_manifest_uses_repo_local_readme_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tour_root = tmp_path / "public_tours"
    incoming_root = tmp_path / "incoming"
    artifact_root = tmp_path / "unwritable_artifacts"
    _write_base_tour(tour_root, "needs-exports")
    monkeypatch.setenv("EA_ARTIFACT_DIR", str(artifact_root))
    monkeypatch.chdir(tmp_path)
    original_write_text = Path.write_text

    def write_text_with_permission_errors(self: Path, *args, **kwargs):
        if self.name == "README.propertyquarry-export.txt" and (
            incoming_root in self.parents or artifact_root in self.parents
        ):
            raise PermissionError("configured readme target is not writable")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text_with_permission_errors)

    manifest = build_export_manifest(
        tour_root=tour_root,
        incoming_root=incoming_root,
        providers={"pano2vr"},
        limit_per_provider=1,
    )
    prepared = prepare_export_drop_dirs(manifest)

    assert len(prepared) == 4
    row = next(row for row in prepared if row["provider"] == "pano2vr")
    assert row["provider"] == "pano2vr"
    assert Path(row["readme"]) == tmp_path / "_completion" / "property_tour_exports" / "drop-readmes" / "needs-exports" / "pano2vr" / "README.propertyquarry-export.txt"
    assert row["artifact_readme"] == row["readme"]
    assert row["artifact_readme_write_error"] == ""
    body = Path(row["readme"]).read_text(encoding="utf-8")
    assert "Copy the complete Pano2VR output folder" in body
    assert "import_pano2vr_export.py" in body


def test_materialize_property_tour_export_manifest_reports_ready_drop_status(tmp_path: Path) -> None:
    tour_root = tmp_path / "public_tours"
    incoming_root = tmp_path / "incoming"
    _write_base_tour(tour_root, "needs-krpano")
    manifest = build_export_manifest(
        tour_root=tour_root,
        incoming_root=incoming_root,
        providers={"krpano"},
        limit_per_provider=1,
    )
    krpano_row = next(row for row in manifest["imports"] if row["provider"] == "krpano")
    asset_dir = Path(krpano_row["asset_dir"])
    asset_dir.mkdir(parents=True)
    _write_equirectangular_image(asset_dir / "panorama.jpg")

    status_rows = build_drop_status_rows({"imports": [krpano_row]})

    assert status_rows == [
        {
            "slug": "needs-krpano",
            "provider": "krpano",
            "export_dir": str(asset_dir.resolve()),
            "status": "ready_for_import",
            "file_count": 1,
            "present_sample": ["panorama.jpg"],
            "missing": [],
            "accepted_entry": "panorama.jpg",
        }
    ]


def test_materialize_property_tour_export_manifest_names_missing_krpano_as_real_assets(tmp_path: Path) -> None:
    tour_root = tmp_path / "public_tours"
    incoming_root = tmp_path / "incoming"
    _write_base_tour(tour_root, "needs-real-krpano")

    manifest = build_export_manifest(
        tour_root=tour_root,
        incoming_root=incoming_root,
        providers={"krpano"},
        limit_per_provider=1,
    )
    krpano_row = next(row for row in manifest["imports"] if row["provider"] == "krpano")
    asset_dir = Path(krpano_row["asset_dir"])
    asset_dir.mkdir(parents=True)

    status_rows = build_drop_status_rows({"imports": [krpano_row]})

    assert status_rows[0]["missing"] == ["krpano_real_panorama_or_real_cubemap_faces"]


def test_materialize_property_tour_export_manifest_cli_writes_receipt(tmp_path: Path) -> None:
    tour_root = tmp_path / "public_tours"
    incoming_root = tmp_path / "incoming"
    output = tmp_path / "manifest.json"
    _write_base_tour(tour_root, "cli-needs-exports")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "materialize_property_tour_export_manifest.py"),
            "--tour-root",
            str(tour_root),
            "--incoming-root",
            str(incoming_root),
            "--write",
            str(output),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["status"] == "waiting_for_verified_assets"
    assert manifest["import_count"] == 4
    assert len(manifest["drop_status"]) == 4
    assert manifest["drop_status_summary"] == {"ready_for_import": 0, "waiting_for_assets": 4, "other": 0}
    assert '"drop_status_summary"' in result.stdout
    assert "cli-needs-exports" in result.stdout


def test_materialize_property_tour_export_manifest_cli_can_prepare_drop_dirs(tmp_path: Path) -> None:
    tour_root = tmp_path / "public_tours"
    incoming_root = tmp_path / "incoming"
    output = tmp_path / "manifest.json"
    _write_base_tour(tour_root, "cli-prepares-exports")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "materialize_property_tour_export_manifest.py"),
            "--tour-root",
            str(tour_root),
            "--incoming-root",
            str(incoming_root),
            "--prepare-dirs",
            "--write",
            str(output),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert len(manifest["prepared_drop_dirs"]) == 4
    assert all(Path(row["readme"]).is_file() for row in manifest["prepared_drop_dirs"])
    assert all(row["drop_status"]["status"] == "waiting_for_assets" for row in manifest["prepared_drop_dirs"])
